"""Project-neutral source projection for a Literary chapter-loop run."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


PROJECT_BINDING_SCHEMA = "literary_project_binding_v1"
PROJECT_DOCUMENT_SCHEMA = "literary_project_document_projection_v1"


class LiteraryProjectSourceBridgeError(ValueError):
    pass


def prepare_literary_project_source_v1(
    *,
    job_root: Path,
    output_root: Path,
    chapter_ids: Sequence[str] | None = None,
    chapter_range: str | None = None,
    chapter_count: int | None = None,
) -> dict[str, Any]:
    """Project translatable chapters without mutating the canonical App project."""

    root = Path(job_root).resolve()
    if not root.is_dir():
        raise LiteraryProjectSourceBridgeError("project job root is absent")
    manifest_path = root / "source_manifest.json"
    manifest = _read_object(manifest_path, "project source manifest")
    if manifest.get("contract_version") != "project_runtime_source_v2":
        raise LiteraryProjectSourceBridgeError(
            "project source manifest is not project_runtime_source_v2"
        )
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list) or "literary_v1" not in profiles:
        raise LiteraryProjectSourceBridgeError(
            "project is not finalized for the literary_v1 profile"
        )
    job_id = _required_id(manifest.get("job_id"), "job_id")
    project_id = _required_id(manifest.get("project_id"), "project_id")
    source_identity = _sha256(
        manifest.get("source_identity_sha256"), "source_identity_sha256"
    )
    document_path = _resolve_job_relative(
        root, manifest.get("source_document"), "source_document"
    )
    snapshot_root = document_path.parent
    structure_path = snapshot_root / "structure_manifest.json"
    frozen_db_path = root / "memory.sqlite3"
    for path, label in (
        (document_path, "project document"),
        (structure_path, "structure manifest"),
        (frozen_db_path, "frozen runtime database"),
    ):
        if not path.is_file():
            raise LiteraryProjectSourceBridgeError(f"{label} is absent")

    document = load_literary_source_document_v1(document_path)
    structure = _read_object(structure_path, "structure manifest")
    translatable = _string_list(
        structure.get("translatable_chapter_ids"),
        "structure translatable_chapter_ids",
    )
    if not translatable:
        raise LiteraryProjectSourceBridgeError(
            "structure manifest has no translatable chapters"
        )
    if len(translatable) != len(set(translatable)):
        raise LiteraryProjectSourceBridgeError(
            "structure manifest repeats a translatable chapter"
        )

    chapters_by_id = {
        str(row.get("chapter_id")): dict(row)
        for row in document["chapters"]
        if isinstance(row, Mapping)
    }
    document_order = [
        str(row["chapter_id"])
        for row in document["chapters"]
        if isinstance(row, Mapping)
    ]
    if [chapter_id for chapter_id in document_order if chapter_id in set(translatable)] != translatable:
        raise LiteraryProjectSourceBridgeError(
            "translatable chapter order differs from the canonical document"
        )
    missing = [chapter_id for chapter_id in translatable if chapter_id not in chapters_by_id]
    if missing:
        raise LiteraryProjectSourceBridgeError(
            "translatable chapters are absent from the project document: "
            + ", ".join(missing)
        )
    manifest_translatable = [
        str(row.get("chapter_id"))
        for row in manifest.get("chapters") or []
        if isinstance(row, Mapping) and row.get("translation_policy") == "translate"
    ]
    if manifest_translatable != translatable:
        raise LiteraryProjectSourceBridgeError(
            "source and structure manifests disagree on translatable chapters"
        )
    expected_counts = {
        str(row["chapter_id"]): int(row["block_count"])
        for row in manifest.get("chapters") or []
        if isinstance(row, Mapping)
        and row.get("chapter_id") in set(translatable)
        and isinstance(row.get("block_count"), int)
    }
    for chapter_id in translatable:
        observed = len(chapters_by_id[chapter_id].get("blocks") or [])
        if expected_counts.get(chapter_id) != observed:
            raise LiteraryProjectSourceBridgeError(
                f"chapter block count drifted for {chapter_id}"
            )

    selected = _select_chapters(
        translatable,
        chapter_ids=chapter_ids,
        chapter_range=chapter_range,
        chapter_count=chapter_count,
    )
    selected_chapters = [chapters_by_id[chapter_id] for chapter_id in selected]
    projected_document = {
        "schema_version": document.get("schema_version"),
        "doc_id": document.get("doc_id")
        or document.get("document_id")
        or document.get("id"),
        "metadata": dict(document.get("metadata") or {}),
        "chapters": selected_chapters,
        "literary_projection": {
            "schema_version": PROJECT_DOCUMENT_SCHEMA,
            "project_id": project_id,
            "job_id": job_id,
            "source_identity_sha256": source_identity,
            "selected_chapter_ids": selected,
        },
    }
    projection_body = {
        "project_id": project_id,
        "job_id": job_id,
        "source_identity_sha256": source_identity,
        "selected_chapter_ids": selected,
        "chapter_block_counts": {
            chapter_id: len(chapters_by_id[chapter_id]["blocks"])
            for chapter_id in selected
        },
    }
    projection_id = canonical_hash(projection_body)
    target_root = Path(output_root).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    document_output = target_root / "literary_document.json"
    binding_output = target_root / "literary_project_binding.json"
    _write_absent_or_equal(document_output, projected_document)

    source_artifacts = {
        "source_manifest": {
            "job_relative_ref": "source_manifest.json",
            "physical_sha256": file_sha256(manifest_path),
        },
        "document": {
            "job_relative_ref": _job_relative(root, document_path),
            "physical_sha256": file_sha256(document_path),
        },
        "structure_manifest": {
            "job_relative_ref": _job_relative(root, structure_path),
            "physical_sha256": file_sha256(structure_path),
        },
        "frozen_db": {
            "job_relative_ref": "memory.sqlite3",
            "physical_sha256": file_sha256(frozen_db_path),
        },
    }
    binding_body = {
        "schema_version": PROJECT_BINDING_SCHEMA,
        "project_id": project_id,
        "job_id": job_id,
        "document_id": str(projected_document["doc_id"]),
        "source_identity_sha256": source_identity,
        "projection_id": projection_id,
        "selected_chapter_ids": selected,
        "chapter_count": len(selected),
        "block_count": sum(len(row["blocks"]) for row in selected_chapters),
        "projected_document_ref": "literary_document.json",
        "projected_document_sha256": file_sha256(document_output),
        "source_artifacts": source_artifacts,
        "canonical_project_mutated": False,
    }
    binding = {**binding_body, "binding_hash": canonical_hash(binding_body)}
    _write_absent_or_equal(binding_output, binding)
    return {
        "schema_version": "literary_project_source_prepare_report_v1",
        "project_id": project_id,
        "job_id": job_id,
        "source_identity_sha256": source_identity,
        "projection_id": projection_id,
        "selected_chapter_ids": selected,
        "chapter_count": len(selected),
        "block_count": binding["block_count"],
        "document_path": str(document_output),
        "project_binding_path": str(binding_output),
        "frozen_db_path": str(frozen_db_path),
        "provider_calls": 0,
        "canonical_project_mutated": False,
    }


def validate_literary_project_binding_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema_version") != PROJECT_BINDING_SCHEMA:
        raise LiteraryProjectSourceBridgeError("literary project binding schema differs")
    observed = row.pop("binding_hash", None)
    if not isinstance(observed, str) or canonical_hash(row) != observed:
        raise LiteraryProjectSourceBridgeError("literary project binding hash drifted")
    _required_id(row.get("project_id"), "project_id")
    _required_id(row.get("job_id"), "job_id")
    _sha256(row.get("source_identity_sha256"), "source_identity_sha256")
    _sha256(row.get("projected_document_sha256"), "projected_document_sha256")
    selected = _string_list(row.get("selected_chapter_ids"), "selected_chapter_ids")
    if not selected or len(selected) != len(set(selected)):
        raise LiteraryProjectSourceBridgeError("selected chapter set is invalid")
    if row.get("chapter_count") != len(selected):
        raise LiteraryProjectSourceBridgeError("project binding chapter count drifted")
    if row.get("canonical_project_mutated") is not False:
        raise LiteraryProjectSourceBridgeError(
            "project binding must record a read-only canonical project"
        )
    return {**row, "binding_hash": observed}


def _select_chapters(
    translatable: Sequence[str],
    *,
    chapter_ids: Sequence[str] | None,
    chapter_range: str | None,
    chapter_count: int | None,
) -> list[str]:
    requested_modes = sum(
        value is not None
        for value in (
            chapter_ids,
            chapter_range,
            chapter_count,
        )
    )
    if requested_modes > 1:
        raise LiteraryProjectSourceBridgeError(
            "chapter ids, range, and count are mutually exclusive"
        )
    if chapter_ids is not None:
        selected = [_required_id(value, "chapter_id") for value in chapter_ids]
    elif chapter_range is not None:
        parts = chapter_range.split(":")
        if len(parts) != 2 or not all(parts):
            raise LiteraryProjectSourceBridgeError(
                "chapter range must be START_ID:END_ID"
            )
        try:
            start = translatable.index(parts[0])
            end = translatable.index(parts[1])
        except ValueError as exc:
            raise LiteraryProjectSourceBridgeError(
                "chapter range contains a foreign chapter"
            ) from exc
        if start > end:
            raise LiteraryProjectSourceBridgeError("chapter range reverses order")
        selected = list(translatable[start : end + 1])
    elif chapter_count is not None:
        if (
            isinstance(chapter_count, bool)
            or not isinstance(chapter_count, int)
            or not 1 <= chapter_count <= len(translatable)
        ):
            raise LiteraryProjectSourceBridgeError(
                "chapter count is outside the project"
            )
        selected = list(translatable[:chapter_count])
    else:
        selected = list(translatable)
    if not selected or len(selected) != len(set(selected)):
        raise LiteraryProjectSourceBridgeError("chapter selection is invalid")
    try:
        positions = [translatable.index(chapter_id) for chapter_id in selected]
    except ValueError as exc:
        raise LiteraryProjectSourceBridgeError(
            "chapter selection contains a foreign chapter"
        ) from exc
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise LiteraryProjectSourceBridgeError(
            "chapter selection must be contiguous"
        )
    return selected


def _resolve_job_relative(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise LiteraryProjectSourceBridgeError(f"{label} is missing")
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise LiteraryProjectSourceBridgeError(f"{label} is not job-relative")
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LiteraryProjectSourceBridgeError(f"{label} escapes the job root") from exc
    return path


def _job_relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _required_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise LiteraryProjectSourceBridgeError(f"{label} is invalid")
    return value


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise LiteraryProjectSourceBridgeError(f"{label} is not a SHA-256")
    return value.lower()


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise LiteraryProjectSourceBridgeError(f"{label} must be a string list")
    return list(value)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryProjectSourceBridgeError(f"cannot load {label}") from exc
    if not isinstance(value, dict):
        raise LiteraryProjectSourceBridgeError(f"{label} must be an object")
    return value


def _write_absent_or_equal(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    encoded = (canonical_json(dict(value)) + "\n").encode("utf-8")
    if target.exists():
        if target.read_bytes() != encoded:
            raise LiteraryProjectSourceBridgeError(
                f"immutable project projection drifted: {target.name}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, target)


__all__ = [
    "LiteraryProjectSourceBridgeError",
    "prepare_literary_project_source_v1",
    "validate_literary_project_binding_v1",
]
