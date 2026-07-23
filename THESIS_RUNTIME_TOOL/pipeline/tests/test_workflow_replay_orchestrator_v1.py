from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.eval.workflow_component_v1 import build_scoring_receipt_v1
from pipeline.scripts import run_workflow_orchestrator_v1 as orchestrator_cli
from pipeline.workflow_replay.contracts_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
)
from pipeline.workflow_replay.orchestrator_v1 import (
    StaticBaselineInputProviderV1,
    WorkflowComponentPausedV1,
    WorkflowOrchestratorError,
    WorkflowOrchestratorV1,
    load_workflow_runtime_registration_v1,
    validate_workflow_runtime_registration_v1,
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


WORKFLOW_ID = "wf_orchestrator_fixture_v1"
JOB_ID = "job_orchestrator_fixture_v1"
COMMIT = "a" * 40
CREATED_AT = "2026-07-23T00:00:00Z"


def _binding(ref: str, kind: str) -> dict[str, str]:
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
            "binding": _binding(
                "source/structure_manifest.json", "structure_manifest"
            ),
        },
        {
            "role": "asset_manifest",
            "binding": _binding("source/asset_manifest.json", "asset_manifest"),
        },
        {
            "role": "admitted_projection",
            "binding": _binding(
                "source/admitted_projection_v1.json", "admitted_projection"
            ),
        },
        {
            "role": "normalization_receipt",
            "binding": _binding(
                "source/normalization_receipt.json", "normalization_receipt"
            ),
        },
        {
            "role": "package_seal",
            "binding": _binding(
                "source/source_lifecycle_v2.json", "source_package_seal"
            ),
        },
    ]


def _relay(tmp_path: Path) -> WorkflowRelayV1:
    return WorkflowRelayV1(
        tmp_path / "parent",
        workflow_run_id=WORKFLOW_ID,
        job_id=JOB_ID,
        source_package_bindings=_source_bindings(),
        stages=(
            StageDefinitionV1(
                "translation.translate",
                "translation",
                "translate",
                1,
                "Translate",
                "translator",
            ),
            StageDefinitionV1(
                "evaluation.score",
                "evaluation",
                "score",
                2,
                "Score",
                "evaluation",
            ),
            StageDefinitionV1(
                "publication.export",
                "publication",
                "export",
                3,
                "Export",
                "publication",
            ),
        ),
        code_commit=COMMIT,
        created_at=CREATED_AT,
        clock=lambda: CREATED_AT,
    )


def _event(
    *,
    component_id: str,
    run_id: str,
    seq: int,
    event: str,
    stage_id: str | None,
    attempt: int,
    payload: dict | None = None,
) -> ComponentEventInputV1:
    row = {
        "schema": f"{component_id}_component_event_v1",
        "event_id": f"evt_{run_id}_{seq:08d}",
        "workflow_run_id": WORKFLOW_ID,
        "flow_kind": f"{component_id}_flow",
        "component_id": component_id,
        "component_run_id": run_id,
        "component_attempt_id": attempt,
        "component_attempt_index": attempt,
        "component_seq": seq,
        "ts": CREATED_AT,
        "stage_id": stage_id,
        "agent": f"{component_id}_fixture",
        "event": event,
        "severity": "info",
        "payload": payload or {},
    }
    return ComponentEventInputV1(
        value=row,
        source_bytes=canonical_json_bytes(row),
        public_payload=row["payload"],
    )


def _write_snapshot(
    root: Path,
    *,
    component_id: str,
    run_id: str,
    stage_id: str,
    events: tuple[ComponentEventInputV1, ...],
    status: str,
    attempt: int,
    artifact_files: dict[str, tuple[bytes, dict[str, str]]],
) -> ComponentSnapshotV1:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "workflow_run_id": WORKFLOW_ID,
        "component_id": component_id,
        "component_run_id": run_id,
        "component_attempt_id": attempt,
        "status": status,
    }
    index = {"component_id": component_id, "artifacts": []}
    files: dict[str, bytes] = {
        "component_manifest.json": canonical_json_bytes(manifest),
        "events.jsonl": b"".join(event.source_bytes + b"\n" for event in events),
    }
    artifacts = []
    created_event_id = next(
        (
            event.value["event_id"]
            for event in reversed(events)
            if event.value["stage_id"] == stage_id
        ),
        events[-1].value["event_id"],
    )
    for relative, (payload, binding) in artifact_files.items():
        files[relative] = payload
        index["artifacts"].append(
            {
                **binding,
                "relative_path": relative,
                "producer_stage_id": stage_id,
                "parent_artifact_refs": [],
                "created_event_id": created_event_id,
            }
        )
        artifacts.append(
            ComponentArtifactInputV1(
                binding=binding,
                source_relative_path=relative,
                producer_stage_id=stage_id,
                parent_artifact_refs=(),
                created_event_id=created_event_id,
            )
        )
    files["artifact_index.json"] = canonical_json_bytes(index)
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return ComponentSnapshotV1(
        workflow_run_id=WORKFLOW_ID,
        component_flow_kind=f"{component_id}_flow",
        component_id=component_id,
        component_run_id=run_id,
        component_attempt_id=attempt,
        component_attempt_index=attempt,
        status=status,
        validator_id=f"{component_id}.fixture.validator_v1",
        validator_revision="v1",
        validation_receipt_sha256=canonical_sha256(manifest),
        files=tuple(
            ComponentFileInputV1(relative, physical_sha256(payload))
            for relative, payload in sorted(files.items())
        ),
        events=events,
        artifacts=tuple(artifacts),
    )


