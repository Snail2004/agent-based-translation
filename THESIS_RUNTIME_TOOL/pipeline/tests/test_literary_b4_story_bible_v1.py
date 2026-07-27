from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.b4_story_bible_assembler_v1 import (
    ANCHOR_OUTPUT_SCHEMA_VERSION,
    B4StoryBibleError,
    MANIFEST_SCHEMA_VERSION,
    PROFILE_SCHEMA_VERSION,
    assemble_b4_story_bible_v1,
    resolve_b4_evidence_ref_v1,
    validate_address_anchor_output_v1,
    verify_b4_story_bible_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def _sealed(body: dict, field: str) -> dict:
    return {**deepcopy(body), field: canonical_hash(body)}


def _write(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _card(
    entity_id: str,
    surface: str,
    chapter_id: str,
    *,
    claims: list[dict] | None = None,
    kind: str = "person",
) -> dict:
    return {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "aliases": [],
        "referent_kind": {
            "value": kind,
            "basis": "explicit_textual",
            "effective": True,
            "semantic_status": "unreviewed",
        },
        "record_class": "confirmed_entity",
        "first_seen": {
            "chapter_id": chapter_id,
            "block_id": f"{chapter_id}_b001",
            "order_index": 1,
        },
        "claims": claims or [],
        "source_refs": [],
    }


def _claim(field: str, value: str, chapter_id: str) -> dict:
    return {
        "field": field,
        "value": value,
        "status": "supported",
        "basis": "explicit_textual",
        "anchor_block_ids": [f"{chapter_id}_b001"],
        "provenance": {"chapter_id": chapter_id},
    }


def _fixture(tmp_path: Path, *, target_order: int = 6) -> tuple[dict, dict]:
    chapter_ids = [f"wh_ch{index:02d}" for index in range(1, target_order + 1)]
    cards = {
        "card_joseph": ("Joseph", "wh_ch01"),
        "card_nelly": ("Nelly", "wh_ch01"),
        "card_dean": ("Mrs. Dean", "wh_ch02"),
        "card_edgar": ("Edgar Linton", "wh_ch04"),
        "card_isabella": ("Isabella", "wh_ch04"),
    }
    projection_entities = []
    for card_id, (surface, first_chapter) in cards.items():
        if int(first_chapter[-2:]) > target_order:
            continue
        member_chapters = [first_chapter]
        if card_id == "card_edgar" and target_order >= 6:
            member_chapters.append("wh_ch06")
        projection_entities.append(
            {
                "effective_entity_id": f"effective_{card_id}",
                "canonical_surface": surface,
                "stable_surfaces": [surface],
                "aliases": [],
                "referent_kind": {
                    "value": "person",
                    "basis": "explicit_textual",
                    "effective": True,
                    "semantic_status": "unreviewed",
                },
                "record_class": "confirmed_entity",
                "member_card_ids": [card_id],
                "member_chapters": member_chapters,
                "first_seen": {
                    "chapter_id": first_chapter,
                    "block_id": f"{first_chapter}_b001",
                    "order_index": 1,
                },
                "source_refs": [],
                "decision_refs": [],
            }
        )
    pending_cases = []
    if target_order >= 2:
        pending_cases.append(
            {
                "component_id": "identity_pending_1",
                "card_ids": ["card_nelly", "card_dean"],
                "candidate_set": ["card_dean"],
                "current_candidate_set": ["card_dean"],
                "question_type": "identity_linkage",
                "reason": "The supplied evidence does not settle the identity.",
                "resolution_condition": "A direct source link is required.",
                "chapter_id": "wh_ch02",
                "review_route": "identity_auditor",
                "state": "evidence_needed",
            }
        )
    projection_body = {
        "schema_version": "literary_b1_reconciled_projection_v1",
        "book_id": "fixture_book",
        "effective_entities": projection_entities,
        "pending_cases": pending_cases,
        "resolved_distinct_cases": [],
        "identity_authority_granted": False,
        "source_registry_hashes": [],
    }
    projection_path = _write(
        tmp_path / "identity.json", _sealed(projection_body, "projection_hash")
    )

    chapter_rows = []
    capsule_rows = []
    for order, chapter_id in enumerate(chapter_ids, start=1):
        chapter_cards = []
        glossary = []
        relations = []
        if order == 1:
            chapter_cards.extend(
                (
                    _card(
                        "card_joseph",
                        "Joseph",
                        chapter_id,
                        claims=[
                            _claim("gender", "masculine", chapter_id),
                            _claim("life_stage", "adult", chapter_id),
                        ],
                    ),
                    _card(
                        "card_nelly",
                        "Nelly",
                        chapter_id,
                        claims=[_claim("gender", "feminine", chapter_id)],
                    ),
                )
            )
            glossary.append(
                {
                    "term_id": "term_joseph",
                    "surface": "t' fowld",
                    "contextual_sense": "the fold",
                    "source_block_ids": ["wh_ch01_b001"],
                }
            )
        if order == 2:
            chapter_cards.append(_card("card_dean", "Mrs. Dean", chapter_id))
        if order == 4:
            chapter_cards.extend(
                (
                    _card(
                        "card_edgar",
                        "Edgar Linton",
                        chapter_id,
                        claims=[_claim("life_stage", "child", chapter_id)],
                    ),
                    _card("card_isabella", "Isabella", chapter_id),
                )
            )
        if order == 6:
            chapter_cards.append(
                _card(
                    "card_edgar",
                    "Edgar Linton",
                    "wh_ch04",
                    claims=[
                        _claim("life_stage", "child", "wh_ch04"),
                        _claim("life_stage", "adult", "wh_ch06"),
                    ],
                )
            )
            relations.append(
                {
                    "relation_edge_id": "edge_contested_1",
                    "source_entity_id": "card_edgar",
                    "target_entity_id": "card_isabella",
                    "relation": "parent_of",
                    "relation_family": "parent_child",
                    "relation_note": None,
                    "anchor_block_ids": ["wh_ch06_b001"],
                    "chapter_id": "wh_ch06",
                    "semantic_status": "structurally_contested",
                    "structurally_contested": True,
                    "contested_group_id": "contest_1",
                    "contested_rule": "E-1",
                    "effective": False,
                }
            )
        registry_body = {
            "schema_version": "fixture_registry_v1",
            "chapter_id": chapter_id,
            "cards": chapter_cards,
            "relation_edges": relations,
            "glossary_entries": glossary,
            "pending_reviews": [],
        }
        registry = _sealed(registry_body, "registry_hash")

        blocks = [f"{chapter_id}_b001", f"{chapter_id}_b002"]
        turns = []
        if order == 1:
            turns.append(
                {
                    "speaker_turn_id": "turn_joseph_glossary",
                    "block_id": "wh_ch01_b001",
                    "utterance_anchor": "T' fowld is shut.",
                    "speaker": {
                        "surface": "Joseph",
                        "resolution_status": "resolved_candidate",
                        "candidate_card_ids": ["card_joseph"],
                    },
                    "addressee": {
                        "surface": "",
                        "resolution_status": "no_addressee",
                        "candidate_card_ids": [],
                    },
                    "address_terms": [],
                    "register_cue": "neutral",
                    "register_cue_raw": None,
                    "delivery_tone": "muttered",
                }
            )
        if order == target_order:
            turns.extend(
                (
                    {
                        "speaker_turn_id": "turn_he_nelly",
                        "block_id": f"{chapter_id}_b001",
                        "utterance_anchor": "Come here, Nelly.",
                        "speaker": {
                            "surface": "he",
                            "resolution_status": "resolved_candidate",
                            "candidate_card_ids": ["card_edgar"],
                        },
                        "addressee": {
                            "surface": "Nelly",
                            "resolution_status": "unresolved",
                            "candidate_card_ids": [],
                        },
                        "address_terms": ["Nelly"],
                        "register_cue": "neutral",
                        "register_cue_raw": None,
                        "delivery_tone": "plain",
                    },
                    {
                        "speaker_turn_id": "turn_joseph_edgar_1",
                        "block_id": f"{chapter_id}_b001",
                        "utterance_anchor": "Sir, the gate is shut.",
                        "speaker": {
                            "surface": "Joseph",
                            "resolution_status": "resolved_candidate",
                            "candidate_card_ids": ["card_joseph"],
                        },
                        "addressee": {
                            "surface": "Edgar",
                            "resolution_status": "resolved_candidate",
                            "candidate_card_ids": ["card_edgar"],
                        },
                        "address_terms": ["sir"],
                        "register_cue": "deferential",
                        "register_cue_raw": None,
                        "delivery_tone": "plain",
                    },
                    {
                        "speaker_turn_id": "turn_joseph_edgar_2",
                        "block_id": f"{chapter_id}_b002",
                        "utterance_anchor": "Sir, I heard you.",
                        "speaker": {
                            "surface": "Joseph",
                            "resolution_status": "resolved_candidate",
                            "candidate_card_ids": ["card_joseph"],
                        },
                        "addressee": {
                            "surface": "Edgar",
                            "resolution_status": "resolved_candidate",
                            "candidate_card_ids": ["card_edgar"],
                        },
                        "address_terms": ["sir"],
                        "register_cue": "deferential",
                        "register_cue_raw": None,
                        "delivery_tone": "plain",
                    },
                )
            )
        frame = {
            "frame_segment_id": f"frame_{chapter_id}",
            "start_block_id": blocks[0],
            "end_block_id": blocks[-1],
            "covered_block_ids": blocks,
            "narrator_surface": "Nelly",
            "narrator_status": "resolved_candidate",
            "candidate_card_ids": ["card_nelly"],
            "narrative_mode": "embedded_story",
        }
        interaction_body = {
            "schema_version": "fixture_b2_v1",
            "chapter_id": chapter_id,
            "frame_segments": [frame],
            "speaker_turns": turns,
            "salient_events": [],
            "review_requests": [],
        }
        interaction = _sealed(interaction_body, "artifact_hash")

        state = {
            "state_id": f"state_{chapter_id}",
            "semantic_key": f"key_{chapter_id}",
            "state_domain": "role",
            "state_value": "serves the household",
            "subject_referent_refs": ["ref_joseph"],
            "counterpart_referent_refs": [],
            "valid_from_block_id": blocks[0],
            "valid_to_block_id": None,
            "source_block_ids": [blocks[0]],
            "observations": [],
            "consolidated_state_ids": [],
            "corroborating_state_ids": [],
            "opened_by_observation_id": f"obs_{chapter_id}",
        }
        temporal_body = {
            "schema_version": "fixture_b3_v1",
            "chapter_id": chapter_id,
            "effective_state_projection": [state],
            "new_state_rows": [state],
            "confirmed_observation_rows": [],
            "historical_observations": [],
            "non_effective_observations": [],
            "pending_cases": (
                [
                    {
                        "pending_case_id": "pending_state_1",
                        "chapter_id": chapter_id,
                        "review_route": "temporal_review",
                        "reason": "The interval remains unresolved.",
                        "reason_codes": ["model_requested_review"],
                    }
                ]
                if order == target_order
                else []
            ),
            "resolved_cases": (
                [
                    {
                        "pending_case_id": "unknown_window_1",
                        "chapter_id": chapter_id,
                        "review_route": "identity_review",
                        "disposition": "origin_unknown",
                        "reason": "The source does not state the onset.",
                        "unknowable_window": {
                            "from_chapter": chapter_id,
                            "to_chapter": chapter_id,
                            "blocker": "onset_not_stated_in_source",
                        },
                    }
                ]
                if order == target_order
                else []
            ),
        }
        temporal = _sealed(temporal_body, "artifact_hash")
        component_body = {
            "schema_version": "fixture_component_catalog_v1",
            "chapter_id": chapter_id,
            "components": [
                {
                    "component_id": f"component_{chapter_id}",
                    "candidate_cards": [
                        {
                            "referent_ref": "ref_joseph",
                            "candidate_card_id": "card_joseph",
                            "canonical_surface": "Joseph",
                        }
                    ],
                }
            ],
        }
        component = _sealed(component_body, "catalog_hash")
        summary_body = {
            "schema_version": "fixture_b0_v1",
            "chapter_id": chapter_id,
            "chapter_order": order,
            "summary": {
                "narrative_handoff": {
                    "ending_position": f"End of {chapter_id}.",
                    "frame_summary": "The embedded account continues.",
                    "frame_refs": [f"frame_{chapter_id}"],
                    "entities_mentioned": ["Nelly"],
                    "locations_mentioned": [],
                }
            },
        }
        summary = _sealed(summary_body, "artifact_hash")
        capsule_rows.append(
            {
                "capsule_id": f"capsule_{chapter_id}",
                "chapter_id": chapter_id,
                "chapter_order": order,
                "text": f"Events of {chapter_id}.",
                "entity_refs": [],
                "event_refs": [],
                "state_refs": [f"state_{chapter_id}"],
            }
        )
        chapter_root = tmp_path / chapter_id
        chapter_rows.append(
            {
                "chapter_id": chapter_id,
                "chapter_order": order,
                "registry_path": _write(
                    chapter_root / "registry.json", registry
                ),
                "interaction_path": _write(
                    chapter_root / "interaction.json", interaction
                ),
                "recovery_path": None,
                "temporal_path": _write(
                    chapter_root / "temporal.json", temporal
                ),
                "component_catalog_path": _write(
                    chapter_root / "component.json", component
                ),
                "summary_path": _write(
                    chapter_root / "summary.json", summary
                ),
            }
        )
    target_id = chapter_ids[-1]
    capsule_body = {
        "schema_version": "fixture_capsules_v1",
        "append_only": True,
        "capsules": capsule_rows,
    }
    capsule_path = _write(
        tmp_path / "capsule_log.json",
        _sealed(capsule_body, "capsule_log_hash"),
    )
    target_turn_blocks = [f"{target_id}_b001", f"{target_id}_b002"]
    window_body = {
        "schema_version": "fixture_window_plan_v1",
        "chapter_id": target_id,
        "windows": [
            {
                "window_id": "window_1",
                "active_block_ids": [target_turn_blocks[0]],
                "preceding_tail_block_ids": [],
                "estimated_active_source_tokens": 10,
            },
            {
                "window_id": "window_2",
                "active_block_ids": [target_turn_blocks[1]],
                "preceding_tail_block_ids": [target_turn_blocks[0]],
                "estimated_active_source_tokens": 10,
            },
        ],
    }
    window_path = _write(
        tmp_path / "window_plan.json",
        _sealed(window_body, "window_plan_hash"),
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "target_chapter_id": target_id,
        "target_chapter_order": target_order,
        "chapters": chapter_rows,
        "identity_projection_path": projection_path if target_order > 1 else None,
        "capsule_log_path": capsule_path,
        "window_plan_path": window_path,
        "_manifest_path": str(tmp_path / "manifest.json"),
    }
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "b4_token_budget": None,
        "memory_dormancy_chapters": 3,
    }
    return manifest, profile


def _assemble(tmp_path: Path, *, target_order: int = 6):
    manifest, profile = _fixture(tmp_path, target_order=target_order)
    return assemble_b4_story_bible_v1(manifest=manifest, profile=profile)


def _evidence_refs(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ref":
                assert isinstance(item, str)
                refs.add(item)
            else:
                refs.update(_evidence_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_evidence_refs(item))
    return refs


def test_t1_as_of_purity_halts_on_injected_future_decision(tmp_path: Path) -> None:
    manifest, profile = _fixture(tmp_path, target_order=2)
    projection_path = Path(manifest["identity_projection_path"])
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection.pop("projection_hash")
    projection["pending_cases"].append(
        {
            "component_id": "future_case",
            "card_ids": ["card_joseph"],
            "question_type": "identity_linkage",
            "reason": "Injected future decision.",
            "resolution_condition": "None.",
            "chapter_id": "wh_ch05",
        }
    )
    _write(projection_path, _sealed(projection, "projection_hash"))
    with pytest.raises(B4StoryBibleError, match="outside the input prefix|future"):
        assemble_b4_story_bible_v1(manifest=manifest, profile=profile)


def test_t2_t3_t4_t5_real_contract_features_survive(tmp_path: Path) -> None:
    assembly = _assemble(tmp_path)
    bible = assembly.stable

    unresolved = bible["open_questions"]["unresolved_address"]
    assert any(
        row["speaker_surface"] == "he"
        and row["addressee_surface"] == "Nelly"
        and row["unresolved_side"] == "addressee"
        for row in unresolved
    )

    edgar = next(
        row
        for row in bible["entities"]
        if row["canonical_surface"] == "Edgar Linton"
    )
    life_stage = edgar["claims"]["life_stage"]
    assert life_stage["claim_conflict"] is True
    assert set(life_stage["values"]) == {"child", "adult"}

    contested = [
        row for row in bible["relations"] if row["structurally_contested"]
    ]
    assert [row["relation_edge_id"] for row in contested] == ["edge_contested_1"]
    assert bible["open_questions"]["contested_relations"][0][
        "relation_edge_ids"
    ] == ["edge_contested_1"]

    joseph = next(
        row
        for row in bible["idiolect"]
        if row["effective_entity_id"] == "effective_card_joseph"
    )
    assert [row["surface"] for row in joseph["glossary_terms_in_own_speech"]] == [
        "t' fowld"
    ]
    serialized = canonical_json(joseph).casefold()
    assert "yorkshire" not in serialized
    assert "dialect" not in serialized


def test_t4_removing_either_contested_copy_fails_verification(
    tmp_path: Path,
) -> None:
    assembly = _assemble(tmp_path)
    order_by_id = {f"wh_ch{index:02d}": index for index in range(1, 7)}
    for field in ("relations", "open_questions"):
        broken = deepcopy(assembly.stable)
        broken.pop("artifact_hash")
        if field == "relations":
            broken["relations"] = []
        else:
            broken["open_questions"]["contested_relations"] = []
        broken = _sealed(broken, "artifact_hash")
        with pytest.raises(B4StoryBibleError, match="not mirrored"):
            verify_b4_story_bible_v1(
                broken,
                order_by_id=order_by_id,
                evidence_index=assembly.evidence_index,
            )


def test_t6_determinism_is_byte_exact(tmp_path: Path) -> None:
    manifest, profile = _fixture(tmp_path)
    first = assemble_b4_story_bible_v1(manifest=manifest, profile=profile)
    second = assemble_b4_story_bible_v1(manifest=manifest, profile=profile)
    assert canonical_json(first.stable) == canonical_json(second.stable)
    assert [canonical_json(row) for row in first.window_slices] == [
        canonical_json(row) for row in second.window_slices
    ]
    assert canonical_json(first.ui_view) == canonical_json(second.ui_view)


def test_t7_missing_required_input_halts(tmp_path: Path) -> None:
    manifest, profile = _fixture(tmp_path)
    manifest["chapters"][2]["interaction_path"] = str(
        tmp_path / "absent_interaction.json"
    )
    with pytest.raises(B4StoryBibleError, match="required .* is missing"):
        assemble_b4_story_bible_v1(manifest=manifest, profile=profile)


def test_t8_t9_two_tier_output_uses_one_stable_prefix_and_exact_active_cover(
    tmp_path: Path,
) -> None:
    assembly = _assemble(tmp_path)
    hashes = {
        row["lineage"]["story_bible_lineage_hash"]
        for row in assembly.window_slices
    }
    assert hashes == {assembly.stable["lineage"]["lineage_hash"]}
    active_turns = [
        turn["speaker_turn_id"]
        for window in assembly.window_slices
        for turn in window["speaker_turns"]
        if turn["window_membership"] == "active"
    ]
    assert sorted(active_turns) == sorted(
        ["turn_he_nelly", "turn_joseph_edgar_1", "turn_joseph_edgar_2"]
    )
    assert len(active_turns) == len(set(active_turns))
    assert any(
        turn["window_membership"] == "tail"
        for turn in assembly.window_slices[1]["speaker_turns"]
    )


def test_t10_unresolved_endpoint_is_not_anchorable(tmp_path: Path) -> None:
    assembly = _assemble(tmp_path)
    window_pairs = [
        pair
        for window in assembly.window_slices
        for pair in window["address_pairs"]
    ]
    pair = next(
        row
        for row in window_pairs
        if row["speaker_surface"] == "he"
        and row["addressee_surface"] == "Nelly"
    )
    assert pair["addressee_resolved"] is False
    assert pair["anchorable"] is False
    assert pair["pair_id"] is None
    assert pair["unanchored"] is True
    assert assembly.report["provider_calls"] == 0


def test_t20_resolved_pair_id_is_stable_across_windows_and_anchor(
    tmp_path: Path,
) -> None:
    assembly = _assemble(tmp_path)
    pair_ids = {
        pair["pair_id"]
        for window in assembly.window_slices
        for pair in window["address_pairs"]
        if pair["speaker_effective_entity_id"] == "effective_card_joseph"
        and pair["addressee_effective_entity_id"] == "effective_card_edgar"
    }
    assert len(pair_ids) == 1
    pair_id = next(iter(pair_ids))
    assert pair_id is not None
    assert pair_id in {
        row["pair_id"] for row in assembly.address_anchor_input["pairs"]
    }


def test_t21_unresolved_pair_is_explicitly_unanchored(tmp_path: Path) -> None:
    assembly = _assemble(tmp_path)
    pair = next(
        pair
        for window in assembly.window_slices
        for pair in window["address_pairs"]
        if pair["speaker_surface"] == "he"
        and pair["addressee_surface"] == "Nelly"
    )
    assert pair["pair_id"] is None
    assert pair["unanchored"] is True
    assert None not in {
        row["pair_id"] for row in assembly.address_anchor_input["pairs"]
    }


def test_t11_high_confidence_on_known_gap_emits_issue(tmp_path: Path) -> None:
    assembly = _assemble(tmp_path)
    anchor_input = deepcopy(assembly.address_anchor_input)
    anchor_input.pop("artifact_hash")
    row = deepcopy(anchor_input["pairs"][0])
    row["pair_id"] = "gap_pair"
    row["evidence_completeness"]["addressee_resolved"] = False
    anchor_input["pairs"] = [row]
    anchor_input = _sealed(anchor_input, "artifact_hash")
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": "wh_ch06",
        "anchor_input_artifact_hash": anchor_input["artifact_hash"],
        "pair_decisions": [
            {
                "pair_id": "gap_pair",
                "pronoun_pair": {
                    "speaker": "tôi",
                    "addressee": "ông",
                },
                "vocative_options": [{"form": "thưa ông"}],
                "register_shifts": [],
                "evidence_refs": ["wh_ch06_b001"],
                "model_confidence": "high",
                "not_anchored": None,
            }
        ],
    }
    validated = validate_address_anchor_output_v1(
        anchor_input=anchor_input, response=response
    )
    assert validated["review_issues"] == [
        {
            "issue_kind": "anchor_confidence_exceeds_evidence",
            "pair_id": "gap_pair",
        }
    ]


def test_t12_not_anchored_is_preserved_without_default(tmp_path: Path) -> None:
    assembly = _assemble(tmp_path)
    pair_id = assembly.address_anchor_input["pairs"][0]["pair_id"]
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": "wh_ch06",
        "anchor_input_artifact_hash": assembly.address_anchor_input[
            "artifact_hash"
        ],
        "pair_decisions": [
            {
                "pair_id": pair_id,
                "pronoun_pair": None,
                "vocative_options": [],
                "register_shifts": [],
                "evidence_refs": [],
                "model_confidence": "low",
                "not_anchored": {
                    "reason": "The supplied evidence does not support one form."
                },
            }
        ],
    }
    validated = validate_address_anchor_output_v1(
        anchor_input=assembly.address_anchor_input,
        response=response,
    )
    row = validated["pair_decisions"][0]
    assert row["not_anchored"]["reason"]
    assert row["pronoun_pair"] is None


