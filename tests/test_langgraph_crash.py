"""Full fresh-process graph recovery, locally and through a real MCP server."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from test_operation_crash import ProviderFixture

from langgraph_effect_ledger.langchain import ExecutionBoundary
from langgraph_effect_ledger.operations import EffectExecutor


@unittest.skipUnless(os.name == "posix", "SIGKILL process-group failure injection requires POSIX")
class GraphCrashTest(ProviderFixture, unittest.TestCase):
    def agent_worker(self, action, transport, boundary="normal"):
        worker = Path(__file__).parent / "fixtures" / "langgraph_worker.py"
        proc = subprocess.Popen(
            [sys.executable, str(worker), str(self.root), self.url, action, transport, boundary],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        def cleanup():
            if proc.poll() is None:
                self.stop_worker(proc)
            proc.communicate(timeout=5)
        self.addCleanup(cleanup)
        return proc

    def stop_worker(self, proc):
        # MCP SDK starts its server in a separate process session. Kill the
        # fixture's explicitly recorded child as well as the graph process.
        marker = self.root / f"mcp-server-{proc.pid}.pid"
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
            marker.unlink()
        if proc.poll() is None:
            proc.kill()

    def finish(self, worker):
        out, err = worker.communicate(timeout=15)
        self.assertEqual(worker.returncode, 0, err)
        return json.loads(out)

    def assert_model_calls(self, stages):
        with closing(sqlite3.connect(self.root / "model.sqlite")) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT stage FROM calls ORDER BY rowid")], stages)

    def remote_commit_crash(self, transport, retry_crash=False):
        first = self.agent_worker("start", transport)
        self.assertTrue(self.committed.wait(10))
        self.stop_worker(first)
        first.communicate(timeout=5)
        self.release.set()
        # Model output was durably saved before the external request.
        with closing(sqlite3.connect(self.root / "checkpoints.sqlite")) as db:
            saved = SqliteSaver(db).get_tuple({"configurable": {"thread_id": "crash-thread"}})
            self.assertEqual(saved.checkpoint["channel_values"]["messages"][-1].tool_calls[0]["args"],
                             {"text": "hello"} if transport == "boundary" else {"request": {"text": "hello"}})
        paused = self.finish(self.agent_worker("resume", transport))
        self.assertEqual(len(paused["interrupts"]), 1)
        self.assertEqual(paused["results"], [])
        self.assert_model_calls(["plan"])
        self.assertEqual(self.count(), 1)
        data = paused["interrupts"][0]
        # Repeat resume without any recovery authority: stay paused, no redispatch.
        again = self.finish(self.agent_worker("resume", transport))
        self.assertEqual(again["interrupts"][0]["operation_id"], data["operation_id"])
        executor = EffectExecutor(self.root / "ledger.sqlite", scope="test-account")
        expected_count = 1
        if retry_crash:
            grant = dict(expected_version=data["version"], decision_id="retry-once", action="retry",
                         reason="Operator explicitly accepts a second send", workers_stopped=True)
            executor.resolve(data["operation_id"], **grant)
            self.committed.clear()
            self.release.clear()
            second = self.agent_worker("resume", transport)
            self.assertTrue(self.committed.wait(10))
            self.stop_worker(second)
            second.communicate(timeout=5)
            self.release.set()
            # Re-delivering the old permission after the second crash must not
            # authorize a third attempt. Two sends were explicitly accepted.
            executor.resolve(data["operation_id"], **grant)
            paused = self.finish(self.agent_worker("resume", transport))
            data = paused["interrupts"][0]
            self.assertEqual(data["attempt"], 2)
            self.assertEqual(data["state"], "in_flight")
            expected_count = 2
            self.assertEqual(self.count(), 2)
            self.assert_model_calls(["plan"])
        result = {'remote_id': 'msg-1'}
        if transport == 'boundary':
            result = ExecutionBoundary.result(json.dumps(result))
        executor.resolve(data["operation_id"], expected_version=data["version"],
                         decision_id="remote-confirmed", action="complete", result=result,
                         reason="Remote commit verified; worker group killed", workers_stopped=True)
        # A fresh process also models a crash between recovery commit and resume.
        done = self.finish(self.agent_worker("resume", transport))
        self.assertEqual(done["interrupts"], [])
        self.assertEqual(done["final"], "Workflow completed")
        self.assertEqual(done["results"], [{"remote_id": "msg-1"}])
        self.assertEqual(self.count(), expected_count)
        self.assert_model_calls(["plan", "final"])

    def completed_ledger_crash(self, transport):
        self.release.set()
        first = self.agent_worker("start", transport, "after-ledger")
        deadline = time.monotonic() + 10
        while not (self.root / "ledger-committed").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((self.root / "ledger-committed").exists())
        self.stop_worker(first)
        first.communicate(timeout=5)
        self.assert_model_calls(["plan"])
        done = self.finish(self.agent_worker("resume", transport))
        self.assertEqual(done["interrupts"], [])
        self.assertEqual(done["results"], [{"remote_id": "msg-1"}])
        self.assertEqual(done["final"], "Workflow completed")
        self.assertEqual(self.count(), 1)
        self.assert_model_calls(["plan", "final"])

    def test_local_remote_commit_then_kill(self):
        self.remote_commit_crash("local")

    def test_mcp_remote_commit_then_kill(self):
        self.remote_commit_crash("mcp")

    def test_mcp_retry_crash_does_not_reuse_old_permission(self):
        self.remote_commit_crash("mcp", retry_crash=True)

    def test_local_ledger_commit_then_kill(self):
        self.completed_ledger_crash("local")

    def test_mcp_ledger_commit_then_kill(self):
        self.completed_ledger_crash("mcp")

    def test_boundary_remote_commit_then_kill(self):
        self.remote_commit_crash('boundary')

    def test_boundary_ledger_commit_then_kill(self):
        self.completed_ledger_crash('boundary')

    def test_boundary_retry_crash_does_not_reuse_old_permission(self):
        self.remote_commit_crash('boundary', retry_crash=True)
