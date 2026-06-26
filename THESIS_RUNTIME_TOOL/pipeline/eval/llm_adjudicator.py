from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


PROMPT_VERSION = "d2l_locate_only_v4"
RESULT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "occurrence_location",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "occurrence_id": {"type": "string"},
                "found": {"type": "boolean"},
                "target_quote": {"type": "string"},
                "left_context": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["occurrence_id", "found", "target_quote", "left_context", "confidence"],
        },
    },
}


@dataclass(frozen=True)
class AdjudicationInput:
    occurrence_id: str
    source_term: str
    occurrence_index: int
    source_sentence: str
    target_region: str


def build_messages(item: AdjudicationInput) -> list[dict[str, str]]:
    """Build the locate-only T3 prompt. This function does not call an LLM."""

    system = (
        "You are an occurrence-level LOCATOR for an English-to-Vietnamese translation evaluation. You do exactly ONE thing: find the Vietnamese text that translates ONE specific occurrence of the marked English term. You do NOT score, judge correctness, or translate anything new.\n"
        "Input fields:\n"
        "- SOURCE_SENTENCE: the English sentence.\n"
        "- TARGET_REGION: the Vietnamese text aligned to that sentence.\n"
        "- TERM: the English term to locate.\n"
        "- OCCURRENCE_INDEX: which occurrence of TERM inside SOURCE_SENTENCE to locate (1-based, left to right). Locate exactly that occurrence.\n"
        "Output:\n"
        "- found: true if a Vietnamese rendering of this occurrence exists; false if the term is kept in English, dropped, or replaced by a pronoun (then target_quote = \"\").\n"
        "- target_quote: the Vietnamese word(s) the translator actually used for this occurrence, copied verbatim from TARGET_REGION. Use whatever the translator wrote, even if it is not the standard term. Stay close to the rendering; include a neighboring word only if needed to copy a contiguous verbatim string. Do not include emphasis markers such as *.\n"
        "- left_context: the Vietnamese word(s) immediately before target_quote in TARGET_REGION, used to pin the exact position when the same words repeat. \"\" if none.\n"
        "- confidence: high, medium, or low - how sure you are of the location.\n"
        "Rules:\n"
        "1. Copy target_quote verbatim from TARGET_REGION. Never invent or translate.\n"
        "2. Honor OCCURRENCE_INDEX. If TERM repeats, do not return a different occurrence.\n"
        "3. Prefer found = false over guessing a loosely related word.\n"
        "4. Return only the JSON, no extra fields, no scoring."
    )
    payload = {
        "occurrence_id": item.occurrence_id,
        "TERM": item.source_term,
        "OCCURRENCE_INDEX": item.occurrence_index,
        "SOURCE_SENTENCE": item.source_sentence,
        "TARGET_REGION": item.target_region,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def validate_payload(payload: Any, occurrence_id: str, target_region: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("occurrence_id")) != occurrence_id:
        return {
            "found": False,
            "target_quote": "",
            "left_context": "",
            "confidence": "low",
            "validation_error": "invalid_payload",
        }
    found = bool(payload.get("found"))
    quote = str(payload.get("target_quote") or "")
    left_context = str(payload.get("left_context") or "")
    if found and not _quote_in_region(quote, target_region):
        return {
            "found": False,
            "target_quote": "",
            "left_context": "",
            "confidence": "low",
            "validation_error": "quote_not_in_target_region",
        }
    confidence = str(payload.get("confidence") or "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "found": found,
        "target_quote": _clean_quote(quote) if found else "",
        "left_context": left_context if found else "",
        "confidence": confidence,
        "validation_error": "",
    }


def _quote_in_region(quote: str, target_region: str) -> bool:
    clean_quote = _clean_quote(quote)
    if not clean_quote:
        return False
    return clean_quote in _clean_quote(target_region)


def _clean_quote(value: str) -> str:
    return " ".join(str(value or "").replace("*", "").split())
