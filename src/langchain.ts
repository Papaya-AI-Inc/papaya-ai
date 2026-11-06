import { papayaUsageFromResult, papayaModelFromResult } from "./usage.js";
import { BaseCallbackHandler } from "@langchain/core/callbacks/base";

import {
  Papaya,
  type PapayaPayloadRef,
  type PapayaTrace,
  type PapayaTraceSpan,
  type RunOptions,
  type SpanKind,
  type SpanStatus,
} from "./index.js";

type Serialized = unknown;
type MessageBatches = unknown[][];
type LaneSnapshot = {
  input: MessageBatches;
  output: MessageBatches;
};
type LaneState = {
  activeRunIds: Set<string>;
  previous?: LaneSnapshot;
};
type RunState = {
  trace: PapayaTrace;
  span: PapayaTraceSpan;
  kind: SpanKind;
  parentRunId?: string;
  inputMessages?: MessageBatches;
  llm?: {
    laneKey: string;
    laneInput?: MessageBatches;
    overlapped: boolean;
  };
};

export type PapayaCallbackHandlerOptions = RunOptions & {
  metadata?: Record<string, unknown>;
  captureLLM?: boolean;
};

const idText = (value: unknown): string => String(value);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const jsonable = (value: unknown, seen = new WeakSet<object>()): unknown => {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "bigint") return value.toString();
  if (Array.isArray(value)) return value.map((item) => jsonable(item, seen));
  if (!isRecord(value)) return String(value);
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  try {
    if ("content" in value) {
      return {
        ...Object.fromEntries(Object.entries(value).map(([key, item]) => [key, jsonable(item, seen)])),
        role: roleFromMessage(value),
      };
    }
    if (typeof value.toJSON === "function") {
      try {
        return jsonable(value.toJSON(), seen);
      } catch {
        // Fall through to enumerable fields.
      }
    }
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, jsonable(item, seen)]));
  } finally {
    seen.delete(value);
  }
};

const roleFromMessage = (message: Record<string, unknown>): string => {
  const raw = typeof message.role === "string"
    ? message.role
    : typeof message.type === "string"
      ? message.type
      : typeof message._getType === "function"
        ? String(message._getType())
        : message.constructor?.name?.toLowerCase();
  const text = String(raw ?? "message").toLowerCase();
  if (["human", "user", "humanmessage"].includes(text)) return "user";
  if (["ai", "assistant", "aimessage", "model"].includes(text)) return "assistant";
  if (["system", "systemmessage", "developer"].includes(text)) return "system";
  if (["tool", "toolmessage", "function"].includes(text)) return "tool";
  return text;
};

const presentText = (value: unknown): string | undefined => {
  if (typeof value !== "string" || value.length === 0 || value === "undefined") return undefined;
  return value;
};

const firstValue = (source: Record<string, unknown>, ...keys: string[]): unknown => {
  for (const key of keys) {
    if (key in source) return source[key];
  }
  return undefined;
};

const compactToolCall = (value: unknown): unknown => {
  const serialized = jsonable(value);
  if (!isRecord(serialized)) return serialized;
  const functionValue = isRecord(serialized.function) ? serialized.function : undefined;
  const name = presentText(firstValue(serialized, "name")) ?? presentText(functionValue?.name);
  const result: Record<string, unknown> = {};
  const callId = presentText(firstValue(serialized, "id"));
  if (callId) result.id = callId;
  if (name) result.name = name;
  if ("args" in serialized) result.args = serialized.args;
  else if (functionValue && "arguments" in functionValue) result.args = functionValue.arguments;
  if ("error" in serialized && serialized.error !== undefined && serialized.error !== null && serialized.error !== "") {
    result.error = serialized.error;
  }
  return Object.keys(result).length > 0 ? result : serialized;
};

