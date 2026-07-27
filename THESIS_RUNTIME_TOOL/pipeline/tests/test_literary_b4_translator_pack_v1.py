from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.literary.b4_translator_pack_v1 import (
    B4TranslatorPackError,
    calibrated_token_estimate_v1,
    project_translator_pack_tiered_v2,
    project_translator_pack_v1,
    seal_translator_pack_v1,
    translator_pack_prompt_view_v1,
)
from pipeline.literary.b4_translator_v1 import (
    assert_stable_prefixes_v1,
    render_translation_window_request_v1,
)
from pipeline.literary.b4_story_bible_assembler_v1 import (
    SCHEMA_VERSION as STORY_BIBLE_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.scripts.run_literary_b4_translator_pack_v1 import (
    _current_planning_window_slices_v1,
)


def _seal(body: dict) -> dict:
    return {**deepcopy(body), "artifact_hash": canonical_hash(body)}


def _entity(
    entity_id: str,
    *,
    first_chapter: str,
    member_chapters: list[str],
) -> dict:
    return {
        "effective_entity_id": entity_id,
        "canonical_surface": entity_id,
        "aliases": [],
        "stable_surfaces": [entity_id],
        "claims": {
            "gender": {"value": "unknown", "evidence_ref": f"ev:{entity_id}"}
        },
        "referent_kind": "person",
        "first_seen": f"{first_chapter}_b001",
        "member_card_ids": [f"card:{entity_id}"],
        "member_chapters": member_chapters,
        "record_class": "person",
        "established_in_chapter": first_chapter,
    }


def _story() -> dict:
    body = {
        "schema_version": STORY_BIBLE_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "chapter_id": "bk_ch08",
        "chapter_order": 8,
        "entities": [
            _entity("speaker_old", first_chapter="bk_ch01", member_chapters=["bk_ch01"]),
            _entity(
                "silent_core",
                first_chapter="bk_ch02",
                member_chapters=["bk_ch02", "bk_ch04"],
            ),
            _entity("current_new", first_chapter="bk_ch08", member_chapters=["bk_ch08"]),
            _entity("dormant_extra", first_chapter="bk_ch01", member_chapters=["bk_ch01"]),
        ],
        "relations": [
            {
                "relation_edge_id": "relation_other_kin",
                "source_effective_entity_id": "speaker_old",
                "target_effective_entity_id": "current_new",
                "relation": "other_kin",
                "relation_family": "kinship_link",
                "relation_note": "related by marriage",
                "structurally_contested": False,
                "effective": True,
                "semantic_status": "auditor_reviewed",
            },
            {
                "relation_edge_id": "relation_out",
                "source_effective_entity_id": "dormant_extra",
                "target_effective_entity_id": "current_new",
                "relation": "associated_with",
                "relation_family": "entity_link",
                "relation_note": None,
                "structurally_contested": False,
                "effective": True,
            },
        ],
        "states": [
            {
                "state_id": "state_open",
                "state_domain": "presence",
                "state_value": "speaker_old is present",
                "semantic_key": "presence:speaker_old",
                "lifecycle_status": "open",
                "subject_referents": [
                    {"effective_entity_id": "speaker_old", "observed_surfaces": ["old"]}
                ],
                "counterpart_referents": [],
                "valid_from_block_id": "bk_ch08_b001",
                "valid_to_block_id": None,
            },
            {
                "state_id": "state_closed",
                "state_domain": "presence",
                "state_value": "dormant state",
                "semantic_key": "presence:dormant_extra",
                "lifecycle_status": "closed",
                "subject_referents": [
                    {"effective_entity_id": "dormant_extra"}
                ],
                "counterpart_referents": [],
                "valid_from_block_id": "bk_ch01_b001",
                "valid_to_block_id": "bk_ch02_b001",
            },
        ],
        "idiolect": [
            {"effective_entity_id": "speaker_old", "surface": "speaker_old"},
            {"effective_entity_id": "silent_core", "surface": "silent_core"},
        ],
        "narrative_position": {
            "capsules": [
                {"chapter_id": f"bk_ch{index:02d}", "chapter_order": index}
                for index in range(1, 9)
            ],
            "frames": [],
            "handoff": None,
        },
        "open_questions": {
            "pending_identity_cases": [
                {
                    "component_id": "identity_pending",
                    "card_ids": ["card:silent_core"],
                    "evidence": ["long evidence"],
                }
            ],
            "pending_states": [
                {"pending_case_id": "pending_state", "review_route": "temporal_review"}
            ],
            "unresolved_address": [
                {
                    "speaker_effective_entity_id": "speaker_old",
                    "speaker_surface": "speaker_old",
                    "addressee_effective_entity_id": "current_new",
                    "addressee_surface": "current_new",
                    "evidence_ref": "address_decided",
                },
                {
                    "speaker_effective_entity_id": "current_new",
                    "speaker_surface": "current_new",
                    "addressee_effective_entity_id": "speaker_old",
                    "addressee_surface": "speaker_old",
                    "evidence_ref": "address_unanchored",
                },
            ],
            "contested_relations": [{"relation_edge_id": "contested_1"}],
            "unknowable_windows": [{"window_id": "unknown_1"}],
        },
        "lineage": {"large": ["audit"] * 20},
        "memory_budget": {},
        "provider_calls": 0,
    }
    return _seal(body)


def _endpoint(entity_id: str) -> dict:
    return {
        "surface": entity_id,
        "candidate_card_ids": [f"card:{entity_id}"],
        "effective_entity_ids": [entity_id],
        "resolution_status": "resolved_candidate",
        "resolved_to_effective_entity": True,
        "unresolved": False,
    }


def _pair_id(speaker: str, addressee: str) -> str:
    return canonical_hash(
        {
            "speaker_effective_entity_id": speaker,
            "addressee_effective_entity_id": addressee,
        }
    )[:24]


def _window(order: int) -> dict:
    speaker, addressee = (
        ("speaker_old", "current_new")
        if order == 1
        else ("current_new", "speaker_old")
    )
    pair_id = _pair_id(speaker, addressee)
    block_id = f"bk_ch08_b00{order}"
    turn_id = f"turn_{order}"
    body = {
        "schema_version": "literary_b4_window_slice_v1",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch08",
        "window_id": f"window_{order}",
        "window_order": order,
        "window_plan_hash": "2" * 64,
        "active_block_ids": [block_id],
        "preceding_tail_block_ids": [],
        "estimated_active_source_tokens": 20,
        "speaker_turns": [
            {
                "speaker_turn_id": turn_id,
                "block_id": block_id,
                "chapter_id": "bk_ch08",
                "chapter_order": 8,
                "frame_segment_id": "frame_1",
                "speaker": _endpoint(speaker),
                "addressee": _endpoint(addressee),
                "address_terms": [],
                "register_cue": "neutral",
                "register_cue_raw": None,
                "delivery_tone": None,
                "utterance_anchor": "Speak.",
                "window_membership": "active",
                "established_in_chapter": "bk_ch08",
            }
        ],
        "address_pairs": [
            {
                "pair_id": pair_id,
                "unanchored": False,
                "speaker_effective_entity_id": speaker,
                "addressee_effective_entity_id": addressee,
                "turn_ids": [turn_id],
                "source_block_ids": [block_id],
            }
        ],
        "lineage": {},
        "provider_calls": 0,
    }
    return _seal(body)


def _anchor(story: dict) -> dict:
    pair_ids = [
        _pair_id("speaker_old", "current_new"),
        _pair_id("current_new", "speaker_old"),
    ]
    body = {
        "schema_version": "literary_b4_address_anchor_artifact_v3",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch08",
        "style_profile_version": "profile_v1",
        "measured_arm": False,
        "story_bible_artifact_hash": story["artifact_hash"],
        "anchor_input_artifact_hash": "4" * 64,
        "request_fingerprint": "5" * 64,
        "pair_decisions": [
            {
                "pair_id": pair_ids[0],
                "pronoun_pair": {"speaker": "toi", "addressee": "ong"},
                "vocative_options": [],
                "register_shifts": [],
                "evidence_refs": ["bk_ch08_b001"],
                "model_confidence": "medium",
                "not_anchored": None,
            },
            {
                "pair_id": pair_ids[1],
                "pronoun_pair": None,
                "vocative_options": [],
                "register_shifts": [],
                "evidence_refs": [],
                "model_confidence": "low",
                "not_anchored": {"reason": "insufficient evidence"},
            },
        ],
        "review_issues": [],
        "normalization_observations": [],
        "provider_called": True,
        "provider_receipt": {"receipt_id": "fixture"},
        "translation_performed": False,
        "semantic_record_mutation_performed": False,
    }
    return _seal(body)


def _project():
    story = _story()
    anchor = _anchor(story)
    windows = [_window(1), _window(2)]
    return (
        project_translator_pack_v1(
            story_bible=story,
            address_anchor=anchor,
            window_slices=windows,
        ),
        story,
        anchor,
        windows,
    )


def _project_tiered():
    story = _story()
    anchor = _anchor(story)
    windows = [_window(1), _window(2)]
    return (
        project_translator_pack_tiered_v2(
            story_bible=story,
            address_anchor=anchor,
            window_slices=windows,
        ),
        story,
        anchor,
        windows,
    )


def _sealed_pack(projected, *, budget: int = 20_000) -> dict:
    return seal_translator_pack_v1(
        projected=projected,
        budget_report={
            "translator_cap_tokens": 64_000,
            "headroom_tokens": 4_000,
            "fixed_prompt_upper_bound_tokens": 20_000,
            "pack_budget_tokens": budget,
            "pack_estimated_tokens": 5_000,
            "max_full_prompt_upper_bound_tokens": 25_000,
            "safety_multiplier": 1.25,
            "calibration_artifact_hash": "6" * 64,
        },
    )


def test_t27_chapter_speaker_and_silent_core_cast_are_kept() -> None:
    projected, _, _, _ = _project()
    assert "speaker_old" in projected.relevant_entity_ids
    assert "silent_core" in projected.relevant_entity_ids
    chapter_present_only = {"speaker_old", "current_new"}
    assert "silent_core" not in chapter_present_only


def test_t28_dormant_out_of_scope_entity_is_recorded_as_omitted() -> None:
    projected, _, _, _ = _project()
    assert "dormant_extra" not in projected.relevant_entity_ids
    assert {
        "section": "entities",
        "record_id": "dormant_extra",
        "reason_code": "out_of_chapter_scope",
    } in projected.omissions


def test_t29_other_kin_relation_retains_its_note() -> None:
    projected, _, _, _ = _project()
    row = next(
        row for row in projected.body["relations"] if row["relation"] == "other_kin"
    )
    assert row["relation_note"] == "related by marriage"


def test_t30_answered_address_is_dropped_and_not_anchored_flag_is_kept() -> None:
    projected, _, _, _ = _project()
    rows = projected.body["open_questions"]["unresolved_address"]
    assert rows == [
        {
            "speaker_effective_entity_id": "current_new",
            "speaker_surface": "current_new",
            "addressee_effective_entity_id": "speaker_old",
            "addressee_surface": "speaker_old",
            "unresolved": True,
        }
    ]
    assert any(
        row["reason_code"] == "answered_by_anchor"
        for row in projected.omissions
    )


def test_t31_budget_overflow_halts_without_semantic_trimming() -> None:
    projected, _, _, _ = _project()
    with pytest.raises(B4TranslatorPackError) as caught:
        seal_translator_pack_v1(
            projected=projected,
            budget_report={
                "translator_cap_tokens": 10_000,
                "headroom_tokens": 4_000,
                "fixed_prompt_upper_bound_tokens": 7_000,
                "pack_budget_tokens": -1_000,
                "pack_estimated_tokens": 5_000,
                "max_full_prompt_upper_bound_tokens": 12_000,
                "safety_multiplier": 1.25,
                "calibration_artifact_hash": "7" * 64,
            },
        )
    report = caught.value.report
    assert report is not None
    assert report["entities"] == projected.body["entities"]
    assert report["pack_budget"]["omitted_by_reason"]["budget_exceeded"] == 1


def test_t32_filtered_sections_have_exact_accounting() -> None:
    projected, _, _, _ = _project()
    filtered_reason = {
        "entities": "out_of_chapter_scope",
        "relations": "out_of_chapter_scope",
        "states": "out_of_chapter_scope",
        "idiolect": "out_of_chapter_scope",
        "open_questions.unresolved_address": "answered_by_anchor",
    }
    for section, reason in filtered_reason.items():
        omitted = sum(
            row["section"] == section and row["reason_code"] == reason
            for row in projected.omissions
        )
        assert projected.kept_counts[section] + omitted == projected.source_counts[
            section
        ]


def test_t33_pack_prefix_is_byte_identical_across_chapter_windows() -> None:
    projected, _, anchor, windows = _project()
    pack = _sealed_pack(projected)
    chapter = {
        "chapter_id": "bk_ch08",
        "blocks": [
            {"block_id": "bk_ch08_b001", "clean_text": "Source one."},
            {"block_id": "bk_ch08_b002", "clean_text": "Source two."},
        ],
    }
    rendered = [
        render_translation_window_request_v1(
            style_profile="- Prompt version: profile_v1.",
            style_profile_version="profile_v1",
            measured_arm=False,
            translator_pack_bytes=(
                json.dumps(pack, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8"),
            address_anchor_bytes=(
                json.dumps(anchor, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8"),
            window_slice_bytes=(
                json.dumps(window, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8"),
            chapter=chapter,
            accepted_tail_translations={},
        )
        for window in windows
    ]
    assert assert_stable_prefixes_v1(rendered) == rendered[0].stable_prefix_sha256


def test_tiered_v2_keeps_full_chapter_context_and_silent_core_identity() -> None:
    projected, _, _, _ = _project_tiered()
    entities = {
        row["effective_entity_id"]: row for row in projected.body["entities"]
    }

    assert entities["speaker_old"]["memory_tier"] == "chapter_context"
    assert entities["speaker_old"]["claims"]
    assert entities["current_new"]["memory_tier"] == "chapter_context"
    assert entities["silent_core"] == {
        "effective_entity_id": "silent_core",
        "canonical_surface": "silent_core",
        "aliases": [],
        "stable_surfaces": ["silent_core"],
        "claims": {"gender": {"value": "unknown"}},
        "referent_kind": "person",
        "memory_tier": "core_identity",
    }
    assert "dormant_extra" not in entities


def test_tiered_v2_scopes_relations_states_and_open_questions_structurally() -> None:
    projected, _, _, _ = _project_tiered()

    assert [row["relation"] for row in projected.body["relations"]] == [
        "other_kin"
    ]
    assert [row["semantic_key"] for row in projected.body["states"]] == [
        "presence:speaker_old"
    ]
    open_questions = projected.body["open_questions"]
    assert open_questions["pending_identity_cases"] == []
    assert open_questions["pending_states"] == [
        {
            "pending_case_id": "pending_state",
            "review_route": "temporal_review",
            "unresolved": True,
        }
    ]
    assert open_questions["unresolved_address"] == [
        {
            "speaker_effective_entity_id": "current_new",
            "speaker_surface": "current_new",
            "addressee_effective_entity_id": "speaker_old",
            "addressee_surface": "speaker_old",
            "unresolved": True,
        }
    ]
    assert {
        "section": "open_questions.pending_identity_cases",
        "record_id": "identity_pending",
        "reason_code": "out_of_chapter_scope",
    } in projected.omissions


def test_tiered_v2_keeps_a_silent_relation_counterpart_as_a_capsule() -> None:
    story_body = deepcopy(_story())
    story_body.pop("artifact_hash")
    story_body["relations"].append(
        {
            "relation_edge_id": "relation_to_silent_core",
            "source_effective_entity_id": "current_new",
            "target_effective_entity_id": "silent_core",
            "relation": "associated_with",
            "relation_family": "entity_link",
            "relation_note": "current context reaches a silent core entity",
            "structurally_contested": False,
            "effective": True,
        }
    )
    story = _seal(story_body)
    anchor = _anchor(story)
    projected = project_translator_pack_tiered_v2(
        story_bible=story,
        address_anchor=anchor,
        window_slices=[_window(1), _window(2)],
    )
    entities = {
        row["effective_entity_id"]: row for row in projected.body["entities"]
    }

    assert entities["silent_core"]["memory_tier"] == "core_identity"
    assert entities["silent_core"]["claims"] == {
        "gender": {"value": "unknown"}
    }
    assert any(
        row["target_effective_entity_id"] == "silent_core"
        for row in projected.body["relations"]
    )
    assert projected.body["open_questions"]["pending_identity_cases"] == []


def test_tiered_v2_capsule_keeps_only_translation_relevant_claim_values() -> None:
    story_body = deepcopy(_story())
    story_body.pop("artifact_hash")
    silent_core = next(
        row
        for row in story_body["entities"]
        if row["effective_entity_id"] == "silent_core"
    )
    silent_core["claims"] = {
        "gender": {"value": "feminine", "evidence_ref": "ev:gender"},
        "life_stage": {"value": "adult", "evidence_ref": "ev:age"},
        "role_or_occupation": {
            "value": "housekeeper",
            "evidence_ref": "ev:role",
        },
        "presence_basis": {
            "value": "directly_present",
            "evidence_ref": "ev:presence",
        },
    }
    story = _seal(story_body)
    projected = project_translator_pack_tiered_v2(
        story_bible=story,
        address_anchor=_anchor(story),
        window_slices=[_window(1), _window(2)],
    )
    capsule = next(
        row
        for row in projected.body["entities"]
        if row["effective_entity_id"] == "silent_core"
    )

    assert capsule["claims"] == {
        "gender": {"value": "feminine"},
        "life_stage": {"value": "adult"},
        "role_or_occupation": {"value": "housekeeper"},
    }


def test_tiered_v2_columnar_prompt_preserves_retained_entity_fields() -> None:
    projected, _, _, _ = _project_tiered()
    pack = _sealed_pack(projected)
    prompt_view = translator_pack_prompt_view_v1(pack)
    table = prompt_view["entities"]
    rows = {
        row[0]: dict(zip(table["columns"], row, strict=True))
        for row in table["rows"]
    }

    assert prompt_view["schema_version"] == "literary_b4_translator_pack_prompt_v2"
    assert rows["speaker_old"]["aliases"] == []
    assert rows["speaker_old"]["stable_surfaces"] == ["speaker_old"]
    assert rows["speaker_old"]["claims"] == {
        "gender": {"value": "unknown", "evidence_ref": "ev:speaker_old"}
    }
    assert rows["silent_core"]["memory_tier"] == "core_identity"
    assert rows["silent_core"]["claims"] == {
        "gender": {"value": "unknown"}
    }
    assert rows["silent_core"]["first_seen_chapter"] is None


def test_tiered_v2_columnar_prompt_is_smaller_at_chapter_scale() -> None:
    story_body = deepcopy(_story())
    story_body.pop("artifact_hash")
    story_body["entities"].extend(
        _entity(
            f"silent_core_{index}",
            first_chapter="bk_ch02",
            member_chapters=["bk_ch02", "bk_ch04"],
        )
        for index in range(20)
    )
    story = _seal(story_body)
    anchor = _anchor(story)
    windows = [_window(1), _window(2)]
    baseline = project_translator_pack_v1(
        story_bible=story,
        address_anchor=anchor,
        window_slices=windows,
    )
    tiered = project_translator_pack_tiered_v2(
        story_bible=story,
        address_anchor=anchor,
        window_slices=windows,
    )
    baseline_view = translator_pack_prompt_view_v1(_sealed_pack(baseline))
    tiered_view = translator_pack_prompt_view_v1(_sealed_pack(tiered))

    assert calibrated_token_estimate_v1(
        tiered_view, safety_multiplier=1.25
    ) < calibrated_token_estimate_v1(
        baseline_view, safety_multiplier=1.25
    )


def test_legacy_planning_window_pair_scope_is_migrated_without_source_mutation() -> None:
    source = _window(1)
    legacy = deepcopy(source)
    body = deepcopy(legacy)
    body.pop("artifact_hash")
    for pair in body["address_pairs"]:
        pair.pop("pair_id")
        pair.pop("unanchored")
    legacy = _seal(body)
    original = deepcopy(legacy)

    current, migrated, reports = _current_planning_window_slices_v1([legacy])

    assert legacy == original
    assert current == migrated
    assert current[0]["address_pairs"][0]["pair_id"] == _pair_id(
        "speaker_old", "current_new"
    )
    assert current[0]["address_pairs"][0]["unanchored"] is False
    assert reports[0]["source_artifact_hash"] == legacy["artifact_hash"]
    assert reports[0]["migrated_pair_count"] == 1


def test_legacy_planning_window_migration_rejects_a_tampered_source() -> None:
    legacy = _window(1)
    legacy["address_pairs"][0].pop("pair_id")
    legacy["address_pairs"][0].pop("unanchored")
    with pytest.raises(B4TranslatorPackError, match="window slice hash mismatch"):
        _current_planning_window_slices_v1([legacy])
