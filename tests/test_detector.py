"""Legacy mixed-effect detector checks."""

import warnings
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt

from langgraph_effect_ledger import MixedEffectDetector, MixedEffectWarning, analyze

LEDGER: list[str] = []
TURNS: list[int] = []


def _charge(amount: int) -> None:
    LEDGER.append(f"charge:{amount}")


def _ask(question: str) -> str:
    return str(interrupt({"ask": question}))


def _wrapper(question: str) -> str:
    return _ask(question)


@tool
def effect_then_gate(amount: int) -> str:
    """Charge, then ask. Dangerous."""
    _charge(amount)
    return str(interrupt({"ask": "confirm?"}))


@tool
def gate_then_effect(amount: int) -> str:
    """Ask, then charge. Safe."""
    answer = interrupt({"ask": "ok?"})
    _charge(amount)
    return str(answer)


@tool
def two_level(amount: int) -> str:
    """Charge, then ask two levels down. Static cannot see it."""
    _charge(amount)
    return _wrapper("confirm?")


@tool
def loop_order(items: list[int]) -> str:
    """Ask then charge inside a loop. Dangerous from the second pass."""
    answer = "none"
    for item in items:
        answer = str(interrupt({"ask": f"ok {item}?"}))
        _charge(item)
    return answer


@tool
def plain(amount: int) -> str:
    """Charge and return. Never suspends."""
    _charge(amount)
    return "done"


@dataclass
class Ctx:
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


def run(tool_obj, args: dict) -> MixedEffectDetector:  # noqa: ANN001
    TURNS.clear()
    detector = MixedEffectDetector(warn=False)
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": tool_obj.name, "args": args}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model,
        [tool_obj],
        middleware=[detector],
        checkpointer=InMemorySaver(),
        context_schema=Ctx,
    )
    agent.invoke(
        {"messages": [("user", "go")]},
        {"configurable": {"thread_id": f"t-{tool_obj.name}"}},
        context=Ctx(),
    )
    return detector


def verdict_of(tool_obj, args: dict) -> str | None:  # noqa: ANN001
    finding = run(tool_obj, args).findings.get(tool_obj.name)
    return finding.verdict if finding else None


def main() -> None:
    amount = {"amount": 100}


    assert verdict_of(effect_then_gate, amount) == "risky"
    assert verdict_of(gate_then_effect, amount) == "safe"
    assert verdict_of(loop_order, {"items": [1, 2]}) == "risky"


    assert verdict_of(two_level, amount) == "unknown"


    assert verdict_of(plain, amount) is None


    assert analyze(effect_then_gate)[0] == "risky"


    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        MixedEffectDetector()._record("x", effect_then_gate)
        MixedEffectDetector()._record("y", gate_then_effect)
    assert len(caught) == 1, [str(w.message) for w in caught]
    assert issubclass(caught[0].category, MixedEffectWarning)

    print("ok")
    print(run(effect_then_gate, amount).report())
    print(run(two_level, amount).report())


if __name__ == "__main__":
    main()
