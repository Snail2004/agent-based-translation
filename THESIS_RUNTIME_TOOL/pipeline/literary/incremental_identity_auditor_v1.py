"""Reversible identity hearings inside the chapter-prefix cycle.

The module turns content-addressed review leads into bounded identity components.
It never mints a global entity, publishes a global surface, or erases a local
candidate.  A later chapter may reopen a component only when its evidence
manifest changes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
import unicodedata

from pipeline.literary.chapter_cycle_review_v1 import (
    verify_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    CHAPTER_CONFIRMED_SCOPE,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.incremental_identity_auditor_prompts_v1 import (
    PROMPT_ID,
    PROMPT_SHA256,
    load_incremental_identity_prompt_v1,
)


INDEX_SCHEMA_VERSION = "incremental_identity_index_v2"
INDEX_VALIDATOR_VERSION = "incremental_identity_index_validator_v2"
DECISION_SCHEMA_VERSION = "incremental_identity_decision_v2"
LEDGER_SCHEMA_VERSION = "incremental_identity_ledger_v2"
LEDGER_VALIDATOR_VERSION = "incremental_identity_ledger_validator_v2"
DEFAULT_MAX_SOURCE_BLOCKS = 32
ALLOWED_ACTIONS = frozenset({"keep", "link_to", "pending"})
SURFACE_SCOPE_ACTIONS = frozenset(
    {
        "confirm_block_scope",
        "confirm_chapter_scope",
        "nominate_book_candidate",
        "keep_pending",
        "dismiss_dormant",
    }
)
SURFACE_EVIDENCE_NEEDS = frozenset(
    {
        "explicit_naming",
        "cross_chapter_recurrence",
        "referent_attribution",
        "scope_disambiguation",
        "identity_resolution",
        "additional_source_context",
    }
)
# A reversible link carries no identity authority and must remain reopenable.
# Only an explicit distinct decision closes identity-membership review rows.
RESOLVED_STATUSES = frozenset({"resolved_distinct"})
IDENTITY_DISPUTE_FIELDS = frozenset(
    {None, "identity_membership", "alias_target", "alias_scope"}
)
STABLE_CLAIM_FIELDS = frozenset(
    {"referent_kind", "referential_gender", "identity_summary"}
)


class IncrementalIdentityError(RuntimeError):
    """Raised when incremental identity evidence or a decision is invalid."""


@dataclass(frozen=True)
class RenderedIncrementalIdentityRequestV1:
    component_id: str
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    semantic_payload: dict[str, Any]


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IncrementalIdentityError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row.strip() for row in value
    ):
        raise IncrementalIdentityError(f"{label} must be a string list")
    if not allow_empty and not value:
        raise IncrementalIdentityError(f"{label} cannot be empty")
    if len(value) != len(set(value)):
        raise IncrementalIdentityError(f"{label} contains duplicates")
    return list(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise IncrementalIdentityError(f"{label} has a foreign shape")


def _surface_key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _block_text(block: Mapping[str, Any]) -> str:
    for key in ("source_text", "text", "content", "raw_text"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _document_catalog(document: Mapping[str, Any]) -> dict[str, Any]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise IncrementalIdentityError("document has no chapters")
    block_by_id: dict[str, dict[str, Any]] = {}
    block_order: dict[str, int] = {}
    chapter_blocks: dict[str, list[str]] = {}
    absolute = 0
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise IncrementalIdentityError("document chapter must be an object")
        chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
        if chapter_id in chapter_blocks:
            raise IncrementalIdentityError("document repeats a chapter id")
        blocks = chapter.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise IncrementalIdentityError("document chapter has no blocks")
        ordered = sorted(
            blocks,
            key=lambda row: (int(row.get("order_index") or 0), str(row.get("block_id") or "")),
        )
        chapter_blocks[chapter_id] = []
        for block in ordered:
            if not isinstance(block, Mapping):
                raise IncrementalIdentityError("document block must be an object")
            block_id = _required_string(block.get("block_id"), "block_id")
            if block_id in block_by_id:
                raise IncrementalIdentityError("document repeats a block id")
            block_by_id[block_id] = {
                "chapter_id": chapter_id,
                "block_id": block_id,
                "text": _block_text(block),
            }
            block_order[block_id] = absolute
            chapter_blocks[chapter_id].append(block_id)
            absolute += 1
    return {
        "block_by_id": block_by_id,
        "block_order": block_order,
        "chapter_blocks": chapter_blocks,
    }


def _component_model_view(component: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "component_id": component["component_id"],
        "candidate_prior_card_ids": _clone(
            component["candidate_prior_card_ids"]
        ),
        "surface_keys": _clone(component["surface_keys"]),
        "prior_hearing_count": int(component["prior_hearing_count"]),
        "trigger_state": component["trigger_state"],
    }


def _dispute_model_view(dispute: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "disputed_field": dispute.get("disputed_field"),
        "historical_value": _clone(dispute.get("historical_value")),
        "status": dispute.get("status"),
        "pending_reason_codes": _clone(
            dispute.get("pending_reason_codes") or []
        ),
    }


def _candidate_card_model_view(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "prior_card_id": card["prior_card_id"],
        "canonical_surface": card["canonical_surface"],
        "stable_surfaces": _clone(card["stable_surfaces"]),
        "effective_claims": _clone(card["effective_claims"]),
        "disputed_claims": [
            _dispute_model_view(row) for row in card.get("disputed_claims") or []
        ],
        "authority_scope": card["authority_scope"],
        "first_supported_block_id": card["first_supported_block_id"],
    }


def _review_lead_model_view(lead: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "review_item_id": lead["review_item_id"],
        "review_case_id": lead.get("review_case_id"),
        "chapter_id": lead["chapter_id"],
        "source_kind": lead["source_kind"],
        "disputed_field": lead.get("disputed_field"),
        "surface_key": lead.get("surface_key"),
        "surface": lead.get("surface"),
        "source_block_ids": _clone(lead.get("source_block_ids") or []),
        "reason_code": lead["reason_code"],
    }


class _UnionFind:
    def __init__(self, values: Sequence[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a = self.find(left)
        b = self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _card_views(prefix: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = [
        *(prefix.get("b0_context_cards") or []),
        *(prefix.get("candidate_only_context_cards") or []),
    ]
    cards: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise IncrementalIdentityError("prefix context card must be an object")
        card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if card_id in cards:
            raise IncrementalIdentityError("prefix context repeats a card id")
        cards[card_id] = _clone(dict(raw))
    return cards


def _source_closure(
    *, direct_ids: Sequence[str], catalog: Mapping[str, Any]
) -> list[str]:
    block_by_id = catalog["block_by_id"]
    chapter_blocks = catalog["chapter_blocks"]
    selected: set[str] = set()
    for block_id in direct_ids:
        if block_id not in block_by_id:
            raise IncrementalIdentityError("identity evidence cites a foreign block")
        selected.add(block_id)
        chapter_id = block_by_id[block_id]["chapter_id"]
        ids = chapter_blocks[chapter_id]
        index = ids.index(block_id)
        if index > 0:
            selected.add(ids[index - 1])
        if index + 1 < len(ids):
            selected.add(ids[index + 1])
    return sorted(selected, key=catalog["block_order"].__getitem__)


def _previous_state_by_key(previous_ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    if previous_ledger is None:
        return {}
    verified = verify_incremental_identity_ledger_v1(previous_ledger)
    return {row["component_key"]: row for row in verified["component_states"]}


def _card_surface_keys(card: Mapping[str, Any]) -> set[str]:
    return {
        key
        for value in card.get("stable_surfaces") or []
        if (key := _surface_key(value))
    }


def _review_lead_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the reason for a hearing without exposing unrelated ledger state."""

    return {
        "review_item_id": row["review_item_id"],
        "review_case_id": row.get("review_case_id"),
        "chapter_id": row["chapter_id"],
        "source_kind": row["source_kind"],
        "disputed_field": row.get("disputed_field"),
        "surface_key": row.get("surface_key"),
        "surface": row.get("surface"),
        "source_block_ids": list(row.get("source_block_ids") or []),
        "reason_code": row["reason_code"],
        "evidence_manifest_hash": row["evidence_manifest_hash"],
    }


