from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from pipeline.literary.chapter_prefix_prior_v1 import (
    build_chapter_prefix_prior_bundle_v1,
    extend_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    REVIEW_LEDGER_SCHEMA_VERSION,
    REVIEW_LEDGER_VALIDATOR_VERSION,
)
from pipeline.literary.incremental_identity_auditor_v1 import (
    build_incremental_identity_index_v1,
    build_incremental_identity_ledger_v1,
    validate_incremental_identity_response_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.review_case_ledger_v1 import (
    build_review_case_ledger_v1,
    apply_identity_surface_decisions_to_review_cases_v1,
    finalize_review_case_ledger_v1,
    project_ready_cases_to_chapter_review_ledger_v1,
    select_relevant_review_cases_v1,
    verify_relevant_review_case_packet_v1,
    verify_review_case_ledger_v1,
)
from pipeline.scripts.run_b0_prior_challenge_experiment import build_envelope, run_dry


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def _document() -> dict:
    return {
        "document_id": "review_case_book",
        "chapters": [
            {
                "chapter_id": "bk_ch01",
                "blocks": [
                    {
                        "block_id": "bk_ch01_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Mrs. Vale entered, and a servant called her missis.",
                    }
                ],
            },
            {
                "chapter_id": "bk_ch02",
                "blocks": [
                    {
                        "block_id": "bk_ch02_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "The servant again used missis while Mrs. Vale listened.",
                    }
                ],
            },
        ],
    }


def _claim(value: str) -> dict:
    return {
        "value": value,
        "basis": "explicit",
        "support_block_ids": ["bk_ch01_b001"],
        "address_validation_state": "valid",
        "semantic_status": "unreviewed",
    }


def _inventory() -> dict:
    entity = {
        "candidate_id": "local_mrs_vale",
        "canonical_surface": "Mrs. Vale",
        "surface_status": "located",
        "canonical_name_class": "title_plus_name",
        "alternative_names": [
            {
                "surface": "missis",
                "name_class": "stable_nickname",
                "source_block_ids": ["bk_ch01_b001"],
            }
        ],
        "name_locations": [
            {
                "surface": "Mrs. Vale",
                "name_class": "title_plus_name",
                "source_block_ids": ["bk_ch01_b001"],
            },
            {
                "surface": "missis",
                "name_class": "stable_nickname",
                "source_block_ids": ["bk_ch01_b001"],
            },
        ],
        "source_block_ids": ["bk_ch01_b001"],
        "referent_kind_claim": _claim("person"),
        "referential_gender_claim": _claim("feminine"),
        "identity_summary_draft": "A named resident distinguished by a stable title and surname.",
        "identity_summary_status": "unreviewed",
        "publication_state": "clean_provisional",
        "audit_reasons": [],
        "conflict_status": "no_detected_identity_conflict",
    }
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": "bk_ch01",
        "source_inventory_hash": "source_inventory_ch01",
        "request_fingerprint": "request_ch01",
        "conflict_manifest_hash": "manifest_ch01",
        "entity_candidates": [entity],
        "pending_entity_candidates": [],
        "closed_entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "quarantined_surfaces": [],
        "global_surface_bindings": [],
        "component_decisions": [],
        "conflict_summary": {},
        "source_validation_report": {},
        "production_publish_performed": False,
    }
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _empty_inventory_ch02() -> dict:
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": "bk_ch02",
        "source_inventory_hash": "source_inventory_ch02",
        "request_fingerprint": "request_ch02",
        "conflict_manifest_hash": "manifest_ch02",
        "entity_candidates": [],
        "pending_entity_candidates": [],
        "closed_entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "quarantined_surfaces": [],
        "global_surface_bindings": [],
        "component_decisions": [],
        "conflict_summary": {},
        "source_validation_report": {},
        "production_publish_performed": False,
    }
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _empty_chapter_review(lineage: str) -> dict:
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": lineage,
        "coverage_through_chapter_id": "bk_ch02",
        "observed_queue_hashes": ["7" * 64],
        "review_items": [],
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def _challenge_artifact(*, case_id: str, reason: str) -> dict:
    body = {
        "prior_enrichment_requests": [],
        "review_case_observations": [
            {
                "review_case_id": case_id,
                "observation": "supports",
                "source_block_ids": ["bk_ch02_b001"],
                "reason": reason,
            }
        ],
    }
    return {**body, "prior_challenge_artifact_hash": canonical_hash(body)}