class _SnapshotAdapter:
    def __init__(self, executor, terminal: bool) -> None:
        self.executor = executor
        self.terminal = terminal
        self.validator_id = executor.validator_id
        self.validator_revision = "v1"

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1:
        assert component_root == self.executor.root
        assert workflow_run_id == WORKFLOW_ID
        snapshot = self.executor.snapshot
        if self.terminal and snapshot.status != "succeeded":
            raise ValueError("terminal snapshot required")
        return snapshot


def _translation_fragment(
    s0_binding: dict[str, str],
    s1_binding: dict[str, str],
) -> dict:
    source = {
        "schema": "canonical_source_binding_v1",
        **{row["role"]: row["binding"] for row in _source_bindings()},
    }
    coverage = {
        "admitted_block_count": 1,
        "translated_block_count": 1,
        "preserved_block_count": 0,
        "missing_block_count": 0,
        "failed_block_count": 0,
        "ordered_block_ids_sha256": canonical_sha256(["b1"]),
        "status": "exact_cover",
    }
    fragment = {
        "schema": "scoring_handoff_fragment_v1",
        "fragment_sha256": "0" * 64,
        "artifact_ref": "art_scoring_handoff_fragment",
        "workflow_run_id": WORKFLOW_ID,
        "flow_kind": "terminology_translation",
        "component_id": "translation",
        "translation_component_run_id": "translation_run_v1",
        "translation_component_attempt_id": 2,
        "reserved_evaluation_component_run_id": "evaluation_run_v1",
        "source_binding": source,
        "source_binding_sha256": canonical_sha256(source),
        "translation_inputs": [
            {
                "arm_id": "s0",
                "artifact": s0_binding,
                "coverage": coverage,
            },
            {
                "arm_id": "s1",
                "artifact": s1_binding,
                "coverage": coverage,
            },
        ],
        "glossary_binding": None,
        "context_memory_binding": None,
        "admitted_projection_binding": source["admitted_projection"],
        "selected_chapter_ids": ["ch1"],
        "admitted_universe": {
            "ordered_block_ids_sha256": canonical_sha256(["b1"]),
            "block_count": 1,
            "status": "exact_cover",
        },
        "producer_lineage": {
            "git_commit": COMMIT,
            "pipeline_version": "fixture",
            "config_sha256": "b" * 64,
            "code_sha256": "c" * 64,
        },
        "status": "translation_component_ready",
        "created_at": CREATED_AT,
    }
    payload = copy.deepcopy(fragment)
    payload.pop("fragment_sha256")
    fragment["fragment_sha256"] = canonical_sha256(payload)
    return fragment


