import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";
import { redactPapayaSdkEmailAddresses } from "../src/redaction.js";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/redaction-regression.json", import.meta.url), "utf8")) as {
  cases: string[]; scalingBytes: number[];
};
const previous = (text: string): string => text.replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted-email]");

test("email masking preserves the previous matching boundaries", () => {
  for (const text of fixture.cases) assert.equal(redactPapayaSdkEmailAddresses(text), previous(text), text);
  let seed = 173;
  const alphabet = "abcXYZ09._%+-@ /\né";
  for (let n = 0; n < 20_000; n++) {
    let text = "";
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const length = seed % 128;
    for (let i = 0; i < length; i++) {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      text += alphabet[seed % alphabet.length];
    }
    assert.equal(redactPapayaSdkEmailAddresses(text), previous(text), `synthetic case ${n}`);
  }
});

test("large no-at and invalid-domain payloads finish within a bounded subprocess", () => {
  const moduleUrl = new URL("../src/redaction.ts", import.meta.url).href;
  const source = `import assert from 'node:assert/strict';
    import {redactPapayaSdkEmailAddresses as redact} from ${JSON.stringify(moduleUrl)};
    for (const size of ${JSON.stringify(fixture.scalingBytes)}) {
      const plain='a'.repeat(size); assert.equal(redact(plain),plain);
      const invalid=plain+'@'+'b'.repeat(size); assert.equal(redact(invalid),invalid);
      assert.equal(redact(plain+'@example.test'),'[redacted-email]');
    }`;
  execFileSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", source], {
    timeout: 8_000, stdio: "pipe",
  });
});
