from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import unicodedata

from pipeline.literary import builder_schema_v3 as schema_v3
from pipeline.literary.builder_validators_v3 import (
    SHAPE_CONTRACTS,
    deepest_active_segment_at,
    validate_chapter_brief_v3,
    validate_digest_v3,
    validate_lexicon_v3,
    validate_narrative_v3,
    validate_shape_contract,
)
from pipeline.literary.source_anchor import (
    REFERENCE_FIELD_MAP,
    locate_anchor,
    nfc_block_string,
    remap_references,
)


def _blocks() -> list[dict[str, object]]:
    return [
        {
            "block_id": "bk_ch01_b001",
            "block_type": "paragraph",
            "order_index": 1,
            "clean_text": "Alice greeted Bob. Mira writes a letter.",
            "source_text": "Alice greeted Bob. Mira writes a letter.",
        },
        {
            "block_id": "bk_ch01_b002",
            "block_type": "dialogue",
            "order_index": 2,
            "clean_text": "Bob answered Alice. Ravel arrived.",
            "source_text": "Bob answered Alice. Ravel arrived.",
        },
        {
            "block_id": "bk_ch01_b003",
            "block_type": "paragraph",
            "order_index": 3,
            "clean_text": "The dog followed Bob.",
            "source_text": "The dog followed Bob.",
        },
    ]


def _brief(*, scenes: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "chapter_id": "bk_ch01",
        "cast_claims": [
            {
                "surface": "Alice",
                "surface_kind": "proper_name",
                "referent_kind_claim": "person",
                "role_hint": "visitor",
                "scene_range": ["bk_ch01_b001", "bk_ch01_b003"],
                "source_block_ids": ["bk_ch01_b001"],
                "anchor_text": "Alice",
                "evidence_quote": "Alice greeted Bob.",
            }
        ],
        "setting": {
            "place": "an unnamed house",
            "time_frame_hint": "frame_present",
            "scene_shape": "few_scenes",
        },
        "scenes_party_size": scenes
        if scenes is not None
        else [
            {
                "block_range": ["bk_ch01_b001", "bk_ch01_b003"],
                "co_present_count": 2,
                "participants": ["Alice", "Bob"],
            }
        ],
        "neutral_premise": "A visitor enters a household and an exchange begins.",
    }


def _lexicon() -> dict[str, object]:
    return {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001", "bk_ch01_b002", "bk_ch01_b003"],
        "context_only_used": False,
        "character_mentions": [
            {
                "surface": "Alice",
                "mention_type": "name",
                "referent_kind_claim": "person",
                "anchor_text": "Alice",
                "evidence_quote": "Alice greeted Bob.",
                "block_id": "bk_ch01_b001",
            },
            {
                "surface": "Bob",
                "mention_type": "name",
                "referent_kind_claim": "person",
                "anchor_text": "Bob",
                "evidence_quote": "Alice greeted Bob.",
                "block_id": "bk_ch01_b001",
            },
        ],
        "glossary_candidates": [
            {
                "source_term": "house",
                "proposed_target_vi": "ngoi nha",
                "category": "place",
                "do_not_translate": False,
                "block_ids": ["bk_ch01_b001"],
            }
        ],
    }


def _endpoint(
    surface: str,
    evidence_quote: str,
    mention_ref: str | None,
    *,
    scope: str = "individual",
    kind: str = "person",
    method: str = "explicit_tag",
) -> dict[str, object]:
    return {
        "surface": surface,
        "reference_scope": scope,
        "referent_kind_claim": kind,
        "mention_ref": mention_ref,
        "attribution_method": method,
        "anchor_text": surface,
        "evidence_quote": evidence_quote,
    }


def _narrative(mentions: list[dict[str, object]]) -> dict[str, object]:
    alice_id = str(mentions[0]["mention_id"])
    bob_id = str(mentions[1]["mention_id"])
    evidence = "Alice greeted Bob."
    return {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001", "bk_ch01_b002", "bk_ch01_b003"],
        "context_only_used": False,
        "speaker_turns": [
            {
                "speaker": _endpoint("Alice", evidence, alice_id),
                "addressee": _endpoint("Bob", evidence, bob_id),
                "utterance_quote": "Alice greeted Bob.",
                "address_terms": [],
                "register_cue": "neutral",
                "block_id": "bk_ch01_b001",
            }
        ],
        "relation_events": [
            {
                "actor": _endpoint("Alice", evidence, alice_id),
                "target": _endpoint("Bob", evidence, bob_id),
                "event_type": "greets",
                "evidence_quote": evidence,
                "block_id": "bk_ch01_b001",
            }
        ],
    }


