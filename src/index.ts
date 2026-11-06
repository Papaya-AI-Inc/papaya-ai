import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";
import { redactPapayaSdkEmailAddresses } from "./redaction.js";
import { PapayaCaptureError, PapayaExporter, type PapayaExporterOptions, type PapayaExportResult, type PapayaFrozenBatch } from "./exporter.js";
import { papayaUsageFromResult, papayaModelFromResult } from "./usage.js";
export { PapayaCaptureError } from "./exporter.js";
export type { PapayaExporterOptions, PapayaExportSnapshot, PapayaExportReceipt } from "./exporter.js";

export type CaptureMode = "metadata" | "redacted" | "full";
export type SpanStatus = "success" | "failed" | "partial" | "unknown";
export type SpanKind = "workflow" | "agent" | "llm" | "tool" | "retrieval" | "embedding" | "reranker" | "memory" | "state_transition" | "guardrail" | "router" | "human" | "handoff" | "evaluator" | "other";

export type PapayaOptions = RunOptions & PapayaExporterOptions & {
  apiKey?: string;
  endpoint?: string;
  project?: string;
  environment?: string;
  capture?: CaptureMode;
  serviceName?: string;
  serviceVersion?: string;
  /**
   * Papaya recommendation ids (`agfind-...`) your application config declares as applied.
   * Papaya owns this channel: it never becomes trace metadata and is never redacted, so put
   * nothing but recommendation ids here. The SDK sends only the newest
   * `APPLIED_RECOMMENDATION_WINDOW` entries of the list.
   */
  appliedRecommendations?: string[];
  maxBatchBytes?: number;
  debug?: boolean;
};

export type PapayaFlushResult = PapayaExportResult & { active: { traces: number; bytes: number; unfinishedTraces: number; completedTraces: number }; blockedCapture: { traces: number; bytes: number } };

export type NativeFetch = typeof globalThis.fetch;
export type FetchInput = Parameters<NativeFetch>[0];
export type FetchInit = NonNullable<Parameters<NativeFetch>[1]>;

export type PapayaFetchCallOptions = RunOptions & {
  provider?: "openai" | "claude" | "anthropic" | "gemini" | "bedrock" | string;
  model?: string;
  spanName?: string;
};

export type PapayaFetchInit = FetchInit & {
  papaya?: PapayaFetchCallOptions;
};

export type PapayaFetchDefaults = RunOptions & {
  provider?: string;
  model?: string;
  spanName?: string;
};

export type PapayaFetch = (input: FetchInput, init?: PapayaFetchInit) => Promise<Response>;

export type RunOptions = {
  runId?: string;
  traceId?: string;
  sessionId?: string;
  conversationId?: string;
  userId?: string;
  organizationId?: string;
  workflowKey?: string;
  workflowLabel?: string;
  conversational?: boolean;
  group?: { name: string; value: string };
  metadata?: Record<string, unknown>;
  provider?: string;
};

type PayloadRef = {
  contentType: "text" | "json" | "messages" | "binary_ref";
  value?: unknown;
  contentRef?: string;
  redactionState: CaptureMode;
  byteLength?: number;
  sha256?: string;
};

type TraceSpan = {
  spanId: string;
  parentSpanId?: string;
  name: string;
  kind: SpanKind;
  startedAt: string;
  endedAt?: string;
  status: SpanStatus;
  error?: { type?: string; message?: string; code?: string };
  inputPayload?: PayloadRef;
  outputPayload?: PayloadRef;
  modelRef?: { provider?: string; requested?: string; used?: string };
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
    cacheReadInputTokens?: number;
    cacheCreationInputTokens?: number;
    reasoningTokens?: number;
    costUsd?: number;
    pricingSource?: "provider" | "papaya_catalog" | "customer" | "unknown";
  };
  tool?: { name: string; arguments?: PayloadRef; result?: PayloadRef };
  attributes?: Record<string, unknown>;
};

type ActiveRun = Required<Pick<RunOptions, "traceId" | "runId">> & RunOptions & {
  rootSpanId: string;
  spans: TraceSpan[];
};

export type PapayaPayloadRef = PayloadRef;
export type PapayaTraceSpan = TraceSpan;
export type PapayaTrace = ActiveRun;

export type PapayaStartTraceOptions = {
  rootSpanId?: string;
  rootName?: string;
  rootKind?: SpanKind;
  inputValue?: unknown;
  inputPayload?: PapayaPayloadRef;
  modelRef?: TraceSpan["modelRef"];
  attributes?: Record<string, unknown>;
  startedAt?: string;
};

export type PapayaStartSpanOptions = {
  trace?: PapayaTrace;
  spanId?: string;
  parentSpanId?: string;
  name: string;
  kind: SpanKind;
  inputValue?: unknown;
  inputPayload?: PapayaPayloadRef;
  modelRef?: TraceSpan["modelRef"];
  attributes?: Record<string, unknown>;
  startedAt?: string;
};

export type PapayaFinishSpanOptions = {
  outputValue?: unknown;
  outputPayload?: PapayaPayloadRef;
  usage?: TraceSpan["usage"];
  modelUsed?: string;
  error?: unknown;
  endedAt?: string;
};

type TraceBatch = {
  schemaVersion: "2026-06-05";
  batchId: string;
  sentAt: string;
  sdk: {
    name: "@papaya-ai/tracing";
    version: string;
    language: "typescript";
    runtime?: string;
    framework?: string;
  };
  resource: {
    serviceName?: string;
    serviceVersion?: string;
    environment?: string;
  };
  /** Papaya-owned control bag. Never customer content, so never redacted. Omitted when empty. */
  papaya?: { appliedRecommendations: string[] };
  traces: Array<ActiveRun & { rootSpanId: string }>;
};

