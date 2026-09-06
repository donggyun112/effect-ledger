"""LangChain effect boundary. Install last among tool middleware; see docs/langchain-boundary.md."""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

from ._graph_boundary import checked_identity, pause
from .operations import EffectExecutor, Operation, _json, _text

_CURRENT: ContextVar[Operation | None] = ContextVar('effect_operation', default=None)


class _ControlFlow(BaseException):
    """Carry graph control flow through the framework-free handler error boundary."""

    def __init__(self, signal: GraphBubbleUp):
        self.signal = signal


def current_operation() -> Operation:
    """Return the task-local protected attempt, or raise LookupError outside a tool."""
    operation = _CURRENT.get()
    if operation is None:
        raise LookupError('No protected effect is executing')
    return operation


class ExecutionBoundary(AgentMiddleware):
    """Protect declared single-effect tools and commit results before outer processing.

    Requires durable checkpoints and serialized threads. Do not combine with durable_tool on the same effect."""

    def __init__(self, executor: EffectExecutor, *, effects: Mapping[str, str],
                 workflow_id: str | None = None,
                 operation_id: Callable[[ToolRuntime], str] | None = None) -> None:
        if operation_id is None:
            _text(workflow_id, 'workflow_id')
        self.executor = executor
        self.effects = {_text(name, 'tool name'): _text(effect, 'effect')
                        for name, effect in effects.items()}
        self.workflow_id = workflow_id
        self.operation_id = operation_id

    @staticmethod
    def result(content: str | list, *, artifact: Any = None) -> dict[str, Any]:
        """Create the result envelope for a trusted operator's complete decision."""
        return ExecutionBoundary._encode(ToolMessage(
            content=content, artifact=artifact, tool_call_id='stored'))

    @staticmethod
    def _encode(message: Any) -> dict[str, Any]:
        if not isinstance(message, ToolMessage) or message.status != 'success':
            raise ValueError('Protected tools must return a successful ToolMessage')
        data = message.model_dump(mode='python', exclude={'id', 'tool_call_id', 'name'})
        envelope = {'format': 'langchain-tool-result:v1', 'message': data}
        _json(envelope)  # Reject coercion of non-JSON artifacts.
        return envelope

    @staticmethod
    def _reply(status: dict[str, Any], request: ToolCallRequest, effect: str) -> ToolMessage:
        if status['state'] != 'completed':
            return pause(status, effect, request.runtime.config)
        try:
            envelope = status['result']
            if envelope['format'] != 'langchain-tool-result:v1':
                raise ValueError('Unknown result envelope')
            message = ToolMessage.model_validate({**envelope['message'],
                'tool_call_id': request.tool_call['id'], 'name': request.tool_call['name'],
                'id': None})
            if message.status != 'success':
                raise ValueError('Completion cannot contain an error ToolMessage')
            return message
        except Exception as exc:
            invalid = {**status, 'state': 'result_error', 'unresolved': True,
                       'error': type(exc).__name__}
        return pause(invalid, effect, request.runtime.config)

    @staticmethod
    def _failure(operation_id: str, exc: Exception) -> dict[str, Any]:
        return {'operation_id': operation_id, 'state': 'transport_error',
                'unresolved': True, 'error': type(exc).__name__, 'version': None}

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        effect = self.effects.get(request.tool_call['name'])
        if effect is None:
            return handler(request)
        identity = checked_identity(request.runtime, self.workflow_id, self.operation_id, effect)

        def execute(owned: Operation):
            token = _CURRENT.set(owned)
            try:
                frozen = request.override(tool_call={**request.tool_call, 'args': owned.request})
                return self._encode(handler(frozen))
            except GraphBubbleUp as signal:
                raise _ControlFlow(signal) from signal
            finally:
                _CURRENT.reset(token)

        try:
            status = self.executor.execute(identity, effect, request.tool_call['args'], execute).response()
        except _ControlFlow as flow:
            raise flow.signal
        except Exception as exc:
            status = self._failure(identity, exc)
        return self._reply(status, request, effect)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        effect = self.effects.get(request.tool_call['name'])
        if effect is None:
            return await handler(request)
        identity = checked_identity(request.runtime, self.workflow_id, self.operation_id, effect)

        async def execute(owned: Operation):
            token = _CURRENT.set(owned)
            try:
                frozen = request.override(tool_call={**request.tool_call, 'args': owned.request})
                return self._encode(await handler(frozen))
            except GraphBubbleUp as signal:
                raise _ControlFlow(signal) from signal
            finally:
                _CURRENT.reset(token)

        try:
            status = (await self.executor.aexecute(
                identity, effect, request.tool_call['args'], execute)).response()
        except _ControlFlow as flow:
            raise flow.signal
        except Exception as exc:
            status = self._failure(identity, exc)
        return self._reply(status, request, effect)
