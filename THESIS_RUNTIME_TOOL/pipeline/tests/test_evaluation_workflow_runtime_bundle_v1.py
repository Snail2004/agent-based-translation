from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.eval.benchmark_v1 import (
    augment_common_input_with_benchmark_overlays_v1,
    build_benchmark_manifest_v1,
    build_benchmark_preflight_v1,
    build_overlay_from_common_arm_v1,
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
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    canonical_sha256 as evaluation_canonical_sha256,
)
from pipeline.eval.d2l_input_v1 import (
    D2L_CANONICAL_POLICY,
    seal_d2l_evaluation_input,
)
from pipeline.eval.d2l_package_adapter_v1 import project_d2l_evaluation_package
from pipeline.eval.end_to_end_runner_v1 import LocalSfQeRuntimeV1
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
)
from pipeline.eval.local_sf_qe_v1 import SF_QE_MODEL_ID
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config
from pipeline.eval.workflow_component_writer_v1 import (
    validate_evaluation_workflow_component_package_v1,
)
from pipeline.eval.workflow_runtime_bundle_v1 import (
    EvaluationRuntimeObjectRegistryV1,
    WorkflowScoringBaselineTemplateSourcesV1,
    WorkflowScoringRuntimeArtifactSourcesV1,
    build_evaluation_workflow_registration_from_baseline_template_v1,
    build_registered_evaluation_workflow_executor_v1,
    load_workflow_scoring_baseline_template_from_workflow_runtime_v1,
    load_workflow_scoring_baseline_template_v1,
    load_workflow_scoring_runtime_bundle_v1,
    materialize_workflow_scoring_baseline_template_v1,
    materialize_workflow_scoring_runtime_bundle_v1,
    validate_workflow_scoring_runtime_bundle_v1,
)
from pipeline.workflow_replay.contracts_v1 import (
    SOURCE_BINDING_ROLES_V1,
    build_scoring_handoff_v1,
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
)
from pipeline.ingest.admitted_projection import build_admitted_projection
from pipeline.ingest.canonical_source_package import (
    canonical_json_sha256,
    seal_asset_manifest,
)
from pipeline.tests.test_evaluation_benchmark_runner_v1 import (
    COMMIT,
    NOW,
    _Predictor,
    _config,
    _sources,
)
from pipeline.tests.test_evaluation_canonical_d2l_benchmark_bridge_v1 import (
    _source_fixture as _neutral_source_fixture,
    _translation_artifact as _canonical_translation_artifact,
)


CHAPTER_ID = "d2l_preliminaries"
WORKFLOW_RUN_ID = "workflow_evaluation_runtime_fixture"
COMPONENT_RUN_ID = "evalcomp_runtime_fixture"
JOB_ID = "job_evaluation_runtime_fixture"
SOURCE_BINDING_SHA256 = "a" * 64
ARM_IDS = ("s0", "s1", "community", "google_nmt", "llm_lc")
BENCHMARK_ARM_IDS = ("S0", "S1", "community", "google_nmt", "llm_lc")
ARM_ROLES = {
    "S0": "pipeline_ablation",
    "S1": "thesis_system",
    "community": "human_community",
    "google_nmt": "conventional_nmt",
    "llm_lc": "long_context_diagnostic",
}


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    return path


def _physical_binding(
    path: Path,
    *,
    artifact_ref: str,
    artifact_kind: str,
    schema_version: str = "1.0.0",
) -> dict[str, str]:
    return {
        "artifact_ref": artifact_ref,
        "artifact_kind": artifact_kind,
        "schema_version": schema_version,
        "sha256": physical_sha256(path.read_bytes()),
        "sha256_kind": "physical",
    }


def _artifact_file(
    root: Path,
    *,
    artifact_ref: str,
    artifact_kind: str,
    body: object,
    schema_version: str = "1.0.0",
) -> tuple[Path, dict[str, str]]:
    path = _write_json(root / artifact_ref, body)
    return path, _physical_binding(
        path,
        artifact_ref=artifact_ref,
        artifact_kind=artifact_kind,
        schema_version=schema_version,
    )