const compactMessage = (value: unknown): unknown => {
  if (!isRecord(value)) return jsonable(value);
  const serializedValue = jsonable(value);
  const serialized = isRecord(serializedValue) ? serializedValue : {};
  const additional = isRecord(firstValue(value, "additional_kwargs", "additionalKwargs"))
    ? firstValue(value, "additional_kwargs", "additionalKwargs") as Record<string, unknown>
    : isRecord(firstValue(serialized, "additional_kwargs", "additionalKwargs"))
      ? firstValue(serialized, "additional_kwargs", "additionalKwargs") as Record<string, unknown>
      : undefined;
  const content = "content" in value
    ? jsonable(value.content)
    : "content" in serialized
      ? serialized.content
      : "";
  const result: Record<string, unknown> = {
    role: roleFromMessage(value),
    content,
  };

  const name = presentText(firstValue(value, "name")) ?? presentText(firstValue(serialized, "name"));
  const messageId = presentText(firstValue(value, "id")) ?? presentText(firstValue(serialized, "id"));
  const toolCallsValue = firstValue(value, "tool_calls", "toolCalls")
    ?? firstValue(serialized, "tool_calls", "toolCalls")
    ?? firstValue(additional ?? {}, "tool_calls", "toolCalls");
  const invalidToolCallsValue = firstValue(value, "invalid_tool_calls", "invalidToolCalls")
    ?? firstValue(serialized, "invalid_tool_calls", "invalidToolCalls");
  const toolCallId = presentText(firstValue(value, "tool_call_id", "toolCallId"))
    ?? presentText(firstValue(serialized, "tool_call_id", "toolCallId"));
  const artifact = firstValue(value, "artifact") ?? firstValue(serialized, "artifact");

  if (name) result.name = name;
  if (messageId) result.id = messageId;
  if (Array.isArray(toolCallsValue) && toolCallsValue.length > 0) {
    result.toolCalls = toolCallsValue.map((call) => compactToolCall(call));
  }
  if (Array.isArray(invalidToolCallsValue) && invalidToolCallsValue.length > 0) {
    result.invalidToolCalls = invalidToolCallsValue.map((call) => compactToolCall(call));
  }
  if (toolCallId) result.toolCallId = toolCallId;
  if (artifact !== undefined && artifact !== null) result.artifact = jsonable(artifact);
  return result;
};

const compactMessageBatches = (messages: unknown): MessageBatches => {
  if (!Array.isArray(messages)) return [[compactMessage(messages)]];
  if (messages.every((item) => Array.isArray(item))) {
    return messages.map((batch) => (batch as unknown[]).map((message) => compactMessage(message)));
  }
  return [messages.map((message) => compactMessage(message))];
};

const isMessageLike = (value: unknown): boolean =>
  isRecord(value)
  && "content" in value
  && (
    "role" in value
    || "type" in value
    || typeof value._getType === "function"
    || ["humanmessage", "aimessage", "systemmessage", "toolmessage"].includes(
      String(value.constructor?.name ?? "").toLowerCase(),
    )
  );

const compactLangChainValue = (value: unknown): unknown => {
  if (Array.isArray(value)) {
    if (value.length > 0 && value.every((item) => isMessageLike(item))) {
      return value.map((message) => compactMessage(message));
    }
    return value.map((item) => compactLangChainValue(item));
  }
  if (!isRecord(value)) return jsonable(value);
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, compactLangChainValue(item)]),
  );
};

const messageBatchesFromValue = (value: unknown): MessageBatches => {
  const batches: MessageBatches = [];
  const visit = (current: unknown): void => {
    if (Array.isArray(current)) {
      if (current.length > 0 && current.every((item) => isMessageLike(item))) {
        batches.push(current.map((message) => compactMessage(message)));
        return;
      }
      current.forEach((item) => visit(item));
      return;
    }
    if (isRecord(current)) Object.values(current).forEach((item) => visit(item));
  };
  visit(value);
  return batches;
};

const compactGenerations = (value: unknown): unknown => {
  const generations = isRecord(value) && Array.isArray(value.generations) ? value.generations : undefined;
  if (!generations) return jsonable(value);
  return generations.map((batch) => {
    const items = Array.isArray(batch) ? batch : [batch];
    return items.map((generation) => {
      if (isRecord(generation) && "message" in generation) return compactMessage(generation.message);
      if (isRecord(generation) && typeof generation.text === "string") {
        return { role: "assistant", content: generation.text };
      }
      return jsonable(generation);
    });
  });
};

const messageBatchesFromPayload = (payload: PapayaPayloadRef | undefined): MessageBatches | undefined => {
  const value = payload?.value;
  if (!Array.isArray(value) || !value.every((batch) => Array.isArray(batch))) return undefined;
  return value as MessageBatches;
};

const stableValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map((item) => stableValue(item));
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, stableValue(value[key])]),
  );
};

const messagesEqual = (left: unknown, right: unknown, ignoreSourceMessageId = false): boolean => {
  const comparable = (value: unknown): unknown => {
    if (!ignoreSourceMessageId || !isRecord(value)) return stableValue(value);
    const { id: _sourceMessageId, ...rest } = value;
    return stableValue(rest);
  };
  return JSON.stringify(comparable(left)) === JSON.stringify(comparable(right));
};