def test_blocked_alias_survives_as_retrievable_case_and_replay_is_idempotent() -> None:
    document = _document()
    inventory = _inventory()
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    ledger = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix,
        audited_inventory=inventory,
    )
    alias_cases = [
        row for row in ledger["review_cases"] if row["case_type"] == "alias_scope"
    ]
    assert len(alias_cases) == 1
    assert alias_cases[0]["surface"] == "missis"
    assert alias_cases[0]["status"] == "collecting_evidence"
    assert alias_cases[0]["authority_effect"] == "retrieval_only"

    packet = select_relevant_review_cases_v1(
        ledger=ledger,
        chapter=document["chapters"][1],
        prefix_bundle=prefix,
    )
    verified_packet = verify_relevant_review_case_packet_v1(
        packet, expected_chapter_id="bk_ch02"
    )
    assert verified_packet["packets"][0]["review_case_id"] == alias_cases[0][
        "review_case_id"
    ]
    assert verified_packet["packets"][0]["current_surface_hit_block_ids"] == [
        "bk_ch02_b001"
    ]

    replay = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix,
        audited_inventory=inventory,
        previous_ledger=ledger,
    )
    replay_case = next(
        row for row in replay["review_cases"] if row["case_type"] == "alias_scope"
    )
    assert len(replay_case["evidence_history"]) == 1


def test_unlocated_dormant_referent_survives_only_in_review_lifecycle() -> None:
    document = _document()
    inventory = _inventory()
    inventory.pop("conflict_audited_inventory_hash")
    inventory["unresolved_referents"] = [
        {
            "candidate_id": "unresolved_hound",
            "surface": "the hound",
            "referent_kind_claim": "animal",
            "short_description": "An unnamed hound proposed without a literal address.",
            "source_block_ids": [],
            "proposed_support_block_ids": ["bk_ch01_b001"],
            "surface_match_block_ids": [],
            "address_validation_state": "surface_absent_from_support",
            "lifecycle_state": "dormant_unresolved",
            "publication_state": "not_published",
            "issue": "unnamed_but_salient",
        }
    ]
    inventory["conflict_audited_inventory_hash"] = canonical_hash(inventory)

    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    assert all(
        row["canonical_surface"] != "the hound"
        for row in [
            *prefix["b0_context_cards"],
            *prefix["candidate_only_context_cards"],
        ]
    )

    ledger = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix,
        audited_inventory=inventory,
    )
    review_case = next(
        row
        for row in ledger["review_cases"]
        if row["surface"] == "the hound"
    )
    assert review_case["status"] == "collecting_evidence"
    assert review_case["authority_effect"] == "retrieval_only"
    assert review_case["evidence_history"][0]["source_block_ids"] == []


def test_book_end_finalizer_gives_every_nonterminal_case_an_owner() -> None:
    document = _document()
    inventory = _inventory()
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    ledger = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix,
        audited_inventory=inventory,
    )
    finalized = finalize_review_case_ledger_v1(ledger)
    verify_review_case_ledger_v1(finalized)
    case = finalized["review_cases"][0]
    assert case["status"] == "book_end_pending"
    assert case["next_actor"] == "book_end_auditor"
    assert case["evidence_needed"] == ["whole_book_closure"]
    tampered = deepcopy(finalized)
    tampered["review_cases"][0]["next_actor"] = "none"
    tampered_body = {
        key: value
        for key, value in tampered.items()
        if key != "review_case_ledger_hash"
    }
    tampered["review_case_ledger_hash"] = canonical_hash(tampered_body)
    try:
        verify_review_case_ledger_v1(tampered)
    except Exception as exc:
        assert "owner" in str(exc)
    else:
        raise AssertionError("nonterminal owner tamper was accepted")