def _d2l_package(
    root: Path,
    *,
    translation_paths: dict[str, Path],
    profile_path: Path,
) -> tuple[Path, dict]:
    source = _sources()[0]
    artifacts = [
        {
            "artifact_id": "artifact-source",
            "kind": "source_manifest",
            "relative_path": "input/source_manifest.json",
            "sha256": hashlib.sha256(b"source-manifest").hexdigest(),
            "size_bytes": 15,
        },
        {
            "artifact_id": "artifact-profile",
            "kind": "runtime_profile",
            "relative_path": "input/runtime_profile.json",
            "sha256": physical_sha256(profile_path.read_bytes()),
            "size_bytes": len(profile_path.read_bytes()),
        },
    ]
    arms = []
    translations = []
    for arm_id, role in (("s0", "baseline"), ("s1", "candidate")):
        artifact_id = f"artifact-{arm_id}"
        path = translation_paths[arm_id]
        digest = physical_sha256(path.read_bytes())
        arms.append(
            {
                "arm_id": arm_id,
                "role": role,
                "label": arm_id.upper(),
                "translation_artifact_id": artifact_id,
                "translation_sha256": digest,
            }
        )
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "kind": "translation",
                "relative_path": f"translations/{arm_id}.json",
                "sha256": digest,
                "size_bytes": len(path.read_bytes()),
            }
        )
        for block in source.blocks:
            preserved = block.admission == "preserve"
            translations.append(
                {
                    "arm_id": arm_id,
                    "block_id": block.block_id,
                    "status": "passthrough" if preserved else "translated",
                    "target_text": (
                        block.source_text
                        if preserved
                        else f"Ban dich {arm_id} {block.block_id}"
                    ),
                    "error_code": None,
                    "source_artifact_id": artifact_id,
                }
            )
    draft = {
        "schema_id": "D2LEvaluationInputV1",
        "schema_version": "1.0.0",
        "package_id": "d2l_eval_runtime_fixture",
        "created_at": NOW,
        "producer": {
            "workstream": "d2l",
            "component": "runtime_fixture_exporter",
            "component_version": "1.0.0",
            "code_commit": COMMIT,
        },
        "identity": {
            "project_id": "d2l",
            "logical_run_id": "d2l_runtime_fixture",
            "document_id": "d2l",
            "profile_id": "technical_d2l_v1",
            "experiment_id": "d2l_runtime_fixture_attempt",
            "selected_chapter_ids": [CHAPTER_ID],
            "source_db_sha256": source.source_binding.source_db_sha256,
            "runtime_manifest_sha256": source.source_binding.runtime_manifest_sha256,
            "source_manifest_artifact_id": "artifact-source",
        },
        "runtime_profile": {
            "profile_id": "technical_d2l_v1",
            "profile_version": "1.0.0",
            "source_language": "en",
            "target_language": "vi",
            "domain": "deep_learning",
            "source_artifact_id": "artifact-profile",
        },
        "arms": arms,
        "blocks": [
            {
                "block_id": block.block_id,
                "chapter_id": block.chapter_id,
                "order_index": block.order_index,
                "block_type": block.block_type,
                "source_text": block.source_text,
                "admission": block.admission,
            }
            for block in source.blocks
        ],
        "translations": translations,
        "runtime_terms": [],
        "injection_rows": [],
        "artifacts": artifacts,
        "integrity": {
            "artifact_set_sha256": evaluation_canonical_sha256(
                {"artifacts": artifacts}, policy=D2L_CANONICAL_POLICY
            ),
            "package_sha256": "0" * 64,
        },
    }
    payload = seal_d2l_evaluation_input(draft)
    return _write_json(root / "producer" / "d2l_input.json", payload), payload


def _full_common(
    d2l_payload: dict,
    *,
    translation_bindings: dict[str, dict[str, str]],
) -> CommonEvaluationInputV1:
    d2l_common = project_d2l_evaluation_package(d2l_payload)
    return _with_external_arms(
        d2l_common, translation_bindings=translation_bindings
    )


def _with_external_arms(
    d2l_common: CommonEvaluationInputV1,
    *,
    translation_bindings: dict[str, dict[str, str]],
) -> CommonEvaluationInputV1:
    d2l_arms = tuple(
        CommonArmV1(
            arm.artifact_id,
            arm.artifact_sha256,
            arm.logical_run_id,
            arm.attempt_run_id,
            arm.arm_id.upper(),
            arm.profile_id,
            arm.profile_config_sha256,
            arm.source_language,
            arm.target_language,
        )
        for arm in d2l_common.arms
    )
    d2l_translations = tuple(
        CommonTranslationV1(
            row.arm_id.upper(),
            row.block_id,
            row.status,
            row.target_text,
            row.error_code,
        )
        for row in d2l_common.translations
    )
    external_arms = []
    external_translations = []
    for arm_id in ARM_IDS[2:]:
        binding = translation_bindings[arm_id]
        external_arms.append(
            CommonArmV1(
                f"artifact-{arm_id}",
                binding["sha256"],
                f"logical-{arm_id}",
                f"attempt-{arm_id}",
                arm_id,
                f"profile-{arm_id}",
                hashlib.sha256(f"profile-{arm_id}".encode()).hexdigest(),
                "en",
                "vi",
            )
        )
        for block in d2l_common.blocks:
            if block.admission == "preserve":
                status = "preserved"
                target_text = block.source_text
            elif block.admission == "exclude":
                status = "excluded"
                target_text = None
            elif block.admission == "review_required":
                status = "review_held"
                target_text = None
            else:
                status = "translated"
                target_text = f"Ban dich {arm_id} {block.block_id}"
            external_translations.append(
                CommonTranslationV1(
                    arm_id,
                    block.block_id,
                    status,
                    target_text,
                    None,
                )
            )
    return CommonEvaluationInputV1(
        d2l_common.source_schema_id,
        d2l_common.source_schema_version,
        d2l_common.source_binding,
        d2l_common.blocks,
        (*d2l_arms, *external_arms),
        (*d2l_translations, *external_translations),
    )


