from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    AGGREGATION_VERSION,
    LOCAL_HARD_FLAGS,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration import (
    ContextThresholdPolicy,
    DEVELOPMENT_POLICY_STATUS,
)


def compute_context_result(
    judge: Mapping[str, Any],
) -> tuple[int, str, list[str]]:
    if judge["judgeability"] != "JUDGEABLE" or judge["scores"] is None:
        raise ValueError("cannot score a non-judgeable context")
    if (
        judge["flags"]["translator_external_error"]
        or judge["flags"]["insufficient_context"]
    ):
        raise ValueError("cannot score translator/context error flags")
    scores = judge["scores"]
    flags = judge["flags"]
    raw_score = sum(int(value) for value in scores.values())
    hard_flags: list[str] = []
    if scores["semantic_equivalence"] <= 2:
        hard_flags.append("SEMANTIC_EQUIVALENCE_LTE_2")
    if scores["domain_sense_fit"] == 0:
        hard_flags.append("DOMAIN_SENSE_FIT_ZERO")
    if flags["semantic_contradiction"]:
        hard_flags.append("SEMANTIC_CONTRADICTION")
    if flags["wrong_sense"]:
        hard_flags.append("WRONG_SENSE")
    if flags["candidate_induced_distortion"]:
        hard_flags.append("CANDIDATE_INDUCED_DISTORTION")
    if hard_flags or raw_score <= 5:
        label = "FAIL"
    elif (
        raw_score >= 8
        and scores["semantic_equivalence"] >= 3
        and scores["domain_sense_fit"] >= 1
    ):
        label = "PASS"
    else:
        label = "MINOR"
    return raw_score, label, sorted(hard_flags)


def merge_judge_labels(
    primary: str, secondary: str
) -> tuple[str, bool]:
    if primary == secondary:
        return primary, False
    if {primary, secondary} == {"PASS", "MINOR"}:
        return "MINOR", False
    if "FAIL" in {primary, secondary}:
        return "FAIL", True
    raise AssertionError(f"unexpected judge labels: {primary}, {secondary}")


def aggregate_contextual_evidence(
    context_results: Sequence[Mapping[str, Any]],
    *,
    invalid_context_count: int,
    context_flags: Sequence[str],
    contrastive_results: Sequence[Mapping[str, Any]],
    threshold_policy: ContextThresholdPolicy,
) -> dict[str, Any]:
    raw_scores = [int(row["raw_score"]) for row in context_results]
    counts = Counter(str(row["label"]) for row in context_results)
    if raw_scores:
        normalized_score: float | None = round(
            sum(raw_scores) / (10 * len(raw_scores)), 4
        )
        minimum: int | None = min(raw_scores)
        maximum: int | None = max(raw_scores)
        score_range: int | None = maximum - minimum
    else:
        normalized_score = None
        minimum = None
        maximum = None
        score_range = None
    status = contextual_status(
        normalized_score=normalized_score,
        pass_count=counts["PASS"],
        minor_count=counts["MINOR"],
        fail_count=counts["FAIL"],
        valid_context_count=len(raw_scores),
        context_flags=context_flags,
        contrastive_results=contrastive_results,
        threshold_policy=threshold_policy,
    )
    return {
        "C": normalized_score,
        "score_interpretation": "normalized_contextual_support_not_probability",
        "raw_context_scores": raw_scores,
        "valid_context_count": len(raw_scores),
        "invalid_context_count": int(invalid_context_count),
        "pass_count": counts["PASS"],
        "minor_count": counts["MINOR"],
        "fail_count": counts["FAIL"],
        "minimum_raw_score": minimum,
        "maximum_raw_score": maximum,
        "score_range": score_range,
        "status": status,
        "aggregation_policy_version": AGGREGATION_VERSION,
        "threshold_policy_version": threshold_policy.policy_version,
        "threshold_policy_status": threshold_policy.policy_status,
    }


def contextual_status(
    *,
    normalized_score: float | None,
    pass_count: int,
    minor_count: int,
    fail_count: int,
    valid_context_count: int,
    context_flags: Sequence[str],
    contrastive_results: Sequence[Mapping[str, Any]],
    threshold_policy: ContextThresholdPolicy,
) -> str:
    flags = set(context_flags)
    contrastive = {str(row["result"]) for row in contrastive_results}
    if "SENSE_DEFINITION_INVALID" in flags or valid_context_count < 3:
        return "CONTEXT_UNJUDGEABLE"
    if "SEPARATE_SENSE_REQUIRED" in contrastive:
        return "SENSE_BOUNDARY_DETECTED"
    if (
        flags & LOCAL_HARD_FLAGS
        or fail_count >= threshold_policy.unsupported_min_fail
        or (
            normalized_score is not None
            and normalized_score < threshold_policy.unsupported_below_c
        )
    ):
        return "CONTEXT_UNSUPPORTED"
    if (
        normalized_score is not None
        and normalized_score >= threshold_policy.supported_min_c
        and pass_count >= threshold_policy.supported_min_pass
        and minor_count <= threshold_policy.supported_max_minor
        and fail_count == 0
        and not flags & LOCAL_HARD_FLAGS
    ):
        return "CONTEXT_SUPPORTED"
    return "CONTEXT_CONDITIONAL"


def global_recommendation(
    *,
    contextual_status_value: str,
    context_flags: Sequence[str],
    threshold_policy_status: str,
) -> str:
    flags = set(context_flags)
    if contextual_status_value == "SENSE_BOUNDARY_DETECTED":
        return "SENSE_SPLIT_RECOMMENDED"
    if contextual_status_value == "CONTEXT_UNSUPPORTED":
        return "INELIGIBLE_CONTEXTUAL_EVIDENCE"
    if threshold_policy_status == DEVELOPMENT_POLICY_STATUS:
        return "REQUIRES_GLOBAL_REVIEW"
    if (
        contextual_status_value == "CONTEXT_SUPPORTED"
        and "SENSE_DEFINITION_UNVERIFIED" not in flags
        and "JUDGE_DISAGREEMENT" not in flags
        and "SECOND_JUDGE_UNAVAILABLE" not in flags
        and "PAIRWISE_TIEBREAKER_UNAVAILABLE" not in flags
        and "MISSING_CONTRASTIVE_CONTEXT" not in flags
        and "INCOMPLETE_CONTEXT_TYPE_COVERAGE" not in flags
    ):
        return "ELIGIBLE_FOR_COMBINATION"
    return "REQUIRES_GLOBAL_REVIEW"

