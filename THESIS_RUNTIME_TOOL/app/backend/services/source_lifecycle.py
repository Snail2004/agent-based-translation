from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from config import ALLOWED_SOURCE_EXTENSIONS, THESIS_JOBS_ROOT, THESIS_PANDOC_EXE
from pipeline.ingest.admitted_projection import validate_admitted_projection
from pipeline.ingest.canonical_source_package import (
    canonical_json_sha256,
    validate_canonical_source_package,
)
from pipeline.ingest.draft_structure import (
    DRAFT_PROJECT_STATE_VERSION,
    HIERARCHY_OVERLAY_VERSION,
    DraftStructureError,
    apply_correction_plan,
    apply_hierarchy_plan,
    build_correction_plan,
    build_draft_structure_report,
    build_hierarchy_plan,
    validate_draft_structure_report_shape,
)
from pipeline.ingest.d2l_presegmented_adapter import (
    AUTHORITATIVE_D2L_CAPTURE,
    D2lPresegmentedAdapterError,
    convert_d2l_presegmented_capture,
    validate_d2l_presegmented_output,
)
from pipeline.ingest.presegmented_source_normalizer import (
    normalize_presegmented_source,
)
from pipeline.ingest.source_package_materializer import materialize_source_package
from pipeline.ingest.source_package_exporter import (
    OVERLAY_VERSION,
    SourcePackageExportError,
    export_source_package,
)
from pipeline.ingest.unified_source_normalizer import (
    detect_source_format,
    normalize_source,
    validate_normalization_contract,
    write_unified_normalization,
)


SOURCE_LIFECYCLE_VERSION = "source_lifecycle_v1"
SOURCE_LIFECYCLE_V2_VERSION = "source_lifecycle_v2"
SOURCE_PACKAGE_DECISION_VERSION = "source_package_decision_event_v1"
SOURCE_PACKAGE_REVISION_VERSION = "source_package_revision_event_v2"
SOURCE_PACKAGE_FINALIZATION_VERSION = "source_package_finalization_v1"
SOURCE_PACKAGE_RUN_START_VERSION = "source_package_run_start_v1"
MANAGED_RUNTIME_BINDING_VERSION = "managed_source_runtime_binding_v1"
MANAGED_RUNTIME_MANIFEST_VERSION = "project_runtime_source_v2"
SOURCE_PACKAGE_PUBLICATION_VERSION = "source_package_publication_v1"
SOURCE_PACKAGE_STATUS_VERSION = "source_package_status_v1"
SOURCE_PACKAGE_REVIEW_VERSION = "source_package_review_v1"
SOURCE_PACKAGE_ISSUE_QUEUE_VERSION = "source_package_issue_queue_v1"
SOURCE_PACKAGE_UNIT_BLOCKS_VERSION = "source_package_unit_blocks_v1"
SOURCE_PACKAGE_REVIEW_BINDING_FIELDS = (
    "state_sha256",
    "candidate_tree_sha256",
    "document_sha256",
    "structure_sha256",
    "report_sha256",
)
CANDIDATE_DIRECTORY = "source_package_candidates"
DECISION_DIRECTORY = "source_package_decisions"
HIERARCHY_DIRECTORY = "source_package_hierarchy"
FINALIZATION_DIRECTORY = "source_package_finalizations"
RUN_START_DIRECTORY = "source_package_run_starts"
PUBLICATION_DIRECTORY = "source_package_publications"
PRESEGMENTED_CAPTURE_DIRECTORY = "source_package_captures"
STATE_FILENAME = "source_lifecycle_v1.json"
STATE_V2_FILENAME = "source_lifecycle_v2.json"
REPORT_FILENAME = "draft_structure_report.json"
MUTATION_LOCK_FILENAME = ".source_lifecycle.lock"
MUTATION_LOCK_TIMEOUT_SECONDS = 300.0
MUTATION_LOCK_POLL_SECONDS = 0.05
REQUIRED_PACKAGE_FILES = {
    "document.json",
    "structure_manifest.json",
    "normalization_receipt.json",
    "asset_manifest.json",
    "admitted_projection_v1.json",
    REPORT_FILENAME,
}

_CANDIDATE_VALIDATION_CACHE_MAX_ENTRIES = 8
_candidate_validation_cache_lock = threading.RLock()
_candidate_validation_cache: OrderedDict[
    tuple[str, str, str, str, str, str], dict[str, Any]
] = OrderedDict()
_candidate_validation_inflight: dict[
    tuple[str, str, str, str, str, str], threading.Event
] = {}
_candidate_validation_request_local = threading.local()

_UNIT_READ_SNAPSHOT_CACHE_MAX_ENTRIES = 4
_UNIT_READ_SNAPSHOT_CACHE_MAX_ENTRY_BYTES = 32 * 1024 * 1024
_UNIT_READ_SNAPSHOT_CACHE_MAX_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _UnitReadSnapshot:
    key: tuple[str, ...]
    doc_id: str
    lifecycle: str
    pipeline_run_count: int
    expected: tuple[tuple[str, str], ...]
    units: tuple[tuple[str, bytes], ...]
    retained_bytes: int


_unit_read_snapshot_cache_lock = threading.RLock()
_unit_read_snapshot_cache: OrderedDict[
    tuple[str, ...], _UnitReadSnapshot
] = OrderedDict()
_unit_read_snapshot_cache_bytes = 0

_STATE_FIELDS = {
    "schema_version",
    "doc_id",
    "lifecycle",
    "pipeline_run_count",
    "source",
    "candidate",
    "package",
    "draft_structure",
    "policies",
    "integrity",
}
_SOURCE_FIELDS = {"filename", "format", "sha256"}
_CANDIDATE_FIELDS = {
    "candidate_id",
    "relative_path",
    "tree_sha256",
    "file_count",
}
_PACKAGE_FIELDS = {
    "document",
    "structure",
    "asset_manifest",
    "admitted_projection",
    "normalization_receipt",
}
_IDENTITY_FIELDS = {"schema_version", "sha256"}
_DRAFT_FIELDS = {"report", "global_policy"}
_POLICY_FIELDS = {"admission", "global_structure"}
_INTEGRITY_FIELDS = {"payload_sha256"}
_V2_STATE_FIELDS = {
    "schema_version",
    "doc_id",
    "lifecycle",
    "pipeline_run_count",
    "source",
    "candidate",
    "package",
    "draft_structure",
    "policies",
    "bootstrap",
    "latest_decision",
    "hierarchy",
    "finalization",
    "experimental",
    "integrity",
}
_BOOTSTRAP_FIELDS = {"schema_version", "file_sha256", "payload_sha256"}
_DECISION_IDENTITY_FIELDS = {"schema_version", "sha256", "relative_path"}
_NULL_BINDING_FIELDS = {"schema_version", "sha256"}
_EXPERIMENTAL_FIELDS = {"authority", "load_bearing"}
_RUN_START_BINDING_FIELDS = {"schema_version", "sha256", "relative_path"}
_RUN_START_EVENT_FIELDS = {
    "schema_version",
    "doc_id",
    "run_id",
    "job_id",
    "parent",
    "runtime",
    "integrity",
}
_RUN_START_PARENT_FIELDS = {
    "state_sha256",
    "candidate_tree_sha256",
    "latest_decision_sha256",
    "hierarchy_sha256",
    "finalization_sha256",
}
_RUN_START_RUNTIME_FIELDS = {
    "manifest_schema_version",
    "manifest_sha256",
    "manifest_relative_path",
    "managed_source",
}
_MANAGED_RUNTIME_BINDING_FIELDS = {
    "schema_version",
    "state_sha256",
    "candidate",
    "package",
    "latest_decision",
    "hierarchy",
    "finalization",
}
_DECISION_FIELDS = {
    "schema_version",
    "doc_id",
    "operation",
    "source",
    "bootstrap",
    "parent",
    "request",
    "plan",
    "correction_receipt",
    "child",
    "hierarchy",
    "finalization",
    "integrity",
}
_DECISION_PARENT_FIELDS = {
    "state_schema_version",
    "state_sha256",
    "candidate_tree_sha256",
    "decision_sha256",
}
_DECISION_CHILD_FIELDS = {"candidate", "package", "draft_structure", "policies"}
_CORRECTION_REQUEST_FIELDS = {
    "expected_state_sha256",
    "expected_candidate_tree_sha256",
    "expected_report_sha256",
    "approved",
    "user",
    "actions",
}
_HIERARCHY_REQUEST_FIELDS = {
    "expected_state_sha256",
    "expected_candidate_tree_sha256",
    "expected_report_sha256",
    "approved",
    "user",
    "actions",
}
_FINALIZATION_REQUEST_FIELDS = {
    "expected_state_sha256",
    "expected_candidate_tree_sha256",
    "expected_report_sha256",
    "expected_hierarchy_sha256",
    "approved",
    "user",
}
_REVISION_EVENT_FIELDS = {
    "schema_version",
    "doc_id",
    "operation",
    "source",
    "bootstrap",
    "parent",
    "request",
    "child",
    "hierarchy",
    "finalization",
    "integrity",
}
_FINALIZATION_RECORD_FIELDS = {
    "schema_version",
    "doc_id",
    "lifecycle",
    "pipeline_run_count",
    "source",
    "bootstrap",
    "parent",
    "candidate",
    "package",
    "draft_structure",
    "policies",
    "hierarchy",
    "approved_by",
    "integrity",
}
_APPROVED_BY_FIELDS = {"kind", "identifier"}
_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_DEPTH = threading.local()


class SourceLifecycleError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _state_path(project_path: Path) -> Path:
    return project_path / "working" / STATE_FILENAME


def _state_v2_path(project_path: Path) -> Path:
    return project_path / "working" / STATE_V2_FILENAME


def _candidate_parent(project_path: Path) -> Path:
    return project_path / "working" / CANDIDATE_DIRECTORY


def _presegmented_capture_parent(project_path: Path) -> Path:
    return project_path / "working" / PRESEGMENTED_CAPTURE_DIRECTORY


def _decision_parent(project_path: Path) -> Path:
    return project_path / "working" / DECISION_DIRECTORY


def _hierarchy_parent(project_path: Path) -> Path:
    return project_path / "working" / HIERARCHY_DIRECTORY


def _finalization_parent(project_path: Path) -> Path:
    return project_path / "working" / FINALIZATION_DIRECTORY


def _run_start_parent(project_path: Path) -> Path:
    return project_path / "working" / RUN_START_DIRECTORY


def _run_start_path(project_path: Path, payload_sha256: str) -> Path:
    return _run_start_parent(project_path) / f"runstart_{payload_sha256}.json"


def _publication_parent(project_path: Path) -> Path:
    return project_path / "exports" / PUBLICATION_DIRECTORY


def _managed_state_exists(project_path: Path) -> bool:
    return _path_exists(_state_v2_path(project_path)) or _path_exists(
        _state_path(project_path)
    )


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_reparse_point(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = int(getattr(details, "st_file_attributes", 0))
    return stat.S_ISLNK(details.st_mode) or bool(attributes & 0x400)


def _require_plain_path(path: Path, *, owner: str) -> None:
    if _is_reparse_point(path):
        raise SourceLifecycleError(
            "source_package_path_unsafe",
            f"{owner} must not be a symbolic link or reparse point.",
            409,
        )


def _require_project_root(project_path: str | Path) -> Path:
    root = Path(project_path)
    if not root.is_dir():
        raise SourceLifecycleError(
            "missing_project",
            "Project directory is unavailable.",
            404,
        )
    _require_plain_path(root, owner="project root")
    return root.resolve(strict=True)


def _safe_tree_has_entries(path: Path, *, owner: str) -> bool:
    if not _path_exists(path):
        return False
    _require_plain_path(path, owner=owner)
    if not path.is_dir():
        raise SourceLifecycleError(
            "source_package_path_unsafe",
            f"{owner} must be a directory.",
            409,
        )
    found = False
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                f"{owner} could not be inspected safely.",
                409,
            ) from exc
        for entry in entries:
            found = True
            _require_plain_path(entry, owner=f"{owner} entry {entry.name}")
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise SourceLifecycleError(
                    "source_package_path_unsafe",
                    f"{owner} contains an unsupported filesystem entry.",
                    409,
                )
    return found


def _safe_file_exists(path: Path, *, owner: str) -> bool:
    if not _path_exists(path):
        return False
    _require_plain_path(path, owner=owner)
    if not path.is_file():
        raise SourceLifecycleError(
            "source_package_path_unsafe",
            f"{owner} must be a regular file.",
            409,
        )
    return True


def _external_runtime_evidence(
    doc_id: str,
    *,
    allowed_managed_runtime: dict[str, Any] | None = None,
) -> list[str]:
    jobs_root = Path(THESIS_JOBS_ROOT)
    if not _path_exists(jobs_root):
        return []
    _require_plain_path(jobs_root, owner="thesis jobs root")
    if not jobs_root.is_dir():
        raise SourceLifecycleError(
            "source_package_path_unsafe",
            "THESIS_JOBS_ROOT must be a directory.",
            409,
        )

    legacy_job_ids: set[str] = set()
    try:
        job_entries = sorted(jobs_root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise SourceLifecycleError(
            "source_package_path_unsafe",
            "THESIS_JOBS_ROOT could not be inspected safely.",
            409,
        ) from exc
    for entry in job_entries:
        _require_plain_path(entry, owner=f"thesis jobs entry {entry.name}")
        if not entry.is_dir():
            continue
        manifest_path = entry / "source_manifest.json"
        if not _path_exists(manifest_path):
            continue
        _require_plain_path(manifest_path, owner=f"runtime manifest {entry.name}")
        if not manifest_path.is_file():
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                "Runtime source_manifest.json must be a regular file.",
                409,
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or manifest.get("project_id") != doc_id:
            continue
        if manifest.get("job_id") != entry.name:
            raise SourceLifecycleError(
                "legacy_runtime_evidence_invalid",
                "Runtime evidence for this project has a mismatched registered job_id.",
                409,
            )
        if (
            allowed_managed_runtime is not None
            and manifest.get("contract_version") == MANAGED_RUNTIME_MANIFEST_VERSION
            and manifest.get("managed_source") == allowed_managed_runtime
        ):
            continue
        legacy_job_ids.add(entry.name)

    evidence: list[str] = []
    if legacy_job_ids:
        evidence.append("prepared_runtime")
    registry_path = jobs_root / "thesis_runs.jsonl"
    if legacy_job_ids and _path_exists(registry_path):
        _require_plain_path(registry_path, owner="thesis run registry")
        if not registry_path.is_file():
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                "Thesis run registry must be a regular file.",
                409,
            )
        try:
            lines = registry_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise SourceLifecycleError(
                "legacy_runtime_evidence_invalid",
                "Thesis run registry could not be read.",
                409,
            ) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("job_id") in legacy_job_ids:
                evidence.append("registered_run")
                break
    return evidence


def _legacy_occupancy(
    project_path: Path,
    *,
    doc_id: str,
    allowed_managed_runtime: dict[str, Any] | None = None,
) -> list[str]:
    evidence: list[str] = []
    if _safe_tree_has_entries(project_path / "canonical", owner="canonical directory"):
        evidence.append("canonical_data")
    if _safe_tree_has_entries(
        project_path / "working" / "jobs",
        owner="legacy project jobs directory",
    ):
        evidence.append("project_legacy_job")
    if _safe_file_exists(
        project_path / "working" / "extraction_report.json",
        owner="legacy extraction report",
    ):
        evidence.append("project_extraction_report")
    if _safe_tree_has_entries(
        project_path / "working" / "normalized",
        owner="legacy normalizer directory",
    ):
        evidence.append("project_normalizer_state")
    evidence.extend(
        _external_runtime_evidence(
            doc_id,
            allowed_managed_runtime=allowed_managed_runtime,
        )
    )
    return evidence


def _require_no_legacy_occupancy(project_path: Path, *, doc_id: str) -> None:
    evidence = _legacy_occupancy(project_path, doc_id=doc_id)
    if evidence:
        raise SourceLifecycleError(
            "legacy_project_not_adoptable",
            "Existing legacy project or runtime evidence prevents managed normalization: "
            + ", ".join(evidence),
            409,
        )


def _mutation_lock(project_path: Path) -> threading.RLock:
    key = os.path.normcase(str(project_path))
    with _MUTATION_LOCKS_GUARD:
        return _MUTATION_LOCKS.setdefault(key, threading.RLock())


def _try_acquire_os_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_is_contended(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN} or getattr(
        exc, "winerror", None
    ) in {33, 36}


@contextmanager
def _os_mutation_lock(project_path: Path) -> Iterator[None]:
    working = project_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    _require_plain_path(working, owner="working directory")
    lock_path = working / MUTATION_LOCK_FILENAME
    if _path_exists(lock_path):
        _require_plain_path(lock_path, owner="source lifecycle mutation lock")
        if not lock_path.is_file():
            raise SourceLifecycleError(
                "source_lifecycle_lock_invalid",
                "Source lifecycle mutation lock must be a regular file.",
                409,
            )
    deadline = time.monotonic() + MUTATION_LOCK_TIMEOUT_SECONDS
    acquired = False
    try:
        with lock_path.open("a+b") as handle:
            _require_plain_path(lock_path, owner="source lifecycle mutation lock")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            while True:
                try:
                    _try_acquire_os_lock(handle)
                    break
                except OSError as exc:
                    if not _lock_is_contended(exc):
                        raise
                    if time.monotonic() >= deadline:
                        raise SourceLifecycleError(
                            "source_lifecycle_busy",
                            "Another process is mutating this source project.",
                            423,
                        ) from exc
                    time.sleep(MUTATION_LOCK_POLL_SECONDS)
            acquired = True
            try:
                yield
            finally:
                _release_os_lock(handle)
    except SourceLifecycleError:
        raise
    except OSError as exc:
        if acquired:
            raise
        raise SourceLifecycleError(
            "source_lifecycle_lock_failed",
            f"Unable to acquire the source lifecycle mutation lock: {exc}",
            409,
        ) from exc


@contextmanager
def _managed_mutation_guard(project_path: Path) -> Iterator[None]:
    key = os.path.normcase(str(project_path))
    with _mutation_lock(project_path):
        depths = getattr(_MUTATION_DEPTH, "values", None)
        if depths is None:
            depths = {}
            _MUTATION_DEPTH.values = depths
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        with _os_mutation_lock(project_path):
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)


@contextmanager
def _managed_source_mutation_guard(project_path: Path) -> Iterator[None]:
    with _managed_mutation_guard(project_path):
        _unit_read_snapshot_cache_evict_project(project_path)
        try:
            yield
        finally:
            _unit_read_snapshot_cache_evict_project(project_path)


@contextmanager
def source_lifecycle_mutation_guard(
    project_path: str | Path,
) -> Iterator[Path]:
    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        yield root


def _require_confined_existing(path: Path, root: Path, *, owner: str) -> Path:
    _require_plain_path(path, owner=owner)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise SourceLifecycleError(
            "source_package_path_unsafe",
            f"{owner} is unavailable or escapes the project.",
            409,
        ) from exc
    return resolved


def _require_exact_fields(payload: dict[str, Any], expected: set[str], *, owner: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            f"{owner} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}.",
            409,
        )


def _require_sha256(value: Any, *, owner: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            f"{owner} must be a lowercase SHA-256 digest.",
            409,
        )
    return value


