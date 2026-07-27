from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.book_entity_registry_prompts_v1 import prompt_manifest_v1
from pipeline.literary.book_entity_claim_overlay_v1 import (
    BookEntityClaimOverlayError,
    apply_stable_claim_overlay_to_global_registry_v1,
    build_book_entity_stable_claim_overlay_v1,
    verify_book_entity_stable_claim_overlay_v1,
)
from pipeline.literary.chapter_cycle_book_end_v1 import (
    ChapterCycleBookEndError,
    build_chapter_cycle_book_end_handoff_v1,
    render_chapter_cycle_book_end_requests_v1,
    seal_chapter_cycle_global_registry_v1,
    verify_chapter_cycle_book_end_handoff_v1,
    verify_chapter_cycle_book_end_request_set_v1,
    verify_sealed_chapter_cycle_global_registry_v1,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    REVIEW_LEDGER_SCHEMA_VERSION,
    REVIEW_LEDGER_VALIDATOR_VERSION,
)
from pipeline.literary.chapter_cycle_prefix_runner_v1 import (
    ChapterCyclePrefixRunnerError,
    assemble_chapter_cycle_prefix_v1,
    verify_chapter_cycle_prefix_assembly_v1,
)
from pipeline.literary.book_entity_registry_store_v1 import (
    BookEntityRegistryStoreV1,
    BookEntityStaleParentError,
    BookEntityStoreError,
    prepare_book_entity_generation_v1,
)
from pipeline.literary.book_entity_registry_v1 import (
    BookEntityContractError,
    build_book_entity_index_v1,
    build_global_entity_registry_v1,
    render_cross_chapter_request_v1,
    validate_cross_chapter_response_v1,
    verify_book_entity_index_v1,
    verify_global_entity_registry_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.chapter_prefix_prior_v1 import (
    apply_claim_projection_to_prefix_bundle_v1,
    build_chapter_prefix_prior_bundle_v1,
    extend_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    CLAIM_PROJECTION_SCHEMA_VERSION,
    CLAIM_VALIDATOR_VERSION,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def _document() -> dict:
    return {
        "document_id": "book_fixture",
        "chapters": [
            {
                "chapter_id": "bk_ch01",
                "blocks": [
                    {
                        "block_id": "bk_ch01_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Mr. Vale signed Vale beneath the note.",
                    },
                    {
                        "block_id": "bk_ch01_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "North House stood beyond the field.",
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
                        "clean_text": "Mr. Vale returned and used the family name Vale.",
                    },
                    {
                        "block_id": "bk_ch02_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "An unnamed visitor waited outside.",
                    },
                ],
            },
            {
                "chapter_id": "bk_ch03",
                "blocks": [
                    {
                        "block_id": "bk_ch03_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Clara Vale stated that Vale was also her family name.",
                    },
                    {
                        "block_id": "bk_ch03_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "South Lodge remained empty.",
                    },
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
    canonical: str,
    name_class: str | None,
    block_id: str,
    alternatives: list[tuple[str, str]],
    *,
    summary: str,
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
        "referential_gender_claim": _claim("neutral", block_id) if kind == "person" else None,
        "identity_summary_draft": summary,
        "identity_summary_status": "unreviewed",
        "publication_state": "clean_provisional",
        "audit_reasons": [],
        "conflict_status": "no_detected_identity_conflict",
    }


def _inventory(chapter_id: str) -> dict:
    if chapter_id == "bk_ch01":
        entities = [
            _entity(
                "local_vale_1",
                "Mr. Vale",
                "title_plus_name",
                "bk_ch01_b001",
                [("Vale", "proper_name")],
                summary="An adult visitor identified by the titled family name.",
            ),
            _entity(
                "local_north",
                "North House",
                "proper_name",
                "bk_ch01_b002",
                [],
                summary="A named residence north of the field.",
                kind="place",
            ),
        ]
        bindings = [
            {
                "surface_key": "mr. vale",
                "action": "bind_global",
                "target_candidate_id": "local_vale_1",
                "source_block_ids": ["bk_ch01_b001"],
                "resolution_note": "Supported only in this chapter.",
            }
        ]
        unresolved = []
    elif chapter_id == "bk_ch02":
        entities = [
            _entity(
                "local_vale_2",
                "Mr. Vale",
                "title_plus_name",
                "bk_ch02_b001",
                [("Vale", "proper_name")],
                summary="An adult visitor identified by the titled family name.",
            ),
            _entity(
                "local_visitor",
                "unnamed visitor",
                None,
                "bk_ch02_b002",
                [],
                summary="An unnamed visitor waiting outside.",
            ),
        ]
        bindings = [
            {
                "surface_key": "mr. vale",
                "action": "bind_global",
                "target_candidate_id": "local_vale_2",
                "source_block_ids": ["bk_ch02_b001"],
                "resolution_note": "Supported only in this chapter.",
            }
        ]
        unresolved = []
    else:
        entities = [
            _entity(
                "local_clara",
                "Clara Vale",
                "proper_name",
                "bk_ch03_b001",
                [("Vale", "proper_name")],
                summary="A woman who explicitly shares the family name.",
            ),
            _entity(
                "local_south",
                "South Lodge",
                "proper_name",
                "bk_ch03_b002",
                [],
                summary="A named lodge south of the field.",
                kind="place",
            ),
        ]
        bindings = []
        unresolved = [
            {
                "candidate_id": "unresolved_one",
                "surface": "an unseen caller",
                "source_block_ids": ["bk_ch03_b001"],
            }
        ]
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": chapter_id,
        "source_inventory_hash": f"source_{chapter_id}",
        "request_fingerprint": f"request_{chapter_id}",
        "conflict_manifest_hash": f"manifest_{chapter_id}",
        "entity_candidates": entities,
        "pending_entity_candidates": [],
        "closed_entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": unresolved,
        "quarantined_surfaces": [],
        "global_surface_bindings": bindings,
        "component_decisions": [],
        "conflict_summary": {},
        "source_validation_report": {},
        "production_publish_performed": False,
    }
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _orientations() -> dict[str, dict]:
    result = {}
    for chapter_id in ("bk_ch01", "bk_ch02", "bk_ch03"):
        body = {"orientation_draft": f"Short bounded gist for {chapter_id}."}
        result[chapter_id] = {**body, "orientation_hash": canonical_hash(body)}
    return result


def _index(**kwargs) -> dict:
    return build_book_entity_index_v1(
        document=_document(),
        audited_inventories={key: _inventory(key) for key in _orientations()},
        chapter_orientations=_orientations(),
        **kwargs,
    )


def _response(index: dict, component_id: str | None = None) -> dict:
    component = next(
        row
        for row in index["components"]
        if component_id is None or row["component_id"] == component_id
    )
    refs = component["candidate_refs"]
    by_local = {row["local_candidate_id"]: row["candidate_ref"] for row in index["candidate_rows"]}
    target = by_local["local_vale_1"]
    actions = []
    for ref in refs:
        if ref == target or ref == by_local["local_clara"]:
            action, merge_target = "keep", None
        else:
            action, merge_target = "merge_into", target
        row = next(item for item in index["candidate_rows"] if item["candidate_ref"] == ref)
        actions.append(
            {
                "candidate_ref": ref,
                "action": action,
                "target_candidate_ref": merge_target,
                "split_partitions": [],
                "source_block_ids": [row["source_block_ids"][0]],
                "resolution_note": "The supplied chapter evidence supports this bounded action.",
            }
        )
    surfaces = []
    for case in component["contested_surfaces"]:
        if case["surface_key"] == "mr vale":
            action = "promote_book_global"
            surface_target = target
            evidence = [
                next(block for block in case["source_block_ids"] if block.startswith("bk_ch01_")),
                next(block for block in case["source_block_ids"] if block.startswith("bk_ch02_")),
            ]
        else:
            action = "quarantine"
            surface_target = None
            evidence = [case["source_block_ids"][0]]
        surfaces.append(
            {
                "surface_case_id": case["surface_case_id"],
                "action": action,
                "target_candidate_ref": surface_target,
                "source_block_ids": evidence,
                "resolution_note": "The supplied surface evidence determines this scope only.",
            }
        )
    return {
        "component_id": component["component_id"],
        "candidate_actions": actions,
        "surface_actions": surfaces,
    }


def _decision(index: dict) -> dict:
    component = next(row for row in index["components"] if not row["overflow"])
    request = render_cross_chapter_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    return validate_cross_chapter_response_v1(
        _response(index, component["component_id"]),
        index=index,
        request_fingerprint=request.request_fingerprint,
    )


def _full_prefix() -> dict:
    document = _document()
    bundle = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_inventory("bk_ch01"),
        coverage_through_chapter_id="bk_ch01",
    )
    for chapter_id in ("bk_ch02", "bk_ch03"):
        bundle = extend_chapter_prefix_prior_bundle_v1(
            bundle=bundle,
            document=document,
            audited_inventory=_inventory(chapter_id),
            next_chapter_id=chapter_id,
        )
    return bundle


def _pending_gender_projection(prefix: dict, prior_card_id: str) -> dict:
    claim = next(
        row for row in prefix["claim_cards"] if row["prior_card_id"] == prior_card_id
    )
    effective = {
        "referent_kind": claim["referent_kind"],
        "referential_gender": None,
        "identity_summary": claim["identity_summary"],
    }
    dispute = {
        "disputed_field": "referential_gender",
        "historical_value": claim["referential_gender"],
        "status": "pending",
        "pending_reason_codes": ["conflicting_evidence"],
        "evidence_manifest_hashes": ["a" * 64],
        "hearing_count": 1,
        "automatic_hearing_limit": 2,
        "same_evidence_reopen_forbidden": True,
        "next_review_trigger": "new_evidence_or_book_end",
        "revision_ids": ["bclaimrev1_overlay_fixture"],
    }
    body = {
        "schema_version": CLAIM_PROJECTION_SCHEMA_VERSION,
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "registry_generation_hash": "b" * 64,
        "claim_ledger_hash": "c" * 64,
        "projected_prior_cards": [
            {
                "prior_card_id": prior_card_id,
                "source_prior_card_hash": canonical_hash(claim),
                "original_prior_card": deepcopy(claim),
                "effective_claims": effective,
                "disputed_claims": [dispute],
                "authority_state": "partial_pending",
                "claim_states": [],
            }
        ],
    }
    return {**body, "projection_hash": canonical_hash(body)}


def _empty_review_ledger(prefix: dict) -> dict:
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": prefix["coverage_through_chapter_id"],
        "observed_queue_hashes": ["d" * 64],
        "review_items": [],
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def test_index_requires_exact_chapter_coverage_and_valid_hashes() -> None:
    inventories = {key: _inventory(key) for key in _orientations()}
    inventories.pop("bk_ch03")
    with pytest.raises(BookEntityContractError, match="exact-cover"):
        build_book_entity_index_v1(
            document=_document(),
            audited_inventories=inventories,
            chapter_orientations=_orientations(),
        )
    inventories["bk_ch03"] = _inventory("bk_ch03")
    inventories["bk_ch01"]["entity_candidates"][0]["canonical_surface"] = "Tampered"
    with pytest.raises(BookEntityContractError, match="hash mismatch"):
        build_book_entity_index_v1(
            document=_document(),
            audited_inventories=inventories,
            chapter_orientations=_orientations(),
        )


def test_missing_optional_gists_do_not_modify_or_block_current_b0_contract() -> None:
    index = build_book_entity_index_v1(
        document=_document(),
        audited_inventories={key: _inventory(key) for key in _orientations()},
        chapter_orientations=None,
    )
    assert index["chapter_gists"] == []
    assert index["missing_optional_gist_chapter_ids"] == [
        "bk_ch01",
        "bk_ch02",
        "bk_ch03",
    ]
    component = index["components"][0]
    request = render_cross_chapter_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    assert request.semantic_payload["chapter_gists"] == []


def test_byte_equivalent_claims_compress_without_merging_candidate_refs() -> None:
    index = _index()
    rows = [
        row
        for row in index["candidate_rows"]
        if row["local_candidate_id"] in {"local_vale_1", "local_vale_2"}
    ]
    assert rows[0]["candidate_ref"] != rows[1]["candidate_ref"]
    assert rows[0]["claim_group_id"] == rows[1]["claim_group_id"]
    group = next(
        row for row in index["claim_groups"] if row["claim_group_id"] == rows[0]["claim_group_id"]
    )
    assert group["origin_count"] == 2
    assert all("stable_claim" not in row for row in index["candidate_rows"])


def test_cross_chapter_collision_is_bounded_and_never_auto_merged() -> None:
    index = _index()
    assert len(index["components"]) == 1
    component = index["components"][0]
    assert component["overflow"] is False
    assert len(component["candidate_refs"]) == 3
    assert all(
        "entity_id" not in row and "merged" not in row for row in index["candidate_rows"]
    )
    assert "local_north" in {
        row["local_candidate_id"]
        for row in index["candidate_rows"]
        if row["candidate_ref"] in index["clean_candidate_refs"]
    }


def test_local_bind_global_is_projected_only_as_chapter_scoped() -> None:
    index = _index()
    assert index["chapter_scoped_bindings"]
    assert {row["scope_authority"] for row in index["chapter_scoped_bindings"]} == {
        "chapter_scoped"
    }
    assert {row["source_action"] for row in index["chapter_scoped_bindings"]} == {
        "bind_global"
    }
    assert all("book_global" not in canonical_json(row) for row in index["chapter_scoped_bindings"])


def test_request_contains_only_owned_chapters_blocks_and_component() -> None:
    index = _index()
    component = index["components"][0]
    request = render_cross_chapter_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    payload = request.semantic_payload
    assert payload["component_id"] == component["component_id"]
    assert {row["chapter_id"] for row in payload["chapter_gists"]} == set(
        component["chapter_ids"]
    )
    assert {row["block_id"] for row in payload["source_blocks"]} == set(
        component["source_block_ids"]
    )
    assert "North House stood" not in request.messages[1]["content"]
    assert request.messages[1]["content"] == canonical_json(payload)


def test_candidate_and_surface_actions_must_exact_cover() -> None:
    index = _index()
    component = index["components"][0]
    response = _response(index)
    response["candidate_actions"].pop()
    with pytest.raises(BookEntityContractError, match="exact-cover"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )


def test_foreign_candidate_surface_and_block_ids_are_rejected() -> None:
    index = _index()
    response = _response(index)
    response["candidate_actions"][0]["candidate_ref"] = "bkcand_foreign"
    with pytest.raises(BookEntityContractError, match="exact-cover"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )
    response = _response(index)
    response["surface_actions"][0]["surface_case_id"] = "bisurf_foreign"
    with pytest.raises(BookEntityContractError, match="exact-cover"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )
    response = _response(index)
    response["candidate_actions"][0]["source_block_ids"] = ["bk_ch99_b999"]
    with pytest.raises(BookEntityContractError, match="foreign source blocks"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )
    response = _response(index)
    response["surface_actions"].pop()
    with pytest.raises(BookEntityContractError, match="exact-cover"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )


def test_merge_target_must_be_kept_in_same_component() -> None:
    index = _index()
    response = _response(index)
    target = next(row for row in response["candidate_actions"] if row["action"] == "keep")
    target["action"] = "pending"
    with pytest.raises(BookEntityContractError, match="merge target must be kept"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )


def test_book_global_surface_requires_every_observed_chapter() -> None:
    index = _index()
    response = _response(index)
    promoted = next(
        row for row in response["surface_actions"] if row["action"] == "promote_book_global"
    )
    promoted["source_block_ids"] = promoted["source_block_ids"][:1]
    with pytest.raises(BookEntityContractError, match="every observed chapter"):
        validate_cross_chapter_response_v1(
            response, index=index, request_fingerprint="request_one"
        )


def test_split_exact_covers_blocks_and_materializes_pending_only() -> None:
    document = _document()
    inventories = {key: _inventory(key) for key in _orientations()}
    candidate = inventories["bk_ch01"]["entity_candidates"][0]
    candidate["source_block_ids"] = ["bk_ch01_b001", "bk_ch01_b002"]
    candidate["name_locations"][0]["source_block_ids"] = ["bk_ch01_b001", "bk_ch01_b002"]
    candidate["alternative_names"][0]["source_block_ids"] = [
        "bk_ch01_b001",
        "bk_ch01_b002",
    ]
    inventories["bk_ch01"]["conflict_audited_inventory_hash"] = canonical_hash(
        {
            key: value
            for key, value in inventories["bk_ch01"].items()
            if key != "conflict_audited_inventory_hash"
        }
    )
    index = build_book_entity_index_v1(
        document=document,
        audited_inventories=inventories,
        chapter_orientations=_orientations(),
    )
    response = _response(index)
    ref = next(
        row["candidate_ref"]
        for row in index["candidate_rows"]
        if row["local_candidate_id"] == "local_vale_1"
    )
    action = next(row for row in response["candidate_actions"] if row["candidate_ref"] == ref)
    action.update(
        {
            "action": "split",
            "target_candidate_ref": None,
            "split_partitions": [
                {
                    "source_block_ids": ["bk_ch01_b001"],
                    "retained_surfaces": ["Mr. Vale", "Vale"],
                    "resolution_note": "First supplied block partition.",
                },
                {
                    "source_block_ids": ["bk_ch01_b002"],
                    "retained_surfaces": ["Mr. Vale", "Vale"],
                    "resolution_note": "Second supplied block partition.",
                },
            ],
        }
    )
    # Merge rows that targeted this ref must no longer merge into a non-kept split row.
    for row in response["candidate_actions"]:
        if row.get("target_candidate_ref") == ref:
            row.update({"action": "pending", "target_candidate_ref": None})
    for row in response["surface_actions"]:
        if row.get("target_candidate_ref") == ref:
            row.update(
                {
                    "action": "quarantine",
                    "target_candidate_ref": None,
                    "source_block_ids": row["source_block_ids"][:1],
                }
            )
    request = render_cross_chapter_request_v1(
        index=index,
        component_id=index["components"][0]["component_id"],
        document=document,
        design_doc=DESIGN_DOC,
    )
    decision = validate_cross_chapter_response_v1(
        response, index=index, request_fingerprint=request.request_fingerprint
    )
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[decision], document=document
    )
    split_rows = [
        row for row in snapshot["pending_entities"] if row["root_candidate_ref"] == ref
    ]
    assert len(split_rows) == 2
    assert all(row["registry_category"] == "pending" for row in split_rows)
    assert all(
        ref not in row["member_candidate_refs"]
        for row in snapshot["book_confirmed_entities"]
    )


def test_overflow_component_is_not_truncated_and_stays_pending() -> None:
    index = _index(max_component_candidates=2)
    component = index["components"][0]
    assert component["overflow"] is True
    assert len(component["candidate_refs"]) == 3
    with pytest.raises(BookEntityContractError, match="overflow"):
        render_cross_chapter_request_v1(
            index=index,
            component_id=component["component_id"],
            document=_document(),
            design_doc=DESIGN_DOC,
        )
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[], document=_document()
    )
    assert len(
        [
            row
            for row in snapshot["pending_entities"]
            if row["root_candidate_ref"] in set(component["candidate_refs"])
        ]
    ) == 3


def test_registry_separates_book_chapter_local_pending_and_derives_positions() -> None:
    index = _index()
    decision = _decision(index)
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[decision], document=_document()
    )
    verify_global_entity_registry_v1(snapshot)
    assert snapshot["book_confirmed_entities"]
    assert any(
        row["canonical_profile"]["canonical_surface"] == "unnamed visitor"
        for row in snapshot["chapter_local_entities"]
    )
    assert snapshot["pending_entities"]
    assert all(row["first_supported_block_id"] in row["source_block_ids"] for row in [
        *snapshot["book_confirmed_entities"],
        *snapshot["chapter_local_entities"],
        *snapshot["pending_entities"],
    ])
    assert {row["scope_authority"] for row in snapshot["chapter_surface_bindings"]} <= {
        "chapter_scoped"
    }


