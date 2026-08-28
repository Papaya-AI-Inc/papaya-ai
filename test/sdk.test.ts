import assert from "node:assert/strict";
import type OpenAI from "openai";

import { Papaya, type PapayaFetchInit } from "../src/index.js";

type CapturedRequest = {
  url: string;
  init?: RequestInit;
  body: Record<string, unknown>;
};

type ExportedTrace = {
  workflowKey?: string;
  sessionId?: string;
  spans?: Array<{
    name?: string;
    status?: string;
    inputPayload?: { value?: unknown; redactionState?: string; byteLength?: number };
    outputPayload?: { value?: unknown };
    modelRef?: { provider?: string; requested?: string; used?: string };
    usage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
    attributes?: { method?: string; sessionId?: string; metadata?: Record<string, unknown> };
  }>;
};

const openAIChatCompletion = {
  id: "completion-1",
  object: "chat.completion",
  created: 1_700_000_000,
  model: "gpt-test-used",
  choices: [{
    index: 0,
    finish_reason: "stop",
    logprobs: null,
    message: { role: "assistant", content: "hello back", refusal: null, annotations: [] },
  }],
  usage: {
    prompt_tokens: 11,
    completion_tokens: 7,
    total_tokens: 18,
  },
} satisfies OpenAI.Chat.Completions.ChatCompletion;

class OpenAIClientHarness {
  public readonly providerRequests: unknown[] = [];
  public readonly chat = {
    completions: {
      create: async (request: unknown) => {
        this.providerRequests.push(request);
        return openAIChatCompletion;
      },
    },
  };
}

const captured: CapturedRequest[] = [];
const originalFetch = globalThis.fetch;

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const body = typeof init?.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : {};
  captured.push({ url: String(input), init, body });
  return new Response(JSON.stringify({ accepted: 1, rejected: 0 }), {
    status: 202,
    headers: { "content-type": "application/json" },
  });
}) as typeof fetch;

