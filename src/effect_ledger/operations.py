"""Framework-free execution authority, composed with an atomic durable store."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .models import _MISSING, Operation, OperationConflict, _json, _text
from .recovery import RecoveryPolicy
from .store import OperationStore


class EffectExecutor:
    """Execute single effects in one scope. All hosts must share its durable store."""

    def __init__(self, path: str | Path | None = None, *, scope: str,
                 store: OperationStore | None = None,
                 recovery: RecoveryPolicy | None = None) -> None:
        self.scope = _text(scope, "scope")
        if (path is None) == (store is None):
            raise ValueError("Supply exactly one of path or store")
        if store is None:
            from .sqlite import SQLiteOperationStore
            store = SQLiteOperationStore(path)
        self.store = store
        self.recovery = recovery
        self.path = getattr(store, "path", None)

    def get(self, operation_id: str) -> Operation | None:
        return self.store.get(self.scope, _text(operation_id, "operation_id"))

    def unresolved(self, *, limit: int = 50) -> list[Operation]:
        """List operations in this scope awaiting a decision. Reading grants nothing."""
        return self.store.unresolved(self.scope, limit=limit)

    def bind(self, effect: str, handler: Callable[[Operation], Any]) -> Callable:
        """Bind an explicit effect; returned callable accepts (operation_id, request)."""
        _text(effect, "effect")
        def execute(operation_id: str, request: dict[str, Any]) -> Operation:
            return self.execute(operation_id, effect, request, handler)
        return execute

    def execute(self, operation_id: str, effect: str, request: dict[str, Any],
                handler: Callable[[Operation], Any]) -> Operation:
        """Commit a claim before dispatch; replay completion or block duplicates.

        Handler errors become indeterminate; BaseException leaves in_flight.
        Storage errors and OperationConflict can follow a successful effect.
        Reconcile the same operation ID; an exception never authorizes retry."""
        _text(operation_id, "operation_id")
        _text(effect, "effect")
        if type(request) is not dict:
            raise ValueError("request must be a JSON object")
        claim = self.store.claim(self.scope, operation_id, effect, json.loads(_json(request)))
        if not claim.acquired:
            return claim.operation
        owned = claim.operation
        try:
            result = _json(handler(owned))
        except Exception as exc:
            return self.store.finish(owned, "indeterminate", None, type(exc).__name__)
        return self.store.finish(owned, "completed", result, None)

    async def aexecute(self, operation_id: str, effect: str, request: dict[str, Any],
                       handler: Callable[[Operation], Awaitable[Any]]) -> Operation:
        """Async execute with threaded store I/O. Cancellation never grants a retry."""
        _text(operation_id, "operation_id")
        _text(effect, "effect")
        if type(request) is not dict:
            raise ValueError("request must be a JSON object")
        claim = await asyncio.to_thread(self.store.claim, self.scope, operation_id,
                                        effect, json.loads(_json(request)))
        if not claim.acquired:
            return claim.operation
        owned = claim.operation
        try:
            result = _json(await handler(owned))
        except Exception as exc:
            return await asyncio.to_thread(self.store.finish, owned, "indeterminate",
                                            None, type(exc).__name__)
        return await asyncio.to_thread(self.store.finish, owned, "completed", result, None)

    def resolve(self, operation_id: str, *, expected_version: int, decision_id: str,
                action: str, reason: str, workers_stopped: bool,
                result: Any = _MISSING) -> Operation:
        """Apply a version-bound decision; identical replay cannot regrant execution.

        workers_stopped asserts old workers and outstanding requests were reconciled.
        This trusted API is not a remote fence and must not be a model tool."""
        return self.store.resolve(self.scope, operation_id,
            expected_version=expected_version, decision_id=decision_id, action=action,
            reason=reason, workers_stopped=workers_stopped, result=result)

    def recover(self, operation_id: str, *, workers_stopped: bool) -> Operation:
        """Consult the trusted policy for the current version without running a handler.

        Abstention and policy exceptions leave authority unchanged."""
        if workers_stopped is not True:
            raise ValueError("Confirm old workers cannot continue before recovering")
        record = self.get(operation_id)
        if record is None:
            raise OperationConflict("Unknown operation")
        if record.state not in ("in_flight", "indeterminate") or self.recovery is None:
            return record
        decision = self.recovery(record)
        if decision is None:
            return record
        return self.resolve(operation_id, expected_version=record.version,
            decision_id=decision.decision_id, action=decision.action,
            reason=decision.reason, workers_stopped=True, result=decision.result)
