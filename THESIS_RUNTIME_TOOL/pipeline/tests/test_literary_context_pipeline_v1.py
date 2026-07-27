from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.chapter_prefix_prior_v1 import (
    build_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.context_pipeline_profile_v1 import (
    LiteraryContextPipelineProfileError,
    load_context_pipeline_profile_v1,
)
from pipeline.literary.literary_context_pipeline_v1 import (
    LiteraryContextPipelineError,
    _run_b2_chapter,
    _run_recovery_chapter,
    b2_source_tree_hash_v1,
    build_context_chapter_checkpoint_v1,
    generate_chapter_runtime_profiles_v1,
    replay_context_pipeline_artifacts_v1,
    snapshot_completed_b1_prefix_v1,
    tree_hash_v1,
    verify_context_chapter_checkpoint_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_context_pipeline_openai_gpt54_v1.json"
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _claim(value: str, block_id: str) -> dict:
    return {
        "value": value,
        "basis": "explicit",
        "support_block_ids": [block_id],
        "address_validation_state": "valid",
        "semantic_status": "unreviewed",
    }


def _chapter(chapter_id: str) -> dict:
    return {
        "chapter_id": chapter_id,
        "blocks": [
            {
                "block_id": f"{chapter_id}_h001",
                "order_index": 0,
                "block_type": "heading",
                "clean_text": "Chapter",
            },
            {
                "block_id": f"{chapter_id}_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr. Vale greeted Robin at North House.",
            },
            {
                "block_id": f"{chapter_id}_b002",
                "order_index": 2,
                "block_type": "dialogue",
                "clean_text": '"Robin, come here," said Mr. Vale.',
            },
        ],
    }


def _audited_inventory(chapter_id: str) -> dict:
    block_id = f"{chapter_id}_b001"
    entity = {
        "candidate_id": f"local_vale_{chapter_id}",
        "canonical_surface": "Mr. Vale",
        "surface_status": "located",
        "canonical_name_class": "title_plus_name",
        "alternative_names": [],
        "name_locations": [
            {
                "surface": "Mr. Vale",
                "name_class": "title_plus_name",
                "source_block_ids": [block_id],
            }
        ],
        "source_block_ids": [block_id],
        "referent_kind_claim": _claim("person", block_id),
        "referential_gender_claim": _claim("masculine", block_id),
        "identity_summary_draft": "A named visitor associated with the house.",
        "identity_summary_status": "unreviewed",
        "publication_state": "clean_provisional",
        "audit_reasons": [],
        "conflict_status": "no_detected_identity_conflict",
    }
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": chapter_id,
        "source_inventory_hash": f"inventory_{chapter_id}",
        "request_fingerprint": f"request_{chapter_id}",
        "conflict_manifest_hash": f"manifest_{chapter_id}",
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


def _fake_b1_run(tmp_path: Path, *, source_head: str) -> Path:
    chapters = [_chapter("book_ch01"), _chapter("book_ch02")]
    document = {"document_id": "book", "chapters": chapters}
    document_path = tmp_path / "document.json"
    _write_json(document_path, document)
    root = tmp_path / "b1"
    report_rows = []
    for ordinal, chapter in enumerate(chapters, 1):
        prefix = build_chapter_prefix_prior_bundle_v1(
            document=document,
            audited_inventory=_audited_inventory(chapter["chapter_id"]),
            coverage_through_chapter_id=chapter["chapter_id"],
        )
        report_path = (
            root
            / "artifacts"
            / "chapters"
            / f"ch{ordinal:03d}"
            / "chapter_report.json"
        )
        _write_json(report_path.parent / "final_prefix.json", prefix)
        report_body = {
            "chapter_id": chapter["chapter_id"],
            "b2_enabled": False,
            "b2_ready": False,
            "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        }
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write_json(report_path, report)
        report_rows.append(
            {
                "chapter_id": chapter["chapter_id"],
                "path": report_path.relative_to(root).as_posix(),
                "report_hash": report["report_hash"],
            }
        )
    plan_body = {
        "document_path": str(document_path.resolve()),
        "document_sha256": file_sha256(document_path),
        "ordered_chapter_ids": ["book_ch01", "book_ch02"],
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    _write_json(root / "run_plan.json", plan)
    summary_body = {
        "status": "complete",
        "production_publish_performed": False,
        "b2": {"enabled": False},
        "plan_hash": plan["plan_hash"],
        "completed_chapter_ids": ["book_ch01", "book_ch02"],
        "chapter_reports": report_rows,
    }
    summary = {**summary_body, "summary_hash": canonical_hash(summary_body)}
    _write_json(root / "run_summary.json", summary)
    _write_json(
        root / "stages" / "b1" / "live" / "run_envelope_001.json",
        {"git_head": source_head},
    )
    return root


def _hashed(body: dict, field: str) -> dict:
    return {**body, field: canonical_hash(body)}


def _fake_b2_and_recovery(
    tmp_path: Path,
    *,
    b1_root: Path,
    chapter_id: str,
    source_head: str,
) -> tuple[Path, Path]:
    from pipeline.literary.b2_context_v1 import load_real_b1_run_input_v1

    loaded = load_real_b1_run_input_v1(
        b1_root, current_git_head=source_head
    )
    chapter = next(
        row for row in loaded["chapters"] if row["chapter_id"] == chapter_id
    )
    b2_root = tmp_path / f"b2_{chapter_id}"
    artifact = _hashed(
        {
            "chapter_id": chapter_id,
            "speaker_turns": [],
            "interaction_events": [],
            "review_requests": [],
            "production_publish_performed": False,
        },
        "artifact_hash",
    )
    _write_json(b2_root / "chapter_b2_artifact.json", artifact)
    b2_seal = _hashed(
        {
            "chapter_id": chapter_id,
            "source_run_root": str(b1_root.resolve()),
            "source_tree_hash": b2_source_tree_hash_v1(b1_root),
            "source_document_sha256": loaded["source_document_sha256"],
            "source_run_git_head": loaded["source_run_git_head"],
            "source_chapter_report_hash": chapter["chapter_report_hash"],
            "source_prefix_bundle_hash": chapter["prefix_bundle_hash"],
            "production_publish_performed": False,
        },
        "seal_hash",
    )
    _write_json(b2_root / "run_seal.json", b2_seal)
    b2_report = _hashed(
        {
            "status": "complete_exploratory_ch1_canary",
            "chapter_id": chapter_id,
            "chapter_artifact_hash": artifact["artifact_hash"],
            "calls_performed": 1,
            "visible_tokens": 10,
            "production_publish_performed": False,
        },
        "report_hash",
    )
    _write_json(b2_root / "live_report.json", b2_report)

    recovery_root = tmp_path / f"recovery_{chapter_id}"
    projection = _hashed(
        {
            "chapter_id": chapter_id,
            "interaction_events": [],
            "pending_registry_tickets": [],
            "pending_event_cases": [],
            "production_publish_performed": False,
        },
        "effective_projection_hash",
    )
    _write_json(
        recovery_root / "effective_b2_projection.json", projection
    )
    recovery_seal = _hashed(
        {
            "chapter_id": chapter_id,
            "source_b2_root": str(b2_root.resolve()),
            "source_tree_hash": tree_hash_v1(b2_root),
            "source_b2_artifact_hash": artifact["artifact_hash"],
            "production_publish_performed": False,
        },
        "seal_hash",
    )
    _write_json(recovery_root / "run_seal.json", recovery_seal)
    recovery_report = _hashed(
        {
            "status": "complete",
            "chapter_id": chapter_id,
            "source_b2_artifact_hash": artifact["artifact_hash"],
            "effective_projection_hash": projection[
                "effective_projection_hash"
            ],
            "provider_calls": 1,
            "visible_tokens": 10,
            "pending_registry_ticket_count": 0,
            "pending_event_case_count": 0,
            "production_publish_performed": False,
        },
        "report_hash",
    )
    _write_json(recovery_root / "live_report.json", recovery_report)
    return b2_root, recovery_root


def test_recommended_context_profile_is_closed_and_loadable() -> None:
    profile = load_context_pipeline_profile_v1(PROFILE_PATH)
    assert profile.profile_id == "literary_context_pipeline_openai_gpt54_v1"
    assert profile.role_bindings["b2_frame"] == "literary_b2_frame"
    assert profile.safety["provider_fallback_allowed"] is False
    assert profile.safety["production_publish_enabled"] is False


def test_profile_rejects_safety_weakening(tmp_path: Path) -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    payload["safety"]["provider_fallback_allowed"] = True
    broken = tmp_path / PROFILE_PATH.name
    for dependency in (
        "literary_pipeline_profile_openai_gpt54_samehead_v1.json",
        "literary_b2_phase_a_profile_v1.json",
        "literary_provider_profile_openai_gpt54_samehead_v1.json",
        "literary_structured_output_policy_v1.json",
    ):
        (tmp_path / dependency).write_bytes(
            (PROFILE_PATH.parent / dependency).read_bytes()
        )
    _write_json(broken, payload)
    with pytest.raises(
        LiteraryContextPipelineProfileError, match="safety contract"
    ):
        load_context_pipeline_profile_v1(broken)


def test_snapshot_stopped_prefix_and_generated_profiles_are_bounded(
    tmp_path: Path,
) -> None:
    source = _fake_b1_run(tmp_path, source_head="head")
    before = tree_hash_v1(source)
    snapshot = tmp_path / "snapshot"
    result = snapshot_completed_b1_prefix_v1(
        source_run_root=source,
        output_root=snapshot,
        chapter_count=1,
        current_git_head="head",
    )
    assert result["completed_chapter_ids"] == ["book_ch01"]
    assert tree_hash_v1(source) == before

    generated = generate_chapter_runtime_profiles_v1(
        output_root=tmp_path / "profiles",
        profile=load_context_pipeline_profile_v1(PROFILE_PATH),
        b1_snapshot_root=snapshot,
        chapter_id="book_ch01",
        current_git_head="head",
    )
    b2_profile = json.loads(
        Path(generated["b2_profile_path"]).read_text(encoding="utf-8")
    )
    assert generated["interaction_calls"] == 1
    assert b2_profile["limits"]["max_total_calls"] == 2
    assert (
        b2_profile["safety"]["prior_frame_candidate_carry_required"]
        is False
    )


def test_context_checkpoint_closes_exact_b1_b2_recovery_lineage(
    tmp_path: Path,
) -> None:
    b1_root = _fake_b1_run(tmp_path, source_head="head")
    b2_root, recovery_root = _fake_b2_and_recovery(
        tmp_path,
        b1_root=b1_root,
        chapter_id="book_ch01",
        source_head="head",
    )
    checkpoint = build_context_chapter_checkpoint_v1(
        plan_hash="plan",
        chapter_id="book_ch01",
        chapter_ordinal=1,
        b1_root=b1_root,
        b2_root=b2_root,
        recovery_root=recovery_root,
        current_git_head="head",
    )
    checkpoint_path = tmp_path / "context_checkpoint.json"
    _write_json(checkpoint_path, checkpoint)
    verified = verify_context_chapter_checkpoint_v1(
        checkpoint_path, current_git_head="head"
    )
    assert verified["authority_boundary"] == {
        "b1_pending_claims_are_effective": False,
        "raw_b2_is_translator_authority": False,
        "effective_projection_is_chapter_observation_authority": True,
        "relation_phase_inference_performed": False,
    }
    assert checkpoint["b1"]["tree_matches_b2_seal"] is True

    tampered = deepcopy(checkpoint)
    tampered["recovery"]["effective_projection_hash"] = "0" * 64
    _write_json(tmp_path / "tampered.json", tampered)
    with pytest.raises(LiteraryContextPipelineError, match="hash mismatch"):
        verify_context_chapter_checkpoint_v1(
            tmp_path / "tampered.json", current_git_head="head"
        )


def test_offline_replay_performs_zero_api_and_keeps_sources_immutable(
    tmp_path: Path,
) -> None:
    b1_root = _fake_b1_run(tmp_path, source_head="head")
    b2_root, recovery_root = _fake_b2_and_recovery(
        tmp_path,
        b1_root=b1_root,
        chapter_id="book_ch01",
        source_head="head",
    )
    before = {
        "b1": tree_hash_v1(b1_root),
        "b2": tree_hash_v1(b2_root),
        "recovery": tree_hash_v1(recovery_root),
    }
    summary = replay_context_pipeline_artifacts_v1(
        output_root=tmp_path / "replay",
        b1_root=b1_root,
        chapter_artifacts=[
            {
                "chapter_id": "book_ch01",
                "b2_root": str(b2_root),
                "recovery_root": str(recovery_root),
            }
        ],
        current_git_head="head",
    )
    assert summary["status"] == "complete"
    assert summary["api_calls_performed"] == 0
    assert summary["historical_source_tree_drift"] is False
    assert before == {
        "b1": tree_hash_v1(b1_root),
        "b2": tree_hash_v1(b2_root),
        "recovery": tree_hash_v1(recovery_root),
    }


def test_b2_resume_continues_incomplete_attempt_without_reprepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "b2" / "ch001" / "attempt_001"
    attempt.mkdir(parents=True)
    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "execute_b2_frame_live_v1",
        lambda **_: calls.append("frame"),
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "execute_b2_interactions_live_v1",
        lambda **_: calls.append("interactions"),
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "prepare_b2_ch1_canary_v1",
        lambda **_: pytest.fail("resume must not prepare a second B2 root"),
    )

    result = _run_b2_chapter(
        root=tmp_path,
        chapter_ordinal=1,
        snapshot_root=tmp_path / "snapshot",
        b2_profile_path=tmp_path / "profile.json",
        credential_root=tmp_path,
        frozen_db=tmp_path / "frozen.sqlite3",
        current_git_head="head",
        max_attempts=1,
    )

    assert result == attempt
    assert calls == ["frame", "interactions"]


def test_recovery_resume_creates_new_immutable_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = tmp_path / "b2_recovery" / "ch001" / "attempt_001"
    prior.mkdir(parents=True)
    observed: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        observed.update(kwargs)
        output = Path(str(kwargs["output_root"]))
        output.mkdir(parents=True)

    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "run_b2_recovery_live_v1",
        fake_run,
    )
    result = _run_recovery_chapter(
        root=tmp_path,
        chapter_ordinal=1,
        b2_root=tmp_path / "b2",
        recovery_profile_path=tmp_path / "profile.json",
        credential_root=tmp_path,
        frozen_db=tmp_path / "frozen.sqlite3",
        max_attempts=2,
    )

    assert result.name == "attempt_002"
    assert observed["resume_from_root"] == prior
