from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_string,
    require_unique,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    CONTEXT_TYPES,
    CONTRASTIVE_RESULTS,
    JUDGEABILITY,
    SENSE_RELATIONS,
    TRIAL_STATUSES,
    require_bool,
)


def selector_schema() -> dict[str, Any]:
    annotation = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "context_id",
            "sense_relation",
            "context_type",
            "judgeability",
            "reason",
        ],
        "properties": {
            "context_id": {"type": "string"},
            "sense_relation": {"enum": sorted(SENSE_RELATIONS)},
            "context_type": {"enum": sorted(CONTEXT_TYPES)},
            "judgeability": {"enum": sorted(JUDGEABILITY)},
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["term_id", "sense_id", "scope_id", "annotations"],
        "properties": {
            "term_id": {"type": "string"},
            "sense_id": {"type": "string"},
            "scope_id": {"type": "string"},
            "annotations": {"type": "array", "items": annotation},
        },
    }


def trial_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "context_id",
            "candidate_id",
            "trial_translation",
            "candidate_surface_used",
            "candidate_usage_confirmed",
            "applied_expansion",
        ],
        "properties": {
            "context_id": {"type": "string"},
            "candidate_id": {"type": "string"},
            "trial_translation": {"type": "string"},
            "candidate_surface_used": {"type": "string"},
            "candidate_usage_confirmed": {"type": "boolean"},
            "applied_expansion": {
                "anyOf": [{"type": "null"}, {"type": "string"}]
            },
        },
    }


def trial_gate_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "context_id",
            "candidate_id",
            "trial_status",
            "candidate_usage_valid",
            "external_translation_error",
            "missing_content",
            "added_content",
            "reason",
        ],
        "properties": {
            "context_id": {"type": "string"},
            "candidate_id": {"type": "string"},
            "trial_status": {"enum": sorted(TRIAL_STATUSES)},
            "candidate_usage_valid": {"type": "boolean"},
            "external_translation_error": {"type": "boolean"},
            "missing_content": {"type": "boolean"},
            "added_content": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    }


def context_judge_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "context_id",
            "candidate_id",
            "judgeability",
            "scores",
            "flags",
            "evidence",
            "variant_observation",
            "reason",
        ],
        "properties": {
            "context_id": {"type": "string"},
            "candidate_id": {"type": "string"},
            "judgeability": {"enum": sorted(JUDGEABILITY)},
            "scores": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "semantic_equivalence",
                            "domain_sense_fit",
                            "collocation_naturalness",
                            "grammatical_fit",
                            "no_candidate_induced_distortion",
                        ],
                        "properties": {
                            "semantic_equivalence": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 4,
                            },
                            "domain_sense_fit": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 2,
                            },
                            "collocation_naturalness": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 2,
                            },
                            "grammatical_fit": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "no_candidate_induced_distortion": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                    },
                ]
            },
            "flags": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "semantic_contradiction",
                    "wrong_sense",
                    "candidate_induced_distortion",
                    "translator_external_error",
                    "insufficient_context",
                ],
                "properties": {
                    key: {"type": "boolean"}
                    for key in (
                        "semantic_contradiction",
                        "wrong_sense",
                        "candidate_induced_distortion",
                        "translator_external_error",
                        "insufficient_context",
                    )
                },
            },
            "evidence": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_span", "target_span"],
                        "properties": {
                            "source_span": {"type": "string"},
                            "target_span": {"type": "string"},
                        },
                    },
                ]
            },
            "variant_observation": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "surface_used",
                    "requires_expansion",
                    "suggested_expansion",
                ],
                "properties": {
                    "surface_used": {"type": "string"},
                    "requires_expansion": {"type": "boolean"},
                    "suggested_expansion": {
                        "anyOf": [{"type": "null"}, {"type": "string"}]
                    },
                },
            },
            "reason": {"type": "string"},
        },
    }