type PapayaPendingBatch = PapayaFrozenBatch & { batch: TraceBatch };

const SDK_VERSION = "0.1.5";
const DEFAULT_MAX_BATCH_BYTES = 512 * 1024;

/**
 * How many applied-recommendation markers a batch carries. The customer config list grows for the
 * life of the application; the wire payload must not. Kept identical in the Python SDK.
 */
export const APPLIED_RECOMMENDATION_WINDOW = 25;

/**
 * Deliberately looser than the exact `findings.id` shape, so a future change to the id minter
 * cannot invalidate markers already written into customer configs.
 */
const MARKER_PATTERN = /^agfind-[A-Za-z0-9_-]{1,56}$/;

const isMarker = (value: unknown): value is string =>
  typeof value === "string" && MARKER_PATTERN.test(value);

/**
 * Builds the Papaya-owned control bag for one batch. Pure function of `applied`, so an unchanged
 * customer config yields a byte-identical bag on every flush. Exported for tests.
 *
 * Returns `undefined` — meaning the `papaya` key is omitted entirely — when there is nothing to
 * send. An empty object or empty array is never emitted.
 */
export const buildPapayaControlBag = (applied?: string[]): { appliedRecommendations: string[] } | undefined => {
  if (!Array.isArray(applied) || applied.length === 0) return undefined;
  // Dedupe keeping the LAST occurrence, so re-adding a marker moves it to the newest position
  // rather than pinning it to its first slot: reverse, dedupe (Set keeps the first hit it sees),
  // then reverse back into the customer's own order.
  const ordered = [...new Set(applied.filter(isMarker).reverse())].reverse();
  // The coding agent appends, so the newest markers are the tail.
  const newest = ordered.slice(-APPLIED_RECOMMENDATION_WINDOW);
  return newest.length > 0 ? { appliedRecommendations: newest } : undefined;
};

const storage = new AsyncLocalStorage<ActiveRun>();

const iso = (): string => new Date().toISOString();

const id = (prefix: string): string => `${prefix}_${randomBytes(16).toString("base64url")}`;

const jsonableValue = (value: unknown, seen = new WeakSet<object>()): unknown => {
  if (value === undefined || value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "bigint") return value.toString();
  if (Array.isArray(value)) return value.map((item) => jsonableValue(item, seen));
  if (typeof value !== "object") return String(value);
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  try {
    const toJSON = (value as { toJSON?: unknown }).toJSON;
    if (typeof toJSON === "function") {
      try {
        const serialized = toJSON.call(value) as unknown;
        if (serialized !== value) return jsonableValue(serialized, seen);
      } catch {
        // Fall through to enumerable runtime fields.
      }
    }
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, jsonableValue(item, seen)]));
  } finally {
    seen.delete(value);
  }
};

const redactString = (value: string): string =>
  redactPapayaSdkEmailAddresses(value)
    .replace(/\b\d{3}-\d{2}-\d{4}\b/g, "[redacted-ssn]")
    .replace(/\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b/g, "[redacted-phone]")
    .replace(/\b(?:sk|pk|papaya|openai|anthropic|gemini|aws)[-_][A-Za-z0-9_-]{12,}\b/gi, "[redacted-secret]")
    .replace(/Bearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [redacted-token]");

const redactValue = (value: unknown): unknown => {
  if (typeof value === "string") return redactString(value);
  if (Array.isArray(value)) return value.map((item) => redactValue(item));
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => {
    if (/^(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|secret|password)$/i.test(key)) return [key, "[redacted-secret]"];
    return [key, redactValue(item)];
  }));
};

const contentTypeFor = (value: unknown): PayloadRef["contentType"] => {
  if (Array.isArray(value) && value.every((item) => item && typeof item === "object" && "role" in item)) return "messages";
  if (typeof value === "string") return "text";
  return "json";
};

const byteLength = (value: unknown): number => {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null);
  return new TextEncoder().encode(text).length;
};

const payload = (value: unknown, capture: CaptureMode): PayloadRef => {
  const normalized = jsonableValue(value);
  if (capture === "metadata") {
    return { contentType: contentTypeFor(normalized), redactionState: "metadata", byteLength: byteLength(normalized) };
  }
  const captured = capture === "redacted" ? redactValue(normalized) : normalized;
  return {
    contentType: contentTypeFor(captured),
    value: captured,
    redactionState: capture,
    byteLength: byteLength(captured),
  };
};

const errorPayload = (error: unknown): TraceSpan["error"] => {
  if (error instanceof Error) return { type: error.name, message: error.message };
  return { message: String(error) };
};

const errorStringProp = (value: unknown, key: string): string | undefined => {
  if (!value || typeof value !== "object" || !(key in value)) return undefined;
  const raw = (value as Record<string, unknown>)[key];
  return typeof raw === "string" && raw.trim() ? raw : undefined;
};

const modelFromArgs = (args: unknown[]): string | undefined => {
  const first = args[0];
  if (first && typeof first === "object" && "model" in first && typeof (first as { model?: unknown }).model === "string") {
    return (first as { model: string }).model;
  }
  if (first && typeof first === "object" && "modelId" in first && typeof (first as { modelId?: unknown }).modelId === "string") {
    return (first as { modelId: string }).modelId;
  }
  if (first && typeof first === "object" && "modelVersion" in first && typeof (first as { modelVersion?: unknown }).modelVersion === "string") {
    return (first as { modelVersion: string }).modelVersion;
  }
  if (first && typeof first === "object" && "input" in first) {
    const commandInput = (first as { input?: unknown }).input;
    if (commandInput && typeof commandInput === "object" && "modelId" in commandInput && typeof (commandInput as { modelId?: unknown }).modelId === "string") {
      return (commandInput as { modelId: string }).modelId;
    }
    if (commandInput && typeof commandInput === "object" && "model" in commandInput && typeof (commandInput as { model?: unknown }).model === "string") {
      return (commandInput as { model: string }).model;
    }
  }
  return undefined;
};