def build_incremental_identity_index_v1(
    *,
    document: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    review_ledger: Mapping[str, Any],
    previous_identity_ledger: Mapping[str, Any] | None = None,
    max_source_blocks: int = DEFAULT_MAX_SOURCE_BLOCKS,
) -> dict[str, Any]:
    if max_source_blocks < 4:
        raise IncrementalIdentityError("identity source-block cap is too small")
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle, document=document)
    reviews = verify_chapter_cycle_review_ledger_v1(review_ledger)
    if reviews["state_lineage_id"] != prefix["state_lineage_id"]:
        raise IncrementalIdentityError("identity review crosses state lineage")
    if reviews["coverage_through_chapter_id"] != prefix["coverage_through_chapter_id"]:
        raise IncrementalIdentityError("identity review coverage is stale")
    previous_by_key = _previous_state_by_key(previous_identity_ledger)
    if previous_identity_ledger is not None:
        previous = verify_incremental_identity_ledger_v1(previous_identity_ledger)
        if previous["state_lineage_id"] != prefix["state_lineage_id"]:
            raise IncrementalIdentityError("previous identity ledger crosses state lineage")

    cards = _card_views(prefix)
    queued = [
        _clone(row)
        for row in reviews["review_items"]
        if row.get("route") == "book_identity_auditor"
        and row.get("lifecycle_state") in {"queued", "book_end_pending"}
    ]
    queued_ids = {
        card_id
        for row in queued
        for card_id in _string_list(
            row.get("subject_prior_card_ids"), "subject_prior_card_ids"
        )
    }
    foreign = queued_ids - set(cards)
    if foreign:
        raise IncrementalIdentityError("identity review references a foreign prefix card")

    # A later evidence row may cite only one member of a component heard before.
    # Reattach the old candidates by prior membership or shared reviewed surface;
    # this only builds a hearing packet and never decides identity.
    expanded_ids = set(queued_ids)
    matched_previous_keys: set[str] = set()
    queued_surface_keys = {
        key
        for card_id in queued_ids
        for key in _card_surface_keys(cards[card_id])
    }
    for key, state in previous_by_key.items():
        prior_ids = set(state.get("candidate_prior_card_ids") or []) & set(cards)
        prior_surfaces = set(state.get("surface_keys") or [])
        if prior_ids.intersection(queued_ids) or prior_surfaces.intersection(
            queued_surface_keys
        ):
            expanded_ids.update(prior_ids)
            matched_previous_keys.add(key)

    all_ids = sorted(expanded_ids)
    uf = _UnionFind(all_ids)
    for row in queued:
        owners = _string_list(row["subject_prior_card_ids"], "subject_prior_card_ids")
        for owner in owners[1:]:
            uf.union(owners[0], owner)
    for key in sorted(matched_previous_keys):
        state = previous_by_key[key]
        prior_ids = [
            card_id
            for card_id in state.get("candidate_prior_card_ids") or []
            if card_id in uf.parent
        ]
        touching = [
            card_id
            for card_id in queued_ids
            if card_id in uf.parent
            and _card_surface_keys(cards[card_id]).intersection(
                set(state.get("surface_keys") or [])
            )
        ]
        bridge = [*prior_ids, *touching]
        for card_id in bridge[1:]:
            uf.union(bridge[0], card_id)

    all_ids = sorted(
        {
            card_id
            for card_id in all_ids
            if card_id in cards
        }
    )
    groups: dict[str, list[str]] = {}
    for card_id in all_ids:
        groups.setdefault(uf.find(card_id), []).append(card_id)

    catalog = _document_catalog(document)
    uncertainties = list(prefix.get("prefix_identity_uncertainties") or [])
    components: list[dict[str, Any]] = []
    singleton_review_item_ids: list[str] = []
    for owner_ids in sorted(groups.values(), key=lambda row: tuple(row)):
        owner_set = set(owner_ids)
        owned_reviews = [
            row
            for row in queued
            if owner_set.intersection(row["subject_prior_card_ids"])
        ]
        review_item_ids = sorted(row["review_item_id"] for row in owned_reviews)
        is_bounded_singleton_hearing = bool(owned_reviews) and all(
            row.get("disputed_field")
            in {"identity_membership", "alias_scope", "alias_target"}
            and row.get("review_case_id")
            and row.get("surface_key")
            for row in owned_reviews
        )
        if len(owner_ids) < 2 and not is_bounded_singleton_hearing:
            singleton_review_item_ids.extend(review_item_ids)
            continue
        surface_keys = sorted(
            {
                str(row.get("surface_key"))
                for row in uncertainties
                if set(row.get("prior_card_ids") or []).issubset(owner_set)
                and set(row.get("prior_card_ids") or [])
                and str(row.get("surface_key") or "").strip()
            }
        )
        surface_keys = sorted(
            set(surface_keys).union(
                str(row.get("surface_key"))
                for row in owned_reviews
                if str(row.get("surface_key") or "").strip()
            )
        )
        prior_surface_keys = {
            surface
            for state in previous_by_key.values()
            if set(state.get("candidate_prior_card_ids") or []).intersection(owner_set)
            for surface in state.get("surface_keys") or []
        }
        if prior_surface_keys:
            surface_keys = sorted(prior_surface_keys)
        if not surface_keys:
            per_card = [_card_surface_keys(cards[card_id]) for card_id in owner_ids]
            common = set.intersection(*per_card) if per_card else set()
            surface_keys = sorted(common or set.union(*per_card))
        component_key_body = {
            "state_lineage_id": prefix["state_lineage_id"],
            "surface_keys": surface_keys,
        }
        component_key = "incidkey1_" + canonical_hash(component_key_body)[:20]
        direct_ids = {
            str(ref["block_id"])
            for card_id in owner_ids
            for ref in cards[card_id].get("provenance_refs") or []
            if isinstance(ref, Mapping) and str(ref.get("block_id") or "").strip()
        }
        direct_ids.update(
            block_id
            for row in owned_reviews
            for block_id in row.get("source_block_ids") or []
        )
        ordered_direct = sorted(direct_ids, key=catalog["block_order"].__getitem__)
        source_ids = _source_closure(direct_ids=ordered_direct, catalog=catalog)
        evidence_body = {
            "component_key": component_key,
            "candidate_card_hashes": sorted(
                cards[card_id]["context_card_hash"] for card_id in owner_ids
            ),
            "review_evidence_hashes": sorted(
                row["evidence_manifest_hash"] for row in owned_reviews
            ),
            "source_block_ids": source_ids,
        }
        evidence_hash = canonical_hash(evidence_body)
        prior = previous_by_key.get(component_key)
        prior_hash = prior.get("latest_evidence_manifest_hash") if prior else None
        trigger_state = (
            "duplicate_suppressed"
            if prior_hash == evidence_hash
            else ("new_component" if prior is None else "new_evidence")
        )
        hearing_count = int(prior.get("hearing_count", 0)) if prior else 0
        overflow = len(source_ids) > max_source_blocks
        component_body = {
            "component_key": component_key,
            "candidate_prior_card_ids": sorted(owner_ids),
            "surface_keys": surface_keys,
            "review_item_ids": review_item_ids,
            "review_leads": [_review_lead_view(row) for row in owned_reviews],
            "direct_source_block_ids": ordered_direct,
            "source_block_ids": source_ids,
            "evidence_manifest_hash": evidence_hash,
            "prior_hearing_count": hearing_count,
            "trigger_state": trigger_state,
            "overflow": overflow,
        }
        component_id = "incidcomp1_" + canonical_hash(component_body)[:20]
        components.append({"component_id": component_id, **component_body})

    components.sort(key=lambda row: row["component_id"])
    body = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "validator_version": INDEX_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": prefix["coverage_through_chapter_id"],
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "review_ledger_hash": reviews["review_ledger_hash"],
        "previous_identity_ledger_hash": (
            previous_identity_ledger.get("identity_ledger_hash")
            if previous_identity_ledger is not None
            else None
        ),
        "candidate_cards": [cards[card_id] for card_id in sorted(cards)],
        "components": components,
        "singleton_review_item_ids": sorted(set(singleton_review_item_ids)),
        "component_bounds": {"max_source_blocks": max_source_blocks},
        "production_publish_performed": False,
    }
    return {**body, "identity_index_hash": canonical_hash(body)}


