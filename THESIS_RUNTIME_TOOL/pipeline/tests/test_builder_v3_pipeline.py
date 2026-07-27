from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary import builder_v3_pipeline as pipeline_v3
from pipeline.literary.builder_pilot import build_literary_windows
from pipeline.literary.builder_v3_pipeline import (
    REQUEST_CONTRACT_HASHES,
    REQUEST_SHAPE_CONTRACT,
    StageAttemptResult,
    SyntheticStageExecutor,
    V3StageRequest,
    _build_request,
    _m1_semantic_projection,
    _m2_semantic_projection,
    run_m1_v3,
    run_m2_v3,
)
from pipeline.literary.checkpoint import CheckpointError, canonical_hash, canonical_json
from pipeline.literary.checkpoint_v3 import (
    M1_CHECKPOINT_SCHEMA_VERSION_V3,
    M1_GROUND_STATE_VERSION_V3,
    M2_CHECKPOINT_SCHEMA_VERSION_V3,
    VALIDATOR_CONTRACT_VERSION,
    builder_v3_root,
    contract_versions,
    current_pointer_path,
    publish_generation,
    read_current_checkpoint,
    read_state_from_checkpoint,
    validate_v3_checkpoint,
)


NAMES = [("Alice", "Bob"), ("Mira", "Ravel"), ("Iris", "Noel")]


def _chapter(number: int, *, extra_blocks: int = 0) -> dict[str, object]:
    chapter_id = f"bk_ch{number:02d}"
    first, second = NAMES[number - 1]
    blocks: list[dict[str, object]] = [
        {
            "block_id": f"{chapter_id}_b001",
            "block_type": "paragraph",
            "order_index": number * 100 + 1,
            "clean_text": f"{first} greeted {second}.",
            "source_text": f"{first} greeted {second}.",
        },
        {
            "block_id": f"{chapter_id}_b002",
            "block_type": "dialogue",
            "order_index": number * 100 + 2,
            "clean_text": f"{second} answered {first}.",
            "source_text": f"{second} answered {first}.",
        },
        {
            "block_id": f"{chapter_id}_b003",
            "block_type": "paragraph",
            "order_index": number * 100 + 3,
            "clean_text": f"{first} and {second} left the room.",
            "source_text": f"{first} and {second} left the room.",
        },
    ]
    for offset in range(extra_blocks):
        ordinal = offset + 4
        marker = "FUTURE_SENTINEL" if ordinal == 6 else f"filler-{number}-{ordinal}"
        blocks.append(
            {
                "block_id": f"{chapter_id}_b{ordinal:03d}",
                "block_type": "paragraph",
                "order_index": number * 100 + ordinal,
                "clean_text": marker,
                "source_text": marker,
            }
        )
    return {"chapter_id": chapter_id, "chapter_label": f"Chapter {number}", "blocks": blocks}


def _document(*, extra_blocks: int = 0) -> dict[str, object]:
    return {
        "document_id": "builder-v3-fixture",
        "chapters": [
            _chapter(number, extra_blocks=extra_blocks if number == 1 else 0)
            for number in range(1, 4)
        ],
    }


