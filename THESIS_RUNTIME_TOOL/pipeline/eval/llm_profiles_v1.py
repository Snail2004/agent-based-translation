from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from pipeline.llm_backend import (
    ContractValidationError,
    canonical_sha256,
    validate_pipeline_profile,
)

from pipeline.eval.scorer_prompts_v3 import (
    PJ_COMMON_CANDIDATE_ID,
    PJ_COMMON_PROMPT_SHA256,
    SF_BT_SEMANTIC_CANDIDATE_ID,
    SF_BT_SEMANTIC_PROMPT_SHA256,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    SF_BT_REVERSE_CANDIDATE_ID,
    SF_BT_REVERSE_PROMPT_SHA256,
)


__all__ = [
    "EVALUATION_LLM_ROLE_IDS",
    "EVALUATION_LLM_PROFILE_ID",
    "EVALUATION_LLM_PROFILE_REVISION",
    "EVALUATION_PROMPT_VALIDATED_JSON_OUTPUT_INSTRUCTION_V1",
    "build_evaluation_llm_profile_v1",
    "evaluation_response_schema_v1",
    "evaluation_role_budget_v1",
    "evaluation_role_contract_v1",
]


EVALUATION_LLM_PROFILE_ID = "evaluation_semantic_llm_v1"
EVALUATION_LLM_PROFILE_REVISION = "v1"
EVALUATION_PROMPT_VALIDATED_JSON_OUTPUT_INSTRUCTION_V1 = (
    "OUTPUT ENVELOPE: Return one raw JSON object. Do not use Markdown code "
    "fences or any text before or after the JSON object."
)

SF_BT_BACK_TRANSLATOR_ROLE_ID = "evaluation.sf_bt.back_translator"
SF_BT_SEMANTIC_JUDGE_ROLE_ID = "evaluation.sf_bt.semantic_judge"
PJ_JUDGE_ROLE_ID = "evaluation.pj.judge"
EVALUATION_LLM_ROLE_IDS = frozenset(
    {
        SF_BT_BACK_TRANSLATOR_ROLE_ID,
        SF_BT_SEMANTIC_JUDGE_ROLE_ID,
        PJ_JUDGE_ROLE_ID,
    }
)

_JSON_SCHEMA_DIALECT = "json_schema_2020_12"
_STRUCTURED_OUTPUT_MODES = frozenset(
    {"required", "prompt_validated", "preferred", "disabled"}
)
_COMMON_GENERATION = {
    "context_window_tokens": 64_000,
    "max_input_tokens": 12_000,
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": None,
    "reasoning_effort": "none",
    "verbosity": None,
}
_NO_RETRY = {
    "max_retries": 0,
    "backoff_policy": "none",
    "initial_delay_ms": 0,
    "max_delay_ms": 0,
    "retryable_codes": [],
}
_NO_SEMANTIC_RETRY_IN_SEAL = {
    "max_retries": 0,
    "retryable_categories": [],
}
_PROMPT_VALIDATED_COMPLETION_CERTIFICATION_TOKENS = 4_096

_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: {
        "type": "object",
        "additionalProperties": False,
        "required": ["back_translation"],
        "properties": {
            "back_translation": {
                "type": "string",
                "minLength": 1,
            }
        },
    },
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "flags", "note"],
        "properties": {
            "score": {"type": "integer", "enum": [0, 25, 50, 75, 100]},
            "flags": {
                "type": "array",
                "maxItems": 6,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": [
                        "semantic_mismatch",
                        "numeric_mismatch",
                        "negation_mismatch",
                        "coverage_mismatch",
                        "untranslated_residue",
                        "format_only",
                    ],
                },
            },
            "note": {"type": "string", "maxLength": 240},
        },
    },
    PJ_JUDGE_ROLE_ID: {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "overall_verdict",
            "style_verdict",
            "tags",
            "note",
        ],
        "properties": {
            "overall_verdict": {
                "type": "string",
                "enum": ["candidate_1", "candidate_2", "tie"],
            },
            "style_verdict": {
                "type": "string",
                "enum": ["candidate_1", "candidate_2", "tie"],
            },
            "tags": {
                "type": "array",
                "maxItems": 3,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": [
                        "grammar",
                        "naturalness",
                        "word_choice",
                        "terminology",
                        "meaning",
                        "omission_addition",
                        "formatting",
                        "tone_voice",
                    ],
                },
            },
            "note": {"type": "string", "maxLength": 240},
        },
    },
}

