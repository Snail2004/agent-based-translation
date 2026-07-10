from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.builder_pilot import (
    _build_validation_retry_messages,
    built_in_fixture_payloads,
    build_chapter_brief_messages,
    build_dry_run_artifacts,
    build_digest_messages,
    build_literary_windows,
    build_lexicon_messages,
    build_narrative_messages,
    estimate_m2,
    estimate_m3,
    estimate_m1,
    load_system_prompt_for_chapter,
    load_system_prompt_from_design,
    load_wuthering_heights_epub,
    neighbor_summaries_for_index,
    render_chapter_brief_for_injection,
    render_neighbor_summaries,
    roster_from_ledger,
    safe_brief_neutral_premise,
    run_m3,
    seed_entity_ledger_from_chapter_brief,
    validate_builtin_fixtures,
    validate_chapter_brief,
    validate_digest,
    validate_lexicon,
    validate_narrative,
    validate_story_bible,
)
from pipeline.agents.llm_config import LLMConfig


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
PINNED_EPUB = (
    REPO_ROOT
    / "reference"
    / "literary"
    / "wuthering_heights"
    / "en"
    / "wuthering_heights_gutenberg_768_epub3_images.epub"
)


def test_l2a0_acceptance_fixtures_pass() -> None:
    reports = validate_builtin_fixtures()

    assert all(report.ok for report in reports), [report.to_dict() for report in reports]
    assert {report.name for report in reports} >= {
        "narrative",
        "story_bible",
        "fixture_group_not_person",
        "fixture_vocative_specific_person",
    }


def test_group_reference_kind_named_is_allowed_but_not_minted_person() -> None:
    fixtures = built_in_fixture_payloads()
    narrative = fixtures["group_addressee_narrative"]

    report = validate_narrative(
        narrative,
        valid_block_ids={"ch04_b012"},
        known_entity_ids={"ent_mr_earnshaw", "ent_mrs_earnshaw"},
    )
    story_report = validate_story_bible(fixtures["partial_story_bible"])

    assert report.ok
    assert story_report.ok
    assert narrative["speaker_turns"][0]["addressee"]["reference_kind"] == "group"
    assert narrative["speaker_turns"][0]["addressee"]["resolution_status"] == "named"
    assert all(
        entity["canonical"] != "the household"
        for entity in fixtures["partial_story_bible"]["registry_T2_entities"]
    )


def test_vocative_wife_targets_specific_person() -> None:
    fixtures = built_in_fixture_payloads()
    turn = fixtures["vocative_narrative"]["speaker_turns"][0]

    assert turn["address_term_used"] == "wife"
    assert turn["addressee"]["reference_kind"] == "person"
    assert turn["addressee"]["candidate_entity_ids"] == ["ent_mrs_earnshaw"]


def test_partial_story_bible_declares_open_scope() -> None:
    story = built_in_fixture_payloads()["partial_story_bible"]
    report = validate_story_bible(story)

    assert report.ok
    assert story["scope"] == "ch1-4"
    assert story["status"] == "partial_story_bible"
    assert story["entity_relations"][0]["valid_to_block"] is None
    assert story["entity_relations"][0]["status"] == "open_within_scope"


def test_lexicon_validator_drops_pronoun_without_retrying_window() -> None:
    payload = {
        "chapter_id": "wh_ch01",
        "window_block_ids": ["ch01_b001"],
        "context_only_used": False,
        "glossary_candidates": [],
        "character_mentions": [
            {
                "mention_id": "m_ch01_b001_01",
                "surface": "he",
                "mention_type": "name",
                "resolution_status": "candidate",
                "candidate_entity_ids": [],
                "block_ids": ["ch01_b001"],
            }
        ],
    }

    report = validate_lexicon(payload, valid_block_ids={"ch01_b001"})

    assert report.ok
    assert report.counts["pronoun_dropped"] == 1
    assert report.counts["mentions"] == 0
    assert payload["character_mentions"] == []
    assert any("plain pronoun" in warning for warning in report.warnings)


def test_lexicon_validator_normalizes_named_candidate_ids() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "glossary_candidates": [],
        "character_mentions": [
            {
                "mention_id": "m_bk_ch01_b001_01",
                "surface": "Mr. Alden",
                "mention_type": "name",
                "resolution_status": "named",
                "candidate_entity_ids": ["ent_alden"],
                "block_ids": ["bk_ch01_b001"],
            }
        ],
    }

    report = validate_lexicon(
        payload,
        valid_block_ids={"bk_ch01_b001"},
        chapter_block_ids={"bk_ch01_b001"},
        known_entity_ids={"ent_alden"},
    )

    assert report.ok
    assert report.counts["mention_named_ids_cleared"] == 1
    assert payload["character_mentions"][0]["candidate_entity_ids"] == []
    assert any("named surface is authoritative" in warning for warning in report.warnings)


