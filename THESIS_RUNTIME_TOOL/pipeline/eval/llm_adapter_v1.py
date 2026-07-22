from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.contracts_v1 import (
    require_enum,
    require_exact_keys,
    require_mapping,
    require_sha256,
    require_string,
)
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    EVALUATION_PROMPT_VALIDATED_JSON_OUTPUT_INSTRUCTION_V1,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    evaluation_response_schema_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.scorer_prompts_v3 import (
    RenderedPromptV3,
    parse_pj_response_v2,
    parse_sf_bt_semantic_response_v3,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    _parse_back_translation_response,
)
from pipeline.llm_backend import (
    SharedLlmBackend,
    canonical_sha256,
    validate_api_source,
    validate_capability_evidence,
    validate_cache_observation,
    validate_llm_attempt_usage,
    validate_pipeline_profile,
    validate_resolved_llm_run_seal,
)


__all__ = [
    "EVALUATION_LLM_CACHE_MODES",
    "build_evaluation_input_bindings_v1",
    "build_evaluation_request_body_v1",
    "execute_evaluation_llm_attempt_v1",
    "validate_evaluation_accepted_attempt_outcome_v1",
]


EVALUATION_LLM_CACHE_MODES = frozenset({"bypass", "read_only", "read_write"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def build_evaluation_request_body_v1(
    *,
    profile: Mapping[str, Any],
    role_id: str,
    source: Mapping[str, Any],
    capability: Mapping[str, Any],
    rendered_prompt: RenderedPromptV3,
) -> dict[str, Any]:
    normalized_profile = validate_pipeline_profile(profile)
    role = _profile_role(normalized_profile, role_id)
    normalized_source = validate_api_source(source)
    normalized_capability = validate_capability_evidence(capability)
    _validate_rendered_prompt(role_id, rendered_prompt)
    _require_target_records(
        role=role,
        source=normalized_source,
        capability=normalized_capability,
    )
    return _build_request_body(
        role=role,
        source=normalized_source,
        capability=normalized_capability,
        rendered_prompt=rendered_prompt,
    )


def build_evaluation_input_bindings_v1(
    *,
    scorer_input_packet_sha256: str,
    rendered_prompt: RenderedPromptV3,
    request_body: Mapping[str, Any],
    extra_bindings: Sequence[Mapping[str, str]] = (),
) -> list[dict[str, str]]:
    packet_sha256 = _require_sha256(
        scorer_input_packet_sha256, "$.scorer_input_packet_sha256"
    )
    rendered_sha256 = _require_sha256(
        rendered_prompt.rendered_prompt_sha256,
        "$.rendered_prompt.rendered_prompt_sha256",
    )
    if sha256(rendered_prompt.rendered_prompt.encode("utf-8")).hexdigest() != rendered_sha256:
        raise ContractValidationError(
            "rendered_prompt_hash",
            "$.rendered_prompt.rendered_prompt_sha256",
            "rendered prompt hash differs from its text",
        )
    rows = [
        {"name": "scorer_input_packet", "sha256": packet_sha256},
        {"name": "rendered_prompt", "sha256": rendered_sha256},
        {
            "name": "transport_request_body",
            "sha256": canonical_sha256(request_body),
        },
    ]
    seen = {row["name"] for row in rows}
    for index, raw in enumerate(extra_bindings):
        path = f"$.extra_bindings[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"name", "sha256"}:
            raise ContractValidationError(
                "input_binding_shape",
                path,
                "extra binding requires exactly name and sha256",
            )
        name = raw["name"]
        if (
            not isinstance(name, str)
            or not name
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,191}", name) is None
        ):
            raise ContractValidationError(
                "input_binding_name",
                f"{path}.name",
                "binding name is not a canonical identifier",
            )
        if name in seen:
            raise ContractValidationError(
                "input_binding_duplicate",
                f"{path}.name",
                "binding name is repeated",
            )
        rows.append(
            {
                "name": name,
                "sha256": _require_sha256(raw["sha256"], f"{path}.sha256"),
            }
        )
        seen.add(name)
    return rows


def execute_evaluation_llm_attempt_v1(
    *,
    backend: SharedLlmBackend,
    seal: Mapping[str, Any],
    logical_request_id: str,
    rendered_prompt: RenderedPromptV3,
    cache_mode: str,
    cost_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one physical attempt and apply the role's local semantic validator.

    This function never retries, chooses a fallback, or advances an attempt
    index. A semantic rejection requires a newly resolved seal.
    """

    if cache_mode not in EVALUATION_LLM_CACHE_MODES:
        raise ContractValidationError(
            "cache_mode",
            "$.cache_mode",
            f"cache_mode must be one of {sorted(EVALUATION_LLM_CACHE_MODES)}",
        )
    if cost_fact is not None:
        try:
            canonical_sha256(cost_fact)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                "cost_fact",
                "$.cost_fact",
                "cost fact must be finite canonical JSON",
            ) from exc
    normalized_seal = validate_resolved_llm_run_seal(seal)
    role_id = normalized_seal["role_id"]
    if role_id not in EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "role_id",
            "$.seal.role_id",
            "seal is not an Evaluation semantic LLM role",
        )
    if normalized_seal["fallback_plan"]["enabled"]:
        raise ContractValidationError(
            "fallback_forbidden",
            "$.seal.fallback_plan",
            "Evaluation semantic adapters do not select fallback targets",
        )
    _validate_rendered_prompt(role_id, rendered_prompt)
    role = normalized_seal["role_binding"]["record"]
    _require_role_contract(role_id, role)
    request_body = _build_request_body(
        role=role,
        source=normalized_seal["primary"]["source"],
        capability=normalized_seal["primary"]["capability"],
        rendered_prompt=rendered_prompt,
    )
    _require_seal_input(
        normalized_seal,
        name="rendered_prompt",
        expected_sha256=rendered_prompt.rendered_prompt_sha256,
    )
    _require_seal_input(
        normalized_seal,
        name="transport_request_body",
        expected_sha256=canonical_sha256(request_body),
    )
    _require_seal_input(normalized_seal, name="scorer_input_packet")
    cache_read = cache_mode in {"read_only", "read_write"}
    cache_write = cache_mode == "read_write"
    backend_result = backend.execute_one_attempt(
        seal=normalized_seal,
        logical_request_id=logical_request_id,
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=request_body,
        target_index=0,
        allow_response_cache_read=cache_read,
        allow_response_cache_write=cache_write,
        cost_fact=cost_fact,
    )
    response_bytes = backend_result["response_bytes"]
    try:
        response_text, finish_reason = _extract_response_text(
            protocol=normalized_seal["primary"]["source"]["protocol"],
            response_bytes=response_bytes,
        )
    except ContractValidationError as exc:
        return _semantic_rejection(
            seal=normalized_seal,
            logical_request_id=logical_request_id,
            backend_result=backend_result,
            response_text=None,
            category="parse",
            error=exc,
        )
    if finish_reason != "stop":
        return _semantic_rejection(
            seal=normalized_seal,
            logical_request_id=logical_request_id,
            backend_result=backend_result,
            response_text=response_text,
            category="pipeline_semantic",
            error=ContractValidationError(
                "finish_reason",
                "$.provider_response",
                f"provider finish reason {finish_reason!r} is not complete",
            ),
        )
    try:
        semantic_output = _parse_role_response(role_id, response_text)
    except ContractValidationError as exc:
        return _semantic_rejection(
            seal=normalized_seal,
            logical_request_id=logical_request_id,
            backend_result=backend_result,
            response_text=response_text,
            category="canonical_schema",
            error=exc,
        )
    return _attempt_outcome(
        status="accepted",
        seal=normalized_seal,
        logical_request_id=logical_request_id,
        backend_result=backend_result,
        response_text=response_text,
        semantic_output=semantic_output,
        semantic_error=None,
    )


def validate_evaluation_accepted_attempt_outcome_v1(
    value: Mapping[str, Any],
    *,
    seal: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate one persisted accepted outcome before checkpoint reuse."""

    normalized_seal = validate_resolved_llm_run_seal(seal)
    row = require_mapping(value, path="$.outcome")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "status",
            "role_id",
            "seal_sha256",
            "logical_request_id",
            "backend_status",
            "provider_called",
            "response_artifact_sha256",
            "semantic_response_sha256",
            "response_text",
            "semantic_output",
            "semantic_error",
            "usage",
            "cache_observation",
        },
        path="$.outcome",
    )
    role_id = require_enum(row["role_id"], EVALUATION_LLM_ROLE_IDS, path="$.outcome.role_id")
    if role_id != normalized_seal["role_id"]:
        raise ContractValidationError("role_binding", "$.outcome.role_id", "outcome role differs from its seal")
    seal_sha256 = require_sha256(row["seal_sha256"], path="$.outcome.seal_sha256")
    if seal_sha256 != normalized_seal["seal_sha256"]:
        raise ContractValidationError("seal_binding", "$.outcome.seal_sha256", "outcome references another seal")
    provider_called = row["provider_called"]
    if not isinstance(provider_called, bool):
        raise ContractValidationError("type", "$.outcome.provider_called", "expected boolean")
    response_text = require_string(row["response_text"], path="$.outcome.response_text")
    response_sha256 = require_sha256(row["semantic_response_sha256"], path="$.outcome.semantic_response_sha256")
    if sha256(response_text.encode("utf-8")).hexdigest() != response_sha256:
        raise ContractValidationError("response_hash", "$.outcome.semantic_response_sha256", "response text hash drift")
    semantic_output = _parse_role_response(role_id, response_text)
    if row["semantic_output"] != semantic_output:
        raise ContractValidationError("semantic_output", "$.outcome.semantic_output", "persisted semantic output differs from local validation")
    if row["semantic_error"] is not None:
        raise ContractValidationError("semantic_error", "$.outcome.semantic_error", "accepted outcome cannot carry a semantic error")
    usage = None if row["usage"] is None else validate_llm_attempt_usage(require_mapping(row["usage"], path="$.outcome.usage"))
    cache = None if row["cache_observation"] is None else validate_cache_observation(require_mapping(row["cache_observation"], path="$.outcome.cache_observation"))
    backend_status = require_enum(row["backend_status"], {"provider_succeeded", "cache_hit"}, path="$.outcome.backend_status")
    if backend_status == "provider_succeeded":
        if provider_called is not True or usage is None:
            raise ContractValidationError("backend_binding", "$.outcome", "provider success requires a physical usage row")
    elif provider_called is not False or usage is not None or cache is None or cache["lookup_status"] != "hit":
        raise ContractValidationError("backend_binding", "$.outcome", "cache hit requires one hit observation and no physical usage")
    for nested, path in ((usage, "$.outcome.usage"), (cache, "$.outcome.cache_observation")):
        if nested is not None and nested["seal_sha256"] != seal_sha256:
            raise ContractValidationError("seal_binding", path, "nested attempt evidence references another seal")
    return {
        "schema_id": require_enum(row["schema_id"], {"EvaluationLlmAttemptOutcomeV1"}, path="$.outcome.schema_id"),
        "schema_version": require_enum(row["schema_version"], {"1.0.0"}, path="$.outcome.schema_version"),
        "status": require_enum(row["status"], {"accepted"}, path="$.outcome.status"),
        "role_id": role_id,
        "seal_sha256": seal_sha256,
        "logical_request_id": require_string(row["logical_request_id"], path="$.outcome.logical_request_id"),
        "backend_status": backend_status,
        "provider_called": provider_called,
        "response_artifact_sha256": require_sha256(row["response_artifact_sha256"], path="$.outcome.response_artifact_sha256"),
        "semantic_response_sha256": response_sha256,
        "response_text": response_text,
        "semantic_output": semantic_output,
        "semantic_error": None,
        "usage": usage,
        "cache_observation": cache,
    }


