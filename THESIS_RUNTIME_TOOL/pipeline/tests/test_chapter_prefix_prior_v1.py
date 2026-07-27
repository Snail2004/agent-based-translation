from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.agents.provider_profile import ResolvedCredential
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    prior_challenge_response_schema,
    validate_prior_cards,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    CLAIM_PROJECTION_SCHEMA_VERSION,
    CLAIM_VALIDATOR_VERSION,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    CHAPTER_CONFIRMED_SCOPE,
    ChapterPrefixPriorError,
    apply_claim_projection_to_prefix_bundle_v1,
    apply_glossary_dispositions_to_prefix_bundle_v1,
    b0_inputs_from_prefix_bundle_v1,
    build_chapter_prefix_prior_bundle_v1,
    extend_chapter_prefix_prior_bundle_v1,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    REVIEW_LEDGER_SCHEMA_VERSION,
    REVIEW_LEDGER_VALIDATOR_VERSION,
    append_prefix_identity_uncertainties_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.structured_output_policy_v1 import (
    load_literary_structured_output_policy,
    resolve_structured_output_contract,
)
import pipeline.scripts.run_b0_prior_challenge_experiment as prior_runner
from pipeline.scripts.run_b0_prior_challenge_experiment import build_envelope


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"
STRUCTURED_OUTPUT_POLICY = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_structured_output_policy_v1.json"
)


def _openai_prior_contract():
    return resolve_structured_output_contract(
        load_literary_structured_output_policy(STRUCTURED_OUTPUT_POLICY),
        role_id="literary_b0",
        provider="openai",
        base_url=None,
        model_id="gpt-5.4",
        canonical_schema=prior_challenge_response_schema(),
    )


def _document() -> dict:
    return {
        "document_id": "synthetic_book",
        "chapters": [
            {
                "chapter_id": "bk_ch01",
                "blocks": [
                    {
                        "block_id": "bk_ch01_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Mr. Vale entered North House.",
                    },
                    {
                        "block_id": "bk_ch01_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "An unnamed hound waited by the gate.",
                    },
                ],
            },
            {
                "chapter_id": "bk_ch02",
                "blocks": [
                    {
                        "block_id": "bk_ch02_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Vale returned, and the hound followed.",
                    }
                ],
            },
        ],
    }


def _claim(value: str, block_id: str) -> dict:
    return {
        "value": value,
        "basis": "explicit",
        "support_block_ids": [block_id],
        "address_validation_state": "valid",
        "semantic_status": "unreviewed",
    }


def _entity(
    candidate_id: str,
    surface: str,
    block_id: str,
    *,
    state: str = "clean_provisional",
) -> dict:
    alternatives = (
        [{"surface": "Vale", "name_class": "proper_name", "source_block_ids": [block_id]}]
        if surface == "Mr. Vale"
        else []
    )
    return {
        "candidate_id": candidate_id,
        "canonical_surface": surface,
        "surface_status": "located",
        "canonical_name_class": (
            "title_plus_name" if alternatives else "proper_name"
        ),
        "alternative_names": alternatives,
        "name_locations": [
            {
                "surface": surface,
                "name_class": "proper_name",
                "source_block_ids": [block_id],
            },
            *alternatives,
        ],
        "source_block_ids": [block_id],
        "referent_kind_claim": _claim("person", block_id),
        "referential_gender_claim": _claim("masculine", block_id),
        "identity_summary_draft": "A named visitor associated with the residence.",
        "identity_summary_status": "unreviewed",
        "publication_state": state,
        "audit_reasons": [],
        "conflict_status": "no_detected_identity_conflict",
    }


