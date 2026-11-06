import { createHash } from "node:crypto";
import { sendPapayaMultipart, type PapayaMultipartState } from "./multipart.js";

export type PapayaExporterOptions = {
  maxPendingBatches?: number;
  maxPendingBytes?: number;
  maxActiveTraces?: number;
  maxActiveBytes?: number;
  maxTraceBytes?: number;
  maxRetryAgeMs?: number;
  maxAttempts?: number;
  retryBaseMs?: number;
  retryMaxMs?: number;
  retryJitter?: number;
  requestTimeoutMs?: number;
  flushTimeoutMs?: number;
  diagnosticLimit?: number;
  /** Explicit protocol choice; never falls back to the legacy JSON endpoint. */
  multipart?: boolean;
  multipartIdleTtlSeconds?: number;
};
export type PapayaExportState = "queued" | "retrying" | "sending" | "terminalFailed" | "sent";
export type PapayaExportCount = { batches: number; traces: number; bytes: number };
export type PapayaExportReceipt = {
  batchId: string; traceIds: string[]; traceCount: number; traceIdsOmitted: number;
  bodyBytes: number; attempts: number; state: PapayaExportState; code: string;
  deadlineExceeded: boolean; httpStatus?: number; accepted?: number; rejected?: number;
  multipart?: { uploadId?: string; sha256?: string; partCount?: number; rawSha256?: string; rawBytes?: number };
};
export type PapayaExportSnapshot = {
  admitted: PapayaExportCount; sent: PapayaExportCount; rejected: PapayaExportCount; captureFailed: PapayaExportCount;
  queued: PapayaExportCount; retrying: PapayaExportCount; sending: PapayaExportCount; terminalFailed: PapayaExportCount;
  pending: PapayaExportCount; receipts: PapayaExportReceipt[];
  diagnostics: { code: string; traces: number; bytes: number }[];
  highWater: { batches: number; bytes: number; activeTraces: number; activeBytes: number };
  authPaused: boolean; closed: boolean; workerFailed: boolean;
};
export type PapayaExportResult = PapayaExportSnapshot & {
  status: "sent" | "failed" | "skipped"; traceCount: number; endpoint: string;
  reason?: "empty" | "missing_api_key"; httpStatus?: number;
  errorCode?: string; responseText?: string; timedOut: boolean; targetReceipts: PapayaExportReceipt[];
};
export class PapayaCaptureError extends Error {
  constructor(public readonly code: string) { super(code); this.name = "PapayaCaptureError"; }
}
export class PapayaExportError extends Error {
  constructor(public readonly code: string, public readonly status = 400, public readonly headers?: Headers) {
    super(code); this.name = "PapayaExportError";
  }
}
export type PapayaFrozenBatch = { batchId: string; traceIds: string[]; body: string };
export type PapayaOwnedBatch = PapayaFrozenBatch & {
  bytes: number; enqueuedAt: number; nextAttempt: number; attempts: number; completedAttempts: number;
  state: PapayaExportState; code: string; deadlineExceeded: boolean; httpStatus?: number;
  accepted?: number; rejected?: number; responseText?: string; multipartState: PapayaMultipartState;
};
export type PapayaRequestResult = { status: number; text: string; headers: Headers };
export type PapayaRequest = (method: string, url: string, headers: Record<string, string>, body?: string | Buffer) => Promise<PapayaRequestResult>;
const empty = (): PapayaExportCount => ({ batches: 0, traces: 0, bytes: 0 });
const count = (items: PapayaOwnedBatch[]): PapayaExportCount => ({ batches: items.length,
  traces: items.reduce((n, b) => n + b.traceIds.length, 0), bytes: items.reduce((n, b) => n + b.bytes, 0) });
const safeId = (id: string): string => id.length <= 256 ? id : `sha256:${createHash("sha256").update(id).digest("hex")}`;
const receipt = (b: PapayaOwnedBatch): PapayaExportReceipt => ({ batchId: safeId(b.batchId),
  traceIds: b.traceIds.slice(0, 64).map(safeId), traceCount: b.traceIds.length,
  traceIdsOmitted: Math.max(0, b.traceIds.length - 64), bodyBytes: b.bytes, attempts: b.attempts,
  state: b.state, code: b.code, deadlineExceeded: b.deadlineExceeded, httpStatus: b.httpStatus,
  accepted: b.accepted, rejected: b.rejected, ...(b.multipartState.sha256 ? { multipart: {
    uploadId: b.multipartState.uploadId, sha256: b.multipartState.sha256, partCount: b.multipartState.partCount,
    rawSha256: b.multipartState.rawSha256, rawBytes: b.multipartState.rawBytes } } : {}) });

