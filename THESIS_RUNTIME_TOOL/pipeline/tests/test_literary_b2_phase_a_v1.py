from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from pipeline.literary.b2_context_v1 import (
    B2ContextBudgetError,
    B2ContextError,
    build_b2_windows_v1,
    build_candidate_packet_v1,
    load_b2_phase_a_profile,
    load_real_b1_run_input_v1,
    render_b2_frame_request_v1,
    render_b2_interaction_request_v1,
)
from pipeline.literary.b2_contract_v1 import (
    B2ContractError,
    normalize_b2_frame_response_v1,
    normalize_b2_interaction_response_v1,
)
from pipeline.literary.b2_phase_a_v1 import build_b2_phase_a_bundle_v1
from pipeline.literary.b2_prompts_v1 import (
    B2_FRAME_SYSTEM_PROMPT,
    B2_INTERACTION_SYSTEM_PROMPT,
    b2_frame_response_schema,
    b2_interaction_response_schema,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    build_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_b2_phase_a_profile_v1.json"
)


def _profile():
    return load_b2_phase_a_profile(PROFILE_PATH)


def _chapter() -> dict:
    return {
        "chapter_id": "book_ch01",
        "blocks": [
            {
                "block_id": "book_ch01_h001",
                "order_index": 0,
                "block_type": "heading",
                "clean_text": "Chapter One",
            },
            {
                "block_id": "book_ch01_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr. Vale greeted Robin at North House.",
            },
            {
                "block_id": "book_ch01_b002",
                "order_index": 2,
                "block_type": "dialogue",
                "clean_text": '"Robin, come here," said Mr. Vale.',
            },
            {
                "block_id": "book_ch01_b003",
                "order_index": 3,
                "block_type": "paragraph",
                "clean_text": "His hand withdrew as Robin refused.",
            },
        ],
    }


def _card(
    card_id: str,
    canonical_surface: str,
    *,
    stable_surfaces: list[str] | None = None,
    authority_scope: str = "chapter_confirmed",
    block_id: str = "book_ch01_b001",
    disputed: bool = False,
) -> dict:
    disputes = (
        [
            {
                "disputed_field": "referential_gender",
                "status": "pending",
                "pending_reason_codes": ["conflicting_evidence"],
                "next_review_trigger": "new_source_evidence",
                "hearing_count": 1,
            }
        ]
        if disputed
        else []
    )
    return {
        "prior_card_id": card_id,
        "canonical_surface": canonical_surface,
        "stable_surfaces": stable_surfaces or [canonical_surface],
        "authority_scope": authority_scope,
        "effective_claims": {
            "referent_kind": "person",
            "referential_gender": "masculine",
            "identity_summary": "A named resident associated with the house.",
        },
        "disputed_claims": disputes,
        "first_supported_block_id": block_id,
        "provenance_refs": [
            {"chapter_id": "book_ch01", "block_id": block_id}
        ],
    }


def _prefix(*cards: dict, candidate_only: list[dict] | None = None) -> dict:
    return {
        "prefix_bundle_hash": "prefix_" + "a" * 57,
        "b0_context_cards": list(cards),
        "candidate_only_context_cards": list(candidate_only or []),
        "prefix_identity_uncertainties": [],
    }


def _frame_request(prefix: dict | None = None) -> dict:
    return render_b2_frame_request_v1(
        chapter=_chapter(),
        prefix_bundle=prefix or _prefix(_card("card_vale", "Mr. Vale", stable_surfaces=["Mr. Vale", "Vale"])),
        profile=_profile(),
    )


def _interaction_request(prefix: dict | None = None) -> tuple[dict, dict]:
    chapter = _chapter()
    windows = build_b2_windows_v1(chapter, profile=_profile())
    request = render_b2_interaction_request_v1(
        window=windows[0],
        prefix_bundle=prefix
        or _prefix(_card("card_vale", "Mr. Vale", stable_surfaces=["Mr. Vale", "Vale"])),
        profile=_profile(),
        frame_context={"frame_segments": []},
    )
    return windows[0], request


