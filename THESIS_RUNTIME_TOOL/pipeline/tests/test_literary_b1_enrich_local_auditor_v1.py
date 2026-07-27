from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b1_enrich_local_auditor_v1 import (
    B1EnrichLocalAuditError,
    OUTPUT_SCHEMA_ID,
    PROMPT_ID,
    build_b1_enrich_local_audit_manifest_v1,
    merge_b1_enrich_local_audit_batch_artifacts_v1,
    plan_b1_enrich_local_audit_batches_v1,
    render_b1_enrich_local_audit_request_v1,
    shared_b1_enrich_local_audit_request_v1,
    validate_b1_enrich_local_audit_response_v1,
)
from pipeline.literary.request_token_preflight_v1 import (
    measure_literary_request_token_preflight_v1,
)


DESIGN = Path("../design/LITERARY_PROMPT_DESIGN.md")


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
                "clean_text": "The lintel bore 1500 and the name Robin Vale.",
            },
            {
                "block_id": "bk_ch01_b004",
                "order_index": 4,
                "clean_text": "A brindled dog drove the intruder away.",
            },
        ],
    }


def _scan():
    return {
        "artifact_hash": "a" * 64,
        "chapter_id": "bk_ch01",
        "entity_observations": [
            {
                "observation_id": "b1obs_mara",
                "surface": "Mara Vale",
                "source_block_ids": ["bk_ch01_b001"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named participant.",
            },
            {
                "observation_id": "b1obs_house",
                "surface": "North House",
                "source_block_ids": ["bk_ch01_b001"],
                "referent_kind_claim": "place",
                "record_class": "named_entity_candidate",
                "presence_basis": "referenced_by_other",
                "scan_note": "Named place.",
            },
            {
                "observation_id": "b1obs_date",
                "surface": "1500",
                "source_block_ids": ["bk_ch01_b003"],
                "referent_kind_claim": "unknown",
                "record_class": "unresolved_named_reference",
                "presence_basis": "inscription_or_document",
                "scan_note": "Bare carved date.",
            },
        ],
        "glossary_observations": [],
    }


def _claim(field, value):
    return {
        "field": field,
        "status": "supported",
        "value": value,
        "basis": "explicit_textual",
        "anchor_block_ids": ["bk_ch01_b001"],
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
        "identity_summary": f"Source-grounded dossier for {surface}.",
        "distinguishing_note": None,
        "authority_scope": "chapter_provisional",
    }


def _enrich():
    mara = _dossier(
        "b1obs_mara",
        "Mara Vale",
        "person",
        [_claim("gender", "feminine"), _claim("life_stage", "adult")],
    )
    mara["links"] = [
        {
            "relation": "resides_at",
            "target_ref": "scan:b1obs_house",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b001"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        },
        {
            "relation": "owned_by",
            "target_ref": "scan:foreign",
            "basis": "contextual_inference",
            "anchor_block_ids": ["bk_ch01_b002"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        },
    ]
    mara["aliases_observed"] = [
        {
            "surface": "Mara Vale",
            "anchor_block_ids": ["bk_ch01_b002"],
            "status": "proposed",
        }
    ]
    house = _dossier(
        "b1obs_house",
        "North House",
        "place",
        [_claim("place_type", "dwelling")],
    )
    additional = {
        "additional_entity_id": "b1add_dog",
        "surface": "A brindled dog",
        "source_block_ids": ["bk_ch01_b004"],
        "referent_kind_claim": "animal",
        "claims": [_claim("species", "dog")],
        "kinship_links": [],
        "links": [],
        "address_forms_used": [],
        "aliases_observed": [],
        "identity_summary": "An individualized brindled dog.",
        "distinguishing_note": None,
        "authority_scope": "chapter_provisional",
    }
    body = {
        "schema_version": "literary_b1_enrich_artifact_v1",
        "chapter_id": "bk_ch01",
        "request_fingerprint": "c" * 64,
        "scan_artifact_hash": "a" * 64,
        "entity_dossiers": [mara, house],
        "additional_entity_dossiers": [additional],
        "spurious_challenges": [
            {
                "scan_observation_id": "b1obs_date",
                "reason": "A bare date is not a persistent referent.",
                "source_block_ids": ["bk_ch01_b003"],
                "requires_local_auditor": True,
            }
        ],
        "same_referent_proposals": [],
        "conflict_findings": [
            {
                "scan_observation_id": "b1obs_mara",
                "field": "role_or_occupation",
                "existing_value": "tenant",
                "observed_value": "owner",
                "source_block_ids": ["bk_ch01_b002"],
                "reason": "Current wording challenges the prior claim.",
                "requires_hearing": True,
            }
        ],
        "presence_correction_findings": [
            {
                "scan_observation_id": "b1obs_house",
                "proposed_presence_basis": "direct_presence",
                "source_block_ids": ["bk_ch01_b001"],
                "reason": "The place is entered directly.",
                "requires_local_auditor": True,
            },
            {
                "scan_observation_id": "b1obs_date",
                "proposed_presence_basis": "inscription_or_document",
                "source_block_ids": ["bk_ch01_b003"],
                "reason": "Same as the Scan classification.",
                "requires_local_auditor": True,
            },
        ],
        "glossary_items": [
            {
                "term_observation_id": "b1term_house",
                "surface": "house",
                "contextual_sense": "The main room or the building.",
                "ambiguity_status": "ambiguous",
                "source_block_ids": ["bk_ch01_b002"],
                "translation_authority_granted": False,
            }
        ],
        "quarantined_tasks": [],
        "review_issues": [],
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "metrics": {},
        "artifact_hash": "b" * 64,
    }
    return body


def _response(manifest):
    decisions = []
    for component in manifest["components"]:
        action = "accept_proposal"
        decisions.append(
            {
                "component_id": component["component_id"],
                "action": action,
                "revised_relation": None,
                "revised_relation_note": None,
                "revised_target_ref": None,
                "source_block_ids": [component["direct_source_block_ids"][0]],
                "resolution_note": "The cited chapter evidence supports this disposition.",
            }
        )
    return {
        "schema_id": OUTPUT_SCHEMA_ID,
        "chapter_id": "bk_ch01",
        "manifest_hash": manifest["manifest_hash"],
        "decisions": decisions,
        "unasked_same_referent_observations": [],
    }


def _non_direct_source(manifest, component_id):
    component = next(
        row for row in manifest["components"] if row["component_id"] == component_id
    )
    return next(
        block_id
        for block_id in manifest["allowed_source_block_ids"]
        if block_id not in component["direct_source_block_ids"]
    )


def test_manifest_batches_only_exceptions_and_unreviewed_proposals() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    kinds = [row["component_kind"] for row in manifest["components"]]
    assert sorted(kinds) == [
        "additional_entity",
        "alias_proposal",
        "entity_link",
        "glossary_ambiguity",
        "presence_correction",
        "spurious_challenge",
    ]
    assert manifest["mechanical_noops"] == [
        {
            "kind": "presence_correction_same_as_scan",
            "scan_observation_id": "b1obs_date",
            "presence_basis": "inscription_or_document",
        }
    ]
    assert manifest["quarantined_rows"][0]["reason"] == (
        "target_ref_outside_supplied_entity_pool"
    )
    assert len({row["ref"] for row in manifest["entity_cards"]}) == len(
        manifest["entity_cards"]
    )
    assert len({row["block_id"] for row in manifest["source_blocks"]}) == len(
        manifest["source_blocks"]
    )


def test_valid_response_exact_covers_components_without_book_authority() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    artifact = validate_b1_enrich_local_audit_response_v1(
        _response(manifest),
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )
    assert artifact["metrics"]["component_count"] == 6
    assert artifact["metrics"]["cross_chapter_referral_count"] == 0
    assert artifact["identity_authority_granted"] is False
    assert artifact["book_authority_granted"] is False
    alias = next(
        row
        for row in artifact["accepted_components"]
        if row["component_kind"] == "alias_proposal"
    )
    assert alias["authority_scope"] == "chapter_confirmed_alias_no_global_authority"


def test_mechanical_name_variant_becomes_a_review_component_not_a_decision() -> None:
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
        }
    )
    enrich = _enrich()
    enrich["entity_dossiers"].append(
        _dossier(
            "b1obs_mara_short",
            "Mara",
            "person",
            [_claim("gender", "feminine"), _claim("life_stage", "adult")],
        )
    )

    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=scan, enrich_artifact=enrich
    )
    component = next(
        row
        for row in manifest["components"]
        if row["component_kind"] == "same_referent_proposal"
    )

    assert component["subject_ref"] == "scan:b1obs_mara_short"
    assert component["proposal"]["target_ref"] == "scan:b1obs_mara"
    assert component["proposal"]["proposal_basis"] == "name_variant"
    assert component["proposal"]["identity_authority_granted"] is False


