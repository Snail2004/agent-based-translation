from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.workflow_replay.contracts_v1 import (
    WorkflowReplayContractError,
    canonical_json_bytes,
    canonical_sha256,
    normalize_d2l_scoring_fragment_v1,
    physical_sha256,
    scoring_input_set_sha256_v1,
    validate_scoring_handoff_v1,
)
from pipeline.workflow_replay.relay_v1 import (
    ComponentArtifactInputV1,
    ComponentEventInputV1,
    ComponentFileInputV1,
    ComponentSnapshotV1,
    StageDefinitionV1,
    WorkflowRelayV1,
    validate_workflow_parent_package_v1,
)


WORKFLOW_ID = "workflow_fixture_v1"
JOB_ID = "job_fixture_v1"
COMMIT = "a" * 40
CREATED_AT = "2026-07-22T00:00:00Z"


class FixtureAdapter:
    def __init__(self, snapshot: ComponentSnapshotV1) -> None:
        self.snapshot = snapshot
        self.validator_id = snapshot.validator_id
        self.validator_revision = snapshot.validator_revision

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1:
        assert component_root.is_dir()
        return self.snapshot


class MutatingSourceAdapter(FixtureAdapter):
    def __init__(
        self,
        snapshot: ComponentSnapshotV1,
        *,
        live_source: Path,
    ) -> None:
        super().__init__(snapshot)
        self.live_source = live_source
        self.captured_root: Path | None = None

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1:
        self.captured_root = component_root
        assert component_root != self.live_source
        snapshot = super().validate_and_snapshot(
            component_root,
            workflow_run_id=workflow_run_id,
        )
        (self.live_source / "artifacts" / "output.json").write_bytes(
            b'{"changed_after_capture":true}'
        )
        return snapshot


class Clock:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self) -> str:
        self.index += 1
        return f"2026-07-22T00:00:{self.index:02d}Z"


def _binding(ref: str, kind: str = "source") -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": "1.0.0",
        "sha256": canonical_sha256({"ref": ref}),
        "sha256_kind": "physical",
    }


def _source_bindings() -> list[dict[str, object]]:
    return [
        {"role": "document", "binding": _binding("source/document.json", "document")},
        {
            "role": "structure_manifest",
            "binding": _binding("source/structure_manifest.json", "structure_manifest"),
        },
        {
            "role": "asset_manifest",
            "binding": _binding("source/asset_manifest.json", "asset_manifest"),
        },
        {
            "role": "admitted_projection",
            "binding": _binding("source/admitted_projection_v1.json", "admitted_projection"),
        },
        {
            "role": "normalization_receipt",
            "binding": _binding("source/normalization_receipt.json", "normalization_receipt"),
        },
        {
            "role": "package_seal",
            "binding": _binding("source/source_lifecycle_v2.json", "source_package_seal"),
        },
    ]


def _stages() -> tuple[StageDefinitionV1, ...]:
    return (
        StageDefinitionV1(
            "translation.translate", "translation", "translate", 1, "Translate", "translator"
        ),
        StageDefinitionV1(
            "evaluation.score", "evaluation", "score", 2, "Score", "sf_qe"
        ),
        StageDefinitionV1(
            "publication.export", "publication", "export", 3, "Export", "publisher"
        ),
    )


def _relay(tmp_path: Path, *, reconstructed: bool = False) -> WorkflowRelayV1:
    clock = Clock()
    return WorkflowRelayV1(
        tmp_path / "workflow",
        workflow_run_id=WORKFLOW_ID,
        job_id=JOB_ID,
        source_package_bindings=_source_bindings(),
        stages=_stages(),
        code_commit=COMMIT,
        reconstructed=reconstructed,
        created_at=CREATED_AT,
        clock=clock,
    )


def _event(
    *,
    component_id: str,
    component_run_id: str,
    seq: int,
    event: str,
    stage_id: str | None,
    attempt: int = 1,
    public_payload: dict[str, object] | None = None,
    source_payload: dict[str, object] | None = None,
) -> ComponentEventInputV1:
    value = {
        "schema": f"{component_id}_component_event_v1",
        "event_id": f"evt_{component_run_id}_{seq:08d}",
        "workflow_run_id": WORKFLOW_ID,
        "flow_kind": f"{component_id}_flow",
        "component_id": component_id,
        "component_run_id": component_run_id,
        "component_attempt_id": attempt,
        "component_seq": seq,
        "ts": f"2026-07-22T00:01:{seq:02d}Z",
        "stage_id": stage_id,
        "agent": "fixture_runner",
        "event": event,
        "severity": "error" if event.endswith("failed") else "info",
        "payload": source_payload if source_payload is not None else (public_payload or {}),
    }
    return ComponentEventInputV1(
        value=value,
        source_bytes=canonical_json_bytes(value),
        public_payload=public_payload or {},
    )


