"""Translation-only output contract for technical D2L S1 windows."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from pipeline.translate.d2l_soft_glossary_v1 import (
    injected_target_options,
    token_sequence_count,
)


POLICY_ID = "d2l_translation_slots_v1"
PROMPT_VERSION = "s1_d2l_translation_slots_v3_0_protected"
TRANSLATIONS_KEY = "translations"
GLOSSARY_REVIEW_POLICY_ID = "d2l_glossary_presence_review_v1"
GLOSSARY_REVIEW_MATCH_RULE_ID = "unicode_nfkc_casefold_alnum_tokens_presence_v1"
PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID = (
    "d2l_glossary_presence_review_v2_protected_lexical"
)
PROTECTED_LEXICAL_GLOSSARY_REVIEW_MATCH_RULE_ID = (
    "unicode_nfkc_casefold_alnum_tokens_protected_lexical_v2"
)


def build_slot_map(block_ids: Sequence[str]) -> dict[str, str]:
    """Return deterministic short-slot -> canonical-block mapping."""

    canonical_ids = [str(block_id) for block_id in block_ids]
    if any(not block_id for block_id in canonical_ids):
        raise ValueError("Translation slot block IDs must be nonempty")
    if len(set(canonical_ids)) != len(canonical_ids):
        raise ValueError("Translation slot block IDs must be unique")
    width = max(2, len(str(len(canonical_ids))))
    return {
        f"T{index:0{width}d}": block_id
        for index, block_id in enumerate(canonical_ids, start=1)
    }


def invert_slot_map(slot_to_block: Mapping[str, str]) -> dict[str, str]:
    block_to_slot = {
        str(block_id): str(slot_id)
        for slot_id, block_id in slot_to_block.items()
    }
    if len(block_to_slot) != len(slot_to_block):
        raise ValueError("Translation slot map must map one slot to one block")
    return block_to_slot


def slotize_blocks(
    blocks: Sequence[Mapping[str, Any]],
    slot_to_block: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Replace prompt-facing block IDs with short slots without changing content."""

    block_to_slot = invert_slot_map(slot_to_block)
    observed: set[str] = set()
    slotized: list[dict[str, Any]] = []
    for raw_block in blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        if block_id not in block_to_slot:
            raise ValueError(f"Prompt block is absent from slot map: {block_id}")
        if block_id in observed:
            raise ValueError(f"Prompt block is duplicated: {block_id}")
        observed.add(block_id)
        block["block_id"] = block_to_slot[block_id]
        slotized.append(block)
    missing = sorted(set(block_to_slot) - observed)
    if missing:
        raise ValueError("Slot map references missing prompt blocks: " + ", ".join(missing))
    return slotized


def render_system_prompt(prompt_version: str) -> str:
    return (
        "You are an autonomous technical English-to-Vietnamese translator. "
        "Translate only; do not audit terminology, report decisions, explain, or comment.\n\n"
        "OUTPUT CONTRACT: Return only one valid JSON object with exactly one top-level "
        f"key, {TRANSLATIONS_KEY}. Its value must be an object keyed by every short slot "
        "from the source window. Copy each slot exactly, include every slot once, and add "
        "no other key. Every value must be the full Vietnamese translation string.\n\n"
        "Example output:\n"
        '{"translations":{"T01":"Bản dịch thứ nhất.","T02":"Bản dịch thứ hai."}}\n\n'
        "TRANSLATION POLICY:\n"
        "- Preserve source meaning, definitions, variable references, and logical relations.\n"
        "- Use clear, natural Vietnamese suitable for a technical book.\n"
        "- Prefer supplied terminology when the local technical sense fits; otherwise "
        "translate naturally. Do not report the choice.\n"
        "- Copy every protected placeholder exactly once and in source order within its slot.\n"
        "- Do not add explanations, footnotes, translator comments, Markdown fences, or "
        "metadata.\n"
        f"- PROMPT VERSION: {prompt_version}\n"
    )