_ROLE_SPECS: dict[str, dict[str, Any]] = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: {
        "preset_id": "evaluation.sf_bt.back_translator.recommended_v1",
        "preset_revision": "v1",
        "prompt": {
            "id": SF_BT_REVERSE_CANDIDATE_ID,
            "revision": "v3_1",
            "sha256": SF_BT_REVERSE_PROMPT_SHA256,
        },
        "response_schema_id": "evaluation_sf_bt_reverse_response_v1",
        "validator_id": "evaluation_sf_bt_reverse_validator_v1",
        "semantic_extension_id": "evaluation_sf_bt_reverse_semantics_v1",
        "max_output_tokens": 4_096,
    },
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: {
        "preset_id": "evaluation.sf_bt.semantic_judge.recommended_v1",
        "preset_revision": "v1",
        "prompt": {
            "id": SF_BT_SEMANTIC_CANDIDATE_ID,
            "revision": "v3",
            "sha256": SF_BT_SEMANTIC_PROMPT_SHA256,
        },
        "response_schema_id": "evaluation_sf_bt_semantic_response_v1",
        "validator_id": "evaluation_sf_bt_semantic_validator_v3",
        "semantic_extension_id": "evaluation_sf_bt_semantic_semantics_v1",
        "max_output_tokens": 512,
    },
    PJ_JUDGE_ROLE_ID: {
        "preset_id": "evaluation.pj.judge.recommended_v1",
        "preset_revision": "v1",
        "prompt": {
            "id": PJ_COMMON_CANDIDATE_ID,
            "revision": "v2",
            "sha256": PJ_COMMON_PROMPT_SHA256,
        },
        "response_schema_id": "evaluation_pj_response_v2",
        "validator_id": "evaluation_pj_response_validator_v2",
        "semantic_extension_id": "evaluation_pj_semantics_v1",
        "max_output_tokens": 512,
    },
}


def evaluation_response_schema_v1(role_id: str) -> dict[str, Any]:
    _require_role_id(role_id)
    return deepcopy(_RESPONSE_SCHEMAS[role_id])


def evaluation_role_contract_v1(role_id: str) -> dict[str, Any]:
    _require_role_id(role_id)
    spec = _ROLE_SPECS[role_id]
    schema = _RESPONSE_SCHEMAS[role_id]
    schema_sha256 = canonical_sha256(schema)
    validator = {
        "id": spec["validator_id"],
        "revision": "v1",
        "sha256": canonical_sha256(
            {
                "role_id": role_id,
                "response_schema_sha256": schema_sha256,
                "local_contract": spec["validator_id"],
            }
        ),
    }
    return {
        "role_id": role_id,
        "prompt": deepcopy(spec["prompt"]),
        "response_schema": {
            "id": spec["response_schema_id"],
            "revision": "v1",
            "sha256": schema_sha256,
        },
        "validator": validator,
    }


def evaluation_role_budget_v1(
    role_id: str,
    *,
    structured_output_mode: str = "required",
) -> dict[str, Any]:
    """Return the pipeline-owned resource envelope before provider binding."""

    _require_role_id(role_id)
    if structured_output_mode not in _STRUCTURED_OUTPUT_MODES:
        raise ContractValidationError(
            "unsupported Evaluation structured-output mode: "
            f"{structured_output_mode!r}"
        )
    spec = _ROLE_SPECS[role_id]
    max_output_tokens = spec["max_output_tokens"]
    max_completion_tokens = max_output_tokens
    if structured_output_mode == "prompt_validated":
        max_completion_tokens = max(
            max_completion_tokens,
            _PROMPT_VALIDATED_COMPLETION_CERTIFICATION_TOKENS,
        )
    return {
        "role_id": role_id,
        "preset_id": spec["preset_id"],
        "preset_revision": spec["preset_revision"],
        "generation": {
            **deepcopy(_COMMON_GENERATION),
            "max_output_tokens": max_output_tokens,
        },
        "transport_retry": deepcopy(_NO_RETRY),
        "semantic_retry": deepcopy(_NO_SEMANTIC_RETRY_IN_SEAL),
        "limits": {
            "max_calls": 1,
            "max_prompt_tokens": _COMMON_GENERATION["max_input_tokens"],
            "max_completion_tokens": max_completion_tokens,
            "max_total_tokens": (
                _COMMON_GENERATION["max_input_tokens"]
                + max_completion_tokens
            ),
            "max_cost_usd": None,
            "request_timeout_ms": 180_000,
        },
    }


