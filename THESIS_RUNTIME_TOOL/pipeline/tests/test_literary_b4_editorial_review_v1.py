from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b4_editorial_review_v1 import (
    B4EditorialReviewError,
    RESPONSE_SCHEMA_VERSION,
    apply_approved_editorial_reviews_v1,
    build_editorial_approval_v1,
    build_editorial_review_artifact_v1,
    build_editorial_review_packets_v1,
    render_editorial_review_request_v1,
    validate_editorial_review_response_v1,
)
from pipeline.literary.b4_translator_pack_v1 import (
    SCHEMA_VERSION as TRANSLATOR_PACK_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash


STYLE_PROFILE = "Use restrained literary Vietnamese."


def _seal(body: dict) -> dict:
    return {**deepcopy(body), "artifact_hash": canonical_hash(body)}


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "clean_text": "The house was not hospitable.",
            },
            {
                "block_id": "bk_ch01_b002",
                "clean_text": '"Come in, sir," he said.',
            },
            {
                "block_id": "bk_ch01_b003",
                "clean_text": "A pint of water struck him.",
            },
            {
                "block_id": "bk_ch01_b004",
                "clean_text": "The night was quiet.",
            },
        ],
    }


def _translation(pack_hash: str) -> dict:
    body = {
        "schema_version": "literary_b4_translation_chapter_v7",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "chapter_order": 1,
        "translator_pack_artifact_hash": pack_hash,
        "style_profile_version": "style_v1",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "source_text": "The house was not hospitable.",
                "target_text": "Ngôi nhà rất hiếu khách.",
            },
            {
                "block_id": "bk_ch01_b002",
                "source_text": '"Come in, sir," he said.',
                "target_text": '"Mời ông vào," ông ta nói.',
            },
            {
                "block_id": "bk_ch01_b003",
                "source_text": "A pint of water struck him.",
                "target_text": "Một pint nước dội vào ông.",
            },
            {
                "block_id": "bk_ch01_b004",
                "source_text": "The night was quiet.",
                "target_text": "Đêm ấy yên tĩnh.",
            },
        ],
        "provider_calls": 1,
    }
    return _seal(body)


def _pack() -> dict:
    body = {
        "schema_version": TRANSLATOR_PACK_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "chapter_order": 1,
        "story_bible_artifact_hash": "2" * 64,
        "address_anchor_artifact_hash": "3" * 64,
        "entities": [
            {
                "effective_entity_id": "b0ent_speaker",
                "canonical_surface": "Mr. Lockwood",
                "stable_surfaces": ["Lockwood"],
                "aliases": [],
                "referent_kind": "person",
                "memory_tier": "detail",
                "claims": {},
                "first_seen": "bk_ch01",
            }
        ],
        "relations": [],
        "states": [],
        "idiolect": [],
        "narrative_position": {
            "capsules": [],
            "frames": [
                {
                    "frame_segment_id": "frame_1",
                    "start_block_id": "bk_ch01_b001",
                    "end_block_id": "bk_ch01_b004",
                    "narrative_mode": "direct_current",
                    "narrator_effective_entity_ids": ["b0ent_speaker"],
                }
            ],
            "handoff": {},
        },
        "open_questions": {
            "contested_relations": [],
            "pending_identity_cases": [],
            "pending_states": [],
            "unknowable_windows": [],
            "unresolved_address": [],
        },
        "pack_budget": {"fits": True, "omissions": []},
        "projection_policy": {},
        "projection_metrics": {},
        "projection_strategy": "tiered_v2",
        "planning_only": False,
        "provider_calls": 0,
    }
    return _seal(body)


def _lint(translation_hash: str) -> dict:
    observation = {
        "observation_id": "b4obs1_pint",
        "observation_kind": "verbatim_source_token_carry_through",
        "block_id": "bk_ch01_b003",
        "token": "pint",
        "occurrence_count": 1,
    }
    body = {
        "schema_version": "literary_b4_translation_lint_report_v2",
        "status": "clean",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "source_translation_artifact_hash": translation_hash,
        "source_translation_schema_version": (
            "literary_b4_translation_chapter_v7"
        ),
        "window_plan_hash": "4" * 64,
        "lint_policy_hash": "5" * 64,
        "translated_block_count": 4,
        "issue_count": 0,
        "issue_by_kind": {},
        "issues": [],
        "source_carry_through_checked": True,
        "translator_pack_artifact_hash": None,
        "observation_count": 1,
        "observation_by_kind": {
            "verbatim_source_token_carry_through": 1
        },
        "observations": [observation],
        "mechanical_fix_requested": False,
        "mechanical_correction_count": 0,
        "mechanical_corrections": [],
        "remaining_issue_count": 0,
        "remaining_issue_by_kind": {},
        "remaining_issues": [],
        "corrected_translation_artifact_hash": None,
        "provider_calls": 0,
        "semantic_record_mutation_performed": False,
    }
    return _seal(body)


