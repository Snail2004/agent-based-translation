from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.literary.builder_pilot import (
    M1_CHECKPOINT_SCHEMA_VERSION,
    M2_CHECKPOINT_SCHEMA_VERSION,
    _checkpoint_path,
    _checkpoint_prompt_hashes,
    load_system_prompt_from_design,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document
from pipeline.literary.checkpoint import (
    artifact_manifest,
    build_checkpoint,
    chapter_source_hash,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint_atomic,
)
from pipeline.literary.story_bible_v2 import (
    M3_V2_CHECKPOINT_SCHEMA_VERSION,
    M3_V2_STAGE,
    M3V2SemanticGateError,
    M3V2TechnicalGateError,
    apply_identity_partition_response,
    apply_phase_segment_response,
    build_identity_messages,
    build_identity_atoms_as_of,
    build_m3_v2_checkpoint,
    build_story_bible_v2,
    build_phase_messages,
    count_identity_evidence_cross_group_source_atoms,
    _copy_state,
    empty_m3_v2_state,
    load_m3_v2_input_chain,
    make_m3_v2_request_llm,
    _merge_mapped_phase_batches,
    _normalize_identity_responses_by_shard,
    _phase_rows_as_of,
    _remap_phase_rows_to_final_ids,
    _runtime_identity_shards,
    _runtime_phase_shards,
    _scope_payloads,
    normalize_identity_evidence_atom_ids,
    _persist_m3_v2_raw_response,
    rerender_m3_v2_from_checkpoints,
    run_m3_v2_from_responses,
    run_m3_v2_dry_run,
    validate_identity_partition_response,
    validate_phase_segment_response,
    validate_story_bible_v2,
    write_m3_v2_checkpoint_atomic,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
RUNTIME_ROOT = REPO_ROOT / "THESIS_RUNTIME_TOOL"


def _config() -> LLMConfig:
    return LLMConfig(
        model="fake-m3-v2",
        temperature=0.0,
        reasoning_effort="none",
        max_output_tokens=512,
        prompt_token_cap=8_000,
        pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
    )


def _hook_result(
    payload: dict | None,
    *,
    json_error: str | None = None,
    from_cache: bool = False,
) -> LLMResult:
    text = json.dumps(payload, ensure_ascii=False) if payload is not None else "{bad json"
    return LLMResult(
        text=text,
        parsed_json=payload,
        json_error=json_error,
        model="fake-m3-v2",
        system_fingerprint="fp_m3v2_test",
        usage=LLMUsage(prompt_tokens=17, cached_tokens=0, completion_tokens=9),
        cost_usd=0.0 if from_cache else 0.000017,
        latency_ms=2,
        from_cache=from_cache,
        cache_key="cache_m3v2_test",
    )


def _document() -> dict:
    return {
        "metadata": {"chapter_prefix": "bk_ch"},
        "chapters": [
            {
                "chapter_id": "bk_ch01",
                "blocks": [
                    {
                        "block_id": "bk_ch01_b001",
                        "order_index": 0,
                        "clean_text": "Mr. Rowan called the master.",
                        "source_text": "Mr. Rowan called the master.",
                    },
                    {
                        "block_id": "bk_ch01_b002",
                        "order_index": 1,
                        "clean_text": "The master entered the room.",
                        "source_text": "The master entered the room.",
                    },
                ],
            },
            {
                "chapter_id": "bk_ch02",
                "blocks": [
                    {
                        "block_id": "bk_ch02_b001",
                        "order_index": 2,
                        "clean_text": "Mira warned Rowan about the house.",
                        "source_text": "Mira warned Rowan about the house.",
                    }
                ],
            },
        ],
    }


def _document_with_chapter_count(count: int) -> dict:
    """Expand the neutral fixture without changing its lexical evidence."""

    assert count >= 1
    template = _document()["chapters"][0]
    block_count = len(template["blocks"])
    chapters: list[dict] = []
    for chapter_index in range(1, count + 1):
        chapter_id = f"bk_ch{chapter_index:02d}"
        blocks: list[dict] = []
        for block_index, source in enumerate(template["blocks"], start=1):
            block = copy.deepcopy(source)
            block["block_id"] = f"{chapter_id}_b{block_index:03d}"
            block["order_index"] = (chapter_index - 1) * block_count + block_index - 1
            blocks.append(block)
        chapters.append({"chapter_id": chapter_id, "blocks": blocks})
    return {"metadata": {"chapter_prefix": "bk_ch"}, "chapters": chapters}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_chain(
    tmp_path: Path,
    *,
    include_relation: bool = True,
    document: dict | None = None,
) -> tuple[dict, Path, Path]:
    document = copy.deepcopy(document) if document is not None else _document()
    m1_dir = tmp_path / "m1"
    m2_dir = tmp_path / "m2"
    chapter_ids = [str(chapter["chapter_id"]) for chapter in document["chapters"]]
    _write_json(m1_dir / "m1_report.json", {"chapters_selected": chapter_ids, "entity_ledger": {}})
    parent_m1: str | None = None
    parent_m2: str | None = None
    for index, chapter in enumerate(document["chapters"]):
        chapter_id = str(chapter["chapter_id"])
        first_block = str(chapter["blocks"][0]["block_id"])
        lexicon_path = m1_dir / "lexicon" / f"wb_{chapter_id}_001.json"
        narrative_path = m1_dir / "narrative" / f"wb_{chapter_id}_001.json"
        mentions = [
            {
                "mention_id": f"m_{chapter_id}_master_01",
                "surface": "the master",
                "block_ids": [first_block],
                "candidate_entity_ids": [],
            }
        ]
        if chapter_id == "bk_ch01":
            mentions.append(
                {
                    "mention_id": "m_bk_ch01_master_02",
                    "surface": "the master",
                    "block_ids": ["bk_ch01_b002"],
                    "candidate_entity_ids": [],
                }
            )
        _write_json(
            lexicon_path,
            {"parsed_json": {"chapter_id": chapter_id, "character_mentions": mentions}, "validation": {"ok": True}},
        )
        event_id = f"e_{chapter_id}"
        _write_json(
            narrative_path,
            {
                "parsed_json": {
                    "chapter_id": chapter_id,
                    "relation_events": [
                        {
                            "event_id": event_id,
                            "block_id": first_block,
                            "event_type": "warns",
                            "evidence_quote": "warned" if chapter_id == "bk_ch02" else "called",
                            "actor": {"surface": "Mira", "candidate_entity_ids": ["ent_mira"]},
                            "target": {"surface": "Rowan", "candidate_entity_ids": ["ent_rowan"]},
                        }
                    ]
                    if include_relation
                    else [],
                },
                "validation": {"ok": True},
            },
        )
        m1_checkpoint = build_checkpoint(
            {
                "stage": "m1",
                "chapter_id": chapter_id,
                "chapter_index": index,
                "chapter_sequence_prefix": chapter_ids[: index + 1],
                "source_hash": chapter_source_hash(chapter),
                "prompt_hashes": _checkpoint_prompt_hashes(DESIGN_DOC, "m1", chapter_id),
                "config_hash": "m1-test-config",
                "schema_version": M1_CHECKPOINT_SCHEMA_VERSION,
                "parent_checkpoint_hash": parent_m1,
                "state": {"entity_ledger": {}},
                "artifact_manifest": artifact_manifest([lexicon_path, narrative_path], root=m1_dir),
            }
        )
        write_checkpoint_atomic(_checkpoint_path(m1_dir, "m1", chapter_id), m1_checkpoint)
        parent_m1 = str(m1_checkpoint["checkpoint_hash"])

        digest_path = m2_dir / "digest" / f"{chapter_id}.json"
        _write_json(
            digest_path,
            {
                "parsed_json": {
                    "chapter_id": chapter_id,
                    "chapter_rolling_summary": f"Summary {chapter_id}",
                    "narration_frame_segments": [],
                    "relation_event_summary": [
                        {
                            "pair": ["ent_mira", "ent_rowan"],
                            "event_ids": [event_id],
                            "observed_valence_hint": "unclear",
                            "status": "evidence_only",
                        }
                    ]
                    if include_relation
                    else [],
                    "translator_relevant_facts": [],
                },
                "validation": {"ok": True},
            },
        )
        m2_checkpoint = build_checkpoint(
            {
                "stage": "m2",
                "chapter_id": chapter_id,
                "chapter_index": index,
                "chapter_sequence_prefix": chapter_ids[: index + 1],
                "source_hash": chapter_source_hash(chapter),
                "prompt_hashes": _checkpoint_prompt_hashes(DESIGN_DOC, "m2", chapter_id),
                "config_hash": "m2-test-config",
                "schema_version": M2_CHECKPOINT_SCHEMA_VERSION,
                "parent_checkpoint_hash": parent_m2,
                "input_m1_checkpoint_hash": str(m1_checkpoint["checkpoint_hash"]),
                "state": {"chapter_summaries": []},
                "digest_summary": f"Summary {chapter_id}",
                "artifact_manifest": artifact_manifest([digest_path], root=m2_dir),
            }
        )
        write_checkpoint_atomic(_checkpoint_path(m2_dir, "m2", chapter_id), m2_checkpoint)
        parent_m2 = str(m2_checkpoint["checkpoint_hash"])
    return document, m1_dir, m2_dir


def _real_ch3_identity_shard_one_fixture() -> tuple[
    dict, list[dict], dict, dict[str, str]
]:
    """Load the frozen raw response and exactly the as-of state it received."""

    raw_path = (
        RUNTIME_ROOT
        / "data"
        / "reports"
        / "literary_m4d_b4v2"
        / "raw_responses"
        / "m3_v2"
        / "wh_ch03"
        / "literary_identity_partition_v1_shard_01_attempt_02_resume_01.json"
    )
    checkpoint_path = (
        RUNTIME_ROOT
        / "data"
        / "reports"
        / "literary_m4d_b4v2"
        / "checkpoints"
        / M3_V2_STAGE
        / "wh_ch02.json"
    )
    m1_dir = RUNTIME_ROOT / "data" / "reports" / "literary_m4_full"
    config_path = RUNTIME_ROOT / "pipeline" / "configs" / "llm_prepass_m4full_m2_gpt54.yaml"
    if not all(path.is_file() for path in [raw_path, checkpoint_path, config_path, DEFAULT_EPUB]):
        pytest.skip("real M3 v2 ch3 fixture is not present")

    document, _mapping = _load_document("wuthering_heights", DEFAULT_EPUB)
    chapters = ["wh_ch01", "wh_ch02", "wh_ch03"]
    config = load_llm_config(config_path)
    chain = load_m3_v2_input_chain(
        document=document,
        chapters=chapters,
        m1_dir=m1_dir,
        m2_dir=m1_dir,
        design_doc=DESIGN_DOC,
    )
    scopes = _scope_payloads(
        document=document,
        chain=chain,
        m1_dir=m1_dir,
        m2_dir=m1_dir,
        design_doc=DESIGN_DOC,
        config=config,
    )
    state = _copy_state((read_checkpoint(checkpoint_path).get("state") or {}).get("m3_state") or {})
    shards = _runtime_identity_shards(
        frontier_atoms=scopes[2]["frontier_atoms"],
        state=state,
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch03",
        scope=str(scopes[2]["scope"]),
        identity_hints=scopes[2]["identity_hints"],
        prompt_cap=config.prompt_token_cap,
    )
    source = {
        str(block["block_id"]): str(block.get("clean_text") or block.get("source_text") or "")
        for chapter in document.get("chapters") or []
        for block in chapter.get("blocks") or []
    }
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    return raw["parsed_json"], shards[0]["items"], state, source


def _real_ch4_identity_shard_fixtures() -> tuple[
    list[dict], list[dict], dict, list[dict], dict[str, str]
]:
    """Load the two halted ch4 responses with the exact runtime shard plan."""

    root = RUNTIME_ROOT / "data" / "reports" / "literary_m4d_b4v2"
    raw_dir = root / "raw_responses" / "m3_v2" / "wh_ch04"
    raw_paths = [
        raw_dir / f"literary_identity_partition_v1_shard_{index:02d}_attempt_01.json"
        for index in [1, 2]
    ]
    checkpoint_path = root / "checkpoints" / M3_V2_STAGE / "wh_ch03.json"
    m1_dir = RUNTIME_ROOT / "data" / "reports" / "literary_m4_full"
    config_path = RUNTIME_ROOT / "pipeline" / "configs" / "llm_prepass_m4full_m2_gpt54.yaml"
    if not all(path.is_file() for path in [*raw_paths, checkpoint_path, config_path, DEFAULT_EPUB]):
        pytest.skip("real M3 v2 ch4 shard fixtures are not present")

    document, _mapping = _load_document("wuthering_heights", DEFAULT_EPUB)
    chapters = ["wh_ch01", "wh_ch02", "wh_ch03", "wh_ch04"]
    config = load_llm_config(config_path)
    chain = load_m3_v2_input_chain(
        document=document,
        chapters=chapters,
        m1_dir=m1_dir,
        m2_dir=m1_dir,
        design_doc=DESIGN_DOC,
    )
    scopes = _scope_payloads(
        document=document,
        chain=chain,
        m1_dir=m1_dir,
        m2_dir=m1_dir,
        design_doc=DESIGN_DOC,
        config=config,
    )
    state = _copy_state((read_checkpoint(checkpoint_path).get("state") or {}).get("m3_state") or {})
    scope = scopes[3]
    shards = _runtime_identity_shards(
        frontier_atoms=scope["frontier_atoms"],
        state=state,
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch04",
        scope=str(scope["scope"]),
        identity_hints=scope["identity_hints"],
        prompt_cap=config.prompt_token_cap,
    )
    responses = [json.loads(path.read_text(encoding="utf-8"))["parsed_json"] for path in raw_paths]
    source = {
        str(block["block_id"]): str(block.get("clean_text") or block.get("source_text") or "")
        for chapter in document.get("chapters") or []
        for block in chapter.get("blocks") or []
    }
    return responses, shards, state, scope["frontier_atoms"], source


def _valid_identity_response(atoms: list[dict]) -> dict:
    """Produce a model-shaped, evidence-backed response for a synthetic scope."""

    assert atoms
    first = atoms[0]
    member_ids = [str(atom["atom_id"]) for atom in atoms]
    return {
        "groups": [
            {
                "group_key": "synthetic_group_01",
                "reuse_entity_id": None,
                "referent_kind": "person",
                "canonical_atom_id": str(first["atom_id"]),
                "member_atom_ids": member_ids,
                "status": "resolved",
                "alias_bindings": [
                    {
                        "surface": str(first["surface"]),
                        "member_atom_ids": member_ids,
                        "valid_from_block": str(first["block_id"]),
                        "valid_until_block": None,
                    }
                ],
                "evidence": [
                    {
                        "block_id": str(first["block_id"]),
                        "quote": str(first["surface"]),
                        "source_atom_ids": member_ids,
                        "supports": "same_identity",
                    }
                ]
                if len(member_ids) > 1
                else [],
            }
        ]
    }


def _single_atom_group(atom: dict, *, reuse_entity_id: str | None = None) -> dict:
    return {
        "group_key": f"g_{atom['atom_id']}",
        "reuse_entity_id": reuse_entity_id,
        "referent_kind": "person",
        "canonical_atom_id": str(atom["atom_id"]),
        "member_atom_ids": [str(atom["atom_id"])],
        "status": "resolved",
        "alias_bindings": [
            {
                "surface": str(atom["surface"]),
                "member_atom_ids": [str(atom["atom_id"])],
                "valid_from_block": str(atom["block_id"]),
                "valid_until_block": None,
            }
        ],
        "evidence": [],
    }


def _phase_event(event_id: str, block_id: str, quote: str) -> dict:
    return {
        "event_id": event_id,
        "block_id": block_id,
        "event_type": "addresses",
        "evidence_quote": quote,
        "actor": {},
        "target": {},
    }


def test_m3_v2_phase_rows_reject_missing_event_join() -> None:
    with pytest.raises(ValueError, match="e_missing"):
        _phase_rows_as_of(
            digests=[
                {
                    "chapter_id": "bk_ch01",
                    "relation_event_summary": [
                        {
                            "pair": ["ent_mira", "ent_rowan"],
                            "event_ids": ["e_missing"],
                        }
                    ],
                }
            ],
            event_index={},
        )


def test_m3_v2_merges_collapsed_final_pairs_and_preserves_all_events() -> None:
    young_history = {
        "source_chapter_id": "wh_ch02",
        "event_ids": ["e_wh_ch02_b013_01", "e_wh_ch02_b029_01", "e_wh_ch02_b048_01"],
        "events": [
            _phase_event("e_wh_ch02_b013_01", "wh_ch02_b013", "Sit down"),
            _phase_event("e_wh_ch02_b029_01", "wh_ch02_b029", "looked down on me"),
            _phase_event("e_wh_ch02_b048_01", "wh_ch02_b048", "brutal curse"),
        ],
    }
    hareton_history = {
        "source_chapter_id": "wh_ch02",
        "event_ids": ["e_wh_ch02_b053_01", "e_wh_ch02_b080_01"],
        "events": [
            _phase_event("e_wh_ch02_b053_01", "wh_ch02_b053", "respect it"),
            _phase_event("e_wh_ch02_b080_01", "wh_ch02_b080", "go with him"),
        ],
    }
    mapped = [
        {
            "provisional_pair": ["ent_the_young_man", "ent_mr_lockwood"],
            "pair": ["ent_hareton_earnshaw", "ent_mr_lockwood"],
            "history": [young_history],
        },
        {
            "provisional_pair": ["ent_hareton_earnshaw", "ent_mr_lockwood"],
            "pair": ["ent_hareton_earnshaw", "ent_mr_lockwood"],
            "history": [hareton_history],
        },
    ]

    merged, audit = _merge_mapped_phase_batches(phase_rows=mapped)

    assert audit == {
        "provisional_pair_batches": 2,
        "final_pairs_sent": 1,
        "collapsed_pair_batches": 1,
        "history_rows_sent": 2,
        "events_sent": 5,
    }
    assert merged[0]["pair"] == ["ent_hareton_earnshaw", "ent_mr_lockwood"]
    before = {
        event_id
        for row in mapped
        for history in row["history"]
        for event_id in history["event_ids"]
    }
    after = {
        event["event_id"]
        for history in merged[0]["history"]
        for event in history["events"]
    }
    assert after == before

    shards = _runtime_phase_shards(
        phase_batches=merged,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        scope="M3_asof_bk_ch02",
        prompt_cap=8_000,
    )
    payload = json.loads(shards[0]["messages"][1]["content"])
    assert payload["pair_evidence"][0]["pair"] == [
        "ent_hareton_earnshaw",
        "ent_mr_lockwood",
    ]
    assert "Sit down" in shards[0]["messages"][1]["content"]

    with pytest.raises(M3V2TechnicalGateError, match="phase_input_wiring_error"):
        _merge_mapped_phase_batches(
            phase_rows=[
                {
                    "pair": ["ent_mira", "ent_rowan"],
                    "history": [
                        {
                            "source_chapter_id": "bk_ch01",
                            "event_ids": [],
                            "events": [],
                        }
                    ],
                }
            ]
        )


def _hareton_binding_state() -> dict:
    state = empty_m3_v2_state()
    state["entities"] = [
        {"entity_id": "ent_hareton_earnshaw"},
        {"entity_id": "ent_mr_heathcliff"},
        {"entity_id": "ent_mrs_heathcliff"},
    ]
    state["atom_to_entity"] = {
        "atom_heathcliff_b058": "ent_mr_heathcliff",
        "atom_hareton_b058": "ent_hareton_earnshaw",
    }
    state["atom_catalog"] = {
        "atom_heathcliff_b058": {
            "block_id": "wh_ch02_b058",
            "surface": "Heathcliff",
        },
        "atom_hareton_b058": {
            "block_id": "wh_ch02_b058",
            "surface": "Hareton",
        },
    }
    state["hint_to_entities"] = {
        "ent_heathcliff": ["ent_mr_heathcliff"],
        "ent_mrs_heathcliff": ["ent_mrs_heathcliff"],
    }
    return state


def _hareton_witness_row(*, extra_event: dict | None = None) -> dict:
    events = [
        {
            "event_id": "e_wh_ch02_b058_01",
            "block_id": "wh_ch02_b058",
            "actor": {"surface": "Heathcliff", "candidate_entity_ids": []},
            "target": {"surface": "Hareton", "candidate_entity_ids": []},
        }
    ]
    if extra_event is not None:
        events.append(extra_event)
    return {
        "provisional_pair": ["ent_hareton", "ent_heathcliff"],
        "history": [
            {
                "source_chapter_id": "wh_ch02",
                "event_ids": [str(event["event_id"]) for event in events],
                "events": events,
            }
        ],
    }


def test_m3_v2_binds_hareton_only_from_unambiguous_recorded_witnesses() -> None:
    state = _hareton_binding_state()
    mapped, unresolved, audit = _remap_phase_rows_to_final_ids(
        state, [_hareton_witness_row()]
    )
    assert unresolved == []
    assert mapped[0]["pair"] == ["ent_hareton_earnshaw", "ent_mr_heathcliff"]
    assert audit["provisional_bindings"] == 1
    assert audit["provisional_binding_witnesses"][0]["provisional_id"] == "ent_hareton"
    assert audit["provisional_binding_witnesses"][0]["witnesses"] == [
        {
            "event_id": "e_wh_ch02_b058_01",
            "block_id": "wh_ch02_b058",
            "side": "target",
            "method": "pair_mate_elimination",
            "final_entity_id": "ent_hareton_earnshaw",
        }
    ]

    conflict_state = _hareton_binding_state()
    conflict_state["entities"].append({"entity_id": "ent_other_hareton"})
    conflict_state["atom_to_entity"]["atom_hareton_b059"] = "ent_other_hareton"
    conflict_state["atom_catalog"]["atom_hareton_b059"] = {
        "block_id": "wh_ch02_b059",
        "surface": "Hareton",
    }
    conflict_event = {
        "event_id": "e_wh_ch02_b059_01",
        "block_id": "wh_ch02_b059",
        "actor": {"surface": "Heathcliff", "candidate_entity_ids": ["ent_heathcliff"]},
        "target": {"surface": "Hareton", "candidate_entity_ids": ["ent_hareton"]},
    }
    _mapped, unresolved, audit = _remap_phase_rows_to_final_ids(
        conflict_state, [_hareton_witness_row(extra_event=conflict_event)]
    )
    assert audit["provisional_bindings"] == 0
    assert [row["reason"] for row in unresolved] == ["unresolved_or_collapsed_pair"]

    zero_witness = _hareton_witness_row()
    zero_witness["history"][0]["events"][0]["target"] = {
        "surface": "he",
        "candidate_entity_ids": ["ent_hareton"],
    }
    _mapped, unresolved, audit = _remap_phase_rows_to_final_ids(state, [zero_witness])
    assert audit["provisional_bindings"] == 0
    assert [row["reason"] for row in unresolved] == ["unresolved_or_collapsed_pair"]


def test_m3_v2_dry_run_uses_asof_manifests_and_keeps_same_surface_atoms_separate(tmp_path: Path) -> None:
    document, m1_dir, m2_dir = _make_chain(tmp_path)
    chain = load_m3_v2_input_chain(
        document=document,
        chapters=["bk_ch01", "bk_ch02"],
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        design_doc=DESIGN_DOC,
    )
    atoms = build_identity_atoms_as_of(
        document=document,
        m1_dir=m1_dir,
        m1_checkpoints=[chain["m1_checkpoints"][0]],
    )["atoms"]

    master_atoms = [atom for atom in atoms if atom["surface"] == "the master"]
    assert len(master_atoms) == 2
    assert len({atom["atom_id"] for atom in master_atoms}) == 2
    assert {atom["chapter_id"] for atom in atoms} == {"bk_ch01"}

    report = run_m3_v2_dry_run(
        document,
        ["bk_ch01", "bk_ch02"],
        out_dir=tmp_path / "out",
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
    )
    assert report["zero_api"] is True
    assert report["estimate"]["physical_calls"] == 4
    assert len(report["rendered_prompts"]) == 4
    assert not (tmp_path / "out" / "checkpoints" / M3_V2_STAGE).exists()
    assert report["gate_battery"]["checks"]["m1_m2_chain_validated"] is True
    assert report["gate_battery"]["checks"]["stable_id_ch01_to_ch04"].startswith("awaits")
    rendered = json.loads(Path(report["rendered_prompts"][0]["path"]).read_text(encoding="utf-8"))
    assert rendered["mode"] == "literary_identity_partition_v1"
    assert "the master" in rendered["messages"][1]["content"]


def test_m3_v2_system_prompts_remain_book_neutral_through_the_runtime_loader() -> None:
    forbidden = ["heathcliff", "lockwood", "hareton", "wuthering heights", "catherine linton"]
    for version in ["literary_identity_partition_v1", "literary_phase_segment_v2"]:
        prompt = load_system_prompt_from_design(DESIGN_DOC, version).casefold()
        assert f"prompt version: {version}" in prompt
        assert not any(token in prompt for token in forbidden)


def test_m3_v2_rejects_broken_m2_ancestor_chain(tmp_path: Path) -> None:
    document, m1_dir, m2_dir = _make_chain(tmp_path)
    path = _checkpoint_path(m2_dir, "m2", "bk_ch02")
    broken = read_checkpoint(path)
    broken["parent_checkpoint_hash"] = "not-the-real-parent"
    write_checkpoint_atomic(path, build_checkpoint(broken))

    with pytest.raises(ValueError, match="Invalid M2 as-of checkpoint bk_ch02"):
        load_m3_v2_input_chain(
            document=document,
            chapters=["bk_ch01", "bk_ch02"],
            m1_dir=m1_dir,
            m2_dir=m2_dir,
            design_doc=DESIGN_DOC,
        )


def test_identity_and_phase_validators_enforce_exact_evidence_contract() -> None:
    atoms = [
        {"atom_id": "atom_a", "block_id": "bk_ch01_b001", "surface": "Mira"},
        {"atom_id": "atom_b", "block_id": "bk_ch01_b002", "surface": "Miss Mira"},
    ]
    source = {
        "bk_ch01_b001": "Mira spoke first.",
        "bk_ch01_b002": "Miss Mira replied.",
    }
    identity = {
        "groups": [
            {
                "group_key": "g1",
                "reuse_entity_id": None,
                "referent_kind": "person",
                "canonical_atom_id": "atom_a",
                "member_atom_ids": ["atom_a", "atom_b"],
                "status": "resolved",
                "alias_bindings": [
                    {
                        "surface": "Mira",
                        "member_atom_ids": ["atom_a", "atom_b"],
                        "valid_from_block": "bk_ch01_b001",
                        "valid_until_block": None,
                    }
                ],
                "evidence": [
                    {
                        "block_id": "bk_ch01_b002",
                        "quote": "Miss Mira",
                        "source_atom_ids": ["atom_a", "atom_b"],
                        "supports": "same_identity",
                    }
                ],
            }
        ]
    }
    assert validate_identity_partition_response(
        identity,
        atoms=atoms,
        prior_entity_ids=set(),
        source_text_by_block=source,
    ) == []
    identity["groups"][0]["member_atom_ids"].append("unknown_atom")
    assert any(
        "unknown:unknown_atom" in error
        for error in validate_identity_partition_response(
            identity,
            atoms=atoms,
            prior_entity_ids=set(),
            source_text_by_block=source,
        )
    )

    phase = {
        "relation_phases": [
            {
                "pair": ["ent_mira", "ent_rowan"],
                "phase_label": "friendly",
                "valid_from_block": "bk_ch01_b001",
                "valid_until_block": None,
                "trigger_block": "bk_ch01_b001",
                "trigger_evidence": "Mira",
                "status": "open",
            }
        ],
        "relation_facts": [
            {
                "subject_ref": "ent_mira",
                "predicate_code": "neighbor_of",
                "object_ref": "ent_rowan",
                "valid_from_block": "bk_ch01_b001",
                "evidence_block": "bk_ch01_b001",
                "evidence_quote": "Mira",
                "predicate_note": "",
            }
        ],
    }
    assert validate_phase_segment_response(
        phase,
        entity_ids={"ent_mira", "ent_rowan"},
        source_text_by_block=source,
        block_ordinals={"bk_ch01_b001": 0, "bk_ch01_b002": 1},
    ) == []
    phase["relation_facts"][0]["predicate_code"] = "untyped_relation"
    assert any(
        "predicate_code invalid" in error
        for error in validate_phase_segment_response(
            phase,
            entity_ids={"ent_mira", "ent_rowan"},
            source_text_by_block=source,
            block_ordinals={"bk_ch01_b001": 0, "bk_ch01_b002": 1},
        )
    )


def test_m3_v2_checkpoint_contract_binds_m1_m2_and_artifact_manifest(tmp_path: Path) -> None:
    document = _document()
    chapter = document["chapters"][0]
    artifact = tmp_path / "story_bible" / "bk_ch01.json"
    _write_json(artifact, {"scope": "bk_ch01"})
    checkpoint = build_m3_v2_checkpoint(
        out_dir=tmp_path,
        chapter=chapter,
        chapter_index=0,
        chapter_sequence_prefix=["bk_ch01"],
        design_doc=DESIGN_DOC,
        config=_config(),
        input_m1_checkpoint_hash="m1hash",
        input_m2_checkpoint_hash="m2hash",
        parent_checkpoint_hash=None,
        state={"identity_groups": []},
        raw_responses=[{"mode": "identity", "raw_text": "{}"}],
        published_artifacts=[artifact],
    )
    path = write_m3_v2_checkpoint_atomic(tmp_path, checkpoint)
    loaded = read_checkpoint(path)
    assert validate_checkpoint(
        loaded,
        root=tmp_path,
        expected={
            "stage": M3_V2_STAGE,
            "schema_version": M3_V2_CHECKPOINT_SCHEMA_VERSION,
            "input_m1_checkpoint_hash": "m1hash",
            "input_m2_checkpoint_hash": "m2hash",
        },
    ) == []


def test_m3_v2_synthetic_apply_publishes_and_resumes_without_api(tmp_path: Path) -> None:
    """A clean model-shaped response reaches the only publish path, then restores."""

    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)
    atoms = build_identity_atoms_as_of(
        document=document,
        m1_dir=m1_dir,
        m1_checkpoints=[
            read_checkpoint(_checkpoint_path(m1_dir, "m1", "bk_ch01"))
        ],
    )["atoms"]
    out_dir = tmp_path / "m3"
    responses = {
        "M3_asof_bk_ch01": {
            "identity": _valid_identity_response(atoms),
            "phase": {"relation_facts": [], "relation_phases": []},
        }
    }

    report = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        responses_by_scope=responses,
    )
    assert report["zero_api"] is True
    assert report["status"] == "needs_claude_gate"
    assert [row["status"] for row in report["scopes"]] == ["published"]

    story_path = out_dir / "story_bible_v2" / "bk_ch01_story_bible.json"
    story = json.loads(story_path.read_text(encoding="utf-8"))
    assert story["scope"] == "M3_asof_bk_ch01"
    assert len(story["registry_T2_entities"]) == 1
    checkpoint_path = out_dir / "checkpoints" / M3_V2_STAGE / "bk_ch01.json"
    checkpoint = read_checkpoint(checkpoint_path)
    assert validate_checkpoint(
        checkpoint,
        root=out_dir,
        expected={
            "stage": M3_V2_STAGE,
            "chapter_id": "bk_ch01",
            "schema_version": M3_V2_CHECKPOINT_SCHEMA_VERSION,
        },
    ) == []


