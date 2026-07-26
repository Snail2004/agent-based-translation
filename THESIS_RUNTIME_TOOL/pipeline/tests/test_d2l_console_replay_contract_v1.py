from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    D2LConsoleContractError,
    D2LTranslationComponentEventWriter,
    STAGE_IDS,
    build_checkpoint,
    build_component_manifest,
    build_component_usage_snapshot,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    project_work_journal_term_batches,
    scoring_fragment_sha256,
    validate_component_event,
    validate_component_event_stream,
    validate_component_manifest,
    validate_component_usage_snapshot_sequence,
    validate_artifact_index,
    validate_scoring_handoff_fragment,
    validate_term_lifecycle_batch,
    validate_term_lifecycle_event_sequence,
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


def test_write_json_retries_transient_windows_replace_denial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "artifact_index.json"
    real_replace = os.replace
    attempts = 0

    def transient_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "Access is denied", str(target))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", transient_replace)
    monkeypatch.setattr(
        "pipeline.prepass.d2l_console_replay_contract_v1.time.sleep",
        lambda _seconds: None,
    )

    write_json(destination, {"schema": "fixture", "value": 1})

    assert attempts == 3
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "schema": "fixture",
        "value": 1,
    }
    assert not destination.with_name(destination.name + ".tmp").exists()


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
    cached_input_tokens: int | None = 0,
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
        "cached_input_tokens": cached_input_tokens,
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
    cached_input_tokens: int | None = 0,
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
            cached_input_tokens=cached_input_tokens,
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


def test_usage_snapshot_allows_attempt_gap_without_accepted_usage() -> None:
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

    resumed = build_component_usage_snapshot(
        previous_snapshots=[first],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=3,
        stage_id="b1_candidate_discovery",
        work_id="window_2",
        accepted_usage=_accepted_provider(
            "request_2",
            attempt_usage_id="attempt_2",
        ),
    )

    assert resumed["component_attempt_id"] == 3
    assert resumed["snapshot_seq"] == 2
    assert validate_component_usage_snapshot_sequence([first, resumed]) == resumed


def test_usage_snapshot_preserves_unreported_provider_cache_as_unknown() -> None:
    first = build_component_usage_snapshot(
        previous_snapshots=[],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=1,
        stage_id="translator",
        work_id="window_1",
        accepted_usage=_accepted_provider(
            "request_unreported_cache",
            attempt_usage_id="attempt_unreported_cache",
            cached_input_tokens=None,
        ),
    )
    final = build_component_usage_snapshot(
        previous_snapshots=[first],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=1,
        stage_id=None,
        work_id=None,
        accepted_usage=None,
        component_final=True,
    )

    assert first["accepted_usage"]["usage"]["cached_input_tokens"] is None
    assert first["stage_cumulative"]["cached_input_tokens"] is None
    assert final["component_cumulative"]["cached_input_tokens"] is None
    assert validate_component_usage_snapshot_sequence([first, final]) == final


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


def _term_work_entry(
    observations: list[dict[str, object]],
    *,
    attempt: int = 2,
    journal_seq: int = 1,
    work_item_id: str = "b1_window_0001",
) -> dict[str, object]:
    return {
        "schema_version": "d2l_stage_work_journal_v1",
        "workflow_run_id": WORKFLOW_RUN_ID,
        "component_run_id": COMPONENT_RUN_ID,
        "component_attempt_id": attempt,
        "stage_id": "b1_candidate_discovery",
        "journal_seq": journal_seq,
        "previous_entry_sha256": None,
        "work_item_id": work_item_id,
        "work_contract_id": "candidate_contract_v1",
        "input_sha256": "A" * 64,
        "result": {"candidate_observations": observations},
        "result_sha256": "B" * 64,
        "entry_sha256": f"{journal_seq:064X}",
    }


def _term_validation_event(
    *,
    attempt: int = 2,
    component_seq: int = 8,
    work_item_id: str = "b1_window_0001",
) -> dict[str, object]:
    return {
        "event_id": f"evt_{COMPONENT_RUN_ID}_{component_seq:08d}",
        "component_attempt_id": attempt,
        "component_seq": component_seq,
        "event": "validation_passed",
        "stage_id": "b1_candidate_discovery",
        "payload": {"subject_ref": work_item_id},
    }


def _project_term_observations(
    observations: list[dict[str, object]],
    *,
    attempt: int = 2,
) -> list[dict[str, object]]:
    return project_work_journal_term_batches(
        stage_id="b1_candidate_discovery",
        journal_ref="runtime/work_items/b1_candidate_discovery.jsonl",
        entry=_term_work_entry(observations, attempt=attempt),
        validation_event=_term_validation_event(attempt=attempt),
        previous_rows=[],
        projection_mode="live",
        completed=1,
        total=179,
        unit="windows",
    )


