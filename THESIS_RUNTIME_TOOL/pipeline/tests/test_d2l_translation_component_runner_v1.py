from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    build_scoring_handoff_fragment,
    build_stage_plan,
    canonical_sha256,
    file_sha256,
    validate_translation_component_package,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    STAGE_RECEIPT_SCHEMA,
    build_stage_receipt,
)
from pipeline.prepass.d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
    component_writer_is_active,
    stage_writer_is_active,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import (
    TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE,
)
from pipeline.prepass.d2l_stage_work_journal_v1 import D2LStageWorkJournal
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    ComponentRunnerError,
    D2LTranslationComponentRunner,
    RUNNER_SCHEMA,
)


GIT_COMMIT = "1" * 40
CONFIG_SHA = "2" * 64
CODE_SHA = "3" * 64
PROFILE_SHA = "4" * 64
CHAPTERS = ["d2l_multilayer_perceptrons"]
TOOL_ROOT = Path(__file__).resolve().parents[2]


def _wait_until(predicate, *, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_process_only(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.kill()
    process.wait(timeout=10)


def _kill_process_tree(pid: int) -> None:
    if not _pid_is_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            check=False,
            capture_output=True,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def _event_counts(root: Path) -> Counter[str]:
    return Counter(
        json.loads(line)["event"]
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )


def _binding(ref: str, kind: str, schema: str, digest: str) -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": digest,
        "sha256_kind": "physical",
    }


def _source_binding() -> dict[str, object]:
    def item(ref: str, kind: str, schema: str) -> dict[str, str]:
        digest = sha256(ref.encode("utf-8")).hexdigest().upper()
        return _binding(ref, kind, schema, digest)

    return {
        "schema": "canonical_source_binding_v1",
        "document": item("src_document", "source_document", "document_v1"),
        "structure_manifest": item(
            "src_structure", "structure_manifest", "structure_manifest_v1"
        ),
        "asset_manifest": item("src_assets", "asset_manifest", "asset_manifest_v1"),
        "admitted_projection": item(
            "src_projection", "admitted_projection", "admitted_projection_v1"
        ),
        "normalization_receipt": item(
            "src_receipt", "normalization_receipt", "normalization_receipt_v1"
        ),
        "package_seal": item(
            "src_package_seal", "source_package_seal", "source_package_seal_v1"
        ),
    }


def _write_payloads(root: Path, *, attempt_id: int) -> None:
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    s0_path = artifacts / "translation_s0.json"
    s1_path = artifacts / "translation_s1.json"
    glossary_path = artifacts / "glossary.json"
    s0_path.write_text('{"arm":"s0","blocks":["b001"]}\n', encoding="utf-8")
    s1_path.write_text('{"arm":"s1","blocks":["b001"]}\n', encoding="utf-8")
    glossary_path.write_text('{"entries":[]}\n', encoding="utf-8")
    source = _source_binding()
    universe_sha = sha256(b"b001").hexdigest().upper()
    inputs = []
    for arm_id, path in (("s0", s0_path), ("s1", s1_path)):
        inputs.append(
            {
                "arm_id": arm_id,
                "artifact": _binding(
                    f"art_translation_{arm_id}",
                    "translation_artifact",
                    "TranslationArtifactV1",
                    file_sha256(path),
                ),
                "producer_component_run_id": "tr_component_test_v1",
                "producer_component_attempt_id": attempt_id,
                "profile_id": f"d2l_{arm_id}_v1",
                "profile_sha256": PROFILE_SHA,
                "config_sha256": CONFIG_SHA,
                "selected_chapter_ids": CHAPTERS,
                "coverage": {
                    "admitted_block_count": 1,
                    "translated_block_count": 1,
                    "preserved_block_count": 0,
                    "missing_block_count": 0,
                    "failed_block_count": 0,
                    "ordered_block_ids_sha256": universe_sha,
                    "status": "exact_cover",
                },
                "source_binding_sha256": canonical_sha256(source),
            }
        )
    fragment = build_scoring_handoff_fragment(
        workflow_run_id="wf_component_test_v1",
        translation_component_run_id="tr_component_test_v1",
        translation_component_attempt_id=attempt_id,
        reserved_evaluation_component_run_id="ev_component_test_v1",
        artifact_ref="art_scoring_handoff_fragment",
        source_binding=source,
        translation_inputs=inputs,
        glossary_binding=_binding(
            "art_glossary",
            "glossary",
            "D2LGlossaryV1",
            file_sha256(glossary_path),
        ),
        context_memory_binding=None,
        selected_chapter_ids=CHAPTERS,
        admitted_universe={
            "ordered_block_ids_sha256": universe_sha,
            "block_count": 1,
            "status": "exact_cover",
        },
        producer_lineage={
            "git_commit": GIT_COMMIT,
            "pipeline_version": "d2l_translation_component_runner_v1",
            "config_sha256": CONFIG_SHA,
            "code_sha256": CODE_SHA,
        },
        created_at="2026-07-22T00:00:00Z",
    )
    (root / "scoring_handoff_fragment.json").write_text(
        json.dumps(fragment, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact_spec(
    ref: str,
    kind: str,
    schema: str,
    path: str,
) -> dict[str, object]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "relative_path": path,
        "parent_artifact_refs": [],
        "metadata": {},
    }


def _plan(*, attempt_id: int) -> dict[str, object]:
    units = {row["stage_id"]: row["progress"]["unit"] for row in build_stage_plan()}
    stages = []
    for stage_id in STAGE_IDS:
        specs: list[dict[str, object]] = []
        if stage_id == "glossary_seal":
            specs.append(
                _artifact_spec(
                    "art_glossary", "glossary", "D2LGlossaryV1", "artifacts/glossary.json"
                )
            )
        elif stage_id == "translator":
            specs.extend(
                [
                    _artifact_spec(
                        "art_translation_s0",
                        "translation_artifact",
                        "TranslationArtifactV1",
                        "artifacts/translation_s0.json",
                    ),
                    _artifact_spec(
                        "art_translation_s1",
                        "translation_artifact",
                        "TranslationArtifactV1",
                        "artifacts/translation_s1.json",
                    ),
                ]
            )
        elif stage_id == "scoring_handoff_fragment":
            specs.append(
                _artifact_spec(
                    "art_scoring_handoff_fragment",
                    "scoring_handoff_fragment",
                    "scoring_handoff_fragment_v1",
                    "scoring_handoff_fragment.json",
                )
            )
        stages.append(
            {
                "stage_id": stage_id,
                "producer": stage_id,
                "command": [sys.executable, "-c", "pass"],
                "cwd": None,
                "artifact_specs": specs,
                "total": 1,
                "unit": units[stage_id],
                "work_id": f"work_{stage_id}",
                "mode": "execute",
                "timeout_seconds": 30,
                "receipt_ref": None,
            }
        )
    return {
        "schema": RUNNER_SCHEMA,
        "workflow_run_id": "wf_component_test_v1",
        "component_run_id": "tr_component_test_v1",
        "pipeline_id": "d2l_terminology",
        "pipeline_version": "d2l_translation_component_runner_v1",
        "source_binding": _source_binding(),
        "config_sha256": CONFIG_SHA,
        "code_revision": GIT_COMMIT,
        "selected_chapter_ids": CHAPTERS,
        "stages": stages,
        "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
    }


def test_runner_emits_terminal_component_package(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    result = D2LTranslationComponentRunner(_plan(attempt_id=1), root).run()

    assert result["terminal_event"] == "run_done"
    assert result["artifact_count"] == 4
    counts = _event_counts(root)
    assert "cost_snapshot" not in counts
    assert "response_received" not in counts
    assert not (root / "workflow_manifest.json").exists()
    assert not (root / "scoring_handoff.json").exists()
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert all(stage["status"] == "succeeded" for stage in manifest["stages"])
    assert validate_translation_component_package(root)["component_attempt_id"] == 1


def test_runner_pause_resume_increments_component_attempt(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))

    paused = D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
    ).run()
    assert paused["terminal_event"] is None
    manifest1 = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest1["status"] == "paused"
    assert manifest1["component_attempt_id"] == 1
    assert manifest1["resume"]["resume_available"] is True

    resumed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert resumed["terminal_event"] == "run_done"
    assert resumed["component_attempt_id"] == 2
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row["event"] == "run_resumed" for row in events) == 1
    assert [row["component_attempt_id"] for row in events if row["event"] == "run_resumed"] == [2]