def test_m3_v2_rerender_maps_closed_phase_boundary_from_checkpoint_state(tmp_path: Path) -> None:
    """A legacy Story Bible can be mechanically republished without an M3 re-run."""

    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)
    chain = load_m3_v2_input_chain(
        document=document,
        chapters=["bk_ch01"],
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        design_doc=DESIGN_DOC,
    )
    chapter = chain["selected"][0]
    state = empty_m3_v2_state()
    state["entities"] = [
        {
            "entity_id": "ent_mira",
            "canonical": "Mira",
            "referent_kind": "person",
            "status": "resolved",
            "aliases": [],
        },
        {
            "entity_id": "ent_rowan",
            "canonical": "Rowan",
            "referent_kind": "person",
            "status": "resolved",
            "aliases": [],
        },
    ]
    state["relation_phases"] = [
        {
            "pair": ["ent_mira", "ent_rowan"],
            "phase_label": "strained",
            "valid_from_block": "bk_ch01_b001",
            "valid_until_block": "bk_ch01_b002",
            "trigger_block": "bk_ch01_b001",
            "trigger_evidence": "Mira",
            "trigger_evidence_block": "bk_ch01_b001",
            "status": "closed",
        }
    ]
    digests = [{"chapter_id": "bk_ch01", "narration_frame_segments": []}]
    canonical_story = build_story_bible_v2(
        chapter=chapter,
        state=state,
        m1_dir=m1_dir,
        m1_checkpoints=chain["m1_checkpoints"],
        digests=digests,
    )
    assert canonical_story["entity_relations"][0]["valid_to_block"] == "bk_ch01_b002"
    assert "valid_until_block" not in canonical_story["entity_relations"][0]

    legacy_story = copy.deepcopy(canonical_story)
    legacy_phase = legacy_story["entity_relations"][0]
    legacy_phase["valid_until_block"] = legacy_phase.pop("valid_to_block")
    assert "relation_phase leaked internal valid_until_block" in validate_story_bible_v2(
        legacy_story,
        expected_turn_count=0,
        expected_event_count=0,
    )

    out_dir = tmp_path / "m3"
    story_path = out_dir / "story_bible_v2" / "bk_ch01_story_bible.json"
    _write_json(story_path, legacy_story)
    old_checkpoint = build_m3_v2_checkpoint(
        out_dir=out_dir,
        chapter=chapter,
        chapter_index=0,
        chapter_sequence_prefix=["bk_ch01"],
        design_doc=DESIGN_DOC,
        config=_config(),
        input_m1_checkpoint_hash=str(chain["m1_checkpoints"][0]["checkpoint_hash"]),
        input_m2_checkpoint_hash=str(chain["m2_checkpoints"][0]["checkpoint_hash"]),
        parent_checkpoint_hash=None,
        state={
            "m3_state": state,
            "identity_audit": {},
            "phase_audit": {},
            "request_accounting": {},
        },
        raw_responses=[],
        published_artifacts=[story_path],
    )
    write_m3_v2_checkpoint_atomic(out_dir, old_checkpoint)

    report = rerender_m3_v2_from_checkpoints(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
    )

    assert report["zero_api"] is True
    assert report["status"] == "rerendered"
    assert report["resume"]["validated"] == ["bk_ch01"]
    rebuilt_story = json.loads(story_path.read_text(encoding="utf-8"))
    rebuilt_phase = rebuilt_story["entity_relations"][0]
    state_phase = read_checkpoint(
        out_dir / "checkpoints" / M3_V2_STAGE / "bk_ch01.json"
    )["state"]["m3_state"]["relation_phases"][0]
    assert rebuilt_phase["valid_to_block"] == state_phase["valid_until_block"]
    assert "valid_until_block" not in rebuilt_phase
    assert validate_story_bible_v2(
        rebuilt_story,
        expected_turn_count=0,
        expected_event_count=0,
    ) == []

    resumed = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        responses_by_scope={},
        resume=True,
    )
    assert resumed["status"] == "needs_claude_gate"
    assert resumed["resume"]["restored"] == ["bk_ch01"]
    assert resumed["scopes"] == []


