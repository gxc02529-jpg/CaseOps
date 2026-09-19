"""Coordinate scoped intake, aggregation, graph routing, retrieval, and approval."""

from __future__ import annotations

from caseops.agents.router import ExecutionRouter
from caseops.domain.models import (
    ApprovalDecision,
    AuditEvent,
    ExecutionMode,
    Ticket,
    TicketStatus,
    WorkflowResult,
)
from caseops.governance.approval import ApprovalGate
from caseops.governance.scope import DataScope
from caseops.repositories.memory import InMemoryCaseStore
from caseops.retrieval.hybrid import HybridRetriever
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
    ) -> None:
        self.store = store
        self.aggregator = aggregator
        self.retriever = retriever
        self.approval_gate = approval_gate
        self.router = router or ExecutionRouter()
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
        proposal = self.approval_gate.propose(tickets)
        approval_required = self.approval_gate.requires_approval(proposal)
        status = TicketStatus.PENDING_APPROVAL if approval_required else TicketStatus.DISPATCHED
        for ticket in tickets:
            ticket.touch(status)

        result = WorkflowResult(
            case_id=case.id,
            mode=mode,
            status=status,
            ticket_ids=case.ticket_ids,
            summary=_summary(case.shared_diagnosis, evidence),
            evidence=evidence,
            proposed_action=proposal.action,
            approval_required=approval_required,
            dispatched_ticket_ids=() if approval_required else case.ticket_ids,
            metadata={
                "route_reason": graph_state["route_reason"],
                "planned_steps": graph_state["planned_steps"],
                "match_reasons": list(case.match_reasons),
                "action_reason": proposal.reason,
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


def _summary(shared_diagnosis: bool, evidence: tuple) -> str:
    diagnosis = "已合并同源工单进行统一研判" if shared_diagnosis else "已完成单工单研判"
    evidence_note = f"，检索到 {len(evidence)} 条可追溯依据" if evidence else "，未检索到高相关依据"
    return diagnosis + evidence_note + "；后续仍按用户工单独立处置。"

