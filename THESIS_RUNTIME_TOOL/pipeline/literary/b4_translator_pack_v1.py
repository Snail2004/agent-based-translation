"""Deterministic, budgeted Translator projection over a sealed Story Bible."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping, Sequence

from pipeline.literary.b4_address_anchor_v1 import (
    B4AddressAnchorError,
    verify_address_anchor_artifact_v1,
)
from pipeline.literary.b4_story_bible_assembler_v1 import (
    SCHEMA_VERSION as STORY_BIBLE_SCHEMA_VERSION,
    WINDOW_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


SCHEMA_VERSION = "literary_b4_translator_pack_v1"
PROMPT_VIEW_SCHEMA_VERSION = "literary_b4_translator_pack_prompt_v1"
TIERED_PROMPT_VIEW_SCHEMA_VERSION = "literary_b4_translator_pack_prompt_v2"
DEFAULT_DORMANCY_WINDOW_CHAPTERS = 6
PROJECTION_STRATEGIES = frozenset({"spec_v1", "tiered_v2"})
OMISSION_REASONS = frozenset(
    {
        "out_of_chapter_scope",
        "answered_by_anchor",
        "audit_only_section",
        "evidence_body_stripped",
        "field_trimmed",
        "budget_exceeded",
    }
)


class B4TranslatorPackError(RuntimeError):
    """Raised when a Translator Pack cannot be built without silent loss."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = deepcopy(dict(report)) if report is not None else None


@dataclass(frozen=True)
class ProjectedTranslatorPackV1:
    body: dict[str, Any]
    omissions: tuple[dict[str, Any], ...]
    source_counts: dict[str, int]
    kept_counts: dict[str, int]
    relevant_entity_ids: tuple[str, ...]
    current_speaker_entity_ids: tuple[str, ...]


