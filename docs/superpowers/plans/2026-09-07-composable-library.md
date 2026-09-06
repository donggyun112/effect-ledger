# Composable community library implementation plan

**Goal:** Hosts compose storage, identity, recovery and framework adapters without replacing the execution contract.

**Architecture:** A framework-free executor uses an atomic OperationStore protocol. SQLite and PostgreSQL implement the same transitions. Trusted recovery policies return version-bound decisions; adapters supply identity and transport.

**Constraints:** Python >=3.10; zero required dependencies; optional psycopg, LangChain and MCP extras. Preserve SQLite data and existing constructor. Never infer safe retry from age or an exception. No publication or migration of user databases.

- [x] Extract models and atomic `claim(scope, id, effect, request) -> Claim`, `finish(owned, state, result, error)`, `get(scope, id)`, `resolve(scope, id, ...)` storage contract. Keep SQLite tables compatible. Run existing operation/crash tests and contract tests through injected stores.
- [x] Add trusted `RecoveryPolicy(Operation) -> RecoveryDecision | None`, explicit supervisor `recover(..., workers_stopped=True)`, and `bind(effect, handler)` callable. Test abstention, completion, retry consumption, stale decision and policy exceptions.
- [x] Add optional `PostgresOperationStore(dsn)` with durable transactions and scope-level transaction locks. Execute the same behavioral suite on a temporary real PostgreSQL instance, including independent clients and decision races.
- [x] Add `operation_id(runtime)` callback to durable_tool, retain derived identity default. Test host identity survives resume and is absent from model schema.
- [x] Document composable public APIs, operational responsibilities and backend limits; review, run full suite on Python 3.13 and 3.10, build distribution.

Verification commands: `uv run --all-extras python -W error::ResourceWarning -m unittest discover -s tests -v`; `uv build`. PostgreSQL tests require `EFFECT_LEDGER_TEST_DSN` pointing to an isolated test database; use unique scopes and never truncate shared tables.


Verification receipt (2026-09-07):
- Python 3.13: 67 tests passed, ResourceWarning treated as error.
- Python 3.10 isolated wheel + all extras: 67 tests passed, ResourceWarning treated as error.
- Python 3.10 isolated core-only wheel: 24 tests passed; psycopg/LangChain/MCP not imported.
- Real PostgreSQL 17 Docker server: shared behavioral and process-crash contracts, 18 tests, included in both full suites.
- Independent read-only review: no additional concrete defects; reviewer reran PostgreSQL and composition suites.
- Legacy ledger script: 9 checks passed; detector script passed.
- sdist/wheel built; py.typed included. No release published.
- Host ID callback failure now interrupts without derived-ID fallback; regression test included.
- Remaining scope: no leases/liveness proof, no connection pool, no DB failover/network-partition validation; same LangGraph thread requires host serialization.
