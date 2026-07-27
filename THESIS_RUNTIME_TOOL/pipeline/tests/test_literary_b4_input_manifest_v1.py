from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.b4_input_manifest_v1 import (
    B4InputManifestError,
    build_b4_input_manifest_from_chapter_run_v1,
)


def _write(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "component"
    _write(root / "component_manifest.json", {"project_id": "fixture_book"})
    chapter_ids = ("book_ch01", "book_ch02")
    identity = root / "artifacts" / "chapters" / "ch002" / "identity_apply"
    hashes = []
    for index, chapter_id in enumerate(chapter_ids):
        registry_hash = f"{index + 1}" * 64
        hashes.append(registry_hash)
        _write(
            identity / f"source_registry_{index:02d}.json",
            {
                "chapter_id": chapter_id,
                "registry_hash": registry_hash,
            },
        )
    _write(
        identity / "reconciled_projection.json",
        {
            "book_id": "fixture_book",
            "source_registry_hashes": hashes,
        },
    )
    for order, chapter_id in enumerate(chapter_ids, start=1):
        chapter = root / "artifacts" / "chapters" / f"ch{order:03d}"
        for stage, filename in (
            ("b0_summary", "chapter_summary_artifact.json"),
            ("b2_frame_interaction", "chapter_b2_artifact.json"),
            ("b3_apply", "reconciled_temporal_artifact.json"),
            ("b3_temporal", "component_catalog.json"),
        ):
            _write(chapter / stage / filename, {"chapter_id": chapter_id})
        _write(
            chapter / "b0_summary" / "capsule_log.json",
            {"chapter_id": chapter_id},
        )
        _write(
            chapter / "b2_frame_interaction" / "window_plan.json",
            {"chapter_id": chapter_id},
        )
    return root


def test_manifest_builder_binds_projection_registries_in_chapter_order(
    tmp_path: Path,
) -> None:
    root = _fixture(tmp_path)
    manifest = build_b4_input_manifest_from_chapter_run_v1(
        chapter_run_root=root,
        target_chapter_order=2,
    )
    assert manifest["book_id"] == "fixture_book"
    assert manifest["target_chapter_id"] == "book_ch02"
    assert [row["chapter_id"] for row in manifest["chapters"]] == [
        "book_ch01",
        "book_ch02",
    ]
    assert manifest["chapters"][0]["registry_path"].endswith(
        "source_registry_00.json"
    )


def test_manifest_builder_halts_when_projection_registry_binding_differs(
    tmp_path: Path,
) -> None:
    root = _fixture(tmp_path)
    path = (
        root
        / "artifacts"
        / "chapters"
        / "ch002"
        / "identity_apply"
        / "reconciled_projection.json"
    )
    projection = json.loads(path.read_text(encoding="utf-8"))
    projection["source_registry_hashes"].reverse()
    _write(path, projection)
    with pytest.raises(
        B4InputManifestError,
        match="differ from the projection binding",
    ):
        build_b4_input_manifest_from_chapter_run_v1(
            chapter_run_root=root,
            target_chapter_order=2,
        )
