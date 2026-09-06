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
            script = Path(__file__).resolve().parents[1] / "examples" / "durable_agent.py"
            def run(*args):
                completed = subprocess.run(
                    [sys.executable, str(script), "--state-dir", directory, *args],
                    text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)
            pause = run("start", "--lose-response")
            self.assertFalse(pause["completed"])
            data = pause["interrupts"][0]
            self.assertEqual(run("status")["interrupts"][0]["operation_id"], data["operation_id"])
            self.assertFalse(run("resume")["completed"])
            run("confirm", "--operation-id", data["operation_id"], "--version", str(data["version"]),
                "--decision-id", "confirmed-1", "--message-id", "1", "--workers-stopped")
            self.assertTrue(run("resume")["completed"])
            self.assertTrue(run("status")["completed"])
            with closing(sqlite3.connect(Path(directory) / "mailbox.sqlite")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
