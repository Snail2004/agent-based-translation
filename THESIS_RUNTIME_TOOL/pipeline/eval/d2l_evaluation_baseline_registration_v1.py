from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.canonical_d2l_benchmark_bridge_v1 import (
    FinalizedCanonicalSourceArtifactsV1,
    derive_finalized_canonical_source_binding_v1,
    load_finalized_canonical_d2l_source_v1,
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_rfc3339,
    require_sha256,
    require_string,
)
from pipeline.eval.d2l_five_arm_baselines_v1 import (
    D2LFiveArmBaselineMaterialV1,
    materialize_d2l_five_arm_baselines_v1,
)
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EVALUATION_CHAPTER_IDS_V1,
    EVALUATION_SCORER_IDS_V1,
    EvaluationWorkflowSettingsAuthorityV1,
)
from pipeline.eval.workflow_component_v1 import ARM_IDS_V1
from pipeline.eval.workflow_runtime_bundle_v1 import (
    WorkflowScoringBaselineTemplateSourcesV1,
)
from pipeline.eval.workflow_runtime_factory_v1 import (
    RegisteredEvaluationBaselineTemplateV1,
    build_evaluation_registered_option_facts_v1,
    register_evaluation_baseline_template_v1,
)
from pipeline.prepass.d2l_project_campaign_v2 import load_project
from pipeline.workflow_replay.contracts_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
)


__all__ = [
    "D2LEvaluationBaselineRegistrationV1",
    "register_d2l_evaluation_baseline_v1",
]


_TEMPLATE_ROOT = Path("workflow/evaluation_baseline_template")
_SETTINGS_OPTION_ID = "evaluation_workflow_settings_v1"
_PROFILE_REF = "authority/profiles/evaluation_production_v1.json"
_SELECTION_REF = "authority/selections/d2l_five_chapter_v1.json"


@dataclass(frozen=True, slots=True)
class D2LEvaluationBaselineRegistrationV1:
    registration: RegisteredEvaluationBaselineTemplateV1
    baseline_material: D2LFiveArmBaselineMaterialV1
    registered_option_facts: Mapping[str, Any]
    registered_option_sha256: str