def test_stable_claim_overlay_nulls_only_pending_field_after_identity_resolution() -> None:
    document = _document()
    index = _index()
    prefix = _full_prefix()
    source = next(
        row
        for row in prefix["source_entity_manifest"]
        if row["source_candidate_id"] == "local_vale_1"
    )
    prefix = apply_claim_projection_to_prefix_bundle_v1(
        bundle=prefix,
        projection=_pending_gender_projection(prefix, source["prior_card_id"]),
    )
    overlay = build_book_entity_stable_claim_overlay_v1(
        index=index,
        prefix_bundle=prefix,
        document=document,
    )
    verify_book_entity_stable_claim_overlay_v1(overlay, index=index)
    base = build_global_entity_registry_v1(
        index=index,
        decisions=[_decision(index)],
        document=document,
    )
    effective = apply_stable_claim_overlay_to_global_registry_v1(
        snapshot=base,
        index=index,
        overlay=overlay,
    )
    ref = next(
        row["candidate_ref"]
        for row in index["candidate_rows"]
        if row["local_candidate_id"] == "local_vale_1"
    )
    entity = next(
        row
        for table in (
            "book_confirmed_entities",
            "chapter_local_entities",
            "pending_entities",
        )
        for row in effective[table]
        if ref in row.get("member_candidate_refs", [])
    )
    assert entity["canonical_profile"]["referential_gender_claim"] is None
    assert entity["canonical_profile"]["referent_kind_claim"] == "person"
    assert entity["canonical_profile"]["identity_summary_draft"]
    assert next(
        row
        for row in entity["canonical_profile"]["effective_claim_states"]
        if row["field"] == "referential_gender"
    )["status"] == "pending_source_dispute"
    assert effective["pending_stable_claims"] == [
        next(
            row
            for row in effective["pending_stable_claims"]
            if row["entity_id"] == entity["entity_id"]
            and row["field"] == "referential_gender"
        )
    ]
    assert effective["has_open_uncertainty"] is True


