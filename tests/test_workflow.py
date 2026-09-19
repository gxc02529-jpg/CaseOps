from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from caseops.application import CaseOpsService
from caseops.domain.models import ApprovalDecision, ExecutionMode, KnowledgeStatus, Ticket, TicketStatus
from caseops.governance.approval import ApprovalGate
from caseops.governance.scope import DataScope, ScopeViolation
from caseops.repositories import InMemoryCaseStore
from caseops.retrieval import HybridRetriever, KnowledgeDocument
from caseops.tickets import TicketAggregator
from caseops.providers import DiagnosisOutput


def complete_ticket(**changes) -> Ticket:
    values = {
        "tenant_id": "acme",
        "requester_id": "user-1",
        "subject": "升级后无法登录",
        "description": "登录页循环跳转并返回 401",
        "component": "auth",
        "product_version": "2.1.0",
        "reproduction_steps": "输入账号密码后点击登录",
        "impact_scope": "三个企业账号",
        "order_scope": ("order-1",),
        "tags": (),
    }
    values.update(changes)
    return Ticket(**values)


def build_service() -> CaseOpsService:
    documents = [
        KnowledgeDocument(
            id="doc-auth-1",
            tenant_id="acme",
            title="2.1.0 登录循环处理",
            content="清理旧会话缓存并重新签发登录令牌。",
            order_scope=("order-1",),
        ),
        KnowledgeDocument(
            id="doc-secret",
            tenant_id="other",
            title="其他租户私有方案",
            content="不可跨租户检索。",
        ),
    ]
    return CaseOpsService(
        store=InMemoryCaseStore(),
        aggregator=TicketAggregator(threshold=0.5),
        retriever=HybridRetriever(documents),
        approval_gate=ApprovalGate(frozenset({"refund", "delete_data", "change_permission"})),
    )


def admin_scope() -> DataScope:
    return DataScope("acme", "agent-1", order_scope=("order-1",), roles=("tenant_admin",))


def test_aggregator_merges_shared_diagnosis_but_keeps_ticket_ids() -> None:
    first = complete_ticket(id="tkt-a", requester_id="user-a")
    second = complete_ticket(id="tkt-b", requester_id="user-b")
    bundle = TicketAggregator(threshold=0.5).aggregate(first, [first, second])
    assert bundle.shared_diagnosis is True
    assert bundle.ticket_ids == ("tkt-a", "tkt-b")
    assert "same_component" in bundle.match_reasons


def test_aggregator_rejects_old_and_cross_tenant_tickets() -> None:
    now = datetime.now(UTC)
    primary = complete_ticket(id="tkt-primary", created_at=now)
    old = complete_ticket(id="tkt-old", created_at=now - timedelta(days=10))
    foreign = complete_ticket(id="tkt-foreign", tenant_id="other", created_at=now)
    bundle = TicketAggregator(threshold=0.1, window_hours=72).aggregate(primary, [old, foreign])
    assert bundle.ticket_ids == ("tkt-primary",)


def test_missing_context_routes_to_clarify() -> None:
    service = build_service()
    scope = admin_scope()
    ticket = complete_ticket(product_version=None, reproduction_steps=None)
    service.intake(ticket, scope)
    result = service.process(ticket.id, scope)
    assert result.mode is ExecutionMode.CLARIFY
    assert result.status is TicketStatus.NEEDS_CLARIFICATION
    assert len(result.clarification_questions) == 2


def test_known_issue_routes_to_single_and_filters_evidence() -> None:
    service = build_service()
    scope = admin_scope()
    ticket = complete_ticket(tags=("known_issue",))
    service.intake(ticket, scope)
    result = service.process(ticket.id, scope)
    assert result.mode is ExecutionMode.SINGLE
    assert result.status is TicketStatus.DISPATCHED
    assert all(item.tenant_id == "acme" for item in result.evidence)
    assert "doc-secret" not in {item.document_id for item in result.evidence}


def test_sensitive_action_waits_for_approval() -> None:
    service = build_service()
    scope = admin_scope()
    ticket = complete_ticket(tags=("refund_request",))
    service.intake(ticket, scope)
    result = service.process(ticket.id, scope)
    assert result.status is TicketStatus.PENDING_APPROVAL
    assert result.dispatched_ticket_ids == ()
    approved = service.approve(result.case_id, ApprovalDecision(True, "lead-1"), scope)
    assert approved.status is TicketStatus.DISPATCHED
    assert approved.dispatched_ticket_ids == (ticket.id,)


def test_sensitive_model_action_cannot_bypass_approval() -> None:
    class RefundDiagnoser:
        def diagnose(self, _tickets, _evidence) -> DiagnosisOutput:
            return DiagnosisOutput(
                summary="建议退款",
                proposed_action="refund",
                confidence=0.8,
                cited_evidence_ids=[],
            )

    service = build_service()
    service.diagnoser = RefundDiagnoser()
    scope = admin_scope()
    ticket = complete_ticket(tags=())
    service.intake(ticket, scope)
    result = service.process(ticket.id, scope)
    assert result.status is TicketStatus.PENDING_APPROVAL
    assert result.proposed_action == "refund"


def test_closed_case_requires_review_before_publication() -> None:
    service = build_service()
    scope = admin_scope()
    ticket = complete_ticket(tags=("known_issue",))
    service.intake(ticket, scope)
    result = service.process(ticket.id, scope)
    candidate = service.close_case(result.case_id, "清理缓存并重新签发令牌", scope)
    assert candidate.status is KnowledgeStatus.CANDIDATE
    reviewed = service.review_knowledge(
        candidate.id,
        approved=True,
        reviewer_id="reviewer-1",
        scope=scope,
        publish=True,
    )
    assert reviewed.status is KnowledgeStatus.PUBLISHED
    hits = service.retriever.search("清理缓存", scope)
    assert candidate.id in {item.document_id for item in hits}


def test_scope_blocks_cross_tenant_intake() -> None:
    service = build_service()
    with pytest.raises(ScopeViolation):
        service.intake(complete_ticket(tenant_id="other"), admin_scope())