def build_evaluation_llm_profile_v1(
    *,
    primary_targets: Mapping[str, Mapping[str, Any]],
    profile_id: str = EVALUATION_LLM_PROFILE_ID,
    profile_revision: str = EVALUATION_LLM_PROFILE_REVISION,
    structured_output_mode: str = "preferred",
) -> dict[str, Any]:
    """Build a concrete profile without choosing a source, model, or credential."""

    if not isinstance(primary_targets, Mapping) or not primary_targets:
        raise ContractValidationError(
            "Evaluation LLM profile requires at least one concrete role target"
        )
    unknown = set(primary_targets) - EVALUATION_LLM_ROLE_IDS
    if unknown:
        raise ContractValidationError(
            f"Evaluation LLM profile contains unsupported roles: {sorted(unknown)}"
        )
    if structured_output_mode not in _STRUCTURED_OUTPUT_MODES:
        raise ContractValidationError(
            f"unsupported Evaluation structured-output mode: {structured_output_mode!r}"
        )
    roles = [
        _build_role_binding(
            role_id,
            primary_targets[role_id],
            structured_output_mode=structured_output_mode,
        )
        for role_id in sorted(primary_targets)
    ]
    return validate_pipeline_profile(
        {
            "schema_version": "pipeline_profile_v1",
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "workstream": "evaluation",
            "role_bindings": roles,
        }
    )


def _build_role_binding(
    role_id: str,
    primary_target: Mapping[str, Any],
    *,
    structured_output_mode: str,
) -> dict[str, Any]:
    spec = _ROLE_SPECS[role_id]
    contract = evaluation_role_contract_v1(role_id)
    budget = evaluation_role_budget_v1(
        role_id,
        structured_output_mode=structured_output_mode,
    )
    semantic_extension = {
        "role_id": role_id,
        "semantic_acceptance": "local_validator",
        "semantic_retry": "new_seal_required",
        "publication_authority": "evaluation_only",
    }
    preset_revision = spec["preset_revision"]
    if structured_output_mode == "prompt_validated":
        preset_revision = "v3-prompt-validated"
    elif structured_output_mode != "preferred":
        preset_revision = f"v2-{structured_output_mode.replace('_', '-')}"
    return {
        "workstream": "evaluation",
        "role_id": role_id,
        "preset_id": spec["preset_id"],
        "preset_revision": preset_revision,
        "primary": deepcopy(dict(primary_target)),
        "fallback_plan": {"enabled": False, "steps": []},
        "generation": budget["generation"],
        "transport_retry": budget["transport_retry"],
        "semantic_retry": budget["semantic_retry"],
        "limits": budget["limits"],
        "structured_output": {
            "mode": structured_output_mode,
            "schema_dialect": (
                None
                if structured_output_mode == "disabled"
                else _JSON_SCHEMA_DIALECT
            ),
        },
        "namespaces": {
            "output": f"{role_id}.output",
            "checkpoint": f"{role_id}.checkpoint",
            "cache": f"{role_id}.cache",
        },
        "prompt": contract["prompt"],
        "response_schema": contract["response_schema"],
        "validator": contract["validator"],
        "semantic_extension": {
            "id": spec["semantic_extension_id"],
            "schema_version": "v1",
            "sha256": canonical_sha256(semantic_extension),
        },
    }


def _require_role_id(role_id: str) -> None:
    if role_id not in EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            f"unsupported Evaluation semantic LLM role: {role_id!r}"
        )