const recordValue = (value: unknown): Record<string, unknown> | undefined =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;

const numberValue = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) ? value : undefined;

const compactRecord = <T extends Record<string, unknown>>(record: T): Partial<T> =>
  Object.fromEntries(Object.entries(record).filter(([, value]) => value !== undefined)) as Partial<T>;

const isPromiseLike = (value: unknown): value is Promise<unknown> =>
  Boolean(value) && typeof value === "object" && typeof (value as { then?: unknown }).then === "function";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const runOptionsFrom = (value: unknown): RunOptions | undefined => {
  if (!isRecord(value)) return undefined;
  const options: RunOptions = {};
  if (typeof value.provider === "string") options.provider = value.provider;
  if (typeof value.runId === "string") options.runId = value.runId;
  if (typeof value.traceId === "string") options.traceId = value.traceId;
  if (typeof value.sessionId === "string") options.sessionId = value.sessionId;
  if (typeof value.conversationId === "string") options.conversationId = value.conversationId;
  if (typeof value.userId === "string") options.userId = value.userId;
  if (typeof value.organizationId === "string") options.organizationId = value.organizationId;
  if (typeof value.workflowKey === "string") options.workflowKey = value.workflowKey;
  if (typeof value.workflowLabel === "string") options.workflowLabel = value.workflowLabel;
  if (typeof value.conversational === "boolean") options.conversational = value.conversational;
  if (isRecord(value.group) && typeof value.group.name === "string" && typeof value.group.value === "string") {
    options.group = { name: value.group.name, value: value.group.value };
  }
  if (isRecord(value.metadata)) options.metadata = value.metadata;
  return options;
};

const mergeRunOptions = (...optionSets: Array<RunOptions | undefined>): RunOptions => {
  const result: RunOptions = {};
  for (const options of optionSets) {
    if (!options) continue;
    if (options.provider !== undefined) result.provider = options.provider;
    if (options.runId !== undefined) result.runId = options.runId;
    if (options.traceId !== undefined) result.traceId = options.traceId;
    if (options.sessionId !== undefined) result.sessionId = options.sessionId;
    if (options.conversationId !== undefined) result.conversationId = options.conversationId;
    if (options.userId !== undefined) result.userId = options.userId;
    if (options.organizationId !== undefined) result.organizationId = options.organizationId;
    if (options.workflowKey !== undefined) result.workflowKey = options.workflowKey;
    if (options.workflowLabel !== undefined) result.workflowLabel = options.workflowLabel;
    if (options.conversational !== undefined) result.conversational = options.conversational;
    if (options.group !== undefined) result.group = { ...options.group };
    if (options.metadata) result.metadata = { ...(result.metadata ?? {}), ...options.metadata };
  }
  return result;
};

const providerArgsAndPapayaOptions = (args: unknown[]): { providerArgs: unknown[]; callOptions?: RunOptions } => {
  const first = args[0];
  if (!isRecord(first) || !("papaya" in first)) return { providerArgs: args };
  const { papaya, ...providerRequest } = first;
  return {
    providerArgs: [providerRequest, ...args.slice(1)],
    callOptions: runOptionsFrom(papaya),
  };
};

const isRequestInput = (input: FetchInput): input is Request =>
  typeof Request !== "undefined" && input instanceof Request;

const fetchUrl = (input: FetchInput): string =>
  isRequestInput(input) ? input.url : String(input);

const requestMethod = (input: FetchInput): string =>
  isRequestInput(input) ? input.method : "GET";

const providerFromUrl = (url: string): string => {
  if (url.includes("generativelanguage.googleapis.com")) return "gemini";
  if (url.includes("api.anthropic.com")) return "claude";
  if (url.includes("api.openai.com")) return "openai";
  if (url.includes("bedrock-runtime")) return "bedrock";
  return "fetch";
};

const modelFromRest = (url: string, body: unknown): string | undefined => {
  if (body && typeof body === "object" && "model" in body) {
    return String((body as { model: unknown }).model);
  }
  return url.match(/\/models\/([^:/?]+)/)?.[1];
};

const captureableFetchBody = (body: BodyInit | null | undefined): unknown => {
  if (typeof body === "string") {
    try {
      return JSON.parse(body) as unknown;
    } catch {
      return body;
    }
  }
  if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {
    return Object.fromEntries(body.entries());
  }
  if (!body) return undefined;
  return {
    contentType: (body as { constructor?: { name?: string } }).constructor?.name ?? "BodyInit",
    note: "body was not read by Papaya",
  };
};

const MAX_FETCH_RESPONSE_CAPTURE_BYTES = 64 * 1024;
const textLikeContentType = (contentType: string | null): boolean => {
  const lower = contentType?.toLowerCase() ?? "";
  return lower.includes("application/json") ||
    lower.includes("+json") ||
    lower.startsWith("text/") && !lower.includes("text/event-stream");
};

const parseResponseText = (contentType: string | null, text: string): unknown => {
  const trimmed = text.trim();
  if (!trimmed) return "";
  const lower = contentType?.toLowerCase() ?? "";
  if (lower.includes("json")) {
    try {
      return JSON.parse(trimmed) as unknown;
    } catch {
      return text;
    }
  }
  return text;
};

