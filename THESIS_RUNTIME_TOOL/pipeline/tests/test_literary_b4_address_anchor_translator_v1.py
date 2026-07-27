from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.literary.b4_address_anchor_v1 import (
    B4AddressAnchorError,
    build_address_anchor_artifact_v1,
    render_address_anchor_request_v1,
    validate_address_anchor_response_v1,
)
from pipeline.literary.b4_story_bible_assembler_v1 import (
    ANCHOR_INPUT_SCHEMA_VERSION,
    ANCHOR_OUTPUT_SCHEMA_VERSION,
    SCHEMA_VERSION as STORY_BIBLE_SCHEMA_VERSION,
    WINDOW_SCHEMA_VERSION,
)
from pipeline.literary.b4_translator_v1 import (
    B4TranslatorError,
    PROMPT_ID,
    RESPONSE_SCHEMA_VERSION,
    SYSTEM_PROMPT,
    assemble_translation_chapter_v1,
    assert_reference_scoring_allowed_v1,
    assert_stable_prefixes_v1,
    build_translation_window_artifact_v1,
    render_translation_window_request_v1,
    translator_window_prompt_view_v1,
    validate_translation_window_response_v1,
)
from pipeline.literary.b4_translator_pack_v1 import (
    project_translator_pack_v1,
    seal_translator_pack_v1,
)
from pipeline.literary.checkpoint import canonical_hash


STYLE_VERSION = "literary_style_profile_demo_v1"
STYLE_PROFILE = (
    "Use restrained contemporary Vietnamese for this demo arm.\n"
    f"- Prompt version: {STYLE_VERSION}."
)
PAIR_ID = "pair_01"
PRONOUN_PAIR = {"speaker": "tôi", "addressee": "ông"}
VOCATIVE = "thưa ông"


def test_translator_prompt_keeps_authority_boundary_without_self_check() -> None:
    assert PROMPT_ID == "literary_b4_translator_v8"
    assert f"Prompt version: {PROMPT_ID}." in SYSTEM_PROMPT
    assert "active source text as authoritative" in SYSTEM_PROMPT
    assert "pack as authoritative for resolved identity" in SYSTEM_PROMPT
    assert "subject, action, and object" not in SYSTEM_PROMPT
    assert "irony and emotional direction" not in SYSTEM_PROMPT
    assert "foreign-language contamination" not in SYSTEM_PROMPT
    assert "Do not report this check." not in SYSTEM_PROMPT
    assert "Wuthering Heights" not in SYSTEM_PROMPT


def _seal(body: dict, field: str = "artifact_hash") -> dict:
    return {**deepcopy(body), field: canonical_hash(body)}


def _anchor_input(*, gap: bool = False) -> dict:
    body = {
        "schema_version": ANCHOR_INPUT_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "story_bible_artifact_hash": "1" * 64,
        "pairs": [
            {
                "pair_id": PAIR_ID,
                "speaker_effective_entity_id": "entity_speaker",
                "addressee_effective_entity_id": "entity_addressee",
                "speaker_surface": "the narrator",
                "addressee_surface": "the landlord",
                "observed_terms": [
                    {
                        "term": "sir",
                        "count": 1,
                        "chapters": ["bk_ch01"],
                        "example_anchor": "Good evening, sir.",
                        "established_in_chapter": "bk_ch01",
                    }
                ],
                "registers": [{"register_cue": "formal", "count": 2}],
                "tones": [],
                "turn_count": 2,
                "example_anchor": "Good evening, sir.",
                "source_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
                "relations": [],
                "speaker_claims": {
                    "gender": {"value": "masculine", "evidence_ref": "e1"},
                    "life_stage": {"value": "adult", "evidence_ref": "e2"},
                },
                "addressee_claims": {
                    "gender": {"value": "masculine", "evidence_ref": "e3"},
                    "life_stage": {"value": "adult", "evidence_ref": "e4"},
                },
                "evidence_completeness": {
                    "speaker_resolved": True,
                    "addressee_resolved": not gap,
                    "turn_count": 2,
                    "vocative_count": 1,
                    "relation_present": False,
                    "relation_contested": False,
                    "missing_claims": {
                        "speaker": [],
                        "addressee": ["life_stage"] if gap else [],
                    },
                    "pending_identity": False,
                    "anchorable": True,
                },
            }
        ],
        "provider_calls": 0,
    }
    return _seal(body)


