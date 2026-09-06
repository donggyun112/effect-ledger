"""Actual create_agent checkpoints and recovery; model is deterministic, no API."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver

from langgraph_effect_ledger.operations import EffectExecutor
from langgraph_effect_ledger.langgraph import DurableAgentRunner, durable_tool


class ScriptedModel(BaseChatModel):
    calls: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append([m.type for m in messages])
        if any(isinstance(m, ToolMessage) for m in messages):
            message = AIMessage(content="Workflow completed")
        else:
            message = AIMessage(content="", tool_calls=[{
                "id": "model-call-1", "name": "send_message",
                "args": {"request": {"text": "hello"}},
            }])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "durability-test"


class GraphFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executor = EffectExecutor(self.root / "effects.sqlite", scope="test")
        self.effects = []
        self.model = ScriptedModel()
        self.config = {"configurable": {"thread_id": "thread-1"}}

    def build(self, fail=False, transport=None, operation_id=None):
        def handler(call):
            self.effects.append(call)
            if fail:
                raise TimeoutError("effect applied but response lost")
            return {"message_id": 1}
        def execute(operation_id, effect, request):
            return self.executor.execute(operation_id, effect, request, handler).response()
        tool = durable_tool(
            name="send_message", description="Send one message",
            workflow_id="mail-agent:v1", effect="message.send:v1", execute=transport or execute,
            operation_id=operation_id,
        )
        connection = sqlite3.connect(self.root / "checkpoints.sqlite", check_same_thread=False)
        self.addCleanup(connection.close)
        graph = create_agent(self.model, [tool], checkpointer=SqliteSaver(connection))
        return DurableAgentRunner(graph)

    def resolve(self, pause, action="complete"):
        payload = pause["__interrupt__"][0].value
        kwargs = {"result": {"message_id": 1}} if action == "complete" else {}
        return self.executor.resolve(
            payload["operation_id"], expected_version=payload["version"],
            decision_id="decision-1", action=action, reason="Provider checked, worker stopped",
            workers_stopped=True, **kwargs,
        )


class GraphRecoveryTest(GraphFixture, unittest.TestCase):
    def test_host_identity_survives_resume_and_a_new_graph_thread(self):
        def identity(runtime):
            return runtime.config['configurable']['business_operation_id']
        self.config['configurable']['business_operation_id'] = 'order-37:confirmation'
        pause = self.build(fail=True, operation_id=identity).start(
            {'messages': [('user', 'send')]}, self.config)
        self.assertEqual(pause['__interrupt__'][0].value['operation_id'], 'order-37:confirmation')
        self.resolve(pause)
        self.build(operation_id=identity).resume(self.config)
        self.config['configurable']['thread_id'] = 'fresh-thread'
        done = self.build(operation_id=identity).start({'messages': [('user', 'send')]}, self.config)
        self.assertFalse(done.get('__interrupt__'))
        self.assertEqual(len(self.effects), 1)

    def test_host_identity_needs_no_derived_namespace_and_is_hidden(self):
        tool = durable_tool(name='send', description='send', effect='send:v1',
                            execute=lambda *args: None, operation_id=lambda runtime: 'host-id')
        self.assertEqual(set(tool.tool_call_schema.model_json_schema()['properties']), {'request'})

    def test_host_identity_failure_pauses_before_model_can_replan(self):
        def unavailable(runtime):
            raise ValueError('Business identity is unavailable')
        pause = self.build(operation_id=unavailable).start(
            {'messages': [('user', 'send')]}, self.config)
        self.assertTrue(pause.get('__interrupt__'))
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.effects), 0)
        done = self.build(operation_id=lambda runtime: 'restored-business-id').resume(self.config)
        self.assertFalse(done.get('__interrupt__'))
        self.assertEqual(self.effects[0].operation_id, 'restored-business-id')

    def test_unresolved_blocks_model_and_repeated_resume_then_completes(self):
        runner = self.build(fail=True)
        paused = runner.start({"messages": [("user", "send")]}, self.config)
        self.assertTrue(paused["__interrupt__"])
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.effects), 1)
        operation_id = paused["__interrupt__"][0].value["operation_id"]
        for _ in range(3):
            paused = self.build().resume(self.config)
            self.assertEqual(paused["__interrupt__"][0].value["operation_id"], operation_id)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.model.calls), 1)
        self.resolve(paused)
        done = self.build().resume(self.config)
        self.assertFalse(done.get("__interrupt__"))
        self.assertEqual(done["messages"][-1].content, "Workflow completed")
        tool_result = next(m for m in done["messages"] if isinstance(m, ToolMessage))
        self.assertEqual(json.loads(tool_result.content), {"message_id": 1})
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.model.calls), 2)

    def test_retry_uses_saved_request_and_same_operation_id(self):
        pause = self.build(fail=True).start({"messages": [("user", "send")]}, self.config)
        self.resolve(pause, action="retry")
        done = self.build().resume(self.config)
        self.assertFalse(done.get("__interrupt__"))
        self.assertEqual(len(self.effects), 2)
        self.assertEqual(self.effects[0].operation_id, self.effects[1].operation_id)
        self.assertEqual(self.effects[1].request, {"text": "hello"})
        self.build().resume(self.config)  # completed graph is not restarted
        self.assertEqual(len(self.effects), 2)
        self.assertEqual(len(self.model.calls), 2)

    def test_new_input_cannot_replace_an_unresolved_workflow(self):
        runner = self.build(fail=True)
        runner.start({"messages": [("user", "send")]}, self.config)
        with self.assertRaises(ValueError):
            runner.start({"messages": [("user", "send something else")]}, self.config)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.model.calls), 1)

    def test_tool_schema_hides_operation_identity_and_recovery_authority(self):
        tool = durable_tool(name="send", description="send", workflow_id="v1", effect="send:v1",
                            execute=lambda *args: None)
        self.assertEqual(set(tool.tool_call_schema.model_json_schema()["properties"]), {"request"})

    def test_model_intent_is_durable_before_effect_dispatch(self):
        def transport(operation_id, effect, request):
            with closing(sqlite3.connect(self.root / "checkpoints.sqlite")) as connection:
                saved = SqliteSaver(connection).get_tuple(self.config)
                last = saved.checkpoint["channel_values"]["messages"][-1]
                self.assertEqual(last.tool_calls[0]["args"], {"request": {"text": "hello"}})
            return self.executor.execute(operation_id, effect, request, lambda call: "ok").response()
        done = self.build(transport=transport).start({"messages": [("user", "send")]}, self.config)
        self.assertFalse(done.get("__interrupt__"))

    def test_transport_errors_and_malformed_completion_pause(self):
        def unavailable(*args):
            raise ConnectionError("server disconnected")
        pause = self.build(transport=unavailable).start({"messages": [("user", "send")]}, self.config)
        operation_id = pause["__interrupt__"][0].value["operation_id"]
        def malformed(*args):
            return {"operation_id": operation_id, "state": "completed", "unresolved": False}
        pause = self.build(transport=malformed).resume(self.config)
        self.assertEqual(pause["__interrupt__"][0].value["state"], "transport_error")
        self.assertEqual(len(self.model.calls), 1)
        done = self.build().resume(self.config)
        self.assertEqual(done["messages"][-1].content, "Workflow completed")

    def test_each_unresolved_resume_makes_only_one_transport_call(self):
        calls = []
        def transport(op_id, effect, request):
            calls.append(op_id)
            return {"operation_id": op_id, "state": "indeterminate", "unresolved": True,
                    "version": 2, "attempt": 1, "result": None, "error": "TimeoutError"}
        runner = self.build(transport=transport)
        runner.start({"messages": [("user", "send")]}, self.config)
        for _ in range(4):
            runner.resume(self.config)
        self.assertEqual(len(calls), 5)

    def test_in_memory_checkpoints_and_time_travel_are_rejected(self):
        from langgraph.checkpoint.memory import InMemorySaver
        with self.assertRaises(ValueError):
            DurableAgentRunner(create_agent(self.model, [], checkpointer=InMemorySaver()))
        runner = self.build()
        with self.assertRaises(ValueError):
            runner.start({"messages": [("user", "go")]}, {
                "configurable": {"thread_id": "one", "checkpoint_id": "old"},
            })
        self.assertEqual(self.effects, [])

    def test_ordinary_human_interrupt_is_not_automatically_approved(self):
        from langchain_core.tools import tool
        from langgraph.types import interrupt
        @tool
        def send_message(request: dict) -> str:
            """Ask for ordinary human approval, without an effect."""
            return "approved" if interrupt({"kind": "human_approval"}) else "declined"
        with closing(sqlite3.connect(self.root / "checkpoints.sqlite", check_same_thread=False)) as db:
            runner = DurableAgentRunner(create_agent(self.model, [send_message], checkpointer=SqliteSaver(db)))
            pause = runner.start({"messages": [("user", "ask")]}, self.config)
            with self.assertRaises(ValueError):
                runner.resume(self.config)
            done = runner.resume(self.config, responses={pause["__interrupt__"][0].id: False})
            result = next(m for m in done["messages"] if isinstance(m, ToolMessage))
            self.assertEqual(result.content, "declined")

    def test_parallel_tools_do_not_repeat_completed_sibling_on_resume(self):
        class ParallelModel(ScriptedModel):
            def _generate(self, messages, **kwargs):
                result = super()._generate(messages, **kwargs)
                message = result.generations[0].message
                if message.tool_calls:
                    message.tool_calls.append({"id": "model-call-2", "name": "send_message",
                                               "args": {"request": {"text": "fail"}}, "type": "tool_call"})
                return result
        self.model = ParallelModel()
        sent = []
        def handler(call):
            sent.append(call.request["text"])
            if call.request["text"] == "fail":
                raise TimeoutError()
            return {"id": "first"}
        def execute(op_id, effect, request):
            return self.executor.execute(op_id, effect, request, handler).response()
        runner = self.build(transport=execute)
        pause = runner.start({"messages": [("user", "send two")]}, self.config)
        self.assertEqual(sorted(sent), ["fail", "hello"])
        self.assertEqual(len(self.model.calls), 1)
        self.resolve(pause)
        done = runner.resume(self.config)
        self.assertEqual(done["messages"][-1].content, "Workflow completed")
        self.assertEqual(len([m for m in done["messages"] if isinstance(m, ToolMessage)]), 2)
        self.assertEqual(sorted(sent), ["fail", "hello"])

    def test_new_model_turn_with_reused_tool_call_id_is_a_distinct_operation(self):
        class RepeatingModel(ScriptedModel):
            def _generate(self, messages, **kwargs):
                # A new user turn requests an intentional second message. The
                # provider happens to reuse the tool call ID from the prior turn.
                visible = [messages[-1]] if messages[-1].type == "human" else messages
                return super()._generate(visible, **kwargs)
        self.model = RepeatingModel()
        runner = self.build()
        runner.start({"messages": [("user", "send")]}, self.config)
        runner.start({"messages": [("user", "send another")]}, self.config)
        self.assertEqual(len(self.effects), 2)
        self.assertNotEqual(self.effects[0].operation_id, self.effects[1].operation_id)


class AsyncGraphRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_agent_waits_for_operator_then_resumes(self):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = EffectExecutor(root / "effects.sqlite", scope="async")
            calls = []
            def handler(call):
                calls.append(call)
                raise TimeoutError()
            def execute(op_id, effect, request):
                return executor.execute(op_id, effect, request, handler).response()
            effect = durable_tool(name="send_message", description="send", workflow_id="async",
                                  effect="send:v1", execute=execute)
            model = ScriptedModel()
            config = {"configurable": {"thread_id": "async-thread"}}
            async with AsyncSqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
                runner = DurableAgentRunner(create_agent(model, [effect], checkpointer=saver))
                pause = await runner.astart({"messages": [("user", "send")]}, config)
                pause = await runner.aresume(config)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(model.calls), 1)
                with self.assertRaises(ValueError):
                    await runner.astart({"messages": [("user", "other")]}, config)
                data = pause["__interrupt__"][0].value
                executor.resolve(data["operation_id"], expected_version=data["version"],
                                 decision_id="async-1", action="complete", result={"id": 1},
                                 reason="verified", workers_stopped=True)
                done = await runner.aresume(config)
                self.assertEqual(done["messages"][-1].content, "Workflow completed")
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
