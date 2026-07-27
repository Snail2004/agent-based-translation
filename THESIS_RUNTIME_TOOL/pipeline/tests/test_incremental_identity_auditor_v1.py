from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    REVIEW_LEDGER_SCHEMA_VERSION,
    REVIEW_LEDGER_VALIDATOR_VERSION,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    CHAPTER_CONFIRMED_SCOPE,
    PREFIX_PRIOR_SCHEMA_VERSION,
    PREFIX_PRIOR_VALIDATOR_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.incremental_identity_auditor_prompts_v1 import (
    PROMPT_ID,
    PROMPT_SHA256,
    load_incremental_identity_prompt_v1,
)
from pipeline.literary.incremental_identity_auditor_v1 import (
    IncrementalIdentityError,
    apply_incremental_identity_ledger_to_prefix_v1,
    apply_incremental_identity_ledger_to_review_v1,
    build_incremental_identity_index_v1,
    build_incremental_identity_ledger_v1,
    normalize_surface_scope_action_coverage_v1,
    render_incremental_identity_request_v1,
    validate_incremental_identity_response_v1,
    verify_incremental_identity_index_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    load_literary_structured_output_policy,
    resolve_structured_output_contract,
)
from pipeline.scripts.run_incremental_identity_auditor_v1_live import (
    _resolved_response_format,
)


DESIGN_DOC = Path(__file__).resolve().parents[3] / "design" / "LITERARY_PROMPT_DESIGN.md"
POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_structured_output_policy_v1.json"
)


def _document() -> dict:
    return {
        "document_id": "synthetic_identity_book",
        "chapters": [
            {
                "chapter_id": "syn_ch01",
                "blocks": [
                    {
                        "block_id": f"syn_ch01_b{index:03d}",
                        "order_index": index,
                        "source_text": text,
                    }
                    for index, text in enumerate(
                        [
                            "An old lintel bears the name Rowan Vale.",
                            "The inscription is dated many years earlier.",
                            "Nothing here identifies a living visitor.",
                        ],
                        start=1,
                    )
                ],
            },
            {
                "chapter_id": "syn_ch02",
                "blocks": [
                    {
                        "block_id": f"syn_ch02_b{index:03d}",
                        "order_index": index,
                        "source_text": text,
                    }
                    for index, text in enumerate(
                        [
                            "A young visitor answered to Rowan Vale.",
                            "The visitor crossed the room and spoke.",
                            "A witness distinguished the visitor from the inscription.",
                            "Later testimony supplies another attributed action.",
                        ],
                        start=1,
                    )
                ],
            },
        ],
    }


def _card(
    card_id: str,
    *,
    chapter_id: str,
    block_id: str,
    summary: str | None,
    extra_summary_dispute: bool = False,
    uncertainty_id: str = "synthetic_uncertainty",
) -> dict:
    disputes = [
        {
            "disputed_field": "identity_membership",
            "historical_value": None,
            "status": "pending",
            "pending_reason_codes": ["conflicting_evidence"],
            "evidence_manifest_hashes": [],
            "hearing_count": 0,
            "automatic_hearing_limit": 2,
            "same_evidence_reopen_forbidden": True,
            "next_review_trigger": "identity_resolution",
            "revision_ids": [],
            "uncertainty_id": uncertainty_id,
        }
    ]
    if extra_summary_dispute:
        disputes.append(
            {
                "disputed_field": "identity_summary",
                "historical_value": "Historical inscription.",
                "status": "pending",
                "pending_reason_codes": ["conflicting_evidence"],
                "evidence_manifest_hashes": ["1" * 64],
                "hearing_count": 1,
                "automatic_hearing_limit": 2,
                "same_evidence_reopen_forbidden": True,
                "next_review_trigger": "identity_resolution",
                "revision_ids": ["synthetic_revision"],
            }
        )
    body = {
        "prior_card_id": card_id,
        "canonical_surface": "Rowan Vale",
        "stable_surfaces": ["Rowan Vale"],
        "effective_claims": {
            "referent_kind": "person",
            "referential_gender": None,
            "identity_summary": summary,
        },
        "disputed_claims": disputes,
        "authority_scope": CANDIDATE_ONLY_SCOPE,
        "first_supported_block_id": block_id,
        "provenance_refs": [{"chapter_id": chapter_id, "block_id": block_id}],
        "source_candidate_id": f"source_{card_id}",
    }
    return {**body, "context_card_hash": canonical_hash(body)}


