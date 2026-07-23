from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.benchmark_v1 import (
    build_benchmark_manifest_v1,
    build_benchmark_preflight_v1,
    build_overlay_from_common_arm_v1,
    slice_common_input_chapter_v1,
)
from pipeline.eval.canonical_d2l_benchmark_bridge_v1 import (
    FinalizedCanonicalSourceArtifactsV1,
    build_canonical_d2l_common_input_v1,
)
from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    CommonTranslationV1,
    build_common_evaluation_input,
    source_binding_to_dict,
    validate_translation_artifact,
)
from pipeline.eval.community_aligned_translation_v1 import (
    COMMUNITY_ALIGNED_TRANSLATION_SCHEMA_ID,
    build_common_aligned_evaluation_input_v1,
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_int,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
)
from pipeline.eval.end_to_end_runner_v1 import LocalSfQeRuntimeV1
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
)
from pipeline.eval.local_sf_qe_v1 import BatchPredictorV1, SF_QE_MODEL_ID
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config
from pipeline.eval.workflow_executor_v1 import (
    EvaluationWorkflowExecutorRegistrationV1,
    RegisteredEvaluationWorkflowExecutorV1,
)
from pipeline.eval.workflow_runtime_bundle_v1 import (
    EvaluationRuntimeObjectRegistryV1,
    LoadedWorkflowScoringBaselineTemplateV1,
    LoadedWorkflowScoringRuntimeV1,
    WorkflowScoringBaselineTemplateSourcesV1,
    WorkflowScoringRuntimeArtifactSourcesV1,
    build_evaluation_workflow_registration_from_baseline_template_v1,
    build_registered_evaluation_workflow_executor_v1,
    load_workflow_scoring_baseline_template_from_workflow_runtime_v1,
    load_workflow_scoring_runtime_bundle_v1,
    materialize_workflow_scoring_baseline_template_v1,
    materialize_workflow_scoring_runtime_bundle_v1,
)
from pipeline.eval.workflow_component_v1 import (
    SOURCE_BINDING_ROLES_V1,
    validate_scoring_handoff_v1,
    validate_typed_artifact_binding_v1,
)
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    canonical_sha256 as shared_canonical_sha256,
    resolve_source_credential,
    validate_api_source,
    validate_capability_evidence,
    validate_pipeline_profile,
)
from pipeline.llm_backend.credentials_v1 import CredentialProvider
from pipeline.llm_backend.transport_v1 import TransportSender
from pipeline.workflow_replay.contracts_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
)
from pipeline.workflow_replay.orchestrator_v1 import (
    validate_workflow_runtime_registration_v1,
)


__all__ = [
    "BuiltEvaluationExecutorRuntimeV1",
    "BuiltEvaluationRuntimeRegistryV1",
    "EvaluationServerRuntimeConfigV1",
    "PreparedEvaluationProductionRuntimeV1",
    "PreparedEvaluationRuntimeBundleV1",
    "RegisteredEvaluationBaselineTemplateV1",
    "build_evaluation_executor_runtime_v1",
    "build_evaluation_registered_option_facts_v1",
    "build_evaluation_runtime_object_registry_v1",
    "prepare_evaluation_production_runtime_v1",
    "prepare_evaluation_runtime_bundle_v1",
    "register_evaluation_baseline_template_v1",
]


_WORKFLOW_RUNTIME_FILE_NAME = "workflow_runtime_v1.json"
_BASELINE_TEMPLATE_RELATIVE_ROOT = "workflow/evaluation_baseline_template"
_RUNTIME_IDENTITY_RELATIVE_PATH = "_runtime/runtime_identity_v1.json"
_RUNTIME_ARTIFACT_STORE_RELATIVE_PATH = "_runtime/raw_responses"


def build_evaluation_registered_option_facts_v1(
    authority: EvaluationWorkflowSettingsAuthorityV1,
    *,
    evaluation_profile_ref: str,
    policy_profile_ref: str | None,
    shared_selection_ref: str,
) -> dict[str, Any]:
    """Build the exact server-advertised facts sealed before Translation."""

    def normalized_binding(value: Mapping[str, Any], *, path: str) -> dict[str, str]:
        binding = validate_typed_artifact_binding_v1(value, path=path)
        if binding["sha256_kind"] != "physical":
            raise ContractValidationError(
                "runtime_file_authority",
                f"{path}.sha256_kind",
                "registered Evaluation authority must use physical hashes",
            )
        return binding

    profiles = [
        normalized_binding(row, path=f"$.evaluation_profiles[{index}]")
        for index, row in enumerate(authority.evaluation_profiles)
    ]
    policies = [
        normalized_binding(row, path=f"$.policy_profiles[{index}]")
        for index, row in enumerate(authority.policy_profiles)
    ]
    selections = [
        normalized_binding(row, path=f"$.shared_selections[{index}]")
        for index, row in enumerate(authority.shared_selections)
    ]

    def catalog_member(
        rows: Sequence[Mapping[str, str]],
        artifact_ref: str,
        *,
        path: str,
    ) -> dict[str, str]:
        ref = require_relative_path(artifact_ref, path=path)
        matches = [dict(row) for row in rows if row["artifact_ref"] == ref]
        if len(matches) != 1:
            raise ContractValidationError(
                "authority_binding",
                path,
                "registered artifact reference is missing or ambiguous",
            )
        return matches[0]

    profile = catalog_member(
        profiles,
        evaluation_profile_ref,
        path="$.evaluation_profile_ref",
    )
    policy = (
        None
        if policy_profile_ref is None
        else catalog_member(
            policies,
            policy_profile_ref,
            path="$.policy_profile_ref",
        )
    )
    selection = catalog_member(
        selections,
        shared_selection_ref,
        path="$.shared_selection_ref",
    )
    return {
        "settings_schema_id": "EvaluationWorkflowSettingsV1",
        "settings_schema_version": "1.1.0",
        "arm_ids": list(authority.arm_ids),
        "scorer_ids": list(authority.scorer_ids),
        "aggregation_policy": authority.aggregation_policy_id,
        "report_policy": authority.report_policy_id,
        "verdict_policy": authority.verdict_policy_id,
        "scoring_handoff_authority": (
            "exact_ordered_five_arm_scoring_handoff_v1"
        ),
        "registered_authority": {
            "status": "ready",
            "benchmark_preset_ref": normalized_binding(
                authority.benchmark_preset,
                path="$.benchmark_preset",
            ),
            "evaluation_config_ref": normalized_binding(
                authority.evaluation_config,
                path="$.evaluation_config",
            ),
            "scorer_set_ref": normalized_binding(
                authority.scorer_set,
                path="$.scorer_set",
            ),
            "evaluation_profile_ref": profile,
            "policy_profile_ref": policy,
            "shared_selection_ref": selection,
        },
    }
_RUNTIME_CACHE_RELATIVE_PATH = "_runtime/response_cache.sqlite3"
_RUNTIME_QUOTA_RELATIVE_PATH = "_runtime/quota_leases"
_PREPARED_INPUTS_RELATIVE_ROOT = "_runtime/prepared_inputs_v1"
_ARM_ORDER = ("s0", "s1", "community", "google_nmt", "llm_lc")
_BENCHMARK_ARM_BY_SETTINGS_ARM = {
    "s0": "S0",
    "s1": "S1",
    "community": "community",
    "google_nmt": "google_nmt",
    "llm_lc": "llm_lc",
}
_BENCHMARK_ROLE_BY_ARM = {
    "S0": "pipeline_ablation",
    "S1": "thesis_system",
    "community": "human_community",
    "google_nmt": "conventional_nmt",
    "llm_lc": "long_context_diagnostic",
}
_METHOD_METADATA = {
    "sf_qe": {
        "display_name": "Semantic fidelity QE",
        "method_version": "sf_qe_cometkiwi_native_x100_v1",
        "scorer_kind": "unary",
    },
    "sf_bt": {
        "display_name": "Semantic fidelity back-translation",
        "method_version": "sf_bt_reverse_v3_1_semantic_v3",
        "scorer_kind": "unary",
    },
    "pj": {
        "display_name": "Pairwise judge",
        "method_version": "pj_common_v2_counterbalanced_v1",
        "scorer_kind": "pairwise",
    },
}


