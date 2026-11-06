import asyncio
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from papaya_ai import Papaya


class PapayaProviderIdentityTest(unittest.TestCase):
    def probe(self, route, *, asynchronous=False, returned_model="gpt-4o-returned-version"):
        batches = []
        def transport(_url, _headers, body):
            batches.append(json.loads(body))
            return 202, '{"accepted":1,"rejected":0}'
        papaya = Papaya.init(api_key="synthetic-local-only", transport=transport)
        messages = [{"role": "user", "content": "Synthetic message"}]
        extra = {"temperature": 0.25}
        expected = {"model": "gpt-4o-requested", "messages": messages, "extra_body": extra}
        response = {"id": "synthetic-provider-response", "usage": {"prompt_tokens": 8, "completion_tokens": 3}}
        if returned_model is not None:
            response["model"] = returned_model
        calls = []
        def call(**kwargs):
            self.assertEqual(kwargs, expected)
            self.assertIs(kwargs["messages"], messages)
            self.assertIs(kwargs["extra_body"], extra)
            calls.append(kwargs)
            return response
        async def async_call(**kwargs):
            await asyncio.sleep(0)
            return call(**kwargs)
        raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=async_call if asynchronous else call)))
        wrapped = papaya.openai(raw) if route == "openai" else papaya.wrap_client(route, raw)
        result = wrapped.chat.completions.create(**expected)
        if asynchronous:
            result = asyncio.run(result)
        self.assertIs(result, response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(messages, [{"role": "user", "content": "Synthetic message"}])
        self.assertEqual(extra, {"temperature": 0.25})
        self.assertEqual(papaya.flush()["status"], "sent")
        span = next(s for s in batches[0]["traces"][0]["spans"] if s["kind"] == "llm")
        self.assertEqual(span["modelRef"], {"provider": route, "requested": "gpt-4o-requested", "used": returned_model or "gpt-4o-requested"})
        self.assertEqual(span["attributes"]["provider"], route)
        papaya.close()
        return span

    def test_direct_and_compatible_routes_keep_requested_returned_and_provider_separate(self):
        direct = self.probe("openai")
        compatible = self.probe("openrouter")
        self.assertEqual(direct["modelRef"]["requested"], compatible["modelRef"]["requested"])
        self.assertNotEqual(direct["modelRef"]["provider"], compatible["modelRef"]["provider"])

    def test_async_override_preserves_provider_argument_identity(self):
        self.probe("compatible-gateway", asynchronous=True)

    def test_missing_returned_model_retains_requested_model_without_rewriting_request(self):
        self.probe("openrouter", returned_model=None)


if __name__ == "__main__":
    unittest.main()
