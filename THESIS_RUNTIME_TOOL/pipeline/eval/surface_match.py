from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from pipeline.eval.thesis_scoring import normalize_apostrophe


MASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"```.*?```", flags=re.DOTALL),
    re.compile(r"`[^`]*`"),
    re.compile(r":[A-Za-z0-9_-]+:`[^`]*`"),
    re.compile(r"\$[^$\n]*\$"),
    re.compile(r"https?://[^\s)`]+", flags=re.IGNORECASE),
    re.compile(r"\b[\w.-]+\.(?:ai|io|com|org)(?:/[^\s)`]*)?", flags=re.IGNORECASE),
    re.compile(r"\.\. _[^:\n]+:"),
)


@dataclass(frozen=True)
class SurfaceOwner:
    owner_id: str
    needle: str
    case_sensitive: bool = False


@dataclass(frozen=True)
class SurfaceSpan:
    owner_id: str
    needle: str
    start: int
    end: int
    surface: str


@lru_cache(maxsize=8192)
def mask_non_prose(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    mask = [False] * len(value)
    for pattern in MASK_PATTERNS:
        for match in pattern.finditer(value):
            start, end = match.span()
            for index in range(start, end):
                mask[index] = True
    return "".join(" " if is_masked else char for char, is_masked in zip(value, mask, strict=True))


def normalize_surface(text: str) -> str:
    """Length-preserving normalization so spans still index the original text."""

    return unicodedata.normalize("NFC", normalize_apostrophe(str(text or "")))


def find_spans(
    text: str,
    needle: str,
    *,
    case_sensitive: bool = False,
) -> list[tuple[int, int, str]]:
    value = str(text or "")
    normalized_text = mask_non_prose(normalize_surface(value))
    pattern = surface_pattern(needle)
    if not normalized_text or not pattern:
        return []
    flags = re.UNICODE if case_sensitive else re.UNICODE | re.IGNORECASE
    return [
        (match.start(), match.end(), value[match.start():match.end()])
        for match in re.finditer(pattern, normalized_text, flags=flags)
    ]


def allocate_spans(
    text: str,
    owners: Iterable[SurfaceOwner | tuple[str, str] | tuple[str, str, bool]],
) -> dict[str, list[SurfaceSpan]]:
    """Allocate non-overlapping spans across all owners, longest first."""

    owner_items = [_coerce_owner(owner) for owner in owners]
    candidates: list[SurfaceSpan] = []
    for owner in owner_items:
        if not owner.owner_id or not owner.needle:
            continue
        for start, end, surface in find_spans(
            text,
            owner.needle,
            case_sensitive=owner.case_sensitive,
        ):
            candidates.append(SurfaceSpan(owner.owner_id, owner.needle, start, end, surface))

    occupied = [False] * len(str(text or ""))
    selected: list[SurfaceSpan] = []
    for item in sorted(
        candidates,
        key=lambda span: (
            -(span.end - span.start),
            span.start,
            span.end,
            span.owner_id,
            span.needle,
        ),
    ):
        if item.start == item.end or any(occupied[item.start:item.end]):
            continue
        selected.append(item)
        for index in range(item.start, item.end):
            occupied[index] = True

    grouped: dict[str, list[SurfaceSpan]] = defaultdict(list)
    for item in sorted(selected, key=lambda span: (span.start, span.end, span.owner_id, span.needle)):
        grouped[item.owner_id].append(item)
    return dict(grouped)


def surface_pattern(needle: str) -> str:
    normalized_needle = normalize_surface(needle).strip()
    if not normalized_needle:
        return ""
    pieces = [piece for piece in re.split(r"\s+", normalized_needle) if piece]
    if not pieces:
        return ""
    body = r"\s+".join(re.escape(piece) for piece in pieces)
    prefix = r"(?<!\w)" if _is_word_char(normalized_needle[0]) else ""
    suffix = r"(?!\w)" if _is_word_char(normalized_needle[-1]) else ""
    return prefix + body + suffix


def _coerce_owner(owner: SurfaceOwner | tuple[str, str] | tuple[str, str, bool]) -> SurfaceOwner:
    if isinstance(owner, SurfaceOwner):
        return owner
    if len(owner) == 2:
        return SurfaceOwner(str(owner[0]), str(owner[1]), False)
    return SurfaceOwner(str(owner[0]), str(owner[1]), bool(owner[2]))


def _is_word_char(value: str) -> bool:
    return bool(re.match(r"\w", value, flags=re.UNICODE))
