from __future__ import annotations

import copy
import json
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from .contracts_v1 import (
    COMPONENT_IDS_V1,
    FLOW_KIND_V1,
    SCHEMA_VERSION_V1,
    WorkflowReplayContractError,
    build_scoring_handoff_v1,
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
    validate_parent_event_public_payload,
    validate_scoring_handoff_v1,
    validate_scoring_receipt_v1,
    validate_source_package_bindings_v1,
    validate_typed_artifact_binding_v1,
    validate_workflow_artifact_index_v1,
    validate_workflow_event_v1,
    validate_workflow_manifest_v1,
)


_COMPONENT_STATUS = frozenset({"pending", "running", "paused", "failed", "succeeded"})
_TERMINAL_STATUS = frozenset({"failed", "succeeded"})
_SEVERITIES = frozenset({"info", "warning", "error"})
_RESUME_EVENTS = frozenset({"run_resumed", "component_resumed"})
_START_EVENTS = frozenset({"run_start", "component_started"})
_TERMINAL_EVENTS = frozenset({"run_done", "run_failed", "component_done", "component_failed"})
_MANDATORY_COMPONENT_FILES = frozenset(
    {"component_manifest.json", "events.jsonl", "artifact_index.json"}
)
_MODULE_LOCKS: dict[str, threading.RLock] = {}
_MODULE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class StageDefinitionV1:
    stage_id: str
    component_id: str
    local_stage_id: str
    order: int
    label: str
    producer: str


@dataclass(frozen=True)
class ComponentFileInputV1:
    relative_path: str
    physical_sha256: str


@dataclass(frozen=True)
class ComponentEventInputV1:
    value: Mapping[str, Any]
    source_bytes: bytes
    public_payload: Mapping[str, Any]


@dataclass(frozen=True)
class ComponentArtifactInputV1:
    binding: Mapping[str, Any]
    source_relative_path: str
    producer_stage_id: str
    parent_artifact_refs: tuple[str, ...]
    created_event_id: str


@dataclass(frozen=True)
class ComponentSnapshotV1:
    workflow_run_id: str
    component_flow_kind: str
    component_id: str
    component_run_id: str
    component_attempt_id: str | int
    status: str
    validator_id: str
    validator_revision: str
    validation_receipt_sha256: str
    files: tuple[ComponentFileInputV1, ...]
    events: tuple[ComponentEventInputV1, ...]
    artifacts: tuple[ComponentArtifactInputV1, ...]
    component_attempt_index: int | None = None


class ValidatedComponentAdapterV1(Protocol):
    validator_id: str
    validator_revision: str

    def validate_and_snapshot(
        self, component_root: Path, *, workflow_run_id: str
    ) -> ComponentSnapshotV1: ...


