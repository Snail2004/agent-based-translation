from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b0_entity_inventory_auditor_experiment import (
    PROMPT_ID,
    build_audit_case_manifest,
    entity_inventory_auditor_response_schema,
    render_inventory_auditor_request,
    validate_and_apply_auditor_response,
)
from pipeline.literary.b0_entity_inventory_experiment import (
    validate_entity_inventory_response,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.scripts.run_b0_entity_inventory_auditor_experiment import (
    InventoryAuditorExperimentError,
    _transport_config as auditor_transport_config,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def test_comparison_model_override_uses_matching_auditor_quota_gates() -> None:
    transport = auditor_transport_config("gpt-5.4-mini")
    assert transport.auditor_model_id == "gpt-5.4-mini"
    assert transport.role_quota_gate_ids["auditor"] == (
        "openai-row2-mini",
        "openai-row1-mini",
    )
    with pytest.raises(
        InventoryAuditorExperimentError, match="unsupported comparison model"
    ):
        auditor_transport_config("unregistered-model")


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "block_type": "paragraph",
                "order_index": 1,
                "clean_text": "Ms. Vale entered North House with Brindle, an old female hound.",
            },
            {
                "block_id": "bk_ch01_b002",
                "block_type": "paragraph",
                "order_index": 2,
                "clean_text": "The visitor called Brindle a good hound and used the local word stormfast.",
            },
            {
                "block_id": "bk_ch01_b003",
                "block_type": "paragraph",
                "order_index": 3,
                "clean_text": "A name, Rowan Vale, was carved over the inner door.",
            },
            {
                "block_id": "bk_ch01_b004",
                "block_type": "paragraph",
                "order_index": 4,
                "clean_text": "Rain crossed the empty northern field.",
            },
            {
                "block_id": "bk_ch01_b005",
                "block_type": "paragraph",
                "order_index": 5,
                "clean_text": "The visitor later explained why the house mattered.",
            },
        ],
    }


def _inventory() -> dict:
    raw = {
        "entity_candidates": [
            {
                "canonical_surface": "Ms. Vale",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "title_plus_name",
                "alternative_names": [
                    {
                        "surface": "Vale",
                        "name_class": "proper_name",
                        "support_block_ids": ["bk_ch01_b001"],
                    }
                ],
                "referent_kind_claim": {
                    "value": "person",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": {
                    "value": "feminine",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "identity_summary_draft": "Named visitor associated with North House.",
            },
            {
                "canonical_surface": "North House",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "place",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": {
                    "value": "neutral",
                    "basis": "contextual",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "identity_summary_draft": "A named house in the chapter.",
            },
        ],
        "glossary_candidates": [
            {
                "surface": "stormfast",
                "category_claim": "regional_term",
                "short_description": "A locally marked expression.",
                "support_block_ids": ["bk_ch01_b002"],
            }
        ],
        "unresolved_referents": [
            {
                "surface": "Rowan Vale",
                "referent_kind_claim": "person",
                "short_description": "A person named only on an inscription.",
                "issue": "identity_ambiguous",
                "support_block_ids": ["bk_ch01_b003"],
            }
        ],
        "chapter_priority_order": [],
    }
    return validate_entity_inventory_response(
        raw, _chapter(), request_fingerprint="req_b0_test"
    )


def _response(inventory: dict) -> dict:
    person = next(
        row for row in inventory["entity_candidates"] if row["canonical_surface"] == "Ms. Vale"
    )
    place = next(
        row
        for row in inventory["entity_candidates"]
        if row["canonical_surface"] == "North House"
    )
    glossary = inventory["glossary_candidates"][0]
    unresolved = inventory["unresolved_referents"][0]
    return {
        "chapter_id": "bk_ch01",
        "entity_decisions": [
            {
                "candidate_id": person["candidate_id"],
                "action": "confirm_as_is",
                "target_candidate_id": None,
                "canonical_surface_update": None,
                "canonical_name_class_operation": "keep",
                "canonical_name_class_value": None,
                "referent_kind_update": None,
                "referential_gender_operation": "keep",
                "referential_gender_value": None,
                "identity_summary_update": None,
                "retained_alternative_name_surfaces": ["Vale"],
                "publication_scope": "global",
                "source_block_ids": ["bk_ch01_b001"],
                "resolution_note": "The chapter explicitly presents this named person.",
            },
            {
                "candidate_id": place["candidate_id"],
                "action": "confirm_with_patch",
                "target_candidate_id": None,
                "canonical_surface_update": None,
                "canonical_name_class_operation": "keep",
                "canonical_name_class_value": None,
                "referent_kind_update": None,
                "referential_gender_operation": "clear",
                "referential_gender_value": None,
                "identity_summary_update": None,
                "retained_alternative_name_surfaces": [],
                "publication_scope": "global",
                "source_block_ids": ["bk_ch01_b001"],
                "resolution_note": "The place candidate is valid but carries no gender.",
            },
        ],
        "glossary_decisions": [
            {
                "candidate_id": glossary["candidate_id"],
                "action": "confirm_as_is",
                "target_candidate_id": None,
                "category_update": None,
                "short_description_update": None,
                "source_block_ids": ["bk_ch01_b002"],
                "resolution_note": "The source marks this as a local expression.",
            }
        ],
        "unresolved_decisions": [
            {
                "candidate_id": unresolved["candidate_id"],
                "action": "keep_dormant",
                "source_block_ids": ["bk_ch01_b003"],
                "resolution_note": "The inscription alone does not establish an active person.",
            }
        ],
        "additional_issue_tickets": [],
    }


def test_auditor_prompt_is_book_neutral_and_narrow() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    lowered = prompt.casefold()
    for forbidden in ("heathcliff", "lockwood", "wuthering", "gatsby", "madam"):
        assert forbidden not in lowered
    assert "identity_summary" in prompt
    assert "audit_case_manifest" in lowered
    assert "does not by itself establish" in lowered
    assert "gold" in lowered
    assert "no gold labels" in lowered


def test_render_contains_full_source_once_and_no_gold() -> None:
    inventory = _inventory()
    request = render_inventory_auditor_request(
        chapter=_chapter(), inventory=inventory, design_doc=DESIGN_DOC
    )
    assert request.role == "auditor"
    assert request.prompt_id == PROMPT_ID
    assert set(request.sections) == {
        "source_blocks",
        "entity_roster",
        "glossary_roster",
        "unresolved_roster",
        "audit_case_manifest",
        "routine_checklist",
    }
    rendered = "\n".join(row["content"] for row in request.messages)
    assert rendered.count("Ms. Vale entered North House") == 1
    assert "registry_gold" not in rendered
    assert request.response_schema_hash
    assert entity_inventory_auditor_response_schema()["additionalProperties"] is False


def test_case_manifest_flags_scope_context_and_glossary() -> None:
    cases = build_audit_case_manifest(_inventory(), _chapter())
    by_target = {row["target_candidate_id"]: row for row in cases}
    inventory = _inventory()
    place = next(
        row
        for row in inventory["entity_candidates"]
        if row["canonical_surface"] == "North House"
    )
    assert set(by_target[place["candidate_id"]]["issue_codes"]) == {
        "contextual_gender_check",
        "kind_gender_scope_conflict",
    }
    glossary = inventory["glossary_candidates"][0]
    assert by_target[glossary["candidate_id"]]["issue_codes"] == [
        "glossary_termhood_check"
    ]


def test_auditor_applies_typed_patches_without_production_publish() -> None:
    inventory = _inventory()
    request = render_inventory_auditor_request(
        chapter=_chapter(), inventory=inventory, design_doc=DESIGN_DOC
    )
    audited = validate_and_apply_auditor_response(
        _response(inventory),
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint=request.request_fingerprint,
    )
    assert audited["audit_summary"] == {
        "confirmed_entity_count": 2,
        "pending_entity_count": 0,
        "closed_entity_count": 0,
        "confirmed_glossary_count": 1,
        "pending_glossary_count": 0,
        "closed_glossary_count": 0,
        "retained_unresolved_count": 1,
        "closed_unresolved_count": 0,
        "additional_issue_count": 0,
        "extended_support_decision_count": 0,
        "extended_support_block_count": 0,
    }
    place = next(
        row
        for row in audited["entity_candidates"]
        if row["canonical_surface"] == "North House"
    )
    assert place["referential_gender_claim"] is None
    assert place["publication_state"] == "auditor_confirmed"
    assert audited["unresolved_referents"][0]["lifecycle_state"] == "dormant_unresolved"
    assert audited["production_publish_performed"] is False


def test_entity_decisions_must_exact_cover() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["entity_decisions"].pop()
    with pytest.raises(ValueError, match="exact-cover"):
        validate_and_apply_auditor_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_audit",
        )


