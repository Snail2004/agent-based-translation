from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_string,
    require_unique,
)
from pipeline.eval.scorer_prompts_v3 import (
    SF_BT_SEMANTIC_CANDIDATE_ID,
    SF_BT_SEMANTIC_PROMPT_SHA256,
    parse_sf_bt_semantic_response_v3,
)


__all__ = [
    "DEFAULT_SF_BT_BAND_CALIBRATION_FIXTURE_PATH",
    "SF_BT_BAND_CALIBRATION_SCORES",
    "analyze_sf_bt_band_calibration",
    "load_default_sf_bt_band_calibration_fixture",
    "project_sf_bt_band_calibration_case",
    "sf_bt_band_calibration_fixture_sha256",
    "validate_sf_bt_band_calibration_fixture",
]


DEFAULT_SF_BT_BAND_CALIBRATION_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "eval"
    / "sf_bt_band_calibration_v1.json"
)

_SCHEMA_ID = "SFBTBandCalibrationFixtureV1"
_SCHEMA_VERSION = "1.0.0"
SF_BT_BAND_CALIBRATION_SCORES = (0, 25, 50, 75, 100)
_SCORE_SET = frozenset(SF_BT_BAND_CALIBRATION_SCORES)
_EXPECTED_REASON_BY_SCORE = {
    0: "contradiction_or_unrelated",
    25: "substantial_claim_loss_or_divergence",
    50: "noticeable_fact_or_relation_drift",
    75: "minor_specificity_drift",
    100: "semantic_equivalence",
}
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("judge_contract", "allowed_scores"),
            ("cases",),
        }
    ),
)
_ANALYSIS_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("judge_contract", "allowed_scores"),
            ("case_results",),
            ("per_expected_band",),
            ("confusion_matrix",),
            ("predicted_distribution",),
            ("case_results", "*", "flags"),
        }
    ),
)


def load_default_sf_bt_band_calibration_fixture() -> dict[str, Any]:
    try:
        raw = json.loads(
            DEFAULT_SF_BT_BAND_CALIBRATION_FIXTURE_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "fixture_file",
            str(DEFAULT_SF_BT_BAND_CALIBRATION_FIXTURE_PATH),
            "default SF-BT band calibration fixture is unreadable or malformed",
        ) from exc
    return validate_sf_bt_band_calibration_fixture(raw)