def _endpoint(
    *,
    surface: str | None,
    status: str,
    candidate_ids: list[str],
    reference_form: str = "proper_name",
) -> dict:
    return {
        "surface": surface,
        "reference_form": reference_form,
        "resolution_status": status,
        "candidate_card_ids": candidate_ids,
        "attribution_method": "nearby_context",
    }


def _frame_response(*, starts: list[dict] | None = None) -> dict:
    return {
        "schema_version": "literary_b2_frame_response_v1",
        "chapter_id": "book_ch01",
        "chapter_orientation": {
            "chapter_gist": "A visitor meets a resident and receives a refusal.",
            "narrative_mode": "first_person_character",
            "setting_surfaces": ["North House"],
        },
        "frame_starts": list(starts or []),
        "review_requests": [],
    }


def _interaction_response(
    *, turns: list[dict] | None = None, events: list[dict] | None = None
) -> dict:
    return {
        "schema_version": "literary_b2_interaction_response_v1",
        "chapter_id": "book_ch01",
        "window_id": "b2w1_book_ch01_01",
        "speaker_turns": list(turns or []),
        "interaction_events": list(events or []),
        "review_requests": [],
    }


def _turn(
    *,
    block_id: str = "book_ch01_b002",
    anchor: str = '"Robin, come here,"',
    speaker: dict | None = None,
) -> dict:
    return {
        "block_id": block_id,
        "utterance_anchor": anchor,
        "speaker": speaker
        or _endpoint(
            surface="Mr. Vale",
            status="resolved_candidate",
            candidate_ids=["card_vale"],
        ),
        "addressee": _endpoint(
            surface="Robin",
            status="unresolved",
            candidate_ids=[],
        ),
        "address_terms": ["Robin"],
        "register_cue": "neutral",
    }


def test_prompts_are_book_neutral_and_schemas_are_closed() -> None:
    prompt = B2_FRAME_SYSTEM_PROMPT + B2_INTERACTION_SYSTEM_PROMPT
    assert "candidate cards are hints" in prompt.casefold()
    for forbidden in ("wuthering", "heathcliff", "catherine", "gatsby"):
        assert forbidden not in prompt.casefold()
    assert b2_frame_response_schema()["additionalProperties"] is False
    assert b2_interaction_response_schema()["additionalProperties"] is False


def test_windows_exact_cover_active_blocks_and_tail_is_read_only() -> None:
    profile = replace(
        _profile(),
        target_active_source_tokens=8,
        max_active_blocks=1,
        preceding_tail_blocks=1,
    )
    windows = build_b2_windows_v1(_chapter(), profile=profile)
    assert [block for row in windows for block in row["active_block_ids"]] == [
        "book_ch01_b001",
        "book_ch01_b002",
        "book_ch01_b003",
    ]
    assert windows[1]["preceding_tail_block_ids"] == ["book_ch01_b001"]


def test_candidate_packet_groups_repeated_surface_and_preserves_collision() -> None:
    blocks = [
        {
            "block_id": "book_ch01_b001",
            "clean_text": "Vale met Vale.",
            "block_type": "paragraph",
        }
    ]
    prefix = _prefix(
        _card("card_one", "Mr. Vale", stable_surfaces=["Mr. Vale", "Vale"]),
        _card("card_two", "Mrs. Vale", stable_surfaces=["Mrs. Vale", "Vale"]),
    )
    packet = build_candidate_packet_v1(
        chapter_id="book_ch01",
        active_blocks=blocks,
        tail_blocks=[],
        prefix_bundle=prefix,
        candidate_card_cap=8,
        profile=_profile(),
    )
    vale = next(row for row in packet["surface_groups"] if row["source_surface"] == "Vale")
    assert vale["block_ids"] == ["book_ch01_b001"]
    assert vale["candidate_card_ids"] == ["card_one", "card_two"]
    assert len(packet["candidate_cards"]) == 2