def project_translator_pack_v1(
    *,
    story_bible: Mapping[str, Any],
    address_anchor: Mapping[str, Any],
    window_slices: Sequence[Mapping[str, Any]],
    dormancy_window_chapters: int = DEFAULT_DORMANCY_WINDOW_CHAPTERS,
    planning_only: bool = False,
) -> ProjectedTranslatorPackV1:
    """Build the semantic projection before transport-budget sealing."""

    story = _verify_sealed(story_bible, "Story Bible")
    if story.get("schema_version") != STORY_BIBLE_SCHEMA_VERSION:
        raise B4TranslatorPackError("unsupported Story Bible schema")
    try:
        anchor = verify_address_anchor_artifact_v1(address_anchor)
    except B4AddressAnchorError as exc:
        raise B4TranslatorPackError(str(exc)) from exc
    if not isinstance(dormancy_window_chapters, int) or isinstance(
        dormancy_window_chapters, bool
    ) or dormancy_window_chapters <= 0:
        raise B4TranslatorPackError("dormancy window must be a positive integer")
    windows = [_verify_sealed(row, "window slice") for row in window_slices]
    if not windows:
        raise B4TranslatorPackError("Translator Pack requires at least one window")
    if any(row.get("schema_version") != WINDOW_SCHEMA_VERSION for row in windows):
        raise B4TranslatorPackError("unsupported B4 window slice schema")
    chapter_id = _text(story.get("chapter_id"), "Story Bible chapter_id")
    if anchor.get("chapter_id") != chapter_id or any(
        row.get("chapter_id") != chapter_id for row in windows
    ):
        raise B4TranslatorPackError("Translator Pack inputs belong to different chapters")
    if anchor.get("story_bible_artifact_hash") != story.get("artifact_hash"):
        raise B4TranslatorPackError("Address Anchor belongs to another Story Bible")
    if not isinstance(planning_only, bool):
        raise B4TranslatorPackError("planning_only must be boolean")

    entities = _object_rows(story.get("entities"), "Story Bible entities")
    entity_by_id = {
        _text(row.get("effective_entity_id"), "effective_entity_id"): row
        for row in entities
    }
    if len(entity_by_id) != len(entities):
        raise B4TranslatorPackError("Story Bible repeats an effective entity id")
    chapter_order = _positive_int(story.get("chapter_order"), "chapter_order")
    order_by_id = _chapter_order_index(story, chapter_id, chapter_order)
    pair_endpoints = _window_pair_endpoints(windows)
    anchor_pair_ids = {
        _text(row.get("pair_id"), "Address Anchor pair_id")
        for row in _object_rows(anchor.get("pair_decisions"), "pair_decisions")
    }

    chapter_present: set[str] = set()
    current_speakers: set[str] = set()
    for window in windows:
        for turn in _object_rows(window.get("speaker_turns"), "speaker_turns"):
            speaker_ids = _endpoint_effective_ids(turn.get("speaker"))
            addressee_ids = _endpoint_effective_ids(turn.get("addressee"))
            current_speakers.update(speaker_ids)
            chapter_present.update(speaker_ids)
            chapter_present.update(addressee_ids)
        for pair in _object_rows(window.get("address_pairs"), "address_pairs"):
            for field in (
                "speaker_effective_entity_id",
                "addressee_effective_entity_id",
            ):
                value = pair.get(field)
                if isinstance(value, str) and value:
                    chapter_present.add(value)
    for pair_id in anchor_pair_ids:
        endpoints = pair_endpoints.get(pair_id)
        if endpoints is None:
            raise B4TranslatorPackError(
                f"Address Anchor pair is absent from all windows: {pair_id}"
            )
        chapter_present.update(endpoints)
    for entity in entities:
        entity_id = str(entity["effective_entity_id"])
        if _first_seen_chapter(entity) == chapter_id:
            chapter_present.add(entity_id)

    core_cast: set[str] = set()
    for entity in entities:
        entity_id = str(entity["effective_entity_id"])
        member_chapters = _string_rows(
            entity.get("member_chapters") or [], "entity member_chapters"
        )
        if len(set(member_chapters)) >= 2:
            core_cast.add(entity_id)
            continue
        member_orders = [
            order
            for row in member_chapters
            for order in [_chapter_order_from_id(row, order_by_id)]
            if order is not None
        ]
        if member_orders and chapter_order - max(member_orders) <= dormancy_window_chapters:
            core_cast.add(entity_id)
    kept_ids = chapter_present | core_cast
    unknown_ids = kept_ids - set(entity_by_id)
    if unknown_ids:
        raise B4TranslatorPackError(
            f"relevance set cites entities absent from Story Bible: {sorted(unknown_ids)}"
        )

    omissions: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    kept_counts: dict[str, int] = {}

    projected_entities = []
    trimmed_entity_fields = 0
    for source in entities:
        entity_id = str(source["effective_entity_id"])
        if entity_id not in kept_ids:
            _omit(omissions, "entities", entity_id, "out_of_chapter_scope")
            continue
        claims = {
            str(field): deepcopy(value)
            for field, value in (source.get("claims") or {}).items()
            if _claim_has_values(value)
        }
        projected_entities.append(
            {
                "effective_entity_id": entity_id,
                "canonical_surface": source.get("canonical_surface"),
                "aliases": deepcopy(source.get("aliases") or []),
                "stable_surfaces": deepcopy(source.get("stable_surfaces") or []),
                "claims": claims,
                "referent_kind": _referent_kind_value(source.get("referent_kind")),
                "first_seen": {"chapter_id": _first_seen_chapter(source)},
            }
        )
        trimmed_entity_fields += len(
            set(source)
            - {
                "effective_entity_id",
                "canonical_surface",
                "aliases",
                "stable_surfaces",
                "claims",
                "referent_kind",
                "first_seen",
            }
        )
    source_counts["entities"] = len(entities)
    kept_counts["entities"] = len(projected_entities)
    _field_trim_omission(omissions, "entities", trimmed_entity_fields)

    relations = _object_rows(story.get("relations"), "Story Bible relations")
    projected_relations = []
    trimmed_relation_fields = 0
    for index, source in enumerate(relations):
        source_id = _text(
            source.get("source_effective_entity_id"), "relation source entity"
        )
        target_id = _text(
            source.get("target_effective_entity_id"), "relation target entity"
        )
        record_id = str(source.get("relation_edge_id") or f"relation:{index}")
        if source_id not in kept_ids or target_id not in kept_ids:
            _omit(omissions, "relations", record_id, "out_of_chapter_scope")
            continue
        projected_relations.append(
            {
                "source_effective_entity_id": source_id,
                "target_effective_entity_id": target_id,
                "relation": source.get("relation"),
                "relation_family": source.get("relation_family"),
                "relation_note": source.get("relation_note"),
                "structurally_contested": bool(source.get("structurally_contested")),
                "effective": bool(source.get("effective")),
            }
        )
        trimmed_relation_fields += len(
            set(source)
            - {
                "source_effective_entity_id",
                "target_effective_entity_id",
                "relation",
                "relation_family",
                "relation_note",
                "structurally_contested",
                "effective",
            }
        )
    source_counts["relations"] = len(relations)
    kept_counts["relations"] = len(projected_relations)
    _field_trim_omission(omissions, "relations", trimmed_relation_fields)

    states = _object_rows(story.get("states"), "Story Bible states")
    projected_states = []
    trimmed_state_fields = 0
    for index, source in enumerate(states):
        state_id = str(source.get("state_id") or f"state:{index}")
        subject_refs = _state_effective_refs(source.get("subject_referents"))
        counterpart_refs = _state_effective_refs(source.get("counterpart_referents"))
        involved = set(subject_refs) | set(counterpart_refs)
        if source.get("lifecycle_status") != "open" or not involved.intersection(
            kept_ids
        ):
            _omit(omissions, "states", state_id, "out_of_chapter_scope")
            continue
        projected_states.append(
            {
                "state_domain": source.get("state_domain"),
                "state_value": source.get("state_value"),
                "semantic_key": source.get("semantic_key"),
                "lifecycle_status": source.get("lifecycle_status"),
                "subject_referent_refs": subject_refs,
                "counterpart_referent_refs": counterpart_refs,
                "valid_from_block_id": source.get("valid_from_block_id"),
                "valid_to_block_id": source.get("valid_to_block_id"),
            }
        )
        trimmed_state_fields += len(
            set(source)
            - {
                "state_domain",
                "state_value",
                "semantic_key",
                "lifecycle_status",
                "valid_from_block_id",
                "valid_to_block_id",
                "subject_referents",
                "counterpart_referents",
            }
        )
    source_counts["states"] = len(states)
    kept_counts["states"] = len(projected_states)
    _field_trim_omission(omissions, "states", trimmed_state_fields)

    idiolect = _object_rows(story.get("idiolect"), "Story Bible idiolect")
    projected_idiolect = []
    for source in idiolect:
        entity_id = _text(source.get("effective_entity_id"), "idiolect entity id")
        if entity_id not in kept_ids or entity_id not in current_speakers:
            _omit(omissions, "idiolect", entity_id, "out_of_chapter_scope")
            continue
        projected_idiolect.append(deepcopy(source))
    source_counts["idiolect"] = len(idiolect)
    kept_counts["idiolect"] = len(projected_idiolect)

    open_questions = story.get("open_questions")
    if not isinstance(open_questions, Mapping):
        raise B4TranslatorPackError("Story Bible open_questions must be an object")
    card_to_effective = {
        card_id: str(row["effective_entity_id"])
        for row in entities
        for card_id in _string_rows(row.get("member_card_ids") or [], "member_card_ids")
    }
    projected_identity = []
    identity_rows = _object_rows(
        open_questions.get("pending_identity_cases") or [],
        "pending_identity_cases",
    )
    for index, source in enumerate(identity_rows):
        record_id = str(source.get("component_id") or f"identity:{index}")
        card_ids = _string_rows(source.get("card_ids") or [], "identity card_ids")
        unknown_cards = set(card_ids) - set(card_to_effective)
        if unknown_cards:
            raise B4TranslatorPackError(
                f"pending identity cards are absent from Story Bible: {sorted(unknown_cards)}"
            )
        projected_identity.append(
            {
                "entity_ids": sorted({card_to_effective[row] for row in card_ids}),
                "unresolved": True,
            }
        )
        _omit(
            omissions,
            "open_questions.pending_identity_cases",
            record_id,
            "evidence_body_stripped",
        )

    pending_state_rows = _object_rows(
        open_questions.get("pending_states") or [], "pending_states"
    )
    projected_pending_states = []
    for index, source in enumerate(pending_state_rows):
        record_id = str(source.get("pending_case_id") or f"pending_state:{index}")
        # The current Story Bible schema does not carry entity refs on pending
        # cases. Keeping the flag is the only fail-closed projection available.
        projected_pending_states.append(
            {
                "pending_case_id": record_id,
                "review_route": source.get("review_route"),
                "unresolved": True,
            }
        )
        _omit(
            omissions,
            "open_questions.pending_states",
            record_id,
            "evidence_body_stripped",
        )

    decisions = {
        str(row["pair_id"]): row
        for row in _object_rows(anchor.get("pair_decisions"), "pair_decisions")
    }
    unresolved_rows = _object_rows(
        open_questions.get("unresolved_address") or [], "unresolved_address"
    )
    projected_unresolved_address = []
    for index, source in enumerate(unresolved_rows):
        speaker_id = source.get("speaker_effective_entity_id")
        addressee_id = source.get("addressee_effective_entity_id")
        pair_id = _resolved_pair_id(speaker_id, addressee_id)
        record_id = str(source.get("evidence_ref") or f"unresolved_address:{index}")
        decision = decisions.get(pair_id) if pair_id is not None else None
        if decision is not None and decision.get("not_anchored") is None:
            _omit(
                omissions,
                "open_questions.unresolved_address",
                record_id,
                "answered_by_anchor",
            )
            continue
        projected_unresolved_address.append(
            {
                "speaker_effective_entity_id": speaker_id,
                "speaker_surface": source.get("speaker_surface"),
                "addressee_effective_entity_id": addressee_id,
                "addressee_surface": source.get("addressee_surface"),
                "unresolved": True,
            }
        )
        _omit(
            omissions,
            "open_questions.unresolved_address",
            record_id,
            "evidence_body_stripped",
        )

    contested = deepcopy(
        _object_rows(open_questions.get("contested_relations") or [], "contested_relations")
    )
    unknowable = deepcopy(
        _object_rows(open_questions.get("unknowable_windows") or [], "unknowable_windows")
    )
    source_counts.update(
        {
            "open_questions.pending_identity_cases": len(identity_rows),
            "open_questions.pending_states": len(pending_state_rows),
            "open_questions.unresolved_address": len(unresolved_rows),
            "open_questions.contested_relations": len(contested),
            "open_questions.unknowable_windows": len(unknowable),
        }
    )
    kept_counts.update(
        {
            "open_questions.pending_identity_cases": len(projected_identity),
            "open_questions.pending_states": len(projected_pending_states),
            "open_questions.unresolved_address": len(projected_unresolved_address),
            "open_questions.contested_relations": len(contested),
            "open_questions.unknowable_windows": len(unknowable),
        }
    )
    _omit(omissions, "lineage", "lineage", "audit_only_section")

    body = {
        "schema_version": SCHEMA_VERSION,
        "book_id": story.get("book_id"),
        "chapter_id": chapter_id,
        "chapter_order": chapter_order,
        "story_bible_artifact_hash": story.get("artifact_hash"),
        "address_anchor_artifact_hash": anchor.get("artifact_hash"),
        "planning_only": planning_only,
        "entities": sorted(
            projected_entities, key=lambda row: str(row["effective_entity_id"])
        ),
        "relations": sorted(
            projected_relations,
            key=lambda row: (
                str(row["source_effective_entity_id"]),
                str(row["target_effective_entity_id"]),
                str(row.get("relation")),
            ),
        ),
        "states": sorted(
            projected_states,
            key=lambda row: (str(row.get("semantic_key")), str(row.get("state_value"))),
        ),
        "idiolect": sorted(
            projected_idiolect, key=lambda row: str(row["effective_entity_id"])
        ),
        "narrative_position": deepcopy(story.get("narrative_position")),
        "open_questions": {
            "pending_identity_cases": projected_identity,
            "pending_states": projected_pending_states,
            "unresolved_address": projected_unresolved_address,
            "contested_relations": contested,
            "unknowable_windows": unknowable,
        },
        "provider_calls": 0,
    }
    return ProjectedTranslatorPackV1(
        body=body,
        omissions=tuple(
            sorted(
                omissions,
                key=lambda row: (
                    str(row["section"]),
                    str(row["reason_code"]),
                    str(row["record_id"]),
                ),
            )
        ),
        source_counts=source_counts,
        kept_counts=kept_counts,
        relevant_entity_ids=tuple(sorted(kept_ids)),
        current_speaker_entity_ids=tuple(sorted(current_speakers)),
    )