def _write_snapshot(
    root: Path,
    *,
    component_id: str,
    run_id: str,
    local_stage: str,
    events: tuple[ComponentEventInputV1, ...] | None = None,
    status: str = "succeeded",
    attempt: int = 1,
    artifact_bytes: bytes = b'{"ok":true}',
    artifact_ref: str | None = None,
    workflow_run_id: str = WORKFLOW_ID,
) -> ComponentSnapshotV1:
    root.mkdir(parents=True, exist_ok=True)
    artifact_ref = artifact_ref or f"{component_id}_output"
    if events is None:
        events = (
            _event(
                component_id=component_id,
                component_run_id=run_id,
                seq=1,
                event="component_started",
                stage_id=None,
                public_payload={"stage_count": 1},
            ),
            _event(
                component_id=component_id,
                component_run_id=run_id,
                seq=2,
                event="stage_started",
                stage_id=local_stage,
                public_payload={
                    "current_work_id": "work_1",
                    "progress": {"completed": 0, "total": 1, "unit": "items"},
                },
            ),
            _event(
                component_id=component_id,
                component_run_id=run_id,
                seq=3,
                event="stage_completed",
                stage_id=local_stage,
                public_payload={"progress": {"completed": 1, "total": 1, "unit": "items"}},
            ),
            _event(
                component_id=component_id,
                component_run_id=run_id,
                seq=4,
                event="component_done",
                stage_id=None,
                public_payload={"artifact_ref": artifact_ref},
            ),
        )
    manifest = {
        "schema_id": f"{component_id.title()}ComponentManifestV1",
        "workflow_run_id": workflow_run_id,
        "component_id": component_id,
        "component_run_id": run_id,
        "component_attempt_id": attempt,
        "status": status,
    }
    index = {
        "schema_id": f"{component_id.title()}ArtifactIndexV1",
        "artifact_ref": artifact_ref,
    }
    files = {
        "component_manifest.json": canonical_json_bytes(manifest),
        "events.jsonl": b"".join(item.source_bytes + b"\n" for item in events),
        "artifact_index.json": canonical_json_bytes(index),
        "artifacts/output.json": artifact_bytes,
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    file_rows = tuple(
        ComponentFileInputV1(relative, physical_sha256(data))
        for relative, data in sorted(files.items())
    )
    artifact = ComponentArtifactInputV1(
        binding={
            "artifact_ref": artifact_ref,
            "artifact_kind": "translation_artifact" if component_id == "translation" else "component_artifact",
            "schema_version": "1.0.0",
            "sha256": physical_sha256(artifact_bytes),
            "sha256_kind": "physical",
        },
        source_relative_path="artifacts/output.json",
        producer_stage_id=local_stage,
        parent_artifact_refs=(),
        created_event_id=events[-2 if len(events) >= 2 else -1].value["event_id"],
    )
    return ComponentSnapshotV1(
        workflow_run_id=workflow_run_id,
        component_flow_kind=f"{component_id}_flow",
        component_id=component_id,
        component_run_id=run_id,
        component_attempt_id=attempt,
        status=status,
        validator_id=f"{component_id}.component.validator_v1",
        validator_revision="v1",
        validation_receipt_sha256=canonical_sha256(manifest),
        files=file_rows,
        events=events,
        artifacts=(artifact,),
    )


def _read_events(root: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


def test_ingest_projects_parent_sequence_hash_chain_and_stage(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source, component_id="translation", run_id="translation_run_1", local_stage="translate"
    )

    manifest = relay.ingest_component(source, adapter=FixtureAdapter(snapshot))

    assert manifest["latest_event_seq"] == 4
    assert manifest["status"] == "running"
    assert manifest["components"][0]["component_run_id"] == "translation_run_1"
    assert manifest["stages"][0]["status"] == "succeeded"
    events = _read_events(relay.root)
    assert [row["seq"] for row in events] == [1, 2, 3, 4]
    assert events[1]["stage_id"] == "translation.translate"
    assert events[1]["component"]["component_seq"] == 2
    assert events[1]["schema_id"] == "WorkflowEventV1"
    assert events[1]["schema_version"] == "1.0.0"
    assert events[1]["integrity"]["previous_event_sha256"] == events[0]["integrity"]["event_sha256"]
    index = json.loads((relay.root / "artifact_index.json").read_text())
    assert index["artifacts"][0]["created_event_id"] == "workflow_event_00000003"
    assert index["artifacts"][0]["binding"]["artifact_ref"].startswith("components/translation/")
    assert validate_workflow_parent_package_v1(relay.root) == manifest


def test_ingest_validates_and_materializes_one_captured_component_tree(
    tmp_path: Path,
) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
    )
    adapter = MutatingSourceAdapter(snapshot, live_source=source)

    relay.ingest_component(source, adapter=adapter)

    assert adapter.captured_root is not None
    import_record = json.loads(
        next((relay.root / "relay_imports").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    imported = (
        relay.root
        / "components"
        / "translation"
        / "translation_run_1"
        / "snapshots"
        / import_record["snapshot_sha256"]
        / "artifacts"
        / "output.json"
    )
    assert imported.read_bytes() == b'{"ok":true}'
    assert (source / "artifacts" / "output.json").read_bytes() != imported.read_bytes()
    validate_workflow_parent_package_v1(relay.root)


def test_ingest_rejects_unfinished_component_temp_file_before_validation(
    tmp_path: Path,
) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
    )
    (source / "component_manifest.json.tmp").write_bytes(b'{"partial":true}')

    with pytest.raises(WorkflowReplayContractError) as exc:
        relay.ingest_component(source, adapter=FixtureAdapter(snapshot))

    assert exc.value.code == "component_capture_incomplete"
    assert not list((relay.root / "relay_imports").glob("*.json"))


def test_parent_package_rederivation_rejects_materialized_artifact_drift(
    tmp_path: Path,
) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
    )
    relay.ingest_component(source, adapter=FixtureAdapter(snapshot))
    validate_workflow_parent_package_v1(relay.root)

    index = json.loads((relay.root / "artifact_index.json").read_text())
    artifact_path = relay.root / index["artifacts"][0]["binding"]["artifact_ref"]
    artifact_path.write_bytes(b'{"tampered":true}')

    with pytest.raises(WorkflowReplayContractError) as exc:
        validate_workflow_parent_package_v1(relay.root)
    assert exc.value.code == "artifact_physical_drift"


def test_exact_reingest_is_idempotent(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source, component_id="translation", run_id="translation_run_1", local_stage="translate"
    )
    relay.ingest_component(source, adapter=FixtureAdapter(snapshot))
    before = {
        name: (relay.root / name).read_bytes()
        for name in ("workflow_manifest.json", "events.jsonl", "artifact_index.json")
    }

    relay.ingest_component(source, adapter=FixtureAdapter(snapshot))

    assert len(list((relay.root / "relay_imports").glob("*.json"))) == 1
    assert all((relay.root / name).read_bytes() == data for name, data in before.items())


def test_event_id_reuse_with_unequal_bytes_fails_before_parent_projection(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    original = _write_snapshot(
        source, component_id="translation", run_id="translation_run_1", local_stage="translate"
    )
    relay.ingest_component(source, adapter=FixtureAdapter(original))
    before = (relay.root / "events.jsonl").read_bytes()
    changed_events = list(original.events)
    changed_events[1] = _event(
        component_id="translation",
        component_run_id="translation_run_1",
        seq=2,
        event="stage_started",
        stage_id="translate",
        public_payload={"progress": {"completed": 0, "total": 2, "unit": "items"}},
    )
    changed = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=tuple(changed_events),
    )

    with pytest.raises(WorkflowReplayContractError, match="event ID reused"):
        relay.ingest_component(source, adapter=FixtureAdapter(changed))

    assert (relay.root / "events.jsonl").read_bytes() == before
    assert len(list((relay.root / "relay_imports").glob("*.json"))) == 1


def test_foreign_workflow_and_component_global_seq_fail_closed(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    foreign = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        workflow_run_id="foreign_workflow",
    )
    with pytest.raises(WorkflowReplayContractError, match="foreign workflow"):
        relay.ingest_component(source, adapter=FixtureAdapter(foreign))

    valid = _write_snapshot(
        source, component_id="translation", run_id="translation_run_1", local_stage="translate"
    )
    event = dict(valid.events[0].value)
    event["seq"] = 1
    event_input = replace(
        valid.events[0], value=event, source_bytes=canonical_json_bytes(event)
    )
    invalid = replace(valid, events=(event_input, *valid.events[1:]))
    with pytest.raises(WorkflowReplayContractError, match="component cannot assign parent seq"):
        relay.ingest_component(source, adapter=FixtureAdapter(invalid))


def test_resume_advances_attempt_without_relaying_prefix_twice(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    paused_events = (
        _event(component_id="translation", component_run_id="translation_run_1", seq=1, event="component_started", stage_id=None),
        _event(component_id="translation", component_run_id="translation_run_1", seq=2, event="stage_started", stage_id="translate"),
        _event(component_id="translation", component_run_id="translation_run_1", seq=3, event="stage_paused", stage_id="translate"),
    )
    paused = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=paused_events,
        status="paused",
    )
    relay.ingest_component(source, adapter=FixtureAdapter(paused))
    resumed_events = (
        *paused_events,
        _event(component_id="translation", component_run_id="translation_run_1", seq=4, event="component_resumed", stage_id=None, attempt=2),
        _event(component_id="translation", component_run_id="translation_run_1", seq=5, event="stage_started", stage_id="translate", attempt=2),
        _event(component_id="translation", component_run_id="translation_run_1", seq=6, event="stage_completed", stage_id="translate", attempt=2),
        _event(component_id="translation", component_run_id="translation_run_1", seq=7, event="component_done", stage_id=None, attempt=2),
    )
    resumed = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=resumed_events,
        status="succeeded",
        attempt=2,
    )

    manifest = relay.ingest_component(source, adapter=FixtureAdapter(resumed))

    assert manifest["latest_event_seq"] == 7
    assert manifest["components"][0]["component_attempt_id"] == 2
    assert [row["component"]["component_seq"] for row in _read_events(relay.root)] == list(range(1, 8))