def test_t13_translation_payload_is_forbidden_but_anchor_forms_are_not(
    tmp_path: Path,
) -> None:
    assembly = _assemble(tmp_path)
    assert "pronoun_pair" not in canonical_json(assembly.stable)
    pair_id = assembly.address_anchor_input["pairs"][0]["pair_id"]
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": "wh_ch06",
        "anchor_input_artifact_hash": assembly.address_anchor_input[
            "artifact_hash"
        ],
        "pair_decisions": [
            {
                "pair_id": pair_id,
                "pronoun_pair": {
                    "speaker": "tôi",
                    "addressee": "ông",
                },
                "vocative_options": [{"form": "thưa ông"}],
                "register_shifts": [],
                "evidence_refs": ["wh_ch06_b001"],
                "model_confidence": "medium",
                "not_anchored": None,
                "translated_sentence": "This field is prohibited.",
            }
        ],
    }
    with pytest.raises(B4StoryBibleError, match="translated text"):
        validate_address_anchor_output_v1(
            anchor_input=assembly.address_anchor_input,
            response=response,
        )


def test_t14_every_evidence_ref_resolves_and_foreign_ref_halts(
    tmp_path: Path,
) -> None:
    manifest, profile = _fixture(tmp_path)
    assembly = assemble_b4_story_bible_v1(manifest=manifest, profile=profile)
    refs = _evidence_refs(assembly.stable)
    assert refs
    for ref in refs:
        assert resolve_b4_evidence_ref_v1(
            evidence_ref=ref,
            evidence_index=assembly.evidence_index,
            manifest=manifest,
        )
    with pytest.raises(B4StoryBibleError, match="not indexed"):
        resolve_b4_evidence_ref_v1(
            evidence_ref="b4evid1_corrupted",
            evidence_index=assembly.evidence_index,
            manifest=manifest,
        )