def project_translator_pack_tiered_v2(
    *,
    story_bible: Mapping[str, Any],
    address_anchor: Mapping[str, Any],
    window_slices: Sequence[Mapping[str, Any]],
    dormancy_window_chapters: int = DEFAULT_DORMANCY_WINDOW_CHAPTERS,
    planning_only: bool = False,
) -> ProjectedTranslatorPackV1:
    """Keep a full chapter context plus identity-only capsules for silent core cast."""

    baseline = project_translator_pack_v1(
        story_bible=story_bible,
        address_anchor=address_anchor,
        window_slices=window_slices,
        dormancy_window_chapters=dormancy_window_chapters,
        planning_only=planning_only,
    )
    story = _verify_sealed(story_bible, "Story Bible")
    windows = [_verify_sealed(row, "window slice") for row in window_slices]
    chapter_id = _text(story.get("chapter_id"), "Story Bible chapter_id")
    chapter_context_ids = _chapter_context_entity_ids_v2(
        story=story,
        windows=windows,
        chapter_id=chapter_id,
    )
    relation_record_ids = _source_record_id_queues_v2(
        rows=_object_rows(story.get("relations"), "Story Bible relations"),
        signature=_relation_signature_v2,
        id_field="relation_edge_id",
        fallback_prefix="relation",
    )
    state_record_ids = _source_record_id_queues_v2(
        rows=_object_rows(story.get("states"), "Story Bible states"),
        signature=_state_signature_v2,
        id_field="state_id",
        fallback_prefix="state",
    )
    open_questions = story.get("open_questions")
    if not isinstance(open_questions, Mapping):
        raise B4TranslatorPackError("Story Bible open_questions must be an object")
    source_identity_rows = _object_rows(
        open_questions.get("pending_identity_cases") or [],
        "pending_identity_cases",
    )
    address_record_ids = _source_record_id_queues_v2(
        rows=_object_rows(
            open_questions.get("unresolved_address") or [],
            "unresolved_address",
        ),
        signature=_address_signature_v2,
        id_field="evidence_ref",
        fallback_prefix="unresolved_address",
    )
    baseline_ids = set(baseline.relevant_entity_ids)
    missing_context = chapter_context_ids - baseline_ids
    if missing_context:
        raise B4TranslatorPackError(
            "tiered chapter context is absent from the baseline relevance set: "
            f"{sorted(missing_context)}"
        )

    active_relations: list[dict[str, Any]] = []
    omissions = [deepcopy(row) for row in baseline.omissions]
    for row in baseline.body["relations"]:
        source_id = _text(
            row.get("source_effective_entity_id"), "relation source entity"
        )
        target_id = _text(
            row.get("target_effective_entity_id"), "relation target entity"
        )
        record_id = _take_source_record_id_v2(
            relation_record_ids,
            _relation_signature_v2(row),
            "projected relation",
        )
        if not {source_id, target_id}.intersection(chapter_context_ids):
            _omit(
                omissions,
                "relations",
                record_id,
                "out_of_chapter_scope",
            )
            continue
        active_relations.append(deepcopy(row))

    active_states: list[dict[str, Any]] = []
    for row in baseline.body["states"]:
        record_id = _take_source_record_id_v2(
            state_record_ids,
            _state_signature_v2(row),
            "projected state",
        )
        involved = set(
            _string_rows(
                row.get("subject_referent_refs") or [],
                "state subject_referent_refs",
            )
        )
        involved.update(
            _string_rows(
                row.get("counterpart_referent_refs") or [],
                "state counterpart_referent_refs",
            )
        )
        if not involved.intersection(chapter_context_ids):
            _omit(
                omissions,
                "states",
                record_id,
                "out_of_chapter_scope",
            )
            continue
        active_states.append(deepcopy(row))

    detail_ids = set(chapter_context_ids)
    projected_entities: list[dict[str, Any]] = []
    identity_capsule_count = 0
    identity_capsule_trimmed_fields = 0
    for row in baseline.body["entities"]:
        entity_id = _text(row.get("effective_entity_id"), "effective_entity_id")
        if entity_id in detail_ids:
            projected_entities.append(
                {**deepcopy(row), "memory_tier": "chapter_context"}
            )
            continue
        identity_capsule_count += 1
        source_claims = (
            row.get("claims") if isinstance(row.get("claims"), Mapping) else {}
        )
        capsule_claims = _capsule_claim_values_v2(row.get("claims"))
        identity_capsule_trimmed_fields += int("first_seen" in row)
        identity_capsule_trimmed_fields += len(source_claims)
        projected_entities.append(
            {
                "effective_entity_id": entity_id,
                "canonical_surface": row.get("canonical_surface"),
                "aliases": deepcopy(row.get("aliases") or []),
                "stable_surfaces": deepcopy(row.get("stable_surfaces") or []),
                "claims": capsule_claims,
                "referent_kind": deepcopy(row.get("referent_kind")),
                "memory_tier": "core_identity",
            }
        )
    if identity_capsule_trimmed_fields:
        omissions.append(
            {
                "section": "entities",
                "record_id": (
                    f"tiered_identity_field_count:"
                    f"{identity_capsule_trimmed_fields}"
                ),
                "reason_code": "field_trimmed",
                "count": identity_capsule_trimmed_fields,
            }
        )

    baseline_open = baseline.body["open_questions"]
    projected_identity: list[dict[str, Any]] = []
    for index, row in enumerate(baseline_open["pending_identity_cases"]):
        if index >= len(source_identity_rows):
            raise B4TranslatorPackError(
                "projected pending identity case lacks a source row"
            )
        source_identity = source_identity_rows[index]
        record_id = str(
            source_identity.get("component_id") or f"identity:{index}"
        )
        entity_ids = set(
            _string_rows(row.get("entity_ids") or [], "pending identity entity_ids")
        )
        if entity_ids and not entity_ids.intersection(detail_ids):
            _omit(
                omissions,
                "open_questions.pending_identity_cases",
                record_id,
                "out_of_chapter_scope",
            )
            continue
        projected_identity.append(deepcopy(row))

    current_address_keys = _current_address_pair_keys_v2(windows)
    projected_unresolved_address: list[dict[str, Any]] = []
    for row in baseline_open["unresolved_address"]:
        record_id = _take_source_record_id_v2(
            address_record_ids,
            _address_signature_v2(row),
            "projected unresolved address",
        )
        if _address_pair_key_v2(row) not in current_address_keys:
            _omit(
                omissions,
                "open_questions.unresolved_address",
                record_id,
                "out_of_chapter_scope",
            )
            continue
        projected_unresolved_address.append(deepcopy(row))

    body = deepcopy(baseline.body)
    body["projection_strategy"] = "tiered_v2"
    body["projection_policy"] = {
        "chapter_context_sources": [
            "current_window_endpoint",
            "current_chapter_membership",
            "first_seen_in_current_chapter",
        ],
        "active_relation_selection_hops": 1,
        "detail_relation_expansion_hops": 0,
        "silent_core_projection": "identity_capsule",
        "silent_core_claim_values": [
            "gender",
            "life_stage",
            "role_or_occupation",
        ],
        "pending_state_projection": "retain_all_until_entity_refs_exist",
    }
    body["projection_metrics"] = {
        "chapter_context_entity_count": len(chapter_context_ids),
        "detail_entity_count": len(detail_ids),
        "core_identity_capsule_count": identity_capsule_count,
        "active_relation_count": len(active_relations),
        "active_state_count": len(active_states),
    }
    body["entities"] = sorted(
        projected_entities, key=lambda row: str(row["effective_entity_id"])
    )
    body["relations"] = active_relations
    body["states"] = active_states
    body["open_questions"] = {
        "pending_identity_cases": projected_identity,
        "pending_states": deepcopy(baseline_open["pending_states"]),
        "unresolved_address": projected_unresolved_address,
        "contested_relations": deepcopy(baseline_open["contested_relations"]),
        "unknowable_windows": deepcopy(baseline_open["unknowable_windows"]),
    }

    kept_counts = deepcopy(baseline.kept_counts)
    kept_counts["relations"] = len(active_relations)
    kept_counts["states"] = len(active_states)
    kept_counts["open_questions.pending_identity_cases"] = len(projected_identity)
    kept_counts["open_questions.unresolved_address"] = len(
        projected_unresolved_address
    )
    return ProjectedTranslatorPackV1(
        body=body,
        omissions=tuple(
            sorted(
                omissions,
                key=lambda row: (
                    str(row["section"]),
                    str(row["reason_code"]),
                    str(row["record_id"]),
                ),
            )
        ),
        source_counts=deepcopy(baseline.source_counts),
        kept_counts=kept_counts,
        relevant_entity_ids=baseline.relevant_entity_ids,
        current_speaker_entity_ids=baseline.current_speaker_entity_ids,
    )


