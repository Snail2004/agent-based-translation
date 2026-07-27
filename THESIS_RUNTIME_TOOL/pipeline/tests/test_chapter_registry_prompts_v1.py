from __future__ import annotations

import json
from pathlib import Path

from pipeline.literary.builder_pilot import (
    load_system_prompt_for_chapter,
    load_system_prompt_from_design,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"

PROMPT_IDS = (
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


def _required_shape(prompt_id: str) -> dict[str, object]:
    prompt = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
    prefix = "- Required JSON shape: "
    matches = [line[len(prefix) :] for line in prompt.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1
    return json.loads(matches[0])


def test_prompt_loader_returns_exact_reviewed_blockquote_without_bleed() -> None:
    for prompt_id in PROMPT_IDS:
        loaded = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
        assert loaded == _reviewed_blockquote(prompt_id)
        assert loaded.count(f"Prompt version: {prompt_id}") == 1
        assert all(other not in loaded for other in PROMPT_IDS if other != prompt_id)


def test_prompt_examples_are_parseable_and_have_closed_top_level_shapes() -> None:
    orient = _required_shape("literary_chapter_orient_v1")
    extract = _required_shape("literary_registry_extract_v1")
    resolve = _required_shape("literary_registry_resolve_v1")

    assert set(orient) == {
        "chapter_id",
        "gist",
        "setting_notes",
        "narrator_hypotheses",
        "salient_surface_checklist",
    }
    assert set(extract) == {
        "chapter_id",
        "window_block_ids",
        "context_only_used",
        "character_mentions",
        "glossary_candidates",
    }
    assert set(resolve) == {
        "chapter_id",
        "request_id",
        "owned_occurrence_ids",
        "existing_attachments",
        "new_partitions",
        "pending",
        "context_requests",
    }


def test_prompts_are_book_neutral_and_chapter_placeholder_is_runtime_rendered() -> None:
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


def test_chapter_orientation_is_coverage_only_not_identity_authority() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_chapter_orient_v1")
    shape = _required_shape("literary_chapter_orient_v1")

    assert "NOT an identity pass" in prompt
    assert "COVERAGE CHECKLIST" in prompt
    assert "Never flatten uncertainty" in prompt
    assert "Do not include a bare pronoun" in prompt
    assert "anchor_text MUST equal surface exactly" in prompt
    assert set(shape["salient_surface_checklist"][0]) == {
        "surface",
        "salience_note",
        "anchor_text",
        "evidence_quote",
        "block_id",
        "occurrence_hint",
    }
    serialized = json.dumps(shape, sort_keys=True)
    assert "entity_id" not in serialized
    assert "candidate_id" not in serialized
    assert "scene_range" not in serialized
    assert "referent_kind_claim" not in serialized


def test_round_zero_extract_cannot_mint_or_route_and_must_fail_closed() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_registry_extract_v1")
    shape = _required_shape("literary_registry_extract_v1")
    mention = shape["character_mentions"][0]
    proposal = mention["identity_proposal"]
    request = mention["context_requests"][0]

    assert "Candidate cards are untrusted possibilities" in prompt
    assert "Round 0 MUST NOT output propose_new_entity" in prompt
    assert "You do not receive the full registry" in prompt
    assert "chapter-orientation cast/checklist" in prompt
    assert "A prior candidate never permits you to omit a fresh occurrence" in prompt
    assert "canonical_surface_candidate is always null in Round 0" in prompt
    assert "propose_new_entity" not in proposal["operation"]
    assert proposal["retrieval_trace_ids"] == []
    assert "routing_disposition" not in mention
    assert "mention_id" not in mention
    assert "as_of_position" not in request
    assert set(shape["glossary_candidates"][0]) == {
        "source_term",
        "proposed_target_vi",
        "category",
        "do_not_translate",
        "block_ids",
    }


def test_resolver_exact_cover_contract_has_no_model_minted_partition_id() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, "literary_registry_resolve_v1")
    shape = _required_shape("literary_registry_resolve_v1")
    new_partition = shape["new_partitions"][0]

    assert "MUST form an exact cover" in prompt
    assert "never mint an id" in prompt
    assert "Same spelling" in prompt
    assert "retrieval-round cap is exhausted" in prompt
    assert "commits the chapter atomically" in prompt
    assert "entity_id" not in new_partition
    assert "partition_id" not in new_partition
    assert "local_partition_key" not in new_partition
    assert set(new_partition) == {
        "occurrence_ids",
        "referent_kind_claim",
        "canonical_surface_candidate",
        "alias_surfaces",
        "reason_code",
        "binding_evidence_refs",
        "retrieval_trace_ids",
        "rejected_candidate_entity_ids",
        "rejected_pending_ids",
    }