def test_m3_v2_identity_gate_rejects_missing_atom_and_wrong_quote(tmp_path: Path) -> None:
    document, m1_dir, _m2_dir = _make_chain(tmp_path)
    checkpoint = read_checkpoint(_checkpoint_path(m1_dir, "m1", "bk_ch01"))
    atoms = build_identity_atoms_as_of(
        document=document,
        m1_dir=m1_dir,
        m1_checkpoints=[checkpoint],
    )["atoms"]
    source = {
        block["block_id"]: block["clean_text"]
        for block in document["chapters"][0]["blocks"]
    }

    missing = _valid_identity_response(atoms)
    missing["groups"][0]["member_atom_ids"] = [str(atoms[0]["atom_id"])]
    missing["groups"][0]["alias_bindings"][0]["member_atom_ids"] = [str(atoms[0]["atom_id"])]
    missing["groups"][0]["evidence"][0]["source_atom_ids"] = [str(atoms[0]["atom_id"])]
    with pytest.raises(M3V2SemanticGateError, match="identity_response_rejected"):
        apply_identity_partition_response(
            empty_m3_v2_state(),
            missing,
            atoms=atoms,
            source_text_by_block=source,
        )

    wrong_quote = _valid_identity_response(atoms)
    wrong_quote["groups"][0]["evidence"][0]["quote"] = "invented evidence"
    with pytest.raises(M3V2SemanticGateError, match="identity_response_rejected"):
        apply_identity_partition_response(
            empty_m3_v2_state(),
            wrong_quote,
            atoms=atoms,
            source_text_by_block=source,
        )


