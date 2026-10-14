# Attribute remapping

Use your own attribute names and map their values into Papaya's canonical trace schema. Attribute remapping is a general normalization feature: it can supply session identifiers, workflow labels, model names, usage fields, and other supported attributes. Trace stitching is one application of that feature, covered in the second part of this guide.

- [1. Attribute remapping](#1-attribute-remapping)
- [2. How Does That Handle Trace Stitching?](#2-how-does-that-handle-trace-stitching)

This guide applies to the Python and TypeScript SDKs. Contract: mapping version `1.0`, target `papaya.normalized-trace.v1`. Requires a Papaya ingest endpoint with this contract enabled. SDK source version 0.1.4 supports this generic metadata carrier; no package upgrade is needed solely to attach the configuration.

## 1. Attribute remapping

### What a mapping does

Put the configuration in trace metadata under the literal key `papaya.remap`. For each rule, `from` names the captured source attribute and `to` names its canonical destination.

| Rule scope | Source is read from | Destination applies to |
| --- | --- | --- |
| `trace` | `/metadata/...` on the physical trace | The trace |
| `span` | `/attributes/...` on each captured span | That span |

Paths use JSON Pointer: `~0` escapes `~`, and `~1` escapes `/`. There are no wildcard paths, expressions, coercions, or stringified-JSON parsing.

Papaya copies values into a working trace before normalization and usage/pricing. The captured source stays unchanged, and every rule reads that original source. One rule's output cannot become another rule's input.

### End-to-end example

Suppose your application calls its session attribute `customer_session` and its span-level provider attribute `model_provider`. This captured trace fragment includes the remapping configuration. SDK trace/span IDs, timestamps, and other envelope fields are omitted to focus on the mapping; it is not a complete ingest request.

```json
{
  "metadata": {
    "customer_session": "session-42",
    "papaya.remap": {
      "version": "1.0",
      "targetSchema": "papaya.normalized-trace.v1",
      "trace": [
        {
          "from": "/metadata/customer_session",
          "to": "/sessionId"
        }
      ],
      "span": [
        {
          "from": "/attributes/model_provider",
          "to": "/modelRef/provider",
          "onMissing": "skip",
          "onConflict": "replace"
        }
      ]
    }
  },
  "spans": [
    {
      "attributes": {
        "model_provider": "openai"
      }
    }
  ]
}
```

The relevant fields in the working trace become:

```json
{
  "sessionId": "session-42",
  "spans": [
    {
      "modelRef": {
        "provider": "openai"
      }
    }
  ]
}
```

The two rules are:

```text
trace.metadata.customer_session → trace.sessionId
span.attributes.model_provider → span.modelRef.provider
```

The original metadata and span attributes remain source evidence. `onMissing: "skip"` allows spans without `model_provider` to continue. `onConflict: "replace"` explicitly allows this rule to correct an existing provider value; choose it only when that replacement is intended.

### Attach the configuration through the SDK

Both SDKs carry the literal metadata key `papaya.remap`. LangChain callback defaults and root invocation metadata are copied into the emitted trace. See the [TypeScript SDK](../../README.md) and [Python SDK](https://github.com/Papaya-AI-Inc/papaya-ai/blob/main/packages/papaya-ai-python/README.md) for run and callback integration.

Capture/redaction policy still applies to source values. Validated mapping configuration is preserved as control structure. Inspect the emitted trace from your installed SDK or callback override to confirm it includes the fields you want to map.

### Supported canonical destinations

General attribute rules allow **10 trace destinations and 11 span destinations**. Each destination may appear once in its scope. Trace stitching adds five event destinations described in Part 2, bringing the contract to **26 destinations total**.

Authenticated tenancy, trace/span IDs, parent relationships, captured trace/span timestamps, statuses, and arbitrary payload fields cannot be remapped.

| Scope | Destination | Type | Meaning |
| --- | --- | --- | --- |
| trace | `/sessionId` | string | Session identifier; exact value, no trimming. |
| trace | `/conversationId` | string | Conversation identifier; not an automatic HITL thread alias. |
| trace | `/userId` | string | Customer user identifier; capture policy still applies. |
| trace | `/organizationId` | string | Customer organization label; cannot change authenticated tenancy. |
| trace | `/workflowKey` | string | Sender workflow identity; must remain stable across the approval pair. |
| trace | `/workflowLabel` | string | Sender display label; does not override classified workflow assignment. |
| trace | `/conversational` | boolean | Whether the trace represents conversational activity. |
| trace | `/group/name` | group-name | One grouping dimension; lower-case name, 1–64 characters. |
| trace | `/group/value` | group-value | One grouping value; 1–512 UTF-8 bytes. |
| trace | `/metadata/thread_id` | identifier | HITL thread identity. All span thread aliases must agree. |
| span | `/modelRef/provider` | string | Observed provider name. |
| span | `/modelRef/requested` | string | Requested model identifier. |
| span | `/modelRef/used` | string | Observed model identifier. |
| span | `/usage/inputTokens` | token-count | Measured input tokens; never a configured token limit. |
| span | `/usage/outputTokens` | token-count | Measured output tokens. |
| span | `/usage/totalTokens` | token-count | Measured total tokens; source counting semantics retained. |
| span | `/usage/cacheReadInputTokens` | token-count | Measured cache-read input tokens. |
| span | `/usage/cacheCreationInputTokens` | token-count | Measured cache-write input tokens. |
| span | `/usage/reasoningTokens` | token-count | Measured reasoning tokens; do not add twice to output tokens. |
| span | `/usage/costUsd` | cost | Observed USD cost only; derived provenance is customer / observed. |
| span | `/tool/name` | string | Tool name on an already existing kind=tool span; creates no execution. |

### Missing values, conflicts, and limits

`onMissing` defaults to `error`. Optional trace/span rules may use `skip`; null is invalid. Equal destination values are no-ops. `onConflict` defaults to `error`; trace/span rules may explicitly choose `replace`.

| Limit | Maximum |
| --- | --- |
| Mapping configuration | 16 KiB |
| Pointer | 512 UTF-8 bytes, 16 tokens |
| Trace rules | 10 |
| Span rules | 11 |
| Rule applications per trace | 50,000, including any event projections |

A failed mapping fails preparation atomically for that trace; valid siblings may proceed. Live Papaya trace ingestion is supported. General archive/import mapping fails explicitly with `mapping_route_unsupported`.

A transport acknowledgment confirms landing. Inspect normalization diagnostics, mapping provenance, and the resulting canonical fields to confirm the rules succeeded.

### Canonical schemas

- [Mapping configuration JSON Schema](mapping-v1.schema.json)
- [Normalized trace destination schema](normalized-trace-v1.schema.json)
- [Canonical read models](canonical-read-models.schema.json) — read-only API shapes, not additional remappable destinations
- [Complete 26-field registry](remappable-fields.v1.json)

Schema identifiers use versioned URNs. JSON Schema validates structure; the server also validates value types, byte/work limits, scope, and event references. These files are downloadable from this GitHub repository. SDK documentation is not hosted on the Papaya website.

## 2. How Does That Handle Trace Stitching?

Human-in-the-loop (HITL) approval can pause an execution and resume it in a later physical trace. The `hitl` portion of `papaya.remap` supplies an application approval identity, or a set of approval events, to Papaya's stitcher. The stitcher then joins matching pause/resume executions into one logical trace.

Mapping supplies the canonical information the stitcher consumes. It does not invent a missing approval boundary or establish a match solely from a shared thread and nearby timestamps.

### Keep approval identity stable

Create a new opaque ID for each approval instance. Send the same ID on that approval's pause and resume. Use a different ID for a later approval, even when LangGraph reuses its native interrupt-site ID.

Keep workspace, agent/app, environment, `workflowKey`, `metadata.thread_id`, and graph namespace (when supplied) stable. Papaya stores your ID as `correlationId` with `correlationType: application`. Native LangGraph interrupt IDs stay separate diagnostic evidence. Matching never guesses from thread and timing alone.

### Choose the capture form

| What the physical trace contains | Configuration |
| --- | --- |
| Exactly one complete captured framework pause or resume | One approval ID with `cardinality: "scalar"` |
| A supplied event object with its own phase/span/time | Explicit fields with `cardinality: "one"` |
| Multiple boundaries, or supplied events replacing missing framework evidence | Event array with `cardinality: "many"` |

### One captured boundary: scalar request ID

**Scalar means one value**, such as `"approval-A"`. Send the same value on that approval's paused and resumed traces. Papaya obtains the phase, span ID, and time from the complete captured framework boundary.

In scalar mode the destination is **fixed and implicit**:

```text
/metadata/request_id → hitlEvents[0].correlationId
```

Papaya sets `correlationType: "application"`. There is no configurable `to` in this form, and adding one fails configuration validation. General trace/span rules and explicit event field rules use `to` as shown elsewhere in this guide.

When each physical trace has exactly one complete captured LangGraph pause or resume, put the request ID and this mapping in metadata on both traces.

```json
{
  "workflowKey": "approval-workflow",
  "metadata": {
    "thread_id": "thread-123",
    "request_id": "approval-A",
    "papaya.remap": {
      "version": "1.0",
      "targetSchema": "papaya.normalized-trace.v1",
      "hitl": {
        "from": "/metadata/request_id",
        "cardinality": "scalar"
      }
    }
  }
}
```

If your key is `approval_request_id`, change `from` to `/metadata/approval_request_id`.

Preserve the standard `GraphInterrupt` / `__interrupt__` pause and root `Command` resume capture. A custom flag or request ID alone does not establish a boundary. Metadata-only capture or an override that removes this evidence can make scalar mode unavailable. Missing/malformed evidence fails with `hitl_boundary_unavailable`; multiple observed boundaries fail with `hitl_scalar_ambiguous`.

### Missing boundaries or sequential approvals: explicit events

One pending approval at a time still allows an execution to resume approval A and then pause approval B. That execution needs two distinct approval IDs. Use explicit events for this case, or when the callback removes standard framework evidence.

Replace these synthetic span IDs and times with the actual Papaya span references and observed times inside their recorded start/end intervals. A LangChain run ID is not automatically a Papaya span ID.

```json
{
  "workflowKey": "approval-workflow",
  "metadata": {
    "thread_id": "thread-123",
    "approval_events": [
      {
        "phase": "resume",
        "approval_request_id": "approval-A",
        "span_id": "actual-resume-span",
        "at": "2026-10-13T17:00:00Z"
      },
      {
        "phase": "pause",
        "approval_request_id": "approval-B",
        "span_id": "actual-pause-span",
        "at": "2026-10-13T17:00:10Z"
      }
    ],
    "papaya.remap": {
      "version": "1.0",
      "targetSchema": "papaya.normalized-trace.v1",
      "hitl": {
        "from": "/metadata/approval_events",
        "cardinality": "many",
        "fields": [
          {
            "from": "/phase",
            "to": "/phase"
          },
          {
            "from": "/approval_request_id",
            "to": "/correlationId"
          },
          {
            "from": "/span_id",
            "to": "/spanId"
          },
          {
            "from": "/at",
            "to": "/occurredAt"
          }
        ]
      }
    }
  }
}
```

Send pause A in the first trace, resume A and pause B in the middle trace, and resume B in the last trace. Expect one logical trace, three source segments, and two approval matches. Explicit events do not need native interrupt IDs.

For a simple pair, send one pause event and one resume event on their respective traces, using the same approval ID and each execution's own span ID and timestamp. `cardinality: "many"` accepts an event array; `"one"` accepts one event object. Required fields are phase, correlation ID, span ID, and occurred time. `/nativeInterruptId` is optional diagnostic evidence.

| Physical trace | Declared events | Approval IDs |
| --- | --- | --- |
| First | Pause | A |
| Middle | Resume, then pause | A, B |
| Last | Resume | B |

### Canonical approval-event fields

The five event destinations complete the 26-field remapping contract. Explicit mappings require the first four; the native framework ID is optional diagnostic evidence. `fields[].from` and `fields[].to` are relative to each source event and its canonical event, respectively.

| Scope | Destination | Type | Meaning |
| --- | --- | --- | --- |
| hitl_event | `/phase` | phase | pause or resume, case-sensitive. |
| hitl_event | `/correlationId` | identifier | Per-approval application identity, stable across its pair and retries. |
| hitl_event | `/spanId` | identifier | Reference to an existing span in this same physical trace. |
| hitl_event | `/occurredAt` | timestamp | Observed UTC instant for this boundary, within its span interval. |
| hitl_event | `/nativeInterruptId` | identifier | Optional original framework ID, retained for diagnostics. |

| Event-specific limit | Maximum |
| --- | --- |
| Event field rules | 5 |
| Explicit events per trace | 64 |
| Selected event data per trace | 64 KiB |
| Approval ID | Nonempty exact string, 256 UTF-8 bytes |

HITL field conflicts cannot be replaced, and required event fields cannot be skipped.

### Capture approval metadata before export

Scalar mode needs no event-writing helper when the existing callback preserves its standard boundary and the ID is available in initial invocation metadata. Initial metadata does not discover an ID created later during the invocation.

For explicit events, append each observed event before trace finalization/export. The callback has no dedicated approval-event helper. Obtain the actual captured Papaya span reference and use an RFC 3339 timestamp inside that span's recorded interval. For resume A followed by pause B, append both records rather than overwriting one scalar ID.

Use opaque IDs without sensitive data. Capture/redaction policy still applies to source values; redacted IDs cannot be matched. Validated mapping configuration is preserved as control structure.

### Verify one approval before expanding

1. Send one pause/resume pair with a fresh approval ID and matching workflow/thread scope.
2. Inspect both emitted traces for the ID, mapping configuration, and standard boundary or explicit event.
3. Verify one logical trace, two source segments, one application approval match, and the measured human-wait separator in the timeline.
4. Test resume A followed by pause B with separate event IDs. Reversed delivery should produce the same three segments and two matches; retries must not duplicate spans or usage.

A transport acknowledgment confirms landing. Check normalization diagnostics and the timeline to confirm stitching. Missing counterparts remain unresolved. Conflicting IDs and concurrent boundaries are not guessed. Historical captures lacking the ID or boundary cannot be repaired by mapping configuration alone.

### Stitching configuration examples

- [Scalar approval-ID mapping](examples/oculon-scalar-request-id-mapping.json)
- [Explicit-event mapping](examples/oculon-request-id-mapping.json)
