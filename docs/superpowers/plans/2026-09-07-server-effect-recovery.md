# Server Effect Recovery Implementation Plan

> **For agentic workers:** Execute inline using the agreed architecture and the executing-plans workflow. The user authorized implementation in this session.

**Goal:** A server-owned, fail-closed effect executor with an MCP interface.

**Architecture:** SQLite commits exclusive attempts and immutable requests before
effects. Recovery uses versioned operator decisions. MCP exposes execution/status
with a server-fixed scope and never exposes recovery authority.

**Tech Stack:** Python >=3.10, sqlite3, unittest, optional mcp>=1.28,<2.

**Spec:** docs/superpowers/specs/2026-09-07-server-effect-recovery.md

## Global constraints

No external accounts or real messages; tests use a local fake HTTP provider.
Preserve existing experimental files. Do not claim exactly-once or infer safety
from timeouts. All JSON arguments must have string keys and finite numbers.

## Task 1: Core (tests/test_operations.py, operations.py)

- [x] Write tests asserting a restarted executor replays a stored result,
  mismatching requests conflict, and exceptions leave an unresolved operation.
- [x] Run `uv run python -m unittest discover -s tests -p test_operations.py -v`;
  confirm the missing API fails, then implement `EffectExecutor(path, scope)`,
  `execute(operation_id, effect, request, handler)`, and `get(operation_id)`.
- [x] Test `resolve(operation_id, expected_version=..., decision_id=...,
  action='retry'|'complete', reason=..., workers_stopped=True, result=...)`:
  stale decisions conflict; repeated decisions never grant another execution.
- [x] Implement SQLite BEGIN IMMEDIATE transactions, conditionally finish the
  owned attempt, and record recovery decisions atomically with transitions.
- [x] Run the core tests and verify no effect runs on conflicts or invalid input.

## Task 2: Real failure boundaries (tests/test_operation_crash.py)

- [x] Start a local HTTP provider with its own durable effect counter.
- [x] Kill the worker after that counter commits, before returning the response.
- [x] Run a fresh worker with the same operation ID; assert remote counter is 1.
- [x] Resolve with a confirmed result and assert replay returns it.
- [x] Race independent worker processes on the same operation and assert only
  one performs the effect; bound all waits and clean up children on failure.

## Task 3: MCP and packaging (mcp.py, examples/, pyproject.toml, README.md)

- [x] Write a real stdio ClientSession test: tools/call replay, conflict, status,
  and no exposed recovery tool. Watch it fail before writing the adapter.
- [x] Implement `create_server(executor, effects)` with a fixed effect registry,
  structured unresolved results and status. Offload synchronous execution from
  the server event loop without interpreting cancellation as permission to retry.
- [x] Make new core imports independent of LangChain; retain legacy exports
  lazily and place framework dependencies in extras. Install and lock extras.
- [x] Document runnable local example and operator recovery, host obligations,
  state meanings and limits. Preserve old experiments as historical material.
- [x] Run all new tests, legacy scripts, wheel build and independent core import.


## Verification receipt

- New unittest suite: 19 passed (Python 3.13, MCP 1.29.1).
- Built wheel installed alone in isolated Python 3.10: 16 core/crash tests passed.
- Legacy ledger script: 9 checks passed; detector script passed.
- sdist and wheel builds passed.
- Independent read-only review: no confirmed defect within documented limits;
  added late-completion fencing, concurrent retry-grant consumption, and real MCP
  request cancellation tests from the review recommendations.
- Test fixtures and example now explicitly close SQLite connections; the suite
  passes with ResourceWarning promoted to errors.
- Work remains uncommitted on feat/server-effect-recovery; the repository had no
  baseline commit and all original project files were untracked.

This validates a single-host SQLite implementation and local fake providers.
It does not validate remote production services, multi-host deployment, or host
workflow enforcement of unresolved responses.
