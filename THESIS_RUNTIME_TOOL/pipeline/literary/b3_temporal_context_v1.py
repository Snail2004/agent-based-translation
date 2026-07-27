"""Verified B2 input loading and bounded B3 temporal request planning."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_registry_to_b2_input_v1 import (
    PACKAGE_FILENAME as B1_REGISTRY_B2_PACKAGE_FILENAME,
    B1RegistryToB2InputError,
    verify_b2_registry_input_package_v1,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    ARTIFACT_SCHEMA_VERSION as B2_SPEAKER_RECOVERY_ARTIFACT_SCHEMA_VERSION,
    B2SlimSpeakerRecoveryError,
    verify_b2_slim_speaker_recovery_artifact_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryError,
    verify_b2_recovery_index_v1,
)
from pipeline.literary.b2_review_routing_v1 import (
    ReviewRoutingError,
    route_review,
)
from pipeline.literary.b3_temporal_prompts_v1 import (
    B3_TEMPORAL_PROMPT_ID_V1,
    B3_TEMPORAL_SYSTEM_PROMPT_V1,
    bind_b3_temporal_response_schema_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.b3_parked_identity_v1 import (
    attach_parked_identity_to_candidate_cards_v1,
    verify_parked_identity_index_v1,
)
from pipeline.literary.b3_parked_identity_v2 import (
    PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2,
    attach_parked_identities_to_candidate_cards_v2,
    verify_parked_identity_index_v2,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


B3_PROFILE_SCHEMA_VERSION_V1 = "literary_b3_temporal_phase_a_profile_v1"
B3_INPUT_SCHEMA_VERSION_V1 = "literary_b3_temporal_input_v1_1"
B3_COMPONENT_SCHEMA_VERSION_V1 = "literary_b3_temporal_component_v1"
B3_REQUEST_SCHEMA_VERSION_V1 = "literary_b3_temporal_request_v1"
B3_PLAN_SCHEMA_VERSION_V1 = "literary_b3_temporal_phase_a_plan_v1"


class B3TemporalContextError(RuntimeError):
    pass


class B3TemporalBudgetError(B3TemporalContextError):
    pass


@dataclass(frozen=True)
class B3TemporalProfileV1:
    source_path: Path
    profile_id: str
    role_id: str
    recommended_preset_id: str
    recommended_model: str
    max_components_per_request: int
    max_requests_per_chapter: int
    max_candidate_cards_per_request: int
    max_turns_per_component: int
    max_events_per_component: int
    max_source_blocks_per_component: int
    max_prior_states_per_component: int
    provenance_refs_per_card: int
    prompt_tokens_per_request: int
    output_tokens_per_request: int
    safety: Mapping[str, Any]
    profile_hash: str
    profile_sha256: str


def load_b3_temporal_profile_v1(path: Path) -> B3TemporalProfileV1:
    source = Path(path).resolve()
    payload = _read_object(source, "B3 profile")
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "role",
            "batching",
            "context_caps",
            "token_caps",
            "safety",
        },
        "B3 profile",
    )
    if payload["schema_version"] != B3_PROFILE_SCHEMA_VERSION_V1:
        raise B3TemporalContextError("foreign B3 profile schema")
    role = _mapping(payload["role"], "B3 role")
    _exact_keys(
        role,
        {"role_id", "recommended_preset_id", "recommended_model"},
        "B3 role",
    )
    batching = _mapping(payload["batching"], "B3 batching")
    _exact_keys(
        batching,
        {
            "default_scope",
            "max_components_per_request",
            "max_requests_per_chapter",
            "split_only_between_components",
        },
        "B3 batching",
    )
    if batching["default_scope"] != "chapter_batch":
        raise B3TemporalContextError("B3 default scope must remain chapter_batch")
    if batching["split_only_between_components"] is not True:
        raise B3TemporalContextError("B3 may split only between complete components")
    caps = _mapping(payload["context_caps"], "B3 context caps")
    _exact_keys(
        caps,
        {
            "max_candidate_cards_per_request",
            "max_turns_per_component",
            "max_events_per_component",
            "max_source_blocks_per_component",
            "max_prior_states_per_component",
            "provenance_refs_per_card",
        },
        "B3 context caps",
    )
    tokens = _mapping(payload["token_caps"], "B3 token caps")
    _exact_keys(
        tokens,
        {"prompt_tokens_per_request", "output_tokens_per_request"},
        "B3 token caps",
    )
    safety = _mapping(payload["safety"], "B3 safety")
    expected_safety = {
        "phase": "phase_a_zero_api",
        "semantic_ambiguity_action": "persist_pending_and_continue",
        "single_component_overflow_action": "halt_before_api",
        "provider_fallback_allowed": False,
        "gold_or_oracle_allowed": False,
        "production_publish_enabled": False,
    }
    if safety != expected_safety:
        raise B3TemporalContextError("B3 safety contract was weakened or changed")
    return B3TemporalProfileV1(
        source_path=source,
        profile_id=_required_string(payload["profile_id"], "profile_id"),
        role_id=_required_string(role["role_id"], "role_id"),
        recommended_preset_id=_required_string(
            role["recommended_preset_id"], "recommended_preset_id"
        ),
        recommended_model=_required_string(
            role["recommended_model"], "recommended_model"
        ),
        max_components_per_request=_bounded_int(
            batching["max_components_per_request"],
            "max_components_per_request",
            1,
            64,
        ),
        max_requests_per_chapter=_bounded_int(
            batching["max_requests_per_chapter"],
            "max_requests_per_chapter",
            1,
            16,
        ),
        max_candidate_cards_per_request=_bounded_int(
            caps["max_candidate_cards_per_request"],
            "max_candidate_cards_per_request",
            1,
            256,
        ),
        max_turns_per_component=_bounded_int(
            caps["max_turns_per_component"],
            "max_turns_per_component",
            1,
            512,
        ),
        max_events_per_component=_bounded_int(
            caps["max_events_per_component"],
            "max_events_per_component",
            1,
            128,
        ),
        max_source_blocks_per_component=_bounded_int(
            caps["max_source_blocks_per_component"],
            "max_source_blocks_per_component",
            1,
            256,
        ),
        max_prior_states_per_component=_bounded_int(
            caps["max_prior_states_per_component"],
            "max_prior_states_per_component",
            0,
            128,
        ),
        provenance_refs_per_card=_bounded_int(
            caps["provenance_refs_per_card"],
            "provenance_refs_per_card",
            0,
            32,
        ),
        prompt_tokens_per_request=_bounded_int(
            tokens["prompt_tokens_per_request"],
            "prompt_tokens_per_request",
            1_000,
            100_000,
        ),
        output_tokens_per_request=_bounded_int(
            tokens["output_tokens_per_request"],
            "output_tokens_per_request",
            256,
            32_000,
        ),
        safety=dict(safety),
        profile_hash=canonical_hash(payload),
        profile_sha256=file_sha256(source),
    )


def load_b2_temporal_input_v1(
    b2_run_root: Path,
    *,
    speaker_recovery_root: Path | None = None,
    parked_identity_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(b2_run_root).resolve()
    if not root.is_dir():
        raise B3TemporalContextError(f"B2 run root is absent: {root}")
    seal = _verified_hashed_object(root / "run_seal.json", "seal_hash", "B2 seal")
    artifact = _verified_hashed_object(
        root / "chapter_b2_artifact.json", "artifact_hash", "B2 chapter artifact"
    )
    if artifact.get("schema_version") != "literary_b2_slim_chapter_artifact_v1":
        raise B3TemporalContextError("B3 V1 requires the B2 Slim chapter artifact")
    if Path(str(seal.get("output_root") or "")).resolve() != root:
        raise B3TemporalContextError("B2 seal output root differs from supplied root")
    chapter_id = _required_string(artifact.get("chapter_id"), "B2 chapter_id")
    if chapter_id != seal.get("chapter_id"):
        raise B3TemporalContextError("B2 chapter identity differs from seal")
    if artifact.get("run_seal_hash") != seal.get("seal_hash"):
        raise B3TemporalContextError("B2 chapter artifact is bound to another seal")
    if artifact.get("production_publish_performed") is not False:
        raise B3TemporalContextError("B2 source unexpectedly claims production publish")

    interaction_refs = artifact.get("interaction_artifacts")
    if not isinstance(interaction_refs, list) or not interaction_refs:
        raise B3TemporalContextError("B2 artifact has no interaction artifacts")
    expected_interactions = {
        _required_string(row.get("window_id"), "interaction window_id"): _required_string(
            row.get("artifact_hash"), "interaction artifact hash"
        )
        for row in interaction_refs
        if isinstance(row, Mapping)
    }
    if len(expected_interactions) != len(interaction_refs):
        raise B3TemporalContextError("B2 interaction reference is malformed or repeated")

    source_blocks: dict[str, dict[str, str]] = {}
    candidate_cards: dict[str, dict[str, Any]] = {}
    observed_interactions: dict[str, str] = {}
    request_fingerprints: list[str] = []
    for interaction_path in sorted(root.glob("interactions/*/interaction_artifact.json")):
        row = _verified_hashed_object(
            interaction_path, "artifact_hash", "B2 interaction artifact"
        )
        window_id = _required_string(row.get("window_id"), "B2 window_id")
        if window_id in observed_interactions:
            raise B3TemporalContextError("duplicate B2 interaction window")
        observed_interactions[window_id] = str(row["artifact_hash"])
        request = _verified_hashed_object(
            interaction_path.with_name("request.json"),
            "request_fingerprint",
            "B2 rendered request",
        )
        if request.get("chapter_id") != chapter_id or request.get("window_id") != window_id:
            raise B3TemporalContextError("B2 request identity differs from interaction")
        payload = _user_json_payload(request)
        if payload.get("chapter_id") != chapter_id or payload.get("window_id") != window_id:
            raise B3TemporalContextError("B2 user payload identity differs from request")
        packet = _mapping(payload.get("candidate_packets"), "B2 candidate packet")
        _verify_inline_hash(packet, "packet_hash", "B2 candidate packet")
        for raw_block in payload.get("active_blocks") or []:
            block = _mapping(raw_block, "B2 active block")
            block_id = _required_string(block.get("block_id"), "block_id")
            text = _required_string(block.get("text"), "block text")
            normalized = {"block_id": block_id, "text": text}
            prior = source_blocks.setdefault(block_id, normalized)
            if prior != normalized:
                raise B3TemporalContextError("B2 source block content drifted across windows")
        for raw_card in packet.get("candidate_cards") or []:
            card = deepcopy(_mapping(raw_card, "B2 candidate card"))
            card_id = _required_string(card.get("candidate_card_id"), "candidate_card_id")
            prior = candidate_cards.setdefault(card_id, card)
            if canonical_json(prior) != canonical_json(card):
                raise B3TemporalContextError("B2 candidate card drifted across windows")
        request_fingerprints.append(str(request["request_fingerprint"]))

    if observed_interactions != expected_interactions:
        raise B3TemporalContextError("B2 interaction artifacts do not match chapter index")

    prefix = _load_bound_prefix_bundle(seal)
    enriched_cards = _enrich_candidate_cards(
        candidate_cards=candidate_cards,
        prefix_bundle=prefix,
        chapter_id=chapter_id,
    )
    parked_index_hash = None
    if parked_identity_index is not None:
        if (
            parked_identity_index.get("schema_version")
            == PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2
        ):
            verified_parked = verify_parked_identity_index_v2(parked_identity_index)
            enriched_cards = attach_parked_identities_to_candidate_cards_v2(
                candidate_cards=enriched_cards,
                index=verified_parked,
            )
        else:
            verified_parked = verify_parked_identity_index_v1(parked_identity_index)
            enriched_cards = attach_parked_identity_to_candidate_cards_v1(
                candidate_cards=enriched_cards,
                index=verified_parked,
            )
        parked_index_hash = verified_parked["index_hash"]

    turns = [
        deepcopy(_mapping(row, "B2 speaker turn"))
        for row in artifact.get("speaker_turns") or []
    ]
    events = [
        deepcopy(_mapping(row, "B2 salient event"))
        for row in artifact.get("salient_events") or []
    ]
    _validate_b2_evidence_ids(turns, events, set(enriched_cards))
    review_requests = deepcopy(list(artifact.get("review_requests") or []))
    (
        turns,
        review_requests,
        speaker_recovery_binding,
        local_recovery_cards,
    ) = _apply_speaker_recovery_v1(
        recovery_root=speaker_recovery_root,
        chapter_artifact=artifact,
        turns=turns,
        review_requests=review_requests,
        allowed_candidate_card_ids=set(enriched_cards),
    )
    recovered_cards = _enrich_local_recovery_cards_v1(
        candidate_cards=local_recovery_cards,
        prefix_bundle=prefix,
        chapter_id=chapter_id,
    )
    if set(enriched_cards).intersection(recovered_cards):
        raise B3TemporalContextError(
            "speaker recovery local card collides with a B2 candidate"
        )
    enriched_cards.update(recovered_cards)
    candidate_to_ref = {
        card_id: card["referent_ref"] for card_id, card in enriched_cards.items()
    }
    all_candidate_ids = set(candidate_to_ref)
    _validate_b2_evidence_ids(turns, events, all_candidate_ids)

    frames = [
        deepcopy(_mapping(row, "B2 frame segment"))
        for row in artifact.get("frame_segments") or []
    ]
    frame_by_block: dict[str, str] = {}
    frame_by_id: dict[str, dict[str, Any]] = {}
    for frame in frames:
        frame_id = _required_string(frame.get("frame_segment_id"), "frame_segment_id")
        if frame_id in frame_by_id:
            raise B3TemporalContextError("duplicate B2 frame segment id")
        frame_by_id[frame_id] = frame
        for block_id in frame.get("covered_block_ids") or []:
            block_id = _required_string(block_id, "frame covered block_id")
            if block_id in frame_by_block:
                raise B3TemporalContextError("B2 frame segments overlap")
            frame_by_block[block_id] = frame_id
    coverage = _mapping(artifact.get("active_block_coverage"), "B2 coverage")
    covered = list(coverage.get("covered_block_ids") or [])
    if set(covered) != set(source_blocks) or len(covered) != len(set(covered)):
        raise B3TemporalContextError("B2 source blocks do not exact-cover chapter artifact")
    if set(frame_by_block) != set(source_blocks):
        raise B3TemporalContextError("B2 frame segments do not exact-cover source blocks")

    input_body = {
        "schema_version": B3_INPUT_SCHEMA_VERSION_V1,
        "source_b2_run_root": str(root),
        "source_b2_seal_hash": seal["seal_hash"],
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "source_b2_request_fingerprints": sorted(request_fingerprints),
        "source_prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "source_document_sha256": artifact.get("source_document_sha256"),
        "chapter_id": chapter_id,
        "frame_segments": frames,
        "speaker_turns": turns,
        "salient_events": events,
        "review_requests": review_requests,
        "speaker_recovery_binding": speaker_recovery_binding,
        "source_blocks": [source_blocks[block_id] for block_id in covered],
        "candidate_cards": [enriched_cards[key] for key in sorted(enriched_cards)],
        "candidate_card_to_referent_ref": candidate_to_ref,
        "parked_identity_index_hash": parked_index_hash,
        "identity_lineage_id": prefix.get("state_lineage_id"),
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**input_body, "input_hash": canonical_hash(input_body)}


def build_b3_temporal_components_v1(
    *,
    temporal_input: Mapping[str, Any],
    profile: B3TemporalProfileV1,
    prior_states: Sequence[Mapping[str, Any]] = (),
    prior_pending_cases: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    chapter_id = _required_string(temporal_input.get("chapter_id"), "chapter_id")
    timeline_reviews: list[dict[str, Any]] = []
    for raw_review in temporal_input.get("review_requests") or []:
        review = deepcopy(_mapping(raw_review, "B2 review request"))
        try:
            destination = route_review(review)
        except (KeyError, ReviewRoutingError) as exc:
            raise B3TemporalContextError(
                "B2 review reached B3 without a valid typed route"
            ) from exc
        if destination == "E":
            raise B3TemporalContextError(
                "frame-structure review reached B3 temporal intake"
            )
        if destination == "D":
            timeline_reviews.append(review)
    card_by_id = {
        _required_string(row.get("candidate_card_id"), "candidate_card_id"): deepcopy(
            _mapping(row, "candidate card")
        )
        for row in temporal_input.get("candidate_cards") or []
    }
    card_by_ref = {row["referent_ref"]: row for row in card_by_id.values()}
    candidate_to_ref = dict(temporal_input.get("candidate_card_to_referent_ref") or {})
    source_by_id = {
        _required_string(row.get("block_id"), "block_id"): deepcopy(
            _mapping(row, "source block")
        )
        for row in temporal_input.get("source_blocks") or []
    }
    frame_by_id = {
        _required_string(row.get("frame_segment_id"), "frame_segment_id"): deepcopy(
            _mapping(row, "frame segment")
        )
        for row in temporal_input.get("frame_segments") or []
    }
    frame_by_block = {
        str(block_id): frame_id
        for frame_id, row in frame_by_id.items()
        for block_id in row.get("covered_block_ids") or []
    }
    block_order = {block_id: index for index, block_id in enumerate(source_by_id)}
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    assigned_turns: set[str] = set()
    assigned_events: set[str] = set()

    for raw_turn in temporal_input.get("speaker_turns") or []:
        turn = _compact_turn(_mapping(raw_turn, "speaker turn"), candidate_to_ref)
        turn_id = turn["speaker_turn_id"]
        if turn_id in assigned_turns:
            raise B3TemporalContextError("speaker turn id is repeated")
        assigned_turns.add(turn_id)
        refs = sorted(
            set(turn["speaker"]["referent_refs"] + turn["addressee"]["referent_refs"])
        )
        unresolved_key = _turn_unresolved_key(turn) if not refs else ""
        kind = "relationship_evidence" if len(refs) >= 2 else (
            "entity_state_evidence" if refs else "unresolved_evidence"
        )
        key = (kind, tuple(refs), unresolved_key)
        component = groups.setdefault(key, _new_component_seed(chapter_id, kind, refs, key))
        component["speaker_turns"].append(turn)
        component["source_block_ids"].add(turn["block_id"])
        component["frame_segment_ids"].add(frame_by_block[turn["block_id"]])

    for raw_event in temporal_input.get("salient_events") or []:
        event = _compact_event(_mapping(raw_event, "salient event"), candidate_to_ref)
        event_id = event["salient_event_id"]
        if event_id in assigned_events:
            raise B3TemporalContextError("salient event id is repeated")
        assigned_events.add(event_id)
        refs = sorted(
            {
                ref
                for participant in event["participants"]
                for ref in participant["referent_refs"]
            }
        )
        domain_hint = _event_domain_hint(event["event_kind"])
        if domain_hint == "relationship" and refs:
            kind = "relationship_evidence"
            key = (kind, tuple(refs), "")
        elif domain_hint == "world_state":
            kind = "world_state_evidence"
            key = (kind, event["event_scope"], tuple(refs), event_id if not refs else "")
        elif refs:
            kind = "entity_state_evidence"
            key = (kind, domain_hint, tuple(refs))
        else:
            kind = "unresolved_evidence"
            key = (kind, domain_hint, event_id)
        component = groups.setdefault(key, _new_component_seed(chapter_id, kind, refs, key))
        component["domain_hints"].add(domain_hint)
        component["salient_events"].append(event)
        for block_id in event["source_block_ids"]:
            component["source_block_ids"].add(block_id)
            component["frame_segment_ids"].add(frame_by_block[block_id])

    normalized_prior = [_normalized_prior_state(row) for row in prior_states]
    normalized_pending = (
        None
        if prior_pending_cases is None
        else [_normalized_prior_pending_case(row) for row in prior_pending_cases]
    )
    components: list[dict[str, Any]] = []
    for seed in groups.values():
        refs = set(seed["referent_refs"])
        relevant_prior = [
            row
            for row in normalized_prior
            if row["lifecycle_status"] == "open"
            and row["authority_status"] == "effective"
            and _state_referents(row)
            and _state_referents(row) <= refs
        ]
        relevant_pending = (
            []
            if normalized_pending is None
            else [
                row
                for row in normalized_pending
                if _pending_referents(row)
                and _pending_referents(row) <= refs
            ]
        )
        if (
            len(relevant_prior) + len(relevant_pending)
            > profile.max_prior_states_per_component
        ):
            raise B3TemporalBudgetError("B3 prior-state context exceeds component cap")
        turns = sorted(
            seed["speaker_turns"],
            key=lambda row: (block_order[row["block_id"]], row["speaker_turn_id"]),
        )
        events = sorted(
            seed["salient_events"],
            key=lambda row: (
                block_order[row["anchor_block_id"]],
                row["salient_event_id"],
            ),
        )
        block_ids = sorted(seed["source_block_ids"], key=block_order.__getitem__)
        frame_ids = sorted(
            seed["frame_segment_ids"],
            key=lambda value: block_order[
                next(
                    block_id
                    for block_id in frame_by_id[value]["covered_block_ids"]
                    if block_id in seed["source_block_ids"]
                )
            ],
        )
        _enforce_component_caps(
            turns=turns,
            events=events,
            block_ids=block_ids,
            profile=profile,
        )
        cards = [
            _compact_card(card_by_ref[ref], profile.provenance_refs_per_card)
            for ref in sorted(refs)
            if ref in card_by_ref
        ]
        if len(cards) != len(refs):
            raise B3TemporalContextError("component referent lacks candidate card")
        review_rows = _relevant_review_rows(
            timeline_reviews,
            component_refs=refs,
            component_block_ids=set(block_ids),
            candidate_to_ref=candidate_to_ref,
        )
        body = {
            "schema_version": B3_COMPONENT_SCHEMA_VERSION_V1,
            "chapter_id": chapter_id,
            "component_kind": seed["component_kind"],
            "domain_hints": sorted(seed["domain_hints"]),
            "referent_refs": sorted(refs),
            "candidate_cards": cards,
            "speaker_turns": turns,
            "salient_events": events,
            "source_blocks": [source_by_id[block_id] for block_id in block_ids],
            "frame_segments": [
                _compact_frame(frame_by_id[frame_id], set(block_ids), candidate_to_ref)
                for frame_id in frame_ids
            ],
            "prior_open_states": relevant_prior,
            "b2_review_requests": review_rows,
        }
        if normalized_pending is not None:
            body["prior_pending_cases"] = relevant_pending
        component_id = "b3comp1_" + canonical_hash(
            {
                "chapter_id": chapter_id,
                "component_kind": seed["component_kind"],
                "referent_refs": sorted(refs),
                "speaker_turn_ids": [row["speaker_turn_id"] for row in turns],
                "salient_event_ids": [row["salient_event_id"] for row in events],
            }
        )[:20]
        components.append(
            {"component_id": component_id, **body, "component_hash": canonical_hash(body)}
        )

    components.sort(key=lambda row: (_component_first_block(row, block_order), row["component_id"]))
    for index, row in enumerate(components, 1):
        row["component_ordinal"] = index
    return components


def build_b3_temporal_phase_a_bundle_v1(
    *,
    temporal_input: Mapping[str, Any],
    profile: B3TemporalProfileV1,
    prior_states: Sequence[Mapping[str, Any]] = (),
    prior_pending_cases: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    components = build_b3_temporal_components_v1(
        temporal_input=temporal_input,
        profile=profile,
        prior_states=prior_states,
        prior_pending_cases=prior_pending_cases,
    )
    chapter_id = _required_string(temporal_input.get("chapter_id"), "chapter_id")
    batches = _pack_components_v1(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
    )
    requests = [
        _render_b3_batch_request_v1(
            temporal_input=temporal_input,
            components=batch,
            profile=profile,
            batch_ordinal=index,
        )
        for index, batch in enumerate(batches, 1)
    ]
    if len(requests) > profile.max_requests_per_chapter:
        raise B3TemporalBudgetError("B3 request count exceeds per-chapter cap")
    covered = [
        component_id
        for request in requests
        for component_id in request["component_ids"]
    ]
    expected = [row["component_id"] for row in components]
    if set(covered) != set(expected) or len(covered) != len(set(covered)):
        raise B3TemporalContextError("B3 requests do not exact-cover components")

    total_prompt = sum(
        int(row["token_reserve"]["prompt_token_reserve"]) for row in requests
    )
    total_output = sum(
        int(row["token_reserve"]["output_token_cap"]) for row in requests
    )
    plan_body = {
        "schema_version": B3_PLAN_SCHEMA_VERSION_V1,
        "phase": "phase_a_zero_api",
        "chapter_id": chapter_id,
        "source_input_hash": temporal_input.get("input_hash"),
        "source_b2_artifact_hash": temporal_input.get("source_b2_artifact_hash"),
        "source_prefix_bundle_hash": temporal_input.get("source_prefix_bundle_hash"),
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "profile_sha256": profile.profile_sha256,
        "role_id": profile.role_id,
        "recommended_preset_id": profile.recommended_preset_id,
        "recommended_model": profile.recommended_model,
        "component_count": len(components),
        "request_count": len(requests),
        "requests": [
            {
                "batch_id": row["batch_id"],
                "component_ids": list(row["component_ids"]),
                "request_fingerprint": row["request_fingerprint"],
                "token_reserve": deepcopy(row["token_reserve"]),
            }
            for row in requests
        ],
        "token_reserve": {
            "prompt_token_reserve": total_prompt,
            "output_token_reserve": total_output,
            "total_token_reserve": total_prompt + total_output,
        },
        "api_calls_performed": 0,
        "gold_or_oracle_loaded": False,
        "historical_artifact_mutated": False,
        "production_publish_performed": False,
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    return {"plan": plan, "components": components, "requests": requests}


def refresh_b3_temporal_component_prior_context_v1(
    *,
    component: Mapping[str, Any],
    profile: B3TemporalProfileV1,
    prior_states: Sequence[Mapping[str, Any]],
    prior_pending_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Refresh only cross-chapter temporal context without regrouping evidence."""

    row = deepcopy(_mapping(component, "B3 component"))
    component_id = _required_string(row.pop("component_id", None), "component_id")
    component_hash = _required_string(row.pop("component_hash", None), "component_hash")
    ordinal = row.pop("component_ordinal", None)
    if canonical_hash(row) != component_hash:
        raise B3TemporalContextError("B3 component hash mismatch before prior refresh")
    refs = set(row.get("referent_refs") or [])
    normalized_prior = [_normalized_prior_state(value) for value in prior_states]
    normalized_pending = [
        _normalized_prior_pending_case(value) for value in prior_pending_cases
    ]
    relevant_prior = [
        value
        for value in normalized_prior
        if value["lifecycle_status"] == "open"
        and value["authority_status"] == "effective"
        and _state_referents(value)
        and _state_referents(value) <= refs
    ]
    relevant_pending = [
        value
        for value in normalized_pending
        if _pending_referents(value) and _pending_referents(value) <= refs
    ]
    if (
        len(relevant_prior) + len(relevant_pending)
        > profile.max_prior_states_per_component
    ):
        raise B3TemporalBudgetError("B3 refreshed prior context exceeds component cap")
    row["prior_open_states"] = relevant_prior
    row["prior_pending_cases"] = relevant_pending
    refreshed = {
        "component_id": component_id,
        **row,
        "component_hash": canonical_hash(row),
    }
    if ordinal is not None:
        refreshed["component_ordinal"] = ordinal
    return refreshed


