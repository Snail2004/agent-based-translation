from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from pipeline.ingest.document_contract import runtime_block_type
from pipeline.ingest.normalization_adapters import AdapterUnavailableError, run_pandoc
from pipeline.ingest.normalization_ir import TOKEN_RE, normalize_text
from pipeline.ingest.source_package_materializer import materialize_source_package


NORMALIZER_VERSION = "markdown_hybrid_normalizer_v2"
MANIFEST_SCHEMA_VERSION = "markdown_structure_manifest_v1"
DOCUMENT_SCHEMA_VERSION = "1.5.0"

_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_DECORATED_DISPLAY_MATH_CLOSER_RE = re.compile(
    r"^\$\$[\s\]\)}*_,.;:!?]+$"
)
_TABLE_DELIMITER_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_THEMATIC_BREAK_RE = re.compile(r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")
_LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_FOOTNOTE_RE = re.compile(r"^\s*\[\^[^]]+\]:")
_DIRECTIVE_RE = re.compile(r"^\s*:[A-Za-z][A-Za-z0-9_-]*:")
_HTML_BLOCK_RE = re.compile(r"^\s*</?[A-Za-z][^>]*>")
_HEADING_ATTR_RE = re.compile(r"\s*\{[^{}]*\}\s*$")
_ROMAN_RE = re.compile(r"^[ivxlcdm]+[.:-]?$", re.IGNORECASE)
_ARABIC_RE = re.compile(r"^\d+[.:-]?$", re.IGNORECASE)
_PREFIXED_CONTENT_RE = re.compile(
    r"^(?:chapter|chapitre|capitulo|capitolo|kapitel|chuong|stave|letter)\s+[\wivxlcdm]+",
    re.IGNORECASE,
)
_FRONT_TITLE_RE = re.compile(
    r"^(?:contents|table\s+of\s+contents|illustrations|list\s+of\s+illustrations|"
    r"preface|introduction|dedication|title\s*page|imprint|cover)$",
    re.IGNORECASE,
)
_BACK_TITLE_RE = re.compile(
    r"^(?:afterword|endnotes|notes|colophon|bibliography|references|index|"
    r"appendix|appendices|glossary|license)$",
    re.IGNORECASE,
)
_LICENSE_RE = re.compile(r"(?:project\s+gutenberg.*license|full\s+license)", re.IGNORECASE)
_INLINE_IMAGE_RE = re.compile(r"!\[([^]]*)\]\([^)]*\)")
_INLINE_LINK_RE = re.compile(r"\[([^]]+)\]\([^)]*\)")
_RAW_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MarkdownBlock:
    ordinal: int
    kind: str
    source_text: str
    heading_level: int | None
    heading_title: str | None
    line_start: int
    line_end: int
    anchor: str | None


@dataclass(frozen=True)
class MarkdownUnit:
    unit_id: str
    order_index: int
    title: str
    start_block: int
    end_block: int
    role: str
    translation_policy: str
    confidence: float
    evidence: tuple[str, ...]
    review_required: bool


@dataclass(frozen=True)
class MarkdownNormalizationResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]


def _slug(value: str, *, fallback: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value.casefold()).strip("_")
    return slug or fallback