def _digest(
    *,
    endpoints: list[str] | None = None,
    events: list[str] | None = None,
    frames: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "chapter_id": "bk_ch01",
        "chapter_rolling_summary": "A visitor meets the household.",
        "narration_frame_segments": frames
        if frames is not None
        else [
            {
                "local_segment_key": "present",
                "parent_local_key": None,
                "narrator_surface": "Narrator",
                "frame_kind": "primary_narration",
                "story_time_label": "frame_present",
                "block_range": ["bk_ch01_b001", "bk_ch01_b003"],
                "start_boundary": None,
                "end_boundary": None,
                "status": "proposed",
                "evidence_quote": "Alice greeted Bob.",
            }
        ],
        "relation_observations": []
        if not endpoints or not events
        else [
            {
                "event_id": events[0],
                "endpoint_refs": endpoints[:2],
                "observed_valence_hint": "positive",
                "block_id": "bk_ch01_b001",
                "evidence_quote": "Alice greeted Bob.",
            }
        ],
        "character_state_changes": [],
        "unresolved_threads": [],
        "translator_relevant_facts": [],
    }


def _narrative_result() -> tuple[list[dict[str, object]], dict[str, object]]:
    lexicon = validate_lexicon_v3(_lexicon(), blocks=_blocks())
    assert lexicon.report.ok, lexicon.report.to_dict()
    narrative = validate_narrative_v3(
        _narrative(lexicon.payload["character_mentions"]),
        blocks=_blocks(),
        mentions=lexicon.payload["character_mentions"],
    )
    assert narrative.report.ok, narrative.report.to_dict()
    return lexicon.payload["character_mentions"], narrative.payload


def test_happy_path_per_stage_and_deterministic_code_mint() -> None:
    brief = validate_chapter_brief_v3(_brief(), blocks=_blocks())
    assert brief.report.ok, brief.report.to_dict()
    assert brief.payload["cast_claims"][0]["cast_claim_id"] == "cc_bk_ch01_01"

    first = validate_lexicon_v3(_lexicon(), blocks=_blocks())
    second = validate_lexicon_v3(_lexicon(), blocks=_blocks())
    assert first.report.ok and second.report.ok
    assert first.payload == second.payload

    narrative_first = validate_narrative_v3(
        _narrative(first.payload["character_mentions"]),
        blocks=_blocks(),
        mentions=first.payload["character_mentions"],
    )
    narrative_second = validate_narrative_v3(
        _narrative(second.payload["character_mentions"]),
        blocks=_blocks(),
        mentions=second.payload["character_mentions"],
    )
    assert narrative_first.report.ok and narrative_second.report.ok
    assert narrative_first.payload == narrative_second.payload

    endpoints = [
        narrative_first.payload["speaker_turns"][0]["speaker"]["endpoint_id"],
        narrative_first.payload["speaker_turns"][0]["addressee"]["endpoint_id"],
    ]
    digest = validate_digest_v3(
        _digest(endpoints=endpoints, events=[narrative_first.payload["relation_events"][0]["event_id"]]),
        blocks=_blocks(),
        mention_ids=[row["mention_id"] for row in first.payload["character_mentions"]],
        endpoint_ids=endpoints,
        event_ids=[narrative_first.payload["relation_events"][0]["event_id"]],
    )
    assert digest.report.ok, digest.report.to_dict()


def test_locator_fixtures_are_fail_closed_or_disambiguated() -> None:
    ambiguous = {
        "block_id": "b1",
        "clean_text": "Mira spoke. Mira spoke.",
        "source_text": "Mira spoke. Mira spoke.",
    }
    assert not locate_anchor(ambiguous, anchor_text="Mira", evidence_quote="Mira spoke.").ok

    unique_inside = {
        "block_id": "b2",
        "clean_text": "Mira left. Mira returned.",
        "source_text": "Mira left. Mira returned.",
    }
    result = locate_anchor(unique_inside, anchor_text="Mira", evidence_quote="Mira returned.")
    assert result.ok and result.anchor is not None
    assert result.anchor.char_start == unique_inside["clean_text"].rfind("Mira")

    hinted = {
        "block_id": "b3",
        "clean_text": "Mira greeted Mira.",
        "source_text": "Mira greeted Mira.",
    }
    result = locate_anchor(
        hinted,
        anchor_text="Mira",
        evidence_quote="Mira greeted Mira.",
        occurrence_hint=2,
    )
    assert result.ok and result.anchor is not None
    assert result.anchor.char_start == hinted["clean_text"].rfind("Mira")


