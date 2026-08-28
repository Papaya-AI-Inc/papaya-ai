# Papaya AI SDK

Papaya is a TypeScript tracing SDK for production AI agents. It wraps your existing OpenAI, Anthropic/Claude, Gemini, Bedrock, Vercel AI SDK, or `fetch` calls without proxying them. It captures traces locally, redacts payloads by default, and sends them when you call `flush()`.

## Install

```sh
npm install @papaya-ai/tracing
```

Set a Papaya ingest token for your service:

```sh
PAPAYA_API_KEY=ppy_live_...
```

## One-Prompt Setup

If you want your AI IDE to add tracing for you, paste this prompt:

```text
Add the @papaya-ai/tracing SDK to my existing project so my LLM calls show up as
traces in Papaya. Keep the changes minimal. Do not refactor anything else.
If adding Papaya would replace an existing observability or tracing wrapper,
stop and confirm with me first; otherwise prefer composing Papaya alongside it.
Reference: https://github.com/Papaya-AI-Inc/papaya-ai

1. Install:
   npm i @papaya-ai/tracing

2. Initialize once, in a shared module:

   import { Papaya } from "@papaya-ai/tracing";

   export const papaya = Papaya.init({
     apiKey: process.env.PAPAYA_API_KEY,
     project: "<my-project>",
   });

3. Apply one of these, matching how I call the model:

   A. If I use a provider SDK, wrap the client once and keep calling it as before:

      const openai = papaya.openai(new OpenAI());
      await openai.chat.completions.create({ ... });

      Use the matching wrapper if I use Anthropic/Claude, Bedrock, Gemini, or Vercel AI SDK.

   B. If I call the model over raw HTTP, swap fetch for wrapped fetch:

      const llmFetch = papaya.fetch(globalThis.fetch);
      await llmFetch(url, { method, headers, body, papaya: { provider, model } });

   C. If I use LangChain or LangGraph in TypeScript, use the callback handler:

      import { PapayaCallbackHandler } from "@papaya-ai/tracing/langchain";

      const callback = new PapayaCallbackHandler(papaya, { workflowKey, sessionId, userId });
      await agent.invoke(input, { callbacks: [callback] });

   D. If this is a Python app, use the Python SDK. For LangChain/LangGraph:

      pip install "papaya-ai[langchain]"

      import os
      from papaya_ai import Papaya
      from papaya_ai.integrations.langchain import PapayaCallbackHandler

      papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"], project="<my-project>")
      callback = PapayaCallbackHandler(papaya, workflow_key="<workflow>")
      result = agent.invoke(input, config={"callbacks": [callback]})

      For direct Python provider SDK calls, wrap the client once:

      openai = papaya.openai(OpenAI())

      Use one capture path for the same model call: callback handler for
      LangChain/LangGraph trees, provider wrapper for direct SDK calls.

4. Make sure traces are sent even when my code fails:

   try {
     // ... my model calls ...
   } finally {
     await papaya.flush();
   }

   In a long-running server, flush on an interval and once more on shutdown.

Find where I create my model client and where my request or job ends. Show me the
exact lines to add for init, the wrapper, and the try/finally flush. Leave the
rest of my code unchanged.
```

## Quick Start

```ts
import OpenAI from "openai";
import { Papaya } from "@papaya-ai/tracing";

const papaya = Papaya.init({
  apiKey: process.env.PAPAYA_API_KEY!,
});

const openai = papaya.openai(new OpenAI());

await openai.chat.completions.create({
  model: "gpt-4.1-mini",
  messages,
});

await papaya.flush();
```

## Fetch-Based Agent Loops

Use `papaya.fetch()` when your agent loop calls providers directly. The wrapper calls the provider URL with your original request, strips the Papaya-only `papaya` field before the provider sees it, records header names instead of header values, and preserves streaming responses.

