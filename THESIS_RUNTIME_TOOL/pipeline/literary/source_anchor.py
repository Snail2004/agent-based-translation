"""Deterministic, NFC-normalized source coordinates for Builder v3.

This module is intentionally offline and independent from the live Builder.
Models supply verbatim text; code locates that text and mints all identifiers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
import unicodedata


@dataclass(frozen=True, order=True)
class SourceAnchor:
    """A half-open Unicode code-point range in the NFC source string."""

    block_id: str
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if not self.block_id:
            raise ValueError("SourceAnchor.block_id must be non-empty")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("SourceAnchor must be a non-empty half-open range")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "block_id": self.block_id,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }

    @classmethod
    def from_value(cls, value: SourceAnchor | Mapping[str, Any]) -> SourceAnchor:
        if isinstance(value, cls):
            return value
        return cls(
            block_id=str(value["block_id"]),
            char_start=int(value["char_start"]),
            char_end=int(value["char_end"]),
        )


@dataclass(frozen=True)
class LocateResult:
    """A location result that makes fail-closed reasons available to validators."""

    anchor: SourceAnchor | None
    failure_reason: str | None = None
    evidence_range: tuple[int, int] | None = None

    @property
    def ok(self) -> bool:
        return self.anchor is not None


@dataclass(frozen=True, order=True)
class SourcePoint:
    """A comparable point in the chapter's NFC source coordinate space."""

    block_order: int
    char_offset: int

    def __post_init__(self) -> None:
        if self.block_order < 0 or self.char_offset < 0:
            raise ValueError("SourcePoint coordinates must be non-negative")

    def to_dict(self) -> dict[str, int]:
        return {"block_order": self.block_order, "char_offset": self.char_offset}


@dataclass(frozen=True)
class SourceInterval:
    """A half-open interval over chapter-order and Unicode code-point offsets."""

    start: SourcePoint
    end: SourcePoint

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("SourceInterval must be non-empty and half-open")

    def contains_point(self, point: SourcePoint) -> bool:
        return self.start <= point < self.end

    def contains_interval(self, other: SourceInterval) -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: SourceInterval) -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}