def _build_request_body(
    *,
    role: Mapping[str, Any],
    source: Mapping[str, Any],
    capability: Mapping[str, Any],
    rendered_prompt: RenderedPromptV3,
) -> dict[str, Any]:
    generation = role["generation"]
    schema = evaluation_response_schema_v1(role["role_id"])
    native_schema = capability["capability_kind"] == "native_structured_output"
    protocol = source["protocol"]
    prompt = rendered_prompt.rendered_prompt
    if role["structured_output"]["mode"] == "prompt_validated":
        prompt = (
            f"{prompt.rstrip()}\n\n"
            f"{EVALUATION_PROMPT_VALIDATED_JSON_OUTPUT_INSTRUCTION_V1}"
        )
    if protocol == "google_genai_generate_content":
        generation_config: dict[str, Any] = {
            "maxOutputTokens": generation["max_output_tokens"],
            "temperature": generation["temperature"],
            "topP": generation["top_p"],
            "responseMimeType": "application/json",
        }
        if generation["reasoning_effort"] == "none":
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}
        if native_schema:
            generation_config["responseJsonSchema"] = schema
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
    if protocol == "openai_chat_completions":
        response_format: dict[str, Any]
        if native_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": role["response_schema"]["id"],
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
        return {
            "model": role["primary"]["requested_model_id"],
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": generation["max_output_tokens"],
            "temperature": generation["temperature"],
            "top_p": generation["top_p"],
            "response_format": response_format,
        }
    if protocol == "openai_responses":
        if native_schema:
            response_format = {
                "type": "json_schema",
                "name": role["response_schema"]["id"],
                "strict": True,
                "schema": schema,
            }
        else:
            response_format = {"type": "json_object"}
        return {
            "model": role["primary"]["requested_model_id"],
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "max_output_tokens": generation["max_output_tokens"],
            "temperature": generation["temperature"],
            "top_p": generation["top_p"],
            "text": {"format": response_format},
        }
    if protocol == "local_in_process":
        return {
            "prompt": prompt,
            "generation": {
                "max_output_tokens": generation["max_output_tokens"],
                "temperature": generation["temperature"],
                "top_p": generation["top_p"],
                "reasoning_effort": generation["reasoning_effort"],
            },
            "response_format": {
                "type": "json_schema" if native_schema else "json_object",
                "schema": schema if native_schema else None,
            },
        }
    raise ContractValidationError(
        "protocol",
        "$.seal.primary.source.protocol",
        f"unsupported shared protocol {protocol!r}",
    )