def _anchor_artifact(*, anchored: bool = True, gap: bool = False) -> dict:
    rendered = render_address_anchor_request_v1(
        anchor_input=_anchor_input(gap=gap),
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
    )
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": "bk_ch01",
        "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
        "pair_decisions": [
            {
                "pair_id": "P1",
                "pronoun_pair": deepcopy(PRONOUN_PAIR) if anchored else None,
                "vocative_options": (
                    [{"form": VOCATIVE}, {"form": "ông"}] if anchored else []
                ),
                "register_shifts": [],
                "evidence_refs": ["bk_ch01_b001"] if anchored else [],
                "model_confidence": "high" if gap else "medium",
                "not_anchored": (
                    None
                    if anchored
                    else {"reason": "The evidence does not support one form."}
                ),
            }
        ],
    }
    validated = validate_address_anchor_response_v1(
        rendered=rendered,
        response=response,
    )
    return build_address_anchor_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt={"receipt_id": "fixture_anchor"},
        provider_called=True,
    )


def _story(anchor: dict) -> dict:
    body = {
        "schema_version": STORY_BIBLE_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "chapter_order": 1,
        "entities": [
            _entity("entity_speaker", "the narrator"),
            _entity("entity_addressee", "the landlord"),
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
                }
            ],
            "handoff": None,
        },
        "open_questions": {},
        "lineage": {},
        "memory_budget": {},
        "provider_calls": 0,
    }
    story = _seal(body)
    anchor_body = deepcopy(anchor)
    anchor_body.pop("artifact_hash")
    anchor_body["story_bible_artifact_hash"] = story["artifact_hash"]
    return story, _seal(anchor_body)


def _entity(entity_id: str, surface: str) -> dict:
    return {
        "effective_entity_id": entity_id,
        "canonical_surface": surface,
        "aliases": [],
        "stable_surfaces": [surface],
        "claims": {},
        "referent_kind": "person",
        "first_seen": "bk_ch01_b001",
        "member_card_ids": [entity_id],
        "member_chapters": ["bk_ch01"],
        "record_class": "person",
        "established_in_chapter": "bk_ch01",
    }


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {"block_id": f"bk_ch01_b00{index}", "clean_text": f"Source {index}."}
            for index in range(1, 5)
        ],
    }


def _pair(turn_ids: list[str], source_blocks: list[str]) -> dict:
    return {
        "pair_id": PAIR_ID,
        "unanchored": False,
        "speaker_effective_entity_id": "entity_speaker",
        "addressee_effective_entity_id": "entity_addressee",
        "speaker_surface": "I",
        "addressee_surface": "sir",
        "speaker_resolved": True,
        "addressee_resolved": True,
        "observed_terms": [],
        "registers": [{"register_cue": "formal", "count": len(turn_ids)}],
        "tones": [],
        "turn_count": len(turn_ids),
        "vocative_count": len(turn_ids),
        "relation_present": False,
        "relation_contested": False,
        "relation_edge_ids": [],
        "missing_claims": {"speaker": [], "addressee": []},
        "pending_identity": False,
        "anchorable": True,
        "example_anchor": "Good evening, sir.",
        "turn_ids": turn_ids,
        "source_block_ids": source_blocks,
        "chapters": ["bk_ch01"],
        "established_in_chapter": "bk_ch01",
    }


def _turn(turn_id: str, block_id: str, *, membership: str) -> dict:
    return {
        "speaker_turn_id": turn_id,
        "block_id": block_id,
        "chapter_id": "bk_ch01",
        "chapter_order": 1,
        "frame_segment_id": "frame_1",
        "speaker": {
            "surface": "I",
            "candidate_card_ids": ["entity_speaker"],
            "effective_entity_ids": ["entity_speaker"],
            "resolution_status": "resolved_candidate",
            "resolved_to_effective_entity": True,
            "unresolved": False,
        },
        "addressee": {
            "surface": "sir",
            "candidate_card_ids": ["entity_addressee"],
            "effective_entity_ids": ["entity_addressee"],
            "resolution_status": "resolved_candidate",
            "resolved_to_effective_entity": True,
            "unresolved": False,
        },
        "address_terms": ["sir"],
        "register_cue": "formal",
        "register_cue_raw": None,
        "delivery_tone": None,
        "utterance_anchor": "Good evening, sir.",
        "window_membership": membership,
        "established_in_chapter": "bk_ch01",
    }