def render_b3_temporal_phase_a_batch_v1(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
    batch_ordinal: int,
) -> dict[str, Any]:
    """Public deterministic renderer used by sequential chapter runners."""

    return _render_b3_batch_request_v1(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
        batch_ordinal=batch_ordinal,
    )


def _pack_components_v1(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
) -> list[list[Mapping[str, Any]]]:
    # First-fit decreasing avoids an extra API call caused only by source-order
    # packing. Components remain semantically independent and are restored to
    # source order inside each final batch.
    weighted = sorted(
        components,
        key=lambda row: (-len(canonical_json(row)), int(row["component_ordinal"])),
    )
    bins: list[list[Mapping[str, Any]]] = []
    for component in weighted:
        placed = False
        for index, batch in enumerate(bins):
            if len(batch) >= profile.max_components_per_request:
                continue
            trial = sorted(
                [*batch, component], key=lambda row: int(row["component_ordinal"])
            )
            try:
                _render_b3_batch_request_v1(
                    temporal_input=temporal_input,
                    components=trial,
                    profile=profile,
                    batch_ordinal=index + 1,
                )
            except B3TemporalBudgetError:
                continue
            bins[index] = trial
            placed = True
            break
        if placed:
            continue
        trial = [component]
        try:
            _render_b3_batch_request_v1(
                temporal_input=temporal_input,
                components=trial,
                profile=profile,
                batch_ordinal=len(bins) + 1,
            )
        except B3TemporalBudgetError as exc:
            raise B3TemporalBudgetError(
                f"single B3 component exceeds prompt cap: {component['component_id']}"
            ) from exc
        bins.append(trial)
    bins.sort(key=lambda batch: min(int(row["component_ordinal"]) for row in batch))
    return bins


