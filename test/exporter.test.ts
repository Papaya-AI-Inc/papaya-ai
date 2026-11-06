import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { Papaya, PapayaCaptureError, type PapayaOptions } from "../src/index.js";

const originalFetch = globalThis.fetch;
const instances: Papaya[] = [];
const init = (options: PapayaOptions = {}) => {
  const instance = Papaya.init({ apiKey: "synthetic-sdk-key", endpoint: "https://synthetic.invalid/api/v1/ingest/traces", retryBaseMs: 25, retryJitter: 0, ...options });
  instances.push(instance); return instance;
};
const capture = (sdk: Papaya, traceId: string, value: unknown = "synthetic") => {
  const trace = sdk.startTrace({ traceId }, { inputValue: value }); sdk.finishTrace(trace, "success"); return trace;
};
const mock = (fn: (body: { batchId: string; traces: Array<{ traceId: string }> }, init?: RequestInit) => Response | Promise<Response>) => {
  globalThis.fetch = (async (_url, options) => fn(JSON.parse(String(options?.body)), options)) as typeof fetch;
};
const ok = () => new Response("{}", { status: 202 });
afterEach(async () => { for (const sdk of instances.splice(0)) await sdk.close(0); globalThis.fetch = originalFetch; });

test("invalid capacity, deadline and multipart options fail explicitly", () => {
  for (const options of [{ maxPendingBatches: 0 }, { maxTraceBytes: -1 }, { requestTimeoutMs: 0 }, { maxRetryAgeMs: NaN }, { retryJitter: 2 }, { multipartIdleTtlSeconds: 60 }, { multipart: true, multipartIdleTtlSeconds: 3 }]) assert.throws(() => init(options), RangeError);
});

test("one poisoned batch cannot block three healthy batches; automatic retries keep frozen identities", async () => {
  const sdk = init({ maxBatchBytes: 1, retryBaseMs: 150 });
  const bodies: string[] = []; const ids: string[] = [];
  mock((body, options) => { ids.push(body.traces[0]!.traceId); bodies.push(String(options?.body)); return body.traces[0]!.traceId === "poison" ? new Response("", { status: 503 }) : ok(); });
  for (const id of ["poison", "healthy-1", "healthy-2", "healthy-3"]) capture(sdk, id);
  const result = await sdk.flush();
  assert.equal(result.status, "failed"); assert.equal(result.sent.traces, 3);
  assert.deepEqual(ids, ["poison", "healthy-1", "healthy-2", "healthy-3"]);
  await delay(190); assert.equal(ids[4], "poison"); assert.equal(bodies[0], bodies[4]);
  assert.equal(sdk.exportStatus().pending.traces, 1);
});

test("Retry-After and exponential backoff cannot be bypassed by repeated flush", async () => {
  const sdk = init({ retryBaseMs: 10 }); let calls = 0;
  mock(() => { calls++; return calls === 1 ? new Response("", { status: 429, headers: { "Retry-After": ".12" } }) : ok(); });
  capture(sdk, "retry-after"); await sdk.flush();
  const early = await sdk.flush(20); assert.equal(early.timedOut, true); assert.equal(calls, 1);
  const final = await sdk.flush(500); assert.equal(final.status, "sent"); assert.equal(calls, 2);
});

test("401/403 pause all sends and retain payloads until explicit reconfiguration", async () => {
  for (const status of [401, 403]) {
    const sdk = init({ maxBatchBytes: 1 }); let calls = 0;
    mock((_body, options) => { calls++; return (options?.headers as Record<string, string>).Authorization === "Bearer replacement-synthetic" ? ok() : new Response("", { status }); });
    capture(sdk, `auth-${status}`); capture(sdk, `later-${status}`);
    assert.equal((await sdk.flush()).authPaused, true); assert.equal(calls, 1);
    await sdk.flush(10); assert.equal(calls, 1); assert.equal(sdk.exportStatus().pending.traces, 2);
    sdk.configureExport({ apiKey: "replacement-synthetic" });
    assert.equal((await sdk.close(500)).status, "sent"); assert.equal(calls, 3);
  }
});