const captureableFetchResponse = async (response: Response): Promise<Record<string, unknown>> => {
  const contentType = response.headers.get("content-type");
  const contentLengthHeader = response.headers.get("content-length");
  const contentLength = contentLengthHeader ? Number(contentLengthHeader) : undefined;
  const base: Record<string, unknown> = {
    status: response.status,
    statusText: response.statusText,
    contentType,
  };

  if (!textLikeContentType(contentType)) return base;
  if (Number.isFinite(contentLength) && Number(contentLength) > MAX_FETCH_RESPONSE_CAPTURE_BYTES) {
    return { ...base, bodyCapture: "skipped_large_response", contentLength };
  }

  try {
    const text = await response.clone().text();
    const encodedLength = new TextEncoder().encode(text).length;
    if (encodedLength > MAX_FETCH_RESPONSE_CAPTURE_BYTES) {
      return { ...base, bodyCapture: "skipped_large_response", contentLength: encodedLength };
    }
    return { ...base, body: parseResponseText(contentType, text) };
  } catch (error) {
    return {
      ...base,
      bodyCapture: "failed",
      bodyCaptureError: error instanceof Error ? error.message : String(error),
    };
  }
};

const addHeaderNames = (names: Set<string>, headers: HeadersInit | undefined): void => {
  if (!headers) return;
  for (const name of new Headers(headers).keys()) names.add(name);
};

const fetchHeaderNames = (input: FetchInput, init: FetchInit): string[] => {
  const names = new Set<string>();
  if (isRequestInput(input)) addHeaderNames(names, input.headers);
  addHeaderNames(names, init.headers);
  return [...names].sort();
};

export class Papaya {
  private readonly options: Required<Pick<PapayaOptions, "endpoint" | "capture" | "project" | "environment">> & Omit<PapayaOptions, "endpoint" | "capture" | "project" | "environment">;
  private readonly defaultRunOptions: RunOptions;
  private readonly completed: ActiveRun[] = [];
  private readonly exporter: PapayaExporter;
  private readonly active = new Map<ActiveRun, { size: number; spans: Map<TraceSpan, number> }>();
  private readonly spanOwners = new WeakMap<TraceSpan, ActiveRun>();
  private readonly completedSizes = new Map<ActiveRun, number>();
  private admissionClosed = false;

  private constructor(options: PapayaOptions = {}) {
    const {
      apiKey,
      endpoint,
      capture,
      project,
      environment,
      serviceName,
      serviceVersion,
      // Must be destructured explicitly: anything left over falls into runDefaults and would
      // become per-run metadata, which is customer content and gets redacted server-side.
      appliedRecommendations,
      maxBatchBytes,
      debug,
      maxPendingBatches, maxPendingBytes, maxActiveTraces, maxActiveBytes, maxTraceBytes,
      maxRetryAgeMs, maxAttempts, retryBaseMs, retryMaxMs, retryJitter,
      requestTimeoutMs, flushTimeoutMs, diagnosticLimit, multipart, multipartIdleTtlSeconds,
      ...runDefaults
    } = options;
    this.options = {
      apiKey: apiKey ?? process.env.PAPAYA_API_KEY ?? process.env.PAPAYA_INGEST_TOKEN,
      endpoint: endpoint ?? "https://papaya.fyi/api/v1/ingest/traces",
      capture: capture ?? "redacted",
      project: project ?? "default",
      environment: environment ?? "development",
      serviceName,
      serviceVersion,
      appliedRecommendations,
      maxBatchBytes: typeof maxBatchBytes === "number" && maxBatchBytes > 0
        ? Math.trunc(maxBatchBytes)
        : DEFAULT_MAX_BATCH_BYTES,
      debug,
    };
    this.defaultRunOptions = runOptionsFrom(runDefaults) ?? {};
    this.exporter = new PapayaExporter(this.options.apiKey, this.options.endpoint, {
      maxPendingBatches, maxPendingBytes, maxActiveTraces, maxActiveBytes, maxTraceBytes,
      maxRetryAgeMs, maxAttempts, retryBaseMs, retryMaxMs, retryJitter,
      requestTimeoutMs, flushTimeoutMs, diagnosticLimit, multipart, multipartIdleTtlSeconds,
    }, frozen => {
      const original = JSON.parse(frozen.body) as TraceBatch;
      const midpoint = Math.ceil(original.traces.length / 2);
      return [this.newBatch(original.traces.slice(0, midpoint)), this.newBatch(original.traces.slice(midpoint))];
    }, `@papaya-ai/tracing/${SDK_VERSION}`);
  }

  static init(options: PapayaOptions = {}): Papaya {
    return new Papaya(options);
  }

  capturePayload(value: unknown): PapayaPayloadRef {
    try { return payload(value, this.options.capture); }
    catch { throw this.captureError("serialization_failed", 0, true); }
  }

  captureUsage(value: unknown) {
    const diagnostics = new Set<string>();
    const usage = papayaUsageFromResult(value, code => diagnostics.add(code));
    for (const code of diagnostics) this.exporter.note(code);
    return usage;
  }

  async run<T>(options: RunOptions, fn: () => T | Promise<T>): Promise<T> {
    let run: ActiveRun;
    try { run = this.createRun(mergeRunOptions(this.defaultRunOptions, options)); }
    catch (failure) { this.telemetryFailure(failure); return fn(); }
    return storage.run(run, async () => {
      try {
        const result = await fn();
        this.safelyFinish(run, "success");
        return result;
      } catch (error) {
        this.safelyFinish(run, "failed", error);
        throw error;
      }
    });
  }

  startTrace(options: RunOptions = {}, startOptions: PapayaStartTraceOptions = {}): PapayaTrace {
    return this.manualCapture(() => this.createRun(mergeRunOptions(this.defaultRunOptions, options), startOptions));
  }

