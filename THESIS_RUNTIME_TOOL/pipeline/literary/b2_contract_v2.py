"""Integrity checks and uncertainty-preserving normalization for B2 V2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b2_contract_v1 import (
    B2ContractError,
    _checked_candidate_ids,
    _code_review,
    _deduplicated_reviews,
    _exact_spans,
    _mint_id,
    _normalize_review_requests,
    _required_string,
    _row_candidate_ids,
    _reject_foreign_response_identity,
    _source_block_catalog,
    _validated_candidate_packet,
    _validated_request,
    _validated_response,
)
from pipeline.literary.b2_prompts_v2 import (
    B2_INTERACTION_PROMPT_ID_V2_1,
    B2_INTERACTION_SYSTEM_PROMPT_V2_1,
    bind_b2_interaction_response_schema_v2,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


B2_INTERACTION_ARTIFACT_SCHEMA_VERSION_V2 = "literary_b2_interaction_artifact_v2"


def normalize_b2_interaction_response_v2(
    *, request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    supplied_schema = request.get("response_schema")
    if not isinstance(supplied_schema, Mapping):
        raise B2ContractError("B2 V2 interaction request omits response schema")
    request_body, payload = _validated_request(
        request,
        request_kind="window_interaction",
        prompt_id=B2_INTERACTION_PROMPT_ID_V2_1,
        prompt_text=B2_INTERACTION_SYSTEM_PROMPT_V2_1,
        response_schema=dict(supplied_schema),
    )
    chapter_id = _required_string(payload.get("chapter_id"), "interaction chapter_id")
    window_id = _required_string(payload.get("window_id"), "interaction window_id")

    active = _source_block_catalog(payload.get("active_blocks"), "active_blocks")
    tail = _source_block_catalog(
        payload.get("preceding_tail"), "preceding_tail", empty_ok=True
    )
    if set(active).intersection(tail):
        raise B2ContractError("B2 V2 active and preceding-tail blocks overlap")
    order = {block_id: index for index, block_id in enumerate(active)}
    candidate_ids = _validated_candidate_packet(payload.get("candidate_packets"))
    expected_schema = bind_b2_interaction_response_schema_v2(
        chapter_id=chapter_id,
        window_id=window_id,
        active_block_ids=list(active),
        support_block_ids=[*active, *tail],
        candidate_card_ids=sorted(candidate_ids),
    )
    if canonical_json(request_body["response_schema"]) != canonical_json(
        expected_schema
    ):
        raise B2ContractError("B2 V2 response-schema bindings differ from context")
    source_raw = deepcopy(dict(response))
    _reject_foreign_response_identity(
        source_raw,
        expected={"chapter_id": chapter_id, "window_id": window_id},
    )
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            source_raw,
            expected={"chapter_id": chapter_id, "window_id": window_id},
        )
    )
    raw = _validated_response(
        normalized_response, expected_schema, "B2 V2 interaction"
    )
    code_reviews: list[dict[str, Any]] = []

    turns = _normalize_observation_rows_v2(
        raw.get("speaker_turns"),
        row_kind="speaker_turn",
        anchor_field="utterance_anchor",
        endpoint_fields=("speaker", "addressee"),
        active_blocks=active,
        tail_blocks=tail,
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        code_reviews=code_reviews,
    )
    events = _normalize_observation_rows_v2(
        raw.get("interaction_events"),
        row_kind="interaction_event",
        anchor_field="event_anchor",
        endpoint_fields=("actor", "target"),
        active_blocks=active,
        tail_blocks=tail,
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        code_reviews=code_reviews,
    )
    overlap_count = _mark_turn_event_overlaps(
        turns=turns,
        events=events,
        active_blocks=active,
        code_reviews=code_reviews,
    )
    turns = _mint_observation_ids("speaker_turn", "b2turn2", turns)
    events = _mint_observation_ids("interaction_event", "b2event2", events)

    model_reviews = _normalize_review_requests(
        raw.get("review_requests"),
        allowed_block_ids=set(active),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        origin="model",
    )
    reviews = _deduplicated_reviews([*model_reviews, *code_reviews], block_order=order)
    body = {
        "schema_version": B2_INTERACTION_ARTIFACT_SCHEMA_VERSION_V2,
        "request_fingerprint": request_body["request_fingerprint"],
        "chapter_id": chapter_id,
        "window_id": window_id,
        "speaker_turns": turns,
        "interaction_events": events,
        "review_requests": reviews,
        "normalization_counts": {
            "raw_speaker_turns": len(raw.get("speaker_turns") or []),
            "normalized_speaker_turns": len(turns),
            "raw_interaction_events": len(raw.get("interaction_events") or []),
            "normalized_interaction_events": len(events),
            "model_review_requests": len(model_reviews),
            "code_review_requests": len(code_reviews),
            "unlocatable_rows": sum(
                row["grounding_status"] == "review_required_unlocatable"
                for row in [*turns, *events]
            ),
            "speaker_support_review_rows": sum(
                row["speaker_authority_status"] == "pending_review"
                for row in turns
            ),
            "turn_event_overlap_events": overlap_count,
        },
        "raw_response_sha256": canonical_hash(source_raw),
        "production_publish_performed": False,
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def _normalize_observation_rows_v2(
    rows: Any,
    *,
    row_kind: str,
    anchor_field: str,
    endpoint_fields: Sequence[str],
    active_blocks: Mapping[str, str],
    tail_blocks: Mapping[str, str],
    block_order: Mapping[str, int],
    allowed_candidate_ids: set[str],
    code_reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise B2ContractError(f"{row_kind} rows must be a list")
    exact: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = deepcopy(dict(raw_row))
        block_id = _required_string(row.get("block_id"), f"{row_kind} block_id")
        if block_id in tail_blocks:
            raise B2ContractError(f"{row_kind} may not be owned by a tail block")
        if block_id not in active_blocks:
            raise B2ContractError(f"{row_kind} cites a foreign source block")
        endpoint_conflict = False
        for field in endpoint_fields:
            endpoint, consistent = _normalized_endpoint_v2(
                row.get(field),
                allowed_candidate_ids=allowed_candidate_ids,
                label=f"{row_kind}.{field}",
            )
            row[field] = endpoint
            endpoint_conflict = endpoint_conflict or not consistent

        anchor = _required_string(row.get(anchor_field), f"{row_kind} anchor")
        spans = _exact_spans(active_blocks[block_id], anchor)
        grounding_status = "grounded" if spans else "review_required_unlocatable"
        row_status = "accepted_observation"
        if endpoint_conflict:
            row_status = "review_required_endpoint_contract"
            code_reviews.append(
                _code_review(
                    review_kind="endpoint_contract",
                    source_block_ids=[block_id],
                    candidate_card_ids=_row_candidate_ids(row),
                    reason="Endpoint status and candidate-card cardinality disagree; the observation remains pending.",
                )
            )
        if not spans:
            row_status = "review_required_unlocatable"
            code_reviews.append(
                _code_review(
                    review_kind="unlocatable_anchor",
                    source_block_ids=[block_id],
                    candidate_card_ids=_row_candidate_ids(row),
                    reason="The model anchor is not an exact substring of its active source block; the row was retained for review.",
                )
            )

        normalized = {
            **row,
            "source_spans": spans,
            "grounding_status": grounding_status,
            "row_status": row_status,
        }
        if row_kind == "speaker_turn":
            _normalize_speaker_support(
                normalized,
                active_blocks=active_blocks,
                tail_blocks=tail_blocks,
                code_reviews=code_reviews,
            )
        exact.setdefault(canonical_hash(normalized), normalized)

    result = list(exact.values())
    collision_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in result:
        key = (str(row["block_id"]), str(row[anchor_field]))
        collision_groups.setdefault(key, []).append(row)
    for (block_id, _anchor), alternatives in collision_groups.items():
        if len(alternatives) <= 1:
            continue
        candidate_union = sorted(
            {candidate_id for row in alternatives for candidate_id in _row_candidate_ids(row)}
        )
        for row in alternatives:
            row["row_status"] = "review_required_conflicting_rows"
        code_reviews.append(
            _code_review(
                review_kind=f"{row_kind}_conflict",
                source_block_ids=[block_id],
                candidate_card_ids=candidate_union,
                reason="Distinct observations share one source anchor; code retained every alternative and selected none.",
            )
        )
    result.sort(
        key=lambda row: (
            block_order[str(row["block_id"])],
            str(row[anchor_field]),
            canonical_hash(row),
        )
    )
    return result


def _normalized_endpoint_v2(
    endpoint: Any, *, allowed_candidate_ids: set[str], label: str
) -> tuple[dict[str, Any], bool]:
    if not isinstance(endpoint, Mapping):
        raise B2ContractError(f"{label} must be an object")
    result = deepcopy(dict(endpoint))
    supplied = _checked_candidate_ids(
        result.get("candidate_card_ids"), allowed_candidate_ids, label
    )
    result["candidate_card_ids"] = supplied
    status = str(result.get("resolution_status"))
    consistent = (
        (status == "resolved_candidate" and len(supplied) == 1)
        or (
            status in {"resolved_joint_candidates", "ambiguous_candidates"}
            and len(supplied) >= 2
        )
        or (status in {"unresolved", "non_entity_voice"} and not supplied)
    )
    if not consistent:
        result["model_resolution_status"] = status
        result["resolution_status"] = "pending_contract_conflict"
    return result, consistent


def _normalize_speaker_support(
    row: dict[str, Any],
    *,
    active_blocks: Mapping[str, str],
    tail_blocks: Mapping[str, str],
    code_reviews: list[dict[str, Any]],
) -> None:
    support = row.get("speaker_support")
    if not isinstance(support, Mapping):
        raise B2ContractError("speaker_support must be an object")
    normalized = deepcopy(dict(support))
    support_block_id = _required_string(
        normalized.get("source_block_id"), "speaker support block"
    )
    source = active_blocks.get(support_block_id)
    if source is None:
        source = tail_blocks.get(support_block_id)
    if source is None:
        raise B2ContractError("speaker support cites a foreign block")
    support_anchor = _required_string(
        normalized.get("support_anchor"), "speaker support anchor"
    )
    support_spans = _exact_spans(source, support_anchor)
    normalized["source_spans"] = support_spans
    normalized["grounding_status"] = (
        "grounded" if support_spans else "review_required_unlocatable"
    )
    row["speaker_support"] = normalized

    speaker_status = str(row["speaker"].get("resolution_status"))
    support_kind = str(normalized.get("support_kind"))
    if (
        not support_spans
        or support_kind == "unresolved"
        or speaker_status in {"unresolved", "non_entity_voice", "pending_contract_conflict"}
    ):
        row["speaker_authority_status"] = "pending_review"
        row["row_status"] = "review_required_speaker_attribution"
        code_reviews.append(
            _code_review(
                review_kind="speaker_attribution",
                source_block_ids=[str(row["block_id"])],
                candidate_card_ids=_row_candidate_ids(row),
                reason="Speaker attribution is unresolved or its cited support is not source-locatable; no speaker authority was granted.",
            )
        )
    elif support_kind == "explicit_reporting_clause":
        row["speaker_authority_status"] = "provisional_explicit"
    else:
        row["speaker_authority_status"] = "provisional_contextual"


def _mark_turn_event_overlaps(
    *,
    turns: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    active_blocks: Mapping[str, str],
    code_reviews: list[dict[str, Any]],
) -> int:
    by_block: dict[str, list[dict[str, Any]]] = {}
    for turn in turns:
        by_block.setdefault(str(turn["block_id"]), []).append(turn)
    overlap_count = 0
    for event in events:
        block_id = str(event["block_id"])
        quoted_spans = _quoted_spans(active_blocks.get(block_id, ""))
        overlapping_turns = [
            turn
            for turn in by_block.get(block_id, [])
            if _event_overlaps_spoken_span(
                turn_spans=turn.get("source_spans"),
                event_spans=event.get("source_spans"),
                quoted_spans=quoted_spans,
            )
        ]
        if not overlapping_turns:
            continue
        overlap_count += 1
        event["row_status"] = "review_required_turn_event_overlap"
        candidate_ids = sorted(
            set(_row_candidate_ids(event)).union(
                candidate_id
                for turn in overlapping_turns
                for candidate_id in _row_candidate_ids(turn)
            )
        )
        code_reviews.append(
            _code_review(
                review_kind="turn_event_overlap",
                source_block_ids=[block_id],
                candidate_card_ids=candidate_ids,
                reason="A non-speech interaction event overlaps a speaker-turn source span; both rows were retained but the event remains pending to prevent double counting.",
            )
        )
    return overlap_count


def _event_overlaps_spoken_span(
    *, turn_spans: Any, event_spans: Any, quoted_spans: Sequence[Mapping[str, int]]
) -> bool:
    if quoted_spans:
        spoken_spans = _span_intersections(turn_spans, quoted_spans)
        if spoken_spans:
            return _spans_overlap(spoken_spans, event_spans)
    return _spans_overlap(turn_spans, event_spans)


def _quoted_spans(text: str) -> list[dict[str, int]]:
    pairs = {"\u201c": "\u201d", "\u00ab": "\u00bb", '"': '"'}
    result: list[dict[str, int]] = []
    start: int | None = None
    closer: str | None = None
    for index, char in enumerate(text):
        if closer is not None:
            if char == closer:
                result.append({"char_start": int(start), "char_end": index + 1})
                start = None
                closer = None
            continue
        if char in pairs:
            start = index
            closer = pairs[char]
    return result


def _span_intersections(left: Any, right: Any) -> list[dict[str, int]]:
    if not isinstance(left, list) or not isinstance(right, Sequence):
        return []
    result: list[dict[str, int]] = []
    for a in left:
        if not isinstance(a, Mapping):
            continue
        for b in right:
            if not isinstance(b, Mapping):
                continue
            start = max(int(a["char_start"]), int(b["char_start"]))
            end = min(int(a["char_end"]), int(b["char_end"]))
            if start < end:
                result.append({"char_start": start, "char_end": end})
    return result


def _spans_overlap(left: Any, right: Any) -> bool:
    if not isinstance(left, list) or not isinstance(right, list):
        return False
    return any(
        int(a["char_start"]) < int(b["char_end"])
        and int(b["char_start"]) < int(a["char_end"])
        for a in left
        for b in right
        if isinstance(a, Mapping) and isinstance(b, Mapping)
    )


def _mint_observation_ids(
    row_kind: str, prefix: str, rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {f"{row_kind}_id": _mint_id(prefix, row), **row}
        for row in rows
    ]


__all__ = [
    "B2_INTERACTION_ARTIFACT_SCHEMA_VERSION_V2",
    "normalize_b2_interaction_response_v2",
]
