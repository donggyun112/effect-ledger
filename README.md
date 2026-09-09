# effect-ledger

[![CI](https://github.com/donggyun112/effect-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/donggyun112/effect-ledger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/donggyun112/effect-ledger/blob/main/LICENSE)

*[한국어 README](https://github.com/donggyun112/effect-ledger/blob/main/README.ko.md)*

## Start here

Install the adapter that matches your application:

```bash
pip install "effect-ledger[langchain]"           # LangChain or LangGraph
pip install "effect-ledger[langchain,postgres]"  # shared PostgreSQL ledger
pip install "effect-ledger[mcp]"                 # remote effects over MCP
```

Inside a clone, run `uv sync --extra langchain` or `uv sync --all-extras`.
The core package needs Python 3.10+ and has no runtime dependencies.

| Your application | Add this boundary |
|---|---|
| `create_agent(...)` | `ExecutionBoundary` middleware |
| A root `StateGraph` with `messages` state | `LedgerToolNode` in place of `ToolNode` |
| An effect handled by another process | `durable_tool(...)` |

For an existing LangChain agent, add one middleware:

```python
from langchain.agents import create_agent
from effect_ledger import EffectExecutor
from effect_ledger.langchain import ExecutionBoundary
from effect_ledger.langgraph import LedgerRunner

boundary = ExecutionBoundary(
    EffectExecutor("effects.sqlite", scope="account-1"),
    workflow_id="mail-agent",
)
agent = create_agent(
    model, [send_message], middleware=[boundary], checkpointer=saver,
)
runner = LedgerRunner(agent)
```

Every registered tool passes through the ledger. Use the `tools` mapping for
read-only tools, control tools and stable effect names. If the agent has several
tool middleware components, put `ExecutionBoundary` last so it sits closest to
the tool.

Use [LedgerToolNode](#use-your-own-langgraph) when you build the `StateGraph`
yourself.

## Why it exists

An agent charges a card, then the process dies before the provider's reply
arrives. LangGraph resumes from its checkpoint and may call the tool again.

effect-ledger writes an operation record before it calls the tool. If the
provider outcome is lost, the operation stays `unresolved`. A later run finds
that record and stops before another charge. The host can then check the
provider and record a `complete` or `retry` decision.

Provider idempotency and exactly-once delivery remain provider concerns. The
ledger records what may already have happened and blocks an unverified retry.

![A graph of model, ledger and unresolved. The ledger is drawn as a box holding
three steps: commit the claim, send_confirmation, record the outcome. The claim
is committed while nothing has been sent; the confirmation goes out and its
reply is lost, so the run stops on unresolved as indeterminate. On resume the
send step is greyed out and never runs, and the attempt count is unchanged. The
host records the confirmed outcome and the ledger replays it. Two counters, messages
sent and provider attempts, stay at
one](https://raw.githubusercontent.com/donggyun112/effect-ledger/main/docs/recovery-walk.gif)

`EffectExecutor.execute()` commits the claim, calls the tool, and records the
outcome. The claim therefore exists before anything has been sent. If the reply
is lost, the run stops on `unresolved` before a second confirmation is sent.

Resuming without a decision re-enters the boundary, finds the unresolved
operation and stops before the send step. The attempt count stays unchanged.
After the host confirms the provider outcome, the ledger stores and replays that
result. Both counters stay at one.

Every value on that screen was captured from a real run of the composed graph in
[examples/execution_boundary_agent.py](https://github.com/donggyun112/effect-ledger/blob/main/examples/execution_boundary_agent.py),
including the `in_flight` rows, which were sampled from the store while the
attempt was still running. Source:
[docs/demo/recovery-walk.html](https://github.com/donggyun112/effect-ledger/blob/main/docs/demo/recovery-walk.html).

The demo graph composes `EffectExecutor` directly, so the boundary appears as a
node. `ExecutionBoundary` runs inside an agent's existing `tools` node.
`langgraph.json` exposes both versions for `langgraph dev`.

See the [LangChain execution boundary guide](https://github.com/donggyun112/effect-ledger/blob/main/docs/langchain-boundary.md)
for tool policies and middleware ordering. The [composition guide](https://github.com/donggyun112/effect-ledger/blob/main/docs/composition.md)
covers storage, business IDs, recovery policies and multi-host deployment.

## Use your own LangGraph

Replace your `ToolNode` with `LedgerToolNode`. Keep your existing `@tool`
functions and model loop; each call is recorded separately, including multiple
calls to the same tool in one model response.

```python
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from effect_ledger import EffectExecutor, RecoveryDecision
from effect_ledger.langchain import READ_ONLY
from effect_ledger.langgraph import LedgerRunner, LedgerToolNode

# model, tools, saver and provider_for come from your application.
tools = [send_mail, send_slack, create_ticket, search]
model_with_tools = model.bind_tools(tools)

def reconcile(operation):
    provider = provider_for(operation.effect)
    receipt = provider.find_confirmed_receipt(
        operation.provider_key, operation.request,
    )
    if receipt is None:
        return None
    return RecoveryDecision(
        action="complete",
        decision_id=f"{operation.effect}:{receipt.id}",
        reason="Provider confirmed this operation",
        result=LedgerToolNode.result(
            "Completed", artifact={"receipt_id": receipt.id},
        ),
    )

executor = EffectExecutor(
    "effects.sqlite", scope="account-1", recovery=reconcile,
)

builder = StateGraph(MessagesState)
builder.add_node("model", lambda state: {
    "messages": [model_with_tools.invoke(state["messages"])]
})
builder.add_node("tools", LedgerToolNode(
    tools,
    executor=executor,
    workflow_id="order-notifications",
    policies={
        "send_mail": "mail.send:v1",
        "send_slack": "slack.send:v1",
        "create_ticket": "ticket.create:v1",
        "search": READ_ONLY,
    },
))
builder.add_edge(START, "model")
builder.add_conditional_edges("model", tools_condition)
builder.add_edge("tools", "model")

runner = LedgerRunner(builder.compile(checkpointer=saver))
config = {"configurable": {"thread_id": "order-123"}}
outcome = runner.start(
    {"messages": [("user", "Send a confirmation email and notify Slack.")]},
    config,
)
```

The node uses the same execution boundary as the middleware. Unlisted tools are
protected with effect names `langchain.tool:<tool-name>`; `policies` can pin a
stable effect name, for example `{"send_mail": "mail.send:v1"}`. `READ_ONLY`
tools bypass the ledger and can run again on resume. Do not apply the middleware
or `durable_tool` to the same tools a second time.

If mail completes and Slack loses its reply, the graph pauses. Resuming replays
mail's stored `ToolMessage` and finds Slack still unresolved in the ledger. Both
effects keep their original attempt count. Native ToolNode can run sibling calls
concurrently, so a pause is not evidence that every worker or provider request
has stopped. Some pending calls may surface on subsequent resumes;
`executor.unresolved()` lists the unsettled operations in the scope.

### Recovery decisions belong to the host

The ledger owns the state transition, version check, decision idempotency and
stored-result replay. It cannot know whether a mail provider accepted a request,
whether a payment settled or whether an old worker can still continue. Looking
up provider receipts and interpreting them is application business logic.

An automated recovery worker, webhook, operations service or admin tool can run
the business check. It leaves the operation unresolved when the evidence is
inconclusive. With enough evidence, it submits a trusted `complete` or `retry`
decision. The API does not require a human operator.

After the host stops old workers, the recovery worker asks the configured policy
to reconcile the operation. `recover()` reads the current record and version,
applies a returned decision and leaves an inconclusive operation unchanged:

```python
pending = outcome["__interrupt__"][0].value
record = executor.recover(
    pending["operation_id"],
    workers_stopped=True,
)
if record.state == "completed":
    outcome = runner.resume(config)
```

The recovery policy uses `LedgerToolNode.result(...)` because a confirmed result
must contain the ToolMessage envelope. `resume()` only wakes the thread. A
trusted policy may return `action="retry"` after it proves that the previous
attempt cannot take effect; that decision grants one more attempt.

### Operation identity

When the host does not supply its own operation IDs, `workflow_id` namespaces
tool calls from different graphs. The default operation ID combines:

```text
workflow_id + thread_id + checkpointed parent AIMessage ID + tool-call ID
```

Keep `workflow_id` stable across restarts and routine deployments. Changing it
creates a new operation-ID space, so previously completed effects can execute
again. Version the stable effect name in `policies` when a tool's external
meaning changes; do not use routine workflow-ID changes as a migration strategy.

If the application already owns a durable ID for every individual action, pass
an `operation_id` callback instead and omit `workflow_id`:

```python
tools_node = LedgerToolNode(
    tools,
    executor=executor,
    operation_id=lambda runtime: runtime.state["operation_ids"][runtime.tool_call_id],
)
```

The callback must return a different ID for different actions and the same ID
when replaying one action. Never return one shared ID for every tool call.

### Supported graph shape

The supported graph is a root graph with `MessagesState`
(or a `messages` field using `add_messages`), a durable checkpointer and serialized
invocations per thread through `LedgerRunner`. Each protected tool performs one
effect and returns JSON-compatible content/artifacts. Subgraphs, handoffs,
time travel and approval interrupts inside protected tools are outside this
contract. Use `astart`/`aresume` with an async checkpointer for async tools.

### Run the multi-tool recovery example

[examples/ledger_tool_node.py](examples/ledger_tool_node.py) runs without a model
API key. It simulates mail and Slack with a local SQLite delivery table and loses
only Slack's reply. Run these commands one at a time, with a fresh state directory:

```bash
uv sync --extra langchain
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo start --lose-response
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo resume
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo confirm --workers-stopped
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo resume
```

The first two commands report `paused`; the last reports `completed`. Both
delivery counts stay at 1. `confirm` verifies the local receipt against the
operation's provider key, channel and original text. The `--workers-stopped`
flag asserts that previous processes/requests cannot continue; it does not stop
them. Use `status` to inspect the checkpoint and outstanding records.

### Store the ledger in PostgreSQL

Install `effect-ledger[langchain,postgres]` and replace only the executor setup:

```python
import os
from effect_ledger.postgres import PostgresOperationStore

# DATABASE_URL=postgresql://user:password@localhost:5432/my_app
executor = EffectExecutor(
    store=PostgresOperationStore(os.environ["DATABASE_URL"]),
    scope="account-1",
)
```

Create `my_app` beforehand; its name is your choice. The store creates/migrates
`operations`, `decisions` and `schema_version` in the configured `search_path`.
All workers share the same DB, schema and scope. SQLite records are not copied
automatically. LangGraph's durable checkpointer is configured separately; it can
use the same PostgreSQL DB, but checkpoint and ledger writes remain separate
transactions. Changing the ledger backend does not change the tools node.

## Execution contract

The host stores a logical operation ID durably before the call and reuses it
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
should first wait on `get_effect` for completion before escalating to recovery.
Keep the same operation ID even
when a store error prevented a response from arriving. The handler is a
synchronous function; SDK-internal retries and partial success across multiple
effects are the responsibility of the handler or provider adapter.

### No lease or automatic expiry

`in_flight` does not mean a worker is alive. There is no lease and no heartbeat,
so an operation can sit there because the worker is still running, or because it
was killed a week ago. The ledger cannot tell those apart, and neither can you
from the outside.

Nothing expires automatically. Elapsed time never moves an operation out of
`in_flight` because a timer cannot see the provider outcome. The host settles the
operation through `resolve` after it has stopped old workers and reconciled the
provider request.

If a lease is ever added it will be an investigation signal and never a claim:
expiry would tell you where to look, and would still leave `resolve` as the only
way to grant another attempt.

## MCP server example

[examples/mcp_server.py](https://github.com/donggyun112/effect-ledger/blob/main/examples/mcp_server.py) is a local non-idempotent
mailbox that appends messages to a separate SQLite file. It touches no real
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
`isError=false`. The host must inspect `unresolved` and hold downstream work.
Showing the model an error sentence does not by itself complete a fail-closed
path. The server cannot detect a semantic duplicate submitted under a new ID.

Adding `--lose-response` simulates a response lost after the mailbox write. A
repeated call appends nothing and returns `indeterminate`. Real kill-based
verification lives in the tests.

## Recover an unresolved operation

The recovery API is not exposed as an MCP tool. Call it from a trusted host path
after stopping existing workers and checking provider requests already sent.
`workers_stopped=True` records the caller's assertion; it cannot block a remote
effect.

The host owns the business check. It can run in an automated recovery worker,
webhook, operations service or admin tool. The `effect-ledger` console reads and
records decisions but never contacts the provider.

```console
$ effect-ledger --db effects.sqlite --scope account-1 list
STATE          VER ATT  EFFECT                   OPERATION ID
indeterminate    2   1  payment.charge:v1        charge-1

$ effect-ledger --db effects.sqlite --scope account-1 show charge-1
{ "request": { "amount": 4200, "card": "tok_x" }, "state": "indeterminate", "version": 2, ... }

# After checking the provider's records for this request:
$ effect-ledger --db effects.sqlite --scope account-1 resolve charge-1 \
    --complete --result-json '{"charge_id": "ch_77"}' \
    --expected-version 2 --decision-id operator-charge-1 \
    --reason "Stripe shows ch_77; workers drained" --workers-stopped
```

Pass the version that the recovery process investigated. A stale version makes
the decision fail if the operation changed during reconciliation. `--db` also
accepts a `postgresql://` DSN.

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
    reason="Mailbox confirms message 1; previous workers stopped",
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

An `OperationConflict` from `execute()` does not prove that the effect failed.
A request-binding mismatch raises before execution. A changed claim version can
also raise after the external effect succeeds, while its result is being stored.
Look up and adjudicate the same operation; never retry
under a new ID on the strength of the exception alone.

## Store and deployment scope

SQLite acquires the claim atomically with `BEGIN IMMEDIATE` and commits it
before the effect with `synchronous=FULL`. No database lock is held across a
network call. The scope is processes on one host sharing the same local-disk
database. It is not an implementation for network filesystems or multiple hosts.
Losing the database, restoring a stale backup or deleting the ledger breaks the
guarantee. No automatic expiry or deletion is implemented.

The ledger carries a schema version and upgrades in place when opened. A build
refuses a ledger written by a newer release at startup, giving downgrade errors
one clear location.

For multiple hosts, inject `PostgresOperationStore(dsn)` from `[postgres]`.
Hosts using the same database and scope share the claim. It serializes short
per-scope transactions and holds no lock during an external call. Connection
pooling and database failover are not included. The ledger's
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
- After the new process calls again and the host confirms completion, the
  provider effect remains a single occurrence.
- Four independent processes calling concurrently acquire exactly one claim.
- An MCP stdio connection is genuinely restarted to verify result replay,
  conflict, unresolved state and recovery.

CI runs this suite on Python 3.10 through 3.13 against a real PostgreSQL service, and
fails the build if any test reports as skipped.

The design rationale is preserved in order under `probes/`. Each file runs as-is
and imports no package code. Design notes and implementation plans are in
`docs/superpowers/`.
