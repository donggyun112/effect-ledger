import tempfile
import unittest
from pathlib import Path

from langgraph_effect_ledger import EffectExecutor, SQLiteOperationStore, RecoveryDecision, OperationConflict


class CompositionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteOperationStore(Path(directory.name) / 'effects.db')

    def test_injected_store_and_bound_effect_replay_across_executors(self):
        calls = []
        one = EffectExecutor(store=self.store, scope='a')
        send = one.bind('send:v1', lambda op: calls.append(op.provider_key) or {'id': 1})
        original = send('host-id', {'text': 'hello'})
        two = EffectExecutor(store=self.store, scope='a')
        replay = two.bind('send:v1', lambda op: self.fail('duplicate'))('host-id', {'text': 'hello'})
        self.assertEqual(original, replay)
        self.assertEqual(len(calls), 1)

    def test_policy_requires_explicit_supervision_and_can_confirm_null(self):
        observed = []
        def policy(op):
            observed.append(op)
            return RecoveryDecision('complete', 'verified-1', 'Provider confirmed', result=None)
        executor = EffectExecutor(store=self.store, scope='a', recovery=policy)
        def fail(op):
            raise TimeoutError()
        pending = executor.execute('one', 'send:v1', {}, fail)
        self.assertEqual(observed, [])
        with self.assertRaises(ValueError):
            executor.recover('one', workers_stopped=False)
        self.assertEqual(observed, [])
        completed = executor.recover('one', workers_stopped=True)
        self.assertEqual(completed.state, 'completed')
        self.assertIsNone(completed.result)
        self.assertEqual(observed[0].provider_key, pending.provider_key)

    def test_default_policy_abstains_and_failure_never_grants_retry(self):
        executor = EffectExecutor(store=self.store, scope='a')
        def fail(op):
            raise TimeoutError()
        pending = executor.execute('one', 'send:v1', {}, fail)
        self.assertEqual(executor.recover('one', workers_stopped=True), pending)
        broken = EffectExecutor(store=self.store, scope='a', recovery=fail)
        with self.assertRaises(TimeoutError):
            broken.recover('one', workers_stopped=True)
        self.assertEqual(executor.get('one'), pending)

    def test_policy_retry_consumed_once_and_same_decision_cannot_regrant(self):
        executor = EffectExecutor(store=self.store, scope='a', recovery=lambda op:
            RecoveryDecision('retry', 'absence-1', 'Provider confirmed no effect'))
        keys = []
        def fail(op):
            keys.append(op.provider_key)
            raise TimeoutError()
        executor.execute('one', 'send:v1', {}, fail)
        self.assertEqual(executor.recover('one', workers_stopped=True).state, 'ready')
        executor.execute('one', 'send:v1', {}, fail)
        with self.assertRaises(OperationConflict):
            executor.recover('one', workers_stopped=True)
        executor.execute('one', 'send:v1', {}, lambda op: self.fail('third dispatch'))
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_policy_result_cannot_overwrite_concurrent_operator_decision(self):
        executor = EffectExecutor(store=self.store, scope='a')
        def policy(op):
            executor.resolve(op.operation_id, expected_version=op.version,
                decision_id='operator', action='complete', reason='Verified remotely',
                workers_stopped=True, result='actual')
            return RecoveryDecision('retry', 'stale-policy', 'Old absence observation')
        executor.recovery = policy
        def fail(op):
            raise TimeoutError()
        executor.execute('one', 'send:v1', {}, fail)
        with self.assertRaises(OperationConflict):
            executor.recover('one', workers_stopped=True)
        self.assertEqual(executor.get('one').result, 'actual')

    def test_constructor_rejects_ambiguous_or_missing_store(self):
        with self.assertRaises(ValueError):
            EffectExecutor(scope='a')
        with self.assertRaises(ValueError):
            EffectExecutor('another.db', store=self.store, scope='a')