def test_t15_unresolved_address_projection_is_target_chapter_scoped(
    tmp_path: Path,
) -> None:
    assembly = _assemble(tmp_path)
    unresolved = assembly.stable["open_questions"]["unresolved_address"]
    assert sum(row["turn_count"] for row in unresolved) == 1
    assert len(unresolved) == 1
    assert all(row["example_anchor"] and row["evidence_ref"] for row in unresolved)
    projection = assembly.stable["open_question_projection"]
    assert projection["unresolved_address"] == {
        "scope": "target_chapter_only",
        "target_chapter_id": "wh_ch06",
        "selected_groups": 1,
    }
    assert projection["terminal_origin_unknown"]["source_rows"] == 1
    assert assembly.stable["open_questions"]["unknowable_windows"] == []


def test_t16_multi_value_claim_survives_compact_projection(
    tmp_path: Path,
) -> None:
    assembly = _assemble(tmp_path)
    edgar = next(
        row
        for row in assembly.stable["entities"]
        if row["canonical_surface"] == "Edgar Linton"
    )
    life_stage = edgar["claims"]["life_stage"]
    assert life_stage["claim_conflict"] is True
    assert set(life_stage["values"]) == {"child", "adult"}
    assert isinstance(life_stage["evidence_ref"], str)
    assert "anchor_block_ids" not in canonical_json(edgar["claims"])
    assert "anchor_block_ids" not in canonical_json(assembly.ui_view)


