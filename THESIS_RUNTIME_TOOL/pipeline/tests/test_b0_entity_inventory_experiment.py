from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.literary.b0_entity_inventory_experiment import (
    PROMPT_ID,
    entity_inventory_response_schema,
    evaluate_inventory_against_gold,
    render_entity_inventory_request,
    validate_entity_inventory_response,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.scripts.run_b0_entity_inventory_experiment import (
    EntityInventoryExperimentError,
    _transport_config as inventory_transport_config,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def test_comparison_model_override_uses_matching_b0_quota_gates() -> None:
    transport = inventory_transport_config("gpt-5.4-mini")
    assert transport.b0_model_id == "gpt-5.4-mini"
    assert transport.role_quota_gate_ids["b0"] == (
        "openai-row2-mini",
        "openai-row1-mini",
    )
    with pytest.raises(EntityInventoryExperimentError, match="unsupported comparison model"):
        inventory_transport_config("unregistered-model")


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
                "clean_text": "The hound waited while Vale explained the local word stormfast.",
            },
        ],
    }


def test_prompt_is_book_neutral_and_entity_focused() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    lowered = prompt.casefold()
    for forbidden in ("heathcliff", "lockwood", "wuthering", "gatsby", "madam"):
        assert forbidden not in lowered
    assert "chapter summary" in lowered
    assert "entity_candidates" in prompt
    assert "narrator analysis" in lowered
    assert "alternative_names" in prompt
    assert "support_block_ids" in prompt
    assert "identity_summary_draft" in prompt
    assert "need not repeat the canonical name" in lowered
    assert "confidence" in lowered
    assert "do not output" in lowered


def test_render_request_contains_only_source_and_no_gold() -> None:
    request = render_entity_inventory_request(chapter=_chapter(), design_doc=DESIGN_DOC)
    assert request.role == "b0"
    assert request.prompt_id == PROMPT_ID
    assert set(request.sections) == {"source_blocks"}
    rendered = "\n".join(row["content"] for row in request.messages)
    assert "registry_gold" not in rendered
    assert "Ms. Vale" in rendered
    assert request.response_schema_hash
    assert entity_inventory_response_schema()["additionalProperties"] is False


def test_validator_locates_surfaces_and_records_bad_rows() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "Ms. Vale",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "title_plus_name",
                "alternative_names": [
                    {
                        "surface": "Vale",
                        "name_class": "proper_name",
                        "support_block_ids": ["bk_ch01_b002"],
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
                "identity_summary_draft": "The named visitor associated with North House.",
            },
            {
                "canonical_surface": "Invented Person",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "person",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": None,
                "identity_summary_draft": "",
            },
            {
                "canonical_surface": "Brindle",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "animal",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": {
                    "value": "feminine",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "identity_summary_draft": "An individualized old hound.",
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
        "unresolved_referents": [],
        "chapter_priority_order": [
            {
                "surface": "Ms. Vale",
                "item_class": "new_entity",
                "source_block_id": "bk_ch01_b001",
            },
            {
                "surface": "Ms. Vale",
                "item_class": "new_entity",
                "source_block_id": "bk_ch01_b002",
            },
        ],
    }
    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_test"
    )
    assert len(inventory["entity_candidates"]) == 2
    vale = next(row for row in inventory["entity_candidates"] if row["canonical_surface"] == "Ms. Vale")
    assert vale["source_block_ids"] == ["bk_ch01_b001", "bk_ch01_b002"]
    assert vale["referent_kind_claim"]["value"] == "person"
    assert vale["referential_gender_claim"]["value"] == "feminine"
    assert vale["alternative_names"] == [
        {
            "surface": "Vale",
            "name_class": "proper_name",
            "source_block_ids": ["bk_ch01_b002"],
            "proposed_support_block_ids": ["bk_ch01_b002"],
            "address_validation_state": "valid",
            "address_issues": [],
            "surface_match_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            "ownership_state": "single_candidate_claim",
        }
    ]
    brindle = next(
        row for row in inventory["entity_candidates"] if row["canonical_surface"] == "Brindle"
    )
    assert brindle["source_block_ids"] == ["bk_ch01_b001"]
    assert inventory["validation_report"]["rejected_row_count"] == 1
    assert inventory["validation_report"]["claim_issue_count"] == 0
    assert inventory["glossary_candidates"][0]["source_block_ids"] == ["bk_ch01_b002"]
    assert inventory["chapter_priority_order"] == [
        {
            "rank": 1,
            "surface": "Ms. Vale",
            "item_class": "new_entity",
            "source_block_id": "bk_ch01_b001",
            "resolved_refs": [vale["candidate_id"]],
            "authority_effect": "none",
        }
    ]
    assert inventory["validation_report"]["priority_issue_count"] == 1


