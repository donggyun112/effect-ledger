"""Runnable execution-boundary demo: one local tool, no LLM/API credentials.

The tool writes a row to a local mailbox and then loses its reply. The claim is
already committed, so the resumed graph stops as unresolved instead of writing a
second row. Only an operator who checked the mailbox can release it.
"""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver

from effect_ledger import EffectExecutor
from effect_ledger.langchain import ExecutionBoundary, current_operation
from effect_ledger.langgraph import LedgerRunner

ORDER_ID = "order-123"
TEXT = "Your order shipped"


class DemoModel(BaseChatModel):
    """A deterministic model makes crash/recovery demonstrations reproducible."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage):
            message = AIMessage(content=f"Confirmation sent: {messages[-1].content}")
        else:
            message = AIMessage(content="", tool_calls=[{
                "id": "demo-call", "name": "send_confirmation",
                "args": {"order_id": ORDER_ID, "text": TEXT},
            }])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "local-demo"


def mailbox_tool(mailbox: Path, lose_response: bool):
    """Build the one protected tool. Its arguments stay as the model wrote them."""

    @tool
    def send_confirmation(order_id: str, text: str) -> dict:
        """Send one order confirmation."""
        with closing(sqlite3.connect(mailbox)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS messages "
                       "(order_id TEXT, body TEXT, provider_key TEXT)")
            # A real provider takes provider_key as its idempotency key.
            row = db.execute("INSERT INTO messages VALUES (?, ?, ?)",
                             (order_id, text, current_operation().provider_key))
            db.commit()
            message_id = row.lastrowid
        if lose_response:
            # The message is out. This process never learns that it went.
            raise ConnectionResetError("Response lost after the confirmation was sent")
        return {"message_id": message_id}

    return send_confirmation


def studio_graph():
    """Entry point for `langgraph dev`; see langgraph.json.

    The platform owns the checkpointer and the thread, so neither is built here.
    The tool always loses its reply: the point on screen is that resuming the
    graph does not send a second confirmation."""
    root = Path(os.environ.get("EFFECT_LEDGER_DEMO_DIR", "/tmp/boundary-demo")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    boundary = ExecutionBoundary(
        EffectExecutor(root / "effects.sqlite", scope="account-1"),
        workflow_id="orders:v1", tools={"send_confirmation": "confirmation.send:v1"})
    return create_agent(DemoModel(), [mailbox_tool(root / "mailbox.sqlite", True)],
                        middleware=[boundary])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--thread", default="order-workflow-123")
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
    executor = EffectExecutor(ledger, scope="account-1")

    if args.command == "confirm":
        record = executor.get(args.operation_id)
        with closing(sqlite3.connect(mailbox)) as db:
            row = db.execute("SELECT order_id, body FROM messages WHERE rowid=?",
                             (args.message_id,)).fetchone()
        if record is None or row is None or record.request != {"order_id": row[0], "text": row[1]}:
            raise ValueError("Mailbox row does not match the operation request")
        status = executor.resolve(
            args.operation_id, expected_version=args.version, decision_id=args.decision_id,
            action="complete",
            result=ExecutionBoundary.result(
                f"{record.request['order_id']} was already confirmed, mailbox row "
                f"{args.message_id}", artifact={"message_id": args.message_id}),
            reason="Operator verified local mailbox row and stopped prior workers",
            workers_stopped=args.workers_stopped,
        )
        print(json.dumps(status.response()))
        return

    boundary = ExecutionBoundary(executor, workflow_id="orders:v1",
                                 tools={"send_confirmation": "confirmation.send:v1"})
    effect = mailbox_tool(mailbox, getattr(args, "lose_response", False))
    with closing(sqlite3.connect(root / "checkpoints.sqlite", check_same_thread=False)) as db:
        runner = LedgerRunner(create_agent(
            DemoModel(), [effect], middleware=[boundary], checkpointer=SqliteSaver(db)))
        config = {"configurable": {"thread_id": args.thread}}
        if args.command == "status":
            state = runner.graph.get_state(config)
            values, pending = state.values, state.interrupts
        else:
            values = (runner.start({"messages": [("user", f"Confirm {ORDER_ID}")]}, config)
                      if args.command == "start" else runner.resume(config))
            pending = values.get("__interrupt__", [])
        print(json.dumps({
            "interrupts": [{"id": item.id, **item.value} for item in pending],
            "completed": not pending and bool(values.get("messages"))
                         and isinstance(values["messages"][-1], AIMessage)
                         and not values["messages"][-1].tool_calls,
            "last_message": values["messages"][-1].content if values.get("messages") else None,
        }))


if __name__ == "__main__":
    main()
