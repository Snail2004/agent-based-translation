from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .presegmented_source_bundle import (
    BLOCK_MAP_SCHEMA_VERSION,
    ENCODING,
    LINE_ENDINGS,
    SCHEMA_VERSION,
    SOURCE_BLOCK_KINDS,
    TEXT_POLICY,
    PresegmentedBundle,
    PresegmentedBundleError,
    load_presegmented_bundle,
)


ADAPTER_VERSION = "d2l_presegmented_adapter_v1"
RECEIPT_SCHEMA_VERSION = "d2l_presegmented_adapter_receipt_v1"
CONVERSION_POLICY = "d2l_marked_capture_mechanical_v1"
LEGACY_MANIFEST_SCHEMA_VERSION = "chatgpt_web_full_book_input_manifest_v1"
LEGACY_BLOCK_MAP_SCHEMA_VERSION = "chatgpt_web_full_book_block_map_v1"

LEGACY_MANIFEST_FILE = "manifest.json"
LEGACY_BLOCK_MAP_FILE = "block_map.json"
OUTPUT_MANIFEST_FILE = "manifest.json"
OUTPUT_BLOCK_MAP_FILE = "block_map.json"
OUTPUT_RECEIPT_FILE = "d2l_presegmented_adapter_receipt_v1.json"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKER_RE = re.compile(r"^B[0-9]{4}$")


class D2lPresegmentedAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class D2lCaptureSeal:
    document_id: str
    source_file: str
    source_sha256: str
    source_utf8_bytes: int
    source_text_utf8_bytes: int
    block_map_sha256: str
    block_map_utf8_bytes: int
    manifest_sha256: str
    manifest_utf8_bytes: int
    source_db_sha256: str
    block_count: int
    chapter_count: int


AUTHORITATIVE_D2L_CAPTURE = D2lCaptureSeal(
    document_id="d2l",
    source_file="d2l_full_book_en_marked_v1.md",
    source_sha256="ebc05ffca36036b5ac2b9b1e6c6daa62d8449232a22b102653e1e028ef6d62d2",
    source_utf8_bytes=2_608_718,
    source_text_utf8_bytes=2_503_083,
    block_map_sha256="980ae6a472eef0c2a29ebc39007475c1413b92be4510b935f7258d0bb303afbe",
    block_map_utf8_bytes=2_383_268,
    manifest_sha256="84d4eb1a63c481f9c464b5ca908f03f92541b6d6187fa7d22fed2eeeff411504",
    manifest_utf8_bytes=990,
    source_db_sha256="64d98965f8859869931152b2aa814fb03afbf15e6a9853532fd0ef28b555c715",
    block_count=8_803,
    chapter_count=22,
)


@dataclass(frozen=True)
class D2lPresegmentedAdapterResult:
    output_root: Path
    bundle: PresegmentedBundle
    receipt_path: Path
    receipt_sha256: str
    adapter_identity_sha256: str

    def summary(self) -> dict[str, Any]:
        return {
            "adapter_version": ADAPTER_VERSION,
            "document_id": self.bundle.document_id,
            "blocks": self.bundle.block_count,
            "chapters": self.bundle.chapter_count,
            "bundle_identity_sha256": self.bundle.identity_sha256,
            "receipt_sha256": self.receipt_sha256,
            "adapter_identity_sha256": self.adapter_identity_sha256,
            "output_root": str(self.output_root),
        }


