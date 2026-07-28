from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    AGGREGATION_VERSION,
    JUDGE_VERSION,
    PROVENANCE_VERSION,
    RUBRIC_VERSION,
    SELECTOR_VERSION,
    SUPPORT_SET_VERSION,
    TRIAL_TRANSLATOR_VERSION,
    sha256_text,
)


def build_candidate_provenance(
    candidate: Mapping[str, Any],
    pairwise_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    relevant_pairwise = sorted(
        (
            row
            for row in pairwise_records
            if candidate["candidate_id"]
            in {row["candidate_a_id"], row["candidate_b_id"]}
        ),
        key=lambda row: str(row["observation_id"]),
    )
    provenances = list(
        candidate_provider_provenances(candidate, relevant_pairwise)
    )
    selector_source_hashes = sorted(
        {
            str(row["source_sha256"])
            for row in candidate["selector_context_sources"]
        }
    )
    accepted_source_hashes = sorted(
        {str(row["source_sha256"]) for row in candidate["context_results"]}
    )
    excluded_source_hashes = sorted(
        {
            str(row["source_provenance"]["source_hash"])
            for row in candidate["excluded_contexts"]
        }
    )
    contrastive_source_hashes = sorted(
        {
            str(row["source_provenance"]["source_hash"])
            for row in candidate["contrastive_results"]
        }
    )
    attempted_source_hashes = sorted(
        set(selector_source_hashes)
        | set(accepted_source_hashes)
        | set(excluded_source_hashes)
        | set(contrastive_source_hashes)
    )
    prompt_hashes_by_role: dict[str, list[str]] = {}
    for provenance in provenances:
        prompt_hashes_by_role.setdefault(
            str(provenance["role"]), []
        ).append(str(provenance["prompt_sha256"]))
    prompt_hashes_by_role = {
        role: sorted(set(values))
        for role, values in sorted(prompt_hashes_by_role.items())
    }
    model_ids = sorted(
        {str(provenance["model_id"]) for provenance in provenances}
    )
    response_hashes = sorted(
        {
            str(provenance["response_sha256"])
            for provenance in provenances
            if provenance.get("response_sha256") is not None
        }
    )
    pairwise_ids = [str(row["observation_id"]) for row in relevant_pairwise]
    evidence_package = _candidate_evidence_package(
        candidate, pairwise_observation_ids=pairwise_ids
    )
    evidence_package_sha256 = sha256_text(
        json.dumps(
            evidence_package,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return {
        "cst_evidence_id": "cst_ev_" + evidence_package_sha256[:24],
        "evidence_package_sha256": evidence_package_sha256,
        "provenance_version": PROVENANCE_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "aggregation_policy_version": AGGREGATION_VERSION,
        "context_selector_version": SELECTOR_VERSION,
        "trial_translator_version": TRIAL_TRANSLATOR_VERSION,
        "judge_version": JUDGE_VERSION,
        "attempted_source_hashes": attempted_source_hashes,
        "selector_source_hashes": selector_source_hashes,
        "accepted_source_hashes": accepted_source_hashes,
        "excluded_source_hashes": excluded_source_hashes,
        "contrastive_source_hashes": contrastive_source_hashes,
        "model_ids": model_ids,
        "prompt_hashes_by_role": prompt_hashes_by_role,
        "response_hashes": response_hashes,
        "pairwise_observation_ids": pairwise_ids,
        "candidate_generation": dict(candidate["candidate_generation"]),
        "judge_independence_status": candidate["judge_independence"][
            "status"
        ],
    }


def candidate_provider_provenances(
    candidate: Mapping[str, Any],
    pairwise_records: Sequence[Mapping[str, Any]] = (),
) -> Iterable[Mapping[str, Any]]:
    if candidate["selector_provenance"] is not None:
        yield candidate["selector_provenance"]
    for row in candidate["context_results"]:
        for attempt in row["trial_attempts"]:
            yield attempt["trial_provenance"]
            yield attempt["gate_provenance"]
        yield row["primary_judge"]["provenance"]
        if row["secondary_judge"] is not None:
            yield row["secondary_judge"]["provenance"]
    for row in candidate["excluded_contexts"]:
        for attempt in row["trial_attempts"]:
            yield attempt["trial_provenance"]
            yield attempt["gate_provenance"]
        if row["judge_provenance"] is not None:
            yield row["judge_provenance"]
    for row in candidate["contrastive_results"]:
        yield row["provenance"]
    for row in pairwise_records:
        if row["status"] == "COMPLETED":
            yield row["provenance"]


def _candidate_evidence_package(
    candidate: Mapping[str, Any],
    *,
    pairwise_observation_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "sense_id": candidate["sense_id"],
        "scope_id": candidate["scope_id"],
        "candidate_translation": candidate["candidate_translation"],
        "selector_source_hashes": [
            row["source_sha256"]
            for row in candidate["selector_context_sources"]
        ],
        "context_results": [
            {
                "context_id": row["context_id"],
                "source_sha256": row["source_sha256"],
                "raw_score": row["raw_score"],
                "label": row["label"],
                "local_hard_flags": row["local_hard_flags"],
                "trial_response_hashes": [
                    {
                        "trial": attempt["trial_provenance"]["response_sha256"],
                        "gate": attempt["gate_provenance"]["response_sha256"],
                        "status": attempt["effective_trial_status"],
                    }
                    for attempt in row["trial_attempts"]
                ],
                "primary_judge_response_sha256": row["primary_judge"][
                    "provenance"
                ]["response_sha256"],
                "secondary_judge_response_sha256": (
                    None
                    if row["secondary_judge"] is None
                    else row["secondary_judge"]["provenance"][
                        "response_sha256"
                    ]
                ),
            }
            for row in candidate["context_results"]
        ],
        "excluded_contexts": [
            {
                "context_id": row["context_id"],
                "source_sha256": row["source_provenance"]["source_hash"],
                "reason": row["reason"],
                "trial_response_hashes": [
                    {
                        "trial": attempt["trial_provenance"]["response_sha256"],
                        "gate": attempt["gate_provenance"]["response_sha256"],
                        "status": attempt["effective_trial_status"],
                    }
                    for attempt in row["trial_attempts"]
                ],
                "judge_response_sha256": (
                    None
                    if row["judge_provenance"] is None
                    else row["judge_provenance"]["response_sha256"]
                ),
            }
            for row in candidate["excluded_contexts"]
        ],
        "contrastive_results": [
            {
                "context_id": row["context_id"],
                "source_sha256": row["source_provenance"]["source_hash"],
                "result": row["result"],
                "response_sha256": row["provenance"]["response_sha256"],
            }
            for row in candidate["contrastive_results"]
        ],
        "contextual_evidence": candidate["contextual_evidence"],
        "context_flags": candidate["context_flags"],
        "judge_independence": candidate["judge_independence"],
        "application_contract": candidate["application_contract"],
        "support_set_version": SUPPORT_SET_VERSION,
        "pairwise_observation_ids": list(pairwise_observation_ids),
    }