def test_lexicon_validator_drops_outside_window_entries_by_kind() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b002"],
        "context_only_used": True,
        "glossary_candidates": [
            {
                "source_term": "Blackmoor",
                "proposed_target_vi": "Blackmoor",
                "category": "place",
                "do_not_translate": True,
                "termhood": "proper_name",
                "block_ids": ["bk_ch01_b003"],
            },
            {
                "source_term": "Raven Hall",
                "proposed_target_vi": "Raven Hall",
                "category": "place",
                "do_not_translate": True,
                "termhood": "proper_name",
                "block_ids": ["bk_ch01_b002", "bk_ch01_b003"],
            }
        ],
        "character_mentions": [
            {
                "mention_id": "m_bk_ch01_b001_01",
                "surface": "Mira",
                "mention_type": "name",
                "resolution_status": "named",
                "candidate_entity_ids": [],
                "block_ids": ["bk_ch01_b001"],
            },
            {
                "mention_id": "m_bk_ch01_b002_02",
                "surface": "Bram",
                "mention_type": "name",
                "resolution_status": "named",
                "candidate_entity_ids": [],
                "block_ids": ["bk_ch01_b002", "bk_ch01_b003"],
            },
            {
                "mention_id": "m_bk_ch01_b999_01",
                "surface": "Rook",
                "mention_type": "name",
                "resolution_status": "named",
                "candidate_entity_ids": [],
                "block_ids": ["bk_ch01_b999"],
            },
            {
                "mention_id": "m_bk_ch01_b002_01",
                "surface": "Mr. Alden",
                "mention_type": "name",
                "resolution_status": "named",
                "candidate_entity_ids": [],
                "block_ids": ["bk_ch01_b002"],
            },
        ],
    }

    report = validate_lexicon(
        payload,
        valid_block_ids={"bk_ch01_b002"},
        chapter_block_ids={"bk_ch01_b001", "bk_ch01_b002", "bk_ch01_b003"},
    )

    assert report.ok
    assert report.counts["outside_window_neighbor_dropped"] == 4
    assert report.counts["outside_window_nonexistent_dropped"] == 1
    assert payload["glossary_candidates"] == [
        {
            "source_term": "Raven Hall",
            "proposed_target_vi": "Raven Hall",
            "category": "place",
            "do_not_translate": True,
            "termhood": "proper_name",
            "block_ids": ["bk_ch01_b002"],
        }
    ]
    assert [item["surface"] for item in payload["character_mentions"]] == ["Bram", "Mr. Alden"]
    assert payload["character_mentions"][0]["block_ids"] == ["bk_ch01_b002"]


def test_narrative_validator_normalizes_named_candidate_id_conflicts() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "speaker_turns": [
            {
                "turn_id": "t_bk_ch01_b001_01",
                "speaker": {
                    "surface": "I",
                    "reference_kind": "narrator",
                    "resolution_status": "named",
                    "candidate_entity_ids": ["ent_narrator"],
                    "attribution_method": "nearby_context",
                    "confidence": "high",
                },
                "addressee": {
                    "surface": "Mr. Alden",
                    "reference_kind": "person",
                    "resolution_status": "named",
                    "candidate_entity_ids": ["ent_alden"],
                    "attribution_method": "explicit_tag",
                    "confidence": "high",
                },
                "utterance_quote": "Mr. Alden?",
                "address_term_used": "Mr. Alden",
                "register_cue": "neutral",
                "utterance_gist": "calls to Mr. Alden",
                "block_id": "bk_ch01_b001",
            }
        ],
        "relation_events": [],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"bk_ch01_b001"},
        known_entity_ids={"ent_narrator", "ent_alden"},
    )

    assert report.ok
    assert report.counts["named_pronoun_downgraded"] == 1
    assert report.counts["named_ids_cleared"] == 1
    turn = payload["speaker_turns"][0]
    assert turn["speaker"]["resolution_status"] == "candidate"
    assert turn["speaker"]["candidate_entity_ids"] == ["ent_narrator"]
    assert turn["addressee"]["resolution_status"] == "named"
    assert turn["addressee"]["candidate_entity_ids"] == []


def test_narrative_validator_drops_outside_window_entries_by_kind() -> None:
    def reference(surface: str) -> dict[str, object]:
        return {
            "surface": surface,
            "reference_kind": "person",
            "resolution_status": "named",
            "candidate_entity_ids": [],
            "attribution_method": "explicit_tag",
            "confidence": "high",
        }

    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b002"],
        "context_only_used": True,
        "speaker_turns": [
            {
                "turn_id": "t_bk_ch01_b003_01",
                "speaker": reference("Mira"),
                "addressee": reference("Mr. Alden"),
                "utterance_quote": "Wait.",
                "address_term_used": "",
                "register_cue": "neutral",
                "utterance_gist": "asks him to wait",
                "block_id": "bk_ch01_b003",
            },
            {
                "turn_id": "t_bk_ch01_b002_01",
                "speaker": reference("Mr. Alden"),
                "addressee": reference("Mira"),
                "utterance_quote": "Come in.",
                "address_term_used": "",
                "register_cue": "neutral",
                "utterance_gist": "invites her inside",
                "block_id": "bk_ch01_b002",
            },
        ],
        "relation_events": [
            {
                "event_id": "e_bk_ch01_b999_01",
                "actor": reference("Mira"),
                "target": reference("Mr. Alden"),
                "event_type": "addresses",
                "evidence_quote": "Mr. Alden",
                "block_id": "bk_ch01_b999",
            },
            {
                "event_id": "e_bk_ch01_b002_01",
                "actor": reference("Mr. Alden"),
                "target": reference("Mira"),
                "event_type": "invites",
                "evidence_quote": "Come in.",
                "block_id": "bk_ch01_b002",
            },
        ],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"bk_ch01_b002"},
        chapter_block_ids={"bk_ch01_b001", "bk_ch01_b002", "bk_ch01_b003"},
    )

    assert report.ok
    assert report.counts["outside_window_neighbor_dropped"] == 1
    assert report.counts["outside_window_nonexistent_dropped"] == 1
    assert [item["turn_id"] for item in payload["speaker_turns"]] == ["t_bk_ch01_b002_01"]
    assert [item["event_id"] for item in payload["relation_events"]] == ["e_bk_ch01_b002_01"]


