from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.b2_context_v1 import load_real_b1_run_input_v1
from pipeline.literary.chapter_prefix_prior_v1 import (
    build_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
import pipeline.literary.identity_reconciled_b1_snapshot_v1 as subject
from pipeline.literary.literary_context_pipeline_v1 import tree_hash_v1


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _hashed(body: dict, field: str) -> dict:
    return {**body, field: canonical_hash(body)}


def _claim(value: str, block_id: str) -> dict:
    return {
        "value": value,
        "basis": "explicit",
        "support_block_ids": [block_id],
        "address_validation_state": "valid",
        "semantic_status": "unreviewed",
    }


def _audited_inventory() -> dict:
    entity = {
        "candidate_id": "local_vale",
        "canonical_surface": "Mr. Vale",
        "surface_status": "located",
        "canonical_name_class": "title_plus_name",
        "alternative_names": [],
        "name_locations": [
            {
                "surface": "Mr. Vale",
                "name_class": "title_plus_name",
                "source_block_ids": ["book_ch01_b001"],
            }
        ],
        "source_block_ids": ["book_ch01_b001"],
        "referent_kind_claim": _claim("person", "book_ch01_b001"),
        "referential_gender_claim": _claim("masculine", "book_ch01_b001"),
        "identity_summary_draft": "A named visitor associated with the house.",
        "identity_summary_status": "unreviewed",
        "publication_state": "clean_provisional",
        "audit_reasons": [],
        "conflict_status": "no_detected_identity_conflict",
    }
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": "book_ch01",
        "source_inventory_hash": "inventory_source",
        "request_fingerprint": "request",
        "conflict_manifest_hash": "manifest",
        "entity_candidates": [entity],
        "pending_entity_candidates": [],
        "closed_entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "quarantined_surfaces": [],
        "global_surface_bindings": [],
        "component_decisions": [],
        "conflict_summary": {},
        "source_validation_report": {},
        "production_publish_performed": False,
    }
    return {
        **body,
        "conflict_audited_inventory_hash": canonical_hash(body),
    }


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    document = {
        "document_id": "book",
        "chapters": [
            {
                "chapter_id": "book_ch01",
                "blocks": [
                    {
                        "block_id": "book_ch01_b001",
                        "block_type": "paragraph",
                        "source_text": "Mr. Vale entered the house.",
                    }
                ],
            }
        ],
    }
    document_path = tmp_path / "document.json"
    _write_json(document_path, document)
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_audited_inventory(),
        coverage_through_chapter_id="book_ch01",
    )
    source = tmp_path / "source"
    report_path = source / "artifacts" / "chapters" / "ch001" / "chapter_report.json"
    _write_json(report_path.parent / "final_prefix.json", prefix)
    report_body = {
        "schema_version": "literary_unified_chapter_report_v1",
        "chapter_id": "book_ch01",
        "chapter_ordinal": 1,
        "coverage_through_chapter_id": "book_ch01",
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "active_context_card_count": len(prefix["b0_context_cards"]),
        "candidate_only_card_count": len(prefix["candidate_only_context_cards"]),
        "glossary_context_card_count": len(prefix["glossary_context_cards"]),
        "b2_enabled": False,
        "b2_ready": False,
        "production_publish_performed": False,
    }
    report = _hashed(report_body, "report_hash")
    _write_json(report_path, report)
    plan_body = {
        "document_path": str(document_path.resolve()),
        "document_sha256": file_sha256(document_path),
        "ordered_chapter_ids": ["book_ch01"],
    }
    plan = _hashed(plan_body, "plan_hash")
    _write_json(source / "run_plan.json", plan)
    summary_body = {
        "schema_version": "literary_pipeline_run_summary_v1",
        "plan_hash": plan["plan_hash"],
        "state_hash": "old_state",
        "state_generation": 2,
        "status": "complete",
        "completed_chapter_ids": ["book_ch01"],
        "sealed_chapter_ids": ["book_ch01"],
        "run_api_call_count": 1,
        "semantic_pending_count": 1,
        "cumulative_hashes": {"prefix_hash": prefix["prefix_bundle_hash"]},
        "chapter_reports": [
            {
                "chapter_id": "book_ch01",
                "path": report_path.relative_to(source).as_posix(),
                "report_hash": report["report_hash"],
                "prefix_bundle_hash": prefix["prefix_bundle_hash"],
            }
        ],
        "b2": {"enabled": False, "ready": False},
        "production_publish_performed": False,
    }
    _write_json(source / "run_summary.json", _hashed(summary_body, "summary_hash"))
    _write_json(
        source / "stages" / "source" / "live" / "run_envelope_001.json",
        {"git_head": "old_head"},
    )

    lineage = prefix["state_lineage_id"]
    prepare = tmp_path / "prepare"
    identity_index = {"identity_index_hash": "index_hash"}
    bridge = {"state_lineage_id": lineage, "bridge_hash": "bridge_hash"}
    prepare_body = {
        "source_root": str(source.resolve()),
        "identity_index_hash": "index_hash",
        "bridge_hash": "bridge_hash",
        "production_publish_performed": False,
    }
    prepare_report = _hashed(prepare_body, "report_hash")
    _write_json(prepare / "identity_index.json", identity_index)
    _write_json(prepare / "semantic_identity_occurrence_bridge.json", bridge)
    _write_json(prepare / "prepare_report.json", prepare_report)

    shared_root = tmp_path / "shared"
    provider_body = b'{"choices":[],"usage":{"total_tokens":5}}'
    provider_path = shared_root / "provider.tmp"
    provider_path.parent.mkdir(parents=True)
    provider_path.write_bytes(provider_body)
    provider_sha = file_sha256(provider_path)
    final_provider_path = shared_root / "artifacts" / provider_sha[:2] / provider_sha
    final_provider_path.parent.mkdir(parents=True)
    provider_path.replace(final_provider_path)

    recovery = tmp_path / "recovery"
    decision = {
        "decision_hash": "decision_hash",
        "status": "resolved_distinct",
    }
    review_body = {
        "state_lineage_id": lineage,
        "review_items": [{"review_item_id": "r1", "lifecycle_state": "closed"}],
    }
    review = _hashed(review_body, "review_ledger_hash")
    identity_body = {
        "state_lineage_id": lineage,
        "decision_history": [{"decision_hash": "decision_hash"}],
        "component_states": [],
        "production_publish_performed": False,
    }
    identity = _hashed(identity_body, "identity_ledger_hash")
    case_body = {"state_lineage_id": lineage, "cases": []}
    cases = _hashed(case_body, "review_case_ledger_hash")
    recovery_body = {
        "source_prepare_report_hash": prepare_report["report_hash"],
        "provider_called": False,
        "production_publish_performed": False,
        "mandatory_stop_required": False,
        "decision_hash": "decision_hash",
        "decision_status": "resolved_distinct",
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "review_ledger_hash": review["review_ledger_hash"],
        "identity_ledger_hash": identity["identity_ledger_hash"],
        "review_case_ledger_hash": cases["review_case_ledger_hash"],
        "source_failed_attempt_root": str(shared_root.resolve()),
        "source_provider_artifact_sha256": provider_sha,
    }
    _write_json(recovery / "recovery_report.json", _hashed(recovery_body, "report_hash"))
    _write_json(recovery / "decision.json", decision)
    _write_json(recovery / "prefix_post_identity.json", prefix)
    _write_json(recovery / "review_ledger_post_identity.json", review)
    _write_json(recovery / "identity_ledger.json", identity)
    _write_json(recovery / "review_case_ledger_post_identity.json", cases)
    _write_json(recovery / "surface_scope_normalizations.json", {"rows": []})

    monkeypatch.setattr(subject, "verify_incremental_identity_index_v1", dict)
    monkeypatch.setattr(
        subject,
        "verify_incremental_identity_decision_v1",
        lambda value, index: dict(value),
    )
    monkeypatch.setattr(subject, "verify_chapter_cycle_review_ledger_v1", dict)
    monkeypatch.setattr(subject, "verify_incremental_identity_ledger_v1", dict)
    monkeypatch.setattr(subject, "verify_review_case_ledger_v1", dict)
    monkeypatch.setattr(subject, "verify_semantic_identity_occurrence_bridge_v1", dict)
    return source, prepare, recovery