def _extract_response_text(
    *, protocol: str, response_bytes: bytes
) -> tuple[str, str]:
    row = _strict_json_object(response_bytes)
    if protocol == "openai_chat_completions":
        choices = row.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
        ):
            raise _provider_shape("OpenAI choices are absent")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise _provider_shape("OpenAI message is absent")
        return (
            _required_response_text(message.get("content")),
            _normalize_finish_reason(choices[0].get("finish_reason")),
        )
    if protocol == "openai_responses":
        parts: list[str] = []
        output = row.get("output")
        if not isinstance(output, list):
            raise _provider_shape("Responses output is absent")
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, Mapping)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    parts.append(part["text"])
        return (
            _required_response_text("".join(parts)),
            "stop"
            if row.get("status") == "completed"
            else "length"
            if row.get("status") == "incomplete"
            else "unknown",
        )
    if protocol == "google_genai_generate_content":
        candidates = row.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], Mapping)
        ):
            raise _provider_shape("Google candidates are absent")
        content = candidates[0].get("content")
        if not isinstance(content, Mapping) or not isinstance(
            content.get("parts"), list
        ):
            raise _provider_shape("Google candidate content is absent")
        parts = [
            part["text"]
            for part in content["parts"]
            if isinstance(part, Mapping)
            and part.get("thought") is not True
            and isinstance(part.get("text"), str)
        ]
        return (
            _required_response_text("".join(parts)),
            _normalize_finish_reason(candidates[0].get("finishReason")),
        )
    if protocol == "local_in_process":
        return (
            _required_response_text(row.get("output_text")),
            _normalize_finish_reason(row.get("finish_reason")),
        )
    raise ContractValidationError(
        "protocol",
        "$.provider_response",
        f"unsupported response protocol {protocol!r}",
    )