def _script_for_chapter(number: int, *, all_blocks: int = 3) -> dict[tuple[str, str, str | None], dict[str, object]]:
    chapter_id = f"bk_ch{number:02d}"
    first, second = NAMES[number - 1]
    block_ids = [f"{chapter_id}_b{value:03d}" for value in range(1, all_blocks + 1)]
    evidence = f"{first} greeted {second}."
    first_mention = f"m_{chapter_id}_b001_01"
    second_mention = f"m_{chapter_id}_b001_02"
    event_id = f"e_{chapter_id}_b001_01"
    return {
        ("b0", chapter_id, None): {
            "chapter_id": chapter_id,
            "cast_claims": [
                {
                    "surface": first,
                    "surface_kind": "proper_name",
                    "referent_kind_claim": "person",
                    "role_hint": "visitor",
                    "scene_range": [block_ids[0], block_ids[-1]],
                    "source_block_ids": [block_ids[0]],
                    "anchor_text": first,
                    "evidence_quote": evidence,
                }
            ],
            "setting": {
                "place": "an unnamed room",
                "time_frame_hint": "frame_present",
                "scene_shape": "few_scenes",
            },
            "scenes_party_size": [
                {
                    "block_range": [block_ids[0], block_ids[-1]],
                    "co_present_count": 2,
                    "participants": [first, second],
                }
            ],
            "neutral_premise": f"{first} and {second} meet.",
        },
        ("b1", chapter_id, f"w_{chapter_id}_01"): {
            "chapter_id": chapter_id,
            "window_block_ids": block_ids,
            "context_only_used": False,
            "character_mentions": [
                {
                    "surface": first,
                    "mention_type": "name",
                    "referent_kind_claim": "person",
                    "anchor_text": first,
                    "evidence_quote": evidence,
                    "block_id": block_ids[0],
                },
                {
                    "surface": second,
                    "mention_type": "name",
                    "referent_kind_claim": "person",
                    "anchor_text": second,
                    "evidence_quote": evidence,
                    "block_id": block_ids[0],
                },
            ],
            "glossary_candidates": [],
        },
        ("b2", chapter_id, f"w_{chapter_id}_01"): {
            "chapter_id": chapter_id,
            "window_block_ids": block_ids,
            "context_only_used": False,
            "speaker_turns": [
                {
                    "speaker": _endpoint(first, evidence, first_mention),
                    "addressee": _endpoint(second, evidence, second_mention),
                    "utterance_quote": evidence,
                    "address_terms": [],
                    "register_cue": "neutral",
                    "block_id": block_ids[0],
                }
            ],
            "relation_events": [
                {
                    "actor": _endpoint(first, evidence, first_mention),
                    "target": _endpoint(second, evidence, second_mention),
                    "event_type": "greets",
                    "evidence_quote": evidence,
                    "block_id": block_ids[0],
                }
            ],
        },
        ("b3", chapter_id, None): {
            "chapter_id": chapter_id,
            "chapter_rolling_summary": f"{first} meets {second} in chapter {number}.",
            "narration_frame_segments": [
                {
                    "local_segment_key": "present",
                    "parent_local_key": None,
                    "narrator_surface": "Narrator",
                    "narrator_ref": first_mention,
                    "frame_kind": "primary_narration",
                    "story_time_label": "frame_present",
                    "block_range": [block_ids[0], block_ids[-1]],
                    "start_boundary": None,
                    "end_boundary": None,
                    "status": "proposed",
                    "evidence_quote": evidence,
                }
            ],
            "relation_observations": [
                {
                    "event_id": event_id,
                    "endpoint_refs": [f"{event_id}#actor", f"{event_id}#target"],
                    "observed_valence_hint": "positive",
                    "block_id": block_ids[0],
                    "evidence_quote": evidence,
                }
            ],
            "character_state_changes": [],
            "unresolved_threads": [],
            "translator_relevant_facts": [],
            "motifs": [
                {
                    "note": "Greetings recur.",
                    "block_ids": [block_ids[0]],
                    "subject_refs": [f"{event_id}#actor"],
                }
            ],
        },
    }


def _endpoint(surface: str, evidence: str, mention_ref: str) -> dict[str, object]:
    return {
        "surface": surface,
        "reference_scope": "individual",
        "referent_kind_claim": "person",
        "mention_ref": mention_ref,
        "attribution_method": "explicit_tag",
        "anchor_text": surface,
        "evidence_quote": evidence,
    }


def _scripts() -> dict[tuple[str, str, str | None], dict[str, object]]:
    result: dict[tuple[str, str, str | None], dict[str, object]] = {}
    for number in range(1, 4):
        result.update(_script_for_chapter(number))
    return result


def _sentinel_scripts() -> dict[tuple[str, str, str | None], dict[str, object]]:
    chapter_id = "bk_ch01"
    block_ids = [f"{chapter_id}_b{value:03d}" for value in range(1, 7)]
    scripts: dict[tuple[str, str, str | None], dict[str, object]] = {
        ("b0", chapter_id, None): {
            "chapter_id": chapter_id,
            "cast_claims": [
                {
                    "surface": "Alice",
                    "surface_kind": "proper_name",
                    "referent_kind_claim": "person",
                    "role_hint": "ROLE_NEAR_SENTINEL",
                    "scene_range": [block_ids[0], block_ids[0]],
                    "source_block_ids": [block_ids[0]],
                    "anchor_text": "Alice",
                    "evidence_quote": "Alice greeted Bob.",
                },
                {
                    "surface": "FUTURE_SENTINEL",
                    "surface_kind": "descriptor",
                    "referent_kind_claim": "unknown",
                    "role_hint": "ROLE_FAR_SENTINEL",
                    "scene_range": [block_ids[-1], block_ids[-1]],
                    "source_block_ids": [block_ids[-1]],
                    "anchor_text": "FUTURE_SENTINEL",
                    "evidence_quote": "FUTURE_SENTINEL",
                },
            ],
            "setting": {
                "place": "an unnamed room",
                "time_frame_hint": "frame_present",
                "scene_shape": "many_scenes_or_travel",
            },
            "scenes_party_size": [
                {
                    "block_range": [block_id, block_id],
                    "co_present_count": 0,
                    "participants": [],
                }
                for block_id in block_ids
            ],
            "neutral_premise": "Several brief moments occur.",
        },
        ("b3", chapter_id, None): {
            "chapter_id": chapter_id,
            "chapter_rolling_summary": "Several brief moments occur.",
            "narration_frame_segments": [
                {
                    "local_segment_key": "present",
                    "parent_local_key": None,
                    "narrator_surface": "Narrator",
                    "narrator_ref": None,
                    "frame_kind": "primary_narration",
                    "story_time_label": "frame_present",
                    "block_range": [block_ids[0], block_ids[-1]],
                    "start_boundary": None,
                    "end_boundary": None,
                    "status": "proposed",
                    "evidence_quote": "Alice greeted Bob.",
                }
            ],
            "relation_observations": [],
            "character_state_changes": [],
            "unresolved_threads": [],
            "translator_relevant_facts": [],
            "motifs": [],
        },
    }
    for ordinal, block_id in enumerate(block_ids, start=1):
        scripts[("b1", chapter_id, f"w_{chapter_id}_{ordinal:02d}")] = {
            "chapter_id": chapter_id,
            "window_block_ids": [block_id],
            "context_only_used": False,
            "character_mentions": [],
            "glossary_candidates": [],
        }
        scripts[("b2", chapter_id, f"w_{chapter_id}_{ordinal:02d}")] = {
            "chapter_id": chapter_id,
            "window_block_ids": [block_id],
            "context_only_used": False,
            "speaker_turns": [],
            "relation_events": [],
        }
    return scripts


