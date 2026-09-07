# The composable execution ledger

*[한국어](composition.ko.md)*

The unit of this library is one explicit external effect. You wire the call site
with `executor.bind(effect, handler)` and choose the store, the business ID, the
recovery policy and the framework binding separately. Community extensions are
added as stores and provider adapters that implement this contract. Nothing here
infers the effect boundary or the ownership of a business ID for you.

For tools you already have, [ExecutionBoundary](langchain-boundary.md) is the
entry point. The bind/store/recovery contract below is the core API, independent
of that boundary.

```text
host business ID ─┐
LangGraph ────────┼─ EffectExecutor ─ OperationStore ─ SQLite / Postgres / your own
MCP server ───────┘       │
                          ├─ bind(effect, handler) ─ external provider
                          └─ recover() ─ RecoveryPolicy ─ check version, store decision
```

## Swapping the store

```python
from effect_ledger import EffectExecutor, SQLiteOperationStore

executor = EffectExecutor(store=SQLiteOperationStore("effects.sqlite"), scope="account-1")
# The shorter form uses the same SQLite implementation.
executor = EffectExecutor("effects.sqlite", scope="account-1")
```

Postgres is an optional dependency. A core install loads neither psycopg nor
LangChain nor MCP.

```python
import os
from effect_ledger import EffectExecutor
from effect_ledger.postgres import PostgresOperationStore

executor = EffectExecutor(
    store=PostgresOperationStore(os.environ["EFFECT_LEDGER_DSN"]),
    scope="account-1",
)
```

Prepare a database or a dedicated search_path for Postgres. The constructor
creates the `operations` and `decisions` tables when they are missing and does
not migrate an existing schema. Grant the database account what it needs, and
keep every worker on the same database, schema and scope. Scope is not an
authentication boundary.

Short database transactions are serialized by a per-scope advisory lock, and the
initial table creation is serialized by a shared one. A connection is opened and
closed per transaction and is never held across an external effect or a recovery
lookup. READ COMMITTED and `synchronous_commit=on` are set explicitly. Database
throughput within one scope is bounded by that lock. Connection pooling,
automatic failover and a distributed graph scheduler are not provided. This uses
the [PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html)
and [psycopg transaction](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
contracts.

## Binding a single effect

```python
# provider.send is whichever provider SDK call your application chose.
def send_to_provider(operation):
    return provider.send(
        **operation.request,
        idempotency_key=operation.provider_key,
    )

send = executor.bind("message.send:v1", send_to_provider)
record = send("order-123:confirmation", {"text": "Order confirmed"})
```

Pass that argument only when the provider genuinely supports the key. Whether a
key is supported, how long it is retained, and what the SDK retries internally
are things the provider adapter has to know. `bind` is a thin entry point; it
does not turn a function that performs several external effects at once into a
safe single effect.

When `record.state == "completed"`, carry on with `record.result`. While it is
unresolved, expose the status from `record.response()` and hold. A store error
or an `OperationConflict` can arrive after the effect, so keep the same business
ID.

## Host business IDs and LangGraph

Core and MCP take an `operation_id` directly. LangGraph's `durable_tool` can also
receive one through a callback.

```python
from effect_ledger.langgraph import durable_tool

send_tool = durable_tool(
    name="send_confirmation", description="Send the order confirmation",
    effect="message.send:v1", execute=transport.execute,
    operation_id=lambda runtime: runtime.config["configurable"]["confirmation_id"],
)
config = {"configurable": {
    "thread_id": "workflow-123",
    "confirmation_id": "order-123:confirmation",
}}
```

Use this shape when the workflow carries a business constraint such as one
confirmation message. Different intents need different IDs. Collapsing several
tools, or several calls to one tool, onto a single business ID either suppresses
an intended effect or raises an argument conflict. Read the ID from the host's
durable business record and pass the same value on every resume. Do not mint a
UUID inside the callback, and do not hand ownership of the ID to model output.

The callback is not exposed in the model schema. A callback failure or an empty
ID holds the graph with an `identity_error` interrupt rather than substituting a
derived ID. Restore the correct host mapping, then resume.

Omit the callback and identity is derived from workflow_id, thread_id, the parent
message ID and the tool_call_id. A host ID separates business identity from the
checkpointer's message IDs. A durable checkpointer is still required to restore
the original arguments and the workflow's progress.

## Recovery policy

A `RecoveryPolicy` is a trusted synchronous callback of `Operation ->
RecoveryDecision | None`. It is never called automatically during execution. A
supervisor stops the previous workers, reconciles requests already sent, and then
calls `executor.recover(id, workers_stopped=True)`. A scheduler may make that
call instead of a person if it can confirm those conditions. The library does not
prove for you that a worker stopped.

```python
from effect_ledger import EffectExecutor, RecoveryDecision

def reconcile(operation):
    # Your provider adapter: return a completion record that corresponds exactly
    # to the original request, or nothing.
    receipt = provider.find_confirmed_receipt(operation.provider_key, operation.request)
    if receipt is None:
        return None  # An empty search result does not authorize a retry.
    return RecoveryDecision(
        action="complete", decision_id=receipt.durable_decision_id,
        reason="Provider confirmed this exact operation", result=receipt.result,
    )

executor = EffectExecutor(store=store, scope="account-1", recovery=reconcile)
record = executor.recover("order-123:confirmation", workers_stopped=True)
```

The default and `None` both hold. A policy exception propagates without changing
authority. `complete` requires an explicit result, and `None` is a valid one.
`retry` permits exactly one more attempt and carries no result. It needs
provider-specific evidence that also rules out the earlier request landing later.
Twenty-four hours elapsed, a timeout, and a failed lookup are not evidence. This
release ships no provider-specific automatic recovery.

A decision is bound to the version that was read. If another supervisor decided
while you were looking, the result is a stale conflict rather than an overwrite.
To replay the same decision, call `resolve` with the full arguments preserved,
including the ID and the original `expected_version`. `recover` re-reads, so
producing a new decision for a later attempt requires separate evidence and a
separate decision ID. Reusing an old ID never adds retry authority.

## The contract for your own store

`OperationStore` is a Protocol, not a base class you must inherit.

| Method | Required atomicity and durability |
|---|---|
| `get(scope, id)` | return the committed Operation, or None |
| `unresolved(scope, limit)` | return operations awaiting a decision; read-only, grants nothing |
| `claim(scope, id, effect, request)` | store the key and original arguments before the effect; `Claim.acquired` is True for exactly one racer; a completed or unresolved re-call returns False |
| `finish(owned, state, result, error)` | record the completion or the unknown outcome only while in_flight and matching `owned.version`; otherwise raise OperationConflict |
| `resolve(scope, id, ...)` | check the version, reject a duplicate decision ID and apply the transition, all in one transaction |

The `result` given to `finish` is either a JSON string the core has already
validated and serialized, or None. The public `Operation.result` is the decoded
JSON value. Comparing the binding for one ID normalizes JSON key order only: the
values and the effect version cannot change. A shared store isolates scopes.
Completed and unresolved records are never expired automatically.

Apply `OperationsContract` from `tests/test_operations.py` and `CrashContract`
from `tests/test_operation_crash.py` to both SQLite and a real PostgreSQL. They
check claim races across independent processes, SIGKILL after the effect landed,
replay of a confirmed completion, decision ID races, and a late completion
against a changed version. This was verified against PostgreSQL in local Docker,
not against a real multi-machine network partition or a database failover.
