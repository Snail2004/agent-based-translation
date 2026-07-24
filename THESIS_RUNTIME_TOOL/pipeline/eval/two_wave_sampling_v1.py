from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import itertools
import json
import math
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import (
    CommonBlockV1,
    CommonEvaluationInputV1,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_number,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.workflow_component_v1 import validate_typed_artifact_binding_v1


__all__ = [
    "ARM_DISPLAY_NAMES_V1",
    "METHOD_DISPLAY_NAMES_V1",
    "TWO_WAVE_CHAPTER_IDS_V1",
    "TWO_WAVE_CLUSTER_SIZE_V1",
    "TWO_WAVE_WAVE_A_QUOTAS_V1",
    "TWO_WAVE_WAVE_B_QUOTAS_V1",
    "build_two_wave_sampling_manifest_v1",
    "build_two_wave_method_stage_payload_v1",
    "build_two_wave_uncertainty_decision_v1",
    "build_two_wave_work_plan_v1",
    "two_wave_component_stages_v1",
    "two_wave_workflow_stages_v1",
    "validate_two_wave_sampling_manifest_v1",
    "validate_two_wave_method_stage_payload_v1",
    "validate_two_wave_uncertainty_decision_v1",
    "validate_two_wave_work_plan_v1",
]


SAMPLING_SCHEMA_ID = "EvaluationTwoWaveSamplingManifestV1"
SAMPLING_SCHEMA_VERSION = "1.0.0"
UNCERTAINTY_SCHEMA_ID = "EvaluationTwoWaveUncertaintyDecisionV1"
UNCERTAINTY_SCHEMA_VERSION = "1.0.0"
WORK_PLAN_SCHEMA_ID = "EvaluationTwoWaveWorkPlanV1"
WORK_PLAN_SCHEMA_VERSION = "1.0.0"
METHOD_STAGE_SCHEMA_ID = "EvaluationTwoWaveMethodStagePayloadV1"
METHOD_STAGE_SCHEMA_VERSION = "1.0.0"

TWO_WAVE_CHAPTER_IDS_V1 = (
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
    "d2l_deep_learning_computation",
    "d2l_convolutional_neural_networks",
)
TWO_WAVE_CLUSTER_SIZE_V1 = 5
TWO_WAVE_WAVE_A_QUOTAS_V1 = {
    "d2l_preliminaries": 11,
    "d2l_linear_networks": 11,
    "d2l_multilayer_perceptrons": 15,
    "d2l_deep_learning_computation": 6,
    "d2l_convolutional_neural_networks": 7,
}
TWO_WAVE_WAVE_B_QUOTAS_V1 = {
    "d2l_preliminaries": 22,
    "d2l_linear_networks": 21,
    "d2l_multilayer_perceptrons": 30,
    "d2l_deep_learning_computation": 13,
    "d2l_convolutional_neural_networks": 14,
}

BENCHMARK_ARM_IDS_V1 = ("S0", "S1", "community", "google_nmt", "llm_lc")
ARM_DISPLAY_NAMES_V1 = {
    "S0": "ABT-Base",
    "S1": "ABT-Context",
    "community": "D2L-Community",
    "google_nmt": "Google-Translate",
    "llm_lc": "LLM-BookContext",
}
METHOD_DISPLAY_NAMES_V1 = {
    "sf_qe": "DTQ",
    "sf_bt": "BTF",
    "pj": "MTQ-5",
    "tc_occ": "TC-Occ",
    "ta_occ": "TA-Occ",
}

_SAMPLING_HASH_PATH = ("integrity", "manifest_sha256")
_UNCERTAINTY_HASH_PATH = ("integrity", "decision_sha256")
_WORK_PLAN_HASH_PATH = ("integrity", "work_plan_sha256")
_SAMPLING_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("identity", "selected_chapter_ids"),
            ("population", "eligible_block_ids"),
            ("clusters",),
            ("clusters", "*", "block_ids"),
            ("clusters", "*", "order_indexes"),
            ("waves", "wave_a", "cluster_ids"),
            ("waves", "wave_a", "block_ids"),
            ("waves", "wave_b", "cluster_ids"),
            ("waves", "wave_b", "block_ids"),
            ("method_policy", "full_universe_method_ids"),
            ("method_policy", "sampled_method_ids"),
            ("method_policy", "pair_orientations"),
        }
    ),
)
_UNCERTAINTY_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("coverage_bindings",),
            ("method_artifact_bindings",),
            ("pair_evidence",),
            ("pair_evidence", "*", "methods"),
            ("reasons",),
        }
    ),
)
_WORK_PLAN_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("selected_chapter_ids",),
            ("selected_arm_ids",),
            ("active_cluster_ids",),
            ("active_block_ids",),
            ("incremental_cluster_ids",),
            ("incremental_block_ids",),
            ("pair_ids",),
            ("stages",),
        }
    ),
)

_LENGTH_BANDS = (
    (1000, "short"),
    (2500, "medium"),
    (10**18, "long"),
)
_TERM_DENSITY_BANDS = (
    (0.0, "zero"),
    (5.0, "low"),
    (15.0, "medium"),
    (float("inf"), "high"),
)
_METHOD_STAGE_SPECS = {
    "btf_wave_a": ("sf_bt", "wave_a", False),
    "mtq5_wave_a": ("pj", "wave_a", False),
    "btf_wave_b": ("sf_bt", "wave_b", True),
    "mtq5_wave_b": ("pj", "wave_b", True),
}
_STAGE_ARTIFACT_SCHEMA_ID = "EvaluationTwoWaveStageArtifactV1"
_STAGE_ARTIFACT_SCHEMA_VERSION = "1.0.0"


def build_two_wave_sampling_manifest_v1(
    chapter_inputs: Mapping[str, CommonEvaluationInputV1],
    *,
    seed: str,
    created_at: str,
    producer_code_commit: str,
    term_occurrence_counts: Mapping[str, int] | None = None,
    term_feature_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_inputs = _validate_chapter_inputs(chapter_inputs)
    normalized_seed = require_string(seed, path="$.seed", maximum=200)
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    eligible_blocks = tuple(
        block
        for chapter_id in TWO_WAVE_CHAPTER_IDS_V1
        for block in normalized_inputs[chapter_id].blocks
        if block.admission == "translate"
    )
    term_counts, feature_row = _normalize_term_features(
        eligible_blocks,
        term_occurrence_counts=term_occurrence_counts,
        term_feature_binding=term_feature_binding,
    )

    clusters: list[dict[str, Any]] = []
    chapter_source_hashes: dict[str, str] = {}
    partition_offsets: dict[str, int] = {}
    for chapter_id in TWO_WAVE_CHAPTER_IDS_V1:
        common_input = normalized_inputs[chapter_id]
        chapter_blocks = tuple(
            block for block in common_input.blocks if block.admission == "translate"
        )
        chapter_source_hashes[chapter_id] = _stable_sha256(
            source_binding_to_dict(common_input.source_binding)
        )
        quota_b = TWO_WAVE_WAVE_B_QUOTAS_V1[chapter_id]
        offset = _select_partition_offset(
            chapter_blocks,
            seed=normalized_seed,
            chapter_id=chapter_id,
            required_cluster_count=quota_b,
        )
        partition_offsets[chapter_id] = offset
        candidates = _partition_candidates(
            chapter_blocks,
            offset=offset,
            seed=normalized_seed,
            term_counts=term_counts,
        )
        selected = _stratified_order(
            candidates,
            seed=normalized_seed,
            chapter_id=chapter_id,
        )[:quota_b]
        if len(selected) != quota_b:
            raise ContractValidationError(
                "sampling_capacity",
                f"$.chapter_inputs.{chapter_id}",
                f"chapter requires {quota_b} non-overlapping clusters but only "
                f"{len(selected)} are available",
            )
        for ordinal, candidate in enumerate(selected):
            clusters.append(
                {
                    **candidate,
                    "chapter_cluster_ordinal": ordinal,
                }
            )

    wave_a = _wave_scope(
        clusters,
        wave_id="wave_a",
        quotas=TWO_WAVE_WAVE_A_QUOTAS_V1,
    )
    wave_b = _wave_scope(
        clusters,
        wave_id="wave_b",
        quotas=TWO_WAVE_WAVE_B_QUOTAS_V1,
        prefix_cluster_ids=wave_a["cluster_ids"],
    )
    draft = {
        "schema_id": SAMPLING_SCHEMA_ID,
        "schema_version": SAMPLING_SCHEMA_VERSION,
        "created_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "two_wave_sampling_v1",
            "component_version": SAMPLING_SCHEMA_VERSION,
            "code_commit": commit,
        },
        "identity": {
            "sampling_policy_id": "d2l_five_chapter_two_wave_v1",
            "sampling_policy_version": "1.0.0",
            "seed": normalized_seed,
            "cluster_size": TWO_WAVE_CLUSTER_SIZE_V1,
            "selected_chapter_ids": list(TWO_WAVE_CHAPTER_IDS_V1),
        },
        "source_bindings": chapter_source_hashes,
        "source_features": feature_row,
        "population": {
            "eligible_block_count": len(eligible_blocks),
            "eligible_block_ids": [block.block_id for block in eligible_blocks],
            "chapter_eligible_counts": {
                chapter_id: sum(
                    block.admission == "translate"
                    for block in normalized_inputs[chapter_id].blocks
                )
                for chapter_id in TWO_WAVE_CHAPTER_IDS_V1
            },
            "chapter_partition_offsets": partition_offsets,
        },
        "clusters": clusters,
        "waves": {"wave_a": wave_a, "wave_b": wave_b},
        "method_policy": {
            "full_universe_method_ids": ["sf_qe", "tc_occ", "ta_occ"],
            "sampled_method_ids": ["sf_bt", "pj"],
            "pair_policy": "all_unordered_pairs",
            "pair_orientations": ["canonical", "reversed"],
            "missing_arm_policy": "hold_comparison_no_replacement",
            "wave_b_open_policy": (
                "paired_ci95_includes_zero_or_btf_mtq5_direction_disagrees"
            ),
            "terminal_uncertainty_policy": "inconclusive_after_wave_b",
        },
        "integrity": {"manifest_sha256": "0" * 64},
    }
    sealed = seal_payload(
        draft,
        policy=_SAMPLING_POLICY,
        hash_path=_SAMPLING_HASH_PATH,
    )
    return validate_two_wave_sampling_manifest_v1(sealed)