def test_materializer_preserves_source_and_emits_current_head_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, prepare, recovery = _fixture(tmp_path, monkeypatch)
    before = tree_hash_v1(source)
    output = tmp_path / "derived"

    result = subject.materialize_identity_reconciled_b1_snapshot_v1(
        source_run_root=source,
        prepare_root=prepare,
        recovery_root=recovery,
        output_root=output,
        current_git_head="new_head",
    )

    assert result["decision_status"] == "resolved_distinct"
    assert result["provider_calls_performed"] == 0
    assert result["certification_eligible"] is True
    assert tree_hash_v1(source) == before
    loaded = load_real_b1_run_input_v1(output, current_git_head="new_head")
    assert loaded["certification_eligible"] is True
    summary = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["run_api_call_count"] == 2
    assert summary["semantic_pending_count"] == 0
    assert summary["state_generation"] == 3
    assert (output / "identity_reconciliation" / "provider_response.json").is_file()


def test_materializer_rejects_foreign_prepare_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, prepare, recovery = _fixture(tmp_path, monkeypatch)
    report_path = prepare / "prepare_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("report_hash")
    report["source_root"] = str((tmp_path / "foreign").resolve())
    _write_json(report_path.with_suffix(".new"), _hashed(report, "report_hash"))
    report_path.unlink()
    report_path.with_suffix(".new").replace(report_path)

    with pytest.raises(
        subject.IdentityReconciledSnapshotError,
        match="another B1 run",
    ):
        subject.materialize_identity_reconciled_b1_snapshot_v1(
            source_run_root=source,
            prepare_root=prepare,
            recovery_root=recovery,
            output_root=tmp_path / "derived",
            current_git_head="new_head",
        )
