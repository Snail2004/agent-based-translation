"""Ticket-only speaker recovery for the Literary B2 Slim contract."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.literary.b2_live_canary_v1 import SLIM_CHAPTER_ARTIFACT_SCHEMA_VERSION
from pipeline.literary.b2_prompts_v3 import B2_SLIM_INTERACTION_PROMPT_ID_V11
from pipeline.literary.b2_review_routing_v1 import (
    ReviewRoutingError,
    route_review,
)
from pipeline.literary.b2_recovery_batch_v1 import (
    batch_request_payload_v1,
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    RECOVERY_INDEX_SCHEMA_VERSION,
    RECOVERY_VALIDATOR_VERSION,
    RenderedB2RecoveryRequestV1,
    build_registry_recovery_ledger_v1,
    validate_registry_recovery_component_quarantines_v1,
    verify_b2_recovery_index_v1,
    verify_registry_recovery_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


ARTIFACT_SCHEMA_VERSION = "literary_b2_slim_speaker_recovery_artifact_v1"
EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION = (
    "literary_b2_effective_review_projection_v1"
)
MAX_TICKETS_PER_COMPONENT = 12
MAX_SOURCE_BLOCKS_PER_COMPONENT = 160
MAX_CANDIDATE_CARDS_PER_COMPONENT = 256
ROUTE_A_ENDPOINT_ROLES = {
    "speaker_attribution": "speaker",
    "addressee_identity": "addressee",
}
UNSUPPORTED_ROUTE_A_REVIEW_KIND = "unsupported_route_a_review_kind"
ROUTE_A_REVIEW_SPANS_MULTIPLE_FRAMES = (
    "route_a_review_spans_multiple_frame_segments"
)


class B2SlimSpeakerRecoveryError(RuntimeError):
    pass


def _route_a_endpoint_role_v1(review: Mapping[str, Any]) -> str | None:
    review_kind = review.get("review_kind")
    return (
        ROUTE_A_ENDPOINT_ROLES.get(review_kind)
        if isinstance(review_kind, str)
        else None
    )


def _held_route_a_review_v1(
    review: Mapping[str, Any],
    *,
    hold_reason: str = UNSUPPORTED_ROUTE_A_REVIEW_KIND,
) -> dict[str, Any]:
    source_review = deepcopy(dict(review))
    body = {
        "review_id": _required_string(source_review.get("review_id"), "review_id"),
        "review_kind": _required_string(
            source_review.get("review_kind"), "review_kind"
        ),
        "blocking_kind": _required_string(
            source_review.get("blocking_kind"), "blocking_kind"
        ),
        "hold_reason": hold_reason,
        "state": "pending_unserviceable",
        "source_review": source_review,
    }
    return {"hold_id": _mint_id("b2srhold1", body), **body}


def _expected_held_route_a_reviews_v1(
    chapter: Mapping[str, Any],
) -> list[dict[str, Any]]:
    held: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()
    frame_by_block = _chapter_frame_by_block_v1(chapter)
    for raw_review in chapter.get("review_requests") or []:
        if not isinstance(raw_review, Mapping) or raw_review.get("status") != "pending":
            continue
        review = deepcopy(dict(raw_review))
        review_id = _required_string(review.get("review_id"), "review_id")
        if review_id in seen_review_ids:
            raise B2SlimSpeakerRecoveryError("B2 review request repeats")
        seen_review_ids.add(review_id)
        try:
            destination = route_review(review)
        except (KeyError, ReviewRoutingError) as exc:
            raise B2SlimSpeakerRecoveryError(
                "B2 review has no valid typed route"
            ) from exc
        if destination == "A":
            hold_reason = _route_a_hold_reason_v1(
                review,
                frame_by_block=frame_by_block,
            )
            if hold_reason is not None:
                held.append(
                    _held_route_a_review_v1(review, hold_reason=hold_reason)
                )
    return sorted(held, key=lambda row: row["review_id"])


def _verify_held_route_a_reviews_v1(
    *,
    chapter: Mapping[str, Any],
    rows: Any,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise B2SlimSpeakerRecoveryError("held route-A reviews are malformed")
    observed = [deepcopy(dict(row)) for row in rows]
    expected = _expected_held_route_a_reviews_v1(chapter)
    if observed != expected:
        raise B2SlimSpeakerRecoveryError("held route-A review set differs")
    return observed


def load_b2_slim_speaker_source_v1(
    b2_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(b2_root).resolve()
    chapter = _read_object(root / "chapter_b2_artifact.json", "B2 chapter artifact")
    chapter = _verified_hash(chapter, "artifact_hash", "B2 chapter artifact")
    manifests = chapter.get("interaction_artifacts")
    if not isinstance(manifests, list) or not manifests:
        raise B2SlimSpeakerRecoveryError("B2 chapter has no interaction artifacts")

    requests: list[dict[str, Any]] = []
    observed_windows: set[str] = set()
    for manifest in manifests:
        if not isinstance(manifest, Mapping):
            raise B2SlimSpeakerRecoveryError("interaction manifest must be an object")
        window_id = _required_string(manifest.get("window_id"), "window_id")
        matches = list((root / "interactions").glob(f"*_{window_id}"))
        if len(matches) != 1:
            raise B2SlimSpeakerRecoveryError(
                f"interaction directory is missing or repeated for {window_id}"
            )
        interaction = _verified_hash(
            _read_object(
                matches[0] / "interaction_artifact.json",
                f"interaction artifact {window_id}",
            ),
            "artifact_hash",
            f"interaction artifact {window_id}",
        )
        if interaction["artifact_hash"] != manifest.get("artifact_hash"):
            raise B2SlimSpeakerRecoveryError("interaction manifest hash mismatch")
        request = _verified_request(
            _read_object(matches[0] / "request.json", f"interaction request {window_id}")
        )
        if (
            request.get("window_id") != window_id
            or interaction.get("window_id") != window_id
            or interaction.get("request_fingerprint")
            != request.get("request_fingerprint")
        ):
            raise B2SlimSpeakerRecoveryError("interaction request lineage mismatch")
        requests.append(request)
        observed_windows.add(window_id)
    if len(observed_windows) != len(manifests):
        raise B2SlimSpeakerRecoveryError("interaction windows are repeated")
    return chapter, requests


def build_b2_slim_speaker_recovery_index_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    interaction_requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    chapter = _verified_hash(
        chapter_artifact, "artifact_hash", "B2 chapter artifact"
    )
    if chapter.get("schema_version") != SLIM_CHAPTER_ARTIFACT_SCHEMA_VERSION:
        raise B2SlimSpeakerRecoveryError("foreign B2 chapter artifact schema")
    if chapter.get("identity_or_claim_mutation_performed") is not False:
        raise B2SlimSpeakerRecoveryError("B2 source mutated identity or claims")
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    context = _request_context(interaction_requests, chapter_id=chapter_id)
    expected_windows = {
        _required_string(row.get("window_id"), "artifact window_id")
        for row in chapter.get("interaction_artifacts") or []
    }
    if expected_windows != set(context["window_ids"]):
        raise B2SlimSpeakerRecoveryError(
            "interaction requests do not exact-cover artifact windows"
        )

    frames, frame_by_block = _frame_catalog_v1(chapter=chapter, context=context)
    route_a_reviews: list[dict[str, Any]] = []
    held_route_a_reviews: list[dict[str, Any]] = []
    for raw_review in chapter.get("review_requests") or []:
        if not isinstance(raw_review, Mapping) or raw_review.get("status") != "pending":
            continue
        review = deepcopy(dict(raw_review))
        try:
            destination = route_review(review)
        except (KeyError, ReviewRoutingError) as exc:
            raise B2SlimSpeakerRecoveryError(
                "B2 review has no valid typed route"
            ) from exc
        if destination == "E":
            raise B2SlimSpeakerRecoveryError(
                "frame-structure review reached Speaker Recovery"
            )
        if destination == "A":
            hold_reason = _route_a_hold_reason_v1(
                review,
                frame_by_block=frame_by_block,
            )
            if hold_reason is not None:
                held_route_a_reviews.append(
                    _held_route_a_review_v1(review, hold_reason=hold_reason)
                )
            else:
                route_a_reviews.append(review)

    turns = [
        deepcopy(dict(row))
        for row in chapter.get("speaker_turns") or []
        if isinstance(row, Mapping)
    ]
    ticket_seeds: dict[tuple[str, str], dict[str, Any]] = {}
    consumed_review_ids: set[str] = set()
    for review in sorted(route_a_reviews, key=lambda row: str(row.get("review_id"))):
        review_id = _required_string(review.get("review_id"), "review_id")
        block_ids = [
            _required_string(value, "review block_id")
            for value in review.get("source_block_ids") or []
        ]
        if not block_ids or any(block_id not in frame_by_block for block_id in block_ids):
            raise B2SlimSpeakerRecoveryError("route-A review cites missing blocks")
        frame_ids = {frame_by_block[block_id] for block_id in block_ids}
        if len(frame_ids) != 1:
            raise B2SlimSpeakerRecoveryError(
                "route-A review spans multiple frame segments"
            )
        frame_id = next(iter(frame_ids))
        endpoint_role = _route_a_endpoint_role_v1(review)
        if endpoint_role is None:
            raise B2SlimSpeakerRecoveryError(
                "serviceable route-A review lost its endpoint role"
            )
        matching_turns = [
            turn for turn in turns if turn.get("block_id") in set(block_ids)
        ]
        if not matching_turns:
            raise B2SlimSpeakerRecoveryError(
                "route-A review has no source turn"
            )
        signal = _review_signal(review)
        consumed_review_ids.add(review_id)
        for turn in matching_turns:
            endpoint = turn.get(endpoint_role)
            if not isinstance(endpoint, Mapping):
                raise B2SlimSpeakerRecoveryError(
                    f"route-A turn omits {endpoint_role} endpoint"
                )
            turn_id = _required_string(
                turn.get("speaker_turn_id"), "speaker_turn_id"
            )
            key = (turn_id, endpoint_role)
            seed = ticket_seeds.setdefault(
                key,
                {
                    "turn": deepcopy(turn),
                    "endpoint_role": endpoint_role,
                    "frame_segment_id": frame_id,
                    "candidate_card_ids": set(),
                    "review_signals": {},
                },
            )
            if (
                seed["turn"] != turn
                or seed["frame_segment_id"] != frame_id
                or seed["endpoint_role"] != endpoint_role
            ):
                raise B2SlimSpeakerRecoveryError(
                    "route-A recovery sees inconsistent turn snapshots"
                )
            frame_blocks = frames[frame_id]["covered_block_ids"]
            seed["candidate_card_ids"].update(
                candidate_id
                for block_id in frame_blocks
                for candidate_id in context["cards_by_block"].get(block_id, set())
            )
            seed["candidate_card_ids"].update(
                _required_string(value, "endpoint candidate id")
                for value in endpoint.get("candidate_card_ids") or []
            )
            seed["candidate_card_ids"].update(signal["candidate_card_ids"])
            seed["candidate_card_ids"].update(signal["competing_card_ids"])
            previous = seed["review_signals"].get(review_id)
            if previous is not None and previous != signal:
                raise B2SlimSpeakerRecoveryError(
                    "route-A recovery sees inconsistent review signals"
                )
            seed["review_signals"][review_id] = deepcopy(signal)

    tickets: list[dict[str, Any]] = []
    for (turn_id, endpoint_role), seed in sorted(
        ticket_seeds.items(),
        key=lambda item: (
            context["block_order"][item[1]["turn"]["block_id"]],
            item[0][0],
            item[0][1],
        ),
    ):
        turn = seed["turn"]
        endpoint = turn[endpoint_role]
        frame_id = seed["frame_segment_id"]
        review_signals = [
            seed["review_signals"][review_id]
            for review_id in sorted(seed["review_signals"])
        ]
        candidate_ids = sorted(seed["candidate_card_ids"])
        foreign = set(candidate_ids) - set(context["cards"])
        if foreign:
            raise B2SlimSpeakerRecoveryError(
                "speaker review references a foreign candidate card"
            )
        block_id = _required_string(turn.get("block_id"), "speaker turn block_id")
        anchor = _required_string(
            turn.get("utterance_anchor"), "speaker turn anchor"
        )
        evidence = {
            "source_turn": turn,
            "review_signals": review_signals,
            "source_text_sha256": hashlib.sha256(
                context["blocks"][block_id]["text"].encode("utf-8")
            ).hexdigest(),
        }
        body = {
            "chapter_id": chapter_id,
            "source_row_kind": "speaker_turn",
            "source_row_id": turn_id,
            "endpoint_role": endpoint_role,
            "observed_surface": endpoint.get("surface"),
            "reference_form": "unknown",
            "resolution_status": str(
                endpoint.get("resolution_status") or "unresolved"
            ),
            "candidate_card_ids": candidate_ids,
            "issue_kind": (
                "contextual_speaker_attribution"
                if endpoint_role == "speaker"
                else "contextual_addressee_identity"
            ),
            "source_anchor": anchor,
            "source_block_ids": [block_id],
            "source_window_id": frame_id,
            "source_frame_segment_id": frame_id,
            "source_review_signals": review_signals,
            "evidence_hash": canonical_hash(evidence),
            "lifecycle_state": "open",
            "hearing_count": 0,
            "authority_effect": "none",
        }
        tickets.append(
            {"ticket_id": _mint_id("b2slimgap1", body), **body}
        )
    if consumed_review_ids != {
        _required_string(row.get("review_id"), "review_id") for row in route_a_reviews
    }:
        raise B2SlimSpeakerRecoveryError("route-A reviews are not exact-covered")

    tickets.sort(
        key=lambda row: (
            context["block_order"][row["source_block_ids"][0]],
            row["source_row_id"],
        )
    )
    components = _components(
        tickets, context=context, frames=frames
    )
    body = {
        "schema_version": RECOVERY_INDEX_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "chapter_id": chapter_id,
        "source_b2_artifact_hash": chapter["artifact_hash"],
        "source_request_fingerprints": sorted(context["request_fingerprints"]),
        "source_blocks": [
            deepcopy(context["blocks"][block_id])
            for block_id in context["ordered_block_ids"]
        ],
        "candidate_cards": [
            deepcopy(context["cards"][card_id]) for card_id in sorted(context["cards"])
        ],
        "held_route_a_reviews": sorted(
            held_route_a_reviews, key=lambda row: row["review_id"]
        ),
        "registry_gap_tickets": tickets,
        "event_review_cases": [],
        "registry_components": components,
        "event_components": [],
        "counts": {
            "registry_gap_tickets": len(tickets),
            "event_review_cases": 0,
            "registry_components": len(components),
            "event_components": 0,
            "overflow_components": sum(bool(row["overflow"]) for row in components),
            "held_route_a_reviews": len(held_route_a_reviews),
        },
        "semantic_halt_required": False,
        "book_global_identity_mutation_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
        "slim_speaker_policy": {
            "trigger": "typed_route_a_turn_endpoint_or_typed_hold",
            "accepted_turn_reinspection": False,
            "unticketed_turn_mutation": False,
            "frame_evidence_contiguous": True,
            "unsupported_review_kind_inference": False,
        },
    }
    index = {**body, "recovery_index_hash": canonical_hash(body)}
    verify_b2_recovery_index_v1(index)
    _verify_held_route_a_reviews_v1(
        chapter=chapter,
        rows=index["held_route_a_reviews"],
    )
    return index


def render_b2_slim_speaker_recovery_request_v1(
    index: Mapping[str, Any],
) -> RenderedB2RecoveryRequestV1 | None:
    verified = verify_b2_recovery_index_v1(index)
    component_ids = [
        row["component_id"]
        for row in verified["registry_components"]
        if not row["overflow"]
    ]
    if not component_ids:
        return None
    return render_registry_recovery_batch_request_v1(
        index=verified, component_ids=component_ids
    )


def make_b2_slim_speaker_recovery_validator_v1(
    *, index: Mapping[str, Any], request: RenderedB2RecoveryRequestV1
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    component_ids = [
        row["component_id"]
        for row in verify_b2_recovery_index_v1(index)["registry_components"]
        if not row["overflow"]
    ]

    def validate(response: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_registry_recovery_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            request_fingerprint=request.request_fingerprint,
        )

    return validate


def apply_b2_slim_speaker_recovery_decision_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    index: Mapping[str, Any],
    batch_decision: Mapping[str, Any],
) -> dict[str, Any]:
    chapter = _verified_hash(
        chapter_artifact, "artifact_hash", "B2 chapter artifact"
    )
    verified_index = verify_b2_recovery_index_v1(index)
    if chapter["artifact_hash"] != verified_index["source_b2_artifact_hash"]:
        raise B2SlimSpeakerRecoveryError("decision targets another B2 artifact")
    held_route_a_reviews = _verify_held_route_a_reviews_v1(
        chapter=chapter,
        rows=verified_index.get("held_route_a_reviews", []),
    )
    decision = _verified_hash(
        batch_decision, "batch_decision_hash", "speaker recovery decision"
    )
    if decision.get("recovery_index_hash") != verified_index["recovery_index_hash"]:
        raise B2SlimSpeakerRecoveryError("decision targets another recovery index")
    try:
        component_quarantines = (
            validate_registry_recovery_component_quarantines_v1(
                index=verified_index,
                quarantines=decision.get("quarantined_components") or [],
            )
        )
    except B2RecoveryContractError as exc:
        raise B2SlimSpeakerRecoveryError(
            "speaker recovery component quarantine is invalid"
        ) from exc
    quarantine_by_ticket = {
        action_row["ticket_id"]: {
            "reason": component["reason"],
            "action_hashes": list(action_row["action_hashes"]),
        }
        for component in component_quarantines
        for action_row in component["ticket_action_hashes"]
    }
    tickets = {
        row["ticket_id"]: row for row in verified_index["registry_gap_tickets"]
    }
    turns = {
        row["speaker_turn_id"]: deepcopy(dict(row))
        for row in chapter.get("speaker_turns") or []
    }
    cards = {
        row["candidate_card_id"]: row for row in verified_index["candidate_cards"]
    }
    actions = [
        deepcopy(dict(action))
        for component in decision.get("component_decisions") or []
        for action in component.get("ticket_actions") or []
        if isinstance(action, Mapping)
    ]
    registry_recovery_ledger = None
    if any(action.get("action") == "create_chapter_local" for action in actions):
        try:
            registry_recovery_ledger = build_registry_recovery_ledger_v1(
                index=verified_index,
                decisions=decision.get("component_decisions") or [],
                quarantined_components=component_quarantines,
            )
            registry_recovery_ledger = verify_registry_recovery_ledger_v1(
                registry_recovery_ledger,
                index=verified_index,
            )
        except B2RecoveryContractError as exc:
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery decision cannot build its registry ledger"
            ) from exc
    local_cards = {
        row["candidate_card_id"]: row
        for row in (
            registry_recovery_ledger["local_candidate_cards"]
            if registry_recovery_ledger is not None
            else []
        )
    }
    if set(cards).intersection(local_cards):
        raise B2SlimSpeakerRecoveryError(
            "speaker recovery local card collides with a supplied card"
        )
    all_cards = {**cards, **local_cards}
    recovery_resolutions = {
        row["ticket_id"]: row
        for row in (
            registry_recovery_ledger["ticket_resolutions"]
            if registry_recovery_ledger is not None
            else []
        )
    }
    observed_ticket_ids = [
        _required_string(row.get("ticket_id"), "ticket_id") for row in actions
    ]
    if (
        set(observed_ticket_ids).intersection(quarantine_by_ticket)
        or set(observed_ticket_ids).union(quarantine_by_ticket) != set(tickets)
    ):
        raise B2SlimSpeakerRecoveryError(
            "decision and quarantine do not exact-cover tickets"
        )

    actions_by_ticket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        actions_by_ticket[action["ticket_id"]].append(action)

    candidates_by_endpoint: dict[tuple[str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    quarantined_ticket_actions: list[dict[str, Any]] = []
    ticket_outcomes: dict[str, dict[str, str]] = {}
    for ticket_id in sorted(tickets):
        ticket = tickets[ticket_id]
        turn = turns.get(ticket["source_row_id"])
        if turn is None:
            raise B2SlimSpeakerRecoveryError("ticket source turn is missing")
        component_quarantine = quarantine_by_ticket.get(ticket_id)
        if component_quarantine is not None:
            quarantine = _quarantined_ticket_action_hashes_v1(
                ticket=ticket,
                action_hashes=component_quarantine["action_hashes"],
                reason=component_quarantine["reason"],
            )
            quarantined_ticket_actions.append(quarantine)
            ticket_outcomes[ticket_id] = {
                "status": "unresolved_ambiguous",
                "decision_action": "quarantined",
                "narrowed_candidate_card_ids": [],
                "model_reasons": [],
                "quarantine_reason": quarantine["reason"],
            }
            continue
        ticket_actions = actions_by_ticket[ticket_id]
        if len(ticket_actions) != 1:
            quarantine = _quarantined_ticket_action_v1(
                ticket=ticket,
                actions=ticket_actions,
                reason="speaker recovery decision repeats a ticket",
            )
            quarantined_ticket_actions.append(quarantine)
            ticket_outcomes[ticket_id] = {
                "status": "unresolved_ambiguous",
                "decision_action": "quarantined",
                "narrowed_candidate_card_ids": [],
                "model_reasons": [
                    str(row.get("resolution_note"))
                    for row in ticket_actions
                    if isinstance(row.get("resolution_note"), str)
                    and row["resolution_note"].strip()
                ],
                "quarantine_reason": quarantine["reason"],
            }
            continue
        action = ticket_actions[0]
        recovery_resolution = recovery_resolutions.get(ticket_id) or {
            "ticket_id": ticket_id,
            "source_row_id": ticket["source_row_id"],
            "endpoint_role": ticket["endpoint_role"],
            "action": action.get("action"),
            "bound_candidate_card_id": action.get("target_candidate_card_id"),
        }
        try:
            candidate = _endpoint_overlay_candidate_v1(
                ticket=ticket,
                action=action,
                turn=turn,
                cards=all_cards,
                local_candidate_card_ids=set(local_cards),
                recovery_resolution=recovery_resolution,
            )
        except B2SlimSpeakerRecoveryError as exc:
            quarantine = _quarantined_ticket_action_v1(
                ticket=ticket,
                actions=ticket_actions,
                reason=str(exc),
            )
            quarantined_ticket_actions.append(quarantine)
            ticket_outcomes[ticket_id] = {
                "status": "unresolved_ambiguous",
                "decision_action": "quarantined",
                "narrowed_candidate_card_ids": [],
                "model_reasons": [
                    str(action.get("resolution_note"))
                    if isinstance(action.get("resolution_note"), str)
                    and action["resolution_note"].strip()
                    else ""
                ],
                "quarantine_reason": quarantine["reason"],
            }
            continue
        candidates_by_endpoint[
            (ticket["source_row_id"], ticket["endpoint_role"])
        ].append(candidate)

    speaker_overlays: list[dict[str, Any]] = []
    addressee_overlays: list[dict[str, Any]] = []
    for turn_id, endpoint_role in sorted(candidates_by_endpoint):
        candidates = candidates_by_endpoint[(turn_id, endpoint_role)]
        signatures = {
            canonical_hash(
                {
                    "action": row["action"],
                    "endpoint_role": row["endpoint_role"],
                    "effective_endpoint": row["effective_endpoint"],
                    "authority_status": row["authority_status"],
                    "source_turn_snapshot_hash": row["source_turn_snapshot_hash"],
                    "original_endpoint": row["original_endpoint"],
                    "narrowed_candidate_card_ids": row[
                        "narrowed_candidate_card_ids"
                    ],
                }
            )
            for row in candidates
        }
        if len(signatures) != 1:
            for candidate in candidates:
                ticket_id = candidate["ticket_id"]
                ticket = tickets[ticket_id]
                quarantine = _quarantined_ticket_action_v1(
                    ticket=ticket,
                    actions=[candidate["ticket_action"]],
                    reason="speaker recovery decisions conflict for one turn",
                )
                quarantined_ticket_actions.append(quarantine)
                ticket_outcomes[ticket_id] = {
                    "status": "unresolved_ambiguous",
                    "decision_action": "quarantined",
                    "narrowed_candidate_card_ids": deepcopy(
                        candidate.get("narrowed_candidate_card_ids") or []
                    ),
                    "model_reasons": [
                        str(candidate.get("resolution_note"))
                        if isinstance(candidate.get("resolution_note"), str)
                        and candidate["resolution_note"].strip()
                        else ""
                    ],
                    "quarantine_reason": quarantine["reason"],
                }
            continue
        overlay = _collapse_endpoint_overlay_candidates_v1(candidates)
        (
            speaker_overlays
            if endpoint_role == "speaker"
            else addressee_overlays
        ).append(overlay)
        for ticket_id in overlay["ticket_ids"]:
            ticket_outcomes[ticket_id] = {
                "status": (
                    "resolved"
                    if overlay["action"]
                    in {"attach_existing", "create_chapter_local"}
                    else "unresolved_ambiguous"
                ),
                "decision_action": overlay["action"],
                "narrowed_candidate_card_ids": deepcopy(
                    overlay.get("narrowed_candidate_card_ids") or []
                ),
                "model_reasons": [
                    str(resolution.get("resolution_note"))
                    for resolution in overlay.get("ticket_resolutions") or []
                    if isinstance(resolution.get("resolution_note"), str)
                    and resolution["resolution_note"].strip()
                ],
                "quarantine_reason": "",
            }

    review_dispositions = _route_a_review_dispositions_v1(
        tickets=tickets,
        ticket_outcomes=ticket_outcomes,
    )
    ambiguity_records = _ambiguity_records_v1(
        tickets=tickets,
        ticket_outcomes=ticket_outcomes,
        dispositions=review_dispositions,
        index=verified_index,
        actions_by_ticket=actions_by_ticket,
    )
    body = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "chapter_id": chapter["chapter_id"],
        "source_b2_artifact_hash": chapter["artifact_hash"],
        "recovery_index_hash": verified_index["recovery_index_hash"],
        "batch_decision_hash": decision["batch_decision_hash"],
        "speaker_overlays": speaker_overlays,
        "addressee_overlays": addressee_overlays,
        "held_route_a_reviews": held_route_a_reviews,
        "unresolved_ambiguities": ambiguity_records,
        "review_dispositions": sorted(
            review_dispositions, key=lambda row: (row["review_id"], row["ticket_id"])
        ),
        "quarantined_ticket_actions": sorted(
            quarantined_ticket_actions,
            key=lambda row: (row["speaker_turn_id"], row["ticket_id"]),
        ),
        "ticketed_speaker_turn_ids": sorted(
            {row["source_row_id"] for row in tickets.values()}
        ),
        "accepted_turn_reinspection_performed": False,
        "unticketed_turn_mutation_performed": False,
        "source_artifact_mutated": False,
        "identity_or_claim_mutation_performed": False,
        "book_global_authority_granted": False,
        "translation_performed": False,
        "production_publish_performed": False,
    }
    if registry_recovery_ledger is not None:
        body["registry_recovery_ledger"] = registry_recovery_ledger
    artifact = {**body, "artifact_hash": canonical_hash(body)}
    return verify_b2_slim_speaker_recovery_artifact_v1(
        chapter_artifact=chapter,
        recovery_artifact=artifact,
        allowed_candidate_card_ids=set(cards),
        recovery_index=verified_index,
    )


def verify_b2_slim_speaker_recovery_artifact_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    recovery_artifact: Mapping[str, Any],
    allowed_candidate_card_ids: set[str],
    recovery_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify route-A endpoint overlays without changing the B2 source."""

    chapter = _verified_hash(
        chapter_artifact, "artifact_hash", "B2 chapter artifact"
    )
    if chapter.get("schema_version") != SLIM_CHAPTER_ARTIFACT_SCHEMA_VERSION:
        raise B2SlimSpeakerRecoveryError("foreign B2 chapter artifact schema")
    artifact = _verified_hash(
        recovery_artifact,
        "artifact_hash",
        "speaker recovery artifact",
    )
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise B2SlimSpeakerRecoveryError("foreign speaker recovery artifact schema")
    _required_string(artifact.get("recovery_index_hash"), "recovery_index_hash")
    _required_string(artifact.get("batch_decision_hash"), "batch_decision_hash")
    if (
        artifact.get("chapter_id") != chapter.get("chapter_id")
        or artifact.get("source_b2_artifact_hash") != chapter.get("artifact_hash")
    ):
        raise B2SlimSpeakerRecoveryError("speaker recovery source lineage differs")
    for field in (
        "accepted_turn_reinspection_performed",
        "unticketed_turn_mutation_performed",
        "source_artifact_mutated",
        "identity_or_claim_mutation_performed",
        "book_global_authority_granted",
        "translation_performed",
        "production_publish_performed",
    ):
        if artifact.get(field) is not False:
            raise B2SlimSpeakerRecoveryError(
                f"speaker recovery safety flag differs: {field}"
            )

    local_candidate_card_ids: set[str] = set()
    raw_registry_ledger = artifact.get("registry_recovery_ledger")
    if raw_registry_ledger is not None:
        if not isinstance(raw_registry_ledger, Mapping) or recovery_index is None:
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery registry ledger lacks its recovery index"
            )
        try:
            registry_ledger = verify_registry_recovery_ledger_v1(
                raw_registry_ledger,
                index=recovery_index,
            )
        except B2RecoveryContractError as exc:
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery registry ledger is invalid"
            ) from exc
        if (
            registry_ledger.get("source_b2_artifact_hash")
            != chapter.get("artifact_hash")
            or registry_ledger.get("recovery_index_hash")
            != artifact.get("recovery_index_hash")
        ):
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery registry ledger lineage differs"
            )
        local_candidate_card_ids = {
            _required_string(
                row.get("candidate_card_id"),
                "local candidate_card_id",
            )
            for row in registry_ledger.get("local_candidate_cards") or []
            if isinstance(row, Mapping)
        }
        if len(local_candidate_card_ids) != len(
            registry_ledger.get("local_candidate_cards") or []
        ) or local_candidate_card_ids.intersection(allowed_candidate_card_ids):
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery local candidate cards differ or collide"
            )

    held_route_a_reviews = _verify_held_route_a_reviews_v1(
        chapter=chapter,
        rows=artifact.get("held_route_a_reviews", []),
    )
    held_route_a_review_ids = {
        row["review_id"] for row in held_route_a_reviews
    }

    turns: dict[str, dict[str, Any]] = {}
    for raw_turn in chapter.get("speaker_turns") or []:
        if not isinstance(raw_turn, Mapping):
            raise B2SlimSpeakerRecoveryError("B2 speaker turn must be an object")
        turn = deepcopy(dict(raw_turn))
        turn_id = _required_string(turn.get("speaker_turn_id"), "speaker_turn_id")
        if turn_id in turns:
            raise B2SlimSpeakerRecoveryError("B2 chapter repeats a speaker turn")
        turns[turn_id] = turn

    speaker_overlays = artifact.get("speaker_overlays")
    addressee_overlays = artifact.get("addressee_overlays")
    ticketed_turn_ids = artifact.get("ticketed_speaker_turn_ids")
    if (
        not isinstance(speaker_overlays, list)
        or not isinstance(addressee_overlays, list)
        or not isinstance(ticketed_turn_ids, list)
    ):
        raise B2SlimSpeakerRecoveryError("speaker recovery overlay index is malformed")

    represented_ticket_ids: set[str] = set()
    overlay_ids: set[str] = set()
    speaker_turn_ids = _verify_endpoint_overlay_collection_v1(
        overlays=speaker_overlays,
        endpoint_role="speaker",
        turns=turns,
        allowed_candidate_card_ids=allowed_candidate_card_ids,
        local_candidate_card_ids=local_candidate_card_ids,
        represented_ticket_ids=represented_ticket_ids,
        overlay_ids=overlay_ids,
    )
    addressee_turn_ids = _verify_endpoint_overlay_collection_v1(
        overlays=addressee_overlays,
        endpoint_role="addressee",
        turns=turns,
        allowed_candidate_card_ids=allowed_candidate_card_ids,
        local_candidate_card_ids=local_candidate_card_ids,
        represented_ticket_ids=represented_ticket_ids,
        overlay_ids=overlay_ids,
    )

    quarantine_rows = artifact.get("quarantined_ticket_actions") or []
    if not isinstance(quarantine_rows, list):
        raise B2SlimSpeakerRecoveryError(
            "speaker recovery quarantine index is malformed"
        )
    quarantined_ticket_ids: set[str] = set()
    quarantined_turn_ids: set[str] = set()
    quarantine_ids: set[str] = set()
    for raw_quarantine in quarantine_rows:
        if not isinstance(raw_quarantine, Mapping):
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery quarantine row must be an object"
            )
        quarantine = deepcopy(dict(raw_quarantine))
        quarantine_id = _required_string(
            quarantine.pop("quarantine_id", None), "quarantine_id"
        )
        if (
            quarantine_id in quarantine_ids
            or quarantine_id != _mint_id("b2spkq1", quarantine)
        ):
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery quarantine id differs or repeats"
            )
        quarantine_ids.add(quarantine_id)
        ticket_id = _required_string(
            quarantine.get("ticket_id"), "quarantine ticket_id"
        )
        turn_id = _required_string(
            quarantine.get("speaker_turn_id"), "quarantine speaker_turn_id"
        )
        action_hashes = quarantine.get("action_hashes")
        if (
            ticket_id in quarantined_ticket_ids
            or ticket_id in represented_ticket_ids
            or turn_id not in turns
            or quarantine.get("state") != "unreviewed"
            or not isinstance(quarantine.get("reason"), str)
            or not quarantine["reason"].strip()
            or not isinstance(quarantine.get("action_count"), int)
            or quarantine["action_count"] < 1
            or not isinstance(action_hashes, list)
            or len(action_hashes) != quarantine["action_count"]
            or not all(
                isinstance(value, str) and len(value) == 64
                for value in action_hashes
            )
        ):
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery quarantine row differs"
            )
        quarantined_ticket_ids.add(ticket_id)
        quarantined_turn_ids.add(turn_id)

    if not all(
        isinstance(value, str) and value.strip() for value in ticketed_turn_ids
    ) or (
        len(ticketed_turn_ids) != len(set(ticketed_turn_ids))
        or set(ticketed_turn_ids)
        != speaker_turn_ids | addressee_turn_ids | quarantined_turn_ids
    ):
        raise B2SlimSpeakerRecoveryError("ticketed turn index differs from overlays")
    seen_review_ids: set[str] = set()
    disposition_ticket_ids: set[str] = set()
    all_ticket_ids = represented_ticket_ids | quarantined_ticket_ids
    for raw_disposition in artifact.get("review_dispositions") or []:
        if not isinstance(raw_disposition, Mapping):
            raise B2SlimSpeakerRecoveryError("review disposition must be an object")
        review_id = _required_string(raw_disposition.get("review_id"), "review_id")
        if review_id in seen_review_ids:
            raise B2SlimSpeakerRecoveryError("review disposition repeats")
        seen_review_ids.add(review_id)
        primary_ticket_id = _required_string(
            raw_disposition.get("ticket_id"), "review disposition ticket_id"
        )
        raw_ticket_ids = raw_disposition.get("ticket_ids")
        ticket_ids = (
            [primary_ticket_id]
            if raw_ticket_ids is None
            else list(raw_ticket_ids)
            if isinstance(raw_ticket_ids, list)
            else []
        )
        if (
            not ticket_ids
            or ticket_ids[0] != primary_ticket_id
            or not all(
                isinstance(value, str) and value.strip() for value in ticket_ids
            )
            or len(ticket_ids) != len(set(ticket_ids))
            or not set(ticket_ids).issubset(all_ticket_ids)
        ):
            raise B2SlimSpeakerRecoveryError("review disposition targets a foreign ticket")
        disposition_ticket_ids.update(ticket_ids)
        action = raw_disposition.get("decision_action")
        expected_status = {
            "attach_existing": "resolved",
            "create_chapter_local": "resolved",
            "mixed_resolved": "resolved",
            "keep_pending": "unresolved_ambiguous",
            "mixed": "unresolved_ambiguous",
            "quarantined": "unresolved_ambiguous",
        }.get(action)
        if expected_status is None or raw_disposition.get("status") != expected_status:
            raise B2SlimSpeakerRecoveryError("review disposition is inconsistent")
        narrowed = raw_disposition.get("narrowed_candidate_card_ids")
        if (
            not isinstance(narrowed, list)
            or not all(
                isinstance(value, str) and value.strip() for value in narrowed
            )
            or len(narrowed) != len(set(narrowed))
            or not set(narrowed).issubset(allowed_candidate_card_ids)
        ):
            raise B2SlimSpeakerRecoveryError(
                "review disposition narrowed candidates are malformed"
            )
        _required_string(
            raw_disposition.get("frame_segment_id"),
            "review disposition frame_segment_id",
        )
    if disposition_ticket_ids != all_ticket_ids:
        raise B2SlimSpeakerRecoveryError(
            "review dispositions do not exact-cover recovery tickets"
        )
    serviceable_route_a_review_ids: set[str] = set()
    for raw_review in chapter.get("review_requests") or []:
        if not isinstance(raw_review, Mapping) or raw_review.get("status") != "pending":
            continue
        try:
            destination = route_review(raw_review)
        except (KeyError, ReviewRoutingError) as exc:
            raise B2SlimSpeakerRecoveryError(
                "B2 review has no valid typed route"
            ) from exc
        if destination != "A":
            continue
        review_id = _required_string(raw_review.get("review_id"), "review_id")
        if review_id in held_route_a_review_ids:
            continue
        if _route_a_endpoint_role_v1(raw_review) is None:
            raise B2SlimSpeakerRecoveryError(
                "unserviceable route-A review was not held"
            )
        else:
            serviceable_route_a_review_ids.add(review_id)
    if seen_review_ids != serviceable_route_a_review_ids:
        raise B2SlimSpeakerRecoveryError(
            "review dispositions do not exact-cover serviceable route-A reviews"
        )

    unresolved_review_ids = {
        str(row["review_id"])
        for row in artifact.get("review_dispositions") or []
        if row.get("status") == "unresolved_ambiguous"
    }
    ambiguity_review_ids: set[str] = set()
    frames = {
        str(row.get("frame_segment_id")): row
        for row in chapter.get("frame_segments") or []
        if isinstance(row, Mapping)
    }
    ambiguity_rows = artifact.get("unresolved_ambiguities")
    if not isinstance(ambiguity_rows, list):
        raise B2SlimSpeakerRecoveryError("ambiguity record index is malformed")
    for raw_record in ambiguity_rows:
        if not isinstance(raw_record, Mapping):
            raise B2SlimSpeakerRecoveryError("ambiguity record must be an object")
        record = deepcopy(dict(raw_record))
        review_id = _required_string(record.get("review_id"), "ambiguity review_id")
        if (
            review_id in ambiguity_review_ids
            or record.get("outcome") != "unresolved_ambiguous"
        ):
            raise B2SlimSpeakerRecoveryError("ambiguity record differs or repeats")
        ambiguity_review_ids.add(review_id)
        frame_id = _required_string(
            record.get("frame_segment_id"), "ambiguity frame_segment_id"
        )
        frame = frames.get(frame_id)
        scope = record.get("supplied_scope")
        if frame is None or not isinstance(scope, Mapping):
            raise B2SlimSpeakerRecoveryError("ambiguity supplied scope is missing")
        frame_blocks = list(frame.get("covered_block_ids") or [])
        if (
            scope.get("frame_segment_id") != frame_id
            or scope.get("block_id_range")
            != [frame.get("start_block_id"), frame.get("end_block_id")]
            or scope.get("block_count") != len(frame_blocks)
            or scope.get("source_block_ids") != frame_blocks
        ):
            raise B2SlimSpeakerRecoveryError(
                "ambiguity supplied scope is narrower than its frame"
            )
        scope_cards = scope.get("candidate_card_ids")
        narrowed = record.get("narrowed_candidate_card_ids")
        if (
            not isinstance(scope_cards, list)
            or not isinstance(narrowed, list)
            or len(scope_cards) != len(set(scope_cards))
            or len(narrowed) != len(set(narrowed))
            or not set(scope_cards).issubset(allowed_candidate_card_ids)
            or not set(narrowed).issubset(set(scope_cards))
        ):
            raise B2SlimSpeakerRecoveryError(
                "ambiguity candidate scope is malformed"
            )
        if not isinstance(record.get("model_reason"), str):
            raise B2SlimSpeakerRecoveryError("ambiguity model reason is malformed")
    if ambiguity_review_ids != unresolved_review_ids:
        raise B2SlimSpeakerRecoveryError(
            "ambiguity records do not exact-cover unresolved route-A reviews"
        )
    return deepcopy(artifact)


