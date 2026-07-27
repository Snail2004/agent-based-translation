from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.b1_scan_v1 import (
    _estimate_memory_row_tokens_v1,
    _measure_model_visible_memory_tokens_v1,
    allocate_b1_scan_memory_v1,
    build_b1_registry_roster_v1,
    build_prior_candidate_packets_v1,
    render_b1_scan_request_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    LiterarySharedRuntimeProfileV2Error,
    load_literary_shared_runtime_profile_v2,
)


DESIGN_DOC = Path(__file__).resolve().parents[3] / "design" / "LITERARY_PROMPT_DESIGN.md"
MEMORY_PROFILE = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b1_scan_v10_24k_16k_12k_memory.json"
)
ORDER = {f"bk_ch{index:02d}": index for index in range(1, 7)}


def _chapter(text: str = "A quiet morning passed.") -> dict:
    return {
        "chapter_id": "bk_ch06",
        "blocks": [
            {
                "block_id": "bk_ch06_b001",
                "order_index": 1,
                "clean_text": text,
            }
        ],
    }


def _card(
    card_id: str,
    surface: str,
    *,
    chapter_ids: tuple[str, ...] = ("bk_ch01",),
    referent_kind: str = "person",
    record_class: str = "confirmed_entity",
) -> dict:
    refs = [
        {
            "chapter_id": chapter_id,
            "block_id": f"{chapter_id}_b001",
        }
        for chapter_id in chapter_ids
    ]
    return {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "referent_kind": referent_kind,
        "identity_summary": f"{surface} is a prior referent.",
        "record_class": record_class,
        "presence_basis": "direct_presence",
        "claim_state": "confirmed",
        "first_supported_block_id": refs[0]["block_id"],
        "provenance_refs": refs,
    }


def _allocate(
    cards: list[dict],
    *,
    chapter: dict | None = None,
    budget: int,
) -> tuple[list[dict], list[dict], dict]:
    source = chapter or _chapter()
    return allocate_b1_scan_memory_v1(
        chapter=source,
        prior_cards=cards,
        prior_candidate_packets=build_prior_candidate_packets_v1(
            chapter=source, prior_cards=cards
        ),
        registry_roster=build_b1_registry_roster_v1(cards),
        memory_token_budget=budget,
        memory_dormancy_chapters=3,
        chapter_order_by_id=ORDER,
    )


def test_t1_dormant_nonperson_surface_hit_still_reaches_packet_channel() -> None:
    card = _card(
        "card_old_house",
        "Old House",
        referent_kind="place",
    )

    packets, _roster, report = _allocate(
        [card],
        chapter=_chapter("Lockwood entered Old House."),
        budget=10_000,
    )

    assert [row["prior_card"]["prior_card_id"] for row in packets] == [
        "card_old_house"
    ]
    assert {
        row["reason"] for row in report["omitted"]
    } == {"covered_by_admitted_packet"}


def test_t2_all_omissions_are_recorded_and_counts_reconcile() -> None:
    recent = _card(
        "card_recent_person",
        "Recent Person",
        chapter_ids=("bk_ch05",),
    )
    old_places = [
        _card(
            f"card_old_place_{index}",
            f"Old Place {index}",
            referent_kind="place",
        )
        for index in range(8)
    ]
    cards = [recent, *old_places]
    roster = build_b1_registry_roster_v1(cards)
    recent_row = next(
        row for row in roster if row["prior_card_id"] == "card_recent_person"
    )
    budget = _estimate_memory_row_tokens_v1(recent_row)

    packets, admitted_roster, report = _allocate(cards, budget=budget)

    assert packets == []
    assert [row["prior_card_id"] for row in admitted_roster] == [
        "card_recent_person"
    ]
    assert report["admitted"]["roster_rows"] + report["omitted_counts"][
        "roster_rows"
    ] == report["built"]["roster_rows"]
    assert len(report["omitted"]) == report["omitted_counts"]["roster_rows"]
    assert {row["tier"] for row in report["omitted"]} == {3}


def test_t3_evicted_tier_one_row_emits_review_issue() -> None:
    recent = _card(
        "card_recent_person",
        "Recent Person",
        chapter_ids=("bk_ch05",),
    )

    _packets, _roster, report = _allocate([recent], budget=1)

    assert report["omitted"][0]["tier"] == 1
    assert report["review_issues"][0]["reason"] == (
        "memory_budget_evicted_identity_row"
    )


