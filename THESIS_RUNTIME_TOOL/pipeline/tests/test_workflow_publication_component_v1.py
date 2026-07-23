from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.ingest.canonical_source_package import seal_asset_manifest
from pipeline.workflow_replay.contracts_v1 import canonical_sha256, physical_sha256
from pipeline.workflow_replay.publication_component_v1 import (
    PublicationComponentError,
    publish_selected_chapters_v1,
    validate_publication_component_package_v1,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _source_package(root: Path) -> tuple[Path, list[dict]]:
    package = root / "source"
    package.mkdir()
    document = {
        "schema_version": "1.5.0",
        "doc_id": "doc_publication",
        "metadata": {
            "title": "Fixture",
            "author": "",
            "domain": "technical",
            "genre": "technical_book",
            "source_language": "en",
            "target_language": "vi",
            "source_format": "markdown",
            "license": "unknown",
            "raw_sha256": "a" * 64,
            "extraction_tool": "fixture",
            "pipeline_version": "fixture",
            "contamination_risk": "low",
        },
        "chapters": [
            {
                "chapter_id": "ch1",
                "order_index": 0,
                "title": "One",
                "blocks": [
                    {
                        "block_id": "b1",
                        "order_index": 0,
                        "block_type": "paragraph",
                        "is_chapter_opening": True,
                        "source_text": "Value $x$.",
                        "clean_text": "Value $x$.",
                        "sentences": [],
                        "page_ids": [],
                        "quality_flags": [],
                        "annotations": {},
                    }
                ],
            },
            {
                "chapter_id": "ch2",
                "order_index": 1,
                "title": "Two",
                "blocks": [
                    {
                        "block_id": "b2",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "is_chapter_opening": True,
                        "source_text": "Other.",
                        "clean_text": "Other.",
                        "sentences": [],
                        "page_ids": [],
                        "quality_flags": [],
                        "annotations": {},
                    }
                ],
            },
        ],
    }
    structure = {
        "schema_version": "presegmented_structure_manifest_v1",
        "normalizer_version": "fixture",
        "doc_id": "doc_publication",
        "source": {
            "path": str((package / "source.md").resolve()),
            "sha256": "a" * 64,
            "format": "markdown",
            "provenance": {},
        },
        "extractor": {"name": "fixture", "version": "v1", "mode": "exact"},
        "cross_check": {"status": "not_applicable", "review_required": False},
        "warnings": [],
        "units": [
            {
                "unit_id": "ch1",
                "chapter_id": "ch1",
                "order_index": 0,
                "title": "One",
                "block_range": [0, 1],
                "role": "content_unit",
                "translation_policy": "translate",
                "confidence": 1.0,
                "evidence": ["fixture"],
                "review_required": False,
            },
            {
                "unit_id": "ch2",
                "chapter_id": "ch2",
                "order_index": 1,
                "title": "Two",
                "block_range": [1, 2],
                "role": "content_unit",
                "translation_policy": "translate",
                "confidence": 1.0,
                "evidence": ["fixture"],
                "review_required": False,
            },
        ],
        "translatable_chapter_ids": ["ch1", "ch2"],
        "review_required_unit_ids": [],
        "review_required_chapter_ids": [],
        "exact_cover": {
            "expected_blocks": 2,
            "covered_blocks": 2,
            "overlap_count": 0,
            "missing_count": 0,
            "coverage": 1.0,
        },
        "source_map": [
            {"block_id": "b1"},
            {"block_id": "b2"},
        ],
        "block_policies": [
            {"block_id": "b1", "translation_policy": "translate"},
            {"block_id": "b2", "translation_policy": "translate"},
        ],
    }
    structure["structure_sha256"] = canonical_sha256(structure)
    equation_bytes = b"x"
    equation_path = package / "assets" / "eq.tex"
    equation_path.parent.mkdir()
    equation_path.write_bytes(equation_bytes)
    assets = [
        {
            "asset_id": "eq_x",
            "kind": "equation",
            "media_type": "application/x-tex",
            "translation_policy": "translate_structured",
            "availability": "materialized",
            "source_locator": {
                "block_id": "b1",
                "inline_math_ordinal": 0,
            },
            "metadata": {"display": "inline"},
            "package_path": "assets/eq.tex",
            "sha256": physical_sha256(equation_bytes),
            "review_required": False,
        }
    ]
    bindings = [
        {
            "block_id": "b1",
            "source_kind": "prose",
            "semantic_kind": "text",
            "semantic_subtype": "mixed_structured_content",
            "render_role": "text",
            "translation_policy": "translate_structured",
            "review_required": False,
            "asset_ids": ["eq_x"],
        },
        {
            "block_id": "b2",
            "source_kind": "prose",
            "semantic_kind": "text",
            "semantic_subtype": None,
            "render_role": "text",
            "translation_policy": "translate",
            "review_required": False,
            "asset_ids": [],
        },
    ]
    asset_manifest = seal_asset_manifest(
        document,
        structure,
        assets=assets,
        block_bindings=bindings,
    )
    _write_json(package / "document.json", document)
    _write_json(package / "structure_manifest.json", structure)
    _write_json(package / "asset_manifest.json", asset_manifest)
    artifact = {
        "schema_id": "TranslationArtifactV1",
        "schema_version": "1.0.0",
        "translations": [
            {
                "block_id": "b1",
                "error_code": None,
                "status": "translated",
                "target_text": "Gia tri $x$.",
            }
        ],
    }
    artifact_path = root / "s1.json"
    _write_json(artifact_path, artifact)
    binding = {
        "artifact_ref": "components/translation/s1.json",
        "artifact_kind": "translation_artifact",
        "schema_version": "TranslationArtifactV1",
        "sha256": physical_sha256(artifact_path.read_bytes()),
        "sha256_kind": "physical",
    }
    source_bindings = [
        {
            "role": role,
            "binding": {
                "artifact_ref": f"source/{role}.json",
                "artifact_kind": role,
                "schema_version": "1.0.0",
                "sha256": canonical_sha256({"role": role}),
                "sha256_kind": "physical",
            },
        }
        for role in (
            "document",
            "structure_manifest",
            "asset_manifest",
            "admitted_projection",
            "normalization_receipt",
            "package_seal",
        )
    ]
    return package, [
        {
            "artifact": artifact,
            "path": artifact_path,
            "binding": binding,
            "source_bindings": source_bindings,
        }
    ]


def test_selected_chapter_publication_is_terminal_and_reusable(
    tmp_path: Path,
) -> None:
    package, rows = _source_package(tmp_path)
    item = rows[0]
    result = publish_selected_chapters_v1(
        component_root=tmp_path / "component",
        source_package_root=package,
        workflow_run_id="wf_publication_v1",
        component_run_id="pub_publication_v1",
        selected_chapter_ids=["ch1"],
        translation_artifact=item["path"],
        translation_artifact_binding=item["binding"],
        source_package_bindings=item["source_bindings"],
        producer_code_commit="a" * 40,
        created_at="2026-07-23T00:00:00Z",
    )
    validation = validate_publication_component_package_v1(
        result.component_root,
        require_terminal=True,
    )
    assert validation["manifest"]["status"] == "succeeded"
    assert validation["export_manifest"]["counts"]["chapters_rendered"] == 1
    assert validation["export_manifest"]["counts"]["chapters_excluded"] == 0
    html_text = (
        result.component_root / "artifacts/publication/document.html"
    ).read_text(encoding="utf-8")
    assert "Gia tri" in html_text
    assert "{{asset:" not in html_text

    reused = publish_selected_chapters_v1(
        component_root=result.component_root,
        source_package_root=package,
        workflow_run_id="wf_publication_v1",
        component_run_id="pub_publication_v1",
        selected_chapter_ids=["ch1"],
        translation_artifact=item["path"],
        translation_artifact_binding=item["binding"],
        source_package_bindings=item["source_bindings"],
        producer_code_commit="a" * 40,
        created_at="2026-07-23T00:00:00Z",
    )
    assert reused.manifest == result.manifest


def test_publication_rejects_missing_protected_math_and_tampered_output(
    tmp_path: Path,
) -> None:
    package, rows = _source_package(tmp_path)
    item = rows[0]
    broken = copy.deepcopy(item["artifact"])
    broken["translations"][0]["target_text"] = "Gia tri x."
    broken_path = tmp_path / "broken.json"
    _write_json(broken_path, broken)
    broken_binding = dict(item["binding"])
    broken_binding["sha256"] = physical_sha256(broken_path.read_bytes())
    with pytest.raises(PublicationComponentError, match="changed protected asset"):
        publish_selected_chapters_v1(
            component_root=tmp_path / "broken_component",
            source_package_root=package,
            workflow_run_id="wf_publication_v1",
            component_run_id="pub_publication_v1",
            selected_chapter_ids=["ch1"],
            translation_artifact=broken_path,
            translation_artifact_binding=broken_binding,
            source_package_bindings=item["source_bindings"],
            producer_code_commit="a" * 40,
            created_at="2026-07-23T00:00:00Z",
        )

    result = publish_selected_chapters_v1(
        component_root=tmp_path / "component",
        source_package_root=package,
        workflow_run_id="wf_publication_v1",
        component_run_id="pub_publication_v1",
        selected_chapter_ids=["ch1"],
        translation_artifact=item["path"],
        translation_artifact_binding=item["binding"],
        source_package_bindings=item["source_bindings"],
        producer_code_commit="a" * 40,
        created_at="2026-07-23T00:00:00Z",
    )
    output = result.component_root / "artifacts/publication/document.md"
    output.write_text("tampered", encoding="utf-8")
    with pytest.raises(PublicationComponentError, match="bytes drifted"):
        validate_publication_component_package_v1(result.component_root)