def validate_two_wave_sampling_manifest_v1(
    value: Mapping[str, Any],
    *,
    chapter_inputs: Mapping[str, CommonEvaluationInputV1] | None = None,
    term_occurrence_counts: Mapping[str, int] | None = None,
    term_feature_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = require_mapping(value, path="$sampling")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "identity",
            "source_bindings",
            "source_features",
            "population",
            "clusters",
            "waves",
            "method_policy",
            "integrity",
        },
        path="$sampling",
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {SAMPLING_SCHEMA_ID}, path="$sampling.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {SAMPLING_SCHEMA_VERSION},
            path="$sampling.schema_version",
        ),
        "created_at": require_rfc3339(
            row["created_at"], path="$sampling.created_at"
        ),
        "producer": _validate_producer(row["producer"], path="$sampling.producer"),
        "identity": _validate_sampling_identity(row["identity"]),
        "source_bindings": _validate_source_binding_hashes(row["source_bindings"]),
        "source_features": _validate_source_features(row["source_features"]),
        "population": _validate_population(row["population"]),
        "clusters": _validate_clusters(row["clusters"]),
        "waves": _validate_waves(row["waves"]),
        "method_policy": _validate_method_policy(row["method_policy"]),
        "integrity": _one_hash(
            row["integrity"], "manifest_sha256", "$sampling.integrity"
        ),
    }
    _validate_sampling_references(normalized)
    if not verify_payload_hash(
        normalized,
        policy=_SAMPLING_POLICY,
        hash_path=_SAMPLING_HASH_PATH,
    ):
        raise ContractValidationError(
            "manifest_hash",
            "$sampling.integrity.manifest_sha256",
            "sampling manifest self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_SAMPLING_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("sampling manifest must remain an object")
    if chapter_inputs is not None:
        rebuilt = build_two_wave_sampling_manifest_v1(
            chapter_inputs,
            seed=canonical["identity"]["seed"],
            created_at=canonical["created_at"],
            producer_code_commit=canonical["producer"]["code_commit"],
            term_occurrence_counts=term_occurrence_counts,
            term_feature_binding=term_feature_binding,
        )
        if rebuilt != canonical:
            raise ContractValidationError(
                "sampling_rederivation",
                "$sampling",
                "sampling manifest differs from deterministic source-only rederivation",
            )
    return canonical


def build_two_wave_method_stage_payload_v1(
    sampling_manifest: Mapping[str, Any],
    *,
    stage_id: str,
    sample_coverage_sha256: str,
    cluster_pair_deltas: Mapping[str, Mapping[str, int | float]],
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    method_id, active_wave, incremental_only = _method_stage_spec(stage_id)
    coverage_sha256 = require_sha256(
        sample_coverage_sha256,
        path="$.sample_coverage_sha256",
    )
    rows = require_mapping(cluster_pair_deltas, path="$.cluster_pair_deltas")
    expected_cluster_ids = _method_stage_cluster_ids(manifest, stage_id)
    if set(rows) != set(expected_cluster_ids):
        raise ContractValidationError(
            "cluster_exact_cover",
            "$.cluster_pair_deltas",
            "method stage must cover its exact frozen cluster set",
        )
    pair_specs = _pair_specs()
    cluster_rows: list[dict[str, Any]] = []
    for cluster_id in expected_cluster_ids:
        pair_values = require_mapping(
            rows[cluster_id],
            path=f"$.cluster_pair_deltas.{cluster_id}",
        )
        if set(pair_values) != {pair_id for pair_id, _, _ in pair_specs}:
            raise ContractValidationError(
                "pair_exact_cover",
                f"$.cluster_pair_deltas.{cluster_id}",
                "each method cluster must cover all ten arm pairs",
            )
        cluster_rows.append(
            {
                "cluster_id": cluster_id,
                "pair_rows": [
                    {
                        "pair_id": pair_id,
                        "arm_1_id": arm_1_id,
                        "arm_2_id": arm_2_id,
                        "delta_arm_1_minus_arm_2": require_number(
                            pair_values[pair_id],
                            path=(
                                f"$.cluster_pair_deltas.{cluster_id}.{pair_id}"
                            ),
                        ),
                    }
                    for pair_id, arm_1_id, arm_2_id in pair_specs
                ],
            }
        )
    payload = {
        "schema_id": METHOD_STAGE_SCHEMA_ID,
        "schema_version": METHOD_STAGE_SCHEMA_VERSION,
        "stage_id": stage_id,
        "method_id": method_id,
        "active_wave": active_wave,
        "incremental_only": incremental_only,
        "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "sample_coverage_sha256": coverage_sha256,
        "cluster_count": len(expected_cluster_ids),
        "cluster_ids": list(expected_cluster_ids),
        "cluster_rows": cluster_rows,
    }
    return validate_two_wave_method_stage_payload_v1(
        payload,
        sampling_manifest=manifest,
        expected_stage_id=stage_id,
        expected_coverage_sha256=coverage_sha256,
    )


def validate_two_wave_method_stage_payload_v1(
    value: Mapping[str, Any],
    *,
    sampling_manifest: Mapping[str, Any],
    expected_stage_id: str,
    expected_coverage_sha256: str,
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    stage_id = require_enum(
        expected_stage_id,
        set(_METHOD_STAGE_SPECS),
        path="$.expected_stage_id",
    )
    method_id, active_wave, incremental_only = _method_stage_spec(stage_id)
    coverage_sha256 = require_sha256(
        expected_coverage_sha256,
        path="$.expected_coverage_sha256",
    )
    row = require_mapping(value, path="$method_stage")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "stage_id",
            "method_id",
            "active_wave",
            "incremental_only",
            "sampling_manifest_sha256",
            "sample_coverage_sha256",
            "cluster_count",
            "cluster_ids",
            "cluster_rows",
        },
        path="$method_stage",
    )
    observed_incremental = row["incremental_only"]
    if not isinstance(observed_incremental, bool):
        raise ContractValidationError(
            "type",
            "$method_stage.incremental_only",
            "expected boolean",
        )
    expected_cluster_ids = _method_stage_cluster_ids(manifest, stage_id)
    normalized_cluster_ids = _string_list(
        row["cluster_ids"],
        path="$method_stage.cluster_ids",
    )
    if normalized_cluster_ids != list(expected_cluster_ids):
        raise ContractValidationError(
            "cluster_exact_cover",
            "$method_stage.cluster_ids",
            "method stage cluster IDs differ from the frozen wave partition",
        )
    raw_cluster_rows = require_list(
        row["cluster_rows"],
        path="$method_stage.cluster_rows",
    )
    if len(raw_cluster_rows) != len(expected_cluster_ids):
        raise ContractValidationError(
            "cluster_exact_cover",
            "$method_stage.cluster_rows",
            "method stage cluster row count differs from the frozen wave partition",
        )
    pair_specs = _pair_specs()
    normalized_cluster_rows: list[dict[str, Any]] = []
    for cluster_index, (cluster_id, raw_cluster) in enumerate(
        zip(expected_cluster_ids, raw_cluster_rows, strict=True)
    ):
        cluster_path = f"$method_stage.cluster_rows[{cluster_index}]"
        cluster = require_mapping(raw_cluster, path=cluster_path)
        require_exact_keys(
            cluster,
            required={"cluster_id", "pair_rows"},
            path=cluster_path,
        )
        pair_rows = require_list(
            cluster["pair_rows"],
            path=f"{cluster_path}.pair_rows",
        )
        if len(pair_rows) != len(pair_specs):
            raise ContractValidationError(
                "pair_exact_cover",
                f"{cluster_path}.pair_rows",
                "each method cluster must cover all ten arm pairs",
            )
        normalized_pairs: list[dict[str, Any]] = []
        for pair_index, ((pair_id, arm_1_id, arm_2_id), raw_pair) in enumerate(
            zip(pair_specs, pair_rows, strict=True)
        ):
            pair_path = f"{cluster_path}.pair_rows[{pair_index}]"
            pair = require_mapping(raw_pair, path=pair_path)
            require_exact_keys(
                pair,
                required={
                    "pair_id",
                    "arm_1_id",
                    "arm_2_id",
                    "delta_arm_1_minus_arm_2",
                },
                path=pair_path,
            )
            normalized_pairs.append(
                {
                    "pair_id": require_enum(
                        pair["pair_id"], {pair_id}, path=f"{pair_path}.pair_id"
                    ),
                    "arm_1_id": require_enum(
                        pair["arm_1_id"],
                        {arm_1_id},
                        path=f"{pair_path}.arm_1_id",
                    ),
                    "arm_2_id": require_enum(
                        pair["arm_2_id"],
                        {arm_2_id},
                        path=f"{pair_path}.arm_2_id",
                    ),
                    "delta_arm_1_minus_arm_2": require_number(
                        pair["delta_arm_1_minus_arm_2"],
                        path=f"{pair_path}.delta_arm_1_minus_arm_2",
                    ),
                }
            )
        normalized_cluster_rows.append(
            {
                "cluster_id": require_enum(
                    cluster["cluster_id"],
                    {cluster_id},
                    path=f"{cluster_path}.cluster_id",
                ),
                "pair_rows": normalized_pairs,
            }
        )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"],
            {METHOD_STAGE_SCHEMA_ID},
            path="$method_stage.schema_id",
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {METHOD_STAGE_SCHEMA_VERSION},
            path="$method_stage.schema_version",
        ),
        "stage_id": require_enum(
            row["stage_id"], {stage_id}, path="$method_stage.stage_id"
        ),
        "method_id": require_enum(
            row["method_id"], {method_id}, path="$method_stage.method_id"
        ),
        "active_wave": require_enum(
            row["active_wave"], {active_wave}, path="$method_stage.active_wave"
        ),
        "incremental_only": observed_incremental,
        "sampling_manifest_sha256": require_sha256(
            row["sampling_manifest_sha256"],
            path="$method_stage.sampling_manifest_sha256",
        ),
        "sample_coverage_sha256": require_sha256(
            row["sample_coverage_sha256"],
            path="$method_stage.sample_coverage_sha256",
        ),
        "cluster_count": require_int(
            row["cluster_count"],
            path="$method_stage.cluster_count",
            minimum=1,
        ),
        "cluster_ids": normalized_cluster_ids,
        "cluster_rows": normalized_cluster_rows,
    }
    if normalized["incremental_only"] is not incremental_only:
        raise ContractValidationError(
            "stage_policy",
            "$method_stage.incremental_only",
            "method stage incremental policy differs from the closed schedule",
        )
    if (
        normalized["sampling_manifest_sha256"]
        != manifest["integrity"]["manifest_sha256"]
    ):
        raise ContractValidationError(
            "manifest_binding",
            "$method_stage.sampling_manifest_sha256",
            "method stage belongs to another sampling manifest",
        )
    if normalized["sample_coverage_sha256"] != coverage_sha256:
        raise ContractValidationError(
            "coverage_binding",
            "$method_stage.sample_coverage_sha256",
            "method stage belongs to another sample coverage artifact",
        )
    if normalized["cluster_count"] != len(expected_cluster_ids):
        raise ContractValidationError(
            "cluster_exact_cover",
            "$method_stage.cluster_count",
            "method stage cluster count differs from the frozen wave partition",
        )
    return normalized