def test_evaluation_progress_and_terminal_outcome_project_truthfully(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "evaluation"
    running_events = (
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=1,
            event="component_started",
            stage_id=None,
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=2,
            event="stage_start",
            stage_id="score",
            public_payload={"work_total": 2, "work_unit": "chapters"},
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=3,
            event="progress",
            stage_id="score",
            public_payload={
                "completed": 1,
                "total": 2,
                "unit": "chapters",
                "current_work_id": "chapter_1",
            },
        ),
    )
    running = _write_snapshot(
        source,
        component_id="evaluation",
        run_id="evaluation_run_1",
        local_stage="score",
        events=running_events,
        status="running",
    )

    manifest = relay.ingest_component(source, adapter=FixtureAdapter(running))

    stage = manifest["stages"][1]
    assert stage["status"] == "running"
    assert stage["progress"] == {
        "completed": 1,
        "total": 2,
        "unit": "chapters",
    }
    assert stage["current_work_id"] == "chapter_1"

    failed_events = (
        *running_events,
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=4,
            event="stage_done",
            stage_id="score",
            public_payload={"outcome": "blocked"},
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=5,
            event="component_failed",
            stage_id="__component__",
            public_payload={
                "outcome": "failed",
                "reason_code": "benchmark_preflight_blocked",
            },
        ),
    )
    failed = _write_snapshot(
        source,
        component_id="evaluation",
        run_id="evaluation_run_1",
        local_stage="score",
        events=failed_events,
        status="failed",
    )

    manifest = relay.ingest_component(source, adapter=FixtureAdapter(failed))

    stage = manifest["stages"][1]
    assert stage["status"] == "failed"
    assert stage["progress"] == {
        "completed": 1,
        "total": 2,
        "unit": "chapters",
    }
    assert stage["current_work_id"] is None