def test_semantic_claim_support_needs_only_a_valid_chapter_address() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "North House",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "place",
                    "basis": "contextual",
                    "support_block_ids": ["bk_ch01_b002"],
                },
                "referential_gender_claim": {
                    "value": "feminine",
                    "basis": "contextual",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "identity_summary_draft": "A named location in the chapter.",
            }
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }
    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_claim_degrade"
    )
    assert len(inventory["entity_candidates"]) == 1
    row = inventory["entity_candidates"][0]
    assert row["referent_kind_claim"] == {
        "value": "place",
        "basis": "contextual",
        "support_block_ids": ["bk_ch01_b002"],
        "proposed_support_block_ids": ["bk_ch01_b002"],
        "address_validation_state": "valid",
        "semantic_status": "unreviewed",
    }
    assert row["referential_gender_claim"]["value"] == "feminine"
    assert row["referential_gender_claim"]["semantic_status"] == "unreviewed"
    assert row["publication_state"] == "pending_auditor"
    assert row["audit_reasons"] == ["kind_gender_scope_conflict"]
    assert inventory["validation_report"]["claim_issue_count"] == 0
    assert inventory["validation_report"]["rejected_row_count"] == 0


def test_unlocated_canonical_surface_is_preserved_for_auditor_repair() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "the house visitor",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": None,
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "person",
                    "basis": "contextual",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": None,
                "identity_summary_draft": "An unnamed visitor associated with the house.",
            }
        ],
        "chapter_priority_order": [],
        "glossary_candidates": [],
        "unresolved_referents": [
            {
                "surface": "The hound",
                "referent_kind_claim": "animal",
                "short_description": "A locally described animal reference.",
                "issue": "surface_scope_uncertain",
                "support_block_ids": ["bk_ch01_b002"],
            }
        ],
    }
    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_surface_repair"
    )
    assert len(inventory["entity_candidates"]) == 1
    row = inventory["entity_candidates"][0]
    assert row["surface_status"] == "unlocated_pending_repair"
    assert row["source_block_ids"] == ["bk_ch01_b001"]
    assert row["name_locations"] == [
        {
            "surface": "the house visitor",
            "name_class": None,
            "source_block_ids": [],
            "proposed_support_block_ids": ["bk_ch01_b001"],
            "address_validation_state": "surface_absent_from_support",
            "address_issues": ["surface_absent_from_support"],
            "surface_match_block_ids": [],
            "ownership_state": "single_candidate_claim",
        }
    ]
    assert row["publication_state"] == "pending_auditor"
    assert row["audit_reasons"] == [
        "canonical_surface_repair_required",
        "canonical_surface_support_review_required",
        "unnamed_candidate_scope_review_required",
    ]
    assert inventory["validation_report"]["pending_surface_repair_count"] == 1
    assert inventory["validation_report"]["rejected_row_count"] == 0
    unresolved = inventory["unresolved_referents"][0]
    assert unresolved["lifecycle_state"] == "dormant_unresolved"
    assert unresolved["publication_state"] == "not_published"


def test_omitted_canonical_support_is_derived_from_exact_surface_hits() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "Ms. Vale",
                "canonical_name_class": "title_plus_name",
                "alternative_names": [],
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
                "identity_summary_draft": "The named visitor associated with North House.",
            }
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }

    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_omitted_support_exact"
    )

    assert inventory["validation_report"]["rejected_row_count"] == 0
    row = inventory["entity_candidates"][0]
    assert row["source_block_ids"] == ["bk_ch01_b001"]
    assert row["name_locations"][0]["address_validation_state"] == "valid"
    assert inventory["validation_report"][
        "canonical_surface_support_normalizations"
    ] == [
        {
            "row_index": 0,
            "surface": "Ms. Vale",
            "normalization_kind": "omitted_canonical_surface_support",
            "action": "derived_from_exact_surface_matches",
            "derived_block_ids": ["bk_ch01_b001"],
        }
    ]


