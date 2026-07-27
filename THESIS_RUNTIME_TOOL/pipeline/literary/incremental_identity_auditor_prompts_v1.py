"""Pinned prompt contract for reversible chapter-cycle identity review."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash


PROMPT_CONTRACT_VERSION = "incremental_identity_auditor_prompts_v2"
PROMPT_ID = "literary_incremental_identity_surface_auditor_v2"
PROMPT_SHA256 = "da79451910d1318d58db8fbc0d0a9e26701a088877d002dd01774a4742e35e4e"
PROMPT_UTF8_BYTES = 4536


class IncrementalIdentityPromptError(ValueError):
    """Raised when reviewed prompt bytes drift from their pinned identity."""


def load_incremental_identity_prompt_v1(design_doc: Path) -> str:
    text = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    if f"Prompt version: {PROMPT_ID}." not in text:
        raise IncrementalIdentityPromptError("incremental identity prompt marker mismatch")
    encoded = text.encode("utf-8")
    observed = sha256(encoded).hexdigest()
    if observed != PROMPT_SHA256 or len(encoded) != PROMPT_UTF8_BYTES:
        raise IncrementalIdentityPromptError(
            "incremental identity prompt bytes differ from the reviewed contract"
        )
    return text


def prompt_manifest_v1(design_doc: Path) -> dict[str, Any]:
    text = load_incremental_identity_prompt_v1(design_doc)
    body = {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "prompt_id": PROMPT_ID,
        "sha256": PROMPT_SHA256,
        "utf8_bytes": len(text.encode("utf-8")),
    }
    return {**body, "manifest_hash": canonical_hash(body)}


__all__ = [
    "IncrementalIdentityPromptError",
    "PROMPT_CONTRACT_VERSION",
    "PROMPT_ID",
    "PROMPT_SHA256",
    "PROMPT_UTF8_BYTES",
    "load_incremental_identity_prompt_v1",
    "prompt_manifest_v1",
]