def test_m3_v2_normalizes_the_real_call_one_response_and_retains_cross_group_evidence() -> None:
    raw_path = (
        REPO_ROOT
        / "THESIS_RUNTIME_TOOL"
        / "data"
        / "reports"
        / "literary_m4d_b4v2"
        / "raw_responses"
        / "m3_v2"
        / "wh_ch01"
        / "literary_identity_partition_v1_shard_01_attempt_01.json"
    )
    if not raw_path.is_file():
        pytest.skip("real M3 v2 call-one artifact is not present")

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    response = raw["parsed_json"]
    atom_ids = {
        str(atom_id)
        for group in response["groups"]
        for atom_id in group["member_atom_ids"]
    }
    atoms = [
        {
            "atom_id": atom_id,
            "mention_id": atom_id.removeprefix("atom_").split("__", 1)[0],
            "surface": next(
                alias["surface"]
                for group in response["groups"]
                for alias in group["alias_bindings"]
                if atom_id in alias["member_atom_ids"]
            ),
        }
        for atom_id in sorted(atom_ids)
    ]
    source_text_by_block: dict[str, list[str]] = {}
    for group in response["groups"]:
        for row in group["evidence"]:
            source_text_by_block.setdefault(str(row["block_id"]), []).append(str(row["quote"]))
        for alias in group["alias_bindings"]:
            source_text_by_block.setdefault(str(alias["valid_from_block"]), [])
            if alias.get("valid_until_block") is not None:
                source_text_by_block.setdefault(str(alias["valid_until_block"]), [])
    source = {block_id: "\n".join(quotes) for block_id, quotes in source_text_by_block.items()}

    normalized, count = normalize_identity_evidence_atom_ids(response, atoms=atoms)
    assert count == 17
    assert count_identity_evidence_cross_group_source_atoms(normalized) == 4
    assert validate_identity_partition_response(
        normalized,
        atoms=atoms,
        prior_entity_ids=set(),
        source_text_by_block=source,
    ) == []
    _state, audit = apply_identity_partition_response(
        empty_m3_v2_state(),
        response,
        atoms=atoms,
        source_text_by_block=source,
    )
    assert audit["evidence_atom_id_normalized"] == 17
    assert audit["evidence_cross_group_source_atoms"] == 4


