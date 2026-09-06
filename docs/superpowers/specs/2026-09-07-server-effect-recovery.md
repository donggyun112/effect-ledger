# Server-owned effect recovery

Approved direction: implement the architecture discussed in chat, starting with
one non-idempotent effect and an MCP server adapter. Keep the existing experimental
LangChain modules available; they do not supply the new execution guarantee.

## Contract

The host persists a logical operation ID before sending it. A trusted server fixes
the scope (account/tenant), effect name/version and JSON request. SQLite commits
the operation, a provider key and exclusive attempt before calling the handler.
Same ID with a different request/effect conflicts. Completed results replay.
In-flight or indeterminate attempts never run again automatically, even after a
restart or timeout. A handler exception is indeterminate, not proof of no effect.

Recovery is an operator-only Python API, not an MCP tool. It requires an expected
record version, unique decision ID, reason, and explicit confirmation that old
workers cannot continue dispatching. Confirmed completion includes a JSON result;
retry permission moves the operation to ready and is consumed atomically by one
attempt. Repeating a decision cannot grant another retry. Version checks fence
late local completions, but cannot fence requests already sent to a provider.

States: in_flight -> completed | indeterminate; either unresolved state -> ready
or completed via recovery; ready -> in_flight with a new attempt. A surviving
in_flight record means running OR crashed: no automatic death inference.

## Boundaries

Python >=3.10, stdlib core (sqlite3), optional MCP SDK v1 adapter with explicit
upper bound, optional legacy LangChain dependencies. SQLite is for persistent
local disk shared by processes on one host, not a distributed database. No TTL,
automatic provider retry, inferred semantic deduplication, arbitrary tool proxy,
or exactly-once claim. Handlers represent one effect and own provider-specific
idempotency/reconciliation. Host integration must preserve operation IDs and
stop dependent work on unresolved results. Recovery trust/auth belongs to the
embedding application. MCP exposes only execution and status in a fixed scope.

## Acceptance

Tests cover durable result replay, canonical request binding, scopes, invalid
JSON, concurrent execution across independent processes, exception/cancellation,
CAS recovery and duplicate decisions. A local fake HTTP service persists each
non-idempotent request in its own SQLite DB; a worker is killed after the remote
commit, before the response. A fresh worker using the same ID must not resend.
Operator completion must restore a result that a subsequent MCP call can replay.
MCP execution/status are tested over a real stdio client connection.
