from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from pipeline.ingest.normalization_ir import looks_like_dialogue, normalize_text
from pipeline.ingest.pdf_formula_detector import (
    DETECTOR_SCHEMA_VERSION,
    FORMULA_LABELS,
    MODEL_LABELS,
    FormulaDetectionResult,
    FormulaDetectorConfig,
    FormulaRegion,
    PdfFormulaDetectorError,
    detect_pdf_formula_regions,
    formula_region_id,
)
from pipeline.ingest.pdf_formula_cluster import build_formula_cluster
from pipeline.ingest.pdf_opendataloader_adapter import (
    ConvertExecutor,
    PdfExtraction,
    extract_pdf,
)


DOCUMENT_SCHEMA_VERSION = "1.5.0"
MANIFEST_SCHEMA_VERSION = "pdf_structure_manifest_v1"
NORMALIZER_VERSION = "pdf_normalizer_v3"
OBJECT_REPLACEMENT_CHARACTER = "\uFFFC"
FORMULA_CLUSTER_ODL_BBOX_TOLERANCE_PT = 2.0

_CHILD_FIELDS = ("kids", "list items", "rows", "cells")
_RICH_NODE_TYPES = {"table", "image", "line art", "formula"}
_CONTAINER_TYPES = {"text block", "table row", "table cell"}
_TEXT_NODE_TYPES = {
    "caption",
    "footer",
    "header",
    "heading",
    "list item",
    "paragraph",
    "text",
}


class PdfNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class PdfOutlineEntry:
    level: int
    title: str
    page_number: int
    top_y: float | None


@dataclass(frozen=True)
class PdfGeometry:
    page_sizes: tuple[tuple[float, float], ...]
    outline: tuple[PdfOutlineEntry, ...]
    pymupdf_version: str


@dataclass(frozen=True)
class PdfNormalizationResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]


@dataclass(frozen=True)
class _PdfBlock:
    source_kind: str
    text: str
    page_number: int
    bbox_pdf: tuple[float, float, float, float]
    odl_path: str
    odl_node_id: str | None
    heading_level: int | None
    has_extracted_text: bool
    rich_payload: dict[str, Any] | None
    formula_detection: dict[str, Any] | None = None
    original_source_kind: str | None = None
    original_bbox_pdf: tuple[float, float, float, float] | None = None
    force_review: bool = False
    formula_cluster_seed: dict[str, Any] | None = None


GeometryReader = Callable[[Path], PdfGeometry]
FormulaDetectorExecutor = Callable[
    [Path, FormulaDetectorConfig],
    FormulaDetectionResult,
]