def test_auditor_observation_is_retained_without_current_pass_authority() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    response["unasked_same_referent_observations"] = [
        {
            "subject_ref": "scan:b1obs_mara",
            "target_ref": "scan:b1obs_house",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The pair merits a later identity-specific proposal.",
        }
    ]

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )

    observation = artifact["unasked_same_referent_observations"][0]
    assert observation["left_ref"] == "scan:b1obs_mara"
    assert observation["right_ref"] == "scan:b1obs_house"
    assert observation["lifecycle_state"] == "proposed_for_next_pass"
    assert observation["identity_authority_granted"] is False
    assert observation["applied_in_current_pass"] is False
    assert artifact["metrics"]["reviewed_component_count"] == 6


def test_wrong_chapter_echo_keeps_all_local_audit_decisions() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    response["chapter_id"] = "copied_example_chapter"

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )

    assert artifact["chapter_id"] == "bk_ch01"
    assert artifact["metrics"]["component_count"] == len(manifest["components"])
    assert artifact["response_normalization_notes"][0]["field"] == "chapter_id"


def test_missing_component_fails_exact_cover() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    response["decisions"].pop()
    with pytest.raises(B1EnrichLocalAuditError, match="exact-cover"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint="d" * 64,
        )


def test_one_decision_without_direct_evidence_is_quarantined() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    bad = response["decisions"][0]
    bad["source_block_ids"] = [
        _non_direct_source(manifest, bad["component_id"])
    ]

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )

    assert artifact["metrics"]["component_count"] == 6
    assert artifact["metrics"]["reviewed_component_count"] == 5
    assert artifact["metrics"]["unreviewed_component_count"] == 1
    assert len(artifact["decisions"]) == 5
    assert all(
        row["component_id"] != bad["component_id"] for row in artifact["decisions"]
    )
    assert artifact["review_issues"] == [
        {
            "row_type": "local_audit_decision",
            "state": "unreviewed",
            "component_id": bad["component_id"],
            "component_kind": manifest["components"][0]["component_kind"],
            "subject_ref": manifest["components"][0]["subject_ref"],
            "reason": "decision cites no direct component evidence",
            "cited_source_block_ids": bad["source_block_ids"],
            "direct_source_block_ids": manifest["components"][0][
                "direct_source_block_ids"
            ],
            "raw_row": bad,
        }
    ]
    assert artifact["quarantined_rows"][-1] == artifact["review_issues"][0]