def register_d2l_evaluation_baseline_v1(
    *,
    job_root: Path,
    expected_job_id: str,
    project_id: str,
    selected_chapter_ids: Sequence[str],
    community_alignment_root: Path,
    google_capture_paths: Sequence[Path],
    llm_lc_marked_path: Path,
    llm_lc_expected_sha256: str,
    llm_lc_expected_marker_count: int,
    evaluation_profile_path: Path,
    created_at: str,
    producer_code_commit: str,
) -> D2LEvaluationBaselineRegistrationV1:
    """Register the accepted D2L five-arm evidence for one finalized App job."""

    root = Path(job_root).resolve()
    created = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    job_id = require_string(expected_job_id, path="$.expected_job_id")
    project_name = require_string(project_id, path="$.project_id")
    chapters = tuple(selected_chapter_ids)
    if chapters != tuple(EVALUATION_CHAPTER_IDS_V1):
        raise ContractValidationError(
            "chapter_scope",
            "$.selected_chapter_ids",
            "D2L production baseline registration requires the exact five-chapter order",
        )

    project = load_project(root, verify_tree=True)
    if project.manifest["job_id"] != job_id:
        raise ContractValidationError(
            "job_identity",
            "$.expected_job_id",
            "D2L source manifest belongs to another job",
        )
    if project.manifest["document_doc_id"] != project_name:
        raise ContractValidationError(
            "project_identity",
            "$.project_id",
            "D2L source manifest belongs to another project",
        )
    source_artifacts = FinalizedCanonicalSourceArtifactsV1(
        document=project.package_root / "document.json",
        structure_manifest=project.package_root / "structure_manifest.json",
        asset_manifest=project.package_root / "asset_manifest.json",
        admitted_projection=(
            project.package_root / "admitted_projection_v1.json"
        ),
        package_seal=project.finalization_path,
    )
    source_binding = derive_finalized_canonical_source_binding_v1(
        source_artifacts=source_artifacts,
        project_id=project_name,
    )
    source = load_finalized_canonical_d2l_source_v1(
        source_artifacts=source_artifacts,
        expected_source_binding=source_binding,
        selected_chapter_ids=chapters,
    )

    template_root = root / _TEMPLATE_ROOT
    baseline_material = materialize_d2l_five_arm_baselines_v1(
        source,
        output_root=template_root / "baselines",
        source_finalization_path=project.finalization_path,
        admitted_projection_path=source_artifacts.admitted_projection,
        candidate_tree_sha256=project.source_snapshot[
            "package_tree_sha256"
        ].lower(),
        community_alignment_root=Path(community_alignment_root),
        google_capture_paths=tuple(Path(path) for path in google_capture_paths),
        selected_chapter_ids=chapters,
        llm_lc_marked_path=Path(llm_lc_marked_path),
        llm_lc_expected_sha256=require_sha256(
            llm_lc_expected_sha256,
            path="$.llm_lc_expected_sha256",
        ),
        llm_lc_expected_marker_count=llm_lc_expected_marker_count,
        created_at=created,
        producer_code_commit=commit,
    )

    authority, authority_files, profile_id = _materialize_authority(
        template_root=template_root,
        evaluation_profile_path=Path(evaluation_profile_path),
        selected_chapter_ids=chapters,
    )
    facts = build_evaluation_registered_option_facts_v1(
        authority,
        evaluation_profile_ref=_PROFILE_REF,
        policy_profile_ref=None,
        shared_selection_ref=_SELECTION_REF,
    )
    option_sha256 = canonical_sha256(facts)
    template_id = (
        "evaluation-d2l-five-chapter-"
        + canonical_sha256(
            {
                "job_id": job_id,
                "source_binding_sha256": canonical_sha256(
                    project.source_binding
                ),
                "registered_option_sha256": option_sha256,
                "external_inputs": list(
                    baseline_material.external_translation_inputs
                ),
            }
        )[:24]
    )
    registration = register_evaluation_baseline_template_v1(
        root,
        job_id=job_id,
        source_binding_sha256=canonical_sha256(project.source_binding),
        supported_chapter_ids=chapters,
        template_id=template_id,
        created_at=created,
        producer_code_commit=commit,
        settings_option_id=_SETTINGS_OPTION_ID,
        registered_option_sha256=option_sha256,
        evaluation_profile_id=profile_id,
        evaluation_profile_ref=_PROFILE_REF,
        policy_profile_id=None,
        policy_profile_ref=None,
        shared_selection_ref=_SELECTION_REF,
        settings_authority=authority,
        artifact_sources=WorkflowScoringBaselineTemplateSourcesV1(
            external_translation_inputs=list(
                baseline_material.external_translation_inputs
            ),
            external_translation_artifacts={
                row["translation_artifact"]["artifact_ref"]:
                    baseline_material.artifact_paths[row["arm_id"]]
                for row in baseline_material.external_translation_inputs
            },
            authority_artifacts=authority_files,
        ),
        caveats=(
            "Community artifact certifies accepted alignment, not translation quality.",
            "GPT Web authority is the 3016-marker full capture; the 143-marker partial capture is forbidden.",
        ),
    )
    loaded_facts = build_evaluation_registered_option_facts_v1(
        registration.loaded_template.settings_authority,
        evaluation_profile_ref=_PROFILE_REF,
        policy_profile_ref=None,
        shared_selection_ref=_SELECTION_REF,
    )
    if (
        loaded_facts != facts
        or canonical_sha256(loaded_facts) != option_sha256
        or registration.loaded_template.registered_option[
            "registered_option_sha256"
        ]
        != option_sha256
    ):
        raise ContractValidationError(
            "registered_option",
            "$.registered_option_sha256",
            "persisted Evaluation option differs from registered authority",
        )
    return D2LEvaluationBaselineRegistrationV1(
        registration=registration,
        baseline_material=baseline_material,
        registered_option_facts=copy.deepcopy(facts),
        registered_option_sha256=option_sha256,
    )


