from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence
from xml.etree import ElementTree as ET

from pipeline.agents.llm_client import LLMClient, LLMResult, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.checkpoint import (
    CheckpointLock,
    artifact_manifest,
    build_checkpoint,
    canonical_hash,
    chapter_source_hash,
    config_hash,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint_atomic,
)
from pipeline.prepass.runner import build_d2l_prepass_windows


PROMPT_SOURCE = "design/LITERARY_PROMPT_DESIGN.md"
BRIEF_VERSION = "literary_chapter_brief_v1"
LEXICON_VERSION = "literary_lexicon_v1"
NARRATIVE_VERSION = "literary_narrative_v1"
DIGEST_VERSION = "literary_digest_v1"
CONSOLIDATE_VERSION = "literary_consolidate_v1"
RESPONSE_FORMAT_JSON = {"type": "json_object"}
M1_CHECKPOINT_SCHEMA_VERSION = "literary_m1_checkpoint_v1"
M2_CHECKPOINT_SCHEMA_VERSION = "literary_m2_checkpoint_v1"
NEIGHBOR_SUMMARY_K = 2
PACK_POLICY_VERSION = "literary_registry_pack_v1"

GLOSSARY_CATEGORIES = {"place", "object", "cultural", "other"}
MENTION_TYPES = {"name", "nickname", "descriptor"}
RESOLUTION_STATUSES = {"named", "candidate", "unknown"}
REFERENCE_KINDS = {"person", "group", "narrator", "reader", "unknown"}
ATTRIBUTION_METHODS = {
    "explicit_tag",
    "turn_alternation",
    "nearby_context",
    "narrator_inference",
    "unspecified",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
BRIEF_TIME_FRAME_HINTS = {"frame_present", "past_recollection", "unclear"}
BRIEF_SCENE_SHAPES = {"single_scene_one_location", "few_scenes", "many_scenes_or_travel"}
BRIEF_SURFACE_KINDS = {"proper_name", "descriptor"}
BRIEF_LEAK_TOKENS = {
    "friend",
    "friends",
    "enemy",
    "enemies",
    "rival",
    "lover",
    "ally",
    "allies",
    "betray",
    "reconcile",
    "hate",
    "love",
    "trust",
    "distrust",
}
DIGEST_STORY_TIME = {"frame_present", "retrospective_past", "embedded_flashback"}
DIGEST_CHANGE_ATTRIBUTES = {"social_status", "alias_or_title", "life_status", "residence"}
DIGEST_OBSERVED_SCOPE = {"this_chapter"}
DIGEST_THREAD_KINDS = {"mystery", "pending_transition", "question"}
DIGEST_VALENCE_HINTS = {"positive", "negative", "mixed", "unclear"}
DIGEST_FACT_TYPES = {"narrator", "register", "speech_style", "status", "setting"}
PHASE_LABELS = {
    "allied",
    "friendly",
    "neutral",
    "strained",
    "hostile",
    "estranged",
    "dependent",
    "reconciled",
}
ADDRESS_EVIDENCE_LEVELS = {"observed", "inferred", "unsupported"}
GROUP_REFERENCE_KINDS = {"group", "narrator", "reader"}
PLAIN_PRONOUNS = {
    "i",
    "me",
    "my",
    "mine",
    "myself",
    "you",
    "your",
    "yours",
    "yourself",
    "he",
    "him",
    "his",
    "himself",
    "she",
    "her",
    "hers",
    "herself",
    "it",
    "its",
    "itself",
    "we",
    "us",
    "our",
    "ours",
    "ourselves",
    "they",
    "them",
    "their",
    "theirs",
    "themselves",
}
PHASE_LEAK_EVENT_TYPES = {
    "ally",
    "allied",
    "enemy",
    "enemies",
    "friend",
    "friendly",
    "hostile",
    "hostility",
    "rival",
    "rivalry",
    "strained",
    "estranged",
    "reconciled",
    "dependent",
    "relationship",
    "phase",
}


@dataclass(frozen=True)
class ValidationReport:
    name: str
    ok: bool
    errors: list[str]
    warnings: list[str]
    counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LiteraryWindow:
    window_id: str
    chapter_id: str
    blocks: list[dict[str, Any]]
    previous_tail: list[dict[str, Any]]
    next_tail: list[dict[str, Any]]
    est_src_tokens: int

    @property
    def block_ids(self) -> list[str]:
        return [str(block["block_id"]) for block in self.blocks]


@dataclass(frozen=True)
class EntityResolver:
    old_to_new: dict[str, str]
    surface_to_new: dict[str, str]
    narrator_entity_id: str | None


@dataclass(frozen=True)
class CallRecord:
    mode: str
    window_id: str
    tag: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int
    from_cache: bool
    cache_key: str
    validation: ValidationReport

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["validation"] = self.validation.to_dict()
        return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_wuthering_heights_epub(epub_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load Gutenberg #768 EPUB into the stripped document.json shape."""

    return load_literary_epub(
        epub_path,
        doc_id="wuthering_heights",
        title="Wuthering Heights",
        author="Emily Bronte",
        source="Project Gutenberg #768 EPUB3",
        chapter_prefix="wh_ch",
        expected_chapters=34,
    )


def load_great_gatsby_epub(epub_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load Gutenberg #64317 EPUB into the stripped document.json shape."""

    return load_literary_epub(
        epub_path,
        doc_id="great_gatsby",
        title="The Great Gatsby",
        author="F. Scott Fitzgerald",
        source="Project Gutenberg #64317 EPUB3",
        chapter_prefix="gg_ch",
        expected_chapters=9,
    )


def load_literary_epub(
    epub_path: Path,
    *,
    doc_id: str,
    title: str,
    author: str,
    source: str,
    chapter_prefix: str,
    expected_chapters: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a Gutenberg EPUB into the stripped document.json shape.

    Supports both one-chapter-per-file EPUBs and multi-chapter XHTML files with
    TOC anchors, which is required for The Great Gatsby.
    """

    epub_path = Path(epub_path)
    with zipfile.ZipFile(epub_path) as archive:
        opf_path = _opf_path(archive)
        base = PurePosixPath(opf_path).parent
        toc_path = str(base / "toc.xhtml")
        toc_items = _toc_chapter_items(archive.read(toc_path).decode("utf-8"), toc_path)
        if expected_chapters is not None and len(toc_items) != expected_chapters:
            raise ValueError(f"Expected {expected_chapters} chapter TOC entries, found {len(toc_items)}")
        chapters: list[dict[str, Any]] = []
        mapping: list[dict[str, Any]] = []
        global_order = 0
        for idx, item in enumerate(toc_items, start=1):
            href_file, href_fragment = _split_href(item["href"])
            href = str((base / href_file).as_posix())
            next_fragment = None
            if idx < len(toc_items):
                next_file, candidate_next_fragment = _split_href(toc_items[idx]["href"])
                if next_file == href_file:
                    next_fragment = candidate_next_fragment
            xhtml = archive.read(href).decode("utf-8")
            chapter_xhtml = _slice_xhtml_fragment(
                xhtml,
                start_fragment=href_fragment,
                stop_fragment=next_fragment,
                source_href=href,
            )
            chapter_id = f"{chapter_prefix}{idx:02d}"
            chapter_blocks = _blocks_from_xhtml(
                chapter_xhtml,
                chapter_id=chapter_id,
                order_start=global_order,
            )
            global_order += len(chapter_blocks)
            chapters.append(
                {
                    "chapter_id": chapter_id,
                    "title": item["label"],
                    "blocks": chapter_blocks,
                }
            )
            mapping.append(
                {
                    "unit_id": chapter_id,
                    "narrative_chapter": idx,
                    "chapter_label": item["label"],
                    "volume": None,
                    "source_href": href,
                    "block_count": len(chapter_blocks),
                    "first_block_id": chapter_blocks[0]["block_id"] if chapter_blocks else None,
                    "last_block_id": chapter_blocks[-1]["block_id"] if chapter_blocks else None,
                }
            )

    document = {
        "doc_id": doc_id,
        "metadata": {
            "title": title,
            "author": author,
            "source": source,
            "source_path": str(epub_path),
            "source_sha256": sha256_file(epub_path),
            "source_language": "en",
            "target_language": "vi",
            "chapter_prefix": chapter_prefix,
        },
        "chapters": chapters,
    }
    return document, mapping


def select_chapters(document: dict[str, Any], chapters: list[str]) -> list[dict[str, Any]]:
    wanted = {_normalize_chapter_arg(chapter, document=document) for chapter in chapters}
    selected = [
        chapter
        for chapter in document.get("chapters") or []
        if str(chapter.get("chapter_id") or "") in wanted
    ]
    missing = wanted - {str(chapter.get("chapter_id") or "") for chapter in selected}
    if missing:
        raise ValueError(f"Missing requested chapters: {sorted(missing)}")
    return selected


def build_window_manifest(
    chapters: list[dict[str, Any]],
    *,
    window_target_tokens: int = 500,
    window_max_blocks: int = 8,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        windows = build_literary_windows(
            chapter,
            target_tokens=window_target_tokens,
            max_blocks=window_max_blocks,
        )
        for window in windows:
            rows.append(
                {
                    "chapter_id": window.chapter_id,
                    "window_id": window.window_id,
                    "block_ids": window.block_ids,
                    "previous_tail_block_ids": [
                        str(block["block_id"]) for block in window.previous_tail
                    ],
                    "next_tail_block_ids": [
                        str(block["block_id"]) for block in window.next_tail
                    ],
                    "est_src_tokens": window.est_src_tokens,
                    "mode_calls": [LEXICON_VERSION, NARRATIVE_VERSION],
                }
            )
    return rows


def build_literary_windows(
    chapter: dict[str, Any],
    *,
    target_tokens: int = 500,
    max_blocks: int = 8,
) -> list[LiteraryWindow]:
    base_windows = build_d2l_prepass_windows(
        chapter,
        target_tokens=target_tokens,
        max_blocks=max_blocks,
    )
    blocks_by_id = {str(block["block_id"]): block for block in chapter.get("blocks") or []}
    ordered_blocks = sorted(
        [block for block in chapter.get("blocks") or [] if block.get("block_id")],
        key=lambda block: int(block.get("order_index") or 0),
    )
    index_by_id = {str(block["block_id"]): idx for idx, block in enumerate(ordered_blocks)}
    windows: list[LiteraryWindow] = []
    for window in base_windows:
        first = index_by_id[str(window.blocks[0]["block_id"])]
        last = index_by_id[str(window.blocks[-1]["block_id"])]
        previous_tail = ordered_blocks[max(0, first - 2) : first]
        next_tail = ordered_blocks[last + 1 : min(len(ordered_blocks), last + 3)]
        windows.append(
            LiteraryWindow(
                window_id=window.window_id,
                chapter_id=window.chapter_id,
                blocks=[blocks_by_id[str(block["block_id"])] for block in window.blocks],
                previous_tail=previous_tail,
                next_tail=next_tail,
                est_src_tokens=window.est_src_tokens,
            )
        )
    return windows


def render_block_markers(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{block['block_id']}] {block.get('clean_text') or block.get('source_text') or ''}"
        for block in blocks
    )


def neighbor_summaries_for_index(
    summaries: list[dict[str, str]],
    current_index: int,
    *,
    k: int = 2,
) -> list[dict[str, str]]:
    """Return the bounded K-nearest prior chapter gists for a chapter index."""

    if current_index <= 0 or k <= 0:
        return []
    start = max(0, current_index - k)
    return summaries[start:current_index]


def render_neighbor_summaries(summaries: list[dict[str, str]]) -> str:
    if not summaries:
        return "(none)"
    rendered: list[str] = []
    for item in summaries:
        chapter_id = str(item.get("chapter_id") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not chapter_id or not summary:
            continue
        rendered.append(f"{chapter_id}\n{summary}")
    return "\n\n".join(rendered) if rendered else "(none)"


def build_dry_run_artifacts(document: dict[str, Any], chapters: list[str]) -> dict[str, Any]:
    selected = select_chapters(document, chapters)
    window_manifest = build_window_manifest(selected)
    sample_window = _first_window(selected)
    fixtures = built_in_fixture_payloads()
    fixture_reports = validate_builtin_fixtures(fixtures)
    return {
        "manifest": {
            "phase": "L2A-0",
            "zero_api": True,
            "prompt_source": PROMPT_SOURCE,
            "chapters": chapters,
            "scaffold_status": "dry_run_only",
        },
        "window_manifest": window_manifest,
        "sample_prompt_context": {
            "chapter_id": sample_window.chapter_id,
            "window_id": sample_window.window_id,
            "block_ids": sample_window.block_ids,
            "previous_tail_block_ids": [
                str(block["block_id"]) for block in sample_window.previous_tail
            ],
            "next_tail_block_ids": [
                str(block["block_id"]) for block in sample_window.next_tail
            ],
            "english_source_window_with_block_markers": render_block_markers(sample_window.blocks),
            "calls_planned": [BRIEF_VERSION, LEXICON_VERSION, NARRATIVE_VERSION],
        },
        "fixture_validation": [report.to_dict() for report in fixture_reports],
        "next_step": "Claude verifies scaffold before any API-backed L2A-1 run.",
    }


def validate_builtin_fixtures(fixtures: dict[str, Any] | None = None) -> list[ValidationReport]:
    payloads = fixtures or built_in_fixture_payloads()
    reports = [
        validate_narrative(
            payloads["group_addressee_narrative"],
            valid_block_ids={"ch04_b012"},
            known_entity_ids={"ent_mr_earnshaw", "ent_mrs_earnshaw"},
        ),
        validate_narrative(
            payloads["vocative_narrative"],
            valid_block_ids={"ch04_b012"},
            known_entity_ids={"ent_mr_earnshaw", "ent_mrs_earnshaw"},
        ),
        validate_story_bible(payloads["partial_story_bible"]),
    ]
    # Acceptance guards that span more than one schema validator.
    group_entities = {
        str(entity.get("canonical"))
        for entity in payloads["partial_story_bible"].get("registry_T2_entities") or []
    }
    if "the household" in group_entities:
        reports.append(
            ValidationReport(
                name="fixture_group_not_person",
                ok=False,
                errors=["group addressee was minted as a person entity"],
                warnings=[],
                counts={},
            )
        )
    else:
        reports.append(
            ValidationReport(
                name="fixture_group_not_person",
                ok=True,
                errors=[],
                warnings=[],
                counts={},
            )
        )
    addressee = payloads["vocative_narrative"]["speaker_turns"][0]["addressee"]
    reports.append(
        ValidationReport(
            name="fixture_vocative_specific_person",
            ok=(
                addressee.get("reference_kind") == "person"
                and addressee.get("candidate_entity_ids") == ["ent_mrs_earnshaw"]
            ),
            errors=[]
            if addressee.get("candidate_entity_ids") == ["ent_mrs_earnshaw"]
            else ["vocative wife did not resolve to ent_mrs_earnshaw"],
            warnings=[],
            counts={},
        )
    )
    return reports


def built_in_fixture_payloads() -> dict[str, Any]:
    ref_mr = {
        "surface": "Mr. Earnshaw",
        "reference_kind": "person",
        "resolution_status": "named",
        "candidate_entity_ids": [],
        "attribution_method": "explicit_tag",
        "confidence": "high",
    }
    ref_household = {
        "surface": "the household",
        "reference_kind": "group",
        "resolution_status": "named",
        "candidate_entity_ids": [],
        "attribution_method": "nearby_context",
        "confidence": "medium",
    }
    ref_wife = {
        "surface": "wife",
        "reference_kind": "person",
        "resolution_status": "candidate",
        "candidate_entity_ids": ["ent_mrs_earnshaw"],
        "attribution_method": "nearby_context",
        "confidence": "medium",
    }
    return {
        "group_addressee_narrative": {
            "chapter_id": "wh_ch04",
            "window_block_ids": ["ch04_b012"],
            "context_only_used": False,
            "speaker_turns": [
                {
                    "turn_id": "t_ch04_b012_01",
                    "speaker": ref_mr,
                    "addressee": ref_household,
                    "utterance_quote": "See here!",
                    "address_term_used": "",
                    "register_cue": "household",
                    "utterance_gist": "addresses the gathered household",
                    "block_id": "ch04_b012",
                }
            ],
            "relation_events": [],
        },
        "vocative_narrative": {
            "chapter_id": "wh_ch04",
            "window_block_ids": ["ch04_b012"],
            "context_only_used": False,
            "speaker_turns": [
                {
                    "turn_id": "t_ch04_b012_02",
                    "speaker": ref_mr,
                    "addressee": ref_wife,
                    "utterance_quote": "See here, wife!",
                    "address_term_used": "wife",
                    "register_cue": "paternal",
                    "utterance_gist": "uses a vocative for Mrs. Earnshaw",
                    "block_id": "ch04_b012",
                }
            ],
            "relation_events": [],
        },
        "partial_story_bible": {
            "scope": "ch1-4",
            "artifact_scope_end_block": "wh_ch04_b999",
            "status": "partial_story_bible",
            "registry_T1_glossary": [],
            "registry_T2_entities": [
                {
                    "entity_id": "ent_mr_earnshaw",
                    "canonical": "Mr. Earnshaw",
                    "entity_type": "person",
                    "aliases": [
                        {
                            "surface": "Mr. Earnshaw",
                            "valid_from_block": "wh_ch04_b001",
                            "valid_to_block": None,
                            "status": "open_within_scope",
                        }
                    ],
                },
                {
                    "entity_id": "ent_mrs_earnshaw",
                    "canonical": "Mrs. Earnshaw",
                    "entity_type": "person",
                    "aliases": [
                        {
                            "surface": "wife",
                            "valid_from_block": "wh_ch04_b012",
                            "valid_to_block": None,
                            "status": "open_within_scope",
                        }
                    ],
                },
            ],
            "registry_T3_speaker_turns": [],
            "registry_T4_chapter_digests": [],
            "entity_relations": [
                {
                    "pair": ["ent_mr_earnshaw", "ent_mrs_earnshaw"],
                    "phase_label": "friendly",
                    "valid_from_block": "wh_ch04_b012",
                    "valid_to_block": None,
                    "status": "open_within_scope",
                    "trigger_block": "wh_ch04_b012",
                    "trigger_evidence": "See here, wife!",
                }
            ],
            "entity_state_intervals": [
                {
                    "entity_id": "ent_mrs_earnshaw",
                    "attribute": "social_status",
                    "value": "wife_of_mr_earnshaw",
                    "valid_from_block": "wh_ch04_b012",
                    "valid_to_block": None,
                    "status": "open_within_scope",
                    "trigger_block": "wh_ch04_b012",
                    "evidence": "wife",
                }
            ],
            "address_policies": [
                {
                    "pair": ["ent_mr_earnshaw", "ent_mrs_earnshaw"],
                    "phase_ref": "friendly@wh_ch04_b012",
                    "a_to_b": {
                        "self": "ta",
                        "address": "mình",
                        "register": "familial",
                        "evidence_level": "observed",
                        "needs_human_review": True,
                    },
                    "b_to_a": {
                        "self": "",
                        "address": "",
                        "register": "",
                        "evidence_level": "unsupported",
                        "needs_human_review": True,
                    },
                }
            ],
            "narration_frame_segments": [],
            "unresolved_threads": [],
        },
    }


def load_system_prompt_from_design(design_doc: Path, prompt_version: str) -> str:
    text = Path(design_doc).read_text(encoding="utf-8")
    marker = f"- Prompt version: {prompt_version}."
    marker_index = text.find(marker)
    if marker_index < 0:
        raise ValueError(f"Prompt version not found in design doc: {prompt_version}")
    quote_start = text.rfind("\n>", 0, marker_index)
    if quote_start < 0:
        raise ValueError(f"Blockquote start not found for {prompt_version}")
    quote_start += 1
    quote_end = text.find("\n### ", marker_index)
    if quote_end < 0:
        quote_end = text.find("\n---", marker_index)
    if quote_end < 0:
        quote_end = len(text)
    lines: list[str] = []
    for line in text[quote_start:quote_end].splitlines():
        if line.startswith("> "):
            lines.append(line[2:])
        elif line == ">":
            lines.append("")
        elif lines:
            break
    prompt = "\n".join(lines).strip()
    if marker not in prompt:
        raise ValueError(f"Extracted prompt for {prompt_version} did not include marker")
    return prompt


def load_system_prompt_for_chapter(design_doc: Path, prompt_version: str, chapter_id: str) -> str:
    """Render book-neutral examples with the active chapter id to prevent copied bad ids."""
    prompt = load_system_prompt_from_design(design_doc, prompt_version)
    return prompt.replace("bk_ch01", str(chapter_id))


def _checkpoint_path(out_dir: Path, stage: str, chapter_id: str) -> Path:
    return Path(out_dir) / "checkpoints" / stage / f"{chapter_id}.json"


def _checkpoint_prompt_hashes(
    design_doc: Path,
    stage: str,
    chapter_id: str,
) -> dict[str, str]:
    versions = (
        [BRIEF_VERSION, LEXICON_VERSION, NARRATIVE_VERSION]
        if stage == "m1"
        else [DIGEST_VERSION]
    )
    return {
        version: canonical_hash(
            load_system_prompt_for_chapter(design_doc, version, chapter_id)
        )
        for version in versions
    }


def _checkpoint_config_hash(
    config: LLMConfig,
    stage: str,
    *,
    window_target_tokens: int = 500,
    window_max_blocks: int = 8,
) -> str:
    return config_hash(
        {
            "stage": stage,
            "model": config.model,
            "temperature": config.temperature,
            "seed": config.seed,
            "reasoning_effort": config.reasoning_effort,
            "verbosity": config.verbosity,
            "response_format": RESPONSE_FORMAT_JSON,
            "max_output_tokens": config.max_output_tokens,
            "daily_token_cap": config.daily_token_cap,
            "pricing": config.pricing,
            "prompt_token_cap": config.prompt_token_cap,
            "window_target_tokens": window_target_tokens if stage == "m1" else None,
            "window_max_blocks": window_max_blocks if stage == "m1" else None,
            "neighbor_k": NEIGHBOR_SUMMARY_K,
            "pack_policy_version": PACK_POLICY_VERSION,
        }
    )


def _empty_accounting() -> dict[str, int | float]:
    return {
        "logical_calls": 0,
        "attempts": 0,
        "cache_hits": 0,
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "incremental_cost_usd": 0.0,
        "incremental_prompt_tokens": 0,
        "incremental_cached_tokens": 0,
        "incremental_completion_tokens": 0,
        "incremental_reasoning_tokens": 0,
    }


def _add_accounting(
    left: dict[str, int | float],
    right: dict[str, int | float],
) -> dict[str, int | float]:
    result = _empty_accounting()
    for key in result:
        value = float(left.get(key, 0)) + float(right.get(key, 0))
        result[key] = round(value, 12) if key.endswith("cost_usd") else int(value)
    return result


def _incremental_accounting_view(
    accounting: dict[str, int | float],
) -> dict[str, int | float]:
    return {
        "logical_calls": int(accounting.get("logical_calls", 0)),
        "attempts": int(accounting.get("attempts", 0)),
        "cache_hits": int(accounting.get("cache_hits", 0)),
        "cost_usd": float(accounting.get("incremental_cost_usd", 0)),
        "prompt_tokens": int(accounting.get("incremental_prompt_tokens", 0)),
        "cached_tokens": int(accounting.get("incremental_cached_tokens", 0)),
        "completion_tokens": int(accounting.get("incremental_completion_tokens", 0)),
        "reasoning_tokens": int(accounting.get("incremental_reasoning_tokens", 0)),
    }


def _add_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    result = dict(left)
    for key, value in right.items():
        result[key] = int(result.get(key, 0)) + int(value)
    return result


def _diff_counts(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in after}


def _chapter_accounting(call_records: list[dict[str, Any]]) -> dict[str, int | float]:
    result = _empty_accounting()
    result["logical_calls"] = len(call_records)
    result["attempts"] = sum(int(item.get("attempts") or 0) for item in call_records)
    for item in call_records:
        result["cache_hits"] = int(result["cache_hits"]) + int(item.get("cache_hits") or 0)
        result["cost_usd"] = float(result["cost_usd"]) + float(item.get("cost_usd") or 0)
        for key in [
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
        ]:
            result[key] = int(result[key]) + int(item.get(key) or 0)
        result["incremental_cost_usd"] = float(result["incremental_cost_usd"]) + float(item.get("incremental_cost_usd") or 0)
        for key in ["prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens"]:
            incremental_key = f"incremental_{key}"
            result[incremental_key] = int(result[incremental_key]) + int(item.get(incremental_key) or 0)
    result["cost_usd"] = round(float(result["cost_usd"]), 12)
    result["incremental_cost_usd"] = round(float(result["incremental_cost_usd"]), 12)
    return result


def _chapter_checkpoint_clean(stage: str, counts: dict[str, int]) -> bool:
    if stage == "m1":
        return all(
            int(counts.get(key, 0)) == 0
            for key in [
                "brief_failed",
                "lexicon_failed",
                "narrative_failed",
                "parse_fail",
                "phase_leak",
            ]
        )
    return all(
        int(counts.get(key, 0)) == 0
        for key in ["digest_failed", "parse_fail"]
    )


def _chapter_work_dir(out_dir: Path, stage: str, chapter_id: str) -> Path:
    path = Path(out_dir) / ".checkpoint_work" / stage / chapter_id
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _promote_chapter_artifacts(
    work_dir: Path,
    out_dir: Path,
    stage: str,
    chapter_id: str,
) -> list[Path]:
    promoted: list[Path] = []
    subdirs = ["brief", "lexicon", "narrative"] if stage == "m1" else ["digest"]
    for subdir in subdirs:
        source_dir = Path(work_dir) / subdir
        destination_dir = Path(out_dir) / subdir
        destination_dir.mkdir(parents=True, exist_ok=True)
        pattern = f"wb_{chapter_id}_*.json" if subdir in {"lexicon", "narrative"} else f"{chapter_id}.json"
        sources = sorted(source_dir.glob(pattern)) if source_dir.exists() else []
        for stale in destination_dir.glob(pattern):
            stale.unlink()
        for source in sources:
            destination = destination_dir / source.name
            os.replace(source, destination)
            promoted.append(destination)
    shutil.rmtree(work_dir, ignore_errors=True)
    return promoted


def _checkpoint_expected(
    *,
    stage: str,
    chapter: dict[str, Any],
    chapter_index: int,
    chapter_sequence_prefix: list[str],
    design_doc: Path,
    config_hash_value: str,
    parent_checkpoint_hash: str | None,
    input_m1_checkpoint_hash: str | None = None,
) -> dict[str, Any]:
    expected = {
        "stage": stage,
        "chapter_id": str(chapter["chapter_id"]),
        "chapter_index": chapter_index,
        "chapter_sequence_prefix": chapter_sequence_prefix,
        "source_hash": chapter_source_hash(chapter),
        "prompt_hashes": _checkpoint_prompt_hashes(
            design_doc, stage, str(chapter["chapter_id"])
        ),
        "config_hash": config_hash_value,
        "schema_version": (
            M1_CHECKPOINT_SCHEMA_VERSION if stage == "m1" else M2_CHECKPOINT_SCHEMA_VERSION
        ),
        "parent_checkpoint_hash": parent_checkpoint_hash,
    }
    if stage == "m2":
        expected["input_m1_checkpoint_hash"] = input_m1_checkpoint_hash
    return expected


def _load_valid_checkpoint_prefix(
    *,
    stage: str,
    selected: list[dict[str, Any]],
    out_dir: Path,
    design_doc: Path,
    config_hash_value: str,
    input_m1_hashes: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checkpoints: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    parent_hash: str | None = None
    prefix: list[str] = []
    for chapter_index, chapter in enumerate(selected):
        chapter_id = str(chapter["chapter_id"])
        prefix.append(chapter_id)
        path = _checkpoint_path(out_dir, stage, chapter_id)
        if not path.is_file():
            mismatches.append({"chapter_id": chapter_id, "fields": ["missing"]})
            break
        try:
            checkpoint = read_checkpoint(path)
        except Exception as exc:
            mismatches.append(
                {"chapter_id": chapter_id, "fields": [f"read_error:{type(exc).__name__}"]}
            )
            break
        expected = _checkpoint_expected(
            stage=stage,
            chapter=chapter,
            chapter_index=chapter_index,
            chapter_sequence_prefix=list(prefix),
            design_doc=design_doc,
            config_hash_value=config_hash_value,
            parent_checkpoint_hash=parent_hash,
            input_m1_checkpoint_hash=(input_m1_hashes or {}).get(chapter_id),
        )
        errors = validate_checkpoint(checkpoint, root=out_dir, expected=expected)
        if errors:
            mismatches.append({"chapter_id": chapter_id, "fields": errors})
            break
        checkpoints.append(checkpoint)
        parent_hash = str(checkpoint["checkpoint_hash"])
    return checkpoints, mismatches


def _require_resume_from_document_start(
    document: dict[str, Any], selected: list[dict[str, Any]]
) -> None:
    document_chapters = document.get("chapters") or []
    if not selected or not document_chapters:
        return
    expected = str(document_chapters[0].get("chapter_id") or "")
    actual = str(selected[0].get("chapter_id") or "")
    if actual != expected:
        raise ValueError(
            "--resume requires the full chapter prefix from the document start; "
            f"expected first chapter {expected}, got {actual}"
        )


def _m1_checkpoint_chain_for_m2(
    *,
    selected: list[dict[str, Any]],
    m1_dir: Path,
    design_doc: Path,
    m1_report: dict[str, Any],
) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    parent_hash: str | None = None
    prefix: list[str] = []
    reported_selected = m1_report.get("chapters_selected")
    report_chapters = (
        [str(chapter_id) for chapter_id in reported_selected]
        if isinstance(reported_selected, list)
        else []
    )
    for chapter_index, chapter in enumerate(selected):
        chapter_id = str(chapter["chapter_id"])
        prefix.append(chapter_id)
        path = _checkpoint_path(m1_dir, "m1", chapter_id)
        if not path.is_file():
            # A final M1 ledger is as-of only when that report itself contains
            # exactly this one chapter. A multi-chapter legacy report would
            # leak future entities into a one-chapter M2 digest.
            if len(selected) == 1 and report_chapters == [chapter_id]:
                return []
            raise ValueError(f"M2 requires M1 as-of checkpoint for {chapter_id}: {path}")
        checkpoint = read_checkpoint(path)
        expected = {
            "stage": "m1",
            "chapter_id": chapter_id,
            "chapter_index": chapter_index,
            "chapter_sequence_prefix": list(prefix),
            "source_hash": chapter_source_hash(chapter),
            "prompt_hashes": _checkpoint_prompt_hashes(design_doc, "m1", chapter_id),
            "schema_version": M1_CHECKPOINT_SCHEMA_VERSION,
            "parent_checkpoint_hash": parent_hash,
            "config_hash": checkpoint.get("config_hash"),
        }
        errors = validate_checkpoint(checkpoint, root=m1_dir, expected=expected)
        if errors:
            raise ValueError(f"Invalid M1 as-of checkpoint {chapter_id}: {errors}")
        checkpoints.append(checkpoint)
        parent_hash = str(checkpoint["checkpoint_hash"])
    return checkpoints


def build_chapter_brief_messages(
    *,
    design_doc: Path,
    chapter: dict[str, Any],
    registry_context_pack: str,
    neighbor_summaries: str,
) -> list[dict[str, str]]:
    chapter_id = str(chapter["chapter_id"])
    return [
        {
            "role": "system",
            "content": load_system_prompt_for_chapter(design_doc, BRIEF_VERSION, chapter_id),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    "REGISTRY_SO_FAR\n" + (registry_context_pack.strip() or "(none yet)"),
                    "NEIGHBOR_SUMMARIES_GIST_ONLY\n"
                    + (neighbor_summaries.strip() or "(none)"),
                    f"CHAPTER_ID\n{chapter_id}",
                    "FULL_CHAPTER_TEXT_WITH_BLOCK_MARKERS\n"
                    + render_block_markers(chapter.get("blocks") or []),
                ]
            ),
        },
    ]


def build_lexicon_messages(
    *,
    design_doc: Path,
    chapter_id: str,
    window: LiteraryWindow,
    registry_context_pack: str,
    chapter_brief: str = "",
    neighbor_summaries: str = "",
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": load_system_prompt_for_chapter(design_doc, LEXICON_VERSION, chapter_id),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    "CHAPTER_BRIEF\n" + (chapter_brief.strip() or "(none)"),
                    "NEIGHBOR_SUMMARIES_GIST_ONLY\n"
                    + (neighbor_summaries.strip() or "(none)"),
                    "REGISTRY_CONTEXT_PACK\n" + (registry_context_pack or "(none yet)"),
                    f"CHAPTER_ID\n{chapter_id}",
                    _tail_section("PREVIOUS_WINDOW_TAIL_CONTEXT_ONLY", window.previous_tail),
                    _tail_section("NEXT_WINDOW_TAIL_CONTEXT_ONLY", window.next_tail),
                    "ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS\n"
                    + render_block_markers(window.blocks),
                ]
            ),
        },
    ]


def build_narrative_messages(
    *,
    design_doc: Path,
    chapter_id: str,
    window: LiteraryWindow,
    narrator_hints: str,
    chapter_roster: str,
    window_mentions: str,
    chapter_brief: str = "",
    neighbor_summaries: str = "",
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": load_system_prompt_for_chapter(design_doc, NARRATIVE_VERSION, chapter_id),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    "CHAPTER_BRIEF\n" + (chapter_brief.strip() or "(none)"),
                    "NEIGHBOR_SUMMARIES_GIST_ONLY\n"
                    + (neighbor_summaries.strip() or "(none)"),
                    "ACTIVE_NARRATOR_HINTS_BY_BLOCK_RANGE\n" + (narrator_hints or "(unknown)"),
                    "CHAPTER_ROSTER_ON_STAGE\n" + (chapter_roster or "(none yet)"),
                    "WINDOW_MENTIONS_FROM_LEXICON_PASS\n" + (window_mentions or "(none)"),
                    f"CHAPTER_ID\n{chapter_id}",
                    _tail_section("PREVIOUS_WINDOW_TAIL_CONTEXT_ONLY", window.previous_tail),
                    _tail_section("NEXT_WINDOW_TAIL_CONTEXT_ONLY", window.next_tail),
                    "ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS\n"
                    + render_block_markers(window.blocks),
                ]
            ),
        },
    ]


def build_digest_messages(
    *,
    design_doc: Path,
    chapter: dict[str, Any],
    previous_summary: str = "",
    neighbor_summaries: str = "",
    chapter_brief: str = "",
    chapter_roster: str = "",
    chapter_relation_events: str = "",
) -> list[dict[str, str]]:
    chapter_id = str(chapter["chapter_id"])
    return [
        {
            "role": "system",
            "content": load_system_prompt_for_chapter(design_doc, DIGEST_VERSION, chapter_id),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    "PREVIOUS_CHAPTER_ROLLING_SUMMARY\n"
                    + (previous_summary.strip() or "(none)"),
                    "NEIGHBOR_SUMMARIES_GIST_ONLY\n"
                    + (neighbor_summaries.strip() or "(none)"),
                    "CHAPTER_BRIEF\n" + (chapter_brief.strip() or "(none)"),
                    "CHAPTER_ROSTER\n" + (chapter_roster.strip() or "(none)"),
                    "CHAPTER_RELATION_EVENTS\n"
                    + (chapter_relation_events.strip() or "(none)"),
                    f"CHAPTER_ID\n{chapter_id}",
                    "FULL_CHAPTER_TEXT_WITH_BLOCK_MARKERS\n"
                    + render_block_markers(chapter.get("blocks") or []),
                ]
            ),
        },
    ]


def registry_context_from_ledger(ledger: dict[str, dict[str, Any]], window: LiteraryWindow) -> str:
    window_text = " ".join(
        str(block.get("clean_text") or block.get("source_text") or "")
        for block in window.blocks
    ).casefold()
    lines: list[str] = []
    seeded_lines: list[str] = []
    for entity_id, entity in sorted(ledger.items()):
        aliases = [str(alias) for alias in entity.get("aliases") or [] if str(alias)]
        canonical = str(entity.get("canonical") or "").strip()
        if any(alias.casefold() in window_text for alias in aliases):
            alias_text = ", ".join(aliases[:2])
            lines.append(f"{entity_id} | {canonical or aliases[0]} | {alias_text}")
        elif entity.get("source") == "chapter_brief_cast" and canonical:
            seeded_lines.append(
                f"{entity_id} | {canonical} | seeded:chapter_brief_cast:no_surface_alias"
            )
        if len(lines) >= 15:
            break
    for line in seeded_lines:
        if len(lines) >= 15:
            break
        lines.append(line)
    return "\n".join(lines)


def roster_from_ledger(ledger: dict[str, dict[str, Any]]) -> str:
    lines = []
    for entity_id, entity in sorted(ledger.items()):
        aliases = [str(alias) for alias in entity.get("aliases") or [] if str(alias)]
        canonical = str(entity.get("canonical") or "").strip()
        if aliases:
            lines.append(f"{entity_id} | {canonical or aliases[0]} | {', '.join(aliases[:3])}")
        elif entity.get("source") == "chapter_brief_cast" and canonical:
            lines.append(f"{entity_id} | {canonical} | seeded:chapter_brief_cast:no_surface_alias")
        if len(lines) >= 30:
            break
    return "\n".join(lines)


def mentions_summary(lexicon_output: dict[str, Any]) -> str:
    lines: list[str] = []
    for mention in lexicon_output.get("character_mentions") or []:
        if not isinstance(mention, dict):
            continue
        lines.append(
            " | ".join(
                [
                    str(mention.get("mention_id") or ""),
                    str(mention.get("surface") or ""),
                    str(mention.get("mention_type") or ""),
                    str(mention.get("resolution_status") or ""),
                    ",".join(str(item) for item in mention.get("candidate_entity_ids") or []),
                ]
            )
        )
    return "\n".join(lines)


def render_chapter_brief_for_injection(brief: dict[str, Any] | None) -> str:
    if not isinstance(brief, dict):
        return "(none)"
    lines: list[str] = []
    setting = brief.get("setting") if isinstance(brief.get("setting"), dict) else {}
    place = str(setting.get("place") or "").strip()
    time_hint = str(setting.get("time_frame_hint") or "").strip()
    scene_shape = str(setting.get("scene_shape") or "").strip()
    if place or time_hint or scene_shape:
        lines.append(
            "setting | "
            + " | ".join(part for part in [place, time_hint, scene_shape] if part)
        )
    premise = safe_brief_neutral_premise(brief)
    if premise:
        lines.append(f"neutral_premise | {premise}")
    for cast in brief.get("cast_on_stage") or []:
        if not isinstance(cast, dict):
            continue
        surface = str(cast.get("surface") or "").strip()
        role = str(cast.get("role_hint") or "").strip()
        block = str(cast.get("first_seen_block") or "").strip()
        if _has_brief_leak_token(role):
            continue
        if surface:
            lines.append(f"cast | {surface} | {role or 'unknown_role'} | {block}")
    for scene in brief.get("scenes_party_size") or []:
        if not isinstance(scene, dict):
            continue
        block_range = scene.get("block_range") or []
        participants = scene.get("participants") or []
        if len(block_range) >= 2:
            lines.append(
                "scene | "
                + f"{block_range[0]}..{block_range[1]} | "
                + f"co_present_count={scene.get('co_present_count')} | "
                + ", ".join(str(item) for item in participants if str(item))
            )
    return "\n".join(lines) if lines else "(none)"


def safe_brief_neutral_premise(brief: dict[str, Any] | None) -> str:
    if not isinstance(brief, dict):
        return ""
    premise = str(brief.get("neutral_premise") or "").strip()
    if not premise or _has_brief_leak_token(premise):
        return ""
    return premise


def update_entity_ledger_from_lexicon(
    ledger: dict[str, dict[str, Any]],
    lexicon_output: dict[str, Any],
) -> None:
    for mention in lexicon_output.get("character_mentions") or []:
        if not isinstance(mention, dict):
            continue
        if mention.get("resolution_status") != "named":
            continue
        surface = str(mention.get("surface") or "").strip()
        if not surface:
            continue
        entity_id = _entity_id_for_surface(surface)
        entity = ledger.setdefault(
            entity_id,
            {"entity_id": entity_id, "canonical": surface, "aliases": []},
        )
        aliases = entity.setdefault("aliases", [])
        if surface not in aliases:
            aliases.append(surface)


def seed_entity_ledger_from_chapter_brief(
    ledger: dict[str, dict[str, Any]],
    brief: dict[str, Any] | None,
    *,
    chapter_block_ids: Sequence[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    report: dict[str, list[dict[str, Any]]] = {"seeded_cast": [], "seed_skipped_cast": []}
    if not isinstance(brief, dict):
        return report
    valid_blocks = set(chapter_block_ids or [])
    seen_seed_ids: set[str] = set()
    for idx, cast in enumerate(brief.get("cast_on_stage") or []):
        if not isinstance(cast, dict):
            report["seed_skipped_cast"].append(
                {"index": idx, "surface": "", "reason": "not_object"}
            )
            continue
        surface = str(cast.get("surface") or "").strip()
        surface_kind = str(cast.get("surface_kind") or "").strip()
        if not surface:
            report["seed_skipped_cast"].append(
                {"index": idx, "surface": "", "reason": "empty_surface"}
            )
            continue
        if surface_kind != "proper_name":
            report["seed_skipped_cast"].append(
                {
                    "index": idx,
                    "surface": surface,
                    "surface_kind": surface_kind or None,
                    "reason": "surface_kind_not_proper_name",
                }
            )
            continue
        first_seen_block = str(cast.get("first_seen_block") or "").strip()
        if valid_blocks and first_seen_block not in valid_blocks:
            report["seed_skipped_cast"].append(
                {
                    "index": idx,
                    "surface": surface,
                    "surface_kind": surface_kind,
                    "first_seen_block": first_seen_block or None,
                    "reason": "first_seen_block_outside_chapter",
                }
            )
            continue
        entity_id = _entity_id_for_surface(surface)
        if entity_id in seen_seed_ids:
            report["seed_skipped_cast"].append(
                {
                    "index": idx,
                    "surface": surface,
                    "surface_kind": surface_kind,
                    "entity_id": entity_id,
                    "reason": "duplicate_seed_in_chapter",
                }
            )
            continue
        seen_seed_ids.add(entity_id)
        entity = ledger.setdefault(
            entity_id,
            {
                "entity_id": entity_id,
                "canonical": surface,
                "aliases": [],
                "source": "chapter_brief_cast",
                "evidence_scope": "chapter_level",
                "surface_evidence_block": None,
                "seeded": True,
            },
        )
        entity.setdefault("aliases", [])
        entity.setdefault("source", "chapter_brief_cast")
        entity.setdefault("evidence_scope", "chapter_level")
        entity.setdefault("surface_evidence_block", None)
        report["seeded_cast"].append(
            {
                "index": idx,
                "surface": surface,
                "surface_kind": surface_kind,
                "entity_id": entity_id,
                "source": "chapter_brief_cast",
                "evidence_scope": "chapter_level",
                "surface_evidence_block": None,
                "first_seen_block": first_seen_block or None,
            }
        )
    return report


def narrator_hints_for_window(window: LiteraryWindow) -> str:
    start = window.block_ids[0]
    end = window.block_ids[-1]
    return f"{start}..{end} | unknown"


def _entity_id_for_surface(surface: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", surface.casefold()).strip("_")
    return "ent_" + (slug or "unknown")


def _tail_section(label: str, blocks: list[dict[str, Any]]) -> str:
    if not blocks:
        return f"{label}\n(none)"
    return f"{label}\n" + render_block_markers(blocks)


def _load_m1_report(m1_dir: Path) -> dict[str, Any]:
    report_path = Path(m1_dir) / "m1_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"M1 report not found for M2 digest: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    counts = report.get("validation_counts") or {}
    if counts.get("lexicon_failed") or counts.get("narrative_failed"):
        raise ValueError(
            "M2 requires a clean M1 report; found "
            f"lexicon_failed={counts.get('lexicon_failed')} "
            f"narrative_failed={counts.get('narrative_failed')}"
        )
    if counts.get("brief_failed"):
        raise ValueError(
            "M2 requires a clean M1 chapter brief; found "
            f"brief_failed={counts.get('brief_failed')}"
        )
    return report


def _chapter_roster_from_m1(report: dict[str, Any]) -> str:
    ledger = report.get("entity_ledger") or {}
    if not isinstance(ledger, dict):
        return ""
    return roster_from_ledger(ledger)


def _chapter_relation_events_from_m1(m1_dir: Path, chapter: dict[str, Any]) -> str:
    valid_block_ids = {str(block.get("block_id")) for block in chapter.get("blocks") or []}
    rows: list[str] = []
    narrative_dir = Path(m1_dir) / "narrative"
    for artifact_path in sorted(narrative_dir.glob("*.json")):
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        parsed = payload.get("parsed_json")
        if not isinstance(parsed, dict):
            continue
        if str(parsed.get("chapter_id") or "") != str(chapter.get("chapter_id") or ""):
            continue
        for event in parsed.get("relation_events") or []:
            if not isinstance(event, dict):
                continue
            block_id = str(event.get("block_id") or "")
            if block_id not in valid_block_ids:
                continue
            actor = event.get("actor")
            target = event.get("target")
            if not _digest_ref_allowed(actor) or not _digest_ref_allowed(target):
                continue
            event_id = str(event.get("event_id") or "")
            event_type = str(event.get("event_type") or "")
            pair = f"{_reference_label(actor)} -> {_reference_label(target)}"
            quote = str(event.get("evidence_quote") or "").strip().replace("\n", " ")
            if len(quote) > 120:
                quote = quote[:117] + "..."
            rows.append(" | ".join([pair, event_type, block_id, event_id, quote]))
    return "\n".join(rows)


def _chapter_brief_from_m1(m1_dir: Path, chapter_id: str) -> str:
    path = Path(m1_dir) / "brief" / f"{chapter_id}.json"
    if not path.exists():
        return "(none)"
    payload = json.loads(path.read_text(encoding="utf-8"))
    parsed = payload.get("parsed_json")
    validation = payload.get("validation") or {}
    if not isinstance(parsed, dict) or not validation.get("ok"):
        return "(none)"
    return render_chapter_brief_for_injection(parsed)


def _digest_ref_allowed(ref: Any) -> bool:
    return isinstance(ref, dict) and ref.get("reference_kind") in {"person", "narrator"}


def _reference_label(ref: Any) -> str:
    if not isinstance(ref, dict):
        return "unknown"
    candidates = [str(item) for item in ref.get("candidate_entity_ids") or [] if str(item)]
    if candidates:
        return candidates[0]
    surface = str(ref.get("surface") or "").strip()
    return surface or str(ref.get("reference_kind") or "unknown")


def _load_digest_payload(digest_dir: Path, chapter_id: str) -> dict[str, Any]:
    path = Path(digest_dir) / f"{chapter_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Digest artifact not found for M3: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    parsed = payload.get("parsed_json")
    validation = payload.get("validation") or {}
    if not isinstance(parsed, dict) or not validation.get("ok"):
        raise ValueError(f"M3 requires a clean digest artifact: {path}")
    return parsed


def _build_story_bible_chapter(
    *,
    chapter: dict[str, Any],
    m1_dir: Path,
    m1_report: dict[str, Any],
    digest: dict[str, Any],
) -> dict[str, Any]:
    block_ids = [str(block.get("block_id")) for block in chapter.get("blocks") or []]
    first_block = block_ids[0]
    last_block = block_ids[-1]
    mention_first_blocks = _mention_first_blocks(m1_dir)
    old_to_new, entities, surface_to_new, identity_merges = _consolidate_entities_from_m1(
        m1_report,
        mention_first_blocks,
        first_block,
    )
    resolver = EntityResolver(
        old_to_new=old_to_new,
        surface_to_new=surface_to_new,
        narrator_entity_id=_active_narrator_entity_id(digest, old_to_new, surface_to_new),
    )
    event_index = _relation_event_index(m1_dir, resolver)
    speaker_turns = _consolidate_speaker_turns(m1_dir, resolver)
    _apply_entity_presence_status(entities, event_index, speaker_turns)
    glossary = _consolidate_glossary(m1_dir)
    relations = _consolidate_relations(digest, event_index, resolver)
    state_intervals, state_audit = _consolidate_state_intervals(digest, resolver)
    address_policies = _propose_address_policies(relations, speaker_turns)
    canary_report = _chapter_one_canary_report(
        entities=entities,
        speaker_turns=speaker_turns,
        relations=relations,
        state_intervals=state_intervals,
    )
    audit = {
        "scope": "M3_ch1",
        "identity_merges": identity_merges,
        "historical_mentions": [
            str(entity.get("entity_id"))
            for entity in entities
            if entity.get("presence_status") == "mentioned_historical"
        ],
        **state_audit,
        "micro_calls": 0,
        "escalated": 0,
        "pairs_blocked_for_runtime": 0,
        "phase_segmentation_mode": "generic_valence_fallback_no_book_specific_rules",
    }
    return {
        "scope": "ch1",
        "artifact_scope_end_block": last_block,
        "status": "partial_story_bible",
        "registry_T1_glossary": glossary,
        "registry_T2_entities": entities,
        "registry_T3_speaker_turns": speaker_turns,
        "registry_T4_chapter_digests": [digest],
        "entity_relations": relations,
        "entity_state_intervals": state_intervals,
        "address_policies": address_policies,
        "narration_frame_segments": digest.get("narration_frame_segments") or [],
        "unresolved_threads": digest.get("unresolved_threads") or [],
        "audit": audit,
        "canary_report": canary_report,
        "source_ranges": {"first_block": first_block, "last_block": last_block},
    }


def _mention_first_blocks(m1_dir: Path) -> dict[str, str]:
    first: dict[str, str] = {}
    for artifact_path in sorted((Path(m1_dir) / "lexicon").glob("*.json")):
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        parsed = payload.get("parsed_json")
        if not isinstance(parsed, dict):
            continue
        for mention in parsed.get("character_mentions") or []:
            if not isinstance(mention, dict):
                continue
            surface = str(mention.get("surface") or "").strip()
            block_ids = [str(item) for item in mention.get("block_ids") or [] if str(item)]
            if surface and block_ids:
                first.setdefault(_surface_key(surface), block_ids[0])
    return first


def _consolidate_entities_from_m1(
    m1_report: dict[str, Any],
    mention_first_blocks: dict[str, str],
    fallback_block: str,
) -> tuple[dict[str, str], list[dict[str, Any]], dict[str, str], list[dict[str, Any]]]:
    clusters: dict[str, dict[str, Any]] = {}
    old_to_new: dict[str, str] = {}
    surface_to_new: dict[str, str] = {}
    ledger = m1_report.get("entity_ledger") or {}
    if not isinstance(ledger, dict):
        ledger = {}
    for old_id, entity in sorted(ledger.items()):
        aliases = [str(alias).strip() for alias in entity.get("aliases") or [] if str(alias).strip()]
        if not aliases:
            canonical = str(entity.get("canonical") or old_id)
            aliases = [canonical]
        cluster_key = _entity_cluster_key(aliases, str(entity.get("canonical") or old_id))
        final_id = f"ent_{cluster_key}" if cluster_key else str(old_id)
        old_to_new[str(old_id)] = final_id
        cluster = clusters.setdefault(
            final_id,
            {
                "entity_id": final_id,
                "canonical": _canonical_from_aliases(aliases),
                "entity_type": "person",
                "source_entity_ids": [],
                "aliases": {},
            },
        )
        surface_to_new[_surface_key(str(old_id))] = final_id
        surface_to_new[_strip_honorific_key(str(old_id).removeprefix("ent_"))] = final_id
        cluster["source_entity_ids"].append(str(old_id))
        for alias in aliases:
            key = _surface_key(alias)
            core_key = _strip_honorific_key(alias)
            surface_to_new[key] = final_id
            if core_key:
                surface_to_new[core_key] = final_id
            block = mention_first_blocks.get(key, fallback_block)
            cluster["aliases"].setdefault(
                alias,
                {
                    "surface": alias,
                    "valid_from_block": block,
                    "valid_to_block": None,
                    "status": "open_within_scope",
                },
            )
    entities: list[dict[str, Any]] = []
    for final_id, cluster in sorted(clusters.items()):
        aliases = sorted(
            cluster["aliases"].values(),
            key=lambda item: (str(item.get("valid_from_block")), str(item.get("surface"))),
        )
        entity = {
            "entity_id": final_id,
            "canonical": cluster["canonical"],
            "entity_type": "person",
            "source_entity_ids": sorted(set(cluster["source_entity_ids"])),
            "aliases": aliases,
            "presence_status": "unclassified",
        }
        entities.append(entity)
    identity_merges = [
        {
            "merged_into": entity["entity_id"],
            "source_entity_ids": entity["source_entity_ids"],
            "reason": "honorific_variant_same_surface_core",
        }
        for entity in entities
        if len(entity.get("source_entity_ids") or []) > 1
    ]
    return old_to_new, entities, surface_to_new, identity_merges


def _consolidate_glossary(m1_dir: Path) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for artifact_path in sorted((Path(m1_dir) / "lexicon").glob("*.json")):
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        parsed = payload.get("parsed_json")
        if not isinstance(parsed, dict):
            continue
        for term in parsed.get("glossary_candidates") or []:
            if not isinstance(term, dict):
                continue
            category = str(term.get("category") or "")
            source = str(term.get("source_term") or "").strip()
            if category != "place" or not source:
                continue
            key = _surface_key(source)
            row = by_key.setdefault(
                key,
                {
                    "source_term": source,
                    "proposed_target_vi": str(term.get("proposed_target_vi") or ""),
                    "category": category,
                    "block_ids": [],
                    "status": "candidate",
                },
            )
            for block_id in term.get("block_ids") or []:
                value = str(block_id)
                if value not in row["block_ids"]:
                    row["block_ids"].append(value)
    return sorted(by_key.values(), key=lambda item: item["source_term"].casefold())


def _relation_event_index(m1_dir: Path, resolver: EntityResolver) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for artifact_path in sorted((Path(m1_dir) / "narrative").glob("*.json")):
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        parsed = payload.get("parsed_json")
        if not isinstance(parsed, dict):
            continue
        for event in parsed.get("relation_events") or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id:
                continue
            actor_id = _resolve_reference_entity(event.get("actor"), resolver)
            target_id = _resolve_reference_entity(event.get("target"), resolver)
            events[event_id] = {
                "event_id": event_id,
                "actor_entity_id": actor_id,
                "target_entity_id": target_id,
                "event_type": str(event.get("event_type") or ""),
                "evidence_quote": str(event.get("evidence_quote") or ""),
                "block_id": str(event.get("block_id") or ""),
            }
    return events


def _consolidate_speaker_turns(
    m1_dir: Path,
    resolver: EntityResolver,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for artifact_path in sorted((Path(m1_dir) / "narrative").glob("*.json")):
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        parsed = payload.get("parsed_json")
        if not isinstance(parsed, dict):
            continue
        for turn in parsed.get("speaker_turns") or []:
            if not isinstance(turn, dict):
                continue
            speaker_id = _resolve_reference_entity(turn.get("speaker"), resolver)
            addressee_id = _resolve_reference_entity(
                turn.get("addressee"),
                resolver,
                speaker_id=speaker_id,
            )
            if not speaker_id:
                continue
            row = {
                **turn,
                "speaker_entity_id": speaker_id,
                "addressee_entity_id": addressee_id,
                "resolution_status_consolidated": "resolved"
                if addressee_id
                else "speaker_resolved_addressee_unknown",
            }
            turns.append(row)
    turns.sort(key=lambda item: (str(item.get("block_id")), str(item.get("turn_id"))))
    return turns


def _consolidate_relations(
    digest: dict[str, Any],
    event_index: dict[str, dict[str, Any]],
    resolver: EntityResolver,
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    for relation in digest.get("relation_event_summary") or []:
        if not isinstance(relation, dict):
            continue
        pair = [_remap_entity_ref(str(item), resolver) for item in relation.get("pair") or []]
        if len(pair) != 2 or not pair[0] or not pair[1] or pair[0] == pair[1]:
            continue
        event_ids = [str(item) for item in relation.get("event_ids") or []]
        events = [event_index[item] for item in event_ids if item in event_index]
        if not events:
            continue
        trigger = sorted(events, key=lambda item: item["block_id"])[0]
        phase_label = _phase_label_from_valence_hint(str(relation.get("observed_valence_hint") or ""))
        relations.append(
            {
                "pair": pair,
                "phase_label": phase_label,
                "valid_from_block": trigger["block_id"],
                "valid_to_block": None,
                "status": "open_within_scope",
                "trigger_block": trigger["block_id"],
                "trigger_evidence": trigger["evidence_quote"],
                "source_event_ids": event_ids,
                "needs_human_review": True,
                "phase_source": "observed_valence_hint_fallback",
            }
        )
    relations.sort(key=lambda item: (item["valid_from_block"], item["pair"]))
    return relations


def _consolidate_state_intervals(
    digest: dict[str, Any],
    resolver: EntityResolver,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    intervals: list[dict[str, Any]] = []
    dropped_temporary = 0
    for change in digest.get("character_state_changes") or []:
        if not isinstance(change, dict):
            continue
        entity_id = _remap_entity_ref(str(change.get("entity_ref") or ""), resolver)
        attribute = str(change.get("attribute") or "")
        to_value = str(change.get("to") or "")
        observed_scope = str(change.get("observed_scope") or "")
        if attribute == "residence" and "visiting" in to_value and observed_scope == "this_chapter":
            dropped_temporary += 1
            continue
        if not entity_id:
            continue
        intervals.append(
            {
                "entity_id": entity_id,
                "attribute": attribute,
                "value": to_value,
                "valid_from_block": str(change.get("trigger_block") or ""),
                "valid_to_block": None,
                "status": "open_within_scope",
                "trigger_block": str(change.get("trigger_block") or ""),
                "evidence": str(change.get("evidence_quote") or ""),
            }
        )
    return intervals, {"state_changes_dropped_temporary": dropped_temporary}


def _propose_address_policies(
    relations: list[dict[str, Any]],
    speaker_turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observed: dict[tuple[str, str], list[str]] = {}
    for turn in speaker_turns:
        speaker = str(turn.get("speaker_entity_id") or "")
        addressee = str(turn.get("addressee_entity_id") or "")
        term = str(turn.get("address_term_used") or "").strip()
        if speaker and addressee and term:
            observed.setdefault((speaker, addressee), [])
            if term not in observed[(speaker, addressee)]:
                observed[(speaker, addressee)].append(term)
    policies: list[dict[str, Any]] = []
    for relation in relations:
        a, b = relation["pair"]
        policies.append(
            {
                "pair": [a, b],
                "phase_ref": f"{relation['phase_label']}@{relation['valid_from_block']}",
                "a_to_b": _address_direction_policy(observed.get((a, b), [])),
                "b_to_a": _address_direction_policy(observed.get((b, a), [])),
                "proposal_only": True,
            }
        )
    return policies


def _address_direction_policy(terms: list[str]) -> dict[str, Any]:
    if not terms:
        return {
            "self": "",
            "address": "",
            "register": "",
            "evidence_level": "unsupported",
            "needs_human_review": True,
            "runtime_usable": False,
        }
    return {
        "self": "",
        "address": "",
        "register": "",
        "evidence_level": "observed",
        "needs_human_review": True,
        "runtime_usable": False,
        "observed_terms": terms,
    }


def _chapter_one_canary_report(
    *,
    entities: list[dict[str, Any]],
    speaker_turns: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    state_intervals: list[dict[str, Any]],
) -> dict[str, Any]:
    heathcliff_entities = [
        entity
        for entity in entities
        if any("heathcliff" == _surface_key(alias.get("surface", "")) for alias in entity.get("aliases") or [])
        or any("mr_heathcliff" == _surface_key(alias.get("surface", "")) for alias in entity.get("aliases") or [])
    ]
    heath_aliases = {
        str(alias.get("surface"))
        for entity in heathcliff_entities
        for alias in entity.get("aliases") or []
    }
    hareton = next((entity for entity in entities if entity.get("entity_id") == "ent_hareton_earnshaw"), None)
    hareton_in_runtime = any(
        turn.get("speaker_entity_id") == "ent_hareton_earnshaw"
        or turn.get("addressee_entity_id") == "ent_hareton_earnshaw"
        for turn in speaker_turns
    ) or any("ent_hareton_earnshaw" in (relation.get("pair") or []) for relation in relations)
    lockwood_residence = [
        interval
        for interval in state_intervals
        if interval.get("entity_id") == "ent_lockwood" and interval.get("attribute") == "residence"
    ]
    checks = {
        "heathcliff_honorific_merge": len(heathcliff_entities) == 1
        and {"Heathcliff", "Mr. Heathcliff"}.issubset(heath_aliases),
        "hareton_historical_not_present": bool(hareton)
        and hareton.get("presence_status") == "mentioned_historical"
        and not hareton_in_runtime,
        "lockwood_temporary_residence_dropped": not lockwood_residence,
    }
    return {"pass": all(checks.values()), "checks": checks}


def _resolve_reference_entity(
    ref: Any,
    resolver: EntityResolver,
    *,
    speaker_id: str | None = None,
) -> str | None:
    if not isinstance(ref, dict):
        return None
    candidate_ids = [str(item) for item in ref.get("candidate_entity_ids") or [] if str(item)]
    if len(candidate_ids) == 1:
        mapped = _remap_entity_ref(candidate_ids[0], resolver)
        if mapped:
            return mapped
    elif len(candidate_ids) > 1:
        return None
    if ref.get("reference_kind") == "narrator":
        return resolver.narrator_entity_id
    surface = str(ref.get("surface") or "").strip()
    key = _surface_key(surface)
    if key in PLAIN_PRONOUNS:
        return None
    return resolver.surface_to_new.get(key) or resolver.surface_to_new.get(_strip_honorific_key(surface))


def _remap_entity_ref(value: str, resolver: EntityResolver) -> str | None:
    if value in resolver.old_to_new:
        return resolver.old_to_new[value]
    key = _surface_key(value)
    if key in resolver.surface_to_new:
        return resolver.surface_to_new[key]
    core_key = _strip_honorific_key(value)
    if core_key in resolver.surface_to_new:
        return resolver.surface_to_new[core_key]
    return value if value.startswith("ent_") else None


def _phase_label_from_valence_hint(valence_hint: str) -> str:
    if valence_hint == "positive":
        return "friendly"
    if valence_hint == "negative":
        return "strained"
    if valence_hint == "mixed":
        return "strained"
    return "neutral"


def _active_narrator_entity_id(
    digest: dict[str, Any],
    old_to_new: dict[str, str],
    surface_to_new: dict[str, str],
) -> str | None:
    resolver = EntityResolver(old_to_new=old_to_new, surface_to_new=surface_to_new, narrator_entity_id=None)
    for segment in digest.get("narration_frame_segments") or []:
        if not isinstance(segment, dict):
            continue
        narrator_ref = str(segment.get("narrator_ref") or "")
        mapped = _remap_entity_ref(narrator_ref, resolver)
        if mapped:
            return mapped
    return None


def _apply_entity_presence_status(
    entities: list[dict[str, Any]],
    event_index: dict[str, dict[str, Any]],
    speaker_turns: list[dict[str, Any]],
) -> None:
    runtime_ids: set[str] = set()
    for event in event_index.values():
        for field in ["actor_entity_id", "target_entity_id"]:
            value = event.get(field)
            if value:
                runtime_ids.add(str(value))
    for turn in speaker_turns:
        for field in ["speaker_entity_id", "addressee_entity_id"]:
            value = turn.get(field)
            if value:
                runtime_ids.add(str(value))
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "")
        if entity_id in runtime_ids:
            entity["presence_status"] = "present_or_narrating"
        else:
            entity["presence_status"] = "mentioned_historical"
            entity["auditor_note"] = "Mentioned in text but absent from resolved runtime roles in this pilot scope."


def _entity_cluster_key(aliases: list[str], fallback: str) -> str:
    for alias in aliases:
        core = _strip_honorific_key(alias)
        if core:
            return core
    return _strip_honorific_key(fallback) or _surface_key(fallback)


def _canonical_from_aliases(aliases: list[str]) -> str:
    for alias in aliases:
        if _surface_key(alias) == _strip_honorific_key(alias):
            return alias
    if aliases:
        stripped = _strip_leading_honorific(aliases[0])
        return stripped or aliases[0]
    return ""


def _surface_key(surface: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(surface).casefold()).strip("_")


def _strip_honorific_key(surface: str) -> str:
    return _surface_key(_strip_leading_honorific(surface))


def _strip_leading_honorific(surface: str) -> str:
    value = str(surface).strip()
    value = re.sub(r"^(ent_)?", "", value, flags=re.IGNORECASE)
    return re.sub(r"^(mr|mrs|miss|ms|dr|sir|madam|lady|lord)[._\s-]+", "", value, flags=re.IGNORECASE).strip()


def estimate_m1(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    window_target_tokens: int = 500,
    window_max_blocks: int = 8,
) -> dict[str, Any]:
    """Estimate the L2A-1 M1 B0+B1+B2 run without any model calls."""

    selected = select_chapters(document, chapters)
    calls: list[dict[str, Any]] = []
    max_prompt_tokens = 0
    total_prompt_tokens = 0
    chapter_summaries: list[dict[str, str]] = []
    window_count = 0
    for chapter_index, chapter in enumerate(selected):
        chapter_id = str(chapter["chapter_id"])
        neighbor_text = render_neighbor_summaries(
            neighbor_summaries_for_index(chapter_summaries, chapter_index, k=2)
        )
        brief_messages = build_chapter_brief_messages(
            design_doc=design_doc,
            chapter=chapter,
            registry_context_pack="",
            neighbor_summaries=neighbor_text,
        )
        brief_prompt_tokens = estimate_prompt_tokens(brief_messages, RESPONSE_FORMAT_JSON)
        max_prompt_tokens = max(max_prompt_tokens, brief_prompt_tokens)
        total_prompt_tokens += brief_prompt_tokens
        calls.append(
            {
                "chapter_id": chapter_id,
                "window_id": chapter_id,
                "mode": BRIEF_VERSION,
                "prompt_tokens_est": brief_prompt_tokens,
                "max_output_tokens": config.max_output_tokens,
            }
        )
        chapter_summaries.append(
            {
                "chapter_id": chapter_id,
                "summary": "(generated by chapter brief during real run)",
            }
        )
        for window in build_literary_windows(
            chapter,
            target_tokens=window_target_tokens,
            max_blocks=window_max_blocks,
        ):
            window_count += 1
            lex_messages = build_lexicon_messages(
                design_doc=design_doc,
                chapter_id=chapter_id,
                window=window,
                registry_context_pack="",
                chapter_brief="(generated by chapter brief during real run)",
                neighbor_summaries=neighbor_text,
            )
            narrative_messages = build_narrative_messages(
                design_doc=design_doc,
                chapter_id=chapter_id,
                window=window,
                narrator_hints=narrator_hints_for_window(window),
                chapter_roster="",
                window_mentions="",
                chapter_brief="(generated by chapter brief during real run)",
                neighbor_summaries=neighbor_text,
            )
            for mode, messages in [
                (LEXICON_VERSION, lex_messages),
                (NARRATIVE_VERSION, narrative_messages),
            ]:
                prompt_tokens = estimate_prompt_tokens(messages, RESPONSE_FORMAT_JSON)
                max_prompt_tokens = max(max_prompt_tokens, prompt_tokens)
                total_prompt_tokens += prompt_tokens
                calls.append(
                    {
                        "chapter_id": chapter_id,
                        "window_id": window.window_id,
                        "mode": mode,
                        "prompt_tokens_est": prompt_tokens,
                        "max_output_tokens": config.max_output_tokens,
                    }
                )
    upper_tokens = total_prompt_tokens + len(calls) * config.max_output_tokens
    cost_cap = _estimate_cost_cap(
        prompt_tokens=total_prompt_tokens,
        completion_tokens=len(calls) * config.max_output_tokens,
        config=config,
    )
    return {
        "phase": "L2A-1",
        "milestone": "M1",
        "zero_api": True,
        "prompt_source": str(design_doc),
        "chapters_requested": chapters,
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "calls": len(calls),
        "windows": window_count,
        "modes": [BRIEF_VERSION, LEXICON_VERSION, NARRATIVE_VERSION],
        "window_config": {
            "target_tokens": window_target_tokens,
            "max_blocks": window_max_blocks,
        },
        "prompt_tokens_est": total_prompt_tokens,
        "max_prompt_tokens_est": max_prompt_tokens,
        "max_output_tokens_per_call": config.max_output_tokens,
        "total_tokens_upper_bound": upper_tokens,
        "prompt_token_cap": config.prompt_token_cap,
        "cost_cap_usd": cost_cap,
        "call_estimates": calls,
        "token_growth_halt": max_prompt_tokens > int(config.prompt_token_cap or 10**12),
    }


def run_m1(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    client: LLMClient,
    out_dir: Path,
    confirm_usd: float,
    window_target_tokens: int = 500,
    window_max_blocks: int = 8,
    resume: bool = False,
) -> dict[str, Any]:
    lock = CheckpointLock(Path(out_dir))
    lock.acquire()
    try:
        return _run_m1_locked(
            document,
            chapters,
            design_doc=design_doc,
            config=config,
            client=client,
            out_dir=out_dir,
            confirm_usd=confirm_usd,
            window_target_tokens=window_target_tokens,
            window_max_blocks=window_max_blocks,
            resume=resume,
            lock_took_over_stale=lock.took_over_stale,
        )
    finally:
        lock.release()


def _run_m1_locked(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    client: LLMClient,
    out_dir: Path,
    confirm_usd: float,
    window_target_tokens: int = 500,
    window_max_blocks: int = 8,
    resume: bool = False,
    lock_took_over_stale: bool = False,
) -> dict[str, Any]:
    estimate = estimate_m1(
        document,
        chapters,
        design_doc=design_doc,
        config=config,
        window_target_tokens=window_target_tokens,
        window_max_blocks=window_max_blocks,
    )
    if estimate["token_growth_halt"]:
        raise SystemExit(
            "M1 refused: estimated prompt tokens exceed cap "
            f"{estimate['max_prompt_tokens_est']} > {estimate['prompt_token_cap']}"
        )
    if float(estimate["cost_cap_usd"]) > confirm_usd:
        raise SystemExit(
            "M1 refused: estimate cost cap "
            f"${estimate['cost_cap_usd']:.4f} exceeds --confirm-usd ${confirm_usd:.4f}"
        )

    selected = select_chapters(document, chapters)
    if resume:
        _require_resume_from_document_start(document, selected)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "brief").mkdir(parents=True, exist_ok=True)
    (out_dir / "lexicon").mkdir(parents=True, exist_ok=True)
    (out_dir / "narrative").mkdir(parents=True, exist_ok=True)
    ledger: dict[str, dict[str, Any]] = {}
    chapter_summaries: list[dict[str, str]] = []
    call_records: list[dict[str, Any]] = []
    validation_counts: dict[str, int] = {
        "brief_ok": 0,
        "brief_failed": 0,
        "seeded_cast": 0,
        "seed_skipped_cast": 0,
        "lexicon_ok": 0,
        "lexicon_failed": 0,
        "narrative_ok": 0,
        "narrative_failed": 0,
        "parse_fail": 0,
        "phase_leak": 0,
        "attribution_enum_dropped": 0,
        "attribution_enum_normalized": 0,
        "pronoun_dropped": 0,
        "mention_named_ids_cleared": 0,
        "named_pronoun_downgraded": 0,
        "named_ids_cleared": 0,
        "outside_window_neighbor_dropped": 0,
        "outside_window_nonexistent_dropped": 0,
        "context_only_used_true": 0,
        "brief_leak_tokens_dropped": 0,
        "nonperson_event_dropped": 0,
    }
    total_cost = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_reasoning_tokens = 0
    total_cached_tokens = 0
    cache_hits = 0
    calls = 0
    checkpoint_config = _checkpoint_config_hash(
        config,
        "m1",
        window_target_tokens=window_target_tokens,
        window_max_blocks=window_max_blocks,
    )
    restored_checkpoints: list[dict[str, Any]] = []
    resume_mismatches: list[dict[str, Any]] = []
    restored_accounting = _empty_accounting()
    restored_chapters: list[str] = []
    ran_chapters: list[str] = []
    checkpoint_parent_hash: str | None = None
    checkpoint_chain_clean = True
    if resume:
        restored_checkpoints, resume_mismatches = _load_valid_checkpoint_prefix(
            stage="m1",
            selected=selected,
            out_dir=out_dir,
            design_doc=design_doc,
            config_hash_value=checkpoint_config,
        )
        for checkpoint in restored_checkpoints:
            restored_chapters.append(str(checkpoint["chapter_id"]))
            restored_accounting = _add_accounting(
                restored_accounting, checkpoint.get("accounting") or {}
            )
            validation_counts = _add_counts(
                validation_counts, checkpoint.get("validation_counts") or {}
            )
            call_records.extend(checkpoint.get("call_records") or [])
        if restored_checkpoints:
            latest_state = restored_checkpoints[-1].get("state") or {}
            ledger = dict(latest_state.get("entity_ledger") or {})
            chapter_summaries = list(latest_state.get("chapter_summaries") or [])
            checkpoint_parent_hash = str(restored_checkpoints[-1]["checkpoint_hash"])
            calls = int(restored_accounting["attempts"])
            cache_hits = int(restored_accounting["cache_hits"])
            total_cost = float(restored_accounting["cost_usd"])
            total_prompt_tokens = int(restored_accounting["prompt_tokens"])
            total_cached_tokens = int(restored_accounting["cached_tokens"])
            total_completion_tokens = int(restored_accounting["completion_tokens"])
            total_reasoning_tokens = int(restored_accounting["reasoning_tokens"])
    start_index = len(restored_checkpoints)
    this_attempt_call_start = len(call_records)

    for chapter_index, chapter in enumerate(selected[start_index:], start=start_index):
        chapter_id = str(chapter["chapter_id"])
        ran_chapters.append(chapter_id)
        chapter_call_start = len(call_records)
        chapter_counts_before = dict(validation_counts)
        chapter_work = _chapter_work_dir(out_dir, "m1", chapter_id)
        (chapter_work / "brief").mkdir(parents=True, exist_ok=True)
        (chapter_work / "lexicon").mkdir(parents=True, exist_ok=True)
        (chapter_work / "narrative").mkdir(parents=True, exist_ok=True)
        block_ids = [str(block.get("block_id")) for block in chapter.get("blocks") or []]
        neighbor_text = render_neighbor_summaries(
            neighbor_summaries_for_index(
                chapter_summaries, chapter_index, k=NEIGHBOR_SUMMARY_K
            )
        )
        brief_messages = build_chapter_brief_messages(
            design_doc=design_doc,
            chapter=chapter,
            registry_context_pack=roster_from_ledger(ledger),
            neighbor_summaries=neighbor_text,
        )
        brief_result = _call_json_validated_chapter(
            client,
            brief_messages,
            tag=f"lit_m1_{chapter_id}_{BRIEF_VERSION}",
            mode=BRIEF_VERSION,
            chapter_id=chapter_id,
            block_ids=block_ids,
            out_path=chapter_work / "brief" / f"{chapter_id}.json",
            validate=lambda payload, ids=block_ids: validate_chapter_brief(
                payload,
                chapter_block_ids=ids,
            ),
        )
        calls += len(brief_result["attempts"])
        total_cost += float(brief_result["cost_usd"])
        total_prompt_tokens += int(brief_result["prompt_tokens"])
        total_completion_tokens += int(brief_result["completion_tokens"])
        total_reasoning_tokens += int(brief_result["reasoning_tokens"])
        total_cached_tokens += int(brief_result["cached_tokens"])
        cache_hits += int(brief_result["cache_hits"])
        call_records.append(_call_summary(brief_result))
        brief_validation = brief_result["validation"]
        validation_counts["brief_ok" if brief_validation["ok"] else "brief_failed"] += 1
        if brief_result["json_error"]:
            validation_counts["parse_fail"] += 1
        validation_counts["brief_leak_tokens_dropped"] += int(
            (brief_validation.get("counts") or {}).get("leak_tokens_dropped", 0)
        )
        brief_payload = brief_result.get("parsed_json") if brief_validation["ok"] else None
        chapter_brief_text = render_chapter_brief_for_injection(brief_payload)
        seed_report = seed_entity_ledger_from_chapter_brief(
            ledger,
            brief_payload,
            chapter_block_ids=block_ids,
        )
        validation_counts["seeded_cast"] += len(seed_report["seeded_cast"])
        validation_counts["seed_skipped_cast"] += len(seed_report["seed_skipped_cast"])
        chapter_summaries.append(
            {
                "chapter_id": chapter_id,
                "summary": safe_brief_neutral_premise(brief_payload)
                or "(brief unavailable)",
                "seed_report": seed_report,
            }
        )
        for window in build_literary_windows(
            chapter,
            target_tokens=window_target_tokens,
            max_blocks=window_max_blocks,
        ):
            lex_messages = build_lexicon_messages(
                design_doc=design_doc,
                chapter_id=chapter_id,
                window=window,
                registry_context_pack=registry_context_from_ledger(ledger, window),
                chapter_brief=chapter_brief_text,
                neighbor_summaries=neighbor_text,
            )
            lex_result = _call_json_validated(
                client,
                lex_messages,
                tag=f"lit_m1_{window.window_id}_{LEXICON_VERSION}",
                mode=LEXICON_VERSION,
                window=window,
                out_path=chapter_work / "lexicon" / f"{window.window_id}.json",
                validate=lambda payload: validate_lexicon(
                    payload,
                    valid_block_ids=set(window.block_ids),
                    chapter_block_ids=set(block_ids),
                    known_entity_ids=set(ledger),
                ),
            )
            calls += len(lex_result["attempts"])
            total_cost += float(lex_result["cost_usd"])
            total_prompt_tokens += int(lex_result["prompt_tokens"])
            total_completion_tokens += int(lex_result["completion_tokens"])
            total_reasoning_tokens += int(lex_result["reasoning_tokens"])
            total_cached_tokens += int(lex_result["cached_tokens"])
            cache_hits += int(lex_result["cache_hits"])
            call_records.append(_call_summary(lex_result))
            lex_validation = lex_result["validation"]
            if lex_validation["ok"]:
                validation_counts["lexicon_ok"] += 1
                update_entity_ledger_from_lexicon(ledger, lex_result["parsed_json"] or {})
            else:
                validation_counts["lexicon_failed"] += 1
            if lex_result["json_error"]:
                validation_counts["parse_fail"] += 1
            validation_counts["context_only_used_true"] += int(
                (lex_validation.get("counts") or {}).get("context_only_used_true", 0)
            )
            validation_counts["pronoun_dropped"] += int(
                (lex_validation.get("counts") or {}).get("pronoun_dropped", 0)
            )
            validation_counts["mention_named_ids_cleared"] += int(
                (lex_validation.get("counts") or {}).get("mention_named_ids_cleared", 0)
            )
            validation_counts["outside_window_neighbor_dropped"] += int(
                (lex_validation.get("counts") or {}).get("outside_window_neighbor_dropped", 0)
            )
            validation_counts["outside_window_nonexistent_dropped"] += int(
                (lex_validation.get("counts") or {}).get("outside_window_nonexistent_dropped", 0)
            )

            narrative_messages = build_narrative_messages(
                design_doc=design_doc,
                chapter_id=chapter_id,
                window=window,
                narrator_hints=narrator_hints_for_window(window),
                chapter_roster=roster_from_ledger(ledger),
                window_mentions=mentions_summary(lex_result["parsed_json"] or {}),
                chapter_brief=chapter_brief_text,
                neighbor_summaries=neighbor_text,
            )
            narrative_result = _call_json_validated(
                client,
                narrative_messages,
                tag=f"lit_m1_{window.window_id}_{NARRATIVE_VERSION}",
                mode=NARRATIVE_VERSION,
                window=window,
                out_path=chapter_work / "narrative" / f"{window.window_id}.json",
                validate=lambda payload: validate_narrative(
                    payload,
                    valid_block_ids=set(window.block_ids),
                    chapter_block_ids=set(block_ids),
                    known_entity_ids=set(ledger),
                ),
            )
            calls += len(narrative_result["attempts"])
            total_cost += float(narrative_result["cost_usd"])
            total_prompt_tokens += int(narrative_result["prompt_tokens"])
            total_completion_tokens += int(narrative_result["completion_tokens"])
            total_reasoning_tokens += int(narrative_result["reasoning_tokens"])
            total_cached_tokens += int(narrative_result["cached_tokens"])
            cache_hits += int(narrative_result["cache_hits"])
            call_records.append(_call_summary(narrative_result))
            narrative_validation = narrative_result["validation"]
            if narrative_validation["ok"]:
                validation_counts["narrative_ok"] += 1
            else:
                validation_counts["narrative_failed"] += 1
            if narrative_result["json_error"]:
                validation_counts["parse_fail"] += 1
            validation_counts["phase_leak"] += int(
                (narrative_validation.get("counts") or {}).get("phase_leak", 0)
            )
            validation_counts["attribution_enum_dropped"] += int(
                (narrative_validation.get("counts") or {}).get("attribution_enum_dropped", 0)
            )
            validation_counts["attribution_enum_normalized"] += int(
                (narrative_validation.get("counts") or {}).get("attribution_enum_normalized", 0)
            )
            validation_counts["named_pronoun_downgraded"] += int(
                (narrative_validation.get("counts") or {}).get("named_pronoun_downgraded", 0)
            )
            validation_counts["named_ids_cleared"] += int(
                (narrative_validation.get("counts") or {}).get("named_ids_cleared", 0)
            )
            validation_counts["nonperson_event_dropped"] += int(
                (narrative_validation.get("counts") or {}).get("nonperson_event_dropped", 0)
            )
            validation_counts["outside_window_neighbor_dropped"] += int(
                (narrative_validation.get("counts") or {}).get("outside_window_neighbor_dropped", 0)
            )
            validation_counts["outside_window_nonexistent_dropped"] += int(
                (narrative_validation.get("counts") or {}).get("outside_window_nonexistent_dropped", 0)
            )
            validation_counts["context_only_used_true"] += int(
                (narrative_validation.get("counts") or {}).get("context_only_used_true", 0)
            )
        promoted = _promote_chapter_artifacts(chapter_work, out_dir, "m1", chapter_id)
        chapter_counts = _diff_counts(validation_counts, chapter_counts_before)
        chapter_calls = call_records[chapter_call_start:]
        chapter_accounting = _chapter_accounting(chapter_calls)
        if checkpoint_chain_clean and _chapter_checkpoint_clean("m1", chapter_counts):
            checkpoint_base = {
                **_checkpoint_expected(
                    stage="m1",
                    chapter=chapter,
                    chapter_index=chapter_index,
                    chapter_sequence_prefix=[
                        str(item["chapter_id"]) for item in selected[: chapter_index + 1]
                    ],
                    design_doc=design_doc,
                    config_hash_value=checkpoint_config,
                    parent_checkpoint_hash=checkpoint_parent_hash,
                ),
                "state": {
                    "entity_ledger": ledger,
                    "chapter_summaries": chapter_summaries,
                },
                "cast_seed_report": seed_report,
                "validation_counts": chapter_counts,
                "accounting": chapter_accounting,
                "call_records": chapter_calls,
                "artifact_manifest": artifact_manifest(promoted, root=out_dir),
            }
            checkpoint = build_checkpoint(checkpoint_base)
            write_checkpoint_atomic(
                _checkpoint_path(out_dir, "m1", chapter_id), checkpoint
            )
            checkpoint_parent_hash = str(checkpoint["checkpoint_hash"])
        else:
            checkpoint_chain_clean = False

    ran_artifact_accounting = _chapter_accounting(
        call_records[this_attempt_call_start:]
    )
    this_attempt_accounting = _incremental_accounting_view(ran_artifact_accounting)
    combined_accounting = _add_accounting(restored_accounting, ran_artifact_accounting)
    report = {
        "phase": "L2A-1",
        "milestone": "M1",
        "status": "needs_claude_gate",
        "prompt_source": str(design_doc),
        "model": config.model,
        "chapters_requested": chapters,
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "modes": [BRIEF_VERSION, LEXICON_VERSION, NARRATIVE_VERSION],
        "window_config": {
            "target_tokens": window_target_tokens,
            "max_blocks": window_max_blocks,
        },
        "estimate": estimate,
        "actual": {
            "calls": calls,
            "cache_hits": cache_hits,
            "cost_usd": round(total_cost, 12),
            "prompt_tokens": total_prompt_tokens,
            "cached_tokens": total_cached_tokens,
            "completion_tokens": total_completion_tokens,
            "reasoning_tokens": total_reasoning_tokens,
        },
        "accounting_resume": {
            "restored_total": restored_accounting,
            "this_attempt": this_attempt_accounting,
            "combined_total": combined_accounting,
        },
        "resume": {
            "enabled": resume,
            "resumed_from_checkpoint": restored_chapters,
            "ran": ran_chapters,
            "mismatches": resume_mismatches,
            "lock_took_over_stale": lock_took_over_stale,
        },
        "checkpoint_config_hash": checkpoint_config,
        "validation_counts": validation_counts,
        "cast_seed_report": {
            "chapters": [
                {
                    "chapter_id": item["chapter_id"],
                    "seeded_cast": item.get("seed_report", {}).get("seeded_cast", []),
                    "seed_skipped_cast": item.get("seed_report", {}).get("seed_skipped_cast", []),
                }
                for item in chapter_summaries
            ]
        },
        "entity_ledger_size": len(ledger),
        "entity_ledger": ledger,
        "chapter_summaries": chapter_summaries,
        "call_records": call_records,
        "artifacts": {
            "brief_dir": str(out_dir / "brief"),
            "lexicon_dir": str(out_dir / "lexicon"),
            "narrative_dir": str(out_dir / "narrative"),
            "report": str(out_dir / "m1_report.json"),
        },
        "stop": "M1 complete. Claude must verify artifacts before M2.",
    }
    (out_dir / "m1_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def estimate_m2(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    m1_dir: Path,
) -> dict[str, Any]:
    """Estimate the L2A-1 M2 chapter digest run without model calls."""

    selected = select_chapters(document, chapters)
    m1_report = _load_m1_report(m1_dir)
    m1_checkpoints = _m1_checkpoint_chain_for_m2(
        selected=selected,
        m1_dir=m1_dir,
        design_doc=design_doc,
        m1_report=m1_report,
    )
    fallback_roster = _chapter_roster_from_m1(m1_report)
    calls: list[dict[str, Any]] = []
    max_prompt_tokens = 0
    total_prompt_tokens = 0
    chapter_summaries: list[dict[str, str]] = []
    for chapter_index, chapter in enumerate(selected):
        relation_events = _chapter_relation_events_from_m1(m1_dir, chapter)
        chapter_id = str(chapter["chapter_id"])
        chapter_roster = (
            roster_from_ledger(
                (m1_checkpoints[chapter_index].get("state") or {}).get("entity_ledger") or {}
            )
            if m1_checkpoints
            else fallback_roster
        )
        neighbor_text = render_neighbor_summaries(
            neighbor_summaries_for_index(
                chapter_summaries, chapter_index, k=NEIGHBOR_SUMMARY_K
            )
        )
        messages = build_digest_messages(
            design_doc=design_doc,
            chapter=chapter,
            previous_summary="",
            neighbor_summaries=neighbor_text,
            chapter_brief=_chapter_brief_from_m1(m1_dir, chapter_id),
            chapter_roster=chapter_roster,
            chapter_relation_events=relation_events,
        )
        prompt_tokens = estimate_prompt_tokens(messages, RESPONSE_FORMAT_JSON)
        max_prompt_tokens = max(max_prompt_tokens, prompt_tokens)
        total_prompt_tokens += prompt_tokens
        calls.append(
            {
                "chapter_id": chapter_id,
                "mode": DIGEST_VERSION,
                "prompt_tokens_est": prompt_tokens,
                "max_output_tokens": config.max_output_tokens,
                "neighbor_summary_count": len(
                    neighbor_summaries_for_index(
                        chapter_summaries, chapter_index, k=NEIGHBOR_SUMMARY_K
                    )
                ),
                "relation_event_lines": len(
                    [line for line in relation_events.splitlines() if line.strip()]
                ),
            }
        )
        chapter_summaries.append(
            {
                "chapter_id": chapter_id,
                "summary": "(generated by this chapter digest during real run)",
            }
        )
    upper_tokens = total_prompt_tokens + len(calls) * config.max_output_tokens
    cost_cap = _estimate_cost_cap(
        prompt_tokens=total_prompt_tokens,
        completion_tokens=len(calls) * config.max_output_tokens,
        config=config,
    )
    return {
        "phase": "L2A-1",
        "milestone": "M2",
        "zero_api": True,
        "prompt_source": str(design_doc),
        "m1_report": str(Path(m1_dir) / "m1_report.json"),
        "chapters_requested": chapters,
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "calls": len(calls),
        "modes": [DIGEST_VERSION],
        "prompt_tokens_est": total_prompt_tokens,
        "max_prompt_tokens_est": max_prompt_tokens,
        "max_output_tokens_per_call": config.max_output_tokens,
        "total_tokens_upper_bound": upper_tokens,
        "prompt_token_cap": config.prompt_token_cap,
        "cost_cap_usd": cost_cap,
        "call_estimates": calls,
        "token_growth_halt": max_prompt_tokens > int(config.prompt_token_cap or 10**12),
    }


def run_m2(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    client: LLMClient,
    out_dir: Path,
    m1_dir: Path,
    confirm_usd: float,
    resume: bool = False,
) -> dict[str, Any]:
    lock = CheckpointLock(Path(out_dir))
    lock.acquire()
    try:
        return _run_m2_locked(
            document,
            chapters,
            design_doc=design_doc,
            config=config,
            client=client,
            out_dir=out_dir,
            m1_dir=m1_dir,
            confirm_usd=confirm_usd,
            resume=resume,
            lock_took_over_stale=lock.took_over_stale,
        )
    finally:
        lock.release()


def _run_m2_locked(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    client: LLMClient,
    out_dir: Path,
    m1_dir: Path,
    confirm_usd: float,
    resume: bool = False,
    lock_took_over_stale: bool = False,
) -> dict[str, Any]:
    estimate = estimate_m2(
        document,
        chapters,
        design_doc=design_doc,
        config=config,
        m1_dir=m1_dir,
    )
    if estimate["token_growth_halt"]:
        raise SystemExit(
            "M2 refused: estimated prompt tokens exceed cap "
            f"{estimate['max_prompt_tokens_est']} > {estimate['prompt_token_cap']}"
        )
    if float(estimate["cost_cap_usd"]) > confirm_usd:
        raise SystemExit(
            "M2 refused: estimate cost cap "
            f"${estimate['cost_cap_usd']:.4f} exceeds --confirm-usd ${confirm_usd:.4f}"
        )

    selected = select_chapters(document, chapters)
    if resume:
        _require_resume_from_document_start(document, selected)
    m1_report = _load_m1_report(m1_dir)
    m1_checkpoints = _m1_checkpoint_chain_for_m2(
        selected=selected,
        m1_dir=m1_dir,
        design_doc=design_doc,
        m1_report=m1_report,
    )
    fallback_roster = _chapter_roster_from_m1(m1_report)
    input_m1_hashes = {
        str(item["chapter_id"]): str(item["checkpoint_hash"]) for item in m1_checkpoints
    }
    digest_dir = Path(out_dir) / "digest"
    digest_dir.mkdir(parents=True, exist_ok=True)
    call_records: list[dict[str, Any]] = []
    validation_counts: dict[str, int] = {
        "digest_ok": 0,
        "digest_failed": 0,
        "parse_fail": 0,
        "frame_segments": 0,
        "scenes": 0,
        "state_changes": 0,
        "translator_facts": 0,
        "motifs": 0,
    }
    total_cost = 0.0
    total_prompt_tokens = 0
    total_cached_tokens = 0
    total_completion_tokens = 0
    total_reasoning_tokens = 0
    cache_hits = 0
    calls = 0
    chapter_summaries: list[dict[str, str]] = []
    checkpoint_config = _checkpoint_config_hash(config, "m2")
    restored_checkpoints: list[dict[str, Any]] = []
    resume_mismatches: list[dict[str, Any]] = []
    restored_accounting = _empty_accounting()
    restored_chapters: list[str] = []
    ran_chapters: list[str] = []
    checkpoint_parent_hash: str | None = None
    checkpoint_chain_clean = True
    if resume:
        restored_checkpoints, resume_mismatches = _load_valid_checkpoint_prefix(
            stage="m2",
            selected=selected,
            out_dir=out_dir,
            design_doc=design_doc,
            config_hash_value=checkpoint_config,
            input_m1_hashes=input_m1_hashes,
        )
        for checkpoint in restored_checkpoints:
            restored_chapters.append(str(checkpoint["chapter_id"]))
            restored_accounting = _add_accounting(
                restored_accounting, checkpoint.get("accounting") or {}
            )
            validation_counts = _add_counts(
                validation_counts, checkpoint.get("validation_counts") or {}
            )
            call_records.extend(checkpoint.get("call_records") or [])
        if restored_checkpoints:
            chapter_summaries = list(
                (restored_checkpoints[-1].get("state") or {}).get("chapter_summaries") or []
            )
            checkpoint_parent_hash = str(restored_checkpoints[-1]["checkpoint_hash"])
            calls = int(restored_accounting["attempts"])
            cache_hits = int(restored_accounting["cache_hits"])
            total_cost = float(restored_accounting["cost_usd"])
            total_prompt_tokens = int(restored_accounting["prompt_tokens"])
            total_cached_tokens = int(restored_accounting["cached_tokens"])
            total_completion_tokens = int(restored_accounting["completion_tokens"])
            total_reasoning_tokens = int(restored_accounting["reasoning_tokens"])
    start_index = len(restored_checkpoints)
    this_attempt_call_start = len(call_records)

    for chapter_index, chapter in enumerate(selected[start_index:], start=start_index):
        chapter_id = str(chapter["chapter_id"])
        ran_chapters.append(chapter_id)
        chapter_call_start = len(call_records)
        chapter_counts_before = dict(validation_counts)
        chapter_work = _chapter_work_dir(out_dir, "m2", chapter_id)
        (chapter_work / "digest").mkdir(parents=True, exist_ok=True)
        block_ids = [str(block.get("block_id")) for block in chapter.get("blocks") or []]
        relation_events = _chapter_relation_events_from_m1(m1_dir, chapter)
        chapter_roster = (
            roster_from_ledger(
                (m1_checkpoints[chapter_index].get("state") or {}).get("entity_ledger") or {}
            )
            if m1_checkpoints
            else fallback_roster
        )
        neighbor_text = render_neighbor_summaries(
            neighbor_summaries_for_index(
                chapter_summaries, chapter_index, k=NEIGHBOR_SUMMARY_K
            )
        )
        messages = build_digest_messages(
            design_doc=design_doc,
            chapter=chapter,
            previous_summary="",
            neighbor_summaries=neighbor_text,
            chapter_brief=_chapter_brief_from_m1(m1_dir, chapter_id),
            chapter_roster=chapter_roster,
            chapter_relation_events=relation_events,
        )
        digest_result = _call_json_validated_chapter(
            client,
            messages,
            tag=f"lit_m2_{chapter_id}_{DIGEST_VERSION}",
            mode=DIGEST_VERSION,
            chapter_id=chapter_id,
            block_ids=block_ids,
            out_path=chapter_work / "digest" / f"{chapter_id}.json",
            validate=lambda payload, ids=block_ids: validate_digest(
                payload,
                chapter_block_ids=ids,
            ),
        )
        calls += len(digest_result["attempts"])
        total_cost += float(digest_result["cost_usd"])
        total_prompt_tokens += int(digest_result["prompt_tokens"])
        total_cached_tokens += int(digest_result["cached_tokens"])
        total_completion_tokens += int(digest_result["completion_tokens"])
        total_reasoning_tokens += int(digest_result["reasoning_tokens"])
        cache_hits += int(digest_result["cache_hits"])
        call_records.append(_call_summary(digest_result))
        validation = digest_result["validation"]
        validation_counts["digest_ok" if validation["ok"] else "digest_failed"] += 1
        if digest_result["json_error"]:
            validation_counts["parse_fail"] += 1
        for key in ["frame_segments", "scenes", "state_changes", "translator_facts", "motifs"]:
            validation_counts[key] += int((validation.get("counts") or {}).get(key, 0))
        parsed = digest_result.get("parsed_json") or {}
        if isinstance(parsed, dict):
            digest_summary = str(parsed.get("chapter_rolling_summary") or "")
            chapter_summaries.append(
                {
                    "chapter_id": chapter_id,
                    "summary": digest_summary.strip() or "(digest summary unavailable)",
                }
            )
        promoted = _promote_chapter_artifacts(chapter_work, out_dir, "m2", chapter_id)
        chapter_counts = _diff_counts(validation_counts, chapter_counts_before)
        chapter_calls = call_records[chapter_call_start:]
        chapter_accounting = _chapter_accounting(chapter_calls)
        if checkpoint_chain_clean and _chapter_checkpoint_clean("m2", chapter_counts):
            checkpoint_base = {
                **_checkpoint_expected(
                    stage="m2",
                    chapter=chapter,
                    chapter_index=chapter_index,
                    chapter_sequence_prefix=[
                        str(item["chapter_id"]) for item in selected[: chapter_index + 1]
                    ],
                    design_doc=design_doc,
                    config_hash_value=checkpoint_config,
                    parent_checkpoint_hash=checkpoint_parent_hash,
                    input_m1_checkpoint_hash=input_m1_hashes.get(chapter_id),
                ),
                "state": {"chapter_summaries": chapter_summaries},
                "digest_summary": digest_summary if isinstance(parsed, dict) else "",
                "validation_counts": chapter_counts,
                "accounting": chapter_accounting,
                "call_records": chapter_calls,
                "artifact_manifest": artifact_manifest(promoted, root=out_dir),
            }
            checkpoint = build_checkpoint(checkpoint_base)
            write_checkpoint_atomic(
                _checkpoint_path(out_dir, "m2", chapter_id), checkpoint
            )
            checkpoint_parent_hash = str(checkpoint["checkpoint_hash"])
        else:
            checkpoint_chain_clean = False

    ran_artifact_accounting = _chapter_accounting(
        call_records[this_attempt_call_start:]
    )
    this_attempt_accounting = _incremental_accounting_view(ran_artifact_accounting)
    combined_accounting = _add_accounting(restored_accounting, ran_artifact_accounting)
    report = {
        "phase": "L2A-1",
        "milestone": "M2",
        "status": "needs_claude_gate",
        "prompt_source": str(design_doc),
        "m1_report": str(Path(m1_dir) / "m1_report.json"),
        "model": config.model,
        "chapters_requested": chapters,
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "modes": [DIGEST_VERSION],
        "estimate": estimate,
        "actual": {
            "calls": calls,
            "cache_hits": cache_hits,
            "cost_usd": round(total_cost, 12),
            "prompt_tokens": total_prompt_tokens,
            "cached_tokens": total_cached_tokens,
            "completion_tokens": total_completion_tokens,
            "reasoning_tokens": total_reasoning_tokens,
        },
        "accounting_resume": {
            "restored_total": restored_accounting,
            "this_attempt": this_attempt_accounting,
            "combined_total": combined_accounting,
        },
        "resume": {
            "enabled": resume,
            "resumed_from_checkpoint": restored_chapters,
            "ran": ran_chapters,
            "mismatches": resume_mismatches,
            "lock_took_over_stale": lock_took_over_stale,
        },
        "checkpoint_config_hash": checkpoint_config,
        "input_m1_checkpoint_hashes": input_m1_hashes,
        "validation_counts": validation_counts,
        "chapter_summaries": chapter_summaries,
        "call_records": call_records,
        "artifacts": {
            "digest_dir": str(digest_dir),
            "report": str(Path(out_dir) / "m2_report.json"),
        },
        "stop": "M2 complete. Claude must verify digest before M3.",
    }
    (Path(out_dir) / "m2_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def estimate_m3(
    document: dict[str, Any],
    chapters: list[str],
    *,
    m1_dir: Path,
    digest_dir: Path,
) -> dict[str, Any]:
    selected = select_chapters(document, chapters)
    _load_m1_report(m1_dir)
    for chapter in selected:
        _load_digest_payload(digest_dir, str(chapter["chapter_id"]))
    return {
        "phase": "L2A-1",
        "milestone": "M3",
        "zero_api": True,
        "m1_report": str(Path(m1_dir) / "m1_report.json"),
        "digest_dir": str(digest_dir),
        "chapters_requested": chapters,
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "calls": 0,
        "modes": [CONSOLIDATE_VERSION],
        "cost_cap_usd": 0.0,
        "micro_calls": 0,
        "note": (
            "M3 scaffold is zero-API: candidate refs resolve from B1/B2 evidence, "
            "unresolved refs stay unresolved, and relation phases use a generic "
            "valence fallback marked needs_human_review."
        ),
    }


def run_m3(
    document: dict[str, Any],
    chapters: list[str],
    *,
    out_dir: Path,
    m1_dir: Path,
    digest_dir: Path,
) -> dict[str, Any]:
    estimate = estimate_m3(document, chapters, m1_dir=m1_dir, digest_dir=digest_dir)
    selected = select_chapters(document, chapters)
    m1_report = _load_m1_report(m1_dir)
    story_dir = Path(out_dir) / "story_bible"
    story_dir.mkdir(parents=True, exist_ok=True)
    stories: list[dict[str, Any]] = []
    validation_counts: dict[str, int] = {
        "story_bible_ok": 0,
        "story_bible_failed": 0,
        "entities": 0,
        "aliases_with_valid_range": 0,
        "phases": 0,
        "open_intervals": 0,
        "entity_state_intervals": 0,
        "address_policies_proposed": 0,
        "address_dirs_unsupported": 0,
        "state_changes_dropped_temporary": 0,
        "canary_pass": 0,
        "canary_fail": 0,
    }
    call_records: list[dict[str, Any]] = []
    for chapter in selected:
        chapter_id = str(chapter["chapter_id"])
        digest_payload = _load_digest_payload(digest_dir, chapter_id)
        story = _build_story_bible_chapter(
            chapter=chapter,
            m1_dir=Path(m1_dir),
            m1_report=m1_report,
            digest=digest_payload,
        )
        validation = validate_story_bible(story)
        story["validation"] = validation.to_dict()
        story_path = story_dir / f"{chapter_id}_story_bible.json"
        story_path.write_text(
            json.dumps(story, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stories.append({"chapter_id": chapter_id, "path": str(story_path), "validation": validation.to_dict()})
        validation_counts["story_bible_ok" if validation.ok else "story_bible_failed"] += 1
        for key in [
            "entities",
            "aliases_with_valid_range",
            "phases",
            "open_intervals",
            "entity_state_intervals",
            "address_policies_proposed",
            "address_dirs_unsupported",
        ]:
            validation_counts[key] += int(validation.counts.get(key, 0))
        audit = story.get("audit") or {}
        validation_counts["state_changes_dropped_temporary"] += int(
            audit.get("state_changes_dropped_temporary") or 0
        )
        canary = story.get("canary_report") or {}
        validation_counts["canary_pass" if canary.get("pass") else "canary_fail"] += 1
        call_records.append(
            {
                "mode": CONSOLIDATE_VERSION,
                "window_id": chapter_id,
                "ok": validation.ok,
                "attempts": 0,
                "cost_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cache_hits": 0,
                "errors": validation.errors,
                "counts": validation.counts,
            }
        )
    report = {
        "phase": "L2A-1",
        "milestone": "M3",
        "status": "needs_claude_gate",
        "m1_report": str(Path(m1_dir) / "m1_report.json"),
        "digest_dir": str(digest_dir),
        "chapters_requested": chapters,
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "modes": [CONSOLIDATE_VERSION],
        "estimate": estimate,
        "actual": {
            "calls": 0,
            "cache_hits": 0,
            "cost_usd": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
        },
        "validation_counts": validation_counts,
        "call_records": call_records,
        "artifacts": {
            "story_bible_dir": str(story_dir),
            "stories": stories,
            "report": str(Path(out_dir) / "m3_report.json"),
        },
        "stop": "M3 complete. Claude must verify Story Bible before M4.",
    }
    (Path(out_dir) / "m3_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def revalidate_m1_artifacts(out_dir: Path) -> dict[str, Any]:
    report_path = out_dir / "m1_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"M1 report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    call_records: list[dict[str, Any]] = []
    validation_counts: dict[str, int] = {
        "brief_ok": 0,
        "brief_failed": 0,
        "lexicon_ok": 0,
        "lexicon_failed": 0,
        "narrative_ok": 0,
        "narrative_failed": 0,
        "parse_fail": 0,
        "phase_leak": 0,
        "attribution_enum_dropped": 0,
        "attribution_enum_normalized": 0,
        "pronoun_dropped": 0,
        "mention_named_ids_cleared": 0,
        "named_pronoun_downgraded": 0,
        "named_ids_cleared": 0,
        "outside_window_neighbor_dropped": 0,
        "outside_window_nonexistent_dropped": 0,
        "context_only_used_true": 0,
        "brief_leak_tokens_dropped": 0,
        "nonperson_event_dropped": 0,
    }
    chapter_blocks_by_id: dict[str, set[str]] = {}
    for brief_path in sorted((out_dir / "brief").glob("*.json")):
        brief_payload = json.loads(brief_path.read_text(encoding="utf-8"))
        chapter_id = str(brief_payload.get("chapter_id") or "")
        if chapter_id:
            chapter_blocks_by_id[chapter_id] = {
                str(block_id) for block_id in brief_payload.get("block_ids") or []
            }
    for subdir, mode in [
        ("brief", BRIEF_VERSION),
        ("lexicon", LEXICON_VERSION),
        ("narrative", NARRATIVE_VERSION),
    ]:
        for artifact_path in sorted((out_dir / subdir).glob("*.json")):
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            parsed = payload.get("parsed_json")
            valid_block_ids = set(str(block_id) for block_id in payload.get("block_ids") or [])
            chapter_block_ids = chapter_blocks_by_id.get(
                str(payload.get("chapter_id") or ""),
                valid_block_ids,
            )
            known_ids = _known_ids_from_report(report)
            if not isinstance(parsed, dict):
                validation = ValidationReport(
                    name=mode,
                    ok=False,
                    errors=[f"json_parse_error: {payload.get('json_error')}"],
                    warnings=[],
                    counts={},
                )
                validation_counts["parse_fail"] += 1
            elif mode == BRIEF_VERSION:
                validation = validate_chapter_brief(
                    parsed,
                    chapter_block_ids=list(valid_block_ids),
                )
            elif mode == LEXICON_VERSION:
                validation = validate_lexicon(
                    parsed,
                    valid_block_ids=valid_block_ids,
                    chapter_block_ids=chapter_block_ids,
                    known_entity_ids=known_ids,
                )
            else:
                validation = validate_narrative(
                    parsed,
                    valid_block_ids=valid_block_ids,
                    chapter_block_ids=chapter_block_ids,
                    known_entity_ids=known_ids,
                )
            payload["validation"] = validation.to_dict()
            artifact_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if mode == BRIEF_VERSION:
                validation_counts["brief_ok" if validation.ok else "brief_failed"] += 1
                validation_counts["brief_leak_tokens_dropped"] += int(
                    validation.counts.get("leak_tokens_dropped", 0)
                )
            elif mode == LEXICON_VERSION:
                validation_counts["lexicon_ok" if validation.ok else "lexicon_failed"] += 1
                validation_counts["pronoun_dropped"] += int(
                    validation.counts.get("pronoun_dropped", 0)
                )
                validation_counts["mention_named_ids_cleared"] += int(
                    validation.counts.get("mention_named_ids_cleared", 0)
                )
            else:
                validation_counts["narrative_ok" if validation.ok else "narrative_failed"] += 1
                validation_counts["phase_leak"] += int(validation.counts.get("phase_leak", 0))
                validation_counts["attribution_enum_dropped"] += int(
                    validation.counts.get("attribution_enum_dropped", 0)
                )
                validation_counts["attribution_enum_normalized"] += int(
                    validation.counts.get("attribution_enum_normalized", 0)
                )
                validation_counts["named_pronoun_downgraded"] += int(
                    validation.counts.get("named_pronoun_downgraded", 0)
                )
                validation_counts["named_ids_cleared"] += int(
                    validation.counts.get("named_ids_cleared", 0)
                )
                validation_counts["nonperson_event_dropped"] += int(
                    validation.counts.get("nonperson_event_dropped", 0)
                )
            validation_counts["outside_window_neighbor_dropped"] += int(
                validation.counts.get("outside_window_neighbor_dropped", 0)
            )
            validation_counts["outside_window_nonexistent_dropped"] += int(
                validation.counts.get("outside_window_nonexistent_dropped", 0)
            )
            validation_counts["context_only_used_true"] += int(
                validation.counts.get("context_only_used_true", 0)
            )
            call_records.append(_call_summary(payload))
    report["validation_counts"] = validation_counts
    report["call_records"] = call_records
    report["status"] = "needs_claude_gate"
    report["revalidated_zero_api"] = True
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _known_ids_from_report(report: dict[str, Any]) -> set[str]:
    return set((report.get("entity_ledger") or {}).keys())


def _estimate_cost_cap(*, prompt_tokens: int, completion_tokens: int, config: LLMConfig) -> float:
    pricing = config.pricing
    cost = (
        (prompt_tokens / 1_000_000) * pricing["input"]
        + (completion_tokens / 1_000_000) * pricing["output"]
    )
    return round(cost, 12)


def _call_json_validated(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    tag: str,
    mode: str,
    window: LiteraryWindow,
    out_path: Path,
    validate: Any,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    current_messages = messages
    parsed: dict[str, Any] | None = None
    validation = ValidationReport(mode, False, ["not run"], [], {})
    json_error: str | None = None
    for attempt_index in [1, 2]:
        result = client.call(
            current_messages,
            response_format=RESPONSE_FORMAT_JSON,
            tag=f"{tag}:attempt{attempt_index}",
        )
        json_error = result.json_error
        if isinstance(result.parsed_json, dict):
            parsed = result.parsed_json
            validation = validate(parsed)
        else:
            validation = ValidationReport(
                name=mode,
                ok=False,
                errors=[f"json_parse_error: {result.json_error}"],
                warnings=[],
                counts={},
            )
        attempts.append(_llm_attempt_payload(result, validation, attempt_index))
        if validation.ok:
            break
        if attempt_index == 1:
            current_messages = _build_validation_retry_messages(
                messages,
                prior_output=result.text,
                validation_errors=validation.errors,
            )
    payload = {
        "mode": mode,
        "window_id": window.window_id,
        "chapter_id": window.chapter_id,
        "block_ids": window.block_ids,
        "previous_tail_block_ids": [str(block["block_id"]) for block in window.previous_tail],
        "next_tail_block_ids": [str(block["block_id"]) for block in window.next_tail],
        "messages": messages,
        "parsed_json": parsed,
        "json_error": json_error,
        "validation": validation.to_dict(),
        "attempts": attempts,
        "cost_usd": round(sum(float(item["cost_usd"]) for item in attempts), 12),
        "prompt_tokens": sum(int(item["usage"]["prompt_tokens"]) for item in attempts),
        "cached_tokens": sum(int(item["usage"]["cached_tokens"]) for item in attempts),
        "completion_tokens": sum(int(item["usage"]["completion_tokens"]) for item in attempts),
        "reasoning_tokens": sum(int(item["usage"]["reasoning_tokens"]) for item in attempts),
        "cache_hits": sum(1 for item in attempts if item["from_cache"]),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _call_json_validated_chapter(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    tag: str,
    mode: str,
    chapter_id: str,
    block_ids: list[str],
    out_path: Path,
    validate: Any,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    current_messages = messages
    parsed: dict[str, Any] | None = None
    validation = ValidationReport(mode, False, ["not run"], [], {})
    json_error: str | None = None
    for attempt_index in [1, 2]:
        result = client.call(
            current_messages,
            response_format=RESPONSE_FORMAT_JSON,
            tag=f"{tag}:attempt{attempt_index}",
        )
        json_error = result.json_error
        if isinstance(result.parsed_json, dict):
            parsed = result.parsed_json
            validation = validate(parsed)
        else:
            validation = ValidationReport(
                name=mode,
                ok=False,
                errors=[f"json_parse_error: {result.json_error}"],
                warnings=[],
                counts={},
            )
        attempts.append(_llm_attempt_payload(result, validation, attempt_index))
        if validation.ok:
            break
        if attempt_index == 1:
            current_messages = _build_validation_retry_messages(
                messages,
                prior_output=result.text,
                validation_errors=validation.errors,
            )
    payload = {
        "mode": mode,
        "window_id": chapter_id,
        "chapter_id": chapter_id,
        "block_ids": block_ids,
        "messages": messages,
        "parsed_json": parsed,
        "json_error": json_error,
        "validation": validation.to_dict(),
        "attempts": attempts,
        "cost_usd": round(sum(float(item["cost_usd"]) for item in attempts), 12),
        "prompt_tokens": sum(int(item["usage"]["prompt_tokens"]) for item in attempts),
        "cached_tokens": sum(int(item["usage"]["cached_tokens"]) for item in attempts),
        "completion_tokens": sum(int(item["usage"]["completion_tokens"]) for item in attempts),
        "reasoning_tokens": sum(int(item["usage"]["reasoning_tokens"]) for item in attempts),
        "cache_hits": sum(1 for item in attempts if item["from_cache"]),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _llm_attempt_payload(
    result: LLMResult,
    validation: ValidationReport,
    attempt_index: int,
) -> dict[str, Any]:
    return {
        "attempt": attempt_index,
        "model": result.model,
        "system_fingerprint": result.system_fingerprint,
        "from_cache": result.from_cache,
        "cache_key": result.cache_key,
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "cached_tokens": result.usage.cached_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "reasoning_tokens": result.usage.reasoning_tokens,
        },
        "json_error": result.json_error,
        "validation": validation.to_dict(),
        "raw_text": result.text,
    }


def _call_summary(call_payload: dict[str, Any]) -> dict[str, Any]:
    live_attempts = [
        attempt for attempt in call_payload["attempts"] if not attempt.get("from_cache")
    ]
    return {
        "mode": call_payload["mode"],
        "window_id": call_payload["window_id"],
        "ok": bool(call_payload["validation"]["ok"]),
        "attempts": len(call_payload["attempts"]),
        "cost_usd": call_payload["cost_usd"],
        "prompt_tokens": call_payload["prompt_tokens"],
        "cached_tokens": call_payload["cached_tokens"],
        "completion_tokens": call_payload["completion_tokens"],
        "reasoning_tokens": call_payload["reasoning_tokens"],
        "cache_hits": call_payload["cache_hits"],
        "errors": call_payload["validation"]["errors"],
        "counts": call_payload["validation"].get("counts") or {},
        "incremental_cost_usd": round(
            sum(float(item.get("cost_usd") or 0) for item in live_attempts), 12
        ),
        "incremental_prompt_tokens": sum(int((item.get("usage") or {}).get("prompt_tokens") or 0) for item in live_attempts),
        "incremental_cached_tokens": sum(int((item.get("usage") or {}).get("cached_tokens") or 0) for item in live_attempts),
        "incremental_completion_tokens": sum(int((item.get("usage") or {}).get("completion_tokens") or 0) for item in live_attempts),
        "incremental_reasoning_tokens": sum(int((item.get("usage") or {}).get("reasoning_tokens") or 0) for item in live_attempts),
    }


def _build_validation_retry_messages(
    messages: list[dict[str, str]],
    *,
    prior_output: str,
    validation_errors: list[str],
) -> list[dict[str, str]]:
    return [
        *messages,
        {"role": "assistant", "content": prior_output},
        {
            "role": "user",
            "content": (
                "The previous response failed validation. Return corrected JSON only. "
                "Do not add prose. Return the SAME items you already produced. "
                "Correct ONLY the fields named in the errors. Do NOT drop, merge, or add "
                "any turn, event, or mention. Validation errors:\n"
                + "\n".join(validation_errors[:12])
            ),
        },
    ]


def validate_chapter_brief(
    obj: dict[str, Any],
    *,
    chapter_block_ids: list[str],
) -> ValidationReport:
    valid_blocks = set(chapter_block_ids)
    errors: list[str] = []
    warnings: list[str] = []
    counts = {
        "cast_on_stage": 0,
        "cast_proper_name": 0,
        "cast_descriptor": 0,
        "scenes": 0,
        "cast_dropped": 0,
        "scene_dropped": 0,
        "leak_tokens_dropped": 0,
        "dropped_bad_block": 0,
    }
    _require_top(
        obj,
        ["chapter_id", "cast_on_stage", "setting", "scenes_party_size", "neutral_premise"],
        errors,
    )
    setting = obj.get("setting") if isinstance(obj.get("setting"), dict) else {}
    if not isinstance(setting, dict):
        errors.append("setting must be an object")
        setting = {}
    if setting.get("time_frame_hint") not in BRIEF_TIME_FRAME_HINTS:
        errors.append(f"setting.time_frame_hint invalid: {setting.get('time_frame_hint')}")
    if setting.get("scene_shape") not in BRIEF_SCENE_SHAPES:
        errors.append(f"setting.scene_shape invalid: {setting.get('scene_shape')}")

    premise = str(obj.get("neutral_premise") or "")
    if _has_brief_leak_token(premise):
        counts["leak_tokens_dropped"] += 1
        warnings.append("neutral_premise contained a relationship verdict token; blank it before injection")

    for idx, cast in enumerate(_as_list(obj.get("cast_on_stage"), "cast_on_stage", errors)):
        if not isinstance(cast, dict):
            counts["cast_dropped"] += 1
            warnings.append(f"cast_on_stage[{idx}] dropped because it is not an object")
            continue
        surface = str(cast.get("surface") or "").strip()
        block = str(cast.get("first_seen_block") or "").strip()
        role = str(cast.get("role_hint") or "").strip()
        surface_kind = str(cast.get("surface_kind") or "").strip()
        if not surface or not block:
            counts["cast_dropped"] += 1
            warnings.append(f"cast_on_stage[{idx}] dropped because surface/first_seen_block is empty")
            continue
        if surface_kind not in BRIEF_SURFACE_KINDS:
            errors.append(
                f"cast_on_stage[{idx}].surface_kind invalid: {cast.get('surface_kind')}"
            )
            continue
        if block not in valid_blocks:
            counts["cast_dropped"] += 1
            counts["dropped_bad_block"] += 1
            warnings.append(f"cast_on_stage[{idx}] dropped because first_seen_block is outside chapter: {block}")
            continue
        if _has_brief_leak_token(role):
            counts["cast_dropped"] += 1
            counts["leak_tokens_dropped"] += 1
            warnings.append(f"cast_on_stage[{idx}] dropped because role_hint leaks relationship verdict")
            continue
        counts["cast_on_stage"] += 1
        if surface_kind == "proper_name":
            counts["cast_proper_name"] += 1
        else:
            counts["cast_descriptor"] += 1

    for idx, scene in enumerate(_as_list(obj.get("scenes_party_size"), "scenes_party_size", errors)):
        if not isinstance(scene, dict):
            counts["scene_dropped"] += 1
            warnings.append(f"scenes_party_size[{idx}] dropped because it is not an object")
            continue
        block_range = scene.get("block_range")
        if not isinstance(block_range, list) or len(block_range) != 2:
            counts["scene_dropped"] += 1
            warnings.append(f"scenes_party_size[{idx}] dropped because block_range is not [start,end]")
            continue
        bad_blocks = [str(block) for block in block_range if str(block) not in valid_blocks]
        if bad_blocks:
            counts["scene_dropped"] += 1
            counts["dropped_bad_block"] += len(bad_blocks)
            warnings.append(f"scenes_party_size[{idx}] dropped because block_range outside chapter: {bad_blocks}")
            continue
        if not isinstance(scene.get("co_present_count"), int):
            counts["scene_dropped"] += 1
            warnings.append(f"scenes_party_size[{idx}] dropped because co_present_count is not int")
            continue
        counts["scenes"] += 1

    return _report("chapter_brief", errors, warnings, counts)


def _has_brief_leak_token(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(re.search(rf"\b{re.escape(token)}\b", lowered) for token in BRIEF_LEAK_TOKENS)


def validate_lexicon(
    obj: dict[str, Any],
    *,
    valid_block_ids: set[str],
    chapter_block_ids: set[str] | None = None,
    known_entity_ids: set[str] | None = None,
) -> ValidationReport:
    known = known_entity_ids or set()
    errors: list[str] = []
    warnings: list[str] = []
    counts = {
        "glossary": 0,
        "mentions": 0,
        "named": 0,
        "candidate": 0,
        "unknown": 0,
        "dropped_bad_block": 0,
        "pronoun_dropped": 0,
        "mention_named_ids_cleared": 0,
        "outside_window_neighbor_dropped": 0,
        "outside_window_nonexistent_dropped": 0,
        "context_only_used_true": 1 if obj.get("context_only_used") else 0,
    }
    _require_top(obj, ["chapter_id", "window_block_ids", "context_only_used", "glossary_candidates", "character_mentions"], errors)
    kept_terms: list[Any] = []
    for idx, term in enumerate(_as_list(obj.get("glossary_candidates"), "glossary_candidates", errors)):
        _require_item(term, ["source_term", "proposed_target_vi", "category", "do_not_translate", "termhood", "block_ids"], f"glossary_candidates[{idx}]", errors)
        term_blocks = [str(block_id) for block_id in term.get("block_ids") or []]
        drop_kind, bad_blocks = _outside_window_drop_kind(
            term_blocks,
            valid_block_ids=valid_block_ids,
            chapter_block_ids=chapter_block_ids,
        )
        if drop_kind is not None:
            counts[f"outside_window_{drop_kind}_dropped"] += 1
            counts["dropped_bad_block"] += len(bad_blocks)
            term_blocks = [block_id for block_id in term_blocks if block_id in valid_block_ids]
            if term_blocks:
                term["block_ids"] = term_blocks
                warnings.append(
                    f"glossary_candidates[{idx}].block_ids filtered because some "
                    f"were outside active window ({drop_kind}): {bad_blocks}"
                )
            else:
                warnings.append(
                    f"glossary_candidates[{idx}] dropped because block_ids are "
                    f"outside active window ({drop_kind}): {bad_blocks}"
                )
                continue
        kept_terms.append(term)
        counts["glossary"] += 1
        if term.get("category") not in GLOSSARY_CATEGORIES:
            errors.append(f"glossary_candidates[{idx}].category invalid: {term.get('category')}")
        bad_blocks = _bad_blocks(term_blocks, valid_block_ids)
        if bad_blocks:
            counts["dropped_bad_block"] += len(bad_blocks)
            errors.append(f"glossary_candidates[{idx}].block_ids outside window: {bad_blocks}")
    if isinstance(obj.get("glossary_candidates"), list):
        obj["glossary_candidates"] = kept_terms
    mention_surfaces: set[str] = set()
    kept_mentions: list[Any] = []
    for idx, mention in enumerate(_as_list(obj.get("character_mentions"), "character_mentions", errors)):
        surface = str(mention.get("surface") or "").strip()
        if surface.casefold() in PLAIN_PRONOUNS:
            counts["pronoun_dropped"] += 1
            warnings.append(f"character_mentions[{idx}] dropped because surface is plain pronoun: {surface}")
            continue
        _require_item(mention, ["mention_id", "surface", "mention_type", "resolution_status", "candidate_entity_ids", "block_ids"], f"character_mentions[{idx}]", errors)
        mention_blocks = [str(block_id) for block_id in mention.get("block_ids") or []]
        drop_kind, bad_blocks = _outside_window_drop_kind(
            mention_blocks,
            valid_block_ids=valid_block_ids,
            chapter_block_ids=chapter_block_ids,
        )
        if drop_kind is not None:
            counts[f"outside_window_{drop_kind}_dropped"] += 1
            counts["dropped_bad_block"] += len(bad_blocks)
            mention_blocks = [block_id for block_id in mention_blocks if block_id in valid_block_ids]
            if mention_blocks:
                mention["block_ids"] = mention_blocks
                warnings.append(
                    f"character_mentions[{idx}].block_ids filtered because some "
                    f"were outside active window ({drop_kind}): {bad_blocks}"
                )
            else:
                warnings.append(
                    f"character_mentions[{idx}] dropped because block_ids are "
                    f"outside active window ({drop_kind}): {bad_blocks}"
                )
                continue
        counts["mentions"] += 1
        kept_mentions.append(mention)
        mention_surfaces.add(surface.casefold())
        if mention.get("mention_type") not in MENTION_TYPES:
            errors.append(f"character_mentions[{idx}].mention_type invalid: {mention.get('mention_type')}")
        status = str(mention.get("resolution_status") or "")
        if status not in RESOLUTION_STATUSES:
            errors.append(f"character_mentions[{idx}].resolution_status invalid: {status}")
        else:
            counts[status] += 1
        candidate_ids = mention.get("candidate_entity_ids")
        if status == "named" and isinstance(candidate_ids, list) and candidate_ids:
            unknown_ids = [
                str(item)
                for item in candidate_ids
                if known and str(item) not in known
            ]
            if unknown_ids:
                errors.append(
                    f"character_mentions[{idx}].candidate_entity_ids unknown: {unknown_ids}"
                )
            mention["candidate_entity_ids"] = []
            counts["mention_named_ids_cleared"] += 1
            warnings.append(
                f"character_mentions[{idx}].candidate_entity_ids cleared "
                "because named surface is authoritative"
            )
        _validate_candidate_ids(
            path=f"character_mentions[{idx}]",
            status=status,
            candidate_ids=mention.get("candidate_entity_ids"),
            known_entity_ids=known,
            errors=errors,
        )
        if "canonical_entity_id" in mention:
            errors.append(f"character_mentions[{idx}] must not include canonical_entity_id")
        bad_blocks = _bad_blocks(mention_blocks, valid_block_ids)
        if bad_blocks:
            counts["dropped_bad_block"] += len(bad_blocks)
            errors.append(f"character_mentions[{idx}].block_ids outside window: {bad_blocks}")
    if isinstance(obj.get("character_mentions"), list):
        obj["character_mentions"] = kept_mentions
    for idx, term in enumerate(_as_list(obj.get("glossary_candidates"), "glossary_candidates", errors)):
        source = str(term.get("source_term") or "").strip().casefold()
        if source and source in mention_surfaces:
            warnings.append(f"glossary_candidates[{idx}] overlaps character mention: {term.get('source_term')}")
    return _report("lexicon", errors, warnings, counts)


def validate_narrative(
    obj: dict[str, Any],
    *,
    valid_block_ids: set[str],
    chapter_block_ids: set[str] | None = None,
    known_entity_ids: set[str] | None = None,
) -> ValidationReport:
    known = known_entity_ids or set()
    errors: list[str] = []
    warnings: list[str] = []
    counts = {
        "turns": 0,
        "events": 0,
        "phase_leak": 0,
        "address_term_present": 0,
        "context_only_used_true": 1 if obj.get("context_only_used") else 0,
        "unknown_with_evidence": 0,
        "unknown_empty": 0,
        "nonperson_event_dropped": 0,
        "attribution_enum_dropped": 0,
        "attribution_enum_normalized": 0,
        "named_pronoun_downgraded": 0,
        "named_ids_cleared": 0,
        "outside_window_neighbor_dropped": 0,
        "outside_window_nonexistent_dropped": 0,
    }
    _require_top(obj, ["chapter_id", "window_block_ids", "context_only_used", "speaker_turns", "relation_events"], errors)
    kept_turns: list[Any] = []
    for idx, turn in enumerate(_as_list(obj.get("speaker_turns"), "speaker_turns", errors)):
        _require_item(turn, ["turn_id", "speaker", "addressee", "utterance_quote", "address_term_used", "register_cue", "block_id"], f"speaker_turns[{idx}]", errors)
        block_id = str(turn.get("block_id") or "")
        if not block_id:
            if "block_id" in turn:
                errors.append(f"speaker_turns[{idx}].block_id is required")
        elif block_id not in valid_block_ids:
            drop_kind, bad_blocks = _outside_window_drop_kind(
                [block_id],
                valid_block_ids=valid_block_ids,
                chapter_block_ids=chapter_block_ids,
            )
            if drop_kind is not None:
                counts[f"outside_window_{drop_kind}_dropped"] += 1
                warnings.append(
                    f"speaker_turns[{idx}] dropped because block_id is "
                    f"outside active window ({drop_kind}): {bad_blocks}"
                )
                continue
            errors.append(f"speaker_turns[{idx}].block_id outside window: {block_id}")
        counts["turns"] += 1
        if str(turn.get("address_term_used") or "").strip():
            counts["address_term_present"] += 1
        for role in ["speaker", "addressee"]:
            _validate_reference(
                turn.get(role),
                path=f"speaker_turns[{idx}].{role}",
                known_entity_ids=known,
                errors=errors,
                warnings=warnings,
                counts=counts,
            )
        kept_turns.append(turn)
    if isinstance(obj.get("speaker_turns"), list):
        obj["speaker_turns"] = kept_turns
    kept_events: list[Any] = []
    for idx, event in enumerate(_as_list(obj.get("relation_events"), "relation_events", errors)):
        event_errors: list[str] = []
        _require_item(event, ["event_id", "actor", "target", "event_type", "evidence_quote", "block_id"], f"relation_events[{idx}]", errors)
        block_id = str(event.get("block_id") or "")
        if not block_id:
            if "block_id" in event:
                event_errors.append(f"relation_events[{idx}].block_id is required")
        elif block_id not in valid_block_ids:
            drop_kind, bad_blocks = _outside_window_drop_kind(
                [block_id],
                valid_block_ids=valid_block_ids,
                chapter_block_ids=chapter_block_ids,
            )
            if drop_kind is not None:
                counts[f"outside_window_{drop_kind}_dropped"] += 1
                warnings.append(
                    f"relation_events[{idx}] dropped because block_id is "
                    f"outside active window ({drop_kind}): {bad_blocks}"
                )
                continue
            event_errors.append(f"relation_events[{idx}].block_id outside window: {block_id}")
        event_type = str(event.get("event_type") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", event_type):
            event_errors.append(f"relation_events[{idx}].event_type must be lower_snake_case: {event_type}")
        if event_type in PHASE_LEAK_EVENT_TYPES or "phase" in event_type:
            counts["phase_leak"] += 1
            event_errors.append(f"relation_events[{idx}].event_type leaks phase/relation label: {event_type}")
        nonperson = False
        for role in ["actor", "target"]:
            ref = event.get(role)
            _validate_reference(
                ref,
                path=f"relation_events[{idx}].{role}",
                known_entity_ids=known,
                errors=event_errors,
                warnings=warnings,
                counts=counts,
            )
            if isinstance(ref, dict) and ref.get("reference_kind") not in {"person", "narrator"}:
                nonperson = True
                warnings.append(
                    f"relation_events[{idx}] dropped because {role}.reference_kind is "
                    f"{ref.get('reference_kind')}"
                )
        if nonperson:
            counts["nonperson_event_dropped"] += 1
            continue
        counts["events"] += 1
        kept_events.append(event)
        errors.extend(event_errors)
    if isinstance(obj.get("relation_events"), list):
        obj["relation_events"] = kept_events
    return _report("narrative", errors, warnings, counts)


def validate_digest(obj: dict[str, Any], *, chapter_block_ids: list[str]) -> ValidationReport:
    valid = set(chapter_block_ids)
    errors: list[str] = []
    warnings: list[str] = []
    counts = {
        "frame_segments": 0,
        "scenes": 0,
        "state_changes": 0,
        "unresolved_mystery": 0,
        "unresolved_pending_transition": 0,
        "unresolved_question": 0,
        "translator_facts": 0,
        "motifs": 0,
    }
    _require_top(
        obj,
        [
            "chapter_id",
            "chapter_rolling_summary",
            "narration_frame_segments",
            "scene_summaries",
            "character_state_changes",
            "relation_event_summary",
            "unresolved_threads",
            "motifs",
            "translator_relevant_facts",
        ],
        errors,
    )
    segments = _as_list(obj.get("narration_frame_segments"), "narration_frame_segments", errors)
    counts["frame_segments"] = len(segments)
    _validate_contiguous_segments(segments, chapter_block_ids, errors)
    for idx, segment in enumerate(segments):
        if segment.get("story_time_label") not in DIGEST_STORY_TIME:
            errors.append(f"narration_frame_segments[{idx}].story_time_label invalid: {segment.get('story_time_label')}")
    scenes = _as_list(obj.get("scene_summaries"), "scene_summaries", errors)
    counts["scenes"] = len(scenes)
    for idx, scene in enumerate(scenes):
        _validate_block_range(scene.get("block_range"), valid, f"scene_summaries[{idx}].block_range", errors)
    for idx, change in enumerate(_as_list(obj.get("character_state_changes"), "character_state_changes", errors)):
        counts["state_changes"] += 1
        if change.get("attribute") not in DIGEST_CHANGE_ATTRIBUTES:
            errors.append(f"character_state_changes[{idx}].attribute invalid: {change.get('attribute')}")
        if change.get("observed_scope") not in DIGEST_OBSERVED_SCOPE:
            errors.append(f"character_state_changes[{idx}].observed_scope invalid: {change.get('observed_scope')}")
        if change.get("trigger_block") not in valid:
            errors.append(f"character_state_changes[{idx}].trigger_block outside chapter: {change.get('trigger_block')}")
    for idx, relation in enumerate(_as_list(obj.get("relation_event_summary"), "relation_event_summary", errors)):
        for forbidden in ["valid_from_block", "valid_to_block", "phase_label"]:
            if forbidden in relation:
                errors.append(f"relation_event_summary[{idx}] must not finalize {forbidden}")
        if relation.get("status") != "evidence_only":
            errors.append(f"relation_event_summary[{idx}].status must be evidence_only")
        if relation.get("observed_valence_hint") not in DIGEST_VALENCE_HINTS:
            errors.append(f"relation_event_summary[{idx}].observed_valence_hint invalid: {relation.get('observed_valence_hint')}")
    for idx, thread in enumerate(_as_list(obj.get("unresolved_threads"), "unresolved_threads", errors)):
        kind = str(thread.get("kind") or "")
        if kind not in DIGEST_THREAD_KINDS:
            errors.append(f"unresolved_threads[{idx}].kind invalid: {kind}")
        else:
            counts[f"unresolved_{kind}"] += 1
        if thread.get("opened_block") not in valid:
            errors.append(f"unresolved_threads[{idx}].opened_block outside chapter: {thread.get('opened_block')}")
    motifs = _as_list(obj.get("motifs"), "motifs", errors)
    counts["motifs"] = len(motifs)
    for idx, motif in enumerate(motifs):
        bad = _bad_blocks(motif.get("block_ids"), valid)
        if bad:
            errors.append(f"motifs[{idx}].block_ids outside chapter: {bad}")
    facts = _as_list(obj.get("translator_relevant_facts"), "translator_relevant_facts", errors)
    counts["translator_facts"] = len(facts)
    if len(facts) > 8:
        errors.append("translator_relevant_facts exceeds max 8")
    for idx, fact in enumerate(facts):
        if fact.get("fact_type") not in DIGEST_FACT_TYPES:
            errors.append(f"translator_relevant_facts[{idx}].fact_type invalid: {fact.get('fact_type')}")
        bad = _bad_blocks(fact.get("block_evidence"), valid)
        if bad:
            errors.append(f"translator_relevant_facts[{idx}].block_evidence outside chapter: {bad}")
    return _report("digest", errors, warnings, counts)


def validate_story_bible(obj: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    counts = {
        "entities": 0,
        "aliases_with_valid_range": 0,
        "phases": 0,
        "open_intervals": 0,
        "entity_state_intervals": 0,
        "address_policies_proposed": 0,
        "address_dirs_unsupported": 0,
    }
    _require_top(
        obj,
        [
            "scope",
            "status",
            "registry_T1_glossary",
            "registry_T2_entities",
            "registry_T3_speaker_turns",
            "registry_T4_chapter_digests",
            "entity_relations",
            "entity_state_intervals",
            "address_policies",
            "narration_frame_segments",
            "unresolved_threads",
        ],
        errors,
    )
    scope = str(obj.get("scope") or "")
    if not scope:
        errors.append("scope is required")
    if str(obj.get("status") or "") not in {"partial_story_bible", "open_within_scope"}:
        errors.append("status must declare partial/open pilot scope")
    entity_ids: set[str] = set()
    for idx, entity in enumerate(_as_list(obj.get("registry_T2_entities"), "registry_T2_entities", errors)):
        counts["entities"] += 1
        entity_id = str(entity.get("entity_id") or "")
        if not entity_id:
            errors.append(f"registry_T2_entities[{idx}].entity_id missing")
        entity_ids.add(entity_id)
        if entity.get("entity_type") in GROUP_REFERENCE_KINDS:
            errors.append(f"registry_T2_entities[{idx}] must not mint group/narrator/reader as person entity")
        for alias_idx, alias in enumerate(_as_list(entity.get("aliases"), f"registry_T2_entities[{idx}].aliases", errors)):
            if "valid_from_block" not in alias or "valid_to_block" not in alias:
                errors.append(f"registry_T2_entities[{idx}].aliases[{alias_idx}] missing valid_range")
            else:
                counts["aliases_with_valid_range"] += 1
            if alias.get("valid_to_block") is None:
                status = str(alias.get("status") or "")
                if status != "open_within_scope":
                    errors.append(f"registry_T2_entities[{idx}].aliases[{alias_idx}] open alias must use status open_within_scope")
    for idx, relation in enumerate(_as_list(obj.get("entity_relations"), "entity_relations", errors)):
        counts["phases"] += 1
        _validate_pair(relation.get("pair"), entity_ids, f"entity_relations[{idx}].pair", errors)
        if relation.get("phase_label") not in PHASE_LABELS:
            errors.append(f"entity_relations[{idx}].phase_label invalid: {relation.get('phase_label')}")
        if relation.get("valid_to_block") is None:
            counts["open_intervals"] += 1
            status = str(relation.get("status") or "")
            if status != "open_within_scope":
                errors.append(f"entity_relations[{idx}] open interval must use status open_within_scope in pilot")
    for idx, interval in enumerate(_as_list(obj.get("entity_state_intervals"), "entity_state_intervals", errors)):
        counts["entity_state_intervals"] += 1
        if interval.get("entity_id") not in entity_ids:
            errors.append(f"entity_state_intervals[{idx}].entity_id unknown: {interval.get('entity_id')}")
        if interval.get("attribute") not in DIGEST_CHANGE_ATTRIBUTES:
            errors.append(f"entity_state_intervals[{idx}].attribute invalid: {interval.get('attribute')}")
        if interval.get("valid_to_block") is None and interval.get("status") != "open_within_scope":
            errors.append(f"entity_state_intervals[{idx}] open interval must use status open_within_scope")
    for idx, policy in enumerate(_as_list(obj.get("address_policies"), "address_policies", errors)):
        counts["address_policies_proposed"] += 1
        _validate_pair(policy.get("pair"), entity_ids, f"address_policies[{idx}].pair", errors)
        for direction in ["a_to_b", "b_to_a"]:
            value = policy.get(direction)
            if not isinstance(value, dict):
                errors.append(f"address_policies[{idx}].{direction} must be object")
                continue
            if value.get("evidence_level") not in ADDRESS_EVIDENCE_LEVELS:
                errors.append(f"address_policies[{idx}].{direction}.evidence_level invalid: {value.get('evidence_level')}")
            if value.get("evidence_level") == "unsupported":
                counts["address_dirs_unsupported"] += 1
            if "needs_human_review" not in value:
                errors.append(f"address_policies[{idx}].{direction}.needs_human_review missing")
    return _report("story_bible", errors, warnings, counts)


def _opf_path(archive: zipfile.ZipFile) -> str:
    root = ET.fromstring(archive.read("META-INF/container.xml"))
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = root.find(".//c:rootfile", ns)
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB container missing OPF rootfile")
    return str(rootfile.get("full-path"))


def _toc_chapter_items(toc_xml: str, toc_path: str) -> list[dict[str, str]]:
    root = ET.fromstring(toc_xml)
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    items: list[dict[str, str]] = []
    for anchor in root.findall(".//x:a", ns):
        label = _collapse_ws("".join(anchor.itertext()))
        href = str(anchor.get("href") or "")
        if re.fullmatch(r"(?:CHAPTER\s+)?[IVXLCDM]+", label):
            items.append({"label": label, "href": href})
    if not items:
        raise ValueError(f"No chapter entries found in {toc_path}")
    return items


def _split_href(href: str) -> tuple[str, str | None]:
    href_file, sep, fragment = str(href).partition("#")
    return href_file, fragment if sep and fragment else None


def _slice_xhtml_fragment(
    xhtml: str,
    *,
    start_fragment: str | None,
    stop_fragment: str | None,
    source_href: str,
) -> str:
    if not start_fragment:
        return xhtml
    start = _find_id_start(xhtml, start_fragment)
    if start < 0:
        raise ValueError(f"Fragment #{start_fragment} not found in {source_href}")
    stop = len(xhtml)
    if stop_fragment:
        candidate_stop = _find_id_start(xhtml, stop_fragment)
        if candidate_stop > start:
            stop = candidate_stop
    return xhtml[start:stop]


def _find_id_start(xhtml: str, fragment: str) -> int:
    match = re.search(
        rf"<[^>]+\bid=[\"']{re.escape(fragment)}[\"'][^>]*>",
        xhtml,
        flags=re.IGNORECASE,
    )
    return match.start() if match else -1


def _blocks_from_xhtml(xhtml: str, *, chapter_id: str, order_start: int) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xhtml)
    except ET.ParseError:
        return _blocks_from_xhtml_regex(xhtml, chapter_id=chapter_id, order_start=order_start)
    blocks: list[dict[str, Any]] = []
    local_order = 0
    for element in root.iter():
        tag = _local_name(element.tag)
        if tag not in {"h2", "p"}:
            continue
        text = _collapse_ws("".join(element.itertext()))
        if not text:
            continue
        local_order += 1
        block_id = f"{chapter_id}_b{local_order:03d}"
        block_type = "heading" if tag == "h2" else ("dialogue" if text.startswith(("“", '"')) else "paragraph")
        blocks.append(
            {
                "block_id": block_id,
                "order_index": order_start + local_order - 1,
                "block_type": block_type,
                "clean_text": text,
                "source_text": text,
                "annotations": {},
                "is_chapter_opening": local_order == 1,
                "page_ids": [],
                "quality_flags": [],
            }
        )
    if not blocks:
        raise ValueError(f"No text blocks extracted for {chapter_id}")
    return blocks


def _blocks_from_xhtml_regex(xhtml: str, *, chapter_id: str, order_start: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    local_order = 0
    for match in re.finditer(r"<(h[1-3]|p)\b[^>]*>(.*?)</\1>", xhtml, flags=re.IGNORECASE | re.DOTALL):
        tag = match.group(1).lower()
        text = _collapse_ws(re.sub(r"<[^>]+>", " ", match.group(2)))
        if not text:
            continue
        local_order += 1
        block_id = f"{chapter_id}_b{local_order:03d}"
        block_type = "heading" if tag.startswith("h") else ("dialogue" if text.startswith(("â€œ", "“", '"')) else "paragraph")
        blocks.append(
            {
                "block_id": block_id,
                "order_index": order_start + local_order - 1,
                "block_type": block_type,
                "clean_text": text,
                "source_text": text,
                "annotations": {},
                "is_chapter_opening": local_order == 1,
                "page_ids": [],
                "quality_flags": [],
            }
        )
    if not blocks:
        raise ValueError(f"No text blocks extracted for {chapter_id}")
    return blocks


def _first_window(chapters: list[dict[str, Any]]) -> LiteraryWindow:
    if not chapters:
        raise ValueError("No chapters selected")
    windows = build_literary_windows(chapters[0])
    if not windows:
        raise ValueError("No windows built")
    return windows[0]


def _normalize_chapter_arg(value: str, *, document: dict[str, Any] | None = None) -> str:
    raw = str(value).strip()
    prefix = "wh_ch"
    if document is not None:
        prefix = str((document.get("metadata") or {}).get("chapter_prefix") or prefix)
    if raw.isdigit():
        return f"{prefix}{int(raw):02d}"
    if re.fullmatch(r"ch\d+", raw):
        return f"{prefix}{int(raw[2:]):02d}"
    return raw


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _report(name: str, errors: list[str], warnings: list[str], counts: dict[str, int]) -> ValidationReport:
    return ValidationReport(name=name, ok=not errors, errors=errors, warnings=warnings, counts=counts)


def _require_top(obj: dict[str, Any], required: list[str], errors: list[str]) -> None:
    for field in required:
        if field not in obj:
            errors.append(f"missing required field: {field}")


def _require_item(item: Any, required: list[str], path: str, errors: list[str]) -> None:
    if not isinstance(item, dict):
        errors.append(f"{path} must be object")
        return
    for field in required:
        if field not in item:
            errors.append(f"{path}.{field} is required")


def _as_list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    errors.append(f"{path} must be list")
    return []


def _bad_blocks(values: Any, valid_block_ids: set[str]) -> list[str]:
    return [str(value) for value in values or [] if str(value) not in valid_block_ids]


def _outside_window_drop_kind(
    block_ids: list[str],
    *,
    valid_block_ids: set[str],
    chapter_block_ids: set[str] | None,
) -> tuple[str | None, list[str]]:
    bad_blocks = [block_id for block_id in block_ids if block_id not in valid_block_ids]
    if not bad_blocks or chapter_block_ids is None:
        return None, bad_blocks
    if any(block_id not in chapter_block_ids for block_id in bad_blocks):
        return "nonexistent", bad_blocks
    return "neighbor", bad_blocks


def _validate_candidate_ids(
    *,
    path: str,
    status: str,
    candidate_ids: Any,
    known_entity_ids: set[str],
    errors: list[str],
) -> None:
    ids = candidate_ids if isinstance(candidate_ids, list) else []
    if not isinstance(candidate_ids, list):
        errors.append(f"{path}.candidate_entity_ids must be list")
        return
    if status == "candidate":
        if not ids:
            errors.append(f"{path}.candidate_entity_ids required when candidate")
    elif ids:
        errors.append(f"{path}.candidate_entity_ids must be empty when {status}")
    unknown = [str(item) for item in ids if known_entity_ids and str(item) not in known_entity_ids]
    if unknown:
        errors.append(f"{path}.candidate_entity_ids unknown: {unknown}")


def _validate_reference(
    value: Any,
    *,
    path: str,
    known_entity_ids: set[str],
    errors: list[str],
    warnings: list[str],
    counts: dict[str, int],
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be object")
        return
    _require_item(
        value,
        ["surface", "reference_kind", "resolution_status", "candidate_entity_ids", "attribution_method", "confidence"],
        path,
        errors,
    )
    if value.get("reference_kind") not in REFERENCE_KINDS:
        errors.append(f"{path}.reference_kind invalid: {value.get('reference_kind')}")
    _normalize_named_reference_candidate_ids(
        value,
        path=path,
        known_entity_ids=known_entity_ids,
        errors=errors,
        warnings=warnings,
        counts=counts,
    )
    _normalize_attribution_method(
        value,
        path=path,
        warnings=warnings,
        counts=counts,
    )
    status = str(value.get("resolution_status") or "")
    if status not in RESOLUTION_STATUSES:
        errors.append(f"{path}.resolution_status invalid: {status}")
    _validate_candidate_ids(
        path=path,
        status=status,
        candidate_ids=value.get("candidate_entity_ids"),
        known_entity_ids=known_entity_ids,
        errors=errors,
    )
    if value.get("attribution_method") not in ATTRIBUTION_METHODS:
        errors.append(f"{path}.attribution_method invalid: {value.get('attribution_method')}")
    if value.get("confidence") not in CONFIDENCE_LEVELS:
        errors.append(f"{path}.confidence invalid: {value.get('confidence')}")
    if status == "unknown":
        if str(value.get("surface") or "").strip():
            counts["unknown_with_evidence"] = counts.get("unknown_with_evidence", 0) + 1
        else:
            counts["unknown_empty"] = counts.get("unknown_empty", 0) + 1


def _normalize_attribution_method(
    value: dict[str, Any],
    *,
    path: str,
    warnings: list[str],
    counts: dict[str, int],
) -> None:
    method = str(value.get("attribution_method") or "")
    if method not in RESOLUTION_STATUSES:
        return
    value["attribution_method"] = "unspecified"
    counts["attribution_enum_normalized"] = counts.get("attribution_enum_normalized", 0) + 1
    warnings.append(f"{path}.attribution_method normalized from {method} to unspecified")


def _normalize_named_reference_candidate_ids(
    value: dict[str, Any],
    *,
    path: str,
    known_entity_ids: set[str],
    errors: list[str],
    warnings: list[str],
    counts: dict[str, int],
) -> None:
    candidate_ids = value.get("candidate_entity_ids")
    if (
        str(value.get("resolution_status") or "") != "named"
        or not isinstance(candidate_ids, list)
        or not candidate_ids
    ):
        return

    unknown_ids = [
        str(item)
        for item in candidate_ids
        if known_entity_ids and str(item) not in known_entity_ids
    ]
    if unknown_ids:
        errors.append(f"{path}.candidate_entity_ids unknown: {unknown_ids}")

    surface = str(value.get("surface") or "").strip().casefold()
    if surface in PLAIN_PRONOUNS:
        value["resolution_status"] = "unknown" if unknown_ids else "candidate"
        value["candidate_entity_ids"] = [] if unknown_ids else candidate_ids
        counts["named_pronoun_downgraded"] = counts.get("named_pronoun_downgraded", 0) + 1
        warnings.append(f"{path} normalized from named pronoun to {value['resolution_status']}")
        return

    value["candidate_entity_ids"] = []
    counts["named_ids_cleared"] = counts.get("named_ids_cleared", 0) + 1
    warnings.append(f"{path}.candidate_entity_ids cleared because named surface is authoritative")


def _validate_contiguous_segments(
    segments: list[Any],
    chapter_block_ids: list[str],
    errors: list[str],
) -> None:
    expected_start = 0
    index_by_block = {block_id: idx for idx, block_id in enumerate(chapter_block_ids)}
    for idx, segment in enumerate(segments):
        if not isinstance(segment, dict):
            errors.append(f"narration_frame_segments[{idx}] must be object")
            continue
        block_range = segment.get("block_range")
        if not isinstance(block_range, list) or len(block_range) != 2:
            errors.append(f"narration_frame_segments[{idx}].block_range must be [start,end]")
            continue
        start, end = map(str, block_range)
        if start not in index_by_block or end not in index_by_block:
            errors.append(f"narration_frame_segments[{idx}].block_range outside chapter: {block_range}")
            continue
        start_i = index_by_block[start]
        end_i = index_by_block[end]
        if start_i != expected_start:
            errors.append(f"narration_frame_segments[{idx}] starts at {start}, expected {chapter_block_ids[expected_start]}")
        if end_i < start_i:
            errors.append(f"narration_frame_segments[{idx}] has reversed block_range")
        expected_start = end_i + 1
    if segments and expected_start != len(chapter_block_ids):
        errors.append("narration_frame_segments do not cover full chapter")


def _validate_block_range(value: Any, valid: set[str], path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or len(value) != 2:
        errors.append(f"{path} must be [start,end]")
        return
    bad = _bad_blocks(value, valid)
    if bad:
        errors.append(f"{path} outside chapter: {bad}")


def _validate_pair(value: Any, entity_ids: set[str], path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or len(value) != 2:
        errors.append(f"{path} must be two entity ids")
        return
    unknown = [str(item) for item in value if str(item) not in entity_ids]
    if unknown:
        errors.append(f"{path} references unknown entity ids: {unknown}")