def test_every_decision_without_direct_evidence_fails_whole_response() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    for row in response["decisions"]:
        row["source_block_ids"] = [
            _non_direct_source(manifest, row["component_id"])
        ]

    with pytest.raises(B1EnrichLocalAuditError, match="all local-audit decisions"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint="d" * 64,
        )


def test_resolution_note_has_no_hidden_limit_beyond_response_schema() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    component_id = response["decisions"][0]["component_id"]
    response["decisions"][0]["resolution_note"] = "evidence " * 60

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )

    decision = next(
        row for row in artifact["decisions"] if row["component_id"] == component_id
    )
    assert decision["resolution_note"] == ("evidence " * 60).strip()


def test_relation_revision_must_use_supplied_target_and_same_family() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    link = next(
        row
        for row in response["decisions"]
        if next(
            component
            for component in manifest["components"]
            if component["component_id"] == row["component_id"]
        )["component_kind"]
        == "entity_link"
    )
    link.update(
        {
            "action": "revise_proposal",
            "revised_relation": "resides_at",
            "revised_target_ref": "scan:foreign",
        }
    )
    with pytest.raises(B1EnrichLocalAuditError, match="outside supplied refs"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint="d" * 64,
        )


def test_entity_link_revision_to_other_link_requires_and_preserves_note() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    component = next(
        row
        for row in manifest["components"]
        if row["component_kind"] == "entity_link"
    )
    decision = next(
        row
        for row in response["decisions"]
        if row["component_id"] == component["component_id"]
    )
    decision.update(
        {
            "action": "revise_proposal",
            "revised_relation": "other_link",
            "revised_relation_note": "acts as guardian of",
            "revised_target_ref": component["proposal"]["target_ref"],
        }
    )

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )

    revised = next(
        row
        for row in artifact["revised_components"]
        if row["component_id"] == component["component_id"]
    )
    assert revised["revised_proposal"]["relation"] == "other_link"
    assert revised["revised_proposal"]["relation_note"] == "acts as guardian of"
    assert revised["revised_proposal"]["relation_raw"] == "acts as guardian of"
    assert revised["revised_proposal"]["relation_status"] == "model_other"

    decision["revised_relation_note"] = None
    with pytest.raises(ValueError, match="revised_relation_note"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint="d" * 64,
        )


