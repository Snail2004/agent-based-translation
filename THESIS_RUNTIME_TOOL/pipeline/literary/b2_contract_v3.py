"""Integrity checks and authority-safe normalization for Literary B2 V3."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b2_contract_v1 import (
    B2ContractError,
    _checked_candidate_ids,
    _deduplicated_reviews,
    _exact_spans,
    _mint_id,
    _reject_foreign_response_identity,
    _required_string,
    _source_block_catalog,
    _validated_candidate_packet,
    _validated_request,
    _validated_response,
)
from pipeline.literary.b2_prompts_v3 import (
    B2_FRAME_PROMPT_ID_V5,
    B2_FRAME_SYSTEM_PROMPT_V5,
    B2_SLIM_INTERACTION_PROMPT_ID_V11,
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11,
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)
from pipeline.literary.b2_review_routing_v1 import (
    code_review_v1,
    mechanical_anchor_spans_v1,
    normalize_model_reviews_v1,
)


B2_FRAME_ARTIFACT_SCHEMA_VERSION_V2 = "literary_b2_frame_artifact_v2"
B2_INTERACTION_ARTIFACT_SCHEMA_VERSION_V3 = "literary_b2_interaction_artifact_v3"


def normalize_b2_frame_response_v2(
    *, request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    supplied_schema = request.get("response_schema")
    if not isinstance(supplied_schema, Mapping):
        raise B2ContractError("B2 V2 frame request omits response schema")
    request_body, payload = _validated_request(
        request,
        request_kind="chapter_frame",
        prompt_id=B2_FRAME_PROMPT_ID_V5,
        prompt_text=B2_FRAME_SYSTEM_PROMPT_V5,
        response_schema=dict(supplied_schema),
    )
    chapter_id = _required_string(payload.get("chapter_id"), "frame chapter_id")
    blocks = _source_block_catalog(payload.get("chapter_blocks"), "chapter_blocks")
    ordered_ids = list(blocks)
    if not ordered_ids:
        raise B2ContractError("B2 V2 frame request has no active source blocks")
    order = {block_id: index for index, block_id in enumerate(ordered_ids)}
    candidate_ids = _validated_candidate_packet(payload.get("candidate_packets"))
    expected_schema = b2_frame_response_schema_v2()
    if canonical_json(request_body["response_schema"]) != canonical_json(
        expected_schema
    ):
        raise B2ContractError("B2 V2 frame response-schema bindings differ from context")
    source_raw = deepcopy(dict(response))
    _reject_foreign_response_identity(source_raw, expected={"chapter_id": chapter_id})
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            source_raw,
            expected={"chapter_id": chapter_id},
        )
    )
    intake_response = _validated_response(
        normalized_response,
        _review_intake_response_schema_v1(expected_schema),
        "B2 V2 frame review intake",
    )
    accepted_review_rows, model_reviews, quarantined_reviews = (
        normalize_model_reviews_v1(
            intake_response.get("review_requests"),
            allowed_block_ids=set(ordered_ids),
            block_order=order,
            allowed_candidate_ids=candidate_ids,
        )
    )
    strict_response = deepcopy(dict(intake_response))
    strict_response["review_requests"] = accepted_review_rows
    raw = _validated_response(strict_response, expected_schema, "B2 V2 frame")

    code_reviews: list[dict[str, Any]] = []
    starts_by_block: dict[str, list[dict[str, Any]]] = {}
    exact_seen: set[str] = set()
    for raw_start in raw.get("frame_starts") or []:
        start = deepcopy(dict(raw_start))
        block_id = _required_string(start.get("start_block_id"), "frame start block")
        if block_id not in blocks:
            raise B2ContractError("B2 V2 frame response cites a foreign source block")
        candidates = _checked_candidate_ids(
            start.get("candidate_card_ids"), candidate_ids, "frame narrator"
        )
        status, consistent = _normalized_frame_status(
            str(start.get("narrator_status")), candidates
        )
        cue = start.get("boundary_cue_anchor")
        cue_spans: list[dict[str, int]] = []
        cue_grounding = "not_supplied"
        if cue is not None:
            cue = _required_string(cue, "frame boundary cue")
            cue_spans = _exact_spans(blocks[block_id], cue)
            if not cue_spans:
                cue_spans = mechanical_anchor_spans_v1(blocks[block_id], cue)
            cue_grounding = "grounded" if cue_spans else "review_required_unlocatable"
        normalization_status = "accepted"
        if not consistent:
            normalization_status = "review_required_contract_conflict"
            code_reviews.append(
                code_review_v1(
                    callsite="frame_narrator_contract",
                    review_kind="narrator_contract",
                    source_block_ids=[block_id],
                    candidate_card_ids=candidates,
                    reason=(
                        "Narrator status and candidate-card cardinality disagree; "
                        "no narrator authority was granted."
                    ),
                )
            )
        if cue is not None and not cue_spans:
            normalization_status = "review_required_unlocatable_boundary_cue"
            code_reviews.append(
                code_review_v1(
                    callsite="frame_source_anchor",
                    review_kind="source_anchor",
                    source_block_ids=[block_id],
                    candidate_card_ids=candidates,
                    reason=(
                        "The frame boundary cue is not an exact substring of its "
                        "source block; the frame start remains pending."
                    ),
                )
            )
        normalized = {
            "start_block_id": block_id,
            "narrator_surface": start.get("narrator_surface"),
            "narrator_status": status,
            "candidate_card_ids": candidates,
            "narrative_mode": start.get("narrative_mode"),
            "boundary_cue_anchor": cue,
            "boundary_cue_spans": cue_spans,
            "boundary_cue_grounding": cue_grounding,
            "normalization_status": normalization_status,
        }
        fingerprint = canonical_hash(normalized)
        if fingerprint in exact_seen:
            continue
        exact_seen.add(fingerprint)
        starts_by_block.setdefault(block_id, []).append(normalized)

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
                "narrative_mode": "unclear",
                "boundary_cue_anchor": None,
                "boundary_cue_spans": [],
                "boundary_cue_grounding": "not_supplied",
                "normalization_status": "review_required_conflicting_rows",
                "raw_alternatives": alternatives,
            }
        )
        code_reviews.append(
            code_review_v1(
                callsite="frame_row_conflict",
                review_kind="frame_row_conflict",
                source_block_ids=[block_id],
                candidate_card_ids=candidate_union,
                reason=(
                    "Distinct frame proposals share one start block; code retained "
                    "every alternative and selected none."
                ),
            )
        )

    first_id = ordered_ids[0]
    if not collapsed or collapsed[0]["start_block_id"] != first_id:
        collapsed.insert(0, _unknown_initial_frame(first_id))
        code_reviews.append(
            code_review_v1(
                callsite="frame_missing_initial",
                review_kind="missing_initial_frame",
                source_block_ids=[first_id],
                candidate_card_ids=[],
                reason=(
                    "No model frame started at the first active block; an unknown "
                    "segment preserves exact coverage."
                ),
            )
        )

    collapsed.sort(key=lambda row: order[str(row["start_block_id"])])
    segments: list[dict[str, Any]] = []
    for index, start in enumerate(collapsed):
        start_position = order[str(start["start_block_id"])]
        end_position = (
            order[str(collapsed[index + 1]["start_block_id"])] - 1
            if index + 1 < len(collapsed)
            else len(ordered_ids) - 1
        )
        if end_position < start_position:
            raise B2ContractError("B2 V2 frame starts cannot form exact-cover segments")
        segment_body = {
            **deepcopy(start),
            "end_block_id": ordered_ids[end_position],
            "covered_block_ids": ordered_ids[start_position : end_position + 1],
        }
        segments.append(
            {"frame_segment_id": _mint_id("b2frm2", segment_body), **segment_body}
        )
    if [block for row in segments for block in row["covered_block_ids"]] != ordered_ids:
        raise B2ContractError("normalized B2 V2 frames do not exact-cover chapter blocks")

    reviews = _deduplicated_reviews([*model_reviews, *code_reviews], block_order=order)
    body = {
        "schema_version": B2_FRAME_ARTIFACT_SCHEMA_VERSION_V2,
        "request_fingerprint": request_body["request_fingerprint"],
        "chapter_id": chapter_id,
        "frame_segments": segments,
        "review_requests": reviews,
        "quarantined_review_requests": quarantined_reviews,
        "normalization_counts": {
            "raw_frame_starts": len(raw.get("frame_starts") or []),
            "normalized_frame_segments": len(segments),
            "model_review_requests": len(model_reviews),
            "code_review_requests": len(code_reviews),
            "quarantined_review_requests": len(quarantined_reviews),
            "unlocatable_boundary_cues": sum(
                row["boundary_cue_grounding"] == "review_required_unlocatable"
                for row in segments
            ),
        },
        "raw_response_sha256": canonical_hash(source_raw),
        "production_publish_performed": False,
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def normalize_b2_interaction_response_v3(
    *, request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    supplied_schema = request.get("response_schema")
    if not isinstance(supplied_schema, Mapping):
        raise B2ContractError("B2 V3 interaction request omits response schema")
    request_body, payload = _validated_request(
        request,
        request_kind="window_interaction",
        prompt_id=B2_SLIM_INTERACTION_PROMPT_ID_V11,
        prompt_text=B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11,
        response_schema=dict(supplied_schema),
    )
    chapter_id = _required_string(payload.get("chapter_id"), "interaction chapter_id")
    window_id = _required_string(payload.get("window_id"), "interaction window_id")
    active = _source_block_catalog(payload.get("active_blocks"), "active_blocks")
    tail = _source_block_catalog(
        payload.get("preceding_tail"), "preceding_tail", empty_ok=True
    )
    if set(active).intersection(tail):
        raise B2ContractError("B2 V3 active and preceding-tail blocks overlap")
    order = {block_id: index for index, block_id in enumerate(active)}
    candidate_ids = _validated_candidate_packet(payload.get("candidate_packets"))
    expected_schema = b2_interaction_response_schema_v3()
    if canonical_json(request_body["response_schema"]) != canonical_json(
        expected_schema
    ):
        raise B2ContractError("B2 V3 response-schema bindings differ from context")
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
    normalized_response, utterance_anchor_normalizations = (
        _normalize_overlong_utterance_anchors_v1(normalized_response)
    )
    intake_response = _validated_response(
        normalized_response,
        _interaction_intake_response_schema_v1(expected_schema),
        "B2 V3 interaction review intake",
    )
    accepted_review_rows, model_reviews, quarantined_reviews = (
        normalize_model_reviews_v1(
            intake_response.get("review_requests"),
            allowed_block_ids=set(active),
            block_order=order,
            allowed_candidate_ids=candidate_ids,
        )
    )
    (
        strict_turn_rows,
        turn_rows_for_normalization,
        quarantined_register_cues,
    ) = _partition_register_cue_rows_v1(
        intake_response.get("speaker_turns"),
        response_schema=expected_schema,
    )
    strict_event_rows, quarantined_salient_events = (
        _partition_salient_event_rows_v1(
            intake_response.get("salient_events"),
            response_schema=expected_schema,
        )
    )
    strict_response = deepcopy(dict(intake_response))
    strict_response["review_requests"] = accepted_review_rows
    strict_response["speaker_turns"] = strict_turn_rows
    strict_response["salient_events"] = strict_event_rows
    raw = _validated_response(
        strict_response, expected_schema, "B2 V3 interaction"
    )

    code_reviews: list[dict[str, Any]] = []
    turns = _normalize_turns_v3(
        turn_rows_for_normalization,
        active_blocks=active,
        tail_block_ids=set(tail),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        code_reviews=code_reviews,
    )
    events = _normalize_salient_events_v3(
        raw.get("salient_events"),
        active_blocks=active,
        tail_block_ids=set(tail),
        block_order=order,
        allowed_candidate_ids=candidate_ids,
        code_reviews=code_reviews,
    )
    reviews = _deduplicated_reviews([*model_reviews, *code_reviews], block_order=order)
    body = {
        "schema_version": B2_INTERACTION_ARTIFACT_SCHEMA_VERSION_V3,
        "request_fingerprint": request_body["request_fingerprint"],
        "chapter_id": chapter_id,
        "window_id": window_id,
        "speaker_turns": turns,
        "salient_events": events,
        "review_requests": reviews,
        "quarantined_review_requests": quarantined_reviews,
        "quarantined_register_cues": quarantined_register_cues,
        "quarantined_salient_events": quarantined_salient_events,
        "utterance_anchor_normalizations": utterance_anchor_normalizations,
        "normalization_counts": {
            "raw_speaker_turns": len(intake_response.get("speaker_turns") or []),
            "normalized_speaker_turns": len(turns),
            "quarantined_register_cues": len(quarantined_register_cues),
            "truncated_utterance_anchors": len(utterance_anchor_normalizations),
            "raw_salient_events": len(intake_response.get("salient_events") or []),
            "normalized_salient_events": len(events),
            "quarantined_salient_events": len(quarantined_salient_events),
            "model_review_requests": len(model_reviews),
            "code_review_requests": len(code_reviews),
            "quarantined_review_requests": len(quarantined_reviews),
            "unlocatable_rows": sum(
                row["grounding_status"] == "review_required_unlocatable"
                for row in [*turns, *events]
            ),
            "non_authoritative_events": sum(
                row["event_authority_status"] != "provisional_occurred_observation"
                for row in events
            ),
        },
        "raw_response_sha256": canonical_hash(source_raw),
        "production_publish_performed": False,
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def _normalize_overlong_utterance_anchors_v1(
    response: Mapping[str, Any],
    *,
    maximum_length: int = 500,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bound copied source spans without changing any semantic field."""

    normalized = deepcopy(dict(response))
    rows = normalized.get("speaker_turns")
    if not isinstance(rows, list):
        return normalized, []

    notes: list[dict[str, Any]] = []
    normalized_rows: list[Any] = []
    for row_index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            normalized_rows.append(raw_row)
            continue
        row = deepcopy(dict(raw_row))
        anchor = row.get("utterance_anchor")
        if isinstance(anchor, str) and len(anchor) > maximum_length:
            row["utterance_anchor"] = anchor[:maximum_length]
            notes.append(
                {
                    "row_index": row_index,
                    "block_id": row.get("block_id"),
                    "raw_anchor_sha256": canonical_hash(anchor),
                    "raw_length": len(anchor),
                    "normalized_length": maximum_length,
                    "normalization_reason": "utterance_anchor_exceeded_schema_bound",
                }
            )
        normalized_rows.append(row)
    normalized["speaker_turns"] = normalized_rows
    return normalized, notes


