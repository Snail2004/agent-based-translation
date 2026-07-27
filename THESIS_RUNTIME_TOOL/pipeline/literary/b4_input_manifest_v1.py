"""Deterministic B0-B3 lineage binding for the B4 input manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.b4_story_bible_assembler_v1 import (
    MANIFEST_SCHEMA_VERSION,
)


class B4InputManifestError(RuntimeError):
    """Raised when a chapter prefix cannot be bound to one B4 manifest."""


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise B4InputManifestError(f"required {label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise B4InputManifestError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B4InputManifestError(f"{label} must be a JSON object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise B4InputManifestError(f"{label} must be a non-empty string")
    return value


def _chapter_root(root: Path, order: int) -> Path:
    return root / "artifacts" / "chapters" / f"ch{order:03d}"


def _chapter_file(
    chapter_root: Path,
    stage: str,
    filename: str,
    label: str,
) -> Path:
    path = (chapter_root / stage / filename).resolve()
    if not path.is_file():
        raise B4InputManifestError(f"required {label} is missing: {path}")
    return path


def build_b4_input_manifest_from_chapter_run_v1(
    *,
    chapter_run_root: Path,
    target_chapter_order: int,
) -> dict[str, Any]:
    """Bind one complete B0-B3 chapter prefix without latest-path discovery."""

    root = Path(chapter_run_root).resolve()
    if (
        not isinstance(target_chapter_order, int)
        or isinstance(target_chapter_order, bool)
        or target_chapter_order <= 0
    ):
        raise B4InputManifestError("target_chapter_order must be positive")

    component_manifest = _read_object(
        root / "component_manifest.json", "chapter-loop component manifest"
    )
    project_id = _text(
        component_manifest.get("project_id"),
        "chapter-loop component manifest project_id",
    )
    target_root = _chapter_root(root, target_chapter_order)
    projection_path: Path | None = None
    registries_by_chapter: dict[str, Path] = {}
    book_id: str

    if target_chapter_order == 1:
        registry_path = _chapter_file(
            target_root,
            "b1_registry_writer",
            "chapter_registry.json",
            "chapter 1 registry",
        )
        registry = _read_object(registry_path, "chapter 1 registry")
        chapter_id = _text(registry.get("chapter_id"), "chapter 1 registry chapter_id")
        registries_by_chapter[chapter_id] = registry_path
        book_id = project_id
    else:
        identity_root = target_root / "identity_apply"
        projection_path = _chapter_file(
            target_root,
            "identity_apply",
            "reconciled_projection.json",
            "identity projection",
        )
        projection = _read_object(projection_path, "identity projection")
        book_id = _text(projection.get("book_id"), "identity projection book_id")
        if book_id != project_id:
            raise B4InputManifestError(
                "identity projection book_id differs from the chapter-loop project"
            )
        expected_hashes = projection.get("source_registry_hashes")
        if not isinstance(expected_hashes, list) or any(
            not isinstance(value, str) or not value for value in expected_hashes
        ):
            raise B4InputManifestError(
                "identity projection source_registry_hashes is malformed"
            )
        if len(expected_hashes) != target_chapter_order:
            raise B4InputManifestError(
                "identity projection does not exact-cover the chapter prefix"
            )
        loaded_hashes: list[str] = []
        for index in range(target_chapter_order):
            registry_path = (
                identity_root / f"source_registry_{index:02d}.json"
            ).resolve()
            registry = _read_object(
                registry_path, f"identity source registry {index}"
            )
            chapter_id = _text(
                registry.get("chapter_id"),
                f"identity source registry {index} chapter_id",
            )
            registry_hash = _text(
                registry.get("registry_hash"),
                f"identity source registry {index} registry_hash",
            )
            if chapter_id in registries_by_chapter:
                raise B4InputManifestError(
                    "identity source registries repeat a chapter"
                )
            registries_by_chapter[chapter_id] = registry_path
            loaded_hashes.append(registry_hash)
        if loaded_hashes != expected_hashes:
            raise B4InputManifestError(
                "identity source registries differ from the projection binding"
            )

    chapters: list[dict[str, Any]] = []
    for order in range(1, target_chapter_order + 1):
        chapter_root = _chapter_root(root, order)
        summary_path = _chapter_file(
            chapter_root,
            "b0_summary",
            "chapter_summary_artifact.json",
            f"chapter {order} B0 summary",
        )
        summary = _read_object(summary_path, f"chapter {order} B0 summary")
        chapter_id = _text(
            summary.get("chapter_id"), f"chapter {order} B0 chapter_id"
        )
        registry_path = registries_by_chapter.get(chapter_id)
        if registry_path is None:
            raise B4InputManifestError(
                f"identity projection lacks chapter registry: {chapter_id}"
            )
        paths = {
            "registry_path": registry_path,
            "interaction_path": _chapter_file(
                chapter_root,
                "b2_frame_interaction",
                "chapter_b2_artifact.json",
                f"chapter {order} B2 interaction",
            ),
            "temporal_path": _chapter_file(
                chapter_root,
                "b3_apply",
                "reconciled_temporal_artifact.json",
                f"chapter {order} B3 temporal",
            ),
            "component_catalog_path": _chapter_file(
                chapter_root,
                "b3_temporal",
                "component_catalog.json",
                f"chapter {order} B3 component catalog",
            ),
            "summary_path": summary_path,
        }
        for field, path in paths.items():
            if field == "registry_path":
                continue
            payload = _read_object(path, f"chapter {order} {field}")
            if payload.get("chapter_id") != chapter_id:
                raise B4InputManifestError(
                    f"chapter {order} {field} chapter_id mismatch"
                )
        recovery_path = (
            chapter_root / "speaker_recovery" / "speaker_recovery_artifact.json"
        ).resolve()
        if recovery_path.is_file():
            recovery = _read_object(
                recovery_path, f"chapter {order} speaker recovery"
            )
            if recovery.get("chapter_id") != chapter_id:
                raise B4InputManifestError(
                    f"chapter {order} recovery chapter_id mismatch"
                )
            recovery_value: str | None = str(recovery_path)
        else:
            recovery_value = None
        chapters.append(
            {
                "chapter_id": chapter_id,
                "chapter_order": order,
                **{field: str(path) for field, path in paths.items()},
                "recovery_path": recovery_value,
            }
        )

    target_id = str(chapters[-1]["chapter_id"])
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "book_id": book_id,
        "target_chapter_id": target_id,
        "target_chapter_order": target_chapter_order,
        "chapters": chapters,
        "identity_projection_path": (
            str(projection_path) if projection_path is not None else None
        ),
        "capsule_log_path": str(
            _chapter_file(
                target_root,
                "b0_summary",
                "capsule_log.json",
                "target capsule log",
            )
        ),
        "window_plan_path": str(
            _chapter_file(
                target_root,
                "b2_frame_interaction",
                "window_plan.json",
                "target window plan",
            )
        ),
    }


def write_b4_input_manifest_v1(
    manifest: Mapping[str, Any],
    *,
    output_path: Path,
) -> Path:
    path = Path(output_path).resolve()
    if path.exists():
        raise B4InputManifestError(f"output manifest already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "B4InputManifestError",
    "build_b4_input_manifest_from_chapter_run_v1",
    "write_b4_input_manifest_v1",
]