def test_kinship_revision_to_other_kin_requires_and_preserves_note() -> None:
    enrich = _enrich()
    enrich["entity_dossiers"][0]["kinship_links"] = [
        {
            "relation": "father_of",
            "relation_note": "The source describes an in-law relation.",
            "target_ref": "scan:b1obs_house",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b002"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        }
    ]
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=enrich
    )
    response = _response(manifest)
    component = next(
        row
        for row in manifest["components"]
        if row["component_kind"] == "kinship_link"
    )
    decision = next(
        row
        for row in response["decisions"]
        if row["component_id"] == component["component_id"]
    )
    decision.update(
        {
            "action": "revise_proposal",
            "revised_relation": "other_kin",
            "revised_relation_note": "father-in-law of",
            "revised_target_ref": component["proposal"]["target_ref"],
        }
    )

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=enrich,
        request_fingerprint="d" * 64,
    )
    revised = next(
        row
        for row in artifact["revised_components"]
        if row["component_id"] == component["component_id"]
    )
    assert revised["revised_proposal"]["relation"] == "other_kin"
    assert revised["revised_proposal"]["relation_note"] == "father-in-law of"

    decision["revised_relation_note"] = None
    with pytest.raises(ValueError, match="revised_relation_note"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=enrich,
            request_fingerprint="d" * 64,
        )


def test_cross_family_revision_quarantines_only_that_decision() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    component = next(
        row
        for row in manifest["components"]
        if row["component_kind"] == "entity_link"
    )
    decision = next(
        row
        for row in response["decisions"]
        if row["component_id"] == component["component_id"]
    )
    decision.update(
        {
            "action": "revise_proposal",
            "revised_relation": "father_of",
            "revised_target_ref": component["proposal"]["target_ref"],
        }
    )

    artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint="d" * 64,
    )

    assert len(artifact["decisions"]) == len(manifest["components"]) - 1
    assert artifact["review_issues"] == [
        {
            "row_type": "local_audit_decision",
            "state": "unreviewed",
            "component_id": component["component_id"],
            "component_kind": "entity_link",
            "subject_ref": component["subject_ref"],
            "reason": "revised relation crosses its closed family",
            "cited_source_block_ids": decision["source_block_ids"],
            "direct_source_block_ids": component["direct_source_block_ids"],
            "raw_row": decision,
        }
    ]


