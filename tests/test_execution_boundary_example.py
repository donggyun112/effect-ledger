"""Smoke test the documented operator commands in separate processes."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class ExampleTest(unittest.TestCase):
    def test_documented_start_confirm_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (Path(__file__).resolve().parents[1]
                      / "examples" / "execution_boundary_agent.py")
            def run(*args):
                completed = subprocess.run(
                    [sys.executable, str(script), "--state-dir", directory, *args],
                    text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)
            def sent():
                with closing(sqlite3.connect(Path(directory) / "mailbox.sqlite")) as db:
                    return db.execute("SELECT count(*) FROM messages").fetchone()[0]
            pause = run("start", "--lose-response")
            self.assertFalse(pause["completed"])
            data = pause["interrupts"][0]
            self.assertEqual(run("status")["interrupts"][0]["operation_id"], data["operation_id"])
            # The confirmation is already out. A resume without a decision must not send it again.
            self.assertFalse(run("resume")["completed"])
            self.assertEqual(sent(), 1)
            run("confirm", "--operation-id", data["operation_id"], "--version", str(data["version"]),
                "--decision-id", "verified-confirmation-123", "--message-id", "1",
                "--workers-stopped")
            self.assertTrue(run("resume")["completed"])
            self.assertTrue(run("status")["completed"])
            self.assertEqual(sent(), 1)
