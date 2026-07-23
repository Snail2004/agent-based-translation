from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationPlanError,
    _sha256_json,
)
from pipeline.prepass.d2l_b2_multi_target_contract_v1 import (
    MultiTargetContractError,
    prompt_sha256,
    render_messages,
    response_schema_sha256,
    validate_output,
)
from pipeline.prepass.d2l_b2_multi_target_plan_v1 import (
    MultiTargetCaps,
    build_multi_target_plan,
    packetize_multi_target_items,
)
from pipeline.prepass.d2l_b2_target_collision_plan_v1 import (
    build_target_collision_plan,
)


def _row(
    candidate_id: str,
    source: str,
    targets: list[tuple[str, str | None]],
    blocks: list[str],
) -> dict:
    return {
        "candidate_id": candidate_id,
        "chapter_id": "chapter_alpha",
        "surfaces": [source],
        "decision": "admit",
        "canonical_source": source,
        "target_proposals": [
            {"target_vi": target, "applicability": applicability}
            for target, applicability in targets
        ],
        "directive": "translate",
        "evidence_block_ids": blocks,
        "evidence_complete": True,
        "decision_rationale": "The source names a reusable technical unit.",
        "source_member_candidate_ids": [candidate_id],
        "lineage": {
            "authority_kind": "fixture",
            "authority_hash": "A" * 64,
            "source_index_sha256": "D" * 64,
            "source_member_candidate_ids": [candidate_id],
        },
    }


def _fixture() -> tuple[dict, dict]:
    rows = [
        _row(
            "cand_example",
            "example",
            [("vi du", None), ("mau", "for a data instance")],
            ["b001", "b002"],
        ),
        _row(
            "cand_sample",
            "sample",
            [("mau", None), ("mau du lieu", "for an observed data row")],
            ["b002", "b003"],
        ),
        _row("cand_tensor", "tensor", [("tensor", None)], ["b004"]),
    ]
    index = {
        "index_version": "fixture_index",
        "chapter_ids": ["chapter_alpha"],
        "source_stage1_plan_sha256": "B" * 64,
        "source_stage1_draft_sha256": "C" * 64,
        "decisions": rows,
        "source_blocks": [
            {"block_id": "b001", "text": "This is a worked example."},
            {"block_id": "b002", "text": "Each example is one sample."},
            {"block_id": "b003", "text": "A sample is drawn from data."},
            {"block_id": "b004", "text": "A tensor stores values."},
        ],
        "production_publish_allowed": False,
    }
    index["counts"] = {"total": 3, "admit": 3, "review": 0, "reject": 0}
    index["index_sha256"] = _sha256_json(index)
    stage2 = build_target_collision_plan(index)
    return index, stage2


def _packet() -> dict:
    return {
        "packet_id": "mtpkt_fixture",
        "chapter_id": "chapter_alpha",
        "review_items": [
            {
                "candidate_id": "cand_example",
                "canonical_source": "example",
                "surfaces": ["example", "examples"],
                "target_proposals": [
                    {"target_vi": "vi du", "applicability": None},
                    {
                        "target_vi": "mau",
                        "applicability": "for a data instance",
                    },
                ],
                "directive": "translate",
                "evidence_block_ids": ["b001", "b002"],
                "reason_codes": ["multiple_distinct_target_proposals"],
            }
        ],
        "source_blocks": [
            {"block_id": "b001", "text": "This is a worked example."},
            {"block_id": "b002", "text": "Each example is a data instance."},
        ],
    }


def _resolved() -> dict:
    return {
        "packet_id": "mtpkt_fixture",
        "decisions": [
            {
                "candidate_id": "cand_example",
                "action": "resolve",
                "target_dispositions": [
                    {
                        "target_vi": "vi du",
                        "disposition": "canonical",
                        "applicability": None,
                    },
                    {
                        "target_vi": "mau",
                        "disposition": "alternative",
                        "applicability": "Only for a data instance.",
                    },
                ],
                "evidence_block_ids": ["b001", "b002"],
                "rationale": "The blocks show two distinct source senses.",
                "pending_reason": None,
            }
        ],
    }


def test_prompt_schema_and_render_are_stable_and_book_neutral() -> None:
    assert len(prompt_sha256()) == 64
    assert len(response_schema_sha256()) == 64
    rendered = render_messages(_packet())
    assert rendered == render_messages(_packet())
    assert "worked example" not in rendered[0]["content"]
    assert "community glossary" in rendered[0]["content"]


def test_resolved_decision_exact_covers_targets() -> None:
    validation = validate_output(_resolved(), packet=_packet())
    assert validation.errors == ()
    assert validation.missing_candidate_ids == ()
    assert [
        row.disposition
        for row in validation.decisions[0].target_dispositions
    ] == ["canonical", "alternative"]


def test_pending_requires_all_targets_pending() -> None:
    payload = _resolved()
    decision = payload["decisions"][0]
    decision["action"] = "pending"
    decision["pending_reason"] = "The supplied blocks do not settle usage."
    for row in decision["target_dispositions"]:
        row["disposition"] = "pending"
        row["applicability"] = None
    assert validate_output(payload, packet=_packet()).errors == ()
    decision["target_dispositions"][0]["disposition"] = "canonical"
    assert "mark every target pending" in validate_output(
        payload, packet=_packet()
    ).errors[0]


