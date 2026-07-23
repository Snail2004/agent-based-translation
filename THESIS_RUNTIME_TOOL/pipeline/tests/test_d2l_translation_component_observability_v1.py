from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    STAGE_RECEIPT_SCHEMA,
    build_stage_receipt,
)
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    build_scoring_handoff_fragment,
    build_stage_plan,
    canonical_sha256,
    file_sha256,
    validate_translation_component_package,
)
from pipeline.prepass.d2l_terminology_memory_delta_v1 import (
    COMMIT_PACKAGE_SCHEMA,
    COMMIT_RECEIPT_SCHEMA,
    MEMORY_DELTA_BATCH_SCHEMA,
    SEALED_GLOSSARY_SCHEMA,
    commit_glossary_draft,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    D2LTranslationComponentRunner,
    RUNNER_SCHEMA,
)
from pipeline.translate.d2l_translation_quality_observation_v1 import (
    SCHEMA_VERSION as QUALITY_SCHEMA,
    build_quality_observation,
)


WORKFLOW_ID = "wf_observability_test_v1"
COMPONENT_ID = "tr_observability_test_v1"
CHAPTERS = ["d2l_multilayer_perceptrons"]
CONFIG_SHA = "2" * 64
PROFILE_SHA = "3" * 64
CODE_COMMIT = "4" * 40


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _binding(ref: str, kind: str, schema: str, digest: str) -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": digest,
        "sha256_kind": "physical",
    }


def _source_binding() -> dict[str, object]:
    values = {
        "document": ("source_document", "document_v1"),
        "structure_manifest": ("structure_manifest", "structure_manifest_v1"),
        "asset_manifest": ("asset_manifest", "asset_manifest_v1"),
        "admitted_projection": ("admitted_projection", "admitted_projection_v1"),
        "normalization_receipt": ("normalization_receipt", "normalization_receipt_v1"),
        "package_seal": ("source_package_seal", "source_package_seal_v1"),
    }
    return {
        "schema": "canonical_source_binding_v1",
        **{
            key: _binding(
                f"src_{key}", kind, schema, sha256(key.encode("utf-8")).hexdigest().upper()
            )
            for key, (kind, schema) in values.items()
        },
    }


def _entry(entry_id: str, source: str, target: str) -> dict[str, object]:
    candidate_id = f"candidate_{entry_id}"
    return {
        "entry_id": entry_id,
        "canonical_source": source,
        "canonical_target_vi": target,
        "alternative_targets": [],
        "surfaces": [source],
        "chapter_id": CHAPTERS[0],
        "status": "ready_draft",
        "directive": "translate",
        "canonical_applicability": None,
        "evidence_block_ids": [f"block_{entry_id}"],
        "evidence_complete": True,
        "source_member_candidate_ids": [candidate_id],
        "decision_rationale": "Stable technical term.",
        "pending_target_proposals": [],
        "rejected_target_proposals": [],
        "resolution": {
            "authority_kind": "deterministic_single_target",
            "authority_sha256": "A" * 64,
            "packet_id": None,
            "auditor_rationale": None,
            "auditor_cited_evidence_block_ids": [],
            "pending_reason": None,
        },
        "source_lineage": {
            "authority_kind": "source_b2_singleton",
            "authority_hash": "B" * 64,
            "source_index_sha256": "C" * 64,
            "source_member_candidate_ids": [candidate_id],
        },
    }