def test_t17_identity_pending_rows_group_by_component_without_card_loss(
    tmp_path: Path,
) -> None:
    manifest, profile = _fixture(tmp_path)
    projection_path = Path(manifest["identity_projection_path"])
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection.pop("projection_hash")
    duplicate = deepcopy(projection["pending_cases"][0])
    duplicate["card_ids"] = ["card_joseph"]
    projection["pending_cases"].append(duplicate)
    _write(projection_path, _sealed(projection, "projection_hash"))

    assembly = assemble_b4_story_bible_v1(manifest=manifest, profile=profile)
    rows = assembly.stable["open_questions"]["pending_identity_cases"]
    assert len(rows) == 1
    assert rows[0]["card_ids"] == ["card_dean", "card_joseph", "card_nelly"]
    assert rows[0]["source_row_count"] == 2
    report = assembly.stable["open_question_projection"]["identity"]
    assert report["source_rows"] == 2
    assert report["source_components"] == 1
    assert report["grouped_rows"] == 1


def test_t18_identity_group_metadata_conflict_halts(tmp_path: Path) -> None:
    manifest, profile = _fixture(tmp_path)
    projection_path = Path(manifest["identity_projection_path"])
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection.pop("projection_hash")
    duplicate = deepcopy(projection["pending_cases"][0])
    duplicate["reason"] = "Conflicting reason for the same component."
    projection["pending_cases"].append(duplicate)
    _write(projection_path, _sealed(projection, "projection_hash"))

    with pytest.raises(B4StoryBibleError, match="one component disagree"):
        assemble_b4_story_bible_v1(manifest=manifest, profile=profile)


def test_t19_state_chapter_falls_back_to_exact_source_block_prefix(
    tmp_path: Path,
) -> None:
    manifest, profile = _fixture(tmp_path)
    temporal_path = Path(manifest["chapters"][-1]["temporal_path"])
    temporal = json.loads(temporal_path.read_text(encoding="utf-8"))
    temporal.pop("artifact_hash")
    temporal["new_state_rows"] = []
    _write(temporal_path, _sealed(temporal, "artifact_hash"))

    assembly = assemble_b4_story_bible_v1(manifest=manifest, profile=profile)
    target_state = next(
        row
        for row in assembly.stable["states"]
        if row["state_id"] == "state_wh_ch06"
    )
    assert target_state["established_in_chapter"] == "wh_ch06"
    assert target_state["establishment_basis"] == "source_block_chapter"