def test_component_halt_pauses_active_stage_and_resume_finishes_it(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "evaluation"
    paused_events = (
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=1,
            event="component_started",
            stage_id=None,
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=2,
            event="stage_start",
            stage_id="score",
            public_payload={"work_total": 2, "work_unit": "chapters"},
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=3,
            event="progress",
            stage_id="score",
            public_payload={
                "completed": 1,
                "total": 2,
                "unit": "chapters",
                "current_work_id": "chapter_1",
            },
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=4,
            event="component_halted",
            stage_id="__component__",
            public_payload={
                "reason_code": "operator_pause",
                "resume_available": True,
            },
        ),
    )
    paused = _write_snapshot(
        source,
        component_id="evaluation",
        run_id="evaluation_run_1",
        local_stage="score",
        events=paused_events,
        status="paused",
    )

    manifest = relay.ingest_component(source, adapter=FixtureAdapter(paused))

    stage = manifest["stages"][1]
    assert stage["status"] == "paused"
    assert stage["current_work_id"] == "chapter_1"
    assert manifest["active_stage_id"] is None

    resumed_events = (
        *paused_events,
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=5,
            event="component_resumed",
            stage_id="__component__",
            attempt=2,
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=6,
            event="stage_start",
            stage_id="score",
            attempt=2,
            public_payload={"work_total": 2, "work_unit": "chapters"},
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=7,
            event="progress",
            stage_id="score",
            attempt=2,
            public_payload={
                "completed": 2,
                "total": 2,
                "unit": "chapters",
                "current_work_id": "chapter_2",
            },
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=8,
            event="stage_done",
            stage_id="score",
            attempt=2,
            public_payload={"outcome": "succeeded"},
        ),
        _event(
            component_id="evaluation",
            component_run_id="evaluation_run_1",
            seq=9,
            event="component_done",
            stage_id="__component__",
            attempt=2,
            public_payload={"outcome": "succeeded"},
        ),
    )
    resumed = _write_snapshot(
        source,
        component_id="evaluation",
        run_id="evaluation_run_1",
        local_stage="score",
        events=resumed_events,
        status="succeeded",
        attempt=2,
    )

    manifest = relay.ingest_component(source, adapter=FixtureAdapter(resumed))

    stage = manifest["stages"][1]
    assert stage["status"] == "succeeded"
    assert stage["progress"] == {
        "completed": 2,
        "total": 2,
        "unit": "chapters",
    }
    assert stage["current_work_id"] is None


