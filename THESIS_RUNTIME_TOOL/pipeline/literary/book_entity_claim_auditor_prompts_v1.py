"""Pinned prompt contract for bounded cross-chapter prior-claim review."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash


PROMPT_CONTRACT_VERSION = "book_entity_claim_auditor_prompts_v2"
PROMPT_ID = "literary_cross_chapter_prior_claim_auditor_v2"
PROMPT_SHA256 = "de722f0dc9b06a3f53928c3589ef3e284864ee2fce3795481063ef256b5ff1fd"
PROMPT_UTF8_BYTES = 3401


class BookEntityClaimPromptError(ValueError):
    """Raised when reviewed prompt bytes drift from their pinned identity."""


def load_book_entity_claim_prompt_v1(design_doc: Path) -> str:
    text = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    if f"Prompt version: {PROMPT_ID}." not in text:
        raise BookEntityClaimPromptError("prior-claim prompt marker mismatch")
    encoded = text.encode("utf-8")
    observed = sha256(encoded).hexdigest()
    if observed != PROMPT_SHA256 or len(encoded) != PROMPT_UTF8_BYTES:
        raise BookEntityClaimPromptError(
            "prior-claim prompt bytes differ from the reviewed contract"
        )
    return text


def claim_prompt_manifest_v1(design_doc: Path) -> dict[str, Any]:
    text = load_book_entity_claim_prompt_v1(design_doc)
    body = {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "prompt_id": PROMPT_ID,
        "sha256": PROMPT_SHA256,
        "utf8_bytes": len(text.encode("utf-8")),
    }
    return {**body, "manifest_hash": canonical_hash(body)}


__all__ = [
    "BookEntityClaimPromptError",
    "PROMPT_CONTRACT_VERSION",
    "PROMPT_ID",
    "PROMPT_SHA256",
    "PROMPT_UTF8_BYTES",
    "claim_prompt_manifest_v1",
    "load_book_entity_claim_prompt_v1",
]
