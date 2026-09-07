# LangGraph Recovery Implementation Plan

**Goal:** Complete the active end-to-end durable recovery goal.
**Spec:** ../specs/2026-09-07-langgraph-recovery.md
**Architecture:** Explicit durable effect tool + guarded graph runner + optional MCP client.
**Tech:** Python >=3.10; LangChain/LangGraph extras; SQLite checkpoints; MCP v1.

## Tasks

- [x] Write integration tests using real create_agent and SqliteSaver for stable
  tool IDs, repeated unresolved resume, operator complete/retry, no model progress
  while unresolved, and blocking new inputs to an unfinished thread.
- [x] Observe missing adapter failure, implement langgraph.py with durable_tool
  and LedgerRunner, and pass the tests. Add ordinary HITL/async cases.
- [x] Add MCP stdio execute client and test the same graph recovery against the
  real example server. Scope remains server-owned and resolve is never a tool.
- [x] Add process failure workers and a durable fake provider. Kill after remote
  commit and after ledger completion; inspect persisted model messages and resume
  fresh processes to completion. Assert provider count and model planning count.
- [x] Document runnable full agent example, operator steps, guarantees and limits.
- [x] Obtain read-only independent code review, fix findings and run the complete
  test suite, minimum Python verification and wheel build. Complete the goal only
  when actual graph + server recovery is proven.


## Verification receipt

- Full suite: 38 unittest cases passed on Python 3.13, with ResourceWarning errors enabled.
- Built wheel installed in an isolated Python 3.10 environment with both extras:
  all 38 cases passed again, including real process and MCP recovery.
- Legacy ledger script: 9 checks passed; detector script passed.
- sdist and wheel builds passed.
- Independent read-only review identified growing transport calls across repeated
  unresolved resumes. Added a failing regression (15 versus 5 calls) and moved
  authority lookup outside the interrupt replay loop; regression now passes.
- Added a failed-then-fixed identity regression: parent AIMessage ID distinguishes
  new intentional model turns even when a provider reuses tool_call_id.
- Verified a second SIGKILL during an operator-authorized retry over MCP; replaying
  the prior decision cannot grant a third send.
- Runnable example's start/status/resume/confirm/resume sequence is tested across
  separate processes and leaves the independently persisted mailbox count at 1.

Supported scope: root create_agent, latest durable checkpoint, fixed workflow and
server namespaces, serialized invocations per thread, local SQLite, explicit
single-effect handlers. No semantic dedup, remote reconciliation, arbitrary
subgraph/time-travel migration or distributed workflow scheduling claim.
