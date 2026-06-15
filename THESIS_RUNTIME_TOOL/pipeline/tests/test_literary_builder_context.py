from __future__ import annotations

import json
import sqlite3

from pipeline.prepass.literary_context import build_literary_builder_context_pack
from pipeline.prepass.prompt import LITERARY_PROMPT_VERSION, build_messages
from pipeline.prepass.registry import PrepassRegistry
from pipeline.scripts import render_literary_prompts as render_prompts


def _registry() -> PrepassRegistry:
    registry = PrepassRegistry()
    registry.merge(
        {
            "chapter_id": "treasure_island_ch02",
            "glossary_candidates": [
                {
                    "source_term": "Admiral Benbow Inn",
                    "proposed_target_vi": "quán trọ Admiral Benbow",
                    "do_not_translate": False,
                    "category": "place",
                    "block_ids": ["ch02_b001"],
                },
                {
                    "source_term": "assizes",
                    "proposed_target_vi": "phiên tòa đại hình",
                    "do_not_translate": False,
                    "category": "cultural",
                    "block_ids": ["ch02_b020"],
                },
            ],
            "entities": [
                {
                    "entity_id": "ent_narrator",
                    "canonical_source": "the narrator",
                    "aliases_source": ["Jim"],
                    "entity_type": "person",
                    "proposed_target_vi": "người kể chuyện",
                    "aliases_target_vi": ["Jim"],
                },
                {
                    "entity_id": "ent_captain",
                    "canonical_source": "the captain",
                    "aliases_source": ["the old seaman", "captain"],
                    "entity_type": "person",
                    "proposed_target_vi": "thuyền trưởng",
                    "aliases_target_vi": ["ông thuyền trưởng"],
                },
                {
                    "entity_id": "ent_black_dog",
                    "canonical_source": "Black Dog",
                    "aliases_source": ["Black Dog"],
                    "entity_type": "person",
                    "proposed_target_vi": "Black Dog",
                    "aliases_target_vi": ["Black Dog"],
                },
            ],
            "relations": [
                {
                    "a": "ent_narrator",
                    "b": "ent_captain",
                    "relation": "lodger/inn-boy",
                    "state_label": "wary_curiosity",
                    "address_a_to_b_vi": "ông",
                    "address_b_to_a_vi": "cậu",
                },
                {
                    "a": "ent_black_dog",
                    "b": "ent_captain",
                    "relation": "old associates",
                    "state_label": "tense_reunion",
                    "address_a_to_b_vi": "ông",
                    "address_b_to_a_vi": "ông",
                },
            ],
        }
    )
    return registry


def _chapter(text: str) -> dict:
    return {
        "chapter_id": "treasure_island_ch03",
        "blocks": [
            {
                "block_id": "treasure_island_ch03_b001",
                "clean_text": text,
                "source_text": text,
            }
        ],
    }


def test_literary_context_pack_filters_to_current_text():
    pack = build_literary_builder_context_pack(
        _chapter("I saw the captain at the Admiral Benbow Inn."),
        _registry(),
        budget_tokens=600,
        recent_carryover_limit=0,
    )
    rendered = pack.render_context()
    audit = pack.to_dict()

    assert "REGISTRY_CONTEXT_POLICY" in rendered
    assert "Admiral Benbow Inn -> quán trọ Admiral Benbow" in rendered
    assert "ent_narrator | the narrator -> người kể chuyện" in rendered
    assert "ent_captain | the captain -> thuyền trưởng" in rendered
    assert "ent_narrator<->ent_captain [lodger/inn-boy]" in rendered
    assert "assizes -> phiên tòa đại hình" not in rendered
    assert "ent_black_dog | Black Dog" not in rendered
    assert any(item["item_id"] == "assizes" for item in audit["excluded"])
    assert any(item["item_id"] == "ent_black_dog" for item in audit["excluded"])


def test_literary_context_pack_logs_dropped_by_budget():
    pack = build_literary_builder_context_pack(
        _chapter("I saw the captain at the Admiral Benbow Inn."),
        _registry(),
        budget_tokens=5,
        recent_carryover_limit=0,
    )

    assert pack.token_estimate <= 5
    assert pack.dropped_by_budget
    assert all("budget:" in item["reason"] for item in pack.dropped_by_budget)


def test_literary_context_pack_keeps_narrator_on_first_person():
    pack = build_literary_builder_context_pack(
        _chapter("I waited by the door. No named narrator appears here."),
        _registry(),
        budget_tokens=600,
        recent_carryover_limit=0,
    )

    rendered = pack.render_context()

    assert "NARRATOR CARD" in rendered
    assert "ent_narrator | the narrator -> người kể chuyện" in rendered


def test_literary_context_carryover_does_not_activate_relation():
    registry = _registry()
    registry.relations[tuple(sorted(("ent_black_dog", "ent_narrator")))] = {
        "a": "ent_black_dog",
        "b": "ent_narrator",
        "state_label": "not_visible",
        "address_a_to_b_vi": "cậu",
        "address_b_to_a_vi": "ông",
    }

    pack = build_literary_builder_context_pack(
        _chapter("I waited by the door."),
        registry,
        budget_tokens=600,
        recent_carryover_limit=1,
    )
    rendered = pack.render_context()

    assert "RECENT CARRYOVER" in rendered
    assert "not_visible" not in rendered


