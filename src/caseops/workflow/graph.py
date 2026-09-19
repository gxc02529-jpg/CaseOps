"""LangGraph state machine for SINGLE, PIPELINE, and CLARIFY execution."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from caseops.agents.router import ExecutionRouter
from caseops.domain.models import ExecutionMode, Ticket


class TicketWorkflowState(TypedDict, total=False):
    tickets: list[Ticket]
    mode: ExecutionMode
    route_reason: str
    missing_fields: list[str]
    clarification_questions: list[str]
    planned_steps: list[str]


def build_routing_graph(router: ExecutionRouter | None = None):
    """Compile the explicit routing graph used by the application service."""

    policy = router or ExecutionRouter()

    def inspect(state: TicketWorkflowState) -> TicketWorkflowState:
        plan = policy.plan(state["tickets"])
        return {
            "mode": plan.mode,
            "route_reason": plan.reason,
            "missing_fields": list(plan.missing_fields),
        }

    def clarify(state: TicketWorkflowState) -> TicketWorkflowState:
        plan = policy.plan(state["tickets"])
        return {
            "clarification_questions": list(policy.clarification_questions(plan)),
            "planned_steps": ["request_missing_context"],
        }

    def single(_: TicketWorkflowState) -> TicketWorkflowState:
        return {"planned_steps": ["apply_playbook", "dispatch_independently"]}

    def pipeline(_: TicketWorkflowState) -> TicketWorkflowState:
        return {
            "planned_steps": [
                "retrieve_evidence",
                "diagnose_shared_cause",
                "check_approval_gate",
                "dispatch_independently",
            ]
        }

    def select_mode(state: TicketWorkflowState) -> str:
        return str(state["mode"])

    builder = StateGraph(TicketWorkflowState)
    builder.add_node("inspect", inspect)
    builder.add_node("clarify", clarify)
    builder.add_node("single", single)
    builder.add_node("pipeline", pipeline)
    builder.add_edge(START, "inspect")
    builder.add_conditional_edges(
        "inspect",
        select_mode,
        {
            ExecutionMode.CLARIFY.value: "clarify",
            ExecutionMode.SINGLE.value: "single",
            ExecutionMode.PIPELINE.value: "pipeline",
        },
    )
    builder.add_edge("clarify", END)
    builder.add_edge("single", END)
    builder.add_edge("pipeline", END)
    return builder.compile()

