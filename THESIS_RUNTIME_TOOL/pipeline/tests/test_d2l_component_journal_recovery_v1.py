from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest

from pipeline.prepass import d2l_component_journal_recovery_v1 as recovery
from pipeline.prepass.d2l_component_journal_recovery_v1 import (
    D2LComponentJournalRecoveryError,
    D2LComponentJournalRecoveryRequestV1,
    RECOVERY_REASON,
    REQUEST_SCHEMA,
    recover_d2l_component_journal_v1,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    D2LStageObservationJournalWriter,
    read_observation_journal,
)
from pipeline.prepass.d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
)
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    build_component_usage_snapshot,
    build_stage_plan,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    validate_translation_component_package,
)
from pipeline.prepass.d2l_stage_work_journal_v1 import (
    D2LStageWorkJournal,
    read_work_journal,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    D2LTranslationComponentRunner,
    RUNNER_SCHEMA,
)
from pipeline.workflow_replay.adapters_v1 import (
    D2LTranslationComponentAdapterV1,
)
from pipeline.workflow_replay.relay_v1 import (
    StageDefinitionV1,
    WorkflowRelayV1,
)


TOOL_ROOT = Path(__file__).resolve().parents[2]
GIT_COMMIT = "1" * 40
CONFIG_SHA = "2" * 64
WORKFLOW_ID = "wf_journal_recovery_v1"
COMPONENT_RUN_ID = "tr_journal_recovery_v1"
STAGE_ID = "b1_candidate_discovery"
CALL_COUNT = 6
TOKENS_PER_CALL = 12


def _binding(ref: str, kind: str, schema: str) -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": sha256(ref.encode("utf-8")).hexdigest().upper(),
        "sha256_kind": "physical",
    }


def _source_binding() -> dict[str, object]:
    return {
        "schema": "canonical_source_binding_v1",
        "document": _binding(
            "src_document",
            "source_document",
            "document_v1",
        ),
        "structure_manifest": _binding(
            "src_structure",
            "structure_manifest",
            "structure_manifest_v1",
        ),
        "asset_manifest": _binding(
            "src_assets",
            "asset_manifest",
            "asset_manifest_v1",
        ),
        "admitted_projection": _binding(
            "src_projection",
            "admitted_projection",
            "admitted_projection_v1",
        ),
        "normalization_receipt": _binding(
            "src_receipt",
            "normalization_receipt",
            "normalization_receipt_v1",
        ),
        "package_seal": _binding(
            "src_package_seal",
            "source_package_seal",
            "source_package_seal_v1",
        ),
    }


def _plan() -> ComponentPlan:
    stage_rows = build_stage_plan()
    units = {
        row["stage_id"]: row["progress"]["unit"]
        for row in stage_rows
    }
    stages = []
    for stage_id in STAGE_IDS:
        stages.append(
            {
                "stage_id": stage_id,
                "producer": stage_id,
                "command": [sys.executable, "-c", "pass"],
                "cwd": None,
                "artifact_specs": [],
                "total": CALL_COUNT if stage_id == STAGE_ID else 1,
                "unit": units[stage_id],
                "work_id": f"work_{stage_id}",
                "mode": "execute",
                "timeout_seconds": 30,
                "receipt_ref": None,
            }
        )
    return ComponentPlan.from_mapping(
        {
            "schema": RUNNER_SCHEMA,
            "workflow_run_id": WORKFLOW_ID,
            "component_run_id": COMPONENT_RUN_ID,
            "pipeline_id": "d2l_terminology",
            "pipeline_version": (
                "d2l_translation_component_runner_v1"
            ),
            "source_binding": _source_binding(),
            "config_sha256": CONFIG_SHA,
            "code_revision": GIT_COMMIT,
            "selected_chapter_ids": ["d2l_preliminaries"],
            "stages": stages,
            "scoring_handoff_fragment_ref": (
                "scoring_handoff_fragment.json"
            ),
        }
    )


def _parent_source_bindings() -> list[dict[str, object]]:
    source = _source_binding()
    return [
        {"role": role, "binding": source[role]}
        for role in (
            "document",
            "structure_manifest",
            "asset_manifest",
            "admitted_projection",
            "normalization_receipt",
            "package_seal",
        )
    ]


