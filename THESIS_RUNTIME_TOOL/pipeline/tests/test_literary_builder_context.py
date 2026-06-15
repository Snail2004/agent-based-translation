from __future__ import annotations

from pipeline.prepass.literary_context import build_literary_builder_context_pack
from pipeline.prepass.prompt import build_messages
from pipeline.prepass.registry import PrepassRegistry


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
                    "state_label": "wary_curiosity",
                    "address_a_to_b_vi": "ông",
                    "address_b_to_a_vi": "cậu",
                },
                {
                    "a": "ent_black_dog",
                    "b": "ent_captain",
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
    assert "ent_narrator<->ent_captain" in rendered
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
