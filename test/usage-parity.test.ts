import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Papaya } from "../src/index.js";
import { PapayaCallbackHandler } from "../src/langchain.js";
import { papayaUsageFromResult as usage, papayaModelFromResult } from "../src/usage.js";

const fixtures = JSON.parse(readFileSync(new URL("../scripts/fixtures/papaya-python-sdk-m4-provider-fixtures.json", import.meta.url), "utf8")) as { providers: Array<{ case: string; result: unknown; expected: Record<string, unknown> }> };
for (const fixture of fixtures.providers) test(`same Petavue M4 fixture as Python: ${fixture.case}`, () => {
  const diagnostics: string[] = [];
  assert.deepEqual(usage(fixture.result, code => diagnostics.push(code)), { ...fixture.expected, pricingSource: "provider" }); assert.deepEqual(diagnostics, []);
});

test("shallow fields win; nested cache/reasoning fill missing fields without adding totals", () => {
  assert.deepEqual(usage({ usage: { input_tokens: 10, output_tokens: 6, total_tokens: 16 }, response_metadata: { usage: { input_tokens: 999, input_tokens_details: { cached_tokens: 4 }, output_tokens_details: { reasoning_tokens: 3 }, cost_usd: .125 } } }), {
    inputTokens: 10, outputTokens: 6, totalTokens: 16, cacheReadInputTokens: 4, reasoningTokens: 3, costUsd: .125, pricingSource: "provider",
  });
  assert.deepEqual(usage({ usage: { input_tokens: 10, output_tokens: 2, cache_read_input_tokens: 4, cache_creation_input_tokens: 3 } }), { inputTokens: 10, outputTokens: 2, totalTokens: 12, cacheReadInputTokens: 4, cacheCreationInputTokens: 3 });
  assert.deepEqual(usage({ usageMetadata: { prompt_token_count: 11, candidates_token_count: 5, total_token_count: 19, cached_content_token_count: 2, thoughts_token_count: 3 } }), { inputTokens: 11, outputTokens: 5, totalTokens: 19, cacheReadInputTokens: 2, reasoningTokens: 3 });
});

test("gateway account charge zero wins; upstream cost and customer content are never substituted", () => {
  for (const key of ["costUsd", "cost_usd", "cost", "total_cost", "totalCost"]) assert.deepEqual(usage({ usage: { [key]: 0 }, response: { usage: { cost_usd: 9.99 } } }), { costUsd: 0, pricingSource: "provider" });
  for (const value of [{ cost_details: { upstream_inference_cost: 9.99 } }, { messages: [{ usage: { costUsd: 99 } }] }, { choices: [{ message: { costUsd: 99 } }] }, { calls: [], usage: { costUsd: 99 } }, { data: [{ usage: { costUsd: 99 } }] }]) assert.equal(usage(value), undefined);
});

test("invalid numeric aliases cannot shadow valid observations, including zero", () => {
  for (const invalid of [-1, NaN, Infinity, -Infinity, true, "3"]) assert.deepEqual(usage({ usage: { inputTokens: invalid, input_tokens: 4, costUsd: invalid }, response: { usage: { output_tokens: 2, cost_usd: .1 } } }), { inputTokens: 4, outputTokens: 2, totalTokens: 6, costUsd: .1, pricingSource: "provider" });
});

test("request/response identities prevent mixing calls; shared objects never double count", () => {
  const diagnostics: string[] = [];
  assert.deepEqual(usage({ id: "one", usage: { input_tokens: 4 }, response: { id: "two", usage: { costUsd: .5 } } }, code => diagnostics.push(code)), { inputTokens: 4, totalTokens: 4 });
  assert.ok(diagnostics.includes("usage_scope_conflict"));
  const shared = { usage: { inputTokens: 4, outputTokens: 2 } };
  assert.deepEqual(usage({ body: shared, response: shared }), { inputTokens: 4, outputTokens: 2, totalTokens: 6 });
  assert.deepEqual(usage({ id: "response-one", usage: { inputTokens: 4 }, response_metadata: { request_id: "http-request", cost_usd: 0 } }), { inputTokens: 4, totalTokens: 4, costUsd: 0, pricingSource: "provider" });
});

