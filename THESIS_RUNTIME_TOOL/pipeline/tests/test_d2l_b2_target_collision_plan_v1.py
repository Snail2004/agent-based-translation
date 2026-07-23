from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationCaps,
    ConsolidationPlanError,
    _index_counts,
    _sha256_json,
    packetize_components,
)
from pipeline.prepass.d2l_b2_target_collision_plan_v1 import (
    INDEX_VERSION,
    PLAN_VERSION,
    _current_entry_from_audit,
    build_post_morphology_index,
    build_target_collision_plan,
)


def _candidate(
    candidate_id: str,
    source: str,
    targets: list[str],
    block_id: str,
    *,
    decision: str = "admit",
) -> dict:
    return {
        "candidate_id": candidate_id,
        "chapter_id": "chapter_alpha",
        "surfaces": [source],
        "decision": decision,
        "canonical_source": source if decision == "admit" else None,
        "target_proposals": [
            {"target_vi": target, "applicability": None}
            for target in targets
        ],
        "directive": "translate" if decision == "admit" else None,
        "evidence_block_ids": [block_id],
        "evidence_complete": decision == "admit",
        "decision_rationale": "Synthetic source-grounded rationale.",
        "lineage": {
            "packet_id": "packet",
            "manifest_sha256": "A" * 64,
            "source_request_sha256": "B" * 64,
            "validation_sha256": "C" * 64,
        },
    }


def _fixture() -> tuple[dict, dict, dict]:
    rows = [
        _candidate("cand_model", "model", ["mo hinh"], "b001"),
        _candidate("cand_models", "models", ["mo hinh"], "b002"),
        _candidate("cand_arch", "architecture", ["mo hinh"], "b003"),
        _candidate("cand_example", "example", ["vi du", "mau"], "b004"),
        _candidate("cand_tensor", "tensor", ["tensor"], "b005"),
        _candidate("cand_noise", "ordinary phrase", [], "b006", decision="reject"),
    ]
    source_index = {
        "index_version": "fixture_source",
        "chapter_ids": ["chapter_alpha"],
        "source_lineage": [],
        "decisions": rows,
        "source_blocks": [
            {"block_id": f"b00{number}", "text": f"Context {number}."}
            for number in range(1, 7)
        ],
    }
    source_index["counts"] = _index_counts(rows)
    source_index["index_sha256"] = _sha256_json(source_index)
    component = {
        "chapter_id": "chapter_alpha",
        "component_id": "morph_pair",
        "reason_codes": ["source_form_variant"],
        "members": [
            _member(rows[0]),
            _member(rows[1]),
        ],
        "edges": [
            {
                "left_candidate_id": "cand_model",
                "right_candidate_id": "cand_models",
                "signals": ["shared_target", "source_form_variant"],
            }
        ],
        "source_block_ids": ["b001", "b002"],
    }
    stage1_plan = {
        "plan_version": "fixture_stage1",
        "selection_scope": "stage1_strong_morphology_only",
        "selection_rule": "fixture",
        "production_publish_allowed": False,
        "source_index_sha256": source_index["index_sha256"],
        "source_full_plan_sha256": "D" * 64,
        "prior_audit_draft_sha256s": [],
        "components": [component],
        "queue_reopen_context": [],
        "resolved_reuse": [],
        "deferred_candidate_ids": [
            "cand_arch",
            "cand_example",
            "cand_tensor",
        ],
        "later_stage_frontier": {},
        "provisional_clean": [],
        "pending_admission": [],
        "rejected_ledger": [],
        "counts": {},
    }
    stage1_plan["plan_sha256"] = _sha256_json(stage1_plan)
    entry = {
        "draft_entry_id": "entry_model",
        "status": "audited_draft",
        "component_id": "morph_pair",
        "member_candidate_ids": ["cand_model", "cand_models"],
        "canonical_source": "model",
        "source_variants": ["model", "models"],
        "canonical_target_vi": "mo hinh",
        "alternative_targets": [],
        "directive": "translate",
        "evidence_block_ids": ["b001", "b002"],
        "auditor_cited_evidence_block_ids": ["b001", "b002"],
        "rationale": "The forms are one audited concept.",
    }
    stage1_draft = {
        "draft_version": "fixture_draft",
        "production_published": False,
        "source_index_sha256": source_index["index_sha256"],
        "source_plan_sha256": stage1_plan["plan_sha256"],
        "audited_entries": [entry],
        "provisional_clean": [],
        "pending_components": [],
        "pending_admission": [],
        "rejected_ledger": [],
        "counts": {},
    }
    stage1_draft["draft_sha256"] = _sha256_json(stage1_draft)
    return source_index, stage1_plan, stage1_draft


def _member(row: dict) -> dict:
    return {
        key: deepcopy(value)
        for key, value in row.items()
        if key
        in {
            "candidate_id",
            "canonical_source",
            "decision_rationale",
            "directive",
            "evidence_block_ids",
            "evidence_complete",
            "surfaces",
            "target_proposals",
        }
    }


def test_builds_atomic_post_morphology_entries_and_collision() -> None:
    source, stage1, draft = _fixture()
    current = build_post_morphology_index(
        source_index=source, stage1_plan=stage1, stage1_draft=draft
    )
    plan = build_target_collision_plan(current)

    assert current["index_version"] == INDEX_VERSION
    assert current["counts"]["source_admitted_candidates"] == 5
    assert current["counts"]["current_admitted_entries"] == 4
    assert current["counts"]["source_candidate_exact_cover"] == 5
    assert plan["plan_version"] == PLAN_VERSION
    assert plan["counts"]["target_collision_components"] == 1
    assert plan["counts"]["target_collision_entries"] == 2
    component = plan["components"][0]
    assert len(component["members"]) == 2
    morphology = next(
        row
        for row in current["decisions"]
        if row.get("source_member_candidate_ids")
        == ["cand_model", "cand_models"]
    )
    assert morphology["candidate_id"] in {
        row["candidate_id"] for row in component["members"]
    }
    assert plan["later_stage_frontier"]["multi_target"]["status"] == "blocked"


