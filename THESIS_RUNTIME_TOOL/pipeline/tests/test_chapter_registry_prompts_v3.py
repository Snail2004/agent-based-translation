from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_schema_v3 import PROMPT_IDS, response_json_schema


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


def _required_shape(prompt_id: str) -> dict[str, Any]:
    prompt = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
    prefix = "- Required JSON shape: "
    values = [line[len(prefix) :] for line in prompt.splitlines() if line.startswith(prefix)]
    assert len(values) == 1
    return json.loads(values[0])


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_v3_loader_bytes_equal_reviewed_bytes_and_hashes_are_stable() -> None:
    loaded_prompts: dict[str, str] = {}
    for role, prompt_id in PROMPT_IDS.items():
        loaded = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
        assert loaded == _reviewed_blockquote(prompt_id)
        assert loaded.count(f"Prompt version: {prompt_id}") == 1
        assert hashlib.sha256(loaded.encode("utf-8")).hexdigest()
        loaded_prompts[role] = loaded
    for role, loaded in loaded_prompts.items():
        assert all(
            other_id not in loaded
            for other_role, other_id in PROMPT_IDS.items()
            if other_role != role
        )


def test_v3_prompt_examples_match_closed_runtime_schema() -> None:
    b0 = _required_shape(PROMPT_IDS["b0"])
    b1 = _required_shape(PROMPT_IDS["b1"])
    auditor = _required_shape(PROMPT_IDS["auditor"])

    assert set(b0) == {"gist", "narrator_hypotheses", "salient_registry_checklist"}
    assert set(b1) == {"new_entities", "new_glossary_items", "tickets"}
    assert set(auditor) == {"ticket_dispositions"}
    for role, example in (("b0", b0), ("b1", b1), ("auditor", auditor)):
        schema = response_json_schema(role)
        assert set(example) == set(schema["required"])


def test_v3_b1_is_stable_delta_only_and_has_no_contextual_binding_contract() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_IDS["b1"])
    shape = _required_shape(PROMPT_IDS["b1"])
    keys = _all_keys(shape)

    assert "Candidate packets are mechanical retrieval hints, not identity answers" in prompt
    assert "exactly one candidate never proves identity" in prompt
    assert "Build a stable registry DELTA, not an occurrence inventory" in prompt
    assert "Bare pronouns, forms of address, reusable roles" in prompt
    assert "Never merge, bind, or author an alias" in prompt
    assert "Three empty lists are valid only after reading" in prompt
    assert keys.isdisjoint(
        {
            "alias_id",
            "local_bindings",
            "occurrence_id",
            "anchor_text",
            "evidence_quote",
            "occurrence_hint",
            "event_id",
            "speaker_ref",
            "tool_calls",
            "confidence",
        }
    )


def test_v3_auditor_is_ticket_exact_cover_and_alias_gate_is_independent() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_IDS["auditor"])
    shape = _required_shape(PROMPT_IDS["auditor"])

    assert "ticket_dispositions must exact-cover" in prompt
    assert "Every promotion and merge still passes the independent commit-time alias gate" in prompt
    assert "defer_to_b2" in prompt
    assert "zero global aliases" in prompt
    assert set(shape["ticket_dispositions"][0]) == {
        "ticket_id",
        "action",
        "source_entity_id",
        "target_entity_id",
        "source_glossary_id",
        "target_glossary_id",
        "resolved_referent_kind",
        "revised_identity_summary",
        "name_class",
        "resolution_note",
    }


def test_v3_prompts_are_book_neutral_and_contain_no_answer_ids_or_keys() -> None:
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
    )
    for prompt_id in PROMPT_IDS.values():
        lowered = load_system_prompt_from_design(DESIGN_DOC, prompt_id).casefold()
        assert all(value not in lowered for value in forbidden)
