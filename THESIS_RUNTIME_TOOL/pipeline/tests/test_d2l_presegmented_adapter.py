from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pipeline.ingest import d2l_presegmented_adapter as adapter
from pipeline.ingest.d2l_presegmented_adapter import (
    D2lCaptureSeal,
    D2lPresegmentedAdapterError,
    convert_d2l_presegmented_capture,
    validate_d2l_presegmented_output,
)


REAL_CAPTURE_ROOT = Path(
    r"C:\work\agent-based-translation-baseline-captures\d2l_full_book_chatgpt_web_oneshot_v1\input"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _row(marker: str, block_id: str, chapter_id: str, order: int, kind: str, text: str):
    encoded = text.encode("utf-8")
    return {
        "marker": marker,
        "block_id": block_id,
        "chapter_id": chapter_id,
        "order_index": order,
        "block_type": kind,
        "source_sha256": _sha256(encoded),
        "source_utf8_bytes": len(encoded),
    }


def _source_bytes(texts: list[str]) -> bytes:
    chunks = [f"[[B{index + 1:04d}]]\n{text}" for index, text in enumerate(texts)]
    return ("\n\n".join(chunks) + "\n").encode("utf-8")


def _base_payloads() -> tuple[bytes, dict, dict]:
    texts = ["# Chapter One", "First prose.", "# Chapter Two", "Second prose."]
    rows = [
        _row("B0001", "d2l_ch1_b001", "d2l_ch1", 0, "heading", texts[0]),
        _row("B0002", "d2l_ch1_b002", "d2l_ch1", 1, "prose", texts[1]),
        _row("B0003", "d2l_ch2_b001", "d2l_ch2", 2, "heading", texts[2]),
        _row("B0004", "d2l_ch2_b002", "d2l_ch2", 3, "math_block", texts[3]),
    ]
    block_map = {
        "schema_version": adapter.LEGACY_BLOCK_MAP_SCHEMA_VERSION,
        "document_id": "d2l",
        "rows": rows,
    }
    manifest = {
        "block_count": 4,
        "block_map_file": adapter.LEGACY_BLOCK_MAP_FILE,
        "block_map_sha256": "0" * 64,
        "chapter_count": 2,
        "created_at": "2026-07-20T22:13:42Z",
        "document_id": "d2l",
        "encoding": "UTF-8 without BOM",
        "intended_mode": "chatgpt_web_single_chat_single_prompt_no_continue",
        "line_endings": "LF",
        "prompt_file": "prompt.txt",
        "prompt_sha256": "1" * 64,
        "schema_version": adapter.LEGACY_MANIFEST_SCHEMA_VERSION,
        "source_db_path": r"C:\evidence\memory.sqlite3",
        "source_db_sha256": "2" * 64,
        "source_text_utf8_bytes": sum(row["source_utf8_bytes"] for row in rows),
        "upload_file": "d2l_full_book_en_marked_v1.md",
        "upload_file_sha256": "0" * 64,
        "upload_file_utf8_bytes": 0,
    }
    return _source_bytes(texts), block_map, manifest


def _write_capture(
    root: Path,
    source: bytes,
    block_map: dict,
    manifest: dict,
    *,
    map_bytes: bytes | None = None,
    manifest_bytes: bytes | None = None,
) -> D2lCaptureSeal:
    root.mkdir(parents=True, exist_ok=True)
    source_path = root / "d2l_full_book_en_marked_v1.md"
    map_path = root / "block_map.json"
    manifest_path = root / "manifest.json"
    source_path.write_bytes(source)
    actual_map_bytes = map_bytes if map_bytes is not None else _json_bytes(block_map)
    map_path.write_bytes(actual_map_bytes)

    updated_manifest = dict(manifest)
    updated_manifest["upload_file_sha256"] = _sha256(source)
    updated_manifest["upload_file_utf8_bytes"] = len(source)
    updated_manifest["block_map_sha256"] = _sha256(actual_map_bytes)
    actual_manifest_bytes = (
        manifest_bytes if manifest_bytes is not None else _json_bytes(updated_manifest)
    )
    manifest_path.write_bytes(actual_manifest_bytes)
    return D2lCaptureSeal(
        document_id="d2l",
        source_file="d2l_full_book_en_marked_v1.md",
        source_sha256=_sha256(source),
        source_utf8_bytes=len(source),
        source_text_utf8_bytes=updated_manifest["source_text_utf8_bytes"],
        block_map_sha256=_sha256(actual_map_bytes),
        block_map_utf8_bytes=len(actual_map_bytes),
        manifest_sha256=_sha256(actual_manifest_bytes),
        manifest_utf8_bytes=len(actual_manifest_bytes),
        source_db_sha256=updated_manifest["source_db_sha256"],
        block_count=updated_manifest["block_count"],
        chapter_count=updated_manifest["chapter_count"],
    )


def _capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, block_map, manifest = _base_payloads()
    root = tmp_path / "capture"
    seal = _write_capture(root, source, block_map, manifest)
    monkeypatch.setattr(adapter, "AUTHORITATIVE_D2L_CAPTURE", seal)
    return root, seal, source, block_map, manifest


def _reseal(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    source: bytes,
    block_map: dict,
    manifest: dict,
    **kwargs,
) -> D2lCaptureSeal:
    seal = _write_capture(root, source, block_map, manifest, **kwargs)
    monkeypatch.setattr(adapter, "AUTHORITATIVE_D2L_CAPTURE", seal)
    return seal


def test_converts_rows_mechanically_and_preserves_source_bytes(tmp_path, monkeypatch):
    root, _seal, source, block_map, _manifest = _capture(tmp_path, monkeypatch)
    result = convert_d2l_presegmented_capture(root, tmp_path / "output")

    assert result.bundle.block_count == 4
    assert result.bundle.chapter_count == 2
    assert [chapter.title for chapter in result.bundle.chapters] == [
        "Chapter One",
        "Chapter Two",
    ]
    assert (result.output_root / "d2l_full_book_en_marked_v1.md").read_bytes() == source
    converted = json.loads((result.output_root / "block_map.json").read_text("utf-8"))
    assert converted["rows"] == block_map["rows"]
    assert [row["block_type"] for row in converted["rows"]] == [
        "heading",
        "prose",
        "heading",
        "math_block",
    ]
    assert not (root / "prompt.txt").exists()
    assert set(path.name for path in result.output_root.iterdir()) == {
        "manifest.json",
        "block_map.json",
        "d2l_full_book_en_marked_v1.md",
        adapter.OUTPUT_RECEIPT_FILE,
    }


def test_receipt_binds_upstream_and_output_identities(tmp_path, monkeypatch):
    root, seal, _source, _block_map, _manifest = _capture(tmp_path, monkeypatch)
    result = convert_d2l_presegmented_capture(root, tmp_path / "output")
    receipt = json.loads(result.receipt_path.read_text("utf-8"))

    assert receipt["upstream"]["legacy_manifest"]["sha256"] == seal.manifest_sha256
    assert receipt["upstream"]["legacy_block_map"]["sha256"] == seal.block_map_sha256
    assert receipt["upstream"]["marked_source"]["sha256"] == seal.source_sha256
    assert receipt["upstream"]["source_db_sha256"] == seal.source_db_sha256
    assert receipt["output"]["bundle_identity_sha256"] == result.bundle.identity_sha256
    assert validate_d2l_presegmented_output(
        result.output_root, expected_receipt_sha256=result.receipt_sha256
    ).adapter_identity_sha256 == result.adapter_identity_sha256


def test_repeat_conversion_is_byte_deterministic(tmp_path, monkeypatch):
    root, _seal, _source, _block_map, _manifest = _capture(tmp_path, monkeypatch)
    first = convert_d2l_presegmented_capture(root, tmp_path / "one")
    second = convert_d2l_presegmented_capture(root, tmp_path / "two")

    for name in [
        "manifest.json",
        "block_map.json",
        "d2l_full_book_en_marked_v1.md",
        adapter.OUTPUT_RECEIPT_FILE,
    ]:
        assert (first.output_root / name).read_bytes() == (second.output_root / name).read_bytes()
    assert first.bundle.identity_sha256 == second.bundle.identity_sha256
    assert first.receipt_sha256 == second.receipt_sha256
    assert first.adapter_identity_sha256 == second.adapter_identity_sha256


@pytest.mark.parametrize("target", ["source", "map", "manifest"])
def test_rejects_physical_hash_tamper_before_output(tmp_path, monkeypatch, target):
    root, _seal, _source, _block_map, _manifest = _capture(tmp_path, monkeypatch)
    path = {
        "source": root / "d2l_full_book_en_marked_v1.md",
        "map": root / "block_map.json",
        "manifest": root / "manifest.json",
    }[target]
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(D2lPresegmentedAdapterError, match="sealed inventory"):
        convert_d2l_presegmented_capture(root, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_rejects_duplicate_json_keys_even_when_physically_resealed(tmp_path, monkeypatch):
    root, _seal, source, block_map, manifest = _capture(tmp_path, monkeypatch)
    duplicate_map = (
        '{"schema_version":"chatgpt_web_full_book_block_map_v1",'
        '"schema_version":"chatgpt_web_full_book_block_map_v1",'
        '"document_id":"d2l","rows":[]}'
    ).encode("utf-8")
    _reseal(
        monkeypatch,
        root,
        source,
        block_map,
        manifest,
        map_bytes=duplicate_map,
    )
    with pytest.raises(D2lPresegmentedAdapterError, match="duplicate key"):
        convert_d2l_presegmented_capture(root, tmp_path / "output")


def test_rejects_unknown_legacy_manifest_field(tmp_path, monkeypatch):
    root, _seal, source, block_map, manifest = _capture(tmp_path, monkeypatch)
    manifest["unexpected"] = True
    _reseal(monkeypatch, root, source, block_map, manifest)
    with pytest.raises(D2lPresegmentedAdapterError, match="unknown keys"):
        convert_d2l_presegmented_capture(root, tmp_path / "output")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows.__setitem__(1, {**rows[1], "marker": "B0003"}), "B0001..B8803"),
        (
            lambda rows: rows.__setitem__(1, {**rows[1], "block_id": rows[0]["block_id"]}),
            "duplicate block IDs",
        ),
        (lambda rows: rows.__setitem__(1, {**rows[1], "order_index": 7}), "order_index"),
        (lambda rows: rows.__setitem__(1, {**rows[1], "block_type": "unknown"}), "not supported"),
        (
            lambda rows: rows.__setitem__(3, {**rows[3], "chapter_id": "d2l_ch1"}),
            "interleaved",
        ),
        (
            lambda rows: rows.__setitem__(2, {**rows[2], "block_type": "prose"}),
            "does not begin with a heading",
        ),
    ],
)
def test_rejects_resealed_map_semantic_drift(tmp_path, monkeypatch, mutation, message):
    root, _seal, source, block_map, manifest = _capture(tmp_path, monkeypatch)
    mutation(block_map["rows"])
    _reseal(monkeypatch, root, source, block_map, manifest)
    with pytest.raises(D2lPresegmentedAdapterError, match=message):
        convert_d2l_presegmented_capture(root, tmp_path / "output")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: b"\xef\xbb\xbf" + payload, "BOM"),
        (lambda payload: payload.replace(b"\n", b"\r\n"), "LF line endings"),
        (
            lambda payload: payload.replace(b"[[B0001]]", b"[[B0002]]", 1),
            "exactly cover",
        ),
        (
            lambda payload: payload.replace(b"[[B0002]]", b" [[B0002]]", 1),
            "malformed marker",
        ),
    ],
)
def test_rejects_resealed_source_transport_drift(tmp_path, monkeypatch, mutate, message):
    root, _seal, source, block_map, manifest = _capture(tmp_path, monkeypatch)
    changed = mutate(source)
    _reseal(monkeypatch, root, changed, block_map, manifest)
    with pytest.raises(D2lPresegmentedAdapterError, match=message):
        convert_d2l_presegmented_capture(root, tmp_path / "output")