def test_m3_v2_short_evidence_atom_id_is_strict_when_ambiguous_or_unknown() -> None:
    atoms = [
        {"atom_id": "atom_m_same__bk_ch01_b001", "mention_id": "m_same"},
        {"atom_id": "atom_m_same__bk_ch01_b002", "mention_id": "m_same"},
    ]
    ambiguous_response = {
        "groups": [
            {
                "group_key": "g1",
                "canonical_atom_id": atoms[0]["atom_id"],
                "member_atom_ids": [atoms[0]["atom_id"]],
                "referent_kind": "person",
                "status": "resolved",
                "alias_bindings": [],
                "evidence": [
                    {
                        "block_id": "bk_ch01_b001",
                        "quote": "Mira",
                        "source_atom_ids": ["atom_m_same"],
                        "supports": "same_identity",
                    }
                ],
            },
            {
                "group_key": "g2",
                "canonical_atom_id": atoms[1]["atom_id"],
                "member_atom_ids": [atoms[1]["atom_id"]],
                "referent_kind": "person",
                "status": "resolved",
                "alias_bindings": [],
                "evidence": [],
            },
        ]
    }
    normalized, count = normalize_identity_evidence_atom_ids(ambiguous_response, atoms=atoms)
    assert count == 0
    assert normalized["groups"][0]["evidence"][0]["source_atom_ids"] == ["atom_m_same"]
    assert any(
        "source_atom_ids invalid" in error
        for error in validate_identity_partition_response(
            normalized,
            atoms=atoms,
            prior_entity_ids=set(),
            source_text_by_block={"bk_ch01_b001": "Mira", "bk_ch01_b002": "Mira"},
        )
    )

    unknown_response = json.loads(json.dumps(ambiguous_response))
    unknown_response["groups"][0]["evidence"][0]["source_atom_ids"] = ["atom_missing"]
    normalized, count = normalize_identity_evidence_atom_ids(unknown_response, atoms=atoms)
    assert count == 0
    assert normalized["groups"][0]["evidence"][0]["source_atom_ids"] == ["atom_missing"]
    assert any(
        "source_atom_ids invalid" in error
        for error in validate_identity_partition_response(
            normalized,
            atoms=atoms,
            prior_entity_ids=set(),
            source_text_by_block={"bk_ch01_b001": "Mira", "bk_ch01_b002": "Mira"},
        )
    )


def test_m3_v2_real_ch3_identity_response_passes_mechanical_resolution_ladder() -> None:
    response, atoms, state, source = _real_ch3_identity_shard_one_fixture()

    applied, audit = apply_identity_partition_response(
        state,
        response,
        atoms=atoms,
        source_text_by_block=source,
    )

    assert audit["atom_id_suffix_repaired"] == 6
    assert audit["atom_id_suffix_repaired_members"] == 4
    assert audit["atom_id_suffix_repaired_evidence"] == 2
    assert audit["atom_id_suffix_repaired_aliases"] == 4
    assert audit["reuse_hint_normalized"] == 1
    assert audit["reuse_mint_equivalent"] == 2
    assert audit["reuse_duplicate_unions"] == 1
    entity_ids = {str(row["entity_id"]) for row in applied["entities"]}
    assert "ent_heathcliff" not in entity_ids
    assert {"ent_mr_heathcliff", "ent_mrs_heathcliff", "ent_catherine_earnshaw", "ent_hindley"} <= entity_ids


def test_m3_v2_ch4_shard_jurisdiction_strips_only_foreign_assignment() -> None:
    responses, shards, state, atoms, source = _real_ch4_identity_shard_fixtures()
    normalized, shard_audit = _normalize_identity_responses_by_shard(
        responses,
        identity_shards=shards,
    )
    target = "atom_m_wh_ch04_b044_01__wh_ch04_b044"
    shard_one_group = next(group for group in normalized[0]["groups"] if group["group_key"] == "grp3")
    assert target not in shard_one_group["member_atom_ids"]
    assert any(
        target in row.get("source_atom_ids", []) for row in shard_one_group["evidence"]
    )
    assert shard_audit["member_out_of_shard_stripped"] == 1

    applied, audit = apply_identity_partition_response(
        state,
        {"groups": [group for response in normalized for group in response["groups"]]},
        atoms=atoms,
        source_text_by_block=source,
    )
    assert set(str(atom["atom_id"]) for atom in atoms) <= set(applied["atom_to_entity"])
    assert applied["atom_to_entity"][target] == "ent_hindley"
    assert audit["referent_kind_out_of_enum"] == 1
    family_review = next(
        row
        for row in applied["review_only"]
        if row.get("referent_kind_raw") == "family"
    )
    assert family_review["referent_kind"] == "unknown"
    assert family_review["status"] == "quarantine"


def test_m3_v2_shard_foreign_member_halts_when_owner_does_not_claim_it() -> None:
    atoms = [
        {"atom_id": "atom_mira", "block_id": "bk_ch01_b001", "surface": "Mira"},
        {"atom_id": "atom_rowan", "block_id": "bk_ch01_b002", "surface": "Rowan"},
    ]
    foreign_claim = _single_atom_group(atoms[0])
    foreign_claim["member_atom_ids"] = ["atom_mira", "atom_rowan"]
    foreign_claim["evidence"] = [
        {
            "block_id": "bk_ch01_b001",
            "quote": "Mira",
            "source_atom_ids": ["atom_mira"],
            "supports": "same_identity",
        }
    ]
    normalized, audit = _normalize_identity_responses_by_shard(
        [{"groups": [foreign_claim]}, {"groups": []}],
        identity_shards=[{"items": [atoms[0]]}, {"items": [atoms[1]]}],
    )
    assert audit["member_out_of_shard_stripped"] == 1
    with pytest.raises(M3V2SemanticGateError, match="identity_response_rejected"):
        apply_identity_partition_response(
            empty_m3_v2_state(),
            {"groups": [group for response in normalized for group in response["groups"]]},
            atoms=atoms,
            source_text_by_block={"bk_ch01_b001": "Mira", "bk_ch01_b002": "Rowan"},
        )


def test_m3_v2_out_of_enum_referent_kind_quarantines_even_when_resolved() -> None:
    atom = {"atom_id": "atom_mira", "block_id": "bk_ch01_b001", "surface": "Mira"}
    response = _single_atom_group(atom)
    response["referent_kind"] = "family"
    response["status"] = "resolved"

    applied, audit = apply_identity_partition_response(
        empty_m3_v2_state(),
        {"groups": [response]},
        atoms=[atom],
        source_text_by_block={"bk_ch01_b001": "Mira"},
    )
    assert audit["referent_kind_out_of_enum"] == 1
    assert applied["entities"][0]["referent_kind"] == "unknown"
    assert applied["entities"][0]["status"] == "quarantine"
    assert applied["review_only"][0]["referent_kind_raw"] == "family"


def test_m3_v2_wrong_suffix_repair_remains_strict_when_mention_prefix_is_ambiguous() -> None:
    atoms = [
        {
            "atom_id": "atom_m_same__bk_ch01_b001",
            "mention_id": "m_same",
            "surface": "Mira",
            "block_id": "bk_ch01_b001",
        },
        {
            "atom_id": "atom_m_same__bk_ch01_b002",
            "mention_id": "m_same",
            "surface": "Mira",
            "block_id": "bk_ch01_b002",
        },
    ]
    first = _single_atom_group(atoms[0])
    first["member_atom_ids"] = ["atom_m_same__bk_ch01_b999"]
    first["alias_bindings"][0]["member_atom_ids"] = ["atom_m_same__bk_ch01_b999"]
    response = {"groups": [first, _single_atom_group(atoms[1])]}

    with pytest.raises(M3V2SemanticGateError, match="identity_response_rejected"):
        apply_identity_partition_response(
            empty_m3_v2_state(),
            response,
            atoms=atoms,
            source_text_by_block={"bk_ch01_b001": "Mira", "bk_ch01_b002": "Mira"},
        )


def test_m3_v2_suffix_repair_cannot_bypass_exact_partition() -> None:
    atoms = [
        {
            "atom_id": "atom_m_one__bk_ch01_b001",
            "mention_id": "m_one",
            "surface": "Mira",
            "block_id": "bk_ch01_b001",
        },
        {
            "atom_id": "atom_m_two__bk_ch01_b002",
            "mention_id": "m_two",
            "surface": "Rowan",
            "block_id": "bk_ch01_b002",
        },
    ]
    repaired_then_duplicate = _single_atom_group(atoms[0])
    repaired_then_duplicate["member_atom_ids"] = ["atom_m_one__bk_ch01_b999"]
    duplicate = _single_atom_group(atoms[0])
    duplicate["group_key"] = "duplicate_after_repair"

    with pytest.raises(M3V2SemanticGateError, match="identity_response_rejected"):
        apply_identity_partition_response(
            empty_m3_v2_state(),
            {"groups": [repaired_then_duplicate, duplicate]},
            atoms=atoms,
            source_text_by_block={"bk_ch01_b001": "Mira", "bk_ch01_b002": "Rowan"},
        )


