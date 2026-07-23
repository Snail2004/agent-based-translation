from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import (
    seal_translation_artifact,
    validate_translation_artifact,
)
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    SCORING_FRAGMENT_SCHEMA,
    STAGE_IDS,
    build_scoring_handoff_fragment,
    canonical_sha256,
    file_sha256,
    validate_component_manifest,
    validate_scoring_handoff_fragment,
)
from pipeline.prepass.d2l_project_campaign_v2 import (
    D2LCampaignError,
    LoadedProject,
    bind_component_plan,
    load_campaign,
    load_project,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    RUNNER_SCHEMA,
    ComponentPlan,
)


STAGE_RUNNER_VERSION = "d2l_project_stage_runner_v1"
DRY_PROFILE_ID = "dry_no_api_identity_projection_v1"


class D2LStageRunnerError(RuntimeError):
    """Raised when a campaign stage cannot be executed without identity drift."""


_STAGE_ARTIFACTS: dict[str, tuple[dict[str, Any], ...]] = {
    "preflight": (
        {
            "artifact_ref": "art_preflight",
            "artifact_kind": "d2l_campaign_preflight",
            "schema_version": "d2l_campaign_stage_preflight_v1",
            "relative_path": "artifacts/preflight/report.json",
            "parent_artifact_refs": [],
        },
    ),
    "b1_candidate_discovery": (
        {
            "artifact_ref": "art_b1_candidate_discovery",
            "artifact_kind": "d2l_candidate_discovery",
            "schema_version": "d2l_candidate_discovery_dry_v1",
            "relative_path": "artifacts/b1_candidate_discovery/candidates.json",
            "parent_artifact_refs": ["art_preflight"],
        },
        {
            "artifact_ref": "art_b1_proposal_timeline",
            "artifact_kind": "d2l_candidate_proposal_timeline",
            "schema_version": "d2l_candidate_proposal_timeline_v1",
            "relative_path": "artifacts/b1_candidate_discovery/proposal_timeline.json",
            "parent_artifact_refs": ["art_preflight"],
        },
    ),
    "candidate_index": (
        {
            "artifact_ref": "art_candidate_index",
            "artifact_kind": "d2l_candidate_index",
            "schema_version": "d2l_candidate_index_dry_v1",
            "relative_path": "artifacts/candidate_index/index.json",
            "parent_artifact_refs": [
                "art_b1_candidate_discovery",
                "art_b1_proposal_timeline",
            ],
        },
    ),
    "b2_admission_translation": (
        {
            "artifact_ref": "art_b2_admission",
            "artifact_kind": "d2l_b2_admission_decisions",
            "schema_version": "d2l_b2_admission_dry_v1",
            "relative_path": "artifacts/b2_admission_translation/decisions.json",
            "parent_artifact_refs": ["art_candidate_index"],
        },
    ),
    "auditor_morphology": (
        {
            "artifact_ref": "art_auditor_morphology",
            "artifact_kind": "d2l_morphology_decisions",
            "schema_version": "d2l_morphology_dry_v1",
            "relative_path": "artifacts/auditor_morphology/decisions.json",
            "parent_artifact_refs": ["art_b2_admission"],
        },
    ),
    "auditor_target_collision": (
        {
            "artifact_ref": "art_auditor_target_collision",
            "artifact_kind": "d2l_target_collision_decisions",
            "schema_version": "d2l_target_collision_dry_v1",
            "relative_path": "artifacts/auditor_target_collision/decisions.json",
            "parent_artifact_refs": ["art_auditor_morphology"],
        },
    ),
    "auditor_multi_target": (
        {
            "artifact_ref": "art_auditor_multi_target",
            "artifact_kind": "d2l_multi_target_decisions",
            "schema_version": "d2l_multi_target_dry_v1",
            "relative_path": "artifacts/auditor_multi_target/decisions.json",
            "parent_artifact_refs": ["art_auditor_target_collision"],
        },
    ),
    "glossary_seal": (
        {
            "artifact_ref": "art_glossary",
            "artifact_kind": "glossary",
            "schema_version": "D2LGlossaryDryV1",
            "relative_path": "artifacts/glossary_seal/glossary.json",
            "parent_artifact_refs": ["art_auditor_multi_target"],
        },
        {
            "artifact_ref": "art_glossary_memory_delta",
            "artifact_kind": "memory_delta_v1",
            "schema_version": "memory_delta_v1",
            "relative_path": "artifacts/glossary_seal/memory_delta.json",
            "parent_artifact_refs": ["art_glossary"],
        },
    ),
    "translator": (
        {
            "artifact_ref": "art_translation_s0",
            "artifact_kind": "translation_artifact",
            "schema_version": "TranslationArtifactV1",
            "relative_path": "artifacts/translator/s0.json",
            "parent_artifact_refs": ["art_glossary"],
        },
        {
            "artifact_ref": "art_translation_s1",
            "artifact_kind": "translation_artifact",
            "schema_version": "TranslationArtifactV1",
            "relative_path": "artifacts/translator/s1.json",
            "parent_artifact_refs": ["art_glossary"],
        },
    ),
    "translation_quality_audit": (
        {
            "artifact_ref": "art_translation_quality_observations",
            "artifact_kind": "translation_quality_observations",
            "schema_version": "d2l_translation_quality_observations_dry_v1",
            "relative_path": "artifacts/quality/observations.json",
            "parent_artifact_refs": ["art_translation_s0", "art_translation_s1"],
        },
        {
            "artifact_ref": "art_translation_quality_state",
            "artifact_kind": "translation_quality_state",
            "schema_version": "d2l_translation_quality_state_dry_v1",
            "relative_path": "artifacts/quality/state.json",
            "parent_artifact_refs": ["art_translation_quality_observations"],
        },
    ),
    "scoring_handoff_fragment": (
        {
            "artifact_ref": "art_scoring_handoff_fragment",
            "artifact_kind": "scoring_handoff_fragment",
            "schema_version": SCORING_FRAGMENT_SCHEMA,
            "relative_path": "scoring_handoff_fragment.json",
            "parent_artifact_refs": [
                "art_glossary",
                "art_translation_s0",
                "art_translation_s1",
                "art_translation_quality_state",
            ],
        },
    ),
}


