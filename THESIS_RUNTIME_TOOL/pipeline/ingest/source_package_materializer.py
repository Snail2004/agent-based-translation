from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import html
import json
import mimetypes
import os
import posixpath
import re
import tempfile
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlsplit
from xml.etree import ElementTree as ET

from pipeline.ingest.admitted_projection import build_admitted_projection
from pipeline.ingest.canonical_source_package import (
    CanonicalSourcePackageError,
    canonical_json_sha256,
    seal_asset_manifest,
    validate_canonical_source_package,
)


_RICH_KINDS = {"table", "image", "equation", "code"}
_TEXT_KINDS = {
    "caption",
    "paragraph",
    "dialogue",
    "block_quote",
    "list",
    "list_item",
    "footnote",
    "address",
    "summary",
    "verse",
    "preformatted",
}
_STRUCTURAL_KINDS = {"separator", "directive", "footer", "header"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_HTML_STRUCTURAL_TAGS = {
    "html",
    "body",
    "header",
    "footer",
    "nav",
    "main",
    "article",
    "section",
    "aside",
    "div",
    "figure",
    "details",
    "ul",
    "ol",
    "dl",
    "p",
    "pre",
    "figcaption",
    "dt",
    "dd",
    "address",
    "summary",
    "blockquote",
    "li",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
_AUTO_CLOSE_SAME = {"p", "li", "dt", "dd", "tr", "th", "td", "option"}
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<target><[^>]+>|[^\s)]+)"
    r"(?:\s+[\"'](?P<title>[^\"']*)[\"'])?\s*\)"
)
_DISPLAY_MATH_RE = re.compile(
    r"(?<!\\)\$\$(?P<math>.+?)(?<!\\)\$\$",
    re.DOTALL,
)
_INLINE_MATH_RE = re.compile(r"(?<!\\)\$(?!\$)(?P<math>[^\n$]+?)(?<!\\)\$")


@dataclass(frozen=True)
class SourcePackageWriteResult:
    asset_manifest_path: Path
    admitted_projection_path: Path
    validation_report: dict[str, Any]


@dataclass
class _RawHtmlNode:
    tag: str
    path: str
    parent_path: str
    attrs: dict[str, str]
    start_offset: int
    end_offset: int | None