def test_resume_without_explicit_event_and_append_after_terminal_reject(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    invalid_events = (
        _event(component_id="translation", component_run_id="translation_run_1", seq=1, event="component_started", stage_id=None),
        _event(component_id="translation", component_run_id="translation_run_1", seq=2, event="stage_started", stage_id="translate", attempt=2),
    )
    invalid = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=invalid_events,
        status="running",
        attempt=2,
    )
    with pytest.raises(WorkflowReplayContractError, match="explicit resume event"):
        relay.ingest_component(source, adapter=FixtureAdapter(invalid))

    terminal = _write_snapshot(
        source, component_id="translation", run_id="translation_run_1", local_stage="translate"
    )
    relay.ingest_component(source, adapter=FixtureAdapter(terminal))
    extra_events = (
        *terminal.events,
        _event(component_id="translation", component_run_id="translation_run_1", seq=5, event="stage_started", stage_id="translate"),
    )
    extra = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=extra_events,
        status="running",
    )
    with pytest.raises(WorkflowReplayContractError, match="after terminal"):
        relay.ingest_component(source, adapter=FixtureAdapter(extra))


def test_artifact_ref_drift_rejects(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    paused_events = (
        _event(component_id="translation", component_run_id="translation_run_1", seq=1, event="component_started", stage_id=None),
        _event(component_id="translation", component_run_id="translation_run_1", seq=2, event="stage_paused", stage_id="translate"),
    )
    first = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=paused_events,
        status="paused",
        artifact_bytes=b"first",
    )
    relay.ingest_component(source, adapter=FixtureAdapter(first))
    resumed_events = (
        *paused_events,
        _event(component_id="translation", component_run_id="translation_run_1", seq=3, event="component_resumed", stage_id=None, attempt=2),
        _event(component_id="translation", component_run_id="translation_run_1", seq=4, event="component_done", stage_id=None, attempt=2),
    )
    changed = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=resumed_events,
        status="succeeded",
        attempt=2,
        artifact_bytes=b"changed",
    )
    with pytest.raises(WorkflowReplayContractError, match="artifact ref reused"):
        relay.ingest_component(source, adapter=FixtureAdapter(changed))