def _read_json_object(path: Path, *, owner: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceLifecycleError(
            "source_package_invalid",
            f"{owner} is unreadable or malformed.",
            409,
        ) from exc
    if not isinstance(payload, dict):
        raise SourceLifecycleError(
            "source_package_invalid",
            f"{owner} must contain a JSON object.",
            409,
        )
    return payload


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish_immutable_json(
    path: Path,
    payload: dict[str, Any],
    *,
    owner: str,
) -> bool:
    """Publish once in the single-process experimental lifecycle.

    Returning False means an exact byte-identical orphan was reused.  This is
    intentionally not a cross-process compare-and-swap primitive.
    """

    rendered = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if _path_exists(path):
        _require_plain_path(path, owner=owner)
        if not path.is_file() or path.read_bytes() != rendered:
            raise SourceLifecycleError(
                "source_package_decision_collision",
                f"{owner} exists with different bytes.",
                409,
            )
        return False
    _atomic_json_write(path, payload)
    if path.read_bytes() != rendered:
        raise SourceLifecycleError(
            "source_package_decision_collision",
            f"{owner} differs after publication.",
            409,
        )
    return True


def _server_source(project_path: Path) -> tuple[Path, str, str]:
    raw = project_path / "raw"
    if not raw.is_dir():
        raise SourceLifecycleError(
            "source_missing",
            "Upload a source file before normalization.",
            409,
        )
    _require_plain_path(raw, owner="raw source directory")
    files: list[Path] = []
    for entry in sorted(raw.iterdir(), key=lambda item: item.name.casefold()):
        _require_plain_path(entry, owner=f"raw source entry {entry.name}")
        if entry.is_file() and entry.suffix.casefold() in ALLOWED_SOURCE_EXTENSIONS:
            files.append(entry)
    if len(files) != 1:
        raise SourceLifecycleError(
            "source_ambiguous" if files else "source_missing",
            "The project must contain exactly one server-owned source file.",
            409,
        )
    source = _require_confined_existing(files[0], project_path, owner="source file")
    if source.name != f"source{source.suffix.casefold()}":
        raise SourceLifecycleError(
            "source_path_not_managed",
            "The source must be stored under its server-owned filename.",
            409,
        )
    try:
        source_format = detect_source_format(source)
    except ValueError as exc:
        raise SourceLifecycleError("source_format_unsupported", str(exc), 400) from exc
    return source, source_format, hashlib.sha256(source.read_bytes()).hexdigest()


def _project_state(doc_id: str) -> dict[str, Any]:
    return {
        "schema_version": DRAFT_PROJECT_STATE_VERSION,
        "doc_id": doc_id,
        "lifecycle": "draft",
        "pipeline_run_count": 0,
    }


def _identity(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "sha256": canonical_json_sha256(payload),
    }


def _load_core_package(
    candidate_root: Path,
    *,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
) -> dict[str, dict[str, Any]]:
    for filename in REQUIRED_PACKAGE_FILES - {REPORT_FILENAME}:
        path = candidate_root / filename
        if not path.is_file():
            raise SourceLifecycleError(
                "source_package_incomplete",
                f"Candidate is missing {filename}.",
                409,
            )
        _require_plain_path(path, owner=filename)
    document = _read_json_object(candidate_root / "document.json", owner="document.json")
    structure = _read_json_object(
        candidate_root / "structure_manifest.json",
        owner="structure_manifest.json",
    )
    receipt = _read_json_object(
        candidate_root / "normalization_receipt.json",
        owner="normalization_receipt.json",
    )
    asset_manifest = _read_json_object(
        candidate_root / "asset_manifest.json",
        owner="asset_manifest.json",
    )
    projection = _read_json_object(
        candidate_root / "admitted_projection_v1.json",
        owner="admitted_projection_v1.json",
    )

    if document.get("doc_id") != doc_id or structure.get("doc_id") != doc_id:
        raise SourceLifecycleError(
            "source_package_foreign_document",
            "Candidate document identity differs from the project.",
            409,
        )
    source = structure.get("source")
    if not isinstance(source, dict):
        raise SourceLifecycleError(
            "source_package_invalid",
            "Structure source identity must be an object.",
            409,
        )
    structure_path = source.get("path")
    if not isinstance(structure_path, str) or not structure_path:
        raise SourceLifecycleError(
            "source_package_invalid",
            "Structure source path is missing.",
            409,
        )
    try:
        bound_source = Path(structure_path).resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise SourceLifecycleError(
            "source_package_source_unavailable",
            "Candidate source path is unavailable.",
            409,
        ) from exc
    if bound_source != source_path:
        raise SourceLifecycleError(
            "source_package_foreign_source",
            "Candidate is bound to a different source path.",
            409,
        )
    if source.get("format") != source_format or source.get("sha256") != source_sha256:
        raise SourceLifecycleError(
            "source_package_source_changed",
            "Candidate source identity differs from the uploaded source.",
            409,
        )
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("source_format") != source_format:
        raise SourceLifecycleError(
            "source_package_format_mismatch",
            "Document source format differs from the uploaded source.",
            409,
        )

    try:
        expected_receipt = validate_normalization_contract(
            document,
            structure,
            expected_format=source_format,
        )
        if receipt != expected_receipt:
            raise ValueError("normalization receipt differs from authoritative inputs")
        validate_canonical_source_package(
            document,
            structure,
            asset_manifest,
            package_root=candidate_root,
        )
        validate_admitted_projection(
            projection,
            document,
            structure,
            asset_manifest,
        )
    except ValueError as exc:
        raise SourceLifecycleError(
            "source_package_invalid",
            str(exc),
            409,
        ) from exc
    return {
        "document": document,
        "structure": structure,
        "normalization_receipt": receipt,
        "asset_manifest": asset_manifest,
        "admitted_projection": projection,
    }


def _tree_files(candidate_root: Path) -> list[Path]:
    _require_plain_path(candidate_root, owner="candidate directory")
    files: list[Path] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name.casefold())
        for entry in ordered:
            path = Path(entry.path)
            _require_plain_path(path, owner="candidate entry")
            if entry.is_dir(follow_symlinks=False):
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                raise SourceLifecycleError(
                    "source_package_path_unsafe",
                    "Candidate contains an unsupported filesystem entry.",
                    409,
                )

    visit(candidate_root)
    files.sort(key=lambda item: item.relative_to(candidate_root).as_posix())
    return files


def _expected_files(asset_manifest: dict[str, Any]) -> set[str]:
    expected = set(REQUIRED_PACKAGE_FILES)
    assets = asset_manifest.get("assets")
    if not isinstance(assets, list):
        raise SourceLifecycleError(
            "source_package_invalid",
            "Asset manifest assets must be a list.",
            409,
        )
    for asset in assets:
        if not isinstance(asset, dict):
            raise SourceLifecycleError(
                "source_package_invalid",
                "Asset rows must be objects.",
                409,
            )
        if asset.get("availability") != "materialized":
            continue
        package_path = asset.get("package_path")
        if not isinstance(package_path, str):
            raise SourceLifecycleError(
                "source_package_invalid",
                "Materialized asset path is missing.",
                409,
            )
        relative = PurePosixPath(package_path)
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                "Materialized asset path is not normalized.",
                409,
            )
        expected.add(relative.as_posix())
    return expected


def _materialized_asset_identity(
    candidate_root: Path,
    asset_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset in asset_manifest["assets"]:
        if asset.get("availability") != "materialized":
            continue
        package_path = str(asset["package_path"])
        path = _require_confined_existing(
            candidate_root / Path(*PurePosixPath(package_path).parts),
            candidate_root,
            owner="materialized asset",
        )
        rows.append(
            {
                "path": package_path,
                "size": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )
    rows.sort(key=lambda row: row["path"])
    return rows


def _validate_deterministic_child_sidecars(
    child_root: Path,
    child_evidence: dict[str, Any],
    *,
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
) -> None:
    try:
        with tempfile.TemporaryDirectory(
            prefix="source-package-lineage-",
        ) as temporary:
            expected_root = Path(temporary)
            result = materialize_source_package(
                document,
                structure_manifest,
                expected_root,
            )
            expected_manifest = _read_json_object(
                result.asset_manifest_path,
                owner="deterministic asset_manifest.json",
            )
            expected_projection = _read_json_object(
                result.admitted_projection_path,
                owner="deterministic admitted_projection_v1.json",
            )
            if child_evidence["asset_manifest"] != expected_manifest:
                raise SourceLifecycleError(
                    "source_package_decision_invalid",
                    "Decision child asset manifest differs from deterministic "
                    "correction materialization.",
                    409,
                )
            if child_evidence["admitted_projection"] != expected_projection:
                raise SourceLifecycleError(
                    "source_package_decision_invalid",
                    "Decision child admission differs from deterministic "
                    "correction materialization.",
                    409,
                )
            if _materialized_asset_identity(
                child_root,
                child_evidence["asset_manifest"],
            ) != _materialized_asset_identity(
                expected_root,
                expected_manifest,
            ):
                raise SourceLifecycleError(
                    "source_package_decision_invalid",
                    "Decision child asset bytes differ from deterministic "
                    "correction materialization.",
                    409,
                )
    except SourceLifecycleError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            f"Deterministic correction materialization failed: {exc}",
            409,
        ) from exc


def _tree_identity(candidate_root: Path, asset_manifest: dict[str, Any]) -> dict[str, Any]:
    files = _tree_files(candidate_root)
    actual = {path.relative_to(candidate_root).as_posix() for path in files}
    expected = _expected_files(asset_manifest)
    if actual != expected:
        raise SourceLifecycleError(
            "source_package_file_set_invalid",
            f"Candidate file set differs; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}.",
            409,
        )
    rows = [
        {
            "path": path.relative_to(candidate_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in files
    ]
    return {
        "tree_sha256": canonical_json_sha256(rows),
        "file_count": len(rows),
        "rows": rows,
    }


def _candidate_validation_state_scope(state: dict[str, Any]) -> str:
    return canonical_json_sha256(
        {
            "schema_version": state.get("schema_version"),
            "state_sha256": (state.get("integrity") or {}).get("payload_sha256"),
            "candidate": state.get("candidate"),
            "package": state.get("package"),
            "draft_structure": state.get("draft_structure"),
            "policies": state.get("policies"),
        }
    )


def _candidate_validation_cache_key(
    candidate_root: Path,
    *,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
    validation_scope: str | None,
) -> tuple[str, str, str, str, str, str]:
    return (
        os.path.normcase(str(candidate_root.resolve(strict=False))),
        os.path.normcase(str(source_path.resolve(strict=False))),
        source_format,
        source_sha256,
        doc_id,
        validation_scope or "unscoped",
    )


def _candidate_validation_cache_base(
    key: tuple[str, str, str, str, str, str],
) -> tuple[str, str, str, str, str]:
    return key[:5]


def _candidate_validation_cache_forget(
    key: tuple[str, str, str, str, str, str],
    entry: dict[str, Any],
) -> None:
    with _candidate_validation_cache_lock:
        if _candidate_validation_cache.get(key) is entry:
            _candidate_validation_cache.pop(key, None)


def _candidate_validation_cache_remember(
    key: tuple[str, str, str, str, str, str],
    evidence: dict[str, Any],
) -> None:
    with _candidate_validation_cache_lock:
        _candidate_validation_cache[key] = {"evidence": evidence}
        _candidate_validation_cache.move_to_end(key)
        while len(_candidate_validation_cache) > _CANDIDATE_VALIDATION_CACHE_MAX_ENTRIES:
            _candidate_validation_cache.popitem(last=False)


def _candidate_validation_cache_entry(
    key: tuple[str, str, str, str, str, str],
) -> tuple[tuple[str, str, str, str, str, str], dict[str, Any]] | None:
    with _candidate_validation_cache_lock:
        entry = _candidate_validation_cache.get(key)
        if entry is None:
            return None
        _candidate_validation_cache.move_to_end(key)
        return key, entry


def _candidate_validation_cache_clear() -> None:
    with _candidate_validation_cache_lock:
        _candidate_validation_cache.clear()


def _normalized_cache_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _unit_read_snapshot_cache_key(
    project_path: Path,
    candidate_root: Path,
    *,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
    expected: dict[str, str],
) -> tuple[str, ...]:
    return (
        _normalized_cache_path(project_path),
        _normalized_cache_path(candidate_root),
        _normalized_cache_path(source_path),
        doc_id,
        source_format,
        source_sha256,
        *(expected[name] for name in SOURCE_PACKAGE_REVIEW_BINDING_FIELDS),
    )


def _unit_read_snapshot_cache_entry(
    key: tuple[str, ...],
) -> _UnitReadSnapshot | None:
    with _unit_read_snapshot_cache_lock:
        snapshot = _unit_read_snapshot_cache.get(key)
        if snapshot is not None:
            _unit_read_snapshot_cache.move_to_end(key)
        return snapshot


def _unit_read_snapshot_cache_remember(
    snapshot: _UnitReadSnapshot,
) -> _UnitReadSnapshot:
    global _unit_read_snapshot_cache_bytes
    if (
        snapshot.retained_bytes > _UNIT_READ_SNAPSHOT_CACHE_MAX_ENTRY_BYTES
        or snapshot.retained_bytes > _UNIT_READ_SNAPSHOT_CACHE_MAX_TOTAL_BYTES
    ):
        return snapshot
    with _unit_read_snapshot_cache_lock:
        existing = _unit_read_snapshot_cache.get(snapshot.key)
        if existing is not None:
            _unit_read_snapshot_cache.move_to_end(snapshot.key)
            return existing
        while _unit_read_snapshot_cache and (
            len(_unit_read_snapshot_cache)
            >= _UNIT_READ_SNAPSHOT_CACHE_MAX_ENTRIES
            or _unit_read_snapshot_cache_bytes + snapshot.retained_bytes
            > _UNIT_READ_SNAPSHOT_CACHE_MAX_TOTAL_BYTES
        ):
            _old_key, old = _unit_read_snapshot_cache.popitem(last=False)
            _unit_read_snapshot_cache_bytes -= old.retained_bytes
        _unit_read_snapshot_cache[snapshot.key] = snapshot
        _unit_read_snapshot_cache_bytes += snapshot.retained_bytes
        return snapshot


def _unit_read_snapshot_cache_evict_project(project_path: Path) -> None:
    global _unit_read_snapshot_cache_bytes
    project_key = _normalized_cache_path(project_path)
    with _unit_read_snapshot_cache_lock:
        stale = [
            key for key in _unit_read_snapshot_cache if key[0] == project_key
        ]
        for key in stale:
            snapshot = _unit_read_snapshot_cache.pop(key)
            _unit_read_snapshot_cache_bytes -= snapshot.retained_bytes


def _unit_read_snapshot_cache_clear() -> None:
    global _unit_read_snapshot_cache_bytes
    with _unit_read_snapshot_cache_lock:
        _unit_read_snapshot_cache.clear()
        _unit_read_snapshot_cache_bytes = 0


@contextmanager
def _candidate_validation_request_scope() -> Iterator[None]:
    current = getattr(_candidate_validation_request_local, "evidence", None)
    if current is not None:
        yield
        return
    _candidate_validation_request_local.evidence = {}
    try:
        yield
    finally:
        del _candidate_validation_request_local.evidence


def _validate_candidate_uncached(
    candidate_root: Path,
    *,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
) -> dict[str, Any]:
    if not _path_exists(candidate_root):
        raise SourceLifecycleError(
            "source_package_candidate_missing",
            "Managed source-package candidate is unavailable.",
            409,
        )
    _require_plain_path(candidate_root, owner="candidate directory")
    if not candidate_root.is_dir():
        raise SourceLifecycleError(
            "source_package_candidate_missing",
            "Managed source-package candidate is not a directory.",
            409,
        )
    package = _load_core_package(
        candidate_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
    )
    report_path = candidate_root / REPORT_FILENAME
    if not report_path.is_file():
        raise SourceLifecycleError(
            "source_package_incomplete",
            f"Candidate is missing {REPORT_FILENAME}.",
            409,
        )
    report = _read_json_object(report_path, owner=REPORT_FILENAME)
    project_state = _project_state(doc_id)
    try:
        validate_draft_structure_report_shape(report)
        expected_report = build_draft_structure_report(
            package["document"],
            package["structure"],
            package["asset_manifest"],
            package["admitted_projection"],
            project_state,
            package_root=candidate_root,
        )
    except ValueError as exc:
        raise SourceLifecycleError(
            "draft_structure_report_invalid",
            str(exc),
            409,
        ) from exc
    if report != expected_report:
        raise SourceLifecycleError(
            "draft_structure_report_stale",
            "Draft Structure report differs from authoritative package inputs.",
            409,
        )
    tree = _tree_identity(candidate_root, package["asset_manifest"])
    package_identities = {
        "document": _identity(package["document"]),
        "structure": _identity(package["structure"]),
        "asset_manifest": _identity(package["asset_manifest"]),
        "admitted_projection": _identity(package["admitted_projection"]),
        "normalization_receipt": _identity(package["normalization_receipt"]),
    }
    global_policy = report["global_skeleton"]["inputs"]["policy"]
    policies = {
        "admission": copy.deepcopy(package["admitted_projection"]["policy"]),
        "global_structure": copy.deepcopy(global_policy),
    }
    return {
        **package,
        "report": report,
        "tree": tree,
        "package_identities": package_identities,
        "policies": policies,
    }


def _validate_candidate(
    candidate_root: Path,
    *,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
    validation_scope: str | None = None,
) -> dict[str, Any]:
    key = _candidate_validation_cache_key(
        candidate_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
        validation_scope=validation_scope,
    )
    base = _candidate_validation_cache_base(key)
    request_evidence = getattr(_candidate_validation_request_local, "evidence", None)
    if request_evidence is not None and base in request_evidence:
        evidence = request_evidence[base]
        _candidate_validation_cache_remember(key, evidence)
        return evidence

    while True:
        cached = _candidate_validation_cache_entry(key)
        if cached is not None:
            matched_key, entry = cached
            evidence = entry["evidence"]
            try:
                current_tree = _tree_identity(
                    candidate_root,
                    evidence["asset_manifest"],
                )
            except SourceLifecycleError:
                _candidate_validation_cache_forget(matched_key, entry)
                raise
            if current_tree == evidence["tree"]:
                if matched_key != key:
                    _candidate_validation_cache_remember(key, evidence)
                if request_evidence is not None:
                    request_evidence[base] = evidence
                return evidence
            _candidate_validation_cache_forget(matched_key, entry)

        with _candidate_validation_cache_lock:
            if _candidate_validation_cache_entry(key) is not None:
                continue
            waiter = _candidate_validation_inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                _candidate_validation_inflight[key] = waiter
                owns_validation = True
            else:
                owns_validation = False
        if owns_validation:
            break
        waiter.wait()

    try:
        evidence = _validate_candidate_uncached(
            candidate_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        _candidate_validation_cache_remember(key, evidence)
        if request_evidence is not None:
            request_evidence[base] = evidence
        return evidence
    finally:
        with _candidate_validation_cache_lock:
            completed = _candidate_validation_inflight.pop(key, None)
            if completed is not None:
                completed.set()


def _files_byte_identical(left: Path, right: Path) -> bool:
    left_files = _tree_files(left)
    right_files = _tree_files(right)
    left_names = [path.relative_to(left).as_posix() for path in left_files]
    right_names = [path.relative_to(right).as_posix() for path in right_files]
    if left_names != right_names:
        return False
    return all(
        left_path.read_bytes() == right_path.read_bytes()
        for left_path, right_path in zip(left_files, right_files, strict=True)
    )


def _state_payload(
    *,
    project_path: Path,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
    candidate_root: Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = candidate_root.name
    relative_path = candidate_root.relative_to(project_path).as_posix()
    report = evidence["report"]
    payload: dict[str, Any] = {
        "schema_version": SOURCE_LIFECYCLE_VERSION,
        "doc_id": doc_id,
        "lifecycle": "draft",
        "pipeline_run_count": 0,
        "source": {
            "filename": source_path.name,
            "format": source_format,
            "sha256": source_sha256,
        },
        "candidate": {
            "candidate_id": candidate_id,
            "relative_path": relative_path,
            "tree_sha256": evidence["tree"]["tree_sha256"],
            "file_count": evidence["tree"]["file_count"],
        },
        "package": copy.deepcopy(evidence["package_identities"]),
        "draft_structure": {
            "report": {
                "schema_version": report.get("schema_version"),
                "sha256": canonical_json_sha256(report),
            },
            "global_policy": copy.deepcopy(evidence["policies"]["global_structure"]),
        },
        "policies": copy.deepcopy(evidence["policies"]),
    }
    payload["integrity"] = {"payload_sha256": canonical_json_sha256(payload)}
    return payload


def _bootstrap_identity(project_path: Path, state_v1: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(project_path)
    return {
        "schema_version": SOURCE_LIFECYCLE_VERSION,
        "file_sha256": _file_sha256(path),
        "payload_sha256": state_v1["integrity"]["payload_sha256"],
    }


def _null_binding(schema_version: str) -> dict[str, Any]:
    return {"schema_version": schema_version, "sha256": None}


def _v2_state_payload(
    *,
    doc_id: str,
    source: dict[str, Any],
    bootstrap: dict[str, Any],
    decision_sha256: str,
    child: dict[str, Any],
    decision_schema_version: str = SOURCE_PACKAGE_DECISION_VERSION,
    hierarchy: dict[str, Any] | None = None,
    finalization: dict[str, Any] | None = None,
    lifecycle: str = "draft",
    authority: str = "single_app_process",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SOURCE_LIFECYCLE_V2_VERSION,
        "doc_id": doc_id,
        "lifecycle": lifecycle,
        "pipeline_run_count": 0,
        "source": copy.deepcopy(source),
        "candidate": copy.deepcopy(child["candidate"]),
        "package": copy.deepcopy(child["package"]),
        "draft_structure": copy.deepcopy(child["draft_structure"]),
        "policies": copy.deepcopy(child["policies"]),
        "bootstrap": copy.deepcopy(bootstrap),
        "latest_decision": {
            "schema_version": decision_schema_version,
            "sha256": decision_sha256,
            "relative_path": (
                f"working/{DECISION_DIRECTORY}/srcdec_{decision_sha256}.json"
            ),
        },
        "hierarchy": copy.deepcopy(
            hierarchy or _null_binding(HIERARCHY_OVERLAY_VERSION)
        ),
        "finalization": copy.deepcopy(
            finalization or _null_binding(SOURCE_PACKAGE_FINALIZATION_VERSION)
        ),
        "experimental": {
            "authority": authority,
            "load_bearing": False,
        },
    }
    payload["integrity"] = {"payload_sha256": canonical_json_sha256(payload)}
    return payload


def _managed_runtime_binding(state: dict[str, Any]) -> dict[str, Any]:
    if (
        state.get("schema_version") != SOURCE_LIFECYCLE_V2_VERSION
        or state.get("lifecycle") != "finalized_pre_run"
        or state.get("pipeline_run_count") != 0
        or not isinstance(state.get("finalization"), dict)
        or state["finalization"].get("sha256") is None
    ):
        raise SourceLifecycleError(
            "source_package_not_finalized",
            "Managed runtime preparation requires finalized_pre_run state.",
            409,
        )
    return {
        "schema_version": MANAGED_RUNTIME_BINDING_VERSION,
        "state_sha256": state["integrity"]["payload_sha256"],
        "candidate": copy.deepcopy(state["candidate"]),
        "package": copy.deepcopy(state["package"]),
        "latest_decision": copy.deepcopy(state["latest_decision"]),
        "hierarchy": copy.deepcopy(state["hierarchy"]),
        "finalization": copy.deepcopy(state["finalization"]),
    }


def _frozen_state_payload(
    parent_state: dict[str, Any],
    *,
    run_start_sha256: str,
) -> dict[str, Any]:
    payload = copy.deepcopy(parent_state)
    payload.pop("integrity", None)
    payload["lifecycle"] = "run_started_frozen"
    payload["pipeline_run_count"] = 1
    payload["run_start"] = {
        "schema_version": SOURCE_PACKAGE_RUN_START_VERSION,
        "sha256": run_start_sha256,
        "relative_path": (
            f"working/{RUN_START_DIRECTORY}/runstart_{run_start_sha256}.json"
        ),
    }
    payload["experimental"] = {
        "authority": "os_locked_first_run",
        "load_bearing": True,
    }
    payload["integrity"] = {"payload_sha256": canonical_json_sha256(payload)}
    return payload


def _validate_correction_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SourceLifecycleError(
            "source_package_correction_invalid",
            "Correction request must be a JSON object.",
            400,
        )
    actual = set(payload)
    if actual != _CORRECTION_REQUEST_FIELDS:
        raise SourceLifecycleError(
            "source_package_correction_invalid",
            "Correction request fields differ; "
            f"missing={sorted(_CORRECTION_REQUEST_FIELDS - actual)}, "
            f"extra={sorted(actual - _CORRECTION_REQUEST_FIELDS)}.",
            400,
        )
    for name in (
        "expected_state_sha256",
        "expected_candidate_tree_sha256",
        "expected_report_sha256",
    ):
        try:
            _require_sha256(payload.get(name), owner=name)
        except SourceLifecycleError as exc:
            raise SourceLifecycleError(
                "source_package_correction_invalid",
                str(exc),
                400,
            ) from exc
    if payload.get("approved") is not True:
        raise SourceLifecycleError(
            "source_package_correction_approval_required",
            "Correction request requires approved=true.",
            400,
        )
    user = payload.get("user")
    if not isinstance(user, str) or not user.strip():
        raise SourceLifecycleError(
            "source_package_correction_invalid",
            "Correction request user must be a nonempty string.",
            400,
        )
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise SourceLifecycleError(
            "source_package_correction_actions_required",
            "Correction request must contain at least one action.",
            400,
        )
    if any(not isinstance(action, dict) for action in actions):
        raise SourceLifecycleError(
            "source_package_correction_invalid",
            "Every correction action must be an object.",
            400,
        )
    return {
        "expected_state_sha256": payload["expected_state_sha256"],
        "expected_candidate_tree_sha256": payload[
            "expected_candidate_tree_sha256"
        ],
        "expected_report_sha256": payload["expected_report_sha256"],
        "approved": True,
        "user": user.strip(),
        "actions": copy.deepcopy(actions),
    }


def _validate_hierarchy_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _HIERARCHY_REQUEST_FIELDS:
        raise SourceLifecycleError(
            "source_package_hierarchy_invalid",
            "Hierarchy request fields differ from the closed contract.",
            400,
        )
    for name in (
        "expected_state_sha256",
        "expected_candidate_tree_sha256",
        "expected_report_sha256",
    ):
        try:
            _require_sha256(payload.get(name), owner=name)
        except SourceLifecycleError as exc:
            raise SourceLifecycleError(
                "source_package_hierarchy_invalid", str(exc), 400
            ) from exc
    if payload.get("approved") is not True:
        raise SourceLifecycleError(
            "source_package_hierarchy_approval_required",
            "Hierarchy request requires approved=true.",
            400,
        )
    user = payload.get("user")
    actions = payload.get("actions")
    if not isinstance(user, str) or not user.strip():
        raise SourceLifecycleError(
            "source_package_hierarchy_invalid",
            "Hierarchy request user must be a nonempty string.",
            400,
        )
    if not isinstance(actions, list) or any(
        not isinstance(action, dict) for action in actions
    ):
        raise SourceLifecycleError(
            "source_package_hierarchy_invalid",
            "Hierarchy actions must be a list of objects.",
            400,
        )
    return {
        "expected_state_sha256": payload["expected_state_sha256"],
        "expected_candidate_tree_sha256": payload[
            "expected_candidate_tree_sha256"
        ],
        "expected_report_sha256": payload["expected_report_sha256"],
        "approved": True,
        "user": user.strip(),
        "actions": copy.deepcopy(actions),
    }


def _validate_finalization_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _FINALIZATION_REQUEST_FIELDS:
        raise SourceLifecycleError(
            "source_package_finalization_invalid",
            "Finalization request fields differ from the closed contract.",
            400,
        )
    for name in (
        "expected_state_sha256",
        "expected_candidate_tree_sha256",
        "expected_report_sha256",
    ):
        try:
            _require_sha256(payload.get(name), owner=name)
        except SourceLifecycleError as exc:
            raise SourceLifecycleError(
                "source_package_finalization_invalid", str(exc), 400
            ) from exc
    hierarchy_sha = payload.get("expected_hierarchy_sha256")
    if hierarchy_sha is not None:
        try:
            _require_sha256(hierarchy_sha, owner="expected_hierarchy_sha256")
        except SourceLifecycleError as exc:
            raise SourceLifecycleError(
                "source_package_finalization_invalid", str(exc), 400
            ) from exc
    if payload.get("approved") is not True:
        raise SourceLifecycleError(
            "source_package_finalization_approval_required",
            "Finalization request requires approved=true.",
            400,
        )
    user = payload.get("user")
    if not isinstance(user, str) or not user.strip():
        raise SourceLifecycleError(
            "source_package_finalization_invalid",
            "Finalization request user must be a nonempty string.",
            400,
        )
    return {
        "expected_state_sha256": payload["expected_state_sha256"],
        "expected_candidate_tree_sha256": payload[
            "expected_candidate_tree_sha256"
        ],
        "expected_report_sha256": payload["expected_report_sha256"],
        "expected_hierarchy_sha256": hierarchy_sha,
        "approved": True,
        "user": user.strip(),
    }


def _classification_from_report_unit(unit: dict[str, Any]) -> str:
    policy = str(unit.get("translation_policy") or "")
    if unit.get("review_required") is True or policy == "review":
        return "review"
    if policy in {"translate", "preserve", "exclude"}:
        return policy
    raise SourceLifecycleError(
        "source_package_correction_invalid",
        f"Unit {unit.get('unit_id')} has an unsupported translation policy.",
        409,
    )


def _require_effective_actions(
    plan: dict[str, Any],
    report: dict[str, Any],
) -> None:
    units = {str(row["unit_id"]): row for row in report["units"]}
    for action in plan["actions"]:
        if action.get("status") != "candidate":
            raise SourceLifecycleError(
                "source_package_correction_review_required",
                "Every server-built action must be a candidate before the request can apply.",
                409,
            )
        if action["action_type"] != "update_unit":
            continue
        parameters = action["parameters"]
        unit = units[action["target_unit_ids"][0]]
        title_changes = (
            parameters.get("new_title") is not None
            and parameters["new_title"] != unit["title"]
        )
        classification_changes = (
            parameters.get("classification") is not None
            and parameters["classification"]
            != _classification_from_report_unit(unit)
        )
        if not title_changes and not classification_changes:
            raise SourceLifecycleError(
                "source_package_correction_noop",
                f"Action {action['action_id']} does not change its target unit.",
                409,
            )


def _validate_state_shape(state: dict[str, Any], *, doc_id: str) -> None:
    _require_exact_fields(state, _STATE_FIELDS, owner="source lifecycle")
    if state.get("schema_version") != SOURCE_LIFECYCLE_VERSION:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Source lifecycle schema version differs.",
            409,
        )
    if state.get("doc_id") != doc_id:
        raise SourceLifecycleError(
            "source_lifecycle_foreign_project",
            "Source lifecycle belongs to a different project.",
            409,
        )
    run_count = state.get("pipeline_run_count")
    if isinstance(run_count, bool) or not isinstance(run_count, int):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Source lifecycle pipeline_run_count must be an integer.",
            409,
        )
    if state.get("lifecycle") != "draft" or run_count != 0:
        raise SourceLifecycleError(
            "source_lifecycle_frozen",
            "Managed source lifecycle is no longer an editable draft.",
            409,
        )
    for name, fields in {
        "source": _SOURCE_FIELDS,
        "candidate": _CANDIDATE_FIELDS,
        "package": _PACKAGE_FIELDS,
        "draft_structure": _DRAFT_FIELDS,
        "policies": _POLICY_FIELDS,
        "integrity": _INTEGRITY_FIELDS,
    }.items():
        value = state.get(name)
        if not isinstance(value, dict):
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                f"source lifecycle {name} must be an object.",
                409,
            )
        _require_exact_fields(value, fields, owner=f"source lifecycle {name}")
    for name, identity in state["package"].items():
        if not isinstance(identity, dict):
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                f"package identity {name} must be an object.",
                409,
            )
        _require_exact_fields(identity, _IDENTITY_FIELDS, owner=f"package identity {name}")
        _require_sha256(identity.get("sha256"), owner=f"package identity {name}.sha256")
    report_identity = state["draft_structure"].get("report")
    if not isinstance(report_identity, dict):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Draft report identity must be an object.",
            409,
        )
    _require_exact_fields(report_identity, _IDENTITY_FIELDS, owner="draft report identity")
    _require_sha256(report_identity.get("sha256"), owner="draft report identity.sha256")
    for owner, value in {
        "source.sha256": state["source"].get("sha256"),
        "candidate.tree_sha256": state["candidate"].get("tree_sha256"),
        "integrity.payload_sha256": state["integrity"].get("payload_sha256"),
    }.items():
        _require_sha256(value, owner=owner)
    file_count = state["candidate"].get("file_count")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count <= 0:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate file_count must be a positive integer.",
            409,
        )
    expected_integrity = canonical_json_sha256(
        {key: copy.deepcopy(value) for key, value in state.items() if key != "integrity"}
    )
    if state["integrity"].get("payload_sha256") != expected_integrity:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Source lifecycle payload hash differs.",
            409,
        )