def test_transport_retry_exhaustion_pauses_and_resumes_same_stage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    marker = tmp_path / "transport_recovered"
    script = tmp_path / "transport_pause_once.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            marker = Path(sys.argv[1])
            if not marker.exists():
                marker.write_text("retry later", encoding="utf-8")
                raise SystemExit({TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE})
            """
        ),
        encoding="utf-8",
    )
    raw_plan = _plan(attempt_id=2)
    raw_plan["stages"][0]["command"] = [
        sys.executable,
        str(script),
        str(marker),
    ]
    plan = ComponentPlan.from_mapping(raw_plan)

    paused = D2LTranslationComponentRunner(plan, root).run()
    assert paused["terminal_event"] is None
    first_manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert first_manifest["status"] == "paused"
    assert first_manifest["active_stage_id"] == "preflight"
    assert first_manifest["resume"]["paused_reason"] == (
        "transport_retry_exhausted"
    )
    assert _event_counts(root)["run_failed"] == 0

    completed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 2


def test_runner_honors_pause_marker_only_at_stage_boundary(tmp_path: Path) -> None:
    root = tmp_path / "component"
    pause_file = tmp_path / "PAUSE"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))

    # The marker is created before execution to model an App pause request.
    # The current stage is still allowed to finish; the next stage is the
    # checkpoint boundary.
    pause_file.write_text("paused_by_user\n", encoding="utf-8")
    paused = D2LTranslationComponentRunner(
        plan,
        root,
        pause_file=pause_file,
    ).run()

    assert paused["terminal_event"] is None
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "paused"
    assert manifest["active_stage_id"] == "b1_candidate_discovery"
    assert manifest["resume"]["paused_reason"] == "user_requested_pause"


def test_checkpoint_binds_durable_work_journal_lineage(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    journal = D2LStageWorkJournal(
        path=root / "runtime/work_items/b1_candidate_discovery.jsonl",
        workflow_run_id="wf_component_test_v1",
        component_run_id="tr_component_test_v1",
        component_attempt_id=1,
        stage_id="b1_candidate_discovery",
    )
    journal.append(
        work_item_id="b1_window_0001",
        work_contract_id="candidate_v1",
        input_sha256=canonical_sha256({"source": "window"}),
        result={"window_id": "b1_window_0001"},
    )
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))

    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="preflight",
    ).run()
    manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    checkpoint = json.loads(
        (root / manifest["resume"]["checkpoint_ref"]).read_text(
            encoding="utf-8"
        )
    )
    state = checkpoint["state"]["work_journals"]["b1_candidate_discovery"]
    assert state["entry_count"] == 1
    assert state["journal_ref"] == (
        "runtime/work_items/b1_candidate_discovery.jsonl"
    )

    journal.append(
        work_item_id="b1_window_0002",
        work_contract_id="candidate_v1",
        input_sha256=canonical_sha256({"source": "changed-after-checkpoint"}),
        result={"window_id": "b1_window_0002"},
    )
    with pytest.raises(
        ComponentRunnerError,
        match="work journal lineage mismatch",
    ):
        D2LTranslationComponentRunner(plan, root).run(resume=True)


def _write_streaming_stage_script(tmp_path: Path) -> Path:
    script = tmp_path / "streaming_stage.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time
            from pathlib import Path

            sys.path.insert(0, sys.argv[2])
            from pipeline.prepass.d2l_component_stage_receipt_v1 import (
                D2LStageObservationJournalWriter,
                build_stage_receipt,
                read_observation_journal,
            )
            from pipeline.prepass.d2l_console_replay_contract_v1 import (
                build_component_usage_snapshot,
                write_json,
            )

            root = Path(sys.argv[1])
            manifest = json.loads(
                (root / "component_manifest.json").read_text(encoding="utf-8")
            )
            attempt = int(manifest["component_attempt_id"])
            stage_id = "preflight"
            producer = "preflight"
            work_id = "work_preflight"
            unit = "checks"
            journal_path = root / "runtime/component_observations.jsonl"
            entries = read_observation_journal(journal_path)
            prior_snapshots = [
                dict(entry["observation"]["payload"])
                for entry in entries
                if entry["observation"]["event"] == "usage_snapshot"
            ]
            writer = D2LStageObservationJournalWriter(
                path=journal_path,
                workflow_run_id=manifest["workflow_run_id"],
                component_run_id=manifest["component_run_id"],
                component_attempt_id=attempt,
                stage_id=stage_id,
                producer=producer,
                work_id=work_id,
            )
            observations = []

            def append(event, payload):
                observation = {
                    "event": event,
                    "agent": producer,
                    "severity": "info",
                    "ts": f"2026-07-23T00:00:0{attempt}Z",
                    "payload": payload,
                }
                writer.append(observation)
                observations.append(observation)

            append(
                "work_progress",
                {
                    "work_kind": unit,
                    "work_id": work_id,
                    "progress": {"completed": attempt - 1, "total": 2, "unit": unit},
                },
            )
            logical_request_id = f"request_attempt_{attempt}"
            request_work_id = f"request_work_{attempt}"
            append(
                "request_sent",
                {
                    "logical_request_id": logical_request_id,
                    "physical_attempt_index": 1,
                    "work_kind": unit,
                    "work_id": request_work_id,
                    "provider_id": "provider",
                    "model_id": "model",
                    "source_id": "source",
                    "masked_quota_bucket": "bucket-***",
                },
            )
            usage = {
                "logical_request_id": logical_request_id,
                "physical_attempt_index": 1,
                "provider_id": "provider",
                "model_id": "model",
                "source_id": "source",
                "masked_quota_bucket": "bucket-***",
                "prompt_tokens": 10 * attempt,
                "completion_tokens": 2 * attempt,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 12 * attempt,
                "latency_ms": 5,
                "finish_reason": "stop",
                "cost_usd": None,
                "currency": None,
                "cost_status": "unknown",
                "cache_status": "miss",
                "cache_mechanism": "local_exact_cache",
            }
            append("response_received", {"usage": usage})
            snapshot = build_component_usage_snapshot(
                previous_snapshots=prior_snapshots,
                workflow_run_id=manifest["workflow_run_id"],
                component_run_id=manifest["component_run_id"],
                component_attempt_id=attempt,
                stage_id=stage_id,
                work_id=request_work_id,
                accepted_usage={
                    "identity_kind": "provider_attempt",
                    "attempt_usage_id": f"attempt_usage_{attempt}",
                    "cache_observation_id": f"cache_observation_{attempt}",
                    "logical_request_id": logical_request_id,
                    "semantic_attempt_index": 1,
                    "transport_retry_ordinal": 0,
                    "physical_attempt_index": 1,
                    "provider_called": True,
                    "source_revision": "source_v1",
                    "usage": usage,
                },
            )
            append("usage_snapshot", snapshot)
            append(
                "work_progress",
                {
                    "work_kind": unit,
                    "work_id": work_id,
                    "progress": {"completed": attempt, "total": 2, "unit": unit},
                },
            )
            if attempt == 1:
                time.sleep(30)
            receipt = build_stage_receipt(
                workflow_run_id=manifest["workflow_run_id"],
                component_run_id=manifest["component_run_id"],
                component_attempt_id=attempt,
                stage_id=stage_id,
                producer=producer,
                work_id=work_id,
                observations=observations,
            )
            write_json(root / "artifacts/preflight_receipt.json", receipt)
            """
        ),
        encoding="utf-8",
    )
    return script