def extract_slot_translations(
    parsed_json: Mapping[str, Any] | None,
    slot_to_block: Mapping[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Validate the exact slot envelope and map values to canonical block IDs."""

    expected_slots = list(slot_to_block)
    if parsed_json is None:
        return {}, [f"JSON parse failed; expected slots: {expected_slots}"]
    if not isinstance(parsed_json, Mapping):
        return {}, ["Translator output must be a JSON object"]

    errors: list[str] = []
    top_level_keys = set(parsed_json)
    if top_level_keys != {TRANSLATIONS_KEY}:
        missing = {TRANSLATIONS_KEY} - top_level_keys
        extra = top_level_keys - {TRANSLATIONS_KEY}
        if missing:
            errors.append(f"Missing top-level key: {TRANSLATIONS_KEY}")
        for key in sorted(extra):
            errors.append(f"Unexpected top-level key: {key}")

    raw_translations = parsed_json.get(TRANSLATIONS_KEY)
    if not isinstance(raw_translations, Mapping):
        errors.append(f"{TRANSLATIONS_KEY} must be a JSON object")
        return {}, errors

    translations: dict[str, str] = {}
    for slot_id, block_id in slot_to_block.items():
        value = raw_translations.get(slot_id)
        if value is None:
            errors.append(f"Missing translation slot: {slot_id}")
        elif not isinstance(value, str):
            errors.append(
                f"Non-string value for {slot_id}: {type(value).__name__}"
            )
        else:
            translations[str(block_id)] = value
    for key in raw_translations:
        if key not in slot_to_block:
            errors.append(f"Unexpected translation slot: {key}")
    return translations, errors


def parse_slot_json_text(text: str) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Parse JSON while rejecting duplicate keys at any object depth."""

    duplicate_keys: list[str] = []

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys.append(str(key))
            result[str(key)] = value
        return result

    try:
        parsed = json.loads(text, object_pairs_hook=object_pairs)
    except json.JSONDecodeError as exc:
        return None, [f"JSON parse failed: {exc}"]
    if duplicate_keys:
        return None, [
            "Duplicate JSON key: " + key for key in sorted(set(duplicate_keys))
        ]
    if not isinstance(parsed, Mapping):
        return None, ["Translator output must be a JSON object"]
    return parsed, []


def slot_reask_note(errors: Sequence[str], slot_to_block: Mapping[str, str]) -> str:
    samples = "; ".join(str(error) for error in list(errors)[:5])
    slots = ", ".join(slot_to_block)
    return (
        f"Output errors: {samples}. Return JSON only in this exact shape: "
        f'{{"{TRANSLATIONS_KEY}":{{"T01":"..."}}}}. '
        f"Use exactly these slots: {slots}. Include each once with a Vietnamese string. "
        "No other top-level key, metadata, explanation, or Markdown fence."
    )


def glossary_review_rows(
    blocks: Sequence[Mapping[str, Any]],
    translations: Mapping[str, str],
    context_pack: Any | None,
    *,
    policy_id: str = GLOSSARY_REVIEW_POLICY_ID,
    match_rule_id: str = GLOSSARY_REVIEW_MATCH_RULE_ID,
) -> list[dict[str, Any]]:
    """Flag absent preferred targets mechanically without rejecting translations."""

    preferences = injected_target_options(context_pack)
    if not preferences:
        return []
    rows: list[dict[str, Any]] = []
    for raw_block in blocks:
        block_id = str(raw_block.get("block_id") or "")
        source = str(
            raw_block.get("source_text") or raw_block.get("clean_text") or ""
        )
        target = translations.get(block_id)
        if not isinstance(target, str):
            continue
        for source_term in sorted(preferences):
            source_count = token_sequence_count(source, source_term)
            if source_count == 0:
                continue
            preferred_targets = sorted(preferences[source_term])
            matched_targets = [
                preferred_target
                for preferred_target in preferred_targets
                if token_sequence_count(target, preferred_target) > 0
            ]
            if matched_targets:
                continue
            identity = {
                "policy_id": policy_id,
                "block_id": block_id,
                "source_term_normalized": source_term,
                "preferred_targets_normalized": preferred_targets,
                "source_occurrences": source_count,
            }
            review_id = "grv_" + sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16]
            rows.append(
                {
                    "review_id": review_id,
                    "status": "review_required",
                    "reason_code": "no_injected_target_detected",
                    "match_rule_id": match_rule_id,
                    **identity,
                }
            )
    return rows


def glossary_review_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    policy_id: str = GLOSSARY_REVIEW_POLICY_ID,
    match_rule_id: str = GLOSSARY_REVIEW_MATCH_RULE_ID,
) -> dict[str, Any]:
    return {
        "policy_id": policy_id,
        "match_rule_id": match_rule_id,
        "review_rows": len(rows),
        "review_blocks": len(
            {str(row.get("block_id") or "") for row in rows}
        ),
    }


__all__ = [
    "GLOSSARY_REVIEW_MATCH_RULE_ID",
    "GLOSSARY_REVIEW_POLICY_ID",
    "PROTECTED_LEXICAL_GLOSSARY_REVIEW_MATCH_RULE_ID",
    "PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID",
    "POLICY_ID",
    "PROMPT_VERSION",
    "TRANSLATIONS_KEY",
    "build_slot_map",
    "extract_slot_translations",
    "glossary_review_rows",
    "glossary_review_summary",
    "invert_slot_map",
    "parse_slot_json_text",
    "render_system_prompt",
    "slot_reask_note",
    "slotize_blocks",
]
