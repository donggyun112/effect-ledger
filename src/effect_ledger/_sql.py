"""Shared SQL state transitions; backend transactions provide serialization."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .models import _MISSING, Operation, OperationConflict, _json, _text
from .store import Claim

# Named explicitly rather than selected with *, so a ledger widened by a newer
# release still reads here instead of failing to build an Operation.
_COLUMNS = ("scope", "operation_id", "effect", "request", "provider_key", "state",
            "attempt", "version", "result", "error", "created_at")
_FIELDS = ", ".join(_COLUMNS)

# Append-only; the index of a step is the version it produces. Step 0 is the
# original shape, so a ledger written before versioning replays it as a no-op
# and reaches the same place as a fresh file by the same single path.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE IF NOT EXISTS operations (
            scope TEXT NOT NULL, operation_id TEXT NOT NULL,
            effect TEXT NOT NULL, request TEXT NOT NULL,
            provider_key TEXT NOT NULL, state TEXT NOT NULL,
            attempt INTEGER NOT NULL, version INTEGER NOT NULL,
            result TEXT, error TEXT,
            PRIMARY KEY (scope, operation_id)
        )""",
        """CREATE TABLE IF NOT EXISTS decisions (
            scope TEXT NOT NULL, decision_id TEXT NOT NULL,
            payload TEXT NOT NULL, decided_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (scope, decision_id)
        )""",
    ),
    # Text, written by the host, so both backends return the same sortable value.
    # Rows that predate the column keep NULL rather than a fabricated time.
    ("ALTER TABLE operations ADD COLUMN created_at TEXT",),
)
SCHEMA_VERSION = len(_MIGRATIONS)


