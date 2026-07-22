from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_exact_keys,
    require_mapping,
    require_string,
)
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    EVALUATION_PROMPT_VALIDATED_JSON_OUTPUT_INSTRUCTION_V1,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    evaluation_response_schema_v1,
    evaluation_role_budget_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.scorer_prompts_v3 import (
    parse_pj_response_v2,
    parse_sf_bt_semantic_response_v3,
)
from pipeline.llm_backend import (
    ContractValidationError as SharedContractValidationError,
    SharedLlmCapabilityProbe,
    canonical_sha256,
    create_capability_probe_seal,
    validate_api_source,
)


__all__ = [
    "EVALUATION_CAPABILITY_PROBE_PROFILE_ID",
    "EVALUATION_CAPABILITY_PROBE_PROFILE_REVISION",
    "EvaluationCapabilityProbePlanV1",
    "build_clean_evaluation_probe_implementation_binding_v1",
    "build_evaluation_capability_probe_plan_v1",
    "build_evaluation_json_object_capability_probe_plan_v1",
    "evaluation_capability_probe_implementation_sha256_v1",
    "execute_evaluation_capability_probe_once_v1",
    "validate_evaluation_capability_payload_v1",
]


EVALUATION_CAPABILITY_PROBE_PROFILE_ID = (
    "evaluation_live_pilot_capability_probe_v5"
)
EVALUATION_CAPABILITY_PROBE_PROFILE_REVISION = "v5"
SHARED_CAPABILITY_PROBE_REVISION = (
    "534b180aae8f685f2014ec23dcb32d741fb75480"
)

_JSON_SCHEMA_DIALECT = "json_schema_2020_12"
_CAPABILITY_KIND = "native_structured_output"
_JSON_OBJECT_CAPABILITY_KIND = "json_object"
_THIRD_PARTY_PROBE_PROFILE_ID = "evaluation_third_party_json_object_probe_v6"
_THIRD_PARTY_PROBE_PROFILE_REVISION = "v6"
_OFFICIAL_GOOGLE_BASE_URLS = frozenset(
    {
        "https://generativelanguage.googleapis.com/v1",
        "https://generativelanguage.googleapis.com/v1beta",
    }
)
_OFFICIAL_OPENAI_BASE_URLS = frozenset({"https://api.openai.com/v1"})
_PROBE_MAX_OUTPUT_TOKENS = 512
_THIRD_PARTY_PROBE_MAX_PROMPT_TOKENS = 8_192
_THIRD_PARTY_PROBE_MAX_COMPLETION_CERTIFICATION_TOKENS = 2_048
_PROBE_LIMITS = {
    "max_calls": 1,
    "max_prompt_utf8_bytes": 32_768,
    "max_response_utf8_bytes": 16_384,
    "max_prompt_tokens": 4_096,
    "max_completion_tokens": _PROBE_MAX_OUTPUT_TOKENS,
    "max_total_tokens": 4_096 + _PROBE_MAX_OUTPUT_TOKENS,
    "request_timeout_ms": 120_000,
}
_ID_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_AUTHORITY_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_AUTHORITY_MARKERS = (
    "gold",
    "oracle",
    "human_reference",
    "human_translation",
    "reference_translation",
    "result_callback",
)

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
_IMPLEMENTATION_PATHS = (
    Path("THESIS_RUNTIME_TOOL/pipeline/eval/live_pilot_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/eval/live_pilot_capability_run_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/eval/llm_profiles_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/eval/scorer_prompts_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/eval/sf_bt_stage_contracts_v1.py"),
)

