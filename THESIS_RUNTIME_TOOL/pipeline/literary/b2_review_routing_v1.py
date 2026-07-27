"""Typed, language-blind routing for Literary B2 review requests."""

from __future__ import annotations

from copy import deepcopy
import unicodedata
from typing import Any, Mapping, Sequence

from pipeline.literary.b2_contract_v1 import (
    B2ContractError,
    _checked_candidate_ids,
    _required_string,
)


BLOCKING_KINDS = frozenset(
    {
        "scene_ambiguity",
        "unresolved_entity",
        "anchor_defect",
        "timeline_pending",
        "frame_structure",
    }
)
MODEL_REVIEW_QUARANTINE_REASONS = (
    "unknown_candidate_id",
    "invalid_blocking_kind",
    "event_review_requires_timeline_pending",
    "competing_on_non_entity",
    "insufficient_competing",
    "foreign_source_block",
    "malformed_review_row",
)
_EVENT_TIMELINE_REVIEW_KINDS = frozenset(
    {"event_participant", "event_significance", "event_actuality"}
)

_CODE_CALLSITE_BLOCKING_KIND = {
    "frame_narrator_contract": "frame_structure",
    "frame_source_anchor": "anchor_defect",
    "frame_row_conflict": "frame_structure",
    "frame_missing_initial": "frame_structure",
    "turn_endpoint_contract": "scene_ambiguity",
    "turn_source_anchor": "anchor_defect",
    "turn_speaker_pending": "scene_ambiguity",
    "turn_conflicting_rows": "scene_ambiguity",
    "event_participant_contract": "timeline_pending",
    "event_participant_pending": "timeline_pending",
    "event_source_anchor": "anchor_defect",
    "event_significance_pending": "timeline_pending",
    "event_actuality_uncertain": "timeline_pending",
    "event_conflicting_rows": "timeline_pending",
}
_CONDITIONAL_CODE_CALLSITE = "turn_addressee_identity"


class ReviewRoutingError(RuntimeError):
    pass


def route_review(review: Mapping[str, Any]) -> str:
    kind = review["blocking_kind"]
    if kind == "anchor_defect":
        return "C"
    if kind == "unresolved_entity":
        return "B"
    if kind == "scene_ambiguity":
        return "A"
    if kind == "timeline_pending":
        return "D"
    if kind == "frame_structure":
        return "E"
    raise ReviewRoutingError(f"unroutable blocking_kind: {kind!r}")