def _project_term_stage(
    *,
    stage_id: str,
    work_item_id: str,
    result: dict[str, object],
    previous_rows: list[dict[str, object]],
    component_seq: int,
) -> list[dict[str, object]]:
    entry = _term_work_entry(
        [],
        attempt=2,
        work_item_id=work_item_id,
    )
    entry["stage_id"] = stage_id
    entry["result"] = result
    validation = _term_validation_event(
        attempt=2,
        component_seq=component_seq,
        work_item_id=work_item_id,
    )
    validation["stage_id"] = stage_id
    return project_work_journal_term_batches(
        stage_id=stage_id,
        journal_ref=f"runtime/work_items/{stage_id}.jsonl",
        entry=entry,
        validation_event=validation,
        previous_rows=previous_rows,
        projection_mode="live",
        completed=1,
        total=1,
        unit="packets",
    )


def test_term_lifecycle_live_batch_is_deterministic_and_provisional() -> None:
    observations = [
        {
            "source_surface": "gradient",
            "anchor_block_ids": ["block_2", "block_1"],
        },
        {
            "source_surface": "example",
            "anchor_block_ids": ["block_3"],
        },
    ]
    first = _project_term_observations(observations)
    second = _project_term_observations(copy.deepcopy(observations))

    assert first == second
    assert len(first) == 1
    batch = validate_term_lifecycle_batch(
        first[0],
        stage_id="b1_candidate_discovery",
    )
    assert batch["projection_mode"] == "live"
    assert batch["timing_authority"] == "recorded"
    assert {row["state"] for row in batch["rows"]} == {"proposed"}
    assert {row["authority"] for row in batch["rows"]} == {"none"}
    assert batch["summary"]["observations"] == 2
    assert batch["summary"]["unique_surfaces"] == 2
    assert validate_term_lifecycle_event_sequence(
        [
            {
                "event": "term_lifecycle",
                "stage_id": "b1_candidate_discovery",
                "payload": batch,
            }
        ]
    )["rows_by_id"]


def test_term_lifecycle_normalizes_line_wrapped_surface_but_rejects_unsafe_control() -> None:
    batch = _project_term_observations(
        [
            {
                "source_surface": "left-hand-side\nderivative",
                "anchor_block_ids": ["block_1"],
            }
        ]
    )[0]

    assert batch["rows"][0]["surfaces"] == ["left-hand-side derivative"]
    validate_term_lifecycle_batch(
        batch,
        stage_id="b1_candidate_discovery",
    )

    with pytest.raises(D2LConsoleContractError, match="control character"):
        _project_term_observations(
            [
                {
                    "source_surface": "unsafe\u001bterm",
                    "anchor_block_ids": ["block_1"],
                }
            ]
        )


def test_term_lifecycle_over_cap_splits_with_exact_stable_cover() -> None:
    observations = [
        {
            "source_surface": f"technical term {index:04d}",
            "anchor_block_ids": [f"block_{index:04d}"],
        }
        for index in range(300)
    ]
    batches = _project_term_observations(observations)

    assert len(batches) >= 3
    row_ids = [
        row["row_id"]
        for batch in batches
        for row in batch["rows"]
    ]
    assert len(row_ids) == len(set(row_ids)) == len(observations)
    assert all(len(canonical_json_bytes(batch)) <= 60_000 for batch in batches)
    state = validate_term_lifecycle_event_sequence(
        [
            {
                "event": "term_lifecycle",
                "stage_id": "b1_candidate_discovery",
                "payload": batch,
            }
            for batch in batches
        ]
    )
    assert len(state["rows_by_id"]) == len(observations)
    assert _project_term_observations(observations) == batches


def test_term_lifecycle_duplicate_batch_id_with_hash_drift_fails_closed() -> None:
    batch = _project_term_observations(
        [{"source_surface": "gradient", "anchor_block_ids": ["block_1"]}]
    )[0]
    drift = copy.deepcopy(batch)
    drift["summary"]["through_work_id"] = "different_work"
    unsigned = dict(drift)
    unsigned.pop("batch_sha256")
    drift["batch_sha256"] = canonical_sha256(unsigned)
    validate_term_lifecycle_batch(
        drift,
        stage_id="b1_candidate_discovery",
    )

    with pytest.raises(D2LConsoleContractError, match="batch ID.*unequal hash"):
        validate_term_lifecycle_event_sequence(
            [
                {
                    "event": "term_lifecycle",
                    "stage_id": "b1_candidate_discovery",
                    "payload": batch,
                },
                {
                    "event": "term_lifecycle",
                    "stage_id": "b1_candidate_discovery",
                    "payload": drift,
                },
            ]
        )


