"""Runnable durable agent demo: local MCP mailbox, no LLM/API credentials."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver
from mcp import StdioServerParameters

from langgraph_effect_ledger.langgraph import DurableAgentRunner, durable_tool
from langgraph_effect_ledger.mcp_client import StdioEffectClient
from langgraph_effect_ledger.operations import EffectExecutor


class DemoModel(BaseChatModel):
    """A deterministic model makes crash/recovery demonstrations reproducible."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage):
            message = AIMessage(content=f"Message confirmed: {messages[-1].content}")
        else:
            message = AIMessage(content="", tool_calls=[{
                "id": "demo-call", "name": "send_message", "args": {"request": {"text": "hello"}},
            }])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "local-demo"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--thread", default="demo-1")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--lose-response", action="store_true")
    commands.add_parser("status")
    commands.add_parser("resume")
    confirm = commands.add_parser("confirm", help="Operator verifies a local mailbox row")
    confirm.add_argument("--operation-id", required=True)
    confirm.add_argument("--version", type=int, required=True)
    confirm.add_argument("--decision-id", required=True)
    confirm.add_argument("--message-id", type=int, required=True)
    confirm.add_argument("--workers-stopped", action="store_true", required=True)
    args = parser.parse_args()
    root = args.state_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    ledger, mailbox = root / "effects.sqlite", root / "mailbox.sqlite"
    if args.command == "confirm":
        executor = EffectExecutor(ledger, scope="local-mailbox")
        record = executor.get(args.operation_id)
        with closing(sqlite3.connect(mailbox)) as db:
            row = db.execute("SELECT body FROM messages WHERE rowid=?", (args.message_id,)).fetchone()
        if record is None or row is None or record.request != {"text": row[0]}:
            raise ValueError("Mailbox row does not match the operation request")
        status = executor.resolve(
            args.operation_id, expected_version=args.version, decision_id=args.decision_id,
            action="complete", result={"message_id": args.message_id},
            reason="Operator verified local mailbox row and stopped prior workers",
            workers_stopped=args.workers_stopped,
        )
        print(json.dumps(status.response()))
        return

    server_args = [str(Path(__file__).with_name("mcp_server.py")),
                   "--ledger", str(ledger), "--mailbox", str(mailbox)]
    if getattr(args, "lose_response", False):
        server_args.append("--lose-response")
    client = StdioEffectClient(StdioServerParameters(command=sys.executable, args=server_args))
    effect = durable_tool(name="send_message", description="Send one message to the local mailbox",
                          workflow_id="durable-demo:v1", effect="message.send:v1", execute=client.execute)
    with closing(sqlite3.connect(root / "checkpoints.sqlite", check_same_thread=False)) as db:
        runner = DurableAgentRunner(create_agent(DemoModel(), [effect], checkpointer=SqliteSaver(db)))
        config = {"configurable": {"thread_id": args.thread}}
        if args.command == "status":
            state = runner.graph.get_state(config)
            values, pending = state.values, state.interrupts
        else:
            values = runner.start({"messages": [("user", "send hello")]}, config) if args.command == "start" else runner.resume(config)
            pending = values.get("__interrupt__", [])
        print(json.dumps({
            "interrupts": [{"id": item.id, **item.value} for item in pending],
            "completed": not pending and bool(values.get("messages")) and isinstance(values["messages"][-1], AIMessage)
                         and not values["messages"][-1].tool_calls,
            "last_message": values["messages"][-1].content if values.get("messages") else None,
        }))


if __name__ == "__main__":
    main()