def build_two_wave_uncertainty_decision_v1(
    sampling_manifest: Mapping[str, Any],
    *,
    completed_wave: str,
    sample_coverage_sha256s: Mapping[str, str],
    method_stage_artifacts: Sequence[Mapping[str, Any]],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    wave = require_enum(completed_wave, {"wave_a", "wave_b"}, path="$.completed_wave")
    coverage_bindings = _validate_coverage_bindings(
        sample_coverage_sha256s,
        completed_wave=wave,
    )
    accepted_artifacts, artifact_bindings, scorer_run_binding = (
        _validate_method_stage_artifacts(
            method_stage_artifacts,
            sampling_manifest=manifest,
            completed_wave=wave,
            coverage_bindings=coverage_bindings,
        )
    )
    evidence = _derive_pair_evidence(
        manifest,
        completed_wave=wave,
        method_stage_artifacts=accepted_artifacts,
    )
    decision, reasons, headline_status = _uncertainty_outcome(wave, evidence)
    draft = {
        "schema_id": UNCERTAINTY_SCHEMA_ID,
        "schema_version": UNCERTAINTY_SCHEMA_VERSION,
        "created_at": require_rfc3339(created_at, path="$.created_at"),
        "producer": {
            "workstream": "evaluation",
            "component": "two_wave_sampling_v1",
            "component_version": UNCERTAINTY_SCHEMA_VERSION,
            "code_commit": require_commit(
                producer_code_commit, path="$.producer_code_commit"
            ),
        },
        "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "completed_wave": wave,
        "cumulative_cluster_count": len(
            manifest["waves"][wave]["cluster_ids"]
        ),
        "coverage_bindings": coverage_bindings,
        "scorer_run_binding": scorer_run_binding,
        "method_artifact_bindings": artifact_bindings,
        "pair_evidence": evidence,
        "decision": decision,
        "reasons": reasons,
        "headline_status": headline_status,
        "integrity": {"decision_sha256": "0" * 64},
    }
    sealed = seal_payload(
        draft,
        policy=_UNCERTAINTY_POLICY,
        hash_path=_UNCERTAINTY_HASH_PATH,
    )
    return validate_two_wave_uncertainty_decision_v1(
        sealed,
        sampling_manifest=manifest,
        sample_coverage_sha256s=sample_coverage_sha256s,
        method_stage_artifacts=method_stage_artifacts,
    )


def validate_two_wave_uncertainty_decision_v1(
    value: Mapping[str, Any],
    *,
    sampling_manifest: Mapping[str, Any],
    sample_coverage_sha256s: Mapping[str, str],
    method_stage_artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    row = require_mapping(value, path="$decision")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "sampling_manifest_sha256",
            "completed_wave",
            "cumulative_cluster_count",
            "coverage_bindings",
            "scorer_run_binding",
            "method_artifact_bindings",
            "pair_evidence",
            "decision",
            "reasons",
            "headline_status",
            "integrity",
        },
        path="$decision",
    )
    completed_wave = require_enum(
        row["completed_wave"],
        {"wave_a", "wave_b"},
        path="$decision.completed_wave",
    )
    coverage_bindings = _validate_coverage_bindings(
        sample_coverage_sha256s,
        completed_wave=completed_wave,
    )
    accepted_artifacts, artifact_bindings, scorer_run_binding = (
        _validate_method_stage_artifacts(
            method_stage_artifacts,
            sampling_manifest=manifest,
            completed_wave=completed_wave,
            coverage_bindings=coverage_bindings,
        )
    )
    expected_evidence = _derive_pair_evidence(
        manifest,
        completed_wave=completed_wave,
        method_stage_artifacts=accepted_artifacts,
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {UNCERTAINTY_SCHEMA_ID}, path="$decision.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {UNCERTAINTY_SCHEMA_VERSION},
            path="$decision.schema_version",
        ),
        "created_at": require_rfc3339(
            row["created_at"], path="$decision.created_at"
        ),
        "producer": _validate_producer(row["producer"], path="$decision.producer"),
        "sampling_manifest_sha256": require_sha256(
            row["sampling_manifest_sha256"],
            path="$decision.sampling_manifest_sha256",
        ),
        "completed_wave": completed_wave,
        "cumulative_cluster_count": require_int(
            row["cumulative_cluster_count"],
            path="$decision.cumulative_cluster_count",
            minimum=1,
        ),
        "coverage_bindings": _validate_decision_coverage_bindings(
            row["coverage_bindings"]
        ),
        "scorer_run_binding": _validate_scorer_run_binding(
            row["scorer_run_binding"]
        ),
        "method_artifact_bindings": _validate_decision_artifact_bindings(
            row["method_artifact_bindings"]
        ),
        "pair_evidence": _validate_pair_evidence(
            row["pair_evidence"],
            expected_unit_count=len(manifest["waves"][completed_wave]["cluster_ids"]),
        ),
        "decision": require_enum(
            row["decision"],
            {"open_wave_b", "stop_conclusive", "stop_inconclusive"},
            path="$decision.decision",
        ),
        "reasons": _string_list(row["reasons"], path="$decision.reasons"),
        "headline_status": require_enum(
            row["headline_status"],
            {"INCONCLUSIVE", "PENDING_WAVE_B", "METHOD_RESULTS_AVAILABLE"},
            path="$decision.headline_status",
        ),
        "integrity": _one_hash(
            row["integrity"], "decision_sha256", "$decision.integrity"
        ),
    }
    if (
        normalized["sampling_manifest_sha256"]
        != manifest["integrity"]["manifest_sha256"]
    ):
        raise ContractValidationError(
            "manifest_binding",
            "$decision.sampling_manifest_sha256",
            "uncertainty decision belongs to another sampling manifest",
        )
    expected_cluster_count = len(
        manifest["waves"][normalized["completed_wave"]]["cluster_ids"]
    )
    if normalized["cumulative_cluster_count"] != expected_cluster_count:
        raise ContractValidationError(
            "unit_count",
            "$decision.cumulative_cluster_count",
            "decision unit count differs from the frozen cumulative wave",
        )
    if normalized["coverage_bindings"] != coverage_bindings:
        raise ContractValidationError(
            "coverage_binding",
            "$decision.coverage_bindings",
            "decision coverage bindings differ from the accepted scorer inputs",
        )
    if normalized["scorer_run_binding"] != scorer_run_binding:
        raise ContractValidationError(
            "scorer_run_binding",
            "$decision.scorer_run_binding",
            "decision belongs to another scorer component/settings identity",
        )
    if normalized["method_artifact_bindings"] != artifact_bindings:
        raise ContractValidationError(
            "artifact_binding",
            "$decision.method_artifact_bindings",
            "decision method artifacts differ from accepted scorer artifacts",
        )
    if normalized["pair_evidence"] != expected_evidence:
        raise ContractValidationError(
            "evidence_rederivation",
            "$decision.pair_evidence",
            "pair evidence is not the deterministic rederivation of scorer rows",
        )
    expected_decision, expected_reasons, expected_status = _uncertainty_outcome(
        normalized["completed_wave"], normalized["pair_evidence"]
    )
    if expected_decision != normalized["decision"]:
        raise ContractValidationError(
            "decision_policy",
            "$decision.decision",
            "decision differs from the closed uncertainty rule",
        )
    if expected_reasons != normalized["reasons"]:
        raise ContractValidationError(
            "decision_policy",
            "$decision.reasons",
            "decision reasons differ from the closed uncertainty rule",
        )
    if expected_status != normalized["headline_status"]:
        raise ContractValidationError(
            "decision_policy",
            "$decision.headline_status",
            "headline status differs from the closed uncertainty rule",
        )
    if not verify_payload_hash(
        normalized,
        policy=_UNCERTAINTY_POLICY,
        hash_path=_UNCERTAINTY_HASH_PATH,
    ):
        raise ContractValidationError(
            "decision_hash",
            "$decision.integrity.decision_sha256",
            "uncertainty decision self-hash does not match",
        )
    canonical = canonicalize(normalized, policy=_UNCERTAINTY_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("uncertainty decision must remain an object")
    return canonical


def build_two_wave_work_plan_v1(
    sampling_manifest: Mapping[str, Any],
    *,
    active_wave: str,
    arm_ids: Sequence[str] = BENCHMARK_ARM_IDS_V1,
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    wave = require_enum(active_wave, {"wave_a", "wave_b"}, path="$.active_wave")
    arms = tuple(
        require_enum(item, BENCHMARK_ARM_IDS_V1, path="$.arm_ids[*]")
        for item in arm_ids
    )
    if arms != BENCHMARK_ARM_IDS_V1:
        raise ContractValidationError(
            "arm_order",
            "$.arm_ids",
            "two-wave production work requires the exact ordered five-arm set",
        )
    scope = manifest["waves"][wave]
    pair_ids = [
        f"{first}__{second}"
        for first, second in itertools.combinations(arms, 2)
    ]
    cluster_count = scope["cluster_count"]
    block_count = scope["block_count"]
    incremental_cluster_ids = (
        list(scope["cluster_ids"])
        if wave == "wave_a"
        else list(
            scope["cluster_ids"][
                manifest["waves"]["wave_a"]["cluster_count"] :
            ]
        )
    )
    incremental_cluster_set = set(incremental_cluster_ids)
    incremental_block_ids = [
        block_id
        for cluster in manifest["clusters"]
        if cluster["cluster_id"] in incremental_cluster_set
        for block_id in cluster["block_ids"]
    ]
    incremental_cluster_count = len(incremental_cluster_ids)
    incremental_block_count = len(incremental_block_ids)
    draft = {
        "schema_id": WORK_PLAN_SCHEMA_ID,
        "schema_version": WORK_PLAN_SCHEMA_VERSION,
        "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "active_wave": wave,
        "selected_chapter_ids": list(TWO_WAVE_CHAPTER_IDS_V1),
        "selected_arm_ids": list(arms),
        "active_cluster_ids": list(scope["cluster_ids"]),
        "active_block_ids": list(scope["block_ids"]),
        "incremental_cluster_ids": incremental_cluster_ids,
        "incremental_block_ids": incremental_block_ids,
        "pair_ids": pair_ids,
        "logical_work": {
            "dtq_full_rows": manifest["population"]["eligible_block_count"] * len(arms),
            "terminology_full_blocks": manifest["population"]["eligible_block_count"],
            "btf_sampled_rows": block_count * len(arms),
            "btf_incremental_rows": incremental_block_count * len(arms),
            "mtq5_cluster_pair_orientations": cluster_count * len(pair_ids) * 2,
            "mtq5_incremental_cluster_pair_orientations": (
                incremental_cluster_count * len(pair_ids) * 2
            ),
        },
        "stages": list(two_wave_workflow_stages_v1()),
        "integrity": {"work_plan_sha256": "0" * 64},
    }
    sealed = seal_payload(
        draft,
        policy=_WORK_PLAN_POLICY,
        hash_path=_WORK_PLAN_HASH_PATH,
    )
    return validate_two_wave_work_plan_v1(sealed, sampling_manifest=manifest)


def validate_two_wave_work_plan_v1(
    value: Mapping[str, Any],
    *,
    sampling_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    row = require_mapping(value, path="$work_plan")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "sampling_manifest_sha256",
            "active_wave",
            "selected_chapter_ids",
            "selected_arm_ids",
            "active_cluster_ids",
            "active_block_ids",
            "incremental_cluster_ids",
            "incremental_block_ids",
            "pair_ids",
            "logical_work",
            "stages",
            "integrity",
        },
        path="$work_plan",
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {WORK_PLAN_SCHEMA_ID}, path="$work_plan.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {WORK_PLAN_SCHEMA_VERSION},
            path="$work_plan.schema_version",
        ),
        "sampling_manifest_sha256": require_sha256(
            row["sampling_manifest_sha256"],
            path="$work_plan.sampling_manifest_sha256",
        ),
        "active_wave": require_enum(
            row["active_wave"], {"wave_a", "wave_b"}, path="$work_plan.active_wave"
        ),
        "selected_chapter_ids": _enum_list(
            row["selected_chapter_ids"],
            TWO_WAVE_CHAPTER_IDS_V1,
            path="$work_plan.selected_chapter_ids",
        ),
        "selected_arm_ids": _enum_list(
            row["selected_arm_ids"],
            BENCHMARK_ARM_IDS_V1,
            path="$work_plan.selected_arm_ids",
        ),
        "active_cluster_ids": _string_list(
            row["active_cluster_ids"], path="$work_plan.active_cluster_ids"
        ),
        "active_block_ids": _string_list(
            row["active_block_ids"], path="$work_plan.active_block_ids"
        ),
        "incremental_cluster_ids": _string_list(
            row["incremental_cluster_ids"],
            path="$work_plan.incremental_cluster_ids",
        ),
        "incremental_block_ids": _string_list(
            row["incremental_block_ids"],
            path="$work_plan.incremental_block_ids",
        ),
        "pair_ids": _string_list(row["pair_ids"], path="$work_plan.pair_ids"),
        "logical_work": _validate_logical_work(row["logical_work"]),
        "stages": _validate_stages(row["stages"]),
        "integrity": _one_hash(
            row["integrity"], "work_plan_sha256", "$work_plan.integrity"
        ),
    }
    if normalized["sampling_manifest_sha256"] != manifest["integrity"]["manifest_sha256"]:
        raise ContractValidationError(
            "manifest_binding",
            "$work_plan.sampling_manifest_sha256",
            "work plan belongs to another sampling manifest",
        )
    expected = _work_plan_fields(
        manifest,
        active_wave=normalized["active_wave"],
        arm_ids=tuple(normalized["selected_arm_ids"]),
    )
    for key in (
        "selected_chapter_ids",
        "active_cluster_ids",
        "active_block_ids",
        "incremental_cluster_ids",
        "incremental_block_ids",
        "pair_ids",
        "logical_work",
        "stages",
    ):
        if normalized[key] != expected[key]:
            raise ContractValidationError(
                "work_plan_policy",
                f"$work_plan.{key}",
                "work plan differs from deterministic sampling policy",
            )
    if not verify_payload_hash(
        normalized,
        policy=_WORK_PLAN_POLICY,
        hash_path=_WORK_PLAN_HASH_PATH,
    ):
        raise ContractValidationError(
            "work_plan_hash",
            "$work_plan.integrity.work_plan_sha256",
            "work plan self-hash does not match",
        )
    canonical = canonicalize(normalized, policy=_WORK_PLAN_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("work plan must remain an object")
    return canonical


def two_wave_workflow_stages_v1() -> tuple[dict[str, Any], ...]:
    rows = (
        ("preflight", "evaluation_preflight", False),
        ("sample_plan", "evaluation_sampler", False),
        ("dtq_full", "evaluation_local_scorer", False),
        ("terminology_occurrence_full", "evaluation_terminology_scorer", False),
        ("btf_wave_a", "evaluation_back_translation", False),
        ("mtq5_wave_a", "evaluation_pairwise_judge", False),
        ("uncertainty_gate_wave_a", "evaluation_uncertainty_gate", False),
        ("btf_wave_b", "evaluation_back_translation", True),
        ("mtq5_wave_b", "evaluation_pairwise_judge", True),
        ("uncertainty_gate_wave_b", "evaluation_uncertainty_gate", True),
        ("aggregation", "evaluation_aggregator", False),
        ("report_final", "evaluation_report_writer", False),
    )
    return tuple(
        {
            "stage_id": stage_id,
            "ordinal": ordinal,
            "agent": agent,
            "conditional": conditional,
        }
        for ordinal, (stage_id, agent, conditional) in enumerate(rows)
    )


def two_wave_component_stages_v1() -> tuple[dict[str, Any], ...]:
    """Project the schedule into the closed Console component-stage contract."""
    return tuple(
        {
            "stage_id": stage["stage_id"],
            "ordinal": stage["ordinal"],
            "agent": stage["agent"],
        }
        for stage in two_wave_workflow_stages_v1()
    )


def _validate_chapter_inputs(
    value: Mapping[str, CommonEvaluationInputV1],
) -> dict[str, CommonEvaluationInputV1]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("type", "$.chapter_inputs", "expected an object")
    if tuple(value.keys()) != TWO_WAVE_CHAPTER_IDS_V1:
        raise ContractValidationError(
            "chapter_order",
            "$.chapter_inputs",
            "chapter inputs must use the exact locked five-chapter order",
        )
    result: dict[str, CommonEvaluationInputV1] = {}
    project_document: tuple[str, str] | None = None
    block_ids: set[str] = set()
    for chapter_id in TWO_WAVE_CHAPTER_IDS_V1:
        common_input = value[chapter_id]
        if not isinstance(common_input, CommonEvaluationInputV1):
            raise ContractValidationError(
                "type",
                f"$.chapter_inputs.{chapter_id}",
                "expected CommonEvaluationInputV1",
            )
        observed = {block.chapter_id for block in common_input.blocks}
        if observed != {chapter_id}:
            raise ContractValidationError(
                "chapter_scope",
                f"$.chapter_inputs.{chapter_id}.blocks",
                "each chapter input must contain exactly its named chapter",
            )
        current_identity = (common_input.project_id, common_input.document_id)
        if project_document is None:
            project_document = current_identity
        elif current_identity != project_document:
            raise ContractValidationError(
                "source_identity",
                f"$.chapter_inputs.{chapter_id}",
                "all chapters must belong to one project/document",
            )
        for block in common_input.blocks:
            if block.block_id in block_ids:
                raise ContractValidationError(
                    "duplicate_block",
                    f"$.chapter_inputs.{chapter_id}.blocks",
                    f"block {block.block_id!r} appears in multiple chapters",
                )
            block_ids.add(block.block_id)
        result[chapter_id] = common_input
    return result


def _normalize_term_features(
    eligible_blocks: Sequence[CommonBlockV1],
    *,
    term_occurrence_counts: Mapping[str, int] | None,
    term_feature_binding: Mapping[str, Any] | None,
) -> tuple[dict[str, int] | None, dict[str, Any]]:
    if (term_occurrence_counts is None) != (term_feature_binding is None):
        raise ContractValidationError(
            "feature_binding",
            "$.term_features",
            "term counts and their typed artifact binding must be supplied together",
        )
    if term_occurrence_counts is None:
        return None, {
            "mode": "block_metadata_only",
            "term_feature_binding": None,
            "term_density_status": "unknown_not_used_as_stratum",
        }
    expected_ids = [block.block_id for block in eligible_blocks]
    if set(term_occurrence_counts) != set(expected_ids):
        raise ContractValidationError(
            "feature_exact_cover",
            "$.term_occurrence_counts",
            "term feature map must exact-cover every eligible source block",
        )
    counts = {
        block_id: require_int(
            term_occurrence_counts[block_id],
            path=f"$.term_occurrence_counts.{block_id}",
            minimum=0,
        )
        for block_id in expected_ids
    }
    binding = validate_typed_artifact_binding_v1(
        term_feature_binding, path="$.term_feature_binding"
    )
    return counts, {
        "mode": "sealed_source_term_counts_v1",
        "term_feature_binding": binding,
        "term_density_status": "used_as_source_only_stratum",
    }


def _select_partition_offset(
    blocks: Sequence[CommonBlockV1],
    *,
    seed: str,
    chapter_id: str,
    required_cluster_count: int,
) -> int:
    candidates = [
        offset
        for offset in range(TWO_WAVE_CLUSTER_SIZE_V1)
        if len(blocks[offset:]) // TWO_WAVE_CLUSTER_SIZE_V1 >= required_cluster_count
    ]
    if not candidates:
        raise ContractValidationError(
            "sampling_capacity",
            f"$.chapter_inputs.{chapter_id}",
            "chapter lacks enough eligible blocks for the locked Wave B quota",
        )
    return min(
        candidates,
        key=lambda offset: _stable_sha256(
            {"seed": seed, "chapter_id": chapter_id, "partition_offset": offset}
        ),
    )


def _partition_candidates(
    blocks: Sequence[CommonBlockV1],
    *,
    offset: int,
    seed: str,
    term_counts: Mapping[str, int] | None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    usable = blocks[offset:]
    for start in range(0, len(usable) - TWO_WAVE_CLUSTER_SIZE_V1 + 1, 5):
        selected = tuple(usable[start : start + TWO_WAVE_CLUSTER_SIZE_V1])
        if len(selected) != TWO_WAVE_CLUSTER_SIZE_V1:
            continue
        stratum = _cluster_stratum(selected, term_counts=term_counts)
        block_ids = [block.block_id for block in selected]
        priority = _stable_sha256(
            {
                "seed": seed,
                "chapter_id": selected[0].chapter_id,
                "block_ids": block_ids,
                "stratum": stratum,
            }
        )
        result.append(
            {
                "cluster_id": "cluster_" + priority[:24],
                "chapter_id": selected[0].chapter_id,
                "chapter_cluster_ordinal": -1,
                "block_ids": block_ids,
                "order_indexes": [block.order_index for block in selected],
                "stratum": stratum,
                "priority_sha256": priority,
            }
        )
    return result


def _stratified_order(
    candidates: Sequence[dict[str, Any]],
    *,
    seed: str,
    chapter_id: str,
) -> list[dict[str, Any]]:
    groups: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for candidate in sorted(candidates, key=lambda item: item["priority_sha256"]):
        groups[_stable_text(candidate["stratum"])].append(candidate)
    strata = sorted(
        groups,
        key=lambda key: _stable_sha256(
            {"seed": seed, "chapter_id": chapter_id, "stratum": key}
        ),
    )
    result: list[dict[str, Any]] = []
    while any(groups[key] for key in strata):
        for key in strata:
            if groups[key]:
                result.append(groups[key].popleft())
    return result


def _cluster_stratum(
    blocks: Sequence[CommonBlockV1],
    *,
    term_counts: Mapping[str, int] | None,
) -> dict[str, str]:
    type_counts = Counter(block.block_type for block in blocks)
    max_count = max(type_counts.values())
    dominant = min(key for key, count in type_counts.items() if count == max_count)
    if len(type_counts) > 1:
        dominant = "mixed:" + dominant
    char_count = sum(len(block.source_text) for block in blocks)
    length_band = next(label for maximum, label in _LENGTH_BANDS if char_count <= maximum)
    if term_counts is None:
        density_band = "unknown"
    else:
        occurrence_count = sum(term_counts[block.block_id] for block in blocks)
        density = 1000.0 * occurrence_count / max(1, char_count)
        density_band = next(
            label for maximum, label in _TERM_DENSITY_BANDS if density <= maximum
        )
    return {
        "block_type_family": dominant,
        "length_band": length_band,
        "term_density_band": density_band,
    }


def _wave_scope(
    clusters: Sequence[Mapping[str, Any]],
    *,
    wave_id: str,
    quotas: Mapping[str, int],
    prefix_cluster_ids: Sequence[str] = (),
) -> dict[str, Any]:
    selected: list[Mapping[str, Any]] = []
    for chapter_id in TWO_WAVE_CHAPTER_IDS_V1:
        selected.extend(
            cluster
            for cluster in clusters
            if cluster["chapter_id"] == chapter_id
            and cluster["chapter_cluster_ordinal"] < quotas[chapter_id]
        )
    if prefix_cluster_ids:
        selected_by_id = {cluster["cluster_id"]: cluster for cluster in selected}
        prefix = [selected_by_id[cluster_id] for cluster_id in prefix_cluster_ids]
        prefix_set = set(prefix_cluster_ids)
        selected = prefix + [
            cluster for cluster in selected if cluster["cluster_id"] not in prefix_set
        ]
    return {
        "wave_id": wave_id,
        "cumulative": True,
        "chapter_cluster_quotas": dict(quotas),
        "cluster_count": len(selected),
        "block_count": len(selected) * TWO_WAVE_CLUSTER_SIZE_V1,
        "cluster_ids": [cluster["cluster_id"] for cluster in selected],
        "block_ids": [
            block_id for cluster in selected for block_id in cluster["block_ids"]
        ],
    }


def _validate_sampling_identity(value: Any) -> dict[str, Any]:
    path = "$sampling.identity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "sampling_policy_id",
            "sampling_policy_version",
            "seed",
            "cluster_size",
            "selected_chapter_ids",
        },
        path=path,
    )
    return {
        "sampling_policy_id": require_enum(
            row["sampling_policy_id"],
            {"d2l_five_chapter_two_wave_v1"},
            path=f"{path}.sampling_policy_id",
        ),
        "sampling_policy_version": require_enum(
            row["sampling_policy_version"], {"1.0.0"}, path=f"{path}.sampling_policy_version"
        ),
        "seed": require_string(row["seed"], path=f"{path}.seed", maximum=200),
        "cluster_size": require_int(
            row["cluster_size"], path=f"{path}.cluster_size", minimum=1
        ),
        "selected_chapter_ids": _enum_list(
            row["selected_chapter_ids"],
            TWO_WAVE_CHAPTER_IDS_V1,
            path=f"{path}.selected_chapter_ids",
        ),
    }