def _inventory() -> dict:
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": "bk_ch01",
        "source_inventory_hash": "source_inventory_ch01",
        "request_fingerprint": "request_ch01",
        "conflict_manifest_hash": "manifest_ch01",
        "entity_candidates": [_entity("local_vale", "Mr. Vale", "bk_ch01_b001")],
        "pending_entity_candidates": [
            _entity(
                "local_alder_pending",
                "Alder",
                "bk_ch01_b002",
                state="pending",
            )
        ],
        "closed_entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": [
            {
                "candidate_id": "unresolved_hound",
                "surface": "the hound",
                "referent_kind_claim": "animal",
                "short_description": "An unnamed hound salient in the chapter.",
                "source_block_ids": ["bk_ch01_b002"],
            }
        ],
        "quarantined_surfaces": [],
        "global_surface_bindings": [],
        "component_decisions": [],
        "conflict_summary": {},
        "source_validation_report": {},
        "production_publish_performed": False,
    }
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _inventory_with_glossary(
    *,
    lifecycle_state: str = "chapter_confirmed",
) -> dict:
    inventory = deepcopy(_inventory())
    inventory.pop("conflict_audited_inventory_hash")
    table = {
        "chapter_confirmed": "glossary_candidates",
        "pending_evidence": "pending_glossary_candidates",
        "rejected_dormant": "dormant_glossary_candidates",
    }[lifecycle_state]
    inventory["glossary_candidates"] = []
    inventory["pending_glossary_candidates"] = []
    inventory["dormant_glossary_candidates"] = []
    inventory[table] = [
        {
            "candidate_id": "gloss_north_house",
            "surface": "North House",
            "category_claim": "place_term",
            "short_description": "A locally significant named residence.",
            "source_block_ids": ["bk_ch01_b001"],
            "preferred_rendering_vi": (
                "Bắc Trang" if lifecycle_state == "chapter_confirmed" else None
            ),
            "render_policy": (
                "advisory_meaning"
                if lifecycle_state == "chapter_confirmed"
                else "none"
            ),
            "publication_state": lifecycle_state,
        }
    ]
    return {
        **inventory,
        "conflict_audited_inventory_hash": canonical_hash(inventory),
    }


def _glossary_challenge_artifact(
    *,
    glossary_card_id: str,
    verdict: str,
    reason: str | None,
) -> dict:
    body = {
        "schema_version": "fixture_prior_challenge_v1",
        "chapter_id": "bk_ch02",
        "code_derived_glossary_presence": [
            {
                "glossary_card_id": glossary_card_id,
                "current_surface_hits": [
                    {
                        "surface": "North House",
                        "current_block_ids": ["bk_ch02_b001"],
                    }
                ],
            }
        ],
        "prior_glossary_dispositions": [
            {
                "glossary_card_id": glossary_card_id,
                "verdict": verdict,
                "source_block_ids": ["bk_ch02_b001"],
                "reason": reason,
            }
        ],
    }
    return {**body, "prior_challenge_artifact_hash": canonical_hash(body)}


def _second_inventory_with_surface_collision() -> dict:
    body = deepcopy(_inventory())
    body.pop("conflict_audited_inventory_hash")
    body["chapter_id"] = "bk_ch02"
    body["source_inventory_hash"] = "source_inventory_ch02"
    body["request_fingerprint"] = "request_ch02"
    body["conflict_manifest_hash"] = "manifest_ch02"
    body["entity_candidates"] = [
        _entity("local_vale_second", "Vale", "bk_ch02_b001")
    ]
    body["pending_entity_candidates"] = []
    body["unresolved_referents"] = []
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _pending_projection(
    bundle: dict,
    *,
    identity_pending: bool = False,
    disputed_field: str = "identity_summary",
    prior_card_id: str | None = None,
) -> dict:
    claim = next(
        row
        for row in bundle["claim_cards"]
        if prior_card_id is None or row["prior_card_id"] == prior_card_id
    )
    field = "identity_membership" if identity_pending else disputed_field
    dispute = {
        "disputed_field": field,
        "historical_value": None if identity_pending else claim[field],
        "status": "pending",
        "pending_reason_codes": ["conflicting_evidence"],
        "evidence_manifest_hashes": ["a" * 64],
        "hearing_count": 1,
        "automatic_hearing_limit": 2,
        "same_evidence_reopen_forbidden": True,
        "next_review_trigger": (
            "identity_resolution" if identity_pending else "new_evidence_or_book_end"
        ),
        "revision_ids": ["bclaimrev1_synthetic"],
    }
    effective = {
        "referent_kind": claim["referent_kind"],
        "referential_gender": claim["referential_gender"],
        "identity_summary": claim["identity_summary"],
    }
    if not identity_pending:
        effective[field] = None
    body = {
        "schema_version": CLAIM_PROJECTION_SCHEMA_VERSION,
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "state_lineage_id": bundle["state_lineage_id"],
        "registry_generation_hash": "b" * 64,
        "claim_ledger_hash": "c" * 64,
        "projected_prior_cards": [
            {
                "prior_card_id": claim["prior_card_id"],
                "source_prior_card_hash": canonical_hash(claim),
                "original_prior_card": deepcopy(claim),
                "effective_claims": effective,
                "disputed_claims": [dispute],
                "authority_state": (
                    "candidate_only" if identity_pending else "partial_pending"
                ),
                "claim_states": [],
            }
        ],
    }
    return {**body, "projection_hash": canonical_hash(body)}