def test_mention_ids_are_block_global_not_per_surface() -> None:
    result = validate_lexicon_v3(_lexicon(), blocks=_blocks())
    assert result.report.ok
    assert [row["mention_id"] for row in result.payload["character_mentions"]] == [
        "m_bk_ch01_b001_01",
        "m_bk_ch01_b001_02",
    ]


def test_same_anchor_turn_event_uses_deterministic_local_tiebreak() -> None:
    mentions, narrative_payload = _narrative_result()
    result = validate_narrative_v3(
        _narrative(mentions),
        blocks=_blocks(),
        mentions=mentions,
    )
    assert result.report.ok
    turn = result.payload["speaker_turns"][0]
    event = result.payload["relation_events"][0]
    assert turn["turn_id"] == "t_bk_ch01_b001_01"
    assert event["event_id"] == "e_bk_ch01_b001_01"
    assert turn["position_key"][-1] == 1
    assert event["position_key"][-1] == 2
    assert narrative_payload["speaker_turns"][0]["turn_id"] == turn["turn_id"]


def test_mention_ref_must_resolve_inside_the_current_window() -> None:
    mentions, _ = _narrative_result()
    cross_window_mentions = deepcopy(mentions)
    cross_window_mentions[0]["block_id"] = "bk_ch01_b002"
    payload = _narrative(mentions)
    payload["window_block_ids"] = ["bk_ch01_b001"]
    result = validate_narrative_v3(
        payload,
        blocks=_blocks()[:1],
        mentions=cross_window_mentions,
    )
    assert result.report.ok, result.report.to_dict()
    assert result.report.counts["dropped_unresolved_mention_ref"] >= 1
    assert result.payload["speaker_turns"] == []


def test_scene_overlap_and_gap_are_fatal() -> None:
    overlap = validate_chapter_brief_v3(
        _brief(
            scenes=[
                {"block_range": ["bk_ch01_b001", "bk_ch01_b002"], "co_present_count": 1, "participants": []},
                {"block_range": ["bk_ch01_b002", "bk_ch01_b003"], "co_present_count": 1, "participants": []},
            ]
        ),
        blocks=_blocks(),
    )
    assert not overlap.report.ok
    assert overlap.report.counts["scene_overlap"] == 1

    gap = validate_chapter_brief_v3(
        _brief(
            scenes=[
                {"block_range": ["bk_ch01_b001", "bk_ch01_b001"], "co_present_count": 1, "participants": []},
                {"block_range": ["bk_ch01_b003", "bk_ch01_b003"], "co_present_count": 1, "participants": []},
            ]
        ),
        blocks=_blocks(),
    )
    assert not gap.report.ok
    assert gap.report.counts["scene_gap"] == 1


def test_full_two_axis_table_routes_and_flags_exactly() -> None:
    cases = [
        ("individual", "person", "eligible", 0),
        ("individual", "animal", "route_out", 0),
        ("individual", "place", "invalid", 1),
        ("group", "group_reference", "route_out", 0),
        ("group", "person", "invalid", 1),
        ("group", "unknown", "deferred", 0),
        ("narrator", "person", "discourse_only", 0),
        ("narrator", "animal", "invalid", 1),
        ("reader", "unknown", "discourse_only", 0),
        ("unknown", "person", "deferred", 0),
    ]
    for scope, kind, expected_eligibility, expected_invalid in cases:
        payload = {
            "chapter_id": "bk_ch01",
            "window_block_ids": ["bk_ch01_b001"],
            "context_only_used": False,
            "speaker_turns": [
                {
                    "speaker": _endpoint(
                        "Alice",
                        "Alice greeted Bob.",
                        None,
                        scope=scope,
                        kind=kind,
                        method="nearby_context",
                    ),
                    "addressee": _endpoint("Bob", "Alice greeted Bob.", None),
                    "utterance_quote": "Alice greeted Bob.",
                    "address_terms": [],
                    "register_cue": "neutral",
                    "block_id": "bk_ch01_b001",
                }
            ],
            "relation_events": [],
        }
        result = validate_narrative_v3(payload, blocks=_blocks()[:1])
        assert result.report.ok, (scope, kind, result.report.to_dict())
        speaker = result.payload["speaker_turns"][0]["speaker"]
        assert speaker["runtime_eligibility"] == expected_eligibility
        assert result.report.counts.get("flag_invalid_two_axis", 0) == expected_invalid
        assert speaker["attribution_method"] == "nearby_context"


