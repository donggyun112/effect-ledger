"""LangChain effect boundary. Install last among tool middleware; see docs/langchain-boundary.md."""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from enum import Enum
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

from ._graph_boundary import checked_identity, pause
from .operations import EffectExecutor, Operation, OperationConflict, _json, _text

_CURRENT: ContextVar[Operation | None] = ContextVar('effect_operation', default=None)


class ToolPolicy(Enum):
    READ_ONLY = 'read_only'
    CONTROL = 'control'


READ_ONLY = ToolPolicy.READ_ONLY
CONTROL = ToolPolicy.CONTROL


class UnsupportedToolResult(ValueError):
    """A durable tool returned a value that cannot be safely replayed."""


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
    """Protect every tool by default and commit results before outer processing.

    READ_ONLY and CONTROL tools bypass the ledger. Requires durable checkpoints and
    serialized threads. Do not combine with durable_tool on the same effect."""

    def __init__(self, executor: EffectExecutor, *,
                 tools: Mapping[str, str | ToolPolicy] | None = None,
                 workflow_id: str | None = None,
                 operation_id: Callable[[ToolRuntime], str] | None = None) -> None:
        if operation_id is None:
            _text(workflow_id, 'workflow_id')
        self.executor = executor
        self.tool_policies: dict[str, str | ToolPolicy] = {}
        for name, policy in (tools or {}).items():
            name = _text(name, 'tool name')
            if isinstance(policy, str):
                policy = _text(policy, 'effect')
            elif policy not in (READ_ONLY, CONTROL):
                raise ValueError('tool policy must be READ_ONLY, CONTROL, or an effect name')
            self.tool_policies[name] = policy
        self.workflow_id = workflow_id
        self.operation_id = operation_id

    def _policy(self, name: str) -> str | ToolPolicy:
        return self.tool_policies.get(name, f'langchain.tool:{name}')

    @staticmethod
    def result(content: str | list, *, artifact: Any = None) -> dict[str, Any]:
        """Create the result envelope for a trusted operator's complete decision."""
        return ExecutionBoundary._encode(ToolMessage(
            content=content, artifact=artifact, tool_call_id='stored'))

    @staticmethod
    def _encode(message: Any) -> dict[str, Any]:
        if not isinstance(message, ToolMessage):
            raise UnsupportedToolResult(
                'Durable tools must return ToolMessage; mark no-effect control tools CONTROL')
        if message.status != 'success':
            raise ValueError('Protected tools must return a successful ToolMessage')
        data = message.model_dump(mode='python', exclude={'id', 'tool_call_id', 'name'})
        envelope = {'format': 'langchain-tool-result:v1', 'message': data}
        try:
            _json(envelope)  # Reject coercion of non-JSON artifacts.
        except ValueError as exc:
            raise UnsupportedToolResult(
                'Durable tool results and artifacts must contain only JSON values') from exc
        return envelope

    @staticmethod
    def _reply(status: dict[str, Any], request: ToolCallRequest, effect: str) -> ToolMessage:
        if status['state'] != 'completed':
            if status.get('error') == 'UnsupportedToolResult':
                status = {**status, 'configuration_hint':
                    'Mark no-effect Command tools CONTROL; effect results and artifacts must be JSON.'}
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

    def _failure(self, operation_id: str, effect: str, tool_name: str,
                 exc: Exception) -> dict[str, Any]:
        status = {'operation_id': operation_id, 'state': 'transport_error',
                  'unresolved': True, 'error': type(exc).__name__, 'version': None}
        if isinstance(exc, OperationConflict):
            try:
                existing = self.executor.get(operation_id)
            except Exception:
                existing = None
            if existing is not None and existing.effect != effect:
                identity = "workflow_id" if self.operation_id is None else "operation_id"
                status['configuration_hint'] = (
                    f"Operation is bound to {existing.effect!r}, but {tool_name!r} resolved "
                    f"to {effect!r}. Configure tools={{{tool_name!r}: "
                    f"{existing.effect!r}}} to preserve the effect name and replay the "
                    f"recorded outcome. A new {identity} dispatches this effect again even "
                    f"though the bound operation is already {existing.state}; choose it only "
                    f"to perform a deliberately new action.")
        return status

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        effect = self._policy(request.tool_call['name'])
        if effect in (READ_ONLY, CONTROL):
            return handler(request)
        assert isinstance(effect, str)
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
            status = self._failure(identity, effect, request.tool_call['name'], exc)
        return self._reply(status, request, effect)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        effect = self._policy(request.tool_call['name'])
        if effect in (READ_ONLY, CONTROL):
            return await handler(request)
        assert isinstance(effect, str)
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
            status = self._failure(identity, effect, request.tool_call['name'], exc)
        return self._reply(status, request, effect)