def test_prefix_adapter_keeps_history_separate_from_context_authority() -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    verified = verify_chapter_prefix_prior_bundle_v1(bundle, document=_document())
    assert len(verified["claim_cards"]) == 1
    assert verified["b0_context_cards"][0]["authority_scope"] == CHAPTER_CONFIRMED_SCOPE
    assert {
        row["canonical_surface"] for row in verified["candidate_only_context_cards"]
    } == {"Alder", "the hound"}
    assert all(
        row["authority_scope"] == CANDIDATE_ONLY_SCOPE
        for row in verified["candidate_only_context_cards"]
    )
    assert verified["covered_chapter_ids"] == ["bk_ch01"]


def test_prefix_adapter_excludes_contextual_alias_but_keeps_title_base() -> None:
    document = _document()
    document["chapters"][0]["blocks"][0]["clean_text"] = (
        "Mr. Vale, called the householder here, entered North House."
    )
    inventory = _inventory()
    inventory.pop("conflict_audited_inventory_hash")
    entity = inventory["entity_candidates"][0]
    contextual = {
        "surface": "the householder",
        "name_class": "stable_nickname",
        "source_block_ids": ["bk_ch01_b001"],
        "surface_match_block_ids": ["bk_ch01_b001"],
        "address_validation_state": "valid",
    }
    entity["alternative_names"].append(contextual)
    entity["name_locations"].append(contextual)
    inventory["conflict_audited_inventory_hash"] = canonical_hash(inventory)

    bundle = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    card = next(
        row for row in bundle["b0_context_cards"]
        if row["canonical_surface"] == "Mr. Vale"
    )

    assert card["stable_surfaces"] == ["Mr. Vale", "Vale"]
    assert entity["alternative_names"][-1]["surface"] == "the householder"


def test_dormant_unresolved_uses_only_code_located_surface_provenance() -> None:
    inventory = deepcopy(_inventory())
    inventory.pop("conflict_audited_inventory_hash")
    inventory["unresolved_referents"] = [
        {
            "candidate_id": "unresolved_hound",
            "surface": "the hound",
            "referent_kind_claim": "animal",
            "short_description": "An unnamed hound salient in the chapter.",
            "source_block_ids": [],
            "proposed_support_block_ids": ["bk_ch01_b001"],
            "surface_match_block_ids": ["bk_ch01_b002"],
            "address_validation_state": "surface_absent_from_support",
            "lifecycle_state": "dormant_unresolved",
            "publication_state": "not_published",
        }
    ]
    inventory["conflict_audited_inventory_hash"] = canonical_hash(inventory)

    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    card = next(
        row
        for row in bundle["candidate_only_context_cards"]
        if row["canonical_surface"] == "the hound"
    )
    manifest = next(
        row
        for row in bundle["source_entity_manifest"]
        if row["prior_card_id"] == card["prior_card_id"]
    )
    assert card["authority_scope"] == CANDIDATE_ONLY_SCOPE
    assert card["first_supported_block_id"] == "bk_ch01_b002"
    assert card["provenance_refs"] == [
        {"chapter_id": "bk_ch01", "block_id": "bk_ch01_b002"}
    ]
    assert manifest["all_source_block_ids"] == ["bk_ch01_b002"]
    assert "bk_ch01_b001" not in manifest["all_source_block_ids"]


