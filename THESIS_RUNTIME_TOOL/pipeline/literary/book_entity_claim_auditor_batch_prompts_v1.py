"""Pinned prompt contract for batched prior-claim review."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash


PROMPT_CONTRACT_VERSION = "book_entity_claim_auditor_batch_prompts_v2"
PROMPT_ID = "literary_cross_chapter_prior_claim_auditor_batch_v2"
PROMPT_SHA256 = "94ca0d0b8062796e02e6f416198247a2b178809bf7aa71c207c7dc50f0713c71"
PROMPT_UTF8_BYTES = 3562


class BookEntityClaimBatchPromptError(ValueError):
    """Raised when reviewed batch-prompt bytes drift from their identity."""


def load_book_entity_claim_batch_prompt_v1(design_doc: Path) -> str:
    text = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    if f"Prompt version: {PROMPT_ID}." not in text:
        raise BookEntityClaimBatchPromptError("prior-claim batch prompt marker mismatch")
    encoded = text.encode("utf-8")
    observed = sha256(encoded).hexdigest()
    if observed != PROMPT_SHA256 or len(encoded) != PROMPT_UTF8_BYTES:
        raise BookEntityClaimBatchPromptError(
            "prior-claim batch prompt bytes differ from the reviewed contract"
        )
    return text


def claim_batch_prompt_manifest_v1(design_doc: Path) -> dict[str, Any]:
    text = load_book_entity_claim_batch_prompt_v1(design_doc)
    body = {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "prompt_id": PROMPT_ID,
        "sha256": PROMPT_SHA256,
        "utf8_bytes": len(text.encode("utf-8")),
    }
    return {**body, "manifest_hash": canonical_hash(body)}


__all__ = [
    "BookEntityClaimBatchPromptError",
    "PROMPT_CONTRACT_VERSION",
    "PROMPT_ID",
    "PROMPT_SHA256",
    "PROMPT_UTF8_BYTES",
    "claim_batch_prompt_manifest_v1",
    "load_book_entity_claim_batch_prompt_v1",
]