@dataclass(frozen=True, slots=True)
class RegisteredEvaluationBaselineTemplateV1:
    workflow_runtime_path: Path
    baseline_template_path: Path
    loaded_template: LoadedWorkflowScoringBaselineTemplateV1


@dataclass(frozen=True, slots=True)
class PreparedEvaluationRuntimeBundleV1:
    baseline_template: LoadedWorkflowScoringBaselineTemplateV1
    registration: EvaluationWorkflowExecutorRegistrationV1
    bundle_path: Path
    loaded_runtime: LoadedWorkflowScoringRuntimeV1


@dataclass(frozen=True, slots=True)
class PreparedEvaluationProductionRuntimeV1:
    prepared_bundle: PreparedEvaluationRuntimeBundleV1
    executor_runtime: BuiltEvaluationExecutorRuntimeV1
    prepared_inputs_root: Path


@dataclass(frozen=True, slots=True)
class EvaluationServerRuntimeConfigV1:
    local_sf_qe_predictor: BatchPredictorV1 | None = None
    local_sf_qe_checkpoint_sha256: str | None = None
    local_sf_qe_package_name: str | None = None
    local_sf_qe_package_version: str | None = None
    local_sf_qe_device: str | None = None
    local_sf_qe_batch_size: int | None = None
    llm_profile: Mapping[str, Any] | None = None
    api_sources: Sequence[Mapping[str, Any]] = ()
    capability_evidence: Sequence[Mapping[str, Any]] = ()
    credential_provider: CredentialProvider | None = None
    sender: TransportSender | None = None
    cache_mode: str = "read_write"
    cost_fact: Mapping[str, Any] | None = None
    clock: Callable[[], datetime] | None = None
    monotonic: Callable[[], float] | None = None


@dataclass(frozen=True, slots=True)
class BuiltEvaluationRuntimeRegistryV1:
    loaded_runtime: LoadedWorkflowScoringRuntimeV1
    registry: EvaluationRuntimeObjectRegistryV1
    runtime_identity_path: Path
    runtime_identity: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BuiltEvaluationExecutorRuntimeV1:
    executor: RegisteredEvaluationWorkflowExecutorV1
    scoring_handoff: Mapping[str, Any]
    workflow_settings: Mapping[str, Any]
    registered_option: Mapping[str, Any]
    runtime_registry: BuiltEvaluationRuntimeRegistryV1


def register_evaluation_baseline_template_v1(
    job_root: Path,
    *,
    job_id: str,
    source_binding_sha256: str,
    supported_chapter_ids: Sequence[str],
    template_id: str,
    created_at: str,
    producer_code_commit: str,
    settings_option_id: str,
    registered_option_sha256: str,
    evaluation_profile_id: str,
    evaluation_profile_ref: str,
    policy_profile_id: str | None,
    policy_profile_ref: str | None,
    shared_selection_ref: str,
    settings_authority: EvaluationWorkflowSettingsAuthorityV1,
    artifact_sources: WorkflowScoringBaselineTemplateSourcesV1,
    caveats: Sequence[str] = (),
) -> RegisteredEvaluationBaselineTemplateV1:
    """Register pre-run baseline authority from explicit, accepted files only."""

    root = Path(job_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    chapters = _ordered_nonempty_strings(
        supported_chapter_ids, path="$.supported_chapter_ids"
    )
    source_sha = require_sha256(
        source_binding_sha256, path="$.source_binding_sha256"
    )
    template_root = _contained_path(root, _BASELINE_TEMPLATE_RELATIVE_ROOT)
    template_path = materialize_workflow_scoring_baseline_template_v1(
        template_root,
        template_id=require_string(template_id, path="$.template_id"),
        created_at=require_rfc3339(created_at, path="$.created_at"),
        producer_code_commit=require_commit(
            producer_code_commit, path="$.producer_code_commit"
        ),
        source_binding_sha256=source_sha,
        settings_option_id=require_string(
            settings_option_id, path="$.settings_option_id"
        ),
        registered_option_sha256=require_sha256(
            registered_option_sha256, path="$.registered_option_sha256"
        ),
        evaluation_profile_id=require_string(
            evaluation_profile_id, path="$.evaluation_profile_id"
        ),
        evaluation_profile_ref=require_relative_path(
            evaluation_profile_ref, path="$.evaluation_profile_ref"
        ),
        policy_profile_id=policy_profile_id,
        policy_profile_ref=policy_profile_ref,
        shared_selection_ref=require_relative_path(
            shared_selection_ref, path="$.shared_selection_ref"
        ),
        settings_authority=settings_authority,
        artifact_sources=artifact_sources,
        caveats=caveats,
    )
    loaded = load_workflow_scoring_baseline_from_explicit_path_v1(template_path)
    if loaded.template["supported_chapter_ids"] != chapters:
        raise ContractValidationError(
            "chapter_scope",
            "$.supported_chapter_ids",
            "runtime registration must exact-match baseline authority chapters",
        )

    registration = {
        "schema_id": "WorkflowRuntimeRegistrationV1",
        "schema_version": "1.0.0",
        "job_id": require_string(job_id, path="$.job_id"),
        "source_binding_sha256": source_sha,
        "translation_executor_id": "d2l_project_campaign_v1",
        "baseline_bundle": {
            "arm_ids": ["community", "google_nmt", "llm_lc"],
            "artifact_ref": (
                f"{_BASELINE_TEMPLATE_RELATIVE_ROOT}/"
                f"{template_path.name}"
            ),
            "sha256": physical_sha256(template_path.read_bytes()),
            "sha256_kind": "physical",
            "status": "ready",
        },
        "evaluation_executor_id": "evaluation_five_arm_benchmark_v1",
        "publication_executor_id": "selected_chapter_publication_v1",
        "supported_chapter_ids": chapters,
        "status": "ready",
        "blockers": [],
        "integrity": {"registration_sha256": "0" * 64},
    }
    unhashed = copy.deepcopy(registration)
    unhashed["integrity"].pop("registration_sha256")
    registration["integrity"]["registration_sha256"] = canonical_sha256(unhashed)
    normalized = validate_workflow_runtime_registration_v1(
        registration,
        expected_job_id=registration["job_id"],
        expected_source_binding_sha256=source_sha,
        selected_chapter_ids=chapters,
    )
    runtime_path = root / _WORKFLOW_RUNTIME_FILE_NAME
    _persist_bytes_create_or_equal(
        runtime_path, canonical_json_bytes(normalized) + b"\n"
    )
    loaded_from_registration = (
        load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
            root,
            expected_job_id=registration["job_id"],
            expected_source_binding_sha256=source_sha,
            selected_chapter_ids=chapters,
        )
    )
    return RegisteredEvaluationBaselineTemplateV1(
        workflow_runtime_path=runtime_path,
        baseline_template_path=template_path,
        loaded_template=loaded_from_registration,
    )


