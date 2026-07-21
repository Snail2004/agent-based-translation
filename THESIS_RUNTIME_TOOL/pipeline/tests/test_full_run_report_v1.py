from __future__ import annotations

import copy
import importlib
import json
import math
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError, canonical_sha256
from pipeline.eval.full_run_report_v1 import (
    FULL_RUN_CANONICAL_POLICY,
    seal_full_run_report,
    validate_full_run_report,
)


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_v1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _reseal(payload: dict) -> dict:
    payload["integrity"]["artifact_set_sha256"] = canonical_sha256(
        {"artifacts": payload["artifacts"]}, policy=FULL_RUN_CANONICAL_POLICY
    )
    return seal_full_run_report(payload)


@pytest.mark.parametrize("name", ["full_run_one_arm.json", "full_run_s0_s1.json"])
def test_versioned_fixtures_validate_without_mutating_input(name):
    payload = _load(name)
    before = copy.deepcopy(payload)
    normalized = validate_full_run_report(payload)
    assert payload == before
    assert normalized["schema_id"] == "FullRunReportV1"


def test_public_validator_entry_point_is_directly_reusable_by_transport_code():
    module = importlib.import_module("pipeline.eval.full_run_report_v1")
    validator = getattr(module, "validate_full_run_report")
    payload = _load("full_run_one_arm.json")
    before = copy.deepcopy(payload)

    canonical = validator(payload)

    assert canonical["schema_id"] == "FullRunReportV1"
    assert payload == before


def test_one_arm_has_no_fabricated_comparison_or_claim():
    normalized = validate_full_run_report(_load("full_run_one_arm.json"))
    assert len(normalized["arms"]) == 1
    assert normalized["metrics"][0]["comparison"] == {
        "status": "not_applicable",
        "baseline_arm_id": None,
        "candidate_arm_id": None,
        "delta": None,
        "wins": None,
        "ties": None,
        "losses": None,
    }
    assert normalized["claim"]["verdict"] == "NOT_APPLICABLE"


def test_one_arm_rejects_available_comparison_even_with_existing_arm_ids():
    payload = _load("full_run_one_arm.json")
    comparison = payload["metrics"][0]["comparison"]
    comparison.update(
        {
            "status": "available",
            "baseline_arm_id": "final",
            "candidate_arm_id": "final",
            "delta": 0,
        }
    )
    with pytest.raises(ContractValidationError):
        validate_full_run_report(_reseal(payload))


def test_s0_s1_preserves_explicit_roles_values_comparison_and_verdict():
    normalized = validate_full_run_report(_load("full_run_s0_s1.json"))
    roles = {row["arm_id"]: row["role"] for row in normalized["arms"]}
    assert roles == {"s0": "baseline", "s1": "candidate"}
    comparison = normalized["metrics"][0]["comparison"]
    assert comparison["baseline_arm_id"] == "s0"
    assert comparison["candidate_arm_id"] == "s1"
    assert comparison["delta"] == 12.0
    assert (comparison["wins"], comparison["ties"], comparison["losses"]) == (8, 1, 3)
    assert normalized["claim"]["verdict"] == "BETTER"


def test_arm_artifacts_and_comparison_roles_are_authoritative():
    duplicate_artifact = _load("full_run_s0_s1.json")
    duplicate_artifact["arms"][1]["translation_artifact_id"] = duplicate_artifact[
        "arms"
    ][0]["translation_artifact_id"]
    duplicate_artifact["arms"][1]["translation_sha256"] = duplicate_artifact[
        "arms"
    ][0]["translation_sha256"]
    with pytest.raises(ContractValidationError, match="duplicate"):
        validate_full_run_report(_reseal(duplicate_artifact))

    wrong_role = _load("full_run_s0_s1.json")
    wrong_role["metrics"][0]["comparison"]["baseline_arm_id"] = "s1"
    wrong_role["metrics"][0]["comparison"]["candidate_arm_id"] = "s0"
    with pytest.raises(ContractValidationError, match="comparison_role"):
        validate_full_run_report(_reseal(wrong_role))

    insufficient_unknown = _load("full_run_s0_s1.json")
    comparison = insufficient_unknown["metrics"][0]["comparison"]
    comparison.update(
        {
            "status": "insufficient",
            "baseline_arm_id": "missing-arm",
            "candidate_arm_id": "s1",
            "delta": None,
            "wins": None,
            "ties": None,
            "losses": None,
        }
    )
    with pytest.raises(ContractValidationError, match="arm_reference"):
        validate_full_run_report(_reseal(insufficient_unknown))


