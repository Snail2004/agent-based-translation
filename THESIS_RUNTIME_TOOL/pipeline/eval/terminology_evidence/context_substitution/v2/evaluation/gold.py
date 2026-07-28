from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_number,
    require_string,
    require_unique,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    GOLD_SCHEMA_ID,
    GOLD_SCHEMA_VERSION,
    require_bool,
)


GOLD_CONTEXT_LABELS = frozenset(
    {"PASS", "MINOR", "FAIL", "NOT_JUDGEABLE"}
)
GOLD_SENSE_LABELS = frozenset(
    {"SAME_SENSE", "OUT_OF_SCOPE", "SPLIT_REQUIRED", "AMBIGUOUS"}
)
GOLD_VARIANT_LABELS = frozenset(
    {"VALID_VARIANT", "CONDITIONAL_VARIANT", "INVALID_VARIANT"}
)


def evaluate_gold_cases(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    validated = validate_gold_dataset(cases)
    if not validated:
        raise ContractValidationError(
            "gold_cases", "$.cases", "at least one gold case is required"
        )
    context_pairs = [
        (row["gold_context_label"], row["predicted_context_label"])
        for row in validated
    ]
    sense_pairs = [
        (row["gold_sense_label"], row["predicted_sense_label"])
        for row in validated
    ]
    variant_pairs = [
        (row["gold_variant_label"], row["predicted_variant_label"])
        for row in validated
        if row["gold_variant_label"] is not None
    ]
    predicted_fail = [
        row for row in validated if row["predicted_context_label"] == "FAIL"
    ]
    wrong_sense = [
        row
        for row in validated
        if row["gold_sense_label"] in {"OUT_OF_SCOPE", "SPLIT_REQUIRED"}
    ]
    return {
        "schema_id": GOLD_SCHEMA_ID,
        "schema_version": GOLD_SCHEMA_VERSION,
        "case_count": len(validated),
        "context_accuracy": _accuracy(context_pairs),
        "context_cohen_kappa": _cohen_kappa(context_pairs),
        "fail_precision": _ratio(
            sum(row["gold_context_label"] == "FAIL" for row in predicted_fail),
            len(predicted_fail),
        ),
        "wrong_sense_recall": _ratio(
            sum(
                row["predicted_sense_label"]
                in {"OUT_OF_SCOPE", "SPLIT_REQUIRED"}
                for row in wrong_sense
            ),
            len(wrong_sense),
        ),
        "sense_accuracy": _accuracy(sense_pairs),
        "sense_cohen_kappa": _cohen_kappa(sense_pairs),
        "variant_accuracy": (
            _accuracy(variant_pairs) if variant_pairs else None
        ),
        "translator_misattribution_rate": _ratio(
            sum(
                row["translator_error_gold"]
                and row["translator_error_attributed_to_candidate"]
                for row in validated
            ),
            sum(row["translator_error_gold"] for row in validated),
        ),
        "decision_flip_rate": _ratio(
            sum(row["decision_flipped"] for row in validated),
            len(validated),
        ),
        "stability_rate": _ratio(
            sum(row["repeat_consistent"] for row in validated),
            len(validated),
        ),
        "invalid_context_replacement_rate": _ratio(
            sum(row["invalid_context_replaced"] for row in validated),
            sum(row["invalid_context_seen"] for row in validated),
        ),
        "usage": {
            "input_tokens": sum(row["input_tokens"] for row in validated),
            "output_tokens": sum(row["output_tokens"] for row in validated),
            "total_tokens": sum(
                row["input_tokens"] + row["output_tokens"]
                for row in validated
            ),
        },
        "probability_calibration_error": None,
        "probability_calibration_status": "NOT_APPLICABLE_NON_PROBABILISTIC_C",
    }


def validate_gold_case(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {
        "case_id",
        "gold_context_label",
        "predicted_context_label",
        "gold_sense_label",
        "predicted_sense_label",
        "gold_variant_label",
        "predicted_variant_label",
        "translator_error_gold",
        "translator_error_attributed_to_candidate",
        "decision_flipped",
        "repeat_consistent",
        "invalid_context_seen",
        "invalid_context_replaced",
        "input_tokens",
        "output_tokens",
    }
    require_exact_keys(row, required=required, path=path)
    gold_variant = row["gold_variant_label"]
    predicted_variant = row["predicted_variant_label"]
    if (gold_variant is None) != (predicted_variant is None):
        raise ContractValidationError(
            "variant_pair",
            path,
            "gold and predicted variant labels must both be null or both present",
        )
    if gold_variant is not None:
        gold_variant = require_enum(
            gold_variant,
            GOLD_VARIANT_LABELS,
            path=f"{path}.gold_variant_label",
        )
        predicted_variant = require_enum(
            predicted_variant,
            GOLD_VARIANT_LABELS,
            path=f"{path}.predicted_variant_label",
        )
    result = {
        "case_id": require_string(row["case_id"], path=f"{path}.case_id"),
        "gold_context_label": require_enum(
            row["gold_context_label"],
            GOLD_CONTEXT_LABELS,
            path=f"{path}.gold_context_label",
        ),
        "predicted_context_label": require_enum(
            row["predicted_context_label"],
            GOLD_CONTEXT_LABELS,
            path=f"{path}.predicted_context_label",
        ),
        "gold_sense_label": require_enum(
            row["gold_sense_label"],
            GOLD_SENSE_LABELS,
            path=f"{path}.gold_sense_label",
        ),
        "predicted_sense_label": require_enum(
            row["predicted_sense_label"],
            GOLD_SENSE_LABELS,
            path=f"{path}.predicted_sense_label",
        ),
        "gold_variant_label": gold_variant,
        "predicted_variant_label": predicted_variant,
    }
    for key in (
        "translator_error_gold",
        "translator_error_attributed_to_candidate",
        "decision_flipped",
        "repeat_consistent",
        "invalid_context_seen",
        "invalid_context_replaced",
    ):
        result[key] = require_bool(row[key], path=f"{path}.{key}")
    for key in ("input_tokens", "output_tokens"):
        number = require_number(row[key], path=f"{path}.{key}", minimum=0)
        if int(number) != number:
            raise ContractValidationError(
                "type", f"{path}.{key}", "token count must be an integer"
            )
        result[key] = int(number)
    if result["invalid_context_replaced"] and not result["invalid_context_seen"]:
        raise ContractValidationError(
            "replacement_binding",
            path,
            "replacement cannot occur without an invalid context",
        )
    return result


def validate_gold_dataset(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.cases")
    validated = [
        validate_gold_case(row, path=f"$.cases[{index}]")
        for index, row in enumerate(rows)
    ]
    require_unique(
        [row["case_id"] for row in validated], path="$.cases[*].case_id"
    )
    return validated


def _accuracy(pairs: Sequence[tuple[str, str]]) -> float:
    return round(_ratio(sum(left == right for left, right in pairs), len(pairs)), 6)


def _cohen_kappa(pairs: Sequence[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    labels = sorted({value for pair in pairs for value in pair})
    observed = _ratio(sum(left == right for left, right in pairs), len(pairs))
    left_counts = Counter(left for left, _right in pairs)
    right_counts = Counter(right for _left, right in pairs)
    expected = sum(
        (left_counts[label] / len(pairs)) * (right_counts[label] / len(pairs))
        for label in labels
    )
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 6)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


