from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import shutil
from typing import Any

import pytest

from pipeline.literary import b4_handoff_v3 as handoff
from pipeline.literary.b4_handoff_v3 import (
    B4HandoffError,
    assemble_b4_input_bundle,
    build_book_source_manifest,
    build_complete_ground_evidence,
    build_occurrence_cards,
    build_occurrence_routing_view,
    load_verified_builder_v3_inputs,
    state_lineage_id_for_manifest,
    verify_b4_input_bundle_identity,
)
from pipeline.literary.builder_v3_pipeline import (
    SyntheticStageExecutor,
    run_m1_v3,
    run_m2_v3,
)
from pipeline.literary.checkpoint import CheckpointError, canonical_hash, canonical_json


NAMES = [("Alice", "Bob"), ("Mira", "Ravel"), ("Iris", "Noel")]
GROUND_CHANNELS = {
    "glossary_inputs",
    "dialogue_turn_inputs",
    "relation_event_inputs",
    "phase_observation_inputs",
    "state_change_inputs",
    "unresolved_thread_inputs",
    "translator_fact_inputs",
    "motif_inputs",
    "rolling_summary_inputs",
    "frame_claim_inputs",
    "frame_leaf_index",
}


def _chapter_text(number: int) -> str:
    first, second = NAMES[number - 1]
    padding = "quiet hearthside detail " * 20
    return (
        f"The canine mother, known in this scene as Madam, remained an animal by "
        f"the hearth. {padding}{first} greeted {second} beside the chamber door. "
        f'Later {first} said, "{second}, stay."'
    )


def _chapter(number: int) -> dict[str, Any]:
    chapter_id = f"bk_ch{number:02d}"
    first, second = NAMES[number - 1]
    return {
        "chapter_id": chapter_id,
        "chapter_label": f"Chapter {number}",
        "blocks": [
            {
                "block_id": f"{chapter_id}_b001",
                "block_type": "paragraph",
                "order_index": number * 100 + 1,
                "clean_text": _chapter_text(number),
                "source_text": _chapter_text(number),
            },
            {
                "block_id": f"{chapter_id}_b002",
                "block_type": "paragraph",
                "order_index": number * 100 + 2,
                "clean_text": f"{first} and {second} left the room.",
                "source_text": f"{first} and {second} left the room.",
            },
            {
                "block_id": f"{chapter_id}_b003",
                "block_type": "paragraph",
                "order_index": number * 100 + 3,
                "clean_text": "A silent frame-only passage contains no character occurrence.",
                "source_text": "A silent frame-only passage contains no character occurrence.",
            },
            {
                "block_id": f"{chapter_id}_b004",
                "block_type": "heading",
                "order_index": number * 100 + 4,
                "clean_text": "Cafe\u0301 source heading",
                "source_text": "Cafe\u0301 source heading",
            },
        ],
    }


def _document() -> dict[str, Any]:
    return {
        "document_id": "b4-handoff-v3-fixture",
        "chapters": [_chapter(number) for number in range(1, 4)],
    }


def _endpoint(
    surface: str,
    evidence: str,
    mention_ref: str,
    *,
    occurrence_hint: int = 1,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "reference_scope": "individual",
        "referent_kind_claim": "person",
        "mention_ref": mention_ref,
        "attribution_method": "explicit_tag",
        "anchor_text": surface,
        "evidence_quote": evidence,
        "occurrence_hint": occurrence_hint,
    }


