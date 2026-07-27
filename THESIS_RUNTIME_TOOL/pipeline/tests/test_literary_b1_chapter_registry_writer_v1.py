from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    B1ChapterRegistryWriterError,
    _build_relation_edges,
    _mark_structurally_impossible_kinship_v1,
    bind_b2_review_routing_to_hearing_queue_v1,
    build_b1_cross_chapter_hearing_queue_v1,
    build_b1_prior_context_cards_v1,
    seal_b1_chapter_registry_v1,
    verify_b1_cross_chapter_hearing_queue_v1,
    verify_b1_chapter_registry_v1,
    _normalize_relation_candidate,
    _within_chapter_same_referent_groups,
)
from pipeline.literary.b2_review_resolution_v1 import (
    build_review_routing_plan_from_artifacts_v1,
)
from pipeline.literary.b1_enrich_local_auditor_v1 import (
    OUTPUT_SCHEMA_ID,
    build_b1_enrich_local_audit_manifest_v1,
    validate_b1_enrich_local_audit_response_v1,
)
from pipeline.literary.b1_scan_v1 import build_prior_candidate_packets_v1
from pipeline.literary.checkpoint import canonical_hash


def _chapter():
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "order_index": 1,
                "clean_text": "Mara Vale entered North House.",
            },
            {
                "block_id": "bk_ch01_b002",
                "order_index": 2,
                "clean_text": "North House is my own, said Mara Vale.",
            },
            {
                "block_id": "bk_ch01_b003",
                "order_index": 3,
                "clean_text": "Her mother warned Mara Vale.",
            },
            {
                "block_id": "bk_ch01_b004",
                "order_index": 4,
                "clean_text": "The lintel bore 1500.",
            },
        ],
    }


def _claim(field, value, basis="explicit_textual", block="bk_ch01_b001"):
    return {
        "field": field,
        "status": "supported",
        "value": value,
        "basis": basis,
        "anchor_block_ids": [block],
        "story_time_note": None,
        "semantic_status": "unreviewed",
    }


def _not_applicable_claim(field):
    return {
        "field": field,
        "status": "not_applicable",
        "value": None,
        "basis": "not_applicable",
        "anchor_block_ids": [],
        "story_time_note": None,
        "semantic_status": "unreviewed",
    }


def _dossier(scan_id, surface, kind, claims):
    return {
        "scan_observation_id": scan_id,
        "task_ref": f"scan:{scan_id}",
        "surface": surface,
        "referent_kind_claim": kind,
        "claims": claims,
        "kinship_links": [],
        "links": [],
        "address_forms_used": [],
        "aliases_observed": [],
        "identity_summary": f"A concise dossier for {surface}.",
        "distinguishing_note": None,
        "authority_scope": "chapter_provisional",
    }


def _with_hash(body):
    return {**body, "artifact_hash": canonical_hash(body)}


def _rehash_artifact(artifact):
    body = deepcopy(artifact)
    body.pop("artifact_hash", None)
    return _with_hash(body)


