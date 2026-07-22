from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pipeline.ingest.d2l_presegmented_adapter import D2lPresegmentedAdapterResult
from pipeline.ingest.presegmented_source_bundle import (
    PresegmentedBlock,
    PresegmentedBundle,
    PresegmentedChapter,
)
from pipeline.ingest.presegmented_source_normalizer import (
    normalize_presegmented_source,
)
from pipeline.ingest.unified_source_normalizer import (
    UnifiedContractError,
    write_unified_normalization,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _synthetic_adapter(tmp_path: Path) -> tuple[Path, Path, D2lPresegmentedAdapterResult]:
    source_bytes = (
        b"[[B0001]]\n# Chapter I\n\n"
        b"[[B0002]]\n:label: chapter_one\n\n"
        b"[[B0003]]\nStory text.\n"
    )
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    bundle_source = bundle_root / "d2l_full_book_en_marked_v1.md"
    bundle_source.write_bytes(source_bytes)
    (bundle_root / "manifest.json").write_text("{}\n", encoding="utf-8", newline="\n")
    (bundle_root / "block_map.json").write_text("{}\n", encoding="utf-8", newline="\n")
    receipt = {
        "upstream": {
            "marked_source": {"sha256": "1" * 64},
            "legacy_block_map": {"sha256": "2" * 64},
            "legacy_manifest": {"sha256": "3" * 64},
            "source_db_sha256": "4" * 64,
        }
    }
    receipt_path = bundle_root / "d2l_presegmented_adapter_receipt_v1.json"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    markers = [b"[[B0001]]\n", b"[[B0002]]\n", b"[[B0003]]\n"]
    texts = ["# Chapter I", ":label: chapter_one", "Story text."]
    kinds = ["heading", "label", "prose"]
    ids = ["d2l_chapter_one_b001", "d2l_chapter_one_b002", "d2l_chapter_one_b003"]
    blocks: list[PresegmentedBlock] = []
    marker_offsets = [source_bytes.index(marker) for marker in markers]
    for index, (marker, text, kind, block_id) in enumerate(
        zip(markers, texts, kinds, ids, strict=True)
    ):
        content_start = marker_offsets[index] + len(marker)
        content_end = (
            marker_offsets[index + 1]
            if index + 1 < len(marker_offsets)
            else len(source_bytes)
        )
        encoded = text.encode("utf-8")
        blocks.append(
            PresegmentedBlock(
                marker=f"B{index + 1:04d}",
                block_id=block_id,
                chapter_id="d2l_chapter_one",
                order_index=index,
                block_type=kind,
                source_text=text,
                source_sha256=_sha256(encoded),
                source_utf8_bytes=len(encoded),
                source_start_offset=content_start,
                source_end_offset=content_end,
            )
        )
    bundle = PresegmentedBundle(
        bundle_root=bundle_root,
        manifest={
            "schema_version": "presegmented_source_bundle_v1",
            "document_id": "d2l",
            "source_format": "markdown",
            "source_file": bundle_source.name,
            "block_map_file": "block_map.json",
            "block_map_sha256": "5" * 64,
        },
        block_map={},
        source_sha256=_sha256(source_bytes),
        source_utf8_bytes=len(source_bytes),
        blocks=tuple(blocks),
        chapters=(
            PresegmentedChapter(
                chapter_id="d2l_chapter_one",
                order_index=0,
                title="Chapter I",
                first_block_index=0,
                last_block_index=2,
                block_ids=tuple(ids),
            ),
        ),
        identity_sha256="a" * 64,
    )
    adapter = D2lPresegmentedAdapterResult(
        output_root=bundle_root,
        bundle=bundle,
        receipt_path=receipt_path,
        receipt_sha256=_sha256(receipt_path.read_bytes()),
        adapter_identity_sha256="b" * 64,
    )
    source_path = tmp_path / "source.md"
    source_path.write_bytes(source_bytes)
    return source_path, bundle_root, adapter


def test_presegmented_normalizer_preserves_ids_provenance_and_label_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bundle_root, adapter = _synthetic_adapter(tmp_path)
    monkeypatch.setattr(
        "pipeline.ingest.presegmented_source_normalizer.validate_d2l_presegmented_output",
        lambda *_args, **_kwargs: adapter,
    )
    result = normalize_presegmented_source(
        source,
        bundle_root=bundle_root,
        doc_id="project_import_01",
        capture_relative_path=(
            "working/source_package_captures/"
            f"d2lps_{adapter.adapter_identity_sha256}"
        ),
    )

    assert result.document["doc_id"] == "project_import_01"
    assert result.document["chapters"][0]["chapter_id"] == "d2l_chapter_one"
    assert [
        block["block_id"] for block in result.document["chapters"][0]["blocks"]
    ] == [block.block_id for block in adapter.bundle.blocks]
    assert [
        block["order_index"] for block in result.document["chapters"][0]["blocks"]
    ] == [0, 1, 2]
    assert all(
        block["block_type"] in {"heading", "paragraph", "dialogue", "footnote"}
        for block in result.document["chapters"][0]["blocks"]
    )
    provenance = result.structure_manifest["source"]["provenance"]
    assert provenance["upstream_document_id"] == "d2l"
    assert provenance["adapter_receipt_sha256"] == adapter.receipt_sha256
    assert provenance["adapter_identity_sha256"] == adapter.adapter_identity_sha256
    assert provenance["bundle_identity_sha256"] == adapter.bundle.identity_sha256
    label_row = next(
        row
        for row in result.structure_manifest["source_map"]
        if row["source_block_kind"] == "label"
    )
    label_policy = next(
        row
        for row in result.structure_manifest["block_policies"]
        if row["block_id"] == label_row["block_id"]
    )
    assert label_policy["translation_policy"] == "preserve"

    output = tmp_path / "package"
    write_unified_normalization(result, output)
    asset_manifest = json.loads(
        (output / "asset_manifest.json").read_text(encoding="utf-8")
    )
    projection = json.loads(
        (output / "admitted_projection_v1.json").read_text(encoding="utf-8")
    )
    binding = next(
        row for row in asset_manifest["block_bindings"] if row["block_id"] == label_row["block_id"]
    )
    projection_row = next(
        row for row in projection["rows"] if row["block_id"] == label_row["block_id"]
    )
    prose_id = "d2l_chapter_one_b003"
    prose_binding = next(
        row for row in asset_manifest["block_bindings"] if row["block_id"] == prose_id
    )
    prose_projection = next(
        row for row in projection["rows"] if row["block_id"] == prose_id
    )
    assert binding == {
        "block_id": label_row["block_id"],
        "source_kind": "label",
        "semantic_kind": "structural",
        "semantic_subtype": "label",
        "translation_policy": "preserve",
        "asset_ids": [],
        "render_role": "structural",
        "review_required": False,
    }
    assert projection_row["channel"] == "preserve_only"
    assert prose_binding["semantic_kind"] == "text"
    assert prose_binding["review_required"] is False
    assert prose_projection["channel"] == "semantic_text"


def test_presegmented_normalizer_rejects_source_bytes_outside_validated_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bundle_root, adapter = _synthetic_adapter(tmp_path)
    source.write_bytes(source.read_bytes() + b"\nforeign")
    monkeypatch.setattr(
        "pipeline.ingest.presegmented_source_normalizer.validate_d2l_presegmented_output",
        lambda *_args, **_kwargs: adapter,
    )
    with pytest.raises(UnifiedContractError, match="byte-identical"):
        normalize_presegmented_source(
            source,
            bundle_root=bundle_root,
            doc_id="project_import_02",
        )


def test_presegmented_normalizer_rejects_unsafe_capture_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, bundle_root, adapter = _synthetic_adapter(tmp_path)
    monkeypatch.setattr(
        "pipeline.ingest.presegmented_source_normalizer.validate_d2l_presegmented_output",
        lambda *_args, **_kwargs: adapter,
    )
    with pytest.raises(UnifiedContractError, match="capture_relative_path"):
        normalize_presegmented_source(
            source,
            bundle_root=bundle_root,
            doc_id="project_import_03",
            capture_relative_path="../foreign",
        )
