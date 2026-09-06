"""Real processes + a non-idempotent HTTP service with independent persistence."""

import json
import os
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from langgraph_effect_ledger.operations import EffectExecutor


def executor_for(db, scope):
    if str(db).startswith(('postgresql://', 'postgres://')):
        from langgraph_effect_ledger.postgres import PostgresOperationStore
        return EffectExecutor(store=PostgresOperationStore(str(db)), scope=scope)
    return EffectExecutor(db, scope=scope)


def run_worker(db, url, barrier=None):
    executor = executor_for(db, os.environ.get('EFFECT_LEDGER_CRASH_SCOPE', 'test-account'))
    if barrier is not None:
        print("READY", flush=True)
        sys.stdin.readline()
    def send(call):
        request = Request(url, json.dumps(call.request).encode(), method="POST")
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    result = executor.execute("message-1", "message.send:v1", {"text": "hello"}, send)
    print(json.dumps(result.response()), flush=True)


class ProviderFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "ledger.sqlite"
        self.scope = 'test-account'
        self.remote_db = self.root / "provider.sqlite"
        with closing(sqlite3.connect(self.remote_db)) as db, db:
            db.execute("CREATE TABLE messages (body TEXT)")
        self.committed, self.release = threading.Event(), threading.Event()
        outer = self
        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                with closing(sqlite3.connect(outer.remote_db)) as db, db:
                    db.execute("INSERT INTO messages VALUES (?)", (body,))
                outer.committed.set()  # remote commit happened, response not sent
                outer.release.wait(10)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"remote_id":"msg-1"}')
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def worker(self, barrier=False):
        proc = subprocess.Popen(
            [sys.executable, __file__, "worker", str(self.db), self.url]
            + (["barrier"] if barrier else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, 'EFFECT_LEDGER_CRASH_SCOPE': self.scope},
        )
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        self.addCleanup(cleanup)
        return proc

    def count(self):
        with closing(sqlite3.connect(self.remote_db)) as db, db:
            return db.execute("SELECT count(*) FROM messages").fetchone()[0]


class CrashContract:
    def test_kill_after_remote_commit_blocks_restart_until_operator_completion(self):
        worker = self.worker()
        self.assertTrue(self.committed.wait(10), "worker must reach remote commit")
        worker.kill()
        worker.communicate(timeout=5)
        self.release.set()
        replay = self.worker()
        out, err = replay.communicate(timeout=10)
        self.assertEqual(replay.returncode, 0, err)
        self.assertEqual(json.loads(out)["state"], "in_flight")
        self.assertEqual(self.count(), 1)

        executor = executor_for(self.db, self.scope)
        record = executor.get("message-1")
        executor.resolve(
            "message-1", expected_version=record.version, decision_id="verified-msg-1",
            action="complete", result={"remote_id": "msg-1"},
            reason="Provider DB confirms msg-1; worker killed and joined",
            workers_stopped=True,
        )
        recovered = self.worker()
        out, err = recovered.communicate(timeout=10)
        self.assertEqual(recovered.returncode, 0, err)
        self.assertEqual(json.loads(out)["result"], {"remote_id": "msg-1"})
        self.assertEqual(json.loads(out)["state"], "completed")
        self.assertEqual(self.count(), 1)

    def test_independent_processes_share_one_execution_claim(self):
        # Readiness is bounded through a separate reader thread, not a blocking read.
        workers = [self.worker(barrier=True) for _ in range(4)]
        for worker in workers:
            ready = threading.Event()
            line = []
            def read(proc=worker, event=ready, lines=line):
                lines.append(proc.stdout.readline())
                event.set()
            threading.Thread(target=read, daemon=True).start()
            self.assertTrue(ready.wait(10))
            self.assertEqual(line, ["READY\n"])
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        try:
            self.assertTrue(self.committed.wait(5))
            deadline = time.monotonic() + 5
            while sum(worker.poll() is not None for worker in workers) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            losers = [worker for worker in workers if worker.poll() is not None]
            winners = [worker for worker in workers if worker.poll() is None]
            self.assertEqual(len(losers), 3)
            self.assertEqual(len(winners), 1)
            states = []
            for worker in losers:
                out, err = worker.communicate(timeout=5)
                self.assertEqual(worker.returncode, 0, err)
                result = json.loads(out)
                self.assertEqual(result["state"], "in_flight")
                self.assertEqual(result["next_action"], "wait")
                states.append(result["state"])
        finally:
            self.release.set()
        out, err = winners[0].communicate(timeout=5)
        self.assertEqual(winners[0].returncode, 0, err)
        states.append(json.loads(out)["state"])
        self.assertEqual(states.count("completed"), 1)
        self.assertEqual(states.count("in_flight"), 3)
        self.assertEqual(self.count(), 1)
        # A later replay is also completed, without owning a new execution.
        replay = self.worker()
        out, err = replay.communicate(timeout=5)
        self.assertEqual(replay.returncode, 0, err)
        self.assertEqual(json.loads(out)["state"], "completed")
        self.assertEqual(self.count(), 1)


class CrashTest(CrashContract, ProviderFixture, unittest.TestCase):
    pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        run_worker(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        unittest.main()