def _validate_source_binding_hashes(value: Any) -> dict[str, str]:
    path = "$sampling.source_bindings"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=TWO_WAVE_CHAPTER_IDS_V1, path=path)
    return {
        chapter_id: require_sha256(row[chapter_id], path=f"{path}.{chapter_id}")
        for chapter_id in TWO_WAVE_CHAPTER_IDS_V1
    }


def _validate_source_features(value: Any) -> dict[str, Any]:
    path = "$sampling.source_features"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"mode", "term_feature_binding", "term_density_status"},
        path=path,
    )
    mode = require_enum(
        row["mode"],
        {"block_metadata_only", "sealed_source_term_counts_v1"},
        path=f"{path}.mode",
    )
    if mode == "block_metadata_only":
        if row["term_feature_binding"] is not None:
            raise ContractValidationError(
                "feature_binding",
                f"{path}.term_feature_binding",
                "metadata-only mode cannot bind a term feature artifact",
            )
        status = require_enum(
            row["term_density_status"],
            {"unknown_not_used_as_stratum"},
            path=f"{path}.term_density_status",
        )
        binding = None
    else:
        binding = validate_typed_artifact_binding_v1(
            row["term_feature_binding"], path=f"{path}.term_feature_binding"
        )
        status = require_enum(
            row["term_density_status"],
            {"used_as_source_only_stratum"},
            path=f"{path}.term_density_status",
        )
    return {
        "mode": mode,
        "term_feature_binding": binding,
        "term_density_status": status,
    }