class WorkflowRelayV1:
    """Single-writer projection of isolated component packages into one parent package."""

    def __init__(
        self,
        root: Path | str,
        *,
        workflow_run_id: str,
        job_id: str,
        source_package_bindings: Sequence[Mapping[str, Any]],
        stages: Sequence[StageDefinitionV1],
        code_commit: str,
        reconstructed: bool = False,
        created_at: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.workflow_run_id = _identifier(workflow_run_id, "workflow_run_id")
        self.job_id = _identifier(job_id, "job_id")
        self.code_commit = _commit(code_commit, "code_commit")
        self.reconstructed = _boolean(reconstructed, "reconstructed")
        self._clock = clock or _utc_now
        self.created_at = _timestamp(created_at or self._clock(), "created_at")
        self.source_package_bindings = validate_source_package_bindings_v1(
            list(source_package_bindings)
        )
        self.stages = _validate_stages(stages)
        self._stage_by_component_local = {
            (stage.component_id, stage.local_stage_id): stage for stage in self.stages
        }
        self._stage_by_id = {stage.stage_id: stage for stage in self.stages}
        self._ensure_root()
        self._lock = _module_lock(self.root)
        with self._exclusive():
            self._write_or_validate_config()
            self._project()

    @classmethod
    def open_existing(
        cls,
        root: Path | str,
        *,
        clock: Callable[[], str] | None = None,
    ) -> "WorkflowRelayV1":
        """Open an existing relay package without creating or rewriting files."""

        resolved_root = Path(root).resolve()
        config = _read_relay_config(resolved_root)
        instance = cls.__new__(cls)
        instance.root = resolved_root
        instance.workflow_run_id = config["workflow_run_id"]
        instance.job_id = config["job_id"]
        instance.code_commit = config["code_commit"]
        instance.reconstructed = config["reconstructed"]
        instance._clock = clock or _utc_now
        instance.created_at = config["created_at"]
        instance.source_package_bindings = config["source_package_bindings"]
        instance.stages = config["stages"]
        instance._stage_by_component_local = {
            (stage.component_id, stage.local_stage_id): stage
            for stage in instance.stages
        }
        instance._stage_by_id = {stage.stage_id: stage for stage in instance.stages}
        instance._lock = _module_lock(resolved_root)
        return instance

    def ingest_component(
        self,
        component_root: Path | str,
        *,
        adapter: ValidatedComponentAdapterV1,
    ) -> dict[str, Any]:
        source_root = Path(component_root).resolve()
        snapshot = adapter.validate_and_snapshot(
            source_root, workflow_run_id=self.workflow_run_id
        )
        if snapshot.validator_id != adapter.validator_id:
            raise WorkflowReplayContractError(
                "validator_identity",
                "$.validator_id",
                "adapter and validation receipt disagree",
            )
        if snapshot.validator_revision != adapter.validator_revision:
            raise WorkflowReplayContractError(
                "validator_identity",
                "$.validator_revision",
                "adapter and validation receipt disagree",
            )
        normalized = self._normalize_snapshot(snapshot, source_root=source_root)
        with self._exclusive():
            imports = self._load_imports()
            snapshot_sha = normalized["snapshot_sha256"]
            for record in imports:
                if record["snapshot_sha256"] == snapshot_sha:
                    self._project(imports=imports)
                    return self.load_manifest()
            self._validate_union(imports + [self._preview_import(normalized, imports)])
            self._materialize_component_files(normalized, source_root=source_root)
            accepted_at = self._clock()
            record = self._preview_import(normalized, imports, accepted_at=accepted_at)
            record_path = self.root / "relay_imports" / (
                f"{record['acceptance_ordinal']:08d}_{record['import_sha256']}.json"
            )
            _write_json_absent_or_equal(record_path, record)
            imports.append(record)
            self._project(imports=imports)
            return self.load_manifest()

    def publish_scoring_handoff(
        self,
        *,
        handoff_id: str,
        source_package_bindings: Sequence[Mapping[str, Any]],
        optional_bindings: Mapping[str, Mapping[str, Any] | None],
        translation_inputs: Sequence[Mapping[str, Any]],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        handoff = build_scoring_handoff_v1(
            workflow_run_id=self.workflow_run_id,
            handoff_id=handoff_id,
            created_at=created_at or self._clock(),
            producer_code_commit=self.code_commit,
            source_package_bindings=source_package_bindings,
            optional_bindings=optional_bindings,
            translation_inputs=translation_inputs,
        )
        if handoff["source_package_bindings"] != self.source_package_bindings:
            raise WorkflowReplayContractError(
                "source_binding_drift",
                "$.source_package_bindings",
                "handoff must bind the parent workflow source package exactly",
            )
        with self._exclusive():
            artifact_index = validate_workflow_artifact_index_v1(
                _read_exact_json(self.root / "artifact_index.json")
            )
            self._validate_scoring_handoff_artifact_lineage(handoff, artifact_index)
            path = self.root / "handoffs" / "scoring_handoff.json"
            _write_json_absent_or_equal(path, handoff)
            self._write_relay_artifact(
                {
                    "binding": {
                        "artifact_ref": "handoffs/scoring_handoff.json",
                        "artifact_kind": "scoring_handoff_v1",
                        "schema_version": SCHEMA_VERSION_V1,
                        "sha256": handoff["integrity"]["handoff_sha256"],
                        "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
                    },
                    "physical_sha256": physical_sha256(path.read_bytes()),
                    "producer_component_id": "neutral_relay",
                    "producer_component_run_id": self.workflow_run_id,
                    "producer_component_attempt_id": None,
                    "producer_component_attempt_index": None,
                    "producer_stage_id": "relay.scoring_handoff",
                    "parent_artifact_refs": [],
                    "created_event_id": None,
                }
            )
            self._project()
        return handoff

    def publish_evaluation_settings(
        self,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Publish the exact Evaluation settings derived from the parent handoff."""

        from pipeline.eval.evaluation_workflow_settings_v1 import (
            validate_evaluation_workflow_settings_v1,
        )

        with self._exclusive():
            handoff_path = self.root / "handoffs" / "scoring_handoff.json"
            if not handoff_path.is_file():
                raise WorkflowReplayContractError(
                    "missing_handoff",
                    "$.scoring_handoff",
                    "Evaluation settings require a published scoring handoff",
                )
            handoff = validate_scoring_handoff_v1(
                _read_exact_json(handoff_path)
            )
            accepted = validate_evaluation_workflow_settings_v1(
                settings,
                scoring_handoff=handoff,
            )
            path = (
                self.root
                / "handoffs"
                / "evaluation_workflow_settings.json"
            )
            _write_json_absent_or_equal(path, accepted)
            self._write_relay_artifact(
                {
                    "binding": {
                        "artifact_ref": (
                            "handoffs/evaluation_workflow_settings.json"
                        ),
                        "artifact_kind": "evaluation_workflow_settings_v1",
                        "schema_version": accepted["schema_version"],
                        "sha256": accepted["settings_sha256"],
                        "sha256_kind": (
                            "canonical:EvaluationWorkflowSettingsV1@1.1.0"
                        ),
                    },
                    "physical_sha256": physical_sha256(path.read_bytes()),
                    "producer_component_id": "neutral_relay",
                    "producer_component_run_id": self.workflow_run_id,
                    "producer_component_attempt_id": None,
                    "producer_component_attempt_index": None,
                    "producer_stage_id": "relay.evaluation_settings",
                    "parent_artifact_refs": [
                        "handoffs/scoring_handoff.json"
                    ],
                    "created_event_id": None,
                }
            )
            self._project()
        return accepted

    def accept_scoring_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        with self._exclusive():
            handoff_path = self.root / "handoffs" / "scoring_handoff.json"
            if not handoff_path.is_file():
                raise WorkflowReplayContractError(
                    "missing_handoff", "$.scoring_handoff", "scoring handoff is not published"
                )
            handoff = validate_scoring_handoff_v1(_read_exact_json(handoff_path))
            accepted = validate_scoring_receipt_v1(receipt, handoff=handoff)
            path = self.root / "handoffs" / "scoring_receipt.json"
            _write_json_absent_or_equal(path, accepted)
            self._write_relay_artifact(
                {
                    "binding": {
                        "artifact_ref": "handoffs/scoring_receipt.json",
                        "artifact_kind": "scoring_receipt_v1",
                        "schema_version": SCHEMA_VERSION_V1,
                        "sha256": accepted["integrity"]["receipt_sha256"],
                        "sha256_kind": "canonical:ScoringReceiptV1@1.0.0",
                    },
                    "physical_sha256": physical_sha256(path.read_bytes()),
                    "producer_component_id": "evaluation",
                    "producer_component_run_id": accepted["evaluation_component_run_id"],
                    "producer_component_attempt_id": accepted[
                        "evaluation_component_attempt_id"
                    ],
                    "producer_component_attempt_index": _attempt_index_from_id(
                        accepted["evaluation_component_attempt_id"]
                    ),
                    "producer_stage_id": "evaluation.scoring_receipt",
                    "parent_artifact_refs": ["handoffs/scoring_handoff.json"],
                    "created_event_id": None,
                }
            )
            self._project()
            return accepted

    def recover(self) -> dict[str, Any]:
        with self._exclusive():
            self._project()
            return self.load_manifest()

    def load_manifest(self) -> dict[str, Any]:
        path = self.root / "workflow_manifest.json"
        if not path.is_file():
            raise WorkflowReplayContractError(
                "missing_manifest", "$.workflow_manifest", "parent manifest does not exist"
            )
        return validate_workflow_manifest_v1(_read_exact_json(path))

    def validate_parent_package(self) -> dict[str, Any]:
        """Rederive and validate every current parent projection from immutable imports."""

        config = _read_relay_config(self.root)
        expected_identity = {
            "workflow_run_id": self.workflow_run_id,
            "job_id": self.job_id,
            "code_commit": self.code_commit,
            "reconstructed": self.reconstructed,
            "created_at": self.created_at,
            "source_package_bindings": self.source_package_bindings,
            "stages": self.stages,
        }
        observed_identity = {key: config[key] for key in expected_identity}
        if observed_identity != expected_identity:
            raise WorkflowReplayContractError(
                "relay_config_drift",
                "$.relay_config",
                "open relay identity differs from the sealed root configuration",
            )

        imports = self._load_imports()
        self._validate_import_materialization(imports)
        documents = self._build_projection_documents(
            imports, materialize_artifacts=False
        )
        expected_files = {
            "events.jsonl": documents["events_jsonl"],
            "artifact_index.json": canonical_json_bytes(documents["artifact_index"]),
            "workflow_manifest.json": canonical_json_bytes(documents["manifest"]),
        }
        for relative, expected_bytes in expected_files.items():
            observed = _read_regular_bytes(self.root / relative)
            if observed != expected_bytes:
                raise WorkflowReplayContractError(
                    "parent_projection_drift",
                    f"$.{relative}",
                    "current parent projection differs from immutable import records",
                )

        expected_event_names: set[str] = set()
        event_by_id: dict[str, dict[str, Any]] = {}
        for event, encoded in zip(documents["events"], documents["event_bytes"]):
            event_sha = event["integrity"]["event_sha256"]
            name = f"{event['seq']:08d}_{event_sha}.json"
            expected_event_names.add(name)
            if _read_regular_bytes(self.root / "event_records" / name) != encoded:
                raise WorkflowReplayContractError(
                    "event_record_drift",
                    f"$.event_records.{name}",
                    "immutable parent event record differs from its current projection",
                )
            event_by_id[event["event_id"]] = event
        actual_event_names = {
            path.name for path in (self.root / "event_records").glob("*.json")
        }
        if actual_event_names != expected_event_names:
            raise WorkflowReplayContractError(
                "event_record_exact_cover",
                "$.event_records",
                "immutable parent event records do not exact-cover the current chain",
            )

        manifest = documents["manifest"]
        manifest_sha = manifest["integrity"]["manifest_sha256"]
        revision = self.root / "manifest_revisions" / f"{manifest_sha}.json"
        if _read_regular_bytes(revision) != canonical_json_bytes(manifest):
            raise WorkflowReplayContractError(
                "manifest_revision_drift",
                "$.manifest_revisions",
                "current manifest revision bytes differ",
            )

        handoff: dict[str, Any] | None = None
        indexed_refs = {
            row["binding"]["artifact_ref"]
            for row in documents["artifact_index"]["artifacts"]
        }
        for index, artifact in enumerate(documents["artifact_index"]["artifacts"]):
            binding = artifact["binding"]
            artifact_path = _resolve_under(self.root, binding["artifact_ref"])
            artifact_bytes = _read_regular_bytes(artifact_path)
            observed_physical = physical_sha256(artifact_bytes)
            if observed_physical != artifact["imported_physical_sha256"]:
                raise WorkflowReplayContractError(
                    "artifact_physical_drift",
                    f"$.artifact_index.artifacts[{index}]",
                    "indexed parent artifact bytes differ from the imported receipt",
                )
            if binding["sha256_kind"] == "physical" and binding["sha256"] != observed_physical:
                raise WorkflowReplayContractError(
                    "artifact_binding_drift",
                    f"$.artifact_index.artifacts[{index}].binding.sha256",
                    "physical binding differs from parent artifact bytes",
                )
            created_event_id = artifact["created_event_id"]
            if created_event_id is not None:
                event = event_by_id.get(created_event_id)
                if event is None:
                    raise WorkflowReplayContractError(
                        "unknown_event",
                        f"$.artifact_index.artifacts[{index}].created_event_id",
                        "artifact creator event is absent from the parent chain",
                    )
                producer = artifact["producer"]
                if (
                    event["component"]["component_id"] != producer["component_id"]
                    or event["component"]["component_run_id"]
                    != producer["component_run_id"]
                ):
                    raise WorkflowReplayContractError(
                        "artifact_event_lineage",
                        f"$.artifact_index.artifacts[{index}]",
                        "artifact producer differs from its creator event",
                    )
            if binding["artifact_ref"] == "handoffs/scoring_handoff.json":
                handoff = validate_scoring_handoff_v1(json.loads(artifact_bytes))
            elif binding["artifact_ref"] == "handoffs/scoring_receipt.json":
                if handoff is None:
                    handoff_path = self.root / "handoffs" / "scoring_handoff.json"
                    handoff = validate_scoring_handoff_v1(_read_exact_json(handoff_path))
                validate_scoring_receipt_v1(json.loads(artifact_bytes), handoff=handoff)

        if handoff is not None:
            self._validate_scoring_handoff_artifact_lineage(
                handoff, documents["artifact_index"]
            )

        artifact_by_ref = {
            row["binding"]["artifact_ref"]: row
            for row in documents["artifact_index"]["artifacts"]
        }

        for index, component in enumerate(manifest["components"]):
            binding = component["manifest"]
            if binding["artifact_ref"] in indexed_refs:
                raise WorkflowReplayContractError(
                    "component_manifest_authority",
                    f"$.manifest.components[{index}].manifest",
                    "component snapshot manifests are not parent output artifacts",
                )
            manifest_bytes = _read_regular_bytes(
                _resolve_under(self.root, binding["artifact_ref"])
            )
            if physical_sha256(manifest_bytes) != binding["sha256"]:
                raise WorkflowReplayContractError(
                    "component_manifest_drift",
                    f"$.manifest.components[{index}].manifest",
                    "component manifest bytes differ from the validated snapshot",
                )

        for index, stage in enumerate(manifest["stages"]):
            for artifact_ref in stage["artifact_refs"]:
                artifact = artifact_by_ref.get(artifact_ref)
                if artifact is None or artifact["producer"]["stage_id"] != stage["stage_id"]:
                    raise WorkflowReplayContractError(
                        "stage_artifact_lineage",
                        f"$.manifest.stages[{index}].artifact_refs",
                        "stage artifact reference has foreign producer lineage",
                    )
        return manifest

    def _validate_scoring_handoff_artifact_lineage(
        self,
        handoff: Mapping[str, Any],
        artifact_index: Mapping[str, Any],
    ) -> None:
        artifacts = {
            row["binding"]["artifact_ref"]: row
            for row in artifact_index["artifacts"]
        }
        for index, translation_input in enumerate(handoff["translation_inputs"]):
            if translation_input["producer"]["component_id"] != "translation":
                continue
            binding = translation_input["translation_artifact"]
            indexed = artifacts.get(binding["artifact_ref"])
            if indexed is None or indexed["binding"] != binding:
                raise WorkflowReplayContractError(
                    "scoring_input_lineage",
                    f"$.scoring_handoff.translation_inputs[{index}]",
                    "D2L scoring input does not bind an exact imported parent artifact",
                )
        for name in ("glossary", "context"):
            binding = handoff["optional_bindings"][name]
            if binding is None:
                continue
            indexed = artifacts.get(binding["artifact_ref"])
            if indexed is None or indexed["binding"] != binding:
                raise WorkflowReplayContractError(
                    "scoring_optional_lineage",
                    f"$.scoring_handoff.optional_bindings.{name}",
                    "D2L optional binding does not match an imported parent artifact",
                )

    def _validate_import_materialization(
        self, imports: Sequence[Mapping[str, Any]]
    ) -> None:
        for index, record in enumerate(imports):
            snapshot = copy.deepcopy(dict(record))
            snapshot.pop("import_sha256", None)
            snapshot.pop("acceptance_ordinal", None)
            snapshot.pop("accepted_at", None)
            snapshot["schema_id"] = "ValidatedComponentSnapshotV1"
            snapshot["schema_version"] = SCHEMA_VERSION_V1
            snapshot_sha = snapshot.pop("snapshot_sha256", None)
            if not isinstance(snapshot_sha, str) or canonical_sha256(snapshot) != snapshot_sha:
                raise WorkflowReplayContractError(
                    "snapshot_hash",
                    f"$.relay_imports[{index}].snapshot_sha256",
                    "validated component snapshot hash drift",
                )
            snapshot_root = (
                self.root
                / "components"
                / record["component_id"]
                / record["component_run_id"]
                / "snapshots"
                / snapshot_sha
            )
            for file_index, file_row in enumerate(record["files"]):
                file_path = _resolve_under(snapshot_root, file_row["relative_path"])
                observed = physical_sha256(_read_regular_bytes(file_path))
                if observed != file_row["physical_sha256"]:
                    raise WorkflowReplayContractError(
                        "component_snapshot_file_drift",
                        f"$.relay_imports[{index}].files[{file_index}]",
                        "materialized component snapshot bytes differ",
                    )
            events_path = _resolve_under(snapshot_root, "events.jsonl")
            event_lines = _read_regular_bytes(events_path).splitlines(keepends=True)
            if len(event_lines) != len(record["events"]):
                raise WorkflowReplayContractError(
                    "component_event_exact_cover",
                    f"$.relay_imports[{index}].events",
                    "component event file and validated snapshot differ in length",
                )
            for event_index, (line, event) in enumerate(zip(event_lines, record["events"])):
                if not line.endswith(b"\n") or physical_sha256(line[:-1]) != event["source_event_sha256"]:
                    raise WorkflowReplayContractError(
                        "component_event_drift",
                        f"$.relay_imports[{index}].events[{event_index}]",
                        "component event bytes differ from the owning validator receipt",
                    )

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise WorkflowReplayContractError(
                "unsafe_root", "$.root", "relay root must not be a symlink"
            )
        for relative in (
            "relay_imports",
            "relay_artifacts",
            "event_records",
            "manifest_revisions",
            "components",
            "handoffs",
        ):
            path = self.root / relative
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise WorkflowReplayContractError(
                    "unsafe_root", f"$.root.{relative}", "relay directory must not be a symlink"
                )

    @contextmanager
    def _exclusive(self):
        with self._lock:
            lock_path = self.root / ".relay.lock"
            lock_path.touch(exist_ok=True)
            with lock_path.open("r+b") as handle:
                _os_lock(handle)
                try:
                    yield
                finally:
                    _os_unlock(handle)

    def _write_or_validate_config(self) -> None:
        config = {
            "schema_id": "WorkflowRelayConfigV1",
            "schema_version": SCHEMA_VERSION_V1,
            "workflow_run_id": self.workflow_run_id,
            "flow_kind": FLOW_KIND_V1,
            "job_id": self.job_id,
            "source_package_bindings": self.source_package_bindings,
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "component_id": stage.component_id,
                    "local_stage_id": stage.local_stage_id,
                    "order": stage.order,
                    "label": stage.label,
                    "producer": stage.producer,
                }
                for stage in self.stages
            ],
            "code_commit": self.code_commit,
            "reconstructed": self.reconstructed,
            "created_at": self.created_at,
        }
        config["config_sha256"] = canonical_sha256(config)
        _write_json_absent_or_equal(self.root / "relay_config.json", config)

    def _normalize_snapshot(
        self, snapshot: ComponentSnapshotV1, *, source_root: Path
    ) -> dict[str, Any]:
        if not source_root.is_dir() or source_root.is_symlink():
            raise WorkflowReplayContractError(
                "component_root", "$.component_root", "expected a real component directory"
            )
        workflow_run_id = _identifier(snapshot.workflow_run_id, "snapshot.workflow_run_id")
        if workflow_run_id != self.workflow_run_id:
            raise WorkflowReplayContractError(
                "workflow_identity", "snapshot.workflow_run_id", "foreign workflow"
            )
        component_id = _enum(snapshot.component_id, set(COMPONENT_IDS_V1), "snapshot.component_id")
        component_run_id = _identifier(snapshot.component_run_id, "snapshot.component_run_id")
        attempt_id = _attempt_id(snapshot.component_attempt_id, "snapshot.component_attempt_id")
        if snapshot.component_attempt_index is None:
            if isinstance(attempt_id, str):
                raise WorkflowReplayContractError(
                    "attempt_identity",
                    "snapshot.component_attempt_index",
                    "string attempt IDs require an explicit numeric attempt index",
                )
            attempt_index = attempt_id
        else:
            attempt_index = _integer(
                snapshot.component_attempt_index,
                "snapshot.component_attempt_index",
                minimum=1,
            )
        status = _enum(snapshot.status, set(_COMPONENT_STATUS), "snapshot.status")
        validator_id = _identifier(snapshot.validator_id, "snapshot.validator_id")
        validator_revision = _identifier(
            snapshot.validator_revision, "snapshot.validator_revision"
        )
        validation_receipt_sha256 = _sha256(
            snapshot.validation_receipt_sha256, "snapshot.validation_receipt_sha256"
        )
        files: list[dict[str, str]] = []
        file_paths: set[str] = set()
        for index, item in enumerate(snapshot.files):
            relative = _relative_path(item.relative_path, f"snapshot.files[{index}].relative_path")
            if relative in file_paths:
                raise WorkflowReplayContractError(
                    "duplicate_file", f"snapshot.files[{index}]", "component file repeats"
                )
            file_paths.add(relative)
            expected_sha = _sha256(item.physical_sha256, f"snapshot.files[{index}].physical_sha256")
            source = _resolve_component_file(source_root, relative)
            observed_sha = physical_sha256(source.read_bytes())
            if observed_sha != expected_sha:
                raise WorkflowReplayContractError(
                    "component_file_hash",
                    f"snapshot.files[{index}]",
                    "component file bytes differ from validated snapshot",
                )
            files.append({"relative_path": relative, "physical_sha256": expected_sha})
        if not _MANDATORY_COMPONENT_FILES.issubset(file_paths):
            missing = sorted(_MANDATORY_COMPONENT_FILES - file_paths)
            raise WorkflowReplayContractError(
                "component_file_exact_cover",
                "snapshot.files",
                f"missing mandatory files: {', '.join(missing)}",
            )

        events: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        previous_attempt_index = 1
        attempt_ids_by_index: dict[int, str | int] = {}
        for index, item in enumerate(snapshot.events):
            path = f"snapshot.events[{index}]"
            if not isinstance(item.source_bytes, bytes) or not item.source_bytes:
                raise WorkflowReplayContractError("source_event_bytes", path, "event bytes required")
            try:
                decoded = json.loads(item.source_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkflowReplayContractError(
                    "source_event_bytes", path, "event bytes are not one JSON object"
                ) from exc
            if decoded != dict(item.value):
                raise WorkflowReplayContractError(
                    "source_event_bytes", path, "event bytes do not encode the supplied event"
                )
            row = item.value
            required = {
                "event_id",
                "workflow_run_id",
                "component_id",
                "component_run_id",
                "component_attempt_id",
                "component_seq",
                "ts",
                "stage_id",
                "agent",
                "event",
                "severity",
                "payload",
            }
            missing = sorted(required - set(row))
            if missing:
                raise WorkflowReplayContractError(
                    "missing_keys", path, f"missing common event fields: {', '.join(missing)}"
                )
            if "seq" in row:
                raise WorkflowReplayContractError(
                    "global_sequence_authority", f"{path}.seq", "component cannot assign parent seq"
                )
            event_id = _identifier(row["event_id"], f"{path}.event_id")
            if event_id in event_ids:
                raise WorkflowReplayContractError("duplicate_event_id", path, "event ID repeats")
            event_ids.add(event_id)
            if row["workflow_run_id"] != workflow_run_id or row["component_id"] != component_id or row["component_run_id"] != component_run_id:
                raise WorkflowReplayContractError(
                    "event_identity", path, "event identity differs from component snapshot"
                )
            event_attempt_id = _attempt_id(
                row["component_attempt_id"], f"{path}.component_attempt_id"
            )
            if "component_attempt_index" in row:
                event_attempt_index = _integer(
                    row["component_attempt_index"],
                    f"{path}.component_attempt_index",
                    minimum=1,
                )
            elif isinstance(event_attempt_id, int):
                event_attempt_index = event_attempt_id
            else:
                raise WorkflowReplayContractError(
                    "attempt_identity",
                    f"{path}.component_attempt_index",
                    "string attempt IDs require a numeric attempt index",
                )
            prior_attempt_id = attempt_ids_by_index.setdefault(
                event_attempt_index, event_attempt_id
            )
            if prior_attempt_id != event_attempt_id:
                raise WorkflowReplayContractError(
                    "attempt_identity", path, "one attempt index has multiple attempt IDs"
                )
            if event_attempt_index > attempt_index:
                raise WorkflowReplayContractError(
                    "attempt_identity", path, "event belongs to a future attempt"
                )
            if index == 0 and event_attempt_index != 1:
                raise WorkflowReplayContractError(
                    "attempt_lineage", path, "component event stream must begin at attempt 1"
                )
            if (
                event_attempt_index < previous_attempt_index
                or event_attempt_index > previous_attempt_index + 1
            ):
                raise WorkflowReplayContractError(
                    "attempt_lineage", path, "component attempt sequence is not contiguous"
                )
            event_name = _identifier(row["event"], f"{path}.event")
            if (
                event_attempt_index == previous_attempt_index + 1
                and event_name not in _RESUME_EVENTS
            ):
                raise WorkflowReplayContractError(
                    "attempt_lineage", path, "new attempt must begin with an explicit resume event"
                )
            previous_attempt_index = event_attempt_index
            component_seq = _integer(row["component_seq"], f"{path}.component_seq", minimum=1)
            if component_seq != index + 1:
                raise WorkflowReplayContractError(
                    "component_sequence", f"{path}.component_seq", "expected contiguous sequence from 1"
                )
            stage = row["stage_id"]
            projected_stage_id: str | None
            if stage is None or stage == "__component__":
                projected_stage_id = None
            else:
                local_stage = _identifier(stage, f"{path}.stage_id")
                definition = self._stage_by_component_local.get((component_id, local_stage))
                if definition is None:
                    raise WorkflowReplayContractError(
                        "unknown_stage", f"{path}.stage_id", "stage is not declared by parent workflow"
                    )
                projected_stage_id = definition.stage_id
            source_sha = physical_sha256(item.source_bytes)
            public_payload = validate_parent_event_public_payload(dict(item.public_payload))
            events.append(
                {
                    "source_event_id": event_id,
                    "source_event_sha256": source_sha,
                    "source_event_sha256_kind": "physical",
                    "component_attempt_id": event_attempt_id,
                    "component_attempt_index": event_attempt_index,
                    "component_seq": component_seq,
                    "ts": _timestamp(row["ts"], f"{path}.ts"),
                    "stage_id": projected_stage_id,
                    "local_stage_id": stage,
                    "agent": _identifier(row["agent"], f"{path}.agent"),
                    "event": event_name,
                    "severity": _enum(row["severity"], set(_SEVERITIES), f"{path}.severity"),
                    "payload": public_payload,
                }
            )
        if not events:
            raise WorkflowReplayContractError(
                "component_event_exact_cover", "snapshot.events", "component event stream is empty"
            )
        if max(item["component_attempt_index"] for item in events) != attempt_index:
            raise WorkflowReplayContractError(
                "attempt_identity",
                "snapshot.component_attempt_id",
                "current attempt has no event evidence",
            )
        if attempt_ids_by_index[attempt_index] != attempt_id:
            raise WorkflowReplayContractError(
                "attempt_identity",
                "snapshot.component_attempt_id",
                "current manifest attempt ID differs from event evidence",
            )
        last_event = events[-1]["event"]
        if status in _TERMINAL_STATUS and last_event not in _TERMINAL_EVENTS:
            raise WorkflowReplayContractError(
                "terminal_status",
                "snapshot.status",
                "terminal status requires a terminal last event",
            )
        if status not in _TERMINAL_STATUS and last_event in _TERMINAL_EVENTS:
            raise WorkflowReplayContractError(
                "terminal_status",
                "snapshot.status",
                "nonterminal status cannot carry a terminal last event",
            )

        artifacts: list[dict[str, Any]] = []
        artifact_refs: set[str] = set()
        for index, item in enumerate(snapshot.artifacts):
            path = f"snapshot.artifacts[{index}]"
            binding = validate_typed_artifact_binding_v1(item.binding, path=f"{path}.binding")
            component_ref = binding["artifact_ref"]
            if component_ref in artifact_refs:
                raise WorkflowReplayContractError("duplicate_artifact_ref", path, "artifact ref repeats")
            artifact_refs.add(component_ref)
            source_relative = _relative_path(item.source_relative_path, f"{path}.source_relative_path")
            if source_relative not in file_paths:
                raise WorkflowReplayContractError(
                    "artifact_file", path, "artifact source is absent from validated component files"
                )
            source = _resolve_component_file(source_root, source_relative)
            imported_physical_sha = physical_sha256(source.read_bytes())
            if binding["sha256_kind"] == "physical" and binding["sha256"] != imported_physical_sha:
                raise WorkflowReplayContractError(
                    "artifact_hash", path, "physical artifact binding differs from source bytes"
                )
            producer_stage = self._stage_by_component_local.get(
                (component_id, item.producer_stage_id)
            )
            if producer_stage is None:
                raise WorkflowReplayContractError(
                    "unknown_stage", f"{path}.producer_stage_id", "artifact producer stage is unknown"
                )
            created_event_id = _identifier(item.created_event_id, f"{path}.created_event_id")
            if created_event_id not in event_ids:
                raise WorkflowReplayContractError(
                    "unknown_event", f"{path}.created_event_id", "artifact creator event is unknown"
                )
            parent_refs = [
                _relative_path(parent, f"{path}.parent_artifact_refs[{i}]")
                for i, parent in enumerate(item.parent_artifact_refs)
            ]
            if len(parent_refs) != len(set(parent_refs)) or component_ref in parent_refs:
                raise WorkflowReplayContractError(
                    "artifact_parent", f"{path}.parent_artifact_refs", "invalid artifact parents"
                )
            artifacts.append(
                {
                    "component_artifact_ref": component_ref,
                    "binding": binding,
                    "source_relative_path": source_relative,
                    "imported_physical_sha256": imported_physical_sha,
                    "producer_stage_id": producer_stage.stage_id,
                    "parent_artifact_refs": parent_refs,
                    "created_event_id": created_event_id,
                }
            )
        unknown_parents = sorted(
            {
                parent
                for artifact in artifacts
                for parent in artifact["parent_artifact_refs"]
                if parent not in artifact_refs
            }
        )
        if unknown_parents:
            raise WorkflowReplayContractError(
                "artifact_parent", "snapshot.artifacts", "unknown parent refs: " + ", ".join(unknown_parents)
            )
        _reject_component_artifact_cycles(artifacts)

        normalized = {
            "schema_id": "ValidatedComponentSnapshotV1",
            "schema_version": SCHEMA_VERSION_V1,
            "workflow_run_id": workflow_run_id,
            "component_flow_kind": _identifier(
                snapshot.component_flow_kind, "snapshot.component_flow_kind"
            ),
            "component_id": component_id,
            "component_run_id": component_run_id,
            "component_attempt_id": attempt_id,
            "component_attempt_index": attempt_index,
            "status": status,
            "validator": {
                "validator_id": validator_id,
                "validator_revision": validator_revision,
                "validation_receipt_sha256": validation_receipt_sha256,
            },
            "files": files,
            "events": events,
            "artifacts": artifacts,
        }
        normalized["snapshot_sha256"] = canonical_sha256(normalized)
        return normalized

    def _materialize_component_files(
        self, normalized: Mapping[str, Any], *, source_root: Path
    ) -> None:
        snapshot_sha = normalized["snapshot_sha256"]
        destination_root = (
            self.root
            / "components"
            / normalized["component_id"]
            / normalized["component_run_id"]
            / "snapshots"
            / snapshot_sha
        )
        for row in normalized["files"]:
            source = _resolve_component_file(source_root, row["relative_path"])
            destination = _resolve_under(destination_root, row["relative_path"])
            _write_bytes_absent_or_equal(destination, source.read_bytes())

    def _preview_import(
        self,
        normalized: Mapping[str, Any],
        imports: Sequence[Mapping[str, Any]],
        *,
        accepted_at: str | None = None,
    ) -> dict[str, Any]:
        ordinal = len(imports) + 1
        record = copy.deepcopy(dict(normalized))
        record.pop("schema_id", None)
        record.pop("schema_version", None)
        record = {
            "schema_id": "WorkflowComponentImportV1",
            "schema_version": SCHEMA_VERSION_V1,
            "acceptance_ordinal": ordinal,
            "accepted_at": _timestamp(accepted_at or self._clock(), "accepted_at"),
            **record,
        }
        record["import_sha256"] = canonical_sha256(record)
        return record

    def _load_imports(self) -> list[dict[str, Any]]:
        imports = []
        for path in sorted((self.root / "relay_imports").glob("*.json")):
            row = _read_exact_json(path)
            if not isinstance(row, dict):
                raise WorkflowReplayContractError("type", str(path), "import record must be object")
            digest = row.get("import_sha256")
            payload = dict(row)
            payload.pop("import_sha256", None)
            if not isinstance(digest, str) or canonical_sha256(payload) != digest.lower():
                raise WorkflowReplayContractError("import_hash", str(path), "import record hash drift")
            expected_name = f"{row.get('acceptance_ordinal', 0):08d}_{digest.lower()}.json"
            if path.name != expected_name:
                raise WorkflowReplayContractError(
                    "import_identity", str(path), "import filename differs from sealed identity"
                )
            imports.append(row)
        imports.sort(key=lambda row: row["acceptance_ordinal"])
        for index, row in enumerate(imports, start=1):
            if row["acceptance_ordinal"] != index:
                raise WorkflowReplayContractError(
                    "import_sequence", "$.relay_imports", "import ordinals must be contiguous"
                )
        self._validate_union(imports)
        return imports

    def _validate_union(self, imports: Sequence[Mapping[str, Any]]) -> None:
        component_runs: dict[str, str] = {}
        events: dict[tuple[str, str, str], str] = {}
        last_seq: dict[tuple[str, str], int] = {}
        terminal: set[tuple[str, str]] = set()
        artifacts: dict[tuple[str, str, str], tuple[str, str]] = {}
        for record in imports:
            component_id = record["component_id"]
            component_run_id = record["component_run_id"]
            previous_run = component_runs.setdefault(component_id, component_run_id)
            if previous_run != component_run_id:
                raise WorkflowReplayContractError(
                    "component_run_identity",
                    "$.relay_imports",
                    "one parent workflow cannot silently replace a component run",
                )
            run_key = (component_id, component_run_id)
            for event in record["events"]:
                event_key = (component_id, component_run_id, event["source_event_id"])
                prior_sha = events.get(event_key)
                if prior_sha is not None:
                    if prior_sha != event["source_event_sha256"]:
                        raise WorkflowReplayContractError(
                            "event_id_reuse", "$.relay_imports", "event ID reused with unequal bytes"
                        )
                    continue
                if run_key in terminal:
                    raise WorkflowReplayContractError(
                        "terminal_append", "$.relay_imports", "event appended after terminal component"
                    )
                expected = last_seq.get(run_key, 0) + 1
                if event["component_seq"] != expected:
                    raise WorkflowReplayContractError(
                        "component_sequence", "$.relay_imports", "component sequence gap or duplicate"
                    )
                events[event_key] = event["source_event_sha256"]
                last_seq[run_key] = expected
                if event["event"] in _TERMINAL_EVENTS:
                    terminal.add(run_key)
            for artifact in record["artifacts"]:
                key = (component_id, component_run_id, artifact["component_artifact_ref"])
                identity = (
                    artifact["binding"]["sha256"],
                    artifact["imported_physical_sha256"],
                )
                prior = artifacts.setdefault(key, identity)
                if prior != identity:
                    raise WorkflowReplayContractError(
                        "artifact_ref_reuse",
                        "$.relay_imports",
                        "artifact ref reused with unequal validated bytes",
                    )

    def _project(self, *, imports: Sequence[Mapping[str, Any]] | None = None) -> None:
        records = list(imports) if imports is not None else self._load_imports()
        documents = self._build_projection_documents(records, materialize_artifacts=True)
        for event, encoded in zip(documents["events"], documents["event_bytes"]):
            event_sha = event["integrity"]["event_sha256"]
            _write_bytes_absent_or_equal(
                self.root / "event_records" / f"{event['seq']:08d}_{event_sha}.json",
                encoded,
            )
        _atomic_write_bytes(self.root / "events.jsonl", documents["events_jsonl"])
        _atomic_write_json(self.root / "artifact_index.json", documents["artifact_index"])
        manifest = documents["manifest"]
        manifest_sha = manifest["integrity"]["manifest_sha256"]
        _write_json_absent_or_equal(
            self.root / "manifest_revisions" / f"{manifest_sha}.json", manifest
        )
        _atomic_write_json(self.root / "workflow_manifest.json", manifest)

    def _build_projection_documents(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        materialize_artifacts: bool,
    ) -> dict[str, Any]:
        state = self._derive_projection(
            records, materialize_artifacts=materialize_artifacts
        )
        event_id_map = {
            (row["component_id"], row["component_run_id"], row["source_event_id"]):
            f"workflow_event_{seq:08d}"
            for seq, row in enumerate(state["events"], start=1)
        }
        for artifact in state["artifacts"]:
            source_event_id = artifact.pop("source_created_event_id")
            event_key = (
                artifact["producer"]["component_id"],
                artifact["producer"]["component_run_id"],
                source_event_id,
            )
            if event_key not in event_id_map:
                raise WorkflowReplayContractError(
                    "unknown_event", "$.artifact_index", "artifact creator event was not relayed"
                )
            artifact["created_event_id"] = event_id_map[event_key]
        events: list[dict[str, Any]] = []
        event_bytes: list[bytes] = []
        previous_event_sha: str | None = None
        for seq, source in enumerate(state["events"], start=1):
            event = {
                "schema_id": "WorkflowEventV1",
                "schema_version": SCHEMA_VERSION_V1,
                "event_id": f"workflow_event_{seq:08d}",
                "workflow_run_id": self.workflow_run_id,
                "flow_kind": FLOW_KIND_V1,
                "seq": seq,
                "accepted_at": source["accepted_at"],
                "component": {
                    "component_id": source["component_id"],
                    "component_run_id": source["component_run_id"],
                    "component_attempt_id": source["component_attempt_id"],
                    "component_attempt_index": source["component_attempt_index"],
                    "component_seq": source["component_seq"],
                    "source_event_id": source["source_event_id"],
                    "source_event_sha256": source["source_event_sha256"],
                    "source_event_sha256_kind": "physical",
                    "validator_id": source["validator_id"],
                    "validator_revision": source["validator_revision"],
                },
                "stage_id": source["stage_id"],
                "agent": source["agent"],
                "event": source["event"],
                "severity": source["severity"],
                "payload": source["payload"],
                "integrity": {
                    "previous_event_sha256": previous_event_sha,
                    "event_sha256": "0" * 64,
                },
            }
            payload = copy.deepcopy(event)
            payload["integrity"].pop("event_sha256")
            event_sha = canonical_sha256(payload)
            event["integrity"]["event_sha256"] = event_sha
            previous_event_sha = event_sha
            encoded = canonical_json_bytes(event)
            validate_workflow_event_v1(event)
            events.append(event)
            event_bytes.append(encoded)

        artifact_index = {
            "schema_id": "WorkflowArtifactIndexV1",
            "schema_version": SCHEMA_VERSION_V1,
            "workflow_run_id": self.workflow_run_id,
            "flow_kind": FLOW_KIND_V1,
            "artifacts": state["artifacts"] + self._load_relay_artifacts(),
            "integrity": {"artifact_index_sha256": "0" * 64},
        }
        artifact_payload = copy.deepcopy(artifact_index)
        artifact_payload["integrity"].pop("artifact_index_sha256")
        artifact_index_sha = canonical_sha256(artifact_payload)
        artifact_index["integrity"]["artifact_index_sha256"] = artifact_index_sha
        validate_workflow_artifact_index_v1(artifact_index)

        stages = self._project_stages(state["events"], state["artifacts"])
        components = state["components"]
        status = _workflow_status(components)
        active_stage_id = next(
            (row["stage_id"] for row in reversed(stages) if row["status"] == "running"),
            None,
        )
        updated_at = state["events"][-1]["accepted_at"] if state["events"] else self.created_at
        manifest = {
            "schema_id": "WorkflowManifestV1",
            "schema_version": SCHEMA_VERSION_V1,
            "workflow_run_id": self.workflow_run_id,
            "flow_kind": FLOW_KIND_V1,
            "job_id": self.job_id,
            "source_package_bindings": self.source_package_bindings,
            "status": status,
            "started_at": self.created_at,
            "updated_at": updated_at,
            "active_stage_id": active_stage_id,
            "components": components,
            "stages": stages,
            "resume": {
                "available": any(row["status"] == "paused" for row in components),
                "component_id": next(
                    (row["component_id"] for row in components if row["status"] == "paused"),
                    None,
                ),
            },
            "reconstructed": self.reconstructed,
            "timing_authority": "logical_order_only" if self.reconstructed else "recorded",
            "latest_event_seq": len(state["events"]),
            "artifact_index_sha256": artifact_index_sha,
            "integrity": {"manifest_sha256": "0" * 64},
        }
        manifest_payload = copy.deepcopy(manifest)
        manifest_payload["integrity"].pop("manifest_sha256")
        manifest_sha = canonical_sha256(manifest_payload)
        manifest["integrity"]["manifest_sha256"] = manifest_sha
        validate_workflow_manifest_v1(manifest)
        return {
            "events": events,
            "event_bytes": event_bytes,
            "events_jsonl": b"".join(encoded + b"\n" for encoded in event_bytes),
            "artifact_index": artifact_index,
            "manifest": manifest,
        }

    def _derive_projection(
        self,
        imports: Sequence[Mapping[str, Any]],
        *,
        materialize_artifacts: bool = True,
    ) -> dict[str, Any]:
        self._validate_union(imports)
        seen_events: dict[tuple[str, str, str], str] = {}
        event_rows: list[dict[str, Any]] = []
        artifact_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        latest_components: dict[str, dict[str, Any]] = {}
        for record in imports:
            component_id = record["component_id"]
            component_run_id = record["component_run_id"]
            snapshot_root = (
                f"components/{component_id}/{component_run_id}/snapshots/"
                f"{record['snapshot_sha256']}"
            )
            manifest_file = next(
                row for row in record["files"] if row["relative_path"] == "component_manifest.json"
            )
            latest_components[component_id] = {
                "component_id": component_id,
                "component_run_id": component_run_id,
                "component_attempt_id": record["component_attempt_id"],
                "component_attempt_index": record["component_attempt_index"],
                "status": record["status"],
                "manifest": {
                    "artifact_ref": f"{snapshot_root}/component_manifest.json",
                    "artifact_kind": "component_manifest",
                    "schema_version": record["schema_version"],
                    "sha256": manifest_file["physical_sha256"],
                    "sha256_kind": "physical",
                },
                "last_component_seq": record["events"][-1]["component_seq"] if record["events"] else 0,
                "terminal": record["status"] in _TERMINAL_STATUS,
                "validator": record["validator"],
            }
            for event in record["events"]:
                key = (component_id, component_run_id, event["source_event_id"])
                prior = seen_events.get(key)
                if prior is not None:
                    continue
                seen_events[key] = event["source_event_sha256"]
                event_rows.append(
                    {
                        **copy.deepcopy(event),
                        "component_id": component_id,
                        "component_run_id": component_run_id,
                        "validator_id": record["validator"]["validator_id"],
                        "validator_revision": record["validator"]["validator_revision"],
                        "accepted_at": record["accepted_at"],
                        "acceptance_ordinal": record["acceptance_ordinal"],
                    }
                )
            for artifact in record["artifacts"]:
                key = (component_id, component_run_id, artifact["component_artifact_ref"])
                parent_ref = (
                    f"components/{component_id}/{component_run_id}/artifacts/"
                    f"{artifact['component_artifact_ref']}"
                )
                binding = copy.deepcopy(artifact["binding"])
                binding["artifact_ref"] = parent_ref
                artifact_rows.setdefault(
                    key,
                    {
                        "binding": binding,
                        "component_artifact_ref": artifact["component_artifact_ref"],
                        "imported_physical_sha256": artifact["imported_physical_sha256"],
                        "producer": {
                            "component_id": component_id,
                            "component_run_id": component_run_id,
                            "component_attempt_id": record["component_attempt_id"],
                            "component_attempt_index": record["component_attempt_index"],
                            "stage_id": artifact["producer_stage_id"],
                        },
                        "parent_artifact_refs": [
                            f"components/{component_id}/{component_run_id}/artifacts/{item}"
                            for item in artifact["parent_artifact_refs"]
                        ],
                        "source_created_event_id": artifact["created_event_id"],
                    },
                )
                source_relative = artifact["source_relative_path"]
                snapshot_source = self.root / snapshot_root / PurePosixPath(source_relative)
                stable_destination = self.root / PurePosixPath(parent_ref)
                if materialize_artifacts:
                    _write_bytes_absent_or_equal(
                        stable_destination, snapshot_source.read_bytes()
                    )
        if self.reconstructed:
            event_rows.sort(key=self._reconstruction_key)
        else:
            event_rows.sort(key=lambda row: (row["acceptance_ordinal"], row["component_seq"]))
        components = [
            latest_components[component_id]
            for component_id in COMPONENT_IDS_V1
            if component_id in latest_components
        ]
        return {
            "events": event_rows,
            "artifacts": [artifact_rows[key] for key in sorted(artifact_rows)],
            "components": components,
        }

    def _reconstruction_key(self, row: Mapping[str, Any]) -> tuple[int, int, str]:
        if row["stage_id"] is None:
            if row["event"] in _START_EVENTS:
                rank = -1
            elif row["event"] in _TERMINAL_EVENTS:
                rank = len(self.stages) + 1
            else:
                rank = len(self.stages)
        else:
            rank = self._stage_by_id[row["stage_id"]].order
        return rank, row["component_seq"], row["source_event_id"]

    def _project_stages(
        self,
        events: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        artifact_refs_by_stage: dict[str, list[str]] = {}
        for artifact in artifacts:
            stage_id = artifact["producer"]["stage_id"]
            artifact_refs_by_stage.setdefault(stage_id, []).append(
                artifact["binding"]["artifact_ref"]
            )
        projected = {
            definition.stage_id: {
                "stage_id": definition.stage_id,
                "component_id": definition.component_id,
                "local_stage_id": definition.local_stage_id,
                "order": definition.order,
                "label": definition.label,
                "producer": definition.producer,
                "status": "pending",
                "progress": None,
                "current_work_id": None,
                "artifact_refs": sorted(
                    artifact_refs_by_stage.get(definition.stage_id, [])
                ),
            }
            for definition in self.stages
        }

        for event in events:
            name = event["event"]
            stage_id = event["stage_id"]
            if stage_id is None:
                if name in {"component_halted", "run_paused"}:
                    for row in projected.values():
                        if (
                            row["component_id"] == event["component_id"]
                            and row["status"] == "running"
                        ):
                            row["status"] = "paused"
                elif name in {"component_failed", "run_failed"}:
                    for row in projected.values():
                        if (
                            row["component_id"] == event["component_id"]
                            and row["status"] == "running"
                        ):
                            row["status"] = "failed"
                            row["current_work_id"] = None
                continue

            row = projected[stage_id]
            payload = event["payload"]
            if name in {"stage_start", "stage_started"}:
                row["status"] = "running"
            elif name in {"stage_done", "stage_completed"}:
                outcome = payload.get("outcome")
                if outcome in {None, "succeeded", "skipped", "reused"}:
                    row["status"] = "succeeded"
                elif outcome in {"failed", "blocked", "cancelled"}:
                    row["status"] = "failed"
                else:
                    raise WorkflowReplayContractError(
                        "stage_outcome",
                        f"$.events[{event['component_seq']}].payload.outcome",
                        "stage completion outcome cannot be projected",
                    )
                row["current_work_id"] = None
            elif name == "stage_failed":
                row["status"] = "failed"
                row["current_work_id"] = None
            elif name == "stage_paused":
                row["status"] = "paused"

            if isinstance(payload.get("progress"), Mapping):
                row["progress"] = copy.deepcopy(dict(payload["progress"]))
            elif name == "progress" and all(
                key in payload for key in ("completed", "total", "unit")
            ):
                row["progress"] = {
                    "completed": copy.deepcopy(payload["completed"]),
                    "total": copy.deepcopy(payload["total"]),
                    "unit": copy.deepcopy(payload["unit"]),
                }
            elif name in {"stage_start", "stage_started"} and all(
                key in payload for key in ("work_total", "work_unit")
            ):
                row["progress"] = {
                    "completed": 0,
                    "total": copy.deepcopy(payload["work_total"]),
                    "unit": copy.deepcopy(payload["work_unit"]),
                }

            if "current_work_id" in payload:
                current_work_id = payload["current_work_id"]
                if current_work_id is not None and not isinstance(current_work_id, str):
                    raise WorkflowReplayContractError(
                        "current_work_id",
                        f"$.events[{event['component_seq']}].payload.current_work_id",
                        "current work ID must be a string or null",
                    )
                row["current_work_id"] = current_work_id
            elif name == "work_started" and isinstance(payload.get("work_id"), str):
                row["current_work_id"] = payload["work_id"]

            if row["status"] in {"failed", "succeeded"}:
                row["current_work_id"] = None

        return [projected[definition.stage_id] for definition in self.stages]

    def _write_relay_artifact(self, row: Mapping[str, Any]) -> None:
        binding = validate_typed_artifact_binding_v1(row["binding"], path="$.binding")
        record = {
            "binding": binding,
            "component_artifact_ref": binding["artifact_ref"],
            "imported_physical_sha256": _sha256(
                row["physical_sha256"], "$.physical_sha256"
            ),
            "producer": {
                "component_id": _identifier(
                    row["producer_component_id"], "$.producer_component_id"
                ),
                "component_run_id": _identifier(
                    row["producer_component_run_id"], "$.producer_component_run_id"
                ),
                "component_attempt_id": row["producer_component_attempt_id"],
                "component_attempt_index": row["producer_component_attempt_index"],
                "stage_id": _identifier(row["producer_stage_id"], "$.producer_stage_id"),
            },
            "parent_artifact_refs": [
                _relative_path(item, "$.parent_artifact_refs[*]")
                for item in row["parent_artifact_refs"]
            ],
            "created_event_id": row["created_event_id"],
        }
        record["record_sha256"] = canonical_sha256(record)
        _write_json_absent_or_equal(
            self.root / "relay_artifacts" / f"{record['record_sha256']}.json", record
        )

    def _load_relay_artifacts(self) -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / "relay_artifacts").glob("*.json")):
            row = _read_exact_json(path)
            if not isinstance(row, dict):
                raise WorkflowReplayContractError("type", str(path), "artifact record must be object")
            payload = dict(row)
            digest = payload.pop("record_sha256", None)
            if not isinstance(digest, str) or canonical_sha256(payload) != digest.lower():
                raise WorkflowReplayContractError(
                    "artifact_record_hash", str(path), "relay artifact record hash drift"
                )
            if path.name != f"{digest.lower()}.json":
                raise WorkflowReplayContractError(
                    "artifact_record_identity",
                    str(path),
                    "relay artifact filename differs from sealed identity",
                )
            binding = validate_typed_artifact_binding_v1(row["binding"], path=str(path))
            previous = result.setdefault(binding["artifact_ref"], payload)
            if previous != payload:
                raise WorkflowReplayContractError(
                    "artifact_ref_reuse", str(path), "relay artifact ref reused with unequal bytes"
                )
        return [result[key] for key in sorted(result)]


def validate_workflow_parent_package_v1(root: Path | str) -> dict[str, Any]:
    """Open and fully rederive an existing parent workflow package read-only."""

    return WorkflowRelayV1.open_existing(root).validate_parent_package()


def _read_relay_config(root: Path) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise WorkflowReplayContractError(
            "unsafe_root", "$.root", "existing relay root must be a real directory"
        )
    row = _read_exact_json(root / "relay_config.json")
    if not isinstance(row, Mapping):
        raise WorkflowReplayContractError(
            "type", "$.relay_config", "expected an object"
        )
    required = {
        "schema_id",
        "schema_version",
        "workflow_run_id",
        "flow_kind",
        "job_id",
        "source_package_bindings",
        "stages",
        "code_commit",
        "reconstructed",
        "created_at",
        "config_sha256",
    }
    if set(row) != required:
        raise WorkflowReplayContractError(
            "relay_config_shape",
            "$.relay_config",
            "relay configuration keys differ from V1",
        )
    payload = copy.deepcopy(dict(row))
    digest = _sha256(payload.pop("config_sha256"), "$.relay_config.config_sha256")
    if canonical_sha256(payload) != digest:
        raise WorkflowReplayContractError(
            "relay_config_hash",
            "$.relay_config.config_sha256",
            "relay configuration hash drift",
        )
    if row["schema_id"] != "WorkflowRelayConfigV1" or row["schema_version"] != SCHEMA_VERSION_V1:
        raise WorkflowReplayContractError(
            "relay_config_schema",
            "$.relay_config",
            "unsupported relay configuration schema",
        )
    if row["flow_kind"] != FLOW_KIND_V1:
        raise WorkflowReplayContractError(
            "flow_kind", "$.relay_config.flow_kind", "unexpected workflow flow kind"
        )
    stage_rows = row["stages"]
    if not isinstance(stage_rows, list):
        raise WorkflowReplayContractError(
            "type", "$.relay_config.stages", "expected an array"
        )
    stages: list[StageDefinitionV1] = []
    stage_keys = {
        "stage_id", "component_id", "local_stage_id", "order", "label", "producer"
    }
    for index, stage_row in enumerate(stage_rows):
        if not isinstance(stage_row, Mapping) or set(stage_row) != stage_keys:
            raise WorkflowReplayContractError(
                "stage_shape",
                f"$.relay_config.stages[{index}]",
                "relay stage keys differ from V1",
            )
        stages.append(
            StageDefinitionV1(
                stage_id=stage_row["stage_id"],
                component_id=stage_row["component_id"],
                local_stage_id=stage_row["local_stage_id"],
                order=stage_row["order"],
                label=stage_row["label"],
                producer=stage_row["producer"],
            )
        )
    return {
        "workflow_run_id": _identifier(
            row["workflow_run_id"], "$.relay_config.workflow_run_id"
        ),
        "job_id": _identifier(row["job_id"], "$.relay_config.job_id"),
        "code_commit": _commit(row["code_commit"], "$.relay_config.code_commit"),
        "reconstructed": _boolean(
            row["reconstructed"], "$.relay_config.reconstructed"
        ),
        "created_at": _timestamp(row["created_at"], "$.relay_config.created_at"),
        "source_package_bindings": validate_source_package_bindings_v1(
            row["source_package_bindings"]
        ),
        "stages": _validate_stages(tuple(stages)),
    }


def _validate_stages(value: Sequence[StageDefinitionV1]) -> tuple[StageDefinitionV1, ...]:
    if not value:
        raise WorkflowReplayContractError("stages", "$.stages", "at least one stage is required")
    normalized: list[StageDefinitionV1] = []
    ids: set[str] = set()
    local_ids: set[tuple[str, str]] = set()
    for index, stage in enumerate(value):
        path = f"$.stages[{index}]"
        component_id = _enum(stage.component_id, set(COMPONENT_IDS_V1), f"{path}.component_id")
        local_stage = _identifier(stage.local_stage_id, f"{path}.local_stage_id")
        stage_id = _identifier(stage.stage_id, f"{path}.stage_id")
        if not stage_id.startswith(component_id + "."):
            raise WorkflowReplayContractError(
                "stage_namespace", f"{path}.stage_id", "parent stage must be component-namespaced"
            )
        if stage_id in ids or (component_id, local_stage) in local_ids:
            raise WorkflowReplayContractError("duplicate_stage", path, "stage identity repeats")
        ids.add(stage_id)
        local_ids.add((component_id, local_stage))
        order = _integer(stage.order, f"{path}.order", minimum=1)
        if order != index + 1:
            raise WorkflowReplayContractError("stage_order", f"{path}.order", "orders must be contiguous")
        normalized.append(
            StageDefinitionV1(
                stage_id=stage_id,
                component_id=component_id,
                local_stage_id=local_stage,
                order=order,
                label=_string(stage.label, f"{path}.label"),
                producer=_identifier(stage.producer, f"{path}.producer"),
            )
        )
    return tuple(normalized)


def _reject_component_artifact_cycles(artifacts: Sequence[Mapping[str, Any]]) -> None:
    graph = {
        row["component_artifact_ref"]: tuple(row["parent_artifact_refs"])
        for row in artifacts
    }
    visited: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            raise WorkflowReplayContractError(
                "artifact_parent_cycle",
                "snapshot.artifacts",
                "component artifact parent graph contains a cycle",
            )
        if node in visited:
            return
        active.add(node)
        for parent in graph[node]:
            visit(parent)
        active.remove(node)
        visited.add(node)

    for artifact_ref in graph:
        visit(artifact_ref)


def _workflow_status(components: Sequence[Mapping[str, Any]]) -> str:
    statuses = {row["status"] for row in components}
    if "failed" in statuses:
        return "failed"
    if "paused" in statuses:
        return "paused"
    if len(components) == len(COMPONENT_IDS_V1) and statuses == {"succeeded"}:
        return "succeeded"
    if components:
        return "running"
    return "pending"


def _module_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _MODULE_LOCKS_GUARD:
        return _MODULE_LOCKS.setdefault(key, threading.RLock())


def _os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _os_unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _resolve_component_file(root: Path, relative: str) -> Path:
    path = _resolve_under(root, relative)
    if not path.is_file() or path.is_symlink():
        raise WorkflowReplayContractError(
            "component_file", relative, "component input must be a regular non-symlink file"
        )
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise WorkflowReplayContractError("component_file", relative, "special files are forbidden")
    return path


def _resolve_under(root: Path, relative: str) -> Path:
    target = (root / PurePosixPath(relative)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkflowReplayContractError("path_escape", relative, "path escapes its root") from exc
    return target


def _write_bytes_absent_or_equal(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise WorkflowReplayContractError(
                "immutable_collision", str(path), "existing immutable bytes differ"
            )
        return
    _atomic_write_bytes(path, data, replace=False)


def _write_json_absent_or_equal(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes_absent_or_equal(path, canonical_json_bytes(value))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, canonical_json_bytes(value))


def _atomic_write_bytes(path: Path, data: bytes, *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            raise WorkflowReplayContractError(
                "immutable_collision", str(path), "immutable destination already exists"
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_regular_bytes(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise WorkflowReplayContractError(
            "regular_file", str(path), "expected a regular non-symlink file"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WorkflowReplayContractError(
            "file_read", str(path), "unable to read artifact bytes"
        ) from exc


def _read_exact_json(path: Path) -> Any:
    data = _read_regular_bytes(path)
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise WorkflowReplayContractError(
            "json", str(path), "invalid JSON artifact"
        ) from exc
    if canonical_json_bytes(value) != data:
        raise WorkflowReplayContractError(
            "canonical_json", str(path), "artifact bytes are not canonical JSON"
        )
    return value


def _relative_path(value: Any, label: str) -> str:
    result = _string(value, label)
    if "\\" in result or (len(result) >= 2 and result[1] == ":"):
        raise WorkflowReplayContractError("relative_path", label, "portable relative path required")
    path = PurePosixPath(result)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowReplayContractError("relative_path", label, "unsafe relative path")
    return path.as_posix()


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowReplayContractError("string", label, "nonempty string required")
    return value


def _identifier(value: Any, label: str) -> str:
    result = _string(value, label)
    if len(result) > 192 or any(char.isspace() for char in result):
        raise WorkflowReplayContractError("identifier", label, "invalid identifier")
    return result


def _enum(value: Any, allowed: set[str], label: str) -> str:
    result = _string(value, label)
    if result not in allowed:
        raise WorkflowReplayContractError("enum", label, f"expected one of {sorted(allowed)}")
    return result


def _integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkflowReplayContractError("integer", label, f"expected integer >= {minimum}")
    return value


def _attempt_id(value: Any, label: str) -> str | int:
    if isinstance(value, bool):
        raise WorkflowReplayContractError("attempt_identity", label, "bool is not an attempt ID")
    if isinstance(value, int):
        return _integer(value, label, minimum=1)
    return _identifier(value, label)


def _attempt_index_from_id(value: str) -> int:
    suffix = value.rsplit("_", 1)[-1]
    if not suffix.isdigit() or int(suffix) < 1:
        raise WorkflowReplayContractError(
            "attempt_identity", "$.evaluation_component_attempt_id", "attempt ID has no numeric index"
        )
    return int(suffix)


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise WorkflowReplayContractError("boolean", label, "exact bool required")
    return value


def _sha256(value: Any, label: str) -> str:
    result = _string(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise WorkflowReplayContractError("sha256", label, "expected SHA-256")
    return result


def _commit(value: Any, label: str) -> str:
    result = _string(value, label).lower()
    if len(result) != 40 or any(char not in "0123456789abcdef" for char in result):
        raise WorkflowReplayContractError("commit", label, "expected full Git commit")
    return result


def _timestamp(value: Any, label: str) -> str:
    result = _string(value, label)
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowReplayContractError("timestamp", label, "expected RFC3339 timestamp") from exc
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "ComponentArtifactInputV1",
    "ComponentEventInputV1",
    "ComponentFileInputV1",
    "ComponentSnapshotV1",
    "StageDefinitionV1",
    "ValidatedComponentAdapterV1",
    "WorkflowRelayV1",
    "validate_workflow_parent_package_v1",
]