def prepare_evaluation_runtime_bundle_v1(
    *,
    job_root: Path,
    expected_job_id: str,
    expected_source_binding_sha256: str,
    selected_chapter_ids: Sequence[str],
    scoring_handoff_path: Path,
    locked_selection: Mapping[str, Any],
    workflow_run_id: str,
    component_run_id: str,
    evaluation_output_root: Path,
    runtime_bundle_root: Path,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    artifact_sources: WorkflowScoringRuntimeArtifactSourcesV1,
    arm_presentations: Sequence[Mapping[str, Any]],
    method_presentations: Sequence[Mapping[str, Any]],
    chapter_runtime_bindings: Sequence[Mapping[str, Any]],
    caveats: Sequence[str] = (),
) -> PreparedEvaluationRuntimeBundleV1:
    """Materialize exact Settings 1.1 and a run-specific bundle after Translation."""

    explicit_handoff = Path(scoring_handoff_path).resolve()
    if explicit_handoff != Path(artifact_sources.scoring_handoff).resolve():
        raise ContractValidationError(
            "handoff_binding",
            "$.artifact_sources.scoring_handoff",
            "runtime sources must use the exact handoff supplied to preparation",
        )
    handoff = _read_json(explicit_handoff)
    loaded_template = (
        load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
            Path(job_root),
            expected_job_id=require_string(
                expected_job_id, path="$.expected_job_id"
            ),
            expected_source_binding_sha256=require_sha256(
                expected_source_binding_sha256,
                path="$.expected_source_binding_sha256",
            ),
            selected_chapter_ids=selected_chapter_ids,
        )
    )
    registration = build_evaluation_workflow_registration_from_baseline_template_v1(
        loaded_template,
        scoring_handoff=handoff,
        locked_selection=locked_selection,
        workflow_run_id=require_string(
            workflow_run_id, path="$.workflow_run_id"
        ),
        component_run_id=require_string(
            component_run_id, path="$.component_run_id"
        ),
        output_root=Path(evaluation_output_root),
        generated_at=require_rfc3339(generated_at, path="$.generated_at"),
        producer_code_commit=require_commit(
            producer_code_commit, path="$.producer_code_commit"
        ),
        evaluation_logical_run_id=require_string(
            evaluation_logical_run_id, path="$.evaluation_logical_run_id"
        ),
        evaluation_attempt_run_id=require_string(
            evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
        ),
    )
    bundle_path = materialize_workflow_scoring_runtime_bundle_v1(
        Path(runtime_bundle_root),
        registration=registration,
        artifact_sources=artifact_sources,
        arm_presentations=arm_presentations,
        method_presentations=method_presentations,
        chapter_runtime_bindings=chapter_runtime_bindings,
        caveats=caveats,
    )
    loaded_runtime = load_workflow_scoring_runtime_bundle_v1(bundle_path)
    return PreparedEvaluationRuntimeBundleV1(
        baseline_template=loaded_template,
        registration=registration,
        bundle_path=bundle_path,
        loaded_runtime=loaded_runtime,
    )


def prepare_evaluation_production_runtime_v1(
    *,
    job_root: Path,
    expected_job_id: str,
    expected_source_binding_sha256: str,
    scoring_handoff_path: Path,
    producer_handoff_artifacts: Mapping[str, Path],
    locked_selection: Mapping[str, Any],
    workflow_run_id: str,
    component_run_id: str,
    evaluation_output_root: Path,
    runtime_bundle_root: Path,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    server_runtime: EvaluationServerRuntimeConfigV1,
    caveats: Sequence[str] = (),
) -> PreparedEvaluationProductionRuntimeV1:
    """Prepare every Evaluation-owned runtime artifact from sealed producer facts.

    The neutral caller supplies only the parent handoff, explicit producer files,
    the locked selection, run identities, and server-owned runtime objects.
    Benchmark manifests, overlays, chapter configs, presentations, and runtime
    bindings are derived here and never synthesized by App or relay code.
    """

    raw_chapters = locked_selection.get("selected_chapter_ids")
    if not isinstance(raw_chapters, (list, tuple)):
        raise ContractValidationError(
            "type",
            "$.locked_selection.selected_chapter_ids",
            "locked selection must carry an ordered chapter list",
        )
    selected_chapters = tuple(
        _ordered_nonempty_strings(
            raw_chapters,
            path="$.locked_selection.selected_chapter_ids",
        )
    )
    handoff_path = Path(scoring_handoff_path).resolve()
    handoff = validate_scoring_handoff_v1(_read_json(handoff_path))
    loaded_template = (
        load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
            Path(job_root),
            expected_job_id=require_string(
                expected_job_id, path="$.expected_job_id"
            ),
            expected_source_binding_sha256=require_sha256(
                expected_source_binding_sha256,
                path="$.expected_source_binding_sha256",
            ),
            selected_chapter_ids=selected_chapters,
        )
    )
    registration = build_evaluation_workflow_registration_from_baseline_template_v1(
        loaded_template,
        scoring_handoff=handoff,
        locked_selection=locked_selection,
        workflow_run_id=require_string(
            workflow_run_id, path="$.workflow_run_id"
        ),
        component_run_id=require_string(
            component_run_id, path="$.component_run_id"
        ),
        output_root=Path(evaluation_output_root),
        generated_at=require_rfc3339(generated_at, path="$.generated_at"),
        producer_code_commit=require_commit(
            producer_code_commit, path="$.producer_code_commit"
        ),
        evaluation_logical_run_id=require_string(
            evaluation_logical_run_id, path="$.evaluation_logical_run_id"
        ),
        evaluation_attempt_run_id=require_string(
            evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
        ),
    )
    settings = registration.materialized_workflow_settings
    if tuple(settings["selected_chapter_ids"]) != selected_chapters:
        raise ContractValidationError(
            "chapter_scope",
            "$.locked_selection.selected_chapter_ids",
            "materialized settings changed the locked chapter order",
        )
    if tuple(handoff["translation_inputs"][index]["arm_id"] for index in range(5)) != _ARM_ORDER:
        raise ContractValidationError(
            "arm_order",
            "$handoff.translation_inputs",
            "production preparation requires the exact five-arm order",
        )

    handoff_artifacts = _resolve_handoff_artifact_paths(
        handoff=handoff,
        loaded_template=loaded_template,
        producer_handoff_artifacts=producer_handoff_artifacts,
    )
    common = _build_five_arm_common_input(
        handoff=handoff,
        selected_chapter_ids=selected_chapters,
        handoff_artifacts=handoff_artifacts,
    )
    selected_settings_arms = tuple(settings["selected_arm_ids"])
    selected_benchmark_arms = tuple(
        _BENCHMARK_ARM_BY_SETTINGS_ARM[row] for row in selected_settings_arms
    )
    scoped_common = _select_common_arms(
        common,
        selected_benchmark_arms=selected_benchmark_arms,
    )

    selected_scorers = tuple(settings["selected_scorer_ids"])
    _validate_local_runtime_config(
        server_runtime, required="sf_qe" in selected_scorers
    )
    llm_material = _validate_llm_runtime_config(
        server_runtime,
        selected_scorers=selected_scorers,
        required=any(row in {"sf_bt", "pj"} for row in selected_scorers),
    )
    method_definitions, method_presentations = _build_method_material(
        selected_scorers=selected_scorers,
        producer_code_commit=producer_code_commit,
        llm_material=llm_material,
    )
    arm_presentations = _build_arm_presentations(selected_settings_arms)
    chapter_runtime_bindings = _build_chapter_runtime_bindings(
        selected_chapters=selected_chapters,
        selected_scorers=selected_scorers,
    )

    prepared_root = _contained_path(
        Path(evaluation_output_root), _PREPARED_INPUTS_RELATIVE_ROOT
    )
    prepared_root.mkdir(parents=True, exist_ok=True)
    sources: list[CommonSourceSnapshotV1] = []
    source_evidence: list[dict[str, Any]] = []
    package_seal_binding = next(
        row["binding"]
        for row in handoff["source_package_bindings"]
        if row["role"] == "package_seal"
    )
    for chapter_id in selected_chapters:
        chapter_common = slice_common_input_chapter_v1(scoped_common, chapter_id)
        sources.append(_source_snapshot_from_common(chapter_common))
        source_evidence.append(
            {
                "chapter_id": chapter_id,
                "source_artifact_id": (
                    "canonical-source-package-"
                    + package_seal_binding["sha256"][:24]
                ),
                "source_artifact_sha256": package_seal_binding["sha256"],
                "source_evidence_kind": "canonical_source_package_v1",
            }
        )

    manifest = build_benchmark_manifest_v1(
        sources,
        source_evidence,
        benchmark_id=(
            "evaluation-"
            + canonical_sha256(
                {
                    "workflow_run_id": workflow_run_id,
                    "component_run_id": component_run_id,
                    "settings_sha256": settings["settings_sha256"],
                }
            )[:24]
        ),
        created_at=generated_at,
        producer_code_commit=producer_code_commit,
        selected_chapter_ids=selected_chapters,
        selected_arm_ids=selected_benchmark_arms,
    )
    manifest_path = _persist_json_artifact(
        prepared_root / "benchmark_manifest.json", manifest
    )

    overlay_paths: dict[tuple[str, str], Path] = {}
    overlay_payloads: list[Mapping[str, Any]] = []
    for chapter_id in selected_chapters:
        for arm_id in selected_benchmark_arms:
            overlay = build_overlay_from_common_arm_v1(
                scoped_common,
                chapter_id=chapter_id,
                arm_id=arm_id,
                benchmark_role=_BENCHMARK_ROLE_BY_ARM[arm_id],
                created_at=generated_at,
                producer_code_commit=producer_code_commit,
            )
            overlay_paths[(chapter_id, arm_id)] = _persist_json_artifact(
                prepared_root / "overlays" / chapter_id / f"{arm_id}.json",
                overlay,
            )
            overlay_payloads.append(overlay)
    preflight = build_benchmark_preflight_v1(
        manifest,
        sources,
        overlay_payloads,
        created_at=generated_at,
        producer_code_commit=producer_code_commit,
    )
    if preflight["status"] != "ready":
        raise ContractValidationError(
            "benchmark_preflight",
            "$.benchmark_preflight.status",
            "five-arm benchmark is not exact-cover ready",
        )
    preflight_path = _persist_json_artifact(
        prepared_root / "benchmark_preflight.json", preflight
    )

    comparison_pairs = _build_pairwise_round_robin(
        selected_benchmark_arms
    ) if "pj" in selected_scorers else []
    chapter_configs: dict[str, Path] = {}
    for chapter_id in selected_chapters:
        chapter_common = slice_common_input_chapter_v1(
            scoped_common, chapter_id
        )
        config = _build_chapter_config(
            chapter_common,
            workflow_run_id=workflow_run_id,
            component_run_id=component_run_id,
            chapter_id=chapter_id,
            generated_at=generated_at,
            producer_code_commit=producer_code_commit,
            methods=method_definitions,
            comparison_pairs=comparison_pairs,
        )
        chapter_configs[chapter_id] = _persist_json_artifact(
            prepared_root / "chapter_configs" / f"{chapter_id}.json",
            config,
        )

    authority_artifacts = _template_authority_artifact_paths(
        loaded_template
    )
    artifact_sources = WorkflowScoringRuntimeArtifactSourcesV1(
        scoring_handoff=handoff_path,
        benchmark_manifest=manifest_path,
        benchmark_preflight=preflight_path,
        handoff_artifacts=handoff_artifacts,
        authority_artifacts=authority_artifacts,
        overlays=overlay_paths,
        chapter_configs=chapter_configs,
        d2l_evaluation_input=None,
    )
    prepared = prepare_evaluation_runtime_bundle_v1(
        job_root=job_root,
        expected_job_id=expected_job_id,
        expected_source_binding_sha256=expected_source_binding_sha256,
        selected_chapter_ids=selected_chapters,
        scoring_handoff_path=handoff_path,
        locked_selection=locked_selection,
        workflow_run_id=workflow_run_id,
        component_run_id=component_run_id,
        evaluation_output_root=evaluation_output_root,
        runtime_bundle_root=runtime_bundle_root,
        generated_at=generated_at,
        producer_code_commit=producer_code_commit,
        evaluation_logical_run_id=evaluation_logical_run_id,
        evaluation_attempt_run_id=evaluation_attempt_run_id,
        artifact_sources=artifact_sources,
        arm_presentations=arm_presentations,
        method_presentations=method_presentations,
        chapter_runtime_bindings=chapter_runtime_bindings,
        caveats=caveats,
    )
    executor_runtime = build_evaluation_executor_runtime_v1(
        prepared.bundle_path,
        evaluation_output_root=evaluation_output_root,
        server_runtime=server_runtime,
    )
    if (
        executor_runtime.workflow_settings["settings_sha256"]
        != settings["settings_sha256"]
    ):
        raise ContractValidationError(
            "settings_materialization",
            "$.workflow_settings.settings_sha256",
            "executor did not reuse the preparation settings",
        )
    return PreparedEvaluationProductionRuntimeV1(
        prepared_bundle=prepared,
        executor_runtime=executor_runtime,
        prepared_inputs_root=prepared_root,
    )


