"""FastAPI surface for the CaseOps reference service."""

from __future__ import annotations

from dataclasses import asdict

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from caseops.application.service import CaseOpsService
from caseops.domain.models import ApprovalDecision, Ticket
from caseops.governance.approval import ApprovalGate
from caseops.governance.scope import DataScope
from caseops.repositories.memory import InMemoryCaseStore
from caseops.retrieval.hybrid import HybridRetriever
from caseops.providers.openai_compatible import OpenAICompatibleDiagnoser, RuleBasedDiagnoser
from caseops.settings import get_settings
from caseops.tickets.aggregation import TicketAggregator


class TicketCreate(BaseModel):
    requester_id: str
    subject: str
    description: str
    component: str | None = None
    product_version: str | None = None
    reproduction_steps: str | None = None
    impact_scope: str | None = None
    order_scope: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ProcessRequest(BaseModel):
    primary_ticket_id: str


class ApprovalRequest(BaseModel):
    approved: bool
    note: str = ""


class ResolutionRequest(BaseModel):
    resolution: str = Field(min_length=1)


class KnowledgeReviewRequest(BaseModel):
    approved: bool
    publish: bool = False


def _build_service() -> CaseOpsService:
    settings = get_settings()
    diagnoser = (
        OpenAICompatibleDiagnoser(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            model=settings.model_name,
        )
        if settings.model_api_key
        else RuleBasedDiagnoser()
    )
    return CaseOpsService(
        store=InMemoryCaseStore(),
        aggregator=TicketAggregator(
            threshold=settings.similarity_threshold,
            window_hours=settings.aggregation_window_hours,
        ),
        retriever=HybridRetriever(),
        approval_gate=ApprovalGate(settings.approval_action_set),
        diagnoser=diagnoser,
    )


def request_scope(
    tenant_id: str = Header(alias="X-Tenant-ID"),
    actor_id: str = Header(alias="X-Actor-ID"),
    roles: str = Header(default="support_agent", alias="X-Roles"),
    order_scope: str = Header(default="", alias="X-Order-Scope"),
) -> DataScope:
    return DataScope(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=tuple(item.strip() for item in roles.split(",") if item.strip()),
        order_scope=tuple(item.strip() for item in order_scope.split(",") if item.strip()),
    )


def create_app(service: CaseOpsService | None = None) -> FastAPI:
    app = FastAPI(title="CaseOps API", version="0.1.0")
    app.state.service = service or _build_service()

    def current_service() -> CaseOpsService:
        return app.state.service

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/tickets", status_code=201)
    def create_ticket(
        payload: TicketCreate,
        scope: DataScope = Depends(request_scope),
        caseops: CaseOpsService = Depends(current_service),
    ) -> dict:
        ticket = Ticket(
            tenant_id=scope.tenant_id,
            requester_id=payload.requester_id,
            subject=payload.subject,
            description=payload.description,
            component=payload.component,
            product_version=payload.product_version,
            reproduction_steps=payload.reproduction_steps,
            impact_scope=payload.impact_scope,
            order_scope=tuple(payload.order_scope),
            tags=tuple(payload.tags),
        )
        try:
            return asdict(caseops.intake(ticket, scope))
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/v1/cases/process")
    def process_case(
        payload: ProcessRequest,
        scope: DataScope = Depends(request_scope),
        caseops: CaseOpsService = Depends(current_service),
    ) -> dict:
        try:
            return asdict(caseops.process(payload.primary_ticket_id, scope))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/v1/cases/{case_id}/approval")
    def approve_case(
        case_id: str,
        payload: ApprovalRequest,
        scope: DataScope = Depends(request_scope),
        caseops: CaseOpsService = Depends(current_service),
    ) -> dict:
        try:
            decision = ApprovalDecision(payload.approved, scope.actor_id, payload.note)
            return asdict(caseops.approve(case_id, decision, scope))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/cases/{case_id}/close", status_code=201)
    def close_case(
        case_id: str,
        payload: ResolutionRequest,
        scope: DataScope = Depends(request_scope),
        caseops: CaseOpsService = Depends(current_service),
    ) -> dict:
        try:
            return asdict(caseops.close_case(case_id, payload.resolution, scope))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/knowledge/{candidate_id}/review")
    def review_knowledge(
        candidate_id: str,
        payload: KnowledgeReviewRequest,
        scope: DataScope = Depends(request_scope),
        caseops: CaseOpsService = Depends(current_service),
    ) -> dict:
        try:
            return asdict(
                caseops.review_knowledge(
                    candidate_id,
                    approved=payload.approved,
                    reviewer_id=scope.actor_id,
                    scope=scope,
                    publish=payload.publish,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("caseops.api:app", host=settings.api_host, port=settings.api_port, reload=False)
