from __future__ import annotations

import pytest

from pipeline.translate.d2l_translation_quality_state_v1 import (
    AUDIT_INVALID_EXHAUSTED,
    AUDIT_NOT_RUN,
    AUDIT_VALID,
    HOLD,
    HOLD_AUDIT_CONTRACT,
    HOLD_TRANSLATION_CONTRACT,
    PASS_AFTER_RETRY,
    PASS_FIRST,
    REPAIR_MECHANICAL,
    REPAIR_NONE,
    REPAIR_SEMANTIC,
    RETRY_TARGETED,
    reduce_quality_decision,
)


def _major(issue_type: str = "meaning_omission") -> dict:
    return {"issue_type": issue_type, "severity": "major"}


def _advisory() -> dict:
    return {"issue_type": "style_or_fluency_advisory", "severity": "advisory"}


def test_clean_and_advisory_only_initial_candidates_pass_first() -> None:
    clean = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=False,
        audit_contract_status=AUDIT_VALID,
    )
    advisory = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=False,
        audit_contract_status=AUDIT_VALID,
        auditor_findings=[_advisory()],
    )

    assert clean.state == advisory.state == PASS_FIRST
    assert clean.publication_ready and advisory.publication_ready
    assert not clean.translation_retry_requested


def test_major_semantic_or_mechanical_initial_finding_requests_one_retry() -> None:
    semantic = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=False,
        audit_contract_status=AUDIT_VALID,
        auditor_findings=[_major()],
    )
    mechanical = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=False,
        audit_contract_status=AUDIT_VALID,
        deterministic_major_findings=[_major("unexpected_output_script")],
    )

    assert semantic.state == mechanical.state == RETRY_TARGETED
    assert semantic.translation_retry_requested
    assert mechanical.translation_retry_requested
    assert not semantic.publication_ready


def test_clean_repair_passes_after_retry_for_both_origins() -> None:
    for origin in (REPAIR_MECHANICAL, REPAIR_SEMANTIC):
        decision = reduce_quality_decision(
            candidate_auditable=True,
            translation_retry_used=True,
            repair_origin=origin,
            audit_contract_status=AUDIT_VALID,
        )
        assert decision.state == PASS_AFTER_RETRY
        assert decision.publication_ready


def test_semantic_failure_after_mechanical_repair_has_distinct_hold_branch() -> None:
    decision = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=True,
        repair_origin=REPAIR_MECHANICAL,
        audit_contract_status=AUDIT_VALID,
        auditor_findings=[_major()],
    )

    assert decision.state == HOLD
    assert decision.hold_branch == "mechanical_repair_then_semantic_hold"
    assert not decision.publication_ready


def test_semantic_failure_after_semantic_retry_has_distinct_hold_branch() -> None:
    decision = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=True,
        repair_origin=REPAIR_SEMANTIC,
        audit_contract_status=AUDIT_VALID,
        auditor_findings=[_major()],
    )

    assert decision.state == HOLD
    assert decision.hold_branch == "semantic_retry_then_hold"


def test_unresolved_mechanical_failure_after_retry_holds_translation_contract() -> None:
    decision = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=True,
        repair_origin=REPAIR_MECHANICAL,
        audit_contract_status=AUDIT_VALID,
        deterministic_major_findings=[_major("unexpected_output_script")],
    )

    assert decision.state == HOLD_TRANSLATION_CONTRACT
    assert decision.hold_branch == "hold_translation_contract"


def test_unauditable_candidate_retries_once_then_holds() -> None:
    retry = reduce_quality_decision(
        candidate_auditable=False,
        translation_retry_used=False,
        repair_origin=REPAIR_NONE,
        audit_contract_status=AUDIT_NOT_RUN,
    )
    hold = reduce_quality_decision(
        candidate_auditable=False,
        translation_retry_used=True,
        repair_origin=REPAIR_MECHANICAL,
        audit_contract_status=AUDIT_NOT_RUN,
    )

    assert retry.state == RETRY_TARGETED
    assert hold.state == HOLD_TRANSLATION_CONTRACT
    assert hold.hold_branch == "hold_translation_contract"


def test_exhausted_invalid_audit_holds_without_translation_retry() -> None:
    decision = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=False,
        audit_contract_status=AUDIT_INVALID_EXHAUSTED,
    )

    assert decision.state == HOLD_AUDIT_CONTRACT
    assert decision.hold_branch == "hold_audit_contract"
    assert not decision.translation_retry_requested


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "candidate_auditable": True,
            "translation_retry_used": True,
            "repair_origin": REPAIR_NONE,
            "audit_contract_status": AUDIT_VALID,
        },
        {
            "candidate_auditable": True,
            "translation_retry_used": False,
            "repair_origin": REPAIR_SEMANTIC,
            "audit_contract_status": AUDIT_VALID,
        },
        {
            "candidate_auditable": True,
            "translation_retry_used": False,
            "audit_contract_status": AUDIT_NOT_RUN,
        },
        {
            "candidate_auditable": False,
            "translation_retry_used": False,
            "audit_contract_status": AUDIT_VALID,
        },
    ],
)
def test_impossible_state_combinations_fail_closed(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        reduce_quality_decision(**kwargs)


def test_decision_serialization_is_closed_and_stable() -> None:
    decision = reduce_quality_decision(
        candidate_auditable=True,
        translation_retry_used=False,
        audit_contract_status=AUDIT_VALID,
        auditor_findings=[_major("numeric_or_comparison_error")],
    )

    assert decision.to_dict() == {
        "policy_id": "d2l_translation_publication_eligibility_v1",
        "state": RETRY_TARGETED,
        "publication_ready": False,
        "translation_retry_requested": True,
        "hold_branch": None,
        "reason_codes": ["numeric_or_comparison_error"],
    }