def test_component_artifact_parent_cycle_rejects_before_import(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        artifact_ref="artifact_a",
    )
    second_bytes = b'{"second":true}'
    second_path = source / "artifacts" / "second.json"
    second_path.write_bytes(second_bytes)
    first = replace(snapshot.artifacts[0], parent_artifact_refs=("artifact_b",))
    second = ComponentArtifactInputV1(
        binding={
            "artifact_ref": "artifact_b",
            "artifact_kind": "translation_artifact",
            "schema_version": "1.0.0",
            "sha256": physical_sha256(second_bytes),
            "sha256_kind": "physical",
        },
        source_relative_path="artifacts/second.json",
        producer_stage_id="translate",
        parent_artifact_refs=("artifact_a",),
        created_event_id=snapshot.events[-2].value["event_id"],
    )
    snapshot = replace(
        snapshot,
        files=snapshot.files
        + (ComponentFileInputV1("artifacts/second.json", physical_sha256(second_bytes)),),
        artifacts=(first, second),
    )

    with pytest.raises(WorkflowReplayContractError) as exc:
        relay.ingest_component(source, adapter=FixtureAdapter(snapshot))
    assert exc.value.code == "artifact_parent_cycle"
    assert list((relay.root / "relay_imports").glob("*.json")) == []


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"raw_prompt": "private"}, "private_parent_payload"),
        ({"cost_status": "unknown", "cost_usd": 0}, "unknown_cost"),
        ({"response": {"text": "private"}}, "private_parent_payload"),
    ],
)
def test_parent_payload_rejects_private_or_false_cost(
    tmp_path: Path, payload: dict[str, object], match: str
) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    events = (
        _event(
            component_id="translation",
            component_run_id="translation_run_1",
            seq=1,
            event="component_started",
            stage_id=None,
            public_payload=payload,
            source_payload={"raw_prompt": "may remain private inside component"},
        ),
    )
    snapshot = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        events=events,
        status="running",
    )
    with pytest.raises(WorkflowReplayContractError, match=match):
        relay.ingest_component(source, adapter=FixtureAdapter(snapshot))


def test_recovery_rebuilds_projection_after_import_commit(tmp_path: Path, monkeypatch) -> None:
    relay = _relay(tmp_path)
    source = tmp_path / "translation"
    snapshot = _write_snapshot(
        source, component_id="translation", run_id="translation_run_1", local_stage="translate"
    )
    original_project = relay._project
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected projection crash")
        return original_project(*args, **kwargs)

    monkeypatch.setattr(relay, "_project", fail_once)
    with pytest.raises(RuntimeError, match="injected projection crash"):
        relay.ingest_component(source, adapter=FixtureAdapter(snapshot))
    assert len(list((relay.root / "relay_imports").glob("*.json"))) == 1

    manifest = relay.recover()

    assert manifest["latest_event_seq"] == 4
    assert len(_read_events(relay.root)) == 4


def test_reconstruction_declares_logical_timing_and_stage_order(tmp_path: Path) -> None:
    relay = _relay(tmp_path, reconstructed=True)
    evaluation_root = tmp_path / "evaluation"
    evaluation = _write_snapshot(
        evaluation_root,
        component_id="evaluation",
        run_id="evaluation_run_1",
        local_stage="score",
    )
    translation_root = tmp_path / "translation"
    translation = _write_snapshot(
        translation_root,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
    )
    relay.ingest_component(evaluation_root, adapter=FixtureAdapter(evaluation))
    manifest = relay.ingest_component(translation_root, adapter=FixtureAdapter(translation))

    events = _read_events(relay.root)
    stage_events = [row for row in events if row["stage_id"] is not None]
    assert stage_events[0]["stage_id"] == "translation.translate"
    assert stage_events[-1]["stage_id"] == "evaluation.score"
    assert manifest["reconstructed"] is True
    assert manifest["timing_authority"] == "logical_order_only"