def _normalize_turns_v3(
    rows: Any,
    *,
    active_blocks: Mapping[str, str],
    tail_block_ids: set[str],
    block_order: Mapping[str, int],
    allowed_candidate_ids: set[str],
    code_reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise B2ContractError("speaker_turn rows must be a list")
    exact: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = deepcopy(dict(raw_row))
        block_id = _required_string(row.get("block_id"), "speaker_turn block_id")
        _require_active_block(block_id, active_blocks, tail_block_ids, "speaker_turn")
        speaker, speaker_consistent = _normalized_endpoint_v3(
            row.get("speaker"),
            allowed_candidate_ids=allowed_candidate_ids,
            label="speaker_turn.speaker",
        )
        addressee, addressee_consistent = _normalized_endpoint_v3(
            row.get("addressee"),
            allowed_candidate_ids=allowed_candidate_ids,
            label="speaker_turn.addressee",
        )
        anchor = _required_string(row.get("utterance_anchor"), "speaker_turn anchor")
        spans = _exact_spans(active_blocks[block_id], anchor)
        if not spans:
            spans = mechanical_anchor_spans_v1(active_blocks[block_id], anchor)
        grounding_status = _grounding_status(spans)
        row_status = "accepted_observation"
        if not speaker_consistent or not addressee_consistent:
            row_status = "review_required_endpoint_contract"
            code_reviews.append(
                code_review_v1(
                    callsite="turn_endpoint_contract",
                    review_kind="speaker_attribution",
                    source_block_ids=[block_id],
                    candidate_card_ids=_candidate_ids_from_endpoints(
                        [speaker, addressee]
                    ),
                    reason=(
                        "Turn endpoint status and candidate-card cardinality "
                        "disagree; conflicting authority was withheld."
                    ),
                )
            )
        if grounding_status != "grounded":
            row_status = "review_required_source_anchor"
            code_reviews.append(
                code_review_v1(
                    callsite="turn_source_anchor",
                    review_kind="source_anchor",
                    source_block_ids=[block_id],
                    candidate_card_ids=_candidate_ids_from_endpoints(
                        [speaker, addressee]
                    ),
                    reason=(
                        "The utterance anchor is absent or non-unique in its active "
                        "block; the turn remains pending."
                    ),
                )
            )
        speaker_authority = (
            "provisional_resolved"
            if speaker_consistent
            and speaker["resolution_status"] in {
                "resolved_candidate",
                "resolved_joint_candidates",
            }
            and grounding_status == "grounded"
            else "pending_review"
        )
        addressee_authority = (
            "provisional_resolved"
            if addressee_consistent
            and addressee["resolution_status"] in {
                "resolved_candidate",
                "resolved_joint_candidates",
            }
            else "pending_or_absent"
        )
        if speaker_authority == "pending_review":
            row_status = "review_required_speaker_attribution"
            code_reviews.append(
                code_review_v1(
                    callsite="turn_speaker_pending",
                    review_kind="speaker_attribution",
                    source_block_ids=[block_id],
                    candidate_card_ids=speaker["candidate_card_ids"],
                    reason=(
                        "The speaker is unresolved, ambiguous, or not "
                        "source-grounded; no speaker authority was granted."
                    ),
                )
            )
        if addressee["resolution_status"] in {
            "ambiguous_candidates",
            "pending_contract_conflict",
        }:
            code_reviews.append(
                code_review_v1(
                    callsite="turn_addressee_identity",
                    review_kind="addressee_identity",
                    source_block_ids=[block_id],
                    candidate_card_ids=addressee["candidate_card_ids"],
                    addressee_resolution_status=addressee["resolution_status"],
                    reason=(
                        "The addressee remains ambiguous; the turn is retained "
                        "without addressee authority."
                    ),
                )
            )
        normalized = {
            **row,
            "speaker": speaker,
            "addressee": addressee,
            "source_spans": spans,
            "grounding_status": grounding_status,
            "speaker_authority_status": speaker_authority,
            "addressee_authority_status": addressee_authority,
            "row_status": row_status,
        }
        exact.setdefault(canonical_hash(normalized), normalized)

    result = list(exact.values())
    _mark_conflicting_rows(
        result,
        key=lambda row: (str(row["block_id"]), str(row["utterance_anchor"])),
        review_kind="speaker_attribution",
        code_callsite="turn_conflicting_rows",
        reason=(
            "Distinct turn observations share one source anchor; code retained "
            "every alternative and selected none."
        ),
        code_reviews=code_reviews,
    )
    result.sort(
        key=lambda row: (
            block_order[str(row["block_id"])],
            _first_span_start(row.get("source_spans")),
            str(row["utterance_anchor"]),
            canonical_hash(row),
        )
    )
    normalized_result: list[dict[str, Any]] = []
    for index, row in enumerate(result, 1):
        row_with_id = {"speaker_turn_id": _mint_id("b2turn3", row), **row}
        row_with_id["turn_index_in_window"] = index
        normalized_result.append(row_with_id)
    return normalized_result


def _normalize_salient_events_v3(
    rows: Any,
    *,
    active_blocks: Mapping[str, str],
    tail_block_ids: set[str],
    block_order: Mapping[str, int],
    allowed_candidate_ids: set[str],
    code_reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise B2ContractError("salient_event rows must be a list")
    exact: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = deepcopy(dict(raw_row))
        source_ids = row.get("source_block_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise B2ContractError("salient_event source_block_ids must be non-empty")
        if len(source_ids) != len(set(source_ids)):
            raise B2ContractError("salient_event repeats a source block id")
        for block_id in source_ids:
            _require_active_block(str(block_id), active_blocks, tail_block_ids, "salient_event")
        ordered_source_ids = sorted(
            (str(block_id) for block_id in source_ids), key=lambda value: block_order[value]
        )
        anchor_block_id = _required_string(
            row.get("anchor_block_id"), "salient_event anchor_block_id"
        )
        if anchor_block_id not in ordered_source_ids:
            raise B2ContractError("salient_event anchor block is not a source block")
        anchor = _required_string(row.get("event_anchor"), "salient_event anchor")
        spans = _exact_spans(active_blocks[anchor_block_id], anchor)
        if not spans:
            spans = mechanical_anchor_spans_v1(
                active_blocks[anchor_block_id], anchor
            )
        grounding_status = _grounding_status(spans)

        raw_participants = row.get("participants")
        if not isinstance(raw_participants, list) or not raw_participants:
            raise B2ContractError("salient_event participants must be non-empty")
        participants: list[dict[str, Any]] = []
        participant_conflict = False
        for index, raw_participant in enumerate(raw_participants):
            participant, consistent = _normalized_endpoint_v3(
                raw_participant,
                allowed_candidate_ids=allowed_candidate_ids,
                label=f"salient_event.participants[{index}]",
            )
            participant["role"] = dict(raw_participant).get("role")
            participants.append(participant)
            participant_conflict = participant_conflict or not consistent

        participant_authority = _participant_authority_status(participants)

        review_status = str(row.get("review_status"))
        event_status = str(row.get("event_status"))
        evidence_mode = str(row.get("evidence_mode"))
        row_status = "accepted_observation"
        if participant_conflict:
            row_status = "review_required_event_participant"
            code_reviews.append(
                code_review_v1(
                    callsite="event_participant_contract",
                    review_kind="event_participant",
                    source_block_ids=ordered_source_ids,
                    candidate_card_ids=_candidate_ids_from_endpoints(participants),
                    reason=(
                        "Event participant status and candidate-card cardinality "
                        "disagree; participant authority was withheld."
                    ),
                )
            )
        elif participant_authority in {"ambiguous", "unresolved"}:
            code_reviews.append(
                code_review_v1(
                    callsite="event_participant_pending",
                    review_kind="event_participant",
                    source_block_ids=ordered_source_ids,
                    candidate_card_ids=_candidate_ids_from_endpoints(participants),
                    reason=(
                        "No fully resolved event participant is available; the "
                        "event may remain visible but cannot create participant "
                        "authority."
                    ),
                )
            )
        if grounding_status != "grounded":
            row_status = "review_required_source_anchor"
            code_reviews.append(
                code_review_v1(
                    callsite="event_source_anchor",
                    review_kind="source_anchor",
                    source_block_ids=[anchor_block_id],
                    candidate_card_ids=_candidate_ids_from_endpoints(participants),
                    reason=(
                        "The event anchor is absent or non-unique in its anchor "
                        "block; the event remains pending."
                    ),
                )
            )
        if review_status == "pending_review":
            row_status = "review_required_model_flag"
            code_reviews.append(
                code_review_v1(
                    callsite="event_significance_pending",
                    review_kind="event_significance",
                    source_block_ids=ordered_source_ids,
                    candidate_card_ids=_candidate_ids_from_endpoints(participants),
                    reason=(
                        "The model retained this salient event as pending review; "
                        "no durable authority was granted."
                    ),
                )
            )
        if event_status == "uncertain":
            code_reviews.append(
                code_review_v1(
                    callsite="event_actuality_uncertain",
                    review_kind="event_actuality",
                    source_block_ids=ordered_source_ids,
                    candidate_card_ids=_candidate_ids_from_endpoints(participants),
                    reason=(
                        "Event actuality is uncertain; the observation remains "
                        "non-authoritative."
                    ),
                )
            )

        if (
            row_status == "accepted_observation"
            and event_status == "occurred"
            and evidence_mode == "directly_narrated"
            and review_status == "resolved"
            and participant_authority != "unresolved"
        ):
            authority = "provisional_occurred_observation"
        elif row_status != "accepted_observation":
            authority = "pending_review"
        else:
            authority = "non_authoritative_report_or_proposal"
        normalized = {
            **row,
            "source_block_ids": ordered_source_ids,
            "participants": participants,
            "participant_authority_status": participant_authority,
            "source_spans": spans,
            "grounding_status": grounding_status,
            "event_authority_status": authority,
            "row_status": row_status,
        }
        exact.setdefault(canonical_hash(normalized), normalized)

    result = list(exact.values())
    _mark_conflicting_rows(
        result,
        key=lambda row: (str(row["anchor_block_id"]), str(row["event_anchor"])),
        review_kind="event_significance",
        code_callsite="event_conflicting_rows",
        reason=(
            "Distinct salient-event observations share one source anchor; code "
            "retained every alternative and selected none."
        ),
        code_reviews=code_reviews,
    )
    result.sort(
        key=lambda row: (
            block_order[str(row["anchor_block_id"])],
            _first_span_start(row.get("source_spans")),
            str(row["event_anchor"]),
            canonical_hash(row),
        )
    )
    return [
        {"salient_event_id": _mint_id("b2evt3", row), **row} for row in result
    ]


def _normalized_endpoint_v3(
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
        or (
            status
            in {
                "unresolved",
                "non_entity_voice",
                # An utterance with no listener, or one aimed outside the
                # scene, carries no candidate for the same reason unresolved
                # does. Leaving them out here downgraded a correct reading to
                # pending_contract_conflict and then opened a review for an
                # ambiguity that did not exist.
                "no_addressee",
                "addressee_outside_scene",
            }
            and not supplied
        )
    )
    if not consistent:
        result["model_resolution_status"] = status
        result["resolution_status"] = "pending_contract_conflict"
    return result, consistent


def _normalized_frame_status(
    status: str, candidate_ids: Sequence[str]
) -> tuple[str, bool]:
    consistent = (
        (status == "resolved_candidate" and len(candidate_ids) == 1)
        or (status == "ambiguous_candidates" and len(candidate_ids) >= 2)
        or (status in {"external_or_authorial", "unknown"} and not candidate_ids)
    )
    return (status, True) if consistent else ("pending_contract_conflict", False)


def _unknown_initial_frame(block_id: str) -> dict[str, Any]:
    return {
        "start_block_id": block_id,
        "narrator_surface": None,
        "narrator_status": "unknown",
        "candidate_card_ids": [],
        "narrative_mode": "unclear",
        "boundary_cue_anchor": None,
        "boundary_cue_spans": [],
        "boundary_cue_grounding": "not_supplied",
        "normalization_status": "review_required_missing_initial_frame",
    }


def _require_active_block(
    block_id: str,
    active_blocks: Mapping[str, str],
    tail_block_ids: set[str],
    label: str,
) -> None:
    if block_id in tail_block_ids:
        raise B2ContractError(f"{label} may not be owned by a tail block")
    if block_id not in active_blocks:
        raise B2ContractError(f"{label} cites a foreign source block")


def _grounding_status(spans: Sequence[Mapping[str, int]]) -> str:
    if not spans:
        return "review_required_unlocatable"
    if len(spans) > 1:
        return "review_required_ambiguous_occurrence"
    return "grounded"


def _review_intake_response_schema_v1(
    response_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Relax only the six row faults that review intake can quarantine."""

    relaxed = deepcopy(dict(response_schema))
    try:
        review_list = relaxed["properties"]["review_requests"]
        strict_row = deepcopy(dict(review_list["items"]))
        row_properties = strict_row["properties"]
    except (KeyError, TypeError, ValueError) as exc:
        raise B2ContractError("B2 review schema has an unexpected shape") from exc
    strict_row["required"] = [
        value
        for value in strict_row.get("required") or []
        if value not in {"blocking_kind", "source_block_ids"}
    ]
    row_properties["blocking_kind"] = {}
    source_blocks = deepcopy(dict(row_properties["source_block_ids"]))
    source_blocks["minItems"] = 0
    row_properties["source_block_ids"] = source_blocks
    review_list["items"] = {
        "anyOf": [
            strict_row,
            {"not": {"type": "object"}},
        ]
    }
    return relaxed


def _interaction_intake_response_schema_v1(
    response_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Relax row-owned fields that can be quarantined before strict validation."""

    relaxed = _review_intake_response_schema_v1(response_schema)
    try:
        cue_schema = relaxed["properties"]["speaker_turns"]["items"]["properties"][
            "register_cue"
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise B2ContractError(
            "B2 interaction schema has an unexpected register-cue shape"
        ) from exc
    if not isinstance(cue_schema, dict):
        raise B2ContractError(
            "B2 interaction register-cue schema must be an object"
        )
    cue_schema.pop("enum", None)
    try:
        event_list = relaxed["properties"]["salient_events"]
    except (KeyError, TypeError, ValueError) as exc:
        raise B2ContractError(
            "B2 interaction schema has an unexpected salient-event shape"
        ) from exc
    if not isinstance(event_list, dict) or "items" not in event_list:
        raise B2ContractError(
            "B2 interaction salient-event schema must be an array with items"
        )
    event_list["items"] = {}
    return relaxed


def _partition_register_cue_rows_v1(
    rows: Any,
    *,
    response_schema: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize the open register enum without losing an otherwise valid turn."""

    if not isinstance(rows, list):
        raise B2ContractError("speaker_turn rows must be a list")
    try:
        cue_schema = response_schema["properties"]["speaker_turns"]["items"][
            "properties"
        ]["register_cue"]
        allowed_values = cue_schema["enum"]
    except (KeyError, TypeError, ValueError) as exc:
        raise B2ContractError(
            "B2 interaction schema has an unexpected register-cue enum"
        ) from exc
    if (
        not isinstance(allowed_values, list)
        or not allowed_values
        or any(not isinstance(value, str) for value in allowed_values)
    ):
        raise B2ContractError("B2 interaction register-cue enum is malformed")
    allowed = set(allowed_values)
    if "other" not in allowed:
        raise B2ContractError("B2 interaction register-cue enum lacks other")

    strict_rows: list[dict[str, Any]] = []
    normalization_rows: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise B2ContractError("speaker_turn row must be an object")
        row = deepcopy(dict(raw_row))
        cue = row.get("register_cue")
        cue_raw = row.get("register_cue_raw")
        if cue in allowed and cue != "other" and cue_raw is None:
            strict_rows.append(deepcopy(row))
            row["register_cue_status"] = "in_vocabulary"
            normalization_rows.append(row)
            continue
        if cue == "other" and isinstance(cue_raw, str) and cue_raw:
            strict_rows.append(deepcopy(row))
            row["register_cue_status"] = "model_other"
            normalization_rows.append(row)
            continue

        if cue == "other":
            quarantine_reason = "other_register_cue_missing_raw"
            normalized_raw = cue_raw
        elif cue in allowed:
            quarantine_reason = "known_register_cue_has_raw"
            normalized_raw = cue_raw
        else:
            quarantine_reason = "unsupported_register_cue"
            normalized_raw = cue

        quarantined.append(
            {
                "quarantine_reason": quarantine_reason,
                "raw_value": normalized_raw,
                "block_id": row.get("block_id"),
                "utterance_anchor": row.get("utterance_anchor"),
                "raw_turn_sha256": canonical_hash(row),
            }
        )
        if cue in allowed and cue != "other":
            strict_rows.append(deepcopy(row))
        row["register_cue"] = None
        row["register_cue_raw"] = normalized_raw
        row["register_cue_status"] = "quarantined_invalid_enum"
        normalization_rows.append(row)

    return strict_rows, normalization_rows, quarantined


def _partition_salient_event_rows_v1(
    rows: Any,
    *,
    response_schema: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Quarantine one schema-invalid event without discarding its whole window."""

    if not isinstance(rows, list):
        raise B2ContractError("salient_event rows must be a list")
    try:
        event_schema = response_schema["properties"]["salient_events"]["items"]
    except (KeyError, TypeError, ValueError) as exc:
        raise B2ContractError(
            "B2 interaction schema has an unexpected salient-event item"
        ) from exc
    if not isinstance(event_schema, Mapping):
        raise B2ContractError("B2 interaction salient-event item must be an object")

    validator = Draft202012Validator(dict(event_schema))
    accepted: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            quarantined.append(
                {
                    "quarantine_reason": "event_row_not_object",
                    "row_index": row_index,
                    "schema_path": "",
                    "validator_message": "salient_event row must be an object",
                    "raw_event": deepcopy(raw_row),
                    "raw_event_sha256": canonical_hash(raw_row),
                }
            )
            continue
        row = deepcopy(dict(raw_row))
        errors = sorted(
            validator.iter_errors(row),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
        if not errors:
            accepted.append(row)
            continue
        first = errors[0]
        quarantined.append(
            {
                "quarantine_reason": "event_response_schema_violation",
                "row_index": row_index,
                "schema_path": ".".join(str(part) for part in first.absolute_path),
                "validator_message": first.message,
                "raw_event": row,
                "raw_event_sha256": canonical_hash(row),
            }
        )
    return accepted, quarantined


def _candidate_ids_from_endpoints(
    endpoints: Sequence[Mapping[str, Any]],
) -> list[str]:
    return sorted(
        {
            str(candidate_id)
            for endpoint in endpoints
            for candidate_id in endpoint.get("candidate_card_ids") or []
        }
    )


def _participant_authority_status(
    participants: Sequence[Mapping[str, Any]],
) -> str:
    statuses = {str(row.get("resolution_status")) for row in participants}
    if "pending_contract_conflict" in statuses:
        return "contract_conflict"
    resolved = statuses.intersection(
        {"resolved_candidate", "resolved_joint_candidates"}
    )
    ambiguous = "ambiguous_candidates" in statuses
    if resolved and not ambiguous and statuses.issubset(
        {"resolved_candidate", "resolved_joint_candidates"}
    ):
        return "complete"
    if resolved:
        return "partial"
    if ambiguous:
        return "ambiguous"
    return "unresolved"


def _mark_conflicting_rows(
    rows: Sequence[dict[str, Any]],
    *,
    key: Any,
    review_kind: str,
    code_callsite: str,
    reason: str,
    code_reviews: list[dict[str, Any]],
) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    for alternatives in groups.values():
        if len(alternatives) <= 1:
            continue
        source_ids = sorted(
            {
                str(block_id)
                for row in alternatives
                for block_id in (
                    row.get("source_block_ids") or [row.get("block_id")]
                )
                if block_id
            }
        )
        candidate_ids = sorted(
            {
                candidate_id
                for row in alternatives
                for candidate_id in _candidate_ids_from_row(row)
            }
        )
        for row in alternatives:
            row["row_status"] = "review_required_conflicting_rows"
            if "event_authority_status" in row:
                row["event_authority_status"] = "pending_review"
            if "speaker_authority_status" in row:
                row["speaker_authority_status"] = "pending_review"
        code_reviews.append(
            code_review_v1(
                callsite=code_callsite,
                review_kind=review_kind,
                source_block_ids=source_ids,
                candidate_card_ids=candidate_ids,
                reason=reason,
            )
        )


def _candidate_ids_from_row(row: Mapping[str, Any]) -> list[str]:
    endpoints: list[Mapping[str, Any]] = []
    for field in ("speaker", "addressee"):
        value = row.get(field)
        if isinstance(value, Mapping):
            endpoints.append(value)
    participants = row.get("participants")
    if isinstance(participants, list):
        endpoints.extend(value for value in participants if isinstance(value, Mapping))
    return _candidate_ids_from_endpoints(endpoints)


def _first_span_start(spans: Any) -> int:
    if not isinstance(spans, list) or not spans:
        return 2**31 - 1
    first = spans[0]
    if not isinstance(first, Mapping):
        return 2**31 - 1
    return int(first.get("char_start", 2**31 - 1))


__all__ = [
    "B2_FRAME_ARTIFACT_SCHEMA_VERSION_V2",
    "B2_INTERACTION_ARTIFACT_SCHEMA_VERSION_V3",
    "normalize_b2_frame_response_v2",
    "normalize_b2_interaction_response_v3",
]
