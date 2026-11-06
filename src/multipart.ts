import { createHash } from "node:crypto";
import { PapayaExportError, type PapayaOwnedBatch, type PapayaRequest, type PapayaRequestResult } from "./exporter.js";

export type PapayaMultipartState = {
  binding?: string; uploadId?: string; sha256?: string; partCount?: number; partSize?: number;
  rawSha256?: string; rawBytes?: number;
};
type PapayaMultipartPart = { partNumber: number; byteLength: number; sha256: string };
const digest = (value: string | Buffer): string => createHash("sha256").update(value).digest("hex");
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const fail = (code: string, status = 400): never => { throw new PapayaExportError(`multipart_${code}`, status); };

/** Full bytes, immutable identities, serial parts and explicit landed receipts; no new client dependencies. */
export async function sendPapayaMultipart(item: PapayaOwnedBatch, endpoint: string, headers: Record<string, string>, request: PapayaRequest, idleTtlSeconds?: number): Promise<PapayaRequestResult> {
  let parsed: URL;
  try { parsed = new URL(endpoint); } catch { return fail("invalid_endpoint"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash || !parsed.pathname.endsWith("/traces")) fail("invalid_endpoint");
  const base = endpoint.slice(0, -"/traces".length), state = item.multipartState;
  const binding = digest(JSON.stringify([endpoint, headers.Authorization]));
  if (state.binding && state.binding !== binding) fail("session_configuration_changed", 409);
  // Bind before session creation: an ambiguous create may already own a server session.
  state.binding = binding;
  const json = async (method: string, url: string, body?: string | Buffer, extra?: Record<string, string>): Promise<Record<string, unknown>> => {
    const response = await request(method, url, { ...headers, ...extra }, body);
    if (response.status < 200 || response.status >= 300) {
      let error: unknown;
      try { error = JSON.parse(response.text); } catch { /* fixed protocol code only */ }
      const busy = response.status === 409 && method === "POST" && url.endsWith("/complete") && object(error) && error.error === "trace_upload_operation_in_progress";
      throw new PapayaExportError(busy ? "multipart_operation_in_progress" : "multipart_http_error", busy ? 503 : response.status, response.headers);
    }
    let value: unknown;
    try { value = JSON.parse(response.text); } catch { return fail("invalid_response"); }
    if (!object(value)) fail("invalid_response");
    return value as Record<string, unknown>;
  };
  let config: Record<string, unknown>;
  try { config = await json("GET", `${base}/config`); } catch (error) {
    if (error instanceof PapayaExportError && [404, 405].includes(error.status)) fail("unsupported", error.status);
    throw error;
  }
  const capability = object(config.features) ? config.features.multipart : undefined;
  if (!object(capability) || capability.enabled !== true) fail("unsupported");
  const cap = capability as Record<string, unknown>;
  for (const [key, max] of Object.entries({ maxBytes: 32 * 1024 ** 2, partSizeBytes: 5 * 1024 ** 2, maxParts: 7, minIdleTtlSeconds: 3600, maxIdleTtlSeconds: 3600 })) {
    if (!Number.isInteger(cap[key]) || Number(cap[key]) <= 0 || Number(cap[key]) > max) fail("invalid_capability");
  }
  if (Number(cap.minIdleTtlSeconds) > Number(cap.maxIdleTtlSeconds)) fail("invalid_capability");
  if (idleTtlSeconds !== undefined && (idleTtlSeconds < Number(cap.minIdleTtlSeconds) || idleTtlSeconds > Number(cap.maxIdleTtlSeconds))) fail("idle_ttl_out_of_range");
  const bytes = Buffer.from(item.body), partSize = Number(cap.partSizeBytes), partCount = Math.ceil(bytes.length / partSize);
  if (bytes.length > Number(cap.maxBytes) || partCount > Number(cap.maxParts)) fail("max_bytes_exceeded", 413);
  const sha256 = digest(bytes);
  if ((state.sha256 && state.sha256 !== sha256) || (state.partSize && state.partSize !== partSize)) fail("session_mismatch", 409);
  Object.assign(state, { sha256, partSize, partCount });
  const parts: PapayaMultipartPart[] = Array.from({ length: partCount }, (_, i) => {
    const chunk = bytes.subarray(i * partSize, (i + 1) * partSize);
    return { partNumber: i + 1, byteLength: chunk.length, sha256: digest(chunk) };
  });
  const status = (value: Record<string, unknown>): Set<number> => {
    if (typeof value.uploadId !== "string" || !value.uploadId || value.uploadId.length > 256 || value.batchId !== item.batchId || value.byteLength !== bytes.length || value.sha256 !== sha256 || value.partSizeBytes !== partSize || (state.uploadId && state.uploadId !== value.uploadId)) fail("session_mismatch", 409);
    state.uploadId = value.uploadId as string;
    if (["failed", "expired", "aborted"].includes(String(value.state))) fail(String(value.state), 410);
    if (!["uploading", "assembling", "validating", "publishing", "landed"].includes(String(value.state))) fail("invalid_state");
    if (!Array.isArray(value.parts) || value.parts.length > parts.length) fail("invalid_parts");
    const verified = new Set<number>();
    for (const p of value.parts as unknown[]) {
      if (!object(p) || !Number.isInteger(p.partNumber)) fail("invalid_parts");
      const part = p as Record<string, unknown>, n = Number(part.partNumber), expected = parts[n - 1];
      if (!expected || verified.has(n) || part.byteLength !== expected.byteLength || part.sha256 !== expected.sha256) fail("part_conflict", 409);
      verified.add(n);
    }
    return verified;
  };
  const landed = (value: Record<string, unknown>): PapayaRequestResult | undefined => {
    if (value.state !== "landed") return;
    const r = value.receipt;
    if (!object(r) || r.uploadId !== state.uploadId || r.batchId !== item.batchId || r.accepted !== item.traceIds.length || r.rejected !== 0 || !Array.isArray(r.warnings) || typeof r.rawSha256 !== "string" || !/^[0-9a-f]{64}$/.test(r.rawSha256) || !Number.isInteger(r.rawBytes) || Number(r.rawBytes) <= 0 || typeof r.landedAt !== "string" || !r.landedAt) fail("invalid_landing_receipt");
    const receipt = r as Record<string, unknown>;
    state.rawSha256 = receipt.rawSha256 as string; state.rawBytes = Number(receipt.rawBytes);
    return { status: 202, text: JSON.stringify({ accepted: item.traceIds.length, rejected: 0 }), headers: new Headers() };
  };
  let value = state.uploadId ? await json("GET", `${base}/trace-uploads/${encodeURIComponent(state.uploadId)}`)
    : await json("POST", `${base}/trace-uploads`, JSON.stringify({ schemaVersion: "2026-06-05", batchId: item.batchId, byteLength: bytes.length, sha256, ...(idleTtlSeconds === undefined ? {} : { idleTtlSeconds }) }));
  const verified = status(value), found = landed(value);
  if (found) return found;
  if (value.state !== "uploading" && verified.size !== parts.length) fail("operation_in_progress", 503);
  const url = `${base}/trace-uploads/${encodeURIComponent(state.uploadId!)}`;
  for (const part of parts) {
    if (verified.has(part.partNumber)) continue;
    const offset = (part.partNumber - 1) * partSize;
    const reply = await json("PUT", `${url}/parts/${part.partNumber}`, bytes.subarray(offset, offset + part.byteLength), { "Content-Type": "application/octet-stream", "X-Papaya-Part-Sha256": part.sha256 });
    if ("state" in reply) { if (!status(reply).has(part.partNumber)) fail("part_not_acknowledged"); }
    else if (reply.partNumber !== part.partNumber || reply.byteLength !== part.byteLength || reply.sha256 !== part.sha256) fail("invalid_part_receipt");
  }
  value = await json("POST", `${url}/complete`, JSON.stringify({ parts }));
  if (!("state" in value) && "landedAt" in value) value = { state: "landed", receipt: value };
  else status(value);
  return landed(value) ?? fail("operation_in_progress", 503);
}