def _scan():
    body = {
        "schema_version": "literary_b1_scan_artifact_v1",
        "chapter_id": "bk_ch01",
        "request_fingerprint": "s" * 64,
        "entity_observations": [
            {
                "observation_id": "b1obs_mara",
                "surface": "Mara Vale",
                "source_block_ids": ["bk_ch01_b001", "bk_ch01_b003"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named participant.",
                "authority_scope": "chapter_provisional",
            },
            {
                "observation_id": "b1obs_house",
                "surface": "North House",
                "source_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
                "referent_kind_claim": "place",
                "record_class": "named_entity_candidate",
                "presence_basis": "referenced_by_other",
                "scan_note": "Named place.",
                "authority_scope": "chapter_provisional",
            },
            {
                "observation_id": "b1obs_mother",
                "surface": "mother",
                "source_block_ids": ["bk_ch01_b003"],
                "referent_kind_claim": "person",
                "record_class": "important_unnamed_referent",
                "presence_basis": "reported_only",
                "scan_note": "Individualized reported parent.",
                "authority_scope": "chapter_provisional",
            },
            {
                "observation_id": "b1obs_date",
                "surface": "1500",
                "source_block_ids": ["bk_ch01_b004"],
                "referent_kind_claim": "unknown",
                "record_class": "unresolved_named_reference",
                "presence_basis": "inscription_or_document",
                "scan_note": "Bare carved date.",
                "authority_scope": "chapter_provisional",
            },
        ],
        "glossary_observations": [],
        "continuity_routes": [],
        "review_issues": [],
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "metrics": {},
    }
    return _with_hash(body)


def _enrich(scan):
    mara = _dossier(
        "b1obs_mara",
        "Mara Vale",
        "person",
        [
            _claim("gender", "feminine"),
            _claim("life_stage", "adult", "contextual_inference"),
        ],
    )
    mara["links"] = [
        {
            "relation": "resides_at",
            "target_ref": "scan:b1obs_house",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b001"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        }
    ]
    mara["kinship_links"] = [
        {
            "relation": "child_of",
            "target_ref": "scan:b1obs_mother",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b003"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
            "relation_note": None,
        }
    ]
    mara["aliases_observed"] = [
        {
            "surface": "M. Vale",
            "anchor_block_ids": ["bk_ch01_b002"],
            "status": "proposed",
        }
    ]
    house = _dossier(
        "b1obs_house",
        "North House",
        "place",
        [_claim("place_type", "dwelling", block="bk_ch01_b002")],
    )
    house["links"] = [
        {
            "relation": "resides_at",
            "target_ref": "scan:b1obs_mara",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b001"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        }
    ]
    mother = _dossier(
        "b1obs_mother",
        "mother",
        "person",
        [_claim("gender", "feminine", block="bk_ch01_b003")],
    )
    mother["kinship_links"] = [
        {
            "relation": "mother_of",
            "target_ref": "scan:b1obs_mara",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b003"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
            "relation_note": None,
        }
    ]
    body = {
        "schema_version": "literary_b1_enrich_artifact_v1",
        "chapter_id": "bk_ch01",
        "request_fingerprint": "e" * 64,
        "scan_artifact_hash": scan["artifact_hash"],
        "entity_dossiers": [mara, house, mother],
        "additional_entity_dossiers": [],
        "spurious_challenges": [
            {
                "scan_observation_id": "b1obs_date",
                "reason": "A bare date is not a persistent referent.",
                "source_block_ids": ["bk_ch01_b004"],
                "requires_local_auditor": True,
            }
        ],
        "same_referent_proposals": [],
        "conflict_findings": [],
        "presence_correction_findings": [],
        "glossary_items": [],
        "quarantined_tasks": [],
        "review_issues": [],
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "metrics": {},
    }
    return _with_hash(body)


def _audit(chapter, scan, enrich):
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter, scan_artifact=scan, enrich_artifact=enrich
    )
    decisions = []
    for component in manifest["components"]:
        action = "accept_proposal"
        if (
            component["component_kind"] == "entity_link"
            and component["subject_ref"] == "scan:b1obs_house"
        ):
            action = "reject_proposal"
        decisions.append(
            {
                "component_id": component["component_id"],
                "action": action,
                "revised_relation": None,
                "revised_relation_note": None,
                "revised_target_ref": None,
                "source_block_ids": [component["direct_source_block_ids"][0]],
                "resolution_note": "The direct chapter evidence supports this disposition.",
            }
        )
    response = {
        "schema_id": OUTPUT_SCHEMA_ID,
        "chapter_id": "bk_ch01",
        "manifest_hash": manifest["manifest_hash"],
        "decisions": decisions,
        "unasked_same_referent_observations": [],
    }
    return validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        request_fingerprint="a" * 64,
    )


def test_writer_reuses_only_confirmed_continuity_and_queues_pending_identity() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    enrich["continuity_cases"] = [
        {
            "continuity_case_id": "b1cont_mara",
            "chapter_id": "bk_ch01",
            "scan_artifact_hash": scan["artifact_hash"],
            "prior_card_id": "b0ent_prior_mara",
            "current_scan_observation_ids": ["b1obs_mara"],
            "scan_verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "Current evidence supports the supplied continuity candidate.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "prior_card_snapshot": {
                "prior_card_id": "b0ent_prior_mara",
                "canonical_surface": "Mara Vale",
                "stable_surfaces": ["Mara Vale"],
            },
            "identity_authority_granted": False,
            "evidence_manifest_hash": "a" * 64,
        },
        {
            "continuity_case_id": "b1cont_house",
            "chapter_id": "bk_ch01",
            "scan_artifact_hash": scan["artifact_hash"],
            "prior_card_id": "b0ent_prior_house",
            "current_scan_observation_ids": ["b1obs_house"],
            "scan_verdict": "uncertain",
            "reason_code": "prior_reference_not_established_entity",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The prior occurrence is not established strongly enough.",
            "packet_action": "withhold_prior_card",
            "hearing_required": True,
            "mechanical_risk_codes": ["prior_claim_state_is_not_confirmed"],
            "prior_card_snapshot": {
                "prior_card_id": "b0ent_prior_house",
                "canonical_surface": "North House",
                "stable_surfaces": ["North House"],
            },
            "identity_authority_granted": False,
            "evidence_manifest_hash": "b" * 64,
        },
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    by_surface = {row["canonical_surface"]: row for row in registry["cards"]}
    assert by_surface["Mara Vale"]["entity_id"] == "b0ent_prior_mara"
    assert by_surface["North House"]["entity_id"] != "b0ent_prior_house"

    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    assert len(queue["components"]) == 1
    component = queue["components"][0]
    assert component["question_type"] == "identity_linkage"
    assert component["prior_card_ids"] == ["b0ent_prior_house"]
    assert component["current_entity_ids"] == [by_surface["North House"]["entity_id"]]
    assert component["lifecycle_state"] == "ready_for_hearing"
    assert component["identity_authority_granted"] is False


def test_writer_groups_multiple_observations_confirmed_for_one_prior_card() -> None:
    groups, accepted = _within_chapter_same_referent_groups(
        source_refs=["scan:b1obs_a", "scan:b1obs_b", "scan:b1obs_other"],
        decisions=[],
        continued_prior_id_by_scan={
            "b1obs_a": "b0ent_prior",
            "b1obs_b": "b0ent_prior",
            "b1obs_other": "b0ent_other",
        },
    )

    assert groups == [
        ["scan:b1obs_a", "scan:b1obs_b"],
        ["scan:b1obs_other"],
    ]
    assert accepted == []


def test_writer_routes_competing_direct_continuations_to_one_hearing() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    enrich["continuity_cases"] = [
        {
            "continuity_case_id": f"b1cont_mara_{suffix}",
            "chapter_id": "bk_ch01",
            "scan_artifact_hash": scan["artifact_hash"],
            "prior_card_id": prior_id,
            "current_scan_observation_ids": ["b1obs_mara"],
            "scan_verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The model proposed this supplied continuity candidate.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [
                "same_surface_matches_multiple_prior_cards"
            ],
            "prior_card_snapshot": {
                "prior_card_id": prior_id,
                "canonical_surface": "Mara Vale",
                "stable_surfaces": ["Mara Vale"],
            },
            "identity_authority_granted": False,
            "evidence_manifest_hash": suffix * 64,
        }
        for suffix, prior_id in (
            ("a", "b0ent_prior_mara_a"),
            ("b", "b0ent_prior_mara_b"),
        )
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    mara = next(
        row for row in registry["cards"] if row["canonical_surface"] == "Mara Vale"
    )
    assert mara["entity_id"] not in {
        "b0ent_prior_mara_a",
        "b0ent_prior_mara_b",
    }

    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    assert len(queue["components"]) == 1
    component = queue["components"][0]
    assert component["prior_card_ids"] == [
        "b0ent_prior_mara_a",
        "b0ent_prior_mara_b",
    ]
    assert component["current_entity_ids"] == [mara["entity_id"]]
    assert component["lifecycle_state"] == "ready_for_hearing"
    assert all(
        "multiple_direct_continuations_for_one_observation"
        in context["trigger"]["mechanical_risk_codes"]
        for context in component["candidate_contexts"]
    )


def test_writer_routes_local_merge_spanning_distinct_prior_ids_to_hearing() -> None:
    chapter = _chapter()
    scan = _scan()
    scan["entity_observations"].append(
        {
            "observation_id": "b1obs_mara_short",
            "surface": "Mara",
            "source_block_ids": ["bk_ch01_b002"],
            "referent_kind_claim": "person",
            "record_class": "named_entity_candidate",
            "presence_basis": "direct_presence",
            "scan_note": "Short named surface.",
            "authority_scope": "chapter_provisional",
        }
    )
    scan = _rehash_artifact(scan)
    enrich = _enrich(scan)
    enrich["entity_dossiers"].append(
        _dossier(
            "b1obs_mara_short",
            "Mara",
            "person",
            [
                _claim("gender", "feminine", block="bk_ch01_b002"),
                _claim("life_stage", "adult", block="bk_ch01_b002"),
            ],
        )
    )
    enrich["continuity_cases"] = [
        {
            "continuity_case_id": f"b1cont_mara_{suffix}",
            "chapter_id": "bk_ch01",
            "scan_artifact_hash": scan["artifact_hash"],
            "prior_card_id": prior_id,
            "current_scan_observation_ids": [scan_id],
            "scan_verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": [block_id],
            "reason": "The model proposed this supplied continuity candidate.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "prior_card_snapshot": {
                "prior_card_id": prior_id,
                "canonical_surface": surface,
                "stable_surfaces": [surface],
            },
            "identity_authority_granted": False,
            "evidence_manifest_hash": suffix * 64,
        }
        for suffix, prior_id, scan_id, surface, block_id in (
            (
                "a",
                "b0ent_prior_mara_full",
                "b1obs_mara",
                "Mara Vale",
                "bk_ch01_b001",
            ),
            (
                "b",
                "b0ent_prior_mara_short",
                "b1obs_mara_short",
                "Mara",
                "bk_ch01_b002",
            ),
        )
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    mara = next(
        row
        for row in registry["cards"]
        if set(row["source_refs"]) == {
            "scan:b1obs_mara",
            "scan:b1obs_mara_short",
        }
    )
    assert mara["entity_id"] not in {
        "b0ent_prior_mara_full",
        "b0ent_prior_mara_short",
    }

    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    assert len(queue["components"]) == 1
    component = queue["components"][0]
    assert component["prior_card_ids"] == [
        "b0ent_prior_mara_full",
        "b0ent_prior_mara_short",
    ]
    assert component["current_entity_ids"] == [mara["entity_id"]]
    assert component["lifecycle_state"] == "ready_for_hearing"
    assert all(
        "local_same_referent_spans_distinct_prior_entities"
        in context["trigger"]["mechanical_risk_codes"]
        for context in component["candidate_contexts"]
    )


def test_writer_carries_referenced_prior_without_scan_observation_or_hearing() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    enrich["continuity_cases"] = [
        {
            "continuity_case_id": "b1cont_grange",
            "chapter_id": "bk_ch01",
            "scan_artifact_hash": scan["artifact_hash"],
            "prior_card_id": "b0ent_prior_grange",
            "current_scan_observation_ids": [],
            "scan_verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b002"],
            "reason": "The current chapter references the supplied place.",
            "packet_action": "carry_referenced_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [
                "no_exact_current_scan_observation",
                "referenced_prior_without_current_observation",
            ],
            "prior_card_snapshot": {
                "prior_card_id": "b0ent_prior_grange",
                "canonical_surface": "South Grange",
                "stable_surfaces": ["South Grange"],
                "referent_kind": "place",
                "identity_summary": "South Grange is a named dwelling.",
                "record_class": "confirmed_entity",
                "claim_state": "confirmed",
                "profile_claims": [
                    {
                        "field": "place_type",
                        "status": "supported",
                        "value": "dwelling",
                        "basis": "explicit_textual",
                        "effective": True,
                        "anchor_block_ids": ["bk_ch00_b001"],
                        "story_time_note": None,
                        "validity": {"from_block": None, "to_block": None},
                        "semantic_status": "unreviewed",
                    }
                ],
                "distinguishing_note": None,
            },
            "identity_authority_granted": False,
            "evidence_manifest_hash": "c" * 64,
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    carried = next(
        row for row in registry["cards"] if row["entity_id"] == "b0ent_prior_grange"
    )
    assert carried["canonical_surface"] == "South Grange"
    assert carried["record_state"] == "chapter_confirmed"
    assert carried["support_block_ids"] == ["bk_ch01_b002"]
    assert carried["presence_history"] == [
        {
            "chapter_id": "bk_ch01",
            "presence_basis": "referenced_by_other",
            "semantic_status": "referenced_prior_carry",
            "source_block_ids": ["bk_ch01_b002"],
        }
    ]
    assert carried["claims"][0]["effective"] is True
    assert registry["metrics"]["referenced_prior_carry_count"] == 1
    assert not any(
        row.get("prior_card_id") == "b0ent_prior_grange"
        for row in registry["pending_reviews"]
    )
    projected = next(
        row
        for row in build_b1_prior_context_cards_v1(registry)
        if row["prior_card_id"] == "b0ent_prior_grange"
    )
    assert projected["claim_state"] == "confirmed"
    assert projected["profile_claims"][0]["field"] == "place_type"


def test_writer_rejects_legacy_include_without_current_observation() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    enrich["continuity_cases"] = [
        {
            "continuity_case_id": "b1cont_invalid",
            "prior_card_id": "b0ent_prior_grange",
            "current_scan_observation_ids": [],
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "prior_card_snapshot": {
                "prior_card_id": "b0ent_prior_grange",
            },
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    with pytest.raises(
        B1ChapterRegistryWriterError,
        match="current_scan_observation_ids must be a non-empty string list",
    ):
        seal_b1_chapter_registry_v1(
            chapter=chapter,
            scan_artifact=scan,
            enrich_artifact=enrich,
            audit_artifact=audit,
        )


def test_writer_merges_only_accepted_within_chapter_identity_components() -> None:
    chapter = _chapter()
    scan = _scan()
    scan["entity_observations"].extend(
        [
            {
                "observation_id": "b1obs_mara_short",
                "surface": "Mara",
                "source_block_ids": ["bk_ch01_b002"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Short named surface.",
                "authority_scope": "chapter_provisional",
            },
            {
                "observation_id": "b1obs_owner",
                "surface": "the owner",
                "source_block_ids": ["bk_ch01_b002"],
                "referent_kind_claim": "person",
                "record_class": "important_unnamed_referent",
                "presence_basis": "direct_presence",
                "scan_note": "Chapter-local description.",
                "authority_scope": "chapter_provisional",
            },
        ]
    )
    scan = _rehash_artifact(scan)
    enrich = _enrich(scan)
    enrich["entity_dossiers"].extend(
        [
            _dossier(
                "b1obs_mara_short",
                "Mara",
                "person",
                [
                    _claim("gender", "feminine", block="bk_ch01_b002"),
                    _claim("life_stage", "adult", block="bk_ch01_b002"),
                ],
            ),
            _dossier(
                "b1obs_owner",
                "the owner",
                "person",
                [
                    _claim("gender", "feminine", block="bk_ch01_b002"),
                    _claim("life_stage", "adult", block="bk_ch01_b002"),
                ],
            ),
        ]
    )
    enrich["same_referent_proposals"] = [
        {
            "subject_ref": "scan:b1obs_owner",
            "target_ref": "scan:b1obs_mara",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            "reason": "The chapter identifies the described owner as Mara Vale.",
            "retrieval_surface_policy": "subject_evidence_only",
            "requires_local_auditor": True,
            "identity_authority_granted": False,
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    mara = next(row for row in registry["cards"] if row["canonical_surface"] == "Mara Vale")
    assert mara["stable_surfaces"] == ["Mara Vale", "Mara"]
    assert "the owner" not in mara["stable_surfaces"]
    assert set(mara["source_refs"]) == {
        "scan:b1obs_mara",
        "scan:b1obs_mara_short",
        "scan:b1obs_owner",
    }
    evidence_by_surface = {
        row["surface"]: row for row in mara["merged_observation_evidence"]
    }
    assert evidence_by_surface["Mara"]["retrieval_surface_authority"] == (
        "stable_name_variant"
    )
    assert evidence_by_surface["the owner"]["retrieval_surface_authority"] == (
        "evidence_only"
    )
    assert registry["metrics"]["within_chapter_merge_count"] == 1
    assert registry["metrics"]["absorbed_card_count"] == 2
    verify_b1_chapter_registry_v1(registry)


def test_writer_projects_multiple_merges_in_final_card_order() -> None:
    chapter = _chapter()
    scan = _scan()
    house = next(
        row
        for row in scan["entity_observations"]
        if row["observation_id"] == "b1obs_house"
    )
    house["source_block_ids"] = ["bk_ch01_b002"]
    scan["entity_observations"].extend(
        [
            {
                "observation_id": "b1obs_z_owner",
                "surface": "the owner",
                "source_block_ids": ["bk_ch01_b001"],
                "referent_kind_claim": "person",
                "record_class": "important_unnamed_referent",
                "presence_basis": "direct_presence",
                "scan_note": "Chapter-local description.",
                "authority_scope": "chapter_provisional",
            },
            {
                "observation_id": "b1obs_a_house_alias",
                "surface": "the dwelling",
                "source_block_ids": ["bk_ch01_b002"],
                "referent_kind_claim": "place",
                "record_class": "important_unnamed_referent",
                "presence_basis": "referenced_by_other",
                "scan_note": "Chapter-local place description.",
                "authority_scope": "chapter_provisional",
            },
        ]
    )
    scan = _rehash_artifact(scan)
    enrich = _enrich(scan)
    enrich["entity_dossiers"].extend(
        [
            _dossier(
                "b1obs_z_owner",
                "the owner",
                "person",
                [
                    _claim("gender", "feminine"),
                    _claim("life_stage", "adult"),
                ],
            ),
            _dossier(
                "b1obs_a_house_alias",
                "the dwelling",
                "place",
                [_claim("place_type", "dwelling", block="bk_ch01_b002")],
            ),
        ]
    )
    enrich["same_referent_proposals"] = [
        {
            "subject_ref": "scan:b1obs_z_owner",
            "target_ref": "scan:b1obs_mara",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The described owner is Mara Vale.",
            "retrieval_surface_policy": "subject_evidence_only",
            "requires_local_auditor": True,
            "identity_authority_granted": False,
        },
        {
            "subject_ref": "scan:b1obs_a_house_alias",
            "target_ref": "scan:b1obs_house",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b002"],
            "reason": "The dwelling is North House.",
            "retrieval_surface_policy": "subject_evidence_only",
            "requires_local_auditor": True,
            "identity_authority_granted": False,
        },
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    projected = [
        card["within_chapter_identity_merge"]
        for card in registry["cards"]
        if card.get("within_chapter_identity_merge") is not None
    ]
    assert len(projected) == 2
    assert registry["within_chapter_identity_merges"] == projected
    verify_b1_chapter_registry_v1(registry)


def test_evidence_only_duplicate_of_canonical_name_does_not_fail_merge_guard() -> None:
    chapter = _chapter()
    scan = _scan()
    scan["entity_observations"].append(
        {
            "observation_id": "b1obs_mara_written",
            "surface": "Mara Vale",
            "source_block_ids": ["bk_ch01_b002"],
            "referent_kind_claim": "unknown",
            "record_class": "unresolved_named_reference",
            "presence_basis": "inscription_or_document",
            "scan_note": "The same name appears in a written record.",
            "authority_scope": "chapter_provisional",
        }
    )
    scan = _rehash_artifact(scan)
    enrich = _enrich(scan)
    enrich["entity_dossiers"].append(
        _dossier(
            "b1obs_mara_written",
            "Mara Vale",
            "unknown",
            [_claim("referent_kind", "unclear", block="bk_ch01_b002")],
        )
    )
    enrich["same_referent_proposals"] = [
        {
            "subject_ref": "scan:b1obs_mara_written",
            "target_ref": "scan:b1obs_mara",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b002"],
            "reason": "The written name refers to the named participant in this chapter.",
            "retrieval_surface_policy": "subject_evidence_only",
            "requires_local_auditor": True,
            "identity_authority_granted": False,
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    mara = next(row for row in registry["cards"] if row["canonical_surface"] == "Mara Vale")
    assert mara["stable_surfaces"] == ["Mara Vale"]
    duplicate = next(
        row
        for row in mara["merged_observation_evidence"]
        if row["source_ref"] == "scan:b1obs_mara_written"
    )
    assert duplicate["retrieval_surface_authority"] == "evidence_only"
    verify_b1_chapter_registry_v1(registry)


def test_writer_resolves_refs_after_all_merge_group_ids_are_allocated() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    mara = next(
        row
        for row in enrich["entity_dossiers"]
        if row["scan_observation_id"] == "b1obs_mara"
    )
    mara["address_forms_used"] = [
        {
            "counterpart_ref": "scan:b1obs_mother",
            "mode": "to",
            "form": "mother",
            "anchor_block_ids": ["bk_ch01_b003"],
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    by_surface = {row["canonical_surface"]: row for row in registry["cards"]}
    address_form = by_surface["Mara Vale"]["address_forms_used"][0]
    assert address_form["counterpart_entity_id"] == by_surface["mother"]["entity_id"]


def test_stable_conflict_routes_directly_with_both_profile_snapshots() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    prior_snapshot = {
        "prior_card_id": "b0ent_prior_mara",
        "canonical_surface": "Mara Vale",
        "stable_surfaces": ["Mara Vale"],
        "identity_summary": "Mara was recorded as a tenant.",
    }
    enrich["continuity_cases"] = [
        {
            "continuity_case_id": "b1cont_mara",
            "chapter_id": "bk_ch01",
            "scan_artifact_hash": scan["artifact_hash"],
            "prior_card_id": "b0ent_prior_mara",
            "current_scan_observation_ids": ["b1obs_mara"],
            "scan_verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "Current evidence supports continuity.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "prior_card_snapshot": prior_snapshot,
            "identity_authority_granted": False,
            "evidence_manifest_hash": "a" * 64,
        }
    ]
    enrich["conflict_findings"] = [
        {
            "scan_observation_id": "b1obs_mara",
            "field": "role_or_occupation",
            "existing_value": "tenant",
            "observed_value": "owner",
            "source_block_ids": ["bk_ch01_b002"],
            "reason": "The current chapter explicitly identifies ownership.",
            "prior_card_id": "b0ent_prior_mara",
            "continuity_case_ids": ["b1cont_mara"],
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    stable = next(row for row in queue["components"] if row["question_type"] == "stable_claim")
    assert stable["review_route"] == "stable_claim_auditor"
    assert stable["prior_card_snapshot"] == prior_snapshot
    assert stable["current_card_snapshot"]["entity_id"] == "b0ent_prior_mara"
    assert stable["current_dossier_snapshot"]["scan_observation_id"] == "b1obs_mara"
    assert stable["lifecycle_state"] == "ready_for_hearing"


def test_prior_context_projection_adds_existing_claims_without_new_authority() -> None:
    registry = _sealed()
    cards = build_b1_prior_context_cards_v1(registry)
    mara = next(row for row in cards if row["canonical_surface"] == "Mara Vale")
    assert {row["field"] for row in mara["profile_claims"]} == {
        "gender",
        "life_stage",
    }
    assert any(row["effective"] is False for row in mara["profile_claims"])
    assert mara["claim_state"] == "confirmed"


def test_local_cross_chapter_referral_is_preserved_with_an_explicit_route() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter, scan_artifact=scan, enrich_artifact=enrich
    )
    decisions = []
    for component in manifest["components"]:
        action = (
            "refer_cross_chapter"
            if component["component_kind"] == "alias_proposal"
            else "accept_proposal"
        )
        decisions.append(
            {
                "component_id": component["component_id"],
                "action": action,
                "revised_relation": None,
                "revised_relation_note": None,
                "revised_target_ref": None,
                "source_block_ids": [component["direct_source_block_ids"][0]],
                "resolution_note": "Another chapter is needed for the alias scope.",
            }
        )
    audit = validate_b1_enrich_local_audit_response_v1(
        {
            "schema_id": OUTPUT_SCHEMA_ID,
            "chapter_id": "bk_ch01",
            "manifest_hash": manifest["manifest_hash"],
            "decisions": decisions,
            "unasked_same_referent_observations": [],
        },
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        request_fingerprint="r" * 64,
    )
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    referral = next(
        row
        for row in queue["components"]
        if row["question_type"] == "local_cross_chapter_referral"
    )
    assert referral["component_kind"] == "alias_proposal"
    assert referral["review_route"] == "identity_auditor"
    assert referral["lifecycle_state"] == "ready_for_hearing"
    assert referral["identity_authority_granted"] is False

    tampered = deepcopy(queue)
    tampered["components"][0]["review_route"] = "foreign_auditor"
    with pytest.raises(B1ChapterRegistryWriterError, match="hash mismatch"):
        verify_b1_cross_chapter_hearing_queue_v1(tampered)


def test_additional_entity_referral_carries_its_enriched_dossier() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    enrich["additional_entity_dossiers"] = [
        {
            "additional_entity_id": "b1add_dog",
            "surface": "a brindled dog",
            "source_block_ids": ["bk_ch01_b004"],
            "referent_kind_claim": "animal",
            "claims": [_claim("species", "dog", block="bk_ch01_b004")],
            "kinship_links": [],
            "links": [],
            "address_forms_used": [],
            "aliases_observed": [],
            "identity_summary": "An individualized brindled dog.",
            "distinguishing_note": None,
            "authority_scope": "chapter_provisional",
        }
    ]
    enrich = _rehash_artifact(enrich)
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter, scan_artifact=scan, enrich_artifact=enrich
    )
    decisions = []
    for component in manifest["components"]:
        decisions.append(
            {
                "component_id": component["component_id"],
                "action": (
                    "refer_cross_chapter"
                    if component["component_kind"] == "additional_entity"
                    else "accept_proposal"
                ),
                "revised_relation": None,
                "revised_relation_note": None,
                "revised_target_ref": None,
                "source_block_ids": [component["direct_source_block_ids"][0]],
                "resolution_note": "Preserve the current dossier for identity review.",
            }
        )
    audit = validate_b1_enrich_local_audit_response_v1(
        {
            "schema_id": OUTPUT_SCHEMA_ID,
            "chapter_id": "bk_ch01",
            "manifest_hash": manifest["manifest_hash"],
            "decisions": decisions,
            "unasked_same_referent_observations": [],
        },
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        request_fingerprint="r" * 64,
    )
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    referral = next(
        row
        for row in queue["components"]
        if row["subject_ref"] == "additional:b1add_dog"
    )
    assert referral["review_route"] == "identity_auditor"
    assert referral["lifecycle_state"] == "ready_for_hearing"
    assert referral["current_dossier_snapshot"]["additional_entity_id"] == "b1add_dog"


def _sealed():
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    audit = _audit(chapter, scan, enrich)
    return seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )


def test_route_b_within_binding_reuses_open_local_case_without_new_hearing() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    audit = _audit(chapter, scan, enrich)
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    cards = registry["cards"][:2]
    member_refs = [card["source_refs"][0] for card in cards]
    local_audit = _with_hash({
        "chapter_id": "bk_ch01",
        "decisions": [
            {
                "component_id": "b1lac_open_identity",
                "component_kind": "same_referent_proposal",
                "subject_ref": member_refs[0],
                "original_proposal": {"target_ref": member_refs[1]},
                "action": "keep_pending",
            }
        ],
    })
    b2_artifact = _with_hash({
        "chapter_id": "bk_ch01",
        "review_requests": [
            {
                "review_id": "b2review_identity",
                "blocking_kind": "unresolved_entity",
                "competing_card_ids": [
                    cards[0]["entity_id"],
                    cards[1]["entity_id"],
                ],
                "source_block_ids": ["bk_ch01_b001"],
            }
        ],
    })
    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2_artifact,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
    )
    routed = bind_b2_review_routing_to_hearing_queue_v1(
        hearing_queue=queue,
        routing_plan=plan,
        chapter_registry=registry,
        b2_artifact=b2_artifact,
    )
    binding = routed["b2_review_bindings"][0]
    assert binding["destination"] == "WITHIN"
    assert binding["action"] == "attach_existing_case"
    assert binding["case_id"] == "b1lac_open_identity"
    assert routed["components"] == queue["components"]
    assert routed["b2_within_case_requests"] == []
    verify_b1_cross_chapter_hearing_queue_v1(routed)


def test_route_b_cross_binding_accepts_a_sealed_carried_candidate() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    audit = _audit(chapter, scan, enrich)
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    current_card = registry["cards"][0]
    carried_card = deepcopy(registry["cards"][1])
    carried_card["effective_entity_id"] = "b0ent_carried"
    carried_card["entity_id"] = "b0ent_carried"
    carried_card["first_seen"] = {
        **carried_card["first_seen"],
        "chapter_id": "bk_ch00",
    }
    carried_card.pop("source_refs")
    carried_card["provenance_refs"] = [
        {"chapter_id": "bk_ch00", "block_id": "bk_ch00_b001"}
    ]
    local_audit = _with_hash(
        {"chapter_id": "bk_ch01", "decisions": []}
    )
    b2_artifact = _with_hash({
        "chapter_id": "bk_ch01",
        "review_requests": [
            {
                "review_id": "b2review_carried_identity",
                "blocking_kind": "unresolved_entity",
                "competing_card_ids": [
                    current_card["entity_id"],
                    carried_card["entity_id"],
                ],
                "source_block_ids": ["bk_ch01_b001"],
            }
        ],
    })
    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2_artifact,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
        candidate_scope_cards=[carried_card],
    )

    routed = bind_b2_review_routing_to_hearing_queue_v1(
        hearing_queue=queue,
        routing_plan=plan,
        chapter_registry=registry,
        b2_artifact=b2_artifact,
        candidate_scope_cards=[carried_card],
    )

    binding = next(
        row
        for row in routed["b2_review_bindings"]
        if row["review_id"] == "b2review_carried_identity"
    )
    assert binding["destination"] == "CROSS"
    assert binding["action"] == "open_new_case"
    component = next(
        row
        for row in routed["components"]
        if row["component_id"] == binding["case_id"]
    )
    assert component["prior_card_ids"] == ["b0ent_carried"]
    assert component["current_entity_ids"] == [current_card["entity_id"]]
    verify_b1_cross_chapter_hearing_queue_v1(routed)


def test_route_b_superseded_cross_case_is_retained_without_reopening() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    audit = _audit(chapter, scan, enrich)
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    current_card = registry["cards"][0]
    carried_card = deepcopy(registry["cards"][1])
    carried_card["effective_entity_id"] = "b0ent_carried_superseded"
    carried_card["entity_id"] = "b0ent_carried_superseded"
    carried_card["first_seen"] = {
        **carried_card["first_seen"],
        "chapter_id": "bk_ch00",
    }
    local_audit = _with_hash({"chapter_id": "bk_ch01", "decisions": []})
    review = {
        "review_id": "b2review_superseded_identity",
        "blocking_kind": "unresolved_entity",
        "competing_card_ids": [
            current_card["entity_id"],
            carried_card["entity_id"],
        ],
        "source_block_ids": ["bk_ch01_b001"],
    }
    b2_artifact = _with_hash(
        {"chapter_id": "bk_ch01", "review_requests": [review]}
    )
    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2_artifact,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
        candidate_scope_cards=[carried_card],
    )
    plan["route_b"][0].update(
        {
            "action": "attached_case_superseded",
            "case_id": "b1xhear_retired",
            "matched_case_member_refs": sorted(
                [current_card["entity_id"], carried_card["entity_id"]]
            ),
        }
    )
    unsigned = deepcopy(plan)
    unsigned.pop("routing_plan_hash")
    plan["routing_plan_hash"] = canonical_hash(unsigned)

    routed = bind_b2_review_routing_to_hearing_queue_v1(
        hearing_queue=queue,
        routing_plan=plan,
        chapter_registry=registry,
        b2_artifact=b2_artifact,
        candidate_scope_cards=[carried_card],
    )

    binding = routed["b2_review_bindings"][0]
    assert binding["action"] == "attached_case_superseded"
    assert binding["case_id"] == "b1xhear_retired"
    assert binding["review_request"] == review
    assert routed["components"] == queue["components"]
    verify_b1_cross_chapter_hearing_queue_v1(routed)


def test_route_b_partial_overlap_is_parked_without_merging_case_sets() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    audit = _audit(chapter, scan, enrich)
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    cards = registry["cards"][:3]
    refs = [card["source_refs"][0] for card in cards]
    local_audit = _with_hash({
        "chapter_id": "bk_ch01",
        "decisions": [
            {
                "component_id": "b1lac_open_identity",
                "component_kind": "same_referent_proposal",
                "subject_ref": refs[0],
                "original_proposal": {"target_ref": refs[1]},
                "action": "keep_pending",
            }
        ],
    })
    b2_artifact = _with_hash({
        "chapter_id": "bk_ch01",
        "review_requests": [
            {
                "review_id": "b2review_partial_overlap",
                "blocking_kind": "unresolved_entity",
                "competing_card_ids": [
                    cards[1]["entity_id"],
                    cards[2]["entity_id"],
                ],
                "source_block_ids": ["bk_ch01_b001"],
            }
        ],
    })
    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2_artifact,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
    )
    routed = bind_b2_review_routing_to_hearing_queue_v1(
        hearing_queue=queue,
        routing_plan=plan,
        chapter_registry=registry,
        b2_artifact=b2_artifact,
    )
    binding = routed["b2_review_bindings"][0]
    assert binding["action"] == "hold_partial_overlap"
    assert binding["case_id"] == "b1lac_open_identity"
    request = routed["b2_within_case_requests"][0]
    assert request["component_kind"] == "same_referent_partial_overlap_hold"
    assert request["lifecycle_state"] == "parked_pending_identity_hearing"
    assert request["proposed_member_refs"] == sorted([refs[1], refs[2]])
    assert request["existing_member_refs"] == sorted([refs[0], refs[1]])
    assert request["identity_authority_granted"] is False
    assert routed["components"] == queue["components"]
    verify_b1_cross_chapter_hearing_queue_v1(routed)


def test_writer_applies_audit_and_preserves_field_authority() -> None:
    registry = _sealed()
    assert registry["chapter_authority_granted"] is True
    assert registry["identity_authority_granted"] is False
    assert registry["book_authority_granted"] is False
    assert len(registry["cards"]) == 3
    assert registry["metrics"]["dormant_observation_count"] == 1
    assert registry["dormant_observations"][0]["observation"]["surface"] == "1500"

    mara = next(row for row in registry["cards"] if row["canonical_surface"] == "Mara Vale")
    authority = {row["field"]: row["effective"] for row in mara["claims"]}
    assert authority == {"gender": True, "life_stage": False}
    assert mara["aliases"][0]["lookup_authority"] == "chapter_only"
    assert mara["stable_surfaces"] == ["Mara Vale"]


def test_writer_preserves_anchorless_not_applicable_claim() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    house = next(
        row
        for row in enrich["entity_dossiers"]
        if row["scan_observation_id"] == "b1obs_house"
    )
    house["claims"].append(_not_applicable_claim("role_or_occupation"))
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    house_card = next(
        row for row in registry["cards"] if row["canonical_surface"] == "North House"
    )
    claim = next(
        row for row in house_card["claims"] if row["field"] == "role_or_occupation"
    )
    assert claim["status"] == "not_applicable"
    assert claim["value"] is None
    assert claim["basis"] == "not_applicable"
    assert claim["anchor_block_ids"] == []
    assert claim["effective"] is False

    prior_card = next(
        row
        for row in build_b1_prior_context_cards_v1(registry)
        if row["canonical_surface"] == "North House"
    )
    projected_claim = next(
        row
        for row in prior_card["profile_claims"]
        if row["field"] == "role_or_occupation"
    )
    assert projected_claim["anchor_block_ids"] == []
    assert projected_claim["effective"] is False


@pytest.mark.parametrize("status", ["supported", "unclear"])
def test_writer_rejects_anchorless_assertive_claim(status: str) -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    house = next(
        row
        for row in enrich["entity_dossiers"]
        if row["scan_observation_id"] == "b1obs_house"
    )
    house["claims"].append(
        {
            "field": "role_or_occupation",
            "status": status,
            "value": "estate" if status == "supported" else None,
            "basis": "explicit_textual" if status == "supported" else None,
            "anchor_block_ids": [],
            "story_time_note": None,
            "semantic_status": "unreviewed",
        }
    )
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    with pytest.raises(
        B1ChapterRegistryWriterError,
        match="anchor_block_ids must be a non-empty list",
    ):
        seal_b1_chapter_registry_v1(
            chapter=chapter,
            scan_artifact=scan,
            enrich_artifact=enrich,
            audit_artifact=audit,
        )


def test_writer_rejects_anchored_not_applicable_claim() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    house = next(
        row
        for row in enrich["entity_dossiers"]
        if row["scan_observation_id"] == "b1obs_house"
    )
    claim = _not_applicable_claim("role_or_occupation")
    claim["anchor_block_ids"] = ["bk_ch01_b002"]
    house["claims"].append(claim)
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    with pytest.raises(
        B1ChapterRegistryWriterError,
        match="not_applicable claim anchor_block_ids must be empty",
    ):
        seal_b1_chapter_registry_v1(
            chapter=chapter,
            scan_artifact=scan,
            enrich_artifact=enrich,
            audit_artifact=audit,
        )


def test_writer_keeps_only_accepted_links_and_deduplicates_inverse_kinship() -> None:
    registry = _sealed()
    edges = registry["relation_edges"]
    assert len(edges) == 2
    residence = next(row for row in edges if row["relation"] == "resides_at")
    assert residence["effective"] is True
    parent = next(row for row in edges if row["relation_family"] == "parent_child")
    assert parent["relation"] == "parent_of"
    assert parent["relation_variants"] == ["child_of", "mother_of"]
    rejected = registry["curation_log"]["rejected_components"]
    assert len(rejected) == 1
    assert rejected[0]["original_proposal"]["relation"] == "resides_at"


def test_writer_quarantines_relation_that_collapses_to_self_after_merge() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    enrich["same_referent_proposals"] = [
        {
            "subject_ref": "scan:b1obs_mother",
            "target_ref": "scan:b1obs_mara",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b003"],
            "reason": "The proposal intentionally exercises post-merge validation.",
            "retrieval_surface_policy": "subject_evidence_only",
            "requires_local_auditor": True,
            "identity_authority_granted": False,
        }
    ]
    enrich = _rehash_artifact(enrich)
    audit = _audit(chapter, scan, enrich)

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )

    assert all(
        edge["source_entity_id"] != edge["target_entity_id"]
        for edge in registry["relation_edges"]
    )
    issue = next(
        row
        for row in registry["pending_reviews"]
        if row.get("reason_code")
        == "relation_collapsed_to_self_after_identity_merge"
    )
    assert issue["relation_family"] == "parent_child"
    assert issue["source_entity_id"] == issue["target_entity_id"]
    assert issue in registry["diagnostics"]
    assert any(
        row["component_kind"] == "kinship_link"
        and row["action"] == "accept_proposal"
        for row in registry["curation_log"]["decisions"]
    )
    verify_b1_chapter_registry_v1(registry)


def test_other_link_preserves_source_to_target_direction() -> None:
    row = _normalize_relation_candidate(
        component_kind="entity_link",
        relation="other_link",
        relation_note="acts as guardian of",
        relation_raw="guardian_of",
        relation_status="model_other",
        source_entity_id="z_source_entity",
        target_entity_id="a_target_entity",
        proposal={"anchor_block_ids": ["bk_ch01_b001"]},
        decision={
            "component_id": "b1lac_other_link",
            "source_block_ids": ["bk_ch01_b001"],
        },
        chapter_id="bk_ch01",
    )

    assert row["source_entity_id"] == "z_source_entity"
    assert row["target_entity_id"] == "a_target_entity"
    assert row["relation"] == "other_link"
    assert row["relation_raw"] == "guardian_of"


def _relation_decision(
    *,
    component_id: str,
    relation: str,
    note: str | None,
    action: str = "revise_proposal",
) -> dict:
    return {
        "component_id": component_id,
        "component_kind": "kinship_link",
        "action": action,
        "subject_ref": "scan:subject",
        "original_proposal": {
            "target_ref": "scan:target",
            "relation": "spouse_of",
            "relation_note": None,
            "anchor_block_ids": ["bk_ch01_b001"],
        },
        "revised_relation": relation if action == "revise_proposal" else None,
        "revised_relation_note": note if action == "revise_proposal" else None,
        "revised_target_ref": (
            "scan:target" if action == "revise_proposal" else None
        ),
        "source_block_ids": ["bk_ch01_b001"],
    }


def test_other_kin_revised_note_reaches_registry_edge() -> None:
    edges, issues = _build_relation_edges(
        decisions=[
            _relation_decision(
                component_id="b1lac_other_kin",
                relation="other_kin",
                note="married into the target's family",
            )
        ],
        entity_id_by_ref={
            "scan:subject": "b0ent_subject",
            "scan:target": "b0ent_target",
        },
        chapter_id="bk_ch01",
    )

    assert issues == []
    assert len(edges) == 1
    edge = edges[0]
    assert edge["relation"] == "other_kin"
    assert edge["relation_note"] == "married into the target's family"
    assert edge["relation_raw"] == "married into the target's family"
    assert edge["relation_status"] == "model_other"


def test_other_kin_notes_keep_same_pair_as_distinct_edges() -> None:
    edges, issues = _build_relation_edges(
        decisions=[
            _relation_decision(
                component_id="b1lac_other_kin_1",
                relation="other_kin",
                note="sibling-in-law through A",
            ),
            _relation_decision(
                component_id="b1lac_other_kin_2",
                relation="other_kin",
                note="cousin by marriage through B",
            ),
        ],
        entity_id_by_ref={
            "scan:subject": "b0ent_subject",
            "scan:target": "b0ent_target",
        },
        chapter_id="bk_ch01",
    )

    assert issues == []
    assert len(edges) == 2
    assert {row["relation_note"] for row in edges} == {
        "sibling-in-law through A",
        "cousin by marriage through B",
    }
    assert len({row["relation_edge_id"] for row in edges}) == 2


def test_normal_kinship_relation_does_not_gain_note_fields() -> None:
    edges, issues = _build_relation_edges(
        decisions=[
            _relation_decision(
                component_id="b1lac_spouse",
                relation="spouse_of",
                note=None,
                action="accept_proposal",
            )
        ],
        entity_id_by_ref={
            "scan:subject": "b0ent_subject",
            "scan:target": "b0ent_target",
        },
        chapter_id="bk_ch01",
    )

    assert issues == []
    assert len(edges) == 1
    edge = edges[0]
    assert edge["relation"] == "spouse_of"
    assert "relation_note" not in edge
    assert "relation_raw" not in edge
    assert "relation_status" not in edge


def _structural_edge(
    edge_id: str,
    source: str,
    relation: str,
    target: str,
    *,
    anchor: str = "bk_ch06_b009",
) -> dict:
    return {
        "relation_edge_id": edge_id,
        "relation_family": (
            "parent_child" if relation == "parent_of" else relation
        ),
        "relation": relation,
        "relation_variants": [relation],
        "source_entity_id": source,
        "target_entity_id": target,
        "chapter_id": "bk_ch06",
        "anchor_block_ids": [anchor],
        "semantic_status": "auditor_reviewed",
        "effective": True,
        "source_component_ids": [f"component_{edge_id}"],
        "validity_scope": "as_of_chapter",
    }


def test_structural_detector_marks_real_shared_parent_inversion_without_deletion() -> None:
    original = [
        _structural_edge("edge_parent_edgar", "mrs_linton", "parent_of", "edgar"),
        _structural_edge(
            "edge_parent_isabella", "mrs_linton", "parent_of", "isabella"
        ),
        _structural_edge("edge_inversion", "edgar", "parent_of", "isabella"),
    ]

    edges, issues = _mark_structurally_impossible_kinship_v1(original)

    assert {row["relation_edge_id"] for row in edges} == {
        "edge_parent_edgar",
        "edge_parent_isabella",
        "edge_inversion",
    }
    assert len(issues) == 1
    assert issues[0]["contested_rule"] == "E-1"
    assert issues[0]["shared_anchor_block_ids"] == ["bk_ch06_b009"]
    assert issues[0]["relation_edge_ids"] == sorted(
        row["relation_edge_id"] for row in original
    )
    assert all(row["structurally_contested"] is True for row in edges)
    assert all(row["effective"] is False for row in edges)
    assert all(row["semantic_status"] == "structurally_contested" for row in edges)
    assert all(
        row["contested_group_id"] == issues[0]["contested_group_id"]
        for row in edges
    )
    assert all(row["contested_rule"] == "E-1" for row in edges)
    assert all(row["effective"] is True for row in original)


def test_structural_detector_allows_clean_siblings_with_shared_parent() -> None:
    original = [
        _structural_edge("edge_parent_a", "parent", "parent_of", "child_a"),
        _structural_edge("edge_parent_b", "parent", "parent_of", "child_b"),
        _structural_edge("edge_siblings", "child_a", "sibling_of", "child_b"),
    ]

    edges, issues = _mark_structurally_impossible_kinship_v1(original)

    assert issues == []
    assert edges == sorted(original, key=lambda row: row["relation_edge_id"])


def test_structural_detector_marks_parent_cycle() -> None:
    original = [
        _structural_edge("edge_xy", "x", "parent_of", "y", anchor="b1"),
        _structural_edge("edge_yx", "y", "parent_of", "x", anchor="b2"),
    ]

    edges, issues = _mark_structurally_impossible_kinship_v1(original)

    assert len(issues) == 1
    assert issues[0]["contested_rule"] == "E-3"
    assert issues[0]["shared_anchor_block_ids"] == []
    assert {row["relation_edge_id"] for row in edges} == {"edge_xy", "edge_yx"}
    assert all(not row["effective"] for row in edges)


def test_prior_projection_is_ready_for_scan_without_global_alias_leak() -> None:
    registry = _sealed()
    cards = registry["prior_cards_projection"]["cards"]
    mara = next(row for row in cards if row["canonical_surface"] == "Mara Vale")
    mother = next(row for row in cards if row["canonical_surface"] == "mother")
    assert mara["record_class"] == "confirmed_entity"
    assert mara["claim_state"] == "confirmed"
    assert mara["stable_surfaces"] == ["Mara Vale"]
    assert mother["record_class"] == "important_unnamed_referent"
    assert mother["claim_state"] == "provisional"
    next_chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "order_index": 1,
                "clean_text": "Mara Vale returned to North House.",
            }
        ],
    }
    packets = build_prior_candidate_packets_v1(
        chapter=next_chapter, prior_cards=cards
    )
    assert {
        row["prior_card"]["canonical_surface"] for row in packets
    } == {"Mara Vale", "North House"}


def test_writer_is_deterministic_and_verifies_its_seal() -> None:
    first = _sealed()
    second = _sealed()
    assert first == second
    verify_b1_chapter_registry_v1(first)


def test_writer_fails_closed_on_input_or_output_tamper() -> None:
    chapter = _chapter()
    scan = _scan()
    enrich = _enrich(scan)
    audit = _audit(chapter, scan, enrich)
    tampered = deepcopy(enrich)
    tampered["entity_dossiers"][0]["identity_summary"] = "Changed after validation."
    with pytest.raises(B1ChapterRegistryWriterError, match="artifact hash mismatch"):
        seal_b1_chapter_registry_v1(
            chapter=chapter,
            scan_artifact=scan,
            enrich_artifact=tampered,
            audit_artifact=audit,
        )

    registry = _sealed()
    registry["cards"][0]["canonical_surface"] = "Tampered"
    with pytest.raises(B1ChapterRegistryWriterError, match="registry hash mismatch"):
        verify_b1_chapter_registry_v1(registry)