def test_multi_target_is_deferred_without_joining_collision() -> None:
    source, stage1, draft = _fixture()
    current = build_post_morphology_index(
        source_index=source, stage1_plan=stage1, stage1_draft=draft
    )
    plan = build_target_collision_plan(current)

    assert plan["counts"]["multi_target_deferred_entries"] == 1
    assert plan["multi_target_deferred"][0]["canonical_source"] == "example"
    component_sources = {
        row["canonical_source"]
        for component in plan["components"]
        for row in component["members"]
    }
    assert "example" not in component_sources


def test_zero_collision_is_sealed_noop_and_unblocks_next_stage() -> None:
    source, stage1, draft = _fixture()
    source["decisions"][2]["target_proposals"] = [
        {"target_vi": "kien truc", "applicability": None}
    ]
    source["counts"] = _index_counts(source["decisions"])
    source["index_sha256"] = _sha256_json(
        {key: value for key, value in source.items() if key != "index_sha256"}
    )
    stage1["source_index_sha256"] = source["index_sha256"]
    stage1["plan_sha256"] = _sha256_json(
        {key: value for key, value in stage1.items() if key != "plan_sha256"}
    )
    draft["source_index_sha256"] = source["index_sha256"]
    draft["source_plan_sha256"] = stage1["plan_sha256"]
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )
    current = build_post_morphology_index(
        source_index=source, stage1_plan=stage1, stage1_draft=draft
    )
    plan = build_target_collision_plan(current)
    packets, dry = packetize_components(
        plan=plan,
        index=current,
        caps=ConsolidationCaps(
            max_components=6,
            max_members=16,
            max_unique_blocks=24,
            prompt_token_cap=6000,
        ),
    )

    assert plan["stage_status"] == "complete_no_review_required"
    assert plan["auditor_required"] is False
    assert plan["later_stage_frontier"]["multi_target"]["status"] == "ready"
    assert packets == []
    assert dry["totals"]["packet_count"] == 0


def test_rejects_tampered_stage1_draft() -> None:
    source, stage1, draft = _fixture()
    draft["audited_entries"][0]["canonical_target_vi"] = "invented"

    with pytest.raises(ConsolidationPlanError, match="stage-1 draft hash mismatch"):
        build_post_morphology_index(
            source_index=source, stage1_plan=stage1, stage1_draft=draft
        )


def test_rejects_resealed_semantic_invention() -> None:
    source, stage1, draft = _fixture()
    draft["audited_entries"][0]["canonical_target_vi"] = "invented"
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )

    with pytest.raises(ConsolidationPlanError, match="invented a target"):
        build_post_morphology_index(
            source_index=source, stage1_plan=stage1, stage1_draft=draft
        )


def test_pending_morphology_blocks_stage2() -> None:
    source, stage1, draft = _fixture()
    draft["audited_entries"] = []
    draft["pending_components"] = [
        {
            "component_id": "morph_pair",
            "member_candidate_ids": ["cand_model", "cand_models"],
            "pending_reason": "Insufficient evidence.",
        }
    ]
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )

    with pytest.raises(ConsolidationPlanError, match="still pending"):
        build_post_morphology_index(
            source_index=source, stage1_plan=stage1, stage1_draft=draft
        )


def test_rejects_stage1_assignment_drift() -> None:
    source, stage1, draft = _fixture()
    stage1["deferred_candidate_ids"].remove("cand_tensor")
    stage1["plan_sha256"] = _sha256_json(
        {key: value for key, value in stage1.items() if key != "plan_sha256"}
    )
    draft["source_plan_sha256"] = stage1["plan_sha256"]
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )

    with pytest.raises(ConsolidationPlanError, match="does not exact-cover"):
        build_post_morphology_index(
            source_index=source, stage1_plan=stage1, stage1_draft=draft
        )


def test_deterministic_and_does_not_mutate_inputs() -> None:
    source, stage1, draft = _fixture()
    originals = deepcopy((source, stage1, draft))

    first_index = build_post_morphology_index(
        source_index=source, stage1_plan=stage1, stage1_draft=draft
    )
    second_index = build_post_morphology_index(
        source_index=source, stage1_plan=stage1, stage1_draft=draft
    )
    first_plan = build_target_collision_plan(first_index)
    second_plan = build_target_collision_plan(second_index)

    assert first_index == second_index
    assert first_plan == second_plan
    assert (source, stage1, draft) == originals


def test_morphology_surface_order_has_stable_case_tiebreak() -> None:
    left = _candidate("cand_model", "model", ["mo hinh"], "b001")
    right = _candidate("cand_models", "models", ["mo hinh"], "b002")
    left["surfaces"] = ["model", "Model"]
    right["surfaces"] = ["models", "Models"]
    entry = {
        "member_candidate_ids": ["cand_model", "cand_models"],
        "canonical_source": "model",
        "canonical_target_vi": "mo hinh",
        "alternative_targets": [],
        "directive": "translate",
        "evidence_block_ids": ["b001", "b002"],
        "rationale": "The forms are one audited concept.",
    }

    current = _current_entry_from_audit(
        entry=entry,
        members={"cand_model": left, "cand_models": right},
        source_index_sha256="A" * 64,
        authority_kind="stage1_live_audit",
        authority_hash="B" * 64,
    )

    assert current["surfaces"] == ["Model", "model", "Models", "models"]
