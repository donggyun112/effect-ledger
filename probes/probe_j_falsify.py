import ast
import inspect
import textwrap
from typing import Callable

from langgraph.types import interrupt

LEDGER: list[str] = []
STATE: dict[str, int] = {}


def _charge(amount: int) -> None:
    LEDGER.append(f"charge:{amount}")


def _ask(question: str) -> str:
    return str(interrupt({"ask": question}))


def nested_loop_effect(items: list[int]) -> str:
    """Nested loop effect."""
    for item in items:
        _charge(item)
        answer = interrupt({"ask": f"confirm {item}?"})
    return str(answer)


def helper_interrupt(amount: int) -> str:
    """Helper interrupt."""
    _charge(amount)
    answer = _ask("confirm?")
    return answer


def assignment_effect(amount: int) -> str:
    """Assignment effect."""
    STATE["charged"] = amount
    answer = interrupt({"ask": "confirm?"})
    return str(answer)


def try_block_effect(amount: int) -> str:
    """Try block effect."""
    try:
        _charge(amount)
        answer = interrupt({"ask": "confirm?"})
    except KeyError:
        answer = "skipped"
    return str(answer)


def nested_pure_gate(flag: bool) -> str:
    """Nested pure gate."""
    if flag:
        answer = interrupt({"ask": "ok?"})
    else:
        answer = "auto"
    return str(answer)


FIXTURES: list[tuple[Callable, str]] = [
    (nested_loop_effect, "risky"),
    (helper_interrupt, "risky"),
    (assignment_effect, "risky"),
    (try_block_effect, "risky"),
    (nested_pure_gate, "safe"),
]


def _tree(func: Callable) -> ast.FunctionDef:
    return ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]  # type: ignore[return-value]


def _has_interrupt(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name) and child.func.id == "interrupt")
            or (isinstance(child.func, ast.Attribute) and child.func.attr == "interrupt")
        )
        for child in ast.walk(node)
    )


def detector_v2(func: Callable) -> str:
    tree = _tree(func)
    before: list[ast.stmt] = []
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        if _has_interrupt(statement):
            return (
                "risky"
                if any(isinstance(c, ast.Call) for s in before for c in ast.walk(s))
                else "safe"
            )
        before.append(statement)
    return "n/a"


def _effectful(node: ast.AST) -> bool:
    """ effectful."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and not _has_interrupt(child):
            return True
        if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = getattr(child, "targets", None) or [getattr(child, "target", None)]
            for target in targets:

                if isinstance(target, (ast.Subscript, ast.Attribute)):
                    return True
    return False


def detector_v3(func: Callable, module_funcs: dict[str, Callable] | None = None) -> str:
    """Detector v3."""
    tree = _tree(func)
    module_funcs = module_funcs or {}
    seen_effect = False
    verdict = "n/a"

    def walk(body: list[ast.stmt]) -> bool:
        """Walk."""
        nonlocal seen_effect, verdict
        for statement in body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                continue


            blocks = [
                b
                for attr in ("body", "orelse", "finalbody")
                for b in [getattr(statement, attr, None)]
                if b
            ]
            direct_interrupt = _has_interrupt(statement) and not any(
                _has_interrupt(ast.Module(body=b, type_ignores=[])) for b in blocks
            )

            if direct_interrupt:
                verdict = "risky" if seen_effect else "safe"
                return True

            if blocks:

                header_nodes = [
                    getattr(statement, name, None) for name in ("test", "iter", "items")
                ]
                for node in header_nodes:
                    if node is not None and _effectful(node):
                        seen_effect = True
                for block in blocks:
                    if walk(block):
                        return True
                continue


            for child in ast.walk(statement):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    target = module_funcs.get(child.func.id)
                    if target is not None and _has_interrupt(_tree(target)):
                        verdict = "risky" if seen_effect else "safe"
                        return True

            if _effectful(statement):
                seen_effect = True
        return False

    walk(tree.body)
    return verdict


def score(name: str, detector: Callable[[Callable], str]) -> None:
    print(f"== {name}")
    false_negatives: list[str] = []
    false_positives: list[str] = []
    for func, truth in FIXTURES:
        got = detector(func)
        ok = got == truth
        if not ok:
            (false_negatives if truth == "risky" else false_positives).append(func.__name__)
        print(f"  {'  ' if ok else '!!'} {func.__name__:22s} expected={truth:6s} actual={got}")
    print(f"  False negatives: {false_negatives or 'none'}")
    print(f"  False positives: {false_positives or 'none'}")
    print(f"  Soundness: {'holds' if not false_negatives else 'violated'}")
    print()


if __name__ == "__main__":
    score("v2 (passed probe_i)", detector_v2)
    helpers = {"_charge": _charge, "_ask": _ask}
    score("v3 (counterexamples handled)", lambda f: detector_v3(f, helpers))