def _window(order: int) -> dict:
    if order == 1:
        active = ["bk_ch01_b001", "bk_ch01_b002"]
        tail = []
        turns = [
            _turn("turn_1", "bk_ch01_b001", membership="active"),
            _turn("turn_2", "bk_ch01_b002", membership="active"),
        ]
        pair = _pair(["turn_1", "turn_2"], active)
    else:
        active = ["bk_ch01_b003", "bk_ch01_b004"]
        tail = ["bk_ch01_b002"]
        turns = [
            _turn("turn_2", "bk_ch01_b002", membership="tail"),
            _turn("turn_3", "bk_ch01_b003", membership="active"),
        ]
        pair = _pair(["turn_2", "turn_3"], ["bk_ch01_b002", "bk_ch01_b003"])
    body = {
        "schema_version": WINDOW_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "window_id": f"window_{order}",
        "window_order": order,
        "window_plan_hash": "2" * 64,
        "active_block_ids": active,
        "preceding_tail_block_ids": tail,
        "estimated_active_source_tokens": 20,
        "speaker_turns": turns,
        "address_pairs": [pair],
        "lineage": {},
        "provider_calls": 0,
    }
    return _seal(body)


def _raw(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _pack(story: dict, anchor: dict) -> dict:
    projected = project_translator_pack_v1(
        story_bible=story,
        address_anchor=anchor,
        window_slices=[_window(1), _window(2)],
    )
    return seal_translator_pack_v1(
        projected=projected,
        budget_report={
            "translator_cap_tokens": 64_000,
            "headroom_tokens": 4_000,
            "fixed_prompt_upper_bound_tokens": 2_000,
            "pack_budget_tokens": 58_000,
            "pack_estimated_tokens": 1_000,
            "max_full_prompt_upper_bound_tokens": 3_000,
            "safety_multiplier": 1.25,
            "calibration_artifact_hash": "3" * 64,
        },
    )


def _rebind_pack(pack: dict, anchor: dict) -> dict:
    body = deepcopy(pack)
    body.pop("artifact_hash")
    body["address_anchor_artifact_hash"] = anchor["artifact_hash"]
    return _seal(body)


def _render(order: int, *, anchored: bool = True):
    initial_anchor = _anchor_artifact(anchored=anchored)
    story, anchor = _story(initial_anchor)
    pack = _pack(story, anchor)
    window = _window(order)
    tails = {} if order == 1 else {"bk_ch01_b002": "Đích 2."}
    rendered = render_translation_window_request_v1(
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
        translator_pack_bytes=_raw(pack),
        address_anchor_bytes=_raw(anchor),
        window_slice_bytes=_raw(window),
        chapter=_chapter(),
        accepted_tail_translations=tails,
    )
    return rendered, story, anchor, window


def _response(rendered, *, wrong_anchor: bool = False) -> dict:
    blocks = []
    turns_by_block = {}
    for turn in rendered.window_slice["speaker_turns"]:
        if turn["window_membership"] == "active":
            turns_by_block.setdefault(turn["block_id"], []).append(
                turn["speaker_turn_id"]
            )
    for block_id in rendered.window_slice["active_block_ids"]:
        turn_ids = turns_by_block.get(block_id, [])
        forms = []
        for turn_id in turn_ids:
            forms.append(
                {
                    "turn_id": turn_id,
                    "pair_id": PAIR_ID,
                    "pronoun_pair": (
                        {"speaker": "ta", "addressee": "ngươi"}
                        if wrong_anchor
                        else deepcopy(PRONOUN_PAIR)
                    ),
                    "vocative_used": VOCATIVE,
                    "pronoun_realization": "overt",
                    "from_anchor": not wrong_anchor,
                }
            )
        source = next(
            row
            for row in _chapter()["blocks"]
            if row["block_id"] == block_id
        )
        blocks.append(
            {
                "block_id": block_id,
                "source_text": source["clean_text"],
                "target_text": (
                    f"Thưa ông, đích {block_id[-1]}."
                    if turn_ids
                    else f"Đích {block_id[-1]}."
                ),
                "turn_refs": turn_ids,
                "address_forms_used": forms,
                "anchor_deviations": [],
                "untranslatable_notes": [],
            }
        )
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "chapter_id": "bk_ch01",
        "window_id": rendered.window_slice["window_id"],
        "style_profile_version": STYLE_VERSION,
        "measured_arm": False,
        "story_bible_artifact_hash": rendered.translator_pack[
            "story_bible_artifact_hash"
        ],
        "address_anchor_artifact_hash": rendered.address_anchor["artifact_hash"],
        "blocks": blocks,
    }


def _translation_only_response(rendered, *, wrong_anchor: bool = False) -> dict:
    del wrong_anchor
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "blocks": [
            {
                "block_id": block_id,
                "target_text": f"Ban dich {block_id[-1]}.",
            }
            for block_id in rendered.window_slice["active_block_ids"]
        ],
    }


_response = _translation_only_response


