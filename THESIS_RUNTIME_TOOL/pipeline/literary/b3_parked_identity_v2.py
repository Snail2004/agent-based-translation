"""Many-to-many parked cross-chapter identity projection for B3.

The original parked-identity contract assumed that one persistent card could
belong to only one unresolved hearing.  A later, deliberately reopened
hearing can ask a narrower question about the same card, so the v2 projection
preserves every applicable hearing instead of choosing one.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b3_parked_identity_v1 import (
    B3ParkedIdentityError,
    _component_requests,
    _mapping,
    _read_list,
    _string_set,
    _text,
    _tree_hash,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256


PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2 = "literary_b3_parked_identity_index_v2"


def build_parked_identity_index_v2(hearing_root: Path) -> dict[str, Any]:
    root = Path(hearing_root).resolve()
    if not root.is_dir():
        raise B3ParkedIdentityError("cross-chapter hearing root is absent")
    decisions_path = root / "validated_decisions.json"
    decisions = _read_list(decisions_path, "validated hearing decisions")
    requests = _component_requests(root)
    parked: list[dict[str, Any]] = []
    seen_components: set[str] = set()
    for raw_decision in decisions:
        decision = _mapping(raw_decision, "hearing decision")
        if decision.get("verdict") != "insufficient_evidence":
            continue
        component_id = _text(decision.get("component_id"), "hearing component_id")
        if component_id in seen_components:
            raise B3ParkedIdentityError(
                "parked hearing decision repeats a component"
            )
        seen_components.add(component_id)
        resolution = _text(
            decision.get("resolution_condition"), "hearing resolution_condition"
        )
        request = requests.get(component_id)
        if request is None:
            raise B3ParkedIdentityError(
                "parked hearing decision has no unique component request"
            )
        sections = _mapping(request.get("sections"), "hearing request sections")
        card_ids = {
            _text(row.get("entity_id"), "current snapshot entity_id")
            for raw in sections.get("current_card_snapshots") or []
            for row in [_mapping(raw, "current card snapshot")]
        }
        card_ids.update(
            _text(value, "prior_card_id")
            for value in sections.get("prior_card_ids") or []
        )
        if not card_ids:
            raise B3ParkedIdentityError("parked hearing has an empty card set")
        parked.append(
            {
                "hearing_component_id": component_id,
                "resolution_condition": resolution,
                "card_ids": sorted(card_ids),
            }
        )
    parked.sort(key=lambda row: row["hearing_component_id"])
    body = {
        "schema_version": PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2,
        "source_hearing_root": str(root),
        "source_hearing_tree_hash": _tree_hash(root),
        "source_validated_decisions_sha256": file_sha256(decisions_path),
        "parked_identities": parked,
    }
    return {**body, "index_hash": canonical_hash(body)}


def empty_parked_identity_index_v2() -> dict[str, Any]:
    body = {
        "schema_version": PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2,
        "source_hearing_root": None,
        "source_hearing_tree_hash": None,
        "source_validated_decisions_sha256": None,
        "parked_identities": [],
    }
    return {**body, "index_hash": canonical_hash(body)}


def verify_parked_identity_index_v2(index: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(index))
    observed = row.pop("index_hash", None)
    if observed != canonical_hash(row):
        raise B3ParkedIdentityError("parked identity index hash mismatch")
    if index.get("schema_version") != PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2:
        raise B3ParkedIdentityError("foreign parked identity index")
    parked = index.get("parked_identities")
    if not isinstance(parked, list):
        raise B3ParkedIdentityError("parked identity rows must be a list")
    seen_components: set[str] = set()
    for raw in parked:
        item = _mapping(raw, "parked identity row")
        if set(item) != {
            "hearing_component_id",
            "resolution_condition",
            "card_ids",
        }:
            raise B3ParkedIdentityError("parked identity row shape differs")
        component_id = _text(
            item.get("hearing_component_id"), "hearing_component_id"
        )
        _text(item.get("resolution_condition"), "resolution_condition")
        card_ids = _string_set(item.get("card_ids"), "parked card_ids")
        if component_id in seen_components:
            raise B3ParkedIdentityError("parked identity component repeats")
        if list(item["card_ids"]) != sorted(card_ids):
            raise B3ParkedIdentityError("parked card_ids are not canonical")
        seen_components.add(component_id)
    return deepcopy(dict(index))


def parked_identities_by_card_id_v2(
    index: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    verified = verify_parked_identity_index_v2(index)
    result: dict[str, list[dict[str, Any]]] = {}
    for raw in verified["parked_identities"]:
        item = deepcopy(dict(raw))
        for card_id in item["card_ids"]:
            result.setdefault(card_id, []).append(deepcopy(item))
    for rows in result.values():
        rows.sort(key=lambda row: row["hearing_component_id"])
    return result


def attach_parked_identities_to_candidate_cards_v2(
    *,
    candidate_cards: Mapping[str, Mapping[str, Any]],
    index: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    by_card = parked_identities_by_card_id_v2(index)
    candidate_to_ref = {
        card_id: _text(card.get("referent_ref"), "candidate referent_ref")
        for card_id, card in candidate_cards.items()
    }
    supplied_ids = set(candidate_cards)
    result: dict[str, dict[str, Any]] = {}
    for card_id, raw_card in candidate_cards.items():
        card = deepcopy(dict(raw_card))
        markers: list[dict[str, Any]] = []
        for parked in by_card.get(card_id, []):
            other_ids = set(parked["card_ids"]) - {card_id}
            markers.append(
                {
                    "hearing_component_id": parked["hearing_component_id"],
                    "resolution_condition": parked["resolution_condition"],
                    "co_parked_referent_refs": sorted(
                        candidate_to_ref[value]
                        for value in other_ids
                        if value in candidate_to_ref
                    ),
                    "parked_set_partially_supplied": not other_ids <= supplied_ids,
                }
            )
        if markers:
            card["parked_identities"] = markers
        result[card_id] = card
    return result


def parked_hearings_for_card_ids_v2(
    *, index: Mapping[str, Any], card_ids: Sequence[str]
) -> list[dict[str, Any]]:
    supplied = {value for value in card_ids if isinstance(value, str) and value}
    if not supplied:
        return []
    verified = verify_parked_identity_index_v2(index)
    return [
        deepcopy(dict(row))
        for row in verified["parked_identities"]
        if supplied <= set(row["card_ids"])
    ]


__all__ = [
    "PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2",
    "attach_parked_identities_to_candidate_cards_v2",
    "build_parked_identity_index_v2",
    "empty_parked_identity_index_v2",
    "parked_hearings_for_card_ids_v2",
    "parked_identities_by_card_id_v2",
    "verify_parked_identity_index_v2",
]
