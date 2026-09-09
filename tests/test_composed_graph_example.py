"""The composed graph must refuse the retry from the ledger, not from the graph."""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from effect_ledger import EffectExecutor

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "execution_boundary_agent.py"


def _example():
    spec = importlib.util.spec_from_file_location("boundary_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposedGraphTest(unittest.TestCase):
    def test_resume_walks_back_through_the_ledger_and_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["EFFECT_LEDGER_DEMO_DIR"] = directory
            try:
                graph = _example().ledger_graph()
            finally:
                del os.environ["EFFECT_LEDGER_DEMO_DIR"]
            root = Path(directory)
            graph.checkpointer = MemorySaver()
            config = {"configurable": {"thread_id": "order-workflow-1"}}

            def sent():
                with closing(sqlite3.connect(root / "mailbox.sqlite")) as db:
                    return db.execute("SELECT count(*) FROM messages").fetchone()[0]

            def stopped_on():
                snapshot = graph.get_state(config)
                return list(snapshot.next), snapshot.values.get("pending")

            graph.invoke({"messages": [("user", "Confirm order-123")]}, config)
            nodes, pending = stopped_on()
            self.assertEqual(nodes, ["unresolved"])
            self.assertEqual(pending["state"], "indeterminate")
            self.assertEqual(pending["attempt"], 1)
            self.assertEqual(sent(), 1)

            graph.invoke(Command(resume="operator looked"), config)
            nodes, resumed = stopped_on()
            # The lap through the ledger changed nothing: no second confirmation,
            # and no new attempt against the provider.
            self.assertEqual(nodes, ["unresolved"])
            self.assertEqual(resumed["attempt"], pending["attempt"])
            self.assertEqual(resumed["state"], "indeterminate")
            self.assertEqual(sent(), 1)

            executor = EffectExecutor(root / "effects.sqlite", scope="account-1")
            operation = executor.unresolved()[0]
            executor.resolve(
                operation.operation_id, expected_version=operation.version,
                decision_id="verified-confirmation-123", action="complete",
                result="order-123 confirmed, mailbox row 1",
                reason="Operator verified the mailbox row and stopped prior workers",
                workers_stopped=True,
            )

            values = graph.invoke(Command(resume="recovered"), config)
            self.assertEqual(stopped_on()[0], [])
            self.assertIn("mailbox row 1", values["messages"][-1].content)
            self.assertEqual(sent(), 1)