def _parent_stages(
    plan: ComponentPlan,
) -> tuple[StageDefinitionV1, ...]:
    labels = {
        row["stage_id"]: row["label"]
        for row in build_stage_plan()
    }
    return tuple(
        StageDefinitionV1(
            stage_id=f"translation.{stage.stage_id}",
            component_id="translation",
            local_stage_id=stage.stage_id,
            order=index,
            label=labels[stage.stage_id],
            producer=stage.producer,
        )
        for index, stage in enumerate(plan.stages, start=1)
    )


def _append_observation(
    writer: D2LStageObservationJournalWriter,
    *,
    event: str,
    call_index: int,
    payload: dict[str, object],
) -> None:
    writer.append(
        {
            "event": event,
            "agent": STAGE_ID,
            "severity": "info",
            "ts": f"2026-07-25T00:00:{call_index:02d}Z",
            "payload": payload,
        }
    )


def _usage(call_index: int) -> dict[str, object]:
    logical_request_id = f"logical_request_{call_index:04d}"
    return {
        "logical_request_id": logical_request_id,
        "physical_attempt_index": 1,
        "provider_id": "shopapi",
        "model_id": "gemini-3.5-flash",
        "source_id": "shopaikey_gemini_proxy_v1",
        "masked_quota_bucket": "shopapi-***",
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": TOKENS_PER_CALL,
        "latency_ms": 5,
        "finish_reason": "stop",
        "cost_usd": None,
        "currency": None,
        "cost_status": "unknown",
        "cache_status": "miss",
        "cache_mechanism": "local_exact_cache",
    }


def _append_call_head(
    *,
    root: Path,
    writer: D2LStageObservationJournalWriter,
    call_index: int,
) -> tuple[str, str]:
    logical_request_id = f"logical_request_{call_index:04d}"
    work_item_id = f"b1_window_{call_index:04d}"
    usage = _usage(call_index)
    _append_observation(
        writer,
        event="request_sent",
        call_index=call_index,
        payload={
            "logical_request_id": logical_request_id,
            "physical_attempt_index": 1,
            "work_kind": "windows",
            "work_id": work_item_id,
            "provider_id": usage["provider_id"],
            "model_id": usage["model_id"],
            "source_id": usage["source_id"],
            "masked_quota_bucket": usage["masked_quota_bucket"],
        },
    )
    _append_observation(
        writer,
        event="response_received",
        call_index=call_index,
        payload={"usage": usage},
    )
    prior_snapshots = [
        dict(entry["observation"]["payload"])
        for entry in read_observation_journal(
            root / "runtime/component_observations.jsonl"
        )
        if entry["observation"]["event"] == "usage_snapshot"
    ]
    snapshot = build_component_usage_snapshot(
        previous_snapshots=prior_snapshots,
        workflow_run_id=WORKFLOW_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=1,
        stage_id=STAGE_ID,
        work_id=work_item_id,
        accepted_usage={
            "identity_kind": "provider_attempt",
            "attempt_usage_id": f"attempt_usage_{call_index:04d}",
            "cache_observation_id": (
                f"cache_observation_{call_index:04d}"
            ),
            "logical_request_id": logical_request_id,
            "semantic_attempt_index": 1,
            "transport_retry_ordinal": 0,
            "physical_attempt_index": 1,
            "provider_called": True,
            "source_revision": "shopapi_v1",
            "usage": usage,
        },
    )
    _append_observation(
        writer,
        event="usage_snapshot",
        call_index=call_index,
        payload=snapshot,
    )
    return logical_request_id, work_item_id


