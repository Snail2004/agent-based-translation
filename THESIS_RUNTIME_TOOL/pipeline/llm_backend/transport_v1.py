"""One-shot provider transport envelopes and provider usage normalization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .capability_probe_contracts_v1 import validate_capability_probe_seal
from .contracts_v1 import ContractValidationError, canonical_json, validate_api_source
from .credentials_v1 import ResolvedCredential
from .resolver_v1 import validate_resolved_llm_run_seal


class TransportCallError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        safe_message: str,
        status_code: int | None = None,
        response: "RawTransportResponse | None" = None,
        response_body_truncated: bool = False,
    ):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code
        self.response = response
        self.response_body_truncated = response_body_truncated


class TransportSender(Protocol):
    def send(self, request: "PreparedTransportRequest") -> "RawTransportResponse": ...


@dataclass(frozen=True)
class RawTransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    request_id: str | None = None


class PreparedTransportRequest:
    __slots__ = (
        "method",
        "url",
        "protocol",
        "source_id",
        "requested_model_id",
        "body",
        "body_sha256",
        "timeout_seconds",
        "max_error_body_bytes",
        "_headers",
    )

    def __init__(
        self,
        *,
        method: str,
        url: str | None,
        protocol: str,
        source_id: str,
        requested_model_id: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_error_body_bytes: int = 0,
    ) -> None:
        if (
            not isinstance(max_error_body_bytes, int)
            or isinstance(max_error_body_bytes, bool)
            or max_error_body_bytes < 0
            or max_error_body_bytes > 65_536
        ):
            raise ValueError("max_error_body_bytes must be between 0 and 65536")
        self.method = method
        self.url = url
        self.protocol = protocol
        self.source_id = source_id
        self.requested_model_id = requested_model_id
        self.body = body
        self.body_sha256 = sha256(body).hexdigest()
        self.timeout_seconds = timeout_seconds
        self.max_error_body_bytes = max_error_body_bytes
        self._headers = dict(headers)

    def headers_for_transport(self) -> dict[str, str]:
        return dict(self._headers)

    def __repr__(self) -> str:
        return (
            "PreparedTransportRequest("
            f"method={self.method!r}, url={self.url!r}, protocol={self.protocol!r}, "
            f"source_id={self.source_id!r}, requested_model_id={self.requested_model_id!r}, "
            "headers=<redacted>, "
            f"body_sha256={self.body_sha256!r}, timeout_seconds={self.timeout_seconds!r}, "
            f"max_error_body_bytes={self.max_error_body_bytes!r})"
        )

    def __reduce__(self):
        raise TypeError("prepared transport requests may not be serialized")


class UrllibTransportSender:
    """Standard-library sender. It performs one request and never retries."""

    def send(self, request: PreparedTransportRequest) -> RawTransportResponse:
        if request.url is None:
            raise TransportCallError(
                code="in_process_sender_required",
                safe_message="local in-process transport requires an injected sender",
            )
        wire_request = Request(
            request.url,
            data=request.body,
            headers=request.headers_for_transport(),
            method=request.method,
        )
        try:
            with urlopen(wire_request, timeout=request.timeout_seconds) as response:
                headers = {key.casefold(): value for key, value in response.headers.items()}
                return RawTransportResponse(
                    status_code=int(response.status),
                    headers=headers,
                    body=response.read(),
                    request_id=headers.get("x-request-id"),
                )
        except HTTPError as exc:
            headers = {
                key.casefold(): value for key, value in (exc.headers or {}).items()
            }
            response = None
            truncated = False
            if request.max_error_body_bytes:
                try:
                    body = exc.read(request.max_error_body_bytes + 1)
                except Exception:
                    body = b""
                if isinstance(body, bytes) and body:
                    truncated = len(body) > request.max_error_body_bytes
                    response = RawTransportResponse(
                        status_code=int(exc.code),
                        headers=headers,
                        body=body[: request.max_error_body_bytes],
                        request_id=headers.get("x-request-id"),
                    )
            raise TransportCallError(
                code=f"http_{exc.code}",
                status_code=int(exc.code),
                safe_message=f"provider returned HTTP {exc.code}",
                response=response,
                response_body_truncated=truncated,
            ) from exc
        except URLError as exc:
            raise TransportCallError(
                code="connection",
                safe_message="provider connection failed",
            ) from exc
        except TimeoutError as exc:
            raise TransportCallError(
                code="timeout",
                safe_message="provider request timed out",
            ) from exc


class CallableInProcessSender:
    """Explicit local callback adapter used only for local_in_process sources."""

    def __init__(self, callback: Callable[[bytes], RawTransportResponse]) -> None:
        self._callback = callback

    def send(self, request: PreparedTransportRequest) -> RawTransportResponse:
        if request.protocol != "local_in_process" or request.url is not None:
            raise TransportCallError(
                code="invalid_request",
                safe_message="in-process sender received a network request",
            )
        return self._callback(request.body)


def prepare_transport_request(
    *,
    seal: Mapping[str, Any],
    request_body: Mapping[str, Any],
    credential: ResolvedCredential | None,
    timeout_seconds: float,
    target_index: int = 0,
) -> PreparedTransportRequest:
    """Build one protocol-specific wire envelope from a sealed target."""

    normalized = validate_resolved_llm_run_seal(seal)
    targets = [normalized["primary"], *normalized["fallback_plan"]["steps"]]
    if not isinstance(target_index, int) or isinstance(target_index, bool):
        raise ContractValidationError("target_index must be an integer")
    if target_index < 0 or target_index >= len(targets):
        raise ContractValidationError("target_index is outside the sealed target plan")
    if target_index > 0 and not normalized["fallback_plan"]["enabled"]:
        raise ContractValidationError("fallback target is not enabled by the seal")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ContractValidationError("timeout_seconds must be positive")

    target = targets[target_index]
    source = target["source"]
    requested_model = target["target"]["requested_model_id"]
    encoded = validate_transport_request_body(
        seal=normalized,
        request_body=request_body,
        target_index=target_index,
    )
    return _prepare_source_transport_request(
        source=source,
        requested_model_id=requested_model,
        encoded_body=encoded,
        credential=credential,
        timeout_seconds=timeout_seconds,
    )


def prepare_capability_probe_transport_request(
    *,
    probe_seal: Mapping[str, Any],
    request_body: Mapping[str, Any],
    credential: ResolvedCredential | None,
    timeout_seconds: float,
) -> PreparedTransportRequest:
    """Build the one wire request authorized by a capability-only seal."""

    normalized = validate_capability_probe_seal(probe_seal)
    encoded = validate_capability_probe_request_body(
        probe_seal=normalized,
        request_body=request_body,
    )
    return _prepare_source_transport_request(
        source=normalized["source_binding"]["record"],
        requested_model_id=normalized["capability_intent"]["requested_model_id"],
        encoded_body=encoded,
        credential=credential,
        timeout_seconds=timeout_seconds,
        max_error_body_bytes=normalized["limits"]["max_response_utf8_bytes"],
    )


def validate_capability_probe_request_body(
    *,
    probe_seal: Mapping[str, Any],
    request_body: Mapping[str, Any],
) -> bytes:
    """Bind a probe request to its exact model, schema and sealed body hash."""

    normalized = validate_capability_probe_seal(probe_seal)
    source = normalized["source_binding"]["record"]
    intent = normalized["capability_intent"]
    body = deepcopy(dict(request_body))
    protocol = source["protocol"]
    requested_model = intent["requested_model_id"]
    if protocol in {"openai_chat_completions", "openai_responses"}:
        if body.get("model") != requested_model:
            raise ContractValidationError(
                "OpenAI-compatible probe model differs from seal"
            )
    elif protocol == "google_genai_generate_content":
        if "model" in body:
            raise ContractValidationError(
                "Google probe model belongs in the sealed route"
            )

    _validate_probe_response_contract(body=body, protocol=protocol, seal=normalized)
    encoded = canonical_json(body).encode("utf-8")
    if sha256(encoded).hexdigest() != normalized["request_body_sha256"]:
        raise ContractValidationError("probe request body differs from seal")
    if len(encoded) > normalized["limits"]["max_prompt_utf8_bytes"]:
        raise ContractValidationError("probe request exceeds sealed UTF-8 byte cap")
    return encoded


def _prepare_source_transport_request(
    *,
    source: Mapping[str, Any],
    requested_model_id: str,
    encoded_body: bytes,
    credential: ResolvedCredential | None,
    timeout_seconds: float,
    max_error_body_bytes: int = 0,
) -> PreparedTransportRequest:
    source = validate_api_source(source)
    if not isinstance(requested_model_id, str) or not requested_model_id:
        raise ContractValidationError("requested model must be nonempty")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ContractValidationError("timeout_seconds must be positive")
    headers = {"content-type": "application/json"}
    url: str | None
    protocol = source["protocol"]
    if protocol == "local_in_process":
        if credential is not None:
            raise ContractValidationError("in-process transport may not receive a credential")
        url = None
    else:
        if credential is None:
            raise ContractValidationError("network transport lacks its resolved credential")
        base_url = source["base_url"].rstrip("/")
        secret = credential.reveal_for_transport()
        if protocol == "openai_chat_completions":
            url = f"{base_url}/chat/completions"
            headers["authorization"] = f"Bearer {secret}"
        elif protocol == "openai_responses":
            url = f"{base_url}/responses"
            headers["authorization"] = f"Bearer {secret}"
        elif protocol == "google_genai_generate_content":
            url = (
                f"{base_url}/models/{quote(requested_model_id, safe='')}:generateContent"
            )
            headers["x-goog-api-key"] = secret
        else:
            raise ContractValidationError("unsupported transport protocol")
    return PreparedTransportRequest(
        method="POST",
        url=url,
        protocol=protocol,
        source_id=source["source_id"],
        requested_model_id=requested_model_id,
        headers=headers,
        body=encoded_body,
        timeout_seconds=float(timeout_seconds),
        max_error_body_bytes=max_error_body_bytes,
    )


def _validate_probe_response_contract(
    *, body: Mapping[str, Any], protocol: str, seal: Mapping[str, Any]
) -> None:
    intent = seal["capability_intent"]
    capability_kind = intent["capability_kind"]
    if capability_kind == "json_object":
        if protocol == "openai_chat_completions":
            response_format = body.get("response_format")
            if not isinstance(response_format, Mapping) or response_format.get("type") != "json_object":
                raise ContractValidationError(
                    "json_object probe lacks exact OpenAI response_format"
                )
        elif protocol == "openai_responses":
            response_format = (body.get("text") or {}).get("format")
            if not isinstance(response_format, Mapping) or response_format.get("type") != "json_object":
                raise ContractValidationError(
                    "json_object probe lacks exact Responses format"
                )
        elif protocol == "google_genai_generate_content":
            generation = body.get("generationConfig")
            if not isinstance(generation, Mapping) or generation.get("responseMimeType") != "application/json":
                raise ContractValidationError(
                    "json_object probe lacks Google JSON response MIME type"
                )
        elif protocol == "local_in_process":
            if body.get("response_format") != "json_object":
                raise ContractValidationError(
                    "in-process json_object probe lacks response_format"
                )
        return

    expected_schema = seal["response_schema"]
    schema_name = intent["schema_name"]
    if protocol == "openai_chat_completions":
        response_format = body.get("response_format")
        if not isinstance(response_format, Mapping) or response_format.get("type") != "json_schema":
            raise ContractValidationError("probe lacks native OpenAI JSON schema mode")
        declared = response_format.get("json_schema")
        if (
            not isinstance(declared, Mapping)
            or declared.get("name") != schema_name
            or declared.get("strict") is not True
            or declared.get("schema") != expected_schema
        ):
            raise ContractValidationError("OpenAI probe schema differs from seal")
    elif protocol == "openai_responses":
        text = body.get("text")
        declared = text.get("format") if isinstance(text, Mapping) else None
        if (
            not isinstance(declared, Mapping)
            or declared.get("type") != "json_schema"
            or declared.get("name") != schema_name
            or declared.get("strict") is not True
            or declared.get("schema") != expected_schema
        ):
            raise ContractValidationError("Responses probe schema differs from seal")
    elif protocol == "google_genai_generate_content":
        generation = body.get("generationConfig")
        if (
            not isinstance(generation, Mapping)
            or generation.get("responseMimeType") != "application/json"
            or generation.get("responseJsonSchema") != expected_schema
        ):
            raise ContractValidationError("Google probe schema differs from seal")
    elif protocol == "local_in_process":
        if body.get("response_schema") != expected_schema or body.get("strict") is not True:
            raise ContractValidationError("in-process probe schema differs from seal")


def validate_transport_request_body(
    *,
    seal: Mapping[str, Any],
    request_body: Mapping[str, Any],
    target_index: int = 0,
) -> bytes:
    """Validate and canonically encode a request before cache lookup or transport."""

    normalized = validate_resolved_llm_run_seal(seal)
    targets = [normalized["primary"], *normalized["fallback_plan"]["steps"]]
    if not isinstance(target_index, int) or isinstance(target_index, bool):
        raise ContractValidationError("target_index must be an integer")
    if target_index < 0 or target_index >= len(targets):
        raise ContractValidationError("target_index is outside the sealed target plan")
    if target_index > 0 and not normalized["fallback_plan"]["enabled"]:
        raise ContractValidationError("fallback target is not enabled by the seal")
    target = targets[target_index]
    requested_model = target["target"]["requested_model_id"]
    body = deepcopy(dict(request_body))
    protocol = target["source"]["protocol"]
    if protocol in {"openai_chat_completions", "openai_responses"}:
        if body.get("model") != requested_model:
            raise ContractValidationError("OpenAI-compatible request model differs from seal")
    elif protocol == "google_genai_generate_content":
        if "model" in body:
            raise ContractValidationError("Google request model belongs in the sealed route")
    encoded = canonical_json(body).encode("utf-8")
    if sha256(encoded).hexdigest() not in {
        binding["sha256"] for binding in normalized["input_bindings"]
    }:
        raise ContractValidationError(
            "transport request body is not present in sealed input bindings"
        )
    return encoded


def normalize_provider_response(
    *, request: PreparedTransportRequest, response: RawTransportResponse
) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise TransportCallError(
            code=f"http_{response.status_code}",
            status_code=response.status_code,
            safe_message=f"provider returned HTTP {response.status_code}",
        )
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportCallError(
            code="invalid_json",
            safe_message="provider returned invalid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise TransportCallError(
            code="invalid_json_shape",
            safe_message="provider response is not a JSON object",
        )
    if request.protocol == "openai_chat_completions":
        facts = _normalize_openai_chat(payload)
    elif request.protocol == "openai_responses":
        facts = _normalize_openai_responses(payload)
    elif request.protocol == "google_genai_generate_content":
        facts = _normalize_google(payload)
    elif request.protocol == "local_in_process":
        facts = _normalize_local(payload)
    else:
        raise TransportCallError(
            code="unsupported_protocol",
            safe_message="response protocol is unsupported",
        )
    facts["raw_response_sha256"] = sha256(response.body).hexdigest()
    facts["request_id"] = response.request_id or facts.get("request_id")
    facts["payload"] = payload
    return facts


def _normalize_openai_chat(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(payload.get("usage"), "OpenAI usage")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise TransportCallError(code="invalid_response", safe_message="OpenAI choices are absent")
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "request_id": _optional_string(payload.get("id")),
        "observed_model_id": _required_string(payload.get("model"), "model"),
        "finish_reason": _normalize_finish_reason(choices[0].get("finish_reason")),
        "prompt_tokens": _optional_nonnegative_int(usage.get("prompt_tokens")),
        "cached_input_tokens": _optional_nonnegative_int(prompt_details.get("cached_tokens")),
        "completion_tokens": _optional_nonnegative_int(usage.get("completion_tokens")),
        "reasoning_tokens": _optional_nonnegative_int(completion_details.get("reasoning_tokens")),
        "total_tokens": _optional_nonnegative_int(usage.get("total_tokens")),
    }


def _normalize_openai_responses(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(payload.get("usage"), "Responses usage")
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    status = payload.get("status")
    finish_reason = "stop" if status == "completed" else "unknown"
    if status == "incomplete":
        finish_reason = "length"
    return {
        "request_id": _optional_string(payload.get("id")),
        "observed_model_id": _required_string(payload.get("model"), "model"),
        "finish_reason": finish_reason,
        "prompt_tokens": _optional_nonnegative_int(usage.get("input_tokens")),
        "cached_input_tokens": _optional_nonnegative_int(input_details.get("cached_tokens")),
        "completion_tokens": _optional_nonnegative_int(usage.get("output_tokens")),
        "reasoning_tokens": _optional_nonnegative_int(output_details.get("reasoning_tokens")),
        "total_tokens": _optional_nonnegative_int(usage.get("total_tokens")),
    }


def _normalize_google(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(payload.get("usageMetadata"), "Google usage")
    candidates = payload.get("candidates") or []
    finish = candidates[0].get("finishReason") if candidates and isinstance(candidates[0], dict) else None
    prompt_tokens = _optional_nonnegative_int(usage.get("promptTokenCount"))
    candidate_tokens = _optional_nonnegative_int(usage.get("candidatesTokenCount"))
    reasoning_tokens = _optional_nonnegative_int(usage.get("thoughtsTokenCount"))
    total_tokens = _optional_nonnegative_int(usage.get("totalTokenCount"))
    completion_tokens = None
    if candidate_tokens is not None:
        if reasoning_tokens is not None:
            completion_tokens = candidate_tokens + reasoning_tokens
        elif prompt_tokens is not None and total_tokens is not None:
            minimum_total = prompt_tokens + candidate_tokens
            if total_tokens < minimum_total:
                raise TransportCallError(
                    code="invalid_usage",
                    safe_message=(
                        "Google totalTokenCount is smaller than prompt plus "
                        "candidate tokens"
                    ),
                )
            completion_tokens = total_tokens - prompt_tokens
        else:
            completion_tokens = candidate_tokens
    return {
        "request_id": _optional_string(payload.get("responseId")),
        "observed_model_id": _required_string(payload.get("modelVersion"), "modelVersion"),
        "finish_reason": _normalize_finish_reason(finish),
        "prompt_tokens": prompt_tokens,
        "cached_input_tokens": _optional_nonnegative_int(usage.get("cachedContentTokenCount")),
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _normalize_local(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(payload.get("usage"), "local usage")
    return {
        "request_id": _optional_string(payload.get("request_id")),
        "observed_model_id": _required_string(payload.get("model"), "model"),
        "finish_reason": _normalize_finish_reason(payload.get("finish_reason")),
        "prompt_tokens": _optional_nonnegative_int(usage.get("prompt_tokens")),
        "cached_input_tokens": _optional_nonnegative_int(usage.get("cached_input_tokens")),
        "completion_tokens": _optional_nonnegative_int(usage.get("completion_tokens")),
        "reasoning_tokens": _optional_nonnegative_int(usage.get("reasoning_tokens")),
        "total_tokens": _optional_nonnegative_int(usage.get("total_tokens")),
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TransportCallError(code="missing_usage", safe_message=f"{label} is absent")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TransportCallError(code="invalid_response", safe_message=f"provider {label} is absent")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TransportCallError(code="invalid_usage", safe_message="provider usage is invalid")
    return value


def _normalize_finish_reason(value: Any) -> str:
    if value is None:
        return "unknown"
    normalized = str(value).casefold()
    return {
        "stop": "stop",
        "completed": "stop",
        "max_tokens": "length",
        "length": "length",
        "content_filter": "content_filter",
        "safety": "safety",
        "tool_calls": "tool_call",
        "tool_call": "tool_call",
        "error": "error",
    }.get(normalized, "unknown")
