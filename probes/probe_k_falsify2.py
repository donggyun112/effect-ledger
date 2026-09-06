import ast
import sys
import textwrap
import inspect
from typing import Callable

from langchain_core.tools import tool
from langgraph.types import interrupt

sys.path.insert(0, str(__file__.rsplit("/", 1)[0]))
from probe_j_falsify import FIXTURES as J_FIXTURES  # noqa: E402
from probe_j_falsify import _effectful, _has_interrupt, _tree, detector_v3  # noqa: E402

LEDGER: list[str] = []


def _charge(amount: int) -> None:
    LEDGER.append(f"charge:{amount}")


def _ask(question: str) -> str:
    return str(interrupt({"ask": question}))


def _wrapper(question: str) -> str:
    return _ask(question)


HANDLERS: dict[str, Callable[[str], str]] = {"ask": _ask}


def loop_order(items: list[int]) -> str:
    """Loop order."""
    answer = "none"
    for item in items:
        answer = str(interrupt({"ask": f"confirm {item}?"}))
        _charge(item)
    return answer


def two_level_helper(amount: int) -> str:
    """Two level helper."""
    _charge(amount)
    return _wrapper("confirm?")


def dynamic_dispatch(amount: int) -> str:
    """Dynamic dispatch."""
    _charge(amount)
    return HANDLERS["ask"]("confirm?")


@tool
def decorated_tool(amount: int) -> str:
    """Decorated tool."""
    _charge(amount)
    answer = interrupt({"ask": "confirm?"})
    return str(answer)


def loop_pure_gate(items: list[int]) -> str:
    """Loop pure gate."""
    answer = "none"
    for item in items:
        answer = str(interrupt({"ask": f"ok {item}?"}))
    return answer


ATTACKS: list[tuple[Callable, str]] = [
    (loop_order, "risky"),
    (two_level_helper, "risky"),
    (dynamic_dispatch, "risky"),
    (decorated_tool, "risky"),
    (loop_pure_gate, "safe"),
]

HELPERS = {"_charge": _charge, "_ask": _ask, "_wrapper": _wrapper}


def detector_v4(func: Callable, module_funcs: dict[str, Callable]) -> str:
    """Detector v4."""
    try:
        tree = _tree(func)
    except (OSError, TypeError):
        return "unreadable"

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            body = ast.Module(body=node.body, type_ignores=[])
            if _has_interrupt(body) and any(
                _effectful(s) for s in node.body if not _has_interrupt(s)
            ):
                return "risky"
    return detector_v3(func, module_funcs)


def score(name: str, detector: Callable[[Callable], str], fixtures) -> list[str]:  # noqa: ANN001
    print(f"== {name}")
    false_negatives: list[str] = []
    false_positives: list[str] = []
    for func, truth in fixtures:
        try:
            got = detector(func)
        except Exception as error:  # noqa: BLE001
            got = f"error:{type(error).__name__}"
        ok = got == truth
        if not ok:
            (false_negatives if truth == "risky" else false_positives).append(
                getattr(func, "name", getattr(func, "__name__", "?"))
            )
        label = getattr(func, "name", getattr(func, "__name__", "?"))
        print(f"  {'  ' if ok else '!!'} {label:20s} expected={truth:6s} actual={got}")
    print(f"  False negatives: {false_negatives or 'none'}")
    print(f"  False positives: {false_positives or 'none'}")
    print(f"  Soundness: {'holds' if not false_negatives else 'violated'}")
    print()
    return false_negatives


if __name__ == "__main__":
    v3 = lambda f: detector_v3(f, HELPERS)  # noqa: E731
    v4 = lambda f: detector_v4(f, HELPERS)  # noqa: E731

    print("--- Counterexample round 2 ---")
    score("v3", v3, ATTACKS)
    score("v4 (loop handling)", v4, ATTACKS)

    print("--- Regression: round 1 fixtures ---")
    score("v4", v4, J_FIXTURES)
