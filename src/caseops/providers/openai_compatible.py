"""Validated diagnosis through DeepSeek or another OpenAI-compatible provider."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from caseops.domain.models import Evidence, Ticket


class DiagnosisOutput(BaseModel):
    summary: str = Field(min_length=1, max_length=1200)
    proposed_action: str = Field(default="troubleshoot", min_length=1, max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)
    cited_evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class Diagnoser(Protocol):
    def diagnose(self, tickets: Sequence[Ticket], evidence: Sequence[Evidence]) -> DiagnosisOutput: ...


class RuleBasedDiagnoser:
    """Offline fallback that keeps local development deterministic."""

    def diagnose(self, tickets: Sequence[Ticket], evidence: Sequence[Evidence]) -> DiagnosisOutput:
        shared = len(tickets) > 1
        summary = "同源工单已完成统一研判" if shared else "工单已完成研判"
        if evidence:
            summary += f"，可引用 {len(evidence)} 条租户范围内的知识依据。"
        else:
            summary += "，当前没有足够知识依据，建议人工复核。"
        return DiagnosisOutput(
            summary=summary,
            proposed_action="troubleshoot",
            confidence=0.75 if evidence else 0.35,
            cited_evidence_ids=[item.document_id for item in evidence[:3]],
        )


class OpenAICompatibleDiagnoser:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_attempts: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def diagnose(self, tickets: Sequence[Ticket], evidence: Sequence[Evidence]) -> DiagnosisOutput:
        allowed_evidence = {item.document_id for item in evidence}
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 SaaS 技术支持研判助手。只使用给定证据，输出 JSON："
                        "summary、proposed_action、confidence、cited_evidence_ids。"
                        "证据不足时降低 confidence，不得编造引用。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tickets": [
                                {
                                    "id": item.id,
                                    "subject": item.subject,
                                    "description": item.description,
                                    "component": item.component,
                                    "product_version": item.product_version,
                                }
                                for item in tickets
                            ],
                            "evidence": [
                                {
                                    "id": item.document_id,
                                    "title": item.title,
                                    "excerpt": item.excerpt,
                                }
                                for item in evidence
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }

        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                result = DiagnosisOutput.model_validate_json(content)
                if not set(result.cited_evidence_ids).issubset(allowed_evidence):
                    raise ValueError("model cited evidence outside the retrieved set")
                return result
            except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
                last_error = exc
        raise RuntimeError("model diagnosis failed validation after retries") from last_error