const commonPrefixLength = (left: unknown[], right: unknown[]): number => {
  const maximum = Math.min(left.length, right.length);
  let index = 0;
  while (index < maximum && messagesEqual(left[index], right[index])) index += 1;
  return index;
};

const boundaryOverlapLength = (previousOutput: unknown[], suffix: unknown[]): number => {
  const maximum = Math.min(previousOutput.length, suffix.length);
  for (let size = maximum; size > 0; size -= 1) {
    const outputTail = previousOutput.slice(previousOutput.length - size);
    const suffixHead = suffix.slice(0, size);
    if (outputTail.every((message, index) => messagesEqual(message, suffixHead[index], true))) return size;
  }
  return 0;
};

const removeKnownHistory = (known: MessageBatches, current: MessageBatches): MessageBatches => {
  return current.map((batch) => {
    let retained = batch;
    for (const previousBatch of known) {
      if (previousBatch.length === 0 || previousBatch.length > retained.length) continue;
      for (let start = 0; start <= retained.length - previousBatch.length; start += 1) {
        if (
          previousBatch.every((message, index) =>
            messagesEqual(message, retained[start + index], true))
        ) {
          retained = [
            ...retained.slice(0, start),
            ...retained.slice(start + previousBatch.length),
          ];
          break;
        }
      }
    }
    return retained;
  });
};

const compactRepeatedHistory = (previous: LaneSnapshot, current: MessageBatches): MessageBatches | undefined => {
  if (previous.input.length !== current.length) return undefined;
  return current.map((batch, index) => {
    const previousInput = previous.input[index] ?? [];
    const previousOutput = previous.output[index] ?? [];
    const repeatedInput = commonPrefixLength(previousInput, batch);
    const suffix = batch.slice(repeatedInput);
    const repeatedOutput = boundaryOverlapLength(previousOutput, suffix);
    return suffix.slice(repeatedOutput);
  });
};

const payloadWithValue = (payload: PapayaPayloadRef, value: unknown): PapayaPayloadRef => ({
  ...payload,
  value,
  byteLength: new TextEncoder().encode(JSON.stringify(value ?? null)).length,
});

const generationsFromResponse = (value: unknown): unknown => {
  return compactGenerations(value);
};

const serializedName = (serialized: Serialized, fallback: string): string => {
  if (typeof serialized === "string" && serialized) return serialized;
  if (isRecord(serialized)) {
    if (typeof serialized.name === "string" && serialized.name) return serialized.name;
    if (Array.isArray(serialized.id) && serialized.id.length > 0) return String(serialized.id.at(-1));
    if (typeof serialized.id === "string" && serialized.id) return serialized.id;
  }
  return fallback;
};

const numberValue = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

// LangChain puts one call's result in llmOutput and its first generation message.
// Read these protocol locations only; prompt/tool content is never a usage source.
const firstGenerationMessage = (value: unknown): unknown => {
  if (!isRecord(value) || !Array.isArray(value.generations)) return undefined;
  const first = value.generations[0];
  const generation = Array.isArray(first) ? first[0] : first;
  return isRecord(generation) ? generation.message ?? generation : undefined;
};
const usageFromNested = (value: unknown, extract: (v: unknown) => ReturnType<typeof papayaUsageFromResult>) => {
  const primary = extract(value);
  const message = extract(firstGenerationMessage(value));
  if (!primary) return message;
  if (!message) return primary;
  return { ...message, ...primary };
};

const modelLabel = (serialized: Serialized, extraParams?: Record<string, unknown>, metadata?: Record<string, unknown>): string | undefined => {
  const invocation = isRecord(extraParams?.invocation_params) ? extraParams.invocation_params : extraParams;
  const value = invocation?.model ?? invocation?.model_name ?? invocation?.modelName ?? metadata?.ls_model_name ?? metadata?.model;
  if (typeof value === "string" && value) return value;
  return serializedName(serialized, "");
};

const modelFromResponse = (value: unknown): string | undefined =>
  papayaModelFromResult(value) ?? papayaModelFromResult(firstGenerationMessage(value));