def build_evaluation_runtime_object_registry_v1(
    bundle_path: Path,
    *,
    evaluation_output_root: Path,
    server_runtime: EvaluationServerRuntimeConfigV1,
) -> BuiltEvaluationRuntimeRegistryV1:
    """Build concrete runtime objects from one validated run-specific bundle."""

    loaded = load_workflow_scoring_runtime_bundle_v1(bundle_path)
    return _build_evaluation_runtime_object_registry_from_loaded_v1(
        loaded,
        evaluation_output_root=Path(evaluation_output_root),
        server_runtime=server_runtime,
    )


def build_evaluation_executor_runtime_v1(
    bundle_path: Path,
    *,
    evaluation_output_root: Path,
    server_runtime: EvaluationServerRuntimeConfigV1,
) -> BuiltEvaluationExecutorRuntimeV1:
    """Build the concrete registry and the registered Evaluation executor."""

    built = build_evaluation_runtime_object_registry_v1(
        bundle_path,
        evaluation_output_root=evaluation_output_root,
        server_runtime=server_runtime,
    )
    executor, handoff, settings, option = (
        build_registered_evaluation_workflow_executor_v1(
            bundle_path,
            evaluation_output_root=evaluation_output_root,
            runtime_registry=built.registry,
        )
    )
    return BuiltEvaluationExecutorRuntimeV1(
        executor=executor,
        scoring_handoff=handoff,
        workflow_settings=settings,
        registered_option=option,
        runtime_registry=built,
    )


def load_workflow_scoring_baseline_from_explicit_path_v1(
    template_path: Path,
) -> LoadedWorkflowScoringBaselineTemplateV1:
    # Local import keeps the public factory dependency graph acyclic.
    from pipeline.eval.workflow_runtime_bundle_v1 import (
        load_workflow_scoring_baseline_template_v1,
    )

    return load_workflow_scoring_baseline_template_v1(template_path)


