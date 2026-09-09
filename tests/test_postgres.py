"""Real PostgreSQL runs the same contract as SQLite; no shared-table cleanup."""
import os
import unittest
from uuid import uuid4

from test_operation_crash import CrashContract, ProviderFixture
from test_operations import OperationsContract

from effect_ledger import EffectExecutor
from effect_ledger._sql import _MIGRATIONS, SCHEMA_VERSION


@unittest.skipUnless(os.environ.get('EFFECT_LEDGER_TEST_DSN'), 'Set EFFECT_LEDGER_TEST_DSN for real PostgreSQL')
class PostgresTest(OperationsContract, unittest.TestCase):
    def setUp(self):
        self.namespace = str(uuid4())
        self.calls = []
        self.executor = self.make_executor()

    def make_executor(self, scope='account-a'):
        from effect_ledger.postgres import PostgresOperationStore
        return EffectExecutor(store=PostgresOperationStore(os.environ['EFFECT_LEDGER_TEST_DSN']),
                              scope=self.namespace + scope)


@unittest.skipUnless(os.environ.get('EFFECT_LEDGER_TEST_DSN'), 'Set EFFECT_LEDGER_TEST_DSN for real PostgreSQL')
class PostgresCrashTest(CrashContract, ProviderFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db = os.environ['EFFECT_LEDGER_TEST_DSN']
        self.scope = str(uuid4())


@unittest.skipUnless(os.environ.get('EFFECT_LEDGER_TEST_DSN'), 'Set EFFECT_LEDGER_TEST_DSN for real PostgreSQL')
class PostgresMigrationTest(unittest.TestCase):
    """Upgrading a shared ledger runs once and cannot be rehearsed in production.

    In its own schema, so the pre-versioning shape never meets the contract tables.
    """

    def setUp(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        self.psycopg = psycopg
        self.schema = 'migration_' + uuid4().hex[:8]
        self.dsn = make_conninfo(os.environ['EFFECT_LEDGER_TEST_DSN'],
                                 options=f'-csearch_path={self.schema}')
        with psycopg.connect(os.environ['EFFECT_LEDGER_TEST_DSN'], autocommit=True) as db:
            db.execute(f'CREATE SCHEMA {self.schema}')
        self.addCleanup(self._drop)
        with psycopg.connect(self.dsn, autocommit=True) as db:
            for statement in _MIGRATIONS[0]:
                db.execute(statement)

    def _drop(self):
        with self.psycopg.connect(os.environ['EFFECT_LEDGER_TEST_DSN'], autocommit=True) as db:
            db.execute(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')

    def store(self):
        from effect_ledger.postgres import PostgresOperationStore
        return PostgresOperationStore(self.dsn)

    def query(self, statement, parameters=()):
        with self.psycopg.connect(self.dsn, autocommit=True) as db:
            return db.execute(statement, parameters).fetchone()

    def test_an_unversioned_ledger_migrates_and_keeps_its_unresolved_work(self):
        with self.psycopg.connect(self.dsn, autocommit=True) as db:
            db.execute('INSERT INTO operations VALUES '
                       "(%s, %s, %s, %s, %s, 'indeterminate', 1, 2, NULL, 'TimeoutError')",
                       ('acct', 'charge-1', 'payment.charge:v1', '{"amount":4200}', 'key-1'))
        store = self.store()
        self.store()  # A second ALTER would abort the transaction rather than pass.
        self.assertEqual(self.query('SELECT version FROM schema_version WHERE id=1')[0],
                         SCHEMA_VERSION)
        record = store.get('acct', 'charge-1')
        self.assertEqual(record.state, 'indeterminate')
        self.assertEqual(record.request, {'amount': 4200})
        # The row predates the column; inventing a creation time would be a lie.
        self.assertIsNone(record.created_at)

    def test_a_ledger_from_a_newer_release_is_refused_by_name(self):
        self.store()
        with self.psycopg.connect(self.dsn, autocommit=True) as db:
            db.execute('UPDATE schema_version SET version=%s WHERE id=1', (SCHEMA_VERSION + 1,))
        with self.assertRaises(ValueError) as caught:
            self.store()
        self.assertIn(f'v{SCHEMA_VERSION + 1}', str(caught.exception))
