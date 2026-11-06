export type PapayaProviderUsage = Partial<Record<"inputTokens" | "outputTokens" | "totalTokens" | "cacheReadInputTokens" | "cacheCreationInputTokens" | "reasoningTokens" | "costUsd", number>> & { pricingSource?: "provider" };
const usageKeys = ["usage", "token_usage", "usage_metadata", "usageMetadata", "tokenUsage"];
const wrappers = ["body", "response", "response_metadata", "responseMetadata", "result", "data", "llm_output", "llmOutput", "metadata"];
const fieldKeys = {
  inputTokens: ["inputTokens", "input_tokens", "prompt_tokens", "promptTokenCount", "prompt_token_count"],
  outputTokens: ["outputTokens", "output_tokens", "completion_tokens", "candidatesTokenCount", "candidates_token_count"],
  totalTokens: ["totalTokens", "total_tokens", "totalTokenCount", "total_token_count"],
  cacheReadInputTokens: ["cacheReadInputTokens", "cache_read_input_tokens", "cached_input_tokens", "cachedContentTokenCount", "cached_content_token_count"],
  cacheCreationInputTokens: ["cacheCreationInputTokens", "cache_creation_input_tokens"],
  reasoningTokens: ["reasoningTokens", "reasoning_tokens", "thoughtsTokenCount", "thoughts_token_count"],
  costUsd: ["costUsd", "cost_usd"],
} as const;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const get = (v: unknown, key: string, diagnostic: (code: string) => void): unknown => {
  try { return record(v) ? v[key] : undefined; } catch { diagnostic("usage_record_unreadable"); return undefined; }
};
const number = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0;

/** Recover one provider call, never sum repeated observations or traverse customer content. */
export function papayaUsageFromResult(value: unknown, diagnostic: (code: string) => void = () => {}): PapayaProviderUsage | undefined {
  const fields: PapayaProviderUsage = {}, ids = new Map<string, string>(), visited = new Set<object>();
  const queue: Array<{ value: unknown; depth: number; path: Set<object>; scoped: boolean }> = [{ value, depth: 0, path: new Set(), scoped: false }];
  const read = (v: unknown, keys: readonly string[]) => {
    for (const key of keys) { const n = get(v, key, diagnostic); if (number(n)) return n; }
  };
  const merge = (v: unknown, scoped: boolean) => {
    const next: PapayaProviderUsage = {};
    for (const field of Object.keys(fieldKeys) as Array<keyof typeof fieldKeys>) next[field] = read(v, fieldKeys[field]);
    if (scoped && next.costUsd === undefined) next.costUsd = read(v, ["cost", "total_cost", "totalCost"]);
    for (const [field, containers, keys] of [
      ["cacheReadInputTokens", ["input_tokens_details", "prompt_tokens_details"], ["cached_tokens"]],
      ["cacheReadInputTokens", ["input_token_details", "inputTokenDetails"], ["cache_read", "cacheRead"]],
      ["cacheCreationInputTokens", ["input_token_details", "inputTokenDetails"], ["cache_creation", "cacheCreation"]],
      ["reasoningTokens", ["output_tokens_details", "completion_tokens_details"], ["reasoning_tokens"]],
      ["reasoningTokens", ["output_token_details", "outputTokenDetails"], ["reasoning"]],
    ] as const) for (const container of containers) if (next[field] === undefined) next[field] = read(get(v, container, diagnostic), keys);
    for (const field of Object.keys(fieldKeys) as Array<keyof typeof fieldKeys>) if (fields[field] === undefined && next[field] !== undefined) fields[field] = next[field];
  };
  while (queue.length) {
    const { value: v, depth, path, scoped } = queue.shift()!;
    if (!record(v)) continue;
    if (path.has(v)) { diagnostic("usage_traversal_cycle_detected"); continue; }
    if (visited.has(v)) continue;
    if (depth > 16) { diagnostic("usage_traversal_depth_exceeded"); continue; }
    if (visited.size >= 256) { diagnostic("usage_traversal_nodes_exceeded"); break; }
    visited.add(v);
    if (["spans", "traces", "children", "calls"].some(k => get(v, k, diagnostic) != null)) continue;
    const provider = [...usageKeys, "model", "modelVersion", "object", "type"].some(k => get(v, k, diagnostic) != null);
    const identityKeys = [["request", "request_id"], ["request", "requestId"], ["response", "response_id"], ["response", "responseId"], ...(provider ? [["response", "id"]] : [])];
    const candidates = new Map(ids); let conflict = false;
    for (const [kind, key] of identityKeys) { const id = get(v, key!, diagnostic); if (typeof id === "string" && id) {
      if (candidates.has(kind!) && candidates.get(kind!) !== id) { conflict = true; break; } candidates.set(kind!, id);
    } }
    if (conflict) { diagnostic("usage_scope_conflict"); continue; }
    for (const [kind, id] of candidates) ids.set(kind, id);
    for (const key of usageKeys) merge(get(v, key, diagnostic), true);
    merge(v, scoped || provider);
    for (const key of [...usageKeys, ...wrappers]) {
      const child = get(v, key, diagnostic);
      if (record(child)) queue.push({ value: child, depth: depth + 1, path: new Set([...path, v]), scoped: true });
    }
  }
  if (!Object.values(fields).some(v => v !== undefined)) return;
  if (fields.totalTokens === undefined && (fields.inputTokens !== undefined || fields.outputTokens !== undefined)) {
    const total = (fields.inputTokens ?? 0) + (fields.outputTokens ?? 0); if (number(total)) fields.totalTokens = total;
  }
  if (fields.costUsd !== undefined) fields.pricingSource = "provider";
  return fields;
}

export function papayaModelFromResult(value: unknown): string | undefined {
  const seen = new Set<object>(), pending: Array<[unknown, number]> = [[value, 0]];
  while (pending.length && seen.size < 256) {
    const [v, depth] = pending.shift()!;
    if (!record(v) || seen.has(v) || depth > 16) continue;
    seen.add(v);
    for (const key of ["model", "model_name", "modelName", "model_id", "modelId", "model_version", "modelVersion"]) {
      const label = get(v, key, () => {}); if (typeof label === "string" && label) return label;
    }
    for (const key of wrappers) pending.push([get(v, key, () => {}), depth + 1]);
  }
}
