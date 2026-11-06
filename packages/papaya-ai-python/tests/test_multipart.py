"""Lossless multipart regressions, including real loopback HTTP interruption.

All identities and payloads are synthetic. No provider or external network calls.
"""
import asyncio
import hashlib
import http.server
import json
import pathlib
import socket
import sys
import threading
import time
import unittest
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import Papaya, PapayaCaptureError

MIB = 1024 * 1024
FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "multipart-regression.json").read_text())


def trace(papaya, trace_id="multipart-trace", text="synthetic complete payload"):
    value = papaya.start_trace({"traceId": trace_id, "workflowKey": FIXTURE["workflowKey"]}, input_value={"text": text})
    papaya.finish_trace(value, "success", output_value=FIXTURE["marker"])
    return value


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("Synthetic multipart condition exceeded its deadline")


class MultipartApi:
    def __init__(self):
        self.enabled = True
        self.cap_max = 32 * MIB
        self.calls = []
        self.session = None
        self.parts = {}
        self.raw = None
        self.lost_part = False
        self.lost_complete = False
        self.conflicting_part = False
        self.force_state = None
        self.corrupt_receipt = False
        self.status_count = 0
        self.complete_count = 0
        self.abort = False
        self.slow = False
        self.policy_mutates = False

    def status(self):
        value = dict(self.session)
        value["parts"] = [{"partNumber": number, "byteLength": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                          for number, body in sorted(self.parts.items())]
        if self.conflicting_part and value["parts"]:
            value["parts"][0]["sha256"] = "0" * 64
        if self.force_state:
            value["state"] = self.force_state
        return value

    def request(self, method, url, headers, body):
        path = urlsplit(url).path
        self.calls.append((method, path, len(body), hashlib.sha256(body).hexdigest()))
        self.assert_headers(headers, body)
        if path.endswith("/config"):
            return self.reply({"features": {"multipart": {"enabled": self.enabled, "maxBytes": self.cap_max,
                "partSizeBytes": 5 * MIB, "maxParts": 7, "minIdleTtlSeconds": 60, "maxIdleTtlSeconds": 3600}}})
        if method == "POST" and path.endswith("/trace-uploads"):
            payload = json.loads(body)
            if self.session is None:
                self.session = {"uploadId": "synthetic-upload", "batchId": payload["batchId"],
                    "state": "uploading", "byteLength": payload["byteLength"], "sha256": payload["sha256"],
                    "partSizeBytes": 5 * MIB, "expiresAt": "2030-01-01T00:00:00Z", "hardExpiresAt": "2030-01-02T00:00:00Z"}
            elif any(self.session[key] != payload[key] for key in ("batchId", "byteLength", "sha256")):
                return 409, '{}'
            return self.reply(self.status(), 201)
        if method == "GET":
            self.status_count += 1
            return self.reply(self.status())
        if method == "PUT":
            number = int(path.rsplit("/", 1)[1])
            if headers.get("X-Papaya-Part-Sha256") != hashlib.sha256(body).hexdigest():
                return 400, '{}'
            if number in self.parts and self.parts[number] != body:
                return 409, '{}'
            self.parts[number] = body
            if self.lost_part:
                self.lost_part = False
                raise ConnectionResetError("synthetic lost part acknowledgement")
            return self.reply({"partNumber": number, "byteLength": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        if method == "POST" and path.endswith("/complete"):
            self.complete_count += 1
            if json.loads(body)["parts"] != self.status()["parts"]:
                return 409, '{}'
            self.raw = b"".join(self.parts[number] for number in sorted(self.parts))
            if len(self.raw) != self.session["byteLength"] or hashlib.sha256(self.raw).hexdigest() != self.session["sha256"]:
                return 400, '{}'
            self.session["state"] = "landed"
            self.session["receipt"] = {"uploadId": self.session["uploadId"], "batchId": self.session["batchId"],
                "accepted": len(json.loads(self.raw)["traces"]), "rejected": 0, "warnings": [],
                "rawSha256": "a" * 64 if self.policy_mutates else hashlib.sha256(self.raw).hexdigest(),
                "rawBytes": len(self.raw), "landedAt": "2030-01-01T00:00:00Z"}
            if self.corrupt_receipt:
                self.session["receipt"]["batchId"] = "incorrect-batch"
            if self.lost_complete:
                self.lost_complete = False
                raise ConnectionResetError("synthetic lost complete acknowledgement")
            return self.reply(self.status(), 202)
        return 404, '{}'

    @staticmethod
    def reply(value, status=200):
        return status, json.dumps(value)

    @staticmethod
    def assert_headers(headers, body):
        assert headers["Content-Length"] == str(len(body))


class MultipartTest(unittest.TestCase):
    def make(self, api=None, **options):
        api = api or MultipartApi()
        papaya = Papaya.init(api_key="synthetic-key", endpoint="https://papaya.example/api/v1/ingest/traces",
            multipart=True, multipart_requester=api.request, retry_base=0.015, retry_max=0.015, retry_jitter=0,
            capture="full", **options)
        self.addCleanup(papaya.close, deadline=0.02)
        return papaya, api

    def test_opt_in_limits_and_legacy_transport_contract(self):
        legacy = Papaya.init(transport=lambda *_args: (202, '{}'))
        self.assertEqual(legacy.max_trace_bytes, 8 * MIB)
        self.assertEqual(legacy.max_active_bytes, 16 * MIB)
        multipart, _ = self.make()
        self.assertEqual(multipart.max_trace_bytes, 32 * MIB)
        self.assertEqual(multipart.max_active_bytes, 64 * MIB)
        override, _ = self.make(max_trace_bytes=4096, max_active_bytes=8192)
        self.assertEqual((override.max_trace_bytes, override.max_active_bytes), (4096, 8192))
        with self.assertRaisesRegex(ValueError, "POST-only"):
            Papaya.init(multipart=True, transport=lambda *_args: (202, '{}'))
        with self.assertRaisesRegex(ValueError, "require multipart"):
            Papaya.init(multipart_requester=lambda *_args: (202, '{}'))

    def test_interrupted_part_reads_status_and_skips_verified_exact_part(self):
        api = MultipartApi()
        api.lost_part = True
        papaya, _ = self.make(api)
        trace(papaya, text="s" * (6 * MIB))
        first = papaya.flush()
        self.assertEqual(first["sent"]["traces"], 0)
        wait_for(lambda: papaya.export_status()["sent"]["traces"] == 1)
        puts = [call for call in api.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 2)
        self.assertEqual([call[1].rsplit("/", 1)[1] for call in puts], ["1", "2"])
        self.assertEqual(api.status_count, 1)
        self.assertEqual(api.complete_count, 1)
        self.assertEqual(json.loads(api.raw)["traces"][0]["spans"][0]["outputPayload"]["value"], FIXTURE["marker"])

    def test_lost_completion_is_recovered_from_landed_status_without_recompletion(self):
        api = MultipartApi()
        api.lost_complete = True
        papaya, _ = self.make(api)
        trace(papaya)
        papaya.flush()
        result = wait_for(lambda: papaya.export_status() if papaya.export_status()["sent"]["traces"] else None)
        self.assertEqual(api.complete_count, 1)
        self.assertEqual(api.status_count, 1)
        self.assertEqual(len([call for call in api.calls if call[0] == "PUT"]), 1)
        receipt = result["receipts"][-1]
        self.assertEqual(receipt["multipart"]["uploadId"], "synthetic-upload")
        self.assertEqual(receipt["multipart"]["sha256"], hashlib.sha256(api.raw).hexdigest())

    def test_lost_creation_acknowledgment_reuses_identical_batch_identity_and_hash(self):
        api = MultipartApi()
        lost = [True]
        def requester(method, url, headers, body):
            response = api.request(method, url, headers, body)
            if method == "POST" and url.endswith("/trace-uploads") and lost[0]:
                lost[0] = False
                raise ConnectionResetError("synthetic create acknowledgement lost")
            return response
        papaya, _ = self.make(api)
        papaya.multipart_requester = requester
        papaya.transport.requester = requester
        trace(papaya)
        papaya.flush()
        wait_for(lambda: papaya.export_status()["sent"]["traces"] == 1)
        creates = [call for call in api.calls if call[0] == "POST" and call[1].endswith("/trace-uploads")]
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0], creates[1])
        self.assertEqual(api.complete_count, 1)

    def test_busy_session_is_retryable_and_status_precedes_later_work(self):
        api = MultipartApi()
        api.force_state = "assembling"
        papaya, _ = self.make(api)
        trace(papaya)
        first = papaya.flush()
        self.assertEqual(first["retrying"]["traces"], 1)
        self.assertEqual(first["errorCode"], "multipart_operation_in_progress")
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))
        api.force_state = None
        wait_for(lambda: papaya.export_status()["sent"]["traces"] == 1)
        self.assertEqual(api.status_count, 1)

    def test_retryable_completion_failure_resumes_all_server_states_with_same_manifest(self):
        for state in ("assembling", "validating", "publishing"):
            with self.subTest(state=state):
                api = MultipartApi()
                completions = []
                def requester(method, url, headers, body):
                    if method == "POST" and url.endswith("/complete"):
                        completions.append(body)
                        if len(completions) == 1:
                            api.session["state"] = state
                            return 503, '{"error":"trace_upload_operation_failed"}'
                    return api.request(method, url, headers, body)
                papaya, _ = self.make(api)
                papaya.transport.requester = requester
                trace(papaya, text="s" * (6 * MIB))
                first = papaya.flush()
                self.assertEqual(first["retrying"]["traces"], 1)
                wait_for(lambda: papaya.export_status()["sent"]["traces"] == 1)
                self.assertEqual(len(completions), 2)
                self.assertEqual(completions[0], completions[1])
                self.assertEqual(api.status_count, 1)
                self.assertEqual(len([call for call in api.calls if call[0] == "PUT"]), 2,
                                 "verified parts must not be uploaded again")
                self.assertEqual(api.complete_count, 1)

    def test_busy_completion_lease_conflict_is_retryable_then_resumes_exact_manifest(self):
        api = MultipartApi()
        completions = []
        def requester(method, url, headers, body):
            if method == "POST" and url.endswith("/complete"):
                completions.append(body)
                if len(completions) == 1:
                    api.session["state"] = "publishing"
                    return 409, '{"error":"trace_upload_operation_in_progress"}'
            return api.request(method, url, headers, body)
        papaya, _ = self.make(api)
        papaya.transport.requester = requester
        trace(papaya)
        first = papaya.flush()
        self.assertEqual(first["retrying"]["traces"], 1)
        self.assertEqual(first["errorCode"], "multipart_operation_in_progress")
        wait_for(lambda: papaya.export_status()["sent"]["traces"] == 1)
        self.assertEqual(len(completions), 2)
        self.assertEqual(completions[0], completions[1])
        self.assertEqual(api.status_count, 1)
        self.assertEqual(len([call for call in api.calls if call[0] == "PUT"]), 1)

    def test_genuine_completion_conflicts_and_noncompletion_busy_code_remain_terminal(self):
        for operation, response in (("complete", '{"error":"trace_upload_policy_changed_after_publication"}'),
                                    ("complete", '{"error":"trace_upload_manifest_mismatch"}'),
                                    ("complete", '{"error":"trace_upload_content_conflict"}'),
                                    ("complete", 'not-json'),
                                    ("create", '{"error":"trace_upload_operation_in_progress"}')):
            with self.subTest(operation=operation, response=response):
                api = MultipartApi()
                conflicts = []
                def requester(method, url, headers, body):
                    target = "/complete" if operation == "complete" else "/trace-uploads"
                    if method == "POST" and url.endswith(target):
                        conflicts.append(body)
                        return 409, response
                    return api.request(method, url, headers, body)
                papaya, _ = self.make(api)
                papaya.transport.requester = requester
                trace(papaya)
                outcome = papaya.flush()
                self.assertEqual(outcome["terminalFailed"]["traces"], 1)
                self.assertEqual(outcome["sent"]["traces"], 0)
                self.assertEqual(len(conflicts), 1)
                self.assertEqual(api.status_count, 0)
                self.assertEqual(len(papaya._exporter._batches), 1)

    def test_canceled_async_wait_and_close_stop_new_parts_but_keep_inflight_owner(self):
        api = MultipartApi()
        entered, release = threading.Event(), threading.Event()
        def requester(method, url, headers, body):
            if method == "PUT":
                entered.set()
                release.wait(2)
            return api.request(method, url, headers, body)
        papaya, _ = self.make(api)
        papaya.transport.requester = requester
        trace(papaya, text="s" * (6 * MIB))
        async def run():
            waiter = asyncio.create_task(papaya.aflush(timeout=1))
            until = time.monotonic() + 1
            while not entered.is_set() and time.monotonic() < until:
                await asyncio.sleep(0.002)
            self.assertTrue(entered.is_set())
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            start = time.monotonic()
            closed = await papaya.aclose(deadline=0.03)
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(closed["sending"]["traces"], 1)
        try:
            asyncio.run(run())
        finally:
            release.set()
        wait_for(lambda: papaya.export_status()["sending"]["traces"] == 0)
        self.assertEqual(len([call for call in api.calls if call[0] == "PUT"]), 1)
        self.assertEqual(api.complete_count, 0)
        self.assertEqual(papaya.export_status()["pending"]["traces"], 1)

    def test_missing_capability_and_oversized_total_fail_without_any_upload(self):
        for enabled, maximum, code in ((False, 32 * MIB, "multipart_unsupported"),
                                       (True, 512, "multipart_max_bytes_exceeded")):
            with self.subTest(code=code):
                api = MultipartApi()
                api.enabled, api.cap_max = enabled, maximum
                papaya, _ = self.make(api)
                trace(papaya, text="s" * 1024)
                result = papaya.flush()
                self.assertEqual(result["errorCode"], code)
                self.assertEqual(result["terminalFailed"]["traces"], 1)
                self.assertEqual([call[0] for call in api.calls], ["GET"])
                self.assertEqual(result["pending"]["traces"], 1)

    def test_conflicting_status_part_fails_retaining_the_complete_frozen_body(self):
        api = MultipartApi()
        api.lost_part = True
        api.conflicting_part = True
        papaya, _ = self.make(api)
        trace(papaya)
        papaya.flush()
        result = wait_for(lambda: papaya.export_status() if papaya.export_status()["terminalFailed"]["traces"] else None)
        self.assertEqual(result["receipts"][-1]["code"], "multipart_part_conflict")
        frozen = papaya._exporter._batches[0]
        self.assertEqual(hashlib.sha256(frozen.body).hexdigest(), frozen.multipart_state["sha256"])
        self.assertEqual(api.complete_count, 0)

    def test_terminal_session_states_never_create_replacement_or_acknowledge(self):
        for state in ("failed", "expired", "aborted"):
            with self.subTest(state=state):
                api = MultipartApi()
                api.force_state = state
                papaya, _ = self.make(api)
                trace(papaya)
                result = papaya.flush()
                self.assertEqual(result["errorCode"], "multipart_" + state)
                self.assertEqual(result["sent"]["traces"], 0)
                self.assertEqual(len([call for call in api.calls if call[0] == "POST"]), 1)

    def test_mismatched_landing_receipt_is_terminal_and_post_policy_hash_is_distinct(self):
        api = MultipartApi()
        api.corrupt_receipt = True
        papaya, _ = self.make(api)
        trace(papaya)
        result = papaya.flush()
        self.assertEqual(result["errorCode"], "multipart_invalid_landing_receipt")
        self.assertEqual(result["sent"]["traces"], 0)
        api2 = MultipartApi()
        api2.policy_mutates = True
        papaya2, _ = self.make(api2)
        trace(papaya2)
        result2 = papaya2.flush()
        self.assertEqual(result2["sent"]["traces"], 1)
        receipt = result2["targetReceipts"][0]["multipart"]
        self.assertEqual(receipt["rawSha256"], "a" * 64)
        self.assertNotEqual(receipt["rawSha256"], receipt["sha256"])

    def loopback(self, api):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                pass
            def run_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if api.slow and self.path.endswith("/config"):
                    self.close_connection = True
                    try:
                        for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.015)
                    except OSError:
                        api.abort = True
                    return
                try:
                    status, text = api.request(self.command, "http://loopback" + self.path, dict(self.headers), body)
                except ConnectionResetError:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                encoded = text.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            do_GET = run_request
            do_PUT = run_request
            do_POST = run_request
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_port) + "/api/v1/ingest/traces"

    def test_native_http_preserves_nine_mib_media_history_and_resumes_lost_acknowledgments(self):
        api = MultipartApi()
        api.lost_part, api.lost_complete = True, True
        endpoint = self.loopback(api)
        papaya = Papaya.init(api_key="synthetic-key", endpoint=endpoint, multipart=True,
            capture="full", retry_base=0.015, retry_max=0.015, retry_jitter=0, request_timeout=2)
        self.addCleanup(papaya.close, deadline=0.05)
        media = FIXTURE["media"]["urlPrefix"] + "a" * (9 * MIB)
        source = {"media": media, "history": FIXTURE["history"] * 200}
        value = papaya.start_trace({"traceId": "native-large", "workflowKey": FIXTURE["workflowKey"]}, input_value=source)
        papaya.finish_trace(value, "success", output_value=FIXTURE["marker"])
        papaya.flush()
        result = wait_for(lambda: papaya.export_status() if papaya.export_status()["sent"]["traces"] else None, timeout=8)
        self.assertEqual(api.complete_count, 1)
        self.assertEqual(api.status_count, 2)
        self.assertGreater(len(api.raw), 9 * MIB)
        raw_trace = json.loads(api.raw)["traces"][0]
        self.assertEqual(raw_trace["spans"][0]["inputPayload"]["value"], source)
        self.assertEqual(raw_trace["spans"][0]["outputPayload"]["value"], FIXTURE["marker"])
        self.assertEqual(len([call for call in api.calls if call[0] == "PUT"]), 2)
        self.assertEqual(result["receipts"][-1]["multipart"]["sha256"], hashlib.sha256(api.raw).hexdigest())

    def test_native_header_trickle_request_deadline_aborts_and_lifecycle_wait_is_bounded(self):
        api = MultipartApi()
        api.slow = True
        papaya = Papaya.init(api_key="synthetic-key", endpoint=self.loopback(api), multipart=True,
                            request_timeout=0.06, retry_base=1, retry_max=1, retry_jitter=0)
        trace(papaya)
        started = time.monotonic()
        papaya.flush(timeout=0.2)
        closed = papaya.close(deadline=0.04)
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(closed["sent"]["traces"], 0)
        wait_for(lambda: api.abort, timeout=1)
        wait_for(lambda: not [thread for thread in threading.enumerate() if thread.name == "papaya-multipart-io"], timeout=1)

    def test_provider_identity_survives_multipart_admission_failure(self):
        papaya, _ = self.make(max_trace_bytes=256)
        response = object()
        error = RuntimeError("synthetic provider failure")
        class Completions:
            def create(self, **_kwargs):
                return response
        class Client:
            pass
        client = Client()
        client.chat = Client()
        client.chat.completions = Completions()
        wrapped = papaya.openai(client)
        self.assertIs(wrapped.chat.completions.create(model="synthetic", messages=[{"role": "user", "content": "s" * 1024}]), response)
        def fail(**_kwargs):
            raise error
        client.chat.completions.create = fail
        wrapped = papaya.openai(client)
        with self.assertRaises(RuntimeError) as observed:
            wrapped.chat.completions.create(model="synthetic", messages=[])
        self.assertIs(observed.exception, error)
        self.assertGreater(papaya.export_status()["rejected"]["traces"], 0)


if __name__ == "__main__":
    unittest.main()
