import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

LEDGER: list[str] = []
TURNS: list[int] = []


def _charge(amount: int) -> None:
    LEDGER.append(f"charge:{amount}")


def _ask(question: str) -> str:
    return str(interrupt({"ask": question}))


def _wrapper(question: str) -> str:
    return _ask(question)


HANDLERS: dict[str, Callable[[str], str]] = {"ask": _ask}


@tool
def direct(amount: int) -> str:
    """Charge then ask directly."""
    _charge(amount)
    return str(interrupt({"ask": "confirm?"}))


@tool
def two_level(amount: int) -> str:
    """Charge then ask two levels down."""
    _charge(amount)
    return _wrapper("confirm?")


@tool
def dynamic(amount: int) -> str:
    """Charge then ask through a dispatch table."""
    _charge(amount)
    return HANDLERS["ask"]("confirm?")


@tool
def no_interrupt(amount: int) -> str:
    """Charge and return. No suspension."""
    _charge(amount)
    return "done"


def unwrap(obj: Any) -> Callable | None:
    """Unwrap."""
    for attribute in ("func", "coroutine", "__wrapped__"):
        candidate = getattr(obj, attribute, None)
        if callable(candidate):
            return unwrap(candidate) or candidate
    return obj if inspect.isfunction(obj) else None


def static_finds_interrupt(obj: Any) -> str:
    """Static finds interrupt."""
    func = unwrap(obj)
    if func is None:
        return "unreadable"
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError):
        return "unreadable"
    found = any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "interrupt")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "interrupt")
        )
        for node in ast.walk(tree)
    )
    return "proven" if found else "not_found"


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


class Detector(AgentMiddleware):
    """Detector."""

    def __init__(self) -> None:
        super().__init__()
        self.suspends: set[str] = set()

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        name = request.tool_call["name"]
        try:
            return handler(request)
        except GraphInterrupt:
            self.suspends.add(name)
            raise


def observe(tool_obj) -> bool:  # noqa: ANN001
    """Observe."""
    TURNS.clear()
    detector = Detector()
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": tool_obj.name, "args": {"amount": 100}}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model,
        [tool_obj],
        middleware=[detector],
        checkpointer=InMemorySaver(),
        context_schema=Verdicts,
    )
    agent.invoke(
        {"messages": [("user", "go")]},
        {"configurable": {"thread_id": f"L-{tool_obj.name}"}},
        context=Verdicts(),
    )
    return tool_obj.name in detector.suspends


TOOLS = [(direct, True), (two_level, True), (dynamic, True), (no_interrupt, False)]

if __name__ == "__main__":
    print(f"{'Tool':12s} {'Static(source)':22s} {'Runtime(interrupt)':22s} Expected")
    static_miss: list[str] = []
    runtime_miss: list[str] = []
    for tool_obj, suspends in TOOLS:
        static = static_finds_interrupt(tool_obj)
        runtime = observe(tool_obj)
        if suspends and static != "proven":
            static_miss.append(tool_obj.name)
        if suspends != runtime:
            runtime_miss.append(tool_obj.name)
        print(f"{tool_obj.name:12s} {static:22s} {str(runtime):22s} {suspends}")

    print()
    print("Static misses:", static_miss or "none")
    print("Runtime misses:", runtime_miss or "none")
    print()
    print("Unwrap check: @tool source ->", static_finds_interrupt(direct))