def test_t1_stable_prefix_is_byte_identical_and_perturbation_halts() -> None:
    first, _, _, _ = _render(1)
    second, _, _, _ = _render(2)
    assert assert_stable_prefixes_v1([first, second]) == first.stable_prefix_sha256
    assert "provider_receipt" not in first.stable_prefix_messages[-1]["content"]
    assert (
        first.address_anchor["artifact_hash"]
        in first.stable_prefix_messages[-1]["content"]
    )
    assert len(first.messages) == 2
    assert "[TRANSLATION_REQUEST_PACK]" in first.messages[-1]["content"]

    changed_anchor = deepcopy(second.address_anchor)
    changed_anchor.pop("artifact_hash")
    changed_anchor["review_issues"] = [{"issue_kind": "perturbed"}]
    changed_anchor = _seal(changed_anchor)
    changed_pack = _rebind_pack(second.translator_pack, changed_anchor)
    perturbed = render_translation_window_request_v1(
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
        translator_pack_bytes=_raw(changed_pack),
        address_anchor_bytes=_raw(changed_anchor),
        window_slice_bytes=_raw(second.window_slice),
        chapter=_chapter(),
        accepted_tail_translations={"bk_ch01_b002": "Đích 2."},
    )
    with pytest.raises(B4TranslatorError, match="byte-identical prefix"):
        assert_stable_prefixes_v1([first, perturbed])


def test_post_anchor_window_prompt_compacts_decided_pairs_only() -> None:
    rendered, _, _, source_window = _render(1)
    prompt_window = rendered.model_input_pack["window_context"]["window_slice"]

    assert rendered.window_slice == source_window
    assert prompt_window["source_window_artifact_hash"] == source_window[
        "artifact_hash"
    ]
    turn = prompt_window["speaker_turns"][0]
    assert set(turn) == {
        "speaker_turn_id",
        "block_id",
        "frame_segment_id",
        "speaker",
        "addressee",
        "address_terms",
        "register_cue",
        "delivery_tone",
        "utterance_anchor",
    }
    assert turn["speaker"] == {"effective_entity_id": "entity_speaker"}
    assert turn["addressee"] == {"effective_entity_id": "entity_addressee"}
    pair = prompt_window["address_pairs"][0]
    assert set(pair) == {
        "pair_id",
        "speaker_effective_entity_id",
        "addressee_effective_entity_id",
        "turn_ids",
    }
    assert "observed_terms" not in pair
    assert "missing_claims" not in pair


def test_post_anchor_window_prompt_keeps_evidence_for_not_anchored_pair() -> None:
    rendered, _, _, source_window = _render(1, anchored=False)
    prompt_window = translator_window_prompt_view_v1(
        window=source_window,
        anchor=rendered.address_anchor,
    )
    pair = prompt_window["address_pairs"][0]

    assert pair == source_window["address_pairs"][0]
    assert "observed_terms" in pair
    assert "missing_claims" in pair
    assert "example_anchor" in pair


def test_post_anchor_window_prompt_rejects_ambiguous_resolved_endpoint() -> None:
    _, story, anchor, window = _render(1)
    changed_window = deepcopy(window)
    changed_window.pop("artifact_hash")
    changed_window["speaker_turns"][0]["speaker"]["effective_entity_ids"] = [
        "entity_speaker",
        "entity_other",
    ]
    changed_window = _seal(changed_window)

    with pytest.raises(
        B4TranslatorError,
        match="resolved window speaker must name exactly one effective entity",
    ):
        render_translation_window_request_v1(
            style_profile=STYLE_PROFILE,
            style_profile_version=STYLE_VERSION,
            measured_arm=False,
            translator_pack_bytes=_raw(_pack(story, anchor)),
            address_anchor_bytes=_raw(anchor),
            window_slice_bytes=_raw(changed_window),
            chapter=_chapter(),
            accepted_tail_translations={},
        )


def test_t21_unresolved_window_pair_is_valid_without_anchor_membership() -> None:
    _, story, anchor, window = _render(1)
    changed_window = deepcopy(window)
    changed_window.pop("artifact_hash")
    changed_window["address_pairs"][0]["pair_id"] = None
    changed_window["address_pairs"][0]["unanchored"] = True
    changed_window = _seal(changed_window)
    rendered = render_translation_window_request_v1(
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
        translator_pack_bytes=_raw(_pack(story, anchor)),
        address_anchor_bytes=_raw(anchor),
        window_slice_bytes=_raw(changed_window),
        chapter=_chapter(),
        accepted_tail_translations={},
    )
    response = _response(rendered)
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["blocks"]
    assert rendered.model_input_pack["window_context"]["window_slice"][
        "address_pairs"
    ][0]["pair_id"] is None


