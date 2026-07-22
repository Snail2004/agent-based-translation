from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATASET_FILES, THESIS_JOBS_ROOT
from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.document_loader import load_document
from pipeline.scripts.prepare_source import prepare_source
from services.source_lifecycle import (
    MANAGED_RUNTIME_MANIFEST_VERSION,
    SourceLifecycleError,
    freeze_managed_source_for_run,
    get_managed_runtime_context,
    get_managed_runtime_status_context,
    get_source_package_status,
    source_lifecycle_mutation_guard,
)
from services.structure_manifest import (
    STRUCTURE_MANIFEST_FILENAME,
    chapter_routing,
    read_structure_manifest,
)
from services.workspace import get_project_path


PROJECT_RUNTIME_CONTRACT_VERSION = "project_runtime_source_v1"
PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION = MANAGED_RUNTIME_MANIFEST_VERSION
SUPPORTED_PROFILES = ("technical_d2l_v1", "literary_v1")
_JOB_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class ProjectRuntimeError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _translate_lifecycle_error(exc: SourceLifecycleError) -> ProjectRuntimeError:
    return ProjectRuntimeError(exc.code, str(exc), exc.status)


def _managed_project_status(doc_id: str) -> dict[str, Any] | None:
    project_path = get_project_path(doc_id)
    try:
        status = get_source_package_status(project_path, doc_id)
    except SourceLifecycleError as exc:
        raise _translate_lifecycle_error(exc) from exc
    return status if status.get("managed") is True else None


def get_project_runtime_status(
    doc_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any]:
    try:
        status_context = get_managed_runtime_status_context(
            get_project_path(doc_id), doc_id
        )
    except SourceLifecycleError as exc:
        raise _translate_lifecycle_error(exc) from exc
    if status_context is None:
        return _get_legacy_project_runtime_status(doc_id, jobs_root=jobs_root)
    return _get_managed_project_runtime_status_summary(
        doc_id,
        status_context=status_context,
        jobs_root=jobs_root,
    )


def prepare_project_runtime(
    doc_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any]:
    managed_status = _managed_project_status(doc_id)
    if managed_status is None:
        return _prepare_legacy_project_runtime(doc_id, jobs_root=jobs_root)
    return _prepare_managed_project_runtime(
        doc_id,
        managed_status=managed_status,
        jobs_root=jobs_root,
    )


def _get_legacy_project_runtime_status(
    doc_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any]:
    source_state = _project_source_state(doc_id)
    source_path = source_state["document_path"]
    source_sha256 = source_state["document_sha256"]
    source_identity_sha256 = source_state["source_identity_sha256"]
    job_id = _job_id(doc_id, source_identity_sha256)
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(root, job_id)
    base = {
        "contract_version": PROJECT_RUNTIME_CONTRACT_VERSION,
        "project_id": doc_id,
        "job_id": job_id,
        "source_sha256": source_sha256,
        "source_identity_sha256": source_identity_sha256,
        "prepared": False,
        "profiles": list(SUPPORTED_PROFILES),
    }
    if not job_dir.exists():
        return base

    manifest = _validated_manifest(
        job_dir,
        doc_id=doc_id,
        job_id=job_id,
        source_sha256=source_sha256,
        source_identity_sha256=source_identity_sha256,
        structure_sha256=source_state["structure_sha256"],
    )
    return {
        **base,
        **manifest,
        "prepared": True,
        "runtime_db": str(job_dir / "memory.sqlite3"),
        "manifest_path": str(job_dir / "source_manifest.json"),
    }


