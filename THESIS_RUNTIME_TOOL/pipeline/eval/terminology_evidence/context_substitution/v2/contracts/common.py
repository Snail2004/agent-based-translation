from __future__ import annotations

import hashlib
from typing import Any

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
)


SCHEMA_ID = "D2LContextSubstitutionRunV2"
SCHEMA_VERSION = "2.2.0"
RUBRIC_VERSION = "d2l_context_substitution_rubric_v2_1"
SELECTOR_VERSION = "d2l_context_selector_v2_1"
CONTEXT_DEDUP_POLICY_VERSION = "d2l_context_dedup_lexical_v2"
TRIAL_TRANSLATOR_VERSION = "d2l_context_trial_translator_v2_1"
TRIAL_QUALITY_GATE_VERSION = "d2l_trial_translation_quality_gate_v2_1"
JUDGE_VERSION = "d2l_context_judge_v2_1"
CONTRASTIVE_JUDGE_VERSION = "d2l_contrastive_sense_judge_v2"
AGGREGATION_VERSION = "d2l_context_aggregate_normalized_v2_1"
STATUS_POLICY_VERSION = "d2l_context_status_development_heuristic_v2_1"
APPLICATION_CONTRACT_VERSION = "d2l_context_application_contract_v2_1"
SUPPORT_SET_VERSION = "d2l_context_support_set_v2_1"
OOD_POLICY_VERSION = "d2l_context_ood_not_materialized_v2"
PROVENANCE_VERSION = "d2l_context_evidence_provenance_v2_1"
GOLD_SCHEMA_ID = "D2LContextSubstitutionGoldEvaluationV2"
GOLD_SCHEMA_VERSION = "2.0.0"

PROVIDER_ROUTE_IDS = (
    "shopaikey_gemini",
    "ckey_gemini",
    "gemini_official",
)

HASH_PATH = ("integrity", "run_sha256")
CONTEXT_TYPES = frozenset(
    {
        "definition",
        "typical_usage",
        "domain_collocation",
        "syntactic_variation",
        "same_sense_difficult",
        "contrastive",
        "unknown",
    }
)
REQUIRED_SAME_SENSE_CONTEXT_TYPES = (
    "definition",
    "typical_usage",
    "domain_collocation",
    "syntactic_variation",
    "same_sense_difficult",
)
SENSE_RELATIONS = frozenset({"SAME_SENSE", "CONTRASTIVE", "AMBIGUOUS"})
SENSE_DEFINITION_STATUSES = frozenset(
    {"VERIFIED", "UNVERIFIED", "INVALID"}
)
TRIAL_STATUSES = frozenset(
    {
        "VALID",
        "INVALID_CANDIDATE_USAGE",
        "EXTERNAL_TRANSLATION_ERROR",
        "INCOMPLETE_TRANSLATION",
        "ADDED_MEANING",
        "AMBIGUOUS_SOURCE",
        "SCHEMA_INVALID",
    }
)
JUDGEABILITY = frozenset(
    {
        "JUDGEABLE",
        "INSUFFICIENT_CONTEXT",
        "INVALID_SOURCE",
        "INVALID_TRIAL_TRANSLATION",
        "AMBIGUOUS_SENSE",
        "SENSE_DEFINITION_UNCERTAIN",
    }
)
CONTRASTIVE_RESULTS = frozenset(
    {
        "APPLICABLE_TO_OTHER_SENSE",
        "OUT_OF_SCOPE",
        "SEPARATE_SENSE_REQUIRED",
        "AMBIGUOUS",
    }
)
CONTEXT_LABELS = frozenset({"PASS", "MINOR", "FAIL"})
CONTEXTUAL_STATUSES = frozenset(
    {
        "CONTEXT_SUPPORTED",
        "CONTEXT_CONDITIONAL",
        "CONTEXT_UNSUPPORTED",
        "CONTEXT_UNJUDGEABLE",
        "SENSE_BOUNDARY_DETECTED",
    }
)
GLOBAL_RECOMMENDATIONS = frozenset(
    {
        "ELIGIBLE_FOR_COMBINATION",
        "REQUIRES_GLOBAL_REVIEW",
        "INELIGIBLE_CONTEXTUAL_EVIDENCE",
        "SENSE_SPLIT_RECOMMENDED",
    }
)
PAIRWISE_PREFERENCES = frozenset(
    {"CONTEXTUAL_PREFERENCE_A", "CONTEXTUAL_PREFERENCE_B", "TIE"}
)
VARIANT_STATUSES = frozenset({"OBSERVED_VALID", "PROPOSED"})
PROVIDER_ROLES = frozenset(
    {
        "context_selector",
        "trial_translator",
        "trial_translation_quality_gate",
        "context_judge",
        "contrastive_sense_judge",
        "pairwise_tiebreaker",
    }
)
CONTEXT_FLAGS = frozenset(
    {
        "SENSE_DEFINITION_UNVERIFIED",
        "SENSE_DEFINITION_INVALID",
        "INSUFFICIENT_VALID_SAME_SENSE_CONTEXTS",
        "MISSING_CONTRASTIVE_CONTEXT",
        "SECOND_JUDGE_UNAVAILABLE",
        "JUDGE_DISAGREEMENT",
        "SEMANTIC_EQUIVALENCE_LTE_2",
        "DOMAIN_SENSE_FIT_ZERO",
        "SEMANTIC_CONTRADICTION",
        "WRONG_SENSE",
        "CANDIDATE_INDUCED_DISTORTION",
        "PAIRWISE_TIEBREAKER_UNAVAILABLE",
        "INCOMPLETE_CONTEXT_TYPE_COVERAGE",
    }
)
LOCAL_HARD_FLAGS = frozenset(
    {
        "SEMANTIC_EQUIVALENCE_LTE_2",
        "DOMAIN_SENSE_FIT_ZERO",
        "SEMANTIC_CONTRADICTION",
        "WRONG_SENSE",
        "CANDIDATE_INDUCED_DISTORTION",
    }
)