def test_prior_challenge_dry_render_reads_only_relevant_previous_cases(
    tmp_path: Path,
) -> None:
    document = _document()
    inventory = _inventory()
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    ledger = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix,
        audited_inventory=inventory,
    )
    document_path = tmp_path / "document.json"
    prefix_path = tmp_path / "prefix.json"
    ledger_path = tmp_path / "review_cases.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    envelope_parts = build_envelope(
        document_path=document_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=prefix_path,
        corruption_manifest_path=None,
        review_case_ledger_path=ledger_path,
    )
    full_review_cases = envelope_parts[6]
    assert full_review_cases is not None
    assert full_review_cases["review_case_manifest_hash"]
    assert full_review_cases["packets"][0]["packet_hash"]
    assert "packet_hash" not in envelope_parts[1].sections[
        "relevant_review_cases"
    ]["packets"][0]
    report = run_dry(
        document_path=document_path,
        design_doc=DESIGN_DOC,
        chapter_id="bk_ch02",
        prior_cards_path=None,
        prior_bundle_path=prefix_path,
        corruption_manifest_path=None,
        output_dir=tmp_path / "dry",
        review_case_ledger_path=ledger_path,
    )
    assert report["relevant_review_case_count"] == 1
    request = json.loads((tmp_path / "dry" / "request.json").read_text("utf-8"))
    packets = request["sections"]["relevant_review_cases"]["packets"]
    assert [row["surface"] for row in packets] == ["missis"]
    assert packets[0]["current_surface_hit_block_ids"] == ["bk_ch02_b001"]


def test_pending_entity_reopens_on_new_blocks_and_closes_after_identity_hearing() -> None:
    document = _document()
    inventory = _inventory()
    inventory_body = {
        key: deepcopy(value)
        for key, value in inventory.items()
        if key != "conflict_audited_inventory_hash"
    }
    entity = inventory_body["entity_candidates"].pop()
    entity["publication_state"] = "pending"
    inventory_body["pending_entity_candidates"] = [entity]
    pending_inventory = {
        **inventory_body,
        "conflict_audited_inventory_hash": canonical_hash(inventory_body),
    }
    prefix_ch01 = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=pending_inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    ledger_ch01 = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix_ch01,
        audited_inventory=pending_inventory,
    )
    entity_case = next(
        row for row in ledger_ch01["review_cases"] if row["case_type"] == "entity_identity"
    )
    challenge_body = {
        "prior_enrichment_requests": [],
        "review_case_observations": [
            {
                "review_case_id": entity_case["review_case_id"],
                "observation": "supports",
                "source_block_ids": ["bk_ch02_b001"],
                "reason": "The current named occurrence supports continuity.",
            }
        ],
    }
    challenge = {
        **challenge_body,
        "prior_challenge_artifact_hash": canonical_hash(challenge_body),
    }
    inventory_ch02 = _empty_inventory_ch02()
    prefix_ch02 = extend_chapter_prefix_prior_bundle_v1(
        bundle=prefix_ch01,
        document=document,
        audited_inventory=inventory_ch02,
        next_chapter_id="bk_ch02",
    )
    ledger_ch02 = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch02",
        prefix_bundle=prefix_ch02,
        audited_inventory=inventory_ch02,
        previous_ledger=ledger_ch01,
        prior_challenge_artifact=challenge,
    )
    reopened = next(
        row
        for row in ledger_ch02["review_cases"]
        if row["review_case_id"] == entity_case["review_case_id"]
    )
    assert reopened["status"] == "ready_for_review"
    base_review = _empty_chapter_review(prefix_ch02["state_lineage_id"])
    projected_review = project_ready_cases_to_chapter_review_ledger_v1(
        case_ledger=ledger_ch02,
        chapter_review_ledger=base_review,
    )
    index = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=prefix_ch02,
        review_ledger=projected_review,
    )
    assert len(index["components"]) == 1
    component = index["components"][0]
    card_id = component["candidate_prior_card_ids"][0]
    decision = validate_incremental_identity_response_v1(
        {
            "component_id": component["component_id"],
            "candidate_actions": [
                {
                    "prior_card_id": card_id,
                    "action": "keep",
                    "target_prior_card_id": None,
                    "source_block_ids": ["bk_ch02_b001"],
                    "resolution_note": "The new named occurrence supports this candidate.",
                }
            ],
            "surface_scope_actions": [],
        },
        index=index,
        request_fingerprint="f" * 64,
    )
    identity = build_incremental_identity_ledger_v1(
        index=index,
        decisions=[decision],
    )
    closed = apply_identity_surface_decisions_to_review_cases_v1(
        case_ledger=ledger_ch02,
        chapter_review_ledger=projected_review,
        identity_ledger=identity,
    )
    final_case = next(
        row
        for row in closed["review_cases"]
        if row["review_case_id"] == entity_case["review_case_id"]
    )
    assert final_case["status"] == "closed"
    assert final_case["next_actor"] == "none"

    tampered = deepcopy(closed)
    tampered_case = next(
        row
        for row in tampered["review_cases"]
        if row["review_case_id"] == entity_case["review_case_id"]
    )
    tampered_case["hearing_count"] += 1
    tampered_body = {
        key: value
        for key, value in tampered.items()
        if key != "review_case_ledger_hash"
    }
    tampered["review_case_ledger_hash"] = canonical_hash(tampered_body)
    try:
        verify_review_case_ledger_v1(tampered)
    except Exception as exc:
        assert "hearing" in str(exc)
    else:
        raise AssertionError("tampered hearing count was accepted")