class _TranslationExecutor:
    validator_id = "translation.fixture.validator_v1"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshot: ComponentSnapshotV1
        self.calls = 0

    def adapter(self, terminal: bool) -> _SnapshotAdapter:
        return _SnapshotAdapter(self, terminal)

    def execute(self, observer) -> Path:
        self.calls += 1
        started = (
            _event(
                component_id="translation",
                run_id="translation_run_v1",
                seq=1,
                event="component_started",
                stage_id=None,
                attempt=1,
            ),
            _event(
                component_id="translation",
                run_id="translation_run_v1",
                seq=2,
                event="stage_started",
                stage_id="translate",
                attempt=1,
                payload={
                    "progress": {"completed": 0, "total": 1, "unit": "blocks"}
                },
            ),
            _event(
                component_id="translation",
                run_id="translation_run_v1",
                seq=3,
                event="component_halted",
                stage_id=None,
                attempt=1,
            ),
        )
        if self.calls == 1:
            self.snapshot = _write_snapshot(
                self.root,
                component_id="translation",
                run_id="translation_run_v1",
                stage_id="translate",
                events=started,
                status="paused",
                attempt=1,
                artifact_files={},
            )
            observer(self.root, False)
            raise WorkflowComponentPausedV1("translation")

        s0_bytes = b'{"arm":"s0","translations":[{"block_id":"b1"}]}'
        s1_bytes = b'{"arm":"s1","translations":[{"block_id":"b1"}]}'
        s0_binding = {
            "artifact_ref": "art_translation_s0",
            "artifact_kind": "translation_artifact",
            "schema_version": "TranslationArtifactV1",
            "sha256": physical_sha256(s0_bytes),
            "sha256_kind": "physical",
        }
        s1_binding = {
            "artifact_ref": "art_translation_s1",
            "artifact_kind": "translation_artifact",
            "schema_version": "TranslationArtifactV1",
            "sha256": physical_sha256(s1_bytes),
            "sha256_kind": "physical",
        }
        fragment = _translation_fragment(s0_binding, s1_binding)
        fragment_bytes = canonical_json_bytes(fragment)
        fragment_binding = {
            "artifact_ref": "art_scoring_handoff_fragment",
            "artifact_kind": "scoring_handoff_fragment_v1",
            "schema_version": "1.0.0",
            "sha256": physical_sha256(fragment_bytes),
            "sha256_kind": "physical",
        }
        events = (
            *started,
            _event(
                component_id="translation",
                run_id="translation_run_v1",
                seq=4,
                event="component_resumed",
                stage_id=None,
                attempt=2,
            ),
            _event(
                component_id="translation",
                run_id="translation_run_v1",
                seq=5,
                event="stage_completed",
                stage_id="translate",
                attempt=2,
                payload={
                    "outcome": "succeeded",
                    "progress": {"completed": 1, "total": 1, "unit": "blocks"},
                },
            ),
            _event(
                component_id="translation",
                run_id="translation_run_v1",
                seq=6,
                event="component_done",
                stage_id=None,
                attempt=2,
            ),
        )
        self.snapshot = _write_snapshot(
            self.root,
            component_id="translation",
            run_id="translation_run_v1",
            stage_id="translate",
            events=events,
            status="succeeded",
            attempt=2,
            artifact_files={
                "artifacts/s0.json": (s0_bytes, s0_binding),
                "artifacts/s1.json": (s1_bytes, s1_binding),
                "scoring_handoff_fragment.json": (
                    fragment_bytes,
                    fragment_binding,
                ),
            },
        )
        observer(self.root, True)
        return self.root


class _EvaluationExecutor:
    validator_id = "evaluation.fixture.validator_v1"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshot: ComponentSnapshotV1

    def adapter(self, terminal: bool) -> _SnapshotAdapter:
        return _SnapshotAdapter(self, terminal)

    def execute(self, handoff, observer) -> Path:
        receipt = build_scoring_receipt_v1(
            handoff,
            handoff_artifact_ref="handoffs/scoring_handoff.json",
            evaluation_component_run_id="evaluation_run_v1",
            evaluation_component_attempt_id="evalcomp_attempt_0001",
            accepted_at=CREATED_AT,
            producer_code_commit="e" * 40,
            status="accepted",
        )
        payload = canonical_json_bytes(receipt)
        binding = {
            "artifact_ref": "scoring_receipt.json",
            "artifact_kind": "scoring_receipt_v1",
            "schema_version": "1.0.0",
            "sha256": physical_sha256(payload),
            "sha256_kind": "physical",
        }
        events = (
            _event(
                component_id="evaluation",
                run_id="evaluation_run_v1",
                seq=1,
                event="component_started",
                stage_id=None,
                attempt=1,
            ),
            _event(
                component_id="evaluation",
                run_id="evaluation_run_v1",
                seq=2,
                event="stage_started",
                stage_id="score",
                attempt=1,
            ),
            _event(
                component_id="evaluation",
                run_id="evaluation_run_v1",
                seq=3,
                event="stage_completed",
                stage_id="score",
                attempt=1,
                payload={"outcome": "succeeded"},
            ),
            _event(
                component_id="evaluation",
                run_id="evaluation_run_v1",
                seq=4,
                event="component_done",
                stage_id=None,
                attempt=1,
            ),
        )
        self.snapshot = _write_snapshot(
            self.root,
            component_id="evaluation",
            run_id="evaluation_run_v1",
            stage_id="score",
            events=events,
            status="succeeded",
            attempt=1,
            artifact_files={"scoring_receipt.json": (payload, binding)},
        )
        observer(self.root, True)
        return self.root