def test_t22_foreign_non_null_window_pair_halts_before_transport() -> None:
    _, story, anchor, window = _render(1)
    changed_window = deepcopy(window)
    changed_window.pop("artifact_hash")
    changed_window["address_pairs"][0]["pair_id"] = "foreign_pair"
    changed_window = _seal(changed_window)
    with pytest.raises(
        B4TranslatorError,
        match="absent from the Address Anchor",
    ):
        render_translation_window_request_v1(
            style_profile=STYLE_PROFILE,
            style_profile_version=STYLE_VERSION,
            measured_arm=False,
            translator_pack_bytes=_raw(_pack(story, anchor)),
            address_anchor_bytes=_raw(anchor),
            window_slice_bytes=_raw(changed_window),
            chapter=_chapter(),
            accepted_tail_translations={},
        )


def _legacy_test_t24_pronoun_difference_is_recorded_without_halting() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    changed = next(
        form
        for block in response["blocks"]
        for form in block["address_forms_used"]
    )
    changed["pronoun_pair"] = {"speaker": "ta", "addressee": "người"}
    changed["from_anchor"] = False
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    deviations = [
        deviation
        for block in validated["blocks"]
        for deviation in block["anchor_deviations"]
    ]
    assert [row["rule"] for row in deviations] == [
        "pronoun_pair_differs_without_declared_deviation"
    ]
    assert validated["anchor_deviation_count"] == 1


def test_t25_group_a_repeated_source_block_still_halts() -> None:
    _, story, anchor, window = _render(1)
    chapter = _chapter()
    chapter["blocks"].append(deepcopy(chapter["blocks"][0]))
    with pytest.raises(B4TranslatorError, match="repeats a source block"):
        render_translation_window_request_v1(
            style_profile=STYLE_PROFILE,
            style_profile_version=STYLE_VERSION,
            measured_arm=False,
            translator_pack_bytes=_raw(_pack(story, anchor)),
            address_anchor_bytes=_raw(anchor),
            window_slice_bytes=_raw(window),
            chapter=chapter,
            accepted_tail_translations={},
        )


def test_translation_only_response_derives_auditable_source_context() -> None:
    rendered, _, _, _ = _render(1)
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=_response(rendered),
    )

    assert validated["translator_output_contract"] == "translation_only_v1"
    assert validated["address_metadata_collected"] is False
    assert validated["model_input_pack_hash"] == rendered.model_input_pack[
        "pack_hash"
    ]
    assert validated["blocks"][0] == {
        "block_id": "bk_ch01_b001",
        "source_text": "Source 1.",
        "target_text": "Ban dich 1.",
        "turn_refs": ["turn_1"],
    }


def test_translation_only_schema_rejects_address_metadata() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    response["blocks"][0]["address_forms_used"] = []

    with pytest.raises(B4TranslatorError, match="Additional properties"):
        validate_translation_window_response_v1(
            rendered=rendered,
            response=response,
        )


def test_translation_only_response_requires_exact_block_order() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    response["blocks"].reverse()

    with pytest.raises(B4TranslatorError, match="exact-cover.*in order"):
        validate_translation_window_response_v1(
            rendered=rendered,
            response=response,
        )


def _legacy_test_t26_deviation_rule_counts_sum_to_total() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered, wrong_anchor=True)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    row["target_text"] = "Xin chào."
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert sum(validated["anchor_deviation_by_rule"].values()) == validated[
        "anchor_deviation_count"
    ]


def _legacy_test_address_metadata_mismatch_quarantines_only_the_owned_turn() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    form = row["address_forms_used"][0]
    target_text = row["target_text"]
    row["anchor_deviations"] = [
        {
            "turn_id": form["turn_id"],
            "pair_id": form["pair_id"],
            "anchored_pronoun_pair": deepcopy(PRONOUN_PAIR),
            "used_pronoun_pair": deepcopy(form["pronoun_pair"]),
            "vocative": "different vocative",
            "reason": "The prose used a different vocative.",
        }
    ]

    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )

    validated_row = next(
        block
        for block in validated["blocks"]
        if block["block_id"] == row["block_id"]
    )
    assert validated_row["target_text"] == target_text
    assert validated_row["address_forms_used"] == []
    assert validated_row["anchor_deviations"] == []
    assert validated_row["address_metadata_unverified_turn_ids"] == [
        form["turn_id"]
    ]
    assert validated["address_metadata_unverified_count"] == 1
    assert validated["address_metadata_unverified_turn_ids"] == [
        form["turn_id"]
    ]
    assert validated["quarantined_address_metadata"] == [
        {
            "block_id": row["block_id"],
            "turn_id": form["turn_id"],
            "pair_id": form["pair_id"],
            "reason_codes": [
                "declared_deviation_misstates_address_metadata"
            ],
            "raw_address_form": form,
            "raw_anchor_deviation": row["anchor_deviations"][0],
        }
    ]
    assert validated["pronoun_realization_counts"] == {
        "overt": 1,
        "dropped": 0,
    }


