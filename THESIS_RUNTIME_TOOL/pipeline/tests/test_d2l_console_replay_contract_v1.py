from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    D2LConsoleContractError,
    D2LTranslationComponentEventWriter,
    STAGE_IDS,
    build_checkpoint,
    build_component_manifest,
    build_component_usage_snapshot,
    canonical_sha256,
    file_sha256,
    scoring_fragment_sha256,
    validate_component_event_stream,
    validate_component_manifest,
    validate_component_usage_snapshot_sequence,
    validate_artifact_index,
    validate_scoring_handoff_fragment,
    validate_translation_component_package,
    write_component_manifest_snapshot,
    write_json,
)
from pipeline.scripts.build_d2l_console_replay_fixtures_v1 import (
    CHAPTER_IDS,
    COMPONENT_RUN_ID,
    CONFIG_SHA,
    GIT_COMMIT,
    WORKFLOW_RUN_ID,
    _source_binding,
    build_fixture,
)


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "translation_component"
    build_fixture(root)
    return root


def _committed_fixture() -> Path:
    return Path(__file__).parent / "fixtures" / "d2l_console_replay_v1" / "translation_component"


def _manifest(
    *,
    status: str = "planned",
    attempt: int = 1,
    resume: dict | None = None,
    stages: list[dict] | None = None,
) -> dict:
    return build_component_manifest(
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=attempt,
        pipeline_id="d2l_terminology",
        pipeline_version="translation_component_v1",
        source_binding=_source_binding(),
        config_sha256=CONFIG_SHA,
        code_revision=GIT_COMMIT,
        selected_chapter_ids=CHAPTER_IDS,
        started_at="2026-07-22T00:00:00Z",
        updated_at="2026-07-22T00:00:01Z",
        status=status,
        active_stage_id="preflight" if status in {"running", "paused"} else None,
        stages=stages,
        resume=resume,
    )


def _write_manifest(root: Path, manifest: dict) -> None:
    write_json(root / "component_manifest.json", manifest)


def test_recorded_translation_component_fixture_is_valid(tmp_path: Path) -> None:
    result = validate_translation_component_package(_fixture(tmp_path))
    assert result["terminal_event"] == "run_done"
    assert result["artifact_count"] == 4
    assert result["component_attempt_id"] == 1


def test_committed_fixture_is_valid_and_deterministic(tmp_path: Path) -> None:
    committed = _committed_fixture()
    assert validate_translation_component_package(committed)["terminal_event"] == "run_done"
    rebuilt = _fixture(tmp_path)
    committed_files = sorted(path.relative_to(committed) for path in committed.rglob("*") if path.is_file())
    rebuilt_files = sorted(path.relative_to(rebuilt) for path in rebuilt.rglob("*") if path.is_file())
    assert rebuilt_files == committed_files
    for relative in committed_files:
        rebuilt_bytes = (rebuilt / relative).read_bytes().replace(b"\r\n", b"\n")
        committed_bytes = (committed / relative).read_bytes().replace(b"\r\n", b"\n")
        assert rebuilt_bytes == committed_bytes


def _usage(
    logical_request_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_status: str,
) -> dict:
    return {
        "logical_request_id": logical_request_id,
        "physical_attempt_index": 1,
        "provider_id": "provider",
        "model_id": "model",
        "source_id": "source",
        "masked_quota_bucket": "bucket-***",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": prompt_tokens + completion_tokens,
        "latency_ms": 10,
        "finish_reason": "stop",
        "cost_usd": None,
        "currency": None,
        "cost_status": "unknown",
        "cache_status": cache_status,
        "cache_mechanism": (
            "local_exact_cache" if cache_status in {"hit", "miss"} else "none"
        ),
    }


def _accepted_provider(
    logical_request_id: str,
    *,
    attempt_usage_id: str,
) -> dict:
    return {
        "identity_kind": "provider_attempt",
        "attempt_usage_id": attempt_usage_id,
        "cache_observation_id": f"cache_{attempt_usage_id}",
        "logical_request_id": logical_request_id,
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "physical_attempt_index": 1,
        "provider_called": True,
        "source_revision": "source_v1",
        "usage": _usage(
            logical_request_id,
            prompt_tokens=10,
            completion_tokens=2,
            cache_status="miss",
        ),
    }


