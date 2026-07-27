from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.step5_types import CanonicalRecord, Step5ContractError, SupportSet


class SupportError(Step5ContractError):
    """Raised when support algebra or its reverse index is malformed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportedItem(CanonicalRecord):
    item_id: str
    support_alternatives: tuple[SupportSet, ...]

    def __post_init__(self) -> None:
        if not self.item_id or not self.support_alternatives:
            raise SupportError("supported items require at least one support alternative")
        ids = [row.support_set_id for row in self.support_alternatives]
        if len(ids) != len(set(ids)):
            raise SupportError("support-set ids must be unique per item")


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportReverseIndex(CanonicalRecord):
    support_index_hash: str
    support_to_items: Mapping[str, tuple[str, ...]]
    items: Mapping[str, SupportedItem]

    def to_canonical_payload(self) -> dict[str, object]:
        return {
            "support_to_items": {
                key: list(value) for key, value in sorted(self.support_to_items.items())
            },
            "items": {
                key: value.to_canonical_payload()
                for key, value in sorted(self.items.items())
            },
        }


def build_support_reverse_index(
    items: tuple[SupportedItem, ...],
) -> SupportReverseIndex:
    by_id: dict[str, SupportedItem] = {}
    reverse: dict[str, set[str]] = {}
    for item in items:
        if item.item_id in by_id:
            raise SupportError(f"duplicate supported item id: {item.item_id}")
        by_id[item.item_id] = item
        for alternative in item.support_alternatives:
            for member_id in alternative.member_ids:
                reverse.setdefault(member_id, set()).add(item.item_id)
    payload = {
        "support_to_items": {
            key: sorted(value) for key, value in sorted(reverse.items())
        },
        "items": {
            key: value.to_canonical_payload() for key, value in sorted(by_id.items())
        },
    }
    return SupportReverseIndex(
        support_index_hash=canonical_hash(payload),
        support_to_items={
            key: tuple(sorted(value)) for key, value in sorted(reverse.items())
        },
        items=by_id,
    )


def verify_support_reverse_index(index: SupportReverseIndex) -> None:
    if canonical_hash(index.to_canonical_payload()) != index.support_index_hash:
        raise SupportError("support reverse-index hash mismatch")
    rebuilt = build_support_reverse_index(tuple(index.items.values()))
    if rebuilt.to_canonical_payload() != index.to_canonical_payload():
        raise SupportError("support reverse-index content mismatch")


def _is_supported(item: SupportedItem, unavailable: set[str]) -> bool:
    return any(
        alternative.member_ids.isdisjoint(unavailable)
        for alternative in item.support_alternatives
    )


def compute_invalidation_cone(
    index: SupportReverseIndex,
    *,
    unavailable_support_ids: frozenset[str],
) -> tuple[str, ...]:
    """Return deterministic topological invalidations from lost supports."""

    verify_support_reverse_index(index)
    unavailable = set(unavailable_support_ids)
    invalidated: list[str] = []
    queued = sorted(
        {
            item_id
            for support_id in unavailable
            for item_id in index.support_to_items.get(support_id, ())
        }
    )
    while queued:
        item_id = queued.pop(0)
        if item_id in unavailable:
            continue
        item = index.items[item_id]
        if _is_supported(item, unavailable):
            continue
        unavailable.add(item_id)
        invalidated.append(item_id)
        for dependent in index.support_to_items.get(item_id, ()):
            if dependent not in unavailable and dependent not in queued:
                queued.append(dependent)
        queued.sort()
    return tuple(invalidated)


__all__ = [
    "SupportError",
    "SupportReverseIndex",
    "SupportedItem",
    "build_support_reverse_index",
    "compute_invalidation_cone",
    "verify_support_reverse_index",
]
