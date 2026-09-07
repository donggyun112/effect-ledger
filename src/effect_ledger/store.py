"""Atomic store contract; methods commit before returning.

claim binds request/effect/key and grants one attempt. finish checks its version.
resolve records the decision and transition atomically; replay cannot regrant.
Transactions must not span handlers or recovery policies."""
from dataclasses import dataclass
from typing import Any, Protocol

from .models import _MISSING, Operation


@dataclass(frozen=True)
class Claim:
    operation: Operation
    acquired: bool


class OperationStore(Protocol):
    def get(self, scope: str, operation_id: str) -> Operation | None: ...

    def unresolved(self, scope: str, *, limit: int) -> list[Operation]: ...

    def claim(self, scope: str, operation_id: str, effect: str,
              request: dict[str, Any]) -> Claim: ...

    def finish(self, owned: Operation, state: str, result: str | None,
               error: str | None) -> Operation: ...

    def resolve(self, scope: str, operation_id: str, *, expected_version: int,
                decision_id: str, action: str, reason: str, workers_stopped: bool,
                result: Any = _MISSING) -> Operation: ...
