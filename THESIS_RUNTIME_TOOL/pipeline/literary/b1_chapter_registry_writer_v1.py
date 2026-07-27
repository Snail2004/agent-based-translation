"""Deterministic chapter-registry writer for B1 Scan/Enrich/Local Audit.

The writer applies already-recorded dispositions, mints opaque chapter entity
ids, computes field authority, and seals one immutable chapter artifact.  It
does not reinterpret source language, merge cross-chapter identities, or grant
book-wide alias authority.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.b1_scan_v1 import (
    PRESENCE_BASES,
    REFERENT_KINDS,
    _normalized_surface,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    reopen_admissibility_v1,
)
from pipeline.literary.b2_review_resolution_v1 import (
    verify_review_routing_plan_v1,
)


REGISTRY_SCHEMA_VERSION = "literary_b1_chapter_registry_v1"
PRIOR_CARDS_SCHEMA_VERSION = "literary_b1_prior_cards_v1"
CROSS_CHAPTER_QUEUE_SCHEMA_VERSION = "literary_b1_cross_chapter_hearing_queue_v2"
LEGACY_CROSS_CHAPTER_QUEUE_SCHEMA_VERSION = (
    "literary_b1_cross_chapter_hearing_queue_v1"
)

EFFECTIVE_BASES = frozenset(
    {"explicit_textual", "self_identification", "auditor_confirmed"}
)
NON_EFFECTIVE_STATUSES = frozenset({"disputed", "superseded"})
DECISION_ACTIONS = frozenset(
    {
        "accept_proposal",
        "revise_proposal",
        "reject_proposal",
        "keep_pending",
        "refer_cross_chapter",
    }
)


class B1ChapterRegistryWriterError(ValueError):
    pass


def seal_b1_chapter_registry_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    audit_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a validated chapter cycle and return an immutable sealed registry."""

    chapter_id, block_order, block_ids, source_chapter_hash = _chapter_identity(chapter)
    _verify_input_lineage(
        chapter_id=chapter_id,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
        audit_artifact=audit_artifact,
    )
    decisions = _validated_decisions(audit_artifact, block_ids=block_ids)
    decision_index = {
        _decision_key(
            row["component_kind"], row["subject_ref"], row["original_proposal"]
        ): row
        for row in decisions
    }

    scan_rows = _mapping_by(
        scan_artifact.get("entity_observations"),
        key="observation_id",
        label="B1-Scan entity observation",
    )
    dossiers = _mapping_by(
        enrich_artifact.get("entity_dossiers"),
        key="scan_observation_id",
        label="B1-Enrich entity dossier",
    )
    additional_rows = _mapping_by(
        enrich_artifact.get("additional_entity_dossiers"),
        key="additional_entity_id",
        label="B1-Enrich additional dossier",
    )

    dormant_scan_ids = _accepted_spurious_scan_ids(decisions)
    direct_collision_case_ids = _conflicting_direct_continuity_case_ids(
        enrich_artifact
    )
    local_merge_collision_case_ids = _cross_prior_same_referent_case_ids(
        enrich_artifact,
        decisions=decisions,
    )
    structural_hearing_case_ids = (
        direct_collision_case_ids | local_merge_collision_case_ids
    )
    (
        continued_id_by_scan,
        continued_case_id_by_prior,
    ) = _continued_prior_identity_by_scan(
        enrich_artifact,
        excluded_case_ids=structural_hearing_case_ids,
    )
    card_sources: list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None]] = []
    for scan_id, scan in sorted(scan_rows.items()):
        if scan_id in dormant_scan_ids:
            continue
        ref = f"scan:{scan_id}"
        card_sources.append((ref, scan, dossiers.get(scan_id)))

    for additional_id, dossier in sorted(additional_rows.items()):
        ref = f"additional:{additional_id}"
        decision = _lookup_decision(
            decision_index,
            kind="additional_entity",
            subject_ref=ref,
            proposal=dossier,
        )
        if decision is not None and decision["action"] == "reject_proposal":
            continue
        surface = _required_string(dossier.get("surface"), "additional surface")
        source_blocks = _block_list(
            dossier.get("source_block_ids"), block_ids, "additional source blocks"
        )
        synthetic_scan = {
            "observation_id": additional_id,
            "surface": surface,
            "source_block_ids": source_blocks,
            "referent_kind_claim": dossier.get("referent_kind_claim", "unknown"),
            "record_class": "important_unnamed_referent",
            "presence_basis": "unclear",
            "scan_note": "Recovered by B1-Enrich and retained with audit history.",
            "authority_scope": "chapter_provisional",
        }
        card_sources.append((ref, synthetic_scan, dossier))

    source_by_ref = {source_ref: (scan, dossier) for source_ref, scan, dossier in card_sources}
    groups, merge_decisions = _within_chapter_same_referent_groups(
        source_refs=list(source_by_ref),
        decisions=decisions,
        continued_prior_id_by_scan=continued_id_by_scan,
    )
    entity_id_by_ref: dict[str, str] = {}
    group_plans: list[tuple[list[str], str]] = []
    for group in groups:
        representative_ref = _representative_source_ref(
            group,
            source_by_ref=source_by_ref,
            block_ids=block_ids,
            block_order=block_order,
        )
        continued_ids = {
            continued_id_by_scan[source_ref.removeprefix("scan:")]
            for source_ref in group
            if source_ref.startswith("scan:")
            and source_ref.removeprefix("scan:") in continued_id_by_scan
        }
        if len(continued_ids) > 1:
            raise B1ChapterRegistryWriterError(
                "chapter-local merge would collapse distinct prior entity ids"
            )
        representative_scan, _representative_dossier = source_by_ref[representative_ref]
        group_entity_id = next(iter(continued_ids), None) or _mint_entity_id(
            chapter_id=chapter_id,
            source_ref=representative_ref,
            surface=_required_string(
                representative_scan.get("surface"), "representative surface"
            ),
            source_block_ids=_block_list(
                representative_scan.get("source_block_ids"),
                block_ids,
                "representative source blocks",
            ),
        )
        for source_ref in group:
            entity_id_by_ref[source_ref] = group_entity_id
        group_plans.append((group, representative_ref))

    if len(groups) != len(set(entity_id_by_ref.values())):
        raise B1ChapterRegistryWriterError("entity ids collide across merge groups")

    # Resolve every chapter-local reference only after the complete ID map exists.
    cards: list[dict[str, Any]] = []
    within_chapter_merges: list[dict[str, Any]] = []
    for group, representative_ref in group_plans:
        member_cards: dict[str, dict[str, Any]] = {}
        for source_ref in group:
            scan, dossier = source_by_ref[source_ref]
            member_cards[source_ref] = _build_card(
                chapter_id=chapter_id,
                source_ref=source_ref,
                entity_id=entity_id_by_ref[source_ref],
                scan=scan,
                dossier=dossier,
                decision_index=decision_index,
                entity_id_by_ref=entity_id_by_ref,
                block_ids=block_ids,
                block_order=block_order,
            )
        group_decisions = [
            row
            for row in merge_decisions
            if row["subject_ref"] in group
            and row["original_proposal"].get("target_ref") in group
        ]
        source_component_ids = [
            _required_string(row.get("component_id"), "merge component id")
            for row in group_decisions
        ]
        continued_refs_by_prior: dict[str, list[str]] = {}
        for source_ref in group:
            if not source_ref.startswith("scan:"):
                continue
            prior_id = continued_id_by_scan.get(source_ref.removeprefix("scan:"))
            if prior_id is not None:
                continued_refs_by_prior.setdefault(prior_id, []).append(source_ref)
        source_component_ids.extend(
            continued_case_id_by_prior[prior_id]
            for prior_id, continued_refs in continued_refs_by_prior.items()
            if len(continued_refs) > 1
        )
        card = _merge_within_chapter_cards(
            representative_ref=representative_ref,
            member_cards=member_cards,
            source_by_ref=source_by_ref,
            source_component_ids=source_component_ids,
            block_order=block_order,
        )
        cards.append(card)
        if len(group) > 1:
            within_chapter_merges.append(
                deepcopy(card["within_chapter_identity_merge"])
            )
    carried_cards = _build_referenced_prior_cards(
        chapter_id=chapter_id,
        enrich_artifact=enrich_artifact,
        block_ids=block_ids,
        block_order=block_order,
    )
    active_ids = {row["entity_id"] for row in cards}
    carried_ids = {row["entity_id"] for row in carried_cards}
    if active_ids & carried_ids or len(carried_ids) != len(carried_cards):
        raise B1ChapterRegistryWriterError(
            "referenced prior carry collides with a current chapter card"
        )
    cards.extend(carried_cards)
    cards.sort(key=lambda row: (row["first_seen"]["order_index"], row["entity_id"]))
    within_chapter_merges = [
        deepcopy(card["within_chapter_identity_merge"])
        for card in cards
        if card.get("within_chapter_identity_merge") is not None
    ]

    relation_edges, relation_projection_issues = _build_relation_edges(
        decisions=decisions,
        entity_id_by_ref=entity_id_by_ref,
        chapter_id=chapter_id,
    )
    relation_edges, structural_relation_issues = (
        _mark_structurally_impossible_kinship_v1(relation_edges)
    )
    relation_projection_issues.extend(structural_relation_issues)
    glossary_entries = _build_glossary_entries(
        enrich_artifact=enrich_artifact,
        decisions=decisions,
        block_ids=block_ids,
        chapter_id=chapter_id,
    )
    dormant_observations = _build_dormant_observations(
        decisions=decisions,
        scan_rows=scan_rows,
    )
    pending_reviews = [
        deepcopy(row)
        for row in decisions
        if row["action"] in {"keep_pending", "refer_cross_chapter"}
    ]
    pending_reviews.extend(deepcopy(audit_artifact.get("quarantined_rows") or []))
    pending_reviews.extend(
        deepcopy(
            audit_artifact.get("deferred_enrich_rows", {}).get("quarantined_tasks")
            or []
        )
    )
    pending_reviews.extend(
        {
            "row_type": "cross_chapter_identity_linkage",
            "state": "pending_no_authority",
            "continuity_case_id": _required_string(
                row.get("continuity_case_id"), "continuity_case_id"
            ),
            "prior_card_id": _required_string(
                row.get("prior_card_id"), "prior_card_id"
            ),
            "current_scan_observation_ids": _string_values(
                row.get("current_scan_observation_ids"),
                "current_scan_observation_ids",
                allow_empty=True,
            ),
            "source_block_ids": _string_values(
                row.get("source_block_ids"), "source_block_ids"
            ),
            "reason": _required_string(row.get("reason"), "continuity reason"),
        }
        for row in _sequence_of_mappings(
            enrich_artifact.get("continuity_cases") or [], "continuity cases"
        )
        if row.get("hearing_required") is True
        or _required_string(
            row.get("continuity_case_id"), "continuity_case_id"
        )
        in structural_hearing_case_ids
    )
    pending_reviews.extend(
        {
            "row_type": "cross_chapter_stable_claim",
            "state": "pending_no_authority",
            **deepcopy(dict(row)),
        }
        for row in _sequence_of_mappings(
            enrich_artifact.get("conflict_findings"), "conflict findings"
        )
        if isinstance(row.get("prior_card_id"), str) and row.get("prior_card_id")
    )
    pending_reviews.extend(deepcopy(relation_projection_issues))
    diagnostics = deepcopy(
        audit_artifact.get("deferred_enrich_rows", {}).get("review_issues") or []
    )
    diagnostics.extend(deepcopy(relation_projection_issues))

    prior_cards = _project_prior_cards(cards)
    lineage = {
        "source_chapter_hash": source_chapter_hash,
        "scan_artifact_hash": scan_artifact["artifact_hash"],
        "enrich_artifact_hash": enrich_artifact["artifact_hash"],
        "local_audit_artifact_hash": audit_artifact["artifact_hash"],
    }
    body = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "lineage": lineage,
        "cards": cards,
        "relation_edges": relation_edges,
        "glossary_entries": glossary_entries,
        "prior_cards_projection": {
            "schema_version": PRIOR_CARDS_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "cards": prior_cards,
        },
        "dormant_observations": dormant_observations,
        "pending_reviews": pending_reviews,
        "diagnostics": diagnostics,
        "curation_log": {
            "decisions": deepcopy(decisions),
            "mechanical_noops": deepcopy(audit_artifact.get("mechanical_noops") or []),
            "rejected_components": deepcopy(audit_artifact.get("rejected_components") or []),
        },
        "within_chapter_identity_merges": within_chapter_merges,
        "id_alias_table": [],
        "chapter_authority_granted": True,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
        "metrics": {
            "active_card_count": len(cards),
            "effective_claim_count": sum(
                1 for card in cards for claim in card["claims"] if claim["effective"]
            ),
            "provisional_claim_count": sum(
                1 for card in cards for claim in card["claims"] if not claim["effective"]
            ),
            "relation_edge_count": len(relation_edges),
            "structural_contradiction_count": len(structural_relation_issues),
            "glossary_entry_count": len(glossary_entries),
            "dormant_observation_count": len(dormant_observations),
            "pending_review_count": len(pending_reviews),
            "diagnostic_count": len(diagnostics),
            "within_chapter_merge_count": len(within_chapter_merges),
            "referenced_prior_carry_count": len(carried_cards),
            "absorbed_card_count": sum(
                len(row["member_source_refs"]) - 1
                for row in within_chapter_merges
            ),
        },
    }
    sealed = {**body, "registry_hash": canonical_hash(body)}
    verify_b1_chapter_registry_v1(sealed)
    return sealed