def _build_evaluation_runtime_object_registry_from_loaded_v1(
    loaded: LoadedWorkflowScoringRuntimeV1,
    *,
    evaluation_output_root: Path,
    server_runtime: EvaluationServerRuntimeConfigV1,
) -> BuiltEvaluationRuntimeRegistryV1:
    if not isinstance(loaded, LoadedWorkflowScoringRuntimeV1):
        raise TypeError("loaded must be LoadedWorkflowScoringRuntimeV1")
    root = Path(evaluation_output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected_scorers = tuple(loaded.bundle["identity"]["selected_scorer_ids"])
    needs_local = "sf_qe" in selected_scorers
    needs_llm = any(row in {"sf_bt", "pj"} for row in selected_scorers)
    runtime_bindings = tuple(loaded.bundle["chapter_runtime_bindings"])

    local_runtime = _validate_local_runtime_config(
        server_runtime, required=needs_local
    )
    llm_material = _validate_llm_runtime_config(
        server_runtime,
        selected_scorers=selected_scorers,
        required=needs_llm,
    )
    identity = _runtime_identity(
        loaded,
        server_runtime=server_runtime,
        local_runtime=local_runtime,
        llm_material=llm_material,
    )
    identity_path = _contained_path(root, _RUNTIME_IDENTITY_RELATIVE_PATH)
    _persist_bytes_create_or_equal(
        identity_path, canonical_json_bytes(identity) + b"\n"
    )

    local_runtimes: dict[str, LocalSfQeRuntimeV1] = {}
    if local_runtime is not None:
        for runtime_id in sorted(
            {
                row["local_sf_qe_runtime_id"]
                for row in runtime_bindings
                if row["local_sf_qe_runtime_id"] is not None
            }
        ):
            local_runtimes[runtime_id] = local_runtime

    llm_runners: dict[str, SharedEvaluationRoleRunnerV1] = {}
    shared_ledgers: dict[str, SharedLlmAttemptLedger] = {}
    if llm_material is not None:
        artifact_store = ContentAddressedArtifactStore(
            _contained_path(root, _RUNTIME_ARTIFACT_STORE_RELATIVE_PATH)
        )
        response_cache = ApplicationResponseCache(
            index_path=_contained_path(root, _RUNTIME_CACHE_RELATIVE_PATH),
            artifact_store=artifact_store,
        )
        scheduler = PhysicalQuotaScheduler(
            _contained_path(root, _RUNTIME_QUOTA_RELATIVE_PATH)
        )
        runner_ledger_bindings: dict[str, tuple[str, str]] = {}
        selected_chapters = tuple(
            loaded.bundle["identity"]["selected_chapter_ids"]
        )
        for ordinal, (chapter_id, binding) in enumerate(
            zip(selected_chapters, runtime_bindings, strict=True)
        ):
            llm_runtime_id = binding["llm_roles_runtime_id"]
            ledger_runtime_id = binding["shared_ledger_runtime_id"]
            ledger_relative_path = binding["shared_ledger_relative_path"]
            if (
                llm_runtime_id is None
                or ledger_runtime_id is None
                or ledger_relative_path is None
            ):
                raise ContractValidationError(
                    "runtime_binding",
                    f"$.chapter_runtime_bindings[{ordinal}]",
                    "LLM scorer selection requires complete runtime bindings",
                )
            child_root = root / "chapters" / f"{ordinal:02d}_{chapter_id}"
            ledger_path = _contained_path(child_root, ledger_relative_path)
            existing_ledger = shared_ledgers.get(ledger_runtime_id)
            if existing_ledger is None:
                ledger = SharedLlmAttemptLedger(ledger_path)
                shared_ledgers[ledger_runtime_id] = ledger
            elif existing_ledger.path != ledger_path:
                raise ContractValidationError(
                    "runtime_registration",
                    f"$.chapter_runtime_bindings[{ordinal}]",
                    "one ledger runtime ID cannot name different physical ledgers",
                )
            previous = runner_ledger_bindings.get(llm_runtime_id)
            current = (ledger_runtime_id, ledger_relative_path)
            if previous is not None and previous != current:
                raise ContractValidationError(
                    "runtime_registration",
                    f"$.chapter_runtime_bindings[{ordinal}]",
                    "one role-runner ID cannot use different attempt ledgers",
                )
            runner_ledger_bindings[llm_runtime_id] = current

        for llm_runtime_id, (ledger_runtime_id, _relative) in sorted(
            runner_ledger_bindings.items()
        ):
            ledger = shared_ledgers[ledger_runtime_id]
            backend = SharedLlmBackend(
                credential_provider=llm_material["credential_provider"],
                scheduler=scheduler,
                ledger=ledger,
                response_cache=response_cache,
                sender=llm_material["sender"],
                clock=server_runtime.clock,
            )
            llm_runners[llm_runtime_id] = SharedEvaluationRoleRunnerV1(
                backend=backend,
                profile=llm_material["profile"],
                api_sources=llm_material["sources"],
                capability_evidence=llm_material["capabilities"],
                run_id=loaded.bundle["identity"]["evaluation_logical_run_id"],
                attempt_run_id=loaded.bundle["identity"][
                    "evaluation_attempt_run_id"
                ],
                cache_mode=server_runtime.cache_mode,
                cost_fact=server_runtime.cost_fact,
            )

    registry = EvaluationRuntimeObjectRegistryV1(
        local_sf_qe_runtimes=local_runtimes,
        llm_role_runners=llm_runners,
        shared_ledgers=shared_ledgers,
    )
    return BuiltEvaluationRuntimeRegistryV1(
        loaded_runtime=loaded,
        registry=registry,
        runtime_identity_path=identity_path,
        runtime_identity=identity,
    )


def _validate_local_runtime_config(
    value: EvaluationServerRuntimeConfigV1,
    *,
    required: bool,
) -> LocalSfQeRuntimeV1 | None:
    fields = (
        value.local_sf_qe_predictor,
        value.local_sf_qe_checkpoint_sha256,
        value.local_sf_qe_package_name,
        value.local_sf_qe_package_version,
        value.local_sf_qe_device,
        value.local_sf_qe_batch_size,
    )
    if not required:
        if any(row is not None for row in fields):
            raise ContractValidationError(
                "unused_runtime",
                "$.server_runtime.local_sf_qe",
                "unselected SF-QE must not receive runtime authority",
            )
        return None
    if any(row is None for row in fields):
        raise ContractValidationError(
            "runtime_registration",
            "$.server_runtime.local_sf_qe",
            "selected SF-QE requires predictor and sealed package metadata",
        )
    assert value.local_sf_qe_predictor is not None
    assert value.local_sf_qe_checkpoint_sha256 is not None
    assert value.local_sf_qe_package_name is not None
    assert value.local_sf_qe_package_version is not None
    assert value.local_sf_qe_device is not None
    assert value.local_sf_qe_batch_size is not None
    return LocalSfQeRuntimeV1(
        predictor=value.local_sf_qe_predictor,
        checkpoint_sha256=require_sha256(
            value.local_sf_qe_checkpoint_sha256,
            path="$.server_runtime.local_sf_qe.checkpoint_sha256",
        ),
        package_name=require_string(
            value.local_sf_qe_package_name,
            path="$.server_runtime.local_sf_qe.package_name",
        ),
        package_version=require_string(
            value.local_sf_qe_package_version,
            path="$.server_runtime.local_sf_qe.package_version",
        ),
        device=require_string(
            value.local_sf_qe_device,
            path="$.server_runtime.local_sf_qe.device",
        ),
        batch_size=require_int(
            value.local_sf_qe_batch_size,
            path="$.server_runtime.local_sf_qe.batch_size",
            minimum=1,
        ),
        clock=value.clock,
        monotonic=value.monotonic,
    )


def _validate_llm_runtime_config(
    value: EvaluationServerRuntimeConfigV1,
    *,
    selected_scorers: Sequence[str],
    required: bool,
) -> dict[str, Any] | None:
    supplied = (
        value.llm_profile is not None
        or bool(value.api_sources)
        or bool(value.capability_evidence)
        or value.credential_provider is not None
        or value.sender is not None
        or value.cost_fact is not None
    )
    if not required:
        if supplied:
            raise ContractValidationError(
                "unused_runtime",
                "$.server_runtime.llm",
                "unselected LLM scorers must not receive provider authority",
            )
        return None
    if (
        value.llm_profile is None
        or not value.api_sources
        or not value.capability_evidence
        or value.credential_provider is None
        or value.sender is None
    ):
        raise ContractValidationError(
            "runtime_registration",
            "$.server_runtime.llm",
            "selected SF-BT/PJ requires profile, sources, capabilities, credential resolver, and sender",
        )
    profile = validate_pipeline_profile(value.llm_profile)
    if profile["workstream"] != "evaluation":
        raise ContractValidationError(
            "runtime_authority",
            "$.server_runtime.llm_profile.workstream",
            "Evaluation cannot execute another workstream profile",
        )
    if any(row["fallback_plan"]["enabled"] for row in profile["role_bindings"]):
        raise ContractValidationError(
            "fallback_forbidden",
            "$.server_runtime.llm_profile.role_bindings",
            "production Evaluation factory does not add or accept fallback targets",
        )
    required_roles: set[str] = set()
    if "sf_bt" in selected_scorers:
        required_roles.update(
            {SF_BT_BACK_TRANSLATOR_ROLE_ID, SF_BT_SEMANTIC_JUDGE_ROLE_ID}
        )
    if "pj" in selected_scorers:
        required_roles.add(PJ_JUDGE_ROLE_ID)
    role_ids = {row["role_id"] for row in profile["role_bindings"]}
    if not required_roles.issubset(role_ids):
        raise ContractValidationError(
            "runtime_role_exact_cover",
            "$.server_runtime.llm_profile.role_bindings",
            "profile lacks a role required by the selected scorers",
        )
    sources = tuple(validate_api_source(row) for row in value.api_sources)
    capabilities = tuple(
        validate_capability_evidence(row) for row in value.capability_evidence
    )
    referenced_sources = {
        (row["primary"]["source_id"], row["primary"]["source_revision"])
        for row in profile["role_bindings"]
    }
    supplied_sources = {
        (row["source_id"], row["source_revision"]) for row in sources
    }
    if supplied_sources != referenced_sources:
        raise ContractValidationError(
            "runtime_source_exact_cover",
            "$.server_runtime.api_sources",
            "server sources must exact-cover sealed profile targets",
        )
    referenced_capabilities = {
        (
            row["primary"]["capability_id"],
            row["primary"]["capability_revision"],
        )
        for row in profile["role_bindings"]
    }
    supplied_capabilities = {
        (row["capability_id"], row["capability_revision"])
        for row in capabilities
    }
    if supplied_capabilities != referenced_capabilities:
        raise ContractValidationError(
            "runtime_capability_exact_cover",
            "$.server_runtime.capability_evidence",
            "server capabilities must exact-cover sealed profile targets",
        )
    for source in sources:
        resolve_source_credential(
            source=source, provider=value.credential_provider
        )
    return {
        "profile": profile,
        "sources": sources,
        "capabilities": capabilities,
        "credential_provider": value.credential_provider,
        "sender": value.sender,
    }


def _runtime_identity(
    loaded: LoadedWorkflowScoringRuntimeV1,
    *,
    server_runtime: EvaluationServerRuntimeConfigV1,
    local_runtime: LocalSfQeRuntimeV1 | None,
    llm_material: Mapping[str, Any] | None,
) -> dict[str, Any]:
    bundle_identity = loaded.bundle["identity"]
    local = (
        None
        if local_runtime is None
        else {
            "checkpoint_sha256": local_runtime.checkpoint_sha256,
            "package_name": local_runtime.package_name,
            "package_version": local_runtime.package_version,
            "device": local_runtime.device,
            "batch_size": local_runtime.batch_size,
        }
    )
    llm = None
    if llm_material is not None:
        llm = {
            "profile_id": llm_material["profile"]["profile_id"],
            "profile_revision": llm_material["profile"]["profile_revision"],
            "profile_sha256": shared_canonical_sha256(llm_material["profile"]),
            "source_records": sorted(
                (
                    {
                        "source_id": row["source_id"],
                        "source_revision": row["source_revision"],
                        "source_sha256": shared_canonical_sha256(row),
                        "credential_ref": row["credential_ref"],
                        "physical_quota_bucket_id": row[
                            "physical_quota_bucket_id"
                        ],
                    }
                    for row in llm_material["sources"]
                ),
                key=lambda row: (row["source_id"], row["source_revision"]),
            ),
            "capability_records": sorted(
                (
                    {
                        "capability_id": row["capability_id"],
                        "capability_revision": row["capability_revision"],
                        "capability_sha256": shared_canonical_sha256(row),
                    }
                    for row in llm_material["capabilities"]
                ),
                key=lambda row: (
                    row["capability_id"],
                    row["capability_revision"],
                ),
            ),
            "cache_mode": server_runtime.cache_mode,
            "cost_fact_sha256": (
                None
                if server_runtime.cost_fact is None
                else shared_canonical_sha256(server_runtime.cost_fact)
            ),
        }
    material = {
        "schema_id": "EvaluationRuntimeIdentityV1",
        "schema_version": "1.0.0",
        "workflow_run_id": bundle_identity["workflow_run_id"],
        "component_run_id": bundle_identity["component_run_id"],
        "evaluation_logical_run_id": bundle_identity[
            "evaluation_logical_run_id"
        ],
        "evaluation_attempt_run_id": bundle_identity[
            "evaluation_attempt_run_id"
        ],
        "bundle_sha256": loaded.bundle["integrity"]["bundle_sha256"],
        "settings_sha256": bundle_identity["settings_sha256"],
        "selected_chapter_ids": copy.deepcopy(
            bundle_identity["selected_chapter_ids"]
        ),
        "selected_scorer_ids": copy.deepcopy(
            bundle_identity["selected_scorer_ids"]
        ),
        "chapter_runtime_bindings": copy.deepcopy(
            loaded.bundle["chapter_runtime_bindings"]
        ),
        "local_sf_qe": local,
        "shared_llm": llm,
    }
    return {
        **material,
        "integrity": {"runtime_identity_sha256": canonical_sha256(material)},
    }


def _resolve_handoff_artifact_paths(
    *,
    handoff: Mapping[str, Any],
    loaded_template: LoadedWorkflowScoringBaselineTemplateV1,
    producer_handoff_artifacts: Mapping[str, Path],
) -> dict[str, Path]:
    external_refs = {
        row["translation_artifact"]["artifact_ref"]
        for row in loaded_template.template["external_translation_inputs"]
    }
    declared_rows = [
        row["binding"] for row in handoff["source_package_bindings"]
    ]
    declared_rows.extend(
        row
        for row in handoff["optional_bindings"].values()
        if row is not None
    )
    declared_rows.extend(
        row["translation_artifact"] for row in handoff["translation_inputs"]
    )
    declared_refs = [row["artifact_ref"] for row in declared_rows]
    expected_producer_refs = [
        row for row in declared_refs if row not in external_refs
    ]
    supplied_producer_refs = list(producer_handoff_artifacts)
    if set(supplied_producer_refs) != set(expected_producer_refs):
        raise ContractValidationError(
            "source_map_exact_cover",
            "$.producer_handoff_artifacts",
            "producer paths must exact-cover source, optional, S0, and S1 refs",
        )
    if any(row in producer_handoff_artifacts for row in external_refs):
        raise ContractValidationError(
            "baseline_authority",
            "$.producer_handoff_artifacts",
            "external baseline bytes must come from the registered template",
        )

    result: dict[str, Path] = {}
    for index, binding in enumerate(declared_rows):
        artifact_ref = binding["artifact_ref"]
        if artifact_ref in external_refs:
            path = loaded_template.file_paths.get(artifact_ref)
            if path is None:
                raise ContractValidationError(
                    "missing_artifact",
                    f"$template.external_translation_inputs[{index}]",
                    "registered external baseline file is absent",
                )
        else:
            path = Path(producer_handoff_artifacts[artifact_ref]).resolve()
        if not Path(path).is_file():
            raise ContractValidationError(
                "missing_artifact",
                str(path),
                "explicit handoff artifact file is absent",
            )
        if binding["sha256_kind"] != "physical":
            raise ContractValidationError(
                "hash_kind",
                f"$handoff.artifacts[{index}].sha256_kind",
                "runtime file transport requires physical hashes",
            )
        if physical_sha256(Path(path).read_bytes()) != binding["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                artifact_ref,
                "explicit artifact bytes differ from the handoff binding",
            )
        result[artifact_ref] = Path(path).resolve()
    return result


def _build_five_arm_common_input(
    *,
    handoff: Mapping[str, Any],
    selected_chapter_ids: Sequence[str],
    handoff_artifacts: Mapping[str, Path],
) -> CommonEvaluationInputV1:
    source_by_role = {
        row["role"]: handoff_artifacts[row["binding"]["artifact_ref"]]
        for row in handoff["source_package_bindings"]
    }
    if tuple(source_by_role) != SOURCE_BINDING_ROLES_V1:
        raise ContractValidationError(
            "source_binding_exact_cover",
            "$handoff.source_package_bindings",
            "source package role order drift",
        )
    translation_paths = {
        row["arm_id"]: handoff_artifacts[
            row["translation_artifact"]["artifact_ref"]
        ]
        for row in handoff["translation_inputs"]
    }
    if tuple(translation_paths) != _ARM_ORDER:
        raise ContractValidationError(
            "arm_order",
            "$handoff.translation_inputs",
            "five-arm input order drift",
        )
    d2l_common = build_canonical_d2l_common_input_v1(
        source_artifacts=FinalizedCanonicalSourceArtifactsV1(
            document=source_by_role["document"],
            structure_manifest=source_by_role["structure_manifest"],
            asset_manifest=source_by_role["asset_manifest"],
            admitted_projection=source_by_role["admitted_projection"],
            package_seal=source_by_role["package_seal"],
        ),
        s0_translation_artifact=translation_paths["s0"],
        s1_translation_artifact=translation_paths["s1"],
        selected_chapter_ids=selected_chapter_ids,
    )
    source = _source_snapshot_from_common(d2l_common)
    _validate_handoff_admitted_universe(source, handoff)
    raw_artifacts = {
        arm_id: _read_json(translation_paths[arm_id]) for arm_id in _ARM_ORDER
    }
    if (
        raw_artifacts["community"].get("schema_id")
        == COMMUNITY_ALIGNED_TRANSLATION_SCHEMA_ID
    ):
        common = build_common_aligned_evaluation_input_v1(
            source,
            machine_translation_artifacts={
                arm_id: raw_artifacts[arm_id]
                for arm_id in ("s0", "s1", "google_nmt", "llm_lc")
            },
            community_aligned_artifact=raw_artifacts["community"],
        )
    else:
        artifacts = [
            validate_translation_artifact(raw_artifacts[arm_id])
            for arm_id in _ARM_ORDER
        ]
        common = _drop_review_held_source_rows(
            build_common_evaluation_input(source, artifacts)
        )
    return _normalize_benchmark_arm_ids(common)


def _validate_handoff_admitted_universe(
    source: CommonSourceSnapshotV1,
    handoff: Mapping[str, Any],
) -> None:
    admitted_ids = [
        row.block_id for row in source.blocks if row.admission != "review_required"
    ]
    expected_count = len(admitted_ids)
    expected_sha256 = canonical_sha256(admitted_ids)
    for index, row in enumerate(handoff["translation_inputs"]):
        coverage = row["coverage"]
        if (
            coverage["expected_block_count"] != expected_count
            or coverage["block_universe_sha256"] != expected_sha256
        ):
            raise ContractValidationError(
                "coverage_universe_drift",
                f"$handoff.translation_inputs[{index}].coverage",
                "handoff coverage differs from the canonical admitted source universe",
            )


def _drop_review_held_source_rows(
    common: CommonEvaluationInputV1,
) -> CommonEvaluationInputV1:
    admitted_ids = {
        row.block_id for row in common.blocks if row.admission != "review_required"
    }
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=tuple(row for row in common.blocks if row.block_id in admitted_ids),
        arms=common.arms,
        translations=tuple(
            row for row in common.translations if row.block_id in admitted_ids
        ),
    )