def verify_incremental_identity_index_v1(index: Mapping[str, Any]) -> dict[str, Any]:
    if index.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise IncrementalIdentityError("foreign incremental identity index schema")
    if index.get("validator_version") != INDEX_VALIDATOR_VERSION:
        raise IncrementalIdentityError("incremental identity validator mismatch")
    body = dict(index)
    observed = _required_string(body.pop("identity_index_hash", None), "identity_index_hash")
    if canonical_hash(body) != observed:
        raise IncrementalIdentityError("incremental identity index hash mismatch")
    if index.get("production_publish_performed") is not False:
        raise IncrementalIdentityError("incremental identity index claims publication")
    components = index.get("components")
    cards = index.get("candidate_cards")
    if not isinstance(components, list) or not isinstance(cards, list):
        raise IncrementalIdentityError("incremental identity index collections are malformed")
    card_ids = {_required_string(row.get("prior_card_id"), "prior_card_id") for row in cards}
    seen: set[str] = set()
    for row in components:
        component_id = _required_string(row.get("component_id"), "component_id")
        if component_id in seen:
            raise IncrementalIdentityError("incremental identity index repeats a component")
        seen.add(component_id)
        owners = _string_list(row.get("candidate_prior_card_ids"), "candidate ids")
        if not set(owners).issubset(card_ids):
            raise IncrementalIdentityError("incremental identity component owners are malformed")
        _string_list(row.get("source_block_ids"), "component source blocks")
        _required_string(row.get("evidence_manifest_hash"), "evidence manifest hash")
        review_leads = row.get("review_leads")
        if not isinstance(review_leads, list) or {
            lead.get("review_item_id")
            for lead in review_leads
            if isinstance(lead, Mapping)
        } != set(row.get("review_item_ids") or []):
            raise IncrementalIdentityError("incremental identity review leads are malformed")
        if len(owners) < 2 and not (
            review_leads
            and all(
                lead.get("disputed_field")
                in {"identity_membership", "alias_scope", "alias_target"}
                and lead.get("review_case_id")
                and lead.get("surface_key")
                for lead in review_leads
                if isinstance(lead, Mapping)
            )
        ):
            raise IncrementalIdentityError(
                "singleton component is not a surface-scope hearing"
            )
        if row.get("trigger_state") not in {
            "new_component",
            "new_evidence",
            "duplicate_suppressed",
        }:
            raise IncrementalIdentityError("incremental identity trigger state is foreign")
        if not isinstance(row.get("overflow"), bool):
            raise IncrementalIdentityError("incremental identity overflow flag is malformed")
    return _clone(dict(index))