def _run_all(root: Path) -> tuple[dict[str, object], dict[str, object], SyntheticStageExecutor]:
    executor = SyntheticStageExecutor(_scripts())
    chapters = ["bk_ch01", "bk_ch02", "bk_ch03"]
    m1 = run_m1_v3(_document(), chapters, executor=executor, out_dir=root)
    assert m1["status"] == "complete", m1
    m2 = run_m2_v3(
        _document(), chapters, executor=executor, out_dir=root, m1v3_dir=root
    )
    assert m2["status"] == "complete", m2
    return m1, m2, executor


def test_three_chapter_states_round_trip_and_reference_indexes_are_complete(tmp_path: Path) -> None:
    m1, m2, _executor = _run_all(tmp_path)
    assert m1["ran_chapters"] == ["bk_ch01", "bk_ch02", "bk_ch03"]
    assert m2["ran_chapters"] == ["bk_ch01", "bk_ch02", "bk_ch03"]

    m1_checkpoint = read_current_checkpoint(
        out_dir=tmp_path, stage="m1v3", chapter_id="bk_ch03"
    )
    m2_checkpoint = read_current_checkpoint(
        out_dir=tmp_path, stage="m2v3", chapter_id="bk_ch03"
    )
    assert m1_checkpoint and m2_checkpoint
    m1_state = read_state_from_checkpoint(m1_checkpoint, out_dir=tmp_path)
    m2_state = read_state_from_checkpoint(m2_checkpoint, out_dir=tmp_path)
    assert json.loads(json.dumps(m1_state)) == m1_state
    assert json.loads(json.dumps(m2_state)) == m2_state
    assert canonical_hash(_m1_semantic_projection(m1_state)) == m1_state["semantic_state_hash"]
    assert canonical_hash(_m2_semantic_projection(m2_state)) == m2_state["semantic_state_hash"]

    injected = deepcopy(m1_state)
    injected["b0_payload"] = {"cast_claims": []}
    injected["semantic_state_hash"] = canonical_hash(_m1_semantic_projection(injected))
    with pytest.raises(CheckpointError, match="retired b0_payload"):
        pipeline_v3._validate_restored_state(injected, stage="m1v3")

    kinds = [row["kind"] for row in m1_state["reference_index"]]
    assert kinds.count("cast_claim") == 0
    assert "b0_payload" not in m1_state
    assert kinds.count("mention") == 2
    assert kinds.count("turn") == 1
    assert kinds.count("event") == 1
    assert kinds.count("endpoint") == 4
    assert len({row["id"] for row in m1_state["reference_index"]}) == len(
        m1_state["reference_index"]
    )
    assert [row["kind"] for row in m2_state["digest_reference_index"]] == [
        "frame_segment"
    ]
    assert m2_state["digest_payload"]["motifs"] == [
        {
            "note": "Greetings recur.",
            "block_ids": ["bk_ch03_b001"],
            "subject_refs": ["e_bk_ch03_b001_01#actor"],
        }
    ]
    assert m2_state["digest_payload"]["narration_frame_segments"][0][
        "narrator_ref"
    ] == "m_bk_ch03_b001_01"
    assert [row["chapter_id"] for row in m2_state["prior_summary_provenance"]] == [
        "bk_ch01",
        "bk_ch02",
    ]