def test_t4_absent_budget_keeps_rendered_request_byte_identical() -> None:
    cards = [_card("card_prior", "Prior Person")]
    baseline = render_b1_scan_request_v1(
        chapter=_chapter(),
        design_doc=DESIGN_DOC,
        prior_cards=cards,
    )
    explicit_absence = render_b1_scan_request_v1(
        chapter=_chapter(),
        design_doc=DESIGN_DOC,
        prior_cards=cards,
        memory_token_budget=None,
        memory_dormancy_chapters=3,
        chapter_order_by_id=ORDER,
    )

    assert explicit_absence.to_dict() == baseline.to_dict()
    assert explicit_absence.messages == baseline.messages
    assert "memory_budget_report" not in baseline.sections


def test_t5_allocator_is_deterministic() -> None:
    cards = [
        _card(f"card_{index}", f"Person {index}", chapter_ids=("bk_ch03",))
        for index in range(12)
    ]

    first = _allocate(cards, budget=500)
    second = _allocate(cards, budget=500)

    assert second == first


def test_memory_omission_ledger_is_auditable_but_not_model_visible() -> None:
    cards = [
        _card(f"card_{index}", f"Person {index}", chapter_ids=("bk_ch03",))
        for index in range(12)
    ]

    rendered = render_b1_scan_request_v1(
        chapter=_chapter(),
        design_doc=DESIGN_DOC,
        prior_cards=cards,
        memory_token_budget=100,
        memory_dormancy_chapters=3,
        chapter_order_by_id=ORDER,
    )

    assert rendered.sections["memory_budget_report"]["omitted"]
    assert "memory_budget_report" not in rendered.messages[1]["content"]


def test_memory_profile_loads_scoped_b1_scan_budget() -> None:
    profile = load_literary_shared_runtime_profile_v2(
        MEMORY_PROFILE,
        expected_role_ids={"literary.b1.scan"},
    )

    generation = profile.role_presets["literary.b1.scan"].generation
    assert generation["memory_token_budget"] == 12_000
    assert generation["memory_dormancy_chapters"] == 3
    assert generation["max_input_tokens"] == 24_000
    assert generation["max_output_tokens"] == 16_384


def test_memory_fields_are_rejected_for_non_scan_role(
    tmp_path: Path,
) -> None:
    payload = json.loads(MEMORY_PROFILE.read_text(encoding="utf-8"))
    payload["roles"][0]["role_id"] = "literary.b2.frame"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        LiterarySharedRuntimeProfileV2Error,
        match="reserved for literary.b1.scan",
    ):
        load_literary_shared_runtime_profile_v2(
            path,
            expected_role_ids={"literary.b2.frame"},
        )


def test_t5b_recent_principal_fixture_rows_survive_pressure() -> None:
    principal = [
        _card("card_mrs_dean", "Mrs. Dean", chapter_ids=("bk_ch04",)),
        _card("card_mr_earnshaw", "Mr. Earnshaw", chapter_ids=("bk_ch03",)),
        _card("card_cathy", "Cathy", chapter_ids=("bk_ch04",)),
        _card("card_mr_linton", "Mr. Linton", chapter_ids=("bk_ch04",)),
        _card("card_frances", "Frances", chapter_ids=("bk_ch04",)),
    ]
    dormant = [
        _card(
            f"card_scenery_{index}",
            f"Scenery {index}",
            referent_kind="object",
        )
        for index in range(30)
    ]
    principal_cost = _measure_model_visible_memory_tokens_v1(
        packets=[],
        roster=build_b1_registry_roster_v1(principal),
    )

    _packets, roster, report = _allocate(
        [*principal, *dormant],
        budget=principal_cost,
    )

    assert {row["canonical_surface"] for row in roster} == {
        "Mrs. Dean",
        "Mr. Earnshaw",
        "Cathy",
        "Mr. Linton",
        "Frances",
    }
    assert report["omitted_counts"]["roster_rows"] == len(dormant)
    assert {row["tier"] for row in report["omitted"]} == {3}
