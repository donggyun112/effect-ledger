"""Fresh agent process with durable model, checkpoint and effect records."""

import json
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver

from langgraph_effect_ledger.langchain import ExecutionBoundary, current_operation
from langgraph_effect_ledger.langgraph import DurableAgentRunner, durable_tool
from langgraph_effect_ledger.operations import EffectExecutor

root, url, action, transport, boundary = Path(sys.argv[1]), *sys.argv[2:]


class Model(BaseChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        final = any(isinstance(m, ToolMessage) for m in messages)
        call_id = "call-" + uuid4().hex  # replanning would produce a DIFFERENT ID
        with closing(sqlite3.connect(root / "model.sqlite")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (stage TEXT, call_id TEXT)")
            db.execute("INSERT INTO calls VALUES (?, ?)", ("final" if final else "plan", call_id))
        message = AIMessage(content="Workflow completed") if final else AIMessage(content="", tool_calls=[{
            "id": call_id, "name": "send_message", "args": (
                {"text": "hello"} if transport == "boundary" else {"request": {"text": "hello"}}),
        }])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "crash-test"


def send(call):
    with urlopen(Request(url, json.dumps(call.request).encode(), method="POST"), timeout=15) as response:
        return json.load(response)


executor = EffectExecutor(root / "ledger.sqlite", scope="test-account")
if transport == "mcp":
    from mcp import StdioServerParameters

    from langgraph_effect_ledger.mcp_client import StdioEffectClient
    client = StdioEffectClient(StdioServerParameters(command=sys.executable, args=[
        str(Path(__file__).with_name("langgraph_http_server.py")), str(root / "ledger.sqlite"), url,
    ]))
    dispatch = client.execute
else:
    def dispatch(operation_id, effect, request):
        return executor.execute(operation_id, effect, request, send).response()


def execute(operation_id, effect, request):
    status = dispatch(operation_id, effect, request)
    if boundary == "after-ledger" and status["state"] == "completed":
        (root / "ledger-committed").touch()
        threading.Event().wait(30)  # parent kills us before the graph sees result
    return status


effect = durable_tool(name="send_message", description="send a message",
                      workflow_id="crash-agent:v1", effect="message.send:v1", execute=execute)
middleware = []
if transport == 'boundary':
    @tool
    def send_message(text: str):
        """Send one message."""
        return send(current_operation())
    class AfterCommit(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            message = handler(request)
            if boundary == 'after-ledger':
                (root / 'ledger-committed').touch()
                threading.Event().wait(30)
            return message
    effect = send_message
    middleware = [AfterCommit(), ExecutionBoundary(executor,
        tools={'send_message': 'message.send:v1'}, workflow_id='crash-agent:v1')]
with closing(sqlite3.connect(root / "checkpoints.sqlite", check_same_thread=False)) as db:
    runner = DurableAgentRunner(create_agent(Model(), [effect], middleware=middleware, checkpointer=SqliteSaver(db)))
    config = {"configurable": {"thread_id": "crash-thread"}}
    result = runner.start({"messages": [("user", "send")]}, config) if action == "start" else runner.resume(config)
    print(json.dumps({
        "interrupts": [item.value for item in result.get("__interrupt__", [])],
        "final": result["messages"][-1].content,
        "results": [json.loads(m.content) for m in result["messages"] if isinstance(m, ToolMessage)],
    }), flush=True)