def test_three_chapter_straight_and_resume_have_identical_semantics_and_identities(
    tmp_path: Path,
) -> None:
    straight = tmp_path / "straight"
    resumed = tmp_path / "resumed"
    straight_m1, straight_m2, _ = _run_all(straight)

    first_executor = SyntheticStageExecutor(_scripts())
    first_m1 = run_m1_v3(_document(), ["bk_ch01"], executor=first_executor, out_dir=resumed)
    assert first_m1["status"] == "complete"
    resumed_m1 = run_m1_v3(
        _document(), ["bk_ch01", "bk_ch02", "bk_ch03"], executor=SyntheticStageExecutor(_scripts()), out_dir=resumed, resume=True
    )
    assert resumed_m1["restored_chapters"] == ["bk_ch01"]
    first_m2 = run_m2_v3(
        _document(), ["bk_ch01"], executor=SyntheticStageExecutor(_scripts()), out_dir=resumed, m1v3_dir=resumed
    )
    assert first_m2["status"] == "complete"
    resumed_m2 = run_m2_v3(
        _document(),
        ["bk_ch01", "bk_ch02", "bk_ch03"],
        executor=SyntheticStageExecutor(_scripts()),
        out_dir=resumed,
        m1v3_dir=resumed,
        resume=True,
    )
    assert resumed_m2["restored_chapters"] == ["bk_ch01"]
    assert resumed_m1["semantic_state_hashes"] == straight_m1["semantic_state_hashes"]
    assert resumed_m1["checkpoint_identity_hashes"] == straight_m1[
        "checkpoint_identity_hashes"
    ]
    assert resumed_m2["semantic_state_hashes"] == straight_m2["semantic_state_hashes"]
    assert resumed_m2["checkpoint_identity_hashes"] == straight_m2[
        "checkpoint_identity_hashes"
    ]


def test_b0less_checkpoint_versions_reject_old_m1_and_m2(tmp_path: Path) -> None:
    _run_all(tmp_path)
    for stage, expected_version, old_version in (
        ("m1v3", M1_CHECKPOINT_SCHEMA_VERSION_V3, "literary_m1_checkpoint_v3"),
        ("m2v3", M2_CHECKPOINT_SCHEMA_VERSION_V3, "literary_m2_checkpoint_v3"),
    ):
        checkpoint = read_current_checkpoint(
            out_dir=tmp_path, stage=stage, chapter_id="bk_ch03"
        )
        assert checkpoint and checkpoint["schema_version"] == expected_version
        stale = deepcopy(checkpoint)
        stale["schema_version"] = old_version
        stale["checkpoint_identity"]["schema_version"] = old_version
        stale["checkpoint_identity_hash"] = canonical_hash(stale["checkpoint_identity"])
        errors = validate_v3_checkpoint(stale, root=tmp_path)
        assert "schema_version" in errors


def test_request_contract_and_fingerprint_are_stage_scoped() -> None:
    original = dict(REQUEST_CONTRACT_HASHES)
    changed = deepcopy(REQUEST_SHAPE_CONTRACT)
    changed["b2"]["ordering"]["window_mentions"] = "reverse"
    changed_hashes = {stage: canonical_hash(value) for stage, value in changed.items()}
    assert changed_hashes["b2"] != original["b2"]
    assert {stage: changed_hashes[stage] for stage in ("b1", "b3")} == {
        stage: original[stage] for stage in ("b1", "b3")
    }
    blocks = [{"block_id": "b1", "order_index": 1, "block_type": "paragraph", "text": "x"}]
    first = _build_request(
        stage="b1",
        chapter_id="c1",
        window_id="w_c1_01",
        allowlisted_sections={"active_window_blocks": blocks, "context_only_tail": []},
        lineage_manifest=[],
    )
    second = _build_request(
        stage="b1",
        chapter_id="c1",
        window_id="w_c1_01",
        allowlisted_sections={
            "active_window_blocks": [{**blocks[0], "text": "y"}],
            "context_only_tail": [],
        },
        lineage_manifest=[],
    )
    assert first.request_fingerprint != second.request_fingerprint
    with pytest.raises(ValueError, match="sections mismatch"):
        _build_request(
            stage="b1",
            chapter_id="c1",
            window_id="w_c1_01",
            allowlisted_sections={
                "active_window_blocks": blocks,
                "context_only_tail": [],
                "registry": [],
            },
            lineage_manifest=[],
        )
    with pytest.raises(ValueError, match="unsupported Builder-v3 request stage"):
        _build_request(
            stage="b0",
            chapter_id="c1",
            window_id=None,
            allowlisted_sections={"chapter_blocks": blocks},
            lineage_manifest=[],
        )


