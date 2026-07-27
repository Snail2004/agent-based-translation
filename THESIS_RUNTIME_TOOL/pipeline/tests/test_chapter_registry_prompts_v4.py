from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_prompts_v4 import (
    PROMPT_IDS,
    PROMPT_SHA256,
    RegistryPromptV4Error,
    load_registry_prompt_v4,
    prompt_manifest_v4,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _reviewed_blockquote(prompt_id: str) -> str:
    text = DESIGN_DOC.read_text(encoding="utf-8")
    heading_at = text.index(f"### {prompt_id}")
    marker_at = text.index(f"- Prompt version: {prompt_id}.", heading_at)
    quote_start = text.rfind("\n>", heading_at, marker_at + 1)
    assert quote_start >= 0
    quote_start += 1
    boundaries = [
        value
        for value in (text.find("\n### ", marker_at), text.find("\n---", marker_at))
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


def _required_shape(role: str) -> dict[str, Any]:
    prompt = load_registry_prompt_v4(DESIGN_DOC, role)
    prefix = "- Required JSON shape: "
    rows = [line[len(prefix) :] for line in prompt.splitlines() if line.startswith(prefix)]
    assert len(rows) == 1
    return json.loads(rows[0])


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_v4_loader_bytes_equal_reviewed_bytes_and_pinned_hashes() -> None:
    for role, prompt_id in PROMPT_IDS.items():
        loaded = load_registry_prompt_v4(DESIGN_DOC, role)
        assert loaded == _reviewed_blockquote(prompt_id)
        assert loaded == load_system_prompt_from_design(DESIGN_DOC, prompt_id)
        assert loaded.count(f"Prompt version: {prompt_id}.") == 1
        assert hashlib.sha256(loaded.encode("utf-8")).hexdigest() == PROMPT_SHA256[role]
        assert all(
            other_id not in loaded
            for other_role, other_id in PROMPT_IDS.items()
            if other_role != role
        )


def test_v4_prompt_manifest_is_content_addressed_and_closed() -> None:
    manifest = prompt_manifest_v4(DESIGN_DOC)
    assert [row["role"] for row in manifest["prompts"]] == ["b0", "b1", "auditor"]
    assert {row["prompt_id"] for row in manifest["prompts"]} == set(PROMPT_IDS.values())
    assert all(row["sha256"] == PROMPT_SHA256[row["role"]] for row in manifest["prompts"])
    assert all(row["utf8_bytes"] > 0 for row in manifest["prompts"])
    assert len(manifest["manifest_hash"]) == 64
    with pytest.raises(RegistryPromptV4Error, match="unknown registry v4 prompt role"):
        load_registry_prompt_v4(DESIGN_DOC, "resolver")


def test_v4_required_examples_have_closed_top_level_shapes() -> None:
    assert set(_required_shape("b0")) == {
        "orientation_draft",
        "narrative_context",
        "attention_items",
    }
    assert set(_required_shape("b1")) == {
        "new_entities",
        "new_glossary_items",
        "surface_updates",
        "tickets",
    }
    assert set(_required_shape("auditor")) == {
        "ticket_dispositions",
        "profile_revisions",
    }


def test_v4_b0_is_advisory_prose_not_identity_authority() -> None:
    prompt = load_registry_prompt_v4(DESIGN_DOC, "b0")
    shape = _required_shape("b0")
    keys = _all_keys(shape)

    assert "at most 220 words" in prompt
    assert "not a block-range assignment or an exact cover" in prompt
    assert "ordinary one-off props" in prompt
    assert "neither an exhaustive entity inventory, a checklist B1 must cover" in prompt
    assert "nor a ceiling on B1 discovery" in prompt
    assert "must not state or imply an entity kind" in prompt
    assert set(shape["attention_items"][0]) == {
        "surface",
        "source_block_ids",
        "why_noticed",
    }
    assert set(shape["narrative_context"]) == {
        "mode",
        "note",
        "support_block_ids",
    }
    assert keys.isdisjoint(
        {
            "entity_id",
            "referent_kind_claim",
            "glossary_category",
            "expected_action",
            "evidence_quote",
            "anchor_text",
            "occurrence_hint",
            "scene_range",
            "confidence",
        }
    )


def test_v4_b1_is_narrow_delta_with_prejoined_non_authoritative_context() -> None:
    prompt = load_registry_prompt_v4(DESIGN_DOC, "b1")
    shape = _required_shape("b1")
    keys = _all_keys(shape)

    assert "Emit a stable registry DELTA, not an occurrence inventory" in prompt
    assert "exactly one candidate never proves identity" in prompt
    assert "Do not acknowledge, dismiss, exact-cover, or cite attention ids" in prompt
    assert "Each packet keeps one active source surface" in prompt
    assert "If an existing compatible row needs no stable change, emit nothing" in prompt
    assert "Four empty lists are valid after reading" in prompt
    assert set(shape["new_entities"][0]) == {
        "surface",
        "name_class",
        "referent_kind_claim",
        "identity_summary",
        "referential_gender_claim",
        "source_block_ids",
        "initial_surface_updates",
    }
    assert set(shape["surface_updates"][0]) == {
        "update_kind",
        "surface",
        "target_entity_id",
        "name_class",
        "source_block_ids",
        "reason",
    }
    assert keys.isdisjoint(
        {
            "occurrence_id",
            "evidence_quote",
            "anchor_text",
            "occurrence_hint",
            "offset",
            "span",
            "event_id",
            "speaker_ref",
            "tool_calls",
            "retrieval_requests",
            "attention_dispositions",
            "confidence",
        }
    )


def test_v4_b1_gender_is_optional_source_addressed_and_time_scoped_facts_are_excluded() -> None:
    prompt = load_registry_prompt_v4(DESIGN_DOC, "b1")
    shape = _required_shape("b1")

    assert "referential_gender_claim is optional" in prompt
    assert "only the exact active blocks that support this facet" in prompt
    assert "An explicit neutral value is not the same as absence" in prompt
    assert "must not contain exact age, age band, temporary mood" in prompt
    assert set(shape["new_entities"][0]["referential_gender_claim"]) == {
        "value",
        "support_block_ids",
    }
    assert {"proposed_identity_summary", "proposed_referential_gender"} <= set(
        shape["tickets"][0]
    )


def test_v4_auditor_exact_covers_exceptions_and_consolidates_profile_once() -> None:
    prompt = load_registry_prompt_v4(DESIGN_DOC, "auditor")
    shape = _required_shape("auditor")

    assert "ticket_dispositions must exact-cover" in prompt
    assert "do not receive the B0 attention inventory" in prompt
    assert "at most one profile revision per target entity" in prompt
    assert "Every promotion and merge still passes the independent commit-time alias gate" in prompt
    assert set(shape["ticket_dispositions"][0]) == {
        "ticket_id",
        "action",
        "source_entity_id",
        "target_entity_id",
        "source_glossary_id",
        "target_glossary_id",
        "resolved_referent_kind",
        "name_class",
        "valid_block_ids",
        "resolution_note",
    }
    assert set(shape["profile_revisions"][0]) == {
        "target_entity_id",
        "source_ticket_ids",
        "referent_kind_update",
        "identity_summary_update",
        "referential_gender_update",
        "resolution_note",
    }


def test_v4_prompts_are_book_neutral_and_contain_no_keys_or_answer_ids() -> None:
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
        "ent2_",
        "reggen2_",
        "sk-",
        "aiza",
        "aq.a",
    )
    for role in PROMPT_IDS:
        lowered = load_registry_prompt_v4(DESIGN_DOC, role).casefold()
        assert all(value not in lowered for value in forbidden)


def test_v4_prompt_contract_has_no_live_provider_or_store_wiring() -> None:
    module_path = RUNTIME_ROOT / "pipeline" / "literary" / "chapter_registry_prompts_v4.py"
    source = module_path.read_text(encoding="utf-8")
    forbidden = (
        "judge_client",
        "openai",
        "gemini",
        "requests",
        "socket",
        "sqlite3",
        "run_chapter_registry",
        "publish",
        "write_text",
        "write_bytes",
    )
    assert all(value not in source.casefold() for value in forbidden)
