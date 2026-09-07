# durable_tool: the remote transport and MCP path

*[한국어](langgraph-recovery.ko.md)*

> **This is the secondary path.** Most of the time you want the
> [ExecutionBoundary guide](langchain-boundary.md) instead: one middleware over
> the tools you already have, and that is the recommended route.
>
> Reach for `durable_tool` only when that is not enough — when **another process
> performs the effect** and this side only carries
> `execute(operation_id, effect, request)` across. An MCP server or a remote HTTP
> provider, where the ledger lives outside this process. Never apply both to the
> same tool.

The recovery interrupt sits inside an explicit effect tool of `create_agent`. The
server's ledger owns the claim, and the LangGraph checkpointer preserves the
original model request and the workflow's progress.

## Running it yourself

```bash
uv sync --all-extras
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo start --lose-response
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo status
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo resume
```

This runs without an external API key. A deterministic demo model writes `hello`
once into the MCP server's local mailbox, then simulates a lost response. The
final resume also holds, because no decision has been made. Identify the
unresolved operation by the `operation_id` and `version` under `interrupts`.

Once the earlier demo processes have exited and you have confirmed that the
mailbox row is the operation in question, pass the values from that output.

```bash
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo confirm \
  --operation-id <printed-operation_id> --version <printed-version> \
  --decision-id confirmed-message-1 --message-id 1 --workers-stopped
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo resume
```

This returns `completed: true` and the final model response. The mailbox database
still holds exactly one message. `confirm` is a local operator command rather
than a model tool, and it checks the named row against the request. A real
service has to correlate the original operation with the provider's result far
more strictly: another operation that sent the same content is not evidence of
this one.

Run a new scenario in a different state-dir or on a new thread. Deleting the
files and reusing the existing thread breaks the durability contract. The example
keeps three independent databases: checkpoints.sqlite (the graph),
effects.sqlite (the ledger) and mailbox.sqlite (the fake external system).

## Wiring it into an application

`durable_tool` is a StructuredTool that takes a synchronous
`execute(operation_id, effect, request)` callback. The callback returns the shape
of the server's `Operation.response()`. You can embed the executor directly, or
connect to an MCP server through `StdioEffectClient.execute`.

```python
from langchain.agents import create_agent
from effect_ledger.langgraph import LedgerRunner, durable_tool

# model, saver and transport are your model, durable checkpointer and effect server.
send = durable_tool(
    name="send_message", description="Send one message",
    workflow_id="mail-agent:v1", effect="message.send:v1",
    execute=transport.execute,
)
runner = LedgerRunner(create_agent(model, [send], checkpointer=saver))
config = {"configurable": {"thread_id": "business-workflow-123"}}
result = runner.start({"messages": [("user", "send a message")]}, config)
# After the operator's decision is stored on the server:
result = runner.resume(config)
```

Use SqliteSaver for sync, and AsyncSqliteSaver with `await runner.astart(...)` /
`await runner.aresume(...)` for async. An in-memory checkpointer, and a graph
with no checkpointer, are rejected. Whether another checkpointer is genuinely
durable is for the application to guarantee.

## The flow this guarantees

1. `durability="sync"` stores the AIMessage and the tool arguments before the
   effect runs.
2. The business ID comes from the host's `operation_id` callback, or from
   workflow_id, thread_id, the parent AIMessage ID and the tool_call_id. It is
   the same across a restart and different for a deliberate operation on a new
   model turn. The effect version is not part of the ID, so changing the effect
   version of an existing operation makes the server detect a conflict.
3. The server commits the start record, then performs the effect.
4. On an unresolved state, a transport error or a malformed response, the tool
   interrupts. It does not hand an error ToolMessage to the model to reason on.
   A `start` that injects new input into an unresolved thread is also rejected.
5. The operator records, against a specific version, either a confirmed
   completion or a single retry, through the server's `resolve`.
6. `resume` re-enters with the stored arguments. **A resume value is not a retry
   authorization.** It checks the server: still unresolved means it stops again;
   completed means the JSON result is restored as a ToolMessage.
7. The graph proceeds to the model's final response. Resuming a finished graph
   does not start new work.

Storing the decision and resuming the graph are not one transaction. A crash
between them is safe because both sides keep durable records, so replaying the
same decision and the same graph resume are both safe. An ordinary user approval
interrupt requires an explicit `responses={interrupt_id: answer}`; the runner's
automatic signal is limited to re-checking a recovery.

## The failure windows that were verified

These run a real HTTP provider, a real MCP server and a real LangGraph worker.
The provider stores the effect in its own database and the test SIGKILLs the
worker and the MCP child. Both the local executor and the MCP path are covered.

- Right after the external commit, before the response or the ledger completion:
  a fresh agent stops as unresolved.
- After the ledger completion, before the graph received the result: a fresh
  agent replays the result.
- After the operator's completion decision, before the resume: a new process
  receives the result and proceeds to the final response.
- Killed again right after the external commit of a permitted retry: replaying
  the previous decision does not permit a further attempt.

These check the effect count, the number of model planning calls, and the
arguments preserved in the checkpoint. Separately verified: a completed sibling
call of a parallel tool, async, ordinary HITL, repeated resumes, and tool call ID
reuse on a new model turn.

```bash
uv run --all-extras python -m unittest discover -s tests -p 'test_langgraph*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_recovery_agent_example.py -v
```

## Scope

- Only the latest checkpoint of a root `create_agent`. Subgraph namespaces, time
  travel, and recovery that edits the original request through a direct
  `update_state` are not supported.
- Calls on one thread are serialized by the host. The runner is not a distributed
  scheduler and not a distributed lock.
- Calling `graph.invoke` directly bypasses the runner's input guards and its
  durability setting.
- Preserve the workflow namespace, the parent message, the checkpoint, the server
  scope and the ledger together.
- A semantic duplicate — the same business operation re-planned by a new model
  request — cannot be distinguished.
- Model call cost, effects in arbitrary nodes, and a provider SDK's internal
  retries are not protected.
- A provider lookup can be wired in through the host's `RecoveryPolicy`. The
  default is a manual decision, and elapsed time alone never implies that a retry
  is safe. See the [composition API](composition.md).
- MCP stdio opens a server session per call, and each resume checks the server
  once. Interrupt history accumulates across repeated resumes. Poll with
  status/get_effect, and resume when a decision or the external state has changed.

`next_action=wait` in a ledger response means another execution may still
complete, so wait for the state first. It is not proof of liveness. With no lease
in the current implementation, whether a worker has stopped is for the host to
determine. `next_action=reconcile` hands confirmation of the effect to the
operator. In both cases the graph holds its downstream work while unresolved.
