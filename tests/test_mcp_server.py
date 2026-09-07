"""Real MCP stdio round trips against the example non-idempotent mailbox."""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from effect_ledger.operations import EffectExecutor


class MCPTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / "ledger.sqlite"
        self.mailbox = self.root / "mailbox.sqlite"

    @asynccontextmanager
    async def session(self, lose_response=False):
        example = Path(__file__).resolve().parents[1] / "examples" / "mcp_server.py"
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(example), "--ledger", str(self.ledger), "--mailbox", str(self.mailbox)]
            + (["--lose-response"] if lose_response else []),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def send(self, session, text="hello", effect="message.send:v1"):
        return await asyncio.wait_for(session.call_tool("execute_effect", {
            "operation_id": "message-1", "effect": effect,
            "request": {"text": text},
        }), 10)

    def count(self):
        with closing(sqlite3.connect(self.mailbox)) as db, db:
            return db.execute("SELECT count(*) FROM messages").fetchone()[0]

    async def test_restart_replay_conflict_and_status_over_stdio(self):
        async with self.session() as session:
            tools = await session.list_tools()
            self.assertEqual({t.name for t in tools.tools}, {"execute_effect", "get_effect"})
            first = await self.send(session)
            self.assertFalse(first.isError)
            self.assertEqual(first.structuredContent["result"], {"message_id": 1})
            conflict = await self.send(session, text="changed")
            self.assertTrue(conflict.isError)
            self.assertEqual(conflict.structuredContent["error"], "operation_conflict")
            unknown = await self.send(session, effect="not-registered")
            self.assertTrue(unknown.isError)
            status = await session.call_tool("get_effect", {"operation_id": "message-1"})
            self.assertEqual(status.structuredContent["state"], "completed")
        async with self.session() as session:
            replay = await self.send(session)
            self.assertEqual(replay.structuredContent["result"], {"message_id": 1})
        self.assertEqual(self.count(), 1)

    async def test_unresolved_survives_restart_and_operator_completion_replays(self):
        async with self.session(lose_response=True) as session:
            result = await self.send(session)
            self.assertTrue(result.structuredContent["unresolved"])
            self.assertEqual(result.structuredContent["state"], "indeterminate")
        async with self.session() as session:
            result = await self.send(session)
            self.assertTrue(result.structuredContent["unresolved"])
        self.assertEqual(self.count(), 1)
        executor = EffectExecutor(self.ledger, scope="local-mailbox")
        executor.resolve(
            "message-1", expected_version=result.structuredContent["version"],
            decision_id="operator-confirmed-1", action="complete",
            reason="Mailbox contains message 1 and server has stopped",
            workers_stopped=True, result={"message_id": 1},
        )
        async with self.session() as session:
            recovered = await self.send(session)
            self.assertFalse(recovered.structuredContent["unresolved"])
            self.assertEqual(recovered.structuredContent["result"], {"message_id": 1})
        self.assertEqual(self.count(), 1)

    async def test_cancelled_mcp_call_does_not_allow_a_second_dispatch(self):
        fixture = Path(__file__).parent / "fixtures" / "mcp_blocking_server.py"
        params = StdioServerParameters(command=sys.executable, args=[str(fixture), str(self.root)])
        args = {"operation_id": "one", "effect": "send:v1", "request": {}}
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                call = asyncio.create_task(session.call_tool("execute_effect", args))
                try:
                    async def wait_entered():
                        while not (self.root / "entered").exists():
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(wait_entered(), 5)
                    call.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await call
                    duplicate = await asyncio.wait_for(session.call_tool("execute_effect", args), 5)
                    self.assertEqual(duplicate.structuredContent["state"], "in_flight")
                    self.assertEqual(duplicate.structuredContent["next_action"], "wait")
                finally:
                    (self.root / "release").touch()
                    if not call.done():
                        call.cancel()
                    await asyncio.gather(call, return_exceptions=True)
                async def wait_completed():
                    while True:
                        status = await session.call_tool("get_effect", {"operation_id": "one"})
                        if status.structuredContent["state"] == "completed":
                            return status
                        await asyncio.sleep(0.01)
                self.assertEqual((await asyncio.wait_for(wait_completed(), 5)).structuredContent["result"], "done")
        self.assertEqual((self.root / "effects").read_text(), "effect\n")


if __name__ == "__main__":
    unittest.main()