def test_rejects_heading_without_markdown_title(tmp_path, monkeypatch):
    root, _seal, source, block_map, manifest = _capture(tmp_path, monkeypatch)
    changed = source.replace(b"# Chapter Two", b"Chapter Two")
    row = block_map["rows"][2]
    row["source_sha256"] = _sha256(b"Chapter Two")
    row["source_utf8_bytes"] = len(b"Chapter Two")
    manifest["source_text_utf8_bytes"] = sum(
        item["source_utf8_bytes"] for item in block_map["rows"]
    )
    _reseal(monkeypatch, root, changed, block_map, manifest)
    with pytest.raises(D2lPresegmentedAdapterError, match="Markdown title"):
        convert_d2l_presegmented_capture(root, tmp_path / "output")


def test_rejects_row_hash_drift_after_all_container_hashes_are_resealed(tmp_path, monkeypatch):
    root, _seal, source, block_map, manifest = _capture(tmp_path, monkeypatch)
    block_map["rows"][1]["source_sha256"] = "f" * 64
    _reseal(monkeypatch, root, source, block_map, manifest)
    with pytest.raises(D2lPresegmentedAdapterError, match="block hash mismatch"):
        convert_d2l_presegmented_capture(root, tmp_path / "output")


def test_rejects_existing_or_nested_output_root(tmp_path, monkeypatch):
    root, _seal, _source, _block_map, _manifest = _capture(tmp_path, monkeypatch)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(D2lPresegmentedAdapterError, match="must not already exist"):
        convert_d2l_presegmented_capture(root, existing)
    with pytest.raises(D2lPresegmentedAdapterError, match="must not be inside"):
        convert_d2l_presegmented_capture(root, root / "output")