const responseAttributesFromResponse = (value: unknown): Record<string, unknown> | undefined => {
  const serialized = jsonable(value);
  let responseMetadata: Record<string, unknown> | undefined;
  const visit = (item: unknown): void => {
    if (responseMetadata) return;
    if (Array.isArray(item)) {
      for (const child of item) visit(child);
      return;
    }
    if (!isRecord(item)) return;
    for (const key of ["response_metadata", "responseMetadata", "generation_info", "generationInfo"]) {
      if (isRecord(item[key])) {
        responseMetadata = item[key] as Record<string, unknown>;
        return;
      }
    }
    for (const child of Object.values(item)) visit(child);
  };
  visit(serialized);
  if (!responseMetadata) return undefined;

  const finishReason = presentText(
    responseMetadata.finish_reason
      ?? responseMetadata.finishReason
      ?? responseMetadata.stop_reason
      ?? responseMetadata.stopReason,
  );
  const excluded = new Set([
    "finish_reason", "finishReason", "stop_reason", "stopReason",
    "model", "model_name", "modelName",
    "usage", "usage_metadata", "usageMetadata", "token_usage", "tokenUsage",
  ]);
  const providerMetadata = Object.fromEntries(
    Object.entries(responseMetadata).filter(([key, item]) =>
      !excluded.has(key) && item !== undefined && item !== null),
  );
  const attributes: Record<string, unknown> = {};
  if (finishReason) attributes.finishReason = finishReason;
  if (Object.keys(providerMetadata).length > 0) attributes.langchainResponseMetadata = providerMetadata;
  return Object.keys(attributes).length > 0 ? attributes : undefined;
};

const knownChainRunTypes = new Set(["chain", "llm", "tool", "retriever", "parser", "prompt", "router"]);

export class PapayaCallbackHandler extends BaseCallbackHandler {
  name = "PapayaCallbackHandler";
  ignoreLLM = false;
  ignoreChain = false;
  ignoreAgent = false;
  ignoreRetriever = false;
  ignoreCustomEvent = true;
  raiseError = false;
  awaitHandlers = false;

  private readonly papaya: Papaya;
  private readonly options: PapayaCallbackHandlerOptions;
  private readonly runs = new Map<string, RunState>();
  private readonly lanes = new Map<string, LaneState>();

  constructor(papaya: Papaya, options: PapayaCallbackHandlerOptions = {}) {
    super();
    this.papaya = papaya;
    this.options = options;
  }

  copy(): PapayaCallbackHandler {
    return this;
  }

  private start(input: {
    runId: unknown;
    parentRunId?: unknown;
    name: string;
    kind: SpanKind;
    inputValue?: unknown;
    inputPayload?: PapayaPayloadRef;
    metadata?: Record<string, unknown>;
    modelRef?: { provider?: string; requested?: string };
    inputMessages?: MessageBatches;
    llm?: RunState["llm"];
  }): void {
    const runId = idText(input.runId);
    const parentRunId = input.parentRunId === undefined ? undefined : idText(input.parentRunId);
    const parent = parentRunId ? this.runs.get(parentRunId) : undefined;
    const metadata = { ...(this.options.metadata ?? {}), ...(input.metadata ?? {}) };
    const attributes = {
      framework: "langchain",
      langchainRunId: runId,
      ...(parentRunId ? { langchainParentRunId: parentRunId } : {}),
      metadata,
    };

    if (!parent) {
      const trace = this.papaya.startTrace({
        traceId: this.options.traceId,
        runId: this.options.runId,
        sessionId: this.options.sessionId,
        conversationId: this.options.conversationId,
        userId: this.options.userId,
        organizationId: this.options.organizationId,
        workflowKey: this.options.workflowKey ?? "langchain_callback",
        workflowLabel: this.options.workflowLabel ?? "LangChain callback run",
        conversational: this.options.conversational,
        metadata: { ...metadata, framework: "langchain" },
      }, {
        rootName: input.name,
        rootKind: input.kind === "agent" ? "workflow" : input.kind,
        inputValue: input.inputValue,
        inputPayload: input.inputPayload,
        modelRef: input.modelRef,
        attributes,
      });
      this.runs.set(runId, {
        trace,
        span: trace.spans[0]!,
        kind: input.kind,
        parentRunId,
        ...(input.inputMessages && input.inputMessages.length > 0
          ? { inputMessages: input.inputMessages }
          : {}),
        ...(input.llm ? { llm: input.llm } : {}),
      });
      return;
    }

    const span = this.papaya.startSpan({
      trace: parent.trace,
      parentSpanId: parent.span.spanId,
      name: input.name,
      kind: input.kind,
      inputValue: input.inputValue,
      inputPayload: input.inputPayload,
      modelRef: input.modelRef,
      attributes,
    });
    this.runs.set(runId, {
      trace: parent.trace,
      span,
      kind: input.kind,
      parentRunId,
      ...(input.inputMessages && input.inputMessages.length > 0
        ? { inputMessages: input.inputMessages }
        : {}),
      ...(input.llm ? { llm: input.llm } : {}),
    });
  }