def test_narrative_validator_allows_missing_utterance_gist() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "speaker_turns": [
            {
                "turn_id": "t_bk_ch01_b001_01",
                "speaker": {
                    "surface": "Mira",
                    "reference_kind": "person",
                    "resolution_status": "named",
                    "candidate_entity_ids": [],
                    "attribution_method": "explicit_tag",
                    "confidence": "high",
                },
                "addressee": {
                    "surface": "Mr. Alden",
                    "reference_kind": "person",
                    "resolution_status": "named",
                    "candidate_entity_ids": [],
                    "attribution_method": "explicit_tag",
                    "confidence": "high",
                },
                "utterance_quote": "Mr. Alden?",
                "address_term_used": "Mr. Alden",
                "register_cue": "neutral",
                "block_id": "bk_ch01_b001",
            }
        ],
        "relation_events": [],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"bk_ch01_b001"},
        chapter_block_ids={"bk_ch01_b001"},
    )

    assert report.ok
    assert not any("utterance_gist" in error for error in report.errors)
    assert report.counts["turns"] == 1


def test_narrative_validator_keeps_missing_block_id_as_hard_failure() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "speaker_turns": [],
        "relation_events": [
            {
                "event_id": "e_bk_ch01_missing_01",
                "actor": {
                    "surface": "Mira",
                    "reference_kind": "person",
                    "resolution_status": "named",
                    "candidate_entity_ids": [],
                    "attribution_method": "explicit_tag",
                    "confidence": "high",
                },
                "target": {
                    "surface": "Mr. Alden",
                    "reference_kind": "person",
                    "resolution_status": "named",
                    "candidate_entity_ids": [],
                    "attribution_method": "explicit_tag",
                    "confidence": "high",
                },
                "event_type": "addresses",
                "evidence_quote": "Mr. Alden",
            }
        ],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"bk_ch01_b001"},
        chapter_block_ids={"bk_ch01_b001"},
    )

    assert not report.ok
    assert report.counts["outside_window_neighbor_dropped"] == 0
    assert report.counts["outside_window_nonexistent_dropped"] == 0
    assert any("block_id is required" in error for error in report.errors)


def test_narrative_validator_keeps_unknown_candidate_id_as_hard_failure() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "speaker_turns": [
            {
                "turn_id": "t_bk_ch01_b001_01",
                "speaker": {
                    "surface": "I",
                    "reference_kind": "narrator",
                    "resolution_status": "named",
                    "candidate_entity_ids": ["ent_invented"],
                    "attribution_method": "nearby_context",
                    "confidence": "high",
                },
                "addressee": {
                    "surface": "",
                    "reference_kind": "unknown",
                    "resolution_status": "unknown",
                    "candidate_entity_ids": [],
                    "attribution_method": "nearby_context",
                    "confidence": "low",
                },
                "utterance_quote": "I said nothing.",
                "address_term_used": "",
                "register_cue": "neutral",
                "utterance_gist": "withholds comment",
                "block_id": "bk_ch01_b001",
            }
        ],
        "relation_events": [],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"bk_ch01_b001"},
        known_entity_ids={"ent_narrator"},
    )

    assert not report.ok
    assert any("candidate_entity_ids unknown" in error for error in report.errors)
    assert payload["speaker_turns"][0]["speaker"]["resolution_status"] == "unknown"
    assert payload["speaker_turns"][0]["speaker"]["candidate_entity_ids"] == []


def test_validation_retry_messages_preserve_full_output_and_request_field_edits() -> None:
    prior_output = '{"items":["' + ("x" * 6000) + '"]}'

    messages = _build_validation_retry_messages(
        [{"role": "system", "content": "Return JSON."}],
        prior_output=prior_output,
        validation_errors=["items[0].status invalid"],
    )

    assert messages[-2] == {"role": "assistant", "content": prior_output}
    assert len(messages[-2]["content"]) > 4000
    assert "Return the SAME items" in messages[-1]["content"]
    assert "Do NOT drop, merge, or add" in messages[-1]["content"]


def test_digest_validator_rejects_phase_finalization() -> None:
    blocks = ["ch04_b001", "ch04_b002"]
    payload = {
        "chapter_id": "wh_ch04",
        "chapter_rolling_summary": "Lockwood asks Nelly about the household.",
        "narration_frame_segments": [
            {
                "narrator_ref": "ent_lockwood",
                "block_range": ["ch04_b001", "ch04_b002"],
                "story_time_label": "frame_present",
            }
        ],
        "scene_summaries": [],
        "character_state_changes": [],
        "relation_event_summary": [
            {
                "pair": ["ent_hindley", "ent_heathcliff"],
                "observed_valence_hint": "negative",
                "event_ids": ["e1"],
                "status": "evidence_only",
                "phase_label": "hostile",
            }
        ],
        "unresolved_threads": [],
        "motifs": [],
        "translator_relevant_facts": [],
    }

    report = validate_digest(payload, chapter_block_ids=blocks)

    assert not report.ok
    assert any("must not finalize phase_label" in error for error in report.errors)


def _digest_with_segments(segments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "chapter_id": "bk_ch01",
        "chapter_rolling_summary": "A short chapter summary.",
        "narration_frame_segments": segments,
        "scene_summaries": [],
        "character_state_changes": [],
        "relation_event_summary": [],
        "unresolved_threads": [],
        "motifs": [],
        "translator_relevant_facts": [],
    }


def test_digest_coverage_skips_heading_at_chapter_start() -> None:
    payload = _digest_with_segments(
        [
            {
                "narrator_ref": "ent_alden",
                "block_range": ["bk_ch01_b002", "bk_ch01_b003"],
                "story_time_label": "frame_present",
            }
        ]
    )
    blocks = [
        {"block_id": "bk_ch01_b001", "block_type": "heading", "text": "CHAPTER I"},
        {"block_id": "bk_ch01_b002", "block_type": "paragraph", "text": "Alden entered."},
        {"block_id": "bk_ch01_b003", "block_type": "paragraph", "text": "Mira answered."},
    ]

    report = validate_digest(
        payload,
        chapter_block_ids=[block["block_id"] for block in blocks],
        chapter_blocks=blocks,
    )

    assert report.ok
    assert report.counts["nonnarrative_block_skipped"] == 1