def test_stable_claim_overlay_requires_full_prefix_and_detects_tamper() -> None:
    document = _document()
    index = _index()
    partial = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_inventory("bk_ch01"),
        coverage_through_chapter_id="bk_ch01",
    )
    with pytest.raises(BookEntityClaimOverlayError, match="whole-book"):
        build_book_entity_stable_claim_overlay_v1(
            index=index,
            prefix_bundle=partial,
            document=document,
        )
    overlay = build_book_entity_stable_claim_overlay_v1(
        index=index,
        prefix_bundle=_full_prefix(),
        document=document,
    )
    tampered = deepcopy(overlay)
    current_kind = tampered["candidate_claim_rows"][0]["effective_claims"]["referent_kind"]
    tampered["candidate_claim_rows"][0]["effective_claims"]["referent_kind"] = (
        "person" if current_kind != "person" else "place"
    )
    tampered_body = dict(tampered)
    tampered_body.pop("overlay_hash")
    tampered["overlay_hash"] = canonical_hash(tampered_body)
    with pytest.raises(BookEntityClaimOverlayError, match="row hash"):
        verify_book_entity_stable_claim_overlay_v1(tampered, index=index)


def test_registry_verifier_rejects_hidden_pending_stable_claims() -> None:
    index = _index()
    base = build_global_entity_registry_v1(
        index=index,
        decisions=[_decision(index)],
        document=_document(),
    )
    overlay = build_book_entity_stable_claim_overlay_v1(
        index=index,
        prefix_bundle=_full_prefix(),
        document=_document(),
    )
    effective = apply_stable_claim_overlay_to_global_registry_v1(
        snapshot=base,
        index=index,
        overlay=overlay,
    )
    forged = deepcopy(effective)
    forged["pending_stable_claims"] = [
        {
            "pending_stable_claim_id": "bkpendclaim1_forged",
            "entity_id": effective["book_confirmed_entities"][0]["entity_id"],
            "field": "referential_gender",
            "status": "pending_source_dispute",
            "candidate_refs": [],
            "overlay_hash": effective["stable_claim_overlay_hash"],
        }
    ]
    forged["has_open_uncertainty"] = False
    body = dict(forged)
    body.pop("snapshot_hash")
    forged["snapshot_hash"] = canonical_hash(body)
    with pytest.raises(BookEntityContractError, match="hides open uncertainty"):
        verify_global_entity_registry_v1(forged)


