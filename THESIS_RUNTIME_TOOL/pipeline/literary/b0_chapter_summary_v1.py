"""End-of-chapter orientation summary and immutable capsule contract."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    B2SlimSpeakerRecoveryError,
    EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION,
    verify_b2_effective_review_projection_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryError,
    verify_b2_recovery_index_v1,
    verify_registry_recovery_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.model_ref_v1 import (
    model_ref_instruction_v1,
    project_model_request_v1,
)
from pipeline.literary.b3_parked_identity_v1 import (
    B3ParkedIdentityError,
    empty_parked_identity_index_v1,
    parked_hearing_for_card_ids_v1,
    verify_parked_identity_index_v1,
)
from pipeline.literary.b3_parked_identity_v2 import (
    PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2,
    parked_hearings_for_card_ids_v2,
    verify_parked_identity_index_v2,
)
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


ROLE_ID = "literary.b0.chapter_summary"
PROMPT_ID = "literary_b0_chapter_summary_v1"
PACKET_SCHEMA_VERSION = "literary_b0_summary_context_v1_1"
RESPONSE_SCHEMA_VERSION = "literary_b0_summary_response_v1"
ARTIFACT_SCHEMA_VERSION = "literary_chapter_summary_artifact_v1"
CAPSULE_LOG_SCHEMA_VERSION = "literary_capsule_log_v1"
_ENTITY_TRANSPORT_LABEL_RE = re.compile(r"E[1-9][0-9]*")

SYSTEM_PROMPT = """You are the end-of-chapter Summary Builder.
Prompt version: literary_b0_chapter_summary_v1.

Summarize ONE supplied chapter for orientation in the next chapter. Use only
the current chapter text and the compact audited B1/B2/B3 records supplied in
this request. Do not use outside knowledge about any book. Do not create,
merge, split, or correct entities, events, states, frames, or review cases.