def incremental_identity_response_schema_v1() -> dict[str, Any]:
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "prior_card_id",
            "action",
            "target_prior_card_id",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "prior_card_id": {"type": "string"},
            "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
            "target_prior_card_id": {"type": ["string", "null"]},
            "source_block_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True,
            },
            "resolution_note": {"type": "string", "minLength": 1},
        },
    }
    surface_action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "review_item_id",
            "action",
            "target_prior_card_id",
            "valid_block_ids",
            "source_block_ids",
            "evidence_needed",
            "resolution_note",
        ],
        "properties": {
            "review_item_id": {"type": "string"},
            "action": {"type": "string", "enum": sorted(SURFACE_SCOPE_ACTIONS)},
            "target_prior_card_id": {"type": ["string", "null"]},
            "valid_block_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "source_block_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True,
            },
            "evidence_needed": {
                "anyOf": [
                    {"type": "string", "enum": sorted(SURFACE_EVIDENCE_NEEDS)},
                    {"type": "null"},
                ]
            },
            "resolution_note": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["component_id", "candidate_actions", "surface_scope_actions"],
        "properties": {
            "component_id": {"type": "string"},
            "candidate_actions": {
                "type": "array",
                "items": action,
                "minItems": 1,
            },
            "surface_scope_actions": {
                "type": "array",
                "items": surface_action,
            },
        },
    }


