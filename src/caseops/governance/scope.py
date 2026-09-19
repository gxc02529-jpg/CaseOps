"""Request-scoped authorization that follows data through every workflow stage."""

from __future__ import annotations

from dataclasses import dataclass

from caseops.domain.models import Evidence, Ticket


class ScopeViolation(PermissionError):
    """Raised when an entity crosses a tenant or order boundary."""


@dataclass(frozen=True, slots=True)
class DataScope:
    tenant_id: str
    actor_id: str
    order_scope: tuple[str, ...] = ()
    roles: tuple[str, ...] = ("support_agent",)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")

    @property
    def is_privileged(self) -> bool:
        return bool({"tenant_admin", "security_reviewer"}.intersection(self.roles))

    def allows_order_scope(self, entity_scope: tuple[str, ...]) -> bool:
        if self.is_privileged or not entity_scope:
            return True
        if not self.order_scope:
            return False
        return bool(set(self.order_scope).intersection(entity_scope))

    def assert_ticket(self, ticket: Ticket) -> None:
        if ticket.tenant_id != self.tenant_id:
            raise ScopeViolation("ticket tenant does not match request tenant")
        if not self.allows_order_scope(ticket.order_scope):
            raise ScopeViolation("ticket is outside the actor's order scope")

    def allows_evidence(self, evidence: Evidence) -> bool:
        return evidence.tenant_id == self.tenant_id and self.allows_order_scope(evidence.order_scope)

    def milvus_filter(self) -> str:
        """Build the mandatory portion of a Milvus expression for adapter use."""

        tenant = self.tenant_id.replace('"', '\\"')
        clauses = [f'tenant_id == "{tenant}"']
        if self.order_scope and not self.is_privileged:
            values = ", ".join(f'"{item.replace(chr(34), chr(92) + chr(34))}"' for item in self.order_scope)
            clauses.append(f"order_id in [{values}]")
        return " and ".join(clauses)

