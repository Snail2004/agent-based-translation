from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_mapping,
    require_sha256,
    require_string,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.workflow_component_v1 import (
    ARM_IDS_V1,
    validate_scoring_handoff_v1,
    validate_typed_artifact_binding_v1,
)


__all__ = [
    "EVALUATION_CHAPTER_IDS_V1",
    "EVALUATION_SCORER_IDS_V1",
    "EvaluationWorkflowSettingsAuthorityV1",
    "build_evaluation_workflow_settings_v1",
    "validate_evaluation_workflow_settings_v1",
]


SCHEMA_ID = "EvaluationWorkflowSettingsV1"
SCHEMA_VERSION = "1.1.0"
EVALUATION_CHAPTER_IDS_V1 = (
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
    "d2l_deep_learning_computation",
    "d2l_convolutional_neural_networks",
)
EVALUATION_SCORER_IDS_V1 = ("sf_qe", "sf_bt", "pj")
_HASH_PATH = ("settings_sha256",)
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("selected_chapter_ids",),
            ("selected_arm_ids",),
            ("selected_scorer_ids",),
        }
    ),
)
_FORBIDDEN_RUNTIME_AUTHORITY_TOKENS = (
    "gold",
    "oracle",
    "human_reference",
    "result_callback",
    "evaluation_result",
)


@dataclass(frozen=True, slots=True)
class EvaluationWorkflowSettingsAuthorityV1:
    """Server-owned catalog facts from which a run may select.

    Callers choose only registered artifact references and an optional arm pair.
    The authority itself is not accepted from a modal payload.
    """

    benchmark_preset: Mapping[str, Any]
    evaluation_config: Mapping[str, Any]
    scorer_set: Mapping[str, Any]
    evaluation_profiles: Sequence[Mapping[str, Any]]
    policy_profiles: Sequence[Mapping[str, Any]]
    shared_selections: Sequence[Mapping[str, Any]]
    chapter_ids: Sequence[str] = EVALUATION_CHAPTER_IDS_V1
    arm_ids: Sequence[str] = ARM_IDS_V1
    scorer_ids: Sequence[str] = EVALUATION_SCORER_IDS_V1
    aggregation_policy_id: str = "method_specific_only"
    report_policy_id: str = "full_run_report_v1"
    verdict_policy_id: str = "no_cross_method_composite"


def build_evaluation_workflow_settings_v1(
    *,
    authority: EvaluationWorkflowSettingsAuthorityV1,
    scoring_handoff: Mapping[str, Any],
    evaluation_profile_ref: str,
    policy_profile_ref: str | None,
    shared_selection_ref: str,
    highlight_pair: Mapping[str, Any] | None,
    selected_chapter_ids: Sequence[str] | None = None,
    selected_arm_ids: Sequence[str] | None = None,
    selected_scorer_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    catalog = _normalize_authority(authority)
    handoff = validate_scoring_handoff_v1(scoring_handoff)
    profile = _resolve_registered_binding(
        catalog["evaluation_profiles"],
        evaluation_profile_ref,
        path="$.evaluation_profile_ref",
    )
    policy = (
        None
        if policy_profile_ref is None
        else _resolve_registered_binding(
            catalog["policy_profiles"],
            policy_profile_ref,
            path="$.policy_profile_ref",
        )
    )
    selection = _resolve_registered_binding(
        catalog["shared_selections"],
        shared_selection_ref,
        path="$.shared_selection_ref",
    )
    chapters = _validate_ordered_selection(
        catalog["chapter_ids"] if selected_chapter_ids is None else selected_chapter_ids,
        allowed=catalog["chapter_ids"],
        minimum=1,
        path="$.selected_chapter_ids",
    )
    arms = _validate_ordered_selection(
        catalog["arm_ids"] if selected_arm_ids is None else selected_arm_ids,
        allowed=catalog["arm_ids"],
        minimum=2,
        path="$.selected_arm_ids",
    )
    scorers = _validate_ordered_selection(
        catalog["scorer_ids"] if selected_scorer_ids is None else selected_scorer_ids,
        allowed=catalog["scorer_ids"],
        minimum=1,
        path="$.selected_scorer_ids",
    )
    draft = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "benchmark_preset_ref": catalog["benchmark_preset"],
        "evaluation_config_ref": catalog["evaluation_config"],
        "evaluation_profile_ref": profile,
        "policy_profile_ref": policy,
        "scorer_set_ref": catalog["scorer_set"],
        "scoring_handoff": {
            "artifact_ref": "handoffs/scoring_handoff.json",
            "artifact_kind": "scoring_handoff_v1",
            "schema_version": handoff["schema_version"],
            "sha256": handoff["integrity"]["handoff_sha256"],
            "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
            "input_set_sha256": handoff["input_set_sha256"],
        },
        "selected_chapter_ids": list(chapters),
        "selected_arm_ids": list(arms),
        "selected_scorer_ids": list(scorers),
        "highlight_pair": _validate_highlight_pair(
            highlight_pair, arm_ids=arms, path="$.highlight_pair"
        ),
        "shared_selection_ref": selection,
        "settings_sha256": "0" * 64,
    }
    return validate_evaluation_workflow_settings_v1(
        seal_payload(draft, policy=_POLICY, hash_path=_HASH_PATH),
        authority=authority,
        scoring_handoff=handoff,
    )