def contrastive_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "context_id",
            "candidate_id",
            "tested_sense_id",
            "result",
            "reason",
        ],
        "properties": {
            "context_id": {"type": "string"},
            "candidate_id": {"type": "string"},
            "tested_sense_id": {"type": "string"},
            "result": {"enum": sorted(CONTRASTIVE_RESULTS)},
            "reason": {"type": "string"},
        },
    }


def validate_selector(
    value: Any,
    *,
    term: Mapping[str, Any],
    contexts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    path = "$.selector"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"term_id", "sense_id", "scope_id", "annotations"},
        path=path,
    )
    _require_binding(row, "term_id", term["term_id"], path=path)
    _require_binding(row, "sense_id", term["sense_id"], path=path)
    _require_binding(row, "scope_id", term["scope_id"], path=path)
    annotations = [
        validate_selector_annotation(
            item, path=f"{path}.annotations[{index}]"
        )
        for index, item in enumerate(
            require_list(row["annotations"], path=f"{path}.annotations")
        )
    ]
    expected_ids = [
        str(context.get("context_id") or context["block_id"])
        for context in contexts
    ]
    actual_ids = [annotation["context_id"] for annotation in annotations]
    require_unique(actual_ids, path=f"{path}.annotations[*].context_id")
    if sorted(actual_ids) != sorted(expected_ids):
        raise ContractValidationError(
            "selector_cover",
            f"{path}.annotations",
            "annotations must cover every supplied context exactly once",
        )
    by_id = {annotation["context_id"]: annotation for annotation in annotations}
    return {
        "term_id": term["term_id"],
        "sense_id": term["sense_id"],
        "scope_id": term["scope_id"],
        "annotations": [by_id[context_id] for context_id in expected_ids],
    }


def validate_selector_annotation(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "sense_relation",
            "context_type",
            "judgeability",
            "reason",
        },
        path=path,
    )
    relation = require_enum(
        row["sense_relation"], SENSE_RELATIONS, path=f"{path}.sense_relation"
    )
    context_type = require_enum(
        row["context_type"], CONTEXT_TYPES, path=f"{path}.context_type"
    )
    if relation == "CONTRASTIVE" and context_type != "contrastive":
        raise ContractValidationError(
            "selector_relation",
            f"{path}.context_type",
            "contrastive relation requires contrastive context type",
        )
    if relation == "SAME_SENSE" and context_type == "contrastive":
        raise ContractValidationError(
            "selector_relation",
            f"{path}.context_type",
            "same-sense context cannot use contrastive type",
        )
    return {
        "context_id": require_string(
            row["context_id"], path=f"{path}.context_id"
        ),
        "sense_relation": relation,
        "context_type": context_type,
        "judgeability": require_enum(
            row["judgeability"], JUDGEABILITY, path=f"{path}.judgeability"
        ),
        "reason": require_string(
            row["reason"], path=f"{path}.reason", maximum=2_000
        ),
    }


def validate_trial(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    path = "$.trial"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "candidate_id",
            "trial_translation",
            "candidate_surface_used",
            "candidate_usage_confirmed",
            "applied_expansion",
        },
        path=path,
    )
    _require_binding(row, "context_id", context_id, path=path)
    _require_binding(row, "candidate_id", candidate_id, path=path)
    return {
        "context_id": context_id,
        "candidate_id": candidate_id,
        "trial_translation": require_string(
            row["trial_translation"],
            path=f"{path}.trial_translation",
            maximum=20_000,
        ),
        "candidate_surface_used": require_string(
            row["candidate_surface_used"],
            path=f"{path}.candidate_surface_used",
            maximum=500,
        ),
        "candidate_usage_confirmed": require_bool(
            row["candidate_usage_confirmed"],
            path=f"{path}.candidate_usage_confirmed",
        ),
        "applied_expansion": _nullable_string(
            row["applied_expansion"],
            path=f"{path}.applied_expansion",
            maximum=500,
        ),
    }


