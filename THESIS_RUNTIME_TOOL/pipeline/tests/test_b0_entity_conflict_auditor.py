from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b0_entity_conflict_auditor import (
    PROMPT_ID,
    build_identity_conflict_manifest,
    entity_conflict_response_schema,
    normalize_source_boundary_violations,
    render_entity_conflict_request,
    validate_and_apply_conflict_response,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "order_index": 1,
                "clean_text": "Mr. Vale entered the hall and signed Vale beneath the note.",
            },
            {
                "block_id": "bk_ch01_b002",
                "order_index": 2,
                "clean_text": "Clara Vale answered that Vale was also her family name.",
            },
            {
                "block_id": "bk_ch01_b003",
                "order_index": 3,
                "clean_text": "The two visitors continued their separate accounts.",
            },
            {
                "block_id": "bk_ch01_b004",
                "order_index": 4,
                "clean_text": "North House stood beyond the empty field.",
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
    canonical: str,
    name_class: str | None,
    block_id: str,
    alternatives: list[tuple[str, str]],
    *,
    kind: str = "person",
) -> dict:
    alternative_names = [
        {"surface": surface, "name_class": cls, "source_block_ids": [block_id]}
        for surface, cls in alternatives
    ]
    return {
        "candidate_id": candidate_id,
        "canonical_surface": canonical,
        "surface_status": "located",
        "canonical_name_class": name_class,
        "alternative_names": alternative_names,
        "name_locations": [
            {
                "surface": canonical,
                "name_class": name_class,
                "source_block_ids": [block_id],
            },
            *alternative_names,
        ],
        "source_block_ids": [block_id],
        "referent_kind_claim": _claim(kind, block_id),
        "referential_gender_claim": (
            _claim("unknown", block_id) if kind == "person" else None
        ),
        "identity_summary_draft": f"Stable description for {canonical}.",
        "identity_summary_status": "unreviewed",
        "publication_state": "pending_auditor",
        "audit_reasons": [],
    }


def _inventory() -> dict:
    body = {
        "schema_version": "fixture_inventory_v1",
        "chapter_id": "bk_ch01",
        "request_fingerprint": "req_inventory",
        "entity_candidates": [
            _entity(
                "ent_a",
                "Mr. Vale",
                "title_plus_name",
                "bk_ch01_b001",
                [("Vale", "proper_name")],
            ),
            _entity(
                "ent_b",
                "Clara Vale",
                "proper_name",
                "bk_ch01_b002",
                [("Vale", "proper_name")],
            ),
            _entity(
                "ent_place",
                "North House",
                "proper_name",
                "bk_ch01_b004",
                [],
                kind="place",
            ),
        ],
        "glossary_candidates": [],
        "chapter_priority_order": [],
        "unresolved_referents": [],
        "validation_report": {},
    }
    return {**body, "inventory_hash": canonical_hash(body)}


def _inventory_with_glossary() -> dict:
    inventory = deepcopy(_inventory())
    inventory.pop("inventory_hash")
    inventory["glossary_candidates"] = [
        {
            "candidate_id": "gloss_north_house",
            "surface": "North House",
            "category_claim": "place_term",
            "short_description": "A locally significant named residence.",
            "source_block_ids": ["bk_ch01_b004"],
            "surface_match_block_ids": ["bk_ch01_b004"],
            "address_validation_state": "valid",
            "publication_state": "pending_auditor",
        }
    ]
    return {**inventory, "inventory_hash": canonical_hash(inventory)}


def _response_with_glossary(
    inventory: dict,
    *,
    action: str,
    preferred_rendering_vi: str | None = None,
) -> dict:
    response = _response(inventory)
    response["glossary_dispositions"] = [
        {
            "candidate_id": "gloss_north_house",
            "action": action,
            "category_update": None,
            "local_sense_update": None,
            "preferred_rendering_vi": preferred_rendering_vi,
            "render_policy": (
                "advisory_meaning" if action == "confirm_chapter" else "none"
            ),
            "source_block_ids": ["bk_ch01_b004"],
            "resolution_note": "The chapter evidence supports this lifecycle decision.",
        }
    ]
    return response


