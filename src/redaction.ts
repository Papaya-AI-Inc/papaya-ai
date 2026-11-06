/** Preserve the SDK email pattern while visiting each local-part run once. */
export const redactPapayaSdkEmailAddresses = (value: string): string => {
  if (!value.includes("@")) return value;
  const localRuns = /[A-Z0-9._%+-]+/gi;
  const domain = /[A-Z0-9.-]+\.[A-Z]{2,}/iy;
  const parts: string[] = [];
  let consumed = 0;
  for (let local = localRuns.exec(value); local; local = localRuns.exec(value)) {
    const end = local.index + local[0].length;
    const start = Math.max(local.index, consumed);
    if (start >= end || value[end] !== "@") continue;
    domain.lastIndex = end + 1;
    const match = domain.exec(value);
    if (!match) continue;
    parts.push(value.slice(consumed, start), "[redacted-email]");
    consumed = domain.lastIndex;
  }
  if (parts.length === 0) return value;
  parts.push(value.slice(consumed));
  return parts.join("");
};
