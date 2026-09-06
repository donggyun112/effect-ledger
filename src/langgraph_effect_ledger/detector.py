"""Legacy detector for effects preceding an internal interrupt. See docs/legacy-middleware.md."""

from __future__ import annotations

import ast
import inspect
import textwrap
import warnings
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langgraph.errors import GraphInterrupt

__all__ = ["Finding", "MixedEffectDetector", "MixedEffectWarning", "analyze"]


class MixedEffectWarning(UserWarning):
    """A tool may repeat an effect when resumed."""


@dataclass(frozen=True)
class Finding:
    tool: str
    verdict: str  # "risky" | "safe" | "unknown"
    detail: str

    def __str__(self) -> str:
        return f"[{self.verdict}] {self.tool}: {self.detail}"


def unwrap(obj: Any) -> Callable | None:
    """Unwrap a tool to its function, or return None."""
    for attribute in ("func", "coroutine", "__wrapped__"):
        inner = getattr(obj, attribute, None)
        if callable(inner):
            return unwrap(inner) or inner
    return obj if inspect.isfunction(obj) else None


def _tree(func: Callable) -> ast.FunctionDef | None:
    try:
        parsed = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return None
    body = parsed.body[0]
    return body if isinstance(body, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _is_interrupt_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    target = node.func
    return (isinstance(target, ast.Name) and target.id == "interrupt") or (
        isinstance(target, ast.Attribute) and target.attr == "interrupt"
    )


def _holds_interrupt(node: ast.AST) -> bool:
    return any(_is_interrupt_call(child) for child in ast.walk(node))


def _blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    return [
        block
        for name in ("body", "orelse", "finalbody")
        for block in [getattr(statement, name, None)]
        if block
    ]


def _effectful(node: ast.AST) -> bool:
    """Detect calls and writes to nonlocal state."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and not _is_interrupt_call(child):
            return True
        targets = (
            getattr(child, "targets", None) or [getattr(child, "target", None)]
            if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign))
            else []
        )
        if any(isinstance(t, (ast.Subscript, ast.Attribute)) for t in targets if t):
            return True
    return False


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _helper_interrupts(
    statement: ast.stmt, namespace: dict[str, Any]
) -> tuple[bool, bool]:
    """Return whether a called helper interrupts and contains effects."""
    for child in ast.walk(statement):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            helper = namespace.get(child.func.id)
            tree = _tree(helper) if callable(helper) else None
            if tree is not None and _holds_interrupt(tree):
                return True, any(_effectful(s) for s in tree.body)
    return False, False


def analyze(obj: Any) -> tuple[str, str]:
    """Return (verdict, detail), with verdict risky, safe, or unknown."""
    func = unwrap(obj)
    if func is None:
        return "unknown", "Could not unwrap function"
    tree = _tree(func)
    if tree is None:
        return "unknown", "Could not read source"
    namespace = getattr(func, "__globals__", {})


    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            body = node.body
            if any(_holds_interrupt(s) for s in body) and any(
                _effectful(s) for s in body if not _holds_interrupt(s)
            ):
                return "risky", "Loop contains effects and interrupts"

    seen_effect = False
    found_interrupt = False
    risky_reason: str | None = None

    def walk(body: list[ast.stmt]) -> None:


        nonlocal seen_effect, found_interrupt, risky_reason
        for statement in body:
            if _is_docstring(statement):
                continue
            blocks = _blocks(statement)
            nested_interrupt = any(
                _holds_interrupt(ast.Module(body=block, type_ignores=[])) for block in blocks
            )

            if _holds_interrupt(statement) and not nested_interrupt:
                found_interrupt = True
                if seen_effect and risky_reason is None:
                    risky_reason = "Effect precedes interrupt"
                continue

            if blocks:

                for name in ("test", "iter", "items"):
                    header = getattr(statement, name, None)
                    if header is not None and _effectful(header):
                        seen_effect = True
                for block in blocks:
                    walk(block)
                continue

            calls_gate, gate_has_effect = _helper_interrupts(statement, namespace)
            if calls_gate:
                found_interrupt = True
                if risky_reason is None:
                    if gate_has_effect:
                        risky_reason = "Called helper contains effects and interrupts"
                    elif seen_effect:
                        risky_reason = "Effect precedes interrupt"
                continue

            if _effectful(statement):
                seen_effect = True

    walk(tree.body)
    if risky_reason is not None:
        return "risky", risky_reason
    if found_interrupt:
        return "safe", "No effect precedes interrupt"
    return "unknown", "Interrupt not found in source; may be indirect or dynamic"


class MixedEffectDetector(AgentMiddleware):
    """Observe tool interrupts and warn about potential repeated effects."""

    def __init__(self, *, warn: bool = True) -> None:
        super().__init__()
        self.warn = warn
        self.findings: dict[str, Finding] = {}

    def _record(self, name: str, tool: Any) -> None:
        if name in self.findings:
            return
        verdict, detail = analyze(tool)
        finding = Finding(tool=name, verdict=verdict, detail=detail)
        self.findings[name] = finding
        if self.warn and verdict != "safe":
            warnings.warn(
                f"{finding} — Effect may repeat on resume. "
                "Separate the effect from the interrupt.",
                MixedEffectWarning,
                stacklevel=4,
            )

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        try:
            return handler(request)
        except GraphInterrupt:
            self._record(request.tool_call["name"], request.tool)
            raise

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ANN201
        try:
            return await handler(request)
        except GraphInterrupt:
            self._record(request.tool_call["name"], request.tool)
            raise

    def report(self) -> str:
        if not self.findings:
            return "No interrupting tools observed."
        order = {"risky": 0, "unknown": 1, "safe": 2}
        rows = sorted(self.findings.values(), key=lambda f: order[f.verdict])
        return "\n".join(str(row) for row in rows)