def normalize_model_reviews_v1(
    rows: Any,
    *,
    allowed_block_ids: set[str],
    block_order: Mapping[str, int],
    allowed_candidate_ids: set[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    if not isinstance(rows, list):
        raise B2ContractError("review_requests must be a list")
    accepted_raw_rows: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for raw in rows:
        quarantine = _quarantined_model_review_v1(
            raw,
            allowed_block_ids=allowed_block_ids,
            allowed_candidate_ids=allowed_candidate_ids,
        )
        if quarantine is not None:
            quarantined.append(quarantine)
            continue
        if not isinstance(raw, Mapping):
            raise B2ContractError("review quarantine classifier admitted a non-object")
        row = deepcopy(dict(raw))
        block_ids = row.get("source_block_ids")
        if not isinstance(block_ids, list) or not block_ids:
            raise B2ContractError(
                "review quarantine classifier admitted missing source blocks"
            )
        normalized_block_ids = [
            _required_string(value, "review source block") for value in block_ids
        ]
        candidates = _checked_candidate_ids(
            row.get("candidate_card_ids"), allowed_candidate_ids, "review request"
        )
        competing = _checked_candidate_ids(
            row.get("competing_card_ids"),
            allowed_candidate_ids,
            "review competing",
        )
        kind = row.get("blocking_kind")
        if kind not in BLOCKING_KINDS:
            raise B2ContractError(
                "review quarantine classifier admitted an invalid blocking_kind"
            )
        normalized = {
            "review_kind": row.get("review_kind"),
            "blocking_kind": kind,
            "source_block_ids": sorted(
                set(normalized_block_ids), key=block_order.__getitem__
            ),
            "candidate_card_ids": candidates,
            "competing_card_ids": competing,
            "reason": row.get("reason"),
            "origin": "model",
            "status": "pending",
        }
        route_review(normalized)
        accepted_raw_rows.append(deepcopy(dict(raw)))
        result.append(normalized)
    return accepted_raw_rows, result, quarantined


def _quarantined_model_review_v1(
    raw: Any,
    *,
    allowed_block_ids: set[str],
    allowed_candidate_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return _quarantine_record_v1(
            raw,
            quarantine_reason="malformed_review_row",
            offending_values=[deepcopy(raw)],
        )

    row = dict(raw)
    candidate_ids = _well_formed_string_list(row.get("candidate_card_ids"))
    competing_ids = _well_formed_string_list(row.get("competing_card_ids"))
    unknown_candidates = _stable_unique(
        [
            value
            for values in (candidate_ids, competing_ids)
            if values is not None
            for value in values
            if value not in allowed_candidate_ids
        ]
    )
    if unknown_candidates:
        return _quarantine_record_v1(
            row,
            quarantine_reason="unknown_candidate_id",
            offending_values=unknown_candidates,
        )

    kind = row.get("blocking_kind")
    if kind not in BLOCKING_KINDS:
        return _quarantine_record_v1(
            row,
            quarantine_reason="invalid_blocking_kind",
            offending_values=(
                [deepcopy(kind)] if "blocking_kind" in row else []
            ),
        )

    if (
        row.get("review_kind") in _EVENT_TIMELINE_REVIEW_KINDS
        and kind != "timeline_pending"
    ):
        return _quarantine_record_v1(
            row,
            quarantine_reason="event_review_requires_timeline_pending",
            offending_values=[deepcopy(kind)],
        )

    if competing_ids is not None:
        if kind != "unresolved_entity" and competing_ids:
            return _quarantine_record_v1(
                row,
                quarantine_reason="competing_on_non_entity",
                offending_values=competing_ids,
            )
        if kind == "unresolved_entity" and len(competing_ids) < 2:
            return _quarantine_record_v1(
                row,
                quarantine_reason="insufficient_competing",
                offending_values=competing_ids,
            )

    block_ids = _well_formed_string_list(row.get("source_block_ids"))
    if block_ids is not None:
        foreign_blocks = _stable_unique(
            [value for value in block_ids if value not in allowed_block_ids]
        )
        if foreign_blocks:
            return _quarantine_record_v1(
                row,
                quarantine_reason="foreign_source_block",
                offending_values=foreign_blocks,
            )
    if not isinstance(row.get("source_block_ids"), list) or not row.get(
        "source_block_ids"
    ):
        return _quarantine_record_v1(
            row,
            quarantine_reason="malformed_review_row",
            offending_values=[],
        )
    return None


def _quarantine_record_v1(
    raw: Any,
    *,
    quarantine_reason: str,
    offending_values: list[Any],
) -> dict[str, Any]:
    if quarantine_reason not in MODEL_REVIEW_QUARANTINE_REASONS:
        raise ReviewRoutingError(
            f"unknown model-review quarantine reason: {quarantine_reason!r}"
        )
    row = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        "quarantine_reason": quarantine_reason,
        "offending_values": deepcopy(offending_values),
        "review_kind": deepcopy(row.get("review_kind")),
        "blocking_kind": deepcopy(row.get("blocking_kind")),
        "source_block_ids": deepcopy(
            row.get("source_block_ids")
            if isinstance(row.get("source_block_ids"), list)
            else []
        ),
        "reason": deepcopy(row.get("reason")),
    }


def _well_formed_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        return None
    return list(value)


def _stable_unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def code_review_v1(
    *,
    callsite: str,
    review_kind: str,
    source_block_ids: Sequence[str],
    candidate_card_ids: Sequence[str],
    reason: str,
    addressee_resolution_status: str | None = None,
) -> dict[str, Any]:
    candidates = sorted(set(candidate_card_ids))
    if callsite == _CONDITIONAL_CODE_CALLSITE:
        if (
            addressee_resolution_status == "ambiguous_candidates"
            and len(candidates) >= 2
        ):
            blocking_kind = "unresolved_entity"
            competing = candidates
        else:
            blocking_kind = "scene_ambiguity"
            competing = []
    else:
        try:
            blocking_kind = _CODE_CALLSITE_BLOCKING_KIND[callsite]
        except KeyError as exc:
            raise ReviewRoutingError(
                f"unregistered B2 code-review callsite: {callsite!r}"
            ) from exc
        competing = []
    if callsite.startswith("event_") and competing:
        raise ReviewRoutingError(
            "event code-review callsite emitted competing card ids"
        )
    row = {
        "review_kind": review_kind,
        "blocking_kind": blocking_kind,
        "source_block_ids": list(source_block_ids),
        "candidate_card_ids": candidates,
        "competing_card_ids": competing,
        "reason": reason,
        "origin": "code",
        "code_callsite": callsite,
        "status": "pending",
    }
    route_review(row)
    return row


def registered_code_review_callsites_v1() -> tuple[str, ...]:
    return tuple(sorted({*_CODE_CALLSITE_BLOCKING_KIND, _CONDITIONAL_CODE_CALLSITE}))


def mechanical_anchor_spans_v1(text: str, anchor: str) -> list[dict[str, int]]:
    """Locate one anchor after syntax-only Unicode and whitespace normalization."""

    normalized_text, offsets = _normalized_text_with_offsets(text)
    normalized_anchor, _unused = _normalized_text_with_offsets(anchor)
    if not normalized_anchor:
        return []
    starts: list[int] = []
    cursor = 0
    while True:
        found = normalized_text.find(normalized_anchor, cursor)
        if found < 0:
            break
        starts.append(found)
        cursor = found + 1
    if len(starts) != 1:
        return []
    start = starts[0]
    end = start + len(normalized_anchor) - 1
    return [
        {
            "char_start": offsets[start][0],
            "char_end": offsets[end][1],
        }
    ]


def mechanical_anchor_defect_v1(review: Mapping[str, Any]) -> dict[str, Any]:
    if route_review(review) != "C":
        raise ReviewRoutingError("non-anchor review entered route C")
    block_ids = review.get("source_block_ids")
    if not isinstance(block_ids, list) or not block_ids:
        raise ReviewRoutingError("anchor defect has no source block")
    return {
        "review_id": review.get("review_id"),
        "route": "C",
        "outcome": "mechanical_defect",
        "source_block_ids": list(block_ids),
        "model_call_performed": False,
    }


def _normalized_text_with_offsets(text: str) -> tuple[str, list[tuple[int, int]]]:
    quote_map = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201f": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
    characters: list[str] = []
    offsets: list[tuple[int, int]] = []
    whitespace_open = False
    for index, source_char in enumerate(text):
        for char in unicodedata.normalize("NFKC", quote_map.get(source_char, source_char)):
            if char.isspace():
                if whitespace_open:
                    offsets[-1] = (offsets[-1][0], index + 1)
                    continue
                characters.append(" ")
                offsets.append((index, index + 1))
                whitespace_open = True
                continue
            whitespace_open = False
            characters.append(char)
            offsets.append((index, index + 1))
    return "".join(characters), offsets


__all__ = [
    "BLOCKING_KINDS",
    "MODEL_REVIEW_QUARANTINE_REASONS",
    "ReviewRoutingError",
    "code_review_v1",
    "mechanical_anchor_defect_v1",
    "mechanical_anchor_spans_v1",
    "normalize_model_reviews_v1",
    "registered_code_review_callsites_v1",
    "route_review",
]