  private finish(input: {
    runId: unknown;
    status: SpanStatus;
    outputValue?: unknown;
    outputPayload?: PapayaPayloadRef;
    outputMessages?: MessageBatches;
    usage?: Record<string, number | string | undefined>;
    modelUsed?: string;
    responseAttributes?: Record<string, unknown>;
    error?: unknown;
  }): void {
    const runId = idText(input.runId);
    const state = this.runs.get(runId);
    if (!state) return;
    if (input.responseAttributes) {
      state.span.attributes = {
        ...(state.span.attributes ?? {}),
        ...input.responseAttributes,
      };
    }
    if (state.llm) {
      const lane = this.lanes.get(state.llm.laneKey);
      lane?.activeRunIds.delete(runId);
      if (
        lane
        && input.status === "success"
        && !state.llm.overlapped
        && lane.activeRunIds.size === 0
        && state.llm.laneInput
      ) {
        lane.previous = {
          input: state.llm.laneInput,
          output: input.outputMessages ?? [],
        };
      } else if (lane) {
        lane.previous = undefined;
      }
    }
    const childLaneKey = `parent:${runId}`;
    const childLane = this.lanes.get(childLaneKey);
    if (childLane && childLane.activeRunIds.size === 0) this.lanes.delete(childLaneKey);
    const rootLaneKey = `root:${runId}`;
    const rootLane = this.lanes.get(rootLaneKey);
    if (rootLane && rootLane.activeRunIds.size === 0) this.lanes.delete(rootLaneKey);
    this.runs.delete(runId);
    if (!state.parentRunId) {
      this.papaya.finishTrace(state.trace, input.status, {
        outputValue: input.outputValue,
        outputPayload: input.outputPayload,
        usage: input.usage as never,
        modelUsed: input.modelUsed,
        error: input.error,
      });
      return;
    }
    this.papaya.finishSpan(state.span, input.status, {
      outputValue: input.outputValue,
      outputPayload: input.outputPayload,
      usage: input.usage as never,
      modelUsed: input.modelUsed,
      error: input.error,
    });
  }

  private captureChatInput(runIdValue: unknown, parentRunIdValue: unknown, messages: unknown): {
    inputPayload: PapayaPayloadRef;
    llm: NonNullable<RunState["llm"]>;
  } {
    const runId = idText(runIdValue);
    const parentRunId = parentRunIdValue === undefined ? undefined : idText(parentRunIdValue);
    const laneKey = parentRunId ? `parent:${parentRunId}` : `root:${runId}`;
    const lane = this.lanes.get(laneKey) ?? { activeRunIds: new Set<string>() };
    this.lanes.set(laneKey, lane);

    if (lane.activeRunIds.size > 0) {
      for (const activeRunId of lane.activeRunIds) {
        const active = this.runs.get(activeRunId);
        if (active?.llm) active.llm.overlapped = true;
      }
      lane.previous = undefined;
    }

    const compact = compactMessageBatches(messages);
    const fullPayload = this.papaya.capturePayload(compact);
    const fullInput = messageBatchesFromPayload(fullPayload);
    const parentMessages = parentRunId ? this.runs.get(parentRunId)?.inputMessages : undefined;
    const afterParent = fullInput && parentMessages
      ? removeKnownHistory(parentMessages, fullInput)
      : fullInput;
    const retained = afterParent && lane.activeRunIds.size === 0 && lane.previous
      ? compactRepeatedHistory(lane.previous, afterParent)
      : afterParent !== fullInput
        ? afterParent
        : undefined;
    const inputPayload = retained ? payloadWithValue(fullPayload, retained) : fullPayload;
    lane.activeRunIds.add(runId);

    return {
      inputPayload,
      llm: {
        laneKey,
        ...(afterParent ? { laneInput: afterParent } : {}),
        overlapped: lane.activeRunIds.size > 1,
      },
    };
  }