def test_m3_v2_identity_gate_rejects_unknown_reuse_and_split_tie(tmp_path: Path) -> None:
    document, m1_dir, _m2_dir = _make_chain(tmp_path)
    checkpoint = read_checkpoint(_checkpoint_path(m1_dir, "m1", "bk_ch01"))
    atoms = build_identity_atoms_as_of(
        document=document,
        m1_dir=m1_dir,
        m1_checkpoints=[checkpoint],
    )["atoms"]
    source = {
        block["block_id"]: block["clean_text"]
        for block in document["chapters"][0]["blocks"]
    }

    unknown_reuse = _valid_identity_response(atoms)
    unknown_reuse["groups"][0]["reuse_entity_id"] = "ent_not_in_prior_state"
    with pytest.raises(M3V2SemanticGateError, match="identity_response_rejected"):
        apply_identity_partition_response(
            empty_m3_v2_state(),
            unknown_reuse,
            atoms=atoms,
            source_text_by_block=source,
        )

    prior = empty_m3_v2_state()
    prior["entities"] = [
        {
            "entity_id": "ent_existing",
            "canonical": "the master",
            "referent_kind": "person",
            "member_atom_ids": [],
            "aliases": [],
            "supersedes_entity_ids": [],
            "status": "resolved",
        }
    ]
    split_tie = {
        "groups": [
            _single_atom_group(atoms[0], reuse_entity_id="ent_existing"),
            _single_atom_group(atoms[1], reuse_entity_id="ent_existing"),
        ]
    }
    split_tie["groups"][0]["evidence"] = [
        {
            "block_id": str(atoms[0]["block_id"]),
            "quote": str(atoms[0]["surface"]),
            "source_atom_ids": [str(atoms[0]["atom_id"]), str(atoms[1]["atom_id"])],
            "supports": "different_identity",
        }
    ]
    with pytest.raises(M3V2SemanticGateError, match="stable_id_split_tie"):
        apply_identity_partition_response(
            prior,
            split_tie,
            atoms=atoms,
            source_text_by_block=source,
        )


def test_m3_v2_phase_apply_marks_fact_published_and_real_messages_omit_scaffold_note() -> None:
    source = {"bk_ch02_b001": "Mira warned Rowan about the house."}
    state = empty_m3_v2_state()
    state["entities"] = [
        {
            "entity_id": "ent_mira",
            "canonical": "Mira",
            "referent_kind": "person",
            "member_atom_ids": [],
            "aliases": [],
            "supersedes_entity_ids": [],
            "status": "resolved",
        },
        {
            "entity_id": "ent_rowan",
            "canonical": "Rowan",
            "referent_kind": "person",
            "member_atom_ids": [],
            "aliases": [],
            "supersedes_entity_ids": [],
            "status": "resolved",
        },
    ]
    phase = {
        "relation_facts": [
            {
                "subject_ref": "ent_mira",
                "predicate_code": "neighbor_of",
                "object_ref": "ent_rowan",
                "valid_from_block": "bk_ch02_b001",
                "evidence_block": "bk_ch02_b001",
                "evidence_quote": "Mira warned Rowan",
                "predicate_note": "",
            }
        ],
        "relation_phases": [
            {
                "pair": ["ent_mira", "ent_rowan"],
                "phase_label": "strained",
                "valid_from_block": "bk_ch02_b001",
                "valid_until_block": None,
                "trigger_block": "bk_ch02_b001",
                "trigger_evidence": "warned",
                "status": "open",
            }
        ],
    }
    applied, audit = apply_phase_segment_response(
        state,
        phase,
        allowed_pairs={("ent_mira", "ent_rowan")},
        source_text_by_block=source,
        block_ordinals={"bk_ch02_b001": 1},
    )
    assert audit["facts_applied"] == 1
    assert applied["relation_facts"][0]["status"] == "published"
    assert applied["relation_phases"][0]["phase_label"] == "strained"

    identity_messages = build_identity_messages(
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch01",
        scope="M3_asof_bk_ch01",
        atoms=[],
        prior_groups=[],
        identity_hints=[],
        scaffold_only=False,
    )
    phase_messages = build_phase_messages(
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch01",
        scope="M3_asof_bk_ch01",
        phase_rows=[],
        scaffold_only=False,
    )
    assert "dry_run_note" not in identity_messages[1]["content"]
    assert "dry_run_note" not in phase_messages[1]["content"]


def test_m3_v2_pair_quarantine_publishes_other_pairs_and_hides_blocked_runtime_pair(
    tmp_path: Path,
) -> None:
    """A bad pair cannot erase a valid pair or leak into runtime policy output."""

    source = {
        "bk_ch01_b001": "Mira welcomed Rowan.",
        "bk_ch01_b002": "Mira later distrusted Rowan.",
        "bk_ch01_b003": "Rowan warned Tala.",
    }
    state = empty_m3_v2_state()
    state["entities"] = [
        {
            "entity_id": entity_id,
            "canonical": entity_id.removeprefix("ent_").title(),
            "referent_kind": "person",
            "member_atom_ids": [],
            "aliases": [],
            "supersedes_entity_ids": [],
            "status": "resolved",
        }
        for entity_id in ["ent_mira", "ent_rowan", "ent_tala"]
    ]
    prior_phase = {
        "pair": ["ent_mira", "ent_rowan"],
        "phase_label": "friendly",
        "valid_from_block": "bk_ch01_b001",
        "valid_until_block": None,
        "trigger_block": "bk_ch01_b001",
        "trigger_evidence": "welcomed Rowan",
        "status": "open",
    }
    state["relation_phases"] = [copy.deepcopy(prior_phase)]
    response = {
        "relation_facts": [],
        "relation_phases": [
            {
                "pair": ["ent_mira", "ent_rowan"],
                "phase_label": "strained",
                "valid_from_block": "bk_ch01_b002",
                "valid_until_block": None,
                "trigger_block": "bk_ch01_b002",
                "trigger_evidence": "not in the source",
                "status": "open",
            },
            {
                "pair": ["ent_rowan", "ent_tala"],
                "phase_label": "strained",
                "valid_from_block": "bk_ch01_b003",
                "valid_until_block": None,
                "trigger_block": "bk_ch01_b003",
                "trigger_evidence": "warned Tala",
                "status": "open",
            },
        ],
    }

    applied, audit = apply_phase_segment_response(
        state,
        response,
        allowed_pairs={
            ("ent_mira", "ent_rowan"),
            ("ent_rowan", "ent_tala"),
        },
        source_text_by_block=source,
        block_ordinals={block_id: index for index, block_id in enumerate(source)},
        scope_end_block="bk_ch01_b003",
    )

    assert audit["pairs_blocked_for_runtime"] == 1
    assert audit["phases_applied"] == 1
    assert prior_phase in applied["relation_phases"]
    assert any(
        row["pair"] == ["ent_mira", "ent_rowan"]
        and row["prior_history_retained"] is True
        and row["needs_human_review"] is True
        for row in applied["review_only"]
        if row.get("kind") == "relation_pair_blocked_for_runtime"
    )

    story = build_story_bible_v2(
        chapter={"chapter_id": "bk_ch01", "blocks": []},
        state=applied,
        m1_dir=tmp_path,
        m1_checkpoints=[],
        digests=[{"chapter_id": "bk_ch01", "narration_frame_segments": []}],
    )
    blocked_pair = {"ent_mira", "ent_rowan"}
    assert all(set(row["pair"]) != blocked_pair for row in story["entity_relations"])
    assert all(set(row["pair"]) != blocked_pair for row in story["address_policies"])
    assert story["blocked_for_runtime_pairs"][0]["pair"] == ["ent_mira", "ent_rowan"]


def test_m3_v2_pair_quarantine_halts_when_every_returned_pair_is_rejected() -> None:
    state = empty_m3_v2_state()
    state["entities"] = [
        {
            "entity_id": entity_id,
            "canonical": entity_id.removeprefix("ent_").title(),
            "referent_kind": "person",
            "member_atom_ids": [],
            "aliases": [],
            "supersedes_entity_ids": [],
            "status": "resolved",
        }
        for entity_id in ["ent_mira", "ent_rowan"]
    ]
    response = {
        "relation_facts": [],
        "relation_phases": [
            {
                "pair": ["ent_mira", "ent_rowan"],
                "phase_label": "friendly",
                "valid_from_block": "bk_ch01_b001",
                "valid_until_block": None,
                "trigger_block": "bk_ch01_b001",
                "trigger_evidence": "not in the source",
                "status": "open",
            }
        ],
    }
    with pytest.raises(M3V2SemanticGateError) as exc_info:
        apply_phase_segment_response(
            state,
            response,
            allowed_pairs={("ent_mira", "ent_rowan")},
            source_text_by_block={"bk_ch01_b001": "Mira welcomed Rowan."},
            block_ordinals={"bk_ch01_b001": 0},
            scope_end_block="bk_ch01_b001",
        )
    assert exc_info.value.code == "phase_all_pairs_blocked_for_runtime"


