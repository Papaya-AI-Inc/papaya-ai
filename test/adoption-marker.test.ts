import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { APPLIED_RECOMMENDATION_WINDOW, buildPapayaControlBag, Papaya, type PapayaOptions } from "../src/index.js";

type ParityCase = {
  name: string;
  input: string[];
  expected: { appliedRecommendations: string[] } | null;
};

const fixture = JSON.parse(
  readFileSync(fileURLToPath(new URL("./adoption-marker-parity.json", import.meta.url)), "utf8"),
) as { window: number; cases: ParityCase[] };

// The Python SDK reads this same fixture. If either implementation drifts, one
// of the two suites fails — which matters, because a silent divergence would
// drop customer markers on one runtime only.
for (const testCase of fixture.cases) {
  const actual = buildPapayaControlBag(testCase.input) ?? null;
  assert.deepEqual(actual, testCase.expected, `parity case: ${testCase.name}`);
}

assert.equal(
  APPLIED_RECOMMENDATION_WINDOW,
  fixture.window,
  "the SDK window must match the shared fixture, and the server's MAX_ADOPTION_MARKERS_PER_BATCH",
);

// The window keeps the NEWEST markers. A customer config grows for the life of
// the application; the payload must not.
const many = Array.from({ length: APPLIED_RECOMMENDATION_WINDOW + 5 }, (_unused, index) =>
  `agfind-${1756089600000 + index}-abcdefab`);
const windowed = buildPapayaControlBag(many);
assert.equal(windowed?.appliedRecommendations.length, APPLIED_RECOMMENDATION_WINDOW);
assert.deepEqual(windowed?.appliedRecommendations, many.slice(-APPLIED_RECOMMENDATION_WINDOW));
assert.equal(windowed?.appliedRecommendations.includes(many[0]), false);

// buildPapayaControlBag must not mutate the caller's array: it is the customer's own
// config object, and reversing it in place would corrupt their state.
const original = ["agfind-1-aaaa", "agfind-2-bbbb", "agfind-1-aaaa"];
const snapshot = [...original];
buildPapayaControlBag(original);
assert.deepEqual(original, snapshot, "buildPapayaControlBag must not mutate the caller's list");

// Nothing to send means the key is omitted entirely — never null, never {}.
assert.equal(buildPapayaControlBag(undefined), undefined);
assert.equal(buildPapayaControlBag([]), undefined);
assert.equal(buildPapayaControlBag(["junk"]), undefined);

// End to end through the real client: the bag must ride on the BATCH envelope,
// and must never leak into trace.metadata, which is customer content and is
// redacted server-side.
const captured: Array<Record<string, unknown>> = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
  captured.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
  return new Response(JSON.stringify({ accepted: 1, rejected: 0 }), {
    status: 202,
    headers: { "content-type": "application/json" },
  });
}) as typeof fetch;

const exportOne = async (options: PapayaOptions): Promise<Record<string, unknown>> => {
  captured.length = 0;
  const client = Papaya.init(options);
  const trace = client.startTrace({ workflowKey: "checkout" });
  client.finishTrace(trace, "success");
  await client.flush();
  assert.equal(captured.length, 1, "expected exactly one exported batch");
  return captured[0]!;
};

try {
  const marker = "agfind-1756089600000-aaaaaaaa";
  const batch = await exportOne({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
    metadata: { plan: "standard" },
    appliedRecommendations: [marker],
  }) as { papaya?: unknown; traces?: Array<{ metadata?: unknown }> };

  assert.deepEqual(batch.papaya, { appliedRecommendations: [marker] });
  // The customer's own run metadata must still be exported untouched — the bag
  // is an addition, not a replacement.
  assert.deepEqual(batch.traces?.[0]?.metadata, { plan: "standard" });
  assert.equal(
    JSON.stringify(batch.traces ?? []).includes(marker),
    false,
    "adoption markers must never appear anywhere inside a trace",
  );

  // An SDK configured without the option must produce a batch indistinguishable
  // from one built before this feature existed: the key absent, not null.
  const legacyBatch = await exportOne({
    apiKey: "papaya-test-token",
    endpoint: "https://papaya.example/api/v1/ingest/traces",
  });
  assert.equal("papaya" in legacyBatch, false, "the papaya key must be omitted, not null");
} finally {
  globalThis.fetch = originalFetch;
}

console.log("adoption marker tests passed");
