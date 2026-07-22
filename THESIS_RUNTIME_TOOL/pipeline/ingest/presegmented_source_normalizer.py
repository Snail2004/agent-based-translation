from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from pathlib import Path, PurePosixPath
from typing import Any

from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.d2l_presegmented_adapter import (
    ADAPTER_VERSION,
    AUTHORITATIVE_D2L_CAPTURE,
    OUTPUT_RECEIPT_FILE,
    validate_d2l_presegmented_output,
)
from pipeline.ingest.document_contract import runtime_block_type
from pipeline.ingest.unified_source_normalizer import (
    UnifiedContractError,
    UnifiedNormalizationResult,
    validate_normalization_contract,
)


NORMALIZER_VERSION = "presegmented_source_normalizer_v1"
MANIFEST_SCHEMA_VERSION = "presegmented_structure_manifest_v1"
DOCUMENT_SCHEMA_VERSION = "1.5.0"

_PRESERVE_KINDS = {
    "code",
    "directive",
    "equation",
    "image",
    "label",
    "math",
    "math_block",
    "raw_html",
    "separator",
}
_STRUCTURED_TRANSLATE_KINDS = {"footnote", "list", "table"}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _policy(source_kind: str) -> str:
    if source_kind in _PRESERVE_KINDS:
        return "preserve"
    if source_kind in _STRUCTURED_TRANSLATE_KINDS:
        return "translate_structured"
    return "translate"


def _heading_level(source_kind: str, text: str) -> int | None:
    if source_kind != "heading":
        return None
    first_line = text.splitlines()[0] if text.splitlines() else ""
    marker = first_line.partition(" ")[0]
    if 1 <= len(marker) <= 6 and set(marker) == {"#"}:
        return len(marker)
    return None


def _canonical_span(
    source_bytes: bytes,
    *,
    start_offset: int,
    end_offset: int,
    canonical_text: str,
    newline_offsets: list[int],
) -> tuple[list[int], list[int]]:
    encoded = canonical_text.encode("utf-8")
    fragment = source_bytes[start_offset:end_offset]
    relative_start = fragment.find(encoded)
    if relative_start < 0 or fragment.find(encoded, relative_start + 1) >= 0:
        raise UnifiedContractError(
            "presegmented block text does not resolve uniquely inside its source span"
        )
    byte_start = start_offset + relative_start
    byte_end = byte_start + len(encoded)
    line_start = bisect_left(newline_offsets, byte_start) + 1
    line_end = bisect_left(newline_offsets, max(byte_start, byte_end - 1)) + 1
    return [byte_start, byte_end], [line_start, line_end]


def _capture_relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise UnifiedContractError("capture_relative_path must be a normalized relative POSIX path")
    return value