class _MutatingExecutor(SyntheticStageExecutor):
    def execute(self, request: V3StageRequest, *, attempt_no: int, bypass_cache: bool = False) -> StageAttemptResult:
        local = request.body()
        local["allowlisted_sections"] = {"mutated": True}
        return super().execute(request, attempt_no=attempt_no, bypass_cache=bypass_cache)


def test_executor_receives_immutable_bytes_and_audit_survives_rejection(tmp_path: Path) -> None:
    scripts = _scripts()
    scripts[("b1", "bk_ch01", "w_bk_ch01_01")].pop("chapter_id")
    executor = _MutatingExecutor(scripts)
    report = run_m1_v3(_document(), ["bk_ch01"], executor=executor, out_dir=tmp_path)
    assert report["status"] == "halted"
    assert not current_pointer_path(tmp_path, "m1v3", "bk_ch01").exists()
    request_files = list((builder_v3_root(tmp_path) / "audit").rglob("request.json"))
    raw_files = list((builder_v3_root(tmp_path) / "audit").rglob("raw_result.json"))
    validation_files = list((builder_v3_root(tmp_path) / "audit").rglob("validation.json"))
    assert len(request_files) == len(raw_files) == len(validation_files) == 1
    seen = executor.call_log[-1]["canonical_request_json"].encode("utf-8")
    assert request_files[-1].read_bytes() == seen
    assert any(
        json.loads(path.read_text(encoding="utf-8"))["ok"] is False
        for path in validation_files
    )


def test_foreign_reference_halts_without_publishing_m2(tmp_path: Path) -> None:
    executor = SyntheticStageExecutor(_scripts())
    m1 = run_m1_v3(_document(), ["bk_ch01"], executor=executor, out_dir=tmp_path)
    assert m1["status"] == "complete"
    scripts = _scripts()
    scripts[("b3", "bk_ch01", None)]["relation_observations"][0]["event_id"] = "e_foreign"
    failing = SyntheticStageExecutor(scripts)
    report = run_m2_v3(
        _document(), ["bk_ch01"], executor=failing, out_dir=tmp_path, m1v3_dir=tmp_path
    )
    assert report["status"] == "halted"
    assert not current_pointer_path(tmp_path, "m2v3", "bk_ch01").exists()
    assert len(failing.call_log) == 1


def test_missing_k2_context_halts_before_executor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    m1_executor = SyntheticStageExecutor(_scripts())
    assert run_m1_v3(
        _document(), ["bk_ch01", "bk_ch02", "bk_ch03"], executor=m1_executor, out_dir=source
    )["status"] == "complete"
    target = tmp_path / "target"
    executor = SyntheticStageExecutor(_scripts())
    report = run_m2_v3(
        _document(),
        ["bk_ch03"],
        executor=executor,
        out_dir=target,
        m1v3_dir=source,
        digest_context=target,
    )
    assert report["status"] == "halted"
    assert executor.call_log == []
    assert not current_pointer_path(target, "m2v3", "bk_ch03").exists()


def test_stage_scoped_sentinel_matrix_and_untrusted_projection(tmp_path: Path) -> None:
    document = _document(extra_blocks=3)
    scripts = _sentinel_scripts()
    executor = SyntheticStageExecutor(scripts)
    m1 = run_m1_v3(
        document,
        ["bk_ch01"],
        executor=executor,
        out_dir=tmp_path,
        window_max_blocks=1,
    )
    assert m1["status"] == "complete", m1
    assert not any(row["key"][0] == "b0" for row in executor.call_log)
    b1_first = next(
        row for row in executor.call_log if row["key"] == ["b1", "bk_ch01", "w_bk_ch01_01"]
    )
    b2_first = next(
        row for row in executor.call_log if row["key"] == ["b2", "bk_ch01", "w_bk_ch01_01"]
    )
    assert "FUTURE_SENTINEL" not in b1_first["canonical_request_json"]
    assert "FUTURE_SENTINEL" not in json.dumps(
        json.loads(b2_first["canonical_request_json"])["allowlisted_sections"],
        ensure_ascii=False,
    )
    b2_sections = json.loads(b2_first["canonical_request_json"])["allowlisted_sections"]
    assert set(b2_sections) == {
        "active_window_blocks",
        "context_only_tail",
        "window_mentions",
    }
    assert "b0_scene_projection" not in b2_sections
    assert "b0_typed_projection" not in b2_sections
    assert "ROLE_FAR_SENTINEL" not in json.dumps(b2_sections, ensure_ascii=False)
    assert "ROLE_NEAR_SENTINEL" not in b1_first["canonical_request_json"]

    m2 = run_m2_v3(
        document,
        ["bk_ch01"],
        executor=executor,
        out_dir=tmp_path,
        m1v3_dir=tmp_path,
    )
    assert m2["status"] == "complete", m2
    b3 = next(row for row in executor.call_log if row["key"] == ["b3", "bk_ch01", None])
    b3_body = json.loads(b3["canonical_request_json"])
    assert "FUTURE_SENTINEL" in json.dumps(
        b3_body["allowlisted_sections"]["chapter_blocks"], ensure_ascii=False
    )
    assert "ROLE_NEAR_SENTINEL" not in json.dumps(
        b3_body["allowlisted_sections"], ensure_ascii=False
    )
    assert "ROLE_FAR_SENTINEL" not in json.dumps(
        b3_body["allowlisted_sections"], ensure_ascii=False
    )


