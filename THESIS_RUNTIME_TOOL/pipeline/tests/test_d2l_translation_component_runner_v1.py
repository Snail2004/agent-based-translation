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
    D2LTranslationComponentEventWriter,
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
    preflight_resume_runtime_revision_from_plan_file,
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
    prepared_attempts: list[int] = []

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

    resumed = D2LTranslationComponentRunner(
        plan,
        root,
        resume_attempt_preparer=prepared_attempts.append,
    ).run(resume=True)
    assert resumed["terminal_event"] == "run_done"
    assert resumed["component_attempt_id"] == 2
    assert prepared_attempts == [2]
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row["event"] == "run_resumed" for row in events) == 1
    assert [row["component_attempt_id"] for row in events if row["event"] == "run_resumed"] == [2]


def test_resume_attempt_preparer_fails_before_component_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
    ).run()
    paths = [
        root / "component_manifest.json",
        root / "artifact_index.json",
        root / "events.jsonl",
    ]
    before = {path: path.read_bytes() for path in paths}

    def reject(attempt: int) -> None:
        assert attempt == 2
        assert {path: path.read_bytes() for path in paths} == before
        raise RuntimeError("transport attempt preparation rejected")

    with pytest.raises(
        RuntimeError,
        match="transport attempt preparation rejected",
    ):
        D2LTranslationComponentRunner(
            plan,
            root,
            resume_attempt_preparer=reject,
        ).run(resume=True)

    assert {path: path.read_bytes() for path in paths} == before


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


