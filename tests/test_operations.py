"""Behavioral contract for server-owned effects; no provider mocks."""

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

from effect_ledger.operations import EffectExecutor, OperationConflict


class OperationsContract:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.sqlite"
        self.executor = self.make_executor()
        self.calls = []

    def make_executor(self, scope="account-a"):
        from effect_ledger import SQLiteOperationStore
        return EffectExecutor(store=SQLiteOperationStore(self.path), scope=scope)

    def effect(self, call):
        self.calls.append(call)
        return {"remote_id": "message-1", "status": "accepted"}

    def execute(self, handler=None, **kwargs):
        return self.executor.execute(
            "send-1", "send:v1", kwargs.get("request", {"text": "hello"}),
            handler or self.effect,
        )

    def resolve(self, record, decision="d1", action="retry", **kwargs):
        return self.executor.resolve(
            "send-1", expected_version=record.version, decision_id=decision,
            action=action, reason="Operator checked provider and stopped worker",
            workers_stopped=True, **kwargs,
        )

    def test_unresolved_lists_only_this_scope_and_grants_nothing(self):
        def lost(call):
            raise TimeoutError("Remote accepted but the response was lost")
        self.execute(handler=lost)
        self.executor.execute("done-1", "send:v1", {"text": "done"}, self.effect)
        other = self.make_executor(scope="account-b")
        other.execute("other-1", "send:v1", {"text": "elsewhere"}, lost)

        listed = self.executor.unresolved()
        self.assertEqual([r.operation_id for r in listed], ["send-1"])
        self.assertEqual(listed[0].state, "indeterminate")
        self.assertEqual(listed[0].request, {"text": "hello"})
        self.assertEqual([r.operation_id for r in other.unresolved()], ["other-1"])

        # Listing is a read. It must not move the operation or permit a retry.
        self.assertEqual(self.executor.get("send-1").version, listed[0].version)
        self.assertEqual(self.execute().state, "indeterminate")
        self.assertEqual(len(self.calls), 1)

        self.resolve(listed[0], action="complete", result={"remote_id": "message-1"})
        self.assertEqual(self.executor.unresolved(), [])
        for bad in (0, -1, "5"):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.executor.unresolved(limit=bad)

    def test_restart_replays_full_result_and_preserves_provider_key(self):
        first = self.execute()
        self.executor = self.make_executor()
        replay = self.execute()
        self.assertEqual(replay.result, {"remote_id": "message-1", "status": "accepted"})
        self.assertEqual(replay.state, "completed")
        self.assertEqual(first.provider_key, replay.provider_key)
        self.assertEqual(len(self.calls), 1)

    def test_request_is_fixed_before_handler_can_mutate_it(self):
        def mutate(call):
            call.request["text"] = "changed"
            return "ok"
        self.execute(mutate)
        self.assertEqual(self.executor.get("send-1").request, {"text": "hello"})
        with self.assertRaises(OperationConflict):
            self.execute(request={"text": "changed"})

    def test_changed_effect_conflicts_and_scope_isolates_operations(self):
        self.execute()
        with self.assertRaises(OperationConflict):
            self.executor.execute("send-1", "send:v2", {"text": "hello"}, self.effect)
        self.make_executor("account-b").execute(
            "send-1", "send:v1", {"text": "hello"}, self.effect,
        )
        self.assertEqual(len(self.calls), 2)

    def test_json_key_order_does_not_create_a_new_request(self):
        self.execute(request={"a": 1, "b": [True, None]})
        self.execute(request={"b": [True, None], "a": 1})
        self.assertEqual(len(self.calls), 1)

    def test_invalid_requests_never_execute(self):
        for request in ({"x": float("nan")}, {1: "value"}, {"x": (1, 2)}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.execute(request=request)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.executor.get("send-1"))

    def test_exception_never_implies_safe_retry(self):
        def failure(call):
            self.calls.append(call)
            raise TimeoutError("response lost")
        result = self.execute(failure)
        self.assertEqual(result.state, "indeterminate")
        self.assertEqual(result.response()["next_action"], "reconcile")
        self.assertEqual(self.execute().state, "indeterminate")
        self.assertEqual(len(self.calls), 1)

    def test_cancellation_leaves_attempt_blocked(self):
        def cancel(call):
            self.calls.append(call)
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.execute(cancel)
        self.assertIn(self.execute().state, {"in_flight", "indeterminate"})
        self.assertEqual(len(self.calls), 1)

    def test_unserializable_result_does_not_repeat_effect(self):
        def bad_result(call):
            self.calls.append(call)
            return object()
        self.assertEqual(self.execute(bad_result).state, "indeterminate")
        self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_retry_grant_is_consumed_once_even_when_decision_is_repeated(self):
        def fail(call):
            self.calls.append(call)
            raise TimeoutError()
        first = self.execute(fail)
        self.resolve(first)
        second = self.execute(fail)
        self.resolve(first)  # lost recovery response, same decision resent
        self.execute()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(first.provider_key, second.provider_key)
        with self.assertRaises(OperationConflict):
            self.resolve(first, decision="new-but-stale")
        self.resolve(second, decision="d2")
        self.assertEqual(self.execute().state, "completed")

    def test_completion_requires_result_and_preserves_explicit_null(self):
        def fail(call):
            raise TimeoutError()
        record = self.execute(fail)
        with self.assertRaises(ValueError):
            self.resolve(record, action="complete")
        self.resolve(record, action="complete", result=None)
        self.assertEqual(self.execute().state, "completed")
        self.assertIsNone(self.execute().result)
        self.assertEqual(self.calls, [])

    def test_recovery_requires_stopped_workers_and_rejects_reused_decision(self):
        def fail(call):
            raise TimeoutError()
        record = self.execute(fail)
        with self.assertRaises(ValueError):
            self.executor.resolve(
                "send-1", expected_version=record.version, decision_id="d1",
                action="retry", reason="not stopped", workers_stopped=False,
            )
        self.resolve(record)
        with self.assertRaises(OperationConflict):
            self.resolve(record, action="complete", result="invented")

    def test_duplicate_during_live_execution_never_dispatches(self):
        entered, release = Event(), Event()
        def blocking(call):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return "done"
        with ThreadPoolExecutor() as pool:
            task = pool.submit(self.execute, blocking)
            try:
                self.assertTrue(entered.wait(5))
                other = self.make_executor()
                duplicate = other.execute("send-1", "send:v1", {"text": "hello"}, self.effect)
                self.assertEqual(duplicate.state, "in_flight")
                self.assertEqual(duplicate.response()["next_action"], "wait")
                self.assertEqual(self.calls, [])
            finally:
                release.set()
            self.assertEqual(task.result().state, "completed")
        self.assertEqual(self.executor.get("send-1").response()["next_action"], "use_result")

    def test_late_completion_cannot_overwrite_recovery_result(self):
        entered, release = Event(), Event()
        def blocking(call):
            self.calls.append(call)  # effect happened before the commit conflict
            entered.set()
            release.wait(5)
            return "late worker result"
        with ThreadPoolExecutor() as pool:
            task = pool.submit(self.execute, blocking)
            try:
                self.assertTrue(entered.wait(5))
                # Deliberately violate the operator precondition to verify the
                # local version fence. This does NOT prove remote effect fencing.
                self.resolve(self.executor.get("send-1"), action="complete", result="verified result")
            finally:
                release.set()
            with self.assertRaises(OperationConflict):
                task.result()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.execute().result, "verified result")
        self.assertEqual(len(self.calls), 1)

    def test_concurrent_callers_consume_one_recovery_grant(self):
        def fail(call):
            raise TimeoutError()
        record = self.execute(fail)
        self.resolve(record)
        def retry(_):
            return self.make_executor().execute(
                "send-1", "send:v1", {"text": "hello"}, self.effect,
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(retry, range(8)))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.executor.get("send-1").attempt, 2)

    def test_same_decision_cannot_authorize_two_operations_concurrently(self):
        def fail(call):
            raise TimeoutError()
        one = self.execute(fail)
        two = self.executor.execute('send-2', 'send:v1', {}, fail)
        barrier = Barrier(2)
        def resolve(record):
            executor = self.make_executor()
            barrier.wait(timeout=5)
            try:
                return executor.resolve(record.operation_id, expected_version=record.version,
                    decision_id='shared-decision', action='retry', reason='Verified absent',
                    workers_stopped=True).state
            except OperationConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(resolve, [one, two]))
        self.assertCountEqual(outcomes, ['ready', 'conflict'])
        self.assertCountEqual([self.executor.get(op.operation_id).state for op in (one, two)],
                              ['ready', 'indeterminate'])

    def test_initial_claim_has_exactly_one_winner(self):
        barrier = Barrier(8)
        def claim(_):
            executor = self.make_executor()
            barrier.wait(timeout=5)
            return executor.store.claim(executor.scope, 'new', 'send:v1', {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(claim, range(8)))
        self.assertEqual(sum(claim.acquired for claim in claims), 1)
        self.assertEqual(len({claim.operation.provider_key for claim in claims}), 1)


class OperationsTest(OperationsContract, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
