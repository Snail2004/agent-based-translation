from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pipeline.ingest.document_contract import (
    DocumentContractError,
    validate_locked_document,
)
from pipeline.ingest.epub_normalizer import normalize_epub
from pipeline.ingest.html_normalizer import normalize_html
from pipeline.ingest.markdown_normalizer import normalize_markdown
from pipeline.ingest.pdf_formula_detector import (
    FormulaDetectionResult,
    FormulaDetectorConfig,
)
from pipeline.ingest.pdf_normalizer import normalize_pdf
from pipeline.ingest.source_package_materializer import materialize_source_package
from pipeline.ingest.txt_normalizer import normalize_txt


RECEIPT_SCHEMA_VERSION = "normalization_receipt_v1"
SUPPORTED_SUFFIXES = {
    ".epub": "epub",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".txt": "txt",
}


class UnifiedContractError(ValueError):
    pass


@dataclass(frozen=True)
class UnifiedNormalizationResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]
    receipt: dict[str, Any]


Normalizer = Callable[..., Any]


def detect_source_format(source_path: str | Path) -> str:
    source = Path(source_path)
    source_format = SUPPORTED_SUFFIXES.get(source.suffix.casefold())
    if source_format is None:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported source format {source.suffix or '<none>'}; expected one of: {supported}")
    return source_format


def _normalizer_for(source_format: str) -> Normalizer:
    return {
        "epub": normalize_epub,
        "html": normalize_html,
        "markdown": normalize_markdown,
        "pdf": normalize_pdf,
        "txt": normalize_txt,
    }[source_format]


def _flatten_blocks(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for chapter in document.get("chapters") or []
        for block in chapter.get("blocks") or []
    ]


def _require_nonempty_string(payload: dict[str, Any], field: str, *, owner: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise UnifiedContractError(f"{owner}.{field} must be a non-empty string")
    return value


def validate_normalization_contract(
    document: dict[str, Any],
    manifest: dict[str, Any],
    *,
    expected_format: str,
) -> dict[str, Any]:
    try:
        validate_locked_document(document)
    except DocumentContractError as exc:
        raise UnifiedContractError(str(exc)) from exc
    doc_id = _require_nonempty_string(document, "doc_id", owner="document")
    if manifest.get("doc_id") != doc_id:
        raise UnifiedContractError("document and structure manifest doc_id values differ")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise UnifiedContractError("document.metadata must be an object")
    if metadata.get("source_format") != expected_format:
        raise UnifiedContractError("document source_format does not match detected source format")

    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("format") != expected_format:
        raise UnifiedContractError("manifest source format does not match detected source format")
    source_sha256 = _require_nonempty_string(source, "sha256", owner="manifest.source")
    if metadata.get("raw_sha256") != source_sha256:
        raise UnifiedContractError("document raw_sha256 does not match manifest source sha256")
    structure_sha256 = _require_nonempty_string(
        manifest,
        "structure_sha256",
        owner="manifest",
    )

    chapters = document.get("chapters")
    units = manifest.get("units")
    if not isinstance(chapters, list) or not chapters:
        raise UnifiedContractError("document.chapters must be a non-empty list")
    if not isinstance(units, list) or len(units) != len(chapters):
        raise UnifiedContractError("manifest units must map one-to-one to document chapters")

    chapter_ids = [_require_nonempty_string(chapter, "chapter_id", owner="chapter") for chapter in chapters]
    if len(chapter_ids) != len(set(chapter_ids)):
        raise UnifiedContractError("document contains duplicate chapter_id values")
    unit_ids = [
        _require_nonempty_string(unit, "unit_id", owner="manifest.unit")
        for unit in units
    ]
    if len(unit_ids) != len(set(unit_ids)):
        raise UnifiedContractError("manifest contains duplicate unit_id values")
    unit_chapter_ids = [
        _require_nonempty_string(unit, "chapter_id", owner="manifest.unit")
        for unit in units
    ]
    if unit_chapter_ids != chapter_ids:
        raise UnifiedContractError("manifest unit chapter order differs from document chapter order")
    for unit in units:
        if not isinstance(unit.get("review_required"), bool):
            raise UnifiedContractError("every manifest unit must declare review_required")
        _require_nonempty_string(unit, "role", owner="manifest.unit")
        _require_nonempty_string(unit, "translation_policy", owner="manifest.unit")

    blocks = _flatten_blocks(document)
    if not blocks:
        raise UnifiedContractError("document contains no canonical blocks")
    block_ids = [_require_nonempty_string(block, "block_id", owner="block") for block in blocks]
    if len(block_ids) != len(set(block_ids)):
        raise UnifiedContractError("document contains duplicate block_id values")
    for block in blocks:
        if not isinstance(block.get("source_text"), str) or not isinstance(block.get("clean_text"), str):
            raise UnifiedContractError("every block must contain source_text and clean_text strings")
        if block.get("annotations") not in ({}, None):
            raise UnifiedContractError("normalized blocks must not contain runtime annotations")

    source_map = manifest.get("source_map")
    if not isinstance(source_map, list):
        raise UnifiedContractError("manifest.source_map must be a list")
    mapped_ids = [item.get("block_id") for item in source_map if isinstance(item, dict)]
    if mapped_ids != block_ids:
        raise UnifiedContractError("manifest source_map must cover every block once in document order")

    exact_cover = manifest.get("exact_cover")
    if not isinstance(exact_cover, dict):
        raise UnifiedContractError("manifest.exact_cover must be an object")
    if (
        exact_cover.get("coverage") != 1.0
        or exact_cover.get("overlap_count") != 0
        or exact_cover.get("missing_count") != 0
        or exact_cover.get("expected_blocks") != len(blocks)
        or exact_cover.get("covered_blocks") != len(blocks)
    ):
        raise UnifiedContractError("normalization must provide exact, non-overlapping block coverage")

    chapter_id_set = set(chapter_ids)
    translatable = manifest.get("translatable_chapter_ids")
    review_chapters = manifest.get("review_required_chapter_ids")
    review_units = manifest.get("review_required_unit_ids")
    if not isinstance(translatable, list) or not set(translatable).issubset(chapter_id_set):
        raise UnifiedContractError("translatable chapter ids must be a subset of document chapters")
    if not isinstance(review_chapters, list) or not set(review_chapters).issubset(chapter_id_set):
        raise UnifiedContractError("review chapter ids must be a subset of document chapters")
    if not isinstance(review_units, list):
        raise UnifiedContractError("review unit ids must be a list")
    if not set(review_units).issubset(set(unit_ids)):
        raise UnifiedContractError("review unit ids must be a subset of manifest units")

    expected_review_units = [
        unit["unit_id"]
        for unit in units
        if unit["review_required"]
    ]
    expected_review_chapters = [
        unit["chapter_id"]
        for unit in units
        if unit["review_required"]
    ]
    expected_translatable = [
        unit["chapter_id"]
        for unit in units
        if unit["role"] == "content_unit"
        and unit["translation_policy"] == "translate"
        and not unit["review_required"]
    ]
    if review_units != expected_review_units or review_chapters != expected_review_chapters:
        raise UnifiedContractError("review lists do not match manifest unit decisions")
    if translatable != expected_translatable:
        raise UnifiedContractError("translatable chapter ids do not match manifest unit decisions")

    if review_units:
        status = "review_required"
    elif translatable:
        status = "ready"
    else:
        status = "no_translatable_content"
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "source_format": expected_format,
        "source_sha256": source_sha256,
        "document_schema_version": document["schema_version"],
        "manifest_schema_version": manifest.get("schema_version"),
        "normalizer_version": manifest.get("normalizer_version"),
        "structure_sha256": structure_sha256,
        "status": status,
        "counts": {
            "units": len(units),
            "translatable_units": len(translatable),
            "review_required_units": len(review_units),
            "blocks": len(blocks),
        },
        "translatable_chapter_ids": list(translatable),
        "review_required_chapter_ids": list(review_chapters),
    }


