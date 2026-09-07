"""Shared identity and suspension rules for LangGraph effect adapters."""
import hashlib
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.types import interrupt

from .operations import _json, _text


def tool_identity(runtime: ToolRuntime, workflow_id: str | None,
                  operation_id: Callable[[ToolRuntime], str] | None) -> str:
    if operation_id is not None:
        return _text(operation_id(runtime), "operation_id")
    thread_id = runtime.config.get("configurable", {}).get("thread_id")
    _text(thread_id, "thread_id")
    _text(runtime.tool_call_id, "tool_call_id")
    # Tool call IDs can be reused on later model turns. Bind to the durable
    # parent message too, so a new intentional action is not silently dropped.
    parent = next((message for message in reversed(runtime.state["messages"])
                   if isinstance(message, AIMessage) and any(
                       call["id"] == runtime.tool_call_id for call in message.tool_calls)), None)
    if parent is None:
        raise ValueError("Tool call has no checkpointed parent AIMessage")
    _text(parent.id, "parent message ID")
    binding = _json([workflow_id, thread_id, parent.id, runtime.tool_call_id])
    return "lg-" + hashlib.sha256(binding.encode()).hexdigest()


def pause(status: dict[str, Any], effect: str, config: dict | None = None) -> Any:
    def wait(_: Any) -> Any:
        # Historical resume answers only wake the graph; no retry permission or I/O.
        while True:
            interrupt({**status, "kind": "effect_recovery", "effect": effect})
    if config is None:
        return wait(None)
    # Python 3.10 async middleware tracing may lose the implicit runnable context.
    # A native synchronous Runnable restores the explicitly supplied graph config
    # for interrupt without relying on private ContextVars or spawning another task.
    return RunnableLambda(wait).invoke(None, config=config)


def checked_identity(runtime: ToolRuntime, workflow_id: str | None,
                     operation_id: Callable[[ToolRuntime], str] | None, effect: str) -> str:
    try:
        return tool_identity(runtime, workflow_id, operation_id)
    except Exception as exc:
        status = {"operation_id": None, "state": "identity_error", "unresolved": True,
                  "error": type(exc).__name__, "version": None}
    return pause(status, effect, runtime.config)
