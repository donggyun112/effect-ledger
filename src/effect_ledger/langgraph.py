"""Effect tools and recovery for root LangChain agents with durable, serialized threads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ._graph_boundary import checked_identity, pause
from .operations import _json, _text


def durable_tool(
    *, name: str, description: str, workflow_id: str | None = None, effect: str,
    execute: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    operation_id: Callable[[ToolRuntime], str] | None = None,
) -> StructuredTool:
    """Wrap one effect transport returning Operation.response().

    Errors pause the graph; resume never authorizes retries. Host operation IDs
    must remain stable on replay. Otherwise workflow/checkpoint IDs define identity."""
    if operation_id is None:
        _text(workflow_id, "workflow_id")
    _text(effect, "effect")

    def dispatch(operation_id: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            status = execute(operation_id, effect, request)
            # Never interpret malformed or mismatched transport replies as success.
            _json(status)
            if not isinstance(status, dict) or status.get("operation_id") != operation_id:
                raise ValueError("Mismatched operation response")
            if status.get("state") not in ("in_flight", "indeterminate", "ready", "completed"):
                raise ValueError("Unknown operation state")
            if status["state"] == "completed" and (
                status.get("unresolved") is not False or "result" not in status
            ):
                raise ValueError("Incomplete completion response")
            return status
        except Exception as exc:
            return {"operation_id": operation_id, "state": "transport_error",
                    "unresolved": True, "error": type(exc).__name__, "version": None}

    def run(request: dict[str, Any], runtime: ToolRuntime):
        resolved_id = checked_identity(runtime, workflow_id, operation_id, effect)
        status = dispatch(resolved_id, request)
        if status["state"] == "completed":
            return _json(status["result"]), status
        # Consume old interrupt answers without repeating transport I/O. A new
        # invocation rechecks authority once, before consuming any resume values.
        return pause(status, effect)

    async def arun(request: dict[str, Any], runtime: ToolRuntime):
        resolved_id = checked_identity(runtime, workflow_id, operation_id, effect)
        status = await asyncio.to_thread(dispatch, resolved_id, request)
        if status["state"] == "completed":
            return _json(status["result"]), status
        return pause(status, effect)

    return StructuredTool.from_function(
        func=run, coroutine=arun, name=name, description=description,
        response_format="content_and_artifact",
    )


class LedgerRunner:
    """Guard root-agent start/resume with synchronous checkpoint durability.

    The host serializes threads. Direct graph calls and time travel bypass these guards."""

    def __init__(self, graph: Any) -> None:
        if graph.checkpointer is None or isinstance(graph.checkpointer, InMemorySaver):
            raise ValueError("A durable checkpointer is required")
        self.graph = graph

    @staticmethod
    def _config(config: dict[str, Any]) -> dict[str, Any]:
        configurable = config.get("configurable", {})
        _text(configurable.get("thread_id"), "thread_id")
        if configurable.get("checkpoint_id") or configurable.get("checkpoint_ns"):
            raise ValueError("Only root agents at the latest checkpoint are supported")
        return config

    @staticmethod
    def _resume_input(snapshot: Any, responses: dict[str, Any] | None) -> Command | None:
        pending = [item for task in snapshot.tasks for item in task.interrupts]
        answers = dict(responses or {})
        ids = {item.id for item in pending}
        if set(answers) - ids:
            raise ValueError("Resume answers contain stale or unknown interrupt IDs")
        for item in pending:
            recovery = isinstance(item.value, dict) and item.value.get("kind") == "effect_recovery"
            if item.id not in answers:
                if not recovery:
                    raise ValueError("Ordinary human approval requires an explicit answer")
                answers[item.id] = True  # Wake-up only: the tool rechecks the server.
        return Command(resume=answers) if pending else None

    def start(self, inputs: dict[str, Any], config: dict[str, Any], *, context: Any = None) -> dict:
        config = self._config(config)
        snapshot = self.graph.get_state(config)
        if snapshot.next or snapshot.interrupts:
            raise ValueError("Thread has unfinished work; resolve and resume it first")
        return self.graph.invoke(inputs, config, context=context, durability="sync")

    def resume(
        self, config: dict[str, Any], *, responses: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict:
        config = self._config(config)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            raise ValueError("Unknown thread; start it first")
        command = self._resume_input(snapshot, responses)
        if not snapshot.next and not snapshot.interrupts:
            return snapshot.values
        return self.graph.invoke(command, config, context=context, durability="sync")

    async def astart(self, inputs: dict[str, Any], config: dict[str, Any], *, context: Any = None) -> dict:
        config = self._config(config)
        snapshot = await self.graph.aget_state(config)
        if snapshot.next or snapshot.interrupts:
            raise ValueError("Thread has unfinished work; resolve and resume it first")
        return await self.graph.ainvoke(inputs, config, context=context, durability="sync")

    async def aresume(
        self, config: dict[str, Any], *, responses: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict:
        config = self._config(config)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            raise ValueError("Unknown thread; start it first")
        command = self._resume_input(snapshot, responses)
        if not snapshot.next and not snapshot.interrupts:
            return snapshot.values
        return await self.graph.ainvoke(command, config, context=context, durability="sync")
