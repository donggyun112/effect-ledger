"""Operator console for unresolved effects. Never expose this as a model tool.

Reading is safe. Deciding is not: `resolve` is the only path that grants another
attempt or declares an effect complete, and it trusts what the operator asserts.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .models import Operation, OperationConflict
from .operations import EffectExecutor


def _executor(database: str, scope: str) -> EffectExecutor:
    if database.startswith(("postgresql://", "postgres://")):
        from .postgres import PostgresOperationStore
        return EffectExecutor(store=PostgresOperationStore(database), scope=scope)
    return EffectExecutor(database, scope=scope)


def _summary(record: Operation) -> dict[str, Any]:
    return {"operation_id": record.operation_id, "effect": record.effect,
            "state": record.state, "attempt": record.attempt, "version": record.version,
            "error": record.error, "created_at": record.created_at}


def _list(executor: EffectExecutor, args: argparse.Namespace) -> int:
    records = executor.unresolved(limit=args.limit)
    if args.json:
        print(json.dumps([_summary(r) for r in records], indent=2))
    elif not records:
        print("No unresolved operations.")
    else:
        print(f"{'STATE':<14} {'VER':>3} {'ATT':>3}  {'EFFECT':<24} OPERATION ID")
        for r in records:
            print(f"{r.state:<14} {r.version:>3} {r.attempt:>3}  {r.effect:<24} {r.operation_id}")
        print(f"\n{len(records)} unresolved. `show <id>` prints the request; "
              "settle one with `resolve`.")
    return 0


def _show(executor: EffectExecutor, args: argparse.Namespace) -> int:
    record = executor.get(args.operation_id)
    if record is None:
        print(f"Unknown operation: {args.operation_id}", file=sys.stderr)
        return 2
    print(json.dumps({**_summary(record), "request": record.request,
                      "result": record.result}, indent=2, sort_keys=True))
    if record.state in ("in_flight", "indeterminate"):
        # The version is the interlock, so name the one the operator just read.
        print(f"\nUnresolved. Confirm the provider's own records against the request "
              f"above, stop the workers, then pass --expected-version {record.version}.",
              file=sys.stderr)
    return 0


def _resolve(executor: EffectExecutor, args: argparse.Namespace) -> int:
    if not args.workers_stopped:
        print("Refusing: pass --workers-stopped only after confirming that no worker "
              "can still be running this operation.", file=sys.stderr)
        return 2
    if args.action == "complete":
        try:
            result = json.loads(args.result_json)
        except json.JSONDecodeError as exc:
            print(f"--result-json is not valid JSON: {exc}", file=sys.stderr)
            return 2
        extra = {"result": result}
    else:
        extra = {}
    try:
        record = executor.resolve(
            args.operation_id, expected_version=args.expected_version,
            decision_id=args.decision_id, action=args.action, reason=args.reason,
            workers_stopped=True, **extra)
    except OperationConflict as exc:
        current = executor.get(args.operation_id)
        print(f"Rejected: {exc}", file=sys.stderr)
        if current is not None:
            print(f"The operation is now state={current.state!r} version={current.version}. "
                  "Look at it again before deciding; the version you passed described a "
                  "state that no longer exists.", file=sys.stderr)
        return 3
    print(json.dumps(record.response(), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="effect-ledger", description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, metavar="PATH_OR_DSN",
                        help="SQLite file, or a postgresql:// DSN")
    parser.add_argument("--scope", required=True, help="account or tenant scope")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="unresolved operations in this scope")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(run=_list)

    show = commands.add_parser("show", help="one operation with its stored request")
    show.add_argument("operation_id")
    show.set_defaults(run=_show)

    resolve = commands.add_parser(
        "resolve", help="settle one operation (trusted operators only)",
        description="Records a decision you already made by checking the provider. "
                    "This command performs no external call and verifies nothing "
                    "about the provider on your behalf.")
    resolve.add_argument("operation_id")
    action = resolve.add_mutually_exclusive_group(required=True)
    action.add_argument("--complete", dest="action", action="store_const", const="complete",
                        help="the effect is confirmed done; requires --result-json")
    action.add_argument("--retry", dest="action", action="store_const", const="retry",
                        help="permit exactly one more attempt")
    # Deliberately not auto-filled from the store: this version is what makes the
    # decision refer to the state the operator actually looked at.
    resolve.add_argument("--expected-version", type=int, required=True,
                         help="version from `show`, as observed when you decided")
    resolve.add_argument("--decision-id", required=True,
                         help="stable ID; replaying it cannot grant a second attempt")
    resolve.add_argument("--reason", required=True, help="what you checked, and where")
    resolve.add_argument("--result-json", help="stored result for --complete")
    resolve.add_argument("--workers-stopped", action="store_true",
                         help="assert no worker can still be running this operation")
    resolve.set_defaults(run=_resolve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "resolve":
        if args.action == "complete" and args.result_json is None:
            parser.error("--complete requires --result-json")
        if args.action == "retry" and args.result_json is not None:
            parser.error("--retry cannot carry a result")
    try:
        return args.run(_executor(args.db, args.scope), args)
    except (ValueError, LookupError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
