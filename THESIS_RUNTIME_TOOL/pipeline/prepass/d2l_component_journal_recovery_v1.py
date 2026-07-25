"""Trusted-prefix recovery for a D2L journal publication race."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .d2l_component_stage_receipt_v1 import (
    observation_journal_state,
    read_observation_journal,
)
from .d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
    D2LComponentWriterLeaseError,
    D2LStageWriterLease,
)
from .d2l_console_replay_contract_v1 import (
    D2LTranslationComponentEventWriter,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    validate_artifact_index,
    validate_component_manifest,
    validate_translation_component_package,
)
from .d2l_stage_work_journal_v1 import (
    read_work_journal,
    work_journal_state,
)
from .d2l_translation_component_runner_v1 import (
    ComponentPlan,
    D2LTranslationComponentRunner,
)


REQUEST_SCHEMA = "d2l_component_journal_recovery_request_v1"
RECEIPT_SCHEMA = "d2l_component_journal_recovery_receipt_v1"
PREPARED_SCHEMA = "d2l_component_journal_recovery_prepared_v1"
TREE_INDEX_SCHEMA = "d2l_component_tree_index_v1"
RECOVERY_REASON = "journal_publication_race_recovered"
_SHA256_RE = re.compile(r"^[0-9A-F]{64}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_DELTA_EVENTS = (
    "request_sent",
    "response_received",
    "usage_snapshot",
    "validation_passed",
    "work_progress",
)


class D2LComponentJournalRecoveryError(RuntimeError):
    """Raised when journal recovery cannot be certified without inference."""


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest().upper()


def _require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").strip().upper()
    if not _SHA256_RE.fullmatch(normalized):
        raise D2LComponentJournalRecoveryError(
            f"{label} must be an uppercase SHA-256"
        )
    return normalized


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a positive integer"
        )
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a nonnegative integer"
        )
    return value


def _nonempty(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256:
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a bounded nonempty string"
        )
    return normalized


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a regular file"
        )
    return path


def _regular_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a regular directory"
        )
    return path


def _load_json_bytes(value: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D2LComponentJournalRecoveryError(
            f"{label} must be UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a JSON object"
        )
    return parsed


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    return _load_json_bytes(_regular_file(path, label).read_bytes(), label)


def _normalize_relative_path(value: Any, label: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or len(raw) > 4096:
        raise D2LComponentJournalRecoveryError(
            f"{label} must be a bounded relative path"
        )
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise D2LComponentJournalRecoveryError(
            f"{label} must be confined"
        )
    return str(path)


def _resolve_under(root: Path, relative: str, label: str) -> Path:
    normalized = _normalize_relative_path(relative, label)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate == resolved_root or resolved_root not in resolved_candidate.parents:
        raise D2LComponentJournalRecoveryError(f"{label} escapes its root")
    return resolved_candidate


def _write_absent_or_equal(path: Path, value: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != value:
            raise D2LComponentJournalRecoveryError(
                f"transaction file drift: {path.name}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _tree_index(root: Path) -> dict[str, Any]:
    _regular_directory(root, "tree root")
    rows: list[dict[str, Any]] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in sorted(names):
            child = current / name
            if child.is_symlink():
                raise D2LComponentJournalRecoveryError(
                    "component tree contains a symlink"
                )
        for filename in sorted(filenames):
            path = current / filename
            if path.is_symlink() or not path.is_file():
                raise D2LComponentJournalRecoveryError(
                    "component tree contains a non-regular file"
                )
            relative = str(path.relative_to(root)).replace("\\", "/")
            rows.append(
                {
                    "relative_path": relative,
                    "physical_sha256": file_sha256(path),
                    "byte_count": path.stat().st_size,
                }
            )
    rows.sort(key=lambda row: row["relative_path"])
    payload = {
        "schema": TREE_INDEX_SCHEMA,
        "files": rows,
    }
    payload["tree_sha256"] = canonical_sha256(payload)
    return payload


def _validate_tree_index(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    if set(row) != {"schema", "files", "tree_sha256"}:
        raise D2LComponentJournalRecoveryError(
            "tree index fields are invalid"
        )
    if row["schema"] != TREE_INDEX_SCHEMA:
        raise D2LComponentJournalRecoveryError(
            "tree index schema is invalid"
        )
    files = row["files"]
    if not isinstance(files, list):
        raise D2LComponentJournalRecoveryError(
            "tree index files must be a list"
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, Mapping) or set(item) != {
            "relative_path",
            "physical_sha256",
            "byte_count",
        }:
            raise D2LComponentJournalRecoveryError(
                f"tree index file {index} is invalid"
            )
        relative = _normalize_relative_path(
            item["relative_path"],
            f"tree index file {index}",
        )
        if relative in seen:
            raise D2LComponentJournalRecoveryError(
                "tree index has duplicate paths"
            )
        seen.add(relative)
        normalized.append(
            {
                "relative_path": relative,
                "physical_sha256": _require_sha256(
                    item["physical_sha256"],
                    f"tree index file {index} hash",
                ),
                "byte_count": _nonnegative_int(
                    item["byte_count"],
                    f"tree index file {index} byte_count",
                ),
            }
        )
    if normalized != sorted(normalized, key=lambda item: item["relative_path"]):
        raise D2LComponentJournalRecoveryError(
            "tree index files are not sorted"
        )
    observed = _require_sha256(row["tree_sha256"], "tree_sha256")
    expected_payload = {
        "schema": TREE_INDEX_SCHEMA,
        "files": normalized,
    }
    if canonical_sha256(expected_payload) != observed:
        raise D2LComponentJournalRecoveryError(
            "tree index hash drift"
        )
    expected_payload["tree_sha256"] = observed
    return expected_payload


def _verify_tree(root: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    observed = _tree_index(root)
    normalized = _validate_tree_index(expected)
    if observed != normalized:
        raise D2LComponentJournalRecoveryError("component tree hash drift")
    return observed


def _copy_tree_exact(
    source: Path,
    destination: Path,
    expected: Mapping[str, Any],
) -> None:
    normalized = _validate_tree_index(expected)
    if destination.exists():
        _verify_tree(destination, normalized)
        return
    if destination.is_symlink():
        raise D2LComponentJournalRecoveryError(
            "tree destination must not be a symlink"
        )
    temporary = destination.with_name(
        f".{destination.name}.{uuid4().hex}.tmp"
    )
    temporary.mkdir(parents=True)
    try:
        for item in normalized["files"]:
            source_path = _resolve_under(
                source,
                item["relative_path"],
                "tree source path",
            )
            _regular_file(source_path, "tree source file")
            if (
                file_sha256(source_path) != item["physical_sha256"]
                or source_path.stat().st_size != item["byte_count"]
            ):
                raise D2LComponentJournalRecoveryError(
                    "tree source file drift"
                )
            destination_path = _resolve_under(
                temporary,
                item["relative_path"],
                "tree destination path",
            )
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_bytes(source_path.read_bytes())
        _verify_tree(temporary, normalized)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _file_rows_by_path(index: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row["relative_path"]): dict(row)
        for row in index["files"]
    }


def _jsonl_lines(path: Path, label: str) -> list[bytes]:
    value = _regular_file(path, label).read_bytes()
    if value and not value.endswith(b"\n"):
        raise D2LComponentJournalRecoveryError(
            f"{label} must end with newline"
        )
    return value.splitlines(keepends=True)


def _component_event_rows(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(_jsonl_lines(path, label), start=1):
        row = _load_json_bytes(line.rstrip(b"\r\n"), f"{label} row {index}")
        expected = (
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        if line != expected:
            raise D2LComponentJournalRecoveryError(
                f"{label} row {index} differs from the component event encoding"
            )
        rows.append(row)
    return rows


@dataclass(frozen=True)
class D2LComponentJournalRecoveryRequestV1:
    component_root: Path
    transaction_root: Path
    relay_import_file: Path
    relay_import_physical_sha256: str
    relay_import_sha256: str
    relay_import_ordinal: int
    snapshot_root: Path
    snapshot_sha256: str
    component_plan_file: Path
    component_plan_physical_sha256: str
    expected_manifest_sha256: str
    expected_events_sha256: str
    expected_artifact_index_sha256: str
    expected_observation_journal_sha256: str
    expected_work_journal_sha256: str
    expected_trusted_event_count: int
    expected_active_event_count: int
    expected_trusted_observation_count: int
    expected_active_observation_count: int
    expected_trusted_work_count: int
    expected_active_work_count: int
    expected_accepted_result_count: int
    expected_total_tokens: int
    stage_id: str
    recovery_reason: str = RECOVERY_REASON

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "D2LComponentJournalRecoveryRequestV1":
        row = dict(value)
        expected = {
            "schema",
            "component_root",
            "transaction_root",
            "relay_import_file",
            "relay_import_physical_sha256",
            "relay_import_sha256",
            "relay_import_ordinal",
            "snapshot_root",
            "snapshot_sha256",
            "component_plan_file",
            "component_plan_physical_sha256",
            "expected_manifest_sha256",
            "expected_events_sha256",
            "expected_artifact_index_sha256",
            "expected_observation_journal_sha256",
            "expected_work_journal_sha256",
            "expected_trusted_event_count",
            "expected_active_event_count",
            "expected_trusted_observation_count",
            "expected_active_observation_count",
            "expected_trusted_work_count",
            "expected_active_work_count",
            "expected_accepted_result_count",
            "expected_total_tokens",
            "stage_id",
            "recovery_reason",
        }
        if set(row) != expected or row.get("schema") != REQUEST_SCHEMA:
            raise D2LComponentJournalRecoveryError(
                "journal recovery request fields are invalid"
            )
        row.pop("schema")
        for key in (
            "component_root",
            "transaction_root",
            "relay_import_file",
            "snapshot_root",
            "component_plan_file",
        ):
            row[key] = Path(str(row[key]))
        return cls(**row)

    def normalized(self) -> "D2LComponentJournalRecoveryRequestV1":
        component_root = self.component_root.resolve()
        transaction_root = self.transaction_root.resolve()
        relay_import_file = self.relay_import_file.resolve()
        snapshot_root = self.snapshot_root.resolve()
        component_plan_file = self.component_plan_file.resolve()
        if self.component_root.is_symlink():
            raise D2LComponentJournalRecoveryError(
                "component_root must not be a symlink"
            )
        for path, label in (
            (transaction_root, "transaction_root"),
            (snapshot_root, "snapshot_root"),
        ):
            if path.exists() and path.is_symlink():
                raise D2LComponentJournalRecoveryError(
                    f"{label} must not be a symlink"
                )
        _regular_file(relay_import_file, "relay import")
        _regular_directory(snapshot_root, "snapshot root")
        _regular_file(component_plan_file, "component plan")
        if (
            transaction_root == component_root
            or component_root in transaction_root.parents
            or transaction_root in component_root.parents
            or snapshot_root == component_root
            or component_root in snapshot_root.parents
        ):
            raise D2LComponentJournalRecoveryError(
                "recovery roots overlap unsafely"
            )
        stage_id = _nonempty(self.stage_id, "stage_id")
        recovery_reason = _nonempty(
            self.recovery_reason,
            "recovery_reason",
        )
        if recovery_reason != RECOVERY_REASON:
            raise D2LComponentJournalRecoveryError(
                "recovery_reason is not the registered journal race reason"
            )
        return D2LComponentJournalRecoveryRequestV1(
            component_root=component_root,
            transaction_root=transaction_root,
            relay_import_file=relay_import_file,
            relay_import_physical_sha256=_require_sha256(
                self.relay_import_physical_sha256,
                "relay_import_physical_sha256",
            ),
            relay_import_sha256=_require_sha256(
                self.relay_import_sha256,
                "relay_import_sha256",
            ),
            relay_import_ordinal=_positive_int(
                self.relay_import_ordinal,
                "relay_import_ordinal",
            ),
            snapshot_root=snapshot_root,
            snapshot_sha256=_require_sha256(
                self.snapshot_sha256,
                "snapshot_sha256",
            ),
            component_plan_file=component_plan_file,
            component_plan_physical_sha256=_require_sha256(
                self.component_plan_physical_sha256,
                "component_plan_physical_sha256",
            ),
            expected_manifest_sha256=_require_sha256(
                self.expected_manifest_sha256,
                "expected_manifest_sha256",
            ),
            expected_events_sha256=_require_sha256(
                self.expected_events_sha256,
                "expected_events_sha256",
            ),
            expected_artifact_index_sha256=_require_sha256(
                self.expected_artifact_index_sha256,
                "expected_artifact_index_sha256",
            ),
            expected_observation_journal_sha256=_require_sha256(
                self.expected_observation_journal_sha256,
                "expected_observation_journal_sha256",
            ),
            expected_work_journal_sha256=_require_sha256(
                self.expected_work_journal_sha256,
                "expected_work_journal_sha256",
            ),
            expected_trusted_event_count=_positive_int(
                self.expected_trusted_event_count,
                "expected_trusted_event_count",
            ),
            expected_active_event_count=_positive_int(
                self.expected_active_event_count,
                "expected_active_event_count",
            ),
            expected_trusted_observation_count=_positive_int(
                self.expected_trusted_observation_count,
                "expected_trusted_observation_count",
            ),
            expected_active_observation_count=_positive_int(
                self.expected_active_observation_count,
                "expected_active_observation_count",
            ),
            expected_trusted_work_count=_positive_int(
                self.expected_trusted_work_count,
                "expected_trusted_work_count",
            ),
            expected_active_work_count=_positive_int(
                self.expected_active_work_count,
                "expected_active_work_count",
            ),
            expected_accepted_result_count=_positive_int(
                self.expected_accepted_result_count,
                "expected_accepted_result_count",
            ),
            expected_total_tokens=_positive_int(
                self.expected_total_tokens,
                "expected_total_tokens",
            ),
            stage_id=stage_id,
            recovery_reason=recovery_reason,
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA,
            "relay_import_physical_sha256": (
                self.relay_import_physical_sha256
            ),
            "relay_import_sha256": self.relay_import_sha256,
            "relay_import_ordinal": self.relay_import_ordinal,
            "snapshot_sha256": self.snapshot_sha256,
            "component_plan_physical_sha256": (
                self.component_plan_physical_sha256
            ),
            "expected_manifest_sha256": self.expected_manifest_sha256,
            "expected_events_sha256": self.expected_events_sha256,
            "expected_artifact_index_sha256": (
                self.expected_artifact_index_sha256
            ),
            "expected_observation_journal_sha256": (
                self.expected_observation_journal_sha256
            ),
            "expected_work_journal_sha256": (
                self.expected_work_journal_sha256
            ),
            "expected_trusted_event_count": (
                self.expected_trusted_event_count
            ),
            "expected_active_event_count": self.expected_active_event_count,
            "expected_trusted_observation_count": (
                self.expected_trusted_observation_count
            ),
            "expected_active_observation_count": (
                self.expected_active_observation_count
            ),
            "expected_trusted_work_count": (
                self.expected_trusted_work_count
            ),
            "expected_active_work_count": self.expected_active_work_count,
            "expected_accepted_result_count": (
                self.expected_accepted_result_count
            ),
            "expected_total_tokens": self.expected_total_tokens,
            "stage_id": self.stage_id,
            "recovery_reason": self.recovery_reason,
        }


def _validate_relay_authority(
    req: D2LComponentJournalRecoveryRequestV1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    import_bytes = req.relay_import_file.read_bytes()
    if _sha256_bytes(import_bytes) != req.relay_import_physical_sha256:
        raise D2LComponentJournalRecoveryError(
            "relay import physical hash drift"
        )
    record = _load_json_bytes(import_bytes, "relay import")
    if canonical_json_bytes(record) != import_bytes:
        raise D2LComponentJournalRecoveryError(
            "relay import is not canonical JSON"
        )
    if (
        record.get("schema_id") != "WorkflowComponentImportV1"
        or record.get("schema_version") != "1.0.0"
        or record.get("acceptance_ordinal") != req.relay_import_ordinal
    ):
        raise D2LComponentJournalRecoveryError(
            "relay import identity is invalid"
        )
    observed_import_sha = _require_sha256(
        record.get("import_sha256"),
        "relay import_sha256",
    )
    payload = dict(record)
    payload.pop("import_sha256", None)
    if (
        canonical_sha256(payload) != observed_import_sha
        or observed_import_sha != req.relay_import_sha256
    ):
        raise D2LComponentJournalRecoveryError(
            "relay import hash drift"
        )
    expected_name = (
        f"{req.relay_import_ordinal:08d}_"
        f"{req.relay_import_sha256.lower()}.json"
    )
    if req.relay_import_file.name.lower() != expected_name:
        raise D2LComponentJournalRecoveryError(
            "relay import filename identity mismatch"
        )
    snapshot_sha = _require_sha256(
        record.get("snapshot_sha256"),
        "relay snapshot_sha256",
    )
    if snapshot_sha != req.snapshot_sha256:
        raise D2LComponentJournalRecoveryError(
            "relay snapshot hash differs from request"
        )
    workflow_root = req.relay_import_file.parent.parent
    expected_snapshot_root = (
        workflow_root
        / "components"
        / str(record.get("component_id"))
        / str(record.get("component_run_id"))
        / "snapshots"
        / req.snapshot_sha256.lower()
    ).resolve()
    if os.path.normcase(str(expected_snapshot_root)) != os.path.normcase(
        str(req.snapshot_root)
    ):
        raise D2LComponentJournalRecoveryError(
            "snapshot root is not bound to the relay import"
        )
    files = record.get("files")
    if not isinstance(files, list) or not files:
        raise D2LComponentJournalRecoveryError(
            "relay import files are invalid"
        )
    snapshot_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, Mapping) or set(item) != {
            "relative_path",
            "physical_sha256",
        }:
            raise D2LComponentJournalRecoveryError(
                f"relay import file {index} is invalid"
            )
        relative = _normalize_relative_path(
            item["relative_path"],
            f"relay import file {index}",
        )
        if relative in seen:
            raise D2LComponentJournalRecoveryError(
                "relay import has duplicate file paths"
            )
        seen.add(relative)
        path = _resolve_under(
            req.snapshot_root,
            relative,
            f"snapshot file {index}",
        )
        _regular_file(path, f"snapshot file {index}")
        digest = _require_sha256(
            item["physical_sha256"],
            f"snapshot file {index} hash",
        )
        if file_sha256(path) != digest:
            raise D2LComponentJournalRecoveryError(
                "snapshot file physical hash drift"
            )
        snapshot_rows.append(
            {
                "relative_path": relative,
                "physical_sha256": digest,
                "byte_count": path.stat().st_size,
            }
        )
    snapshot_rows.sort(key=lambda row: row["relative_path"])
    snapshot_index = {
        "schema": TREE_INDEX_SCHEMA,
        "files": snapshot_rows,
    }
    snapshot_index["tree_sha256"] = canonical_sha256(snapshot_index)
    if _tree_index(req.snapshot_root) != snapshot_index:
        raise D2LComponentJournalRecoveryError(
            "snapshot directory differs from relay file cover"
        )
    normalized_snapshot = dict(record)
    normalized_snapshot.pop("import_sha256", None)
    normalized_snapshot.pop("acceptance_ordinal", None)
    normalized_snapshot.pop("accepted_at", None)
    normalized_snapshot["schema_id"] = "ValidatedComponentSnapshotV1"
    observed_snapshot_sha = normalized_snapshot.pop(
        "snapshot_sha256",
        None,
    )
    if (
        _require_sha256(
            observed_snapshot_sha,
            "normalized snapshot_sha256",
        )
        != req.snapshot_sha256
        or canonical_sha256(normalized_snapshot) != req.snapshot_sha256
    ):
        raise D2LComponentJournalRecoveryError(
            "relay snapshot canonical hash drift"
        )
    return record, snapshot_index


def _validate_plan(
    req: D2LComponentJournalRecoveryRequestV1,
) -> ComponentPlan:
    if file_sha256(req.component_plan_file) != (
        req.component_plan_physical_sha256
    ):
        raise D2LComponentJournalRecoveryError(
            "component plan physical hash drift"
        )
    plan = ComponentPlan.from_mapping(
        _load_json_file(req.component_plan_file, "component plan")
    )
    if req.stage_id not in {stage.stage_id for stage in plan.stages}:
        raise D2LComponentJournalRecoveryError(
            "recovery stage is absent from component plan"
        )
    return plan


def _event_rows(path: Path) -> list[dict[str, Any]]:
    rows = _component_event_rows(path, "component events")
    for index, row in enumerate(rows, start=1):
        if row.get("component_seq") != index:
            raise D2LComponentJournalRecoveryError(
                "component event sequence is not contiguous"
            )
    return rows


def _validate_active_prestate(
    req: D2LComponentJournalRecoveryRequestV1,
    *,
    plan: ComponentPlan,
    relay_import: Mapping[str, Any],
) -> dict[str, Any]:
    root = _regular_directory(req.component_root, "component root")
    paths = {
        "manifest": root / "component_manifest.json",
        "events": root / "events.jsonl",
        "index": root / "artifact_index.json",
        "observations": root / "runtime/component_observations.jsonl",
        "work": root / f"runtime/work_items/{req.stage_id}.jsonl",
    }
    expected_hashes = {
        "manifest": req.expected_manifest_sha256,
        "events": req.expected_events_sha256,
        "index": req.expected_artifact_index_sha256,
        "observations": req.expected_observation_journal_sha256,
        "work": req.expected_work_journal_sha256,
    }
    for key, path in paths.items():
        _regular_file(path, f"active {key}")
        if file_sha256(path) != expected_hashes[key]:
            raise D2LComponentJournalRecoveryError(
                f"active {key} prestate hash drift"
            )
    manifest = validate_component_manifest(
        _load_json_file(paths["manifest"], "active manifest")
    )
    if (
        manifest["workflow_run_id"] != plan.workflow_run_id
        or manifest["component_run_id"] != plan.component_run_id
        or manifest["component_attempt_id"] != 1
        or manifest["status"] != "failed"
        or manifest["active_stage_id"] is not None
    ):
        raise D2LComponentJournalRecoveryError(
            "active failed manifest identity is invalid"
        )
    if (
        relay_import.get("workflow_run_id") != plan.workflow_run_id
        or relay_import.get("component_run_id") != plan.component_run_id
        or relay_import.get("component_attempt_id") != 1
        or relay_import.get("status") != "running"
    ):
        raise D2LComponentJournalRecoveryError(
            "trusted relay identity differs from component plan"
        )
    active_events = _event_rows(paths["events"])
    if len(active_events) != req.expected_active_event_count:
        raise D2LComponentJournalRecoveryError(
            "active event count drift"
        )
    if active_events[-2]["event"] != "validation_failed" or (
        active_events[-1]["event"] != "stage_done"
        or active_events[-1]["payload"].get("outcome") != "failed"
    ):
        raise D2LComponentJournalRecoveryError(
            "active suffix is not the registered broken closure"
        )
    active_observations = read_observation_journal(paths["observations"])
    active_work = read_work_journal(paths["work"])
    if (
        len(active_observations) != req.expected_active_observation_count
        or len(active_work) != req.expected_active_work_count
    ):
        raise D2LComponentJournalRecoveryError(
            "active journal counts drift"
        )
    return {
        "paths": paths,
        "manifest": manifest,
        "events": active_events,
        "observations": active_observations,
        "work": active_work,
        "tree_index": _tree_index(root),
    }


def _validate_trusted_prefix_and_delta(
    req: D2LComponentJournalRecoveryRequestV1,
    *,
    plan: ComponentPlan,
    active: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot_manifest_path = req.snapshot_root / "component_manifest.json"
    snapshot_events_path = req.snapshot_root / "events.jsonl"
    snapshot_index_path = req.snapshot_root / "artifact_index.json"
    snapshot_obs_path = (
        req.snapshot_root / "runtime/component_observations.jsonl"
    )
    snapshot_work_path = (
        req.snapshot_root
        / f"runtime/work_items/{req.stage_id}.jsonl"
    )
    validation = validate_translation_component_package(
        req.snapshot_root,
        require_terminal=False,
    )
    manifest = validate_component_manifest(
        _load_json_file(snapshot_manifest_path, "snapshot manifest")
    )
    if (
        manifest["workflow_run_id"] != plan.workflow_run_id
        or manifest["component_run_id"] != plan.component_run_id
        or manifest["component_attempt_id"] != 1
        or manifest["status"] != "running"
        or manifest["active_stage_id"] != req.stage_id
        or validation["terminal_event"] is not None
    ):
        raise D2LComponentJournalRecoveryError(
            "trusted snapshot is not the expected running prefix"
        )
    validate_artifact_index(
        _load_json_file(snapshot_index_path, "snapshot artifact index"),
        manifest=manifest,
        artifact_root=req.snapshot_root,
    )
    trusted_events = _event_rows(snapshot_events_path)
    trusted_observations = read_observation_journal(snapshot_obs_path)
    trusted_work = read_work_journal(snapshot_work_path)
    if (
        len(trusted_events) != req.expected_trusted_event_count
        or len(trusted_observations)
        != req.expected_trusted_observation_count
        or len(trusted_work) != req.expected_trusted_work_count
    ):
        raise D2LComponentJournalRecoveryError(
            "trusted prefix counts drift"
        )
    for snapshot_path, active_path, label in (
        (snapshot_events_path, active["paths"]["events"], "events"),
        (snapshot_obs_path, active["paths"]["observations"], "observations"),
        (snapshot_work_path, active["paths"]["work"], "work"),
    ):
        trusted_bytes = snapshot_path.read_bytes()
        active_bytes = active_path.read_bytes()
        if not active_bytes.startswith(trusted_bytes):
            raise D2LComponentJournalRecoveryError(
                f"trusted {label} is not an exact active byte prefix"
            )
    delta_observations = active["observations"][
        req.expected_trusted_observation_count :
    ]
    delta_work = active["work"][req.expected_trusted_work_count :]
    if (
        len(delta_observations) != 5
        or tuple(
            entry["observation"]["event"]
            for entry in delta_observations
        )
        != _DELTA_EVENTS
        or len(delta_work) != 1
    ):
        raise D2LComponentJournalRecoveryError(
            "active journal delta is not exactly one accepted provider call"
        )
    stage = next(
        stage for stage in plan.stages if stage.stage_id == req.stage_id
    )
    for entry in delta_observations:
        if (
            entry["workflow_run_id"] != plan.workflow_run_id
            or entry["component_run_id"] != plan.component_run_id
            or entry["component_attempt_id"] != 1
            or entry["stage_id"] != req.stage_id
            or entry["producer"] != stage.producer
            or entry["work_id"] != stage.work_id
        ):
            raise D2LComponentJournalRecoveryError(
                "journal delta contains foreign observation identity"
            )
    request_payload = delta_observations[0]["observation"]["payload"]
    response_payload = delta_observations[1]["observation"]["payload"]
    usage_payload = delta_observations[2]["observation"]["payload"]
    validation_payload = delta_observations[3]["observation"]["payload"]
    progress_payload = delta_observations[4]["observation"]["payload"]
    request_id = request_payload.get("logical_request_id")
    request_work_id = request_payload.get("work_id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or request_work_id != delta_work[0]["work_item_id"]
        or response_payload.get("usage", {}).get("logical_request_id")
        != request_id
        or usage_payload.get("accepted_usage", {}).get(
            "logical_request_id"
        )
        != request_id
        or validation_payload.get("subject_ref") != request_work_id
        or progress_payload.get("progress", {}).get("completed")
        != req.expected_active_work_count
    ):
        raise D2LComponentJournalRecoveryError(
            "accepted call delta identity is inconsistent"
        )
    cumulative = usage_payload.get("component_cumulative")
    if (
        not isinstance(cumulative, Mapping)
        or cumulative.get("accepted_result_count")
        != req.expected_accepted_result_count
        or cumulative.get("physical_attempt_count")
        != req.expected_accepted_result_count
        or cumulative.get("total_tokens") != req.expected_total_tokens
    ):
        raise D2LComponentJournalRecoveryError(
            "accepted call cumulative usage differs from request"
        )
    return {
        "manifest": manifest,
        "events": trusted_events,
        "observations": trusted_observations,
        "work": trusted_work,
        "validation": validation,
        "delta_observations": delta_observations,
        "delta_work": delta_work,
        "observation_state": observation_journal_state(
            active["observations"]
        ),
        "work_state": work_journal_state(active["work"]),
    }


def _prepare_component(
    req: D2LComponentJournalRecoveryRequestV1,
    *,
    plan: ComponentPlan,
    active: Mapping[str, Any],
    trusted: Mapping[str, Any],
    snapshot_index: Mapping[str, Any],
    transaction_dir: Path,
) -> dict[str, Any]:
    prepared_record_path = transaction_dir / "prepared.json"
    if prepared_record_path.exists():
        prepared = validate_prepared_recovery_v1(
            _load_json_file(prepared_record_path, "prepared recovery")
        )
        prepared_root = transaction_dir / prepared["prepared_component_ref"]
        if prepared_root.exists():
            _verify_tree(prepared_root, prepared["poststate_tree"])
        return prepared

    build_root = transaction_dir / (
        f".prepared_component.{uuid4().hex}.tmp"
    )
    _copy_tree_exact(req.snapshot_root, build_root, snapshot_index)
    (build_root / "runtime").mkdir(parents=True, exist_ok=True)
    (build_root / "runtime/work_items").mkdir(parents=True, exist_ok=True)
    (build_root / "runtime/component_observations.jsonl").write_bytes(
        active["paths"]["observations"].read_bytes()
    )
    (
        build_root / f"runtime/work_items/{req.stage_id}.jsonl"
    ).write_bytes(active["paths"]["work"].read_bytes())

    runner = D2LTranslationComponentRunner(plan, build_root)
    runner.manifest = validate_component_manifest(
        _load_json_file(
            build_root / "component_manifest.json",
            "prepared component manifest",
        )
    )
    artifact_index = validate_artifact_index(
        _load_json_file(
            build_root / "artifact_index.json",
            "prepared artifact index",
        ),
        manifest=runner.manifest,
        artifact_root=build_root,
    )
    runner.artifacts = list(artifact_index["artifacts"])
    runner._current_attempt = 1
    runner._journal_cursor = req.expected_trusted_observation_count
    runner.writer = D2LTranslationComponentEventWriter(
        build_root / "events.jsonl",
        manifest=runner.manifest,
        component_attempt_id=1,
        recover_existing_attempt=True,
    )
    if (
        runner.writer.component_seq
        != req.expected_trusted_event_count
        or not runner.writer._recovery_checkpoint_only
    ):
        raise D2LComponentJournalRecoveryError(
            "trusted event writer did not open at the sealed prefix"
        )
    # The normal writer permits only a checkpoint when reopening the same
    # attempt. This transaction has separately proved the exact relay prefix
    # and durable journal delta, so it may replay that bounded delta before
    # producing the recovery checkpoint.
    runner.writer._recovery_checkpoint_only = False
    stage = next(
        stage for stage in plan.stages if stage.stage_id == req.stage_id
    )
    runner._drain_observation_journal(
        stage,
        allow_incomplete_tail=False,
    )
    prepared_lines = _jsonl_lines(
        build_root / "events.jsonl",
        "prepared component events",
    )
    trusted_lines = _jsonl_lines(
        req.snapshot_root / "events.jsonl",
        "trusted component events",
    )
    active_lines = _jsonl_lines(
        active["paths"]["events"],
        "active component events",
    )
    if prepared_lines[: len(trusted_lines)] != trusted_lines:
        raise D2LComponentJournalRecoveryError(
            "runner changed trusted event prefix"
        )
    if prepared_lines[
        req.expected_trusted_event_count :
        req.expected_trusted_event_count + 3
    ] != active_lines[
        req.expected_trusted_event_count :
        req.expected_trusted_event_count + 3
    ]:
        raise D2LComponentJournalRecoveryError(
            "runner did not byte-reconstruct active request/response/usage"
        )
    runner._drain_term_work_journal(
        stage,
        projection_mode="live",
    )
    runner._pause(req.stage_id, req.recovery_reason)
    validation = validate_translation_component_package(
        build_root,
        require_terminal=False,
    )
    manifest = validate_component_manifest(
        _load_json_file(
            build_root / "component_manifest.json",
            "prepared paused manifest",
        )
    )
    if (
        manifest["component_attempt_id"] != 1
        or manifest["status"] != "paused"
        or manifest["active_stage_id"] != req.stage_id
        or manifest["resume"]["paused_reason"] != req.recovery_reason
        or not manifest["resume"]["resume_available"]
        or validation["terminal_event"] is not None
    ):
        raise D2LComponentJournalRecoveryError(
            "prepared package did not reach the registered paused state"
        )
    usage = validation.get("component_usage")
    if (
        not isinstance(usage, Mapping)
        or usage.get("accepted_result_count")
        != req.expected_accepted_result_count
        or usage.get("total_tokens") != req.expected_total_tokens
    ):
        raise D2LComponentJournalRecoveryError(
            "prepared package usage is not exact"
        )
    final_events = _event_rows(build_root / "events.jsonl")
    if any(
        event["event"] in {"validation_failed", "run_failed"}
        for event in final_events[req.expected_trusted_event_count :]
    ):
        raise D2LComponentJournalRecoveryError(
            "prepared package retained failed closure events"
        )
    if sum(
        event["event"] == "validation_passed"
        and event["payload"].get("subject_ref")
        == trusted["delta_work"][0]["work_item_id"]
        for event in final_events
    ) != 1:
        raise D2LComponentJournalRecoveryError(
            "accepted sixth call was not projected exactly once"
        )
    poststate_tree = _tree_index(build_root)
    prepared_ref = (
        "prepared/"
        f"component.{poststate_tree['tree_sha256']}"
    )
    prepared_root = transaction_dir / prepared_ref
    prepared_root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(build_root, prepared_root)
    prepared_payload = {
        "schema": PREPARED_SCHEMA,
        "workflow_run_id": plan.workflow_run_id,
        "component_run_id": plan.component_run_id,
        "component_attempt_id": 1,
        "prepared_component_ref": prepared_ref,
        "poststate_tree": poststate_tree,
        "post_package_validation": validation,
        "post_package_validation_sha256": canonical_sha256(validation),
        "checkpoint_ref": manifest["resume"]["checkpoint_ref"],
        "checkpoint_sha256": manifest["resume"]["checkpoint_sha256"],
        "provider_call_count": 0,
        "semantic_replay_count": 0,
        "accepted_result_count": req.expected_accepted_result_count,
        "total_tokens": req.expected_total_tokens,
    }
    prepared_payload["prepared_sha256"] = canonical_sha256(
        prepared_payload
    )
    _write_absent_or_equal(
        prepared_record_path,
        canonical_json_bytes(prepared_payload),
    )
    return prepared_payload


def validate_prepared_recovery_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(value)
    required = {
        "schema",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "prepared_component_ref",
        "poststate_tree",
        "post_package_validation",
        "post_package_validation_sha256",
        "checkpoint_ref",
        "checkpoint_sha256",
        "provider_call_count",
        "semantic_replay_count",
        "accepted_result_count",
        "total_tokens",
        "prepared_sha256",
    }
    if set(row) != required or row.get("schema") != PREPARED_SCHEMA:
        raise D2LComponentJournalRecoveryError(
            "prepared recovery fields are invalid"
        )
    observed = _require_sha256(
        row.pop("prepared_sha256"),
        "prepared_sha256",
    )
    if canonical_sha256(row) != observed:
        raise D2LComponentJournalRecoveryError(
            "prepared recovery hash drift"
        )
    row["prepared_sha256"] = observed
    row["prepared_component_ref"] = _normalize_relative_path(
        row["prepared_component_ref"],
        "prepared_component_ref",
    )
    row["poststate_tree"] = _validate_tree_index(
        row["poststate_tree"]
    )
    if (
        canonical_sha256(row["post_package_validation"])
        != _require_sha256(
            row["post_package_validation_sha256"],
            "post_package_validation_sha256",
        )
    ):
        raise D2LComponentJournalRecoveryError(
            "prepared package validation hash drift"
        )
    _require_sha256(row["checkpoint_sha256"], "checkpoint_sha256")
    if row["provider_call_count"] != 0 or row["semantic_replay_count"] != 0:
        raise D2LComponentJournalRecoveryError(
            "prepared recovery cannot contain provider or semantic replay"
        )
    return row


def _quarantine_component(
    req: D2LComponentJournalRecoveryRequestV1,
    *,
    active: Mapping[str, Any],
    transaction_dir: Path,
) -> dict[str, Any]:
    tree = active["tree_index"]
    quarantine_ref = (
        "quarantine/"
        f"component.{tree['tree_sha256']}"
    )
    quarantine_root = transaction_dir / quarantine_ref
    _copy_tree_exact(req.component_root, quarantine_root, tree)
    index_ref = "quarantine/file_index.json"
    _write_absent_or_equal(
        transaction_dir / index_ref,
        canonical_json_bytes(tree),
    )
    return {
        "component_ref": quarantine_ref,
        "file_index_ref": index_ref,
        "tree_sha256": tree["tree_sha256"],
        "file_count": len(tree["files"]),
    }


def _load_quarantine(
    transaction_dir: Path,
) -> dict[str, Any]:
    quarantine_index = _validate_tree_index(
        _load_json_file(
            transaction_dir / "quarantine/file_index.json",
            "quarantine file index",
        )
    )
    quarantine = {
        "component_ref": (
            "quarantine/"
            f"component.{quarantine_index['tree_sha256']}"
        ),
        "file_index_ref": "quarantine/file_index.json",
        "tree_sha256": quarantine_index["tree_sha256"],
        "file_count": len(quarantine_index["files"]),
    }
    _verify_tree(
        transaction_dir / quarantine["component_ref"],
        quarantine_index,
    )
    return quarantine


def _poststate_matches(
    component_root: Path,
    prepared: Mapping[str, Any],
) -> bool:
    if not component_root.is_dir() or component_root.is_symlink():
        return False
    try:
        return _tree_index(component_root) == prepared["poststate_tree"]
    except D2LComponentJournalRecoveryError:
        return False


def _restore_interrupted_preinstall(
    component_root: Path,
    install_backup: Path,
) -> None:
    if component_root.exists():
        return
    if not install_backup.is_dir() or install_backup.is_symlink():
        raise D2LComponentJournalRecoveryError(
            "component root is missing without a valid install backup"
        )
    os.replace(install_backup, component_root)


def _install_prepared(
    req: D2LComponentJournalRecoveryRequestV1,
    *,
    prepared: Mapping[str, Any],
    active_tree: Mapping[str, Any],
    transaction_dir: Path,
) -> None:
    prepared_root = transaction_dir / prepared["prepared_component_ref"]
    install_backup = transaction_dir / "install/active_preinstall"
    install_backup.parent.mkdir(parents=True, exist_ok=True)
    _restore_interrupted_preinstall(req.component_root, install_backup)
    if _poststate_matches(req.component_root, prepared):
        validate_translation_component_package(
            req.component_root,
            require_terminal=False,
        )
        return
    _verify_tree(req.component_root, active_tree)
    if install_backup.exists():
        raise D2LComponentJournalRecoveryError(
            "install backup collision before replacement"
        )
    _verify_tree(prepared_root, prepared["poststate_tree"])
    os.replace(req.component_root, install_backup)
    try:
        os.replace(prepared_root, req.component_root)
        _verify_tree(req.component_root, prepared["poststate_tree"])
        validation = validate_translation_component_package(
            req.component_root,
            require_terminal=False,
        )
        if canonical_sha256(validation) != prepared[
            "post_package_validation_sha256"
        ]:
            raise D2LComponentJournalRecoveryError(
                "installed package validation differs from prepared state"
            )
    except Exception:
        if req.component_root.exists():
            if prepared_root.exists():
                raise D2LComponentJournalRecoveryError(
                    "prepared tree collision during install rollback"
                )
            os.replace(req.component_root, prepared_root)
        if install_backup.exists():
            os.replace(install_backup, req.component_root)
        raise


def _receipt_with_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    row["receipt_sha256"] = canonical_sha256(row)
    return row


def validate_journal_recovery_receipt_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema") != RECEIPT_SCHEMA:
        raise D2LComponentJournalRecoveryError(
            "journal recovery receipt schema is invalid"
        )
    observed = _require_sha256(
        row.pop("receipt_sha256", None),
        "receipt_sha256",
    )
    if canonical_sha256(row) != observed:
        raise D2LComponentJournalRecoveryError(
            "journal recovery receipt hash drift"
        )
    row["receipt_sha256"] = observed
    return row


def _expected_receipt(
    req: D2LComponentJournalRecoveryRequestV1,
    *,
    transaction_id: str,
    transaction_sha: str,
    relay_import: Mapping[str, Any],
    quarantine: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    validation = validate_translation_component_package(
        req.component_root,
        require_terminal=False,
    )
    manifest = validate_component_manifest(
        _load_json_file(
            req.component_root / "component_manifest.json",
            "recovered manifest",
        )
    )
    return _receipt_with_hash(
        {
            "schema": RECEIPT_SCHEMA,
            "transaction_id": transaction_id,
            "transaction_sha256": transaction_sha,
            "workflow_run_id": manifest["workflow_run_id"],
            "component_run_id": manifest["component_run_id"],
            "component_attempt_id": manifest["component_attempt_id"],
            "recovery_reason": req.recovery_reason,
            "authority": {
                "relay_import_ref": str(
                    req.relay_import_file
                ),
                "relay_import_physical_sha256": (
                    req.relay_import_physical_sha256
                ),
                "relay_import_sha256": req.relay_import_sha256,
                "relay_import_ordinal": req.relay_import_ordinal,
                "snapshot_ref": str(req.snapshot_root),
                "snapshot_sha256": req.snapshot_sha256,
                "snapshot_event_count": req.expected_trusted_event_count,
                "snapshot_observation_count": (
                    req.expected_trusted_observation_count
                ),
                "snapshot_work_count": req.expected_trusted_work_count,
                "relay_accepted_at": relay_import["accepted_at"],
            },
            "prestate": {
                "component_manifest_sha256": (
                    req.expected_manifest_sha256
                ),
                "events_sha256": req.expected_events_sha256,
                "artifact_index_sha256": (
                    req.expected_artifact_index_sha256
                ),
                "observation_journal_sha256": (
                    req.expected_observation_journal_sha256
                ),
                "work_journal_sha256": (
                    req.expected_work_journal_sha256
                ),
                "event_count": req.expected_active_event_count,
                "observation_count": (
                    req.expected_active_observation_count
                ),
                "work_count": req.expected_active_work_count,
                "tree_sha256": quarantine["tree_sha256"],
            },
            "quarantine": dict(quarantine),
            "reconstruction": {
                "byte_identical_event_seqs": [
                    req.expected_trusted_event_count + offset
                    for offset in (1, 2, 3)
                ],
                "discarded_broken_event_seqs": [
                    req.expected_active_event_count - 1,
                    req.expected_active_event_count,
                ],
                "accepted_result_count": (
                    req.expected_accepted_result_count
                ),
                "total_tokens": req.expected_total_tokens,
                "provider_call_count": 0,
                "semantic_replay_count": 0,
            },
            "poststate": {
                "component_manifest_sha256": file_sha256(
                    req.component_root / "component_manifest.json"
                ),
                "events_sha256": file_sha256(
                    req.component_root / "events.jsonl"
                ),
                "artifact_index_sha256": file_sha256(
                    req.component_root / "artifact_index.json"
                ),
                "observation_journal_sha256": file_sha256(
                    req.component_root
                    / "runtime/component_observations.jsonl"
                ),
                "work_journal_sha256": file_sha256(
                    req.component_root
                    / f"runtime/work_items/{req.stage_id}.jsonl"
                ),
                "tree_sha256": prepared["poststate_tree"]["tree_sha256"],
                "checkpoint_ref": prepared["checkpoint_ref"],
                "checkpoint_sha256": prepared["checkpoint_sha256"],
                "status": "paused",
            },
            "post_package_validation": validation,
            "post_package_validation_sha256": canonical_sha256(validation),
        }
    )


def recover_d2l_component_journal_v1(
    request: D2LComponentJournalRecoveryRequestV1,
) -> dict[str, Any]:
    """Recover one failed journal race without replaying provider work."""

    req = request.normalized()
    relay_import, snapshot_index = _validate_relay_authority(req)
    plan = _validate_plan(req)
    if (
        relay_import["workflow_run_id"] != plan.workflow_run_id
        or relay_import["component_run_id"] != plan.component_run_id
    ):
        raise D2LComponentJournalRecoveryError(
            "relay import and plan identity differ"
        )
    identity = {
        **req.identity_payload(),
        "workflow_run_id": plan.workflow_run_id,
        "component_run_id": plan.component_run_id,
        "component_plan_sha256": plan.plan_sha256,
    }
    transaction_sha = canonical_sha256(identity)
    transaction_id = (
        f"d2l_journal_recovery_{transaction_sha[:32].lower()}"
    )
    transaction_dir = req.transaction_root / transaction_id
    if transaction_dir.exists() and transaction_dir.is_symlink():
        raise D2LComponentJournalRecoveryError(
            "transaction directory must not be a symlink"
        )
    transaction_dir.mkdir(parents=True, exist_ok=True)
    request_payload = {
        **identity,
        "schema": REQUEST_SCHEMA,
        "transaction_id": transaction_id,
        "transaction_sha256": transaction_sha,
    }
    _write_absent_or_equal(
        transaction_dir / "request.json",
        canonical_json_bytes(request_payload),
    )

    try:
        with D2LComponentWriterLease(
            req.component_root
        ), D2LStageWriterLease(req.component_root):
            prepared_record_path = transaction_dir / "prepared.json"
            install_backup = transaction_dir / "install/active_preinstall"
            if not req.component_root.exists() and install_backup.exists():
                _restore_interrupted_preinstall(
                    req.component_root,
                    install_backup,
                )
            receipt_path = transaction_dir / "receipt.json"
            if prepared_record_path.exists():
                prepared = validate_prepared_recovery_v1(
                    _load_json_file(
                        prepared_record_path,
                        "prepared recovery",
                    )
                )
                if _poststate_matches(req.component_root, prepared):
                    quarantine = _load_quarantine(transaction_dir)
                    expected = _expected_receipt(
                        req,
                        transaction_id=transaction_id,
                        transaction_sha=transaction_sha,
                        relay_import=relay_import,
                        quarantine=quarantine,
                        prepared=prepared,
                    )
                    expected_bytes = canonical_json_bytes(expected)
                    if receipt_path.exists():
                        receipt_bytes = _regular_file(
                            receipt_path,
                            "journal recovery receipt",
                        ).read_bytes()
                        validate_journal_recovery_receipt_v1(
                            _load_json_bytes(
                                receipt_bytes,
                                "journal recovery receipt",
                            )
                        )
                        if receipt_bytes != expected_bytes:
                            raise D2LComponentJournalRecoveryError(
                                "committed recovery receipt differs "
                                "from transaction"
                            )
                    else:
                        _write_absent_or_equal(
                            receipt_path,
                            expected_bytes,
                        )
                    return expected
                if receipt_path.exists():
                    raise D2LComponentJournalRecoveryError(
                        "receipt exists without the committed poststate"
                    )

            active = _validate_active_prestate(
                req,
                plan=plan,
                relay_import=relay_import,
            )
            trusted = _validate_trusted_prefix_and_delta(
                req,
                plan=plan,
                active=active,
            )
            quarantine = _quarantine_component(
                req,
                active=active,
                transaction_dir=transaction_dir,
            )
            prepared = _prepare_component(
                req,
                plan=plan,
                active=active,
                trusted=trusted,
                snapshot_index=snapshot_index,
                transaction_dir=transaction_dir,
            )
            _install_prepared(
                req,
                prepared=prepared,
                active_tree=active["tree_index"],
                transaction_dir=transaction_dir,
            )
            receipt = _expected_receipt(
                req,
                transaction_id=transaction_id,
                transaction_sha=transaction_sha,
                relay_import=relay_import,
                quarantine=quarantine,
                prepared=prepared,
            )
            _write_absent_or_equal(
                receipt_path,
                canonical_json_bytes(receipt),
            )
            return receipt
    except D2LComponentWriterLeaseError as exc:
        raise D2LComponentJournalRecoveryError(str(exc)) from exc


__all__ = [
    "D2LComponentJournalRecoveryError",
    "D2LComponentJournalRecoveryRequestV1",
    "RECOVERY_REASON",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "recover_d2l_component_journal_v1",
    "validate_journal_recovery_receipt_v1",
    "validate_prepared_recovery_v1",
]
