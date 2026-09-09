# LedgerToolNode implementation plan

Approved scope: expose the existing execution boundary as a tools node for a
root StateGraph with `messages` state, durable checkpoints and serialized thread
invocations. Keep native tool schemas, ToolMessage results and sync/async calls.

1. Write graph integration tests for multiple tools, partial failure, repeated
   unresolved resumes, completion and authorized retry. Verify the missing API
   fails before implementing it.
2. Implement `effect_ledger.langgraph.LedgerToolNode` on native ToolNode wrapper
   hooks, sharing ExecutionBoundary rather than duplicating execution logic.
   Require stable workflow identity or a host operation ID callback. Test
   read-only policies, artifacts, same-tool multiple calls and async execution.
3. Add an executable, API-key-free multi-tool example with separate start,
   confirm and resume commands and durable SQLite files. Explain PostgreSQL
   injection, automatic identity, result envelopes and supported graph scope in
   README.md and README.ko.md.
4. Run focused tests, the full available suite, lint and package build. Review
   the diff for identity, partial-batch replay and documentation accuracy.

Non-goals: arbitrary subgraph/time-travel guarantees, automatic provider
reconciliation, multi-effect tool transactions and handoff support.