def _replace_fixture_identity(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _replace_fixture_identity(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_fixture_identity(item) for item in value]
    if isinstance(value, str):
        return value.replace("neutral_fixture_ch01", CHAPTER_ID).replace(
            "neutral_fixture", "d2l"
        )
    return value


def _canonical_source_fixture(
    root: Path,
) -> tuple[FinalizedCanonicalSourceArtifactsV1, dict, dict]:
    neutral_paths, _neutral_binding, _neutral_projection = _neutral_source_fixture(
        root / "neutral"
    )
    document = _replace_fixture_identity(
        json.loads(neutral_paths.document.read_text("utf-8"))
    )
    structure = _replace_fixture_identity(
        json.loads(neutral_paths.structure_manifest.read_text("utf-8"))
    )
    stale_asset_manifest = _replace_fixture_identity(
        json.loads(neutral_paths.asset_manifest.read_text("utf-8"))
    )
    assert isinstance(document, dict)
    assert isinstance(structure, dict)
    assert isinstance(stale_asset_manifest, dict)
    asset_manifest = seal_asset_manifest(
        document,
        structure,
        assets=copy.deepcopy(stale_asset_manifest["assets"]),
        block_bindings=copy.deepcopy(stale_asset_manifest["block_bindings"]),
    )
    projection = build_admitted_projection(document, structure, asset_manifest)
    binding = {
        "binding_kind": "canonical_source_package_v1",
        "project_id": "d2l",
        "document_id": "d2l",
        "document": {
            "schema_version": document["schema_version"],
            "sha256": canonical_json_sha256(document),
        },
        "structure": {
            "schema_version": structure["schema_version"],
            "sha256": canonical_json_sha256(structure),
        },
        "asset_manifest": {
            "schema_version": asset_manifest["schema_version"],
            "sha256": canonical_json_sha256(asset_manifest),
        },
        "admitted_projection": {
            "schema_version": projection["schema_version"],
            "payload_sha256": projection["integrity"]["payload_sha256"],
        },
        "admission_policy": copy.deepcopy(projection["policy"]),
    }
    finalization_body = {
        "schema_version": "source_package_finalization_v1",
        "lifecycle": "finalized_pre_run",
        "doc_id": "d2l",
        "package": {
            "document": {
                "schema_version": document["schema_version"],
                "sha256": canonical_json_sha256(document),
            },
            "structure": {
                "schema_version": structure["schema_version"],
                "sha256": canonical_json_sha256(structure),
            },
            "asset_manifest": {
                "schema_version": asset_manifest["schema_version"],
                "sha256": canonical_json_sha256(asset_manifest),
            },
            "admitted_projection": {
                "schema_version": projection["schema_version"],
                "sha256": canonical_json_sha256(projection),
            },
        },
        "policies": {"admission": copy.deepcopy(projection["policy"])},
    }
    finalization = {
        **finalization_body,
        "integrity": {
            "payload_sha256": canonical_json_sha256(finalization_body)
        },
    }
    source_root = root / "canonical"
    return (
        FinalizedCanonicalSourceArtifactsV1(
            document=_write_json(source_root / "document.json", document),
            structure_manifest=_write_json(
                source_root / "structure_manifest.json", structure
            ),
            asset_manifest=_write_json(
                source_root / "asset_manifest.json", asset_manifest
            ),
            admitted_projection=_write_json(
                source_root / "admitted_projection_v1.json", projection
            ),
            package_seal=_write_json(
                source_root / "source_package_finalization_v1.json",
                finalization,
            ),
        ),
        binding,
        projection,
    )


def _authority_files(
    root: Path,
) -> tuple[EvaluationWorkflowSettingsAuthorityV1, dict[str, Path]]:
    rows = (
        ("presets/narrow_five_chapter_d2l_v1.json", "evaluation_benchmark_preset_v1"),
        ("configs/evaluation_config_v1.json", "evaluation_run_config_v1"),
        ("scorers/sf_qe_sf_bt_pj_v1.json", "evaluation_scorer_set_v1"),
        ("profiles/evaluation_fixture_v1.json", "evaluation_profile_v1"),
        ("selections/evaluation_five_chapter_v1.json", "evaluation_shared_selection_v1"),
    )
    bindings = {}
    paths = {}
    for artifact_ref, artifact_kind in rows:
        path, binding = _artifact_file(
            root,
            artifact_ref=artifact_ref,
            artifact_kind=artifact_kind,
            body={"artifact_kind": artifact_kind, "artifact_ref": artifact_ref},
        )
        paths[artifact_ref] = path
        bindings[artifact_ref] = binding
    authority = EvaluationWorkflowSettingsAuthorityV1(
        benchmark_preset=bindings[rows[0][0]],
        evaluation_config=bindings[rows[1][0]],
        scorer_set=bindings[rows[2][0]],
        evaluation_profiles=(bindings[rows[3][0]],),
        policy_profiles=(),
        shared_selections=(bindings[rows[4][0]],),
    )
    return authority, paths


def _locked_selection(
    registered_option_sha256: str,
    *,
    selected_scorer_ids: tuple[str, ...] = ("sf_qe",),
) -> dict:
    basis = {
        "settings_option_id": "evaluation_workflow_settings_v1",
        "selected_chapter_ids": [CHAPTER_ID],
        "selected_arm_ids": list(ARM_IDS),
        "selected_scorer_ids": list(selected_scorer_ids),
        "highlight_pair": {
            "baseline_arm_id": "s0",
            "candidate_arm_id": "s1",
        },
        "registered_option_sha256": registered_option_sha256,
    }
    return {**basis, "selection_sha256": canonical_sha256(basis)}


def _config_for_scorers(
    common: CommonEvaluationInputV1,
    selected_scorer_ids: tuple[str, ...],
) -> dict:
    if selected_scorer_ids == ("sf_qe",):
        return _config(common)
    payload = copy.deepcopy(_config(common))
    payload["config_id"] = "workflow-runtime-selected-scorers-fixture"
    payload["methods"] = [
        {
            "method_id": method_id,
            "method_version": f"{method_id}-fixture-v1",
            "scorer_kind": "pairwise" if method_id == "pj" else "unary",
            "profile_scope": "common",
            "eligible_admissions": ["translate"],
        }
        for method_id in selected_scorer_ids
    ]
    payload["comparison_pairs"] = (
        [{"pair_id": "s0-vs-s1", "arm_1_id": "S0", "arm_2_id": "S1"}]
        if "pj" in selected_scorer_ids
        else []
    )
    payload["integrity"]["config_sha256"] = "0" * 64
    return seal_evaluation_run_config(payload)


def _fixture(
    tmp_path: Path,
    *,
    canonical: bool = False,
    selected_scorer_ids: tuple[str, ...] = ("sf_qe",),
):
    inputs = tmp_path / "inputs"
    source_paths: dict[str, Path] = {}
    source_bindings: list[dict] = []
    canonical_paths: FinalizedCanonicalSourceArtifactsV1 | None = None
    canonical_binding: dict | None = None
    canonical_projection: dict | None = None
    if canonical:
        canonical_paths, canonical_binding, canonical_projection = (
            _canonical_source_fixture(inputs)
        )
        role_sources = {
            "document": (
                canonical_paths.document,
                "canonical_document_v1",
            ),
            "structure_manifest": (
                canonical_paths.structure_manifest,
                "structure_manifest_v1",
            ),
            "asset_manifest": (
                canonical_paths.asset_manifest,
                "asset_manifest_v1",
            ),
            "admitted_projection": (
                canonical_paths.admitted_projection,
                "admitted_projection_v1",
            ),
            "normalization_receipt": (
                _write_json(
                    inputs / "canonical" / "normalization_receipt_v1.json",
                    {"schema_version": "normalization_receipt_v1"},
                ),
                "normalization_receipt_v1",
            ),
            "package_seal": (
                canonical_paths.package_seal,
                "source_package_finalization_v1",
            ),
        }
        for role in SOURCE_BINDING_ROLES_V1:
            path, artifact_kind = role_sources[role]
            artifact_ref = f"source/{role}.json"
            source_paths[artifact_ref] = path
            source_bindings.append(
                {
                    "role": role,
                    "binding": _physical_binding(
                        path,
                        artifact_ref=artifact_ref,
                        artifact_kind=artifact_kind,
                        schema_version=str(
                            json.loads(path.read_text("utf-8")).get(
                                "schema_version", "1.0.0"
                            )
                        ),
                    ),
                }
            )
    else:
        for role in SOURCE_BINDING_ROLES_V1:
            artifact_ref = f"source/{role}.json"
            path, binding = _artifact_file(
                inputs,
                artifact_ref=artifact_ref,
                artifact_kind=f"{role}_v1",
                body={"role": role},
            )
            source_paths[artifact_ref] = path
            source_bindings.append({"role": role, "binding": binding})
    admitted = source_bindings[3]["binding"]

    translation_paths: dict[str, Path] = {}
    translation_bindings: dict[str, dict[str, str]] = {}
    for arm_id in ARM_IDS:
        artifact_ref = f"translations/{arm_id}.json"
        if canonical:
            assert canonical_paths is not None
            assert canonical_binding is not None
            assert canonical_projection is not None
            document = json.loads(canonical_paths.document.read_text("utf-8"))
            payload = _canonical_translation_artifact(
                document,
                canonical_projection,
                canonical_binding,
                arm_id=arm_id,
            )
            path = _write_json(inputs / artifact_ref, payload)
            binding = _physical_binding(
                path,
                artifact_ref=artifact_ref,
                artifact_kind="translation_artifact_v1",
                schema_version=payload["schema_version"],
            )
        else:
            path, binding = _artifact_file(
                inputs,
                artifact_ref=artifact_ref,
                artifact_kind="translation_artifact_v1",
                body={"arm_id": arm_id, "chapter_id": CHAPTER_ID},
            )
        translation_paths[arm_id] = path
        translation_bindings[arm_id] = binding

    if canonical:
        assert canonical_paths is not None
        d2l_path = None
        d2l_payload = None
        common = _with_external_arms(
            build_canonical_d2l_common_input_v1(
                source_artifacts=canonical_paths,
                s0_translation_artifact=translation_paths["s0"],
                s1_translation_artifact=translation_paths["s1"],
                selected_chapter_ids=(CHAPTER_ID,),
            ),
            translation_bindings=translation_bindings,
        )
    else:
        profile_path = _write_json(
            inputs / "producer" / "runtime_profile.json",
            {"profile_id": "technical_d2l_v1"},
        )
        d2l_path, d2l_payload = _d2l_package(
            inputs,
            translation_paths=translation_paths,
            profile_path=profile_path,
        )
        common = _full_common(
            d2l_payload,
            translation_bindings=translation_bindings,
        )
    universe = canonical_sha256(
        {"block_ids": [block.block_id for block in common.blocks]}
    )
    coverage = {
        "expected_block_count": len(common.blocks),
        "translated_block_count": sum(
            block.admission in {"translate", "translate_structured"}
            for block in common.blocks
        ),
        "preserved_block_count": sum(
            block.admission == "preserve" for block in common.blocks
        ),
        "excluded_block_count": sum(
            block.admission == "exclude" for block in common.blocks
        ),
        "review_held_block_count": sum(
            block.admission == "review_required" for block in common.blocks
        ),
        "missing_block_count": 0,
        "failed_block_count": 0,
        "block_universe_sha256": universe,
    }
    handoff = build_scoring_handoff_v1(
        workflow_run_id=WORKFLOW_RUN_ID,
        handoff_id="scoring_handoff_runtime_fixture",
        created_at=NOW,
        producer_code_commit=COMMIT,
        source_package_bindings=source_bindings,
        optional_bindings={"glossary": None, "context": None, "projection": None},
        translation_inputs=[
            {
                "arm_id": arm_id,
                "translation_artifact": translation_bindings[arm_id],
                "producer": {
                    "component_id": (
                        "translation"
                        if arm_id in {"s0", "s1"}
                        else f"{arm_id}_baseline"
                    ),
                    "component_run_id": (
                        "translation_runtime_fixture"
                        if arm_id in {"s0", "s1"}
                        else f"{arm_id}_runtime_fixture"
                    ),
                },
                "coverage": coverage,
                "source_binding": admitted,
            }
            for arm_id in ARM_IDS
        ],
    )
    handoff_path = _write_json(inputs / "handoffs" / "scoring_handoff.json", handoff)

    source_snapshot = (
        CommonSourceSnapshotV1(
            source_schema_id=common.source_schema_id,
            source_schema_version=common.source_schema_version,
            source_binding=common.source_binding,
            blocks=common.blocks,
        )
        if canonical
        else _sources()[0]
    )
    source_evidence_path = (
        canonical_paths.package_seal
        if canonical and canonical_paths is not None
        else d2l_path
    )
    assert source_evidence_path is not None
    evidence = [
        {
            "chapter_id": CHAPTER_ID,
            "source_artifact_id": "d2l-runtime-fixture",
            "source_artifact_sha256": physical_sha256(
                source_evidence_path.read_bytes()
            ),
            "source_evidence_kind": (
                "canonical_source_package_v1"
                if canonical
                else "d2l_evaluation_package"
            ),
        }
    ]
    manifest = build_benchmark_manifest_v1(
        [source_snapshot],
        evidence,
        benchmark_id="d2l-runtime-fixture",
        created_at=NOW,
        producer_code_commit=COMMIT,
        selected_chapter_ids=[CHAPTER_ID],
        selected_arm_ids=BENCHMARK_ARM_IDS,
    )
    overlays = OrderedDict()
    overlay_payloads = []
    for arm_id in BENCHMARK_ARM_IDS:
        overlay = build_overlay_from_common_arm_v1(
            common,
            chapter_id=CHAPTER_ID,
            arm_id=arm_id,
            benchmark_role=ARM_ROLES[arm_id],
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
        overlay_path = _write_json(
            inputs / "overlays" / arm_id / f"{CHAPTER_ID}.json",
            overlay,
        )
        overlays[(CHAPTER_ID, arm_id)] = overlay_path
        overlay_payloads.append(overlay)
    preflight = build_benchmark_preflight_v1(
        manifest,
        [source_snapshot],
        overlay_payloads,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    manifest_path = _write_json(inputs / "runtime" / "manifest.json", manifest)
    preflight_path = _write_json(inputs / "runtime" / "preflight.json", preflight)
    d2l_only_common = CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
        arms=tuple(arm for arm in common.arms if arm.arm_id in {"S0", "S1"}),
        translations=tuple(
            row for row in common.translations if row.arm_id in {"S0", "S1"}
        ),
    )
    runtime_common = augment_common_input_with_benchmark_overlays_v1(
        d2l_only_common,
        [
            overlay
            for overlay in overlay_payloads
            if overlay["arm"]["arm_id"] not in {"S0", "S1"}
        ],
    )
    config_path = _write_json(
        inputs / "runtime" / "config.json",
        _config_for_scorers(runtime_common, selected_scorer_ids),
    )

    authority, authority_paths = _authority_files(inputs)
    option_sha = canonical_sha256(
        {"settings_option_id": "evaluation_workflow_settings_v1", "revision": 1}
    )
    selection = _locked_selection(
        option_sha, selected_scorer_ids=selected_scorer_ids
    )
    template_path = materialize_workflow_scoring_baseline_template_v1(
        tmp_path / "baseline-template",
        template_id="d2l-five-arm-baseline-template-v1",
        created_at=NOW,
        producer_code_commit=COMMIT,
        source_binding_sha256=SOURCE_BINDING_SHA256,
        settings_option_id="evaluation_workflow_settings_v1",
        registered_option_sha256=option_sha,
        evaluation_profile_id="evaluation_fixture_v1",
        evaluation_profile_ref="profiles/evaluation_fixture_v1.json",
        policy_profile_id=None,
        policy_profile_ref=None,
        shared_selection_ref="selections/evaluation_five_chapter_v1.json",
        settings_authority=authority,
        artifact_sources=WorkflowScoringBaselineTemplateSourcesV1(
            external_translation_inputs=handoff["translation_inputs"][2:],
            external_translation_artifacts={
                translation_bindings[arm_id]["artifact_ref"]: translation_paths[arm_id]
                for arm_id in ARM_IDS[2:]
            },
            authority_artifacts=authority_paths,
        ),
        caveats=("Pre-run fixture template.",),
    )
    loaded_template = load_workflow_scoring_baseline_template_v1(template_path)
    output_root = tmp_path / "evaluation-output"
    registration = build_evaluation_workflow_registration_from_baseline_template_v1(
        loaded_template,
        scoring_handoff=handoff,
        locked_selection=selection,
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        output_root=output_root,
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation_runtime_fixture",
        evaluation_attempt_run_id="evaluation_runtime_fixture_attempt",
    )
    settings = registration.materialized_workflow_settings
    artifact_sources = WorkflowScoringRuntimeArtifactSourcesV1(
        scoring_handoff=handoff_path,
        d2l_evaluation_input=d2l_path,
        benchmark_manifest=manifest_path,
        benchmark_preflight=preflight_path,
        handoff_artifacts={
            **source_paths,
            **{
                translation_bindings[arm_id]["artifact_ref"]: translation_paths[arm_id]
                for arm_id in ARM_IDS
            },
        },
        authority_artifacts=authority_paths,
        overlays=overlays,
        chapter_configs=OrderedDict(((CHAPTER_ID, config_path),)),
    )
    arm_presentations = [
        {
            "arm_id": arm_id,
            "role": (
                "baseline"
                if arm_id == "s0"
                else "candidate"
                if arm_id == "s1"
                else "reference"
                if arm_id == "community"
                else "external_baseline"
            ),
            "kind": (
                "human_reference"
                if arm_id == "community"
                else "machine_baseline"
                if arm_id in {"google_nmt", "llm_lc"}
                else "system"
            ),
            "label": arm_id,
        }
        for arm_id in ARM_IDS
    ]
    display_names = {
        "sf_qe": "Semantic fidelity QE",
        "sf_bt": "Semantic fidelity back-translation",
        "pj": "Pairwise judge",
    }
    method_presentations = [
        {
            "display_name": display_names[method_id],
            "method": {
                "method_id": method_id,
                "method_version": (
                    "sf-qe-fixture-v1"
                    if method_id == "sf_qe"
                    else f"{method_id}-fixture-v1"
                ),
                "implementation_commit": COMMIT,
                "prompt_version": (
                    None if method_id == "sf_qe" else f"{method_id}-prompt-v1"
                ),
                "model_id": (
                    SF_QE_MODEL_ID
                    if method_id == "sf_qe"
                    else "evaluation-fixture-model"
                ),
            },
        }
        for method_id in selected_scorer_ids
    ]
    needs_llm = any(
        method_id in {"sf_bt", "pj"} for method_id in selected_scorer_ids
    )
    chapter_runtime_bindings = [
        {
            "chapter_id": CHAPTER_ID,
            "local_sf_qe_runtime_id": (
                "local_sf_qe.fixture.v1"
                if "sf_qe" in selected_scorer_ids
                else None
            ),
            "llm_roles_runtime_id": (
                "llm_roles.fixture.v1" if needs_llm else None
            ),
            "shared_ledger_runtime_id": (
                "shared_ledger.fixture.v1" if needs_llm else None
            ),
            "shared_ledger_relative_path": (
                "usage/attempt_ledger.sqlite3" if needs_llm else None
            ),
        }
    ]
    return {
        "registration": registration,
        "artifact_sources": artifact_sources,
        "arm_presentations": arm_presentations,
        "method_presentations": method_presentations,
        "chapter_runtime_bindings": chapter_runtime_bindings,
        "handoff": handoff,
        "settings": settings,
        "selection": selection,
        "template_path": template_path,
        "output_root": output_root,
    }


def _registry() -> EvaluationRuntimeObjectRegistryV1:
    return EvaluationRuntimeObjectRegistryV1(
        local_sf_qe_runtimes={
            "local_sf_qe.fixture.v1": LocalSfQeRuntimeV1(
                predictor=_Predictor(0.5),
                checkpoint_sha256="7" * 64,
                package_name="unbabel-comet",
                package_version="2.2.7",
                device="cpu",
                batch_size=8,
                clock=lambda: datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc),
                monotonic=lambda: 1.0,
            )
        },
        llm_role_runners={},
        shared_ledgers={},
    )


def _materialize(tmp_path: Path, *, canonical: bool = False):
    fixture = _fixture(tmp_path, canonical=canonical)
    bundle_path = materialize_workflow_scoring_runtime_bundle_v1(
        tmp_path / "bundle",
        registration=fixture["registration"],
        artifact_sources=fixture["artifact_sources"],
        arm_presentations=fixture["arm_presentations"],
        method_presentations=fixture["method_presentations"],
        chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
        caveats=("Fixture-only local scorer.",),
    )
    return fixture, bundle_path


def _write_runtime_registration(job_root: Path, template_path: Path) -> None:
    destination_root = job_root / "workflow" / "evaluation_baseline_template"
    for source in template_path.parent.rglob("*"):
        if not source.is_file():
            continue
        destination_file = destination_root / source.relative_to(template_path.parent)
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        destination_file.write_bytes(source.read_bytes())
    destination = destination_root / template_path.name
    row = {
        "schema_id": "WorkflowRuntimeRegistrationV1",
        "schema_version": "1.0.0",
        "job_id": JOB_ID,
        "source_binding_sha256": SOURCE_BINDING_SHA256,
        "translation_executor_id": "d2l_project_campaign_v1",
        "baseline_bundle": {
            "arm_ids": ["community", "google_nmt", "llm_lc"],
            "artifact_ref": (
                "workflow/evaluation_baseline_template/"
                "workflow_scoring_baseline_template_v1.json"
            ),
            "sha256": physical_sha256(destination.read_bytes()),
            "sha256_kind": "physical",
            "status": "ready",
        },
        "evaluation_executor_id": "evaluation_five_arm_benchmark_v1",
        "publication_executor_id": "selected_chapter_publication_v1",
        "supported_chapter_ids": [CHAPTER_ID],
        "status": "ready",
        "blockers": [],
        "integrity": {"registration_sha256": "0" * 64},
    }
    unhashed = copy.deepcopy(row)
    unhashed["integrity"].pop("registration_sha256")
    row["integrity"]["registration_sha256"] = canonical_sha256(unhashed)
    _write_json(job_root / "workflow_runtime_v1.json", row)


def test_file_backed_bundle_executes_and_echoes_registered_authority(
    tmp_path: Path,
) -> None:
    fixture, bundle_path = _materialize(tmp_path)

    loaded = load_workflow_scoring_runtime_bundle_v1(bundle_path)
    assert loaded.registered_option["registered_option_sha256"] == (
        fixture["registration"].registered_option_sha256
    )
    executor, handoff, settings, registered = (
        build_registered_evaluation_workflow_executor_v1(
            bundle_path,
            evaluation_output_root=fixture["output_root"],
            runtime_registry=_registry(),
        )
    )
    assert handoff == fixture["handoff"]
    assert settings == fixture["settings"]
    assert registered == loaded.registered_option

    executor.execute(handoff, lambda _path, _terminal: None)
    package = validate_evaluation_workflow_component_package_v1(
        fixture["output_root"], handoff, require_terminal=True
    )
    assert package["receipt"]["status"] == "accepted"
    assert package["workflow_settings"]["settings_sha256"] == settings["settings_sha256"]


def test_canonical_bundle_builds_from_source_package_and_exact_s0_s1(
    tmp_path: Path,
) -> None:
    fixture, bundle_path = _materialize(tmp_path, canonical=True)

    loaded = load_workflow_scoring_runtime_bundle_v1(bundle_path)
    assert loaded.bundle["bindings"]["d2l_input"] == {
        "mode": "canonical_source_package_v1",
        "legacy_artifact": None,
    }
    assert loaded.legacy_d2l_evaluation_input is None
    assert loaded.d2l_common_input.source_schema_id == "CanonicalSourcePackageV1"
    executor, handoff, settings, _registered = (
        build_registered_evaluation_workflow_executor_v1(
            bundle_path,
            evaluation_output_root=fixture["output_root"],
            runtime_registry=_registry(),
        )
    )
    executor.execute(handoff, lambda _path, _terminal: None)
    package = validate_evaluation_workflow_component_package_v1(
        fixture["output_root"], handoff, require_terminal=True
    )
    assert package["receipt"]["status"] == "accepted"
    assert package["workflow_settings"]["settings_sha256"] == settings["settings_sha256"]


def test_canonical_bundle_rejects_mixed_and_tampered_source_modes(
    tmp_path: Path,
) -> None:
    fixture, bundle_path = _materialize(tmp_path, canonical=True)
    bundle = json.loads(bundle_path.read_text("utf-8"))
    bundle["bindings"]["d2l_input"] = {
        "mode": "canonical_source_package_v1",
        "legacy_artifact": {
            "artifact_ref": "runtime/d2l_evaluation_input_v1.json",
            "artifact_kind": "d2l_evaluation_input_v1",
            "schema_version": "1.0.0",
            "sha256": "f" * 64,
            "sha256_kind": "physical",
        },
    }
    with pytest.raises(ContractValidationError, match="canonical mode cannot carry"):
        validate_workflow_scoring_runtime_bundle_v1(bundle)

    source_path = fixture["artifact_sources"].handoff_artifacts[
        "source/document.json"
    ]
    source_path.write_bytes(source_path.read_bytes() + b" ")
    with pytest.raises(ContractValidationError, match="producer binding"):
        materialize_workflow_scoring_runtime_bundle_v1(
            tmp_path / "tampered",
            registration=fixture["registration"],
            artifact_sources=fixture["artifact_sources"],
            arm_presentations=fixture["arm_presentations"],
            method_presentations=fixture["method_presentations"],
            chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
        )


def test_workflow_runtime_loader_uses_exact_registered_ref_without_scan(
    tmp_path: Path,
) -> None:
    fixture, _bundle_path = _materialize(tmp_path)
    job_root = tmp_path / "job"
    _write_runtime_registration(job_root, fixture["template_path"])
    _write_json(job_root / "workflow" / "ignored.json", {"not": "a bundle"})

    loaded = load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
        job_root,
        expected_job_id=JOB_ID,
        expected_source_binding_sha256=SOURCE_BINDING_SHA256,
        selected_chapter_ids=[CHAPTER_ID],
    )
    registration = build_evaluation_workflow_registration_from_baseline_template_v1(
        loaded,
        scoring_handoff=fixture["handoff"],
        locked_selection=fixture["selection"],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        output_root=fixture["output_root"],
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation_runtime_fixture",
        evaluation_attempt_run_id="evaluation_runtime_fixture_attempt",
    )

    assert registration.materialized_workflow_settings == fixture["settings"]
    assert loaded.registered_option["settings_option_id"] == (
        "evaluation_workflow_settings_v1"
    )