class _RawHtmlParser(HTMLParser):
    """Retain source slices while reproducing the normalizer's DOM paths."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0]
        for match in re.finditer(r"\n", source):
            self.line_offsets.append(match.end())
        self.root = _RawHtmlNode(
            tag="document",
            path="",
            parent_path="",
            attrs={},
            start_offset=0,
            end_offset=len(source),
        )
        self.stack = [self.root]
        self.nodes: list[_RawHtmlNode] = []
        self.child_counts: dict[tuple[str, str], int] = {}

    def _offset(self) -> int:
        line, column = self.getpos()
        line_index = max(0, min(line - 1, len(self.line_offsets) - 1))
        return self.line_offsets[line_index] + column

    def _token_end(self, start: int) -> int:
        closing = self.source.find(">", start)
        return len(self.source) if closing < 0 else closing + 1

    def _close_top(self, offset: int) -> None:
        if len(self.stack) <= 1:
            return
        node = self.stack.pop()
        if node.end_offset is None:
            node.end_offset = offset

    def _append_node(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        name = tag.casefold()
        start = self._offset()
        if self.stack[-1].tag == "p" and name in _HTML_STRUCTURAL_TAGS:
            self._close_top(start)
        if name in _AUTO_CLOSE_SAME and self.stack[-1].tag == name:
            self._close_top(start)
        parent = self.stack[-1]
        count_key = (parent.path, name)
        ordinal = self.child_counts.get(count_key, 0) + 1
        self.child_counts[count_key] = ordinal
        path = f"{parent.path}/{name}[{ordinal}]"
        node = _RawHtmlNode(
            tag=name,
            path=path,
            parent_path=parent.path,
            attrs={str(key).casefold(): str(value or "") for key, value in attrs},
            start_offset=start,
            end_offset=None,
        )
        self.nodes.append(node)
        if self_closing or name in _VOID_TAGS:
            node.end_offset = self._token_end(start)
        else:
            self.stack.append(node)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_node(tag, attrs, self_closing=False)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._append_node(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        start = self._offset()
        end = self._token_end(start)
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag != name:
                continue
            for dangling in self.stack[index + 1 :]:
                if dangling.end_offset is None:
                    dangling.end_offset = start
            matched = self.stack[index]
            if matched.end_offset is None:
                matched.end_offset = end
            del self.stack[index:]
            return

    def close(self) -> None:
        super().close()
        for node in self.stack[1:]:
            if node.end_offset is None:
                node.end_offset = len(self.source)
        self.stack = [self.root]

    def by_path(self) -> dict[str, _RawHtmlNode]:
        return {node.path: node for node in self.nodes}

    def fragment(self, node: _RawHtmlNode) -> bytes:
        end = node.end_offset if node.end_offset is not None else len(self.source)
        return self.source[node.start_offset:end].encode("utf-8")

    def descendants(self, path: str, *, tag: str | None = None) -> list[_RawHtmlNode]:
        prefix = f"{path}/"
        return [
            node
            for node in self.nodes
            if node.path.startswith(prefix) and (tag is None or node.tag == tag)
        ]


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_html_text(fragment: bytes) -> str:
    parser = _PlainTextParser()
    parser.feed(fragment.decode("utf-8", errors="replace"))
    return " ".join(" ".join(parser.parts).split())


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_json_write(path: Path, payload: Any) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_bytes_write(path, encoded)


def _safe_suffix(value: str | None, *, fallback: str) -> str:
    suffix = str(value or "").casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    return fallback


def _canonical_key(payload: dict[str, Any]) -> str:
    return canonical_json_sha256(payload)


class _AssetCollector:
    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root
        self.assets: list[dict[str, Any]] = []
        self._by_key: dict[str, str] = {}
        self._by_id: dict[str, str] = {}

    def _identity(
        self,
        *,
        kind: str,
        media_type: str,
        policy: str,
        availability: str,
        source_locator: dict[str, Any],
        metadata: dict[str, Any],
        payload_sha256: str | None,
    ) -> tuple[str, str]:
        key = _canonical_key(
            {
                "kind": kind,
                "media_type": media_type,
                "translation_policy": policy,
                "availability": availability,
                "source_locator": source_locator,
                "metadata": metadata,
                "payload_sha256": payload_sha256,
            }
        )
        asset_id = f"ast_{key[:24]}"
        collision = self._by_id.get(asset_id)
        if collision is not None and collision != key:
            raise CanonicalSourcePackageError(f"asset id collision: {asset_id}")
        self._by_id[asset_id] = key
        return key, asset_id

    def materialized(
        self,
        *,
        kind: str,
        media_type: str,
        policy: str,
        source_locator: dict[str, Any],
        metadata: dict[str, Any],
        payload: bytes,
        suffix: str,
        review_required: bool = False,
    ) -> str:
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        key, asset_id = self._identity(
            kind=kind,
            media_type=media_type,
            policy=policy,
            availability="materialized",
            source_locator=source_locator,
            metadata=metadata,
            payload_sha256=payload_sha256,
        )
        prior = self._by_key.get(key)
        if prior is not None:
            return prior
        package_path = f"assets/{asset_id}{_safe_suffix(suffix, fallback='.bin')}"
        _atomic_bytes_write(
            self.package_root / Path(*PurePosixPath(package_path).parts),
            payload,
        )
        self.assets.append(
            {
                "asset_id": asset_id,
                "kind": kind,
                "media_type": media_type,
                "translation_policy": policy,
                "availability": "materialized",
                "package_path": package_path,
                "sha256": payload_sha256,
                "source_locator": source_locator,
                "metadata": metadata,
                "review_required": review_required,
            }
        )
        self._by_key[key] = asset_id
        return asset_id

    def unavailable(
        self,
        *,
        kind: str,
        media_type: str,
        policy: str,
        availability: str,
        source_locator: dict[str, Any],
        metadata: dict[str, Any],
        review_required: bool,
    ) -> str:
        if availability not in {"source_reference", "missing"}:
            raise ValueError(f"Unsupported unavailable asset state: {availability}")
        key, asset_id = self._identity(
            kind=kind,
            media_type=media_type,
            policy=policy,
            availability=availability,
            source_locator=source_locator,
            metadata=metadata,
            payload_sha256=None,
        )
        prior = self._by_key.get(key)
        if prior is not None:
            return prior
        self.assets.append(
            {
                "asset_id": asset_id,
                "kind": kind,
                "media_type": media_type,
                "translation_policy": policy,
                "availability": availability,
                "package_path": None,
                "sha256": None,
                "source_locator": source_locator,
                "metadata": metadata,
                "review_required": review_required,
            }
        )
        self._by_key[key] = asset_id
        return asset_id


def _flatten_document(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    blocks: list[dict[str, Any]] = []
    chapter_by_block: dict[str, str] = {}
    for chapter in document.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        for block in chapter.get("blocks") or []:
            block_id = str(block.get("block_id") or "")
            blocks.append(block)
            chapter_by_block[block_id] = chapter_id
    return blocks, chapter_by_block


def _verified_source(structure_manifest: dict[str, Any]) -> Path:
    source = structure_manifest.get("source")
    if not isinstance(source, dict):
        raise CanonicalSourcePackageError("structure.source must be an object")
    path_value = source.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise CanonicalSourcePackageError("structure.source.path must be present")
    source_path = Path(path_value).resolve()
    if not source_path.is_file():
        raise CanonicalSourcePackageError(f"source file is unavailable: {source_path}")
    expected = source.get("sha256")
    actual = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if actual != expected:
        raise CanonicalSourcePackageError("source bytes changed after normalization")
    return source_path


def _source_rows(structure_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = structure_manifest.get("source_map")
    if not isinstance(rows, list):
        raise CanonicalSourcePackageError("structure.source_map must be a list")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CanonicalSourcePackageError("structure.source_map rows must be objects")
        block_id = str(row.get("block_id") or "")
        if not block_id or block_id in result:
            raise CanonicalSourcePackageError(
                "structure.source_map must contain unique block ids"
            )
        result[block_id] = row
    return result


def _unit_rows(structure_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = structure_manifest.get("units")
    if not isinstance(rows, list):
        raise CanonicalSourcePackageError("structure.units must be a list")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CanonicalSourcePackageError("structure.units rows must be objects")
        chapter_id = str(row.get("chapter_id") or "")
        if not chapter_id or chapter_id in result:
            raise CanonicalSourcePackageError(
                "structure.units must map each chapter id exactly once"
            )
        result[chapter_id] = row
    return result


def _block_policy_rows(structure_manifest: dict[str, Any]) -> dict[str, str]:
    rows = structure_manifest.get("block_policies") or []
    if not isinstance(rows, list):
        raise CanonicalSourcePackageError("structure.block_policies must be a list")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CanonicalSourcePackageError(
                "structure.block_policies rows must be objects"
            )
        block_id = str(row.get("block_id") or "")
        policy = str(row.get("translation_policy") or "")
        if block_id:
            result[block_id] = policy
    return result


def _default_policy(source_kind: str, unit: dict[str, Any]) -> str:
    unit_policy = str(unit.get("translation_policy") or "review")
    if unit.get("review_required") or unit_policy == "review":
        return "review"
    if unit_policy in {"preserve", "exclude"}:
        return unit_policy
    if source_kind == "table":
        return "translate_structured"
    if source_kind in {"code", "math_block", "equation", "image", "raw_html"}:
        return "preserve"
    if source_kind in _STRUCTURAL_KINDS:
        return "preserve"
    return "translate"


def _semantic_shape(source_kind: str) -> tuple[str, str | None, str]:
    if source_kind == "heading":
        return "text", "heading", "text"
    if source_kind in _TEXT_KINDS:
        subtype = None if source_kind == "paragraph" else source_kind
        return "text", subtype, "text"
    if source_kind == "table":
        return "table", "source_table", "asset"
    if source_kind == "image":
        return "image", None, "asset"
    if source_kind in {"math_block", "equation", "math"}:
        return "equation", "source_math", "asset"
    if source_kind == "code":
        return "code", "source_code", "asset"
    if source_kind == "raw_html":
        return "structural", "raw_html", "asset"
    if source_kind in _STRUCTURAL_KINDS:
        return "structural", source_kind, "structural"
    return "unknown", source_kind or None, "placeholder"


def _line_fragment(source: Path, row: dict[str, Any]) -> bytes | None:
    line_range = row.get("line_range")
    if (
        not isinstance(line_range, list)
        or len(line_range) != 2
        or not all(isinstance(value, int) for value in line_range)
    ):
        return None
    start, end = line_range
    if start < 1 or end < start:
        return None
    with source.open(
        "r",
        encoding="utf-8-sig",
        errors="replace",
        newline="",
    ) as stream:
        lines = stream.readlines()
    if end > len(lines):
        return None
    return "".join(lines[start - 1 : end]).encode("utf-8")


def _source_locator(
    source_format: str,
    row: dict[str, Any],
    *,
    block_id: str,
) -> dict[str, Any]:
    locator = {"block_id": block_id, "source_format": source_format}
    for key in (
        "source_path",
        "line_range",
        "markdown_anchor",
        "html_path",
        "html_anchor",
        "epub_file",
        "epub_anchor",
        "pandoc_path",
        "page_number",
        "bbox_pdf",
        "odl_path",
        "odl_node_id",
        "provenance_precision",
    ):
        value = row.get(key)
        if value is not None:
            locator[key] = value
    return locator


def _target_value(value: str) -> str:
    target = html.unescape(str(value or "").strip())
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    return target


def _data_uri_payload(target: str) -> tuple[bytes, str] | None:
    if not target.casefold().startswith("data:") or "," not in target:
        return None
    header, data = target.split(",", 1)
    descriptor = header[5:]
    media_type = descriptor.split(";", 1)[0] or "application/octet-stream"
    try:
        payload = (
            base64.b64decode(data, validate=True)
            if ";base64" in descriptor.casefold()
            else unquote_to_bytes(data)
        )
    except (ValueError, binascii.Error):
        return None
    return payload, media_type


def _local_resource(
    source: Path,
    target: str,
) -> tuple[bytes, str, str] | None:
    value = _target_value(target)
    data_uri = _data_uri_payload(value)
    if data_uri is not None:
        payload, media_type = data_uri
        extension = mimetypes.guess_extension(media_type) or ".bin"
        return payload, media_type, extension
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    relative = unquote(parsed.path).replace("/", os.sep)
    candidate = (source.parent / relative).resolve()
    root = source.parent.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return candidate.read_bytes(), media_type, candidate.suffix or ".bin"


def _resource_asset(
    collector: _AssetCollector,
    *,
    source: Path,
    target: str,
    policy: str,
    locator: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[str, bool]:
    payload = _local_resource(source, target)
    resource_locator = {**locator, "resource_target": _target_value(target)}
    if payload is None:
        return (
            collector.unavailable(
                kind="image",
                media_type=mimetypes.guess_type(target)[0] or "application/octet-stream",
                policy=policy,
                availability="missing",
                source_locator=resource_locator,
                metadata=metadata,
                review_required=True,
            ),
            True,
        )
    content, media_type, suffix = payload
    return (
        collector.materialized(
            kind="image",
            media_type=media_type,
            policy=policy,
            source_locator=resource_locator,
            metadata=metadata,
            payload=content,
            suffix=suffix,
        ),
        False,
    )


class _HtmlContext:
    def __init__(self, source: Path) -> None:
        with source.open(
            "r",
            encoding="utf-8-sig",
            errors="replace",
            newline="",
        ) as stream:
            self.text = stream.read()
        self.parser = _RawHtmlParser(self.text)
        self.parser.feed(self.text)
        self.parser.close()
        self.nodes = self.parser.by_path()

    def node(self, path: str | None) -> _RawHtmlNode | None:
        return self.nodes.get(str(path or ""))


def _markdown_assets(
    collector: _AssetCollector,
    *,
    source: Path,
    row: dict[str, Any],
    block_id: str,
    source_kind: str,
    policy: str,
    semantic_kind: str,
) -> tuple[list[str], bool, bool]:
    fragment = _line_fragment(source, row)
    locator = _source_locator("markdown", row, block_id=block_id)
    asset_ids: list[str] = []
    review_required = False
    text = (fragment or b"").decode("utf-8", errors="replace")
    image_matches = list(_MARKDOWN_IMAGE_RE.finditer(text))
    display_math_matches = (
        []
        if source_kind in {"math_block", "code", "raw_html"}
        else list(_DISPLAY_MATH_RE.finditer(text))
    )
    display_spans = [match.span() for match in display_math_matches]
    inline_math = [
        match.group("math").strip()
        for match in _INLINE_MATH_RE.finditer(text)
        if match.group("math").strip()
        and not any(
            start <= match.start() and match.end() <= end
            for start, end in display_spans
        )
    ]
    mixed_structured = (
        source_kind != "image" and bool(image_matches)
    ) or (
        source_kind not in {"math_block", "code", "raw_html"}
        and bool(display_math_matches or inline_math)
    )
    asset_policy = (
        "translate_structured"
        if mixed_structured and policy == "translate"
        else policy
    )
    if mixed_structured and fragment is not None:
        asset_ids.append(
            collector.materialized(
                kind="raw_fragment",
                media_type="text/markdown",
                policy=asset_policy,
                source_locator={**locator, "fragment_scope": "mixed_block"},
                metadata={
                    "source_kind": source_kind,
                    "fragment_encoding": "utf-8",
                    "placement": "mixed_block_template",
                },
                payload=fragment,
                suffix=".md",
            )
        )
    if source_kind in {"code", "math_block", "table", "raw_html", "image"}:
        if fragment is None:
            kind = {
                "math_block": "equation",
                "raw_html": "raw_fragment",
            }.get(source_kind, source_kind)
            asset_ids.append(
                collector.unavailable(
                    kind=kind,
                    media_type="text/markdown",
                    policy=asset_policy,
                    availability="missing",
                    source_locator=locator,
                    metadata={"source_kind": source_kind},
                    review_required=True,
                )
            )
            return asset_ids, True, False
        kind = {
            "math_block": "equation",
            "raw_html": "raw_fragment",
            "image": "raw_fragment",
        }.get(source_kind, source_kind)
        asset_ids.append(
            collector.materialized(
                kind=kind,
                media_type="text/markdown",
                policy=asset_policy,
                source_locator=locator,
                metadata={
                    "source_kind": source_kind,
                    "fragment_encoding": "utf-8",
                    **({"display": "block"} if source_kind == "math_block" else {}),
                },
                payload=fragment,
                suffix=".md",
            )
        )

    if image_matches:
        for index, match in enumerate(image_matches):
            asset_id, missing = _resource_asset(
                collector,
                source=source,
                target=match.group("target"),
                policy=asset_policy,
                locator={**locator, "image_ordinal": index},
                metadata={
                    "alt_text": match.group("alt"),
                    "title": match.group("title"),
                    "source_syntax": "markdown",
                },
            )
            asset_ids.append(asset_id)
            review_required = review_required or missing

    for index, match in enumerate(display_math_matches):
        expression = match.group("math").strip()
        if not expression:
            continue
        asset_ids.append(
            collector.materialized(
                kind="equation",
                media_type="application/x-tex",
                policy=asset_policy,
                source_locator={**locator, "display_math_ordinal": index},
                metadata={"display": "block", "source_syntax": "markdown"},
                payload=expression.encode("utf-8"),
                suffix=".tex",
            )
        )

    if source_kind not in {"math_block", "code", "raw_html"} and inline_math:
        for index, expression in enumerate(inline_math):
            asset_ids.append(
                collector.materialized(
                    kind="equation",
                    media_type="application/x-tex",
                    policy=asset_policy,
                    source_locator={**locator, "inline_math_ordinal": index},
                    metadata={"display": "inline", "source_syntax": "markdown"},
                    payload=expression.encode("utf-8"),
                    suffix=".tex",
                )
            )
    return list(dict.fromkeys(asset_ids)), review_required, mixed_structured


def _html_assets(
    collector: _AssetCollector,
    *,
    source: Path,
    context: _HtmlContext,
    row: dict[str, Any],
    block_id: str,
    source_kind: str,
    policy: str,
) -> tuple[list[str], bool, bool]:
    locator = _source_locator("html", row, block_id=block_id)
    node = context.node(row.get("html_path"))
    asset_ids: list[str] = []
    review_required = False
    math_nodes = []
    image_nodes = []
    if node is not None and source_kind not in {"image", "code"}:
        math_nodes = (
            [node]
            if node.tag == "math"
            else context.parser.descendants(node.path, tag="math")
        )
        image_nodes = context.parser.descendants(node.path, tag="img")
    mixed_structured = bool(
        (math_nodes and source_kind not in {"equation", "math"})
        or image_nodes
    )
    asset_policy = (
        "translate_structured"
        if mixed_structured and policy == "translate"
        else policy
    )
    if mixed_structured and node is not None:
        asset_ids.append(
            collector.materialized(
                kind="raw_fragment",
                media_type="text/html",
                policy=asset_policy,
                source_locator={
                    **locator,
                    "html_path": node.path,
                    "fragment_scope": "mixed_block",
                },
                metadata={
                    "source_kind": source_kind,
                    "fragment_encoding": "utf-8",
                    "placement": "mixed_block_template",
                },
                payload=context.parser.fragment(node),
                suffix=".html",
            )
        )
    if source_kind in {"table", "code", "image"}:
        if node is None:
            asset_ids.append(
                collector.unavailable(
                    kind=source_kind,
                    media_type="text/html",
                    policy=asset_policy,
                    availability="missing",
                    source_locator=locator,
                    metadata={"source_kind": source_kind},
                    review_required=True,
                )
            )
            return asset_ids, True, False
        asset_ids.append(
            collector.materialized(
                kind="raw_fragment" if source_kind == "image" else source_kind,
                media_type="text/html",
                policy=asset_policy,
                source_locator={**locator, "html_path": node.path},
                metadata={
                    "source_kind": source_kind,
                    "fragment_encoding": "utf-8",
                },
                payload=context.parser.fragment(node),
                suffix=".html",
            )
        )
        if source_kind == "image":
            target = node.attrs.get("src")
            if target:
                asset_id, missing = _resource_asset(
                    collector,
                    source=source,
                    target=target,
                    policy=asset_policy,
                    locator={**locator, "html_attribute": "src"},
                    metadata={
                        "alt_text": node.attrs.get("alt", ""),
                        "title": node.attrs.get("title"),
                        "source_syntax": "html",
                    },
                )
                asset_ids.append(asset_id)
                review_required = review_required or missing
            else:
                review_required = True

    if math_nodes:
        for index, math_node in enumerate(math_nodes):
            asset_ids.append(
                collector.materialized(
                    kind="equation",
                    media_type="application/mathml+xml",
                    policy=asset_policy,
                    source_locator={
                        **locator,
                        "html_path": math_node.path,
                        "math_ordinal": index,
                    },
                    metadata={"source_syntax": "mathml"},
                    payload=context.parser.fragment(math_node),
                    suffix=".mathml",
                )
            )
    for index, image_node in enumerate(image_nodes):
        target = image_node.attrs.get("src")
        if not target:
            review_required = True
            continue
        asset_id, missing = _resource_asset(
            collector,
            source=source,
            target=target,
            policy=asset_policy,
            locator={
                **locator,
                "html_path": image_node.path,
                "image_ordinal": index,
                "html_attribute": "src",
            },
            metadata={
                "alt_text": image_node.attrs.get("alt", ""),
                "title": image_node.attrs.get("title"),
                "source_syntax": "html",
                "placement": "mixed_block_child",
            },
        )
        asset_ids.append(asset_id)
        review_required = review_required or missing
    return list(dict.fromkeys(asset_ids)), review_required, mixed_structured


def _materialize_html_inventory(
    collector: _AssetCollector,
    *,
    source: Path,
    context: _HtmlContext,
) -> None:
    """Preserve rich nodes even when an empty/alt-less node emitted no text block."""

    for node in context.parser.nodes:
        if node.tag not in {"table", "math", "img"}:
            continue
        policy = "translate_structured" if node.tag == "table" else "preserve"
        kind = {
            "table": "table",
            "math": "equation",
            "img": "raw_fragment",
        }[node.tag]
        collector.materialized(
            kind=kind,
            media_type=(
                "application/mathml+xml"
                if node.tag == "math"
                else "text/html"
            ),
            policy=policy,
            source_locator={
                "source_format": "html",
                "html_path": node.path,
                "inventory_scope": "source_rich_node",
            },
            metadata={
                "source_tag": node.tag,
                "placement": "source_inventory",
            },
            payload=context.parser.fragment(node),
            suffix=".mathml" if node.tag == "math" else ".html",
        )
        if node.tag == "img":
            target = node.attrs.get("src")
            if not target:
                continue
            _resource_asset(
                collector,
                source=source,
                target=target,
                policy=policy,
                locator={
                    "source_format": "html",
                    "html_path": node.path,
                    "html_attribute": "src",
                    "inventory_scope": "source_rich_node",
                },
                metadata={
                    "alt_text": node.attrs.get("alt", ""),
                    "title": node.attrs.get("title"),
                    "placement": "source_inventory",
                },
            )


def _epub_rootfile(archive: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(archive.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError) as exc:
        raise CanonicalSourcePackageError("EPUB container is unavailable") from exc
    for element in root.iter():
        if str(element.tag).rsplit("}", 1)[-1] == "rootfile":
            value = element.attrib.get("full-path")
            if value:
                return str(value)
    raise CanonicalSourcePackageError("EPUB rootfile is unavailable")


def _safe_epub_member(base_member: str, target: str) -> str | None:
    value = _target_value(target).replace("\\", "/")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    target_path = unquote(parsed.path)
    if not target_path:
        return None
    base = posixpath.dirname(base_member)
    member = posixpath.normpath(posixpath.join(base, target_path))
    if member == ".." or member.startswith("../") or member.startswith("/"):
        return None
    return member


class _EpubContext:
    def __init__(self, source: Path, collector: _AssetCollector) -> None:
        self.source = source
        self.archive = zipfile.ZipFile(source)
        self.members = set(self.archive.namelist())
        self.rootfile = _epub_rootfile(self.archive)
        self.manifest = self._manifest_items()
        self.resource_assets: dict[tuple[str, str], str] = {}
        self._html: dict[str, _HtmlContext] = {}
        self._used_paths: set[tuple[str, str]] = set()
        self._materialize_embedded_resources(collector)

    def close(self) -> None:
        self.archive.close()

    def _manifest_items(self) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(self.archive.read(self.rootfile))
        except (KeyError, ET.ParseError) as exc:
            raise CanonicalSourcePackageError("EPUB OPF is unavailable") from exc
        base = PurePosixPath(self.rootfile).parent
        result: list[dict[str, Any]] = []
        for element in root.iter():
            if str(element.tag).rsplit("}", 1)[-1] != "item":
                continue
            href = str(element.attrib.get("href") or "")
            if not href:
                continue
            member = posixpath.normpath(str(base.joinpath(unquote(href))))
            if member == ".." or member.startswith("../") or member.startswith("/"):
                continue
            result.append(
                {
                    "id": str(element.attrib.get("id") or ""),
                    "member": member,
                    "media_type": str(
                        element.attrib.get("media-type") or "application/octet-stream"
                    ),
                    "properties": sorted(
                        token
                        for token in str(element.attrib.get("properties") or "").split()
                        if token
                    ),
                }
            )
        return result

    def _materialize_embedded_resources(self, collector: _AssetCollector) -> None:
        ignored = {
            "application/xhtml+xml",
            "text/html",
            "application/x-dtbncx+xml",
            "application/oebps-package+xml",
        }
        for item in self.manifest:
            member = item["member"]
            media_type = item["media_type"]
            if media_type in ignored or member not in self.members:
                continue
            kind = "image" if media_type.startswith("image/") else "embedded_file"
            suffix = PurePosixPath(member).suffix or mimetypes.guess_extension(media_type) or ".bin"
            asset_id = collector.materialized(
                kind=kind,
                media_type=media_type,
                policy="preserve",
                source_locator={
                    "source_format": "epub",
                    "opf_rootfile": self.rootfile,
                    "manifest_id": item["id"],
                    "epub_member": member,
                },
                metadata={
                    "properties": item["properties"],
                    "placement": "opf_manifest_resource",
                },
                payload=self.archive.read(member),
                suffix=suffix,
            )
            self.resource_assets[(member, "preserve")] = asset_id

    def resource_asset(
        self,
        collector: _AssetCollector,
        *,
        base_member: str,
        target: str,
        policy: str,
        locator: dict[str, Any],
    ) -> tuple[str, bool]:
        member = _safe_epub_member(base_member, target)
        if member is None or member not in self.members:
            return (
                collector.unavailable(
                    kind="image",
                    media_type=mimetypes.guess_type(target)[0]
                    or "application/octet-stream",
                    policy=policy,
                    availability="missing",
                    source_locator={**locator, "resource_target": target},
                    metadata={"source_syntax": "epub"},
                    review_required=True,
                ),
                True,
            )
        prior = self.resource_assets.get((member, policy))
        if prior is not None:
            return prior, False
        media_type = next(
            (
                item["media_type"]
                for item in self.manifest
                if item["member"] == member
            ),
            mimetypes.guess_type(member)[0] or "application/octet-stream",
        )
        asset_id = collector.materialized(
            kind="image" if media_type.startswith("image/") else "embedded_file",
            media_type=media_type,
            policy=policy,
            source_locator={
                **locator,
                "resource_target": target,
                "epub_member": member,
            },
            metadata={"source_syntax": "epub"},
            payload=self.archive.read(member),
            suffix=PurePosixPath(member).suffix or ".bin",
        )
        self.resource_assets[(member, policy)] = asset_id
        return asset_id, False

    def _html_context(self, member: str) -> _HtmlContext | None:
        if member not in self.members:
            return None
        if member not in self._html:
            payload = self.archive.read(member)
            temporary = Path(tempfile.mkdtemp()) / "member.xhtml"
            try:
                temporary.write_bytes(payload)
                self._html[member] = _HtmlContext(temporary)
            finally:
                try:
                    temporary.unlink()
                    temporary.parent.rmdir()
                except OSError:
                    pass
        return self._html[member]

    def fragment(
        self,
        *,
        member: str,
        source_kind: str,
        block_text: str,
        resource_targets: list[str],
    ) -> tuple[bytes | None, str | None, bool]:
        context = self._html_context(member)
        if context is None:
            return None, None, True
        tag = {
            "table": "table",
            "code": "pre",
            "image": "img",
            "equation": "math",
            "math": "math",
        }.get(source_kind)
        if tag is None:
            return None, None, False
        candidates = [
            node
            for node in context.parser.nodes
            if node.tag == tag and (member, node.path) not in self._used_paths
        ]
        if not candidates:
            return None, None, True
        if tag == "img" and resource_targets:
            wanted = {
                _safe_epub_member(member, target)
                for target in resource_targets
            }
            matching = [
                node
                for node in candidates
                if _safe_epub_member(member, node.attrs.get("src", "")) in wanted
            ]
            if len(matching) == 1:
                chosen = matching[0]
                self._used_paths.add((member, chosen.path))
                return context.parser.fragment(chosen), chosen.path, False
        target_text = " ".join(str(block_text or "").casefold().split())
        scored: list[tuple[float, int, _RawHtmlNode]] = []
        target_tokens = set(re.findall(r"\w+", target_text))
        for index, node in enumerate(candidates):
            fragment = context.parser.fragment(node)
            candidate_text = " ".join(_plain_html_text(fragment).casefold().split())
            candidate_tokens = set(re.findall(r"\w+", candidate_text))
            if target_text and candidate_text == target_text:
                score = 1.0
            elif target_tokens:
                score = len(target_tokens & candidate_tokens) / len(target_tokens)
            else:
                score = 0.0
            scored.append((score, -index, node))
        score, _neg_index, chosen = max(scored, key=lambda item: (item[0], item[1]))
        if source_kind != "image" and score < 0.5:
            return None, None, True
        self._used_paths.add((member, chosen.path))
        return context.parser.fragment(chosen), chosen.path, False

    def mixed_fragment(
        self,
        *,
        member: str,
        block_text: str,
        resource_targets: list[str],
    ) -> tuple[bytes | None, str | None, bool]:
        context = self._html_context(member)
        if context is None:
            return None, None, True
        candidates = [
            node
            for node in context.parser.nodes
            if node.tag
            in {"p", "li", "blockquote", "figcaption", "dt", "dd", "address"}
            and (member, node.path) not in self._used_paths
        ]
        if not candidates:
            return None, None, True

        wanted = {
            resolved
            for target in resource_targets
            if (resolved := _safe_epub_member(member, target)) is not None
        }
        if wanted:
            resource_matches = []
            for node in candidates:
                descendants = context.parser.descendants(node.path, tag="img")
                members = {
                    resolved
                    for child in descendants
                    if (
                        resolved := _safe_epub_member(
                            member,
                            child.attrs.get("src", ""),
                        )
                    )
                    is not None
                }
                if members & wanted:
                    resource_matches.append(node)
            if resource_matches:
                candidates = resource_matches

        target_text = " ".join(str(block_text or "").casefold().split())
        target_tokens = set(re.findall(r"\w+", target_text))
        scored: list[tuple[float, int, _RawHtmlNode]] = []
        for index, node in enumerate(candidates):
            fragment = context.parser.fragment(node)
            candidate_text = " ".join(_plain_html_text(fragment).casefold().split())
            candidate_tokens = set(re.findall(r"\w+", candidate_text))
            if target_text and candidate_text == target_text:
                score = 1.0
            elif target_tokens:
                score = len(target_tokens & candidate_tokens) / len(target_tokens)
            else:
                score = 0.0
            scored.append((score, -index, node))
        score, _negative_index, chosen = max(
            scored,
            key=lambda item: (item[0], item[1]),
        )
        if score < 0.5 and not wanted:
            return None, None, True
        self._used_paths.add((member, chosen.path))
        return context.parser.fragment(chosen), chosen.path, score < 0.5


def _epub_assets(
    collector: _AssetCollector,
    *,
    context: _EpubContext,
    row: dict[str, Any],
    block: dict[str, Any],
    block_id: str,
    source_kind: str,
    policy: str,
) -> tuple[list[str], bool, bool]:
    locator = _source_locator("epub", row, block_id=block_id)
    member = str(row.get("epub_file") or "")
    resource_targets = [
        str(target)
        for target in row.get("resource_targets") or []
        if isinstance(target, str) and target
    ]
    math_fragments = [
        str(value)
        for value in row.get("math_fragments") or []
        if isinstance(value, str) and value
    ]
    asset_ids: list[str] = []
    review_required = False
    mixed_structured = bool(
        source_kind not in {"image", "equation", "math"} and (resource_targets or math_fragments)
    )
    asset_policy = (
        "translate_structured"
        if mixed_structured and policy == "translate"
        else policy
    )
    if mixed_structured:
        fragment, xhtml_path, uncertain = context.mixed_fragment(
            member=member,
            block_text=str(block.get("source_text") or ""),
            resource_targets=resource_targets,
        )
        if fragment is None:
            asset_ids.append(
                collector.unavailable(
                    kind="raw_fragment",
                    media_type="application/xhtml+xml",
                    policy=asset_policy,
                    availability="missing",
                    source_locator={**locator, "fragment_scope": "mixed_block"},
                    metadata={"placement": "mixed_block_template"},
                    review_required=True,
                )
            )
            review_required = True
        else:
            asset_ids.append(
                collector.materialized(
                    kind="raw_fragment",
                    media_type="application/xhtml+xml",
                    policy=asset_policy,
                    source_locator={
                        **locator,
                        "xhtml_path": xhtml_path,
                        "fragment_scope": "mixed_block",
                    },
                    metadata={"placement": "mixed_block_template"},
                    payload=fragment,
                    suffix=".xhtml",
                    review_required=uncertain,
                )
            )
            review_required = review_required or uncertain
    if source_kind in {"table", "code", "image", "equation", "math"}:
        fragment, xhtml_path, uncertain = context.fragment(
            member=member,
            source_kind=source_kind,
            block_text=str(block.get("source_text") or ""),
            resource_targets=resource_targets,
        )
        kind = {
            "math": "equation",
            "equation": "equation",
            "image": "raw_fragment",
        }.get(source_kind, source_kind)
        if fragment is None:
            asset_ids.append(
                collector.unavailable(
                    kind=kind,
                    media_type="application/xhtml+xml",
                    policy=asset_policy,
                    availability="missing",
                    source_locator=locator,
                    metadata={
                        "source_kind": source_kind,
                        "fragment_scope": "xhtml_node",
                    },
                    review_required=True,
                )
            )
            review_required = True
        else:
            asset_ids.append(
                collector.materialized(
                    kind=kind,
                    media_type="application/xhtml+xml",
                    policy=asset_policy,
                    source_locator={**locator, "xhtml_path": xhtml_path},
                    metadata={
                        "source_kind": source_kind,
                        "fragment_scope": "xhtml_node",
                    },
                    payload=fragment,
                    suffix=".xhtml",
                    review_required=uncertain,
                )
            )
            review_required = review_required or uncertain

    for index, target in enumerate(resource_targets):
        asset_id, missing = context.resource_asset(
            collector,
            base_member=member,
            target=target,
            policy=asset_policy,
            locator={**locator, "resource_ordinal": index},
        )
        asset_ids.append(asset_id)
        review_required = review_required or missing

    for index, expression in enumerate(math_fragments):
        asset_ids.append(
            collector.materialized(
                kind="equation",
                media_type="application/x-tex",
                policy=asset_policy,
                source_locator={**locator, "math_ordinal": index},
                metadata={"source_syntax": "pandoc_math"},
                payload=expression.encode("utf-8"),
                suffix=".tex",
            )
        )
    return list(dict.fromkeys(asset_ids)), review_required, mixed_structured


class _PdfContext:
    def __init__(
        self,
        source: Path,
        collector: _AssetCollector,
    ) -> None:
        try:
            import pymupdf
        except ImportError as exc:
            raise CanonicalSourcePackageError(
                "PyMuPDF is unavailable for PDF asset preservation"
            ) from exc
        self.pymupdf = pymupdf
        self.document = pymupdf.open(source)
        self.source_pdf_asset_id = collector.materialized(
            kind="embedded_file",
            media_type="application/pdf",
            policy="preserve",
            source_locator={
                "source_format": "pdf",
                "source_scope": "complete_document",
            },
            metadata={"placement": "canonical_source_pdf"},
            payload=source.read_bytes(),
            suffix=".pdf",
        )

    def close(self) -> None:
        self.document.close()

    def crop(self, *, page_number: int, bbox_pdf: list[Any]) -> bytes:
        if page_number < 1 or page_number > len(self.document):
            raise CanonicalSourcePackageError("PDF asset page is outside the source")
        if not isinstance(bbox_pdf, list) or len(bbox_pdf) != 4:
            raise CanonicalSourcePackageError("PDF asset bounding box is invalid")
        try:
            x0, y0, x1, y1 = [float(value) for value in bbox_pdf]
        except (TypeError, ValueError) as exc:
            raise CanonicalSourcePackageError(
                "PDF asset bounding box is invalid"
            ) from exc
        page = self.document[page_number - 1]
        page_height = float(page.rect.height)
        clip = self.pymupdf.Rect(x0, page_height - y1, x1, page_height - y0)
        clip &= page.rect
        if clip.is_empty or clip.width <= 0 or clip.height <= 0:
            raise CanonicalSourcePackageError("PDF asset crop has no positive area")
        pixmap = page.get_pixmap(
            matrix=self.pymupdf.Matrix(2.0, 2.0),
            clip=clip,
            colorspace=self.pymupdf.csRGB,
            alpha=False,
        )
        return pixmap.tobytes("png")


def _pdf_assets(
    collector: _AssetCollector,
    *,
    context: _PdfContext,
    row: dict[str, Any],
    block_id: str,
    source_kind: str,
    policy: str,
) -> tuple[list[str], bool, bool]:
    if source_kind not in {"table", "image", "equation"}:
        return [], False, False
    locator = _source_locator("pdf", row, block_id=block_id)
    review_required = bool(row.get("review_required"))
    kind = source_kind
    metadata = {
        "source_pdf_asset_id": context.source_pdf_asset_id,
        "source_kind": source_kind,
        "placement": "pdf_bbox_clip",
    }
    formula_detection = row.get("formula_detection")
    if isinstance(formula_detection, dict):
        metadata["formula_detection"] = copy.deepcopy(formula_detection)
    asset_ids: list[str] = []
    try:
        crop = context.crop(
            page_number=int(row.get("page_number") or 0),
            bbox_pdf=row.get("bbox_pdf"),
        )
    except (CanonicalSourcePackageError, TypeError, ValueError):
        asset_ids.append(
            collector.unavailable(
                kind=kind,
                media_type="image/png",
                policy=policy,
                availability="source_reference",
                source_locator=locator,
                metadata={**metadata, "reason": "pdf_bbox_crop_failed"},
                review_required=True,
            )
        )
        review_required = True
    else:
        asset_ids.append(
            collector.materialized(
                kind=kind,
                media_type="image/png",
                policy=policy,
                source_locator=locator,
                metadata=metadata,
                payload=crop,
                suffix=".png",
                review_required=review_required,
            )
        )

    rich_payload = row.get("rich_payload")
    if source_kind == "table" and isinstance(rich_payload, dict):
        payload = (
            json.dumps(
                rich_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        asset_ids.append(
            collector.materialized(
                kind="raw_fragment",
                media_type="application/json",
                policy=policy,
                source_locator={**locator, "fragment_scope": "odl_table_structure"},
                metadata={
                    "source_pdf_asset_id": context.source_pdf_asset_id,
                    "placement": "odl_table_structure",
                },
                payload=payload,
                suffix=".json",
                review_required=review_required,
            )
        )
    return list(dict.fromkeys(asset_ids)), review_required, False


def materialize_source_package(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    output_dir: str | Path,
) -> SourcePackageWriteResult:
    package_root = Path(output_dir)
    package_root.mkdir(parents=True, exist_ok=True)
    asset_manifest_path = package_root / "asset_manifest.json"
    admitted_projection_path = package_root / "admitted_projection_v1.json"
    asset_manifest_path.unlink(missing_ok=True)
    admitted_projection_path.unlink(missing_ok=True)
    source = _verified_source(structure_manifest)
    source_format = str((structure_manifest.get("source") or {}).get("format") or "")
    if source_format not in {"txt", "markdown", "html", "epub", "pdf"}:
        raise CanonicalSourcePackageError(
            f"unsupported source format for P1 materialization: {source_format}"
        )

    blocks, chapter_by_block = _flatten_document(document)
    source_rows = _source_rows(structure_manifest)
    unit_rows = _unit_rows(structure_manifest)
    policy_rows = _block_policy_rows(structure_manifest)
    if [str(block.get("block_id") or "") for block in blocks] != list(source_rows):
        raise CanonicalSourcePackageError(
            "document blocks and structure source_map differ before materialization"
        )

    collector = _AssetCollector(package_root)
    html_context = _HtmlContext(source) if source_format == "html" else None
    if html_context is not None:
        _materialize_html_inventory(
            collector,
            source=source,
            context=html_context,
        )
    epub_context = (
        _EpubContext(source, collector) if source_format == "epub" else None
    )
    pdf_context = (
        _PdfContext(source, collector) if source_format == "pdf" else None
    )
    bindings: list[dict[str, Any]] = []
    try:
        for block in blocks:
            block_id = str(block.get("block_id") or "")
            row = source_rows[block_id]
            chapter_id = chapter_by_block[block_id]
            unit = unit_rows.get(chapter_id)
            if unit is None:
                raise CanonicalSourcePackageError(
                    f"no structure unit owns chapter: {chapter_id}"
                )
            source_kind = str(
                row.get("source_block_kind")
                or block.get("block_type")
                or "unknown"
            )
            policy = policy_rows.get(block_id) or _default_policy(source_kind, unit)
            semantic_kind, semantic_subtype, render_role = _semantic_shape(source_kind)
            review_required = bool(
                unit.get("review_required")
                or policy == "review"
                or row.get("review_required")
            )
            asset_ids: list[str] = []
            mixed_structured = False

            if source_format == "markdown":
                asset_ids, asset_review, mixed_structured = _markdown_assets(
                    collector,
                    source=source,
                    row=row,
                    block_id=block_id,
                    source_kind=source_kind,
                    policy=policy,
                    semantic_kind=semantic_kind,
                )
                review_required = review_required or asset_review
            elif source_format == "html" and html_context is not None:
                asset_ids, asset_review, mixed_structured = _html_assets(
                    collector,
                    source=source,
                    context=html_context,
                    row=row,
                    block_id=block_id,
                    source_kind=source_kind,
                    policy=policy,
                )
                review_required = review_required or asset_review
            elif source_format == "epub" and epub_context is not None:
                asset_ids, asset_review, mixed_structured = _epub_assets(
                    collector,
                    context=epub_context,
                    row=row,
                    block=block,
                    block_id=block_id,
                    source_kind=source_kind,
                    policy=policy,
                )
                review_required = review_required or asset_review
            elif source_format == "pdf" and pdf_context is not None:
                asset_ids, asset_review, mixed_structured = _pdf_assets(
                    collector,
                    context=pdf_context,
                    row=row,
                    block_id=block_id,
                    source_kind=source_kind,
                    policy=policy,
                )
                review_required = review_required or asset_review

            if mixed_structured and policy == "translate":
                policy = "translate_structured"
                semantic_kind = "text"
                semantic_subtype = "mixed_structured_content"
                render_role = "text"
                for asset_id in asset_ids:
                    asset = next(
                        item for item in collector.assets if item["asset_id"] == asset_id
                    )
                    if asset["translation_policy"] != policy:
                        raise CanonicalSourcePackageError(
                            "mixed structured assets must be created with structured policy"
                        )

            if semantic_kind in _RICH_KINDS and not asset_ids:
                missing_kind = {
                    "equation": "equation",
                    "image": "image",
                    "table": "table",
                    "code": "code",
                }[semantic_kind]
                asset_ids.append(
                    collector.unavailable(
                        kind=missing_kind,
                        media_type="application/octet-stream",
                        policy=policy,
                        availability="missing",
                        source_locator=_source_locator(
                            source_format,
                            row,
                            block_id=block_id,
                        ),
                        metadata={"reason": "rich_block_not_recoverable"},
                        review_required=True,
                    )
                )
                review_required = True

            bindings.append(
                {
                    "block_id": block_id,
                    "source_kind": source_kind,
                    "semantic_kind": semantic_kind,
                    "semantic_subtype": semantic_subtype,
                    "translation_policy": policy,
                    "asset_ids": asset_ids,
                    "render_role": render_role,
                    "review_required": review_required,
                }
            )
    finally:
        if epub_context is not None:
            epub_context.close()
        if pdf_context is not None:
            pdf_context.close()

    expected_source_sha256 = str(
        (structure_manifest.get("source") or {}).get("sha256") or ""
    )
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_source_sha256:
        raise CanonicalSourcePackageError(
            "source bytes changed while materializing the source package"
        )

    manifest = seal_asset_manifest(
        document,
        structure_manifest,
        assets=collector.assets,
        block_bindings=bindings,
    )
    report = validate_canonical_source_package(
        document,
        structure_manifest,
        manifest,
        package_root=package_root,
    )
    projection = build_admitted_projection(
        document,
        structure_manifest,
        manifest,
    )
    try:
        _atomic_json_write(asset_manifest_path, manifest)
        _atomic_json_write(admitted_projection_path, projection)
    except Exception:
        asset_manifest_path.unlink(missing_ok=True)
        admitted_projection_path.unlink(missing_ok=True)
        raise
    return SourcePackageWriteResult(
        asset_manifest_path=asset_manifest_path,
        admitted_projection_path=admitted_projection_path,
        validation_report=report,
    )


__all__ = [
    "SourcePackageWriteResult",
    "materialize_source_package",
]