def _d2l_fragment() -> dict[str, object]:
    source = {"schema": "canonical_source_binding_v1"}
    for row in _source_bindings():
        source[row["role"]] = row["binding"]
    source_hash = canonical_sha256(source)
    universe_hash = canonical_sha256(["block_1"])
    rows = []
    for arm in ("s0", "s1"):
        rows.append(
            {
                "arm_id": arm,
                "artifact": _binding(f"d2l/{arm}.json", "translation_artifact"),
                "producer_component_run_id": "translation_run_1",
                "producer_component_attempt_id": 1,
                "profile_id": f"profile_{arm}",
                "profile_sha256": "b" * 64,
                "config_sha256": "c" * 64,
                "selected_chapter_ids": ["chapter_1"],
                "coverage": {
                    "admitted_block_count": 1,
                    "translated_block_count": 1,
                    "preserved_block_count": 0,
                    "missing_block_count": 0,
                    "failed_block_count": 0,
                    "ordered_block_ids_sha256": universe_hash,
                    "status": "exact_cover",
                },
                "source_binding_sha256": source_hash,
            }
        )
    fragment = {
        "schema": "scoring_handoff_fragment_v1",
        "fragment_sha256": "0" * 64,
        "artifact_ref": "d2l/scoring_handoff_fragment.json",
        "workflow_run_id": WORKFLOW_ID,
        "flow_kind": "terminology_translation",
        "component_id": "translation",
        "translation_component_run_id": "translation_run_1",
        "translation_component_attempt_id": 1,
        "reserved_evaluation_component_run_id": "evaluation_run_1",
        "source_binding": source,
        "source_binding_sha256": source_hash,
        "translation_inputs": rows,
        "glossary_binding": None,
        "context_memory_binding": None,
        "admitted_projection_binding": source["admitted_projection"],
        "selected_chapter_ids": ["chapter_1"],
        "admitted_universe": {
            "ordered_block_ids_sha256": universe_hash,
            "block_count": 1,
            "status": "exact_cover",
        },
        "producer_lineage": {
            "git_commit": "d" * 40,
            "pipeline_version": "translation_component_v1",
            "config_sha256": "c" * 64,
            "code_sha256": "e" * 64,
        },
        "status": "translation_component_ready",
        "created_at": CREATED_AT,
    }
    unhashed = dict(fragment)
    unhashed.pop("fragment_sha256")
    fragment["fragment_sha256"] = canonical_sha256(unhashed).upper()
    return fragment


def _baseline_input(arm: str, admitted: dict[str, str]) -> dict[str, object]:
    return {
        "arm_id": arm,
        "translation_artifact": _binding(f"baselines/{arm}.json", "translation_artifact"),
        "producer": {
            "component_id": f"baseline_{arm}",
            "component_run_id": f"{arm}_run_1",
        },
        "coverage": {
            "expected_block_count": 1,
            "block_universe_sha256": canonical_sha256(["block_1"]),
            "translated_block_count": 1,
            "preserved_block_count": 0,
            "excluded_block_count": 0,
            "review_held_block_count": 0,
            "missing_block_count": 0,
            "failed_block_count": 0,
        },
        "source_binding": admitted,
    }


def _receipt(handoff: dict[str, object], *, artifact_ref: str = "handoffs/scoring_handoff.json") -> dict[str, object]:
    receipt = {
        "schema_id": "ScoringReceiptV1",
        "schema_version": "1.0.0",
        "workflow_run_id": WORKFLOW_ID,
        "flow_kind": "translation_evaluation_publication",
        "evaluation_component_run_id": "evaluation_run_1",
        "evaluation_component_attempt_id": "evalcomp_attempt_0001",
        "scoring_handoff": {
            "artifact_ref": artifact_ref,
            "artifact_kind": "scoring_handoff_v1",
            "schema_version": "1.0.0",
            "sha256": handoff["integrity"]["handoff_sha256"],
            "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
        },
        "accepted_translation_inputs": copy.deepcopy(handoff["translation_inputs"]),
        "accepted_input_set_sha256": handoff["input_set_sha256"],
        "accepted_at": "2026-07-22T00:05:00Z",
        "status": "accepted",
        "rejection_code": None,
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_component_v1",
            "component_version": "1.0.0",
            "code_commit": "e" * 40,
        },
        "integrity": {"receipt_sha256": "0" * 64},
    }
    payload = copy.deepcopy(receipt)
    payload["integrity"].pop("receipt_sha256")
    receipt["integrity"]["receipt_sha256"] = canonical_sha256(payload)
    return receipt