def render_incremental_identity_request_v1(
    *,
    index: Mapping[str, Any],
    component_id: str,
    document: Mapping[str, Any],
    design_doc: Path,
    previous_identity_ledger: Mapping[str, Any] | None = None,
) -> RenderedIncrementalIdentityRequestV1:
    verified = verify_incremental_identity_index_v1(index)
    component = next(
        (row for row in verified["components"] if row["component_id"] == component_id),
        None,
    )
    if component is None:
        raise IncrementalIdentityError("unknown incremental identity component")
    if component["overflow"]:
        raise IncrementalIdentityError("overflow identity component cannot be rendered")
    if component["trigger_state"] == "duplicate_suppressed":
        raise IncrementalIdentityError("unchanged identity evidence cannot reopen a hearing")
    cards = {row["prior_card_id"]: row for row in verified["candidate_cards"]}
    catalog = _document_catalog(document)
    previous_history: list[dict[str, Any]] = []
    if previous_identity_ledger is not None:
        previous = verify_incremental_identity_ledger_v1(previous_identity_ledger)
        previous_history = [
            {
                "hearing_number": row["hearing_number"],
                "status": row["status"],
                "evidence_manifest_hash": row["evidence_manifest_hash"],
                "candidate_actions": row["candidate_actions"],
                "surface_scope_actions": row.get("surface_scope_actions") or [],
            }
            for row in previous["decision_history"]
            if row["component_key"] == component["component_key"]
        ]
    payload = {
        "schema_version": "incremental_identity_request_payload_v1",
        "component": _component_model_view(component),
        "candidate_cards": [
            _candidate_card_model_view(cards[card_id])
            for card_id in component["candidate_prior_card_ids"]
        ],
        "review_leads": [
            _review_lead_model_view(row) for row in component["review_leads"]
        ],
        "prior_hearings": previous_history,
        "source_blocks": [
            catalog["block_by_id"][block_id] for block_id in component["source_block_ids"]
        ],
        "authority_boundary": {
            "book_global_authority": False,
            "candidate_history_is_append_only": True,
            "same_evidence_cannot_reopen": True,
        },
    }
    prompt = load_incremental_identity_prompt_v1(design_doc)
    fingerprint = canonical_hash(
        {
            "prompt_id": PROMPT_ID,
            "prompt_sha256": PROMPT_SHA256,
            "identity_index_hash": verified["identity_index_hash"],
            "component_id": component_id,
            "evidence_manifest_hash": component["evidence_manifest_hash"],
            "payload": payload,
        }
    )
    return RenderedIncrementalIdentityRequestV1(
        component_id=component_id,
        request_fingerprint=fingerprint,
        messages=(
            {"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json(payload)},
        ),
        response_schema=incremental_identity_response_schema_v1(),
        semantic_payload=payload,
    )


def validate_incremental_identity_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_incremental_identity_index_v1(index)
    if not isinstance(response, Mapping):
        raise IncrementalIdentityError("incremental identity response must be an object")
    _exact_keys(
        response,
        {"component_id", "candidate_actions", "surface_scope_actions"},
        "identity response",
    )
    component_id = _required_string(response.get("component_id"), "component_id")
    component = next(
        (row for row in verified["components"] if row["component_id"] == component_id),
        None,
    )
    if component is None or component["overflow"]:
        raise IncrementalIdentityError("identity response owns an unknown component")
    if component["trigger_state"] == "duplicate_suppressed":
        raise IncrementalIdentityError("identity response replays unchanged evidence")
    raw_actions = response.get("candidate_actions")
    if not isinstance(raw_actions, list):
        raise IncrementalIdentityError("candidate_actions must be a list")
    expected_ids = set(component["candidate_prior_card_ids"])
    allowed_sources = set(component["source_block_ids"])
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            raise IncrementalIdentityError("identity action must be an object")
        _exact_keys(
            raw,
            {
                "prior_card_id",
                "action",
                "target_prior_card_id",
                "source_block_ids",
                "resolution_note",
            },
            "identity action",
        )
        card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if card_id not in expected_ids or card_id in seen:
            raise IncrementalIdentityError("identity actions do not exact-cover candidates")
        seen.add(card_id)
        action = _required_string(raw.get("action"), "action")
        if action not in ALLOWED_ACTIONS:
            raise IncrementalIdentityError("identity action is foreign")
        target = raw.get("target_prior_card_id")
        if target is not None:
            target = _required_string(target, "target_prior_card_id")
        if action == "link_to":
            if target not in expected_ids or target == card_id:
                raise IncrementalIdentityError("identity link target is invalid")
        elif target is not None:
            raise IncrementalIdentityError("only link_to may carry a target")
        sources = _string_list(raw.get("source_block_ids"), "source_block_ids")
        if not set(sources).issubset(allowed_sources):
            raise IncrementalIdentityError("identity action cites a foreign source block")
        note = _required_string(raw.get("resolution_note"), "resolution_note")
        if len(note) > 800:
            raise IncrementalIdentityError("identity resolution note is too long")
        actions.append(
            {
                "prior_card_id": card_id,
                "action": action,
                "target_prior_card_id": target,
                "source_block_ids": sorted(sources),
                "resolution_note": note,
            }
        )
    if seen != expected_ids:
        raise IncrementalIdentityError("identity actions do not exact-cover candidates")
    action_by_id = {row["prior_card_id"]: row for row in actions}
    for row in actions:
        if row["action"] == "link_to" and action_by_id[row["target_prior_card_id"]][
            "action"
        ] != "keep":
            raise IncrementalIdentityError("identity link target must be kept")

    expected_surface_review_ids = {
        row["review_item_id"]
        for row in component["review_leads"]
        if row.get("disputed_field") in {"alias_scope", "alias_target"}
    }
    raw_surface_actions = response.get("surface_scope_actions")
    if not isinstance(raw_surface_actions, list):
        raise IncrementalIdentityError("surface_scope_actions must be a list")
    surface_actions: list[dict[str, Any]] = []
    seen_surface_review_ids: set[str] = set()
    for raw in raw_surface_actions:
        if not isinstance(raw, Mapping):
            raise IncrementalIdentityError("surface-scope action must be an object")
        _exact_keys(
            raw,
            {
                "review_item_id",
                "action",
                "target_prior_card_id",
                "valid_block_ids",
                "source_block_ids",
                "evidence_needed",
                "resolution_note",
            },
            "surface-scope action",
        )
        review_item_id = _required_string(
            raw.get("review_item_id"), "review_item_id"
        )
        if (
            review_item_id not in expected_surface_review_ids
            or review_item_id in seen_surface_review_ids
        ):
            raise IncrementalIdentityError(
                "surface-scope actions do not exact-cover supplied leads"
            )
        seen_surface_review_ids.add(review_item_id)
        action = _required_string(raw.get("action"), "surface action")
        if action not in SURFACE_SCOPE_ACTIONS:
            raise IncrementalIdentityError("surface-scope action is foreign")
        target = raw.get("target_prior_card_id")
        if target is not None:
            target = _required_string(target, "target_prior_card_id")
        valid_block_ids = _string_list(
            raw.get("valid_block_ids"), "valid_block_ids", allow_empty=True
        )
        source_block_ids = _string_list(
            raw.get("source_block_ids"), "surface source_block_ids"
        )
        if not set(valid_block_ids).issubset(allowed_sources) or not set(
            source_block_ids
        ).issubset(allowed_sources):
            raise IncrementalIdentityError(
                "surface-scope action cites a foreign source block"
            )
        if action == "confirm_block_scope":
            if target not in expected_ids or not valid_block_ids:
                raise IncrementalIdentityError(
                    "block-scope action requires a target and valid blocks"
                )
        elif action in {"confirm_chapter_scope", "nominate_book_candidate"}:
            if target not in expected_ids or valid_block_ids:
                raise IncrementalIdentityError(
                    "chapter/book surface action has invalid target or block scope"
                )
        elif target is not None or valid_block_ids:
            raise IncrementalIdentityError(
                "pending/dormant surface action cannot grant scope"
            )
        evidence_needed = raw.get("evidence_needed")
        if action == "keep_pending":
            if evidence_needed not in SURFACE_EVIDENCE_NEEDS:
                raise IncrementalIdentityError(
                    "pending surface action lacks a bounded evidence request"
                )
        elif evidence_needed is not None:
            raise IncrementalIdentityError(
                "resolved surface action cannot request more evidence"
            )
        note = _required_string(raw.get("resolution_note"), "resolution_note")
        if len(note) > 800:
            raise IncrementalIdentityError("surface resolution note is too long")
        surface_actions.append(
            {
                "review_item_id": review_item_id,
                "action": action,
                "target_prior_card_id": target,
                "valid_block_ids": sorted(valid_block_ids),
                "source_block_ids": sorted(source_block_ids),
                "evidence_needed": evidence_needed,
                "resolution_note": note,
            }
        )
    if seen_surface_review_ids != expected_surface_review_ids:
        raise IncrementalIdentityError(
            "surface-scope actions do not exact-cover supplied leads"
        )
    status = (
        "pending"
        if any(row["action"] == "pending" for row in actions)
        or any(row["action"] == "keep_pending" for row in surface_actions)
        else (
            "provisional_link"
            if any(row["action"] == "link_to" for row in actions)
            else "resolved_distinct"
        )
    )
    body = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "validator_version": INDEX_VALIDATOR_VERSION,
        "identity_index_hash": verified["identity_index_hash"],
        "component_id": component_id,
        "component_key": component["component_key"],
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "evidence_manifest_hash": component["evidence_manifest_hash"],
        "hearing_number": int(component["prior_hearing_count"]) + 1,
        "status": status,
        "candidate_actions": sorted(actions, key=lambda row: row["prior_card_id"]),
        "surface_scope_actions": sorted(
            surface_actions, key=lambda row: row["review_item_id"]
        ),
        "review_item_ids": list(component["review_item_ids"]),
        "authority_effect": "none",
        "production_publish_performed": False,
    }
    return {**body, "decision_hash": canonical_hash(body)}