def _script_for_chapter(number: int) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    chapter_id = f"bk_ch{number:02d}"
    block_1 = f"{chapter_id}_b001"
    block_2 = f"{chapter_id}_b002"
    block_3 = f"{chapter_id}_b003"
    block_4 = f"{chapter_id}_b004"
    first, second = NAMES[number - 1]
    text = _chapter_text(number)
    dog_clause = "The canine mother, known in this scene as Madam, remained an animal by the hearth."
    event_clause = f"{first} greeted {second} beside the chamber door."
    utterance = f'Later {first} said, "{second}, stay."'
    mention_madam = f"m_{block_1}_01"
    mention_first = f"m_{block_1}_02"
    mention_second = f"m_{block_1}_03"
    mention_door = f"m_{block_1}_04"
    turn_id = f"t_{block_1}_01"
    event_id = f"e_{block_1}_01"
    return {
        ("b0", chapter_id, None): {
            "chapter_id": chapter_id,
            "cast_claims": [
                {
                    "surface": "The canine mother",
                    "surface_kind": "descriptor",
                    "referent_kind_claim": "animal",
                    "role_hint": "animal in the room",
                    "scene_range": [block_1, block_2],
                    "source_block_ids": [block_1],
                    "anchor_text": "The canine mother",
                    "evidence_quote": dog_clause,
                }
            ],
            "setting": {
                "place": "an unnamed room",
                "time_frame_hint": "frame_present",
                "scene_shape": "single_scene_one_location",
            },
            "scenes_party_size": [
                {
                    "block_range": [block_1, block_2],
                    "co_present_count": 3,
                    "participants": [first, second, "Madam"],
                },
                {
                    "block_range": [block_3, block_3],
                    "co_present_count": 0,
                    "participants": [],
                },
            ],
            "neutral_premise": f"{first} and {second} meet in a room.",
        },
        ("b1", chapter_id, f"w_{chapter_id}_01"): {
            "chapter_id": chapter_id,
            "window_block_ids": [block_1, block_2, block_3, block_4],
            "context_only_used": False,
            "character_mentions": [
                {
                    "surface": "Madam",
                    "mention_type": "descriptor",
                    "referent_kind_claim": "animal",
                    "anchor_text": "Madam",
                    "evidence_quote": dog_clause,
                    "block_id": block_1,
                },
                {
                    "surface": first,
                    "mention_type": "name",
                    "referent_kind_claim": "person",
                    "anchor_text": first,
                    "evidence_quote": event_clause,
                    "block_id": block_1,
                    "occurrence_hint": 1,
                },
                {
                    "surface": second,
                    "mention_type": "name",
                    "referent_kind_claim": "person",
                    "anchor_text": second,
                    "evidence_quote": event_clause,
                    "block_id": block_1,
                    "occurrence_hint": 1,
                },
                {
                    "surface": "chamber door",
                    "mention_type": "descriptor",
                    "referent_kind_claim": "object",
                    "anchor_text": "chamber door",
                    "evidence_quote": event_clause,
                    "block_id": block_1,
                },
            ],
            "glossary_candidates": [
                {
                    "source_term": "chamber door",
                    "proposed_target_vi": "cua phong",
                    "category": "object",
                    "do_not_translate": False,
                    "block_ids": [block_1],
                }
            ],
        },
        ("b2", chapter_id, f"w_{chapter_id}_01"): {
            "chapter_id": chapter_id,
            "window_block_ids": [block_1, block_2, block_3, block_4],
            "context_only_used": False,
            "speaker_turns": [
                {
                    "speaker": _endpoint(first, utterance, mention_first, occurrence_hint=2),
                    "addressee": _endpoint(second, utterance, mention_second, occurrence_hint=2),
                    "utterance_quote": utterance,
                    "address_terms": [
                        {
                            "anchor_text": second,
                            "evidence_quote": f'"{second}, stay."',
                            "addressee_ref": "addressee",
                        }
                    ],
                    "register_cue": "neutral",
                    "block_id": block_1,
                }
            ],
            "relation_events": [
                {
                    "actor": _endpoint(first, event_clause, mention_first),
                    "target": _endpoint(second, event_clause, mention_second),
                    "event_type": "greets",
                    "evidence_quote": event_clause,
                    "block_id": block_1,
                }
            ],
        },
        ("b3", chapter_id, None): {
            "chapter_id": chapter_id,
            "chapter_rolling_summary": f"{first} greets {second} in chapter {number}.",
            "narration_frame_segments": [
                {
                    "local_segment_key": "present",
                    "parent_local_key": None,
                    "narrator_surface": first,
                    "narrator_ref": mention_first,
                    "frame_kind": "primary_narration",
                    "story_time_label": "frame_present",
                    "block_range": [block_1, block_3],
                    "start_boundary": None,
                    "end_boundary": None,
                    "status": "proposed",
                    "evidence_quote": event_clause,
                }
            ],
            "relation_observations": [
                {
                    "event_id": event_id,
                    "endpoint_refs": [f"{event_id}#actor", f"{event_id}#target"],
                    "observed_valence_hint": "positive",
                    "block_id": block_1,
                    "evidence_quote": event_clause,
                    "transition_hint": {
                        "trigger_event_id": event_id,
                        "note": "The greeting opens a cordial phase.",
                    },
                }
            ],
            "character_state_changes": [
                {
                    "subject_ref": mention_first,
                    "attribute": "social_status",
                    "from_value": "visitor",
                    "to_value": "guest",
                    "trigger_ref": event_id,
                    "evidence_quote": event_clause,
                }
            ],
            "unresolved_threads": [
                {
                    "thread_local_id": f"thread_{number}",
                    "description": "Why the visit matters remains unresolved.",
                    "opened_block": block_1,
                    "kind": "question",
                    "subject_refs": [mention_second],
                }
            ],
            "translator_relevant_facts": [
                {
                    "fact_type": "status",
                    "fact": f"{first} is treated as a guest.",
                    "block_evidence": [block_1],
                    "inference_basis": "stated",
                    "subject_ref": mention_first,
                    "event_ids": [event_id],
                }
            ],
            "motifs": [
                {
                    "note": "Greetings recur.",
                    "block_ids": [block_1],
                    "subject_refs": [f"{event_id}#actor"],
                }
            ],
        },
    }