def _legacy_test_address_metadata_quarantine_does_not_accept_foreign_turn_ids() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    row["address_forms_used"][0]["turn_id"] = "foreign_turn"

    with pytest.raises(
        B4TranslatorError,
        match="address form cites a foreign or repeated turn",
    ):
        validate_translation_window_response_v1(
            rendered=rendered,
            response=response,
        )


def test_t3_not_anchored_pair_has_no_default_downstream() -> None:
    rendered, _, _, _ = _render(1, anchored=False)
    response = _response(rendered)
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["address_metadata_collected"] is False
    decision = rendered.address_anchor["pair_decisions"][0]
    assert decision["pronoun_pair"] is None
    assert decision["not_anchored"]["reason"]


def test_t4_high_confidence_over_known_gap_emits_issue() -> None:
    rendered = render_address_anchor_request_v1(
        anchor_input=_anchor_input(gap=True),
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
    )
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": "bk_ch01",
        "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
        "pair_decisions": [
            {
                "pair_id": "P1",
                "pronoun_pair": deepcopy(PRONOUN_PAIR),
                "vocative_options": [{"form": VOCATIVE}],
                "register_shifts": [],
                "evidence_refs": ["bk_ch01_b001"],
                "model_confidence": "high",
                "not_anchored": None,
            }
        ],
    }
    validated = validate_address_anchor_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["review_issues"] == [
        {
            "issue_kind": "anchor_confidence_exceeds_evidence",
            "pair_id": PAIR_ID,
        }
    ]


def test_t7_noop_register_shift_is_removed_and_recorded() -> None:
    rendered = render_address_anchor_request_v1(
        anchor_input=_anchor_input(),
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
    )
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": "bk_ch01",
        "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
        "pair_decisions": [
            {
                "pair_id": "P1",
                "pronoun_pair": deepcopy(PRONOUN_PAIR),
                "vocative_options": [{"form": VOCATIVE}],
                "register_shifts": [
                    {
                        "register_cue": "formal",
                        "pronoun_pair": deepcopy(PRONOUN_PAIR),
                        "rationale": "No semantic change.",
                    }
                ],
                "evidence_refs": ["bk_ch01_b001"],
                "model_confidence": "medium",
                "not_anchored": None,
            }
        ],
    }
    validated = validate_address_anchor_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["pair_decisions"][0]["register_shifts"] == []
    assert validated["normalization_observations"] == [
        {
            "observation_kind": "noop_register_shift_removed",
            "pair_id": PAIR_ID,
            "register_cue": "formal",
        }
    ]


def _legacy_test_t8_missing_declared_forms_are_recorded() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    row["target_text"] = "Xin chào."
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert set(validated["anchor_deviation_by_rule"]) == {
        "declared_addressee_pronoun_absent_from_target",
        "declared_addressee_vocative_absent_from_target",
    }


def _legacy_test_t9_foreign_vocative_records_deviation_without_halting() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    row["address_forms_used"][0]["vocative_used"] = "ngài Heathcliff"
    row["address_forms_used"][0]["pronoun_realization"] = "dropped"
    row["target_text"] = "Ngài Heathcliff, xin mời vào."
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    validated_row = next(
        block for block in validated["blocks"] if block["block_id"] == row["block_id"]
    )
    assert validated_row["anchor_deviations"] == [
        {
            "turn_id": row["address_forms_used"][0]["turn_id"],
            "pair_id": PAIR_ID,
            "rule": "vocative_outside_anchor_options",
            "anchored_pronoun_pair": PRONOUN_PAIR,
            "used_pronoun_pair": PRONOUN_PAIR,
            "vocative": "ngài Heathcliff",
            "reason": "vocative_outside_anchor_options",
        }
    ]


def _legacy_test_t15_missing_overt_pronoun_is_recorded_after_stripping() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    row["target_text"] = "Xin chao."
    form = row["address_forms_used"][0]
    form["pronoun_realization"] = "overt"
    form["vocative_used"] = None
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["anchor_deviation_by_rule"] == {
        "declared_addressee_pronoun_absent_from_target": 1
    }