def _validate_v2_state_shape(state: dict[str, Any], *, doc_id: str) -> None:
    lifecycle = state.get("lifecycle")
    expected_fields = (
        _V2_STATE_FIELDS | {"run_start"}
        if lifecycle == "run_started_frozen"
        else _V2_STATE_FIELDS
    )
    _require_exact_fields(state, expected_fields, owner="source lifecycle v2")
    if state.get("schema_version") != SOURCE_LIFECYCLE_V2_VERSION:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Source lifecycle v2 schema version differs.",
            409,
        )
    if state.get("doc_id") != doc_id:
        raise SourceLifecycleError(
            "source_lifecycle_foreign_project",
            "Source lifecycle v2 belongs to a different project.",
            409,
        )
    run_count = state.get("pipeline_run_count")
    if isinstance(run_count, bool) or not isinstance(run_count, int):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Source lifecycle v2 pipeline_run_count must be an integer.",
            409,
        )
    if lifecycle in {"draft", "finalized_pre_run"}:
        valid_run_count = run_count == 0
    elif lifecycle == "run_started_frozen":
        valid_run_count = run_count >= 1
    else:
        valid_run_count = False
    if not valid_run_count:
        raise SourceLifecycleError(
            "source_lifecycle_frozen",
            "Managed source lifecycle state or pipeline_run_count differs.",
            409,
        )
    shaped_fields = {
        "source": _SOURCE_FIELDS,
        "candidate": _CANDIDATE_FIELDS,
        "package": _PACKAGE_FIELDS,
        "draft_structure": _DRAFT_FIELDS,
        "policies": _POLICY_FIELDS,
        "bootstrap": _BOOTSTRAP_FIELDS,
        "latest_decision": _DECISION_IDENTITY_FIELDS,
        "hierarchy": _NULL_BINDING_FIELDS,
        "finalization": _NULL_BINDING_FIELDS,
        "experimental": _EXPERIMENTAL_FIELDS,
        "integrity": _INTEGRITY_FIELDS,
    }
    if lifecycle == "run_started_frozen":
        shaped_fields["run_start"] = _RUN_START_BINDING_FIELDS
    for name, fields in shaped_fields.items():
        value = state.get(name)
        if not isinstance(value, dict):
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                f"source lifecycle v2 {name} must be an object.",
                409,
            )
        _require_exact_fields(value, fields, owner=f"source lifecycle v2 {name}")
    for name, identity in state["package"].items():
        if not isinstance(identity, dict):
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                f"package identity {name} must be an object.",
                409,
            )
        _require_exact_fields(identity, _IDENTITY_FIELDS, owner=f"package identity {name}")
        _require_sha256(identity.get("sha256"), owner=f"package identity {name}.sha256")
    report_identity = state["draft_structure"].get("report")
    if not isinstance(report_identity, dict):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Draft report identity must be an object.",
            409,
        )
    _require_exact_fields(report_identity, _IDENTITY_FIELDS, owner="draft report identity")
    _require_sha256(report_identity.get("sha256"), owner="draft report identity.sha256")
    for owner, value in {
        "source.sha256": state["source"].get("sha256"),
        "candidate.tree_sha256": state["candidate"].get("tree_sha256"),
        "bootstrap.file_sha256": state["bootstrap"].get("file_sha256"),
        "bootstrap.payload_sha256": state["bootstrap"].get("payload_sha256"),
        "latest_decision.sha256": state["latest_decision"].get("sha256"),
        "integrity.payload_sha256": state["integrity"].get("payload_sha256"),
    }.items():
        _require_sha256(value, owner=owner)
    if state["bootstrap"].get("schema_version") != SOURCE_LIFECYCLE_VERSION:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Lifecycle bootstrap schema version differs.",
            409,
        )
    if state["latest_decision"].get("schema_version") not in {
        SOURCE_PACKAGE_DECISION_VERSION,
        SOURCE_PACKAGE_REVISION_VERSION,
    }:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Latest decision schema version differs.",
            409,
        )
    decision_sha = state["latest_decision"]["sha256"]
    expected_decision_path = f"working/{DECISION_DIRECTORY}/srcdec_{decision_sha}.json"
    if state["latest_decision"].get("relative_path") != expected_decision_path:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Latest decision path differs from its content address.",
            409,
        )
    for name, expected_version in {
        "hierarchy": HIERARCHY_OVERLAY_VERSION,
        "finalization": SOURCE_PACKAGE_FINALIZATION_VERSION,
    }.items():
        if state[name].get("schema_version") != expected_version:
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                f"{name} binding schema version differs.",
                409,
            )
        binding_sha = state[name].get("sha256")
        if binding_sha is not None:
            _require_sha256(binding_sha, owner=f"{name}.sha256")
    if lifecycle == "draft" and state["finalization"]["sha256"] is not None:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Draft lifecycle must not retain a finalization binding.",
            409,
        )
    if (
        lifecycle in {"finalized_pre_run", "run_started_frozen"}
        and state["finalization"]["sha256"] is None
    ):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Finalized pre-run lifecycle requires a finalization binding.",
            409,
        )
    expected_experimental = (
        {"authority": "os_locked_first_run", "load_bearing": True}
        if lifecycle == "run_started_frozen"
        else None
    )
    if expected_experimental is None:
        valid_experimental = (
            state["experimental"].get("authority")
            in {"single_app_process", "os_locked_pre_run"}
            and state["experimental"].get("load_bearing") is False
        )
    else:
        valid_experimental = state["experimental"] == expected_experimental
    if not valid_experimental:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Managed pre-run authority metadata differs.",
            409,
        )
    if lifecycle == "run_started_frozen":
        run_start = state["run_start"]
        if run_start.get("schema_version") != SOURCE_PACKAGE_RUN_START_VERSION:
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "Run-start binding schema version differs.",
                409,
            )
        run_start_sha = _require_sha256(
            run_start.get("sha256"), owner="run_start.sha256"
        )
        expected_path = (
            f"working/{RUN_START_DIRECTORY}/runstart_{run_start_sha}.json"
        )
        if run_start.get("relative_path") != expected_path:
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "Run-start binding path differs from its content address.",
                409,
            )
    file_count = state["candidate"].get("file_count")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count <= 0:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate file_count must be a positive integer.",
            409,
        )
    expected_integrity = canonical_json_sha256(
        {key: copy.deepcopy(value) for key, value in state.items() if key != "integrity"}
    )
    if state["integrity"].get("payload_sha256") != expected_integrity:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Source lifecycle v2 payload hash differs.",
            409,
        )


def _read_managed_state(project_path: Path, *, doc_id: str) -> dict[str, Any] | None:
    path = _state_path(project_path)
    if not _path_exists(path):
        return None
    _require_plain_path(project_path / "working", owner="working directory")
    _require_plain_path(path, owner="source lifecycle state")
    state = _read_json_object(path, owner=STATE_FILENAME)
    _validate_state_shape(state, doc_id=doc_id)
    return state


def _read_v2_state(project_path: Path, *, doc_id: str) -> dict[str, Any] | None:
    path = _state_v2_path(project_path)
    if not _path_exists(path):
        return None
    _require_plain_path(project_path / "working", owner="working directory")
    _require_plain_path(path, owner="source lifecycle v2 state")
    state = _read_json_object(path, owner=STATE_V2_FILENAME)
    _validate_v2_state_shape(state, doc_id=doc_id)
    return state


def _read_authoritative_state(
    project_path: Path,
    *,
    doc_id: str,
) -> dict[str, Any] | None:
    # Presence makes v2 authoritative.  A malformed v2 must never fall back to v1.
    if _path_exists(_state_v2_path(project_path)):
        return _read_v2_state(project_path, doc_id=doc_id)
    return _read_managed_state(project_path, doc_id=doc_id)


def _candidate_root_from_state(project_path: Path, state: dict[str, Any]) -> Path:
    candidate_id = state["candidate"].get("candidate_id")
    tree_sha256 = state["candidate"].get("tree_sha256")
    if not isinstance(candidate_id, str) or candidate_id != f"srcpkg_{tree_sha256}":
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate ID is not content-addressed by its tree hash.",
            409,
        )
    expected_relative = f"working/{CANDIDATE_DIRECTORY}/{candidate_id}"
    if state["candidate"].get("relative_path") != expected_relative:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate path differs from its immutable managed location.",
            409,
        )
    candidate_parent = _candidate_parent(project_path)
    _require_plain_path(candidate_parent, owner="candidate parent")
    if not candidate_parent.is_dir():
        raise SourceLifecycleError(
            "source_package_candidate_missing",
            "Managed candidate parent is unavailable.",
            409,
        )
    return _require_confined_existing(
        project_path / Path(*PurePosixPath(expected_relative).parts),
        project_path,
        owner="source package candidate",
    )


def _child_binding(
    *,
    project_path: Path,
    candidate_root: Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    report = evidence["report"]
    return {
        "candidate": {
            "candidate_id": candidate_root.name,
            "relative_path": candidate_root.relative_to(project_path).as_posix(),
            "tree_sha256": evidence["tree"]["tree_sha256"],
            "file_count": evidence["tree"]["file_count"],
        },
        "package": copy.deepcopy(evidence["package_identities"]),
        "draft_structure": {
            "report": {
                "schema_version": report.get("schema_version"),
                "sha256": canonical_json_sha256(report),
            },
            "global_policy": copy.deepcopy(evidence["policies"]["global_structure"]),
        },
        "policies": copy.deepcopy(evidence["policies"]),
    }


def _decision_path(project_path: Path, decision_sha256: str) -> Path:
    return _decision_parent(project_path) / f"srcdec_{decision_sha256}.json"


def _hierarchy_plan_path(project_path: Path, plan_sha256: str) -> Path:
    return _hierarchy_parent(project_path) / f"hplan_{plan_sha256}.json"


def _hierarchy_overlay_path(project_path: Path, overlay_sha256: str) -> Path:
    return _hierarchy_parent(project_path) / f"hoverlay_{overlay_sha256}.json"


def _finalization_path(project_path: Path, finalization_sha256: str) -> Path:
    return _finalization_parent(project_path) / f"srcfin_{finalization_sha256}.json"


def _bound_identity(schema_version: str, sha256: str) -> dict[str, Any]:
    _require_sha256(sha256, owner=f"{schema_version}.sha256")
    return {"schema_version": schema_version, "sha256": sha256}


def _validate_decision_shape(
    event: dict[str, Any],
    *,
    doc_id: str,
    expected_sha256: str,
) -> None:
    _require_exact_fields(event, _DECISION_FIELDS, owner="source package decision")
    if event.get("schema_version") != SOURCE_PACKAGE_DECISION_VERSION:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision schema version differs.",
            409,
        )
    if event.get("doc_id") != doc_id:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision belongs to a different project.",
            409,
        )
    if event.get("operation") != "boundary_correction":
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision operation is unsupported.",
            409,
        )
    for name, fields in {
        "source": _SOURCE_FIELDS,
        "bootstrap": _BOOTSTRAP_FIELDS,
        "parent": _DECISION_PARENT_FIELDS,
        "child": _DECISION_CHILD_FIELDS,
        "hierarchy": _NULL_BINDING_FIELDS,
        "finalization": _NULL_BINDING_FIELDS,
        "integrity": _INTEGRITY_FIELDS,
    }.items():
        value = event.get(name)
        if not isinstance(value, dict):
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                f"Decision {name} must be an object.",
                409,
            )
        _require_exact_fields(value, fields, owner=f"decision {name}")
    request_payload = _validate_correction_request(event.get("request"))
    if request_payload != event.get("request"):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision request is not canonical.",
            409,
        )
    if not isinstance(event.get("plan"), dict) or not isinstance(
        event.get("correction_receipt"), dict
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision plan and receipt must be objects.",
            409,
        )
    if event["hierarchy"] != _null_binding("draft_structure_hierarchy_overlay_v1"):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision hierarchy binding must remain typed null.",
            409,
        )
    if event["finalization"] != _null_binding("source_package_finalization_v1"):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision finalization binding must remain typed null.",
            409,
        )
    if event["bootstrap"].get("schema_version") != SOURCE_LIFECYCLE_VERSION:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision bootstrap schema differs.",
            409,
        )
    for owner, value in {
        "decision.bootstrap.file_sha256": event["bootstrap"].get("file_sha256"),
        "decision.bootstrap.payload_sha256": event["bootstrap"].get("payload_sha256"),
        "decision.parent.state_sha256": event["parent"].get("state_sha256"),
        "decision.parent.candidate_tree_sha256": event["parent"].get(
            "candidate_tree_sha256"
        ),
        "decision.integrity.payload_sha256": event["integrity"].get("payload_sha256"),
    }.items():
        _require_sha256(value, owner=owner)
    parent_decision = event["parent"].get("decision_sha256")
    if parent_decision is not None:
        _require_sha256(parent_decision, owner="decision.parent.decision_sha256")
    expected_payload_sha = canonical_json_sha256(
        {key: copy.deepcopy(value) for key, value in event.items() if key != "integrity"}
    )
    if event["integrity"].get("payload_sha256") != expected_payload_sha:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision payload hash differs.",
            409,
        )
    if expected_payload_sha != expected_sha256:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision content address differs.",
            409,
        )


