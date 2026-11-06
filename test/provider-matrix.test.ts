import assert from "node:assert/strict";
import type Anthropic from "@anthropic-ai/sdk";
import { ConverseCommand, type ConverseCommandOutput } from "@aws-sdk/client-bedrock-runtime";
import { GenerateContentResponse } from "@google/genai";
import { AIMessage, HumanMessage } from "@langchain/core/messages";
import type { ChatGeneration, LLMResult } from "@langchain/core/outputs";
import { Annotation, END, START, StateGraph } from "@langchain/langgraph";
import { generateText } from "ai";
import { MockLanguageModelV4 } from "ai/test";
import type OpenAI from "openai";

import { Papaya } from "../src/index.js";
import { PapayaCallbackHandler } from "../src/langchain.js";

const SUPPORTED_TYPESCRIPT_COMBINATIONS = [
  "openai",
  "anthropic",
  "gemini",
  "bedrock",
  "vercel",
  "langchain",
  "langgraph",
] as const;

type CapturedBatch = {
  traces?: Array<{
    spans?: Array<{
      kind?: string;
      attributes?: { provider?: string };
      outputPayload?: { value?: unknown };
      usage?: {
        inputTokens?: number;
        outputTokens?: number;
        totalTokens?: number;
        cacheReadInputTokens?: number;
        reasoningTokens?: number;
      };
    }>;
  }>;
};

const captured: CapturedBatch[] = [];
const originalFetch = globalThis.fetch;

globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
  const body = typeof init?.body === "string" ? JSON.parse(init.body) as CapturedBatch : {};
  captured.push(body);
  return new Response(JSON.stringify({ accepted: Array.isArray(body.traces) ? body.traces.length : 0, rejected: 0 }), {
    status: 202,
    headers: { "content-type": "application/json" },
  });
}) as typeof fetch;

const newPapaya = (): Papaya => Papaya.init({
  apiKey: "papaya-test-token",
  endpoint: "https://papaya.example/api/v1/ingest/traces",
  capture: "full",
});

const providerSpan = (batch: CapturedBatch) => {
  const span = batch.traces?.[0]?.spans?.find((item) => item.kind === "llm");
  assert.ok(span);
  return span;
};

const assertProviderCapture = (
  batch: CapturedBatch,
  provider: string,
  usage: { inputTokens: number; outputTokens: number; totalTokens: number },
  rawMarker: string,
): void => {
  const span = providerSpan(batch);
  assert.equal(span.attributes?.provider, provider);
  assert.deepEqual({
    inputTokens: span.usage?.inputTokens,
    outputTokens: span.usage?.outputTokens,
    totalTokens: span.usage?.totalTokens,
  }, usage);
  assert.equal(typeof span.outputPayload?.value, "object");
  assert.equal(JSON.stringify(span.outputPayload?.value).includes(rawMarker), true);
};