_SYNTHETIC_PROMPTS = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: (
        "This is a transport capability check, not a scored evaluation. "
        "Translate the following Vietnamese sentence into English. Return JSON "
        "only, with exactly this shape: "
        '{"back_translation":"English translation"}. '
        "Sentence: He thong luu ba hang du lieu."
    ),
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: (
        "This is a transport capability check, not a scored evaluation. "
        "Compare the meanings of these two synthetic passages. Return JSON "
        "only, with exactly these fields: "
        '{"score":100,"flags":[],"note":"one short English sentence"}. '
        "score must be one of 0, 25, 50, 75, or 100. flags may contain only "
        "semantic_mismatch, numeric_mismatch, negation_mismatch, "
        "coverage_mismatch, untranslated_residue, or format_only. "
        "Passage A: The system preserved three rows. Passage B: Three rows "
        "were preserved by the system."
    ),
    PJ_JUDGE_ROLE_ID: (
        "This is a transport capability check, not a scored evaluation. "
        "Compare two synthetic Vietnamese translations of the English source. "
        "Return JSON only, with exactly these fields: "
        '{"overall_verdict":"tie","style_verdict":"tie","tags":[],'
        '"note":"one short English sentence"}. '
        "Each verdict must be candidate_1, candidate_2, or tie. tags may "
        "contain only grammar, naturalness, word_choice, terminology, meaning, "
        "omission_addition, formatting, or tone_voice. The note must contain "
        "at most 25 English words. "
        "Source: The system preserved three rows. Candidate 1: He thong giu "
        "lai ba hang. Candidate 2: He thong da luu ba dong."
    ),
}


@dataclass(frozen=True, slots=True)
class EvaluationCapabilityProbePlanV1:
    role_id: str
    source: dict[str, Any]
    response_schema: dict[str, Any]
    request_body: dict[str, Any]
    implementation_binding: dict[str, str]
    seal: dict[str, Any]


def build_evaluation_capability_probe_plan_v1(
    *,
    role_id: str,
    source: Mapping[str, Any],
    requested_model_id: str,
    accepted_observed_model_ids: Sequence[str],
    probe_run_id: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str],
    capability_revision: str = "official_native_json_schema_v5",
) -> EvaluationCapabilityProbePlanV1:
    """Seal one schema-only qualification call for one Evaluation LLM role."""

    return _build_evaluation_capability_probe_plan(
        role_id=role_id,
        source=source,
        requested_model_id=requested_model_id,
        accepted_observed_model_ids=accepted_observed_model_ids,
        probe_run_id=probe_run_id,
        issued_at_utc=issued_at_utc,
        implementation_binding=implementation_binding,
        capability_revision=capability_revision,
        capability_kind=_CAPABILITY_KIND,
        probe_profile_id=EVALUATION_CAPABILITY_PROBE_PROFILE_ID,
        probe_profile_revision=EVALUATION_CAPABILITY_PROBE_PROFILE_REVISION,
        source_policy=_require_official_native_structured_output_source,
    )


def build_evaluation_json_object_capability_probe_plan_v1(
    *,
    role_id: str,
    source: Mapping[str, Any],
    requested_model_id: str,
    accepted_observed_model_ids: Sequence[str],
    probe_run_id: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str],
    capability_revision: str = "third_party_json_object_v6",
) -> EvaluationCapabilityProbePlanV1:
    """Seal a third-party JSON-object syntax canary with local validation."""

    return _build_evaluation_capability_probe_plan(
        role_id=role_id,
        source=source,
        requested_model_id=requested_model_id,
        accepted_observed_model_ids=accepted_observed_model_ids,
        probe_run_id=probe_run_id,
        issued_at_utc=issued_at_utc,
        implementation_binding=implementation_binding,
        capability_revision=capability_revision,
        capability_kind=_JSON_OBJECT_CAPABILITY_KIND,
        probe_profile_id=_THIRD_PARTY_PROBE_PROFILE_ID,
        probe_profile_revision=_THIRD_PARTY_PROBE_PROFILE_REVISION,
        source_policy=_require_third_party_json_object_source,
    )


