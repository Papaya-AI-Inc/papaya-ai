# Papaya AI Python SDK

Python tracing SDK for production AI agents. This package mirrors the native Papaya trace envelope used by `@papaya-ai/tracing` and adds a LangChain/LangGraph callback handler for framework-level trace trees.

## Report the code version

Set `service_version` in `Papaya.init()` to the deployed git commit SHA, or `my-service@<sha>`, and `service_name` to your service name. The SDK sends these as `resource.serviceVersion` and `resource.serviceName`. Papaya verifies the SHA in each connected repository; 7–39-character abbreviated SHAs must resolve there. Release labels can also resolve through matching git tags in each connected repository. Papaya tries the whole label, its final `:`, `@`, or `/` token, then that token without one leading word prefix (for example `prod-v2.6.40` → `v2.6.40`). Push the matching tag; annotated tags are peeled to their commit, and branches are never used for these lookups. Image digests (`sha256:...`) are deployment versions, not git commits. Unresolved versions use the repository's configured version policy with an explicit reason.

For multiple repositories, use wrapper trace metadata or LangChain invocation metadata:

```python
config={"callbacks": [callback], "metadata": {
    "codeRevisions": [{"repo": "example/agent", "commit": "<full 40-character git SHA>"}]
}}
```

Explicit repository revisions take precedence. Report the version that produced the trace, not the version running at upload time.

## Quick Start

### LangChain / LangGraph

```python
import os

from papaya_ai import Papaya
from papaya_ai.integrations.langchain import PapayaCallbackHandler

papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"])
callback = PapayaCallbackHandler(papaya, workflow_key="support_agent")

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Help this customer"}]},
    config={"callbacks": [callback]},
)

papaya.flush()
```

The LangChain dependency is optional:

```sh
pip install "papaya-ai[langchain]"
```

### Provider SDK Wrappers

Use provider wrappers when your app calls SDK clients directly instead of going
through LangChain callbacks.

```python
import os

from papaya_ai import Papaya

papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"])
openai = papaya.openai(OpenAI())

try:
    with papaya.run({"workflowKey": "support_agent", "sessionId": session_id}):
        result = openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": user_message}],
        )
finally:
    papaya.flush()
```

To compare one customer-defined dimension in the Observability Groups tab, attach one name-value pair to each run. Reusing a name creates one selectable group namespace; changing the name creates another. Use stable values and never put secrets or URL query strings in a group.

```python
with papaya.run({
    "workflowKey": "crawler",
    "group": {"name": "url", "value": "northfield.example.gov"},
}):
    # ...model and tool calls...
    pass
```

The same wrapper shape is available as `papaya.openai(...)`,
`papaya.anthropic(...)`, `papaya.claude(...)`, `papaya.gemini(...)`, and
`papaya.bedrock(...)`.

### Compatible providers and model labels

`papaya.openai(client)` labels the captured route as `openai`. When an
OpenAI-compatible client calls another provider or gateway, use the existing
explicit override:

```python
# compatible_client is your already configured provider SDK client.
client = papaya.wrap_client("openrouter", compatible_client)
response = client.chat.completions.create(
    model="openai/gpt-4o", messages=messages,
)
```

The captured `modelRef.provider` is `openrouter`, `modelRef.requested` is the
model argument sent to the provider, and `modelRef.used` is the model returned
by the provider (or the requested model when the response omits it). These
labels remain separate when the returned model differs. The override changes
telemetry only: it does not inject a provider label, change the model argument,
or modify the original message objects passed to the provider. Choose the route
label explicitly; the SDK does not infer it from an endpoint URL.

## Export lifecycle and delivery outcomes

Capture stays in the calling thread; uploads and scheduled retries use a dedicated
exporter thread. Use `aflush()` and `aclose()` in async applications so waiting for
delivery does not block the event loop or consume its default executor:

```python
async def traced_call(openai, messages):
    with papaya.run({"workflowKey": "support_agent"}):
        response = await openai.chat.completions.create(
            model="gpt-4.1-mini", messages=messages,
        )
    outcome = await papaya.aflush(timeout=2.0)
    if outcome["status"] != "sent":
        print("Papaya delivery status:", outcome["status"])
    return response

# At application shutdown; deadline is seconds from this call.
shutdown = await papaya.aclose(deadline=5.0)
```

Synchronous applications can use `flush(timeout=2.0)` and `close(deadline=5.0)`.
These methods wait on the caller's thread. A flush waits for one attempt of each
batch in its initial target set, respecting scheduled backoff; it does not promise
that every retry has succeeded. Retried batches retain their exact bytes and IDs,
and a delayed failed batch lets eligible healthy batches proceed.

`export_status()` and flush/close results include cumulative `admitted`, `sent`,
`rejected` and `captureFailed` counters, current `queued`, `retrying`, `sending`
and `terminalFailed` counts/bytes, active unfinished/completed capture, high-water
marks, and bounded diagnostic receipts. Pending-capacity backpressure retains
completed capture and reports it separately; repeated flushes do not count that
retained work as a new rejection. A native receipt with partial acceptance remains
terminal and exposes its reported accepted/rejected counts; it does not mark the
entire batch sent. A close that cannot drain returns explicit undelivered state.

