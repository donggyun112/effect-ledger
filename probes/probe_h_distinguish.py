from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt

CHARGES: list[str] = []
ASKS: list[str] = []
TURNS: list[int] = []
TRACE: list[str] = []


@dataclass
class Verdicts:
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
    TRACE.append(f"tool:effect#{len(CHARGES)}")
    answer = interrupt({"source": "tool", "ask": f"charged {amount}, confirm?"})
    ASKS.append(str(answer))
    TRACE.append(f"tool:resumed({answer})")
    return f"charged {amount} ({answer})"


class Ledger(AgentMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self.records: dict[str, str] = {}

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        call_id = request.tool_call["id"]
        state = self.records.get(call_id)
        TRACE.append(f"ledger:enter({state})")

        if state == "started":

            context = request.runtime.context
            verdict = (context.calls if context else {}).get(call_id)
            TRACE.append(f"ledger:indeterminate(verdict={verdict})")
            if verdict != "retry_safe":
                return ToolMessage(
                    content="INDETERMINATE", tool_call_id=call_id, status="error"
                )
        elif state == "completed":
            return ToolMessage(content="replayed", tool_call_id=call_id)

        self.records[call_id] = "started"
        try:
            result = handler(request)
        except GraphInterrupt:
            self.records[call_id] = "suspended"
            TRACE.append("ledger:caught GraphInterrupt")
            raise
        self.records[call_id] = "completed"
        return result


def pending(result: dict) -> list | None:
    return [i.value for i in result.get("__interrupt__", [])] or None


def main() -> None:
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
    config = {"configurable": {"thread_id": "probe-h"}}

    print("1 Initial:", pending(agent.invoke({"messages": [("user", "pay")]}, config, context=Verdicts())))
    print("   Ledger:", ledger.records, "<- suspended distinguishes an intentional interrupt")
    print("2 resume 'ok' :", pending(agent.invoke(Command(resume="ok"), config, context=Verdicts())))
    print("   Ledger:", ledger.records)
    print()
    print("Charge count:", len(CHARGES), "| Human answers:", ASKS)
    print("trace:", TRACE)
    print()
    print("H1 Interrupt detection:", "PASS" if "ledger:caught GraphInterrupt" in TRACE else "FAIL: no propagated exception")
    print("H2 Normal HITL:", "PASS" if ASKS else "FAIL: approval flow blocked")
    print("H3 Duplicate effects:", f"{len(CHARGES)}times" + (" (duplicate)" if len(CHARGES) > 1 else ""))


if __name__ == "__main__":
    main()