def _ascii_fold(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    return folded.encode("ascii", "ignore").decode("ascii").casefold().strip()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_yaml_front_matter(lines: Sequence[str]) -> tuple[int, dict[str, str], list[str]]:
    if not lines or lines[0].strip() != "---":
        return 0, {}, []
    closing = next(
        (index for index in range(1, min(len(lines), 500)) if lines[index].strip() in {"---", "..."}),
        None,
    )
    if closing is None:
        return 0, {}, ["unclosed_yaml_front_matter"]
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().casefold().replace("-", "_")
        value = value.strip().strip("'\"")
        if key in {"title", "author", "language", "lang"} and value:
            metadata[key] = value
    return closing + 1, metadata, []


def _heading_title(raw: str) -> tuple[str, str | None]:
    value = raw.strip()
    anchor: str | None = None
    attr_match = _HEADING_ATTR_RE.search(value)
    if attr_match:
        attributes = attr_match.group(0)
        id_match = re.search(r"#([A-Za-z][A-Za-z0-9_.:-]*)", attributes)
        anchor = id_match.group(1) if id_match else None
        value = value[: attr_match.start()].rstrip()
    value = re.sub(r"\s+#+\s*$", "", value).strip()
    return value, anchor


def _classify_group(lines: Sequence[str]) -> str:
    nonempty = [line for line in lines if line.strip()]
    if not nonempty:
        return "paragraph"
    first = nonempty[0]
    if len(nonempty) >= 2 and _TABLE_DELIMITER_RE.match(nonempty[1]):
        return "table"
    if re.match(r"^\s*!\[[^]]*\]\([^)]*\)\s*$", first):
        return "image"
    if _FOOTNOTE_RE.match(first):
        return "footnote"
    if _DIRECTIVE_RE.match(first):
        return "directive"
    if _LIST_RE.match(first):
        return "list"
    if first.lstrip().startswith(">"):
        return "block_quote"
    if _HTML_BLOCK_RE.match(first):
        return "raw_html"
    if _THEMATIC_BREAK_RE.match(first):
        return "separator"
    if first.startswith("    ") or first.startswith("\t"):
        return "code"
    return "paragraph"


def _scan_markdown(source: Path) -> tuple[list[MarkdownBlock], dict[str, str], list[str]]:
    raw = source.read_text(encoding="utf-8-sig", errors="replace")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    metadata_end, metadata, warnings = _parse_yaml_front_matter(lines)
    blocks: list[MarkdownBlock] = []
    buffer: list[str] = []
    buffer_start = metadata_end + 1

    def emit(
        text_lines: Sequence[str],
        start_index: int,
        end_index: int,
        *,
        kind: str | None = None,
        heading_level: int | None = None,
        heading_title: str | None = None,
        anchor: str | None = None,
    ) -> None:
        source_text = "\n".join(text_lines).strip("\n")
        if not source_text.strip():
            return
        blocks.append(
            MarkdownBlock(
                ordinal=len(blocks),
                kind=kind or _classify_group(text_lines),
                source_text=source_text,
                heading_level=heading_level,
                heading_title=heading_title,
                line_start=start_index + 1,
                line_end=end_index + 1,
                anchor=anchor,
            )
        )

    def flush(end_index: int) -> None:
        nonlocal buffer, buffer_start
        if buffer:
            emit(buffer, buffer_start, end_index)
        buffer = []
        buffer_start = end_index + 1

    index = metadata_end
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        fence = _FENCE_RE.match(line)
        if fence:
            flush(index - 1)
            marker = fence.group(1)
            closing_fence = re.compile(
                rf"{re.escape(marker[0])}{{{len(marker)},}}\s*"
            )
            fence_lines = [line]
            end = index + 1
            while end < len(lines):
                fence_lines.append(lines[end])
                candidate = lines[end].strip()
                if closing_fence.fullmatch(candidate):
                    break
                end += 1
            if end >= len(lines):
                end = len(lines) - 1
                warnings.append(f"unclosed_code_fence:line_{index + 1}")
            emit(fence_lines, index, end, kind="code")
            index = end + 1
            buffer_start = index
            continue

        if stripped.startswith("$$") and not _DECORATED_DISPLAY_MATH_CLOSER_RE.fullmatch(
            stripped
        ):
            flush(index - 1)
            math_lines = [line]
            opening_offset = line.find("$$")
            closed = line.find("$$", opening_offset + 2) >= 0
            end = index
            while not closed and end + 1 < len(lines):
                end += 1
                math_lines.append(lines[end])
                closed = "$$" in lines[end]
            if not closed:
                end = len(lines) - 1
                warnings.append(f"unclosed_math_fence:line_{index + 1}")
            emit(math_lines, index, end, kind="math_block")
            index = end + 1
            buffer_start = index
            continue

        atx = _ATX_HEADING_RE.match(line)
        if atx:
            flush(index - 1)
            title, anchor = _heading_title(atx.group(2))
            emit(
                [line],
                index,
                index,
                kind="heading",
                heading_level=len(atx.group(1)),
                heading_title=title,
                anchor=anchor,
            )
            index += 1
            buffer_start = index
            continue

        if (
            stripped
            and index + 1 < len(lines)
            and _SETEXT_RE.match(lines[index + 1])
            and not _THEMATIC_BREAK_RE.match(line)
        ):
            flush(index - 1)
            underline = lines[index + 1]
            level = 1 if underline.lstrip().startswith("=") else 2
            title, anchor = _heading_title(line)
            emit(
                [line, underline],
                index,
                index + 1,
                kind="heading",
                heading_level=level,
                heading_title=title,
                anchor=anchor,
            )
            index += 2
            buffer_start = index
            continue

        if not stripped:
            flush(index - 1)
            index += 1
            buffer_start = index
            continue

        if not buffer:
            buffer_start = index
        buffer.append(line)
        index += 1

    flush(len(lines) - 1)
    return blocks, metadata, warnings


def _heading_role(title: str) -> str | None:
    value = normalize_text(title)
    if _LICENSE_RE.search(value) or _BACK_TITLE_RE.fullmatch(value):
        return "back_matter"
    if _FRONT_TITLE_RE.fullmatch(value):
        return "front_matter"
    return None


def _heading_family(title: str) -> str | None:
    value = _ascii_fold(title)
    if _ROMAN_RE.fullmatch(value):
        return "roman"
    if _ARABIC_RE.fullmatch(value):
        return "arabic"
    if _PREFIXED_CONTENT_RE.match(value):
        return "prefixed"
    return None


def _select_content_headings(blocks: Sequence[MarkdownBlock]) -> list[MarkdownBlock]:
    headings = [block for block in blocks if block.kind == "heading" and block.heading_title]
    ordinary = [block for block in headings if _heading_role(block.heading_title or "") is None]
    explicit: dict[tuple[int, str], list[MarkdownBlock]] = {}
    for block in ordinary:
        family = _heading_family(block.heading_title or "")
        if family and block.heading_level is not None:
            explicit.setdefault((block.heading_level, family), []).append(block)
    repeated = [
        (len(items), -level, family, items)
        for (level, family), items in explicit.items()
        if len(items) >= 2
    ]
    if repeated:
        return list(max(repeated)[3])

    prefixed_single = [
        block for block in ordinary if _heading_family(block.heading_title or "") == "prefixed"
    ]
    if len(prefixed_single) == 1:
        return prefixed_single

    top_level = [block for block in ordinary if block.heading_level == 1]
    if top_level:
        return top_level
    return []


def _materialize_units(blocks: Sequence[MarkdownBlock], warnings: Sequence[str]) -> tuple[MarkdownUnit, ...]:
    content = _select_content_headings(blocks)
    if not content:
        return (
            MarkdownUnit(
                unit_id="u0001_document",
                order_index=0,
                title="Document",
                start_block=0,
                end_block=len(blocks),
                role="unknown",
                translation_policy="review",
                confidence=0.0,
                evidence=("no_reliable_content_boundary",),
                review_required=True,
            ),
        )

    boundaries: list[tuple[int, str, str, float, tuple[str, ...]]] = []
    first_content = content[0].ordinal
    if first_content > 0:
        boundaries.append((0, "Front matter", "front_matter", 0.92, ("content_prefix",)))
    for block in content:
        family = _heading_family(block.heading_title or "")
        evidence = (
            f"markdown_heading_level:{block.heading_level}",
            f"heading_family:{family or 'top_level'}",
        )
        boundaries.append((block.ordinal, block.heading_title or "Unit", "content_unit", 0.90, evidence))

    back_headings = [
        block
        for block in blocks
        if block.kind == "heading"
        and block.ordinal > first_content
        and block.heading_title
        and _heading_role(block.heading_title) == "back_matter"
    ]
    if back_headings:
        back = min(back_headings, key=lambda block: block.ordinal)
        boundaries = [item for item in boundaries if item[0] < back.ordinal]
        boundaries.append(
            (back.ordinal, back.heading_title or "Back matter", "back_matter", 0.90, ("standardized_back_heading",))
        )

    boundaries.sort(key=lambda item: item[0])
    structural_review = bool(warnings)
    units: list[MarkdownUnit] = []
    used_ids: set[str] = set()
    for order, boundary in enumerate(boundaries):
        start, title, role, confidence, evidence = boundary
        end = boundaries[order + 1][0] if order + 1 < len(boundaries) else len(blocks)
        if start >= end:
            continue
        base = _slug(title, fallback=f"unit_{order + 1}")
        unit_id = f"u{order + 1:04d}_{base}"
        suffix = 2
        while unit_id in used_ids:
            unit_id = f"u{order + 1:04d}_{base}_{suffix}"
            suffix += 1
        used_ids.add(unit_id)
        units.append(
            MarkdownUnit(
                unit_id=unit_id,
                order_index=len(units),
                title=title,
                start_block=start,
                end_block=end,
                role=role,
                translation_policy="translate" if role == "content_unit" else "preserve",
                confidence=confidence,
                evidence=evidence,
                review_required=structural_review and role == "content_unit",
            )
        )
    return tuple(units)


def _validate_exact_cover(units: Sequence[MarkdownUnit], block_count: int) -> dict[str, Any]:
    owners: list[int | None] = [None] * block_count
    for unit in units:
        if unit.start_block < 0 or unit.end_block > block_count or unit.start_block >= unit.end_block:
            raise ValueError(f"Invalid Markdown unit range: {unit.unit_id}")
        for index in range(unit.start_block, unit.end_block):
            if owners[index] is not None:
                raise ValueError(f"Markdown unit overlap at block {index}")
            owners[index] = unit.order_index
    missing = [index for index, owner in enumerate(owners) if owner is None]
    if missing:
        raise ValueError(f"Markdown unit exact-cover missing blocks: {missing[:10]}")
    return {
        "expected_blocks": block_count,
        "covered_blocks": block_count,
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }


def _counter_coverage(left: Sequence[str], right: Sequence[str]) -> float:
    if not left:
        return 1.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    matched = sum(min(count, right_counts[token]) for token, count in left_counts.items())
    return matched / sum(left_counts.values())


def _semantic_source_text(block: MarkdownBlock) -> str:
    """Project native Markdown into the visible content Pandoc is expected to retain."""
    text = block.source_text
    if block.kind == "heading":
        return block.heading_title or ""
    if block.kind == "directive":
        return ""
    if block.kind == "code":
        lines = text.splitlines()
        if lines and _FENCE_RE.match(lines[0]):
            lines = lines[1:]
            if lines and re.fullmatch(r"\s*(?:`{3,}|~{3,})\s*", lines[-1]):
                lines = lines[:-1]
        else:
            lines = [line[4:] if line.startswith("    ") else line.lstrip("\t") for line in lines]
        return "\n".join(lines)
    if block.kind == "math_block":
        stripped = text.strip()
        if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) >= 4:
            return stripped[2:-2].strip()
        return stripped
    if block.kind == "list":
        text = "\n".join(_LIST_RE.sub("", line, count=1) for line in text.splitlines())
    elif block.kind == "block_quote":
        text = "\n".join(re.sub(r"^\s*>\s?", "", line) for line in text.splitlines())
    elif block.kind == "footnote":
        text = _FOOTNOTE_RE.sub("", text, count=1)

    text = _INLINE_IMAGE_RE.sub(r"\1", text)
    text = _INLINE_LINK_RE.sub(r"\1", text)
    text = _HEADING_ATTR_RE.sub("", text)
    text = _RAW_HTML_TAG_RE.sub(" ", text)
    return text