class _PublicationExecutor:
    validator_id = "publication.fixture.validator_v1"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshot: ComponentSnapshotV1
        self.selected_path: Path | None = None

    def adapter(self, terminal: bool) -> _SnapshotAdapter:
        return _SnapshotAdapter(self, terminal)

    def execute(
        self,
        *,
        scoring_handoff,
        selected_translation_input,
        selected_translation_path,
        selected_chapter_ids,
        observer,
    ) -> Path:
        assert scoring_handoff["translation_inputs"][1] == selected_translation_input
        assert selected_chapter_ids == ["ch1"]
        self.selected_path = selected_translation_path
        payload = b'{"publication":"ok"}'
        binding = {
            "artifact_ref": "publication/export_manifest.json",
            "artifact_kind": "publication_export_manifest",
            "schema_version": "1.0.0",
            "sha256": physical_sha256(payload),
            "sha256_kind": "physical",
        }
        events = (
            _event(
                component_id="publication",
                run_id="publication_run_v1",
                seq=1,
                event="component_started",
                stage_id=None,
                attempt=1,
            ),
            _event(
                component_id="publication",
                run_id="publication_run_v1",
                seq=2,
                event="stage_started",
                stage_id="export",
                attempt=1,
            ),
            _event(
                component_id="publication",
                run_id="publication_run_v1",
                seq=3,
                event="stage_completed",
                stage_id="export",
                attempt=1,
                payload={"outcome": "succeeded"},
            ),
            _event(
                component_id="publication",
                run_id="publication_run_v1",
                seq=4,
                event="component_done",
                stage_id=None,
                attempt=1,
            ),
        )
        self.snapshot = _write_snapshot(
            self.root,
            component_id="publication",
            run_id="publication_run_v1",
            stage_id="export",
            events=events,
            status="succeeded",
            attempt=1,
            artifact_files={"artifacts/export.json": (payload, binding)},
        )
        observer(self.root, True)
        return self.root


def _baseline_rows() -> list[dict]:
    admitted = _source_bindings()[3]["binding"]
    return [
        {
            "arm_id": arm_id,
            "translation_artifact": _binding(
                f"baselines/{arm_id}.json", "translation_artifact"
            ),
            "producer": {
                "component_id": f"baseline_{arm_id}",
                "component_run_id": f"{arm_id}_run_v1",
            },
            "coverage": {
                "expected_block_count": 1,
                "block_universe_sha256": canonical_sha256(["b1"]),
                "translated_block_count": 1,
                "preserved_block_count": 0,
                "excluded_block_count": 0,
                "review_held_block_count": 0,
                "missing_block_count": 0,
                "failed_block_count": 0,
            },
            "source_binding": admitted,
        }
        for arm_id in ("community", "google_nmt", "llm_lc")
    ]


def test_orchestrator_preserves_pause_resume_and_finishes_parent(
    tmp_path: Path,
) -> None:
    relay = _relay(tmp_path)
    translation = _TranslationExecutor(tmp_path / "translation")
    evaluation = _EvaluationExecutor(tmp_path / "evaluation")
    publication = _PublicationExecutor(tmp_path / "publication")
    orchestrator = WorkflowOrchestratorV1(
        relay.root,
        translation_executor=translation,
        baseline_provider=StaticBaselineInputProviderV1(_baseline_rows()),
        evaluation_executor=evaluation,
        publication_executor=publication,
        selected_chapter_ids=["ch1"],
        translation_adapter_factory=translation.adapter,
        evaluation_adapter_factory=evaluation.adapter,
        publication_adapter_factory=publication.adapter,
    )

    with pytest.raises(WorkflowComponentPausedV1):
        orchestrator.run()
    paused = validate_workflow_parent_package_v1(relay.root)
    assert paused["status"] == "paused"
    assert paused["components"][0]["component_attempt_index"] == 1

    result = orchestrator.run()
    assert result.manifest["status"] == "succeeded"
    assert [row["component_id"] for row in result.manifest["components"]] == [
        "translation",
        "evaluation",
        "publication",
    ]
    assert [row["arm_id"] for row in result.scoring_handoff["translation_inputs"]] == [
        "s0",
        "s1",
        "community",
        "google_nmt",
        "llm_lc",
    ]
    assert result.scoring_receipt["accepted_translation_inputs"] == result.scoring_handoff[
        "translation_inputs"
    ]
    assert result.manifest["latest_event_seq"] == 14
    assert publication.selected_path == translation.root / "artifacts/s1.json"


