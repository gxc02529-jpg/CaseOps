from __future__ import annotations

import httpx
import pytest

from caseops.domain.models import Evidence, Ticket
from caseops.providers import OpenAICompatibleDiagnoser


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def post(self, *_args, **_kwargs) -> httpx.Response:
        self.calls += 1
        request = httpx.Request("POST", "https://model.example/v1/chat/completions")
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": self.content}}]},
        )


def ticket() -> Ticket:
    return Ticket(tenant_id="acme", requester_id="u1", subject="登录失败", description="返回 401")


def evidence() -> Evidence:
    return Evidence("doc-1", "登录方案", "清理缓存", 0.9, "acme")


def test_model_output_is_validated() -> None:
    client = FakeClient(
        '{"summary":"依据文档处理","proposed_action":"troubleshoot",'
        '"confidence":0.9,"cited_evidence_ids":["doc-1"]}'
    )
    provider = OpenAICompatibleDiagnoser(
        base_url="https://model.example/v1",
        api_key="test",
        model="test-model",
        client=client,
    )
    result = provider.diagnose([ticket()], [evidence()])
    assert result.confidence == 0.9
    assert client.calls == 1


def test_model_cannot_cite_unretrieved_evidence() -> None:
    client = FakeClient(
        '{"summary":"伪造引用","proposed_action":"troubleshoot",'
        '"confidence":0.9,"cited_evidence_ids":["doc-other"]}'
    )
    provider = OpenAICompatibleDiagnoser(
        base_url="https://model.example/v1",
        api_key="test",
        model="test-model",
        max_attempts=1,
        client=client,
    )
    with pytest.raises(RuntimeError, match="failed validation"):
        provider.diagnose([ticket()], [evidence()])