def _build_evaluation_capability_probe_plan(
    *,
    role_id: str,
    source: Mapping[str, Any],
    requested_model_id: str,
    accepted_observed_model_ids: Sequence[str],
    probe_run_id: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str],
    capability_revision: str,
    capability_kind: str,
    probe_profile_id: str,
    probe_profile_revision: str,
    source_policy: Callable[[Mapping[str, Any]], None],
) -> EvaluationCapabilityProbePlanV1:

    if role_id not in EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "role_id", "$.role_id", f"unsupported Evaluation role {role_id!r}"
        )
    normalized_source = _validate_source(source)
    source_policy(normalized_source)
    _reject_forbidden_authority(normalized_source, path="$.source")
    model_id = require_string(
        requested_model_id, path="$.requested_model_id", maximum=256
    )
    observed_models = _accepted_models(accepted_observed_model_ids, model_id)
    _reject_forbidden_authority(model_id, path="$.requested_model_id")
    _reject_forbidden_authority(
        observed_models, path="$.accepted_observed_model_ids"
    )
    binding = _validate_implementation_binding(implementation_binding)
    contract = evaluation_role_contract_v1(role_id)
    generation = evaluation_role_budget_v1(role_id)["generation"]
    schema = evaluation_response_schema_v1(role_id)
    if canonical_sha256(schema) != contract["response_schema"]["sha256"]:
        raise ContractValidationError(
            "schema_hash",
            "$.response_schema",
            "Evaluation response schema differs from its role contract",
        )
    request_body = _request_body(
        source=normalized_source,
        requested_model_id=model_id,
        role_id=role_id,
        response_schema=schema,
        schema_name=contract["response_schema"]["id"],
        generation=generation,
        capability_kind=capability_kind,
    )
    capability_id = _capability_id(
        source=normalized_source,
        requested_model_id=model_id,
        role_id=role_id,
        schema_sha256=contract["response_schema"]["sha256"],
        validator_sha256=contract["validator"]["sha256"],
        capability_kind=capability_kind,
    )
    intent = {
        "capability_id": capability_id,
        "capability_revision": require_string(
            capability_revision, path="$.capability_revision", maximum=192
        ),
        "requested_model_id": model_id,
        "accepted_observed_model_ids": observed_models,
        "capability_kind": capability_kind,
        "schema_name": contract["response_schema"]["id"],
        "schema_dialect": _JSON_SCHEMA_DIALECT,
        "schema_sha256": contract["response_schema"]["sha256"],
        "local_validator_id": contract["validator"]["id"],
        "local_validator_sha256": contract["validator"]["sha256"],
    }
    limits = deepcopy(_PROBE_LIMITS)
    if capability_kind == _JSON_OBJECT_CAPABILITY_KIND:
        limits["max_prompt_tokens"] = _THIRD_PARTY_PROBE_MAX_PROMPT_TOKENS
        limits["max_completion_tokens"] = (
            _THIRD_PARTY_PROBE_MAX_COMPLETION_CERTIFICATION_TOKENS
        )
        limits["max_total_tokens"] = (
            _THIRD_PARTY_PROBE_MAX_PROMPT_TOKENS
            + _THIRD_PARTY_PROBE_MAX_COMPLETION_CERTIFICATION_TOKENS
        )
    try:
        seal = create_capability_probe_seal(
            source=normalized_source,
            consumer_workstream="evaluation",
            role_id=role_id,
            probe_run_id=probe_run_id,
            probe_profile_id=probe_profile_id,
            probe_profile_revision=probe_profile_revision,
            implementation_binding=binding,
            capability_intent=intent,
            response_schema=schema,
            request_body=request_body,
            limits=limits,
            issued_at_utc=issued_at_utc,
        )
    except SharedContractValidationError as exc:
        raise ContractValidationError(
            "shared_probe_contract",
            "$.probe",
            f"shared capability probe rejected the Evaluation plan: {exc}",
        ) from exc
    return EvaluationCapabilityProbePlanV1(
        role_id=role_id,
        source=deepcopy(normalized_source),
        response_schema=deepcopy(schema),
        request_body=deepcopy(request_body),
        implementation_binding=deepcopy(binding),
        seal=deepcopy(seal),
    )


def execute_evaluation_capability_probe_once_v1(
    *,
    probe: SharedLlmCapabilityProbe,
    plan: EvaluationCapabilityProbePlanV1,
    cost_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = evaluation_role_contract_v1(plan.role_id)
    result = probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=lambda payload: validate_evaluation_capability_payload_v1(
            plan.role_id, payload
        ),
        local_validator_id=contract["validator"]["id"],
        local_validator_sha256=contract["validator"]["sha256"],
        cost_fact=cost_fact,
    )
    if set(result) != {
        "status",
        "provider_called",
        "probe_seal_sha256",
        "receipt",
        "capability_evidence",
    }:
        raise ContractValidationError(
            "probe_result", "$.probe_result", "shared probe exposed unknown fields"
        )
    if result["provider_called"] is not True:
        raise ContractValidationError(
            "probe_result",
            "$.probe_result.provider_called",
            "capability probe must represent exactly one physical provider call",
        )
    if result["probe_seal_sha256"] != plan.seal["seal_sha256"]:
        raise ContractValidationError(
            "probe_result",
            "$.probe_result.probe_seal_sha256",
            "capability result belongs to another sealed probe",
        )
    return deepcopy(result)


