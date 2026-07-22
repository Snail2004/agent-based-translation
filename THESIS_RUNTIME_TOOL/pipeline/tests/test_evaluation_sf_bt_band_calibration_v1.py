from __future__ import annotations

import copy
import json

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.scorer_prompts_v3 import (
    SF_BT_SEMANTIC_CANDIDATE_ID,
    SF_BT_SEMANTIC_PROMPT_SHA256,
)
from pipeline.eval.sf_bt_band_calibration_v1 import (
    SF_BT_BAND_CALIBRATION_SCORES,
    analyze_sf_bt_band_calibration,
    load_default_sf_bt_band_calibration_fixture,
    project_sf_bt_band_calibration_case,
    sf_bt_band_calibration_fixture_sha256,
    validate_sf_bt_band_calibration_fixture,
)


def test_default_fixture_is_balanced_and_bound_to_current_judge() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()

    assert fixture["review_status"] == "approved_independent_semantic_review"
    assert fixture["runtime_admission"] == "forbidden"
    assert fixture["judge_contract"] == {
        "candidate_id": SF_BT_SEMANTIC_CANDIDATE_ID,
        "prompt_sha256": SF_BT_SEMANTIC_PROMPT_SHA256,
        "allowed_scores": list(SF_BT_BAND_CALIBRATION_SCORES),
    }
    assert len(fixture["cases"]) == 15
    assert {
        score: sum(row["expected_score"] == score for row in fixture["cases"])
        for score in SF_BT_BAND_CALIBRATION_SCORES
    } == {score: 3 for score in SF_BT_BAND_CALIBRATION_SCORES}


def test_each_ladder_uses_one_reference_and_all_five_bands() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    ladder_ids = list(dict.fromkeys(row["ladder_id"] for row in fixture["cases"]))

    assert len(ladder_ids) == 3
    for ladder_id in ladder_ids:
        ladder = [row for row in fixture["cases"] if row["ladder_id"] == ladder_id]
        assert [row["expected_score"] for row in ladder] == [100, 75, 50, 25, 0]
        assert len({row["reference_passage"] for row in ladder}) == 1
        assert len({row["candidate_passage"] for row in ladder}) == 5


def test_projection_exposes_only_passages_and_balances_orientation() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    projections = [project_sf_bt_band_calibration_case(row) for row in fixture["cases"]]

    assert all(
        set(row) == {"presentation_id", "passage_a", "passage_b"}
        for row in projections
    )
    first_count = sum(
        row["presentation_id"] == "calibration_reference_first"
        for row in projections
    )
    assert 5 <= first_count <= 10
    rendered_projection = json.dumps(projections, sort_keys=True)
    assert "expected_score" not in rendered_projection
    assert "expected_primary_reason" not in rendered_projection
    assert "author_note" not in rendered_projection
    for case in fixture["cases"]:
        assert case["author_note"] not in rendered_projection
        assert case["expected_primary_reason"] not in rendered_projection


def test_fixture_hash_is_deterministic_and_validation_does_not_mutate() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    original = copy.deepcopy(fixture)

    first = sf_bt_band_calibration_fixture_sha256(fixture)
    second = sf_bt_band_calibration_fixture_sha256(copy.deepcopy(fixture))

    assert first == second
    assert len(first) == 64
    assert fixture == original


def test_perfect_predictions_measure_perfect_band_and_order_accuracy() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    analysis = analyze_sf_bt_band_calibration(
        fixture,
        _responses(fixture, lambda row: row["expected_score"]),
    )

    assert analysis["interpretation"] == "measurement_only_not_a_calibration_pass"
    assert analysis["summary"] == {
        "case_count": 15,
        "exact_band_count": 15,
        "exact_band_accuracy": 1.0,
        "within_one_band_count": 15,
        "within_one_band_accuracy": 1.0,
        "mean_absolute_point_error": 0.0,
        "mean_band_distance": 0.0,
        "ordered_pair_count": 30,
        "strict_order_count": 30,
        "strict_monotonic_pair_rate": 1.0,
        "noninversion_count": 30,
        "noninversion_pair_rate": 1.0,
        "tie_pair_count": 0,
        "inversion_count": 0,
        "severe_inversion_count": 0,
    }
    assert analysis["predicted_distribution"] == [
        {"score": score, "count": 3} for score in SF_BT_BAND_CALIBRATION_SCORES
    ]