def seal_translator_pack_v1(
    *,
    projected: ProjectedTranslatorPackV1,
    budget_report: Mapping[str, Any],
) -> dict[str, Any]:
    report = deepcopy(dict(budget_report))
    required = (
        "translator_cap_tokens",
        "headroom_tokens",
        "fixed_prompt_upper_bound_tokens",
        "pack_budget_tokens",
        "pack_estimated_tokens",
        "max_full_prompt_upper_bound_tokens",
        "safety_multiplier",
        "calibration_artifact_hash",
    )
    for field in required:
        if field not in report:
            raise B4TranslatorPackError(f"pack budget report lacks {field}")
    omissions = [deepcopy(row) for row in projected.omissions]
    fits = (
        int(report["pack_estimated_tokens"]) <= int(report["pack_budget_tokens"])
        and int(report["max_full_prompt_upper_bound_tokens"])
        + int(report["headroom_tokens"])
        <= int(report["translator_cap_tokens"])
    )
    if not fits:
        _omit(omissions, "translator_pack", "translator_pack", "budget_exceeded")
    counts_by_reason = Counter(str(row["reason_code"]) for row in omissions)
    counts_by_section = Counter(str(row["section"]) for row in omissions)
    body = {
        **deepcopy(projected.body),
        "pack_budget": {
            **report,
            "dormancy_window_chapters": report.get(
                "dormancy_window_chapters", DEFAULT_DORMANCY_WINDOW_CHAPTERS
            ),
            "source_counts": deepcopy(projected.source_counts),
            "kept_counts": deepcopy(projected.kept_counts),
            "relevant_entity_count": len(projected.relevant_entity_ids),
            "current_speaker_entity_count": len(
                projected.current_speaker_entity_ids
            ),
            "omitted_count": len(omissions),
            "omitted_by_reason": dict(sorted(counts_by_reason.items())),
            "omitted_by_section": dict(sorted(counts_by_section.items())),
            "omissions": omissions,
            "fits": fits,
        },
    }
    sealed = {**body, "artifact_hash": canonical_hash(body)}
    if not fits:
        raise B4TranslatorPackError(
            "Translator Pack exceeds its derived prompt budget",
            report=sealed,
        )
    return verify_translator_pack_v1(sealed)


