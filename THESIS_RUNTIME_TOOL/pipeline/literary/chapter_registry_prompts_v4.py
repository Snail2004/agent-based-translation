"""Prompt-only contract for the chapter-registry v4 Phase-A gate.

This module deliberately has no provider, runtime, schema, or store wiring.
It pins reviewed prompt identities and bytes before the typed scaffold exists.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash


PROMPT_CONTRACT_VERSION = "chapter_registry_prompts_v4_1_b0"
PROMPT_IDS = {
    "b0": "literary_chapter_orient_v4_1",
    "b1": "literary_stable_registry_delta_v4",
    "auditor": "literary_registry_exception_audit_v4",
}

# Filled only after the reviewed design-doc bytes are finalized. Any byte edit
# requires a new prompt ID as well as a new digest.
PROMPT_SHA256 = {
    "b0": "dab38163d3f56cda0e91058d61e28c86b0bfd3ff17e9f77c26cc6d2f11a9addd",
    "b1": "f6f022c0b2a04ecffd810a26b4d878f5f2540aa5fcc91881d0dc307cef499d95",
    "auditor": "9e7166c0b9828812f24db84a9bedad2b6da0110fb4b19b4979131959f4b8e637",
}


class RegistryPromptV4Error(ValueError):
    """Raised when the prompt-only v4 contract does not match reviewed bytes."""


def load_registry_prompt_v4(design_doc: Path, role: str) -> str:
    if role not in PROMPT_IDS:
        raise RegistryPromptV4Error(f"unknown registry v4 prompt role: {role}")
    prompt_id = PROMPT_IDS[role]
    text = load_system_prompt_from_design(Path(design_doc), prompt_id)
    marker = f"Prompt version: {prompt_id}."
    if marker not in text:
        raise RegistryPromptV4Error(f"prompt marker mismatch: {prompt_id}")
    observed = sha256(text.encode("utf-8")).hexdigest()
    expected = PROMPT_SHA256[role]
    if observed != expected:
        raise RegistryPromptV4Error(
            f"prompt digest mismatch for {prompt_id}: expected={expected}, observed={observed}"
        )
    return text


def prompt_manifest_v4(design_doc: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for role in ("b0", "b1", "auditor"):
        text = load_registry_prompt_v4(design_doc, role)
        rows.append(
            {
                "role": role,
                "prompt_id": PROMPT_IDS[role],
                "sha256": PROMPT_SHA256[role],
                "utf8_bytes": len(text.encode("utf-8")),
            }
        )
    payload = {"contract_version": PROMPT_CONTRACT_VERSION, "prompts": rows}
    return {**payload, "manifest_hash": canonical_hash(payload)}


__all__ = [
    "PROMPT_CONTRACT_VERSION",
    "PROMPT_IDS",
    "PROMPT_SHA256",
    "RegistryPromptV4Error",
    "load_registry_prompt_v4",
    "prompt_manifest_v4",
]