def _verify_endpoint_overlay_collection_v1(
    *,
    overlays: Sequence[Mapping[str, Any]],
    endpoint_role: str,
    turns: Mapping[str, Mapping[str, Any]],
    allowed_candidate_card_ids: set[str],
    local_candidate_card_ids: set[str],
    represented_ticket_ids: set[str],
    overlay_ids: set[str],
) -> set[str]:
    overlay_turn_ids: set[str] = set()
    for raw_overlay in overlays:
        if not isinstance(raw_overlay, Mapping):
            raise B2SlimSpeakerRecoveryError("endpoint overlay must be an object")
        overlay = deepcopy(dict(raw_overlay))
        overlay_id = _required_string(overlay.pop("overlay_id", None), "overlay_id")
        if overlay_id in overlay_ids or overlay_id != _mint_id("b2endov1", overlay):
            raise B2SlimSpeakerRecoveryError("endpoint overlay id differs or repeats")
        overlay_ids.add(overlay_id)
        if overlay.get("endpoint_role") != endpoint_role:
            raise B2SlimSpeakerRecoveryError("endpoint overlay role differs")
        turn_id = _required_string(
            overlay.get("speaker_turn_id"), "overlay speaker_turn_id"
        )
        if turn_id in overlay_turn_ids:
            raise B2SlimSpeakerRecoveryError("endpoint overlay turn repeats")
        overlay_turn_ids.add(turn_id)
        turn = turns.get(turn_id)
        if turn is None:
            raise B2SlimSpeakerRecoveryError("endpoint overlay targets a foreign turn")
        endpoint = turn.get(endpoint_role)
        if (
            not isinstance(endpoint, Mapping)
            or overlay.get("source_turn_snapshot_hash") != canonical_hash(turn)
            or overlay.get("original_endpoint") != dict(endpoint)
        ):
            raise B2SlimSpeakerRecoveryError("endpoint overlay source snapshot differs")
        compatibility_original = overlay.get(
            "original_speaker"
            if endpoint_role == "speaker"
            else "original_addressee"
        )
        compatibility_effective = overlay.get(
            "effective_speaker"
            if endpoint_role == "speaker"
            else "effective_addressee"
        )
        if (
            compatibility_original != overlay.get("original_endpoint")
            or compatibility_effective != overlay.get("effective_endpoint")
        ):
            raise B2SlimSpeakerRecoveryError(
                "endpoint overlay compatibility fields differ"
            )

        primary_ticket_id = _required_string(
            overlay.get("ticket_id"), "overlay ticket_id"
        )
        ticket_ids = overlay.get("ticket_ids")
        if (
            not isinstance(ticket_ids, list)
            or not ticket_ids
            or ticket_ids[0] != primary_ticket_id
            or not all(
                isinstance(value, str) and value.strip() for value in ticket_ids
            )
            or len(ticket_ids) != len(set(ticket_ids))
            or represented_ticket_ids.intersection(ticket_ids)
        ):
            raise B2SlimSpeakerRecoveryError(
                "endpoint overlay ticket index differs or repeats"
            )
        represented_ticket_ids.update(ticket_ids)
        source_block_ids = overlay.get("source_block_ids")
        narrowed = overlay.get("narrowed_candidate_card_ids")
        if (
            not isinstance(source_block_ids, list)
            or len(source_block_ids) != len(set(source_block_ids))
            or turn.get("block_id") not in source_block_ids
            or not isinstance(narrowed, list)
            or len(narrowed) != len(set(narrowed))
            or not set(narrowed).issubset(allowed_candidate_card_ids)
        ):
            raise B2SlimSpeakerRecoveryError("endpoint overlay evidence differs")

        action = overlay.get("action")
        effective = overlay.get("effective_endpoint")
        if action == "keep_pending":
            if (
                effective is not None
                or overlay.get("authority_status") != "pending_review"
            ):
                raise B2SlimSpeakerRecoveryError(
                    "unresolved endpoint overlay grants authority"
                )
        elif action in {"attach_existing", "create_chapter_local"}:
            candidate_ids = (
                effective.get("candidate_card_ids")
                if isinstance(effective, Mapping)
                else None
            )
            expected_candidate_ids = (
                local_candidate_card_ids
                if action == "create_chapter_local"
                else allowed_candidate_card_ids
            )
            if (
                not isinstance(effective, Mapping)
                or effective.get("resolution_status") != "resolved_candidate"
                or effective.get("resolution_basis")
                != "speaker_recovery_auditor"
                or overlay.get("authority_status")
                != "auditor_confirmed_chapter_local"
                or not isinstance(candidate_ids, list)
                or len(candidate_ids) != 1
                or candidate_ids[0] not in expected_candidate_ids
                or narrowed
            ):
                raise B2SlimSpeakerRecoveryError(
                    "resolved endpoint overlay is malformed"
                )
        else:
            raise B2SlimSpeakerRecoveryError("unknown endpoint recovery action")

        ticket_resolutions = overlay.get("ticket_resolutions")
        if not isinstance(ticket_resolutions, list):
            raise B2SlimSpeakerRecoveryError(
                "endpoint overlay ticket resolutions are malformed"
            )
        resolution_ids: set[str] = set()
        for raw_resolution in ticket_resolutions:
            if not isinstance(raw_resolution, Mapping):
                raise B2SlimSpeakerRecoveryError(
                    "endpoint overlay ticket resolution must be an object"
                )
            resolution_id = _required_string(
                raw_resolution.get("ticket_id"), "ticket resolution ticket_id"
            )
            resolution_blocks = raw_resolution.get("source_block_ids")
            resolution_narrowed = raw_resolution.get(
                "narrowed_candidate_card_ids"
            )
            if (
                resolution_id in resolution_ids
                or resolution_id not in set(ticket_ids)
                or raw_resolution.get("action") != action
                or not isinstance(resolution_blocks, list)
                or turn.get("block_id") not in resolution_blocks
                or not isinstance(resolution_narrowed, list)
                or len(resolution_narrowed) != len(set(resolution_narrowed))
                or not set(resolution_narrowed).issubset(
                    allowed_candidate_card_ids | local_candidate_card_ids
                )
            ):
                raise B2SlimSpeakerRecoveryError(
                    "endpoint overlay ticket resolution differs"
                )
            resolution_ids.add(resolution_id)
        if resolution_ids != set(ticket_ids):
            raise B2SlimSpeakerRecoveryError(
                "endpoint overlay ticket resolutions do not exact-cover tickets"
            )
    return overlay_turn_ids