def _validate_revision_event_shape(
    event: dict[str, Any],
    *,
    doc_id: str,
    expected_sha256: str,
) -> None:
    _require_exact_fields(event, _REVISION_EVENT_FIELDS, owner="source package revision")
    if event.get("schema_version") != SOURCE_PACKAGE_REVISION_VERSION:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision schema version differs.",
            409,
        )
    if event.get("doc_id") != doc_id:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision belongs to a different project.",
            409,
        )
    operation = event.get("operation")
    if operation not in {"hierarchy_update", "finalize_pre_run"}:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision operation is unsupported.",
            409,
        )
    for name, fields in {
        "source": _SOURCE_FIELDS,
        "bootstrap": _BOOTSTRAP_FIELDS,
        "parent": _DECISION_PARENT_FIELDS,
        "child": _DECISION_CHILD_FIELDS,
        "hierarchy": _NULL_BINDING_FIELDS,
        "finalization": _NULL_BINDING_FIELDS,
        "integrity": _INTEGRITY_FIELDS,
    }.items():
        value = event.get(name)
        if not isinstance(value, dict):
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                f"Revision {name} must be an object.",
                409,
            )
        _require_exact_fields(value, fields, owner=f"revision {name}")
    if event["hierarchy"].get("schema_version") != HIERARCHY_OVERLAY_VERSION:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision hierarchy schema differs.",
            409,
        )
    hierarchy_sha = event["hierarchy"].get("sha256")
    if hierarchy_sha is not None:
        _require_sha256(hierarchy_sha, owner="revision hierarchy.sha256")
    if (
        event["finalization"].get("schema_version")
        != SOURCE_PACKAGE_FINALIZATION_VERSION
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision finalization schema differs.",
            409,
        )
    finalization_sha = event["finalization"].get("sha256")
    if finalization_sha is not None:
        _require_sha256(finalization_sha, owner="revision finalization.sha256")
    if operation == "hierarchy_update":
        request = _validate_hierarchy_request(event.get("request"))
        if request != event.get("request") or hierarchy_sha is None:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Hierarchy revision request or binding differs.",
                409,
            )
        if finalization_sha is not None:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Hierarchy revision must invalidate finalization.",
                409,
            )
    else:
        request = _validate_finalization_request(event.get("request"))
        if request != event.get("request") or finalization_sha is None:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Finalization revision request or binding differs.",
                409,
            )
    if event["bootstrap"].get("schema_version") != SOURCE_LIFECYCLE_VERSION:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision bootstrap schema differs.",
            409,
        )
    for owner, value in {
        "revision.bootstrap.file_sha256": event["bootstrap"].get("file_sha256"),
        "revision.bootstrap.payload_sha256": event["bootstrap"].get(
            "payload_sha256"
        ),
        "revision.parent.state_sha256": event["parent"].get("state_sha256"),
        "revision.parent.candidate_tree_sha256": event["parent"].get(
            "candidate_tree_sha256"
        ),
    }.items():
        _require_sha256(value, owner=owner)
    parent_decision = event["parent"].get("decision_sha256")
    if parent_decision is not None:
        _require_sha256(parent_decision, owner="revision.parent.decision_sha256")
    expected_payload_sha = canonical_json_sha256(
        {key: copy.deepcopy(value) for key, value in event.items() if key != "integrity"}
    )
    if event["integrity"].get("payload_sha256") != expected_payload_sha:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision payload hash differs.",
            409,
        )
    if expected_payload_sha != expected_sha256:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision content address differs.",
            409,
        )


def _read_decision(
    project_path: Path,
    *,
    doc_id: str,
    decision_sha256: str,
) -> dict[str, Any]:
    parent = _decision_parent(project_path)
    _require_plain_path(parent, owner="decision parent")
    if not parent.is_dir():
        raise SourceLifecycleError(
            "source_package_decision_missing",
            "Decision parent is unavailable.",
            409,
        )
    raw_path = _decision_path(project_path, decision_sha256)
    if not _path_exists(raw_path):
        raise SourceLifecycleError(
            "source_package_decision_missing",
            "Source package decision is unavailable.",
            409,
        )
    path = _require_confined_existing(
        raw_path,
        project_path,
        owner="source package decision",
    )
    event = _read_json_object(path, owner="source package decision")
    if event.get("schema_version") == SOURCE_PACKAGE_DECISION_VERSION:
        _validate_decision_shape(
            event, doc_id=doc_id, expected_sha256=decision_sha256
        )
    elif event.get("schema_version") == SOURCE_PACKAGE_REVISION_VERSION:
        _validate_revision_event_shape(
            event, doc_id=doc_id, expected_sha256=decision_sha256
        )
    else:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision schema version is unsupported.",
            409,
        )
    return event


def _validated_candidate_for_state(
    project_path: Path,
    *,
    doc_id: str,
    state: dict[str, Any],
    source_path: Path,
    source_format: str,
    source_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    candidate_root = _candidate_root_from_state(project_path, state)
    evidence = _validate_candidate(
        candidate_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
        validation_scope=_candidate_validation_state_scope(state),
    )
    expected = _child_binding(
        project_path=project_path,
        candidate_root=candidate_root,
        evidence=evidence,
    )
    for name in ("candidate", "package", "draft_structure", "policies"):
        if state.get(name) != expected[name]:
            raise SourceLifecycleError(
                "source_lifecycle_stale",
                f"Source lifecycle {name} differs from the validated candidate.",
                409,
            )
    return candidate_root, evidence


def _read_hierarchy_artifact(
    project_path: Path,
    *,
    prefix: str,
    payload_sha256: str,
) -> dict[str, Any]:
    parent = _hierarchy_parent(project_path)
    _require_plain_path(parent, owner="hierarchy artifact parent")
    if not parent.is_dir():
        raise SourceLifecycleError(
            "source_package_hierarchy_missing",
            "Hierarchy artifact parent is unavailable.",
            409,
        )
    raw_path = (
        _hierarchy_plan_path(project_path, payload_sha256)
        if prefix == "hplan"
        else _hierarchy_overlay_path(project_path, payload_sha256)
    )
    if not _path_exists(raw_path):
        raise SourceLifecycleError(
            "source_package_hierarchy_missing",
            "Hierarchy artifact is unavailable.",
            409,
        )
    path = _require_confined_existing(
        raw_path, project_path, owner="hierarchy artifact"
    )
    payload = _read_json_object(path, owner="hierarchy artifact")
    integrity = payload.get("integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("payload_sha256") != payload_sha256
    ):
        raise SourceLifecycleError(
            "source_package_hierarchy_invalid",
            "Hierarchy artifact identity differs from its content address.",
            409,
        )
    return payload


def _hierarchy_plan_for_revision(
    project_path: Path,
    *,
    report: dict[str, Any],
    actions: list[dict[str, Any]],
    user: str,
    current_binding: dict[str, Any],
) -> dict[str, Any]:
    units = report.get("units")
    if not isinstance(units, list) or any(not isinstance(row, dict) for row in units):
        raise DraftStructureError("draft report units must be a list of objects")
    unit_ids = [row.get("unit_id") for row in units]
    if any(not isinstance(unit_id, str) or not unit_id for unit_id in unit_ids):
        raise DraftStructureError("draft report unit IDs must be nonempty strings")
    canonical = {
        row["unit_id"]: row.get("parent_unit_id")
        for row in units
    }
    current = copy.deepcopy(canonical)
    overlay_sha = current_binding.get("sha256")
    if overlay_sha is not None:
        overlay = _read_hierarchy_artifact(
            project_path, prefix="hoverlay", payload_sha256=overlay_sha
        )
        expected_overlay_sha = canonical_json_sha256(
            {
                key: copy.deepcopy(value)
                for key, value in overlay.items()
                if key != "integrity"
            }
        )
        rows = overlay.get("rows")
        if (
            overlay.get("schema_version") != HIERARCHY_OVERLAY_VERSION
            or overlay.get("doc_id") != report.get("doc_id")
            or expected_overlay_sha != overlay_sha
            or not isinstance(rows, list)
            or len(rows) != len(unit_ids)
        ):
            raise DraftStructureError("current hierarchy overlay is invalid")
        for index, (unit_id, row) in enumerate(zip(unit_ids, rows, strict=True)):
            if (
                not isinstance(row, dict)
                or set(row) != {"child_unit_id", "order_index", "parent_unit_id"}
                or row.get("child_unit_id") != unit_id
                or row.get("order_index") != index
                or (
                    row.get("parent_unit_id") is not None
                    and row.get("parent_unit_id") not in canonical
                )
            ):
                raise DraftStructureError("current hierarchy overlay rows are invalid")
            current[unit_id] = row.get("parent_unit_id")

    proposed = copy.deepcopy(current)
    claimed: set[str] = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise DraftStructureError(
                f"hierarchy action_specs[{index}] must be an object"
            )
        action_type = action.get("action_type")
        expected_fields = (
            {"action_type", "child_unit_id", "parent_unit_id"}
            if action_type == "set_parent"
            else {"action_type", "child_unit_id"}
        )
        if action_type not in {"set_parent", "clear_parent"} or set(action) != expected_fields:
            raise DraftStructureError(
                "hierarchy action type or fields differ from the closed contract"
            )
        child_id = action.get("child_unit_id")
        parent_id = action.get("parent_unit_id") if action_type == "set_parent" else None
        if (
            not isinstance(child_id, str)
            or not child_id
            or child_id not in canonical
            or child_id in claimed
        ):
            raise DraftStructureError("hierarchy child unit is unknown or claimed twice")
        if parent_id is not None and (
            not isinstance(parent_id, str) or not parent_id or parent_id not in canonical
        ):
            raise DraftStructureError("hierarchy parent unit is unknown")
        claimed.add(child_id)
        proposed[child_id] = parent_id
    if proposed == current:
        raise DraftStructureError("hierarchy actions do not change the current overlay")

    desired_specs: list[dict[str, Any]] = []
    for unit_id in unit_ids:
        parent_id = proposed[unit_id]
        if parent_id == canonical[unit_id]:
            continue
        if parent_id is None:
            desired_specs.append(
                {"action_type": "clear_parent", "child_unit_id": unit_id}
            )
        else:
            desired_specs.append(
                {
                    "action_type": "set_parent",
                    "child_unit_id": unit_id,
                    "parent_unit_id": parent_id,
                }
            )
    plan = build_hierarchy_plan(
        report,
        desired_specs,
        proposer={"kind": "human", "identifier": user},
    )
    if any(row.get("status") != "candidate" for row in plan["actions"]):
        raise DraftStructureError(
            "hierarchy revision does not produce a valid complete overlay"
        )
    return plan


def _finalization_record_payload(
    *,
    doc_id: str,
    source: dict[str, Any],
    bootstrap: dict[str, Any],
    parent_state: dict[str, Any],
    child: dict[str, Any],
    hierarchy: dict[str, Any],
    user: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PACKAGE_FINALIZATION_VERSION,
        "doc_id": doc_id,
        "lifecycle": "finalized_pre_run",
        "pipeline_run_count": 0,
        "source": copy.deepcopy(source),
        "bootstrap": copy.deepcopy(bootstrap),
        "parent": {
            "state_schema_version": parent_state["schema_version"],
            "state_sha256": parent_state["integrity"]["payload_sha256"],
            "candidate_tree_sha256": parent_state["candidate"]["tree_sha256"],
            "decision_sha256": (
                parent_state.get("latest_decision", {}).get("sha256")
            ),
        },
        "candidate": copy.deepcopy(child["candidate"]),
        "package": copy.deepcopy(child["package"]),
        "draft_structure": copy.deepcopy(child["draft_structure"]),
        "policies": copy.deepcopy(child["policies"]),
        "hierarchy": copy.deepcopy(hierarchy),
        "approved_by": {"kind": "human", "identifier": user},
    }
    payload["integrity"] = {"payload_sha256": canonical_json_sha256(payload)}
    return payload


def _read_finalization_record(
    project_path: Path,
    *,
    doc_id: str,
    finalization_sha256: str,
) -> dict[str, Any]:
    parent = _finalization_parent(project_path)
    _require_plain_path(parent, owner="finalization parent")
    if not parent.is_dir():
        raise SourceLifecycleError(
            "source_package_finalization_missing",
            "Finalization parent is unavailable.",
            409,
        )
    raw_path = _finalization_path(project_path, finalization_sha256)
    if not _path_exists(raw_path):
        raise SourceLifecycleError(
            "source_package_finalization_missing",
            "Finalization record is unavailable.",
            409,
        )
    path = _require_confined_existing(
        raw_path, project_path, owner="source package finalization"
    )
    payload = _read_json_object(path, owner="source package finalization")
    _require_exact_fields(
        payload, _FINALIZATION_RECORD_FIELDS, owner="source package finalization"
    )
    if (
        payload.get("schema_version") != SOURCE_PACKAGE_FINALIZATION_VERSION
        or payload.get("doc_id") != doc_id
        or payload.get("lifecycle") != "finalized_pre_run"
        or payload.get("pipeline_run_count") != 0
    ):
        raise SourceLifecycleError(
            "source_package_finalization_invalid",
            "Finalization record identity or lifecycle differs.",
            409,
        )
    approved_by = payload.get("approved_by")
    if not isinstance(approved_by, dict):
        raise SourceLifecycleError(
            "source_package_finalization_invalid",
            "Finalization authority must be an object.",
            409,
        )
    _require_exact_fields(
        approved_by, _APPROVED_BY_FIELDS, owner="finalization authority"
    )
    expected_sha = canonical_json_sha256(
        {key: copy.deepcopy(value) for key, value in payload.items() if key != "integrity"}
    )
    if (
        not isinstance(payload.get("integrity"), dict)
        or payload["integrity"].get("payload_sha256") != expected_sha
        or expected_sha != finalization_sha256
    ):
        raise SourceLifecycleError(
            "source_package_finalization_invalid",
            "Finalization record hash differs.",
            409,
        )
    return payload


def _require_safe_runtime_identifier(value: Any, *, owner: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(not (char.isalnum() or char in "._-") for char in value)
    ):
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            f"{owner} must contain only letters, numbers, dot, dash, or underscore.",
            409,
        )
    return value


def _runtime_manifest_evidence(
    *,
    doc_id: str,
    job_id: str,
    manifest_path: str | Path,
    managed_source: dict[str, Any],
) -> tuple[Path, dict[str, Any], str, str]:
    _require_exact_fields(
        managed_source,
        _MANAGED_RUNTIME_BINDING_FIELDS,
        owner="managed runtime binding",
    )
    jobs_root = Path(THESIS_JOBS_ROOT).resolve()
    _require_plain_path(jobs_root, owner="runtime jobs root")
    if not jobs_root.is_dir():
        raise SourceLifecycleError(
            "source_package_runtime_missing",
            "Runtime jobs root is unavailable.",
            409,
        )
    raw_manifest_path = Path(manifest_path)
    if not _path_exists(raw_manifest_path):
        raise SourceLifecycleError(
            "source_package_runtime_missing",
            "Managed runtime manifest is unavailable.",
            409,
        )
    path = _require_confined_existing(
        raw_manifest_path, jobs_root, owner="managed runtime manifest"
    )
    if not path.is_file():
        raise SourceLifecycleError(
            "source_package_runtime_missing",
            "Managed runtime manifest is unavailable.",
            409,
        )
    expected_path = jobs_root / job_id / "source_manifest.json"
    if path != expected_path.resolve():
        raise SourceLifecycleError(
            "source_package_runtime_invalid",
            "Managed runtime manifest path differs from its job identity.",
            409,
        )
    manifest = _read_json_object(path, owner="managed runtime manifest")
    if (
        manifest.get("contract_version") != MANAGED_RUNTIME_MANIFEST_VERSION
        or manifest.get("project_id") != doc_id
        or manifest.get("job_id") != job_id
        or manifest.get("managed_source") != managed_source
    ):
        raise SourceLifecycleError(
            "source_package_runtime_invalid",
            "Managed runtime manifest is not bound to the finalized package.",
            409,
        )
    relative_path = path.relative_to(jobs_root).as_posix()
    return path, manifest, relative_path, _file_sha256(path)


def _run_start_event_payload(
    *,
    doc_id: str,
    run_id: str,
    job_id: str,
    parent_state: dict[str, Any],
    manifest_relative_path: str,
    manifest_sha256: str,
    managed_source: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PACKAGE_RUN_START_VERSION,
        "doc_id": doc_id,
        "run_id": run_id,
        "job_id": job_id,
        "parent": {
            "state_sha256": parent_state["integrity"]["payload_sha256"],
            "candidate_tree_sha256": parent_state["candidate"]["tree_sha256"],
            "latest_decision_sha256": parent_state["latest_decision"]["sha256"],
            "hierarchy_sha256": parent_state["hierarchy"]["sha256"],
            "finalization_sha256": parent_state["finalization"]["sha256"],
        },
        "runtime": {
            "manifest_schema_version": MANAGED_RUNTIME_MANIFEST_VERSION,
            "manifest_sha256": manifest_sha256,
            "manifest_relative_path": manifest_relative_path,
            "managed_source": copy.deepcopy(managed_source),
        },
    }
    payload["integrity"] = {"payload_sha256": canonical_json_sha256(payload)}
    return payload


def _read_run_start_event(
    project_path: Path,
    *,
    doc_id: str,
    run_start_sha256: str,
    parent_state: dict[str, Any],
) -> dict[str, Any]:
    parent = _run_start_parent(project_path)
    _require_plain_path(parent, owner="run-start parent")
    if not parent.is_dir():
        raise SourceLifecycleError(
            "source_package_run_start_missing",
            "Run-start evidence parent is unavailable.",
            409,
        )
    raw_path = _run_start_path(project_path, run_start_sha256)
    if not _path_exists(raw_path):
        raise SourceLifecycleError(
            "source_package_run_start_missing",
            "Run-start evidence is unavailable.",
            409,
        )
    path = _require_confined_existing(
        raw_path, project_path, owner="run-start evidence"
    )
    event = _read_json_object(path, owner="run-start evidence")
    _require_exact_fields(event, _RUN_START_EVENT_FIELDS, owner="run-start evidence")
    if (
        event.get("schema_version") != SOURCE_PACKAGE_RUN_START_VERSION
        or event.get("doc_id") != doc_id
    ):
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start evidence identity differs.",
            409,
        )
    run_id = _require_safe_runtime_identifier(event.get("run_id"), owner="run_id")
    job_id = _require_safe_runtime_identifier(event.get("job_id"), owner="job_id")
    parent_identity = event.get("parent")
    runtime = event.get("runtime")
    integrity = event.get("integrity")
    if not isinstance(parent_identity, dict) or not isinstance(runtime, dict):
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start parent and runtime bindings must be objects.",
            409,
        )
    _require_exact_fields(parent_identity, _RUN_START_PARENT_FIELDS, owner="run-start parent")
    _require_exact_fields(runtime, _RUN_START_RUNTIME_FIELDS, owner="run-start runtime")
    expected_parent = {
        "state_sha256": parent_state["integrity"]["payload_sha256"],
        "candidate_tree_sha256": parent_state["candidate"]["tree_sha256"],
        "latest_decision_sha256": parent_state["latest_decision"]["sha256"],
        "hierarchy_sha256": parent_state["hierarchy"]["sha256"],
        "finalization_sha256": parent_state["finalization"]["sha256"],
    }
    if parent_identity != expected_parent:
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start parent differs from the finalized package revision.",
            409,
        )
    expected_binding = _managed_runtime_binding(parent_state)
    if (
        runtime.get("manifest_schema_version") != MANAGED_RUNTIME_MANIFEST_VERSION
        or runtime.get("managed_source") != expected_binding
    ):
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start runtime binding differs from the finalized package.",
            409,
        )
    manifest_sha = _require_sha256(
        runtime.get("manifest_sha256"), owner="run-start manifest sha256"
    )
    manifest_relative_path = runtime.get("manifest_relative_path")
    if manifest_relative_path != f"{job_id}/source_manifest.json":
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start runtime path differs from its job identity.",
            409,
        )
    manifest_path = Path(THESIS_JOBS_ROOT) / Path(
        *PurePosixPath(manifest_relative_path).parts
    )
    _path, _manifest, actual_relative, actual_sha = _runtime_manifest_evidence(
        doc_id=doc_id,
        job_id=job_id,
        manifest_path=manifest_path,
        managed_source=expected_binding,
    )
    if actual_relative != manifest_relative_path or actual_sha != manifest_sha:
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start runtime manifest bytes differ.",
            409,
        )
    expected_sha = canonical_json_sha256(
        {key: copy.deepcopy(value) for key, value in event.items() if key != "integrity"}
    )
    if (
        not isinstance(integrity, dict)
        or set(integrity) != _INTEGRITY_FIELDS
        or integrity.get("payload_sha256") != expected_sha
        or expected_sha != run_start_sha256
        or path.read_bytes()
        != (
            json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    ):
        raise SourceLifecycleError(
            "source_package_run_start_invalid",
            "Run-start evidence hash differs from its content address.",
            409,
        )
    # Keep the identifiers live in validation so a malformed type cannot pass.
    assert run_id and job_id
    return event


def _validate_revision_transition(
    project_path: Path,
    *,
    doc_id: str,
    event: dict[str, Any],
    decision_sha256: str,
    parent_state: dict[str, Any],
    source_path: Path,
    source_format: str,
    source_sha256: str,
    bootstrap: dict[str, Any],
    expected_source: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_payload = event["request"]
    if (
        request_payload["expected_state_sha256"]
        != parent_state["integrity"]["payload_sha256"]
        or request_payload["expected_candidate_tree_sha256"]
        != parent_state["candidate"]["tree_sha256"]
        or request_payload["expected_report_sha256"]
        != parent_state["draft_structure"]["report"]["sha256"]
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision request is not bound to its parent state.",
            409,
        )
    parent_root, parent_evidence = _validated_candidate_for_state(
        project_path,
        doc_id=doc_id,
        state=parent_state,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
    )
    expected_child = _child_binding(
        project_path=project_path,
        candidate_root=parent_root,
        evidence=parent_evidence,
    )
    if event["child"] != expected_child:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Revision changed or rebound immutable package bytes.",
            409,
        )
    parent_hierarchy = copy.deepcopy(
        parent_state.get("hierarchy")
        or _null_binding(HIERARCHY_OVERLAY_VERSION)
    )
    if event["operation"] == "hierarchy_update":
        try:
            expected_plan = _hierarchy_plan_for_revision(
                project_path,
                report=parent_evidence["report"],
                actions=request_payload["actions"],
                user=request_payload["user"],
                current_binding=parent_hierarchy,
            )
            expected_overlay = apply_hierarchy_plan(
                parent_evidence["document"],
                parent_evidence["structure"],
                parent_evidence["asset_manifest"],
                parent_evidence["admitted_projection"],
                _project_state(doc_id),
                parent_evidence["report"],
                expected_plan,
                package_root=parent_root,
            )
        except DraftStructureError as exc:
            raise SourceLifecycleError(
                "source_package_hierarchy_invalid", str(exc), 409
            ) from exc
        plan_sha = expected_plan["integrity"]["payload_sha256"]
        overlay_sha = expected_overlay["integrity"]["payload_sha256"]
        if event["hierarchy"] != _bound_identity(
            HIERARCHY_OVERLAY_VERSION, overlay_sha
        ):
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Revision hierarchy identity differs from deterministic apply.",
                409,
            )
        if event["hierarchy"] == parent_hierarchy:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Hierarchy revision does not change the current overlay.",
                409,
            )
        if _read_hierarchy_artifact(
            project_path, prefix="hplan", payload_sha256=plan_sha
        ) != expected_plan or _read_hierarchy_artifact(
            project_path, prefix="hoverlay", payload_sha256=overlay_sha
        ) != expected_overlay:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Published hierarchy artifacts differ from deterministic apply.",
                409,
            )
        expected_state = _v2_state_payload(
            doc_id=doc_id,
            source=expected_source,
            bootstrap=bootstrap,
            decision_sha256=decision_sha256,
            child=expected_child,
            decision_schema_version=SOURCE_PACKAGE_REVISION_VERSION,
            hierarchy=event["hierarchy"],
            finalization=_null_binding(SOURCE_PACKAGE_FINALIZATION_VERSION),
            lifecycle="draft",
            authority="os_locked_pre_run",
        )
        return expected_state, event

    if parent_state.get("lifecycle") != "draft":
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Finalization must start from a draft revision.",
            409,
        )
    expected_hierarchy_sha = request_payload["expected_hierarchy_sha256"]
    if expected_hierarchy_sha != parent_hierarchy["sha256"]:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Finalization request hierarchy identity differs.",
            409,
        )
    if event["hierarchy"] != parent_hierarchy:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Finalization changed the hierarchy binding.",
            409,
        )
    expected_record = _finalization_record_payload(
        doc_id=doc_id,
        source=expected_source,
        bootstrap=bootstrap,
        parent_state=parent_state,
        child=expected_child,
        hierarchy=parent_hierarchy,
        user=request_payload["user"],
    )
    finalization_sha = expected_record["integrity"]["payload_sha256"]
    if event["finalization"] != _bound_identity(
        SOURCE_PACKAGE_FINALIZATION_VERSION, finalization_sha
    ) or _read_finalization_record(
        project_path,
        doc_id=doc_id,
        finalization_sha256=finalization_sha,
    ) != expected_record:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Finalization binding differs from the exact package record.",
            409,
        )
    expected_state = _v2_state_payload(
        doc_id=doc_id,
        source=expected_source,
        bootstrap=bootstrap,
        decision_sha256=decision_sha256,
        child=expected_child,
        decision_schema_version=SOURCE_PACKAGE_REVISION_VERSION,
        hierarchy=parent_hierarchy,
        finalization=event["finalization"],
        lifecycle="finalized_pre_run",
        authority="os_locked_pre_run",
    )
    return expected_state, event