  finishTrace(trace: PapayaTrace, status: SpanStatus, options: PapayaFinishSpanOptions = {}): void {
    this.manualCapture(() => this.finishRun(trace, status, options.error, options), trace);
  }

  startSpan(options: PapayaStartSpanOptions): PapayaTraceSpan {
    const run = options.trace ?? storage.getStore();
    if (!run) throw new Error("Papaya.startSpan requires an explicit trace or an active papaya.run() scope.");
    return this.manualCapture(() => this.captureSpan(options), run);
  }

  private captureSpan(options: PapayaStartSpanOptions): PapayaTraceSpan {
    const run = options.trace ?? storage.getStore();
    if (!run) {
      throw new Error("Papaya.startSpan requires an explicit trace or an active papaya.run() scope.");
    }
    if (!this.active.has(run)) throw this.captureError("capture_not_active");
    const span: TraceSpan = {
      spanId: options.spanId ?? id("span"),
      parentSpanId: options.parentSpanId ?? run.rootSpanId,
      name: options.name,
      kind: options.kind,
      startedAt: options.startedAt ?? iso(),
      status: "unknown",
      ...(options.inputPayload
        ? { inputPayload: options.inputPayload }
        : options.inputValue !== undefined
          ? { inputPayload: payload(options.inputValue, this.options.capture) }
          : {}),
      ...(options.modelRef ? { modelRef: options.modelRef } : {}),
      ...(options.attributes ? { attributes: options.attributes } : {}),
    };
    run.spans.push(span);
    this.spanOwners.set(span, run);
    this.active.get(run)?.spans.set(span, -1);
    this.accountSpan(span);
    return span;
  }

  finishSpan(span: PapayaTraceSpan, status: SpanStatus, options: PapayaFinishSpanOptions = {}): void {
    this.manualCapture(() => this.completeSpan(span, status, options), this.spanOwners.get(span));
  }

  private completeSpan(span: PapayaTraceSpan, status: SpanStatus, options: PapayaFinishSpanOptions): void {
    span.endedAt = options.endedAt ?? iso();
    span.status = status;
    if (options.outputPayload) span.outputPayload = options.outputPayload;
    else if (options.outputValue !== undefined) span.outputPayload = payload(options.outputValue, this.options.capture);
    if (options.usage) span.usage = compactRecord(options.usage) as TraceSpan["usage"];
    if (options.modelUsed) span.modelRef = { ...span.modelRef, used: options.modelUsed };
    if (options.error !== undefined) span.error = errorPayload(options.error);
    this.accountSpan(span);
  }

  openai<T extends object>(client: T, options?: RunOptions): T {
    return this.wrapClient("openai", client, [], options);
  }

  claude<T extends object>(client: T, options?: RunOptions): T {
    return this.wrapClient("claude", client, [], options);
  }

  anthropic<T extends object>(client: T, options?: RunOptions): T {
    return this.claude(client, options);
  }

  gemini<T extends object>(client: T, options?: RunOptions): T {
    return this.wrapClient("gemini", client, [], options);
  }

  bedrock<T extends object>(client: T, options?: RunOptions): T {
    return this.wrapClient("bedrock", client, [], options);
  }

  vercel<T extends object>(client: T, options?: RunOptions): T {
    return this.wrapClient("vercel", client, [], options);
  }

  fetch(fetchImpl: NativeFetch = globalThis.fetch, defaults: PapayaFetchDefaults = {}): PapayaFetch {
    return (input: FetchInput, init?: PapayaFetchInit) =>
      this.captureFetch(fetchImpl, input, init, defaults);
  }

  async flush(timeoutMs = this.exporter.limits.flushTimeoutMs, signal?: AbortSignal): Promise<PapayaFlushResult> {
    if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw new RangeError("timeoutMs must be finite and nonnegative");
    return this.flushPending(timeoutMs, signal);
  }

  configureExport(options: { apiKey?: string; endpoint?: string }): void {
    const apiKey = Object.hasOwn(options, "apiKey") ? options.apiKey : this.options.apiKey;
    const endpoint = options.endpoint ?? this.options.endpoint;
    this.exporter.configure(apiKey, endpoint);
    this.options.apiKey = apiKey; this.options.endpoint = endpoint;
  }

  private activeStatus() {
    const unfinished = [...this.active.values()];
    return {
      traces: unfinished.length + this.completed.length,
      bytes: unfinished.reduce((n, v) => n + v.size, 0) + [...this.completedSizes.values()].reduce((n, v) => n + v, 0),
      unfinishedTraces: unfinished.length, completedTraces: this.completed.length,
    };
  }

  exportStatus() { return { ...this.exporter.snapshot(), active: this.activeStatus() }; }

  async close(timeoutMs = 5_000): Promise<PapayaFlushResult> {
    if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw new RangeError("timeoutMs must be finite and nonnegative");
    this.admissionClosed = true;
    const until = performance.now() + timeoutMs;
    const targets: ReturnType<PapayaExporter["targets"]> = [];
    let result: PapayaExportResult;
    try {
      do {
        this.freezeCompletedBatches();
        const added = this.exporter.targets().filter(t => !targets.some(old => old.batch === t.batch));
        targets.push(...added);
        result = await this.exporter.wait(targets, Math.max(0, until - performance.now()), true);
        if (!this.completed.length || !added.length || result.authPaused || result.reason === "missing_api_key") break;
      } while (performance.now() < until);
    } finally { this.exporter.stop(); }
    return this.withActive(this.exporter.result(targets, result!.timedOut), true);
  }

