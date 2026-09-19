"""Tenant-safe sparse+dense retrieval with an injectable production reranker."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from caseops.domain.models import Evidence
from caseops.governance.scope import DataScope


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    id: str
    tenant_id: str
    title: str
    content: str
    order_scope: tuple[str, ...] = ()
    source_uri: str | None = None


Reranker = Callable[[str, list[Evidence]], list[Evidence]]


class HybridRetriever:
    """In-memory reference adapter mirroring the BM25+dense+rerank production path."""

    def __init__(
        self,
        documents: Iterable[KnowledgeDocument] = (),
        *,
        sparse_weight: float = 0.45,
        dense_weight: float = 0.55,
        reranker: Reranker | None = None,
    ) -> None:
        if not math.isclose(sparse_weight + dense_weight, 1.0, abs_tol=1e-9):
            raise ValueError("retrieval weights must add up to 1.0")
        self._documents = list(documents)
        self.sparse_weight = sparse_weight
        self.dense_weight = dense_weight
        self.reranker = reranker

    def add(self, document: KnowledgeDocument) -> None:
        self._documents.append(document)

    def search(self, query: str, scope: DataScope, *, top_k: int = 5) -> tuple[Evidence, ...]:
        candidates = [document for document in self._documents if _document_allowed(document, scope)]
        if not candidates or not query.strip():
            return ()

        corpus_tokens = [_tokens(document.title + " " + document.content) for document in candidates]
        query_tokens = _tokens(query)
        document_frequency = Counter(token for tokens in corpus_tokens for token in set(tokens))

        evidence: list[Evidence] = []
        for document, tokens in zip(candidates, corpus_tokens, strict=True):
            sparse = _bm25_lite(query_tokens, tokens, document_frequency, len(candidates))
            dense = _cosine_overlap(query_tokens, tokens)
            score = self.sparse_weight * sparse + self.dense_weight * dense
            evidence.append(
                Evidence(
                    document_id=document.id,
                    title=document.title,
                    excerpt=document.content[:320],
                    score=round(score, 6),
                    tenant_id=document.tenant_id,
                    order_scope=document.order_scope,
                    source_uri=document.source_uri,
                )
            )

        ranked = sorted(evidence, key=lambda item: item.score, reverse=True)
        if self.reranker is not None:
            ranked = self.reranker(query, ranked)
        return tuple(item for item in ranked[:top_k] if item.score > 0.0)


def _document_allowed(document: KnowledgeDocument, scope: DataScope) -> bool:
    if document.tenant_id != scope.tenant_id:
        return False
    return scope.allows_order_scope(document.order_scope)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold())


def _bm25_lite(query: list[str], document: list[str], df: Counter[str], corpus_size: int) -> float:
    if not query or not document:
        return 0.0
    frequencies = Counter(document)
    score = 0.0
    for token in set(query):
        term_frequency = frequencies[token] / (frequencies[token] + 1.2) if frequencies[token] else 0.0
        inverse_frequency = math.log(1.0 + (corpus_size - df[token] + 0.5) / (df[token] + 0.5))
        score += term_frequency * inverse_frequency
    return score / (score + 1.0)


def _cosine_overlap(query: list[str], document: list[str]) -> float:
    if not query or not document:
        return 0.0
    left = Counter(query)
    right = Counter(document)
    dot = sum(left[token] * right[token] for token in left.keys() & right.keys())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

