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


NORMALIZER_VERSION = "txt_hybrid_normalizer_v1"
MANIFEST_SCHEMA_VERSION = "txt_structure_manifest_v1"
DOCUMENT_SCHEMA_VERSION = "1.5.0"

_GUTENBERG_START_RE = re.compile(
    r"^\s*\*{3}\s*START OF (?:THIS|THE) PROJECT GUTENBERG (?:EBOOK|ETEXT).*?\*{3}\s*$",
    re.IGNORECASE,
)
_GUTENBERG_END_RE = re.compile(
    r"^\s*\*{3}\s*END OF (?:THIS|THE) PROJECT GUTENBERG (?:EBOOK|ETEXT).*?\*{3}\s*$",
    re.IGNORECASE,
)
_TOC_RE = re.compile(r"^(?:contents|table\s+of\s+contents)$", re.IGNORECASE)
_PREFIX_RE = re.compile(
    r"^(chapter|stave|letter|book|part|section)\s+([ivxlcdm]+|\d+|[a-z]+)\b.*$",
    re.IGNORECASE,
)
_ROMAN_ONLY_RE = re.compile(r"^[ivxlcdm]{1,12}[.:-]?$", re.IGNORECASE)
_ARABIC_ONLY_RE = re.compile(r"^\d{1,4}[.:-]?$")
_THEMATIC_BREAK_RE = re.compile(r"^\s*(?:[-=_*]\s*){3,}$")
_BACK_TITLE_RE = re.compile(
    r"^(?:afterword|endnotes|notes|colophon|bibliography|references|index|"
    r"appendix|appendices|glossary|license)$",
    re.IGNORECASE,
)
_FRONT_TITLE_RE = re.compile(
    r"^(?:contents|table\s+of\s+contents|illustrations|list\s+of\s+illustrations|"
    r"preface|dedication|title\s*page|imprint|cover)$",
    re.IGNORECASE,
)
_NOTE_RE = re.compile(r"^(?:\[(?:editor|translator|illustration)|note\s*:)", re.IGNORECASE)
_METADATA_RE = re.compile(r"^(title|author|language)\s*:\s*(.+)$", re.IGNORECASE)

_WORD_ORDINALS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


@dataclass(frozen=True)
class TxtBlock:
    ordinal: int
    kind: str
    source_text: str
    line_start: int
    line_end: int
    candidate_type: str | None = None
    candidate_family: str | None = None
    ordinal_value: int | None = None


@dataclass(frozen=True)
class TxtUnit:
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
class TxtNormalizationResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]


def _slug(value: str, *, fallback: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value.casefold()).strip("_")
    return slug or fallback