  private newBatch(traces: ActiveRun[]): PapayaPendingBatch {
    // Built here, at batch time, rather than at construction: every split batch goes through this
    // builder, so this is what guarantees each one carries the bag.
    const papaya = buildPapayaControlBag(this.options.appliedRecommendations);
    const batch: TraceBatch = {
      schemaVersion: "2026-06-05",
      batchId: id("batch"),
      sentAt: iso(),
      sdk: {
        name: "@papaya-ai/tracing",
        version: SDK_VERSION,
        language: "typescript",
        runtime: `node/${process.version}`,
      },
      resource: {
        serviceName: this.options.serviceName,
        serviceVersion: this.options.serviceVersion,
        environment: this.options.environment,
      },
      ...(papaya ? { papaya } : {}),
      traces,
    };
    return { batch, batchId: batch.batchId, traceIds: traces.map(t => t.traceId), body: JSON.stringify(batch) };
  }

  private freezeCompletedBatches(): void {
    while (this.completed.length) {
      let batch = this.newBatch([this.completed[0]!]);
      let count = 1;
      // Count bytes once per trace; avoid serializing a growing batch for every item.
      const parts = [JSON.stringify(this.completed[0]!)];
      let bytes = Buffer.byteLength(batch.body);
      while (count < this.completed.length) {
        const part = JSON.stringify(this.completed[count]!);
        const added = Buffer.byteLength(part) + 1;
        if (bytes + added > (this.options.maxBatchBytes ?? DEFAULT_MAX_BATCH_BYTES)) break;
        parts.push(part); bytes += added; count++;
      }
      if (count > 1) {
        batch.batch.traces = this.completed.slice(0, count);
        // Serialize the envelope once, then append already serialized trace snapshots.
        const envelope = { ...batch.batch, traces: [] };
        batch = { ...batch, traceIds: batch.batch.traces.map(t => t.traceId), body: JSON.stringify(envelope).slice(0, -2) + parts.join(",") + "]}" };
      }
      if (!this.exporter.admit(batch)) break;
      for (const trace of this.completed.splice(0, count)) this.completedSizes.delete(trace);
    }
  }

  private withActive(result: PapayaExportResult, closing = false): PapayaFlushResult {
    const active = this.activeStatus();
    const blockedCapture = { traces: this.completed.length, bytes: [...this.completedSizes.values()].reduce((n, v) => n + v, 0) };
    if (blockedCapture.traces || (closing && active.traces)) {
      result.status = "failed"; result.errorCode = "undelivered_capture"; delete result.reason;
    }
    return { ...result, active, blockedCapture };
  }

  private async flushPending(timeoutMs: number, signal?: AbortSignal): Promise<PapayaFlushResult> {
    this.freezeCompletedBatches();
    return this.withActive(await this.exporter.wait(this.exporter.targets(), timeoutMs, false, signal));
  }

  private manualCapture<T>(capture: () => T, run?: ActiveRun): T {
    try { return capture(); }
    catch (error) {
      if (error instanceof PapayaCaptureError) {
        if (run && error.code === "serialization_failed") this.active.delete(run);
        throw error;
      }
      if (run) this.active.delete(run);
      throw this.captureError("serialization_failed", 0, true);
    }
  }
  private captureError(code: string, bytes = 0, capture = false): PapayaCaptureError {
    this.exporter.record(code, bytes, capture); return new PapayaCaptureError(code);
  }
  private captureSize(value: unknown): number {
    try { return Buffer.byteLength(JSON.stringify(value)); } catch { throw this.captureError("serialization_failed", 0, true); }
  }
  private checkCapacity(size: number, extraTrace = false): void {
    const active = this.activeStatus(), limits = this.exporter.limits;
    const code = extraTrace && this.admissionClosed ? "capture_closed" : extraTrace && active.traces >= limits.maxActiveTraces ? "active_traces_capacity" : active.bytes + size > limits.maxActiveBytes ? "active_bytes_capacity" : undefined;
    if (code) throw this.captureError(code, Math.max(0, size));
  }
  private accountSpan(span: TraceSpan): void {
    const run = this.spanOwners.get(span), owned = run && this.active.get(run);
    if (!owned) throw this.captureError("capture_not_active");
    let size: number, delta: number;
    try {
      size = this.captureSize(span); delta = size - (owned.spans.get(span) ?? 0);
      if (owned.size + delta > this.exporter.limits.maxTraceBytes) throw this.captureError("trace_bytes_capacity", size);
      this.checkCapacity(delta);
    } catch (error) { this.active.delete(run!); throw error; }
    owned.size += delta; owned.spans.set(span, size);
    const active = this.activeStatus(); this.exporter.updateActive(active.traces, active.bytes);
  }
  private telemetryFailure(error: unknown, run?: ActiveRun): void {
    if (run) this.active.delete(run);
    if (!(error instanceof PapayaCaptureError)) this.exporter.record("capture_failed", 0, true);
  }
  private safelyFinish(run: ActiveRun, status: SpanStatus, error?: unknown): void {
    try { if (this.active.has(run)) this.finishRun(run, status, error); } catch (failure) { this.telemetryFailure(failure, run); }
  }

  private wrapClient<T extends object>(provider: string, client: T, path: string[] = [], wrapperOptions?: RunOptions): T {
    const papaya = this;
    return new Proxy(client, {
      get(target, property, receiver) {
        const value = Reflect.get(target, property, receiver) as unknown;
        if (typeof property === "symbol") return value;
        if (typeof value === "function") {
          return function wrappedProviderCall(this: unknown, ...args: unknown[]) {
            return papaya.captureProviderCall(provider, [...path, property], (providerArgs) => value.apply(this === receiver ? target : this, providerArgs), args, wrapperOptions);
          };
        }
        if (value && typeof value === "object") return papaya.wrapClient(provider, value as object, [...path, property], wrapperOptions) as unknown;
        return value;
      },
    }) as T;
  }

