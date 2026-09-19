"""Pure domain models shared by the API, workflow, and infrastructure adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutionMode(StrEnum):
    """How much orchestration a case needs before it can be dispatched."""

    SINGLE = "SINGLE"
    PIPELINE = "PIPELINE"
    CLARIFY = "CLARIFY"


class TicketStatus(StrEnum):
    NEW = "new"
    NEEDS_CLARIFICATION = "needs_clarification"
    TRIAGED = "triaged"
    PENDING_APPROVAL = "pending_approval"
    DISPATCHED = "dispatched"
    RESOLVED = "resolved"
    CLOSED = "closed"


class KnowledgeStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


@dataclass(slots=True)
class Ticket:
    tenant_id: str
    requester_id: str
    subject: str
    description: str
    component: str | None = None
    product_version: str | None = None
    reproduction_steps: str | None = None
    impact_scope: str | None = None
    order_scope: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: f"tkt_{uuid4().hex[:12]}")
    status: TicketStatus = TicketStatus.NEW
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def missing_diagnostic_fields(self) -> tuple[str, ...]:
        fields = {
            "product_version": self.product_version,
            "reproduction_steps": self.reproduction_steps,
            "impact_scope": self.impact_scope,
        }
        return tuple(name for name, value in fields.items() if not str(value or "").strip())

    def touch(self, status: TicketStatus) -> None:
        self.status = status
        self.updated_at = utc_now()


@dataclass(frozen=True, slots=True)
class CaseBundle:
    """A shared diagnosis with one independent handling loop per source ticket."""

    tenant_id: str
    primary_ticket_id: str
    ticket_ids: tuple[str, ...]
    shared_diagnosis: bool
    match_reasons: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: f"case_{uuid4().hex[:12]}")


@dataclass(frozen=True, slots=True)
class Evidence:
    document_id: str
    title: str
    excerpt: str
    score: float
    tenant_id: str
    order_scope: tuple[str, ...] = ()
    source_uri: str | None = None


@dataclass(frozen=True, slots=True)
class RoutePlan:
    mode: ExecutionMode
    reason: str
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    reviewer_id: str
    note: str = ""
    decided_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkflowResult:
    case_id: str
    mode: ExecutionMode
    status: TicketStatus
    ticket_ids: tuple[str, ...]
    summary: str
    clarification_questions: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    proposed_action: str | None = None
    approval_required: bool = False
    dispatched_ticket_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class KnowledgeCandidate:
    tenant_id: str
    case_id: str
    question: str
    answer: str
    source_ticket_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: f"kb_{uuid4().hex[:12]}")
    status: KnowledgeStatus = KnowledgeStatus.CANDIDATE
    created_at: datetime = field(default_factory=utc_now)
    reviewed_by: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    tenant_id: str
    event_type: str
    actor_id: str
    entity_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"evt_{uuid4().hex[:12]}")
    occurred_at: datetime = field(default_factory=utc_now)