def validate_sf_bt_band_calibration_fixture(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "fixture_set_id",
            "review_status",
            "book_neutral",
            "runtime_admission",
            "judge_contract",
            "cases",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(root["schema_id"], {_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "fixture_set_id": require_string(
            root["fixture_set_id"], path="$.fixture_set_id"
        ),
        "review_status": require_enum(
            root["review_status"],
            {
                "draft_requires_independent_semantic_review",
                "approved_independent_semantic_review",
            },
            path="$.review_status",
        ),
        "book_neutral": _require_true(root["book_neutral"], path="$.book_neutral"),
        "runtime_admission": require_enum(
            root["runtime_admission"], {"forbidden"}, path="$.runtime_admission"
        ),
        "judge_contract": _validate_judge_contract(root["judge_contract"]),
        "cases": _validate_cases(root["cases"]),
    }
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical band-calibration fixture must remain an object")
    return canonical


def sf_bt_band_calibration_fixture_sha256(payload: Mapping[str, Any]) -> str:
    validated = validate_sf_bt_band_calibration_fixture(payload)
    return canonical_sha256(validated, policy=_POLICY)


def project_sf_bt_band_calibration_case(
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a fixture row to two unlabeled passages without oracle metadata."""

    row = _validate_case(case, path="$.case")
    reference_first = int(hashlib.sha256(row["case_id"].encode("utf-8")).hexdigest(), 16) % 2 == 0
    passage_a = row["reference_passage"] if reference_first else row["candidate_passage"]
    passage_b = row["candidate_passage"] if reference_first else row["reference_passage"]
    return {
        "presentation_id": (
            "calibration_reference_first"
            if reference_first
            else "calibration_candidate_first"
        ),
        "passage_a": passage_a,
        "passage_b": passage_b,
    }


def analyze_sf_bt_band_calibration(
    fixture: Mapping[str, Any],
    raw_responses_by_case_id: Mapping[str, str],
) -> dict[str, Any]:
    """Measure band behavior without declaring a pass/fail calibration threshold."""

    validated = validate_sf_bt_band_calibration_fixture(fixture)
    responses = require_mapping(raw_responses_by_case_id, path="$.responses")
    expected_ids = [row["case_id"] for row in validated["cases"]]
    actual_ids = list(responses.keys())
    if any(not isinstance(case_id, str) for case_id in actual_ids):
        raise ContractValidationError(
            "response_case_id", "$.responses", "response case IDs must be strings"
        )
    missing = sorted(set(expected_ids) - set(actual_ids))
    unexpected = sorted(set(actual_ids) - set(expected_ids))
    if missing or unexpected or len(actual_ids) != len(expected_ids):
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise ContractValidationError(
            "response_exact_cover",
            "$.responses",
            "responses must exactly cover fixture cases (" + "; ".join(details) + ")",
        )

    parsed_by_case: dict[str, dict[str, Any]] = {}
    case_results: list[dict[str, Any]] = []
    for case in validated["cases"]:
        case_id = case["case_id"]
        raw_response = require_string(
            responses[case_id], path=f"$.responses.{case_id}"
        )
        parsed = parse_sf_bt_semantic_response_v3(raw_response)
        parsed_by_case[case_id] = parsed
        expected_score = case["expected_score"]
        predicted_score = parsed["score"]
        absolute_error = abs(predicted_score - expected_score)
        case_results.append(
            {
                "case_id": case_id,
                "ladder_id": case["ladder_id"],
                "expected_score": expected_score,
                "predicted_score": predicted_score,
                "absolute_point_error": absolute_error,
                "band_distance": absolute_error // 25,
                "exact_band": predicted_score == expected_score,
                "within_one_band": absolute_error <= 25,
                "flags": parsed["flags"],
                "note": parsed["note"],
            }
        )

    summary = _summarize(case_results)
    response_rows = [
        {
            "case_id": case_id,
            "response": parsed_by_case[case_id],
        }
        for case_id in expected_ids
    ]
    analysis = {
        "schema_id": "SFBTBandCalibrationAnalysisV1",
        "schema_version": "1.0.0",
        "fixture_set_id": validated["fixture_set_id"],
        "fixture_sha256": sf_bt_band_calibration_fixture_sha256(validated),
        "judge_contract": validated["judge_contract"],
        "response_set_sha256": canonical_sha256(
            {"responses": response_rows},
            policy=CanonicalPolicy(
                set_like_paths=frozenset(),
                semantic_sequence_paths=frozenset(
                    {
                        ("responses",),
                        ("responses", "*", "response", "flags"),
                    }
                ),
            ),
        ),
        "interpretation": "measurement_only_not_a_calibration_pass",
        "summary": summary,
        "per_expected_band": _per_expected_band(case_results),
        "confusion_matrix": _confusion_matrix(case_results),
        "predicted_distribution": _predicted_distribution(case_results),
        "case_results": case_results,
    }
    canonical = canonicalize(analysis, policy=_ANALYSIS_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical band-calibration analysis must remain an object")
    return canonical


def _validate_judge_contract(value: Any) -> dict[str, Any]:
    path = "$.judge_contract"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"candidate_id", "prompt_sha256", "allowed_scores"},
        path=path,
    )
    scores = require_list(row["allowed_scores"], path=f"{path}.allowed_scores")
    normalized_scores = [
        require_int(score, path=f"{path}.allowed_scores[{index}]", minimum=0)
        for index, score in enumerate(scores)
    ]
    if normalized_scores != list(SF_BT_BAND_CALIBRATION_SCORES):
        raise ContractValidationError(
            "score_bands",
            f"{path}.allowed_scores",
            "allowed scores must be exactly 0, 25, 50, 75, 100",
        )
    return {
        "candidate_id": require_enum(
            row["candidate_id"],
            {SF_BT_SEMANTIC_CANDIDATE_ID},
            path=f"{path}.candidate_id",
        ),
        "prompt_sha256": require_enum(
            row["prompt_sha256"],
            {SF_BT_SEMANTIC_PROMPT_SHA256},
            path=f"{path}.prompt_sha256",
        ),
        "allowed_scores": normalized_scores,
    }


def _validate_cases(value: Any) -> list[dict[str, Any]]:
    path = "$.cases"
    rows = require_list(value, path=path)
    result = [_validate_case(row, path=f"{path}[{index}]") for index, row in enumerate(rows)]
    require_unique([row["case_id"] for row in result], path=f"{path}.case_id")
    if len(result) != 15:
        raise ContractValidationError(
            "case_count", path, "fixture must contain exactly 15 cases"
        )
    ladder_ids = list(dict.fromkeys(row["ladder_id"] for row in result))
    if len(ladder_ids) != 3:
        raise ContractValidationError(
            "ladder_count", path, "fixture must contain exactly three ladders"
        )
    for ladder_id in ladder_ids:
        ladder = [row for row in result if row["ladder_id"] == ladder_id]
        if [row["expected_score"] for row in ladder] != [100, 75, 50, 25, 0]:
            raise ContractValidationError(
                "ladder_scores",
                path,
                f"ladder {ladder_id} must be ordered 100, 75, 50, 25, 0",
            )
        references = {row["reference_passage"] for row in ladder}
        if len(references) != 1:
            raise ContractValidationError(
                "ladder_reference",
                path,
                f"ladder {ladder_id} must use one stable reference passage",
            )
        candidates = [row["candidate_passage"] for row in ladder]
        require_unique(candidates, path=f"{path}.{ladder_id}.candidate_passage")
    counts = Counter(row["expected_score"] for row in result)
    if any(counts[score] != 3 for score in SF_BT_BAND_CALIBRATION_SCORES):
        raise ContractValidationError(
            "band_balance", path, "fixture must contain exactly three cases per score band"
        )
    return result


def _validate_case(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "case_id",
            "ladder_id",
            "expected_score",
            "reference_passage",
            "candidate_passage",
            "expected_primary_reason",
            "author_note",
        },
        path=path,
    )
    expected_score = require_int(
        row["expected_score"], path=f"{path}.expected_score", minimum=0
    )
    if expected_score not in _SCORE_SET:
        raise ContractValidationError(
            "expected_score",
            f"{path}.expected_score",
            "expected score must be one of 0, 25, 50, 75, or 100",
        )
    expected_reason = require_enum(
        row["expected_primary_reason"],
        frozenset(_EXPECTED_REASON_BY_SCORE.values()),
        path=f"{path}.expected_primary_reason",
    )
    if expected_reason != _EXPECTED_REASON_BY_SCORE[expected_score]:
        raise ContractValidationError(
            "reason_band_binding",
            f"{path}.expected_primary_reason",
            "expected reason does not match the declared score band",
        )
    return {
        "case_id": require_string(row["case_id"], path=f"{path}.case_id"),
        "ladder_id": require_string(row["ladder_id"], path=f"{path}.ladder_id"),
        "expected_score": expected_score,
        "reference_passage": require_string(
            row["reference_passage"], path=f"{path}.reference_passage", maximum=600
        ),
        "candidate_passage": require_string(
            row["candidate_passage"], path=f"{path}.candidate_passage", maximum=600
        ),
        "expected_primary_reason": expected_reason,
        "author_note": require_string(
            row["author_note"], path=f"{path}.author_note", maximum=320
        ),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n_cases = len(rows)
    exact_count = sum(row["exact_band"] for row in rows)
    within_one_count = sum(row["within_one_band"] for row in rows)
    pairs = _ordered_ladder_pairs(rows)
    strict_count = sum(
        high["predicted_score"] > low["predicted_score"] for high, low in pairs
    )
    noninversion_count = sum(
        high["predicted_score"] >= low["predicted_score"] for high, low in pairs
    )
    tie_count = sum(
        high["predicted_score"] == low["predicted_score"] for high, low in pairs
    )
    inversion_count = len(pairs) - noninversion_count
    severe_inversion_count = sum(
        high["expected_score"] - low["expected_score"] >= 50
        and high["predicted_score"] < low["predicted_score"]
        for high, low in pairs
    )
    return {
        "case_count": n_cases,
        "exact_band_count": exact_count,
        "exact_band_accuracy": exact_count / n_cases,
        "within_one_band_count": within_one_count,
        "within_one_band_accuracy": within_one_count / n_cases,
        "mean_absolute_point_error": sum(row["absolute_point_error"] for row in rows)
        / n_cases,
        "mean_band_distance": sum(row["band_distance"] for row in rows) / n_cases,
        "ordered_pair_count": len(pairs),
        "strict_order_count": strict_count,
        "strict_monotonic_pair_rate": strict_count / len(pairs),
        "noninversion_count": noninversion_count,
        "noninversion_pair_rate": noninversion_count / len(pairs),
        "tie_pair_count": tie_count,
        "inversion_count": inversion_count,
        "severe_inversion_count": severe_inversion_count,
    }


def _ordered_ladder_pairs(
    rows: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    ladder_ids = list(dict.fromkeys(row["ladder_id"] for row in rows))
    for ladder_id in ladder_ids:
        ladder = [row for row in rows if row["ladder_id"] == ladder_id]
        for high_index, high in enumerate(ladder):
            for low in ladder[high_index + 1 :]:
                pairs.append((high, low))
    return pairs


def _per_expected_band(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for score in SF_BT_BAND_CALIBRATION_SCORES:
        band_rows = [row for row in rows if row["expected_score"] == score]
        result.append(
            {
                "expected_score": score,
                "case_count": len(band_rows),
                "exact_band_count": sum(row["exact_band"] for row in band_rows),
                "within_one_band_count": sum(
                    row["within_one_band"] for row in band_rows
                ),
                "mean_absolute_point_error": sum(
                    row["absolute_point_error"] for row in band_rows
                )
                / len(band_rows),
            }
        )
    return result


def _confusion_matrix(rows: list[dict[str, Any]]) -> list[dict[str, int]]:
    return [
        {
            "expected_score": expected,
            "predicted_score": predicted,
            "count": sum(
                row["expected_score"] == expected
                and row["predicted_score"] == predicted
                for row in rows
            ),
        }
        for expected in SF_BT_BAND_CALIBRATION_SCORES
        for predicted in SF_BT_BAND_CALIBRATION_SCORES
    ]


def _predicted_distribution(rows: list[dict[str, Any]]) -> list[dict[str, int]]:
    counts = Counter(row["predicted_score"] for row in rows)
    return [
        {"score": score, "count": counts[score]}
        for score in SF_BT_BAND_CALIBRATION_SCORES
    ]


def _require_true(value: Any, *, path: str) -> bool:
    if value is not True:
        raise ContractValidationError("value", path, "expected true")
    return True


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")
