from __future__ import annotations

import copy

import pytest

from pipeline.translate.d2l_translation_quality_observation_v1 import (
    D2LQualityObservationError,
    build_quality_observation,
    validate_quality_observation,
)


def _finding(block_id: str = "b002") -> dict[str, str]:
    return {
        "block_id": block_id,
        "issue_type": "style_or_fluency_advisory",
        "severity": "advisory",
        "source_evidence": "the source phrase",
        "target_evidence": "cum dich",
        "reason": "The wording is understandable but awkward.",
    }


def test_quality_findings_are_visible_but_nonblocking() -> None:
    report = build_quality_observation(
        audited_block_ids=["b001", "b002"],
        findings=[_finding()],
        source_translation_artifact_refs=["translation_s0", "translation_s1"],
    )

    assert validate_quality_observation(report) == report
    assert report["counts"] == {"pass": 1, "issue": 1, "findings": 1, "total": 2}
    assert report["blocks"][0]["quality_status"] == "pass"
    assert report["blocks"][1]["quality_status"] == "issue"
    assert all(row["continue_to_scoring"] for row in report["blocks"])


def test_foreign_finding_is_rejected() -> None:
    with pytest.raises(D2LQualityObservationError, match="foreign block"):
        build_quality_observation(
            audited_block_ids=["b001"],
            findings=[_finding("foreign")],
            source_translation_artifact_refs=["translation_s1"],
        )


def test_one_sided_evidence_matches_auditor_issue_semantics() -> None:
    unsupported = _finding()
    unsupported.update(
        issue_type="unsupported_addition",
        source_evidence="",
        target_evidence="phan tu them",
    )
    omission = _finding()
    omission.update(
        issue_type="meaning_omission",
        source_evidence="source phrase",
        target_evidence="",
    )

    report = build_quality_observation(
        audited_block_ids=["b002"],
        findings=[unsupported, omission],
        source_translation_artifact_refs=["translation_s1"],
    )

    assert report["counts"] == {"pass": 0, "issue": 1, "findings": 2, "total": 1}


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda row: row["blocks"][1].update(quality_status="pass"), "status"),
        (lambda row: row["blocks"][1].update(continue_to_scoring=False), "nonblocking"),
        (lambda row: row["counts"].update(issue=0), "counts mismatch"),
        (lambda row: row.update(report_sha256="0" * 64), "hash drift"),
    ],
)
def test_tampered_quality_observation_fails_closed(mutation, error: str) -> None:
    report = build_quality_observation(
        audited_block_ids=["b001", "b002"],
        findings=[_finding()],
        source_translation_artifact_refs=["translation_s1"],
    )
    tampered = copy.deepcopy(report)
    mutation(tampered)

    with pytest.raises(D2LQualityObservationError, match=error):
        validate_quality_observation(tampered)


def test_mechanical_failure_and_raw_model_payload_cannot_be_relayed() -> None:
    report = build_quality_observation(
        audited_block_ids=["b001"],
        findings=[],
        source_translation_artifact_refs=["translation_s1"],
    )
    mechanically_invalid = copy.deepcopy(report)
    mechanically_invalid["mechanical_validation_status"] = "failed"
    with pytest.raises(D2LQualityObservationError, match="mechanically invalid"):
        validate_quality_observation(mechanically_invalid)

    raw_payload = copy.deepcopy(report)
    raw_payload["raw_response"] = "model bytes"
    with pytest.raises(D2LQualityObservationError, match="forbidden key"):
        validate_quality_observation(raw_payload)