def validate_trial_gate(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    path = "$.trial_gate"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "candidate_id",
            "trial_status",
            "candidate_usage_valid",
            "external_translation_error",
            "missing_content",
            "added_content",
            "reason",
        },
        path=path,
    )
    _require_binding(row, "context_id", context_id, path=path)
    _require_binding(row, "candidate_id", candidate_id, path=path)
    status = require_enum(
        row["trial_status"], TRIAL_STATUSES, path=f"{path}.trial_status"
    )
    usage_valid = require_bool(
        row["candidate_usage_valid"], path=f"{path}.candidate_usage_valid"
    )
    external_error = require_bool(
        row["external_translation_error"],
        path=f"{path}.external_translation_error",
    )
    missing = require_bool(
        row["missing_content"], path=f"{path}.missing_content"
    )
    added = require_bool(row["added_content"], path=f"{path}.added_content")
    if status == "VALID" and (
        not usage_valid or external_error or missing or added
    ):
        raise ContractValidationError(
            "trial_gate_consistency",
            path,
            "VALID gate has contradictory defect flags",
        )
    required_flag = {
        "INVALID_CANDIDATE_USAGE": not usage_valid,
        "EXTERNAL_TRANSLATION_ERROR": external_error,
        "INCOMPLETE_TRANSLATION": missing,
        "ADDED_MEANING": added,
        "AMBIGUOUS_SOURCE": True,
        "SCHEMA_INVALID": True,
        "VALID": True,
    }[status]
    if not required_flag:
        raise ContractValidationError(
            "trial_gate_consistency",
            path,
            f"{status} lacks its required defect flag",
        )
    return {
        "context_id": context_id,
        "candidate_id": candidate_id,
        "trial_status": status,
        "candidate_usage_valid": usage_valid,
        "external_translation_error": external_error,
        "missing_content": missing,
        "added_content": added,
        "reason": require_string(
            row["reason"], path=f"{path}.reason", maximum=2_000
        ),
    }


