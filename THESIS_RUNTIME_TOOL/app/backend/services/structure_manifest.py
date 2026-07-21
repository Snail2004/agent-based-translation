from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


STRUCTURE_MANIFEST_FILENAME = "structure_manifest.json"
STRUCTURE_MANIFEST_SCHEMA = "epub_structure_manifest_v1"
UNIT_ROLES = {"front_matter", "content_unit", "container", "back_matter", "unknown"}
TRANSLATION_POLICIES = {"translate", "preserve", "exclude", "review"}
ROLE_POLICY = {
    "front_matter": "preserve",
    "content_unit": "translate",
    "container": "preserve",
    "back_matter": "exclude",
    "unknown": "review",
}


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_structure_manifest(
    document: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if manifest.get("schema_version") != STRUCTURE_MANIFEST_SCHEMA:
        raise ValueError("Unsupported structure manifest schema")
    if manifest.get("doc_id") != document.get("doc_id"):
        raise ValueError("Structure manifest doc_id does not match document.json")

    metadata = document.get("metadata") or {}
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Structure manifest source must be an object")
    document_source_hash = str(metadata.get("raw_sha256") or "")
    manifest_source_hash = str(source.get("sha256") or "")
    if not document_source_hash or document_source_hash != manifest_source_hash:
        raise ValueError("Structure manifest source hash does not match document.json")

    chapters = document.get("chapters") or []
    chapter_ids = [str(chapter.get("chapter_id") or "") for chapter in chapters]
    if not chapter_ids or any(not chapter_id for chapter_id in chapter_ids):
        raise ValueError("Document chapters must have non-empty chapter_id values")
    if len(set(chapter_ids)) != len(chapter_ids):
        raise ValueError("Document chapter_id values must be unique")

    units = manifest.get("units")
    if not isinstance(units, list):
        raise ValueError("Structure manifest units must be a list")
    if any(not isinstance(unit, Mapping) for unit in units):
        raise ValueError("Every structure manifest unit must be an object")
    if any(type(unit.get("review_required")) is not bool for unit in units):
        raise ValueError("Structure unit review_required must be a boolean")
    unit_chapter_ids = [str(unit.get("chapter_id") or "") for unit in units]
    unit_ids = [str(unit.get("unit_id") or "") for unit in units]
    if unit_chapter_ids != chapter_ids:
        raise ValueError("Structure manifest units do not exact-cover document chapters in order")
    if any(not unit_id for unit_id in unit_ids) or len(set(unit_ids)) != len(unit_ids):
        raise ValueError("Structure manifest unit_id values must be non-empty and unique")
    next_block_index = 0
    for unit, chapter in zip(units, chapters, strict=True):
        if unit.get("role") not in UNIT_ROLES:
            raise ValueError(f"Unknown structure unit role: {unit.get('role')}")
        if unit.get("translation_policy") not in TRANSLATION_POLICIES:
            raise ValueError(
                f"Unknown structure translation policy: {unit.get('translation_policy')}"
            )
        expected_policy = ROLE_POLICY[str(unit["role"])]
        if unit.get("translation_policy") != expected_policy:
            raise ValueError(
                f"Structure unit role {unit.get('role')} requires policy {expected_policy}"
            )
        block_range = unit.get("block_range")
        if (
            not isinstance(block_range, list)
            or len(block_range) != 2
            or any(not isinstance(value, int) for value in block_range)
        ):
            raise ValueError("Structure unit block_range must contain two integer offsets")
        start, end = block_range
        if start != next_block_index or end < start:
            raise ValueError("Structure unit block ranges must be contiguous and non-overlapping")
        if end - start != len(chapter.get("blocks") or []):
            raise ValueError("Structure unit block_range does not match its document chapter")
        next_block_index = end

    expected_translatable = [
        str(unit["chapter_id"])
        for unit in units
        if unit.get("role") == "content_unit"
    ]
    if list(manifest.get("translatable_chapter_ids") or []) != expected_translatable:
        raise ValueError("translatable_chapter_ids must equal the content-unit chapter list")

    expected_review = [
        str(unit["chapter_id"])
        for unit in units
        if unit["review_required"]
    ]
    if list(manifest.get("review_required_chapter_ids") or []) != expected_review:
        raise ValueError("review_required_chapter_ids does not match unit review flags")

    exact_cover = manifest.get("exact_cover")
    if not isinstance(exact_cover, Mapping):
        raise ValueError("Structure manifest exact_cover must be an object")
    document_block_ids = [
        str(block.get("block_id") or "")
        for chapter in chapters
        for block in chapter.get("blocks") or []
    ]
    source_map = manifest.get("source_map")
    if not isinstance(source_map, list):
        raise ValueError("Structure manifest source_map must be a list")
    if any(not isinstance(row, Mapping) for row in source_map):
        raise ValueError("Every structure manifest source_map row must be an object")
    source_map_ids = [str(row.get("block_id") or "") for row in source_map]
    if source_map_ids != document_block_ids:
        raise ValueError("Structure source_map must exact-cover document blocks in order")
    if (
        exact_cover.get("coverage") != 1.0
        or exact_cover.get("overlap_count") != 0
        or exact_cover.get("missing_count") != 0
        or exact_cover.get("expected_blocks") != len(document_block_ids)
        or exact_cover.get("covered_blocks") != len(document_block_ids)
        or next_block_index != len(document_block_ids)
    ):
        raise ValueError("Structure manifest block coverage is not exact")

    expected_hash = _canonical_hash(
        {
            "normalizer_version": manifest.get("normalizer_version"),
            "source_sha256": manifest_source_hash,
            "units": units,
            "source_map": source_map,
        }
    )
    if manifest.get("structure_sha256") != expected_hash:
        raise ValueError("Structure manifest content hash is invalid")
    return manifest


def read_structure_manifest(
    project_path: Path,
    document: dict[str, Any],
) -> dict[str, Any] | None:
    path = project_path / "canonical" / STRUCTURE_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid structure manifest JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Structure manifest must be a JSON object")
    return validate_structure_manifest(document, payload)


def chapter_routing(manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not manifest:
        return {}
    return {
        str(unit["chapter_id"]): {
            "unit_id": unit.get("unit_id"),
            "unit_role": unit.get("role"),
            "translation_policy": unit.get("translation_policy"),
            "parent_unit_id": unit.get("parent_unit_id"),
            "review_required": unit["review_required"],
        }
        for unit in manifest.get("units") or []
    }


__all__ = [
    "STRUCTURE_MANIFEST_FILENAME",
    "chapter_routing",
    "read_structure_manifest",
    "validate_structure_manifest",
]
