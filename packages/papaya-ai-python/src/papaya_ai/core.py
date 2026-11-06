from __future__ import annotations

import base64
import contextvars
import inspect
import json
import math
import os
import platform
import re
import secrets
import threading
import time
from collections import deque
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Literal
from .version import __version__
from .exporter import PapayaCaptureError, _PapayaExporter, _default_transport_for
from .multipart import PapayaMultipartRequester, _PapayaMultipartTransport

CaptureMode = Literal["metadata", "redacted", "full"]
SpanStatus = Literal["success", "failed", "partial", "unknown"]
SpanKind = Literal[
    "workflow",
    "agent",
    "llm",
    "tool",
    "retrieval",
    "embedding",
    "reranker",
    "memory",
    "state_transition",
    "guardrail",
    "router",
    "human",
    "handoff",
    "evaluator",
    "other",
]
Transport = Callable[[str, dict[str, str], bytes], Any]
_EXPORT_UNSET = object()

_active_run: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("papaya_active_run", default=None)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    encoded = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
    return f"{prefix}_{encoded}"


def _json_default(value: Any) -> str:
    return str(value)


def _jsonable(value: Any, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    seen = seen or set()
    marker = id(value)
    if marker in seen:
        return "[Circular]"
    seen.add(marker)
    try:
        if isinstance(value, dict):
            return {str(key): _jsonable(item, seen) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(item, seen) for item in value]
        for method in ("model_dump", "dict", "to_dict"):
            fn = getattr(value, method, None)
            if not callable(fn):
                continue
            try:
                if method == "model_dump":
                    return _jsonable(fn(serialize_as_any=True), seen)
                return _jsonable(fn(), seen)
            except TypeError:
                if method == "model_dump":
                    try:
                        return _jsonable(fn(), seen)
                    except TypeError:
                        pass
        return str(value)
    finally:
        seen.remove(marker)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, default=_json_default, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_text(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"), allow_nan=False)


def _byte_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(_json_bytes(value))


_EMAIL_LOCAL_RUN = re.compile(r"[A-Z0-9._%+-]+", re.I)
_EMAIL_DOMAIN = re.compile(r"[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _redact_email_addresses(value: str) -> str:
    # Visit each maximal local-part run once. An unanchored `local+@domain`
    # pattern rescans every suffix of long no-@ strings (including media).
    if "@" not in value:
        return value
    parts: list[str] = []
    consumed = 0
    for local in _EMAIL_LOCAL_RUN.finditer(value):
        end = local.end()
        start = max(local.start(), consumed)
        if start >= end or end == len(value) or value[end] != "@":
            continue
        domain = _EMAIL_DOMAIN.match(value, end + 1)
        if domain is None:
            continue
        # A previous domain may consume the prefix of this local run. Match
        # its remaining suffix exactly as re.sub would after that match.
        parts.extend((value[consumed:start], "[redacted-email]"))
        consumed = domain.end()
    if not parts:
        return value
    parts.append(value[consumed:])
    return "".join(parts)


def _redact_string(value: str) -> str:
    value = _redact_email_addresses(value)
    value = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[redacted-ssn]", value)
    value = re.sub(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "[redacted-phone]", value)
    value = re.sub(r"\b(?:sk|pk|papaya|openai|anthropic|gemini|aws)[-_][A-Za-z0-9_-]{12,}\b", "[redacted-secret]", value, flags=re.I)
    value = re.sub(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted-token]", value, flags=re.I)
    return value


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"^(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|secret|password)$", key_text, flags=re.I):
                result[key_text] = "[redacted-secret]"
            else:
                result[key_text] = _redact_value(item)
        return result
    return value


def _content_type(value: Any) -> str:
    if isinstance(value, str):
        return "text"
    if isinstance(value, list) and all(isinstance(item, dict) and "role" in item for item in value):
        return "messages"
    return "json"


def _payload(value: Any, capture: CaptureMode) -> dict[str, Any]:
    normalized = _jsonable(value)
    if capture == "metadata":
        return {
            "contentType": _content_type(normalized),
            "redactionState": "metadata",
            "byteLength": _byte_length(normalized),
        }
    captured = _redact_value(normalized) if capture == "redacted" else normalized
    return {
        "contentType": _content_type(captured),
        "value": captured,
        "redactionState": capture,
        "byteLength": _byte_length(captured),
    }


def _error_payload(error: BaseException | Any) -> dict[str, str]:
    if isinstance(error, BaseException):
        return {"type": error.__class__.__name__, "message": str(error)}
    return {"message": str(error)}


# Papaya-owned control bag. Mirrors `controlBag` in packages/papaya-ai/src/index.ts.
# The customer supplies values only through the single `applied_recommendations`
# option, so they can never inject arbitrary keys into the bag.
APPLIED_RECOMMENDATION_WINDOW = 25

# Deliberately looser than the exact minted id shape, so a future change to id
# minting cannot invalidate markers already deployed in customer configs.
# `\Z` (not `$`) mirrors the JavaScript `$`, which never matches before a trailing newline.
_MARKER_PATTERN = re.compile(r"^agfind-[A-Za-z0-9_-]{1,56}\Z")


def _control_bag(applied: list[str] | None) -> dict[str, Any] | None:
    """Build the batch-level `papaya` control bag, or None when there is nothing to send.

    Pure function of the input list, so an unchanged customer config produces a
    byte-identical bag on every flush.
    """
    if not isinstance(applied, (list, tuple)) or not applied:
        return None
    valid = [marker for marker in applied if isinstance(marker, str) and _MARKER_PATTERN.match(marker)]
    # Deduplicate keeping the LAST occurrence, so re-adding a marker moves it to the
    # newest position instead of pinning it to its first slot. dict.fromkeys keeps the
    # FIRST occurrence, so reverse going in and reverse coming back out.
    ordered = list(reversed(list(dict.fromkeys(reversed(valid)))))
    window = ordered[-APPLIED_RECOMMENDATION_WINDOW:]
    return {"appliedRecommendations": window} if window else None


def _merge_options(*option_sets: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for options in option_sets:
        if not options:
            continue
        for key, value in options.items():
            if value is None:
                continue
            if key == "metadata" and isinstance(value, dict):
                result["metadata"] = {**result.get("metadata", {}), **value}
            else:
                result[key] = value
    return result


_RUN_OPTION_KEYS = (
    "traceId",
    "runId",
    "sessionId",
    "conversationId",
    "userId",
    "organizationId",
    "workflowKey",
    "workflowLabel",
    "conversational",
    "group",
    "metadata",
)


def _run_options_from_trace(trace: dict[str, Any] | None) -> dict[str, Any] | None:
    if not trace:
        return None
    return {key: trace[key] for key in _RUN_OPTION_KEYS if key in trace}


def _provider_args_and_options(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any], dict[str, Any] | None]:
    provider_args = args
    provider_kwargs = dict(kwargs)
    call_options = provider_kwargs.pop("papaya", None)

    if provider_args and isinstance(provider_args[0], dict) and "papaya" in provider_args[0]:
        request = dict(provider_args[0])
        call_options = request.pop("papaya", call_options)
        provider_args = (request, *provider_args[1:])

    return provider_args, provider_kwargs, call_options if isinstance(call_options, dict) else None


def _call_input(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if kwargs and args:
        return {"args": list(args), "kwargs": kwargs}
    if kwargs:
        return kwargs
    if len(args) == 1:
        return args[0]
    return list(args)


def _value_get(value: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(value, dict) and key in value:
            return value[key]
        if hasattr(value, key):
            return getattr(value, key)
    return None


def _number_value(value: Any) -> int | float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


_PAPAYA_USAGE_KEYS = ("usage", "token_usage", "usage_metadata", "usageMetadata", "tokenUsage")
_PAPAYA_USAGE_CONTAINERS = ("body", "response", "response_metadata", "result", "data", "llm_output", "metadata")
_PAPAYA_USAGE_FIELDS = {
    "inputTokens": ("inputTokens", "input_tokens", "prompt_tokens", "promptTokenCount", "prompt_token_count"),
    "outputTokens": ("outputTokens", "output_tokens", "completion_tokens", "candidatesTokenCount", "candidates_token_count"),
    "totalTokens": ("totalTokens", "total_tokens", "totalTokenCount", "total_token_count"),
    "cacheReadInputTokens": ("cacheReadInputTokens", "cache_read_input_tokens", "cached_input_tokens", "cachedContentTokenCount", "cached_content_token_count"),
    "cacheCreationInputTokens": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
    "reasoningTokens": ("reasoningTokens", "reasoning_tokens", "thoughtsTokenCount", "thoughts_token_count"),
    "costUsd": ("costUsd", "cost_usd"),
}


def _usage_diagnostic(diagnostics: list[str] | None, code: str) -> None:
    if diagnostics is not None and code not in diagnostics:
        diagnostics.append(code)


def _usage_get(value: Any, key: str, diagnostics: list[str] | None) -> Any:
    try:
        return value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    except Exception:
        _usage_diagnostic(diagnostics, "usage_record_unreadable")
        return None


def _usage_number(value: Any, keys: tuple[str, ...], diagnostics: list[str] | None) -> int | float | None:
    for key in keys:
        number = _number_value(_usage_get(value, key, diagnostics))
        if number is not None:
            return number
    return None


def _usage_fields(value: Any, diagnostics: list[str] | None, *, scoped: bool) -> dict[str, Any]:
    fields = {field: _usage_number(value, keys, diagnostics) for field, keys in _PAPAYA_USAGE_FIELDS.items()}
    if scoped and fields["costUsd"] is None:
        # Gateway account charge wins. Upstream inference cost is a separate
        # amount and is deliberately never substituted for the account charge.
        fields["costUsd"] = _usage_number(value, ("cost", "total_cost", "totalCost"), diagnostics)
    details = (
        ("cacheReadInputTokens", ("input_tokens_details", "prompt_tokens_details"), ("cached_tokens",)),
        ("cacheReadInputTokens", ("input_token_details", "inputTokenDetails"), ("cache_read", "cacheRead")),
        ("cacheCreationInputTokens", ("input_token_details", "inputTokenDetails"), ("cache_creation", "cacheCreation")),
        ("reasoningTokens", ("output_tokens_details", "completion_tokens_details"), ("reasoning_tokens",)),
        ("reasoningTokens", ("output_token_details", "outputTokenDetails"), ("reasoning",)),
    )
    for field, containers, keys in details:
        for container in containers:
            if fields[field] is None:
                fields[field] = _usage_number(_usage_get(value, container, diagnostics), keys, diagnostics)
    return {key: item for key, item in fields.items() if item is not None}


def _usage_from_record(value: Any, *, diagnostics: list[str] | None = None, scoped: bool = False) -> dict[str, Any] | None:
    fields: dict[str, Any] = {}
    for key in _PAPAYA_USAGE_KEYS:
        usage = _usage_get(value, key, diagnostics)
        if usage is not None:
            for field, item in _usage_fields(usage, diagnostics, scoped=True).items():
                fields.setdefault(field, item)
    provider_record = scoped or any(_usage_get(value, key, diagnostics) is not None
        for key in (*_PAPAYA_USAGE_KEYS, "model", "modelVersion", "object", "type"))
    for field, item in _usage_fields(value, diagnostics, scoped=provider_record).items():
        fields.setdefault(field, item)
    return fields or None


def _usage_scope_ids(value: Any, diagnostics: list[str] | None) -> list[tuple[str, str]]:
    keys = (("request", "request_id"), ("request", "requestId"),
            ("response", "response_id"), ("response", "responseId"))
    if any(_usage_get(value, key, diagnostics) is not None for key in (*_PAPAYA_USAGE_KEYS, "model", "object", "type")):
        keys += (("response", "id"),)
    identities = []
    for kind, key in keys:
        item = _usage_get(value, key, diagnostics)
        if isinstance(item, str) and item:
            identities.append((kind, item))
    return identities


def _usage_from_result(value: Any, seen: set[int] | None = None, *, diagnostics: list[str] | None = None) -> dict[str, Any] | None:
    """Recover one call's fields without walking content or other calls.

    Breadth-first precedence is stable: declared usage before direct fields,
    then recognized response wrappers. Subsets and duplicate observations never
    add to totals. Limits bound both provider-object access and graph traversal.
    """
    visited = set() if seen is None else set(seen)
    pending = deque([(value, 0, frozenset(), False)])
    fields: dict[str, Any] = {}
    scope_ids: dict[str, str] = {}
    while pending:
        record, depth, ancestors, scoped = pending.popleft()
        if record is None or isinstance(record, (str, bytes, bytearray, int, float, bool, list, tuple, set)):
            continue
        marker = id(record)
        if marker in ancestors:
            _usage_diagnostic(diagnostics, "usage_traversal_cycle_detected")
            continue
        if marker in visited:
            continue
        if depth > 16:
            _usage_diagnostic(diagnostics, "usage_traversal_depth_exceeded")
            continue
        if len(visited) >= 256:
            _usage_diagnostic(diagnostics, "usage_traversal_nodes_exceeded")
            break
        visited.add(marker)
        # Trace/child aggregates are not provider responses. Never copy their
        # totals onto a call, nor inspect prompts, messages, or tool contents.
        if any(_usage_get(record, key, diagnostics) is not None for key in ("spans", "traces", "children", "calls")):
            continue
        identities = _usage_scope_ids(record, diagnostics)
        candidate_ids = dict(scope_ids)
        conflict = False
        for kind, identity in identities:
            if kind in candidate_ids and candidate_ids[kind] != identity:
                conflict = True
                break
            candidate_ids[kind] = identity
        if conflict:
            _usage_diagnostic(diagnostics, "usage_scope_conflict")
            continue
        scope_ids = candidate_ids
        for field, item in (_usage_from_record(record, diagnostics=diagnostics, scoped=scoped) or {}).items():
            fields.setdefault(field, item)
        path = ancestors | {marker}
        for key in (*_PAPAYA_USAGE_KEYS, *_PAPAYA_USAGE_CONTAINERS):
            child = _usage_get(record, key, diagnostics)
            if child is not None:
                pending.append((child, depth + 1, path, True))
    if not fields:
        return None
    if "totalTokens" not in fields and ("inputTokens" in fields or "outputTokens" in fields):
        total = _number_value(fields.get("inputTokens", 0) + fields.get("outputTokens", 0))
        if total is not None:
            fields["totalTokens"] = total
    if "costUsd" in fields:
        fields["pricingSource"] = "provider"
    return fields


def _model_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    model = kwargs.get("model") or kwargs.get("model_id") or kwargs.get("modelId")
    if isinstance(model, str):
        return model
    if args and isinstance(args[0], dict):
        model = args[0].get("model") or args[0].get("model_id") or args[0].get("modelId")
        if isinstance(model, str):
            return model
    return None


def _model_from_result(value: Any, seen: set[int] | None = None) -> str | None:
    if value is None:
        return None
    seen = seen or set()
    if id(value) in seen:
        return None
    seen.add(id(value))
    model = _value_get(value, "model", "model_name", "modelName", "model_id", "modelId", "model_version", "modelVersion")
    if isinstance(model, str) and model:
        return model
    children: list[Any] = []
    if isinstance(value, dict):
        children.extend(value.values())
    elif isinstance(value, (list, tuple)):
        children.extend(value)
    else:
        for key in ("body", "response", "response_metadata", "usage_metadata", "llm_output"):
            child = getattr(value, key, None)
            if child is not None:
                children.append(child)
    for child in children:
        model = _model_from_result(child, seen)
        if model:
            return model
    return None


def _proxyable(value: Any) -> bool:
    return value is not None and not isinstance(value, (str, bytes, bytearray, int, float, bool, dict, list, tuple, set))


class _RunScope(AbstractContextManager[dict[str, Any]]):
    def __init__(self, papaya: "Papaya", options: dict[str, Any]):
        self._papaya = papaya
        self._options = options
        self._run: dict[str, Any] | None = None
        self._token: contextvars.Token[dict[str, Any] | None] | None = None

    def __enter__(self) -> dict[str, Any]:
        try:
            self._run = self._papaya.start_trace(self._options)
        except Exception as error:
            self._papaya._telemetry_failure(error)
            # A rejected handle only carries instrumentation state. It is never exported.
            self._run = {"_papaya_capture_rejected": True}
        self._token = _active_run.set(self._run)
        return self._run

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if self._token is not None:
            try:
                _active_run.reset(self._token)
            except Exception as error:
                self._papaya._telemetry_failure(error)
        if self._run is not None:
            try:
                if not self._run.get("_papaya_capture_rejected"):
                    self._papaya.finish_trace(self._run, "failed" if exc else "success", error=exc)
            except Exception as error:
                self._papaya._telemetry_failure(error, self._run)
        return False


class _PapayaClientProxy:
    def __init__(
        self,
        papaya: "Papaya",
        provider: str,
        target: Any,
        path: list[str] | None = None,
        options: dict[str, Any] | None = None,
    ):
        object.__setattr__(self, "_papaya", papaya)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_path", path or [])
        object.__setattr__(self, "_options", options or {})

    def __getattr__(self, name: str) -> Any:
        value = getattr(object.__getattribute__(self, "_target"), name)
        path = [*object.__getattribute__(self, "_path"), name]
        if callable(value):
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                return object.__getattribute__(self, "_papaya")._capture_provider_call(
                    object.__getattribute__(self, "_provider"),
                    path,
                    value,
                    args,
                    kwargs,
                    object.__getattribute__(self, "_options"),
                )
            return wrapped
        if _proxyable(value):
            return _PapayaClientProxy(
                object.__getattribute__(self, "_papaya"),
                object.__getattribute__(self, "_provider"),
                value,
                path,
                object.__getattribute__(self, "_options"),
            )
        return value

    def __repr__(self) -> str:
        return f"<PapayaClientProxy provider={object.__getattribute__(self, '_provider')!r} target={object.__getattribute__(self, '_target')!r}>"


def _capture_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def locked(self: "Papaya", *args: Any, **kwargs: Any) -> Any:
        with self._capture_lock:
            try:
                return method(self, *args, **kwargs)
            except PapayaCaptureError:
                raise
            except Exception:
                target = kwargs.get("trace")
                if method.__name__ == "finish_trace" and args:
                    target = args[0]
                if method.__name__ == "finish_span" and args:
                    owner = self._active.get(self._span_owners.get(id(args[0])))
                    target = owner["trace"] if owner else target
                if isinstance(target, dict):
                    self._remove_active(target)
                raise self._capture_error("serialization_failed", capture=True) from None
    return locked


class Papaya:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        project: str | None = None,
        environment: str | None = None,
        capture: CaptureMode = "redacted",
        service_name: str | None = None,
        service_version: str | None = None,
        applied_recommendations: list[str] | None = None,
        max_batch_bytes: int = 512 * 1024,
        debug: bool = False,
        transport: Transport | None = None,
        multipart: bool = False,
        multipart_requester: PapayaMultipartRequester | None = None,
        multipart_idle_ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
        max_pending_batches: int = 64,
        max_pending_bytes: int = 64 * 1024 * 1024,
        max_active_traces: int = 128,
        max_active_bytes: int | None = None,
        max_trace_bytes: int | None = None,
        max_retry_age: float = 300.0,
        max_attempts: int = 8,
        retry_base: float = 0.25,
        retry_max: float = 30.0,
        retry_jitter: float = 0.2,
        request_timeout: float = 10.0,
        flush_timeout: float = 10.0,
        diagnostic_limit: int = 128,
    ):
        self.api_key = api_key or os.getenv("PAPAYA_API_KEY") or os.getenv("PAPAYA_INGEST_TOKEN")
        self.endpoint = endpoint or "https://papaya.fyi/api/v1/ingest/traces"
        self.project = project or "default"
        self.environment = environment or "development"
        self.capture = capture
        self.service_name = service_name
        self.service_version = service_version
        # Papaya-owned, not customer content: kept off default_run_options on purpose so it
        # never leaks into per-run options or trace metadata.
        self.applied_recommendations = applied_recommendations
        if type(multipart) is not bool:
            raise ValueError("multipart must be a boolean")
        if multipart_requester is not None and not callable(multipart_requester):
            raise ValueError("multipart_requester must be callable")
        if not multipart and (multipart_requester is not None or multipart_idle_ttl_seconds is not None):
            raise ValueError("multipart options require multipart=True")
        if multipart and transport is not None:
            raise ValueError("multipart uses multipart_requester; the legacy transport is POST-only")
        if multipart_idle_ttl_seconds is not None and (type(multipart_idle_ttl_seconds) is not int or
                not 60 <= multipart_idle_ttl_seconds <= 3600):
            raise ValueError("multipart_idle_ttl_seconds must be an integer from 60 to 3600")
        max_active_bytes = (64 if multipart else 16) * 1024 * 1024 if max_active_bytes is None else max_active_bytes
        max_trace_bytes = (32 if multipart else 8) * 1024 * 1024 if max_trace_bytes is None else max_trace_bytes
        for name, value in (("max_pending_batches", max_pending_batches), ("max_pending_bytes", max_pending_bytes),
                            ("max_active_traces", max_active_traces), ("max_active_bytes", max_active_bytes),
                            ("max_trace_bytes", max_trace_bytes), ("max_attempts", max_attempts),
                            ("diagnostic_limit", diagnostic_limit)):
            if type(value) is not int or value <= 0:
                raise ValueError(name + " must be a positive integer")
        for name, value in (("max_retry_age", max_retry_age), ("request_timeout", request_timeout),
                            ("flush_timeout", flush_timeout), ("retry_base", retry_base),
                            ("retry_max", retry_max), ("retry_jitter", retry_jitter)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(name + " must be finite and nonnegative")
        if max_retry_age <= 0 or request_timeout <= 0 or retry_jitter > 1:
            raise ValueError("retry age/request timeout must be positive; jitter must be <= 1")
        self.max_batch_bytes = max_batch_bytes if max_batch_bytes > 0 else 512 * 1024
        self.debug = debug
        self.request_timeout, self.flush_timeout = request_timeout, flush_timeout
        self.multipart = multipart
        self.multipart_requester, self.multipart_idle_ttl_seconds = multipart_requester, multipart_idle_ttl_seconds
        self.transport = (_PapayaMultipartTransport(self.request_timeout, multipart_requester,
                          multipart_idle_ttl_seconds) if multipart else transport or _default_transport_for(self.request_timeout))
        self.default_run_options = {"metadata": metadata} if metadata else {}
        self.max_active_traces, self.max_active_bytes, self.max_trace_bytes = max_active_traces, max_active_bytes, max_trace_bytes
        self._completed: list[dict[str, Any]] = []
        self._completed_sizes: list[int] = []
        self._active: dict[int, dict[str, Any]] = {}
        self._span_owners: dict[int, int] = {}
        self._capture_lock = threading.RLock()
        self._admission_closed = False
        self._exporter = _PapayaExporter(api_key=self.api_key, endpoint=self.endpoint, transport=self.transport,
            max_pending_batches=max_pending_batches, max_pending_bytes=max_pending_bytes,
            max_retry_age=max_retry_age, max_attempts=max_attempts, retry_base=retry_base,
            retry_max=retry_max, retry_jitter=retry_jitter, flush_timeout=flush_timeout,
            diagnostic_limit=diagnostic_limit, user_agent="papaya-ai-python/" + __version__)

    @classmethod
    def init(cls, **options: Any) -> "Papaya":
        return cls(**options)

    def run(self, options: dict[str, Any] | None = None, **kwargs: Any) -> _RunScope:
        return _RunScope(self, _merge_options(self.default_run_options, options, kwargs))

    def _active_totals(self) -> tuple[int, int]:
        return len(self._active) + len(self._completed), sum(item["size"] for item in self._active.values()) + sum(self._completed_sizes)

    def _capture_error(self, code: str, *, size: int = 0, capture: bool = False) -> PapayaCaptureError:
        self._exporter.record(code, size=size, capture=capture)
        return PapayaCaptureError(code)

    def _remove_active(self, trace: dict[str, Any]) -> None:
        owned = self._active.pop(id(trace), None)
        if owned:
            for span_id in owned["spans"]:
                self._span_owners.pop(span_id, None)

    def _telemetry_failure(self, error: Exception, trace: dict[str, Any] | None = None) -> None:
        with self._capture_lock:
            if trace is not None:
                self._remove_active(trace)
                trace["_papaya_capture_rejected"] = True
            if not isinstance(error, PapayaCaptureError):
                self._exporter.record("capture_failed", capture=True)

    def _capture_size(self, value: Any) -> int:
        try:
            return len(_json_bytes(value))
        except Exception:
            raise self._capture_error("serialization_failed", capture=True) from None

    def _check_capture_capacity(self, *, size: int, extra_trace: bool = False) -> None:
        count, current = self._active_totals()
        code = ("capture_closed" if self._admission_closed and extra_trace else
                "active_traces_capacity" if extra_trace and count >= self.max_active_traces else
                "active_bytes_capacity" if current + size > self.max_active_bytes else None)
        if code:
            raise self._capture_error(code, size=max(0, size))

    def _account_span(self, span: dict[str, Any]) -> None:
        # Charge only the changed span. Re-serializing the growing trace on every
        # tool call would itself introduce quadratic work across long tool loops.
        trace_key = self._span_owners.get(id(span))
        owned = self._active.get(trace_key) if trace_key is not None else None
        if owned is None:
            raise self._capture_error("capture_not_active")
        size = self._capture_size(span)
        delta = size - owned["spans"].get(id(span), 0)
        if owned["size"] + delta > self.max_trace_bytes:
            self._remove_active(owned["trace"])
            raise self._capture_error("trace_bytes_capacity", size=size)
        try:
            self._check_capture_capacity(size=delta)
        except Exception:
            self._remove_active(owned["trace"])
            raise
        owned["size"] += delta
        owned["spans"][id(span)] = size
        self._exporter.update_active(*self._active_totals())

    def capture_payload(self, value: Any) -> dict[str, Any]:
        try:
            return _payload(value, self.capture)
        except Exception:
            raise self._capture_error("serialization_failed", capture=True) from None

    def wrap_client(self, provider: str, client: Any, options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return _PapayaClientProxy(self, provider, client, options=_merge_options(options, kwargs))

    def openai(self, client: Any, options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.wrap_client("openai", client, options, **kwargs)

    def claude(self, client: Any, options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.wrap_client("claude", client, options, **kwargs)

    def anthropic(self, client: Any, options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.claude(client, options, **kwargs)

    def gemini(self, client: Any, options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.wrap_client("gemini", client, options, **kwargs)

    def bedrock(self, client: Any, options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.wrap_client("bedrock", client, options, **kwargs)

    @_capture_locked
    def start_trace(
        self,
        options: dict[str, Any] | None = None,
        *,
        root_span_id: str | None = None,
        root_name: str | None = None,
        root_kind: SpanKind = "workflow",
        input_value: Any | None = None,
        input_payload: dict[str, Any] | None = None,
        model_ref: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        try:
            merged = _jsonable(_merge_options(self.default_run_options, options))
        except Exception:
            raise self._capture_error("serialization_failed", capture=True) from None
        self._check_capture_capacity(size=0, extra_trace=True)
        trace_id = merged.get("traceId") or _id("trace")
        run_id = merged.get("runId") or _id("run")
        root_id = root_span_id or _id("span")
        root_span: dict[str, Any] = {
            "spanId": root_id,
            "name": root_name or merged.get("workflowLabel") or merged.get("workflowKey") or "papaya.run",
            "kind": root_kind,
            "startedAt": started_at or _iso(),
            "status": "unknown",
            "attributes": {
                "project": self.project,
                "environment": self.environment,
                "metadata": merged.get("metadata"),
                **_jsonable(attributes or {}),
            },
        }
        if input_payload is not None:
            root_span["inputPayload"] = _jsonable(input_payload)
        elif input_value is not None:
            root_span["inputPayload"] = _payload(input_value, self.capture)
        if model_ref:
            root_span["modelRef"] = _jsonable(model_ref)
        trace = {
            **merged,
            "traceId": trace_id,
            "runId": run_id,
            "rootSpanId": root_id,
            "spans": [root_span],
        }
        size = self._capture_size(trace)
        if size > self.max_trace_bytes:
            raise self._capture_error("trace_bytes_capacity", size=size)
        self._check_capture_capacity(size=size, extra_trace=True)
        self._active[id(trace)] = {"trace": trace, "size": size, "spans": {id(root_span): self._capture_size(root_span)}}
        self._span_owners[id(root_span)] = id(trace)
        self._exporter.update_active(*self._active_totals())
        return trace

    @_capture_locked
    def finish_trace(
        self,
        trace: dict[str, Any],
        status: SpanStatus,
        *,
        output_value: Any | None = None,
        output_payload: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        model_used: str | None = None,
        error: BaseException | Any | None = None,
        ended_at: str | None = None,
    ) -> None:
        if id(trace) not in self._active:
            raise self._capture_error("capture_not_active")
        try:
            root = trace["spans"][0]
            self.finish_span(root, status, output_value=output_value, output_payload=output_payload,
                             usage=usage, model_used=model_used, error=error, ended_at=ended_at)
            # A final whole-trace validation covers direct manual edits as well.
            encoded = _json_bytes(trace)
            size = len(encoded)
            previous = self._active[id(trace)]["size"]
            if size > self.max_trace_bytes:
                raise self._capture_error("trace_bytes_capacity", size=size)
            self._check_capture_capacity(size=size - previous)
            frozen = json.loads(encoded)
            self._remove_active(trace)
            self._completed.append(frozen)
            self._completed_sizes.append(size)
            self._exporter.update_active(*self._active_totals())
        except Exception as failure:
            self._remove_active(trace)
            if isinstance(failure, PapayaCaptureError):
                raise
            raise self._capture_error("serialization_failed", capture=True) from None

    @_capture_locked
    def start_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace: dict[str, Any] | None = None,
        parent_span_id: str | None = None,
        span_id: str | None = None,
        input_value: Any | None = None,
        input_payload: dict[str, Any] | None = None,
        model_ref: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        target = trace if trace is not None else _active_run.get()
        if target is None:
            target = self.start_trace({"workflowKey": name})
            _active_run.set(target)
        if id(target) not in self._active:
            raise self._capture_error("capture_not_active")
        span: dict[str, Any] = {
            "spanId": span_id or _id("span"),
            "parentSpanId": parent_span_id or target["rootSpanId"],
            "name": name,
            "kind": kind,
            "startedAt": started_at or _iso(),
            "status": "unknown",
        }
        if input_payload is not None:
            span["inputPayload"] = _jsonable(input_payload)
        elif input_value is not None:
            span["inputPayload"] = _payload(input_value, self.capture)
        if model_ref:
            span["modelRef"] = _jsonable(model_ref)
        if attributes:
            span["attributes"] = _jsonable(attributes)
        self._span_owners[id(span)] = id(target)
        # A comma separates the newly appended span from the previous span.
        self._active[id(target)]["spans"][id(span)] = -1
        self._account_span(span)
        target["spans"].append(span)
        return span

    def _capture_provider_call(
        self,
        provider: str,
        path: list[str],
        call: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        wrapper_options: dict[str, Any] | None = None,
    ) -> Any:
        try:
            provider_args, provider_kwargs, call_options = _provider_args_and_options(args, kwargs)
        except Exception as failure:
            self._telemetry_failure(failure)
            return call(*args, **kwargs)
        active_trace = _active_run.get()
        if active_trace is not None and active_trace.get("_papaya_capture_rejected"):
            return call(*provider_args, **provider_kwargs)
        try:
            boundary = _merge_options(self.default_run_options, wrapper_options, _run_options_from_trace(active_trace), call_options)
            trace = active_trace if active_trace is not None else self.start_trace({
                "workflowKey": boundary.get("workflowKey") or f"{provider}.{'.'.join(path)}", **boundary})
        except Exception as failure:
            self._telemetry_failure(failure, active_trace)
            return call(*provider_args, **provider_kwargs)
        return self._capture_provider_call_in_trace(trace, provider, path, call, provider_args,
            provider_kwargs, boundary, finish_trace=active_trace is None)

    def _capture_provider_call_in_trace(
        self,
        trace: dict[str, Any],
        provider: str,
        path: list[str],
        call: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        boundary: dict[str, Any],
        *,
        finish_trace: bool = False,
    ) -> Any:
        span = None
        model = None
        try:
            method = ".".join(path)
            model = _model_from_call(args, kwargs)
            span = self.start_span(trace=trace, name=f"{provider}.{method}", kind="llm",
                input_value=_call_input(args, kwargs), model_ref={"provider": provider, "requested": model},
                attributes={"provider": provider, "method": method,
                            **{key: boundary.get(key) for key in ("workflowKey", "workflowLabel", "sessionId",
                               "conversationId", "userId", "organizationId", "metadata")}})
        except Exception as failure:
            self._telemetry_failure(failure, trace)

        def finish(status: SpanStatus, result: Any | None = None, error: BaseException | Any | None = None) -> None:
            if span is None:
                return
            try:
                usage_diagnostics: list[str] = []
                usage = _usage_from_result(result, diagnostics=usage_diagnostics)
                if usage_diagnostics:
                    # Discovery limits do not reject an otherwise valid trace.
                    # Use the existing bounded diagnostic queue without changing
                    # rejected/capture-failed accounting or exposing payloads.
                    with self._exporter.condition:
                        for code in usage_diagnostics:
                            self._exporter._diagnostics.append({"code": code, "traces": 1, "bytes": 0})
                        self._exporter._changed()
                self.finish_span(span, status, output_value=result, usage=usage,
                                 model_used=_model_from_result(result) or model, error=error)
                if finish_trace:
                    self.finish_trace(trace, status, output_value=result, error=error)
            except Exception as failure:
                self._telemetry_failure(failure, trace)

        try:
            result = call(*args, **kwargs)
        except BaseException as error:
            finish("failed", error=error)
            raise
        if inspect.isawaitable(result):
            async def await_and_finish() -> Any:
                try:
                    value = await result
                except BaseException as error:
                    finish("failed", error=error)
                    raise
                finish("success", result=value)
                return value
            return await_and_finish()
        finish("success", result=result)
        return result

    @_capture_locked
    def finish_span(
        self,
        span: dict[str, Any],
        status: SpanStatus,
        *,
        output_value: Any | None = None,
        output_payload: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        model_used: str | None = None,
        error: BaseException | Any | None = None,
        ended_at: str | None = None,
    ) -> None:
        span["endedAt"] = ended_at or _iso()
        span["status"] = status
        if output_payload is not None:
            span["outputPayload"] = _jsonable(output_payload)
        elif output_value is not None:
            span["outputPayload"] = _payload(output_value, self.capture)
        if usage:
            span["usage"] = {key: value for key, value in usage.items() if value is not None}
        if model_used:
            span["modelRef"] = {**span.get("modelRef", {}), "used": model_used}
        if error is not None:
            span["error"] = _error_payload(error)

        self._account_span(span)

    def _new_pending_batch(self, traces: list[dict[str, Any]]) -> dict[str, Any]:
        # Built here, at flush time, so every split batch carries the bag and the bag
        # tracks the current config rather than a snapshot taken at construction.
        papaya = _control_bag(self.applied_recommendations)
        batch = {
            "schemaVersion": "2026-06-05",
            "batchId": _id("batch"),
            "sentAt": _iso(),
            "sdk": {
                "name": "papaya-ai",
                "version": __version__,
                "language": "python",
                "runtime": f"python/{platform.python_version()}",
            },
            "resource": {
                "serviceName": self.service_name,
                "serviceVersion": self.service_version,
                "environment": self.environment,
            },
            # Omitted entirely when there is nothing to send: never `"papaya": null`,
            # never an empty object, never an empty array.
            **({"papaya": papaya} if papaya else {}),
            "traces": traces,
        }
        return {"batch": batch, "body": _json_bytes(batch)}

    def _freeze_completed_batches(self) -> None:
        if not self._completed:
            return
        # Completed objects are SDK-owned JSON snapshots, detached from callers.
        while self._completed:
            pending = self._new_pending_batch([])
            encoded_traces = [_json_bytes(self._completed[0])]
            count = 1
            current_size = len(pending["body"]) + self._completed_sizes[0]
            while count < len(self._completed) and current_size + self._completed_sizes[count] + 1 <= self.max_batch_bytes:
                encoded_traces.append(_json_bytes(self._completed[count]))
                current_size += self._completed_sizes[count] + 1
                count += 1
            pending["batch"]["traces"] = self._completed[:count]
            # The empty envelope ends in []}; replace only that empty array.
            pending["body"] = pending["body"][:-2] + b",".join(encoded_traces) + b"]}"
            if not self._exporter.admit(pending["batch"]["batchId"],
                    [trace["traceId"] for trace in pending["batch"]["traces"]], pending["body"]):
                break
            del self._completed[:count]
            del self._completed_sizes[:count]

    def _sync_export_configuration(self) -> None:
        # Legacy direct assignment may supply a missing key, but only the explicit
        # configuration API resumes a server authentication stop.
        with self._exporter.condition:
            self._exporter.api_key = self.api_key
            self._exporter.endpoint = self.endpoint
            self._exporter.transport = self.transport
            self._exporter._ensure_worker()
            self._exporter._changed()

    def configure_export(self, *, api_key: Any = _EXPORT_UNSET,
                         endpoint: Any = _EXPORT_UNSET, transport: Any = _EXPORT_UNSET) -> None:
        with self._capture_lock:
            if api_key is not _EXPORT_UNSET:
                self.api_key = api_key
            if endpoint is not _EXPORT_UNSET:
                self.endpoint = endpoint
            if transport is not _EXPORT_UNSET:
                if self.multipart and transport is not None:
                    raise ValueError("multipart uses multipart_requester; the legacy transport is POST-only")
                self.transport = (_PapayaMultipartTransport(self.request_timeout, self.multipart_requester,
                                  self.multipart_idle_ttl_seconds) if self.multipart else
                                  transport or _default_transport_for(self.request_timeout))
            self._exporter.configure(api_key=self.api_key, endpoint=self.endpoint, transport=self.transport)

    def export_status(self) -> dict[str, Any]:
        with self._capture_lock:
            result = self._exporter.snapshot()
            count, size = self._active_totals()
            result["active"] = {"traces": count, "bytes": size,
                                "unfinishedTraces": len(self._active), "completedTraces": len(self._completed)}
            return result

    def _flush_targets(self) -> Any:
        with self._capture_lock:
            # Keep the initial target snapshot atomic with admission: a fast
            # transport may otherwise finish and remove a batch before flush
            # has acquired its delivery receipt.
            with self._exporter.condition:
                self._sync_export_configuration()
                try:
                    self._freeze_completed_batches()
                except Exception as failure:
                    self._telemetry_failure(failure)
                return self._exporter.targets(), {"traces": len(self._completed), "bytes": sum(self._completed_sizes)}

    def _with_active(self, result: dict[str, Any], *, closing: bool = False,
                     blocked_capture: dict[str, int] | None = None) -> dict[str, Any]:
        active = self.export_status()["active"]
        result["active"] = active
        result["blockedCapture"] = blocked_capture or {"traces": 0, "bytes": 0}
        if ((blocked_capture and blocked_capture["traces"]) or (closing and active["traces"])) and result["status"] != "failed":
            result["status"] = "failed"
            result["reason"] = "undelivered_capture"
        return result

    def flush(self, timeout: float | None = None) -> dict[str, Any]:
        targets, blocked = self._flush_targets()
        return self._with_active(self._exporter.wait(targets, self.flush_timeout if timeout is None else self._timeout(timeout)), blocked_capture=blocked)

    async def aflush(self, timeout: float | None = None) -> dict[str, Any]:
        targets, blocked = self._flush_targets()
        return self._with_active(await self._exporter.await_targets(targets, self.flush_timeout if timeout is None else self._timeout(timeout)), blocked_capture=blocked)

    @staticmethod
    def _timeout(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("timeout must be finite and nonnegative")
        return value

    def _close_targets(self) -> Any:
        with self._capture_lock:
            self._admission_closed = True
            targets, _ = self._flush_targets()
            return targets

    def close(self, deadline: float = 5.0) -> dict[str, Any]:
        timeout = self._timeout(deadline)
        until = time.monotonic() + timeout
        targets = self._close_targets()
        all_targets = list(targets)
        while True:
            result = self._exporter.wait(targets, max(0.0, until - time.monotonic()), drain=True)
            with self._capture_lock:
                pending_completed = bool(self._completed)
            if not pending_completed or time.monotonic() >= until or result["authPaused"] or not self.api_key:
                break
            previous = {id(item) for item, _ in targets}
            targets, _ = self._flush_targets()
            added = [(item, attempt) for item, attempt in targets if id(item) not in previous]
            all_targets.extend(added)
            if not added:
                break
        self._exporter.close_admission()
        with self._exporter.condition:
            result = self._exporter._result(all_targets, result["timedOut"], True)
        return self._with_active(result, closing=True)

    async def aclose(self, deadline: float = 5.0) -> dict[str, Any]:
        timeout = self._timeout(deadline)
        until = time.monotonic() + timeout
        targets = self._close_targets()
        all_targets = list(targets)
        try:
            while True:
                result = await self._exporter.await_targets(targets, max(0.0, until - time.monotonic()), drain=True)
                with self._capture_lock:
                    pending_completed = bool(self._completed)
                if not pending_completed or time.monotonic() >= until or result["authPaused"] or not self.api_key:
                    break
                previous = {id(item) for item, _ in targets}
                targets, _ = self._flush_targets()
                added = [(item, attempt) for item, attempt in targets if id(item) not in previous]
                all_targets.extend(added)
                if not added:
                    break
        finally:
            self._exporter.close_admission()
        with self._exporter.condition:
            result = self._exporter._result(all_targets, result["timedOut"], True)
        return self._with_active(result, closing=True)


__all__ = ["Papaya", "PapayaCaptureError", "CaptureMode", "SpanKind", "SpanStatus"]