def _validate_population(value: Any) -> dict[str, Any]:
    path = "$sampling.population"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "eligible_block_count",
            "eligible_block_ids",
            "chapter_eligible_counts",
            "chapter_partition_offsets",
        },
        path=path,
    )
    block_ids = _string_list(
        row["eligible_block_ids"], path=f"{path}.eligible_block_ids"
    )
    if require_int(
        row["eligible_block_count"], path=f"{path}.eligible_block_count", minimum=0
    ) != len(block_ids):
        raise ContractValidationError(
            "population_count",
            f"{path}.eligible_block_count",
            "eligible block count differs from the ID list",
        )
    return {
        "eligible_block_count": len(block_ids),
        "eligible_block_ids": block_ids,
        "chapter_eligible_counts": _chapter_int_map(
            row["chapter_eligible_counts"],
            path=f"{path}.chapter_eligible_counts",
            maximum=None,
        ),
        "chapter_partition_offsets": _chapter_int_map(
            row["chapter_partition_offsets"],
            path=f"{path}.chapter_partition_offsets",
            maximum=4,
        ),
    }


def _validate_clusters(value: Any) -> list[dict[str, Any]]:
    path = "$sampling.clusters"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        require_exact_keys(
            row,
            required={
                "cluster_id",
                "chapter_id",
                "chapter_cluster_ordinal",
                "block_ids",
                "order_indexes",
                "stratum",
                "priority_sha256",
            },
            path=item_path,
        )
        block_ids = _string_list(row["block_ids"], path=f"{item_path}.block_ids")
        order_indexes = [
            require_int(item, path=f"{item_path}.order_indexes[{position}]", minimum=0)
            for position, item in enumerate(
                require_list(row["order_indexes"], path=f"{item_path}.order_indexes")
            )
        ]
        if len(block_ids) != 5 or len(order_indexes) != 5:
            raise ContractValidationError(
                "cluster_size", item_path, "every cluster must contain exactly five blocks"
            )
        if order_indexes != sorted(order_indexes):
            raise ContractValidationError(
                "source_order",
                f"{item_path}.order_indexes",
                "cluster order indexes must increase",
            )
        stratum = require_mapping(row["stratum"], path=f"{item_path}.stratum")
        require_exact_keys(
            stratum,
            required={"block_type_family", "length_band", "term_density_band"},
            path=f"{item_path}.stratum",
        )
        result.append(
            {
                "cluster_id": require_string(
                    row["cluster_id"], path=f"{item_path}.cluster_id"
                ),
                "chapter_id": require_enum(
                    row["chapter_id"],
                    TWO_WAVE_CHAPTER_IDS_V1,
                    path=f"{item_path}.chapter_id",
                ),
                "chapter_cluster_ordinal": require_int(
                    row["chapter_cluster_ordinal"],
                    path=f"{item_path}.chapter_cluster_ordinal",
                    minimum=0,
                ),
                "block_ids": block_ids,
                "order_indexes": order_indexes,
                "stratum": {
                    "block_type_family": require_string(
                        stratum["block_type_family"],
                        path=f"{item_path}.stratum.block_type_family",
                    ),
                    "length_band": require_enum(
                        stratum["length_band"],
                        {"short", "medium", "long"},
                        path=f"{item_path}.stratum.length_band",
                    ),
                    "term_density_band": require_enum(
                        stratum["term_density_band"],
                        {"unknown", "zero", "low", "medium", "high"},
                        path=f"{item_path}.stratum.term_density_band",
                    ),
                },
                "priority_sha256": require_sha256(
                    row["priority_sha256"], path=f"{item_path}.priority_sha256"
                ),
            }
        )
    require_unique([item["cluster_id"] for item in result], path=f"{path}.cluster_id")
    all_blocks = [block_id for item in result for block_id in item["block_ids"]]
    require_unique(all_blocks, path=f"{path}.block_ids")
    return result