def _legacy_test_t16_shared_form_is_not_stripped_from_current_pair() -> None:
    rendered, story, anchor, window = _render(1)
    changed_anchor = deepcopy(anchor)
    changed_anchor.pop("artifact_hash")
    changed_anchor["pair_decisions"].append(
        {
            "pair_id": "pair_02",
            "pronoun_pair": deepcopy(PRONOUN_PAIR),
            "vocative_options": [{"form": PRONOUN_PAIR["addressee"]}],
            "register_shifts": [],
            "evidence_refs": ["bk_ch01_b001"],
            "model_confidence": "medium",
            "not_anchored": None,
        }
    )
    changed_anchor = _seal(changed_anchor)
    changed_pack = _rebind_pack(rendered.translator_pack, changed_anchor)
    rendered = render_translation_window_request_v1(
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
        translator_pack_bytes=_raw(changed_pack),
        address_anchor_bytes=_raw(changed_anchor),
        window_slice_bytes=_raw(window),
        chapter=_chapter(),
        accepted_tail_translations={},
    )
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    form = row["address_forms_used"][0]
    form["pronoun_pair"] = deepcopy(PRONOUN_PAIR)
    form["pronoun_realization"] = "overt"
    form["vocative_used"] = None
    row["target_text"] = f"Moi {PRONOUN_PAIR['addressee']} vao."

    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["pronoun_realization_counts"]["overt"] >= 1


def _legacy_test_t17_longest_other_form_is_removed_before_nested_form() -> None:
    rendered, story, anchor, window = _render(1)
    changed_anchor = deepcopy(anchor)
    changed_anchor.pop("artifact_hash")
    changed_anchor["pair_decisions"].append(
        {
            "pair_id": "pair_02",
            "pronoun_pair": {"speaker": "Lock", "addressee": "ngai"},
            "vocative_options": [
                {"form": f"{PRONOUN_PAIR['addressee']} Lockwood"}
            ],
            "register_shifts": [],
            "evidence_refs": ["bk_ch01_b001"],
            "model_confidence": "medium",
            "not_anchored": None,
        }
    )
    changed_anchor = _seal(changed_anchor)
    changed_pack = _rebind_pack(rendered.translator_pack, changed_anchor)
    rendered = render_translation_window_request_v1(
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
        translator_pack_bytes=_raw(changed_pack),
        address_anchor_bytes=_raw(changed_anchor),
        window_slice_bytes=_raw(window),
        chapter=_chapter(),
        accepted_tail_translations={},
    )
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    form = row["address_forms_used"][0]
    form["pronoun_pair"] = deepcopy(PRONOUN_PAIR)
    form["pronoun_realization"] = "overt"
    form["vocative_used"] = None
    row["target_text"] = (
        f"Dat ngua cho {PRONOUN_PAIR['addressee']} Lockwood."
    )
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["anchor_deviation_by_rule"] == {
        "declared_addressee_pronoun_absent_from_target": 1
    }


def _legacy_test_t18_dropped_with_vocative_records_only_pronoun_absence() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    form = row["address_forms_used"][0]
    form["pronoun_realization"] = "dropped"
    form["vocative_used"] = VOCATIVE
    row["target_text"] = f"{VOCATIVE}, xin moi vao."

    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["pronoun_realization_counts"]["dropped"] == 1
    assert validated["addressee_pronoun_absent_count"] == 1
    assert validated["address_marker_absent_count"] == 0
    assert [
        item["observation_kind"] for item in validated["validation_observations"]
    ] == ["addressee_pronoun_absent"]


def _legacy_test_t19_dropped_without_vocative_records_both_absences() -> None:
    rendered, _, _, _ = _render(1)
    response = _response(rendered)
    row = next(block for block in response["blocks"] if block["turn_refs"])
    form = row["address_forms_used"][0]
    form["pronoun_realization"] = "dropped"
    form["vocative_used"] = None
    row["target_text"] = "Dat ngua vao chuong."

    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert validated["addressee_pronoun_absent_count"] == 1
    assert validated["address_marker_absent_count"] == 1
    assert [
        item["observation_kind"] for item in validated["validation_observations"]
    ] == ["addressee_pronoun_absent", "address_marker_absent"]


def test_t5_tail_is_context_only_and_absent_from_output() -> None:
    rendered, _, _, _ = _render(2)
    response = _response(rendered)
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=response,
    )
    assert "bk_ch01_b002" in rendered.messages[-1]["content"]
    assert "bk_ch01_b002" not in {
        row["block_id"] for row in validated["blocks"]
    }