def _response(inventory: dict) -> dict:
    manifest = build_identity_conflict_manifest(inventory, _chapter())
    assert len(manifest["components"]) == 1
    component = manifest["components"][0]
    surface_actions = []
    for surface in component["contested_surfaces"]:
        if surface["surface_key"] == "mr. vale":
            surface_actions.append(
                {
                    "surface_key": surface["surface_key"],
                    "action": "bind_global",
                    "target_candidate_id": "ent_a",
                    "source_block_ids": ["bk_ch01_b001"],
                    "resolution_note": "The supplied titled form identifies the first candidate.",
                }
            )
        else:
            surface_actions.append(
                {
                    "surface_key": surface["surface_key"],
                    "action": "quarantine",
                    "target_candidate_id": None,
                    "source_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
                    "resolution_note": "The shared family surface cannot identify one candidate.",
                }
            )
    return {
        "chapter_id": "bk_ch01",
        "component_decisions": [
            {
                "component_id": component["component_id"],
                "candidate_actions": [
                    {
                        "candidate_id": "ent_a",
                        "action": "keep",
                        "target_candidate_id": None,
                        "selected_canonical_surface": "Mr. Vale",
                        "source_block_ids": ["bk_ch01_b001"],
                        "resolution_note": "The titled surface is separately supported.",
                    },
                    {
                        "candidate_id": "ent_b",
                        "action": "keep",
                        "target_candidate_id": None,
                        "selected_canonical_surface": "Clara Vale",
                        "source_block_ids": ["bk_ch01_b002"],
                        "resolution_note": "The full supplied name identifies another person.",
                    },
                ],
                "surface_actions": surface_actions,
            }
        ],
        "glossary_dispositions": [],
    }


def test_prompt_is_book_neutral_and_has_no_profile_rewrite_power() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    lowered = prompt.casefold()
    for forbidden in ("heathcliff", "catherine", "linton", "gatsby", "madam"):
        assert forbidden not in lowered
    assert "selected_canonical_surface" in prompt
    assert "never add" in lowered
    assert "identity_summary_update" not in prompt
    assert "glossary_dispositions" in prompt
    assert "advisory_meaning" in prompt


def test_manifest_builds_one_conflict_and_leaves_clean_candidate_out() -> None:
    manifest = build_identity_conflict_manifest(_inventory(), _chapter())
    assert manifest["clean_candidate_ids"] == ["ent_place"]
    assert set(manifest["conflict_candidate_ids"]) == {"ent_a", "ent_b"}
    assert len(manifest["components"]) == 1
    component = manifest["components"][0]
    assert {row["surface_key"] for row in component["contested_surfaces"]} == {
        "mr. vale",
        "vale",
    }


def test_manifest_preserves_first_and_last_candidate_surface_witnesses() -> None:
    chapter = {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": f"bk_ch01_b{index:03d}",
                "order_index": index,
                "clean_text": (
                    "The Grange was mentioned here."
                    if index in {2, 7}
                    else f"Context block {index}."
                ),
            }
            for index in range(1, 9)
        ],
    }
    candidate = _entity(
        "ent_grange",
        "The Grange",
        None,
        "bk_ch01_b002",
        [],
        kind="place",
    )
    candidate["name_locations"][0]["source_block_ids"] = [
        "bk_ch01_b002",
        "bk_ch01_b007",
    ]
    candidate["source_block_ids"] = ["bk_ch01_b002", "bk_ch01_b007"]
    candidate["audit_reasons"] = ["unnamed_candidate_scope_review_required"]
    body = {
        "schema_version": "fixture_inventory_v1",
        "chapter_id": "bk_ch01",
        "request_fingerprint": "req_two_witnesses",
        "entity_candidates": [candidate],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "validation_report": {},
    }
    inventory = {**body, "inventory_hash": canonical_hash(body)}

    manifest = build_identity_conflict_manifest(inventory, chapter)

    assert len(manifest["components"]) == 1
    assert manifest["components"][0]["allowed_source_block_ids"] == [
        "bk_ch01_b001",
        "bk_ch01_b002",
        "bk_ch01_b003",
        "bk_ch01_b006",
        "bk_ch01_b007",
        "bk_ch01_b008",
    ]


