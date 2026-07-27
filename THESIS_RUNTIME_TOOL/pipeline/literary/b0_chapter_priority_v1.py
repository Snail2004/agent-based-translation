"""Mechanical validation for non-authoritative B0 chapter priorities."""

from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


PRIORITY_SCHEMA_VERSION = "b0_chapter_priority_v1"
MAX_PRIORITY_ITEMS = 15


def _surface_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n.,;:!?\"'()[]{}")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _block_text(block: Mapping[str, Any]) -> str:
    return unicodedata.normalize(
        "NFC",
        str(
            block.get("clean_text")
            or block.get("source_text")
            or block.get("text")
            or ""
        ),
    )


def _contains_surface(text: str, surface: str) -> bool:
    start_guard = r"(?<!\w)" if surface[0].isalnum() else ""
    end_guard = r"(?!\w)" if surface[-1].isalnum() else ""
    return (
        re.search(
            start_guard + re.escape(surface) + end_guard,
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        is not None
    )


def priority_schema(*, item_classes: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": MAX_PRIORITY_ITEMS,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["surface", "item_class", "source_block_id"],
            "properties": {
                "surface": {"type": "string", "minLength": 1},
                "item_class": {
                    "type": "string",
                    "enum": sorted(set(item_classes)),
                },
                "source_block_id": {"type": "string", "minLength": 1},
            },
        },
    }


def make_priority_target(
    *, item_class: str, ref_id: str, surface: str, block_ids: Sequence[str]
) -> dict[str, Any]:
    return {
        "item_class": _required_string(item_class, "item_class"),
        "ref_id": _required_string(ref_id, "ref_id"),
        "surface": _required_string(surface, "surface"),
        "block_ids": sorted(
            {
                _required_string(block_id, "block_id")
                for block_id in block_ids
            }
        ),
    }


def validate_priority_order(
    raw_rows: Any,
    *,
    chapter_blocks: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    allowed_item_classes: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop malformed scheduling hints without invalidating semantic inventory."""

    if not isinstance(raw_rows, list):
        raise ValueError("chapter_priority_order must be a list")
    allowed_classes = set(allowed_item_classes)
    block_by_id = {
        _required_string(block.get("block_id"), "block_id"): block
        for block in chapter_blocks
    }
    normalized_targets: list[dict[str, Any]] = []
    for raw in targets:
        target = make_priority_target(
            item_class=str(raw.get("item_class") or ""),
            ref_id=str(raw.get("ref_id") or ""),
            surface=str(raw.get("surface") or ""),
            block_ids=list(raw.get("block_ids") or []),
        )
        normalized_targets.append(target)

    accepted: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for rank, raw in enumerate(raw_rows[:MAX_PRIORITY_ITEMS], start=1):
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("priority row must be an object")
            if set(raw) != {"surface", "item_class", "source_block_id"}:
                raise ValueError("priority row field set differs")
            surface = _required_string(raw.get("surface"), "priority surface")
            item_class = _required_string(raw.get("item_class"), "priority item_class")
            block_id = _required_string(
                raw.get("source_block_id"), "priority source_block_id"
            )
            if item_class not in allowed_classes:
                raise ValueError("priority item_class is outside the closed table")
            block = block_by_id.get(block_id)
            if block is None:
                raise ValueError("priority row cites a foreign block")
            if not _contains_surface(_block_text(block), surface):
                raise ValueError("priority surface is absent from its cited block")
            key = (item_class, _surface_key(surface), block_id)
            if key in seen:
                raise ValueError("priority row duplicates an earlier row")
            resolved = sorted(
                {
                    str(target["ref_id"])
                    for target in normalized_targets
                    if target["item_class"] == item_class
                    and _surface_key(target["surface"]) == _surface_key(surface)
                    and block_id in target["block_ids"]
                }
            )
            if not resolved:
                raise ValueError("priority row does not reference an emitted or supplied item")
            seen.add(key)
            accepted.append(
                {
                    "rank": rank,
                    "surface": surface,
                    "item_class": item_class,
                    "source_block_id": block_id,
                    "resolved_refs": resolved,
                    "authority_effect": "none",
                }
            )
        except ValueError as exc:
            issues.append(
                {
                    "raw_rank": rank,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                }
            )
    if len(raw_rows) > MAX_PRIORITY_ITEMS:
        issues.append(
            {
                "raw_rank": MAX_PRIORITY_ITEMS + 1,
                "reason": "priority list exceeds the bounded cap",
                "raw_row_count": len(raw_rows),
            }
        )
    return accepted, issues


__all__ = [
    "MAX_PRIORITY_ITEMS",
    "PRIORITY_SCHEMA_VERSION",
    "make_priority_target",
    "priority_schema",
    "validate_priority_order",
]