def test_dormant_unresolved_without_literal_source_stays_out_of_prefix() -> None:
    inventory = deepcopy(_inventory())
    inventory.pop("conflict_audited_inventory_hash")
    inventory["unresolved_referents"] = [
        {
            "candidate_id": "unresolved_hound",
            "surface": "the hound",
            "referent_kind_claim": "animal",
            "short_description": "An unnamed hound salient in the chapter.",
            "source_block_ids": [],
            "proposed_support_block_ids": ["bk_ch01_b001"],
            "surface_match_block_ids": [],
            "address_validation_state": "surface_absent_from_support",
            "lifecycle_state": "dormant_unresolved",
            "publication_state": "not_published",
        }
    ]
    inventory["conflict_audited_inventory_hash"] = canonical_hash(inventory)

    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )

    assert all(
        row["canonical_surface"] != "the hound"
        for row in [
            *bundle["b0_context_cards"],
            *bundle["candidate_only_context_cards"],
        ]
    )
    assert all(
        row["source_candidate_id"] != "unresolved_hound"
        for row in bundle["source_entity_manifest"]
    )


@pytest.mark.parametrize(
    ("lifecycle_state", "authority_scope", "render_policy"),
    [
        ("chapter_confirmed", CHAPTER_CONFIRMED_SCOPE, "advisory_meaning"),
        ("pending_evidence", CANDIDATE_ONLY_SCOPE, "none"),
        ("rejected_dormant", "dormant", "none"),
    ],
)
def test_prefix_preserves_glossary_lifecycle_without_book_authority(
    lifecycle_state: str,
    authority_scope: str,
    render_policy: str,
) -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory_with_glossary(
            lifecycle_state=lifecycle_state
        ),
        coverage_through_chapter_id="bk_ch01",
    )
    card = bundle["glossary_context_cards"][0]
    assert card["lifecycle_state"] == lifecycle_state
    assert card["authority_scope"] == authority_scope
    assert card["render_policy"] == render_policy
    assert card["authority_scope"] != "book_confirmed"
    if lifecycle_state != "chapter_confirmed":
        assert card["preferred_rendering_vi"] is None


def test_dormant_glossary_reopens_only_as_pending_and_replay_is_idempotent() -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory_with_glossary(
            lifecycle_state="rejected_dormant"
        ),
        coverage_through_chapter_id="bk_ch01",
    )
    card_id = bundle["glossary_context_cards"][0]["glossary_card_id"]
    artifact = _glossary_challenge_artifact(
        glossary_card_id=card_id,
        verdict="compatible",
        reason=None,
    )
    reopened = apply_glossary_dispositions_to_prefix_bundle_v1(
        bundle=bundle,
        challenge_artifact=artifact,
    )
    card = reopened["glossary_context_cards"][0]
    assert card["lifecycle_state"] == "pending_evidence"
    assert card["authority_scope"] == CANDIDATE_ONLY_SCOPE
    assert card["preferred_rendering_vi"] is None
    assert card["hearing_count"] == 2

    replay = apply_glossary_dispositions_to_prefix_bundle_v1(
        bundle=reopened,
        challenge_artifact=artifact,
    )
    assert replay["prefix_bundle_hash"] == reopened["prefix_bundle_hash"]
    assert replay["glossary_context_cards"][0]["hearing_count"] == 2