try {
  const provider = new OpenAIClientHarness();
  const papaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    project: "checkout",
    environment: "test",
    capture: "redacted",
    metadata: { plan: "standard" },
  });
  const openai = papaya.openai(provider, {
    workflowKey: "checkout",
    metadata: { release: "sha-test", plan: "enterprise" },
  });

  await openai.chat.completions.create({
    model: "gpt-test",
    messages: [{ role: "user", content: "email ada@example.com token Bearer abc.secret.token" }],
    authorization: "Bearer should-not-reach-papaya",
    papaya: {
      sessionId: "session-1",
      userId: "user-1",
      metadata: { claimId: "claim-1", plan: "enterprise-plus" },
    },
  });
  const flushResult = await papaya.flush();

  assert.equal(provider.providerRequests.length, 1);
  const providerRequest = provider.providerRequests[0] as Record<string, unknown>;
  assert.equal("papaya" in providerRequest, false);
  assert.equal(providerRequest.authorization, "Bearer should-not-reach-papaya");

  assert.equal(captured.length, 1);
  assert.deepEqual(flushResult, {
    status: "sent",
    traceCount: 1,
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    httpStatus: 202,
    responseText: JSON.stringify({ accepted: 1, rejected: 0 }),
  });
  assert.equal(captured[0]?.url, "https://papaya.example/api/v1/ingest/traces");
  assert.equal((captured[0]?.init?.headers as Record<string, string>).Authorization, "Bearer papaya-test-token");
  assert.equal((captured[0]?.init?.headers as Record<string, string>)["User-Agent"], "@papaya-ai/tracing/0.1.4");
  assert.deepEqual(captured[0]?.body.sdk, {
    name: "@papaya-ai/tracing",
    version: "0.1.4",
    language: "typescript",
    runtime: `node/${process.version}`,
  });
  const exported = JSON.stringify(captured[0]?.body);
  assert.equal(exported.includes("ada@example.com"), false);
  assert.equal(exported.includes("Bearer abc.secret.token"), false);
  assert.equal(exported.includes("Bearer should-not-reach-papaya"), false);
  assert.equal(exported.includes("[redacted-email]"), true);
  assert.equal(exported.includes("Bearer [redacted-token]"), true);
  assert.equal(exported.includes("[redacted-secret]"), true);
  assert.equal(exported.includes("enterprise-plus"), true);

  const traces = captured[0]?.body.traces as Array<{ workflowKey?: string; sessionId?: string; spans?: Array<{ attributes?: Record<string, unknown>; usage?: Record<string, unknown> }> }>;
  assert.equal(traces.length, 1);
  assert.equal(traces[0]?.workflowKey, "checkout");
  assert.equal(traces[0]?.sessionId, "session-1");
  const llmSpan = traces[0]?.spans?.find((span) => span.attributes?.method === "chat.completions.create");
  assert.ok(llmSpan);
  assert.equal(llmSpan.usage?.inputTokens, 11);
  assert.equal(llmSpan.attributes?.sessionId, "session-1");
  assert.deepEqual(llmSpan.attributes?.metadata, {
    plan: "enterprise-plus",
    release: "sha-test",
    claimId: "claim-1",
  });

  const metadataProvider = new OpenAIClientHarness();
  const metadataPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    capture: "metadata",
  });
  const metadataOpenai = metadataPapaya.openai(metadataProvider);
  await metadataOpenai.chat.completions.create({
    model: "gpt-test",
    messages: [{ role: "user", content: "metadata-only secret" }],
  });
  await metadataPapaya.flush();

  const metadataBody = captured[1]?.body;
  assert.ok(metadataBody);
  const metadataText = JSON.stringify(metadataBody);
  assert.equal(metadataText.includes("metadata-only secret"), false);
  assert.equal(metadataText.includes("\"byteLength\""), true);
  assert.equal(metadataText.includes("\"redactionState\":\"metadata\""), true);

  const missingKeyPapaya = Papaya.init({
    endpoint: "https://papaya.example/api/v1/ingest/traces",
  });
  const missingKeyOpenai = missingKeyPapaya.openai(new OpenAIClientHarness());
  await missingKeyOpenai.chat.completions.create({
    model: "gpt-test",
    messages: [{ role: "user", content: "hello" }],
  });
  const missingKeyFlush = await missingKeyPapaya.flush();
  assert.deepEqual(missingKeyFlush, {
    status: "skipped",
    traceCount: 1,
    reason: "missing_api_key",
  });

  const providerFetchCalls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  const providerResponses: Response[] = [];
  const providerFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    providerFetchCalls.push({ input, init });
    const url = String(input);
    if (url.includes("generativelanguage.googleapis.com")) {
      return new Response("temporary model outage", {
        status: 503,
        headers: { "content-type": "text/plain" },
      });
    }
    const response = new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("data: ok\n\n"));
        controller.close();
      },
    }), {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
    providerResponses.push(response);
    return response;
  }) as typeof fetch;

  const fetchPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    capture: "redacted",
    metadata: { workspaceDefault: true },
  });
  const llmFetch = fetchPapaya.fetch(providerFetch, {
    workflowKey: "custom_agent_loop",
    metadata: { release: "sha-fetch" },
  });

  const streamedResponse = await fetchPapaya.run({
    workflowKey: "customer_support_agent",
    workflowLabel: "Customer support agent",
    sessionId: "fetch-session",
    metadata: { route: "/api/agent", stage: "run" },
  }, async () => {
    const firstAttempt = await llmFetch("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-goog-api-key": "gemini-provider-secret",
      },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: "please help ada@example.com" }] }],
      }),
      papaya: {
        provider: "gemini",
        model: "gemini-3.5-flash",
        spanName: "gemini.chat",
        metadata: { attempt: 1 },
      },
    } satisfies PapayaFetchInit);
    assert.equal(firstAttempt.ok, false);

    return llmFetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: {
        authorization: "Bearer openai-provider-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: "gpt-fetch",
        messages: [{ role: "user", content: "email fetch@example.com token Bearer provider.secret.token" }],
      }),
      papaya: {
        provider: "openai",
        spanName: "openai.chat",
        metadata: { attempt: 2, route: "provider" },
      },
    } satisfies PapayaFetchInit);
  });
  assert.equal(streamedResponse, providerResponses[0]);

  const fetchFlush = await fetchPapaya.flush();
  assert.equal(fetchFlush.status, "sent");
  assert.equal(fetchFlush.traceCount, 1);
  assert.equal(providerFetchCalls.length, 2);
  assert.equal("papaya" in (providerFetchCalls[0]?.init as Record<string, unknown>), false);
  assert.equal("papaya" in (providerFetchCalls[1]?.init as Record<string, unknown>), false);

  const fetchBody = captured.at(-1)?.body;
  assert.ok(fetchBody);
  const fetchExport = JSON.stringify(fetchBody);
  assert.equal(fetchExport.includes("gemini-provider-secret"), false);
  assert.equal(fetchExport.includes("openai-provider-secret"), false);
  assert.equal(fetchExport.includes("provider.secret.token"), false);
  assert.equal(fetchExport.includes("fetch@example.com"), false);
  assert.equal(fetchExport.includes("[redacted-email]"), true);
  assert.equal(fetchExport.includes("text/event-stream"), true);

  const fetchTrace = (fetchBody.traces as ExportedTrace[])[0];
  assert.equal(fetchTrace?.workflowKey, "customer_support_agent");
  assert.equal(fetchTrace?.sessionId, "fetch-session");
  const geminiSpan = fetchTrace?.spans?.find((span) => span.name === "gemini.chat");
  const openaiSpan = fetchTrace?.spans?.find((span) => span.name === "openai.chat");
  assert.ok(geminiSpan);
  assert.ok(openaiSpan);
  assert.equal(geminiSpan.status, "failed");
  assert.equal(geminiSpan.modelRef?.requested, "gemini-3.5-flash");
  assert.equal(openaiSpan.status, "success");
  assert.equal(openaiSpan.modelRef?.requested, "gpt-fetch");
  assert.deepEqual(openaiSpan.attributes?.metadata, {
    workspaceDefault: true,
    release: "sha-fetch",
    route: "provider",
    stage: "run",
    attempt: 2,
  });
  const openaiPayload = openaiSpan.inputPayload?.value as { headerNames?: string[]; body?: { messages?: Array<{ content?: string }> } };
  assert.deepEqual(openaiPayload.headerNames, ["authorization", "content-type"]);
  assert.equal(JSON.stringify(openaiPayload.body).includes("[redacted-email]"), true);

  const implicitPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
  });
  const implicitFetch = implicitPapaya.fetch(providerFetch);
  await implicitFetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    body: JSON.stringify({ model: "claude-fetch", messages: [] }),
    papaya: { metadata: { attempt: 1 } },
  } satisfies PapayaFetchInit);
  const implicitFlush = await implicitPapaya.flush();
  assert.equal(implicitFlush.status, "sent");
  assert.equal(implicitFlush.traceCount, 1);
  const implicitTrace = (captured.at(-1)?.body.traces as ExportedTrace[])[0];
  assert.equal(implicitTrace?.workflowKey, "claude.fetch");
  const implicitSpan = implicitTrace?.spans?.find((span) => span.name === "claude.fetch" && span.attributes?.method === "fetch");
  assert.ok(implicitSpan);
  assert.equal(implicitSpan.modelRef?.requested, "claude-fetch");

  const jsonProviderFetch = (async () =>
    new Response(JSON.stringify({
      id: "resp_1",
      model: "gpt-fetch-json-used",
      output_text: "I am the support assistant.",
      usage: { input_tokens: 5, output_tokens: 6 },
    }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })) as typeof fetch;
  const jsonPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    capture: "redacted",
  });
  const jsonFetch = jsonPapaya.fetch(jsonProviderFetch, {
    workflowKey: "json_fetch_agent",
  });
  const jsonResponse = await jsonFetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      model: "gpt-fetch-json",
      input: [{ role: "user", content: "who are you?" }],
    }),
  });
  assert.deepEqual(await jsonResponse.json(), {
    id: "resp_1",
    model: "gpt-fetch-json-used",
    output_text: "I am the support assistant.",
    usage: { input_tokens: 5, output_tokens: 6 },
  });
  const jsonFlush = await jsonPapaya.flush();
  assert.equal(jsonFlush.status, "sent");
  const jsonTrace = (captured.at(-1)?.body.traces as ExportedTrace[])[0];
  const jsonSpan = jsonTrace?.spans?.find((span) => span.name === "openai.fetch");
  assert.ok(jsonSpan);
  const jsonOutput = jsonSpan.outputPayload?.value as { body?: { output_text?: string }; contentType?: string };
  assert.equal(jsonOutput.contentType, "application/json");
  assert.equal(jsonOutput.body?.output_text, "I am the support assistant.");
  assert.equal(jsonSpan.usage?.inputTokens, 5);
  assert.equal(jsonSpan.usage?.outputTokens, 6);
  assert.equal(jsonSpan.usage?.totalTokens, 11);

  const retryBodies: string[] = [];
  let retryAttempt = 0;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    retryBodies.push(String(init?.body ?? ""));
    retryAttempt += 1;
    return new Response(retryAttempt === 1 ? "temporary" : "accepted", {
      status: retryAttempt === 1 ? 503 : 202,
    });
  }) as typeof fetch;
  const retryPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
  });
  const retryTrace = retryPapaya.startTrace({ workflowKey: "retry-stability" });
  retryPapaya.finishTrace(retryTrace, "success");
  const retryFirst = await retryPapaya.flush();
  const retrySecond = await retryPapaya.flush();
  assert.equal(retryFirst.status, "failed");
  assert.equal(retrySecond.status, "sent");
  assert.equal(retryBodies.length, 2);
  assert.equal(retryBodies[0], retryBodies[1]);
  assert.equal(
    (JSON.parse(retryBodies[0]!) as { batchId?: string }).batchId,
    (JSON.parse(retryBodies[1]!) as { batchId?: string }).batchId,
  );

  const splitBodies: Array<{ batchId: string; traceIds: string[] }> = [];
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}")) as {
      batchId: string;
      traces: Array<{ traceId: string }>;
    };
    splitBodies.push({
      batchId: body.batchId,
      traceIds: body.traces.map((trace) => trace.traceId),
    });
    return new Response(body.traces.length > 1 ? "split" : "accepted", {
      status: body.traces.length > 1 ? 413 : 202,
    });
  }) as typeof fetch;
  const splitPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    maxBatchBytes: 1024 * 1024,
  });
  for (const traceId of ["trace-one", "trace-two", "trace-three"]) {
    const trace = splitPapaya.startTrace({ traceId, workflowKey: "split-test" });
    splitPapaya.finishTrace(trace, "success");
  }
  const splitResult = await splitPapaya.flush();
  assert.equal(splitResult.status, "sent");
  assert.equal(splitResult.traceCount, 3);
  assert.deepEqual(splitBodies.map((batch) => batch.traceIds), [
    ["trace-one", "trace-two", "trace-three"],
    ["trace-one", "trace-two"],
    ["trace-one"],
    ["trace-two"],
    ["trace-three"],
  ]);
  assert.equal(new Set(splitBodies.map((batch) => batch.batchId)).size, splitBodies.length);

  const packedBodies: Array<{ bytes: number; traceIds: string[] }> = [];
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const bodyText = String(init?.body ?? "{}");
    const body = JSON.parse(bodyText) as { traces: Array<{ traceId: string }> };
    packedBodies.push({
      bytes: Buffer.byteLength(bodyText),
      traceIds: body.traces.map((trace) => trace.traceId),
    });
    return new Response("accepted", { status: 202 });
  }) as typeof fetch;
  const maxBatchBytes = 1_200;
  const packedPapaya = Papaya.init({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    maxBatchBytes,
  });
  for (const traceId of ["packed-one", "packed-two", "packed-three"]) {
    const trace = packedPapaya.startTrace(
      { traceId, workflowKey: "exact-byte-pack" },
      { inputValue: { text: "x".repeat(400) } },
    );
    packedPapaya.finishTrace(trace, "success");
  }
  const packedResult = await packedPapaya.flush();
  assert.equal(packedResult.status, "sent");
  assert.ok(packedBodies.length > 1);
  assert.equal(
    packedBodies.every((body) => body.bytes <= maxBatchBytes || body.traceIds.length === 1),
    true,
  );
  assert.deepEqual(packedBodies.flatMap((body) => body.traceIds), [
    "packed-one",
    "packed-two",
    "packed-three",
  ]);

  console.log("papaya-ai SDK tests passed");
} finally {
  globalThis.fetch = originalFetch;
}
