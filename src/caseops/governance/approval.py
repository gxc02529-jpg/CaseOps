"""Human approval gate for irreversible or sensitive support actions."""

from __future__ import annotations

from dataclasses import dataclass

from caseops.domain.models import Ticket


_TAG_ACTIONS = {
    "refund_request": "refund",
    "delete_request": "delete_data",
    "permission_request": "change_permission",
}


@dataclass(frozen=True, slots=True)
class ActionProposal:
    action: str
    reason: str


class ApprovalGate:
    def __init__(self, approval_actions: frozenset[str]) -> None:
        self.approval_actions = approval_actions

    def propose(self, tickets: tuple[Ticket, ...], *, model_action: str | None = None) -> ActionProposal:
        tags = {tag.casefold() for ticket in tickets for tag in ticket.tags}
        for tag, action in _TAG_ACTIONS.items():
            if tag in tags:
                return ActionProposal(action=action, reason=f"ticket tag requests {action}")
        normalized_action = str(model_action or "").strip().casefold()
        if normalized_action and normalized_action != "troubleshoot":
            return ActionProposal(action=normalized_action, reason="validated diagnosis proposes this action")
        return ActionProposal(action="troubleshoot", reason="diagnostic response can be dispatched safely")

    def requires_approval(self, proposal: ActionProposal) -> bool:
        return proposal.action in self.approval_actions