def test_digest_coverage_skips_letterless_separator() -> None:
    payload = _digest_with_segments(
        [
            {
                "narrator_ref": "ent_alden",
                "block_range": ["bk_ch01_b001", "bk_ch01_b002"],
                "story_time_label": "frame_present",
            },
            {
                "narrator_ref": "ent_mira",
                "block_range": ["bk_ch01_b004", "bk_ch01_b004"],
                "story_time_label": "retrospective_past",
            },
        ]
    )
    blocks = [
        {"block_id": "bk_ch01_b001", "block_type": "paragraph", "text": "Alden entered."},
        {"block_id": "bk_ch01_b002", "block_type": "paragraph", "text": "Mira answered."},
        {"block_id": "bk_ch01_b003", "block_type": "paragraph", "text": "* * * * *"},
        {"block_id": "bk_ch01_b004", "block_type": "paragraph", "text": "The story continued."},
    ]

    report = validate_digest(
        payload,
        chapter_block_ids=[block["block_id"] for block in blocks],
        chapter_blocks=blocks,
    )

    assert report.ok
    assert report.counts["nonnarrative_block_skipped"] == 1


def test_digest_coverage_still_rejects_real_narrative_gap() -> None:
    payload = _digest_with_segments(
        [
            {
                "narrator_ref": "ent_alden",
                "block_range": ["bk_ch01_b001", "bk_ch01_b001"],
                "story_time_label": "frame_present",
            },
            {
                "narrator_ref": "ent_mira",
                "block_range": ["bk_ch01_b003", "bk_ch01_b003"],
                "story_time_label": "retrospective_past",
            },
        ]
    )
    blocks = [
        {"block_id": "bk_ch01_b001", "block_type": "paragraph", "text": "Alden entered."},
        {"block_id": "bk_ch01_b002", "block_type": "paragraph", "text": "Mira answered."},
        {"block_id": "bk_ch01_b003", "block_type": "paragraph", "text": "The story continued."},
    ]

    report = validate_digest(
        payload,
        chapter_block_ids=[block["block_id"] for block in blocks],
        chapter_blocks=blocks,
    )

    assert not report.ok
    assert report.counts["nonnarrative_block_skipped"] == 0
    assert any("starts at bk_ch01_b003, expected bk_ch01_b002" in error for error in report.errors)


def test_narrative_validator_drops_non_person_relation_target_without_failing_window() -> None:
    payload = {
        "chapter_id": "wh_ch01",
        "window_block_ids": ["wh_ch01_b017"],
        "context_only_used": False,
        "speaker_turns": [],
        "relation_events": [
            {
                "event_id": "e_wh_ch01_b017_01",
                "actor": {
                    "surface": "Mr. Heathcliff",
                    "reference_kind": "person",
                    "resolution_status": "named",
                    "candidate_entity_ids": [],
                    "attribution_method": "explicit_tag",
                    "confidence": "high",
                },
                "target": {
                    "surface": "dog",
                    "reference_kind": "unknown",
                    "resolution_status": "unknown",
                    "candidate_entity_ids": [],
                    "attribution_method": "narrator_inference",
                    "confidence": "low",
                },
                "event_type": "warns",
                "evidence_quote": "he warned the dog",
                "block_id": "wh_ch01_b017",
            }
        ],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"wh_ch01_b017"},
        known_entity_ids={"ent_mr_heathcliff"},
    )

    assert report.ok
    assert report.counts["events"] == 0
    assert report.counts["nonperson_event_dropped"] == 1
    assert any("dropped because target.reference_kind" in warning for warning in report.warnings)


def test_narrative_validator_normalizes_resolution_status_in_attribution_method() -> None:
    payload = {
        "chapter_id": "gg_ch01",
        "window_block_ids": ["gg_ch01_b022"],
        "context_only_used": False,
        "speaker_turns": [
            {
                "turn_id": "t_gg_ch01_b022_01",
                "speaker": {
                    "surface": "I",
                    "reference_kind": "narrator",
                    "resolution_status": "candidate",
                    "candidate_entity_ids": ["ent_nick"],
                    "attribution_method": "candidate",
                    "confidence": "medium",
                },
                "addressee": {
                    "surface": "",
                    "reference_kind": "unknown",
                    "resolution_status": "unknown",
                    "candidate_entity_ids": [],
                    "attribution_method": "nearby_context",
                    "confidence": "low",
                },
                "utterance_quote": "I said nothing.",
                "address_term_used": "",
                "register_cue": "neutral",
                "utterance_gist": "Nick withholds comment.",
                "block_id": "gg_ch01_b022",
            }
        ],
        "relation_events": [],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"gg_ch01_b022"},
        known_entity_ids={"ent_nick"},
    )

    assert report.ok
    assert report.counts["attribution_enum_dropped"] == 0
    assert report.counts["attribution_enum_normalized"] == 1
    assert len(payload["speaker_turns"]) == 1
    assert payload["speaker_turns"][0]["speaker"]["attribution_method"] == "unspecified"