def test_omitted_unlocated_support_stays_reviewable_instead_of_being_rejected() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "the unnamed visitor",
                "canonical_name_class": None,
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "person",
                    "basis": "contextual",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": None,
                "identity_summary_draft": "An unnamed visitor associated with the house.",
            }
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }

    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_omitted_support_unlocated"
    )

    assert inventory["validation_report"]["rejected_row_count"] == 0
    row = inventory["entity_candidates"][0]
    assert row["surface_status"] == "unlocated_pending_repair"
    assert row["publication_state"] == "pending_auditor"
    assert row["source_block_ids"] == ["bk_ch01_b001"]
    assert row["name_locations"][0]["address_validation_state"] == "missing_support"
    assert "canonical_surface_repair_required" in row["audit_reasons"]
    assert "canonical_surface_support_review_required" in row["audit_reasons"]
    assert inventory["validation_report"][
        "canonical_surface_support_normalizations"
    ][0]["action"] == "preserved_pending_source_review"


def test_foreign_claim_addresses_are_not_persisted_as_evidence() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "Brindle",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "animal",
                    "basis": "explicit",
                    "support_block_ids": ["foreign_b001"],
                },
                "referential_gender_claim": {
                    "value": "feminine",
                    "basis": "explicit",
                    "support_block_ids": ["foreign_b002"],
                },
                "identity_summary_draft": "An individualized hound.",
            }
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }
    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_foreign_claim"
    )
    row = inventory["entity_candidates"][0]
    assert row["referent_kind_claim"]["value"] == "animal"
    assert row["referent_kind_claim"]["support_block_ids"] == []
    assert row["referent_kind_claim"]["address_validation_state"] == "foreign_block_removed"
    assert row["referential_gender_claim"]["value"] == "feminine"
    assert row["referential_gender_claim"]["support_block_ids"] == []
    assert inventory["validation_report"]["claim_issue_count"] == 0
    assert inventory["validation_report"]["rejected_row_count"] == 0


def test_invalid_optional_name_metadata_is_dropped_not_entity() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "Brindle",
                "canonical_surface_support_block_ids": ["bk_ch01_b001"],
                "canonical_name_class": "null",
                "alternative_names": [
                    {
                        "surface": "Brindle",
                        "name_class": "stable_nickname",
                        "support_block_ids": ["bk_ch01_b001"],
                    },
                    {
                        "surface": "Absent Name",
                        "name_class": "proper_name",
                        "support_block_ids": ["bk_ch01_b002"],
                    },
                ],
                "referent_kind_claim": {
                    "value": "animal",
                    "basis": "explicit",
                    "support_block_ids": ["bk_ch01_b001"],
                },
                "referential_gender_claim": None,
                "identity_summary_draft": "An individualized hound.",
            }
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }
    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_optional_degrade"
    )
    row = inventory["entity_candidates"][0]
    assert row["canonical_name_class"] is None
    assert [name["surface"] for name in row["alternative_names"]] == ["Absent Name"]
    assert inventory["validation_report"]["canonical_name_class_issue_count"] == 1
    assert inventory["validation_report"]["alternative_name_issue_count"] == 1
    assert "alternative_name_support_review_required" in row["audit_reasons"]
    assert inventory["validation_report"]["rejected_row_count"] == 0


def test_response_schema_limits_claim_evidence_to_two_blocks() -> None:
    schema = entity_inventory_response_schema()
    entity = schema["properties"]["entity_candidates"]["items"]
    kind_blocks = entity["properties"]["referent_kind_claim"]["properties"][
        "support_block_ids"
    ]
    assert kind_blocks["minItems"] == 1
    assert kind_blocks["maxItems"] == 2
    assert entity["properties"]["alternative_names"]["type"] == "array"
    assert "identity_summary_draft" in entity["required"]