def test_m3_v2_real_ch4_phase_fixture_quarantines_only_reported_speech_pair(
    tmp_path: Path,
) -> None:
    """The real cached ch4 response is the regression fixture for Amendment #10."""

    root = RUNTIME_ROOT / "data" / "reports" / "literary_m4d_b4v2"
    raw_path = (
        root
        / "raw_responses"
        / "m3_v2"
        / "wh_ch04"
        / "literary_phase_segment_v2_shard_01_attempt_01.json"
    )
    if not raw_path.is_file():
        pytest.skip("real M3 v2 ch4 phase fixture is not present")

    responses, shards, state, atoms, source = _real_ch4_identity_shard_fixtures()
    responses, _shard_audit = _normalize_identity_responses_by_shard(
        responses,
        identity_shards=shards,
    )
    state, _identity_audit = apply_identity_partition_response(
        state,
        {"groups": [group for response in responses for group in response.get("groups") or []]},
        atoms=atoms,
        source_text_by_block=source,
    )
    response = json.loads(raw_path.read_text(encoding="utf-8"))["parsed_json"]
    allowed_pairs = {
        tuple(sorted(str(value) for value in row["pair"]))
        for row in response["relation_phases"]
    }
    applied, audit = apply_phase_segment_response(
        state,
        response,
        allowed_pairs=allowed_pairs,
        source_text_by_block=source,
        block_ordinals={block_id: index for index, block_id in enumerate(source)},
        scope_end_block="wh_ch04_b044",
    )

    blocked_pair = {"ent_mr_heathcliff", "ent_the_master"}
    assert audit["pairs_blocked_for_runtime"] == 1
    assert audit["phases_applied"] == 8
    assert any(
        set(row["pair"]) == blocked_pair
        and len(row["returned_relation_phases"]) == 2
        and any("trigger_evidence not_source_substring" in error for error in row["reject_reasons"])
        for row in applied["review_only"]
        if row.get("kind") == "relation_pair_blocked_for_runtime"
    )
    story = build_story_bible_v2(
        chapter={"chapter_id": "wh_ch04", "blocks": []},
        state=applied,
        m1_dir=tmp_path,
        m1_checkpoints=[],
        digests=[{"chapter_id": "wh_ch04", "narration_frame_segments": []}],
    )
    assert all(set(row["pair"]) != blocked_pair for row in story["entity_relations"])
    assert all(set(row["pair"]) != blocked_pair for row in story["address_policies"])


def test_m3_v2_request_hook_uses_runtime_prompt_and_persists_raw_usage(tmp_path: Path) -> None:
    """The hook receives the exact in-loop prompt and persists before apply/publish."""

    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)
    observed: list[tuple[list[dict[str, str]], dict]] = []

    def request_llm(messages: list[dict[str, str]], meta: dict) -> LLMResult:
        observed.append((messages, dict(meta)))
        user = json.loads(messages[1]["content"])
        if meta["mode"] == "literary_identity_partition_v1":
            return _hook_result(_valid_identity_response(user["atoms"]))
        return _hook_result({"relation_facts": [], "relation_phases": []})

    out_dir = tmp_path / "m3_hook"
    report = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        request_llm=request_llm,
        confirm_usd=1.0,
    )
    assert report["zero_api"] is False
    assert report["status"] == "needs_claude_gate"
    assert report["request_accounting"]["combined"] == {
        "logical_calls": 2,
        "attempts": 2,
        "technical_retries": 0,
        "poisoned_cache_replays": 0,
        "cache_hits": 0,
        "cost_usd": 0.000034,
        "prompt_tokens": 34,
        "cached_tokens": 0,
        "completion_tokens": 18,
        "reasoning_tokens": 0,
    }
    assert [meta["mode"] for _messages, meta in observed] == [
        "literary_identity_partition_v1",
        "literary_phase_segment_v2",
    ]
    assert all("dry_run_note" not in messages[1]["content"] for messages, _meta in observed)

    dry = run_m3_v2_dry_run(
        document,
        ["bk_ch01"],
        out_dir=tmp_path / "m3_dry",
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
    )
    rendered = [
        json.loads(Path(row["path"]).read_text(encoding="utf-8"))
        for row in dry["rendered_prompts"]
    ]
    for mode, messages in [
        (meta["mode"], messages) for messages, meta in observed
    ]:
        scaffold = next(row for row in rendered if row["mode"] == mode)
        expected_user = json.loads(scaffold["messages"][1]["content"])
        assert expected_user.pop("dry_run_note")
        assert scaffold["messages"][0] == messages[0]
        assert expected_user == json.loads(messages[1]["content"])

    checkpoint = read_checkpoint(out_dir / "checkpoints" / M3_V2_STAGE / "bk_ch01.json")
    raw = checkpoint["raw_responses"]
    assert len(raw) == 2
    assert all(row["source"] == "request_llm" for row in raw)
    assert all(row["usage"]["prompt_tokens"] == 17 for row in raw)
    assert all((out_dir / row["raw_response_path"]).is_file() for row in raw)


def test_m3_v2_config_bump_rebuilds_prefix_from_stocked_replay_cache(tmp_path: Path) -> None:
    """A response-shaping cap invalidates checkpoints but not compatible replay entries."""

    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)
    atoms = build_identity_atoms_as_of(
        document=document,
        m1_dir=m1_dir,
        m1_checkpoints=[read_checkpoint(_checkpoint_path(m1_dir, "m1", "bk_ch01"))],
    )["atoms"]
    out_dir = tmp_path / "m3_config_rebuild"
    responses = {
        "M3_asof_bk_ch01": {
            "identity": _valid_identity_response(atoms),
            "phase": {"relation_facts": [], "relation_phases": []},
        }
    }
    first = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        responses_by_scope=responses,
    )
    assert first["status"] == "needs_claude_gate"
    story_before = json.loads(
        (out_dir / "story_bible_v2" / "bk_ch01_story_bible.json").read_text(encoding="utf-8")
    )

    cached_calls: list[dict] = []

    def stocked_replay_cache(messages: list[dict[str, str]], meta: dict) -> LLMResult:
        cached_calls.append(dict(meta))
        user = json.loads(messages[1]["content"])
        if meta["mode"] == "literary_identity_partition_v1":
            return _hook_result(_valid_identity_response(user["atoms"]), from_cache=True)
        return _hook_result({"relation_facts": [], "relation_phases": []}, from_cache=True)

    rebuilt = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=replace(_config(), max_output_tokens=1024),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        request_llm=stocked_replay_cache,
        confirm_usd=1.0,
        resume=True,
    )
    assert rebuilt["status"] == "needs_claude_gate"
    assert rebuilt["resume"]["restored"] == []
    assert rebuilt["resume"]["mismatches"] == [{"chapter_id": "bk_ch01", "fields": ["config_hash"]}]
    assert len(cached_calls) == 2
    assert all(meta["bypass_cache"] is False for meta in cached_calls)
    accounting = rebuilt["request_accounting"]["combined"]
    assert accounting["cache_hits"] == 2
    assert accounting["technical_retries"] == 0
    assert accounting["poisoned_cache_replays"] == 0
    story_after = json.loads(
        (out_dir / "story_bible_v2" / "bk_ch01_story_bible.json").read_text(encoding="utf-8")
    )
    assert story_after == story_before


def test_m3_v2_poisoned_cache_replay_bypasses_once_and_is_audited(tmp_path: Path) -> None:
    """A cached truncated JSON is replayed for audit, then retried as a fresh request."""

    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)
    calls: list[dict] = []

    def request_llm(messages: list[dict[str, str]], meta: dict) -> LLMResult:
        calls.append(dict(meta))
        user = json.loads(messages[1]["content"])
        if meta["mode"] == "literary_identity_partition_v1" and meta["attempt_index"] == 1:
            return _hook_result(None, json_error="unterminated_json", from_cache=True)
        if meta["mode"] == "literary_identity_partition_v1":
            return _hook_result(_valid_identity_response(user["atoms"]))
        return _hook_result({"relation_facts": [], "relation_phases": []})

    out_dir = tmp_path / "m3_poisoned_cache"
    report = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        request_llm=request_llm,
        confirm_usd=1.0,
    )
    assert report["status"] == "needs_claude_gate"
    accounting = report["request_accounting"]["combined"]
    assert accounting["logical_calls"] == 2
    assert accounting["attempts"] == 3
    assert accounting["cache_hits"] == 1
    assert accounting["poisoned_cache_replays"] == 1
    assert accounting["technical_retries"] == 0
    assert calls[1]["bypass_cache"] is True
    checkpoint = read_checkpoint(out_dir / "checkpoints" / M3_V2_STAGE / "bk_ch01.json")
    poisoned = [
        row for row in checkpoint["raw_responses"] if row["technical_failure_class"] == "poisoned_cache_replay"
    ]
    assert len(poisoned) == 1
    raw = json.loads((out_dir / poisoned[0]["raw_response_path"]).read_text(encoding="utf-8"))
    assert raw["from_cache"] is True
    assert raw["technical_failure_class"] == "poisoned_cache_replay"


def test_m3_v2_poisoned_cache_replay_is_excluded_from_ten_percent_retry_gate(tmp_path: Path) -> None:
    """One free poison across eight logical calls would be 12.5% under the old formula."""

    document = _document_with_chapter_count(4)
    document, m1_dir, m2_dir = _make_chain(
        tmp_path,
        include_relation=False,
        document=document,
    )
    identity_attempts = 0

    def request_llm(messages: list[dict[str, str]], meta: dict) -> LLMResult:
        nonlocal identity_attempts
        user = json.loads(messages[1]["content"])
        if meta["mode"] == "literary_identity_partition_v1":
            identity_attempts += 1
            if identity_attempts == 1:
                return _hook_result(None, json_error="truncated", from_cache=True)
            return _hook_result(_valid_identity_response(user["atoms"]))
        return _hook_result({"relation_facts": [], "relation_phases": []})

    report = run_m3_v2_from_responses(
        document,
        ["bk_ch01", "bk_ch02", "bk_ch03", "bk_ch04"],
        out_dir=tmp_path / "m3_poisoned_rate",
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        request_llm=request_llm,
        confirm_usd=1.0,
        max_technical_retry_rate=0.10,
    )
    assert report["status"] == "needs_claude_gate"
    accounting = report["request_accounting"]["combined"]
    assert accounting["logical_calls"] == 8
    assert accounting["attempts"] == 9
    assert accounting["poisoned_cache_replays"] == 1
    assert accounting["technical_retries"] == 0
    assert 1 / accounting["logical_calls"] == pytest.approx(0.125)