def _materialize_authority(
    *,
    template_root: Path,
    evaluation_profile_path: Path,
    selected_chapter_ids: Sequence[str],
) -> tuple[
    EvaluationWorkflowSettingsAuthorityV1,
    dict[str, Path],
    str,
]:
    profile_path = Path(evaluation_profile_path).resolve()
    profile = _read_json(profile_path, label="Evaluation profile")
    profile_id = require_string(
        profile.get("profile_id"), path="$.evaluation_profile.profile_id"
    )
    rows = {
        "authority/presets/d2l_five_chapter_v1.json": (
            "evaluation_benchmark_preset_v1",
            {
                "schema_id": "EvaluationBenchmarkPresetV1",
                "schema_version": "1.0.0",
                "preset_id": "d2l_five_chapter_five_arm_v1",
                "chapter_ids": list(selected_chapter_ids),
                "arm_ids": list(ARM_IDS_V1),
                "scorer_ids": list(EVALUATION_SCORER_IDS_V1),
            },
        ),
        "authority/config/evaluation_workflow_v1.json": (
            "evaluation_run_config_v1",
            {
                "schema_id": "EvaluationWorkflowConfigV1",
                "schema_version": "1.0.0",
                "aggregation_policy_id": "method_specific_only",
                "report_policy_id": "full_run_report_v1",
                "verdict_policy_id": "no_cross_method_composite",
                "five_arm_order": list(ARM_IDS_V1),
            },
        ),
        "authority/scorers/sf_qe_sf_bt_pj_v1.json": (
            "evaluation_scorer_set_v1",
            {
                "schema_id": "EvaluationScorerSetV1",
                "schema_version": "1.0.0",
                "scorer_set_id": "sf_qe_sf_bt_pj_v1",
                "scorer_ids": list(EVALUATION_SCORER_IDS_V1),
                "pairwise_policy": "full_round_robin_selected_arms_v1",
            },
        ),
        _SELECTION_REF: (
            "evaluation_shared_selection_v1",
            {
                "schema_id": "EvaluationSharedSelectionV1",
                "schema_version": "1.0.0",
                "selection_id": "d2l_five_chapter_v1",
                "chapter_ids": list(selected_chapter_ids),
                "arm_ids": list(ARM_IDS_V1),
                "scorer_ids": list(EVALUATION_SCORER_IDS_V1),
            },
        ),
    }
    paths: dict[str, Path] = {}
    bindings: dict[str, dict[str, str]] = {}
    for artifact_ref, (artifact_kind, payload) in rows.items():
        path = template_root / Path(*artifact_ref.split("/"))
        _write_create_or_equal(path, payload)
        paths[artifact_ref] = path
        bindings[artifact_ref] = _binding(
            artifact_ref=artifact_ref,
            artifact_kind=artifact_kind,
            schema_version=str(payload["schema_version"]),
            path=path,
        )
    bindings[_PROFILE_REF] = _binding(
        artifact_ref=_PROFILE_REF,
        artifact_kind="evaluation_profile_v1",
        schema_version=str(profile.get("schema_version", "pipeline_profile_v1")),
        path=profile_path,
    )
    authority = EvaluationWorkflowSettingsAuthorityV1(
        benchmark_preset=bindings[
            "authority/presets/d2l_five_chapter_v1.json"
        ],
        evaluation_config=bindings[
            "authority/config/evaluation_workflow_v1.json"
        ],
        scorer_set=bindings[
            "authority/scorers/sf_qe_sf_bt_pj_v1.json"
        ],
        evaluation_profiles=(bindings[_PROFILE_REF],),
        policy_profiles=(),
        shared_selections=(bindings[_SELECTION_REF],),
        chapter_ids=tuple(selected_chapter_ids),
        arm_ids=ARM_IDS_V1,
        scorer_ids=EVALUATION_SCORER_IDS_V1,
    )
    ordered_paths = {
        "authority/presets/d2l_five_chapter_v1.json": paths[
            "authority/presets/d2l_five_chapter_v1.json"
        ],
        "authority/config/evaluation_workflow_v1.json": paths[
            "authority/config/evaluation_workflow_v1.json"
        ],
        "authority/scorers/sf_qe_sf_bt_pj_v1.json": paths[
            "authority/scorers/sf_qe_sf_bt_pj_v1.json"
        ],
        _PROFILE_REF: profile_path,
        _SELECTION_REF: paths[_SELECTION_REF],
    }
    return authority, ordered_paths, profile_id


def _binding(
    *,
    artifact_ref: str,
    artifact_kind: str,
    schema_version: str,
    path: Path,
) -> dict[str, str]:
    return {
        "artifact_ref": artifact_ref,
        "artifact_kind": artifact_kind,
        "schema_version": schema_version,
        "sha256": physical_sha256(Path(path).read_bytes()),
        "sha256_kind": "physical",
    }


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ContractValidationError(
            "missing_artifact",
            f"$.{label}",
            f"{label} must be an existing regular file",
        )
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "invalid_json", f"$.{label}", f"{label} must be UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "type", f"$.{label}", f"{label} must be a JSON object"
        )
    return value


def _write_create_or_equal(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = canonical_json_bytes(dict(payload)) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError(
                "immutable_output",
                str(path),
                "existing authority artifact differs from deterministic output",
            )
        return
    path.write_bytes(encoded)