def test_book_end_handoff_and_seal_require_full_identity_exact_cover() -> None:
    document = _document()
    prefix = _full_prefix()
    handoff = build_chapter_cycle_book_end_handoff_v1(
        document=document,
        audited_inventories={key: _inventory(key) for key in _orientations()},
        final_prefix_bundle=prefix,
        review_ledger=_empty_review_ledger(prefix),
        chapter_orientations=_orientations(),
    )
    verify_chapter_cycle_book_end_handoff_v1(handoff, document=document)
    assert handoff["b2_ready"] is False
    assert handoff["required_identity_component_ids"]
    request_set = render_chapter_cycle_book_end_requests_v1(
        handoff=handoff,
        document=document,
        design_doc=DESIGN_DOC,
    )
    verify_chapter_cycle_book_end_request_set_v1(
        request_set, handoff=handoff
    )
    assert request_set["api_calls_performed"] == 0
    assert [row["component_id"] for row in request_set["requests"]] == handoff[
        "required_identity_component_ids"
    ]
    tampered_request_set = deepcopy(request_set)
    tampered_request_set["requests"][0]["messages"][-1]["content"] = "{}"
    request_body = {
        key: value
        for key, value in tampered_request_set["requests"][0].items()
        if key != "request_hash"
    }
    tampered_request_set["requests"][0]["request_hash"] = canonical_hash(request_body)
    request_set_body = dict(tampered_request_set)
    request_set_body.pop("request_set_hash")
    tampered_request_set["request_set_hash"] = canonical_hash(request_set_body)
    with pytest.raises(ChapterCycleBookEndError, match="differs from rendered"):
        verify_chapter_cycle_book_end_request_set_v1(
            tampered_request_set, handoff=handoff
        )
    with pytest.raises(ChapterCycleBookEndError, match="exact-cover"):
        seal_chapter_cycle_global_registry_v1(
            handoff=handoff,
            decisions=[],
            document=document,
        )
    sealed = seal_chapter_cycle_global_registry_v1(
        handoff=handoff,
        decisions=[_decision(handoff["book_index"])],
        document=document,
    )
    verify_sealed_chapter_cycle_global_registry_v1(sealed)
    assert sealed["b2_ready"] is True
    assert sealed["global_registry_snapshot"]["stable_claim_overlay_hash"] == (
        handoff["stable_claim_overlay"]["overlay_hash"]
    )


