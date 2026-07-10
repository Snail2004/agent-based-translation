from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import (
    M1_CHECKPOINT_SCHEMA_VERSION,
    M2_CHECKPOINT_SCHEMA_VERSION,
    _checkpoint_path,
    _checkpoint_prompt_hashes,
    load_system_prompt_from_design,
)
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
    apply_identity_partition_response,
    apply_phase_segment_response,
    build_identity_messages,
    build_identity_atoms_as_of,
    build_m3_v2_checkpoint,
    build_phase_messages,
    empty_m3_v2_state,
    load_m3_v2_input_chain,
    make_m3_v2_request_llm,
    run_m3_v2_from_responses,
    run_m3_v2_dry_run,
    validate_identity_partition_response,
    validate_phase_segment_response,
    write_m3_v2_checkpoint_atomic,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _config() -> LLMConfig:
    return LLMConfig(
        model="fake-m3-v2",
        temperature=0.0,
        reasoning_effort="none",
        max_output_tokens=512,
        prompt_token_cap=8_000,
        pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
    )


def _hook_result(payload: dict | None, *, json_error: str | None = None) -> LLMResult:
    text = json.dumps(payload, ensure_ascii=False) if payload is not None else "{bad json"
    return LLMResult(
        text=text,
        parsed_json=payload,
        json_error=json_error,
        model="fake-m3-v2",
        system_fingerprint="fp_m3v2_test",
        usage=LLMUsage(prompt_tokens=17, cached_tokens=0, completion_tokens=9),
        cost_usd=0.000017,
        latency_ms=2,
        from_cache=False,
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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_chain(tmp_path: Path, *, include_relation: bool = True) -> tuple[dict, Path, Path]:
    document = _document()
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