def _runtime_text(block: MarkdownBlock) -> str:
    """Return text suitable for a model while retaining source syntax separately."""
    if block.kind == "heading":
        return block.heading_title or ""
    if block.kind == "block_quote":
        return "\n".join(
            re.sub(r"^\s*>\s?", "", line) for line in block.source_text.splitlines()
        )
    if block.kind == "footnote":
        return _FOOTNOTE_RE.sub("", block.source_text, count=1).lstrip()
    return block.source_text


def _cross_check(
    source: Path,
    blocks: Sequence[MarkdownBlock],
    pandoc_executable: str | None,
) -> dict[str, Any]:
    if pandoc_executable is None:
        return {"status": "skipped", "review_required": False}
    try:
        pandoc = run_pandoc(source, executable=pandoc_executable)
    except (AdapterUnavailableError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "review_required": True, "reason": str(exc)}
    native_tokens = [
        token.casefold()
        for block in blocks
        for token in TOKEN_RE.findall(_semantic_source_text(block))
    ]
    pandoc_tokens = [
        token.casefold()
        for block in pandoc.blocks
        for token in TOKEN_RE.findall(block.text)
    ]
    coverage = _counter_coverage(native_tokens, pandoc_tokens)
    semantic_kinds = sorted({block.kind for block in pandoc.blocks})
    return {
        "status": "ok",
        "adapter_version": pandoc.adapter_version,
        "native_block_count": len(blocks),
        "pandoc_block_count": len(pandoc.blocks),
        "native_token_count": len(native_tokens),
        "pandoc_token_count": len(pandoc_tokens),
        "native_covered_by_pandoc": round(coverage, 6),
        "pandoc_covered_by_native": round(_counter_coverage(pandoc_tokens, native_tokens), 6),
        "pandoc_semantic_kinds": semantic_kinds,
        "review_required": bool(native_tokens and coverage < 0.98),
        "threshold": 0.98,
    }


