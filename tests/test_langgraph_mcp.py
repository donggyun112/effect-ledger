"""Graph checkpoint + real MCP stdio server + independently durable mailbox."""

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

from mcp import StdioServerParameters

from test_langgraph_recovery import GraphFixture
from langgraph_effect_ledger.mcp_client import StdioEffectClient
from langgraph_effect_ledger.operations import EffectExecutor


class GraphMCPTest(GraphFixture, unittest.TestCase):
    def test_agent_recovers_through_mcp_without_resending_message(self):
        server = Path(__file__).resolve().parents[1] / "examples" / "mcp_server.py"
        mailbox = self.root / "mailbox.sqlite"
        ledger = self.root / "mcp-effects.sqlite"
        args = [str(server), "--ledger", str(ledger), "--mailbox", str(mailbox)]
        broken = StdioEffectClient(StdioServerParameters(command=sys.executable, args=args + ["--lose-response"]))
        good = StdioEffectClient(StdioServerParameters(command=sys.executable, args=args))
        pause = self.build(transport=broken.execute).start({"messages": [("user", "send")]}, self.config)
        self.assertEqual(len(self.model.calls), 1)
        pause = self.build(transport=good.execute).resume(self.config)
        data = pause["__interrupt__"][0].value
        self.assertEqual(data["state"], "indeterminate")
        authority = EffectExecutor(ledger, scope="local-mailbox")
        authority.resolve(data["operation_id"], expected_version=data["version"],
                          decision_id="verified-mcp", action="complete", result={"message_id": 1},
                          reason="Mailbox checked, stdio servers closed", workers_stopped=True)
        done = self.build(transport=good.execute).resume(self.config)
        self.assertEqual(done["messages"][-1].content, "Workflow completed")
        with closing(sqlite3.connect(mailbox)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(len(self.model.calls), 2)