def test_glossary_challenge_demotes_confirmed_card_and_clears_rendering() -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory_with_glossary(),
        coverage_through_chapter_id="bk_ch01",
    )
    card_id = bundle["glossary_context_cards"][0]["glossary_card_id"]
    demoted = apply_glossary_dispositions_to_prefix_bundle_v1(
        bundle=bundle,
        challenge_artifact=_glossary_challenge_artifact(
            glossary_card_id=card_id,
            verdict="challenge",
            reason="The current use contradicts the earlier local sense.",
        ),
    )
    card = demoted["glossary_context_cards"][0]
    assert card["lifecycle_state"] == "pending_evidence"
    assert card["preferred_rendering_vi"] is None
    assert card["render_policy"] == "none"


def test_pending_field_is_null_in_effective_context_but_history_is_unchanged() -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    projected = apply_claim_projection_to_prefix_bundle_v1(
        bundle=bundle,
        projection=_pending_projection(bundle),
    )
    assert projected["claim_cards"] == bundle["claim_cards"]
    context = projected["b0_context_cards"][0]
    assert context["effective_claims"]["identity_summary"] is None
    assert context["disputed_claims"][0]["historical_value"]
    assert context["authority_scope"] == CHAPTER_CONFIRMED_SCOPE
    next_inputs = b0_inputs_from_prefix_bundle_v1(projected)
    assert next_inputs["prior_cards"] == []
    demoted = next(
        row
        for row in next_inputs["candidate_only_context_cards"]
        if row["prior_card_id"] == context["prior_card_id"]
    )
    assert demoted["effective_claims"]["referent_kind"] == "person"
    assert demoted["effective_claims"]["identity_summary"] is None


def test_projection_hash_uses_canonical_prior_card_ordering() -> None:
    inventory = _inventory()
    entity = inventory["entity_candidates"][0]
    entity["referent_kind_claim"]["support_block_ids"] = ["bk_ch01_b002"]
    entity["referential_gender_claim"]["support_block_ids"] = ["bk_ch01_b002"]
    body = dict(inventory)
    body.pop("conflict_audited_inventory_hash")
    inventory["conflict_audited_inventory_hash"] = canonical_hash(body)
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    assert [
        row["block_id"] for row in bundle["claim_cards"][0]["provenance_refs"]
    ] == ["bk_ch01_b002", "bk_ch01_b001"]

    projection = _pending_projection(bundle)
    normalized = validate_prior_cards(bundle["claim_cards"])[0]
    projected = projection["projected_prior_cards"][0]
    projected["original_prior_card"] = normalized
    projected["source_prior_card_hash"] = canonical_hash(normalized)
    projection_body = dict(projection)
    projection_body.pop("projection_hash")
    projection["projection_hash"] = canonical_hash(projection_body)

    applied = apply_claim_projection_to_prefix_bundle_v1(
        bundle=bundle,
        projection=projection,
    )
    assert applied["claim_cards"] == bundle["claim_cards"]
    assert applied["b0_context_cards"][0]["effective_claims"][
        "identity_summary"
    ] is None


def test_identity_pending_moves_card_to_candidate_only_without_deleting_history() -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    projected = apply_claim_projection_to_prefix_bundle_v1(
        bundle=bundle,
        projection=_pending_projection(bundle, identity_pending=True),
    )
    assert projected["b0_context_cards"] == []
    assert len(projected["claim_cards"]) == 1
    card = next(
        row
        for row in projected["candidate_only_context_cards"]
        if row["prior_card_id"] == projected["claim_cards"][0]["prior_card_id"]
    )
    assert card["authority_scope"] == CANDIDATE_ONLY_SCOPE
    assert card["disputed_claims"][0]["disputed_field"] == "identity_membership"


def test_projection_updates_only_supplied_cards_and_preserves_other_context() -> None:
    inventory = _inventory()
    inventory["entity_candidates"].append(
        _entity("local_north", "North House", "bk_ch01_b001")
    )
    body = dict(inventory)
    body.pop("conflict_audited_inventory_hash")
    inventory["conflict_audited_inventory_hash"] = canonical_hash(body)
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    target = next(
        row for row in bundle["claim_cards"] if row["canonical_surface"] == "Mr. Vale"
    )
    projected = apply_claim_projection_to_prefix_bundle_v1(
        bundle=bundle,
        projection=_pending_projection(
            bundle, prior_card_id=target["prior_card_id"]
        ),
    )
    assert len(projected["claim_cards"]) == 2
    assert {
        row["canonical_surface"] for row in projected["b0_context_cards"]
    } == {"Mr. Vale", "North House"}
    untouched = next(
        row
        for row in projected["b0_context_cards"]
        if row["canonical_surface"] == "North House"
    )
    assert untouched["disputed_claims"] == []