def test_t6_demo_arm_cannot_emit_reference_based_scores() -> None:
    rendered1, story, anchor, _ = _render(1)
    rendered2, _, _, _ = _render(2)
    validated1 = validate_translation_window_response_v1(
        rendered=rendered1,
        response=_response(rendered1),
    )
    validated2 = validate_translation_window_response_v1(
        rendered=rendered2,
        response=_response(rendered2),
    )
    artifact1 = build_translation_window_artifact_v1(
        validated_response=validated1,
        provider_receipt={"receipt_id": "w1"},
        provider_called=True,
    )
    artifact2 = build_translation_window_artifact_v1(
        validated_response=validated2,
        provider_receipt={"receipt_id": "w2"},
        provider_called=True,
    )
    plan = {
        "windows": [
            {
                "window_id": "window_1",
                "active_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            },
            {
                "window_id": "window_2",
                "active_block_ids": ["bk_ch01_b003", "bk_ch01_b004"],
            },
        ],
        "window_plan_hash": "2" * 64,
    }
    chapter = assemble_translation_chapter_v1(
        translator_pack=rendered1.translator_pack,
        address_anchor=anchor,
        window_plan=plan,
        window_artifacts=[artifact1, artifact2],
        chapter=_chapter(),
    )
    with pytest.raises(B4TranslatorError, match="measured_arm=true"):
        assert_reference_scoring_allowed_v1(chapter)


def test_chapter_assembly_omits_non_active_heading_block() -> None:
    rendered1, story, anchor, _ = _render(1)
    rendered2, _, _, _ = _render(2)
    artifacts = []
    for rendered in (rendered1, rendered2):
        validated = validate_translation_window_response_v1(
            rendered=rendered,
            response=_response(rendered),
        )
        artifacts.append(
            build_translation_window_artifact_v1(
                validated_response=validated,
                provider_receipt={"receipt_id": rendered.window_slice["window_id"]},
                provider_called=True,
            )
        )
    plan = {
        "windows": [
            {
                "window_id": "window_1",
                "active_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            },
            {
                "window_id": "window_2",
                "active_block_ids": ["bk_ch01_b003", "bk_ch01_b004"],
            },
        ],
        "window_plan_hash": "2" * 64,
    }
    chapter_source = _chapter()
    chapter_source["blocks"].insert(
        0,
        {"block_id": "bk_ch01_heading", "clean_text": "CHAPTER I"},
    )

    chapter = assemble_translation_chapter_v1(
        translator_pack=rendered1.translator_pack,
        address_anchor=anchor,
        window_plan=plan,
        window_artifacts=artifacts,
        chapter=chapter_source,
    )

    assert [row["block_id"] for row in chapter["blocks"]] == [
        "bk_ch01_b001",
        "bk_ch01_b002",
        "bk_ch01_b003",
        "bk_ch01_b004",
    ]


def _legacy_test_chapter_assembly_preserves_quarantined_address_metadata() -> None:
    rendered1, _, anchor, _ = _render(1)
    rendered2, _, _, _ = _render(2)
    response1 = _response(rendered1)
    row = next(block for block in response1["blocks"] if block["turn_refs"])
    form = row["address_forms_used"][0]
    row["anchor_deviations"] = [
        {
            "turn_id": form["turn_id"],
            "pair_id": form["pair_id"],
            "anchored_pronoun_pair": deepcopy(PRONOUN_PAIR),
            "used_pronoun_pair": deepcopy(form["pronoun_pair"]),
            "vocative": "different vocative",
            "reason": "The prose used a different vocative.",
        }
    ]
    artifacts = []
    for rendered, response in (
        (rendered1, response1),
        (rendered2, _response(rendered2)),
    ):
        validated = validate_translation_window_response_v1(
            rendered=rendered,
            response=response,
        )
        artifacts.append(
            build_translation_window_artifact_v1(
                validated_response=validated,
                provider_receipt=None,
                provider_called=False,
            )
        )
    plan = {
        "windows": [
            {
                "window_id": "window_1",
                "active_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            },
            {
                "window_id": "window_2",
                "active_block_ids": ["bk_ch01_b003", "bk_ch01_b004"],
            },
        ],
        "window_plan_hash": "2" * 64,
    }

    chapter = assemble_translation_chapter_v1(
        translator_pack=rendered1.translator_pack,
        address_anchor=anchor,
        window_plan=plan,
        window_artifacts=artifacts,
        chapter=_chapter(),
    )

    assert chapter["address_metadata_unverified_count"] == 1
    assert chapter["address_metadata_unverified_turn_ids"] == [
        form["turn_id"]
    ]
    assert chapter["quarantined_address_metadata"][0]["raw_address_form"] == form