  private async captureFetch(
    fetchImpl: NativeFetch,
    input: FetchInput,
    init: PapayaFetchInit = {},
    defaults: PapayaFetchDefaults,
  ): Promise<Response> {
    const { papaya: callPapaya, ...providerInit } = init;
    const url = fetchUrl(input);
    const requestBody = captureableFetchBody(providerInit.body);
    const provider = callPapaya?.provider ?? defaults.provider ?? providerFromUrl(url);
    const model = callPapaya?.model ?? defaults.model ?? modelFromRest(url, requestBody);
    const activeRun = storage.getStore();
    const callOptions = activeRun
      ? mergeRunOptions(this.defaultRunOptions, defaults, activeRun, runOptionsFrom(callPapaya))
      : mergeRunOptions(this.defaultRunOptions, defaults, runOptionsFrom(callPapaya));
    const spanName = callPapaya?.spanName ?? defaults.spanName;

    if (activeRun) {
      return this.captureFetchInRun(fetchImpl, input, providerInit, {
        provider,
        model,
        url,
        requestBody,
        callOptions,
        spanName,
      });
    }

    let implicitRun: ActiveRun;
    try { implicitRun = this.createRun({ workflowKey: callOptions.workflowKey ?? `${provider}.fetch`, ...callOptions }); }
    catch (failure) { this.telemetryFailure(failure); return fetchImpl(input, providerInit); }

    return storage.run(implicitRun, async () => {
      try {
        const response = await this.captureFetchInRun(fetchImpl, input, providerInit, {
          provider,
          model,
          url,
          requestBody,
          callOptions,
          spanName,
        });
        this.safelyFinish(implicitRun, response.ok ? "success" : "partial");
        return response;
      } catch (error) {
        this.safelyFinish(implicitRun, "failed", error);
        throw error;
      }
    });
  }

  private async captureFetchInRun(
    fetchImpl: NativeFetch,
    input: FetchInput,
    init: FetchInit,
    info: {
      provider: string;
      model?: string;
      url: string;
      requestBody: unknown;
      callOptions: RunOptions;
      spanName?: string;
    },
  ): Promise<Response> {
    const run = storage.getStore();
    if (!run || !this.active.has(run)) return fetchImpl(input, init);

    let span: TraceSpan;
    try { span = {
      spanId: id("span"),
      parentSpanId: run.rootSpanId,
      name: info.spanName ?? `${info.provider}.fetch`,
      kind: "llm",
      startedAt: iso(),
      status: "unknown",
      inputPayload: payload({
        url: info.url,
        method: init.method ?? requestMethod(input),
        headerNames: fetchHeaderNames(input, init),
        body: info.requestBody,
      }, this.options.capture),
      modelRef: { provider: info.provider, requested: info.model },
      attributes: {
        provider: info.provider,
        method: "fetch",
        workflowKey: info.callOptions.workflowKey,
        workflowLabel: info.callOptions.workflowLabel,
        sessionId: info.callOptions.sessionId,
        conversationId: info.callOptions.conversationId,
        userId: info.callOptions.userId,
        organizationId: info.callOptions.organizationId,
        metadata: info.callOptions.metadata,
      },
    };
    run.spans.push(span);
    this.spanOwners.set(span, run); this.active.get(run)?.spans.set(span, -1); this.accountSpan(span);
    } catch (failure) { this.telemetryFailure(failure, run); return fetchImpl(input, init); }

    try {
      const response = await fetchImpl(input, init);
      span.endedAt = iso();
      span.status = response.ok ? "success" : "failed";
      try {
      const capturedResponse = await captureableFetchResponse(response);
      span.outputPayload = payload(capturedResponse, this.options.capture);
      span.usage = this.captureUsage(capturedResponse);
      span.modelRef = { ...span.modelRef, used: papayaModelFromResult(capturedResponse) ?? info.model };
      this.accountSpan(span);
      } catch (failure) { this.telemetryFailure(failure, run); }
      return response;
    } catch (error) {
      span.endedAt = iso();
      span.status = "failed";
      try { span.error = errorPayload(error); this.accountSpan(span); } catch (failure) { this.telemetryFailure(failure, run); }
      throw error;
    }
  }

  private createRun(options: RunOptions, startOptions: PapayaStartTraceOptions = {}): ActiveRun {
    const rootSpanId = startOptions.rootSpanId ?? id("span");
    const traceId = options.traceId ?? id("trace");
    const runId = options.runId ?? id("run");
    this.checkCapacity(0, true);
    const run: ActiveRun = {
      ...options,
      traceId,
      runId,
      rootSpanId,
      spans: [{
        spanId: rootSpanId,
        name: startOptions.rootName ?? options.workflowLabel ?? options.workflowKey ?? "papaya.run",
        kind: startOptions.rootKind ?? "workflow",
        startedAt: startOptions.startedAt ?? iso(),
        status: "unknown",
        ...(startOptions.inputPayload
          ? { inputPayload: startOptions.inputPayload }
          : startOptions.inputValue !== undefined
            ? { inputPayload: payload(startOptions.inputValue, this.options.capture) }
            : {}),
        ...(startOptions.modelRef ? { modelRef: startOptions.modelRef } : {}),
        attributes: {
          project: this.options.project,
          environment: this.options.environment,
          metadata: options.metadata,
          ...(startOptions.attributes ?? {}),
        },
      }],
    };
    const size = this.captureSize(run);
    if (size > this.exporter.limits.maxTraceBytes) throw this.captureError("trace_bytes_capacity", size);
    this.checkCapacity(size, true);
    this.active.set(run, { size, spans: new Map([[run.spans[0]!, this.captureSize(run.spans[0])]]) });
    this.spanOwners.set(run.spans[0]!, run);
    const active = this.activeStatus(); this.exporter.updateActive(active.traces, active.bytes);
    return run;
  }