def test_repeated_projections_preserve_prior_disputes_and_are_idempotent() -> None:
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    card_id = bundle["claim_cards"][0]["prior_card_id"]
    first = apply_claim_projection_to_prefix_bundle_v1(
        bundle=bundle,
        projection=_pending_projection(bundle, prior_card_id=card_id),
    )
    gender_projection = _pending_projection(
        bundle,
        disputed_field="referential_gender",
        prior_card_id=card_id,
    )
    second = apply_claim_projection_to_prefix_bundle_v1(
        bundle=first,
        projection=gender_projection,
    )
    context = next(
        row for row in second["b0_context_cards"] if row["prior_card_id"] == card_id
    )
    assert context["effective_claims"]["identity_summary"] is None
    assert context["effective_claims"]["referential_gender"] is None
    assert {row["disputed_field"] for row in context["disputed_claims"]} == {
        "identity_summary",
        "referential_gender",
    }
    replay = apply_claim_projection_to_prefix_bundle_v1(
        bundle=second,
        projection=gender_projection,
    )
    assert replay["prefix_bundle_hash"] == second["prefix_bundle_hash"]


def test_tampered_inventory_and_foreign_block_fail_closed() -> None:
    tampered = _inventory()
    tampered["entity_candidates"][0]["identity_summary_draft"] = "tampered"
    with pytest.raises(ChapterPrefixPriorError, match="hash mismatch"):
        build_chapter_prefix_prior_bundle_v1(
            document=_document(),
            audited_inventory=tampered,
            coverage_through_chapter_id="bk_ch01",
        )
    foreign = _inventory()
    foreign["entity_candidates"][0]["source_block_ids"] = ["bk_ch02_b001"]
    body = dict(foreign)
    body.pop("conflict_audited_inventory_hash")
    foreign["conflict_audited_inventory_hash"] = canonical_hash(body)
    with pytest.raises(ChapterPrefixPriorError, match="foreign source blocks"):
        build_chapter_prefix_prior_bundle_v1(
            document=_document(),
            audited_inventory=foreign,
            coverage_through_chapter_id="bk_ch01",
        )


def test_dry_envelope_consumes_prefix_bundle_without_full_registry_dump(
    tmp_path: Path,
) -> None:
    document = _document()
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    document_path = tmp_path / "document.json"
    bundle_path = tmp_path / "prefix_bundle.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    (
        envelope,
        request,
        chapter,
        priors,
        candidates,
        glossaries,
        _review_cases,
        manifest,
        _schema,
    ) = build_envelope(
        document_path=document_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=bundle_path,
        corruption_manifest_path=None,
    )
    assert chapter["chapter_id"] == "bk_ch02"
    assert [row["canonical_surface"] for row in priors] == ["Mr. Vale"]
    assert {row["canonical_surface"] for row in candidates} == {"Alder", "the hound"}
    assert glossaries == []
    packet_surfaces = {
        row["candidate_only_card"]["canonical_surface"]
        for row in request.sections["candidate_only_surface_hits"]
    }
    assert packet_surfaces == {"the hound"}
    assert all(
        set(row) == {"block_id", "text"}
        for row in request.sections["source_blocks"]
    )
    prior_view = request.sections["supplied_prior_packets"][0]["prior_card"]
    assert set(prior_view) == {
        "prior_card_id",
        "canonical_surface",
        "stable_surfaces",
        "referent_kind",
        "referential_gender",
        "identity_summary",
    }
    candidate_view = request.sections["candidate_only_surface_hits"][0][
        "candidate_only_card"
    ]
    assert set(candidate_view) == {
        "prior_card_id",
        "canonical_surface",
        "stable_surfaces",
        "effective_claims",
        "disputed_claims",
    }
    assert "provenance_refs" not in candidate_view
    assert "context_card_hash" not in candidate_view
    assert envelope["prior_input"]["input_kind"] == "chapter_prefix_prior_bundle"
    assert manifest is None


