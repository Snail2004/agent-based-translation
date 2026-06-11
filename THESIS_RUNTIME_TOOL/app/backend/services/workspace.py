import re
import shutil
from pathlib import Path
from typing import Any

from config import DATASET_FILES, PROJECT_SUBDIRS, PROJECTS_ROOT, SAMPLE_ROOT, TRANSLATION_REVIEW_TEMPLATE
from services.dataset_io import atomic_write_bytes, default_review_state, read_json, write_json_atomic


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ProjectError(ValueError):
    pass


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECTS_ROOT.parent))
    except ValueError:
        return str(path)


def validate_doc_id(doc_id: str) -> str:
    if not PROJECT_ID_RE.match(doc_id):
        raise ProjectError("Invalid doc_id. Use letters, numbers, dot, dash, or underscore.")
    return doc_id


def get_project_path(doc_id: str) -> Path:
    validate_doc_id(doc_id)
    root = PROJECTS_ROOT.resolve()
    path = (root / doc_id).resolve()
    if root not in path.parents and path != root:
        raise ProjectError("Project path escapes the workspace root.")
    return path


def ensure_project_dirs(doc_id: str) -> dict[str, Path]:
    project_path = get_project_path(doc_id)
    dirs = {"project": project_path}
    for name in PROJECT_SUBDIRS:
        dirs[name] = project_path / name
        dirs[name].mkdir(parents=True, exist_ok=True)
    return dirs


def ensure_working_files(project_path: Path, document: dict[str, Any] | None = None) -> None:
    working = project_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    (working / "jobs").mkdir(parents=True, exist_ok=True)
    drafts_path = working / "drafts.json"
    if not drafts_path.exists():
        write_json_atomic(drafts_path, {"references": {}})
    review_log = working / "translation_review_log.csv"
    if not review_log.exists() and TRANSLATION_REVIEW_TEMPLATE.exists():
        shutil.copy2(TRANSLATION_REVIEW_TEMPLATE, review_log)
    if document is not None:
        review_path = working / "review_state.json"
        if not review_path.exists():
            write_json_atomic(review_path, default_review_state(document))


def _project_meta_path(project_path: Path) -> Path:
    return project_path / "working" / "project_meta.json"


def read_project_meta(project_path: Path) -> dict[str, Any]:
    path = _project_meta_path(project_path)
    if not path.exists():
        return {"doc_id": project_path.name, "metadata": {}, "note": "", "status": "created"}
    meta = read_json(path)
    meta.setdefault("doc_id", project_path.name)
    meta.setdefault("metadata", {})
    meta.setdefault("note", "")
    return meta


def write_project_meta(project_path: Path, meta: dict[str, Any]) -> None:
    working = project_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_project_meta_path(project_path), meta)


