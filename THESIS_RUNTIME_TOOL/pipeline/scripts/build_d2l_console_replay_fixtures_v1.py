"""Build a deterministic 0-API D2L Translation component package.

This is a contract fixture, not a historical run and not a parent workflow
package.  The neutral relay owns the parent manifest and global event stream.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    COMPONENT_ID,
    D2LTranslationComponentEventWriter,
    SCORING_FRAGMENT_SCHEMA,
    STAGE_IDS,
    build_component_manifest,
    build_scoring_handoff_fragment,
    build_stage_plan,
    canonical_sha256,
    file_sha256,
    validate_translation_component_package,
    write_component_manifest_snapshot,
    write_json,
)


FIXTURE_VERSION = "d2l_translation_component_fixture_v1"
WORKFLOW_RUN_ID = "wf_fixture_translation_v1"
COMPONENT_RUN_ID = "tr_fixture_component_v1"
ATTEMPT_ID = 1
CHAPTER_IDS = ["d2l_multilayer_perceptrons"]
GIT_COMMIT = "a" * 40
CONFIG_SHA = "b" * 64
CODE_SHA = "c" * 64
SOURCE_SHA_SEED = "fixture-source"


def _ts(seconds: int) -> str:
    return (
        datetime(2026, 7, 22, 0, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _binding(ref: str, kind: str, schema: str, seed: str) -> dict[str, Any]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": sha256(seed.encode("utf-8")).hexdigest().upper(),
        "sha256_kind": "physical",
    }


def _source_binding() -> dict[str, Any]:
    return {
        "schema": "canonical_source_binding_v1",
        "document": _binding("src_document", "source_document", "document_v1", "document"),
        "structure_manifest": _binding(
            "src_structure", "structure_manifest", "structure_manifest_v1", "structure"
        ),
        "asset_manifest": _binding(
            "src_assets", "asset_manifest", "asset_manifest_v1", "assets"
        ),
        "admitted_projection": _binding(
            "src_projection", "admitted_projection", "admitted_projection_v1", "projection"
        ),
        "normalization_receipt": _binding(
            "src_receipt", "normalization_receipt", "normalization_receipt_v1", "receipt"
        ),
        "package_seal": _binding(
            "src_package_seal", "source_package_seal", "source_package_seal_v1", "seal"
        ),
    }


def _progress(completed: int, total: int | None, unit: str) -> dict[str, Any]:
    return {"completed": completed, "total": total, "unit": unit}


def _artifact_row(
    manifest: dict[str, Any],
    *,
    ref: str,
    kind: str,
    schema: str,
    stage_id: str,
    relative_path: str,
    created_event_id: str,
    sha256_value: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "workflow_run_id": manifest["workflow_run_id"],
        "flow_kind": manifest["flow_kind"],
        "component_id": COMPONENT_ID,
        "component_run_id": manifest["component_run_id"],
        "component_attempt_id": manifest["component_attempt_id"],
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": sha256_value,
        "sha256_kind": "physical",
        "producer_stage_id": stage_id,
        "parent_artifact_refs": [],
        "created_event_id": created_event_id,
        "relative_path": relative_path,
        "availability": "available",
        "metadata": metadata or {},
    }


def build_fixture(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    source_binding = _source_binding()
    manifest = build_component_manifest(
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=ATTEMPT_ID,
        pipeline_id="d2l_terminology",
        pipeline_version="translation_component_v1",
        source_binding=source_binding,
        config_sha256=CONFIG_SHA,
        code_revision=GIT_COMMIT,
        selected_chapter_ids=CHAPTER_IDS,
        started_at=_ts(0),
        updated_at=_ts(0),
    )
    initial_manifest_binding = write_component_manifest_snapshot(root, manifest)

    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    s0_path = artifacts_dir / "translation_s0.json"
    s1_path = artifacts_dir / "translation_s1.json"
    glossary_path = artifacts_dir / "glossary.json"
    s0_path.write_text('{"arm":"s0","blocks":["b001"]}\n', encoding="utf-8")
    s1_path.write_text('{"arm":"s1","blocks":["b001"]}\n', encoding="utf-8")
    glossary_path.write_text('{"entries":[]}\n', encoding="utf-8")

    universe_sha = sha256(b"d2l_multilayer_perceptrons_b001").hexdigest().upper()
    source_binding_sha = canonical_sha256(source_binding)
    translation_inputs = []
    for arm_id, path, profile_id in (
        ("s0", s0_path, "d2l_s0_v1"),
        ("s1", s1_path, "d2l_s1_v1"),
    ):
        translation_inputs.append(
            {
                "arm_id": arm_id,
                "artifact": _binding(
                    f"art_translation_{arm_id}",
                    "translation_artifact",
                    "TranslationArtifactV1",
                    path.read_bytes().decode("utf-8"),
                ),
                "producer_component_run_id": COMPONENT_RUN_ID,
                "producer_component_attempt_id": ATTEMPT_ID,
                "profile_id": profile_id,
                "profile_sha256": "d" * 64,
                "config_sha256": CONFIG_SHA,
                "selected_chapter_ids": CHAPTER_IDS,
                "coverage": {
                    "admitted_block_count": 1,
                    "translated_block_count": 1,
                    "preserved_block_count": 0,
                    "missing_block_count": 0,
                    "failed_block_count": 0,
                    "ordered_block_ids_sha256": universe_sha,
                    "status": "exact_cover",
                },
                "source_binding_sha256": source_binding_sha,
            }
        )
    fragment = build_scoring_handoff_fragment(
        workflow_run_id=WORKFLOW_RUN_ID,
        translation_component_run_id=COMPONENT_RUN_ID,
        translation_component_attempt_id=ATTEMPT_ID,
        reserved_evaluation_component_run_id="ev_fixture_component_v1",
        artifact_ref="art_scoring_handoff_fragment",
        source_binding=source_binding,
        translation_inputs=translation_inputs,
        glossary_binding=_binding(
            "art_glossary",
            "glossary",
            "D2LGlossaryV1",
            glossary_path.read_bytes().decode("utf-8"),
        ),
        context_memory_binding=None,
        selected_chapter_ids=CHAPTER_IDS,
        admitted_universe={
            "ordered_block_ids_sha256": universe_sha,
            "block_count": 1,
            "status": "exact_cover",
        },
        producer_lineage={
            "git_commit": GIT_COMMIT,
            "pipeline_version": "translation_component_v1",
            "config_sha256": CONFIG_SHA,
            "code_sha256": CODE_SHA,
        },
        created_at=_ts(1),
    )
    fragment_path = root / "scoring_handoff_fragment.json"
    write_json(fragment_path, fragment)

    event_path = root / "events.jsonl"
    writer = D2LTranslationComponentEventWriter(
        event_path,
        manifest=manifest,
        component_attempt_id=ATTEMPT_ID,
    )
    writer.emit(
        "run_start",
        stage_id=None,
        agent="d2l_workflow_runner",
        payload={
            "manifest_ref": initial_manifest_binding["manifest_ref"],
            "manifest_sha256": initial_manifest_binding["manifest_sha256"],
            "selected_chapter_ids": CHAPTER_IDS,
        },
        ts=_ts(1),
    )
    artifact_rows: list[dict[str, Any]] = []
    stage_rows = build_stage_plan()
    for index, stage in enumerate(stage_rows, start=2):
        stage_id = stage["stage_id"]
        unit = stage["progress"]["unit"]
        writer.emit(
            "stage_start",
            stage_id=stage_id,
            agent=stage_id,
            payload={"progress": _progress(0, 1, unit), "current_work_id": f"work_{stage_id}"},
            ts=_ts(index),
        )
        if stage_id == "b1_candidate_discovery":
            writer.emit(
                "work_started",
                stage_id=stage_id,
                agent=stage_id,
                payload={
                    "work_kind": "window",
                    "work_id": "window_001",
                    "progress": _progress(0, 1, "windows"),
                },
                ts=_ts(index + 1),
            )
            writer.emit(
                "request_sent",
                stage_id=stage_id,
                agent=stage_id,
                payload={
                    "logical_request_id": "req_fixture_001",
                    "physical_attempt_index": 1,
                    "work_kind": "window",
                    "work_id": "window_001",
                    "provider_id": "fixture_provider",
                    "model_id": "fixture_model",
                    "source_id": "fixture_source",
                    "masked_quota_bucket": "fixture-bucket",
                },
                ts=_ts(index + 2),
            )
            writer.emit(
                "response_received",
                stage_id=stage_id,
                agent=stage_id,
                payload={
                    "usage": {
                        "logical_request_id": "req_fixture_001",
                        "physical_attempt_index": 1,
                        "provider_id": "fixture_provider",
                        "model_id": "fixture_model",
                        "source_id": "fixture_source",
                        "masked_quota_bucket": "fixture-bucket",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 15,
                        "latency_ms": 10,
                        "finish_reason": "stop",
                        "cost_usd": None,
                        "currency": "USD",
                        "cost_status": "unknown",
                        "cache_status": "miss",
                        "cache_mechanism": "none",
                    }
                },
                ts=_ts(index + 3),
            )
            writer.emit(
                "validation_passed",
                stage_id=stage_id,
                agent="d2l_candidate_validator",
                payload={
                    "validator_id": "d2l_candidate_validator_v1",
                    "subject_ref": "window_001",
                    "reason_codes": ["schema_valid"],
                    "retryable": False,
                },
                ts=_ts(index + 4),
            )
        if stage_id in {"translator", "glossary_seal", "scoring_handoff_fragment"}:
            artifact_specs = {
                "translator": [
                    ("art_translation_s0", "translation_artifact", "TranslationArtifactV1", "artifacts/translation_s0.json", s0_path),
                    ("art_translation_s1", "translation_artifact", "TranslationArtifactV1", "artifacts/translation_s1.json", s1_path),
                ],
                "glossary_seal": [
                    ("art_glossary", "glossary", "D2LGlossaryV1", "artifacts/glossary.json", glossary_path),
                ],
                "scoring_handoff_fragment": [
                    ("art_scoring_handoff_fragment", "scoring_handoff_fragment", SCORING_FRAGMENT_SCHEMA, "scoring_handoff_fragment.json", fragment_path),
                ],
            }
            for ref, kind, schema, relative_path, path in artifact_specs[stage_id]:
                event_id = writer.next_event_id
                row = _artifact_row(
                    manifest,
                    ref=ref,
                    kind=kind,
                    schema=schema,
                    stage_id=stage_id,
                    relative_path=relative_path,
                    created_event_id=event_id,
                    sha256_value=file_sha256(path),
                )
                artifact_rows.append(row)
                write_json(
                    root / "artifact_index.json",
                    {
                        "schema": "d2l_translation_artifact_index_v1",
                        "workflow_run_id": WORKFLOW_RUN_ID,
                        "flow_kind": "terminology_translation",
                        "component_id": COMPONENT_ID,
                        "component_run_id": COMPONENT_RUN_ID,
                        "component_attempt_id": ATTEMPT_ID,
                        "artifacts": artifact_rows,
                    },
                )
                writer.emit(
                    "artifact_created",
                    stage_id=stage_id,
                    agent=stage_id,
                    payload={
                        "artifact_ref": ref,
                        "artifact_kind": kind,
                        "schema_version": schema,
                        "sha256": row["sha256"],
                        "sha256_kind": "physical",
                        "parent_artifact_refs": [],
                    },
                    ts=_ts(index + 5),
                )
        writer.emit(
            "stage_done",
            stage_id=stage_id,
            agent=stage_id,
            payload={
                "outcome": "succeeded",
                "reason_code": "complete",
                "progress": _progress(1, 1, unit),
            },
            ts=_ts(index + 6),
        )
    writer.emit(
        "cost_snapshot",
        stage_id=None,
        agent="d2l_workflow_runner",
        payload={
            "scope": "component",
            "logical_request_count": 1,
            "physical_attempt_count": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 15,
            "cost_usd": None,
            "currency": "USD",
            "cost_status": "unknown",
            "cache_counters": {"hit": 0, "miss": 1},
        },
        ts=_ts(99),
    )
    index_path = root / "artifact_index.json"
    fragment_hash = file_sha256(fragment_path)
    writer.emit(
        "run_done",
        stage_id=None,
        agent="d2l_workflow_runner",
        payload={
            "artifact_index_ref": "artifact_index.json",
            "artifact_index_sha256": file_sha256(index_path),
            "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
            "scoring_handoff_fragment_sha256": fragment_hash,
            "outcome": "succeeded",
        },
        ts=_ts(100),
    )

    final_stages = []
    for stage in stage_rows:
        final = dict(stage)
        final["status"] = "succeeded"
        final["started_at"] = _ts(2)
        final["ended_at"] = _ts(100)
        final["progress"] = {"completed": 1, "total": 1, "unit": stage["progress"]["unit"]}
        final["current_work_id"] = None
        final["artifact_refs"] = [
            row["artifact_ref"] for row in artifact_rows if row["producer_stage_id"] == stage["stage_id"]
        ]
        final_stages.append(final)
    final_manifest = build_component_manifest(
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=ATTEMPT_ID,
        pipeline_id="d2l_terminology",
        pipeline_version="translation_component_v1",
        source_binding=source_binding,
        config_sha256=CONFIG_SHA,
        code_revision=GIT_COMMIT,
        selected_chapter_ids=CHAPTER_IDS,
        started_at=_ts(0),
        updated_at=_ts(100),
        status="succeeded",
        stages=final_stages,
        scoring_handoff_fragment_ref="scoring_handoff_fragment.json",
    )
    write_component_manifest_snapshot(root, final_manifest)
    result = validate_translation_component_package(root)
    write_json(root / "validation.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a 0-API D2L component fixture")
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    result = build_fixture(Path(args.output_root))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