def test_phase_label_cannot_leak_into_event_type() -> None:
    mentions, _ = _narrative_result()
    payload = _narrative(mentions)
    payload["relation_events"][0]["event_type"] = "enemy"
    result = validate_narrative_v3(payload, blocks=_blocks(), mentions=mentions)
    assert not result.report.ok
    assert result.report.counts["phase_leak"] == 1
    assert result.payload["relation_events"] == []


def test_nested_frame_tree_records_deepest_active_leaf() -> None:
    frames = [
        {
            "local_segment_key": "outer",
            "parent_local_key": None,
            "narrator_surface": "Narrator",
            "frame_kind": "primary_narration",
            "story_time_label": "frame_present",
            "block_range": ["bk_ch01_b001", "bk_ch01_b003"],
            "start_boundary": None,
            "end_boundary": None,
            "status": "proposed",
            "evidence_quote": "Alice greeted Bob.",
        },
        {
            "local_segment_key": "letter",
            "parent_local_key": "outer",
            "narrator_surface": "Mira",
            "frame_kind": "letter",
            "story_time_label": "retrospective_past",
            "block_range": ["bk_ch01_b002", "bk_ch01_b002"],
            "start_boundary": None,
            "end_boundary": None,
            "status": "proposed",
            "evidence_quote": "Bob answered Alice.",
        },
    ]
    result = validate_digest_v3(_digest(frames=frames), blocks=_blocks())
    assert result.report.ok, result.report.to_dict()
    assert result.payload["deepest_active_leaf_by_block"] == {
        "bk_ch01_b001": "outer",
        "bk_ch01_b002": "letter",
        "bk_ch01_b003": "outer",
    }


def test_frame_tree_rejects_missing_parent_overlap_gap_and_cycle() -> None:
    base = _digest()["narration_frame_segments"][0]
    missing_parent = deepcopy(base)
    missing_parent["parent_local_key"] = "absent"
    result = validate_digest_v3(_digest(frames=[missing_parent]), blocks=_blocks())
    assert not result.report.ok
    assert result.report.counts["frame_missing_parent"] == 1
    assert result.report.counts["frame_cycle"] == 0

    outer = deepcopy(base)
    outer["local_segment_key"] = "outer"
    left = deepcopy(base)
    left.update({"local_segment_key": "left", "parent_local_key": "outer", "block_range": ["bk_ch01_b001", "bk_ch01_b002"]})
    right = deepcopy(base)
    right.update({"local_segment_key": "right", "parent_local_key": "outer", "block_range": ["bk_ch01_b002", "bk_ch01_b003"]})
    result = validate_digest_v3(_digest(frames=[outer, left, right]), blocks=_blocks())
    assert not result.report.ok
    assert result.report.counts["frame_sibling_overlap"] == 1

    gap = deepcopy(base)
    gap["block_range"] = ["bk_ch01_b001", "bk_ch01_b002"]
    result = validate_digest_v3(_digest(frames=[gap]), blocks=_blocks())
    assert not result.report.ok
    assert result.report.counts["frame_leaf_gap"] == 1

    a = deepcopy(base)
    a.update({"local_segment_key": "a", "parent_local_key": "b"})
    b = deepcopy(base)
    b.update({"local_segment_key": "b", "parent_local_key": "a"})
    result = validate_digest_v3(_digest(frames=[a, b]), blocks=_blocks())
    assert not result.report.ok
    assert result.report.counts["frame_cycle"] == 1