```ts
import { Papaya, type PapayaFetchInit } from "@papaya-ai/tracing";

const papaya = Papaya.init({
  apiKey: process.env.PAPAYA_API_KEY!,
  project: "support-agent",
  environment: "production",
});

const llmFetch = papaya.fetch(globalThis.fetch, {
  workflowKey: "customer_support_agent",
});

await papaya.run({ sessionId, userId }, async () => {
  const response = await llmFetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      authorization: `Bearer ${process.env.OPENAI_API_KEY!}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: "gpt-4.1-mini",
      stream: true,
      messages,
    }),
    papaya: {
      provider: "openai",
      model: "gpt-4.1-mini",
      spanName: "openai.chat",
      metadata: { route: "/api/agent/chat" },
    },
  } satisfies PapayaFetchInit);

  return response;
});

await papaya.flush();
```

Gemini REST calls work the same way:

```ts
await llmFetch("https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent", {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-goog-api-key": process.env.GEMINI_API_KEY!,
  },
  body: JSON.stringify({
    contents: [{ parts: [{ text: "Summarize this customer issue." }] }],
  }),
  papaya: {
    provider: "gemini",
    model: "gemini-flash-latest",
  },
} satisfies PapayaFetchInit);
```

## SDK Wrappers

The package is intentionally provider-SDK-free. The SDK wraps clients by shape,
so you do not need a Papaya-specific provider dependency.

```ts
const openai = papaya.openai(new OpenAI());
const anthropic = papaya.anthropic(new Anthropic());
const claude = papaya.claude(new Anthropic());
const gemini = papaya.gemini(genAI);
const bedrock = papaya.bedrock(bedrockRuntimeClient);
const vercel = papaya.vercel(aiSdkObject);
```

Pass run metadata either when you wrap the client or on a single provider call:

```ts
const openai = papaya.openai(new OpenAI(), {
  workflowKey: "claim_triage",
  metadata: { release: process.env.GIT_SHA },
});

await openai.chat.completions.create({
  model: "gpt-4.1-mini",
  messages,
  papaya: {
    sessionId,
    userId: customerUserId,
    metadata: { claimId },
  },
});
```

The `papaya` field is stripped before the provider SDK or REST endpoint receives the request.

## TypeScript LangChain Callback Handler

For TypeScript LangChain or LangGraph apps, install the optional callback handler
from the `langchain` entrypoint and pass it through the normal runnable config.

```ts
import { Papaya } from "@papaya-ai/tracing";
import { PapayaCallbackHandler } from "@papaya-ai/tracing/langchain";

const papaya = Papaya.init({ apiKey: process.env.PAPAYA_API_KEY, project: "support-agent" });
const callback = new PapayaCallbackHandler(papaya, {
  workflowKey: "support_agent",
  sessionId,
  userId,
});

try {
  const result = await agent.invoke(
    { messages: [{ role: "user", content: userMessage }] },
    { callbacks: [callback] },
  );
} finally {
  await papaya.flush();
}
```

See [`examples/langchain-callback.ts`](examples/langchain-callback.ts) for a
runnable local example with a real OpenAI-backed LangChain chat model.

## Python LangChain Callback Handler

For LangChain or LangGraph apps, use the Python callback handler to capture the
agent tree directly instead of wrapping a provider SDK call. The callback maps
LangChain `run_id` / `parent_run_id` events into native Papaya spans, so chains,
chat model calls, tools, and retrievers stay queryable as structured spans. It
does not ship the conversation tree as a serialized JSON string.

```python
import os

from papaya_ai import Papaya
from papaya_ai.integrations.langchain import PapayaCallbackHandler

papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"], project="support-agent")
callback = PapayaCallbackHandler(
    papaya,
    workflow_key="support_agent",
    session_id=session_id,
    user_id=user_id,
)

try:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config={"callbacks": [callback]},
    )
finally:
    papaya.flush()
```

Use one capture path for the same execution: callback handler for the
LangChain/LangGraph tree, provider wrapper for direct SDK calls. Combining both
around the same model call can duplicate LLM spans unless that is intentional.
See [`examples/python-langchain-callback.py`](examples/python-langchain-callback.py)
for a runnable local example.

## Python Provider SDK Wrappers

The Python package also mirrors the TypeScript provider-wrapper model for direct
SDK calls:

```python
import os

from papaya_ai import Papaya

papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"], project="support-agent")
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