def _render_b3_batch_request_v1(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
    batch_ordinal: int,
) -> dict[str, Any]:
    material = build_b3_temporal_batch_payload_v1(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
        batch_ordinal=batch_ordinal,
    )
    chapter_id = material["chapter_id"]
    batch_id = material["batch_id"]
    component_ids = material["component_ids"]
    user_payload = material["user_payload"]
    response_schema = bind_b3_temporal_response_schema_v1(
        chapter_id=chapter_id,
        batch_id=batch_id,
        component_ids=component_ids,
        referent_refs=material["referent_refs"],
        event_ids=material["event_ids"],
        turn_ids=material["turn_ids"],
        block_ids=material["block_ids"],
        frame_segment_ids=material["frame_segment_ids"],
    )
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V1},
        {"role": "user", "content": canonical_json(user_payload)},
    ]
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 rendered prompt exceeds configured cap")
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V1,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V1,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V1.encode("utf-8")
        ).hexdigest(),
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "component_ids": component_ids,
        "messages": messages,
        "response_schema": response_schema,
        "response_schema_hash": canonical_hash(response_schema),
        "token_reserve": reserve,
        "configured_prompt_cap": profile.prompt_tokens_per_request,
        "configured_output_cap": profile.output_tokens_per_request,
        "api_eligible": False,
        "api_ineligible_reasons": ["phase_a_zero_api"],
        "context_hashes": material["context_hashes"],
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**body, "request_fingerprint": canonical_hash(body)}