def test_mid_block_frame_boundary_is_located_or_fails_closed() -> None:
    outer = _digest()["narration_frame_segments"][0]
    outer["block_range"] = ["bk_ch01_b001", "bk_ch01_b001"]
    child = deepcopy(outer)
    child.update(
        {
            "local_segment_key": "letter",
            "parent_local_key": "present",
            "frame_kind": "letter",
            "story_time_label": "retrospective_past",
        }
    )
    child["start_boundary"] = {
        "anchor_text": "Mira",
        "evidence_quote": "Mira writes a letter.",
    }
    result = validate_digest_v3(_digest(frames=[outer, child]), blocks=_blocks()[:1])
    assert result.report.ok, result.report.to_dict()
    letter = next(
        frame
        for frame in result.payload["narration_frame_segments"]
        if frame["local_segment_key"] == "letter"
    )
    assert letter["start_anchor"]["char_start"] > 0
    assert result.payload["deepest_active_leaf_spans"][-1]["segment_key"] == "letter"
    assert (
        deepest_active_segment_at(
            result.payload,
            block_id="bk_ch01_b001",
            char_offset=letter["start_anchor"]["char_start"],
        )
        == "letter"
    )

    broken = deepcopy(child)
    broken["start_boundary"]["anchor_text"] = "Absent"
    result = validate_digest_v3(_digest(frames=[outer, broken]), blocks=_blocks()[:1])
    assert not result.report.ok
    assert result.report.counts["fail_closed_locate"] == 1

    not_midblock = deepcopy(child)
    not_midblock["start_boundary"] = {
        "anchor_text": "Alice",
        "evidence_quote": "Alice greeted Bob.",
    }
    result = validate_digest_v3(_digest(frames=[outer, not_midblock]), blocks=_blocks()[:1])
    assert not result.report.ok
    assert result.report.counts["dropped_non_midblock_boundary"] == 1


def test_mid_block_siblings_use_source_intervals_for_overlap_and_coverage() -> None:
    block = {
        "block_id": "bk_ch01_b001",
        "block_type": "paragraph",
        "order_index": 1,
        "clean_text": "AlphaBetaGamma",
        "source_text": "AlphaBetaGamma",
    }
    base = _digest()["narration_frame_segments"][0]
    base["block_range"] = ["bk_ch01_b001", "bk_ch01_b001"]
    first = deepcopy(base)
    first.update(
        {
            "local_segment_key": "first",
            "end_boundary": {"anchor_text": "Alpha", "evidence_quote": "AlphaBetaGamma"},
        }
    )
    second = deepcopy(base)
    second.update(
        {
            "local_segment_key": "second",
            "frame_kind": "dream",
            "story_time_label": "retrospective_past",
            "start_boundary": {"anchor_text": "Beta", "evidence_quote": "AlphaBetaGamma"},
        }
    )
    result = validate_digest_v3(_digest(frames=[first, second]), blocks=[block])
    assert result.report.ok, result.report.to_dict()
    assert [span["segment_key"] for span in result.payload["deepest_active_leaf_spans"]] == [
        "first",
        "second",
    ]

    overlapping = deepcopy(first)
    overlapping["end_boundary"] = {
        "anchor_text": "Beta",
        "evidence_quote": "AlphaBetaGamma",
    }
    result = validate_digest_v3(_digest(frames=[overlapping, second]), blocks=[block])
    assert not result.report.ok
    assert result.report.counts["frame_sibling_overlap"] == 1


def test_digest_rejects_clean_surfaces_and_entity_ids_as_occurrence_refs() -> None:
    state = {
        "subject_ref": "Heathcliff",
        "attribute": "residence",
        "from_value": "here",
        "to_value": "there",
        "trigger_ref": "bk_ch01_b001",
        "evidence_quote": "Alice greeted Bob.",
    }
    result = validate_digest_v3(
        {**_digest(), "character_state_changes": [state]},
        blocks=_blocks(),
    )
    assert not result.report.ok
    assert result.payload["character_state_changes"] == []

    state["subject_ref"] = "ent_heathcliff"
    result = validate_digest_v3(
        {**_digest(), "character_state_changes": [state]},
        blocks=_blocks(),
    )
    assert not result.report.ok


def test_retired_fields_are_removed_from_returned_payload_without_mutating_input() -> None:
    payload = _lexicon()
    payload["confidence"] = "high"
    payload["character_mentions"][0]["resolution_status"] = "candidate"
    payload["character_mentions"][0]["candidate_entity_ids"] = ["ent_alice"]
    payload["character_mentions"][0]["termhood"] = "a name"
    payload["character_mentions"][0]["utterance_gist"] = "unused"
    original = deepcopy(payload)
    result = validate_lexicon_v3(payload, blocks=_blocks())
    assert result.report.ok, result.report.to_dict()
    assert payload == original
    serialized = repr(result.payload)
    for retired in ("confidence", "utterance_gist", "termhood", "resolution_status", "candidate_entity_ids"):
        assert retired not in serialized
        assert result.report.counts[f"retired_{retired}_stripped"] == 1


