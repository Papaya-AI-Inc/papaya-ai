import asyncio
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import Papaya
from papaya_ai import core

FIXTURE = json.loads((Path(__file__).parent / "fixtures/redaction-regression.json").read_text())
LEGACY_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


class PapayaRedactionRegressionTest(unittest.TestCase):
    def test_existing_email_grammar_and_overlap_semantics(self):
        for case in FIXTURE["cases"]:
            with self.subTest(input=case["input"]):
                self.assertEqual(core._redact_string(case["input"]), case["expected"])

    def test_matches_legacy_email_behavior_on_bounded_synthetic_inputs(self):
        rng = random.Random(20261018)
        alphabet = "abXY09._%+-@ !/\nİıſK"
        for _ in range(3000):
            value = "".join(rng.choice(alphabet) for _ in range(rng.randrange(100)))
            # These short values carry no other redaction patterns.
            self.assertEqual(core._redact_string(value), LEGACY_EMAIL.sub("[redacted-email]", value))

    def test_long_runs_complete_without_truncating_payload(self):
        # A deadline catches the original multi-minute scan without a fragile
        # machine-specific benchmark ratio. This subprocess has no network calls.
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from papaya_ai.core import _payload
length = int(sys.argv[2])
for value in ("A" * length, "A" * length + "@invalid", "@" * length):
    payload = _payload(value, "redacted")
    assert payload["value"] == value
    assert payload["byteLength"] == len(value.encode("utf-8"))
print("complete")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(ROOT / "src"), str(FIXTURE["longRunCharacters"])],
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertEqual(completed.stdout.strip(), "complete")

    def test_nested_content_keeps_full_size_and_other_masks(self):
        media = "A" * 131072
        value = {"messages": [{"role": "user", "content": [
            {"type": "image", "data": media},
            {"type": "text", "text": "ada@example.test phone 212-555-1234 id 123-45-6789"},
        ]}], "api_key": "synthetic-value", "note": "Bearer synthetic-token"}
        captured = core._payload(value, "redacted")
        content = captured["value"]["messages"][0]["content"]
        self.assertEqual(content[0]["data"], media)
        self.assertEqual(content[1]["text"], "[redacted-email] phone [redacted-phone] id [redacted-ssn]")
        self.assertEqual(captured["value"]["api_key"], "[redacted-secret]")
        self.assertEqual(captured["value"]["note"], "Bearer [redacted-token]")
        self.assertEqual(value["messages"][0]["content"][1]["text"], "ada@example.test phone 212-555-1234 id 123-45-6789")

    def test_full_and_metadata_capture_keep_existing_contract(self):
        value = {"content": "ada@example.test", "media": "A" * 131072}
        self.assertEqual(core._payload(value, "full")["value"], value)
        metadata = core._payload(value, "metadata")
        self.assertNotIn("value", metadata)
        self.assertEqual(metadata["byteLength"], len(core._json_bytes(value)))

    def test_async_wrapper_preserves_provider_arguments_response_and_exception(self):
        papaya = Papaya.init(api_key="synthetic-local-only", capture="redacted", transport=lambda *_: (202, "{}"))
        messages = [{"role": "user", "content": "ada@example.test " + "A" * 131072}]
        result = {"message": "synthetic", "usage": {"input_tokens": 1, "output_tokens": 1}}
        failure = ValueError("synthetic provider error")

        async def provider(**kwargs):
            self.assertIs(kwargs["messages"], messages)
            self.assertTrue(kwargs["messages"][0]["content"].startswith("ada@example.test"))
            await asyncio.sleep(0)
            return result

        async def failing_provider(**kwargs):
            raise failure

        async def run():
            response = await papaya._capture_provider_call("synthetic", ["create"], provider, (), {"messages": messages})
            self.assertIs(response, result)
            with self.assertRaises(ValueError) as caught:
                await papaya._capture_provider_call("synthetic", ["create"], failing_provider, (), {"messages": messages})
            self.assertIs(caught.exception, failure)

        asyncio.run(run())
        captured_input = papaya._completed[0]["spans"][1]["inputPayload"]["value"]
        self.assertTrue(captured_input["messages"][0]["content"].startswith("[redacted-email]"))


if __name__ == "__main__":
    unittest.main()