def _endpoint_overlay_candidate_v1(
    *,
    ticket: Mapping[str, Any],
    action: Mapping[str, Any],
    turn: Mapping[str, Any],
    cards: Mapping[str, Mapping[str, Any]],
    local_candidate_card_ids: set[str],
    recovery_resolution: Mapping[str, Any],
) -> dict[str, Any]:
    action_name = _required_string(action.get("action"), "action")
    if action_name not in {
        "attach_existing",
        "create_chapter_local",
        "keep_pending",
    }:
        raise B2SlimSpeakerRecoveryError(
            "route-A recovery action is unsupported"
        )
    endpoint_role = _required_string(ticket.get("endpoint_role"), "endpoint_role")
    if endpoint_role not in {"speaker", "addressee"}:
        raise B2SlimSpeakerRecoveryError("route-A ticket has a foreign endpoint role")
    endpoint = turn.get(endpoint_role)
    if not isinstance(endpoint, Mapping):
        raise B2SlimSpeakerRecoveryError("route-A turn omits its endpoint")
    source_block_ids = action.get("source_block_ids")
    if (
        not isinstance(source_block_ids, list)
        or not all(
            isinstance(value, str) and value.strip() for value in source_block_ids
        )
        or len(source_block_ids) != len(set(source_block_ids))
        or turn.get("block_id") not in source_block_ids
    ):
        raise B2SlimSpeakerRecoveryError(
            "route-A recovery action cites no direct turn evidence"
        )

    ticket_candidate_ids = set(ticket.get("candidate_card_ids") or [])
    effective_endpoint: dict[str, Any] | None = None
    narrowed_candidate_ids: list[str] = []
    authority_status = "pending_review"
    if (
        recovery_resolution.get("ticket_id") != ticket.get("ticket_id")
        or recovery_resolution.get("action") != action_name
        or recovery_resolution.get("source_row_id") != ticket.get("source_row_id")
        or recovery_resolution.get("endpoint_role") != endpoint_role
    ):
        raise B2SlimSpeakerRecoveryError(
            "route-A recovery ledger resolution differs"
        )
    if action_name in {"attach_existing", "create_chapter_local"}:
        if action_name == "attach_existing":
            target_id = _required_string(
                action.get("target_candidate_card_id"),
                "target_candidate_card_id",
            )
            if target_id not in cards or target_id not in ticket_candidate_ids:
                raise B2SlimSpeakerRecoveryError(
                    "route-A target card was not supplied for its ticket"
                )
        else:
            target_id = _required_string(
                recovery_resolution.get("bound_candidate_card_id"),
                "local bound_candidate_card_id",
            )
            if (
                action.get("target_candidate_card_id") is not None
                or target_id not in cards
                or target_id not in local_candidate_card_ids
            ):
                raise B2SlimSpeakerRecoveryError(
                    "route-A local recovery card differs"
                )
        if "narrowed_candidate_card_ids" in action:
            raw_narrowed = action.get("narrowed_candidate_card_ids")
            if not isinstance(raw_narrowed, list) or raw_narrowed:
                raise B2SlimSpeakerRecoveryError(
                    "resolved route-A action carries narrowed candidates"
                )
        effective_endpoint = {
            "surface": cards[target_id].get("canonical_surface"),
            "resolution_status": "resolved_candidate",
            "candidate_card_ids": [target_id],
            "resolution_basis": "speaker_recovery_auditor",
        }
        authority_status = "auditor_confirmed_chapter_local"
    else:
        raw_narrowed = action.get("narrowed_candidate_card_ids")
        if not isinstance(raw_narrowed, list):
            raise B2SlimSpeakerRecoveryError(
                "unresolved route-A action lacks narrowed candidates"
            )
        narrowed_candidate_ids = [
            _required_string(value, "narrowed candidate card id")
            for value in raw_narrowed
        ]
        if (
            len(narrowed_candidate_ids) != len(set(narrowed_candidate_ids))
            or not set(narrowed_candidate_ids).issubset(ticket_candidate_ids)
        ):
            raise B2SlimSpeakerRecoveryError(
                "unresolved route-A action narrows outside supplied cards"
            )

    return {
        "speaker_turn_id": ticket["source_row_id"],
        "ticket_id": ticket["ticket_id"],
        "endpoint_role": endpoint_role,
        "source_frame_segment_id": ticket["source_frame_segment_id"],
        "source_turn_snapshot_hash": canonical_hash(turn),
        "original_endpoint": deepcopy(dict(endpoint)),
        "action": action_name,
        "effective_endpoint": effective_endpoint,
        "narrowed_candidate_card_ids": narrowed_candidate_ids,
        "authority_status": authority_status,
        "source_block_ids": deepcopy(source_block_ids),
        "resolution_note": action.get("resolution_note"),
        "ticket_action": deepcopy(dict(action)),
    }


