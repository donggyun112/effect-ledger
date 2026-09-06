import ast
import inspect
import textwrap
from typing import Callable

from langgraph.types import interrupt

LEDGER: list[str] = []


def _charge(amount: int) -> None:
    LEDGER.append(f"charge:{amount}")


def pure_gate(amount: int) -> str:
    """Pure gate."""
    answer = interrupt({"ask": "ok?"})
    _charge(amount)
    return f"{answer}"


def effect_then_gate(amount: int) -> str:
    """Effect then gate."""
    LEDGER.append(f"charge:{amount}")
    answer = interrupt({"ask": "confirm?"})
    return f"{answer}"


def indirect_effect_then_gate(amount: int) -> str:
    """Indirect effect then gate."""
    _charge(amount)
    answer = interrupt({"ask": "confirm?"})
    return f"{answer}"


def compute_then_gate(amount: int) -> str:
    """Compute then gate."""
    total = amount * 2
    label = f"total {total}"
    answer = interrupt({"ask": label})
    return f"{answer}"


def branch_effect_then_gate(amount: int) -> str:
    """Branch effect then gate."""
    if amount > 50:
        _charge(amount)
    answer = interrupt({"ask": "confirm?"})
    return f"{answer}"


FIXTURES: list[tuple[Callable, str]] = [
    (pure_gate, "safe"),
    (effect_then_gate, "risky"),
    (indirect_effect_then_gate, "risky"),
    (compute_then_gate, "safe"),
    (branch_effect_then_gate, "risky"),
]


def _tree(func: Callable) -> ast.FunctionDef:
    source = textwrap.dedent(inspect.getsource(func))
    return ast.parse(source).body[0]  # type: ignore[return-value]


def _is_interrupt_stmt(node: ast.AST) -> bool:
    """ is interrupt stmt."""
    return any(
        isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name) and child.func.id == "interrupt")
            or (isinstance(child.func, ast.Attribute) and child.func.attr == "interrupt")
        )
        for child in ast.walk(node)
    )


def _statements_before_interrupt(func: Callable) -> list[ast.stmt] | None:
    """ statements before interrupt."""
    tree = _tree(func)
    body = [s for s in tree.body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)]
    before: list[ast.stmt] = []
    for statement in body:
        if _is_interrupt_stmt(statement):
            return before
        before.append(statement)
    return None


def detector_v1(func: Callable) -> str:
    """Detector v1."""
    before = _statements_before_interrupt(func)
    if before is None:
        return "n/a"
    return "risky" if before else "safe"


def detector_v2(func: Callable) -> str:
    """Detector v2."""
    before = _statements_before_interrupt(func)
    if before is None:
        return "n/a"
    has_call = any(
        isinstance(child, ast.Call) for statement in before for child in ast.walk(statement)
    )
    return "risky" if has_call else "safe"


def score(name: str, detector: Callable[[Callable], str]) -> None:
    print(f"== {name}")
    false_negatives: list[str] = []
    false_positives: list[str] = []
    for func, truth in FIXTURES:
        got = detector(func)
        mark = "  " if got == truth else "!!"
        if got != truth:
            (false_negatives if truth == "risky" else false_positives).append(func.__name__)
        print(f"  {mark} {func.__name__:28s} expected={truth:6s} actual={got}")
    print(f"  False negatives: {false_negatives or 'none'}")
    print(f"  False positives: {false_positives or 'none'}")
    print(f"  Soundness: {'holds' if not false_negatives else 'violated'}")
    print()


if __name__ == "__main__":
    score("H1 detector_v1 — statements before interrupt", detector_v1)
    score("H2 detector_v2 — calls before interrupt", detector_v2)
