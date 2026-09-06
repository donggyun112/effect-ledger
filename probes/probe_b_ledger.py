from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

CHARGES: list[str] = []
TURNS: list[int] = []


class Scripted(BaseChatModel):
    script: list[AIMessage]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        index = len(TURNS)
        TURNS.append(index)
        return ChatResult(
            generations=[ChatGeneration(message=self.script[min(index, len(self.script) - 1)])]
        )

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted"


@tool
def charge(amount: int) -> str:
    """Charge the customer, then ask a human to confirm the receipt."""
    CHARGES.append(f"charge:{amount}")
    note = interrupt({"ask": f"charged {amount}, confirm?"})
    return f"charged {amount} ({note})"


class Ledger(AgentMiddleware):
    """Ledger."""

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[str, str] = {}
        self.blocked: list[str] = []

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        call_id = request.tool_call["id"]
        state = self.records.get(call_id)

        if state == "started":

            self.blocked.append(call_id)
            return ToolMessage(
                content="INDETERMINATE: Started without a result. "
                "Explicit retry authorization is required.",
                tool_call_id=call_id,
                status="error",
            )
        if state == "completed":
            return ToolMessage(content="replayed", tool_call_id=call_id)

        self.records[call_id] = "started"
        result = handler(request)
        self.records[call_id] = "completed"
        return result


def build(middleware: list[AgentMiddleware], thread: str):  # noqa: ANN201
    TURNS.clear()
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"id": "pay-1", "name": "charge", "args": {"amount": 100}}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(model, [charge], middleware=middleware, checkpointer=InMemorySaver())
    return agent, {"configurable": {"thread_id": thread}}


def run(label: str, middleware: list[AgentMiddleware]) -> None:
    CHARGES.clear()
    agent, config = build(middleware, label)
    agent.invoke({"messages": [("user", "pay")]}, config)
    print(f"{label} — charges after interrupt: {CHARGES}")
    agent.invoke(Command(resume="ok"), config)
    print(f"{label} — charges after resume: {CHARGES}")
    print(f"{label} — charge count: {len(CHARGES)}")
    print()


def main() -> None:
    run("B1(no ledger)", [])

    ledger = Ledger()
    run("B2(with ledger)", [ledger])
    print("Ledger records:", ledger.records)
    print("Blocked call IDs:", ledger.blocked)


if __name__ == "__main__":
    main()