def test_literary_builder_prompt_no_full_registry_dump_and_d2l_still_omits():
    registry = _registry()
    chapter = _chapter("The captain entered the Admiral Benbow Inn.")
    pack = build_literary_builder_context_pack(
        chapter,
        registry,
        budget_tokens=600,
        recent_carryover_limit=0,
    )
    literary_messages = build_messages(chapter, pack.render_context(), mode="literary")
    literary_combined = "\n".join(message["content"] for message in literary_messages)

    assert LITERARY_PROMPT_VERSION == "literary_builder_context_v3"
    assert "Prompt version: literary_builder_context_v3" in literary_combined
    assert "Aim for 5-20 glossary terms" not in literary_combined
    assert "REGISTRY_CONTEXT_PACK" in literary_combined
    assert "REGISTRY_SO_FAR" not in literary_combined
    assert "Admiral Benbow Inn -> quán trọ Admiral Benbow" in literary_combined
    assert "assizes -> phiên tòa đại hình" not in literary_combined

    d2l_messages = build_messages(
        {
            "chapter_id": "d2l_demo",
            "blocks": [{"block_id": "d2l_demo_b001", "clean_text": "An agent learns."}],
        },
        "Glossary:\n- leaked -> registry",
        mode="d2l_terminology",
    )
    d2l_combined = "\n".join(message["content"] for message in d2l_messages)

    assert "REGISTRY_POLICY" in d2l_combined
    assert "leaked -> registry" not in d2l_combined


def test_render_chronology_uses_prior_artifacts_only(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE blocks (doc_id TEXT, chapter_id TEXT)")
    conn.executemany(
        "INSERT INTO blocks VALUES (?, ?)",
        [
            ("ti", "treasure_island_ch02"),
            ("ti", "treasure_island_ch03"),
        ],
    )
    prepass_dir = tmp_path / "prepass"
    prepass_dir.mkdir()
    (prepass_dir / "treasure_island_ch02.json").write_text(
        json.dumps(
            {
                "chapter_id": "treasure_island_ch02",
                "glossary_candidates": [
                    {
                        "source_term": "Admiral Benbow Inn",
                        "proposed_target_vi": "Admiral Benbow Inn",
                        "do_not_translate": False,
                        "category": "place",
                        "block_ids": ["treasure_island_ch02_b001"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (prepass_dir / "treasure_island_ch03.json").write_text(
        json.dumps(
            {
                "chapter_id": "treasure_island_ch03",
                "glossary_candidates": [
                    {
                        "source_term": "Black Dog",
                        "proposed_target_vi": "Black Dog",
                        "do_not_translate": False,
                        "category": "other",
                        "block_ids": ["treasure_island_ch03_b001"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry, source = render_prompts._registry_for_builder_sample(
        conn,
        "ti",
        ["ch02", "ch03"],
        prepass_dir,
    )
    first_registry, first_source = render_prompts._registry_for_builder_sample(
        conn,
        "ti",
        ["ch02"],
        prepass_dir,
    )

    assert "prepass_artifacts_prior_chapters" in source
    assert "admiral benbow inn" in registry.glossary
    assert "black dog" not in registry.glossary
    assert first_source == "empty_registry_first_chapter"
    assert not first_registry.glossary
    conn.close()


def test_cache_prefix_identical_and_context_pack_deterministic():
    chapter_a = _chapter("I saw the captain at the Admiral Benbow Inn.")
    chapter_b = _chapter("I waited for the captain.")
    pack_a = build_literary_builder_context_pack(
        chapter_a,
        _registry(),
        budget_tokens=600,
        recent_carryover_limit=0,
    )
    pack_b = build_literary_builder_context_pack(
        chapter_b,
        _registry(),
        budget_tokens=600,
        recent_carryover_limit=0,
    )
    messages_a = build_messages(chapter_a, pack_a.render_context(), mode="literary")
    messages_b = build_messages(chapter_b, pack_b.render_context(), mode="literary")

    assert messages_a[0]["content"] == messages_b[0]["content"]
    assert pack_a.render_context() == build_literary_builder_context_pack(
        chapter_a,
        _registry(),
        budget_tokens=600,
        recent_carryover_limit=0,
    ).render_context()


def test_density_audit_flags_large_jump(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE blocks (
            block_id TEXT, doc_id TEXT, chapter_id TEXT, order_index INTEGER,
            block_type TEXT, text TEXT, original_text TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "treasure_island_ch02_b001",
                "ti",
                "treasure_island_ch02",
                1,
                "prose",
                "The Admiral Benbow Inn stood by the road.",
                "The Admiral Benbow Inn stood by the road.",
            ),
            (
                "treasure_island_ch03_b001",
                "ti",
                "treasure_island_ch03",
                2,
                "prose",
                "Black Dog saw rum, cutlass, spyglass, and cove.",
                "Black Dog saw rum, cutlass, spyglass, and cove.",
            ),
        ],
    )
    prepass_dir = tmp_path / "prepass"
    prepass_dir.mkdir()
    (prepass_dir / "treasure_island_ch02.json").write_text(
        json.dumps(
            {
                "chapter_id": "treasure_island_ch02",
                "glossary_candidates": [
                    {"source_term": "Admiral Benbow Inn", "category": "place"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (prepass_dir / "treasure_island_ch03.json").write_text(
        json.dumps(
            {
                "chapter_id": "treasure_island_ch03",
                "glossary_candidates": [
                    {"source_term": "Black Dog", "category": "other"},
                    {"source_term": "rum", "category": "cultural"},
                    {"source_term": "cutlass", "category": "nautical"},
                    {"source_term": "spyglass", "category": "nautical"},
                ],
            }
        ),
        encoding="utf-8",
    )

    audit = render_prompts._build_density_audit(
        conn,
        "ti",
        ["ch02", "ch03"],
        prepass_dir,
        density_threshold=2.0,
    )

    assert audit["chapters"][0]["status"] == "OK"
    assert audit["chapters"][1]["status"] == "REVIEW_REQUIRED"
    assert audit["chapters"][1]["density_anomaly"] is True
    assert audit["chapters"][1]["hapax_count"] == 4
    conn.close()
