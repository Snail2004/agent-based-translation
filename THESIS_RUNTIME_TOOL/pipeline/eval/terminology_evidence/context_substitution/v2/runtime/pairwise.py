from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_string,
    require_number,
    require_string,
    require_unique,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.providers.base import (
    ContextExecutionError,
    FailoverStructuredModel,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    PAIRWISE_PREFERENCES,
    sha256_text,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.selection import (
    context_identity,
    selector_context_payload,
)


PAIRWISE_VERSION = "d2l_context_pairwise_tiebreaker_v2_1"
PAIRWISE_SYSTEM_PROMPT = (
    "Compare Candidate A and Candidate B only in the supplied same-sense "
    "source contexts. Return exactly the response schema. Select "
    "CONTEXTUAL_PREFERENCE_A, CONTEXTUAL_PREFERENCE_B, or TIE. Do not compute "
    "Context Score, alter sense or scope, identify a glossary winner, or make "
    "the final glossary decision."
)
PAIRWISE_CONFIDENCE = frozenset({"LOW", "MEDIUM", "HIGH"})
PAIRWISE_CLOSE_MARGIN = 0.067


def close_candidate_pairs(
    candidates: Sequence[Mapping[str, Any]],
    *,
    close_margin: float = PAIRWISE_CLOSE_MARGIN,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], float]]:
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
    for term_id in sorted({row["term_id"] for row in candidates}):
        measured = [
            row
            for row in candidates
            if row["term_id"] == term_id
            and row["contextual_evidence"]["C"] is not None
        ]
        measured.sort(
            key=lambda row: (
                -float(row["contextual_evidence"]["C"]),
                row["candidate_id"],
            )
        )
        if len(measured) < 2:
            continue
        margin = round(
            float(measured[0]["contextual_evidence"]["C"])
            - float(measured[1]["contextual_evidence"]["C"]),
            4,
        )
        if margin < close_margin:
            pairs.append((measured[0], measured[1], margin))
    return pairs


def pairwise_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_a_id",
            "candidate_b_id",
            "preferred",
            "confidence",
            "reason",
        ],
        "properties": {
            "candidate_a_id": {"type": "string"},
            "candidate_b_id": {"type": "string"},
            "preferred": {"enum": sorted(PAIRWISE_PREFERENCES)},
            "confidence": {"enum": sorted(PAIRWISE_CONFIDENCE)},
            "reason": {"type": "string"},
        },
    }


def validate_pairwise_response(
    value: Any,
    *,
    candidate_a_id: str,
    candidate_b_id: str,
) -> dict[str, str]:
    path = "$.pairwise"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "candidate_a_id",
            "candidate_b_id",
            "preferred",
            "confidence",
            "reason",
        },
        path=path,
    )
    if (
        require_string(row["candidate_a_id"], path=f"{path}.candidate_a_id")
        != candidate_a_id
        or require_string(
            row["candidate_b_id"], path=f"{path}.candidate_b_id"
        )
        != candidate_b_id
    ):
        raise ContractValidationError(
            "pairwise_binding", path, "candidate pair drift"
        )
    return {
        "candidate_a_id": candidate_a_id,
        "candidate_b_id": candidate_b_id,
        "preferred": require_enum(
            row["preferred"],
            PAIRWISE_PREFERENCES,
            path=f"{path}.preferred",
        ),
        "confidence": require_enum(
            row["confidence"],
            PAIRWISE_CONFIDENCE,
            path=f"{path}.confidence",
        ),
        "reason": require_string(
            row["reason"], path=f"{path}.reason", maximum=2_000
        ),
    }


