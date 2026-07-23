from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts_v1 import canonical_sha256, physical_sha256
from .relay_v1 import (
    ComponentArtifactInputV1,
    ComponentEventInputV1,
    ComponentFileInputV1,
    ComponentSnapshotV1,
)


class D2LTranslationComponentAdapterV1:
    """Invoke D2L's owning validator, then project its accepted package."""

    validator_id = "d2l.console_replay.component_validator_v1"
    validator_revision = "v1"

    def __init__(self, *, require_terminal: bool = False) -> None:
        self.require_terminal = require_terminal

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1:
        from pipeline.prepass.d2l_console_replay_contract_v1 import (
            validate_translation_component_package,
        )

        root = component_root.resolve()
        validation = validate_translation_component_package(
            root, require_terminal=self.require_terminal
        )
        manifest = _load_mapping(root / "component_manifest.json")
        if validation["workflow_run_id"] != workflow_run_id:
            raise ValueError("D2L component validation returned a foreign workflow")
        events = _load_events(root / "events.jsonl")
        index = _load_mapping(root / "artifact_index.json")
        artifacts = []
        for row in index["artifacts"]:
            binding = {
                key: row[key]
                for key in (
                    "artifact_ref",
                    "artifact_kind",
                    "schema_version",
                    "sha256",
                    "sha256_kind",
                )
            }
            artifacts.append(
                ComponentArtifactInputV1(
                    binding=binding,
                    source_relative_path=row["relative_path"],
                    producer_stage_id=row["producer_stage_id"],
                    parent_artifact_refs=tuple(row["parent_artifact_refs"]),
                    created_event_id=row["created_event_id"],
                )
            )
        status = manifest["status"]
        if status == "cancelled":
            status = "failed"
        return ComponentSnapshotV1(
            workflow_run_id=manifest["workflow_run_id"],
            component_flow_kind=manifest["flow_kind"],
            component_id=manifest["component_id"],
            component_run_id=manifest["component_run_id"],
            component_attempt_id=manifest["component_attempt_id"],
            component_attempt_index=manifest["component_attempt_id"],
            status=status,
            validator_id=self.validator_id,
            validator_revision=self.validator_revision,
            validation_receipt_sha256=canonical_sha256(validation),
            files=_component_files(root),
            events=events,
            artifacts=tuple(artifacts),
        )


class EvaluationComponentAdapterV1:
    """Invoke Evaluation's owning package validator and project its evidence."""

    validator_id = "evaluation.workflow_component.validator_v1"
    validator_revision = "v1"

    def __init__(
        self,
        scoring_handoff: Mapping[str, Any],
        *,
        require_terminal: bool = False,
    ) -> None:
        self.scoring_handoff = dict(scoring_handoff)
        self.require_terminal = require_terminal

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1:
        from pipeline.eval.workflow_component_writer_v1 import (
            validate_evaluation_workflow_component_package_v1,
        )

        root = component_root.resolve()
        validation = validate_evaluation_workflow_component_package_v1(
            root,
            self.scoring_handoff,
            require_terminal=self.require_terminal,
        )
        manifest = validation["manifest"]
        if manifest["workflow_run_id"] != workflow_run_id:
            raise ValueError("Evaluation component validation returned a foreign workflow")
        events = _load_events(root / "events.jsonl")
        index = validation["artifact_index"]
        artifacts = []
        for row in index["artifacts"]:
            binding = row["artifact"]
            artifacts.append(
                ComponentArtifactInputV1(
                    binding=binding,
                    source_relative_path=binding["artifact_ref"],
                    producer_stage_id=row["stage_id"],
                    parent_artifact_refs=tuple(row["parent_artifact_refs"]),
                    created_event_id=row["created_by_event_id"],
                )
            )
        last_event = validation["events"][-1]["event"]
        if last_event == "component_done":
            status = "succeeded"
        elif last_event == "component_failed":
            status = "failed"
        elif last_event == "component_halted":
            status = "paused"
        else:
            status = "running"
        validation_receipt = {
            "manifest_sha256": manifest["integrity"]["manifest_sha256"],
            "artifact_index_sha256": index["integrity"]["artifact_index_sha256"],
            "event_count": len(validation["events"]),
            "receipt_sha256": validation["receipt"]["integrity"]["receipt_sha256"],
        }
        return ComponentSnapshotV1(
            workflow_run_id=manifest["workflow_run_id"],
            component_flow_kind=manifest["flow_kind"],
            component_id=manifest["component_id"],
            component_run_id=manifest["component_run_id"],
            component_attempt_id=manifest["component_attempt_id"],
            component_attempt_index=manifest["component_attempt_index"],
            status=status,
            validator_id=self.validator_id,
            validator_revision=self.validator_revision,
            validation_receipt_sha256=canonical_sha256(validation_receipt),
            files=_component_files(root),
            events=events,
            artifacts=tuple(artifacts),
        )


class PublicationComponentAdapterV1:
    """Validate and project the neutral selected-chapter Publication package."""

    validator_id = "publication.component.validator_v1"
    validator_revision = "v1"

    def __init__(self, *, require_terminal: bool = True) -> None:
        self.require_terminal = require_terminal

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1:
        from .publication_component_v1 import (
            validate_publication_component_package_v1,
        )

        root = component_root.resolve()
        validation = validate_publication_component_package_v1(
            root,
            require_terminal=self.require_terminal,
        )
        manifest = validation["manifest"]
        if manifest["workflow_run_id"] != workflow_run_id:
            raise ValueError("Publication component returned a foreign workflow")
        events = _load_events(root / "events.jsonl")
        artifacts = []
        for row in validation["artifact_index"]["artifacts"]:
            artifacts.append(
                ComponentArtifactInputV1(
                    binding=row["artifact"],
                    source_relative_path=row["relative_path"],
                    producer_stage_id=row["stage_id"],
                    parent_artifact_refs=tuple(row["parent_artifact_refs"]),
                    created_event_id=row["created_by_event_id"],
                )
            )
        return ComponentSnapshotV1(
            workflow_run_id=manifest["workflow_run_id"],
            component_flow_kind=manifest["flow_kind"],
            component_id=manifest["component_id"],
            component_run_id=manifest["component_run_id"],
            component_attempt_id=manifest["component_attempt_id"],
            component_attempt_index=manifest["component_attempt_index"],
            status=manifest["status"],
            validator_id=self.validator_id,
            validator_revision=self.validator_revision,
            validation_receipt_sha256=validation[
                "validation_receipt_sha256"
            ],
            files=_component_files(root),
            events=events,
            artifacts=tuple(artifacts),
        )


def _load_events(path: Path) -> tuple[ComponentEventInputV1, ...]:
    result = []
    for raw_line in path.read_bytes().splitlines():
        if not raw_line.strip():
            continue
        value = json.loads(raw_line.decode("utf-8"))
        result.append(
            ComponentEventInputV1(
                value=value,
                source_bytes=raw_line,
                public_payload=value["payload"],
            )
        )
    return tuple(result)


def _component_files(root: Path) -> tuple[ComponentFileInputV1, ...]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"component package contains an unsafe file: {path}")
        relative = path.relative_to(root).as_posix()
        if relative == ".resume_intent.json":
            raise ValueError("component package has an unfinished Resume intent")
        rows.append(
            ComponentFileInputV1(
                relative_path=relative,
                physical_sha256=physical_sha256(path.read_bytes()),
            )
        )
    return tuple(rows)


def _load_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


__all__ = [
    "D2LTranslationComponentAdapterV1",
    "EvaluationComponentAdapterV1",
    "PublicationComponentAdapterV1",
]