Convenience wrappers are available as `papaya.openai(...)`,
`papaya.anthropic(...)`, `papaya.claude(...)`, `papaya.gemini(...)`, and
`papaya.bedrock(...)`. Use these for direct SDK calls; use the callback handler
for LangChain/LangGraph run trees.

## Workflow Boundaries

Papaya creates an implicit single-call run when no explicit run is active. Use `papaya.run()` when one business workflow spans several model calls, retries, retrievals, tools, guardrails, or provider SDKs.

```ts
await papaya.run({
  workflowKey: "refund_agent",
  workflowLabel: "Refund agent",
  sessionId,
  userId,
}, async () => {
  await openai.responses.create({ model: "gpt-4.1-mini", input });
  await llmFetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "anthropic-version": "2023-06-01",
      "x-api-key": process.env.ANTHROPIC_API_KEY!,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "claude-3-5-sonnet-latest", messages }),
    papaya: { provider: "anthropic" },
  });
});

await papaya.flush();
```

## Capture Modes

Configure capture with `Papaya.init({ capture })`.

- `metadata`: records payload type and byte length only.
- `redacted`: records payloads after local redaction. This is the default.
- `full`: records payloads without local redaction. Use only when your Papaya token policy allows it.

Redaction runs locally before export. The hosted ingest API also enforces the token capture policy before raw trace landing.

## Flush Behavior

The first package slice exports traces when you call `await papaya.flush()`. Provider calls are awaited normally, and Papaya export failures do not change provider results. When `debug: true` is enabled, export failures are logged to `console.warn`.

Typical server usage:

```ts
try {
  await handleAgentRequest();
} finally {
  await papaya.flush();
}
```

## Configuration

```ts
const papaya = Papaya.init({
  apiKey: process.env.PAPAYA_API_KEY,
  endpoint: "https://papaya.fyi/api/v1/ingest/traces",
  project: "support-agent",
  environment: "production",
  serviceName: "agent-api",
  serviceVersion: process.env.GIT_SHA,
  capture: "redacted",
  debug: false,
});
```

`apiKey` defaults to `PAPAYA_API_KEY` and then `PAPAYA_INGEST_TOKEN`. `endpoint` defaults to `https://papaya.fyi/api/v1/ingest/traces`.

## Applied Recommendations

Every Papaya recommendation has an id of the form `agfind-<digits>-<hex>`. When your coding agent implements a recommendation, it appends that id to a list in your application config. Pass the list to `Papaya.init` and Papaya marks the recommendation as implemented.

```ts
const papaya = Papaya.init({
  apiKey: process.env.PAPAYA_API_KEY,
  appliedRecommendations: config.papaya.appliedRecommendations,
});
```

The list is append-only on your side. The SDK does the pruning.

- **Papaya owns this channel.** `appliedRecommendations` is the only way to put anything into it. It is a plain list of recommendation ids; there is no way to add other keys or values.
- **It carries no customer content, so it is not redacted.** It travels beside your traces, not inside them: it never becomes trace metadata, and never passes through local or server-side redaction. Put nothing but recommendation ids here.
- **Only the newest 25 are sent.** The SDK takes the tail of your list, keeps your order, drops entries that do not look like recommendation ids, and deduplicates keeping the newest position. Your config list can grow forever; the payload stays about the same size.
- **Nothing is sent when there is nothing to send.** If the list is unset, empty, or every entry was dropped, the SDK omits the field entirely.

The window size is exported as `APPLIED_RECOMMENDATION_WINDOW`.

## Safety Defaults

- Capture defaults to `redacted`.
- Redaction runs locally before export.
- Provider API keys are not exported by the fetch wrapper; it records header names only.
- SDK errors are swallowed unless `debug: true` is enabled.
- Provider calls are awaited normally; Papaya export happens on `flush()`.
- Papaya-only call metadata is stripped before provider SDK and REST calls.
- `appliedRecommendations` is the only input to the Papaya control bag, and it never becomes trace metadata.
- Streaming fetch responses are returned without reading the response body.
- The hosted ingest API enforces capture policy again before raw landing.

## Development

```sh
npm install
npm run typecheck
npm test
npm pack --dry-run
```

Before publishing, follow `RELEASE.md`.