def _prefix() -> dict:
    document = _document()
    manifest = build_book_source_manifest(document)
    uncertainty_body = {
        "surface_key": "rowan vale",
        "prior_card_ids": ["pcard_left", "pcard_right"],
        "chapter_ids": ["syn_ch01", "syn_ch02"],
        "status": "pending_identity_review",
        "authority_effect": "candidate_only",
        "reason_code": "cross_chapter_surface_collision",
    }
    uncertainty = {
        "uncertainty_id": "prefixunc1_" + canonical_hash(uncertainty_body)[:20],
        **uncertainty_body,
    }
    left = _card(
        "pcard_left",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
        summary=None,
        extra_summary_dispute=True,
        uncertainty_id="historical_local_identity_uncertainty",
    )
    right = _card(
        "pcard_right",
        chapter_id="syn_ch02",
        block_id="syn_ch02_b001",
        summary="A young visitor known by this name.",
        uncertainty_id=uncertainty["uncertainty_id"],
    )
    body = {
        "schema_version": PREFIX_PRIOR_SCHEMA_VERSION,
        "validator_version": PREFIX_PRIOR_VALIDATOR_VERSION,
        "state_lineage_id": state_lineage_id_for_manifest(manifest),
        "book_source_manifest_hash": manifest["manifest_hash"],
        "coverage_through_chapter_id": "syn_ch02",
        "covered_chapter_ids": ["syn_ch01", "syn_ch02"],
        "audited_inventory_provenance": [],
        "claim_cards": [
            {"prior_card_id": "pcard_left"},
            {"prior_card_id": "pcard_right"},
        ],
        "b0_context_cards": [],
        "candidate_only_context_cards": [left, right],
        "source_entity_manifest": [],
        "glossary_context_cards": [],
        "source_glossary_manifest": [],
        "claim_projection_hashes": [],
        "glossary_projection_hashes": [],
        "prefix_identity_uncertainties": [uncertainty],
        "production_publish_performed": False,
    }
    return {**body, "prefix_bundle_hash": canonical_hash(body)}


def _review_item(
    *,
    lineage: str,
    item_no: int,
    card_ids: list[str],
    block_ids: list[str],
    disputed_field: str,
    evidence_seed: str,
) -> dict:
    body = {
        "state_lineage_id": lineage,
        "chapter_id": "syn_ch02",
        "source_kind": "synthetic_identity_evidence",
        "route": "book_identity_auditor",
        "subject_prior_card_ids": sorted(card_ids),
        "disputed_field": disputed_field,
        "source_block_ids": sorted(block_ids),
        "evidence_manifest_hash": canonical_hash({"evidence": evidence_seed}),
        "lifecycle_state": "queued",
        "authority_effect": "none",
        "reason_code": "identity_collision",
        "source_artifact_hash": canonical_hash({"artifact": item_no}),
        "reopen_classification": None,
    }
    identity_body = {
        key: value
        for key, value in body.items()
        if key
        in {
            "state_lineage_id",
            "chapter_id",
            "source_kind",
            "route",
            "subject_prior_card_ids",
            "disputed_field",
            "source_block_ids",
            "evidence_manifest_hash",
            "source_artifact_hash",
        }
    }
    return {
        "review_item_id": "cycrev1_" + canonical_hash(identity_body)[:20],
        **body,
    }


def _review(prefix: dict) -> dict:
    lineage = prefix["state_lineage_id"]
    rows = [
        _review_item(
            lineage=lineage,
            item_no=1,
            card_ids=["pcard_left", "pcard_right"],
            block_ids=["syn_ch01_b001", "syn_ch02_b001"],
            disputed_field="identity_membership",
            evidence_seed="collision",
        ),
        _review_item(
            lineage=lineage,
            item_no=2,
            card_ids=["pcard_left"],
            block_ids=["syn_ch01_b002"],
            disputed_field="identity_summary",
            evidence_seed="summary",
        ),
    ]
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": lineage,
        "coverage_through_chapter_id": "syn_ch02",
        "observed_queue_hashes": ["2" * 64],
        "review_items": rows,
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def _surface_review(prefix: dict) -> dict:
    row = _review_item(
        lineage=prefix["state_lineage_id"],
        item_no=9,
        card_ids=["pcard_left"],
        block_ids=["syn_ch02_b001"],
        disputed_field="alias_scope",
        evidence_seed="surface-scope",
    )
    row.update(
        {
            "review_case_id": "litcase1_surface_scope",
            "surface_key": "missis",
            "surface": "missis",
        }
    )
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": "syn_ch02",
        "observed_queue_hashes": ["9" * 64],
        "review_items": [row],
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def _index(*, max_source_blocks: int = 32) -> dict:
    prefix = _prefix()
    return build_incremental_identity_index_v1(
        document=_document(),
        prefix_bundle=prefix,
        review_ledger=_review(prefix),
        max_source_blocks=max_source_blocks,
    )


