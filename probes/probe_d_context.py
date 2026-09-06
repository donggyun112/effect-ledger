import sys
from dataclasses import dataclass, field

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
TRACE: list[str] = []


@dataclass
class Verdicts:
    """Verdicts."""

    calls: dict[str, str] = field(default_factory=dict)


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
    TRACE.append("tool:effect")
    note = interrupt({"source": "tool", "ask": f"charged {amount}, confirm?"})
    TRACE.append(f"tool:resumed({note})")
    return f"charged {amount} ({note})"


class Ledger(AgentMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self.records: dict[str, str] = {}

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        call_id = request.tool_call["id"]
        state = self.records.get(call_id)
        TRACE.append(f"ledger:enter({call_id},{state})")

        if state == "started":
            context = request.runtime.context
            verdict = (context.calls if context else {}).get(call_id)
            TRACE.append(f"ledger:verdict({verdict})")

            if verdict == "retry_safe":
                self.records[call_id] = "started"
                result = handler(request)
                self.records[call_id] = "completed"
                return result
            if verdict == "already_done":
                self.records[call_id] = "completed"
                return ToolMessage(
                    content="effect confirmed out of band; not retried",
                    tool_call_id=call_id,
                )
            return ToolMessage(
                content="INDETERMINATE: no verdict, refusing to retry",
                tool_call_id=call_id,
                status="error",
            )

        if state == "completed":
            return ToolMessage(content="replayed", tool_call_id=call_id)

        self.records[call_id] = "started"
        result = handler(request)
        self.records[call_id] = "completed"
        return result


def main(verdict: str | None) -> None:
    CHARGES.clear()
    TURNS.clear()
    TRACE.clear()
    ledger = Ledger()
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"id": "pay-1", "name": "charge", "args": {"amount": 100}}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model,
        [charge],
        middleware=[ledger],
        checkpointer=InMemorySaver(),
        context_schema=Verdicts,
    )
    config = {"configurable": {"thread_id": f"probe-d-{verdict}"}}

    print(f"== verdict={verdict!r}")
    first = agent.invoke(
        {"messages": [("user", "pay")]}, config, context=Verdicts()
    )
    print("  1 Initial:", [i.value for i in first.get("__interrupt__", [])] or None)

    context = Verdicts(calls={"pay-1": verdict} if verdict else {})
    final = agent.invoke(Command(resume="ok"), config, context=context)
    print("  2 resume:", [i.value for i in final.get("__interrupt__", [])] or None)

    print("  charges:", CHARGES, "| ledger:", ledger.records)
    print("  trace:", TRACE)
    tail = final["messages"][-1]
    print("  Final:", type(tail).__name__, repr(tail.content)[:70])
    print()


if __name__ == "__main__":
    for v in sys.argv[1:] or [None, "already_done", "retry_safe"]:
        main(None if v in (None, "none") else v)