The prose fields are orientation only, never evidence or authority. Select
only ids that exist in the supplied records. Do not invent ids. The chapter
summary must be chronological, coherent English prose of roughly 250-350
words. The capsule must be one or two sentences and at most roughly 60 tokens.
Mention unresolved material as uncertainty, never as settled fact. Output JSON
only and follow the supplied shape exactly.
"""


class B0ChapterSummaryError(RuntimeError):
    pass


def b2_speaker_recovery_candidate_scope_v1(
    *,
    b2_artifact: Mapping[str, Any],
    recovery_artifact: Mapping[str, Any],
    recovery_index: Mapping[str, Any],
) -> set[str]:
    """Recover the persistent candidate universe sealed into a recovery run."""

    _verify_hashed_object(b2_artifact, "artifact_hash", "B2 artifact")
    _verify_hashed_object(
        recovery_artifact,
        "artifact_hash",
        "speaker recovery artifact",
    )
    try:
        verified_index = verify_b2_recovery_index_v1(recovery_index)
    except B2RecoveryError as exc:
        raise B0ChapterSummaryError(
            "speaker recovery candidate scope is invalid"
        ) from exc

    chapter_id = _text(b2_artifact.get("chapter_id"), "B2 chapter_id")
    b2_artifact_hash = _text(
        b2_artifact.get("artifact_hash"),
        "B2 artifact_hash",
    )
    recovery_index_hash = _text(
        verified_index.get("recovery_index_hash"),
        "recovery_index_hash",
    )
    if (
        verified_index.get("chapter_id") != chapter_id
        or verified_index.get("source_b2_artifact_hash") != b2_artifact_hash
        or recovery_artifact.get("chapter_id") != chapter_id
        or recovery_artifact.get("source_b2_artifact_hash") != b2_artifact_hash
        or recovery_artifact.get("recovery_index_hash") != recovery_index_hash
    ):
        raise B0ChapterSummaryError(
            "speaker recovery candidate scope lineage differs"
        )

    raw_cards = verified_index.get("candidate_cards")
    if not isinstance(raw_cards, list):
        raise B0ChapterSummaryError(
            "speaker recovery candidate scope is malformed"
        )
    candidate_ids: list[str] = []
    for raw_card in raw_cards:
        if not isinstance(raw_card, Mapping):
            raise B0ChapterSummaryError(
                "speaker recovery candidate scope is malformed"
            )
        candidate_ids.append(
            _text(
                raw_card.get("candidate_card_id"),
                "speaker recovery candidate_card_id",
            )
        )
    if len(candidate_ids) != len(set(candidate_ids)):
        raise B0ChapterSummaryError(
            "speaker recovery candidate scope repeats a card"
        )
    local_candidate_ids: list[str] = []
    raw_registry_ledger = recovery_artifact.get("registry_recovery_ledger")
    if raw_registry_ledger is not None:
        if not isinstance(raw_registry_ledger, Mapping):
            raise B0ChapterSummaryError(
                "speaker recovery registry ledger is malformed"
            )
        try:
            registry_ledger = verify_registry_recovery_ledger_v1(
                raw_registry_ledger,
                index=verified_index,
            )
        except B2RecoveryError as exc:
            raise B0ChapterSummaryError(
                "speaker recovery registry ledger is invalid"
            ) from exc
        local_candidate_ids = [
            _text(
                row.get("candidate_card_id"),
                "speaker recovery local candidate_card_id",
            )
            for row in registry_ledger.get("local_candidate_cards") or []
            if isinstance(row, Mapping)
        ]
        if len(local_candidate_ids) != len(
            registry_ledger.get("local_candidate_cards") or []
        ):
            raise B0ChapterSummaryError(
                "speaker recovery local candidate scope is malformed"
            )
    if (
        len(local_candidate_ids) != len(set(local_candidate_ids))
        or set(candidate_ids).intersection(local_candidate_ids)
    ):
        raise B0ChapterSummaryError(
            "speaker recovery local candidate scope differs or collides"
        )
    # Chapter-local cards are independently verified above and supplied through
    # the recovery ledger. They must remain disjoint from the persistent scope
    # accepted by the speaker-recovery verifier.
    return set(candidate_ids)


@dataclass(frozen=True)
class RenderedB0SummaryRequestV1:
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    packet: dict[str, Any]


def b0_summary_response_schema_v1() -> dict[str, Any]:
    ref_list = {
        "type": "array",
        "maxItems": 64,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 180},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "chapter_summary",
            "narrative_handoff",
            "salient_event_refs",
            "effective_state_refs",
            "unresolved_case_refs",
            "capsule",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": RESPONSE_SCHEMA_VERSION},
            "chapter_id": {"type": "string", "minLength": 1},
            "chapter_summary": {"type": "string", "minLength": 1, "maxLength": 6000},
            "narrative_handoff": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "frame_summary",
                    "ending_position",
                    "frame_refs",
                    "entities_mentioned",
                    "locations_mentioned",
                ],
                "properties": {
                    "frame_summary": {"type": "string", "minLength": 1, "maxLength": 1800},
                    "ending_position": {"type": "string", "minLength": 1, "maxLength": 1800},
                    "frame_refs": deepcopy(ref_list),
                    "entities_mentioned": deepcopy(ref_list),
                    "locations_mentioned": deepcopy(ref_list),
                },
            },
            "salient_event_refs": deepcopy(ref_list),
            "effective_state_refs": deepcopy(ref_list),
            "unresolved_case_refs": deepcopy(ref_list),
            "capsule": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "entity_refs", "event_refs", "state_refs"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 1200},
                    "entity_refs": deepcopy(ref_list),
                    "event_refs": deepcopy(ref_list),
                    "state_refs": deepcopy(ref_list),
                },
            },
        },
    }


def build_b0_summary_context_v1(
    *,
    chapter: Mapping[str, Any],
    chapter_order: int,
    b1_registry: Mapping[str, Any],
    b2_artifact: Mapping[str, Any],
    b2_effective_review_projection: Mapping[str, Any] | None = None,
    b3_artifact: Mapping[str, Any] | None = None,
    b3_review_overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    chapter_id = _text(chapter.get("chapter_id"), "chapter_id")
    if not isinstance(chapter_order, int) or isinstance(chapter_order, bool) or chapter_order < 1:
        raise B0ChapterSummaryError("chapter_order must be a positive integer")
    verify_b1_chapter_registry_v1(b1_registry)
    _verify_hashed_object(b2_artifact, "artifact_hash", "B2 artifact")
    if b1_registry.get("chapter_id") != chapter_id or b2_artifact.get("chapter_id") != chapter_id:
        raise B0ChapterSummaryError("B0 source artifacts belong to another chapter")
    if b3_artifact is not None:
        _verify_hashed_object(b3_artifact, "artifact_hash", "B3 artifact")
        if b3_artifact.get("chapter_id") != chapter_id:
            raise B0ChapterSummaryError("B3 artifact belongs to another chapter")
    if b3_review_overlay is not None:
        _verify_hashed_object(b3_review_overlay, "overlay_hash", "B3 review overlay")
        if b3_artifact is None:
            raise B0ChapterSummaryError("B3 review overlay requires its B3 artifact")
        if (
            b3_review_overlay.get("chapter_id") != chapter_id
            or b3_review_overlay.get("source_b3_artifact_hash")
            != b3_artifact.get("artifact_hash")
        ):
            raise B0ChapterSummaryError("B3 review overlay lineage differs")

    blocks = _chapter_blocks(chapter)
    cards = [_compact_card(row) for row in b1_registry.get("cards") or []]
    card_ids = {row["entity_id"] for row in cards}
    alias_rows = _relevant_alias_rows(b1_registry.get("id_alias_table") or [], card_ids)
    frames = [_compact_frame(row) for row in b2_artifact.get("frame_segments") or []]
    events = [_compact_event(row) for row in b2_artifact.get("salient_events") or []]
    raw_b2_reviews = list(b2_artifact.get("review_requests") or [])
    if b2_effective_review_projection is not None:
        try:
            effective_b2_reviews = verify_b2_effective_review_projection_v1(
                chapter_artifact=b2_artifact,
                projection=b2_effective_review_projection,
            )
        except B2SlimSpeakerRecoveryError as exc:
            raise B0ChapterSummaryError(
                "B2 effective review projection is invalid"
            ) from exc
        b2_reviews = effective_b2_reviews["effective_review_requests"]
        b2_review_projection_status = "speaker_recovery_applied"
    else:
        if any(
            isinstance(row, Mapping)
            and row.get("review_kind") == "speaker_attribution"
            for row in raw_b2_reviews
        ):
            raise B0ChapterSummaryError(
                "B2 speaker reviews require an effective review projection"
            )
        b2_reviews = raw_b2_reviews
        b2_review_projection_status = "not_required"

    states: dict[str, dict[str, Any]] = {}
    pending_b3: list[dict[str, Any]] = []
    if b3_artifact is not None:
        for raw in b3_artifact.get("effective_state_projection") or []:
            row = _compact_state(raw)
            states[row["state_id"]] = row
        pending_b3 = [deepcopy(dict(row)) for row in b3_artifact.get("pending_cases") or []]
    resolved_b3: set[str] = set()
    retained_b3: set[str] = set()
    if b3_review_overlay is not None:
        for raw in b3_review_overlay.get("confirmed_state_rows") or []:
            row = _compact_state(raw)
            states[row["state_id"]] = row
        resolved_b3.update(b3_review_overlay.get("resolved_pending_case_ids") or [])
        retained_b3.update(b3_review_overlay.get("retained_pending_case_ids") or [])

    active_b3_cases = [
        row
        for row in pending_b3
        if row.get("pending_case_id") not in resolved_b3
        or row.get("pending_case_id") in retained_b3
    ]
    parked_index = (
        b3_artifact.get("parked_identity_index")
        if b3_artifact is not None
        else None
    )
    unresolved = collapse_unresolved_cases_v1(
        b1_cases=b1_registry.get("pending_reviews") or [],
        b2_cases=b2_reviews,
        b3_cases=active_b3_cases,
        parked_identity_index=(
            parked_index
            if parked_index is not None
            else empty_parked_identity_index_v1()
        ),
    )
    unresolved.sort(key=lambda row: (row["origin_stage"], row["case_id"]))

    body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "chapter_metadata": {
            "chapter_id": chapter_id,
            "chapter_order": chapter_order,
        },
        "chapter_source": blocks,
        "b1_entity_roster": cards,
        "b2_narrative_frames": frames,
        "b2_salient_events": events,
        "b2_review_projection_status": b2_review_projection_status,
        "b3_effective_states": [states[key] for key in sorted(states)],
        "unresolved_cases": unresolved,
        "id_alias_table": alias_rows,
        "authority_policy": {
            "summary_authority": "orientation_only",
            "pending_authoritative": False,
            "may_create_semantic_records": False,
            "may_use_outside_knowledge": False,
        },
        "b3_context_status": "available" if b3_artifact is not None else "not_supplied",
        "production_publish_performed": False,
    }
    return {**body, "packet_hash": canonical_hash(body)}


def render_b0_summary_request_v1(
    packet: Mapping[str, Any],
) -> RenderedB0SummaryRequestV1:
    verified = verify_b0_summary_context_v1(packet)
    schema = b0_summary_response_schema_v1()
    messages = (
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": canonical_json(verified)},
    )
    body = {
        "prompt_id": PROMPT_ID,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "packet_hash": verified["packet_hash"],
        "response_schema_hash": canonical_hash(schema),
        "messages": list(messages),
    }
    return RenderedB0SummaryRequestV1(
        request_fingerprint=canonical_hash(body),
        messages=messages,
        response_schema=schema,
        packet=verified,
    )


def shared_b0_summary_request_v1(
    rendered: RenderedB0SummaryRequestV1,
) -> dict[str, Any]:
    return {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }


def make_b0_summary_semantic_validator_v1(
    *,
    packet: Mapping[str, Any],
    rendered: RenderedB0SummaryRequestV1,
    lineage: Mapping[str, str],
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    verified = verify_b0_summary_context_v1(packet)
    if rendered.packet != verified:
        raise B0ChapterSummaryError("rendered B0 packet differs")
    surface_by_model_ref = _surface_by_model_ref_v1(
        packet=verified,
        rendered=rendered,
    )

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        semantic = validate_b0_summary_response_v1(
            packet=verified,
            response=payload,
            surface_by_model_ref=surface_by_model_ref,
        )
        return build_b0_summary_artifact_v1(
            packet=verified,
            semantic_response=semantic,
            request_fingerprint=rendered.request_fingerprint,
            lineage=lineage,
        )

    return validate


def validate_b0_summary_response_v1(
    *,
    packet: Mapping[str, Any],
    response: Mapping[str, Any] | str,
    surface_by_model_ref: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    verified = verify_b0_summary_context_v1(packet)
    source_raw = _parsed_object(response)
    chapter_id = verified["chapter_metadata"]["chapter_id"]
    raw, normalization_notes = normalize_code_owned_response_echoes_v1(
        source_raw,
        expected={"chapter_id": chapter_id},
    )
    errors = sorted(
        Draft202012Validator(b0_summary_response_schema_v1()).iter_errors(raw),
        key=lambda row: list(row.path),
    )
    if errors:
        raise B0ChapterSummaryError(f"B0 summary schema failure: {errors[0].message}")

    known = _known_refs(verified)
    aliases = _alias_map(verified["id_alias_table"])
    quarantined: list[dict[str, str]] = []

    def refs(field: str, values: Sequence[str], family: str) -> list[str]:
        accepted = []
        for value in values:
            resolved = aliases.get(value, value) if family == "entity" else value
            if resolved not in known[family]:
                quarantined.append(
                    {"field": field, "ref": value, "reason": f"unknown_{family}_ref"}
                )
                continue
            if resolved not in accepted:
                accepted.append(resolved)
        return accepted

    handoff = raw["narrative_handoff"]
    capsule = raw["capsule"]
    normalized_entities, entity_labels_resolved = _normalize_handoff_mentions_v1(
        field="narrative_handoff.entities_mentioned",
        values=handoff["entities_mentioned"],
        surface_by_model_ref=surface_by_model_ref,
    )
    normalized_locations, location_labels_resolved = _normalize_handoff_mentions_v1(
        field="narrative_handoff.locations_mentioned",
        values=handoff["locations_mentioned"],
        surface_by_model_ref=surface_by_model_ref,
    )
    normalized_body = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "chapter_summary": raw["chapter_summary"].strip(),
        "narrative_handoff": {
            "frame_summary": handoff["frame_summary"].strip(),
            "ending_position": handoff["ending_position"].strip(),
            "frame_refs": refs("narrative_handoff.frame_refs", handoff["frame_refs"], "frame"),
            "entities_mentioned": normalized_entities,
            "locations_mentioned": normalized_locations,
        },
        "salient_event_refs": refs("salient_event_refs", raw["salient_event_refs"], "event"),
        "effective_state_refs": refs("effective_state_refs", raw["effective_state_refs"], "state"),
        "unresolved_case_refs": refs("unresolved_case_refs", raw["unresolved_case_refs"], "case"),
        "capsule": {
            "text": capsule["text"].strip(),
            "entity_refs": refs("capsule.entity_refs", capsule["entity_refs"], "entity"),
            "event_refs": refs("capsule.event_refs", capsule["event_refs"], "event"),
            "state_refs": refs("capsule.state_refs", capsule["state_refs"], "state"),
        },
    }
    issues = _budget_issues(normalized_body)
    if entity_labels_resolved or location_labels_resolved:
        issues.append("handoff_transport_labels_resolved")
    if quarantined:
        issues.append("unknown_refs_quarantined")
    body = attach_response_normalization_notes_v1(
        {
            **normalized_body,
            "quarantined_refs": quarantined,
            "review_issues": sorted(set(issues)),
            "authority": "orientation_only",
            "packet_hash": verified["packet_hash"],
        },
        normalization_notes,
    )
    return {**body, "semantic_hash": canonical_hash(body)}


def build_b0_summary_artifact_v1(
    *,
    packet: Mapping[str, Any],
    semantic_response: Mapping[str, Any],
    request_fingerprint: str,
    lineage: Mapping[str, str],
) -> dict[str, Any]:
    verified = verify_b0_summary_context_v1(packet)
    semantic = _normalized_semantic_response(verified, semantic_response)
    capsule_body = {
        "chapter_id": semantic["chapter_id"],
        "revision": 1,
        "supersedes_capsule_id": None,
        "text": semantic["capsule"]["text"],
        "entity_refs": semantic["capsule"]["entity_refs"],
        "event_refs": semantic["capsule"]["event_refs"],
        "state_refs": semantic["capsule"]["state_refs"],
        "authority": "orientation_only",
    }
    capsule = {
        "capsule_id": "litcap1_" + canonical_hash(capsule_body)[:20],
        **capsule_body,
    }
    body = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "chapter_id": semantic["chapter_id"],
        "chapter_order": verified["chapter_metadata"]["chapter_order"],
        "request_fingerprint": request_fingerprint,
        "packet_hash": verified["packet_hash"],
        "lineage": deepcopy(dict(lineage)),
        "summary": semantic,
        "capsule": capsule,
        "production_publish_performed": False,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def build_capsule_log_v1(
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = []
    seen_chapters: set[str] = set()
    for artifact in sorted(artifacts, key=lambda row: row["chapter_order"]):
        verify_b0_summary_artifact_v1(artifact)
        chapter_id = artifact["chapter_id"]
        if chapter_id in seen_chapters:
            raise B0ChapterSummaryError("capsule log has duplicate chapter revision")
        seen_chapters.add(chapter_id)
        rows.append(_capsule_log_row_v1(artifact))
    body = {
        "schema_version": CAPSULE_LOG_SCHEMA_VERSION,
        "capsules": rows,
        "authority": "orientation_only",
        "append_only": True,
    }
    return {**body, "capsule_log_hash": canonical_hash(body)}


def verify_capsule_log_v1(log: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(log))
    observed = row.pop("capsule_log_hash", None)
    if observed != canonical_hash(row):
        raise B0ChapterSummaryError("capsule log hash mismatch")
    if (
        log.get("schema_version") != CAPSULE_LOG_SCHEMA_VERSION
        or log.get("authority") != "orientation_only"
        or log.get("append_only") is not True
    ):
        raise B0ChapterSummaryError("capsule log contract differs")
    capsules = log.get("capsules")
    if not isinstance(capsules, list) or not capsules:
        raise B0ChapterSummaryError("capsule log is empty or malformed")
    orders = [capsule.get("chapter_order") for capsule in capsules if isinstance(capsule, Mapping)]
    chapter_ids = [
        capsule.get("chapter_id") for capsule in capsules if isinstance(capsule, Mapping)
    ]
    if (
        len(orders) != len(capsules)
        or not all(isinstance(order, int) and order > 0 for order in orders)
        or orders != list(range(1, len(capsules) + 1))
        or len(chapter_ids) != len(capsules)
        or not all(isinstance(chapter_id, str) and chapter_id for chapter_id in chapter_ids)
        or len(chapter_ids) != len(set(chapter_ids))
    ):
        raise B0ChapterSummaryError("capsule log chapter sequence is invalid")
    for capsule in capsules:
        if (
            capsule.get("authority") != "orientation_only"
            or not isinstance(capsule.get("text"), str)
            or not capsule["text"].strip()
            or not isinstance(capsule.get("summary_artifact_hash"), str)
            or len(capsule["summary_artifact_hash"]) != 64
        ):
            raise B0ChapterSummaryError("capsule log contains a malformed capsule")
    return deepcopy(dict(log))


def append_capsule_log_v1(
    *,
    artifact: Mapping[str, Any],
    prior_log: Mapping[str, Any] | None,
) -> dict[str, Any]:
    current = verify_b0_summary_artifact_v1(artifact)
    chapter_order = current["chapter_order"]
    if prior_log is None:
        if chapter_order != 1:
            raise B0ChapterSummaryError(
                "chapter summary after chapter 1 requires the prior capsule log"
            )
        return build_capsule_log_v1([current])

    prior = verify_capsule_log_v1(prior_log)
    capsules = deepcopy(list(prior["capsules"]))
    if chapter_order != len(capsules) + 1:
        raise B0ChapterSummaryError("prior capsule log does not immediately precede chapter")
    if any(row["chapter_id"] == current["chapter_id"] for row in capsules):
        raise B0ChapterSummaryError("capsule log repeats the current chapter")
    capsules.append(_capsule_log_row_v1(current))
    body = {
        "schema_version": CAPSULE_LOG_SCHEMA_VERSION,
        "capsules": capsules,
        "authority": "orientation_only",
        "append_only": True,
    }
    return {**body, "capsule_log_hash": canonical_hash(body)}


def verify_b0_summary_context_v1(packet: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(packet))
    observed = row.pop("packet_hash", None)
    if observed != canonical_hash(row):
        raise B0ChapterSummaryError("B0 context packet hash mismatch")
    if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise B0ChapterSummaryError("foreign B0 context packet")
    metadata = packet.get("chapter_metadata")
    if not isinstance(metadata, Mapping) or set(metadata) != {"chapter_id", "chapter_order"}:
        raise B0ChapterSummaryError("B0 model-visible metadata differs")
    if packet.get("authority_policy") != {
        "summary_authority": "orientation_only",
        "pending_authoritative": False,
        "may_create_semantic_records": False,
        "may_use_outside_knowledge": False,
    }:
        raise B0ChapterSummaryError("B0 authority policy differs")
    if packet.get("production_publish_performed") is not False:
        raise B0ChapterSummaryError("B0 context claims publication")
    if packet.get("b2_review_projection_status") not in {
        "not_required",
        "speaker_recovery_applied",
    }:
        raise B0ChapterSummaryError("B0 effective B2 review status differs")
    return deepcopy(dict(packet))


def verify_b0_summary_artifact_v1(artifact: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(artifact))
    observed = row.pop("artifact_hash", None)
    if observed != canonical_hash(row):
        raise B0ChapterSummaryError("B0 summary artifact hash mismatch")
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise B0ChapterSummaryError("foreign B0 summary artifact")
    if artifact.get("production_publish_performed") is not False:
        raise B0ChapterSummaryError("B0 summary artifact claims publication")
    if artifact.get("summary", {}).get("authority") != "orientation_only":
        raise B0ChapterSummaryError("B0 summary claims semantic authority")
    return deepcopy(dict(artifact))


def synthetic_b0_context_v1(
    *, include_b3: bool = True, b2_speaker_review_status: str | None = None
) -> dict[str, Any]:
    chapter = {
        "chapter_id": "probe_chapter",
        "blocks": [
            {"block_id": "probe_b001", "order_index": 1, "clean_text": "Mara entered North House."},
            {"block_id": "probe_b002", "order_index": 2, "clean_text": "She agreed to stay."},
        ],
    }
    def card(
        *, entity_id: str, surface: str, kind: str, summary: str, block_id: str
    ) -> dict[str, Any]:
        return {
            "entity_id": entity_id,
            "canonical_surface": surface,
            "stable_surfaces": [surface],
            "referent_kind": {
                "value": kind,
                "basis": "explicit_textual",
                "semantic_status": "unreviewed",
                "effective": True,
            },
            "record_class": "named_entity_candidate",
            "record_state": "chapter_confirmed",
            "first_seen": {
                "chapter_id": "probe_chapter",
                "block_id": block_id,
                "order_index": 1,
            },
            "source_refs": [f"scan:{entity_id}"],
            "support_block_ids": [block_id],
            "presence_history": [
                {
                    "chapter_id": "probe_chapter",
                    "presence_basis": "physically_present",
                    "semantic_status": "observed",
                    "source_block_ids": [block_id],
                }
            ],
            "claims": [],
            "aliases": [],
            "address_forms_used": [],
            "identity_summary": {
                "text": summary,
                "authority_scope": "chapter_provisional",
                "semantic_status": "unreviewed",
            },
            "distinguishing_note": None,
            "chapter_authority": True,
            "identity_authority": False,
            "book_authority": False,
        }

    cards = [
        card(
            entity_id="ent_mara",
            surface="Mara",
            kind="person",
            summary="Mara is the arriving visitor.",
            block_id="probe_b001",
        ),
        card(
            entity_id="ent_house",
            surface="North House",
            kind="place",
            summary="North House is the visited dwelling.",
            block_id="probe_b001",
        ),
    ]
    prior_cards = [
        {
            "prior_card_id": row["entity_id"],
            "canonical_surface": row["canonical_surface"],
            "stable_surfaces": row["stable_surfaces"],
            "referent_kind": row["referent_kind"]["value"],
            "identity_summary": row["identity_summary"]["text"],
            "record_class": "confirmed_entity",
            "presence_basis": row["presence_history"][-1]["presence_basis"],
            "claim_state": "confirmed",
            "first_supported_block_id": row["first_seen"]["block_id"],
            "provenance_refs": [
                {
                    "chapter_id": "probe_chapter",
                    "block_id": block_id,
                }
                for block_id in row["support_block_ids"]
            ],
        }
        for row in sorted(cards, key=lambda item: item["entity_id"])
    ]
    registry_body = {
        "schema_version": "literary_b1_chapter_registry_v1",
        "chapter_id": "probe_chapter",
        "lineage": {},
        "cards": cards,
        "relation_edges": [],
        "glossary_entries": [],
        "prior_cards_projection": {"cards": prior_cards},
        "dormant_observations": [],
        "pending_reviews": [],
        "diagnostics": [],
        "curation_log": {},
        "id_alias_table": [],
        "chapter_authority_granted": True,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
        "metrics": {},
    }
    registry = {**registry_body, "registry_hash": canonical_hash(registry_body)}
    b2_body = {
        "schema_version": "probe_b2",
        "chapter_id": "probe_chapter",
        "frame_segments": [
            {
                "frame_segment_id": "frame_1",
                "start_block_id": "probe_b001",
                "end_block_id": "probe_b002",
                "narrator_surface": "Mara",
                "narrator_status": "resolved_candidate",
                "candidate_card_ids": ["ent_mara"],
                "narrative_mode": "direct_current",
            }
        ],
        "salient_events": [
            {
                "salient_event_id": "event_1",
                "summary": "Mara agrees to stay at North House.",
                "event_kind": "commitment_or_separation",
                "event_status": "occurred",
                "review_status": "resolved",
                "source_block_ids": ["probe_b002"],
                "participants": [],
            }
        ],
        "review_requests": (
            [
                {
                    "review_id": "review_speaker_1",
                    "review_kind": "speaker_attribution",
                    "origin_window_id": "probe_window",
                    "source_block_ids": ["probe_b002"],
                    "candidate_card_ids": ["ent_mara"],
                    "reason": "Speaker remains uncertain.",
                    "origin": "model",
                    "status": "pending",
                    "blocking_kind": "scene_ambiguity",
                    "competing_card_ids": [],
                }
            ]
            if b2_speaker_review_status is not None
            else []
        ),
    }
    b2 = {**b2_body, "artifact_hash": canonical_hash(b2_body)}
    b2_review_projection = None
    if b2_speaker_review_status in {"pending", "resolved"}:
        projection_body = {
            "schema_version": EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION,
            "chapter_id": "probe_chapter",
            "source_b2_artifact_hash": b2["artifact_hash"],
            "source_speaker_recovery_artifact_hash": "f" * 64,
            "effective_review_requests": (
                deepcopy(b2["review_requests"])
                if b2_speaker_review_status == "pending"
                else []
            ),
            "resolved_review_ids": (
                ["review_speaker_1"]
                if b2_speaker_review_status == "resolved"
                else []
            ),
            "unresolved_ambiguous_review_ids": [],
            "terminal_route_a_review_ids": (
                ["review_speaker_1"]
                if b2_speaker_review_status == "resolved"
                else []
            ),
            "speaker_overlay_count": 1,
            "addressee_overlay_count": 0,
        }
        b2_review_projection = {
            **projection_body,
            "projection_hash": canonical_hash(projection_body),
        }
    b3 = None
    if include_b3:
        b3_body = {
            "schema_version": "probe_b3",
            "chapter_id": "probe_chapter",
            "effective_state_projection": [
                {
                    "state_id": "state_1",
                    "state_domain": "residence",
                    "state_value": "stays at North House",
                    "subject_referent_refs": ["ent_mara"],
                    "counterpart_referent_refs": ["ent_house"],
                    "authority_status": "effective",
                    "lifecycle_status": "open",
                    "source_block_ids": ["probe_b002"],
                }
            ],
            "new_state_rows": [],
            "pending_cases": [],
        }
        b3 = {**b3_body, "artifact_hash": canonical_hash(b3_body)}
    return build_b0_summary_context_v1(
        chapter=chapter,
        chapter_order=1,
        b1_registry=registry,
        b2_artifact=b2,
        b2_effective_review_projection=b2_review_projection,
        b3_artifact=b3,
    )


def synthetic_b0_response_v1(packet: Mapping[str, Any]) -> dict[str, Any]:
    verified = verify_b0_summary_context_v1(packet)
    states = [row["state_id"] for row in verified["b3_effective_states"]]
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "chapter_id": verified["chapter_metadata"]["chapter_id"],
        "chapter_summary": "Mara arrives at North House and agrees to remain there. The chapter establishes her arrival and immediate position in the dwelling.",
        "narrative_handoff": {
            "frame_summary": "The chapter follows Mara in a direct current frame.",
            "ending_position": "Mara has agreed to stay at North House.",
            "frame_refs": ["frame_1"],
            "entities_mentioned": ["Mara", "North House"],
            "locations_mentioned": ["North House"],
        },
        "salient_event_refs": ["event_1"],
        "effective_state_refs": states,
        "unresolved_case_refs": [],
        "capsule": {
            "text": "Mara arrives at North House and agrees to stay.",
            "entity_refs": ["ent_mara", "ent_house"],
            "event_refs": ["event_1"],
            "state_refs": states,
        },
    }


def _compact_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    claims = [
        {"field": row.get("field"), "value": row.get("value")}
        for row in raw.get("claims") or []
        if row.get("effective") is True
    ]
    kind = raw.get("referent_kind") or {}
    summary = raw.get("identity_summary") or {}
    return {
        "entity_id": _text(raw.get("entity_id"), "entity_id"),
        "canonical_surface": _text(raw.get("canonical_surface"), "canonical_surface"),
        "referent_kind": kind.get("value"),
        "referent_kind_effective": kind.get("effective") is True,
        "identity_summary": summary.get("text"),
        "record_state": raw.get("record_state"),
        "effective_claims": claims,
    }


def _compact_frame(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "frame_segment_id": _text(raw.get("frame_segment_id"), "frame_segment_id"),
        "start_block_id": raw.get("start_block_id"),
        "end_block_id": raw.get("end_block_id"),
        "narrator_surface": raw.get("narrator_surface"),
        "narrator_status": raw.get("narrator_status"),
        "candidate_card_ids": list(raw.get("candidate_card_ids") or []),
        "narrative_mode": raw.get("narrative_mode"),
    }


def _compact_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "salient_event_id": _text(raw.get("salient_event_id"), "salient_event_id"),
        "summary": raw.get("summary"),
        "event_kind": raw.get("event_kind"),
        "event_status": raw.get("event_status"),
        "review_status": raw.get("review_status"),
        "source_block_ids": list(raw.get("source_block_ids") or []),
        "participants": deepcopy(list(raw.get("participants") or [])),
    }


def _compact_state(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state_id": _text(raw.get("state_id"), "state_id"),
        "state_domain": raw.get("state_domain"),
        "state_value": raw.get("state_value"),
        "subject_referent_refs": list(raw.get("subject_referent_refs") or []),
        "counterpart_referent_refs": list(raw.get("counterpart_referent_refs") or []),
        "authority_status": raw.get("authority_status"),
        "lifecycle_status": raw.get("lifecycle_status"),
        "source_block_ids": list(raw.get("source_block_ids") or []),
    }


def _compact_cases(rows: Sequence[Mapping[str, Any]], origin: str) -> list[dict[str, Any]]:
    result = []
    for raw in rows:
        case_id = _case_id(raw, origin)
        result.append(
            {
                "case_id": case_id,
                "origin_stage": origin,
                "kind": raw.get("review_kind") or raw.get("review_route") or "review",
                "reason": raw.get("reason"),
                "authority": "non_authoritative",
            }
        )
    return result


def collapse_unresolved_cases_v1(
    *,
    b1_cases: Sequence[Mapping[str, Any]],
    b2_cases: Sequence[Mapping[str, Any]],
    b3_cases: Sequence[Mapping[str, Any]],
    parked_identity_index: Mapping[str, Any],
) -> list[dict[str, Any]]:
    try:
        index = _verify_parked_identity_index_any_v1(parked_identity_index)
    except B3ParkedIdentityError as exc:
        raise B0ChapterSummaryError("B0 parked identity index is invalid") from exc
    by_component = {
        row["hearing_component_id"]: deepcopy(dict(row))
        for row in index["parked_identities"]
    }
    for raw in b3_cases:
        if not isinstance(raw, Mapping):
            raise B0ChapterSummaryError("B0 unresolved case must be an object")
        for inherited in _inherited_parked_markers_v1(raw):
            if raw.get("review_route") != "inherited_identity_block":
                raise B0ChapterSummaryError(
                    "B3 inherited parked identity is malformed"
                )
            component_id = _text(
                inherited.get("hearing_component_id"),
                "B3 inherited hearing_component_id",
            )
            resolution = _text(
                inherited.get("resolution_condition"),
                "B3 inherited resolution_condition",
            )
            existing = by_component.get(component_id)
            if existing is not None:
                if resolution != existing["resolution_condition"]:
                    raise B0ChapterSummaryError(
                        "B3 inherited parked identity differs"
                    )
                continue
            by_component[component_id] = {
                "hearing_component_id": component_id,
                "resolution_condition": resolution,
                "card_ids": [],
            }
    grouped: dict[str, list[str]] = {}
    retained: list[dict[str, Any]] = []
    for origin, rows in (("b1", b1_cases), ("b2", b2_cases), ("b3", b3_cases)):
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise B0ChapterSummaryError("B0 unresolved case must be an object")
            component_ids = _parked_components_for_case(
                origin=origin,
                raw=raw,
                index=index,
                by_component=by_component,
            )
            if not component_ids:
                retained.extend(_compact_cases([raw], origin))
                continue
            for component_id in component_ids:
                grouped.setdefault(component_id, []).append(_case_id(raw, origin))
    for component_id, collapsed_ids in grouped.items():
        parked = by_component[component_id]
        retained.append(
            {
                "case_id": component_id,
                "origin_stage": "identity_hearing",
                "kind": "parked_identity",
                "reason": parked["resolution_condition"],
                "resolution_condition": parked["resolution_condition"],
                "collapsed_case_count": len(set(collapsed_ids)),
                "authority": "non_authoritative",
            }
        )
    return retained


def _parked_components_for_case(
    *,
    origin: str,
    raw: Mapping[str, Any],
    index: Mapping[str, Any],
    by_component: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    inherited_markers = _inherited_parked_markers_v1(raw)
    if inherited_markers:
        component_ids: list[str] = []
        for inherited in inherited_markers:
            component_id = inherited.get("hearing_component_id")
            resolution = inherited.get("resolution_condition")
            parked = by_component.get(component_id)
            if parked is None or resolution != parked["resolution_condition"]:
                raise B0ChapterSummaryError("B3 inherited parked identity differs")
            component_ids.append(str(component_id))
        if component_ids != sorted(set(component_ids)):
            raise B0ChapterSummaryError(
                "B3 inherited parked identities are not canonical"
            )
        return component_ids
    if origin == "b1" and raw.get("row_type") != "cross_chapter_identity_linkage":
        return []
    if origin == "b2" and not (
        raw.get("review_kind") == "addressee_identity"
        and raw.get("blocking_kind") == "unresolved_entity"
    ):
        return []
    if origin == "b3":
        return []
    card_ids = _structured_case_card_ids(raw)
    try:
        if index.get("schema_version") == PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2:
            matches = parked_hearings_for_card_ids_v2(
                index=index,
                card_ids=card_ids,
            )
            return (
                [str(matches[0]["hearing_component_id"])]
                if len(matches) == 1
                else []
            )
        parked = parked_hearing_for_card_ids_v1(index=index, card_ids=card_ids)
    except B3ParkedIdentityError as exc:
        raise B0ChapterSummaryError("B0 unresolved case maps ambiguously") from exc
    return [] if parked is None else [str(parked["hearing_component_id"])]


def _verify_parked_identity_index_any_v1(
    index: Mapping[str, Any],
) -> dict[str, Any]:
    if index.get("schema_version") == PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2:
        return verify_parked_identity_index_v2(index)
    return verify_parked_identity_index_v1(index)


def _inherited_parked_markers_v1(
    raw: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    singular = raw.get("inherited_parked_identity")
    plural = raw.get("inherited_parked_identities")
    if singular is not None and plural is not None:
        raise B0ChapterSummaryError(
            "B3 inherited parked identity carries both shapes"
        )
    if plural is not None:
        if (
            not isinstance(plural, list)
            or not all(isinstance(value, Mapping) for value in plural)
        ):
            raise B0ChapterSummaryError(
                "B3 inherited parked identities are malformed"
            )
        return list(plural)
    if singular is None:
        return []
    if not isinstance(singular, Mapping):
        raise B0ChapterSummaryError("B3 inherited parked identity is malformed")
    return [singular]


def _structured_case_card_ids(raw: Mapping[str, Any]) -> list[str]:
    fields = (
        "prior_card_id",
        "prior_card_ids",
        "candidate_card_id",
        "candidate_card_ids",
        "competing_card_ids",
        "current_entity_id",
        "current_entity_ids",
    )
    result: set[str] = set()
    for field in fields:
        value = raw.get(field)
        values = value if isinstance(value, list) else [value]
        result.update(
            item
            for item in values
            if isinstance(item, str) and item.startswith("b0ent_")
        )
    return sorted(result)


def _case_id(raw: Mapping[str, Any], origin: str) -> str:
    return next(
        (
            str(raw[key])
            for key in (
                "pending_case_id",
                "review_id",
                "ticket_id",
                "component_id",
                "continuity_case_id",
            )
            if isinstance(raw.get(key), str) and raw.get(key)
        ),
        f"{origin}_case_{canonical_hash(raw)[:16]}",
    )


def _chapter_blocks(chapter: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = []
    for raw in chapter.get("blocks") or []:
        block_id = _text(raw.get("block_id"), "block_id")
        text = raw.get("clean_text") if isinstance(raw.get("clean_text"), str) else raw.get("text")
        rows.append({"block_id": block_id, "text": _text(text, "block text")})
    if not rows:
        raise B0ChapterSummaryError("chapter has no source blocks")
    return rows


def _relevant_alias_rows(rows: Sequence[Any], card_ids: set[str]) -> list[dict[str, str]]:
    result = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        alias = raw.get("alias_entity_id") or raw.get("superseded_entity_id") or raw.get("from_entity_id")
        canonical = raw.get("canonical_entity_id") or raw.get("to_entity_id")
        if isinstance(alias, str) and isinstance(canonical, str) and canonical in card_ids:
            result.append({"alias_entity_id": alias, "canonical_entity_id": canonical})
    return sorted(result, key=lambda row: row["alias_entity_id"])


def _known_refs(packet: Mapping[str, Any]) -> dict[str, set[str]]:
    return {
        "entity": {row["entity_id"] for row in packet["b1_entity_roster"]},
        "frame": {row["frame_segment_id"] for row in packet["b2_narrative_frames"]},
        "event": {row["salient_event_id"] for row in packet["b2_salient_events"]},
        "state": {row["state_id"] for row in packet["b3_effective_states"]},
        "case": {row["case_id"] for row in packet["unresolved_cases"]},
    }


def _surface_by_model_ref_v1(
    *,
    packet: Mapping[str, Any],
    rendered: RenderedB0SummaryRequestV1,
) -> dict[str, str]:
    surfaces: dict[str, str] = {}
    for card in packet["b1_entity_roster"]:
        entity_id = card.get("entity_id")
        surface = card.get("canonical_surface")
        if isinstance(entity_id, str) and isinstance(surface, str) and surface.strip():
            surfaces[entity_id] = surface.strip()
    for frame in packet["b2_narrative_frames"]:
        candidate_ids = frame.get("candidate_card_ids")
        surface = frame.get("narrator_surface")
        if (
            isinstance(candidate_ids, list)
            and len(candidate_ids) == 1
            and isinstance(candidate_ids[0], str)
            and isinstance(surface, str)
            and surface.strip()
        ):
            surfaces.setdefault(candidate_ids[0], surface.strip())

    _, ref_map = project_model_request_v1(
        shared_b0_summary_request_v1(rendered),
        instruction=model_ref_instruction_v1(),
    )
    return {
        row["local_ref"]: surfaces[row["persistent_id"]]
        for row in ref_map["entries"]
        if row["namespace"] == "entity" and row["persistent_id"] in surfaces
    }


def _normalize_handoff_mentions_v1(
    *,
    field: str,
    values: Sequence[str],
    surface_by_model_ref: Mapping[str, str] | None,
) -> tuple[list[str], int]:
    accepted: list[str] = []
    labels_resolved = 0
    for raw in values:
        value = raw.strip()
        if surface_by_model_ref is not None and _ENTITY_TRANSPORT_LABEL_RE.fullmatch(
            value
        ):
            if value not in surface_by_model_ref:
                raise B0ChapterSummaryError(
                    f"{field} contains an unknown entity transport label"
                )
            value = surface_by_model_ref[value]
            labels_resolved += 1
        if value not in accepted:
            accepted.append(value)
    return accepted, labels_resolved


def _capsule_log_row_v1(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **deepcopy(dict(artifact["capsule"])),
        "summary_artifact_hash": artifact["artifact_hash"],
        "chapter_order": artifact["chapter_order"],
    }


def _alias_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {row["alias_entity_id"]: row["canonical_entity_id"] for row in rows}


def _budget_issues(response: Mapping[str, Any]) -> list[str]:
    issues = []
    words = len(response["chapter_summary"].split())
    if words < 200:
        issues.append("chapter_summary_under_target")
    if words > 400:
        issues.append("chapter_summary_over_target")
    capsule_tokens = max(1, math.ceil(len(response["capsule"]["text"]) / 4))
    if capsule_tokens > 60:
        issues.append("capsule_over_budget")
    prose = " ".join(
        (
            response["chapter_summary"],
            response["narrative_handoff"]["frame_summary"],
            response["narrative_handoff"]["ending_position"],
            response["capsule"]["text"],
        )
    )
    if math.ceil(len(prose) / 4) > 700:
        issues.append("summary_total_over_budget")
    return issues


def _normalized_semantic_response(
    packet: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    row = deepcopy(dict(response))
    observed = row.pop("semantic_hash", None)
    if observed == canonical_hash(row) and row.get("packet_hash") == packet["packet_hash"]:
        return deepcopy(dict(response))
    return validate_b0_summary_response_v1(packet=packet, response=response)


def _verify_hashed_object(value: Mapping[str, Any], field: str, label: str) -> None:
    body = deepcopy(dict(value))
    observed = body.pop(field, None)
    if observed != canonical_hash(body):
        raise B0ChapterSummaryError(f"{label} hash mismatch")


def _parsed_object(value: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise B0ChapterSummaryError("B0 summary response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise B0ChapterSummaryError("B0 summary response must be an object")
    return parsed


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B0ChapterSummaryError(f"{label} must be non-empty text")
    return value


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "CAPSULE_LOG_SCHEMA_VERSION",
    "PACKET_SCHEMA_VERSION",
    "PROMPT_ID",
    "RESPONSE_SCHEMA_VERSION",
    "ROLE_ID",
    "SYSTEM_PROMPT",
    "B0ChapterSummaryError",
    "RenderedB0SummaryRequestV1",
    "b0_summary_response_schema_v1",
    "b2_speaker_recovery_candidate_scope_v1",
    "build_b0_summary_artifact_v1",
    "build_b0_summary_context_v1",
    "collapse_unresolved_cases_v1",
    "append_capsule_log_v1",
    "build_capsule_log_v1",
    "make_b0_summary_semantic_validator_v1",
    "render_b0_summary_request_v1",
    "shared_b0_summary_request_v1",
    "synthetic_b0_context_v1",
    "synthetic_b0_response_v1",
    "validate_b0_summary_response_v1",
    "verify_b0_summary_artifact_v1",
    "verify_capsule_log_v1",
    "verify_b0_summary_context_v1",
]