def validate_evaluation_workflow_settings_v1(
    value: Mapping[str, Any],
    *,
    authority: EvaluationWorkflowSettingsAuthorityV1 | None = None,
    scoring_handoff: Mapping[str, Any],
) -> dict[str, Any]:
    catalog = None if authority is None else _normalize_authority(authority)
    handoff = validate_scoring_handoff_v1(scoring_handoff)
    row = require_mapping(value, path="$settings")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "benchmark_preset_ref",
            "evaluation_config_ref",
            "evaluation_profile_ref",
            "policy_profile_ref",
            "scorer_set_ref",
            "scoring_handoff",
            "selected_chapter_ids",
            "selected_arm_ids",
            "selected_scorer_ids",
            "highlight_pair",
            "shared_selection_ref",
            "settings_sha256",
        },
        path="$settings",
    )
    selected_chapter_ids = _validate_ordered_selection(
        row["selected_chapter_ids"],
        allowed=(
            EVALUATION_CHAPTER_IDS_V1 if catalog is None else catalog["chapter_ids"]
        ),
        minimum=1,
        path="$settings.selected_chapter_ids",
    )
    selected_arm_ids = _validate_ordered_selection(
        row["selected_arm_ids"],
        allowed=ARM_IDS_V1 if catalog is None else catalog["arm_ids"],
        minimum=2,
        path="$settings.selected_arm_ids",
    )
    selected_scorer_ids = _validate_ordered_selection(
        row["selected_scorer_ids"],
        allowed=(
            EVALUATION_SCORER_IDS_V1 if catalog is None else catalog["scorer_ids"]
        ),
        minimum=1,
        path="$settings.selected_scorer_ids",
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {SCHEMA_ID}, path="$settings.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {SCHEMA_VERSION},
            path="$settings.schema_version",
        ),
        "benchmark_preset_ref": _validate_authority_binding(
            row["benchmark_preset_ref"], path="$settings.benchmark_preset_ref"
        ),
        "evaluation_config_ref": _validate_authority_binding(
            row["evaluation_config_ref"], path="$settings.evaluation_config_ref"
        ),
        "evaluation_profile_ref": _validate_authority_binding(
            row["evaluation_profile_ref"], path="$settings.evaluation_profile_ref"
        ),
        "policy_profile_ref": (
            None
            if row["policy_profile_ref"] is None
            else _validate_authority_binding(
                row["policy_profile_ref"], path="$settings.policy_profile_ref"
            )
        ),
        "scorer_set_ref": _validate_authority_binding(
            row["scorer_set_ref"], path="$settings.scorer_set_ref"
        ),
        "scoring_handoff": _validate_handoff_binding(
            row["scoring_handoff"], path="$settings.scoring_handoff"
        ),
        "selected_chapter_ids": list(selected_chapter_ids),
        "selected_arm_ids": list(selected_arm_ids),
        "selected_scorer_ids": list(selected_scorer_ids),
        "highlight_pair": _validate_highlight_pair(
            row["highlight_pair"],
            arm_ids=selected_arm_ids,
            path="$settings.highlight_pair",
        ),
        "shared_selection_ref": _validate_authority_binding(
            row["shared_selection_ref"], path="$settings.shared_selection_ref"
        ),
        "settings_sha256": require_sha256(
            row["settings_sha256"], path="$settings.settings_sha256"
        ),
    }
    if catalog is not None:
        _require_exact_binding(
            normalized["benchmark_preset_ref"],
            catalog["benchmark_preset"],
            path="$settings.benchmark_preset_ref",
        )
        _require_exact_binding(
            normalized["evaluation_config_ref"],
            catalog["evaluation_config"],
            path="$settings.evaluation_config_ref",
        )
        _require_exact_binding(
            normalized["scorer_set_ref"],
            catalog["scorer_set"],
            path="$settings.scorer_set_ref",
        )
        _require_registered_binding(
            normalized["evaluation_profile_ref"],
            catalog["evaluation_profiles"],
            path="$settings.evaluation_profile_ref",
        )
        if normalized["policy_profile_ref"] is not None:
            _require_registered_binding(
                normalized["policy_profile_ref"],
                catalog["policy_profiles"],
                path="$settings.policy_profile_ref",
            )
        _require_registered_binding(
            normalized["shared_selection_ref"],
            catalog["shared_selections"],
            path="$settings.shared_selection_ref",
        )
    expected_handoff = {
        "artifact_ref": "handoffs/scoring_handoff.json",
        "artifact_kind": "scoring_handoff_v1",
        "schema_version": handoff["schema_version"],
        "sha256": handoff["integrity"]["handoff_sha256"],
        "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
        "input_set_sha256": handoff["input_set_sha256"],
    }
    if normalized["scoring_handoff"] != expected_handoff:
        raise ContractValidationError(
            "handoff_binding",
            "$settings.scoring_handoff",
            "settings must bind the exact parent-owned scoring handoff",
        )
    if not verify_payload_hash(
        normalized, policy=_POLICY, hash_path=_HASH_PATH
    ):
        raise ContractValidationError(
            "settings_hash",
            "$settings.settings_sha256",
            "settings hash drift",
        )
    result = canonicalize(normalized, policy=_POLICY)
    assert isinstance(result, dict)
    return result


