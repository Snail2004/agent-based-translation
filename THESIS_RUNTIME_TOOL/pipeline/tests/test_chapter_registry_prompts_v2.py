from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import (
    load_system_prompt_for_chapter,
    load_system_prompt_from_design,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"

PROMPT_IDS = (
    "literary_chapter_orient_v2",
    "literary_registry_delta_v2_2",
    "literary_registry_audit_v1_1",
)
FROZEN_V1_IDS = (
    "literary_chapter_orient_v1",
    "literary_registry_extract_v1",
    "literary_registry_resolve_v1",
)


def _reviewed_blockquote(prompt_id: str) -> str:
    """Extract the reviewed blockquote independently of the runtime loader."""

    text = DESIGN_DOC.read_text(encoding="utf-8")
    heading_at = text.index(f"### {prompt_id}")
    marker_at = text.index(f"- Prompt version: {prompt_id}.", heading_at)
    quote_start = text.rfind("\n>", heading_at, marker_at + 1)
    assert quote_start >= 0
    quote_start += 1
    boundaries = [
        value
        for value in (
            text.find("\n### ", marker_at),
            text.find("\n---", marker_at),
        )
        if value >= 0
    ]
    quote_end = min(boundaries) if boundaries else len(text)
    lines: list[str] = []
    for line in text[quote_start:quote_end].splitlines():
        if line.startswith("> "):
            lines.append(line[2:])
        elif line == ">":
            lines.append("")
        elif lines:
            break
    return "\n".join(lines).strip()


def _required_shape(prompt_id: str) -> dict[str, Any]:
    prompt = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
    prefix = "- Required JSON shape: "
    matches = [line[len(prefix) :] for line in prompt.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1
    return json.loads(matches[0])


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_v2_prompt_loader_returns_exact_reviewed_bytes_without_marker_bleed() -> None:
    all_ids = PROMPT_IDS + FROZEN_V1_IDS
    for prompt_id in PROMPT_IDS:
        loaded = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
        assert loaded == _reviewed_blockquote(prompt_id)
        assert loaded.count(f"Prompt version: {prompt_id}") == 1
        assert all(other not in loaded for other in all_ids if other != prompt_id)


def test_v2_prompt_examples_parse_with_closed_top_level_shapes() -> None:
    orient = _required_shape("literary_chapter_orient_v2")
    delta = _required_shape("literary_registry_delta_v2_2")
    audit = _required_shape("literary_registry_audit_v1_1")

    assert set(orient) == {
        "gist",
        "narrator_hypotheses",
        "salient_surface_checklist",
    }
    assert set(delta) == {
        "new_entities",
        "new_aliases",
        "new_glossary_items",
        "local_bindings",
        "tickets",
    }
    assert set(audit) == {
        "entity_dispositions",
        "alias_dispositions",
        "glossary_dispositions",
        "local_binding_dispositions",
        "ticket_dispositions",
        "profile_revisions",
    }


def test_orientation_v2_is_prose_coverage_without_coordinate_or_identity_fields() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_chapter_orient_v2")
    shape = _required_shape("literary_chapter_orient_v2")
    keys = _all_keys(shape)

    assert "NOT an identity pass or a scene parser" in prompt
    assert "COVERAGE CHECKLIST" in prompt
    assert "A narrator hypothesis is orientation only" in prompt
    assert "never use the checklist to limit" in prompt
    assert set(shape["narrator_hypotheses"][0]) == {"surface", "note", "block_ids"}
    assert set(shape["salient_surface_checklist"][0]) == {"surface", "block_id"}
    assert keys.isdisjoint(
        {
            "entity_id",
            "referent_kind_claim",
            "anchor_text",
            "evidence_quote",
            "occurrence_hint",
            "scene_range",
            "offset",
        }
    )


def test_registry_delta_v2_is_sequential_delta_only_and_has_no_tool_or_event_contract() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_registry_delta_v2_2")
    shape = _required_shape("literary_registry_delta_v2_2")
    keys = _all_keys(shape)

    assert "Build a DELTA, not an occurrence inventory" in prompt
    assert "You never receive the full registry" in prompt
    assert "latest validated sequential revision" in prompt
    assert "Each surface candidate packet is scoped to its source_surface" in prompt
    assert "Even exactly one candidate can be wrong" in prompt
    assert "not identity authority, ranking, confidence, or an answer" in prompt
    assert "must be represented as an entity, never only as a glossary item" in prompt
    assert "Do not emit the same source surface" in prompt
    assert "Five empty lists are valid only after checking" in prompt
    assert "When TARGETED_SALIENT_SURFACES is present" in prompt
    assert "mutually incompatible claims that cannot both be true" in prompt
    assert "provisional status is a transaction state" in prompt
    assert "Never open importance_review merely to affirm" in prompt
    assert "may be empty or omitted when there is no alias" in prompt
    assert "OPEN_TICKETS are read-only context" in prompt
    assert "never copy, repeat, rephrase, affirm, or resolve" in prompt
    assert "Do not output speaker turns, events, relations, phases" in prompt
    assert "unlocatable_surface and missing_salient_surface are code-owned" in prompt
    assert set(shape["new_entities"][0]) == {
        "surface",
        "mention_type",
        "referent_kind_claim",
        "short_description",
        "created_from_block_id",
        "support_block_ids",
        "initial_aliases",
    }
    assert set(shape["new_entities"][0]["initial_aliases"][0]) == {
        "surface",
        "alias_type",
        "support_block_ids",
    }
    assert keys.isdisjoint(
        {
            "occurrence_id",
            "anchor_text",
            "evidence_quote",
            "occurrence_hint",
            "context_requests",
            "retrieval_requests",
            "confidence",
            "event_id",
            "relation_phases",
            "proposed_target_vi",
            "do_not_translate",
        }
    )


def test_registry_delta_v2_separates_global_alias_local_descriptor_and_glossary() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_registry_delta_v2_2")
    shape = _required_shape("literary_registry_delta_v2_2")

    assert "One surface may legitimately belong to several entities" in prompt
    assert "The binding is block-local and advisory" in prompt
    assert "Bare pronouns are out of scope" in prompt
    assert "a conflicting category or description opens glossary_collision" in prompt
    assert set(shape["new_aliases"][0]) == {
        "surface",
        "alias_type",
        "target_entity_id",
        "support_block_ids",
    }
    assert set(shape["local_bindings"][0]) == {
        "surface",
        "block_id",
        "target_entity_id",
        "support_block_ids",
    }
    assert set(shape["new_glossary_items"][0]) == {
        "surface",
        "category_claim",
        "short_description",
        "created_from_block_id",
        "support_block_ids",
    }


def test_registry_audit_v1_exact_covers_only_exceptions_and_cannot_rebuild_registry() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_registry_audit_v1_1")
    shape = _required_shape("literary_registry_audit_v1_1")

    assert "Audit ONLY the supplied exception rows and open tickets" in prompt
    assert "do not invent dispositions" in prompt
    assert "Copy every supplied id exactly once" in prompt
    assert "Never merge or split two entities that were both confirmed" in prompt
    assert "This prompt is not called at all when the exception manifest is empty" in prompt
    assert set(shape["glossary_dispositions"][0]) == {
        "glossary_id",
        "action",
    }
    assert set(shape["ticket_dispositions"][0]) == {
        "ticket_id",
        "action",
        "resolution_note",
    }
    assert "new_entities" not in shape
    assert "new_aliases" not in shape
    assert "tool_calls" not in _all_keys(shape)


def test_v2_prompts_are_book_neutral_and_placeholder_is_runtime_rendered() -> None:
    forbidden = (
        "heathcliff",
        "cathy",
        "catherine",
        "earnshaw",
        "lockwood",
        "nelly",
        "joseph",
        "jabez",
        "wuthering",
        "gatsby",
        "carraway",
        "daisy",
        "juno",
        "the master",
        "madam",
    )
    for prompt_id in PROMPT_IDS:
        loaded = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
        lowered = loaded.casefold()
        assert all(value not in lowered for value in forbidden)
        rendered = load_system_prompt_for_chapter(
            DESIGN_DOC,
            prompt_id,
            "novel_ch07",
        )
        if "bk_ch01" in loaded:
            assert "bk_ch01" not in rendered
            assert "novel_ch07" in rendered
        else:
            assert rendered == loaded
