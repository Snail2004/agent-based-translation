from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


SUPPORTED_SOURCE_FORMATS = {"epub", "markdown", "html", "pdf", "txt"}
VERBATIM_KINDS = {"code", "formula", "table"}
TOKEN_RE = re.compile(r"[\w]+(?:['’-][\w]+)*", re.UNICODE)


def source_format_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix == ".epub":
        return "epub"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".txt":
        return "txt"
    raise ValueError(f"Unsupported benchmark source format: {suffix or '<none>'}")


def normalize_text(text: str, kind: str = "paragraph") -> str:
    value = unicodedata.normalize("NFC", str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if kind in VERBATIM_KINDS:
        return "\n".join(line.rstrip() for line in value.strip().splitlines())
    return re.sub(r"\s+", " ", value).strip()


def looks_like_dialogue(text: str) -> bool:
    value = text.lstrip()
    return value.startswith(('"', "“", "‘", "'", "—"))


def normalize_kind(kind: str, text: str) -> str:
    value = str(kind or "paragraph").strip().lower().replace("-", "_")
    aliases = {
        "section_header": "heading",
        "title": "heading",
        "text": "paragraph",
        "para": "paragraph",
        "plain": "paragraph",
        "quote": "block_quote",
        "picture": "image",
        "list": "list_item",
        "equation": "formula",
    }
    value = aliases.get(value, value)
    if value == "paragraph" and looks_like_dialogue(text):
        return "dialogue"
    return value


@dataclass(frozen=True)
class ObservedBlock:
    ordinal: int
    kind: str
    text: str
    heading_level: int | None = None
    source_ref: str | None = None
    native_provenance: bool = False

    def __post_init__(self) -> None:
        normalized_kind = normalize_kind(self.kind, self.text)
        normalized_text = normalize_text(self.text, normalized_kind)
        if self.ordinal < 0:
            raise ValueError("Block ordinal must be non-negative")
        if not normalized_text:
            raise ValueError("Observed blocks must contain text")
        if self.heading_level is not None and self.heading_level < 1:
            raise ValueError("Heading level must be positive")
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "text", normalized_text)

    def stable_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObservedUnit:
    ordinal: int
    title: str
    unit_kind: str
    boundary_level: int | None
    block_ordinals: tuple[int, ...]

    def stable_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["block_ordinals"] = list(self.block_ordinals)
        return value


@dataclass(frozen=True)
class AdapterResult:
    adapter: str
    adapter_version: str
    source_path: str
    source_format: str
    blocks: tuple[ObservedBlock, ...]
    units: tuple[ObservedUnit, ...]
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.source_format not in SUPPORTED_SOURCE_FORMATS:
            raise ValueError(f"Unsupported source format: {self.source_format}")
        expected = list(range(len(self.blocks)))
        actual = [block.ordinal for block in self.blocks]
        if actual != expected:
            raise ValueError(f"Block ordinals must be contiguous: {actual[:10]}")

    def stable_payload(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "source_format": self.source_format,
            "blocks": [block.stable_dict() for block in self.blocks],
            "units": [unit.stable_dict() for unit in self.units],
            "warnings": list(self.warnings),
            "metadata": self.metadata,
        }

    def output_sha256(self) -> str:
        payload = json.dumps(
            self.stable_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_json_dict(self) -> dict[str, Any]:
        value = self.stable_payload()
        value.update(
            {
                "source_path": self.source_path,
                "duration_seconds": round(self.duration_seconds, 6),
                "output_sha256": self.output_sha256(),
            }
        )
        return value


def segment_units(
    blocks: Sequence[ObservedBlock],
    *,
    fallback_title: str = "Document",
) -> tuple[ObservedUnit, ...]:
    if not blocks:
        return ()
    heading_counts = Counter(
        block.heading_level
        for block in blocks
        if block.kind == "heading" and block.heading_level is not None
    )
    repeated = sorted(level for level, count in heading_counts.items() if count >= 2)
    boundary_level = repeated[0] if repeated else None
    boundaries = [
        index
        for index, block in enumerate(blocks)
        if boundary_level is not None
        and block.kind == "heading"
        and block.heading_level == boundary_level
    ]
    if not boundaries:
        title = next((block.text for block in blocks if block.kind == "heading"), fallback_title)
        return (
            ObservedUnit(
                ordinal=0,
                title=title,
                unit_kind="document_unit",
                boundary_level=None,
                block_ordinals=tuple(block.ordinal for block in blocks),
            ),
        )

    units: list[ObservedUnit] = []
    prefix = list(blocks[: boundaries[0]])
    if prefix:
        units.append(
            ObservedUnit(
                ordinal=0,
                title=next((block.text for block in prefix if block.kind == "heading"), "Front matter"),
                unit_kind="front_matter",
                boundary_level=None,
                block_ordinals=tuple(block.ordinal for block in prefix),
            )
        )
    for position, start in enumerate(boundaries):
        end = boundaries[position + 1] if position + 1 < len(boundaries) else len(blocks)
        unit_blocks = blocks[start:end]
        units.append(
            ObservedUnit(
                ordinal=len(units),
                title=blocks[start].text,
                unit_kind="chapter_candidate",
                boundary_level=boundary_level,
                block_ordinals=tuple(block.ordinal for block in unit_blocks),
            )
        )
    return tuple(units)


def lexical_tokens(blocks: Iterable[ObservedBlock]) -> list[str]:
    return [
        token.casefold()
        for block in blocks
        for token in TOKEN_RE.findall(block.text)
    ]


def percentile(values: Sequence[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]