def test_term_lifecycle_rejects_future_origin_and_unbounded_rationale() -> None:
    future_batch = _project_term_observations(
        [{"source_surface": "gradient", "anchor_block_ids": ["block_1"]}],
        attempt=2,
    )[0]
    manifest = _manifest(status="running", attempt=1)
    event = {
        "schema": "d2l_translation_component_event_v1",
        "event_id": f"evt_{COMPONENT_RUN_ID}_00000001",
        "workflow_run_id": WORKFLOW_RUN_ID,
        "flow_kind": "terminology_translation",
        "component_id": "translation",
        "component_run_id": COMPONENT_RUN_ID,
        "component_attempt_id": 1,
        "component_seq": 1,
        "ts": "2026-07-22T00:00:01Z",
        "stage_id": "b1_candidate_discovery",
        "agent": "builder",
        "event": "term_lifecycle",
        "severity": "info",
        "payload": future_batch,
    }
    with pytest.raises(D2LConsoleContractError, match="future attempt"):
        validate_component_event(
            event,
            manifest=manifest,
            expected_component_seq=1,
        )

    b2_entry = _term_work_entry([], attempt=1)
    b2_entry["stage_id"] = "b2_admission_translation"
    b2_entry["work_item_id"] = "b2_packet_0001"
    b2_entry["result"] = {
        "decisions": [
            {
                "candidate_id": "cand_gradient",
                "decision": "admit",
                "canonical_source": "gradient",
                "primary_target_vi": "gradient",
                "primary_use": None,
                "alternates": [],
                "evidence_block_ids": ["block_1"],
                "rationale": "x" * 513,
            }
        ]
    }
    validation = _term_validation_event(
        attempt=1,
        work_item_id="b2_packet_0001",
    )
    validation["stage_id"] = "b2_admission_translation"
    with pytest.raises(D2LConsoleContractError, match="rationale.*exceeds"):
        project_work_journal_term_batches(
            stage_id="b2_admission_translation",
            journal_ref=(
                "runtime/work_items/b2_admission_translation.jsonl"
            ),
            entry=b2_entry,
            validation_event=validation,
            previous_rows=[],
            projection_mode="live",
            completed=1,
            total=1,
            unit="packets",
        )