def build_b3_temporal_batch_payload_v1(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
    batch_ordinal: int,
) -> dict[str, Any]:
    """Build provider-neutral batch material without measuring a prompt version."""

    if not components:
        raise B3TemporalContextError("cannot render an empty B3 batch")
    chapter_id = _required_string(temporal_input.get("chapter_id"), "chapter_id")
    component_ids = [str(row["component_id"]) for row in components]
    batch_id = (
        f"b3batch1_{_safe_id(chapter_id)}_{batch_ordinal:02d}_"
        + canonical_hash(component_ids)[:12]
    )
    referent_refs = sorted(
        {ref for component in components for ref in component.get("referent_refs") or []}
    )
    if len(referent_refs) > profile.max_candidate_cards_per_request:
        raise B3TemporalBudgetError("B3 candidate cards exceed request cap")
    event_ids = sorted(
        {
            row["salient_event_id"]
            for component in components
            for row in component.get("salient_events") or []
        }
    )
    turn_ids = sorted(
        {
            row["speaker_turn_id"]
            for component in components
            for row in component.get("speaker_turns") or []
        }
    )
    block_ids = sorted(
        {
            row["block_id"]
            for component in components
            for row in component.get("source_blocks") or []
        }
    )
    frame_ids = sorted(
        {
            row["frame_segment_id"]
            for component in components
            for row in component.get("frame_segments") or []
        }
    )
    user_payload = {
        "request_kind": "chapter_temporal_state_batch",
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "source_b2_artifact_hash": temporal_input.get("source_b2_artifact_hash"),
        "source_prefix_bundle_hash": temporal_input.get("source_prefix_bundle_hash"),
        **_deduplicated_batch_context(components),
    }
    return {
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "component_ids": component_ids,
        "referent_refs": referent_refs,
        "event_ids": event_ids,
        "turn_ids": turn_ids,
        "block_ids": block_ids,
        "frame_segment_ids": frame_ids,
        "user_payload": user_payload,
        "context_hashes": {
            "source_input_hash": temporal_input.get("input_hash"),
            "source_b2_artifact_hash": temporal_input.get("source_b2_artifact_hash"),
            "source_prefix_bundle_hash": temporal_input.get("source_prefix_bundle_hash"),
            "component_hashes": [row["component_hash"] for row in components],
        },
    }