def test_unaddressed_candidate_is_deferred_without_rendering_an_empty_component() -> None:
    inventory = _inventory()
    pending = _entity(
        "ent_unaddressed",
        "Unseen Label",
        "proper_name",
        "bk_ch01_b003",
        [],
    )
    pending["surface_status"] = "unlocated_pending_repair"
    pending["name_locations"][0].update(
        {
            "source_block_ids": [],
            "proposed_support_block_ids": ["foreign_b001"],
            "surface_match_block_ids": [],
            "address_validation_state": "foreign_block_removed",
            "ownership_state": "single_candidate_claim",
        }
    )
    pending["source_block_ids"] = []
    pending["address_state"] = "pending_source_repair"
    pending["publication_state"] = "pending_source_repair"
    pending["audit_reasons"] = ["entity_source_address_review_required"]
    inventory["entity_candidates"] = [
        next(
            row
            for row in inventory["entity_candidates"]
            if row["candidate_id"] == "ent_place"
        ),
        pending,
    ]
    body = dict(inventory)
    body.pop("inventory_hash", None)
    inventory["inventory_hash"] = canonical_hash(body)

    manifest = build_identity_conflict_manifest(inventory, _chapter())

    assert manifest["components"] == []
    assert manifest["clean_candidate_ids"] == ["ent_place"]
    assert manifest["deferred_source_repair_candidate_ids"] == ["ent_unaddressed"]
    result = validate_and_apply_conflict_response(
        {
            "chapter_id": "bk_ch01",
            "component_decisions": [],
            "glossary_dispositions": [],
        },
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_deferred_source",
    )
    assert [row["candidate_id"] for row in result["entity_candidates"]] == [
        "ent_place"
    ]
    assert [row["candidate_id"] for row in result["deferred_source_repairs"]] == [
        "ent_unaddressed"
    ]
    assert result["conflict_summary"]["deferred_source_repair_count"] == 1


def test_lowercase_descriptor_mislabeled_as_name_is_sent_to_scope_review() -> None:
    inventory = _inventory()
    inventory["entity_candidates"].append(
        _entity(
            "ent_descriptor",
            "separate accounts",
            "proper_name",
            "bk_ch01_b003",
            [],
        )
    )
    manifest = build_identity_conflict_manifest(inventory, _chapter())
    assert "ent_descriptor" in manifest["conflict_candidate_ids"]
    component = next(
        row for row in manifest["components"] if "ent_descriptor" in row["candidate_ids"]
    )
    assert "canonical_scope_review" in component["issue_codes"]


def test_render_contains_only_component_blocks_and_no_glossary() -> None:
    request = render_entity_conflict_request(
        chapter=_chapter(), inventory=_inventory(), design_doc=DESIGN_DOC
    )
    rendered = "\n".join(row["content"] for row in request.messages)
    assert set(request.sections) == {
        "source_blocks",
        "identity_conflict_components",
        "glossary_review",
    }
    assert "source_blocks" not in request.sections["identity_conflict_components"][0]
    assert "source_blocks" not in request.sections["glossary_review"]
    assert len(
        {
            row["block_id"]
            for row in request.sections["source_blocks"]
        }
    ) == len(request.sections["source_blocks"])
    assert "Mr. Vale entered" in rendered
    assert "Clara Vale answered" in rendered
    assert "North House stood" not in rendered
    assert "field-word" not in rendered
    assert "gold" not in request.messages[1]["content"].casefold()
    assert entity_conflict_response_schema()["additionalProperties"] is False