def _normalize_authority(
    authority: EvaluationWorkflowSettingsAuthorityV1,
) -> dict[str, Any]:
    if not isinstance(authority, EvaluationWorkflowSettingsAuthorityV1):
        raise ContractValidationError(
            "settings_authority",
            "$authority",
            "settings authority must be server-owned EvaluationWorkflowSettingsAuthorityV1",
        )
    chapters = tuple(
        require_string(item, path="$.authority.chapter_ids[*]")
        for item in authority.chapter_ids
    )
    arms = tuple(
        require_string(item, path="$.authority.arm_ids[*]") for item in authority.arm_ids
    )
    scorers = tuple(
        require_string(item, path="$.authority.scorer_ids[*]")
        for item in authority.scorer_ids
    )
    if chapters != EVALUATION_CHAPTER_IDS_V1:
        raise ContractValidationError(
            "chapter_selection",
            "$authority.chapter_ids",
            "the narrow Evaluation preset has an exact five-chapter universe",
        )
    if arms != ARM_IDS_V1:
        raise ContractValidationError(
            "arm_order",
            "$authority.arm_ids",
            "Evaluation requires the exact five-arm order",
        )
    if scorers != EVALUATION_SCORER_IDS_V1:
        raise ContractValidationError(
            "scorer_set",
            "$authority.scorer_ids",
            "Evaluation requires the registered SF-QE/SF-BT/PJ scorer set",
        )
    fixed_policies = {
        "aggregation_policy_id": "method_specific_only",
        "report_policy_id": "full_run_report_v1",
        "verdict_policy_id": "no_cross_method_composite",
    }
    for field, expected in fixed_policies.items():
        observed = require_string(getattr(authority, field), path=f"$authority.{field}")
        if observed != expected:
            raise ContractValidationError(
                "fixed_policy",
                f"$authority.{field}",
                f"expected server-owned policy {expected}",
            )
    result = {
        "benchmark_preset": _validate_authority_binding(
            authority.benchmark_preset, path="$authority.benchmark_preset"
        ),
        "evaluation_config": _validate_authority_binding(
            authority.evaluation_config, path="$authority.evaluation_config"
        ),
        "scorer_set": _validate_authority_binding(
            authority.scorer_set, path="$authority.scorer_set"
        ),
        "evaluation_profiles": _validate_binding_catalog(
            authority.evaluation_profiles, path="$authority.evaluation_profiles"
        ),
        "policy_profiles": _validate_binding_catalog(
            authority.policy_profiles, path="$authority.policy_profiles"
        ),
        "shared_selections": _validate_binding_catalog(
            authority.shared_selections, path="$authority.shared_selections"
        ),
        "chapter_ids": chapters,
        "arm_ids": arms,
        "scorer_ids": scorers,
        **fixed_policies,
    }
    if not result["evaluation_profiles"] or not result["shared_selections"]:
        raise ContractValidationError(
            "settings_catalog",
            "$authority",
            "at least one Evaluation profile and shared selection must be registered",
        )
    return result