Default limits are 64 pending batches / 64 MiB, 128 unfinished or completed traces
/ 16 MiB, and 8 MiB per captured trace. Pending limits include sending, retrying
and retained terminal bodies. These are serialized-content byte budgets, rather
than a process-RSS guarantee. Configure them with `max_pending_batches`,
`max_pending_bytes`, `max_active_traces`, `max_active_bytes` and `max_trace_bytes`.
Capacity failures reject telemetry explicitly; admitted content is never trimmed
and the oldest trace is never silently discarded. Provider wrappers and run scopes
record capture failures while preserving application execution, results and
exceptions. Manual capture APIs may raise `PapayaCaptureError`.

Retries default to eight attempts or 300 seconds of queued age, with jittered
exponential backoff. `429` honors `Retry-After`; `401`/`403` pause network attempts
until explicit `configure_export(...)`. **A frozen batch receiving `413` remains
terminal with its full body retained.** This replaces the earlier implicit batch
splitting and ID regeneration. Opt into multipart below for larger traces.

## Lossless multipart uploads

Enable multipart explicitly when traces can exceed the normal JSON ingest limit:

```python
papaya = Papaya.init(
    api_key=os.environ["PAPAYA_API_KEY"],
    multipart=True,
    # Optional; supported values are advertised by the server (60–3600 seconds).
    multipart_idle_ttl_seconds=3600,
)
```

With this option the exporter sends every frozen batch through the authenticated
multipart API, including small batches. It first reads the endpoint's advertised
capability. An endpoint with multipart disabled or missing fails explicitly with
`multipart_unsupported`; the SDK never trims content or silently switches to
another upload route. Current limits are a **32 MiB complete JSON batch**, including
its envelope, 5 MiB parts and at most seven parts. A batch above the advertised
maximum remains retained with `multipart_max_bytes_exceeded`.

Opting in changes the default per-trace capture budget to 32 MiB and the combined
unfinished/completed capture budget to 64 MiB. Explicit `max_trace_bytes` and
`max_active_bytes` values take precedence. The pending frozen-body budget remains
64 MiB. Without this option the existing 8 MiB trace / 16 MiB active defaults stay
in effect. Serialized capture limits and the server's complete batch limit are
different: leave room for the JSON envelope when approaching the maximum.

The exporter freezes the full policy-permitted content once, binds a session to
its batch ID, byte length and SHA-256, and uploads parts serially. A lost part or
completion acknowledgment causes a status check on the next attempt. Verified
parts are skipped; an already landed receipt is reused. Identity/hash conflicts,
expired or aborted sessions, invalid receipts and checksum errors fail explicitly
while retaining the frozen body. A receipt acknowledges durable raw landing;
normalization and rollup publication happen later on the server.

Receipts include a bounded `multipart` record with `uploadId`, the submitted
`sha256`, `partCount`, and the landed `rawSha256` / `rawBytes`. Submitted and landed
hashes can differ when server capture policy changes the raw content. No payload
or bearer value appears in these diagnostics.

The native multipart transport applies `request_timeout` separately to each
control, part and completion request, including slow response headers. Waiting
with `aflush` / `aclose` retains the M2 lifecycle semantics. Closing stops new
multipart requests after its deadline, while an in-flight request keeps one owned
I/O helper until it finishes or is aborted. Cancellation and close do not delete
the server session. Incomplete sessions expire according to server policy;
memory-only SDK state does not survive process termination.

The legacy three-argument `transport(endpoint, headers, body)` contract continues
to serve JSON ingest. Multipart uses native HTTP by default. Tests or integrations
requiring custom I/O can provide
`multipart_requester(method, url, headers, body)` instead; it returns the same
`(status, text[, response_headers])` tuple and must bound its own I/O. Supplying
the POST-only legacy `transport` with `multipart=True` is an explicit configuration
error.

`request_timeout` defaults to ten seconds for the native HTTP transport, including
slow headers and response reads. `flush_timeout` defaults to ten seconds. Custom
three-argument transports keep their callable contract and must bound their own
I/O; they can return `(status, text)` or `(status, text, response_headers)`.
Uninterruptible DNS or a custom callback can remain in flight after a waiting
deadline. The SDK reports that ownership without launching duplicate attempts.
A native acknowledgment received after its request deadline is recorded as
`late_ack`; a custom callback returning later updates its actual delivery outcome.
Canceling `aflush` stops only the wait. Closing stops admission, drains until its deadline and then
stops new attempts; it cannot kill an arbitrary callback already running.

Pending and failed bodies are memory-only and do not survive process termination.
Diagnostic history is bounded; terminal content remains subject to the pending
capacity limits. No disk spool or discard policy is enabled implicitly.

