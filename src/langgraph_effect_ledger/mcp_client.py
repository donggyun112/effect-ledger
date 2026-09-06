"""Synchronous transport for a trusted MCP stdio effect server (SDK v1)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioEffectClient:
    """Open a trusted local server session per call with a fixed DB/scope.

    Synchronous transport for durable_tool; do not call directly in an event loop.
    Transport errors never prove effect failure. No session pooling or HTTP auth."""

    def __init__(self, server: StdioServerParameters, *, timeout: float = 30) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.server = server
        self.timeout = timeout

    def execute(self, operation_id: str, effect: str, request: dict[str, Any]) -> dict[str, Any]:
        async def call():
            async with stdio_client(self.server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "execute_effect", {"operation_id": operation_id, "effect": effect, "request": request},
                        read_timeout_seconds=timedelta(seconds=self.timeout),
                    )
                    if result.isError or not isinstance(result.structuredContent, dict):
                        raise ValueError("Effect server rejected the request or returned no structured state")
                    return result.structuredContent
        async def bounded():
            return await asyncio.wait_for(call(), timeout=self.timeout)
        return asyncio.run(bounded())
