"""Optional MCP SDK v1 adapter. Recovery is an operator API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .operations import EffectExecutor, Operation, OperationConflict


def create_server(
    executor: EffectExecutor, effects: Mapping[str, Callable[[Operation], Any]],
) -> FastMCP:
    """Expose registered single-effect handlers in a fixed scope.

    Hosts persist operation IDs and block dependent work while unresolved.
    HTTP authentication and account routing are deployment responsibilities."""
    registry = dict(effects)
    server = FastMCP(
        "Effect recovery",
        instructions=(
            "Persist a logical operation_id before execution and reuse it on retries. "
            "If unresolved is true, stop dependent work. For next_action=wait, "
            "poll get_effect first; in_flight is not proof a worker is alive or dead. "
            "For next_action=reconcile, ask an operator to inspect the effect. "
            "Never bypass an unresolved operation by generating another ID."
        ),
    )

    def response(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data, allow_nan=False))],
            structuredContent=data, isError=error,
        )

    @server.tool(structured_output=False)
    async def execute_effect(operation_id: str, effect: str, request: dict[str, Any]) -> CallToolResult:
        """Execute a registered effect or return its state/result.

        Preserve operation_id. For wait, poll get_effect; for reconcile, seek
        operator inspection. Unresolved is not completion; time never grants retry."""
        if effect not in registry:
            return response({"error": "unknown_effect", "operation_id": operation_id}, error=True)
        try:
            # Cancelling this await does not stop the thread or grant a new attempt.
            record = await asyncio.to_thread(
                executor.execute, operation_id, effect, request, registry[effect],
            )
        except OperationConflict as exc:
            return response({"error": "operation_conflict", "detail": str(exc),
                             "operation_id": operation_id}, error=True)
        except ValueError as exc:
            return response({"error": "invalid_request", "detail": str(exc),
                             "operation_id": operation_id}, error=True)
        return response(record.response())

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=False)
    async def get_effect(operation_id: str) -> CallToolResult:
        """Read operation state without sending an effect or granting a retry."""
        try:
            record = await asyncio.to_thread(executor.get, operation_id)
        except ValueError as exc:
            return response({"error": "invalid_request", "detail": str(exc)}, error=True)
        if record is None:
            return response({"error": "unknown_operation", "operation_id": operation_id}, error=True)
        return response(record.response())

    return server