def test_glossary_confirm_is_chapter_scoped_and_rendering_is_advisory() -> None:
    inventory = _inventory_with_glossary()
    result = validate_and_apply_conflict_response(
        _response_with_glossary(
            inventory,
            action="confirm_chapter",
            preferred_rendering_vi="Bắc Trang",
        ),
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_glossary_confirm",
    )
    assert result["pending_glossary_candidates"] == []
    assert result["dormant_glossary_candidates"] == []
    row = result["glossary_candidates"][0]
    assert row["lifecycle_state"] == "chapter_confirmed"
    assert row["authority_scope"] == "chapter_confirmed"
    assert row["preferred_rendering_vi"] == "Bắc Trang"
    assert row["render_policy"] == "advisory_meaning"


def test_cross_component_source_citations_downscope_without_guessing() -> None:
    inventory = _inventory_with_glossary()
    response = _response_with_glossary(
        inventory,
        action="confirm_chapter",
        preferred_rendering_vi="Bac Trang",
    )
    component = response["component_decisions"][0]
    component["candidate_actions"][0]["source_block_ids"] = [
        "bk_ch01_b001",
        "bk_ch01_b004",
    ]
    component["surface_actions"][0]["source_block_ids"] = ["bk_ch01_b004"]
    response["glossary_dispositions"][0]["source_block_ids"] = ["bk_ch01_b001"]

    with pytest.raises(ValueError, match="outside its component"):
        validate_and_apply_conflict_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_strict_boundary",
        )

    normalized, records = normalize_source_boundary_violations(
        response,
        chapter=_chapter(),
        inventory=inventory,
    )
    result = validate_and_apply_conflict_response(
        normalized,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_fail_soft_boundary",
        source_boundary_normalizations=records,
    )

    assert len(records) == 3
    assert result["conflict_summary"]["source_boundary_normalization_count"] == 3
    assert result["component_decisions"][0]["candidate_actions"][0]["action"] == (
        "keep_pending"
    )
    assert result["component_decisions"][0]["surface_actions"][0]["action"] == (
        "quarantine"
    )
    assert result["pending_glossary_candidates"][0]["render_policy"] == "none"
    assert result["pending_glossary_candidates"][0]["preferred_rendering_vi"] is None


def test_nullable_glossary_enum_normalizes_only_serialized_null() -> None:
    inventory = _inventory_with_glossary()
    response = _response_with_glossary(
        inventory,
        action="confirm_chapter",
        preferred_rendering_vi="Bac Trang",
    )
    response["glossary_dispositions"][0]["category_update"] = "null"
    result = validate_and_apply_conflict_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_glossary_serialized_null",
    )
    assert result["glossary_candidates"][0]["category_claim"] == "place_term"

    invalid = deepcopy(response)
    invalid["glossary_dispositions"][0]["category_update"] = "named_place"
    with pytest.raises(ValueError, match="outside the closed enum"):
        validate_and_apply_conflict_response(
            invalid,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_glossary_foreign_enum",
        )


@pytest.mark.parametrize("action", ["keep_pending", "reject_dormant"])
def test_nonconfirmed_glossary_is_retained_without_rendering_authority(
    action: str,
) -> None:
    inventory = _inventory_with_glossary()
    response = _response_with_glossary(inventory, action=action)
    result = validate_and_apply_conflict_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint=f"req_glossary_{action}",
    )
    table = (
        "pending_glossary_candidates"
        if action == "keep_pending"
        else "dormant_glossary_candidates"
    )
    row = result[table][0]
    assert row["preferred_rendering_vi"] is None
    assert row["render_policy"] == "none"
    assert row["authority_scope"] in {"candidate_only", "dormant"}


