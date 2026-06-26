from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


PROMPT_VERSION = "d2l_occurrence_adjudicator_v1_review_gated"
RESULT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "occurrence_adjudication",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "occurrence_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["localized", "omitted", "ambiguous", "not_found"],
                },
                "target_quote": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
            },
            "required": ["occurrence_id", "status", "target_quote", "confidence", "reason"],
        },
    },
}


@dataclass(frozen=True)
class AdjudicationInput:
    occurrence_id: str
    source_term: str
    source_sentence: str
    target_region: str
    candidate_quotes: tuple[str, ...] = ()


def build_messages(item: AdjudicationInput) -> list[dict[str, str]]:
    """Build the review-gated T3 prompt. This function does not call an LLM."""

    system = (
        "You are an occurrence-level localization adjudicator for an English to "
        "Vietnamese translation evaluation.\n\n"
        "Your job is not to judge style or translate new text. Your job is to find "
        "whether the marked English term has a concrete rendering inside the provided "
        "Vietnamese target region.\n\n"
        "Rules:\n"
        "1. Return status localized only when target_quote is copied verbatim from "
        "TARGET_REGION and refers to the marked source term occurrence.\n"
        "2. Return omitted when the concept is genuinely not rendered in the target "
        "region.\n"
        "3. Return ambiguous when several spans are plausible and the region is not "
        "enough to choose one.\n"
        "4. Return not_found when no plausible span can be found in the target region.\n"
        "5. Do not use registry variants as the answer unless they actually appear in "
        "the target region and correspond to this occurrence.\n"
        "6. Prefer low confidence over guessing. Confidence is audited against human gold.\n\n"
        "Output must conform exactly to the JSON schema."
    )
    payload = {
        "prompt_version": PROMPT_VERSION,
        "occurrence_id": item.occurrence_id,
        "source_term": item.source_term,
        "source_sentence": item.source_sentence,
        "target_region": item.target_region,
        "candidate_quotes": list(item.candidate_quotes),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def validate_payload(payload: Any, occurrence_id: str, target_region: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("occurrence_id")) != occurrence_id:
        return {
            "status": "not_found",
            "target_quote": "",
            "confidence": "low",
            "reason": "invalid_payload",
        }
    status = str(payload.get("status") or "")
    if status not in {"localized", "omitted", "ambiguous", "not_found"}:
        status = "not_found"
    quote = str(payload.get("target_quote") or "")
    if status == "localized" and quote not in target_region:
        return {
            "status": "not_found",
            "target_quote": quote,
            "confidence": "low",
            "reason": "quote_not_in_target_region",
        }
    confidence = str(payload.get("confidence") or "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "status": status,
        "target_quote": quote if status == "localized" else "",
        "confidence": confidence,
        "reason": str(payload.get("reason") or ""),
    }