def test_single_band_collapse_is_visible_without_fabricated_pass_fail() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    analysis = analyze_sf_bt_band_calibration(
        fixture,
        _responses(fixture, lambda _row: 50),
    )

    assert analysis["summary"]["exact_band_accuracy"] == 0.2
    assert analysis["summary"]["within_one_band_accuracy"] == 0.6
    assert analysis["summary"]["strict_monotonic_pair_rate"] == 0.0
    assert analysis["summary"]["noninversion_pair_rate"] == 1.0
    assert analysis["summary"]["tie_pair_count"] == 30
    assert analysis["predicted_distribution"] == [
        {"score": 0, "count": 0},
        {"score": 25, "count": 0},
        {"score": 50, "count": 15},
        {"score": 75, "count": 0},
        {"score": 100, "count": 0},
    ]


def test_reversed_predictions_surface_all_order_inversions() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    analysis = analyze_sf_bt_band_calibration(
        fixture,
        _responses(fixture, lambda row: 100 - row["expected_score"]),
    )

    assert analysis["summary"]["strict_monotonic_pair_rate"] == 0.0
    assert analysis["summary"]["noninversion_pair_rate"] == 0.0
    assert analysis["summary"]["inversion_count"] == 30
    assert analysis["summary"]["severe_inversion_count"] == 18


def test_analysis_requires_exact_case_coverage_and_canonical_response_bands() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    responses = _responses(fixture, lambda row: row["expected_score"])
    responses.pop(fixture["cases"][0]["case_id"])
    with pytest.raises(ContractValidationError, match="response_exact_cover"):
        analyze_sf_bt_band_calibration(fixture, responses)

    responses = _responses(fixture, lambda row: row["expected_score"])
    responses["foreign_case"] = '{"score":100,"flags":[],"note":"foreign"}'
    with pytest.raises(ContractValidationError, match="response_exact_cover"):
        analyze_sf_bt_band_calibration(fixture, responses)

    responses = _responses(fixture, lambda row: row["expected_score"])
    responses[fixture["cases"][0]["case_id"]] = (
        '{"score":58,"flags":[],"note":"unsupported precision"}'
    )
    with pytest.raises(ContractValidationError, match="score_band"):
        analyze_sf_bt_band_calibration(fixture, responses)


def test_fixture_rejects_unknown_keys_bad_balance_and_reason_drift() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()

    tampered = copy.deepcopy(fixture)
    tampered["gold_hint"] = "forbidden extra authority"
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_sf_bt_band_calibration_fixture(tampered)

    tampered = copy.deepcopy(fixture)
    tampered["cases"][0]["expected_score"] = 75
    tampered["cases"][0]["expected_primary_reason"] = "minor_specificity_drift"
    with pytest.raises(ContractValidationError, match="ladder_scores"):
        validate_sf_bt_band_calibration_fixture(tampered)

    tampered = copy.deepcopy(fixture)
    tampered["cases"][0]["expected_primary_reason"] = "minor_specificity_drift"
    with pytest.raises(ContractValidationError, match="reason_band_binding"):
        validate_sf_bt_band_calibration_fixture(tampered)


def test_analysis_is_deterministic_and_does_not_mutate_inputs() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    responses = _responses(fixture, lambda row: row["expected_score"])
    fixture_before = copy.deepcopy(fixture)
    responses_before = copy.deepcopy(responses)

    first = analyze_sf_bt_band_calibration(fixture, responses)
    second = analyze_sf_bt_band_calibration(copy.deepcopy(fixture), copy.deepcopy(responses))

    assert first == second
    assert fixture == fixture_before
    assert responses == responses_before


def _responses(fixture: dict, score_for_case) -> dict[str, str]:
    return {
        row["case_id"]: json.dumps(
            {
                "score": score_for_case(row),
                "flags": [],
                "note": "synthetic offline observation",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        for row in fixture["cases"]
    }