def normalize_presegmented_source(
    source_path: str | Path,
    *,
    bundle_root: str | Path,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    capture_relative_path: str | None = None,
    pandoc_executable: str | None = None,
    pdf_formula_detector_mode: str = "disabled",
) -> UnifiedNormalizationResult:
    """Normalize a fully validated D2L pre-segmented bundle without re-parsing Markdown."""

    if pdf_formula_detector_mode != "disabled":
        raise UnifiedContractError("presegmented normalization does not accept PDF detector options")
    del pandoc_executable

    source = Path(source_path).resolve(strict=True)
    if not source.is_file() or source.suffix.casefold() not in {".md", ".markdown"}:
        raise UnifiedContractError("presegmented source must be a regular Markdown file")
    source_bytes = source.read_bytes()
    source_sha256 = _sha256(source_bytes)

    bundle_directory = Path(bundle_root).resolve(strict=True)
    receipt_path = bundle_directory / OUTPUT_RECEIPT_FILE
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise UnifiedContractError("presegmented adapter receipt is unavailable")
    receipt_sha256 = _sha256(receipt_path.read_bytes())
    adapter = validate_d2l_presegmented_output(
        bundle_directory,
        expected_receipt_sha256=receipt_sha256,
    )
    bundle = adapter.bundle
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    bundle_source = bundle_directory / str(bundle.manifest["source_file"])
    if source_bytes != bundle_source.read_bytes() or source_sha256 != bundle.source_sha256:
        raise UnifiedContractError(
            "server-owned source is not byte-identical to the validated presegmented bundle"
        )

    capture_path = _capture_relative_path(capture_relative_path)
    provenance: dict[str, Any] = {
        "upstream_document_id": AUTHORITATIVE_D2L_CAPTURE.document_id,
        "upstream_source_sha256": receipt["upstream"]["marked_source"]["sha256"],
        "upstream_block_map_sha256": receipt["upstream"]["legacy_block_map"]["sha256"],
        "upstream_manifest_sha256": receipt["upstream"]["legacy_manifest"]["sha256"],
        "upstream_source_db_sha256": receipt["upstream"]["source_db_sha256"],
        "adapter_version": ADAPTER_VERSION,
        "adapter_receipt_sha256": adapter.receipt_sha256,
        "adapter_identity_sha256": adapter.adapter_identity_sha256,
        "bundle_schema_version": bundle.manifest["schema_version"],
        "bundle_identity_sha256": bundle.identity_sha256,
        "bundle_source_file": bundle.manifest["source_file"],
        "bundle_block_map_file": bundle.manifest["block_map_file"],
        "bundle_manifest_sha256": _sha256(
            (bundle_directory / "manifest.json").read_bytes()
        ),
        "bundle_block_map_sha256": bundle.manifest["block_map_sha256"],
    }
    if capture_path is not None:
        provenance["capture_relative_path"] = capture_path

    source_map: list[dict[str, Any]] = []
    block_policies: list[dict[str, str]] = []
    block_by_id: dict[str, dict[str, Any]] = {}
    newline_offsets = [
        index for index, value in enumerate(source_bytes) if value == ord("\n")
    ]
    for block in bundle.blocks:
        source_byte_range, line_range = _canonical_span(
            source_bytes,
            start_offset=block.source_start_offset,
            end_offset=block.source_end_offset,
            canonical_text=block.source_text,
            newline_offsets=newline_offsets,
        )
        runtime_block = {
            "block_id": block.block_id,
            "order_index": block.order_index,
            "page_ids": [],
            "block_type": runtime_block_type(block.block_type),
            "is_chapter_opening": False,
            "source_text": block.source_text,
            "clean_text": block.source_text,
            "sentences": [],
            "quality_flags": [],
            "annotations": {},
        }
        block_by_id[block.block_id] = runtime_block
        source_map.append(
            {
                "block_id": block.block_id,
                "source_path": source.name,
                "marker": block.marker,
                "source_byte_range": source_byte_range,
                "line_range": line_range,
                "markdown_anchor": block.marker,
                "source_block_kind": block.block_type,
                "heading_level": _heading_level(block.block_type, block.source_text),
                "source_sha256": block.source_sha256,
                "source_utf8_bytes": block.source_utf8_bytes,
                "provenance_precision": "presegmented_exact_block_map",
            }
        )
        block_policies.append(
            {
                "block_id": block.block_id,
                "translation_policy": _policy(block.block_type),
            }
        )

    chapters: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    for chapter in bundle.chapters:
        chapter_blocks = [block_by_id[block_id] for block_id in chapter.block_ids]
        chapter_blocks[0]["is_chapter_opening"] = True
        chapters.append(
            {
                "chapter_id": chapter.chapter_id,
                "order_index": chapter.order_index,
                "title": chapter.title,
                "blocks": chapter_blocks,
            }
        )
        units.append(
            {
                "unit_id": chapter.chapter_id,
                "chapter_id": chapter.chapter_id,
                "order_index": chapter.order_index,
                "title": chapter.title,
                "block_range": [
                    chapter.first_block_index,
                    chapter.last_block_index + 1,
                ],
                "role": "content_unit",
                "translation_policy": "translate",
                "confidence": 1.0,
                "evidence": [
                    "validated_presegmented_block_map",
                    f"bundle_identity:{bundle.identity_sha256}",
                    f"adapter_identity:{adapter.adapter_identity_sha256}",
                ],
                "review_required": False,
            }
        )

    source_identity = {
        "path": str(source),
        "sha256": source_sha256,
        "format": "markdown",
        "provenance": provenance,
    }
    exact_cover = {
        "expected_blocks": bundle.block_count,
        "covered_blocks": bundle.block_count,
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }
    structure = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "doc_id": doc_id,
        "source": source_identity,
        "extractor": {
            "name": "validated_presegmented_source_bundle",
            "version": "v1",
            "mode": "exact_map_no_markdown_reparse",
        },
        "cross_check": {"status": "not_applicable", "review_required": False},
        "warnings": [],
        "units": units,
        "translatable_chapter_ids": [chapter.chapter_id for chapter in bundle.chapters],
        "review_required_unit_ids": [],
        "review_required_chapter_ids": [],
        "exact_cover": exact_cover,
        "source_map": source_map,
        "block_policies": block_policies,
    }
    structure["structure_sha256"] = canonical_json_sha256(
        {
            "normalizer_version": NORMALIZER_VERSION,
            "source": source_identity,
            "units": units,
            "source_map": source_map,
            "block_policies": block_policies,
        }
    )
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": {
            "title": bundle.document_id,
            "author": "",
            "domain": "technical",
            "genre": "technical_book",
            "source_language": source_language,
            "target_language": target_language,
            "source_format": "markdown",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": NORMALIZER_VERSION,
            "pipeline_version": NORMALIZER_VERSION,
            "contamination_risk": "medium",
        },
        "chapters": chapters,
    }
    normalization_receipt = validate_normalization_contract(
        document,
        structure,
        expected_format="markdown",
    )
    if _sha256(source.read_bytes()) != source_sha256:
        raise UnifiedContractError("source changed while presegmented normalization was running")
    return UnifiedNormalizationResult(
        document=document,
        structure_manifest=structure,
        receipt=normalization_receipt,
    )


__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "NORMALIZER_VERSION",
    "normalize_presegmented_source",
]