def _field(block: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(name)
    return getattr(block, name, None)


def nfc_block_string(block: Mapping[str, Any] | Any) -> str:
    """Return the exact Builder-v3 coordinate string for a source block."""

    raw = _field(block, "clean_text") or _field(block, "source_text") or ""
    return unicodedata.normalize("NFC", str(raw))


def nfc_text(value: str | None) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return spans
        spans.append((index, index + len(needle)))
        start = index + len(needle)


def _choose_span(
    spans: Sequence[tuple[int, int]], occurrence_hint: int | None
) -> tuple[int, int] | None:
    if len(spans) == 1:
        return spans[0]
    if occurrence_hint is not None and 1 <= occurrence_hint <= len(spans):
        return spans[occurrence_hint - 1]
    return None


def locate_anchor(
    block: Mapping[str, Any] | Any,
    *,
    anchor_text: str,
    evidence_quote: str,
    occurrence_hint: int | None = None,
) -> LocateResult:
    """Locate an occurrence without guessing when text is ambiguous.

    Evidence is located first.  The identifying anchor must occur uniquely in
    that evidence range; the documented whole-block retry is only allowed when
    an explicit occurrence hint disambiguates it.
    """

    block_id = str(_field(block, "block_id") or "")
    text = nfc_block_string(block)
    anchor = nfc_text(anchor_text)
    evidence = nfc_text(evidence_quote)
    if not block_id or not text:
        return LocateResult(None, "missing_coordinate_source")
    if not anchor or not evidence:
        return LocateResult(None, "missing_anchor_or_evidence")

    evidence_span = _choose_span(_all_spans(text, evidence), occurrence_hint)
    if evidence_span is None:
        return LocateResult(None, "ambiguous_or_missing_evidence")

    inside = _all_spans(text[evidence_span[0] : evidence_span[1]], anchor)
    if len(inside) == 1:
        start = evidence_span[0] + inside[0][0]
        return LocateResult(
            SourceAnchor(block_id, start, evidence_span[0] + inside[0][1]),
            evidence_range=evidence_span,
        )

    whole_block = _choose_span(_all_spans(text, anchor), occurrence_hint)
    if whole_block is None:
        return LocateResult(None, "ambiguous_or_missing_anchor", evidence_span)
    return LocateResult(
        SourceAnchor(block_id, whole_block[0], whole_block[1]),
        evidence_range=evidence_span,
    )


def locate(
    block: Mapping[str, Any] | Any,
    anchor_text: str,
    evidence_quote: str,
    occurrence_hint: int | None = None,
) -> SourceAnchor | None:
    """Compact public locator required by the Builder-v3 contract."""

    return locate_anchor(
        block,
        anchor_text=anchor_text,
        evidence_quote=evidence_quote,
        occurrence_hint=occurrence_hint,
    ).anchor


def anchor_to_dict(value: SourceAnchor | Mapping[str, Any]) -> dict[str, int | str]:
    return SourceAnchor.from_value(value).to_dict()


def _anchor_from_item(item: Mapping[str, Any]) -> SourceAnchor:
    value = item.get("anchor")
    if value is None:
        raise ValueError("item is missing code-filled anchor")
    return SourceAnchor.from_value(value)


def mint_mention_ids(mentions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Mint block-global mention ids from already located anchors.

    Two model rows claiming the exact same occurrence are intentionally rejected:
    no deterministic occurrence identity exists for duplicate rows.
    """

    copied = [deepcopy(dict(item)) for item in mentions]
    by_block: dict[str, list[tuple[int, int, str, int]]] = {}
    for index, item in enumerate(copied):
        anchor = _anchor_from_item(item)
        surface = nfc_text(str(item.get("surface") or ""))
        if not surface:
            raise ValueError("mention surface must be non-empty")
        by_block.setdefault(anchor.block_id, []).append(
            (anchor.char_start, anchor.char_end, surface, index)
        )

    for block_id, rows in by_block.items():
        sorted_rows = sorted(rows, key=lambda row: row[:3])
        seen: set[tuple[int, int, str]] = set()
        for ordinal, (start, end, surface, item_index) in enumerate(sorted_rows, start=1):
            key = (start, end, surface)
            if key in seen:
                raise ValueError(
                    f"duplicate mention occurrence for {block_id}:{start}:{end}:{surface}"
                )
            seen.add(key)
            copied[item_index]["mention_id"] = f"m_{block_id}_{ordinal:02d}"
    return copied


def mint_turn_event_ids(
    speaker_turns: Sequence[Mapping[str, Any]],
    relation_events: Sequence[Mapping[str, Any]],
    *,
    block_order: Mapping[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mint per-type/per-block ids and shared position keys without mutation."""

    turns = [deepcopy(dict(item)) for item in speaker_turns]
    events = [deepcopy(dict(item)) for item in relation_events]
    records: list[dict[str, Any]] = []
    for index, turn in enumerate(turns):
        speaker = turn.get("speaker")
        if not isinstance(speaker, Mapping):
            raise ValueError("turn is missing speaker endpoint")
        anchor = _anchor_from_item(speaker)
        records.append(
            {
                "kind": "turn",
                "type_rank": 0,
                "index": index,
                "block_id": anchor.block_id,
                "char_start": anchor.char_start,
            }
        )
    for index, event in enumerate(events):
        actor = event.get("actor")
        if not isinstance(actor, Mapping):
            raise ValueError("event is missing actor endpoint")
        anchor = _anchor_from_item(actor)
        records.append(
            {
                "kind": "event",
                "type_rank": 1,
                "index": index,
                "block_id": anchor.block_id,
                "char_start": anchor.char_start,
            }
        )

    ties: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for record in records:
        ties.setdefault((record["block_id"], record["char_start"]), []).append(record)
    for tie_records in ties.values():
        for local_ordinal, record in enumerate(
            sorted(tie_records, key=lambda row: (row["type_rank"], row["index"])), start=1
        ):
            record["local_ordinal"] = local_ordinal

    for kind, destination, id_key, endpoint_roles in (
        ("turn", turns, "turn_id", ("speaker", "addressee")),
        ("event", events, "event_id", ("actor", "target")),
    ):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record["kind"] == kind:
                grouped.setdefault(str(record["block_id"]), []).append(record)
        for block_id, group in grouped.items():
            if block_id not in block_order:
                raise ValueError(f"missing block order for {block_id}")
            for sequence, record in enumerate(
                sorted(group, key=lambda row: (row["char_start"], row["local_ordinal"])),
                start=1,
            ):
                item = destination[int(record["index"])]
                prefix = "t" if kind == "turn" else "e"
                minted_id = f"{prefix}_{block_id}_{sequence:02d}"
                item[id_key] = minted_id
                item["position_key"] = (
                    int(block_order[block_id]),
                    int(record["char_start"]),
                    int(record["local_ordinal"]),
                )
                for role in endpoint_roles:
                    endpoint = item.get(role)
                    if endpoint is not None:
                        if not isinstance(endpoint, Mapping):
                            raise ValueError(f"{kind} {role} endpoint must be a mapping")
                        endpoint_dict = dict(endpoint)
                        endpoint_dict["endpoint_id"] = f"{minted_id}#{role}"
                        item[role] = endpoint_dict
                if kind == "turn":
                    address_terms = []
                    for address_index, term in enumerate(item.get("address_terms") or [], start=1):
                        term_dict = dict(term)
                        term_dict["address_occurrence_id"] = f"{minted_id}#addr{address_index}"
                        address_terms.append(term_dict)
                    item["address_terms"] = address_terms
    return turns, events


REFERENCE_FIELD_MAP = {
    "mention_ref": "occurrence_scalar",
    "subject_ref": "occurrence_scalar",
    "event_id": "event_scalar",
    "trigger_event_id": "event_scalar",
    "trigger_ref": "event_scalar",
    "subject_refs": "occurrence_list",
    "endpoint_refs": "occurrence_list",
    "event_ids": "event_list",
}


def remap_references(
    payload: Mapping[str, Any],
    *,
    mention_ids: Mapping[str, str] | None = None,
    endpoint_ids: Mapping[str, str] | None = None,
    event_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a copied payload with only declared occurrence references remapped."""

    mention_ids = dict(mention_ids or {})
    endpoint_ids = dict(endpoint_ids or {})
    event_ids = dict(event_ids or {})
    maps = {
        "occurrence_scalar": {**mention_ids, **endpoint_ids},
        "event_scalar": event_ids,
        "occurrence_list": {**mention_ids, **endpoint_ids},
        "event_list": event_ids,
    }

    def visit(value: Any, field_name: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {str(key): visit(item, str(key)) for key, item in value.items()}
        if isinstance(value, list):
            field_kind = REFERENCE_FIELD_MAP.get(field_name or "")
            mapping = maps.get(field_kind or "") if field_kind and field_kind.endswith("_list") else None
            return [mapping.get(item, item) if mapping and isinstance(item, str) else visit(item) for item in value]
        field_kind = REFERENCE_FIELD_MAP.get(field_name or "")
        mapping = maps.get(field_kind or "") if field_kind and field_kind.endswith("_scalar") else None
        if mapping and isinstance(value, str):
            return mapping.get(value, value)
        return deepcopy(value)

    return visit(payload)


def block_order_index(blocks: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Return the contract's non-heading Builder block order."""

    result: dict[str, int] = {}
    for block in blocks:
        if str(block.get("block_type") or "") not in {"paragraph", "dialogue"}:
            continue
        block_id = str(block.get("block_id") or "")
        if block_id:
            result[block_id] = len(result)
    return result
