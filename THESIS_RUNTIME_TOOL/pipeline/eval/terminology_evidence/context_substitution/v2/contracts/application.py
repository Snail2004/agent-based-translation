from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    APPLICATION_CONTRACT_VERSION,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.surface import (
    contains_surface,
    normalized_surface,
    surfaces_equivalent,
)


def build_application_contract(
    *,
    canonical_target: str,
    context_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    canonical_key = _surface_key(canonical_target)
    observed: dict[tuple[str, str], set[str]] = defaultdict(set)
    disallowed: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in context_results:
        valid_trial = next(
            (
                attempt
                for attempt in row["trial_attempts"]
                if attempt["effective_trial_status"] == "VALID"
            ),
            None,
        )
        if valid_trial is None:
            continue
        observation = row["primary_judge"]["output"]["variant_observation"]
        context_id = str(row["context_id"])
        surface = str(
            valid_trial.get("observed_candidate_surface")
            or valid_trial["trial"]["candidate_surface_used"]
        ).strip()
        target_span = str(
            row["primary_judge"]["output"]["evidence"]["target_span"]
        )
        if not surface or not contains_surface(target_span, canonical_target):
            continue
        if not (
            surfaces_equivalent(
                str(observation["surface_used"]),
                surface,
            )
            and contains_surface(target_span, surface)
        ):
            continue
        if surface:
            status = (
                "OBSERVED_VALID"
                if row["label"] != "FAIL"
                else "PROPOSED"
            )
            observed[(surface, status)].add(context_id)
        expansion = valid_trial["trial"]["applied_expansion"]
        if (
            expansion
            and observation["requires_expansion"]
            and observation["suggested_expansion"]
            and surfaces_equivalent(
                str(observation["suggested_expansion"]),
                str(expansion),
            )
        ):
            observed[(str(expansion).strip(), status)].add(context_id)
        if row["label"] == "FAIL":
            for flag in row["local_hard_flags"]:
                if flag in {
                    "WRONG_SENSE",
                    "SEMANTIC_CONTRADICTION",
                    "CANDIDATE_INDUCED_DISTORTION",
                } and surface and _surface_key(surface) != canonical_key:
                    disallowed[(surface, flag)].add(context_id)

    canonical_contexts = sorted(
        context_id
        for (surface, status), context_ids in observed.items()
        if _surface_key(surface) == canonical_key
        and status == "OBSERVED_VALID"
        for context_id in context_ids
    )
    allowed_variants = []
    for (surface, status), context_ids in sorted(
        observed.items(), key=lambda item: (_surface_key(item[0][0]), item[0][1])
    ):
        if _surface_key(surface) == canonical_key:
            continue
        allowed_variants.append(
            {
                "surface": surface,
                "status": status,
                "context_ids": sorted(context_ids),
                "sealed": False,
            }
        )
    disallowed_variants = [
        {
            "surface": surface,
            "reason": reason,
            "context_ids": sorted(context_ids),
            "sealed": False,
        }
        for (surface, reason), context_ids in sorted(
            disallowed.items(), key=lambda item: (_surface_key(item[0][0]), item[0][1])
        )
    ]
    application_notes = [
        {
            "condition": "judge_observed_expansion_review_required",
            "recommended_form": row["surface"],
            "context_ids": list(row["context_ids"]),
        }
        for row in allowed_variants
        if row["status"] == "PROPOSED"
    ]
    return {
        "schema_version": APPLICATION_CONTRACT_VERSION,
        "canonical_target": canonical_target,
        "canonical_observed_context_ids": sorted(set(canonical_contexts)),
        "allowed_variants": allowed_variants,
        "disallowed_variants": disallowed_variants,
        "application_notes": application_notes,
        "variant_authority": "OBSERVATION_ONLY_NOT_AUTO_SEALED",
    }


def _surface_key(value: str) -> str:
    return normalized_surface(value)