def _response(index: dict, actions: list[dict]) -> dict:
    return {
        "component_id": index["components"][0]["component_id"],
        "candidate_actions": actions,
        "surface_scope_actions": [],
    }


def _action(card_id: str, action: str, target: str | None = None) -> dict:
    return {
        "prior_card_id": card_id,
        "action": action,
        "target_prior_card_id": target,
        "source_block_ids": ["syn_ch01_b001", "syn_ch02_b001"],
        "resolution_note": "The supplied passages support this provisional action.",
    }


def _decision(index: dict, actions: list[dict]) -> dict:
    return validate_incremental_identity_response_v1(
        _response(index, actions), index=index, request_fingerprint="f" * 64
    )


def test_prompt_is_pinned_book_neutral_and_loader_faithful() -> None:
    prompt = load_incremental_identity_prompt_v1(DESIGN_DOC)
    assert PROMPT_ID in prompt
    assert sha256(prompt.encode("utf-8")).hexdigest() == PROMPT_SHA256
    assert len(prompt.encode("utf-8")) == 4536
    assert all(
        term.casefold() not in prompt.casefold()
        for term in ("Hareton", "Heathcliff", "Wuthering Heights", "Catherine")
    )


def test_index_collapses_connected_review_rows_and_renders_reasoned_leads() -> None:
    index = verify_incremental_identity_index_v1(_index())
    assert len(index["components"]) == 1
    component = index["components"][0]
    assert component["candidate_prior_card_ids"] == ["pcard_left", "pcard_right"]
    assert len(component["review_leads"]) == 2
    assert component["surface_keys"] == ["rowan vale"]


def test_single_card_surface_scope_case_gets_a_bounded_hearing() -> None:
    prefix = _prefix()
    review = _surface_review(prefix)
    index = build_incremental_identity_index_v1(
        document=_document(),
        prefix_bundle=prefix,
        review_ledger=review,
    )
    assert len(index["components"]) == 1
    component = index["components"][0]
    assert component["candidate_prior_card_ids"] == ["pcard_left"]
    assert component["surface_keys"] == ["missis"]
    response = {
        "component_id": component["component_id"],
        "candidate_actions": [
            {
                "prior_card_id": "pcard_left",
                "action": "keep",
                "target_prior_card_id": None,
                "source_block_ids": ["syn_ch02_b001"],
                "resolution_note": "The supplied passage supports retaining the card.",
            }
        ],
        "surface_scope_actions": [
            {
                "review_item_id": review["review_items"][0]["review_item_id"],
                "action": "confirm_block_scope",
                "target_prior_card_id": "pcard_left",
                "valid_block_ids": ["syn_ch02_b001"],
                "source_block_ids": ["syn_ch02_b001"],
                "evidence_needed": None,
                "resolution_note": "The supplied use is attributable only in this block.",
            }
        ],
    }
    decision = validate_incremental_identity_response_v1(
        response,
        index=index,
        request_fingerprint="e" * 64,
    )
    assert decision["surface_scope_actions"][0]["action"] == "confirm_block_scope"
    ledger = build_incremental_identity_ledger_v1(
        index=index,
        decisions=[decision],
    )
    projected = apply_incremental_identity_ledger_to_review_v1(
        review_ledger=review,
        identity_ledger=ledger,
    )
    assert projected["review_items"][0]["lifecycle_state"] == "closed"
    rendered = render_incremental_identity_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    assert rendered.semantic_payload["review_leads"][0]["reason_code"]
    assert "review_leads" not in rendered.semantic_payload["component"]
    assert set(rendered.semantic_payload["candidate_cards"][0]) == {
        "prior_card_id",
        "canonical_surface",
        "stable_surfaces",
        "effective_claims",
        "disputed_claims",
        "authority_scope",
        "first_supported_block_id",
    }
    assert set(
        rendered.semantic_payload["candidate_cards"][0]["disputed_claims"][0]
    ) == {
        "disputed_field",
        "historical_value",
        "status",
        "pending_reason_codes",
    }
    assert rendered.semantic_payload["authority_boundary"]["book_global_authority"] is False