test("partial, malformed and terminal responses keep the entire frozen body with bounded receipts", async () => {
  for (const response of [new Response('{"accepted":0,"rejected":1}', { status: 202 }), new Response('{"accepted":true,"rejected":0}', { status: 202 }), new Response("", { status: 400 }), new Response("", { status: 409 }), new Response("", { status: 413 }), new Response("", { status: 302 })]) {
    const sdk = init(); let calls = 0;
    mock(() => { calls++; return response; }); capture(sdk, "first");
    const result = await sdk.flush(); assert.equal(result.status, "failed"); assert.equal(result.sent.traces, 0);
    assert.equal(result.terminalFailed.traces, 1); await sdk.flush(5); assert.equal(calls, 1);
    assert.equal(result.pending.bytes, result.targetReceipts[0]!.bodyBytes);
  }
});

test("pending limits include terminal payloads; completed captures remain owned without eviction", async () => {
  const sdk = init({ maxPendingBatches: 1, maxBatchBytes: 1, diagnosticLimit: 3 });
  const ids: string[] = []; mock(body => { ids.push(body.traces[0]!.traceId); return new Response("", { status: 400 }); });
  capture(sdk, "oldest"); capture(sdk, "blocked");
  await sdk.flush();
  for (let i = 0; i < 10; i++) await sdk.flush(0);
  const status = sdk.exportStatus();
  assert.equal(status.pending.traces, 1); assert.equal(status.active.completedTraces, 1);
  assert.deepEqual(ids, ["oldest"]); assert.ok(status.diagnostics.length <= 3);
  assert.equal(status.rejected.traces, 0);
  assert.equal((await sdk.close(0)).errorCode, "undelivered_capture");
});

test("active count and byte admission fail explicitly; mutable snapshots cannot change an upload", async () => {
  const sdk = init({ maxActiveTraces: 1, capture: "full" });
  const value = { value: "before" }; const trace = capture(sdk, "snapshot", value);
  value.value = "after"; trace.spans[0]!.name = "mutated";
  assert.throws(() => sdk.startTrace(), (e: unknown) => e instanceof PapayaCaptureError && e.code === "active_traces_capacity");
  let frozen = ""; mock((_body, options) => { frozen = String(options?.body); return ok(); });
  assert.equal((await sdk.flush()).status, "sent"); assert.ok(frozen.includes("before")); assert.ok(!frozen.includes("mutated")); assert.ok(!frozen.includes("after"));
  for (const options of [{ maxTraceBytes: 100 }, { maxActiveBytes: 100 }]) {
    const bounded = init(options);
    assert.throws(() => bounded.startTrace({}, { inputValue: "x".repeat(1_000) }), PapayaCaptureError);
    assert.equal(bounded.exportStatus().active.traces, 0);
  }
});

test("span growth is bounded and repeated manual finish is rejected", async () => {
  const sdk = init({ maxTraceBytes: 1_000, capture: "full" });
  const trace = sdk.startTrace(); const span = sdk.startSpan({ trace, name: "tool", kind: "tool" });
  assert.throws(() => sdk.finishSpan(span, "success", { outputValue: "x".repeat(1_001) }), PapayaCaptureError);
  assert.equal(sdk.exportStatus().active.traces, 0);
  const second = init(); const done = capture(second, "once");
  assert.throws(() => second.finishTrace(done, "success"), PapayaCaptureError);
});

test("flush timeout and cancellation retain one request owner; late acknowledgment never duplicates", async () => {
  const sdk = init({ requestTimeoutMs: 10 }); let calls = 0; let finish!: (r: Response) => void;
  mock(() => { calls++; return new Promise(resolve => { finish = resolve; }); }); capture(sdk, "uncancellable");
  assert.equal((await sdk.flush(20)).timedOut, true);
  const controller = new AbortController(); const waiting = sdk.flush(1_000, controller.signal); controller.abort();
  assert.equal((await waiting).errorCode, "flush_cancelled");
  await sdk.flush(5); assert.equal(calls, 1); assert.equal(sdk.exportStatus().sending.traces, 1);
  const last = sdk.flush(500); finish(ok());
  assert.equal((await last).status, "sent"); assert.equal(calls, 1);
  assert.equal(sdk.exportStatus().receipts.at(-1)?.code, "late_ack");
});

test("deadline covers response-body trickling and aborted requests exhaust without discarding", async () => {
  const sdk = init({ requestTimeoutMs: 20, maxAttempts: 1 });
  globalThis.fetch = (async (_url, options) => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode("{")); options?.signal?.addEventListener("abort", () => controller.error(new Error("synthetic abort")), { once: true }); },
  }), { status: 202 })) as typeof fetch;
  capture(sdk, "slow-body"); const result = await sdk.flush(500);
  assert.equal(result.errorCode, "request_timeout"); assert.equal(result.pending.traces, 1); assert.equal(result.targetReceipts[0]!.deadlineExceeded, true);
});