def _normalize_benchmark_arm_ids(
    common: CommonEvaluationInputV1,
) -> CommonEvaluationInputV1:
    arm_by_id = {arm.arm_id: arm for arm in common.arms}
    if set(arm_by_id) != set(_ARM_ORDER) or len(common.arms) != len(_ARM_ORDER):
        raise ContractValidationError(
            "arm_scope",
            "$.common_input.arms",
            "common input must exact-cover the five registered arms",
        )
    translation_by_key = {
        (row.arm_id, row.block_id): row for row in common.translations
    }
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
        arms=tuple(
            CommonArmV1(
                artifact_id=arm.artifact_id,
                artifact_sha256=arm.artifact_sha256,
                logical_run_id=arm.logical_run_id,
                attempt_run_id=arm.attempt_run_id,
                arm_id=_BENCHMARK_ARM_BY_SETTINGS_ARM[arm_id],
                profile_id=arm.profile_id,
                profile_config_sha256=arm.profile_config_sha256,
                source_language=arm.source_language,
                target_language=arm.target_language,
            )
            for arm_id in _ARM_ORDER
            for arm in (arm_by_id[arm_id],)
        ),
        translations=tuple(
            CommonTranslationV1(
                arm_id=_BENCHMARK_ARM_BY_SETTINGS_ARM[arm_id],
                block_id=row.block_id,
                status=row.status,
                target_text=row.target_text,
                error_code=row.error_code,
            )
            for arm_id in _ARM_ORDER
            for block in common.blocks
            for row in (translation_by_key[(arm_id, block.block_id)],)
        ),
    )