def _deduplicated_batch_context(
    components: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    referent_packets: dict[str, dict[str, Any]] = {}
    source_packets: dict[str, dict[str, Any]] = {}
    frame_packets: dict[str, dict[str, Any]] = {}
    compact_components: list[dict[str, Any]] = []
    for component in components:
        component_id = str(component["component_id"])
        for raw_card in component.get("candidate_cards") or []:
            card = deepcopy(dict(raw_card))
            ref = str(card["referent_ref"])
            packet = referent_packets.setdefault(
                ref, {"referent_ref": ref, "component_ids": [], "candidate_card": card}
            )
            if canonical_json(packet["candidate_card"]) != canonical_json(card):
                raise B3TemporalContextError("B3 referent card drifted across components")
            packet["component_ids"].append(component_id)
        for raw_block in component.get("source_blocks") or []:
            block = deepcopy(dict(raw_block))
            block_id = str(block["block_id"])
            packet = source_packets.setdefault(
                block_id,
                {
                    "block_id": block_id,
                    "component_ids": [],
                    "source_text_sha256": hashlib.sha256(
                        block["text"].encode("utf-8")
                    ).hexdigest(),
                },
            )
            if packet["source_text_sha256"] != hashlib.sha256(
                block["text"].encode("utf-8")
            ).hexdigest():
                raise B3TemporalContextError("B3 source block drifted across components")
            packet["component_ids"].append(component_id)
        for raw_frame in component.get("frame_segments") or []:
            frame = deepcopy(dict(raw_frame))
            frame_id = str(frame["frame_segment_id"])
            packet = frame_packets.setdefault(
                frame_id,
                {
                    "frame_segment_id": frame_id,
                    "component_ids": [],
                    "frame": frame,
                },
            )
            if canonical_json(packet["frame"]) != canonical_json(frame):
                # Relevant block lists legitimately differ; merge only that
                # mechanical subset while requiring all semantic fields equal.
                left = dict(packet["frame"])
                right = dict(frame)
                left_blocks = set(left.pop("relevant_block_ids", []))
                right_blocks = set(right.pop("relevant_block_ids", []))
                if canonical_json(left) != canonical_json(right):
                    raise B3TemporalContextError("B3 frame context drifted across components")
                packet["frame"]["relevant_block_ids"] = sorted(
                    left_blocks.union(right_blocks)
                )
            packet["component_ids"].append(component_id)
        compact_components.append(
            {
                "component_id": component_id,
                "component_hash": component["component_hash"],
                "component_kind": component["component_kind"],
                "domain_hints": list(component.get("domain_hints") or []),
                "referent_refs": list(component.get("referent_refs") or []),
                "frame_segment_ids": sorted(
                    {
                        str(row["frame_segment_id"])
                        for row in component.get("frame_segments") or []
                    }
                ),
                "speaker_turns": deepcopy(list(component.get("speaker_turns") or [])),
                "salient_events": deepcopy(list(component.get("salient_events") or [])),
                "prior_open_states": deepcopy(list(component.get("prior_open_states") or [])),
                "b2_review_requests": deepcopy(
                    list(component.get("b2_review_requests") or [])
                ),
                **(
                    {
                        "prior_pending_cases": deepcopy(
                            list(component.get("prior_pending_cases") or [])
                        )
                    }
                    if "prior_pending_cases" in component
                    else {}
                ),
            }
        )
    for packets in (referent_packets, source_packets, frame_packets):
        for packet in packets.values():
            packet["component_ids"] = sorted(set(packet["component_ids"]))
    supplied_referent_refs = set(referent_packets)
    for packet in referent_packets.values():
        markers = packet["candidate_card"].get("parked_identities")
        if isinstance(markers, list):
            for marker in markers:
                if not isinstance(marker, Mapping):
                    continue
                _trim_parked_marker_v1(
                    marker=marker,
                    supplied_referent_refs=supplied_referent_refs,
                )
            continue
        marker = packet["candidate_card"].get("parked_identity")
        if isinstance(marker, Mapping):
            _trim_parked_marker_v1(
                marker=marker,
                supplied_referent_refs=supplied_referent_refs,
            )
    return {
        "components": compact_components,
        "referent_packets": [referent_packets[key] for key in sorted(referent_packets)],
        "source_packets": [source_packets[key] for key in sorted(source_packets)],
        "frame_packets": [frame_packets[key] for key in sorted(frame_packets)],
    }


def _apply_speaker_recovery_v1(
    *,
    recovery_root: Path | None,
    chapter_artifact: Mapping[str, Any],
    turns: Sequence[Mapping[str, Any]],
    review_requests: Sequence[Mapping[str, Any]],
    allowed_candidate_card_ids: set[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    list[dict[str, Any]],
]:
    if recovery_root is None:
        return deepcopy(list(turns)), deepcopy(list(review_requests)), None, []
    root = Path(recovery_root).resolve()
    if not root.is_dir():
        raise B3TemporalContextError(f"speaker recovery root is absent: {root}")
    recovery = _verified_hashed_object(
        root / "speaker_recovery_artifact.json",
        "artifact_hash",
        "B2 speaker recovery artifact",
    )
    recovery_index = None
    if recovery.get("registry_recovery_ledger") is not None:
        recovery_index = _verified_hashed_object(
            root / "recovery_index.json",
            "recovery_index_hash",
            "B2 speaker recovery index",
        )
        try:
            recovery_index = verify_b2_recovery_index_v1(recovery_index)
        except B2RecoveryError as exc:
            raise B3TemporalContextError(
                "B2 speaker recovery index is invalid"
            ) from exc
    try:
        recovery = verify_b2_slim_speaker_recovery_artifact_v1(
            chapter_artifact=chapter_artifact,
            recovery_artifact=recovery,
            allowed_candidate_card_ids=allowed_candidate_card_ids,
            recovery_index=recovery_index,
        )
    except B2SlimSpeakerRecoveryError as exc:
        raise B3TemporalContextError(str(exc)) from exc
    if recovery.get("schema_version") != B2_SPEAKER_RECOVERY_ARTIFACT_SCHEMA_VERSION:
        raise B3TemporalContextError("foreign B2 speaker recovery schema")
    report = _verified_hashed_object(
        root / "canary_report.json",
        "report_hash",
        "B2 speaker recovery report",
    )
    if (
        report.get("schema_version")
        != "literary_b2_speaker_recovery_canary_report_v1"
        or report.get("status") != "semantic_accepted"
        or report.get("chapter_id") != chapter_artifact.get("chapter_id")
        or report.get("source_b2_artifact_hash")
        != chapter_artifact.get("artifact_hash")
        or report.get("speaker_recovery_artifact_hash")
        != recovery.get("artifact_hash")
        or report.get("mandatory_stop_observed") is not True
    ):
        raise B3TemporalContextError("B2 speaker recovery report lineage differs")
    for field in (
        "accepted_turn_reinspection_performed",
        "unticketed_turn_mutation_performed",
        "source_artifact_mutated",
        "identity_or_claim_mutation_performed",
        "book_global_authority_granted",
        "production_publish_performed",
    ):
        if report.get(field) is not False:
            raise B3TemporalContextError(
                f"B2 speaker recovery report safety flag differs: {field}"
            )

    speaker_overlay_by_turn = {
        row["speaker_turn_id"]: row for row in recovery["speaker_overlays"]
    }
    addressee_overlay_by_turn = {
        row["speaker_turn_id"]: row for row in recovery.get("addressee_overlays") or []
    }
    effective_turns: list[dict[str, Any]] = []
    for raw_turn in turns:
        turn = deepcopy(dict(raw_turn))
        speaker_overlay = speaker_overlay_by_turn.get(turn["speaker_turn_id"])
        addressee_overlay = addressee_overlay_by_turn.get(turn["speaker_turn_id"])
        if speaker_overlay is not None:
            turn["speaker_recovery_overlay_id"] = speaker_overlay["overlay_id"]
            turn["speaker_recovery_action"] = speaker_overlay["action"]
            turn["speaker_recovery_authority_status"] = speaker_overlay[
                "authority_status"
            ]
            if speaker_overlay["action"] in {
                "attach_existing",
                "create_chapter_local",
            }:
                turn["speaker"] = deepcopy(speaker_overlay["effective_speaker"])
                turn["speaker_authority_status"] = speaker_overlay[
                    "authority_status"
                ]
                turn["row_status"] = "accepted_observation"
            else:
                turn["speaker_authority_status"] = "pending_review"
        if addressee_overlay is not None:
            turn["addressee_recovery_overlay_id"] = addressee_overlay["overlay_id"]
            turn["addressee_recovery_action"] = addressee_overlay["action"]
            turn["addressee_recovery_authority_status"] = addressee_overlay[
                "authority_status"
            ]
            if addressee_overlay["action"] in {
                "attach_existing",
                "create_chapter_local",
            }:
                turn["addressee"] = deepcopy(
                    addressee_overlay["effective_addressee"]
                )
                turn["addressee_authority_status"] = addressee_overlay[
                    "authority_status"
                ]
            else:
                turn["addressee_authority_status"] = "pending_review"
        effective_turns.append(turn)

    review_by_id: dict[str, dict[str, Any]] = {}
    for raw_review in review_requests:
        review = deepcopy(_mapping(raw_review, "B2 review request"))
        review_id = _required_string(review.get("review_id"), "review_id")
        if review_id in review_by_id:
            raise B3TemporalContextError("B2 review request id repeats")
        review_by_id[review_id] = review
    disposition_by_id: dict[str, dict[str, Any]] = {}
    for raw_disposition in recovery.get("review_dispositions") or []:
        disposition = deepcopy(_mapping(raw_disposition, "review disposition"))
        review_id = _required_string(disposition.get("review_id"), "review_id")
        if review_id not in review_by_id or review_id in disposition_by_id:
            raise B3TemporalContextError("speaker recovery review disposition differs")
        disposition_by_id[review_id] = disposition
    effective_reviews: list[dict[str, Any]] = []
    for review_id, review in review_by_id.items():
        disposition = disposition_by_id.get(review_id)
        if disposition is not None and disposition.get("status") in {
            "resolved",
            "unresolved_ambiguous",
        }:
            continue
        if disposition is not None:
            review["status"] = "pending"
            review["speaker_recovery_ticket_id"] = disposition["ticket_id"]
            review["speaker_recovery_action"] = disposition["decision_action"]
        effective_reviews.append(review)

    binding_body = {
        "schema_version": "literary_b3_b2_speaker_recovery_binding_v1",
        "source_root": str(root),
        "chapter_id": recovery["chapter_id"],
        "source_b2_artifact_hash": recovery["source_b2_artifact_hash"],
        "speaker_recovery_artifact_hash": recovery["artifact_hash"],
        "speaker_recovery_report_hash": report["report_hash"],
        "ticketed_speaker_turn_ids": list(recovery["ticketed_speaker_turn_ids"]),
        "attached_turn_count": sum(
            row["action"] == "attach_existing"
            for row in recovery["speaker_overlays"]
        ),
        "created_local_turn_count": sum(
            row["action"] == "create_chapter_local"
            for row in recovery["speaker_overlays"]
        ),
        "pending_turn_count": sum(
            row["action"] == "keep_pending" for row in recovery["speaker_overlays"]
        ),
        "attached_addressee_count": sum(
            row["action"] == "attach_existing"
            for row in recovery.get("addressee_overlays") or []
        ),
        "created_local_addressee_count": sum(
            row["action"] == "create_chapter_local"
            for row in recovery.get("addressee_overlays") or []
        ),
        "pending_addressee_count": sum(
            row["action"] == "keep_pending"
            for row in recovery.get("addressee_overlays") or []
        ),
        "unresolved_ambiguity_count": sum(
            row.get("status") == "unresolved_ambiguous"
            for row in recovery.get("review_dispositions") or []
        ),
        "identity_or_book_authority_granted": False,
    }
    binding = {**binding_body, "binding_hash": canonical_hash(binding_body)}
    raw_registry_ledger = recovery.get("registry_recovery_ledger")
    local_cards = (
        []
        if raw_registry_ledger is None
        else [
            deepcopy(_mapping(row, "speaker recovery local candidate card"))
            for row in _mapping(
                raw_registry_ledger,
                "speaker recovery registry ledger",
            ).get("local_candidate_cards")
            or []
        ]
    )
    return effective_turns, effective_reviews, binding, local_cards


def _load_bound_prefix_bundle(seal: Mapping[str, Any]) -> dict[str, Any]:
    source_root = Path(
        _required_string(seal.get("source_run_root"), "B2 source_run_root")
    ).resolve()
    expected_hash = _required_string(
        seal.get("source_prefix_bundle_hash"), "source_prefix_bundle_hash"
    )
    package_path = source_root / B1_REGISTRY_B2_PACKAGE_FILENAME
    if package_path.is_file():
        try:
            package = verify_b2_registry_input_package_v1(
                _read_object(package_path, "B1 registry-to-B2 package")
            )
        except B1RegistryToB2InputError as exc:
            raise B3TemporalContextError(str(exc)) from exc
        if package.get("source_document_sha256") != seal.get(
            "source_document_sha256"
        ):
            raise B3TemporalContextError(
                "B1 registry package document differs from B2 seal"
            )
        matches = [
            row
            for row in package.get("chapters") or []
            if row.get("chapter_id") == seal.get("chapter_id")
            and row.get("prefix_bundle_hash") == expected_hash
        ]
        if len(matches) != 1:
            raise B3TemporalContextError(
                "B3 could not resolve exactly one registry prefix bound by the B2 seal"
            )
        prefix = deepcopy(matches[0]["prefix_bundle"])
        prefix["state_lineage_id"] = "litb1lineage1_" + canonical_hash(
            {
                "source_document_sha256": package["source_document_sha256"],
                "lineage_policy": "stable_registry_entity_id_v1",
            }
        )[:24]
        for table in (
            "b0_context_cards",
            "active_context_cards",
            "candidate_only_context_cards",
        ):
            for card in prefix.get(table) or []:
                card["source_candidate_id"] = _required_string(
                    card.get("prior_card_id"), "prior_card_id"
                )
        return prefix

    matches: list[dict[str, Any]] = []
    if source_root.is_dir():
        for path in source_root.glob("artifacts/chapters/*/final_prefix.json"):
            row = _read_object(path, "B1 final prefix")
            if row.get("prefix_bundle_hash") == expected_hash:
                _verify_inline_hash(row, "prefix_bundle_hash", "B1 final prefix")
                matches.append(row)
    if len(matches) != 1:
        raise B3TemporalContextError(
            "B3 could not resolve exactly one B1 prefix bound by the B2 seal"
        )
    return matches[0]


def _enrich_candidate_cards(
    *,
    candidate_cards: Mapping[str, Mapping[str, Any]],
    prefix_bundle: Mapping[str, Any],
    chapter_id: str,
) -> dict[str, dict[str, Any]]:
    prefix_cards: dict[str, dict[str, Any]] = {}
    for table in (
        "b0_context_cards",
        "active_context_cards",
        "candidate_only_context_cards",
    ):
        for raw in prefix_bundle.get(table) or []:
            row = deepcopy(_mapping(raw, f"prefix {table} card"))
            card_id = _required_string(row.get("prior_card_id"), "prior_card_id")
            prior = prefix_cards.setdefault(card_id, row)
            if canonical_json(prior) != canonical_json(row):
                raise B3TemporalContextError("prefix candidate card id is ambiguous")
    lineage_id = _required_string(
        prefix_bundle.get("state_lineage_id"), "state_lineage_id"
    )
    result: dict[str, dict[str, Any]] = {}
    for card_id, raw_card in candidate_cards.items():
        prefix_card = prefix_cards.get(card_id)
        if prefix_card is None:
            raise B3TemporalContextError("B2 candidate is absent from bound B1 prefix")
        source_candidate_id = _required_string(
            prefix_card.get("source_candidate_id"), "source_candidate_id"
        )
        origin_chapter_id = chapter_id
        for provenance in prefix_card.get("provenance_refs") or []:
            if isinstance(provenance, Mapping) and provenance.get("chapter_id"):
                origin_chapter_id = str(provenance["chapter_id"])
                break
        referent_ref = "litref1_" + canonical_hash(
            {
                "state_lineage_id": lineage_id,
                "source_candidate_id": source_candidate_id,
            }
        )[:20]
        result[card_id] = {
            **deepcopy(dict(raw_card)),
            "referent_ref": referent_ref,
            "referent_ref_kind": "candidate_lineage_ref",
            "identity_scope": prefix_card.get("authority_scope"),
            "source_candidate_id": source_candidate_id,
            "origin_chapter_id": origin_chapter_id,
        }
    return result


def _enrich_local_recovery_cards_v1(
    *,
    candidate_cards: Sequence[Mapping[str, Any]],
    prefix_bundle: Mapping[str, Any],
    chapter_id: str,
) -> dict[str, dict[str, Any]]:
    lineage_id = _required_string(
        prefix_bundle.get("state_lineage_id"),
        "state_lineage_id",
    )
    result: dict[str, dict[str, Any]] = {}
    for raw_card in candidate_cards:
        card = deepcopy(_mapping(raw_card, "local recovery candidate card"))
        card_id = _required_string(
            card.get("candidate_card_id"),
            "local recovery candidate_card_id",
        )
        if (
            card_id in result
            or card.get("authority_scope") != "chapter_local_recovery"
            or card.get("stable_surfaces") != []
        ):
            raise B3TemporalContextError(
                "speaker recovery local candidate card is malformed"
            )
        referent_ref = "litref1_" + canonical_hash(
            {
                "state_lineage_id": lineage_id,
                "source_candidate_id": card_id,
            }
        )[:20]
        result[card_id] = {
            **card,
            "referent_ref": referent_ref,
            "referent_ref_kind": "candidate_lineage_ref",
            "identity_scope": "chapter_local_recovery",
            "source_candidate_id": card_id,
            "origin_chapter_id": chapter_id,
        }
    return result


def _validate_b2_evidence_ids(
    turns: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    allowed_candidate_ids: set[str],
) -> None:
    seen_turns: set[str] = set()
    for turn in turns:
        turn_id = _required_string(turn.get("speaker_turn_id"), "speaker_turn_id")
        if turn_id in seen_turns:
            raise B3TemporalContextError("duplicate speaker turn id")
        seen_turns.add(turn_id)
        for endpoint_name in ("speaker", "addressee"):
            endpoint = _mapping(turn.get(endpoint_name), endpoint_name)
            if not set(endpoint.get("candidate_card_ids") or []) <= allowed_candidate_ids:
                raise B3TemporalContextError("speaker turn references a foreign candidate")
    seen_events: set[str] = set()
    for event in events:
        event_id = _required_string(event.get("salient_event_id"), "salient_event_id")
        if event_id in seen_events:
            raise B3TemporalContextError("duplicate salient event id")
        seen_events.add(event_id)
        for participant in event.get("participants") or []:
            row = _mapping(participant, "event participant")
            if not set(row.get("candidate_card_ids") or []) <= allowed_candidate_ids:
                raise B3TemporalContextError("salient event references a foreign candidate")


def _new_component_seed(
    chapter_id: str, kind: str, refs: Sequence[str], key: tuple[Any, ...]
) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "component_kind": kind,
        "referent_refs": set(refs),
        "domain_hints": set(),
        "speaker_turns": [],
        "salient_events": [],
        "source_block_ids": set(),
        "frame_segment_ids": set(),
        "grouping_key_hash": canonical_hash(key),
    }


def _compact_turn(
    turn: Mapping[str, Any], candidate_to_ref: Mapping[str, str]
) -> dict[str, Any]:
    recovery_authority = turn.get("speaker_recovery_authority_status")
    clean = (
        turn.get("grounding_status") == "grounded"
        and turn.get("row_status") == "accepted_observation"
    )
    if recovery_authority == "auditor_confirmed_chapter_local":
        evidence_authority = "auditor_confirmed_chapter_local"
    elif recovery_authority == "pending_review":
        evidence_authority = "pending_review"
    else:
        evidence_authority = "provisional_grounded" if clean else "pending_review"
    return {
        "speaker_turn_id": _required_string(turn.get("speaker_turn_id"), "speaker_turn_id"),
        "block_id": _required_string(turn.get("block_id"), "turn block_id"),
        "utterance_anchor": _required_string(turn.get("utterance_anchor"), "utterance_anchor"),
        "speaker": _compact_endpoint(turn.get("speaker"), candidate_to_ref),
        "addressee": _compact_endpoint(turn.get("addressee"), candidate_to_ref),
        "address_terms": list(turn.get("address_terms") or []),
        "register_cue": turn.get("register_cue"),
        "delivery_tone": turn.get("delivery_tone"),
        "evidence_authority": evidence_authority,
    }


def _compact_event(
    event: Mapping[str, Any], candidate_to_ref: Mapping[str, str]
) -> dict[str, Any]:
    participants = []
    for raw in event.get("participants") or []:
        row = _mapping(raw, "event participant")
        participants.append(
            {
                "role": row.get("role"),
                "surface": row.get("surface"),
                "resolution_status": row.get("resolution_status"),
                "referent_refs": [
                    candidate_to_ref[value]
                    for value in row.get("candidate_card_ids") or []
                ],
            }
        )
    return {
        "salient_event_id": _required_string(
            event.get("salient_event_id"), "salient_event_id"
        ),
        "source_block_ids": list(event.get("source_block_ids") or []),
        "anchor_block_id": _required_string(
            event.get("anchor_block_id"), "event anchor_block_id"
        ),
        "event_anchor": _required_string(event.get("event_anchor"), "event_anchor"),
        "event_kind": _required_string(event.get("event_kind"), "event_kind"),
        "event_scope": event.get("event_scope"),
        "participants": participants,
        "summary": event.get("summary"),
        "memory_role": event.get("memory_role"),
        "event_status": event.get("event_status"),
        "evidence_mode": event.get("evidence_mode"),
        "review_status": event.get("review_status"),
        "event_authority_status": event.get("event_authority_status"),
    }


def _compact_endpoint(value: Any, candidate_to_ref: Mapping[str, str]) -> dict[str, Any]:
    row = _mapping(value, "B2 endpoint")
    return {
        "surface": row.get("surface"),
        "resolution_status": row.get("resolution_status"),
        "referent_refs": [
            candidate_to_ref[candidate_id]
            for candidate_id in row.get("candidate_card_ids") or []
        ],
    }


def _compact_card(card: Mapping[str, Any], provenance_cap: int) -> dict[str, Any]:
    result = {
        "referent_ref": card["referent_ref"],
        "referent_ref_kind": card["referent_ref_kind"],
        "identity_scope": card.get("identity_scope"),
        "candidate_card_id": card["candidate_card_id"],
        "canonical_surface": card.get("canonical_surface"),
        "stable_surfaces": list(card.get("stable_surfaces") or []),
        "authority_scope": card.get("authority_scope"),
        "effective_claims_as_of": deepcopy(dict(card.get("effective_claims_as_of") or {})),
        "relevant_claim_transitions": deepcopy(
            list(card.get("relevant_claim_transitions") or [])
        ),
        "uncertainty_flags": deepcopy(list(card.get("uncertainty_flags") or [])),
        "provenance_refs": deepcopy(list(card.get("provenance_refs") or [])[:provenance_cap]),
    }
    if card.get("parked_identity") is not None:
        result["parked_identity"] = deepcopy(dict(card["parked_identity"]))
    if card.get("parked_identities") is not None:
        result["parked_identities"] = deepcopy(
            list(card["parked_identities"])
        )
    return result


def _trim_parked_marker_v1(
    *,
    marker: Mapping[str, Any],
    supplied_referent_refs: set[str],
) -> None:
    all_co_refs = list(marker.get("co_parked_referent_refs") or [])
    supplied_co_refs = sorted(
        ref for ref in all_co_refs if ref in supplied_referent_refs
    )
    marker["co_parked_referent_refs"] = supplied_co_refs
    marker["parked_set_partially_supplied"] = bool(
        marker.get("parked_set_partially_supplied")
        or len(supplied_co_refs) != len(all_co_refs)
    )


def _compact_frame(
    frame: Mapping[str, Any], block_ids: set[str], candidate_to_ref: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "frame_segment_id": frame["frame_segment_id"],
        "narrator_surface": frame.get("narrator_surface"),
        "narrator_status": frame.get("narrator_status"),
        "narrator_referent_refs": [
            candidate_to_ref[candidate_id]
            for candidate_id in frame.get("candidate_card_ids") or []
            if candidate_id in candidate_to_ref
        ],
        "narrative_mode": frame.get("narrative_mode"),
        "normalization_status": frame.get("normalization_status"),
        "relevant_block_ids": [
            block_id for block_id in frame.get("covered_block_ids") or [] if block_id in block_ids
        ],
    }


def _relevant_review_rows(
    rows: Sequence[Any],
    *,
    component_refs: set[str],
    component_block_ids: set[str],
    candidate_to_ref: Mapping[str, str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        refs = {
            candidate_to_ref[value]
            for value in raw.get("candidate_card_ids") or []
            if value in candidate_to_ref
        }
        blocks = set(raw.get("source_block_ids") or [])
        if not (refs.intersection(component_refs) or blocks.intersection(component_block_ids)):
            continue
        result.append(
            {
                "review_id": raw.get("review_id"),
                "origin": raw.get("origin"),
                "origin_stage": raw.get("origin_stage"),
                "review_kind": raw.get("review_kind"),
                "blocking_kind": raw.get("blocking_kind"),
                "source_block_ids": sorted(blocks.intersection(component_block_ids)),
                "referent_refs": sorted(refs.intersection(component_refs)),
                "candidate_card_ids": sorted(
                    set(raw.get("candidate_card_ids") or [])
                ),
                "reason": raw.get("reason"),
            }
        )
    return result


def _normalized_prior_state(value: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(_mapping(value, "prior state"))
    required = {
        "state_id",
        "semantic_key",
        "state_domain",
        "subject_referent_refs",
        "counterpart_referent_refs",
        "state_value",
        "lifecycle_status",
        "authority_status",
    }
    if not required <= set(row):
        raise B3TemporalContextError("prior state omits required fields")
    if row["lifecycle_status"] not in {"open", "closed"}:
        raise B3TemporalContextError("prior state lifecycle is invalid")
    if row["authority_status"] not in {"effective", "historical", "pending"}:
        raise B3TemporalContextError("prior state authority is invalid")
    row["subject_referent_refs"] = sorted(set(row["subject_referent_refs"]))
    row["counterpart_referent_refs"] = sorted(set(row["counterpart_referent_refs"]))
    return row


def _normalized_prior_pending_case(value: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(_mapping(value, "prior pending case"))
    required = {
        "pending_case_id",
        "chapter_id",
        "review_route",
        "reason_codes",
        "reason",
        "proposed_action",
        "authority_status",
    }
    if not required <= set(row):
        raise B3TemporalContextError("prior pending case omits required fields")
    _required_string(row["pending_case_id"], "pending_case_id")
    _required_string(row["chapter_id"], "pending chapter_id")
    _required_string(row["review_route"], "pending review_route")
    if row["authority_status"] != "pending_review":
        raise B3TemporalContextError("prior pending case claims authority")
    if not isinstance(row["reason_codes"], list) or not all(
        isinstance(item, str) and item for item in row["reason_codes"]
    ):
        raise B3TemporalContextError("prior pending reason codes are malformed")
    row["reason_codes"] = sorted(set(row["reason_codes"]))
    if not isinstance(row["reason"], str) or not row["reason"]:
        raise B3TemporalContextError("prior pending reason is malformed")
    action = row["proposed_action"]
    if action is not None and not isinstance(action, Mapping):
        raise B3TemporalContextError("prior pending action is malformed")
    row["proposed_action"] = deepcopy(dict(action)) if action is not None else None
    return row


def _state_referents(row: Mapping[str, Any]) -> set[str]:
    return set(row.get("subject_referent_refs") or []).union(
        row.get("counterpart_referent_refs") or []
    )


def _pending_referents(row: Mapping[str, Any]) -> set[str]:
    action = row.get("proposed_action")
    if not isinstance(action, Mapping):
        return set()
    return _state_referents(action)


def _event_domain_hint(event_kind: str) -> str:
    return {
        "relationship_bearing_interaction": "relationship",
        "commitment_or_separation": "relationship",
        "life_status_change": "life_status",
        "identity_or_role_change": "role_or_identity",
        "ownership_or_residence_change": "ownership_or_residence",
        "durable_physical_change": "durable_physical_state",
        "world_state_change": "world_state",
        "other_salient_event": "other_durable_state",
    }.get(event_kind, "other_durable_state")


def _turn_unresolved_key(turn: Mapping[str, Any]) -> str:
    values = [
        turn["speaker"].get("surface"),
        turn["addressee"].get("surface"),
    ]
    return "|".join(_normalized_surface(value) for value in values if value)


def _normalized_surface(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _enforce_component_caps(
    *,
    turns: Sequence[Any],
    events: Sequence[Any],
    block_ids: Sequence[str],
    profile: B3TemporalProfileV1,
) -> None:
    if len(turns) > profile.max_turns_per_component:
        raise B3TemporalBudgetError("B3 turns exceed single-component cap")
    if len(events) > profile.max_events_per_component:
        raise B3TemporalBudgetError("B3 events exceed single-component cap")
    if len(block_ids) > profile.max_source_blocks_per_component:
        raise B3TemporalBudgetError("B3 source blocks exceed single-component cap")


def _component_first_block(row: Mapping[str, Any], order: Mapping[str, int]) -> int:
    values = [order[item["block_id"]] for item in row.get("source_blocks") or []]
    return min(values) if values else len(order)


def _user_json_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise B3TemporalContextError("rendered B2 request has no messages")
    users = [row for row in messages if isinstance(row, Mapping) and row.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("content"), str):
        raise B3TemporalContextError("rendered B2 request must contain one JSON user message")
    try:
        payload = json.loads(users[0]["content"])
    except json.JSONDecodeError as exc:
        raise B3TemporalContextError("rendered B2 user payload is invalid JSON") from exc
    return _mapping(payload, "rendered B2 user payload")


def _verified_hashed_object(path: Path, field: str, label: str) -> dict[str, Any]:
    payload = _read_object(path, label)
    _verify_inline_hash(payload, field, label)
    return payload


def _verify_inline_hash(payload: Mapping[str, Any], field: str, label: str) -> None:
    observed = _required_string(payload.get(field), f"{label} {field}")
    body = dict(payload)
    body.pop(field, None)
    if canonical_hash(body) != observed:
        raise B3TemporalContextError(f"{label} hash mismatch")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise B3TemporalContextError(f"cannot read {label}: {path}") from exc
    return _mapping(payload, label)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalContextError(f"{label} must be an object")
    return dict(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise B3TemporalContextError(f"{label} keys differ from contract")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B3TemporalContextError(f"{label} must be a non-empty string")
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise B3TemporalContextError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise B3TemporalContextError(f"{label} is outside the supported range")
    return value


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "chapter"


__all__ = [
    "B3_COMPONENT_SCHEMA_VERSION_V1",
    "B3_INPUT_SCHEMA_VERSION_V1",
    "B3_PLAN_SCHEMA_VERSION_V1",
    "B3_PROFILE_SCHEMA_VERSION_V1",
    "B3_REQUEST_SCHEMA_VERSION_V1",
    "B3TemporalBudgetError",
    "B3TemporalContextError",
    "B3TemporalProfileV1",
    "build_b3_temporal_batch_payload_v1",
    "build_b3_temporal_components_v1",
    "build_b3_temporal_phase_a_bundle_v1",
    "refresh_b3_temporal_component_prior_context_v1",
    "render_b3_temporal_phase_a_batch_v1",
    "load_b2_temporal_input_v1",
    "load_b3_temporal_profile_v1",
]
