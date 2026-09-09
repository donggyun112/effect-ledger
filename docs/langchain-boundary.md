# Putting an execution boundary on tools you already have

*[한국어](langchain-boundary.ko.md)*

This follows Semora's native execution boundary and its store-the-result-first
design. LangChain's Agent, BaseTool/StructuredTool, model loop and the LangGraph
checkpointer are used as they are. Install the middleware and every registered
tool is protected by default. You do not rewrite tool arguments into
`request={...}`.

```python
from langchain.agents import create_agent
from langchain.tools import tool
from effect_ledger import EffectExecutor
from effect_ledger.langchain import CONTROL, READ_ONLY, ExecutionBoundary, current_operation
from effect_ledger.langgraph import LedgerRunner

@tool
def send_confirmation(order_id: str, text: str) -> dict:
    """Send one order confirmation."""
    # provider is your provider SDK. Pass the key only if it really supports one.
    return provider.send(order_id=order_id, text=text,
                         idempotency_key=current_operation().provider_key)

executor = EffectExecutor("effects.sqlite", scope="account-1")
boundary = ExecutionBoundary(
    executor,
    workflow_id="orders:v1",
)
agent = create_agent(
    model, [send_confirmation],
    middleware=[boundary],
    checkpointer=saver,
)
runner = LedgerRunner(agent)
config = {"configurable": {"thread_id": "order-workflow-123"}}
outcome = runner.start({"messages": [("user", "Send the confirmation")]}, config)
```

`model`, `saver` and `provider` are your model, durable checkpointer and provider
connection. `current_operation()` is optional. Existing tools can be left alone;
only a tool that needs the provider key reaches for this accessor. It is valid
only inside a protected execution and adds no argument to the model schema.

## Running it yourself

```bash
uv sync --all-extras
uv run --all-extras python examples/execution_boundary_agent.py --state-dir /tmp/boundary-demo start --lose-response
uv run --all-extras python examples/execution_boundary_agent.py --state-dir /tmp/boundary-demo status
uv run --all-extras python examples/execution_boundary_agent.py --state-dir /tmp/boundary-demo resume
```

[examples/execution_boundary_agent.py](../examples/execution_boundary_agent.py)
runs without an external API key. A deterministic demo model calls one ordinary
`@tool` that writes a confirmation into a local mailbox and then loses its reply.
The claim was committed first, so the resume holds as `indeterminate` instead of
writing a second row. Identify the operation by the `operation_id` and `version`
under `interrupts`.

Once the earlier demo processes have exited and you have confirmed that the
mailbox row is the operation in question, pass the values from that output.

```bash
uv run --all-extras python examples/execution_boundary_agent.py --state-dir /tmp/boundary-demo confirm \
  --operation-id <printed-operation_id> --version <printed-version> \
  --decision-id verified-confirmation-123 --message-id 1 --workers-stopped
uv run --all-extras python examples/execution_boundary_agent.py --state-dir /tmp/boundary-demo resume
```

This returns `completed: true` and the final model response. The mailbox still
holds exactly one row, and its `provider_key` column is the operation's
idempotency key. `confirm` is a local operator command rather than a model tool.

## Tool policy

`tools` is not an allowlist of what to protect. A tool absent from that mapping
still goes through the ledger, under the effect `langchain.tool:<name>`. Name only
the tools that are certainly reads, the control tools with no external effect, and
the effect names you want to survive a deploy.

```python
boundary = ExecutionBoundary(
    executor,
    workflow_id="orders:v1",
    tools={
        "search_orders": READ_ONLY,
        "transfer_to_human": CONTROL,
        "send_confirmation": "confirmation.send:v1",
    },
)
```

`READ_ONLY` and `CONTROL` call the existing handler with no ledger write.
`READ_ONLY` is a correctness claim that nothing outside changes, not a
performance option. Marking a possibly-writing tool `READ_ONLY` to avoid SQLite
contention lets a checkpoint replay repeat its effect.

Handoff and state-control tools that return `Command` must be marked `CONTROL`.
Miss one, or let a durable tool return a non-JSON artifact, and the operation
stops as `indeterminate` after the call really happened. Return types cannot be
classified safely from the outside, so **an integration test that calls every
registered tool once along its real return path is required before deploying.**
An artifact from a tool with an external effect has to become JSON; do not mark
that tool `CONTROL` to make the error go away.

The default effect name contains the tool name. Renaming a protected tool
conflicts on the effect binding when an existing run resumes. **The only recovery
is naming the previous string in `tools`,** which replays the recorded outcome
without calling the effect again.

Raising `workflow_id` is not a recovery. It replaces the whole identity space
with a new operation, so an effect that is already `completed` **runs one more
time.** On a payment or delivery tool that is a duplicate charge or a duplicate
send. Use a new identity space only to perform a deliberately new action.

The generated name carries no `:v1` suffix, because nothing would raise it.

## The boundary and composition order

