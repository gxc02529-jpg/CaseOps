"""Deterministic in-memory persistence used by tests and the local demo API."""

from __future__ import annotations

from collections import defaultdict
from threading import RLock

from caseops.domain.models import AuditEvent, CaseBundle, KnowledgeCandidate, Ticket, WorkflowResult


class InMemoryCaseStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._tickets: dict[str, Ticket] = {}
        self._cases: dict[str, CaseBundle] = {}
        self._results: dict[str, WorkflowResult] = {}
        self._knowledge: dict[str, KnowledgeCandidate] = {}
        self._audit: dict[str, list[AuditEvent]] = defaultdict(list)

    def add_ticket(self, ticket: Ticket) -> Ticket:
        with self._lock:
            if ticket.id in self._tickets:
                raise ValueError(f"ticket already exists: {ticket.id}")
            self._tickets[ticket.id] = ticket
            return ticket

    def get_ticket(self, ticket_id: str) -> Ticket:
        try:
            return self._tickets[ticket_id]
        except KeyError as exc:
            raise KeyError(f"ticket not found: {ticket_id}") from exc

    def list_tickets(self, tenant_id: str) -> tuple[Ticket, ...]:
        return tuple(item for item in self._tickets.values() if item.tenant_id == tenant_id)

    def save_case(self, case: CaseBundle) -> CaseBundle:
        with self._lock:
            self._cases[case.id] = case
            return case

    def get_case(self, case_id: str) -> CaseBundle:
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise KeyError(f"case not found: {case_id}") from exc

    def save_result(self, result: WorkflowResult) -> WorkflowResult:
        with self._lock:
            self._results[result.case_id] = result
            return result

    def get_result(self, case_id: str) -> WorkflowResult:
        try:
            return self._results[case_id]
        except KeyError as exc:
            raise KeyError(f"workflow result not found: {case_id}") from exc

    def save_knowledge(self, candidate: KnowledgeCandidate) -> KnowledgeCandidate:
        with self._lock:
            self._knowledge[candidate.id] = candidate
            return candidate

    def get_knowledge(self, candidate_id: str) -> KnowledgeCandidate:
        try:
            return self._knowledge[candidate_id]
        except KeyError as exc:
            raise KeyError(f"knowledge candidate not found: {candidate_id}") from exc

    def list_knowledge(self, tenant_id: str) -> tuple[KnowledgeCandidate, ...]:
        return tuple(item for item in self._knowledge.values() if item.tenant_id == tenant_id)

    def add_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._audit[event.entity_id].append(event)

    def audit_for(self, entity_id: str) -> tuple[AuditEvent, ...]:
        return tuple(self._audit.get(entity_id, ()))