def _collapse_endpoint_overlay_candidates_v1(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        (deepcopy(dict(row)) for row in candidates),
        key=lambda row: row["ticket_id"],
    )
    if not ordered:
        raise B2SlimSpeakerRecoveryError("route-A overlay group is empty")
    primary = ordered[0]
    ticket_ids = [row["ticket_id"] for row in ordered]
    ticket_resolutions = [
        {
            "ticket_id": row["ticket_id"],
            "action": row["action"],
            "source_block_ids": deepcopy(row["source_block_ids"]),
            "pending_reason": row["ticket_action"].get("pending_reason"),
            "narrowed_candidate_card_ids": deepcopy(
                row["narrowed_candidate_card_ids"]
            ),
            "resolution_note": row["resolution_note"],
        }
        for row in ordered
    ]
    overlay_body = {
        "speaker_turn_id": primary["speaker_turn_id"],
        "endpoint_role": primary["endpoint_role"],
        "source_frame_segment_id": primary["source_frame_segment_id"],
        "ticket_id": ticket_ids[0],
        "ticket_ids": ticket_ids,
        "ticket_resolutions": ticket_resolutions,
        "source_turn_snapshot_hash": primary["source_turn_snapshot_hash"],
        "original_endpoint": deepcopy(primary["original_endpoint"]),
        "action": primary["action"],
        "effective_endpoint": deepcopy(primary["effective_endpoint"]),
        "narrowed_candidate_card_ids": deepcopy(
            primary["narrowed_candidate_card_ids"]
        ),
        "authority_status": primary["authority_status"],
        "source_block_ids": sorted(
            {
                block_id
                for row in ordered
                for block_id in row["source_block_ids"]
            }
        ),
        "resolution_note": primary["resolution_note"],
    }
    if primary["endpoint_role"] == "speaker":
        overlay_body["original_speaker"] = deepcopy(primary["original_endpoint"])
        overlay_body["effective_speaker"] = deepcopy(primary["effective_endpoint"])
    else:
        overlay_body["original_addressee"] = deepcopy(primary["original_endpoint"])
        overlay_body["effective_addressee"] = deepcopy(primary["effective_endpoint"])
    return {"overlay_id": _mint_id("b2endov1", overlay_body), **overlay_body}