def verify_translator_pack_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    pack = _verify_sealed(value, "Translator Pack")
    if pack.get("schema_version") != SCHEMA_VERSION:
        raise B4TranslatorPackError("unsupported Translator Pack schema")
    for field in (
        "book_id",
        "chapter_id",
        "story_bible_artifact_hash",
        "address_anchor_artifact_hash",
    ):
        _text(pack.get(field), f"Translator Pack {field}")
    for field in ("entities", "relations", "states", "idiolect"):
        _object_rows(pack.get(field), f"Translator Pack {field}")
    if not isinstance(pack.get("narrative_position"), Mapping):
        raise B4TranslatorPackError("Translator Pack narrative_position is malformed")
    if not isinstance(pack.get("open_questions"), Mapping):
        raise B4TranslatorPackError("Translator Pack open_questions is malformed")
    budget = pack.get("pack_budget")
    if not isinstance(budget, Mapping) or budget.get("fits") is not True:
        raise B4TranslatorPackError("Translator Pack budget is absent or failed")
    for row in _object_rows(budget.get("omissions"), "Translator Pack omissions"):
        if row.get("reason_code") not in OMISSION_REASONS:
            raise B4TranslatorPackError("Translator Pack omission reason is unknown")
    if "lineage" in pack:
        raise B4TranslatorPackError("Translator Pack must not carry audit lineage")
    return deepcopy(pack)


