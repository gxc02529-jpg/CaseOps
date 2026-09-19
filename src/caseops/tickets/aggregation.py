"""Identify same-origin tickets while preserving independent user handling loops."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from datetime import timedelta

from caseops.domain.models import CaseBundle, Ticket

SemanticSimilarity = Callable[[str, str], float]


class TicketAggregator:
    def __init__(
        self,
        *,
        threshold: float = 0.58,
        window_hours: int = 72,
        semantic_similarity: SemanticSimilarity | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.threshold = threshold
        self.window = timedelta(hours=window_hours)
        self.semantic_similarity = semantic_similarity or _token_similarity

    def aggregate(self, primary: Ticket, candidates: Iterable[Ticket]) -> CaseBundle:
        matched = [primary]
        reasons: list[str] = []
        for candidate in candidates:
            if candidate.id == primary.id or candidate.tenant_id != primary.tenant_id:
                continue
            score, score_reasons = self.similarity(primary, candidate)
            if score >= self.threshold:
                matched.append(candidate)
                reasons.extend(score_reasons)

        unique_reasons = tuple(dict.fromkeys(reasons))
        return CaseBundle(
            tenant_id=primary.tenant_id,
            primary_ticket_id=primary.id,
            ticket_ids=tuple(item.id for item in sorted(matched, key=lambda item: item.created_at)),
            shared_diagnosis=len(matched) > 1,
            match_reasons=unique_reasons,
        )

    def similarity(self, left: Ticket, right: Ticket) -> tuple[float, tuple[str, ...]]:
        age = abs(left.created_at - right.created_at)
        if age > self.window:
            return 0.0, ("outside_time_window",)

        reasons: list[str] = []
        text_score = self.semantic_similarity(_ticket_text(left), _ticket_text(right))
        component_score = _exact_field_score(left.component, right.component)
        version_score = _exact_field_score(left.product_version, right.product_version)
        time_score = max(0.0, 1.0 - age.total_seconds() / self.window.total_seconds())

        if text_score >= 0.45:
            reasons.append("semantic_similarity")
        if component_score:
            reasons.append("same_component")
        if version_score:
            reasons.append("same_version")
        reasons.append("within_time_window")

        score = 0.65 * text_score + 0.15 * component_score + 0.15 * version_score + 0.05 * time_score
        return min(1.0, score), tuple(reasons)


def _ticket_text(ticket: Ticket) -> str:
    return " ".join(part for part in (ticket.subject, ticket.description, ticket.reproduction_steps or "") if part)


def _exact_field_score(left: str | None, right: str | None) -> float:
    left_value = str(left or "").strip().casefold()
    right_value = str(right or "").strip().casefold()
    return float(bool(left_value and right_value and left_value == right_value))


def _token_similarity(left: str, right: str) -> float:
    """Local fallback; production can inject BGE-M3 cosine similarity."""

    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens.intersection(right_tokens))
    denominator = math.sqrt(len(left_tokens) * len(right_tokens))
    return intersection / denominator if denominator else 0.0


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold()))