def test_usage_snapshots_preserve_attempt_cache_and_unknown_cost() -> None:
    first = build_component_usage_snapshot(
        previous_snapshots=[],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=2,
        stage_id="b1_candidate_discovery",
        work_id="window_1",
        accepted_usage=_accepted_provider(
            "request_1",
            attempt_usage_id="attempt_1",
        ),
    )
    cache_usage = {
        "identity_kind": "cache_observation",
        "attempt_usage_id": None,
        "cache_observation_id": "cache_hit_2",
        "logical_request_id": "request_2",
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "physical_attempt_index": None,
        "provider_called": False,
        "source_revision": "source_v1",
        "usage": _usage(
            "request_2",
            prompt_tokens=0,
            completion_tokens=0,
            cache_status="hit",
        ),
    }
    second = build_component_usage_snapshot(
        previous_snapshots=[first],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=2,
        stage_id="b1_candidate_discovery",
        work_id="window_2",
        accepted_usage=cache_usage,
    )
    final = build_component_usage_snapshot(
        previous_snapshots=[first, second],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=2,
        stage_id=None,
        work_id=None,
        accepted_usage=None,
        component_final=True,
    )
    latest = validate_component_usage_snapshot_sequence([first, second, final])

    assert latest == final
    assert final["component_cumulative"] == {
        "logical_request_count": 2,
        "accepted_result_count": 2,
        "physical_attempt_count": 1,
        "cache_observation_count": 2,
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 12,
        "cost_usd": None,
        "currency": None,
        "cost_status": "unknown",
        "cache_counters": {"hit": 1, "miss": 1},
    }


def test_usage_snapshot_rejects_duplicate_provider_attempt() -> None:
    first = build_component_usage_snapshot(
        previous_snapshots=[],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=1,
        stage_id="b1_candidate_discovery",
        work_id="window_1",
        accepted_usage=_accepted_provider(
            "request_1",
            attempt_usage_id="attempt_1",
        ),
    )
    with pytest.raises(D2LConsoleContractError, match="duplicate attempt_usage_id"):
        build_component_usage_snapshot(
            previous_snapshots=[first],
            workflow_run_id=WORKFLOW_RUN_ID,
            component_run_id=COMPONENT_RUN_ID,
            component_attempt_id=2,
            stage_id="b1_candidate_discovery",
            work_id="window_2",
            accepted_usage=_accepted_provider(
                "request_2",
                attempt_usage_id="attempt_1",
            ),
        )