def test_candidate_only_stays_visible_and_disputed_claim_loses_authority() -> None:
    pending = _card(
        "card_pending",
        "Robin",
        authority_scope="candidate_only",
        disputed=True,
    )
    packet = build_candidate_packet_v1(
        chapter_id="book_ch01",
        active_blocks=_chapter()["blocks"][1:],
        tail_blocks=[],
        prefix_bundle=_prefix(candidate_only=[pending]),
        candidate_card_cap=8,
        profile=_profile(),
    )
    rendered = packet["candidate_cards"][0]
    assert rendered["authority_scope"] == "candidate_only"
    assert "referential_gender" not in rendered["effective_claims_as_of"]
    assert rendered["uncertainty_flags"][0]["status"] == "pending"


def test_unmatched_registry_card_is_not_dumped_into_request_context() -> None:
    matched = _card("card_vale", "Mr. Vale")
    unrelated = _card(
        "card_far",
        "Professor Alder",
        block_id="book_ch99_b999",
    )
    packet = build_candidate_packet_v1(
        chapter_id="book_ch01",
        active_blocks=_chapter()["blocks"][1:],
        tail_blocks=[],
        prefix_bundle=_prefix(matched, unrelated),
        candidate_card_cap=8,
        profile=_profile(),
    )
    assert [row["candidate_card_id"] for row in packet["candidate_cards"]] == [
        "card_vale"
    ]


def test_prior_frame_candidate_is_included_once_without_becoming_a_binding() -> None:
    narrator = _card(
        "card_narrator",
        "Morgan Reed",
        block_id="book_ch00_b001",
    )
    source = {
        "candidate_card_id": "card_narrator",
        "source_kind": "prior_frame_narrator_candidate",
        "source_chapter_id": "book_ch00",
        "source_artifact_hash": "f" * 64,
        "source_frame_segment_ids": ["frame_2", "frame_1", "frame_1"],
    }
    packet = build_candidate_packet_v1(
        chapter_id="book_ch01",
        active_blocks=[
            {
                "block_id": "book_ch01_b001",
                "clean_text": "I entered before dawn.",
                "block_type": "paragraph",
            }
        ],
        tail_blocks=[],
        prefix_bundle=_prefix(narrator),
        candidate_card_cap=8,
        profile=_profile(),
        supplemental_candidate_sources=[source],
    )

    assert [row["candidate_card_id"] for row in packet["candidate_cards"]] == [
        "card_narrator"
    ]
    assert packet["surface_groups"] == []
    assert packet["context_candidate_sources"] == [
        {
            **source,
            "source_frame_segment_ids": ["frame_1", "frame_2"],
        }
    ]


def test_prior_frame_candidate_must_exist_in_current_prefix() -> None:
    with pytest.raises(B2ContextError, match="absent from the current prefix"):
        build_candidate_packet_v1(
            chapter_id="book_ch01",
            active_blocks=_chapter()["blocks"][1:],
            tail_blocks=[],
            prefix_bundle=_prefix(),
            candidate_card_cap=8,
            profile=_profile(),
            supplemental_candidate_sources=[
                {
                    "candidate_card_id": "foreign_card",
                    "source_kind": "prior_frame_narrator_candidate",
                    "source_chapter_id": "book_ch00",
                    "source_artifact_hash": "f" * 64,
                    "source_frame_segment_ids": ["frame_1"],
                }
            ],
        )


def test_current_frame_narrator_card_reaches_interaction_packet() -> None:
    profile = _profile()
    window = build_b2_windows_v1(_chapter(), profile=profile)[0]
    request = render_b2_interaction_request_v1(
        window=window,
        prefix_bundle=_prefix(_card("card_vale", "Mr. Vale")),
        profile=profile,
        frame_context={
            "frame_artifact_hash": "a" * 64,
            "applicable_segments": [
                {
                    "frame_segment_id": "frame_1",
                    "candidate_card_ids": ["card_vale"],
                }
            ],
        },
    )
    payload = json.loads(request["messages"][1]["content"])
    sources = payload["candidate_packets"]["context_candidate_sources"]
    assert sources[0]["source_kind"] == "current_frame_narrator_candidate"
    assert sources[0]["candidate_card_id"] == "card_vale"