def _parse_role_response(role_id: str, response_text: str) -> dict[str, Any]:
    if role_id == SF_BT_BACK_TRANSLATOR_ROLE_ID:
        return {"back_translation": _parse_back_translation_response(response_text)}
    if role_id == SF_BT_SEMANTIC_JUDGE_ROLE_ID:
        return parse_sf_bt_semantic_response_v3(response_text)
    if role_id == PJ_JUDGE_ROLE_ID:
        return parse_pj_response_v2(response_text)
    raise ContractValidationError(
        "role_id", "$.role_id", f"unsupported role {role_id!r}"
    )


def _semantic_rejection(
    *,
    seal: Mapping[str, Any],
    logical_request_id: str,
    backend_result: Mapping[str, Any],
    response_text: str | None,
    category: str,
    error: ContractValidationError,
) -> dict[str, Any]:
    semantic_error = {
        "category": category,
        "code": error.code,
        "path": error.path,
        "safe_message": str(error),
        "retry_requires_new_seal": True,
    }
    return _attempt_outcome(
        status="semantic_rejected",
        seal=seal,
        logical_request_id=logical_request_id,
        backend_result=backend_result,
        response_text=response_text,
        semantic_output=None,
        semantic_error=semantic_error,
    )


def _attempt_outcome(
    *,
    status: str,
    seal: Mapping[str, Any],
    logical_request_id: str,
    backend_result: Mapping[str, Any],
    response_text: str | None,
    semantic_output: Mapping[str, Any] | None,
    semantic_error: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_id": "EvaluationLlmAttemptOutcomeV1",
        "schema_version": "1.0.0",
        "status": status,
        "role_id": seal["role_id"],
        "seal_sha256": seal["seal_sha256"],
        "logical_request_id": logical_request_id,
        "backend_status": backend_result["status"],
        "provider_called": backend_result["provider_called"],
        "response_artifact_sha256": backend_result["artifact_sha256"],
        "semantic_response_sha256": (
            None
            if response_text is None
            else sha256(response_text.encode("utf-8")).hexdigest()
        ),
        "response_text": response_text,
        "semantic_output": (
            None if semantic_output is None else deepcopy(dict(semantic_output))
        ),
        "semantic_error": (
            None if semantic_error is None else deepcopy(dict(semantic_error))
        ),
        "usage": deepcopy(backend_result.get("usage")),
        "cache_observation": deepcopy(backend_result.get("cache_observation")),
    }