def build_b1_cross_chapter_hearing_queue_v1(
    *,
    chapter_registry: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    audit_artifact: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None = None,
    reconciled_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize bounded hearings; never adjudicate them in code.

    ``prior_cards`` are the same cards B1-Scan was given.  They are what lets a
    roster recognition proposal become a real hearing: the proposal itself only
    names a prior card id, and a hearing that shows one side of the comparison
    is not a hearing.  Supplying them is optional so an existing caller keeps
    working; when they are absent, roster proposals are counted as unqueued
    rather than silently discarded.
    """

    verify_b1_chapter_registry_v1(chapter_registry)
    chapter_id = _required_string(chapter_registry.get("chapter_id"), "chapter_id")
    _verify_input_lineage(
        chapter_id=chapter_id,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
        audit_artifact=audit_artifact,
    )
    card_by_source_ref = {
        source_ref: deepcopy(dict(card))
        for card in _sequence_of_mappings(chapter_registry.get("cards"), "cards")
        for source_ref in card.get("source_refs") or []
        if isinstance(source_ref, str)
    }
    dossier_by_scan = {
        _required_string(row.get("scan_observation_id"), "scan_observation_id"): row
        for row in _sequence_of_mappings(
            enrich_artifact.get("entity_dossiers"), "entity dossiers"
        )
    }
    dossier_by_source_ref = {
        **{
            f"scan:{scan_id}": row
            for scan_id, row in dossier_by_scan.items()
        },
        **{
            "additional:"
            + _required_string(row.get("additional_entity_id"), "additional_entity_id"): row
            for row in _sequence_of_mappings(
                enrich_artifact.get("additional_entity_dossiers"),
                "additional entity dossiers",
            )
        },
    }
    components: list[dict[str, Any]] = []

    prior_card_by_id = {
        _required_string(row.get("prior_card_id"), "prior_card_id"): deepcopy(dict(row))
        for row in (prior_cards or [])
        if isinstance(row, Mapping)
    }
    roster_by_prior_card, unqueued_roster = _roster_proposals_by_prior_card(
        scan_artifact=scan_artifact,
        prior_card_by_id=prior_card_by_id,
        card_by_source_ref=card_by_source_ref,
        dossier_by_scan=dossier_by_scan,
    )

    continuity_cases = _sequence_of_mappings(
        enrich_artifact.get("continuity_cases") or [], "continuity cases"
    )
    direct_collision_case_ids = _conflicting_direct_continuity_case_ids(
        enrich_artifact
    )
    local_merge_collision_case_ids = _cross_prior_same_referent_case_ids(
        enrich_artifact,
        decisions=_sequence_of_mappings(
            audit_artifact.get("decisions"), "audit decisions"
        ),
    )
    structural_hearing_case_ids = (
        direct_collision_case_ids | local_merge_collision_case_ids
    )
    continuity_by_id = {
        _required_string(row.get("continuity_case_id"), "continuity_case_id"): row
        for row in continuity_cases
    }
    for raw in continuity_cases:
        case_id = _required_string(raw.get("continuity_case_id"), "continuity_case_id")
        structural_collision = case_id in structural_hearing_case_ids
        if raw.get("hearing_required") is not True and not structural_collision:
            continue
        prior_card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        attached = roster_by_prior_card.pop(prior_card_id, None)
        observation_ids = sorted(
            {
                *_string_values(
                    raw.get("current_scan_observation_ids"),
                    "current_scan_observation_ids",
                    allow_empty=True,
                ),
                *(
                    row["scan_observation_id"]
                    for row in attached or []
                    if row.get("scan_observation_id")
                ),
            }
        )
        current_cards = [
            card_by_source_ref[f"scan:{scan_id}"]
            for scan_id in observation_ids
            if f"scan:{scan_id}" in card_by_source_ref
        ]
        current_dossiers = [
            deepcopy(dict(dossier_by_scan[scan_id]))
            for scan_id in observation_ids
            if scan_id in dossier_by_scan
        ]
        ready = (
            bool(observation_ids)
            and len(current_cards) == len(observation_ids)
            and len(current_dossiers) == len(observation_ids)
        )
        current_source_ids = sorted(
            {
                *_string_values(raw.get("source_block_ids"), "source_block_ids"),
                *(
                    block_id
                    for dossier in current_dossiers
                    for block_id in _dossier_evidence_block_ids(dossier)
                ),
                *(
                    block_id
                    for card in current_cards
                    for block_id in card.get("support_block_ids") or []
                    if isinstance(block_id, str) and block_id
                ),
            }
        )
        body = {
            "question_type": "identity_linkage",
            "review_route": "identity_auditor",
            "continuity_case_id": case_id,
            "prior_card_id": prior_card_id,
            "current_scan_observation_ids": observation_ids,
            "current_entity_ids": sorted(
                {_required_string(row.get("entity_id"), "entity_id") for row in current_cards}
            ),
            "prior_card_snapshot": deepcopy(dict(_mapping(raw.get("prior_card_snapshot"), "prior card snapshot"))),
            "current_card_snapshots": current_cards,
            "current_dossier_snapshots": current_dossiers,
            "source_block_ids": current_source_ids,
            "trigger": {
                "scan_verdict": _required_string(raw.get("scan_verdict"), "scan_verdict"),
                "reason_code": _required_string(raw.get("reason_code"), "reason_code"),
                "reason": _required_string(raw.get("reason"), "reason"),
                "mechanical_risk_codes": sorted(
                    {
                        *_string_values(
                            raw.get("mechanical_risk_codes"),
                            "mechanical_risk_codes",
                            allow_empty=True,
                        ),
                        *(
                            ["multiple_direct_continuations_for_one_observation"]
                            if case_id in direct_collision_case_ids
                            else []
                        ),
                        *(
                            ["local_same_referent_spans_distinct_prior_entities"]
                            if case_id in local_merge_collision_case_ids
                            else []
                        ),
                    }
                ),
            },
            "evidence_manifest_hash": _required_string(
                raw.get("evidence_manifest_hash"), "evidence_manifest_hash"
            ),
            "lifecycle_state": "ready_for_hearing" if ready else "waiting_for_enrichment",
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        # One card, one hearing.  A roster proposal naming this same prior card
        # joins the case already open on it instead of starting a rival one:
        # separate hearings on one card can return contradictory answers, and
        # neither would know the other existed.
        if attached:
            body["roster_recognition_proposals"] = attached
            body["source_block_ids"] = sorted(
                {
                    *body["source_block_ids"],
                    *(
                        block_id
                        for row in attached
                        for block_id in row.get("source_block_ids") or []
                    ),
                }
            )
        components.append(
            {
                "component_id": "b1xhear_" + canonical_hash(body)[:20],
                **body,
            }
        )

    # Proposals about a card with no continuity case of its own open their own
    # hearing, carrying both sides: the prior dossier with its chapter-one
    # provenance, and whatever the current chapter built for that surface.
    for prior_card_id in sorted(roster_by_prior_card):
        rows = roster_by_prior_card[prior_card_id]
        prior_snapshot = prior_card_by_id[prior_card_id]
        current_dossiers = [
            deepcopy(dict(row["current_dossier_snapshot"]))
            for row in rows
            if row.get("current_dossier_snapshot")
        ]
        current_cards = [
            deepcopy(dict(row["current_card_snapshot"]))
            for row in rows
            if row.get("current_card_snapshot")
        ]
        body = {
            "question_type": "roster_recognition",
            "review_route": "identity_auditor",
            "prior_card_id": prior_card_id,
            "current_scan_observation_ids": sorted(
                {
                    row["scan_observation_id"]
                    for row in rows
                    if row.get("scan_observation_id")
                }
            ),
            "current_entity_ids": sorted(
                {
                    _required_string(row.get("entity_id"), "entity_id")
                    for row in current_cards
                }
            ),
            "prior_card_snapshot": prior_snapshot,
            "current_card_snapshots": current_cards,
            "current_dossier_snapshots": current_dossiers,
            "roster_recognition_proposals": rows,
            "source_block_ids": sorted(
                {
                    *(
                        block_id
                        for row in rows
                        for block_id in row.get("source_block_ids") or []
                    ),
                    *(
                        ref["block_id"]
                        for ref in prior_snapshot.get("provenance_refs") or []
                        if isinstance(ref, Mapping) and isinstance(ref.get("block_id"), str)
                    ),
                }
            ),
            "lifecycle_state": "ready_for_hearing",
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        components.append(
            {
                "component_id": "b1xhear_" + canonical_hash(body)[:20],
                **body,
            }
        )

    for raw in _sequence_of_mappings(
        enrich_artifact.get("conflict_findings"), "conflict findings"
    ):
        prior_card_id = raw.get("prior_card_id")
        if not isinstance(prior_card_id, str) or not prior_card_id:
            continue
        scan_id = _required_string(raw.get("scan_observation_id"), "scan_observation_id")
        current_card = card_by_source_ref.get(f"scan:{scan_id}")
        current_dossier = dossier_by_scan.get(scan_id)
        case_ids = sorted(
            _string_values(
                raw.get("continuity_case_ids"),
                "continuity_case_ids",
                allow_empty=True,
            )
        )
        matching_cases = [
            continuity_by_id[case_id]
            for case_id in case_ids
            if case_id in continuity_by_id
            and continuity_by_id[case_id].get("prior_card_id") == prior_card_id
        ]
        prior_snapshot = (
            deepcopy(dict(matching_cases[0]["prior_card_snapshot"]))
            if len(matching_cases) == 1
            and isinstance(matching_cases[0].get("prior_card_snapshot"), Mapping)
            else None
        )
        body = {
            "question_type": "stable_claim",
            "review_route": "stable_claim_auditor",
            "continuity_case_ids": case_ids,
            "prior_card_id": prior_card_id,
            "current_scan_observation_id": scan_id,
            "current_entity_id": (
                current_card.get("entity_id") if current_card is not None else None
            ),
            "prior_card_snapshot": prior_snapshot,
            "current_card_snapshot": (
                deepcopy(dict(current_card)) if current_card is not None else None
            ),
            "current_dossier_snapshot": (
                deepcopy(dict(current_dossier))
                if current_dossier is not None
                else None
            ),
            "field": _required_string(raw.get("field"), "conflict field"),
            "existing_value": _required_string(
                raw.get("existing_value"), "existing_value"
            ),
            "observed_value": _required_string(
                raw.get("observed_value"), "observed_value"
            ),
            "source_block_ids": _string_values(
                raw.get("source_block_ids"), "source_block_ids"
            ),
            "reason": _required_string(raw.get("reason"), "conflict reason"),
            "lifecycle_state": (
                "ready_for_hearing"
                if current_card is not None
                and current_dossier is not None
                and prior_snapshot is not None
                else "waiting_for_enrichment"
            ),
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        components.append(
            {
                "component_id": "b1xhear_" + canonical_hash(body)[:20],
                **body,
            }
        )

    local_routes = {
        "additional_entity": "identity_auditor",
        "alias_proposal": "identity_auditor",
        "spurious_challenge": "identity_auditor",
        "entity_link": "temporal_auditor",
        "kinship_link": "temporal_auditor",
        "glossary_ambiguity": "glossary_auditor",
    }
    for referral in _sequence_of_mappings(
        audit_artifact.get("cross_chapter_referrals"), "cross chapter referrals"
    ):
        kind = _required_string(referral.get("component_kind"), "component kind")
        if kind == "stable_claim_conflict":
            continue
        subject_ref = _required_string(referral.get("subject_ref"), "subject ref")
        current_card = card_by_source_ref.get(subject_ref)
        current_dossier = dossier_by_source_ref.get(subject_ref)
        original = _mapping(referral.get("original_proposal"), "original proposal")
        prior_card_id = original.get("prior_card_id")
        if not isinstance(prior_card_id, str) or not prior_card_id:
            prior_card_id = None
            if isinstance(current_dossier, Mapping):
                continuity = current_dossier.get("continuity")
                if isinstance(continuity, Mapping):
                    candidate = continuity.get("continued_prior_card_id")
                    if isinstance(candidate, str) and candidate:
                        prior_card_id = candidate
        route = local_routes.get(kind, "pending_unassigned")
        ready = current_card is not None and route != "pending_unassigned"
        body = {
            "question_type": "local_cross_chapter_referral",
            "review_route": route,
            "local_component_id": _required_string(
                referral.get("component_id"), "component_id"
            ),
            "component_kind": kind,
            "subject_ref": subject_ref,
            "prior_card_id": prior_card_id,
            "prior_card_snapshot": (
                deepcopy(dict(prior_card_by_id[prior_card_id]))
                if isinstance(prior_card_id, str)
                and prior_card_id in prior_card_by_id
                else None
            ),
            "current_entity_id": (
                current_card.get("entity_id") if current_card is not None else None
            ),
            "current_card_snapshot": (
                deepcopy(dict(current_card)) if current_card is not None else None
            ),
            "current_dossier_snapshot": (
                deepcopy(dict(current_dossier))
                if current_dossier is not None
                else None
            ),
            "original_proposal": deepcopy(dict(original)),
            "source_block_ids": _string_values(
                referral.get("source_block_ids"), "source_block_ids"
            ),
            "resolution_note": _required_string(
                referral.get("resolution_note"), "resolution_note"
            ),
            "lifecycle_state": "ready_for_hearing" if ready else "pending_route",
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        components.append(
            {
                "component_id": "b1xhear_" + canonical_hash(body)[:20],
                **body,
            }
        )

    components, cluster_count = _cluster_identity_candidate_hearings_v2(components)
    components, suppressed_reopen_cases = _apply_reopen_gate_v1(
        components=components,
        reconciled_projection=reconciled_projection,
    )
    components.sort(key=lambda row: row["component_id"])
    roster_surfaces = sorted(
        {
            surface
            for card in prior_card_by_id.values()
            for surface in card.get("stable_surfaces") or []
            if isinstance(surface, str) and surface.strip()
        },
        key=lambda value: (_normalized_surface(value), value),
    )
    body = {
        "schema_version": CROSS_CHAPTER_QUEUE_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "registry_hash": chapter_registry["registry_hash"],
        "scan_artifact_hash": scan_artifact["artifact_hash"],
        "enrich_artifact_hash": enrich_artifact["artifact_hash"],
        "local_audit_artifact_hash": audit_artifact["artifact_hash"],
        "reconciled_projection_hash": (
            reconciled_projection.get("projection_hash")
            if isinstance(reconciled_projection, Mapping)
            else None
        ),
        "registry_roster_surfaces": roster_surfaces,
        "components": components,
        "suppressed_reopen_cases": suppressed_reopen_cases,
        "metrics": {
            "component_count": len(components),
            "ready_for_hearing_count": sum(
                1
                for row in components
                if row["lifecycle_state"] == "ready_for_hearing"
            ),
            "waiting_count": sum(
                1
                for row in components
                if row["lifecycle_state"] != "ready_for_hearing"
            ),
            "counts_by_route": {
                route: sum(1 for row in components if row["review_route"] == route)
                for route in sorted({row["review_route"] for row in components})
            },
            "roster_proposal_component_count": sum(
                1 for row in components if row["question_type"] == "roster_recognition"
            ),
            "roster_proposals_attached_to_existing_case": sum(
                len(row.get("roster_recognition_proposals") or [])
                for row in components
                if row["question_type"] == "identity_linkage"
            ),
            "roster_proposals_unqueued_count": len(unqueued_roster),
            "identity_candidate_cluster_count": cluster_count,
            "suppressed_reopen_case_count": len(suppressed_reopen_cases),
        },
        "unqueued_roster_proposals": unqueued_roster,
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
    }
    sealed = {**body, "queue_hash": canonical_hash(body)}
    verify_b1_cross_chapter_hearing_queue_v1(sealed)
    return sealed


def bind_b2_review_routing_to_hearing_queue_v1(
    *,
    hearing_queue: Mapping[str, Any],
    routing_plan: Mapping[str, Any],
    chapter_registry: Mapping[str, Any],
    b2_artifact: Mapping[str, Any],
    candidate_scope_cards: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Bind route-B results without reinterpreting language or granting authority."""

    verify_b1_cross_chapter_hearing_queue_v1(hearing_queue)
    verify_b1_chapter_registry_v1(chapter_registry)
    verify_review_routing_plan_v1(routing_plan)
    chapter_id = _required_string(hearing_queue.get("chapter_id"), "chapter_id")
    if (
        routing_plan.get("chapter_id") != chapter_id
        or chapter_registry.get("chapter_id") != chapter_id
        or b2_artifact.get("chapter_id") != chapter_id
        or routing_plan.get("source_hearing_queue_hash")
        != hearing_queue.get("queue_hash")
        or routing_plan.get("source_registry_hash")
        != chapter_registry.get("registry_hash")
        or routing_plan.get("source_b2_artifact_hash")
        != b2_artifact.get("artifact_hash")
    ):
        raise B1ChapterRegistryWriterError(
            "B2 review-routing lineage differs from hearing inputs"
        )

    review_by_id: dict[str, dict[str, Any]] = {}
    for raw in _sequence_of_mappings(
        b2_artifact.get("review_requests"), "B2 review requests"
    ):
        review = deepcopy(dict(raw))
        review_id = _required_string(review.get("review_id"), "review_id")
        if review_id in review_by_id:
            raise B1ChapterRegistryWriterError("B2 artifact repeats a review")
        review_by_id[review_id] = review
    if set(review_by_id) != set(routing_plan.get("review_ids") or []):
        raise B1ChapterRegistryWriterError(
            "B2 routing plan does not exact-cover its source artifact"
        )

    card_by_id = {
        _required_string(card.get("entity_id"), "entity_id"): deepcopy(dict(card))
        for card in _sequence_of_mappings(
            chapter_registry.get("cards"), "registry cards"
        )
    }
    for raw in candidate_scope_cards:
        if not isinstance(raw, Mapping):
            raise B1ChapterRegistryWriterError(
                "B2 candidate-scope card is malformed"
            )
        card = deepcopy(dict(raw))
        card_id = _required_string(
            card.get("effective_entity_id") or card.get("entity_id"),
            "B2 candidate-scope entity_id",
        )
        first_seen = card.get("first_seen")
        if not isinstance(first_seen, Mapping):
            raise B1ChapterRegistryWriterError(
                f"B2 candidate-scope card has no first_seen: {card_id}"
            )
        _required_string(
            first_seen.get("chapter_id"),
            "B2 candidate-scope first_seen chapter_id",
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
                raise B1ChapterRegistryWriterError(
                    "B2 candidate-scope card has malformed provenance_refs: "
                    f"{card_id}"
                )
        elif not isinstance(source_refs, list) or not all(
            isinstance(value, str) and value.strip() for value in source_refs
        ):
            raise B1ChapterRegistryWriterError(
                f"B2 candidate-scope card has malformed source_refs: {card_id}"
            )
        if card_id in card_by_id:
            continue
        card["entity_id"] = card_id
        card_by_id[card_id] = card
    components = {
        _required_string(row.get("component_id"), "component_id"): deepcopy(dict(row))
        for row in _sequence_of_mappings(
            hearing_queue.get("components"), "hearing components"
        )
    }
    case_aliases = {component_id: component_id for component_id in components}
    bindings: list[dict[str, Any]] = []
    within_case_requests: list[dict[str, Any]] = []

    for raw_route in routing_plan.get("route_b") or []:
        if not isinstance(raw_route, Mapping):
            raise B1ChapterRegistryWriterError("route-B result is malformed")
        route = deepcopy(dict(raw_route))
        review_id = _required_string(route.get("review_id"), "route-B review_id")
        review = review_by_id[review_id]
        destination = _required_string(route.get("destination"), "route-B destination")
        action = _required_string(route.get("action"), "route-B action")
        if destination == "WITHIN":
            if action == "attach_existing_case":
                bindings.append(
                    {
                        "review_id": review_id,
                        "destination": "WITHIN",
                        "action": action,
                        "case_id": _required_string(
                            route.get("case_id"), "within case_id"
                        ),
                        "member_refs": deepcopy(route.get("member_refs")),
                    }
                )
                continue
            if action == "open_new_case":
                body = {
                    "review_id": review_id,
                    "component_kind": "same_referent_proposal",
                    "member_refs": deepcopy(route.get("member_refs")),
                    "source_block_ids": _string_values(
                        review.get("source_block_ids"),
                        "route-B source_block_ids",
                    ),
                    "identity_authority_granted": False,
                    "book_authority_granted": False,
                }
                within_case_requests.append(
                    {
                        "request_id": "b1lacreq_"
                        + canonical_hash(body)[:20],
                        **body,
                    }
                )
                bindings.append(
                    {
                        "review_id": review_id,
                        "destination": "WITHIN",
                        "action": action,
                        "case_id": None,
                        "member_refs": deepcopy(route.get("member_refs")),
                    }
                )
                continue
            if action == "hold_partial_overlap":
                if route.get("identity_authority_granted") is not False:
                    raise B1ChapterRegistryWriterError(
                        "partial-overlap hold claims identity authority"
                    )
                overlapping_case_id = _required_string(
                    route.get("case_id"), "overlapping within case_id"
                )
                proposed_members = _string_values(
                    route.get("member_refs"), "partial-overlap proposed member_refs"
                )
                existing_members = _string_values(
                    route.get("matched_case_member_refs"),
                    "partial-overlap existing member_refs",
                )
                if (
                    not set(proposed_members) & set(existing_members)
                    or set(proposed_members) <= set(existing_members)
                ):
                    raise B1ChapterRegistryWriterError(
                        "partial-overlap hold does not add unresolved members"
                    )
                body = {
                    "review_id": review_id,
                    "component_kind": "same_referent_partial_overlap_hold",
                    "overlapping_case_id": overlapping_case_id,
                    "proposed_member_refs": proposed_members,
                    "existing_member_refs": existing_members,
                    "source_block_ids": _string_values(
                        review.get("source_block_ids"),
                        "route-B source_block_ids",
                    ),
                    "lifecycle_state": "parked_pending_identity_hearing",
                    "identity_authority_granted": False,
                    "book_authority_granted": False,
                }
                within_case_requests.append(
                    {
                        "request_id": "b1lacreq_"
                        + canonical_hash(body)[:20],
                        **body,
                    }
                )
                bindings.append(
                    {
                        "review_id": review_id,
                        "destination": "WITHIN",
                        "action": action,
                        "case_id": overlapping_case_id,
                        "member_refs": proposed_members,
                        "matched_case_member_refs": existing_members,
                    }
                )
                continue
            raise B1ChapterRegistryWriterError("unknown WITHIN route-B action")

        if destination != "CROSS":
            raise B1ChapterRegistryWriterError("unknown route-B destination")
        if action == "hold_partial_overlap":
            if route.get("identity_authority_granted") is not False:
                raise B1ChapterRegistryWriterError(
                    "partial-overlap hold claims identity authority"
                )
            overlapping_case_id = _required_string(
                route.get("case_id"), "overlapping cross case_id"
            )
            proposed_members = _string_values(
                route.get("member_refs"), "partial-overlap proposed member_refs"
            )
            existing_members = _string_values(
                route.get("matched_case_member_refs"),
                "partial-overlap existing member_refs",
            )
            if (
                not set(proposed_members) & set(existing_members)
                or set(proposed_members) <= set(existing_members)
            ):
                raise B1ChapterRegistryWriterError(
                    "partial-overlap hold does not add unresolved members"
                )
            bindings.append(
                {
                    "review_id": review_id,
                    "destination": "CROSS",
                    "action": action,
                    "case_id": overlapping_case_id,
                    "member_refs": proposed_members,
                    "matched_case_member_refs": existing_members,
                    "identity_authority_granted": False,
                    "review_request": review,
                }
            )
            continue
        if action == "attached_case_superseded":
            bindings.append(
                {
                    "review_id": review_id,
                    "destination": "CROSS",
                    "action": action,
                    "case_id": _required_string(
                        route.get("case_id"), "superseded cross case_id"
                    ),
                    "member_refs": deepcopy(route.get("member_refs")),
                    "matched_case_member_refs": deepcopy(
                        route.get("matched_case_member_refs")
                    ),
                    "review_request": review,
                }
            )
            continue
        if action == "attach_existing_case":
            base_case_id = _required_string(route.get("case_id"), "cross case_id")
            current_case_id = case_aliases.get(base_case_id)
            component = components.pop(current_case_id, None)
            if component is None:
                raise B1ChapterRegistryWriterError(
                    "route-B attachment cites a foreign cross case"
                )
            attached = deepcopy(component.get("attached_b2_review_requests") or [])
            attached.append(review)
            attached.sort(key=lambda row: str(row.get("review_id")))
            source_ids = sorted(
                {
                    *(
                        block_id
                        for block_id in component.get("source_block_ids") or []
                        if isinstance(block_id, str) and block_id
                    ),
                    *_string_values(
                        review.get("source_block_ids"),
                        "route-B source_block_ids",
                    ),
                }
            )
            component_body = {
                key: deepcopy(value)
                for key, value in component.items()
                if key != "component_id"
            }
            component_body["attached_b2_review_requests"] = attached
            component_body["source_block_ids"] = source_ids
            component_body["supersedes_component_id"] = current_case_id
            new_case_id = "b1xhear_" + canonical_hash(component_body)[:20]
            components[new_case_id] = {
                "component_id": new_case_id,
                **component_body,
            }
            case_aliases[base_case_id] = new_case_id
            bindings.append(
                {
                    "review_id": review_id,
                    "destination": "CROSS",
                    "action": action,
                    "case_id": base_case_id,
                    "effective_case_id": new_case_id,
                    "member_refs": deepcopy(route.get("member_refs")),
                }
            )
            continue
        if action != "open_new_case":
            raise B1ChapterRegistryWriterError("unknown CROSS route-B action")
        competing_ids = _string_values(
            route.get("competing_card_ids"), "competing_card_ids"
        )
        try:
            competing_cards = [card_by_id[card_id] for card_id in competing_ids]
        except KeyError as exc:
            raise B1ChapterRegistryWriterError(
                "route-B hearing cites a foreign registry card"
            ) from exc
        prior_cards = [
            card
            for card in competing_cards
            if card.get("first_seen", {}).get("chapter_id") != chapter_id
        ]
        current_cards = [
            card
            for card in competing_cards
            if card.get("first_seen", {}).get("chapter_id") == chapter_id
        ]
        if not prior_cards:
            raise B1ChapterRegistryWriterError(
                "CROSS route-B hearing has no prior-chapter card"
            )
        body = {
            "question_type": "identity_linkage",
            "review_route": "identity_auditor",
            "prior_card_ids": sorted(card["entity_id"] for card in prior_cards),
            "prior_candidate_snapshots": prior_cards,
            "current_entity_ids": sorted(
                card["entity_id"] for card in current_cards
            ),
            "current_card_snapshots": current_cards,
            "current_dossier_snapshots": [],
            "source_block_ids": _string_values(
                review.get("source_block_ids"), "route-B source_block_ids"
            ),
            "attached_b2_review_requests": [review],
            "lifecycle_state": "ready_for_hearing",
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        case_id = "b1xhear_" + canonical_hash(body)[:20]
        components[case_id] = {"component_id": case_id, **body}
        bindings.append(
            {
                "review_id": review_id,
                "destination": "CROSS",
                "action": action,
                "case_id": case_id,
                "effective_case_id": case_id,
                "member_refs": deepcopy(route.get("member_refs")),
            }
        )

    body = {
        key: deepcopy(value)
        for key, value in hearing_queue.items()
        if key != "queue_hash"
    }
    ordered_components = sorted(
        components.values(), key=lambda row: row["component_id"]
    )
    body["components"] = ordered_components
    body["b2_review_routing_plan_hash"] = routing_plan["routing_plan_hash"]
    body["b2_review_bindings"] = sorted(
        bindings, key=lambda row: row["review_id"]
    )
    body["b2_within_case_requests"] = sorted(
        within_case_requests, key=lambda row: row["request_id"]
    )
    metrics = deepcopy(dict(body.get("metrics") or {}))
    metrics["component_count"] = len(ordered_components)
    metrics["ready_for_hearing_count"] = sum(
        row.get("lifecycle_state") == "ready_for_hearing"
        for row in ordered_components
    )
    metrics["waiting_count"] = sum(
        row.get("lifecycle_state") != "ready_for_hearing"
        for row in ordered_components
    )
    metrics["counts_by_route"] = {
        route: sum(
            row.get("review_route") == route for row in ordered_components
        )
        for route in sorted(
            {
                str(row.get("review_route"))
                for row in ordered_components
                if row.get("review_route")
            }
        )
    }
    metrics["b2_review_binding_count"] = len(bindings)
    metrics["b2_within_case_request_count"] = len(within_case_requests)
    body["metrics"] = metrics
    sealed = {**body, "queue_hash": canonical_hash(body)}
    verify_b1_cross_chapter_hearing_queue_v1(sealed)
    return sealed


def _cluster_identity_candidate_hearings_v2(
    components: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse pairwise identity questions into one current-referent hearing."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    untouched: list[dict[str, Any]] = []
    for raw in components:
        row = deepcopy(dict(raw))
        if (
            row.get("review_route") != "identity_auditor"
            or row.get("question_type") not in {"identity_linkage", "roster_recognition"}
        ):
            untouched.append(row)
            continue
        current_ids = _component_current_entity_ids(row)
        prior_id = row.get("prior_card_id")
        prior_snapshot = row.get("prior_card_snapshot")
        if (
            len(current_ids) != 1
            or not isinstance(prior_id, str)
            or not prior_id
            or not isinstance(prior_snapshot, Mapping)
        ):
            untouched.append(row)
            continue
        grouped.setdefault(current_ids[0], []).append(row)

    clustered: list[dict[str, Any]] = []
    for current_entity_id in sorted(grouped):
        rows = sorted(grouped[current_entity_id], key=lambda row: row["component_id"])
        candidates: dict[str, dict[str, Any]] = {}
        contexts: list[dict[str, Any]] = []
        current_cards: dict[str, dict[str, Any]] = {}
        current_dossiers: dict[str, dict[str, Any]] = {}
        source_block_ids: set[str] = set()
        observation_ids: set[str] = set()
        continuity_case_ids: set[str] = set()
        roster_proposals: dict[str, dict[str, Any]] = {}
        manifest_hashes: set[str] = set()
        surfaces: set[str] = set()
        ready = True

        for row in rows:
            prior_id = _required_string(row.get("prior_card_id"), "prior_card_id")
            prior_snapshot = deepcopy(
                dict(_mapping(row.get("prior_card_snapshot"), "prior card snapshot"))
            )
            existing = candidates.get(prior_id)
            if existing is not None and canonical_hash(existing) != canonical_hash(
                prior_snapshot
            ):
                raise B1ChapterRegistryWriterError(
                    "one prior candidate has conflicting hearing snapshots"
                )
            candidates[prior_id] = prior_snapshot
            source_block_ids.update(
                _string_values(
                    row.get("source_block_ids"), "source_block_ids", allow_empty=True
                )
            )
            observation_ids.update(
                _string_values(
                    row.get("current_scan_observation_ids"),
                    "current_scan_observation_ids",
                    allow_empty=True,
                )
            )
            case_id = row.get("continuity_case_id")
            if isinstance(case_id, str) and case_id:
                continuity_case_ids.add(case_id)
            continuity_case_ids.update(
                _string_values(
                    row.get("continuity_case_ids") or [],
                    "continuity_case_ids",
                    allow_empty=True,
                )
            )
            manifest_hash = row.get("evidence_manifest_hash")
            if isinstance(manifest_hash, str) and manifest_hash:
                manifest_hashes.add(manifest_hash)
            for card in row.get("current_card_snapshots") or []:
                if not isinstance(card, Mapping):
                    continue
                key = _required_string(card.get("entity_id"), "entity_id")
                current_cards.setdefault(key, deepcopy(dict(card)))
                for value in [
                    card.get("canonical_surface"),
                    *(card.get("stable_surfaces") or []),
                ]:
                    if isinstance(value, str) and value.strip():
                        surfaces.add(value.strip())
            for dossier in row.get("current_dossier_snapshots") or []:
                if not isinstance(dossier, Mapping):
                    continue
                key = _required_string(
                    dossier.get("scan_observation_id"), "scan_observation_id"
                )
                current_dossiers.setdefault(key, deepcopy(dict(dossier)))
                value = dossier.get("surface")
                if isinstance(value, str) and value.strip():
                    surfaces.add(value.strip())
            attached = []
            for proposal in row.get("roster_recognition_proposals") or []:
                if not isinstance(proposal, Mapping):
                    continue
                proposal_copy = deepcopy(dict(proposal))
                proposal_key = canonical_hash(proposal_copy)
                roster_proposals[proposal_key] = proposal_copy
                value = proposal.get("surface")
                if isinstance(value, str) and value.strip():
                    surfaces.add(value.strip())
                attached.append(proposal_copy)
            contexts.append(
                {
                    "prior_card_id": prior_id,
                    "source_component_id": row["component_id"],
                    "question_type": row.get("question_type"),
                    "continuity_case_id": row.get("continuity_case_id"),
                    "trigger": deepcopy(row.get("trigger") or {}),
                    "roster_recognition_proposals": attached,
                }
            )
            ready = ready and row.get("lifecycle_state") == "ready_for_hearing"

        question_type = (
            "identity_linkage"
            if any(row.get("question_type") == "identity_linkage" for row in rows)
            else "roster_recognition"
        )
        body = {
            "question_type": question_type,
            "review_route": "identity_auditor",
            "contested_current_entity_id": current_entity_id,
            "contested_surfaces": sorted(
                surfaces, key=lambda value: (_normalized_surface(value), value)
            ),
            "prior_card_ids": sorted(candidates),
            "prior_candidate_snapshots": [
                candidates[prior_id] for prior_id in sorted(candidates)
            ],
            "candidate_contexts": sorted(
                contexts,
                key=lambda row: (row["prior_card_id"], row["source_component_id"]),
            ),
            "original_component_ids": [row["component_id"] for row in rows],
            "continuity_case_ids": sorted(continuity_case_ids),
            "current_scan_observation_ids": sorted(observation_ids),
            "current_entity_ids": [current_entity_id],
            "current_card_snapshots": [
                current_cards[key] for key in sorted(current_cards)
            ],
            "current_dossier_snapshots": [
                current_dossiers[key] for key in sorted(current_dossiers)
            ],
            "roster_recognition_proposals": [
                roster_proposals[key] for key in sorted(roster_proposals)
            ],
            "source_block_ids": sorted(source_block_ids),
            "evidence_manifest_hashes": sorted(manifest_hashes),
            "lifecycle_state": (
                "ready_for_hearing" if ready else "waiting_for_enrichment"
            ),
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        clustered.append(
            {"component_id": "b1xhear_" + canonical_hash(body)[:20], **body}
        )

    return [*untouched, *clustered], len(clustered)


def _apply_reopen_gate_v1(
    *,
    components: Sequence[Mapping[str, Any]],
    reconciled_projection: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if reconciled_projection is None:
        return [deepcopy(dict(row)) for row in components], []
    _verify_reconciled_projection_binding(reconciled_projection)
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for raw in components:
        row = deepcopy(dict(raw))
        if row.get("review_route") != "identity_auditor":
            kept.append(row)
            continue
        candidate_ids = _component_prior_card_ids(row)
        current_ids = _component_current_entity_ids(row)
        snapshots = {
            _required_string(card.get("prior_card_id"), "prior_card_id"): deepcopy(
                dict(card)
            )
            for card in row.get("prior_candidate_snapshots") or []
            if isinstance(card, Mapping)
        }
        if not candidate_ids or len(current_ids) != 1 or len(snapshots) != len(candidate_ids):
            kept.append(row)
            continue
        current_id = current_ids[0]
        checks: list[dict[str, Any]] = []
        admissible_ids: list[str] = []
        for prior_id in candidate_ids:
            prior = snapshots[prior_id]
            cited = set(
                _string_values(
                    row.get("source_block_ids"), "source_block_ids", allow_empty=True
                )
            )
            cited.update(
                block_id
                for block_id in prior.get("support_block_ids") or []
                if isinstance(block_id, str) and block_id
            )
            cited.update(
                ref["block_id"]
                for ref in prior.get("provenance_refs") or []
                if isinstance(ref, Mapping)
                and isinstance(ref.get("block_id"), str)
                and ref.get("block_id")
            )
            result = reopen_admissibility_v1(
                reconciled_projection,
                card_ids=[prior_id, current_id],
                cited_block_ids=sorted(cited),
            )
            check = {"prior_card_id": prior_id, **deepcopy(dict(result))}
            checks.append(check)
            if result["admissible"]:
                admissible_ids.append(prior_id)
            else:
                suppressed.append(
                    {
                        "source_component_id": row["component_id"],
                        "prior_card_id": prior_id,
                        "current_entity_id": current_id,
                        "prior_state": result["prior_state"],
                        "entry_id": result.get("entry_id"),
                        "reason": result["reason"],
                    }
                )
        if not admissible_ids:
            continue
        row["prior_card_ids"] = sorted(admissible_ids)
        row["prior_candidate_snapshots"] = [
            snapshots[prior_id] for prior_id in sorted(admissible_ids)
        ]
        if isinstance(row.get("candidate_contexts"), list):
            row["candidate_contexts"] = [
                context
                for context in row["candidate_contexts"]
                if isinstance(context, Mapping)
                and context.get("prior_card_id") in admissible_ids
            ]
        row["reopen_checks"] = checks
        body = deepcopy(row)
        body.pop("component_id", None)
        row["component_id"] = "b1xhear_" + canonical_hash(body)[:20]
        kept.append(row)
    return kept, sorted(
        suppressed,
        key=lambda row: (
            row["source_component_id"],
            row["prior_card_id"],
            row["current_entity_id"],
        ),
    )


def _verify_reconciled_projection_binding(projection: Mapping[str, Any]) -> None:
    observed = projection.get("projection_hash")
    if not isinstance(observed, str) or len(observed) != 64:
        raise B1ChapterRegistryWriterError("reconciled projection hash is absent")
    body = deepcopy(dict(projection))
    body.pop("projection_hash", None)
    if canonical_hash(body) != observed:
        raise B1ChapterRegistryWriterError("reconciled projection hash mismatch")


def _component_prior_card_ids(row: Mapping[str, Any]) -> list[str]:
    plural = row.get("prior_card_ids")
    if isinstance(plural, list):
        return sorted(
            {_required_string(value, "prior_card_ids item") for value in plural}
        )
    singular = row.get("prior_card_id")
    return [_required_string(singular, "prior_card_id")] if singular else []


def _component_current_entity_ids(row: Mapping[str, Any]) -> list[str]:
    plural = row.get("current_entity_ids")
    if isinstance(plural, list):
        return sorted(
            {_required_string(value, "current_entity_ids item") for value in plural}
        )
    singular = row.get("current_entity_id")
    return [_required_string(singular, "current_entity_id")] if singular else []


def _roster_proposals_by_prior_card(
    *,
    scan_artifact: Mapping[str, Any],
    prior_card_by_id: Mapping[str, Mapping[str, Any]],
    card_by_source_ref: Mapping[str, Mapping[str, Any]],
    dossier_by_scan: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Group accepted roster proposals by the prior card they name.

    Each row is joined to whatever this chapter built for that surface, so the
    hearing compares two dossiers rather than a dossier against a bare name.
    A proposal naming a card that was not supplied is returned as unqueued -
    visible and countable, never dropped.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    unqueued: list[dict[str, Any]] = []
    observations = {
        _required_string(row.get("observation_id"), "observation_id"): row
        for row in _sequence_of_mappings(
            scan_artifact.get("entity_observations") or [], "entity observations"
        )
    }
    for raw in _sequence_of_mappings(
        scan_artifact.get("roster_recognition_proposals") or [], "roster proposals"
    ):
        prior_card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if prior_card_id not in prior_card_by_id:
            unqueued.append(
                {
                    "proposal_id": raw.get("proposal_id"),
                    "prior_card_id": prior_card_id,
                    "reason": "prior card was not supplied to the queue producer",
                }
            )
            continue
        surface_key = _normalized_surface(raw.get("surface"))
        scan_observation_id = None
        for observation_id, observation in sorted(observations.items()):
            if _normalized_surface(observation.get("surface")) == surface_key:
                scan_observation_id = observation_id
                break
        current_card = (
            card_by_source_ref.get(f"scan:{scan_observation_id}")
            if scan_observation_id
            else None
        )
        current_dossier = (
            dossier_by_scan.get(scan_observation_id) if scan_observation_id else None
        )
        grouped.setdefault(prior_card_id, []).append(
            {
                "proposal_id": raw.get("proposal_id"),
                "surface": raw.get("surface"),
                "reason": raw.get("reason"),
                "source_block_ids": list(raw.get("source_block_ids") or []),
                "scan_observation_id": scan_observation_id,
                "current_card_snapshot": (
                    deepcopy(dict(current_card)) if current_card is not None else None
                ),
                "current_dossier_snapshot": (
                    deepcopy(dict(current_dossier))
                    if current_dossier is not None
                    else None
                ),
                "identity_authority_granted": False,
            }
        )
    for rows in grouped.values():
        rows.sort(key=lambda row: (str(row.get("surface") or ""), str(row.get("proposal_id") or "")))
    return grouped, unqueued


def verify_b1_cross_chapter_hearing_queue_v1(
    queue: Mapping[str, Any],
) -> None:
    if queue.get("schema_version") not in {
        CROSS_CHAPTER_QUEUE_SCHEMA_VERSION,
        LEGACY_CROSS_CHAPTER_QUEUE_SCHEMA_VERSION,
    }:
        raise B1ChapterRegistryWriterError("foreign cross-chapter queue schema")
    observed = queue.get("queue_hash")
    if not isinstance(observed, str):
        raise B1ChapterRegistryWriterError("cross-chapter queue hash is absent")
    body = deepcopy(dict(queue))
    body.pop("queue_hash", None)
    if canonical_hash(body) != observed:
        raise B1ChapterRegistryWriterError("cross-chapter queue hash mismatch")
    components = _sequence_of_mappings(queue.get("components"), "hearing components")
    ids = [_required_string(row.get("component_id"), "component_id") for row in components]
    if len(ids) != len(set(ids)):
        raise B1ChapterRegistryWriterError("cross-chapter queue duplicates a component")
    allowed_routes = {
        "identity_auditor",
        "stable_claim_auditor",
        "temporal_auditor",
        "glossary_auditor",
        "pending_unassigned",
    }
    allowed_states = {"ready_for_hearing", "waiting_for_enrichment", "pending_route"}
    for row in components:
        if row.get("review_route") not in allowed_routes:
            raise B1ChapterRegistryWriterError("cross-chapter route is unknown")
        if row.get("lifecycle_state") not in allowed_states:
            raise B1ChapterRegistryWriterError("cross-chapter lifecycle state is unknown")
        if row.get("identity_authority_granted") is not False:
            raise B1ChapterRegistryWriterError("hearing component grants identity authority")
        if row.get("claim_authority_granted") is not False:
            raise B1ChapterRegistryWriterError("hearing component grants claim authority")


def verify_b1_chapter_registry_v1(registry: Mapping[str, Any]) -> None:
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise B1ChapterRegistryWriterError("foreign chapter registry schema")
    expected_hash = registry.get("registry_hash")
    if not isinstance(expected_hash, str):
        raise B1ChapterRegistryWriterError("chapter registry hash is absent")
    body = dict(registry)
    body.pop("registry_hash", None)
    if canonical_hash(body) != expected_hash:
        raise B1ChapterRegistryWriterError("chapter registry hash mismatch")
    if registry.get("identity_authority_granted") is not False:
        raise B1ChapterRegistryWriterError("chapter writer granted identity authority")
    if registry.get("book_authority_granted") is not False:
        raise B1ChapterRegistryWriterError("chapter writer granted book authority")
    if registry.get("database_mutation_performed") is not False:
        raise B1ChapterRegistryWriterError("chapter artifact claims a database mutation")
    cards = _sequence_of_mappings(registry.get("cards"), "registry cards")
    ids = [_required_string(row.get("entity_id"), "entity_id") for row in cards]
    if len(ids) != len(set(ids)):
        raise B1ChapterRegistryWriterError("registry contains duplicate entity ids")
    known = set(ids)
    projected_merges: list[dict[str, Any]] = []
    for card in cards:
        stable_surfaces = card.get("stable_surfaces")
        if (
            not isinstance(stable_surfaces, list)
            or not stable_surfaces
            or len({_normalized_surface(value) for value in stable_surfaces})
            != len(stable_surfaces)
        ):
            raise B1ChapterRegistryWriterError(
                "registry card stable surfaces are malformed"
            )
        for claim in _sequence_of_mappings(card.get("claims"), "card claims"):
            if claim.get("effective") is not _effective_claim(claim):
                raise B1ChapterRegistryWriterError("claim authority projection differs")
        merge = card.get("within_chapter_identity_merge")
        if merge is None:
            if card.get("merged_observation_evidence") is not None:
                raise B1ChapterRegistryWriterError(
                    "unmerged card carries merged observation evidence"
                )
            continue
        if not isinstance(merge, Mapping):
            raise B1ChapterRegistryWriterError(
                "within-chapter identity merge is malformed"
            )
        member_refs = _string_values(
            merge.get("member_source_refs"), "merge member source refs"
        )
        component_ids = _string_values(
            merge.get("source_component_ids"), "merge source component ids"
        )
        if (
            len(member_refs) < 2
            or set(member_refs) != set(card.get("source_refs") or [])
            or merge.get("representative_source_ref") not in member_refs
            or not component_ids
            or merge.get("authority_scope") != "chapter_only"
            or merge.get("identity_authority_granted") is not False
            or merge.get("book_authority_granted") is not False
        ):
            raise B1ChapterRegistryWriterError(
                "within-chapter identity merge policy differs"
            )
        merged_evidence = _sequence_of_mappings(
            card.get("merged_observation_evidence"),
            "merged observation evidence",
        )
        stable_surface_sources = {
            _normalized_surface(
                _required_string(card.get("canonical_surface"), "canonical surface")
            ),
            *(
                _normalized_surface(
                    _required_string(evidence.get("surface"), "merged surface")
                )
                for evidence in merged_evidence
                if evidence.get("retrieval_surface_authority")
                == "stable_name_variant"
            ),
        }
        for evidence in merged_evidence:
            surface = _required_string(evidence.get("surface"), "merged surface")
            retrieval = evidence.get("retrieval_surface_authority")
            if retrieval not in {"stable_name_variant", "evidence_only"}:
                raise B1ChapterRegistryWriterError(
                    "merged observation retrieval policy differs"
                )
            if (
                retrieval == "evidence_only"
                and _normalized_surface(surface)
                in {_normalized_surface(value) for value in stable_surfaces}
                and _normalized_surface(surface) not in stable_surface_sources
            ):
                raise B1ChapterRegistryWriterError(
                    "descriptive merge evidence leaked into stable surfaces"
                )
        projected_merges.append(deepcopy(dict(merge)))
    declared_merges = registry.get("within_chapter_identity_merges")
    if not (declared_merges is None and not projected_merges) and (
        declared_merges != projected_merges
    ):
        raise B1ChapterRegistryWriterError(
            "within-chapter identity merge projection differs"
        )
    for edge in _sequence_of_mappings(registry.get("relation_edges"), "relation edges"):
        if edge.get("source_entity_id") not in known or edge.get("target_entity_id") not in known:
            raise B1ChapterRegistryWriterError("relation edge cites a foreign entity")
        if edge.get("source_entity_id") == edge.get("target_entity_id"):
            raise B1ChapterRegistryWriterError("relation edge is reflexive")
        expected_edge_id = "litrel1_" + canonical_hash(_edge_identity(edge))[:20]
        if edge.get("relation_edge_id") != expected_edge_id:
            raise B1ChapterRegistryWriterError("relation edge id differs")
    projected = registry.get("prior_cards_projection")
    if not isinstance(projected, Mapping):
        raise B1ChapterRegistryWriterError("prior-card projection is absent")
    if projected.get("cards") != _project_prior_cards(cards):
        raise B1ChapterRegistryWriterError("prior-card projection differs from cards")


def _within_chapter_same_referent_groups(
    *,
    source_refs: Sequence[str],
    decisions: Sequence[Mapping[str, Any]],
    continued_prior_id_by_scan: Mapping[str, str] | None = None,
) -> tuple[list[list[str]], list[Mapping[str, Any]]]:
    parent = {source_ref: source_ref for source_ref in source_refs}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        keep, absorb = sorted((left_root, right_root))
        parent[absorb] = keep

    accepted: list[Mapping[str, Any]] = []
    for decision in decisions:
        if (
            decision.get("component_kind") != "same_referent_proposal"
            or decision.get("action") != "accept_proposal"
        ):
            continue
        subject_ref = _required_string(decision.get("subject_ref"), "subject ref")
        proposal = _mapping(
            decision.get("original_proposal"), "same-referent proposal"
        )
        target_ref = _required_string(proposal.get("target_ref"), "target ref")
        if (
            subject_ref == target_ref
            or subject_ref not in parent
            or target_ref not in parent
        ):
            raise B1ChapterRegistryWriterError(
                "accepted same-referent proposal cites a foreign/reflexive ref"
            )
        basis = proposal.get("proposal_basis")
        expected_policy = {
            "name_variant": "subject_stable_name_variant",
            "chapter_context_description": "subject_evidence_only",
        }.get(basis)
        if (
            expected_policy is None
            or proposal.get("retrieval_surface_policy") != expected_policy
            or proposal.get("identity_authority_granted") is not False
        ):
            raise B1ChapterRegistryWriterError(
                "accepted same-referent proposal policy differs"
            )
        union(subject_ref, target_ref)
        accepted.append(decision)

    continued_refs_by_prior: dict[str, list[str]] = {}
    for scan_id, prior_id in sorted((continued_prior_id_by_scan or {}).items()):
        source_ref = f"scan:{scan_id}"
        if source_ref not in parent:
            raise B1ChapterRegistryWriterError(
                "continued identity cites a foreign scan observation"
            )
        continued_refs_by_prior.setdefault(prior_id, []).append(source_ref)
    for continued_refs in continued_refs_by_prior.values():
        anchor_ref = continued_refs[0]
        for source_ref in continued_refs[1:]:
            union(anchor_ref, source_ref)

    grouped: dict[str, list[str]] = {}
    for source_ref in source_refs:
        grouped.setdefault(find(source_ref), []).append(source_ref)
    groups = [sorted(rows) for rows in grouped.values()]
    groups.sort(key=lambda rows: tuple(rows))
    return groups, accepted


def _representative_source_ref(
    source_refs: Sequence[str],
    *,
    source_by_ref: Mapping[
        str, tuple[Mapping[str, Any], Mapping[str, Any] | None]
    ],
    block_ids: set[str],
    block_order: Mapping[str, int],
) -> str:
    class_rank = {
        "named_entity_candidate": 0,
        "unresolved_named_reference": 1,
        "important_unnamed_referent": 2,
    }

    def rank(source_ref: str) -> tuple[Any, ...]:
        scan, dossier = source_by_ref[source_ref]
        surface = _required_string(scan.get("surface"), "representative surface")
        source_blocks = _block_list(
            scan.get("source_block_ids"), block_ids, "representative source blocks"
        )
        tokens = _normalized_surface(surface).split()
        return (
            class_rank.get(str(scan.get("record_class")), 3),
            0 if dossier is not None else 1,
            -len(tokens),
            -len(surface),
            min(block_order[value] for value in source_blocks),
            source_ref,
        )

    return min(source_refs, key=rank)


def _merge_within_chapter_cards(
    *,
    representative_ref: str,
    member_cards: Mapping[str, Mapping[str, Any]],
    source_by_ref: Mapping[
        str, tuple[Mapping[str, Any], Mapping[str, Any] | None]
    ],
    source_component_ids: Sequence[str],
    block_order: Mapping[str, int],
) -> dict[str, Any]:
    representative = deepcopy(dict(member_cards[representative_ref]))
    if len(member_cards) == 1:
        return representative

    ordered_refs = sorted(
        member_cards,
        key=lambda source_ref: (
            member_cards[source_ref]["first_seen"]["order_index"],
            source_ref,
        ),
    )
    component_ids = sorted(set(source_component_ids))

    stable_surfaces: list[str] = []
    seen_surfaces: set[str] = set()
    stable_ref_order = [representative_ref] + [
        source_ref for source_ref in ordered_refs if source_ref != representative_ref
    ]
    for source_ref in stable_ref_order:
        scan, _dossier = source_by_ref[source_ref]
        if scan.get("record_class") != "named_entity_candidate":
            continue
        surface = _required_string(scan.get("surface"), "merged stable surface")
        normalized = _normalized_surface(surface)
        if normalized in seen_surfaces:
            continue
        seen_surfaces.add(normalized)
        stable_surfaces.append(surface)

    def combined_rows(key: str) -> list[dict[str, Any]]:
        rows = [
            deepcopy(dict(row))
            for source_ref in ordered_refs
            for row in member_cards[source_ref].get(key) or []
            if isinstance(row, Mapping)
        ]
        return _dedupe_mapping_rows(rows)

    support_blocks = sorted(
        {
            block_id
            for source_ref in ordered_refs
            for block_id in member_cards[source_ref]["support_block_ids"]
        },
        key=block_order.__getitem__,
    )
    earliest_card = min(
        (member_cards[source_ref] for source_ref in ordered_refs),
        key=lambda row: (row["first_seen"]["order_index"], row["entity_id"]),
    )
    non_representative_histories = [
        deepcopy(dict(row))
        for source_ref in ordered_refs
        if source_ref != representative_ref
        for row in member_cards[source_ref]["presence_history"]
    ]
    representative_histories = [
        deepcopy(dict(row))
        for row in member_cards[representative_ref]["presence_history"]
    ]
    representative.update(
        {
            "stable_surfaces": stable_surfaces,
            "record_state": (
                "chapter_confirmed"
                if any(
                    source_by_ref[source_ref][0].get("record_class")
                    == "named_entity_candidate"
                    and source_by_ref[source_ref][1] is not None
                    for source_ref in ordered_refs
                )
                else representative["record_state"]
            ),
            "claims": combined_rows("claims"),
            "aliases": combined_rows("aliases"),
            "address_forms_used": combined_rows("address_forms_used"),
            "first_seen": deepcopy(earliest_card["first_seen"]),
            "support_block_ids": support_blocks,
            "presence_history": [
                *non_representative_histories,
                *representative_histories,
            ],
            "source_refs": ordered_refs,
            "merged_observation_evidence": [
                {
                    "source_ref": source_ref,
                    "surface": member_cards[source_ref]["canonical_surface"],
                    "record_class": member_cards[source_ref]["record_class"],
                    "identity_summary": deepcopy(
                        member_cards[source_ref]["identity_summary"]
                    ),
                    "distinguishing_note": deepcopy(
                        member_cards[source_ref]["distinguishing_note"]
                    ),
                    "source_block_ids": deepcopy(
                        member_cards[source_ref]["support_block_ids"]
                    ),
                    "retrieval_surface_authority": (
                        "stable_name_variant"
                        if member_cards[source_ref]["record_class"]
                        == "named_entity_candidate"
                        else "evidence_only"
                    ),
                }
                for source_ref in ordered_refs
                if source_ref != representative_ref
            ],
            "within_chapter_identity_merge": {
                "representative_source_ref": representative_ref,
                "member_source_refs": ordered_refs,
                "source_component_ids": component_ids,
                "authority_scope": "chapter_only",
                "identity_authority_granted": False,
                "book_authority_granted": False,
            },
        }
    )
    return representative


def _dedupe_mapping_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        row = deepcopy(dict(raw))
        key = canonical_hash(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _build_card(
    *,
    chapter_id: str,
    source_ref: str,
    entity_id: str,
    scan: Mapping[str, Any],
    dossier: Mapping[str, Any] | None,
    decision_index: Mapping[str, Mapping[str, Any]],
    entity_id_by_ref: Mapping[str, str],
    block_ids: set[str],
    block_order: Mapping[str, int],
) -> dict[str, Any]:
    surface = _required_string(scan.get("surface"), "card surface")
    source_blocks = _block_list(
        scan.get("all_source_block_ids") or scan.get("source_block_ids"),
        block_ids,
        "card blocks",
    )
    kind = _required_string(scan.get("referent_kind_claim"), "referent kind")
    if kind not in REFERENT_KINDS:
        raise B1ChapterRegistryWriterError("card uses a foreign referent kind")
    presence_basis = _required_string(scan.get("presence_basis"), "presence basis")
    if presence_basis not in PRESENCE_BASES:
        raise B1ChapterRegistryWriterError("card uses a foreign presence basis")
    presence_status = "observed"
    for decision in decision_index.values():
        if (
            decision["component_kind"] != "presence_correction"
            or decision["subject_ref"] != source_ref
        ):
            continue
        proposal = decision["original_proposal"]
        if decision["action"] in {"accept_proposal", "revise_proposal"}:
            finding = proposal.get("finding")
            if not isinstance(finding, Mapping):
                raise B1ChapterRegistryWriterError("presence decision lacks finding")
            proposed = _required_string(
                finding.get("proposed_presence_basis"), "proposed presence basis"
            )
            if proposed not in PRESENCE_BASES:
                raise B1ChapterRegistryWriterError("presence decision uses foreign basis")
            presence_basis = proposed
            presence_status = "auditor_reviewed"

    claims: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    address_forms: list[dict[str, Any]] = []
    identity_summary = None
    distinguishing_note = None
    if dossier is not None:
        for raw in _sequence_of_mappings(dossier.get("claims"), "dossier claims"):
            claim = deepcopy(dict(raw))
            claim["provenance"] = {
                "actor": "b1_enrich",
                "chapter_id": chapter_id,
                "source_ref": source_ref,
            }
            claim["validity"] = {"from_block": None, "to_block": None}
            claim["effective"] = _effective_claim(claim)
            claims.append(claim)
        identity_summary = _required_string(
            dossier.get("identity_summary"), "identity_summary"
        )
        distinguishing_note = dossier.get("distinguishing_note")
        for raw in _sequence_of_mappings(
            dossier.get("address_forms_used"), "address forms"
        ):
            counterpart_ref = _required_string(
                raw.get("counterpart_ref"), "address counterpart ref"
            )
            address_forms.append(
                {
                    **deepcopy(dict(raw)),
                    "counterpart_entity_id": entity_id_by_ref.get(counterpart_ref),
                    "semantic_status": "observed_evidence",
                    "alias_authority": False,
                }
            )
        for alias in _sequence_of_mappings(dossier.get("aliases_observed"), "aliases"):
            decision = _lookup_decision(
                decision_index,
                kind="alias_proposal",
                subject_ref=source_ref,
                proposal=alias,
            )
            if decision is None or decision["action"] not in {
                "accept_proposal",
                "revise_proposal",
            }:
                continue
            aliases.append(
                {
                    "surface": _required_string(alias.get("surface"), "alias surface"),
                    "status": "chapter_confirmed",
                    "lookup_authority": "chapter_only",
                    "first_supported_block_id": _first_block(
                        _block_list(alias.get("anchor_block_ids"), block_ids, "alias blocks"),
                        block_order,
                    ),
                    "source_component_id": decision["component_id"],
                }
            )

    support_blocks = set(source_blocks)
    if dossier is not None:
        for raw in _sequence_of_mappings(
            dossier.get("claims"), "claim anchor_block_ids rows"
        ):
            support_blocks.update(
                _claim_block_list(raw, block_ids, "anchor_block_ids")
            )
        for collection, anchor_key in (
            (dossier.get("kinship_links"), "anchor_block_ids"),
            (dossier.get("links"), "anchor_block_ids"),
            (dossier.get("address_forms_used"), "anchor_block_ids"),
            (dossier.get("aliases_observed"), "anchor_block_ids"),
        ):
            for raw in _sequence_of_mappings(collection, f"{anchor_key} rows"):
                support_blocks.update(
                    _block_list(raw.get(anchor_key), block_ids, anchor_key)
                )
    record_class = _required_string(scan.get("record_class"), "record class")
    first_block_id = _first_block(source_blocks, block_order)
    record_state = (
        "chapter_confirmed"
        if record_class == "named_entity_candidate" and dossier is not None
        else "chapter_provisional"
    )
    return {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "record_class": record_class,
        "record_state": record_state,
        "referent_kind": {
            "value": kind,
            "basis": "contextual_inference",
            "semantic_status": "unreviewed",
            "effective": False,
        },
        "identity_summary": {
            "text": identity_summary,
            "semantic_status": "unreviewed" if identity_summary else "missing",
            "authority_scope": "chapter_provisional",
        },
        "distinguishing_note": distinguishing_note,
        "claims": claims,
        "aliases": aliases,
        "address_forms_used": address_forms,
        "first_seen": {
            "chapter_id": chapter_id,
            "block_id": first_block_id,
            "order_index": block_order[first_block_id],
        },
        "support_block_ids": sorted(support_blocks, key=block_order.__getitem__),
        "presence_history": [
            {
                "chapter_id": chapter_id,
                "presence_basis": presence_basis,
                "semantic_status": presence_status,
                "source_block_ids": sorted(source_blocks, key=block_order.__getitem__),
            }
        ],
        "source_refs": [source_ref],
        "chapter_authority": True,
        "identity_authority": False,
        "book_authority": False,
    }


def _build_referenced_prior_cards(
    *,
    chapter_id: str,
    enrich_artifact: Mapping[str, Any],
    block_ids: set[str],
    block_order: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Carry a referenced prior card without inventing a Scan observation."""

    rows: list[dict[str, Any]] = []
    for case in _sequence_of_mappings(
        enrich_artifact.get("continuity_cases") or [], "continuity cases"
    ):
        if case.get("packet_action") != "carry_referenced_prior_card":
            continue
        if case.get("hearing_required") is not False:
            raise B1ChapterRegistryWriterError(
                "referenced prior carry still requires a hearing"
            )
        current_ids = _string_values(
            case.get("current_scan_observation_ids"),
            "current_scan_observation_ids",
            allow_empty=True,
        )
        if current_ids:
            raise B1ChapterRegistryWriterError(
                "referenced prior carry unexpectedly binds a scan observation"
            )
        prior_id = _required_string(case.get("prior_card_id"), "prior_card_id")
        snapshot = _mapping(case.get("prior_card_snapshot"), "prior card snapshot")
        if snapshot.get("prior_card_id") != prior_id:
            raise B1ChapterRegistryWriterError(
                "referenced prior-card snapshot identity differs"
            )
        source_blocks = _block_list(
            case.get("source_block_ids"), block_ids, "referenced prior source blocks"
        )
        canonical_surface = _required_string(
            snapshot.get("canonical_surface"), "prior canonical_surface"
        )
        stable_surfaces = _string_values(
            snapshot.get("stable_surfaces"), "prior stable_surfaces"
        )
        kind = _required_string(snapshot.get("referent_kind"), "prior referent_kind")
        if kind not in REFERENT_KINDS:
            raise B1ChapterRegistryWriterError(
                "referenced prior carry uses a foreign referent kind"
            )
        claim_state = _required_string(snapshot.get("claim_state"), "prior claim_state")
        if claim_state not in {"confirmed", "provisional"}:
            raise B1ChapterRegistryWriterError(
                "referenced prior carry uses a foreign claim state"
            )
        projected_class = _required_string(
            snapshot.get("record_class"), "prior record_class"
        )
        record_class = (
            "named_entity_candidate"
            if projected_class == "confirmed_entity"
            else projected_class
        )
        if record_class not in {
            "named_entity_candidate",
            "important_unnamed_referent",
            "unresolved_named_reference",
        }:
            raise B1ChapterRegistryWriterError(
                "referenced prior carry uses a foreign record class"
            )
        case_id = _required_string(
            case.get("continuity_case_id"), "continuity_case_id"
        )
        claims: list[dict[str, Any]] = []
        for raw_claim in _sequence_of_mappings(
            snapshot.get("profile_claims") or [], "prior profile claims"
        ):
            claim = deepcopy(dict(raw_claim))
            claim["anchor_block_ids"] = _string_values(
                claim.get("anchor_block_ids") or [],
                "prior claim anchor_block_ids",
                allow_empty=True,
            )
            claim["provenance"] = {
                "actor": "prior_registry_carry",
                "chapter_id": chapter_id,
                "source_ref": prior_id,
            }
            claim["validity"] = deepcopy(
                claim.get("validity")
                if isinstance(claim.get("validity"), Mapping)
                else {"from_block": None, "to_block": None}
            )
            claim["effective"] = _effective_claim(claim)
            claims.append(claim)
        first_block_id = _first_block(source_blocks, block_order)
        rows.append(
            {
                "entity_id": prior_id,
                "canonical_surface": canonical_surface,
                "stable_surfaces": stable_surfaces,
                "record_class": record_class,
                "record_state": (
                    "chapter_confirmed"
                    if claim_state == "confirmed"
                    and record_class == "named_entity_candidate"
                    else "chapter_provisional"
                ),
                "referent_kind": {
                    "value": kind,
                    "basis": "prior_registry_snapshot",
                    "semantic_status": "carried_prior_context",
                    "effective": False,
                },
                "identity_summary": {
                    "text": _required_string(
                        snapshot.get("identity_summary"), "prior identity_summary"
                    ),
                    "semantic_status": "carried_prior_context",
                    "authority_scope": "chapter_provisional",
                },
                "distinguishing_note": deepcopy(
                    snapshot.get("distinguishing_note")
                ),
                "claims": claims,
                "aliases": [],
                "address_forms_used": [],
                "first_seen": {
                    "chapter_id": chapter_id,
                    "block_id": first_block_id,
                    "order_index": block_order[first_block_id],
                },
                "support_block_ids": sorted(
                    source_blocks, key=block_order.__getitem__
                ),
                "presence_history": [
                    {
                        "chapter_id": chapter_id,
                        "presence_basis": "referenced_by_other",
                        "semantic_status": "referenced_prior_carry",
                        "source_block_ids": sorted(
                            source_blocks, key=block_order.__getitem__
                        ),
                    }
                ],
                "source_refs": [prior_id],
                "chapter_authority": True,
                "identity_authority": False,
                "book_authority": False,
            }
        )
    return rows


def _build_relation_edges(
    *,
    decisions: Sequence[Mapping[str, Any]],
    entity_id_by_ref: Mapping[str, str],
    chapter_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for decision in decisions:
        if decision["component_kind"] not in {"entity_link", "kinship_link"}:
            continue
        if decision["action"] not in {"accept_proposal", "revise_proposal"}:
            continue
        subject_ref = decision["subject_ref"]
        proposal = decision["original_proposal"]
        target_ref = (
            decision.get("revised_target_ref")
            if decision["action"] == "revise_proposal"
            else proposal.get("target_ref")
        )
        relation = (
            decision.get("revised_relation")
            if decision["action"] == "revise_proposal"
            else proposal.get("relation")
        )
        relation_note = (
            decision.get("revised_relation_note")
            if decision["action"] == "revise_proposal"
            else proposal.get("relation_note")
        )
        is_open_relation = (
            decision["component_kind"] == "entity_link"
            and relation == "other_link"
        ) or (
            decision["component_kind"] == "kinship_link"
            and relation == "other_kin"
        )
        relation_raw = (
            relation_note
            if decision["action"] == "revise_proposal"
            and is_open_relation
            else proposal.get("relation_raw")
        )
        relation_status = (
            (
                "model_other"
                if is_open_relation
                else "in_vocabulary"
            )
            if decision["action"] == "revise_proposal"
            and decision["component_kind"] in {"entity_link", "kinship_link"}
            else proposal.get("relation_status")
        )
        if subject_ref not in entity_id_by_ref or target_ref not in entity_id_by_ref:
            continue
        candidates.append(
            _normalize_relation_candidate(
                component_kind=decision["component_kind"],
                relation=_required_string(relation, "accepted relation"),
                relation_note=relation_note,
                relation_raw=relation_raw,
                relation_status=relation_status,
                source_entity_id=entity_id_by_ref[subject_ref],
                target_entity_id=entity_id_by_ref[str(target_ref)],
                proposal=proposal,
                decision=decision,
                chapter_id=chapter_id,
            )
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        grouped.setdefault(canonical_hash(_edge_group_identity(row)), []).append(row)
    edges: list[dict[str, Any]] = []
    projection_issues: list[dict[str, Any]] = []
    for group in grouped.values():
        first = group[0]
        variants = sorted({value for row in group for value in row["relation_variants"]})
        anchors = sorted({value for row in group for value in row["anchor_block_ids"]})
        component_ids = sorted({row["source_component_id"] for row in group})
        if first["source_entity_id"] == first["target_entity_id"]:
            projection_issues.append(
                {
                    "row_type": "post_merge_relation_conflict",
                    "state": "unreviewed",
                    "reason_code": "relation_collapsed_to_self_after_identity_merge",
                    "relation_family": first["relation_family"],
                    "relation": first["relation"],
                    "relation_variants": variants,
                    "source_entity_id": first["source_entity_id"],
                    "target_entity_id": first["target_entity_id"],
                    "anchor_block_ids": anchors,
                    "source_component_ids": component_ids,
                    "reason": (
                        "Accepted relation endpoints resolve to the same entity "
                        "after within-chapter identity consolidation."
                    ),
                }
            )
            continue
        edge = {
            "relation_family": first["relation_family"],
            "relation": first["relation"],
            "relation_variants": variants,
            "source_entity_id": first["source_entity_id"],
            "target_entity_id": first["target_entity_id"],
            "chapter_id": chapter_id,
            "anchor_block_ids": anchors,
            "semantic_status": "auditor_reviewed",
            "effective": True,
            "source_component_ids": component_ids,
            "validity_scope": "as_of_chapter",
        }
        for key in ("relation_note", "relation_raw", "relation_status"):
            if key in first:
                edge[key] = deepcopy(first[key])
        edge["relation_edge_id"] = "litrel1_" + canonical_hash(_edge_identity(edge))[:20]
        edges.append(edge)
    edges.sort(key=lambda row: row["relation_edge_id"])
    projection_issues.sort(
        key=lambda row: (
            row["source_entity_id"],
            row["relation_family"],
            tuple(row["source_component_ids"]),
        )
    )
    return edges, projection_issues


def _mark_structurally_impossible_kinship_v1(
    relation_edges: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Surface closed graph contradictions without choosing a correct edge."""

    edges = [deepcopy(dict(row)) for row in relation_edges]
    edge_by_id = {
        _required_string(row.get("relation_edge_id"), "relation edge id"): row
        for row in edges
    }
    parent_edges = [row for row in edges if row.get("relation") == "parent_of"]
    parents_by_child: dict[str, dict[str, list[str]]] = {}
    for edge in parent_edges:
        child_id = _required_string(edge.get("target_entity_id"), "child entity id")
        parent_id = _required_string(edge.get("source_entity_id"), "parent entity id")
        parents_by_child.setdefault(child_id, {}).setdefault(parent_id, []).append(
            edge["relation_edge_id"]
        )

    contradictions: set[tuple[str, tuple[str, ...]]] = set()

    # E-1: a parent and child cannot themselves share a parent.
    for edge in parent_edges:
        source_id = edge["source_entity_id"]
        target_id = edge["target_entity_id"]
        shared_parents = set(parents_by_child.get(source_id, {})) & set(
            parents_by_child.get(target_id, {})
        )
        for shared_parent_id in shared_parents:
            involved = {
                edge["relation_edge_id"],
                *parents_by_child[source_id][shared_parent_id],
                *parents_by_child[target_id][shared_parent_id],
            }
            contradictions.add(("E-1", tuple(sorted(involved))))

    # E-2: the same pair cannot be both siblings and parent/child.
    pair_relations: dict[tuple[str, str], dict[str, list[str]]] = {}
    for edge in edges:
        relation = edge.get("relation")
        if relation not in {"parent_of", "sibling_of"}:
            continue
        pair = tuple(
            sorted(
                (
                    _required_string(
                        edge.get("source_entity_id"), "relation source entity id"
                    ),
                    _required_string(
                        edge.get("target_entity_id"), "relation target entity id"
                    ),
                )
            )
        )
        pair_relations.setdefault(pair, {}).setdefault(str(relation), []).append(
            edge["relation_edge_id"]
        )
    for by_relation in pair_relations.values():
        if {"parent_of", "sibling_of"} <= set(by_relation):
            contradictions.add(
                (
                    "E-2",
                    tuple(
                        sorted(
                            {
                                *by_relation["parent_of"],
                                *by_relation["sibling_of"],
                            }
                        )
                    ),
                )
            )

    # E-3: a two-node parent cycle is structurally impossible.
    directional_parent_edges: dict[tuple[str, str], list[str]] = {}
    for edge in parent_edges:
        key = (edge["source_entity_id"], edge["target_entity_id"])
        directional_parent_edges.setdefault(key, []).append(edge["relation_edge_id"])
    for (source_id, target_id), forward_ids in directional_parent_edges.items():
        reverse_ids = directional_parent_edges.get((target_id, source_id))
        if reverse_ids:
            contradictions.add(
                ("E-3", tuple(sorted({*forward_ids, *reverse_ids})))
            )

    ordered = sorted(contradictions, key=lambda row: (row[0], row[1]))
    assigned_group_by_edge: dict[str, tuple[str, str]] = {}
    issues: list[dict[str, Any]] = []
    for rule, edge_ids in ordered:
        group_id = "litrelcontest1_" + canonical_hash(list(edge_ids))[:20]
        for edge_id in edge_ids:
            previous = assigned_group_by_edge.get(edge_id)
            if previous is not None and previous != (rule, group_id):
                raise B1ChapterRegistryWriterError(
                    "relation edge participates in overlapping structural "
                    "contradictions not covered by the contract"
                )
            assigned_group_by_edge[edge_id] = (rule, group_id)
        anchor_sets = [
            set(edge_by_id[edge_id].get("anchor_block_ids") or [])
            for edge_id in edge_ids
        ]
        shared_anchors = (
            sorted(set.intersection(*anchor_sets)) if anchor_sets else []
        )
        issues.append(
            {
                "row_type": "kinship_structurally_impossible",
                "state": "unreviewed",
                "reason_code": "kinship_structurally_impossible",
                "contested_rule": rule,
                "contested_group_id": group_id,
                "relation_edge_ids": list(edge_ids),
                "shared_anchor_block_ids": shared_anchors,
                "reason": (
                    "Kinship edges form a structurally impossible graph under "
                    f"rule {rule}."
                ),
            }
        )

    for edge_id, (rule, group_id) in assigned_group_by_edge.items():
        edge = edge_by_id[edge_id]
        edge["structurally_contested"] = True
        edge["contested_group_id"] = group_id
        edge["contested_rule"] = rule
        edge["effective"] = False
        edge["semantic_status"] = "structurally_contested"

    edges.sort(key=lambda row: row["relation_edge_id"])
    issues.sort(key=lambda row: (row["contested_rule"], row["contested_group_id"]))
    return edges, issues


def _normalize_relation_candidate(
    *,
    component_kind: str,
    relation: str,
    relation_note: Any,
    relation_raw: Any,
    relation_status: Any,
    source_entity_id: str,
    target_entity_id: str,
    proposal: Mapping[str, Any],
    decision: Mapping[str, Any],
    chapter_id: str,
) -> dict[str, Any]:
    family = relation
    canonical_relation = relation
    source = source_entity_id
    target = target_entity_id
    if component_kind == "kinship_link":
        if relation == "child_of":
            family, canonical_relation = "parent_child", "parent_of"
            source, target = target_entity_id, source_entity_id
        elif relation in {"parent_of", "mother_of", "father_of"}:
            family, canonical_relation = "parent_child", "parent_of"
        elif relation == "grandchild_of":
            family, canonical_relation = "grandparent_child", "grandparent_of"
            source, target = target_entity_id, source_entity_id
        elif relation == "grandparent_of":
            family, canonical_relation = "grandparent_child", "grandparent_of"
        elif relation in {"sibling_of", "spouse_of", "betrothed_to", "other_kin"}:
            family, canonical_relation = relation, relation
            source, target = sorted((source_entity_id, target_entity_id))
    row = {
        "relation_family": family,
        "relation": canonical_relation,
        "relation_variants": [relation],
        "source_entity_id": source,
        "target_entity_id": target,
        "chapter_id": chapter_id,
        "anchor_block_ids": list(
            proposal.get("anchor_block_ids")
            or decision.get("source_block_ids")
            or []
        ),
        "source_component_id": decision["component_id"],
    }
    is_open_relation = (
        component_kind == "entity_link" and relation == "other_link"
    ) or (
        component_kind == "kinship_link" and relation == "other_kin"
    )
    if is_open_relation or (
        component_kind in {"entity_link", "kinship_link"}
        and relation_status == "quarantined_invalid_enum"
    ):
        row.update(
            {
                "relation_note": deepcopy(relation_note),
                "relation_raw": deepcopy(relation_raw),
                "relation_status": deepcopy(
                    relation_status
                    or (
                        "model_other"
                        if is_open_relation and relation_raw is not None
                        else "quarantined_invalid_enum"
                    )
                ),
            }
        )
    return row


def _edge_group_identity(edge: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        "relation_family": edge["relation_family"],
        "relation": edge["relation"],
        "source_entity_id": edge["source_entity_id"],
        "target_entity_id": edge["target_entity_id"],
        "chapter_id": edge["chapter_id"],
    }
    for key in ("relation_note", "relation_raw", "relation_status"):
        if key in edge:
            identity[key] = deepcopy(edge[key])
    return identity


def _edge_identity(edge: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        **_edge_group_identity(edge),
        "relation_variants": list(edge["relation_variants"]),
        "anchor_block_ids": list(edge["anchor_block_ids"]),
        "source_component_ids": list(edge["source_component_ids"]),
    }
    return identity


def _build_glossary_entries(
    *,
    enrich_artifact: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    block_ids: set[str],
    chapter_id: str,
) -> list[dict[str, Any]]:
    decisions_by_subject = {
        row["subject_ref"]: row
        for row in decisions
        if row["component_kind"] == "glossary_ambiguity"
    }
    rows: list[dict[str, Any]] = []
    for raw in _sequence_of_mappings(enrich_artifact.get("glossary_items"), "glossary items"):
        term_id = _required_string(raw.get("term_observation_id"), "term id")
        decision = decisions_by_subject.get(f"glossary:{term_id}")
        disposition = decision["action"] if decision is not None else "clean_unreviewed"
        if disposition == "reject_proposal":
            continue
        entry = {
            "term_id": "litterm1_" + canonical_hash(
                {"chapter_id": chapter_id, "term_observation_id": term_id}
            )[:20],
            "surface": _required_string(raw.get("surface"), "glossary surface"),
            "contextual_sense": _required_string(
                raw.get("contextual_sense"), "contextual sense"
            ),
            "ambiguity_status": _required_string(
                raw.get("ambiguity_status"), "ambiguity status"
            ),
            "source_block_ids": _block_list(
                raw.get("source_block_ids"), block_ids, "glossary blocks"
            ),
            "semantic_status": (
                "auditor_reviewed"
                if disposition in {"accept_proposal", "revise_proposal"}
                else "unreviewed"
            ),
            "authority_scope": "chapter_context_only",
            "translation_authority_granted": False,
        }
        rows.append(entry)
    rows.sort(key=lambda row: row["term_id"])
    return rows


def _build_dormant_observations(
    *, decisions: Sequence[Mapping[str, Any]], scan_rows: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        if decision["component_kind"] != "spurious_challenge":
            continue
        if decision["action"] != "accept_proposal":
            continue
        scan_id = decision["subject_ref"].removeprefix("scan:")
        scan = scan_rows.get(scan_id)
        if scan is None:
            raise B1ChapterRegistryWriterError("spurious decision cites missing scan row")
        rows.append(
            {
                "observation": deepcopy(dict(scan)),
                "state": "dismissed_dormant",
                "source_component_id": decision["component_id"],
                "resolution_note": decision["resolution_note"],
            }
        )
    return rows


def _project_prior_cards(cards: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for card in cards:
        record_class = card.get("record_class")
        projected_class = (
            "confirmed_entity"
            if card.get("record_state") == "chapter_confirmed"
            else (
                record_class
                if record_class in {"important_unnamed_referent", "unresolved_named_reference"}
                else "unresolved_named_reference"
            )
        )
        first_seen = card["first_seen"]
        rows.append(
            {
                "prior_card_id": card["entity_id"],
                "canonical_surface": card["canonical_surface"],
                "stable_surfaces": list(card["stable_surfaces"]),
                "referent_kind": card["referent_kind"]["value"],
                "identity_summary": card["identity_summary"]["text"]
                or f"Unresolved chapter reference: {card['canonical_surface']}",
                "record_class": projected_class,
                "presence_basis": card["presence_history"][-1]["presence_basis"],
                "claim_state": (
                    "confirmed"
                    if card.get("record_state") == "chapter_confirmed"
                    else "provisional"
                ),
                "first_supported_block_id": first_seen["block_id"],
                "provenance_refs": [
                    {"chapter_id": first_seen["chapter_id"], "block_id": block_id}
                    for block_id in card["support_block_ids"]
                ],
            }
        )
    rows.sort(key=lambda row: row["prior_card_id"])
    return rows


def build_b1_prior_context_cards_v1(
    registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project bounded prior identity evidence for the next chapter.

    The projection preserves the existing surface-retrieval card and adds only
    claims already sealed on that card. It does not infer continuity, merge
    entities, or promote any claim's authority.
    """

    verify_b1_chapter_registry_v1(registry)
    projected = registry.get("prior_cards_projection")
    if not isinstance(projected, Mapping):
        raise B1ChapterRegistryWriterError("prior-card projection is absent")
    base_cards = _sequence_of_mappings(projected.get("cards"), "prior cards")
    full_cards = {
        _required_string(row.get("entity_id"), "entity_id"): row
        for row in _sequence_of_mappings(registry.get("cards"), "registry cards")
    }
    rows: list[dict[str, Any]] = []
    for base in base_cards:
        card_id = _required_string(base.get("prior_card_id"), "prior_card_id")
        full = full_cards.get(card_id)
        if full is None:
            raise B1ChapterRegistryWriterError(
                "prior-card projection cites a missing registry card"
            )
        claims = []
        for claim in _sequence_of_mappings(full.get("claims"), "card claims"):
            claims.append(
                {
                    "field": deepcopy(claim.get("field")),
                    "status": deepcopy(claim.get("status")),
                    "value": deepcopy(claim.get("value")),
                    "basis": deepcopy(claim.get("basis")),
                    "effective": deepcopy(claim.get("effective")),
                    "anchor_block_ids": deepcopy(
                        list(claim.get("anchor_block_ids") or [])
                    ),
                    "story_time_note": deepcopy(claim.get("story_time_note")),
                    "validity": deepcopy(claim.get("validity")),
                    "semantic_status": deepcopy(claim.get("semantic_status")),
                }
            )
        claims.sort(
            key=lambda row: (
                str(row.get("field") or ""),
                canonical_hash(row),
            )
        )
        rows.append(
            {
                **deepcopy(dict(base)),
                "profile_claims": claims,
                "distinguishing_note": deepcopy(full.get("distinguishing_note")),
            }
        )
    rows.sort(key=lambda row: row["prior_card_id"])
    return rows


def _verify_input_lineage(
    *,
    chapter_id: str,
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    audit_artifact: Mapping[str, Any],
) -> None:
    for label, artifact in (
        ("B1-Scan", scan_artifact),
        ("B1-Enrich", enrich_artifact),
        ("Local Auditor", audit_artifact),
    ):
        if artifact.get("chapter_id") != chapter_id:
            raise B1ChapterRegistryWriterError(f"{label} chapter differs")
        _verify_artifact_hash(artifact, label)
    if enrich_artifact.get("scan_artifact_hash") != scan_artifact.get("artifact_hash"):
        raise B1ChapterRegistryWriterError("B1-Enrich lineage differs from B1-Scan")
    if audit_artifact.get("scan_artifact_hash") != scan_artifact.get("artifact_hash"):
        raise B1ChapterRegistryWriterError("Local Auditor scan lineage differs")
    if audit_artifact.get("enrich_artifact_hash") != enrich_artifact.get("artifact_hash"):
        raise B1ChapterRegistryWriterError("Local Auditor enrich lineage differs")


def _validated_decisions(
    audit_artifact: Mapping[str, Any], *, block_ids: set[str]
) -> list[dict[str, Any]]:
    decisions = deepcopy(
        _sequence_of_mappings(audit_artifact.get("decisions"), "audit decisions")
    )
    seen: set[str] = set()
    for row in decisions:
        row.setdefault("revised_relation_note", None)
        component_id = _required_string(row.get("component_id"), "component id")
        if component_id in seen:
            raise B1ChapterRegistryWriterError("audit decisions duplicate a component")
        seen.add(component_id)
        if row.get("action") not in DECISION_ACTIONS:
            raise B1ChapterRegistryWriterError("audit decision uses a foreign action")
        _required_string(row.get("component_kind"), "component kind")
        _required_string(row.get("subject_ref"), "subject ref")
        _required_string(row.get("resolution_note"), "resolution note")
        if not isinstance(row.get("original_proposal"), Mapping):
            raise B1ChapterRegistryWriterError("audit decision lacks original proposal")
        _block_list(row.get("source_block_ids"), block_ids, "audit source blocks")
    partition = {
        "accept_proposal": "accepted_components",
        "revise_proposal": "revised_components",
        "reject_proposal": "rejected_components",
        "keep_pending": "pending_components",
        "refer_cross_chapter": "cross_chapter_referrals",
    }
    for action, key in partition.items():
        expected = sorted(
            canonical_hash(_decision_projection(row))
            for row in decisions
            if row["action"] == action
        )
        actual = sorted(
            canonical_hash(_decision_projection(row))
            for row in _sequence_of_mappings(audit_artifact.get(key), key)
        )
        if expected != actual:
            raise B1ChapterRegistryWriterError(f"audit {key} partition differs")
    return decisions


def _decision_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "component_id",
        "component_kind",
        "subject_ref",
        "action",
        "revised_relation",
        "revised_target_ref",
        "source_block_ids",
        "resolution_note",
        "original_proposal",
    )
    if any(key not in row for key in keys):
        raise B1ChapterRegistryWriterError("audit partition row lacks decision fields")
    return {
        **{key: deepcopy(row[key]) for key in keys},
        "revised_relation_note": deepcopy(row.get("revised_relation_note")),
    }


def _accepted_spurious_scan_ids(decisions: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        row["subject_ref"].removeprefix("scan:")
        for row in decisions
        if row["component_kind"] == "spurious_challenge"
        and row["action"] == "accept_proposal"
    }


def _lookup_decision(
    decision_index: Mapping[str, Mapping[str, Any]],
    *,
    kind: str,
    subject_ref: str,
    proposal: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    return decision_index.get(_decision_key(kind, subject_ref, proposal))


def _decision_key(kind: str, subject_ref: str, proposal: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            "component_kind": kind,
            "subject_ref": subject_ref,
            "original_proposal": proposal,
        }
    )


def _effective_claim(claim: Mapping[str, Any]) -> bool:
    semantic_status = claim.get("semantic_status")
    if semantic_status in NON_EFFECTIVE_STATUSES:
        return False
    return claim.get("basis") in EFFECTIVE_BASES or semantic_status == "auditor_reviewed"


def _mint_entity_id(
    *, chapter_id: str, source_ref: str, surface: str, source_block_ids: Sequence[str]
) -> str:
    identity = {
        "chapter_id": chapter_id,
        "source_ref": source_ref,
        "surface": surface,
        "source_block_ids": list(source_block_ids),
    }
    return "b0ent_" + canonical_hash(identity)[:20]


def _chapter_identity(
    chapter: Mapping[str, Any],
) -> tuple[str, dict[str, int], set[str], str]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    raw_blocks = chapter.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise B1ChapterRegistryWriterError("chapter blocks are absent")
    projected: list[dict[str, Any]] = []
    block_order: dict[str, int] = {}
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, Mapping):
            raise B1ChapterRegistryWriterError("chapter block is malformed")
        block_id = _required_string(raw.get("block_id"), "block_id")
        if block_id in block_order:
            raise B1ChapterRegistryWriterError("chapter block ids are duplicated")
        order = raw.get("order_index")
        if not isinstance(order, int):
            order = index
        block_order[block_id] = order
        projected.append(
            {
                "block_id": block_id,
                "order_index": order,
                "text": str(raw.get("clean_text") or raw.get("text") or ""),
            }
        )
    return chapter_id, block_order, set(block_order), canonical_hash(
        {"chapter_id": chapter_id, "blocks": projected}
    )


def _verify_artifact_hash(artifact: Mapping[str, Any], label: str) -> None:
    actual = artifact.get("artifact_hash")
    if not isinstance(actual, str):
        raise B1ChapterRegistryWriterError(f"{label} artifact hash is absent")
    body = dict(artifact)
    body.pop("artifact_hash", None)
    if canonical_hash(body) != actual:
        raise B1ChapterRegistryWriterError(f"{label} artifact hash mismatch")


def _mapping_by(value: Any, *, key: str, label: str) -> dict[str, Mapping[str, Any]]:
    rows = _sequence_of_mappings(value, label)
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        identity = _required_string(row.get(key), f"{label} {key}")
        if identity in result:
            raise B1ChapterRegistryWriterError(f"{label} duplicates {key}")
        result[identity] = row
    return result


def _continued_prior_identity_by_scan(
    enrich_artifact: Mapping[str, Any],
    *,
    excluded_case_ids: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    result: dict[str, str] = {}
    case_id_by_prior: dict[str, str] = {}
    seen_prior_ids: set[str] = set()
    excluded = excluded_case_ids or set()
    for row in _sequence_of_mappings(
        enrich_artifact.get("continuity_cases") or [], "continuity cases"
    ):
        if row.get("packet_action") != "include_prior_card":
            continue
        if row.get("hearing_required") is not False:
            raise B1ChapterRegistryWriterError(
                "continued identity still requires a hearing"
            )
        scan_ids = _string_values(
            row.get("current_scan_observation_ids"),
            "current_scan_observation_ids",
        )
        prior_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        case_id = _required_string(
            row.get("continuity_case_id"), "continuity_case_id"
        )
        if case_id in excluded:
            continue
        if prior_id in seen_prior_ids:
            raise B1ChapterRegistryWriterError(
                "continued identity duplicates a prior-card case"
            )
        seen_prior_ids.add(prior_id)
        case_id_by_prior[prior_id] = case_id
        prior_snapshot = _mapping(row.get("prior_card_snapshot"), "prior card snapshot")
        if prior_snapshot.get("prior_card_id") != prior_id:
            raise B1ChapterRegistryWriterError(
                "continued prior-card snapshot identity differs"
            )
        for scan_id in scan_ids:
            if scan_id in result:
                raise B1ChapterRegistryWriterError(
                    "continued identity maps one scan observation more than once"
                )
            result[scan_id] = prior_id
    return result, case_id_by_prior


def _conflicting_direct_continuity_case_ids(
    enrich_artifact: Mapping[str, Any],
) -> set[str]:
    """Return direct-continuation cases that cannot all own one observation."""

    rows_by_scan: dict[str, list[tuple[str, str]]] = {}
    for row in _sequence_of_mappings(
        enrich_artifact.get("continuity_cases") or [], "continuity cases"
    ):
        if (
            row.get("packet_action") != "include_prior_card"
            or row.get("hearing_required") is not False
        ):
            continue
        case_id = _required_string(
            row.get("continuity_case_id"), "continuity_case_id"
        )
        prior_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        for scan_id in _string_values(
            row.get("current_scan_observation_ids"),
            "current_scan_observation_ids",
        ):
            rows_by_scan.setdefault(scan_id, []).append((case_id, prior_id))

    return {
        case_id
        for rows in rows_by_scan.values()
        if len({prior_id for _case_id, prior_id in rows}) > 1
        for case_id, _prior_id in rows
    }


def _cross_prior_same_referent_case_ids(
    enrich_artifact: Mapping[str, Any],
    *,
    decisions: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Route local merges spanning distinct prior identities to a hearing."""

    continuity_rows: list[tuple[str, str, list[str]]] = []
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        keep, absorb = sorted((left_root, right_root))
        parent[absorb] = keep

    for row in _sequence_of_mappings(
        enrich_artifact.get("continuity_cases") or [], "continuity cases"
    ):
        if (
            row.get("packet_action") != "include_prior_card"
            or row.get("hearing_required") is not False
        ):
            continue
        case_id = _required_string(
            row.get("continuity_case_id"), "continuity_case_id"
        )
        prior_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        scan_ids = _string_values(
            row.get("current_scan_observation_ids"),
            "current_scan_observation_ids",
        )
        for scan_id in scan_ids:
            parent.setdefault(scan_id, scan_id)
        for scan_id in scan_ids[1:]:
            union(scan_ids[0], scan_id)
        continuity_rows.append((case_id, prior_id, scan_ids))

    for decision in decisions:
        if (
            decision.get("component_kind") != "same_referent_proposal"
            or decision.get("action") != "accept_proposal"
        ):
            continue
        subject_ref = _required_string(decision.get("subject_ref"), "subject ref")
        proposal = _mapping(
            decision.get("original_proposal"), "same-referent proposal"
        )
        target_ref = _required_string(proposal.get("target_ref"), "target ref")
        if not (
            subject_ref.startswith("scan:") and target_ref.startswith("scan:")
        ):
            continue
        subject_scan_id = subject_ref.removeprefix("scan:")
        target_scan_id = target_ref.removeprefix("scan:")
        if subject_scan_id in parent and target_scan_id in parent:
            union(subject_scan_id, target_scan_id)

    rows_by_root: dict[str, list[tuple[str, str]]] = {}
    for case_id, prior_id, scan_ids in continuity_rows:
        roots = {find(scan_id) for scan_id in scan_ids}
        if len(roots) != 1:
            raise B1ChapterRegistryWriterError(
                "continued identity spans disconnected scan observations"
            )
        rows_by_root.setdefault(next(iter(roots)), []).append((case_id, prior_id))

    return {
        case_id
        for rows in rows_by_root.values()
        if len({prior_id for _case_id, prior_id in rows}) > 1
        for case_id, _prior_id in rows
    }


def _sequence_of_mappings(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise B1ChapterRegistryWriterError(f"{label} must be a list")
    if any(not isinstance(row, Mapping) for row in value):
        raise B1ChapterRegistryWriterError(f"{label} contains a malformed row")
    return list(value)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise B1ChapterRegistryWriterError(f"{label} must be an object")
    return value


def _dossier_evidence_block_ids(dossier: Mapping[str, Any]) -> list[str]:
    block_ids = {
        row
        for row in dossier.get("source_block_ids") or []
        if isinstance(row, str) and row
    }
    for collection in (
        "claims",
        "kinship_links",
        "links",
        "address_forms_used",
        "aliases_observed",
    ):
        for row in dossier.get(collection) or []:
            if not isinstance(row, Mapping):
                continue
            for block_id in row.get("anchor_block_ids") or []:
                if isinstance(block_id, str) and block_id:
                    block_ids.add(block_id)
    return sorted(block_ids)


def _string_values(
    value: Any, label: str, *, allow_empty: bool = False
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty" if allow_empty else "non-empty"
        raise B1ChapterRegistryWriterError(
            f"{label} must be a {qualifier} string list"
        )
    rows = [_required_string(row, label) for row in value]
    if len(rows) != len(set(rows)):
        raise B1ChapterRegistryWriterError(f"{label} contains duplicates")
    return rows


def _block_list(value: Any, known: set[str], label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise B1ChapterRegistryWriterError(f"{label} must be a non-empty list")
    rows = [_required_string(row, label) for row in value]
    if len(rows) != len(set(rows)):
        raise B1ChapterRegistryWriterError(f"{label} contains duplicates")
    if any(row not in known for row in rows):
        raise B1ChapterRegistryWriterError(f"{label} cites a foreign block")
    return rows


def _claim_block_list(
    claim: Mapping[str, Any], known: set[str], label: str
) -> list[str]:
    status = _required_string(claim.get("status"), "claim status")
    if status == "not_applicable":
        rows = _string_values(
            claim.get("anchor_block_ids"), label, allow_empty=True
        )
        if rows:
            raise B1ChapterRegistryWriterError(
                "not_applicable claim anchor_block_ids must be empty"
            )
        return []
    if status not in {"supported", "unclear"}:
        raise B1ChapterRegistryWriterError("claim uses a foreign status")
    return _block_list(claim.get("anchor_block_ids"), known, label)


def _first_block(blocks: Iterable[str], block_order: Mapping[str, int]) -> str:
    rows = list(blocks)
    if not rows:
        raise B1ChapterRegistryWriterError("cannot select first block from empty evidence")
    return min(rows, key=block_order.__getitem__)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B1ChapterRegistryWriterError(f"{label} must be a non-empty string")
    return value.strip()


__all__ = [
    "B1ChapterRegistryWriterError",
    "CROSS_CHAPTER_QUEUE_SCHEMA_VERSION",
    "PRIOR_CARDS_SCHEMA_VERSION",
    "REGISTRY_SCHEMA_VERSION",
    "bind_b2_review_routing_to_hearing_queue_v1",
    "build_b1_cross_chapter_hearing_queue_v1",
    "build_b1_prior_context_cards_v1",
    "seal_b1_chapter_registry_v1",
    "verify_b1_cross_chapter_hearing_queue_v1",
    "verify_b1_chapter_registry_v1",
]