def _write_b1_term_stage_script(tmp_path: Path) -> Path:
    script = tmp_path / "b1_term_stage.py"
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
            )
            from pipeline.prepass.d2l_console_replay_contract_v1 import (
                canonical_sha256,
            )
            from pipeline.prepass.d2l_stage_work_journal_v1 import (
                D2LStageWorkJournal,
            )

            root = Path(sys.argv[1])
            count_path = Path(sys.argv[3])
            candidate_count = int(sys.argv[4])
            work_before_validation = (
                len(sys.argv) > 5
                and sys.argv[5] == "work_before_validation"
            )
            validation_delay_seconds = (
                float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
            )
            manifest = json.loads(
                (root / "component_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            attempt = int(manifest["component_attempt_id"])
            count = (
                int(count_path.read_text(encoding="ascii"))
                if count_path.exists()
                else 0
            )
            count_path.write_text(str(count + 1), encoding="ascii")
            stage_id = "b1_candidate_discovery"
            producer = stage_id
            stage_work_id = "work_b1_candidate_discovery"
            item_id = "b1_window_0001"
            observation_writer = D2LStageObservationJournalWriter(
                path=root / "runtime/component_observations.jsonl",
                workflow_run_id=manifest["workflow_run_id"],
                component_run_id=manifest["component_run_id"],
                component_attempt_id=attempt,
                stage_id=stage_id,
                producer=producer,
                work_id=stage_work_id,
            )
            def append_validation():
                observation_writer.append(
                    {
                        "event": "validation_passed",
                        "agent": producer,
                        "severity": "info",
                        "ts": "2026-07-25T00:00:02Z",
                        "payload": {
                            "validator_id": "d2l_candidate_discovery_validator_v2",
                            "subject_ref": item_id,
                            "reason_codes": ["exact_local_validation"],
                            "retryable": False,
                        },
                    }
                )

            if not work_before_validation:
                append_validation()
            work_journal = D2LStageWorkJournal(
                path=(
                    root
                    / "runtime/work_items/b1_candidate_discovery.jsonl"
                ),
                workflow_run_id=manifest["workflow_run_id"],
                component_run_id=manifest["component_run_id"],
                component_attempt_id=attempt,
                stage_id=stage_id,
            )
            result = {
                "candidate_observations": [
                    {
                        "source_surface": f"technical term {index:04d}",
                        "anchor_block_ids": [f"block_{index:04d}"],
                    }
                    for index in range(candidate_count)
                ],
                "chapter_id": "d2l_selected_campaign_scope_v1",
                "window_id": "window_0001",
            }
            work_journal.append(
                work_item_id=item_id,
                work_contract_id="candidate_contract_v1",
                input_sha256=canonical_sha256({"window": "window_0001"}),
                result=result,
            )
            if work_before_validation:
                time.sleep(validation_delay_seconds)
                append_validation()
            observation_writer.append(
                {
                    "event": "work_progress",
                    "agent": producer,
                    "severity": "info",
                    "ts": "2026-07-25T00:00:03Z",
                    "payload": {
                        "work_kind": "windows",
                        "work_id": stage_work_id,
                        "progress": {
                            "completed": 1,
                            "total": 1,
                            "unit": "windows",
                        },
                    },
                }
            )
            """
        ),
        encoding="utf-8",
    )
    return script


def test_live_term_projection_waits_for_matching_validation_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    call_count = tmp_path / "b1_calls.txt"
    script = _write_b1_term_stage_script(tmp_path)
    raw_plan = _plan(attempt_id=1)
    raw_plan["stages"][1]["command"] = [
        sys.executable,
        str(script),
        str(root),
        str(TOOL_ROOT),
        str(call_count),
        "2",
        "work_before_validation",
        "0.30",
    ]
    raw_plan["stages"][1]["total"] = 1
    plan = ComponentPlan.from_mapping(raw_plan)
    original_project = (
        D2LTranslationComponentRunner._project_term_work_entry
    )
    deferred = {"count": 0}

    def track_deferred(
        runner: D2LTranslationComponentRunner,
        **kwargs: object,
    ) -> bool:
        projected = original_project(runner, **kwargs)
        if not projected:
            deferred["count"] += 1
        return projected

    monkeypatch.setattr(
        D2LTranslationComponentRunner,
        "_project_term_work_entry",
        track_deferred,
    )

    paused = D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="b1_candidate_discovery",
    ).run()

    assert paused["terminal_event"] is None
    assert deferred["count"] >= 1
    assert call_count.read_text(encoding="ascii") == "1"
    counts = _event_counts(root)
    assert counts["term_lifecycle"] == 1
    assert counts["validation_failed"] == 0
    assert counts["run_failed"] == 0
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert sum(
        row["event"] == "validation_passed"
        and row["stage_id"] == "b1_candidate_discovery"
        and row["payload"]["subject_ref"] == "b1_window_0001"
        for row in events
    ) == 1
    manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "paused"
    assert manifest["stages"][1]["status"] == "succeeded"


def test_failure_closure_drains_durable_usage_before_terminal_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    raw_plan = _plan(attempt_id=1)
    script = _write_streaming_stage_script(tmp_path)
    preflight = raw_plan["stages"][0]
    preflight["command"] = [
        sys.executable,
        str(script),
        str(root),
        str(TOOL_ROOT),
    ]
    preflight["cwd"] = str(TOOL_ROOT)
    preflight["total"] = 2
    preflight["receipt_ref"] = "artifacts/preflight_receipt.json"
    preflight["artifact_specs"] = [
        _artifact_spec(
            "art_preflight_receipt",
            "d2l_stage_event_receipt",
            STAGE_RECEIPT_SCHEMA,
            "artifacts/preflight_receipt.json",
        )
    ]
    plan = ComponentPlan.from_mapping(raw_plan)
    original_drain = (
        D2LTranslationComponentRunner._drain_term_work_journal
    )
    injected = {"value": False}

    def fail_after_usage(
        runner: D2LTranslationComponentRunner,
        stage: object,
        **kwargs: object,
    ) -> None:
        if (
            getattr(stage, "stage_id", None) == "preflight"
            and runner._journal_cursor >= 4
            and not injected["value"]
        ):
            injected["value"] = True
            raise ComponentRunnerError("primary live projection failure")
        original_drain(runner, stage, **kwargs)

    monkeypatch.setattr(
        D2LTranslationComponentRunner,
        "_drain_term_work_journal",
        fail_after_usage,
    )

    with pytest.raises(
        ComponentRunnerError,
        match="primary live projection failure",
    ):
        D2LTranslationComponentRunner(plan, root).run()

    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    usage_events = [
        row for row in events if row["event"] == "usage_snapshot"
    ]
    assert [row["payload"]["snapshot_kind"] for row in usage_events] == [
        "accepted_result",
        "component_final",
    ]
    assert events[-1]["event"] == "run_failed"
    assert events[-1]["payload"]["message"] == (
        "primary live projection failure"
    )
    validated = validate_translation_component_package(root)
    assert validated["terminal_event"] == "run_failed"
    assert validated["component_usage"]["accepted_result_count"] == 1


def test_runner_preserves_primary_error_when_failure_closure_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    runner = D2LTranslationComponentRunner(
        ComponentPlan.from_mapping(_plan(attempt_id=1)),
        root,
    )

    def fail_execution() -> None:
        raise ComponentRunnerError("primary execution failure")

    def fail_closure(_exc: Exception) -> None:
        raise ComponentRunnerError("secondary closure failure")

    monkeypatch.setattr(runner, "_execute_remaining_stages", fail_execution)
    monkeypatch.setattr(runner, "_fail", fail_closure)

    with pytest.raises(
        ComponentRunnerError,
        match="primary execution failure",
    ) as caught:
        runner.run()
    assert isinstance(caught.value.__cause__, ComponentRunnerError)
    assert str(caught.value.__cause__) == "secondary closure failure"


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


def test_runner_chains_attempt_five_from_indexed_repair_receipt(
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
    repair_target.write_text("VALUE = 0\n", encoding="utf-8")
    git("add", repair_relative)
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")

    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    raw_plan = _plan(attempt_id=1)
    raw_plan["code_revision"] = baseline
    plan = ComponentPlan.from_mapping(raw_plan)
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
        repair_code_root=code_root,
    ).run()

    def commit_value(value: int) -> str:
        repair_target.write_text(f"VALUE = {value}\n", encoding="utf-8")
        git("add", repair_relative)
        git("commit", "-m", f"repair {value}")
        return git("rev-parse", "HEAD")

    first_effective = commit_value(1)
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="b2_admission_translation",
        repair_code_root=code_root,
        repair_reason="first_runtime_fix",
    ).run(resume=True)

    # The already indexed receipt is intentionally reused while the runtime
    # remains on the same effective revision; no second receipt is minted.
    for stage_id in (
        "auditor_morphology",
        "auditor_target_collision",
        "auditor_multi_target",
    ):
        D2LTranslationComponentRunner(
            plan,
            root,
            stop_after_stage=stage_id,
            repair_code_root=code_root,
        ).run(resume=True)

    manifest_before = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_before["component_attempt_id"] == 5
    checkpoint_ref = manifest_before["resume"]["checkpoint_ref"]
    checkpoint_before = (root / checkpoint_ref).read_bytes()
    events_before = (root / "events.jsonl").read_bytes()
    index_before = json.loads(
        (root / "artifact_index.json").read_text(encoding="utf-8")
    )
    assert sum(
        row["artifact_kind"] == "d2l_component_repair_receipt"
        for row in index_before["artifacts"]
    ) == 1
    assert any(
        json.loads(line)["event"] in {"request_sent", "response_received"}
        for line in events_before.splitlines()
    ) is False

    second_effective = commit_value(2)
    orphan_chain_path = (
        root / "runtime/repair_receipts/repair_chain_a0006.json"
    )
    orphan_chain_path.write_text("{}\n", encoding="utf-8")
    package_before_collision = {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }
    with pytest.raises(
        ComponentRunnerError,
        match="unindexed chained repair receipt path collision",
    ):
        D2LTranslationComponentRunner(
            plan,
            root,
            repair_code_root=code_root,
            repair_reason="chain_runtime_infrastructure_sync",
        ).run(resume=True)
    assert package_before_collision == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }
    orphan_chain_path.unlink()
    paused_after_chain = D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="translation_quality_audit",
        repair_code_root=code_root,
        repair_reason="chain_runtime_infrastructure_sync",
    ).run(resume=True)

    assert paused_after_chain["terminal_event"] is None
    assert paused_after_chain["component_attempt_id"] == 6
    assert first_effective != second_effective
    assert (root / checkpoint_ref).read_bytes() == checkpoint_before
    assert (root / "events.jsonl").read_bytes().startswith(events_before)
    chain_path = root / "runtime/repair_receipts/repair_chain_a0006.json"
    assert chain_path.is_file()
    chain = json.loads(chain_path.read_text(encoding="utf-8"))
    assert chain["previous_component_attempt_id"] == 5
    assert chain["baseline_code_revision"] == first_effective
    assert chain["effective_code_revision"] == second_effective
    assert chain["parent_repair_artifact_ref"] == "art_component_repair_a0002"
    index = json.loads((root / "artifact_index.json").read_text(encoding="utf-8"))
    matching = [
        row
        for row in index["artifacts"]
        if row["artifact_ref"] == "art_component_repair_chain_a0006"
    ]
    assert len(matching) == 1
    assert matching[0]["sha256"] == file_sha256(chain_path)
    assert matching[0]["parent_artifact_refs"] == ["art_component_repair_a0002"]


def test_resume_revision_preflight_accepts_reviewed_app_delta_without_mutation(
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
    app_paths = [
        "THESIS_RUNTIME_TOOL/app/prototype/app.jsx",
        "THESIS_RUNTIME_TOOL/app/prototype/console.jsx",
        (
            "THESIS_RUNTIME_TOOL/app/prototype/"
            "workflow_live_progress_usage.test.cjs"
        ),
    ]
    for relative in app_paths:
        target = code_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("BASELINE = true\n", encoding="utf-8")
    git("add", *app_paths)
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")

    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    raw_plan = _plan(attempt_id=1)
    raw_plan["code_revision"] = baseline
    plan_path = tmp_path / "component_plan.json"
    plan_path.write_text(
        json.dumps(raw_plan, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    D2LTranslationComponentRunner(
        ComponentPlan.from_mapping(raw_plan),
        root,
        stop_after_stage="candidate_index",
        repair_code_root=code_root,
    ).run()

    for relative in app_paths:
        (code_root / relative).write_text("UPDATED = true\n", encoding="utf-8")
    git("add", *app_paths)
    git("commit", "-m", "reviewed app changes")
    effective = git("rev-parse", "HEAD")
    before = {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }

    preflight = preflight_resume_runtime_revision_from_plan_file(
        plan_path,
        root,
        repair_code_root=code_root,
        repair_reason="journal_publication_race_recovery",
    )

    assert preflight["mode"] == "direct"
    assert preflight["sealed_code_revision"] == baseline
    assert preflight["baseline_code_revision"] == baseline
    assert preflight["effective_code_revision"] == effective
    assert preflight["changed_paths"] == sorted(app_paths)
    assert len(preflight["preflight_sha256"]) == 64
    assert before == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }

    prompt = code_root / "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text('PROMPT = "changed"\n', encoding="utf-8")
    git("add", "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py")
    git("commit", "-m", "semantic drift")
    with pytest.raises(ComponentRunnerError, match="closed mechanical scope"):
        preflight_resume_runtime_revision_from_plan_file(
            plan_path,
            root,
            repair_code_root=code_root,
            repair_reason="journal_publication_race_recovery",
        )
    assert before == {
        name: (root / name).read_bytes()
        for name in (
            "component_manifest.json",
            "artifact_index.json",
            "events.jsonl",
        )
    }


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


def test_term_lifecycle_repair_resume_backfills_attempt_two_without_b1_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        "d2l_console_replay_contract_v1.py"
    )
    repair_target = code_root / repair_relative
    repair_target.parent.mkdir(parents=True)
    repair_target.write_text("TERM_LIFECYCLE = 1\n", encoding="utf-8")
    git("add", repair_relative)
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")

    root = tmp_path / "component"
    _write_payloads(root, attempt_id=3)
    b1_calls = tmp_path / "b1_calls.txt"
    b1_script = _write_b1_term_stage_script(tmp_path)
    raw_plan = _plan(attempt_id=3)
    raw_plan["code_revision"] = baseline
    raw_plan["stages"][1]["command"] = [
        sys.executable,
        str(b1_script),
        str(root),
        str(TOOL_ROOT),
        str(b1_calls),
        "2",
    ]
    raw_plan["stages"][1]["total"] = 1
    plan = ComponentPlan.from_mapping(raw_plan)

    first = D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="preflight",
        repair_code_root=code_root,
    ).run()
    assert first["component_attempt_id"] == 1

    original_drain = (
        D2LTranslationComponentRunner._drain_term_work_journal
    )
    monkeypatch.setattr(
        D2LTranslationComponentRunner,
        "_drain_term_work_journal",
        lambda self, stage, **_kwargs: None,
    )
    second = D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="b1_candidate_discovery",
        repair_code_root=code_root,
    ).run(resume=True)
    monkeypatch.setattr(
        D2LTranslationComponentRunner,
        "_drain_term_work_journal",
        original_drain,
    )
    assert second["component_attempt_id"] == 2
    assert b1_calls.read_text(encoding="ascii") == "1"
    assert _event_counts(root)["term_lifecycle"] == 0
    paused_manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    checkpoint_path = root / paused_manifest["resume"]["checkpoint_ref"]
    checkpoint_before = checkpoint_path.read_bytes()
    checkpoint_sha = file_sha256(checkpoint_path)

    repair_target.write_text("TERM_LIFECYCLE = 2\n", encoding="utf-8")
    git("add", repair_relative)
    git("commit", "-m", "add term lifecycle projection")

    completed = D2LTranslationComponentRunner(
        plan,
        root,
        repair_code_root=code_root,
        repair_reason="add_term_lifecycle_observability",
    ).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 3
    assert b1_calls.read_text(encoding="ascii") == "1"
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert file_sha256(checkpoint_path) == checkpoint_sha

    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    term_events = [
        row for row in events if row["event"] == "term_lifecycle"
    ]
    assert term_events
    assert {
        row["payload"]["projection_mode"] for row in term_events
    } == {"resume_backfill"}
    assert {
        row["payload"]["origin_component_attempt_id"]
        for row in term_events
    } == {2}
    attempt_three = [
        row for row in events if row["component_attempt_id"] == 3
    ]
    resumed_index = next(
        index
        for index, row in enumerate(attempt_three)
        if row["event"] == "run_resumed"
    )
    backfill_index = next(
        index
        for index, row in enumerate(attempt_three)
        if row["event"] == "term_lifecycle"
    )
    next_stage_index = next(
        index
        for index, row in enumerate(attempt_three)
        if row["event"] == "stage_start"
    )
    assert resumed_index < backfill_index < next_stage_index
    assert not any(
        row["event"] == "request_sent"
        and row["stage_id"] == "b1_candidate_discovery"
        and row["component_attempt_id"] == 3
        for row in events
    )
    receipt = json.loads(
        (
            root / "runtime/repair_receipts/repair_a0003.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["changed_paths"] == [repair_relative]
    validated = validate_translation_component_package(root)
    assert validated["term_lifecycle_row_count"] == 2
    assert validated["term_lifecycle_batch_count"] == 1


def test_term_lifecycle_partial_backfill_crash_resumes_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=4)
    b1_calls = tmp_path / "b1_calls.txt"
    b1_script = _write_b1_term_stage_script(tmp_path)
    raw_plan = _plan(attempt_id=4)
    raw_plan["stages"][1]["command"] = [
        sys.executable,
        str(b1_script),
        str(root),
        str(TOOL_ROOT),
        str(b1_calls),
        "200",
    ]
    raw_plan["stages"][1]["total"] = 1
    plan = ComponentPlan.from_mapping(raw_plan)

    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="preflight",
    ).run()
    original_drain = (
        D2LTranslationComponentRunner._drain_term_work_journal
    )
    monkeypatch.setattr(
        D2LTranslationComponentRunner,
        "_drain_term_work_journal",
        lambda self, stage, **_kwargs: None,
    )
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="b1_candidate_discovery",
    ).run(resume=True)
    monkeypatch.setattr(
        D2LTranslationComponentRunner,
        "_drain_term_work_journal",
        original_drain,
    )
    assert b1_calls.read_text(encoding="ascii") == "1"

    original_emit = D2LTranslationComponentEventWriter.emit
    crashed = {"value": False}

    def emit_then_crash(
        writer: D2LTranslationComponentEventWriter,
        event: str,
        **kwargs: object,
    ) -> dict[str, object]:
        result = original_emit(writer, event, **kwargs)
        if event == "term_lifecycle" and not crashed["value"]:
            crashed["value"] = True
            raise KeyboardInterrupt("synthetic crash after durable batch")
        return result

    monkeypatch.setattr(
        D2LTranslationComponentEventWriter,
        "emit",
        emit_then_crash,
    )
    with pytest.raises(
        KeyboardInterrupt,
        match="synthetic crash after durable batch",
    ):
        D2LTranslationComponentRunner(plan, root).run(resume=True)
    monkeypatch.setattr(
        D2LTranslationComponentEventWriter,
        "emit",
        original_emit,
    )

    crashed_manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert crashed_manifest["status"] == "running"
    assert crashed_manifest["component_attempt_id"] == 3
    assert _event_counts(root)["term_lifecycle"] == 1

    completed = D2LTranslationComponentRunner(
        plan,
        root,
        recover_stale=True,
    ).run(resume=True)
    assert completed["terminal_event"] == "run_done"
    assert completed["component_attempt_id"] == 4
    assert b1_calls.read_text(encoding="ascii") == "1"
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    term_events = [
        row for row in events if row["event"] == "term_lifecycle"
    ]
    assert len(term_events) >= 2
    batch_ids = [row["payload"]["batch_id"] for row in term_events]
    row_ids = [
        term_row["row_id"]
        for row in term_events
        for term_row in row["payload"]["rows"]
    ]
    assert len(batch_ids) == len(set(batch_ids))
    assert len(row_ids) == len(set(row_ids)) == 200
    assert {
        row["payload"]["origin_component_attempt_id"]
        for row in term_events
    } == {2}
    assert {
        row["component_attempt_id"] for row in term_events
    } == {3, 4}
    assert any(
        row["event"] == "checkpoint"
        and row["component_attempt_id"] == 3
        and row["payload"]["paused_reason"] == "stale_process_recovered"
        for row in events
    )
    validated = validate_translation_component_package(root)
    assert validated["term_lifecycle_row_count"] == 200
    assert validated["term_lifecycle_batch_count"] == len(term_events)


def test_term_lifecycle_package_rejects_foreign_work_journal_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    b1_calls = tmp_path / "b1_calls.txt"
    b1_script = _write_b1_term_stage_script(tmp_path)
    raw_plan = _plan(attempt_id=1)
    raw_plan["stages"][1]["command"] = [
        sys.executable,
        str(b1_script),
        str(root),
        str(TOOL_ROOT),
        str(b1_calls),
        "1",
    ]
    raw_plan["stages"][1]["total"] = 1
    plan = ComponentPlan.from_mapping(raw_plan)

    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="b1_candidate_discovery",
    ).run()
    assert validate_translation_component_package(
        root,
        require_terminal=False,
    )["term_lifecycle_row_count"] == 1

    journal_path = (
        root / "runtime/work_items/b1_candidate_discovery.jsonl"
    )
    entry = json.loads(journal_path.read_text(encoding="utf-8"))
    entry["workflow_run_id"] = "wf_foreign_term_evidence"
    unsigned = dict(entry)
    unsigned.pop("entry_sha256")
    entry["entry_sha256"] = canonical_sha256(unsigned)
    journal_path.write_text(
        json.dumps(entry, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="term lifecycle work-journal evidence drift",
    ):
        validate_translation_component_package(
            root,
            require_terminal=False,
        )
