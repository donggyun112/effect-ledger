"""Legacy ledger behavior checks."""

from dataclasses import dataclass, field
from types import SimpleNamespace

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

from langgraph_effect_ledger import (
    EffectLedger,
    InMemoryEffectStore,
    Record,
    StoredResult,
)

CHARGES: list[str] = []
ANSWERS: list[str] = []
TURNS: list[int] = []


@dataclass
class Ctx:
    effect_verdicts: dict[str, str] = field(default_factory=dict)


@tool
def charge(amount: int) -> str:
    """Charge the customer."""
    CHARGES.append(f"charge:{amount}")
    return f"charged {amount}"


@tool
def gate_then_charge(amount: int) -> str:
    """Ask first, then charge. The safe ordering."""
    answer = interrupt({"ask": "ok?"})
    ANSWERS.append(str(answer))
    CHARGES.append(f"charge:{amount}")
    return f"charged {amount} ({answer})"


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


def build(tool_obj, ledger: EffectLedger, thread: str):  # noqa: ANN001, ANN201
    TURNS.clear()
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": tool_obj.name, "args": {"amount": 100}}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model,
        [tool_obj],
        middleware=[ledger],
        checkpointer=InMemorySaver(),
        context_schema=Ctx,
    )
    return agent, {"configurable": {"thread_id": thread}}


def fake_request(call_id: str, verdicts: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"id": call_id, "name": "charge"},
        runtime=SimpleNamespace(context=Ctx(effect_verdicts=verdicts or {})),
        tool=charge,
    )


def test_completes_once() -> None:
    CHARGES.clear()
    store = InMemoryEffectStore()
    agent, config = build(charge, EffectLedger(store), "t-plain")
    agent.invoke({"messages": [("user", "go")]}, config, context=Ctx())
    assert CHARGES == ["charge:100"], CHARGES
    record = store.get("call-1")
    assert record.state == "completed"
    assert record.result.content == "charged 100"


def test_normal_hitl_resume_is_not_blocked() -> None:
    CHARGES.clear()
    ANSWERS.clear()
    store = InMemoryEffectStore()
    agent, config = build(gate_then_charge, EffectLedger(store), "t-hitl")

    first = agent.invoke({"messages": [("user", "go")]}, config, context=Ctx())
    assert first.get("__interrupt__"), "Expected a tool interrupt"
    assert store.get("call-1").state == "suspended"

    agent.invoke(Command(resume="ok"), config, context=Ctx())
    assert ANSWERS == ["ok"], ANSWERS
    assert CHARGES == ["charge:100"], CHARGES
    assert store.get("call-1").state == "completed"


def test_crash_leaves_indeterminate() -> None:
    store = InMemoryEffectStore()
    store.put("call-1", Record("started"))
    ledger = EffectLedger(store)

    decision = ledger._decide(fake_request("call-1"), "call-1")
    assert decision.action == "answer"
    assert isinstance(decision.message, ToolMessage)
    assert decision.message.status == "error"
    assert "INDETERMINATE" in decision.message.content
    assert store.get("call-1").state == "started", "Must remain unresolved"


def test_verdict_already_done_does_not_retry() -> None:
    store = InMemoryEffectStore()
    store.put("call-1", Record("started"))
    ledger = EffectLedger(store)

    decision = ledger._decide(fake_request("call-1", {"call-1": "already_done"}), "call-1")
    assert decision.action == "answer"
    assert store.get("call-1").state == "completed"


def test_verdict_retry_safe_runs_once_only() -> None:
    store = InMemoryEffectStore()
    store.put("call-1", Record("started"))
    ledger = EffectLedger(store)
    request = fake_request("call-1", {"call-1": "retry_safe"})

    decision = ledger._decide(request, "call-1")
    assert decision.action == "run"
    assert decision.record.verdict_spent is True

    ledger._begin("call-1", decision.record)
    assert store.get("call-1").verdict_spent is True


    decision = ledger._decide(request, "call-1")
    assert decision.action == "answer", "Verdict must be consumed once"
    assert "INDETERMINATE" in decision.message.content


def test_completed_call_replays_instead_of_rerunning() -> None:
    store = InMemoryEffectStore()
    store.put("call-1", Record("completed", result=StoredResult(content="charged 100")))
    ledger = EffectLedger(store)

    decision = ledger._decide(fake_request("call-1"), "call-1")
    assert decision.action == "answer"
    assert decision.message.content == "charged 100"


def test_failure_after_resume_leaves_it_unresolved() -> None:
    """Test failure after resume leaves it unresolved."""
    CHARGES.clear()
    store = InMemoryEffectStore()
    ledger = EffectLedger(store)
    fail = {"now": False}

    @tool
    def gate_then_charge_then_fail(amount: int) -> str:
        """Ask, charge, then fail on the resumed pass."""
        answer = interrupt({"ask": "ok?"})
        CHARGES.append(f"charge:{amount}")
        if fail["now"]:
            raise RuntimeError("died after the effect")
        return f"charged {amount} ({answer})"

    agent, config = build(gate_then_charge_then_fail, ledger, "t-fail")
    agent.invoke({"messages": [("user", "go")]}, config, context=Ctx())
    assert store.get("call-1").state == "suspended"

    fail["now"] = True
    try:
        agent.invoke(Command(resume="ok"), config, context=Ctx())
    except Exception:  # noqa: BLE001
        pass

    assert CHARGES == ["charge:100"], CHARGES
    assert store.get("call-1").state == "started", (
        "Failure during resume must remain unresolved"
    )


    decision = ledger._decide(fake_request("call-1"), "call-1")
    assert decision.action == "answer"
    assert "INDETERMINATE" in decision.message.content


def test_replay_preserves_status_and_artifact() -> None:
    store = InMemoryEffectStore()
    store.put(
        "call-1",
        Record(
            "completed",
            result=StoredResult(
                content="boom", status="error", artifact={"code": 42}, name="charge"
            ),
        ),
    )
    ledger = EffectLedger(store)

    decision = ledger._decide(fake_request("call-1"), "call-1")
    assert decision.action == "answer"
    message = decision.message
    assert message.status == "error", "Replay must preserve error status"
    assert message.artifact == {"code": 42}
    assert message.name == "charge"


def test_pending_and_resolve() -> None:
    store = InMemoryEffectStore()
    store.put("call-1", Record("started"))
    store.put("call-2", Record("completed", result=StoredResult(content="ok")))
    ledger = EffectLedger(store)

    assert set(ledger.pending()) == {"call-1"}

    ledger.resolve("call-1", "already_done")
    decision = ledger._decide(fake_request("call-1"), "call-1")
    assert decision.action == "answer"
    assert "confirmed out of band" in decision.message.content
    assert not ledger.pending()

    try:
        ledger.resolve("call-1", "probably_fine")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Unknown verdict must be rejected")


def main() -> None:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
            print("ok", name)


if __name__ == "__main__":
    main()