def test_flagged_entity_cannot_be_confirmed_as_is() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["entity_decisions"][1].update(
        {
            "action": "confirm_as_is",
            "referential_gender_operation": "keep",
        }
    )
    with pytest.raises(ValueError, match="flagged entity"):
        validate_and_apply_auditor_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_audit",
        )


def test_non_gender_kind_cannot_keep_gender() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["entity_decisions"][1]["referential_gender_operation"] = "keep"
    with pytest.raises(ValueError, match="cannot carry referential gender"):
        validate_and_apply_auditor_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_audit",
        )


def test_decision_can_extend_to_another_supplied_chapter_block() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["entity_decisions"][0]["source_block_ids"] = ["bk_ch01_b005"]
    audited = validate_and_apply_auditor_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_audit",
    )
    decision = next(
        row
        for row in audited["entity_decisions"]
        if row["candidate_id"] == response["entity_decisions"][0]["candidate_id"]
    )
    assert decision["used_extended_chapter_support"] is True
    assert decision["extended_source_block_ids"] == ["bk_ch01_b005"]


def test_decision_cannot_cite_foreign_block() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["entity_decisions"][0]["source_block_ids"] = ["foreign_b999"]
    with pytest.raises(ValueError, match="outside the supplied chapter"):
        validate_and_apply_auditor_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_audit",
        )


def test_missing_candidate_ticket_requires_located_surface() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["additional_issue_tickets"] = [
        {
            "target_type": "chapter",
            "target_candidate_id": None,
            "issue_code": "missing_candidate",
            "surface": "Invented Stranger",
            "source_block_ids": ["bk_ch01_b001"],
            "note": "This should fail because the surface is absent.",
        }
    ]
    with pytest.raises(ValueError, match="not located"):
        validate_and_apply_auditor_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_audit",
        )


def test_open_promotion_ticket_does_not_publish_entity() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["unresolved_decisions"][0]["action"] = "open_promotion_ticket"
    audited = validate_and_apply_auditor_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_audit",
    )
    assert audited["unresolved_referents"][0]["lifecycle_state"] == "promotion_ticket_open"
    assert len(audited["entity_candidates"]) == 2
