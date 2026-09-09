"""Effect records with host-owned identity and server-owned execution authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class OperationConflict(ValueError):
    """An operation binding, version, or recovery decision conflicts."""


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _json(value: Any) -> str:
    # Reject Python coercions (integer dict keys, tuples) before persisting input.
    def validate(item: Any) -> None:
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                validate(child)
        elif type(item) is list:
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("Only JSON values are supported")
    try:
        validate(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, RecursionError) as exc:
        raise ValueError("Value must be finite, acyclic JSON") from exc


@dataclass(frozen=True)
class Operation:
    scope: str
    operation_id: str
    effect: str
    request: dict[str, Any]
    provider_key: str
    state: str
    attempt: int
    version: int
    result: Any
    error: str | None
    # When the claim was first committed, for operator triage only. None on rows
    # written before the column existed. Age never settles an operation.
    created_at: str | None = None

    def response(self) -> dict[str, Any]:
        """Return public status without request or provider key.

        in_flight is not proof of worker liveness; elapsed time never grants retry."""
        return {
            "operation_id": self.operation_id,
            "state": self.state,
            "attempt": self.attempt,
            "version": self.version,
            "unresolved": self.state in ("in_flight", "indeterminate"),
            "next_action": {"in_flight": "wait", "indeterminate": "reconcile",
                            "ready": "execute", "completed": "use_result"}[self.state],
            "result": self.result,
            "error": self.error,
        }


_MISSING = object()