def normalize_source(
    source_path: str | Path,
    *,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    pandoc_executable: str | None = "pandoc",
    pdf_formula_detector_mode: str = "disabled",
    pdf_formula_detector_config: FormulaDetectorConfig | None = None,
    pdf_formula_detector_executor: (
        Callable[[Path, FormulaDetectorConfig], FormulaDetectionResult] | None
    ) = None,
) -> UnifiedNormalizationResult:
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_format = detect_source_format(source)
    if source_format == "epub" and not pandoc_executable:
        raise ValueError("EPUB normalization requires a Pandoc executable")

    normalizer = _normalizer_for(source_format)
    normalizer_args: dict[str, Any] = {
        "doc_id": doc_id,
        "source_language": source_language,
        "target_language": target_language,
        "pandoc_executable": pandoc_executable,
    }
    if source_format == "pdf":
        normalizer_args.update(
            {
                "formula_detector_mode": pdf_formula_detector_mode,
                "formula_detector_config": pdf_formula_detector_config,
                "formula_detector_executor": pdf_formula_detector_executor,
            }
        )
    elif (
        pdf_formula_detector_mode != "disabled"
        or pdf_formula_detector_config is not None
        or pdf_formula_detector_executor is not None
    ):
        raise ValueError("PDF formula detector options require a PDF source")
    result = normalizer(source, **normalizer_args)
    document = result.document
    manifest = result.structure_manifest
    receipt = validate_normalization_contract(
        document,
        manifest,
        expected_format=source_format,
    )
    if hashlib.sha256(source.read_bytes()).hexdigest() != receipt["source_sha256"]:
        raise UnifiedContractError("source changed while normalization was running")
    return UnifiedNormalizationResult(
        document=document,
        structure_manifest=manifest,
        receipt=receipt,
    )


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_unified_normalization(
    result: UnifiedNormalizationResult,
    output_dir: str | Path,
) -> tuple[Path, Path, Path]:
    destination = Path(output_dir)
    document_path = destination / "document.json"
    manifest_path = destination / "structure_manifest.json"
    receipt_path = destination / "normalization_receipt.json"
    _atomic_json_write(document_path, result.document)
    _atomic_json_write(manifest_path, result.structure_manifest)
    _atomic_json_write(receipt_path, result.receipt)
    materialize_source_package(
        result.document,
        result.structure_manifest,
        destination,
    )
    return document_path, manifest_path, receipt_path


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "SUPPORTED_SUFFIXES",
    "UnifiedContractError",
    "UnifiedNormalizationResult",
    "detect_source_format",
    "normalize_source",
    "validate_normalization_contract",
    "write_unified_normalization",
]