def test_component_package_rejects_parent_owned_files(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    (root / "workflow_manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(D2LConsoleContractError, match="workflow_manifest"):
        validate_translation_component_package(root)


def test_manifest_uses_component_identity_not_parent_seq(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    manifest["component_attempt_id"] = 2
    validate_component_manifest(manifest)
    write_component_manifest_snapshot(root, manifest)
    with pytest.raises(D2LConsoleContractError, match="terminal stream attempt"):
        validate_translation_component_package(root)
    event = json.loads((root / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    event["seq"] = 1
    with pytest.raises(D2LConsoleContractError, match="unknown|keys"):
        from pipeline.prepass.d2l_console_replay_contract_v1 import validate_component_event

        validate_component_event(event, manifest=manifest, expected_component_seq=1)


def test_component_seq_gap_is_rejected(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    rows = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    rows[2]["component_seq"] = 9
    (root / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    with pytest.raises(D2LConsoleContractError, match="component_seq gap"):
        validate_component_event_stream(root / "events.jsonl", manifest=manifest)


def test_resume_keeps_component_run_and_increments_component_attempt(tmp_path: Path) -> None:
    root = tmp_path / "resume_component"
    root.mkdir()
    first_manifest = _manifest(status="running")
    _write_manifest(root, first_manifest)
    writer = D2LTranslationComponentEventWriter(
        root / "events.jsonl",
        manifest=first_manifest,
        component_attempt_id=1,
    )
    writer.emit(
        "run_start",
        stage_id=None,
        agent="runner",
        payload={
            "manifest_ref": "component_manifest.json",
            "manifest_sha256": file_sha256(root / "component_manifest.json"),
            "selected_chapter_ids": CHAPTER_IDS,
        },
        ts="2026-07-22T00:00:00Z",
    )
    checkpoint = build_checkpoint(
        manifest=first_manifest,
        checkpoint_ref="checkpoints/ckpt_1.json",
        stage_id="preflight",
        work_id="work_1",
        resume_available=True,
        paused_reason="bounded_pause",
        created_at="2026-07-22T00:00:02Z",
        state={"last_work_id": "work_1"},
    )
    (root / "checkpoints").mkdir()
    write_json(root / "checkpoints/ckpt_1.json", checkpoint)
    writer.emit(
        "checkpoint",
        stage_id="preflight",
        agent="runner",
        payload={
            "checkpoint_ref": "checkpoints/ckpt_1.json",
            "checkpoint_sha256": file_sha256(root / "checkpoints/ckpt_1.json"),
            "stage_id": "preflight",
            "work_id": "work_1",
            "resume_available": True,
            "paused_reason": "bounded_pause",
        },
        ts="2026-07-22T00:00:02Z",
    )
    paused_stages = copy.deepcopy(first_manifest["stages"])
    paused_stages[0]["status"] = "paused"
    paused_stages[0]["started_at"] = "2026-07-22T00:00:00Z"
    paused_stages[0]["current_work_id"] = "work_1"
    paused = _manifest(
        status="paused",
        attempt=2,
        stages=paused_stages,
        resume={
            "resume_available": True,
            "checkpoint_ref": "checkpoints/ckpt_1.json",
            "checkpoint_sha256": file_sha256(root / "checkpoints/ckpt_1.json"),
            "stage_id": "preflight",
            "work_id": "work_1",
            "paused_reason": "bounded_pause",
        },
    )
    _write_manifest(root, paused)
    resumed_writer = D2LTranslationComponentEventWriter(
        root / "events.jsonl",
        manifest=paused,
        component_attempt_id=2,
    )
    resumed_writer.emit(
        "run_resumed",
        stage_id=None,
        agent="runner",
        payload={
            "previous_component_attempt_id": 1,
            "checkpoint_ref": "checkpoints/ckpt_1.json",
            "checkpoint_sha256": file_sha256(root / "checkpoints/ckpt_1.json"),
            "reason_code": "user_resume",
        },
        ts="2026-07-22T00:01:00Z",
    )
    assert resumed_writer.component_seq == 3
    summary = validate_component_event_stream(root / "events.jsonl", manifest=paused, require_terminal=False)
    assert summary["component_run_id"] == COMPONENT_RUN_ID
    assert summary["component_attempt_id"] == 2

    rows = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    resumed = next(row for row in rows if row["event"] == "run_resumed")
    resumed["payload"]["checkpoint_sha256"] = "b" * 64
    (root / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(D2LConsoleContractError, match="checkpoint does not belong"):
        validate_component_event_stream(root / "events.jsonl", manifest=paused, require_terminal=False)


def test_stale_nonterminal_attempt_cannot_be_relayed_under_new_manifest(tmp_path: Path) -> None:
    root = tmp_path / "stale_component"
    root.mkdir()
    first_manifest = _manifest(status="running")
    first_binding = write_component_manifest_snapshot(root, first_manifest)
    writer = D2LTranslationComponentEventWriter(
        root / "events.jsonl",
        manifest=first_manifest,
        component_attempt_id=1,
    )
    writer.emit(
        "run_start",
        stage_id=None,
        agent="runner",
        payload={
            "manifest_ref": first_binding["manifest_ref"],
            "manifest_sha256": first_binding["manifest_sha256"],
            "selected_chapter_ids": CHAPTER_IDS,
        },
        ts="2026-07-22T00:00:00Z",
    )
    writer.emit(
        "stage_start",
        stage_id="preflight",
        agent="runner",
        payload={
            "progress": {"completed": 0, "total": 1, "unit": "checks"},
            "current_work_id": "work_preflight",
        },
        ts="2026-07-22T00:00:01Z",
    )

    paused_stages = copy.deepcopy(first_manifest["stages"])
    paused_stages[0]["status"] = "paused"
    paused_stages[0]["started_at"] = "2026-07-22T00:00:01Z"
    paused_stages[0]["current_work_id"] = "work_preflight"
    paused_manifest = _manifest(
        status="paused",
        attempt=2,
        stages=paused_stages,
        resume={
            "resume_available": True,
            "checkpoint_ref": "checkpoints/ckpt_1.json",
            "checkpoint_sha256": "a" * 64,
            "stage_id": "preflight",
            "work_id": "work_preflight",
            "paused_reason": "bounded_pause",
        },
    )
    write_component_manifest_snapshot(root, paused_manifest)

    with pytest.raises(D2LConsoleContractError, match="nonterminal stream attempt"):
        validate_translation_component_package(root, require_terminal=False)


def test_resume_cannot_reuse_same_attempt_id(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "running"
    manifest["active_stage_id"] = "translator"
    manifest["scoring_handoff_fragment_ref"] = None
    writer = D2LTranslationComponentEventWriter(root / "events.jsonl", manifest=manifest, component_attempt_id=1)
    with pytest.raises(D2LConsoleContractError, match="terminal"):
        writer.emit("run_start", stage_id=None, agent="runner", payload={})


def test_reopening_nonterminal_stream_requires_new_component_attempt(tmp_path: Path) -> None:
    root = tmp_path / "running_component"
    root.mkdir()
    manifest = _manifest(status="running")
    write_component_manifest_snapshot(root, manifest)
    writer = D2LTranslationComponentEventWriter(
        root / "events.jsonl",
        manifest=manifest,
        component_attempt_id=1,
    )
    initial = write_component_manifest_snapshot(root, manifest)
    writer.emit(
        "run_start",
        stage_id=None,
        agent="runner",
        payload={
            "manifest_ref": initial["manifest_ref"],
            "manifest_sha256": initial["manifest_sha256"],
            "selected_chapter_ids": CHAPTER_IDS,
        },
        ts="2026-07-22T00:00:00Z",
    )

    with pytest.raises(D2LConsoleContractError, match="new component attempt"):
        D2LTranslationComponentEventWriter(
            root / "events.jsonl",
            manifest=manifest,
            component_attempt_id=1,
        )


def test_scoring_fragment_is_exactly_s0_s1_and_has_no_final_input_set(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    fragment_path = root / "scoring_handoff_fragment.json"
    fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
    assert [row["arm_id"] for row in fragment["translation_inputs"]] == ["s0", "s1"]
    assert "input_set_sha256" not in fragment
    validate_scoring_handoff_fragment(fragment)
    fragment["translation_inputs"].append(copy.deepcopy(fragment["translation_inputs"][0]))
    fragment["fragment_sha256"] = "0" * 64
    with pytest.raises(D2LConsoleContractError):
        validate_scoring_handoff_fragment(fragment)


def test_scoring_fragment_rejects_foreign_arm(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    path = root / "scoring_handoff_fragment.json"
    fragment = json.loads(path.read_text(encoding="utf-8"))
    fragment["translation_inputs"][0]["arm_id"] = "community"
    fragment["fragment_sha256"] = scoring_fragment_sha256(fragment)
    with pytest.raises(D2LConsoleContractError, match="ordered s0 and s1"):
        validate_scoring_handoff_fragment(fragment)


def test_source_binding_optional_fields_are_not_silent(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    del manifest["source_binding"]["admitted_projection"]
    with pytest.raises(D2LConsoleContractError, match="missing keys"):
        validate_component_manifest(manifest)


def test_artifact_hash_drift_is_rejected(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    (root / "artifacts/translation_s0.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(D2LConsoleContractError, match="hash drift"):
        validate_translation_component_package(root)


def test_unknown_cost_cannot_be_reported_as_zero(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    rows = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    cost = next(row for row in rows if row["event"] == "cost_snapshot")
    cost["payload"]["cost_usd"] = 0.0
    (root / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    with pytest.raises(D2LConsoleContractError, match="unknown cost"):
        validate_component_event_stream(root / "events.jsonl", manifest=manifest)


def test_raw_prompt_key_is_rejected(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    rows = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    request = next(row for row in rows if row["event"] == "request_sent")
    request["payload"]["raw_prompt"] = "must not persist"
    (root / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    with pytest.raises(D2LConsoleContractError, match="unknown keys|forbidden key"):
        validate_component_event_stream(root / "events.jsonl", manifest=manifest)


def test_component_stage_schedule_is_closed_and_d2l_owned(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert tuple(stage["stage_id"] for stage in manifest["stages"]) == STAGE_IDS
    manifest["stages"].append(copy.deepcopy(manifest["stages"][-1]))
    with pytest.raises(D2LConsoleContractError):
        validate_component_manifest(manifest)


def test_component_package_has_no_global_sequence_or_parent_handoff(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert "seq" not in row
        assert "workflow_seq" not in row
    assert not (root / "workflow_manifest.json").exists()
    assert not (root / "scoring_handoff.json").exists()


def test_events_and_artifacts_bind_flow_and_deterministic_event_ids(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    for expected_seq, event in enumerate(events, start=1):
        assert event["flow_kind"] == "terminology_translation"
        assert event["event_id"] == f"evt_{COMPONENT_RUN_ID}_{expected_seq:08d}"
        assert event["component_seq"] == expected_seq

    artifact_index = json.loads((root / "artifact_index.json").read_text(encoding="utf-8"))
    assert artifact_index["flow_kind"] == "terminology_translation"
    assert all(row["flow_kind"] == "terminology_translation" for row in artifact_index["artifacts"])


def test_artifacts_from_prior_attempt_remain_valid_after_resume(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    manifest["component_attempt_id"] = 2
    artifact_index = json.loads((root / "artifact_index.json").read_text(encoding="utf-8"))
    artifact_index["component_attempt_id"] = 2

    validated = validate_artifact_index(
        artifact_index,
        manifest=manifest,
        artifact_root=root,
    )
    assert {row["component_attempt_id"] for row in validated["artifacts"]} == {1}


def test_scoring_fragment_can_reference_prior_producing_attempt(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    fragment = json.loads((root / "scoring_handoff_fragment.json").read_text(encoding="utf-8"))
    fragment["translation_component_attempt_id"] = 2
    fragment["fragment_sha256"] = scoring_fragment_sha256(fragment)

    validated = validate_scoring_handoff_fragment(fragment)
    assert validated["translation_component_attempt_id"] == 2
    assert {row["producer_component_attempt_id"] for row in validated["translation_inputs"]} == {1}


def test_run_start_manifest_revision_hash_drift_is_rejected(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    first_event = json.loads((root / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    revision_path = root / first_event["payload"]["manifest_ref"]
    revision_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(D2LConsoleContractError, match="run_start manifest revision hash drift"):
        validate_translation_component_package(root)


def test_run_start_immutable_config_drift_is_rejected(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    manifest["config_sha256"] = "e" * 64
    write_component_manifest_snapshot(root, manifest)

    with pytest.raises(D2LConsoleContractError, match="immutable manifest field drifted: config_sha256"):
        validate_translation_component_package(root)


def test_unavailable_translation_cannot_enter_scoring_fragment(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    index_path = root / "artifact_index.json"
    artifact_index = json.loads(index_path.read_text(encoding="utf-8"))
    s0 = next(row for row in artifact_index["artifacts"] if row["artifact_ref"] == "art_translation_s0")
    s0["availability"] = "unavailable"
    write_json(index_path, artifact_index)
    events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    events[-1]["payload"]["artifact_index_sha256"] = file_sha256(index_path)
    (root / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in events),
        encoding="utf-8",
    )

    with pytest.raises(D2LConsoleContractError, match="not available for handoff"):
        validate_translation_component_package(root)


def test_optional_scoring_binding_must_be_complete_or_null(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    fragment = json.loads((root / "scoring_handoff_fragment.json").read_text(encoding="utf-8"))
    del fragment["glossary_binding"]["sha256"]
    fragment["fragment_sha256"] = scoring_fragment_sha256(fragment)

    with pytest.raises(D2LConsoleContractError, match="missing keys"):
        validate_scoring_handoff_fragment(fragment)


def test_d2l_fragment_cannot_claim_final_input_set_hash(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    fragment = json.loads((root / "scoring_handoff_fragment.json").read_text(encoding="utf-8"))
    fragment["input_set_sha256"] = "f" * 64
    fragment["fragment_sha256"] = scoring_fragment_sha256(fragment)

    with pytest.raises(D2LConsoleContractError, match="unknown keys"):
        validate_scoring_handoff_fragment(fragment)
