from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

EFFECTS: list[str] = []
WRAPPED: list[str] = []
MODEL_TURNS: list[int] = []


class Scripted(BaseChatModel):
    """Scripted."""

    script: list[AIMessage]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        index = len(MODEL_TURNS)
        MODEL_TURNS.append(index)
        message = self.script[min(index, len(self.script) - 1)]
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted"


@tool
def create_ticket() -> str:
    """Create a ticket. Runs without approval."""
    EFFECTS.append("create_ticket")
    return "ticket-1"


@tool
def send_email() -> str:
    """Send an email. Requires human approval."""
    decision = interrupt({"ask": "approve send_email?"})
    EFFECTS.append(f"send_email({decision})")
    return "sent"


class Probe(AgentMiddleware):
    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        WRAPPED.append(request.tool_call["name"])
        return handler(request)


def main() -> None:
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "create_ticket", "args": {}},
                    {"id": "c2", "name": "send_email", "args": {}},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model,
        [create_ticket, send_email],
        middleware=[Probe()],
        checkpointer=InMemorySaver(),
    )
    config: dict[str, Any] = {"configurable": {"thread_id": "probe-a"}}

    agent.invoke({"messages": [("user", "go")]}, config)
    print("After interrupt")
    print("  effects:", EFFECTS)
    print("  wrap_tool_call:", WRAPPED)

    agent.invoke(Command(resume="approved"), config)
    print("After resume")
    print("  effects:", EFFECTS)
    print("  wrap_tool_call:", WRAPPED)

    duplicated = EFFECTS.count("create_ticket")
    wrapped_ticket = WRAPPED.count("create_ticket")
    print()
    print(f"create_ticket effects: {duplicated}")
    print(f"create_ticket wrapper calls: {wrapped_ticket}")
    print(
        "Verdict:",
        "Middleware can intercept replay"
        if duplicated > 1 and wrapped_ticket == duplicated
        else "Replay bypasses wrap_tool_call"
        if duplicated > 1
        else "No replay",
    )


if __name__ == "__main__":
    main()