def _prepare_legacy_project_runtime(
    doc_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any]:
    source_state = _project_source_state(doc_id)
    source_path = source_state["document_path"]
    source_identity_sha256 = source_state["source_identity_sha256"]
    job_id = _job_id(doc_id, source_identity_sha256)
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    job_dir = _job_dir(root, job_id)

    if job_dir.exists():
        return {**_get_legacy_project_runtime_status(doc_id, jobs_root=root), "created": False}

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{job_id}.", dir=root))
    try:
        snapshot_dir = temp_dir / "source_snapshot"
        snapshot_path = snapshot_dir / "document.json"
        provenance = prepare_source(source_path, snapshot_path)
        structure_snapshot_path = None
        if source_state["structure_path"] is not None:
            structure_snapshot_path = snapshot_dir / STRUCTURE_MANIFEST_FILENAME
            shutil.copy2(source_state["structure_path"], structure_snapshot_path)
        load_report = load_document(temp_dir / "memory.sqlite3", snapshot_path)
        document = json.loads(snapshot_path.read_text(encoding="utf-8"))
        manifest = _build_manifest(
            doc_id=doc_id,
            job_id=job_id,
            source_path=source_path,
            document=document,
            provenance=provenance,
            load_report=load_report.to_json_dict(),
            runtime_db=temp_dir / "memory.sqlite3",
            source_identity_sha256=source_identity_sha256,
            structure_manifest=source_state["structure_manifest"],
            structure_sha256=source_state["structure_sha256"],
        )
        _write_json_atomic(temp_dir / "source_manifest.json", manifest)

        try:
            os.replace(temp_dir, job_dir)
        except OSError:
            # Windows may report an existing destination as PermissionError rather
            # than FileExistsError. Reuse only when another writer really published
            # the same content-addressed job; status validation remains fail-closed.
            if not job_dir.exists():
                raise
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {**_get_legacy_project_runtime_status(doc_id, jobs_root=root), "created": False}

        return {**_get_legacy_project_runtime_status(doc_id, jobs_root=root), "created": True}
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _managed_context(doc_id: str) -> dict[str, Any]:
    try:
        return get_managed_runtime_context(get_project_path(doc_id), doc_id)
    except SourceLifecycleError as exc:
        raise _translate_lifecycle_error(exc) from exc


def _managed_job_identity(doc_id: str, managed_source: dict[str, Any]) -> tuple[str, str]:
    identity = canonical_json_sha256(managed_source)
    return _job_id(doc_id, identity), identity