def test_prior_envelope_binds_openai_structured_contract_provider(
    tmp_path: Path,
) -> None:
    document = _document()
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    document_path = tmp_path / "document.json"
    bundle_path = tmp_path / "prefix_bundle.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    envelope, *_rest = build_envelope(
        document_path=document_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=bundle_path,
        corruption_manifest_path=None,
        model_id="gpt-5.4",
        structured_output_contract=_openai_prior_contract(),
    )

    assert envelope["provider"] == "openai"
    assert envelope["structured_output_contract"]["provider"] == "openai"


def test_prior_live_routes_openai_credential_to_structured_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document()
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    document_path = tmp_path / "document.json"
    bundle_path = tmp_path / "prefix_bundle.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    contract = _openai_prior_contract()
    envelope, *_rest = build_envelope(
        document_path=document_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=bundle_path,
        corruption_manifest_path=None,
        model_id="gpt-5.4",
        structured_output_contract=contract,
    )
    observed: dict = {}

    class AdapterReached(RuntimeError):
        pass

    def _probe_adapter(**kwargs):
        observed.update(kwargs)
        raise AdapterReached("provider-neutral adapter reached")

    monkeypatch.setattr(
        prior_runner, "call_openai_compatible_structured_v1", _probe_adapter
    )
    credential = ResolvedCredential(
        quota_bucket_id="synthetic-openai-bucket",
        provider="openai",
        credential_revision="synthetic-v1",
        commitment="synthetic-commitment",
        source_path=tmp_path / "secret.txt",
        nonempty_line=1,
        base_url=None,
        request_timeout_ms=10_000,
        secret="synthetic-test-secret",
    )

    with pytest.raises(AdapterReached, match="provider-neutral adapter reached"):
        prior_runner.run_live(
            document_path=document_path,
            design_doc=DESIGN_DOC,
            frozen_db=prior_runner.DEFAULT_FROZEN_DB,
            chapter_id="bk_ch02",
            prior_cards_path=None,
            prior_bundle_path=bundle_path,
            corruption_manifest_path=None,
            output_dir=tmp_path / "live",
            approved_envelope_hash=envelope["envelope_hash"],
            keys_file=None,
            quota_bucket_id="synthetic-openai-bucket",
            usage_roots=[],
            resolved_credential=credential,
            allowed_quota_bucket_ids=["synthetic-openai-bucket"],
            provider_profile_hash="synthetic-profile-hash",
            model_id="gpt-5.4",
            structured_output_contract=contract,
        )

    assert observed["credential"] == credential
    assert observed["contract"] == contract
    assert observed["schema_name"] == "literary_registry_b0_prior_v1"


