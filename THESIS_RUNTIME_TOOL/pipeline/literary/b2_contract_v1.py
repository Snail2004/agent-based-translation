"""Strict integrity checks and continuity-safe normalization for Literary B2 V1."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b2_context_v1 import (
    B2_REQUEST_SCHEMA_VERSION,
    B2ContextError,
)
from pipeline.literary.b2_prompts_v1 import (
    B2_FRAME_PROMPT_ID,
    B2_FRAME_SYSTEM_PROMPT,
    B2_INTERACTION_PROMPT_ID,
    B2_INTERACTION_SYSTEM_PROMPT,
    b2_frame_response_schema,
    b2_interaction_response_schema,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


B2_FRAME_ARTIFACT_SCHEMA_VERSION = "literary_b2_frame_artifact_v1"
B2_INTERACTION_ARTIFACT_SCHEMA_VERSION = "literary_b2_interaction_artifact_v1"


class B2ContractError(B2ContextError):
    """An integrity or closed-contract failure that must pause the run."""


def _reject_foreign_response_identity(
    response: Mapping[str, Any], *, expected: Mapping[str, str]
) -> None:
    """Keep B2 request identity strict before any echo normalization.

    Frame and interaction responses have no independent component identifier.
    Their chapter/window echoes therefore remain part of the request-membership
    boundary and cannot be repaired safely.
    """

    for field, expected_value in expected.items():
        if field in response and response[field] != expected_value:
            raise B2ContractError(f"B2 response {field} differs from request")


def normalize_b2_frame_response_v1(
    *, request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    request_body, payload = _validated_request(
        request,
        request_kind="chapter_frame",
        prompt_id=B2_FRAME_PROMPT_ID,
        prompt_text=B2_FRAME_SYSTEM_PROMPT,
        response_schema=b2_frame_response_schema(),
    )
    chapter_id = _required_string(payload.get("chapter_id"), "frame chapter_id")
    source_raw = deepcopy(dict(response))
    _reject_foreign_response_identity(source_raw, expected={"chapter_id": chapter_id})
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            source_raw,
            expected={"chapter_id": chapter_id},
        )
    )
    raw = _validated_response(
        normalized_response, b2_frame_response_schema(), "B2 frame"
    )

    blocks = _source_block_catalog(payload.get("chapter_blocks"), "chapter_blocks")
    ordered_ids = list(blocks)
    if not ordered_ids:
        raise B2ContractError("B2 frame request has no active source blocks")
    order = {block_id: index for index, block_id in enumerate(ordered_ids)}
    candidate_ids = _validated_candidate_packet(payload.get("candidate_packets"))

    code_reviews: list[dict[str, Any]] = []
    starts_by_block: dict[str, list[dict[str, Any]]] = {}
    exact_seen: set[str] = set()
    for raw_start in raw.get("frame_starts") or []:
        start = deepcopy(dict(raw_start))
        block_id = _required_string(start.get("start_block_id"), "frame start block")
        if block_id not in blocks:
            raise B2ContractError("B2 frame response cites a foreign source block")
        supplied = _checked_candidate_ids(
            start.get("candidate_card_ids"), candidate_ids, "frame narrator"
        )
        normalized_status, status_consistent = _frame_status(
            str(start.get("narrator_status")), supplied
        )
        normalized = {
            "start_block_id": block_id,
            "narrator_surface": start.get("narrator_surface"),
            "narrator_status": normalized_status,
            "candidate_card_ids": supplied,
            "story_time_label": start.get("story_time_label"),
            "boundary_reason": start.get("boundary_reason"),
            "normalization_status": (
                "accepted" if status_consistent else "review_required_contract_conflict"
            ),
        }
        fingerprint = canonical_hash(normalized)
        if fingerprint in exact_seen:
            continue
        exact_seen.add(fingerprint)
        starts_by_block.setdefault(block_id, []).append(normalized)
        if not status_consistent:
            code_reviews.append(
                _code_review(
                    review_kind="narrator_contract",
                    source_block_ids=[block_id],
                    candidate_card_ids=supplied,
                    reason="Narrator status and candidate-card cardinality disagree; no identity authority was granted.",
                )
            )

    collapsed: list[dict[str, Any]] = []
    for block_id in ordered_ids:
        alternatives = starts_by_block.get(block_id) or []
        if not alternatives:
            continue
        if len(alternatives) == 1:
            collapsed.append(alternatives[0])
            continue
        candidate_union = sorted(
            {
                candidate_id
                for row in alternatives
                for candidate_id in row["candidate_card_ids"]
            }
        )
        collapsed.append(
            {
                "start_block_id": block_id,
                "narrator_surface": None,
                "narrator_status": "pending_conflict",
                "candidate_card_ids": candidate_union,
                "story_time_label": "unclear",
                "boundary_reason": "Conflicting frame proposals begin at this source block.",
                "normalization_status": "review_required_conflicting_rows",
                "raw_alternatives": alternatives,
            }
        )
        code_reviews.append(
            _code_review(
                review_kind="frame_row_conflict",
                source_block_ids=[block_id],
                candidate_card_ids=candidate_union,
                reason="Distinct frame proposals share one start block; code retained every alternative and selected none.",
            )
        )

    first_id = ordered_ids[0]
    if not collapsed or collapsed[0]["start_block_id"] != first_id:
        collapsed.insert(
            0,
            {
                "start_block_id": first_id,
                "narrator_surface": None,
                "narrator_status": "unknown",
                "candidate_card_ids": [],
                "story_time_label": "unclear",
                "boundary_reason": "Code inserted a non-authoritative initial frame because the model omitted one.",
                "normalization_status": "review_required_missing_initial_frame",
            },
        )
        code_reviews.append(
            _code_review(
                review_kind="missing_initial_frame",
                source_block_ids=[first_id],
                candidate_card_ids=[],
                reason="No model frame started at the first active source block; an unknown segment preserves exact cover.",
            )
        )

    collapsed.sort(key=lambda row: order[row["start_block_id"]])
    segments: list[dict[str, Any]] = []
    for index, start in enumerate(collapsed):
        start_position = order[start["start_block_id"]]
        end_position = (
            order[collapsed[index + 1]["start_block_id"]] - 1
            if index + 1 < len(collapsed)
            else len(ordered_ids) - 1
        )
        if end_position < start_position:
            raise B2ContractError("B2 frame start order cannot form exact-cover segments")
        segment_body = {
            **deepcopy(start),
            "end_block_id": ordered_ids[end_position],
            "covered_block_ids": ordered_ids[start_position : end_position + 1],
        }
        segments.append(
            {
                "frame_segment_id": _mint_id("b2frm", segment_body),
                **segment_body,
            }
        )
    if [block_id for row in segments for block_id in row["covered_block_ids"]] != ordered_ids:
        raise B2ContractError("normalized B2 frames do not exact-cover chapter blocks")

    model_reviews = _normalize_review_requests(
        raw.get("review_requests"),
        allowed_block_ids=set(ordered_ids),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        origin="model",
    )
    reviews = _deduplicated_reviews([*model_reviews, *code_reviews], block_order=order)
    body = {
        "schema_version": B2_FRAME_ARTIFACT_SCHEMA_VERSION,
        "request_fingerprint": request_body["request_fingerprint"],
        "chapter_id": chapter_id,
        "chapter_orientation": deepcopy(raw["chapter_orientation"]),
        "frame_segments": segments,
        "review_requests": reviews,
        "normalization_counts": {
            "raw_frame_starts": len(raw.get("frame_starts") or []),
            "normalized_frame_segments": len(segments),
            "model_review_requests": len(model_reviews),
            "code_review_requests": len(code_reviews),
        },
        "raw_response_sha256": canonical_hash(source_raw),
        "production_publish_performed": False,
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def normalize_b2_interaction_response_v1(
    *, request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    request_body, payload = _validated_request(
        request,
        request_kind="window_interaction",
        prompt_id=B2_INTERACTION_PROMPT_ID,
        prompt_text=B2_INTERACTION_SYSTEM_PROMPT,
        response_schema=b2_interaction_response_schema(),
    )
    chapter_id = _required_string(payload.get("chapter_id"), "interaction chapter_id")
    window_id = _required_string(payload.get("window_id"), "interaction window_id")
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
        normalized_response, b2_interaction_response_schema(), "B2 interaction"
    )

    active = _source_block_catalog(payload.get("active_blocks"), "active_blocks")
    tail = _source_block_catalog(payload.get("preceding_tail"), "preceding_tail", empty_ok=True)
    if set(active).intersection(tail):
        raise B2ContractError("B2 active and preceding-tail blocks overlap")
    order = {block_id: index for index, block_id in enumerate(active)}
    candidate_ids = _validated_candidate_packet(payload.get("candidate_packets"))
    code_reviews: list[dict[str, Any]] = []

    turns = _normalize_observations(
        raw.get("speaker_turns"),
        row_kind="speaker_turn",
        anchor_field="utterance_anchor",
        endpoint_fields=("speaker", "addressee"),
        active_blocks=active,
        tail_block_ids=set(tail),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        code_reviews=code_reviews,
    )
    events = _normalize_observations(
        raw.get("interaction_events"),
        row_kind="interaction_event",
        anchor_field="event_anchor",
        endpoint_fields=("actor", "target"),
        active_blocks=active,
        tail_block_ids=set(tail),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        code_reviews=code_reviews,
    )
    model_reviews = _normalize_review_requests(
        raw.get("review_requests"),
        allowed_block_ids=set(active),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        origin="model",
    )
    reviews = _deduplicated_reviews([*model_reviews, *code_reviews], block_order=order)
    body = {
        "schema_version": B2_INTERACTION_ARTIFACT_SCHEMA_VERSION,
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
        },
        "raw_response_sha256": canonical_hash(source_raw),
        "production_publish_performed": False,
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def _normalize_observations(
    rows: Any,
    *,
    row_kind: str,
    anchor_field: str,
    endpoint_fields: Sequence[str],
    active_blocks: Mapping[str, str],
    tail_block_ids: set[str],
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
        if block_id in tail_block_ids:
            raise B2ContractError(f"{row_kind} may not be owned by a tail block")
        if block_id not in active_blocks:
            raise B2ContractError(f"{row_kind} cites a foreign source block")
        endpoint_conflict = False
        for field in endpoint_fields:
            endpoint = row.get(field)
            if endpoint is None:
                continue
            normalized_endpoint, consistent = _normalized_endpoint(
                endpoint,
                allowed_candidate_ids=allowed_candidate_ids,
                label=f"{row_kind}.{field}",
            )
            row[field] = normalized_endpoint
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
        fingerprint = canonical_hash(normalized)
        exact.setdefault(fingerprint, normalized)

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
                reason="Distinct observations share the same source anchor; code retained every alternative and selected none.",
            )
        )
    result.sort(
        key=lambda row: (
            block_order[str(row["block_id"])],
            str(row[anchor_field]),
            canonical_hash(row),
        )
    )
    id_prefix = "b2turn" if row_kind == "speaker_turn" else "b2event"
    return [{f"{row_kind}_id": _mint_id(id_prefix, row), **row} for row in result]


def _normalized_endpoint(
    endpoint: Any, *, allowed_candidate_ids: set[str], label: str
) -> tuple[dict[str, Any], bool]:
    if not isinstance(endpoint, Mapping):
        raise B2ContractError(f"{label} must be an object or null")
    result = deepcopy(dict(endpoint))
    supplied = _checked_candidate_ids(
        result.get("candidate_card_ids"), allowed_candidate_ids, label
    )
    result["candidate_card_ids"] = supplied
    status = str(result.get("resolution_status"))
    consistent = (
        (status == "resolved_candidate" and len(supplied) == 1)
        or (status == "ambiguous_candidates" and len(supplied) >= 2)
        or (status in {"unresolved", "non_entity_voice"} and not supplied)
    )
    if not consistent:
        result["model_resolution_status"] = status
        result["resolution_status"] = "pending_contract_conflict"
    return result, consistent


def _frame_status(status: str, candidate_ids: Sequence[str]) -> tuple[str, bool]:
    consistent = (
        (status == "resolved_candidate" and len(candidate_ids) == 1)
        or (status == "ambiguous_candidates" and len(candidate_ids) >= 2)
        or (status in {"external_or_authorial", "unknown"} and not candidate_ids)
    )
    return (status, True) if consistent else ("pending_contract_conflict", False)


def _validated_request(
    request: Mapping[str, Any],
    *,
    request_kind: str,
    prompt_id: str,
    prompt_text: str,
    response_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise B2ContractError("B2 request must be an object")
    request_body = deepcopy(dict(request))
    observed = _required_string(
        request_body.pop("request_fingerprint", None), "request_fingerprint"
    )
    if canonical_hash(request_body) != observed:
        raise B2ContractError("B2 request fingerprint mismatch")
    request_body["request_fingerprint"] = observed
    if request_body.get("schema_version") != B2_REQUEST_SCHEMA_VERSION:
        raise B2ContractError("foreign B2 rendered-request schema")
    if request_body.get("request_kind") != request_kind:
        raise B2ContractError("B2 request kind differs from normalizer")
    if request_body.get("prompt_id") != prompt_id:
        raise B2ContractError("B2 prompt id differs from normalizer")
    expected_prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    if request_body.get("prompt_sha256") != expected_prompt_hash:
        raise B2ContractError("B2 prompt bytes differ from prompt id")
    if request_body.get("response_schema") != response_schema:
        raise B2ContractError("B2 response schema differs from prompt contract")
    if request_body.get("response_schema_hash") != canonical_hash(response_schema):
        raise B2ContractError("B2 response schema hash mismatch")
    messages = request_body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise B2ContractError("B2 rendered request must contain system and user messages")
    if messages[0] != {"role": "system", "content": prompt_text}:
        raise B2ContractError("B2 system prompt message differs from prompt bytes")
    if not isinstance(messages[1], Mapping) or messages[1].get("role") != "user":
        raise B2ContractError("B2 user message is malformed")
    try:
        payload = json.loads(str(messages[1].get("content")))
    except (TypeError, ValueError) as exc:
        raise B2ContractError("B2 user message is not a JSON object") from exc
    if not isinstance(payload, dict):
        raise B2ContractError("B2 user message must decode to an object")
    if payload.get("request_kind") != request_kind:
        raise B2ContractError("B2 user payload kind differs from request")
    if payload.get("chapter_id") != request_body.get("chapter_id"):
        raise B2ContractError("B2 user payload chapter differs from request")
    if payload.get("window_id") != request_body.get("window_id"):
        raise B2ContractError("B2 user payload window differs from request")
    packet = payload.get("candidate_packets")
    if not isinstance(packet, Mapping):
        raise B2ContractError("B2 candidate packet is absent")
    packet_body = deepcopy(dict(packet))
    packet_hash = _required_string(packet_body.pop("packet_hash", None), "packet_hash")
    if canonical_hash(packet_body) != packet_hash:
        raise B2ContractError("B2 candidate packet hash mismatch")
    context_hashes = request_body.get("context_hashes")
    if not isinstance(context_hashes, Mapping) or context_hashes.get(
        "candidate_packet_hash"
    ) != packet_hash:
        raise B2ContractError("B2 request points to a different candidate packet")
    return request_body, payload


def _validated_candidate_packet(packet: Any) -> set[str]:
    if not isinstance(packet, Mapping):
        raise B2ContractError("candidate packet must be an object")
    cards = packet.get("candidate_cards")
    if not isinstance(cards, list):
        raise B2ContractError("candidate packet cards must be a list")
    ids = [_required_string(row.get("candidate_card_id"), "candidate_card_id") for row in cards]
    if len(ids) != len(set(ids)):
        raise B2ContractError("candidate packet repeats a candidate card id")
    allowed = set(ids)
    groups = packet.get("surface_groups")
    if not isinstance(groups, list):
        raise B2ContractError("candidate packet surface groups must be a list")
    for group in groups:
        if not isinstance(group, Mapping):
            raise B2ContractError("candidate surface group must be an object")
        supplied = group.get("candidate_card_ids")
        if not isinstance(supplied, list) or not set(supplied).issubset(allowed):
            raise B2ContractError("candidate surface group cites a foreign card")
    return allowed


def _validated_response(
    response: Mapping[str, Any], schema: Mapping[str, Any], label: str
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise B2ContractError(f"{label} response must be an object")
    body = deepcopy(dict(response))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(body),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "$"
        raise B2ContractError(f"{label} violates response schema at {path}: {first.message}")
    return body


def _source_block_catalog(value: Any, label: str, *, empty_ok: bool = False) -> dict[str, str]:
    if not isinstance(value, list) or (not value and not empty_ok):
        raise B2ContractError(f"{label} must be a {'possibly empty ' if empty_ok else 'non-empty '}list")
    result: dict[str, str] = {}
    for row in value:
        if not isinstance(row, Mapping):
            raise B2ContractError(f"{label} contains a non-object row")
        block_id = _required_string(row.get("block_id"), f"{label} block_id")
        text = row.get("text")
        if not isinstance(text, str):
            raise B2ContractError(f"{label} block text must be a string")
        if block_id in result:
            raise B2ContractError(f"{label} repeats a block id")
        result[block_id] = text
    return result


def _normalize_review_requests(
    rows: Any,
    *,
    allowed_block_ids: set[str],
    block_order: Mapping[str, int],
    allowed_candidate_ids: set[str],
    origin: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise B2ContractError("review_requests must be a list")
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = deepcopy(dict(raw))
        block_ids = row.get("source_block_ids")
        if not isinstance(block_ids, list) or not block_ids:
            raise B2ContractError("review request has no source blocks")
        if not set(block_ids).issubset(allowed_block_ids):
            raise B2ContractError("review request cites a foreign or tail block")
        candidates = _checked_candidate_ids(
            row.get("candidate_card_ids"), allowed_candidate_ids, "review request"
        )
        normalized = {
            "review_kind": row.get("review_kind"),
            "source_block_ids": sorted(set(block_ids), key=lambda value: block_order[value]),
            "candidate_card_ids": candidates,
            "reason": row.get("reason"),
            "origin": origin,
            "status": "pending",
        }
        result.append(normalized)
    return result


def _code_review(
    *,
    review_kind: str,
    source_block_ids: Sequence[str],
    candidate_card_ids: Sequence[str],
    reason: str,
) -> dict[str, Any]:
    return {
        "review_kind": review_kind,
        "source_block_ids": list(source_block_ids),
        "candidate_card_ids": sorted(set(candidate_card_ids)),
        "reason": reason,
        "origin": "code",
        "status": "pending",
    }


def _deduplicated_reviews(
    rows: Sequence[Mapping[str, Any]], *, block_order: Mapping[str, int]
) -> list[dict[str, Any]]:
    exact: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = deepcopy(dict(raw))
        row["source_block_ids"] = sorted(
            set(row.get("source_block_ids") or []), key=lambda value: block_order[value]
        )
        row["candidate_card_ids"] = sorted(set(row.get("candidate_card_ids") or []))
        exact.setdefault(canonical_hash(row), row)
    result = list(exact.values())
    result.sort(
        key=lambda row: (
            min(block_order[value] for value in row["source_block_ids"]),
            str(row["review_kind"]),
            canonical_hash(row),
        )
    )
    return [{"review_id": _mint_id("b2review", row), **row} for row in result]


def _checked_candidate_ids(value: Any, allowed: set[str], label: str) -> list[str]:
    if not isinstance(value, list):
        raise B2ContractError(f"{label} candidate ids must be a list")
    result = [_required_string(item, f"{label} candidate id") for item in value]
    if len(result) != len(set(result)):
        raise B2ContractError(f"{label} repeats a candidate id")
    if not set(result).issubset(allowed):
        raise B2ContractError(f"{label} cites a foreign candidate id")
    return sorted(result)


def _row_candidate_ids(row: Mapping[str, Any]) -> list[str]:
    result: set[str] = set()
    for value in row.values():
        if isinstance(value, Mapping):
            candidate_ids = value.get("candidate_card_ids")
            if isinstance(candidate_ids, list):
                result.update(str(item) for item in candidate_ids)
    return sorted(result)


def _exact_spans(text: str, anchor: str) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            return result
        result.append({"char_start": index, "char_end": index + len(anchor)})
        start = index + max(1, len(anchor))


def _mint_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}_{canonical_hash(payload)[:20]}"


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2ContractError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "B2ContractError",
    "B2_FRAME_ARTIFACT_SCHEMA_VERSION",
    "B2_INTERACTION_ARTIFACT_SCHEMA_VERSION",
    "normalize_b2_frame_response_v1",
    "normalize_b2_interaction_response_v1",
]