def translator_pack_prompt_view_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    pack = verify_translator_pack_v1(value)
    if pack.get("projection_strategy") == "tiered_v2":
        return _tiered_prompt_view_v2(pack)
    return {
        "schema_version": PROMPT_VIEW_SCHEMA_VERSION,
        "book_id": pack["book_id"],
        "chapter_id": pack["chapter_id"],
        "chapter_order": pack["chapter_order"],
        "story_bible_artifact_hash": pack["story_bible_artifact_hash"],
        "entities": deepcopy(pack["entities"]),
        "relations": deepcopy(pack["relations"]),
        "states": deepcopy(pack["states"]),
        "idiolect": deepcopy(pack["idiolect"]),
        "open_questions": deepcopy(pack["open_questions"]),
    }


def _tiered_prompt_view_v2(pack: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": TIERED_PROMPT_VIEW_SCHEMA_VERSION,
        "table_encoding": "columns align positionally with every row",
        "book_id": pack["book_id"],
        "chapter_id": pack["chapter_id"],
        "chapter_order": pack["chapter_order"],
        "story_bible_artifact_hash": pack["story_bible_artifact_hash"],
        "entities": _table_v2(
            pack["entities"],
            (
                "effective_entity_id",
                "memory_tier",
                "canonical_surface",
                "aliases",
                "stable_surfaces",
                "claims",
                "referent_kind",
                "first_seen_chapter",
            ),
            row_builder=_entity_table_row_v2,
        ),
        "relations": _table_v2(
            pack["relations"],
            (
                "source_effective_entity_id",
                "target_effective_entity_id",
                "relation",
                "relation_family",
                "relation_note",
                "structurally_contested",
                "effective",
            ),
        ),
        "states": _table_v2(
            pack["states"],
            (
                "state_domain",
                "state_value",
                "semantic_key",
                "lifecycle_status",
                "subject_referent_refs",
                "counterpart_referent_refs",
                "valid_from_block_id",
                "valid_to_block_id",
            ),
        ),
        "idiolect": _table_v2(
            pack["idiolect"],
            (
                "effective_entity_id",
                "turn_count",
                "register_distribution",
                "tone_distribution",
                "glossary_terms_in_own_speech",
            ),
        ),
        "open_questions": {
            "pending_identity_cases": _table_v2(
                pack["open_questions"]["pending_identity_cases"],
                ("entity_ids", "unresolved"),
            ),
            "pending_states": _table_v2(
                pack["open_questions"]["pending_states"],
                ("pending_case_id", "review_route", "unresolved"),
            ),
            "unresolved_address": _table_v2(
                pack["open_questions"]["unresolved_address"],
                (
                    "speaker_effective_entity_id",
                    "speaker_surface",
                    "addressee_effective_entity_id",
                    "addressee_surface",
                    "unresolved",
                ),
            ),
            "contested_relations": deepcopy(
                pack["open_questions"]["contested_relations"]
            ),
            "unknowable_windows": deepcopy(
                pack["open_questions"]["unknowable_windows"]
            ),
        },
    }


