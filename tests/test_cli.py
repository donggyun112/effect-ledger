"""The operator console reads freely and decides only under the same interlocks."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from effect_ledger import EffectExecutor
from effect_ledger.cli import main


class CLITest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = str(Path(directory.name) / "effects.sqlite")
        self.executor = EffectExecutor(self.db, scope="account-1")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", self.db, "--scope", "account-1", *args])
        return code, out.getvalue(), err.getvalue()

    def strand(self, operation_id, request=None):
        """Leave one operation unresolved the way a lost provider reply does."""
        def handler(owned):
            raise TimeoutError("Remote accepted but the response was lost")
        return self.executor.execute(operation_id, "message.send:v1",
                                     request or {"text": "hello"}, handler)

    def test_list_reports_only_unresolved_operations(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("No unresolved operations.", out)

        self.strand("op-1")
        self.executor.execute("op-2", "message.send:v1", {"text": "done"}, lambda o: {"id": 2})

        code, out, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        listed = json.loads(out)
        self.assertEqual([r["operation_id"] for r in listed], ["op-1"])
        self.assertEqual(listed[0]["state"], "indeterminate")

    def test_show_prints_the_request_and_names_the_version_to_pass(self):
        record = self.strand("op-1", {"text": "charge"})
        code, out, err = self.run_cli("show", "op-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["request"], {"text": "charge"})
        self.assertIn(f"--expected-version {record.version}", err)

        code, _, err = self.run_cli("show", "missing")
        self.assertEqual(code, 2)
        self.assertIn("Unknown operation", err)

    def test_completion_records_the_operator_result(self):
        record = self.strand("op-1")
        code, out, _ = self.run_cli(
            "resolve", "op-1", "--complete", "--result-json", '{"message_id": 7}',
            "--expected-version", str(record.version), "--decision-id", "d-1",
            "--reason", "Provider shows message 7", "--workers-stopped")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], "completed")
        self.assertEqual(self.executor.get("op-1").result, {"message_id": 7})
        self.assertEqual(self.run_cli("list")[1].strip(), "No unresolved operations.")

    def test_deciding_without_asserting_stopped_workers_is_refused(self):
        record = self.strand("op-1")
        code, _, err = self.run_cli(
            "resolve", "op-1", "--retry", "--expected-version", str(record.version),
            "--decision-id", "d-1", "--reason", "looks fine")
        self.assertEqual(code, 2)
        self.assertIn("--workers-stopped", err)
        self.assertEqual(self.executor.get("op-1").state, "indeterminate")

    def test_a_stale_version_is_rejected_and_the_current_state_is_reported(self):
        record = self.strand("op-1")
        self.executor.resolve("op-1", expected_version=record.version, decision_id="first",
                              action="retry", reason="operator retried",
                              workers_stopped=True)
        # The operator is still holding the version they read before that decision.
        code, _, err = self.run_cli(
            "resolve", "op-1", "--retry", "--expected-version", str(record.version),
            "--decision-id", "second", "--reason", "stale view", "--workers-stopped")
        self.assertEqual(code, 3)
        self.assertIn("Rejected", err)
        # The refusal has to name the version that exists now, not the stale one.
        current = self.executor.get("op-1")
        self.assertGreater(current.version, record.version)
        self.assertIn(f"state='ready' version={current.version}", err)

    def test_result_shape_is_validated_before_any_decision_is_stored(self):
        record = self.strand("op-1")
        for bad in ("{not json}", "{'single': 'quotes'}"):
            with self.subTest(bad=bad):
                code, _, err = self.run_cli(
                    "resolve", "op-1", "--complete", "--result-json", bad,
                    "--expected-version", str(record.version), "--decision-id", "d-1",
                    "--reason", "confirmed", "--workers-stopped")
                self.assertEqual(code, 2)
                self.assertIn("not valid JSON", err)
        self.assertEqual(self.executor.get("op-1").state, "indeterminate")

    def test_completion_and_retry_cannot_be_combined_or_left_incomplete(self):
        for args in (
            ["resolve", "op-1", "--complete", "--expected-version", "2",
             "--decision-id", "d", "--reason", "r", "--workers-stopped"],
            ["resolve", "op-1", "--retry", "--result-json", "{}", "--expected-version",
             "2", "--decision-id", "d", "--reason", "r", "--workers-stopped"],
            ["resolve", "op-1", "--complete", "--retry", "--expected-version", "2",
             "--decision-id", "d", "--reason", "r", "--workers-stopped"],
        ):
            with self.subTest(args=args[2:4]), self.assertRaises(SystemExit) as caught:
                self.run_cli(*args)
            self.assertEqual(caught.exception.code, 2)

    def test_replaying_one_decision_cannot_grant_a_second_attempt(self):
        record = self.strand("op-1")
        settle = ("resolve", "op-1", "--retry", "--expected-version", str(record.version),
                  "--decision-id", "d-1", "--reason", "operator retried",
                  "--workers-stopped")
        self.assertEqual(self.run_cli(*settle)[0], 0)
        self.assertEqual(self.executor.get("op-1").state, "ready")
        calls = []
        self.executor.execute("op-1", "message.send:v1", {"text": "hello"},
                              lambda o: calls.append(1) or {"id": 1})
        # The grant is spent; replaying the same decision must not issue another.
        self.assertEqual(self.run_cli(*settle)[0], 0)
        self.executor.execute("op-1", "message.send:v1", {"text": "hello"},
                              lambda o: calls.append(2) or {"id": 2})
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
