from __future__ import annotations

import re
from typing import Any


D2L_TERMINOLOGY_PROMPT_VERSION = "d2l_terminology_v7"
D2L_REGISTRY_OMITTED_TEXT = (
    "Registry intentionally omitted for D2L extraction. Extract terms visible "
    "in this window independently; deterministic consolidation will merge "
    "duplicates and resolve target variants after extraction."
)
LITERARY_PROMPT_VERSION = "literary_builder_context_v2"


def build_messages(
    chapter: dict[str, Any],
    registry_so_far_text: str,
    *,
    mode: str = "literary",
) -> list[dict[str, str]]:
    """Build the World Builder messages from stripped source text only."""

    if mode == "d2l_terminology":
        return build_d2l_terminology_messages(chapter, registry_so_far_text)
    if mode != "literary":
        raise ValueError(f"Unknown prepass mode: {mode}")

    system = (
        "You are the World Builder agent for an autonomous English-Vietnamese "
        "literary translation pipeline. Read only the source chapter provided by "
        "the user. Extract a compact chapter registry JSON for T1 glossary, T2 "
        "entities and relations, T3 summary, and T4 motifs.\n\n"
        "Hard rules:\n"
        f"- Prompt version: {LITERARY_PROMPT_VERSION}.\n"
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
        "or domain-specific phrases. Do not add ordinary words or everyday household "
        "objects. Negative examples: council, chart, terms, bearing, parlor, basin, "
        "breakfast table, stroke. Aim for 5-20 glossary terms per substantial chapter. "
        "Human/person entities belong in entities, not glossary.\n"
        "- If the chapter uses first-person narration, create entity_id ent_narrator. "
        "If the narrator's name is not explicitly visible in the provided chapter text, "
        "set canonical_source to \"the narrator\". When a later chapter reveals a name, "
        "re-emit the SAME entity_id ent_narrator with that visible name as "
        "canonical_source and add visible aliases.\n"
        "- Re-emit any existing registry entity that appears again with a new visible "
        "name, nickname, shortened name, or spelling variant, using the same entity_id.\n"
        "- mention_surfaces is required for EVERY entity that appears in this chapter, "
        "including registry entities that return with new surfaces.\n"
        "- Do not put plain pronouns in aliases_source or mention_surfaces: i, me, my, "
        "mine, myself, you, your, he, him, his, she, her, hers, it, its, we, us, our, "
        "they, them, their.\n"
        "- canonical_source must be one clean source name or label from the text; do "
        "not join alternatives with slashes.\n"
        "- address_a_to_b_vi and address_b_to_a_vi must be Vietnamese address terms, "
        "not mixed English/Vietnamese strings.\n"
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
        '    {"entity_id": "ent_narrator", "canonical_source": "the narrator", '
        '"aliases_source": ["the narrator"], "entity_type": "person|place|object|other", '
        '"proposed_target_vi": "nguoi ke chuyen", "aliases_target_vi": ["nguoi ke chuyen"]}\n'
        "  ],\n"
        '  "relations": [\n'
        '    {"a": "ent_narrator", "b": "ent_captain", "relation": "...", '
        '"address_a_to_b_vi": "ong", "address_b_to_a_vi": "cau be", '
        '"state_label": "wary_curiosity", "trigger_block_id": "ch02_b003", "notes": ""}\n'
        "  ],\n"
        '  "mention_surfaces": [{"entity_id": "ent_narrator", "surfaces": ["the narrator"]}],\n'
        '  "chapter_summary_vi": "<=150 Vietnamese words",\n'
        '  "motifs": [{"note": "...", "block_ids": ["ch02_b003"]}]\n'
        "}"
    )
    user = (
        f"REGISTRY_CONTEXT_PACK\n{registry_so_far_text}\n\n"
        f"CHAPTER_ID\n{chapter['chapter_id']}\n\n"
        "CHAPTER_TEXT_WITH_BLOCK_MARKERS\n"
        f"{render_chapter_blocks(chapter)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_d2l_terminology_messages(
    chapter: dict[str, Any],
    registry_so_far_text: str,
) -> list[dict[str, str]]:
    system = (
        "You are the World Builder agent for an autonomous English-Vietnamese "
        "technical-book translation pipeline. Read only the English source blocks "
        "provided by the user. Build a compact terminology registry for D2L. "
        "Never use any Vietnamese reference, glossary, gold, or external answer key.\n\n"
        "Hard rules:\n"
        f"- Prompt version: {D2L_TERMINOLOGY_PROMPT_VERSION}.\n"
        "- Return only valid JSON matching the requested contract.\n"
        "- Focus on technical terminology that needs book-wide consistency: ML concepts, "
        "math/statistics terms, model/layer names, abbreviations, framework/API names, "
        "and named datasets or algorithms.\n"
        "- The user gives you one source WINDOW, not necessarily a whole chapter. "
        "Extract every controlled technical term visible in this window. Do not impose "
        "a per-chapter cap; windowing is how we keep each call small.\n"
        "- No prior registry is provided for D2L extraction. Re-emit every controlled "
        "term that is visible in the current window even if it might have appeared in "
        "earlier windows. Downstream code consolidates duplicates deterministically.\n"
        "- Keep every string concise. Do not add commentary outside JSON. Keep termhood "
        "under 12 words per term.\n"
        "- Keep JSON compact: include only the 1-3 strongest evidence block ids per term, "
        "not every occurrence in the window.\n"
        "- Do not add ordinary English words unless they are used as technical terms.\n"
        "- Prefer concise exact source surfaces that appear in the window. Include "
        "foundational terms when they are used technically, not only long compounds.\n"
        "- Prefer one canonical source entry per concept. Do not emit separate near-duplicate "
        "subphrases when a more precise source term in the same window covers them.\n"
        "- Each glossary candidate must commit to ONE canonical Vietnamese target in "
        "proposed_target_vi/canonical_target. Put other acceptable Vietnamese forms in "
        "allowed_variants. Put literal wrong translations in forbidden_variants when clear.\n"
        "- All Vietnamese terminology targets MUST use full Vietnamese diacritics. "
        "Do not output ASCII-only Vietnamese such as 'tac nhan', 'mo hinh', "
        "'sieu tham so', or 'dao ham'. Output 'tác nhân', 'mô hình', "
        "'siêu tham số', 'đạo hàm'.\n"
        "- Use do_not_translate=true for framework/library/API names, code identifiers, "
        "dataset names, or terms conventionally kept in English.\n"
        "- term_type must be exactly one of: term, abbreviation, proper_noun, code_api.\n"
        "- category is kept for compatibility and must be exactly one of: nautical, "
        "cultural, object, place, other. Use other for normal D2L terms.\n"
        "- termhood must briefly state why this is a controlled term, not an ordinary word.\n"
        "- evidence_span_ids and block_ids must contain visible block markers from this chapter.\n"
        "- This D2L task is glossary-only. Return entities=[], relations=[], "
        "mention_surfaces=[], and motifs=[] exactly. Put named systems, datasets, "
        "libraries, APIs, and organizations in glossary_candidates with term_type "
        "proper_noun or code_api.\n"
        "- Do not read, infer from, or mention eval_glossary_gold, D2L Vietnamese markdown, "
        "human glossary, gold, or reference translations.\n\n"
        "Required JSON shape:\n"
        "{\n"
        '  "chapter_id": "...",\n'
        '  "glossary_candidates": [\n'
        "    {\n"
        '      "source_term": "agent",\n'
        '      "canonical_source": "agent",\n'
        '      "proposed_target_vi": "tác nhân",\n'
        '      "canonical_target": "tác nhân",\n'
        '      "termhood": "technical term for an acting system",\n'
        '      "term_type": "term|abbreviation|proper_noun|code_api",\n'
        '      "do_not_translate": false,\n'
        '      "category": "other",\n'
        '      "allowed_variants": ["tác tử"],\n'
        '      "forbidden_variants": ["đại lý"],\n'
        '      "block_ids": ["d2l_introduction_index_b001"],\n'
        '      "evidence_span_ids": ["d2l_introduction_index_b001"]\n'
        "    }\n"
        "  ],\n"
        '  "entities": [],\n'
        '  "relations": [],\n'
        '  "mention_surfaces": [],\n'
        '  "chapter_summary_vi": "<=80 Vietnamese words about the technical content",\n'
        '  "motifs": []\n'
        "}"
    )
    user = (
        f"REGISTRY_POLICY\n{D2L_REGISTRY_OMITTED_TEXT}\n\n"
        f"CHAPTER_ID\n{chapter['chapter_id']}\n\n"
        "ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS\n"
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