def _profile_role(profile: Mapping[str, Any], role_id: str) -> dict[str, Any]:
    if role_id not in EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "role_id", "$.role_id", f"unsupported role {role_id!r}"
        )
    role = next(
        (row for row in profile["role_bindings"] if row["role_id"] == role_id),
        None,
    )
    if role is None:
        raise ContractValidationError(
            "role_id", "$.profile.role_bindings", "profile lacks requested role"
        )
    _require_role_contract(role_id, role)
    return role


def _require_role_contract(role_id: str, role: Mapping[str, Any]) -> None:
    expected = evaluation_role_contract_v1(role_id)
    for field in ("prompt", "response_schema", "validator"):
        if role[field] != expected[field]:
            raise ContractValidationError(
                "role_contract",
                f"$.seal.role_binding.record.{field}",
                f"sealed {field} differs from the Evaluation role contract",
            )
    if role["semantic_retry"] != {
        "max_retries": 0,
        "retryable_categories": [],
    }:
        raise ContractValidationError(
            "semantic_retry",
            "$.seal.role_binding.record.semantic_retry",
            "Evaluation semantic retry must use a newly resolved seal",
        )
    if role["transport_retry"]["max_retries"] != 0:
        raise ContractValidationError(
            "transport_retry",
            "$.seal.role_binding.record.transport_retry",
            "this adapter executes one physical attempt and no retry loop",
        )


