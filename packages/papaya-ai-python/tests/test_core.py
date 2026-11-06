import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import Papaya


class PapayaCoreTest(unittest.TestCase):
    def test_flush_exports_native_python_batch_and_redacts_payloads(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append((endpoint, headers, json.loads(body.decode("utf-8"))))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            project="checkout",
            environment="test",
            capture="redacted",
            service_name="python-test",
            metadata={"release": "sha-test"},
            transport=transport,
        )

        with papaya.run({
            "workflowKey": "checkout",
            "sessionId": "session-1",
            "group": {"name": "url", "value": "northfield.example.gov"},
        }) as trace:
            span = papaya.start_span(
                trace=trace,
                name="openai.chat",
                kind="llm",
                input_value=[{"role": "user", "content": "email ada@example.com token Bearer abc.secret.token"}],
                model_ref={"provider": "openai", "requested": "gpt-test"},
            )
            papaya.finish_span(
                span,
                "success",
                output_value={"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
                usage={"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
                model_used="gpt-test",
            )

        result = papaya.flush()

        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(captured), 1)
        endpoint, headers, body = captured[0]
        self.assertEqual(endpoint, "https://papaya.example/api/v1/ingest/traces")
        self.assertEqual(headers["Authorization"], "Bearer papaya-test-token")
        self.assertEqual(body["schemaVersion"], "2026-06-05")
        self.assertEqual(body["sdk"]["name"], "papaya-ai")
        self.assertEqual(body["sdk"]["language"], "python")
        exported = json.dumps(body)
        self.assertNotIn("ada@example.com", exported)
        self.assertNotIn("Bearer abc.secret.token", exported)
        self.assertIn("[redacted-email]", exported)
        self.assertIn("Bearer [redacted-token]", exported)
        self.assertEqual(body["traces"][0]["workflowKey"], "checkout")
        self.assertEqual(body["traces"][0]["group"], {"name": "url", "value": "northfield.example.gov"})
        llm_span = next(span for span in body["traces"][0]["spans"] if span["kind"] == "llm")
        self.assertEqual(llm_span["usage"]["inputTokens"], 5)

    def test_missing_api_key_keeps_completed_trace_for_later_flush(self):
        papaya = Papaya.init(endpoint="https://papaya.example/api/v1/ingest/traces")

        with papaya.run({"workflowKey": "missing-key"}):
            pass

        first = papaya.flush()
        second = papaya.flush()

        for result in (first, second):
            self.assertEqual(
                {key: result[key] for key in ("status", "traceCount", "reason")},
                {"status": "skipped", "traceCount": 1, "reason": "missing_api_key"},
            )
            self.assertEqual(result["sent"]["traces"], 0)
            self.assertEqual(result["queued"]["traces"], 1)

    def test_failed_flush_keeps_completed_trace_for_retry(self):
        calls = []

        def transport(endpoint, headers, body):
            calls.append(json.loads(body.decode("utf-8")))
            if len(calls) == 1:
                return 503, '{"error":"temporarily unavailable"}'
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            transport=transport,
            retry_base=0.01,
            retry_max=0.01,
            retry_jitter=0,
        )

        with papaya.run({"workflowKey": "retryable"}):
            pass

        first = papaya.flush()
        second = papaya.flush()

        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["traceCount"], 1)
        self.assertEqual(second["status"], "sent")
        self.assertEqual(second["traceCount"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0]["batchId"], calls[1]["batchId"])

    def test_413_retains_multi_trace_batch_without_splitting_or_changing_trace_ids(self):
        calls = []

        def transport(endpoint, headers, body):
            batch = json.loads(body.decode("utf-8"))
            calls.append({
                "batchId": batch["batchId"],
                "traceIds": [trace["traceId"] for trace in batch["traces"]],
            })
            if len(batch["traces"]) > 1:
                return 413, '{"error":"too_large"}'
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            max_batch_bytes=1024 * 1024,
            transport=transport,
        )
        for trace_id in ("trace-one", "trace-two", "trace-three"):
            trace = papaya.start_trace({"traceId": trace_id, "workflowKey": "split-test"})
            papaya.finish_trace(trace, "success")

        result = papaya.flush()

        self.assertNotEqual(result["status"], "sent")
        self.assertEqual(result["traceCount"], 3)
        self.assertEqual(result["sent"]["traces"], 0)
        self.assertEqual(result["terminalFailed"]["traces"], 3)
        self.assertEqual(
            [call["traceIds"] for call in calls],
            [["trace-one", "trace-two", "trace-three"]],
        )
        retained = list(papaya._exporter._batches)
        self.assertEqual(len(retained), 1)
        self.assertEqual(json.loads(retained[0].body)["batchId"], calls[0]["batchId"])
        self.assertEqual(
            [trace["traceId"] for trace in json.loads(retained[0].body)["traces"]],
            ["trace-one", "trace-two", "trace-three"],
        )

    def test_exact_byte_packing_keeps_batches_under_target(self):
        calls = []

        def transport(endpoint, headers, body):
            batch = json.loads(body.decode("utf-8"))
            calls.append({
                "bytes": len(body),
                "traceIds": [trace["traceId"] for trace in batch["traces"]],
            })
            return 202, '{"accepted":1,"rejected":0}'

        max_batch_bytes = 1_200
        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            max_batch_bytes=max_batch_bytes,
            transport=transport,
        )
        for trace_id in ("packed-one", "packed-two", "packed-three"):
            trace = papaya.start_trace(
                {"traceId": trace_id, "workflowKey": "exact-byte-pack"},
                input_value={"text": "x" * 400},
            )
            papaya.finish_trace(trace, "success")

        result = papaya.flush()

        self.assertEqual(result["status"], "sent")
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(
            call["bytes"] <= max_batch_bytes or len(call["traceIds"]) == 1
            for call in calls
        ))
        self.assertEqual(
            [trace_id for call in calls for trace_id in call["traceIds"]],
            ["packed-one", "packed-two", "packed-three"],
        )


if __name__ == "__main__":
    unittest.main()