def test_output_validation_rejects_receipt_and_bundle_tamper(tmp_path, monkeypatch):
    root, _seal, _source, _block_map, _manifest = _capture(tmp_path, monkeypatch)
    result = convert_d2l_presegmented_capture(root, tmp_path / "output")
    with pytest.raises(D2lPresegmentedAdapterError, match="expected binding"):
        validate_d2l_presegmented_output(
            result.output_root, expected_receipt_sha256="0" * 64
        )

    map_path = result.output_root / "block_map.json"
    map_path.write_bytes(map_path.read_bytes() + b" ")
    with pytest.raises(D2lPresegmentedAdapterError, match="does not match the receipt"):
        validate_d2l_presegmented_output(
            result.output_root, expected_receipt_sha256=result.receipt_sha256
        )


def test_output_validation_rejects_extra_file(tmp_path, monkeypatch):
    root, _seal, _source, _block_map, _manifest = _capture(tmp_path, monkeypatch)
    result = convert_d2l_presegmented_capture(root, tmp_path / "output")
    (result.output_root / "prompt.txt").write_text("must not enter adapter output", encoding="utf-8")
    with pytest.raises(D2lPresegmentedAdapterError, match="unexpected or missing file"):
        validate_d2l_presegmented_output(
            result.output_root, expected_receipt_sha256=result.receipt_sha256
        )