def test_nfc_coordinate_string_matches_model_render_contract() -> None:
    nfd = "Cafe\u0301 welcomes Mira."
    block = {
        "block_id": "bk_ch01_b001",
        "block_type": "paragraph",
        "clean_text": nfd,
        "source_text": nfd,
    }
    assert nfc_block_string(block) == unicodedata.normalize("NFC", nfd)
    located = locate_anchor(
        block,
        anchor_text="Caf\u00e9",
        evidence_quote="Caf\u00e9 welcomes Mira.",
    )
    assert located.ok and located.anchor is not None
    assert nfc_block_string(block)[located.anchor.char_start : located.anchor.char_end] == "Caf\u00e9"


def test_nonperson_event_is_routed_out_without_being_dropped() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b003"],
        "context_only_used": False,
        "speaker_turns": [],
        "relation_events": [
            {
                "actor": _endpoint("dog", "The dog followed Bob.", None, kind="animal"),
                "target": _endpoint("Bob", "The dog followed Bob.", None),
                "event_type": "follows",
                "evidence_quote": "The dog followed Bob.",
                "block_id": "bk_ch01_b003",
            }
        ],
    }
    result = validate_narrative_v3(payload, blocks=_blocks()[2:])
    assert result.report.ok, result.report.to_dict()
    assert result.payload["relation_events"][0]["runtime_eligibility"] == "route_out"
    assert result.report.counts["route_out_event"] == 1


def test_cast_claim_source_blocks_are_validated_against_scene_range() -> None:
    payload = _brief()
    claim = payload["cast_claims"][0]
    claim.update(
        {
            "surface": "Ravel",
            "anchor_text": "Ravel",
            "evidence_quote": "Ravel arrived.",
            "source_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
        }
    )
    result = validate_chapter_brief_v3(payload, blocks=_blocks())
    assert result.report.ok, result.report.to_dict()
    assert result.payload["cast_claims"][0]["source_block_ids"] == ["bk_ch01_b001", "bk_ch01_b002"]

    outside = deepcopy(payload)
    outside["cast_claims"][0]["source_block_ids"] = ["bk_ch01_b001", "bk_ch01_b004"]
    result = validate_chapter_brief_v3(outside, blocks=_blocks())
    assert not result.report.ok
    assert result.report.counts["cast_claim_source_outside_scene"] == 1


def test_cast_claim_surface_must_match_its_minted_anchor() -> None:
    payload = _brief()
    payload["cast_claims"][0]["anchor_text"] = "Bob"
    result = validate_chapter_brief_v3(payload, blocks=_blocks())
    assert not result.report.ok
    assert result.payload["cast_claims"] == []
    assert result.report.counts["dropped_surface_anchor_mismatch"] == 1


