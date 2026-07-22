from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_number,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    validate_producer,
)
from pipeline.eval.live_pilot_preflight_v1 import (
    validate_evaluation_live_pilot_preflight_binding,
)
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
    evaluation_role_budget_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.offline_orchestrator_v1 import validate_evaluation_run_config
from pipeline.llm_backend import (
    ContractValidationError as SharedContractValidationError,
    canonical_sha256 as shared_canonical_sha256,
    validate_api_source,
    validate_capability_evidence,
    validate_pipeline_profile,
)


__all__ = [
    "EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_ID",
    "EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_VERSION",
    "EvaluationLivePilotProfileBundleV1",
    "build_evaluation_live_pilot_profile_v1",
    "seal_evaluation_live_pilot_profile_v1",
    "validate_evaluation_live_pilot_profile_binding_v1",
    "validate_evaluation_live_pilot_profile_v1",
]


EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_ID = "EvaluationLivePilotProfileV1"
EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_VERSION = "1.0.0"
_ZERO_SHA256 = "0" * 64
_CACHE_MODES = frozenset({"bypass", "read_only", "read_write"})
_ROLE_TO_PREFLIGHT_COUNT = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: "sf_bt_back_translation",
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: "sf_bt_semantic_judge",
    PJ_JUDGE_ROLE_ID: "pj_judge",
}


@dataclass(frozen=True, slots=True)
class EvaluationLivePilotProfileBundleV1:
    artifact: dict[str, Any]
    profile: dict[str, Any]
    api_source: dict[str, Any]
    capabilities: tuple[dict[str, Any], ...]


def build_evaluation_live_pilot_profile_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    api_source: Mapping[str, Any],
    capabilities_by_role: Mapping[str, Mapping[str, Any]],
    *,
    created_at: str,
    producer_code_commit: str,
    profile_id: str,
    profile_revision: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    output_root_relative: str,
    cache_mode: str,
    structured_output_mode: str = "preferred",
) -> EvaluationLivePilotProfileBundleV1:
    config = validate_evaluation_run_config(config_payload)
    preflight = validate_evaluation_live_pilot_preflight_binding(
        preflight_payload, common_input, config
    )
    source = _validate_shared_source(api_source)
    capabilities = _validate_role_capabilities(
        capabilities_by_role,
        source,
        structured_output_mode=structured_output_mode,
    )
    targets = {
        role_id: _target_record(source, capabilities[role_id])
        for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
    }
    try:
        profile = build_evaluation_llm_profile_v1(
            primary_targets=targets,
            profile_id=require_string(profile_id, path="$.profile_id"),
            profile_revision=require_string(
                profile_revision, path="$.profile_revision"
            ),
            structured_output_mode=structured_output_mode,
        )
    except SharedContractValidationError as exc:
        raise ContractValidationError(
            "shared_profile",
            "$.profile",
            f"shared profile validation failed: {exc}",
        ) from exc
    workload = _derive_workload(
        preflight,
        capabilities,
        structured_output_mode=structured_output_mode,
    )
    artifact = seal_evaluation_live_pilot_profile_v1(
        {
            "schema_id": EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_ID,
            "schema_version": EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_VERSION,
            "created_at": require_rfc3339(created_at, path="$.created_at"),
            "producer": {
                "workstream": "evaluation",
                "component": "live_pilot_profile_v1",
                "component_version": "1.0.0",
                "code_commit": require_commit(
                    producer_code_commit, path="$.producer_code_commit"
                ),
            },
            "binding": {
                "project_id": preflight["binding"]["project_id"],
                "document_id": preflight["binding"]["document_id"],
                "config_id": preflight["binding"]["config_id"],
                "config_sha256": preflight["binding"]["config_sha256"],
                "input_set_sha256": preflight["binding"]["input_set_sha256"],
                "plan_id": preflight["binding"]["plan_id"],
                "plan_sha256": preflight["binding"]["plan_sha256"],
                "preflight_sha256": preflight["integrity"]["preflight_sha256"],
                "evaluation_logical_run_id": require_string(
                    evaluation_logical_run_id,
                    path="$.evaluation_logical_run_id",
                ),
                "evaluation_attempt_run_id": require_string(
                    evaluation_attempt_run_id,
                    path="$.evaluation_attempt_run_id",
                ),
                "profile_id": profile["profile_id"],
                "profile_revision": profile["profile_revision"],
                "profile_sha256": shared_canonical_sha256(profile),
                "source_record_sha256": shared_canonical_sha256(source),
                "physical_quota_bucket_id": source[
                    "physical_quota_bucket_id"
                ],
                "output_root_relative": require_relative_path(
                    output_root_relative, path="$.output_root_relative"
                ),
                "cache_mode": require_enum(
                    cache_mode, _CACHE_MODES, path="$.cache_mode"
                ),
            },
            "api_source": source,
            "role_capabilities": [
                {
                    "role_id": role_id,
                    "capability": capabilities[role_id],
                }
                for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
            ],
            "profile": profile,
            "workload": workload,
            "integrity": {"artifact_sha256": _ZERO_SHA256},
        }
    )
    validated = validate_evaluation_live_pilot_profile_binding_v1(
        artifact,
        common_input,
        config,
        preflight,
        expected_api_source=source,
        expected_capabilities_by_role=capabilities,
        expected_profile_id=profile["profile_id"],
        expected_profile_revision=profile["profile_revision"],
        evaluation_logical_run_id=artifact["binding"][
            "evaluation_logical_run_id"
        ],
        evaluation_attempt_run_id=artifact["binding"][
            "evaluation_attempt_run_id"
        ],
        output_root_relative=artifact["binding"]["output_root_relative"],
        cache_mode=artifact["binding"]["cache_mode"],
        expected_structured_output_mode=structured_output_mode,
    )
    return EvaluationLivePilotProfileBundleV1(
        artifact=validated,
        profile=copy.deepcopy(profile),
        api_source=copy.deepcopy(source),
        capabilities=tuple(
            copy.deepcopy(capabilities[role_id])
            for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
        ),
    )


