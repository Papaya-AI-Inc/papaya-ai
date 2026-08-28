from __future__ import annotations

import base64
import contextvars
import inspect
import json
import os
import platform
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from .version import __version__

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
Transport = Callable[[str, dict[str, str], bytes], tuple[int, str]]

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
    return json.dumps(value, default=_json_default, separators=(",", ":")).encode("utf-8")


def _json_text(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"))


def _byte_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(_json_bytes(value))


def _redact_string(value: str) -> str:
    value = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[redacted-email]", value, flags=re.I)
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
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _usage_from_record(value: Any) -> dict[str, Any] | None:
    usage = (
        _value_get(value, "usage")
        or _value_get(value, "token_usage")
        or _value_get(value, "usage_metadata")
        or _value_get(value, "usageMetadata")
    )
    if usage is None:
        usage = value
    input_tokens = _number_value(_value_get(usage, "input_tokens", "prompt_tokens", "inputTokens", "promptTokenCount", "prompt_token_count"))
    output_tokens = _number_value(_value_get(usage, "output_tokens", "completion_tokens", "outputTokens", "candidatesTokenCount", "candidates_token_count"))
    total_tokens = _number_value(_value_get(usage, "total_tokens", "totalTokens", "totalTokenCount", "total_token_count"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    cost_usd = _number_value(_value_get(usage, "cost_usd", "costUsd"))
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens if total_tokens is not None else (input_tokens or 0) + (output_tokens or 0),
        "cacheReadInputTokens": _number_value(_value_get(usage, "cache_read_input_tokens", "cached_input_tokens", "cacheReadInputTokens", "cached_content_token_count")),
        "cacheCreationInputTokens": _number_value(_value_get(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")),
        "reasoningTokens": _number_value(_value_get(usage, "reasoning_tokens", "reasoningTokens", "thoughts_token_count")),
        "costUsd": cost_usd,
        "pricingSource": "provider" if cost_usd is not None else None,
    }


def _usage_from_result(value: Any, seen: set[int] | None = None) -> dict[str, Any] | None:
    if value is None:
        return None
    seen = seen or set()
    if id(value) in seen:
        return None
    seen.add(id(value))
    usage = _usage_from_record(value)
    if usage:
        return usage
    if isinstance(value, dict):
        iterable = value.values()
    elif isinstance(value, (list, tuple)):
        iterable = value
    else:
        iterable = []
        for key in ("body", "response", "response_metadata", "usage_metadata", "llm_output"):
            child = getattr(value, key, None)
            if child is not None:
                iterable = [*iterable, child]
    for item in iterable:
        usage = _usage_from_result(item, seen)
        if usage:
            return usage
    return None


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


def _default_transport(endpoint: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


class _RunScope(AbstractContextManager[dict[str, Any]]):
    def __init__(self, papaya: "Papaya", options: dict[str, Any]):
        self._papaya = papaya
        self._options = options
        self._run: dict[str, Any] | None = None
        self._token: contextvars.Token[dict[str, Any] | None] | None = None

    def __enter__(self) -> dict[str, Any]:
        self._run = self._papaya.start_trace(self._options)
        self._token = _active_run.set(self._run)
        return self._run

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if self._token is not None:
            _active_run.reset(self._token)
        if self._run is not None:
            self._papaya.finish_trace(self._run, "failed" if exc else "success", error=exc)
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
        metadata: dict[str, Any] | None = None,
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
        self.max_batch_bytes = max_batch_bytes if max_batch_bytes > 0 else 512 * 1024
        self.debug = debug
        self.transport = transport or _default_transport
        self.default_run_options = {"metadata": metadata} if metadata else {}
        self._completed: list[dict[str, Any]] = []
        self._pending_batches: list[dict[str, Any]] = []
        self._flush_lock = threading.Lock()

    @classmethod
    def init(cls, **options: Any) -> "Papaya":
        return cls(**options)

    def run(self, options: dict[str, Any] | None = None, **kwargs: Any) -> _RunScope:
        return _RunScope(self, _merge_options(self.default_run_options, options, kwargs))

    def capture_payload(self, value: Any) -> dict[str, Any]:
        return _payload(value, self.capture)

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
        merged = _merge_options(self.default_run_options, options)
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
                **(attributes or {}),
            },
        }
        if input_payload is not None:
            root_span["inputPayload"] = input_payload
        elif input_value is not None:
            root_span["inputPayload"] = _payload(input_value, self.capture)
        if model_ref:
            root_span["modelRef"] = model_ref
        trace = {
            **merged,
            "traceId": trace_id,
            "runId": run_id,
            "rootSpanId": root_id,
            "spans": [root_span],
        }
        return trace

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
        root = trace["spans"][0]
        root["endedAt"] = ended_at or _iso()
        root["status"] = status
        if output_payload is not None:
            root["outputPayload"] = output_payload
        elif output_value is not None:
            root["outputPayload"] = _payload(output_value, self.capture)
        if usage:
            root["usage"] = {key: value for key, value in usage.items() if value is not None}
        if model_used:
            root["modelRef"] = {**root.get("modelRef", {}), "used": model_used}
        if error is not None:
            root["error"] = _error_payload(error)
        if trace not in self._completed:
            self._completed.append(trace)

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
        target = trace or _active_run.get()
        if target is None:
            target = self.start_trace({"workflowKey": name})
            _active_run.set(target)
        span: dict[str, Any] = {
            "spanId": span_id or _id("span"),
            "parentSpanId": parent_span_id or target["rootSpanId"],
            "name": name,
            "kind": kind,
            "startedAt": started_at or _iso(),
            "status": "unknown",
        }
        if input_payload is not None:
            span["inputPayload"] = input_payload
        elif input_value is not None:
            span["inputPayload"] = _payload(input_value, self.capture)
        if model_ref:
            span["modelRef"] = model_ref
        if attributes:
            span["attributes"] = attributes
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
        provider_args, provider_kwargs, call_options = _provider_args_and_options(args, kwargs)
        active_trace = _active_run.get()
        boundary = _merge_options(self.default_run_options, wrapper_options, _run_options_from_trace(active_trace), call_options)

        if active_trace is not None:
            return self._capture_provider_call_in_trace(
                active_trace,
                provider,
                path,
                call,
                provider_args,
                provider_kwargs,
                boundary,
            )

        trace_options = {
            "workflowKey": boundary.get("workflowKey") or f"{provider}.{'.'.join(path)}",
            **boundary,
        }
        trace = self.start_trace(trace_options)
        return self._capture_provider_call_in_trace(
            trace,
            provider,
            path,
            call,
            provider_args,
            provider_kwargs,
            boundary,
            finish_trace=True,
        )

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
        method = ".".join(path)
        model = _model_from_call(args, kwargs)
        span = self.start_span(
            trace=trace,
            name=f"{provider}.{method}",
            kind="llm",
            input_value=_call_input(args, kwargs),
            model_ref={"provider": provider, "requested": model},
            attributes={
                "provider": provider,
                "method": method,
                "workflowKey": boundary.get("workflowKey"),
                "workflowLabel": boundary.get("workflowLabel"),
                "sessionId": boundary.get("sessionId"),
                "conversationId": boundary.get("conversationId"),
                "userId": boundary.get("userId"),
                "organizationId": boundary.get("organizationId"),
                "metadata": boundary.get("metadata"),
            },
        )

        def finish(status: SpanStatus, result: Any | None = None, error: BaseException | Any | None = None) -> None:
            self.finish_span(
                span,
                status,
                output_value=result,
                usage=_usage_from_result(result),
                model_used=_model_from_result(result) or model,
                error=error,
            )
            if finish_trace:
                self.finish_trace(trace, status, output_value=result, error=error)

        try:
            result = call(*args, **kwargs)
        except Exception as error:
            finish("failed", error=error)
            raise

        if inspect.isawaitable(result):
            async def await_and_finish() -> Any:
                try:
                    value = await result
                except Exception as error:
                    finish("failed", error=error)
                    raise
                finish("success", result=value)
                return value
            return await_and_finish()

        finish("success", result=result)
        return result

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
            span["outputPayload"] = output_payload
        elif output_value is not None:
            span["outputPayload"] = _payload(output_value, self.capture)
        if usage:
            span["usage"] = {key: value for key, value in usage.items() if value is not None}
        if model_used:
            span["modelRef"] = {**span.get("modelRef", {}), "used": model_used}
        if error is not None:
            span["error"] = _error_payload(error)

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
        current: list[dict[str, Any]] = []
        traces = self._completed[:]
        self._completed.clear()
        for trace in traces:
            candidate = self._new_pending_batch([*current, trace])
            if current and len(candidate["body"]) > self.max_batch_bytes:
                self._pending_batches.append(self._new_pending_batch(current))
                current = [trace]
            else:
                current.append(trace)
        if current:
            self._pending_batches.append(self._new_pending_batch(current))

    def _split_pending_batch(self, index: int, pending: dict[str, Any]) -> None:
        traces = pending["batch"]["traces"]
        midpoint = (len(traces) + 1) // 2
        self._pending_batches[index:index + 1] = [
            self._new_pending_batch(traces[:midpoint]),
            self._new_pending_batch(traces[midpoint:]),
        ]

    def flush(self) -> dict[str, Any]:
        with self._flush_lock:
            return self._flush_pending()

    def _flush_pending(self) -> dict[str, Any]:
        self._freeze_completed_batches()
        trace_count = sum(len(pending["batch"]["traces"]) for pending in self._pending_batches)
        if trace_count == 0:
            return {"status": "skipped", "traceCount": 0, "reason": "empty"}
        if not self.api_key:
            return {"status": "skipped", "traceCount": trace_count, "reason": "missing_api_key"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"papaya-ai-python/{__version__}",
        }
        last_status = 202
        last_response_text: str | None = None
        index = 0
        while index < len(self._pending_batches):
            pending = self._pending_batches[index]
            pending_trace_count = len(pending["batch"]["traces"])
            try:
                status, response_text = self.transport(self.endpoint, headers, pending["body"])
            except Exception as error:  # pragma: no cover - defensive transport boundary
                if self.debug:
                    print("[papaya] export failed", error, file=sys.stderr)
                return {
                    "status": "failed",
                    "traceCount": pending_trace_count,
                    "endpoint": self.endpoint,
                    "error": str(error),
                }
            if 200 <= status < 300:
                last_status = status
                last_response_text = response_text
                self._pending_batches.pop(index)
                continue
            if status == 413 and pending_trace_count > 1:
                self._split_pending_batch(index, pending)
                continue
            if status == 413:
                self._pending_batches.pop(index)
                return {
                    "status": "failed",
                    "traceCount": 1,
                    "endpoint": self.endpoint,
                    "httpStatus": status,
                    "responseText": response_text,
                    "error": "The trace exceeds the server's single-trace limit.",
                    "errorCode": "oversized_trace",
                }
            terminal = status in {400, 401, 403, 409}
            if terminal:
                self._pending_batches.pop(index)
            if self.debug:
                print(f"[papaya] export failed: {status} {response_text}", file=sys.stderr)
            return {
                "status": "failed",
                "traceCount": pending_trace_count,
                "endpoint": self.endpoint,
                "httpStatus": status,
                "responseText": response_text,
                "errorCode": f"http_{status}" if terminal else "retryable_http_error",
            }
        return {
            "status": "sent",
            "traceCount": trace_count,
            "endpoint": self.endpoint,
            "httpStatus": last_status,
            "responseText": last_response_text,
        }


__all__ = ["Papaya", "CaptureMode", "SpanKind", "SpanStatus"]
