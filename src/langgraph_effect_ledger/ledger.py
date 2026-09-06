"""Legacy tool-call ledger. Mixed effect/interrupt tools are not protected; see docs/legacy-middleware.md."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt

__all__ = [
    "VERDICTS",
    "EffectLedger",
    "EffectStore",
    "InMemoryEffectStore",
    "Record",
    "StoredResult",
]

State = Literal["started", "suspended", "completed"]

VERDICTS = ("retry_safe", "already_done")
"""Accepted verdicts; missing or unknown values deny execution."""

INDETERMINATE = (
    "INDETERMINATE: this call started and never reported a result. "
    "The effect may or may not have gone out. It was not retried. "
    "Supply a verdict (retry_safe | already_done) with the recovering invocation."
)


@dataclass(frozen=True)
class StoredResult:
    """Preserve ToolMessage content, status, and artifact for replay."""

    content: Any
    status: str = "success"
    artifact: Any = None
    name: str | None = None

    @classmethod
    def of(cls, message: ToolMessage) -> StoredResult:
        return cls(
            content=message.content,
            status=getattr(message, "status", "success") or "success",
            artifact=getattr(message, "artifact", None),
            name=getattr(message, "name", None),
        )

    def to_message(self, call_id: str) -> ToolMessage:
        return ToolMessage(
            content=self.content,
            tool_call_id=call_id,
            status=self.status,  # type: ignore[arg-type]
            artifact=self.artifact,
            name=self.name,
        )


@dataclass(frozen=True)
class Record:
    state: State
    result: StoredResult | None = None
    verdict_spent: bool = False


class EffectStore(Protocol):
    """Storage protocol for the legacy tool-call ledger."""

    def get(self, call_id: str) -> Record | None: ...

    def put(self, call_id: str, record: Record) -> None: ...

    def items(self) -> list[tuple[str, Record]]: ...


class InMemoryEffectStore:
    """Process-local store for tests; records do not survive restart."""

    def __init__(self) -> None:
        self._records: dict[str, Record] = {}

    def get(self, call_id: str) -> Record | None:
        return self._records.get(call_id)

    def put(self, call_id: str, record: Record) -> None:
        self._records[call_id] = record

    def items(self) -> list[tuple[str, Record]]:
        return list(self._records.items())

    def __repr__(self) -> str:
        return f"InMemoryEffectStore({self._records})"


@dataclass
class _Decision:
    action: Literal["run", "answer"]
    message: ToolMessage | None = None
    record: Record = field(default_factory=lambda: Record("started"))


class EffectLedger(AgentMiddleware):
    """Record attempts and block unresolved calls. Defaults to an in-memory store."""

    def __init__(
        self,
        store: EffectStore | None = None,
        *,
        verdict_attr: str = "effect_verdicts",
    ) -> None:
        super().__init__()
        self.store: EffectStore = InMemoryEffectStore() if store is None else store
        self.verdict_attr = verdict_attr
        self._resolved: dict[str, str] = {}


    def pending(self) -> dict[str, Record]:
        """Return started and suspended calls without a completed result."""
        return {
            call_id: record
            for call_id, record in self.store.items()
            if record.state != "completed"
        }

    def resolve(self, call_id: str, verdict: str) -> None:
        """Stage a verdict for the next attempt; does not reopen an answered tool call."""
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}; expected one of {VERDICTS}")
        self._resolved[call_id] = verdict

    def _verdict(self, request: Any, call_id: str) -> str | None:
        context = getattr(request.runtime, "context", None)
        table = getattr(context, self.verdict_attr, None) if context else None
        return dict(table or {}).get(call_id) or self._resolved.get(call_id)


    def _finish(self, call_id: str, record: Record, result: Any) -> Any:


        stored = StoredResult.of(result) if isinstance(result, ToolMessage) else None
        self.store.put(call_id, replace(record, state="completed", result=stored))
        return result

    def _begin(self, call_id: str, record: Record) -> Record:
        """Record started before executing or resuming the handler."""
        running = replace(record, state="started")
        self.store.put(call_id, running)
        return running

    def _suspend(self, call_id: str, record: Record) -> None:
        self.store.put(call_id, replace(record, state="suspended"))

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        call_id = request.tool_call["id"]
        decision = self._decide(request, call_id)
        if decision.action == "answer":
            return decision.message
        record = self._begin(call_id, decision.record)
        try:
            result = handler(request)
        except GraphInterrupt:
            self._suspend(call_id, record)
            raise
        return self._finish(call_id, record, result)

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        call_id = request.tool_call["id"]
        decision = self._decide(request, call_id)
        if decision.action == "answer":
            return decision.message
        record = self._begin(call_id, decision.record)
        try:
            result = await handler(request)
        except GraphInterrupt:
            self._suspend(call_id, record)
            raise
        return self._finish(call_id, record, result)


    def _decide(self, request: Any, call_id: str) -> _Decision:
        record = self.store.get(call_id)

        if record is None:
            return _Decision("run", record=Record("started"))

        if record.state == "completed":
            message = (
                record.result.to_message(call_id)
                if record.result is not None
                else ToolMessage(content="already completed", tool_call_id=call_id)
            )
            return _Decision("answer", message, record)

        if record.state == "suspended":

            return _Decision("run", record=record)


        verdict = self._verdict(request, call_id)

        if verdict == "already_done":
            stored = StoredResult(content="effect confirmed out of band; not retried")
            self.store.put(call_id, replace(record, state="completed", result=stored))
            return _Decision("answer", stored.to_message(call_id), record)

        if verdict == "retry_safe" and not record.verdict_spent:
            return _Decision("run", record=replace(record, verdict_spent=True))

        return _Decision(
            "answer",
            ToolMessage(content=INDETERMINATE, tool_call_id=call_id, status="error"),
            record,
        )