def _select_common_arms(
    common: CommonEvaluationInputV1,
    *,
    selected_benchmark_arms: Sequence[str],
) -> CommonEvaluationInputV1:
    selected = tuple(selected_benchmark_arms)
    if len(selected) != len(set(selected)):
        raise ContractValidationError(
            "duplicate", "$.selected_arm_ids", "selected arm IDs must be unique"
        )
    by_id = {row.arm_id: row for row in common.arms}
    if any(row not in by_id for row in selected):
        raise ContractValidationError(
            "arm_scope",
            "$.selected_arm_ids",
            "selected arm is absent from the five-arm handoff",
        )
    selected_set = set(selected)
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
        arms=tuple(by_id[row] for row in selected),
        translations=tuple(
            row for row in common.translations if row.arm_id in selected_set
        ),
    )


def _source_snapshot_from_common(
    common: CommonEvaluationInputV1,
) -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
    )


def _build_method_material(
    *,
    selected_scorers: Sequence[str],
    producer_code_commit: str,
    llm_material: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    role_by_id = (
        {}
        if llm_material is None
        else {
            row["role_id"]: row
            for row in llm_material["profile"]["role_bindings"]
        }
    )
    definitions: list[dict[str, Any]] = []
    presentations: list[dict[str, Any]] = []
    for scorer_id in selected_scorers:
        metadata = _METHOD_METADATA.get(scorer_id)
        if metadata is None:
            raise ContractValidationError(
                "scorer_id",
                "$.selected_scorer_ids",
                f"unsupported scorer {scorer_id}",
            )
        definitions.append(
            {
                "method_id": scorer_id,
                "method_version": metadata["method_version"],
                "scorer_kind": metadata["scorer_kind"],
                "profile_scope": "common",
                "eligible_admissions": [
                    "translate",
                    "translate_structured",
                ],
            }
        )
        if scorer_id == "sf_qe":
            prompt_version = None
            model_id = SF_QE_MODEL_ID
        elif scorer_id == "sf_bt":
            back = _required_role_binding(
                role_by_id, SF_BT_BACK_TRANSLATOR_ROLE_ID
            )
            judge = _required_role_binding(
                role_by_id, SF_BT_SEMANTIC_JUDGE_ROLE_ID
            )
            prompt_version = (
                f"{back['prompt']['id']}@{back['prompt']['revision']}+"
                f"{judge['prompt']['id']}@{judge['prompt']['revision']}"
            )
            model_id = (
                f"back={back['primary']['requested_model_id']};"
                f"judge={judge['primary']['requested_model_id']}"
            )
        else:
            judge = _required_role_binding(role_by_id, PJ_JUDGE_ROLE_ID)
            prompt_version = (
                f"{judge['prompt']['id']}@{judge['prompt']['revision']}"
            )
            model_id = judge["primary"]["requested_model_id"]
        presentations.append(
            {
                "display_name": metadata["display_name"],
                "method": {
                    "method_id": scorer_id,
                    "method_version": metadata["method_version"],
                    "implementation_commit": require_commit(
                        producer_code_commit,
                        path="$.producer_code_commit",
                    ),
                    "prompt_version": prompt_version,
                    "model_id": model_id,
                },
            }
        )
    return definitions, presentations


def _required_role_binding(
    role_by_id: Mapping[str, Mapping[str, Any]], role_id: str
) -> Mapping[str, Any]:
    row = role_by_id.get(role_id)
    if row is None:
        raise ContractValidationError(
            "runtime_role_exact_cover",
            "$.server_runtime.llm_profile.role_bindings",
            f"profile lacks required role {role_id}",
        )
    return row


def _build_arm_presentations(
    selected_settings_arms: Sequence[str],
) -> list[dict[str, str]]:
    result = []
    for arm_id in selected_settings_arms:
        if arm_id == "s0":
            role, kind = "baseline", "system"
        elif arm_id == "s1":
            role, kind = "candidate", "system"
        elif arm_id == "community":
            role, kind = "reference", "human_reference"
        else:
            role, kind = "external_baseline", "machine_baseline"
        result.append(
            {
                "arm_id": arm_id,
                "role": role,
                "kind": kind,
                "label": arm_id,
            }
        )
    return result


def _build_chapter_runtime_bindings(
    *,
    selected_chapters: Sequence[str],
    selected_scorers: Sequence[str],
) -> list[dict[str, Any]]:
    needs_local = "sf_qe" in selected_scorers
    needs_llm = any(row in {"sf_bt", "pj"} for row in selected_scorers)
    result = []
    for ordinal, chapter_id in enumerate(selected_chapters):
        suffix = f"{ordinal:02d}.{chapter_id}"
        result.append(
            {
                "chapter_id": chapter_id,
                "local_sf_qe_runtime_id": (
                    "evaluation.local_sf_qe.v1" if needs_local else None
                ),
                "llm_roles_runtime_id": (
                    f"evaluation.llm_roles.{suffix}.v1"
                    if needs_llm
                    else None
                ),
                "shared_ledger_runtime_id": (
                    f"evaluation.shared_ledger.{suffix}.v1"
                    if needs_llm
                    else None
                ),
                "shared_ledger_relative_path": (
                    "usage/attempt_ledger.sqlite3" if needs_llm else None
                ),
            }
        )
    return result


def _build_pairwise_round_robin(
    selected_benchmark_arms: Sequence[str],
) -> list[dict[str, str]]:
    return [
        {
            "pair_id": f"{left.lower()}-vs-{right.lower()}",
            "arm_1_id": left,
            "arm_2_id": right,
        }
        for left, right in combinations(selected_benchmark_arms, 2)
    ]


def _build_chapter_config(
    common: CommonEvaluationInputV1,
    *,
    workflow_run_id: str,
    component_run_id: str,
    chapter_id: str,
    generated_at: str,
    producer_code_commit: str,
    methods: Sequence[Mapping[str, Any]],
    comparison_pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    arm_artifacts = [
        {
            "arm_id": arm.arm_id,
            "translation_artifact_id": arm.artifact_id,
            "translation_artifact_sha256": arm.artifact_sha256,
            "logical_run_id": arm.logical_run_id,
            "attempt_run_id": arm.attempt_run_id,
            "profile_id": arm.profile_id,
            "profile_config_sha256": arm.profile_config_sha256,
        }
        for arm in common.arms
    ]
    seed = canonical_sha256(
        {
            "workflow_run_id": workflow_run_id,
            "component_run_id": component_run_id,
            "chapter_id": chapter_id,
            "purpose": "evaluation-counterbalance-v1",
        }
    )
    draft = {
        "schema_id": "EvaluationRunConfigV1",
        "schema_version": "1.0.0",
        "config_id": f"evaluation-production-{chapter_id}-v1",
        "created_at": require_rfc3339(
            generated_at, path="$.generated_at"
        ),
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_runtime_factory_v1",
            "component_version": "1.0.0",
            "code_commit": require_commit(
                producer_code_commit, path="$.producer_code_commit"
            ),
        },
        "input_binding": {
            "source_schema_id": common.source_schema_id,
            "source_schema_version": common.source_schema_version,
            "source_binding": source_binding_to_dict(common.source_binding),
            "arm_artifacts": arm_artifacts,
        },
        "methods": copy.deepcopy(list(methods)),
        "comparison_pairs": copy.deepcopy(list(comparison_pairs)),
        "unit_policy": {
            "unit_kind": "block",
            "context_before_blocks": 1,
            "context_after_blocks": 1,
        },
        "blinding": {
            "mode": "opaque_counterbalanced",
            "seed": seed,
        },
        "retry_policy": {"max_transport_attempts": 1},
        "integrity": {"config_sha256": "0" * 64},
    }
    return seal_evaluation_run_config(draft)


def _template_authority_artifact_paths(
    loaded_template: LoadedWorkflowScoringBaselineTemplateV1,
) -> dict[str, Path]:
    authority = loaded_template.template["authority"]
    rows = [
        authority["benchmark_preset"],
        authority["evaluation_config"],
        authority["scorer_set"],
        *authority["evaluation_profiles"],
        *authority["policy_profiles"],
        *authority["shared_selections"],
    ]
    result: dict[str, Path] = {}
    for row in rows:
        artifact_ref = row["artifact_ref"]
        path = loaded_template.file_paths.get(artifact_ref)
        if path is None or not path.is_file():
            raise ContractValidationError(
                "missing_artifact",
                artifact_ref,
                "registered authority artifact is absent",
            )
        if physical_sha256(path.read_bytes()) != row["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                artifact_ref,
                "registered authority bytes drifted",
            )
        result[artifact_ref] = path
    return result


def _persist_json_artifact(
    path: Path, payload: Mapping[str, Any]
) -> Path:
    resolved = Path(path).resolve()
    _persist_bytes_create_or_equal(
        resolved, canonical_json_bytes(payload) + b"\n"
    )
    return resolved


def _ordered_nonempty_strings(
    values: Sequence[str],
    *,
    path: str,
) -> list[str]:
    rows = [
        require_string(value, path=f"{path}[{index}]")
        for index, value in enumerate(values)
    ]
    if not rows:
        raise ContractValidationError(
            "empty_array", path, "at least one chapter is required"
        )
    if len(rows) != len(set(rows)):
        raise ContractValidationError(
            "duplicate", path, "chapter IDs must be unique"
        )
    return rows


def _contained_path(root: Path, relative_path: str) -> Path:
    relative = require_relative_path(relative_path, path="$.relative_path")
    base = Path(root).resolve()
    path = base.joinpath(*relative.split("/")).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape", relative_path, "runtime path escapes its root"
        ) from exc
    return path


def _read_json(path: Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise ContractValidationError(
            "missing_artifact", str(source), "required JSON artifact is absent"
        )
    try:
        value = json.loads(source.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "artifact_json", str(source), "required artifact is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "artifact_shape", str(source), "required artifact must be an object"
        )
    return value


def _persist_bytes_create_or_equal(path: Path, rendered: bytes) -> None:
    destination = Path(path).resolve()
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != rendered:
            raise ContractValidationError(
                "artifact_collision",
                str(destination),
                "existing runtime artifact differs from the sealed request",
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.rename(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != rendered:
                raise ContractValidationError(
                    "artifact_collision",
                    str(destination),
                    "concurrent runtime artifact differs from the sealed request",
                )
    finally:
        temporary.unlink(missing_ok=True)
