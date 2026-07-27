from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pipeline.literary.book_entity_claim_auditor_prompts_v1 import (
    BookEntityClaimPromptError,
    PROMPT_ID,
    PROMPT_SHA256,
    PROMPT_UTF8_BYTES,
    claim_prompt_manifest_v1,
    load_book_entity_claim_prompt_v1,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def test_prior_claim_prompt_bytes_are_pinned_and_loader_faithful() -> None:
    prompt = load_book_entity_claim_prompt_v1(DESIGN_DOC)
    assert prompt == load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    encoded = prompt.encode("utf-8")
    assert len(encoded) == PROMPT_UTF8_BYTES
    assert hashlib.sha256(encoded).hexdigest() == PROMPT_SHA256
    assert prompt.count(f"Prompt version: {PROMPT_ID}.") == 1


def test_prior_claim_prompt_is_bounded_exact_cover_and_has_no_id_power() -> None:
    prompt = load_book_entity_claim_prompt_v1(DESIGN_DOC).casefold()
    assert "exactly one" in prompt
    assert "exact-cover" in prompt
    assert "not the whole book" in prompt
    assert "never invent" in prompt
    assert "persistent entity id" in prompt
    assert "refer_identity_conflict" in prompt


def test_prior_claim_prompt_is_book_neutral_and_has_no_credentials() -> None:
    prompt = load_book_entity_claim_prompt_v1(DESIGN_DOC).casefold()
    forbidden = (
        "heathcliff",
        "catherine",
        "lockwood",
        "gatsby",
        "joseph",
        "madam",
        "ent2_",
        "sk-",
        "aiza",
        "aq.a",
    )
    assert all(value not in prompt for value in forbidden)


def test_prior_claim_prompt_manifest_is_content_addressed() -> None:
    manifest = claim_prompt_manifest_v1(DESIGN_DOC)
    assert manifest["prompt_id"] == PROMPT_ID
    assert manifest["sha256"] == PROMPT_SHA256
    assert manifest["utf8_bytes"] == PROMPT_UTF8_BYTES
    assert len(manifest["manifest_hash"]) == 64


def test_prior_claim_prompt_drift_is_rejected(tmp_path: Path) -> None:
    changed = tmp_path / "design.md"
    changed.write_text(
        DESIGN_DOC.read_text(encoding="utf-8").replace(
            (
                "Evidence roles such as anchor, bridge, direct, and neighbour "
                "describe deterministic retrieval only; they are not conclusions."
            ),
            "Evidence roles describe retrieval only.",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BookEntityClaimPromptError, match="bytes differ"):
        load_book_entity_claim_prompt_v1(changed)