def test_book_end_seal_preserves_pending_field_without_reviving_history() -> None:
    document = _document()
    prefix = _full_prefix()
    source = next(
        row
        for row in prefix["source_entity_manifest"]
        if row["source_candidate_id"] == "local_vale_1"
    )
    prefix = apply_claim_projection_to_prefix_bundle_v1(
        bundle=prefix,
        projection=_pending_gender_projection(prefix, source["prior_card_id"]),
    )
    handoff = build_chapter_cycle_book_end_handoff_v1(
        document=document,
        audited_inventories={key: _inventory(key) for key in _orientations()},
        final_prefix_bundle=prefix,
        review_ledger=_empty_review_ledger(prefix),
        chapter_orientations=_orientations(),
    )
    sealed = seal_chapter_cycle_global_registry_v1(
        handoff=handoff,
        decisions=[_decision(handoff["book_index"])],
        document=document,
    )
    snapshot = sealed["global_registry_snapshot"]
    assert any(
        row["field"] == "referential_gender"
        for row in snapshot["pending_stable_claims"]
    )
    assert snapshot["has_open_uncertainty"] is True


def test_n_chapter_prefix_assembly_is_contiguous_idempotent_and_non_authoritative() -> None:
    document = _document()
    inventories = {key: _inventory(key) for key in _orientations()}
    ch1 = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventories["bk_ch01"],
        coverage_through_chapter_id="bk_ch01",
    )
    target = next(
        row
        for row in ch1["source_entity_manifest"]
        if row["source_candidate_id"] == "local_vale_1"
    )
    projection = _pending_gender_projection(ch1, target["prior_card_id"])
    assembly = assemble_chapter_cycle_prefix_v1(
        document=document,
        audited_inventories=inventories,
        claim_projections_before_chapter={"bk_ch02": projection},
    )
    verify_chapter_cycle_prefix_assembly_v1(assembly, document=document)
    replay = assemble_chapter_cycle_prefix_v1(
        document=document,
        audited_inventories=inventories,
        claim_projections_before_chapter={"bk_ch02": projection},
    )
    assert replay == assembly
    assert assembly["full_book_coverage"] is True
    assert assembly["book_end_handoff_allowed"] is True
    assert assembly["b2_ready"] is False
    context = next(
        row
        for row in [
            *assembly["final_prefix_bundle"]["b0_context_cards"],
            *assembly["final_prefix_bundle"]["candidate_only_context_cards"],
        ]
        if row["prior_card_id"] == target["prior_card_id"]
    )
    assert context["effective_claims"]["referential_gender"] is None


