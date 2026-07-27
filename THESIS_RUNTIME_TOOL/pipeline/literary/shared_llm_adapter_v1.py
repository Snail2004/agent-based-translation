"""Thin Literary semantic adapter over one Shared LLM Backend attempt."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from pipeline.llm_backend import (
    ContractValidationError,
    SharedLlmBackend,
    canonical_sha256,
    resolve_llm_run_seal,
)
from pipeline.literary.shared_llm_profiles_v1 import (
    PROFILE_ID,
    PROFILE_REVISION,
    LiterarySharedRolePreset,
    build_literary_pipeline_profile,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    PROMPT_JSON_INSTRUCTION_ID,
    PROMPT_JSON_INSTRUCTION_REVISION,
    shared_structured_output_for_envelope,
)
from pipeline.literary.structured_output_policy_v1 import (
    project_transport_schema_v1,
)


ADAPTER_VERSION = "literary_shared_llm_adapter_v1"
SUPPORTED_PROTOCOLS = frozenset(
    {"openai_chat_completions", "google_genai_generate_content"}
)
SemanticValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ResponseTransformer = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class LiterarySharedLlmAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiterarySharedAttemptResult:
    status: str
    provider_called: bool
    response_bytes: bytes
    response_text: str
    response_payload: Mapping[str, Any]
    semantic_payload: Mapping[str, Any]
    usage: Mapping[str, Any] | None
    artifact_sha256: str
    seal: Mapping[str, Any]
    cache_observation: Mapping[str, Any] | None


class LiterarySharedLlmAttemptAdapter:
    """Execute one sealed attempt; retry and authority remain outside this class."""

    uses_shared_backend = True

    def __init__(self, *, backend: SharedLlmBackend) -> None:
        self.backend = backend

    def execute(
        self,
        *,
        preset: LiterarySharedRolePreset,
        api_source: Mapping[str, Any],
        capability: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any],
        schema_name: str,
        prompt_ref: Mapping[str, Any],
        response_schema_ref: Mapping[str, Any],
        validator_ref: Mapping[str, Any],
        semantic_extension_ref: Mapping[str, Any],
        structured_output: Mapping[str, Any],
        output_envelope: Mapping[str, Any] | None = None,
        semantic_validator: SemanticValidator,
        response_transformer: ResponseTransformer | None = None,
        run_id: str,
        attempt_run_id: str,
        stage_id: str,
        logical_request_id: str,
        semantic_attempt_index: int = 1,
        transport_retry_ordinal: int = 0,
        additional_input_bindings: Sequence[Mapping[str, str]] = (),
        allow_response_cache_read: bool = False,
        allow_response_cache_write: bool = False,
        cost_fact: Mapping[str, Any] | None = None,
        pipeline_profile_id: str | None = None,
        pipeline_profile_revision: str | None = None,
    ) -> LiterarySharedAttemptResult:
        """Resolve, execute and locally validate one Literary response.

        Raw application-response cache access is disabled by default because
        SharedLlmBackend persists transport evidence before this adapter can run
        the Literary semantic validator.
        """

        if not callable(semantic_validator):
            raise LiterarySharedLlmAdapterError("semantic_validator is required")
        if allow_response_cache_read or allow_response_cache_write:
            raise LiterarySharedLlmAdapterError(
                "raw response cache remains disabled until Literary semantic "
                "acceptance can be bound to reusable artifact publication"
            )
        if response_schema_ref.get("sha256") != canonical_sha256(response_schema):
            raise LiterarySharedLlmAdapterError(
                "runtime response schema differs from the sealed schema reference"
            )
        explicit_output_envelope = output_envelope is not None
        envelope = _normalize_output_envelope(
            output_envelope=output_envelope,
            structured_output=structured_output,
        )
        if explicit_output_envelope:
            expected_structured = shared_structured_output_for_envelope(envelope)
            if dict(structured_output) != expected_structured:
                raise LiterarySharedLlmAdapterError(
                    "output envelope differs from shared structured-output binding"
                )
            if envelope["mode"] == "native_schema" and not (
                _is_direct_official_native_source(api_source)
            ):
                raise LiterarySharedLlmAdapterError(
                    "native Structured Output is limited to direct official sources"
                )
        transport_schema, transport_omissions = resolve_transport_response_schema(
            response_schema=response_schema,
            protocol=str(api_source["protocol"]),
            output_envelope=(envelope if explicit_output_envelope else None),
        )
        transport_schema_hash = canonical_sha256(transport_schema)
        profile_schema_ref = dict(response_schema_ref)
        if transport_schema_hash != response_schema_ref.get("sha256"):
            profile_schema_ref = {
                "id": f"{response_schema_ref['id']}.transport",
                "revision": "openai_projection_v1",
                "sha256": transport_schema_hash,
            }
        profile = build_literary_pipeline_profile(
            preset=preset,
            api_source=api_source,
            capability=capability,
            prompt_ref=prompt_ref,
            response_schema_ref=profile_schema_ref,
            validator_ref=validator_ref,
            semantic_extension_ref=semantic_extension_ref,
            structured_output=structured_output,
            profile_id=pipeline_profile_id or PROFILE_ID,
            profile_revision=pipeline_profile_revision or PROFILE_REVISION,
        )
        request_body = render_literary_request_body(
            preset=preset,
            protocol=str(api_source["protocol"]),
            capability=capability,
            messages=messages,
            response_schema=transport_schema,
            instruction_schema=response_schema,
            schema_name=schema_name,
            structured_output=structured_output,
            output_envelope=(envelope if explicit_output_envelope else None),
            base_url=api_source.get("base_url"),
        )
        bindings = [
            {
                "name": "transport_request_body",
                "sha256": canonical_sha256(request_body),
            },
            {
                "name": "literary_canonical_response_schema",
                "sha256": canonical_sha256(response_schema),
            },
            {
                "name": "literary_transport_schema_projection",
                "sha256": canonical_sha256(
                    {
                        "transport_schema_sha256": transport_schema_hash,
                        "omissions": list(transport_omissions),
                    }
                ),
            },
            *[dict(row) for row in additional_input_bindings],
        ]
        names = [str(row.get("name") or "") for row in bindings]
        if not all(names) or len(names) != len(set(names)):
            raise LiterarySharedLlmAdapterError(
                "input binding names must be nonempty and unique"
            )
        seal = resolve_llm_run_seal(
            profile=profile,
            api_sources=[api_source],
            capability_evidence=[capability],
            role_id=preset.role_id,
            run_id=run_id,
            attempt_run_id=attempt_run_id,
            stage_id=stage_id,
            input_bindings=bindings,
        )
        backend_result = self.backend.execute_one_attempt(
            seal=seal,
            logical_request_id=logical_request_id,
            semantic_attempt_index=semantic_attempt_index,
            transport_retry_ordinal=transport_retry_ordinal,
            request_body=request_body,
            target_index=0,
            allow_response_cache_read=allow_response_cache_read,
            allow_response_cache_write=allow_response_cache_write,
            cost_fact=cost_fact,
        )
        response_payload = _decode_provider_payload(backend_result["response_bytes"])
        response_text = _extract_response_text(
            response_payload,
            protocol=str(api_source["protocol"]),
        )
        semantic_raw = _decode_semantic_object(response_text)
        if response_transformer is not None:
            try:
                semantic_raw = dict(response_transformer(semantic_raw))
            except Exception as exc:
                raise LiterarySharedLlmAdapterError(
                    "Literary model-reference transport resolution rejected the response"
                ) from exc
        try:
            normalized = semantic_validator(semantic_raw)
        except Exception as exc:
            raise LiterarySharedLlmAdapterError(
                "Literary local semantic validation rejected the provider response: "
                f"{_bounded_validator_error(exc)}"
            ) from exc
        if not isinstance(normalized, Mapping):
            raise LiterarySharedLlmAdapterError(
                "Literary semantic validator must return an object"
            )
        return LiterarySharedAttemptResult(
            status="semantic_accepted",
            provider_called=bool(backend_result["provider_called"]),
            response_bytes=backend_result["response_bytes"],
            response_text=response_text,
            response_payload=response_payload,
            semantic_payload=dict(normalized),
            usage=backend_result.get("usage"),
            artifact_sha256=str(backend_result["artifact_sha256"]),
            seal=seal,
            cache_observation=backend_result.get("cache_observation"),
        )


def render_literary_request_body(
    *,
    preset: LiterarySharedRolePreset,
    protocol: str,
    capability: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    response_schema: Mapping[str, Any],
    schema_name: str,
    structured_output: Mapping[str, Any],
    output_envelope: Mapping[str, Any] | None = None,
    base_url: str | None = None,
    instruction_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise LiterarySharedLlmAdapterError(
            f"Literary shared adapter does not support protocol {protocol}"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", schema_name):
        raise LiterarySharedLlmAdapterError("schema_name is not provider-safe")
    envelope = _normalize_output_envelope(
        output_envelope=output_envelope,
        structured_output=structured_output,
    )
    if (
        output_envelope is not None
        and envelope["mode"] == "native_schema"
        and not _is_direct_official_native_binding(
            protocol=protocol, base_url=base_url
        )
    ):
        raise LiterarySharedLlmAdapterError(
            "native Structured Output is limited to direct official sources"
        )
    rows = _normalize_messages(messages)
    if envelope["mode"] in {"json_object", "prompt_json"}:
        rows = _with_json_only_instruction(
            rows,
            response_schema=(instruction_schema or response_schema),
        )
    mode = structured_output.get("mode")
    capability_kind = capability.get("capability_kind")
    if protocol == "openai_chat_completions":
        return _render_openai_chat_request(
            preset=preset,
            messages=rows,
            response_schema=response_schema,
            schema_name=schema_name,
            mode=mode,
            capability_kind=capability_kind,
        )
    return _render_google_request(
        preset=preset,
        messages=rows,
        response_schema=response_schema,
        mode=mode,
        capability_kind=capability_kind,
    )


def _render_openai_chat_request(
    *,
    preset: LiterarySharedRolePreset,
    messages: list[dict[str, str]],
    response_schema: Mapping[str, Any],
    schema_name: str,
    mode: Any,
    capability_kind: Any,
) -> dict[str, Any]:
    generation = preset.generation
    body: dict[str, Any] = {
        "model": preset.requested_model_id,
        "messages": messages,
        "temperature": generation["temperature"],
        "top_p": generation["top_p"],
        "seed": generation["seed"],
        "reasoning_effort": generation["reasoning_effort"],
        "verbosity": generation["verbosity"],
        "max_completion_tokens": generation["max_output_tokens"],
    }
    if mode == "required" or (
        mode == "preferred" and capability_kind == "native_structured_output"
    ):
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": dict(response_schema),
            },
        }
    elif mode in {"prompt_validated", "preferred"}:
        body["response_format"] = {"type": "json_object"}
    elif mode != "disabled":
        raise LiterarySharedLlmAdapterError("unknown structured-output mode")
    return {key: value for key, value in body.items() if value is not None}


def _render_google_request(
    *,
    preset: LiterarySharedRolePreset,
    messages: list[dict[str, str]],
    response_schema: Mapping[str, Any],
    mode: Any,
    capability_kind: Any,
) -> dict[str, Any]:
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    for row in messages:
        if row["role"] == "system":
            system_parts.append({"text": row["content"]})
            continue
        role = "model" if row["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": row["content"]}]})
    generation = preset.generation
    generation_config: dict[str, Any] = {
        "temperature": generation["temperature"],
        "topP": generation["top_p"],
        "seed": generation["seed"],
        "maxOutputTokens": generation["max_output_tokens"],
        "thinkingConfig": {"thinkingBudget": 0},
    }
    if mode == "required" or (
        mode == "preferred" and capability_kind == "native_structured_output"
    ):
        generation_config.update(
            {
                "responseMimeType": "application/json",
                "responseJsonSchema": dict(response_schema),
            }
        )
    elif mode in {"prompt_validated", "preferred"}:
        generation_config["responseMimeType"] = "application/json"
    elif mode != "disabled":
        raise LiterarySharedLlmAdapterError("unknown structured-output mode")
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            key: value for key, value in generation_config.items() if value is not None
        },
    }
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    return body


def _normalize_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise LiterarySharedLlmAdapterError("messages must be an ordered sequence")
    rows: list[dict[str, str]] = []
    for raw in messages:
        if set(raw) != {"role", "content"}:
            raise LiterarySharedLlmAdapterError("message keys differ")
        role = raw["role"]
        content = raw["content"]
        if role not in {"system", "user", "assistant"}:
            raise LiterarySharedLlmAdapterError("message role is unsupported")
        if not isinstance(content, str) or not content:
            raise LiterarySharedLlmAdapterError("message content is empty")
        rows.append({"role": role, "content": content})
    if not rows:
        raise LiterarySharedLlmAdapterError("messages are empty")
    return rows


def build_prompt_json_instruction(
    response_schema: Mapping[str, Any],
) -> str:
    """Return a compact syntax instruction; the local validator remains authority."""

    if response_schema.get("type") != "object":
        raise LiterarySharedLlmAdapterError(
            "prompt JSON output requires an object response schema"
        )
    properties = response_schema.get("properties")
    required = response_schema.get("required")
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        raise LiterarySharedLlmAdapterError(
            "prompt JSON output schema lacks properties or required fields"
        )
    if not all(isinstance(name, str) and name for name in required):
        raise LiterarySharedLlmAdapterError(
            "prompt JSON output required fields are malformed"
        )
    shape_rows: list[str] = []
    for name in sorted(properties):
        schema = properties[name]
        if not isinstance(name, str) or not isinstance(schema, Mapping):
            raise LiterarySharedLlmAdapterError(
                "prompt JSON output properties are malformed"
            )
        value_type = schema.get("type", "value")
        if isinstance(value_type, list):
            value_type = "|".join(str(item) for item in value_type)
        if value_type == "array" and isinstance(schema.get("items"), Mapping):
            item_type = schema["items"].get("type", "value")
            value_type = f"array<{item_type}>"
        shape_rows.append(f"{json.dumps(name, ensure_ascii=False)}:{value_type}")
    required_fields = ", ".join(
        json.dumps(name, ensure_ascii=False) for name in required
    )
    return (
        "OUTPUT ENVELOPE "
        f"[{PROMPT_JSON_INSTRUCTION_ID}/{PROMPT_JSON_INSTRUCTION_REVISION}]: "
        "Return exactly one JSON object. Do not use Markdown fences, prose, "
        "comments, or trailing text. Top-level field types: "
        f"{{{', '.join(shape_rows)}}}. Required top-level fields: "
        f"[{required_fields}]. Follow this complete canonical response "
        "contract: "
        + json.dumps(
            response_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + ". Local validation is authoritative."
    )


def resolve_transport_response_schema(
    *,
    response_schema: Mapping[str, Any],
    protocol: str,
    output_envelope: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    if (
        output_envelope is not None
        and output_envelope.get("mode") == "native_schema"
        and protocol == "openai_chat_completions"
    ):
        projected, omissions = project_transport_schema_v1(response_schema)
        return projected, tuple(omissions)
    return deepcopy(dict(response_schema)), ()


def _with_json_only_instruction(
    rows: list[dict[str, str]], *, response_schema: Mapping[str, Any]
) -> list[dict[str, str]]:
    instruction = build_prompt_json_instruction(response_schema)
    if any(
        row["role"] == "system" and row["content"] == instruction for row in rows
    ):
        return rows
    insert_at = 0
    while insert_at < len(rows) and rows[insert_at]["role"] == "system":
        insert_at += 1
    return [
        *rows[:insert_at],
        {"role": "system", "content": instruction},
        *rows[insert_at:],
    ]


def _normalize_output_envelope(
    *,
    output_envelope: Mapping[str, Any] | None,
    structured_output: Mapping[str, Any],
) -> dict[str, Any]:
    if output_envelope is not None:
        if set(output_envelope) != {
            "mode",
            "schema_dialect",
            "instruction_id",
            "instruction_revision",
        }:
            raise LiterarySharedLlmAdapterError("output envelope keys differ")
        return dict(output_envelope)
    mode = structured_output.get("mode")
    dialect = structured_output.get("schema_dialect")
    if mode == "required":
        return {
            "mode": "native_schema",
            "schema_dialect": dialect,
            "instruction_id": None,
            "instruction_revision": None,
        }
    if mode in {"prompt_validated", "preferred"}:
        return {
            "mode": "json_object",
            "schema_dialect": dialect,
            "instruction_id": PROMPT_JSON_INSTRUCTION_ID,
            "instruction_revision": PROMPT_JSON_INSTRUCTION_REVISION,
        }
    if mode == "disabled":
        return {
            "mode": "prompt_json",
            "schema_dialect": None,
            "instruction_id": PROMPT_JSON_INSTRUCTION_ID,
            "instruction_revision": PROMPT_JSON_INSTRUCTION_REVISION,
        }
    raise LiterarySharedLlmAdapterError("unknown structured-output mode")


def _is_direct_official_native_source(source: Mapping[str, Any]) -> bool:
    return _is_direct_official_native_binding(
        protocol=str(source.get("protocol") or ""),
        base_url=source.get("base_url"),
    )


def _is_direct_official_native_binding(
    *, protocol: str, base_url: Any
) -> bool:
    if not isinstance(base_url, str) or not all(
        0x21 <= ord(character) <= 0x7E for character in base_url
    ):
        return False
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (ValueError, UnicodeError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")
    if protocol == "openai_chat_completions":
        return host == "api.openai.com" and path == "/v1"
    if protocol == "google_genai_generate_content":
        return (
            host == "generativelanguage.googleapis.com" and path == "/v1beta"
        )
    return False


def _decode_provider_payload(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiterarySharedLlmAdapterError("provider response is not JSON") from exc
    if not isinstance(value, dict):
        raise LiterarySharedLlmAdapterError("provider response is not an object")
    return value


def _extract_response_text(payload: Mapping[str, Any], *, protocol: str) -> str:
    if protocol == "openai_chat_completions":
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise LiterarySharedLlmAdapterError(
                "OpenAI-compatible response must contain exactly one choice"
            )
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise LiterarySharedLlmAdapterError("provider message content is empty")
        return content
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise LiterarySharedLlmAdapterError(
            "Google response must contain exactly one candidate"
        )
    content = (
        candidates[0].get("content")
        if isinstance(candidates[0], Mapping)
        else None
    )
    parts = content.get("parts") if isinstance(content, Mapping) else None
    if not isinstance(parts, list) or not parts:
        raise LiterarySharedLlmAdapterError("Google response content is empty")
    texts = [part.get("text") for part in parts if isinstance(part, Mapping)]
    if not texts or any(not isinstance(text, str) or not text for text in texts):
        raise LiterarySharedLlmAdapterError("Google response text is malformed")
    return "".join(texts)


def _decode_semantic_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LiterarySharedLlmAdapterError(
            "provider content is not a JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise LiterarySharedLlmAdapterError(
            "provider semantic response is not an object"
        )
    return value


def _bounded_validator_error(exc: Exception) -> str:
    error_type = type(exc).__name__
    message = str(exc).strip() or "validator supplied no error message"
    if "sk-" in message or "Bearer " in message:
        message = "credential-like material was redacted"
    return f"{error_type}: {message[:4000]}"


__all__ = [
    "ADAPTER_VERSION",
    "LiterarySharedAttemptResult",
    "LiterarySharedLlmAdapterError",
    "LiterarySharedLlmAttemptAdapter",
    "SUPPORTED_PROTOCOLS",
    "build_prompt_json_instruction",
    "render_literary_request_body",
    "resolve_transport_response_schema",
]