def test_non_surface_review_action_is_ignored_without_losing_identity_hearing() -> None:
    index = _index()
    response = _response(
        index,
        [_action("pcard_left", "keep"), _action("pcard_right", "keep")],
    )
    lead = index["components"][0]["review_leads"][0]
    assert lead["disputed_field"] == "identity_membership"
    response["surface_scope_actions"] = [
        {
            "review_item_id": lead["review_item_id"],
            "action": "dismiss_dormant",
            "target_prior_card_id": None,
            "valid_block_ids": [],
            "source_block_ids": list(lead["source_block_ids"]),
            "evidence_needed": None,
            "resolution_note": "This row was placed in the wrong output table.",
        }
    ]

    with pytest.raises(
        IncrementalIdentityError,
        match="surface-scope actions do not exact-cover supplied leads",
    ):
        validate_incremental_identity_response_v1(
            response,
            index=index,
            request_fingerprint="a" * 64,
        )

    normalized, records = normalize_surface_scope_action_coverage_v1(
        response,
        index=index,
    )
    decision = validate_incremental_identity_response_v1(
        normalized,
        index=index,
        request_fingerprint="a" * 64,
    )
    assert decision["surface_scope_actions"] == []
    assert records == [
        {
            "normalization_kind": "non_surface_action_ignored",
            "component_id": index["components"][0]["component_id"],
            "review_item_id": lead["review_item_id"],
            "disputed_field": "identity_membership",
            "original_action": "dismiss_dormant",
            "normalized_action": None,
        }
    ]


def test_missing_surface_action_is_materialized_as_pending_without_authority() -> None:
    prefix = _prefix()
    review = _surface_review(prefix)
    index = build_incremental_identity_index_v1(
        document=_document(),
        prefix_bundle=prefix,
        review_ledger=review,
    )
    response = {
        "component_id": index["components"][0]["component_id"],
        "candidate_actions": [
            {
                "prior_card_id": "pcard_left",
                "action": "keep",
                "target_prior_card_id": None,
                "source_block_ids": ["syn_ch02_b001"],
                "resolution_note": "The supplied source keeps the candidate distinct.",
            }
        ],
        "surface_scope_actions": [],
    }

    normalized, records = normalize_surface_scope_action_coverage_v1(
        response,
        index=index,
    )
    decision = validate_incremental_identity_response_v1(
        normalized,
        index=index,
        request_fingerprint="b" * 64,
    )
    assert decision["status"] == "pending"
    assert decision["surface_scope_actions"][0]["action"] == "keep_pending"
    assert decision["surface_scope_actions"][0]["evidence_needed"] == (
        "scope_disambiguation"
    )
    assert records[0]["normalization_kind"] == "missing_surface_action_pending"


def test_response_exact_cover_and_link_target_are_mechanical_gates() -> None:
    index = _index()
    with pytest.raises(IncrementalIdentityError, match="exact-cover"):
        _decision(index, [_action("pcard_left", "keep")])
    with pytest.raises(IncrementalIdentityError, match="target must be kept"):
        _decision(
            index,
            [
                _action("pcard_left", "link_to", "pcard_right"),
                _action("pcard_right", "pending"),
            ],
        )
    with pytest.raises(IncrementalIdentityError, match="foreign source"):
        foreign = _action("pcard_left", "keep")
        foreign["source_block_ids"] = ["foreign_b001"]
        _decision(index, [foreign, _action("pcard_right", "keep")])


def test_keep_distinct_restores_only_card_without_other_pending_claims() -> None:
    prefix = _prefix()
    review = _review(prefix)
    index = _index()
    decision = _decision(
        index, [_action("pcard_left", "keep"), _action("pcard_right", "keep")]
    )
    ledger = build_incremental_identity_ledger_v1(
        index=index, decisions=[decision]
    )
    projected = apply_incremental_identity_ledger_to_prefix_v1(
        prefix_bundle=prefix, identity_ledger=ledger
    )
    active = {row["prior_card_id"] for row in projected["b0_context_cards"]}
    candidate = {
        row["prior_card_id"] for row in projected["candidate_only_context_cards"]
    }
    assert active == {"pcard_right"}
    assert candidate == {"pcard_left"}
    assert projected["prefix_identity_uncertainties"] == []
    reviewed = apply_incremental_identity_ledger_to_review_v1(
        review_ledger=review, identity_ledger=ledger
    )
    assert sum(row["lifecycle_state"] == "closed" for row in reviewed["review_items"]) == 2
    followups = [
        row
        for row in reviewed["review_items"]
        if row["source_kind"] == "incremental_identity_followup"
    ]
    assert len(followups) == 1
    assert followups[0]["route"] == "stable_claim_rehearing"
    assert followups[0]["lifecycle_state"] == "queued"
    assert followups[0]["disputed_field"] == "identity_summary"