def _managed_base(
    *,
    doc_id: str,
    managed_status: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    if context is None:
        return {
            "contract_version": PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION,
            "project_id": doc_id,
            "job_id": None,
            "source_identity_sha256": None,
            "managed_source": None,
            "lifecycle": managed_status["lifecycle"],
            "pipeline_run_count": managed_status["pipeline_run_count"],
            "prepare_allowed": False,
            "prepared": False,
            "profiles": list(SUPPORTED_PROFILES),
        }
    job_id, identity = _managed_job_identity(doc_id, context["managed_source"])
    return {
        "contract_version": PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION,
        "project_id": doc_id,
        "job_id": job_id,
        "source_identity_sha256": identity,
        "managed_source": copy.deepcopy(context["managed_source"]),
        "lifecycle": context["lifecycle"],
        "pipeline_run_count": context["pipeline_run_count"],
        "prepare_allowed": context["lifecycle"] == "finalized_pre_run",
        "prepared": False,
        "profiles": list(SUPPORTED_PROFILES),
    }


def _get_managed_project_runtime_status(
    doc_id: str,
    *,
    managed_status: dict[str, Any],
    jobs_root: str | Path | None,
) -> dict[str, Any]:
    if managed_status["lifecycle"] == "draft":
        return _managed_base(doc_id=doc_id, managed_status=managed_status, context=None)
    context = _managed_context(doc_id)
    base = _managed_base(doc_id=doc_id, managed_status=managed_status, context=context)
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(root, base["job_id"])
    if not job_dir.exists():
        if context["lifecycle"] == "run_started_frozen":
            raise ProjectRuntimeError(
                "managed_runtime_missing_after_freeze",
                "Frozen managed runtime cannot be recreated after the first run.",
                409,
            )
        return base
    manifest = _validated_managed_manifest(job_dir, context=context)
    return {
        **base,
        **manifest,
        "prepared": True,
        "runtime_db": str(job_dir / "memory.sqlite3"),
        "manifest_path": str(job_dir / "source_manifest.json"),
    }


def _get_managed_project_runtime_status_summary(
    doc_id: str,
    *,
    status_context: dict[str, Any],
    jobs_root: str | Path | None,
) -> dict[str, Any]:
    managed_source = status_context.get("managed_source")
    if managed_source is None:
        return _managed_base(
            doc_id=doc_id,
            managed_status=status_context,
            context=None,
        )
    base = _managed_base(
        doc_id=doc_id,
        managed_status=status_context,
        context=status_context,
    )
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(root, base["job_id"])
    if not job_dir.exists():
        if status_context["lifecycle"] == "run_started_frozen":
            raise ProjectRuntimeError(
                "managed_runtime_missing_after_freeze",
                "Frozen managed runtime cannot be recreated after the first run.",
                409,
            )
        return base

    manifest = _validated_managed_status_manifest(job_dir, context=status_context)
    return {
        **base,
        **manifest,
        "prepared": True,
        "runtime_db": str(job_dir / "memory.sqlite3"),
        "manifest_path": str(job_dir / "source_manifest.json"),
    }


def _prepare_managed_project_runtime(
    doc_id: str,
    *,
    managed_status: dict[str, Any],
    jobs_root: str | Path | None,
) -> dict[str, Any]:
    context = _managed_context(doc_id)
    if context["lifecycle"] == "run_started_frozen":
        return {
            **_get_managed_project_runtime_status(
                doc_id, managed_status=managed_status, jobs_root=jobs_root
            ),
            "created": False,
        }
    if context["lifecycle"] != "finalized_pre_run":
        raise ProjectRuntimeError(
            "source_package_not_finalized",
            "Managed runtime preparation requires finalized_pre_run state.",
            409,
        )
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    job_id, source_identity = _managed_job_identity(doc_id, context["managed_source"])
    job_dir = _job_dir(root, job_id)
    project_path = Path(context["project_path"])
    with source_lifecycle_mutation_guard(project_path):
        # Re-read under the same OS lock before publishing runtime bytes.
        context = _managed_context(doc_id)
        if context["lifecycle"] != "finalized_pre_run":
            raise ProjectRuntimeError(
                "source_package_not_finalized",
                "Managed source package changed before runtime preparation.",
                409,
            )
        current_job_id, current_identity = _managed_job_identity(
            doc_id, context["managed_source"]
        )
        if current_job_id != job_id or current_identity != source_identity:
            raise ProjectRuntimeError(
                "runtime_source_changed",
                "Managed source identity changed before runtime preparation.",
                409,
            )
        if job_dir.exists():
            manifest = _validated_managed_manifest(job_dir, context=context)
            return {
                **_managed_base(
                    doc_id=doc_id, managed_status=managed_status, context=context
                ),
                **manifest,
                "prepared": True,
                "created": False,
                "runtime_db": str(job_dir / "memory.sqlite3"),
                "manifest_path": str(job_dir / "source_manifest.json"),
            }
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{job_id}.", dir=root))
        try:
            package_snapshot = temp_dir / "source_package_snapshot"
            shutil.copytree(Path(context["candidate_root"]), package_snapshot)
            package_tree = _file_tree(package_snapshot)
            if (
                package_tree["tree_sha256"]
                != context["state"]["candidate"]["tree_sha256"]
                or package_tree["file_count"]
                != context["state"]["candidate"]["file_count"]
            ):
                raise ProjectRuntimeError(
                    "managed_runtime_snapshot_mismatch",
                    "Managed package snapshot differs from its finalized candidate.",
                    409,
                )
            lifecycle_snapshot = temp_dir / "lifecycle_snapshot"
            lifecycle_snapshot.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                context["state_path"],
                lifecycle_snapshot / "source_lifecycle_v2.json",
            )
            shutil.copy2(
                context["finalization_path"],
                lifecycle_snapshot / "finalization.json",
            )
            if context["hierarchy_overlay"] is not None:
                shutil.copy2(
                    context["hierarchy_path"],
                    lifecycle_snapshot / "hierarchy_overlay.json",
                )
            document_path = package_snapshot / "document.json"
            load_report = load_document(temp_dir / "memory.sqlite3", document_path)
            document = json.loads(document_path.read_text(encoding="utf-8"))
            manifest = _build_managed_manifest(
                doc_id=doc_id,
                job_id=job_id,
                document=document,
                context=context,
                package_tree=package_tree,
                load_report=load_report.to_json_dict(),
                runtime_db=temp_dir / "memory.sqlite3",
            )
            _write_json_atomic(temp_dir / "source_manifest.json", manifest)
            _validated_managed_manifest(temp_dir, context=context)
            try:
                os.replace(temp_dir, job_dir)
            except OSError:
                if not job_dir.exists():
                    raise
                shutil.rmtree(temp_dir, ignore_errors=True)
                manifest = _validated_managed_manifest(job_dir, context=context)
                created = False
            else:
                manifest = _validated_managed_manifest(job_dir, context=context)
                created = True
            return {
                **_managed_base(
                    doc_id=doc_id, managed_status=managed_status, context=context
                ),
                **manifest,
                "prepared": True,
                "created": created,
                "runtime_db": str(job_dir / "memory.sqlite3"),
                "manifest_path": str(job_dir / "source_manifest.json"),
            }
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise


def _canonical_document_path(doc_id: str) -> Path:
    path = get_project_path(doc_id) / "canonical" / DATASET_FILES["document"]
    if not path.is_file():
        raise ProjectRuntimeError(
            "missing_canonical_document",
            "Project has no canonical/document.json. Extract the source first.",
            404,
        )
    return path


def _project_source_state(doc_id: str) -> dict[str, Any]:
    project_path = get_project_path(doc_id)
    document_path = _canonical_document_path(doc_id)
    try:
        document = json.loads(document_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRuntimeError(
            "canonical_document_invalid",
            f"Invalid canonical/document.json: {exc}",
            409,
        ) from exc

    document_sha256 = _sha256_file(document_path)
    try:
        structure_manifest = read_structure_manifest(project_path, document)
    except ValueError as exc:
        raise ProjectRuntimeError(
            "structure_manifest_invalid",
            str(exc),
            409,
        ) from exc

    structure_path = (
        project_path / "canonical" / STRUCTURE_MANIFEST_FILENAME
        if structure_manifest is not None
        else None
    )
    structure_sha256 = _sha256_file(structure_path) if structure_path is not None else None
    if structure_sha256 is None:
        source_identity_sha256 = document_sha256
    else:
        source_identity_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "document_sha256": document_sha256,
                    "structure_manifest_sha256": structure_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return {
        "document": document,
        "document_path": document_path,
        "document_sha256": document_sha256,
        "source_identity_sha256": source_identity_sha256,
        "structure_manifest": structure_manifest,
        "structure_path": structure_path,
        "structure_sha256": structure_sha256,
    }


def _job_id(doc_id: str, source_sha256: str) -> str:
    component = _JOB_COMPONENT_RE.sub("_", doc_id).strip("._-") or "project"
    return f"src_{component[:48]}_{source_sha256[:12]}"


def _job_dir(root: Path, job_id: str) -> Path:
    path = (root / job_id).resolve()
    if root not in path.parents:
        raise ProjectRuntimeError("runtime_path_escape", "Runtime path escapes THESIS_JOBS_ROOT.", 400)
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_tree(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise ProjectRuntimeError(
            "managed_runtime_snapshot_missing",
            "Managed source-package snapshot is unavailable.",
            409,
        )
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ProjectRuntimeError(
                "managed_runtime_path_unsafe",
                "Managed runtime snapshot contains a symbolic link.",
                409,
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProjectRuntimeError(
                "managed_runtime_path_unsafe",
                "Managed runtime snapshot contains an unsupported entry.",
                409,
            )
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return {
        "tree_sha256": canonical_json_sha256(rows),
        "file_count": len(rows),
        "rows": rows,
    }


def _build_managed_manifest(
    *,
    doc_id: str,
    job_id: str,
    document: dict[str, Any],
    context: dict[str, Any],
    package_tree: dict[str, Any],
    load_report: dict[str, Any],
    runtime_db: Path,
) -> dict[str, Any]:
    structure = context["evidence"]["structure"]
    chapters = _chapter_rows(document, structure)
    hierarchy = context["managed_source"]["hierarchy"]
    hierarchy_snapshot = None
    if hierarchy["sha256"] is not None:
        hierarchy_snapshot = {
            "path": "lifecycle_snapshot/hierarchy_overlay.json",
            "sha256": _sha256_file(
                runtime_db.parent / "lifecycle_snapshot" / "hierarchy_overlay.json"
            ),
            "payload_sha256": hierarchy["sha256"],
        }
    manifest = {
        "contract_version": PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION,
        "job_id": job_id,
        "project_id": doc_id,
        "document_doc_id": str(document.get("doc_id") or doc_id),
        "source_document": "source_package_snapshot/document.json",
        "source_snapshot": "source_package_snapshot/document.json",
        "original_sha256": context["managed_source"]["package"]["document"]["sha256"],
        "source_identity_sha256": canonical_json_sha256(context["managed_source"]),
        "stripped_sha256": _sha256_file(
            runtime_db.parent / "source_package_snapshot" / "document.json"
        ),
        "initial_runtime_db_sha256": _sha256_file(runtime_db),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profiles": list(SUPPORTED_PROFILES),
        "chapters": chapters,
        "chapter_count": len(chapters),
        "block_count": sum(row["block_count"] for row in chapters),
        "structure_manifest": {
            "path": "source_package_snapshot/structure_manifest.json",
            "sha256": context["managed_source"]["package"]["structure"]["sha256"],
            "schema_version": structure.get("schema_version"),
            "structure_sha256": structure.get("structure_sha256"),
        },
        "translatable_chapter_ids": list(
            structure.get("translatable_chapter_ids") or []
        ),
        "review_required_chapter_ids": list(
            structure.get("review_required_chapter_ids") or []
        ),
        "load_report": load_report,
        "managed_source": copy.deepcopy(context["managed_source"]),
        "source_package_snapshot": {
            "path": "source_package_snapshot",
            **copy.deepcopy(package_tree),
        },
        "lifecycle_snapshot": {
            "path": "lifecycle_snapshot/source_lifecycle_v2.json",
            "sha256": _sha256_file(
                runtime_db.parent / "lifecycle_snapshot" / "source_lifecycle_v2.json"
            ),
            "payload_sha256": context["state"]["integrity"]["payload_sha256"],
        },
        "finalization_snapshot": {
            "path": "lifecycle_snapshot/finalization.json",
            "sha256": _sha256_file(
                runtime_db.parent / "lifecycle_snapshot" / "finalization.json"
            ),
            "payload_sha256": context["managed_source"]["finalization"]["sha256"],
        },
        "hierarchy_snapshot": hierarchy_snapshot,
    }
    manifest["manifest_payload_sha256"] = canonical_json_sha256(manifest)
    return manifest


def _validated_managed_manifest(
    job_dir: Path,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = job_dir / "source_manifest.json"
    package_snapshot = job_dir / "source_package_snapshot"
    lifecycle_snapshot = job_dir / "lifecycle_snapshot"
    db_path = job_dir / "memory.sqlite3"
    required = [
        manifest_path,
        package_snapshot / "document.json",
        package_snapshot / "structure_manifest.json",
        package_snapshot / "asset_manifest.json",
        package_snapshot / "admitted_projection_v1.json",
        package_snapshot / "normalization_receipt.json",
        package_snapshot / "draft_structure_report.json",
        lifecycle_snapshot / "source_lifecycle_v2.json",
        lifecycle_snapshot / "finalization.json",
        db_path,
    ]
    if any(not path.is_file() for path in required):
        raise ProjectRuntimeError(
            "managed_runtime_incomplete",
            "Managed runtime snapshot is incomplete.",
            409,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRuntimeError(
            "runtime_manifest_invalid", f"Invalid managed runtime manifest: {exc}", 409
        ) from exc
    job_id, identity = _managed_job_identity(
        context["doc_id"], context["managed_source"]
    )
    expected = {
        "contract_version": PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION,
        "job_id": job_id,
        "project_id": context["doc_id"],
        "source_identity_sha256": identity,
        "managed_source": context["managed_source"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ProjectRuntimeError(
                "runtime_manifest_mismatch",
                f"Managed runtime manifest {key} differs from the finalized package.",
                409,
            )
    payload_hash = manifest.get("manifest_payload_sha256")
    if payload_hash != canonical_json_sha256(
        {key: value for key, value in manifest.items() if key != "manifest_payload_sha256"}
    ):
        raise ProjectRuntimeError(
            "runtime_manifest_tampered",
            "Managed runtime manifest payload hash differs.",
            409,
        )
    package_tree = _file_tree(package_snapshot)
    expected_tree = manifest.get("source_package_snapshot")
    if not isinstance(expected_tree, dict) or expected_tree != {
        "path": "source_package_snapshot",
        **package_tree,
    }:
        raise ProjectRuntimeError(
            "managed_runtime_snapshot_tampered",
            "Managed source-package snapshot differs from the runtime manifest.",
            409,
        )
    if (
        package_tree["tree_sha256"]
        != context["managed_source"]["candidate"]["tree_sha256"]
        or package_tree["file_count"]
        != context["managed_source"]["candidate"]["file_count"]
    ):
        raise ProjectRuntimeError(
            "managed_runtime_snapshot_mismatch",
            "Managed source-package snapshot differs from the finalized candidate.",
            409,
        )
    snapshot_checks = {
        "lifecycle_snapshot": (
            lifecycle_snapshot / "source_lifecycle_v2.json",
            context["managed_source"]["state_sha256"],
        ),
        "finalization_snapshot": (
            lifecycle_snapshot / "finalization.json",
            context["managed_source"]["finalization"]["sha256"],
        ),
    }
    hierarchy_sha = context["managed_source"]["hierarchy"]["sha256"]
    if hierarchy_sha is None:
        if manifest.get("hierarchy_snapshot") is not None:
            raise ProjectRuntimeError(
                "runtime_hierarchy_mismatch",
                "Managed runtime unexpectedly contains a hierarchy snapshot.",
                409,
            )
    else:
        hierarchy_path = lifecycle_snapshot / "hierarchy_overlay.json"
        if not hierarchy_path.is_file():
            raise ProjectRuntimeError(
                "runtime_hierarchy_incomplete",
                "Managed hierarchy snapshot is unavailable.",
                409,
            )
        snapshot_checks["hierarchy_snapshot"] = (hierarchy_path, hierarchy_sha)
    for name, (path, payload_sha) in snapshot_checks.items():
        row = manifest.get(name)
        expected_path = {
            "lifecycle_snapshot": "lifecycle_snapshot/source_lifecycle_v2.json",
            "finalization_snapshot": "lifecycle_snapshot/finalization.json",
            "hierarchy_snapshot": "lifecycle_snapshot/hierarchy_overlay.json",
        }[name]
        if not isinstance(row, dict) or row != {
            "path": expected_path,
            "sha256": _sha256_file(path),
            "payload_sha256": payload_sha,
        }:
            raise ProjectRuntimeError(
                "managed_runtime_lineage_tampered",
                f"Managed runtime {name} differs from its finalized identity.",
                409,
            )
    if (
        context["lifecycle"] != "run_started_frozen"
        and _sha256_file(db_path) != manifest.get("initial_runtime_db_sha256")
    ):
        raise ProjectRuntimeError(
            "runtime_db_tampered",
            "Managed runtime database differs from its initial manifest hash.",
            409,
        )
    return manifest


def _validated_managed_status_manifest(
    job_dir: Path,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Validate sealed runtime status evidence without walking package bytes."""

    manifest_path = job_dir / "source_manifest.json"
    db_path = job_dir / "memory.sqlite3"
    package_snapshot = job_dir / "source_package_snapshot"
    lifecycle_snapshot = job_dir / "lifecycle_snapshot"
    if not manifest_path.is_file():
        raise ProjectRuntimeError(
            "source_package_runtime_missing",
            "Managed runtime manifest is unavailable.",
            409,
        )
    required = [
        db_path,
        package_snapshot / "document.json",
        package_snapshot / "structure_manifest.json",
        package_snapshot / "asset_manifest.json",
        package_snapshot / "admitted_projection_v1.json",
        package_snapshot / "normalization_receipt.json",
        package_snapshot / "draft_structure_report.json",
        lifecycle_snapshot / "source_lifecycle_v2.json",
        lifecycle_snapshot / "finalization.json",
    ]
    if any(not path.is_file() for path in required):
        raise ProjectRuntimeError(
            "managed_runtime_incomplete",
            "Managed runtime snapshot is incomplete.",
            409,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRuntimeError(
            "runtime_manifest_invalid", f"Invalid managed runtime manifest: {exc}", 409
        ) from exc
    if not isinstance(manifest, dict):
        raise ProjectRuntimeError(
            "runtime_manifest_invalid", "Managed runtime manifest must be an object.", 409
        )

    job_id, identity = _managed_job_identity(
        context["doc_id"], context["managed_source"]
    )
    expected = {
        "contract_version": PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION,
        "job_id": job_id,
        "project_id": context["doc_id"],
        "source_identity_sha256": identity,
        "managed_source": context["managed_source"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ProjectRuntimeError(
                "runtime_manifest_mismatch",
                f"Managed runtime manifest {key} differs from the finalized package.",
                409,
            )
    payload_hash = manifest.get("manifest_payload_sha256")
    if payload_hash != canonical_json_sha256(
        {
            key: value
            for key, value in manifest.items()
            if key != "manifest_payload_sha256"
        }
    ):
        raise ProjectRuntimeError(
            "runtime_manifest_tampered",
            "Managed runtime manifest payload hash differs.",
            409,
        )

    if (
        context["lifecycle"] != "run_started_frozen"
        and _sha256_file(db_path) != manifest.get("initial_runtime_db_sha256")
    ):
        raise ProjectRuntimeError(
            "runtime_db_tampered",
            "Managed runtime database differs from its initial manifest hash.",
            409,
        )

    snapshot = manifest.get("source_package_snapshot")
    candidate = context["managed_source"]["candidate"]
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("path") != "source_package_snapshot"
        or snapshot.get("tree_sha256") != candidate["tree_sha256"]
        or snapshot.get("file_count") != candidate["file_count"]
        or not isinstance(snapshot.get("rows"), list)
        or len(snapshot["rows"]) != candidate["file_count"]
    ):
        raise ProjectRuntimeError(
            "managed_runtime_snapshot_mismatch",
            "Managed source-package snapshot identity differs from the finalized candidate.",
            409,
        )

    snapshot_checks = {
        "lifecycle_snapshot": (
            lifecycle_snapshot / "source_lifecycle_v2.json",
            context["managed_source"]["state_sha256"],
        ),
        "finalization_snapshot": (
            lifecycle_snapshot / "finalization.json",
            context["managed_source"]["finalization"]["sha256"],
        ),
    }
    hierarchy_sha = context["managed_source"]["hierarchy"]["sha256"]
    if hierarchy_sha is None:
        if manifest.get("hierarchy_snapshot") is not None:
            raise ProjectRuntimeError(
                "runtime_hierarchy_mismatch",
                "Managed runtime unexpectedly contains a hierarchy snapshot.",
                409,
            )
    else:
        hierarchy_path = lifecycle_snapshot / "hierarchy_overlay.json"
        if not hierarchy_path.is_file():
            raise ProjectRuntimeError(
                "runtime_hierarchy_incomplete",
                "Managed hierarchy snapshot is unavailable.",
                409,
            )
        snapshot_checks["hierarchy_snapshot"] = (hierarchy_path, hierarchy_sha)
    for name, (path, payload_sha) in snapshot_checks.items():
        row = manifest.get(name)
        expected_path = {
            "lifecycle_snapshot": "lifecycle_snapshot/source_lifecycle_v2.json",
            "finalization_snapshot": "lifecycle_snapshot/finalization.json",
            "hierarchy_snapshot": "lifecycle_snapshot/hierarchy_overlay.json",
        }[name]
        if not isinstance(row, dict) or row != {
            "path": expected_path,
            "sha256": _sha256_file(path),
            "payload_sha256": payload_sha,
        }:
            raise ProjectRuntimeError(
                "managed_runtime_lineage_tampered",
                f"Managed runtime {name} differs from its finalized identity.",
                409,
            )

    profiles = manifest.get("profiles")
    chapter_count = manifest.get("chapter_count")
    block_count = manifest.get("block_count")
    if (
        not isinstance(profiles, list)
        or any(not isinstance(value, str) or not value for value in profiles)
        or isinstance(chapter_count, bool)
        or not isinstance(chapter_count, int)
        or chapter_count < 0
        or isinstance(block_count, bool)
        or not isinstance(block_count, int)
        or block_count < 0
    ):
        raise ProjectRuntimeError(
            "runtime_manifest_invalid",
            "Managed runtime status fields are invalid.",
            409,
        )
    return {
        "contract_version": manifest["contract_version"],
        "project_id": manifest["project_id"],
        "job_id": manifest["job_id"],
        "source_identity_sha256": manifest["source_identity_sha256"],
        "profiles": copy.deepcopy(profiles),
        "chapter_count": chapter_count,
        "block_count": block_count,
    }


def freeze_managed_runtime_for_run(
    job_id: str,
    run_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any] | None:
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(root, job_id)
    manifest_path = job_dir / "source_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRuntimeError(
            "runtime_manifest_invalid", f"Invalid runtime manifest: {exc}", 409
        ) from exc
    if manifest.get("contract_version") != PROJECT_RUNTIME_MANAGED_CONTRACT_VERSION:
        return None
    doc_id = manifest.get("project_id")
    if not isinstance(doc_id, str) or not doc_id:
        raise ProjectRuntimeError(
            "runtime_manifest_invalid",
            "Managed runtime manifest project_id is invalid.",
            409,
        )
    context = _managed_context(doc_id)
    _validated_managed_manifest(job_dir, context=context)
    try:
        return freeze_managed_source_for_run(
            get_project_path(doc_id),
            doc_id,
            job_id=job_id,
            run_id=run_id,
            runtime_manifest_path=manifest_path,
        )
    except SourceLifecycleError as exc:
        raise _translate_lifecycle_error(exc) from exc


def _chapter_rows(
    document: dict[str, Any],
    structure_manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    routing = chapter_routing(structure_manifest)
    for index, chapter in enumerate(document.get("chapters") or []):
        row = {
            "chapter_id": str(chapter.get("chapter_id") or ""),
            "order_index": int(chapter.get("order_index") or index + 1),
            "title": str(chapter.get("title") or chapter.get("chapter_id") or ""),
            "block_count": len(chapter.get("blocks") or []),
        }
        row.update(routing.get(row["chapter_id"], {}))
        rows.append(row)
    return rows


def _build_manifest(
    *,
    doc_id: str,
    job_id: str,
    source_path: Path,
    document: dict[str, Any],
    provenance: dict[str, str],
    load_report: dict[str, Any],
    runtime_db: Path,
    source_identity_sha256: str,
    structure_manifest: dict[str, Any] | None,
    structure_sha256: str | None,
) -> dict[str, Any]:
    chapters = _chapter_rows(document, structure_manifest)
    return {
        "contract_version": PROJECT_RUNTIME_CONTRACT_VERSION,
        "job_id": job_id,
        "project_id": doc_id,
        "document_doc_id": str(document.get("doc_id") or doc_id),
        "source_document": f"projects/{doc_id}/canonical/{source_path.name}",
        "source_snapshot": "source_snapshot/document.json",
        "original_sha256": provenance["original_sha256"],
        "source_identity_sha256": source_identity_sha256,
        "stripped_sha256": provenance["stripped_sha256"],
        "initial_runtime_db_sha256": _sha256_file(runtime_db),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profiles": list(SUPPORTED_PROFILES),
        "chapters": chapters,
        "chapter_count": len(chapters),
        "block_count": sum(row["block_count"] for row in chapters),
        "structure_manifest": (
            {
                "path": f"source_snapshot/{STRUCTURE_MANIFEST_FILENAME}",
                "sha256": structure_sha256,
                "schema_version": structure_manifest.get("schema_version"),
                "structure_sha256": structure_manifest.get("structure_sha256"),
            }
            if structure_manifest is not None
            else None
        ),
        "translatable_chapter_ids": list(
            (structure_manifest or {}).get("translatable_chapter_ids") or [
                row["chapter_id"] for row in chapters
            ]
        ),
        "review_required_chapter_ids": list(
            (structure_manifest or {}).get("review_required_chapter_ids") or []
        ),
        "load_report": load_report,
    }


def _validated_manifest(
    job_dir: Path,
    *,
    doc_id: str,
    job_id: str,
    source_sha256: str,
    source_identity_sha256: str,
    structure_sha256: str | None,
) -> dict[str, Any]:
    manifest_path = job_dir / "source_manifest.json"
    snapshot_path = job_dir / "source_snapshot" / "document.json"
    db_path = job_dir / "memory.sqlite3"
    if not manifest_path.is_file() or not snapshot_path.is_file() or not db_path.is_file():
        raise ProjectRuntimeError(
            "runtime_incomplete",
            f"Runtime job {job_id} is incomplete; refusing to reuse it.",
            409,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRuntimeError("runtime_manifest_invalid", f"Invalid runtime manifest: {exc}", 409) from exc

    expected = {
        "contract_version": PROJECT_RUNTIME_CONTRACT_VERSION,
        "job_id": job_id,
        "project_id": doc_id,
        "original_sha256": source_sha256,
        "source_identity_sha256": source_identity_sha256,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ProjectRuntimeError(
                "runtime_manifest_mismatch",
                f"Runtime manifest {key} does not match the current project source.",
                409,
            )
    if _sha256_file(snapshot_path) != manifest.get("stripped_sha256"):
        raise ProjectRuntimeError(
            "runtime_snapshot_tampered",
            "Runtime source snapshot hash does not match its manifest.",
            409,
        )
    stored_structure = manifest.get("structure_manifest")
    if structure_sha256 is None:
        if stored_structure is not None:
            raise ProjectRuntimeError(
                "runtime_structure_mismatch",
                "Runtime expects a structure manifest that the project no longer has.",
                409,
            )
    else:
        structure_snapshot = job_dir / "source_snapshot" / STRUCTURE_MANIFEST_FILENAME
        if not isinstance(stored_structure, dict) or not structure_snapshot.is_file():
            raise ProjectRuntimeError(
                "runtime_structure_incomplete",
                "Runtime structure snapshot is missing.",
                409,
            )
        if stored_structure.get("sha256") != structure_sha256:
            raise ProjectRuntimeError(
                "runtime_structure_mismatch",
                "Runtime structure manifest does not match the current project source.",
                409,
            )
        if _sha256_file(structure_snapshot) != structure_sha256:
            raise ProjectRuntimeError(
                "runtime_structure_tampered",
                "Runtime structure snapshot hash does not match its manifest.",
                409,
            )
    return manifest


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