  handleChainStart(serialized: Serialized, inputs: unknown, runId: string, arg4?: string, _tags?: string[], metadata?: Record<string, unknown>, arg7?: string, arg8?: string): void {
    const declaredOrder = (arg8 !== undefined && this.runs.has(arg8)) || (arg4 !== undefined && knownChainRunTypes.has(arg4));
    const parentRunId = declaredOrder ? arg8 : arg4;
    const runType = declaredOrder ? arg4 : arg7;
    const runName = declaredOrder ? arg7 : arg8;
    this.start({
      runId,
      parentRunId,
      name: runName ?? serializedName(serialized, runType ? `langchain.${runType}` : "langchain.chain"),
      kind: parentRunId ? "workflow" : "agent",
      inputValue: compactLangChainValue(inputs),
      inputMessages: messageBatchesFromValue(inputs),
      metadata,
    });
  }

  handleChainEnd(outputs: unknown, runId: string): void {
    this.finish({ runId, status: "success", outputValue: compactLangChainValue(outputs) });
  }

  handleChainError(error: Error, runId: string): void {
    this.finish({ runId, status: "failed", error });
  }

  handleLLMStart(serialized: Serialized, prompts: string[], runId: string, parentRunId?: string, extraParams?: Record<string, unknown>, _tags?: string[], metadata?: Record<string, unknown>, runName?: string): void {
    if (this.options.captureLLM === false) return;
    const model = modelLabel(serialized, extraParams, metadata);
    this.start({
      runId,
      parentRunId,
      name: runName ?? serializedName(serialized, "langchain.llm"),
      kind: "llm",
      inputValue: prompts,
      metadata,
      modelRef: { provider: presentText(metadata?.ls_provider) ?? "langchain", requested: model },
    });
  }

  handleChatModelStart(serialized: Serialized, messages: unknown[][], runId: string, parentRunId?: string, extraParams?: Record<string, unknown>, _tags?: string[], metadata?: Record<string, unknown>, runName?: string): void {
    if (this.options.captureLLM === false) return;
    const model = modelLabel(serialized, extraParams, metadata);
    const capture = this.captureChatInput(runId, parentRunId, messages);
    this.start({
      runId,
      parentRunId,
      name: runName ?? serializedName(serialized, "langchain.chat_model"),
      kind: "llm",
      inputPayload: capture.inputPayload,
      metadata,
      modelRef: { provider: presentText(metadata?.ls_provider) ?? "langchain", requested: model },
      llm: capture.llm,
    });
  }

  handleLLMEnd(output: unknown, runId: string): void {
    if (this.options.captureLLM === false) return;
    const usage = usageFromNested(output, value => this.papaya.captureUsage(value));
    const modelUsed = modelFromResponse(output);
    const responseAttributes = responseAttributesFromResponse(output);
    const compactOutput = generationsFromResponse(output);
    const outputPayload = this.papaya.capturePayload(compactOutput);
    this.finish({
      runId,
      status: "success",
      outputPayload,
      outputMessages: messageBatchesFromPayload(outputPayload),
      usage,
      modelUsed,
      responseAttributes,
    });
  }

  handleLLMError(error: Error, runId: string): void {
    if (this.options.captureLLM === false) return;
    this.finish({ runId, status: "failed", error });
  }

  handleToolStart(serialized: Serialized, input: string, runId: string, parentRunId?: string, _tags?: string[], metadata?: Record<string, unknown>, runName?: string, toolCallId?: string): void {
    this.start({
      runId,
      parentRunId,
      name: runName ?? serializedName(serialized, "langchain.tool"),
      kind: "tool",
      inputValue: input,
      metadata: { ...metadata, toolCallId },
    });
  }

  handleToolEnd(output: unknown, runId: string): void {
    this.finish({ runId, status: "success", outputValue: jsonable(output) });
  }

  handleToolError(error: Error, runId: string): void {
    this.finish({ runId, status: "failed", error });
  }

  handleRetrieverStart(serialized: Serialized, query: string, runId: string, parentRunId?: string, _tags?: string[], metadata?: Record<string, unknown>, runName?: string): void {
    this.start({
      runId,
      parentRunId,
      name: runName ?? serializedName(serialized, "langchain.retriever"),
      kind: "retrieval",
      inputValue: query,
      metadata,
    });
  }

  handleRetrieverEnd(documents: unknown, runId: string): void {
    this.finish({ runId, status: "success", outputValue: jsonable(documents) });
  }

  handleRetrieverError(error: Error, runId: string): void {
    this.finish({ runId, status: "failed", error });
  }
}

export { PapayaCallbackHandler as PapayaLangChainCallbackHandler };