def test_pending_is_replay_suppressed_until_evidence_changes() -> None:
    prefix = _prefix()
    review = _review(prefix)
    index = _index()
    pending = _decision(
        index,
        [_action("pcard_left", "pending"), _action("pcard_right", "pending")],
    )
    ledger = build_incremental_identity_ledger_v1(index=index, decisions=[pending])
    replay = build_incremental_identity_index_v1(
        document=_document(),
        prefix_bundle=prefix,
        review_ledger=review,
        previous_identity_ledger=ledger,
    )
    assert replay["components"][0]["trigger_state"] == "duplicate_suppressed"
    with pytest.raises(IncrementalIdentityError, match="unchanged identity evidence"):
        render_incremental_identity_request_v1(
            index=replay,
            component_id=replay["components"][0]["component_id"],
            document=_document(),
            design_doc=DESIGN_DOC,
            previous_identity_ledger=ledger,
        )


def test_single_card_new_evidence_reopens_the_previous_component() -> None:
    prefix = _prefix()
    review = _review(prefix)
    index = _index()
    pending = _decision(
        index,
        [_action("pcard_left", "pending"), _action("pcard_right", "pending")],
    )
    ledger = build_incremental_identity_ledger_v1(index=index, decisions=[pending])
    next_review = deepcopy(review)
    body = dict(next_review)
    body.pop("review_ledger_hash")
    body["review_items"] = [
        _review_item(
            lineage=prefix["state_lineage_id"],
            item_no=3,
            card_ids=["pcard_right"],
            block_ids=["syn_ch02_b004"],
            disputed_field="identity_membership",
            evidence_seed="new attributed action",
        )
    ]
    next_review = {**body, "review_ledger_hash": canonical_hash(body)}
    reopened = build_incremental_identity_index_v1(
        document=_document(),
        prefix_bundle=prefix,
        review_ledger=next_review,
        previous_identity_ledger=ledger,
    )
    assert len(reopened["components"]) == 1
    assert reopened["components"][0]["candidate_prior_card_ids"] == [
        "pcard_left",
        "pcard_right",
    ]
    assert reopened["components"][0]["trigger_state"] == "new_evidence"
    assert reopened["components"][0]["prior_hearing_count"] == 1


def test_provisional_link_keeps_ticket_reopenable_and_grants_no_prefix_authority() -> None:
    prefix = _prefix()
    review = _review(prefix)
    index = _index()
    link = _decision(
        index,
        [
            _action("pcard_left", "link_to", "pcard_right"),
            _action("pcard_right", "keep"),
        ],
    )
    assert link["status"] == "provisional_link"
    assert link["authority_effect"] == "none"
    ledger = build_incremental_identity_ledger_v1(index=index, decisions=[link])
    projected = apply_incremental_identity_ledger_to_prefix_v1(
        prefix_bundle=prefix, identity_ledger=ledger
    )
    assert projected["prefix_bundle_hash"] == prefix["prefix_bundle_hash"]
    assert not projected["b0_context_cards"]
    reviewed = apply_incremental_identity_ledger_to_review_v1(
        review_ledger=review, identity_ledger=ledger
    )
    by_field = {row["disputed_field"]: row for row in reviewed["review_items"]}
    assert by_field["identity_membership"]["lifecycle_state"] == "queued"
    assert by_field["identity_summary"]["lifecycle_state"] == "queued"


def test_overflow_is_visible_and_cannot_be_rendered() -> None:
    index = _index(max_source_blocks=4)
    component = index["components"][0]
    assert component["overflow"] is True
    with pytest.raises(IncrementalIdentityError, match="overflow"):
        render_incremental_identity_request_v1(
            index=index,
            component_id=component["component_id"],
            document=_document(),
            design_doc=DESIGN_DOC,
        )


def test_identity_transport_uses_the_sealed_structured_output_contract() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rows"],
        "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
    }
    policy = load_literary_structured_output_policy(POLICY_PATH)
    contract = resolve_structured_output_contract(
        policy,
        role_id="literary_incremental_identity_auditor",
        provider="openai",
        base_url=None,
        model_id="gpt-5.4",
        canonical_schema=schema,
    )

    response_format = _resolved_response_format(schema, contract)

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == contract.transport_schema
