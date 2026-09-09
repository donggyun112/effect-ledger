"""Runnable execution-boundary demo: one local tool, no LLM/API credentials.

The tool writes a row to a local mailbox and then loses its reply. The claim is
already committed, so the resumed graph stops as unresolved instead of writing a
second row. Only an operator who checked the mailbox can release it.
"""

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

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


def mailbox_tool(mailbox: Path, lose_response: bool, latency: float = 0.0):
    """Build the one protected tool. Its arguments stay as the model wrote them.

    latency stands in for the round trip to a provider. It changes nothing about
    the ledger; it only gives the in_flight claim and the lost reply a duration
    long enough to observe from outside the process."""

    @tool
    def send_confirmation(order_id: str, text: str) -> dict:
        """Send one order confirmation."""
        time.sleep(latency)
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
            time.sleep(latency)
            raise ConnectionResetError("Response lost after the confirmation was sent")
        return {"message_id": message_id}

    return send_confirmation


class LedgerState(MessagesState):
    pending: dict[str, Any] | None


def ledger_graph():
    """Studio entry point that puts the ledger in the graph itself.

    `ExecutionBoundary` is middleware, so LangGraph has no node to draw for it
    and the boundary disappears into the tools node. Composed directly on
    `EffectExecutor` the boundary is a node and the ledger's verdict is an edge:
    completed carries on, anything unresolved stops. Resuming then walks
    unresolved -> ledger -> unresolved, because the refusal is the ledger's."""
    root = Path(os.environ.get("EFFECT_LEDGER_DEMO_DIR", "/tmp/boundary-demo")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    executor = EffectExecutor(root / "effects.sqlite", scope="account-1")
    mailbox, chat = root / "mailbox.sqlite", DemoModel()
    latency = float(os.environ.get("EFFECT_LEDGER_DEMO_LATENCY", "0"))

    def send(operation):
        """One external effect. The handler is handed the bound operation."""
        time.sleep(latency)
        with closing(sqlite3.connect(mailbox)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS messages "
                       "(order_id TEXT, body TEXT, provider_key TEXT)")
            db.execute("INSERT INTO messages VALUES (?, ?, ?)",
                       (operation.request["order_id"], operation.request["text"],
                        operation.provider_key))
            db.commit()
        time.sleep(latency)
        # The message is out. This process never learns that it went.
        raise ConnectionResetError("Response lost after the confirmation was sent")

    def model(state):
        return {"messages": [chat.invoke(state["messages"])]}

    def ledger(state, config):
        call = next(message for message in reversed(state["messages"])
                    if isinstance(message, AIMessage) and message.tool_calls).tool_calls[0]
        thread = config["configurable"]["thread_id"]
        status = executor.execute(f"orders:v1:{thread}:{call['id']}", "confirmation.send:v1",
                                  call["args"], send).response()
        if status["state"] != "completed":
            return {"pending": status}
        return {"pending": None, "messages": [ToolMessage(
            content=str(status["result"]), tool_call_id=call["id"], name=call["name"])]}

    def unresolved(state):
        # One interrupt per entry, so a resume walks back into the ledger and is
        # refused there. The cycle on screen is that refusal, not a retry.
        interrupt({**state["pending"], "kind": "effect_recovery"})
        return {}

    builder = StateGraph(LedgerState)
    builder.add_node("model", model)
    builder.add_node("ledger", ledger)
    builder.add_node("unresolved", unresolved)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", lambda state: "ledger" if getattr(
        state["messages"][-1], "tool_calls", None) else END, ["ledger", END])
    builder.add_conditional_edges(
        "ledger", lambda state: "unresolved" if state.get("pending") else "model",
        ["unresolved", "model"])
    builder.add_edge("unresolved", "ledger")
    return builder.compile()


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
    latency = float(os.environ.get("EFFECT_LEDGER_DEMO_LATENCY", "0"))
    return create_agent(DemoModel(), [mailbox_tool(root / "mailbox.sqlite", True, latency)],
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