def _quarantined_ticket_action_v1(
    *,
    ticket: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    reason: str,
) -> dict[str, Any]:
    return _quarantined_ticket_action_hashes_v1(
        ticket=ticket,
        action_hashes=sorted(canonical_hash(dict(action)) for action in actions),
        reason=reason,
    )


def _quarantined_ticket_action_hashes_v1(
    *,
    ticket: Mapping[str, Any],
    action_hashes: Sequence[str],
    reason: str,
) -> dict[str, Any]:
    normalized_hashes = sorted(str(value) for value in action_hashes)
    body = {
        "ticket_id": ticket["ticket_id"],
        "speaker_turn_id": ticket["source_row_id"],
        "state": "unreviewed",
        "reason": _required_string(reason, "quarantine reason"),
        "action_count": len(normalized_hashes),
        "action_hashes": normalized_hashes,
    }
    return {"quarantine_id": _mint_id("b2spkq1", body), **body}


def _route_a_review_dispositions_v1(
    *,
    tickets: Mapping[str, Mapping[str, Any]],
    ticket_outcomes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if set(ticket_outcomes) != set(tickets):
        raise B2SlimSpeakerRecoveryError(
            "route-A recovery outcomes do not exact-cover tickets"
        )
    review_signals: dict[str, dict[str, Any]] = {}
    review_ticket_ids: dict[str, set[str]] = defaultdict(set)
    for ticket_id, ticket in tickets.items():
        signals = ticket.get("source_review_signals") or []
        if not isinstance(signals, list) or not signals:
            raise B2SlimSpeakerRecoveryError(
                "route-A recovery ticket has no review signal"
            )
        for raw_signal in signals:
            if not isinstance(raw_signal, Mapping):
                raise B2SlimSpeakerRecoveryError(
                    "route-A recovery review signal must be an object"
                )
            signal = deepcopy(dict(raw_signal))
            review_id = _required_string(signal.get("review_id"), "review_id")
            previous = review_signals.get(review_id)
            if previous is not None and previous != signal:
                raise B2SlimSpeakerRecoveryError(
                    "route-A recovery review signal differs across tickets"
                )
            review_signals[review_id] = signal
            review_ticket_ids[review_id].add(ticket_id)

    dispositions: list[dict[str, Any]] = []
    for review_id in sorted(review_ticket_ids):
        ticket_ids = sorted(review_ticket_ids[review_id])
        outcomes = [ticket_outcomes[ticket_id] for ticket_id in ticket_ids]
        status = (
            "resolved"
            if all(row["status"] == "resolved" for row in outcomes)
            else "unresolved_ambiguous"
        )
        actions = {row["decision_action"] for row in outcomes}
        if status == "resolved":
            if not actions or not actions.issubset(
                {"attach_existing", "create_chapter_local"}
            ):
                raise B2SlimSpeakerRecoveryError(
                    "resolved route-A review has inconsistent ticket outcomes"
                )
            decision_action = (
                next(iter(actions)) if len(actions) == 1 else "mixed_resolved"
            )
        elif actions == {"keep_pending"}:
            decision_action = "keep_pending"
        elif actions == {"quarantined"}:
            decision_action = "quarantined"
        else:
            decision_action = "mixed"
        dispositions.append(
            {
                "review_id": review_id,
                "ticket_id": ticket_ids[0],
                "ticket_ids": ticket_ids,
                "status": status,
                "decision_action": decision_action,
                "frame_segment_id": _single_value_v1(
                    {
                        str(tickets[ticket_id]["source_frame_segment_id"])
                        for ticket_id in ticket_ids
                    },
                    "route-A review frame",
                ),
                "narrowed_candidate_card_ids": sorted(
                    {
                        candidate_id
                        for row in outcomes
                        for candidate_id in row.get(
                            "narrowed_candidate_card_ids", []
                        )
                    }
                ),
                "model_reasons": [
                    reason
                    for row in outcomes
                    for reason in row.get("model_reasons", [])
                ],
                "quarantine_reasons": sorted(
                    {
                        row["quarantine_reason"]
                        for row in outcomes
                        if row["quarantine_reason"]
                    }
                ),
            }
        )
    return dispositions


def _ambiguity_records_v1(
    *,
    tickets: Mapping[str, Mapping[str, Any]],
    ticket_outcomes: Mapping[str, Mapping[str, Any]],
    dispositions: Sequence[Mapping[str, Any]],
    index: Mapping[str, Any],
    actions_by_ticket: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    component_by_ticket: dict[str, dict[str, Any]] = {}
    for raw_component in index.get("registry_components") or []:
        component = deepcopy(dict(raw_component))
        for ticket_id in component.get("ticket_ids") or []:
            if ticket_id in component_by_ticket:
                raise B2SlimSpeakerRecoveryError(
                    "route-A ticket appears in multiple recovery components"
                )
            component_by_ticket[str(ticket_id)] = component

    records: list[dict[str, Any]] = []
    for raw_disposition in dispositions:
        disposition = deepcopy(dict(raw_disposition))
        if disposition.get("status") != "unresolved_ambiguous":
            continue
        ticket_ids = list(disposition.get("ticket_ids") or [])
        components = [component_by_ticket[ticket_id] for ticket_id in ticket_ids]
        frame_ids = {str(row.get("frame_segment_id")) for row in components}
        frame_id = _single_value_v1(frame_ids, "ambiguity supplied frame")
        source_scopes = {
            tuple(str(value) for value in row.get("source_block_ids") or [])
            for row in components
        }
        if len(source_scopes) != 1:
            raise B2SlimSpeakerRecoveryError(
                "ambiguity record was not supplied one complete frame"
            )
        source_block_ids = list(next(iter(source_scopes)))
        if not source_block_ids:
            raise B2SlimSpeakerRecoveryError("ambiguity supplied frame is empty")
        candidate_ids = sorted(
            {
                str(candidate_id)
                for component in components
                for candidate_id in component.get("candidate_card_ids") or []
            }
        )
        model_reasons = [
            _required_string(value, "route-A model reason")
            for value in disposition.get("model_reasons") or []
            if isinstance(value, str) and value.strip()
        ]
        if not model_reasons:
            model_reasons = [
                _required_string(
                    action.get("resolution_note"),
                    "route-A model resolution note",
                )
                for ticket_id in ticket_ids
                for action in actions_by_ticket.get(ticket_id, [])
                if isinstance(action, Mapping)
                and isinstance(action.get("resolution_note"), str)
                and action["resolution_note"].strip()
            ]
        records.append(
            {
                "review_id": disposition["review_id"],
                "frame_segment_id": frame_id,
                "outcome": "unresolved_ambiguous",
                "narrowed_candidate_card_ids": deepcopy(
                    disposition.get("narrowed_candidate_card_ids") or []
                ),
                "supplied_scope": {
                    "frame_segment_id": frame_id,
                    "block_id_range": [
                        source_block_ids[0],
                        source_block_ids[-1],
                    ],
                    "block_count": len(source_block_ids),
                    "source_block_ids": source_block_ids,
                    "candidate_card_ids": candidate_ids,
                },
                "model_reason": (
                    model_reasons[0]
                    if len(model_reasons) == 1
                    else canonical_json(model_reasons)
                ),
                "model_reasons": model_reasons,
                "ticket_ids": ticket_ids,
                "quarantine_reasons": deepcopy(
                    disposition.get("quarantine_reasons") or []
                ),
            }
        )
    return records


def _single_value_v1(values: set[str], label: str) -> str:
    if len(values) != 1:
        raise B2SlimSpeakerRecoveryError(f"{label} is not singular")
    return next(iter(values))


def build_b2_effective_review_projection_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    recovery_artifact: Mapping[str, Any] | None,
    allowed_candidate_card_ids: set[str],
    recovery_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply ticket dispositions to the B2 review view consumed downstream."""

    chapter = _verified_hash(
        chapter_artifact, "artifact_hash", "B2 chapter artifact"
    )
    raw_reviews = chapter.get("review_requests") or []
    if not isinstance(raw_reviews, list) or not all(
        isinstance(row, Mapping) for row in raw_reviews
    ):
        raise B2SlimSpeakerRecoveryError("B2 review requests are malformed")

    reviews: dict[str, dict[str, Any]] = {}
    serviceable_route_a_review_ids: set[str] = set()
    held_route_a_review_ids = {
        row["review_id"] for row in _expected_held_route_a_reviews_v1(chapter)
    }
    for raw in raw_reviews:
        row = deepcopy(dict(raw))
        review_id = _required_string(row.get("review_id"), "review_id")
        if review_id in reviews:
            raise B2SlimSpeakerRecoveryError("B2 review request repeats")
        reviews[review_id] = row
        try:
            destination = route_review(row)
        except (KeyError, ReviewRoutingError) as exc:
            raise B2SlimSpeakerRecoveryError(
                "B2 review has no valid typed route"
            ) from exc
        if destination == "E":
            raise B2SlimSpeakerRecoveryError(
                "frame-structure review reached Speaker Recovery projection"
            )
        if destination == "A":
            if review_id not in held_route_a_review_ids:
                serviceable_route_a_review_ids.add(review_id)

    if recovery_artifact is None:
        if serviceable_route_a_review_ids:
            raise B2SlimSpeakerRecoveryError(
                "route-A reviews require an effective recovery projection"
            )
        body = {
            "schema_version": EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION,
            "chapter_id": chapter["chapter_id"],
            "source_b2_artifact_hash": chapter["artifact_hash"],
            "source_speaker_recovery_artifact_hash": None,
            "effective_review_requests": [reviews[key] for key in sorted(reviews)],
            "resolved_review_ids": [],
            "unresolved_ambiguous_review_ids": [],
            "terminal_route_a_review_ids": [],
            "held_route_a_review_ids": sorted(held_route_a_review_ids),
            "speaker_overlay_count": 0,
            "addressee_overlay_count": 0,
        }
        return {**body, "projection_hash": canonical_hash(body)}

    recovery = verify_b2_slim_speaker_recovery_artifact_v1(
        chapter_artifact=chapter,
        recovery_artifact=recovery_artifact,
        allowed_candidate_card_ids=allowed_candidate_card_ids,
        recovery_index=recovery_index,
    )
    dispositions: dict[str, dict[str, Any]] = {}
    for raw in recovery.get("review_dispositions") or []:
        row = deepcopy(dict(raw))
        review_id = _required_string(row.get("review_id"), "review_id")
        if review_id not in reviews:
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery disposition targets a foreign review"
            )
        if review_id in dispositions:
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery disposition repeats"
            )
        dispositions[review_id] = row
    if set(dispositions) != serviceable_route_a_review_ids:
        raise B2SlimSpeakerRecoveryError(
            "route-A recovery dispositions do not exact-cover serviceable reviews"
        )
    held_rows = _verify_held_route_a_reviews_v1(
        chapter=chapter,
        rows=recovery.get("held_route_a_reviews", []),
    )
    if {row["review_id"] for row in held_rows} != held_route_a_review_ids:
        raise B2SlimSpeakerRecoveryError(
            "speaker recovery held review set differs"
        )

    effective_reviews: list[dict[str, Any]] = []
    resolved_review_ids: list[str] = []
    unresolved_ambiguous_review_ids: list[str] = []
    for review_id in sorted(reviews):
        review = reviews[review_id]
        disposition = dispositions.get(review_id)
        if disposition is None:
            effective_reviews.append(review)
        elif disposition.get("status") == "resolved":
            resolved_review_ids.append(review_id)
        elif disposition.get("status") == "unresolved_ambiguous":
            unresolved_ambiguous_review_ids.append(review_id)
        else:
            raise B2SlimSpeakerRecoveryError(
                "speaker recovery disposition has an invalid effective status"
            )

    body = {
        "schema_version": EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION,
        "chapter_id": chapter["chapter_id"],
        "source_b2_artifact_hash": chapter["artifact_hash"],
        "source_speaker_recovery_artifact_hash": recovery["artifact_hash"],
        "effective_review_requests": effective_reviews,
        "resolved_review_ids": resolved_review_ids,
        "unresolved_ambiguous_review_ids": unresolved_ambiguous_review_ids,
        "terminal_route_a_review_ids": sorted(
            resolved_review_ids + unresolved_ambiguous_review_ids
        ),
        "held_route_a_review_ids": sorted(held_route_a_review_ids),
        "speaker_overlay_count": len(recovery.get("speaker_overlays") or []),
        "addressee_overlay_count": len(
            recovery.get("addressee_overlays") or []
        ),
    }
    return verify_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        projection={**body, "projection_hash": canonical_hash(body)},
    )


def verify_b2_effective_review_projection_v1(
    *, chapter_artifact: Mapping[str, Any], projection: Mapping[str, Any]
) -> dict[str, Any]:
    chapter = _verified_hash(
        chapter_artifact, "artifact_hash", "B2 chapter artifact"
    )
    verified = _verified_hash(
        projection, "projection_hash", "B2 effective review projection"
    )
    if (
        verified.get("schema_version")
        != EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION
        or verified.get("chapter_id") != chapter.get("chapter_id")
        or verified.get("source_b2_artifact_hash") != chapter.get("artifact_hash")
    ):
        raise B2SlimSpeakerRecoveryError(
            "B2 effective review projection lineage differs"
        )

    source_reviews = chapter.get("review_requests") or []
    source_by_id: dict[str, dict[str, Any]] = {}
    for raw in source_reviews:
        if not isinstance(raw, Mapping):
            raise B2SlimSpeakerRecoveryError("B2 review request must be an object")
        row = deepcopy(dict(raw))
        review_id = _required_string(row.get("review_id"), "review_id")
        if review_id in source_by_id:
            raise B2SlimSpeakerRecoveryError("B2 review request repeats")
        source_by_id[review_id] = row

    pending = verified.get("effective_review_requests")
    resolved = verified.get("resolved_review_ids")
    unresolved = verified.get("unresolved_ambiguous_review_ids")
    terminal = verified.get("terminal_route_a_review_ids")
    held = verified.get("held_route_a_review_ids") or []
    if (
        not isinstance(pending, list)
        or not isinstance(resolved, list)
        or not isinstance(unresolved, list)
        or not isinstance(terminal, list)
        or not isinstance(held, list)
    ):
        raise B2SlimSpeakerRecoveryError(
            "B2 effective review partition is malformed"
        )
    pending_ids: set[str] = set()
    for raw in pending:
        if not isinstance(raw, Mapping):
            raise B2SlimSpeakerRecoveryError(
                "effective B2 review request must be an object"
            )
        review_id = _required_string(raw.get("review_id"), "review_id")
        if review_id in pending_ids or source_by_id.get(review_id) != dict(raw):
            raise B2SlimSpeakerRecoveryError(
                "effective B2 review request differs from its source"
            )
        pending_ids.add(review_id)
    if not all(
        isinstance(value, str) and value
        for value in [*resolved, *unresolved, *terminal, *held]
    ):
        raise B2SlimSpeakerRecoveryError("terminal B2 review ids are malformed")
    resolved_ids = set(resolved)
    unresolved_ids = set(unresolved)
    terminal_ids = set(terminal)
    held_ids = set(held)
    if (
        len(resolved_ids) != len(resolved)
        or len(unresolved_ids) != len(unresolved)
        or len(terminal_ids) != len(terminal)
        or len(held_ids) != len(held)
        or resolved_ids & unresolved_ids
        or terminal_ids != resolved_ids | unresolved_ids
        or pending_ids & terminal_ids
        or not held_ids.issubset(pending_ids)
    ):
        raise B2SlimSpeakerRecoveryError("B2 effective review partition overlaps")
    if pending_ids | terminal_ids != set(source_by_id):
        raise B2SlimSpeakerRecoveryError(
            "B2 effective review projection does not exact-cover source reviews"
        )
    for review_id in terminal_ids:
        try:
            destination = route_review(source_by_id[review_id])
        except (KeyError, ReviewRoutingError) as exc:
            raise B2SlimSpeakerRecoveryError(
                "terminal B2 review has no valid typed route"
            ) from exc
        if destination != "A":
            raise B2SlimSpeakerRecoveryError(
                "non-route-A review was terminated by endpoint recovery"
            )
        if _route_a_endpoint_role_v1(source_by_id[review_id]) is None:
            raise B2SlimSpeakerRecoveryError(
                "unserviceable route-A review was terminated"
            )
    expected_held_ids = {
        row["review_id"] for row in _expected_held_route_a_reviews_v1(chapter)
    }
    if held_ids != expected_held_ids:
        raise B2SlimSpeakerRecoveryError(
            "held route-A review projection differs"
        )
    for count_field in ("speaker_overlay_count", "addressee_overlay_count"):
        if (
            not isinstance(verified.get(count_field), int)
            or verified[count_field] < 0
        ):
            raise B2SlimSpeakerRecoveryError(
                f"{count_field} is malformed"
            )
    recovery_hash = verified.get("source_speaker_recovery_artifact_hash")
    if recovery_hash is not None and (
        not isinstance(recovery_hash, str) or not recovery_hash
    ):
        raise B2SlimSpeakerRecoveryError("speaker recovery hash is malformed")
    return deepcopy(verified)


def request_payload_v1(request: RenderedB2RecoveryRequestV1) -> dict[str, Any]:
    return batch_request_payload_v1(request)


def _request_context(
    requests: Sequence[Mapping[str, Any]], *, chapter_id: str
) -> dict[str, Any]:
    if not requests:
        raise B2SlimSpeakerRecoveryError("interaction requests are empty")
    blocks: dict[str, dict[str, Any]] = {}
    block_order: dict[str, int] = {}
    cards: dict[str, dict[str, Any]] = {}
    cards_by_block: dict[str, set[str]] = defaultdict(set)
    fingerprints: list[str] = []
    window_ids: list[str] = []
    for raw_request in requests:
        request = _verified_request(raw_request)
        if request.get("chapter_id") != chapter_id:
            raise B2SlimSpeakerRecoveryError("interaction request belongs elsewhere")
        window_id = _required_string(request.get("window_id"), "window_id")
        payload = _user_payload(request)
        if payload.get("chapter_id") != chapter_id or payload.get("window_id") != window_id:
            raise B2SlimSpeakerRecoveryError("interaction payload lineage mismatch")
        active_blocks = payload.get("active_blocks")
        tail_blocks = payload.get("preceding_tail")
        packet = payload.get("candidate_packets")
        if not isinstance(active_blocks, list) or not isinstance(tail_blocks, list):
            raise B2SlimSpeakerRecoveryError("interaction blocks are malformed")
        if not isinstance(packet, Mapping) or not isinstance(
            packet.get("candidate_cards"), list
        ):
            raise B2SlimSpeakerRecoveryError("interaction candidate packet is malformed")
        packet_ids: set[str] = set()
        for raw_card in packet["candidate_cards"]:
            if not isinstance(raw_card, Mapping):
                raise B2SlimSpeakerRecoveryError("candidate card must be an object")
            card = deepcopy(dict(raw_card))
            card_id = _required_string(card.get("candidate_card_id"), "candidate_card_id")
            if card_id in cards and canonical_json(cards[card_id]) != canonical_json(card):
                raise B2SlimSpeakerRecoveryError("candidate card payload drifted")
            cards[card_id] = card
            packet_ids.add(card_id)
        for raw_block in [*tail_blocks, *active_blocks]:
            if not isinstance(raw_block, Mapping):
                raise B2SlimSpeakerRecoveryError("source block must be an object")
            block_id = _required_string(raw_block.get("block_id"), "block_id")
            block = {
                "block_id": block_id,
                "block_type": str(raw_block.get("block_type") or "unknown"),
                "text": _required_string(raw_block.get("text"), "block text"),
            }
            if block_id in blocks and canonical_json(blocks[block_id]) != canonical_json(block):
                raise B2SlimSpeakerRecoveryError("source block payload drifted")
            if block_id not in blocks:
                block_order[block_id] = len(block_order)
                blocks[block_id] = block
        for raw_block in active_blocks:
            cards_by_block[str(raw_block["block_id"])].update(packet_ids)
        fingerprints.append(request["request_fingerprint"])
        window_ids.append(window_id)
    if len(window_ids) != len(set(window_ids)):
        raise B2SlimSpeakerRecoveryError("interaction window is repeated")
    return {
        "blocks": blocks,
        "block_order": block_order,
        "ordered_block_ids": [
            block_id
            for block_id, _ordinal in sorted(block_order.items(), key=lambda row: row[1])
        ],
        "cards": cards,
        "cards_by_block": cards_by_block,
        "request_fingerprints": fingerprints,
        "window_ids": window_ids,
    }


def _components(
    tickets: Sequence[dict[str, Any]],
    *,
    context: Mapping[str, Any],
    frames: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ticket in tickets:
        groups[str(ticket["source_frame_segment_id"])].append(ticket)
    components: list[dict[str, Any]] = []
    ordinal = 0
    for frame_id in sorted(groups):
        rows = groups[frame_id]
        frame_block_ids = list(frames[frame_id]["covered_block_ids"])
        for start in range(0, len(rows), MAX_TICKETS_PER_COMPONENT):
            ordinal += 1
            group = rows[start : start + MAX_TICKETS_PER_COMPONENT]
            direct_ids = sorted(
                {bid for row in group for bid in row["source_block_ids"]},
                key=context["block_order"].__getitem__,
            )
            source_ids = list(frame_block_ids)
            candidate_ids = sorted(
                {
                    *(
                        candidate_id
                        for row in group
                        for candidate_id in row["candidate_card_ids"]
                    ),
                    *(
                        candidate_id
                        for block_id in source_ids
                        for candidate_id in context["cards_by_block"].get(block_id, set())
                    ),
                }
            )
            overflow_reasons: list[str] = []
            if len(source_ids) > MAX_SOURCE_BLOCKS_PER_COMPONENT:
                overflow_reasons.append("source_block_cap_exceeded")
            if len(candidate_ids) > MAX_CANDIDATE_CARDS_PER_COMPONENT:
                overflow_reasons.append("candidate_card_cap_exceeded")
            body = {
                "component_kind": "registry_gap",
                "chapter_id": group[0]["chapter_id"],
                "ordinal": ordinal,
                "frame_segment_id": frame_id,
                "ticket_ids": [row["ticket_id"] for row in group],
                "source_block_ids": source_ids[:MAX_SOURCE_BLOCKS_PER_COMPONENT],
                "candidate_card_ids": candidate_ids,
                "overflow": bool(overflow_reasons),
                "overflow_reasons": overflow_reasons,
                "authority_effect": "none",
            }
            components.append(
                {"component_id": _mint_id("b2slimgapcomp1", body), **body}
            )
    return components


def _source_closure(
    direct_ids: Sequence[str], *, context: Mapping[str, Any]
) -> list[str]:
    ordered = context["ordered_block_ids"]
    selected = set(direct_ids)
    for block_id in direct_ids:
        index = ordered.index(block_id)
        for offset in (-1, 0, 1):
            neighbor = index + offset
            if 0 <= neighbor < len(ordered):
                selected.add(ordered[neighbor])
    return [block_id for block_id in ordered if block_id in selected]


def _review_signal(review: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "review_id": _required_string(review.get("review_id"), "review_id"),
        "origin": _required_string(review.get("origin"), "review origin"),
        "blocking_kind": _required_string(
            review.get("blocking_kind"), "review blocking_kind"
        ),
        "candidate_card_ids": sorted(
            _required_string(value, "review candidate id")
            for value in review.get("candidate_card_ids") or []
        ),
        "competing_card_ids": sorted(
            _required_string(value, "review competing id")
            for value in review.get("competing_card_ids") or []
        ),
        "reason": _required_string(review.get("reason"), "review reason"),
    }


def _frame_catalog_v1(
    *,
    chapter: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    frames: dict[str, dict[str, Any]] = {}
    frame_by_block: dict[str, str] = {}
    ordered_ids = list(context["ordered_block_ids"])
    for raw in chapter.get("frame_segments") or []:
        if not isinstance(raw, Mapping):
            raise B2SlimSpeakerRecoveryError("frame segment must be an object")
        frame = deepcopy(dict(raw))
        frame_id = _required_string(
            frame.get("frame_segment_id"), "frame_segment_id"
        )
        block_ids = [
            _required_string(value, "frame covered block")
            for value in frame.get("covered_block_ids") or []
        ]
        if not block_ids or any(value not in context["blocks"] for value in block_ids):
            raise B2SlimSpeakerRecoveryError("frame cites a missing source block")
        positions = [ordered_ids.index(value) for value in block_ids]
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise B2SlimSpeakerRecoveryError("frame evidence is not contiguous")
        if (
            frame.get("start_block_id") != block_ids[0]
            or frame.get("end_block_id") != block_ids[-1]
            or frame_id in frames
        ):
            raise B2SlimSpeakerRecoveryError("frame boundary index differs")
        for block_id in block_ids:
            if block_id in frame_by_block:
                raise B2SlimSpeakerRecoveryError("frame evidence overlaps")
            frame_by_block[block_id] = frame_id
        frames[frame_id] = frame
    if set(frame_by_block) != set(context["blocks"]):
        raise B2SlimSpeakerRecoveryError(
            "frame evidence does not exact-cover the chapter"
        )
    return frames, frame_by_block


def _chapter_frame_by_block_v1(chapter: Mapping[str, Any]) -> dict[str, str]:
    frame_by_block: dict[str, str] = {}
    for raw_frame in chapter.get("frame_segments") or []:
        if not isinstance(raw_frame, Mapping):
            raise B2SlimSpeakerRecoveryError("frame segment must be an object")
        frame_id = _required_string(
            raw_frame.get("frame_segment_id"), "frame_segment_id"
        )
        for raw_block_id in raw_frame.get("covered_block_ids") or []:
            block_id = _required_string(raw_block_id, "frame covered block")
            if block_id in frame_by_block:
                raise B2SlimSpeakerRecoveryError("frame evidence overlaps")
            frame_by_block[block_id] = frame_id
    return frame_by_block


def _route_a_hold_reason_v1(
    review: Mapping[str, Any],
    *,
    frame_by_block: Mapping[str, str],
) -> str | None:
    if _route_a_endpoint_role_v1(review) is None:
        return UNSUPPORTED_ROUTE_A_REVIEW_KIND
    block_ids = [
        _required_string(value, "review block_id")
        for value in review.get("source_block_ids") or []
    ]
    if block_ids and all(block_id in frame_by_block for block_id in block_ids):
        if len({frame_by_block[block_id] for block_id in block_ids}) != 1:
            return ROUTE_A_REVIEW_SPANS_MULTIPLE_FRAMES
    return None


def _verified_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _verified_hash(value, "request_fingerprint", "interaction request")
    if (
        request.get("schema_version") != "literary_b2_rendered_request_v1"
        or request.get("request_kind") != "window_interaction"
        or request.get("prompt_id") != B2_SLIM_INTERACTION_PROMPT_ID_V11
    ):
        raise B2SlimSpeakerRecoveryError("foreign B2 Slim interaction request")
    if canonical_hash(request.get("response_schema")) != request.get(
        "response_schema_hash"
    ):
        raise B2SlimSpeakerRecoveryError("interaction response schema hash mismatch")
    return request


def _user_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise B2SlimSpeakerRecoveryError("interaction messages are malformed")
    rows = [row for row in messages if isinstance(row, Mapping) and row.get("role") == "user"]
    if len(rows) != 1:
        raise B2SlimSpeakerRecoveryError("interaction request needs one user payload")
    try:
        payload = json.loads(_required_string(rows[0].get("content"), "user payload"))
    except json.JSONDecodeError as exc:
        raise B2SlimSpeakerRecoveryError("interaction user payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise B2SlimSpeakerRecoveryError("interaction user payload must be an object")
    return payload


def _verified_hash(
    value: Mapping[str, Any], hash_field: str, label: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B2SlimSpeakerRecoveryError(f"{label} must be an object")
    body = deepcopy(dict(value))
    observed = _required_string(body.pop(hash_field, None), f"{label} {hash_field}")
    if canonical_hash(body) != observed:
        raise B2SlimSpeakerRecoveryError(f"{label} hash mismatch")
    return {**body, hash_field: observed}


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise B2SlimSpeakerRecoveryError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise B2SlimSpeakerRecoveryError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2SlimSpeakerRecoveryError(f"{label} must be a non-empty string")
    return value


def _mint_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}_{canonical_hash(value)[:20]}"


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "EFFECTIVE_REVIEW_PROJECTION_SCHEMA_VERSION",
    "B2SlimSpeakerRecoveryError",
    "apply_b2_slim_speaker_recovery_decision_v1",
    "build_b2_effective_review_projection_v1",
    "build_b2_slim_speaker_recovery_index_v1",
    "load_b2_slim_speaker_source_v1",
    "make_b2_slim_speaker_recovery_validator_v1",
    "render_b2_slim_speaker_recovery_request_v1",
    "request_payload_v1",
    "verify_b2_effective_review_projection_v1",
    "verify_b2_slim_speaker_recovery_artifact_v1",
]