def test_same_immutable_blocks_cannot_reopen_after_pending_hearing() -> None:
    document = _document()
    inventory = _inventory()
    prefix_ch01 = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=inventory,
        coverage_through_chapter_id="bk_ch01",
    )
    ledger_ch01 = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch01",
        prefix_bundle=prefix_ch01,
        audited_inventory=inventory,
    )
    alias_case = next(
        row for row in ledger_ch01["review_cases"] if row["case_type"] == "alias_scope"
    )
    inventory_ch02 = _empty_inventory_ch02()
    prefix_ch02 = extend_chapter_prefix_prior_bundle_v1(
        bundle=prefix_ch01,
        document=document,
        audited_inventory=inventory_ch02,
        next_chapter_id="bk_ch02",
    )
    first_challenge = _challenge_artifact(
        case_id=alias_case["review_case_id"],
        reason="The current use supplies materially new referent evidence.",
    )
    ready = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch02",
        prefix_bundle=prefix_ch02,
        audited_inventory=inventory_ch02,
        previous_ledger=ledger_ch01,
        prior_challenge_artifact=first_challenge,
    )
    review = project_ready_cases_to_chapter_review_ledger_v1(
        case_ledger=ready,
        chapter_review_ledger=_empty_chapter_review(prefix_ch02["state_lineage_id"]),
    )
    index = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=prefix_ch02,
        review_ledger=review,
    )
    component = index["components"][0]
    card_id = component["candidate_prior_card_ids"][0]
    review_item_id = component["review_item_ids"][0]
    decision = validate_incremental_identity_response_v1(
        {
            "component_id": component["component_id"],
            "candidate_actions": [
                {
                    "prior_card_id": card_id,
                    "action": "keep",
                    "target_prior_card_id": None,
                    "source_block_ids": ["bk_ch02_b001"],
                    "resolution_note": "Keep the candidate while scope remains unresolved.",
                }
            ],
            "surface_scope_actions": [
                {
                    "review_item_id": review_item_id,
                    "action": "keep_pending",
                    "target_prior_card_id": None,
                    "valid_block_ids": [],
                    "source_block_ids": ["bk_ch02_b001"],
                    "evidence_needed": "scope_disambiguation",
                    "resolution_note": "The supplied block does not establish wider scope.",
                }
            ],
        },
        index=index,
        request_fingerprint="e" * 64,
    )
    identity = build_incremental_identity_ledger_v1(
        index=index,
        decisions=[decision],
    )
    pending = apply_identity_surface_decisions_to_review_cases_v1(
        case_ledger=ready,
        chapter_review_ledger=review,
        identity_ledger=identity,
    )
    pending_case = next(
        row
        for row in pending["review_cases"]
        if row["review_case_id"] == alias_case["review_case_id"]
    )
    assert pending_case["status"] == "collecting_evidence"
    assert pending_case["hearing_count"] == 1

    replay = build_review_case_ledger_v1(
        document=document,
        chapter_id="bk_ch02",
        prefix_bundle=prefix_ch02,
        audited_inventory=inventory_ch02,
        previous_ledger=pending,
        prior_challenge_artifact=_challenge_artifact(
            case_id=alias_case["review_case_id"],
            reason="Different wording over the same immutable source block.",
        ),
    )
    replay_case = next(
        row
        for row in replay["review_cases"]
        if row["review_case_id"] == alias_case["review_case_id"]
    )
    assert replay_case["status"] == "collecting_evidence"
    assert replay_case["hearing_count"] == 1
    assert len(replay_case["evidence_history"]) == 2
