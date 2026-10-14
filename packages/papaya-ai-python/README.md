# Papaya AI Python SDK

Python tracing SDK for production AI agents. This package mirrors the native Papaya trace envelope used by `@papaya-ai/tracing` and adds a LangChain/LangGraph callback handler for framework-level trace trees.

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

The same wrapper shape is available as `papaya.openai(...)`,
`papaya.anthropic(...)`, `papaya.claude(...)`, `papaya.gemini(...)`, and
`papaya.bedrock(...)`.

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