def _append_call_tail(
    *,
    writer: D2LStageObservationJournalWriter,
    work_journal: D2LStageWorkJournal,
    call_index: int,
    work_item_id: str,
) -> None:
    work_journal.append(
        work_item_id=work_item_id,
        work_contract_id="candidate_contract_v1",
        input_sha256=canonical_sha256(
            {"window_id": work_item_id}
        ),
        result={
            "candidate_observations": [
                {
                    "source_surface": f"technical term {call_index:04d}",
                    "anchor_block_ids": [f"block_{call_index:04d}"],
                }
            ],
            "chapter_id": "d2l_preliminaries",
            "window_id": work_item_id,
        },
    )
    _append_observation(
        writer,
        event="validation_passed",
        call_index=call_index,
        payload={
            "validator_id": "d2l_candidate_discovery_validator_v2",
            "subject_ref": work_item_id,
            "reason_codes": ["exact_local_validation"],
            "retryable": False,
        },
    )
    _append_observation(
        writer,
        event="work_progress",
        call_index=call_index,
        payload={
            "work_kind": "windows",
            "work_id": f"work_{STAGE_ID}",
            "progress": {
                "completed": call_index,
                "total": CALL_COUNT,
                "unit": "windows",
            },
        },
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "component"
    plan = _plan()
    runner = D2LTranslationComponentRunner(plan, root)
    runner._start_new()
    preflight = plan.stages[0]
    runner._start_stage(preflight)
    runner.writer.emit(
        "validation_passed",
        stage_id=preflight.stage_id,
        agent=preflight.producer,
        payload={
            "validator_id": "d2l_component_artifact_validator_v1",
            "subject_ref": preflight.stage_id,
            "reason_codes": ["stage_artifacts_valid"],
            "retryable": False,
        },
    )
    runner._finish_stage(preflight, "succeeded")
    stage = next(
        item for item in plan.stages if item.stage_id == STAGE_ID
    )
    runner._start_stage(stage)
    observation_writer = D2LStageObservationJournalWriter(
        path=root / "runtime/component_observations.jsonl",
        workflow_run_id=WORKFLOW_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=1,
        stage_id=STAGE_ID,
        producer=stage.producer,
        work_id=stage.work_id,
    )
    work_journal = D2LStageWorkJournal(
        path=root / f"runtime/work_items/{STAGE_ID}.jsonl",
        workflow_run_id=WORKFLOW_ID,
        component_run_id=COMPONENT_RUN_ID,
        component_attempt_id=1,
        stage_id=STAGE_ID,
    )
    for call_index in range(1, CALL_COUNT):
        _, work_item_id = _append_call_head(
            root=root,
            writer=observation_writer,
            call_index=call_index,
        )
        _append_call_tail(
            writer=observation_writer,
            work_journal=work_journal,
            call_index=call_index,
            work_item_id=work_item_id,
        )
        runner._drain_observation_journal(
            stage,
            allow_incomplete_tail=False,
        )
        runner._drain_term_work_journal(
            stage,
            projection_mode="live",
        )

    trusted_validation = validate_translation_component_package(
        root,
        require_terminal=False,
    )
    trusted_event_lines = (root / "events.jsonl").read_bytes().splitlines(
        keepends=True
    )
    trusted_observation_count = len(
        read_observation_journal(
            root / "runtime/component_observations.jsonl"
        )
    )
    trusted_work_count = len(
        read_work_journal(
            root / f"runtime/work_items/{STAGE_ID}.jsonl"
        )
    )

    relay = WorkflowRelayV1(
        tmp_path / "parent",
        workflow_run_id=WORKFLOW_ID,
        job_id="job_journal_recovery_v1",
        source_package_bindings=_parent_source_bindings(),
        stages=_parent_stages(plan),
        code_commit=GIT_COMMIT,
        created_at="2026-07-25T00:10:00Z",
        clock=lambda: "2026-07-25T00:10:01Z",
    )
    relay.ingest_component(
        root,
        adapter=D2LTranslationComponentAdapterV1(
            require_terminal=False
        ),
    )
    import_path = next((relay.root / "relay_imports").glob("*.json"))
    import_record = json.loads(
        import_path.read_text(encoding="utf-8")
    )
    snapshot_root = (
        relay.root
        / "components"
        / "translation"
        / COMPONENT_RUN_ID
        / "snapshots"
        / import_record["snapshot_sha256"]
    )

    _, sixth_work_item = _append_call_head(
        root=root,
        writer=observation_writer,
        call_index=CALL_COUNT,
    )
    runner._drain_observation_journal(
        stage,
        allow_incomplete_tail=False,
    )
    _append_call_tail(
        writer=observation_writer,
        work_journal=work_journal,
        call_index=CALL_COUNT,
        work_item_id=sixth_work_item,
    )

    row = runner._stage_row(STAGE_ID)
    row["status"] = "failed"
    row["ended_at"] = "2026-07-25T00:11:00Z"
    row["current_work_id"] = None
    runner.writer.emit(
        "validation_failed",
        stage_id=STAGE_ID,
        agent="d2l_component_runner",
        severity="error",
        payload={
            "validator_id": "d2l_component_stage_execution_v1",
            "subject_ref": STAGE_ID,
            "reason_codes": [
                "ComponentRunnerError",
                "journal_publication_race",
            ],
            "retryable": False,
        },
    )
    runner.writer.emit(
        "stage_done",
        stage_id=STAGE_ID,
        agent=stage.producer,
        severity="error",
        payload={
            "outcome": "failed",
            "reason_code": "ComponentRunnerError",
            "progress": dict(row["progress"]),
        },
    )
    runner.manifest["status"] = "failed"
    runner.manifest["active_stage_id"] = None
    runner._save_manifest()

    plan_path = tmp_path / "component_plan.json"
    plan_path.write_bytes(canonical_json_bytes(plan.canonical_mapping()))
    active_event_lines = (root / "events.jsonl").read_bytes().splitlines(
        keepends=True
    )
    request = D2LComponentJournalRecoveryRequestV1(
        component_root=root,
        transaction_root=tmp_path / "transactions",
        relay_import_file=import_path,
        relay_import_physical_sha256=file_sha256(import_path),
        relay_import_sha256=import_record["import_sha256"],
        relay_import_ordinal=import_record["acceptance_ordinal"],
        snapshot_root=snapshot_root,
        snapshot_sha256=import_record["snapshot_sha256"],
        component_plan_file=plan_path,
        component_plan_physical_sha256=file_sha256(plan_path),
        expected_manifest_sha256=file_sha256(
            root / "component_manifest.json"
        ),
        expected_events_sha256=file_sha256(root / "events.jsonl"),
        expected_artifact_index_sha256=file_sha256(
            root / "artifact_index.json"
        ),
        expected_observation_journal_sha256=file_sha256(
            root / "runtime/component_observations.jsonl"
        ),
        expected_work_journal_sha256=file_sha256(
            root / f"runtime/work_items/{STAGE_ID}.jsonl"
        ),
        expected_trusted_event_count=len(trusted_event_lines),
        expected_active_event_count=len(active_event_lines),
        expected_trusted_observation_count=trusted_observation_count,
        expected_active_observation_count=len(
            read_observation_journal(
                root / "runtime/component_observations.jsonl"
            )
        ),
        expected_trusted_work_count=trusted_work_count,
        expected_active_work_count=len(
            read_work_journal(
                root / f"runtime/work_items/{STAGE_ID}.jsonl"
            )
        ),
        expected_accepted_result_count=CALL_COUNT,
        expected_total_tokens=CALL_COUNT * TOKENS_PER_CALL,
        stage_id=STAGE_ID,
        recovery_reason=RECOVERY_REASON,
    )
    return {
        "root": root,
        "request": request,
        "trusted_validation": trusted_validation,
        "trusted_event_lines": trusted_event_lines,
        "active_event_lines": active_event_lines,
        "active_tree": recovery._tree_index(root),
    }


def _request_mapping(
    request: D2LComponentJournalRecoveryRequestV1,
) -> dict[str, object]:
    row = asdict(request)
    for key in (
        "component_root",
        "transaction_root",
        "relay_import_file",
        "snapshot_root",
        "component_plan_file",
    ):
        row[key] = str(row[key])
    return {"schema": REQUEST_SCHEMA, **row}


def _event_rows(root: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]


def test_recovers_trusted_prefix_without_provider_replay_and_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = fixture["root"]
    request = fixture["request"]
    assert isinstance(root, Path)
    assert isinstance(request, D2LComponentJournalRecoveryRequestV1)

    receipt = recover_d2l_component_journal_v1(request)

    assert receipt["reconstruction"]["provider_call_count"] == 0
    assert receipt["reconstruction"]["semantic_replay_count"] == 0
    assert receipt["reconstruction"]["accepted_result_count"] == 6
    assert receipt["reconstruction"]["total_tokens"] == 72
    trusted_lines = fixture["trusted_event_lines"]
    active_lines = fixture["active_event_lines"]
    assert isinstance(trusted_lines, list)
    assert isinstance(active_lines, list)
    recovered_lines = (root / "events.jsonl").read_bytes().splitlines(
        keepends=True
    )
    trusted_count = request.expected_trusted_event_count
    assert recovered_lines[:trusted_count] == trusted_lines
    assert recovered_lines[trusted_count : trusted_count + 3] == (
        active_lines[trusted_count : trusted_count + 3]
    )
    recovered_events = _event_rows(root)
    assert not any(
        row["event"] in {"validation_failed", "run_failed"}
        for row in recovered_events[trusted_count:]
    )
    assert sum(
        row["event"] == "validation_passed"
        and row["payload"].get("subject_ref") == "b1_window_0006"
        for row in recovered_events
    ) == 1
    validation = validate_translation_component_package(
        root,
        require_terminal=False,
    )
    assert validation["component_usage"]["accepted_result_count"] == 6
    assert validation["component_usage"]["total_tokens"] == 72
    manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "paused"
    assert manifest["component_attempt_id"] == 1
    assert manifest["resume"]["paused_reason"] == RECOVERY_REASON
    quarantine = (
        request.transaction_root
        / receipt["transaction_id"]
        / receipt["quarantine"]["component_ref"]
    )
    assert (quarantine / "events.jsonl").read_bytes() == b"".join(
        active_lines
    )

    assert recover_d2l_component_journal_v1(request) == receipt

    request_path = tmp_path / "request.json"
    request_path.write_bytes(
        canonical_json_bytes(_request_mapping(request))
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pipeline.scripts."
            "recover_d2l_component_journal_recovery_v1",
            "--request-json",
            str(request_path),
        ],
        cwd=TOOL_ROOT,
        check=True,
        capture_output=True,
    )
    assert json.loads(completed.stdout) == receipt