def _slug(value: str, *, fallback: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = ascii_value.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")
    return slug[:60] or fallback


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_pdf_geometry(source: Path) -> PdfGeometry:
    try:
        import pymupdf
    except ImportError as exc:
        raise PdfNormalizationError(
            "PyMuPDF is unavailable for PDF page geometry and bookmarks"
        ) from exc
    try:
        version = importlib.metadata.version("PyMuPDF")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PdfNormalizationError("PyMuPDF version identity is unavailable") from exc

    document = pymupdf.open(source)
    try:
        page_sizes = tuple(
            (round(float(page.rect.width), 3), round(float(page.rect.height), 3))
            for page in document
        )
        outline_rows: list[PdfOutlineEntry] = []
        for row in document.get_toc(simple=False):
            if not isinstance(row, list) or len(row) < 3:
                continue
            level, title, page_number = row[:3]
            if (
                isinstance(level, bool)
                or not isinstance(level, int)
                or level < 1
                or isinstance(page_number, bool)
                or not isinstance(page_number, int)
                or page_number < 1
                or page_number > len(page_sizes)
            ):
                continue
            title_value = re.sub(r"\s+", " ", str(title or "")).strip()
            if not title_value:
                continue
            top_y: float | None = None
            if len(row) >= 4 and isinstance(row[3], dict):
                point = row[3].get("to")
                y_value = getattr(point, "y", None)
                if y_value is not None:
                    try:
                        candidate = float(y_value)
                    except (TypeError, ValueError):
                        candidate = math.nan
                    if math.isfinite(candidate):
                        top_y = round(candidate, 3)
            outline_rows.append(
                PdfOutlineEntry(
                    level=level,
                    title=title_value,
                    page_number=page_number,
                    top_y=top_y,
                )
            )
    finally:
        document.close()
    return PdfGeometry(
        page_sizes=page_sizes,
        outline=tuple(outline_rows),
        pymupdf_version=version,
    )


def _iter_children(node: dict[str, Any]) -> Iterable[tuple[str, int, dict[str, Any]]]:
    for field in _CHILD_FIELDS:
        children = node.get(field)
        if not isinstance(children, list):
            continue
        for index, child in enumerate(children):
            if isinstance(child, dict):
                yield field, index, child


def _all_descendants(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for _field, _index, child in _iter_children(node):
        yield from _all_descendants(child)


def _content_rows(node: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for descendant in _all_descendants(node):
        content = descendant.get("content")
        if not isinstance(content, str):
            continue
        value = content.strip()
        if value and (not rows or rows[-1] != value):
            rows.append(value)
    return rows


def _bbox(value: Any, *, owner: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise PdfNormalizationError(f"{owner} has no valid PDF bounding box")
    coordinates: list[float] = []
    for part in value:
        if isinstance(part, bool):
            raise PdfNormalizationError(f"{owner} bounding box is invalid")
        try:
            coordinate = float(part)
        except (TypeError, ValueError) as exc:
            raise PdfNormalizationError(f"{owner} bounding box is invalid") from exc
        if not math.isfinite(coordinate):
            raise PdfNormalizationError(f"{owner} bounding box is not finite")
        coordinates.append(round(coordinate, 3))
    x0, y0, x1, y1 = coordinates
    if x1 <= x0 or y1 <= y0:
        raise PdfNormalizationError(f"{owner} bounding box has no positive area")
    return x0, y0, x1, y1


def _page_number(node: dict[str, Any], *, owner: str) -> int:
    value = node.get("page number")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PdfNormalizationError(f"{owner} has no valid page number")
    return value


def _node_id(node: dict[str, Any]) -> str | None:
    value = node.get("id")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _source_kind(node_type: str) -> str:
    return {
        "formula": "equation",
        "line art": "image",
        "list": "list_item",
        "list item": "list_item",
        "text": "paragraph",
        "text block": "paragraph",
    }.get(node_type, node_type)


def _rich_type(node: dict[str, Any]) -> str | None:
    for descendant in _all_descendants(node):
        node_type = str(descendant.get("type") or "").strip().casefold()
        if node_type in _RICH_NODE_TYPES:
            return node_type
    return None


def _normalized_block_text(source_kind: str, rows: list[str]) -> tuple[str, bool]:
    if not rows:
        return OBJECT_REPLACEMENT_CHARACTER, False
    separator = "\n" if source_kind in {"table", "equation", "list_item"} else " "
    value = normalize_text(separator.join(rows), source_kind)
    return (value or OBJECT_REPLACEMENT_CHARACTER), bool(value)


def _rich_payload(node: dict[str, Any], rich_type: str) -> dict[str, Any]:
    rich_nodes = [
        copy.deepcopy(descendant)
        for descendant in _all_descendants(node)
        if str(descendant.get("type") or "").strip().casefold() == rich_type
    ]
    return {
        "grouping_node": {
            key: copy.deepcopy(value)
            for key, value in node.items()
            if key not in _CHILD_FIELDS
        },
        "rich_nodes": rich_nodes,
    }


def _emit_block(
    node: dict[str, Any],
    *,
    path: str,
    forced_type: str | None = None,
) -> _PdfBlock:
    node_type = forced_type or str(node.get("type") or "").strip().casefold()
    source_kind = _source_kind(node_type)
    rows = _content_rows(node)
    text, has_extracted_text = _normalized_block_text(source_kind, rows)
    heading_level: int | None = None
    if source_kind == "heading":
        candidate = node.get("heading level")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 1:
            heading_level = candidate
    rich_payload = (
        _rich_payload(node, node_type)
        if node_type in _RICH_NODE_TYPES
        else None
    )
    return _PdfBlock(
        source_kind=source_kind,
        text=text,
        page_number=_page_number(node, owner=path),
        bbox_pdf=_bbox(node.get("bounding box"), owner=path),
        odl_path=path,
        odl_node_id=_node_id(node),
        heading_level=heading_level,
        has_extracted_text=has_extracted_text,
        rich_payload=rich_payload,
    )


def _flatten_node(node: dict[str, Any], *, path: str) -> list[_PdfBlock]:
    node_type = str(node.get("type") or "").strip().casefold()
    if node_type == "text block":
        rich_type = _rich_type(node)
        if rich_type is not None:
            return [_emit_block(node, path=path, forced_type=rich_type)]
        children = list(_iter_children(node))
        if children:
            return [
                block
                for field, index, child in children
                for block in _flatten_node(
                    child,
                    path=f"{path}/{field}[{index}]",
                )
            ]
        return [_emit_block(node, path=path)]
    if node_type == "list":
        return [_emit_block(node, path=path)]
    if node_type in _RICH_NODE_TYPES or node_type in _TEXT_NODE_TYPES:
        return [_emit_block(node, path=path)]
    if node_type in _CONTAINER_TYPES:
        return [
            block
            for field, index, child in _iter_children(node)
            for block in _flatten_node(
                child,
                path=f"{path}/{field}[{index}]",
            )
        ]
    raise PdfNormalizationError(f"unsupported normalized PDF node type: {node_type}")


def _flatten_payload(payload: dict[str, Any]) -> tuple[_PdfBlock, ...]:
    blocks = tuple(
        block
        for index, node in enumerate(payload["kids"])
        for block in _flatten_node(node, path=f"/kids[{index}]")
    )
    if not blocks:
        raise PdfNormalizationError("PDF parser produced no canonical blocks")
    pages = [block.page_number for block in blocks]
    if pages != sorted(pages):
        raise PdfNormalizationError("PDF parser reading order moves backwards across pages")
    paths = [block.odl_path for block in blocks]
    if len(paths) != len(set(paths)):
        raise PdfNormalizationError("PDF parser emitted duplicate structural paths")
    return blocks


def _top_y(block: _PdfBlock, page_height: float) -> float:
    return page_height - block.bbox_pdf[3]


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _bbox_intersection(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _formula_candidates(
    blocks: tuple[_PdfBlock, ...],
    region: FormulaRegion,
) -> list[int]:
    region_area = _bbox_area(region.bbox_pdf)
    candidates: list[int] = []
    for index, block in enumerate(blocks):
        if block.page_number != region.page_number:
            continue
        intersection = _bbox_intersection(block.bbox_pdf, region.bbox_pdf)
        if intersection <= 0.0:
            continue
        block_area = _bbox_area(block.bbox_pdf)
        overlap = max(
            intersection / max(region_area, 1e-9),
            intersection / max(block_area, 1e-9),
        )
        if overlap >= 0.10:
            candidates.append(index)
    return candidates


def _caption_has_formula_peer(
    region: FormulaRegion,
    regions: tuple[FormulaRegion, ...],
    geometry: PdfGeometry,
) -> bool:
    if region.label != "formula_caption":
        return True
    page_width, page_height = geometry.page_sizes[region.page_number - 1]
    for candidate in regions:
        if (
            candidate.page_number != region.page_number
            or candidate.label != "isolate_formula"
        ):
            continue
        vertical_overlap = max(
            0.0,
            min(region.bbox_pdf[3], candidate.bbox_pdf[3])
            - max(region.bbox_pdf[1], candidate.bbox_pdf[1]),
        )
        minimum_height = min(
            region.bbox_pdf[3] - region.bbox_pdf[1],
            candidate.bbox_pdf[3] - candidate.bbox_pdf[1],
        )
        vertical_ratio = vertical_overlap / max(minimum_height, 1e-9)
        horizontal_gap = max(
            0.0,
            candidate.bbox_pdf[0] - region.bbox_pdf[2],
            region.bbox_pdf[0] - candidate.bbox_pdf[2],
        )
        if vertical_ratio >= 0.25 and horizontal_gap <= page_width * 0.25:
            return True

        horizontal_overlap = max(
            0.0,
            min(region.bbox_pdf[2], candidate.bbox_pdf[2])
            - max(region.bbox_pdf[0], candidate.bbox_pdf[0]),
        )
        horizontal_union = max(
            region.bbox_pdf[2],
            candidate.bbox_pdf[2],
        ) - min(region.bbox_pdf[0], candidate.bbox_pdf[0])
        horizontal_ratio = horizontal_overlap / max(horizontal_union, 1e-9)
        vertical_gap = max(
            0.0,
            candidate.bbox_pdf[1] - region.bbox_pdf[3],
            region.bbox_pdf[1] - candidate.bbox_pdf[3],
        )
        if horizontal_ratio >= 0.05 and vertical_gap <= page_height * 0.12:
            return True
    return False


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    tolerance: float = FORMULA_CLUSTER_ODL_BBOX_TOLERANCE_PT,
) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _bbox_union(
    boxes: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    if not boxes:
        raise PdfNormalizationError("formula cluster geometry is empty")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _auto_preserved_formula_clusters(
    blocks: tuple[_PdfBlock, ...],
    regions: tuple[FormulaRegion, ...],
    *,
    candidate_map: dict[str, list[int]],
    block_regions: dict[int, list[FormulaRegion]],
    geometry: PdfGeometry,
    config: FormulaDetectorConfig,
) -> list[tuple[int, tuple[FormulaRegion, ...]]]:
    clusters: list[tuple[int, tuple[FormulaRegion, ...]]] = []
    claimed_blocks: set[int] = set()
    claimed_regions: set[str] = set()
    captions = tuple(region for region in regions if region.label == "formula_caption")

    for formula in regions:
        if formula.label != "isolate_formula" or formula.region_id in claimed_regions:
            continue
        formula_candidates = candidate_map[formula.region_id]
        if (
            formula.confidence < config.acceptance_threshold
            or len(formula_candidates) != 1
        ):
            continue
        block_index = formula_candidates[0]
        block = blocks[block_index]
        if (
            block_index in claimed_blocks
            or block.source_kind not in {"paragraph", "equation"}
            or not _bbox_contains(block.bbox_pdf, formula.bbox_pdf)
        ):
            continue

        linked_captions = [
            caption
            for caption in captions
            if caption.page_number == formula.page_number
            and _caption_has_formula_peer(caption, (formula,), geometry)
        ]
        if len(linked_captions) > 1:
            continue
        members: tuple[FormulaRegion, ...]
        if linked_captions:
            caption = linked_captions[0]
            if (
                caption.confidence < config.acceptance_threshold
                or candidate_map[caption.region_id] != [block_index]
                or not _bbox_contains(block.bbox_pdf, caption.bbox_pdf)
            ):
                continue
            members = (formula, caption)
        else:
            members = (formula,)

        member_ids = {region.region_id for region in members}
        competing_ids = {
            region.region_id for region in block_regions.get(block_index, [])
        }
        if competing_ids != member_ids:
            continue
        if any(
            candidate_map[region.region_id] != [block_index]
            for region in members
        ):
            continue

        clusters.append((block_index, members))
        claimed_blocks.add(block_index)
        claimed_regions.update(member_ids)
    return clusters


def _validate_formula_result(
    result: FormulaDetectionResult,
    *,
    source_sha256: str,
    geometry: PdfGeometry,
    config: FormulaDetectorConfig,
) -> None:
    if result.source_sha256 != source_sha256:
        raise PdfNormalizationError("formula detector source identity mismatch")
    if result.page_count != len(geometry.page_sizes):
        raise PdfNormalizationError("formula detector page count mismatch")
    manifest = result.detector_manifest
    if manifest.get("schema_version") != DETECTOR_SCHEMA_VERSION:
        raise PdfNormalizationError("formula detector manifest schema drifted")
    if (
        manifest.get("mode") != "required"
        or manifest.get("status") != "completed"
    ):
        raise PdfNormalizationError("formula detector did not complete required mode")
    if manifest.get("source_sha256") != result.source_sha256:
        raise PdfNormalizationError("formula detector manifest source identity mismatch")
    if manifest.get("page_count") != result.page_count:
        raise PdfNormalizationError("formula detector manifest page count mismatch")
    if manifest.get("region_count") != len(result.regions):
        raise PdfNormalizationError("formula detector manifest region count mismatch")
    if manifest.get("regions") != [
        region.as_dict() for region in result.regions
    ]:
        raise PdfNormalizationError("formula detector manifest region rows mismatch")
    expected_model = {
        "repo": config.model_repo,
        "revision": config.model_revision,
        "filename": config.model_filename,
        "sha256": config.expected_model_sha256,
        "declared_license": config.model_declared_license,
        "redistribution_reviewed": False,
    }
    if manifest.get("model") != expected_model:
        raise PdfNormalizationError("formula detector manifest model identity mismatch")
    runtime = manifest.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("provider") != config.provider
        or not isinstance(runtime.get("onnxruntime_version"), str)
        or not runtime["onnxruntime_version"]
        or not isinstance(runtime.get("pymupdf_version"), str)
        or not runtime["pymupdf_version"]
    ):
        raise PdfNormalizationError("formula detector runtime identity mismatch")
    expected_preprocessing = {
        "raster_dpi": config.raster_dpi,
        "color_mode": config.color_mode,
        "input_size": config.input_size,
        "normalization": "uint8_rgb_div_255",
        "resize": "aspect_fit_bilinear",
        "letterbox_fill": 114,
        "coordinate_transform": "raster_top_left_to_pdf_bottom_left",
    }
    if manifest.get("preprocessing") != expected_preprocessing:
        raise PdfNormalizationError("formula detector preprocessing identity mismatch")
    expected_postprocessing = {
        "label_map": {str(key): value for key, value in MODEL_LABELS.items()},
        "included_labels": sorted(FORMULA_LABELS),
        "confidence_threshold": config.confidence_threshold,
        "acceptance_threshold": config.acceptance_threshold,
        "nms_iou_threshold": config.nms_iou_threshold,
        "nms_mode": "classwise_deterministic",
    }
    if manifest.get("postprocessing") != expected_postprocessing:
        raise PdfNormalizationError("formula detector postprocessing identity mismatch")
    region_ids: list[str] = []
    expected_order = sorted(
        result.regions,
        key=lambda region: (
            region.page_number,
            -region.bbox_pdf[3],
            region.bbox_pdf[0],
            region.label,
            region.region_id,
        ),
    )
    if list(result.regions) != expected_order:
        raise PdfNormalizationError("formula detector region order drifted")
    for region in result.regions:
        region_ids.append(region.region_id)
        if region.label not in FORMULA_LABELS:
            raise PdfNormalizationError("formula detector emitted a foreign label")
        if MODEL_LABELS.get(region.class_id) != region.label:
            raise PdfNormalizationError("formula detector class and label disagree")
        if (
            region.page_number < 1
            or region.page_number > len(geometry.page_sizes)
        ):
            raise PdfNormalizationError("formula detector emitted a foreign page")
        page_width, page_height = geometry.page_sizes[region.page_number - 1]
        x0, y0, x1, y1 = region.bbox_pdf
        if (
            not all(math.isfinite(value) for value in region.bbox_pdf)
            or x0 < 0.0
            or y0 < 0.0
            or x1 > page_width
            or y1 > page_height
            or x1 <= x0
            or y1 <= y0
        ):
            raise PdfNormalizationError("formula detector emitted an invalid PDF box")
        if not 0.0 <= region.confidence <= 1.0:
            raise PdfNormalizationError("formula detector emitted invalid confidence")
        if region.confidence < config.confidence_threshold:
            raise PdfNormalizationError(
                "formula detector emitted a below-threshold region"
            )
        if region.region_id != formula_region_id(
            page_number=region.page_number,
            label=region.label,
            bbox_pdf=region.bbox_pdf,
        ):
            raise PdfNormalizationError("formula detector region id mismatch")
        left, top, width, height = region.bbox_view
        if (
            not all(math.isfinite(value) for value in region.bbox_view)
            or left < 0.0
            or top < 0.0
            or width <= 0.0
            or height <= 0.0
            or left + width > 1.000001
            or top + height > 1.000001
        ):
            raise PdfNormalizationError("formula detector emitted invalid view box")
        reconstructed_pdf = (
            left * page_width,
            page_height - ((top + height) * page_height),
            (left + width) * page_width,
            page_height - (top * page_height),
        )
        if any(
            abs(actual - expected) > 0.01
            for actual, expected in zip(reconstructed_pdf, region.bbox_pdf)
        ):
            raise PdfNormalizationError("formula detector coordinate transform mismatch")
    if len(region_ids) != len(set(region_ids)):
        raise PdfNormalizationError("formula detector emitted duplicate region ids")


def _synthetic_formula_block(
    region: FormulaRegion,
    *,
    status: str,
    force_review: bool,
) -> _PdfBlock:
    evidence = {
        "region": region.as_dict(),
        "fusion_status": status,
        "odl_match_count": 0,
    }
    return _PdfBlock(
        source_kind="equation",
        text=OBJECT_REPLACEMENT_CHARACTER,
        page_number=region.page_number,
        bbox_pdf=region.bbox_pdf,
        odl_path=f"/visual_formula[{region.region_id}]",
        odl_node_id=None,
        heading_level=None,
        has_extracted_text=False,
        rich_payload={"visual_formula_region": region.as_dict()},
        formula_detection=evidence,
        original_source_kind=None,
        original_bbox_pdf=None,
        force_review=force_review,
    )


def _formula_insert_index(
    blocks: tuple[_PdfBlock, ...],
    region: FormulaRegion,
    geometry: PdfGeometry,
) -> int:
    page_indexes = [
        index for index, block in enumerate(blocks)
        if block.page_number == region.page_number
    ]
    if not page_indexes:
        raise PdfNormalizationError("formula detector page has no parser blocks")
    page_height = geometry.page_sizes[region.page_number - 1][1]
    region_top = page_height - region.bbox_pdf[3]
    for index in page_indexes:
        if _top_y(blocks[index], page_height) >= region_top:
            return index
    return page_indexes[-1] + 1


def _apply_formula_detection(
    blocks: tuple[_PdfBlock, ...],
    result: FormulaDetectionResult,
    *,
    geometry: PdfGeometry,
    config: FormulaDetectorConfig,
) -> tuple[tuple[_PdfBlock, ...], dict[str, Any]]:
    candidate_map = {
        region.region_id: _formula_candidates(blocks, region)
        for region in result.regions
    }
    block_regions: dict[int, list[FormulaRegion]] = {}
    for region in result.regions:
        for block_index in candidate_map[region.region_id]:
            block_regions.setdefault(block_index, []).append(region)

    rewritten = list(blocks)
    insertions: dict[int, list[_PdfBlock]] = {}
    accepted = 0
    review_required = 0
    synthetic = 0
    auto_preserved_clusters = 0
    auto_preserved_regions = 0
    auto_preserved_members = 0
    protected_odl_blocks: set[int] = set()
    consumed_region_ids: set[str] = set()

    for block_index, member_regions in _auto_preserved_formula_clusters(
        blocks,
        result.regions,
        candidate_map=candidate_map,
        block_regions=block_regions,
        geometry=geometry,
        config=config,
    ):
        original = rewritten[block_index]
        group_region_ids = [region.region_id for region in member_regions]
        publication_bbox = _bbox_union(
            (original.bbox_pdf,)
            + tuple(region.bbox_pdf for region in member_regions)
        )
        seed_common = {
            "group_region_ids": group_region_ids,
            "publication_bbox_pdf": list(publication_bbox),
            "page_number": original.page_number,
        }
        rewritten[block_index] = replace(
            original,
            source_kind="equation",
            bbox_pdf=publication_bbox,
            formula_detection={
                "regions": [region.as_dict() for region in member_regions],
                "fusion_status": "auto_preserved_formula_cluster",
                "odl_match_count": 1,
            },
            original_source_kind=(
                original.original_source_kind or original.source_kind
            ),
            original_bbox_pdf=(original.original_bbox_pdf or original.bbox_pdf),
            force_review=False,
            formula_cluster_seed={
                **seed_common,
                "member_region_ids": group_region_ids,
                "member_role": "publication_visual",
            },
        )
        for region in member_regions:
            synthetic_block = _synthetic_formula_block(
                region,
                status="auto_preserved_formula_cluster",
                force_review=False,
            )
            synthetic_block = replace(
                synthetic_block,
                formula_detection={
                    **(synthetic_block.formula_detection or {}),
                    "odl_match_count": 1,
                },
                formula_cluster_seed={
                    **seed_common,
                    "member_region_ids": [region.region_id],
                    "member_role": "duplicate_evidence",
                },
            )
            insertions.setdefault(block_index, []).append(synthetic_block)
        protected_odl_blocks.add(block_index)
        consumed_region_ids.update(group_region_ids)
        synthetic += len(member_regions)
        auto_preserved_clusters += 1
        auto_preserved_regions += len(member_regions)
        auto_preserved_members += len(member_regions) + 1

    for region in result.regions:
        if region.region_id in consumed_region_ids:
            continue
        candidates = candidate_map[region.region_id]
        caption_has_peer = _caption_has_formula_peer(
            region,
            result.regions,
            geometry,
        )
        high_confidence = region.confidence >= config.acceptance_threshold
        uniquely_owned = (
            len(candidates) == 1
            and len(block_regions.get(candidates[0], [])) == 1
        )
        if high_confidence and caption_has_peer and uniquely_owned:
            block_index = candidates[0]
            original = rewritten[block_index]
            rewritten[block_index] = replace(
                original,
                source_kind="equation",
                bbox_pdf=region.bbox_pdf,
                formula_detection={
                    "region": region.as_dict(),
                    "fusion_status": "accepted_unique_overlap",
                    "odl_match_count": 1,
                },
                original_source_kind=original.source_kind,
                original_bbox_pdf=original.bbox_pdf,
            )
            protected_odl_blocks.add(block_index)
            accepted += 1
            continue

        status = (
            "low_confidence"
            if not high_confidence
            else "orphan_formula_caption"
            if not caption_has_peer
            else "ambiguous_odl_overlap"
            if candidates
            else "unmatched_visual_formula"
        )
        for block_index in candidates:
            original = rewritten[block_index]
            prior_regions = (
                list((original.formula_detection or {}).get("regions") or [])
            )
            prior_regions.append(region.as_dict())
            rewritten[block_index] = replace(
                original,
                source_kind="formula_fragment",
                formula_detection={
                    "regions": prior_regions,
                    "fusion_status": status,
                    "odl_match_count": len(candidates),
                },
                original_source_kind=(
                    original.original_source_kind or original.source_kind
                ),
                original_bbox_pdf=(
                    original.original_bbox_pdf or original.bbox_pdf
                ),
                force_review=True,
            )
            protected_odl_blocks.add(block_index)

        insertion_index = (
            min(candidates)
            if candidates
            else _formula_insert_index(blocks, region, geometry)
        )
        synthetic_block = _synthetic_formula_block(
            region,
            status=status,
            force_review=True,
        )
        synthetic_block = replace(
            synthetic_block,
            formula_detection={
                **(synthetic_block.formula_detection or {}),
                "odl_match_count": len(candidates),
            },
        )
        insertions.setdefault(insertion_index, []).append(synthetic_block)
        synthetic += 1
        review_required += 1

    fused: list[_PdfBlock] = []
    for index, block in enumerate(rewritten):
        rows = insertions.get(index, [])
        rows.sort(
            key=lambda item: (
                -item.bbox_pdf[3],
                item.bbox_pdf[0],
                item.odl_path,
            )
        )
        fused.extend(rows)
        fused.append(block)
    rows = insertions.get(len(rewritten), [])
    rows.sort(
        key=lambda item: (
            item.page_number,
            -item.bbox_pdf[3],
            item.bbox_pdf[0],
            item.odl_path,
        )
    )
    fused.extend(rows)
    return tuple(fused), {
        "accepted_region_count": accepted,
        "review_required_region_count": review_required,
        "synthetic_crop_block_count": synthetic,
        "protected_odl_block_count": len(protected_odl_blocks),
        "auto_preserved_cluster_count": auto_preserved_clusters,
        "auto_preserved_region_count": auto_preserved_regions,
        "auto_preserved_member_count": auto_preserved_members,
        "output_block_count": len(fused),
    }


def _outline_boundaries(
    blocks: tuple[_PdfBlock, ...],
    geometry: PdfGeometry,
) -> list[tuple[int, str]]:
    counts = Counter(entry.level for entry in geometry.outline)
    boundary_levels = sorted(level for level, count in counts.items() if count >= 2)
    if not boundary_levels:
        return []
    level = boundary_levels[0]
    boundaries: list[tuple[int, str]] = []
    for entry in geometry.outline:
        if entry.level != level:
            continue
        page_indexes = [
            index
            for index, block in enumerate(blocks)
            if block.page_number == entry.page_number
        ]
        if not page_indexes:
            continue
        if entry.top_y is None:
            block_index = page_indexes[0]
        else:
            page_height = geometry.page_sizes[entry.page_number - 1][1]
            heading_indexes = [
                index for index in page_indexes if blocks[index].source_kind == "heading"
            ]
            candidates = heading_indexes or page_indexes
            block_index = min(
                candidates,
                key=lambda index: abs(_top_y(blocks[index], page_height) - entry.top_y),
            )
        if boundaries and boundaries[-1][0] == block_index:
            continue
        boundaries.append((block_index, entry.title))
    if len(boundaries) < 2:
        return []
    indexes = [index for index, _title in boundaries]
    if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
        return []
    return boundaries


def _heading_boundaries(blocks: tuple[_PdfBlock, ...]) -> list[tuple[int, str]]:
    counts = Counter(
        block.heading_level
        for block in blocks
        if block.source_kind == "heading" and block.heading_level is not None
    )
    levels = sorted(level for level, count in counts.items() if count >= 2)
    if not levels:
        return []
    level = levels[0]
    return [
        (index, block.text)
        for index, block in enumerate(blocks)
        if block.source_kind == "heading" and block.heading_level == level
    ]


def _unit_rows(
    blocks: tuple[_PdfBlock, ...],
    geometry: PdfGeometry,
) -> tuple[list[dict[str, Any]], str]:
    boundaries = _outline_boundaries(blocks, geometry)
    evidence = "pdf_bookmark"
    if not boundaries:
        boundaries = _heading_boundaries(blocks)
        evidence = "repeated_parser_heading_level"
    if not boundaries:
        title = next(
            (block.text for block in blocks if block.source_kind == "heading"),
            "Document",
        )
        return (
            [
                {
                    "title": title,
                    "start_block": 0,
                    "end_block": len(blocks),
                    "role": "unknown",
                    "translation_policy": "review",
                    "confidence": 0.0,
                    "evidence": ["no_reliable_bookmark_or_heading_boundary"],
                    "review_required": True,
                }
            ],
            "synthetic_document_unit",
        )

    rows: list[dict[str, Any]] = []
    first_boundary = boundaries[0][0]
    if first_boundary:
        rows.append(
            {
                "title": next(
                    (
                        block.text
                        for block in blocks[:first_boundary]
                        if block.source_kind == "heading"
                    ),
                    "Front matter",
                ),
                "start_block": 0,
                "end_block": first_boundary,
                "role": "front_matter",
                "translation_policy": "preserve",
                "confidence": 1.0,
                "evidence": [f"prefix_before_{evidence}"],
                "review_required": False,
            }
        )
    for position, (start, title) in enumerate(boundaries):
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(blocks)
        if end <= start:
            raise PdfNormalizationError("PDF unit boundaries are not strictly ordered")
        rows.append(
            {
                "title": title,
                "start_block": start,
                "end_block": end,
                "role": "content_unit",
                "translation_policy": "translate",
                "confidence": 1.0 if evidence == "pdf_bookmark" else 0.9,
                "evidence": [evidence],
                "review_required": False,
            }
        )
    return rows, evidence


def _runtime_kind(block: _PdfBlock) -> str:
    if block.source_kind == "heading":
        return "heading"
    if block.source_kind == "footnote":
        return "footnote"
    if block.source_kind == "paragraph" and looks_like_dialogue(block.text):
        return "dialogue"
    return "paragraph"


def _block_policy(block: _PdfBlock) -> tuple[str, bool]:
    if block.force_review:
        return "review", True
    if (
        (block.formula_detection or {}).get("fusion_status")
        == "auto_preserved_formula_cluster"
    ):
        return "preserve", False
    if block.source_kind == "table":
        return "translate_structured", not block.has_extracted_text
    if block.source_kind in {"image", "equation"}:
        return "preserve", not block.has_extracted_text
    if block.source_kind in {"header", "footer"}:
        return "preserve", False
    if block.source_kind not in {
        "caption",
        "heading",
        "list_item",
        "paragraph",
    }:
        return "review", True
    return "translate", False


def _planned_block_ids(
    raw_units: list[dict[str, Any]],
    blocks: tuple[_PdfBlock, ...],
    *,
    doc_slug: str,
) -> list[str]:
    block_ids = [""] * len(blocks)
    for unit_index, row in enumerate(raw_units):
        unit_id = f"u{unit_index + 1:04d}_{_slug(row['title'], fallback='unit')}"
        chapter_id = f"{doc_slug}_{unit_id}"
        for local_index, block_index in enumerate(
            range(row["start_block"], row["end_block"])
        ):
            block_ids[block_index] = f"{chapter_id}_b{local_index + 1:04d}"
    if any(not block_id for block_id in block_ids):
        raise PdfNormalizationError("PDF block ids do not exact-cover parser blocks")
    return block_ids


def _finalize_formula_clusters(
    blocks: tuple[_PdfBlock, ...],
    block_ids: list[str],
    *,
    doc_id: str,
    source_sha256: str,
) -> tuple[_PdfBlock, ...]:
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, block in enumerate(blocks):
        seed = block.formula_cluster_seed
        if seed is None:
            continue
        key = tuple(str(value) for value in seed.get("group_region_ids") or [])
        if not key:
            raise PdfNormalizationError("formula cluster seed has no detector regions")
        groups.setdefault(key, []).append(index)

    finalized = list(blocks)
    for detector_region_ids, indexes in groups.items():
        first_seed = blocks[indexes[0]].formula_cluster_seed or {}
        page_number = int(first_seed.get("page_number") or 0)
        publication_bbox = first_seed.get("publication_bbox_pdf")
        members: list[dict[str, Any]] = []
        publication_ids: list[str] = []
        for index in indexes:
            block = blocks[index]
            seed = block.formula_cluster_seed or {}
            if (
                tuple(seed.get("group_region_ids") or ()) != detector_region_ids
                or seed.get("page_number") != page_number
                or seed.get("publication_bbox_pdf") != publication_bbox
            ):
                raise PdfNormalizationError("formula cluster seed identity drifted")
            role = str(seed.get("member_role") or "")
            if role == "publication_visual":
                publication_ids.append(block_ids[index])
            members.append(
                {
                    "block_id": block_ids[index],
                    "role": role,
                    "bbox_pdf": list(block.bbox_pdf),
                    "detector_region_ids": list(
                        seed.get("member_region_ids") or []
                    ),
                }
            )
        if len(publication_ids) != 1:
            raise PdfNormalizationError(
                "formula cluster must have exactly one publication visual"
            )
        try:
            cluster = build_formula_cluster(
                doc_id=doc_id,
                source_sha256=source_sha256,
                normalizer_version=NORMALIZER_VERSION,
                page_number=page_number,
                detector_region_ids=list(detector_region_ids),
                members=members,
                publication_block_id=publication_ids[0],
                publication_bbox_pdf=publication_bbox,
            )
        except ValueError as exc:
            raise PdfNormalizationError(str(exc)) from exc
        for index in indexes:
            block = finalized[index]
            seed = block.formula_cluster_seed or {}
            finalized[index] = replace(
                block,
                formula_detection={
                    **(block.formula_detection or {}),
                    "formula_cluster": copy.deepcopy(cluster),
                    "cluster_member_role": seed["member_role"],
                },
                formula_cluster_seed=None,
            )
    return tuple(finalized)


def _exact_cover(unit_rows: list[dict[str, Any]], block_count: int) -> dict[str, Any]:
    coverage: list[int] = []
    for row in unit_rows:
        coverage.extend(range(row["start_block"], row["end_block"]))
    expected = list(range(block_count))
    if coverage != expected:
        raise PdfNormalizationError("PDF units do not exact-cover parser blocks")
    return {
        "expected_blocks": block_count,
        "covered_blocks": len(coverage),
        "coverage": 1.0,
        "overlap_count": 0,
        "missing_count": 0,
    }


def normalize_pdf(
    source_path: str | Path,
    *,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    pandoc_executable: str | None = None,
    convert_executor: ConvertExecutor | None = None,
    package_version: str | None = None,
    java_version: str | None = None,
    geometry_reader: GeometryReader | None = None,
    formula_detector_mode: str = "disabled",
    formula_detector_config: FormulaDetectorConfig | None = None,
    formula_detector_executor: FormulaDetectorExecutor | None = None,
) -> PdfNormalizationResult:
    del pandoc_executable
    source = Path(source_path).resolve()
    if source.suffix.casefold() != ".pdf":
        raise ValueError("PDF normalizer requires a .pdf source")
    if not source.is_file():
        raise FileNotFoundError(source)
    if formula_detector_mode not in {"disabled", "required"}:
        raise PdfNormalizationError(
            "formula_detector_mode must be disabled or required"
        )
    if formula_detector_mode == "disabled" and (
        formula_detector_config is not None
        or formula_detector_executor is not None
    ):
        raise PdfNormalizationError(
            "disabled formula detector mode cannot accept detector configuration"
        )
    if formula_detector_mode == "required" and formula_detector_config is None:
        raise PdfNormalizationError(
            "required formula detector mode needs an explicit model configuration"
        )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    extraction: PdfExtraction = extract_pdf(
        source,
        convert_executor=convert_executor,
        package_version=package_version,
        java_version=java_version,
    )
    geometry = (geometry_reader or _read_pdf_geometry)(source)
    page_count = extraction.payload["number of pages"]
    if len(geometry.page_sizes) != page_count:
        raise PdfNormalizationError(
            "OpenDataLoader and PyMuPDF page counts differ"
        )
    blocks = _flatten_payload(extraction.payload)
    if any(block.page_number > page_count for block in blocks):
        raise PdfNormalizationError("PDF block references a foreign page")

    if formula_detector_mode == "required":
        assert formula_detector_config is not None
        detector = formula_detector_executor or detect_pdf_formula_regions
        try:
            formula_result = detector(source, formula_detector_config)
        except PdfFormulaDetectorError as exc:
            raise PdfNormalizationError(str(exc)) from exc
        _validate_formula_result(
            formula_result,
            source_sha256=source_sha256,
            geometry=geometry,
            config=formula_detector_config,
        )
        blocks, fusion_report = _apply_formula_detection(
            blocks,
            formula_result,
            geometry=geometry,
            config=formula_detector_config,
        )
        formula_detection = copy.deepcopy(formula_result.detector_manifest)
        formula_detection["fusion"] = fusion_report
    else:
        formula_detection = {
            "schema_version": "pdf_formula_detector_v1",
            "mode": "disabled",
            "status": "odl_only",
            "reason": "formula detector was explicitly disabled",
            "region_count": 0,
        }

    raw_units, segmentation_mode = _unit_rows(blocks, geometry)
    exact_cover = _exact_cover(raw_units, len(blocks))
    doc_slug = _slug(doc_id, fallback="doc")
    block_ids = _planned_block_ids(raw_units, blocks, doc_slug=doc_slug)
    blocks = _finalize_formula_clusters(
        blocks,
        block_ids,
        doc_id=doc_id,
        source_sha256=source_sha256,
    )

    document_chapters: list[dict[str, Any]] = []
    source_map: list[dict[str, Any]] = []
    block_policies: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    for unit_index, row in enumerate(raw_units):
        unit_id = f"u{unit_index + 1:04d}_{_slug(row['title'], fallback='unit')}"
        chapter_id = f"{doc_slug}_{unit_id}"
        chapter_blocks: list[dict[str, Any]] = []
        for local_index, block_index in enumerate(
            range(row["start_block"], row["end_block"])
        ):
            block = blocks[block_index]
            block_id = f"{chapter_id}_b{local_index + 1:04d}"
            policy, block_review = _block_policy(block)
            chapter_blocks.append(
                {
                    "block_id": block_id,
                    "order_index": local_index + 1,
                    "page_ids": [block.page_number],
                    "block_type": _runtime_kind(block),
                    "is_chapter_opening": local_index == 0,
                    "source_text": block.text,
                    "clean_text": block.text,
                    "sentences": [],
                    "quality_flags": [],
                    "annotations": {},
                }
            )
            source_row: dict[str, Any] = {
                "block_id": block_id,
                "source_block_kind": block.source_kind,
                "page_number": block.page_number,
                "bbox_pdf": list(block.bbox_pdf),
                "odl_path": block.odl_path,
                "odl_node_id": block.odl_node_id,
                "heading_level": block.heading_level,
                "has_extracted_text": block.has_extracted_text,
                "review_required": block_review,
                "provenance_precision": "odl_node_page_bbox",
            }
            if block.rich_payload is not None:
                source_row["rich_payload"] = block.rich_payload
            if block.formula_detection is not None:
                source_row["formula_detection"] = block.formula_detection
            if block.original_source_kind is not None:
                source_row["odl_original"] = {
                    "source_block_kind": block.original_source_kind,
                    "bbox_pdf": list(
                        block.original_bbox_pdf or block.bbox_pdf
                    ),
                }
            source_map.append(source_row)
            block_policies.append(
                {
                    "block_id": block_id,
                    "translation_policy": policy,
                    "review_required": block_review,
                }
            )
        document_chapters.append(
            {
                "chapter_id": chapter_id,
                "order_index": unit_index + 1,
                "title": row["title"],
                "blocks": chapter_blocks,
            }
        )
        units.append(
            {
                "unit_id": unit_id,
                "chapter_id": chapter_id,
                "order_index": unit_index,
                "title": row["title"],
                "block_range": [row["start_block"], row["end_block"]],
                "role": row["role"],
                "translation_policy": row["translation_policy"],
                "parent_unit_id": None,
                "confidence": row["confidence"],
                "evidence": row["evidence"],
                "review_required": row["review_required"],
            }
        )

    translatable = [
        unit["chapter_id"]
        for unit in units
        if unit["role"] == "content_unit"
        and unit["translation_policy"] == "translate"
        and not unit["review_required"]
    ]
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "doc_id": doc_id,
        "source": {
            "path": str(source),
            "sha256": source_sha256,
            "format": "pdf",
        },
        "extractor": {
            "name": "opendataloader-pdf",
            "package_version": extraction.package_version,
            "adapter_version": extraction.adapter_version,
            "java_version": extraction.java_version,
            "pymupdf_version": geometry.pymupdf_version,
            "raw_json_sha256": extraction.raw_json_sha256,
            "mode": "public_package_api_json",
        },
        "segmentation": {
            "mode": segmentation_mode,
            "bookmark_count": len(geometry.outline),
        },
        "page_geometry": [
            {
                "page_number": index + 1,
                "width": width,
                "height": height,
            }
            for index, (width, height) in enumerate(geometry.page_sizes)
        ],
        "formula_detection": formula_detection,
        "units": units,
        "translatable_chapter_ids": translatable,
        "review_required_unit_ids": [
            unit["unit_id"] for unit in units if unit["review_required"]
        ],
        "review_required_chapter_ids": [
            unit["chapter_id"] for unit in units if unit["review_required"]
        ],
        "exact_cover": exact_cover,
        "source_map": source_map,
        "block_policies": block_policies,
    }
    manifest["structure_sha256"] = _canonical_hash(
        {
            "normalizer_version": NORMALIZER_VERSION,
            "source_sha256": source_sha256,
            "extractor": manifest["extractor"],
            "segmentation": manifest["segmentation"],
            "page_geometry": manifest["page_geometry"],
            "formula_detection": formula_detection,
            "units": units,
            "source_map": source_map,
            "block_policies": block_policies,
        }
    )
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": {
            "title": extraction.payload.get("title") or source.stem,
            "author": extraction.payload.get("author") or "",
            "domain": "unknown",
            "genre": "unknown",
            "source_language": source_language,
            "target_language": target_language,
            "source_format": "pdf",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": NORMALIZER_VERSION,
            "pipeline_version": NORMALIZER_VERSION,
            "contamination_risk": "medium",
        },
        "chapters": document_chapters,
    }
    return PdfNormalizationResult(
        document=document,
        structure_manifest=manifest,
    )


__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "NORMALIZER_VERSION",
    "OBJECT_REPLACEMENT_CHARACTER",
    "FormulaDetectorConfig",
    "PdfGeometry",
    "PdfNormalizationError",
    "PdfNormalizationResult",
    "PdfOutlineEntry",
    "normalize_pdf",
]