def test_narrative_validator_keeps_other_attribution_enum_errors_hard() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "window_block_ids": ["bk_ch01_b001"],
        "context_only_used": False,
        "speaker_turns": [
            {
                "turn_id": "t_bk_ch01_b001_01",
                "speaker": {
                    "surface": "I",
                    "reference_kind": "narrator",
                    "resolution_status": "unknown",
                    "candidate_entity_ids": [],
                    "attribution_method": "guessed_from_vibes",
                    "confidence": "low",
                },
                "addressee": {
                    "surface": "",
                    "reference_kind": "unknown",
                    "resolution_status": "unknown",
                    "candidate_entity_ids": [],
                    "attribution_method": "nearby_context",
                    "confidence": "low",
                },
                "utterance_quote": "I said nothing.",
                "address_term_used": "",
                "register_cue": "neutral",
                "utterance_gist": "withholds comment",
                "block_id": "bk_ch01_b001",
            }
        ],
        "relation_events": [],
    }

    report = validate_narrative(
        payload,
        valid_block_ids={"bk_ch01_b001"},
        known_entity_ids=set(),
    )

    assert not report.ok
    assert report.counts["attribution_enum_normalized"] == 0
    assert any("attribution_method invalid" in error for error in report.errors)


@pytest.mark.skipif(not PINNED_EPUB.exists(), reason="Pinned WH EPUB is not present")
def test_pinned_gutenberg_epub_ingests_34_chapters() -> None:
    document, mapping = load_wuthering_heights_epub(PINNED_EPUB)

    assert document["doc_id"] == "wuthering_heights"
    assert len(document["chapters"]) == 34
    assert len(mapping) == 34
    assert mapping[0]["chapter_label"] == "CHAPTER I"
    assert mapping[-1]["chapter_label"] == "CHAPTER XXXIV"
    assert document["chapters"][0]["blocks"][0]["clean_text"] == "CHAPTER I"


@pytest.mark.skipif(not PINNED_EPUB.exists(), reason="Pinned WH EPUB is not present")
def test_dry_run_artifact_is_zero_api_and_has_sample_window() -> None:
    document, _mapping = load_wuthering_heights_epub(PINNED_EPUB)
    dry_run = build_dry_run_artifacts(document, ["1", "2", "3", "4"])

    assert dry_run["manifest"]["zero_api"] is True
    assert dry_run["manifest"]["prompt_source"] == "design/LITERARY_PROMPT_DESIGN.md"
    assert dry_run["window_manifest"]
    assert dry_run["sample_prompt_context"]["calls_planned"] == [
        "literary_chapter_brief_v1",
        "literary_lexicon_v1",
        "literary_narrative_v1",
    ]
    assert "previous_tail_block_ids" in dry_run["sample_prompt_context"]
    assert "next_tail_block_ids" in dry_run["sample_prompt_context"]
    assert all(row["ok"] for row in dry_run["fixture_validation"])


@pytest.mark.skipif(not PINNED_EPUB.exists(), reason="Pinned WH EPUB is not present")
def test_l2a1_windows_include_context_only_tails() -> None:
    document, _mapping = load_wuthering_heights_epub(PINNED_EPUB)
    chapter = document["chapters"][0]
    windows = build_literary_windows(chapter)

    assert windows
    assert windows[0].previous_tail == []
    assert windows[0].next_tail
    assert windows[1].previous_tail
    assert set(windows[1].block_ids).isdisjoint(
        {str(block["block_id"]) for block in windows[1].previous_tail}
    )


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Literary prompt design doc is not present")
def test_l2a1_prompt_loader_extracts_verbatim_versions() -> None:
    brief = load_system_prompt_from_design(DESIGN_DOC, "literary_chapter_brief_v1")
    lexicon = load_system_prompt_from_design(DESIGN_DOC, "literary_lexicon_v1")
    narrative = load_system_prompt_from_design(DESIGN_DOC, "literary_narrative_v1")

    assert "Prompt version: literary_chapter_brief_v1" in brief
    assert "Prompt version: literary_lexicon_v1" in lexicon
    assert "Prompt version: literary_narrative_v1" in narrative
    assert "do NOT infer relationships" in narrative
    assert "GENERIC honorific" in narrative


def test_l2a2_chapter_brief_validator_accepts_neutral_fixture() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "cast_on_stage": [
            {
                "surface": "Ravel",
                "surface_kind": "proper_name",
                "role_hint": "traveller",
                "first_seen_block": "bk_ch01_b002",
            },
            {
                "surface": "the innkeeper",
                "surface_kind": "descriptor",
                "role_hint": "innkeeper",
                "first_seen_block": "bk_ch01_b004",
            },
        ],
        "setting": {
            "place": "a roadside inn at dusk",
            "time_frame_hint": "frame_present",
            "scene_shape": "single_scene_one_location",
        },
        "scenes_party_size": [
            {
                "block_range": ["bk_ch01_b002", "bk_ch01_b016"],
                "co_present_count": 2,
                "participants": ["Ravel", "the innkeeper"],
            }
        ],
        "neutral_premise": "A tired traveller reaches an inn and takes a room for the night.",
    }

    report = validate_chapter_brief(
        payload,
        chapter_block_ids=["bk_ch01_b002", "bk_ch01_b004", "bk_ch01_b016"],
    )

    assert report.ok
    assert report.counts["cast_on_stage"] == 2
    assert report.counts["scenes"] == 1


def test_l2a2_chapter_brief_leak_guard_drops_entry_without_failing() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "cast_on_stage": [
            {
                "surface": "Ravel",
                "surface_kind": "proper_name",
                "role_hint": "friend",
                "first_seen_block": "bk_ch01_b002",
            }
        ],
        "setting": {
            "place": "a roadside inn",
            "time_frame_hint": "frame_present",
            "scene_shape": "single_scene_one_location",
        },
        "scenes_party_size": [],
        "neutral_premise": "A traveller meets an enemy at an inn.",
    }

    report = validate_chapter_brief(payload, chapter_block_ids=["bk_ch01_b002"])

    assert report.ok
    assert report.counts["cast_on_stage"] == 0
    assert report.counts["cast_dropped"] == 1
    assert report.counts["leak_tokens_dropped"] == 2
    rendered = render_chapter_brief_for_injection(payload)
    assert "friend" not in rendered
    assert "enemy" not in rendered
    assert safe_brief_neutral_premise(payload) == ""