def _validate_decision_lineage(
    project_path: Path,
    *,
    doc_id: str,
    decision_sha256: str,
    state_v1: dict[str, Any],
    source_path: Path,
    source_format: str,
    source_sha256: str,
    seen: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    lineage = set() if seen is None else seen
    if decision_sha256 in lineage or len(lineage) >= 64:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision lineage is cyclic or exceeds the bounded depth.",
            409,
        )
    lineage.add(decision_sha256)
    event = _read_decision(
        project_path,
        doc_id=doc_id,
        decision_sha256=decision_sha256,
    )
    expected_source = {
        "filename": source_path.name,
        "format": source_format,
        "sha256": source_sha256,
    }
    bootstrap = _bootstrap_identity(project_path, state_v1)
    if event["source"] != expected_source or event["bootstrap"] != bootstrap:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision source or bootstrap identity differs.",
            409,
        )

    parent = event["parent"]
    parent_version = parent.get("state_schema_version")
    if parent_version == SOURCE_LIFECYCLE_VERSION:
        if parent.get("decision_sha256") is not None:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Bootstrap decision must not reference an earlier decision.",
                409,
            )
        parent_state = state_v1
    elif parent_version == SOURCE_LIFECYCLE_V2_VERSION:
        previous_sha = parent.get("decision_sha256")
        if not isinstance(previous_sha, str):
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "A v2 parent must reference its prior decision.",
                409,
            )
        parent_state, _previous_event = _validate_decision_lineage(
            project_path,
            doc_id=doc_id,
            decision_sha256=previous_sha,
            state_v1=state_v1,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            seen=lineage,
        )
    else:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision parent lifecycle version is unsupported.",
            409,
        )
    if (
        parent["state_sha256"] != parent_state["integrity"]["payload_sha256"]
        or parent["candidate_tree_sha256"]
        != parent_state["candidate"]["tree_sha256"]
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision parent identity differs from its authoritative lineage.",
            409,
        )
    if event["schema_version"] == SOURCE_PACKAGE_REVISION_VERSION:
        result = _validate_revision_transition(
            project_path,
            doc_id=doc_id,
            event=event,
            decision_sha256=decision_sha256,
            parent_state=parent_state,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            bootstrap=bootstrap,
            expected_source=expected_source,
        )
        lineage.remove(decision_sha256)
        return result
    request_payload = event["request"]
    if (
        request_payload["expected_state_sha256"] != parent["state_sha256"]
        or request_payload["expected_candidate_tree_sha256"]
        != parent["candidate_tree_sha256"]
        or request_payload["expected_report_sha256"]
        != parent_state["draft_structure"]["report"]["sha256"]
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision request is not bound to its parent state.",
            409,
        )

    parent_root, parent_evidence = _validated_candidate_for_state(
        project_path,
        doc_id=doc_id,
        state=parent_state,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
    )
    try:
        expected_plan = build_correction_plan(
            parent_evidence["report"],
            request_payload["actions"],
            proposer={"kind": "human", "identifier": request_payload["user"]},
        )
        _require_effective_actions(expected_plan, parent_evidence["report"])
        if event["plan"] != expected_plan:
            raise SourceLifecycleError(
                "source_package_decision_invalid",
                "Decision plan differs from the server-built plan.",
                409,
            )
        correction = apply_correction_plan(
            parent_evidence["document"],
            parent_evidence["structure"],
            parent_evidence["asset_manifest"],
            parent_evidence["admitted_projection"],
            _project_state(doc_id),
            parent_evidence["report"],
            expected_plan,
            package_root=parent_root,
        )
    except DraftStructureError as exc:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            str(exc),
            409,
        ) from exc
    if event["correction_receipt"] != correction.correction_receipt:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision correction receipt differs from deterministic replay.",
            409,
        )
    child_state_view = {"candidate": event["child"].get("candidate")}
    child_root = _candidate_root_from_state(project_path, child_state_view)
    child_evidence = _validate_candidate(
        child_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
    )
    expected_child = _child_binding(
        project_path=project_path,
        candidate_root=child_root,
        evidence=child_evidence,
    )
    if event["child"] != expected_child:
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision child identities differ from the validated candidate.",
            409,
        )
    if _materialized_asset_identity(
        child_root,
        child_evidence["asset_manifest"],
    ) != _materialized_asset_identity(
        parent_root,
        parent_evidence["asset_manifest"],
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision child changed the immutable materialized asset bytes.",
            409,
        )
    if (
        child_evidence["document"] != correction.document
        or child_evidence["structure"] != correction.structure_manifest
        or child_evidence["normalization_receipt"]
        != correction.normalization_receipt
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision child differs from deterministic correction replay.",
            409,
        )
    _validate_deterministic_child_sidecars(
        child_root,
        child_evidence,
        document=correction.document,
        structure_manifest=correction.structure_manifest,
    )
    if (
        expected_child["package"]["document"]["sha256"]
        == parent_state["package"]["document"]["sha256"]
        and expected_child["package"]["structure"]["sha256"]
        == parent_state["package"]["structure"]["sha256"]
    ):
        raise SourceLifecycleError(
            "source_package_decision_invalid",
            "Decision does not change document or structure identity.",
            409,
        )
    expected_state = _v2_state_payload(
        doc_id=doc_id,
        source=expected_source,
        bootstrap=bootstrap,
        decision_sha256=decision_sha256,
        child=expected_child,
    )
    lineage.remove(decision_sha256)
    return expected_state, event


def _managed_status_v2(
    project_path: Path,
    *,
    doc_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    source_path, source_format, source_sha256 = _server_source(project_path)
    expected_source = {
        "filename": source_path.name,
        "format": source_format,
        "sha256": source_sha256,
    }
    if state["source"] != expected_source:
        raise SourceLifecycleError(
            "source_package_source_changed",
            "Uploaded source differs from the managed lifecycle state.",
            409,
        )
    state_v1 = _read_managed_state(project_path, doc_id=doc_id)
    if state_v1 is None:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Lifecycle v2 requires its immutable v1 bootstrap.",
            409,
        )
    if state["bootstrap"] != _bootstrap_identity(project_path, state_v1):
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Lifecycle v2 bootstrap identity differs from source_lifecycle_v1.json.",
            409,
        )
    pre_run_state, event = _validate_decision_lineage(
        project_path,
        doc_id=doc_id,
        decision_sha256=state["latest_decision"]["sha256"],
        state_v1=state_v1,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
    )
    run_start_event: dict[str, Any] | None = None
    if state["lifecycle"] == "run_started_frozen":
        if pre_run_state.get("lifecycle") != "finalized_pre_run":
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "A frozen run must descend from finalized_pre_run state.",
                409,
            )
        run_start_sha = state["run_start"]["sha256"]
        run_start_event = _read_run_start_event(
            project_path,
            doc_id=doc_id,
            run_start_sha256=run_start_sha,
            parent_state=pre_run_state,
        )
        expected_state = _frozen_state_payload(
            pre_run_state, run_start_sha256=run_start_sha
        )
        allowed_managed_runtime = copy.deepcopy(
            run_start_event["runtime"]["managed_source"]
        )
    else:
        expected_state = pre_run_state
        allowed_managed_runtime = (
            _managed_runtime_binding(pre_run_state)
            if pre_run_state.get("lifecycle") == "finalized_pre_run"
            else None
        )
    if state != expected_state:
        raise SourceLifecycleError(
            "source_lifecycle_stale",
            "Source lifecycle v2 differs from its authoritative decision lineage.",
            409,
        )
    occupancy = _legacy_occupancy(
        project_path,
        doc_id=doc_id,
        allowed_managed_runtime=allowed_managed_runtime,
    )
    if occupancy:
        raise SourceLifecycleError(
            "managed_source_legacy_conflict",
            "Legacy project or runtime evidence appeared after managed publication: "
            + ", ".join(occupancy),
            409,
        )
    lifecycle = state["lifecycle"]
    result = {
        "schema_version": SOURCE_PACKAGE_STATUS_VERSION,
        "doc_id": doc_id,
        "mode": (
            "managed_run_started_frozen"
            if lifecycle == "run_started_frozen"
            else (
                "managed_finalized_pre_run"
                if lifecycle == "finalized_pre_run"
                else "managed_draft"
            )
        ),
        "managed": True,
        "normalize_allowed": lifecycle == "draft",
        "corrections_allowed": lifecycle != "run_started_frozen",
        "hierarchy_allowed": lifecycle != "run_started_frozen",
        "finalization_allowed": lifecycle == "draft",
        "lifecycle": lifecycle,
        "pipeline_run_count": state["pipeline_run_count"],
        "source": copy.deepcopy(state["source"]),
        "candidate": copy.deepcopy(state["candidate"]),
        "package": copy.deepcopy(state["package"]),
        "draft_structure": copy.deepcopy(state["draft_structure"]),
        "policies": copy.deepcopy(state["policies"]),
        "state_sha256": state["integrity"]["payload_sha256"],
        "revision": {
            "schema_version": SOURCE_LIFECYCLE_V2_VERSION,
            "latest_decision_sha256": state["latest_decision"]["sha256"],
            "hierarchy": copy.deepcopy(state["hierarchy"]),
            "finalization": copy.deepcopy(state["finalization"]),
            "authority": state["experimental"]["authority"],
            "load_bearing": state["experimental"]["load_bearing"],
        },
        "latest_decision": {
            "operation": event["operation"],
            "authority": {
                "kind": "human",
                "identifier": event["request"]["user"],
            },
        },
    }
    if run_start_event is not None:
        result["run_start"] = {
            "schema_version": SOURCE_PACKAGE_RUN_START_VERSION,
            "sha256": state["run_start"]["sha256"],
            "run_id": run_start_event["run_id"],
            "job_id": run_start_event["job_id"],
            "runtime_manifest_sha256": run_start_event["runtime"][
                "manifest_sha256"
            ],
        }
    return result


def _managed_status(
    project_path: Path,
    *,
    doc_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    if state.get("schema_version") == SOURCE_LIFECYCLE_V2_VERSION:
        return _managed_status_v2(project_path, doc_id=doc_id, state=state)
    evidence = _legacy_occupancy(project_path, doc_id=doc_id)
    if evidence:
        raise SourceLifecycleError(
            "managed_source_legacy_conflict",
            "Legacy project or runtime evidence appeared after managed publication: "
            + ", ".join(evidence),
            409,
        )
    source_path, source_format, source_sha256 = _server_source(project_path)
    expected_source = {
        "filename": source_path.name,
        "format": source_format,
        "sha256": source_sha256,
    }
    if state["source"] != expected_source:
        raise SourceLifecycleError(
            "source_package_source_changed",
            "Uploaded source differs from the managed lifecycle state.",
            409,
        )
    candidate_id = state["candidate"].get("candidate_id")
    if not isinstance(candidate_id, str) or candidate_id != f"srcpkg_{state['candidate']['tree_sha256']}":
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate ID is not content-addressed by its tree hash.",
            409,
        )
    expected_relative = f"working/{CANDIDATE_DIRECTORY}/{candidate_id}"
    if state["candidate"].get("relative_path") != expected_relative:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate path differs from its immutable managed location.",
            409,
        )
    candidate_parent = _candidate_parent(project_path)
    _require_plain_path(candidate_parent, owner="candidate parent")
    if not candidate_parent.is_dir():
        raise SourceLifecycleError(
            "source_package_candidate_missing",
            "Managed candidate parent is unavailable.",
            409,
        )
    candidate_root = _require_confined_existing(
        project_path / Path(*PurePosixPath(expected_relative).parts),
        project_path,
        owner="source package candidate",
    )
    evidence = _validate_candidate(
        candidate_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
        validation_scope=_candidate_validation_state_scope(state),
    )
    expected_state = _state_payload(
        project_path=project_path,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
        candidate_root=candidate_root,
        evidence=evidence,
    )
    if state != expected_state:
        raise SourceLifecycleError(
            "source_lifecycle_stale",
            "Source lifecycle identities differ from the validated candidate.",
            409,
        )
    return {
        "schema_version": SOURCE_PACKAGE_STATUS_VERSION,
        "doc_id": doc_id,
        "mode": "managed_draft",
        "managed": True,
        "normalize_allowed": True,
        "corrections_allowed": True,
        "hierarchy_allowed": True,
        "finalization_allowed": True,
        "lifecycle": "draft",
        "pipeline_run_count": 0,
        "source": copy.deepcopy(state["source"]),
        "candidate": copy.deepcopy(state["candidate"]),
        "package": copy.deepcopy(state["package"]),
        "draft_structure": copy.deepcopy(state["draft_structure"]),
        "policies": copy.deepcopy(state["policies"]),
        "state_sha256": state["integrity"]["payload_sha256"],
    }


def get_source_package_status(project_path: str | Path, doc_id: str) -> dict[str, Any]:
    root = _require_project_root(project_path)
    with _candidate_validation_request_scope():
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is not None:
            return _managed_status(root, doc_id=doc_id, state=state)
        evidence = _legacy_occupancy(root, doc_id=doc_id)
        if evidence:
            return {
                "schema_version": SOURCE_PACKAGE_STATUS_VERSION,
                "doc_id": doc_id,
                "mode": "legacy_only",
                "managed": False,
                "normalize_allowed": False,
                "reason": "legacy_project_evidence_exists",
                "evidence": evidence,
            }
        try:
            source_path, source_format, source_sha256 = _server_source(root)
            source = {
                "filename": source_path.name,
                "format": source_format,
                "sha256": source_sha256,
            }
            allowed = True
            reason = None
        except SourceLifecycleError as exc:
            if exc.code != "source_missing":
                raise
            source = None
            allowed = False
            reason = "source_missing"
        return {
            "schema_version": SOURCE_PACKAGE_STATUS_VERSION,
            "doc_id": doc_id,
            "mode": "unmanaged_draft",
            "managed": False,
            "normalize_allowed": allowed,
            "source": source,
            "reason": reason,
        }


def ensure_source_upload_allowed(project_path: str | Path) -> None:
    root = _require_project_root(project_path)
    if _managed_state_exists(root):
        raise SourceLifecycleError(
            "managed_source_overwrite_forbidden",
            "Managed draft source is immutable; create a new project revision instead.",
            409,
        )


def ensure_legacy_extract_allowed(project_path: str | Path) -> None:
    root = _require_project_root(project_path)
    if _managed_state_exists(root):
        raise SourceLifecycleError(
            "managed_source_legacy_extract_forbidden",
            "Managed draft projects cannot use the legacy extraction route.",
            409,
        )
    try:
        _source_path, source_format, _source_sha256 = _server_source(root)
    except SourceLifecycleError as exc:
        if exc.code == "source_missing":
            return
        raise
    if source_format == "pdf":
        raise SourceLifecycleError(
            "pdf_requires_source_package_route",
            "PDF sources must use the managed source-package normalization route.",
            409,
        )


def ensure_legacy_normalizer_allowed(project_path: str | Path) -> None:
    root = _require_project_root(project_path)
    if _managed_state_exists(root):
        raise SourceLifecycleError(
            "managed_source_legacy_normalizer_forbidden",
            "Managed draft projects cannot use legacy structure-normalizer routes.",
            409,
        )


def _source_package_review_expected(
    status: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    bindings = _source_package_review_bindings_from_state(state)
    return {
        **bindings,
        "hierarchy_sha256": (state.get("hierarchy") or {}).get("sha256"),
    }


def _source_package_review_bindings_from_state(
    state: dict[str, Any],
) -> dict[str, str]:
    return {
        "state_sha256": state["integrity"]["payload_sha256"],
        "candidate_tree_sha256": state["candidate"]["tree_sha256"],
        "document_sha256": state["package"]["document"]["sha256"],
        "structure_sha256": state["package"]["structure"]["sha256"],
        "report_sha256": state["draft_structure"]["report"]["sha256"],
    }


def _unit_read_candidate_path(
    project_path: Path,
    state: dict[str, Any],
) -> Path:
    candidate_id = state["candidate"].get("candidate_id")
    tree_sha256 = state["candidate"].get("tree_sha256")
    if not isinstance(candidate_id, str) or candidate_id != f"srcpkg_{tree_sha256}":
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate ID is not content-addressed by its tree hash.",
            409,
        )
    relative = state["candidate"].get("relative_path")
    expected_relative = f"working/{CANDIDATE_DIRECTORY}/{candidate_id}"
    if relative != expected_relative:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Candidate path differs from its immutable managed location.",
            409,
        )
    return project_path / Path(*PurePosixPath(expected_relative).parts)


def _review_document_indexes(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    report_units = evidence["report"]["units"]
    chapters = evidence["document"]["chapters"]
    chapter_by_id = {chapter["chapter_id"]: chapter for chapter in chapters}
    unit_by_id = {unit["unit_id"]: unit for unit in report_units}
    unit_order = {
        unit["unit_id"]: position for position, unit in enumerate(report_units)
    }
    block_by_id: dict[str, dict[str, Any]] = {}
    block_owner: dict[str, str] = {}
    block_document_order: dict[str, int] = {}
    unit_blocks: dict[str, list[str]] = {}
    for unit in report_units:
        unit_id = unit["unit_id"]
        chapter = chapter_by_id[unit["chapter_id"]]
        block_ids: list[str] = []
        for block in chapter["blocks"]:
            block_id = block["block_id"]
            block_by_id[block_id] = block
            block_owner[block_id] = unit_id
            block_document_order[block_id] = len(block_document_order)
            block_ids.append(block_id)
        unit_blocks[unit_id] = block_ids
    return {
        "chapter_by_id": chapter_by_id,
        "unit_by_id": unit_by_id,
        "unit_order": unit_order,
        "unit_blocks": unit_blocks,
        "block_by_id": block_by_id,
        "block_owner": block_owner,
        "block_document_order": block_document_order,
    }


def _build_unit_read_snapshot(
    *,
    key: tuple[str, ...],
    doc_id: str,
    lifecycle: str,
    pipeline_run_count: int,
    expected: dict[str, str],
    evidence: dict[str, Any],
) -> _UnitReadSnapshot:
    indexes = _review_document_indexes(evidence)
    units: list[tuple[str, bytes]] = []
    retained_bytes = 1024 + sum(
        len(part.encode("utf-8")) for part in key
    )
    for unit in evidence["report"]["units"]:
        chapter = indexes["chapter_by_id"][unit["chapter_id"]]
        blocks = [
            {
                "block_id": block["block_id"],
                "order_index": block["order_index"],
                "block_type": block["block_type"],
                "source_text": block["source_text"],
            }
            for block in chapter["blocks"]
        ]
        row = {
            "unit": {
                "unit_id": unit["unit_id"],
                "chapter_id": unit["chapter_id"],
                "order_index": unit["order_index"],
                "title": unit["title"],
                "block_count": len(blocks),
            },
            "blocks": blocks,
        }
        rendered = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        retained_bytes += len(rendered) + len(unit["unit_id"].encode("utf-8")) + 128
        units.append((unit["unit_id"], rendered))
    return _UnitReadSnapshot(
        key=key,
        doc_id=doc_id,
        lifecycle=lifecycle,
        pipeline_run_count=pipeline_run_count,
        expected=tuple(
            (name, expected[name])
            for name in SOURCE_PACKAGE_REVIEW_BINDING_FIELDS
        ),
        units=tuple(units),
        retained_bytes=retained_bytes,
    )


def _remember_unit_read_snapshot(
    project_path: Path,
    candidate_root: Path,
    *,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
    lifecycle: str,
    pipeline_run_count: int,
    expected: dict[str, str],
    evidence: dict[str, Any],
) -> _UnitReadSnapshot:
    key = _unit_read_snapshot_cache_key(
        project_path,
        candidate_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
        expected=expected,
    )
    with _unit_read_snapshot_cache_lock:
        existing = _unit_read_snapshot_cache.get(key)
        if existing is not None:
            _unit_read_snapshot_cache.move_to_end(key)
            return existing
        snapshot = _build_unit_read_snapshot(
            key=key,
            doc_id=doc_id,
            lifecycle=lifecycle,
            pipeline_run_count=pipeline_run_count,
            expected=expected,
            evidence=evidence,
        )
        return _unit_read_snapshot_cache_remember(snapshot)


def _unit_read_snapshot_payload(
    snapshot: _UnitReadSnapshot,
    unit_id: str,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    rendered = next(
        (payload for candidate_id, payload in snapshot.units if candidate_id == unit_id),
        None,
    )
    if rendered is None:
        raise SourceLifecycleError(
            "source_package_review_unit_missing",
            "The requested unit is not part of the current structure review.",
            404,
        )
    row = json.loads(rendered.decode("utf-8"))
    all_blocks = row["blocks"]
    blocks = all_blocks[offset : offset + limit]
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PACKAGE_UNIT_BLOCKS_VERSION,
        "doc_id": snapshot.doc_id,
        "lifecycle": snapshot.lifecycle,
        "pipeline_run_count": snapshot.pipeline_run_count,
        "expected": dict(snapshot.expected),
        "unit": row["unit"],
        "blocks": blocks,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(blocks),
            "total": len(all_blocks),
            "has_more": offset + len(blocks) < len(all_blocks),
        },
    }
    payload["integrity"] = {
        "block_count": len(blocks),
        "payload_sha256": canonical_json_sha256(payload),
    }
    return payload