## Attribute Remapping and Approval Stitching

**Availability:** requires a Papaya endpoint with attribute-remapping v1 enabled.
See the [SDK guide and canonical schemas](https://github.com/Papaya-AI-Inc/papaya-ai/blob/main/docs/attribute-remapping/README.md).

Both SDKs accept a JSON object in trace metadata under the literal key
`papaya.remap`. For LangChain/LangGraph, handler defaults and root invocation
metadata are copied into the emitted trace. Remapping runs on the Papaya server.

When each physical trace has exactly one captured LangGraph pause or resume,
send your scalar ID and this mapping in the metadata bag on both sides:

```json
{
  "thread_id": "thread-123",
  "request_id": "approval-A",
  "papaya.remap": {
    "version": "1.0",
    "targetSchema": "papaya.normalized-trace.v1",
    "hitl": { "from": "/metadata/request_id", "cardinality": "scalar" }
  }
}
```

Papaya takes phase, span ID and time from the existing framework boundary and
uses the scalar as an application correlation ID. It preserves the native ID
separately. Use a new request ID for each approval. A missing or malformed boundary
fails with `hitl_boundary_unavailable`; multiple boundaries fail with
`hitl_scalar_ambiguous`. Metadata-only capture can remove the evidence this mode
needs. Keep the same workflow and thread scope on both sides.

For traces that resume A and pause B, or whose override removes framework evidence,
send `metadata.approval_events` with separate observed events and the mapping below.
Replace the example span ID and time with the actual Papaya span ID and timestamp.

```json
{
  "thread_id": "thread-123",
  "approval_events": [{
    "phase": "pause",
    "approval_request_id": "approval-A",
    "span_id": "actual-papaya-span-id",
    "at": "2026-10-13T17:00:10Z"
  }],
  "papaya.remap": {
    "version": "1.0",
    "targetSchema": "papaya.normalized-trace.v1",
    "hitl": {
      "from": "/metadata/approval_events",
      "cardinality": "many",
      "fields": [
        { "from": "/phase", "to": "/phase" },
        { "from": "/approval_request_id", "to": "/correlationId" },
        { "from": "/span_id", "to": "/spanId" },
        { "from": "/at", "to": "/occurredAt" }
      ]
    }
  }
}
```

On resume, use `phase: "resume"`, the same approval ID, and that execution's span
ID and timestamp. Keep the workspace, agent/app, environment, `workflowKey`, and
`thread_id` stable. A subsequent approval gets a new ID. A trace that resumes A
and pauses B must carry both event objects. Papaya stores these IDs as application
correlation IDs; it preserves native LangGraph interrupt IDs separately.

**Scalar mode requires captured framework evidence.** Preserve the standard
`GraphInterrupt` / `__interrupt__` pause and `Command` resume capture. Scalar mode
never treats an arbitrary request ID as proof of an approval. For explicit event
mode, record the events before finalization; the callback has no dedicated helper
to create that array or to discover IDs created later in the invocation.

The public manual trace APIs return the captured trace and root span ID. Callback
integration must obtain the correct captured span reference; a LangChain run ID is
not automatically a Papaya span ID. Event times must fall inside that span's
recorded interval. Use opaque per-approval IDs containing no sensitive data.

Repository tests verify nested remapping metadata survives Python and TypeScript
callback serialization in `full`, `redacted`, and `metadata` capture modes. Real
provider-free LangGraph runs also test scalar pause/resume capture and local server
stitching. Server capture policies still apply. A fresh capture from your deployed
callback remains necessary to verify its override and installed version.

## Applied Recommendations

Every Papaya recommendation has an id of the form `agfind-<digits>-<hex>`. When your coding agent
implements a recommendation, it appends that id to a list in your application config. Pass the list
to the Papaya client and Papaya marks the recommendation as implemented.

```python
PAPAYA_APPLIED = [
    "agfind-1756089600000-a1b2c3d4",
    "agfind-1756431200000-9f4e7b21",
]

papaya = Papaya.init(
    api_key=os.environ["PAPAYA_API_KEY"],
    applied_recommendations=PAPAYA_APPLIED,
)
```

The list is append-only on your side. The SDK does the pruning.

- **Papaya owns this channel.** `applied_recommendations` is the only way to put anything into it. It is a plain list of recommendation ids; there is no way to add other keys or values.
- **It carries no customer content, so it is not redacted.** It travels beside your traces, not inside them: it never becomes trace metadata, and never passes through local or server-side redaction. Put nothing but recommendation ids here.
- **Only the newest 25 are sent.** The SDK takes the tail of your list, keeps your order, drops entries that do not look like recommendation ids, and deduplicates keeping the newest position. Your config list can grow forever; the payload stays about the same size.
- **Nothing is sent when there is nothing to send.** If the list is unset, empty, or every entry was dropped, the SDK omits the field entirely.

The window size is exported as `APPLIED_RECOMMENDATION_WINDOW`.