def _shape_samples() -> dict[str, dict[str, object]]:
    brief = _brief()
    lexicon = _lexicon()
    endpoint = _endpoint("Alice", "Alice greeted Bob.", None)
    turn = {
        "speaker": deepcopy(endpoint),
        "addressee": deepcopy(endpoint),
        "utterance_quote": "Alice greeted Bob.",
        "address_terms": [],
        "register_cue": "neutral",
        "block_id": "bk_ch01_b001",
    }
    event = {
        "actor": deepcopy(endpoint),
        "target": deepcopy(endpoint),
        "event_type": "greets",
        "evidence_quote": "Alice greeted Bob.",
        "block_id": "bk_ch01_b001",
    }
    digest = _digest()
    return {
        "ChapterBrief": deepcopy(brief),
        "Setting": deepcopy(brief["setting"]),
        "CastClaim": deepcopy(brief["cast_claims"][0]),
        "Scene": deepcopy(brief["scenes_party_size"][0]),
        "Lexicon": deepcopy(lexicon),
        "Mention": deepcopy(lexicon["character_mentions"][0]),
        "Glossary": deepcopy(lexicon["glossary_candidates"][0]),
        "Narrative": {
            "chapter_id": "bk_ch01",
            "window_block_ids": ["bk_ch01_b001"],
            "context_only_used": False,
            "speaker_turns": [deepcopy(turn)],
            "relation_events": [deepcopy(event)],
        },
        "Endpoint": deepcopy(endpoint),
        "Turn": turn,
        "AddressTerm": {
            "anchor_text": "Bob",
            "evidence_quote": "Alice greeted Bob.",
            "addressee_ref": "addressee",
        },
        "Event": event,
        "Digest": deepcopy(digest),
        "FrameSegment": deepcopy(digest["narration_frame_segments"][0]),
        "SourceBoundary": {
            "anchor_text": "Mira",
            "evidence_quote": "Mira writes a letter.",
        },
        "RelationObservation": {
            "event_id": "e1",
            "endpoint_refs": ["p1", "p2"],
            "observed_valence_hint": "unclear",
            "block_id": "bk_ch01_b001",
            "evidence_quote": "Alice greeted Bob.",
        },
        "TransitionHint": {"trigger_event_id": "e1", "note": "a change"},
        "StateChange": {
            "subject_ref": "p1",
            "attribute": "residence",
            "from_value": "outside",
            "to_value": "inside",
            "trigger_ref": "e1",
            "evidence_quote": "Alice greeted Bob.",
        },
        "Thread": {
            "thread_local_id": "thread_1",
            "description": "A question remains.",
            "opened_block": "bk_ch01_b001",
            "kind": "question",
        },
        "Fact": {
            "fact_type": "setting",
            "fact": "The scene is indoors.",
            "block_evidence": ["bk_ch01_b001"],
            "inference_basis": "stated",
        },
    }


def test_every_shape_rejects_each_missing_required_field() -> None:
    samples = _shape_samples()
    assert set(samples) == set(SHAPE_CONTRACTS)
    for shape_name, contract in SHAPE_CONTRACTS.items():
        assert validate_shape_contract(shape_name, samples[shape_name]) == []
        for field_name, field_contract in contract.items():
            if not field_contract.required:
                continue
            broken = deepcopy(samples[shape_name])
            broken.pop(field_name)
            errors = validate_shape_contract(shape_name, broken)
            assert errors, (shape_name, field_name)


def test_shape_contract_fields_cannot_drift_from_typed_dataclasses() -> None:
    type_names = {
        "ChapterBrief": "ChapterBrief",
        "Setting": "Setting",
        "CastClaim": "CastClaim",
        "Scene": "Scene",
        "Lexicon": "Lexicon",
        "Mention": "Mention",
        "Glossary": "Glossary",
        "Narrative": "Narrative",
        "Endpoint": "Endpoint",
        "Turn": "Turn",
        "AddressTerm": "AddressTerm",
        "Event": "Event",
        "Digest": "Digest",
        "FrameSegment": "FrameSegment",
        "SourceBoundary": "SourceBoundary",
        "RelationObservation": "RelationObservation",
        "TransitionHint": "TransitionHint",
        "StateChange": "StateChange",
        "Thread": "UnresolvedThread",
        "Fact": "TranslatorRelevantFact",
    }
    code_filled = {
        "ChapterBrief": {"input_max_order"},
        "CastClaim": {"cast_claim_id", "anchor", "evidence_max_order"},
        "Mention": {"mention_id", "anchor"},
        "Endpoint": {"endpoint_id", "anchor", "resolution_evidence", "runtime_eligibility"},
        "Turn": {"turn_id", "position_key"},
        "AddressTerm": {"address_occurrence_id", "anchor"},
        "Event": {"event_id", "position_key"},
        "FrameSegment": {"start_anchor", "end_anchor", "segment_id", "version", "source_interval"},
    }
    for shape_name, type_name in type_names.items():
        dataclass_fields = {field.name for field in fields(getattr(schema_v3, type_name))}
        model_fields = dataclass_fields - code_filled.get(shape_name, set())
        assert set(SHAPE_CONTRACTS[shape_name]) == model_fields, shape_name


def test_shape_contract_rejects_wrong_types_and_illegal_nulls() -> None:
    scene = deepcopy(_shape_samples()["Scene"])
    scene["co_present_count"] = "two"
    assert validate_shape_contract("Scene", scene)

    claim = deepcopy(_shape_samples()["CastClaim"])
    claim["role_hint"] = None
    assert validate_shape_contract("CastClaim", claim)

    endpoint = deepcopy(_shape_samples()["Endpoint"])
    endpoint["mention_ref"] = None
    assert validate_shape_contract("Endpoint", endpoint) == []