```text
model and native HITL
  → outer tool middleware: approval / retry / result post-processing
    → ExecutionBoundary: bind ID and original arguments → commit the claim
      → your tool: one effect
    ← commit the ToolMessage result / hold as unresolved
  ← result post-processing
```

Install `ExecutionBoundary` **last among the tool-wrapping middleware.** LangChain
places the first middleware outermost, so the last boundary sits closest to the
tool. An outer retry that calls the handler repeatedly still goes through the
ledger each time. If outer post-processing fails after receiving the result, the
already-stored tool result is replayed. See the
[LangChain middleware contract](https://docs.langchain.com/oss/python/langchain/middleware/custom).

Semora has one outer capability coordinating its own policy and ledger
collaborators. **That placement was deliberately not copied here,** because any
other LangChain tool middleware may retry on its own. A retry middleware placed
inside the boundary can run the tool several times within one claim. External
effects performed by middleware outside the boundary are not protected either.

The native `HumanInTheLoopMiddleware` works alongside this. Ordinary approval
requires an explicit user response, and no claim is acquired before approval. The
recovery signal in `resume()` does not stand in for a human approval or for a
provider retry authorization.

## Results and recovery

The content, artifact and extra metadata of a successful ToolMessage are
preserved as JSON. On replay the message ID is assigned by the new graph message
and the tool_call_id is bound to the current call. Looking up the same business
ID from a different graph invocation never returns a past tool_call_id.

An ordinary exception, and a ToolMessage with `status=error`, both become
`indeterminate`. This deliberately differs from Semora's treatment of a general
error result as a completion. A `TimeoutError` alone does not establish that the
external effect failed. Cancellation and `GraphInterrupt` do not release the
claim. Do not use an interrupt inside a protected tool as an ordinary approval
path; put the approval gate in front of the effect boundary.

When an operator has confirmed the real outcome and records a completion, the
message result format is explicit.

```python
# pending is the ledger status from outcome['__interrupt__'][0].value.
# Only after the previous workers and the requests they sent were reconciled,
# and the provider's real result was confirmed:
executor.resolve(
    pending["operation_id"], expected_version=pending["version"],
    decision_id="verified-confirmation-123", action="complete",
    reason="Provider confirmed this exact operation", workers_stopped=True,
    result=ExecutionBoundary.result("Confirmation sent", artifact={"message_id": "m-123"}),
)
outcome = runner.resume(config)
```

A `RecoveryPolicy` returns the same envelope as its result.
`ExecutionBoundary.result()` builds a message format; it does not confirm a
provider success and does not authorize a recovery. A malformed completion result
stops as `result_error`. A stored completion is immutable, so the decision has to
carry the correct format.

The `effect-ledger` console lists and settles these from a terminal. See
**Operator recovery** in the README.

## Scope of support

- Existing sync and async tools are supported. An async graph needs a durable
  checkpointer such as AsyncSqliteSaver and `runner.astart` / `runner.aresume`.
  Store I/O is moved to a worker thread.
- Every registered tool is protected by default. Only tools declared `READ_ONLY`
  or `CONTROL` bypass the ledger.
- A durable tool must be one external effect with fixed meaning and a result
  expressible as JSON. Multiple effects and internal approval interrupts are not
  supported. An unsupported result produced after execution is held as unresolved.
- The provider account is fixed by `executor.scope`. Put every effect-relevant
  value in the tool arguments that get bound into the ledger, and change the
  effect version when the implementation's meaning changes. Reading a recipient
  or an amount from `runtime.context` or other mutable state means the stored
  arguments no longer fix what a re-execution does.
- `operation_id=lambda runtime: ...` supplies a host business ID. Omitted, it is
  derived from the workflow and the checkpointed parent message and tool call ID.
  Different business operations need different IDs.
- SDK-internal retries sit inside this boundary. Configure them to match the
  provider's idempotency contract.
- Serializing calls on one thread, and the durable checkpointer, are the host's
  responsibility. The scope is the latest checkpoint of a root `create_agent`;
  no support for arbitrary StateGraphs or subgraphs is claimed.
- Do not apply `durable_tool` and ExecutionBoundary to the same tool. When
  another process performs the effect and the ledger lives outside this one — an
  MCP server, a remote HTTP provider — use the
  [durable_tool path](langgraph-recovery.md).

## Verification

```bash
uv run --all-extras python -m unittest discover -s tests -p test_execution_boundary.py -v
uv run --all-extras python -m unittest discover -s tests -p test_langgraph_crash.py -k boundary -v
uv run --all-extras python -m unittest discover -s tests -p test_execution_boundary_example.py -v
```

Against a real `create_agent`, these check the original schema, holding an error
ToolMessage, replaying a completed result and artifact, host IDs, argument
conflicts, an outer retry, a post-processing failure, the native HITL and async.
With a separate process and HTTP provider they also check SIGKILL after the
external commit, SIGKILL after the ledger commit, and a second SIGKILL during a
permitted retry.