def _slice() -> dict:
    body = {
        "schema_version": "literary_b4_window_slice_v1",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "window_id": "window_1",
        "window_order": 1,
        "window_plan_hash": "4" * 64,
        "active_block_ids": [
            "bk_ch01_b001",
            "bk_ch01_b002",
            "bk_ch01_b003",
            "bk_ch01_b004",
        ],
        "preceding_tail_block_ids": [],
        "estimated_active_source_tokens": 100,
        "speaker_turns": [
            {
                "speaker_turn_id": "turn_1",
                "block_id": "bk_ch01_b002",
                "speaker": {
                    "effective_entity_id": "b0ent_speaker",
                    "surface": "he",
                },
                "addressee": None,
            }
        ],
        "address_pairs": [],
        "lineage": {},
        "provider_calls": 0,
    }
    return _seal(body)


def _prepared(
    *,
    selection_mode: str = "flagged_plus_sample",
    sample_count: int = 1,
) -> tuple[dict, dict, dict]:
    pack = _pack()
    translation = _translation(pack["artifact_hash"])
    packets, report = build_editorial_review_packets_v1(
        translation_artifact=translation,
        chapter=_chapter(),
        translator_pack=pack,
        lint_report=_lint(translation["artifact_hash"]),
        style_profile_version="style_v1",
        style_profile_sha256=canonical_hash(STYLE_PROFILE),
        selection_mode=selection_mode,
        sample_count=sample_count,
        sample_seed="fixture_seed",
        context_radius=1,
        max_candidates_per_batch=8,
        window_slices=[_slice()],
    )
    return packets[0], report, translation


def _response(packet: dict) -> dict:
    rows = []
    for candidate in packet["candidates"]:
        block_id = candidate["block_id"]
        current = candidate["current_target_text"]
        if block_id == "bk_ch01_b003":
            rows.append(
                {
                    "block_id": block_id,
                    "quality_score": 0.7,
                    "suggested_action": "repair",
                    "proposed_target_text": (
                        "Khoảng nửa lít nước dội vào ông."
                    ),
                    "issues": [
                        {
                            "type": "source_carry_through",
                            "severity": "minor",
                            "description": "The source unit remains untranslated.",
                            "evidence_source": "pint",
                            "evidence_target": "pint",
                            "suggested_fix": "Use the configured unit policy.",
                        }
                    ],
                }
            )
        else:
            rows.append(
                {
                    "block_id": block_id,
                    "quality_score": 0.9,
                    "suggested_action": "accept",
                    "proposed_target_text": current,
                    "issues": [],
                }
            )
    return {"schema_version": RESPONSE_SCHEMA_VERSION, "blocks": rows}


def test_routing_keeps_lint_signal_and_adds_deterministic_sample() -> None:
    packet, report, _translation_artifact = _prepared()

    assert report["candidate_block_count"] == 2
    assert report["selection_reason_counts"] == {
        "deterministic_sample": 1,
        (
            "lint_observation:"
            "verbatim_source_token_carry_through"
        ): 1,
    }
    assert "bk_ch01_b003" in packet["candidate_block_ids"]
    carry = next(
        row
        for row in packet["candidates"]
        if row["block_id"] == "bk_ch01_b003"
    )
    assert carry["tier1_findings"][0]["kind"] == (
        "verbatim_source_token_carry_through"
    )
    assert packet["pack_context"]["entities"][0][
        "effective_entity_id"
    ] == "b0ent_speaker"


def test_all_blocks_mode_exact_covers_every_translation_block() -> None:
    packet, report, _translation_artifact = _prepared(
        selection_mode="all_blocks",
        sample_count=0,
    )

    assert report["candidate_block_count"] == 4
    assert packet["candidate_block_ids"] == [
        "bk_ch01_b001",
        "bk_ch01_b002",
        "bk_ch01_b003",
        "bk_ch01_b004",
    ]


def test_source_heading_outside_translation_is_allowed() -> None:
    pack = _pack()
    translation = _translation(pack["artifact_hash"])
    chapter = _chapter()
    chapter["blocks"].insert(
        0,
        {
            "block_id": "bk_ch01_b000",
            "clean_text": "CHAPTER I",
        },
    )

    packets, report = build_editorial_review_packets_v1(
        translation_artifact=translation,
        chapter=chapter,
        translator_pack=pack,
        lint_report=_lint(translation["artifact_hash"]),
        style_profile_version="style_v1",
        style_profile_sha256=canonical_hash(STYLE_PROFILE),
        selection_mode="all_blocks",
    )

    assert report["candidate_block_count"] == 4
    assert packets[0]["candidate_block_ids"] == [
        "bk_ch01_b001",
        "bk_ch01_b002",
        "bk_ch01_b003",
        "bk_ch01_b004",
    ]


def test_translation_must_remain_an_ordered_source_subset() -> None:
    pack = _pack()
    translation = _translation(pack["artifact_hash"])
    translation["blocks"][0], translation["blocks"][1] = (
        translation["blocks"][1],
        translation["blocks"][0],
    )
    translation = _seal(
        {
            key: value
            for key, value in translation.items()
            if key != "artifact_hash"
        }
    )

    with pytest.raises(
        B4EditorialReviewError,
        match="not an ordered source subset",
    ):
        build_editorial_review_packets_v1(
            translation_artifact=translation,
            chapter=_chapter(),
            translator_pack=pack,
            lint_report=_lint(translation["artifact_hash"]),
            style_profile_version="style_v1",
            style_profile_sha256=canonical_hash(STYLE_PROFILE),
            selection_mode="all_blocks",
        )


