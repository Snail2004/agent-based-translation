from __future__ import annotations

import json
from typing import Any


PROMPT_VERSION = "s0_v1"


def build_messages(
    window_blocks: list[dict[str, Any]],
    prompt_version: str = PROMPT_VERSION,
) -> list[dict[str, str]]:
    """Build the S0 translator messages — style policy + source window only.

    S0 PURITY: prompt does NOT contain glossary, entities, summaries, motifs,
    or address/pronoun policy from memory.  Pure style guidance + source text.
    """

    system = (
        "You are an autonomous literary translator operating in a structured pipeline. "
        "Your role is English-to-Vietnamese translation only — no annotation, no commentary.\n\n"
        "OUTPUT CONTRACT: Return ONLY a valid JSON object keyed by block_id. "
        "Every block_id from the user's input MUST appear as a key with its Vietnamese "
        "translation as the value. Do NOT add extra keys, explanations, or markup.\n\n"
        "Example output format:\n"
        '{"ch02_b001": "Cậu bé đứng trước cánh cửa gỗ.", '
        '"ch02_b002": "\"Xin chào,\" cậu nói."}\n\n'
        "STYLE POLICY (Newmark V approach):\n"
        "- DEFAULT: SEMANTIC translation — faithful to source meaning and narrative voice, "
        "preserve the storytelling register.\n"
        "- DIALOGUE: COMMUNICATIVE — natural, idiomatic Vietnamese; "
        "matching the speaker's social role and tone.\n"
        "- CARRY OVER: italicized English terms (ship names, exclamations, proper nouns).\n"
        "- PROHIBITED:\n"
        "  * Word-for-word / calque: translate meaning, not structure.\n"
        "  * ADDING content not present in the source (no adaptation).\n"
        "  * DROPPING content from the source without semantic justification.\n"
        "  * Translator's footnotes or parenthetical comments.\n"
        "- NAME TRANSLATION: proper names remain as-is; "
        "if a conventional Vietnamese form exists, prefer it.\n"
        "- PRONOUN / ADDRESS CHOICE: use consistent Vietnamese pronouns within this window. "
        "Pick whichever form best matches the relationship implied by the text. "
        "You see only this window — consistency beyond the window is not your concern.\n"
        f"- BLOCK IDs: use the full block_id as provided (e.g. ch02_b003).\n"
        f"- PROMPT VERSION: {prompt_version}\n"
    )

    user = _render_source_blocks(window_blocks)

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _render_source_blocks(blocks: list[dict[str, Any]]) -> str:
    """Render source blocks as user content: [<block_id>] <clean_text> per line."""
    lines: list[str] = []
    for block in blocks:
        block_id = str(block.get("block_id", ""))
        text = str(block.get("clean_text") or block.get("source_text") or "").replace(
            "\n", " "
        )
        lines.append(f"[{block_id}] {text}")
    return "\n\n".join(lines)


def extract_translations(
    parsed_json: dict[str, Any] | None,
    expected_block_ids: list[str],
) -> tuple[dict[str, str], list[str]]:
    """Extract block_id → translation mapping from LLM JSON output.

    Returns (translations, errors).
    errors lists missing block_ids or non-string values.
    """
    if parsed_json is None:
        return {}, [f"JSON parse failed; expected keys: {expected_block_ids}"]

    translations: dict[str, str] = {}
    errors: list[str] = []

    for block_id in expected_block_ids:
        value = parsed_json.get(block_id)
        if value is None:
            errors.append(f"Missing block_id: {block_id}")
        elif not isinstance(value, str):
            errors.append(f"Non-string value for {block_id}: {type(value).__name__}")
        else:
            translations[block_id] = value

    # Warn about extra keys the model might have added
    for key in parsed_json:
        if key not in expected_block_ids:
            errors.append(f"Unexpected block_id in output: {key}")

    return translations, errors


def purity_check(
    messages: list[dict[str, str]],
    db_glossary: list[dict[str, Any]],
    db_entities: list[dict[str, Any]],
    db_summaries: list[dict[str, Any]],
) -> list[str]:
    """Assert that system/user messages contain NO memory-derived content.

    Returns a list of violations (empty = passed).
    This is used in tests to verify S0 purity.
    """
    violations: list[str] = []
    all_content = " ".join(msg.get("content", "") for msg in messages).lower()

    # Check glossary terms
    for term in db_glossary:
        source = str(term.get("source_term", "")).lower()
        target = str(term.get("proposed_target_vi", "")).lower()
        if source and source in all_content:
            violations.append(f"Glossary source term found in prompt: {source}")
        if target and len(target) > 3 and target in all_content:
            violations.append(f"Glossary target term found in prompt: {target}")

    # Check entity names
    for entity in db_entities:
        canonical = str(entity.get("canonical_source", "")).lower()
        if canonical and len(canonical) > 3 and canonical in all_content:
            violations.append(f"Entity canonical name found in prompt: {canonical}")

    # Check memory items (summaries, motifs)
    for item in db_summaries:
        content = str(item.get("content", "")).lower()
        if content and len(content) > 10 and content in all_content:
            violations.append(f"Memory item content found in prompt: {content[:50]}...")

    return violations
