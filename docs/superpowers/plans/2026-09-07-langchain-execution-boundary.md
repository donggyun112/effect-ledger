# LangChain execution boundary

Goal: preserve native LangChain tools and their schemas while installing one explicit effect boundary, inspired by Semora's framework-native integration and result-before-projection ordering.

Historical design: the first `ExecutionBoundary` used an explicit effect allowlist. The current public API is default-on and uses one `tools` mapping for exceptions and stable names; see `docs/langchain-boundary.md`. Install last among tool wrappers, closest to tools: approvals, retries and result projections stay outside. Successful native ToolMessage content/artifact are persisted; ordinary exceptions, error ToolMessages and unsupported Command results remain indeterminate. GraphInterrupt/cancellation never become success. Replay adapts the stored message to the current tool call ID. The existing runner retains checkpoint/start/resume guards.

Constraints: core remains framework-free; preserve durable_tool; no changes to Semora; no new agent loop, leases or transcript engine. Tools containing internal approval interrupts or multiple external effects are outside the automatic recovery contract. Hidden runtime inputs affecting the external request require host binding into the operation request/identity contract.

- [x] Write failing native create_agent tests: existing schema, unresolved pause, same-ID replay, changed args, result-before-outer-projection, unregistered tools, async and HITL.
- [x] Extract shared graph identity/pause helpers; add native async core execution; implement boundary using public middleware APIs and store claim/finish contract.
- [x] Test real process crash before result recording through this new entry point. Verify pending retries and ordinary human approval remain separate.
- [x] Document boundary ordering, effect declaration, result envelope and trusted recovery, then independent review and Python 3.10/3.13 verification.


Verification receipt:
- Python 3.13 full suite: 81 passed, including real PostgreSQL 17, ResourceWarning treated as error.
- Python 3.10 isolated built wheel full suite: 81 passed with the same PostgreSQL checks.
- Native boundary tests: 11; new native-boundary SIGKILL scenarios: 3.
- Legacy ledger and detector scripts passed; wheel and sdist built.
- Independent read-only review reran 11 boundary tests, no additional concrete defects.
- Python 3.10 async tracing lost implicit runnable context at interrupt; fixed by a public RunnableLambda supplied with runtime.config. Initial failure and successful full rerun recorded in session.
- GraphBubbleUp passes through the framework-free executor via a private BaseException carrier; it preserves the original graph signal and leaves the claim blocked.
- No Semora source changes or publication performed.
