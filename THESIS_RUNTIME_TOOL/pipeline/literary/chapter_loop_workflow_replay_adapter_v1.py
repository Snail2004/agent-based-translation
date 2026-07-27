"""Adapter from Literary chapter history into the App's WorkflowRelayV1."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_loop_component_contract_v1 import (
    VALIDATOR_ID,
    VALIDATOR_REVISION,
    validate_literary_chapter_loop_component_v1,
    validate_literary_workflow_handoff_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256


class LiteraryChapterLoopWorkflowReplayError(ValueError):
    pass


class LiteraryChapterLoopComponentAdapterV1:
    validator_id = VALIDATOR_ID
    validator_revision = VALIDATOR_REVISION

    def __init__(self, *, require_terminal: bool = False) -> None:
        self.require_terminal = require_terminal

    def validate_and_snapshot(
        self,
        component_root: Path,
        *,
        workflow_run_id: str,
    ) -> Any:
        relay = _relay_module()
        root = Path(component_root).resolve()
        validated = validate_literary_chapter_loop_component_v1(
            root,
            require_terminal=self.require_terminal,
        )
        manifest = validated["manifest"]
        if manifest["workflow_run_id"] != workflow_run_id:
            raise LiteraryChapterLoopWorkflowReplayError(
                "Literary component belongs to another workflow"
            )
        artifact_rows = validated["artifact_index"]["artifacts"]
        artifacts = tuple(
            relay.ComponentArtifactInputV1(
                binding={
                    key: row[key]
                    for key in (
                        "artifact_ref",
                        "artifact_kind",
                        "schema_version",
                        "sha256",
                        "sha256_kind",
                    )
                },
                source_relative_path=row["relative_path"],
                producer_stage_id=row["producer_stage_id"],
                parent_artifact_refs=tuple(row["parent_artifact_refs"]),
                created_event_id=row["created_event_id"],
            )
            for row in artifact_rows
        )
        events = tuple(
            relay.ComponentEventInputV1(
                value=row,
                source_bytes=source_bytes,
                public_payload=row["payload"],
            )
            for row, source_bytes in zip(
                validated["events"], validated["event_bytes"]
            )
        )
        file_refs = set(
            {
                "component_manifest.json",
                "events.jsonl",
                "artifact_index.json",
                "run_plan.json",
                "chapter_loop_session.json",
            }
        )
        file_refs.update(row["relative_path"] for row in artifact_rows)
        project_ref = manifest.get("project_binding_ref")
        if isinstance(project_ref, str):
            file_refs.add(project_ref)
        handoff_path = root / "literary_workflow_handoff.json"
        if handoff_path.is_file():
            file_refs.add("literary_workflow_handoff.json")
        files = tuple(
            relay.ComponentFileInputV1(
                relative_path=relative,
                physical_sha256=file_sha256(root / relative),
            )
            for relative in sorted(file_refs)
        )
        receipt = validated["validation_receipt"]
        return relay.ComponentSnapshotV1(
            workflow_run_id=manifest["workflow_run_id"],
            component_flow_kind=manifest["flow_kind"],
            component_id=manifest["component_id"],
            component_run_id=manifest["component_run_id"],
            component_attempt_id=manifest["component_attempt_id"],
            component_attempt_index=manifest["component_attempt_index"],
            status=manifest["status"],
            validator_id=self.validator_id,
            validator_revision=self.validator_revision,
            validation_receipt_sha256=canonical_hash(receipt),
            files=files,
            events=events,
            artifacts=artifacts,
        )


def sync_literary_chapter_loop_replay_v1(
    *,
    component_root: Path,
    handoff_path: Path,
    relay_root: Path,
    source_package_bindings: Sequence[Mapping[str, Any]],
    code_commit: str,
    require_terminal: bool = False,
    workflow_runtime_root: Path | None = None,
) -> dict[str, Any]:
    relay_module = _relay_module(workflow_runtime_root)
    handoff = validate_literary_workflow_handoff_v1(
        _read_object(handoff_path, "Literary workflow handoff")
    )
    root = Path(relay_root).resolve()
    stages = tuple(
        relay_module.StageDefinitionV1(**dict(row))
        for row in handoff["stage_definitions"]
    )
    config_path = root / "relay_config.json"
    if config_path.is_file():
        relay = relay_module.WorkflowRelayV1.open_existing(root)
        if (
            relay.workflow_run_id != handoff["workflow_run_id"]
            or relay.job_id != handoff["job_id"]
            or relay.code_commit != code_commit
            or list(relay.source_package_bindings)
            != [dict(row) for row in source_package_bindings]
            or tuple(relay.stages) != stages
        ):
            raise LiteraryChapterLoopWorkflowReplayError(
                "existing WorkflowRelayV1 configuration differs from the Literary handoff"
            )
    else:
        relay = relay_module.WorkflowRelayV1(
            root,
            workflow_run_id=handoff["workflow_run_id"],
            job_id=handoff["job_id"],
            source_package_bindings=source_package_bindings,
            stages=stages,
            code_commit=code_commit,
        )
    manifest = relay.ingest_component(
        Path(component_root).resolve(),
        adapter=LiteraryChapterLoopComponentAdapterV1(
            require_terminal=require_terminal
        ),
    )
    validation = relay.validate_parent_package()
    event_count = sum(
        1
        for raw in (root / "events.jsonl").read_bytes().splitlines()
        if raw.strip()
    )
    artifact_index = _read_object(root / "artifact_index.json", "relay artifact index")
    return {
        "schema_version": "literary_workflow_replay_sync_report_v1",
        "workflow_run_id": handoff["workflow_run_id"],
        "job_id": handoff["job_id"],
        "project_id": handoff["project_id"],
        "relay_root": str(root),
        "workflow_status": manifest["status"],
        "event_count": event_count,
        "artifact_count": len(artifact_index.get("artifacts") or []),
        "parent_manifest_sha256": validation["integrity"][
            "manifest_sha256"
        ],
        "provider_calls": 0,
        "production_publish_performed": False,
    }


def _relay_module(workflow_runtime_root: Path | None = None) -> Any:
    if workflow_runtime_root is not None:
        _activate_workflow_runtime_root(workflow_runtime_root)
    try:
        from pipeline.workflow_replay import relay_v1
    except ImportError as exc:
        raise LiteraryChapterLoopWorkflowReplayError(
            "WorkflowRelayV1 is unavailable; integrate this adapter into source-main first"
        ) from exc
    return relay_v1


def _activate_workflow_runtime_root(runtime_root: Path) -> None:
    root = Path(runtime_root).resolve()
    pipeline_root = root / "pipeline"
    relay_path = pipeline_root / "workflow_replay" / "relay_v1.py"
    if not relay_path.is_file():
        raise LiteraryChapterLoopWorkflowReplayError(
            "workflow runtime root has no WorkflowRelayV1"
        )
    import pipeline

    pipeline_path = str(pipeline_root)
    if pipeline_path not in pipeline.__path__:
        pipeline.__path__.append(pipeline_path)
    for package_name in ("ingest", "eval"):
        package = importlib.import_module(f"pipeline.{package_name}")
        source_path = str(pipeline_root / package_name)
        if source_path not in package.__path__:
            package.__path__.append(source_path)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryChapterLoopWorkflowReplayError(
            f"cannot load {label}"
        ) from exc
    if not isinstance(value, dict):
        raise LiteraryChapterLoopWorkflowReplayError(f"{label} must be an object")
    return value


__all__ = [
    "LiteraryChapterLoopComponentAdapterV1",
    "LiteraryChapterLoopWorkflowReplayError",
    "sync_literary_chapter_loop_replay_v1",
]