def _validate_waves(value: Any) -> dict[str, Any]:
    path = "$sampling.waves"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"wave_a", "wave_b"}, path=path)
    return {
        wave_id: _validate_wave(row[wave_id], wave_id=wave_id)
        for wave_id in ("wave_a", "wave_b")
    }


def _validate_wave(value: Any, *, wave_id: str) -> dict[str, Any]:
    path = f"$sampling.waves.{wave_id}"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "wave_id",
            "cumulative",
            "chapter_cluster_quotas",
            "cluster_count",
            "block_count",
            "cluster_ids",
            "block_ids",
        },
        path=path,
    )
    if row["cumulative"] is not True:
        raise ContractValidationError(
            "wave_policy", f"{path}.cumulative", "wave scopes must be cumulative"
        )
    cluster_ids = _string_list(row["cluster_ids"], path=f"{path}.cluster_ids")
    block_ids = _string_list(row["block_ids"], path=f"{path}.block_ids")
    cluster_count = require_int(
        row["cluster_count"], path=f"{path}.cluster_count", minimum=0
    )
    block_count = require_int(
        row["block_count"], path=f"{path}.block_count", minimum=0
    )
    if cluster_count != len(cluster_ids) or block_count != len(block_ids):
        raise ContractValidationError(
            "wave_count", path, "wave counts differ from their ID lists"
        )
    return {
        "wave_id": require_enum(row["wave_id"], {wave_id}, path=f"{path}.wave_id"),
        "cumulative": True,
        "chapter_cluster_quotas": _chapter_int_map(
            row["chapter_cluster_quotas"],
            path=f"{path}.chapter_cluster_quotas",
            maximum=None,
        ),
        "cluster_count": cluster_count,
        "block_count": block_count,
        "cluster_ids": cluster_ids,
        "block_ids": block_ids,
    }


def _validate_method_policy(value: Any) -> dict[str, Any]:
    path = "$sampling.method_policy"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "full_universe_method_ids",
            "sampled_method_ids",
            "pair_policy",
            "pair_orientations",
            "missing_arm_policy",
            "wave_b_open_policy",
            "terminal_uncertainty_policy",
        },
        path=path,
    )
    return {
        "full_universe_method_ids": _enum_list(
            row["full_universe_method_ids"],
            ("sf_qe", "tc_occ", "ta_occ"),
            path=f"{path}.full_universe_method_ids",
        ),
        "sampled_method_ids": _enum_list(
            row["sampled_method_ids"],
            ("sf_bt", "pj"),
            path=f"{path}.sampled_method_ids",
        ),
        "pair_policy": require_enum(
            row["pair_policy"], {"all_unordered_pairs"}, path=f"{path}.pair_policy"
        ),
        "pair_orientations": _enum_list(
            row["pair_orientations"],
            ("canonical", "reversed"),
            path=f"{path}.pair_orientations",
        ),
        "missing_arm_policy": require_enum(
            row["missing_arm_policy"],
            {"hold_comparison_no_replacement"},
            path=f"{path}.missing_arm_policy",
        ),
        "wave_b_open_policy": require_enum(
            row["wave_b_open_policy"],
            {"paired_ci95_includes_zero_or_btf_mtq5_direction_disagrees"},
            path=f"{path}.wave_b_open_policy",
        ),
        "terminal_uncertainty_policy": require_enum(
            row["terminal_uncertainty_policy"],
            {"inconclusive_after_wave_b"},
            path=f"{path}.terminal_uncertainty_policy",
        ),
    }


def _validate_sampling_references(value: Mapping[str, Any]) -> None:
    identity = value["identity"]
    if tuple(identity["selected_chapter_ids"]) != TWO_WAVE_CHAPTER_IDS_V1:
        raise ContractValidationError(
            "chapter_order",
            "$sampling.identity.selected_chapter_ids",
            "sampling scope differs from the locked five chapters",
        )
    if identity["cluster_size"] != TWO_WAVE_CLUSTER_SIZE_V1:
        raise ContractValidationError(
            "cluster_size",
            "$sampling.identity.cluster_size",
            "sampling cluster size must remain five",
        )
    population = value["population"]
    if sum(population["chapter_eligible_counts"].values()) != population[
        "eligible_block_count"
    ]:
        raise ContractValidationError(
            "population_count",
            "$sampling.population.chapter_eligible_counts",
            "chapter populations do not sum to the total population",
        )
    cluster_by_id = {cluster["cluster_id"]: cluster for cluster in value["clusters"]}
    population_ids = set(population["eligible_block_ids"])
    for cluster in value["clusters"]:
        if any(block_id not in population_ids for block_id in cluster["block_ids"]):
            raise ContractValidationError(
                "foreign_block",
                "$sampling.clusters",
                "cluster contains a block outside the eligible population",
            )
    expected_quotas = {
        "wave_a": TWO_WAVE_WAVE_A_QUOTAS_V1,
        "wave_b": TWO_WAVE_WAVE_B_QUOTAS_V1,
    }
    for wave_id, wave in value["waves"].items():
        if wave["chapter_cluster_quotas"] != expected_quotas[wave_id]:
            raise ContractValidationError(
                "wave_quota",
                f"$sampling.waves.{wave_id}.chapter_cluster_quotas",
                "chapter quotas differ from the preregistered policy",
            )
        resolved = [cluster_by_id.get(cluster_id) for cluster_id in wave["cluster_ids"]]
        if any(cluster is None for cluster in resolved):
            raise ContractValidationError(
                "cluster_reference",
                f"$sampling.waves.{wave_id}.cluster_ids",
                "wave references an unknown cluster",
            )
        expected_blocks = [
            block_id
            for cluster in resolved
            if cluster is not None
            for block_id in cluster["block_ids"]
        ]
        if wave["block_ids"] != expected_blocks:
            raise ContractValidationError(
                "cluster_reference",
                f"$sampling.waves.{wave_id}.block_ids",
                "wave block IDs do not match its ordered clusters",
            )
    wave_a = value["waves"]["wave_a"]
    wave_b = value["waves"]["wave_b"]
    if wave_b["cluster_ids"][: wave_a["cluster_count"]] != wave_a["cluster_ids"]:
        raise ContractValidationError(
            "wave_cumulative",
            "$sampling.waves.wave_b.cluster_ids",
            "Wave A must be the exact prefix of cumulative Wave B",
        )
    if wave_b["block_ids"][: wave_a["block_count"]] != wave_a["block_ids"]:
        raise ContractValidationError(
            "wave_cumulative",
            "$sampling.waves.wave_b.block_ids",
            "Wave A blocks must be the exact prefix of cumulative Wave B",
        )


def _method_stage_spec(stage_id: str) -> tuple[str, str, bool]:
    normalized = require_enum(
        stage_id,
        set(_METHOD_STAGE_SPECS),
        path="$.stage_id",
    )
    return _METHOD_STAGE_SPECS[normalized]


def _method_stage_cluster_ids(
    manifest: Mapping[str, Any],
    stage_id: str,
) -> tuple[str, ...]:
    _, active_wave, incremental_only = _method_stage_spec(stage_id)
    wave_ids = tuple(manifest["waves"][active_wave]["cluster_ids"])
    if not incremental_only:
        cluster_ids = wave_ids
    else:
        wave_a_count = manifest["waves"]["wave_a"]["cluster_count"]
        cluster_ids = wave_ids[wave_a_count:]
    if len(cluster_ids) != 50:
        raise ContractValidationError(
            "cluster_policy",
            f"$.stages.{stage_id}",
            "each sampled scorer stage must own exactly 50 clusters",
        )
    return cluster_ids


def _pair_specs() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (f"{first}__{second}", first, second)
        for first, second in itertools.combinations(BENCHMARK_ARM_IDS_V1, 2)
    )


def _validate_coverage_bindings(
    value: Mapping[str, str],
    *,
    completed_wave: str,
) -> list[dict[str, str]]:
    row = require_mapping(value, path="$.sample_coverage_sha256s")
    expected_waves = (
        ("wave_a",) if completed_wave == "wave_a" else ("wave_a", "wave_b")
    )
    if set(row) != set(expected_waves):
        raise ContractValidationError(
            "coverage_exact_cover",
            "$.sample_coverage_sha256s",
            "coverage bindings must name each completed cumulative wave exactly once",
        )
    return [
        {
            "active_wave": wave_id,
            "coverage_sha256": require_sha256(
                row[wave_id],
                path=f"$.sample_coverage_sha256s.{wave_id}",
            ),
        }
        for wave_id in expected_waves
    ]


