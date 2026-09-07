# Default-on Tool Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one `ExecutionBoundary` protect every LangChain tool by default with explicit,
unambiguous read-only, control, and stable-effect overrides.

**Architecture:** `ExecutionBoundary` resolves one policy for every `ToolCallRequest`. Durable
tools reuse the existing executor path; `READ_ONLY` and `CONTROL` pass through without ledger
writes. The existing operation identity remains unchanged, while conflict pauses carry an
actionable rename diagnostic.

**Tech Stack:** Python >=3.10, LangChain >=1.4, LangGraph >=1.2.11, unittest, SQLite.

**Spec:** `docs/superpowers/specs/2026-09-07-effect-decorators-design.md`

## Global Constraints

- Every unconfigured registered tool is durable.
- `tools` is the only per-tool configuration mapping.
- Values are `READ_ONLY`, `CONTROL`, or a non-empty explicit effect string.
- Automatic effects use `langchain.tool:<tool-name>` without a version suffix.
- No automatic retry authorization or inference from return annotations.
- Source and diagnostic text is English.

---

### Task 1: Default-on policy resolution

**Files:**
- Modify: `tests/test_execution_boundary.py`
- Modify: `src/langgraph_effect_ledger/langchain.py`

**Interfaces:**
- Produces: `READ_ONLY`, `CONTROL`, and
  `ExecutionBoundary(executor, *, tools=None, workflow_id=None, operation_id=None)`.

- [x] Add failing integration tests proving an omitted tool is durable, both sentinels bypass
  the ledger, invalid values fail at construction, and explicit strings override effect names.
- [x] Run `uv run --all-extras python -m unittest discover -s tests -p test_execution_boundary.py -v`
  and confirm failures are caused by the missing API.
- [x] Implement immutable sentinels, constructor validation, and one sync/async policy resolver.
- [x] Re-run the focused suite and keep existing replay behavior green.

### Task 2: Real control and result compatibility

**Files:**
- Modify: `tests/test_execution_boundary.py`
- Modify: `src/langgraph_effect_ledger/langchain.py`

**Interfaces:**
- Consumes: `CONTROL` from Task 1.
- Produces: passthrough of native `Command` values for configured control tools.

- [x] Add an actual `create_agent` tool returning `Command`; prove omission pauses as
  indeterminate and `CONTROL` completes without an operation record.
- [x] Add an actual non-JSON artifact tool and prove the durable default pauses.
- [x] Run the focused tests and confirm the new cases fail before implementation where needed.
- [x] Add only the diagnostic error types needed to make the pause explain the configuration
  mistake; do not infer policies from annotations or runtime result types.
- [x] Re-run the focused suite.

### Task 3: Rename-safe diagnostics

**Files:**
- Modify: `tests/test_execution_boundary.py`
- Modify: `src/langgraph_effect_ledger/langchain.py`

**Interfaces:**
- Consumes: automatic and explicit effect resolution from Task 1.
- Produces: an effect-conflict pause containing stored effect, requested effect, stable override
  guidance, and `workflow_id` bump guidance.

- [x] Add a failing replay test using a stable host operation ID across a tool rename.
- [x] Prove the default name conflicts before the renamed provider function runs.
- [x] Prove `tools={"new_name": "old-stable-effect"}` replays the completed result.
- [x] Implement conflict enrichment without exposing request payloads or changing core store
  authority.
- [x] Re-run the focused suite.

### Task 4: Migrate examples and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/langchain-boundary.md`
- Modify: `examples/durable_agent.py`
- Modify: `tests/fixtures/langgraph_worker.py`
- Modify: tests containing the legacy explicit allowlist constructor

**Interfaces:**
- Consumes: the final `tools` API and exported sentinels.
- Produces: examples with default-on behavior and explicit deployment-test obligations.

- [x] Replace every legacy allowlist call with default-on or `tools` configuration.
- [x] Document control/non-JSON integration testing, rename migration, and the danger of using
  `READ_ONLY` to avoid database contention.
- [x] Keep remote `durable_tool` documentation as an advanced MCP transport path.
- [x] Run the example and focused LangGraph crash tests.

### Task 5: Regression and package verification

**Files:**
- Modify: `docs/superpowers/plans/2026-09-07-effect-decorators.md` checkboxes only.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: a release-ready source tree with no stale public examples.

- [x] Search public examples and tests and confirm no obsolete constructor calls remain.
- [x] Run the full Python 3.13 suite, including PostgreSQL when the test DSN is available.
- [x] Run the Python 3.10 compatibility suite.
- [x] Run `uv build` and `git diff --check`.
- [x] Review the final diff against every requirement in the spec.