def test_review_validator_keeps_evidence_grounded_and_does_not_mutate() -> None:
    packet, _report, _translation_artifact = _prepared()
    rendered = render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=STYLE_PROFILE,
    )
    validated = validate_editorial_review_response_v1(
        rendered=rendered,
        response=_response(packet),
    )
    artifact = build_editorial_review_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt=None,
        provider_called=False,
    )

    assert artifact["action_counts"] == {"accept": 1, "repair": 1}
    assert artifact["translation_text_mutation_performed"] is False
    repair = next(
        row
        for row in artifact["blocks"]
        if row["suggested_action"] == "repair"
    )
    assert repair["issues"][0]["issue_id"].startswith("b4edit1_")


def test_review_rejects_fabricated_source_evidence() -> None:
    packet, _report, _translation_artifact = _prepared()
    rendered = render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=STYLE_PROFILE,
    )
    response = _response(packet)
    repair = next(
        row for row in response["blocks"] if row["issues"]
    )
    repair["issues"][0]["evidence_source"] = "foreign evidence"

    with pytest.raises(
        B4EditorialReviewError,
        match="source evidence is not verbatim",
    ):
        validate_editorial_review_response_v1(
            rendered=rendered,
            response=response,
        )


def test_review_rejects_candidate_loss_or_foreign_block() -> None:
    packet, _report, _translation_artifact = _prepared()
    rendered = render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=STYLE_PROFILE,
    )
    response = _response(packet)
    response["blocks"][0]["block_id"] = "foreign_block"

    with pytest.raises(
        B4EditorialReviewError,
        match="exact-cover candidates",
    ):
        validate_editorial_review_response_v1(
            rendered=rendered,
            response=response,
        )


def test_apply_changes_only_explicitly_approved_repair() -> None:
    packet, _report, translation = _prepared()
    rendered = render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=STYLE_PROFILE,
    )
    validated = validate_editorial_review_response_v1(
        rendered=rendered,
        response=_response(packet),
    )
    review = build_editorial_review_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt=None,
        provider_called=False,
    )
    repair = next(
        row for row in review["blocks"] if row["suggested_action"] == "repair"
    )
    approval = build_editorial_approval_v1(
        source_translation_artifact_hash=translation["artifact_hash"],
        review_artifact_hashes=[review["artifact_hash"]],
        decisions=[
            {
                "review_artifact_hash": review["artifact_hash"],
                "block_id": repair["block_id"],
                "decision": "approve",
            }
        ],
    )

    edited, report = apply_approved_editorial_reviews_v1(
        translation_artifact=translation,
        review_artifacts=[review],
        approval_artifact=approval,
    )

    assert report["approved_revision_count"] == 1
    assert edited["editorial_change_count"] == 1
    by_id = {row["block_id"]: row for row in edited["blocks"]}
    assert by_id["bk_ch01_b003"]["target_text"] == (
        "Khoảng nửa lít nước dội vào ông."
    )
    assert by_id["bk_ch01_b001"] == translation["blocks"][0]
    assert edited["semantic_record_mutation_performed"] is False


def test_approval_cannot_apply_accept_or_human_review_row() -> None:
    packet, _report, translation = _prepared()
    rendered = render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=STYLE_PROFILE,
    )
    validated = validate_editorial_review_response_v1(
        rendered=rendered,
        response=_response(packet),
    )
    review = build_editorial_review_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt=None,
        provider_called=False,
    )
    accepted = next(
        row for row in review["blocks"] if row["suggested_action"] == "accept"
    )
    approval = build_editorial_approval_v1(
        source_translation_artifact_hash=translation["artifact_hash"],
        review_artifact_hashes=[review["artifact_hash"]],
        decisions=[
            {
                "review_artifact_hash": review["artifact_hash"],
                "block_id": accepted["block_id"],
                "decision": "approve",
            }
        ],
    )

    with pytest.raises(
        B4EditorialReviewError,
        match="non-repair proposal",
    ):
        apply_approved_editorial_reviews_v1(
            translation_artifact=translation,
            review_artifacts=[review],
            approval_artifact=approval,
        )


def test_lint_lineage_mismatch_halts_before_packet_creation() -> None:
    pack = _pack()
    translation = _translation(pack["artifact_hash"])
    lint = _lint("f" * 64)

    with pytest.raises(
        B4EditorialReviewError,
        match="lint report and translation lineage differ",
    ):
        build_editorial_review_packets_v1(
            translation_artifact=translation,
            chapter=_chapter(),
            translator_pack=pack,
            lint_report=lint,
            style_profile_version="style_v1",
            style_profile_sha256=canonical_hash(STYLE_PROFILE),
            selection_mode="all_blocks",
        )