def test_rejects_active_prefix_drift_before_mutation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = fixture["root"]
    request = fixture["request"]
    assert isinstance(root, Path)
    assert isinstance(request, D2LComponentJournalRecoveryRequestV1)
    events_path = root / "events.jsonl"
    lines = events_path.read_bytes().splitlines(keepends=True)
    first = json.loads(lines[0])
    first["payload"]["drift_probe"] = True
    lines[0] = (
        json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    events_path.write_bytes(b"".join(lines))
    drifted_request = replace(
        request,
        expected_events_sha256=file_sha256(events_path),
    )
    before = recovery._tree_index(root)

    with pytest.raises(
        D2LComponentJournalRecoveryError,
        match="trusted events is not an exact active byte prefix",
    ):
        recover_d2l_component_journal_v1(drifted_request)

    assert recovery._tree_index(root) == before


def test_rejects_active_component_writer_lease_before_mutation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = fixture["root"]
    request = fixture["request"]
    assert isinstance(root, Path)
    assert isinstance(request, D2LComponentJournalRecoveryRequestV1)
    before = recovery._tree_index(root)

    with D2LComponentWriterLease(root):
        with pytest.raises(
            D2LComponentJournalRecoveryError,
            match="writer",
        ):
            recover_d2l_component_journal_v1(request)

    assert recovery._tree_index(root) == before


def test_install_failure_rolls_back_and_retry_commits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    root = fixture["root"]
    request = fixture["request"]
    assert isinstance(root, Path)
    assert isinstance(request, D2LComponentJournalRecoveryRequestV1)
    before = recovery._tree_index(root)
    original_validate = recovery.validate_translation_component_package

    def fail_installed_root(
        package_root: Path,
        *,
        require_terminal: bool = False,
    ) -> dict[str, object]:
        if Path(package_root).resolve() == root.resolve():
            raise RuntimeError("synthetic post-install validation failure")
        return original_validate(
            package_root,
            require_terminal=require_terminal,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            recovery,
            "validate_translation_component_package",
            fail_installed_root,
        )
        with pytest.raises(
            RuntimeError,
            match="synthetic post-install validation failure",
        ):
            recover_d2l_component_journal_v1(request)

    assert recovery._tree_index(root) == before
    receipt = recover_d2l_component_journal_v1(request)
    assert receipt["poststate"]["status"] == "paused"
    assert recover_d2l_component_journal_v1(request) == receipt
    transaction_dir = (
        request.transaction_root / receipt["transaction_id"]
    )
    assert (transaction_dir / "receipt.json").is_file()
    assert len(list(transaction_dir.glob("receipt.json"))) == 1