def validate_evaluation_capability_payload_v1(
    role_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply the same local semantic response validator used by live scoring."""

    row = require_mapping(payload, path="$.capability_payload")
    encoded = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if role_id == SF_BT_BACK_TRANSLATOR_ROLE_ID:
        require_exact_keys(
            row, required={"back_translation"}, path="$.capability_payload"
        )
        return {
            "back_translation": require_string(
                row["back_translation"],
                path="$.capability_payload.back_translation",
            )
        }
    if role_id == SF_BT_SEMANTIC_JUDGE_ROLE_ID:
        return parse_sf_bt_semantic_response_v3(encoded)
    if role_id == PJ_JUDGE_ROLE_ID:
        return parse_pj_response_v2(encoded)
    raise ContractValidationError(
        "role_id", "$.role_id", f"unsupported Evaluation role {role_id!r}"
    )


def build_clean_evaluation_probe_implementation_binding_v1(
    repo_root: Path = _REPO_ROOT,
) -> dict[str, str]:
    root = Path(repo_root).resolve()
    status = _git_text(root, "status", "--porcelain")
    if status:
        raise ContractValidationError(
            "dirty_worktree",
            "$.implementation_binding",
            "live Evaluation capability probes require a clean worktree",
        )
    revision = _git_text(root, "rev-parse", "HEAD")
    if _GIT_OID_RE.fullmatch(revision) is None:
        raise ContractValidationError(
            "consumer_revision",
            "$.implementation_binding.consumer_revision",
            "consumer Git revision is invalid",
        )
    return {
        "shared_core_revision": SHARED_CAPABILITY_PROBE_REVISION,
        "consumer_revision": revision,
        "consumer_implementation_sha256": (
            evaluation_capability_probe_implementation_sha256_v1(root)
        ),
    }


def evaluation_capability_probe_implementation_sha256_v1(
    repo_root: Path = _REPO_ROOT,
) -> str:
    root = Path(repo_root).resolve()
    files: list[dict[str, str]] = []
    for relative_path in _IMPLEMENTATION_PATHS:
        path = root / relative_path
        if not path.is_file():
            raise ContractValidationError(
                "implementation_file",
                "$.implementation_binding",
                f"required implementation file is absent: {relative_path.as_posix()}",
            )
        files.append(
            {
                "path": relative_path.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return canonical_sha256(
        {
            "schema_version": "evaluation_capability_probe_implementation_v1",
            "shared_core_revision": SHARED_CAPABILITY_PROBE_REVISION,
            "files": files,
        }
    )


def _request_body(
    *,
    source: Mapping[str, Any],
    requested_model_id: str,
    role_id: str,
    response_schema: Mapping[str, Any],
    schema_name: str,
    generation: Mapping[str, Any],
    capability_kind: str,
) -> dict[str, Any]:
    prompt = _SYNTHETIC_PROMPTS[role_id]
    if capability_kind == _JSON_OBJECT_CAPABILITY_KIND:
        prompt = (
            f"{prompt.rstrip()} "
            f"{EVALUATION_PROMPT_VALIDATED_JSON_OUTPUT_INSTRUCTION_V1}"
        )
    protocol = source["protocol"]
    if protocol == "google_genai_generate_content":
        generation_config: dict[str, Any] = {
            "temperature": generation["temperature"],
            "topP": generation["top_p"],
            "maxOutputTokens": _PROBE_MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            **(
                {"thinkingConfig": {"thinkingBudget": 0}}
                if generation["reasoning_effort"] == "none"
                else {}
            ),
        }
        if capability_kind == _CAPABILITY_KIND:
            generation_config["responseJsonSchema"] = deepcopy(
                dict(response_schema)
            )
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
    if protocol == "openai_chat_completions":
        response_format: dict[str, Any]
        if capability_kind == _CAPABILITY_KIND:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": deepcopy(dict(response_schema)),
                },
            }
        else:
            response_format = {"type": "json_object"}
        request_body = {
            "model": requested_model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": generation["temperature"],
            "top_p": generation["top_p"],
            "response_format": response_format,
        }
        if capability_kind == _JSON_OBJECT_CAPABILITY_KIND:
            # Match the Evaluation runtime envelope used by the same route.
            request_body["max_completion_tokens"] = _PROBE_MAX_OUTPUT_TOKENS
        else:
            # Preserve the already-qualified official-source probe envelope.
            request_body["max_tokens"] = _PROBE_MAX_OUTPUT_TOKENS
        return request_body
    if protocol == "openai_responses":
        if capability_kind == _CAPABILITY_KIND:
            response_format = {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": deepcopy(dict(response_schema)),
            }
        else:
            response_format = {"type": "json_object"}
        return {
            "model": requested_model_id,
            "input": prompt,
            "temperature": generation["temperature"],
            "top_p": generation["top_p"],
            "max_output_tokens": _PROBE_MAX_OUTPUT_TOKENS,
            "text": {"format": response_format},
        }
    raise ContractValidationError(
        "protocol",
        "$.source.protocol",
        f"unsupported capability-probe protocol {protocol!r}",
    )


def _validate_source(source: Mapping[str, Any]) -> dict[str, Any]:
    try:
        row = validate_api_source(source)
    except SharedContractValidationError as exc:
        raise ContractValidationError(
            "api_source", "$.source", f"shared API source validation failed: {exc}"
        ) from exc
    if not row["enabled"]:
        raise ContractValidationError(
            "api_source", "$.source.enabled", "capability source is disabled"
        )
    return row


def _require_official_native_structured_output_source(
    source: Mapping[str, Any],
) -> None:
    protocol = source["protocol"]
    base_url = source["base_url"]
    if protocol == "google_genai_generate_content":
        valid = (
            source["source_class"] == "remote_api"
            and source["endpoint_class"] == "remote"
            and source["adapter_id"] == "google_genai_rest_v1"
            and source["route_id"] == "models_generate_content"
            and base_url in _OFFICIAL_GOOGLE_BASE_URLS
        )
    elif protocol == "openai_chat_completions":
        valid = (
            source["source_class"] == "remote_api"
            and source["endpoint_class"] == "remote"
            and source["adapter_id"] == "openai_python_v1"
            and source["route_id"] == "chat_completions_create"
            and base_url in _OFFICIAL_OPENAI_BASE_URLS
        )
    elif protocol == "openai_responses":
        valid = (
            source["source_class"] == "remote_api"
            and source["endpoint_class"] == "remote"
            and source["adapter_id"] == "openai_responses_v1"
            and source["route_id"] == "responses_create"
            and base_url in _OFFICIAL_OPENAI_BASE_URLS
        )
    else:
        valid = False
    if not valid:
        raise ContractValidationError(
            "native_structured_output_authority",
            "$.source",
            "native Structured Output probes require a direct official Google or OpenAI source",
        )


def _require_third_party_json_object_source(source: Mapping[str, Any]) -> None:
    openai_compatible = (
        source["source_class"] == "remote_api"
        and source["endpoint_class"] == "remote"
        and source["protocol"] == "openai_chat_completions"
        and source["adapter_id"] == "openai_compatible_chat_v1"
        and source["route_id"] == "chat_completions"
        and source["base_url"] not in _OFFICIAL_OPENAI_BASE_URLS
        and source["base_url"] not in _OFFICIAL_GOOGLE_BASE_URLS
    )
    google_compatible = (
        source["source_class"] == "remote_api"
        and source["endpoint_class"] == "remote"
        and source["protocol"] == "google_genai_generate_content"
        and source["adapter_id"] == "google_genai_rest_v1"
        and source["route_id"] == "models_generate_content"
        and source["base_url"] not in _OFFICIAL_GOOGLE_BASE_URLS
        and source["base_url"] not in _OFFICIAL_OPENAI_BASE_URLS
    )
    if not (openai_compatible or google_compatible):
        raise ContractValidationError(
            "third_party_json_object_authority",
            "$.source",
            "third-party JSON-object probes require a non-official OpenAI-compatible chat or Google-compatible generate-content source",
        )


def _validate_implementation_binding(value: Mapping[str, str]) -> dict[str, str]:
    row = require_mapping(value, path="$.implementation_binding")
    require_exact_keys(
        row,
        required={
            "shared_core_revision",
            "consumer_revision",
            "consumer_implementation_sha256",
        },
        path="$.implementation_binding",
    )
    normalized = {
        "shared_core_revision": require_string(
            row["shared_core_revision"],
            path="$.implementation_binding.shared_core_revision",
        ),
        "consumer_revision": require_string(
            row["consumer_revision"],
            path="$.implementation_binding.consumer_revision",
        ),
        "consumer_implementation_sha256": require_string(
            row["consumer_implementation_sha256"],
            path="$.implementation_binding.consumer_implementation_sha256",
        ),
    }
    if _GIT_OID_RE.fullmatch(normalized["shared_core_revision"]) is None:
        raise ContractValidationError(
            "shared_core_revision",
            "$.implementation_binding.shared_core_revision",
            "shared core revision must be a 40-character Git object ID",
        )
    if normalized["shared_core_revision"] != SHARED_CAPABILITY_PROBE_REVISION:
        raise ContractValidationError(
            "shared_core_revision",
            "$.implementation_binding.shared_core_revision",
            "Evaluation probe is not bound to the accepted shared capability core",
        )
    if _GIT_OID_RE.fullmatch(normalized["consumer_revision"]) is None:
        raise ContractValidationError(
            "consumer_revision",
            "$.implementation_binding.consumer_revision",
            "consumer revision must be a 40-character Git object ID",
        )
    if _SHA256_RE.fullmatch(normalized["consumer_implementation_sha256"]) is None:
        raise ContractValidationError(
            "implementation_sha256",
            "$.implementation_binding.consumer_implementation_sha256",
            "consumer implementation hash must be SHA-256",
        )
    return normalized


def _accepted_models(values: Sequence[str], requested_model_id: str) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ContractValidationError(
            "accepted_models",
            "$.accepted_observed_model_ids",
            "accepted observed models must be a sequence",
        )
    normalized = [
        require_string(value, path="$.accepted_observed_model_ids[]", maximum=256)
        for value in values
    ]
    if not normalized:
        raise ContractValidationError(
            "accepted_models",
            "$.accepted_observed_model_ids",
            "at least one observed model identity is required",
        )
    if normalized != sorted(set(normalized)):
        raise ContractValidationError(
            "accepted_models",
            "$.accepted_observed_model_ids",
            "accepted observed models must be sorted and unique",
        )
    if requested_model_id not in normalized:
        raise ContractValidationError(
            "accepted_models",
            "$.accepted_observed_model_ids",
            "requested model must be accepted as an observed model",
        )
    return normalized


def _capability_id(
    *,
    source: Mapping[str, Any],
    requested_model_id: str,
    role_id: str,
    schema_sha256: str,
    validator_sha256: str,
    capability_kind: str,
) -> str:
    material = {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "requested_model_id": requested_model_id,
        "role_id": role_id,
        "schema_sha256": schema_sha256,
        "validator_sha256": validator_sha256,
    }
    if capability_kind != _CAPABILITY_KIND:
        material["capability_kind"] = capability_kind
    suffix = canonical_sha256(material)[:16]
    role_suffix = role_id.removeprefix("evaluation.")
    kind_slug = "native_so" if capability_kind == _CAPABILITY_KIND else "json_object"
    raw = f"evaluation.{role_suffix}.{kind_slug}.{suffix}".casefold()
    return _ID_SAFE_RE.sub("-", raw)


def _reject_forbidden_authority(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_forbidden_authority(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_forbidden_authority(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    normalized = _AUTHORITY_SEPARATOR_RE.sub("_", value.casefold()).strip("_")
    for marker in _FORBIDDEN_AUTHORITY_MARKERS:
        marker_pattern = _AUTHORITY_SEPARATOR_RE.sub(
            "_", marker.casefold()
        ).strip("_")
        if re.search(
            rf"(?:^|_){re.escape(marker_pattern)}(?:_|$)", normalized
        ):
            raise ContractValidationError(
                "forbidden_authority",
                path,
                f"Evaluation capability identity exposes forbidden authority marker {marker!r}",
            )


def _git_text(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()
