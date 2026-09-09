"""Real root graphs exercise the node's durable per-call execution boundary."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from effect_ledger import EffectExecutor
from effect_ledger import langgraph as adapter
from effect_ledger.langchain import READ_ONLY, ExecutionBoundary, current_operation


def build_graph(node, saver):
    builder = StateGraph(MessagesState)
    builder.add_node('tools', node)
    builder.add_edge(START, 'tools')
    builder.add_edge('tools', END)
    return adapter.LedgerRunner(builder.compile(checkpointer=saver))


def inputs(*names, parent='parent'):
    return {'messages': [AIMessage(content='', id=parent, tool_calls=[
        {'name': name, 'args': {'text': str(index)}, 'id': f'call-{index}'}
        for index, name in enumerate(names)
    ])]}


class LedgerToolNodeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.executor = EffectExecutor(self.root / 'effects.db', scope='test')
        connection = sqlite3.connect(self.root / 'graph.db', check_same_thread=False)
        self.addCleanup(connection.close)
        self.saver = SqliteSaver(connection)
        self.config = {'configurable': {'thread_id': 'thread'}}
        self.calls = []

    def node(self, tools, **kwargs):
        self.assertTrue(hasattr(adapter, 'LedgerToolNode'), 'LedgerToolNode API is missing')
        return adapter.LedgerToolNode(tools, executor=self.executor,
                                      workflow_id='node-test', **kwargs)

    def resolve(self, pending, *, action='complete'):
        self.executor.resolve(
            pending['operation_id'], expected_version=pending['version'],
            decision_id=f"confirmed-{pending['operation_id']}-{pending['version']}",
            action=action, workers_stopped=True, reason='Test provider reconciled',
            **({'result': ExecutionBoundary.result('confirmed', artifact={'receipt': 9})}
               if action == 'complete' else {}),
        )

    def test_multiple_calls_keep_schemas_artifacts_and_distinct_identities(self):
        @tool(response_format='content_and_artifact')
        def send(text: str):
            """Send one message."""
            self.calls.append(current_operation())
            return text, {'receipt': text}

        node = self.node([send], policies={'send': 'mail.send:v1'})
        self.assertEqual(set(send.tool_call_schema.model_json_schema()['properties']), {'text'})
        runner = build_graph(node, self.saver)
        done = runner.start(inputs('send', 'send'), self.config)
        replies = [message for message in done['messages'] if isinstance(message, ToolMessage)]
        self.assertEqual([message.tool_call_id for message in replies], ['call-0', 'call-1'])
        self.assertEqual([message.artifact for message in replies], [{'receipt': '0'}, {'receipt': '1'}])
        self.assertEqual(len({record.operation_id for record in self.calls}), 2)
        self.assertTrue(all(record.effect == 'mail.send:v1' for record in self.calls))
        runner.resume(self.config)
        self.assertEqual(len(self.calls), 2)
        runner.start(inputs('send', parent='next-parent'), self.config)
        self.assertEqual(len(self.calls), 3)  # Reused call ID, new intentional action.

    def test_partial_batch_replays_completed_tools_after_reconstruction(self):
        @tool
        def good(text: str):
            """Complete an external effect."""
            self.calls.append(('good', text))
            return 'sent'

        @tool
        def uncertain(text: str):
            """Lose the provider reply after an effect."""
            self.calls.append(('uncertain', text))
            raise TimeoutError('Reply lost')

        runner = build_graph(self.node([good, uncertain]), self.saver)
        stopped = runner.start(inputs('good', 'uncertain'), self.config)
        pending = stopped['__interrupt__'][0].value
        self.assertEqual(pending['state'], 'indeterminate')
        for _ in range(3):
            runner = build_graph(self.node([good, uncertain]), self.saver)
            self.assertTrue(runner.resume(self.config).get('__interrupt__'))
        self.assertCountEqual(self.calls, [('good', '0'), ('uncertain', '1')])
        self.resolve(pending)
        done = runner.resume(self.config)
        self.assertFalse(done.get('__interrupt__'))
        replies = [message for message in done['messages'] if isinstance(message, ToolMessage)]
        self.assertEqual([message.content for message in replies], ['sent', 'confirmed'])
        self.assertEqual(replies[1].artifact, {'receipt': 9})
        self.assertEqual(len(self.calls), 2)

    def test_multiple_uncertain_calls_can_be_resolved_one_at_a_time(self):
        @tool
        def send(text: str):
            """Lose one reply."""
            self.calls.append(text)
            raise TimeoutError()

        runner = build_graph(self.node([send]), self.saver)
        stopped = runner.start(inputs('send', 'send'), self.config)
        self.assertEqual(len(self.calls), 2)
        for _ in range(2):
            pending = stopped['__interrupt__'][0].value
            self.resolve(pending)
            stopped = runner.resume(self.config)
        self.assertFalse(stopped.get('__interrupt__'))
        self.assertEqual(len(self.calls), 2)

    def test_only_trusted_retry_allows_another_attempt(self):
        @tool
        def send(text: str):
            """Fail once, then succeed."""
            self.calls.append(text)
            if len(self.calls) == 1:
                raise TimeoutError()
            return 'sent'

        runner = build_graph(self.node([send]), self.saver)
        stopped = runner.start(inputs('send'), self.config)
        runner.resume(self.config)
        self.assertEqual(len(self.calls), 1)
        self.resolve(stopped['__interrupt__'][0].value, action='retry')
        done = runner.resume(self.config)
        self.assertFalse(done.get('__interrupt__'))
        self.assertEqual(len(self.calls), 2)

    def test_read_only_bypasses_ledger_and_host_identity_rejects_changed_args(self):
        @tool
        def search(text: str):
            """Read a value."""
            with self.assertRaises(LookupError):
                current_operation()
            return text

        @tool
        def send(text: str):
            """Send one message."""
            self.calls.append(text)
            return text

        node = self.node([search, send], policies={'search': READ_ONLY},
                         operation_id=lambda runtime: 'business-message')
        runner = build_graph(node, self.saver)
        runner.start(inputs('search'), self.config)
        self.assertIsNone(self.executor.get('business-message'))
        runner.start(inputs('send', parent='send-parent'), self.config)
        changed = inputs('send', parent='changed-parent')
        changed['messages'][0].tool_calls[0]['args']['text'] = 'changed'
        stopped = runner.start(changed, self.config)
        self.assertEqual(stopped['__interrupt__'][0].value['error'], 'OperationConflict')
        self.assertEqual(self.calls, ['0'])

    def test_read_only_keeps_native_tool_node_error_handling(self):
        @tool
        def search(query: str):
            """Read one query."""
            return query

        runner = build_graph(self.node([search], policies={'search': READ_ONLY}), self.saver)
        # inputs() supplies `text`, so native ToolNode schema validation reports
        # an error ToolMessage without creating a durable effect operation.
        done = runner.start(inputs('search'), self.config)
        reply = next(message for message in done['messages'] if isinstance(message, ToolMessage))
        self.assertEqual(reply.status, 'error')
        self.assertIsNone(next(iter(self.executor.unresolved()), None))


class AsyncLedgerToolNodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_partial_batch_preserves_context_and_completed_results(self):
        self.assertTrue(hasattr(adapter, 'LedgerToolNode'), 'LedgerToolNode API is missing')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = EffectExecutor(root / 'effects.db', scope='test')
            calls = []

            @tool
            async def send(text: str):
                """Send one message asynchronously."""
                calls.append(current_operation())
                await asyncio.sleep(0)
                if text == '1':
                    raise TimeoutError()
                return text

            async with AsyncSqliteSaver.from_conn_string(str(root / 'graph.db')) as saver:
                node = adapter.LedgerToolNode([send], executor=executor, workflow_id='async')
                runner = build_graph(node, saver)
                config = {'configurable': {'thread_id': 'async'}}
                stopped = await runner.astart(inputs('send', 'send'), config)
                pending = stopped['__interrupt__'][0].value
                await runner.aresume(config)
                self.assertEqual(len(calls), 2)
                executor.resolve(pending['operation_id'], expected_version=pending['version'],
                    decision_id='confirmed', action='complete', workers_stopped=True,
                    reason='Provider verified', result=ExecutionBoundary.result('confirmed'))
                done = await runner.aresume(config)
                self.assertFalse(done.get('__interrupt__'))
                self.assertEqual(len(calls), 2)
                with self.assertRaises(LookupError):
                    current_operation()