export function papayaExporterOptions(options: PapayaExporterOptions): Required<Omit<PapayaExporterOptions, "multipartIdleTtlSeconds">> & Pick<PapayaExporterOptions, "multipartIdleTtlSeconds"> {
  const result = { maxPendingBatches: 64, maxPendingBytes: 64 * 1024 ** 2, maxActiveTraces: 128,
    maxActiveBytes: (options.multipart ? 64 : 16) * 1024 ** 2, maxTraceBytes: (options.multipart ? 32 : 8) * 1024 ** 2,
    maxRetryAgeMs: 300_000, maxAttempts: 8, retryBaseMs: 250, retryMaxMs: 30_000, retryJitter: .2,
    requestTimeoutMs: 10_000, flushTimeoutMs: 10_000, diagnosticLimit: 128, multipart: false,
    ...Object.fromEntries(Object.entries(options).filter(([, v]) => v !== undefined)),
  } as Required<Omit<PapayaExporterOptions, "multipartIdleTtlSeconds">> & Pick<PapayaExporterOptions, "multipartIdleTtlSeconds">;
  for (const key of ["maxPendingBatches", "maxPendingBytes", "maxActiveTraces", "maxActiveBytes", "maxTraceBytes", "maxAttempts", "diagnosticLimit"] as const) {
    if (!Number.isSafeInteger(result[key]) || result[key] <= 0) throw new RangeError(`${key} must be a positive integer`);
  }
  for (const key of ["maxRetryAgeMs", "retryBaseMs", "retryMaxMs", "retryJitter", "requestTimeoutMs", "flushTimeoutMs"] as const) {
    if (!Number.isFinite(result[key]) || result[key] < 0) throw new RangeError(`${key} must be finite and nonnegative`);
  }
  if (!result.maxRetryAgeMs || !result.requestTimeoutMs || result.retryJitter > 1) throw new RangeError("Invalid retry age, request timeout or jitter");
  if (typeof result.multipart !== "boolean") throw new TypeError("multipart must be a boolean");
  if (result.multipartIdleTtlSeconds !== undefined && (!result.multipart || !Number.isInteger(result.multipartIdleTtlSeconds) || result.multipartIdleTtlSeconds < 60 || result.multipartIdleTtlSeconds > 3600)) throw new RangeError("multipartIdleTtlSeconds requires multipart and an integer from 60 to 3600");
  return result;
}

