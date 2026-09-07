"""Trusted, explicit recovery policies; never model-supplied authorization."""
from dataclasses import dataclass
from typing import Any, Protocol

from .models import _MISSING, Operation


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    decision_id: str
    reason: str
    result: Any = _MISSING


class RecoveryPolicy(Protocol):
    def __call__(self, operation: Operation) -> RecoveryDecision | None:
        """Return a decision backed by provider evidence, or None to abstain.

        Timeout or failed lookup is not evidence of absence. Reuse IDs only when
        redelivering the same decision."""
        ...
