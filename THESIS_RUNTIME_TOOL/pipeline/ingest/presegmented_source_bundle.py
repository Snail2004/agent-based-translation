"""Standalone validator for the pre-segmented source bundle contract.

This module deliberately has no dependency on the existing normalizers.  It
validates a small, content-addressed input package before a later adapter
turns it into the canonical source package.  Keeping this boundary standalone
also prevents a legacy block map from silently changing the existing Markdown
normalizer's behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "presegmented_source_bundle_v1"
BLOCK_MAP_SCHEMA_VERSION = "presegmented_block_map_v1"
ENCODING = "UTF-8"
LINE_ENDINGS = "LF"
TEXT_POLICY = "strip_outer_whitespace_v1"
DEFAULT_MANIFEST_NAME = "manifest.json"

SOURCE_FORMATS = frozenset({"epub", "html", "markdown", "pdf", "txt"})
SOURCE_BLOCK_KINDS = frozenset(
    {
        "heading",
        "prose",
        "paragraph",
        "dialogue",
        "footnote",
        "code",
        "math",
        "math_block",
        "equation",
        "image",
        "table",
        "label",
        "raw_html",
        "list",
        "separator",
        "directive",
        "block_quote",
    }
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class PresegmentedBundleError(ValueError):
    """Raised when a bundle cannot be accepted without guessing."""


@dataclass(frozen=True)
class PresegmentedBlock:
    marker: str
    block_id: str
    chapter_id: str
    order_index: int
    block_type: str
    source_text: str
    source_sha256: str
    source_utf8_bytes: int
    source_start_offset: int
    source_end_offset: int


@dataclass(frozen=True)
class PresegmentedChapter:
    chapter_id: str
    order_index: int
    title: str
    first_block_index: int
    last_block_index: int
    block_ids: tuple[str, ...]


@dataclass(frozen=True)
class PresegmentedBundle:
    bundle_root: Path
    manifest: dict[str, Any]
    block_map: dict[str, Any]
    source_sha256: str
    source_utf8_bytes: int
    blocks: tuple[PresegmentedBlock, ...]
    chapters: tuple[PresegmentedChapter, ...]
    identity_sha256: str

    @property
    def document_id(self) -> str:
        return str(self.manifest["document_id"])

    @property
    def source_format(self) -> str:
        return str(self.manifest["source_format"])

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    def summary(self) -> dict[str, Any]:
        """Return stable metadata suitable for a dry-run report."""

        return {
            "schema_version": SCHEMA_VERSION,
            "document_id": self.document_id,
            "source_format": self.source_format,
            "source_sha256": self.source_sha256,
            "block_map_sha256": str(self.manifest["block_map_sha256"]),
            "source_utf8_bytes": self.source_utf8_bytes,
            "blocks": self.block_count,
            "chapters": self.chapter_count,
            "identity_sha256": self.identity_sha256,
        }


def _fail(message: str) -> None:
    raise PresegmentedBundleError(message)


def _is_exact_bool(value: Any) -> bool:
    return type(value) is bool


def _require_string(value: Any, path: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{path} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{path} has an invalid value")
    return value


def _require_sha256(value: Any, path: str) -> str:
    result = _require_string(value, path)
    if _SHA256_RE.fullmatch(result.casefold()) is None:
        _fail(f"{path} must be a lowercase SHA-256 hex string")
    return result


def _require_nonnegative_int(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        _fail(f"{path} must be a non-negative integer")
    return value


def _require_exact_keys(payload: dict[str, Any], required: Iterable[str], path: str) -> None:
    required_set = set(required)
    actual = set(payload)
    missing = sorted(required_set - actual)
    extra = sorted(actual - required_set)
    if missing:
        _fail(f"{path} is missing keys: {', '.join(missing)}")
    if extra:
        _fail(f"{path} contains unknown keys: {', '.join(extra)}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PresegmentedBundleError(f"cannot read {label}: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail(f"{label} must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PresegmentedBundleError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _confined_file(root: Path, relative_name: Any, *, label: str) -> Path:
    name = _require_string(relative_name, f"manifest.{label}")
    if "\\" in name or name.startswith("/"):
        _fail(f"manifest.{label} must be a relative POSIX path")
    relative = PurePosixPath(name)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        _fail(f"manifest.{label} contains an unsafe path")
    root_resolved = root.resolve(strict=True)
    candidate_unresolved = root / Path(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            _fail(f"manifest.{label} must not traverse a symlink")
    candidate = candidate_unresolved.resolve(strict=True)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PresegmentedBundleError(f"manifest.{label} escapes the bundle root") from exc
    if not candidate.is_file():
        _fail(f"manifest.{label} must be a regular file")
    return candidate


def _validate_manifest(manifest: dict[str, Any]) -> None:
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "document_id",
            "source_format",
            "source_file",
            "source_sha256",
            "source_utf8_bytes",
            "block_map_file",
            "block_map_sha256",
            "block_count",
            "chapter_count",
            "encoding",
            "line_endings",
            "text_policy",
            "marker_syntax",
        },
        "manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        _fail("manifest.schema_version is not supported")
    _require_string(manifest["document_id"], "manifest.document_id", pattern=_ID_RE)
    _require_string(manifest["source_format"], "manifest.source_format")
    if manifest["source_format"] not in SOURCE_FORMATS:
        _fail("manifest.source_format is not a locked source format")
    _require_sha256(manifest["source_sha256"], "manifest.source_sha256")
    _require_sha256(manifest["block_map_sha256"], "manifest.block_map_sha256")
    _require_nonnegative_int(manifest["source_utf8_bytes"], "manifest.source_utf8_bytes")
    if manifest["block_count"] <= 0:
        _fail("manifest.block_count must be positive")
    if manifest["chapter_count"] <= 0:
        _fail("manifest.chapter_count must be positive")
    _require_nonnegative_int(manifest["block_count"], "manifest.block_count")
    _require_nonnegative_int(manifest["chapter_count"], "manifest.chapter_count")
    if manifest["encoding"] != ENCODING:
        _fail("manifest.encoding must be UTF-8")
    if manifest["line_endings"] != LINE_ENDINGS:
        _fail("manifest.line_endings must be LF")
    if manifest["text_policy"] != TEXT_POLICY:
        _fail("manifest.text_policy is not supported")
    syntax = manifest["marker_syntax"]
    if not isinstance(syntax, dict):
        _fail("manifest.marker_syntax must be an object")
    _require_exact_keys(syntax, {"prefix", "suffix"}, "manifest.marker_syntax")
    if syntax != {"prefix": "[[", "suffix": "]]"}:
        _fail("manifest.marker_syntax must use the closed [[marker]] syntax")


def _validate_block_map_shape(block_map: dict[str, Any], manifest: dict[str, Any]) -> None:
    _require_exact_keys(block_map, {"schema_version", "document_id", "chapters", "rows"}, "block_map")
    if block_map["schema_version"] != BLOCK_MAP_SCHEMA_VERSION:
        _fail("block_map.schema_version is not supported")
    if block_map["document_id"] != manifest["document_id"]:
        _fail("manifest and block_map document_id values differ")
    chapters = block_map["chapters"]
    rows = block_map["rows"]
    if not isinstance(chapters, list) or not chapters:
        _fail("block_map.chapters must be a non-empty list")
    if not isinstance(rows, list) or not rows:
        _fail("block_map.rows must be a non-empty list")
    if len(chapters) != manifest["chapter_count"]:
        _fail("manifest.chapter_count does not match block_map")
    if len(rows) != manifest["block_count"]:
        _fail("manifest.block_count does not match block_map")
    chapter_ids: list[str] = []
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            _fail(f"block_map.chapters[{index}] must be an object")
        _require_exact_keys(chapter, {"chapter_id", "order_index", "title"}, f"block_map.chapters[{index}]")
        chapter_id = _require_string(chapter["chapter_id"], f"block_map.chapters[{index}].chapter_id", pattern=_ID_RE)
        if chapter["order_index"] != index:
            _fail("block_map chapter order_index values must be contiguous and ordered")
        _require_string(chapter["title"], f"block_map.chapters[{index}].title")
        chapter_ids.append(chapter_id)
    if len(chapter_ids) != len(set(chapter_ids)):
        _fail("block_map contains duplicate chapter_id values")

    required_row_keys = {
        "marker",
        "block_id",
        "chapter_id",
        "order_index",
        "block_type",
        "source_sha256",
        "source_utf8_bytes",
    }
    block_ids: list[str] = []
    markers: list[str] = []
    row_chapters: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            _fail(f"block_map.rows[{index}] must be an object")
        _require_exact_keys(row, required_row_keys, f"block_map.rows[{index}]")
        marker = _require_string(row["marker"], f"block_map.rows[{index}].marker", pattern=_MARKER_RE)
        block_id = _require_string(row["block_id"], f"block_map.rows[{index}].block_id", pattern=_ID_RE)
        chapter_id = _require_string(row["chapter_id"], f"block_map.rows[{index}].chapter_id", pattern=_ID_RE)
        if chapter_id not in chapter_ids:
            _fail(f"block_map.rows[{index}].chapter_id is unknown")
        if row["order_index"] != index:
            _fail("block_map row order_index values must be contiguous and ordered")
        block_type = _require_string(row["block_type"], f"block_map.rows[{index}].block_type")
        if block_type not in SOURCE_BLOCK_KINDS:
            _fail(f"block_map.rows[{index}].block_type is not supported")
        _require_sha256(row["source_sha256"], f"block_map.rows[{index}].source_sha256")
        _require_nonnegative_int(row["source_utf8_bytes"], f"block_map.rows[{index}].source_utf8_bytes")
        block_ids.append(block_id)
        markers.append(marker)
        row_chapters.append(chapter_id)
    if len(block_ids) != len(set(block_ids)):
        _fail("block_map contains duplicate block_id values")
    if len(markers) != len(set(markers)):
        _fail("block_map contains duplicate marker values")
    if not set(row_chapters).issubset(set(chapter_ids)):
        _fail("block_map rows contain unknown chapters")


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_source(source: bytes, rows: list[dict[str, Any]]) -> tuple[PresegmentedBlock, ...]:
    if source.startswith(b"\xef\xbb\xbf"):
        _fail("source must not contain a UTF-8 BOM")
    if b"\r" in source:
        _fail("source must use LF line endings only")
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PresegmentedBundleError(f"source is not valid UTF-8: {exc}") from exc

    row_by_marker = {row["marker"]: row for row in rows}
    marker_lines: list[tuple[str, int, int]] = []
    offset = 0
    for line in source.split(b"\n"):
        try:
            text_line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PresegmentedBundleError(f"source contains invalid UTF-8 at offset {offset}") from exc
        marker_match = re.fullmatch(r"\[\[([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]", text_line)
        if marker_match:
            marker = marker_match.group(1)
            if marker not in row_by_marker:
                _fail(f"source contains an unknown marker: {marker}")
            marker_lines.append((marker, offset, offset + len(line) + 1))
        elif re.match(r"^\s*\[\[[A-Za-z0-9][A-Za-z0-9_.:-]*\]\]", text_line):
            _fail(f"source contains a malformed marker line at offset {offset}")
        offset += len(line) + 1
    if not marker_lines:
        _fail("source contains no marker lines")
    if source[: marker_lines[0][1]].strip():
        _fail("source contains non-whitespace content before the first marker")
    expected_markers = [row["marker"] for row in rows]
    actual_markers = [marker for marker, _start, _end in marker_lines]
    if actual_markers != expected_markers:
        _fail("source markers do not exactly match block_map order")

    blocks: list[PresegmentedBlock] = []
    for index, (marker, _marker_start, content_start) in enumerate(marker_lines):
        content_end = marker_lines[index + 1][1] if index + 1 < len(marker_lines) else len(source)
        raw_content = source[content_start:content_end]
        try:
            canonical_text = raw_content.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise PresegmentedBundleError(f"block {marker} is not valid UTF-8") from exc
        if not canonical_text:
            _fail(f"block {marker} has empty canonical text")
        encoded_text = canonical_text.encode("utf-8")
        row = rows[index]
        actual_sha = _sha256(encoded_text)
        if actual_sha != row["source_sha256"]:
            _fail(f"source hash mismatch for marker {marker}")
        if len(encoded_text) != row["source_utf8_bytes"]:
            _fail(f"source byte count mismatch for marker {marker}")
        blocks.append(
            PresegmentedBlock(
                marker=marker,
                block_id=row["block_id"],
                chapter_id=row["chapter_id"],
                order_index=row["order_index"],
                block_type=row["block_type"],
                source_text=canonical_text,
                source_sha256=actual_sha,
                source_utf8_bytes=len(encoded_text),
                source_start_offset=content_start,
                source_end_offset=content_end,
            )
        )
    return tuple(blocks)


def _build_chapters(block_map: dict[str, Any], blocks: tuple[PresegmentedBlock, ...]) -> tuple[PresegmentedChapter, ...]:
    chapter_rows = block_map["chapters"]
    chapters: list[PresegmentedChapter] = []
    start = 0
    for chapter in chapter_rows:
        chapter_id = chapter["chapter_id"]
        matching = [block for block in blocks if block.chapter_id == chapter_id]
        if not matching:
            _fail(f"chapter {chapter_id} has no blocks")
        if matching[0].order_index != start:
            _fail("chapter block ranges must be contiguous and source ordered")
        expected_indexes = list(range(start, start + len(matching)))
        if [block.order_index for block in matching] != expected_indexes:
            _fail("chapter block order is not contiguous")
        chapters.append(
            PresegmentedChapter(
                chapter_id=chapter_id,
                order_index=chapter["order_index"],
                title=chapter["title"],
                first_block_index=start,
                last_block_index=start + len(matching) - 1,
                block_ids=tuple(block.block_id for block in matching),
            )
        )
        start += len(matching)
    if start != len(blocks):
        _fail("every block must belong to exactly one chapter")
    return tuple(chapters)


def _identity(manifest: dict[str, Any], blocks: tuple[PresegmentedBlock, ...], chapters: tuple[PresegmentedChapter, ...]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "document_id": manifest["document_id"],
        "source_format": manifest["source_format"],
        "source_sha256": manifest["source_sha256"],
        "block_map_sha256": manifest["block_map_sha256"],
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "order_index": chapter.order_index,
                "first_block_index": chapter.first_block_index,
                "last_block_index": chapter.last_block_index,
                "block_ids": list(chapter.block_ids),
            }
            for chapter in chapters
        ],
        "blocks": [
            {
                "marker": block.marker,
                "block_id": block.block_id,
                "chapter_id": block.chapter_id,
                "order_index": block.order_index,
                "block_type": block.block_type,
                "source_sha256": block.source_sha256,
                "source_utf8_bytes": block.source_utf8_bytes,
            }
            for block in blocks
        ],
    }
    return _sha256(_canonical_json(payload))


def load_presegmented_bundle(bundle_root: str | Path) -> PresegmentedBundle:
    """Load and fully validate a directory-based pre-segmented bundle.

    The function reads only files named by the manifest, confines both paths
    to the bundle root, and returns no partially accepted result.  ZIP upload
    and conversion of legacy D2L captures are deliberately separate adapters.
    """

    root_input = Path(bundle_root)
    if root_input.is_symlink():
        _fail("bundle root must not be a symlink")
    root = root_input.resolve(strict=True)
    if not root.is_dir():
        _fail("bundle root must be a regular directory")
    manifest_path = _confined_file(root, DEFAULT_MANIFEST_NAME, label="manifest_file")
    manifest = _read_json(manifest_path, label="manifest.json")
    _validate_manifest(manifest)
    source_path = _confined_file(root, manifest["source_file"], label="source_file")
    block_map_path = _confined_file(root, manifest["block_map_file"], label="block_map_file")
    if source_path == block_map_path or source_path == manifest_path or block_map_path == manifest_path:
        _fail("manifest, source and block map must be distinct files")
    source = source_path.read_bytes()
    source_sha = _sha256(source)
    if source_sha != manifest["source_sha256"]:
        _fail("manifest.source_sha256 does not match source bytes")
    if len(source) != manifest["source_utf8_bytes"]:
        _fail("manifest.source_utf8_bytes does not match source bytes")
    if not source:
        _fail("source must not be empty")
    map_bytes = block_map_path.read_bytes()
    map_sha = _sha256(map_bytes)
    if map_sha != manifest["block_map_sha256"]:
        _fail("manifest.block_map_sha256 does not match block map bytes")
    block_map = _read_json(block_map_path, label="block_map.json")
    _validate_block_map_shape(block_map, manifest)
    blocks = _parse_source(source, block_map["rows"])
    chapters = _build_chapters(block_map, blocks)
    return PresegmentedBundle(
        bundle_root=root,
        manifest=manifest,
        block_map=block_map,
        source_sha256=source_sha,
        source_utf8_bytes=len(source),
        blocks=blocks,
        chapters=chapters,
        identity_sha256=_identity(manifest, blocks, chapters),
    )


validate_presegmented_bundle = load_presegmented_bundle


__all__ = [
    "BLOCK_MAP_SCHEMA_VERSION",
    "ENCODING",
    "LINE_ENDINGS",
    "PresegmentedBlock",
    "PresegmentedBundle",
    "PresegmentedBundleError",
    "PresegmentedChapter",
    "SCHEMA_VERSION",
    "SOURCE_BLOCK_KINDS",
    "SOURCE_FORMATS",
    "TEXT_POLICY",
    "load_presegmented_bundle",
    "validate_presegmented_bundle",
]