def test_lexical_matches_are_retrieval_only_and_shared_surface_is_flagged() -> None:
    chapter = {
        "chapter_id": "syn_ch01",
        "blocks": [
            {
                "block_id": f"syn_ch01_b{index:03d}",
                "order_index": index,
                "clean_text": f"Vale appears in account {index}.",
            }
            for index in range(1, 6)
        ],
    }
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "Vale",
                "canonical_surface_support_block_ids": [support],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "person",
                    "basis": "contextual",
                    "support_block_ids": [support],
                },
                "referential_gender_claim": None,
                "identity_summary_draft": summary,
            }
            for support, summary in (
                ("syn_ch01_b001", "The visitor introduced in the opening account."),
                ("syn_ch01_b005", "The official named in the closing account."),
            )
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }

    inventory = validate_entity_inventory_response(
        response, chapter, request_fingerprint="req_shared_surface"
    )

    assert len(inventory["entity_candidates"]) == 2
    expected_matches = [f"syn_ch01_b{index:03d}" for index in range(1, 6)]
    assert {
        tuple(row["name_locations"][0]["source_block_ids"])
        for row in inventory["entity_candidates"]
    } == {("syn_ch01_b001",), ("syn_ch01_b005",)}
    assert all(
        row["name_locations"][0]["surface_match_block_ids"] == expected_matches
        for row in inventory["entity_candidates"]
    )
    assert all(
        row["name_locations"][0]["ownership_state"] == "multi_candidate_claim"
        for row in inventory["entity_candidates"]
    )
    assert all(
        "surface_ownership_review_required" in row["audit_reasons"]
        for row in inventory["entity_candidates"]
    )


def test_invalid_surface_address_is_preserved_as_pending_not_published() -> None:
    response = {
        "entity_candidates": [
            {
                "canonical_surface": "Unseen Label",
                "canonical_surface_support_block_ids": ["foreign_b001"],
                "canonical_name_class": "proper_name",
                "alternative_names": [],
                "referent_kind_claim": {
                    "value": "person",
                    "basis": "contextual",
                    "support_block_ids": ["foreign_b001"],
                },
                "referential_gender_claim": None,
                "identity_summary_draft": "A referent whose source address needs repair.",
            }
        ],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }

    inventory = validate_entity_inventory_response(
        response, _chapter(), request_fingerprint="req_pending_address"
    )

    row = inventory["entity_candidates"][0]
    assert row["source_block_ids"] == []
    assert row["address_state"] == "pending_source_repair"
    assert row["publication_state"] == "pending_source_repair"
    assert row["name_locations"][0]["proposed_support_block_ids"] == ["foreign_b001"]
    assert row["name_locations"][0]["address_validation_state"] == "foreign_block_removed"
    assert row["name_locations"][0]["address_issues"] == ["foreign_block_removed"]
    assert inventory["validation_report"]["pending_source_repair_entity_count"] == 1


def test_gold_evaluator_is_generic_and_detects_merge_split_and_extra() -> None:
    inventory = {
        "inventory_hash": "inventory",
        "entity_candidates": [
            {
                "candidate_id": "p1",
                "canonical_surface": "Ms. Vale",
                "referent_kind_claim": "person",
                "source_locations": [{"surface": "Ms. Vale", "source_block_ids": ["b1"]}],
            },
            {
                "candidate_id": "p2",
                "canonical_surface": "Vale",
                "referent_kind_claim": "person",
                "source_locations": [{"surface": "Vale", "source_block_ids": ["b2"]}],
            },
            {
                "candidate_id": "extra",
                "canonical_surface": "the chair",
                "referent_kind_claim": "object",
                "source_locations": [{"surface": "the chair", "source_block_ids": ["b2"]}],
            },
        ],
        "glossary_candidates": [
            {"candidate_id": "g1", "surface": "stormfast"}
        ],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }
    gold = {
        "gold_id": "generic_gold",
        "gold_status": "draft",
        "required_confirmed_entities": [
            {
                "gold_entity_id": "vale",
                "canonical_surface": "Ms. Vale",
                "referent_kind": "person",
                "observed_name_surfaces": [{"surface": "Vale"}],
            }
        ],
        "required_pending_or_local_referents": [],
        "required_glossary_items": [
            {"gold_glossary_id": "stormfast", "surface": "stormfast"}
        ],
    }
    evaluation = evaluate_inventory_against_gold(inventory, gold)
    assert evaluation["confirmed_entity_recall"] == 1.0
    assert evaluation["wrong_split_count"] == 1
    assert evaluation["wrong_merge_count"] == 0
    assert evaluation["unmatched_entity_candidate_count"] == 1
    assert evaluation["glossary_recall"] == 1.0