def _block_policy(kind: str) -> str:
    if kind in {"code", "math_block", "image", "directive", "separator", "raw_html"}:
        return "preserve"
    if kind == "table":
        return "translate_structured"
    if kind in {"list", "footnote"}:
        return "translate_structured"
    return "translate"


def normalize_markdown(
    source_path: str | Path,
    *,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    pandoc_executable: str | None = "pandoc",
) -> MarkdownNormalizationResult:
    source = Path(source_path).resolve()
    if source.suffix.casefold() not in {".md", ".markdown"}:
        raise ValueError("Markdown normalizer requires a .md or .markdown source")
    if not source.is_file():
        raise FileNotFoundError(source)

    blocks, metadata, warnings = _scan_markdown(source)
    if not blocks:
        raise ValueError("Markdown source contains no visible canonical text blocks")
    units = _materialize_units(blocks, warnings)
    exact_cover = _validate_exact_cover(units, len(blocks))
    cross_check = _cross_check(source, blocks, pandoc_executable)
    if cross_check.get("review_required"):
        units = tuple(
            replace(
                unit,
                review_required=True,
                evidence=unit.evidence + ("pandoc_content_cross_check_failed",),
            )
            if unit.role == "content_unit"
            else unit
            for unit in units
        )

    document_chapters: list[dict[str, Any]] = []
    source_map: list[dict[str, Any]] = []
    block_policies: list[dict[str, str]] = []
    for unit in units:
        chapter_id = f"{_slug(doc_id, fallback='doc')}_{unit.unit_id}"
        chapter_blocks: list[dict[str, Any]] = []
        for local_order, block_index in enumerate(range(unit.start_block, unit.end_block)):
            block = blocks[block_index]
            block_id = f"{chapter_id}_b{local_order + 1:04d}"
            chapter_blocks.append(
                {
                    "block_id": block_id,
                    "order_index": local_order + 1,
                    "page_ids": [],
                    "block_type": runtime_block_type(block.kind),
                    "is_chapter_opening": local_order == 0,
                    "source_text": block.source_text,
                    "clean_text": _runtime_text(block),
                    "sentences": [],
                    "quality_flags": [],
                    "annotations": {},
                }
            )
            source_map.append(
                {
                    "block_id": block_id,
                    "source_path": source.name,
                    "line_range": [block.line_start, block.line_end],
                    "markdown_anchor": block.anchor,
                    "source_block_kind": block.kind,
                    "heading_level": block.heading_level,
                    "provenance_precision": "markdown_exact_line_range",
                }
            )
            block_policies.append(
                {"block_id": block_id, "translation_policy": _block_policy(block.kind)}
            )
        document_chapters.append(
            {
                "chapter_id": chapter_id,
                "order_index": unit.order_index + 1,
                "title": unit.title,
                "blocks": chapter_blocks,
            }
        )

    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    structure_units = [
        {
            "unit_id": unit.unit_id,
            "chapter_id": document_chapters[index]["chapter_id"],
            "order_index": unit.order_index,
            "title": unit.title,
            "block_range": [unit.start_block, unit.end_block],
            "role": unit.role,
            "translation_policy": unit.translation_policy,
            "confidence": round(unit.confidence, 3),
            "evidence": list(unit.evidence),
            "review_required": unit.review_required,
        }
        for index, unit in enumerate(units)
    ]
    translatable = [
        document_chapters[index]["chapter_id"]
        for index, unit in enumerate(units)
        if unit.role == "content_unit" and not unit.review_required
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "doc_id": doc_id,
        "source": {"path": str(source), "sha256": source_sha256, "format": "markdown"},
        "extractor": {
            "name": "native_markdown_line_scanner",
            "version": "v2",
            "mode": "source_line_blocks_plus_pandoc_cross_check",
        },
        "cross_check": cross_check,
        "warnings": warnings,
        "units": structure_units,
        "translatable_chapter_ids": translatable,
        "review_required_unit_ids": [unit.unit_id for unit in units if unit.review_required],
        "review_required_chapter_ids": [
            document_chapters[index]["chapter_id"]
            for index, unit in enumerate(units)
            if unit.review_required
        ],
        "exact_cover": exact_cover,
        "source_map": source_map,
        "block_policies": block_policies,
    }
    manifest["structure_sha256"] = _canonical_hash(
        {
            "normalizer_version": NORMALIZER_VERSION,
            "source_sha256": source_sha256,
            "units": structure_units,
            "source_map": source_map,
            "block_policies": block_policies,
            "cross_check": cross_check,
            "warnings": warnings,
        }
    )
    title = metadata.get("title") or next(
        (block.heading_title for block in blocks if block.heading_level == 1 and block.heading_title),
        source.stem,
    )
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": {
            "title": title,
            "author": metadata.get("author") or "",
            "domain": "unknown",
            "genre": "unknown",
            "source_language": source_language,
            "target_language": target_language,
            "source_format": "markdown",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": NORMALIZER_VERSION,
            "pipeline_version": NORMALIZER_VERSION,
            "contamination_risk": "medium",
        },
        "chapters": document_chapters,
    }
    return MarkdownNormalizationResult(document=document, structure_manifest=manifest)


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def write_markdown_normalization(
    result: MarkdownNormalizationResult,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    destination = Path(output_dir)
    document_path = destination / "document.json"
    manifest_path = destination / "structure_manifest.json"
    _atomic_json_write(document_path, result.document)
    _atomic_json_write(manifest_path, result.structure_manifest)
    materialize_source_package(
        result.document,
        result.structure_manifest,
        destination,
    )
    return document_path, manifest_path
