from __future__ import annotations

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
from pipeline.ingest.document_loader import load_document
from pipeline.scripts.prepare_source import prepare_source
from services.workspace import get_project_path


PROJECT_RUNTIME_CONTRACT_VERSION = "project_runtime_source_v1"
SUPPORTED_PROFILES = ("technical_d2l_v1", "literary_v1")
_JOB_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class ProjectRuntimeError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def get_project_runtime_status(
    doc_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any]:
    source_path = _canonical_document_path(doc_id)
    source_sha256 = _sha256_file(source_path)
    job_id = _job_id(doc_id, source_sha256)
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(root, job_id)
    base = {
        "contract_version": PROJECT_RUNTIME_CONTRACT_VERSION,
        "project_id": doc_id,
        "job_id": job_id,
        "source_sha256": source_sha256,
        "prepared": False,
        "profiles": list(SUPPORTED_PROFILES),
    }
    if not job_dir.exists():
        return base

    manifest = _validated_manifest(job_dir, doc_id=doc_id, job_id=job_id, source_sha256=source_sha256)
    return {
        **base,
        **manifest,
        "prepared": True,
        "runtime_db": str(job_dir / "memory.sqlite3"),
        "manifest_path": str(job_dir / "source_manifest.json"),
    }


def prepare_project_runtime(
    doc_id: str,
    *,
    jobs_root: str | Path | None = None,
) -> dict[str, Any]:
    source_path = _canonical_document_path(doc_id)
    source_sha256 = _sha256_file(source_path)
    job_id = _job_id(doc_id, source_sha256)
    root = Path(jobs_root or THESIS_JOBS_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    job_dir = _job_dir(root, job_id)

    if job_dir.exists():
        return {**get_project_runtime_status(doc_id, jobs_root=root), "created": False}

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{job_id}.", dir=root))
    try:
        snapshot_dir = temp_dir / "source_snapshot"
        snapshot_path = snapshot_dir / "document.json"
        provenance = prepare_source(source_path, snapshot_path)
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
            return {**get_project_runtime_status(doc_id, jobs_root=root), "created": False}

        return {**get_project_runtime_status(doc_id, jobs_root=root), "created": True}
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


def _chapter_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, chapter in enumerate(document.get("chapters") or []):
        rows.append({
            "chapter_id": str(chapter.get("chapter_id") or ""),
            "order_index": int(chapter.get("order_index") or index + 1),
            "title": str(chapter.get("title") or chapter.get("chapter_id") or ""),
            "block_count": len(chapter.get("blocks") or []),
        })
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
) -> dict[str, Any]:
    chapters = _chapter_rows(document)
    return {
        "contract_version": PROJECT_RUNTIME_CONTRACT_VERSION,
        "job_id": job_id,
        "project_id": doc_id,
        "document_doc_id": str(document.get("doc_id") or doc_id),
        "source_document": f"projects/{doc_id}/canonical/{source_path.name}",
        "source_snapshot": "source_snapshot/document.json",
        "original_sha256": provenance["original_sha256"],
        "stripped_sha256": provenance["stripped_sha256"],
        "initial_runtime_db_sha256": _sha256_file(runtime_db),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profiles": list(SUPPORTED_PROFILES),
        "chapters": chapters,
        "chapter_count": len(chapters),
        "block_count": sum(row["block_count"] for row in chapters),
        "load_report": load_report,
    }


def _validated_manifest(
    job_dir: Path,
    *,
    doc_id: str,
    job_id: str,
    source_sha256: str,
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
    return manifest


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
