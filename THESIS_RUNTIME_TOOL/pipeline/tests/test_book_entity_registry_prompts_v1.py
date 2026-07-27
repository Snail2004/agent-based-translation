from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pipeline.literary.book_entity_registry_prompts_v1 import (
    BookEntityPromptError,
    PROMPT_ID,
    PROMPT_SHA256,
    PROMPT_UTF8_BYTES,
    load_book_entity_prompt_v1,
    prompt_manifest_v1,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def test_cross_chapter_prompt_bytes_are_pinned_and_loader_faithful() -> None:
    prompt = load_book_entity_prompt_v1(DESIGN_DOC)
    assert prompt == load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    assert len(prompt.encode("utf-8")) == PROMPT_UTF8_BYTES
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == PROMPT_SHA256
    assert prompt.count(f"Prompt version: {PROMPT_ID}.") == 1


def test_cross_chapter_prompt_is_one_component_and_has_no_persistent_id_power() -> None:
    prompt = load_book_entity_prompt_v1(DESIGN_DOC)
    lowered = prompt.casefold()
    assert "exactly one" in lowered
    assert "never invent" in lowered
    assert "split never creates a confirmed entity" in lowered
    assert "bind_global proves at most chapter-scoped" in prompt
    assert "whole book" in lowered
    assert "persistent entity id" in lowered


def test_cross_chapter_prompt_is_book_neutral_and_has_no_credentials() -> None:
    lowered = load_book_entity_prompt_v1(DESIGN_DOC).casefold()
    forbidden = (
        "heathcliff",
        "catherine",
        "lockwood",
        "gatsby",
        "madam",
        "juno",
        "ent2_",
        "sk-",
        "aiza",
        "aq.a",
    )
    assert all(value not in lowered for value in forbidden)


def test_prompt_manifest_is_content_addressed() -> None:
    manifest = prompt_manifest_v1(DESIGN_DOC)
    assert manifest["prompt_id"] == PROMPT_ID
    assert manifest["sha256"] == PROMPT_SHA256
    assert manifest["utf8_bytes"] == PROMPT_UTF8_BYTES
    assert len(manifest["manifest_hash"]) == 64


def test_prompt_drift_is_rejected(tmp_path: Path) -> None:
    changed = tmp_path / "design.md"
    changed.write_text(
        DESIGN_DOC.read_text(encoding="utf-8").replace(
            "Repetition is provenance, not a vote.",
            "Repetition is not authority.",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BookEntityPromptError, match="bytes differ"):
        load_book_entity_prompt_v1(changed)