def test_glossary_review_requires_exact_cover_and_downscopes_pending_rendering() -> None:
    inventory = _inventory_with_glossary()
    with pytest.raises(ValueError, match="exact-cover"):
        validate_and_apply_conflict_response(
            _response(inventory),
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_glossary_missing",
        )
    response = _response_with_glossary(inventory, action="keep_pending")
    response["glossary_dispositions"][0]["category_update"] = "place_term"
    response["glossary_dispositions"][0]["local_sense_update"] = "Unconfirmed sense."
    response["glossary_dispositions"][0]["preferred_rendering_vi"] = "unsafe"
    response["glossary_dispositions"][0]["render_policy"] = "advisory_meaning"
    result = validate_and_apply_conflict_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_glossary_unsafe",
    )
    disposition = result["glossary_dispositions"][0]
    assert disposition["category_update"] is None
    assert disposition["local_sense_update"] is None
    assert disposition["preferred_rendering_vi"] is None
    assert disposition["render_policy"] == "none"
    assert (
        result["conflict_summary"]["normalized_non_authoritative_glossary_count"]
        == 1
    )


def test_quarantine_removes_shared_alias_without_merging_entities() -> None:
    inventory = _inventory()
    request = render_entity_conflict_request(
        chapter=_chapter(), inventory=inventory, design_doc=DESIGN_DOC
    )
    result = validate_and_apply_conflict_response(
        _response(inventory),
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint=request.request_fingerprint,
    )
    assert result["conflict_summary"] == {
        "component_count": 1,
        "clean_candidate_count": 1,
        "kept_conflict_candidate_count": 2,
        "pending_candidate_count": 0,
        "deferred_source_repair_count": 0,
        "closed_candidate_count": 0,
        "quarantined_surface_count": 1,
        "global_surface_binding_count": 1,
        "confirmed_glossary_count": 0,
        "pending_glossary_count": 0,
        "dormant_glossary_count": 0,
        "normalized_non_authoritative_surface_count": 0,
        "normalized_non_authoritative_glossary_count": 0,
        "source_boundary_normalization_count": 0,
    }
    by_id = {row["candidate_id"]: row for row in result["entity_candidates"]}
    assert set(by_id) == {"ent_a", "ent_b", "ent_place"}
    assert by_id["ent_a"]["alternative_names"] == []
    assert by_id["ent_b"]["alternative_names"] == []
    assert result["quarantined_surfaces"][0]["surface_key"] == "vale"
    assert result["production_publish_performed"] is False


def test_canonical_label_can_remain_when_its_lookup_surface_is_quarantined() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["component_decisions"][0]["candidate_actions"][1][
        "selected_canonical_surface"
    ] = "Vale"
    result = validate_and_apply_conflict_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_conflict",
    )
    by_id = {row["candidate_id"]: row for row in result["entity_candidates"]}
    assert by_id["ent_b"]["canonical_surface"] == "Vale"
    assert {row["surface_key"] for row in result["quarantined_surfaces"]} == {"vale"}
    assert "vale" not in {
        row["surface_key"] for row in result["global_surface_bindings"]
    }


def test_invented_canonical_surface_is_fatal() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["component_decisions"][0]["candidate_actions"][0][
        "selected_canonical_surface"
    ] = "Mr. Rowan Vale"
    with pytest.raises(ValueError, match="supplied canonical"):
        validate_and_apply_conflict_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_conflict",
        )


def test_foreign_keep_surface_is_downscoped_without_canonical_authority() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["component_decisions"][0]["candidate_actions"][0][
        "selected_canonical_surface"
    ] = "a corrected source form"

    normalized, records = normalize_source_boundary_violations(
        response,
        chapter=_chapter(),
        inventory=inventory,
    )
    result = validate_and_apply_conflict_response(
        normalized,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_foreign_keep_surface",
        source_boundary_normalizations=records,
    )

    action = result["component_decisions"][0]["candidate_actions"][0]
    assert action["action"] == "keep_pending"
    assert action["selected_canonical_surface"] is None
    assert {
        row["normalization_kind"] for row in records
    } == {
        "candidate_canonical_surface_authority",
        "surface_target_authority",
    }
    surface_action = result["component_decisions"][0]["surface_actions"][0]
    assert surface_action["action"] == "quarantine"
    assert surface_action["target_candidate_id"] is None