def seal_evaluation_live_pilot_profile_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    row = copy.deepcopy(dict(payload))
    integrity = row.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ContractValidationError("type", "$.integrity", "expected an object")
    row["integrity"] = {"artifact_sha256": _ZERO_SHA256}
    row["integrity"]["artifact_sha256"] = shared_canonical_sha256(row)
    return validate_evaluation_live_pilot_profile_v1(row)


def validate_evaluation_live_pilot_profile_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "binding",
            "api_source",
            "role_capabilities",
            "profile",
            "workload",
            "integrity",
        },
        path="$",
    )
    source = _validate_shared_source(root["api_source"])
    profile = _validate_shared_profile(root["profile"])
    structured_output_mode = _profile_structured_output_mode(profile)
    role_capabilities = _validate_role_capability_rows(
        root["role_capabilities"],
        source,
        structured_output_mode=structured_output_mode,
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"],
            {EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_ID},
            path="$.schema_id",
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {EVALUATION_LIVE_PILOT_PROFILE_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "api_source": source,
        "role_capabilities": [
            {
                "role_id": role_id,
                "capability": role_capabilities[role_id],
            }
            for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
        ],
        "profile": profile,
        "workload": _validate_workload(root["workload"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _require_internal_binding(normalized, role_capabilities)
    expected_hash_payload = copy.deepcopy(normalized)
    expected_hash_payload["integrity"]["artifact_sha256"] = _ZERO_SHA256
    if (
        shared_canonical_sha256(expected_hash_payload)
        != normalized["integrity"]["artifact_sha256"]
    ):
        raise ContractValidationError(
            "artifact_hash",
            "$.integrity.artifact_sha256",
            "live pilot profile hash differs from canonical content",
        )
    _assert_no_eval_authority(normalized)
    return copy.deepcopy(normalized)


def validate_evaluation_live_pilot_profile_binding_v1(
    payload: Mapping[str, Any],
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    *,
    expected_api_source: Mapping[str, Any],
    expected_capabilities_by_role: Mapping[str, Mapping[str, Any]],
    expected_profile_id: str,
    expected_profile_revision: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    output_root_relative: str,
    cache_mode: str,
    expected_structured_output_mode: str = "preferred",
) -> dict[str, Any]:
    validated = validate_evaluation_live_pilot_profile_v1(payload)
    config = validate_evaluation_run_config(config_payload)
    preflight = validate_evaluation_live_pilot_preflight_binding(
        preflight_payload, common_input, config
    )
    source = _validate_shared_source(expected_api_source)
    capabilities = _validate_role_capabilities(
        expected_capabilities_by_role,
        source,
        structured_output_mode=expected_structured_output_mode,
    )
    expected_binding = {
        "project_id": preflight["binding"]["project_id"],
        "document_id": preflight["binding"]["document_id"],
        "config_id": preflight["binding"]["config_id"],
        "config_sha256": preflight["binding"]["config_sha256"],
        "input_set_sha256": preflight["binding"]["input_set_sha256"],
        "plan_id": preflight["binding"]["plan_id"],
        "plan_sha256": preflight["binding"]["plan_sha256"],
        "preflight_sha256": preflight["integrity"]["preflight_sha256"],
        "evaluation_logical_run_id": require_string(
            evaluation_logical_run_id, path="$.evaluation_logical_run_id"
        ),
        "evaluation_attempt_run_id": require_string(
            evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
        ),
        "profile_id": require_string(
            expected_profile_id, path="$.expected_profile_id"
        ),
        "profile_revision": require_string(
            expected_profile_revision, path="$.expected_profile_revision"
        ),
        "profile_sha256": validated["binding"]["profile_sha256"],
        "source_record_sha256": shared_canonical_sha256(source),
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "output_root_relative": require_relative_path(
            output_root_relative, path="$.output_root_relative"
        ),
        "cache_mode": require_enum(cache_mode, _CACHE_MODES, path="$.cache_mode"),
    }
    if validated["binding"] != expected_binding:
        raise ContractValidationError(
            "live_profile_binding",
            "$.binding",
            "profile artifact references another run, source, profile or output root",
        )
    if validated["api_source"] != source:
        raise ContractValidationError(
            "source_substitution", "$.api_source", "API source differs from expectation"
        )
    observed_capabilities = {
        row["role_id"]: row["capability"]
        for row in validated["role_capabilities"]
    }
    if observed_capabilities != capabilities:
        raise ContractValidationError(
            "capability_substitution",
            "$.role_capabilities",
            "capability evidence differs from expectation",
        )
    expected_targets = {
        role_id: _target_record(source, capabilities[role_id])
        for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
    }
    expected_profile = build_evaluation_llm_profile_v1(
        primary_targets=expected_targets,
        profile_id=expected_binding["profile_id"],
        profile_revision=expected_binding["profile_revision"],
        structured_output_mode=expected_structured_output_mode,
    )
    expected_binding["profile_sha256"] = shared_canonical_sha256(expected_profile)
    if validated["binding"] != expected_binding or validated["profile"] != expected_profile:
        raise ContractValidationError(
            "profile_substitution",
            "$.profile",
            "profile differs from exact expected role targets",
        )
    if validated["workload"] != _derive_workload(
        preflight,
        capabilities,
        structured_output_mode=expected_structured_output_mode,
    ):
        raise ContractValidationError(
            "workload_substitution",
            "$.workload",
            "call and token envelope differs from the sealed preflight",
        )
    return validated


def _validate_shared_source(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        source = validate_api_source(value)
    except SharedContractValidationError as exc:
        raise ContractValidationError(
            "shared_source",
            "$.api_source",
            f"shared API source is invalid: {exc}",
        ) from exc
    if not source["enabled"]:
        raise ContractValidationError(
            "source_disabled", "$.api_source.enabled", "API source is disabled"
        )
    return source


def _validate_role_capabilities(
    value: Mapping[str, Mapping[str, Any]],
    source: Mapping[str, Any],
    *,
    structured_output_mode: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "role_capability_cover",
            "$.capabilities_by_role",
            "exactly one capability is required for every Evaluation live role",
        )
    result: dict[str, dict[str, Any]] = {}
    for role_id in sorted(EVALUATION_LLM_ROLE_IDS):
        result[role_id] = _validate_role_capability(
            role_id,
            value[role_id],
            source,
            structured_output_mode=structured_output_mode,
        )
    return result


def _validate_role_capability_rows(
    value: Any,
    source: Mapping[str, Any],
    *,
    structured_output_mode: str,
) -> dict[str, dict[str, Any]]:
    rows = require_list(value, path="$.role_capabilities")
    result: dict[str, dict[str, Any]] = {}
    observed_order: list[str] = []
    for index, raw in enumerate(rows):
        path = f"$.role_capabilities[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"role_id", "capability"}, path=path)
        role_id = require_enum(
            row["role_id"], EVALUATION_LLM_ROLE_IDS, path=f"{path}.role_id"
        )
        observed_order.append(role_id)
        result[role_id] = _validate_role_capability(
            role_id,
            row["capability"],
            source,
            structured_output_mode=structured_output_mode,
        )
    require_unique(observed_order, path="$.role_capabilities.role_id")
    if observed_order != sorted(EVALUATION_LLM_ROLE_IDS):
        raise ContractValidationError(
            "role_capability_order",
            "$.role_capabilities",
            "role capabilities must use canonical role order",
        )
    return result


def _validate_role_capability(
    role_id: str,
    value: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    structured_output_mode: str,
) -> dict[str, Any]:
    try:
        capability = validate_capability_evidence(value)
    except SharedContractValidationError as exc:
        raise ContractValidationError(
            "shared_capability",
            f"$.role_capabilities.{role_id}",
            f"shared capability evidence is invalid: {exc}",
        ) from exc
    contract = evaluation_role_contract_v1(role_id)
    expected_source_fields = {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
    }
    if any(capability[field] != expected for field, expected in expected_source_fields.items()):
        raise ContractValidationError(
            "capability_source_binding",
            f"$.role_capabilities.{role_id}",
            "capability evidence belongs to another source or route",
        )
    if capability["verdict"] != "qualified":
        raise ContractValidationError(
            "capability_unqualified",
            f"$.role_capabilities.{role_id}.verdict",
            "live role requires qualified capability evidence",
        )
    expected_capability_kind = {
        "required": "native_structured_output",
        "preferred": "native_structured_output",
        "prompt_validated": "json_object",
        "disabled": "text_generation",
    }.get(structured_output_mode)
    if expected_capability_kind is None:
        raise ContractValidationError(
            "structured_output_mode",
            f"$.role_capabilities.{role_id}",
            "unsupported structured-output mode",
        )
    if capability["capability_kind"] != expected_capability_kind:
        raise ContractValidationError(
            "capability_kind",
            f"$.role_capabilities.{role_id}.capability_kind",
            "live role capability kind differs from the sealed output mode",
        )
    expected_contract = {
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": contract["response_schema"]["sha256"],
        "local_validator_id": contract["validator"]["id"],
        "local_validator_sha256": contract["validator"]["sha256"],
    }
    if any(capability[field] != expected for field, expected in expected_contract.items()):
        raise ContractValidationError(
            "capability_contract_binding",
            f"$.role_capabilities.{role_id}",
            "capability evidence does not qualify this exact role contract",
        )
    return capability


def _target_record(
    source: Mapping[str, Any], capability: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "source_record_sha256": shared_canonical_sha256(source),
        "requested_model_id": capability["requested_model_id"],
        "capability_id": capability["capability_id"],
        "capability_revision": capability["capability_revision"],
        "capability_record_sha256": shared_canonical_sha256(capability),
    }


def _derive_workload(
    preflight: Mapping[str, Any],
    capabilities: Mapping[str, Mapping[str, Any]],
    *,
    structured_output_mode: str,
) -> dict[str, Any]:
    physical = preflight["workload"]["physical_call_counts"]
    role_rows: list[dict[str, Any]] = []
    model_totals: dict[str, dict[str, int]] = {}
    for role_id in sorted(EVALUATION_LLM_ROLE_IDS):
        call_count = int(physical[_ROLE_TO_PREFLIGHT_COUNT[role_id]])
        budget = evaluation_role_budget_v1(
            role_id,
            structured_output_mode=structured_output_mode,
        )["limits"]
        model_id = capabilities[role_id]["requested_model_id"]
        row = {
            "role_id": role_id,
            "requested_model_id": model_id,
            "call_count": call_count,
            "reserved_max_prompt_tokens": call_count
            * budget["max_prompt_tokens"],
            "reserved_max_completion_tokens": call_count
            * budget["max_completion_tokens"],
            "reserved_max_total_tokens": call_count * budget["max_total_tokens"],
        }
        role_rows.append(row)
        aggregate = model_totals.setdefault(
            model_id,
            {
                "call_count": 0,
                "reserved_max_prompt_tokens": 0,
                "reserved_max_completion_tokens": 0,
                "reserved_max_total_tokens": 0,
            },
        )
        for field in aggregate:
            aggregate[field] += row[field]
    model_rows = [
        {"requested_model_id": model_id, **model_totals[model_id]}
        for model_id in sorted(model_totals)
    ]
    reserved_prompt_tokens = sum(
        row["reserved_max_prompt_tokens"] for row in role_rows
    )
    reserved_completion_tokens = sum(
        row["reserved_max_completion_tokens"] for row in role_rows
    )
    reserved_total_tokens = sum(
        row["reserved_max_total_tokens"] for row in role_rows
    )
    token_envelope = preflight["workload"]["token_envelope"]
    if (
        reserved_prompt_tokens
        < int(token_envelope["reserved_max_prompt_tokens"])
        or reserved_completion_tokens
        < int(token_envelope["reserved_max_completion_tokens"])
        or reserved_total_tokens
        < int(token_envelope["reserved_max_total_tokens"])
    ):
        raise ContractValidationError(
            "token_envelope",
            "$.workload",
            "profile usage reservation cannot be smaller than preflight generation reservation",
        )
    workload = {
        "sf_qe_local_row_count": int(physical["sf_qe_local_rows"]),
        "scorer_api_call_count": int(physical["total_api_calls"]),
        "qualification_probe_call_cap": int(
            physical["qualification_probe_call_cap"]
        ),
        "role_reservations": role_rows,
        "model_reservations": model_rows,
        "reserved_max_prompt_tokens": reserved_prompt_tokens,
        "reserved_max_completion_tokens": reserved_completion_tokens,
        "reserved_max_total_tokens": reserved_total_tokens,
        "cost_cap_usd": token_envelope["cost_cap_usd"],
    }
    if sum(row["call_count"] for row in role_rows) != workload[
        "scorer_api_call_count"
    ]:
        raise ContractValidationError(
            "call_envelope", "$.workload", "role calls do not sum to scorer calls"
        )
    for field in (
        "reserved_max_prompt_tokens",
        "reserved_max_completion_tokens",
        "reserved_max_total_tokens",
    ):
        if sum(row[field] for row in role_rows) != workload[field]:
            raise ContractValidationError(
                "token_envelope",
                f"$.workload.{field}",
                "role reservations do not sum to preflight reservation",
            )
    return workload


def _profile_structured_output_mode(profile: Mapping[str, Any]) -> str:
    modes = {
        role["structured_output"]["mode"]
        for role in profile["role_bindings"]
    }
    if len(modes) != 1:
        raise ContractValidationError(
            "structured_output_mode",
            "$.profile.role_bindings",
            "all Evaluation live roles must use one sealed output mode",
        )
    return next(iter(modes))


def _validate_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    required = {
        "project_id",
        "document_id",
        "config_id",
        "config_sha256",
        "input_set_sha256",
        "plan_id",
        "plan_sha256",
        "preflight_sha256",
        "evaluation_logical_run_id",
        "evaluation_attempt_run_id",
        "profile_id",
        "profile_revision",
        "profile_sha256",
        "source_record_sha256",
        "physical_quota_bucket_id",
        "output_root_relative",
        "cache_mode",
    }
    require_exact_keys(row, required=required, path=path)
    result = {
        field: require_string(row[field], path=f"{path}.{field}")
        for field in required
        if field
        not in {
            "config_sha256",
            "input_set_sha256",
            "plan_sha256",
            "preflight_sha256",
            "profile_sha256",
            "source_record_sha256",
            "output_root_relative",
            "cache_mode",
        }
    }
    for field in (
        "config_sha256",
        "input_set_sha256",
        "plan_sha256",
        "preflight_sha256",
        "profile_sha256",
        "source_record_sha256",
    ):
        result[field] = require_sha256(row[field], path=f"{path}.{field}")
    result["output_root_relative"] = require_relative_path(
        row["output_root_relative"], path=f"{path}.output_root_relative"
    )
    result["cache_mode"] = require_enum(
        row["cache_mode"], _CACHE_MODES, path=f"{path}.cache_mode"
    )
    return result


def _validate_workload(value: Any) -> dict[str, Any]:
    path = "$.workload"
    row = require_mapping(value, path=path)
    required = {
        "sf_qe_local_row_count",
        "scorer_api_call_count",
        "qualification_probe_call_cap",
        "role_reservations",
        "model_reservations",
        "reserved_max_prompt_tokens",
        "reserved_max_completion_tokens",
        "reserved_max_total_tokens",
        "cost_cap_usd",
    }
    require_exact_keys(row, required=required, path=path)
    roles = _validate_reservations(
        row["role_reservations"], path=f"{path}.role_reservations", role_rows=True
    )
    models = _validate_reservations(
        row["model_reservations"], path=f"{path}.model_reservations", role_rows=False
    )
    result = {
        "sf_qe_local_row_count": require_int(
            row["sf_qe_local_row_count"],
            path=f"{path}.sf_qe_local_row_count",
            minimum=1,
        ),
        "scorer_api_call_count": require_int(
            row["scorer_api_call_count"],
            path=f"{path}.scorer_api_call_count",
            minimum=1,
        ),
        "qualification_probe_call_cap": require_int(
            row["qualification_probe_call_cap"],
            path=f"{path}.qualification_probe_call_cap",
            minimum=0,
        ),
        "role_reservations": roles,
        "model_reservations": models,
        "reserved_max_prompt_tokens": require_int(
            row["reserved_max_prompt_tokens"],
            path=f"{path}.reserved_max_prompt_tokens",
            minimum=1,
        ),
        "reserved_max_completion_tokens": require_int(
            row["reserved_max_completion_tokens"],
            path=f"{path}.reserved_max_completion_tokens",
            minimum=1,
        ),
        "reserved_max_total_tokens": require_int(
            row["reserved_max_total_tokens"],
            path=f"{path}.reserved_max_total_tokens",
            minimum=1,
        ),
        "cost_cap_usd": require_nullable_number(
            row["cost_cap_usd"], path=f"{path}.cost_cap_usd", minimum=0
        ),
    }
    if sum(item["call_count"] for item in roles) != result["scorer_api_call_count"]:
        raise ContractValidationError(
            "call_envelope", path, "role call count differs from scorer call count"
        )
    if sum(item["call_count"] for item in models) != result["scorer_api_call_count"]:
        raise ContractValidationError(
            "model_call_envelope", path, "model call count differs from scorer calls"
        )
    for field in (
        "reserved_max_prompt_tokens",
        "reserved_max_completion_tokens",
        "reserved_max_total_tokens",
    ):
        if sum(item[field] for item in roles) != result[field]:
            raise ContractValidationError(
                "role_token_envelope",
                f"{path}.{field}",
                "role token reservations differ from aggregate reservation",
            )
        if sum(item[field] for item in models) != result[field]:
            raise ContractValidationError(
                "model_token_envelope",
                f"{path}.{field}",
                "model token reservations differ from aggregate reservation",
            )
    expected_models: dict[str, dict[str, int]] = {}
    for role in roles:
        aggregate = expected_models.setdefault(
            role["requested_model_id"],
            {
                "call_count": 0,
                "reserved_max_prompt_tokens": 0,
                "reserved_max_completion_tokens": 0,
                "reserved_max_total_tokens": 0,
            },
        )
        for field in aggregate:
            aggregate[field] += role[field]
    expected_model_rows = [
        {"requested_model_id": model_id, **expected_models[model_id]}
        for model_id in sorted(expected_models)
    ]
    if models != expected_model_rows:
        raise ContractValidationError(
            "model_reservation_projection",
            f"{path}.model_reservations",
            "model reservations are not the exact projection of role reservations",
        )
    return result


def _validate_reservations(
    value: Any, *, path: str, role_rows: bool
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    identity_field = "role_id" if role_rows else "requested_model_id"
    expected_order = sorted(EVALUATION_LLM_ROLE_IDS) if role_rows else None
    for index, raw in enumerate(require_list(value, path=path)):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        required = {
            identity_field,
            "requested_model_id",
            "call_count",
            "reserved_max_prompt_tokens",
            "reserved_max_completion_tokens",
            "reserved_max_total_tokens",
        }
        require_exact_keys(row, required=required, path=item_path)
        normalized = {
            identity_field: (
                require_enum(
                    row[identity_field],
                    EVALUATION_LLM_ROLE_IDS,
                    path=f"{item_path}.{identity_field}",
                )
                if role_rows
                else require_string(
                    row[identity_field], path=f"{item_path}.{identity_field}"
                )
            ),
            "requested_model_id": require_string(
                row["requested_model_id"],
                path=f"{item_path}.requested_model_id",
            ),
        }
        for field in (
            "call_count",
            "reserved_max_prompt_tokens",
            "reserved_max_completion_tokens",
            "reserved_max_total_tokens",
        ):
            normalized[field] = require_int(
                row[field], path=f"{item_path}.{field}", minimum=0
            )
        result.append(normalized)
    identities = [row[identity_field] for row in result]
    require_unique(identities, path=f"{path}.{identity_field}")
    required_order = expected_order if role_rows else sorted(identities)
    if identities != required_order:
        raise ContractValidationError(
            "reservation_order", path, "reservations are not in canonical order"
        )
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$.integrity")
    require_exact_keys(row, required={"artifact_sha256"}, path="$.integrity")
    return {
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path="$.integrity.artifact_sha256"
        )
    }


def _validate_shared_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return validate_pipeline_profile(value)
    except SharedContractValidationError as exc:
        raise ContractValidationError(
            "shared_profile",
            "$.profile",
            f"shared pipeline profile is invalid: {exc}",
        ) from exc


def _require_internal_binding(
    payload: Mapping[str, Any], capabilities: Mapping[str, Mapping[str, Any]]
) -> None:
    binding = payload["binding"]
    source = payload["api_source"]
    profile = payload["profile"]
    if binding["source_record_sha256"] != shared_canonical_sha256(source):
        raise ContractValidationError(
            "source_record_hash",
            "$.binding.source_record_sha256",
            "source record hash differs from embedded source",
        )
    if binding["physical_quota_bucket_id"] != source["physical_quota_bucket_id"]:
        raise ContractValidationError(
            "physical_bucket",
            "$.binding.physical_quota_bucket_id",
            "physical bucket differs from embedded source",
        )
    if (
        binding["profile_id"] != profile["profile_id"]
        or binding["profile_revision"] != profile["profile_revision"]
        or binding["profile_sha256"] != shared_canonical_sha256(profile)
    ):
        raise ContractValidationError(
            "profile_hash", "$.binding", "profile identity or hash differs"
        )
    expected_targets = {
        role_id: _target_record(source, capabilities[role_id])
        for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
    }
    observed_roles = {
        role["role_id"]: role for role in profile["role_bindings"]
    }
    if set(observed_roles) != EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "profile_role_cover", "$.profile.role_bindings", "live roles are incomplete"
        )
    for role_id, target in expected_targets.items():
        role = observed_roles[role_id]
        if role["primary"] != target or role["fallback_plan"] != {
            "enabled": False,
            "steps": [],
        }:
            raise ContractValidationError(
                "profile_target",
                f"$.profile.role_bindings.{role_id}",
                "profile target or fallback differs from capability evidence",
            )
    workload_roles = {
        row["role_id"]: row for row in payload["workload"]["role_reservations"]
    }
    if set(workload_roles) != EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "workload_role_cover",
            "$.workload.role_reservations",
            "workload does not cover every live role",
        )
    for role_id, capability in capabilities.items():
        if (
            workload_roles[role_id]["requested_model_id"]
            != capability["requested_model_id"]
        ):
            raise ContractValidationError(
                "workload_model_binding",
                f"$.workload.role_reservations.{role_id}",
                "workload model differs from capability evidence",
            )


def _assert_no_eval_authority(value: Any, *, path: str = "$") -> None:
    forbidden = {
        "gold",
        "oracle",
        "human_reference",
        "reference_translation",
        "result_callback",
        "expected_winner",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if any(token in normalized_key for token in forbidden):
                raise ContractValidationError(
                    "forbidden_evaluation_authority",
                    f"{path}.{key}",
                    "evaluation-only answer authority is forbidden from live profiles",
                )
            _assert_no_eval_authority(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_eval_authority(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        normalized_value = value.casefold().replace("-", "_")
        if any(token in normalized_value for token in forbidden):
            raise ContractValidationError(
                "forbidden_evaluation_authority",
                path,
                "evaluation-only answer authority is forbidden from live profiles",
            )
