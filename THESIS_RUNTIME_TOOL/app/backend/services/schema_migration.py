import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATASET_FILES, SCHEMA_VERSION
from services.dataset_io import atomic_write_text, write_json_atomic
from services.validator import run_validator


PREVIOUS_VERSION = "1.4.0"


class SchemaMigrationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _canonical(project_path: Path) -> Path:
    return project_path / "canonical"


def _document_path(project_path: Path) -> Path:
    return _canonical(project_path) / DATASET_FILES["document"]


def _relations_path(project_path: Path) -> Path:
    return _canonical(project_path) / DATASET_FILES["entity_relations"]


def _load_document(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise SchemaMigrationError("invalid_document_json", f"Invalid JSON in document.json: {exc}", 400) from exc


def _backup_document(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def migrate_project_to_schema_1_5(project_path: Path) -> dict[str, Any]:
    document_path = _document_path(project_path)
    relations_path = _relations_path(project_path)
    if not document_path.exists():
        raise SchemaMigrationError("missing_document", "Project has no canonical/document.json.", 404)

    document = _load_document(document_path)
    before_version = document.get("schema_version")
    actions: list[str] = []
    backup_path: Path | None = None

    if before_version == PREVIOUS_VERSION:
        backup_path = _backup_document(document_path)
        document["schema_version"] = SCHEMA_VERSION
        write_json_atomic(document_path, document)
        actions.append(f"updated document.json schema_version {PREVIOUS_VERSION} -> {SCHEMA_VERSION}")
    elif before_version == SCHEMA_VERSION:
        actions.append(f"document.json already uses schema_version {SCHEMA_VERSION}")
    else:
        raise SchemaMigrationError(
            "unsupported_schema_version",
            f"Cannot migrate schema_version {before_version!r}; expected {PREVIOUS_VERSION} or {SCHEMA_VERSION}.",
            409,
        )

    if relations_path.exists():
        actions.append("entity_relations.jsonl already exists")
    else:
        atomic_write_text(relations_path, "")
        actions.append("created optional empty entity_relations.jsonl")

    validation = run_validator(_canonical(project_path))
    return {
        "schema_version_before": before_version,
        "schema_version_after": SCHEMA_VERSION,
        "actions": actions,
        "backup": str(backup_path) if backup_path else None,
        "validation": validation,
    }