def _validate_binding_catalog(
    values: Sequence[Mapping[str, Any]], *, path: str
) -> tuple[dict[str, str], ...]:
    rows = tuple(
        _validate_authority_binding(item, path=f"{path}[{index}]")
        for index, item in enumerate(values)
    )
    refs = [row["artifact_ref"] for row in rows]
    if len(refs) != len(set(refs)):
        raise ContractValidationError(
            "settings_catalog", path, "registered artifact references must be unique"
        )
    return rows


def _validate_authority_binding(value: Any, *, path: str) -> dict[str, str]:
    binding = validate_typed_artifact_binding_v1(value, path=path)
    authority_text = (
        f"{binding['artifact_ref']} {binding['artifact_kind']}".lower()
    )
    if any(token in authority_text for token in _FORBIDDEN_RUNTIME_AUTHORITY_TOKENS):
        raise ContractValidationError(
            "forbidden_runtime_authority",
            path,
            "gold/oracle/reference/result authority cannot enter Evaluation runtime settings",
        )
    return binding


def _resolve_registered_binding(
    catalog: Sequence[Mapping[str, str]], artifact_ref: str, *, path: str
) -> dict[str, str]:
    requested = require_string(artifact_ref, path=path)
    matches = [row for row in catalog if row["artifact_ref"] == requested]
    if len(matches) != 1:
        raise ContractValidationError(
            "settings_selection",
            path,
            "selection must resolve to exactly one server-registered artifact",
        )
    return copy.deepcopy(dict(matches[0]))


def _require_registered_binding(
    value: Mapping[str, str],
    catalog: Sequence[Mapping[str, str]],
    *,
    path: str,
) -> None:
    if value not in catalog:
        raise ContractValidationError(
            "settings_selection",
            path,
            "binding is not present in the server-owned settings catalog",
        )


def _require_exact_binding(
    value: Mapping[str, str], expected: Mapping[str, str], *, path: str
) -> None:
    if value != expected:
        raise ContractValidationError(
            "settings_binding", path, "fixed server-owned binding changed"
        )


def _validate_handoff_binding(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "artifact_ref",
            "artifact_kind",
            "schema_version",
            "sha256",
            "sha256_kind",
            "input_set_sha256",
        },
        path=path,
    )
    binding = validate_typed_artifact_binding_v1(
        {key: row[key] for key in row if key != "input_set_sha256"},
        path=path,
    )
    return {
        **binding,
        "input_set_sha256": require_sha256(
            row["input_set_sha256"], path=f"{path}.input_set_sha256"
        ),
    }


def _validate_highlight_pair(
    value: Any, *, arm_ids: Sequence[str], path: str
) -> dict[str, str] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"baseline_arm_id", "candidate_arm_id"}, path=path
    )
    allowed = set(arm_ids)
    baseline = require_enum(
        row["baseline_arm_id"], allowed, path=f"{path}.baseline_arm_id"
    )
    candidate = require_enum(
        row["candidate_arm_id"], allowed, path=f"{path}.candidate_arm_id"
    )
    if baseline == candidate:
        raise ContractValidationError(
            "highlight_pair", path, "highlight arms must be distinct"
        )
    return {"baseline_arm_id": baseline, "candidate_arm_id": candidate}


def _validate_ordered_selection(
    value: Sequence[Any],
    *,
    allowed: Sequence[str],
    minimum: int,
    path: str,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(
            "type_error", path, "selection must be an ordered array"
        )
    rows = tuple(
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(value)
    )
    if len(rows) < minimum:
        raise ContractValidationError(
            "selection_size", path, f"selection requires at least {minimum} item(s)"
        )
    if len(rows) != len(set(rows)):
        raise ContractValidationError(
            "selection_duplicate", path, "selection items must be unique"
        )
    allowed_order = tuple(allowed)
    positions = {item: index for index, item in enumerate(allowed_order)}
    if any(item not in positions for item in rows):
        raise ContractValidationError(
            "settings_selection", path, "selection contains an unregistered item"
        )
    if tuple(sorted(rows, key=positions.__getitem__)) != rows:
        raise ContractValidationError(
            "selection_order", path, "selection must preserve server-owned order"
        )
    return rows
