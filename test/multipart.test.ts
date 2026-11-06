import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { afterEach, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { Papaya, type PapayaOptions } from "../src/index.js";

const hash = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");
const originalFetch = globalThis.fetch;
const instances: Papaya[] = [];
type PapayaTestPart = { partNumber: number; byteLength: number; sha256: string };
class PapayaMultipartFixtureServer {
  config = { enabled: true, maxBytes: 32 * 1024 ** 2, partSizeBytes: 5 * 1024 ** 2, maxParts: 7, minIdleTtlSeconds: 60, maxIdleTtlSeconds: 3600 };
  session?: { uploadId: string; batchId: string; byteLength: number; sha256: string; partSizeBytes: number; state: string; parts: PapayaTestPart[]; receipt?: Record<string, unknown> };
  parts = new Map<number, Buffer>(); requests: Array<{ method: string; path: string; hash: string }> = [];
  lose?: "create" | "part" | "complete";
  failCompletion?: { status: number; error: string; state: string };
  invalidReceipt = false; conflictPart = false; createCount = 0; completeCount = 0;
  landed?: Buffer;
  respond(method: string, url: string, body = Buffer.alloc(0), headers = new Headers()): Response {
    const path = new URL(url).pathname; this.requests.push({ method, path, hash: hash(body) });
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
    const lost = (phase: typeof this.lose) => { if (phase === this.lose) { this.lose = undefined; throw new Error("synthetic lost acknowledgment"); } };
    if (path.endsWith("/config")) return json({ features: { multipart: this.config } });
    if (path.endsWith("/trace-uploads") && method === "POST") {
      const declaration = JSON.parse(body.toString());
      if (!this.session) { this.createCount++; this.session = { ...declaration, uploadId: "synthetic-upload", partSizeBytes: this.config.partSizeBytes, state: "uploading", parts: [] }; }
      else assert.deepEqual([declaration.batchId, declaration.sha256, declaration.byteLength], [this.session.batchId, this.session.sha256, this.session.byteLength]);
      lost("create"); return json(this.session);
    }
    assert.ok(this.session);
    if (method === "GET") {
      if (this.conflictPart && this.session.parts.length) return json({ ...this.session, parts: [{ ...this.session.parts[0], sha256: "f".repeat(64) }] });
      return json(this.session);
    }
    if (method === "PUT") {
      const partNumber = Number(path.split("/").at(-1));
      assert.equal(headers.get("X-Papaya-Part-Sha256"), hash(body));
      const receipt = { partNumber, byteLength: body.length, sha256: hash(body) };
      if (this.parts.has(partNumber)) assert.equal(hash(this.parts.get(partNumber)!), hash(body));
      this.parts.set(partNumber, Buffer.from(body));
      this.session.parts = [...this.parts].map(([n, b]) => ({ partNumber: n, byteLength: b.length, sha256: hash(b) }));
      lost("part"); return json(receipt);
    }
    if (path.endsWith("/complete")) {
      this.completeCount++;
      assert.deepEqual(JSON.parse(body.toString()).parts, [...this.session.parts].sort((a, b) => a.partNumber - b.partNumber));
      if (this.failCompletion) { const error = this.failCompletion; this.failCompletion = undefined; this.session.state = error.state; return json({ error: error.error }, error.status); }
      this.landed = Buffer.concat([...this.parts].sort(([a], [b]) => a - b).map(([, b]) => b));
      assert.equal(hash(this.landed), this.session.sha256); assert.equal(this.landed.length, this.session.byteLength);
      const batch = JSON.parse(this.landed.toString());
      this.session.state = "landed";
      this.session.receipt = { uploadId: this.session.uploadId, batchId: this.session.batchId, accepted: this.invalidReceipt ? 0 : batch.traces.length, rejected: 0, warnings: [], rawSha256: hash("synthetic post-policy raw"), rawBytes: 123, landedAt: new Date().toISOString() };
      lost("complete"); return json(this.session.receipt);
    }
    return json({ error: "unknown route" }, 404);
  }
  install() {
    globalThis.fetch = (async (url, init) => this.respond(init?.method ?? "GET", String(url), typeof init?.body === "string" ? Buffer.from(init.body) : Buffer.from(init?.body as Uint8Array ?? []), new Headers(init?.headers))) as typeof fetch;
  }
}
const init = (options: PapayaOptions = {}) => {
  const sdk = Papaya.init({ apiKey: "synthetic-key", endpoint: "https://synthetic.invalid/api/v1/ingest/traces", multipart: true, capture: "full", retryBaseMs: 15, retryJitter: 0, ...options }); instances.push(sdk); return sdk;
};
const capture = (sdk: Papaya, value: unknown = "synthetic-content-tail") => { const trace = sdk.startTrace({ traceId: "synthetic-trace" }, { inputValue: value }); sdk.finishTrace(trace, "success"); };
afterEach(async () => { for (const sdk of instances.splice(0)) await sdk.close(0); globalThis.fetch = originalFetch; });

for (const phase of ["create", "part", "complete"] as const) test(`ambiguous ${phase} acknowledgment resumes the same immutable upload`, async () => {
  const server = new PapayaMultipartFixtureServer(); server.lose = phase; server.install();
  const sdk = init(); capture(sdk);
  assert.equal((await sdk.flush()).status, "failed");
  assert.equal((await sdk.close(1_000)).status, "sent");
  assert.equal(server.createCount, 1); assert.equal(server.parts.size, 1); assert.equal(server.completeCount, 1);
  const creates = server.requests.filter(r => r.method === "POST" && r.path.endsWith("/trace-uploads"));
  assert.equal(new Set(creates.map(r => r.hash)).size, 1);
  if (phase !== "create") assert.ok(server.requests.some(r => r.method === "GET" && r.path.includes("/trace-uploads/")));
  assert.equal(server.requests.filter(r => r.method === "PUT").length, 1);
  assert.ok(server.landed?.includes("synthetic-content-tail"));
});

for (const state of ["assembling", "validating", "publishing"]) test(`retryable completion resumes ${state} with the exact manifest`, async () => {
  const server = new PapayaMultipartFixtureServer(); server.failCompletion = { status: 503, error: "synthetic_transient", state }; server.install();
  const sdk = init(); capture(sdk); await sdk.flush();
  assert.equal((await sdk.close(500)).status, "sent");
  const manifests = server.requests.filter(r => r.path.endsWith("/complete")); assert.equal(manifests.length, 2); assert.equal(manifests[0]!.hash, manifests[1]!.hash);
  assert.equal(server.requests.filter(r => r.method === "PUT").length, 1);
});

test("only the precise completion lease conflict is retryable", async () => {
  for (const error of ["trace_upload_operation_in_progress", "trace_upload_integrity_conflict"]) {
    const server = new PapayaMultipartFixtureServer(); server.failCompletion = { status: 409, error, state: "assembling" }; server.install();
    const sdk = init(); capture(sdk); await sdk.flush(); const result = await sdk.close(500);
    assert.equal(result.status, error === "trace_upload_operation_in_progress" ? "sent" : "failed");
    if (result.status === "failed") assert.equal(result.pending.traces, 1);
  }
});

test("unsupported capability, server total limit and invalid TTL never fall back or upload", async () => {
  for (const configure of [(s: PapayaMultipartFixtureServer) => { s.config.enabled = false; }, (s: PapayaMultipartFixtureServer) => { s.config.maxBytes = 10; }, (s: PapayaMultipartFixtureServer) => { s.config.minIdleTtlSeconds = 120; }]) {
    const server = new PapayaMultipartFixtureServer(); configure(server); server.install();
    const sdk = init({ multipartIdleTtlSeconds: 60 }); capture(sdk);
    const result = await sdk.flush(); assert.equal(result.status, "failed"); assert.equal(result.pending.traces, 1); assert.equal(server.createCount, 0);
    assert.ok(server.requests.every(r => r.path.endsWith("/config")));
  }
});

test("receipt mismatch retains frozen content while a valid post-policy hash may differ", async () => {
  for (const invalid of [true, false]) {
    const server = new PapayaMultipartFixtureServer(); server.invalidReceipt = invalid; server.install();
    const sdk = init(); capture(sdk); const result = await sdk.flush();
    assert.equal(result.status, invalid ? "failed" : "sent");
    if (invalid) assert.equal(result.errorCode, "multipart_invalid_landing_receipt");
    else assert.notEqual(result.targetReceipts[0]!.multipart?.sha256, result.targetReceipts[0]!.multipart?.rawSha256);
  }
});

test("conflicting part, failed, expired and aborted sessions cannot acknowledge or create replacements", async () => {
  for (const state of ["failed", "expired", "aborted", "part-conflict"]) {
    const server = new PapayaMultipartFixtureServer(); server.lose = "part"; server.install();
    const sdk = init(); capture(sdk); await sdk.flush();
    if (state === "part-conflict") server.conflictPart = true; else server.session!.state = state;
    const result = await sdk.close(500); assert.equal(result.status, "failed"); assert.equal(result.pending.traces, 1); assert.equal(server.createCount, 1); assert.equal(server.completeCount, 0);
  }
});

test("reconfiguration cannot move an ambiguous session to another key or destination", async () => {
  const server = new PapayaMultipartFixtureServer(); server.lose = "create"; server.install();
  const sdk = init(); capture(sdk); await sdk.flush(); const requests = server.requests.length;
  sdk.configureExport({ apiKey: "different-synthetic-key" });
  const result = await sdk.close(500); assert.equal(result.errorCode, "multipart_session_configuration_changed"); assert.equal(server.requests.length, requests); assert.equal(result.pending.traces, 1);
});

test("close stops subsequent parts while retaining an uninterruptible current request", async () => {
  const server = new PapayaMultipartFixtureServer(); let finish!: (r: Response) => void;
  globalThis.fetch = (async (url, options) => {
    if (options?.method === "PUT") return new Promise(resolve => { finish = resolve; });
    return server.respond(options?.method ?? "GET", String(url), Buffer.from(String(options?.body ?? "")), new Headers(options?.headers));
  }) as typeof fetch;
  const sdk = init(); capture(sdk); const result = await sdk.close(20); assert.equal(result.sending.traces, 1);
  const part = { partNumber: 1, byteLength: server.session!.byteLength, sha256: server.session!.sha256 };
  finish(new Response(JSON.stringify(part))); await delay(10); assert.equal(server.completeCount, 0); assert.equal(sdk.exportStatus().pending.traces, 1);
});

test("native HTTP preserves nine MiB of media/history and recovers lost part/completion replies", async () => {
  globalThis.fetch = originalFetch;
  const fixture = new PapayaMultipartFixtureServer(); fixture.lose = "part";
  const http = createServer(async (req, res) => {
    const chunks: Buffer[] = []; for await (const chunk of req) chunks.push(Buffer.from(chunk));
    try {
      const response = fixture.respond(req.method!, `http://127.0.0.1${req.url}`, Buffer.concat(chunks), new Headers(req.headers as Record<string, string>));
      if (req.url?.endsWith("/complete") && fixture.completeCount === 1) { res.destroy(); return; }
      res.writeHead(response.status, { "Content-Type": "application/json" }); res.end(await response.text());
    } catch { res.destroy(); }
  });
  await new Promise<void>(resolve => http.listen(0, "127.0.0.1", resolve));
  const address = http.address(); assert.ok(address && typeof address !== "string");
  try {
    const sdk = init({ endpoint: `http://127.0.0.1:${address.port}/api/v1/ingest/traces` });
    const media = { type: "image_url", image_url: { url: "data:image/png;base64," + "A".repeat(9 * 1024 ** 2) }, history: [{ role: "user", content: "synthetic history" }], marker: "synthetic-complete-tail" };
    capture(sdk, media); await sdk.flush(5_000);
    const result = await sdk.close(5_000); assert.equal(result.status, "sent"); assert.equal(fixture.parts.size, 2); assert.equal(fixture.completeCount, 1); assert.equal(fixture.createCount, 1);
    const landed = JSON.parse(fixture.landed!.toString()); assert.deepEqual(landed.traces[0].spans[0].inputPayload.value, media);
  } finally { http.closeAllConnections(); await new Promise<void>(resolve => http.close(() => resolve())); }
});

test("native request deadline aborts continuously trickled headers", async () => {
  globalThis.fetch = originalFetch;
  const timers = new Set<ReturnType<typeof setInterval>>();
  const http = createServer((req, res) => {
    const socket = res.socket!;
    socket.write("HTTP/1.1 200 OK\r\nX-Synthetic: ");
    const timer = setInterval(() => { if (!socket.destroyed) socket.write("x"); }, 2);
    timers.add(timer); req.on("close", () => { clearInterval(timer); timers.delete(timer); });
  });
  await new Promise<void>(resolve => http.listen(0, "127.0.0.1", resolve));
  const address = http.address(); assert.ok(address && typeof address !== "string");
  try {
    const sdk = init({ endpoint: `http://127.0.0.1:${address.port}/v1/ingest/traces`, requestTimeoutMs: 30, maxAttempts: 1 });
    capture(sdk); const began = performance.now(); const result = await sdk.flush(500);
    assert.equal(result.errorCode, "request_timeout"); assert.equal(result.pending.traces, 1);
    assert.equal(result.targetReceipts[0]!.deadlineExceeded, true); assert.ok(performance.now() - began < 500);
  } finally {
    for (const timer of timers) clearInterval(timer);
    http.closeAllConnections(); await new Promise<void>(resolve => http.close(() => resolve()));
  }
});
