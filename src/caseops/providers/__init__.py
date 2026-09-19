"""External model providers."""

from caseops.providers.openai_compatible import (
    DiagnosisOutput,
    OpenAICompatibleDiagnoser,
    RuleBasedDiagnoser,
)

__all__ = ["DiagnosisOutput", "OpenAICompatibleDiagnoser", "RuleBasedDiagnoser"]