def test_stale_manifest_and_foreign_source_fail_closed() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    response = _response(manifest)
    response["manifest_hash"] = "0" * 64
    with pytest.raises(B1EnrichLocalAuditError, match="manifest hash"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint="d" * 64,
        )
    response = _response(manifest)
    response["decisions"][0]["source_block_ids"] = ["bk_ch01_foreign"]
    with pytest.raises(B1EnrichLocalAuditError, match="outside the packet"):
        validate_b1_enrich_local_audit_response_v1(
            response,
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint="d" * 64,
        )


def test_render_is_one_compact_exception_batch() -> None:
    rendered = render_b1_enrich_local_audit_request_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        design_doc=DESIGN,
    )
    assert rendered.prompt_id == PROMPT_ID
    assert len(rendered.sections["exception_components"]) == 6
    assert len(rendered.messages) == 2
    assert rendered.parent_working_revision_hash == "b" * 64


def test_artifact_lineage_mismatch_fails_before_render() -> None:
    enrich = deepcopy(_enrich())
    enrich["scan_artifact_hash"] = "f" * 64
    with pytest.raises(B1EnrichLocalAuditError, match="lineage"):
        build_b1_enrich_local_audit_manifest_v1(
            chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=enrich
        )


def test_component_batches_are_sized_by_reserve_and_merge_exactly_once() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    singleton_reserves = []
    for component in manifest["components"]:
        rendered = render_b1_enrich_local_audit_request_v1(
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            design_doc=DESIGN,
            component_ids=[component["component_id"]],
        )
        singleton_reserves.append(
            measure_literary_request_token_preflight_v1(
                shared_b1_enrich_local_audit_request_v1(rendered),
                prompt_token_cap=100_000,
                output_token_cap=4096,
            ).prompt_token_reserve
        )
    full_rendered = render_b1_enrich_local_audit_request_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        design_doc=DESIGN,
    )
    full_reserve = measure_literary_request_token_preflight_v1(
        shared_b1_enrich_local_audit_request_v1(full_rendered),
        prompt_token_cap=100_000,
        output_token_cap=4096,
    ).prompt_token_reserve
    cap = max(singleton_reserves)
    assert full_reserve > cap

    plan, batches = plan_b1_enrich_local_audit_batches_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        design_doc=DESIGN,
        prompt_token_cap=cap,
        output_token_cap=4096,
    )
    assert len(batches) > 1
    assert all(batch.token_preflight.fits_prompt_cap for batch in batches)
    planned_ids = [
        component_id
        for batch in batches
        for component_id in batch.component_ids
    ]
    assert planned_ids == [row["component_id"] for row in manifest["components"]]

    artifacts = [
        validate_b1_enrich_local_audit_response_v1(
            _response(batch.manifest),
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            request_fingerprint=batch.rendered.request_fingerprint,
            component_ids=batch.component_ids,
        )
        for batch in batches
    ]
    merged = merge_b1_enrich_local_audit_batch_artifacts_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        batch_plan=plan,
        batch_artifacts=artifacts,
    )
    assert merged["metrics"]["component_count"] == len(manifest["components"])
    assert merged["metrics"]["batch_count"] == len(batches)
    assert len({row["component_id"] for row in merged["decisions"]}) == len(
        manifest["components"]
    )
    with pytest.raises(B1EnrichLocalAuditError, match="artifact count differs"):
        merge_b1_enrich_local_audit_batch_artifacts_v1(
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            batch_plan=plan,
            batch_artifacts=artifacts[:-1],
        )
    with pytest.raises(B1EnrichLocalAuditError, match="lineage differs"):
        merge_b1_enrich_local_audit_batch_artifacts_v1(
            chapter=_chapter(),
            scan_artifact=_scan(),
            enrich_artifact=_enrich(),
            batch_plan=plan,
            batch_artifacts=[artifacts[0] for _batch in batches],
        )