def test_dry_envelope_surface_filters_glossary_context(tmp_path: Path) -> None:
    document = _document()
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_inventory_with_glossary(),
        coverage_through_chapter_id="bk_ch01",
    )
    document_path = tmp_path / "document.json"
    bundle_path = tmp_path / "prefix_bundle.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    (
        _envelope,
        request,
        _chapter_row,
        _priors,
        _candidates,
        glossaries,
        _review_cases,
        _manifest,
        _schema,
    ) = build_envelope(
        document_path=document_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=bundle_path,
        corruption_manifest_path=None,
    )
    assert len(glossaries) == 1
    assert request.sections["supplied_glossary_packets"] == []

    matching_document = deepcopy(document)
    matching_document["chapters"][1]["blocks"][0][
        "clean_text"
    ] = "Vale returned to North House, and the hound followed."
    matching_bundle = build_chapter_prefix_prior_bundle_v1(
        document=matching_document,
        audited_inventory=_inventory_with_glossary(),
        coverage_through_chapter_id="bk_ch01",
    )
    matching_path = tmp_path / "matching_document.json"
    matching_bundle_path = tmp_path / "matching_prefix_bundle.json"
    matching_path.write_text(json.dumps(matching_document), encoding="utf-8")
    matching_bundle_path.write_text(json.dumps(matching_bundle), encoding="utf-8")
    (
        _envelope,
        matching_request,
        _chapter_row,
        _priors,
        _candidates,
        _glossaries,
        _review_cases,
        _manifest,
        _schema,
    ) = build_envelope(
        document_path=matching_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=matching_bundle_path,
        corruption_manifest_path=None,
    )
    packets = matching_request.sections["supplied_glossary_packets"]
    assert len(packets) == 1
    assert packets[0]["glossary_card"]["surface"] == "North House"
    assert set(packets[0]["glossary_card"]) == {
        "glossary_card_id",
        "surface",
        "stable_surfaces",
        "category_claim",
        "local_sense",
        "preferred_rendering_vi",
        "render_policy",
        "lifecycle_state",
    }
    assert packets[0]["current_surface_hits"] == [
        {"surface": "North House", "current_block_ids": ["bk_ch02_b001"]}
    ]


def test_extension_never_auto_merges_same_surface_across_chapters() -> None:
    first = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    extended = extend_chapter_prefix_prior_bundle_v1(
        bundle=first,
        document=_document(),
        audited_inventory=_second_inventory_with_surface_collision(),
        next_chapter_id="bk_ch02",
    )
    assert extended["covered_chapter_ids"] == ["bk_ch01", "bk_ch02"]
    assert len(extended["claim_cards"]) == 2
    assert extended["b0_context_cards"] == []
    collision = extended["prefix_identity_uncertainties"][0]
    assert collision["reason_code"] == "cross_chapter_surface_collision"
    assert len(collision["prior_card_ids"]) == 2
    assert all(
        row["authority_scope"] == CANDIDATE_ONLY_SCOPE
        for row in extended["candidate_only_context_cards"]
        if row["prior_card_id"] in collision["prior_card_ids"]
    )

    tampered = deepcopy(extended)
    tampered["prefix_identity_uncertainties"][0]["status"] = "resolved"
    body = dict(tampered)
    body.pop("prefix_bundle_hash")
    tampered["prefix_bundle_hash"] = canonical_hash(body)
    with pytest.raises(ChapterPrefixPriorError, match="unsafe status"):
        verify_chapter_prefix_prior_bundle_v1(tampered, document=_document())


def test_completed_extension_appends_identity_uncertainty_once_to_review_ledger() -> None:
    first = build_chapter_prefix_prior_bundle_v1(
        document=_document(),
        audited_inventory=_inventory(),
        coverage_through_chapter_id="bk_ch01",
    )
    extended = extend_chapter_prefix_prior_bundle_v1(
        bundle=first,
        document=_document(),
        audited_inventory=_second_inventory_with_surface_collision(),
        next_chapter_id="bk_ch02",
    )
    ledger_body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": first["state_lineage_id"],
        "coverage_through_chapter_id": "bk_ch01",
        "observed_queue_hashes": ["e" * 64],
        "review_items": [],
        "production_publish_performed": False,
    }
    ledger = {**ledger_body, "review_ledger_hash": canonical_hash(ledger_body)}
    appended = append_prefix_identity_uncertainties_v1(
        ledger=ledger,
        prefix_bundle=extended,
        chapter_id="bk_ch02",
    )
    replay = append_prefix_identity_uncertainties_v1(
        ledger=appended,
        prefix_bundle=extended,
        chapter_id="bk_ch02",
    )
    assert replay == appended
    assert len(appended["review_items"]) == 1
    row = appended["review_items"][0]
    assert row["route"] == "book_identity_auditor"
    assert row["authority_effect"] == "none"
    assert set(row["subject_prior_card_ids"]) == set(
        extended["prefix_identity_uncertainties"][0]["prior_card_ids"]
    )
