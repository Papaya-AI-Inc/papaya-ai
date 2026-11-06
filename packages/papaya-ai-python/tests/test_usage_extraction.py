import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import Papaya
from papaya_ai.core import _usage_from_record, _usage_from_result


class PapayaUsageExtractionTest(unittest.TestCase):
    def test_shared_beta_provider_fixtures_recover_the_exact_expected_fields(self):
        path = ROOT.parents[1] / "scripts" / "fixtures" / "papaya-python-sdk-m4-provider-fixtures.json"
        fixtures = json.loads(path.read_text())
        self.assertEqual(fixtures["schema"], "papaya-m4-provider-fixtures/v1")
        self.assertEqual(len(fixtures["providers"]), 5)
        for fixture in fixtures["providers"]:
            with self.subTest(case=fixture["case"]):
                diagnostics = []
                actual = _usage_from_result(fixture["result"], diagnostics=diagnostics)
                self.assertEqual(actual, {**fixture["expected"], "pricingSource": "provider"})
                self.assertEqual(diagnostics, [])

    def test_responses_preserves_shallow_counts_and_recovers_deeper_cost_and_details(self):
        response = {
            "id": "response-synthetic", "model": "model-synthetic",
            "usage": {"input_tokens": 10, "output_tokens": 6, "total_tokens": 16},
            "response_metadata": {"usage": {"input_tokens": 999, "cost_usd": 0.125,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 3}}},
        }
        self.assertEqual(_usage_from_result(response), {
            "inputTokens": 10, "outputTokens": 6, "totalTokens": 16,
            "cacheReadInputTokens": 4, "reasoningTokens": 3,
            "costUsd": 0.125, "pricingSource": "provider"})

    def test_chat_cache_and_reasoning_are_subsets_of_provider_totals(self):
        response = {"usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 3}}}
        self.assertEqual(_usage_from_result(response), {
            "inputTokens": 9, "outputTokens": 5, "totalTokens": 14,
            "cacheReadInputTokens": 4, "reasoningTokens": 3})

    def test_anthropic_cache_counters_remain_separate(self):
        usage = _usage_from_result({"usage": {"input_tokens": 10, "output_tokens": 2,
            "cache_read_input_tokens": 4, "cache_creation_input_tokens": 3}})
        self.assertEqual(usage, {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12,
            "cacheReadInputTokens": 4, "cacheCreationInputTokens": 3})

    def test_gemini_camel_and_snake_usage_details_survive_without_total_inflation(self):
        for usage in [
            {"promptTokenCount": 11, "candidatesTokenCount": 5, "totalTokenCount": 19,
                "cachedContentTokenCount": 2, "thoughtsTokenCount": 3},
            {"prompt_token_count": 11, "candidates_token_count": 5, "total_token_count": 19,
                "cached_content_token_count": 2, "thoughts_token_count": 3},
        ]:
            with self.subTest(usage=usage):
                self.assertEqual(_usage_from_result({"usageMetadata": usage}), {
                    "inputTokens": 11, "outputTokens": 5, "totalTokens": 19,
                    "cacheReadInputTokens": 2, "reasoningTokens": 3})

    def test_langchain_detail_aliases_fill_fields_independently(self):
        self.assertEqual(_usage_from_result({"usage_metadata": {
            "input_tokens": 8, "output_tokens": 2,
            "input_token_details": {"cache_read": 3, "cache_creation": 1},
            "output_token_details": {"reasoning": 1}}}), {
                "inputTokens": 8, "outputTokens": 2, "totalTokens": 10,
                "cacheReadInputTokens": 3, "cacheCreationInputTokens": 1, "reasoningTokens": 1})

    def test_compatible_gateway_account_zero_wins_over_upstream_cost(self):
        self.assertEqual(_usage_from_result({"usage": {"prompt_tokens": 3,
            "completion_tokens": 2, "cost": 0,
            "cost_details": {"upstream_inference_cost": 9.99}},
            "response": {"usage": {"cost_usd": 1.5}}}), {
                "inputTokens": 3, "outputTokens": 2, "totalTokens": 5,
                "costUsd": 0, "pricingSource": "provider"})
        self.assertIsNone(_usage_from_result({"cost_details": {"upstream_inference_cost": 9.99}}))

    def test_cost_only_records_preserve_zero_without_inventing_tokens(self):
        for key in ("costUsd", "cost_usd", "cost", "total_cost", "totalCost"):
            with self.subTest(key=key):
                self.assertEqual(_usage_from_result({"usage": {key: 0}}), {
                    "costUsd": 0, "pricingSource": "provider"})
        self.assertEqual(_usage_from_result({"model": "synthetic", "total_cost": 0.25}), {
            "costUsd": 0.25, "pricingSource": "provider"})

    def test_direct_scoped_metadata_cost_is_recovered_after_shallow_tokens(self):
        self.assertEqual(_usage_from_result({"usage": {"inputTokens": 4},
            "response_metadata": {"total_cost": 0.2}}), {
                "inputTokens": 4, "totalTokens": 4, "costUsd": 0.2, "pricingSource": "provider"})
        self.assertEqual(_usage_from_result({"usage": {"inputTokens": 4}, "cost": 0}), {
            "inputTokens": 4, "totalTokens": 4, "costUsd": 0, "pricingSource": "provider"})

    def test_invalid_numbers_cannot_shadow_valid_aliases_or_deeper_values(self):
        for invalid in (-1, float("nan"), float("inf"), -float("inf"), True, "3"):
            with self.subTest(invalid=invalid):
                self.assertEqual(_usage_from_result({"usage": {
                    "inputTokens": invalid, "input_tokens": 4, "costUsd": invalid},
                    "response": {"usage": {"output_tokens": 2, "total_tokens": 6, "cost_usd": 0.1}}}), {
                        "inputTokens": 4, "outputTokens": 2, "totalTokens": 6,
                        "costUsd": 0.1, "pricingSource": "provider"})
        self.assertIsNone(_usage_from_result({"usage": {"costUsd": float("nan")}}))

    def test_declared_canonical_fields_win_and_duplicate_observations_do_not_sum(self):
        shared = {"usage": {"input_tokens": 50, "output_tokens": 50, "cost_usd": 2}}
        diagnostics = []
        response = {"usage": {"inputTokens": 3, "input_tokens": 99, "outputTokens": 2,
            "totalTokens": 5, "costUsd": 0.25, "cost_usd": 8},
            "response": shared, "body": shared}
        self.assertEqual(_usage_from_result(response, diagnostics=diagnostics), {
            "inputTokens": 3, "outputTokens": 2, "totalTokens": 5,
            "costUsd": 0.25, "pricingSource": "provider"})
        self.assertEqual(diagnostics, [])

    def test_other_calls_parent_aggregates_and_content_are_never_usage_sources(self):
        for response in [
            {"usage": {"costUsd": 99}, "spans": [{"usage": {"costUsd": 0.5}}]},
            {"traces": [{"usage": {"costUsd": 0.5}}]},
            {"children": [{"usage": {"costUsd": 0.5}}]},
            {"calls": [{"usage": {"costUsd": 0.5}}]},
            {"messages": [{"usage": {"costUsd": 0.5}}]},
            {"choices": [{"message": {"costUsd": 0.5}}]},
            {"data": [{"usage": {"inputTokens": 4}}, {"usage": {"costUsd": 0.5}}]},
        ]:
            self.assertIsNone(_usage_from_result(response))

    def test_conflicting_provider_request_identity_never_fills_missing_fields(self):
        diagnostics = []
        response = {"id": "response-one", "usage": {"input_tokens": 4},
            "response": {"id": "response-two", "usage": {"costUsd": 0.5}}}
        self.assertEqual(_usage_from_result(response, diagnostics=diagnostics), {"inputTokens": 4, "totalTokens": 4})
        self.assertEqual(diagnostics, ["usage_scope_conflict"])

    def test_response_and_http_request_ids_are_separate_valid_namespaces(self):
        diagnostics = []
        response = {"id": "chatcmpl-synthetic", "usage": {"input_tokens": 4, "output_tokens": 2},
            "response_metadata": {"request_id": "req-synthetic", "usage": {"cost_usd": 0.001}}}
        self.assertEqual(_usage_from_result(response, diagnostics=diagnostics), {
            "inputTokens": 4, "outputTokens": 2, "totalTokens": 6,
            "costUsd": 0.001, "pricingSource": "provider"})
        self.assertEqual(diagnostics, [])
        diagnostics = []
        response["request_id"] = "different-http-request"
        self.assertEqual(_usage_from_result(response, diagnostics=diagnostics), {
            "inputTokens": 4, "outputTokens": 2, "totalTokens": 6})
        self.assertEqual(diagnostics, ["usage_scope_conflict"])

    def test_real_cycle_is_diagnosed_but_shared_object_is_not_a_cycle(self):
        response = {"usage": {"inputTokens": 3}}
        response["response"] = response
        diagnostics = []
        self.assertEqual(_usage_from_result(response, diagnostics=diagnostics), {"inputTokens": 3, "totalTokens": 3})
        self.assertEqual(diagnostics, ["usage_traversal_cycle_detected"])
        shared = {"usage": {"inputTokens": 3}}
        diagnostics = []
        self.assertEqual(_usage_from_result({"body": shared, "response": shared}, diagnostics=diagnostics), {
            "inputTokens": 3, "totalTokens": 3})
        self.assertEqual(diagnostics, [])

    def test_depth_and_node_budgets_are_explicit_and_do_not_invent_deep_cost(self):
        deep = {"usage": {"costUsd": 99}}
        for _ in range(20):
            deep = {"response": deep}
        diagnostics = []
        self.assertIsNone(_usage_from_result(deep, diagnostics=diagnostics))
        self.assertEqual(diagnostics, ["usage_traversal_depth_exceeded"])
        def branch(depth):
            return {"response": branch(depth - 1), "body": branch(depth - 1)} if depth else {"usage": {"costUsd": 99}}
        diagnostics = []
        self.assertIsNone(_usage_from_result(branch(9), diagnostics=diagnostics))
        self.assertEqual(diagnostics, ["usage_traversal_nodes_exceeded"])

    def test_unreadable_provider_property_is_a_fixed_diagnostic(self):
        class PapayaSyntheticResponse:
            @property
            def usage(self):
                raise ValueError("PRIVATE_SYNTHETIC")
        diagnostics = []
        self.assertIsNone(_usage_from_result(PapayaSyntheticResponse(), diagnostics=diagnostics))
        self.assertEqual(diagnostics, ["usage_record_unreadable"])
        self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(diagnostics))

    def test_wrapper_preserves_provider_request_response_and_surfaces_limits_without_rejecting_trace(self):
        response = {"id": "response-synthetic", "model": "synthetic-used",
            "usage": {"inputTokens": 3}, "response_metadata": {"usage": {"costUsd": 0}}}
        response["body"] = response
        requests = []
        class PapayaSyntheticResource:
            def create(self, **kwargs):
                requests.append(kwargs)
                return response
        provider = type("PapayaSyntheticProvider", (), {"responses": PapayaSyntheticResource()})()
        captured = []
        def transport(_endpoint, _headers, body):
            captured.append(json.loads(body))
            return 202, '{"accepted":1,"rejected":0}'
        papaya = Papaya.init(api_key="synthetic", transport=transport, capture="full")
        messages = [{"role": "user", "content": "synthetic"}]
        result = papaya.openai(provider).responses.create(model="synthetic-requested", input=messages)
        self.assertIs(result, response)
        self.assertIs(requests[0]["input"], messages)
        self.assertEqual(requests[0], {"model": "synthetic-requested", "input": messages})
        self.assertEqual(papaya.flush()["status"], "sent")
        span = next(item for item in captured[0]["traces"][0]["spans"] if item["kind"] == "llm")
        self.assertEqual(span["usage"], {"inputTokens": 3, "totalTokens": 3,
            "costUsd": 0, "pricingSource": "provider"})
        status = papaya.export_status()
        self.assertIn("usage_traversal_cycle_detected", [item["code"] for item in status["diagnostics"]])
        self.assertEqual(status["captureFailed"]["traces"], 0)
        self.assertEqual(status["rejected"]["traces"], 0)


if __name__ == "__main__":
    unittest.main()