def test_translation_phase_stops_truthfully_before_scoring(tmp_path: Path) -> None:
    relay = _relay(tmp_path)
    translation = _TranslationExecutor(tmp_path / "translation")
    orchestrator = WorkflowOrchestratorV1(
        relay.root,
        translation_executor=translation,
        selected_chapter_ids=["ch1"],
        translation_adapter_factory=translation.adapter,
    )

    with pytest.raises(WorkflowComponentPausedV1):
        orchestrator.run_translation()
    result = orchestrator.run_translation()

    assert result.manifest["status"] == "running"
    assert [row["component_id"] for row in result.manifest["components"]] == [
        "translation"
    ]
    assert result.manifest["components"][0]["status"] == "succeeded"
    assert not (relay.root / "handoffs" / "scoring_handoff.json").exists()
    with pytest.raises(
        WorkflowOrchestratorError,
        match="Scoring requires registered baseline",
    ):
        orchestrator.run()


def test_runtime_registration_is_fail_closed_and_self_sealed(
    tmp_path: Path,
) -> None:
    baseline_bytes = canonical_json_bytes({"translation_inputs": []}) + b"\n"
    row = {
        "schema_id": "WorkflowRuntimeRegistrationV1",
        "schema_version": "1.0.0",
        "job_id": JOB_ID,
        "source_binding_sha256": "a" * 64,
        "translation_executor_id": "d2l_project_campaign_v1",
        "baseline_bundle": {
            "arm_ids": ["community", "google_nmt", "llm_lc"],
            "artifact_ref": "workflow/baselines.json",
            "sha256": physical_sha256(baseline_bytes),
            "sha256_kind": "physical",
            "status": "ready",
        },
        "evaluation_executor_id": "evaluation_five_arm_benchmark_v1",
        "publication_executor_id": "selected_chapter_publication_v1",
        "supported_chapter_ids": ["ch1"],
        "status": "ready",
        "blockers": [],
        "integrity": {"registration_sha256": "0" * 64},
    }
    payload = copy.deepcopy(row)
    payload["integrity"].pop("registration_sha256")
    row["integrity"]["registration_sha256"] = canonical_sha256(payload)
    accepted = validate_workflow_runtime_registration_v1(
        row,
        expected_job_id=JOB_ID,
        expected_source_binding_sha256="a" * 64,
        selected_chapter_ids=["ch1"],
    )
    assert accepted["status"] == "ready"
    baseline_path = tmp_path / "workflow" / "baselines.json"
    baseline_path.parent.mkdir(parents=True)
    baseline_path.write_bytes(baseline_bytes)
    (tmp_path / "workflow_runtime_v1.json").write_bytes(
        canonical_json_bytes(row) + b"\n"
    )
    loaded = load_workflow_runtime_registration_v1(
        tmp_path,
        expected_job_id=JOB_ID,
        expected_source_binding_sha256="a" * 64,
        selected_chapter_ids=["ch1"],
    )
    assert loaded == row
    baseline_path.write_bytes(baseline_bytes + b" ")
    with pytest.raises(
        WorkflowOrchestratorError,
        match="baseline bundle bytes drifted",
    ):
        load_workflow_runtime_registration_v1(
            tmp_path,
            expected_job_id=JOB_ID,
            expected_source_binding_sha256="a" * 64,
            selected_chapter_ids=["ch1"],
        )

    tampered = copy.deepcopy(row)
    tampered["baseline_bundle"]["status"] = "review_held"
    with pytest.raises(WorkflowOrchestratorError):
        validate_workflow_runtime_registration_v1(
            tampered,
            expected_job_id=JOB_ID,
            expected_source_binding_sha256="a" * 64,
            selected_chapter_ids=["ch1"],
        )


def test_translation_cli_delegates_only_to_server_owned_d2l_command(
    tmp_path: Path,
) -> None:
    executor = orchestrator_cli._D2LSubprocessExecutorV1(
        job_root=tmp_path / "job",
        campaign_root=tmp_path / "campaign",
        workflow_run_id=WORKFLOW_ID,
        component_run_id="translation_run_v1",
        chapter_ids=("ch1", "ch2"),
        hard_total_token_cap=1234,
        reserved_cost_cap_usd=None,
        code_root=tmp_path,
        runtime_root=tmp_path / "runtime",
        credential_files=(),
        live=False,
        resume=False,
    )

    argv = executor._argv()
    assert argv[1:5] == [
        "-m",
        "pipeline.scripts.run_d2l_project_campaign",
        "app-run",
        "--job-root",
    ]
    assert argv.count("--chapter-id") == 2
    assert "--dry-run" in argv
    assert "--live" not in argv
    assert "--credential-file" not in argv