def _review_issue_queue(
    evidence: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    report = evidence["report"]
    indexes = _review_document_indexes(evidence)
    unit_by_id = indexes["unit_by_id"]
    unit_order = indexes["unit_order"]
    unit_blocks = indexes["unit_blocks"]
    block_owner = indexes["block_owner"]
    block_document_order = indexes["block_document_order"]

    review_block_ids = {
        row["block_id"]
        for row in evidence["asset_manifest"].get("block_bindings", [])
        if isinstance(row, dict) and row.get("review_required") is True
    }
    review_block_ids.update(
        row["block_id"]
        for row in evidence["admitted_projection"].get("rows", [])
        if isinstance(row, dict) and row.get("channel") == "review_required"
    )
    candidates = {
        row["candidate_id"]: row
        for row in report["global_skeleton"].get("candidates", [])
        if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)
    }

    pending: list[dict[str, Any]] = []
    for issue in report["issues"]:
        scope = issue["scope"]
        target_id = issue["target_id"]
        target_unit_id = target_id if scope == "unit" and target_id in unit_by_id else None
        candidate_ids = sorted(
            {
                value.split(":", 1)[1]
                for value in issue["evidence"]
                if isinstance(value, str)
                and value.startswith("candidate_id:")
                and value.split(":", 1)[1] in candidates
            }
        )
        related_unit_ids: set[str] = set()
        related_block_ids: set[str] = set()
        for candidate_id in candidate_ids:
            candidate = candidates[candidate_id]
            related_unit_ids.update(
                value
                for value in candidate.get("unit_ids", [])
                if value in unit_by_id
            )
            related_block_ids.update(
                value
                for value in candidate.get("block_ids", [])
                if value in block_owner
            )
            at_block_id = candidate.get("at_block_id")
            if at_block_id in block_owner:
                related_block_ids.add(at_block_id)
        if target_unit_id is not None:
            related_unit_ids.add(target_unit_id)
            if issue["code"] == "block_requires_review":
                related_block_ids.update(
                    block_id
                    for block_id in unit_blocks[target_unit_id]
                    if block_id in review_block_ids
                )
        related_unit_ids.update(
            block_owner[block_id] for block_id in related_block_ids
        )
        ordered_units = sorted(related_unit_ids, key=unit_order.__getitem__)
        ordered_blocks = sorted(
            related_block_ids,
            key=block_document_order.__getitem__,
        )
        target_block_id = ordered_blocks[0] if ordered_blocks else None
        navigation_unit_id = (
            target_unit_id
            or (ordered_units[0] if ordered_units else None)
            or (report["units"][0]["unit_id"] if report["units"] else None)
        )
        navigation_block_id = target_block_id
        if navigation_block_id is None and navigation_unit_id is not None:
            navigation_block_id = next(
                iter(unit_blocks.get(navigation_unit_id) or []),
                None,
            )
        navigation_unit_order = (
            unit_order.get(navigation_unit_id) if navigation_unit_id else None
        )
        navigation_block_order = (
            block_document_order.get(navigation_block_id)
            if navigation_block_id
            else None
        )
        unit_block_position = None
        if navigation_unit_id and navigation_block_id:
            unit_block_position = unit_blocks[navigation_unit_id].index(
                navigation_block_id
            )
        pending.append(
            {
                "issue_id": issue["issue_id"],
                "code": issue["code"],
                "scope": scope,
                "target_id": target_id,
                "target_unit_id": target_unit_id,
                "target_block_id": target_block_id,
                "related_unit_ids": ordered_units,
                "related_block_ids": ordered_blocks,
                "navigation": {
                    "unit_id": navigation_unit_id,
                    "block_id": navigation_block_id,
                    "unit_order_index": navigation_unit_order,
                    "unit_block_position": unit_block_position,
                    "document_block_position": navigation_block_order,
                },
                "evidence": copy.deepcopy(issue["evidence"]),
            }
        )
    pending.sort(
        key=lambda row: (
            row["navigation"]["unit_order_index"]
            if row["navigation"]["unit_order_index"] is not None
            else len(unit_order),
            row["navigation"]["document_block_position"]
            if row["navigation"]["document_block_position"] is not None
            else len(block_document_order),
            row["code"],
            row["issue_id"],
        )
    )
    rows = [
        {"order_index": order_index, **row}
        for order_index, row in enumerate(pending)
    ]
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PACKAGE_ISSUE_QUEUE_VERSION,
        "doc_id": report["doc_id"],
        "inputs": copy.deepcopy(expected),
        "rows": rows,
    }
    payload["integrity"] = {
        "row_count": len(rows),
        "payload_sha256": canonical_json_sha256(payload),
    }
    return payload