class SQLStore:
    def _transaction(self, scope: str):
        raise NotImplementedError

    def _initialize(self):
        """Bring the ledger to SCHEMA_VERSION, or refuse to read a newer one.

        Reading a ledger a later release widened is the failure this stamp exists
        to name: without it the mismatch surfaces as a TypeError on every read,
        including the operator's own triage commands."""
        with self._transaction("__schema__") as db:
            db.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY, version INTEGER NOT NULL
            )""")
            row = db.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
            current = 0 if row is None else row["version"]
            if current > SCHEMA_VERSION:
                raise ValueError(
                    f"Ledger schema is v{current}; this build of effect-ledger "
                    f"understands v{SCHEMA_VERSION}. Upgrade the code rather than "
                    "reading the ledger with an older release.")
            for step in _MIGRATIONS[current:]:
                for statement in step:
                    db.execute(statement)
            if row is None:
                db.execute("INSERT INTO schema_version(id, version) VALUES (1, ?)",
                           (SCHEMA_VERSION,))
            elif current < SCHEMA_VERSION:
                db.execute("UPDATE schema_version SET version=? WHERE id=1", (SCHEMA_VERSION,))

    @staticmethod
    def _operation(row: Any) -> Operation:
        data = dict(row)
        data["request"] = json.loads(data["request"])
        data["result"] = json.loads(data["result"]) if data["result"] is not None else None
        return Operation(**data)

    def _get(self, db: Any, scope: str, operation_id: str) -> Operation | None:
        row = db.execute(
            f"SELECT {_FIELDS} FROM operations WHERE scope=? AND operation_id=?",
            (scope, operation_id),
        ).fetchone()
        return None if row is None else self._operation(row)

    def get(self, scope: str, operation_id: str) -> Operation | None:
        _text(operation_id, "operation_id")
        with self._transaction(scope) as db:
            return self._get(db, scope, operation_id)

    def unresolved(self, scope: str, *, limit: int) -> list[Operation]:
        """List operations awaiting a decision, oldest first. Read-only; grants nothing.

        Age orders the queue but settles nothing: the oldest row is the one to
        look at first, never the one a timer may retry. Rows written before
        created_at existed carry no time and sort ahead of every dated row."""
        _text(scope, "scope")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._transaction(scope) as db:
            rows = db.execute(
                f"SELECT {_FIELDS} FROM operations WHERE scope=? AND state IN ('in_flight', "
                "'indeterminate') ORDER BY COALESCE(created_at, ''), operation_id LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [self._operation(row) for row in rows]

    def claim(self, scope: str, operation_id: str, effect: str, request: dict[str, Any]) -> Claim:
        """Atomically bind and durably acquire one attempt, or return its status."""
        _text(scope, "scope")
        _text(operation_id, "operation_id")
        _text(effect, "effect")
        if type(request) is not dict:
            raise ValueError("request must be a JSON object")
        payload = _json(request)
        with self._transaction(scope) as db:
            record = self._get(db, scope, operation_id)
            if record is None:
                db.execute(
                    f"INSERT INTO operations({_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, 'in_flight', 1, 1, NULL, NULL, ?)",
                    (scope, operation_id, effect, payload, str(uuid4()),
                     # Always fractional, so a burst inside one second still
                     # sorts by arrival rather than collapsing to ID order.
                     datetime.now(timezone.utc).isoformat(timespec="microseconds")),
                )
            else:
                if record.effect != effect or _json(record.request) != payload:
                    raise OperationConflict("Operation ID is bound to a different effect or request")
                if record.state != "ready":
                    return Claim(record, False)
                db.execute(
                    "UPDATE operations SET state='in_flight', attempt=attempt+1, "
                    "version=version+1, error=NULL WHERE scope=? AND operation_id=?",
                    (scope, operation_id),
                )
            record = self._get(db, scope, operation_id)
            assert record is not None
        return Claim(record, True)

    def finish(self, owned: Operation, state: str, result: str | None, error: str | None) -> Operation:
        scope = owned.scope
        with self._transaction(scope) as db:
            changed = db.execute(
                "UPDATE operations SET state=?, result=?, error=?, version=version+1 "
                "WHERE scope=? AND operation_id=? AND version=? AND state='in_flight'",
                (state, result, error, scope, owned.operation_id, owned.version),
            ).rowcount
            if changed != 1:
                raise OperationConflict("Attempt no longer owns this operation")
            record = self._get(db, scope, owned.operation_id)
            assert record is not None
            return record

    def resolve(
        self, scope: str, operation_id: str, *, expected_version: int, decision_id: str,
        action: str, reason: str, workers_stopped: bool, result: Any = _MISSING,
    ) -> Operation:
        """Atomically apply a trusted decision or replay its current state.

        Requires stopped workers and reconciled provider requests. Completion
        requires an explicit result; identical decisions cannot grant another retry."""
        _text(operation_id, "operation_id")
        _text(decision_id, "decision_id")
        _text(reason, "reason")
        if workers_stopped is not True:
            raise ValueError("Confirm old workers cannot continue before resolving")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        if action not in ("retry", "complete"):
            raise ValueError("action must be retry or complete")
        if action == "complete" and result is _MISSING:
            raise ValueError("Confirmed completion requires a result")
        if action == "retry" and result is not _MISSING:
            raise ValueError("Retry decisions cannot supply a completed result")
        encoded = _json(result) if action == "complete" else None
        payload = _json({
            "operation_id": operation_id, "expected_version": expected_version,
            "action": action, "reason": reason, "result": encoded,
        })
        with self._transaction(scope) as db:
            record = self._get(db, scope, operation_id)
            if record is None:
                raise OperationConflict("Unknown operation")
            decision = db.execute(
                "SELECT payload FROM decisions WHERE scope=? AND decision_id=?",
                (scope, decision_id),
            ).fetchone()
            if decision is not None:
                if decision["payload"] != payload:
                    raise OperationConflict("Decision ID already used with different parameters")
                return record
            if record.version != expected_version or record.state not in ("in_flight", "indeterminate"):
                raise OperationConflict("Stale version or operation is not unresolved")
            db.execute(
                "UPDATE operations SET state=?, result=?, error=NULL, version=version+1 "
                "WHERE scope=? AND operation_id=?",
                ("ready" if action == "retry" else "completed", encoded, scope, operation_id),
            )
            db.execute(
                "INSERT INTO decisions(scope, decision_id, payload) VALUES (?, ?, ?)",
                (scope, decision_id, payload),
            )
            record = self._get(db, scope, operation_id)
            assert record is not None
            return record
