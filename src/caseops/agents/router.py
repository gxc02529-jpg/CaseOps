"""Choose the smallest safe execution mode for a support case."""

from __future__ import annotations

from collections.abc import Iterable

from caseops.domain.models import ExecutionMode, RoutePlan, Ticket


_SINGLE_TAGS = frozenset({"faq", "known_issue", "password_reset", "status_query"})
_FIELD_QUESTIONS = {
    "product_version": "请补充出现问题的产品版本。",
    "reproduction_steps": "请提供可稳定复现问题的操作步骤。",
    "impact_scope": "请说明受影响的用户、订单或环境范围。",
}


class ExecutionRouter:
    """Deterministic routing gate; an LLM may enrich inputs but cannot bypass it."""

    def plan(self, tickets: Iterable[Ticket]) -> RoutePlan:
        items = tuple(tickets)
        if not items:
            raise ValueError("at least one ticket is required")

        missing = tuple(
            dict.fromkeys(field for ticket in items for field in ticket.missing_diagnostic_fields())
        )
        if missing:
            return RoutePlan(
                mode=ExecutionMode.CLARIFY,
                reason="required diagnostic context is missing",
                missing_fields=missing,
            )

        tags = {tag.casefold() for ticket in items for tag in ticket.tags}
        if len(items) == 1 and tags.intersection(_SINGLE_TAGS):
            return RoutePlan(
                mode=ExecutionMode.SINGLE,
                reason="a deterministic single-ticket playbook is available",
            )

        return RoutePlan(
            mode=ExecutionMode.PIPELINE,
            reason="the case needs retrieval, diagnosis, and per-ticket dispatch",
        )

    def clarification_questions(self, plan: RoutePlan) -> tuple[str, ...]:
        if plan.mode is not ExecutionMode.CLARIFY:
            return ()
        return tuple(_FIELD_QUESTIONS[field] for field in plan.missing_fields)