  private finishRun(run: ActiveRun, status: SpanStatus, error?: unknown, options: PapayaFinishSpanOptions = {}): void {
    if (!this.active.has(run)) throw this.captureError("capture_not_active");
    Object.assign(run.spans[0]!, {
      ...run.spans[0]!,
      endedAt: options.endedAt ?? iso(),
      status,
      ...(options.outputPayload
        ? { outputPayload: options.outputPayload }
        : options.outputValue !== undefined
          ? { outputPayload: payload(options.outputValue, this.options.capture) }
          : {}),
      ...(options.usage ? { usage: compactRecord(options.usage) as TraceSpan["usage"] } : {}),
      ...(options.modelUsed ? { modelRef: { ...run.spans[0]?.modelRef, used: options.modelUsed } } : {}),
      ...(error !== undefined ? { error: errorPayload(error) } : {}),
    });
    this.accountSpan(run.spans[0]!);
    const size = this.captureSize(run), previous = this.active.get(run)!.size;
    if (size > this.exporter.limits.maxTraceBytes) { this.active.delete(run); throw this.captureError("trace_bytes_capacity", size); }
    try { this.checkCapacity(size - previous); } catch (error) { this.active.delete(run); throw error; }
    const snapshot = JSON.parse(JSON.stringify(run)) as ActiveRun;
    this.active.delete(run); this.completed.push(snapshot); this.completedSizes.set(snapshot, size);
  }

  private captureProviderCall<T>(provider: string, path: string[], call: (args: unknown[]) => T, args: unknown[], wrapperOptions?: RunOptions): T {
    let prepared: ReturnType<typeof providerArgsAndPapayaOptions>;
    try { prepared = providerArgsAndPapayaOptions(args); }
    catch (failure) { this.telemetryFailure(failure); return call(args); }
    const { providerArgs, callOptions } = prepared;
    provider = callOptions?.provider ?? wrapperOptions?.provider ?? this.defaultRunOptions.provider ?? provider;
    const run = storage.getStore();
    if (run) {
      return this.captureProviderCallInRun(provider, path, (nextArgs) => call(nextArgs), providerArgs, mergeRunOptions(this.defaultRunOptions, wrapperOptions, run, callOptions));
    }

    const implicitRunOptions = mergeRunOptions(this.defaultRunOptions, wrapperOptions, callOptions);
    let implicitRun: ActiveRun;
    try { implicitRun = this.createRun({ workflowKey: implicitRunOptions.workflowKey ?? `${provider}.${path.join(".")}`, ...implicitRunOptions }); }
    catch (failure) { this.telemetryFailure(failure); return call(providerArgs); }
    return storage.run(implicitRun, () => {
      try {
        const result = this.captureProviderCallInRun(provider, path, (nextArgs) => call(nextArgs), providerArgs, implicitRunOptions);
        if (isPromiseLike(result)) {
          return result.then((value) => {
            this.safelyFinish(implicitRun, "success");
            return value;
          }, (error) => {
            this.safelyFinish(implicitRun, "failed", error);
            throw error;
          }) as T;
        }
        this.safelyFinish(implicitRun, "success");
        return result;
      } catch (error) {
        this.safelyFinish(implicitRun, "failed", error);
        throw error;
      }
    });
  }

  private captureProviderCallInRun<T>(provider: string, path: string[], call: (args: unknown[]) => T, args: unknown[], boundary: RunOptions): T {
    const run = storage.getStore();
    if (!run || !this.active.has(run)) return call(args);
    const spanId = id("span");
    const parentSpanId = run.rootSpanId;
    let span: TraceSpan;
    try {
    const model = modelFromArgs(args);
    span = {
      spanId,
      parentSpanId,
      name: `${provider}.${path.join(".")}`,
      kind: "llm",
      startedAt: iso(),
      status: "unknown",
      inputPayload: payload(args.length === 1 ? args[0] : args, this.options.capture),
      modelRef: { provider, requested: model },
      attributes: {
        provider,
        method: path.join("."),
        workflowKey: boundary.workflowKey,
        workflowLabel: boundary.workflowLabel,
        sessionId: boundary.sessionId,
        conversationId: boundary.conversationId,
        userId: boundary.userId,
        organizationId: boundary.organizationId,
        metadata: boundary.metadata,
      },
    };
    run.spans.push(span);
    this.spanOwners.set(span, run); this.active.get(run)?.spans.set(span, -1); this.accountSpan(span);
    } catch (failure) { this.telemetryFailure(failure, run); return call(args); }

    const finish = (status: SpanStatus, result?: unknown, error?: unknown): void => {
      try {
      span.endedAt = iso();
      span.status = status;
      if (result !== undefined) {
        span.outputPayload = payload(result, this.options.capture);
        span.usage = this.captureUsage(result);
        span.modelRef = { ...span.modelRef, used: papayaModelFromResult(result) ?? span.modelRef?.requested };
      }
      if (error !== undefined) span.error = errorPayload(error);
      this.accountSpan(span);
      } catch (failure) { this.telemetryFailure(failure, run); }
    };

    try {
      const result = call(args);
      if (isPromiseLike(result)) {
        return result.then((value) => {
          finish("success", value);
          return value;
        }, (error) => {
          finish("failed", undefined, error);
          throw error;
        }) as T;
      }
      finish("success", result);
      return result;
    } catch (error) {
      finish("failed", undefined, error);
      throw error;
    }
  }
}