test("cycles, graph budgets and unreadable properties produce fixed diagnostics without content", () => {
  const cyclic: Record<string, unknown> = { usage: { inputTokens: 3 } }; cyclic.response = cyclic;
  const codes: string[] = []; assert.deepEqual(usage(cyclic, code => codes.push(code)), { inputTokens: 3, totalTokens: 3 }); assert.ok(codes.includes("usage_traversal_cycle_detected"));
  let deep: unknown = { usage: { costUsd: 99 } }; for (let n = 0; n < 20; n++) deep = { response: deep };
  assert.equal(usage(deep, code => codes.push(code)), undefined); assert.ok(codes.includes("usage_traversal_depth_exceeded"));
  const branch = (depth: number): unknown => depth ? { response: branch(depth - 1), body: branch(depth - 1) } : { usage: { costUsd: 99 } };
  assert.equal(usage(branch(9), code => codes.push(code)), undefined); assert.ok(codes.includes("usage_traversal_nodes_exceeded"));
  const unreadable = Object.defineProperty({}, "usage", { get() { throw new Error("synthetic-private-detail"); } });
  assert.equal(usage(unreadable, code => codes.push(code)), undefined); assert.ok(codes.includes("usage_record_unreadable")); assert.equal(JSON.stringify(codes).includes("synthetic-private-detail"), false);
});

test("native wrapper preserves telemetry-only provider override and requested/returned models", async () => {
  const original = globalThis.fetch; let batch: any; let request: any;
  globalThis.fetch = (async (_url, options) => { batch = JSON.parse(String(options?.body)); return new Response("{}", { status: 202 }); }) as typeof fetch;
  const sdk = Papaya.init({ apiKey: "synthetic-key", endpoint: "https://synthetic.invalid/v1/ingest/traces", capture: "full" });
  try {
    const result = { response: { model_name: "returned-model" }, usage: { cost: 0, prompt_tokens: 2 } };
    const client = sdk.openai({ responses: { create(input: unknown) { request = input; return result; } } }, { provider: "openrouter" });
    const messages = [{ role: "user", content: "synthetic" }];
    assert.equal(client.responses.create({ model: "requested-model", messages, papaya: { workflowKey: "synthetic-gateway" } }), result);
    assert.deepEqual(request, { model: "requested-model", messages }); assert.equal(request.messages, messages);
    await sdk.flush(); const span = batch.traces[0].spans.find((s: any) => s.kind === "llm");
    assert.deepEqual(span.modelRef, { provider: "openrouter", requested: "requested-model", used: "returned-model" });
    assert.equal(span.usage.pricingSource, "provider"); assert.equal(span.usage.costUsd, 0);
    assert.equal(papayaModelFromResult({ response: { modelVersion: "version-used" } }), "version-used");
  } finally { await sdk.close(0); globalThis.fetch = original; }
});

test("LangChain preserves deep usage, provider label and real tool identity", async () => {
  const original = globalThis.fetch; let batch: any;
  globalThis.fetch = (async (_url, options) => { batch = JSON.parse(String(options?.body)); return new Response("{}", { status: 202 }); }) as typeof fetch;
  const sdk = Papaya.init({ apiKey: "synthetic", capture: "full" });
  try {
    const handler = new PapayaCallbackHandler(sdk);
    handler.handleChainStart({}, {}, "root");
    handler.handleChatModelStart({}, [[]], "llm", "root", { model: "requested" }, [], { ls_provider: "openrouter" });
    handler.handleLLMEnd({ generations: [[{ message: { model: "returned", usage_metadata: { input_tokens: 5, output_tokens: 2, input_token_details: { cache_read: 3, cache_creation: 1 }, output_token_details: { reasoning: 1 } } } }]], llmOutput: { token_usage: { prompt_tokens: 5, completion_tokens: 2, cost: 0 } } }, "llm");
    handler.handleToolStart({ name: "actual_tool_name" }, "synthetic", "tool", "root", [], {}, "actual_tool_name", "synthetic-tool-call");
    handler.handleToolEnd("synthetic result", "tool"); handler.handleChainEnd({}, "root");
    await sdk.flush(); const spans = batch.traces[0].spans;
    assert.deepEqual(spans.find((s: any) => s.kind === "llm").usage, { inputTokens: 5, outputTokens: 2, totalTokens: 7, costUsd: 0, pricingSource: "provider", cacheReadInputTokens: 3, cacheCreationInputTokens: 1, reasoningTokens: 1 });
    assert.deepEqual(spans.find((s: any) => s.kind === "llm").modelRef, { provider: "openrouter", requested: "requested", used: "returned" });
    assert.ok(spans.some((s: any) => s.kind === "tool" && s.name === "actual_tool_name"));
  } finally { await sdk.close(0); globalThis.fetch = original; }
});