def validate_context_judge(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    path = "$.context_judge"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "candidate_id",
            "judgeability",
            "scores",
            "flags",
            "evidence",
            "variant_observation",
            "reason",
        },
        path=path,
    )
    _require_binding(row, "context_id", context_id, path=path)
    _require_binding(row, "candidate_id", candidate_id, path=path)
    judgeability = require_enum(
        row["judgeability"], JUDGEABILITY, path=f"{path}.judgeability"
    )
    scores: dict[str, int] | None
    evidence: dict[str, str] | None
    if judgeability == "JUDGEABLE":
        score_row = require_mapping(row["scores"], path=f"{path}.scores")
        maxima = {
            "semantic_equivalence": 4,
            "domain_sense_fit": 2,
            "collocation_naturalness": 2,
            "grammatical_fit": 1,
            "no_candidate_induced_distortion": 1,
        }
        require_exact_keys(score_row, required=set(maxima), path=f"{path}.scores")
        scores = {}
        for key, maximum in maxima.items():
            score = require_int(
                score_row[key], path=f"{path}.scores.{key}", minimum=0
            )
            if score > maximum:
                raise ContractValidationError(
                    "range", f"{path}.scores.{key}", f"must be <= {maximum}"
                )
            scores[key] = score
        evidence_row = require_mapping(row["evidence"], path=f"{path}.evidence")
        require_exact_keys(
            evidence_row,
            required={"source_span", "target_span"},
            path=f"{path}.evidence",
        )
        evidence = {
            "source_span": require_string(
                evidence_row["source_span"],
                path=f"{path}.evidence.source_span",
                maximum=2_000,
            ),
            "target_span": require_string(
                evidence_row["target_span"],
                path=f"{path}.evidence.target_span",
                maximum=2_000,
            ),
        }
    else:
        if row["scores"] is not None or row["evidence"] is not None:
            raise ContractValidationError(
                "judgeability",
                path,
                "non-judgeable output must not score or claim evidence spans",
            )
        scores = None
        evidence = None
    flag_row = require_mapping(row["flags"], path=f"{path}.flags")
    flag_names = {
        "semantic_contradiction",
        "wrong_sense",
        "candidate_induced_distortion",
        "translator_external_error",
        "insufficient_context",
    }
    require_exact_keys(flag_row, required=flag_names, path=f"{path}.flags")
    flags = {
        key: require_bool(flag_row[key], path=f"{path}.flags.{key}")
        for key in sorted(flag_names)
    }
    expected_error_flags = {
        "translator_external_error": (
            judgeability == "INVALID_TRIAL_TRANSLATION"
        ),
        "insufficient_context": judgeability == "INSUFFICIENT_CONTEXT",
    }
    for flag_name, expected in expected_error_flags.items():
        if flags[flag_name] is not expected:
            raise ContractValidationError(
                "judgeability_flag_binding",
                f"{path}.flags.{flag_name}",
                f"must be {expected} for judgeability={judgeability}",
            )
    if judgeability != "JUDGEABLE" and any(
        flags[name]
        for name in (
            "semantic_contradiction",
            "wrong_sense",
            "candidate_induced_distortion",
        )
    ):
        raise ContractValidationError(
            "judgeability_flag_binding",
            f"{path}.flags",
            "non-judgeable output must not claim candidate-quality flags",
        )
    variant_row = require_mapping(
        row["variant_observation"],
        path=f"{path}.variant_observation",
    )
    require_exact_keys(
        variant_row,
        required={
            "surface_used",
            "requires_expansion",
            "suggested_expansion",
        },
        path=f"{path}.variant_observation",
    )
    requires_expansion = require_bool(
        variant_row["requires_expansion"],
        path=f"{path}.variant_observation.requires_expansion",
    )
    suggested_expansion = _nullable_string(
        variant_row["suggested_expansion"],
        path=f"{path}.variant_observation.suggested_expansion",
        maximum=500,
    )
    if requires_expansion != (suggested_expansion is not None):
        raise ContractValidationError(
            "variant_observation",
            f"{path}.variant_observation",
            "requires_expansion must match presence of suggested_expansion",
        )
    variant_observation = {
        "surface_used": require_string(
            variant_row["surface_used"],
            path=f"{path}.variant_observation.surface_used",
            maximum=500,
        ),
        "requires_expansion": requires_expansion,
        "suggested_expansion": suggested_expansion,
    }
    return {
        "context_id": context_id,
        "candidate_id": candidate_id,
        "judgeability": judgeability,
        "scores": scores,
        "flags": flags,
        "evidence": evidence,
        "variant_observation": variant_observation,
        "reason": require_string(
            row["reason"], path=f"{path}.reason", maximum=2_000
        ),
    }


def _nullable_string(
    value: Any,
    *,
    path: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return require_string(value, path=path, maximum=maximum)


def validate_contrastive(
    value: Any,
    *,
    context_id: str,
    candidate_id: str,
    tested_sense_id: str,
) -> dict[str, Any]:
    path = "$.contrastive_judge"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "candidate_id",
            "tested_sense_id",
            "result",
            "reason",
        },
        path=path,
    )
    _require_binding(row, "context_id", context_id, path=path)
    _require_binding(row, "candidate_id", candidate_id, path=path)
    _require_binding(row, "tested_sense_id", tested_sense_id, path=path)
    return {
        "context_id": context_id,
        "candidate_id": candidate_id,
        "tested_sense_id": tested_sense_id,
        "result": require_enum(
            row["result"], CONTRASTIVE_RESULTS, path=f"{path}.result"
        ),
        "reason": require_string(
            row["reason"], path=f"{path}.reason", maximum=2_000
        ),
    }


def _require_binding(
    row: Mapping[str, Any], key: str, expected: str, *, path: str
) -> None:
    if require_string(row[key], path=f"{path}.{key}") != expected:
        raise ContractValidationError(
            "response_binding", f"{path}.{key}", f"{key} drift"
        )