def _fail(message: str) -> None:
    raise D2lPresegmentedAdapterError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _load_json(payload: bytes, label: str) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        _fail(f"{label} must not contain a UTF-8 BOM")
    if b"\r" in payload:
        _fail(f"{label} must use LF line endings only")
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise D2lPresegmentedAdapterError(f"{label} is not valid UTF-8: {exc}") from exc
    try:
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except D2lPresegmentedAdapterError:
        raise
    except json.JSONDecodeError as exc:
        raise D2lPresegmentedAdapterError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing:
        _fail(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if extra:
        _fail(f"{label} contains unknown keys: {', '.join(sorted(extra))}")


def _require_string(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{label} has an invalid value")
    return value


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _require_sha256(value: Any, label: str) -> str:
    result = _require_string(value, label)
    if _SHA256_RE.fullmatch(result) is None:
        _fail(f"{label} must be a lowercase SHA-256 value")
    return result


def _root_directory(path: str | Path, label: str) -> Path:
    root = Path(path)
    if root.is_symlink():
        _fail(f"{label} must not be a symlink")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise D2lPresegmentedAdapterError(f"{label} does not exist") from exc
    if not resolved.is_dir():
        _fail(f"{label} must be a directory")
    return resolved


def _regular_file(root: Path, name: str, label: str) -> Path:
    if Path(name).name != name or "/" in name or "\\" in name:
        _fail(f"{label} must be a root-level file name")
    path = root / name
    if path.is_symlink():
        _fail(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise D2lPresegmentedAdapterError(f"{label} does not exist") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise D2lPresegmentedAdapterError(f"{label} escapes its root") from exc
    if not resolved.is_file():
        _fail(f"{label} must be a regular file")
    return resolved


def _verify_physical(payload: bytes, expected_sha256: str, expected_bytes: int, label: str) -> None:
    if len(payload) != expected_bytes:
        _fail(f"{label} byte count does not match the sealed inventory")
    if _sha256(payload) != expected_sha256:
        _fail(f"{label} SHA-256 does not match the sealed inventory")


def _validate_legacy_manifest(manifest: dict[str, Any], seal: D2lCaptureSeal) -> None:
    expected_keys = {
        "block_count",
        "block_map_file",
        "block_map_sha256",
        "chapter_count",
        "created_at",
        "document_id",
        "encoding",
        "intended_mode",
        "line_endings",
        "prompt_file",
        "prompt_sha256",
        "schema_version",
        "source_db_path",
        "source_db_sha256",
        "source_text_utf8_bytes",
        "upload_file",
        "upload_file_sha256",
        "upload_file_utf8_bytes",
    }
    _require_exact_keys(manifest, expected_keys, "legacy manifest")
    if manifest["schema_version"] != LEGACY_MANIFEST_SCHEMA_VERSION:
        _fail("legacy manifest schema_version is not supported")
    if manifest["document_id"] != seal.document_id:
        _fail("legacy manifest document_id does not match the sealed inventory")
    if manifest["upload_file"] != seal.source_file:
        _fail("legacy manifest upload_file does not match the sealed inventory")
    if manifest["upload_file_sha256"] != seal.source_sha256:
        _fail("legacy manifest source hash does not match the sealed inventory")
    if manifest["upload_file_utf8_bytes"] != seal.source_utf8_bytes:
        _fail("legacy manifest source byte count does not match the sealed inventory")
    if manifest["source_text_utf8_bytes"] != seal.source_text_utf8_bytes:
        _fail("legacy manifest source text byte count does not match the sealed inventory")
    if manifest["block_map_file"] != LEGACY_BLOCK_MAP_FILE:
        _fail("legacy manifest block_map_file is not supported")
    if manifest["block_map_sha256"] != seal.block_map_sha256:
        _fail("legacy manifest block map hash does not match the sealed inventory")
    if manifest["block_count"] != seal.block_count:
        _fail("legacy manifest block_count does not match the sealed inventory")
    if manifest["chapter_count"] != seal.chapter_count:
        _fail("legacy manifest chapter_count does not match the sealed inventory")
    if manifest["source_db_sha256"] != seal.source_db_sha256:
        _fail("legacy manifest source DB hash does not match the sealed inventory")
    if manifest["encoding"] != "UTF-8 without BOM" or manifest["line_endings"] != "LF":
        _fail("legacy manifest encoding contract is not supported")
    if manifest["prompt_file"] != "prompt.txt":
        _fail("legacy manifest prompt_file is unexpected")
    _require_sha256(manifest["prompt_sha256"], "legacy manifest.prompt_sha256")
    _require_string(manifest["created_at"], "legacy manifest.created_at")
    _require_string(manifest["intended_mode"], "legacy manifest.intended_mode")
    _require_string(manifest["source_db_path"], "legacy manifest.source_db_path")


def _validate_legacy_rows(block_map: dict[str, Any], seal: D2lCaptureSeal) -> list[dict[str, Any]]:
    _require_exact_keys(block_map, {"schema_version", "document_id", "rows"}, "legacy block map")
    if block_map["schema_version"] != LEGACY_BLOCK_MAP_SCHEMA_VERSION:
        _fail("legacy block map schema_version is not supported")
    if block_map["document_id"] != seal.document_id:
        _fail("legacy block map document_id does not match the sealed inventory")
    rows = block_map["rows"]
    if not isinstance(rows, list) or len(rows) != seal.block_count:
        _fail("legacy block map rows do not match the sealed block count")

    row_keys = {
        "marker",
        "block_id",
        "chapter_id",
        "order_index",
        "block_type",
        "source_sha256",
        "source_utf8_bytes",
    }
    markers: set[str] = set()
    block_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        label = f"legacy block map.rows[{index}]"
        if not isinstance(row, dict):
            _fail(f"{label} must be an object")
        _require_exact_keys(row, row_keys, label)
        expected_marker = f"B{index + 1:04d}"
        marker = _require_string(row["marker"], f"{label}.marker", pattern=_MARKER_RE)
        if marker != expected_marker:
            _fail("legacy markers must be the exact B0001..B8803 sequence")
        block_id = _require_string(row["block_id"], f"{label}.block_id", pattern=_ID_RE)
        chapter_id = _require_string(row["chapter_id"], f"{label}.chapter_id", pattern=_ID_RE)
        if row["order_index"] != index:
            _fail("legacy row order_index values must be contiguous and ordered")
        block_type = _require_string(row["block_type"], f"{label}.block_type")
        if block_type not in SOURCE_BLOCK_KINDS:
            _fail(f"{label}.block_type is not supported by the frozen contract")
        _require_sha256(row["source_sha256"], f"{label}.source_sha256")
        _require_int(row["source_utf8_bytes"], f"{label}.source_utf8_bytes")
        if marker in markers:
            _fail("legacy block map contains duplicate markers")
        if block_id in block_ids:
            _fail("legacy block map contains duplicate block IDs")
        markers.add(marker)
        block_ids.add(block_id)
        normalized.append(dict(row))
        if chapter_id != row["chapter_id"]:
            _fail("legacy chapter identity changed during validation")
    return normalized


def _parse_marked_source(
    source: bytes, rows: list[dict[str, Any]], seal: D2lCaptureSeal
) -> dict[str, str]:
    if source.startswith(b"\xef\xbb\xbf"):
        _fail("marked source must not contain a UTF-8 BOM")
    if b"\r" in source:
        _fail("marked source must use LF line endings only")
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise D2lPresegmentedAdapterError(f"marked source is not valid UTF-8: {exc}") from exc

    expected = [row["marker"] for row in rows]
    expected_set = set(expected)
    marker_lines: list[tuple[str, int, int]] = []
    offset = 0
    for line in source.split(b"\n"):
        text_line = line.decode("utf-8")
        match = re.fullmatch(r"\[\[(B[0-9]{4})\]\]", text_line)
        if match:
            marker = match.group(1)
            if marker not in expected_set:
                _fail(f"marked source contains unknown marker {marker}")
            marker_lines.append((marker, offset, offset + len(line) + 1))
        elif re.match(r"^\s*\[\[B[0-9]{4}\]\]", text_line):
            _fail(f"marked source contains malformed marker line at offset {offset}")
        offset += len(line) + 1

    if not marker_lines:
        _fail("marked source contains no marker lines")
    if source[: marker_lines[0][1]].strip():
        _fail("marked source contains content before B0001")
    actual = [marker for marker, _start, _end in marker_lines]
    if actual != expected:
        _fail("marked source markers do not exactly cover the legacy map in order")

    texts: dict[str, str] = {}
    total_text_bytes = 0
    for index, (marker, _start, content_start) in enumerate(marker_lines):
        content_end = marker_lines[index + 1][1] if index + 1 < len(marker_lines) else len(source)
        text = source[content_start:content_end].decode("utf-8").strip()
        if not text:
            _fail(f"marked source block {marker} is empty")
        encoded = text.encode("utf-8")
        row = rows[index]
        if _sha256(encoded) != row["source_sha256"]:
            _fail(f"marked source block hash mismatch for {marker}")
        if len(encoded) != row["source_utf8_bytes"]:
            _fail(f"marked source block byte count mismatch for {marker}")
        texts[marker] = text
        total_text_bytes += len(encoded)
    if total_text_bytes != seal.source_text_utf8_bytes:
        _fail("marked source canonical text byte total does not match the sealed inventory")
    return texts


def _derive_chapters(
    rows: list[dict[str, Any]], texts: dict[str, str], seal: D2lCaptureSeal
) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = None
    for row in rows:
        chapter_id = row["chapter_id"]
        if chapter_id == current:
            continue
        if chapter_id in seen:
            _fail("legacy chapter rows are interleaved")
        seen.add(chapter_id)
        current = chapter_id
        if row["block_type"] != "heading":
            _fail(f"chapter {chapter_id} does not begin with a heading block")
        first_line = texts[row["marker"]].splitlines()[0].strip()
        match = re.fullmatch(r"#{1,6}\s*(.+)", first_line)
        if match is None:
            _fail(f"chapter {chapter_id} heading does not have a Markdown title")
        title = match.group(1).strip()
        if not title:
            _fail(f"chapter {chapter_id} has an empty derived title")
        chapters.append(
            {"chapter_id": chapter_id, "order_index": len(chapters), "title": title}
        )
    if len(chapters) != seal.chapter_count:
        _fail("derived chapter count does not match the sealed inventory")
    return chapters


def _artifact(file_name: str, payload: bytes, *, schema_version: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "file": file_name,
        "sha256": _sha256(payload),
        "utf8_bytes": len(payload),
    }
    if schema_version is not None:
        result["schema_version"] = schema_version
    return result


@lru_cache(maxsize=1)
def _receipt_validator() -> Draft202012Validator:
    path = Path(__file__).with_name("schemas") / "d2l_presegmented_adapter_receipt_v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_receipt(receipt: dict[str, Any]) -> None:
    errors = sorted(_receipt_validator().iter_errors(receipt), key=lambda item: list(item.path))
    if errors:
        detail = "; ".join(error.message for error in errors[:3])
        _fail(f"adapter receipt does not match its schema: {detail}")


def _receipt_matches_seal(receipt: dict[str, Any], seal: D2lCaptureSeal) -> None:
    upstream = receipt["upstream"]
    if upstream["legacy_manifest"] != {
        "file": LEGACY_MANIFEST_FILE,
        "sha256": seal.manifest_sha256,
        "utf8_bytes": seal.manifest_utf8_bytes,
        "schema_version": LEGACY_MANIFEST_SCHEMA_VERSION,
    }:
        _fail("adapter receipt legacy manifest binding is invalid")
    if upstream["legacy_block_map"] != {
        "file": LEGACY_BLOCK_MAP_FILE,
        "sha256": seal.block_map_sha256,
        "utf8_bytes": seal.block_map_utf8_bytes,
        "schema_version": LEGACY_BLOCK_MAP_SCHEMA_VERSION,
    }:
        _fail("adapter receipt legacy block map binding is invalid")
    if upstream["marked_source"] != {
        "file": seal.source_file,
        "sha256": seal.source_sha256,
        "utf8_bytes": seal.source_utf8_bytes,
    }:
        _fail("adapter receipt marked source binding is invalid")
    if upstream["source_db_sha256"] != seal.source_db_sha256:
        _fail("adapter receipt source DB provenance is invalid")


def _prepare_output_root(output_root: str | Path, input_root: Path) -> tuple[Path, Path]:
    requested = Path(output_root)
    if requested.exists() or requested.is_symlink():
        _fail("output root must not already exist")
    parent = requested.parent
    if parent.is_symlink():
        _fail("output parent must not be a symlink")
    try:
        parent_resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise D2lPresegmentedAdapterError("output parent does not exist") from exc
    if not parent_resolved.is_dir():
        _fail("output parent must be a directory")
    target = parent_resolved / requested.name
    try:
        target.relative_to(input_root)
    except ValueError:
        pass
    else:
        _fail("output root must not be inside the legacy input root")
    return parent_resolved, target


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _adapter_identity(bundle_identity_sha256: str, receipt_sha256: str) -> str:
    return _sha256(
        _canonical_json(
            {
                "adapter_version": ADAPTER_VERSION,
                "bundle_identity_sha256": bundle_identity_sha256,
                "receipt_sha256": receipt_sha256,
            }
        )
    )


def validate_d2l_presegmented_output(
    output_root: str | Path, *, expected_receipt_sha256: str
) -> D2lPresegmentedAdapterResult:
    root = _root_directory(output_root, "adapter output root")
    expected_receipt_sha256 = _require_sha256(
        expected_receipt_sha256, "expected_receipt_sha256"
    )
    receipt_path = _regular_file(root, OUTPUT_RECEIPT_FILE, "adapter receipt")
    receipt_bytes = receipt_path.read_bytes()
    receipt_sha256 = _sha256(receipt_bytes)
    if receipt_sha256 != expected_receipt_sha256:
        _fail("adapter receipt hash does not match the expected binding")
    receipt = _load_json(receipt_bytes, "adapter receipt")
    _validate_receipt(receipt)
    _receipt_matches_seal(receipt, AUTHORITATIVE_D2L_CAPTURE)

    output = receipt["output"]
    source_name = output["marked_source"]["file"]
    expected_names = {
        OUTPUT_MANIFEST_FILE,
        OUTPUT_BLOCK_MAP_FILE,
        OUTPUT_RECEIPT_FILE,
        source_name,
    }
    actual_names = {entry.name for entry in root.iterdir()}
    if actual_names != expected_names:
        _fail("adapter output contains an unexpected or missing file")
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_file():
            _fail("adapter output entries must be regular files")

    manifest_path = _regular_file(root, OUTPUT_MANIFEST_FILE, "converted manifest")
    map_path = _regular_file(root, OUTPUT_BLOCK_MAP_FILE, "converted block map")
    source_path = _regular_file(root, source_name, "converted marked source")
    physical = {
        "manifest": manifest_path.read_bytes(),
        "block_map": map_path.read_bytes(),
        "marked_source": source_path.read_bytes(),
    }
    for key, payload in physical.items():
        bound = output[key]
        if _sha256(payload) != bound["sha256"] or len(payload) != bound["utf8_bytes"]:
            _fail(f"adapter output {key} does not match the receipt")
    if physical["marked_source"] != source_path.read_bytes():
        _fail("adapter output source changed while validating")

    try:
        bundle = load_presegmented_bundle(root)
    except PresegmentedBundleError as exc:
        raise D2lPresegmentedAdapterError(f"frozen bundle validation failed: {exc}") from exc
    if output["document_id"] != bundle.document_id:
        _fail("adapter receipt document_id does not match the bundle")
    if output["block_count"] != bundle.block_count or output["chapter_count"] != bundle.chapter_count:
        _fail("adapter receipt counts do not match the bundle")
    if output["bundle_identity_sha256"] != bundle.identity_sha256:
        _fail("adapter receipt identity does not match the bundle")
    if output["marked_source"]["sha256"] != receipt["upstream"]["marked_source"]["sha256"]:
        _fail("converted marked source is not byte-identical to the upstream source")

    return D2lPresegmentedAdapterResult(
        output_root=root,
        bundle=bundle,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
        adapter_identity_sha256=_adapter_identity(bundle.identity_sha256, receipt_sha256),
    )


def convert_d2l_presegmented_capture(
    input_root: str | Path, output_root: str | Path
) -> D2lPresegmentedAdapterResult:
    seal = AUTHORITATIVE_D2L_CAPTURE
    input_directory = _root_directory(input_root, "legacy capture root")
    manifest_path = _regular_file(input_directory, LEGACY_MANIFEST_FILE, "legacy manifest")
    map_path = _regular_file(input_directory, LEGACY_BLOCK_MAP_FILE, "legacy block map")
    source_path = _regular_file(input_directory, seal.source_file, "legacy marked source")

    legacy_manifest_bytes = manifest_path.read_bytes()
    legacy_map_bytes = map_path.read_bytes()
    source_bytes = source_path.read_bytes()
    _verify_physical(
        legacy_manifest_bytes, seal.manifest_sha256, seal.manifest_utf8_bytes, "legacy manifest"
    )
    _verify_physical(legacy_map_bytes, seal.block_map_sha256, seal.block_map_utf8_bytes, "legacy block map")
    _verify_physical(source_bytes, seal.source_sha256, seal.source_utf8_bytes, "legacy marked source")

    legacy_manifest = _load_json(legacy_manifest_bytes, "legacy manifest")
    legacy_map = _load_json(legacy_map_bytes, "legacy block map")
    _validate_legacy_manifest(legacy_manifest, seal)
    rows = _validate_legacy_rows(legacy_map, seal)
    texts = _parse_marked_source(source_bytes, rows, seal)
    chapters = _derive_chapters(rows, texts, seal)

    converted_map = {
        "schema_version": BLOCK_MAP_SCHEMA_VERSION,
        "document_id": seal.document_id,
        "chapters": chapters,
        "rows": rows,
    }
    converted_map_bytes = _canonical_json(converted_map)
    converted_manifest = {
        "schema_version": SCHEMA_VERSION,
        "document_id": seal.document_id,
        "source_format": "markdown",
        "source_file": seal.source_file,
        "source_sha256": seal.source_sha256,
        "source_utf8_bytes": seal.source_utf8_bytes,
        "block_map_file": OUTPUT_BLOCK_MAP_FILE,
        "block_map_sha256": _sha256(converted_map_bytes),
        "block_count": seal.block_count,
        "chapter_count": seal.chapter_count,
        "encoding": ENCODING,
        "line_endings": LINE_ENDINGS,
        "text_policy": TEXT_POLICY,
        "marker_syntax": {"prefix": "[[", "suffix": "]]"},
    }
    converted_manifest_bytes = _canonical_json(converted_manifest)

    parent, target = _prepare_output_root(output_root, input_directory)
    temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=parent))
    published = False
    try:
        _write_file(temp / seal.source_file, source_bytes)
        _write_file(temp / OUTPUT_BLOCK_MAP_FILE, converted_map_bytes)
        _write_file(temp / OUTPUT_MANIFEST_FILE, converted_manifest_bytes)
        try:
            bundle = load_presegmented_bundle(temp)
        except PresegmentedBundleError as exc:
            raise D2lPresegmentedAdapterError(f"frozen bundle validation failed: {exc}") from exc

        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "conversion_policy": CONVERSION_POLICY,
            "document_id": seal.document_id,
            "upstream": {
                "legacy_manifest": _artifact(
                    LEGACY_MANIFEST_FILE,
                    legacy_manifest_bytes,
                    schema_version=LEGACY_MANIFEST_SCHEMA_VERSION,
                ),
                "legacy_block_map": _artifact(
                    LEGACY_BLOCK_MAP_FILE,
                    legacy_map_bytes,
                    schema_version=LEGACY_BLOCK_MAP_SCHEMA_VERSION,
                ),
                "marked_source": _artifact(seal.source_file, source_bytes),
                "source_db_sha256": seal.source_db_sha256,
            },
            "output": {
                "manifest": _artifact(
                    OUTPUT_MANIFEST_FILE,
                    converted_manifest_bytes,
                    schema_version=SCHEMA_VERSION,
                ),
                "block_map": _artifact(
                    OUTPUT_BLOCK_MAP_FILE,
                    converted_map_bytes,
                    schema_version=BLOCK_MAP_SCHEMA_VERSION,
                ),
                "marked_source": _artifact(seal.source_file, source_bytes),
                "document_id": bundle.document_id,
                "block_count": bundle.block_count,
                "chapter_count": bundle.chapter_count,
                "bundle_identity_sha256": bundle.identity_sha256,
            },
        }
        _validate_receipt(receipt)
        receipt_bytes = _canonical_json(receipt)
        receipt_sha256 = _sha256(receipt_bytes)
        _write_file(temp / OUTPUT_RECEIPT_FILE, receipt_bytes)
        validate_d2l_presegmented_output(temp, expected_receipt_sha256=receipt_sha256)
        os.replace(temp, target)
        published = True
        return validate_d2l_presegmented_output(
            target, expected_receipt_sha256=receipt_sha256
        )
    finally:
        if not published and temp.exists():
            shutil.rmtree(temp)


__all__ = [
    "ADAPTER_VERSION",
    "AUTHORITATIVE_D2L_CAPTURE",
    "CONVERSION_POLICY",
    "D2lCaptureSeal",
    "D2lPresegmentedAdapterError",
    "D2lPresegmentedAdapterResult",
    "OUTPUT_RECEIPT_FILE",
    "RECEIPT_SCHEMA_VERSION",
    "convert_d2l_presegmented_capture",
    "validate_d2l_presegmented_output",
]