def _scripts() -> dict[tuple[str, str, str | None], dict[str, Any]]:
    result: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for number in range(1, 4):
        result.update(_script_for_chapter(number))
    return result


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("b4-handoff-v3")
    document = _document()
    chapters = ["bk_ch01", "bk_ch02", "bk_ch03"]
    executor = SyntheticStageExecutor(_scripts())
    m1 = run_m1_v3(document, chapters, executor=executor, out_dir=root)
    assert m1["status"] == "complete", m1
    m2 = run_m2_v3(
        document,
        chapters,
        executor=executor,
        out_dir=root,
        m1v3_dir=root,
    )
    assert m2["status"] == "complete", m2
    return {
        "root": root,
        "document": document,
        "chapters": chapters,
        "book_source_manifest": build_book_source_manifest(document),
    }


def _load(built: dict[str, Any], chapters: list[str] | None = None) -> dict[str, Any]:
    selected = chapters or built["chapters"]
    return load_verified_builder_v3_inputs(
        built["document"],
        selected,
        m1v3_dir=built["root"],
        m2v3_dir=built["root"],
    )


def _assemble(built: dict[str, Any], chapters: list[str] | None = None) -> dict[str, Any]:
    selected = chapters or built["chapters"]
    return assemble_b4_input_bundle(
        built["document"],
        selected,
        book_source_manifest=built["book_source_manifest"],
        m1v3_dir=built["root"],
        m2v3_dir=built["root"],
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_complete_bundle_contains_all_b0less_channels_and_p0_fields(built: dict[str, Any]) -> None:
    bundle = _assemble(built)
    ground = bundle["ground_evidence"]
    assert set(ground) == GROUND_CHANNELS | {"dedupe_counts"}
    empty_channels = sorted(channel for channel in GROUND_CHANNELS if not ground[channel])
    assert empty_channels == []
    assert bundle["scope_complete_book"] is True
    assert bundle["knowledge_cutoff_scope"] == "bk_ch03"
    assert any(row["payload"]["subject_refs"] for row in ground["motif_inputs"])
    assert all("narrator_ref" in row["payload"] for row in ground["frame_claim_inputs"])
    assert all(row["ground_item_id"].startswith("g_") for channel in GROUND_CHANNELS for row in ground[channel])
    assert len({row["ground_item_id"] for channel in GROUND_CHANNELS for row in ground[channel]}) == sum(
        len(ground[channel]) for channel in GROUND_CHANNELS
    )
    catalog = bundle["source_block_catalog"]
    assert len(catalog) == 12
    assert {row["block_type"] for row in catalog} == {"paragraph", "heading"}
    assert {row["block_id"] for row in catalog} == {
        f"bk_ch{chapter:02d}_b{block:03d}"
        for chapter in range(1, 4)
        for block in range(1, 5)
    }
    catalog_ids = {row["block_id"] for row in catalog}
    assert all(
        ref["ref_id"] in catalog_ids
        for channel in GROUND_CHANNELS
        for row in ground[channel]
        for ref in row["evidence_refs"]
        if ref["ref_kind"] == "block"
    )
    context_ids = {
        row["block_id"]
        for card in bundle["occurrence_cards"]
        if card["chapter_id"] == "bk_ch01"
        for row in card["context_universe"]["scene_block_candidates"]
    }
    assert context_ids == {"bk_ch01_b001"}
    assert "cast_claim_inputs" not in ground
    assert "bk_ch01_b003" not in context_ids
    assert "bk_ch01_b004" not in context_ids
    assert next(row for row in catalog if row["block_id"] == "bk_ch01_b003")["text"] == (
        "A silent frame-only passage contains no character occurrence."
    )
    assert next(row for row in catalog if row["block_id"] == "bk_ch01_b004")["text"] == (
        "Caf\u00e9 source heading"
    )
    frame_only_ref_ids = {
        ref["ref_id"]
        for ref in ground["frame_claim_inputs"][0]["evidence_refs"]
        if ref["ref_kind"] == "block"
    }
    assert "bk_ch01_b003" in frame_only_ref_ids
    assert [
        [prior["chapter_id"] for prior in row["prior_summaries"]]
        for row in bundle["summary_lineage"]
    ] == [[], ["bk_ch01"], ["bk_ch01", "bk_ch02"]]
    lineage_json = canonical_json(bundle["summary_lineage"])
    assert "source_m2v3_checkpoint_hash" not in lineage_json
    assert "chapter_rolling_summary" not in lineage_json


def test_block_catalog_is_required_for_every_block_evidence_ref(
    built: dict[str, Any],
) -> None:
    bundle = _assemble(built)
    ground = deepcopy(bundle["ground_evidence"])
    ground["frame_claim_inputs"][0]["evidence_refs"].append(
        {"ref_kind": "block", "ref_id": "bk_ch01_missing", "role": None}
    )
    with pytest.raises(B4HandoffError, match="absent from catalog"):
        handoff._validate_block_resolution(
            bundle["source_block_catalog"], bundle["occurrence_cards"], ground
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_occurrence_owner_join_is_exact_cover(built: dict[str, Any], mutation: str) -> None:
    inputs = _load(built)
    mentions = inputs["chapters"][0]["m1_state"]["b1_by_window"][0]["payload"]["character_mentions"]
    if mutation == "missing":
        mentions.pop()
    else:
        mentions.append(deepcopy(mentions[0]))
    with pytest.raises(B4HandoffError, match="exact-cover|exactly one owner"):
        build_occurrence_cards(inputs)


def test_madam_context_is_full_block_and_quote_resolution_is_anchor_scoped(built: dict[str, Any]) -> None:
    inputs = _load(built)
    cards = build_occurrence_cards(inputs)
    madam = next(row for row in cards if row["surface"] == "Madam" and row["chapter_id"] == "bk_ch01")
    active = madam["context_universe"]["active_block"]
    assert len(active["text"]) > 360
    assert "The canine mother" in active["text"]
    assert active["text"] == _chapter_text(1)
    assert madam["evidence_quote"] in active["text"]
    assert handoff._unique_evidence_span("abc abc", "abc", 0, 3) == (0, 3)
    with pytest.raises(B4HandoffError, match="one containing span"):
        handoff._unique_evidence_span("aaaa", "aaa", 1, 2)


def _routing_card(identifier: str, kind: str, **values: Any) -> dict[str, Any]:
    return {
        "occurrence_id": identifier,
        "occurrence_kind": kind,
        "referent_kind_claim": values.pop("referent_kind_claim", "person"),
        **values,
    }


def test_occurrence_routing_uses_locked_endpoint_eligibility_only() -> None:
    cards = [
        _routing_card("m_person", "mention", referent_kind_claim="person"),
        _routing_card("m_animal", "mention", referent_kind_claim="animal"),
        _routing_card("m_unknown", "mention", referent_kind_claim="unknown"),
        _routing_card("ep_eligible", "endpoint", runtime_eligibility="eligible"),
        _routing_card("ep_out", "endpoint", runtime_eligibility="route_out"),
        _routing_card(
            "ep_discourse",
            "endpoint",
            runtime_eligibility="discourse_only",
            reference_scope="narrator",
            referent_kind_claim="unknown",
        ),
        _routing_card("ep_deferred", "endpoint", runtime_eligibility="deferred"),
        _routing_card("ep_invalid", "endpoint", runtime_eligibility="invalid"),
    ]
    routed = build_occurrence_routing_view({"selected_chapters": []}, cards)
    assert routed["counts"] == {
        "total": 8,
        "person": 2,
        "non_person": 2,
        "discourse_only": 1,
        "deferred": 2,
        "invalid_flagged": 1,
    }
    assert [row["occurrence_id"] for row in routed["discourse_only"]] == ["ep_discourse"]
    for bad in (None, "foreign"):
        malformed = [_routing_card("ep_bad", "endpoint", runtime_eligibility=bad)]
        with pytest.raises(B4HandoffError, match="runtime_eligibility"):
            build_occurrence_routing_view({}, malformed)


@pytest.mark.parametrize("mutation", ["swapped", "wrong_block", "foreign"])
def test_phase_observation_topology_is_rechecked(built: dict[str, Any], mutation: str) -> None:
    inputs = _load(built)
    cards = build_occurrence_cards(inputs)
    observation = inputs["chapters"][0]["m2_state"]["digest_payload"]["relation_observations"][0]
    if mutation == "swapped":
        observation["endpoint_refs"].reverse()
    elif mutation == "wrong_block":
        observation["block_id"] = "bk_ch01_b002"
    else:
        observation["event_id"] = "e_foreign"
    with pytest.raises(B4HandoffError, match="topology|foreign event"):
        build_complete_ground_evidence(inputs, cards)


def test_append_only_history_and_duplicate_contract(built: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _assemble(built)
    for channel in ("relation_event_inputs", "phase_observation_inputs", "translator_fact_inputs"):
        assert {row["chapter_id"] for row in bundle["ground_evidence"][channel]} == {
            "bk_ch01",
            "bk_ch02",
            "bk_ch03",
        }
    positions = {("block", "b1"): (0, 0)}
    collector = handoff._GroundCollector()
    kwargs = dict(
        channel="glossary_inputs",
        kind="glossary",
        chapter_id="bk_ch01",
        chapter_index=0,
        source_ordinal=0,
        evidence_refs=[{"ref_kind": "block", "ref_id": "b1", "role": None}],
        payload={"source_term": "x"},
        source_identity="source",
        positions=positions,
    )
    collector.add(**kwargs)
    collector.add(**kwargs)
    assert collector.finish()["dedupe_counts"]["equal_ground_items"] == 1
    monkeypatch.setattr(handoff, "_ground_item_id", lambda **_values: "g_collision")
    conflicting = handoff._GroundCollector()
    conflicting.add(**kwargs)
    with pytest.raises(B4HandoffError, match="collision"):
        conflicting.add(**{**kwargs, "payload": {"source_term": "y"}})


def test_frame_claims_remain_unpromoted_and_occurrence_grounded(built: dict[str, Any]) -> None:
    bundle = _assemble(built)
    rows = bundle["ground_evidence"]["frame_claim_inputs"]
    assert {row["payload"]["status"] for row in rows} == {"proposed"}
    assert all(row["payload"]["version"] == "builder_v3" for row in rows)
    assert all(row["payload"]["narrator_ref"].startswith("m_") for row in rows)
    assert all("narrator_surface" in row["payload"] for row in rows)
    assert "narrator_surfaces" not in canonical_json(bundle)


def test_frame_leaf_index_is_cross_checked_against_frame_tree(built: dict[str, Any]) -> None:
    inputs = _load(built)
    cards = build_occurrence_cards(inputs)
    digest = inputs["chapters"][0]["m2_state"]["digest_payload"]
    digest["deepest_active_leaf_by_block"]["bk_ch01_b001"] = "foreign"
    with pytest.raises(B4HandoffError, match="by-block map"):
        build_complete_ground_evidence(inputs, cards)

def test_prefix_scope_and_mode_contract(built: dict[str, Any]) -> None:
    prefix = _assemble(built, ["bk_ch01", "bk_ch02"])
    assert prefix["scope_complete_book"] is False
    assert prefix["knowledge_cutoff_scope"] == "bk_ch02"
    with pytest.raises(ValueError, match="exact document prefix"):
        _assemble(built, ["bk_ch02"])
    with pytest.raises(ValueError, match="exact document prefix"):
        _assemble(built, ["bk_ch01", "bk_ch03"])
    with pytest.raises(ValueError, match="whole_book_frozen"):
        assemble_b4_input_bundle(
            built["document"],
            built["chapters"],
            book_source_manifest=built["book_source_manifest"],
            m1v3_dir=built["root"],
            m2v3_dir=built["root"],
            knowledge_mode="as_of_experiment",
        )


def test_book_lineage_is_cutoff_stable_while_bundle_snapshot_changes(
    built: dict[str, Any],
) -> None:
    chapter_1 = _assemble(built, ["bk_ch01"])
    chapters_1_2 = _assemble(built, ["bk_ch01", "bk_ch02"])
    assert chapter_1["state_lineage_id"] == chapters_1_2["state_lineage_id"]
    assert chapter_1["book_source_manifest_hash"] == chapters_1_2[
        "book_source_manifest_hash"
    ]
    assert chapter_1["bundle_manifest_hash"] != chapters_1_2["bundle_manifest_hash"]
    assert [row["unit_id"] for row in chapter_1["unit_manifest"]] == ["bk_ch01"]
    assert [row["unit_id"] for row in chapters_1_2["unit_manifest"]] == [
        "bk_ch01",
        "bk_ch02",
    ]


@pytest.mark.parametrize("mutation", ["truncated", "reordered", "changed"])
def test_book_source_manifest_rejects_noncanonical_document(
    built: dict[str, Any], mutation: str
) -> None:
    document = deepcopy(built["document"])
    selected = ["bk_ch01", "bk_ch02"]
    if mutation == "truncated":
        document["chapters"] = document["chapters"][:2]
    elif mutation == "reordered":
        document["chapters"][0], document["chapters"][1] = (
            document["chapters"][1],
            document["chapters"][0],
        )
    else:
        document["chapters"][2]["blocks"][0]["clean_text"] += " changed"
    with pytest.raises(B4HandoffError, match="book source manifest"):
        assemble_b4_input_bundle(
            document,
            selected,
            book_source_manifest=built["book_source_manifest"],
            m1v3_dir=built["root"],
            m2v3_dir=built["root"],
        )


def test_book_lineage_changes_when_the_sealed_source_changes(
    built: dict[str, Any],
) -> None:
    changed = deepcopy(built["document"])
    changed["chapters"][2]["blocks"][0]["clean_text"] += " changed"
    changed_manifest = build_book_source_manifest(changed)
    assert changed_manifest["manifest_hash"] != built["book_source_manifest"][
        "manifest_hash"
    ]
    assert state_lineage_id_for_manifest(changed_manifest) != state_lineage_id_for_manifest(
        built["book_source_manifest"]
    )

    duplicate = deepcopy(built["document"])
    duplicate["chapters"][1]["chapter_id"] = "bk_ch01"
    with pytest.raises(B4HandoffError, match="duplicate chapter ids"):
        build_book_source_manifest(duplicate)


def test_unit_manifest_and_source_scope_contract(built: dict[str, Any]) -> None:
    bundle = _assemble(built)
    provenance = {row["chapter_id"]: row for row in bundle["provenance"]}
    book_rows = {
        row["chapter_id"]: row for row in bundle["book_source_manifest"]["ordered_chapters"]
    }
    assert len(bundle["unit_manifest"]) == len(built["chapters"])
    for unit in bundle["unit_manifest"]:
        chapter_id = unit["unit_id"]
        catalog = [
            row for row in bundle["source_block_catalog"] if row["chapter_id"] == chapter_id
        ]
        assert unit["block_range"] == [catalog[0]["block_id"], catalog[-1]["block_id"]]
        assert unit["parent_chapter"] == chapter_id
        assert unit["cut_reason"] == "author_chapter"
        assert unit["source_hash"] == book_rows[chapter_id]["source_hash"]
        assert unit["m1_checkpoint_refs"] == [
            provenance[chapter_id]["m1v3_identity_hash"]
        ]
    assert all(card["chapter_id"] in built["chapters"] for card in bundle["occurrence_cards"])


def test_loader_fails_closed_for_missing_config_and_source_changes(built: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(CheckpointError, match="missing required"):
        load_verified_builder_v3_inputs(
            built["document"],
            built["chapters"],
            m1v3_dir=tmp_path / "missing",
            m2v3_dir=tmp_path / "missing",
        )
    with pytest.raises(CheckpointError):
        assemble_b4_input_bundle(
            built["document"],
            built["chapters"],
            book_source_manifest=built["book_source_manifest"],
            m1v3_dir=built["root"],
            m2v3_dir=built["root"],
            window_target_tokens=501,
        )
    changed = deepcopy(built["document"])
    changed["chapters"][0]["blocks"][0]["clean_text"] += " changed"
    with pytest.raises(B4HandoffError, match="book source manifest"):
        assemble_b4_input_bundle(
            changed,
            built["chapters"],
            book_source_manifest=built["book_source_manifest"],
            m1v3_dir=built["root"],
            m2v3_dir=built["root"],
        )


def test_loader_rejects_semantic_mismatch_and_wrong_m1_identity(
    built: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = handoff.read_state_from_checkpoint

    def corrupt_semantic(checkpoint: dict[str, Any], *, out_dir: Path) -> dict[str, Any]:
        state = original(checkpoint, out_dir=out_dir)
        if checkpoint["stage"] == "m1v3" and checkpoint["chapter_id"] == "bk_ch01":
            state = deepcopy(state)
            state["b1_by_window"][0]["payload"]["context_only_used"] = True
        return state

    monkeypatch.setattr(handoff, "read_state_from_checkpoint", corrupt_semantic)
    with pytest.raises(CheckpointError, match="semantic_state_hash"):
        _load(built)
    monkeypatch.setattr(handoff, "read_state_from_checkpoint", original)

    def wrong_identity(checkpoint: dict[str, Any], *, out_dir: Path) -> dict[str, Any]:
        state = original(checkpoint, out_dir=out_dir)
        if checkpoint["stage"] == "m2v3" and checkpoint["chapter_id"] == "bk_ch01":
            state = deepcopy(state)
            state["input_m1v3_identity_hash"] = "wrong"
            state["semantic_state_hash"] = canonical_hash(handoff._m2_semantic_projection(state))
        return state

    monkeypatch.setattr(handoff, "read_state_from_checkpoint", wrong_identity)
    with pytest.raises(CheckpointError, match="wrong M1 identity"):
        _load(built)

    def missing_contract(checkpoint: dict[str, Any], *, out_dir: Path) -> dict[str, Any]:
        state = original(checkpoint, out_dir=out_dir)
        if checkpoint["stage"] == "m1v3" and checkpoint["chapter_id"] == "bk_ch01":
            state = deepcopy(state)
            state.pop("contract_versions")
            state["semantic_state_hash"] = canonical_hash(handoff._m1_semantic_projection(state))
        return state

    monkeypatch.setattr(handoff, "read_state_from_checkpoint", missing_contract)
    with pytest.raises(CheckpointError, match="contract mismatch"):
        _load(built)

    def stale_summary_identity(
        checkpoint: dict[str, Any], *, out_dir: Path
    ) -> dict[str, Any]:
        state = original(checkpoint, out_dir=out_dir)
        if checkpoint["stage"] == "m2v3" and checkpoint["chapter_id"] == "bk_ch02":
            state = deepcopy(state)
            state["prior_summary_provenance"][0]["source_m2v3_identity_hash"] = "stale"
            state["semantic_state_hash"] = canonical_hash(
                handoff._m2_semantic_projection(state)
            )
        return state

    monkeypatch.setattr(handoff, "read_state_from_checkpoint", stale_summary_identity)
    with pytest.raises(CheckpointError, match="prior-summary identity lineage"):
        _load(built)


def test_determinism_path_independence_and_evidence_sensitivity(
    built: dict[str, Any], tmp_path: Path
) -> None:
    first = _assemble(built)
    second = _assemble(built)
    assert canonical_json(first) == canonical_json(second)
    copied = tmp_path / "copied"
    shutil.copytree(built["root"], copied)
    relocated = assemble_b4_input_bundle(
        built["document"],
        built["chapters"],
        book_source_manifest=built["book_source_manifest"],
        m1v3_dir=copied,
        m2v3_dir=copied,
    )
    assert relocated == first

    inputs = _load(built)
    cards = build_occurrence_cards(inputs)
    before = build_complete_ground_evidence(inputs, cards)
    inputs["chapters"][0]["m2_state"]["digest_payload"]["motifs"][0]["note"] += " changed"
    after = build_complete_ground_evidence(inputs, cards)
    assert before["motif_inputs"][0]["ground_item_id"] != after["motif_inputs"][0]["ground_item_id"]
    changed_bundle = deepcopy(first)
    changed_bundle["ground_evidence"] = after
    changed_bundle.pop("bundle_manifest_hash")
    assert canonical_hash(changed_bundle) != first["bundle_manifest_hash"]


def test_input_state_and_checkpoint_tree_are_not_mutated(built: dict[str, Any]) -> None:
    before_files = _tree_hashes(built["root"])
    inputs = _load(built)
    frozen = deepcopy(inputs)
    cards = build_occurrence_cards(inputs)
    build_occurrence_routing_view(inputs, cards)
    build_complete_ground_evidence(inputs, cards)
    _assemble(built)
    assert inputs == frozen
    assert _tree_hashes(built["root"]) == before_files


def test_authority_scan_and_internal_import_boundary() -> None:
    with pytest.raises(B4HandoffError, match="forbidden"):
        handoff._assert_no_authority_smuggling({"nested": [{"entity_id": "ent_x"}]})
    with pytest.raises(B4HandoffError, match="entity identifier"):
        handoff._assert_no_authority_smuggling({"subject_refs": ["ent_x"]})
    handoff._assert_no_authority_smuggling({"opaque_source_prose": "the token ent_x is prose"})
    literary_dir = Path(__file__).parents[1] / "literary"
    offenders = []
    for path in literary_dir.glob("*.py"):
        if path.name == "b4_handoff_v3.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "b4_handoff_v3" in text or "B4InputBundle" in text:
            offenders.append(path.name)
    assert offenders == []


def test_manifest_hashes_are_recomputable_from_persisted_contract(built: dict[str, Any]) -> None:
    bundle = _assemble(built)
    assert canonical_hash(handoff._input_identity_projection(bundle)) == bundle[
        "input_identity_manifest_hash"
    ]
    semantic = deepcopy(bundle)
    semantic.pop("bundle_manifest_hash")
    assert canonical_hash(semantic) == bundle["bundle_manifest_hash"]
    verify_b4_input_bundle_identity(bundle)


def test_bundle_identity_verifier_rejects_stale_schema_and_tampering(
    built: dict[str, Any],
) -> None:
    bundle = _assemble(built)
    stale = deepcopy(bundle)
    stale["schema_version"] = "literary_b4_input_bundle_v1"
    with pytest.raises(B4HandoffError, match="schema mismatch"):
        verify_b4_input_bundle_identity(stale)

    tampered = deepcopy(bundle)
    tampered["book_source_manifest"]["ordered_chapters"][0]["source_hash"] = "tampered"
    with pytest.raises(B4HandoffError, match="manifest hash mismatch"):
        verify_b4_input_bundle_identity(tampered)

    retired_channel = deepcopy(bundle)
    retired_channel["ground_evidence"]["cast_claim_inputs"] = []
    semantic = deepcopy(retired_channel)
    semantic.pop("bundle_manifest_hash")
    retired_channel["bundle_manifest_hash"] = canonical_hash(semantic)
    with pytest.raises(B4HandoffError, match="channel contract mismatch"):
        verify_b4_input_bundle_identity(retired_channel)

    widened_context = deepcopy(bundle)
    card = widened_context["occurrence_cards"][0]
    neighbor = next(
        row
        for row in widened_context["source_block_catalog"]
        if row["chapter_id"] == card["chapter_id"] and row["block_id"] != card["block_id"]
    )
    card["context_universe"]["scene_block_candidates"].append(
        {
            "block_id": neighbor["block_id"],
            "order_index": neighbor["order_index"],
            "block_type": neighbor["block_type"],
            "text": neighbor["text"],
        }
    )
    semantic = deepcopy(widened_context)
    semantic.pop("bundle_manifest_hash")
    widened_context["bundle_manifest_hash"] = canonical_hash(semantic)
    with pytest.raises(B4HandoffError, match="active-block-only"):
        verify_b4_input_bundle_identity(widened_context)

    bad_unit = deepcopy(bundle)
    bad_unit["unit_manifest"][0]["block_range"][1] = "bk_ch01_missing"
    bad_unit["input_identity_manifest_hash"] = canonical_hash(
        handoff._input_identity_projection(bad_unit)
    )
    semantic = deepcopy(bad_unit)
    semantic.pop("bundle_manifest_hash")
    bad_unit["bundle_manifest_hash"] = canonical_hash(semantic)
    with pytest.raises(B4HandoffError, match="unit manifest row identity mismatch"):
        verify_b4_input_bundle_identity(bad_unit)
