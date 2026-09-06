# LangGraph recovery integration

User-authorized goal: complete the workflow after actual process failure, not just
block an individual effect. Build on the server-owned executor already tested.

## Architecture

`durable_tool` is an explicit LangChain tool whose entire body is one effect
protocol plus a recovery interrupt. It derives a stable ID from trusted workflow
namespace, checkpoint thread ID, parent AIMessage ID, and model tool call ID. The effect version
is deliberately not part of the ID: a changed version must conflict server-side.
The original AIMessage/tool arguments are checkpointed before tool dispatch with
durability=sync. No model-visible operation ID or recovery authority is accepted.

`DurableAgentRunner` wraps a root compiled create_agent graph: durable checkpointer
required, start rejects unfinished threads, resume reuses saved graph state and
maps only recovery interrupts automatically. Ordinary HITL requires explicit
answers. A resume signal is never a retry grant. Repeated unresolved resumes must
interrupt again without dispatch. No direct state edits, time travel, nested
graph namespaces or concurrent invocations of the same thread are supported.

The tool accepts a synchronous execute(operation_id, effect, request) callback
returning the server's structured status. Local executor embedding and an optional
MCP stdio client implement it. The MCP server is still authoritative. Network
errors/conflicts/malformed replies pause the graph with the stable ID instead of
returning a ToolMessage that lets the model move on. Sync and async tools are
provided; blocking transports are offloaded on the async path.

## Recovery

Operator reads the interrupt's operation ID, reconciles the provider and stops old
workers, then calls executor.resolve outside model tools. Runner.resume checks the
ledger again. If confirmed complete, return the exact JSON result in ToolMessage;
if retry was authorized, consume it once using the original checkpointed request.
If recovery response or graph resume is lost, repeating the same decision and
resume must not dispatch again.

## Acceptance and limits

Use real SqliteSaver files and fresh agent processes. Local HTTP fake provider has
an independent durable effect counter. Kill after remote commit and after ledger
completion but before graph checkpoint; recover without another effect or model
planning call. Test unknown -> interrupt -> operator complete/retry -> final AI
response; stable IDs across reconstruction; repeated resume; ordinary HITL;
parallel tools (completed sibling replay); async execution; real MCP client/server
recovery. No real accounts, no exactly-once or automatic reconciliation claim.
Different model call IDs for a new business request remain outside semantic dedup.