def test_empty_frame_proposal_becomes_unknown_exact_cover_plus_review() -> None:
    artifact = normalize_b2_frame_response_v1(
        request=_frame_request(), response=_frame_response()
    )
    segment = artifact["frame_segments"][0]
    assert segment["narrator_status"] == "unknown"
    assert segment["covered_block_ids"] == [
        "book_ch01_b001",
        "book_ch01_b002",
        "book_ch01_b003",
    ]
    assert any(row["review_kind"] == "missing_initial_frame" for row in artifact["review_requests"])


def test_frame_switch_uses_start_points_and_code_derives_segment_ends() -> None:
    starts = [
        {
            "start_block_id": "book_ch01_b001",
            "narrator_surface": None,
            "narrator_status": "external_or_authorial",
            "candidate_card_ids": [],
            "story_time_label": "frame_present",
            "boundary_reason": "The chapter begins in the outer frame.",
        },
        {
            "start_block_id": "book_ch01_b003",
            "narrator_surface": None,
            "narrator_status": "unknown",
            "candidate_card_ids": [],
            "story_time_label": "retrospective_past",
            "boundary_reason": "The narration moves to a prior event.",
        },
    ]
    artifact = normalize_b2_frame_response_v1(
        request=_frame_request(), response=_frame_response(starts=starts)
    )
    assert [(row["start_block_id"], row["end_block_id"]) for row in artifact["frame_segments"]] == [
        ("book_ch01_b001", "book_ch01_b002"),
        ("book_ch01_b003", "book_ch01_b003"),
    ]


def test_conflicting_frame_rows_remain_pending_with_raw_alternatives() -> None:
    base = {
        "start_block_id": "book_ch01_b001",
        "narrator_surface": None,
        "narrator_status": "external_or_authorial",
        "candidate_card_ids": [],
        "story_time_label": "frame_present",
        "boundary_reason": "An external voice opens the chapter.",
    }
    conflict = {
        **base,
        "narrator_status": "unknown",
        "story_time_label": "retrospective_past",
        "boundary_reason": "The time layer cannot be resolved.",
    }
    artifact = normalize_b2_frame_response_v1(
        request=_frame_request(),
        response=_frame_response(starts=[base, conflict]),
    )
    segment = artifact["frame_segments"][0]
    assert segment["narrator_status"] == "pending_conflict"
    assert len(segment["raw_alternatives"]) == 2
    assert any(row["review_kind"] == "frame_row_conflict" for row in artifact["review_requests"])


def test_frame_foreign_candidate_fails_closed() -> None:
    start = {
        "start_block_id": "book_ch01_b001",
        "narrator_surface": "Vale",
        "narrator_status": "resolved_candidate",
        "candidate_card_ids": ["foreign_card"],
        "story_time_label": "frame_present",
        "boundary_reason": "The voice is named.",
    }
    with pytest.raises(B2ContractError, match="foreign candidate"):
        normalize_b2_frame_response_v1(
            request=_frame_request(), response=_frame_response(starts=[start])
        )


def test_unresolved_pronoun_is_retained_without_new_entity_authority() -> None:
    _window, request = _interaction_request()
    turn = _turn(
        speaker=_endpoint(
            surface="he",
            status="unresolved",
            candidate_ids=[],
            reference_form="pronoun",
        )
    )
    artifact = normalize_b2_interaction_response_v1(
        request=request, response=_interaction_response(turns=[turn])
    )
    assert artifact["speaker_turns"][0]["speaker"]["resolution_status"] == "unresolved"


def test_body_part_cannot_smuggle_a_new_entity_table() -> None:
    _window, request = _interaction_request()
    response = _interaction_response()
    response["new_entities"] = [{"surface": "His hand"}]
    with pytest.raises(B2ContractError, match="violates response schema"):
        normalize_b2_interaction_response_v1(request=request, response=response)


