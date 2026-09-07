"""Real PostgreSQL runs the same contract as SQLite; no shared-table cleanup."""
import os
import unittest
from uuid import uuid4

from test_operation_crash import CrashContract, ProviderFixture
from test_operations import OperationsContract

from effect_ledger import EffectExecutor


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