def _validate_decision_coverage_bindings(value: Any) -> list[dict[str, str]]:
    rows = require_list(value, path="$decision.coverage_bindings")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(rows):
        path = f"$decision.coverage_bindings[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={"active_wave", "coverage_sha256"},
            path=path,
        )
        result.append(
            {
                "active_wave": require_enum(
                    row["active_wave"],
                    {"wave_a", "wave_b"},
                    path=f"{path}.active_wave",
                ),
                "coverage_sha256": require_sha256(
                    row["coverage_sha256"],
                    path=f"{path}.coverage_sha256",
                ),
            }
        )
    return result


def _validate_method_stage_artifacts(
    value: Sequence[Mapping[str, Any]],
    *,
    sampling_manifest: Mapping[str, Any],
    completed_wave: str,
    coverage_bindings: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, str]]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    artifacts = list(value)
    expected_stage_ids = (
        ("btf_wave_a", "mtq5_wave_a")
        if completed_wave == "wave_a"
        else ("btf_wave_a", "mtq5_wave_a", "btf_wave_b", "mtq5_wave_b")
    )
    if len(artifacts) != len(expected_stage_ids):
        raise ContractValidationError(
            "artifact_exact_cover",
            "$.method_stage_artifacts",
            "uncertainty decision requires exact completed BTF/MTQ-5 artifacts",
        )
    coverage_by_wave = {
        row["active_wave"]: row["coverage_sha256"] for row in coverage_bindings
    }
    accepted: list[dict[str, Any]] = []
    bindings: list[dict[str, str]] = []
    scorer_run_binding: dict[str, str] | None = None
    for index, (stage_id, raw) in enumerate(
        zip(expected_stage_ids, artifacts, strict=True)
    ):
        path = f"$.method_stage_artifacts[{index}]"
        artifact = require_mapping(raw, path=path)
        require_exact_keys(
            artifact,
            required={
                "schema_id",
                "schema_version",
                "stage_id",
                "runner_binding",
                "payload",
                "artifact_sha256",
            },
            path=path,
        )
        method_id, active_wave, _ = _method_stage_spec(stage_id)
        runner = require_mapping(
            artifact["runner_binding"],
            path=f"{path}.runner_binding",
        )
        require_exact_keys(
            runner,
            required={
                "component_run_id",
                "sampling_manifest_sha256",
                "settings_sha256",
                "active_wave",
            },
            path=f"{path}.runner_binding",
        )
        normalized_runner = {
            "component_run_id": require_string(
                runner["component_run_id"],
                path=f"{path}.runner_binding.component_run_id",
            ),
            "sampling_manifest_sha256": require_sha256(
                runner["sampling_manifest_sha256"],
                path=f"{path}.runner_binding.sampling_manifest_sha256",
            ),
            "settings_sha256": require_sha256(
                runner["settings_sha256"],
                path=f"{path}.runner_binding.settings_sha256",
            ),
            "active_wave": require_enum(
                runner["active_wave"],
                {active_wave},
                path=f"{path}.runner_binding.active_wave",
            ),
        }
        if (
            normalized_runner["sampling_manifest_sha256"]
            != manifest["integrity"]["manifest_sha256"]
        ):
            raise ContractValidationError(
                "manifest_binding",
                f"{path}.runner_binding.sampling_manifest_sha256",
                "method artifact belongs to another sampling manifest",
            )
        current_run_binding = {
            "component_run_id": normalized_runner["component_run_id"],
            "settings_sha256": normalized_runner["settings_sha256"],
        }
        if scorer_run_binding is None:
            scorer_run_binding = current_run_binding
        elif scorer_run_binding != current_run_binding:
            raise ContractValidationError(
                "scorer_run_binding",
                f"{path}.runner_binding",
                "method artifacts belong to different component/settings identities",
            )
        coverage_sha256 = coverage_by_wave.get(active_wave)
        if coverage_sha256 is None:
            raise ContractValidationError(
                "coverage_binding",
                path,
                "method artifact references an unbound sample wave",
            )
        payload = validate_two_wave_method_stage_payload_v1(
            artifact["payload"],
            sampling_manifest=manifest,
            expected_stage_id=stage_id,
            expected_coverage_sha256=coverage_sha256,
        )
        normalized = {
            "schema_id": require_enum(
                artifact["schema_id"],
                {_STAGE_ARTIFACT_SCHEMA_ID},
                path=f"{path}.schema_id",
            ),
            "schema_version": require_enum(
                artifact["schema_version"],
                {_STAGE_ARTIFACT_SCHEMA_VERSION},
                path=f"{path}.schema_version",
            ),
            "stage_id": require_enum(
                artifact["stage_id"], {stage_id}, path=f"{path}.stage_id"
            ),
            "runner_binding": normalized_runner,
            "payload": payload,
            "artifact_sha256": require_sha256(
                artifact["artifact_sha256"],
                path=f"{path}.artifact_sha256",
            ),
        }
        artifact_body = {
            key: normalized[key]
            for key in (
                "schema_id",
                "schema_version",
                "stage_id",
                "runner_binding",
                "payload",
            )
        }
        if normalized["artifact_sha256"] != _stable_sha256(artifact_body):
            raise ContractValidationError(
                "artifact_hash",
                f"{path}.artifact_sha256",
                "method stage artifact hash does not match its accepted payload",
            )
        accepted.append(normalized)
        bindings.append(
            {
                "stage_id": stage_id,
                "method_id": method_id,
                "active_wave": active_wave,
                "sample_coverage_sha256": coverage_sha256,
                "artifact_sha256": normalized["artifact_sha256"],
            }
        )
    if scorer_run_binding is None:
        raise AssertionError("method artifact exact cover cannot be empty")
    return accepted, bindings, scorer_run_binding


def _validate_scorer_run_binding(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$decision.scorer_run_binding")
    require_exact_keys(
        row,
        required={"component_run_id", "settings_sha256"},
        path="$decision.scorer_run_binding",
    )
    return {
        "component_run_id": require_string(
            row["component_run_id"],
            path="$decision.scorer_run_binding.component_run_id",
        ),
        "settings_sha256": require_sha256(
            row["settings_sha256"],
            path="$decision.scorer_run_binding.settings_sha256",
        ),
    }


def _validate_decision_artifact_bindings(value: Any) -> list[dict[str, str]]:
    rows = require_list(value, path="$decision.method_artifact_bindings")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(rows):
        path = f"$decision.method_artifact_bindings[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "stage_id",
                "method_id",
                "active_wave",
                "sample_coverage_sha256",
                "artifact_sha256",
            },
            path=path,
        )
        result.append(
            {
                "stage_id": require_enum(
                    row["stage_id"],
                    set(_METHOD_STAGE_SPECS),
                    path=f"{path}.stage_id",
                ),
                "method_id": require_enum(
                    row["method_id"],
                    {"sf_bt", "pj"},
                    path=f"{path}.method_id",
                ),
                "active_wave": require_enum(
                    row["active_wave"],
                    {"wave_a", "wave_b"},
                    path=f"{path}.active_wave",
                ),
                "sample_coverage_sha256": require_sha256(
                    row["sample_coverage_sha256"],
                    path=f"{path}.sample_coverage_sha256",
                ),
                "artifact_sha256": require_sha256(
                    row["artifact_sha256"],
                    path=f"{path}.artifact_sha256",
                ),
            }
        )
    return result


