from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from pipeline.ingest.admitted_projection import (
    AdmissionProjectionError,
    validate_admitted_projection,
)
from pipeline.ingest.canonical_source_package import (
    seal_asset_manifest,
    validate_canonical_source_package,
)
from pipeline.ingest.document_contract import validate_locked_document
from pipeline.ingest.pdf_formula_detector import (
    FORMULA_LABELS,
    MODEL_FILENAME,
    MODEL_LABELS,
    FormulaDetectionResult,
    FormulaDetectorConfig,
    FormulaRegion,
    formula_region_id,
)
from pipeline.ingest.pdf_formula_cluster import validate_formula_cluster
from pipeline.ingest.pdf_normalizer import (
    OBJECT_REPLACEMENT_CHARACTER,
    PdfGeometry,
    PdfNormalizationError,
    PdfOutlineEntry,
    normalize_pdf,
)
from pipeline.ingest.pdf_opendataloader_adapter import (
    EXPECTED_OPENDATALOADER_PDF_VERSION,
    PdfAdapterError,
    extract_pdf,
)
from pipeline.ingest.source_package_materializer import materialize_source_package
from pipeline.ingest.source_package_exporter import (
    SourcePackageExportError,
    export_source_package,
    seal_translation_overlay,
)
from pipeline.ingest.unified_source_normalizer import validate_normalization_contract


def _write_pdf(path: Path, *, pages: int = 2) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for page_number in range(1, pages + 1):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {page_number}")
        page.draw_rect(pymupdf.Rect(72, 100, 300, 220))
    document.save(path)
    document.close()
    return path


def _root(source: Path, kids: list[dict], *, pages: int = 2) -> dict:
    return {
        "file name": source.name,
        "number of pages": pages,
        "author": "Fixture Author",
        "title": "Fixture PDF",
        "creation date": None,
        "modification date": None,
        "kids": kids,
    }


