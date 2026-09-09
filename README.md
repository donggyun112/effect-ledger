# effect-ledger

[![CI](https://github.com/donggyun112/effect-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/donggyun112/effect-ledger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/donggyun112/effect-ledger/blob/main/LICENSE)

*[한국어 README](https://github.com/donggyun112/effect-ledger/blob/main/README.ko.md)*

Your agent charged the card. The process died before the provider's reply came
back. The graph resumes from its last checkpoint, calls the tool again, and
charges the card a second time.

That is documented behaviour rather than a bug. A task that started but did not
finish runs again on resume, and keeping the side effect safe is left to you.

**effect-ledger commits a record of the attempt before the effect leaves, so the
second call finds it.** An attempt whose outcome was never recorded stops as
`unresolved` and waits for a person. Nothing is retried on a guess.

It does not make your provider idempotent and it does not give you exactly-once.
It records what may already have gone out, and refuses to guess the rest.

![One run in LangGraph Studio: the tool sends a confirmation and loses the
reply, the graph stops as indeterminate, resuming produces the same interrupt,
and only an operator's confirmed outcome completes it](https://raw.githubusercontent.com/donggyun112/effect-ledger/main/docs/studio-recovery.gif)

One run of
[examples/execution_boundary_agent.py](https://github.com/donggyun112/effect-ledger/blob/main/examples/execution_boundary_agent.py)
in LangGraph Studio. The tool sends the confirmation and loses its reply, so the
attempt stops as `indeterminate`. Resuming the graph produces the same interrupt
rather than a second confirmation, and the mailbox holds one message throughout.

Start with the [LangChain execution boundary](https://github.com/donggyun112/effect-ledger/blob/main/docs/langchain-boundary.md): one
middleware over the tools you already have. Everything else is chosen
separately — the store (SQLite/Postgres), the business ID, the recovery policy —
and the core depends on no framework. [The composition API and extension
contract](https://github.com/donggyun112/effect-ledger/blob/main/docs/composition.md) covers that, up to a multi-host store.

```python
from langchain.agents import create_agent
from effect_ledger import EffectExecutor
from effect_ledger.langchain import ExecutionBoundary
from effect_ledger.langgraph import LedgerRunner

boundary = ExecutionBoundary(
    EffectExecutor("effects.sqlite", scope="account-1"),
    workflow_id="mail-agent:v1",
)
# model, send_message and saver are your existing model, single-effect tool
# and durable checkpointer.
agent = create_agent(model, [send_message], middleware=[boundary], checkpointer=saver)
runner = LedgerRunner(agent)
```

Every registered tool is protected by default. Tool names and argument schemas
are preserved. Read-only and control exceptions, and stable effect names, are
declared through the `tools` mapping described in the
[LangChain execution boundary guide](https://github.com/donggyun112/effect-ledger/blob/main/docs/langchain-boundary.md). With several
tool middlewares, install the boundary last.

## Install

```bash
pip install "effect-ledger[langchain]"
pip install "effect-ledger[mcp]"       # to expose effects over MCP
pip install "effect-ledger[postgres]"  # for a multi-host store
```

Working inside a clone of this repository, use `uv sync --extra langchain`
(or `--all-extras`) instead.

The core needs Python 3.10+ and the standard library only. The MCP extra targets
SDK v1 (`mcp>=1.28,<2`). The LangChain execution boundary and the LangGraph
adapters use the `[langchain]` extra.

## Execution contract

The host **stores a logical operation ID durably before the call** and reuses it
on retry. Do not substitute an MCP request ID or a tool call ID that the model
regenerates each turn. The server fixes the account/tenant scope and the effect
name and version.

```text
host: store operation ID
  → server: bind effect, original JSON and provider key to scope + operation ID
  → store: commit the acquired claim and the start record
  → handler: perform one external effect
  → store: record the result
  → host: receive the result, or hold the operation as unresolved
```

Sending a different effect or request under the same scope and ID is a conflict.
JSON object key order does not matter, but changing a value makes it a new
request. Integer and float representations are also distinguished. Only JSON
values are accepted. Handlers use the stored `operation.provider_key` before
execution when the provider supports it. Preserving the key does not extend the
provider's own idempotency retention window.

| State | Meaning | `execute` with the same ID |
|---|---|---|
| `in_flight` | running, or the worker may have died | returns the current state |
| `indeterminate` | handler exception or result serialization failure | returns the current state |
| `ready` | a trusted recovery decision permits exactly one more attempt | consumes the grant atomically, then runs |
| `completed` | the result is settled by execution or external confirmation | returns the stored result |

The first two states carry `unresolved=true`. Elapsed time, cancellation and
restarts never clear them automatically. In the response, `next_action` is
`wait` for `in_flight` and `reconcile` for `indeterminate`; `ready` returns
`execute` and `completed` returns `use_result`. A caller that loses the race
should first wait on `get_effect` for completion rather than demanding an
operator decision immediately. Keep the same operation ID even
when a store error prevented a response from arriving. The handler is a
synchronous function; SDK-internal retries and partial success across multiple
effects are the responsibility of the handler or provider adapter.

### There is no lease, and that is the point

`in_flight` does not mean a worker is alive. There is no lease and no heartbeat,
so an operation can sit there because the worker is still running, or because it
was killed a week ago. The ledger cannot tell those apart, and neither can you
from the outside.

**So nothing expires here.** No amount of elapsed time moves an operation out of
`in_flight`, because a claim that expires on a timer is a retry permit handed out
by a clock that never saw the provider. Only a person who stopped the workers and
checked the provider can settle it, through `resolve`.

If a lease is ever added it will be an investigation signal and never a claim:
expiry would tell you where to look, and would still leave `resolve` as the only
way to grant another attempt.

## MCP server example

[examples/mcp_server.py](https://github.com/donggyun112/effect-ledger/blob/main/examples/mcp_server.py) is a **local non-idempotent
mailbox** that appends messages to a separate SQLite file. It touches no real
mail and no external account.

```bash
uv run --extra mcp python examples/mcp_server.py --ledger /tmp/effects.sqlite --mailbox /tmp/mailbox.sqlite
```

A stdio MCP client calls these two tools.

```json
{"name":"execute_effect","arguments":{"operation_id":"message-1","effect":"message.send:v1","request":{"text":"hello"}}}
```

```json
{"name":"get_effect","arguments":{"operation_id":"message-1"}}
```

Registered effects are limited to the server-side registry passed to
`create_server(executor, effects)`. Scope and provider key are never tool
arguments. Provider-specific input validation belongs to the handler.

The response carries the same state in `structuredContent` and in the JSON text.
An unresolved answer is a valid state response and may come with
`isError=false`. **The host must inspect `unresolved` and hold downstream work.**
Showing the model an error sentence does not by itself complete a fail-closed
path. A semantic duplicate — the same business request submitted under a new ID
— cannot be detected by the server.

Adding `--lose-response` simulates a response lost after the mailbox write. A
repeated call appends nothing and returns `indeterminate`. Real kill-based
verification lives in the tests.

## Operator recovery

The recovery API is not exposed as an MCP tool. Call it from a trusted
operational path. Decide only **after stopping existing workers and checking the
state of provider requests already sent.** `workers_stopped=True` is the
caller's assertion, not a mechanism that blocks a remote effect.

The `effect-ledger` console does the reading and the recording. It does not do
the checking — no command here talks to your provider.

```console
$ effect-ledger --db effects.sqlite --scope account-1 list
STATE          VER ATT  EFFECT                   OPERATION ID
indeterminate    2   1  payment.charge:v1        charge-1

$ effect-ledger --db effects.sqlite --scope account-1 show charge-1
{ "request": { "amount": 4200, "card": "tok_x" }, "state": "indeterminate", "version": 2, ... }

# Now go read the provider's own records for that request. Then, and only then:
$ effect-ledger --db effects.sqlite --scope account-1 resolve charge-1 \
    --complete --result-json '{"charge_id": "ch_77"}' \
    --expected-version 2 --decision-id operator-charge-1 \
    --reason "Stripe shows ch_77; workers drained" --workers-stopped
```

`--expected-version` is typed in on purpose. Filling it in from the store would
make the decision refer to the row as it is at that instant, which is not what
the operator looked at; passing it by hand is what makes a decision refuse to
land on a state that changed while you were investigating. `--db` also takes a
`postgresql://` DSN.

```python
from effect_ledger import EffectExecutor

executor = EffectExecutor("/tmp/effects.sqlite", scope="local-mailbox")
record = executor.get("message-1")
if record is None:
    raise LookupError("Unknown operation")

# Run only after actually confirming mailbox message_id=1 and that the
# previous server has stopped.
executor.resolve(
    "message-1", expected_version=record.version,
    decision_id="operator-confirmed-message-1",
    action="complete", result={"message_id": 1},
    reason="Mailbox confirms message 1; previous server stopped",
    workers_stopped=True,
)
```

`action="retry"` takes no result and permits exactly one more attempt. It runs
only when the host calls execute again with the same ID, effect and original
request. `complete` requires a confirmed result, and an explicit `None` is
allowed. The decision ID and its arguments must also be preserved before the
call.

Recovery checks the version and stores the decision and the transition in one
transaction. Replaying an identical decision returns the current state only. The
same decision ID with different contents, or a new decision against a stale
version, is rejected. Decision contents, reason and time remain in the
`decisions` table. A late result cannot overwrite a changed version, but it
cannot cancel an external request that already went out either.

An `OperationConflict` from `execute()` does not mean the effect failed. It is
raised before execution when the request binding differs, but a changed claim
version can also raise it **after the external effect succeeded, at the moment
the result is stored.** Look up and adjudicate the same operation; never retry
under a new ID on the strength of the exception alone.

## Store and deployment scope

SQLite acquires the claim atomically with `BEGIN IMMEDIATE` and commits it
before the effect with `synchronous=FULL`. No database lock is held across a
network call. The scope is processes on one host sharing the same local-disk
database. It is not an implementation for network filesystems or multiple hosts.
Losing the database, restoring a stale backup or deleting the ledger breaks the
guarantee. No automatic expiry or deletion is implemented.

The ledger carries a schema version and is upgraded in place when it is opened.
One written by a newer release is refused at startup rather than misread, so a
downgrade stops there instead of failing on every read, including the operator's
own commands.

For multiple hosts, inject `PostgresOperationStore(dsn)` from `[postgres]`.
Hosts using the same database and scope share the claim. It serializes short
per-scope transactions and holds no lock during an external call. Connection
pooling, schema migration and database failover are not included. The ledger's
distributed claim and LangGraph thread scheduling are separate concerns;
serializing the same thread remains the host's responsibility.

Scope is not a substitute for authentication. The examples are for stdio on a
single trusted host. An HTTP deployment must arrange authentication,
authorization and per-account routing separately.

## Verification

```bash
uv run --all-extras python -m unittest discover -s tests -p 'test_operation*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_mcp_server.py -v
uv run --all-extras python -m unittest discover -s tests -p 'test_langgraph*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_recovery_agent_example.py -v
# With a dedicated PostgreSQL test database, run the full contract and kill tests:
EFFECT_LEDGER_TEST_DSN=postgresql://postgres@localhost/effect_ledger_test \
  uv run --all-extras python -m unittest discover -s tests -v
```

- A separate HTTP provider commits an effect to its own database, and the worker
  process is killed immediately afterwards.
- After the new process calls again and an operator confirms completion, the
  provider effect remains a single occurrence.
- Four independent processes calling concurrently acquire exactly one claim.
- An MCP stdio connection is genuinely restarted to verify result replay,
  conflict, unresolved state and recovery.

CI runs this suite on Python 3.10–3.13 against a real PostgreSQL service, and
fails the build if any test reports as skipped.

The design rationale is preserved in order under `probes/`. Each file runs as-is
and imports no package code. Design notes and implementation plans are in
`docs/superpowers/`.

The early `EffectLedger` middleware and `MixedEffectDetector` have been removed.
The middleware sat outside the tool and could not cut a replay of the tool body,
and the detector was a workaround that warned about that limitation through
static analysis (`probes/probe_i~l`). `ExecutionBoundary` commits the claim
before the effect, so an interrupt inside a tool does not release the claim —
the hazard the detector warned about is gone, and so is the detector. Both
modules remain in git history.
