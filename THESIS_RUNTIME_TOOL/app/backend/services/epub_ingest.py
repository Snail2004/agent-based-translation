from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[3]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from pipeline.ingest.epub_normalizer import NORMALIZER_VERSION, normalize_epub
from services.structure_manifest import STRUCTURE_MANIFEST_FILENAME, validate_structure_manifest


DOCUMENT_METADATA_FIELDS = {
    "title",
    "author",
    "domain",
    "genre",
    "source_language",
    "target_language",
    "source_format",
    "license",
    "license_url",
    "source_url",
    "raw_sha256",
    "retrieved_at",
    "extraction_tool",
    "pipeline_version",
    "contamination_risk",
}


def normalize_epub_for_project(
    source_path: Path,
    *,
    doc_id: str,
    metadata: dict[str, Any],
    pandoc_executable: str = "pandoc",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    result = normalize_epub(
        source_path,
        doc_id=doc_id,
        source_language=str(metadata.get("source_language") or "en"),
        target_language=str(metadata.get("target_language") or "vi"),
        pandoc_executable=pandoc_executable,
    )
    document = copy.deepcopy(result.document)
    normalized_metadata = dict(document.get("metadata") or {})
    for key, value in metadata.items():
        if key in DOCUMENT_METADATA_FIELDS and value not in (None, ""):
            normalized_metadata[key] = value
    normalized_metadata.update(
        {
            "source_format": "epub",
            "raw_sha256": result.structure_manifest["source"]["sha256"],
            "extraction_tool": NORMALIZER_VERSION,
        }
    )
    document["metadata"] = normalized_metadata
    manifest = validate_structure_manifest(document, copy.deepcopy(result.structure_manifest))
    report = {
        "normalizer_version": manifest["normalizer_version"],
        "structure_manifest": f"canonical/{STRUCTURE_MANIFEST_FILENAME}",
        "structure_sha256": manifest["structure_sha256"],
        "units": len(manifest["units"]),
        "translatable_units": len(manifest["translatable_chapter_ids"]),
        "review_required_units": len(manifest["review_required_chapter_ids"]),
        "roles": {
            role: sum(1 for unit in manifest["units"] if unit.get("role") == role)
            for role in sorted({str(unit.get("role")) for unit in manifest["units"]})
        },
        "exact_cover": manifest["exact_cover"],
        "extractor": manifest["extractor"],
        "navigation": manifest["navigation"],
    }
    return document, manifest, report


__all__ = ["normalize_epub_for_project"]