def test_contract_rejects_invented_duplicate_missing_and_foreign_values() -> None:
    invented = _resolved()
    invented["decisions"][0]["target_dispositions"][1][
        "target_vi"
    ] = "invented"
    assert "not a supplied B2 target" in validate_output(
        invented, packet=_packet()
    ).errors[0]

    duplicate = _resolved()
    duplicate["decisions"][0]["target_dispositions"][1]["target_vi"] = "vi du"
    assert "repeats a target" in validate_output(
        duplicate, packet=_packet()
    ).errors[0]

    missing = _resolved()
    missing["decisions"][0]["target_dispositions"].pop()
    assert "does not exact-cover supplied targets" in validate_output(
        missing, packet=_packet()
    ).errors[0]

    foreign = _resolved()
    foreign["decisions"][0]["evidence_block_ids"] = ["outside"]
    assert "evidence_block_ids is invalid" in validate_output(
        foreign, packet=_packet()
    ).errors[0]


def test_contract_rejects_bad_canonical_and_alternative_applicability() -> None:
    two_canonical = _resolved()
    two_canonical["decisions"][0]["target_dispositions"][1].update(
        {"disposition": "canonical", "applicability": None}
    )
    assert "requires one canonical target" in validate_output(
        two_canonical, packet=_packet()
    ).errors[0]

    no_condition = _resolved()
    no_condition["decisions"][0]["target_dispositions"][1][
        "applicability"
    ] = ""
    assert "required for an alternative" in validate_output(
        no_condition, packet=_packet()
    ).errors[0]


def test_contract_exact_covers_candidate_ids() -> None:
    missing = {"packet_id": "mtpkt_fixture", "decisions": []}
    assert validate_output(missing, packet=_packet()).missing_candidate_ids == (
        "cand_example",
    )
    duplicate = _resolved()
    duplicate["decisions"].append(deepcopy(duplicate["decisions"][0]))
    assert validate_output(
        duplicate, packet=_packet()
    ).duplicate_candidate_ids == ("cand_example",)


def test_packet_rejects_duplicate_candidate_and_missing_block() -> None:
    duplicate = _packet()
    duplicate["review_items"].append(deepcopy(duplicate["review_items"][0]))
    with pytest.raises(MultiTargetContractError, match="candidate_id is invalid"):
        render_messages(duplicate)

    missing = _packet()
    missing["review_items"][0]["evidence_block_ids"] = ["outside"]
    with pytest.raises(MultiTargetContractError, match="evidence is invalid"):
        render_messages(missing)


def test_plan_exact_covers_multi_target_entries_and_preserves_clean_rows() -> None:
    index, stage2 = _fixture()
    plan = build_multi_target_plan(current_index=index, stage2_plan=stage2)
    assert plan["counts"]["multi_target_review_entries"] == 2
    assert plan["counts"]["single_target_clean_entries"] == 1
    assert plan["counts"]["current_admitted_exact_cover"] == 3
    assert plan["counts"]["target_proposals_under_review"] == 4
    assert plan["auditor_required"] is True


def test_packetizer_reuses_shared_blocks_without_joining_entries() -> None:
    index, stage2 = _fixture()
    plan = build_multi_target_plan(current_index=index, stage2_plan=stage2)
    packets, dry = packetize_multi_target_items(
        plan=plan,
        index=index,
        caps=MultiTargetCaps(
            max_items=2,
            max_target_proposals=4,
            max_unique_blocks=4,
            prompt_token_cap=6000,
        ),
    )
    assert len(packets) == 1
    assert len(packets[0]["review_items"]) == 2
    assert [row["block_id"] for row in packets[0]["source_blocks"]] == [
        "b001",
        "b002",
        "b003",
    ]
    assert dry["totals"]["source_block_reuse_savings"] == 1
    assert dry["totals"]["review_item_count"] == 2


def test_rejects_tampered_or_not_ready_stage2() -> None:
    index, stage2 = _fixture()
    tampered = deepcopy(stage2)
    tampered["multi_target_deferred"][0]["canonical_source"] = "invented"
    with pytest.raises(ConsolidationPlanError, match="stage-2 plan hash mismatch"):
        build_multi_target_plan(current_index=index, stage2_plan=tampered)

    blocked = deepcopy(stage2)
    blocked["later_stage_frontier"]["multi_target"]["status"] = "blocked"
    blocked["plan_sha256"] = _sha256_json(
        {key: value for key, value in blocked.items() if key != "plan_sha256"}
    )
    with pytest.raises(ConsolidationPlanError, match="frontier is not ready"):
        build_multi_target_plan(current_index=index, stage2_plan=blocked)


def test_rejects_resealed_deferred_row_drift_and_tight_cap() -> None:
    index, stage2 = _fixture()
    drifted = deepcopy(stage2)
    drifted["multi_target_deferred"][0]["canonical_source"] = "invented"
    drifted["plan_sha256"] = _sha256_json(
        {key: value for key, value in drifted.items() if key != "plan_sha256"}
    )
    with pytest.raises(ConsolidationPlanError, match="deferred multi-target rows"):
        build_multi_target_plan(current_index=index, stage2_plan=drifted)

    plan = build_multi_target_plan(current_index=index, stage2_plan=stage2)
    with pytest.raises(ConsolidationPlanError, match="cannot fit packet caps"):
        packetize_multi_target_items(
            plan=plan,
            index=index,
            caps=MultiTargetCaps(
                max_items=1,
                max_target_proposals=1,
                max_unique_blocks=1,
                prompt_token_cap=1,
            ),
        )


def test_planner_is_deterministic_and_does_not_mutate_inputs() -> None:
    index, stage2 = _fixture()
    originals = deepcopy((index, stage2))
    first = build_multi_target_plan(current_index=index, stage2_plan=stage2)
    second = build_multi_target_plan(current_index=index, stage2_plan=stage2)
    assert first == second
    assert (index, stage2) == originals