def test_partial_n_chapter_assembly_cannot_enter_book_end_or_skip_chapters() -> None:
    document = _document()
    inventories = {key: _inventory(key) for key in ("bk_ch01", "bk_ch02")}
    partial = assemble_chapter_cycle_prefix_v1(
        document=document,
        audited_inventories=inventories,
        ordered_chapter_ids=["bk_ch01", "bk_ch02"],
    )
    assert partial["full_book_coverage"] is False
    assert partial["book_end_handoff_allowed"] is False
    with pytest.raises(ChapterCyclePrefixRunnerError, match="contiguous prefix"):
        assemble_chapter_cycle_prefix_v1(
            document=document,
            audited_inventories={
                "bk_ch01": _inventory("bk_ch01"),
                "bk_ch03": _inventory("bk_ch03"),
            },
            ordered_chapter_ids=["bk_ch01", "bk_ch03"],
        )


def test_source_repair_rows_survive_index_and_sealed_registry_without_authority() -> None:
    inventories = {key: _inventory(key) for key in _orientations()}
    repair = _entity(
        "local_unaddressed",
        "Unseen Label",
        "proper_name",
        "bk_ch02_b002",
        [],
        summary="A possible referent whose source address remains unresolved.",
    )
    repair["surface_status"] = "unlocated_pending_repair"
    repair["name_locations"][0].update(
        {
            "source_block_ids": [],
            "proposed_support_block_ids": ["foreign_b001"],
            "surface_match_block_ids": [],
            "address_validation_state": "foreign_block_removed",
            "ownership_state": "single_candidate_claim",
        }
    )
    repair["source_block_ids"] = []
    repair["publication_state"] = "pending_source_repair"
    repair["conflict_status"] = "deferred_until_valid_source_address"
    inventories["bk_ch02"]["deferred_source_repairs"] = [repair]
    inventory_body = dict(inventories["bk_ch02"])
    inventory_body.pop("conflict_audited_inventory_hash", None)
    inventories["bk_ch02"]["conflict_audited_inventory_hash"] = canonical_hash(
        inventory_body
    )

    index = build_book_entity_index_v1(
        document=_document(),
        audited_inventories=inventories,
        chapter_orientations=_orientations(),
    )

    assert len(index["pending_source_repairs"]) == 1
    row = index["pending_source_repairs"][0]
    assert row["authority_state"] == "not_authoritative"
    assert row["proposed_support_block_ids"] == ["foreign_b001"]
    assert row["surface_match_block_ids"] == []
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[_decision(index)], document=_document()
    )
    assert snapshot["has_open_uncertainty"] is True
    assert snapshot["pending_source_repairs"] == index["pending_source_repairs"]
    assert all(
        row["canonical_profile"]["canonical_surface"] != "Unseen Label"
        for row in [
            *snapshot["book_confirmed_entities"],
            *snapshot["chapter_local_entities"],
            *snapshot["pending_entities"],
        ]
        if row.get("canonical_profile")
    )
    assert {row["scope_authority"] for row in snapshot["book_surface_bindings"]} == {
        "book_global"
    }