def test_l2a2_chapter_brief_requires_surface_kind() -> None:
    payload = {
        "chapter_id": "bk_ch01",
        "cast_on_stage": [
            {
                "surface": "Ravel",
                "role_hint": "traveller",
                "first_seen_block": "bk_ch01_b002",
            }
        ],
        "setting": {
            "place": "a roadside inn",
            "time_frame_hint": "frame_present",
            "scene_shape": "single_scene_one_location",
        },
        "scenes_party_size": [],
        "neutral_premise": "A traveller reaches an inn.",
    }

    report = validate_chapter_brief(payload, chapter_block_ids=["bk_ch01_b002"])

    assert not report.ok
    assert any("surface_kind invalid" in error for error in report.errors)


def test_l2a2_seed_cast_is_citable_but_not_alias() -> None:
    ledger: dict[str, dict] = {}
    brief = {
        "cast_on_stage": [
            {
                "surface": "Nick",
                "surface_kind": "proper_name",
                "role_hint": "narrator",
                "first_seen_block": "gg_ch01_b002",
            },
            {
                "surface": "his father",
                "surface_kind": "descriptor",
                "role_hint": "father",
                "first_seen_block": "gg_ch01_b002",
            },
        ]
    }

    report = seed_entity_ledger_from_chapter_brief(
        ledger,
        brief,
        chapter_block_ids=["gg_ch01_b002"],
    )

    assert report["seeded_cast"][0]["entity_id"] == "ent_nick"
    assert report["seeded_cast"][0]["surface_evidence_block"] is None
    assert report["seed_skipped_cast"][0]["surface"] == "his father"
    assert ledger["ent_nick"]["aliases"] == []
    assert ledger["ent_nick"]["surface_evidence_block"] is None
    assert "ent_nick | Nick | seeded:chapter_brief_cast:no_surface_alias" in roster_from_ledger(ledger)


def test_l2a2_seed_cast_rejects_wrong_chapter_block_id() -> None:
    ledger: dict[str, dict] = {}
    brief = {
        "cast_on_stage": [
            {
                "surface": "Nick",
                "surface_kind": "proper_name",
                "role_hint": "narrator",
                "first_seen_block": "bk_ch01_b002",
            }
        ]
    }

    report = seed_entity_ledger_from_chapter_brief(
        ledger,
        brief,
        chapter_block_ids=["gg_ch01_b002"],
    )

    assert report["seeded_cast"] == []
    assert report["seed_skipped_cast"][0]["reason"] == "first_seen_block_outside_chapter"
    assert ledger == {}


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_l2a2_prompt_examples_use_active_chapter_prefix() -> None:
    prompt = load_system_prompt_for_chapter(DESIGN_DOC, "literary_lexicon_v1", "gg_ch01")

    assert "gg_ch01_b005" in prompt
    assert "bk_ch01_b005" not in prompt


def test_l2a2_neighbor_summaries_are_bounded_and_flat_size() -> None:
    summaries = [
        {"chapter_id": f"wh_ch{i:02d}", "summary": f"Summary {i}."}
        for i in range(1, 13)
    ]

    ch3 = render_neighbor_summaries(neighbor_summaries_for_index(summaries, 2, k=2))
    ch12 = render_neighbor_summaries(neighbor_summaries_for_index(summaries, 11, k=2))

    assert ch3.count("wh_ch") == 2
    assert ch12.count("wh_ch") == 2
    assert "wh_ch10" in ch12 and "wh_ch11" in ch12
    assert "wh_ch01" not in ch12
    assert abs(len(ch12) - len(ch3)) < 20


@pytest.mark.skipif(
    not PINNED_EPUB.exists() or not DESIGN_DOC.exists(),
    reason="Pinned WH EPUB or prompt design doc is not present",
)
def test_l2a1_m1_estimate_is_zero_api_and_uses_two_modes() -> None:
    document, _mapping = load_wuthering_heights_epub(PINNED_EPUB)
    config = LLMConfig(model="gpt-5.4-mini", prompt_token_cap=6000, max_output_tokens=1024)
    estimate = estimate_m1(document, ["1"], design_doc=DESIGN_DOC, config=config)

    assert estimate["zero_api"] is True
    assert estimate["modes"] == [
        "literary_chapter_brief_v1",
        "literary_lexicon_v1",
        "literary_narrative_v1",
    ]
    assert estimate["calls"] == estimate["windows"] * 2 + 1
    assert estimate["cost_cap_usd"] > 0