def test_foreign_b1_reference_is_cleared_before_m1_publish(tmp_path: Path) -> None:
    scripts = _scripts()
    scripts[("b2", "bk_ch01", "w_bk_ch01_01")]["speaker_turns"][0]["speaker"][
        "mention_ref"
    ] = "m_foreign"
    executor = SyntheticStageExecutor(scripts)
    report = run_m1_v3(
        _document(), ["bk_ch01"], executor=executor, out_dir=tmp_path
    )
    assert report["status"] == "complete", report
    checkpoint = read_current_checkpoint(
        out_dir=tmp_path,
        stage="m1v3",
        chapter_id="bk_ch01",
    )
    assert checkpoint is not None
    state = read_state_from_checkpoint(checkpoint, out_dir=tmp_path)
    assert "m_foreign" not in json.dumps(state, ensure_ascii=False)
    turn = state["b2_by_window"][0]["payload"]["speaker_turns"][0]
    assert turn["speaker"]["mention_ref"] is None
    assert report["validation_counters"]["mention_ref_cleared_foreign"] == 1
    assert len(list((builder_v3_root(tmp_path) / "audit").rglob("raw_result.json"))) == 2


def test_source_metadata_change_invalidates_resume_before_execute(tmp_path: Path) -> None:
    assert run_m1_v3(
        _document(),
        ["bk_ch01"],
        executor=SyntheticStageExecutor(_scripts()),
        out_dir=tmp_path,
    )["status"] == "complete"
    changed = _document()
    changed["chapters"][0]["blocks"][0]["order_index"] = 999
    executor = SyntheticStageExecutor(_scripts())
    report = run_m1_v3(
        changed,
        ["bk_ch01"],
        executor=executor,
        out_dir=tmp_path,
        resume=True,
    )
    assert report["status"] == "halted"
    assert "source_hash" in report["stopping_error"]["message"]
    assert executor.call_log == []


class _WrongModeExecutor(SyntheticStageExecutor):
    def execute(self, request: V3StageRequest, *, attempt_no: int, bypass_cache: bool = False) -> StageAttemptResult:
        result = super().execute(request, attempt_no=attempt_no, bypass_cache=bypass_cache)
        return StageAttemptResult(
            raw_payload=result.raw_payload,
            raw_text=result.raw_text,
            usage=result.usage,
            from_cache=False,
            execution_mode="llm",
            transport_meta=result.transport_meta,
            error=None,
        )


def test_synthetic_transport_contract_is_fail_closed(tmp_path: Path) -> None:
    executor = _WrongModeExecutor(_scripts())
    report = run_m1_v3(
        _document(), ["bk_ch01"], executor=executor, out_dir=tmp_path
    )
    assert report["status"] == "halted"
    assert "execution_mode" in report["stopping_error"]["message"]
    assert len(list((builder_v3_root(tmp_path) / "audit").rglob("raw_result.json"))) == 1
    assert not current_pointer_path(tmp_path, "m1v3", "bk_ch01").exists()


def test_wrong_k_context_is_rejected_before_b3_execute(tmp_path: Path) -> None:
    context = tmp_path / "context"
    assert run_m1_v3(
        _document(),
        ["bk_ch01", "bk_ch02", "bk_ch03"],
        executor=SyntheticStageExecutor(_scripts()),
        out_dir=context,
    )["status"] == "complete"
    assert run_m2_v3(
        _document(),
        ["bk_ch01", "bk_ch02"],
        executor=SyntheticStageExecutor(_scripts()),
        out_dir=context,
        m1v3_dir=context,
        summary_k=1,
    )["status"] == "complete"
    executor = SyntheticStageExecutor(_scripts())
    report = run_m2_v3(
        _document(),
        ["bk_ch03"],
        executor=executor,
        out_dir=tmp_path / "target",
        m1v3_dir=context,
        digest_context=context,
        summary_k=2,
    )
    assert report["status"] == "halted"
    assert "summary_k" in report["stopping_error"]["message"]
    assert executor.call_log == []