def test_validation_quarantine_survives_index_and_sealed_registry() -> None:
    inventories = {key: _inventory(key) for key in _orientations()}
    inventories["bk_ch02"]["source_validation_report"] = {
        "rejected_rows": [
            {
                "row_kind": "entity_candidate",
                "row_index": 3,
                "reason": "contract shape was invalid",
                "raw_row": {"canonical_surface": "Unverified row"},
                "lifecycle_state": "quarantined_contract_error",
            }
        ]
    }
    inventory_body = dict(inventories["bk_ch02"])
    inventory_body.pop("conflict_audited_inventory_hash", None)
    inventories["bk_ch02"]["conflict_audited_inventory_hash"] = canonical_hash(
        inventory_body
    )

    index = build_book_entity_index_v1(
        document=_document(),
        audited_inventories=inventories,
        chapter_orientations=_orientations(),
    )

    assert len(index["validation_quarantines"]) == 1
    quarantine = index["validation_quarantines"][0]
    assert quarantine["chapter_id"] == "bk_ch02"
    assert quarantine["authority_state"] == "not_authoritative"
    assert quarantine["lifecycle_state"] == "open_validation_quarantine"
    assert quarantine["issues"]["rejected_rows"][0]["raw_row"] == {
        "canonical_surface": "Unverified row"
    }

    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[_decision(index)], document=_document()
    )
    assert snapshot["has_open_uncertainty"] is True
    assert snapshot["validation_quarantines"] == index["validation_quarantines"]


def test_rehashed_source_repair_cannot_gain_authority() -> None:
    inventories = {key: _inventory(key) for key in _orientations()}
    repair = _entity(
        "local_unaddressed",
        "Unseen Label",
        "proper_name",
        "bk_ch02_b002",
        [],
        summary="A possible referent whose source address remains unresolved.",
    )
    repair["surface_status"] = "unlocated_pending_repair"
    repair["name_locations"][0].update(
        {
            "source_block_ids": [],
            "proposed_support_block_ids": ["foreign_b001"],
            "surface_match_block_ids": [],
            "address_validation_state": "foreign_block_removed",
            "ownership_state": "single_candidate_claim",
        }
    )
    repair["source_block_ids"] = []
    repair["publication_state"] = "pending_source_repair"
    repair["conflict_status"] = "deferred_until_valid_source_address"
    inventories["bk_ch02"]["deferred_source_repairs"] = [repair]
    inventory_body = dict(inventories["bk_ch02"])
    inventory_body.pop("conflict_audited_inventory_hash", None)
    inventories["bk_ch02"]["conflict_audited_inventory_hash"] = canonical_hash(
        inventory_body
    )
    index = build_book_entity_index_v1(
        document=_document(),
        audited_inventories=inventories,
        chapter_orientations=_orientations(),
    )
    tampered = deepcopy(index)
    tampered["pending_source_repairs"][0]["authority_state"] = "active"
    tampered_body = dict(tampered)
    tampered_body.pop("book_index_hash", None)
    tampered["book_index_hash"] = canonical_hash(tampered_body)

    with pytest.raises(BookEntityContractError, match="source repair has unsafe authority"):
        verify_book_entity_index_v1(tampered)


def test_sealed_registry_cannot_hide_validation_quarantine() -> None:
    inventories = {key: _inventory(key) for key in _orientations()}
    inventories["bk_ch02"]["source_validation_report"] = {
        "rejected_rows": [
            {
                "row_kind": "entity_candidate",
                "row_index": 3,
                "reason": "contract shape was invalid",
                "raw_row": {"canonical_surface": "Unverified row"},
                "lifecycle_state": "quarantined_contract_error",
            }
        ]
    }
    inventory_body = dict(inventories["bk_ch02"])
    inventory_body.pop("conflict_audited_inventory_hash", None)
    inventories["bk_ch02"]["conflict_audited_inventory_hash"] = canonical_hash(
        inventory_body
    )
    index = build_book_entity_index_v1(
        document=_document(),
        audited_inventories=inventories,
        chapter_orientations=_orientations(),
    )
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[_decision(index)], document=_document()
    )

    missing = deepcopy(snapshot)
    missing.pop("validation_quarantines")
    missing_body = dict(missing)
    missing_body.pop("snapshot_hash", None)
    missing["snapshot_hash"] = canonical_hash(missing_body)
    with pytest.raises(BookEntityContractError, match="validation_quarantines"):
        verify_global_entity_registry_v1(missing)

    hidden = deepcopy(snapshot)
    hidden["has_open_uncertainty"] = False
    hidden_body = dict(hidden)
    hidden_body.pop("snapshot_hash", None)
    hidden["snapshot_hash"] = canonical_hash(hidden_body)
    with pytest.raises(BookEntityContractError, match="hides open uncertainty"):
        verify_global_entity_registry_v1(hidden)


def test_deterministic_input_reordering_preserves_index_hash() -> None:
    inventories = {key: _inventory(key) for key in reversed(list(_orientations()))}
    first = _index()
    second = build_book_entity_index_v1(
        document=_document(),
        audited_inventories=inventories,
        chapter_orientations={key: _orientations()[key] for key in reversed(list(_orientations()))},
    )
    assert first["book_index_hash"] == second["book_index_hash"]
    assert canonical_json(first) == canonical_json(second)


