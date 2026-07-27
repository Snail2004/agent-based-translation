from __future__ import annotations

import pytest

from pipeline.scripts.run_literary_b2_review_routing_v1 import (
    _review_scope_from_b2_input_v1,
)


def _chapter(chapter_id: str, registry_hash: str) -> dict[str, object]:
    return {
        "chapter_id": chapter_id,
        "source_registry": {"registry_hash": registry_hash},
    }


def test_first_chapter_does_not_require_a_reconciled_projection() -> None:
    assert _review_scope_from_b2_input_v1(
        package={
            "chapters": [_chapter("wh_ch01", "r1")],
            "ordered_chapter_ids": ["wh_ch01"],
            "reconciled_projection": None,
        },
        chapter_id="wh_ch01",
        registry_hash="r1",
    ) == ([], [])


def test_multi_chapter_package_still_requires_a_reconciled_projection() -> None:
    with pytest.raises(
        ValueError,
        match="multi-chapter B2 input package has no reconciled projection",
    ):
        _review_scope_from_b2_input_v1(
            package={
                "chapters": [
                    _chapter("wh_ch01", "r1"),
                    _chapter("wh_ch02", "r2"),
                ],
                "ordered_chapter_ids": ["wh_ch01", "wh_ch02"],
                "reconciled_projection": None,
            },
            chapter_id="wh_ch02",
            registry_hash="r2",
        )


def test_review_scope_includes_prefix_member_card_not_used_as_effective_id() -> None:
    cards, superseded = _review_scope_from_b2_input_v1(
        package={
            "chapters": [
                {
                    **_chapter("wh_ch06", "r6"),
                    "prefix_bundle": {
                        "b0_context_cards": [],
                        "candidate_only_context_cards": [
                            {
                                "prior_card_id": "card_heathcliff_ch05",
                                "canonical_surface": "Heathcliff",
                                "first_supported_block_id": "wh_ch05_b002",
                                "provenance_refs": [
                                    {
                                        "chapter_id": "wh_ch05",
                                        "block_id": "wh_ch05_b002",
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
            "ordered_chapter_ids": ["wh_ch06"],
            "reconciled_projection": {
                "effective_entities": [
                    {
                        "effective_entity_id": "card_heathcliff_ch04",
                        "member_card_ids": [
                            "card_heathcliff_ch04",
                            "card_heathcliff_ch05",
                        ],
                        "first_seen": {"chapter_id": "wh_ch04"},
                        "source_refs": ["scan:heathcliff"],
                    }
                ],
                "superseded_pending_cases": [],
            },
        },
        chapter_id="wh_ch06",
        registry_hash="r6",
    )

    assert superseded == []
    assert [row.get("entity_id") or row.get("effective_entity_id") for row in cards] == [
        "card_heathcliff_ch04",
        "card_heathcliff_ch05",
    ]
    assert cards[1]["first_seen"] == {
        "chapter_id": "wh_ch05",
        "block_id": "wh_ch05_b002",
    }