def _require_target_records(
    *,
    role: Mapping[str, Any],
    source: Mapping[str, Any],
    capability: Mapping[str, Any],
) -> None:
    target = role["primary"]
    if (
        target["source_id"] != source["source_id"]
        or target["source_revision"] != source["source_revision"]
        or target["source_record_sha256"] != canonical_sha256(source)
    ):
        raise ContractValidationError(
            "source_binding",
            "$.source",
            "source record differs from the role target",
        )
    if (
        target["capability_id"] != capability["capability_id"]
        or target["capability_revision"] != capability["capability_revision"]
        or target["capability_record_sha256"] != canonical_sha256(capability)
    ):
        raise ContractValidationError(
            "capability_binding",
            "$.capability",
            "capability record differs from the role target",
        )
    for field in (
        "source_id",
        "source_revision",
        "adapter_id",
        "protocol",
        "route_id",
        "base_url",
    ):
        if capability[field] != source[field]:
            raise ContractValidationError(
                "capability_source_binding",
                f"$.capability.{field}",
                "capability differs from its source",
            )
    if capability["requested_model_id"] != target["requested_model_id"]:
        raise ContractValidationError(
            "model_binding",
            "$.capability.requested_model_id",
            "capability model differs from the role target",
        )
    if capability["schema_sha256"] != role["response_schema"]["sha256"]:
        raise ContractValidationError(
            "schema_binding",
            "$.capability.schema_sha256",
            "capability schema differs from the role response schema",
        )
    if (
        capability["local_validator_id"] != role["validator"]["id"]
        or capability["local_validator_sha256"] != role["validator"]["sha256"]
    ):
        raise ContractValidationError(
            "validator_binding",
            "$.capability",
            "capability validator differs from the role validator",
        )


def _validate_rendered_prompt(
    role_id: str, rendered_prompt: RenderedPromptV3
) -> None:
    if not isinstance(rendered_prompt, RenderedPromptV3):
        raise ContractValidationError(
            "rendered_prompt_type",
            "$.rendered_prompt",
            "rendered_prompt must be RenderedPromptV3",
        )
    expected = evaluation_role_contract_v1(role_id)["prompt"]
    if (
        rendered_prompt.candidate_id != expected["id"]
        or rendered_prompt.prompt_sha256 != expected["sha256"]
    ):
        raise ContractValidationError(
            "prompt_binding",
            "$.rendered_prompt",
            "rendered prompt belongs to a different role contract",
        )
    observed = sha256(rendered_prompt.rendered_prompt.encode("utf-8")).hexdigest()
    if observed != rendered_prompt.rendered_prompt_sha256:
        raise ContractValidationError(
            "rendered_prompt_hash",
            "$.rendered_prompt.rendered_prompt_sha256",
            "rendered prompt hash differs from its text",
        )


def _require_seal_input(
    seal: Mapping[str, Any],
    *,
    name: str,
    expected_sha256: str | None = None,
) -> None:
    binding = next(
        (row for row in seal["input_bindings"] if row["name"] == name),
        None,
    )
    if binding is None:
        raise ContractValidationError(
            "input_binding",
            "$.seal.input_bindings",
            f"seal lacks required {name} binding",
        )
    if expected_sha256 is not None and binding["sha256"] != expected_sha256:
        raise ContractValidationError(
            "input_binding",
            f"$.seal.input_bindings.{name}",
            f"{name} hash differs from the current input",
        )


def _strict_json_object(response_bytes: bytes) -> dict[str, Any]:
    if not isinstance(response_bytes, bytes):
        raise _provider_shape("provider response is not bytes")
    try:
        decoded = response_bytes.decode("utf-8")
        parsed = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _provider_shape("provider response is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise _provider_shape("provider response is not a JSON object")
    return parsed


def _required_response_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _provider_shape("provider response text is absent")
    return value


def _normalize_finish_reason(value: Any) -> str:
    if value is None:
        return "unknown"
    return {
        "stop": "stop",
        "completed": "stop",
        "max_tokens": "length",
        "length": "length",
        "content_filter": "content_filter",
        "safety": "safety",
        "error": "error",
    }.get(str(value).casefold(), "unknown")


def _provider_shape(message: str) -> ContractValidationError:
    return ContractValidationError(
        "provider_response_shape", "$.provider_response", message
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _require_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ContractValidationError(
            "sha256", path, "value must be a lowercase SHA-256 digest"
        )
    return value