def create_project_shell(doc_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    project_path = get_project_path(doc_id)
    if project_path.exists():
        raise ProjectError(f"Project already exists: {doc_id}")
    dirs = ensure_project_dirs(doc_id)
    meta = {"doc_id": doc_id, "metadata": metadata or {}, "note": "", "status": "created"}
    write_project_meta(dirs["project"], meta)
    ensure_working_files(project_path)
    return project_file_state(doc_id)


def seed_project_from_sample(doc_id: str = "gold_demo_01") -> None:
    dirs = ensure_project_dirs(doc_id)
    canonical = dirs["canonical"]
    document_path = canonical / DATASET_FILES["document"]
    if document_path.exists():
        return

    sample_path = SAMPLE_ROOT / doc_id
    if not sample_path.is_dir():
        raise ProjectError(f"Seed sample not found: {sample_path}")

    for file_name in DATASET_FILES.values():
        source = sample_path / file_name
        if source.exists():
            shutil.copy2(source, canonical / file_name)

    document = read_json(document_path)
    ensure_working_files(dirs["project"], document)


def ensure_seed_project() -> None:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    seed_project_from_sample("gold_demo_01")


def has_project(doc_id: str) -> bool:
    return get_project_path(doc_id).is_dir()


def has_canonical_document(doc_id: str) -> bool:
    return (get_project_path(doc_id) / "canonical" / DATASET_FILES["document"]).exists()


def list_projects() -> list[dict[str, Any]]:
    ensure_seed_project()
    projects: list[dict[str, Any]] = []
    if not PROJECTS_ROOT.exists():
        return projects

    for path in sorted(p for p in PROJECTS_ROOT.iterdir() if p.is_dir()):
        document_path = path / "canonical" / DATASET_FILES["document"]
        title = path.name
        note = ""
        status = "created"
        try:
            meta = read_project_meta(path)
            note = str(meta.get("note") or "")
            meta_title = (meta.get("metadata") or {}).get("title")
            if document_path.exists():
                document = read_json(document_path)
                metadata = document.get("metadata", {})
                title = metadata.get("title") or meta_title or document.get("doc_id") or path.name
                status = "available"
            else:
                title = meta_title or path.name
        except Exception:
            title = path.name
        projects.append({
            "doc_id": path.name,
            "title": title,
            "note": note,
            "status": status,
            "path": display_path(path),
        })
    return projects


def project_file_state(doc_id: str) -> dict[str, Any]:
    project_path = get_project_path(doc_id)
    canonical = project_path / "canonical"
    meta = read_project_meta(project_path) if project_path.exists() else {"metadata": {}, "note": ""}
    title = (meta.get("metadata") or {}).get("title") or doc_id
    document_path = canonical / DATASET_FILES["document"]
    if document_path.exists():
        try:
            document = read_json(document_path)
            title = document.get("metadata", {}).get("title") or document.get("doc_id") or title
        except Exception:
            pass
    return {
        "doc_id": doc_id,
        "path": display_path(project_path),
        "title": title,
        "note": meta.get("note", ""),
        "has_raw": any((project_path / "raw").iterdir()) if (project_path / "raw").exists() else False,
        "has_canonical": canonical.exists(),
        "has_document": (canonical / DATASET_FILES["document"]).exists(),
        "has_working": (project_path / "working").exists(),
        "files": {
            key: (canonical / file_name).exists()
            for key, file_name in DATASET_FILES.items()
        },
    }


def update_project_settings(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project_path = get_project_path(doc_id)
    if not project_path.exists():
        raise ProjectError(f"Project not found: {doc_id}")
    note = payload.get("note")
    meta = read_project_meta(project_path)
    if note is not None:
        meta["note"] = str(note)
    write_project_meta(project_path, meta)
    return project_file_state(doc_id)


def delete_project(doc_id: str, confirm_doc_id: str | None = None) -> dict[str, Any]:
    if doc_id == "gold_demo_01":
        raise ProjectError("gold_demo_01 is the protected golden sample and cannot be deleted.")
    if confirm_doc_id != doc_id:
        raise ProjectError("Delete requires confirm_doc_id matching the project doc_id.")
    project_path = get_project_path(doc_id)
    if not project_path.exists():
        raise ProjectError(f"Project not found: {doc_id}")
    shutil.rmtree(project_path)
    return {"doc_id": doc_id, "deleted": True}


def find_source_file(project_path: Path) -> Path | None:
    raw = project_path / "raw"
    if not raw.exists():
        return None
    for path in sorted(raw.iterdir()):
        if path.is_file() and path.suffix.lower() in {".txt", ".epub"}:
            return path
    return None


def save_source_file(project_path: Path, filename: str, data: bytes, overwrite: bool = False) -> Path:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".txt", ".epub"}:
        if suffix == ".pdf":
            raise ProjectError("PDF logical extraction is not supported in MVP.")
        raise ProjectError("Only .txt and .epub sources are supported in MVP.")
    raw = project_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    target = raw / f"source{suffix}"
    if target.exists() and not overwrite:
        raise ProjectError("Source already exists. Confirm overwrite before replacing it.")
    atomic_write_bytes(target, data)
    return target
