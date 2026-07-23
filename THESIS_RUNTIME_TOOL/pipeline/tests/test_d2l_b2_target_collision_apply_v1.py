from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationPlanError,
    _index_counts,
    _sha256_json,
)
from pipeline.prepass.d2l_b2_target_collision_apply_v1 import (
    INDEX_VERSION,
    PLAN_VERSION,
    apply_target_collision_audit,
)
from pipeline.prepass.d2l_b2_target_collision_plan_v1 import (
    build_target_collision_plan,
)


def _row(candidate_id: str, source: str, targets: list[str], block: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "chapter_id": "chapter_alpha",
        "surfaces": [source],
        "decision": "admit",
        "canonical_source": source,
        "target_proposals": [
            {"target_vi": target, "applicability": None} for target in targets
        ],
        "directive": "translate",
        "evidence_block_ids": [block],
        "evidence_complete": True,
        "decision_rationale": "Source-grounded fixture.",
        "source_member_candidate_ids": [f"root_{candidate_id}"],
        "lineage": {"validation_sha256": "A" * 64},
    }


def _fixture() -> tuple[dict, dict, dict]:
    rows = [
        _row("entry_model", "model", ["mo hinh"], "b001"),
        _row("entry_models", "models", ["mo hinh"], "b002"),
        _row("entry_arch", "architecture", ["mo hinh"], "b003"),
        _row("entry_example", "example", ["vi du", "mau"], "b004"),
    ]
    current = {
        "index_version": "fixture_post_morphology",
        "chapter_ids": ["chapter_alpha"],
        "source_lineage": [],
        "source_stage1_plan_sha256": "B" * 64,
        "source_stage1_draft_sha256": "C" * 64,
        "decisions": rows,
        "source_blocks": [
            {"block_id": f"b00{number}", "text": f"Context {number}."}
            for number in range(1, 5)
        ],
        "production_publish_allowed": False,
    }
    current["counts"] = _index_counts(rows)
    current["index_sha256"] = _sha256_json(current)
    plan = build_target_collision_plan(current)
    component = plan["components"][0]
    entry_by_source = {
        row["canonical_source"]: row for row in current["decisions"]
    }
    merged = _audited_entry(
        component_id=component["component_id"],
        rows=[entry_by_source["model"], entry_by_source["models"]],
        canonical_source="model",
    )
    separate = _audited_entry(
        component_id=component["component_id"],
        rows=[entry_by_source["architecture"]],
        canonical_source="architecture",
    )
    draft = {
        "draft_version": "fixture_stage2_draft",
        "production_published": False,
        "source_index_sha256": current["index_sha256"],
        "source_plan_sha256": plan["plan_sha256"],
        "audited_entries": [merged, separate],
        "provisional_clean": deepcopy(plan["provisional_clean"]),
        "pending_components": [],
        "pending_admission": deepcopy(plan["pending_admission"]),
        "rejected_ledger": deepcopy(plan["rejected_ledger"]),
        "counts": {},
    }
    draft["draft_sha256"] = _sha256_json(draft)
    return current, plan, draft


def _audited_entry(
    *, component_id: str, rows: list[dict], canonical_source: str
) -> dict:
    evidence = sorted(
        {block for row in rows for block in row["evidence_block_ids"]}
    )
    surfaces = sorted(
        {
            value
            for row in rows
            for value in [row["canonical_source"], *row["surfaces"]]
        }
    )
    return {
        "draft_entry_id": f"draft_{canonical_source}",
        "status": "audited_draft",
        "component_id": component_id,
        "member_candidate_ids": [row["candidate_id"] for row in rows],
        "canonical_source": canonical_source,
        "source_variants": surfaces,
        "canonical_target_vi": "mo hinh",
        "alternative_targets": [],
        "directive": "translate",
        "evidence_block_ids": evidence,
        "auditor_cited_evidence_block_ids": evidence,
        "rationale": "Fixture target-collision decision.",
    }


def test_applies_merge_and_keep_separate_then_unblocks_multi_target() -> None:
    current, plan, draft = _fixture()
    resolved_index, resolved_plan = apply_target_collision_audit(
        current_index=current, stage2_plan=plan, stage2_draft=draft
    )

    assert resolved_index["index_version"] == INDEX_VERSION
    assert resolved_index["counts"]["source_admitted_entries"] == 4
    assert resolved_index["counts"]["current_admitted_entries"] == 3
    assert resolved_index["counts"]["root_member_candidate_exact_cover"] == 4
    assert resolved_plan["plan_version"] == PLAN_VERSION
    assert resolved_plan["stage_status"] == "complete_no_review_required"
    assert resolved_plan["components"] == []
    assert resolved_plan["counts"]["single_target_clean_entries"] == 2
    assert resolved_plan["counts"]["multi_target_deferred_entries"] == 1
    assert (
        resolved_plan["later_stage_frontier"]["multi_target"]["requires"]
        == "sealed_stage2_zero_component_plan"
    )


def test_result_is_deterministic() -> None:
    current, plan, draft = _fixture()
    first = apply_target_collision_audit(
        current_index=current, stage2_plan=plan, stage2_draft=draft
    )
    second = apply_target_collision_audit(
        current_index=current, stage2_plan=plan, stage2_draft=draft
    )
    assert first == second


def test_rejects_missing_component_member() -> None:
    current, plan, draft = _fixture()
    draft["audited_entries"] = draft["audited_entries"][:1]
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )
    with pytest.raises(ConsolidationPlanError, match="exact-cover"):
        apply_target_collision_audit(
            current_index=current, stage2_plan=plan, stage2_draft=draft
        )


def test_rejects_invented_target() -> None:
    current, plan, draft = _fixture()
    draft["audited_entries"][0]["canonical_target_vi"] = "invented"
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )
    with pytest.raises(ConsolidationPlanError, match="invented a target"):
        apply_target_collision_audit(
            current_index=current, stage2_plan=plan, stage2_draft=draft
        )


def test_rejects_pending_component() -> None:
    current, plan, draft = _fixture()
    draft["pending_components"] = [{"component_id": plan["components"][0]["component_id"]}]
    draft["draft_sha256"] = _sha256_json(
        {key: value for key, value in draft.items() if key != "draft_sha256"}
    )
    with pytest.raises(ConsolidationPlanError, match="remains pending"):
        apply_target_collision_audit(
            current_index=current, stage2_plan=plan, stage2_draft=draft
        )