def _draft(entries: list[dict[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {
        "draft_version": "d2l_b2_glossary_draft_v2",
        "chapter_ids": CHAPTERS,
        "source_index_sha256": "C" * 64,
        "source_multi_target_draft_sha256": "D" * 64,
        "source_multi_target_plan_sha256": "E" * 64,
        "source_run_manifest_sha256": "F" * 64,
        "source_stage2_plan_sha256": "1" * 64,
        "ready_entries": entries,
        "pending_entries": [],
        "production_published": False,
        "counts": {
            "ready_entries": len(entries),
            "pending_entries": 0,
            "admitted_exact_cover": len(entries),
            "deterministic_single_target_entries": len(entries),
            "multi_target_audited_entries": 0,
            "current_admitted_entries": len(entries),
        },
    }
    value["draft_sha256"] = canonical_sha256(value)
    return value


def _spec(
    ref: str,
    kind: str,
    schema: str,
    relative_path: str,
    *,
    parents: list[str] | None = None,
    count: int | None = None,
) -> dict[str, object]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "relative_path": relative_path,
        "parent_artifact_refs": parents or [],
        "metadata": {"record_count": count} if count is not None else {},
    }


def _usage_observations(stage_id: str, subject_ref: str) -> list[dict[str, object]]:
    request_id = f"req_{stage_id}_1"
    return [
        {
            "event": "request_sent",
            "agent": stage_id,
            "severity": "info",
            "ts": "2026-07-22T00:00:01Z",
            "payload": {
                "logical_request_id": request_id,
                "physical_attempt_index": 1,
                "work_kind": "synthetic_packet",
                "work_id": f"work_{stage_id}",
                "provider_id": "synthetic_0_api",
                "model_id": "synthetic_model",
                "source_id": "synthetic_source",
                "masked_quota_bucket": "not-applicable-***",
            },
        },
        {
            "event": "response_received",
            "agent": stage_id,
            "severity": "info",
            "ts": "2026-07-22T00:00:02Z",
            "payload": {
                "usage": {
                    "logical_request_id": request_id,
                    "physical_attempt_index": 1,
                    "provider_id": "synthetic_0_api",
                    "model_id": "synthetic_model",
                    "source_id": "synthetic_source",
                    "masked_quota_bucket": "not-applicable-***",
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 12,
                    "latency_ms": 1,
                    "finish_reason": "stop",
                    "cost_usd": None,
                    "currency": None,
                    "cost_status": "unknown",
                    "cache_status": "bypass",
                    "cache_mechanism": "none",
                }
            },
        },
        {
            "event": "validation_passed",
            "agent": stage_id,
            "severity": "info",
            "ts": "2026-07-22T00:00:03Z",
            "payload": {
                "validator_id": f"{stage_id}_validator_v1",
                "subject_ref": subject_ref,
                "reason_codes": ["local_validation_passed"],
                "retryable": False,
            },
        },
        {
            "event": "cost_snapshot",
            "agent": stage_id,
            "severity": "info",
            "ts": "2026-07-22T00:00:04Z",
            "payload": {
                "scope": f"stage:{stage_id}",
                "logical_request_count": 1,
                "physical_attempt_count": 1,
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 12,
                "cost_usd": None,
                "currency": None,
                "cost_status": "unknown",
                "cache_counters": {"bypass": 1},
            },
        },
    ]


def _receipt(root: Path, stage_id: str, subject_ref: str) -> tuple[str, dict[str, object]]:
    relative = f"artifacts/{stage_id}/stage_receipt.json"
    receipt = build_stage_receipt(
        workflow_run_id=WORKFLOW_ID,
        component_run_id=COMPONENT_ID,
        component_attempt_id=1,
        stage_id=stage_id,
        producer=stage_id,
        work_id=f"work_{stage_id}",
        observations=_usage_observations(stage_id, subject_ref),
    )
    _write(root / relative, receipt)
    spec = _spec(
        f"art_{stage_id}_receipt",
        "d2l_stage_event_receipt",
        STAGE_RECEIPT_SCHEMA,
        relative,
        parents=[subject_ref],
    )
    return relative, spec


def _prepare_artifacts(root: Path) -> tuple[dict[str, list[dict[str, object]]], dict[str, str]]:
    specs: dict[str, list[dict[str, object]]] = {stage_id: [] for stage_id in STAGE_IDS}
    receipt_refs: dict[str, str] = {}

    preflight = {"schema": "d2l_preflight_report_v1", "status": "passed"}
    _write(root / "artifacts/preflight/report.json", preflight)
    specs["preflight"].append(
        _spec("art_preflight", "preflight_report", "d2l_preflight_report_v1", "artifacts/preflight/report.json")
    )

    candidates = {
        "aggregate_version": "d2l_candidate_discovery_full_chapter_shopaikey_aggregate_v3",
        "candidates": [
            {"candidate_id": "candidate_term_gradient", "surface": "gradient"},
            {"candidate_id": "candidate_term_example", "surface": "example"},
        ],
    }
    candidates["aggregate_sha256"] = canonical_sha256(candidates)
    _write(root / "artifacts/b1/candidates.json", candidates)
    specs["b1_candidate_discovery"].append(
        _spec("art_b1_candidates", "candidate_discovery", candidates["aggregate_version"], "artifacts/b1/candidates.json", parents=["art_preflight"], count=2)
    )
    receipt_refs["b1_candidate_discovery"], receipt_spec = _receipt(
        root, "b1_candidate_discovery", "art_b1_candidates"
    )
    specs["b1_candidate_discovery"].append(receipt_spec)

    candidate_index = {
        "index_version": "d2l_candidate_index_v2",
        "candidates": candidates["candidates"],
        "candidate_count": 2,
    }
    candidate_index["candidate_index_sha256"] = canonical_sha256(candidate_index)
    _write(root / "artifacts/candidate_index.json", candidate_index)
    specs["candidate_index"].append(
        _spec("art_candidate_index", "candidate_index", "d2l_candidate_index_v2", "artifacts/candidate_index.json", parents=["art_b1_candidates"], count=2)
    )

    admission = {"schema": "d2l_b2_admission_result_v1", "admitted": 2, "rejected": 0}
    _write(root / "artifacts/b2/admission.json", admission)
    specs["b2_admission_translation"].append(
        _spec("art_b2_admission", "b2_admission", admission["schema"], "artifacts/b2/admission.json", parents=["art_candidate_index"], count=2)
    )
    receipt_refs["b2_admission_translation"], receipt_spec = _receipt(
        root, "b2_admission_translation", "art_b2_admission"
    )
    specs["b2_admission_translation"].append(receipt_spec)

    audit_chain = [
        ("auditor_morphology", "art_morphology", "d2l_morphology_audit_v1", "art_b2_admission"),
        ("auditor_target_collision", "art_target_collision", "d2l_target_collision_audit_v1", "art_morphology"),
    ]
    for stage_id, ref, schema, parent in audit_chain:
        path = f"artifacts/{stage_id}/result.json"
        _write(root / path, {"schema": schema, "status": "passed"})
        specs[stage_id].append(_spec(ref, stage_id, schema, path, parents=[parent]))
        receipt_refs[stage_id], receipt_spec = _receipt(root, stage_id, ref)
        specs[stage_id].append(receipt_spec)

    glossary_draft = _draft(
        [
            _entry("term_gradient", "gradient", "gradient"),
            _entry("term_example", "example", "vi du"),
        ]
    )
    _write(root / "artifacts/multi_target/glossary_draft.json", glossary_draft)
    specs["auditor_multi_target"].append(
        _spec("art_glossary_draft", "glossary_draft", "d2l_b2_glossary_draft_v2", "artifacts/multi_target/glossary_draft.json", parents=["art_target_collision"], count=2)
    )
    receipt_refs["auditor_multi_target"], receipt_spec = _receipt(
        root, "auditor_multi_target", "art_glossary_draft"
    )
    specs["auditor_multi_target"].append(receipt_spec)

    sealed = commit_glossary_draft(
        draft=glossary_draft,
        output_root=root / "artifacts/glossary_seal",
        workflow_run_id=WORKFLOW_ID,
        component_run_id=COMPONENT_ID,
        component_attempt_id=1,
        stage_id="glossary_seal",
        source_refs=["art_glossary_draft"],
        created_at="2026-07-22T00:00:05Z",
    )
    for ref, kind, schema, filename in (
        ("art_glossary", "sealed_glossary", SEALED_GLOSSARY_SCHEMA, "sealed_glossary.json"),
        ("art_memory_delta", "terminology_memory_delta", MEMORY_DELTA_BATCH_SCHEMA, "memory_delta_v1.json"),
        ("art_glossary_commit_receipt", "glossary_commit_receipt", COMMIT_RECEIPT_SCHEMA, "commit_receipt.json"),
        ("art_glossary_commit_package", "glossary_commit_package", COMMIT_PACKAGE_SCHEMA, "artifact_manifest.json"),
    ):
        specs["glossary_seal"].append(
            _spec(ref, kind, schema, f"artifacts/glossary_seal/{filename}", parents=["art_glossary_draft"], count=2 if ref == "art_glossary" else None)
        )
    assert sealed["memory_delta_batch"]["counts"]["added"] == 2

    translations = {
        "s0": [
            {"block_id": "b001", "text": "Gradient descent."},
            {"block_id": "b002", "text": "An example."},
        ],
        "s1": [
            {"block_id": "b001", "text": "Ha gradient."},
            {"block_id": "b002", "text": "Mot vi du."},
        ],
    }
    for arm_id, rows in translations.items():
        path = root / f"artifacts/translator/{arm_id}.json"
        _write(path, {"schema_version": "TranslationArtifactV1", "arm_id": arm_id, "rows": rows})
        specs["translator"].append(
            _spec(f"art_translation_{arm_id}", "translation_artifact", "TranslationArtifactV1", f"artifacts/translator/{arm_id}.json", parents=["art_glossary"], count=2)
        )
    receipt_refs["translator"], receipt_spec = _receipt(root, "translator", "art_translation_s1")
    specs["translator"].append(receipt_spec)

    quality = build_quality_observation(
        audited_block_ids=["b001", "b002"],
        findings=[
            {
                "block_id": "b002",
                "issue_type": "style_or_fluency_advisory",
                "severity": "advisory",
                "source_evidence": "An example.",
                "target_evidence": "Mot vi du.",
                "reason": "The translation is understandable but terse.",
            }
        ],
        source_translation_artifact_refs=["art_translation_s0", "art_translation_s1"],
    )
    _write(root / "artifacts/quality/report.json", quality)
    specs["translation_quality_audit"].append(
        _spec("art_translation_quality", "translation_quality_observation", QUALITY_SCHEMA, "artifacts/quality/report.json", parents=["art_translation_s0", "art_translation_s1"], count=2)
    )
    receipt_refs["translation_quality_audit"], receipt_spec = _receipt(
        root, "translation_quality_audit", "art_translation_quality"
    )
    specs["translation_quality_audit"].append(receipt_spec)

    source = _source_binding()
    universe_sha = canonical_sha256(["b001", "b002"])
    inputs = []
    for arm_id in ("s0", "s1"):
        path = root / f"artifacts/translator/{arm_id}.json"
        inputs.append(
            {
                "arm_id": arm_id,
                "artifact": _binding(f"art_translation_{arm_id}", "translation_artifact", "TranslationArtifactV1", file_sha256(path)),
                "producer_component_run_id": COMPONENT_ID,
                "producer_component_attempt_id": 1,
                "profile_id": f"d2l_{arm_id}_v1",
                "profile_sha256": PROFILE_SHA,
                "config_sha256": CONFIG_SHA,
                "selected_chapter_ids": CHAPTERS,
                "coverage": {
                    "admitted_block_count": 2,
                    "translated_block_count": 2,
                    "preserved_block_count": 0,
                    "missing_block_count": 0,
                    "failed_block_count": 0,
                    "ordered_block_ids_sha256": universe_sha,
                    "status": "exact_cover",
                },
                "source_binding_sha256": canonical_sha256(source),
            }
        )
    fragment = build_scoring_handoff_fragment(
        workflow_run_id=WORKFLOW_ID,
        translation_component_run_id=COMPONENT_ID,
        translation_component_attempt_id=1,
        reserved_evaluation_component_run_id="ev_observability_test_v1",
        artifact_ref="art_scoring_handoff_fragment",
        source_binding=source,
        translation_inputs=inputs,
        glossary_binding=_binding("art_glossary", "sealed_glossary", SEALED_GLOSSARY_SCHEMA, file_sha256(root / "artifacts/glossary_seal/sealed_glossary.json")),
        context_memory_binding=None,
        selected_chapter_ids=CHAPTERS,
        admitted_universe={"ordered_block_ids_sha256": universe_sha, "block_count": 2, "status": "exact_cover"},
        producer_lineage={"git_commit": CODE_COMMIT, "pipeline_version": "d2l_translation_component_runner_v1_1", "config_sha256": CONFIG_SHA, "code_sha256": "5" * 64},
        created_at="2026-07-22T00:00:06Z",
    )
    _write(root / "scoring_handoff_fragment.json", fragment)
    specs["scoring_handoff_fragment"].append(
        _spec("art_scoring_handoff_fragment", "scoring_handoff_fragment", "scoring_handoff_fragment_v1", "scoring_handoff_fragment.json", parents=["art_glossary", "art_translation_s0", "art_translation_s1", "art_translation_quality"])
    )
    return specs, receipt_refs


def _plan(specs: dict[str, list[dict[str, object]]], receipts: dict[str, str]) -> dict[str, object]:
    units = {row["stage_id"]: row["progress"]["unit"] for row in build_stage_plan()}
    return {
        "schema": RUNNER_SCHEMA,
        "workflow_run_id": WORKFLOW_ID,
        "component_run_id": COMPONENT_ID,
        "pipeline_id": "d2l_terminology",
        "pipeline_version": "d2l_translation_component_runner_v1_1",
        "source_binding": _source_binding(),
        "config_sha256": CONFIG_SHA,
        "code_revision": CODE_COMMIT,
        "selected_chapter_ids": CHAPTERS,
        "stages": [
            {
                "stage_id": stage_id,
                "producer": stage_id,
                "command": [sys.executable, "-c", "pass"],
                "cwd": None,
                "artifact_specs": specs[stage_id],
                "total": 2 if stage_id not in {"preflight", "scoring_handoff_fragment"} else 1,
                "unit": units[stage_id],
                "work_id": f"work_{stage_id}",
                "mode": "execute",
                "timeout_seconds": 30,
                "receipt_ref": receipts.get(stage_id),
            }
            for stage_id in STAGE_IDS
        ],
        "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
    }


def test_full_component_observability_includes_memory_and_quality_audit(tmp_path: Path) -> None:
    root = tmp_path / "component"
    specs, receipts = _prepare_artifacts(root)
    result = D2LTranslationComponentRunner(_plan(specs, receipts), root).run()

    assert result["terminal_event"] == "run_done"
    package = validate_translation_component_package(root)
    assert package["component_attempt_id"] == 1
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert [row["stage_id"] for row in manifest["stages"]] == list(STAGE_IDS)
    assert all(row["status"] == "succeeded" for row in manifest["stages"])

    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert sum(row["event"] == "request_sent" for row in events) == 7
    assert sum(row["event"] == "response_received" for row in events) == 7
    assert sum(row["event"] == "cost_snapshot" for row in events) == 7
    assert all(row["stage_id"] is None for row in events if row["event"] == "cost_snapshot")

    delta = json.loads((root / "artifacts/glossary_seal/memory_delta_v1.json").read_text())
    assert delta["counts"] == {"added": 2, "reinforced": 0, "revised": 0, "total": 2}
    assert {row["lifecycle"] for row in delta["deltas"]} == {"committed"}
    quality = json.loads((root / "artifacts/quality/report.json").read_text())
    assert quality["counts"] == {"pass": 1, "issue": 1, "findings": 1, "total": 2}
    issue = next(row for row in quality["blocks"] if row["quality_status"] == "issue")
    assert issue["continue_to_scoring"] is True

    index = json.loads((root / "artifact_index.json").read_text())
    kinds = {row["artifact_kind"] for row in index["artifacts"]}
    assert "terminology_memory_delta" in kinds
    assert "translation_quality_observation" in kinds
    assert not (root / "workflow_manifest.json").exists()
    assert not (root / "workflow_events.jsonl").exists()
