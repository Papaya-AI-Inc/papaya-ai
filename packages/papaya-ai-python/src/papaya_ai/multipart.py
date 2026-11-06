"""Explicit, lossless multipart delivery of one immutable native trace batch.

Only a validated landed receipt acknowledges delivery. A retained batch owns its
session metadata; retries query status before repeating an ambiguous operation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

from .exporter import _default_transport

PapayaMultipartRequester = Callable[[str, str, dict[str, str], bytes], Any]
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _PapayaMultipartFailure(RuntimeError):
    _papaya_export_error = True

    def __init__(self, code: str, status: int = 400, response_headers: Any = None):
        self.code, self.http_status, self.response_headers = code, status, response_headers
        super().__init__(code)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


class _PapayaMultipartTransport:
    def __init__(self, timeout: float, requester: PapayaMultipartRequester | None = None,
                 idle_ttl_seconds: int | None = None):
        self.timeout, self.requester = timeout, requester
        self.idle_ttl_seconds = idle_ttl_seconds

    def _request(self, method: str, url: str, headers: dict[str, str], body: bytes = b"") -> Any:
        if self.requester is not None:
            return self.requester(method, url, headers, body)
        # A socket timeout alone does not bound trickled response headers. One
        # owned I/O helper plus an abort watchdog enforces this request's budget.
        # If DNS cannot be interrupted, wait for this owner rather than starting
        # duplicate work. Exporter flush/close waiting remains independently bounded.
        done = threading.Event()
        lock = threading.Lock()
        outcome: dict[str, Any] = {}
        deadline = time.monotonic() + self.timeout

        def register_abort(abort: Callable[[], None]) -> None:
            with lock:
                outcome["abort"] = abort
                if outcome.get("timed_out"):
                    abort()

        def invoke() -> None:
            try:
                outcome["response"] = _default_transport(url, headers, body, self.timeout,
                                                          register_abort, method=method)
            except BaseException as error:
                outcome["error"] = error
            finally:
                outcome["finished_at"] = time.monotonic()
                done.set()

        threading.Thread(target=invoke, name="papaya-multipart-io", daemon=True).start()
        if not done.wait(max(0, deadline - time.monotonic())):
            with lock:
                outcome["timed_out"] = True
                if "abort" in outcome:
                    outcome["abort"]()
            done.wait()
        if outcome.get("timed_out") or outcome.get("finished_at", deadline + 1) > deadline:
            # Even a late successful response remains ambiguous. The next export
            # attempt reads server status before retrying any part or completion.
            raise TimeoutError("multipart_request_timeout")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["response"]

    def _json_request(self, method: str, url: str, headers: dict[str, str],
                      body: bytes = b"", *, binary: bool = False) -> dict[str, Any]:
        request_headers = dict(headers)
        request_headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
        request_headers["Content-Length"] = str(len(body))
        response = self._request(method, url, request_headers, body)
        if (not isinstance(response, tuple) or len(response) not in (2, 3) or
                type(response[0]) is not int or not 100 <= response[0] <= 599):
            raise _PapayaMultipartFailure("multipart_invalid_response")
        status, text = response[:2]
        response_headers = response[2] if len(response) == 3 else None
        if not 200 <= status < 300:
            # A completion retry can meet the original, still-owned operation.
            # This precise server lease code is transient; integrity/policy
            # conflicts keep their terminal 409 meaning. Never expose or infer
            # arbitrary response text in export diagnostics.
            if (status == 409 and method == "POST" and url.endswith("/complete") and
                    isinstance(text, str) and len(text.encode("utf-8")) <= 65536):
                try:
                    error = json.loads(text)
                except (ValueError, TypeError):
                    error = None
                if isinstance(error, dict) and error.get("error") == "trace_upload_operation_in_progress":
                    raise _PapayaMultipartFailure("multipart_operation_in_progress", 503, response_headers)
            raise _PapayaMultipartFailure("multipart_http_error", status, response_headers)
        if not isinstance(text, str) or len(text.encode("utf-8")) > 65536:
            raise _PapayaMultipartFailure("multipart_invalid_response")
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            raise _PapayaMultipartFailure("multipart_invalid_response") from None
        if not isinstance(value, dict):
            raise _PapayaMultipartFailure("multipart_invalid_response")
        return value

    @staticmethod
    def _base(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith("/traces")):
            raise _PapayaMultipartFailure("multipart_invalid_endpoint")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:-len("/traces")], "", ""))

    def _capability(self, base: str, headers: dict[str, str], request: Callable[..., Any]) -> dict[str, Any]:
        try:
            config = request("GET", base + "/config", headers)
        except _PapayaMultipartFailure as error:
            if error.http_status in (404, 405):
                raise _PapayaMultipartFailure("multipart_unsupported", error.http_status) from None
            raise
        features = config.get("features")
        capability = features.get("multipart") if isinstance(features, dict) else None
        if not isinstance(capability, dict) or capability.get("enabled") is not True:
            raise _PapayaMultipartFailure("multipart_unsupported")
        limits = (("maxBytes", 32 * 1024 * 1024), ("partSizeBytes", 5 * 1024 * 1024),
                  ("maxParts", 7), ("minIdleTtlSeconds", 3600), ("maxIdleTtlSeconds", 3600))
        if any(type(capability.get(key)) is not int or not 0 < capability[key] <= maximum
               for key, maximum in limits):
            raise _PapayaMultipartFailure("multipart_invalid_capability")
        if capability["minIdleTtlSeconds"] > capability["maxIdleTtlSeconds"]:
            raise _PapayaMultipartFailure("multipart_invalid_capability")
        if self.idle_ttl_seconds is not None and not (capability["minIdleTtlSeconds"] <=
                self.idle_ttl_seconds <= capability["maxIdleTtlSeconds"]):
            raise _PapayaMultipartFailure("multipart_idle_ttl_out_of_range")
        return capability

    @staticmethod
    def _status(value: dict[str, Any], item: Any, state: dict[str, Any],
                parts: list[dict[str, Any]], part_size: int) -> dict[int, dict[str, Any]]:
        upload_id = value.get("uploadId")
        if (not isinstance(upload_id, str) or not upload_id or len(upload_id) > 256 or
                value.get("batchId") != item.batch_id or value.get("byteLength") != len(item.body) or
                value.get("sha256") != state["sha256"] or value.get("partSizeBytes") != part_size or
                (state.get("uploadId") is not None and state["uploadId"] != upload_id)):
            raise _PapayaMultipartFailure("multipart_session_mismatch")
        state["uploadId"] = upload_id
        server_state = value.get("state")
        if server_state in ("failed", "expired", "aborted"):
            raise _PapayaMultipartFailure("multipart_" + server_state, 410)
        if server_state not in ("uploading", "assembling", "validating", "publishing", "landed"):
            raise _PapayaMultipartFailure("multipart_invalid_state")
        receipts = value.get("parts")
        if not isinstance(receipts, list) or len(receipts) > len(parts):
            raise _PapayaMultipartFailure("multipart_invalid_parts")
        verified: dict[int, dict[str, Any]] = {}
        for receipt in receipts:
            if not isinstance(receipt, dict) or type(receipt.get("partNumber")) is not int:
                raise _PapayaMultipartFailure("multipart_invalid_parts")
            number = receipt["partNumber"]
            if number < 1 or number > len(parts) or number in verified or any(
                    receipt.get(key) != parts[number - 1][key] for key in ("byteLength", "sha256")):
                raise _PapayaMultipartFailure("multipart_part_conflict", 409)
            verified[number] = receipt
        return verified

    @staticmethod
    def _landed(value: dict[str, Any], item: Any, state: dict[str, Any]) -> Any:
        receipt = value.get("receipt")
        if value.get("state") != "landed":
            return None
        if (not isinstance(receipt, dict) or receipt.get("uploadId") != state["uploadId"] or
                receipt.get("batchId") != item.batch_id or type(receipt.get("accepted")) is not int or
                receipt["accepted"] != len(item.trace_ids) or type(receipt.get("rejected")) is not int or
                receipt["rejected"] != 0 or not isinstance(receipt.get("warnings"), list) or
                not isinstance(receipt.get("rawSha256"), str) or not _SHA256.fullmatch(receipt["rawSha256"]) or
                type(receipt.get("rawBytes")) is not int or receipt["rawBytes"] <= 0 or
                not isinstance(receipt.get("landedAt"), str) or not receipt["landedAt"]):
            raise _PapayaMultipartFailure("multipart_invalid_landing_receipt")
        # rawSha256/rawBytes describe post-policy raw, which may differ from the
        # submitted batch. The session sha256 separately proves upload integrity.
        state.update({"rawSha256": receipt["rawSha256"], "rawBytes": receipt["rawBytes"]})
        return 202, json.dumps({"accepted": receipt["accepted"], "rejected": 0})

    def _papaya_send_batch(self, item: Any, endpoint: str, headers: dict[str, str]) -> Any:
        def request(method: str, url: str, request_headers: dict[str, str],
                    body: bytes = b"", *, binary: bool = False) -> dict[str, Any]:
            if item.stop_new_requests:
                raise _PapayaMultipartFailure("multipart_exporter_closed", 408)
            try:
                return self._json_request(method, url, request_headers, body, binary=binary)
            except TimeoutError:
                item.attempt_timed_out = True
                raise _PapayaMultipartFailure("multipart_request_timeout", 408) from None

        if item.stop_new_requests:
            raise _PapayaMultipartFailure("multipart_exporter_closed", 408)
        base = self._base(endpoint)
        capability = self._capability(base, headers, request)
        size, part_size = len(item.body), capability["partSizeBytes"]
        count = math.ceil(size / part_size)
        if size > capability["maxBytes"] or count > capability["maxParts"]:
            raise _PapayaMultipartFailure("multipart_max_bytes_exceeded", 413)
        state = item.multipart_state
        digest = hashlib.sha256(item.body).hexdigest()
        if state.get("sha256", digest) != digest:
            raise _PapayaMultipartFailure("multipart_frozen_body_changed", 409)
        state.update({"sha256": digest, "partCount": count})
        parts = [{"partNumber": index + 1, "byteLength": min(part_size, size - index * part_size),
                  "sha256": hashlib.sha256(memoryview(item.body)[index * part_size:(index + 1) * part_size]).hexdigest()}
                 for index in range(count)]
        if "uploadId" in state:
            url = base + "/trace-uploads/" + quote(state["uploadId"], safe="")
            value = request("GET", url, headers)
        else:
            create = {"schemaVersion": "2026-06-05", "batchId": item.batch_id,
                      "byteLength": size, "sha256": digest}
            if self.idle_ttl_seconds is not None:
                create["idleTtlSeconds"] = self.idle_ttl_seconds
            value = request("POST", base + "/trace-uploads", headers, _encoded(create))
            url = base + "/trace-uploads/" + quote(str(value.get("uploadId", "")), safe="")
        verified = self._status(value, item, state, parts, part_size)
        landed = self._landed(value, item, state)
        if landed is not None:
            return landed
        if value["state"] != "uploading" and len(verified) != len(parts):
            raise _PapayaMultipartFailure("multipart_operation_in_progress", 503)
        # Serial parts bound transient body copies to one advertised part. Never
        # reduce content or change identity to work around a server rejection.
        for part in parts:
            number = part["partNumber"]
            if number in verified:
                continue
            offset = (number - 1) * part_size
            part_headers = {**headers, "X-Papaya-Part-Sha256": part["sha256"]}
            result = request("PUT", url + "/parts/" + str(number), part_headers,
                                        item.body[offset:offset + part["byteLength"]], binary=True)
            # A part reply may be a part receipt or a full session status.
            if "state" in result:
                refreshed = self._status(result, item, state, parts, part_size)
                if number not in refreshed:
                    raise _PapayaMultipartFailure("multipart_part_not_acknowledged")
            elif any(result.get(key) != part[key] for key in ("partNumber", "byteLength", "sha256")):
                raise _PapayaMultipartFailure("multipart_invalid_part_receipt")
        # A retryable server failure releases its lease while preserving the
        # assembling/validating/publishing state. Status alone cannot resume that
        # work: retry the identical complete manifest after verifying every part.
        # An active original owner returns the specific retryable lease conflict.
        value = request("POST", url + "/complete", headers, _encoded({"parts": parts}))
        # Complete can return the landed receipt directly or the complete state.
        if "state" not in value and "landedAt" in value:
            value = {"state": "landed", "receipt": value}
        else:
            self._status(value, item, state, parts, part_size)
        landed = self._landed(value, item, state)
        if landed is None:
            raise _PapayaMultipartFailure("multipart_operation_in_progress", 503)
        return landed
