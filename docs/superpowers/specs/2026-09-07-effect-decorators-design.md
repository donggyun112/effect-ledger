# Default-on LangChain boundary and graph effects

Make one LangChain middleware protect every registered tool by default. Keep direct
StateGraph effects explicit because AgentMiddleware cannot observe arbitrary node code.
The executor and store contracts remain unchanged.

## LangChain API

```python
boundary = ExecutionBoundary(
    executor,
    workflow_id="mail-agent:v1",
    tools={
        "search_docs": READ_ONLY,
        "transfer_to_agent": CONTROL,
        "send_message": "message.send:v2",
    },
)
```

Every tool absent from `tools` is a durable effect. Its default effect name is
`langchain.tool:<tool-name>` with no version suffix. A string value supplies a stable
effect name. `READ_ONLY` and `CONTROL` are non-string sentinels and bypass ledger writes.
One mapping prevents contradictory policy and effect declarations.

`effects` is replaced before the first public release. Decorators are not part of this API:
the original callable stays reusable, and the boundary owns execution policy.

Unsupported `Command` results and non-JSON ToolMessage artifacts remain indeterminate when
their tool uses the durable default. This fail-closed behavior cannot be inferred from a
return annotation. Deployments must execute every registered tool's real result path in an
integration test. A control tool must be marked `CONTROL`; an effect tool with a non-JSON
artifact must adapt that artifact to JSON rather than bypass durability.

`READ_ONLY` is a correctness assertion, not a performance switch. Documentation must warn
that marking an effect read-only can duplicate it after replay. It also avoids the two
SQLite writes and scope-level write contention incurred by durable tools.

## Stable names and replay

The default effect name contains the LangChain tool name. Renaming a tool while an existing
operation can replay changes that binding. The boundary must pause before dispatch and expose
a diagnostic naming the stored and requested effects. The diagnostic tells the operator to
map the renamed tool to its previous explicit effect name or start a new workflow identity.
Documentation states that protected tool renames require a stable string override or a
`workflow_id` bump. No automatic suffix implies a version that code cannot advance.

## Direct StateGraph nodes

LangChain middleware covers ToolNode calls only. A later graph adapter composes with the
same executor at `add_node` time without modifying the original function. Each boundary
protects one independently recoverable effect and requires a host-owned operation ID and a
JSON request containing every effect-relevant value. Nodes containing multiple effects must
split them or use multiple explicit effect calls.

The default-on middleware change ships first because it defines the common policy model.
The direct-node adapter follows as a separate testable change and does not weaken tool
coverage.

## Verification

Use actual `create_agent` executions with SQLite checkpoints. Verify default protection,
read-only bypass, control `Command` passthrough, omitted control failure, JSON artifact
failure, explicit effect replay across a rename, and actionable default-name conflict.
Run the existing sync, async, crash, PostgreSQL, MCP, and packaging regression gates.
