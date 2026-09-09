"""A ledger outlives the code that wrote it, so opening one is its own contract.

Losing the ledger breaks the guarantee, and so does failing to read it: a
migration that dies on open takes the operator's own triage commands with it.
"""

import os
import sqlite3
import tempfile
import unittest

from effect_ledger import EffectExecutor
from effect_ledger._sql import _MIGRATIONS, SCHEMA_VERSION


def _legacy(path):
    """Write the shape effect-ledger produced before schemas were versioned."""
    with sqlite3.connect(path) as db:
        for statement in _MIGRATIONS[0]:
            db.execute(statement)
        db.execute(
            "INSERT INTO operations VALUES "
            "(?, ?, ?, ?, ?, 'indeterminate', 1, 2, NULL, 'TimeoutError')",
            ("account-1", "charge-1", "payment.charge:v1", '{"amount":4200}', "key-1"),
        )


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "effects.sqlite")

    def open(self):
        return EffectExecutor(self.path, scope="account-1")

    def version(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT version FROM schema_version WHERE id=1").fetchone()[0]

    def columns(self):
        with sqlite3.connect(self.path) as db:
            return [row[1] for row in db.execute("PRAGMA table_info(operations)")]

    def test_a_fresh_ledger_is_stamped_with_the_current_version(self):
        self.open()
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertIn("created_at", self.columns())

    def test_an_unversioned_ledger_migrates_without_losing_its_operations(self):
        _legacy(self.path)
        record = self.open().get("charge-1")
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(record.state, "indeterminate")
        self.assertEqual(record.request, {"amount": 4200})
        # The row predates the column; inventing a creation time would be a lie.
        self.assertIsNone(record.created_at)

    def test_migrated_rows_stay_decidable(self):
        _legacy(self.path)
        executor = self.open()
        settled = executor.resolve(
            "charge-1", expected_version=2, decision_id="operator-charge-1",
            action="complete", result={"charge_id": "ch_77"},
            reason="provider shows ch_77; workers drained", workers_stopped=True)
        self.assertEqual(settled.state, "completed")

    def test_opening_twice_migrates_once(self):
        _legacy(self.path)
        self.open()
        self.open()  # A second ALTER would abort here rather than pass.
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_a_ledger_from_a_newer_release_is_refused_by_name(self):
        self.open()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE schema_version SET version=? WHERE id=1", (SCHEMA_VERSION + 1,))
        with self.assertRaises(ValueError) as caught:
            self.open()
        self.assertIn(f"v{SCHEMA_VERSION + 1}", str(caught.exception))

    def test_a_column_this_build_does_not_know_does_not_break_reads(self):
        """The failure the version stamp exists to prevent, reached the other way."""
        executor = self.open()
        executor.execute("op-1", "send:v1", {"text": "hello"}, lambda call: {"ok": True})
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE operations ADD COLUMN settled_by TEXT")
        self.assertEqual(self.open().get("op-1").state, "completed")

    def test_unresolved_lists_the_oldest_first(self):
        executor = self.open()
        for name in ("op-c", "op-b", "op-a"):
            executor.execute(name, "send:v1", {"text": name}, _raise)
        stamps = [record.created_at for record in executor.unresolved()]
        self.assertEqual(sorted(stamps), stamps)
        # Ordering by ID alone would have put op-a first.
        self.assertEqual([r.operation_id for r in executor.unresolved()][0], "op-c")


def _raise(call):
    raise TimeoutError("response lost")


if __name__ == "__main__":
    unittest.main()