def _fixture_payload(source: Path) -> dict:
    return _root(
        source,
        [
            {
                "type": "heading",
                "id": 1,
                "page number": 1,
                "bounding box": [72, 700, 260, 720],
                "heading level": 1,
                "content": "Section One",
            },
            {
                "type": "paragraph",
                "id": 2,
                "page number": 1,
                "bounding box": [72, 630, 500, 680],
                "content": "First page prose.",
            },
            {
                "type": "table",
                "id": 3,
                "page number": 1,
                "bounding box": [72, 500, 300, 610],
                "number of rows": 1,
                "number of columns": 1,
                "rows": [
                    {
                        "type": "table row",
                        "row number": 1,
                        "cells": [
                            {
                                "type": "table cell",
                                "page number": 1,
                                "bounding box": [72, 500, 300, 610],
                                "row number": 1,
                                "column number": 1,
                                "row span": 1,
                                "column span": 1,
                                "kids": [
                                    {
                                        "type": "paragraph",
                                        "id": 4,
                                        "page number": 1,
                                        "bounding box": [80, 520, 280, 580],
                                        "content": "Cell content",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "type": "image",
                "id": 5,
                "page number": 1,
                "bounding box": [320, 500, 500, 610],
                "source": "fixture_images/image1.png",
            },
            {
                "type": "heading",
                "id": 6,
                "page number": 2,
                "bounding box": [72, 700, 260, 720],
                "heading level": 1,
                "content": "Section Two",
            },
            {
                "type": "paragraph",
                "id": 7,
                "page number": 2,
                "bounding box": [72, 630, 500, 680],
                "content": "Second page prose.",
            },
        ],
    )


def _formula_only_payload(source: Path) -> dict:
    return _root(
        source,
        [
            {
                "type": "heading",
                "id": 1,
                "page number": 1,
                "bounding box": [72, 700, 260, 720],
                "heading level": 1,
                "content": "Section One",
            },
            {
                "type": "paragraph",
                "id": 2,
                "page number": 1,
                "bounding box": [72, 630, 500, 680],
                "content": "First page prose.",
            },
            {
                "type": "heading",
                "id": 3,
                "page number": 2,
                "bounding box": [72, 700, 260, 720],
                "heading level": 1,
                "content": "Section Two",
            },
            {
                "type": "paragraph",
                "id": 4,
                "page number": 2,
                "bounding box": [72, 630, 500, 680],
                "content": "Second page prose.",
            },
        ],
    )


def _geometry(*, outline: tuple[PdfOutlineEntry, ...] = ()) -> PdfGeometry:
    return PdfGeometry(
        page_sizes=((612.0, 792.0), (612.0, 792.0)),
        outline=outline,
        pymupdf_version="fixture-pymupdf",
    )


def _converter(payload: dict):
    def execute(source: Path, output_dir: Path) -> None:
        emitted = copy.deepcopy(payload)
        emitted["file name"] = source.name
        (output_dir / "result.json").write_text(
            json.dumps(emitted, ensure_ascii=False),
            encoding="utf-8",
        )

    return execute


def _normalize(
    source: Path,
    payload: dict,
    *,
    geometry: PdfGeometry | None = None,
    **formula_options,
):
    return normalize_pdf(
        source,
        doc_id="fixture_pdf",
        convert_executor=_converter(payload),
        package_version=EXPECTED_OPENDATALOADER_PDF_VERSION,
        java_version="fixture-java",
        geometry_reader=lambda _source: geometry or _geometry(),
        **formula_options,
    )


def _formula_config(tmp_path: Path) -> FormulaDetectorConfig:
    model = tmp_path / MODEL_FILENAME
    model.write_bytes(b"formula-model-fixture")
    return FormulaDetectorConfig(model_path=model)


def _bbox_view(
    bbox_pdf: tuple[float, float, float, float],
    *,
    page_width: float = 612.0,
    page_height: float = 792.0,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox_pdf
    return (
        round(x0 / page_width, 6),
        round((page_height - y1) / page_height, 6),
        round((x1 - x0) / page_width, 6),
        round((y1 - y0) / page_height, 6),
    )


def _formula_result(
    source: Path,
    config: FormulaDetectorConfig,
    regions: tuple[FormulaRegion, ...],
    *,
    page_count: int = 2,
) -> FormulaDetectionResult:
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    return FormulaDetectionResult(
        source_sha256=source_sha256,
        page_count=page_count,
        regions=regions,
        detector_manifest={
            "schema_version": "pdf_formula_detector_v1",
            "mode": "required",
            "status": "completed",
            "source_sha256": source_sha256,
            "model": {
                "repo": config.model_repo,
                "revision": config.model_revision,
                "filename": config.model_filename,
                "sha256": config.expected_model_sha256,
                "declared_license": config.model_declared_license,
                "redistribution_reviewed": False,
            },
            "runtime": {
                "onnxruntime_version": "fixture-runtime",
                "provider": "CPUExecutionProvider",
                "pymupdf_version": "fixture-pymupdf",
            },
            "preprocessing": {
                "raster_dpi": 144,
                "color_mode": "RGB",
                "input_size": 1024,
                "normalization": "uint8_rgb_div_255",
                "resize": "aspect_fit_bilinear",
                "letterbox_fill": 114,
                "coordinate_transform": "raster_top_left_to_pdf_bottom_left",
            },
            "postprocessing": {
                "label_map": {
                    str(key): value for key, value in MODEL_LABELS.items()
                },
                "included_labels": sorted(FORMULA_LABELS),
                "confidence_threshold": config.confidence_threshold,
                "acceptance_threshold": config.acceptance_threshold,
                "nms_iou_threshold": config.nms_iou_threshold,
                "nms_mode": "classwise_deterministic",
            },
            "page_count": page_count,
            "region_count": len(regions),
            "regions": [region.as_dict() for region in regions],
        },
    )


def _complete_overlay(document: dict, manifest: dict) -> dict:
    binding_by_id = {
        binding["block_id"]: binding for binding in manifest["block_bindings"]
    }
    translations = []
    for chapter in document["chapters"]:
        for block in chapter["blocks"]:
            binding = binding_by_id[block["block_id"]]
            if binding["translation_policy"] == "translate":
                translations.append(
                    {
                        "block_id": block["block_id"],
                        "text": f"VI::{block['block_id']}",
                        "html": None,
                        "markdown": None,
                    }
                )
            elif binding["translation_policy"] == "translate_structured":
                translations.append(
                    {
                        "block_id": block["block_id"],
                        "text": f"VI structured::{block['block_id']}",
                        "html": "<table><tr><td>VI</td></tr></table>",
                        "markdown": "| VI |\n| --- |\n| VI |",
                    }
                )
    return seal_translation_overlay(document, translations)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_adapter_records_runtime_identities_and_is_deterministic(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    payload = _fixture_payload(source)

    first = extract_pdf(
        source,
        convert_executor=_converter(payload),
        package_version=EXPECTED_OPENDATALOADER_PDF_VERSION,
        java_version="24.0.1",
    )
    second = extract_pdf(
        source,
        convert_executor=_converter(payload),
        package_version=EXPECTED_OPENDATALOADER_PDF_VERSION,
        java_version="24.0.1",
    )

    assert first.package_version == "2.4.3"
    assert first.java_version == "24.0.1"
    assert first.raw_json_sha256 == second.raw_json_sha256
    assert first.payload == second.payload


def test_adapter_stages_unicode_source_on_ascii_content_addressed_path(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "tài liệu thử.pdf")
    payload = _fixture_payload(source)
    observed: list[Path] = []

    def converter(staged_source: Path, output_dir: Path) -> None:
        observed.append(staged_source)
        assert staged_source.name.isascii()
        assert staged_source.read_bytes() == source.read_bytes()
        emitted = copy.deepcopy(payload)
        emitted["file name"] = staged_source.name
        (output_dir / "result.json").write_text(
            json.dumps(emitted, ensure_ascii=False),
            encoding="utf-8",
        )

    extraction = extract_pdf(
        source,
        convert_executor=converter,
        package_version=EXPECTED_OPENDATALOADER_PDF_VERSION,
        java_version="24.0.1",
    )

    assert len(observed) == 1
    assert observed[0].name.startswith("source_")
    assert extraction.payload["file name"] == observed[0].name


def test_adapter_fails_closed_on_version_json_and_bbox_drift(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    called = False

    def should_not_run(_source: Path, _output_dir: Path) -> None:
        nonlocal called
        called = True

    with pytest.raises(PdfAdapterError, match="version drift"):
        extract_pdf(
            source,
            convert_executor=should_not_run,
            package_version="2.4.4",
            java_version="24",
        )
    assert called is False

    malformed = _fixture_payload(source)
    malformed["unexpected"] = True
    with pytest.raises(PdfAdapterError, match="root fields drifted"):
        extract_pdf(
            source,
            convert_executor=_converter(malformed),
            package_version="2.4.3",
            java_version="24",
        )

    malformed = _fixture_payload(source)
    malformed["kids"][0]["bounding box"] = [0, 0, 0, 0]
    with pytest.raises(PdfAdapterError, match="positive area"):
        extract_pdf(
            source,
            convert_executor=_converter(malformed),
            package_version="2.4.3",
            java_version="24",
        )

    malformed = _fixture_payload(source)
    malformed["kids"][0]["type"] = "future parser node"
    with pytest.raises(PdfAdapterError, match="unsupported"):
        extract_pdf(
            source,
            convert_executor=_converter(malformed),
            package_version="2.4.3",
            java_version="24",
        )


def test_pdf_normalizer_preserves_rich_kinds_without_expanding_runtime_schema(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    result = _normalize(source, _fixture_payload(source))

    validate_locked_document(result.document)
    receipt = validate_normalization_contract(
        result.document,
        result.structure_manifest,
        expected_format="pdf",
    )
    blocks = [
        block
        for chapter in result.document["chapters"]
        for block in chapter["blocks"]
    ]
    source_map = result.structure_manifest["source_map"]

    assert receipt["status"] == "ready"
    assert len(result.document["chapters"]) == 2
    assert {block["block_type"] for block in blocks} <= {
        "heading",
        "paragraph",
        "dialogue",
        "footnote",
    }
    assert [row["source_block_kind"] for row in source_map] == [
        "heading",
        "paragraph",
        "table",
        "image",
        "heading",
        "paragraph",
    ]
    assert blocks[3]["source_text"] == OBJECT_REPLACEMENT_CHARACTER
    assert source_map[3]["has_extracted_text"] is False
    assert source_map[3]["review_required"] is True
    assert result.document["metadata"]["source_format"] == "pdf"
    assert "normalizer_version" not in result.document["metadata"]
    assert "structure_sha256" not in result.document["metadata"]


def test_pdf_bookmarks_take_precedence_over_parser_heading_titles(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    geometry = _geometry(
        outline=(
            PdfOutlineEntry(1, "Bookmark A", 1, 72.0),
            PdfOutlineEntry(1, "Bookmark B", 2, 72.0),
        )
    )
    result = _normalize(source, _fixture_payload(source), geometry=geometry)

    assert result.structure_manifest["segmentation"]["mode"] == "pdf_bookmark"
    assert [chapter["title"] for chapter in result.document["chapters"]] == [
        "Bookmark A",
        "Bookmark B",
    ]


def test_image_only_pdf_never_fabricates_semantic_text(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "scan.pdf", pages=1)
    payload = _root(
        source,
        [
            {
                "type": "image",
                "id": 1,
                "page number": 1,
                "bounding box": [40, 90, 570, 700],
                "source": "scan_images/image1.png",
            }
        ],
        pages=1,
    )
    geometry = PdfGeometry(
        page_sizes=((612.0, 792.0),),
        outline=(),
        pymupdf_version="fixture-pymupdf",
    )
    result = _normalize(source, payload, geometry=geometry)
    block = result.document["chapters"][0]["blocks"][0]

    assert block["source_text"] == OBJECT_REPLACEMENT_CHARACTER
    assert result.structure_manifest["units"][0]["review_required"] is True
    assert result.structure_manifest["translatable_chapter_ids"] == []


def test_pdf_materialization_preserves_source_crops_and_admission_channels(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    result = _normalize(source, _fixture_payload(source))
    output = tmp_path / "package"
    written = materialize_source_package(
        result.document,
        result.structure_manifest,
        output,
    )
    asset_manifest = json.loads(
        written.asset_manifest_path.read_text(encoding="utf-8")
    )
    projection = json.loads(
        written.admitted_projection_path.read_text(encoding="utf-8")
    )
    report = validate_canonical_source_package(
        result.document,
        result.structure_manifest,
        asset_manifest,
        package_root=output,
    )

    assert report["counts"]["blocks"] == 6
    assert any(asset["kind"] == "embedded_file" for asset in asset_manifest["assets"])
    assert any(asset["kind"] == "table" for asset in asset_manifest["assets"])
    assert any(asset["kind"] == "image" for asset in asset_manifest["assets"])
    assert any(asset["kind"] == "raw_fragment" for asset in asset_manifest["assets"])
    assert [row["channel"] for row in projection["rows"]] == [
        "semantic_text",
        "semantic_text",
        "structured_translate",
        "review_required",
        "semantic_text",
        "semantic_text",
    ]
    for asset in asset_manifest["assets"]:
        if asset["availability"] == "materialized":
            assert (output / asset["package_path"]).is_file()

    tampered = copy.deepcopy(projection)
    tampered["rows"][2]["channel"] = "semantic_text"
    with pytest.raises(AdmissionProjectionError):
        validate_admitted_projection(
            tampered,
            result.document,
            result.structure_manifest,
            asset_manifest,
        )


def test_pdf_normalization_hashes_are_stable_and_source_bound(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    payload = _fixture_payload(source)
    first = _normalize(source, payload)
    second = _normalize(source, payload)

    assert first.document == second.document
    assert first.structure_manifest == second.structure_manifest

    changed = copy.deepcopy(payload)
    changed["kids"][1]["content"] = "Changed prose."
    third = _normalize(source, changed)
    assert (
        first.structure_manifest["structure_sha256"]
        != third.structure_manifest["structure_sha256"]
    )


def test_pdf_normalizer_rejects_geometry_and_order_mismatch(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    payload = _fixture_payload(source)
    bad_geometry = PdfGeometry(
        page_sizes=((612.0, 792.0),),
        outline=(),
        pymupdf_version="fixture-pymupdf",
    )
    with pytest.raises(PdfNormalizationError, match="page counts differ"):
        _normalize(source, payload, geometry=bad_geometry)

    payload["kids"][0]["page number"] = 2
    with pytest.raises(PdfNormalizationError, match="moves backwards"):
        _normalize(source, payload)


def test_formula_detector_unique_overlap_preserves_text_and_emits_crop(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    config = _formula_config(tmp_path)
    region = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=(70.0, 625.0, 505.0, 685.0),
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.95,
        bbox_pdf=(70.0, 625.0, 505.0, 685.0),
        bbox_view=_bbox_view((70.0, 625.0, 505.0, 685.0)),
    )
    formula_result = _formula_result(source, config, (region,))
    result = _normalize(
        source,
        _fixture_payload(source),
        formula_detector_mode="required",
        formula_detector_config=config,
        formula_detector_executor=lambda _source, _config: formula_result,
    )

    blocks = [
        block
        for chapter in result.document["chapters"]
        for block in chapter["blocks"]
    ]
    source_rows = result.structure_manifest["source_map"]
    formula_index = next(
        index
        for index, row in enumerate(source_rows)
        if row["source_block_kind"] == "equation"
    )
    assert blocks[formula_index]["source_text"] == "First page prose."
    assert source_rows[formula_index]["odl_original"] == {
        "source_block_kind": "paragraph",
        "bbox_pdf": [72.0, 630.0, 500.0, 680.0],
    }
    assert source_rows[formula_index]["bbox_pdf"] == list(region.bbox_pdf)
    assert (
        source_rows[formula_index]["formula_detection"]["fusion_status"]
        == "accepted_unique_overlap"
    )

    first_output = tmp_path / "package-a"
    second_output = tmp_path / "package-b"
    first = materialize_source_package(
        result.document,
        result.structure_manifest,
        first_output,
    )
    second = materialize_source_package(
        result.document,
        result.structure_manifest,
        second_output,
    )
    first_manifest = json.loads(first.asset_manifest_path.read_text("utf-8"))
    second_manifest = json.loads(second.asset_manifest_path.read_text("utf-8"))
    projection = json.loads(first.admitted_projection_path.read_text("utf-8"))
    formula_block_id = blocks[formula_index]["block_id"]
    formula_projection = next(
        row for row in projection["rows"] if row["block_id"] == formula_block_id
    )
    assert formula_projection["channel"] == "preserve_only"
    assert first_manifest == second_manifest
    equation_assets = [
        asset for asset in first_manifest["assets"]
        if asset["kind"] == "equation"
    ]
    assert len(equation_assets) == 1
    assert equation_assets[0]["sha256"]
    assert equation_assets[0]["metadata"]["formula_detection"][
        "fusion_status"
    ] == "accepted_unique_overlap"


def test_formula_detector_conflict_never_leaks_formula_text_to_semantic(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    config = _formula_config(tmp_path)
    region = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=(65.0, 490.0, 510.0, 620.0),
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.93,
        bbox_pdf=(65.0, 490.0, 510.0, 620.0),
        bbox_view=_bbox_view((65.0, 490.0, 510.0, 620.0)),
    )
    formula_result = _formula_result(source, config, (region,))
    result = _normalize(
        source,
        _fixture_payload(source),
        formula_detector_mode="required",
        formula_detector_config=config,
        formula_detector_executor=lambda _source, _config: formula_result,
    )
    output = tmp_path / "package"
    written = materialize_source_package(
        result.document,
        result.structure_manifest,
        output,
    )
    (output / "document.json").write_text(
        json.dumps(result.document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output / "structure_manifest.json").write_text(
        json.dumps(
            result.structure_manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    projection = json.loads(written.admitted_projection_path.read_text("utf-8"))
    channels = {row["block_id"]: row["channel"] for row in projection["rows"]}
    blocks = {
        block["block_id"]: block
        for chapter in result.document["chapters"]
        for block in chapter["blocks"]
    }
    source_rows = result.structure_manifest["source_map"]
    protected = [
        row for row in source_rows
        if row["source_block_kind"] == "formula_fragment"
    ]

    assert len(protected) == 2
    assert all(channels[row["block_id"]] == "review_required" for row in protected)
    assert any(
        blocks[row["block_id"]]["source_text"] == "Cell content"
        for row in protected
    )
    assert all(
        channels[row["block_id"]] != "semantic_text"
        for row in protected
    )
    assert result.structure_manifest["formula_detection"]["fusion"] == {
        "accepted_region_count": 0,
        "review_required_region_count": 1,
        "synthetic_crop_block_count": 1,
        "protected_odl_block_count": 2,
        "auto_preserved_cluster_count": 0,
        "auto_preserved_region_count": 0,
        "auto_preserved_member_count": 0,
        "output_block_count": 7,
    }
    synthetic = [
        row for row in source_rows
        if row["odl_path"] == f"/visual_formula[{region.region_id}]"
    ]
    assert len(synthetic) == 1
    assert channels[synthetic[0]["block_id"]] == "review_required"


def test_formula_caption_beside_formula_forms_one_preserved_cluster(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    config = _formula_config(tmp_path)
    formula = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=(70.8, 640.0, 300.0, 660.0),
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.95,
        bbox_pdf=(70.8, 640.0, 300.0, 660.0),
        bbox_view=_bbox_view((70.8, 640.0, 300.0, 660.0)),
    )
    caption = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="formula_caption",
            bbox_pdf=(310.0, 640.0, 500.25, 660.0),
        ),
        page_number=1,
        label="formula_caption",
        class_id=9,
        confidence=0.90,
        bbox_pdf=(310.0, 640.0, 500.25, 660.0),
        bbox_view=_bbox_view((310.0, 640.0, 500.25, 660.0)),
    )
    formula_result = _formula_result(source, config, (formula, caption))

    result = _normalize(
        source,
        _formula_only_payload(source),
        formula_detector_mode="required",
        formula_detector_config=config,
        formula_detector_executor=lambda _source, _config: formula_result,
    )

    formula_rows = [
        row
        for row in result.structure_manifest["source_map"]
        if row.get("formula_detection")
    ]
    statuses = {
        row["formula_detection"]["fusion_status"]
        for row in formula_rows
    }
    assert "orphan_formula_caption" not in statuses
    assert statuses == {"auto_preserved_formula_cluster"}
    assert len(formula_rows) == 3
    clusters = {
        row["formula_detection"]["formula_cluster"]["formula_cluster_id"]
        for row in formula_rows
    }
    assert len(clusters) == 1
    cluster = validate_formula_cluster(
        formula_rows[0]["formula_detection"]["formula_cluster"]
    )
    assert cluster["detector_region_ids"] == [
        formula.region_id,
        caption.region_id,
    ]
    assert [member["block_id"] for member in cluster["members"]] == [
        row["block_id"] for row in formula_rows
    ]
    assert cluster["publication_bbox_pdf"] == [70.8, 630.0, 500.25, 680.0]
    assert [
        row["formula_detection"]["cluster_member_role"] for row in formula_rows
    ] == ["duplicate_evidence", "duplicate_evidence", "publication_visual"]
    assert result.structure_manifest["formula_detection"]["fusion"] == {
        "accepted_region_count": 0,
        "review_required_region_count": 0,
        "synthetic_crop_block_count": 2,
        "protected_odl_block_count": 1,
        "auto_preserved_cluster_count": 1,
        "auto_preserved_region_count": 2,
        "auto_preserved_member_count": 3,
        "output_block_count": 6,
    }

    output = tmp_path / "cluster-package"
    written = materialize_source_package(
        result.document,
        result.structure_manifest,
        output,
    )
    (output / "document.json").write_text(
        json.dumps(result.document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output / "structure_manifest.json").write_text(
        json.dumps(
            result.structure_manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    projection = json.loads(written.admitted_projection_path.read_text("utf-8"))
    channels = {row["block_id"]: row["channel"] for row in projection["rows"]}
    assert all(channels[row["block_id"]] == "preserve_only" for row in formula_rows)
    assert all(channels[row["block_id"]] != "semantic_text" for row in formula_rows)

    manifest = json.loads(written.asset_manifest_path.read_text("utf-8"))
    overlay = _complete_overlay(result.document, manifest)
    first_export = export_source_package(
        output,
        overlay,
        tmp_path / "cluster-export-a",
    )
    second_export = export_source_package(
        output,
        overlay,
        tmp_path / "cluster-export-b",
    )
    html_text = first_export.html_path.read_text("utf-8")
    assert "Review required" not in html_text
    assert html_text.count('data-formula-cluster-role="publication_visual"') == 1
    assert html_text.count('data-formula-cluster-role="duplicate_evidence"') == 2
    assert first_export.manifest["counts"]["formula_cluster_visuals"] == 1
    assert (
        first_export.manifest["counts"][
            "formula_cluster_duplicate_rows_suppressed"
        ]
        == 2
    )
    assert _tree_bytes(first_export.output_dir) == _tree_bytes(
        second_export.output_dir
    )

    tampered_assets = copy.deepcopy(manifest["assets"])
    tampered = next(
        asset
        for asset in tampered_assets
        if isinstance((asset.get("metadata") or {}).get("formula_detection"), dict)
        and (asset["metadata"]["formula_detection"]).get("formula_cluster")
    )
    tampered["metadata"]["formula_detection"]["formula_cluster"][
        "formula_cluster_id"
    ] = "fcl_" + "0" * 24
    resealed = seal_asset_manifest(
        result.document,
        result.structure_manifest,
        assets=tampered_assets,
        block_bindings=manifest["block_bindings"],
    )
    written.asset_manifest_path.write_text(
        json.dumps(resealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SourcePackageExportError, match="tampered"):
        export_source_package(output, overlay, tmp_path / "tampered-export")

    for field, value in (("label", "plain text"), ("confidence", 0.01)):
        detector_tampered_assets = copy.deepcopy(manifest["assets"])
        detector_tampered = next(
            asset
            for asset in detector_tampered_assets
            if isinstance((asset.get("metadata") or {}).get("formula_detection"), dict)
            and isinstance(
                asset["metadata"]["formula_detection"].get("region"),
                dict,
            )
        )
        detector_tampered["metadata"]["formula_detection"]["region"][field] = value
        detector_resealed = seal_asset_manifest(
            result.document,
            result.structure_manifest,
            assets=detector_tampered_assets,
            block_bindings=manifest["block_bindings"],
        )
        written.asset_manifest_path.write_text(
            json.dumps(
                detector_resealed,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(SourcePackageExportError, match="detector evidence"):
            export_source_package(
                output,
                overlay,
                tmp_path / f"detector-{field}-tampered-export",
            )


def test_formula_cluster_outside_odl_tolerance_remains_review_required(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    config = _formula_config(tmp_path)
    formula_bbox = (69.0, 640.0, 300.0, 660.0)
    caption_bbox = (310.0, 640.0, 340.0, 660.0)
    formula = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=formula_bbox,
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.95,
        bbox_pdf=formula_bbox,
        bbox_view=_bbox_view(formula_bbox),
    )
    caption = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="formula_caption",
            bbox_pdf=caption_bbox,
        ),
        page_number=1,
        label="formula_caption",
        class_id=9,
        confidence=0.90,
        bbox_pdf=caption_bbox,
        bbox_view=_bbox_view(caption_bbox),
    )

    result = _normalize(
        source,
        _formula_only_payload(source),
        formula_detector_mode="required",
        formula_detector_config=config,
        formula_detector_executor=lambda _source, _config: _formula_result(
            source,
            config,
            (formula, caption),
        ),
    )
    formula_rows = [
        row
        for row in result.structure_manifest["source_map"]
        if row.get("formula_detection")
    ]
    assert len(formula_rows) == 3
    assert all(row["review_required"] for row in formula_rows)
    assert all(
        "formula_cluster" not in row["formula_detection"]
        for row in formula_rows
    )


def test_formula_without_caption_forms_one_preserved_cluster(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    config = _formula_config(tmp_path)
    formula_bbox = (100.0, 640.0, 300.0, 660.0)
    formula = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=formula_bbox,
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.95,
        bbox_pdf=formula_bbox,
        bbox_view=_bbox_view(formula_bbox),
    )

    result = _normalize(
        source,
        _formula_only_payload(source),
        formula_detector_mode="required",
        formula_detector_config=config,
        formula_detector_executor=lambda _source, _config: _formula_result(
            source,
            config,
            (formula,),
        ),
    )
    formula_rows = [
        row
        for row in result.structure_manifest["source_map"]
        if row.get("formula_detection")
    ]
    assert len(formula_rows) == 2
    assert all(not row["review_required"] for row in formula_rows)
    cluster = validate_formula_cluster(
        formula_rows[0]["formula_detection"]["formula_cluster"]
    )
    assert cluster["detector_region_ids"] == [formula.region_id]
    assert [member["role"] for member in cluster["members"]] == [
        "duplicate_evidence",
        "publication_visual",
    ]
    assert result.structure_manifest["formula_detection"]["fusion"][
        "auto_preserved_cluster_count"
    ] == 1


def test_competing_formula_caption_remains_review_required(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    config = _formula_config(tmp_path)
    formula = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=(100.0, 640.0, 300.0, 660.0),
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.95,
        bbox_pdf=(100.0, 640.0, 300.0, 660.0),
        bbox_view=_bbox_view((100.0, 640.0, 300.0, 660.0)),
    )
    captions = tuple(
        FormulaRegion(
            region_id=formula_region_id(
                page_number=1,
                label="formula_caption",
                bbox_pdf=bbox,
            ),
            page_number=1,
            label="formula_caption",
            class_id=9,
            confidence=0.90,
            bbox_pdf=bbox,
            bbox_view=_bbox_view(bbox),
        )
        for bbox in (
            (310.0, 640.0, 340.0, 660.0),
            (345.0, 640.0, 375.0, 660.0),
        )
    )
    formula_result = _formula_result(source, config, (formula, *captions))
    result = _normalize(
        source,
        _fixture_payload(source),
        formula_detector_mode="required",
        formula_detector_config=config,
        formula_detector_executor=lambda _source, _config: formula_result,
    )
    formula_rows = [
        row
        for row in result.structure_manifest["source_map"]
        if row.get("formula_detection")
    ]
    assert all(
        row["formula_detection"]["fusion_status"] == "ambiguous_odl_overlap"
        for row in formula_rows
    )
    assert all(row["review_required"] for row in formula_rows)
    assert result.structure_manifest["formula_detection"]["fusion"][
        "auto_preserved_cluster_count"
    ] == 0


def test_formula_detector_mode_is_explicit_and_source_bound(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    payload = _fixture_payload(source)
    disabled = _normalize(source, payload)
    assert disabled.structure_manifest["formula_detection"] == {
        "schema_version": "pdf_formula_detector_v1",
        "mode": "disabled",
        "status": "odl_only",
        "reason": "formula detector was explicitly disabled",
        "region_count": 0,
    }

    config = _formula_config(tmp_path)
    with pytest.raises(PdfNormalizationError, match="needs an explicit"):
        _normalize(
            source,
            payload,
            formula_detector_mode="required",
        )
    with pytest.raises(PdfNormalizationError, match="cannot accept"):
        _normalize(
            source,
            payload,
            formula_detector_mode="disabled",
            formula_detector_config=config,
        )

    stale = _formula_result(source, config, ())
    stale = FormulaDetectionResult(
        source_sha256="0" * 64,
        page_count=stale.page_count,
        regions=stale.regions,
        detector_manifest=stale.detector_manifest,
    )
    with pytest.raises(PdfNormalizationError, match="source identity mismatch"):
        _normalize(
            source,
            payload,
            formula_detector_mode="required",
            formula_detector_config=config,
            formula_detector_executor=lambda _source, _config: stale,
        )

    region = FormulaRegion(
        region_id=formula_region_id(
            page_number=1,
            label="isolate_formula",
            bbox_pdf=(70.0, 625.0, 505.0, 685.0),
        ),
        page_number=1,
        label="isolate_formula",
        class_id=8,
        confidence=0.90,
        bbox_pdf=(70.0, 625.0, 505.0, 685.0),
        bbox_view=_bbox_view((70.0, 625.0, 505.0, 685.0)),
    )
    duplicate = _formula_result(source, config, (region, region))
    with pytest.raises(PdfNormalizationError, match="duplicate region ids"):
        _normalize(
            source,
            payload,
            formula_detector_mode="required",
            formula_detector_config=config,
            formula_detector_executor=lambda _source, _config: duplicate,
        )

    tampered_manifest = copy.deepcopy(
        _formula_result(source, config, (region,)).detector_manifest
    )
    tampered_manifest["regions"] = []
    tampered = FormulaDetectionResult(
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        page_count=2,
        regions=(region,),
        detector_manifest=tampered_manifest,
    )
    with pytest.raises(PdfNormalizationError, match="manifest region rows mismatch"):
        _normalize(
            source,
            payload,
            formula_detector_mode="required",
            formula_detector_config=config,
            formula_detector_executor=lambda _source, _config: tampered,
        )
