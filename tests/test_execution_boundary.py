"""Native LangChain tools behind one explicit effect boundary."""
import sqlite3
import asyncio
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool, ToolException
from langgraph.types import interrupt
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph_effect_ledger import EffectExecutor
from langgraph_effect_ledger.langchain import ExecutionBoundary, current_operation
from langgraph_effect_ledger.langgraph import DurableAgentRunner
from test_langgraph_recovery import ScriptedModel


class NativeModel(ScriptedModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = response.generations[0].message
        if isinstance(message, AIMessage) and message.tool_calls:
            message.tool_calls[0]['args'] = {'text': 'hello'}
            message.tool_calls[0]['id'] = 'native-' + uuid4().hex
        return response


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.executor = EffectExecutor(self.root / 'ledger.db', scope='test')
        self.model = NativeModel()
        self.calls = []
        self.operations = []
        self.config = {'configurable': {'thread_id': 'thread'}}

    def build(self, *, failure=False, outer=(), effects=None, operation_id=None, tool_error=False):
        @tool(response_format='content_and_artifact')
        def send_message(text: str):
            """Send one message."""
            self.calls.append(text)
            if effects != {}:
                self.operations.append(current_operation())
            if tool_error:
                raise ToolException('Unknown provider outcome')
            if failure:
                raise TimeoutError('Remote accepted but response lost')
            return 'Message sent', {'remote_id': 17}
        boundary = ExecutionBoundary(self.executor,
            effects={'send_message': 'message.send:v1'} if effects is None else effects,
            workflow_id='native:v1', operation_id=operation_id)
        send_message.handle_tool_error = True
        connection = sqlite3.connect(self.root / 'graph.db', check_same_thread=False)
        self.addCleanup(connection.close)
        graph = create_agent(self.model, [send_message], middleware=[*outer, boundary],
                             checkpointer=SqliteSaver(connection))
        return DurableAgentRunner(graph), send_message

    def test_native_tool_schema_and_result_survive_boundary(self):
        runner, native = self.build()
        self.assertEqual(set(native.tool_call_schema.model_json_schema()['properties']), {'text'})
        result = runner.start({'messages': [('user', 'send')]}, self.config)
        message = next(item for item in result['messages'] if isinstance(item, ToolMessage))
        self.assertEqual(message.content, 'Message sent')
        self.assertEqual(message.artifact, {'remote_id': 17})
        self.assertEqual(self.calls, ['hello'])

    def test_uncertain_tool_pauses_without_replanning_and_can_be_confirmed(self):
        runner, _ = self.build(failure=True)
        pause = runner.start({'messages': [('user', 'send')]}, self.config)
        pending = pause['__interrupt__'][0].value
        self.assertEqual(pending['state'], 'indeterminate')
        for _ in range(3):
            runner, _ = self.build()
            runner.resume(self.config)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.calls, ['hello'])
        self.executor.resolve(pending['operation_id'], expected_version=pending['version'],
            decision_id='confirmed', action='complete', reason='Provider verified',
            workers_stopped=True, result=ExecutionBoundary.result('Message sent', artifact={'remote_id': 17}))
        done = runner.resume(self.config)
        message = next(item for item in done['messages'] if isinstance(item, ToolMessage))
        self.assertEqual(message.artifact, {'remote_id': 17})
        self.assertEqual(self.calls, ['hello'])

    def test_result_is_committed_before_outer_projection_can_fail(self):
        class BrokenProjection(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                handler(request)
                raise RuntimeError('Projection crashed after effect')
        runner, _ = self.build(outer=[BrokenProjection()])
        with self.assertRaises(RuntimeError):
            runner.start({'messages': [('user', 'send')]}, self.config)
        runner, _ = self.build()
        result = runner.resume(self.config)
        self.assertFalse(result.get('__interrupt__'))
        self.assertEqual(self.calls, ['hello'])

    def test_unregistered_tool_passes_through(self):
        runner, _ = self.build(effects={})
        done = runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertFalse(done.get('__interrupt__'))
        self.assertEqual(self.calls, ['hello'])
        planned = next(item for item in done['messages'] if isinstance(item, AIMessage) and item.tool_calls)
        returned = next(item for item in done['messages'] if isinstance(item, ToolMessage))
        self.assertEqual(returned.tool_call_id, planned.tool_calls[0]['id'])

    def test_error_tool_message_is_not_a_completed_effect(self):
        runner, _ = self.build(tool_error=True)
        pause = runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertEqual(pause['__interrupt__'][0].value['state'], 'indeterminate')
        self.assertEqual(len(self.model.calls), 1)
        runner.resume(self.config)
        self.assertEqual(self.calls, ['hello'])

    def test_outer_retry_cannot_dispatch_a_completed_effect_again(self):
        class Retry(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                first = handler(request)
                second = handler(request)
                self.first = first
                return second
        runner, _ = self.build(outer=[Retry()])
        runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertEqual(self.calls, ['hello'])
        with self.assertRaises(LookupError):
            current_operation()

    def test_host_identity_replays_across_threads_and_rejects_modified_args(self):
        identity = lambda runtime: 'business-37'
        runner, _ = self.build(operation_id=identity)
        runner.start({'messages': [('user', 'send')]}, self.config)
        self.config['configurable']['thread_id'] = 'new-thread'
        done = runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertFalse(done.get('__interrupt__'))
        self.assertEqual(self.calls, ['hello'])
        class ChangeArgs(AgentMiddleware):
            def wrap_tool_call(self, request, handler):
                return handler(request.override(tool_call={**request.tool_call, 'args': {'text': 'changed'}}))
        runner, _ = self.build(operation_id=identity, outer=[ChangeArgs()])
        self.config['configurable']['thread_id'] = 'changed-thread'
        pause = runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertEqual(pause['__interrupt__'][0].value['error'], 'OperationConflict')
        self.assertEqual(self.calls, ['hello'])

    def test_native_human_approval_happens_before_effect_claim(self):
        runner, _ = self.build(outer=[HumanInTheLoopMiddleware(interrupt_on={'send_message': True})])
        pause = runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertTrue(pause.get('__interrupt__'))
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            runner.resume(self.config)
        done = runner.resume(self.config, responses={pause['__interrupt__'][0].id:
            {'decisions': [{'type': 'approve'}]}})
        self.assertFalse(done.get('__interrupt__'))
        self.assertEqual(self.calls, ['hello'])

    def test_internal_graph_interrupt_propagates_but_never_clears_the_claim(self):
        @tool
        def send_message(text: str):
            """Unsupported mixed approval/effect tool: keep the graph signal intact."""
            interrupt({'human': 'approve internal call'})
            self.calls.append(text)
            return 'sent'
        connection = sqlite3.connect(self.root / 'graph.db', check_same_thread=False)
        self.addCleanup(connection.close)
        runner = DurableAgentRunner(create_agent(self.model, [send_message],
            middleware=[ExecutionBoundary(self.executor, effects={'send_message': 'send:v1'},
                                          operation_id=lambda runtime: 'internal')],
            checkpointer=SqliteSaver(connection)))
        pause = runner.start({'messages': [('user', 'send')]}, self.config)
        self.assertEqual(pause['__interrupt__'][0].value, {'human': 'approve internal call'})
        self.assertEqual(self.executor.get('internal').state, 'in_flight')
        again = runner.resume(self.config, responses={pause['__interrupt__'][0].id: True})
        self.assertEqual(again['__interrupt__'][0].value['state'], 'in_flight')
        self.assertEqual(self.calls, [])


class AsyncBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_native_tool_pauses_recovers_and_preserves_context(self):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        with tempfile.TemporaryDirectory() as directory:
            executor = EffectExecutor(Path(directory) / 'effects.db', scope='test')
            calls = []
            @tool
            async def send_message(text: str):
                """Send one message."""
                await asyncio.sleep(0)
                calls.append(current_operation())
                raise TimeoutError()
            boundary = ExecutionBoundary(executor, effects={'send_message': 'send:v1'}, workflow_id='async')
            async with AsyncSqliteSaver.from_conn_string(str(Path(directory) / 'graph.db')) as saver:
                model = NativeModel()
                runner = DurableAgentRunner(create_agent(model, [send_message],
                    middleware=[boundary], checkpointer=saver))
                config = {'configurable': {'thread_id': 'async'}}
                pause = await runner.astart({'messages': [('user', 'send')]}, config)
                pending = pause['__interrupt__'][0].value
                await runner.aresume(config)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(model.calls), 1)
                executor.resolve(pending['operation_id'], expected_version=pending['version'],
                    decision_id='confirmed', action='complete', workers_stopped=True,
                    reason='Verified provider', result=ExecutionBoundary.result('Confirmed'))
                done = await runner.aresume(config)
                self.assertFalse(done.get('__interrupt__'))
                self.assertEqual(len(calls), 1)
                with self.assertRaises(LookupError):
                    current_operation()

    async def test_cancelled_async_attempt_is_never_automatically_reexecuted(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = EffectExecutor(Path(directory) / 'effects.db', scope='test')
            entered = asyncio.Event()
            async def handler(operation):
                entered.set()
                await asyncio.Event().wait()
            task = asyncio.create_task(executor.aexecute('one', 'send:v1', {}, handler))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            async def duplicate(operation):
                self.fail('cancelled attempt was dispatched again')
            result = await executor.aexecute('one', 'send:v1', {}, duplicate)
            self.assertEqual(result.state, 'in_flight')