def test_receipt_schema_is_valid_draft_2020_12():
    schema_path = (
        Path(adapter.__file__).with_name("schemas")
        / "d2l_presegmented_adapter_receipt_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_adapter_has_no_generic_markdown_sqlite_or_transport_dependency():
    source = Path(adapter.__file__).read_text(encoding="utf-8").casefold()
    assert "d2l_markdown_loader" not in source
    assert "unified_source_normalizer" not in source
    assert "sqlite" not in source
    assert "requests" not in source
    assert "http://" not in source
    assert "https://" not in source


@pytest.mark.skipif(not REAL_CAPTURE_ROOT.is_dir(), reason="external D2L capture is unavailable")
def test_authoritative_d2l_capture_canary(tmp_path):
    result = convert_d2l_presegmented_capture(REAL_CAPTURE_ROOT, tmp_path / "d2l")
    assert result.bundle.document_id == "d2l"
    assert result.bundle.block_count == 8_803
    assert result.bundle.chapter_count == 22
    assert result.bundle.blocks[0].marker == "B0001"
    assert result.bundle.blocks[-1].marker == "B8803"
    assert result.bundle.source_sha256 == adapter.AUTHORITATIVE_D2L_CAPTURE.source_sha256
    assert sum(block.source_utf8_bytes for block in result.bundle.blocks) == 2_503_083
    assert validate_d2l_presegmented_output(
        result.output_root, expected_receipt_sha256=result.receipt_sha256
    ).adapter_identity_sha256 == result.adapter_identity_sha256
