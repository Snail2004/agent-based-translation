from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.aggregation import (
    aggregate_contextual_evidence,
    compute_context_result,
    global_recommendation,
    merge_judge_labels,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.application import (
    build_application_contract,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.input import (
    normalize_context_substitution_input,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    AGGREGATION_VERSION,
    APPLICATION_CONTRACT_VERSION,
    CONTEXT_DEDUP_POLICY_VERSION,
    CONTRASTIVE_JUDGE_VERSION,
    JUDGE_VERSION,
    LOCAL_HARD_FLAGS,
    OOD_POLICY_VERSION,
    REQUIRED_SAME_SENSE_CONTEXT_TYPES,
    RUBRIC_VERSION,
    SCHEMA_ID,
    SCHEMA_VERSION,
    SELECTOR_VERSION,
    SUPPORT_SET_VERSION,
    TRIAL_QUALITY_GATE_VERSION,
    TRIAL_TRANSLATOR_VERSION,
    stable_digest,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.pairwise import (
    PAIRWISE_VERSION,
    close_candidate_pairs,
    run_pairwise_tiebreakers,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.prompts import (
    CONTEXT_JUDGE_SYSTEM_PROMPT,
    CONTRASTIVE_SYSTEM_PROMPT,
    SELECTOR_SYSTEM_PROMPT,
    TRIAL_GATE_SYSTEM_PROMPT,
    TRIAL_SYSTEM_PROMPT,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.providers.base import (
    ContextExecutionError,
    FailoverStructuredModel,
    ProviderCallCollector,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.responses import (
    context_judge_schema,
    contrastive_schema,
    selector_schema,
    trial_gate_schema,
    trial_schema,
    validate_context_judge,
    validate_contrastive,
    validate_selector,
    validate_trial,
    validate_trial_gate,
    validate_selector_annotation,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.selection import (
    candidate_profile,
    context_identity,
    missing_required_context_types,
    select_classified_contexts,
    selector_context_payload,
    selector_term_profile,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.surface import (
    trial_surface_binding,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.provenance import (
    source_provenance_from_context,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.evidence.support_set import (
    build_certificate_support_set,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.evidence.provenance import (
    build_candidate_provenance,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration import (
    ContextThresholdPolicy,
    DEVELOPMENT_HEURISTIC_POLICY,
    validate_evaluation_mode,
    validate_threshold_policy,
)


TARGET_ROLES = frozenset(
    {"canonical", "alternative", "rejected", "pending"}
)


def run_d2l_context_substitution(
    input_payload: Mapping[str, Any],
    model: FailoverStructuredModel,
    *,
    candidate_target_ids: Sequence[str] | None = None,
    include_target_roles: Sequence[str] = (
        "canonical",
        "alternative",
        "pending",
    ),
    threshold_policy: ContextThresholdPolicy = DEVELOPMENT_HEURISTIC_POLICY,
    evaluation_mode: str = "DEVELOPMENT",
) -> dict[str, Any]:
    """Build CST V2 contextual evidence packages without glossary decisions."""

    threshold_policy = validate_threshold_policy(threshold_policy.as_dict())
    evaluation_mode = validate_evaluation_mode(
        evaluation_mode, threshold_policy
    )
    with model.collect_calls() as collector:
        return _run_d2l_context_substitution(
            input_payload,
            model,
            candidate_target_ids=candidate_target_ids,
            include_target_roles=include_target_roles,
            call_collector=collector,
            threshold_policy=threshold_policy,
            evaluation_mode=evaluation_mode,
        )


def _run_d2l_context_substitution(
    input_payload: Mapping[str, Any],
    model: FailoverStructuredModel,
    *,
    candidate_target_ids: Sequence[str] | None,
    include_target_roles: Sequence[str],
    call_collector: ProviderCallCollector,
    threshold_policy: ContextThresholdPolicy,
    evaluation_mode: str,
) -> dict[str, Any]:
    input_doc = normalize_context_substitution_input(input_payload)
    selection_contract = input_doc["selection_contract"]
    allowed_roles = frozenset(include_target_roles)
    unknown_roles = sorted(allowed_roles - TARGET_ROLES)
    if unknown_roles:
        raise ValueError(f"unknown target roles: {', '.join(unknown_roles)}")
    requested_ids = (
        frozenset(candidate_target_ids)
        if candidate_target_ids is not None
        else None
    )
    if requested_ids is not None and not requested_ids:
        raise ValueError("candidate_target_ids must not be empty")
    all_target_ids = {
        target["candidate_target_id"]
        for term in input_doc["terms"]
        for target in term["candidate_targets"]
    }
    if requested_ids is not None:
        foreign = sorted(requested_ids - all_target_ids)
        if foreign:
            raise ContractValidationError(
                "foreign_target",
                "$.candidate_target_ids",
                f"unknown candidate target IDs: {', '.join(foreign)}",
            )

    candidates: list[dict[str, Any]] = []
    term_by_id = {term["term_id"]: term for term in input_doc["terms"]}
    for term in input_doc["terms"]:
        selected_targets = [
            target
            for target in term["candidate_targets"]
            if (
                target["candidate_target_id"] in requested_ids
                if requested_ids is not None
                else target["role"] in allowed_roles
            )
        ]
        if not selected_targets:
            continue
        if term["sense_contract"]["definition_review_status"] == "INVALID":
            for target in selected_targets:
                candidates.append(
                    _invalid_sense_candidate(
                        term=term,
                        target=target,
                        threshold_policy=threshold_policy,
                    )
                )
            continue
        selection = _classify_and_select_contexts(
            model=model,
            term=term,
            selection_contract=selection_contract,
        )
        for target in selected_targets:
            candidates.append(
                _evaluate_candidate_primary(
                    model=model,
                    term=term,
                    target=target,
                    selection=selection,
                    threshold_policy=threshold_policy,
                )
            )
    if not candidates:
        raise ContractValidationError(
            "missing_candidate",
            "$.terms",
            "no candidate target matched the requested selection",
        )

    initial_close_ids = _close_margin_candidate_ids(
        candidates,
        close_margin=threshold_policy.pairwise_close_margin,
    )
    for candidate in candidates:
        if needs_second_judge(
            candidate,
            close_candidate_margin=(
                candidate["candidate_id"] in initial_close_ids
            ),
            threshold_policy=threshold_policy,
        ):
            _apply_second_judge(
                model=model,
                candidate=candidate,
                input_term=term_by_id[candidate["term_id"]],
                threshold_policy=threshold_policy,
            )

    pairwise_observations = run_pairwise_tiebreakers(
        model=model,
        candidates=candidates,
        terms_by_id=term_by_id,
        close_margin=threshold_policy.pairwise_close_margin,
    )
    unavailable_candidate_ids = {
        candidate_id
        for row in pairwise_observations
        if row["status"] == "UNAVAILABLE"
        for candidate_id in (row["candidate_a_id"], row["candidate_b_id"])
    }
    for candidate in candidates:
        if candidate["candidate_id"] in unavailable_candidate_ids:
            candidate["context_flags"] = sorted(
                set(candidate["context_flags"])
                | {"PAIRWISE_TIEBREAKER_UNAVAILABLE"}
            )
            _refresh_candidate_evidence(candidate, threshold_policy)
        candidate["provenance"] = build_candidate_provenance(
            candidate, pairwise_observations
        )

    candidates.sort(key=lambda row: (row["term_id"], row["candidate_id"]))
    route_order = [route.route_id for route in model.routes]
    provider_attempts = list(call_collector.attempted_calls)
    payload = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "input_sha256": input_doc["integrity"]["input_sha256"],
        "input_source_artifacts": input_doc["source_artifacts"],
        "execution_policy": {
            "provider_route_order": route_order,
            "selector_version": SELECTOR_VERSION,
            "selector_candidate_independent": True,
            "context_dedup_policy_version": CONTEXT_DEDUP_POLICY_VERSION,
            "trial_translator_version": TRIAL_TRANSLATOR_VERSION,
            "trial_quality_gate_version": TRIAL_QUALITY_GATE_VERSION,
            "judge_version": JUDGE_VERSION,
            "contrastive_judge_version": CONTRASTIVE_JUDGE_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "aggregation_version": AGGREGATION_VERSION,
            "evaluation_mode": evaluation_mode,
            "threshold_policy": threshold_policy.as_dict(),
            "application_contract_version": APPLICATION_CONTRACT_VERSION,
            "support_set_version": SUPPORT_SET_VERSION,
            "ood_policy_version": OOD_POLICY_VERSION,
            "same_sense_target_count": 5,
            "same_sense_minimum_count": 3,
            "contrastive_target_count": 2,
            "contrastive_minimum_count": 1,
            "trial_retry_limit": 1,
            "similarity_threshold": 0.82,
            "second_judge_policy": "conditional_independent_route_v2",
            "pairwise_tiebreaker_version": PAIRWISE_VERSION,
            "pairwise_policy": (
                "close_normalized_C_margin_lt_"
                f"{threshold_policy.pairwise_close_margin}_v2"
            ),
            "provider_failover_policy": (
                "transport_quota_or_structural_invalid_only_v2"
            ),
            "final_decision_owner": "GLOBAL_TERMINOLOGY_VALIDATOR",
            "selector_mode": selection_contract["selector_mode"],
            "selector_authority_status": selection_contract["authority_status"],
            "dataset_manifest_sha256": selection_contract[
                "dataset_manifest_sha256"
            ],
            "parent_dataset_manifest_sha256": selection_contract[
                "parent_dataset_manifest_sha256"
            ],
            "review_artifact_ref": selection_contract["review_artifact_ref"],
            "review_artifact_sha256": selection_contract[
                "review_artifact_sha256"
            ],
            "effective_sense_contract_ref": selection_contract[
                "effective_sense_contract_ref"
            ],
            "effective_sense_contract_sha256": selection_contract[
                "effective_sense_contract_sha256"
            ],
            "raw_response_ledger_policy": model.raw_response_ledger_policy,
        },
        "provider_attempts": provider_attempts,
        "usage": context_usage_summary(
            provider_attempts, route_order=route_order
        ),
        "candidates": candidates,
        "pairwise_observations": pairwise_observations,
        "integrity": {"run_sha256": "0" * 64},
    }
    from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.run import (
        seal_context_substitution_run,
    )

    return seal_context_substitution_run(payload)


def needs_second_judge(
    candidate: Mapping[str, Any],
    *,
    close_candidate_margin: bool,
    threshold_policy: ContextThresholdPolicy,
) -> bool:
    evidence = candidate["contextual_evidence"]
    score = evidence["C"]
    near_demo_threshold = score is not None and any(
        abs(float(score) - threshold)
        <= threshold_policy.second_judge_tolerance
        for threshold in threshold_policy.second_judge_thresholds
    )
    return bool(
        near_demo_threshold
        or evidence["minor_count"]
        or evidence["fail_count"]
        or (
            evidence["score_range"] is not None
            and evidence["score_range"] >= 4
        )
        or any(
            row["result"] in {"SEPARATE_SENSE_REQUIRED", "AMBIGUOUS"}
            for row in candidate["contrastive_results"]
        )
        or any(
            str(row["reason"]).startswith("judgeability:")
            for row in candidate["excluded_contexts"]
        )
        or close_candidate_margin
    )


def context_usage_summary(
    attempts: Sequence[Mapping[str, Any]],
    *,
    route_order: Sequence[str],
) -> dict[str, Any]:
    by_route: dict[str, dict[str, int]] = {}
    for route_id in route_order:
        rows = [
            row for row in attempts if row["provider_route_id"] == route_id
        ]
        by_route[route_id] = {
            "attempt_count": len(rows),
            "accepted_count": sum(row["accepted"] is True for row in rows),
            "rejected_count": sum(row["accepted"] is False for row in rows),
            "input_tokens": sum(int(row["input_tokens"]) for row in rows),
            "output_tokens": sum(int(row["output_tokens"]) for row in rows),
            "reasoning_tokens": sum(
                int(row["reasoning_tokens"]) for row in rows
            ),
            "total_tokens": sum(int(row["total_tokens"]) for row in rows),
        }
    return {
        "attempt_count": len(attempts),
        "accepted_count": sum(row["accepted"] is True for row in attempts),
        "rejected_count": sum(row["accepted"] is False for row in attempts),
        "input_tokens": sum(int(row["input_tokens"]) for row in attempts),
        "output_tokens": sum(int(row["output_tokens"]) for row in attempts),
        "reasoning_tokens": sum(
            int(row["reasoning_tokens"]) for row in attempts
        ),
        "total_tokens": sum(int(row["total_tokens"]) for row in attempts),
        "by_route": by_route,
    }


def _classify_and_select_contexts(
    *,
    model: FailoverStructuredModel,
    term: Mapping[str, Any],
    selection_contract: Mapping[str, Any],
) -> dict[str, Any]:
    contexts = list(term["contexts"])
    if selection_contract["selector_mode"] == "FROZEN_HUMAN_REVIEWED_SELECTION":
        annotations = []
        for context in contexts:
            reviewed = context.get("reviewed_selection")
            if not isinstance(reviewed, Mapping):
                raise ContractValidationError(
                    "selection_binding",
                    f"$.terms[{term['term_id']}].contexts",
                    "frozen selection is missing for a supplied context",
                )
            annotations.append(
                validate_selector_annotation(
                    {
                        "context_id": context_identity(context),
                        "sense_relation": reviewed["sense_relation"],
                        "context_type": reviewed["context_type"],
                        "judgeability": reviewed["judgeability"],
                        "reason": reviewed["reason"],
                    },
                    path=(
                        f"$.terms[{term['term_id']}].contexts"
                        f"[{context_identity(context)}].reviewed_selection"
                    ),
                )
            )
        selector = validate_selector(
            {
                "term_id": term["term_id"],
                "sense_id": term["sense_id"],
                "scope_id": term["scope_id"],
                "annotations": annotations,
            },
            term=term,
            contexts=contexts,
        )
        provenance = None
    else:
        source_profile = selector_term_profile(term)
        selector, provenance = model.call(
            role="context_selector",
            prompt_version=SELECTOR_VERSION,
            system_prompt=SELECTOR_SYSTEM_PROMPT,
            payload={
                "term": source_profile,
                "contexts": [
                    selector_context_payload(context) for context in contexts
                ],
                "required_same_sense_types": list(
                    REQUIRED_SAME_SENSE_CONTEXT_TYPES
                ),
                "selection_note": (
                    "Classify every source context without candidate wording. "
                    "Deterministic code selects the diverse subset."
                ),
            },
            response_schema=selector_schema(),
            validator=lambda value: validate_selector(
                value, term=term, contexts=contexts
            ),
            tag=f"selector:{term['term_id']}",
            max_output_tokens=4_096,
        )
    selected, replacements, contrastive = select_classified_contexts(
        contexts=contexts,
        annotations=selector["annotations"],
    )
    return {
        "annotations": selector["annotations"],
        "provenance": provenance,
        "selector_context_sources": [
            {
                "context_id": context_identity(context),
                "source_sha256": context["source_text_sha256"],
                "source_provenance": source_provenance_from_context(context),
            }
            for context in contexts
        ],
        "same_sense": selected,
        "replacements": replacements,
        "contrastive": contrastive,
        "missing_same_sense_context_types": missing_required_context_types(
            selected
        ),
    }


def _trial_payload(
    *,
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    attempt: int,
) -> dict[str, Any]:
    return {
        "context_id": context_identity(context),
        "source_sentence": context["source_text"],
        "source_sha256": context["source_text_sha256"],
        "source_provenance": source_provenance_from_context(context),
        "candidate_id": candidate["candidate_id"],
        "source_term": candidate["source_term"],
        "candidate_translation": candidate["candidate_translation"],
        "sense_id": candidate["sense_id"],
        "scope_id": candidate["scope_id"],
        "sense_contract": candidate["sense_contract"],
        "part_of_speech": candidate["part_of_speech"],
        "attempt": attempt,
        "candidate_policy": {
            "must_use_exact_literal": True,
            "allow_expansion": True,
            "expansion_must_be_reported": True,
        },
    }


def _run_trial_attempt(
    *,
    model: FailoverStructuredModel,
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    attempt: int,
) -> dict[str, Any]:
    trial, trial_provenance = model.call(
        role="trial_translator",
        prompt_version=TRIAL_TRANSLATOR_VERSION,
        system_prompt=TRIAL_SYSTEM_PROMPT,
        payload=_trial_payload(
            candidate=candidate, context=context, attempt=attempt
        ),
        response_schema=trial_schema(),
        validator=lambda value: validate_trial(
            value,
            context_id=context_identity(context),
            candidate_id=candidate["candidate_id"],
        ),
        tag=(
            f"trial:{candidate['candidate_id']}:"
            f"{context_identity(context)}:{attempt}"
        ),
        max_output_tokens=4_096,
    )
    gate, gate_provenance = model.call(
        role="trial_translation_quality_gate",
        prompt_version=TRIAL_QUALITY_GATE_VERSION,
        system_prompt=TRIAL_GATE_SYSTEM_PROMPT,
        payload={
            "candidate": _candidate_model_profile(candidate),
            "source_context": selector_context_payload(context),
            "trial": trial,
            "audit_boundary": (
                "Classify translator defects separately from candidate quality."
            ),
        },
        response_schema=trial_gate_schema(),
        validator=lambda value: validate_trial_gate(
            value,
            context_id=context_identity(context),
            candidate_id=candidate["candidate_id"],
        ),
        tag=(
            f"trial-gate:{candidate['candidate_id']}:"
            f"{context_identity(context)}:{attempt}"
        ),
        max_output_tokens=2_048,
    )
    local_literal_match, observed_surface = trial_surface_binding(
        canonical_target=candidate["candidate_translation"],
        trial=trial,
    )
    effective_status = gate["trial_status"]
    if effective_status == "VALID" and not local_literal_match:
        effective_status = "INVALID_CANDIDATE_USAGE"
    return {
        "attempt": attempt,
        "trial": trial,
        "trial_provenance": trial_provenance,
        "gate": gate,
        "gate_provenance": gate_provenance,
        "local_candidate_literal_match": local_literal_match,
        "observed_candidate_surface": observed_surface,
        "effective_trial_status": effective_status,
    }


def _run_context_judge(
    *,
    model: FailoverStructuredModel,
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    trial: Mapping[str, Any],
    excluded_routes: Iterable[str] = (),
    excluded_independence_groups: Iterable[str] = (),
    excluded_model_families: Iterable[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    return model.call(
        role="context_judge",
        prompt_version=JUDGE_VERSION,
        system_prompt=CONTEXT_JUDGE_SYSTEM_PROMPT,
        payload={
            "candidate": _candidate_model_profile(candidate),
            "source_context": selector_context_payload(context),
            "trial_translation": trial["trial_translation"],
            "rubric": {
                "semantic_equivalence": 4,
                "domain_sense_fit": 2,
                "collocation_naturalness": 2,
                "grammatical_fit": 1,
                "no_candidate_induced_distortion": 1,
            },
            "forbidden_outputs": [
                "total",
                "label",
                "final_glossary_decision",
                "glossary_winner",
                "recommendation",
                "probability",
                "confidence_percentage",
            ],
        },
        response_schema=context_judge_schema(),
        validator=lambda value: validate_context_judge(
            value,
            context_id=context_identity(context),
            candidate_id=candidate["candidate_id"],
        ),
        tag=(
            f"context-judge:{candidate['candidate_id']}:"
            f"{context_identity(context)}"
        ),
        max_output_tokens=3_072,
        excluded_routes=excluded_routes,
        excluded_independence_groups=excluded_independence_groups,
        excluded_model_families=excluded_model_families,
    )


def _run_contrastive_tests(
    *,
    model: FailoverStructuredModel,
    candidate: Mapping[str, Any],
    contexts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for context in contexts:
        tested_sense_id = "other_" + stable_digest(
            candidate["sense_id"], context_identity(context)
        )[:20]
        result, provenance = model.call(
            role="contrastive_sense_judge",
            prompt_version=CONTRASTIVE_JUDGE_VERSION,
            system_prompt=CONTRASTIVE_SYSTEM_PROMPT,
            payload={
                "candidate": _candidate_model_profile(candidate),
                "contrastive_context": selector_context_payload(context),
                "tested_sense_id": tested_sense_id,
                "boundary_note": (
                    "This context never contributes to normalized C."
                ),
            },
            response_schema=contrastive_schema(),
            validator=lambda value, context_id=context_identity(
                context
            ), sense_id=tested_sense_id: validate_contrastive(
                value,
                context_id=context_id,
                candidate_id=candidate["candidate_id"],
                tested_sense_id=sense_id,
            ),
            tag=(
                f"contrastive:{candidate['candidate_id']}:"
                f"{context_identity(context)}"
            ),
            max_output_tokens=2_048,
        )
        results.append(
            {
                **result,
                "source_provenance": source_provenance_from_context(context),
                "provenance": provenance,
            }
        )
    return results


def _evaluate_candidate_primary(
    *,
    model: FailoverStructuredModel,
    term: Mapping[str, Any],
    target: Mapping[str, Any],
    selection: Mapping[str, Any],
    threshold_policy: ContextThresholdPolicy,
) -> dict[str, Any]:
    profile = candidate_profile(term, target)
    queue = [*selection["same_sense"], *selection["replacements"]]
    context_results: list[dict[str, Any]] = []
    excluded_contexts: list[dict[str, Any]] = []
    attempted_context_ids: list[str] = []
    for context in queue:
        if len(context_results) >= 5:
            break
        attempted_context_ids.append(context_identity(context))
        trial_attempts: list[dict[str, Any]] = []
        for attempt in (1, 2):
            trial_attempts.append(
                _run_trial_attempt(
                    model=model,
                    candidate=profile,
                    context=context,
                    attempt=attempt,
                )
            )
            if trial_attempts[-1]["effective_trial_status"] == "VALID":
                break
        valid_attempt = next(
            (
                row
                for row in trial_attempts
                if row["effective_trial_status"] == "VALID"
            ),
            None,
        )
        if valid_attempt is None:
            excluded_contexts.append(
                {
                    "context_id": context_identity(context),
                    "source_provenance": source_provenance_from_context(
                        context
                    ),
                    "reason": "trial_invalid_after_one_retry",
                    "trial_attempts": trial_attempts,
                    "judge_output": None,
                    "judge_provenance": None,
                }
            )
            continue
        judge, judge_provenance = _run_context_judge(
            model=model,
            candidate=profile,
            context=context,
            trial=valid_attempt["trial"],
        )
        if judge["judgeability"] != "JUDGEABLE":
            excluded_contexts.append(
                {
                    "context_id": context_identity(context),
                    "source_provenance": source_provenance_from_context(
                        context
                    ),
                    "reason": f"judgeability:{judge['judgeability']}",
                    "trial_attempts": trial_attempts,
                    "judge_output": judge,
                    "judge_provenance": judge_provenance,
                }
            )
            continue
        raw_score, label, hard_flags = compute_context_result(judge)
        context_results.append(
            {
                "context_id": context_identity(context),
                "context_type": context["_annotation"]["context_type"],
                "source_sha256": context["source_text_sha256"],
                "source_provenance": source_provenance_from_context(context),
                "trial_attempts": trial_attempts,
                "trial_translation": valid_attempt["trial"][
                    "trial_translation"
                ],
                "primary_judge": {
                    "output": judge,
                    "provenance": judge_provenance,
                },
                "secondary_judge": None,
                "raw_score": raw_score,
                "label": label,
                "local_hard_flags": hard_flags,
                "reason": judge["reason"],
            }
        )
    contrastive_results = _run_contrastive_tests(
        model=model,
        candidate=profile,
        contexts=selection["contrastive"],
    )
    flags: set[str] = set()
    if term["sense_contract"]["definition_review_status"] == "UNVERIFIED":
        flags.add("SENSE_DEFINITION_UNVERIFIED")
    if len(context_results) < 3:
        flags.add("INSUFFICIENT_VALID_SAME_SENSE_CONTEXTS")
    if not selection["contrastive"]:
        flags.add("MISSING_CONTRASTIVE_CONTEXT")
    if selection["missing_same_sense_context_types"]:
        flags.add("INCOMPLETE_CONTEXT_TYPE_COVERAGE")
    candidate = {
        "term_id": term["term_id"],
        "candidate_id": profile["candidate_id"],
        "target_role": target["role"],
        "source_term": profile["source_term"],
        "candidate_translation": profile["candidate_translation"],
        "sense_id": profile["sense_id"],
        "scope_id": profile["scope_id"],
        "sense_contract": profile["sense_contract"],
        "part_of_speech": profile["part_of_speech"],
        "source_occurrences": profile["source_occurrences"],
        "candidate_generation": profile["candidate_generation"],
        "selector_annotations": list(selection["annotations"]),
        "selector_provenance": (
            None
            if selection["provenance"] is None
            else dict(selection["provenance"])
        ),
        "selector_context_sources": list(
            selection["selector_context_sources"]
        ),
        "selected_same_sense_context_ids": attempted_context_ids,
        "selected_contrastive_context_ids": [
            context_identity(context) for context in selection["contrastive"]
        ],
        "missing_same_sense_context_types": list(
            selection["missing_same_sense_context_types"]
        ),
        "context_results": context_results,
        "excluded_contexts": excluded_contexts,
        "contrastive_results": contrastive_results,
        "contextual_evidence": {},
        "context_flags": sorted(flags),
        "sense_boundary_observations": [],
        "application_contract": {},
        "certificate_support_set": {},
        "second_judge_invoked": False,
        "judge_disagreement": False,
        "judge_independence": {
            "status": "NOT_INVOKED",
            "observations": [],
        },
        "recommendation_to_global_validator": "REQUIRES_GLOBAL_REVIEW",
        "final_glossary_decision": None,
        "provenance": {},
    }
    _refresh_candidate_evidence(candidate, threshold_policy)
    return candidate


def _invalid_sense_candidate(
    *,
    term: Mapping[str, Any],
    target: Mapping[str, Any],
    threshold_policy: ContextThresholdPolicy,
) -> dict[str, Any]:
    profile = candidate_profile(term, target)
    candidate = {
        "term_id": term["term_id"],
        "candidate_id": profile["candidate_id"],
        "target_role": target["role"],
        "source_term": profile["source_term"],
        "candidate_translation": profile["candidate_translation"],
        "sense_id": profile["sense_id"],
        "scope_id": profile["scope_id"],
        "sense_contract": profile["sense_contract"],
        "part_of_speech": profile["part_of_speech"],
        "source_occurrences": profile["source_occurrences"],
        "candidate_generation": profile["candidate_generation"],
        "selector_annotations": [],
        "selector_provenance": None,
        "selector_context_sources": [],
        "selected_same_sense_context_ids": [],
        "selected_contrastive_context_ids": [],
        "missing_same_sense_context_types": list(
            REQUIRED_SAME_SENSE_CONTEXT_TYPES
        ),
        "context_results": [],
        "excluded_contexts": [],
        "contrastive_results": [],
        "contextual_evidence": {},
        "context_flags": [
            "SENSE_DEFINITION_INVALID",
            "INSUFFICIENT_VALID_SAME_SENSE_CONTEXTS",
            "MISSING_CONTRASTIVE_CONTEXT",
            "INCOMPLETE_CONTEXT_TYPE_COVERAGE",
        ],
        "sense_boundary_observations": [],
        "application_contract": {},
        "certificate_support_set": {},
        "second_judge_invoked": False,
        "judge_disagreement": False,
        "judge_independence": {
            "status": "NOT_APPLICABLE_INVALID_SENSE",
            "observations": [],
        },
        "recommendation_to_global_validator": "REQUIRES_GLOBAL_REVIEW",
        "final_glossary_decision": None,
        "provenance": {},
    }
    _refresh_candidate_evidence(candidate, threshold_policy)
    return candidate


def _apply_second_judge(
    *,
    model: FailoverStructuredModel,
    candidate: dict[str, Any],
    input_term: Mapping[str, Any],
    threshold_policy: ContextThresholdPolicy,
) -> None:
    by_context_id = {
        context_identity(context): context
        for context in input_term["contexts"]
    }
    candidate["second_judge_invoked"] = True
    disagreement = False
    unavailable = False
    independence_observations: list[dict[str, str]] = []
    for row in candidate["context_results"]:
        context = by_context_id[row["context_id"]]
        primary = row["primary_judge"]
        try:
            secondary_output, secondary_provenance = _run_context_judge(
                model=model,
                candidate=candidate,
                context=context,
                trial={"trial_translation": row["trial_translation"]},
                excluded_routes={
                    primary["provenance"]["provider_route_id"]
                },
                excluded_model_families={
                    primary["provenance"]["model_family"]
                },
                excluded_independence_groups={
                    primary["provenance"]["independence_group"]
                },
            )
        except ContextExecutionError:
            unavailable = True
            continue
        row["secondary_judge"] = {
            "output": secondary_output,
            "provenance": secondary_provenance,
        }
        independence_observations.append(
            _judge_independence_observation(
                context_id=row["context_id"],
                primary=primary["provenance"],
                secondary=secondary_provenance,
            )
        )
        if secondary_output["judgeability"] != "JUDGEABLE":
            disagreement = True
            continue
        secondary_score, secondary_label, secondary_flags = (
            compute_context_result(secondary_output)
        )
        merged_label, significant = merge_judge_labels(
            row["label"], secondary_label
        )
        row["raw_score"] = min(row["raw_score"], secondary_score)
        row["label"] = merged_label
        row["local_hard_flags"] = sorted(
            set(row["local_hard_flags"]) | set(secondary_flags)
        )
        if significant:
            disagreement = True
    flags = set(candidate["context_flags"])
    if unavailable:
        flags.add("SECOND_JUDGE_UNAVAILABLE")
    candidate["judge_disagreement"] = disagreement
    if disagreement:
        flags.add("JUDGE_DISAGREEMENT")
    candidate["context_flags"] = sorted(flags)
    candidate["judge_independence"] = {
        "status": _judge_independence_status(
            observations=independence_observations,
            unavailable=unavailable,
        ),
        "observations": independence_observations,
    }
    _refresh_candidate_evidence(candidate, threshold_policy)


def _refresh_candidate_evidence(
    candidate: dict[str, Any], threshold_policy: ContextThresholdPolicy
) -> None:
    flags = set(candidate["context_flags"])
    flags -= LOCAL_HARD_FLAGS
    flags.update(
        flag
        for row in candidate["context_results"]
        for flag in row["local_hard_flags"]
    )
    if len(candidate["context_results"]) < 3:
        flags.add("INSUFFICIENT_VALID_SAME_SENSE_CONTEXTS")
    else:
        flags.discard("INSUFFICIENT_VALID_SAME_SENSE_CONTEXTS")
    if candidate["selected_contrastive_context_ids"]:
        flags.discard("MISSING_CONTRASTIVE_CONTEXT")
    else:
        flags.add("MISSING_CONTRASTIVE_CONTEXT")
    if candidate["missing_same_sense_context_types"]:
        flags.add("INCOMPLETE_CONTEXT_TYPE_COVERAGE")
    else:
        flags.discard("INCOMPLETE_CONTEXT_TYPE_COVERAGE")
    candidate["context_flags"] = sorted(flags)
    candidate["contextual_evidence"] = aggregate_contextual_evidence(
        candidate["context_results"],
        invalid_context_count=len(candidate["excluded_contexts"]),
        context_flags=candidate["context_flags"],
        contrastive_results=candidate["contrastive_results"],
        threshold_policy=threshold_policy,
    )
    candidate["sense_boundary_observations"] = [
        {
            "contrastive_context_id": row["context_id"],
            "contrastive_sense_id": row["tested_sense_id"],
            "result": row["result"],
            "reason": row["reason"],
        }
        for row in candidate["contrastive_results"]
    ]
    candidate["application_contract"] = build_application_contract(
        canonical_target=candidate["candidate_translation"],
        context_results=candidate["context_results"],
    )
    candidate["certificate_support_set"] = (
        build_certificate_support_set(
            candidate["context_results"], candidate["contrastive_results"]
        )
    )
    candidate["recommendation_to_global_validator"] = global_recommendation(
        contextual_status_value=candidate["contextual_evidence"]["status"],
        context_flags=candidate["context_flags"],
        threshold_policy_status=threshold_policy.policy_status,
    )
    candidate["final_glossary_decision"] = None
    candidate["provenance"] = build_candidate_provenance(candidate)


def _judge_independence_observation(
    *,
    context_id: str,
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
) -> dict[str, str]:
    level = (
        "CROSS_MODEL_FAMILY"
        if primary["model_family"] != secondary["model_family"]
        else "SAME_MODEL_FAMILY"
    )
    return {
        "context_id": context_id,
        "judge_1_model_id": str(primary["model_id"]),
        "judge_1_model_family": str(primary["model_family"]),
        "judge_1_independence_group": str(
            primary["independence_group"]
        ),
        "judge_1_provider_route_id": str(primary["provider_route_id"]),
        "judge_2_model_id": str(secondary["model_id"]),
        "judge_2_model_family": str(secondary["model_family"]),
        "judge_2_independence_group": str(
            secondary["independence_group"]
        ),
        "judge_2_provider_route_id": str(secondary["provider_route_id"]),
        "independence_level": level,
    }


def _judge_independence_status(
    *,
    observations: Sequence[Mapping[str, str]],
    unavailable: bool,
) -> str:
    if unavailable and not observations:
        return "REQUESTED_UNAVAILABLE"
    levels = {row["independence_level"] for row in observations}
    if unavailable:
        return "PARTIAL_WITH_UNAVAILABLE_CONTEXTS"
    if levels == {"CROSS_MODEL_FAMILY"}:
        return "CROSS_MODEL_FAMILY"
    if "SAME_MODEL_FAMILY" in levels:
        return "LOW_JUDGE_INDEPENDENCE"
    if len(levels) == 1:
        return next(iter(levels))
    if levels:
        return "MIXED_INDEPENDENCE"
    return "NOT_INVOKED"


def _candidate_model_profile(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "candidate_id",
            "source_term",
            "candidate_translation",
            "sense_id",
            "scope_id",
            "sense_contract",
            "part_of_speech",
            "source_occurrences",
            "candidate_generation",
        )
    }


def _close_margin_candidate_ids(
    candidates: Sequence[Mapping[str, Any]],
    *,
    close_margin: float,
) -> set[str]:
    result: set[str] = set()
    for candidate_a, candidate_b, _margin in close_candidate_pairs(
        candidates, close_margin=close_margin
    ):
        result.update({candidate_a["candidate_id"], candidate_b["candidate_id"]})
    return result