def _publish_fixture_handoff(relay: WorkflowRelayV1, source: Path) -> dict[str, object]:
    artifact_bytes = {
        "s0": b'{"arm_id":"s0"}',
        "s1": b'{"arm_id":"s1"}',
    }
    fragment = _d2l_fragment()
    for row in fragment["translation_inputs"]:
        row["artifact"]["sha256"] = physical_sha256(artifact_bytes[row["arm_id"]])
    unhashed = copy.deepcopy(fragment)
    unhashed.pop("fragment_sha256")
    fragment["fragment_sha256"] = canonical_sha256(unhashed)
    snapshot = _write_snapshot(
        source,
        component_id="translation",
        run_id="translation_run_1",
        local_stage="translate",
        artifact_bytes=artifact_bytes["s0"],
        artifact_ref="d2l/s0.json",
    )
    s1_path = source / "artifacts" / "s1.json"
    s1_path.write_bytes(artifact_bytes["s1"])
    snapshot = replace(
        snapshot,
        files=snapshot.files
        + (ComponentFileInputV1("artifacts/s1.json", physical_sha256(artifact_bytes["s1"])),),
        artifacts=snapshot.artifacts
        + (
            ComponentArtifactInputV1(
                binding=fragment["translation_inputs"][1]["artifact"],
                source_relative_path="artifacts/s1.json",
                producer_stage_id="translate",
                parent_artifact_refs=(),
                created_event_id=snapshot.events[-2].value["event_id"],
            ),
        ),
    )
    relay.ingest_component(source, adapter=FixtureAdapter(snapshot))
    parent_index = json.loads((relay.root / "artifact_index.json").read_text())
    ref_map = {
        row["component_artifact_ref"]: row["binding"]["artifact_ref"]
        for row in parent_index["artifacts"]
        if row["producer"]["component_id"] == "translation"
    }
    projected = normalize_d2l_scoring_fragment_v1(fragment, artifact_ref_map=ref_map)
    admitted = projected["source_package_bindings"][3]["binding"]
    return relay.publish_scoring_handoff(
        handoff_id="handoff_fixture_v1",
        source_package_bindings=projected["source_package_bindings"],
        optional_bindings=projected["optional_bindings"],
        translation_inputs=[
            *projected["translation_inputs"],
            _baseline_input("community", admitted),
            _baseline_input("google_nmt", admitted),
            _baseline_input("llm_lc", admitted),
        ],
        created_at="2026-07-22T00:04:00Z",
    )


def test_d2l_fragment_plus_three_owned_arms_builds_exact_handoff_and_receipt(
    tmp_path: Path,
) -> None:
    relay = _relay(tmp_path)
    handoff = _publish_fixture_handoff(relay, tmp_path / "translation_handoff")
    accepted_receipt = relay.accept_scoring_receipt(_receipt(handoff))

    assert [row["arm_id"] for row in handoff["translation_inputs"]] == [
        "s0",
        "s1",
        "community",
        "google_nmt",
        "llm_lc",
    ]
    assert handoff["input_set_sha256"] == scoring_input_set_sha256_v1(
        handoff["translation_inputs"]
    )
    assert accepted_receipt["accepted_translation_inputs"] == handoff["translation_inputs"]
    assert all(
        row["translation_artifact"]["artifact_ref"].startswith("components/translation/")
        for row in handoff["translation_inputs"][:2]
    )
    refs = [
        row["binding"]["artifact_ref"]
        for row in json.loads((relay.root / "artifact_index.json").read_text())["artifacts"]
    ]
    assert "handoffs/scoring_handoff.json" in refs
    assert "handoffs/scoring_receipt.json" in refs
    assert validate_workflow_parent_package_v1(relay.root)["latest_event_seq"] == 4


def test_handoff_rejects_d2l_foreign_arm_and_receipt_renamed_ref(tmp_path: Path) -> None:
    fragment = _d2l_fragment()
    fragment["translation_inputs"][1]["arm_id"] = "community"
    unhashed = dict(fragment)
    unhashed.pop("fragment_sha256")
    fragment["fragment_sha256"] = canonical_sha256(unhashed)
    with pytest.raises(WorkflowReplayContractError, match="S0 and S1"):
        normalize_d2l_scoring_fragment_v1(fragment)

    relay = _relay(tmp_path)
    handoff = _publish_fixture_handoff(relay, tmp_path / "translation_handoff")
    with pytest.raises(WorkflowReplayContractError, match="exact parent scoring handoff"):
        relay.accept_scoring_receipt(_receipt(handoff, artifact_ref="foreign/handoff.json"))


def test_handoff_self_hash_and_input_hash_are_load_bearing(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    handoff = _publish_fixture_handoff(relay, tmp_path / "translation_handoff")
    tampered = copy.deepcopy(handoff)
    tampered["translation_inputs"][4]["translation_artifact"]["sha256"] = "f" * 64
    with pytest.raises(WorkflowReplayContractError, match="input set hash drift"):
        validate_scoring_handoff_v1(tampered)


def test_root_config_reopen_rejects_material_drift(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    with pytest.raises(WorkflowReplayContractError, match="existing immutable bytes differ"):
        WorkflowRelayV1(
            relay.root,
            workflow_run_id=WORKFLOW_ID,
            job_id="different_job",
            source_package_bindings=_source_bindings(),
            stages=_stages(),
            code_commit=COMMIT,
            created_at=CREATED_AT,
        )