def _table_v2(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    *,
    row_builder: Any = None,
) -> dict[str, Any]:
    column_list = list(columns)
    return {
        "columns": column_list,
        "rows": [
            (
                row_builder(row)
                if row_builder is not None
                else [deepcopy(row.get(column)) for column in column_list]
            )
            for row in rows
        ],
    }


def _entity_table_row_v2(row: Mapping[str, Any]) -> list[Any]:
    first_seen = row.get("first_seen")
    return [
        deepcopy(row.get("effective_entity_id")),
        deepcopy(row.get("memory_tier")),
        deepcopy(row.get("canonical_surface")),
        deepcopy(row.get("aliases") or []),
        deepcopy(row.get("stable_surfaces") or []),
        deepcopy(row.get("claims") or {}),
        deepcopy(row.get("referent_kind")),
        (
            deepcopy(first_seen.get("chapter_id"))
            if isinstance(first_seen, Mapping)
            else None
        ),
    ]


def _capsule_claim_values_v2(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in ("gender", "life_stage", "role_or_occupation"):
        source = value.get(field)
        if not isinstance(source, Mapping):
            continue
        claim_value = source.get("value")
        if claim_value in (None, "", []):
            continue
        result[field] = {"value": deepcopy(claim_value)}
    return result


def calibrated_token_estimate_v1(value: Any, *, safety_multiplier: float) -> int:
    if not isinstance(safety_multiplier, (int, float)) or safety_multiplier < 1:
        raise B4TranslatorPackError("safety multiplier must be at least 1")
    serialized = canonical_json(value)
    try:
        import tiktoken

        raw = len(tiktoken.get_encoding("o200k_base").encode(serialized))
    except (ImportError, KeyError):
        raw = math.ceil(len(serialized.encode("utf-8")) / 2.38)
    return math.ceil(raw * float(safety_multiplier))


def _chapter_context_entity_ids_v2(
    *,
    story: Mapping[str, Any],
    windows: Sequence[Mapping[str, Any]],
    chapter_id: str,
) -> set[str]:
    result: set[str] = set()
    for window in windows:
        for turn in _object_rows(window.get("speaker_turns"), "speaker_turns"):
            result.update(_endpoint_effective_ids(turn.get("speaker")))
            result.update(_endpoint_effective_ids(turn.get("addressee")))
        for pair in _object_rows(window.get("address_pairs"), "address_pairs"):
            for field in (
                "speaker_effective_entity_id",
                "addressee_effective_entity_id",
            ):
                value = pair.get(field)
                if isinstance(value, str) and value:
                    result.add(value)
    for entity in _object_rows(story.get("entities"), "Story Bible entities"):
        entity_id = _text(entity.get("effective_entity_id"), "effective_entity_id")
        member_chapters = _string_rows(
            entity.get("member_chapters") or [], "entity member_chapters"
        )
        if chapter_id in member_chapters or _first_seen_chapter(entity) == chapter_id:
            result.add(entity_id)
    return result


def _current_address_pair_keys_v2(
    windows: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    return {
        _address_pair_key_v2(row)
        for window in windows
        for row in _object_rows(window.get("address_pairs"), "address_pairs")
    }


def _address_pair_key_v2(value: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(
            value.get("speaker_effective_entity_id")
            or value.get("speaker_surface")
            or ""
        ),
        str(
            value.get("addressee_effective_entity_id")
            or value.get("addressee_surface")
            or ""
        ),
    )


def _source_record_id_queues_v2(
    *,
    rows: Sequence[Mapping[str, Any]],
    signature: Any,
    id_field: str,
    fallback_prefix: str,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for index, row in enumerate(rows):
        result.setdefault(signature(row), []).append(
            str(row.get(id_field) or f"{fallback_prefix}:{index}")
        )
    return result


def _take_source_record_id_v2(
    queues: dict[str, list[str]],
    signature: str,
    label: str,
) -> str:
    values = queues.get(signature)
    if not values:
        raise B4TranslatorPackError(f"{label} cannot be traced to its source row")
    return values.pop(0)


def _relation_signature_v2(value: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            "source_effective_entity_id": value.get(
                "source_effective_entity_id"
            ),
            "target_effective_entity_id": value.get(
                "target_effective_entity_id"
            ),
            "relation": value.get("relation"),
            "relation_family": value.get("relation_family"),
            "relation_note": value.get("relation_note"),
            "structurally_contested": bool(value.get("structurally_contested")),
            "effective": bool(value.get("effective")),
        }
    )


def _state_signature_v2(value: Mapping[str, Any]) -> str:
    subject_refs = (
        _state_effective_refs(value.get("subject_referents"))
        if "subject_referents" in value
        else _string_rows(
            value.get("subject_referent_refs") or [],
            "state subject_referent_refs",
        )
    )
    counterpart_refs = (
        _state_effective_refs(value.get("counterpart_referents"))
        if "counterpart_referents" in value
        else _string_rows(
            value.get("counterpart_referent_refs") or [],
            "state counterpart_referent_refs",
        )
    )
    return canonical_hash(
        {
            "state_domain": value.get("state_domain"),
            "state_value": value.get("state_value"),
            "semantic_key": value.get("semantic_key"),
            "lifecycle_status": value.get("lifecycle_status"),
            "subject_referent_refs": sorted(subject_refs),
            "counterpart_referent_refs": sorted(counterpart_refs),
            "valid_from_block_id": value.get("valid_from_block_id"),
            "valid_to_block_id": value.get("valid_to_block_id"),
        }
    )


def _address_signature_v2(value: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            "speaker_effective_entity_id": value.get(
                "speaker_effective_entity_id"
            ),
            "speaker_surface": value.get("speaker_surface"),
            "addressee_effective_entity_id": value.get(
                "addressee_effective_entity_id"
            ),
            "addressee_surface": value.get("addressee_surface"),
        }
    )


def _chapter_order_index(
    story: Mapping[str, Any], chapter_id: str, chapter_order: int
) -> dict[str, int]:
    result = {chapter_id: chapter_order}
    narrative = story.get("narrative_position")
    if isinstance(narrative, Mapping):
        for row in narrative.get("capsules") or []:
            if not isinstance(row, Mapping):
                continue
            value = row.get("chapter_id")
            order = row.get("chapter_order")
            if isinstance(value, str) and isinstance(order, int) and order > 0:
                result[value] = order
    return result


def _chapter_order_from_id(
    chapter_id: str, order_by_id: Mapping[str, int]
) -> int | None:
    known = order_by_id.get(chapter_id)
    if known is not None:
        return known
    match = re.search(r"(?:^|_)ch([0-9]+)$", chapter_id)
    if match:
        order = int(match.group(1))
        return order if order > 0 else None
    return None


def _window_pair_endpoints(
    windows: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for window in windows:
        for row in _object_rows(window.get("address_pairs"), "address_pairs"):
            speaker = row.get("speaker_effective_entity_id")
            addressee = row.get("addressee_effective_entity_id")
            pair_id = row.get("pair_id") or _resolved_pair_id(speaker, addressee)
            if pair_id is None:
                continue
            endpoints = (_text(speaker, "pair speaker"), _text(addressee, "pair addressee"))
            previous = result.setdefault(str(pair_id), endpoints)
            if previous != endpoints:
                raise B4TranslatorPackError("one pair_id maps to multiple endpoints")
    return result


def _resolved_pair_id(speaker: Any, addressee: Any) -> str | None:
    if not isinstance(speaker, str) or not speaker:
        return None
    if not isinstance(addressee, str) or not addressee:
        return None
    return canonical_hash(
        {
            "speaker_effective_entity_id": speaker,
            "addressee_effective_entity_id": addressee,
        }
    )[:24]


def _endpoint_effective_ids(value: Any) -> set[str]:
    if not isinstance(value, Mapping) or not value.get("resolved_to_effective_entity"):
        return set()
    return set(_string_rows(value.get("effective_entity_ids") or [], "endpoint ids"))


def _state_effective_refs(value: Any) -> list[str]:
    rows = _object_rows(value or [], "state referents")
    return sorted(
        {
            str(row["effective_entity_id"])
            for row in rows
            if isinstance(row.get("effective_entity_id"), str)
            and row.get("effective_entity_id")
        }
    )


def _first_seen_chapter(entity: Mapping[str, Any]) -> str:
    value = entity.get("first_seen")
    if isinstance(value, Mapping):
        return _text(value.get("chapter_id"), "entity first_seen chapter_id")
    if isinstance(value, str):
        match = re.match(r"^(.+)_b[^_]+$", value)
        if match:
            return match.group(1)
    return _text(entity.get("established_in_chapter"), "entity first chapter")


def _referent_kind_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return deepcopy(value.get("value"))
    return deepcopy(value)


def _claim_has_values(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("value") not in (None, "", []):
        return True
    values = value.get("values")
    return isinstance(values, list) and bool(values)


def _field_trim_omission(
    omissions: list[dict[str, Any]], section: str, count: int
) -> None:
    if count > 0:
        omissions.append(
            {
                "section": section,
                "record_id": f"field_count:{count}",
                "reason_code": "field_trimmed",
                "count": count,
            }
        )


def _omit(
    omissions: list[dict[str, Any]], section: str, record_id: str, reason: str
) -> None:
    if reason not in OMISSION_REASONS:
        raise B4TranslatorPackError(f"unknown omission reason: {reason}")
    omissions.append(
        {"section": section, "record_id": str(record_id), "reason_code": reason}
    )


def _verify_sealed(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B4TranslatorPackError(f"{label} must be an object")
    body = deepcopy(dict(value))
    observed = body.pop("artifact_hash", None)
    if observed != canonical_hash(body):
        raise B4TranslatorPackError(f"{label} hash mismatch")
    return deepcopy(dict(value))


def _object_rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise B4TranslatorPackError(f"{label} must be a list of objects")
    return [deepcopy(dict(row)) for row in value]


def _string_rows(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row for row in value
    ):
        raise B4TranslatorPackError(f"{label} must be a list of strings")
    return list(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4TranslatorPackError(f"{label} must be non-empty text")
    return value.strip()


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise B4TranslatorPackError(f"{label} must be a positive integer")
    return value


__all__ = [
    "B4TranslatorPackError",
    "DEFAULT_DORMANCY_WINDOW_CHAPTERS",
    "OMISSION_REASONS",
    "PROJECTION_STRATEGIES",
    "PROMPT_VIEW_SCHEMA_VERSION",
    "ProjectedTranslatorPackV1",
    "SCHEMA_VERSION",
    "calibrated_token_estimate_v1",
    "project_translator_pack_v1",
    "project_translator_pack_tiered_v2",
    "seal_translator_pack_v1",
    "translator_pack_prompt_view_v1",
    "verify_translator_pack_v1",
]