def test_unavailable_usage_preserves_nulls_and_partial_usage_is_not_recomputed():
    one_arm = validate_full_run_report(_load("full_run_one_arm.json"))
    assert all(
        value is None
        for field, value in one_arm["usage"]["totals"].items()
        if field != "currency"
    )
    assert one_arm["usage"]["totals"]["currency"] is None

    payload = _load("full_run_s0_s1.json")
    payload["usage"]["totals"]["total_tokens"] = 999
    normalized = validate_full_run_report(_reseal(payload))
    assert normalized["usage"]["totals"]["total_tokens"] == 999
    assert normalized["usage"]["by_stage"][0]["total_tokens"] == 1200


def test_unknown_usage_cannot_be_encoded_as_known_zero():
    payload = _load("full_run_one_arm.json")
    payload["usage"]["totals"]["total_tokens"] = 0
    with pytest.raises(ContractValidationError, match="usage_unknown"):
        validate_full_run_report(_reseal(payload))


def test_usage_status_basis_and_unknown_attempts_are_consistent():
    unavailable_basis = _load("full_run_s0_s1.json")
    unavailable_basis["usage"]["accounting_basis"] = "unavailable"
    with pytest.raises(ContractValidationError, match="usage_basis"):
        validate_full_run_report(_reseal(unavailable_basis))

    unknown_attempt = _load("full_run_s0_s1.json")
    unknown_attempt["usage"]["status"] = "available"
    unknown_attempt["usage"]["unknown_attempt_count"] = 1
    with pytest.raises(ContractValidationError, match="usage_status"):
        validate_full_run_report(_reseal(unknown_attempt))

    stage_basis = _load("full_run_s0_s1.json")
    stage_basis["usage"]["by_stage"][0]["accounting_basis"] = "unavailable"
    with pytest.raises(ContractValidationError, match="usage_basis"):
        validate_full_run_report(_reseal(stage_basis))


def test_usage_provenance_and_stage_references_are_closed():
    bad_usage_source = _load("full_run_s0_s1.json")
    bad_usage_source["usage"]["source_artifact_ids"] = []
    with pytest.raises(ContractValidationError, match="usage_provenance"):
        validate_full_run_report(_reseal(bad_usage_source))

    bad_stage = _load("full_run_s0_s1.json")
    bad_stage["usage"]["by_stage"][0]["stage_id"] = "unknown-stage"
    with pytest.raises(ContractValidationError, match="stage_reference"):
        validate_full_run_report(_reseal(bad_stage))


def test_unknown_keys_hash_tampering_and_non_finite_values_fail_closed():
    unknown = _load("full_run_s0_s1.json")
    unknown["metrics"][0]["legacy_fallback"] = 0.9
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_full_run_report(unknown)

    tampered = _load("full_run_s0_s1.json")
    tampered["metrics"][0]["comparison"]["delta"] = 99
    with pytest.raises(ContractValidationError, match="report_hash"):
        validate_full_run_report(tampered)

    non_finite = _load("full_run_s0_s1.json")
    non_finite["metrics"][0]["arm_values"][0]["value"] = math.nan
    with pytest.raises(ContractValidationError, match="non_finite"):
        validate_full_run_report(non_finite)