def _title_key(value: str) -> str:
    folded = unicodedata.normalize("NFKD", normalize_text(value))
    ascii_value = folded.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _roman_value(value: str) -> int | None:
    token = value.strip().rstrip(".:-").upper()
    if not token or not re.fullmatch(r"[IVXLCDM]+", token):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(token):
        current = values[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total if total > 0 else None


def _ordinal_value(value: str) -> int | None:
    token = value.strip().rstrip(".:-").casefold()
    if token.isdigit():
        return int(token)
    if token in _WORD_ORDINALS:
        return _WORD_ORDINALS[token]
    return _roman_value(token)


def _candidate_for_line(line: str) -> tuple[str | None, str | None, int | None]:
    value = normalize_text(line)
    if not value:
        return None, None, None
    if _GUTENBERG_START_RE.fullmatch(value):
        return "gutenberg_start", "marker", None
    if _GUTENBERG_END_RE.fullmatch(value):
        return "gutenberg_end", "marker", None
    if _TOC_RE.fullmatch(value):
        return "toc", "toc", None
    if _BACK_TITLE_RE.fullmatch(value):
        return "back", "back", None
    if _FRONT_TITLE_RE.fullmatch(value):
        return "front", "front", None
    prefixed = _PREFIX_RE.fullmatch(value)
    if prefixed:
        prefix = prefixed.group(1).casefold()
        ordinal = _ordinal_value(prefixed.group(2))
        if ordinal is not None:
            return "prefixed", f"prefixed:{prefix}", ordinal
    if _ROMAN_ONLY_RE.fullmatch(value):
        return "ordinal", "ordinal:roman", _roman_value(value)
    if _ARABIC_ONLY_RE.fullmatch(value):
        return "ordinal", "ordinal:arabic", _ordinal_value(value)
    if _THEMATIC_BREAK_RE.fullmatch(value):
        return "separator", "separator", None
    letters = [character for character in value if character.isalpha()]
    words = value.split()
    if (
        1 <= len(words) <= 16
        and len(value) <= 120
        and len(letters) >= 2
        and sum(character.isupper() for character in letters) / len(letters) >= 0.92
        and not re.search(r"[.!?;]$", value)
    ):
        return "all_caps", "all_caps", None
    return None, None, None


def _dialogue_like(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    quote_chars = {'"', "'", "\u201c", "\u2018"}
    if stripped[0] in quote_chars:
        return True
    quote_count = sum(text.count(character) for character in {'"', "\u201c", "\u201d"})
    return quote_count >= 2


def _paragraph_kind(text: str) -> str:
    if _NOTE_RE.match(text):
        return "footnote"
    if _dialogue_like(text):
        return "dialogue"
    return "paragraph"


def _scan_txt(source: Path) -> tuple[list[TxtBlock], dict[str, str], list[str]]:
    raw = source.read_text(encoding="utf-8-sig", errors="replace")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    warnings: list[str] = []
    replacement_count = normalized.count("\ufffd")
    if replacement_count:
        warnings.append(f"decode_replacement_characters:{replacement_count}")

    blocks: list[TxtBlock] = []
    buffer: list[str] = []
    buffer_start = 0

    def emit(
        text_lines: Sequence[str],
        start_index: int,
        end_index: int,
        *,
        candidate: tuple[str | None, str | None, int | None] | None = None,
    ) -> None:
        source_text = "\n".join(text_lines).strip("\n")
        if not source_text.strip():
            return
        candidate_type, family, ordinal = candidate or (None, None, None)
        if candidate_type in {"gutenberg_start", "gutenberg_end", "separator"}:
            kind = "separator"
        else:
            kind = _paragraph_kind(source_text)
        blocks.append(
            TxtBlock(
                ordinal=len(blocks),
                kind=kind,
                source_text=source_text,
                line_start=start_index + 1,
                line_end=end_index + 1,
                candidate_type=candidate_type,
                candidate_family=family,
                ordinal_value=ordinal,
            )
        )

    def flush(end_index: int) -> None:
        nonlocal buffer, buffer_start
        if buffer:
            emit(buffer, buffer_start, end_index)
        buffer = []
        buffer_start = end_index + 1

    for index, line in enumerate(lines):
        if not line.strip():
            flush(index - 1)
            buffer_start = index + 1
            continue
        candidate = _candidate_for_line(line)
        if candidate[0] is not None:
            flush(index - 1)
            emit([line], index, index, candidate=candidate)
            buffer_start = index + 1
            continue
        if not buffer:
            buffer_start = index
        buffer.append(line)
    flush(len(lines) - 1)

    metadata: dict[str, str] = {}
    for block in blocks[:40]:
        for line in block.source_text.splitlines():
            match = _METADATA_RE.match(normalize_text(line))
            if match and match.group(1).casefold() not in metadata:
                metadata[match.group(1).casefold()] = match.group(2).strip()
    return blocks, metadata, warnings


def _content_characters(
    blocks: Sequence[TxtBlock],
    start: int,
    end: int,
    candidate_indices: set[int],
) -> int:
    return sum(
        len(block.source_text)
        for block in blocks[start:end]
        if block.ordinal not in candidate_indices and block.kind != "separator"
    )


def _toc_matched_headings(
    blocks: Sequence[TxtBlock],
    lower_bound: int,
    upper_bound: int,
) -> list[int]:
    toc = next(
        (
            block
            for block in blocks[lower_bound:upper_bound]
            if block.candidate_type == "toc"
        ),
        None,
    )
    if toc is None:
        return []

    entries: list[TxtBlock] = []
    previous_line = toc.line_end
    for block in blocks[toc.ordinal + 1 : upper_bound]:
        if block.line_start - previous_line > 3:
            break
        if block.candidate_type not in {"prefixed", "ordinal", "all_caps"}:
            break
        entries.append(block)
        previous_line = block.line_end
    if len(entries) < 2:
        return []

    matched: list[int] = []
    cursor = entries[-1].ordinal + 1
    for entry in entries:
        key = _title_key(entry.source_text)
        found = next(
            (
                block.ordinal
                for block in blocks[cursor:upper_bound]
                if block.candidate_type in {"prefixed", "ordinal", "all_caps"}
                and _title_key(block.source_text) == key
            ),
            None,
        )
        if found is None:
            return []
        matched.append(found)
        cursor = found + 1
    return matched


def _family_runs(candidates: Sequence[TxtBlock]) -> list[list[TxtBlock]]:
    runs: list[list[TxtBlock]] = []
    current: list[TxtBlock] = []
    for candidate in candidates:
        if current and candidate.ordinal_value == 1 and current[-1].ordinal_value not in {None, 0, 1}:
            runs.append(current)
            current = []
        current.append(candidate)
    if current:
        runs.append(current)
    return runs


def _best_ordinal_headings(
    blocks: Sequence[TxtBlock],
    lower_bound: int,
    upper_bound: int,
) -> list[int]:
    by_family: dict[str, list[TxtBlock]] = {}
    for block in blocks[lower_bound:upper_bound]:
        if block.candidate_type not in {"prefixed", "ordinal"} or not block.candidate_family:
            continue
        by_family.setdefault(block.candidate_family, []).append(block)

    scored: list[tuple[int, int, int, list[int]]] = []
    for family, candidates in by_family.items():
        family_indices = {candidate.ordinal for candidate in candidates}
        for run in _family_runs(candidates):
            if len(run) < 2:
                continue
            start = run[0].ordinal
            end = next(
                (
                    candidate.ordinal
                    for candidate in candidates
                    if candidate.ordinal > run[-1].ordinal and candidate.ordinal_value == 1
                ),
                upper_bound,
            )
            content = _content_characters(blocks, start, end, family_indices)
            scored.append((content, len(run), -start, [candidate.ordinal for candidate in run]))
    if scored:
        return max(scored)[3]

    single_prefixed = [
        block.ordinal
        for block in blocks[lower_bound:upper_bound]
        if block.candidate_type == "prefixed" and block.candidate_family != "prefixed:part"
    ]
    return single_prefixed if len(single_prefixed) == 1 else []


def _select_content_headings(
    blocks: Sequence[TxtBlock],
    lower_bound: int,
    upper_bound: int,
) -> tuple[list[int], tuple[str, ...]]:
    toc_matches = _toc_matched_headings(blocks, lower_bound, upper_bound)
    if len(toc_matches) >= 2:
        return toc_matches, ("toc_recurrence_match",)
    ordinal = _best_ordinal_headings(blocks, lower_bound, upper_bound)
    if ordinal:
        family = blocks[ordinal[0]].candidate_family or "explicit_heading"
        return ordinal, (f"heading_family:{family}",)
    return [], ()


def _unit_title(block: TxtBlock, position: int) -> str:
    title = normalize_text(block.source_text)
    return title or f"Unit {position + 1}"


def _materialize_units(
    blocks: Sequence[TxtBlock],
    warnings: Sequence[str],
) -> tuple[TxtUnit, ...]:
    start_marker = next((block.ordinal for block in blocks if block.candidate_type == "gutenberg_start"), None)
    end_marker = next((block.ordinal for block in blocks if block.candidate_type == "gutenberg_end"), None)

    lower_bound = (start_marker + 1) if start_marker is not None else 0
    upper_bound = end_marker if end_marker is not None else len(blocks)
    headings, evidence = _select_content_headings(blocks, lower_bound, upper_bound)
    structural_review = bool(warnings)

    boundaries: list[tuple[int, str, str, float, tuple[str, ...]]] = []
    if headings:
        first = headings[0]
        if first > 0:
            boundaries.append((0, "Front matter", "front_matter", 0.90, ("content_prefix",)))
        for position, block_index in enumerate(headings):
            boundaries.append(
                (
                    block_index,
                    _unit_title(blocks[block_index], position),
                    "content_unit",
                    0.88,
                    evidence,
                )
            )
    elif start_marker is not None and upper_bound > lower_bound:
        if lower_bound > 0:
            boundaries.append((0, "Front matter", "front_matter", 0.98, ("gutenberg_start_marker",)))
        boundaries.append(
            (
                lower_bound,
                "Body",
                "content_unit",
                0.82,
                ("gutenberg_body_markers", "single_body_unit"),
            )
        )
    else:
        return (
            TxtUnit(
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

    back_boundary = end_marker
    if back_boundary is None and headings:
        back_boundary = next(
            (
                block.ordinal
                for block in blocks[headings[0] + 1 :]
                if block.candidate_type == "back"
            ),
            None,
        )
    if back_boundary is not None and back_boundary > boundaries[-1][0]:
        boundaries = [boundary for boundary in boundaries if boundary[0] < back_boundary]
        boundaries.append(
            (
                back_boundary,
                "Back matter",
                "back_matter",
                0.96 if end_marker is not None else 0.84,
                ("gutenberg_end_marker",) if end_marker is not None else ("standardized_back_heading",),
            )
        )

    boundaries.sort(key=lambda item: item[0])
    units: list[TxtUnit] = []
    used_ids: set[str] = set()
    for boundary_index, boundary in enumerate(boundaries):
        start, title, role, confidence, boundary_evidence = boundary
        end = boundaries[boundary_index + 1][0] if boundary_index + 1 < len(boundaries) else len(blocks)
        if start >= end:
            continue
        base = _slug(title, fallback=f"unit_{boundary_index + 1}")
        unit_id = f"u{boundary_index + 1:04d}_{base}"
        suffix = 2
        while unit_id in used_ids:
            unit_id = f"u{boundary_index + 1:04d}_{base}_{suffix}"
            suffix += 1
        used_ids.add(unit_id)
        units.append(
            TxtUnit(
                unit_id=unit_id,
                order_index=len(units),
                title=title,
                start_block=start,
                end_block=end,
                role=role,
                translation_policy="translate" if role == "content_unit" else "preserve",
                confidence=confidence,
                evidence=boundary_evidence,
                review_required=structural_review and role == "content_unit",
            )
        )
    return tuple(units)


def _validate_exact_cover(units: Sequence[TxtUnit], block_count: int) -> dict[str, Any]:
    owners: list[int | None] = [None] * block_count
    for unit in units:
        if unit.start_block < 0 or unit.end_block > block_count or unit.start_block >= unit.end_block:
            raise ValueError(f"Invalid TXT unit range: {unit.unit_id}")
        for index in range(unit.start_block, unit.end_block):
            if owners[index] is not None:
                raise ValueError(f"TXT unit overlap at block {index}")
            owners[index] = unit.order_index
    missing = [index for index, owner in enumerate(owners) if owner is None]
    if missing:
        raise ValueError(f"TXT unit exact-cover missing blocks: {missing[:10]}")
    return {
        "expected_blocks": block_count,
        "covered_blocks": block_count,
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }


def _flag_ordinal_sequence_anomalies(
    units: Sequence[TxtUnit],
    blocks: Sequence[TxtBlock],
) -> tuple[tuple[TxtUnit, ...], tuple[str, ...]]:
    content_positions = [
        index
        for index, unit in enumerate(units)
        if unit.role == "content_unit"
        and blocks[unit.start_block].ordinal_value is not None
    ]
    if len(content_positions) < 2:
        return tuple(units), ()

    updated = list(units)
    issues: list[str] = []
    previous = blocks[units[content_positions[0]].start_block].ordinal_value
    assert previous is not None
    for sequence_index, unit_position in enumerate(content_positions[1:], start=1):
        unit = units[unit_position]
        actual = blocks[unit.start_block].ordinal_value
        assert actual is not None
        expected = previous + 1
        if actual == expected:
            previous = actual
            continue

        next_actual = None
        if sequence_index + 1 < len(content_positions):
            next_unit = units[content_positions[sequence_index + 1]]
            next_actual = blocks[next_unit.start_block].ordinal_value
        local_outlier = next_actual == expected + 1
        issue = f"ordinal_sequence_anomaly:{unit.unit_id}:expected_{expected}:found_{actual}"
        issues.append(issue)
        updated[unit_position] = replace(
            unit,
            review_required=True,
            evidence=unit.evidence + ("ordinal_sequence_anomaly",),
        )
        previous = expected if local_outlier else actual
    return tuple(updated), tuple(issues)


def _counter_coverage(left: Sequence[str], right: Sequence[str]) -> float:
    if not left:
        return 1.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    matched = sum(min(count, right_counts[token]) for token, count in left_counts.items())
    return matched / sum(left_counts.values())


def _cross_check(
    source: Path,
    blocks: Sequence[TxtBlock],
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
        if block.kind != "separator"
        for token in TOKEN_RE.findall(block.source_text)
    ]
    pandoc_tokens = [
        token.casefold()
        for block in pandoc.blocks
        for token in TOKEN_RE.findall(block.text)
    ]
    coverage = _counter_coverage(native_tokens, pandoc_tokens)
    return {
        "status": "ok",
        "adapter_version": pandoc.adapter_version,
        "native_block_count": len(blocks),
        "pandoc_block_count": len(pandoc.blocks),
        "native_token_count": len(native_tokens),
        "pandoc_token_count": len(pandoc_tokens),
        "native_covered_by_pandoc": round(coverage, 6),
        "pandoc_covered_by_native": round(_counter_coverage(pandoc_tokens, native_tokens), 6),
        "pandoc_semantic_kinds": sorted({block.kind for block in pandoc.blocks}),
        "review_required": bool(native_tokens and coverage < 0.98),
        "threshold": 0.98,
    }


def _block_policy(kind: str, role: str) -> str:
    if role != "content_unit" or kind == "separator":
        return "preserve"
    return "translate"


def normalize_txt(
    source_path: str | Path,
    *,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    pandoc_executable: str | None = "pandoc",
) -> TxtNormalizationResult:
    source = Path(source_path).resolve()
    if source.suffix.casefold() != ".txt":
        raise ValueError("TXT normalizer requires a .txt source")
    if not source.is_file():
        raise FileNotFoundError(source)

    blocks, metadata, warnings = _scan_txt(source)
    if not blocks:
        raise ValueError("TXT source contains no visible text blocks")
    has_start_marker = any(block.candidate_type == "gutenberg_start" for block in blocks)
    has_end_marker = any(block.candidate_type == "gutenberg_end" for block in blocks)
    if has_start_marker and not has_end_marker:
        warnings.append("gutenberg_end_marker_missing")
    if has_end_marker and not has_start_marker:
        warnings.append("gutenberg_start_marker_missing")
    units = _materialize_units(blocks, warnings)
    units, ordinal_issues = _flag_ordinal_sequence_anomalies(units, blocks)
    warnings.extend(ordinal_issues)
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

    selected_headings = {
        unit.start_block
        for unit in units
        if unit.role == "content_unit" and blocks[unit.start_block].candidate_type in {"prefixed", "ordinal", "all_caps"}
    }
    document_chapters: list[dict[str, Any]] = []
    source_map: list[dict[str, Any]] = []
    block_policies: list[dict[str, str]] = []
    for unit in units:
        chapter_id = f"{_slug(doc_id, fallback='doc')}_{unit.unit_id}"
        chapter_blocks: list[dict[str, Any]] = []
        for local_order, block_index in enumerate(range(unit.start_block, unit.end_block)):
            block = blocks[block_index]
            block_id = f"{chapter_id}_b{local_order + 1:04d}"
            source_kind = "heading" if block_index in selected_headings else block.kind
            chapter_blocks.append(
                {
                    "block_id": block_id,
                    "order_index": local_order + 1,
                    "page_ids": [],
                    "block_type": runtime_block_type(source_kind),
                    "is_chapter_opening": local_order == 0,
                    "source_text": block.source_text,
                    "clean_text": block.source_text,
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
                    "source_block_kind": source_kind,
                    "candidate_type": block.candidate_type,
                    "candidate_family": block.candidate_family,
                    "provenance_precision": "txt_exact_line_range",
                }
            )
            block_policies.append(
                {
                    "block_id": block_id,
                    "translation_policy": _block_policy(source_kind, unit.role),
                }
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
        "source": {"path": str(source), "sha256": source_sha256, "format": "txt"},
        "extractor": {
            "name": "native_txt_line_scanner",
            "version": "v1",
            "mode": "line_blocks_plus_pandoc_cross_check",
        },
        "cross_check": cross_check,
        "warnings": list(warnings),
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
            "warnings": list(warnings),
        }
    )
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": {
            "title": metadata.get("title") or source.stem,
            "author": metadata.get("author") or "",
            "domain": "unknown",
            "genre": "unknown",
            "source_language": source_language,
            "target_language": target_language,
            "source_format": "txt",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": NORMALIZER_VERSION,
            "pipeline_version": NORMALIZER_VERSION,
            "contamination_risk": "medium",
        },
        "chapters": document_chapters,
    }
    return TxtNormalizationResult(document=document, structure_manifest=manifest)


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


def write_txt_normalization(
    result: TxtNormalizationResult,
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
