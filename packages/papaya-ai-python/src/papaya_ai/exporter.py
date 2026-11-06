"""Bounded, process-local export ownership and a dedicated upload scheduler."""
from __future__ import annotations

import asyncio
import http.client
import hashlib
import json
import math
import random
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit


class PapayaCaptureError(RuntimeError):
    """An explicit telemetry admission or serialization failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _count() -> dict[str, int]:
    return {"batches": 0, "traces": 0, "bytes": 0}


class _PapayaExportBatch:
    def __init__(self, batch_id: str, trace_ids: list[str], body: bytes):
        self.batch_id = batch_id
        self.trace_ids = tuple(trace_ids)
        self.body = body
        self.enqueued_at = time.monotonic()
        self.next_attempt = self.enqueued_at
        self.attempts = 0
        self.completed_attempts = 0
        self.state = "queued"
        self.code = "queued"
        self.http_status: int | None = None
        self.accepted: int | None = None
        self.rejected: int | None = None
        self.attempt_timed_out = False
        # Small protocol metadata belongs to the retained frozen batch, so
        # reconfiguration cannot forget an ambiguous multipart session.
        self.multipart_state: dict[str, Any] = {}
        self.stop_new_requests = False

    def receipt(self) -> dict[str, Any]:
        # Diagnostic storage has a byte bound as well as a row bound. Full
        # identities and content always remain in the retained frozen body.
        trace_ids = [value if isinstance(value, str) and len(value) <= 256 else
                     "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()
                     for value in self.trace_ids[:64]]
        return {"batchId": self.batch_id, "traceIds": trace_ids,
                "traceCount": len(self.trace_ids), "traceIdsOmitted": max(0, len(self.trace_ids) - 64),
                "bodyBytes": len(self.body), "attempts": self.attempts,
                "state": self.state, "code": self.code,
                "deadlineExceeded": self.attempt_timed_out,
                **({"httpStatus": self.http_status} if self.http_status is not None else {}),
                **({"accepted": self.accepted, "rejected": self.rejected}
                   if self.accepted is not None else {}),
                **({"multipart": {key: value for key, value in self.multipart_state.items()
                    if key in ("uploadId", "sha256", "partCount", "rawSha256", "rawBytes")}}
                   if self.multipart_state else {})}


def _default_transport(endpoint: str, headers: dict[str, str], body: bytes,
                       timeout: float = 10.0,
                       register_abort: Callable[[Callable[[], None]], None] | None = None,
                       method: str = "POST",
                       ) -> tuple[int, str, dict[str, str]]:
    """One request, no redirects, a monotonic socket/read budget and 64KiB response cap."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_endpoint")
    deadline = time.monotonic() + timeout
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    aborted = threading.Event()
    connected_socket: list[Any] = [None]

    def abort() -> None:
        aborted.set()
        active_socket = connected_socket[0] or connection.sock
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active_socket.close()

    if register_abort is not None:
        register_abort(abort)
    response: Any = None

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("request_timeout")
        if connection.sock is not None:
            connection.sock.settimeout(budget)
        return budget

    try:
        connection.connect()
        connected_socket[0] = connection.sock
        if aborted.is_set():
            abort()
            raise TimeoutError("request_timeout")
        remaining()
        connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), body, headers)
        remaining()
        response = connection.getresponse()
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining()
            chunk = response.read1(min(8192, 65537 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 65536:
                raise ValueError("response_too_large")
        remaining()
        retry_after = response.getheader("Retry-After")
        return response.status, b"".join(chunks).decode("utf-8", errors="replace"), (
            {"Retry-After": retry_after} if retry_after is not None else {})
    finally:
        if response is not None:
            response.close()
        connection.close()


def _default_transport_for(timeout: float) -> Callable[..., Any]:
    def transport(endpoint: str, headers: dict[str, str], body: bytes) -> Any:
        return _default_transport(endpoint, headers, body, timeout)
    def invoke(endpoint: str, headers: dict[str, str], body: bytes,
               register_abort: Callable[[Callable[[], None]], None]) -> Any:
        return _default_transport(endpoint, headers, body, timeout, register_abort)
    # Only the SDK's known transport uses the request watchdog. Custom callbacks
    # keep their contract and may own uninterruptible work until they return.
    transport._papaya_request_timeout = timeout  # type: ignore[attr-defined]
    transport._papaya_invoke = invoke  # type: ignore[attr-defined]
    return transport


class _PapayaExporter:
    def __init__(self, *, api_key: str | None, endpoint: str, transport: Callable[..., Any],
                 max_pending_batches: int, max_pending_bytes: int, max_retry_age: float,
                 max_attempts: int, retry_base: float, retry_max: float, retry_jitter: float,
                 flush_timeout: float, diagnostic_limit: int, user_agent: str):
        self.condition = threading.Condition(threading.RLock())
        self.api_key, self.endpoint, self.transport = api_key, endpoint, transport
        self.max_pending_batches, self.max_pending_bytes = max_pending_batches, max_pending_bytes
        self.max_retry_age, self.max_attempts = max_retry_age, max_attempts
        self.retry_base, self.retry_max, self.retry_jitter = retry_base, retry_max, retry_jitter
        self.flush_timeout = flush_timeout
        self.user_agent = user_agent
        self._batches: list[_PapayaExportBatch] = []
        self._cumulative = {key: _count() for key in ("admitted", "sent", "rejected", "captureFailed")}
        self._receipts: deque[dict[str, Any]] = deque(maxlen=diagnostic_limit)
        self._diagnostics: deque[dict[str, Any]] = deque(maxlen=diagnostic_limit)
        self._waiters: list[tuple[Any, Any, Callable[[], bool], Callable[[], dict[str, Any]]]] = []
        self._thread: threading.Thread | None = None
        self.auth_paused = False
        self.closed = False
        self.worker_failed = False
        self._high_water = {"batches": 0, "bytes": 0, "activeTraces": 0, "activeBytes": 0}

    def _add(self, name: str, traces: int, size: int, batches: int = 1) -> None:
        target = self._cumulative[name]
        target["batches"] += batches
        target["traces"] += traces
        target["bytes"] += size

    def record(self, code: str, *, traces: int = 1, size: int = 0,
               capture: bool = False) -> None:
        with self.condition:
            self._add("captureFailed" if capture else "rejected", traces, size, 0)
            self._diagnostics.append({"code": code, "traces": traces, "bytes": size})
            self._changed()

    def update_active(self, traces: int, size: int) -> None:
        with self.condition:
            self._high_water["activeTraces"] = max(self._high_water["activeTraces"], traces)
            self._high_water["activeBytes"] = max(self._high_water["activeBytes"], size)

    def admit(self, batch_id: str, trace_ids: list[str], body: bytes) -> bool:
        with self.condition:
            size = len(body)
            pending_bytes = sum(len(item.body) for item in self._batches)
            code = ("exporter_closed" if self.closed else "worker_crash" if self.worker_failed else
                    "pending_batches_capacity" if len(self._batches) >= self.max_pending_batches else
                    "pending_bytes_capacity" if pending_bytes + size > self.max_pending_bytes else None)
            if code:
                # The caller still owns the completed trace snapshots. This is
                # blocked transfer of existing work, not rejection/discard of
                # another trace on each repeated flush or close.
                if not self._diagnostics or self._diagnostics[-1]["code"] != code:
                    self._diagnostics.append({"code": code, "traces": len(trace_ids), "bytes": size})
                self._changed()
                return False
            self._batches.append(_PapayaExportBatch(batch_id, trace_ids, body))
            self._add("admitted", len(trace_ids), size)
            self._high_water["batches"] = max(self._high_water["batches"], len(self._batches))
            self._high_water["bytes"] = max(self._high_water["bytes"], pending_bytes + size)
            self._ensure_worker()
            self._changed()
            return True

    def configure(self, *, api_key: str | None, endpoint: str,
                  transport: Callable[..., Any]) -> None:
        with self.condition:
            self.api_key, self.endpoint, self.transport = api_key, endpoint, transport
            self.auth_paused = False
            self._ensure_worker()
            self._changed()

    def _ensure_worker(self) -> None:
        if self._thread is None and self._batches and not self.closed and not self.worker_failed and self.api_key and not self.auth_paused:
            self._thread = threading.Thread(target=self._run, name="papaya-exporter", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            result: dict[str, Any] = {key: dict(value) for key, value in self._cumulative.items()}
            for state in ("queued", "retrying", "sending", "terminalFailed"):
                items = [item for item in self._batches if item.state == state]
                result[state] = {"batches": len(items), "traces": sum(len(item.trace_ids) for item in items),
                                 "bytes": sum(len(item.body) for item in items)}
            result.update({"receipts": list(self._receipts), "diagnostics": list(self._diagnostics),
                           "highWater": dict(self._high_water), "authPaused": self.auth_paused,
                           "closed": self.closed, "workerFailed": self.worker_failed,
                           "pending": {"batches": len(self._batches),
                                       "traces": sum(len(item.trace_ids) for item in self._batches),
                                       "bytes": sum(len(item.body) for item in self._batches)}})
            return result

    def _changed(self) -> None:
        self.condition.notify_all()
        for loop, future, ready, result in list(self._waiters):
            if future.done():
                self._waiters.remove((loop, future, ready, result))
            elif ready():
                self._waiters.remove((loop, future, ready, result))
                value = result()
                try:
                    loop.call_soon_threadsafe(self._resolve, future, value)
                except RuntimeError:
                    pass

    @staticmethod
    def _resolve(future: Any, value: Any) -> None:
        if not future.done():
            future.set_result(value)

    def targets(self) -> list[tuple[_PapayaExportBatch, int]]:
        with self.condition:
            return [(item, item.completed_attempts + 1) for item in self._batches]

    def _ready(self, targets: list[tuple[_PapayaExportBatch, int]], drain: bool) -> bool:
        return (self.worker_failed or self.auth_paused or not self.api_key or
                all(item.state in ("sent", "terminalFailed") if drain else
                    (item.state in ("sent", "terminalFailed") or item.completed_attempts >= attempt)
                    for item, attempt in targets))

    def _result(self, targets: list[tuple[_PapayaExportBatch, int]], timed_out: bool,
                drain: bool = False) -> dict[str, Any]:
        snapshot = self.snapshot()
        receipts = [item.receipt() for item, _ in targets]
        failed = [item for item, _ in targets if item.state != "sent"]
        if not targets:
            status, reason = "skipped", "empty"
        elif not self.api_key:
            status, reason = "skipped", "missing_api_key"
        elif failed:
            status, reason = "failed", "timeout" if timed_out else "auth_paused" if self.auth_paused else "undelivered"
        else:
            status, reason = "sent", None
        result: dict[str, Any] = {"status": status, "traceCount": sum(len(item.trace_ids) for item, _ in targets),
                                  **snapshot, "targetReceipts": receipts, "timedOut": timed_out}
        if reason:
            result["reason"] = reason
        if receipts:
            last = receipts[-1]
            if "httpStatus" in last:
                result["httpStatus"] = last["httpStatus"]
            if failed:
                result["errorCode"] = "auth_paused" if self.auth_paused else failed[0].code
        if drain:
            result["inFlight"] = snapshot["sending"]
        return result

    def wait(self, targets: list[tuple[_PapayaExportBatch, int]], timeout: float,
             drain: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while not self._ready(targets, drain):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            return self._result(targets, not self._ready(targets, drain), drain)

    async def await_targets(self, targets: list[tuple[_PapayaExportBatch, int]], timeout: float,
                            drain: bool = False) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self.condition:
            if self._ready(targets, drain) or timeout <= 0:
                return self._result(targets, not self._ready(targets, drain), drain)
            waiter = (loop, future, lambda: self._ready(targets, drain),
                      lambda: self._result(targets, False, drain))
            self._waiters.append(waiter)

        def expire() -> None:
            with self.condition:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                self._resolve(future, self._result(targets, not self._ready(targets, drain), drain))

        timer = loop.call_later(max(0.0, timeout), expire)
        try:
            return await future
        finally:
            timer.cancel()
            with self.condition:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def close_admission(self) -> None:
        with self.condition:
            self.closed = True
            for item in self._batches:
                item.stop_new_requests = True
            self._changed()

    def _terminal(self, item: _PapayaExportBatch, code: str) -> None:
        item.state, item.code = "terminalFailed", code
        self._receipts.append(item.receipt())
        self._diagnostics.append({"code": code, "traces": len(item.trace_ids), "bytes": len(item.body)})

    def _retry_after(self, headers: Any) -> float:
        if not isinstance(headers, dict):
            return 0.0
        value = next((value for key, value in headers.items() if str(key).lower() == "retry-after"), None)
        if value is None:
            return 0.0
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                result = (parsed - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError, AttributeError):
                return 0.0
        return max(0.0, result) if math.isfinite(result) else 0.0

    def _outcome(self, item: _PapayaExportBatch, status: int | None,
                 text: Any, headers: Any, code: str | None) -> None:
        item.completed_attempts = item.attempts
        item.http_status = status
        now = time.monotonic()
        if status is not None and 200 <= status < 300:
            # Never infer trace identity from warnings or arbitrary response text.
            ack: Any = None
            if isinstance(text, str) and len(text.encode("utf-8")) <= 65536:
                try:
                    ack = json.loads(text)
                except (ValueError, TypeError):
                    pass
            if isinstance(ack, dict) and ("accepted" in ack or "rejected" in ack):
                accepted, rejected = ack.get("accepted"), ack.get("rejected")
                valid = (type(accepted) is int and type(rejected) is int and accepted >= 0 and rejected >= 0 and
                         accepted + rejected == len(item.trace_ids))
                if valid:
                    item.accepted, item.rejected = accepted, rejected
                if not valid or accepted != len(item.trace_ids) or rejected != 0:
                    self._terminal(item, "partial_acceptance" if valid else "invalid_acknowledgment")
                    return
            item.state, item.code = "sent", "late_ack" if item.attempt_timed_out else "sent"
            self._add("sent", len(item.trace_ids), len(item.body))
            self._receipts.append(item.receipt())
            self._batches.remove(item)
            return
        if status in (401, 403):
            self.auth_paused = True
            item.state, item.code = "retrying", "auth_paused"
            item.next_attempt = now
            self._diagnostics.append({"code": "auth_paused", "traces": len(item.trace_ids), "bytes": len(item.body)})
            return
        if status is not None and (status == 413 or (400 <= status < 500 and status not in (408, 429)) or 300 <= status < 400):
            self._terminal(item, code or ("oversized_trace" if status == 413 else "http_terminal"))
            return
        if item.attempts >= self.max_attempts or now - item.enqueued_at >= self.max_retry_age:
            self._terminal(item, "request_timeout" if code == "request_timeout" else "retry_exhausted")
            return
        delay = min(self.retry_max, self.retry_base * (2 ** min(item.attempts - 1, 30)))
        delay *= random.uniform(1 - self.retry_jitter, 1 + self.retry_jitter)
        delay = max(delay, self._retry_after(headers))
        item.state, item.code = "retrying", code or "retryable_http_error"
        item.next_attempt = now + delay

    def _call_transport(self, item: _PapayaExportBatch, transport: Callable[..., Any],
                        endpoint: str, headers: dict[str, str]) -> Any:
        batch_transport = getattr(transport, "_papaya_send_batch", None)
        if batch_transport is not None:
            return batch_transport(item, endpoint, headers)
        timeout = getattr(transport, "_papaya_request_timeout", None)
        if timeout is None:
            return transport(endpoint, headers, item.body)
        # Socket timeouts do not impose an overall deadline on DNS or trickled
        # HTTP headers. One dedicated I/O owner lets the scheduler report that
        # deadline without canceling/forgetting an ambiguous in-flight request.
        # No subsequent request/helper is started until this owner returns.
        outcome: dict[str, Any] = {"done": False}

        def register_abort(abort: Callable[[], None]) -> None:
            with self.condition:
                outcome["abort"] = abort
                if item.attempt_timed_out:
                    abort()

        def invoke() -> None:
            try:
                outcome["response"] = transport._papaya_invoke(endpoint, headers, item.body, register_abort)
            except BaseException as error:
                outcome["error"] = error
            finally:
                with self.condition:
                    outcome["done"] = True
                    outcome["finished_at"] = time.monotonic()
                    self._changed()

        until = time.monotonic() + timeout
        threading.Thread(target=invoke, name="papaya-export-io", daemon=True).start()
        timed_out = False
        with self.condition:
            while not outcome["done"]:
                remaining = until - time.monotonic()
                if remaining <= 0 and not timed_out:
                    timed_out = True
                    item.attempt_timed_out = True
                    item.code = "request_timeout"
                    item.completed_attempts = item.attempts
                    self._diagnostics.append({"code": "request_timeout", "traces": len(item.trace_ids), "bytes": len(item.body)})
                    self._changed()
                    if "abort" in outcome:
                        outcome["abort"]()
                self.condition.wait(None if timed_out else max(0.001, remaining))
        if outcome.get("finished_at", until + 1) > until:
            item.attempt_timed_out = True
        if "error" in outcome:
            if item.attempt_timed_out:
                raise TimeoutError("request_timeout")
            raise outcome["error"]
        return outcome["response"]

    def _run(self) -> None:
        try:
            while True:
                with self.condition:
                    now = time.monotonic()
                    live = [item for item in self._batches if item.state in ("queued", "retrying")]
                    for item in live:
                        if now - item.enqueued_at >= self.max_retry_age:
                            self._terminal(item, "retry_exhausted")
                    live = [item for item in live if item.state != "terminalFailed"]
                    if self.closed or not live:
                        self._thread = None
                        self._changed()
                        return
                    if self.auth_paused or not self.api_key:
                        self._thread = None
                        self._changed()
                        return
                    item = next((item for item in live if item.next_attempt <= now), None)
                    if item is None:
                        self._changed()
                        wake = min((min(item.next_attempt, item.enqueued_at + self.max_retry_age) for item in live), default=None)
                        self.condition.wait(None if wake is None else max(0.001, wake - now))
                        continue
                    item.state, item.code = "sending", "sending"
                    item.attempt_timed_out = False
                    item.attempts += 1
                    endpoint, transport = self.endpoint, self.transport
                    headers = {"Authorization": "Bearer " + self.api_key,
                               "Content-Type": "application/json", "User-Agent": self.user_agent}
                    self._changed()
                status, response_text, response_headers, code = None, None, None, None
                try:
                    response = self._call_transport(item, transport, endpoint, headers)
                    if not isinstance(response, tuple) or len(response) not in (2, 3) or type(response[0]) is not int or not 100 <= response[0] <= 599:
                        raise ValueError("invalid_transport_response")
                    status, response_text = response[:2]
                    response_headers = response[2] if len(response) == 3 else None
                except Exception as error:
                    if getattr(error, "_papaya_export_error", False):
                        status, code = error.http_status, error.code
                        response_headers = error.response_headers
                    else:
                        code = "request_timeout" if isinstance(error, TimeoutError) and item.attempt_timed_out else "transport_exception"
                with self.condition:
                    self._outcome(item, status, response_text, response_headers, code)
                    if item.state == "retrying":
                        self._batches.remove(item)
                        self._batches.append(item)
                    self._changed()
        except BaseException:
            with self.condition:
                self.worker_failed = True
                for item in self._batches:
                    if item.state != "terminalFailed":
                        item.completed_attempts = item.attempts
                        self._terminal(item, "worker_crash")
                self._changed()
