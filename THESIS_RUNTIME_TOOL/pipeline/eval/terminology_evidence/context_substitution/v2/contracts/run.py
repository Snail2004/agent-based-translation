from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_number,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.aggregation import (
    aggregate_contextual_evidence,
    compute_context_result,
    global_recommendation,
    merge_judge_labels,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.application import (
    build_application_contract,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    AGGREGATION_VERSION,
    APPLICATION_CONTRACT_VERSION,
    CONTEXT_DEDUP_POLICY_VERSION,
    CONTEXT_FLAGS,
    CONTEXT_LABELS,
    CONTEXT_TYPES,
    CONTEXTUAL_STATUSES,
    CONTRASTIVE_RESULTS,
    CONTRASTIVE_JUDGE_VERSION,
    GLOBAL_RECOMMENDATIONS,
    HASH_PATH,
    JUDGE_VERSION,
    LOCAL_HARD_FLAGS,
    OOD_POLICY_VERSION,
    PROVENANCE_VERSION,
    PROVIDER_ROLES,
    PROVIDER_ROUTE_IDS,
    REQUIRED_SAME_SENSE_CONTEXT_TYPES,
    RUBRIC_VERSION,
    RUN_POLICY,
    SCHEMA_ID,
    SCHEMA_VERSION,
    SELECTOR_VERSION,
    SENSE_DEFINITION_STATUSES,
    SUPPORT_SET_VERSION,
    TRIAL_QUALITY_GATE_VERSION,
    TRIAL_STATUSES,
    TRIAL_TRANSLATOR_VERSION,
    VARIANT_STATUSES,
    require_bool,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.pairwise import (
    PAIRWISE_VERSION,
    close_candidate_pairs,
    validate_pairwise_record,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.surface import (
    trial_surface_binding,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.responses import (
    validate_context_judge,
    validate_contrastive,
    validate_selector_annotation,
    validate_trial,
    validate_trial_gate,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.provenance import (
    validate_source_artifact_bindings,
    validate_source_provenance,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.evidence.support_set import (
    build_certificate_support_set,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.evidence.provenance import (
    build_candidate_provenance,
    candidate_provider_provenances,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration import (
    ContextThresholdPolicy,
    validate_evaluation_mode,
    validate_threshold_policy,
)


_TARGET_ROLES = frozenset(
    {"canonical", "alternative", "rejected", "pending"}
)
_PAIRWISE_FLAG = "PAIRWISE_TIEBREAKER_UNAVAILABLE"


def seal_context_substitution_run(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    sealed = seal_payload(payload, policy=RUN_POLICY, hash_path=HASH_PATH)
    return validate_context_substitution_run(sealed)


def validate_context_substitution_run(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "input_sha256",
            "input_source_artifacts",
            "execution_policy",
            "provider_attempts",
            "usage",
            "candidates",
            "pairwise_observations",
            "integrity",
        },
        path="$",
    )
    execution_policy = _validate_execution_policy(
        root["execution_policy"], path="$.execution_policy"
    )
    attempts = [
        _validate_provider_attempt(
            row, path=f"$.provider_attempts[{index}]"
        )
        for index, row in enumerate(
            require_list(
                root["provider_attempts"], path="$.provider_attempts"
            )
        )
    ]
    if execution_policy["evaluation_mode"] == "FROZEN_TEST_SET" and any(
        attempt["response_sha256"] is not None
        and attempt["raw_response_storage_status"] != "STORED"
        for attempt in attempts
    ):
        raise ContractValidationError(
            "raw_response_ledger",
            "$.provider_attempts",
            "frozen execution requires every provider response to be stored",
        )
    usage = _validate_usage(
        root["usage"],
        attempts=attempts,
        route_order=execution_policy["provider_route_order"],
        path="$.usage",
    )
    pairwise = [
        validate_pairwise_record(
            row,
            path=f"$.pairwise_observations[{index}]",
            close_margin=execution_policy["threshold_policy"][
                "pairwise_close_margin"
            ],
        )
        for index, row in enumerate(
            require_list(
                root["pairwise_observations"],
                path="$.pairwise_observations",
            )
        )
    ]
    candidates = [
        _validate_candidate(
            row,
            pairwise_records=pairwise,
            threshold_policy=validate_threshold_policy(
                execution_policy["threshold_policy"]
            ),
            selector_mode=execution_policy["selector_mode"],
            path=f"$.candidates[{index}]",
        )
        for index, row in enumerate(
            require_list(root["candidates"], path="$.candidates")
        )
    ]
    if not candidates:
        raise ContractValidationError(
            "missing_candidate", "$.candidates", "must not be empty"
        )
    candidate_keys = [
        f"{row['term_id']}\0{row['candidate_id']}" for row in candidates
    ]
    require_unique(candidate_keys, path="$.candidates[*]")
    if candidates != sorted(
        candidates, key=lambda row: (row["term_id"], row["candidate_id"])
    ):
        raise ContractValidationError(
            "candidate_order",
            "$.candidates",
            "candidates must be sorted by term_id and candidate_id",
        )
    _validate_pairwise_bindings(
        candidates=candidates,
        records=pairwise,
        close_margin=execution_policy["threshold_policy"][
            "pairwise_close_margin"
        ],
    )
    _validate_pairwise_flags(candidates=candidates, records=pairwise)
    _validate_nested_provider_bindings(
        attempts=attempts,
        candidates=candidates,
        pairwise_records=pairwise,
    )
    integrity_row = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(
        integrity_row, required={"run_sha256"}, path="$.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "input_sha256": require_sha256(
            root["input_sha256"], path="$.input_sha256"
        ),
        "input_source_artifacts": validate_source_artifact_bindings(
            root["input_source_artifacts"],
            path="$.input_source_artifacts",
        ),
        "execution_policy": execution_policy,
        "provider_attempts": attempts,
        "usage": usage,
        "candidates": candidates,
        "pairwise_observations": pairwise,
        "integrity": {
            "run_sha256": require_sha256(
                integrity_row["run_sha256"],
                path="$.integrity.run_sha256",
            )
        },
    }
    if not verify_payload_hash(
        normalized, policy=RUN_POLICY, hash_path=HASH_PATH
    ):
        raise ContractValidationError(
            "self_hash",
            "$.integrity.run_sha256",
            "context substitution run self-hash mismatch",
        )
    return canonicalize(normalized, policy=RUN_POLICY)


def context_substitution_to_measurements(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    run = validate_context_substitution_run(payload)
    from pipeline.eval.terminology_evidence.legacy_term_evidence.v1 import (
        MEASUREMENTS_SCHEMA_ID,
        SCHEMA_VERSION as TERM_EVIDENCE_SCHEMA_VERSION,
        seal_d2l_term_evidence_measurements,
    )

    measurements = []
    for candidate in run["candidates"]:
        if any(
            row["secondary_judge"] is not None
            for row in candidate["context_results"]
        ):
            raise ContractValidationError(
                "lossy_legacy_projection",
                "$.candidates",
                "V1 measurements cannot preserve merged primary/secondary judge provenance",
            )
        flags = set(candidate["context_flags"])
        measurements.append(
            {
                "term_id": candidate["term_id"],
                "candidate_target_id": candidate["candidate_id"],
                "context_results": [
                    {
                        "block_id": row["context_id"],
                        "label": row["label"],
                        "raw_score": row["raw_score"],
                        "test_translation_vi": row["trial_translation"],
                        "judge_reason": row["reason"],
                        "provenance": {
                            key: row["primary_judge"]["provenance"][key]
                            for key in (
                                "model_id",
                                "prompt_version",
                                "prompt_sha256",
                                "response_sha256",
                            )
                        },
                    }
                    for row in candidate["context_results"]
                ],
                "web_evidence": [],
                "back_translation": None,
                "wrong_concept": bool(
                    flags & LOCAL_HARD_FLAGS
                ),
                "split_required": any(
                    row["result"] == "SEPARATE_SENSE_REQUIRED"
                    for row in candidate["contrastive_results"]
                ),
                "judge_disagreement": candidate["judge_disagreement"],
            }
        )
    return seal_d2l_term_evidence_measurements(
        {
            "schema_id": MEASUREMENTS_SCHEMA_ID,
            "schema_version": TERM_EVIDENCE_SCHEMA_VERSION,
            "input_sha256": run["input_sha256"],
            "measurements": measurements,
            "integrity": {"measurements_sha256": "0" * 64},
        }
    )


def _validate_execution_policy(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "provider_route_order",
        "selector_version",
        "selector_candidate_independent",
        "context_dedup_policy_version",
        "trial_translator_version",
        "trial_quality_gate_version",
        "judge_version",
        "contrastive_judge_version",
        "rubric_version",
        "aggregation_version",
        "evaluation_mode",
        "threshold_policy",
        "application_contract_version",
        "support_set_version",
        "ood_policy_version",
        "same_sense_target_count",
        "same_sense_minimum_count",
        "contrastive_target_count",
        "contrastive_minimum_count",
        "trial_retry_limit",
        "similarity_threshold",
        "second_judge_policy",
        "pairwise_tiebreaker_version",
        "pairwise_policy",
        "provider_failover_policy",
        "final_decision_owner",
        "selector_mode",
        "selector_authority_status",
        "dataset_manifest_sha256",
        "parent_dataset_manifest_sha256",
        "review_artifact_ref",
        "review_artifact_sha256",
        "effective_sense_contract_ref",
        "effective_sense_contract_sha256",
        "raw_response_ledger_policy",
    }
    require_exact_keys(row, required=required, path=path)
    route_order = [
        require_enum(
            child,
            PROVIDER_ROUTE_IDS,
            path=f"{path}.provider_route_order[{index}]",
        )
        for index, child in enumerate(
            require_list(
                row["provider_route_order"],
                path=f"{path}.provider_route_order",
            )
        )
    ]
    if not route_order:
        raise ContractValidationError(
            "provider_routes",
            f"{path}.provider_route_order",
            "at least one route is required",
        )
    require_unique(route_order, path=f"{path}.provider_route_order")
    expected_strings = {
        "selector_version": SELECTOR_VERSION,
        "context_dedup_policy_version": CONTEXT_DEDUP_POLICY_VERSION,
        "trial_translator_version": TRIAL_TRANSLATOR_VERSION,
        "trial_quality_gate_version": TRIAL_QUALITY_GATE_VERSION,
        "judge_version": JUDGE_VERSION,
        "contrastive_judge_version": CONTRASTIVE_JUDGE_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "aggregation_version": AGGREGATION_VERSION,
        "application_contract_version": APPLICATION_CONTRACT_VERSION,
        "support_set_version": SUPPORT_SET_VERSION,
        "ood_policy_version": OOD_POLICY_VERSION,
        "second_judge_policy": "conditional_independent_route_v2",
        "pairwise_tiebreaker_version": PAIRWISE_VERSION,
        "provider_failover_policy": (
            "transport_quota_or_structural_invalid_only_v2"
        ),
        "final_decision_owner": "GLOBAL_TERMINOLOGY_VALIDATOR",
    }
    normalized: dict[str, Any] = {
        "provider_route_order": route_order,
        "evaluation_mode": require_string(
            row["evaluation_mode"], path=f"{path}.evaluation_mode"
        ),
        "threshold_policy": validate_threshold_policy(
            row["threshold_policy"]
        ).as_dict(),
        "selector_candidate_independent": require_bool(
            row["selector_candidate_independent"],
            path=f"{path}.selector_candidate_independent",
        ),
        "selector_mode": require_enum(
            row["selector_mode"],
            {
                "MODEL_CLASSIFICATION_DEVELOPMENT",
                "FROZEN_HUMAN_REVIEWED_SELECTION",
            },
            path=f"{path}.selector_mode",
        ),
        "selector_authority_status": require_enum(
            row["selector_authority_status"],
            {
                "DEVELOPMENT_PENDING_HUMAN_REVIEW",
                "FROZEN_HUMAN_REVIEWED",
            },
            path=f"{path}.selector_authority_status",
        ),
        "dataset_manifest_sha256": require_sha256(
            row["dataset_manifest_sha256"],
            path=f"{path}.dataset_manifest_sha256",
        ),
        "parent_dataset_manifest_sha256": (
            None
            if row["parent_dataset_manifest_sha256"] is None
            else require_sha256(
                row["parent_dataset_manifest_sha256"],
                path=f"{path}.parent_dataset_manifest_sha256",
            )
        ),
        "review_artifact_ref": require_nullable_string(
            row["review_artifact_ref"],
            path=f"{path}.review_artifact_ref",
            maximum=4_000,
        ),
        "review_artifact_sha256": (
            None
            if row["review_artifact_sha256"] is None
            else require_sha256(
                row["review_artifact_sha256"],
                path=f"{path}.review_artifact_sha256",
            )
        ),
        "effective_sense_contract_ref": require_nullable_string(
            row["effective_sense_contract_ref"],
            path=f"{path}.effective_sense_contract_ref",
            maximum=4_000,
        ),
        "effective_sense_contract_sha256": (
            None
            if row["effective_sense_contract_sha256"] is None
            else require_sha256(
                row["effective_sense_contract_sha256"],
                path=f"{path}.effective_sense_contract_sha256",
            )
        ),
        "raw_response_ledger_policy": require_enum(
            row["raw_response_ledger_policy"],
            {"CONTENT_ADDRESSED_V1", "NOT_CONFIGURED_DEVELOPMENT"},
            path=f"{path}.raw_response_ledger_policy",
        ),
    }
    if normalized["selector_candidate_independent"] is not True:
        raise ContractValidationError(
            "selector_policy",
            f"{path}.selector_candidate_independent",
            "CST V2 selector must be candidate independent",
        )
    review_bindings = (
        normalized["review_artifact_ref"],
        normalized["review_artifact_sha256"],
        normalized["effective_sense_contract_ref"],
        normalized["effective_sense_contract_sha256"],
    )
    if normalized["selector_mode"] == "MODEL_CLASSIFICATION_DEVELOPMENT":
        if normalized["selector_authority_status"] != "DEVELOPMENT_PENDING_HUMAN_REVIEW":
            raise ContractValidationError(
                "selector_authority", path, "development selector cannot claim frozen authority"
            )
        if any(item is not None for item in review_bindings):
            raise ContractValidationError(
                "selector_binding", path, "development selector cannot bind human-review artifacts"
            )
    else:
        if normalized["selector_authority_status"] != "FROZEN_HUMAN_REVIEWED":
            raise ContractValidationError(
                "selector_authority", path, "frozen selector requires frozen authority"
            )
        if any(item is None for item in review_bindings):
            raise ContractValidationError(
                "selector_binding", path, "frozen selector requires immutable review bindings"
            )
        for ref_key, sha_key in (
            ("review_artifact_ref", "review_artifact_sha256"),
            ("effective_sense_contract_ref", "effective_sense_contract_sha256"),
        ):
            if not normalized[ref_key].startswith("artifact://") or normalized[sha_key] not in normalized[ref_key]:
                raise ContractValidationError(
                    "selector_binding", f"{path}.{ref_key}", "artifact reference must bind its hash"
                )
        if normalized["raw_response_ledger_policy"] != "CONTENT_ADDRESSED_V1":
            raise ContractValidationError(
                "raw_response_ledger", path, "frozen execution requires content-addressed raw responses"
            )
    normalized["evaluation_mode"] = validate_evaluation_mode(
        normalized["evaluation_mode"],
        validate_threshold_policy(normalized["threshold_policy"]),
    )
    expected_strings["pairwise_policy"] = (
        "close_normalized_C_margin_lt_"
        f"{normalized['threshold_policy']['pairwise_close_margin']}_v2"
    )
    for key, expected in expected_strings.items():
        normalized[key] = require_enum(
            row[key], {expected}, path=f"{path}.{key}"
        )
    expected_ints = {
        "same_sense_target_count": 5,
        "same_sense_minimum_count": 3,
        "contrastive_target_count": 2,
        "contrastive_minimum_count": 1,
        "trial_retry_limit": 1,
    }
    for key, expected in expected_ints.items():
        normalized[key] = require_int(
            row[key], path=f"{path}.{key}", minimum=0
        )
        if normalized[key] != expected:
            raise ContractValidationError(
                "policy_value",
                f"{path}.{key}",
                f"expected {expected}",
            )
    similarity = require_number(
        row["similarity_threshold"],
        path=f"{path}.similarity_threshold",
        minimum=0,
    )
    if similarity != 0.82:
        raise ContractValidationError(
            "policy_value",
            f"{path}.similarity_threshold",
            "expected 0.82",
        )
    normalized["similarity_threshold"] = similarity
    return {
        key: normalized[key]
        for key in row.keys()
    }


def _validate_provider_provenance(
    value: Any, *, path: str
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "provider_route_id",
        "model_id",
        "model_family",
        "independence_group",
        "role",
        "prompt_version",
        "prompt_sha256",
        "response_sha256",
        "request_id",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cached",
        "latency_ms",
    }
    require_exact_keys(row, required=required, path=path)
    input_tokens = require_int(
        row["input_tokens"], path=f"{path}.input_tokens", minimum=0
    )
    output_tokens = require_int(
        row["output_tokens"], path=f"{path}.output_tokens", minimum=0
    )
    reasoning_tokens = require_int(
        row["reasoning_tokens"],
        path=f"{path}.reasoning_tokens",
        minimum=0,
    )
    total_tokens = require_int(
        row["total_tokens"], path=f"{path}.total_tokens", minimum=0
    )
    if total_tokens != input_tokens + output_tokens:
        raise ContractValidationError(
            "usage_total",
            f"{path}.total_tokens",
            "must equal input_tokens + output_tokens",
        )
    if reasoning_tokens > output_tokens:
        raise ContractValidationError(
            "usage_reasoning",
            f"{path}.reasoning_tokens",
            "must be included in output_tokens",
        )
    model_id = require_string(
        row["model_id"], path=f"{path}.model_id", maximum=500
    )
    if "latest" in model_id.casefold():
        raise ContractValidationError(
            "model_binding",
            f"{path}.model_id",
            "must identify a pinned model, not a latest alias",
        )
    return {
        "provider_route_id": require_enum(
            row["provider_route_id"],
            PROVIDER_ROUTE_IDS,
            path=f"{path}.provider_route_id",
        ),
        "model_id": model_id,
        "model_family": require_string(
            row["model_family"],
            path=f"{path}.model_family",
            maximum=500,
        ),
        "independence_group": require_string(
            row["independence_group"],
            path=f"{path}.independence_group",
            maximum=500,
        ),
        "role": require_enum(
            row["role"], PROVIDER_ROLES, path=f"{path}.role"
        ),
        "prompt_version": require_string(
            row["prompt_version"],
            path=f"{path}.prompt_version",
            maximum=500,
        ),
        "prompt_sha256": require_sha256(
            row["prompt_sha256"], path=f"{path}.prompt_sha256"
        ),
        "response_sha256": require_sha256(
            row["response_sha256"], path=f"{path}.response_sha256"
        ),
        "request_id": require_nullable_string(
            row["request_id"], path=f"{path}.request_id", maximum=500
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "cached": require_bool(row["cached"], path=f"{path}.cached"),
        "latency_ms": require_int(
            row["latency_ms"], path=f"{path}.latency_ms", minimum=0
        ),
    }


def _validate_provider_attempt(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "provider_route_id",
        "model_id",
        "model_family",
        "independence_group",
        "role",
        "prompt_version",
        "prompt_sha256",
        "response_sha256",
        "request_id",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cached",
        "latency_ms",
        "accepted",
        "failure_kind",
        "raw_response_ref",
        "raw_response_sha256",
        "raw_response_storage_status",
    }
    require_exact_keys(row, required=required, path=path)
    provenance_input = {
        key: row[key]
        for key in required
        if key
        not in {
            "accepted",
            "failure_kind",
            "raw_response_ref",
            "raw_response_sha256",
            "raw_response_storage_status",
        }
    }
    accepted = require_bool(row["accepted"], path=f"{path}.accepted")
    failure_kind = require_nullable_string(
        row["failure_kind"],
        path=f"{path}.failure_kind",
        maximum=500,
    )
    if accepted != (failure_kind is None):
        raise ContractValidationError(
            "provider_attempt",
            path,
            "accepted and failure_kind are inconsistent",
        )
    if row["response_sha256"] is None:
        if accepted:
            raise ContractValidationError(
                "provider_attempt",
                f"{path}.response_sha256",
                "accepted attempt requires a response hash",
            )
        provenance_input["response_sha256"] = "0" * 64
        normalized = _validate_provider_provenance(
            provenance_input, path=path
        )
        normalized["response_sha256"] = None
    else:
        normalized = _validate_provider_provenance(
            provenance_input, path=path
        )
    storage_status = require_enum(
        row["raw_response_storage_status"],
        {"STORED", "NOT_CONFIGURED", "UNAVAILABLE"},
        path=f"{path}.raw_response_storage_status",
    )
    raw_ref = require_nullable_string(
        row["raw_response_ref"],
        path=f"{path}.raw_response_ref",
        maximum=4_000,
    )
    raw_sha = (
        None
        if row["raw_response_sha256"] is None
        else require_sha256(
            row["raw_response_sha256"],
            path=f"{path}.raw_response_sha256",
        )
    )
    if storage_status == "STORED":
        if raw_ref is None or raw_sha is None:
            raise ContractValidationError(
                "raw_response_ledger",
                path,
                "stored response requires content-addressed ref and hash",
            )
        if not raw_ref.startswith("provider_responses/") or not raw_ref.endswith(".txt"):
            raise ContractValidationError(
                "raw_response_ledger",
                f"{path}.raw_response_ref",
                "stored response ref must be provider_responses/<sha>.txt",
            )
        if raw_sha not in raw_ref or (
            normalized.get("response_sha256") is not None
            and raw_sha != normalized["response_sha256"]
        ):
            raise ContractValidationError(
                "raw_response_ledger",
                path,
                "raw response hash must agree with the provider response hash",
            )
    elif raw_ref is not None:
        raise ContractValidationError(
            "raw_response_ledger",
            f"{path}.raw_response_ref",
            "unstored response cannot claim a physical ref",
        )
    return {
        **normalized,
        "raw_response_ref": raw_ref,
        "raw_response_sha256": raw_sha,
        "raw_response_storage_status": storage_status,
        "accepted": accepted,
        "failure_kind": failure_kind,
    }


def _validate_candidate(
    value: Any,
    *,
    pairwise_records: Sequence[Mapping[str, Any]],
    threshold_policy: ContextThresholdPolicy,
    selector_mode: str,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "term_id",
        "candidate_id",
        "target_role",
        "source_term",
        "candidate_translation",
        "sense_id",
        "scope_id",
        "sense_contract",
        "part_of_speech",
        "source_occurrences",
        "candidate_generation",
        "selector_annotations",
        "selector_provenance",
        "selector_context_sources",
        "selected_same_sense_context_ids",
        "selected_contrastive_context_ids",
        "missing_same_sense_context_types",
        "context_results",
        "excluded_contexts",
        "contrastive_results",
        "contextual_evidence",
        "context_flags",
        "sense_boundary_observations",
        "application_contract",
        "certificate_support_set",
        "second_judge_invoked",
        "judge_disagreement",
        "judge_independence",
        "recommendation_to_global_validator",
        "final_glossary_decision",
        "provenance",
    }
    require_exact_keys(row, required=required, path=path)
    candidate_id = require_string(
        row["candidate_id"], path=f"{path}.candidate_id"
    )
    candidate_translation = require_string(
        row["candidate_translation"],
        path=f"{path}.candidate_translation",
        maximum=500,
    )
    sense_contract = _validate_sense_contract(
        row["sense_contract"], path=f"{path}.sense_contract"
    )
    candidate_generation = _validate_candidate_generation(
        row["candidate_generation"],
        path=f"{path}.candidate_generation",
    )
    annotations = [
        validate_selector_annotation(
            child, path=f"{path}.selector_annotations[{index}]"
        )
        for index, child in enumerate(
            require_list(
                row["selector_annotations"],
                path=f"{path}.selector_annotations",
            )
        )
    ]
    annotation_ids = [child["context_id"] for child in annotations]
    require_unique(annotation_ids, path=f"{path}.selector_annotations")
    selector_context_sources = _validate_selector_context_sources(
        row["selector_context_sources"],
        path=f"{path}.selector_context_sources",
    )
    if [source["context_id"] for source in selector_context_sources] != (
        annotation_ids
    ):
        raise ContractValidationError(
            "selector_source_binding",
            f"{path}.selector_context_sources",
            "selector sources must follow and cover every annotation",
        )
    selector_provenance = (
        None
        if row["selector_provenance"] is None
        else _validate_provider_provenance(
            row["selector_provenance"],
            path=f"{path}.selector_provenance",
        )
    )
    if annotations and selector_provenance is None and selector_mode != "FROZEN_HUMAN_REVIEWED_SELECTION":
        raise ContractValidationError(
            "selector_provenance",
            f"{path}.selector_provenance",
            "selector annotations require provenance",
        )
    if selector_mode == "FROZEN_HUMAN_REVIEWED_SELECTION" and selector_provenance is not None:
        raise ContractValidationError(
            "selector_provenance",
            f"{path}.selector_provenance",
            "frozen reviewed selection must not be reclassified by a provider",
        )
    if selector_provenance is not None and (
        selector_provenance["role"] != "context_selector"
        or selector_provenance["prompt_version"] != SELECTOR_VERSION
    ):
        raise ContractValidationError(
            "selector_provenance",
            f"{path}.selector_provenance",
            "selector role or version mismatch",
        )
    same_ids = _string_list(
        row["selected_same_sense_context_ids"],
        path=f"{path}.selected_same_sense_context_ids",
        minimum=0,
    )
    contrastive_ids = _string_list(
        row["selected_contrastive_context_ids"],
        path=f"{path}.selected_contrastive_context_ids",
        minimum=0,
    )
    require_unique(same_ids, path=f"{path}.selected_same_sense_context_ids")
    require_unique(
        contrastive_ids,
        path=f"{path}.selected_contrastive_context_ids",
    )
    missing_context_types = [
        require_enum(
            child,
            set(REQUIRED_SAME_SENSE_CONTEXT_TYPES),
            path=f"{path}.missing_same_sense_context_types[{index}]",
        )
        for index, child in enumerate(
            require_list(
                row["missing_same_sense_context_types"],
                path=f"{path}.missing_same_sense_context_types",
            )
        )
    ]
    require_unique(
        missing_context_types,
        path=f"{path}.missing_same_sense_context_types",
    )
    if missing_context_types != [
        value
        for value in REQUIRED_SAME_SENSE_CONTEXT_TYPES
        if value in set(missing_context_types)
    ]:
        raise ContractValidationError(
            "context_type_order",
            f"{path}.missing_same_sense_context_types",
            "missing context types must follow the required C1-C5 order",
        )
    annotation_by_id = {child["context_id"]: child for child in annotations}
    if any(
        context_id not in annotation_by_id
        or annotation_by_id[context_id]["sense_relation"] != "SAME_SENSE"
        for context_id in same_ids
    ):
        raise ContractValidationError(
            "selector_binding",
            f"{path}.selected_same_sense_context_ids",
            "selected context is not a same-sense selector annotation",
        )
    selected_context_types = {
        annotation_by_id[context_id]["context_type"]
        for context_id in same_ids
        if context_id in annotation_by_id
    }
    expected_missing_context_types = [
        value
        for value in REQUIRED_SAME_SENSE_CONTEXT_TYPES
        if value not in selected_context_types
    ]
    if missing_context_types != expected_missing_context_types:
        raise ContractValidationError(
            "context_type_coverage",
            f"{path}.missing_same_sense_context_types",
            "missing context types differ from selected same-sense evidence",
        )
    if any(
        context_id not in annotation_by_id
        or annotation_by_id[context_id]["sense_relation"] != "CONTRASTIVE"
        for context_id in contrastive_ids
    ):
        raise ContractValidationError(
            "selector_binding",
            f"{path}.selected_contrastive_context_ids",
            "selected context is not a contrastive selector annotation",
        )
    context_results = [
        _validate_context_result(
            child,
            candidate_id=candidate_id,
            candidate_translation=candidate_translation,
            path=f"{path}.context_results[{index}]",
        )
        for index, child in enumerate(
            require_list(
                row["context_results"], path=f"{path}.context_results"
            )
        )
    ]
    excluded = [
        _validate_excluded_context(
            child,
            candidate_id=candidate_id,
            candidate_translation=candidate_translation,
            path=f"{path}.excluded_contexts[{index}]",
        )
        for index, child in enumerate(
            require_list(
                row["excluded_contexts"], path=f"{path}.excluded_contexts"
            )
        )
    ]
    result_ids = [child["context_id"] for child in context_results]
    excluded_ids = [child["context_id"] for child in excluded]
    require_unique(
        [*result_ids, *excluded_ids],
        path=f"{path}.context_results_and_excluded_contexts",
    )
    if set(same_ids) != set(result_ids) | set(excluded_ids):
        raise ContractValidationError(
            "context_attempt_binding",
            f"{path}.selected_same_sense_context_ids",
            "selected same-sense IDs must exactly cover accepted and excluded attempts",
        )
    contrastive = [
        _validate_contrastive_result(
            child,
            candidate_id=candidate_id,
            path=f"{path}.contrastive_results[{index}]",
        )
        for index, child in enumerate(
            require_list(
                row["contrastive_results"],
                path=f"{path}.contrastive_results",
            )
        )
    ]
    if [child["context_id"] for child in contrastive] != contrastive_ids:
        raise ContractValidationError(
            "contrastive_binding",
            f"{path}.contrastive_results",
            "contrastive results must follow selected contrastive IDs",
        )
    flags = _enum_list(
        row["context_flags"],
        CONTEXT_FLAGS,
        path=f"{path}.context_flags",
    )
    second_judge_invoked = require_bool(
        row["second_judge_invoked"],
        path=f"{path}.second_judge_invoked",
    )
    judge_disagreement = require_bool(
        row["judge_disagreement"],
        path=f"{path}.judge_disagreement",
    )
    judge_independence = _validate_judge_independence(
        row["judge_independence"],
        context_results=context_results,
        second_judge_invoked=second_judge_invoked,
        flags=flags,
        path=f"{path}.judge_independence",
    )
    expected_base_flags = _expected_candidate_flags(
        sense_contract=sense_contract,
        context_results=context_results,
        selected_contrastive_ids=contrastive_ids,
        judge_disagreement=judge_disagreement,
        judge_independence_status=judge_independence["status"],
        missing_context_types=missing_context_types,
    )
    if (set(flags) - {_PAIRWISE_FLAG}) != expected_base_flags:
        raise ContractValidationError(
            "context_flags",
            f"{path}.context_flags",
            "context flags do not match source evidence",
        )
    contextual_evidence = _validate_contextual_evidence(
        row["contextual_evidence"],
        context_results=context_results,
        invalid_context_count=len(excluded),
        context_flags=flags,
        contrastive_results=contrastive,
        threshold_policy=threshold_policy,
        path=f"{path}.contextual_evidence",
    )
    boundary = _validate_sense_boundary_observations(
        row["sense_boundary_observations"],
        contrastive_results=contrastive,
        path=f"{path}.sense_boundary_observations",
    )
    application_contract = _validate_application_contract(
        row["application_contract"],
        expected=build_application_contract(
            canonical_target=candidate_translation,
            context_results=context_results,
        ),
        path=f"{path}.application_contract",
    )
    support_set = _validate_support_set(
        row["certificate_support_set"],
        expected=build_certificate_support_set(
            context_results, contrastive
        ),
        path=f"{path}.certificate_support_set",
    )
    recommendation = require_enum(
        row["recommendation_to_global_validator"],
        GLOBAL_RECOMMENDATIONS,
        path=f"{path}.recommendation_to_global_validator",
    )
    expected_recommendation = global_recommendation(
        contextual_status_value=contextual_evidence["status"],
        context_flags=flags,
        threshold_policy_status=threshold_policy.policy_status,
    )
    if recommendation != expected_recommendation:
        raise ContractValidationError(
            "recommendation",
            f"{path}.recommendation_to_global_validator",
            "recommendation does not match contextual evidence",
        )
    if row["final_glossary_decision"] is not None:
        raise ContractValidationError(
            "authority_boundary",
            f"{path}.final_glossary_decision",
            "CST V2 must leave the final glossary decision null",
        )
    normalized = {
        "term_id": require_string(row["term_id"], path=f"{path}.term_id"),
        "candidate_id": candidate_id,
        "target_role": require_enum(
            row["target_role"], _TARGET_ROLES, path=f"{path}.target_role"
        ),
        "source_term": require_string(
            row["source_term"], path=f"{path}.source_term", maximum=500
        ),
        "candidate_translation": candidate_translation,
        "sense_id": require_string(
            row["sense_id"], path=f"{path}.sense_id", maximum=500
        ),
        "scope_id": require_string(
            row["scope_id"], path=f"{path}.scope_id", maximum=500
        ),
        "sense_contract": sense_contract,
        "part_of_speech": require_string(
            row["part_of_speech"],
            path=f"{path}.part_of_speech",
            maximum=100,
        ),
        "source_occurrences": _string_list(
            row["source_occurrences"],
            path=f"{path}.source_occurrences",
            minimum=1,
        ),
        "candidate_generation": candidate_generation,
        "selector_annotations": annotations,
        "selector_provenance": selector_provenance,
        "selector_context_sources": selector_context_sources,
        "selected_same_sense_context_ids": same_ids,
        "selected_contrastive_context_ids": contrastive_ids,
        "missing_same_sense_context_types": missing_context_types,
        "context_results": context_results,
        "excluded_contexts": excluded,
        "contrastive_results": contrastive,
        "contextual_evidence": contextual_evidence,
        "context_flags": flags,
        "sense_boundary_observations": boundary,
        "application_contract": application_contract,
        "certificate_support_set": support_set,
        "second_judge_invoked": second_judge_invoked,
        "judge_disagreement": judge_disagreement,
        "judge_independence": judge_independence,
        "recommendation_to_global_validator": recommendation,
        "final_glossary_decision": None,
        "provenance": {},
    }
    normalized["provenance"] = _validate_candidate_provenance(
        row["provenance"],
        candidate=normalized,
        pairwise_records=pairwise_records,
        path=f"{path}.provenance",
    )
    return normalized


def _validate_selector_context_sources(
    value: Any, *, path: str
) -> list[dict[str, Any]]:
    sources = []
    for index, child in enumerate(require_list(value, path=path)):
        child_path = f"{path}[{index}]"
        row = require_mapping(child, path=child_path)
        require_exact_keys(
            row,
            required={"context_id", "source_sha256", "source_provenance"},
            path=child_path,
        )
        source_sha256 = require_sha256(
            row["source_sha256"], path=f"{child_path}.source_sha256"
        )
        provenance = validate_source_provenance(
            row["source_provenance"],
            path=f"{child_path}.source_provenance",
        )
        if provenance["source_hash"] != source_sha256:
            raise ContractValidationError(
                "selector_source_binding",
                child_path,
                "selector source hash differs from physical provenance",
            )
        sources.append(
            {
                "context_id": require_string(
                    row["context_id"], path=f"{child_path}.context_id"
                ),
                "source_sha256": source_sha256,
                "source_provenance": provenance,
            }
        )
    require_unique(
        [row["context_id"] for row in sources], path=f"{path}[*].context_id"
    )
    return sources


def _validate_sense_contract(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "definition_en",
            "definition_source",
            "definition_provenance",
            "definition_review_status",
            "sense_inventory_version",
        },
        path=path,
    )
    provenance = _string_list(
        row["definition_provenance"],
        path=f"{path}.definition_provenance",
        minimum=1,
    )
    return {
        "definition_en": require_string(
            row["definition_en"],
            path=f"{path}.definition_en",
            maximum=4_000,
        ),
        "definition_source": require_string(
            row["definition_source"],
            path=f"{path}.definition_source",
            maximum=500,
        ),
        "definition_provenance": provenance,
        "definition_review_status": require_enum(
            row["definition_review_status"],
            SENSE_DEFINITION_STATUSES,
            path=f"{path}.definition_review_status",
        ),
        "sense_inventory_version": require_string(
            row["sense_inventory_version"],
            path=f"{path}.sense_inventory_version",
            maximum=500,
        ),
    }


def _validate_candidate_generation(
    value: Any, *, path: str
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    legacy_keys = {
        "generator_model",
        "prompt_version",
        "run_id",
        "recording_status",
    }
    reviewed_keys = legacy_keys | {
        "candidate_version",
        "candidate_slot_id",
        "candidate_slot_status",
        "formation_method",
    }
    actual_keys = set(row)
    if actual_keys != legacy_keys and actual_keys != reviewed_keys:
        raise ContractValidationError(
            "candidate_generation", path, "unexpected candidate generation fields"
        )
    result = {
        key: require_nullable_string(
            row[key], path=f"{path}.{key}", maximum=500
        )
        for key in ("generator_model", "prompt_version", "run_id")
    }
    status = require_enum(
        row["recording_status"],
        {"RECORDED", "UNAVAILABLE_IN_SEALED_ARTIFACT"},
        path=f"{path}.recording_status",
    )
    if status == "RECORDED" and not any(result.values()):
        raise ContractValidationError(
            "candidate_generation",
            path,
            "RECORDED requires at least one identifier",
        )
    if status == "UNAVAILABLE_IN_SEALED_ARTIFACT" and any(result.values()):
        raise ContractValidationError(
            "candidate_generation",
            path,
            "unavailable metadata must remain null",
        )
    if set(row) == legacy_keys:
        return {**result, "recording_status": status}
    candidate_version = require_nullable_string(
        row["candidate_version"],
        path=f"{path}.candidate_version",
        maximum=64,
    )
    if candidate_version is not None:
        candidate_version = require_sha256(
            candidate_version, path=f"{path}.candidate_version"
        )
    slot_id = require_nullable_string(
        row["candidate_slot_id"],
        path=f"{path}.candidate_slot_id",
        maximum=500,
    )
    source_status = require_enum(
        row["candidate_slot_status"],
        {"RECORDED", "MODEL_GENERATED", "UNAVAILABLE_IN_LEGACY_ARTIFACT"},
        path=f"{path}.candidate_slot_status",
    )
    formation_method = require_nullable_string(
        row["formation_method"],
        path=f"{path}.formation_method",
        maximum=500,
    )
    if source_status == "UNAVAILABLE_IN_LEGACY_ARTIFACT":
        if any(value is not None for value in (candidate_version, slot_id, formation_method)):
            raise ContractValidationError(
                "candidate_version",
                path,
                "legacy-unavailable candidate metadata must remain null",
            )
    elif any(value is None for value in (candidate_version, slot_id, formation_method)):
        raise ContractValidationError(
            "candidate_version",
            path,
            "reviewed-support candidates require version, slot, and formation metadata",
        )
    return {
        **result,
        "recording_status": status,
        "candidate_version": candidate_version,
        "candidate_slot_id": slot_id,
        "candidate_slot_status": source_status,
        "formation_method": formation_method,
    }


def _validate_context_result(
    value: Any,
    *,
    candidate_id: str,
    candidate_translation: str,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "context_id",
        "context_type",
        "source_sha256",
        "source_provenance",
        "trial_attempts",
        "trial_translation",
        "primary_judge",
        "secondary_judge",
        "raw_score",
        "label",
        "local_hard_flags",
        "reason",
    }
    require_exact_keys(row, required=required, path=path)
    context_id = require_string(
        row["context_id"], path=f"{path}.context_id"
    )
    provenance = validate_source_provenance(
        row["source_provenance"],
        path=f"{path}.source_provenance",
    )
    source_sha256 = require_sha256(
        row["source_sha256"], path=f"{path}.source_sha256"
    )
    if provenance["source_hash"] != source_sha256:
        raise ContractValidationError(
            "source_binding",
            path,
            "source hash differs from source provenance",
        )
    attempts = _validate_trial_attempts(
        row["trial_attempts"],
        context_id=context_id,
        candidate_id=candidate_id,
        candidate_translation=candidate_translation,
        path=f"{path}.trial_attempts",
    )
    valid_attempts = [
        attempt
        for attempt in attempts
        if attempt["effective_trial_status"] == "VALID"
    ]
    if len(valid_attempts) != 1 or valid_attempts[0] is not attempts[-1]:
        raise ContractValidationError(
            "trial_attempts",
            f"{path}.trial_attempts",
            "accepted context requires exactly one final VALID attempt",
        )
    trial_translation = require_string(
        row["trial_translation"],
        path=f"{path}.trial_translation",
        maximum=20_000,
    )
    if trial_translation != valid_attempts[0]["trial"]["trial_translation"]:
        raise ContractValidationError(
            "trial_binding",
            f"{path}.trial_translation",
            "trial translation differs from the accepted attempt",
        )
    primary = _validate_judge_wrapper(
        row["primary_judge"],
        context_id=context_id,
        candidate_id=candidate_id,
        expected_role="context_judge",
        path=f"{path}.primary_judge",
    )
    if primary["output"]["judgeability"] != "JUDGEABLE":
        raise ContractValidationError(
            "judgeability",
            f"{path}.primary_judge",
            "accepted context requires a judgeable primary output",
        )
    secondary = (
        None
        if row["secondary_judge"] is None
        else _validate_judge_wrapper(
            row["secondary_judge"],
            context_id=context_id,
            candidate_id=candidate_id,
            expected_role="context_judge",
            path=f"{path}.secondary_judge",
        )
    )
    raw_score, label, local_flags = compute_context_result(primary["output"])
    if secondary is not None and secondary["output"]["judgeability"] == "JUDGEABLE":
        secondary_score, secondary_label, secondary_flags = (
            compute_context_result(secondary["output"])
        )
        label, _significant = merge_judge_labels(label, secondary_label)
        raw_score = min(raw_score, secondary_score)
        local_flags = sorted(set(local_flags) | set(secondary_flags))
    stored_score = require_int(
        row["raw_score"], path=f"{path}.raw_score", minimum=0
    )
    if stored_score > 10 or stored_score != raw_score:
        raise ContractValidationError(
            "context_score",
            f"{path}.raw_score",
            "raw score does not match Judge component scores",
        )
    stored_label = require_enum(
        row["label"], CONTEXT_LABELS, path=f"{path}.label"
    )
    if stored_label != label:
        raise ContractValidationError(
            "context_label",
            f"{path}.label",
            "label does not match code-owned rubric",
        )
    stored_flags = _enum_list(
        row["local_hard_flags"],
        LOCAL_HARD_FLAGS,
        path=f"{path}.local_hard_flags",
    )
    if set(stored_flags) != set(local_flags):
        raise ContractValidationError(
            "context_flags",
            f"{path}.local_hard_flags",
            "local flags do not match code-owned rubric",
        )
    reason = require_string(
        row["reason"], path=f"{path}.reason", maximum=2_000
    )
    if reason != primary["output"]["reason"]:
        raise ContractValidationError(
            "judge_binding",
            f"{path}.reason",
            "context reason must come from the primary Judge",
        )
    return {
        "context_id": context_id,
        "context_type": require_enum(
            row["context_type"],
            CONTEXT_TYPES - {"contrastive"},
            path=f"{path}.context_type",
        ),
        "source_sha256": source_sha256,
        "source_provenance": provenance,
        "trial_attempts": attempts,
        "trial_translation": trial_translation,
        "primary_judge": primary,
        "secondary_judge": secondary,
        "raw_score": stored_score,
        "label": stored_label,
        "local_hard_flags": stored_flags,
        "reason": reason,
    }


def _validate_trial_attempts(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
    candidate_translation: str,
    path: str,
) -> list[dict[str, Any]]:
    rows = [
        _validate_trial_attempt(
            child,
            context_id=context_id,
            candidate_id=candidate_id,
            candidate_translation=candidate_translation,
            path=f"{path}[{index}]",
        )
        for index, child in enumerate(require_list(value, path=path))
    ]
    if not 1 <= len(rows) <= 2:
        raise ContractValidationError(
            "trial_retry", path, "one initial attempt and at most one retry"
        )
    if [row["attempt"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ContractValidationError(
            "trial_retry", path, "attempt ordinals must be contiguous from 1"
        )
    if len(rows) == 2 and rows[0]["effective_trial_status"] == "VALID":
        raise ContractValidationError(
            "trial_retry", path, "a VALID first attempt must not be retried"
        )
    return rows


def _validate_trial_attempt(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
    candidate_translation: str,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "attempt",
            "trial",
            "trial_provenance",
            "gate",
            "gate_provenance",
            "local_candidate_literal_match",
            "observed_candidate_surface",
            "effective_trial_status",
        },
        path=path,
    )
    trial = validate_trial(
        row["trial"], context_id=context_id, candidate_id=candidate_id
    )
    gate = validate_trial_gate(
        row["gate"], context_id=context_id, candidate_id=candidate_id
    )
    trial_provenance = _validate_provider_provenance(
        row["trial_provenance"], path=f"{path}.trial_provenance"
    )
    gate_provenance = _validate_provider_provenance(
        row["gate_provenance"], path=f"{path}.gate_provenance"
    )
    if (
        trial_provenance["role"] != "trial_translator"
        or trial_provenance["prompt_version"] != TRIAL_TRANSLATOR_VERSION
        or gate_provenance["role"] != "trial_translation_quality_gate"
        or gate_provenance["prompt_version"] != TRIAL_QUALITY_GATE_VERSION
    ):
        raise ContractValidationError(
            "trial_provenance", path, "trial role or version mismatch"
        )
    expected_literal_match, expected_observed_surface = trial_surface_binding(
        canonical_target=candidate_translation,
        trial=trial,
    )
    stored_literal_match = require_bool(
        row["local_candidate_literal_match"],
        path=f"{path}.local_candidate_literal_match",
    )
    if stored_literal_match != expected_literal_match:
        raise ContractValidationError(
            "trial_literal",
            f"{path}.local_candidate_literal_match",
            "literal match does not match trial output",
        )
    observed_surface = require_nullable_string(
        row["observed_candidate_surface"],
        path=f"{path}.observed_candidate_surface",
        maximum=500,
    )
    if observed_surface != expected_observed_surface:
        raise ContractValidationError(
            "trial_surface",
            f"{path}.observed_candidate_surface",
            "observed surface does not match the accepted trial text",
        )
    expected_status = gate["trial_status"]
    if expected_status == "VALID" and not expected_literal_match:
        expected_status = "INVALID_CANDIDATE_USAGE"
    effective_status = require_enum(
        row["effective_trial_status"],
        TRIAL_STATUSES,
        path=f"{path}.effective_trial_status",
    )
    if effective_status != expected_status:
        raise ContractValidationError(
            "trial_status",
            f"{path}.effective_trial_status",
            "effective status does not match gate and literal validation",
        )
    return {
        "attempt": require_int(
            row["attempt"], path=f"{path}.attempt", minimum=1
        ),
        "trial": trial,
        "trial_provenance": trial_provenance,
        "gate": gate,
        "gate_provenance": gate_provenance,
        "local_candidate_literal_match": stored_literal_match,
        "observed_candidate_surface": observed_surface,
        "effective_trial_status": effective_status,
    }


def _validate_judge_wrapper(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
    expected_role: str,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"output", "provenance"}, path=path)
    output = validate_context_judge(
        row["output"], context_id=context_id, candidate_id=candidate_id
    )
    provenance = _validate_provider_provenance(
        row["provenance"], path=f"{path}.provenance"
    )
    if (
        provenance["role"] != expected_role
        or provenance["prompt_version"] != JUDGE_VERSION
    ):
        raise ContractValidationError(
            "judge_provenance", path, "Judge role or version mismatch"
        )
    return {"output": output, "provenance": provenance}


def _validate_excluded_context(
    value: Any,
    *,
    candidate_id: str,
    candidate_translation: str,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "source_provenance",
            "reason",
            "trial_attempts",
            "judge_output",
            "judge_provenance",
        },
        path=path,
    )
    context_id = require_string(
        row["context_id"], path=f"{path}.context_id"
    )
    provenance = validate_source_provenance(
        row["source_provenance"], path=f"{path}.source_provenance"
    )
    attempts = _validate_trial_attempts(
        row["trial_attempts"],
        context_id=context_id,
        candidate_id=candidate_id,
        candidate_translation=candidate_translation,
        path=f"{path}.trial_attempts",
    )
    reason = require_string(
        row["reason"], path=f"{path}.reason", maximum=500
    )
    judge_output = row["judge_output"]
    judge_provenance = row["judge_provenance"]
    if (judge_output is None) != (judge_provenance is None):
        raise ContractValidationError(
            "excluded_judge", path, "Judge output/provenance must be paired"
        )
    if judge_output is None:
        if any(
            attempt["effective_trial_status"] == "VALID"
            for attempt in attempts
        ):
            raise ContractValidationError(
                "excluded_trial",
                path,
                "trial exclusion cannot contain a VALID attempt",
            )
        if reason != "trial_invalid_after_one_retry" or len(attempts) != 2:
            raise ContractValidationError(
                "excluded_trial",
                path,
                "trial exclusion requires one exhausted retry",
            )
    else:
        if attempts[-1]["effective_trial_status"] != "VALID":
            raise ContractValidationError(
                "excluded_judge",
                path,
                "Judge exclusion requires a valid trial",
            )
        judge_output = validate_context_judge(
            judge_output,
            context_id=context_id,
            candidate_id=candidate_id,
        )
        if judge_output["judgeability"] == "JUDGEABLE":
            raise ContractValidationError(
                "excluded_judge",
                path,
                "judgeable context must not be excluded",
            )
        judge_provenance = _validate_provider_provenance(
            judge_provenance, path=f"{path}.judge_provenance"
        )
        if (
            judge_provenance["role"] != "context_judge"
            or judge_provenance["prompt_version"] != JUDGE_VERSION
            or reason != f"judgeability:{judge_output['judgeability']}"
        ):
            raise ContractValidationError(
                "excluded_judge", path, "Judge exclusion binding mismatch"
            )
    return {
        "context_id": context_id,
        "source_provenance": provenance,
        "reason": reason,
        "trial_attempts": attempts,
        "judge_output": judge_output,
        "judge_provenance": judge_provenance,
    }


def _validate_contrastive_result(
    value: Any,
    *,
    candidate_id: str,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "context_id",
        "candidate_id",
        "tested_sense_id",
        "result",
        "reason",
        "source_provenance",
        "provenance",
    }
    require_exact_keys(row, required=required, path=path)
    context_id = require_string(
        row["context_id"], path=f"{path}.context_id"
    )
    tested_sense_id = require_string(
        row["tested_sense_id"], path=f"{path}.tested_sense_id"
    )
    validated = validate_contrastive(
        {key: row[key] for key in required if key not in {"source_provenance", "provenance"}},
        context_id=context_id,
        candidate_id=candidate_id,
        tested_sense_id=tested_sense_id,
    )
    source_provenance = validate_source_provenance(
        row["source_provenance"], path=f"{path}.source_provenance"
    )
    provenance = _validate_provider_provenance(
        row["provenance"], path=f"{path}.provenance"
    )
    if (
        provenance["role"] != "contrastive_sense_judge"
        or provenance["prompt_version"] != CONTRASTIVE_JUDGE_VERSION
    ):
        raise ContractValidationError(
            "contrastive_provenance", path, "role or version mismatch"
        )
    return {
        **validated,
        "source_provenance": source_provenance,
        "provenance": provenance,
    }


def _validate_contextual_evidence(
    value: Any,
    *,
    context_results: Sequence[Mapping[str, Any]],
    invalid_context_count: int,
    context_flags: Sequence[str],
    contrastive_results: Sequence[Mapping[str, Any]],
    threshold_policy: ContextThresholdPolicy,
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    expected = aggregate_contextual_evidence(
        context_results,
        invalid_context_count=invalid_context_count,
        context_flags=context_flags,
        contrastive_results=contrastive_results,
        threshold_policy=threshold_policy,
    )
    require_exact_keys(row, required=set(expected), path=path)
    normalized = {
        "C": _nullable_number(
            row["C"], path=f"{path}.C", minimum=0, maximum=1
        ),
        "score_interpretation": require_enum(
            row["score_interpretation"],
            {"normalized_contextual_support_not_probability"},
            path=f"{path}.score_interpretation",
        ),
        "raw_context_scores": [
            require_int(
                child,
                path=f"{path}.raw_context_scores[{index}]",
                minimum=0,
            )
            for index, child in enumerate(
                require_list(
                    row["raw_context_scores"],
                    path=f"{path}.raw_context_scores",
                )
            )
        ],
        "valid_context_count": require_int(
            row["valid_context_count"],
            path=f"{path}.valid_context_count",
            minimum=0,
        ),
        "invalid_context_count": require_int(
            row["invalid_context_count"],
            path=f"{path}.invalid_context_count",
            minimum=0,
        ),
        "pass_count": require_int(
            row["pass_count"], path=f"{path}.pass_count", minimum=0
        ),
        "minor_count": require_int(
            row["minor_count"], path=f"{path}.minor_count", minimum=0
        ),
        "fail_count": require_int(
            row["fail_count"], path=f"{path}.fail_count", minimum=0
        ),
        "minimum_raw_score": _nullable_int(
            row["minimum_raw_score"],
            path=f"{path}.minimum_raw_score",
            minimum=0,
            maximum=10,
        ),
        "maximum_raw_score": _nullable_int(
            row["maximum_raw_score"],
            path=f"{path}.maximum_raw_score",
            minimum=0,
            maximum=10,
        ),
        "score_range": _nullable_int(
            row["score_range"],
            path=f"{path}.score_range",
            minimum=0,
            maximum=10,
        ),
        "status": require_enum(
            row["status"], CONTEXTUAL_STATUSES, path=f"{path}.status"
        ),
        "aggregation_policy_version": require_enum(
            row["aggregation_policy_version"],
            {AGGREGATION_VERSION},
            path=f"{path}.aggregation_policy_version",
        ),
        "threshold_policy_version": require_enum(
            row["threshold_policy_version"],
            {threshold_policy.policy_version},
            path=f"{path}.threshold_policy_version",
        ),
        "threshold_policy_status": require_enum(
            row["threshold_policy_status"],
            {threshold_policy.policy_status},
            path=f"{path}.threshold_policy_status",
        ),
    }
    if normalized != expected:
        raise ContractValidationError(
            "contextual_evidence",
            path,
            "aggregate differs from recomputed CST evidence",
        )
    return normalized


def _validate_sense_boundary_observations(
    value: Any,
    *,
    contrastive_results: Sequence[Mapping[str, Any]],
    path: str,
) -> list[dict[str, str]]:
    rows = []
    for index, child in enumerate(require_list(value, path=path)):
        child_path = f"{path}[{index}]"
        row = require_mapping(child, path=child_path)
        require_exact_keys(
            row,
            required={
                "contrastive_context_id",
                "contrastive_sense_id",
                "result",
                "reason",
            },
            path=child_path,
        )
        rows.append(
            {
                "contrastive_context_id": require_string(
                    row["contrastive_context_id"],
                    path=f"{child_path}.contrastive_context_id",
                ),
                "contrastive_sense_id": require_string(
                    row["contrastive_sense_id"],
                    path=f"{child_path}.contrastive_sense_id",
                ),
                "result": require_string(
                    row["result"], path=f"{child_path}.result"
                ),
                "reason": require_string(
                    row["reason"],
                    path=f"{child_path}.reason",
                    maximum=2_000,
                ),
            }
        )
    expected = [
        {
            "contrastive_context_id": row["context_id"],
            "contrastive_sense_id": row["tested_sense_id"],
            "result": row["result"],
            "reason": row["reason"],
        }
        for row in contrastive_results
    ]
    if rows != expected:
        raise ContractValidationError(
            "sense_boundary", path, "observations differ from contrastive results"
        )
    return rows


def _validate_application_contract(
    value: Any,
    *,
    expected: Mapping[str, Any],
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=set(expected), path=path)
    normalized = {
        "schema_version": require_enum(
            row["schema_version"],
            {APPLICATION_CONTRACT_VERSION},
            path=f"{path}.schema_version",
        ),
        "canonical_target": require_string(
            row["canonical_target"],
            path=f"{path}.canonical_target",
            maximum=500,
        ),
        "canonical_observed_context_ids": _string_list(
            row["canonical_observed_context_ids"],
            path=f"{path}.canonical_observed_context_ids",
            minimum=0,
        ),
        "allowed_variants": [
            _validate_allowed_variant(
                child, path=f"{path}.allowed_variants[{index}]"
            )
            for index, child in enumerate(
                require_list(
                    row["allowed_variants"],
                    path=f"{path}.allowed_variants",
                )
            )
        ],
        "disallowed_variants": [
            _validate_disallowed_variant(
                child, path=f"{path}.disallowed_variants[{index}]"
            )
            for index, child in enumerate(
                require_list(
                    row["disallowed_variants"],
                    path=f"{path}.disallowed_variants",
                )
            )
        ],
        "application_notes": [
            _validate_application_note(
                child, path=f"{path}.application_notes[{index}]"
            )
            for index, child in enumerate(
                require_list(
                    row["application_notes"],
                    path=f"{path}.application_notes",
                )
            )
        ],
        "variant_authority": require_enum(
            row["variant_authority"],
            {"OBSERVATION_ONLY_NOT_AUTO_SEALED"},
            path=f"{path}.variant_authority",
        ),
    }
    if normalized != expected:
        raise ContractValidationError(
            "application_contract",
            path,
            "application contract differs from Judge observations",
        )
    return normalized


def _validate_allowed_variant(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"surface", "status", "context_ids", "sealed"},
        path=path,
    )
    sealed = require_bool(row["sealed"], path=f"{path}.sealed")
    if sealed:
        raise ContractValidationError(
            "variant_authority", f"{path}.sealed", "CST cannot seal variants"
        )
    return {
        "surface": require_string(
            row["surface"], path=f"{path}.surface", maximum=500
        ),
        "status": require_enum(
            row["status"], VARIANT_STATUSES, path=f"{path}.status"
        ),
        "context_ids": _string_list(
            row["context_ids"], path=f"{path}.context_ids", minimum=1
        ),
        "sealed": False,
    }


def _validate_disallowed_variant(
    value: Any, *, path: str
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"surface", "reason", "context_ids", "sealed"},
        path=path,
    )
    sealed = require_bool(row["sealed"], path=f"{path}.sealed")
    if sealed:
        raise ContractValidationError(
            "variant_authority", f"{path}.sealed", "CST cannot seal variants"
        )
    return {
        "surface": require_string(
            row["surface"], path=f"{path}.surface", maximum=500
        ),
        "reason": require_string(
            row["reason"], path=f"{path}.reason", maximum=500
        ),
        "context_ids": _string_list(
            row["context_ids"], path=f"{path}.context_ids", minimum=1
        ),
        "sealed": False,
    }


def _validate_application_note(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"condition", "recommended_form", "context_ids"},
        path=path,
    )
    return {
        "condition": require_string(
            row["condition"], path=f"{path}.condition", maximum=500
        ),
        "recommended_form": require_string(
            row["recommended_form"],
            path=f"{path}.recommended_form",
            maximum=500,
        ),
        "context_ids": _string_list(
            row["context_ids"], path=f"{path}.context_ids", minimum=1
        ),
    }


def _validate_support_set(
    value: Any,
    *,
    expected: Mapping[str, Any],
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=set(expected), path=path)
    positive = _validate_scored_support_contexts(
        row["positive_support_contexts"],
        allowed_labels={"PASS", "MINOR"},
        path=f"{path}.positive_support_contexts",
    )
    negative = _validate_scored_support_contexts(
        row["negative_or_boundary_contexts"],
        allowed_labels={"FAIL"},
        path=f"{path}.negative_or_boundary_contexts",
    )
    contrastive = _validate_contrastive_support_contexts(
        row["contrastive_contexts"],
        path=f"{path}.contrastive_contexts",
    )
    normalized = {
        "positive_support_context_ids": _string_list(
            row["positive_support_context_ids"],
            path=f"{path}.positive_support_context_ids",
            minimum=0,
        ),
        "positive_support_contexts": positive,
        "negative_or_boundary_context_ids": _string_list(
            row["negative_or_boundary_context_ids"],
            path=f"{path}.negative_or_boundary_context_ids",
            minimum=0,
        ),
        "negative_or_boundary_contexts": negative,
        "contrastive_context_ids": _string_list(
            row["contrastive_context_ids"],
            path=f"{path}.contrastive_context_ids",
            minimum=0,
        ),
        "contrastive_contexts": contrastive,
        "materialization_status": require_enum(
            row["materialization_status"],
            {"CONTEXTS_ONLY"},
            path=f"{path}.materialization_status",
        ),
        "embedding_model_version": None,
        "context_centroid_ref": None,
        "ood_policy_version": require_enum(
            row["ood_policy_version"],
            {OOD_POLICY_VERSION},
            path=f"{path}.ood_policy_version",
        ),
        "support_set_version": require_enum(
            row["support_set_version"],
            {SUPPORT_SET_VERSION},
            path=f"{path}.support_set_version",
        ),
        "runtime_tac_ready": require_bool(
            row["runtime_tac_ready"], path=f"{path}.runtime_tac_ready"
        ),
    }
    if (
        row["embedding_model_version"] is not None
        or row["context_centroid_ref"] is not None
        or normalized["runtime_tac_ready"]
    ):
        raise ContractValidationError(
            "support_set",
            path,
            "MVP support set is context-only and not TAC-runtime ready",
        )
    if normalized != expected:
        raise ContractValidationError(
            "support_set",
            path,
            "support set differs from validated contexts",
        )
    return normalized


def _validate_scored_support_contexts(
    value: Any,
    *,
    allowed_labels: set[str],
    path: str,
) -> list[dict[str, Any]]:
    contexts = []
    for index, child in enumerate(require_list(value, path=path)):
        child_path = f"{path}[{index}]"
        item = require_mapping(child, path=child_path)
        require_exact_keys(
            item,
            required={
                "context_id",
                "context_type",
                "raw_score",
                "label",
                "source_sha256",
                "source_provenance",
                "embedding_ref",
            },
            path=child_path,
        )
        if item["embedding_ref"] is not None:
            raise ContractValidationError(
                "support_set",
                f"{child_path}.embedding_ref",
                "MVP must not fabricate embedding references",
            )
        contexts.append(
            {
                "context_id": require_string(
                    item["context_id"], path=f"{child_path}.context_id"
                ),
                "context_type": require_enum(
                    item["context_type"],
                    CONTEXT_TYPES - {"contrastive"},
                    path=f"{child_path}.context_type",
                ),
                "raw_score": require_int(
                    item["raw_score"],
                    path=f"{child_path}.raw_score",
                    minimum=0,
                ),
                "label": require_enum(
                    item["label"], allowed_labels, path=f"{child_path}.label"
                ),
                "source_sha256": require_sha256(
                    item["source_sha256"], path=f"{child_path}.source_sha256"
                ),
                "source_provenance": validate_source_provenance(
                    item["source_provenance"],
                    path=f"{child_path}.source_provenance",
                ),
                "embedding_ref": None,
            }
        )
    return contexts


def _validate_contrastive_support_contexts(
    value: Any, *, path: str
) -> list[dict[str, Any]]:
    contexts = []
    for index, child in enumerate(require_list(value, path=path)):
        child_path = f"{path}[{index}]"
        item = require_mapping(child, path=child_path)
        require_exact_keys(
            item,
            required={
                "context_id",
                "tested_sense_id",
                "result",
                "source_provenance",
                "embedding_ref",
            },
            path=child_path,
        )
        if item["embedding_ref"] is not None:
            raise ContractValidationError(
                "support_set",
                f"{child_path}.embedding_ref",
                "MVP must not fabricate embedding references",
            )
        contexts.append(
            {
                "context_id": require_string(
                    item["context_id"], path=f"{child_path}.context_id"
                ),
                "tested_sense_id": require_string(
                    item["tested_sense_id"],
                    path=f"{child_path}.tested_sense_id",
                ),
                "result": require_enum(
                    item["result"], CONTRASTIVE_RESULTS, path=f"{child_path}.result"
                ),
                "source_provenance": validate_source_provenance(
                    item["source_provenance"],
                    path=f"{child_path}.source_provenance",
                ),
                "embedding_ref": None,
            }
        )
    return contexts


def _validate_judge_independence(
    value: Any,
    *,
    context_results: Sequence[Mapping[str, Any]],
    second_judge_invoked: bool,
    flags: Sequence[str],
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"status", "observations"}, path=path)
    observations = []
    for index, child in enumerate(
        require_list(row["observations"], path=f"{path}.observations")
    ):
        child_path = f"{path}.observations[{index}]"
        item = require_mapping(child, path=child_path)
        require_exact_keys(
            item,
            required={
                "context_id",
                "judge_1_model_id",
                "judge_1_model_family",
                "judge_1_independence_group",
                "judge_1_provider_route_id",
                "judge_2_model_id",
                "judge_2_model_family",
                "judge_2_independence_group",
                "judge_2_provider_route_id",
                "independence_level",
            },
            path=child_path,
        )
        observations.append(
            {
                "context_id": require_string(
                    item["context_id"], path=f"{child_path}.context_id"
                ),
                "judge_1_model_id": require_string(
                    item["judge_1_model_id"],
                    path=f"{child_path}.judge_1_model_id",
                ),
                "judge_1_model_family": require_string(
                    item["judge_1_model_family"],
                    path=f"{child_path}.judge_1_model_family",
                ),
                "judge_1_independence_group": require_string(
                    item["judge_1_independence_group"],
                    path=f"{child_path}.judge_1_independence_group",
                ),
                "judge_1_provider_route_id": require_enum(
                    item["judge_1_provider_route_id"],
                    PROVIDER_ROUTE_IDS,
                    path=f"{child_path}.judge_1_provider_route_id",
                ),
                "judge_2_model_id": require_string(
                    item["judge_2_model_id"],
                    path=f"{child_path}.judge_2_model_id",
                ),
                "judge_2_model_family": require_string(
                    item["judge_2_model_family"],
                    path=f"{child_path}.judge_2_model_family",
                ),
                "judge_2_independence_group": require_string(
                    item["judge_2_independence_group"],
                    path=f"{child_path}.judge_2_independence_group",
                ),
                "judge_2_provider_route_id": require_enum(
                    item["judge_2_provider_route_id"],
                    PROVIDER_ROUTE_IDS,
                    path=f"{child_path}.judge_2_provider_route_id",
                ),
                "independence_level": require_enum(
                    item["independence_level"],
                    {
                        "CROSS_MODEL_FAMILY",
                        "SAME_MODEL_FAMILY",
                    },
                    path=f"{child_path}.independence_level",
                ),
            }
        )
    expected_observations = []
    for result in context_results:
        secondary = result["secondary_judge"]
        if secondary is None:
            continue
        primary_provenance = result["primary_judge"]["provenance"]
        secondary_provenance = secondary["provenance"]
        level = (
            "CROSS_MODEL_FAMILY"
            if primary_provenance["model_family"]
            != secondary_provenance["model_family"]
            else "SAME_MODEL_FAMILY"
        )
        expected_observations.append(
            {
                "context_id": result["context_id"],
                "judge_1_model_id": primary_provenance["model_id"],
                "judge_1_model_family": primary_provenance["model_family"],
                "judge_1_independence_group": primary_provenance[
                    "independence_group"
                ],
                "judge_1_provider_route_id": primary_provenance[
                    "provider_route_id"
                ],
                "judge_2_model_id": secondary_provenance["model_id"],
                "judge_2_model_family": secondary_provenance["model_family"],
                "judge_2_independence_group": secondary_provenance[
                    "independence_group"
                ],
                "judge_2_provider_route_id": secondary_provenance[
                    "provider_route_id"
                ],
                "independence_level": level,
            }
        )
    unavailable = "SECOND_JUDGE_UNAVAILABLE" in flags
    if not second_judge_invoked:
        expected_status = "NOT_INVOKED"
    elif unavailable and not expected_observations:
        expected_status = "REQUESTED_UNAVAILABLE"
    elif unavailable:
        expected_status = "PARTIAL_WITH_UNAVAILABLE_CONTEXTS"
    else:
        levels = {
            item["independence_level"] for item in expected_observations
        }
        if levels == {"CROSS_MODEL_FAMILY"}:
            expected_status = "CROSS_MODEL_FAMILY"
        elif "SAME_MODEL_FAMILY" in levels:
            expected_status = "LOW_JUDGE_INDEPENDENCE"
        else:
            expected_status = "NOT_INVOKED"
    status = require_enum(
        row["status"],
        {
            "NOT_INVOKED",
            "NOT_APPLICABLE_INVALID_SENSE",
            "REQUESTED_UNAVAILABLE",
            "PARTIAL_WITH_UNAVAILABLE_CONTEXTS",
            "CROSS_MODEL_FAMILY",
            "LOW_JUDGE_INDEPENDENCE",
        },
        path=f"{path}.status",
    )
    if status == "NOT_APPLICABLE_INVALID_SENSE":
        if context_results or second_judge_invoked:
            raise ContractValidationError(
                "judge_independence", path, "invalid-sense status is inconsistent"
            )
        expected_status = status
    if observations != expected_observations or status != expected_status:
        raise ContractValidationError(
            "judge_independence",
            path,
            "independence record differs from nested Judge provenance",
        )
    return {"status": status, "observations": observations}


def _expected_candidate_flags(
    *,
    sense_contract: Mapping[str, Any],
    context_results: Sequence[Mapping[str, Any]],
    selected_contrastive_ids: Sequence[str],
    judge_disagreement: bool,
    judge_independence_status: str,
    missing_context_types: Sequence[str],
) -> set[str]:
    flags = {
        flag
        for row in context_results
        for flag in row["local_hard_flags"]
    }
    review_status = sense_contract["definition_review_status"]
    if review_status == "UNVERIFIED":
        flags.add("SENSE_DEFINITION_UNVERIFIED")
    elif review_status == "INVALID":
        flags.add("SENSE_DEFINITION_INVALID")
    if len(context_results) < 3:
        flags.add("INSUFFICIENT_VALID_SAME_SENSE_CONTEXTS")
    if not selected_contrastive_ids:
        flags.add("MISSING_CONTRASTIVE_CONTEXT")
    if missing_context_types:
        flags.add("INCOMPLETE_CONTEXT_TYPE_COVERAGE")
    if judge_disagreement:
        flags.add("JUDGE_DISAGREEMENT")
    if judge_independence_status in {
        "REQUESTED_UNAVAILABLE",
        "PARTIAL_WITH_UNAVAILABLE_CONTEXTS",
    }:
        flags.add("SECOND_JUDGE_UNAVAILABLE")
    return flags


def _validate_candidate_provenance(
    value: Any,
    *,
    candidate: Mapping[str, Any],
    pairwise_records: Sequence[Mapping[str, Any]],
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "cst_evidence_id",
        "evidence_package_sha256",
        "provenance_version",
        "rubric_version",
        "aggregation_policy_version",
        "context_selector_version",
        "trial_translator_version",
        "judge_version",
        "attempted_source_hashes",
        "selector_source_hashes",
        "accepted_source_hashes",
        "excluded_source_hashes",
        "contrastive_source_hashes",
        "model_ids",
        "prompt_hashes_by_role",
        "response_hashes",
        "pairwise_observation_ids",
        "candidate_generation",
        "judge_independence_status",
    }
    require_exact_keys(row, required=required, path=path)
    expected = build_candidate_provenance(candidate, pairwise_records)
    normalized = {
        "cst_evidence_id": require_string(
            row["cst_evidence_id"], path=f"{path}.cst_evidence_id"
        ),
        "evidence_package_sha256": require_sha256(
            row["evidence_package_sha256"],
            path=f"{path}.evidence_package_sha256",
        ),
        "provenance_version": require_enum(
            row["provenance_version"],
            {PROVENANCE_VERSION},
            path=f"{path}.provenance_version",
        ),
        "rubric_version": require_enum(
            row["rubric_version"],
            {RUBRIC_VERSION},
            path=f"{path}.rubric_version",
        ),
        "aggregation_policy_version": require_enum(
            row["aggregation_policy_version"],
            {AGGREGATION_VERSION},
            path=f"{path}.aggregation_policy_version",
        ),
        "context_selector_version": require_enum(
            row["context_selector_version"],
            {SELECTOR_VERSION},
            path=f"{path}.context_selector_version",
        ),
        "trial_translator_version": require_enum(
            row["trial_translator_version"],
            {TRIAL_TRANSLATOR_VERSION},
            path=f"{path}.trial_translator_version",
        ),
        "judge_version": require_enum(
            row["judge_version"],
            {JUDGE_VERSION},
            path=f"{path}.judge_version",
        ),
        "attempted_source_hashes": _sha256_list(
            row["attempted_source_hashes"],
            path=f"{path}.attempted_source_hashes",
        ),
        "selector_source_hashes": _sha256_list(
            row["selector_source_hashes"],
            path=f"{path}.selector_source_hashes",
        ),
        "accepted_source_hashes": _sha256_list(
            row["accepted_source_hashes"],
            path=f"{path}.accepted_source_hashes",
        ),
        "excluded_source_hashes": _sha256_list(
            row["excluded_source_hashes"],
            path=f"{path}.excluded_source_hashes",
        ),
        "contrastive_source_hashes": _sha256_list(
            row["contrastive_source_hashes"],
            path=f"{path}.contrastive_source_hashes",
        ),
        "model_ids": _string_list(
            row["model_ids"], path=f"{path}.model_ids", minimum=0
        ),
        "prompt_hashes_by_role": _validate_prompt_hash_map(
            row["prompt_hashes_by_role"],
            path=f"{path}.prompt_hashes_by_role",
        ),
        "response_hashes": _sha256_list(
            row["response_hashes"], path=f"{path}.response_hashes"
        ),
        "pairwise_observation_ids": _string_list(
            row["pairwise_observation_ids"],
            path=f"{path}.pairwise_observation_ids",
            minimum=0,
        ),
        "candidate_generation": _validate_candidate_generation(
            row["candidate_generation"],
            path=f"{path}.candidate_generation",
        ),
        "judge_independence_status": require_string(
            row["judge_independence_status"],
            path=f"{path}.judge_independence_status",
        ),
    }
    if normalized != expected:
        raise ContractValidationError(
            "candidate_provenance",
            path,
            "candidate provenance differs from nested evidence",
        )
    return normalized


def _validate_pairwise_bindings(
    *,
    candidates: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    close_margin: float,
) -> None:
    expected_pairs = close_candidate_pairs(
        candidates, close_margin=close_margin
    )
    if len(records) != len(expected_pairs):
        raise ContractValidationError(
            "pairwise_cover",
            "$.pairwise_observations",
            "pairwise records must cover every close candidate pair",
        )
    for index, (record, expected) in enumerate(
        zip(records, expected_pairs, strict=True)
    ):
        candidate_a, candidate_b, margin = expected
        expected_context_ids = sorted(
            {row["context_id"] for row in candidate_a["context_results"]}
            & {row["context_id"] for row in candidate_b["context_results"]}
        )[:5]
        if (
            record["term_id"] != candidate_a["term_id"]
            or record["candidate_a_id"] != candidate_a["candidate_id"]
            or record["candidate_b_id"] != candidate_b["candidate_id"]
            or record["score_margin"] != margin
            or record["context_ids"] != expected_context_ids
        ):
            raise ContractValidationError(
                "pairwise_binding",
                f"$.pairwise_observations[{index}]",
                "pairwise record differs from close-candidate evidence",
            )
        if len(expected_context_ids) < 3:
            if (
                record["status"] != "UNAVAILABLE"
                or record["failure_reason"]
                != "insufficient_common_same_sense_contexts"
            ):
                raise ContractValidationError(
                    "pairwise_status",
                    f"$.pairwise_observations[{index}]",
                    "insufficient common contexts must be unavailable",
                )
        elif record["status"] == "COMPLETED":
            provenance = _validate_provider_provenance(
                record["provenance"],
                path=f"$.pairwise_observations[{index}].provenance",
            )
            if (
                provenance["role"] != "pairwise_tiebreaker"
                or provenance["prompt_version"] != PAIRWISE_VERSION
            ):
                raise ContractValidationError(
                    "pairwise_provenance",
                    f"$.pairwise_observations[{index}].provenance",
                    "role or version mismatch",
                )
            record["provenance"] = provenance


def _validate_pairwise_flags(
    *,
    candidates: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> None:
    unavailable_ids = {
        candidate_id
        for row in records
        if row["status"] == "UNAVAILABLE"
        for candidate_id in (row["candidate_a_id"], row["candidate_b_id"])
    }
    for index, candidate in enumerate(candidates):
        expected = candidate["candidate_id"] in unavailable_ids
        actual = _PAIRWISE_FLAG in candidate["context_flags"]
        if actual != expected:
            raise ContractValidationError(
                "pairwise_flag",
                f"$.candidates[{index}].context_flags",
                "pairwise availability flag mismatch",
            )


def _validate_nested_provider_bindings(
    *,
    attempts: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    pairwise_records: Sequence[Mapping[str, Any]],
) -> None:
    accepted_fingerprints = {
        _provenance_fingerprint(row)
        for row in attempts
        if row["accepted"] is True
    }
    nested = [
        provenance
        for candidate in candidates
        for provenance in candidate_provider_provenances(candidate)
    ]
    nested.extend(
        row["provenance"]
        for row in pairwise_records
        if row["status"] == "COMPLETED"
    )
    nested_fingerprints = {
        _provenance_fingerprint(row) for row in nested
    }
    foreign = nested_fingerprints - accepted_fingerprints
    missing = accepted_fingerprints - nested_fingerprints
    if foreign or missing:
        raise ContractValidationError(
            "provider_binding",
            "$.provider_attempts",
            "accepted provider attempts and nested provenance differ",
        )


def _validate_usage(
    value: Any,
    *,
    attempts: Sequence[Mapping[str, Any]],
    route_order: Sequence[str],
    path: str,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "attempt_count",
        "accepted_count",
        "rejected_count",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "by_route",
    }
    require_exact_keys(row, required=required, path=path)
    by_route_row = require_mapping(row["by_route"], path=f"{path}.by_route")
    require_exact_keys(
        by_route_row, required=set(route_order), path=f"{path}.by_route"
    )
    by_route = {
        route_id: _validate_usage_counts(
            by_route_row[route_id],
            path=f"{path}.by_route.{route_id}",
        )
        for route_id in route_order
    }
    normalized = {
        **_validate_usage_counts(
            {key: row[key] for key in required if key != "by_route"},
            path=path,
        ),
        "by_route": by_route,
    }
    expected = _usage_summary(attempts, route_order=route_order)
    if normalized != expected:
        raise ContractValidationError(
            "usage", path, "usage differs from provider attempt ledger"
        )
    return normalized


def _validate_usage_counts(value: Any, *, path: str) -> dict[str, int]:
    row = require_mapping(value, path=path)
    required = {
        "attempt_count",
        "accepted_count",
        "rejected_count",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
    require_exact_keys(row, required=required, path=path)
    result = {
        key: require_int(row[key], path=f"{path}.{key}", minimum=0)
        for key in required
    }
    if result["attempt_count"] != (
        result["accepted_count"] + result["rejected_count"]
    ):
        raise ContractValidationError(
            "usage_count", path, "attempt count must equal accepted + rejected"
        )
    if result["total_tokens"] != (
        result["input_tokens"] + result["output_tokens"]
    ):
        raise ContractValidationError(
            "usage_total", path, "total tokens must equal input + output"
        )
    if result["reasoning_tokens"] > result["output_tokens"]:
        raise ContractValidationError(
            "usage_reasoning", path, "reasoning must be included in output"
        )
    return result


def _usage_summary(
    attempts: Sequence[Mapping[str, Any]],
    *,
    route_order: Sequence[str],
) -> dict[str, Any]:
    def counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        return {
            "attempt_count": len(rows),
            "accepted_count": sum(row["accepted"] is True for row in rows),
            "rejected_count": sum(row["accepted"] is False for row in rows),
            "input_tokens": sum(int(row["input_tokens"]) for row in rows),
            "output_tokens": sum(int(row["output_tokens"]) for row in rows),
            "reasoning_tokens": sum(
                int(row["reasoning_tokens"]) for row in rows
            ),
            "total_tokens": sum(int(row["total_tokens"]) for row in rows),
        }

    return {
        **counts(attempts),
        "by_route": {
            route_id: counts(
                [
                    row
                    for row in attempts
                    if row["provider_route_id"] == route_id
                ]
            )
            for route_id in route_order
        },
    }


def _validate_prompt_hash_map(
    value: Any, *, path: str
) -> dict[str, list[str]]:
    row = require_mapping(value, path=path)
    result = {}
    for role, hashes in row.items():
        normalized_role = require_enum(role, PROVIDER_ROLES, path=path)
        values = [
            require_sha256(
                child, path=f"{path}.{normalized_role}[{index}]"
            )
            for index, child in enumerate(
                require_list(hashes, path=f"{path}.{normalized_role}")
            )
        ]
        require_unique(values, path=f"{path}.{normalized_role}")
        result[normalized_role] = values
    return {key: result[key] for key in sorted(result)}


def _provenance_fingerprint(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        value[key]
        for key in (
            "provider_route_id",
            "model_id",
            "model_family",
            "independence_group",
            "role",
            "prompt_version",
            "prompt_sha256",
            "response_sha256",
            "request_id",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cached",
            "latency_ms",
        )
    )


def _string_list(
    value: Any, *, path: str, minimum: int
) -> list[str]:
    rows = [
        require_string(child, path=f"{path}[{index}]")
        for index, child in enumerate(require_list(value, path=path))
    ]
    if len(rows) < minimum:
        raise ContractValidationError(
            "length", path, f"expected at least {minimum} items"
        )
    require_unique(rows, path=path)
    return rows


def _sha256_list(value: Any, *, path: str) -> list[str]:
    rows = [
        require_sha256(child, path=f"{path}[{index}]")
        for index, child in enumerate(require_list(value, path=path))
    ]
    require_unique(rows, path=path)
    return rows


def _enum_list(
    value: Any,
    allowed: Iterable[str],
    *,
    path: str,
) -> list[str]:
    rows = [
        require_enum(child, allowed, path=f"{path}[{index}]")
        for index, child in enumerate(require_list(value, path=path))
    ]
    require_unique(rows, path=path)
    return rows


def _nullable_int(
    value: Any,
    *,
    path: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    result = require_int(value, path=path, minimum=minimum)
    if result > maximum:
        raise ContractValidationError(
            "range", path, f"must be <= {maximum}"
        )
    return result


def _nullable_number(
    value: Any,
    *,
    path: str,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    result = require_number(value, path=path, minimum=minimum)
    if result > maximum:
        raise ContractValidationError(
            "range", path, f"must be <= {maximum}"
        )
    return result