def run_pairwise_tiebreakers(
    *,
    model: FailoverStructuredModel,
    candidates: Sequence[Mapping[str, Any]],
    terms_by_id: Mapping[str, Mapping[str, Any]],
    close_margin: float = PAIRWISE_CLOSE_MARGIN,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for candidate_a, candidate_b, margin in close_candidate_pairs(
        candidates, close_margin=close_margin
    ):
        term = terms_by_id[candidate_a["term_id"]]
        contexts_by_id = {
            context_identity(context): context for context in term["contexts"]
        }
        common_ids = sorted(
            {
                row["context_id"] for row in candidate_a["context_results"]
            }
            & {
                row["context_id"] for row in candidate_b["context_results"]
            }
        )[:5]
        if len(common_ids) < 3:
            results.append(
                _with_observation_id({
                    "term_id": candidate_a["term_id"],
                    "candidate_a_id": candidate_a["candidate_id"],
                    "candidate_b_id": candidate_b["candidate_id"],
                    "context_ids": common_ids,
                    "score_margin": margin,
                    "status": "UNAVAILABLE",
                    "result": None,
                    "provenance": None,
                    "failure_reason": "insufficient_common_same_sense_contexts",
                })
            )
            continue
        try:
            output, provenance = model.call(
                role="pairwise_tiebreaker",
                prompt_version=PAIRWISE_VERSION,
                system_prompt=PAIRWISE_SYSTEM_PROMPT,
                payload={
                    "term_id": candidate_a["term_id"],
                    "sense_id": candidate_a["sense_id"],
                    "scope_id": candidate_a["scope_id"],
                    "candidate_a": _pairwise_candidate(candidate_a),
                    "candidate_b": _pairwise_candidate(candidate_b),
                    "same_sense_contexts": [
                        selector_context_payload(contexts_by_id[context_id])
                        for context_id in common_ids
                    ],
                    "boundary": (
                        "This is a contextual preference observation only. "
                        "It is not a glossary winner or final decision."
                    ),
                },
                response_schema=pairwise_schema(),
                validator=lambda value, a=candidate_a, b=candidate_b: (
                    validate_pairwise_response(
                        value,
                        candidate_a_id=a["candidate_id"],
                        candidate_b_id=b["candidate_id"],
                    )
                ),
                tag=(
                    f"pairwise:{candidate_a['term_id']}:"
                    f"{candidate_a['candidate_id']}:"
                    f"{candidate_b['candidate_id']}"
                ),
                max_output_tokens=2_048,
            )
            results.append(
                _with_observation_id({
                    "term_id": candidate_a["term_id"],
                    "candidate_a_id": candidate_a["candidate_id"],
                    "candidate_b_id": candidate_b["candidate_id"],
                    "context_ids": common_ids,
                    "score_margin": margin,
                    "status": "COMPLETED",
                    "result": output,
                    "provenance": provenance,
                    "failure_reason": None,
                })
            )
        except ContextExecutionError:
            results.append(
                _with_observation_id({
                    "term_id": candidate_a["term_id"],
                    "candidate_a_id": candidate_a["candidate_id"],
                    "candidate_b_id": candidate_b["candidate_id"],
                    "context_ids": common_ids,
                    "score_margin": margin,
                    "status": "UNAVAILABLE",
                    "result": None,
                    "provenance": None,
                    "failure_reason": "provider_routes_exhausted",
                })
            )
    return results


def validate_pairwise_record(
    value: Any,
    *,
    path: str,
    close_margin: float = PAIRWISE_CLOSE_MARGIN,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "observation_id",
            "term_id",
            "candidate_a_id",
            "candidate_b_id",
            "context_ids",
            "score_margin",
            "status",
            "result",
            "provenance",
            "failure_reason",
        },
        path=path,
    )
    status = require_enum(
        row["status"], {"COMPLETED", "UNAVAILABLE"}, path=f"{path}.status"
    )
    margin = require_number(
        row["score_margin"], path=f"{path}.score_margin", minimum=0
    )
    if margin >= close_margin:
        raise ContractValidationError(
            "pairwise_margin",
            f"{path}.score_margin",
            f"must be below {close_margin}",
        )
    candidate_a_id = require_string(
        row["candidate_a_id"], path=f"{path}.candidate_a_id"
    )
    candidate_b_id = require_string(
        row["candidate_b_id"], path=f"{path}.candidate_b_id"
    )
    result = row["result"]
    provenance = row["provenance"]
    context_ids = [
        require_string(child, path=f"{path}.context_ids[{index}]")
        for index, child in enumerate(
            require_list(row["context_ids"], path=f"{path}.context_ids")
        )
    ]
    require_unique(context_ids, path=f"{path}.context_ids")
    if len(context_ids) > 5:
        raise ContractValidationError(
            "pairwise_contexts",
            f"{path}.context_ids",
            "at most five same-sense contexts are allowed",
        )
    failure_reason = require_nullable_string(
        row["failure_reason"],
        path=f"{path}.failure_reason",
        maximum=500,
    )
    if status == "COMPLETED":
        if result is None or provenance is None or failure_reason is not None:
            raise ContractValidationError(
                "pairwise_status", path, "completed record fields are inconsistent"
            )
        result = validate_pairwise_response(
            result,
            candidate_a_id=candidate_a_id,
            candidate_b_id=candidate_b_id,
        )
        provenance = dict(
            require_mapping(provenance, path=f"{path}.provenance")
        )
    elif (
        result is not None
        or provenance is not None
        or not isinstance(failure_reason, str)
        or not failure_reason
    ):
        raise ContractValidationError(
            "pairwise_status", path, "unavailable record fields are inconsistent"
        )
    return {
        "observation_id": _validate_observation_id(row, path=path),
        "term_id": require_string(row["term_id"], path=f"{path}.term_id"),
        "candidate_a_id": candidate_a_id,
        "candidate_b_id": candidate_b_id,
        "context_ids": context_ids,
        "score_margin": margin,
        "status": status,
        "result": result,
        "provenance": provenance,
        "failure_reason": failure_reason,
    }


def _with_observation_id(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["observation_id"] = _pairwise_observation_id(payload)
    return payload


def _validate_observation_id(
    row: Mapping[str, Any], *, path: str
) -> str:
    observed = require_string(
        row["observation_id"], path=f"{path}.observation_id"
    )
    expected = _pairwise_observation_id(row)
    if observed != expected:
        raise ContractValidationError(
            "pairwise_observation_id",
            f"{path}.observation_id",
            "observation ID differs from the pairwise evidence",
        )
    return observed


def _pairwise_observation_id(row: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in row.items() if key != "observation_id"
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "cst_pair_" + sha256_text(rendered)[:24]


def _pairwise_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "candidate_id",
            "candidate_translation",
            "sense_id",
            "scope_id",
            "sense_contract",
            "part_of_speech",
            "candidate_generation",
        )
    }