def test_bundle_rejects_tampered_runtime_file(tmp_path: Path) -> None:
    _fixture_data, bundle_path = _materialize(tmp_path)
    loaded = load_workflow_scoring_runtime_bundle_v1(bundle_path)
    target = loaded.file_paths["translations/community.json"]
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(ContractValidationError, match="runtime bundle file hash drift"):
        load_workflow_scoring_runtime_bundle_v1(bundle_path)


def test_factory_rejects_unregistered_runtime_id(tmp_path: Path) -> None:
    fixture, bundle_path = _materialize(tmp_path)
    empty_registry = EvaluationRuntimeObjectRegistryV1(
        local_sf_qe_runtimes={},
        llm_role_runners={},
        shared_ledgers={},
    )
    executor, handoff, _settings, _registered = (
        build_registered_evaluation_workflow_executor_v1(
            bundle_path,
            evaluation_output_root=fixture["output_root"],
            runtime_registry=empty_registry,
        )
    )

    with pytest.raises(
        ContractValidationError, match=r"runtime object .* is not registered"
    ):
        executor.execute(handoff, lambda _path, _terminal: None)


def test_workflow_runtime_loader_rejects_foreign_registered_bundle(
    tmp_path: Path,
) -> None:
    fixture, _bundle_path = _materialize(tmp_path)
    job_root = tmp_path / "job"
    _write_runtime_registration(job_root, fixture["template_path"])
    registration_path = job_root / "workflow_runtime_v1.json"
    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    registration["baseline_bundle"]["artifact_ref"] = "workflow/foreign.json"
    registration["baseline_bundle"]["sha256"] = "f" * 64
    unhashed = copy.deepcopy(registration)
    unhashed["integrity"].pop("registration_sha256")
    registration["integrity"]["registration_sha256"] = canonical_sha256(unhashed)
    _write_json(registration_path, registration)

    with pytest.raises(Exception, match="Workflow artifact is missing"):
        load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
            job_root,
            expected_job_id=JOB_ID,
            expected_source_binding_sha256=SOURCE_BINDING_SHA256,
            selected_chapter_ids=[CHAPTER_ID],
        )