def test_term_lifecycle_b2_and_auditors_follow_closed_transitions() -> None:
    surfaces = [
        "gradient",
        "boilerplate",
        "example",
        "covariate shift",
        "parameter",
        "sample",
    ]
    batches = _project_term_observations(
        [
            {
                "source_surface": surface,
                "anchor_block_ids": [f"block_{index}"],
            }
            for index, surface in enumerate(surfaces, start=1)
        ]
    )
    previous_rows = [
        row for batch in batches for row in batch["rows"]
    ]

    b2_batches = _project_term_stage(
        stage_id="b2_admission_translation",
        work_item_id="b2_packet_0001",
        result={
            "decisions": [
                {
                    "candidate_id": f"cand_{surface.replace(' ', '_')}",
                    "decision": decision,
                    "canonical_source": surface,
                    "primary_target_vi": (
                        f"vi_{surface.replace(' ', '_')}"
                        if decision == "admit"
                        else None
                    ),
                    "primary_use": None,
                    "alternates": [],
                    "evidence_block_ids": [f"block_{index}"],
                    "rationale": f"B2 {decision}.",
                }
                for index, (surface, decision) in enumerate(
                    zip(
                        surfaces,
                        (
                            "admit",
                            "reject",
                            "review",
                            "admit",
                            "admit",
                            "admit",
                        ),
                        strict=True,
                    ),
                    start=1,
                )
            ]
        },
        previous_rows=previous_rows,
        component_seq=18,
    )
    batches.extend(b2_batches)
    previous_rows.extend(
        row for batch in b2_batches for row in batch["rows"]
    )

    morphology_batches = _project_term_stage(
        stage_id="auditor_morphology",
        work_item_id="morphology_packet_0001",
        result={
            "decisions": [
                {
                    "component_id": "component_covariate_shift",
                    "action": "pending",
                    "pending_reason": "Needs more evidence.",
                },
                {
                    "component_id": "component_resolved",
                    "action": "keep",
                    "resolved_entries": [
                        {
                            "canonical_source": surface,
                            "canonical_target_vi": (
                                f"vi_{surface.replace(' ', '_')}"
                            ),
                            "member_candidate_ids": [
                                f"cand_{surface.replace(' ', '_')}"
                            ],
                            "alternative_targets": [],
                            "evidence_block_ids": [f"block_{index}"],
                            "rationale": "Morphology resolved.",
                        }
                        for index, surface in (
                            (1, "gradient"),
                            (5, "parameter"),
                            (6, "sample"),
                        )
                    ],
                },
            ]
        },
        previous_rows=previous_rows,
        component_seq=28,
    )
    batches.extend(morphology_batches)
    previous_rows.extend(
        row for batch in morphology_batches for row in batch["rows"]
    )

    collision_batches = _project_term_stage(
        stage_id="auditor_target_collision",
        work_item_id="collision_packet_0001",
        result={
            "decisions": [
                {
                    "component_id": "component_parameter",
                    "action": "pending",
                    "pending_reason": "Target collision remains ambiguous.",
                },
                {
                    "component_id": "component_collision_resolved",
                    "action": "keep",
                    "resolved_entries": [
                        {
                            "canonical_source": surface,
                            "canonical_target_vi": (
                                f"vi_{surface.replace(' ', '_')}"
                            ),
                            "member_candidate_ids": [
                                f"cand_{surface.replace(' ', '_')}"
                            ],
                            "alternative_targets": [],
                            "evidence_block_ids": [f"block_{index}"],
                            "rationale": "Collision resolved.",
                        }
                        for index, surface in (
                            (1, "gradient"),
                            (6, "sample"),
                        )
                    ],
                },
            ]
        },
        previous_rows=previous_rows,
        component_seq=38,
    )
    batches.extend(collision_batches)
    previous_rows.extend(
        row for batch in collision_batches for row in batch["rows"]
    )

    multi_target_batches = _project_term_stage(
        stage_id="auditor_multi_target",
        work_item_id="multi_target_packet_0001",
        result={
            "decisions": [
                {
                    "candidate_id": "cand_gradient",
                    "action": "resolve",
                    "target_dispositions": [
                        {
                            "target_vi": "vi_gradient",
                            "applicability": None,
                            "disposition": "canonical",
                        }
                    ],
                    "evidence_block_ids": ["block_1"],
                    "rationale": "One stable target.",
                },
                {
                    "candidate_id": "cand_sample",
                    "action": "pending",
                    "target_dispositions": [],
                    "evidence_block_ids": ["block_6"],
                    "pending_reason": "Context split remains unresolved.",
                },
            ]
        },
        previous_rows=previous_rows,
        component_seq=48,
    )
    batches.extend(multi_target_batches)

    events = [
        {
            "event": "term_lifecycle",
            "stage_id": stage_id,
            "payload": batch,
        }
        for stage_id, stage_batches in (
            ("b1_candidate_discovery", batches[:1]),
            ("b2_admission_translation", b2_batches),
            ("auditor_morphology", morphology_batches),
            ("auditor_target_collision", collision_batches),
            ("auditor_multi_target", multi_target_batches),
        )
        for batch in stage_batches
    ]
    state = validate_term_lifecycle_event_sequence(events)
    lifecycle_states = {
        row["state"] for row in state["rows_by_id"].values()
    }
    assert lifecycle_states == {
        "proposed",
        "admitted",
        "rejected",
        "review_held",
        "morphology_resolved",
        "morphology_pending",
        "collision_resolved",
        "collision_pending",
        "multi_target_resolved",
        "multi_target_pending",
    }
    assert {
        row["authority"] for row in state["rows_by_id"].values()
    } == {"none"}

    rejected = next(
        row
        for row in state["rows_by_id"].values()
        if row["state"] == "rejected"
    )
    invalid_batch = copy.deepcopy(morphology_batches[0])
    invalid_row = next(
        row
        for row in invalid_batch["rows"]
        if row["state"] == "morphology_resolved"
    )
    invalid_row["supersedes_row_ids"] = [rejected["row_id"]]
    unsigned_row = dict(invalid_row)
    unsigned_row.pop("row_sha256")
    invalid_row["row_sha256"] = canonical_sha256(unsigned_row)
    unsigned_batch = dict(invalid_batch)
    unsigned_batch.pop("batch_sha256")
    invalid_batch["batch_sha256"] = canonical_sha256(unsigned_batch)
    validate_term_lifecycle_batch(
        invalid_batch,
        stage_id="auditor_morphology",
    )
    invalid_events = [
        event
        for event in events
        if event["stage_id"] in {
            "b1_candidate_discovery",
            "b2_admission_translation",
        }
    ]
    invalid_events.append(
        {
            "event": "term_lifecycle",
            "stage_id": "auditor_morphology",
            "payload": invalid_batch,
        }
    )
    with pytest.raises(
        D2LConsoleContractError,
        match="state transition is invalid",
    ):
        validate_term_lifecycle_event_sequence(invalid_events)