def test_store_replay_cas_and_crash_before_pointer_switch(tmp_path: Path) -> None:
    index = _index()
    decision = _decision(index)
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[decision], document=_document()
    )
    prepared = prepare_book_entity_generation_v1(
        snapshot=snapshot,
        index=index,
        decisions=[decision],
        prompt_manifest=prompt_manifest_v1(DESIGN_DOC),
        parent_generation_id=None,
    )
    store = BookEntityRegistryStoreV1(tmp_path / "store")
    assert store.current_generation_id(index["state_lineage_id"]) is None
    with pytest.raises(BookEntityStoreError, match="B2 is blocked"):
        store.load_b2_ready_snapshot(index["state_lineage_id"])
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.commit(
            prepared,
            expected_parent=None,
            before_pointer_switch=lambda: (_ for _ in ()).throw(RuntimeError("simulated crash")),
        )
    assert store.current_generation_id(index["state_lineage_id"]) is None
    store.commit(prepared, expected_parent=None)
    generation_path = (
        tmp_path / "store" / "generations" / f"{prepared.generation_id}.json"
    )
    first_bytes = generation_path.read_bytes()
    assert store.load_generation(prepared.generation_id) == prepared.to_dict()
    assert generation_path.read_bytes() == first_bytes
    loaded = store.load_b2_ready_snapshot(index["state_lineage_id"])
    assert loaded["snapshot_hash"] == snapshot["snapshot_hash"]


def test_store_stale_parent_loses_without_rebase(tmp_path: Path) -> None:
    index = _index()
    decision = _decision(index)
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[decision], document=_document()
    )
    prepared = prepare_book_entity_generation_v1(
        snapshot=snapshot,
        index=index,
        decisions=[decision],
        prompt_manifest=prompt_manifest_v1(DESIGN_DOC),
        parent_generation_id=None,
    )
    store = BookEntityRegistryStoreV1(tmp_path / "store")
    store.commit(prepared, expected_parent=None)
    with pytest.raises(BookEntityStaleParentError, match="stale"):
        store.commit(prepared, expected_parent=None)


def test_hashed_but_semantically_invalid_decision_is_revalidated_on_publish() -> None:
    index = _index()
    decision = _decision(index)
    changed = deepcopy(decision)
    merge = next(
        row for row in changed["candidate_actions"] if row["action"] == "merge_into"
    )
    merge["target_candidate_ref"] = merge["candidate_ref"]
    changed_body = {key: value for key, value in changed.items() if key != "decision_hash"}
    changed["decision_hash"] = canonical_hash(changed_body)
    with pytest.raises(BookEntityContractError, match="merge target"):
        build_global_entity_registry_v1(
            index=index, decisions=[changed], document=_document()
        )


def test_generation_collision_and_pointer_tamper_are_fatal(tmp_path: Path) -> None:
    index = _index()
    decision = _decision(index)
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[decision], document=_document()
    )
    prepared = prepare_book_entity_generation_v1(
        snapshot=snapshot,
        index=index,
        decisions=[decision],
        prompt_manifest=prompt_manifest_v1(DESIGN_DOC),
        parent_generation_id=None,
    )
    collision_store = BookEntityRegistryStoreV1(tmp_path / "collision")
    generation_path = (
        tmp_path
        / "collision"
        / "generations"
        / f"{prepared.generation_id}.json"
    )
    generation_path.parent.mkdir(parents=True)
    generation_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(BookEntityStoreError, match="unequal bytes"):
        collision_store.commit(prepared, expected_parent=None)

    store = BookEntityRegistryStoreV1(tmp_path / "pointer")
    store.commit(prepared, expected_parent=None)
    pointer_path = next((tmp_path / "pointer" / "current").glob("*.json"))
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["generation_id"] = "bookreggen1_" + "0" * 20
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(BookEntityStoreError, match="pointer hash mismatch"):
        store.current_generation_id(index["state_lineage_id"])


def test_tampered_index_snapshot_and_generation_are_rejected(tmp_path: Path) -> None:
    index = _index()
    changed = deepcopy(index)
    changed["claim_groups"][0]["claim_payload"]["canonical_surface"] = "Foreign"
    with pytest.raises(BookEntityContractError, match="hash mismatch"):
        verify_book_entity_index_v1(changed)
    decision = _decision(index)
    snapshot = build_global_entity_registry_v1(
        index=index, decisions=[decision], document=_document()
    )
    changed_snapshot = deepcopy(snapshot)
    changed_snapshot["book_confirmed_entities"][0]["source_block_ids"] = []
    with pytest.raises(BookEntityContractError, match="hash mismatch"):
        verify_global_entity_registry_v1(changed_snapshot)
    prepared = prepare_book_entity_generation_v1(
        snapshot=snapshot,
        index=index,
        decisions=[decision],
        prompt_manifest=prompt_manifest_v1(DESIGN_DOC),
        parent_generation_id=None,
    )
    store = BookEntityRegistryStoreV1(tmp_path / "store")
    store.commit(prepared, expected_parent=None)
    path = tmp_path / "store" / "generations" / f"{prepared.generation_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot"]["b2_ready"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BookEntityStoreError, match="commit payload hash mismatch"):
        store.load_generation(prepared.generation_id)


def test_runtime_boundary_has_no_provider_gold_ingest_or_b2_dependency() -> None:
    paths = [
        RUNTIME_ROOT / "pipeline" / "literary" / "book_entity_registry_prompts_v1.py",
        RUNTIME_ROOT / "pipeline" / "literary" / "book_entity_registry_v1.py",
        RUNTIME_ROOT / "pipeline" / "literary" / "book_entity_registry_store_v1.py",
    ]
    joined = "\n".join(path.read_text(encoding="utf-8").casefold() for path in paths)
    forbidden = (
        "pipeline.ingest",
        "pipeline.translate",
        "judge_client",
        "openai",
        "gemini",
        "requests.",
        "socket",
        "sqlite3",
        "gold_data",
        "oracle",
    )
    assert all(value not in joined for value in forbidden)