def test_stage_validators_actually_enforce_nested_shape_contracts() -> None:
    brief = _brief()
    brief["cast_claims"][0].pop("role_hint")
    assert not validate_chapter_brief_v3(brief, blocks=_blocks()).report.ok

    lexicon = _lexicon()
    lexicon["glossary_candidates"][0].pop("do_not_translate")
    assert not validate_lexicon_v3(lexicon, blocks=_blocks()).report.ok

    narrative = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "speaker_turns": [deepcopy(_shape_samples()["Turn"])],
        "relation_events": [],
    }
    narrative["speaker_turns"][0].pop("utterance_quote")
    assert not validate_narrative_v3(narrative, blocks=_blocks()[:1]).report.ok

    digest = _digest()
    fact = deepcopy(_shape_samples()["Fact"])
    fact.pop("fact")
    digest["translator_relevant_facts"] = [fact]
    assert not validate_digest_v3(digest, blocks=_blocks()).report.ok


def test_remap_covers_every_declared_reference_field() -> None:
    expected_fields = {
        "event_id",
        "trigger_event_id",
        "trigger_ref",
        "mention_ref",
        "subject_ref",
        "subject_refs",
        "endpoint_refs",
        "event_ids",
    }
    assert set(REFERENCE_FIELD_MAP) == expected_fields
    payload = {
        "relation_observations": [
            {
                "event_id": "old_e1",
                "endpoint_refs": ["old_p1", "old_p2"],
                "transition_hint": {"trigger_event_id": "old_e2", "note": "change"},
            }
        ],
        "character_state_changes": [
            {"subject_ref": "old_m1", "trigger_ref": "old_e1"}
        ],
        "unresolved_threads": [{"subject_refs": ["old_m1", "old_p1"]}],
        "translator_relevant_facts": [
            {"subject_ref": "old_p2", "event_ids": ["old_e1", "old_e2"]}
        ],
        "endpoint": {"mention_ref": "old_m1"},
    }
    remapped = remap_references(
        payload,
        mention_ids={"old_m1": "new_m1"},
        endpoint_ids={"old_p1": "new_p1", "old_p2": "new_p2"},
        event_ids={"old_e1": "new_e1", "old_e2": "new_e2"},
    )
    relation = remapped["relation_observations"][0]
    assert relation["event_id"] == "new_e1"
    assert relation["endpoint_refs"] == ["new_p1", "new_p2"]
    assert relation["transition_hint"]["trigger_event_id"] == "new_e2"
    assert remapped["character_state_changes"][0] == {
        "subject_ref": "new_m1",
        "trigger_ref": "new_e1",
    }
    assert remapped["unresolved_threads"][0]["subject_refs"] == ["new_m1", "new_p1"]
    assert remapped["translator_relevant_facts"][0] == {
        "subject_ref": "new_p2",
        "event_ids": ["new_e1", "new_e2"],
    }
    assert remapped["endpoint"]["mention_ref"] == "new_m1"


def test_transition_hint_requires_an_existing_event() -> None:
    relation = deepcopy(_shape_samples()["RelationObservation"])
    relation["transition_hint"] = {"trigger_event_id": "missing", "note": "change"}
    digest = _digest(endpoints=["p1", "p2"], events=["e1"])
    digest["relation_observations"] = [relation]
    result = validate_digest_v3(
        digest,
        blocks=_blocks(),
        endpoint_ids=["p1", "p2"],
        event_ids=["e1"],
    )
    assert not result.report.ok

    relation["transition_hint"]["trigger_event_id"] = "e1"
    digest["relation_observations"] = [relation]
    result = validate_digest_v3(
        digest,
        blocks=_blocks(),
        endpoint_ids=["p1", "p2"],
        event_ids=["e1"],
    )
    assert result.report.ok, result.report.to_dict()


def test_self_overlapping_surface_uses_non_overlapping_occurrences() -> None:
    block = {"block_id": "b1", "clean_text": "aaa", "source_text": "aaa"}
    result = locate_anchor(block, anchor_text="aa", evidence_quote="aaa")
    assert result.ok
    assert result.anchor is not None
    assert (result.anchor.char_start, result.anchor.char_end) == (0, 2)
