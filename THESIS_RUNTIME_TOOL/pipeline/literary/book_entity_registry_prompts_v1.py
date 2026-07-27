"""Pinned prompt contract for bounded cross-chapter identity review."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash


PROMPT_CONTRACT_VERSION = "book_entity_registry_prompts_v1"
PROMPT_ID = "literary_cross_chapter_entity_auditor_v1"
PROMPT_SHA256 = "6a24fbdc159082b98782384b9321ede86d2f6dd5fa9cc87ac38dc8ae26c62549"
PROMPT_UTF8_BYTES = 3710


class BookEntityPromptError(ValueError):
    """Raised when reviewed prompt bytes drift from their pinned identity."""


def load_book_entity_prompt_v1(design_doc: Path) -> str:
    text = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    if f"Prompt version: {PROMPT_ID}." not in text:
        raise BookEntityPromptError("cross-chapter prompt marker mismatch")
    encoded = text.encode("utf-8")
    observed = sha256(encoded).hexdigest()
    if observed != PROMPT_SHA256 or len(encoded) != PROMPT_UTF8_BYTES:
        raise BookEntityPromptError(
            "cross-chapter prompt bytes differ from the reviewed contract"
        )
    return text


def prompt_manifest_v1(design_doc: Path) -> dict[str, Any]:
    text = load_book_entity_prompt_v1(design_doc)
    body = {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "prompt_id": PROMPT_ID,
        "sha256": PROMPT_SHA256,
        "utf8_bytes": len(text.encode("utf-8")),
    }
    return {**body, "manifest_hash": canonical_hash(body)}


__all__ = [
    "BookEntityPromptError",
    "PROMPT_CONTRACT_VERSION",
    "PROMPT_ID",
    "PROMPT_SHA256",
    "PROMPT_UTF8_BYTES",
    "load_book_entity_prompt_v1",
    "prompt_manifest_v1",
]