def test_stale_summary_context_is_rejected_before_b3_execute(tmp_path: Path) -> None:
    context = tmp_path / "context"
    _run_all(context)
    checkpoint = read_current_checkpoint(
        out_dir=context, stage="m2v3", chapter_id="bk_ch02"
    )
    assert checkpoint
    state_path = builder_v3_root(context) / checkpoint["state_path"]
    state_path.write_text(state_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    executor = SyntheticStageExecutor(_scripts())
    report = run_m2_v3(
        _document(),
        ["bk_ch03"],
        executor=executor,
        out_dir=tmp_path / "target",
        m1v3_dir=context,
        digest_context=context,
    )
    assert report["status"] == "halted"
    assert "artifact_" in report["stopping_error"]["message"]
    assert executor.call_log == []


def test_out_of_order_summary_view_is_rejected_before_b3_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "context"
    _run_all(context)
    original = pipeline_v3._summary_entry

    def swapped(*, checkpoint: dict[str, object], state: dict[str, object]) -> dict[str, object]:
        entry = original(checkpoint=checkpoint, state=state)
        chapter_id = entry["view"]["chapter_id"]
        replacement = "bk_ch02" if chapter_id == "bk_ch01" else "bk_ch01"
        entry["view"]["chapter_id"] = replacement
        entry["provenance"]["chapter_id"] = replacement
        return entry

    monkeypatch.setattr(pipeline_v3, "_summary_entry", swapped)
    executor = SyntheticStageExecutor(_scripts())
    report = run_m2_v3(
        _document(),
        ["bk_ch03"],
        executor=executor,
        out_dir=tmp_path / "target",
        m1v3_dir=context,
        digest_context=context,
    )
    assert report["status"] == "halted"
    assert "out of absolute chapter order" in report["stopping_error"]["message"]
    assert executor.call_log == []


def test_empty_summary_is_rejected_before_use() -> None:
    with pytest.raises(CheckpointError, match="empty M2V3 rolling summary"):
        pipeline_v3._summary_entry(
            checkpoint={
                "checkpoint_identity_hash": "identity",
                "checkpoint_hash": "operational",
                "input_max_order": 3,
            },
            state={
                "chapter_id": "bk_ch01",
                "digest_payload": {"chapter_rolling_summary": ""},
            },
        )


def test_legacy_window_projection_matches_pre_v3_golden() -> None:
    windows = build_literary_windows(
        _document()["chapters"][0], target_tokens=500, max_blocks=8
    )
    projection = [
        {
            "window_id": window.window_id,
            "blocks": window.block_ids,
            "previous": [row["block_id"] for row in window.previous_tail],
            "next": [row["block_id"] for row in window.next_tail],
            "est_src_tokens": window.est_src_tokens,
        }
        for window in windows
    ]
    assert canonical_hash(projection) == (
        "000b075e0eff597ffadf3b0fab3d4a9ba6e2b7e654ac8491e5a91d40be80d918"
    )


def test_atomic_generation_crash_keeps_prior_pointer(tmp_path: Path) -> None:
    root = builder_v3_root(tmp_path)
    audit_file = root / "audit" / "fixture" / "request.json"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text("{}", encoding="utf-8")
    state = {"schema_version": M1_GROUND_STATE_VERSION_V3, "chapter_id": "c1"}
    semantic = deepcopy(state)
    identity = {
        "absolute_chapter_index": 0,
        "chapter_sequence_prefix": ["c1"],
        "source_hash": "source",
        "knowledge_mode": "whole_book_frozen",
        "execution_mode": "synthetic",
        "contract_versions": {},
        "request_contract_hashes": {},
        "request_manifest_hash": "manifest-1",
        "window_target_tokens": 500,
        "window_max_blocks": 8,
        "tail_k": 2,
        "summary_k": 0,
        "parent_checkpoint_identity_hash": None,
    }
    first = publish_generation(
        out_dir=tmp_path,
        stage="m1v3",
        chapter_id="c1",
        state=state,
        semantic_projection=semantic,
        identity_base=identity,
        operational_fields={"parent_checkpoint_hash": None},
        audit_artifacts=[{"role": "fixture/request", "path": audit_file}],
    )

    def crash(_path: Path, _pointer: dict[str, object]) -> None:
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        publish_generation(
            out_dir=tmp_path,
            stage="m1v3",
            chapter_id="c1",
            state=state,
            semantic_projection=semantic,
            identity_base={**identity, "request_manifest_hash": "manifest-2"},
            operational_fields={"parent_checkpoint_hash": None},
            audit_artifacts=[{"role": "fixture/request", "path": audit_file}],
            before_pointer_switch=crash,
        )
    current = read_current_checkpoint(out_dir=tmp_path, stage="m1v3", chapter_id="c1")
    assert current and current["checkpoint_hash"] == first["checkpoint_hash"]
    assert len(list((root / "generations" / "m1v3" / "c1").iterdir())) == 2


def test_checkpoint_rejects_execution_mode_and_tampered_state(tmp_path: Path) -> None:
    _run_all(tmp_path)
    with pytest.raises(CheckpointError, match="execution_mode"):
        read_current_checkpoint(
            out_dir=tmp_path,
            stage="m1v3",
            chapter_id="bk_ch01",
            expected={"execution_mode": "llm"},
        )
    checkpoint = read_current_checkpoint(
        out_dir=tmp_path, stage="m1v3", chapter_id="bk_ch01"
    )
    assert checkpoint
    state_path = builder_v3_root(tmp_path) / checkpoint["state_path"]
    state_path.write_text(state_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    errors = validate_v3_checkpoint(checkpoint, root=tmp_path)
    assert any(value.startswith("artifact_") for value in errors)


def test_p0_contract_version_is_bumped() -> None:
    assert VALIDATOR_CONTRACT_VERSION == "literary_builder_v3_validator_contract_v13"
    assert contract_versions()["validator"] == VALIDATOR_CONTRACT_VERSION
    assert M2_CHECKPOINT_SCHEMA_VERSION_V3 == "literary_m2_checkpoint_v3_b0less_v2"


def test_p0_old_validator_contract_checkpoint_is_rejected(tmp_path: Path) -> None:
    root = builder_v3_root(tmp_path)
    audit_file = root / "audit" / "fixture" / "request.json"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text("{}", encoding="utf-8")
    state = {"schema_version": M1_GROUND_STATE_VERSION_V3, "chapter_id": "c1"}
    old_versions = {
        **contract_versions(),
        "validator": "literary_builder_v3_validator_contract_v1",
    }
    publish_generation(
        out_dir=tmp_path,
        stage="m1v3",
        chapter_id="c1",
        state=state,
        semantic_projection=deepcopy(state),
        identity_base={
            "absolute_chapter_index": 0,
            "chapter_sequence_prefix": ["c1"],
            "source_hash": "source",
            "knowledge_mode": "whole_book_frozen",
            "execution_mode": "synthetic",
            "contract_versions": old_versions,
            "request_contract_hashes": {},
            "request_manifest_hash": "manifest-old",
            "window_target_tokens": 500,
            "window_max_blocks": 8,
            "tail_k": 2,
            "summary_k": 0,
            "parent_checkpoint_identity_hash": None,
        },
        operational_fields={"parent_checkpoint_hash": None},
        audit_artifacts=[{"role": "fixture/request", "path": audit_file}],
    )
    with pytest.raises(CheckpointError, match="contract_versions"):
        read_current_checkpoint(
            out_dir=tmp_path,
            stage="m1v3",
            chapter_id="c1",
            expected={"contract_versions": contract_versions()},
        )

def test_checkpoint_rejects_each_deterministic_identity_dimension(tmp_path: Path) -> None:
    _run_all(tmp_path)
    m1_cases = {
        "contract_versions": {**contract_versions(), "validator": "literary_builder_v3_validator_contract_v1"},
        "request_manifest_hash": "stale",
        "semantic_state_hash": "stale",
        "window_target_tokens": 999,
        "parent_checkpoint_identity_hash": "stale",
    }
    for field, value in m1_cases.items():
        with pytest.raises(CheckpointError, match=field):
            read_current_checkpoint(
                out_dir=tmp_path,
                stage="m1v3",
                chapter_id="bk_ch03",
                expected={field: value},
            )
    m2_cases = {
        "summary_k": 99,
        "input_m1v3_identity_hash": "stale",
        "execution_mode": "llm",
    }
    for field, value in m2_cases.items():
        with pytest.raises(CheckpointError, match=field):
            read_current_checkpoint(
                out_dir=tmp_path,
                stage="m2v3",
                chapter_id="bk_ch03",
                expected={field: value},
            )


def test_v3_source_never_names_or_calls_legacy_identity_helpers() -> None:
    source = (
        Path(__file__).parents[1] / "literary" / "builder_v3_pipeline.py"
    ).read_text(encoding="utf-8")
    forbidden = {
        "seed_entity_ledger_from_chapter_brief",
        "update_entity_ledger_from_lexicon",
        "render_chapter_brief_for_injection",
        "REGISTRY_CONTEXT_PACK",
    }
    assert all(value not in source for value in forbidden)
