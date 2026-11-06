"""Petavue exporter regressions: synthetic payloads and deterministic fault transports.

These tests never call a model, use a live credential, or access an external host.
One controlled loopback server verifies the default HTTP request deadline.
The beta acceptance harness separately proves hosted landing and deduplication.
"""

import asyncio
import gc
import concurrent.futures
import hashlib
import http.server
import json
import pathlib
import resource
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import Papaya, PapayaCaptureError


FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "exporter-regression.json").read_text())


def _accepted(_endpoint, _headers, body):
    return 202, json.dumps({"accepted": len(json.loads(body)["traces"]), "rejected": 0})


def _trace(papaya, trace_id, text="synthetic complete payload"):
    trace = papaya.start_trace(
        {"traceId": trace_id, "workflowKey": "synthetic-exporter-regression"},
        input_value={"text": text},
    )
    papaya.finish_trace(trace, "success")
    return trace


def _wait(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    raise AssertionError("Synthetic exporter condition did not complete before its deadline")


def _retained_bodies(papaya):
    batches = papaya._exporter._batches
    values = batches.values() if isinstance(batches, dict) else batches
    return [batch.body for batch in values]


class PapayaExporterTest(unittest.TestCase):
    def make_papaya(self, **kwargs):
        papaya = Papaya.init(
            api_key="synthetic-test-key",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            **kwargs,
        )
        self.addCleanup(papaya.close, deadline=0.05)
        return papaya

    def test_poisoned_first_batch_does_not_block_three_healthy_batches(self):
        calls = []
        attempts = {}
        retry_delay = 0.12

        def transport(_endpoint, _headers, body):
            batch = json.loads(body)
            trace_id = batch["traces"][0]["traceId"]
            attempts[trace_id] = attempts.get(trace_id, 0) + 1
            calls.append((trace_id, time.monotonic(), body))
            if trace_id == FIXTURE["traces"][0]["traceId"] and attempts[trace_id] == 1:
                return 503, '{}'
            return _accepted(_endpoint, _headers, body)

        papaya = self.make_papaya(
            transport=transport, max_batch_bytes=1,
            retry_base=retry_delay, retry_max=retry_delay, retry_jitter=0,
        )
        for fixture in FIXTURE["traces"]:
            _trace(papaya, fixture["traceId"], fixture["marker"])
        papaya.flush()
        _wait(lambda: papaya.export_status()["sent"]["traces"] == 4)
        self.assertEqual([call[0] for call in calls[:4]], [item["traceId"] for item in FIXTURE["traces"]])
        self.assertEqual(calls[4][0], "synthetic-poison")
        self.assertGreaterEqual(calls[4][1] - calls[0][1], retry_delay * 0.9)
        self.assertEqual(calls[0][2], calls[4][2])
        self.assertEqual(hashlib.sha256(calls[0][2]).digest(), hashlib.sha256(calls[4][2]).digest())
        self.assertEqual(papaya.export_status()["terminalFailed"]["traces"], 0)

    def test_ambiguous_transport_timeout_retries_identical_batch_bytes_and_ids(self):
        bodies = []

        def transport(endpoint, headers, body):
            bodies.append(body)
            if len(bodies) == 1:
                raise TimeoutError("Synthetic acknowledgment lost after acceptance")
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(transport=transport, retry_base=0.02, retry_max=0.02, retry_jitter=0)
        _trace(papaya, "ambiguous-trace")
        first = papaya.flush()
        self.assertEqual(first["sent"]["traces"], 0)
        _wait(lambda: papaya.export_status()["sent"]["traces"] == 1)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(json.loads(bodies[0])["traces"][0]["traceId"], "ambiguous-trace")

    def test_retry_after_and_exponential_retry_delay_are_not_bypassed_by_flush(self):
        for status in (429, 503):
            with self.subTest(status=status):
                times = []

                def transport(endpoint, headers, body):
                    times.append(time.monotonic())
                    if len(times) == 1:
                        if status == 429:
                            return 429, '{}', {"Retry-After": "0.12"}
                        return 503, '{}'
                    return _accepted(endpoint, headers, body)

                papaya = self.make_papaya(
                    transport=transport, retry_base=0.08, retry_max=0.2, retry_jitter=0,
                )
                _trace(papaya, "retry-delay-" + str(status))
                papaya.flush()
                papaya.flush(timeout=0.01)
                self.assertEqual(len(times), 1)
                _wait(lambda: papaya.export_status()["sent"]["traces"] == 1)
                minimum = 0.12 if status == 429 else 0.08
                self.assertGreaterEqual(times[1] - times[0], minimum * 0.9)

    def test_auth_failure_pauses_all_network_sends_until_explicit_reconfiguration(self):
        calls = []

        def transport(endpoint, headers, body):
            calls.append((headers["Authorization"], body))
            if len(calls) == 1:
                return 401, '{}'
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(transport=transport, max_batch_bytes=1)
        for index in range(3):
            _trace(papaya, "auth-" + str(index))
        papaya.flush(timeout=0.05)
        self.assertTrue(papaya.export_status()["authPaused"])
        time.sleep(0.08)
        papaya.flush(timeout=0.01)
        self.assertEqual(len(calls), 1)
        self.assertEqual(papaya.export_status()["sent"]["traces"], 0)
        papaya.configure_export(api_key="synthetic-reconfigured-key")
        papaya.flush(timeout=1)
        _wait(lambda: papaya.export_status()["sent"]["traces"] == 3)
        self.assertFalse(papaya.export_status()["authPaused"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(body == calls[0][1] for _header, body in calls[1:]), 1)
        self.assertTrue(all(header == "Bearer synthetic-reconfigured-key" for header, _body in calls[1:]))

    def test_partial_native_receipt_does_not_acknowledge_entire_frozen_batch(self):
        bodies = []

        def transport(_endpoint, _headers, body):
            bodies.append(body)
            return 202, '{"accepted":1,"rejected":1}'

        papaya = self.make_papaya(transport=transport)
        _trace(papaya, "partial-one")
        _trace(papaya, "partial-two")
        result = papaya.flush()
        self.assertNotEqual(result["status"], "sent")
        self.assertEqual(result["sent"]["traces"], 0)
        self.assertEqual(result["terminalFailed"]["traces"], 2)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(_retained_bodies(papaya), bodies)

    def test_legacy_custom_transport_empty_receipt_remains_supported(self):
        papaya = self.make_papaya(transport=lambda *_args: (202, '{}'))
        _trace(papaya, "legacy-receipt")
        self.assertEqual(papaya.flush()["sent"]["traces"], 1)

    def test_inconsistent_native_acknowledgment_cannot_create_false_sent_count(self):
        for receipt in ({"accepted": 0, "rejected": 0}, {"accepted": -1, "rejected": 2}, {"accepted": "1", "rejected": 0}):
            with self.subTest(receipt=receipt):
                papaya = self.make_papaya(transport=lambda *_args: (202, json.dumps(receipt)))
                _trace(papaya, "bad-receipt")
                result = papaya.flush()
                self.assertEqual(result["sent"]["traces"], 0)
                self.assertEqual(result["terminalFailed"]["traces"], 1)
                self.assertNotEqual(result["status"], "sent")

    def test_completed_capture_snapshots_mutable_input_output_and_trace_before_upload(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body))
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(transport=transport)
        source = {"messages": [{"role": "user", "content": "contact ada@example.com"}]}
        output = {"answer": "synthetic original output"}
        trace = papaya.start_trace({"traceId": "snapshot-trace"}, input_value=source)
        papaya.finish_trace(trace, "success", output_value=output)
        source["messages"][0]["content"] = "changed customer input"
        output["answer"] = "changed customer output"
        trace["spans"][0]["inputPayload"]["value"]["messages"][0]["content"] = "changed returned trace"
        papaya.flush()
        frozen = captured[0]["traces"][0]["spans"][0]
        self.assertEqual(frozen["inputPayload"]["value"]["messages"][0]["content"], "contact [redacted-email]")
        self.assertEqual(frozen["outputPayload"]["value"], {"answer": "synthetic original output"})

    def test_pending_count_includes_terminal_batches_and_never_evicts_oldest(self):
        bodies = []

        def transport(_endpoint, _headers, body):
            bodies.append(body)
            return 413, '{}'

        papaya = self.make_papaya(transport=transport, max_batch_bytes=1, max_pending_batches=2)
        for index in range(3):
            _trace(papaya, "capacity-" + str(index))
        result = papaya.flush()
        self.assertEqual(result["terminalFailed"]["traces"], 2)
        self.assertEqual(result["rejected"]["traces"], 0)
        self.assertEqual(result["active"]["completedTraces"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sent"]["traces"], 0)
        self.assertLessEqual(result["highWater"]["batches"], 2)
        self.assertEqual(_retained_bodies(papaya), bodies)
        self.assertEqual([json.loads(body)["traces"][0]["traceId"] for body in bodies], ["capacity-0", "capacity-1"])
        _trace(papaya, "capacity-after-terminal")
        again = papaya.flush()
        self.assertEqual(again["rejected"]["traces"], 0)
        self.assertEqual(again["active"]["completedTraces"], 2)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(_retained_bodies(papaya), bodies)
        self.assertEqual([trace["traceId"] for trace in papaya._completed], ["capacity-2", "capacity-after-terminal"])
        last = papaya.flush(timeout=0.01)
        self.assertEqual(last["rejected"], again["rejected"])
        self.assertEqual(last["active"], again["active"])

    def test_active_and_completed_capacity_are_accounted_before_flush(self):
        papaya = self.make_papaya(transport=_accepted, max_active_traces=2)
        first = papaya.start_trace({"traceId": "active-first"})
        second = papaya.start_trace({"traceId": "active-second"})
        with self.assertRaises(PapayaCaptureError):
            papaya.start_trace({"traceId": "active-rejected"})
        papaya.finish_trace(first, "success")
        # The completed snapshot still occupies memory until frozen/admitted.
        with self.assertRaises(PapayaCaptureError):
            papaya.start_trace({"traceId": "completed-still-retained"})
        papaya.finish_trace(second, "success")
        result = papaya.flush()
        self.assertEqual(result["sent"]["traces"], 2)
        self.assertEqual(result["rejected"]["traces"], 2)
        self.assertLessEqual(result["highWater"]["activeTraces"], 2)

    def test_active_and_single_trace_byte_budgets_reject_explicitly_without_trimming(self):
        for limits in ({"max_active_bytes": 4096}, {"max_trace_bytes": 4096}):
            with self.subTest(limits=limits):
                papaya = self.make_papaya(transport=_accepted, capture="full", **limits)
                with self.assertRaises(PapayaCaptureError) as raised:
                    _trace(papaya, "byte-budget-rejection", "x" * 8192)
                self.assertTrue(raised.exception.code)
                self.assertGreaterEqual(papaya.export_status()["rejected"]["traces"], 1)
                self.assertEqual(papaya.export_status()["sent"]["traces"], 0)

    def test_repeated_admission_failures_keep_bounded_diagnostics_and_exact_counts(self):
        papaya = self.make_papaya(transport=_accepted, max_active_traces=1, diagnostic_limit=4)
        active = papaya.start_trace({"traceId": "retained-active"})
        for index in range(20):
            with self.assertRaises(PapayaCaptureError):
                papaya.start_trace({"traceId": "rejected-" + str(index)})
        result = papaya.export_status()
        self.assertEqual(result["rejected"]["traces"], 20)
        self.assertLessEqual(len(result["diagnostics"]), 4)
        self.assertTrue(result["diagnostics"])
        self.assertEqual(result["active"]["traces"], 1)
        papaya.finish_trace(active, "success")

    def test_capture_flush_race_preserves_every_unique_trace_once(self):
        observed = []
        observed_lock = threading.Lock()

        def transport(endpoint, headers, body):
            ids = [trace["traceId"] for trace in json.loads(body)["traces"]]
            with observed_lock:
                observed.extend(ids)
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(transport=transport)
        start = threading.Barrier(5)

        def capture(worker):
            start.wait()
            for index in range(20):
                _trace(papaya, "race-%s-%s" % (worker, index))
                if index % 3 == 0:
                    papaya.flush(timeout=1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(capture, worker) for worker in range(4)]
            start.wait()
            for future in futures:
                future.result(timeout=5)
        papaya.flush(timeout=2)
        expected = {"race-%s-%s" % (worker, index) for worker in range(4) for index in range(20)}
        self.assertEqual(set(observed), expected)
        self.assertEqual(len(observed), len(expected))
        self.assertEqual(papaya.export_status()["sent"]["traces"], 80)

    def test_immediate_transport_flush_never_loses_its_target_receipt(self):
        papaya = self.make_papaya(transport=_accepted)
        for index in range(100):
            _trace(papaya, "fast-flush-" + str(index))
            result = papaya.flush(timeout=1)
            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["traceCount"], 1)
            self.assertEqual(result["sent"]["traces"], index + 1)
            self.assertEqual(result["targetReceipts"][0]["traceIds"], ["fast-flush-" + str(index)])

    def test_idle_export_thread_retires_and_next_admission_starts_fresh_worker(self):
        before = {thread.ident for thread in threading.enumerate() if thread.name.startswith("papaya-exporter")}
        papaya = self.make_papaya(transport=_accepted)
        for index in range(2):
            _trace(papaya, "worker-restart-" + str(index))
            result = papaya.flush(timeout=1)
            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["sent"]["traces"], index + 1)
            _wait(lambda: not any(
                thread.name.startswith("papaya-exporter") and thread.ident not in before
                for thread in threading.enumerate()
            ), timeout=0.5)

    def test_default_http_request_budget_covers_continuously_trickled_headers(self):
        request_budget = 0.08
        handler_entered = threading.Event()
        client_disconnected = threading.Event()

        class SlowHeaders(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                handler_entered.set()
                try:
                    self.connection.sendall(b"HTTP/1.1 202 Accepted\r\nX-Slow: ")
                    # Every byte arrives inside the socket inactivity timeout.
                    # Only an overall request deadline can bound this exchange.
                    for _ in range(30):
                        self.connection.sendall(b"x")
                        time.sleep(0.02)
                    self.connection.sendall(b"\r\nContent-Length: 2\r\n\r\n{}")
                except OSError:
                    client_disconnected.set()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
        server.daemon_threads = True
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.005), daemon=True)
        serving.start()
        try:
            papaya = Papaya.init(
                api_key="synthetic-loopback-key",
                endpoint="http://127.0.0.1:%s/api/v1/ingest/traces" % server.server_port,
                request_timeout=request_budget, max_attempts=1,
            )
            self.addCleanup(papaya.close, deadline=0.05)
            _trace(papaya, "trickled-header-budget")
            start = time.monotonic()
            result = papaya.flush(timeout=1)
            elapsed = time.monotonic() - start
            self.assertTrue(handler_entered.is_set())
            self.assertLessEqual(elapsed, request_budget + 0.25)
            self.assertNotEqual(result["status"], "sent")
            self.assertEqual(result["sending"]["traces"] + result["terminalFailed"]["traces"], 1)
            self.assertEqual(result["targetReceipts"][0]["code"], "request_timeout")
            self.assertEqual(result["sent"]["traces"], 0)
            self.assertEqual(len(_retained_bodies(papaya)), 1)
            self.assertTrue(client_disconnected.wait(0.2), "The native HTTP socket remained alive beyond its request deadline")
            _wait(lambda: papaya.export_status()["sending"]["traces"] == 0, timeout=0.2)
        finally:
            server.shutdown()
            server.server_close()
            serving.join(timeout=0.5)

    def test_close_drains_completed_backlog_through_single_pending_batch_slot(self):
        observed = []

        def transport(endpoint, headers, body):
            observed.extend(trace["traceId"] for trace in json.loads(body)["traces"])
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(transport=transport, max_batch_bytes=1, max_pending_batches=1)
        for index in range(3):
            _trace(papaya, "close-backlog-" + str(index))
        result = papaya.close(deadline=1)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["sent"]["traces"], 3)
        self.assertEqual(result["rejected"]["traces"], 0)
        self.assertEqual(result["active"]["traces"], 0)
        self.assertEqual(observed, ["close-backlog-0", "close-backlog-1", "close-backlog-2"])

    def test_close_of_terminal_only_or_incomplete_capture_reports_failure(self):
        terminal = self.make_papaya(transport=lambda *_args: (413, '{}'))
        _trace(terminal, "terminal-only-close")
        terminal.flush()
        closed = terminal.close(deadline=0.05)
        self.assertEqual(closed["status"], "failed")
        self.assertEqual(closed["terminalFailed"]["traces"], 1)
        incomplete = self.make_papaya(transport=_accepted)
        incomplete.start_trace({"traceId": "unfinished-at-close"})
        closed = incomplete.close(deadline=0.05)
        self.assertEqual(closed["status"], "failed")
        self.assertEqual(closed["active"]["unfinishedTraces"], 1)
        self.assertEqual(closed["sent"]["traces"], 0)

    def test_close_deadline_stops_future_retries_and_keeps_accounting_stable(self):
        calls = []

        def transport(_endpoint, _headers, body):
            calls.append(body)
            return 503, '{}'

        papaya = self.make_papaya(transport=transport, retry_base=0.12, retry_max=0.12, retry_jitter=0)
        _trace(papaya, "closed-retry-retained")
        papaya.flush()
        before_close = time.monotonic()
        closed = papaya.close(deadline=0.02)
        self.assertLessEqual(time.monotonic() - before_close, 0.27)
        self.assertEqual(closed["status"], "failed")
        self.assertEqual(len(calls), 1)
        initial_counts = {name: closed[name] for name in ("admitted", "sent", "rejected", "captureFailed", "pending")}
        time.sleep(0.15)
        self.assertEqual(len(calls), 1)
        for _ in range(2):
            again = papaya.close(deadline=0.01)
            self.assertEqual({name: again[name] for name in initial_counts}, initial_counts)
        self.assertEqual(_retained_bodies(papaya), [calls[0]])

    def test_close_retains_inflight_and_queued_bodies_without_starting_later_sends(self):
        entered = threading.Event()
        release = threading.Event()
        bodies = []

        def transport(endpoint, headers, body):
            bodies.append(body)
            entered.set()
            release.wait(2)
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(transport=transport, max_batch_bytes=1)
        try:
            for index in range(3):
                _trace(papaya, "close-inflight-" + str(index))
            papaya.flush(timeout=0.02)
            self.assertTrue(entered.is_set())
            closed = papaya.close(deadline=0.02)
            self.assertEqual(closed["sending"]["traces"], 1)
            self.assertEqual(closed["inFlight"]["traces"], 1)
            self.assertEqual(closed["queued"]["traces"], 2)
            self.assertEqual(len(_retained_bodies(papaya)), 3)
            release.set()
            _wait(lambda: papaya.export_status()["sending"]["traces"] == 0)
            time.sleep(0.03)
            self.assertEqual(len(bodies), 1)
            status = papaya.export_status()
            self.assertEqual(status["sent"]["traces"], 1)
            self.assertEqual(status["pending"]["traces"], 2)
            self.assertEqual(len(_retained_bodies(papaya)), 2)
            again = papaya.close(deadline=0.01)
            self.assertEqual(again["pending"], status["pending"])
            self.assertEqual(len(bodies), 1)
        finally:
            release.set()

    def test_uncancellable_custom_transport_flush_timeout_retains_ownership_without_duplicate_attempt(self):
        entered = threading.Event()
        release = threading.Event()
        bodies = []

        def transport(endpoint, headers, body):
            bodies.append(body)
            entered.set()
            release.wait(2)
            return _accepted(endpoint, headers, body)

        papaya = self.make_papaya(
            transport=transport, retry_base=0.01, retry_max=0.01, retry_jitter=0,
        )
        try:
            _trace(papaya, "owned-uncancellable-timeout")
            before = time.monotonic()
            result = papaya.flush(timeout=0.03)
            self.assertLessEqual(time.monotonic() - before, 0.28)
            self.assertTrue(entered.is_set())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["sent"]["traces"], 0)
            self.assertEqual(result["sending"]["traces"], 1)
            self.assertTrue(result["timedOut"])
            self.assertEqual(result["targetReceipts"][0]["attempts"], 1)
            self.assertEqual(_retained_bodies(papaya), bodies)
            time.sleep(0.05)
            self.assertEqual(len(bodies), 1)
            closed = papaya.close(deadline=0.01)
            self.assertEqual(closed["inFlight"]["traces"], 1)
            release.set()
            _wait(lambda: papaya.export_status()["sending"]["traces"] == 0)
            known = papaya.export_status()
            self.assertEqual(known["sent"]["traces"], 1)
            self.assertEqual(known["receipts"][-1]["state"], "sent")
            self.assertEqual(known["receipts"][-1]["attempts"], 1)
            self.assertEqual(len(bodies), 1)
        finally:
            release.set()

    def test_exporter_worker_crash_is_explicit_and_preserves_frozen_content(self):
        bodies = []

        def transport(_endpoint, _headers, body):
            bodies.append(body)
            raise SystemExit("synthetic worker failure")

        papaya = self.make_papaya(transport=transport)
        _trace(papaya, "worker-crash-retained")
        result = papaya.flush(timeout=0.5)
        self.assertTrue(result["workerFailed"])
        self.assertEqual(result["sent"]["traces"], 0)
        self.assertEqual(_retained_bodies(papaya), bodies)
        self.assertTrue(result["diagnostics"])

    def test_retries_are_bounded_by_attempts_and_age_with_terminal_receipt(self):
        for limits in ({"max_attempts": 2}, {"max_retry_age": 0.03}):
            with self.subTest(limits=limits):
                calls = []

                def transport(_endpoint, _headers, body):
                    calls.append(body)
                    return 503, '{}'

                papaya = self.make_papaya(
                    transport=transport, retry_base=0.02, retry_max=0.02, retry_jitter=0, **limits,
                )
                _trace(papaya, "bounded-retry")
                papaya.flush()
                _wait(lambda: papaya.export_status()["terminalFailed"]["traces"] == 1)
                count = len(calls)
                time.sleep(0.04)
                self.assertEqual(len(calls), count)
                if "max_attempts" in limits:
                    self.assertEqual(len(calls), 2)
                self.assertEqual(papaya.export_status()["sent"]["traces"], 0)
                self.assertEqual(_retained_bodies(papaya), [calls[0]])

    def test_pending_bytes_and_process_rss_remain_within_declared_memory_budget(self):
        budget = FIXTURE["memory"]["pendingBodyBudgetBytes"]
        rss_scale = 1 if sys.platform == "darwin" else 1024
        baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_scale
        papaya = self.make_papaya(
            transport=lambda *_args: (413, '{}'), capture="full", max_batch_bytes=1,
            max_pending_bytes=budget, max_pending_batches=128, max_active_traces=128, max_active_bytes=budget,
        )
        for index in range(100):
            try:
                _trace(papaya, "memory-" + str(index), "x" * 65536)
            except PapayaCaptureError:
                pass
            papaya.flush(timeout=0.1)
        result = papaya.export_status()
        retained = _retained_bodies(papaya)
        self.assertLessEqual(sum(len(body) for body in retained), budget)
        self.assertLessEqual(result["highWater"]["bytes"], budget)
        self.assertLessEqual(result["active"]["bytes"], budget)
        self.assertEqual(result["terminalFailed"]["traces"] + result["rejected"]["traces"] + result["active"]["completedTraces"], 100)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_scale
        self.assertLessEqual(max(0, peak - baseline), FIXTURE["memory"]["processGrowthBudgetBytes"])

    def test_provider_argument_response_and_exception_identity_survive_capture_serialization_failure(self):
        class Unserializable:
            def __str__(self):
                raise ValueError("synthetic serialization fault")

        class UnserializableProviderError(RuntimeError):
            def __str__(self):
                raise ValueError("synthetic exception serialization fault")

        for fault in ("input", "output", "exception"):
            with self.subTest(fault=fault):
                calls = []
                messages = [{"role": "user", "content": Unserializable() if fault == "input" else "synthetic"}]
                result = {"value": Unserializable()} if fault == "output" else {"value": "same provider result"}
                provider_error = UnserializableProviderError()

                class Provider:
                    def complete(self, *, model, messages):
                        calls.append((model, messages))
                        if fault == "exception":
                            raise provider_error
                        return result

                papaya = self.make_papaya(transport=_accepted)
                client = papaya.openai(Provider())
                if fault == "exception":
                    with self.assertRaises(UnserializableProviderError) as raised:
                        client.complete(model="synthetic-model", messages=messages)
                    self.assertIs(raised.exception, provider_error)
                else:
                    self.assertIs(client.complete(model="synthetic-model", messages=messages), result)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "synthetic-model")
                self.assertIs(calls[0][1], messages)
                status = papaya.export_status()
                self.assertGreaterEqual(status["captureFailed"]["traces"], 1)
                self.assertTrue(status["diagnostics"])

    def test_provider_identity_and_context_body_survive_admission_failure(self):
        calls = []
        response = object()
        provider_error = RuntimeError("synthetic provider failure")
        messages = [{"role": "user", "content": "synthetic unchanged input"}]

        class Provider:
            def complete(self, *, messages, fail=False):
                calls.append(messages)
                if fail:
                    raise provider_error
                return response

        papaya = self.make_papaya(transport=_accepted, max_active_traces=1)
        active = papaya.start_trace({"traceId": "consume-active-slot"})
        client = papaya.openai(Provider())
        self.assertIs(client.complete(messages=messages), response)
        with self.assertRaises(RuntimeError) as raised:
            client.complete(messages=messages, fail=True)
        self.assertIs(raised.exception, provider_error)
        entered = []
        with papaya.run({"workflowKey": "rejected-scope"}):
            entered.append("customer body still executed")
            self.assertIs(client.complete(messages=messages), response)
        self.assertEqual(entered, ["customer body still executed"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(argument is messages for argument in calls))
        self.assertGreaterEqual(papaya.export_status()["rejected"]["traces"], 3)
        papaya.finish_trace(active, "success")


class PapayaExporterAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_export_preserves_heartbeat_default_executor_and_bounded_cancel_close(self):
        config = FIXTURE["concurrency"]
        for repetition in range(config["repetitions"]):
            with self.subTest(repetition=repetition):
                entered = threading.Event()
                release = threading.Event()
                transport_threads = []

                def transport(endpoint, headers, body):
                    transport_threads.append(threading.current_thread().name)
                    entered.set()
                    release.wait(3)
                    return _accepted(endpoint, headers, body)

                papaya = Papaya.init(
                    api_key="synthetic-test-key", transport=transport, max_batch_bytes=1,
                    endpoint="https://papaya.example/api/v1/ingest/traces",
                )
                # Framework imports can leave old cycles for Python 3.14's first
                # full collection. Clear preceding-test garbage before timing;
                # GC stays enabled throughout the measured capture/export work.
                gc.collect()
                lags = []
                stop = False

                async def heartbeat():
                    interval = config["heartbeatIntervalSeconds"]
                    while not stop:
                        before = asyncio.get_running_loop().time()
                        await asyncio.sleep(interval)
                        lags.append(max(0, (asyncio.get_running_loop().time() - before - interval) * 1000))

                async def capture(index):
                    await asyncio.sleep(0)
                    _trace(papaya, "async-%s-%s" % (repetition, index), "x" * config["payloadCharacters"])

                beat = asyncio.create_task(heartbeat())
                flushing = None
                try:
                    await asyncio.gather(*(capture(index) for index in range(config["captures"])))
                    flushing = asyncio.create_task(papaya.aflush(timeout=2))
                    deadline = asyncio.get_running_loop().time() + 1
                    while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(0.002)
                    self.assertTrue(entered.is_set())
                    before_executor = asyncio.get_running_loop().time()
                    value = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, lambda: "unrelated executor work"),
                        timeout=config["executorDeadlineSeconds"],
                    )
                    self.assertEqual(value, "unrelated executor work")
                    self.assertLess(asyncio.get_running_loop().time() - before_executor, 1)
                    await asyncio.sleep(0.05)
                    flushing.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await flushing
                    before_close = time.monotonic()
                    result = await papaya.aclose(deadline=0.05)
                    self.assertLessEqual(time.monotonic() - before_close, 0.3)
                    self.assertTrue(result["closed"])
                    self.assertEqual(result["sent"]["traces"], 0)
                    accounted = sum(result[name]["traces"] for name in ("sent", "queued", "retrying", "sending", "terminalFailed"))
                    self.assertEqual(accounted, config["captures"])
                    self.assertEqual(result["sending"]["traces"], 1)
                    self.assertEqual(result["rejected"]["traces"], 0)
                    self.assertGreaterEqual(len(lags), 5)
                    sorted_lags = sorted(lags)
                    p95 = sorted_lags[min(len(sorted_lags) - 1, int(len(sorted_lags) * 0.95))]
                    self.assertLessEqual(p95, config["p95LagBudgetMs"])
                    self.assertLessEqual(max(lags), config["maxLagBudgetMs"])
                    self.assertTrue(all("papaya" in name.lower() for name in transport_threads))
                finally:
                    release.set()
                    stop = True
                    await beat
                    if flushing is not None and not flushing.done():
                        flushing.cancel()
                    papaya.close(deadline=0.1)


if __name__ == "__main__":
    unittest.main()