def test_unlocatable_anchor_is_retained_for_review() -> None:
    _window, request = _interaction_request()
    artifact = normalize_b2_interaction_response_v1(
        request=request,
        response=_interaction_response(turns=[_turn(anchor="not present in source")]),
    )
    assert len(artifact["speaker_turns"]) == 1
    assert artifact["speaker_turns"][0]["grounding_status"] == "review_required_unlocatable"
    assert any(row["review_kind"] == "unlocatable_anchor" for row in artifact["review_requests"])


def test_foreign_candidate_and_tail_owned_row_fail_closed() -> None:
    profile = replace(
        _profile(), target_active_source_tokens=8, max_active_blocks=1, preceding_tail_blocks=1
    )
    windows = build_b2_windows_v1(_chapter(), profile=profile)
    request = render_b2_interaction_request_v1(
        window=windows[1],
        prefix_bundle=_prefix(_card("card_vale", "Mr. Vale", stable_surfaces=["Mr. Vale", "Vale"])),
        profile=profile,
        frame_context={"frame_segments": []},
    )
    foreign = _turn(
        speaker=_endpoint(
            surface="Vale",
            status="resolved_candidate",
            candidate_ids=["foreign"],
        )
    )
    foreign["block_id"] = windows[1]["active_block_ids"][0]
    foreign["utterance_anchor"] = _chapter()["blocks"][2]["clean_text"]
    foreign_response = _interaction_response(turns=[foreign])
    foreign_response["window_id"] = request["window_id"]
    with pytest.raises(B2ContractError, match="foreign candidate"):
        normalize_b2_interaction_response_v1(
            request=request, response=foreign_response
        )
    tail_owned = _turn(block_id=windows[1]["preceding_tail_block_ids"][0])
    tail_response = _interaction_response(turns=[tail_owned])
    tail_response["window_id"] = request["window_id"]
    with pytest.raises(B2ContractError, match="tail block"):
        normalize_b2_interaction_response_v1(
            request=request, response=tail_response
        )


def test_endpoint_cardinality_conflict_stays_pending_instead_of_halting() -> None:
    prefix = _prefix(
        _card("card_one", "Mr. Vale", stable_surfaces=["Vale"]),
        _card("card_two", "Mrs. Vale", stable_surfaces=["Vale"]),
    )
    _window, request = _interaction_request(prefix)
    turn = _turn(
        speaker=_endpoint(
            surface="Vale",
            status="resolved_candidate",
            candidate_ids=["card_one", "card_two"],
        )
    )
    artifact = normalize_b2_interaction_response_v1(
        request=request, response=_interaction_response(turns=[turn])
    )
    assert artifact["speaker_turns"][0]["speaker"]["resolution_status"] == "pending_contract_conflict"
    assert artifact["speaker_turns"][0]["row_status"] == "review_required_endpoint_contract"


def test_exact_duplicates_collapse_but_conflicts_remain_visible() -> None:
    _window, request = _interaction_request()
    first = _turn()
    conflicting = deepcopy(first)
    conflicting["register_cue"] = "hostile"
    artifact = normalize_b2_interaction_response_v1(
        request=request,
        response=_interaction_response(turns=[first, deepcopy(first), conflicting]),
    )
    assert len(artifact["speaker_turns"]) == 2
    assert all(row["row_status"] == "review_required_conflicting_rows" for row in artifact["speaker_turns"])
    assert any(row["review_kind"] == "speaker_turn_conflict" for row in artifact["review_requests"])


def test_request_tamper_fails_and_render_is_deterministic_input_immutable() -> None:
    chapter = _chapter()
    prefix = _prefix(_card("card_vale", "Mr. Vale", stable_surfaces=["Mr. Vale", "Vale"]))
    chapter_before = deepcopy(chapter)
    prefix_before = deepcopy(prefix)
    first = render_b2_frame_request_v1(chapter=chapter, prefix_bundle=prefix, profile=_profile())
    second = render_b2_frame_request_v1(chapter=chapter, prefix_bundle=prefix, profile=_profile())
    assert first == second
    assert chapter == chapter_before and prefix == prefix_before
    tampered = deepcopy(first)
    tampered["chapter_id"] = "foreign_chapter"
    with pytest.raises(B2ContractError, match="fingerprint mismatch"):
        normalize_b2_frame_response_v1(request=tampered, response=_frame_response())