_STAGE_PRODUCERS = {
    "preflight": "d2l_campaign_preflight",
    "b1_candidate_discovery": "d2l_candidate_builder",
    "candidate_index": "d2l_candidate_indexer",
    "b2_admission_translation": "d2l_b2_admission_builder",
    "auditor_morphology": "d2l_morphology_auditor",
    "auditor_target_collision": "d2l_target_collision_auditor",
    "auditor_multi_target": "d2l_multi_target_auditor",
    "glossary_seal": "d2l_glossary_writer",
    "translator": "d2l_translator",
    "translation_quality_audit": "d2l_translation_quality_auditor",
    "scoring_handoff_fragment": "d2l_scoring_handoff_writer",
}


def _write_json_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise D2LStageRunnerError(f"immutable stage artifact drift: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D2LStageRunnerError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise D2LStageRunnerError(f"{label} must be a JSON object")
    return value


def _sealed(body: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(body))
    payload["integrity"] = {"payload_sha256": canonical_sha256(payload)}
    return payload


def _selected_rows(project: LoadedProject, chapter_ids: Sequence[str]) -> list[dict[str, Any]]:
    selected = set(chapter_ids)
    return [dict(row) for row in project.block_rows if row["chapter_id"] in selected]


def _validate_project_against_campaign(
    campaign: Mapping[str, Any], project: LoadedProject
) -> list[dict[str, Any]]:
    config = campaign["config"]
    universe = campaign["universe"]
    if project.source_binding != config["source_binding"]:
        raise D2LStageRunnerError("source project binding does not match campaign")
    rows = _selected_rows(project, config["selected_chapter_ids"])
    observed = [row["block_id"] for row in rows]
    declared = [row["block_id"] for row in universe["blocks"]]
    if observed != declared:
        raise D2LStageRunnerError("selected source block order drift")
    for source, compact in zip(rows, universe["blocks"], strict=True):
        if source["channel"] != compact["channel"]:
            raise D2LStageRunnerError("selected source admission drift")
        if canonical_sha256(source["source_text"]) != compact["source_text_sha256"]:
            raise D2LStageRunnerError("selected source text drift")
        if canonical_sha256(source["clean_text"]) != compact["clean_text_sha256"]:
            raise D2LStageRunnerError("selected clean text drift")
    return rows


def _component_attempt(campaign_root: Path, config: Mapping[str, Any]) -> int:
    component_root = campaign_root / str(config["state_layout"]["component_root"])
    manifest = validate_component_manifest(
        _load_json(component_root / "component_manifest.json", "component manifest")
    )
    if manifest["component_run_id"] != config["component_run_id"]:
        raise D2LStageRunnerError("component manifest run identity drift")
    return int(manifest["component_attempt_id"])


def _stage_totals(campaign: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    universe = campaign["universe"]
    limits = campaign["config"]["limits"]
    block_count = int(universe["block_count"])
    b1_windows = int(universe["window_estimates"]["b1"]["window_count"])
    translator_windows = int(universe["window_estimates"]["translator"]["window_count"])
    role_caps = {
        row["role_id"]: int(row["semantic_request_cap"])
        for row in limits["roles"]
    }
    return {
        "preflight": (block_count, "blocks"),
        "b1_candidate_discovery": (b1_windows, "windows"),
        "candidate_index": (block_count, "blocks"),
        "b2_admission_translation": (int(role_caps["d2l.b2.admission"]), "packets"),
        "auditor_morphology": (int(role_caps["d2l.b2.morphology"]), "components"),
        "auditor_target_collision": (
            int(role_caps["d2l.b2.target_collision"]),
            "components",
        ),
        "auditor_multi_target": (int(role_caps["d2l.b2.multi_target"]), "components"),
        "glossary_seal": (1, "seal"),
        "translator": (translator_windows * 2, "windows"),
        "translation_quality_audit": (translator_windows * 2, "windows"),
        "scoring_handoff_fragment": (2, "arms"),
    }


def build_component_plan(
    *,
    campaign_root: str | Path,
    job_root: str | Path,
    code_root: str | Path,
    python_executable: str | Path | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    if not dry_run:
        raise D2LStageRunnerError("live stage execution is not enabled by the 0-API runner")
    campaign = load_campaign(campaign_root)
    project = load_project(job_root, verify_tree=True)
    _validate_project_against_campaign(campaign, project)
    config = campaign["config"]
    root = Path(campaign_root).resolve()
    cwd = Path(code_root).resolve()
    if not (cwd / "pipeline").is_dir():
        raise D2LStageRunnerError("code_root must be the THESIS_RUNTIME_TOOL directory")
    executable = str(Path(python_executable or sys.executable).resolve())
    totals = _stage_totals(campaign)
    stages: list[dict[str, Any]] = []
    for stage_id in STAGE_IDS:
        total, unit = totals[stage_id]
        specs = [
            {
                **deepcopy(spec),
                "metadata": {
                    "execution_mode": "dry_no_api",
                    "stage_runner_version": STAGE_RUNNER_VERSION,
                },
            }
            for spec in _STAGE_ARTIFACTS[stage_id]
        ]
        command = [
            executable,
            "-m",
            "pipeline.scripts.run_d2l_project_campaign",
            "execute-stage",
            "--campaign-root",
            str(root),
            "--job-root",
            str(project.job_root),
            "--stage-id",
            stage_id,
            "--dry-run",
        ]
        stages.append(
            {
                "stage_id": stage_id,
                "producer": _STAGE_PRODUCERS[stage_id],
                "command": command,
                "cwd": str(cwd),
                "artifact_specs": specs,
                "total": total,
                "unit": unit,
                "work_id": f"work_{stage_id}",
                "mode": "execute",
                "timeout_seconds": 600,
                "receipt_ref": None,
            }
        )
    plan = {
        "schema": RUNNER_SCHEMA,
        "workflow_run_id": config["workflow_run_id"],
        "component_run_id": config["component_run_id"],
        "pipeline_id": config["pipeline_id"],
        "pipeline_version": config["pipeline_version"],
        "source_binding": deepcopy(config["source_binding"]),
        "config_sha256": config["integrity"]["payload_sha256"],
        "code_revision": config["code_revision"],
        "selected_chapter_ids": list(config["selected_chapter_ids"]),
        "stages": stages,
        "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
    }
    normalized = ComponentPlan.from_mapping(plan).canonical_mapping()
    bind_component_plan(root, normalized)
    return normalized


def _dry_windows(rows: Sequence[Mapping[str, Any]], target_tokens: int) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    current: list[str] = []
    current_tokens = 0
    for row in rows:
        if row["channel"] != "semantic_text":
            continue
        estimate = max(1, (len(str(row["clean_text"] or row["source_text"])) + 3) // 4)
        if current and current_tokens + estimate > target_tokens:
            windows.append(
                {
                    "window_id": f"b1_window_{len(windows) + 1:04d}",
                    "block_ids": current,
                    "estimated_source_tokens": current_tokens,
                }
            )
            current = []
            current_tokens = 0
        current.append(str(row["block_id"]))
        current_tokens += estimate
    if current:
        windows.append(
            {
                "window_id": f"b1_window_{len(windows) + 1:04d}",
                "block_ids": current,
                "estimated_source_tokens": current_tokens,
            }
        )
    return windows


def _evaluation_source_binding(project: LoadedProject) -> dict[str, Any]:
    inputs = project.projection.get("inputs")
    policy = project.projection.get("policy")
    integrity = project.projection.get("integrity")
    if not isinstance(inputs, Mapping) or not isinstance(policy, Mapping):
        raise D2LStageRunnerError("admitted projection lacks source identities")
    if not isinstance(integrity, Mapping):
        raise D2LStageRunnerError("admitted projection lacks integrity")
    policy_sha = policy.get("policy_sha256") or canonical_sha256(policy)
    return {
        "binding_kind": "canonical_source_package_v1",
        "project_id": project.manifest["project_id"],
        "document_id": project.manifest["document_doc_id"],
        "document": {
            "schema_version": str(inputs["document"]["schema_version"]),
            "sha256": str(inputs["document"]["sha256"]).lower(),
        },
        "structure": {
            "schema_version": str(inputs["structure"]["schema_version"]),
            "sha256": str(inputs["structure"]["sha256"]).lower(),
        },
        "asset_manifest": {
            "schema_version": str(inputs["asset_manifest"]["schema_version"]),
            "sha256": str(inputs["asset_manifest"]["sha256"]).lower(),
        },
        "admitted_projection": {
            "schema_version": str(project.projection["schema_version"]),
            "payload_sha256": str(integrity["payload_sha256"]).lower(),
        },
        "admission_policy": {
            "policy_id": str(policy["policy_id"]),
            "policy_version": str(policy["policy_version"]),
            "policy_sha256": str(policy_sha).lower(),
        },
    }


def _translation_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        channel = row["channel"]
        if channel in {"semantic_text", "structured_translate"}:
            status = "translated"
            target_text: str | None = str(row["source_text"])
        elif channel == "preserve_only":
            status = "preserved"
            target_text = str(row["source_text"])
        elif channel == "review_required":
            status = "review_held"
            target_text = None
        else:
            raise D2LStageRunnerError(f"unsupported channel: {channel}")
        result.append(
            {
                "block_id": row["block_id"],
                "status": status,
                "target_text": target_text,
                "error_code": None,
            }
        )
    return result


def _translation_artifact(
    *,
    campaign: Mapping[str, Any],
    project: LoadedProject,
    rows: Sequence[Mapping[str, Any]],
    arm_id: str,
    component_attempt_id: int,
) -> dict[str, Any]:
    config = campaign["config"]
    translations = _translation_rows(rows)
    counts = Counter(row["status"] for row in translations)
    payload = {
        "schema_id": "TranslationArtifactV1",
        "schema_version": "1.0.0",
        "artifact_id": f"{config['component_run_id']}_{arm_id}_dry",
        "created_at": campaign["seal"]["created_at"],
        "producer": {
            "workstream": "d2l",
            "component": "d2l_project_stage_runner",
            "component_version": STAGE_RUNNER_VERSION,
            "code_commit": config["code_revision"],
        },
        "source_binding": _evaluation_source_binding(project),
        "run_identity": {
            "logical_run_id": config["component_run_id"],
            "attempt_run_id": f"{config['component_run_id']}_a{component_attempt_id}_{arm_id}",
            "arm_id": arm_id,
            "profile_id": DRY_PROFILE_ID,
            "profile_config_sha256": str(config["integrity"]["payload_sha256"]).lower(),
            "source_language": "en",
            "target_language": "vi",
        },
        "translations": translations,
        "coverage": {
            "source_block_count": len(translations),
            "eligible_count": counts["translated"] + counts["missing"] + counts["failed"],
            "translated_count": counts["translated"],
            "preserved_count": counts["preserved"],
            "excluded_count": counts["excluded"],
            "review_held_count": counts["review_held"],
            "missing_count": counts["missing"],
            "failed_count": counts["failed"],
        },
        "integrity": {"artifact_sha256": "0" * 64},
    }
    artifact = seal_translation_artifact(payload)
    validate_translation_artifact(artifact)
    return artifact


def _artifact_binding(component_root: Path, spec: Mapping[str, Any]) -> dict[str, str]:
    path = component_root / str(spec["relative_path"])
    return {
        "artifact_ref": str(spec["artifact_ref"]),
        "artifact_kind": str(spec["artifact_kind"]),
        "schema_version": str(spec["schema_version"]),
        "sha256": file_sha256(path),
        "sha256_kind": "physical",
    }


def _role(config: Mapping[str, Any], role_id: str) -> dict[str, Any]:
    for row in config["semantic_roles"]:
        if row["role_id"] == role_id:
            return dict(row)
    raise D2LStageRunnerError(f"campaign lacks semantic role: {role_id}")


def _dry_stage_payloads(
    *,
    campaign: Mapping[str, Any],
    project: LoadedProject,
    rows: Sequence[Mapping[str, Any]],
    stage_id: str,
    component_attempt_id: int,
) -> dict[str, dict[str, Any]]:
    config = campaign["config"]
    universe = campaign["universe"]
    created_at = campaign["seal"]["created_at"]
    specs = {spec["artifact_ref"]: spec for spec in _STAGE_ARTIFACTS[stage_id]}
    common = {
        "execution_mode": "dry_no_api",
        "semantic_output_authority": False,
        "component_run_id": config["component_run_id"],
        "component_attempt_id": component_attempt_id,
        "selected_chapter_ids": list(config["selected_chapter_ids"]),
        "created_at": created_at,
    }
    if stage_id == "preflight":
        return {
            "art_preflight": _sealed(
                {
                    "schema_version": specs["art_preflight"]["schema_version"],
                    **common,
                    "selected_block_count": len(rows),
                    "channel_counts": dict(universe["channel_counts"]),
                    "source_binding_sha256": config["source_binding_sha256"],
                    "campaign_config_sha256": config["integrity"]["payload_sha256"],
                    "status": "dry_ready",
                }
            )
        }
    if stage_id == "b1_candidate_discovery":
        windows = _dry_windows(rows, 1500)
        discovery = _sealed(
            {
                "schema_version": specs["art_b1_candidate_discovery"]["schema_version"],
                **common,
                "windows": windows,
                "candidate_observations": [],
                "status": "dry_no_semantic_call",
            }
        )
        timeline = _sealed(
            {
                "schema_version": specs["art_b1_proposal_timeline"]["schema_version"],
                **common,
                "proposal_events": [],
                "committed_memory_change": False,
            }
        )
        return {
            "art_b1_candidate_discovery": discovery,
            "art_b1_proposal_timeline": timeline,
        }
    if stage_id == "candidate_index":
        return {
            "art_candidate_index": _sealed(
                {
                    "schema_version": specs["art_candidate_index"]["schema_version"],
                    **common,
                    "source_observation_count": 0,
                    "unique_surface_count": 0,
                    "candidates": [],
                }
            )
        }
    if stage_id == "b2_admission_translation":
        return {
            "art_b2_admission": _sealed(
                {
                    "schema_version": specs["art_b2_admission"]["schema_version"],
                    **common,
                    "candidate_count": 0,
                    "decisions": [],
                }
            )
        }
    if stage_id.startswith("auditor_"):
        ref = next(iter(specs))
        return {
            ref: _sealed(
                {
                    "schema_version": specs[ref]["schema_version"],
                    **common,
                    "component_count": 0,
                    "decisions": [],
                }
            )
        }
    if stage_id == "glossary_seal":
        glossary = _sealed(
            {
                "schema_version": specs["art_glossary"]["schema_version"],
                **common,
                "terms": [],
                "term_count": 0,
                "publication_status": "dry_not_publishable",
            }
        )
        delta = _sealed(
            {
                "schema_version": specs["art_glossary_memory_delta"]["schema_version"],
                **common,
                "lifecycle": "committed",
                "changes": [],
                "change_count": 0,
                "dry_run": True,
            }
        )
        return {"art_glossary": glossary, "art_glossary_memory_delta": delta}
    if stage_id == "translator":
        return {
            "art_translation_s0": _translation_artifact(
                campaign=campaign,
                project=project,
                rows=rows,
                arm_id="s0",
                component_attempt_id=component_attempt_id,
            ),
            "art_translation_s1": _translation_artifact(
                campaign=campaign,
                project=project,
                rows=rows,
                arm_id="s1",
                component_attempt_id=component_attempt_id,
            ),
        }
    if stage_id == "translation_quality_audit":
        observations = _sealed(
            {
                "schema_version": specs["art_translation_quality_observations"]["schema_version"],
                **common,
                "audited_arm_ids": ["s0", "s1"],
                "issues": [],
                "status": "dry_not_audited",
            }
        )
        state = _sealed(
            {
                "schema_version": specs["art_translation_quality_state"]["schema_version"],
                **common,
                "issue_count": 0,
                "blocking": False,
                "status": "dry_not_audited",
            }
        )
        return {
            "art_translation_quality_observations": observations,
            "art_translation_quality_state": state,
        }
    if stage_id != "scoring_handoff_fragment":
        raise D2LStageRunnerError(f"unsupported stage: {stage_id}")

    component_root = Path(campaign["root"]) / str(config["state_layout"]["component_root"])
    index = _load_json(component_root / "artifact_index.json", "artifact index")
    indexed = {row["artifact_ref"]: row for row in index.get("artifacts", [])}
    eligible_ids = [
        str(row["block_id"])
        for row in rows
        if row["channel"] != "review_required"
    ]
    translated_count = sum(
        row["channel"] in {"semantic_text", "structured_translate"} for row in rows
    )
    preserved_count = sum(row["channel"] == "preserve_only" for row in rows)
    coverage = {
        "admitted_block_count": len(eligible_ids),
        "translated_block_count": translated_count,
        "preserved_block_count": preserved_count,
        "missing_block_count": 0,
        "failed_block_count": 0,
        "ordered_block_ids_sha256": canonical_sha256(eligible_ids),
        "status": "exact_cover",
    }
    translation_inputs = []
    for arm_id in ("s0", "s1"):
        ref = f"art_translation_{arm_id}"
        artifact_spec = next(
            spec for spec in _STAGE_ARTIFACTS["translator"] if spec["artifact_ref"] == ref
        )
        indexed_row = indexed.get(ref)
        if not isinstance(indexed_row, Mapping):
            raise D2LStageRunnerError(f"translation artifact is not indexed: {ref}")
        translation_inputs.append(
            {
                "arm_id": arm_id,
                "artifact": _artifact_binding(component_root, artifact_spec),
                "producer_component_run_id": config["component_run_id"],
                "producer_component_attempt_id": int(indexed_row["component_attempt_id"]),
                "profile_id": DRY_PROFILE_ID,
                "profile_sha256": _role(config, f"d2l.translator.{arm_id}")[
                    "semantic_role_sha256"
                ],
                "config_sha256": config["integrity"]["payload_sha256"],
                "selected_chapter_ids": list(config["selected_chapter_ids"]),
                "coverage": dict(coverage),
                "source_binding_sha256": canonical_sha256(config["source_binding"]),
            }
        )
    glossary_spec = _STAGE_ARTIFACTS["glossary_seal"][0]
    fragment = build_scoring_handoff_fragment(
        workflow_run_id=config["workflow_run_id"],
        translation_component_run_id=config["component_run_id"],
        translation_component_attempt_id=component_attempt_id,
        reserved_evaluation_component_run_id=f"eval_{config['workflow_run_id']}",
        artifact_ref="art_scoring_handoff_fragment",
        source_binding=config["source_binding"],
        translation_inputs=translation_inputs,
        glossary_binding=_artifact_binding(component_root, glossary_spec),
        context_memory_binding=None,
        selected_chapter_ids=config["selected_chapter_ids"],
        admitted_universe={
            "ordered_block_ids_sha256": canonical_sha256(eligible_ids),
            "block_count": len(eligible_ids),
            "status": "exact_cover",
        },
        producer_lineage={
            "git_commit": config["code_revision"],
            "pipeline_version": config["pipeline_version"],
            "config_sha256": config["integrity"]["payload_sha256"],
            "code_sha256": file_sha256(Path(__file__)),
        },
        created_at=created_at,
    )
    validate_scoring_handoff_fragment(fragment)
    return {"art_scoring_handoff_fragment": fragment}


def execute_stage(
    *,
    campaign_root: str | Path,
    job_root: str | Path,
    stage_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    if stage_id not in STAGE_IDS:
        raise D2LStageRunnerError(f"unknown stage_id: {stage_id}")
    if not dry_run:
        raise D2LStageRunnerError("live stage execution is not enabled by the 0-API runner")
    campaign = load_campaign(campaign_root)
    project = load_project(job_root, verify_tree=True)
    rows = _validate_project_against_campaign(campaign, project)
    root = Path(campaign_root).resolve()
    config = campaign["config"]
    attempt = _component_attempt(root, config)
    component_root = root / str(config["state_layout"]["component_root"])
    payloads = _dry_stage_payloads(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage_id,
        component_attempt_id=attempt,
    )
    expected_refs = {spec["artifact_ref"] for spec in _STAGE_ARTIFACTS[stage_id]}
    if set(payloads) != expected_refs:
        raise D2LStageRunnerError("stage payloads do not exact-cover artifact specs")
    written = []
    for spec in _STAGE_ARTIFACTS[stage_id]:
        path = component_root / str(spec["relative_path"])
        _write_json_immutable(path, payloads[str(spec["artifact_ref"])])
        written.append(
            {
                "artifact_ref": spec["artifact_ref"],
                "relative_path": spec["relative_path"],
                "sha256": file_sha256(path),
            }
        )
    return {
        "stage_id": stage_id,
        "component_attempt_id": attempt,
        "execution_mode": "dry_no_api",
        "artifacts": written,
    }


__all__ = [
    "DRY_PROFILE_ID",
    "D2LStageRunnerError",
    "STAGE_RUNNER_VERSION",
    "build_component_plan",
    "execute_stage",
]
