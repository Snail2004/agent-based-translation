"""Deterministic projection of parked cross-chapter identity hearings into B3."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, file_sha256


PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V1 = "literary_b3_parked_identity_index_v1"


class B3ParkedIdentityError(RuntimeError):
    pass


def build_parked_identity_index_v1(hearing_root: Path) -> dict[str, Any]:
    root = Path(hearing_root).resolve()
    if not root.is_dir():
        raise B3ParkedIdentityError("cross-chapter hearing root is absent")
    decisions_path = root / "validated_decisions.json"
    decisions = _read_list(decisions_path, "validated hearing decisions")
    requests = _component_requests(root)
    parked: list[dict[str, Any]] = []
    assigned_cards: dict[str, str] = {}
    for raw_decision in decisions:
        decision = _mapping(raw_decision, "hearing decision")
        if decision.get("verdict") != "insufficient_evidence":
            continue
        component_id = _text(decision.get("component_id"), "hearing component_id")
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
        for card_id in card_ids:
            previous = assigned_cards.setdefault(card_id, component_id)
            if previous != component_id:
                raise B3ParkedIdentityError(
                    "one card belongs to multiple parked identity hearings"
                )
        parked.append(
            {
                "hearing_component_id": component_id,
                "resolution_condition": resolution,
                "card_ids": sorted(card_ids),
            }
        )
    parked.sort(key=lambda row: row["hearing_component_id"])
    body = {
        "schema_version": PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V1,
        "source_hearing_root": str(root),
        "source_hearing_tree_hash": _tree_hash(root),
        "source_validated_decisions_sha256": file_sha256(decisions_path),
        "parked_identities": parked,
    }
    return {**body, "index_hash": canonical_hash(body)}


def empty_parked_identity_index_v1() -> dict[str, Any]:
    body = {
        "schema_version": PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V1,
        "source_hearing_root": None,
        "source_hearing_tree_hash": None,
        "source_validated_decisions_sha256": None,
        "parked_identities": [],
    }
    return {**body, "index_hash": canonical_hash(body)}


def verify_parked_identity_index_v1(index: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(index))
    observed = row.pop("index_hash", None)
    if observed != canonical_hash(row):
        raise B3ParkedIdentityError("parked identity index hash mismatch")
    if index.get("schema_version") != PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V1:
        raise B3ParkedIdentityError("foreign parked identity index")
    parked = index.get("parked_identities")
    if not isinstance(parked, list):
        raise B3ParkedIdentityError("parked identity rows must be a list")
    seen_components: set[str] = set()
    seen_cards: set[str] = set()
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
        if component_id in seen_components or seen_cards.intersection(card_ids):
            raise B3ParkedIdentityError("parked identity index is ambiguous")
        seen_components.add(component_id)
        seen_cards.update(card_ids)
    return deepcopy(dict(index))


def parked_identity_by_card_id_v1(
    index: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    verified = verify_parked_identity_index_v1(index)
    result: dict[str, dict[str, Any]] = {}
    for raw in verified["parked_identities"]:
        item = deepcopy(dict(raw))
        for card_id in item["card_ids"]:
            result[card_id] = deepcopy(item)
    return result


def attach_parked_identity_to_candidate_cards_v1(
    *,
    candidate_cards: Mapping[str, Mapping[str, Any]],
    index: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    by_card = parked_identity_by_card_id_v1(index)
    candidate_to_ref = {
        card_id: _text(card.get("referent_ref"), "candidate referent_ref")
        for card_id, card in candidate_cards.items()
    }
    supplied_ids = set(candidate_cards)
    result: dict[str, dict[str, Any]] = {}
    for card_id, raw_card in candidate_cards.items():
        card = deepcopy(dict(raw_card))
        parked = by_card.get(card_id)
        if parked is not None:
            other_ids = set(parked["card_ids"]) - {card_id}
            card["parked_identity"] = {
                "hearing_component_id": parked["hearing_component_id"],
                "resolution_condition": parked["resolution_condition"],
                "co_parked_referent_refs": sorted(
                    candidate_to_ref[value]
                    for value in other_ids
                    if value in candidate_to_ref
                ),
                "parked_set_partially_supplied": not other_ids <= supplied_ids,
            }
        result[card_id] = card
    return result


def parked_hearing_for_card_ids_v1(
    *, index: Mapping[str, Any], card_ids: Sequence[str]
) -> dict[str, Any] | None:
    supplied = {value for value in card_ids if isinstance(value, str) and value}
    if not supplied:
        return None
    verified = verify_parked_identity_index_v1(index)
    matches = [
        row
        for row in verified["parked_identities"]
        if supplied <= set(row["card_ids"])
    ]
    if len(matches) > 1:
        raise B3ParkedIdentityError("card set maps to multiple parked hearings")
    return deepcopy(dict(matches[0])) if matches else None


def _component_requests(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "components").glob("*/request.json")):
        request = _read_object(path, "hearing component request")
        fingerprint = request.get("request_fingerprint")
        expected = canonical_hash(
            {
                "request_schema_version": request.get("schema_version"),
                "component_id": request.get("component_id"),
                "queue_hash": request.get("queue_hash"),
                "prompt_id": request.get("prompt_id"),
                "prompt_sha256": request.get("prompt_sha256"),
                "response_schema_hash": request.get("response_schema_hash"),
                "model_contract": deepcopy(
                    dict(_mapping(request.get("model_contract"), "model contract"))
                ),
                "sections_hash": canonical_hash(
                    dict(_mapping(request.get("sections"), "request sections"))
                ),
            }
        )
        if fingerprint != expected:
            raise B3ParkedIdentityError("hearing component request hash mismatch")
        component_id = _text(request.get("component_id"), "request component_id")
        if component_id in result:
            raise B3ParkedIdentityError("hearing component request is duplicated")
        result[component_id] = request
    return result


def _tree_hash(root: Path) -> str:
    return canonical_hash(
        [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_sha256(path),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
    )


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B3ParkedIdentityError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise B3ParkedIdentityError(f"{label} must be an object")
    return value


def _read_list(path: Path, label: str) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B3ParkedIdentityError(f"cannot read {label}") from exc
    if not isinstance(value, list):
        raise B3ParkedIdentityError(f"{label} must be a list")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise B3ParkedIdentityError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B3ParkedIdentityError(f"{label} must be non-empty text")
    return value


def _string_set(value: Any, label: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise B3ParkedIdentityError(f"{label} must be a non-empty list")
    result = {_text(item, label) for item in value}
    if len(result) != len(value):
        raise B3ParkedIdentityError(f"{label} repeats values")
    return result


__all__ = [
    "PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V1",
    "B3ParkedIdentityError",
    "attach_parked_identity_to_candidate_cards_v1",
    "build_parked_identity_index_v1",
    "empty_parked_identity_index_v1",
    "parked_hearing_for_card_ids_v1",
    "parked_identity_by_card_id_v1",
    "verify_parked_identity_index_v1",
]