test("attempt/age budgets stop retries and retain terminal receipts", async () => {
  for (const options of [{ maxAttempts: 2 }, { maxRetryAgeMs: 40, retryBaseMs: 100 }]) {
    const sdk = init(options); let calls = 0; mock(() => { calls++; return new Response("", { status: 503 }); }); capture(sdk, "exhaust");
    const result = await sdk.close(500); assert.equal(result.terminalFailed.traces, 1); assert.equal(result.errorCode, "retry_exhausted"); assert.ok(calls <= 2);
  }
});

test("close drains a backlog through one pending slot, and stops new capture", async () => {
  const sdk = init({ maxPendingBatches: 1, maxBatchBytes: 1 }); mock(ok);
  for (let n = 0; n < 4; n++) capture(sdk, `drain-${n}`);
  const result = await sdk.close(500); assert.equal(result.status, "sent"); assert.equal(result.sent.traces, 4);
  assert.equal(result.closed, true); assert.equal(result.active.traces, 0);
  assert.throws(() => sdk.startTrace(), (e: unknown) => e instanceof PapayaCaptureError && e.code === "capture_closed");
  const unfinished = init(); unfinished.startTrace(); assert.equal((await unfinished.close(0)).status, "failed");
});

test("close deadline stops future queued sends but keeps an in-flight body owned", async () => {
  const sdk = init({ maxBatchBytes: 1 }); let finish!: (r: Response) => void; let calls = 0;
  mock(() => { calls++; return new Promise(resolve => { finish = resolve; }); });
  capture(sdk, "in-flight"); capture(sdk, "queued");
  const result = await sdk.close(15); assert.equal(result.status, "failed"); assert.equal(result.sending.traces, 1); assert.equal(result.queued.traces, 1);
  finish(ok()); await delay(5); assert.equal(calls, 1); assert.equal(sdk.exportStatus().pending.traces, 1);
});

test("provider argument, result and original error identities survive capture failure", async () => {
  const sdk = init({ maxActiveBytes: 100 }); let seen: unknown; const response = { model: "used" };
  const provider = sdk.openai({ create(arg: unknown) { seen = arg; return response; } });
  const arg = { model: "requested" }; assert.equal(provider.create(arg), response); assert.equal(seen, arg);
  const error = new Error("synthetic provider error"); const broken = sdk.openai({ create() { throw error; } });
  assert.throws(() => broken.create(), e => e === error);
  assert.equal(await sdk.run({}, async () => "application result"), "application result");
  const second = init({ capture: "full" }); const cyclic = Object.defineProperty({}, "unreadable", { enumerable: true, get() { throw new Error("synthetic serialization error"); } });
  const result = second.openai({ create() { return cyclic; } }).create(); assert.equal(result, cyclic);
  assert.ok(second.exportStatus().captureFailed.traces > 0);
});

test("concurrent captures/flushes retain all identities once without blocking timer heartbeat", async () => {
  const sdk = init({ maxBatchBytes: 16_384 }); const ids: string[] = [];
  mock(async body => { await delay(3); ids.push(...body.traces.map(t => t.traceId)); return new Response(JSON.stringify({ accepted: body.traces.length, rejected: 0 }), { status: 202 }); });
  let heartbeats = 0; const heartbeat = setInterval(() => heartbeats++, 1);
  try {
    await Promise.all(Array.from({ length: 24 }, async (_, n) => { capture(sdk, `concurrent-${n}`, "x".repeat(4_096)); await sdk.flush(); }));
    assert.equal((await sdk.close(500)).pending.traces, 0); assert.equal(sdk.exportStatus().sent.traces, 24); assert.equal(ids.length, 24); assert.equal(new Set(ids).size, 24); assert.ok(heartbeats > 0);
  } finally { clearInterval(heartbeat); }
});

test("pending byte budgets include retained terminal payloads and diagnostics never contain response content", async () => {
  const sdk = init({ maxPendingBytes: 1_500, maxBatchBytes: 1, capture: "full" });
  mock(() => new Response("synthetic-private-server-body", { status: 400 }));
  for (let n = 0; n < 4; n++) capture(sdk, `bytes-${n}`, "x".repeat(300));
  await sdk.flush(); const state = sdk.exportStatus();
  assert.ok(state.pending.bytes <= 1_500); assert.ok(state.active.completedTraces > 0);
  assert.equal(JSON.stringify(state).includes("synthetic-private-server-body"), false);
});
