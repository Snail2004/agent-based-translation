from __future__ import annotations

import re
from typing import Any


def build_messages(chapter: dict[str, Any], registry_so_far_text: str) -> list[dict[str, str]]:
    """Build the World Builder messages from stripped source text only."""

    system = (
        "You are the World Builder agent for an autonomous English-Vietnamese "
        "literary translation pipeline. Read only the source chapter provided by "
        "the user. Extract a compact chapter registry JSON for T1 glossary, T2 "
        "entities and relations, T3 summary, and T4 motifs.\n\n"
        "Hard rules:\n"
        "- Return only valid JSON matching the requested contract.\n"
        "- Use entity_id values with prefix ent_ and snake_case.\n"
        "- Every glossary candidate MUST use exactly these keys: source_term, "
        "proposed_target_vi, do_not_translate, category, block_ids.\n"
        "- category MUST be exactly one of: nautical, cultural, object, place, other. "
        "Do not use phrase, legal_term, nautical_term, nautical_object, or relationship.\n"
        "- Every motif MUST use exactly: note, block_ids. Do not use label, theme, or text.\n"
        "- Every block_ids list must contain visible block markers from this chapter.\n"
        "- Include Vietnamese address-policy hints in relations: address_a_to_b_vi "
        "and address_b_to_a_vi. Make them phase-aware through state_label and notes.\n"
        "- Mention surfaces only store source surfaces; do not invent offsets.\n"
        "- Terms are words or phrases that need book-wide consistency and can drift: "
        "nautical terms, culturally specific objects, special objects, place names, "
        "or domain-specific phrases. Do not add ordinary words. Negative examples: "
        "council, chart, terms, bearing. Aim for 5-20 glossary terms per substantial "
        "chapter. Human/person entities belong in entities, not glossary.\n"
        "- If the chapter uses first-person narration (I, me, my), create a person "
        "entity for the narrator. For Treasure Island, use entity_id ent_jim_hawkins, "
        "canonical_source \"Jim Hawkins / first-person narrator\", aliases_source "
        "[\"I\", \"me\", \"my\", \"Jim\"], and proposed_target_vi \"Jim Hawkins\".\n"
        "- Use the visible block markers for block_ids and trigger_block_id.\n\n"
        "Required JSON shape:\n"
        "{\n"
        '  "chapter_id": "...",\n'
        '  "glossary_candidates": [\n'
        '    {"source_term": "...", "proposed_target_vi": "...", '
        '"do_not_translate": false, "category": "nautical|cultural|object|place|other", '
        '"block_ids": ["ch02_b003"]}\n'
        "  ],\n"
        '  "entities": [\n'
        '    {"entity_id": "ent_jim_hawkins", "canonical_source": "Jim Hawkins", '
        '"aliases_source": ["Jim"], "entity_type": "person|place|object|other", '
        '"proposed_target_vi": "Jim Hawkins", "aliases_target_vi": ["Jim"]}\n'
        "  ],\n"
        '  "relations": [\n'
        '    {"a": "ent_jim_hawkins", "b": "ent_captain", "relation": "...", '
        '"address_a_to_b_vi": "ông", "address_b_to_a_vi": "cậu bé", '
        '"state_label": "wary_curiosity", "trigger_block_id": "ch02_b003", "notes": ""}\n'
        "  ],\n"
        '  "mention_surfaces": [{"entity_id": "ent_jim_hawkins", "surfaces": ["Jim"]}],\n'
        '  "chapter_summary_vi": "≤150 Vietnamese words",\n'
        '  "motifs": [{"note": "...", "block_ids": ["ch02_b003"]}]\n'
        "}"
    )
    user = (
        f"REGISTRY_SO_FAR\n{registry_so_far_text}\n\n"
        f"CHAPTER_ID\n{chapter['chapter_id']}\n\n"
        "CHAPTER_TEXT_WITH_BLOCK_MARKERS\n"
        f"{render_chapter_blocks(chapter)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def render_chapter_blocks(chapter: dict[str, Any]) -> str:
    lines: list[str] = []
    for block in chapter.get("blocks") or []:
        block_id = str(block["block_id"])
        marker = short_block_id(block_id)
        text = str(block.get("clean_text") or block.get("source_text") or "").replace(
            "\n", " "
        )
        lines.append(f"[{marker}] {text}")
    return "\n".join(lines)


def short_block_id(block_id: str) -> str:
    match = re.search(r"(ch\d+_b\d+)$", block_id)
    return match.group(1) if match else block_id
