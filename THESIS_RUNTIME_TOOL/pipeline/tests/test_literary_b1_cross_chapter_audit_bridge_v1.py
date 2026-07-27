"""Adversarial tests for the offline queue-to-Auditor bridge (Task A)."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b1_cross_chapter_audit_bridge_v1 import (
    ALIAS_REFERRAL_VERDICTS,
    IDENTITY_LINKAGE_VERDICTS,
    SPURIOUS_REFERRAL_VERDICTS,
    STABLE_CLAIM_VERDICTS,
    B1CrossChapterAuditBridgeError,
    allowed_verdicts_for_component_v1,
    build_cross_chapter_audit_dry_run_v1,
    partition_hearing_queue_v1,
    render_identity_hearing_request_v1,
    render_stable_claim_hearing_request_v1,
    validate_identity_hearing_response_v1,
    validate_stable_claim_hearing_response_v1,
    verify_hearing_queue_binding_v1,
)
from pipeline.literary.b1_chapter_registry_writer_v1 import (
    CROSS_CHAPTER_QUEUE_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash

DESIGN_DOC = Path(__file__).resolve().parents[3] / "design" / "LITERARY_PROMPT_DESIGN.md"

MODEL_CONTRACT = {
    "model_id": "dry_run_unbound",
    "reasoning_effort": "none",
    "temperature": 1.0,
    "seed": 20260721,
    "max_output_tokens": 2500,
}

# Synthetic, book-neutral fixture surfaces.  These names exist only in tests.
PRIOR_INSCRIPTION_CARD = {
    "prior_card_id": "b0ent_priorinscription01",
    "canonical_surface": "Rowan Aldercote",
    "record_class": "unresolved_named_reference",
    "claim_state": "provisional",
    "referent_kind": "unknown",
    "presence_basis": "inscription_or_document",
    "identity_summary": "A name that appears only as carved text above a gate; no living referent is established.",
    "stable_surfaces": ["Rowan Aldercote"],
    "support_block_ids": ["bk_ch01_b012"],
    "provenance_refs": [{"chapter_id": "bk_ch01", "block_id": "bk_ch01_b012"}],
}

CURRENT_CARD = {
    "entity_id": "b1ent_current02",
    "canonical_surface": "Rowan Aldercote",
    "record_class": "named_entity_candidate",
    "referent_kind": "person",
    "support_block_ids": ["bk_ch02_b005"],
}

CURRENT_DOSSIER = {
    "scan_observation_id": "b1obs_current02",
    "surface": "Rowan Aldercote",
    "identity_summary": "A young man who speaks and acts directly in this chapter.",
    "claims": [
        {
            "field": "gender",
            "value": "masculine",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch02_b005"],
        }
    ],
    "evidence_block_ids": ["bk_ch02_b005", "bk_ch02_b007"],
}

SOURCE_BLOCKS = {
    "bk_ch01_b012": "Above the gate the carving read 'Rowan Aldercote' with an old date.",
    "bk_ch02_b005": "'My name is Rowan Aldercote,' said the young man, setting down the pail.",
    "bk_ch02_b007": "The young man walked out toward the barn without another word.",
    "bk_ch02_b009": "She kept the household ledger in the evening.",
}


def _seal_component(body: dict) -> dict:
    return {"component_id": "b1xhear_" + canonical_hash(body)[:20], **body}


def _identity_component(**overrides) -> dict:
    body = {
        "question_type": "identity_linkage",
        "review_route": "identity_auditor",
        "continuity_case_id": "b1cont_syntheticcase0001",
        "prior_card_id": PRIOR_INSCRIPTION_CARD["prior_card_id"],
        "current_scan_observation_ids": [CURRENT_DOSSIER["scan_observation_id"]],
        "current_entity_ids": [CURRENT_CARD["entity_id"]],
        "prior_card_snapshot": deepcopy(PRIOR_INSCRIPTION_CARD),
        "current_card_snapshots": [deepcopy(CURRENT_CARD)],
        "current_dossier_snapshots": [deepcopy(CURRENT_DOSSIER)],
        "source_block_ids": ["bk_ch02_b005", "bk_ch02_b007"],
        "trigger": {
            "scan_verdict": "propose_distinct",
            "reason_code": "presence_class_mismatch",
            "reason": "The prior record is an unresolved carved name; the current referent speaks directly.",
            "mechanical_risk_codes": ["prior_record_is_not_confirmed_entity"],
        },
        "evidence_manifest_hash": canonical_hash({"synthetic": "case0001"}),
        "lifecycle_state": "ready_for_hearing",
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }
    body.update(overrides)
    return _seal_component(body)


def _stable_component(**overrides) -> dict:
    body = {
        "question_type": "stable_claim",
        "review_route": "stable_claim_auditor",
        "continuity_case_ids": ["b1cont_syntheticcase0002"],
        "prior_card_id": "b0ent_priorperson02",
        "current_scan_observation_id": "b1obs_current03",
        "current_entity_id": "b1ent_current03",
        "prior_card_snapshot": {
            "prior_card_id": "b0ent_priorperson02",
            "canonical_surface": "Maren Tull",
            "record_class": "confirmed_entity",
            "claim_state": "confirmed",
            "referent_kind": "person",
            "provenance_refs": [{"chapter_id": "bk_ch01", "block_id": "bk_ch01_b012"}],
        },
        "current_card_snapshot": {
            "entity_id": "b1ent_current03",
            "canonical_surface": "Maren Tull",
            "referent_kind": "person",
        },
        "current_dossier_snapshot": {
            "scan_observation_id": "b1obs_current03",
            "surface": "Maren Tull",
            "identity_summary": "The housekeeper of the valley farm in this chapter.",
        },
        "field": "role_or_occupation",
        "existing_value": "traveling merchant",
        "observed_value": "housekeeper",
        "source_block_ids": ["bk_ch02_b009"],
        "reason": "This chapter shows a household role that conflicts with the stored value.",
        "lifecycle_state": "ready_for_hearing",
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }
    body.update(overrides)
    return _seal_component(body)


def _referral_component(kind: str, **overrides) -> dict:
    body = {
        "question_type": "local_cross_chapter_referral",
        "review_route": "identity_auditor",
        "local_component_id": "b1lac_syntheticlocal01",
        "component_kind": kind,
        "subject_ref": "scan:b1obs_current02",
        "prior_card_id": PRIOR_INSCRIPTION_CARD["prior_card_id"],
        "current_entity_id": CURRENT_CARD["entity_id"],
        "current_card_snapshot": deepcopy(CURRENT_CARD),
        "current_dossier_snapshot": deepcopy(CURRENT_DOSSIER),
        "original_proposal": {"note": "referred across chapters by the Local Auditor"},
        "source_block_ids": ["bk_ch02_b005"],
        "resolution_note": "Needs evidence from another chapter.",
        "lifecycle_state": "ready_for_hearing",
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }
    body.update(overrides)
    return _seal_component(body)


def _seal_queue(components: list[dict], **overrides) -> dict:
    body = {
        "schema_version": CROSS_CHAPTER_QUEUE_SCHEMA_VERSION,
        "chapter_id": "bk_ch02",
        "registry_hash": canonical_hash({"synthetic_registry": 2}),
        "scan_artifact_hash": canonical_hash({"synthetic_scan": 2}),
        "enrich_artifact_hash": canonical_hash({"synthetic_enrich": 2}),
        "local_audit_artifact_hash": canonical_hash({"synthetic_audit": 2}),
        "components": components,
        "metrics": {
            "component_count": len(components),
            "ready_for_hearing_count": sum(
                1 for row in components if row["lifecycle_state"] == "ready_for_hearing"
            ),
            "waiting_count": sum(
                1 for row in components if row["lifecycle_state"] != "ready_for_hearing"
            ),
            "counts_by_route": {
                route: sum(1 for row in components if row["review_route"] == route)
                for route in sorted({row["review_route"] for row in components})
            },
        },
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
    }
    body.update(overrides)
    return {**body, "queue_hash": canonical_hash(body)}


def _dry_run(queue: dict, **overrides):
    kwargs = {
        "queue": queue,
        "source_blocks": SOURCE_BLOCKS,
        "design_doc": DESIGN_DOC,
        "model_contract": MODEL_CONTRACT,
        "expected_registry_hash": queue["registry_hash"],
    }
    kwargs.update(overrides)
    return build_cross_chapter_audit_dry_run_v1(**kwargs)


# ---------------------------------------------------------------------------
# required Task A scenarios
# ---------------------------------------------------------------------------


def test_inscription_versus_present_namesake_reaches_identity_auditor() -> None:
    queue = _seal_queue([_identity_component()])
    report = _dry_run(queue)

    assert report["coverage"] == {
        "component_count": 1,
        "prepared_count": 1,
        "waiting_count": 0,
        "unconsumed_ready_count": 0,
        "covered_component_ids": [queue["components"][0]["component_id"]],
    }
    request = report["prepared_requests"][0]
    assert request["review_route"] == "identity_auditor"
    sections = request["sections"]
    # both sides of the hearing are present with their evidence
    assert sections["prior_candidate_snapshots"][0]["record_class"] == (
        "unresolved_named_reference"
    )
    assert sections["current_dossier_snapshots"][0]["surface"] == "Rowan Aldercote"
    resolved = {row["block_id"] for row in sections["source_blocks"]}
    assert {"bk_ch01_b012", "bk_ch02_b005", "bk_ch02_b007"} <= resolved
    assert sections["allowed_verdicts"] == list(IDENTITY_LINKAGE_VERDICTS)
    # no identity answer is pre-selected anywhere in the packet or the prompt
    system_prompt = request["messages"][0]["content"]
    for verdict in IDENTITY_LINKAGE_VERDICTS:
        assert f'"verdict":"{verdict}"' not in request["messages"][1]["content"]
    assert "Rowan" not in system_prompt  # prompt stays book/fixture-neutral
    assert request["provider_calls"] == 0
    assert report["provider_calls"] == 0


def test_approved_continuation_produces_no_identity_hearing() -> None:
    # an approved continuation never becomes a queue component, so a queue with
    # only a stable-claim conflict must render zero identity requests
    queue = _seal_queue([_stable_component()])
    report = _dry_run(queue)
    routes = [row["review_route"] for row in report["prepared_requests"]]
    assert routes == ["stable_claim_auditor"]


def test_stable_field_conflict_reaches_only_stable_claim_auditor() -> None:
    # route comes from the recorded field, not from prose: the reason text
    # mentions identity-sounding words but the component stays a stable claim
    component = _stable_component(
        reason="Two people may share this name, but this row records a field conflict.",
    )
    queue = _seal_queue([component])
    report = _dry_run(queue)
    assert len(report["prepared_requests"]) == 1
    request = report["prepared_requests"][0]
    assert request["review_route"] == "stable_claim_auditor"
    assert request["sections"]["allowed_verdicts"] == list(STABLE_CLAIM_VERDICTS)
    assert request["sections"]["field"] == "role_or_occupation"


def test_additional_entity_referral_carries_enrich_dossier() -> None:
    component = _referral_component("additional_entity")
    queue = _seal_queue([component])
    report = _dry_run(queue)
    request = report["prepared_requests"][0]
    sections = request["sections"]
    assert sections["referral"]["component_kind"] == "additional_entity"
    assert sections["current_dossier_snapshots"][0]["identity_summary"] == (
        CURRENT_DOSSIER["identity_summary"]
    )
    assert sections["allowed_verdicts"] == list(IDENTITY_LINKAGE_VERDICTS)


def test_referral_verdict_sets_follow_component_kind() -> None:
    assert allowed_verdicts_for_component_v1(
        _referral_component("alias_proposal")
    ) == ALIAS_REFERRAL_VERDICTS
    assert allowed_verdicts_for_component_v1(
        _referral_component("spurious_challenge")
    ) == SPURIOUS_REFERRAL_VERDICTS


def test_waiting_components_are_persisted_but_not_rendered() -> None:
    waiting = _identity_component(
        lifecycle_state="waiting_for_enrichment",
        current_dossier_snapshots=[],
    )
    queue = _seal_queue([waiting, _stable_component()])
    report = _dry_run(queue)
    assert [row["review_route"] for row in report["prepared_requests"]] == [
        "stable_claim_auditor"
    ]
    assert report["waiting_components"] == [
        {
            "component_id": waiting["component_id"],
            "review_route": "identity_auditor",
            "question_type": "identity_linkage",
            "lifecycle_state": "waiting_for_enrichment",
        }
    ]


def test_unconsumed_routes_stay_visible() -> None:
    temporal = _referral_component(
        "entity_link", review_route="temporal_auditor"
    )
    glossary = _referral_component(
        "glossary_ambiguity", review_route="glossary_auditor"
    )
    pending = _referral_component(
        "unknown_kind_for_routing", review_route="pending_unassigned",
        lifecycle_state="pending_route",
    )
    queue = _seal_queue([temporal, glossary, pending, _identity_component()])
    report = _dry_run(queue)
    assert report["unconsumed_routes"]["temporal_auditor"] == [temporal["component_id"]]
    assert report["unconsumed_routes"]["glossary_auditor"] == [glossary["component_id"]]
    assert report["unconsumed_routes"]["pending_unassigned"] == []
    assert report["waiting_components"][0]["component_id"] == pending["component_id"]
    assert report["coverage"]["component_count"] == 4
    assert report["coverage"]["prepared_count"] == 1


# ---------------------------------------------------------------------------
# fail-closed behavior
# ---------------------------------------------------------------------------


def test_foreign_component_id_fails_closed() -> None:
    component = _identity_component()
    component["component_id"] = "b1xhear_forgedforgedforge"
    queue = _seal_queue([component])
    with pytest.raises(B1CrossChapterAuditBridgeError, match="sealed body"):
        verify_hearing_queue_binding_v1(queue)


def test_tampered_snapshot_fails_closed() -> None:
    component = _identity_component()
    queue = _seal_queue([component])
    tampered = deepcopy(queue)
    tampered["components"][0]["prior_card_snapshot"]["identity_summary"] = (
        "A living person fully confirmed in the earlier chapter."
    )
    # queue hash notices first; recompute it to model a full-file forgery, then
    # the component id check must still catch the edit
    body = {key: value for key, value in tampered.items() if key != "queue_hash"}
    tampered["queue_hash"] = canonical_hash(body)
    with pytest.raises(B1CrossChapterAuditBridgeError, match="sealed body"):
        verify_hearing_queue_binding_v1(tampered)


def test_stale_registry_hash_fails_closed() -> None:
    queue = _seal_queue([_identity_component()])
    with pytest.raises(B1CrossChapterAuditBridgeError, match="stale or foreign"):
        _dry_run(queue, expected_registry_hash=canonical_hash({"other": 1}))


def test_duplicate_component_coverage_fails_closed() -> None:
    component = _identity_component()
    queue = _seal_queue([component, deepcopy(component)])
    with pytest.raises(Exception, match="duplicat|twice"):
        _dry_run(queue)


def test_unknown_route_fails_closed() -> None:
    component = _identity_component(review_route="mystery_auditor")
    queue = _seal_queue([component])
    with pytest.raises(Exception, match="route"):
        _dry_run(queue)


def test_missing_source_block_text_fails_closed() -> None:
    queue = _seal_queue([_identity_component()])
    blocks = {k: v for k, v in SOURCE_BLOCKS.items() if k != "bk_ch01_b012"}
    with pytest.raises(B1CrossChapterAuditBridgeError, match="cannot be resolved"):
        _dry_run(queue, source_blocks=blocks)


def test_component_without_cited_blocks_fails_closed() -> None:
    component = _identity_component(
        source_block_ids=[],
        prior_card_snapshot={
            **deepcopy(PRIOR_INSCRIPTION_CARD),
            "support_block_ids": [],
            "provenance_refs": [],
        },
    )
    queue = _seal_queue([component])
    with pytest.raises(B1CrossChapterAuditBridgeError, match="cites no source blocks"):
        _dry_run(queue)


# ---------------------------------------------------------------------------
# response validators (consumer-side contract, no call performed)
# ---------------------------------------------------------------------------


def _identity_response(**overrides) -> dict:
    row = {
        "component_id": None,
        "verdict": "confirmed_distinct",
        "evidence": [
            {"block_id": "bk_ch02_b005", "quote": "'My name is Rowan Aldercote,'"}
        ],
        "reason": "The current referent speaks directly; the prior record is carved text only.",
        "resolution_condition": None,
    }
    row.update(overrides)
    return row


def test_identity_response_verdict_and_merge_rules() -> None:
    component = _identity_component()
    supplied = ["bk_ch01_b012", "bk_ch02_b005", "bk_ch02_b007"]
    ok = validate_identity_hearing_response_v1(
        _identity_response(component_id=component["component_id"]),
        component=component,
        supplied_block_ids=supplied,
    )
    assert ok["verdict"] == "confirmed_distinct"
    assert ok["identity_authority_granted"] is False

    with pytest.raises(B1CrossChapterAuditBridgeError, match="allowed set"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"], verdict="alias_confirmed"
            ),
            component=component,
            supplied_block_ids=supplied,
        )

    with pytest.raises(B1CrossChapterAuditBridgeError, match="supplied prior card"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"],
                verdict="merge_referents",
                merge_target_prior_card_id="b0ent_someoneelse",
            ),
            component=component,
            supplied_block_ids=supplied,
        )

    with pytest.raises(B1CrossChapterAuditBridgeError, match="outside the supplied"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"],
                evidence=[{"block_id": "bk_ch09_b001", "quote": "elsewhere"}],
            ),
            component=component,
            supplied_block_ids=supplied,
        )

    with pytest.raises(B1CrossChapterAuditBridgeError, match="echo"):
        validate_identity_hearing_response_v1(
            _identity_response(component_id="b1xhear_wrong"),
            component=component,
            supplied_block_ids=supplied,
        )

    with pytest.raises(B1CrossChapterAuditBridgeError, match="unknown keys"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"], confidence=0.9
            ),
            component=component,
            supplied_block_ids=supplied,
        )


def test_identity_response_preserves_supported_partial_exclusion() -> None:
    second = {
        **deepcopy(PRIOR_INSCRIPTION_CARD),
        "prior_card_id": "b0ent_othercandidate02",
        "canonical_surface": "Rowan Vale",
        "stable_surfaces": ["Rowan Vale"],
    }
    body = deepcopy(_identity_component())
    body.pop("component_id")
    body.pop("prior_card_id")
    body.pop("prior_card_snapshot")
    body["prior_card_ids"] = sorted(
        [PRIOR_INSCRIPTION_CARD["prior_card_id"], second["prior_card_id"]]
    )
    body["prior_candidate_snapshots"] = sorted(
        [deepcopy(PRIOR_INSCRIPTION_CARD), second],
        key=lambda row: row["prior_card_id"],
    )
    component = _seal_component(body)
    excluded = PRIOR_INSCRIPTION_CARD["prior_card_id"]
    response = _identity_response(
        component_id=component["component_id"],
        verdict="insufficient_evidence",
        excluded_prior_card_ids=[excluded],
        evidence=[
            {
                "block_id": "bk_ch01_b012",
                "quote": "Above the gate the carving read 'Rowan Aldercote' with an old date.",
                "supports_excluded_prior_card_ids": [excluded],
            }
        ],
        resolution_condition="A later block must identify which remaining candidate acts here.",
    )

    accepted = validate_identity_hearing_response_v1(
        response,
        component=component,
        supplied_block_ids=["bk_ch01_b012", "bk_ch02_b005", "bk_ch02_b007"],
    )
    assert accepted["excluded_prior_card_ids"] == [excluded]

    unsupported = deepcopy(response)
    unsupported["evidence"][0]["supports_excluded_prior_card_ids"] = []
    with pytest.raises(B1CrossChapterAuditBridgeError, match="requires a supporting"):
        validate_identity_hearing_response_v1(
            unsupported,
            component=component,
            supplied_block_ids=["bk_ch01_b012", "bk_ch02_b005", "bk_ch02_b007"],
        )


def test_prior_evidence_expansion_is_default_off_and_prior_side_only() -> None:
    queue = _seal_queue(
        [_identity_component()],
        registry_roster_surfaces=["Maren Tull"],
    )
    blocks = {
        **SOURCE_BLOCKS,
        "bk_ch01_b010": "Maren Tull stood beneath the arch.",
        "bk_ch01_b011": "The rain had darkened the stones.",
        "bk_ch01_b013": "A later hand had repaired the lintel.",
        "bk_ch01_b014": "The yard beyond it was empty.",
        "bk_ch02_b004": "A pail rang against the threshold.",
        "bk_ch02_b006": "Someone crossed the room.",
    }
    component = queue["components"][0]

    narrow = render_identity_hearing_request_v1(
        component,
        queue=queue,
        source_blocks=blocks,
        design_doc=DESIGN_DOC,
        model_contract=MODEL_CONTRACT,
    )
    assert narrow["sections"]["prior_evidence_expansion"]["enabled"] is False
    assert all(
        row["role"] == "card_evidence"
        for row in narrow["sections"]["source_blocks"]
    )

    widened = render_identity_hearing_request_v1(
        component,
        queue=queue,
        source_blocks=blocks,
        design_doc=DESIGN_DOC,
        model_contract=MODEL_CONTRACT,
        expand_prior_evidence=True,
    )
    context_ids = widened["sections"]["prior_evidence_expansion"][
        "context_block_ids"
    ]
    assert context_ids == [
        "bk_ch01_b010",
        "bk_ch01_b011",
        "bk_ch01_b013",
        "bk_ch01_b014",
    ]
    assert "bk_ch02_b004" not in context_ids
    assert "bk_ch02_b006" not in context_ids
    roles = {
        row["block_id"]: row["role"]
        for row in widened["sections"]["source_blocks"]
    }
    assert roles["bk_ch01_b012"] == "card_evidence"
    assert roles["bk_ch01_b010"] == "context"


def test_stable_response_anchor_rules() -> None:
    component = _stable_component()
    supplied = ["bk_ch01_b012", "bk_ch02_b009"]
    base = {
        "component_id": component["component_id"],
        "evidence": [{"block_id": "bk_ch02_b009", "quote": "She kept the household ledger"}],
        "reason": "The later chapter states the household role in narration.",
        "resolution_condition": None,
    }
    ok = validate_stable_claim_hearing_response_v1(
        {**base, "verdict": "in_story_change", "effective_from_block_id": "bk_ch02_b009"},
        component=component,
        supplied_block_ids=supplied,
    )
    assert ok["effective_from_block_id"] == "bk_ch02_b009"

    with pytest.raises(B1CrossChapterAuditBridgeError, match="requires effective_from"):
        validate_stable_claim_hearing_response_v1(
            {**base, "verdict": "in_story_change"},
            component=component,
            supplied_block_ids=supplied,
        )

    with pytest.raises(B1CrossChapterAuditBridgeError, match="only legal"):
        validate_stable_claim_hearing_response_v1(
            {
                **base,
                "verdict": "uphold_existing",
                "revealed_at_block_id": "bk_ch02_b009",
            },
            component=component,
            supplied_block_ids=supplied,
        )

    with pytest.raises(B1CrossChapterAuditBridgeError, match="closed set"):
        validate_stable_claim_hearing_response_v1(
            {**base, "verdict": "merge_referents"},
            component=component,
            supplied_block_ids=supplied,
        )


def test_insufficient_evidence_requires_evidence_and_resolution_condition() -> None:
    component = _identity_component()
    supplied = ["bk_ch01_b012", "bk_ch02_b005", "bk_ch02_b007"]
    ok = validate_identity_hearing_response_v1(
        _identity_response(
            component_id=component["component_id"],
            verdict="insufficient_evidence",
            resolution_condition="A later block must explicitly identify the inscription's referent.",
        ),
        component=component,
        supplied_block_ids=supplied,
    )
    assert ok["verdict"] == "insufficient_evidence"
    assert ok["resolution_condition"].startswith("A later block")
    with pytest.raises(B1CrossChapterAuditBridgeError, match="at least one evidence"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"],
                evidence=[],
                resolution_condition="A later block must identify the referent.",
            ),
            component=component,
            supplied_block_ids=supplied,
        )
    with pytest.raises(B1CrossChapterAuditBridgeError, match="resolution_condition"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"],
                verdict="insufficient_evidence",
                resolution_condition=None,
            ),
            component=component,
            supplied_block_ids=supplied,
        )
    with pytest.raises(B1CrossChapterAuditBridgeError, match="only legal"):
        validate_identity_hearing_response_v1(
            _identity_response(
                component_id=component["component_id"],
                resolution_condition="irrelevant after a decision",
            ),
            component=component,
            supplied_block_ids=supplied,
        )


def test_stable_insufficient_evidence_requires_evidence_and_condition() -> None:
    component = _stable_component()
    supplied = ["bk_ch01_b012", "bk_ch02_b009"]
    base = {
        "component_id": component["component_id"],
        "verdict": "insufficient_evidence",
        "evidence": [{"block_id": "bk_ch02_b009", "quote": "A supplied claim"}],
        "reason": "The supplied blocks do not settle whether this is a change.",
        "resolution_condition": "A later block must establish whether the value changed in story time.",
    }
    ok = validate_stable_claim_hearing_response_v1(
        base,
        component=component,
        supplied_block_ids=supplied,
    )
    assert ok["resolution_condition"].startswith("A later block")
    with pytest.raises(B1CrossChapterAuditBridgeError, match="resolution_condition"):
        validate_stable_claim_hearing_response_v1(
            {**base, "resolution_condition": None},
            component=component,
            supplied_block_ids=supplied,
        )


# ---------------------------------------------------------------------------
# determinism and input immutability
# ---------------------------------------------------------------------------


def test_dry_run_is_deterministic_and_does_not_mutate_inputs() -> None:
    queue = _seal_queue([_identity_component(), _stable_component()])
    queue_before = deepcopy(queue)
    blocks_before = deepcopy(SOURCE_BLOCKS)
    first = _dry_run(queue)
    second = _dry_run(queue)
    assert first == second
    assert first["report_hash"] == second["report_hash"]
    assert queue == queue_before
    assert SOURCE_BLOCKS == blocks_before


def test_partition_exact_cover_counts() -> None:
    queue = _seal_queue(
        [
            _identity_component(),
            _stable_component(),
            _referral_component("entity_link", review_route="temporal_auditor"),
        ]
    )
    partition = partition_hearing_queue_v1(queue)
    assert len(partition["covered_component_ids"]) == 3
    assert len(partition["ready_identity"]) == 1
    assert len(partition["ready_stable_claim"]) == 1
    assert partition["unconsumed_ready"]["temporal_auditor"]