/** Process-local ownership. Wait deadlines never release a still-owned request or payload. */
export class PapayaExporter {
  readonly limits: ReturnType<typeof papayaExporterOptions>;
  private batches: PapayaOwnedBatch[] = [];
  private totals = { admitted: empty(), sent: empty(), rejected: empty(), captureFailed: empty() };
  private receipts: PapayaExportReceipt[] = [];
  private diagnostics: PapayaExportSnapshot["diagnostics"] = [];
  private highWater = { batches: 0, bytes: 0, activeTraces: 0, activeBytes: 0 };
  private timer?: ReturnType<typeof setTimeout>;
  private running = false;
  private closed = false;
  private authPaused = false;
  private workerFailed = false;
  private waiters = new Set<() => void>();
  private splitParents = new Map<PapayaOwnedBatch, PapayaOwnedBatch[]>();
  constructor(private apiKey: string | undefined, private endpoint: string, options: PapayaExporterOptions,
    private split: (batch: PapayaFrozenBatch) => PapayaFrozenBatch[], private userAgent: string) {
    this.limits = papayaExporterOptions(options);
  }
  private add(key: keyof PapayaExporter["totals"], b: PapayaFrozenBatch, bytes = Buffer.byteLength(b.body)): void {
    const c = this.totals[key]; c.batches++; c.traces += b.traceIds.length; c.bytes += bytes;
  }
  record(code: string, bytes = 0, capture = false): void {
    const c = this.totals[capture ? "captureFailed" : "rejected"]; c.traces++; c.bytes += bytes;
    this.diagnostic(code, 1, bytes);
  }
  note(code: string): void { this.diagnostic(code, 0, 0); }
  private diagnostic(code: string, traces: number, bytes: number): void {
    this.diagnostics.push({ code, traces, bytes });
    if (this.diagnostics.length > this.limits.diagnosticLimit) this.diagnostics.shift();
  }
  updateActive(traces: number, bytes: number): void {
    this.highWater.activeTraces = Math.max(this.highWater.activeTraces, traces);
    this.highWater.activeBytes = Math.max(this.highWater.activeBytes, bytes);
  }
  admit(batch: PapayaFrozenBatch): boolean {
    const bytes = Buffer.byteLength(batch.body), pending = count(this.batches);
    const code = this.closed ? "exporter_closed" : this.workerFailed ? "worker_crash" : pending.batches >= this.limits.maxPendingBatches ? "pending_batches_capacity"
      : pending.bytes + bytes > this.limits.maxPendingBytes ? "pending_bytes_capacity" : undefined;
    if (code) { if (this.diagnostics.at(-1)?.code !== code) this.diagnostic(code, batch.traceIds.length, bytes); return false; }
    const now = performance.now();
    this.batches.push({ batchId: batch.batchId, traceIds: [...batch.traceIds], body: batch.body,
      bytes, enqueuedAt: now, nextAttempt: now, attempts: 0, completedAttempts: 0,
      state: "queued", code: "queued", deadlineExceeded: false, multipartState: {} });
    this.add("admitted", batch, bytes);
    this.highWater.batches = Math.max(this.highWater.batches, pending.batches + 1);
    this.highWater.bytes = Math.max(this.highWater.bytes, pending.bytes + bytes);
    this.schedule(); return true;
  }
  configure(apiKey: string | undefined, endpoint: string): void {
    if (this.closed) throw new PapayaCaptureError("exporter_closed");
    this.apiKey = apiKey; this.endpoint = endpoint; this.authPaused = false; this.schedule(); this.changed();
  }
  snapshot(): PapayaExportSnapshot {
    return structuredClone({ ...this.totals, ...Object.fromEntries(["queued", "retrying", "sending", "terminalFailed"].map(s => [s, count(this.batches.filter(b => b.state === s))])),
      pending: count(this.batches), receipts: this.receipts, diagnostics: this.diagnostics,
      highWater: this.highWater, authPaused: this.authPaused, closed: this.closed, workerFailed: this.workerFailed }) as PapayaExportSnapshot;
  }
  targets(): Array<{ batch: PapayaOwnedBatch; attempt: number }> {
    return this.batches.map(batch => ({ batch, attempt: batch.completedAttempts + 1 }));
  }
  result(targets: ReturnType<PapayaExporter["targets"]>, timedOut: boolean): PapayaExportResult {
    const failed = targets.filter(t => t.batch.state !== "sent");
    return { ...this.snapshot(), status: !targets.length || !this.apiKey ? "skipped" : failed.length ? "failed" : "sent",
      ...(targets.length ? (!this.apiKey ? { reason: "missing_api_key" as const } : {}) : { reason: "empty" as const }),
      traceCount: targets.reduce((n, t) => n + t.batch.traceIds.length, 0), endpoint: this.endpoint,
      httpStatus: targets.at(-1)?.batch.httpStatus, responseText: targets.at(-1)?.batch.responseText,
      errorCode: failed.length ? this.authPaused ? "auth_paused" : failed[0]!.batch.code : undefined,
      timedOut, targetReceipts: targets.map(t => receipt(t.batch)) };
  }
  wait(targets: ReturnType<PapayaExporter["targets"]>, timeout: number, drain = false, signal?: AbortSignal): Promise<PapayaExportResult> {
    if (!Number.isFinite(timeout) || timeout < 0) throw new RangeError("timeoutMs must be finite and nonnegative");
    const ready = () => this.authPaused || !this.apiKey || this.closed || targets.every(({ batch: b, attempt }) =>
      b.state === "sent" || b.state === "terminalFailed" || (!drain && b.code !== "split" && b.completedAttempts >= attempt));
    const cancelled = () => ({ ...this.result(targets, false), status: "failed" as const, errorCode: "flush_cancelled" });
    if (signal?.aborted) return Promise.resolve(cancelled());
    if (ready() || timeout === 0) return Promise.resolve(this.result(targets, !ready()));
    return new Promise(resolve => {
      const finish = () => { if (ready()) done(false); };
      const timer = setTimeout(() => done(!ready()), timeout);
      const abort = () => done(false, true);
      const done = (timedOut: boolean, aborted = false) => { clearTimeout(timer); this.waiters.delete(finish); signal?.removeEventListener("abort", abort); resolve(aborted ? cancelled() : this.result(targets, timedOut)); };
      signal?.addEventListener("abort", abort, { once: true });
      this.waiters.add(finish); this.schedule();
    });
  }
  stop(): void { this.closed = true; clearTimeout(this.timer); this.changed(); }
  private changed(): void {
    let updated: boolean;
    do {
      updated = false;
      for (const [parent, children] of this.splitParents) {
        if (children.every(b => b.state === "sent")) {
          parent.state = "sent"; parent.code = "sent";
          parent.httpStatus = children.at(-1)?.httpStatus; parent.responseText = children.at(-1)?.responseText;
        } else if (children.some(b => b.state === "terminalFailed")) {
          parent.state = "terminalFailed"; parent.code = "split_child_failed";
        } else continue;
        this.splitParents.delete(parent); updated = true;
      }
    } while (updated);
    for (const waiter of [...this.waiters]) waiter();
  }
  private terminal(b: PapayaOwnedBatch, code: string): void {
    b.state = "terminalFailed"; b.code = code; this.saveReceipt(b); this.diagnostic(code, b.traceIds.length, b.bytes);
  }
  private saveReceipt(b: PapayaOwnedBatch): void {
    this.receipts.push(receipt(b)); if (this.receipts.length > this.limits.diagnosticLimit) this.receipts.shift();
  }
  private schedule(): void {
    clearTimeout(this.timer);
    if (this.running || this.closed || this.workerFailed || !this.apiKey || this.authPaused) return;
    const now = performance.now();
    for (const b of this.batches) if ((b.state === "queued" || b.state === "retrying") && now - b.enqueuedAt >= this.limits.maxRetryAgeMs) this.terminal(b, "retry_exhausted");
    const live = this.batches.filter(b => b.state === "queued" || b.state === "retrying");
    if (!live.length) { this.changed(); return; }
    const due = live.find(b => b.nextAttempt <= now);
    if (due) { this.running = true; void this.send(due).catch(() => {
      this.workerFailed = true;
      for (const b of this.batches) if (b.state !== "terminalFailed") this.terminal(b, "worker_crash");
    }).finally(() => { this.running = false; this.changed(); this.schedule(); }); return; }
    const wake = Math.min(...live.map(b => Math.min(b.nextAttempt, b.enqueuedAt + this.limits.maxRetryAgeMs)));
    this.timer = setTimeout(() => this.schedule(), Math.max(1, wake - now)); this.timer.unref();
  }
  private async request(b: PapayaOwnedBatch, method: string, url: string, headers: Record<string, string>, body?: string | Buffer): Promise<PapayaRequestResult> {
    if (this.closed) throw new PapayaExportError("exporter_closed", 408);
    const controller = new AbortController(), deadline = performance.now() + this.limits.requestTimeoutMs;
    const timer = setTimeout(() => { b.deadlineExceeded = true; controller.abort(); }, this.limits.requestTimeoutMs);
    try {
      const response = await fetch(url, { method, headers, body: body as BodyInit | undefined, signal: controller.signal, redirect: "manual" });
      const chunks: Buffer[] = []; let size = 0;
      const reader = response.body?.getReader();
      if (reader) try { while (true) {
        const { done, value } = await reader.read(); if (done) break;
        size += value.byteLength;
        if (size > 65_536) { await reader.cancel(); throw new PapayaExportError("response_too_large"); }
        chunks.push(Buffer.from(value));
      } } finally { reader.releaseLock(); }
      if (performance.now() > deadline) b.deadlineExceeded = true;
      // Multipart late replies are ambiguous: retry reads the owned session status.
      if (b.deadlineExceeded && this.limits.multipart) throw new PapayaExportError("multipart_request_timeout", 408);
      return { status: response.status, text: Buffer.concat(chunks).toString("utf8"), headers: response.headers };
    } catch (error) {
      if (controller.signal.aborted) throw new PapayaExportError("request_timeout", 408);
      throw error;
    } finally { clearTimeout(timer); }
  }
  private async send(b: PapayaOwnedBatch): Promise<void> {
    b.state = "sending"; b.code = "sending"; b.attempts++; b.deadlineExceeded = false;
    const endpoint = this.endpoint, apiKey = this.apiKey!;
    const headers = { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json", "User-Agent": this.userAgent };
    let response: PapayaRequestResult | undefined, code: string | undefined;
    try {
      const request: PapayaRequest = (...args) => this.request(b, ...args);
      response = this.limits.multipart
        ? await sendPapayaMultipart(b, endpoint, headers, request, this.limits.multipartIdleTtlSeconds)
        : await request("POST", endpoint, headers, b.body);
    } catch (error) {
      code = error instanceof PapayaExportError ? error.code : "transport_exception";
      if (error instanceof PapayaExportError) response = { status: error.status, text: "", headers: error.headers ?? new Headers() };
    }
    b.completedAttempts = b.attempts; b.httpStatus = response?.status; b.responseText = response?.text;
    const status = response?.status;
    if (status !== undefined && status >= 200 && status < 300) {
      let ack: Record<string, unknown> | undefined;
      try { const v: unknown = JSON.parse(response!.text); if (v && typeof v === "object" && !Array.isArray(v)) ack = v as Record<string, unknown>; } catch { /* legacy plain success bodies are allowed */ }
      if (ack && ("accepted" in ack || "rejected" in ack)) {
        const valid = Number.isInteger(ack.accepted) && Number.isInteger(ack.rejected) && Number(ack.accepted) >= 0 && Number(ack.rejected) >= 0 && Number(ack.accepted) + Number(ack.rejected) === b.traceIds.length;
        if (valid) { b.accepted = Number(ack.accepted); b.rejected = Number(ack.rejected); }
        if (!valid || b.accepted !== b.traceIds.length || b.rejected !== 0) { this.terminal(b, valid ? "partial_acceptance" : "invalid_acknowledgment"); return; }
      }
      b.state = "sent"; b.code = b.deadlineExceeded ? "late_ack" : "sent";
      this.add("sent", b, b.bytes); this.saveReceipt(b); this.batches.splice(this.batches.indexOf(b), 1); return;
    }
    if (status === 401 || status === 403) { this.authPaused = true; b.state = "retrying"; b.code = "auth_paused"; b.nextAttempt = performance.now(); this.diagnostic(b.code, b.traceIds.length, b.bytes); return; }
    if (status === 413 && b.traceIds.length > 1 && !this.limits.multipart) {
      const children = this.split(b);
      const bytes = children.reduce((n, child) => n + Buffer.byteLength(child.body), 0);
      // Splitting is atomic: capacity failure preserves the original frozen payload.
      if (this.batches.length - 1 + children.length <= this.limits.maxPendingBatches && count(this.batches).bytes - b.bytes + bytes <= this.limits.maxPendingBytes) {
        this.batches.splice(this.batches.indexOf(b), 1);
        for (const child of children) this.admit(child);
        const replacements = this.batches.splice(this.batches.length - children.length, children.length);
        this.batches.unshift(...replacements);
        b.state = "retrying"; b.code = "split";
        // Payload ownership moved atomically to children; the parent keeps only its receipt metadata.
        b.body = "";
        // The parent remains a flush target until its replacement batches finish.
        this.splitParents.set(b, replacements); return;
      }
      this.terminal(b, "split_capacity"); return;
    }
    if (status !== undefined && ((status >= 300 && status < 400) || (status >= 400 && status < 500 && status !== 408 && status !== 429))) {
      this.terminal(b, code ?? (status === 413 ? "oversized_trace" : "http_terminal")); return;
    }
    if (b.attempts >= this.limits.maxAttempts || performance.now() - b.enqueuedAt >= this.limits.maxRetryAgeMs) { this.terminal(b, code === "request_timeout" ? code : "retry_exhausted"); return; }
    const retryAfter = response?.headers.get("Retry-After");
    const after = retryAfter === null || retryAfter === undefined ? 0 : Number.isFinite(Number(retryAfter)) ? Math.max(0, Number(retryAfter) * 1000) : Math.max(0, Date.parse(retryAfter) - Date.now()) || 0;
    const delay = Math.min(this.limits.retryMaxMs, this.limits.retryBaseMs * 2 ** Math.min(b.attempts - 1, 30)) * (1 + (Math.random() * 2 - 1) * this.limits.retryJitter);
    b.state = "retrying"; b.code = code ?? "retryable_http_error"; b.nextAttempt = performance.now() + Math.max(delay, after);
    this.batches.splice(this.batches.indexOf(b), 1); this.batches.push(b);
  }
}
