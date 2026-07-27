"""Mechanical destinations for typed B2 review requests.

This module deliberately contains no natural-language classification.  The
model-authored ``blocking_kind`` is the only routing input; all other
decisions use ids, sets, and explicitly supplied case metadata.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b2_review_routing_v1 import (
    ReviewRoutingError,
    mechanical_anchor_defect_v1,
    route_review,
)
from pipeline.literary.checkpoint import canonical_hash


ROUTING_PLAN_SCHEMA_VERSION = "literary_b2_review_routing_plan_v1"


class ReviewResolutionError(RuntimeError):
    pass


def open_within_identity_cases_v1(
    local_audit_artifact: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project Local-Auditor same-referent rows into scan-ref case sets."""

    raw_decisions = local_audit_artifact.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ReviewResolutionError("Local Auditor artifact has no decision list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_decisions:
        if not isinstance(raw, Mapping):
            raise ReviewResolutionError("Local Auditor decision is malformed")
        if raw.get("component_kind") != "same_referent_proposal":
            continue
        action = raw.get("action")
        if action == "refer_cross_chapter":
            continue
        if action not in {
            "accept_proposal",
            "revise_proposal",
            "reject_proposal",
            "keep_pending",
        }:
            raise ReviewResolutionError(
                "same-referent case has an unknown decision action"
            )
        case_id = _required_nonempty(raw.get("component_id"), "within case id")
        if case_id in seen:
            raise ReviewResolutionError("Local Auditor repeats a within case")
        seen.add(case_id)
        proposal = raw.get("original_proposal")
        if not isinstance(proposal, Mapping):
            raise ReviewResolutionError(
                "same-referent case has no original proposal"
            )
        members = {
            _required_nonempty(raw.get("subject_ref"), "within subject ref"),
            _required_nonempty(proposal.get("target_ref"), "within target ref"),
        }
        if len(members) != 2:
            raise ReviewResolutionError(
                "same-referent case is reflexive or malformed"
            )
        result.append(
            {
                "case_id": case_id,
                "destination": "WITHIN",
                "member_refs": sorted(members),
                "has_verdict": action != "keep_pending",
                "decision_action": action,
            }
        )
    return sorted(result, key=lambda row: row["case_id"])


def open_cross_identity_cases_v1(
    hearing_queue: Mapping[str, Any],
    *,
    decided_component_ids: Sequence[str] = (),
    superseded_component_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Project existing identity hearings into persistent-card case sets."""

    raw_components = hearing_queue.get("components")
    if not isinstance(raw_components, list):
        raise ReviewResolutionError("cross-chapter queue has no component list")
    decided = {_required_nonempty(value, "decided component id") for value in decided_component_ids}
    superseded = {
        _required_nonempty(value, "superseded component id")
        for value in superseded_component_ids
    }
    # The decision ledger is append-only, so an older insufficient-evidence
    # decision remains recorded after a later decision retires its pending case.
    # Projection-level supersession is the effective state for routing.
    decided -= superseded
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_components:
        if not isinstance(raw, Mapping):
            raise ReviewResolutionError("cross-chapter component is malformed")
        if raw.get("review_route") != "identity_auditor":
            continue
        case_id = _required_nonempty(raw.get("component_id"), "cross case id")
        if case_id in seen:
            raise ReviewResolutionError("cross-chapter queue repeats a case")
        seen.add(case_id)
        members = _component_card_ids(raw)
        if len(members) < 2:
            continue
        row = {
            "case_id": case_id,
            "destination": "CROSS",
            "member_refs": sorted(members),
            "has_verdict": case_id in decided or case_id in superseded,
        }
        if case_id in superseded:
            row["case_state"] = "superseded"
        result.append(row)
    return sorted(result, key=lambda row: row["case_id"])


def route_b_destination(
    competing_card_ids: Sequence[str],
    cards_by_id: Mapping[str, Mapping[str, Any]],
    current_chapter_id: str,
) -> str:
    """Return WITHIN or CROSS from card provenance only."""

    ids = [str(value) for value in competing_card_ids]
    if len(ids) < 2 or len(ids) != len(set(ids)):
        raise ReviewResolutionError("route-B competing cards are malformed")
    missing = sorted(set(ids) - set(cards_by_id))
    if missing:
        raise ReviewResolutionError(
            f"route-B competing cards are outside the supplied scope: {missing}"
        )
    origins = {
        str(cards_by_id[card_id]["first_seen"]["chapter_id"]) for card_id in ids
    }
    if origins == {str(current_chapter_id)}:
        return "WITHIN"
    return "CROSS"


def route_b_member_set(
    *,
    destination: str,
    competing_card_ids: Sequence[str],
    cards_by_id: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Normalize card ids into the namespace owned by the destination."""

    ids = [str(value) for value in competing_card_ids]
    if destination == "CROSS":
        return set(ids)
    if destination != "WITHIN":
        raise ReviewResolutionError(f"unknown route-B destination: {destination!r}")
    result: set[str] = set()
    for card_id in ids:
        card = cards_by_id[card_id]
        source_refs = card.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            raise ReviewResolutionError(
                f"WITHIN card has no source_refs: {card_id}"
            )
        if not all(isinstance(value, str) and value.strip() for value in source_refs):
            raise ReviewResolutionError(
                f"WITHIN card source_refs are malformed: {card_id}"
            )
        result.update(source_refs)
    return result


def _case_member_set(case: Mapping[str, Any], destination: str) -> set[str]:
    members = case.get("member_refs")
    if not isinstance(members, list) or not members:
        raise ReviewResolutionError("open identity case has no member_refs")
    if not all(isinstance(value, str) and value.strip() for value in members):
        raise ReviewResolutionError("open identity case member_refs are malformed")
    if case.get("destination") != destination:
        raise ReviewResolutionError("identity case destination differs")
    if case.get("case_state") not in {None, "superseded"}:
        raise ReviewResolutionError("identity case has an unknown state")
    return set(members)


def _case_has_verdict(case: Mapping[str, Any]) -> bool:
    value = case.get("has_verdict")
    if not isinstance(value, bool):
        raise ReviewResolutionError("identity case lacks explicit has_verdict")
    return value


def match_route_b_case_v1(
    *,
    member_refs: set[str],
    destination: str,
    open_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach, open, or hold according to the section 7.3 set rules."""

    for raw_case in open_cases:
        case = deepcopy(dict(raw_case))
        case_members = _case_member_set(case, destination)
        if member_refs <= case_members:
            if case.get("case_state") == "superseded":
                return {
                    "action": "attached_case_superseded",
                    "destination": destination,
                    "case_id": _required_nonempty(
                        case.get("case_id"), "superseded identity case id"
                    ),
                    "member_refs": sorted(member_refs),
                    "matched_case_member_refs": sorted(case_members),
                }
            return {
                "action": "attach_existing_case",
                "destination": destination,
                "case_id": _required_nonempty(
                    case.get("case_id"), "open identity case id"
                ),
                "member_refs": sorted(member_refs),
                "matched_case_member_refs": sorted(case_members),
            }
        if member_refs & case_members and not (member_refs <= case_members):
            if _case_has_verdict(case):
                break
            return {
                "action": "hold_partial_overlap",
                "destination": destination,
                "case_id": _required_nonempty(
                    case.get("case_id"), "overlapping identity case id"
                ),
                "member_refs": sorted(member_refs),
                "matched_case_member_refs": sorted(case_members),
                "identity_authority_granted": False,
            }
    return {
        "action": "open_new_case",
        "destination": destination,
        "case_id": None,
        "member_refs": sorted(member_refs),
        "matched_case_member_refs": [],
    }


def resolve_route_b_review_v1(
    *,
    review: Mapping[str, Any],
    cards_by_id: Mapping[str, Mapping[str, Any]],
    current_chapter_id: str,
    open_within_cases: Sequence[Mapping[str, Any]] = (),
    open_cross_cases: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if route_review(review) != "B":
        raise ReviewResolutionError("non-route-B review entered identity resolution")
    review_id = review.get("review_id")
    competing = review.get("competing_card_ids")
    if not isinstance(competing, list) or len(competing) < 2:
        raise ReviewResolutionError("route-B review lacks competing cards")
    destination = route_b_destination(
        competing, cards_by_id, current_chapter_id
    )
    member_refs = route_b_member_set(
        destination=destination,
        competing_card_ids=competing,
        cards_by_id=cards_by_id,
    )
    open_cases = (
        open_within_cases if destination == "WITHIN" else open_cross_cases
    )
    result = match_route_b_case_v1(
        member_refs=member_refs,
        destination=destination,
        open_cases=open_cases,
    )
    return {
        "review_id": review_id,
        "route": "B",
        "blocking_kind": review["blocking_kind"],
        "competing_card_ids": list(competing),
        **result,
    }


def resolve_route_c_review_v1(review: Mapping[str, Any]) -> dict[str, Any]:
    """Route C is mechanical and never reaches a model."""

    try:
        return mechanical_anchor_defect_v1(review)
    except (KeyError, ReviewRoutingError) as exc:
        raise ReviewResolutionError(str(exc)) from exc


def build_review_routing_plan_v1(
    *,
    reviews: Sequence[Mapping[str, Any]],
    cards_by_id: Mapping[str, Mapping[str, Any]],
    current_chapter_id: str,
    open_within_cases: Sequence[Mapping[str, Any]] = (),
    open_cross_cases: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Classify every review once and produce route-specific work lists."""

    seen: set[str] = set()
    route_a: list[dict[str, Any]] = []
    route_b: list[dict[str, Any]] = []
    route_c: list[dict[str, Any]] = []
    route_d: list[dict[str, Any]] = []
    route_e: list[dict[str, Any]] = []
    for raw_review in reviews:
        review = deepcopy(dict(raw_review))
        review_id = review.get("review_id")
        if not isinstance(review_id, str) or not review_id:
            raise ReviewResolutionError("review has no review_id")
        if review_id in seen:
            raise ReviewResolutionError("review id repeats")
        seen.add(review_id)
        try:
            destination = route_review(review)
        except (KeyError, ReviewRoutingError) as exc:
            raise ReviewResolutionError("review has no typed route") from exc
        if destination == "A":
            route_a.append({"review_id": review_id, "route": "A"})
        elif destination == "B":
            route_b.append(
                resolve_route_b_review_v1(
                    review=review,
                    cards_by_id=cards_by_id,
                    current_chapter_id=current_chapter_id,
                    open_within_cases=open_within_cases,
                    open_cross_cases=open_cross_cases,
                )
            )
        elif destination == "C":
            route_c.append(resolve_route_c_review_v1(review))
        elif destination == "D":
            route_d.append(
                {
                    "review_id": review_id,
                    "route": "D",
                    "blocking_kind": review["blocking_kind"],
                }
            )
        elif destination == "E":
            route_e.append(
                {
                    "review_id": review_id,
                    "route": "E",
                    "blocking_kind": "frame_structure",
                }
            )
        else:  # route_review currently makes this unreachable by contract.
            raise ReviewResolutionError(f"unhandled review route: {destination!r}")
    return {
        "route_a": route_a,
        "route_b": route_b,
        "route_c": route_c,
        "route_d": route_d,
        "route_e": route_e,
        "review_ids": sorted(seen),
    }


def build_review_routing_plan_from_artifacts_v1(
    *,
    b2_artifact: Mapping[str, Any],
    chapter_registry: Mapping[str, Any],
    local_audit_artifact: Mapping[str, Any],
    hearing_queue: Mapping[str, Any],
    decided_cross_component_ids: Sequence[str] = (),
    superseded_cross_component_ids: Sequence[str] = (),
    candidate_scope_cards: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a sealed, authority-free routing overlay from existing artifacts."""

    _verify_hashed_input(b2_artifact, "artifact_hash", "B2 artifact")
    _verify_hashed_input(
        chapter_registry, "registry_hash", "chapter registry"
    )
    _verify_hashed_input(
        local_audit_artifact,
        "artifact_hash",
        "Local Auditor artifact",
    )
    _verify_hashed_input(
        hearing_queue, "queue_hash", "cross-chapter hearing queue"
    )
    chapter_id = _required_nonempty(
        b2_artifact.get("chapter_id"), "B2 chapter id"
    )
    if chapter_registry.get("chapter_id") != chapter_id:
        raise ReviewResolutionError("B2 and registry chapters differ")
    if hearing_queue.get("chapter_id") != chapter_id:
        raise ReviewResolutionError("B2 and hearing-queue chapters differ")
    if local_audit_artifact.get("chapter_id") != chapter_id:
        raise ReviewResolutionError("B2 and Local-Auditor chapters differ")
    raw_cards = chapter_registry.get("cards")
    if not isinstance(raw_cards, list):
        raise ReviewResolutionError("chapter registry has no card list")
    cards_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_cards:
        if not isinstance(raw, Mapping):
            raise ReviewResolutionError("chapter registry card is malformed")
        card = deepcopy(dict(raw))
        card_id = _required_nonempty(card.get("entity_id"), "registry entity id")
        if card_id in cards_by_id:
            raise ReviewResolutionError("chapter registry repeats an entity id")
        cards_by_id[card_id] = card
    for raw in candidate_scope_cards:
        if not isinstance(raw, Mapping):
            raise ReviewResolutionError("B2 candidate-scope card is malformed")
        card = deepcopy(dict(raw))
        card_id = _required_nonempty(
            card.get("effective_entity_id") or card.get("entity_id"),
            "B2 candidate-scope entity id",
        )
        first_seen = card.get("first_seen")
        if not isinstance(first_seen, Mapping):
            raise ReviewResolutionError(
                f"B2 candidate-scope card has no first_seen: {card_id}"
            )
        _required_nonempty(
            first_seen.get("chapter_id"),
            "B2 candidate-scope first-seen chapter id",
        )
        source_refs = card.get("source_refs")
        if source_refs is None:
            provenance_refs = card.get("provenance_refs")
            if not isinstance(provenance_refs, list) or not provenance_refs or not all(
                isinstance(row, Mapping)
                and isinstance(row.get("chapter_id"), str)
                and row["chapter_id"].strip()
                and isinstance(row.get("block_id"), str)
                and row["block_id"].strip()
                for row in provenance_refs
            ):
                raise ReviewResolutionError(
                    f"B2 candidate-scope card has malformed provenance_refs: {card_id}"
                )
        elif not isinstance(source_refs, list) or not all(
            isinstance(value, str) and value.strip() for value in source_refs
        ):
            raise ReviewResolutionError(
                f"B2 candidate-scope card has malformed source_refs: {card_id}"
            )
        if card_id in cards_by_id:
            continue
        card["entity_id"] = card_id
        cards_by_id[card_id] = card
    prior_snapshots_by_id: dict[str, dict[str, Any]] = {}
    components = hearing_queue.get("components")
    if not isinstance(components, list):
        raise ReviewResolutionError("cross-chapter queue has no component list")
    for raw_component in components:
        if not isinstance(raw_component, Mapping):
            raise ReviewResolutionError("cross-chapter component is malformed")
        snapshots = raw_component.get("prior_candidate_snapshots") or []
        if not isinstance(snapshots, list):
            raise ReviewResolutionError(
                "cross-chapter prior candidate snapshots are malformed"
            )
        for raw_snapshot in snapshots:
            if not isinstance(raw_snapshot, Mapping):
                raise ReviewResolutionError(
                    "cross-chapter prior candidate snapshot is malformed"
                )
            snapshot = deepcopy(dict(raw_snapshot))
            card_id = _required_nonempty(
                snapshot.get("prior_card_id"), "prior candidate card id"
            )
            if card_id in cards_by_id:
                continue
            provenance = snapshot.get("provenance_refs")
            if not isinstance(provenance, list) or not provenance:
                raise ReviewResolutionError(
                    f"prior candidate has no provenance refs: {card_id}"
                )
            origin_chapters = [
                _required_nonempty(
                    row.get("chapter_id") if isinstance(row, Mapping) else None,
                    "prior candidate provenance chapter id",
                )
                for row in provenance
            ]
            if chapter_id in origin_chapters:
                raise ReviewResolutionError(
                    f"prior candidate provenance is not prior: {card_id}"
                )
            snapshot["entity_id"] = card_id
            snapshot["first_seen"] = {"chapter_id": origin_chapters[0]}
            existing = prior_snapshots_by_id.get(card_id)
            if existing is not None and existing != snapshot:
                raise ReviewResolutionError(
                    f"cross-chapter queue disagrees on prior card: {card_id}"
                )
            prior_snapshots_by_id[card_id] = snapshot
    cards_by_id.update(prior_snapshots_by_id)
    reviews = b2_artifact.get("review_requests")
    if not isinstance(reviews, list):
        raise ReviewResolutionError("B2 artifact has no review list")
    plan = build_review_routing_plan_v1(
        reviews=reviews,
        cards_by_id=cards_by_id,
        current_chapter_id=chapter_id,
        open_within_cases=open_within_identity_cases_v1(local_audit_artifact),
        open_cross_cases=open_cross_identity_cases_v1(
            hearing_queue,
            decided_component_ids=decided_cross_component_ids,
            superseded_component_ids=superseded_cross_component_ids,
        ),
    )
    body = {
        "schema_version": ROUTING_PLAN_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_b2_artifact_hash": _required_nonempty(
            b2_artifact.get("artifact_hash"), "B2 artifact hash"
        ),
        "source_registry_hash": _required_nonempty(
            chapter_registry.get("registry_hash"), "registry hash"
        ),
        "source_local_audit_artifact_hash": _required_nonempty(
            local_audit_artifact.get("artifact_hash"),
            "Local Auditor artifact hash",
        ),
        "source_hearing_queue_hash": _required_nonempty(
            hearing_queue.get("queue_hash"), "hearing queue hash"
        ),
        **plan,
        "model_call_performed": False,
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
    }
    sealed = {**body, "routing_plan_hash": canonical_hash(body)}
    verify_review_routing_plan_v1(sealed)
    return sealed


def verify_review_routing_plan_v1(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != ROUTING_PLAN_SCHEMA_VERSION:
        raise ReviewResolutionError("foreign review-routing plan schema")
    observed = plan.get("routing_plan_hash")
    if not isinstance(observed, str):
        raise ReviewResolutionError("review-routing plan hash is absent")
    body = deepcopy(dict(plan))
    body.pop("routing_plan_hash", None)
    if canonical_hash(body) != observed:
        raise ReviewResolutionError("review-routing plan hash mismatch")
    if (
        plan.get("model_call_performed") is not False
        or plan.get("identity_authority_granted") is not False
        or plan.get("registry_mutation_performed") is not False
    ):
        raise ReviewResolutionError("review-routing plan claims forbidden authority")
    declared = plan.get("review_ids")
    if not isinstance(declared, list) or len(declared) != len(set(declared)):
        raise ReviewResolutionError("review-routing plan id index is malformed")
    routed: list[str] = []
    for collection, route in (
        ("route_a", "A"),
        ("route_b", "B"),
        ("route_c", "C"),
        ("route_d", "D"),
        ("route_e", "E"),
    ):
        rows = plan.get(collection)
        if not isinstance(rows, list):
            raise ReviewResolutionError(
                f"review-routing plan lacks {collection}"
            )
        for raw in rows:
            if not isinstance(raw, Mapping) or raw.get("route") != route:
                raise ReviewResolutionError(
                    f"review-routing plan has a malformed {route} row"
                )
            routed.append(
                _required_nonempty(raw.get("review_id"), "routed review id")
            )
    if len(routed) != len(set(routed)) or sorted(routed) != sorted(declared):
        raise ReviewResolutionError(
            "review-routing plan does not exact-cover B2 reviews"
        )


def _component_card_ids(component: Mapping[str, Any]) -> set[str]:
    members: set[str] = set()
    for field in ("prior_card_ids", "current_entity_ids"):
        value = component.get(field)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ReviewResolutionError(
                f"cross identity component {field} is malformed"
            )
        members.update(
            _required_nonempty(item, f"cross identity {field}") for item in value
        )
    for field in ("prior_card_id", "current_entity_id"):
        value = component.get(field)
        if value is not None:
            members.add(_required_nonempty(value, f"cross identity {field}"))
    return members


def _required_nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewResolutionError(f"{label} is absent")
    return value


def _verify_hashed_input(
    value: Mapping[str, Any], hash_field: str, label: str
) -> None:
    observed = value.get(hash_field)
    if not isinstance(observed, str) or not observed:
        raise ReviewResolutionError(f"{label} hash is absent")
    body = deepcopy(dict(value))
    body.pop(hash_field, None)
    if canonical_hash(body) != observed:
        raise ReviewResolutionError(f"{label} hash mismatch")


__all__ = [
    "ROUTING_PLAN_SCHEMA_VERSION",
    "ReviewResolutionError",
    "build_review_routing_plan_v1",
    "build_review_routing_plan_from_artifacts_v1",
    "match_route_b_case_v1",
    "open_cross_identity_cases_v1",
    "open_within_identity_cases_v1",
    "resolve_route_b_review_v1",
    "resolve_route_c_review_v1",
    "route_b_destination",
    "route_b_member_set",
    "verify_review_routing_plan_v1",
]