try {
  const covered = new Set<(typeof SUPPORTED_TYPESCRIPT_COMBINATIONS)[number]>();

  const openAIResponse = {
    id: "chatcmpl-matrix",
    object: "chat.completion",
    created: 1_700_000_000,
    model: "gpt-5.4-mini",
    choices: [{
      index: 0,
      finish_reason: "stop",
      logprobs: null,
      message: {
        role: "assistant",
        content: "openai-runtime-response",
        refusal: null,
        annotations: [],
      },
    }],
    usage: { prompt_tokens: 9, completion_tokens: 4, total_tokens: 13 },
  } satisfies OpenAI.Chat.Completions.ChatCompletion;
  let papaya = newPapaya();
  const openAIClient = {
    chat: { completions: { create: async () => openAIResponse } },
  };
  assert.equal((await papaya.openai(openAIClient).chat.completions.create()).object, "chat.completion");
  assert.equal((await papaya.flush()).status, "sent");
  assertProviderCapture(captured.at(-1)!, "openai", { inputTokens: 9, outputTokens: 4, totalTokens: 13 }, "openai-runtime-response");
  covered.add("openai");

  const anthropicResponse = {
    id: "msg_matrix",
    type: "message",
    role: "assistant",
    model: "claude-sonnet-4-5",
    content: [{ type: "text", text: "anthropic-runtime-response", citations: null }],
    container: null,
    stop_details: null,
    stop_reason: "end_turn",
    stop_sequence: null,
    usage: {
      cache_creation: null,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 2,
      inference_geo: null,
      input_tokens: 10,
      output_tokens: 5,
      output_tokens_details: null,
      server_tool_use: null,
      service_tier: "standard",
    },
  } satisfies Anthropic.Message;
  papaya = newPapaya();
  const anthropicClient = { messages: { create: async () => anthropicResponse } };
  assert.equal((await papaya.anthropic(anthropicClient).messages.create()).type, "message");
  assert.equal((await papaya.flush()).status, "sent");
  assertProviderCapture(captured.at(-1)!, "claude", { inputTokens: 10, outputTokens: 5, totalTokens: 15 }, "anthropic-runtime-response");
  papaya = newPapaya();
  assert.equal((await papaya.claude(anthropicClient).messages.create()).type, "message");
  assert.equal((await papaya.flush()).status, "sent");
  assert.equal(providerSpan(captured.at(-1)!).attributes?.provider, "claude");
  covered.add("anthropic");

  const geminiResponse = Object.assign(new GenerateContentResponse(), {
    responseId: "gemini-matrix",
    modelVersion: "gemini-3.5-flash",
    candidates: [{
      content: { role: "model", parts: [{ text: "gemini-runtime-response" }] },
      finishReason: "STOP" as const,
    }],
    usageMetadata: {
      promptTokenCount: 11,
      candidatesTokenCount: 6,
      totalTokenCount: 17,
      cachedContentTokenCount: 2,
      thoughtsTokenCount: 3,
    },
  });
  assert.ok(geminiResponse instanceof GenerateContentResponse);
  papaya = newPapaya();
  const geminiClient = { models: { generateContent: async () => geminiResponse } };
  assert.equal((await papaya.gemini(geminiClient).models.generateContent()).responseId, "gemini-matrix");
  assert.equal((await papaya.flush()).status, "sent");
  assertProviderCapture(captured.at(-1)!, "gemini", { inputTokens: 11, outputTokens: 6, totalTokens: 17 }, "gemini-runtime-response");
  covered.add("gemini");

  const bedrockResponse = {
    $metadata: { httpStatusCode: 200 },
    output: {
      message: {
        role: "assistant",
        content: [{ text: "bedrock-runtime-response" }],
      },
    },
    stopReason: "end_turn",
    usage: { inputTokens: 12, outputTokens: 7, totalTokens: 19 },
    metrics: { latencyMs: 20 },
  } satisfies ConverseCommandOutput;
  papaya = newPapaya();
  const bedrockClient = { send: async (_command: ConverseCommand) => bedrockResponse };
  const bedrockCommand = new ConverseCommand({
    modelId: "anthropic.claude-3-5-sonnet-20241022-v2:0",
    messages: [{ role: "user", content: [{ text: "hello" }] }],
  });
  assert.equal((await papaya.bedrock(bedrockClient).send(bedrockCommand)).stopReason, "end_turn");
  assert.equal((await papaya.flush()).status, "sent");
  assertProviderCapture(captured.at(-1)!, "bedrock", { inputTokens: 12, outputTokens: 7, totalTokens: 19 }, "bedrock-runtime-response");
  covered.add("bedrock");

  const vercelModel = new MockLanguageModelV4({
    provider: "matrix",
    modelId: "vercel-matrix",
    doGenerate: {
      content: [{ type: "text", text: "vercel-runtime-response" }],
      finishReason: { unified: "stop", raw: "stop" },
      usage: {
        inputTokens: { total: 14, noCache: 14, cacheRead: 0, cacheWrite: 0 },
        outputTokens: { total: 9, text: 9, reasoning: 0 },
      },
      warnings: [],
    },
  });
  papaya = newPapaya();
  const vercelClient = { generateText };
  const vercelResponse = await papaya.vercel(vercelClient).generateText({ model: vercelModel, prompt: "hello" });
  assert.equal(vercelResponse.text, "vercel-runtime-response");
  assert.equal((await papaya.flush()).status, "sent");
  assertProviderCapture(captured.at(-1)!, "vercel", { inputTokens: 14, outputTokens: 9, totalTokens: 23 }, "vercel-runtime-response");
  covered.add("vercel");

  papaya = newPapaya();
  let callback = new PapayaCallbackHandler(papaya, { workflowKey: "langchain_matrix" });
  const langchainMessage = new AIMessage({
    id: "lc-matrix",
    content: "langchain-runtime-response",
    response_metadata: { model_name: "gemini-3.5-flash", model_provider: "google_genai" },
    usage_metadata: {
      input_tokens: 13,
      output_tokens: 8,
      total_tokens: 21,
      input_token_details: { cache_read: 3 },
      output_token_details: { reasoning: 4 },
    },
  });
  const langchainGeneration = {
    text: "langchain-runtime-response",
    message: langchainMessage,
  } satisfies ChatGeneration;
  const langchainResult = {
    generations: [[langchainGeneration]],
    llmOutput: { model_name: "gemini-3.5-flash" },
  } satisfies LLMResult;
  callback.handleChatModelStart(
    { id: ["langchain", "ChatGoogleGenerativeAI"] },
    [[new HumanMessage("hello")]],
    "langchain-matrix",
    undefined,
    { invocation_params: { model: "gemini-3.5-flash" } },
  );
  callback.handleLLMEnd(langchainResult, "langchain-matrix");
  assert.equal((await papaya.flush()).status, "sent");
  const langchainRoot = captured.at(-1)!.traces?.[0]?.spans?.[0];
  assert.equal(langchainRoot?.usage?.totalTokens, 21);
  assert.equal(langchainRoot?.usage?.cacheReadInputTokens, 3);
  assert.equal(langchainRoot?.usage?.reasoningTokens, 4);
  assert.equal(JSON.stringify(langchainRoot?.outputPayload?.value).includes("usage_metadata"), false);
  assert.equal(JSON.stringify(langchainRoot?.outputPayload?.value).includes("langchain-runtime-response"), true);
  covered.add("langchain");

  const GraphState = Annotation.Root({ value: Annotation<string>() });
  const graph = new StateGraph(GraphState)
    .addNode("append_result", (state) => ({ value: `${state.value}-langgraph-runtime-response` }))
    .addEdge(START, "append_result")
    .addEdge("append_result", END)
    .compile();
  papaya = newPapaya();
  callback = new PapayaCallbackHandler(papaya, { workflowKey: "langgraph_matrix" });
  const graphResult = await graph.invoke({ value: "start" }, { callbacks: [callback] });
  assert.equal(graphResult.value, "start-langgraph-runtime-response");
  assert.equal((await papaya.flush()).status, "sent");
  assert.equal(JSON.stringify(captured.at(-1)).includes("langgraph-runtime-response"), true);
  covered.add("langgraph");

  assert.deepEqual([...covered].sort(), [...SUPPORTED_TYPESCRIPT_COMBINATIONS].sort());
  console.log("papaya-ai provider/framework matrix tests passed");
} finally {
  globalThis.fetch = originalFetch;
}