def normalize_surface_scope_action_coverage_v1(
    response: Mapping[str, Any], *, index: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Downscope misplaced or omitted surface rows without guessing authority."""

    verified = verify_incremental_identity_index_v1(index)
    normalized = deepcopy(dict(response))
    component_id = normalized.get("component_id")
    component = next(
        (
            row
            for row in verified["components"]
            if row["component_id"] == component_id
        ),
        None,
    )
    if component is None:
        return normalized, []
    raw_actions = normalized.get("surface_scope_actions")
    if not isinstance(raw_actions, list):
        return normalized, []

    leads = {row["review_item_id"]: row for row in component["review_leads"]}
    expected = {
        review_id: row
        for review_id, row in leads.items()
        if row.get("disputed_field") in {"alias_scope", "alias_target"}
    }
    supplied_ids = [
        row.get("review_item_id")
        for row in raw_actions
        if isinstance(row, Mapping)
    ]
    if len(supplied_ids) != len(set(supplied_ids)):
        return normalized, []

    kept: list[Any] = []
    records: list[dict[str, Any]] = []
    seen_expected: set[str] = set()
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            kept.append(raw)
            continue
        review_id = raw.get("review_item_id")
        if review_id in expected:
            kept.append(deepcopy(dict(raw)))
            seen_expected.add(str(review_id))
            continue
        lead = leads.get(review_id)
        if lead is None:
            kept.append(deepcopy(dict(raw)))
            continue
        records.append(
            {
                "normalization_kind": "non_surface_action_ignored",
                "component_id": component_id,
                "review_item_id": review_id,
                "disputed_field": lead.get("disputed_field"),
                "original_action": raw.get("action"),
                "normalized_action": None,
            }
        )

    allowed_order = list(component["source_block_ids"])
    allowed = set(allowed_order)
    for review_id in sorted(set(expected) - seen_expected):
        lead = expected[review_id]
        lead_sources = {
            str(row)
            for row in lead.get("source_block_ids") or []
            if str(row) in allowed
        }
        sources = [row for row in allowed_order if row in lead_sources]
        if not sources:
            sources = allowed_order[:1]
        evidence_needed = (
            "referent_attribution"
            if lead.get("disputed_field") == "alias_target"
            else "scope_disambiguation"
        )
        kept.append(
            {
                "review_item_id": review_id,
                "action": "keep_pending",
                "target_prior_card_id": None,
                "valid_block_ids": [],
                "source_block_ids": sources,
                "evidence_needed": evidence_needed,
                "resolution_note": (
                    "Downscoped by code: the response omitted this supplied "
                    "surface-scope lead, so no scope authority is granted."
                ),
            }
        )
        records.append(
            {
                "normalization_kind": "missing_surface_action_pending",
                "component_id": component_id,
                "review_item_id": review_id,
                "disputed_field": lead.get("disputed_field"),
                "original_action": None,
                "normalized_action": "keep_pending",
            }
        )

    normalized["surface_scope_actions"] = sorted(
        kept,
        key=lambda row: (
            str(row.get("review_item_id") or "")
            if isinstance(row, Mapping)
            else ""
        ),
    )
    return normalized, sorted(
        records,
        key=lambda row: (row["normalization_kind"], row["review_item_id"]),
    )


def verify_incremental_identity_decision_v1(
    decision: Mapping[str, Any], *, index: Mapping[str, Any]
) -> dict[str, Any]:
    if decision.get("schema_version") != DECISION_SCHEMA_VERSION:
        raise IncrementalIdentityError("foreign incremental identity decision schema")
    body = dict(decision)
    observed = _required_string(body.pop("decision_hash", None), "decision_hash")
    if canonical_hash(body) != observed:
        raise IncrementalIdentityError("incremental identity decision hash mismatch")
    normalized = validate_incremental_identity_response_v1(
        {
            "component_id": decision.get("component_id"),
            "candidate_actions": decision.get("candidate_actions"),
            "surface_scope_actions": decision.get("surface_scope_actions"),
        },
        index=index,
        request_fingerprint=_required_string(
            decision.get("request_fingerprint"), "request_fingerprint"
        ),
    )
    if canonical_json(normalized) != canonical_json(decision):
        raise IncrementalIdentityError("identity decision is not canonical validator output")
    return _clone(dict(decision))


def build_incremental_identity_ledger_v1(
    *,
    index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    previous_identity_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    verified = verify_incremental_identity_index_v1(index)
    previous_history: list[dict[str, Any]] = []
    previous_states: dict[str, dict[str, Any]] = {}
    if previous_identity_ledger is not None:
        previous = verify_incremental_identity_ledger_v1(previous_identity_ledger)
        if previous["state_lineage_id"] != verified["state_lineage_id"]:
            raise IncrementalIdentityError("identity ledger crosses state lineage")
        previous_history = _clone(previous["decision_history"])
        previous_states = {
            row["component_key"]: _clone(row) for row in previous["component_states"]
        }
    eligible = {
        row["component_id"]: row
        for row in verified["components"]
        if not row["overflow"] and row["trigger_state"] != "duplicate_suppressed"
    }
    decision_by_component: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        decision = verify_incremental_identity_decision_v1(raw, index=verified)
        component_id = decision["component_id"]
        if component_id in decision_by_component:
            raise IncrementalIdentityError("identity ledger repeats a component decision")
        decision_by_component[component_id] = decision
    if set(decision_by_component) != set(eligible):
        raise IncrementalIdentityError("identity decisions do not exact-cover eligible components")
    history = [*previous_history, *decision_by_component.values()]
    hashes = [row["decision_hash"] for row in history]
    if len(hashes) != len(set(hashes)):
        raise IncrementalIdentityError("identity decision history repeats a decision")
    states = previous_states
    for component_id, decision in decision_by_component.items():
        component = eligible[component_id]
        states[component["component_key"]] = {
            "component_key": component["component_key"],
            "latest_component_id": component_id,
            "candidate_prior_card_ids": list(component["candidate_prior_card_ids"]),
            "surface_keys": list(component["surface_keys"]),
            "latest_evidence_manifest_hash": component["evidence_manifest_hash"],
            "hearing_count": decision["hearing_number"],
            "status": decision["status"],
            "latest_decision_hash": decision["decision_hash"],
            "review_item_ids": list(component["review_item_ids"]),
        }
    body = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "validator_version": LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": verified["state_lineage_id"],
        "coverage_through_chapter_id": verified["coverage_through_chapter_id"],
        "identity_index_hashes": sorted(
            set(
                [verified["identity_index_hash"]]
                + (
                    list(previous_identity_ledger.get("identity_index_hashes") or [])
                    if previous_identity_ledger is not None
                    else []
                )
            )
        ),
        "decision_history": sorted(
            history, key=lambda row: (row["component_key"], row["hearing_number"])
        ),
        "component_states": sorted(states.values(), key=lambda row: row["component_key"]),
        "production_publish_performed": False,
    }
    return {**body, "identity_ledger_hash": canonical_hash(body)}


def verify_incremental_identity_ledger_v1(ledger: Mapping[str, Any]) -> dict[str, Any]:
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise IncrementalIdentityError("foreign incremental identity ledger schema")
    if ledger.get("validator_version") != LEDGER_VALIDATOR_VERSION:
        raise IncrementalIdentityError("incremental identity ledger validator mismatch")
    body = dict(ledger)
    observed = _required_string(body.pop("identity_ledger_hash", None), "identity_ledger_hash")
    if canonical_hash(body) != observed:
        raise IncrementalIdentityError("incremental identity ledger hash mismatch")
    if ledger.get("production_publish_performed") is not False:
        raise IncrementalIdentityError("incremental identity ledger claims publication")
    history = ledger.get("decision_history")
    states = ledger.get("component_states")
    if not isinstance(history, list) or not isinstance(states, list):
        raise IncrementalIdentityError("incremental identity ledger collections are malformed")
    state_keys: set[str] = set()
    for row in states:
        key = _required_string(row.get("component_key"), "component_key")
        if key in state_keys:
            raise IncrementalIdentityError("identity ledger repeats a component state")
        state_keys.add(key)
        if row.get("status") not in {"resolved_distinct", "provisional_link", "pending"}:
            raise IncrementalIdentityError("identity component state is foreign")
        _required_string(row.get("latest_evidence_manifest_hash"), "evidence hash")
        if int(row.get("hearing_count", -1)) < 1:
            raise IncrementalIdentityError("identity hearing count is invalid")
    return _clone(dict(ledger))


def apply_incremental_identity_ledger_to_prefix_v1(
    *, prefix_bundle: Mapping[str, Any], identity_ledger: Mapping[str, Any]
) -> dict[str, Any]:
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle)
    ledger = verify_incremental_identity_ledger_v1(identity_ledger)
    if prefix["state_lineage_id"] != ledger["state_lineage_id"]:
        raise IncrementalIdentityError("identity projection crosses state lineage")
    if prefix["coverage_through_chapter_id"] != ledger["coverage_through_chapter_id"]:
        raise IncrementalIdentityError("identity projection coverage is stale")
    states = {row["component_key"]: row for row in ledger["component_states"]}
    resolved_sets = [
        set(row["candidate_prior_card_ids"])
        for row in states.values()
        if row["status"] == "resolved_distinct"
        and len(row["candidate_prior_card_ids"]) >= 2
    ]
    resolved_ids = set().union(*resolved_sets) if resolved_sets else set()
    removed_uncertainty_ids = {
        row["uncertainty_id"]
        for row in prefix["prefix_identity_uncertainties"]
        if any(set(row["prior_card_ids"]).issubset(group) for group in resolved_sets)
    }
    claim_ids = {row["prior_card_id"] for row in prefix["claim_cards"]}
    active = {row["prior_card_id"]: _clone(row) for row in prefix["b0_context_cards"]}
    candidates = {
        row["prior_card_id"]: _clone(row)
        for row in prefix["candidate_only_context_cards"]
    }
    for card_id in sorted(resolved_ids):
        row = active.get(card_id) or candidates.get(card_id)
        if row is None:
            raise IncrementalIdentityError("identity projection references a foreign card")
        body = {key: _clone(value) for key, value in row.items() if key != "context_card_hash"}
        body["disputed_claims"] = [
            dispute
            for dispute in body.get("disputed_claims") or []
            if not (
                isinstance(dispute, Mapping)
                and dispute.get("disputed_field") == "identity_membership"
            )
        ]
        if card_id in claim_ids and not body["disputed_claims"]:
            body["authority_scope"] = CHAPTER_CONFIRMED_SCOPE
            active[card_id] = {**body, "context_card_hash": canonical_hash(body)}
            candidates.pop(card_id, None)
        else:
            body["authority_scope"] = CANDIDATE_ONLY_SCOPE
            candidates[card_id] = {**body, "context_card_hash": canonical_hash(body)}
            active.pop(card_id, None)
    body = {
        key: _clone(value)
        for key, value in prefix.items()
        if key not in {
            "prefix_bundle_hash",
            "b0_context_cards",
            "candidate_only_context_cards",
            "prefix_identity_uncertainties",
        }
    }
    body["b0_context_cards"] = sorted(active.values(), key=lambda row: row["prior_card_id"])
    body["candidate_only_context_cards"] = sorted(
        candidates.values(), key=lambda row: row["prior_card_id"]
    )
    body["prefix_identity_uncertainties"] = sorted(
        [
            _clone(row)
            for row in prefix["prefix_identity_uncertainties"]
            if row["uncertainty_id"] not in removed_uncertainty_ids
        ],
        key=lambda row: row["uncertainty_id"],
    )
    result = {**body, "prefix_bundle_hash": canonical_hash(body)}
    return verify_chapter_prefix_prior_bundle_v1(result)


def apply_incremental_identity_ledger_to_review_v1(
    *, review_ledger: Mapping[str, Any], identity_ledger: Mapping[str, Any]
) -> dict[str, Any]:
    review = verify_chapter_cycle_review_ledger_v1(review_ledger)
    identity = verify_incremental_identity_ledger_v1(identity_ledger)
    if review["state_lineage_id"] != identity["state_lineage_id"]:
        raise IncrementalIdentityError("identity decision crosses review lineage")
    decision_by_hash = {
        row["decision_hash"]: row for row in identity["decision_history"]
    }
    surface_action_by_review_id = {
        action["review_item_id"]: action
        for decision in identity["decision_history"]
        for action in decision.get("surface_scope_actions") or []
    }
    resolved_by_review_id = {
        review_id: row
        for row in identity["component_states"]
        if row["status"] in RESOLVED_STATUSES
        for review_id in row["review_item_ids"]
    }
    referenced_ids = set(resolved_by_review_id)
    known = {row["review_item_id"] for row in review["review_items"]}
    if not referenced_ids.issubset(known):
        raise IncrementalIdentityError("identity decision closes a foreign review item")
    rows = []
    followups: list[dict[str, Any]] = []
    for source in review["review_items"]:
        row = _clone(source)
        state = resolved_by_review_id.get(row["review_item_id"])
        surface_action = surface_action_by_review_id.get(row["review_item_id"])
        is_surface_dispute = row.get("disputed_field") in {
            "alias_scope",
            "alias_target",
        }
        closes_identity = (
            state is not None
            and row.get("disputed_field") in IDENTITY_DISPUTE_FIELDS
            and (
                not is_surface_dispute
                or (
                    surface_action is not None
                    and surface_action["action"] != "keep_pending"
                )
            )
        )
        reroutes_stable = (
            state is not None
            and state["status"] == "resolved_distinct"
            and row.get("disputed_field") in STABLE_CLAIM_FIELDS
        )
        if closes_identity or reroutes_stable:
            row["lifecycle_state"] = "closed"
            if reroutes_stable:
                decision = decision_by_hash[state["latest_decision_hash"]]
                action_sources = {
                    action["prior_card_id"]: action["source_block_ids"]
                    for action in decision["candidate_actions"]
                }
                source_ids = sorted(
                    set(row.get("source_block_ids") or []).union(
                        block_id
                        for card_id in row["subject_prior_card_ids"]
                        for block_id in action_sources.get(card_id, [])
                    )
                )
                evidence_hash = canonical_hash(
                    {
                        "closed_review_item_id": row["review_item_id"],
                        "identity_decision_hash": decision["decision_hash"],
                        "disputed_field": row["disputed_field"],
                        "source_block_ids": source_ids,
                    }
                )
                followup_body = {
                    "state_lineage_id": row["state_lineage_id"],
                    "chapter_id": row["chapter_id"],
                    "source_kind": "incremental_identity_followup",
                    "route": "stable_claim_rehearing",
                    "subject_prior_card_ids": list(row["subject_prior_card_ids"]),
                    "disputed_field": row["disputed_field"],
                    "source_block_ids": source_ids,
                    "evidence_manifest_hash": evidence_hash,
                    "lifecycle_state": "queued",
                    "authority_effect": "none",
                    "reason_code": "identity_membership_resolved",
                    "source_artifact_hash": decision["decision_hash"],
                    "reopen_classification": {
                        "route": "new_evidence",
                        "reason": "identity_component_resolved_distinct",
                    },
                }
                identity_body = {
                    key: _clone(value)
                    for key, value in followup_body.items()
                    if key
                    in {
                        "state_lineage_id",
                        "chapter_id",
                        "source_kind",
                        "route",
                        "subject_prior_card_ids",
                        "disputed_field",
                        "source_block_ids",
                        "evidence_manifest_hash",
                        "source_artifact_hash",
                    }
                }
                followups.append(
                    {
                        "review_item_id": "cycrev1_"
                        + canonical_hash(identity_body)[:20],
                        **followup_body,
                    }
                )
        rows.append(row)
    body = {
        key: _clone(value)
        for key, value in review.items()
        if key not in {"review_ledger_hash", "review_items"}
    }
    by_id = {
        row["review_item_id"]: row for row in [*rows, *followups]
    }
    body["review_items"] = sorted(by_id.values(), key=lambda row: row["review_item_id"])
    result = {**body, "review_ledger_hash": canonical_hash(body)}
    return verify_chapter_cycle_review_ledger_v1(result)


__all__ = [
    "ALLOWED_ACTIONS",
    "DECISION_SCHEMA_VERSION",
    "DEFAULT_MAX_SOURCE_BLOCKS",
    "INDEX_SCHEMA_VERSION",
    "IncrementalIdentityError",
    "LEDGER_SCHEMA_VERSION",
    "RenderedIncrementalIdentityRequestV1",
    "apply_incremental_identity_ledger_to_prefix_v1",
    "apply_incremental_identity_ledger_to_review_v1",
    "build_incremental_identity_index_v1",
    "build_incremental_identity_ledger_v1",
    "incremental_identity_response_schema_v1",
    "normalize_surface_scope_action_coverage_v1",
    "render_incremental_identity_request_v1",
    "validate_incremental_identity_response_v1",
    "verify_incremental_identity_decision_v1",
    "verify_incremental_identity_index_v1",
    "verify_incremental_identity_ledger_v1",
]