def test_runner_streams_partial_usage_and_resumes_without_double_count(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    pause_file = tmp_path / "PAUSE"
    _write_payloads(root, attempt_id=2)
    plan_row = _plan(attempt_id=2)
    script = _write_streaming_stage_script(tmp_path)
    preflight = plan_row["stages"][0]
    source_root = Path(__file__).resolve().parents[2]
    preflight["command"] = [
        sys.executable,
        str(script),
        str(root),
        str(source_root),
    ]
    preflight["cwd"] = str(source_root)
    preflight["total"] = 99
    preflight["receipt_ref"] = "artifacts/preflight_receipt.json"
    preflight["artifact_specs"] = [
        _artifact_spec(
            "art_preflight_receipt",
            "d2l_stage_event_receipt",
            STAGE_RECEIPT_SCHEMA,
            "artifacts/preflight_receipt.json",
        )
    ]
    plan = ComponentPlan.from_mapping(plan_row)

    def request_pause_after_first_snapshot() -> None:
        journal = root / "runtime/component_observations.jsonl"
        deadline = time.time() + 10
        while time.time() < deadline:
            if journal.is_file() and '"event":"usage_snapshot"' in journal.read_text(
                encoding="utf-8"
            ):
                pause_file.write_text("pause\n", encoding="utf-8")
                return
            time.sleep(0.02)
        raise AssertionError("streaming stage did not publish its first usage snapshot")

    pause_thread = threading.Thread(
        target=request_pause_after_first_snapshot,
        daemon=True,
    )
    pause_thread.start()
    paused = D2LTranslationComponentRunner(
        plan,
        root,
        pause_file=pause_file,
    ).run()
    pause_thread.join(timeout=2)

    assert paused["terminal_event"] is None
    assert paused["component_usage"]["accepted_result_count"] == 1
    paused_manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    preflight_state = paused_manifest["stages"][0]
    assert preflight_state["status"] == "paused"
    assert preflight_state["progress"] == {
        "completed": 1,
        "total": 2,
        "unit": "checks",
    }

    completed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 2
    assert completed["component_usage"]["accepted_result_count"] == 2
    assert completed["component_usage"]["physical_attempt_count"] == 2
    assert completed["component_usage"]["prompt_tokens"] == 30
    assert completed["component_usage"]["completion_tokens"] == 6
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    usage_events = [row for row in events if row["event"] == "usage_snapshot"]
    assert [row["payload"]["snapshot_seq"] for row in usage_events] == [1, 2, 3]
    assert usage_events[-1]["payload"]["snapshot_kind"] == "component_final"


def test_runner_rejects_plan_with_forbidden_semantic_evidence() -> None:
    plan = _plan(attempt_id=1)
    plan["gold"] = {"term": "answer"}

    with pytest.raises(ComponentRunnerError, match="forbidden key"):
        ComponentPlan.from_mapping(plan)


def test_runner_rejects_duplicate_artifact_refs_before_execution() -> None:
    plan = _plan(attempt_id=1)
    plan["stages"][0]["artifact_specs"] = [
        _artifact_spec(
            "art_glossary",
            "preflight_receipt",
            "preflight_receipt_v1",
            "artifacts/preflight.json",
        )
    ]

    with pytest.raises(ComponentRunnerError, match="declared more than once"):
        ComponentPlan.from_mapping(plan)


def test_runner_imports_validated_child_stage_receipt(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    receipt_ref = "artifacts/preflight_receipt.json"
    receipt = build_stage_receipt(
        workflow_run_id="wf_component_test_v1",
        component_run_id="tr_component_test_v1",
        component_attempt_id=1,
        stage_id="preflight",
        producer="preflight",
        work_id="work_preflight",
        observations=[
            {
                "event": "request_sent",
                "agent": "preflight",
                "severity": "info",
                "ts": "2026-07-22T00:00:01Z",
                "payload": {
                    "logical_request_id": "req_preflight_1",
                    "physical_attempt_index": 1,
                    "work_kind": "check",
                    "work_id": "work_preflight",
                    "provider_id": "fake_provider",
                    "model_id": "fake_model",
                    "source_id": "fake_source",
                    "masked_quota_bucket": "fake-bucket-***",
                },
            },
            {
                "event": "response_received",
                "agent": "preflight",
                "severity": "info",
                "ts": "2026-07-22T00:00:02Z",
                "payload": {
                    "usage": {
                        "logical_request_id": "req_preflight_1",
                        "physical_attempt_index": 1,
                        "provider_id": "fake_provider",
                        "model_id": "fake_model",
                        "source_id": "fake_source",
                        "masked_quota_bucket": "fake-bucket-***",
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 12,
                        "latency_ms": 20,
                        "finish_reason": "stop",
                        "cost_usd": None,
                        "currency": None,
                        "cost_status": "unknown",
                        "cache_status": "miss",
                        "cache_mechanism": "none",
                    }
                },
            },
        ],
    )
    (root / receipt_ref).write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    plan = _plan(attempt_id=1)
    plan["stages"][0]["receipt_ref"] = receipt_ref
    plan["stages"][0]["artifact_specs"] = [
        _artifact_spec(
            "art_preflight_receipt",
            "d2l_stage_event_receipt",
            STAGE_RECEIPT_SCHEMA,
            receipt_ref,
        )
    ]

    result = D2LTranslationComponentRunner(plan, root).run()

    counts = _event_counts(root)
    assert result["terminal_event"] == "run_done"
    assert counts["request_sent"] == 1
    assert counts["response_received"] == 1
    assert result["artifact_count"] == 5


def test_runner_does_not_resume_after_plan_drift(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    D2LTranslationComponentRunner(
        _plan(attempt_id=2), root, stop_after_stage="candidate_index"
    ).run()
    drifted = _plan(attempt_id=2)
    drifted["config_sha256"] = "F" * 64

    with pytest.raises(ComponentRunnerError, match="sealed plan"):
        D2LTranslationComponentRunner(drifted, root).run(resume=True)


def test_runner_rejects_argv_drift_before_write_or_execution(tmp_path: Path) -> None:
    root = tmp_path / "component"
    marker = tmp_path / "replacement-command-ran.txt"
    _write_payloads(root, attempt_id=2)
    D2LTranslationComponentRunner(
        _plan(attempt_id=2), root, stop_after_stage="candidate_index"
    ).run()
    before = {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }
    drifted = _plan(attempt_id=2)
    drifted["stages"][3]["command"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]

    with pytest.raises(ComponentRunnerError, match="runner plan hash mismatch"):
        D2LTranslationComponentRunner(drifted, root).run(resume=True)

    assert not marker.exists()
    assert before == {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }


def test_runner_rejects_material_noncommand_drift_before_write(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    D2LTranslationComponentRunner(
        _plan(attempt_id=2), root, stop_after_stage="candidate_index"
    ).run()
    before = {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }
    drifted = _plan(attempt_id=2)
    drifted["stages"][3]["timeout_seconds"] = 31

    with pytest.raises(ComponentRunnerError, match="runner plan hash mismatch"):
        D2LTranslationComponentRunner(drifted, root).run(resume=True)

    assert before == {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }


def test_runner_pauses_nonzero_stage_and_resumes_same_component(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    marker = tmp_path / "stage_repaired"
    script = tmp_path / "fail_once.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            from pathlib import Path

            marker = Path(sys.argv[1])
            if not marker.exists():
                marker.write_text("repairable", encoding="utf-8")
                print("bounded stage diagnostic", file=sys.stderr)
                raise SystemExit(7)
            """
        ),
        encoding="utf-8",
    )
    raw_plan = _plan(attempt_id=2)
    raw_plan["stages"][3]["command"] = [
        sys.executable,
        str(script),
        str(marker),
    ]
    plan = ComponentPlan.from_mapping(raw_plan)

    result = D2LTranslationComponentRunner(plan, root).run()
    assert result["terminal_event"] is None
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "paused"
    assert manifest["resume"]["resume_available"] is True
    assert manifest["resume"]["paused_reason"] == "stage_process_exit_7"
    assert manifest["scoring_handoff_fragment_ref"] is None
    paused = next(
        stage for stage in manifest["stages"] if stage["stage_id"] == "b2_admission_translation"
    )
    assert paused["status"] == "paused"
    assert paused["ended_at"] is None
    assert paused["current_work_id"] == "work_b2_admission_translation"
    counts = _event_counts(root)
    assert counts["validation_failed"] == 1
    assert counts["run_failed"] == 0
    stderr_log = (
        root
        / "runtime/stage_process_logs/attempt_0001"
        / "b2_admission_translation.stderr.log"
    )
    assert stderr_log.read_text(encoding="utf-8").strip() == (
        "bounded stage diagnostic"
    )

    completed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 2


def test_runner_requires_and_records_explicit_same_run_code_repair(
    tmp_path: Path,
) -> None:
    code_root = tmp_path / "code"
    code_root.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(code_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.name", "CodeX")
    git("config", "user.email", "codex@example.invalid")
    repair_relative = (
        "THESIS_RUNTIME_TOOL/pipeline/prepass/"
        "d2l_translation_component_runner_v1.py"
    )
    repair_target = code_root / repair_relative
    repair_target.parent.mkdir(parents=True)
    repair_target.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", repair_relative)
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")

    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    raw_plan = _plan(attempt_id=2)
    raw_plan["code_revision"] = baseline
    plan = ComponentPlan.from_mapping(raw_plan)
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
        repair_code_root=code_root,
    ).run()

    repair_target.write_text("VALUE = 2\n", encoding="utf-8")
    git("add", repair_relative)
    git("commit", "-m", "mechanical repair")
    effective = git("rev-parse", "HEAD")
    before = {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }

    with pytest.raises(
        ComponentRunnerError,
        match="explicit repair reason is required",
    ):
        D2LTranslationComponentRunner(
            plan,
            root,
            repair_code_root=code_root,
        ).run(resume=True)

    assert before == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }

    completed = D2LTranslationComponentRunner(
        plan,
        root,
        repair_code_root=code_root,
        repair_reason="repair_json_envelope_normalization",
    ).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 2

    receipt_path = root / "runtime/repair_receipts/repair_a0002.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["baseline_code_revision"] == baseline
    assert receipt["effective_code_revision"] == effective
    assert receipt["changed_paths"] == [repair_relative]
    assert receipt["repair_scope_policy_id"] == "d2l_mechanical_repair_paths_v1"
    index = json.loads((root / "artifact_index.json").read_text(encoding="utf-8"))
    repair_artifact = next(
        row
        for row in index["artifacts"]
        if row["artifact_kind"] == "d2l_component_repair_receipt"
    )
    assert repair_artifact["sha256"] == file_sha256(receipt_path)
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    resumed = next(row for row in events if row["event"] == "run_resumed")
    assert resumed["payload"]["reason_code"] == "resume_after_code_repair"


def test_runner_rejects_semantic_code_change_before_resume_mutation(
    tmp_path: Path,
) -> None:
    code_root = tmp_path / "code"
    prompt_relative = "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py"
    prompt_path = code_root / prompt_relative
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text('PROMPT = "sealed"\n', encoding="utf-8")

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(code_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.name", "CodeX")
    git("config", "user.email", "codex@example.invalid")
    git("add", prompt_relative)
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")

    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    raw_plan = _plan(attempt_id=2)
    raw_plan["code_revision"] = baseline
    plan = ComponentPlan.from_mapping(raw_plan)
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
        repair_code_root=code_root,
    ).run()

    prompt_path.write_text('PROMPT = "changed"\n', encoding="utf-8")
    git("add", prompt_relative)
    git("commit", "-m", "semantic prompt change")
    before = {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }

    with pytest.raises(ComponentRunnerError, match="closed mechanical scope"):
        D2LTranslationComponentRunner(
            plan,
            root,
            repair_code_root=code_root,
            repair_reason="incorrectly_claimed_mechanical_fix",
        ).run(resume=True)

    assert before == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }
    assert not (root / "runtime/repair_receipts/repair_a0002.json").exists()


def test_stale_recovery_rejects_semantic_delta_before_package_mutation(
    tmp_path: Path,
) -> None:
    code_root = tmp_path / "code"
    prompt_relative = "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py"
    prompt_path = code_root / prompt_relative
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text('PROMPT = "sealed"\n', encoding="utf-8")

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(code_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.name", "CodeX")
    git("config", "user.email", "codex@example.invalid")
    git("add", prompt_relative)
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")

    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    raw_plan = _plan(attempt_id=2)
    raw_plan["code_revision"] = baseline
    plan = ComponentPlan.from_mapping(raw_plan)
    abandoned = D2LTranslationComponentRunner(
        plan,
        root,
        repair_code_root=code_root,
    )
    abandoned._start_new()
    abandoned._start_stage(plan.stages[0])

    prompt_path.write_text('PROMPT = "changed"\n', encoding="utf-8")
    git("add", prompt_relative)
    git("commit", "-m", "semantic prompt change")
    before = {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }

    with pytest.raises(ComponentRunnerError, match="closed mechanical scope"):
        D2LTranslationComponentRunner(
            plan,
            root,
            recover_stale=True,
            repair_code_root=code_root,
            repair_reason="incorrectly_claimed_mechanical_fix",
        ).run(resume=True)

    assert before == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }
    manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "running"
    assert not (root / "runtime/repair_receipts/repair_a0002.json").exists()


def test_runner_quarantines_incomplete_observation_tail_and_resumes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    marker = tmp_path / "tail_written"
    script = tmp_path / "write_partial_tail_once.py"
    journal_path = root / "runtime/component_observations.jsonl"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            from pathlib import Path

            marker = Path(sys.argv[1])
            journal = Path(sys.argv[2])
            if not marker.exists():
                marker.write_text("written", encoding="utf-8")
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open("ab") as handle:
                    handle.write(b'{"partial_observation"')
                    handle.flush()
                raise SystemExit(9)
            """
        ),
        encoding="utf-8",
    )
    raw_plan = _plan(attempt_id=2)
    raw_plan["stages"][3]["command"] = [
        sys.executable,
        str(script),
        str(marker),
        str(journal_path),
    ]
    plan = ComponentPlan.from_mapping(raw_plan)

    paused = D2LTranslationComponentRunner(plan, root).run()
    assert paused["terminal_event"] is None
    manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "paused"
    assert (
        manifest["resume"]["paused_reason"]
        == "observation_journal_incomplete_tail"
    )
    receipt_path = (
        root
        / "runtime/journal_recovery/a0001"
        / "b2_admission_translation.receipt.json"
    )
    tail_path = (
        root
        / "runtime/journal_recovery/a0001"
        / "b2_admission_translation.tail.bin"
    )
    assert receipt_path.is_file()
    assert tail_path.read_bytes() == b'{"partial_observation"'
    journal_bytes = journal_path.read_bytes()
    assert not journal_bytes or journal_bytes.endswith(b"\n")

    completed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 2
    index = json.loads((root / "artifact_index.json").read_text(encoding="utf-8"))
    assert any(
        row["artifact_kind"] == "d2l_observation_journal_recovery"
        for row in index["artifacts"]
    )


def test_runner_recovers_stale_running_attempt_with_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))

    abandoned = D2LTranslationComponentRunner(plan, root)
    root.mkdir(parents=True, exist_ok=True)
    abandoned._start_new()
    abandoned._start_stage(plan.stages[0])
    stale_journal = root / "runtime/component_observations.jsonl"
    stale_journal.parent.mkdir(parents=True, exist_ok=True)
    stale_journal.write_bytes(b'{"partial_after_parent_crash"')
    before = {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }
    before_journal = stale_journal.read_bytes()

    with pytest.raises(
        ComponentRunnerError,
        match="explicit stale-attempt recovery",
    ):
        D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert before == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }
    assert stale_journal.read_bytes() == before_journal

    completed = D2LTranslationComponentRunner(
        plan,
        root,
        recover_stale=True,
    ).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 2
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    checkpoint_index = next(
        index
        for index, row in enumerate(events)
        if row["event"] == "checkpoint"
        and row["payload"]["paused_reason"] == "stale_process_recovered"
    )
    resumed_index = next(
        index for index, row in enumerate(events) if row["event"] == "run_resumed"
    )
    assert checkpoint_index < resumed_index
    assert events[checkpoint_index]["component_attempt_id"] == 1
    assert events[resumed_index]["component_attempt_id"] == 2
    index = json.loads((root / "artifact_index.json").read_text(encoding="utf-8"))
    assert any(
        row["artifact_kind"] == "d2l_observation_journal_recovery"
        for row in index["artifacts"]
    )