def test_batch_merge_preserves_quarantined_component_as_unreviewed() -> None:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=_chapter(), scan_artifact=_scan(), enrich_artifact=_enrich()
    )
    plan, batches = plan_b1_enrich_local_audit_batches_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        design_doc=DESIGN,
        prompt_token_cap=100_000,
        output_token_cap=4096,
    )
    assert len(batches) == 1
    response = _response(batches[0].manifest)
    bad = response["decisions"][0]
    bad["source_block_ids"] = [
        _non_direct_source(batches[0].manifest, bad["component_id"])
    ]
    batch_artifact = validate_b1_enrich_local_audit_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        request_fingerprint=batches[0].rendered.request_fingerprint,
        component_ids=batches[0].component_ids,
    )

    merged = merge_b1_enrich_local_audit_batch_artifacts_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        enrich_artifact=_enrich(),
        batch_plan=plan,
        batch_artifacts=[batch_artifact],
    )

    assert merged["metrics"]["component_count"] == len(manifest["components"])
    assert merged["metrics"]["reviewed_component_count"] == 5
    assert merged["metrics"]["unreviewed_component_count"] == 1
    assert merged["review_issues"][0]["component_id"] == bad["component_id"]
    assert len(merged["decisions"]) + len(merged["review_issues"]) == len(
        manifest["components"]
    )


def test_129_block_chapter_batches_by_components_not_raw_block_count() -> None:
    chapter = {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": f"bk_ch01_b{index:03d}",
                "order_index": index,
                "clean_text": f"Mara Vale remains near North House, evidence {index}.",
            }
            for index in range(1, 130)
        ],
    }
    enrich = deepcopy(_enrich())
    mara = next(
        row
        for row in enrich["entity_dossiers"]
        if row["scan_observation_id"] == "b1obs_mara"
    )
    mara["links"] = [
        {
            "relation": "resides_at",
            "target_ref": "scan:b1obs_house",
            "basis": "explicit_textual",
            "anchor_block_ids": [f"bk_ch01_b{index:03d}"],
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        }
        for index in range(1, 130, 2)
    ]

    plan, batches = plan_b1_enrich_local_audit_batches_v1(
        chapter=chapter,
        scan_artifact=_scan(),
        enrich_artifact=enrich,
        design_doc=DESIGN,
        prompt_token_cap=20_000,
        output_token_cap=4096,
    )
    assert plan["component_count"] > 32
    assert len(batches) >= 3
    assert all(len(batch.component_ids) <= 32 for batch in batches)
    assert all(batch.token_preflight.fits_prompt_cap for batch in batches)
    assert len(
        [component_id for batch in batches for component_id in batch.component_ids]
    ) == plan["component_count"]
    artifacts = [
        validate_b1_enrich_local_audit_response_v1(
            _response(batch.manifest),
            chapter=chapter,
            scan_artifact=_scan(),
            enrich_artifact=enrich,
            request_fingerprint=batch.rendered.request_fingerprint,
            component_ids=batch.component_ids,
        )
        for batch in batches
    ]
    merged = merge_b1_enrich_local_audit_batch_artifacts_v1(
        chapter=chapter,
        scan_artifact=_scan(),
        enrich_artifact=enrich,
        batch_plan=plan,
        batch_artifacts=artifacts,
    )
    assert merged["metrics"]["component_count"] == plan["component_count"]
    assert merged["metrics"]["batch_count"] == len(batches)
