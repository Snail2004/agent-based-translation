from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


LITERARY_SYSTEM_PROMPT = (
    "You are an autonomous literary translator operating in a structured pipeline. "
    "Your role is English-to-Vietnamese translation only - no annotation, no commentary.\n\n"
    "OUTPUT CONTRACT: Return ONLY a valid JSON object keyed by block_id. "
    "Every block_id from the user's input MUST appear as a key with its Vietnamese "
    "translation as the value. Do NOT add extra keys, explanations, or markup.\n\n"
    "Example output format:\n"
    '{"ch02_b001": "Cậu bé đứng trước cánh cửa gỗ.", '
    '"ch02_b002": "\\"Xin chào,\\" cậu nói."}\n\n'
    "STYLE POLICY (Newmark V approach):\n"
    "- DEFAULT: SEMANTIC translation - faithful to source meaning and narrative voice, "
    "preserve the storytelling register.\n"
    "- DIALOGUE: COMMUNICATIVE - natural, idiomatic Vietnamese; "
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
    "You see only this window - consistency beyond the window is not your concern.\n"
    "- BLOCK IDs: use the full block_id as provided (e.g. ch02_b003).\n"
    "- PROMPT VERSION: {prompt_version}\n"
)


TECHNICAL_D2L_SYSTEM_PROMPT = (
    "You are an autonomous technical translator operating in a structured pipeline. "
    "Your role is English-to-Vietnamese translation only - no annotation, no commentary.\n\n"
    "OUTPUT CONTRACT: Return ONLY a valid JSON object keyed by block_id. "
    "Every block_id from the user's input MUST appear as a key with its Vietnamese "
    "translation as the value. Do NOT add extra keys, explanations, or markup.\n\n"
    "Example output format:\n"
    '{"d2l_intro_b001": "Học sâu đã thay đổi cách chúng ta xây dựng mô hình.", '
    '"d2l_intro_b002": "## Mạng nơ-ron"}\n\n'
    "STYLE POLICY (technical/expository):\n"
    "- Translate headings and prose into clear, accurate Vietnamese for a technical book.\n"
    "- Preserve meaning, definitions, variable references, and logical relations exactly.\n"
    "- Keep terminology consistent inside this window and follow any mandatory terminology "
    "provided by the user message.\n"
    "- Preserve inline code, identifiers, API names, mathematical symbols, equation references, "
    "citations, URLs, filenames, and units such as `.shape`, `16kHz`, `O(n)`, `x_i`, and `Eq. (1)`.\n"
    "- Do not translate code fences, equations, image markup, or labels; those blocks are not "
    "sent to you in this profile.\n"
    "- PROHIBITED:\n"
    "  * Adding explanations, footnotes, or translator comments.\n"
    "  * Dropping source content or changing the technical claim.\n"
    "  * Decorative literary rewriting, dialogue-specific style, or cultural adaptation.\n"
    "- BLOCK IDs: use the full block_id exactly as provided.\n"
    "- PROMPT VERSION: {prompt_version}\n"
)


@dataclass(frozen=True)
class DocumentProfile:
    name: str
    default_experiment_id: str
    system_prompt_template: str
    prompt_versions: dict[str, str]
    translatable_block_types: frozenset[str] | None
    passthrough_block_types: frozenset[str]
    min_injection_occurrences: int
    inject_preserve_terms: bool
    inject_entities: bool

    def prompt_version(self, config: str) -> str:
        key = config.upper()
        return self.prompt_versions.get(key, self.prompt_versions["S0"])

    def system_prompt(self, prompt_version: str) -> str:
        return self.system_prompt_template.replace("{prompt_version}", prompt_version)


PROFILES: dict[str, DocumentProfile] = {
    "literary_v1": DocumentProfile(
        name="literary_v1",
        default_experiment_id="translate_run",
        system_prompt_template=LITERARY_SYSTEM_PROMPT,
        prompt_versions={"S0": "s0_v1", "S1": "s1_v1"},
        translatable_block_types=None,
        passthrough_block_types=frozenset(),
        min_injection_occurrences=0,
        inject_preserve_terms=True,
        inject_entities=True,
    ),
    "technical_d2l_v1": DocumentProfile(
        name="technical_d2l_v1",
        default_experiment_id="d2l_p3",
        system_prompt_template=TECHNICAL_D2L_SYSTEM_PROMPT,
        prompt_versions={"S0": "s0_d2l_v1", "S1": "s1_d2l_v1"},
        translatable_block_types=frozenset({"heading", "prose"}),
        passthrough_block_types=frozenset({"code", "math_block", "image", "label"}),
        min_injection_occurrences=2,
        inject_preserve_terms=False,
        inject_entities=False,
    ),
}


_UNIT_OR_SYMBOL_RE = re.compile(
    r"^(?:\.[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?\s?(?:hz|khz|mhz|ghz|kb|mb|gb|ms|s|%))$",
    re.IGNORECASE,
)
_PRESERVE_TERM_TYPES = {"code_api", "proper_noun"}


def get_profile(name: str | None) -> DocumentProfile:
    profile_name = name or "literary_v1"
    try:
        return PROFILES[profile_name]
    except KeyError as exc:
        raise ValueError(f"Unknown document profile: {profile_name}") from exc


def block_is_translatable(block_type: str, profile: DocumentProfile) -> bool:
    if profile.translatable_block_types is None:
        return True
    return str(block_type or "") in profile.translatable_block_types


def injection_role_for_term(term_row: dict[str, Any]) -> str:
    source = str(term_row.get("source_term") or "")
    term_type = str(term_row.get("term_type") or "").casefold()
    do_not_translate = bool(int(term_row.get("do_not_translate") or 0))
    if do_not_translate or term_type in _PRESERVE_TERM_TYPES or _UNIT_OR_SYMBOL_RE.match(source.strip()):
        return "preserve"
    return "translate"


def term_is_injection_eligible(term_row: dict[str, Any], profile: DocumentProfile) -> bool:
    role = injection_role_for_term(term_row)
    if role == "preserve" and not profile.inject_preserve_terms:
        return False
    occurrences = int(term_row.get("occurrences_count") or 0)
    if occurrences < profile.min_injection_occurrences:
        return False
    return role == "translate" or profile.inject_preserve_terms