def test_overflow_splits_mechanically_without_truncating_candidates() -> None:
    chapter = _chapter()
    chapter["blocks"] = chapter["blocks"][:3]
    chapter["blocks"][1]["clean_text"] = "Mr. Vale entered alone."
    chapter["blocks"][2]["clean_text"] = "Robin waited outside."
    prefix = _prefix(
        _card("card_one", "Mr. Vale", block_id="book_ch01_b001"),
        _card("card_two", "Robin", block_id="book_ch01_b002"),
    )
    profile = replace(
        _profile(),
        interaction_candidate_card_cap=1,
        target_active_source_tokens=10_000,
        max_active_blocks=28,
        preceding_tail_blocks=0,
    )
    real_input = {
        "input_hash": "input_hash",
        "source_run_root": "source",
        "source_plan_hash": "plan",
        "source_summary_hash": "summary",
        "source_document_sha256": "document",
        "source_run_git_head": "head",
        "current_git_head": "head",
        "certification_eligible": True,
        "certification_blockers": [],
        "ordered_chapter_ids": ["book_ch01"],
        "chapters": [
            {
                "chapter_id": "book_ch01",
                "chapter_ordinal": 1,
                "chapter": chapter,
                "prefix_bundle": prefix,
                "prefix_bundle_hash": prefix["prefix_bundle_hash"],
            }
        ],
    }
    bundle = build_b2_phase_a_bundle_v1(real_input=real_input, profile=profile)
    rows = bundle["plan"]["chapters"][0]["interaction_requests"]
    assert len(rows) == 2
    assert [block for row in rows for block in row["active_block_ids"]] == [
        "book_ch01_b001",
        "book_ch01_b002",
    ]