def _derive_pair_evidence(
    manifest: Mapping[str, Any],
    *,
    completed_wave: str,
    method_stage_artifacts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expected_cluster_ids = list(manifest["waves"][completed_wave]["cluster_ids"])
    rows_by_method: dict[str, list[Mapping[str, Any]]] = {
        "sf_bt": [],
        "pj": [],
    }
    for artifact in method_stage_artifacts:
        payload = artifact["payload"]
        rows_by_method[payload["method_id"]].extend(payload["cluster_rows"])
    for method_id, rows in rows_by_method.items():
        observed_cluster_ids = [row["cluster_id"] for row in rows]
        if observed_cluster_ids != expected_cluster_ids:
            raise ContractValidationError(
                "wave_cumulative",
                f"$.method_stage_artifacts.{method_id}",
                "Wave B evidence must be Wave A followed by the exact 50-cluster addition",
            )
    result: list[dict[str, Any]] = []
    for pair_index, (pair_id, arm_1_id, arm_2_id) in enumerate(_pair_specs()):
        methods: list[dict[str, Any]] = []
        for method_id in ("sf_bt", "pj"):
            deltas = [
                row["pair_rows"][pair_index]["delta_arm_1_minus_arm_2"]
                for row in rows_by_method[method_id]
            ]
            mean, low, high = _mean_ci95(deltas)
            methods.append(
                {
                    "method_id": method_id,
                    "delta": mean,
                    "ci95_low": low,
                    "ci95_high": high,
                    "unit_count": len(deltas),
                }
            )
        result.append(
            {
                "pair_id": pair_id,
                "arm_1_id": arm_1_id,
                "arm_2_id": arm_2_id,
                "methods": methods,
            }
        )
    return _validate_pair_evidence(
        result,
        expected_unit_count=len(expected_cluster_ids),
    )


def _mean_ci95(values: Sequence[int | float]) -> tuple[float, float, float]:
    if not values:
        raise ContractValidationError(
            "unit_count",
            "$.method_stage_artifacts",
            "cannot derive confidence interval from zero scorer units",
        )
    numbers = [float(value) for value in values]
    mean = math.fsum(numbers) / len(numbers)
    if len(numbers) == 1:
        margin = 0.0
    else:
        squared = math.fsum((value - mean) ** 2 for value in numbers)
        sample_variance = squared / (len(numbers) - 1)
        margin = 1.96 * math.sqrt(sample_variance / len(numbers))
    point = _rounded_float(mean)
    low = _rounded_float(mean - margin)
    high = _rounded_float(mean + margin)
    return point, min(low, point), max(high, point)


def _rounded_float(value: float) -> float:
    rounded = round(value, 12)
    return 0.0 if rounded == 0 else rounded


def _validate_pair_evidence(
    value: Any,
    *,
    expected_unit_count: int,
) -> list[dict[str, Any]]:
    path = "$.pair_evidence"
    rows = require_list(value, path=path) if not isinstance(value, tuple) else list(value)
    expected_pairs = [
        (first, second)
        for first, second in itertools.combinations(BENCHMARK_ARM_IDS_V1, 2)
    ]
    if len(rows) != len(expected_pairs):
        raise ContractValidationError(
            "pair_exact_cover", path, "evidence must cover all ten unordered arm pairs"
        )
    result: list[dict[str, Any]] = []
    for index, ((first, second), raw) in enumerate(zip(expected_pairs, rows, strict=True)):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        require_exact_keys(
            row,
            required={"pair_id", "arm_1_id", "arm_2_id", "methods"},
            path=item_path,
        )
        expected_pair_id = f"{first}__{second}"
        methods = require_list(row["methods"], path=f"{item_path}.methods")
        if len(methods) != 2:
            raise ContractValidationError(
                "method_exact_cover",
                f"{item_path}.methods",
                "each pair requires BTF and MTQ-5 evidence",
            )
        normalized_methods = []
        for method_index, (expected_method, raw_method) in enumerate(
            zip(("sf_bt", "pj"), methods, strict=True)
        ):
            method_path = f"{item_path}.methods[{method_index}]"
            method = require_mapping(raw_method, path=method_path)
            require_exact_keys(
                method,
                required={"method_id", "delta", "ci95_low", "ci95_high", "unit_count"},
                path=method_path,
            )
            low = require_number(method["ci95_low"], path=f"{method_path}.ci95_low")
            high = require_number(method["ci95_high"], path=f"{method_path}.ci95_high")
            delta = require_number(method["delta"], path=f"{method_path}.delta")
            if low > high or not low <= delta <= high:
                raise ContractValidationError(
                    "confidence_interval",
                    method_path,
                    "CI bounds must be ordered and contain the point estimate",
                )
            normalized_methods.append(
                {
                    "method_id": require_enum(
                        method["method_id"], {expected_method}, path=f"{method_path}.method_id"
                    ),
                    "delta": delta,
                    "ci95_low": low,
                    "ci95_high": high,
                    "unit_count": require_int(
                        method["unit_count"], path=f"{method_path}.unit_count", minimum=1
                    ),
                }
            )
            if normalized_methods[-1]["unit_count"] != expected_unit_count:
                raise ContractValidationError(
                    "unit_count",
                    f"{method_path}.unit_count",
                    "method evidence must use the exact cumulative wave cluster count",
                )
        result.append(
            {
                "pair_id": require_enum(
                    row["pair_id"], {expected_pair_id}, path=f"{item_path}.pair_id"
                ),
                "arm_1_id": require_enum(
                    row["arm_1_id"], {first}, path=f"{item_path}.arm_1_id"
                ),
                "arm_2_id": require_enum(
                    row["arm_2_id"], {second}, path=f"{item_path}.arm_2_id"
                ),
                "methods": normalized_methods,
            }
        )
    return result


def _validate_logical_work(value: Any) -> dict[str, int]:
    path = "$work_plan.logical_work"
    row = require_mapping(value, path=path)
    keys = {
        "dtq_full_rows",
        "terminology_full_blocks",
        "btf_sampled_rows",
        "btf_incremental_rows",
        "mtq5_cluster_pair_orientations",
        "mtq5_incremental_cluster_pair_orientations",
    }
    require_exact_keys(row, required=keys, path=path)
    return {
        key: require_int(row[key], path=f"{path}.{key}", minimum=0)
        for key in keys
    }


def _validate_stages(value: Any) -> list[dict[str, Any]]:
    path = "$work_plan.stages"
    rows = require_list(value, path=path)
    result = []
    for index, raw in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        require_exact_keys(
            row,
            required={"stage_id", "ordinal", "agent", "conditional"},
            path=item_path,
        )
        conditional = row["conditional"]
        if not isinstance(conditional, bool):
            raise ContractValidationError(
                "type", f"{item_path}.conditional", "conditional must be boolean"
            )
        result.append(
            {
                "stage_id": require_string(row["stage_id"], path=f"{item_path}.stage_id"),
                "ordinal": require_int(
                    row["ordinal"], path=f"{item_path}.ordinal", minimum=0
                ),
                "agent": require_string(row["agent"], path=f"{item_path}.agent"),
                "conditional": conditional,
            }
        )
    if [item["ordinal"] for item in result] != list(range(len(result))):
        raise ContractValidationError(
            "stage_order", path, "stage ordinals must be contiguous from zero"
        )
    require_unique([item["stage_id"] for item in result], path=f"{path}.stage_id")
    return result


def _validate_producer(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"workstream", "component", "component_version", "code_commit"},
        path=path,
    )
    return {
        "workstream": require_enum(
            row["workstream"], {"evaluation"}, path=f"{path}.workstream"
        ),
        "component": require_enum(
            row["component"], {"two_wave_sampling_v1"}, path=f"{path}.component"
        ),
        "component_version": require_enum(
            row["component_version"],
            {SAMPLING_SCHEMA_VERSION, UNCERTAINTY_SCHEMA_VERSION},
            path=f"{path}.component_version",
        ),
        "code_commit": require_commit(row["code_commit"], path=f"{path}.code_commit"),
    }


def _chapter_int_map(
    value: Any, *, path: str, maximum: int | None
) -> dict[str, int]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=TWO_WAVE_CHAPTER_IDS_V1, path=path)
    result = {}
    for chapter_id in TWO_WAVE_CHAPTER_IDS_V1:
        item = require_int(row[chapter_id], path=f"{path}.{chapter_id}", minimum=0)
        if maximum is not None and item > maximum:
            raise ContractValidationError(
                "range", f"{path}.{chapter_id}", f"must be <= {maximum}"
            )
        result[chapter_id] = item
    return result


def _enum_list(value: Any, expected: Sequence[str], *, path: str) -> list[str]:
    rows = require_list(value, path=path)
    result = [
        require_enum(item, expected, path=f"{path}[{index}]")
        for index, item in enumerate(rows)
    ]
    if result != list(expected):
        raise ContractValidationError(
            "sequence", path, "values must match the exact registered order"
        )
    return result


def _string_list(value: Any, *, path: str) -> list[str]:
    rows = require_list(value, path=path)
    result = [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(rows)
    ]
    require_unique(result, path=path)
    return result


def _one_hash(value: Any, field: str, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={field}, path=path)
    return {field: require_sha256(row[field], path=f"{path}.{field}")}


def _direction(value: int | float) -> str:
    return "positive" if value > 0 else "negative" if value < 0 else "tie"


def _uncertainty_outcome(
    completed_wave: str, pair_evidence: Sequence[Mapping[str, Any]]
) -> tuple[str, list[str], str]:
    reasons: list[str] = []
    for pair in pair_evidence:
        methods = {method["method_id"]: method for method in pair["methods"]}
        for method in methods.values():
            if method["ci95_low"] <= 0 <= method["ci95_high"]:
                reasons.append(
                    f"{pair['pair_id']}:{method['method_id']}:ci_includes_zero"
                )
        if _direction(methods["sf_bt"]["delta"]) != _direction(
            methods["pj"]["delta"]
        ):
            reasons.append(f"{pair['pair_id']}:btf_mtq5_direction_disagrees")
    normalized_reasons = sorted(set(reasons))
    unresolved = bool(normalized_reasons)
    decision = (
        "open_wave_b"
        if completed_wave == "wave_a" and unresolved
        else "stop_conclusive"
        if not unresolved
        else "stop_inconclusive"
    )
    headline_status = (
        "INCONCLUSIVE"
        if decision == "stop_inconclusive"
        else "PENDING_WAVE_B"
        if decision == "open_wave_b"
        else "METHOD_RESULTS_AVAILABLE"
    )
    return decision, normalized_reasons, headline_status


def _work_plan_fields(
    manifest: Mapping[str, Any],
    *,
    active_wave: str,
    arm_ids: tuple[str, ...],
) -> dict[str, Any]:
    scope = manifest["waves"][active_wave]
    incremental_cluster_ids = (
        list(scope["cluster_ids"])
        if active_wave == "wave_a"
        else list(
            scope["cluster_ids"][
                manifest["waves"]["wave_a"]["cluster_count"] :
            ]
        )
    )
    cluster_by_id = {
        cluster["cluster_id"]: cluster for cluster in manifest["clusters"]
    }
    incremental_block_ids = [
        block_id
        for cluster_id in incremental_cluster_ids
        for block_id in cluster_by_id[cluster_id]["block_ids"]
    ]
    pair_ids = [
        f"{first}__{second}"
        for first, second in itertools.combinations(arm_ids, 2)
    ]
    return {
        "selected_chapter_ids": list(TWO_WAVE_CHAPTER_IDS_V1),
        "active_cluster_ids": list(scope["cluster_ids"]),
        "active_block_ids": list(scope["block_ids"]),
        "incremental_cluster_ids": incremental_cluster_ids,
        "incremental_block_ids": incremental_block_ids,
        "pair_ids": pair_ids,
        "logical_work": {
            "dtq_full_rows": manifest["population"]["eligible_block_count"]
            * len(arm_ids),
            "terminology_full_blocks": manifest["population"][
                "eligible_block_count"
            ],
            "btf_sampled_rows": scope["block_count"] * len(arm_ids),
            "btf_incremental_rows": len(incremental_block_ids) * len(arm_ids),
            "mtq5_cluster_pair_orientations": scope["cluster_count"]
            * len(pair_ids)
            * 2,
            "mtq5_incremental_cluster_pair_orientations": (
                len(incremental_cluster_ids) * len(pair_ids) * 2
            ),
        },
        "stages": list(two_wave_workflow_stages_v1()),
    }


def _stable_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