def test_runner_death_kills_actual_stage_writer_before_lease_reopens(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    stage_pid_path = tmp_path / "actual_stage.pid"
    stage_script = tmp_path / "long_stage.py"
    stage_script.write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path
            import sys
            import time

            pid_path = Path(sys.argv[1])
            pid_path.write_text(str(os.getpid()), encoding="ascii")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    raw_plan = _plan(attempt_id=1)
    raw_plan["stages"][0]["command"] = [
        sys.executable,
        str(stage_script),
        str(stage_pid_path),
    ]
    plan_path = tmp_path / "runner_plan.json"
    plan_path.write_text(
        json.dumps(raw_plan, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    runner = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pipeline.prepass.d2l_translation_component_runner_v1",
            "--plan",
            str(plan_path),
            "--component-root",
            str(root),
        ],
        cwd=TOOL_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stage_pid: int | None = None
    try:
        _wait_until(stage_pid_path.is_file, timeout=30)
        stage_pid = int(stage_pid_path.read_text(encoding="ascii"))
        _wait_until(lambda: component_writer_is_active(root))
        _wait_until(lambda: stage_writer_is_active(root))
        assert runner.poll() is None
        assert _pid_is_alive(stage_pid)

        _kill_process_only(runner)

        assert runner.poll() is not None
        _wait_until(lambda: not _pid_is_alive(stage_pid))
        _wait_until(lambda: not component_writer_is_active(root))
        _wait_until(lambda: not stage_writer_is_active(root))
        with D2LComponentWriterLease(root):
            assert component_writer_is_active(root) is True
    finally:
        if runner.poll() is None:
            _kill_process_tree(runner.pid)
            runner.wait(timeout=10)
        if stage_pid is not None and _pid_is_alive(stage_pid):
            _kill_process_tree(stage_pid)


def test_runner_preserves_unpublished_stage_output_before_retry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    marker = tmp_path / "failed_once"
    output_path = root / "artifacts/b2/retry_output.json"
    script = tmp_path / "write_output_then_fail_once.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            marker = Path(sys.argv[1])
            output = Path(sys.argv[2])
            output.parent.mkdir(parents=True, exist_ok=True)
            if not marker.exists():
                marker.write_text("failed", encoding="utf-8")
                output.write_text(
                    json.dumps({"state": "unpublished"}) + "\\n",
                    encoding="utf-8",
                )
                raise SystemExit(11)
            output.write_text(
                json.dumps({"state": "recovered"}) + "\\n",
                encoding="utf-8",
            )
            """
        ),
        encoding="utf-8",
    )
    raw_plan = _plan(attempt_id=2)
    raw_plan["stages"][3]["artifact_specs"] = [
        _artifact_spec(
            "art_b2_retry_output",
            "d2l_test_output",
            "d2l_test_output_v1",
            "artifacts/b2/retry_output.json",
        )
    ]
    raw_plan["stages"][3]["command"] = [
        sys.executable,
        str(script),
        str(marker),
        str(output_path),
    ]
    plan = ComponentPlan.from_mapping(raw_plan)

    paused = D2LTranslationComponentRunner(plan, root).run()
    assert paused["terminal_event"] is None
    assert output_path.is_file()

    completed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "state": "recovered"
    }
    receipt_path = (
        root
        / "runtime/unpublished_outputs/a0001"
        / "b2_admission_translation/receipt.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["recovered_outputs"][0]["artifact_ref"] == (
        "art_b2_retry_output"
    )
    quarantined = root / receipt["recovered_outputs"][0][
        "quarantined_relative_path"
    ]
    assert json.loads(quarantined.read_text(encoding="utf-8")) == {
        "state": "unpublished"
    }