def test_one_block_candidate_overflow_pauses_without_truncation() -> None:
    prefix = _prefix(
        _card("card_one", "Mr. Vale", stable_surfaces=["Vale"]),
        _card("card_two", "Mrs. Vale", stable_surfaces=["Vale"]),
    )
    profile = replace(_profile(), interaction_candidate_card_cap=1)
    window = {
        "window_id": "b2w1_book_ch01_01",
        "window_hash": "a" * 64,
        "chapter_id": "book_ch01",
        "active_blocks": [_chapter()["blocks"][1]],
        "preceding_tail": [],
    }
    with pytest.raises(B2ContextBudgetError, match="candidate context overflow"):
        render_b2_interaction_request_v1(
            window=window,
            prefix_bundle=prefix,
            profile=profile,
            frame_context=None,
        )


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
        "alternative_names": [
            {
                "surface": "Vale",
                "name_class": "proper_name",
                "source_block_ids": ["book_ch01_b001"],
            }
        ],
        "name_locations": [
            {
                "surface": "Mr. Vale",
                "name_class": "title_plus_name",
                "source_block_ids": ["book_ch01_b001"],
            },
            {
                "surface": "Vale",
                "name_class": "proper_name",
                "source_block_ids": ["book_ch01_b001"],
            },
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
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _fake_real_run(
    tmp_path: Path,
    *,
    source_head: str,
    status: str = "complete",
    sealed_chapter_count: int = 1,
) -> Path:
    chapters = [_chapter()]
    if sealed_chapter_count == 2:
        chapters.append(
            {
                **_chapter(),
                "chapter_id": "book_ch02",
                "blocks": [
                    {
                        **row,
                        "block_id": str(row["block_id"]).replace(
                            "book_ch01", "book_ch02"
                        ),
                    }
                    for row in _chapter()["blocks"]
                ],
            }
        )
    document = {"document_id": "book", "chapters": chapters}
    document_path = tmp_path / "document.json"
    _write_json(document_path, document)
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_audited_inventory(),
        coverage_through_chapter_id="book_ch01",
    )
    root = tmp_path / "run"
    report_path = root / "artifacts" / "chapters" / "ch001" / "chapter_report.json"
    prefix_path = report_path.parent / "final_prefix.json"
    _write_json(prefix_path, prefix)
    report_body = {
        "chapter_id": "book_ch01",
        "b2_enabled": False,
        "b2_ready": False,
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_json(report_path, report)
    plan_body = {
        "document_path": str(document_path.resolve()),
        "document_sha256": file_sha256(document_path),
        "ordered_chapter_ids": [
            row["chapter_id"] for row in chapters
        ],
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    _write_json(root / "run_plan.json", plan)
    summary_body = {
        "status": status,
        "production_publish_performed": False,
        "b2": {"enabled": False},
        "plan_hash": plan["plan_hash"],
        "completed_chapter_ids": ["book_ch01"],
        "chapter_reports": [
            {
                "chapter_id": "book_ch01",
                "path": report_path.relative_to(root).as_posix(),
                "report_hash": report["report_hash"],
            }
        ],
    }
    summary = {**summary_body, "summary_hash": canonical_hash(summary_body)}
    _write_json(root / "run_summary.json", summary)
    _write_json(
        root / "stages" / "stage1" / "live" / "run_envelope_001.json",
        {"git_head": source_head},
    )
    return root


def test_real_run_loader_uses_actual_prefix_and_marks_head_mismatch_only(tmp_path: Path) -> None:
    root = _fake_real_run(tmp_path, source_head="old_head")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    loaded = load_real_b1_run_input_v1(root, current_git_head="current_head")
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert before == after
    assert loaded["certification_eligible"] is False
    assert loaded["certification_blockers"] == ["source_run_head_differs_from_current_head"]
    assert loaded["chapters"][0]["prefix_bundle_hash"] == loaded["chapters"][0]["prefix_bundle"]["prefix_bundle_hash"]


def test_real_run_loader_is_certification_eligible_only_on_same_head(tmp_path: Path) -> None:
    root = _fake_real_run(tmp_path, source_head="same_head")
    loaded = load_real_b1_run_input_v1(root, current_git_head="same_head")
    assert loaded["certification_eligible"] is True
    assert loaded["certification_blockers"] == []


def test_real_run_loader_accepts_stopped_completed_prefix(tmp_path: Path) -> None:
    root = _fake_real_run(
        tmp_path,
        source_head="same_head",
        status="stopped",
        sealed_chapter_count=2,
    )
    loaded = load_real_b1_run_input_v1(root, current_git_head="same_head")

    assert loaded["ordered_chapter_ids"] == ["book_ch01"]
    assert loaded["sealed_chapter_ids"] == ["book_ch01", "book_ch02"]
    assert [row["chapter_id"] for row in loaded["chapters"]] == ["book_ch01"]


def test_real_run_loader_rejects_complete_partial_prefix(tmp_path: Path) -> None:
    root = _fake_real_run(
        tmp_path,
        source_head="same_head",
        status="complete",
        sealed_chapter_count=2,
    )
    with pytest.raises(
        B2ContextError,
        match="completed sealed prefix",
    ):
        load_real_b1_run_input_v1(root, current_git_head="same_head")


def test_profile_keeps_semantic_pending_nonfatal_and_integrity_fail_closed(
    tmp_path: Path,
) -> None:
    profile = _profile()
    assert profile.safety == {
        "semantic_ambiguity_action": "persist_pending_and_continue",
        "integrity_failure_action": "pause_before_next_api_call",
        "candidate_overflow_action": "split_then_pause_if_single_block",
        "gold_in_runtime_request_allowed": False,
        "production_publish_enabled": False,
    }
    broken = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    broken["safety"]["gold_in_runtime_request_allowed"] = True
    temp = tmp_path / "forbidden_b2_profile.json"
    temp.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(B2ContextError, match="safety contract"):
        load_b2_phase_a_profile(temp)
