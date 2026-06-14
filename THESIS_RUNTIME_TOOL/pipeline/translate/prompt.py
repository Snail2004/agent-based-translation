from __future__ import annotations

from typing import Any

from pipeline.translate.profiles import get_profile


PROMPT_VERSION = "s0_v1"
S1_PROMPT_VERSION = "s1_v1"


def prompt_version_for_config(config: str, profile_name: str = "literary_v1") -> str:
    return get_profile(profile_name).prompt_version(config)


def build_messages(
    window_blocks: list[dict[str, Any]],
    prompt_version: str = PROMPT_VERSION,
    *,
    config: str = "S0",
    context_pack: Any | None = None,
    profile_name: str = "literary_v1",
) -> list[dict[str, str]]:
    """Build translator messages.

    S0 PURITY: for every profile, S0 has no glossary/entities/summaries/motifs
    or address policy from memory. S1 differs only by the hard-constraint block.
    """

    config = config.upper()
    profile = get_profile(profile_name)
    if prompt_version == PROMPT_VERSION:
        prompt_version = prompt_version_for_config(config, profile_name)

    system = profile.system_prompt(prompt_version)
    user = (
        _render_s1_user(window_blocks, context_pack)
        if config == "S1"
        else _render_source_blocks(window_blocks)
    )

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


def _render_s1_user(
    blocks: list[dict[str, Any]],
    context_pack: Any | None,
) -> str:
    constraints = ""
    if context_pack is not None and hasattr(context_pack, "render_hard_constraints"):
        constraints = str(context_pack.render_hard_constraints())
    if not constraints:
        constraints = (
            "MANDATORY TERMINOLOGY & NAMES\n"
            "(none)\n"
            "ADDRESS POLICY (xung ho)\n"
            "(none)"
        )
    return f"{constraints}\n\nSOURCE WINDOW\n{_render_source_blocks(blocks)}"


def extract_translations(
    parsed_json: dict[str, Any] | None,
    expected_block_ids: list[str],
) -> tuple[dict[str, str], list[str]]:
    """Extract block_id -> translation mapping from LLM JSON output."""
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
    """Assert that system/user messages contain no memory-derived content."""
    violations: list[str] = []
    all_content = " ".join(msg.get("content", "") for msg in messages).lower()

    for term in db_glossary:
        source = str(term.get("source_term", "")).lower()
        target = str(term.get("proposed_target_vi", "")).lower()
        if source and source in all_content:
            violations.append(f"Glossary source term found in prompt: {source}")
        if target and len(target) > 3 and target in all_content:
            violations.append(f"Glossary target term found in prompt: {target}")

    for entity in db_entities:
        canonical = str(entity.get("canonical_source", "")).lower()
        if canonical and len(canonical) > 3 and canonical in all_content:
            violations.append(f"Entity canonical name found in prompt: {canonical}")

    for item in db_summaries:
        content = str(item.get("content", "")).lower()
        if content and len(content) > 10 and content in all_content:
            violations.append(f"Memory item content found in prompt: {content[:50]}...")

    return violations