@pytest.mark.skipif(
    not PINNED_EPUB.exists() or not DESIGN_DOC.exists(),
    reason="Pinned WH EPUB or prompt design doc is not present",
)
def test_l2a1_m2_estimate_uses_m1_ledger_without_api(tmp_path: Path) -> None:
    document, _mapping = load_wuthering_heights_epub(PINNED_EPUB)
    m1_dir = tmp_path / "m1_1"
    (m1_dir / "narrative").mkdir(parents=True)
    (m1_dir / "m1_report.json").write_text(
        json.dumps(
            {
                "chapters_selected": ["wh_ch01"],
                "validation_counts": {"lexicon_failed": 0, "narrative_failed": 0},
                "entity_ledger": {
                    "ent_mr_heathcliff": {
                        "entity_id": "ent_mr_heathcliff",
                        "canonical": "Mr. Heathcliff",
                        "aliases": ["Mr. Heathcliff", "Heathcliff"],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (m1_dir / "narrative" / "wb_wh_ch01_001.json").write_text(
        json.dumps(
            {
                "parsed_json": {
                    "chapter_id": "wh_ch01",
                    "relation_events": [
                        {
                            "event_id": "e_wh_ch01_b001_01",
                            "actor": {
                                "surface": "I",
                                "reference_kind": "narrator",
                                "candidate_entity_ids": [],
                            },
                            "target": {
                                "surface": "Mr. Heathcliff",
                                "reference_kind": "person",
                                "candidate_entity_ids": ["ent_mr_heathcliff"],
                            },
                            "event_type": "visits",
                            "evidence_quote": "I have just returned from a visit to my landlord",
                            "block_id": "wh_ch01_b001",
                        }
                    ],
                },
                "block_ids": ["wh_ch01_b001"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = LLMConfig(model="gpt-5.4-mini", prompt_token_cap=10000, max_output_tokens=1024)
    estimate = estimate_m2(
        document,
        ["1"],
        design_doc=DESIGN_DOC,
        config=config,
        m1_dir=m1_dir,
    )

    assert estimate["zero_api"] is True
    assert estimate["modes"] == ["literary_digest_v1"]
    assert estimate["calls"] == 1
    assert estimate["call_estimates"][0]["relation_event_lines"] == 1


@pytest.mark.skipif(
    not PINNED_EPUB.exists() or not DESIGN_DOC.exists(),
    reason="Pinned WH EPUB or prompt design doc is not present",
)
def test_l2a1_prompt_builders_mark_context_tail_as_context_only() -> None:
    document, _mapping = load_wuthering_heights_epub(PINNED_EPUB)
    window = build_literary_windows(document["chapters"][0])[0]

    brief_messages = build_chapter_brief_messages(
        design_doc=DESIGN_DOC,
        chapter=document["chapters"][0],
        registry_context_pack="",
        neighbor_summaries="wh_ch00\nEarlier neutral gist.",
    )
    lex_messages = build_lexicon_messages(
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch01",
        window=window,
        registry_context_pack="",
        chapter_brief="cast | Lockwood | visitor | wh_ch01_b001",
        neighbor_summaries="wh_ch00\nEarlier neutral gist.",
    )
    narrative_messages = build_narrative_messages(
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch01",
        window=window,
        narrator_hints="wh_ch01_b001..wh_ch01_b008 | unknown",
        chapter_roster="",
        window_mentions="",
        chapter_brief="scene | wh_ch01_b001..wh_ch01_b008 | co_present_count=2 | Lockwood, Heathcliff",
        neighbor_summaries="wh_ch00\nEarlier neutral gist.",
    )

    assert "NEIGHBOR_SUMMARIES_GIST_ONLY" in brief_messages[1]["content"]
    assert "NEXT_WINDOW_TAIL_CONTEXT_ONLY" in lex_messages[1]["content"]
    assert "NEXT_WINDOW_TAIL_CONTEXT_ONLY" in narrative_messages[1]["content"]
    assert "CHAPTER_BRIEF" in lex_messages[1]["content"]
    assert "CHAPTER_BRIEF" in narrative_messages[1]["content"]
    assert "ACTIVE_NARRATOR_HINTS_BY_BLOCK_RANGE" in narrative_messages[1]["content"]


@pytest.mark.skipif(not PINNED_EPUB.exists() or not DESIGN_DOC.exists(), reason="Inputs missing")
def test_l2a1_digest_prompt_includes_required_sections() -> None:
    document, _mapping = load_wuthering_heights_epub(PINNED_EPUB)
    chapter = document["chapters"][0]

    messages = build_digest_messages(
        design_doc=DESIGN_DOC,
        chapter=chapter,
        previous_summary="(none)",
        neighbor_summaries="wh_ch00\nEarlier neutral gist.",
        chapter_brief="cast | Lockwood | visitor | wh_ch01_b001",
        chapter_roster="ent_mr_heathcliff | Mr. Heathcliff | Heathcliff",
        chapter_relation_events="narrator -> ent_mr_heathcliff | visits | wh_ch01_b001",
    )

    assert "Prompt version: literary_digest_v1" in messages[0]["content"]
    assert "NEIGHBOR_SUMMARIES_GIST_ONLY" in messages[1]["content"]
    assert "CHAPTER_BRIEF" in messages[1]["content"]
    assert "CHAPTER_ROSTER" in messages[1]["content"]
    assert "CHAPTER_RELATION_EVENTS" in messages[1]["content"]
    assert "[wh_ch01_b001]" in messages[1]["content"]


def test_l2a1_m3_consolidates_ch1_watch_items(tmp_path: Path) -> None:
    document = {
        "chapters": [
            {
                "chapter_id": "wh_ch01",
                "blocks": [
                    {"block_id": "wh_ch01_b001", "order_index": 1, "clean_text": "CHAPTER I"},
                    {"block_id": "wh_ch01_b002", "order_index": 2, "clean_text": "I visit Heathcliff."},
                    {"block_id": "wh_ch01_b012", "order_index": 12, "clean_text": "Hareton Earnshaw 1500"},
                    {"block_id": "wh_ch01_b028", "order_index": 28, "clean_text": "I shall go."},
                ],
            }
        ]
    }
    out_dir = tmp_path / "m1_1"
    m1_dir = out_dir
    digest_dir = out_dir / "digest"
    (m1_dir / "lexicon").mkdir(parents=True)
    (m1_dir / "narrative").mkdir(parents=True)
    digest_dir.mkdir(parents=True)
    (m1_dir / "m1_report.json").write_text(
        json.dumps(
            {
                "validation_counts": {"lexicon_failed": 0, "narrative_failed": 0},
                "entity_ledger": {
                    "ent_heathcliff": {"canonical": "Heathcliff", "aliases": ["Heathcliff"]},
                    "ent_mr_heathcliff": {
                        "canonical": "Mr. Heathcliff",
                        "aliases": ["Mr. Heathcliff"],
                    },
                    "ent_hareton_earnshaw": {
                        "canonical": "Hareton Earnshaw",
                        "aliases": ["Hareton Earnshaw"],
                    },
                    "ent_mr_lockwood": {"canonical": "Mr. Lockwood", "aliases": ["Mr. Lockwood"]},
                    "ent_joseph": {"canonical": "Joseph", "aliases": ["Joseph"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (m1_dir / "lexicon" / "wb_wh_ch01_001.json").write_text(
        json.dumps(
            {
                "parsed_json": {
                    "character_mentions": [
                        {"surface": "Mr. Heathcliff", "block_ids": ["wh_ch01_b002"]},
                        {"surface": "Heathcliff", "block_ids": ["wh_ch01_b002"]},
                        {"surface": "Hareton Earnshaw", "block_ids": ["wh_ch01_b012"]},
                        {"surface": "Mr. Lockwood", "block_ids": ["wh_ch01_b002"]},
                    ],
                    "glossary_candidates": [],
                }
            }
        ),
        encoding="utf-8",
    )
    (m1_dir / "narrative" / "wb_wh_ch01_001.json").write_text(
        json.dumps(
            {
                "parsed_json": {
                    "chapter_id": "wh_ch01",
                    "speaker_turns": [
                        {
                            "turn_id": "t1",
                            "speaker": {"surface": "I", "reference_kind": "narrator"},
                            "addressee": {"surface": "Mr. Heathcliff", "reference_kind": "person"},
                            "utterance_quote": "Mr. Heathcliff?",
                            "address_term_used": "Mr. Heathcliff",
                            "register_cue": "formal",
                            "utterance_gist": "addresses landlord",
                            "block_id": "wh_ch01_b002",
                        }
                    ],
                    "relation_events": [
                        {
                            "event_id": "e1",
                            "actor": {"surface": "I", "reference_kind": "narrator"},
                            "target": {"surface": "Mr. Heathcliff", "reference_kind": "person"},
                            "event_type": "addresses",
                            "evidence_quote": "Mr. Heathcliff?",
                            "block_id": "wh_ch01_b002",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    digest = {
        "chapter_id": "wh_ch01",
        "chapter_rolling_summary": "Lockwood visits Heathcliff.",
        "narration_frame_segments": [
            {
                "narrator_ref": "ent_mr_lockwood",
                "block_range": ["wh_ch01_b001", "wh_ch01_b028"],
                "story_time_label": "frame_present",
            }
        ],
        "scene_summaries": [],
        "character_state_changes": [
            {
                "entity_ref": "ent_mr_lockwood",
                "attribute": "residence",
                "from": "new_arrival",
                "to": "visiting_wuthering_heights",
                "trigger_block": "wh_ch01_b002",
                "evidence_quote": "visit",
                "observed_scope": "this_chapter",
            }
        ],
        "relation_event_summary": [
            {
                "pair": ["ent_mr_lockwood", "ent_mr_heathcliff"],
                "observed_valence_hint": "mixed",
                "event_ids": ["e1"],
                "status": "evidence_only",
            }
        ],
        "unresolved_threads": [],
        "motifs": [],
        "translator_relevant_facts": [],
    }
    (digest_dir / "wh_ch01.json").write_text(
        json.dumps({"parsed_json": digest, "validation": {"ok": True}}),
        encoding="utf-8",
    )

    estimate = estimate_m3(document, ["1"], m1_dir=m1_dir, digest_dir=digest_dir)
    report = run_m3(document, ["1"], out_dir=out_dir, m1_dir=m1_dir, digest_dir=digest_dir)
    story = json.loads((out_dir / "story_bible" / "wh_ch01_story_bible.json").read_text())

    assert estimate["zero_api"] is True
    assert report["validation_counts"]["story_bible_ok"] == 1
    assert story["canary_report"]["pass"] is True
    heathcliff = [e for e in story["registry_T2_entities"] if e["entity_id"] == "ent_heathcliff"]
    assert len(heathcliff) == 1
    assert {a["surface"] for a in heathcliff[0]["aliases"]} >= {"Heathcliff", "Mr. Heathcliff"}
    hareton = [e for e in story["registry_T2_entities"] if e["entity_id"] == "ent_hareton_earnshaw"][0]
    assert hareton["presence_status"] == "mentioned_historical"
    assert story["entity_state_intervals"] == []
    observed_dirs = [
        direction
        for policy in story["address_policies"]
        for direction in [policy["a_to_b"], policy["b_to_a"]]
        if direction["evidence_level"] == "observed"
    ]
    assert observed_dirs
    assert all(direction["observed_terms"] for direction in observed_dirs)
    assert all(direction["self"] == "" for direction in observed_dirs)
    assert all(direction["address"] == "" for direction in observed_dirs)
    assert all(direction["register"] == "" for direction in observed_dirs)


def test_l2a1_m3_consolidation_has_no_ch1_answer_tables() -> None:
    source = (REPO_ROOT / "THESIS_RUNTIME_TOOL" / "pipeline" / "literary" / "builder_pilot.py").read_text(
        encoding="utf-8"
    )

    forbidden = [
        "if set(pair)",
        "target_id ==",
        "the_dog",
        "the_bottle",
        "the_biter",
        "surly_owner",
        "his_master",
        '"ent_joseph": "Joseph"',
    ]
    for marker in forbidden:
        assert marker not in source


def test_fixture_json_round_trip() -> None:
    payload = built_in_fixture_payloads()
    encoded = json.dumps(payload, ensure_ascii=False)
    decoded = json.loads(encoded)

    assert decoded["partial_story_bible"]["scope"] == "ch1-4"