RUN_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("candidates", "*", "context_flags"),
            ("candidates", "*", "context_results", "*", "local_hard_flags"),
            (
                "candidates",
                "*",
                "application_contract",
                "canonical_observed_context_ids",
            ),
            (
                "candidates",
                "*",
                "application_contract",
                "allowed_variants",
                "*",
                "context_ids",
            ),
            (
                "candidates",
                "*",
                "application_contract",
                "disallowed_variants",
                "*",
                "context_ids",
            ),
            (
                "candidates",
                "*",
                "application_contract",
                "application_notes",
                "*",
                "context_ids",
            ),
            ("candidates", "*", "provenance", "model_ids"),
            ("candidates", "*", "provenance", "attempted_source_hashes"),
            ("candidates", "*", "provenance", "selector_source_hashes"),
            ("candidates", "*", "provenance", "accepted_source_hashes"),
            ("candidates", "*", "provenance", "excluded_source_hashes"),
            ("candidates", "*", "provenance", "contrastive_source_hashes"),
            ("candidates", "*", "provenance", "response_hashes"),
            ("candidates", "*", "provenance", "pairwise_observation_ids"),
            (
                "candidates",
                "*",
                "provenance",
                "prompt_hashes_by_role",
                "context_selector",
            ),
            (
                "candidates",
                "*",
                "provenance",
                "prompt_hashes_by_role",
                "trial_translator",
            ),
            (
                "candidates",
                "*",
                "provenance",
                "prompt_hashes_by_role",
                "trial_translation_quality_gate",
            ),
            (
                "candidates",
                "*",
                "provenance",
                "prompt_hashes_by_role",
                "context_judge",
            ),
            (
                "candidates",
                "*",
                "provenance",
                "prompt_hashes_by_role",
                "contrastive_sense_judge",
            ),
            (
                "candidates",
                "*",
                "provenance",
                "prompt_hashes_by_role",
                "pairwise_tiebreaker",
            ),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("execution_policy", "provider_route_order"),
            (
                "execution_policy",
                "threshold_policy",
                "second_judge_thresholds",
            ),
            ("provider_attempts",),
            ("pairwise_observations",),
            ("candidates",),
            ("candidates", "*", "source_occurrences"),
            (
                "candidates",
                "*",
                "sense_contract",
                "definition_provenance",
            ),
            ("candidates", "*", "selector_annotations"),
            ("candidates", "*", "selector_context_sources"),
            ("candidates", "*", "selected_same_sense_context_ids"),
            ("candidates", "*", "selected_contrastive_context_ids"),
            ("candidates", "*", "missing_same_sense_context_types"),
            ("candidates", "*", "context_results"),
            ("candidates", "*", "context_results", "*", "trial_attempts"),
            (
                "candidates",
                "*",
                "contextual_evidence",
                "raw_context_scores",
            ),
            ("candidates", "*", "excluded_contexts"),
            ("candidates", "*", "excluded_contexts", "*", "trial_attempts"),
            ("candidates", "*", "contrastive_results"),
            ("candidates", "*", "judge_independence", "observations"),
            ("candidates", "*", "sense_boundary_observations"),
            (
                "candidates",
                "*",
                "application_contract",
                "allowed_variants",
            ),
            (
                "candidates",
                "*",
                "application_contract",
                "disallowed_variants",
            ),
            (
                "candidates",
                "*",
                "application_contract",
                "application_notes",
            ),
            (
                "candidates",
                "*",
                "certificate_support_set",
                "positive_support_contexts",
            ),
            (
                "candidates",
                "*",
                "certificate_support_set",
                "positive_support_context_ids",
            ),
            (
                "candidates",
                "*",
                "certificate_support_set",
                "negative_or_boundary_contexts",
            ),
            (
                "candidates",
                "*",
                "certificate_support_set",
                "negative_or_boundary_context_ids",
            ),
            (
                "candidates",
                "*",
                "certificate_support_set",
                "contrastive_contexts",
            ),
            (
                "candidates",
                "*",
                "certificate_support_set",
                "contrastive_context_ids",
            ),
            ("pairwise_observations", "*", "context_ids"),
        }
    ),
)


def require_bool(value: Any, *, path: str) -> bool:
    if type(value) is not bool:
        raise ContractValidationError("type", path, "expected a boolean")
    return value


def nonnegative_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(
            "type", path, "expected a nonnegative integer"
        )
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_digest(*parts: str) -> str:
    return sha256_text("\0".join(parts))