def test_m3_v2_request_hook_halts_when_parse_retry_rate_exceeds_gate(tmp_path: Path) -> None:
    """A parse repair is technical, but excessive repair cannot publish a scope."""

    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)
    identity_calls = 0

    def request_llm(messages: list[dict[str, str]], meta: dict) -> LLMResult:
        nonlocal identity_calls
        if meta["mode"] == "literary_identity_partition_v1":
            identity_calls += 1
            if identity_calls == 1:
                return _hook_result(None, json_error="invalid_json")
            user = json.loads(messages[1]["content"])
            return _hook_result(_valid_identity_response(user["atoms"]))
        return _hook_result({"relation_facts": [], "relation_phases": []})

    out_dir = tmp_path / "m3_retry_gate"
    report = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        request_llm=request_llm,
        confirm_usd=1.0,
    )
    assert report["status"] == "halted_technical_gate"
    assert report["gate_code"] == "technical_retry_rate_exceeded"
    assert report["request_accounting"]["combined"]["technical_retries"] == 1
    assert report["request_accounting"]["combined"]["poisoned_cache_replays"] == 0
    assert not (out_dir / "story_bible_v2" / "bk_ch01_story_bible.json").exists()
    assert not (out_dir / "checkpoints" / M3_V2_STAGE / "bk_ch01.json").exists()
    raw_paths = list((out_dir / "raw_responses" / M3_V2_STAGE / "bk_ch01").glob("*.json"))
    assert len(raw_paths) == 3


def test_m3_v2_request_hook_retains_transport_attempt_accounting_on_halt(tmp_path: Path) -> None:
    document, m1_dir, m2_dir = _make_chain(tmp_path, include_relation=False)

    def request_llm(_messages: list[dict[str, str]], _meta: dict) -> LLMResult:
        raise RuntimeError("synthetic transport outage")

    out_dir = tmp_path / "m3_transport_halt"
    report = run_m3_v2_from_responses(
        document,
        ["bk_ch01"],
        out_dir=out_dir,
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        request_llm=request_llm,
        confirm_usd=1.0,
    )
    assert report["status"] == "halted_technical_gate"
    assert report["gate_code"] == "request_llm_parse_or_transport_failed"
    assert report["request_accounting"]["combined"]["attempts"] == 2
    assert report["request_accounting"]["combined"]["technical_retries"] == 1
    raw_paths = list((out_dir / "raw_responses" / M3_V2_STAGE / "bk_ch01").glob("*.json"))
    assert len(raw_paths) == 2
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["technical_error"]["type"] == "RuntimeError"
        for path in raw_paths
    )


def test_m3_v2_normalizes_real_phase_response_quotes_and_range() -> None:
    raw_path = (
        REPO_ROOT
        / "THESIS_RUNTIME_TOOL"
        / "data"
        / "reports"
        / "literary_m4d_b4v2"
        / "raw_responses"
        / "m3_v2"
        / "wh_ch01"
        / "literary_phase_segment_v2_shard_01_attempt_01.json"
    )
    if not raw_path.is_file():
        pytest.skip("real M3 v2 phase response artifact is not present")

    response = json.loads(raw_path.read_text(encoding="utf-8"))["parsed_json"]
    source = {
        "wh_ch01_b003": "Mr. Heathcliff?",
        "wh_ch01_b005": "Mr. Lockwood, your new tenant",
        "wh_ch01_b008": "Joseph, take Mr. Lockwood\u2019s horse",
        "wh_ch01_b010": "The Lord help us!",
        "wh_ch01_b025": "Heathcliff watched in silence.",
        "wh_ch01_b026": "Heathcliff\u2019s countenance relaxed into a grin.",
        "wh_ch01_b027": "you are flurried, Mr. Lockwood",
    }
    block_ordinals = {block_id: index for index, block_id in enumerate(source)}
    entity_ids = sorted(
        {
            str(value)
            for phase in response["relation_phases"]
            for value in phase["pair"]
        }
    )
    state = empty_m3_v2_state()
    state["entities"] = [{"entity_id": entity_id} for entity_id in entity_ids]
    allowed_pairs = {
        tuple(sorted(str(value) for value in phase["pair"]))
        for phase in response["relation_phases"]
    }

    applied, audit = apply_phase_segment_response(
        state,
        response,
        allowed_pairs=allowed_pairs,
        source_text_by_block=source,
        block_ordinals=block_ordinals,
        scope_end_block="wh_ch01_b027",
    )

    assert audit["evidence_quote_punct_normalized"] == 3
    friendly = next(
        phase
        for phase in applied["relation_phases"]
        if phase["phase_label"] == "friendly"
    )
    assert friendly["valid_from_block"] == "wh_ch01_b026"
    assert friendly["trigger_evidence_block"] == "wh_ch01_b027"
    assert friendly["trigger_evidence"] == source["wh_ch01_b027"]
    assert [
        fact["evidence_quote"]
        for fact in applied["relation_facts"]
        if fact["evidence_block"] == "wh_ch01_b008"
    ] == [source["wh_ch01_b008"], source["wh_ch01_b008"]]


def test_m3_v2_quote_normalization_keeps_identity_strict_for_ambiguous_matches() -> None:
    atoms = [
        {"atom_id": "atom_mira", "block_id": "bk_ch01_b001", "surface": "Mira"},
        {"atom_id": "atom_miss_mira", "block_id": "bk_ch01_b002", "surface": "Miss Mira"},
    ]
    response = _valid_identity_response(atoms)
    response["groups"][0]["evidence"][0]["quote"] = "Mira\'s coat"
    source = {
        "bk_ch01_b001": "Mira\u2019s coat was wet.",
        "bk_ch01_b002": "Miss Mira replied.",
    }
    audit = {"evidence_quote_punct_normalized": 0}
    assert validate_identity_partition_response(
        response,
        atoms=atoms,
        prior_entity_ids=set(),
        source_text_by_block=source,
        quote_audit=audit,
    ) == []
    assert audit["evidence_quote_punct_normalized"] == 1
    assert response["groups"][0]["evidence"][0]["quote"] == source["bk_ch01_b001"][:11]

    ambiguous = json.loads(json.dumps(response))
    ambiguous["groups"][0]["evidence"][0]["quote"] = "Mira\'s coat"
    ambiguous_source = {
        "bk_ch01_b001": "Mira\u2019s coat; Mira\u2019s coat.",
        "bk_ch01_b002": "Miss Mira replied.",
    }
    assert any(
        "quote not_source_substring" in error
        for error in validate_identity_partition_response(
            ambiguous,
            atoms=atoms,
            prior_entity_ids=set(),
            source_text_by_block=ambiguous_source,
        )
    )


def test_m3_v2_phase_quote_outside_range_stays_rejected() -> None:
    response = {
        "relation_phases": [
            {
                "pair": ["ent_mira", "ent_rowan"],
                "phase_label": "friendly",
                "valid_from_block": "bk_ch01_b001",
                "valid_until_block": None,
                "trigger_block": "bk_ch01_b001",
                "trigger_evidence": "later evidence",
                "status": "open",
            }
        ],
        "relation_facts": [],
    }
    source = {
        "bk_ch01_b001": "Mira arrived.",
        "bk_ch01_b002": "later evidence",
    }
    errors = validate_phase_segment_response(
        response,
        entity_ids={"ent_mira", "ent_rowan"},
        source_text_by_block=source,
        block_ordinals={"bk_ch01_b001": 0, "bk_ch01_b002": 1},
        scope_end_block="bk_ch01_b001",
    )
    assert "relation_phases[0].trigger_evidence not_source_substring" in errors


def test_m3_v2_raw_response_files_are_append_only_across_resume_attempts(tmp_path: Path) -> None:
    meta = {
        "scope": "M3_asof_bk_ch01",
        "chapter_id": "bk_ch01",
        "mode": "literary_identity_partition_v1",
        "shard_index": 1,
        "shard_count": 1,
        "attempt_index": 1,
        "tag": "synthetic-raw-append",
        "prompt_tokens_est": 1,
    }
    first_path, _first_record = _persist_m3_v2_raw_response(
        tmp_path,
        messages=[{"role": "user", "content": "first"}],
        meta=meta,
        source="provided_synthetic",
        provided_json={"marker": "first"},
    )
    second_path, _second_record = _persist_m3_v2_raw_response(
        tmp_path,
        messages=[{"role": "user", "content": "second"}],
        meta=meta,
        source="provided_synthetic",
        provided_json={"marker": "second"},
    )
    assert first_path != second_path
    assert "_resume_01" in second_path.name
    assert json.loads(first_path.read_text(encoding="utf-8"))["parsed_json"] == {"marker": "first"}
    assert json.loads(second_path.read_text(encoding="utf-8"))["parsed_json"] == {"marker": "second"}


def test_m3_v2_llm_client_adapter_uses_json_cache_contract() -> None:
    calls: list[dict] = []

    class FakeClient:
        def call(self, messages, **kwargs):
            calls.append({"messages": messages, **kwargs})
            return _hook_result({"groups": []})

    adapter = make_m3_v2_request_llm(FakeClient())
    result = adapter(
        [{"role": "user", "content": "{}"}],
        {"tag": "m3v2-test", "bypass_cache": True},
    )
    assert result.parsed_json == {"groups": []}
    assert calls == [
        {
            "messages": [{"role": "user", "content": "{}"}],
            "response_format": {"type": "json_object"},
            "tag": "m3v2-test",
            "bypass_cache": True,
        }
    ]
