"""Coordinate scoped intake, aggregation, graph routing, retrieval, and approval."""

from __future__ import annotations

from caseops.agents.router import ExecutionRouter
from caseops.domain.models import (
    ApprovalDecision,
    AuditEvent,
    ExecutionMode,
    KnowledgeCandidate,
    KnowledgeStatus,
    Ticket,
    TicketStatus,
    WorkflowResult,
)
from caseops.governance.approval import ApprovalGate
from caseops.governance.scope import DataScope
from caseops.repositories.memory import InMemoryCaseStore
from caseops.retrieval.hybrid import HybridRetriever
from caseops.providers.openai_compatible import Diagnoser, RuleBasedDiagnoser
from caseops.tickets.aggregation import TicketAggregator
from caseops.workflow.graph import build_routing_graph


class CaseOpsService:
    def __init__(
        self,
        *,
        store: InMemoryCaseStore,
        aggregator: TicketAggregator,
        retriever: HybridRetriever,
        approval_gate: ApprovalGate,
        router: ExecutionRouter | None = None,
        diagnoser: Diagnoser | None = None,
    ) -> None:
        self.store = store
        self.aggregator = aggregator
        self.retriever = retriever
        self.approval_gate = approval_gate
        self.router = router or ExecutionRouter()
        self.diagnoser = diagnoser or RuleBasedDiagnoser()
        self.graph = build_routing_graph(self.router)

    def intake(self, ticket: Ticket, scope: DataScope) -> Ticket:
        scope.assert_ticket(ticket)
        self.store.add_ticket(ticket)
        self._audit(scope, "ticket.intake", ticket.id, {"status": ticket.status.value})
        return ticket

    def process(self, primary_ticket_id: str, scope: DataScope) -> WorkflowResult:
        primary = self.store.get_ticket(primary_ticket_id)
        scope.assert_ticket(primary)
        accessible_candidates = [
            ticket
            for ticket in self.store.list_tickets(scope.tenant_id)
            if _is_accessible(ticket, scope)
        ]
        case = self.aggregator.aggregate(primary, accessible_candidates)
        self.store.save_case(case)
        tickets = tuple(self.store.get_ticket(ticket_id) for ticket_id in case.ticket_ids)
        graph_state = self.graph.invoke({"tickets": list(tickets)})
        mode = graph_state["mode"]

        if mode is ExecutionMode.CLARIFY:
            for ticket in tickets:
                ticket.touch(TicketStatus.NEEDS_CLARIFICATION)
            result = WorkflowResult(
                case_id=case.id,
                mode=mode,
                status=TicketStatus.NEEDS_CLARIFICATION,
                ticket_ids=case.ticket_ids,
                summary="关键信息不足，工作流已暂停并请求补齐。",
                clarification_questions=tuple(graph_state.get("clarification_questions", ())),
                metadata={"route_reason": graph_state["route_reason"]},
            )
            return self._save_result(scope, result)

        query = " ".join(f"{ticket.subject} {ticket.description}" for ticket in tickets)
        evidence = self.retriever.search(query, scope, top_k=5)
        diagnosis = self.diagnoser.diagnose(tickets, evidence)
        proposal = self.approval_gate.propose(tickets, model_action=diagnosis.proposed_action)
        approval_required = self.approval_gate.requires_approval(proposal)
        status = TicketStatus.PENDING_APPROVAL if approval_required else TicketStatus.DISPATCHED
        for ticket in tickets:
            ticket.touch(status)

        result = WorkflowResult(
            case_id=case.id,
            mode=mode,
            status=status,
            ticket_ids=case.ticket_ids,
            summary=diagnosis.summary,
            evidence=evidence,
            proposed_action=proposal.action,
            approval_required=approval_required,
            dispatched_ticket_ids=() if approval_required else case.ticket_ids,
            metadata={
                "route_reason": graph_state["route_reason"],
                "planned_steps": graph_state["planned_steps"],
                "match_reasons": list(case.match_reasons),
                "action_reason": proposal.reason,
                "diagnosis_confidence": diagnosis.confidence,
                "cited_evidence_ids": diagnosis.cited_evidence_ids,
                "model_proposed_action": diagnosis.proposed_action,
            },
        )
        return self._save_result(scope, result)

    def approve(self, case_id: str, decision: ApprovalDecision, scope: DataScope) -> WorkflowResult:
        result = self.store.get_result(case_id)
        case = self.store.get_case(case_id)
        if case.tenant_id != scope.tenant_id:
            raise PermissionError("case tenant does not match request tenant")
        if not result.approval_required:
            raise ValueError("case does not require approval")

        next_status = TicketStatus.DISPATCHED if decision.approved else TicketStatus.TRIAGED
        for ticket_id in result.ticket_ids:
            self.store.get_ticket(ticket_id).touch(next_status)
        result.status = next_status
        result.dispatched_ticket_ids = result.ticket_ids if decision.approved else ()
        result.metadata["approval"] = {
            "approved": decision.approved,
            "reviewer_id": decision.reviewer_id,
            "note": decision.note,
        }
        self.store.save_result(result)
        self._audit(scope, "case.approval", case_id, result.metadata["approval"])
        return result

    def close_case(self, case_id: str, resolution: str, scope: DataScope) -> KnowledgeCandidate:
        result = self.store.get_result(case_id)
        case = self.store.get_case(case_id)
        if case.tenant_id != scope.tenant_id:
            raise PermissionError("case tenant does not match request tenant")
        if result.status is not TicketStatus.DISPATCHED:
            raise ValueError("only a dispatched case can be closed")
        if not resolution.strip():
            raise ValueError("resolution must not be empty")

        tickets = tuple(self.store.get_ticket(ticket_id) for ticket_id in case.ticket_ids)
        for ticket in tickets:
            scope.assert_ticket(ticket)
            ticket.touch(TicketStatus.CLOSED)
        result.status = TicketStatus.CLOSED
        self.store.save_result(result)

        candidate = KnowledgeCandidate(
            tenant_id=scope.tenant_id,
            case_id=case_id,
            question=tickets[0].subject,
            answer=resolution.strip(),
            source_ticket_ids=case.ticket_ids,
            evidence_ids=tuple(item.document_id for item in result.evidence),
        )
        self.store.save_knowledge(candidate)
        self._audit(
            scope,
            "knowledge.candidate_created",
            candidate.id,
            {"case_id": case_id, "source_ticket_ids": list(case.ticket_ids)},
        )
        return candidate

    def review_knowledge(
        self,
        candidate_id: str,
        *,
        approved: bool,
        reviewer_id: str,
        scope: DataScope,
        publish: bool = False,
    ) -> KnowledgeCandidate:
        candidate = self.store.get_knowledge(candidate_id)
        if candidate.tenant_id != scope.tenant_id:
            raise PermissionError("knowledge tenant does not match request tenant")
        candidate.reviewed_by = reviewer_id
        candidate.status = KnowledgeStatus.APPROVED if approved else KnowledgeStatus.REJECTED
        if approved and publish:
            from caseops.retrieval.hybrid import KnowledgeDocument

            self.retriever.add(
                KnowledgeDocument(
                    id=candidate.id,
                    tenant_id=candidate.tenant_id,
                    title=candidate.question,
                    content=candidate.answer,
                    source_uri=f"caseops://cases/{candidate.case_id}",
                )
            )
            candidate.status = KnowledgeStatus.PUBLISHED
        self.store.save_knowledge(candidate)
        self._audit(
            scope,
            "knowledge.reviewed",
            candidate.id,
            {"approved": approved, "published": candidate.status is KnowledgeStatus.PUBLISHED},
        )
        return candidate

    def _save_result(self, scope: DataScope, result: WorkflowResult) -> WorkflowResult:
        self.store.save_result(result)
        self._audit(
            scope,
            "case.processed",
            result.case_id,
            {"mode": result.mode.value, "status": result.status.value},
        )
        return result

    def _audit(self, scope: DataScope, event_type: str, entity_id: str, payload: dict) -> None:
        self.store.add_audit(
            AuditEvent(
                tenant_id=scope.tenant_id,
                actor_id=scope.actor_id,
                event_type=event_type,
                entity_id=entity_id,
                payload=payload,
            )
        )


def _is_accessible(ticket: Ticket, scope: DataScope) -> bool:
    try:
        scope.assert_ticket(ticket)
    except PermissionError:
        return False
    return True
