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
        message = self.script[min(index, len(self.script) - 1)]
        TRACE.append(f"model:turn{index}")
        return ChatResult(generations=[ChatGeneration(message=message)])

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
    """Ledger."""

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
                self.records[call_id] = "abandoned"
                return ToolMessage(
                    content="INDETERMINATE. Retry authorized but not performed here. "
                    "Issue a new tool call to retry.",
                    tool_call_id=call_id,
                )
            if verdict == "already_done":
                self.records[call_id] = "completed"
                return ToolMessage(content="not retried", tool_call_id=call_id)
            return ToolMessage(
                content="INDETERMINATE", tool_call_id=call_id, status="error"
            )

        if state in {"completed", "abandoned"}:
            return ToolMessage(content="not re-run", tool_call_id=call_id)

        self.records[call_id] = "started"
        result = handler(request)
        self.records[call_id] = "completed"
        return result


def pending(result: dict) -> list | None:
    return [i.value for i in result.get("__interrupt__", [])] or None


def main() -> None:
    ledger = Ledger()
    call = lambda cid: AIMessage(  # noqa: E731
        content="", tool_calls=[{"id": cid, "name": "charge", "args": {"amount": 100}}]
    )
    model = Scripted(script=[call("pay-1"), call("pay-2"), AIMessage(content="done")])
    agent = create_agent(
        model,
        [charge],
        middleware=[ledger],
        checkpointer=InMemorySaver(),
        context_schema=Verdicts,
    )
    config = {"configurable": {"thread_id": "probe-g"}}
    retry = Verdicts(calls={"pay-1": "retry_safe"})

    print("1 Initial:", pending(agent.invoke({"messages": [("user", "pay")]}, config, context=Verdicts())))
    print("2 resume 'ok'  :", pending(agent.invoke(Command(resume="ok"), config, context=retry)))
    print("3 resume 'ok2' :", pending(agent.invoke(Command(resume="ok2"), config, context=retry)))
    print()
    print("Charge count:", len(CHARGES), "| Human answers:", ASKS)
    print("Ledger:", ledger.records)
    print("Verdict:", "Fresh approval per retry" if len(ASKS) == len(CHARGES) else "Approval reused")
    print("trace:", TRACE)


if __name__ == "__main__":
    main()