def get_source_package_review(
    project_path: str | Path,
    doc_id: str,
) -> dict[str, Any]:
    root = _require_project_root(project_path)
    with _managed_mutation_guard(root), _candidate_validation_request_scope():
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None:
            raise SourceLifecycleError(
                "source_package_not_managed",
                "Normalize the source package before requesting structure review.",
                409,
            )
        status = _managed_status(root, doc_id=doc_id, state=state)
        source_path, source_format, source_sha256 = _server_source(root)
        _candidate_root, evidence = _validated_candidate_for_state(
            root,
            doc_id=doc_id,
            state=state,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        editable = status["lifecycle"] != "run_started_frozen"
        expected = _source_package_review_expected(status, state)
        current = {
            name: expected[name]
            for name in SOURCE_PACKAGE_REVIEW_BINDING_FIELDS
        }
        _remember_unit_read_snapshot(
            root,
            _candidate_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
            lifecycle=status["lifecycle"],
            pipeline_run_count=status["pipeline_run_count"],
            expected=current,
            evidence=evidence,
        )
        return {
            "schema_version": SOURCE_PACKAGE_REVIEW_VERSION,
            "doc_id": doc_id,
            "lifecycle": status["lifecycle"],
            "pipeline_run_count": status["pipeline_run_count"],
            "authority": "explicit_human_approval_required",
            "experimental": {
                "scope": (
                    "os_locked_pre_run" if editable else "run_started_frozen"
                ),
                "load_bearing": not editable,
            },
            "expected": expected,
            "supported_actions": (
                ["update_unit", "split_unit", "merge_adjacent_units"]
                if editable
                else []
            ),
            "supported_hierarchy_actions": (
                ["set_parent", "clear_parent"] if editable else []
            ),
            "report": copy.deepcopy(evidence["report"]),
            "issue_queue": _review_issue_queue(evidence, expected),
        }


def get_source_package_unit_blocks(
    project_path: str | Path,
    doc_id: str,
    unit_id: str,
    *,
    expected: dict[str, str],
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    if set(expected) != set(SOURCE_PACKAGE_REVIEW_BINDING_FIELDS) or any(
        not isinstance(expected.get(name), str) or not expected[name].strip()
        for name in SOURCE_PACKAGE_REVIEW_BINDING_FIELDS
    ):
        raise SourceLifecycleError(
            "source_package_review_binding_required",
            "Unit block reads require every current review identity.",
            400,
        )
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > 200
    ):
        raise SourceLifecycleError(
            "source_package_review_pagination_invalid",
            "offset must be non-negative and limit must be between 1 and 200.",
            400,
        )
    root = _require_project_root(project_path)
    with _managed_mutation_guard(root), _candidate_validation_request_scope():
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None:
            raise SourceLifecycleError(
                "source_package_not_managed",
                "Normalize the source package before requesting unit blocks.",
                409,
            )
        source_path, source_format, source_sha256 = _server_source(root)
        current_source = {
            "filename": source_path.name,
            "format": source_format,
            "sha256": source_sha256,
        }
        if state.get("source") != current_source:
            raise SourceLifecycleError(
                "source_package_source_changed",
                "Uploaded source differs from the managed lifecycle state.",
                409,
            )
        current = _source_package_review_bindings_from_state(state)
        if expected != current:
            raise SourceLifecycleError(
                "source_package_review_stale",
                "Review identities changed; reload structure review before reading blocks.",
                409,
            )
        candidate_path = _unit_read_candidate_path(root, state)
        key = _unit_read_snapshot_cache_key(
            root,
            candidate_path,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
            expected=current,
        )
        snapshot = _unit_read_snapshot_cache_entry(key)
        if snapshot is None:
            status = _managed_status(root, doc_id=doc_id, state=state)
            candidate_root, evidence = _validated_candidate_for_state(
                root,
                doc_id=doc_id,
                state=state,
                source_path=source_path,
                source_format=source_format,
                source_sha256=source_sha256,
            )
            snapshot = _remember_unit_read_snapshot(
                root,
                candidate_root,
                source_path=source_path,
                source_format=source_format,
                source_sha256=source_sha256,
                doc_id=doc_id,
                lifecycle=status["lifecycle"],
                pipeline_run_count=status["pipeline_run_count"],
                expected=current,
                evidence=evidence,
            )
        return _unit_read_snapshot_payload(
            snapshot,
            unit_id,
            offset=offset,
            limit=limit,
        )


def _publish_candidate_tree(
    *,
    project_path: Path,
    candidates: Path,
    temporary_root: Path,
    evidence: dict[str, Any],
    source_path: Path,
    source_format: str,
    source_sha256: str,
    doc_id: str,
) -> tuple[Path, dict[str, Any], bool]:
    candidate_id = f"srcpkg_{evidence['tree']['tree_sha256']}"
    final_root = candidates / candidate_id
    reused = False
    if _path_exists(final_root):
        _require_plain_path(final_root, owner="source package candidate")
        existing = _validate_candidate(
            final_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        if existing["tree"] != evidence["tree"] or not _files_byte_identical(
            temporary_root,
            final_root,
        ):
            raise SourceLifecycleError(
                "source_package_candidate_collision",
                "Content-addressed candidate exists with different bytes.",
                409,
            )
        reused = True
    else:
        try:
            os.replace(temporary_root, final_root)
        except OSError:
            if not final_root.is_dir():
                raise
            existing = _validate_candidate(
                final_root,
                source_path=source_path,
                source_format=source_format,
                source_sha256=source_sha256,
                doc_id=doc_id,
            )
            if existing["tree"] != evidence["tree"] or not _files_byte_identical(
                temporary_root,
                final_root,
            ):
                raise SourceLifecycleError(
                    "source_package_candidate_collision",
                    "Concurrent candidate differs from generated bytes.",
                    409,
                )
            reused = True
    if reused and temporary_root.exists():
        shutil.rmtree(temporary_root)
    final_evidence = _validate_candidate(
        final_root,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        doc_id=doc_id,
    )
    return final_root, final_evidence, reused


def _require_pre_run_editable(state: dict[str, Any]) -> None:
    if state.get("lifecycle") == "run_started_frozen" or int(
        state.get("pipeline_run_count") or 0
    ) > 0:
        raise SourceLifecycleError(
            "source_package_frozen",
            "The first pipeline run permanently froze this source-package revision.",
            409,
        )


def apply_managed_source_corrections(
    project_path: str | Path,
    doc_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    request_data = _validate_correction_request(request_payload)
    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        return _apply_managed_source_corrections_locked(root, doc_id, request_data)


def _parent_decision_sha(state: dict[str, Any]) -> str | None:
    if state.get("schema_version") != SOURCE_LIFECYCLE_V2_VERSION:
        return None
    return state["latest_decision"]["sha256"]


def _revision_event_payload(
    *,
    doc_id: str,
    operation: str,
    source: dict[str, Any],
    bootstrap: dict[str, Any],
    parent_state: dict[str, Any],
    request: dict[str, Any],
    child: dict[str, Any],
    hierarchy: dict[str, Any],
    finalization: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PACKAGE_REVISION_VERSION,
        "doc_id": doc_id,
        "operation": operation,
        "source": copy.deepcopy(source),
        "bootstrap": copy.deepcopy(bootstrap),
        "parent": {
            "state_schema_version": parent_state["schema_version"],
            "state_sha256": parent_state["integrity"]["payload_sha256"],
            "candidate_tree_sha256": parent_state["candidate"]["tree_sha256"],
            "decision_sha256": _parent_decision_sha(parent_state),
        },
        "request": copy.deepcopy(request),
        "child": copy.deepcopy(child),
        "hierarchy": copy.deepcopy(hierarchy),
        "finalization": copy.deepcopy(finalization),
    }
    payload["integrity"] = {"payload_sha256": canonical_json_sha256(payload)}
    return payload


def apply_managed_source_hierarchy(
    project_path: str | Path,
    doc_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    request_data = _validate_hierarchy_request(request_payload)
    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None:
            raise SourceLifecycleError(
                "source_package_not_managed",
                "Normalize the source package before applying hierarchy.",
                409,
            )
        status = _managed_status(root, doc_id=doc_id, state=state)
        _require_pre_run_editable(state)
        if state["schema_version"] == SOURCE_LIFECYCLE_V2_VERSION:
            latest = _read_decision(
                root,
                doc_id=doc_id,
                decision_sha256=state["latest_decision"]["sha256"],
            )
            if (
                latest.get("operation") == "hierarchy_update"
                and latest.get("request") == request_data
            ):
                return {
                    **status,
                    "hierarchy_created": False,
                    "hierarchy_reused": True,
                    "decision_created": False,
                    "decision_reused": True,
                }
        if (
            request_data["expected_state_sha256"] != status["state_sha256"]
            or request_data["expected_candidate_tree_sha256"]
            != status["candidate"]["tree_sha256"]
            or request_data["expected_report_sha256"]
            != status["draft_structure"]["report"]["sha256"]
        ):
            raise SourceLifecycleError(
                "source_package_hierarchy_stale",
                "Hierarchy request identities differ from the current revision.",
                409,
            )
        source_path, source_format, source_sha256 = _server_source(root)
        candidate_root, evidence = _validated_candidate_for_state(
            root,
            doc_id=doc_id,
            state=state,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        current_hierarchy = state.get("hierarchy") or _null_binding(
            HIERARCHY_OVERLAY_VERSION
        )
        try:
            plan = _hierarchy_plan_for_revision(
                root,
                report=evidence["report"],
                actions=request_data["actions"],
                user=request_data["user"],
                current_binding=current_hierarchy,
            )
            overlay = apply_hierarchy_plan(
                evidence["document"],
                evidence["structure"],
                evidence["asset_manifest"],
                evidence["admitted_projection"],
                _project_state(doc_id),
                evidence["report"],
                plan,
                package_root=candidate_root,
            )
        except DraftStructureError as exc:
            raise SourceLifecycleError(
                "source_package_hierarchy_invalid", str(exc), 409
            ) from exc
        plan_sha = plan["integrity"]["payload_sha256"]
        overlay_sha = overlay["integrity"]["payload_sha256"]
        hierarchy_binding = _bound_identity(HIERARCHY_OVERLAY_VERSION, overlay_sha)
        if hierarchy_binding == current_hierarchy:
            raise SourceLifecycleError(
                "source_package_hierarchy_noop",
                "Hierarchy request does not change the current overlay.",
                409,
            )
        hierarchy_parent = _hierarchy_parent(root)
        if _path_exists(hierarchy_parent):
            _require_plain_path(hierarchy_parent, owner="hierarchy artifact parent")
        hierarchy_parent.mkdir(parents=True, exist_ok=True)
        plan_created = _publish_immutable_json(
            _hierarchy_plan_path(root, plan_sha),
            plan,
            owner="hierarchy plan",
        )
        overlay_created = _publish_immutable_json(
            _hierarchy_overlay_path(root, overlay_sha),
            overlay,
            owner="hierarchy overlay",
        )
        state_v1 = _read_managed_state(root, doc_id=doc_id)
        if state_v1 is None:
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "Hierarchy revision requires its immutable v1 bootstrap.",
                409,
            )
        bootstrap = _bootstrap_identity(root, state_v1)
        child = _child_binding(
            project_path=root, candidate_root=candidate_root, evidence=evidence
        )
        event = _revision_event_payload(
            doc_id=doc_id,
            operation="hierarchy_update",
            source=status["source"],
            bootstrap=bootstrap,
            parent_state=state,
            request=request_data,
            child=child,
            hierarchy=hierarchy_binding,
            finalization=_null_binding(SOURCE_PACKAGE_FINALIZATION_VERSION),
        )
        decision_sha = event["integrity"]["payload_sha256"]
        decisions = _decision_parent(root)
        if _path_exists(decisions):
            _require_plain_path(decisions, owner="decision parent")
        decisions.mkdir(parents=True, exist_ok=True)
        decision_created = _publish_immutable_json(
            _decision_path(root, decision_sha),
            event,
            owner="source package revision",
        )
        expected_v2, _validated = _validate_decision_lineage(
            root,
            doc_id=doc_id,
            decision_sha256=decision_sha,
            state_v1=state_v1,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        try:
            _atomic_json_write(_state_v2_path(root), expected_v2)
        except OSError as exc:
            raise SourceLifecycleError(
                "source_package_hierarchy_failed",
                f"Unable to publish the hierarchy revision pointer: {exc}",
                500,
            ) from exc
        final_status = _managed_status_v2(root, doc_id=doc_id, state=expected_v2)
        return {
            **final_status,
            "hierarchy_created": plan_created or overlay_created,
            "hierarchy_reused": not (plan_created or overlay_created),
            "decision_created": decision_created,
            "decision_reused": not decision_created,
        }


def finalize_managed_source_package(
    project_path: str | Path,
    doc_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    request_data = _validate_finalization_request(request_payload)
    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None:
            raise SourceLifecycleError(
                "source_package_not_managed",
                "Normalize the source package before finalization.",
                409,
            )
        status = _managed_status(root, doc_id=doc_id, state=state)
        _require_pre_run_editable(state)
        if state["schema_version"] == SOURCE_LIFECYCLE_V2_VERSION:
            latest = _read_decision(
                root,
                doc_id=doc_id,
                decision_sha256=state["latest_decision"]["sha256"],
            )
            if (
                latest.get("operation") == "finalize_pre_run"
                and latest.get("request") == request_data
            ):
                return {
                    **status,
                    "finalization_created": False,
                    "finalization_reused": True,
                    "decision_created": False,
                    "decision_reused": True,
                }
        if state.get("lifecycle") != "draft" or state.get("pipeline_run_count") != 0:
            raise SourceLifecycleError(
                "source_package_finalization_frozen",
                "Only a draft revision with no pipeline run can be finalized.",
                409,
            )
        current_hierarchy = state.get("hierarchy") or _null_binding(
            HIERARCHY_OVERLAY_VERSION
        )
        if (
            request_data["expected_state_sha256"] != status["state_sha256"]
            or request_data["expected_candidate_tree_sha256"]
            != status["candidate"]["tree_sha256"]
            or request_data["expected_report_sha256"]
            != status["draft_structure"]["report"]["sha256"]
            or request_data["expected_hierarchy_sha256"]
            != current_hierarchy["sha256"]
        ):
            raise SourceLifecycleError(
                "source_package_finalization_stale",
                "Finalization request identities differ from the current revision.",
                409,
            )
        source_path, source_format, source_sha256 = _server_source(root)
        candidate_root, evidence = _validated_candidate_for_state(
            root,
            doc_id=doc_id,
            state=state,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        child = _child_binding(
            project_path=root, candidate_root=candidate_root, evidence=evidence
        )
        state_v1 = _read_managed_state(root, doc_id=doc_id)
        if state_v1 is None:
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "Finalization requires its immutable v1 bootstrap.",
                409,
            )
        bootstrap = _bootstrap_identity(root, state_v1)
        finalization = _finalization_record_payload(
            doc_id=doc_id,
            source=status["source"],
            bootstrap=bootstrap,
            parent_state=state,
            child=child,
            hierarchy=current_hierarchy,
            user=request_data["user"],
        )
        finalization_sha = finalization["integrity"]["payload_sha256"]
        finalization_parent = _finalization_parent(root)
        if _path_exists(finalization_parent):
            _require_plain_path(finalization_parent, owner="finalization parent")
        finalization_parent.mkdir(parents=True, exist_ok=True)
        finalization_created = _publish_immutable_json(
            _finalization_path(root, finalization_sha),
            finalization,
            owner="source package finalization",
        )
        finalization_binding = _bound_identity(
            SOURCE_PACKAGE_FINALIZATION_VERSION, finalization_sha
        )
        event = _revision_event_payload(
            doc_id=doc_id,
            operation="finalize_pre_run",
            source=status["source"],
            bootstrap=bootstrap,
            parent_state=state,
            request=request_data,
            child=child,
            hierarchy=current_hierarchy,
            finalization=finalization_binding,
        )
        decision_sha = event["integrity"]["payload_sha256"]
        decisions = _decision_parent(root)
        if _path_exists(decisions):
            _require_plain_path(decisions, owner="decision parent")
        decisions.mkdir(parents=True, exist_ok=True)
        decision_created = _publish_immutable_json(
            _decision_path(root, decision_sha),
            event,
            owner="source package revision",
        )
        expected_v2, _validated = _validate_decision_lineage(
            root,
            doc_id=doc_id,
            decision_sha256=decision_sha,
            state_v1=state_v1,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        try:
            _atomic_json_write(_state_v2_path(root), expected_v2)
        except OSError as exc:
            raise SourceLifecycleError(
                "source_package_finalization_failed",
                f"Unable to publish the finalization pointer: {exc}",
                500,
            ) from exc
        final_status = _managed_status_v2(root, doc_id=doc_id, state=expected_v2)
        return {
            **final_status,
            "finalization_created": finalization_created,
            "finalization_reused": not finalization_created,
            "decision_created": decision_created,
            "decision_reused": not decision_created,
        }


def get_managed_runtime_context(
    project_path: str | Path,
    doc_id: str,
) -> dict[str, Any]:
    root = _require_project_root(project_path)
    with _managed_mutation_guard(root):
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None or state.get("schema_version") != SOURCE_LIFECYCLE_V2_VERSION:
            raise SourceLifecycleError(
                "source_package_not_finalized",
                "Managed runtime preparation requires a finalized source package.",
                409,
            )
        status = _managed_status_v2(root, doc_id=doc_id, state=state)
        if state["lifecycle"] not in {"finalized_pre_run", "run_started_frozen"}:
            raise SourceLifecycleError(
                "source_package_not_finalized",
                "Managed runtime preparation requires finalized_pre_run state.",
                409,
            )
        source_path, source_format, source_sha256 = _server_source(root)
        candidate_root, evidence = _validated_candidate_for_state(
            root,
            doc_id=doc_id,
            state=state,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        if state["lifecycle"] == "run_started_frozen":
            run_start = _read_run_start_event(
                root,
                doc_id=doc_id,
                run_start_sha256=state["run_start"]["sha256"],
                parent_state=_validate_decision_lineage(
                    root,
                    doc_id=doc_id,
                    decision_sha256=state["latest_decision"]["sha256"],
                    state_v1=_read_managed_state(root, doc_id=doc_id),
                    source_path=source_path,
                    source_format=source_format,
                    source_sha256=source_sha256,
                )[0],
            )
            managed_source = copy.deepcopy(run_start["runtime"]["managed_source"])
        else:
            run_start = None
            managed_source = _managed_runtime_binding(state)
        finalization_sha = managed_source["finalization"]["sha256"]
        finalization_record = _read_finalization_record(
            root,
            doc_id=doc_id,
            finalization_sha256=finalization_sha,
        )
        hierarchy_overlay = None
        hierarchy_sha = managed_source["hierarchy"]["sha256"]
        if hierarchy_sha is not None:
            hierarchy_overlay = _read_hierarchy_artifact(
                root, prefix="hoverlay", payload_sha256=hierarchy_sha
            )
        return {
            "doc_id": doc_id,
            "project_path": str(root),
            "candidate_root": str(candidate_root),
            "lifecycle": state["lifecycle"],
            "pipeline_run_count": state["pipeline_run_count"],
            "managed_source": managed_source,
            "state": copy.deepcopy(state),
            "state_path": str(_state_v2_path(root)),
            "state_file_sha256": _file_sha256(_state_v2_path(root)),
            "finalization_record": finalization_record,
            "finalization_path": str(
                _finalization_path(root, finalization_sha)
            ),
            "hierarchy_overlay": hierarchy_overlay,
            "hierarchy_path": (
                str(_hierarchy_overlay_path(root, hierarchy_sha))
                if hierarchy_sha is not None
                else None
            ),
            "run_start": copy.deepcopy(run_start),
            "status": status,
            "evidence": evidence,
        }


def get_managed_runtime_status_context(
    project_path: str | Path,
    doc_id: str,
) -> dict[str, Any] | None:
    """Read sealed runtime-status identities without walking candidate bytes.

    Full candidate validation remains mandatory for prepare, freeze,
    publication, and source-package review operations.
    """

    root = _require_project_root(project_path)
    with _managed_mutation_guard(root):
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None:
            return None

        source_path, source_format, source_sha256 = _server_source(root)
        expected_source = {
            "filename": source_path.name,
            "format": source_format,
            "sha256": source_sha256,
        }
        if state.get("source") != expected_source:
            raise SourceLifecycleError(
                "source_package_source_changed",
                "Uploaded source differs from the managed lifecycle state.",
                409,
            )

        if state.get("schema_version") == SOURCE_LIFECYCLE_VERSION:
            return {
                "doc_id": doc_id,
                "project_path": str(root),
                "lifecycle": "draft",
                "pipeline_run_count": 0,
                "managed_source": None,
                "state_sha256": state["integrity"]["payload_sha256"],
                "run_start": None,
            }

        state_v1 = _read_managed_state(root, doc_id=doc_id)
        if state_v1 is None or state.get("bootstrap") != _bootstrap_identity(root, state_v1):
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "Lifecycle v2 bootstrap identity differs from source_lifecycle_v1.json.",
                409,
            )

        decision = _read_decision(
            root,
            doc_id=doc_id,
            decision_sha256=state["latest_decision"]["sha256"],
        )
        expected_child = {
            name: copy.deepcopy(state[name])
            for name in ("candidate", "package", "draft_structure", "policies")
        }
        if (
            state["latest_decision"]["schema_version"]
            != decision.get("schema_version")
            or decision.get("source") != expected_source
            or decision.get("bootstrap") != state["bootstrap"]
            or decision.get("child") != expected_child
            or decision.get("hierarchy") != state["hierarchy"]
            or decision.get("finalization") != state["finalization"]
        ):
            raise SourceLifecycleError(
                "source_lifecycle_stale",
                "Source lifecycle differs from its latest sealed decision.",
                409,
            )

        hierarchy_sha = state["hierarchy"]["sha256"]
        if hierarchy_sha is not None:
            _read_hierarchy_artifact(
                root,
                prefix="hoverlay",
                payload_sha256=hierarchy_sha,
            )

        finalization_sha = state["finalization"]["sha256"]
        if finalization_sha is not None:
            finalization = _read_finalization_record(
                root,
                doc_id=doc_id,
                finalization_sha256=finalization_sha,
            )
            for name, expected in {
                "source": expected_source,
                "bootstrap": state["bootstrap"],
                "candidate": state["candidate"],
                "package": state["package"],
                "draft_structure": state["draft_structure"],
                "policies": state["policies"],
                "hierarchy": state["hierarchy"],
            }.items():
                if finalization.get(name) != expected:
                    raise SourceLifecycleError(
                        "source_package_finalization_invalid",
                        f"Finalization {name} differs from the managed lifecycle.",
                        409,
                    )

        run_start = None
        if state["lifecycle"] == "run_started_frozen":
            run_start_sha = state["run_start"]["sha256"]
            run_start_path = _require_confined_existing(
                _run_start_path(root, run_start_sha),
                root,
                owner="run-start evidence",
            )
            run_start = _read_json_object(run_start_path, owner="run-start evidence")
            _require_exact_fields(
                run_start,
                _RUN_START_EVENT_FIELDS,
                owner="run-start evidence",
            )
            parent = run_start.get("parent")
            runtime = run_start.get("runtime")
            integrity = run_start.get("integrity")
            if not isinstance(parent, dict) or not isinstance(runtime, dict):
                raise SourceLifecycleError(
                    "source_package_run_start_invalid",
                    "Run-start parent and runtime bindings must be objects.",
                    409,
                )
            _require_exact_fields(parent, _RUN_START_PARENT_FIELDS, owner="run-start parent")
            _require_exact_fields(runtime, _RUN_START_RUNTIME_FIELDS, owner="run-start runtime")
            managed_source = runtime.get("managed_source")
            if not isinstance(managed_source, dict):
                raise SourceLifecycleError(
                    "source_package_run_start_invalid",
                    "Run-start managed source binding must be an object.",
                    409,
                )
            _require_exact_fields(
                managed_source,
                _MANAGED_RUNTIME_BINDING_FIELDS,
                owner="managed runtime binding",
            )
            expected_parent = {
                "state_sha256": managed_source.get("state_sha256"),
                "candidate_tree_sha256": state["candidate"]["tree_sha256"],
                "latest_decision_sha256": state["latest_decision"]["sha256"],
                "hierarchy_sha256": hierarchy_sha,
                "finalization_sha256": finalization_sha,
            }
            expected_binding_fields = {
                "candidate": state["candidate"],
                "package": state["package"],
                "latest_decision": state["latest_decision"],
                "hierarchy": state["hierarchy"],
                "finalization": state["finalization"],
            }
            expected_run_start_sha = canonical_json_sha256(
                {
                    key: copy.deepcopy(value)
                    for key, value in run_start.items()
                    if key != "integrity"
                }
            )
            if (
                run_start.get("schema_version") != SOURCE_PACKAGE_RUN_START_VERSION
                or run_start.get("doc_id") != doc_id
                or parent != expected_parent
                or runtime.get("manifest_schema_version")
                != MANAGED_RUNTIME_MANIFEST_VERSION
                or any(
                    managed_source.get(name) != expected
                    for name, expected in expected_binding_fields.items()
                )
                or not isinstance(integrity, dict)
                or set(integrity) != _INTEGRITY_FIELDS
                or integrity.get("payload_sha256") != expected_run_start_sha
                or expected_run_start_sha != run_start_sha
            ):
                raise SourceLifecycleError(
                    "source_package_run_start_invalid",
                    "Run-start evidence differs from the frozen managed source.",
                    409,
                )
            job_id = _require_safe_runtime_identifier(
                run_start.get("job_id"), owner="job_id"
            )
            _require_safe_runtime_identifier(run_start.get("run_id"), owner="run_id")
            if runtime.get("manifest_relative_path") != f"{job_id}/source_manifest.json":
                raise SourceLifecycleError(
                    "source_package_run_start_invalid",
                    "Run-start runtime path differs from its job identity.",
                    409,
                )
            _require_sha256(
                runtime.get("manifest_sha256"), owner="run-start manifest sha256"
            )
        elif state["lifecycle"] == "finalized_pre_run":
            managed_source = _managed_runtime_binding(state)
        else:
            managed_source = None

        occupancy = _legacy_occupancy(
            root,
            doc_id=doc_id,
            allowed_managed_runtime=managed_source,
        )
        if occupancy:
            raise SourceLifecycleError(
                "managed_source_legacy_conflict",
                "Legacy project or runtime evidence appeared after managed publication: "
                + ", ".join(occupancy),
                409,
            )

        return {
            "doc_id": doc_id,
            "project_path": str(root),
            "lifecycle": state["lifecycle"],
            "pipeline_run_count": state["pipeline_run_count"],
            "managed_source": copy.deepcopy(managed_source),
            "state_sha256": state["integrity"]["payload_sha256"],
            "run_start": copy.deepcopy(run_start),
        }


def freeze_managed_source_for_run(
    project_path: str | Path,
    doc_id: str,
    *,
    job_id: str,
    run_id: str,
    runtime_manifest_path: str | Path,
) -> dict[str, Any]:
    root = _require_project_root(project_path)
    job_id = _require_safe_runtime_identifier(job_id, owner="job_id")
    run_id = _require_safe_runtime_identifier(run_id, owner="run_id")
    with _managed_source_mutation_guard(root):
        state = _read_authoritative_state(root, doc_id=doc_id)
        if state is None or state.get("schema_version") != SOURCE_LIFECYCLE_V2_VERSION:
            raise SourceLifecycleError(
                "source_package_not_finalized",
                "A managed finalized source package is required before the first run.",
                409,
            )
        status = _managed_status_v2(root, doc_id=doc_id, state=state)
        if state["lifecycle"] == "run_started_frozen":
            event = _read_run_start_event(
                root,
                doc_id=doc_id,
                run_start_sha256=state["run_start"]["sha256"],
                parent_state=_validate_decision_lineage(
                    root,
                    doc_id=doc_id,
                    decision_sha256=state["latest_decision"]["sha256"],
                    state_v1=_read_managed_state(root, doc_id=doc_id),
                    source_path=_server_source(root)[0],
                    source_format=_server_source(root)[1],
                    source_sha256=_server_source(root)[2],
                )[0],
            )
            if event["run_id"] != run_id or event["job_id"] != job_id:
                raise SourceLifecycleError(
                    "source_package_already_frozen",
                    "This source package is already frozen to a different run or job.",
                    409,
                )
            _path, _manifest, _relative, manifest_sha = _runtime_manifest_evidence(
                doc_id=doc_id,
                job_id=job_id,
                manifest_path=runtime_manifest_path,
                managed_source=event["runtime"]["managed_source"],
            )
            if manifest_sha != event["runtime"]["manifest_sha256"]:
                raise SourceLifecycleError(
                    "source_package_runtime_invalid",
                    "The frozen runtime manifest bytes changed.",
                    409,
                )
            return {**status, "run_start_created": False, "run_start_reused": True}
        if state["lifecycle"] != "finalized_pre_run" or state["pipeline_run_count"] != 0:
            raise SourceLifecycleError(
                "source_package_not_finalized",
                "Only finalized_pre_run state can accept its first pipeline run.",
                409,
            )
        managed_source = _managed_runtime_binding(state)
        _path, _manifest, relative_path, manifest_sha = _runtime_manifest_evidence(
            doc_id=doc_id,
            job_id=job_id,
            manifest_path=runtime_manifest_path,
            managed_source=managed_source,
        )
        event = _run_start_event_payload(
            doc_id=doc_id,
            run_id=run_id,
            job_id=job_id,
            parent_state=state,
            manifest_relative_path=relative_path,
            manifest_sha256=manifest_sha,
            managed_source=managed_source,
        )
        run_start_sha = event["integrity"]["payload_sha256"]
        event_parent = _run_start_parent(root)
        if _path_exists(event_parent):
            _require_plain_path(event_parent, owner="run-start parent")
        event_parent.mkdir(parents=True, exist_ok=True)
        event_created = _publish_immutable_json(
            _run_start_path(root, run_start_sha), event, owner="run-start evidence"
        )
        frozen_state = _frozen_state_payload(
            state, run_start_sha256=run_start_sha
        )
        try:
            _atomic_json_write(_state_v2_path(root), frozen_state)
        except OSError as exc:
            raise SourceLifecycleError(
                "source_package_run_start_failed",
                f"Unable to publish the frozen lifecycle pointer: {exc}",
                500,
            ) from exc
        final_status = _managed_status_v2(root, doc_id=doc_id, state=frozen_state)
        return {
            **final_status,
            "run_start_created": event_created,
            "run_start_reused": not event_created,
        }


def publish_managed_translation(
    project_path: str | Path,
    doc_id: str,
    translation_overlay: dict[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(translation_overlay, dict)
        or translation_overlay.get("schema_version") != OVERLAY_VERSION
    ):
        raise SourceLifecycleError(
            "source_package_publication_invalid",
            f"Publication requires one {OVERLAY_VERSION} JSON object.",
            400,
        )
    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        state = _read_authoritative_state(root, doc_id=doc_id)
        if (
            state is None
            or state.get("schema_version") != SOURCE_LIFECYCLE_V2_VERSION
            or state.get("lifecycle") != "run_started_frozen"
            or int(state.get("pipeline_run_count") or 0) < 1
        ):
            raise SourceLifecycleError(
                "source_package_publication_not_frozen",
                "Publication requires the exact source package frozen by its first run.",
                409,
            )
        status = _managed_status_v2(root, doc_id=doc_id, state=state)
        source_path, source_format, source_sha256 = _server_source(root)
        candidate_root, _evidence = _validated_candidate_for_state(
            root,
            doc_id=doc_id,
            state=state,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        publication_parent = _publication_parent(root)
        if _path_exists(publication_parent):
            _require_plain_path(publication_parent, owner="publication parent")
        publication_parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(
            tempfile.mkdtemp(prefix=".publication-", dir=publication_parent)
        )
        rendered_root = temporary_root / "rendered"
        try:
            try:
                result = export_source_package(
                    candidate_root,
                    copy.deepcopy(translation_overlay),
                    rendered_root,
                    review_mode="error",
                    pandoc_executable=THESIS_PANDOC_EXE,
                )
            except SourcePackageExportError as exc:
                raise SourceLifecycleError(
                    "source_package_publication_invalid", str(exc), 409
                ) from exc
            files = _tree_files(rendered_root)
            rows = [
                {
                    "path": path.relative_to(rendered_root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }
                for path in files
            ]
            publication_binding = {
                "schema_version": SOURCE_PACKAGE_PUBLICATION_VERSION,
                "doc_id": doc_id,
                "frozen_state_sha256": state["integrity"]["payload_sha256"],
                "run_start_sha256": state["run_start"]["sha256"],
                "candidate_tree_sha256": state["candidate"]["tree_sha256"],
                "translation_overlay_sha256": canonical_json_sha256(
                    translation_overlay
                ),
                "export_payload_sha256": result.manifest["integrity"][
                    "export_payload_sha256"
                ],
                "tree_sha256": canonical_json_sha256(rows),
                "file_count": len(rows),
            }
            publication_sha = canonical_json_sha256(publication_binding)
            publication_id = f"publication_{publication_sha}"
            final_root = publication_parent / publication_id
            reused = False
            if _path_exists(final_root):
                _require_plain_path(final_root, owner="publication output")
                if not final_root.is_dir() or not _files_byte_identical(
                    rendered_root, final_root
                ):
                    raise SourceLifecycleError(
                        "source_package_publication_collision",
                        "Content-addressed publication exists with different bytes.",
                        409,
                    )
                reused = True
            else:
                try:
                    os.replace(rendered_root, final_root)
                except OSError:
                    if not final_root.is_dir() or not _files_byte_identical(
                        rendered_root, final_root
                    ):
                        raise
                    reused = True
            if reused and rendered_root.exists():
                shutil.rmtree(rendered_root, ignore_errors=True)
            final_files = _tree_files(final_root)
            final_rows = [
                {
                    "path": path.relative_to(final_root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }
                for path in final_files
            ]
            if (
                canonical_json_sha256(final_rows)
                != publication_binding["tree_sha256"]
                or len(final_rows) != publication_binding["file_count"]
            ):
                raise SourceLifecycleError(
                    "source_package_publication_collision",
                    "Published output differs from the rendered content address.",
                    409,
                )
            return {
                "schema_version": SOURCE_PACKAGE_PUBLICATION_VERSION,
                "doc_id": doc_id,
                "publication_id": publication_id,
                "relative_path": final_root.relative_to(root).as_posix(),
                "created": not reused,
                "reused": reused,
                "binding": publication_binding,
                "artifacts": copy.deepcopy(result.manifest["artifacts"]),
                "lifecycle": status["lifecycle"],
                "pipeline_run_count": status["pipeline_run_count"],
            }
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)


def _apply_managed_source_corrections_locked(
    root: Path,
    doc_id: str,
    request_data: dict[str, Any],
) -> dict[str, Any]:
    state = _read_authoritative_state(root, doc_id=doc_id)
    if state is None:
        raise SourceLifecycleError(
            "source_package_not_managed",
            "Normalize the source package before applying corrections.",
            409,
        )
    status = _managed_status(root, doc_id=doc_id, state=state)
    _require_pre_run_editable(state)
    if state["schema_version"] == SOURCE_LIFECYCLE_V2_VERSION:
        latest = _read_decision(
            root,
            doc_id=doc_id,
            decision_sha256=state["latest_decision"]["sha256"],
        )
        if latest["request"] == request_data:
            return {
                **status,
                "candidate_created": False,
                "candidate_reused": True,
                "decision_created": False,
                "decision_reused": True,
            }
    if (
        request_data["expected_state_sha256"] != status["state_sha256"]
        or request_data["expected_candidate_tree_sha256"]
        != status["candidate"]["tree_sha256"]
        or request_data["expected_report_sha256"]
        != status["draft_structure"]["report"]["sha256"]
    ):
        raise SourceLifecycleError(
            "source_package_correction_stale",
            "Correction request identities differ from the current managed draft.",
            409,
        )

    source_path, source_format, source_sha256 = _server_source(root)
    parent_root, parent_evidence = _validated_candidate_for_state(
        root,
        doc_id=doc_id,
        state=state,
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
    )
    try:
        plan = build_correction_plan(
            parent_evidence["report"],
            request_data["actions"],
            proposer={"kind": "human", "identifier": request_data["user"]},
        )
        _require_effective_actions(plan, parent_evidence["report"])
        correction = apply_correction_plan(
            parent_evidence["document"],
            parent_evidence["structure"],
            parent_evidence["asset_manifest"],
            parent_evidence["admitted_projection"],
            _project_state(doc_id),
            parent_evidence["report"],
            plan,
            package_root=parent_root,
        )
    except DraftStructureError as exc:
        raise SourceLifecycleError(
            "source_package_correction_invalid",
            str(exc),
            409,
        ) from exc
    if (
        canonical_json_sha256(correction.document)
        == canonical_json_sha256(parent_evidence["document"])
        and canonical_json_sha256(correction.structure_manifest)
        == canonical_json_sha256(parent_evidence["structure"])
    ):
        raise SourceLifecycleError(
            "source_package_correction_noop",
            "Accepted actions do not change document or structure identity.",
            409,
        )

    state_v1 = _read_managed_state(root, doc_id=doc_id)
    if state_v1 is None:
        raise SourceLifecycleError(
            "source_lifecycle_invalid",
            "Managed correction requires its immutable v1 bootstrap.",
            409,
        )
    bootstrap_bytes = _state_path(root).read_bytes()
    bootstrap = _bootstrap_identity(root, state_v1)
    candidates = _candidate_parent(root)
    _require_plain_path(candidates, owner="candidate parent")
    candidates.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=".source-correction-", dir=candidates))
    published = False
    try:
        _atomic_json_write(temporary_root / "document.json", correction.document)
        _atomic_json_write(
            temporary_root / "structure_manifest.json",
            correction.structure_manifest,
        )
        _atomic_json_write(
            temporary_root / "normalization_receipt.json",
            correction.normalization_receipt,
        )
        materialize_source_package(
            correction.document,
            correction.structure_manifest,
            temporary_root,
        )
        core = _load_core_package(
            temporary_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        report = build_draft_structure_report(
            core["document"],
            core["structure"],
            core["asset_manifest"],
            core["admitted_projection"],
            _project_state(doc_id),
            package_root=temporary_root,
        )
        validate_draft_structure_report_shape(report)
        _atomic_json_write(temporary_root / REPORT_FILENAME, report)
        if _file_sha256(source_path) != source_sha256:
            raise SourceLifecycleError(
                "source_changed_during_normalization",
                "Source bytes changed while corrections were materialized.",
                409,
            )
        temporary_evidence = _validate_candidate(
            temporary_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        child_root, child_evidence, candidate_reused = _publish_candidate_tree(
            project_path=root,
            candidates=candidates,
            temporary_root=temporary_root,
            evidence=temporary_evidence,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        published = not candidate_reused
        child = _child_binding(
            project_path=root,
            candidate_root=child_root,
            evidence=child_evidence,
        )
        parent_decision = (
            state["latest_decision"]["sha256"]
            if state["schema_version"] == SOURCE_LIFECYCLE_V2_VERSION
            else None
        )
        event: dict[str, Any] = {
            "schema_version": SOURCE_PACKAGE_DECISION_VERSION,
            "doc_id": doc_id,
            "operation": "boundary_correction",
            "source": copy.deepcopy(status["source"]),
            "bootstrap": copy.deepcopy(bootstrap),
            "parent": {
                "state_schema_version": state["schema_version"],
                "state_sha256": status["state_sha256"],
                "candidate_tree_sha256": status["candidate"]["tree_sha256"],
                "decision_sha256": parent_decision,
            },
            "request": copy.deepcopy(request_data),
            "plan": copy.deepcopy(plan),
            "correction_receipt": copy.deepcopy(correction.correction_receipt),
            "child": child,
            "hierarchy": _null_binding("draft_structure_hierarchy_overlay_v1"),
            "finalization": _null_binding("source_package_finalization_v1"),
        }
        decision_sha256 = canonical_json_sha256(event)
        event["integrity"] = {"payload_sha256": decision_sha256}
        decisions = _decision_parent(root)
        if _path_exists(decisions):
            _require_plain_path(decisions, owner="decision parent")
        decisions.mkdir(parents=True, exist_ok=True)
        decision_created = _publish_immutable_json(
            _decision_path(root, decision_sha256),
            event,
            owner="source package decision",
        )
        expected_v2, _validated_event = _validate_decision_lineage(
            root,
            doc_id=doc_id,
            decision_sha256=decision_sha256,
            state_v1=state_v1,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
        )
        if _state_path(root).read_bytes() != bootstrap_bytes:
            raise SourceLifecycleError(
                "source_lifecycle_invalid",
                "Immutable source_lifecycle_v1.json changed during correction.",
                409,
            )
        _require_no_legacy_occupancy(root, doc_id=doc_id)
        _atomic_json_write(_state_v2_path(root), expected_v2)
        final_status = _managed_status_v2(root, doc_id=doc_id, state=expected_v2)
        return {
            **final_status,
            "candidate_created": not candidate_reused,
            "candidate_reused": candidate_reused,
            "decision_created": decision_created,
            "decision_reused": not decision_created,
        }
    except SourceLifecycleError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceLifecycleError(
            "source_package_correction_failed",
            str(exc),
            422,
        ) from exc
    finally:
        if temporary_root.exists() and not published:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _write_atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_plain_path(path.parent, owner=f"{path.name} parent")
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _path_exists(path):
            raise SourceLifecycleError(
                "d2l_presegmented_source_collision",
                f"{path.name} appeared while the import was being published.",
                409,
            )
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _stage_d2l_presegmented_capture(
    root: Path,
    *,
    source_bytes: bytes,
    block_map_bytes: bytes,
    manifest_bytes: bytes,
) -> tuple[Path, Any]:
    for name, payload in {
        "source": source_bytes,
        "block_map": block_map_bytes,
        "manifest": manifest_bytes,
    }.items():
        if not isinstance(payload, bytes) or not payload:
            raise SourceLifecycleError(
                "d2l_presegmented_bundle_invalid",
                f"Uploaded {name} must contain non-empty bytes.",
                400,
            )
    working = root / "working"
    if _path_exists(working):
        _require_plain_path(working, owner="working directory")
        if not working.is_dir():
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                "working must be a directory.",
                409,
            )
    else:
        working.mkdir(parents=True)
    staging = Path(tempfile.mkdtemp(prefix=".d2l-presegmented-", dir=working))
    legacy = staging / "legacy"
    legacy.mkdir()
    try:
        _write_atomic_bytes(
            legacy / AUTHORITATIVE_D2L_CAPTURE.source_file,
            source_bytes,
        )
        _write_atomic_bytes(legacy / "block_map.json", block_map_bytes)
        _write_atomic_bytes(legacy / "manifest.json", manifest_bytes)
        result = convert_d2l_presegmented_capture(legacy, staging / "bundle")
        return staging, result
    except D2lPresegmentedAdapterError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise SourceLifecycleError(
            "d2l_presegmented_bundle_invalid",
            str(exc),
            422,
        ) from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _capture_relative_path(adapter_identity_sha256: str) -> str:
    return (
        f"working/{PRESEGMENTED_CAPTURE_DIRECTORY}/"
        f"d2lps_{adapter_identity_sha256}"
    )


def _validate_existing_capture(
    root: Path,
    *,
    staged_root: Path,
    receipt_sha256: str,
    adapter_identity_sha256: str,
) -> Path:
    relative_path = _capture_relative_path(adapter_identity_sha256)
    capture = root / Path(*PurePosixPath(relative_path).parts)
    capture = _require_confined_existing(
        capture,
        root,
        owner="D2L pre-segmented capture",
    )
    if not capture.is_dir():
        raise SourceLifecycleError(
            "d2l_presegmented_capture_invalid",
            "D2L pre-segmented capture must be a directory.",
            409,
        )
    try:
        validated = validate_d2l_presegmented_output(
            capture,
            expected_receipt_sha256=receipt_sha256,
        )
    except D2lPresegmentedAdapterError as exc:
        raise SourceLifecycleError(
            "d2l_presegmented_capture_invalid",
            str(exc),
            409,
        ) from exc
    if validated.adapter_identity_sha256 != adapter_identity_sha256:
        raise SourceLifecycleError(
            "d2l_presegmented_capture_invalid",
            "D2L pre-segmented capture identity differs.",
            409,
        )
    if not _files_byte_identical(staged_root, capture):
        raise SourceLifecycleError(
            "d2l_presegmented_capture_collision",
            "Existing D2L pre-segmented capture differs from the upload.",
            409,
        )
    return capture


def _publish_d2l_presegmented_capture(
    root: Path,
    *,
    staged_root: Path,
    receipt_sha256: str,
    adapter_identity_sha256: str,
) -> tuple[Path, bool]:
    parent = _presegmented_capture_parent(root)
    if _path_exists(parent):
        _require_plain_path(parent, owner="D2L capture parent")
        if not parent.is_dir():
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                "D2L capture parent must be a directory.",
                409,
            )
    else:
        parent.mkdir(parents=True)
    final = parent / f"d2lps_{adapter_identity_sha256}"
    if _path_exists(final):
        return (
            _validate_existing_capture(
                root,
                staged_root=staged_root,
                receipt_sha256=receipt_sha256,
                adapter_identity_sha256=adapter_identity_sha256,
            ),
            False,
        )
    try:
        os.replace(staged_root, final)
    except OSError:
        if not _path_exists(final):
            raise
        return (
            _validate_existing_capture(
                root,
                staged_root=staged_root,
                receipt_sha256=receipt_sha256,
                adapter_identity_sha256=adapter_identity_sha256,
            ),
            False,
        )
    try:
        validated = validate_d2l_presegmented_output(
            final,
            expected_receipt_sha256=receipt_sha256,
        )
    except D2lPresegmentedAdapterError as exc:
        raise SourceLifecycleError(
            "d2l_presegmented_capture_invalid",
            str(exc),
            409,
        ) from exc
    if validated.adapter_identity_sha256 != adapter_identity_sha256:
        raise SourceLifecycleError(
            "d2l_presegmented_capture_invalid",
            "Published D2L pre-segmented capture identity differs.",
            409,
        )
    return final, True


def _ensure_d2l_presegmented_source(
    root: Path,
    source_bytes: bytes,
) -> tuple[Path, bool]:
    raw = root / "raw"
    if _path_exists(raw):
        _require_plain_path(raw, owner="raw source directory")
        if not raw.is_dir():
            raise SourceLifecycleError(
                "source_package_path_unsafe",
                "raw must be a directory.",
                409,
            )
    else:
        raw.mkdir(parents=True)
    entries = sorted(raw.iterdir(), key=lambda item: item.name.casefold())
    for entry in entries:
        _require_plain_path(entry, owner=f"raw source entry {entry.name}")
    source = raw / "source.md"
    if entries:
        if len(entries) != 1 or entries[0].name != source.name or not source.is_file():
            raise SourceLifecycleError(
                "d2l_presegmented_source_collision",
                "Raw source storage is not empty or does not contain the exact managed source.md.",
                409,
            )
        if source.read_bytes() != source_bytes:
            raise SourceLifecycleError(
                "d2l_presegmented_source_collision",
                "Existing managed source bytes differ from the D2L upload.",
                409,
            )
        return source.resolve(strict=True), False
    _write_atomic_bytes(source, source_bytes)
    if source.read_bytes() != source_bytes:
        raise SourceLifecycleError(
            "d2l_presegmented_source_collision",
            "Managed source differs after publication.",
            409,
        )
    return source.resolve(strict=True), True


def _validate_d2l_presegmented_reuse(
    root: Path,
    doc_id: str,
    *,
    state: dict[str, Any],
    staged_root: Path,
    receipt_sha256: str,
    adapter_identity_sha256: str,
    source_bytes: bytes,
) -> dict[str, Any]:
    if state.get("schema_version") != SOURCE_LIFECYCLE_VERSION:
        raise SourceLifecycleError(
            "d2l_presegmented_import_stale",
            "D2L pre-segmented import cannot replace a revised or finalized package.",
            409,
        )
    _require_pre_run_editable(state)
    status = _managed_status(root, doc_id=doc_id, state=state)
    source_path, source_format, source_sha256 = _server_source(root)
    if source_format != "markdown" or source_path.read_bytes() != source_bytes:
        raise SourceLifecycleError(
            "d2l_presegmented_import_stale",
            "Managed source differs from the D2L pre-segmented upload.",
            409,
        )
    capture = _validate_existing_capture(
        root,
        staged_root=staged_root,
        receipt_sha256=receipt_sha256,
        adapter_identity_sha256=adapter_identity_sha256,
    )
    normalized = normalize_presegmented_source(
        source_path,
        bundle_root=capture,
        doc_id=doc_id,
        source_language="en",
        target_language="vi",
        capture_relative_path=_capture_relative_path(adapter_identity_sha256),
    )
    # _managed_status has already validated the complete candidate and its
    # lifecycle binding. Reuse that evidence instead of running the complete
    # candidate validator a second time; the deterministic producer comparison
    # below is the additional D2L-specific check for this retry.
    candidate_root = _candidate_root_from_state(root, state)
    expected = {
        "document": normalized.document,
        "structure": normalized.structure_manifest,
        "normalization_receipt": normalized.receipt,
    }
    candidate_files = {
        "document": "document.json",
        "structure": "structure_manifest.json",
        "normalization_receipt": "normalization_receipt.json",
    }
    actual = {
        name: _read_json_object(
            candidate_root / candidate_files[name],
            owner=f"managed {name}",
        )
        for name in expected
    }
    if actual != expected:
        raise SourceLifecycleError(
            "d2l_presegmented_import_stale",
            "Managed package was not deterministically produced from this D2L capture.",
            409,
        )
    return {**status, "created": False, "reused": True}


def import_d2l_presegmented_source_package(
    project_path: str | Path,
    doc_id: str,
    *,
    source_bytes: bytes,
    block_map_bytes: bytes,
    manifest_bytes: bytes,
) -> dict[str, Any]:
    """Import the sealed D2L marked-source capture without Markdown re-segmentation."""

    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        staging, staged = _stage_d2l_presegmented_capture(
            root,
            source_bytes=source_bytes,
            block_map_bytes=block_map_bytes,
            manifest_bytes=manifest_bytes,
        )
        state_existed = _managed_state_exists(root)
        source_created = False
        capture_created = False
        capture: Path | None = None
        expected_capture = root / Path(
            *PurePosixPath(
                _capture_relative_path(staged.adapter_identity_sha256)
            ).parts
        )
        capture_existed_before = _path_exists(expected_capture)
        candidate_parent = _candidate_parent(root)
        if _path_exists(candidate_parent):
            _require_plain_path(candidate_parent, owner="candidate parent")
            if not candidate_parent.is_dir():
                raise SourceLifecycleError(
                    "source_package_path_unsafe",
                    "Candidate parent must be a directory.",
                    409,
                )
        candidate_entries_before = (
            {entry.name for entry in candidate_parent.iterdir()}
            if candidate_parent.is_dir()
            else set()
        )

        def rollback_new_artifacts() -> None:
            if state_existed:
                return
            for state_path in (_state_v2_path(root), _state_path(root)):
                if _path_exists(state_path) and not _is_reparse_point(state_path):
                    state_path.unlink()
            if candidate_parent.is_dir() and not _is_reparse_point(candidate_parent):
                for entry in list(candidate_parent.iterdir()):
                    if entry.name not in candidate_entries_before:
                        if entry.is_dir() and not _is_reparse_point(entry):
                            shutil.rmtree(entry, ignore_errors=True)
                        elif entry.is_file() and not _is_reparse_point(entry):
                            entry.unlink(missing_ok=True)
            if (
                not capture_existed_before
                and expected_capture.is_dir()
                and not _is_reparse_point(expected_capture)
            ):
                shutil.rmtree(expected_capture, ignore_errors=True)
            if source_created:
                source = root / "raw" / "source.md"
                if source.is_file() and not _is_reparse_point(source):
                    source.unlink()

        try:
            state = _read_authoritative_state(root, doc_id=doc_id)
            if state is not None:
                return _validate_d2l_presegmented_reuse(
                    root,
                    doc_id,
                    state=state,
                    staged_root=staged.output_root,
                    receipt_sha256=staged.receipt_sha256,
                    adapter_identity_sha256=staged.adapter_identity_sha256,
                    source_bytes=source_bytes,
                )
            _require_no_legacy_occupancy(root, doc_id=doc_id)
            source_path, source_created = _ensure_d2l_presegmented_source(
                root,
                source_bytes,
            )
            capture, capture_created = _publish_d2l_presegmented_capture(
                root,
                staged_root=staged.output_root,
                receipt_sha256=staged.receipt_sha256,
                adapter_identity_sha256=staged.adapter_identity_sha256,
            )

            def producer(path: str | Path, **options: Any) -> Any:
                return normalize_presegmented_source(
                    path,
                    bundle_root=capture,
                    doc_id=options["doc_id"],
                    source_language=options["source_language"],
                    target_language=options["target_language"],
                    capture_relative_path=_capture_relative_path(
                        staged.adapter_identity_sha256
                    ),
                    pandoc_executable=options.get("pandoc_executable"),
                    pdf_formula_detector_mode=options.get(
                        "pdf_formula_detector_mode", "disabled"
                    ),
                )

            result = _normalize_managed_source_package_locked(
                root,
                doc_id,
                normalize_fn=producer,
                write_fn=None,
            )
            state = _read_authoritative_state(root, doc_id=doc_id)
            if state is None:
                raise SourceLifecycleError(
                    "d2l_presegmented_import_failed",
                    "Managed lifecycle state was not published.",
                    409,
                )
            return {
                **result,
                "created": result["created"],
                "reused": result["reused"],
            }
        except SourceLifecycleError:
            rollback_new_artifacts()
            raise
        except (D2lPresegmentedAdapterError, OSError, ValueError) as exc:
            rollback_new_artifacts()
            raise SourceLifecycleError(
                "d2l_presegmented_import_failed",
                str(exc),
                422,
            ) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def normalize_managed_source_package(
    project_path: str | Path,
    doc_id: str,
    *,
    normalize_fn: Callable[..., Any] | None = None,
    write_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    root = _require_project_root(project_path)
    with _managed_source_mutation_guard(root):
        return _normalize_managed_source_package_locked(
            root,
            doc_id,
            normalize_fn=normalize_fn,
            write_fn=write_fn,
        )


def _normalize_managed_source_package_locked(
    root: Path,
    doc_id: str,
    *,
    normalize_fn: Callable[..., Any] | None,
    write_fn: Callable[..., Any] | None,
) -> dict[str, Any]:
    state = _read_authoritative_state(root, doc_id=doc_id)
    if state is not None:
        result = _managed_status(root, doc_id=doc_id, state=state)
        _require_pre_run_editable(state)
        return {**result, "created": False, "reused": True}
    _require_no_legacy_occupancy(root, doc_id=doc_id)

    source_path, source_format, source_sha256 = _server_source(root)
    working = root / "working"
    working.mkdir(parents=True, exist_ok=True)
    _require_plain_path(working, owner="working directory")
    candidates = _candidate_parent(root)
    if _path_exists(candidates):
        _require_plain_path(candidates, owner="candidate parent")
    candidates.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=".source-package-", dir=candidates))
    published = False
    try:
        producer = normalize_fn or normalize_source
        writer = write_fn or write_unified_normalization
        normalized = producer(
            source_path,
            doc_id=doc_id,
            source_language="en",
            target_language="vi",
            pandoc_executable=THESIS_PANDOC_EXE,
            pdf_formula_detector_mode="disabled",
        )
        writer(normalized, temporary_root)
        core = _load_core_package(
            temporary_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        report = build_draft_structure_report(
            core["document"],
            core["structure"],
            core["asset_manifest"],
            core["admitted_projection"],
            _project_state(doc_id),
            package_root=temporary_root,
        )
        validate_draft_structure_report_shape(report)
        _atomic_json_write(temporary_root / REPORT_FILENAME, report)
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
            raise SourceLifecycleError(
                "source_changed_during_normalization",
                "Source bytes changed while normalization was running.",
                409,
            )
        evidence = _validate_candidate(
            temporary_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        candidate_id = f"srcpkg_{evidence['tree']['tree_sha256']}"
        final_root = candidates / candidate_id
        reused = False
        if _path_exists(final_root):
            _require_plain_path(final_root, owner="source package candidate")
            existing = _validate_candidate(
                final_root,
                source_path=source_path,
                source_format=source_format,
                source_sha256=source_sha256,
                doc_id=doc_id,
            )
            if existing["tree"] != evidence["tree"] or not _files_byte_identical(
                temporary_root,
                final_root,
            ):
                raise SourceLifecycleError(
                    "source_package_candidate_collision",
                    "Content-addressed candidate exists with different bytes.",
                    409,
                )
            reused = True
        else:
            try:
                os.replace(temporary_root, final_root)
                published = True
            except OSError:
                if not final_root.is_dir():
                    raise
                existing = _validate_candidate(
                    final_root,
                    source_path=source_path,
                    source_format=source_format,
                    source_sha256=source_sha256,
                    doc_id=doc_id,
                )
                if existing["tree"] != evidence["tree"] or not _files_byte_identical(
                    temporary_root,
                    final_root,
                ):
                    raise SourceLifecycleError(
                        "source_package_candidate_collision",
                        "Concurrent candidate differs from generated bytes.",
                        409,
                    )
                reused = True
        if reused:
            shutil.rmtree(temporary_root)
        final_evidence = _validate_candidate(
            final_root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
        )
        lifecycle = _state_payload(
            project_path=root,
            source_path=source_path,
            source_format=source_format,
            source_sha256=source_sha256,
            doc_id=doc_id,
            candidate_root=final_root,
            evidence=final_evidence,
        )
        _require_no_legacy_occupancy(root, doc_id=doc_id)
        _atomic_json_write(_state_path(root), lifecycle)
        status = _managed_status(root, doc_id=doc_id, state=lifecycle)
        return {**status, "created": not reused, "reused": reused}
    except SourceLifecycleError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceLifecycleError(
            "source_package_normalization_failed",
            str(exc),
            422,
        ) from exc
    finally:
        if temporary_root.exists() and not published:
            shutil.rmtree(temporary_root, ignore_errors=True)


__all__ = [
    "CANDIDATE_DIRECTORY",
    "DECISION_DIRECTORY",
    "FINALIZATION_DIRECTORY",
    "HIERARCHY_DIRECTORY",
    "MANAGED_RUNTIME_MANIFEST_VERSION",
    "PUBLICATION_DIRECTORY",
    "RUN_START_DIRECTORY",
    "SOURCE_LIFECYCLE_VERSION",
    "SOURCE_LIFECYCLE_V2_VERSION",
    "SOURCE_PACKAGE_DECISION_VERSION",
    "SOURCE_PACKAGE_FINALIZATION_VERSION",
    "SOURCE_PACKAGE_PUBLICATION_VERSION",
    "SOURCE_PACKAGE_RUN_START_VERSION",
    "SOURCE_PACKAGE_REVISION_VERSION",
    "SOURCE_PACKAGE_STATUS_VERSION",
    "STATE_FILENAME",
    "STATE_V2_FILENAME",
    "SourceLifecycleError",
    "apply_managed_source_corrections",
    "apply_managed_source_hierarchy",
    "ensure_legacy_extract_allowed",
    "ensure_legacy_normalizer_allowed",
    "ensure_source_upload_allowed",
    "finalize_managed_source_package",
    "freeze_managed_source_for_run",
    "get_managed_runtime_context",
    "get_managed_runtime_status_context",
    "get_source_package_review",
    "get_source_package_status",
    "import_d2l_presegmented_source_package",
    "normalize_managed_source_package",
    "publish_managed_translation",
    "source_lifecycle_mutation_guard",
]