def test_missing_self_hash_and_artifact_set_tampering_fail_closed():
    missing_self_hash = _load("full_run_s0_s1.json")
    del missing_self_hash["integrity"]["report_sha256"]
    with pytest.raises(ContractValidationError, match="missing_keys"):
        validate_full_run_report(missing_self_hash)

    wrong_artifact_set = _load("full_run_s0_s1.json")
    wrong_artifact_set["integrity"]["artifact_set_sha256"] = "0" * 64
    wrong_artifact_set = seal_full_run_report(wrong_artifact_set)
    with pytest.raises(ContractValidationError, match="artifact_set_hash"):
        validate_full_run_report(wrong_artifact_set)


def test_unavailable_metrics_cannot_hide_interval_values_and_claim_needs_evidence():
    hidden_interval = _load("full_run_s0_s1.json")
    metric = hidden_interval["metrics"][0]
    metric["status"] = "failed"
    for arm_value in metric["arm_values"]:
        arm_value.update(
            {
                "value": None,
                "numerator": None,
                "denominator": None,
                "interval_low": 0.1,
                "interval_high": 0.9,
                "interval_level": 0.95,
            }
        )
    hidden_interval["claim"].update(
        {"status": "failed", "verdict": "INCONCLUSIVE"}
    )
    with pytest.raises(ContractValidationError, match="metric_status"):
        validate_full_run_report(_reseal(hidden_interval))

    unavailable_claim_source = _load("full_run_s0_s1.json")
    metric = unavailable_claim_source["metrics"][0]
    metric["status"] = "failed"
    for arm_value in metric["arm_values"]:
        arm_value.update(
            {
                "value": None,
                "numerator": None,
                "denominator": None,
                "interval_low": None,
                "interval_high": None,
                "interval_level": None,
            }
        )
    with pytest.raises(ContractValidationError, match="claim_evidence"):
        validate_full_run_report(_reseal(unavailable_claim_source))


def test_reference_and_path_guards_reject_unknown_or_escaping_artifacts():
    unknown_artifact = _load("full_run_s0_s1.json")
    unknown_artifact["metrics"][0]["source_artifact_ids"] = ["missing-artifact"]
    with pytest.raises(ContractValidationError, match="artifact_reference"):
        validate_full_run_report(_reseal(unknown_artifact))

    unknown_arm = _load("full_run_s0_s1.json")
    unknown_arm["metrics"][0]["arm_values"][0]["arm_id"] = "missing-arm"
    with pytest.raises(ContractValidationError, match="arm_reference"):
        validate_full_run_report(_reseal(unknown_arm))

    bad_path = _load("full_run_s0_s1.json")
    bad_path["artifacts"][0]["relative_path"] = "../other-run/report.json"
    with pytest.raises(ContractValidationError, match="unsafe_path"):
        validate_full_run_report(bad_path)


def test_optional_missing_artifact_is_explicit_but_required_missing_breaks_complete():
    one_arm = validate_full_run_report(_load("full_run_one_arm.json"))
    optional = next(row for row in one_arm["artifacts"] if row["requirement"] == "optional")
    assert optional["status"] == "missing"
    assert optional["relative_path"] is None
    assert optional["sha256"] is None

    required_missing = _load("full_run_s0_s1.json")
    artifact = next(
        row for row in required_missing["artifacts"] if row["artifact_id"] == "artifact-metric"
    )
    artifact.update({"status": "missing", "relative_path": None, "sha256": None})
    with pytest.raises(ContractValidationError, match="report_state"):
        validate_full_run_report(_reseal(required_missing))


def test_set_like_order_is_canonical_but_attempt_and_stage_order_is_semantic():
    payload = _load("full_run_s0_s1.json")
    reordered_sets = copy.deepcopy(payload)
    reordered_sets["arms"].reverse()
    reordered_sets["artifacts"].reverse()
    reordered_sets["metrics"][0]["arm_values"].reverse()
    assert seal_full_run_report(reordered_sets)["integrity"]["report_sha256"] == payload[
        "integrity"
    ]["report_sha256"]

    reordered_sequence = copy.deepcopy(payload)
    reordered_sequence["identity"]["attempt_run_ids"].reverse()
    assert seal_full_run_report(reordered_sequence)["integrity"]["report_sha256"] != payload[
        "integrity"
    ]["report_sha256"]
