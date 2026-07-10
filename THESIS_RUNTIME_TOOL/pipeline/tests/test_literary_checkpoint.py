from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import (
    _checkpoint_config_hash,
    _checkpoint_path,
    _checkpoint_prompt_hashes,
    _load_valid_checkpoint_prefix,
    roster_from_ledger,
    run_m1,
    run_m2,
)
from pipeline.literary.checkpoint import (
    CheckpointLock,
    CheckpointLockedError,
    artifact_manifest,
    build_checkpoint,
    canonical_hash,
    chapter_source_hash,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint_atomic,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _document() -> dict:
    return {
        "chapters": [
            {
                "chapter_id": "bk_ch01",
                "blocks": [
                    {
                        "block_id": "bk_ch01_b001",
                        "order_index": 1,
                        "clean_text": "Alice enters the hall.",
                    }
                ],
            },
            {
                "chapter_id": "bk_ch02",
                "blocks": [
                    {
                        "block_id": "bk_ch02_b001",
                        "order_index": 2,
                        "clean_text": "Bob greets Alice.",
                    }
                ],
            },
        ]
    }


class FakeLiteraryClient:
    def __init__(self) -> None:
        self.digest_prompts: dict[str, str] = {}
        self.calls = 0

    def call(self, messages, *, response_format=None, tag="", bypass_cache=False):
        del response_format, bypass_cache
        self.calls += 1
        user = str(messages[-1]["content"])
        chapter_id = "bk_ch02" if "bk_ch02" in tag else "bk_ch01"
        block_id = f"{chapter_id}_b001"
        if "literary_chapter_brief_v1" in tag:
            name = "Bob" if chapter_id == "bk_ch02" else "Alice"
            payload = {
                "chapter_id": chapter_id,
                "cast_on_stage": [
                    {
                        "surface": name,
                        "surface_kind": "proper_name",
                        "role_hint": "visitor",
                        "first_seen_block": block_id,
                    }
                ],
                "setting": {
                    "place": "a hall",
                    "time_frame_hint": "frame_present",
                    "scene_shape": "single_scene_one_location",
                },
                "scenes_party_size": [
                    {
                        "block_range": [block_id, block_id],
                        "co_present_count": 1,
                        "participants": [name],
                    }
                ],
                "neutral_premise": f"{name} enters a hall.",
            }
        elif "literary_lexicon_v1" in tag:
            payload = {
                "chapter_id": chapter_id,
                "window_block_ids": [block_id],
                "context_only_used": False,
                "glossary_candidates": [],
                "character_mentions": [],
            }
        elif "literary_narrative_v1" in tag:
            payload = {
                "chapter_id": chapter_id,
                "window_block_ids": [block_id],
                "context_only_used": False,
                "speaker_turns": [],
                "relation_events": [],
            }
        elif "literary_digest_v1" in tag:
            self.digest_prompts[chapter_id] = user
            payload = {
                "chapter_id": chapter_id,
                "chapter_rolling_summary": f"Digest for {chapter_id}.",
                "narration_frame_segments": [
                    {
                        "narrator_ref": "unknown",
                        "block_range": [block_id, block_id],
                        "story_time_label": "frame_present",
                    }
                ],
                "scene_summaries": [],
                "character_state_changes": [],
                "relation_event_summary": [],
                "unresolved_threads": [],
                "motifs": [],
                "translator_relevant_facts": [],
            }
        else:  # pragma: no cover - protects the fake from silent protocol drift.
            raise AssertionError(f"Unexpected tag: {tag}")
        text = json.dumps(payload, ensure_ascii=False)
        return LLMResult(
            text=text,
            parsed_json=payload,
            json_error=None,
            model="fake-literary",
            system_fingerprint="fixture",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.001,
            latency_ms=1,
            from_cache=False,
            cache_key=f"fake-{self.calls}",
        )


def _config() -> LLMConfig:
    return LLMConfig(
        model="fake-literary",
        temperature=0.2,
        reasoning_effort="none",
        max_output_tokens=256,
        prompt_token_cap=100_000,
    )


def test_checkpoint_hash_manifest_and_atomic_write(tmp_path: Path) -> None:
    artifact = tmp_path / "brief" / "bk_ch01.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"ok":true}\n', encoding="utf-8")
    base = {
        "stage": "m1",
        "chapter_id": "bk_ch01",
        "artifact_manifest": artifact_manifest([artifact], root=tmp_path),
        "state": {"entity_ledger": {"ent_alice": {"canonical": "Alice"}}},
    }
    checkpoint = build_checkpoint(base)
    path = tmp_path / "checkpoints" / "m1" / "bk_ch01.json"
    write_checkpoint_atomic(path, checkpoint)

    loaded = read_checkpoint(path)
    assert validate_checkpoint(
        loaded,
        root=tmp_path,
        expected={"stage": "m1", "chapter_id": "bk_ch01"},
    ) == []
    assert not list(path.parent.glob("*.tmp"))

    artifact.write_text('{"ok":false}\n', encoding="utf-8")
    errors = validate_checkpoint(
        loaded,
        root=tmp_path,
        expected={"stage": "m1", "chapter_id": "bk_ch01"},
    )
    assert "artifact_size:brief/bk_ch01.json" in errors or "artifact_sha256:brief/bk_ch01.json" in errors


def test_checkpoint_lock_blocks_live_owner_and_takes_over_dead_owner(tmp_path: Path) -> None:
    first = CheckpointLock(tmp_path, alive_check=lambda _pid: True).acquire()
    with pytest.raises(CheckpointLockedError):
        CheckpointLock(tmp_path, alive_check=lambda _pid: True).acquire()
    first.release()

    lock_path = tmp_path / "checkpoints" / "lock.json"
    lock_path.write_text(
        json.dumps({"pid": 999999, "host": socket.gethostname(), "token": "stale"}),
        encoding="utf-8",
    )
    takeover = CheckpointLock(tmp_path, alive_check=lambda _pid: False).acquire()
    assert takeover.took_over_stale is True
    takeover.release()


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_prompt_hash_uses_rendered_chapter_id() -> None:
    first = _checkpoint_prompt_hashes(DESIGN_DOC, "m1", "bk_ch01")
    second = _checkpoint_prompt_hashes(DESIGN_DOC, "m1", "bk_ch02")
    assert first != second


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m1_crash_resume_matches_continuous_state_and_pack(tmp_path: Path) -> None:
    document = _document()
    config = _config()
    continuous_dir = tmp_path / "continuous"
    resumed_dir = tmp_path / "resumed"

    continuous = run_m1(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=continuous_dir,
        confirm_usd=10.0,
    )
    run_m1(
        document,
        ["bk_ch01"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=resumed_dir,
        confirm_usd=10.0,
    )
    resumed = run_m1(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=resumed_dir,
        confirm_usd=10.0,
        resume=True,
    )

    assert canonical_hash(continuous["entity_ledger"]) == canonical_hash(resumed["entity_ledger"])
    assert canonical_hash(continuous["chapter_summaries"]) == canonical_hash(
        resumed["chapter_summaries"]
    )
    continuous_ch1 = read_checkpoint(_checkpoint_path(continuous_dir, "m1", "bk_ch01"))
    resumed_ch1 = read_checkpoint(_checkpoint_path(resumed_dir, "m1", "bk_ch01"))
    continuous_pack = roster_from_ledger(continuous_ch1["state"]["entity_ledger"])
    resumed_pack = roster_from_ledger(resumed_ch1["state"]["entity_ledger"])
    assert continuous_pack == resumed_pack
    assert resumed["resume"]["resumed_from_checkpoint"] == ["bk_ch01"]
    assert resumed["resume"]["ran"] == ["bk_ch02"]
    assert resumed["accounting_resume"]["restored_total"]["attempts"] == 3
    assert resumed["accounting_resume"]["this_attempt"]["attempts"] == 3


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_uses_m1_roster_as_of_each_chapter(tmp_path: Path) -> None:
    document = _document()
    config = _config()
    out_dir = tmp_path / "chain"
    run_m1(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=out_dir,
        confirm_usd=10.0,
    )
    digest_client = FakeLiteraryClient()
    report = run_m2(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=config,
        client=digest_client,
        out_dir=out_dir,
        m1_dir=out_dir,
        confirm_usd=10.0,
    )

    assert "ent_alice" in digest_client.digest_prompts["bk_ch01"]
    assert "ent_bob" not in digest_client.digest_prompts["bk_ch01"]
    assert "ent_alice" in digest_client.digest_prompts["bk_ch02"]
    assert "ent_bob" in digest_client.digest_prompts["bk_ch02"]
    m2_ch2 = read_checkpoint(_checkpoint_path(out_dir, "m2", "bk_ch02"))
    m1_ch2 = read_checkpoint(_checkpoint_path(out_dir, "m1", "bk_ch02"))
    assert m2_ch2["input_m1_checkpoint_hash"] == m1_ch2["checkpoint_hash"]
    assert report["actual"]["calls"] == 2


def test_resume_prefix_breaks_on_parent_or_source_change(tmp_path: Path) -> None:
    document = _document()
    selected = document["chapters"]
    config_value = _checkpoint_config_hash(_config(), "m1")
    parent = None
    for index, chapter in enumerate(selected):
        chapter_id = chapter["chapter_id"]
        artifact = tmp_path / "brief" / f"{chapter_id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}\n", encoding="utf-8")
        payload = build_checkpoint(
            {
                "stage": "m1",
                "chapter_id": chapter_id,
                "chapter_index": index,
                "chapter_sequence_prefix": [item["chapter_id"] for item in selected[: index + 1]],
                "source_hash": chapter_source_hash(chapter),
                "prompt_hashes": _checkpoint_prompt_hashes(DESIGN_DOC, "m1", chapter_id),
                "config_hash": config_value,
                "schema_version": "literary_m1_checkpoint_v1",
                "parent_checkpoint_hash": parent,
                "artifact_manifest": artifact_manifest([artifact], root=tmp_path),
                "state": {"entity_ledger": {}, "chapter_summaries": []},
            }
        )
        write_checkpoint_atomic(_checkpoint_path(tmp_path, "m1", chapter_id), payload)
        parent = payload["checkpoint_hash"]

    checkpoints, mismatches = _load_valid_checkpoint_prefix(
        stage="m1",
        selected=selected,
        out_dir=tmp_path,
        design_doc=DESIGN_DOC,
        config_hash_value=config_value,
    )
    assert len(checkpoints) == 2
    assert mismatches == []

    changed = _document()["chapters"]
    changed[1]["blocks"][0]["clean_text"] = "Changed source."
    checkpoints, mismatches = _load_valid_checkpoint_prefix(
        stage="m1",
        selected=changed,
        out_dir=tmp_path,
        design_doc=DESIGN_DOC,
        config_hash_value=config_value,
    )
    assert len(checkpoints) == 1
    assert mismatches[0]["chapter_id"] == "bk_ch02"
    assert "source_hash" in mismatches[0]["fields"]