def test_keep_pending_supplied_surface_is_normalized_without_authority() -> None:
    inventory = _inventory()
    response = _response(inventory)
    action = response["component_decisions"][0]["candidate_actions"][0]
    action.update(
        {
            "action": "keep_pending",
            "target_candidate_id": None,
            "selected_canonical_surface": "Mr. Vale",
        }
    )
    for surface_action in response["component_decisions"][0]["surface_actions"]:
        if surface_action["target_candidate_id"] == "ent_a":
            surface_action.update(
                {
                    "action": "quarantine",
                    "target_candidate_id": None,
                }
            )
    result = validate_and_apply_conflict_response(
        response,
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_pending_surface_normalization",
    )
    normalized = result["component_decisions"][0]["candidate_actions"][0]
    assert normalized["action"] == "keep_pending"
    assert normalized["selected_canonical_surface"] is None
    assert (
        result["conflict_summary"]["normalized_non_authoritative_surface_count"]
        == 1
    )


def test_keep_pending_foreign_surface_still_fails_closed() -> None:
    inventory = _inventory()
    response = _response(inventory)
    action = response["component_decisions"][0]["candidate_actions"][0]
    action.update(
        {
            "action": "keep_pending",
            "target_candidate_id": None,
            "selected_canonical_surface": "Foreign Name",
        }
    )
    with pytest.raises(ValueError, match="foreign canonical surface"):
        validate_and_apply_conflict_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_pending_foreign_surface",
        )


def test_candidate_and_surface_actions_must_exact_cover() -> None:
    inventory = _inventory()
    response = _response(inventory)
    response["component_decisions"][0]["candidate_actions"].pop()
    with pytest.raises(ValueError, match="exact-cover"):
        validate_and_apply_conflict_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_conflict",
        )
    response = _response(inventory)
    response["component_decisions"][0]["surface_actions"].pop()
    with pytest.raises(ValueError, match="exact-cover"):
        validate_and_apply_conflict_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_conflict",
        )


def test_merge_target_must_be_kept() -> None:
    inventory = _inventory()
    response = deepcopy(_response(inventory))
    actions = response["component_decisions"][0]["candidate_actions"]
    actions[0].update(
        {
            "action": "merge_into",
            "target_candidate_id": "ent_b",
            "selected_canonical_surface": None,
        }
    )
    actions[1].update(
        {
            "action": "keep_pending",
            "target_candidate_id": None,
            "selected_canonical_surface": None,
        }
    )
    with pytest.raises(ValueError, match="merge target must be kept"):
        validate_and_apply_conflict_response(
            response,
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_conflict",
        )


def test_source_validation_report_is_carried_without_authority() -> None:
    inventory = _inventory()
    inventory["validation_report"] = {
        "rejected_rows": [
            {
                "row_kind": "entity_candidate",
                "row_index": 7,
                "reason": "schema violation",
                "raw_row": {"candidate_id": "bad"},
                "lifecycle_state": "quarantined_contract_error",
            }
        ]
    }
    body = dict(inventory)
    body.pop("inventory_hash", None)
    inventory["inventory_hash"] = canonical_hash(body)

    result = validate_and_apply_conflict_response(
        _response(inventory),
        chapter=_chapter(),
        inventory=inventory,
        request_fingerprint="req_conflict",
    )

    assert result["source_validation_report"] == inventory["validation_report"]
    assert result["source_validation_report"] is not inventory["validation_report"]
    assert result["source_validation_report"]["rejected_rows"][0]["raw_row"] == {
        "candidate_id": "bad"
    }


def test_missing_source_validation_report_is_fatal() -> None:
    inventory = _inventory()
    inventory.pop("validation_report")
    body = dict(inventory)
    body.pop("inventory_hash", None)
    inventory["inventory_hash"] = canonical_hash(body)

    with pytest.raises(ValueError, match="source inventory validation_report"):
        validate_and_apply_conflict_response(
            _response(_inventory()),
            chapter=_chapter(),
            inventory=inventory,
            request_fingerprint="req_conflict",
        )
