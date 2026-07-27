from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.chapter_registry_schema_v3 import (
    ALIAS_SCOPE_POLICY_VERSION,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    DELTA_SCHEMA_VERSION,
    ORIENTATION_SCHEMA_VERSION,
    PROMPT_IDS,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RegistryContractError,
    RegistryStaleRevisionError,
    RunConfigV3,
    VALIDATOR_VERSION,
)
from pipeline.literary.chapter_registry_v3 import (
    ChapterRegistryStoreV3,
    ChapterWorkingRegistryV3,
    SyntheticRegistryExecutorV3,
    apply_audit_responses,
    build_b2_candidate_manifest,
    build_registry_generation,
    build_registry_windows,
    checklist_coverage,
    empty_registry_snapshot_v3,
    render_auditor_requests,
    render_b0_request,
    render_b1_request,
    route_alias_for_commit,
    run_synthetic_registry_chapter_v3,
    schedule_targeted_recall,
    select_candidate_packets,
    validate_orientation_response,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
FROZEN_SHA256 = "64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715"


def _config(**overrides: Any) -> RunConfigV3:
    values: dict[str, Any] = {
        "b0_model_id": "fake-orient",
        "b0_reasoning_effort": "none",
        "b0_temperature": 0.0,
        "b0_seed": 1,
        "b0_output_cap": 2000,
        "b1_model_id": "fake-registry",
        "b1_reasoning_effort": "none",
        "b1_temperature": 0.0,
        "b1_seed": 2,
        "b1_output_cap": 2000,
        "auditor_model_id": "fake-auditor",
        "auditor_reasoning_effort": "none",
        "auditor_temperature": 0.0,
        "auditor_seed": 3,
        "auditor_output_cap": 2000,
        "b1_window_target_tokens": 500,
        "b1_window_max_blocks": 8,
        "context_only_tail_k": 1,
        "recency_k": 2,
        "candidate_card_count_cap": 16,
        "candidate_card_token_cap": 4000,
        "candidate_packet_count_cap": 16,
        "targeted_recall_call_cap": 8,
        "ticket_component_cap": 16,
        "auditor_call_cap": 16,
        "auditor_input_token_cap": 12000,
        "auditor_output_token_cap": 3000,
        "ticket_share_warning": 0.5,
        "ticket_share_halt": 0.9,
        "component_share_warning": 0.5,
        "component_share_halt": 0.9,
        "b0_input_cap": 50000,
        "b1_input_cap": 50000,
        "pricing_usd_per_million": {
            role: {"input": 0.0, "cached_input": 0.0, "output": 0.0}
            for role in ("b0", "b1", "auditor")
        },
        "quota_gates": {
            "b0-test": {
                "quota_bucket_id": "test-b0",
                "model_id": "fake-orient",
                "rpm": 100,
                "tpm": 100000,
                "rpd": 1000,
                "internal_utc_day_token_cap": 225000,
            },
            "b1-test": {
                "quota_bucket_id": "test-b1",
                "model_id": "fake-registry",
                "rpm": 100,
                "tpm": 100000,
                "rpd": 1000,
                "internal_utc_day_token_cap": 225000,
            },
            "audit-test": {
                "quota_bucket_id": "test-auditor",
                "model_id": "fake-auditor",
                "rpm": 100,
                "tpm": 100000,
                "rpd": 1000,
                "internal_utc_day_token_cap": 225000,
            },
        },
        "role_quota_gate_ids": {
            "b0": ("b0-test",),
            "b1": ("b1-test",),
            "auditor": ("audit-test",),
        },
        "prompt_versions": dict(PROMPT_IDS),
        "schema_versions": {
            "registry": REGISTRY_SCHEMA_VERSION,
            "b0": ORIENTATION_SCHEMA_VERSION,
            "b1": DELTA_SCHEMA_VERSION,
            "auditor": AUDIT_SCHEMA_VERSION,
        },
        "validator_version": VALIDATOR_VERSION,
        "policy_versions": {
            "candidate_selection": CANDIDATE_POLICY_VERSION,
            "alias_scope": ALIAS_SCOPE_POLICY_VERSION,
            "b2_rescan": B2_RESCAN_POLICY_VERSION,
        },
    }
    values.update(overrides)
    return RunConfigV3(**values)


def _chapter(*texts: str) -> dict[str, Any]:
    blocks = [
        {
            "block_id": "ch01_b001",
            "order_index": 1,
            "block_type": "heading",
            "clean_text": "Chapter One",
        }
    ]
    for index, text in enumerate(texts, 2):
        blocks.append(
            {
                "block_id": f"ch01_b{index:03d}",
                "order_index": index,
                "block_type": "paragraph",
                "clean_text": text,
            }
        )
    return {"chapter_id": "ch01", "blocks": blocks}


def _orientation(chapter: Mapping[str, Any], checklist: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return validate_orientation_response(
        {
            "gist": "A concise source-grounded orientation.",
            "narrator_hypotheses": [],
            "salient_registry_checklist": checklist or [],
        },
        chapter,
    )


def _snapshot_with_entities(*rows: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = empty_registry_snapshot_v3("lineage-test")
    snapshot["entities"] = [dict(row) for row in rows]
    snapshot["snapshot_hash"] = canonical_hash(
        {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    )
    return snapshot


def _entity(
    entity_id: str,
    surface: str,
    *,
    kind: str = "person",
    blocks: tuple[str, ...] = ("ch01_b002",),
    status: str = "confirmed",
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "name_class": "proper_name",
        "referent_kind": kind,
        "identity_summary": f"A stable {kind} named {surface}.",
        "created_from_block_ids": [blocks[0]],
        "support_block_ids": list(blocks),
        "status": status,
        "revision_hash": canonical_hash({"entity_id": entity_id, "surface": surface}),
    }


def _working(chapter: Mapping[str, Any], snapshot: Mapping[str, Any] | None = None) -> ChapterWorkingRegistryV3:
    return ChapterWorkingRegistryV3.create(
        state_lineage_id="lineage-test",
        chapter_id=str(chapter["chapter_id"]),
        source_manifest_hash=canonical_hash(chapter),
        parent_snapshot=snapshot or empty_registry_snapshot_v3("lineage-test"),
    )


def _b1_request(
    chapter: Mapping[str, Any],
    working: ChapterWorkingRegistryV3,
    orientation: Mapping[str, Any] | None = None,
    config: RunConfigV3 | None = None,
) -> Any:
    windows = build_registry_windows(
        chapter, target_tokens=5000, max_blocks=20, preceding_tail_k=1
    )
    window = windows[0]
    return render_b1_request(
        chapter_id=str(chapter["chapter_id"]),
        window_id=str(window["window_id"]),
        orientation=orientation or _orientation(chapter),
        active_blocks=window["blocks"],
        context_only_tail=window["context_only_tail"],
        working=working,
        block_order={str(row["block_id"]): int(row["order_index"]) for row in chapter["blocks"]},
        design_doc=DESIGN_DOC,
        run_config=config or _config(),
    )


def _empty_delta() -> dict[str, Any]:
    return {"new_entities": [], "new_glossary_items": [], "tickets": []}


def _new_entity(surface: str, block_id: str, **changes: Any) -> dict[str, Any]:
    row = {
        "surface": surface,
        "name_class": "proper_name",
        "referent_kind_claim": "person",
        "short_description": f"A stable person named {surface}.",
        "source_block_ids": [block_id],
    }
    row.update(changes)
    return row


def _ticket(
    ticket_type: str,
    surface: str | None,
    block_id: str,
    *,
    candidate_entity_ids: list[str] | None = None,
    kind: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    return {
        "ticket_type": ticket_type,
        "surface": surface,
        "source_block_ids": [block_id],
        "candidate_entity_ids": candidate_entity_ids or [],
        "candidate_glossary_ids": [],
        "referent_kind_claim": kind,
        "proposed_short_description": description,
        "reason": "The supplied evidence requires bounded review.",
    }


def test_probe_01_existing_named_entity_can_emit_empty_delta() -> None:
    chapter = _chapter("Arden returned to the hall.")
    working = _working(chapter, _snapshot_with_entities(_entity("ent3_old", "Arden")))
    request = _b1_request(chapter, working)

    result = working.apply_delta(request, _empty_delta())

    assert result["entity_ids"] == []
    assert [row["entity_id"] for row in working.snapshot()["entities"]] == ["ent3_old"]


def test_probe_02_new_proper_name_is_one_clean_provisional_entity() -> None:
    chapter = _chapter("They greeted Arden in the hall.")
    working = _working(chapter)
    request = _b1_request(chapter, working)

    result = working.apply_delta(
        request,
        {"new_entities": [_new_entity("Arden", "ch01_b002")], "new_glossary_items": [], "tickets": []},
    )

    assert len(result["entity_ids"]) == 1
    assert working.snapshot()["entities"][0]["status"] == "provisional"
    assert result["entity_ids"][0] in working.clean_entity_ids


def test_probe_03_single_candidate_with_fresh_conflict_can_remain_distinct() -> None:
    chapter = _chapter("Arden, a newly arrived woman, entered quietly.")
    working = _working(chapter, _snapshot_with_entities(_entity("ent3_old", "Arden")))
    request = _b1_request(chapter, working)
    response = {
        "new_entities": [
            _new_entity(
                "Arden",
                "ch01_b002",
                short_description="A newly arrived woman distinct from the supplied profile.",
            )
        ],
        "new_glossary_items": [],
        "tickets": [
            _ticket(
                "same_name_collision", "Arden", "ch01_b002", candidate_entity_ids=["ent3_old"]
            )
        ],
    }

    result = working.apply_delta(request, response)

    assert len(result["entity_ids"]) == 1
    assert len(result["ticket_ids"]) == 1


def test_probe_04_same_surface_is_a_multimap_not_an_auto_merge() -> None:
    chapter = _chapter("Arden answered from the doorway.")
    snapshot = _snapshot_with_entities(
        _entity("ent3_a", "Arden"),
        _entity("ent3_b", "Arden", kind="animal"),
    )
    selection = select_candidate_packets(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=chapter["blocks"][1:],
        context_only_tail=[],
        block_order={row["block_id"]: row["order_index"] for row in chapter["blocks"]},
        recency_k=0,
        card_count_cap=8,
        card_token_cap=4000,
        packet_count_cap=8,
    )

    assert len(selection["surface_candidate_packets"]) == 1
    assert {row["entity_id"] for row in selection["surface_candidate_packets"][0]["candidate_entities"]} == {"ent3_a", "ent3_b"}


def test_context_only_tail_never_creates_active_candidate_packets_or_ticket_work() -> None:
    chapter = _chapter("Arden crossed the yard.", "Silence filled the room.")
    snapshot = _snapshot_with_entities(
        _entity("ent3_arden", "Arden", blocks=("ch01_b002",))
    )
    snapshot["tickets"] = [
        {
            "ticket_id": "tick3_tail_only",
            "ticket_type": "profile_conflict",
            "surface": "Arden",
            "source_block_ids": ["ch01_b002"],
            "candidate_entity_ids": ["ent3_arden"],
            "status": "open",
        }
    ]
    selection = select_candidate_packets(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=[chapter["blocks"][2]],
        context_only_tail=[chapter["blocks"][1]],
        block_order={row["block_id"]: row["order_index"] for row in chapter["blocks"]},
        recency_k=2,
        card_count_cap=8,
        card_token_cap=4000,
        packet_count_cap=8,
    )

    assert selection["surface_candidate_packets"] == []
    assert selection["relevant_open_tickets"] == []
    assert selection["unmatched_recency_cards"][0]["candidate_card"]["entity_id"] == "ent3_arden"
    assert selection["candidate_selection_manifest"]["lexical_match_scope"] == "active_blocks_only"


def test_probe_05_possible_alias_is_ticketed_before_any_merge() -> None:
    chapter = _chapter("Arden Vale signed the ledger.")
    working = _working(chapter, _snapshot_with_entities(_entity("ent3_old", "Arden")))
    request = _b1_request(chapter, working)
    response = {
        "new_entities": [_new_entity("Arden Vale", "ch01_b002")],
        "new_glossary_items": [],
        "tickets": [
            _ticket(
                "possible_alias", "Arden Vale", "ch01_b002", candidate_entity_ids=["ent3_old"]
            )
        ],
    }

    result = working.apply_delta(request, response)

    assert len(result["entity_ids"]) == 1
    assert len(working.snapshot()["entities"]) == 2
    assert working.snapshot()["aliases"] == []


def test_probe_06_bare_pronoun_never_becomes_a_stable_entity() -> None:
    chapter = _chapter("He entered quietly.")
    working = _working(chapter)
    request = _b1_request(chapter, working)

    result = working.apply_delta(
        request,
        {"new_entities": [_new_entity("He", "ch01_b002")], "new_glossary_items": [], "tickets": []},
    )

    assert result["entity_ids"] == []
    assert working.snapshot()["entities"] == []
    assert working.snapshot()["tickets"][0]["ticket_type"] == "alias_scope_review"


def test_probe_07_contextual_expression_is_ticket_only() -> None:
    chapter = _chapter("The captain answered sharply.")
    working = _working(chapter)
    request = _b1_request(chapter, working)

    working.apply_delta(
        request,
        {
            "new_entities": [],
            "new_glossary_items": [],
            "tickets": [_ticket("surface_class_review", "The captain", "ch01_b002")],
        },
    )

    assert working.snapshot()["entities"] == []
    assert working.snapshot()["tickets"][0]["ticket_type"] == "surface_class_review"


@pytest.mark.parametrize(
    ("surface", "text", "expected"),
    [
        ("kind friend", "A kind friend arrived.", "pending_scope_review"),
        ("Ember", "They called Ember from the hall.", "eligible_global_alias"),
        ("Ember", "Ember arrived.", "pending_scope_review"),
    ],
)
def test_probes_09_10_11_alias_gate_is_conservative_and_mechanical(
    surface: str, text: str, expected: str
) -> None:
    record = route_alias_for_commit(
        surface=surface,
        name_class="stable_nickname",
        target_entity_id="ent3_target",
        source_block_ids=["b1"],
        source_catalog={"b1": text},
        source_decision_lineage={"ticket_id": "tick3_x"},
    )

    assert record["outcome"] == expected
    assert record["gate_record_hash"]


def test_probe_12_important_unnamed_checklist_is_covered_only_by_typed_ticket() -> None:
    chapter = _chapter("A nameless hound guarded the gate.")
    orientation = _orientation(
        chapter,
        [
            {
                "surface": "nameless hound",
                "block_id": "ch01_b002",
                "checklist_class": "important_unnamed_referent",
                "importance_note": "The individualized animal materially interacts.",
            }
        ],
    )
    working = _working(chapter)
    request = _b1_request(chapter, working, orientation)
    working.apply_delta(
        request,
        {
            "new_entities": [],
            "new_glossary_items": [],
            "tickets": [
                _ticket(
                    "important_unnamed_referent",
                    "nameless hound",
                    "ch01_b002",
                    kind="animal",
                    description="An individualized unnamed guard animal.",
                )
            ],
        },
    )

    coverage = checklist_coverage(orientation, working.snapshot())

    assert coverage["missing_count"] == 0


def test_targeted_recall_is_capability_scoped_to_supplied_checklist_rows() -> None:
    chapter = _chapter("Arden watched a nameless hound guard the gate.")
    checklist = [
        {
            "surface": "Arden",
            "block_id": "ch01_b002",
            "checklist_class": "stable_named_referent",
            "importance_note": "A stable named referent.",
        },
        {
            "surface": "nameless hound",
            "block_id": "ch01_b002",
            "checklist_class": "important_unnamed_referent",
            "importance_note": "An individualized unnamed animal.",
        },
    ]
    orientation = _orientation(chapter, checklist)
    target = [orientation["salient_registry_checklist"][1]]
    working = _working(chapter)
    window = build_registry_windows(
        chapter, target_tokens=5000, max_blocks=20, preceding_tail_k=1
    )[0]
    request = render_b1_request(
        chapter_id="ch01",
        window_id=f"{window['window_id']}:targeted",
        orientation=orientation,
        active_blocks=window["blocks"],
        context_only_tail=window["context_only_tail"],
        working=working,
        block_order={str(row["block_id"]): int(row["order_index"]) for row in chapter["blocks"]},
        design_doc=DESIGN_DOC,
        run_config=_config(),
        targeted_checklist_rows=target,
    )

    assert request.sections["b0_checklist_rows_for_active_blocks"] == target
    with pytest.raises(RegistryContractError, match="out-of-scope entity"):
        working.apply_delta(
            request,
            {
                "new_entities": [_new_entity("Arden", "ch01_b002")],
                "new_glossary_items": [],
                "tickets": [],
            },
            targeted_recall=True,
        )


def test_targeted_recall_must_close_checklist_coverage_before_auditor() -> None:
    chapter = _chapter("Joseph entered the yard.")
    checklist = [
        {
            "surface": "Joseph",
            "block_id": "ch01_b002",
            "checklist_class": "stable_named_referent",
            "importance_note": "A stable named referent.",
        }
    ]
    executor = SyntheticRegistryExecutorV3()
    executor.add(
        "b0",
        {
            "gist": "A named person enters the yard.",
            "narrator_hypotheses": [],
            "salient_registry_checklist": checklist,
        },
    )
    executor.add("b1", _empty_delta())
    executor.add("b1", _empty_delta())

    with pytest.raises(RegistryContractError, match="uncovered checklist rows"):
        run_synthetic_registry_chapter_v3(
            chapter=chapter,
            state_lineage_id="lineage-test",
            parent_snapshot=empty_registry_snapshot_v3("lineage-test"),
            executor=executor,
            design_doc=DESIGN_DOC,
            run_config=_config(),
        )


def _unnamed_audit_fixture() -> tuple[dict[str, Any], dict[str, Any], ChapterWorkingRegistryV3, Any]:
    chapter = _chapter("A nameless hound guarded the gate.")
    orientation = _orientation(chapter)
    working = _working(chapter)
    request = _b1_request(chapter, working, orientation)
    working.apply_delta(
        request,
        {
            "new_entities": [],
            "new_glossary_items": [],
            "tickets": [
                _ticket(
                    "important_unnamed_referent",
                    "nameless hound",
                    "ch01_b002",
                    kind="animal",
                    description="An individualized unnamed guard animal.",
                )
            ],
        },
    )
    audit_request = render_auditor_requests(
        chapter=chapter,
        orientation=orientation,
        working=working,
        design_doc=DESIGN_DOC,
        run_config=_config(),
    )[0]
    ticket_id = audit_request.sections["ticket_component"]["ticket_ids"][0]
    response = {
        "ticket_dispositions": [
            {
                "ticket_id": ticket_id,
                "action": "create_unnamed_entity",
                "source_entity_id": None,
                "target_entity_id": None,
                "source_glossary_id": None,
                "target_glossary_id": None,
                "resolved_referent_kind": "animal",
                "revised_identity_summary": "An individualized unnamed guard animal.",
                "name_class": None,
                "resolution_note": "The cited source supports a distinct unnamed animal.",
            }
        ]
    }
    return chapter, orientation, working, (audit_request, response)


def test_probes_13_14_15_unnamed_entity_has_no_alias_and_reaches_b2_as_candidate_only() -> None:
    chapter, _, working, pair = _unnamed_audit_fixture()
    catalog = {row["block_id"]: row["clean_text"] for row in chapter["blocks"]}
    apply_audit_responses(working=working, request_response_pairs=[pair], source_catalog=catalog)
    snapshot = working.snapshot()
    snapshot["generation_id"] = "reggen3_" + "a" * 20
    manifest = build_b2_candidate_manifest(
        chapter_id="ch01",
        active_blocks=chapter["blocks"][1:],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )

    assert len(snapshot["entities"]) == 1
    assert snapshot["entities"][0]["name_class"] is None
    assert snapshot["aliases"] == []
    assert "support_block" in {row["candidate_source"] for row in manifest["candidate_links"]}
    assert manifest["authoritative_bindings"] == []


def test_probe_16_named_place_is_entity_not_duplicate_glossary() -> None:
    chapter = _chapter("North House stood above the valley.")
    working = _working(chapter)
    request = _b1_request(chapter, working)
    working.apply_delta(
        request,
        {
            "new_entities": [
                _new_entity(
                    "North House",
                    "ch01_b002",
                    referent_kind_claim="place",
                    short_description="A named residence above the valley.",
                )
            ],
            "new_glossary_items": [],
            "tickets": [],
        },
    )

    assert working.snapshot()["entities"][0]["referent_kind"] == "place"
    assert working.snapshot()["glossary_items"] == []


def test_probe_17_generic_common_noun_can_leave_all_lists_empty() -> None:
    chapter = _chapter("A chair stood near the wall.")
    working = _working(chapter)
    result = working.apply_delta(_b1_request(chapter, working), _empty_delta())
    assert result["entity_ids"] == result["glossary_ids"] == result["ticket_ids"] == []


def test_probe_18_one_packet_deduplicates_card_and_collects_all_blocks() -> None:
    chapter = _chapter("Arden entered.", "Arden replied.")
    snapshot = _snapshot_with_entities(_entity("ent3_a", "Arden"))
    selection = select_candidate_packets(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=chapter["blocks"][1:],
        context_only_tail=[],
        block_order={row["block_id"]: row["order_index"] for row in chapter["blocks"]},
        recency_k=0,
        card_count_cap=8,
        card_token_cap=4000,
        packet_count_cap=8,
    )
    packet = selection["surface_candidate_packets"][0]
    assert packet["source_block_ids"] == ["ch01_b002", "ch01_b003"]
    assert len(packet["candidate_entities"]) == 1


def test_probe_19_candidate_overflow_yields_code_ticket_and_no_entity() -> None:
    chapter = _chapter("Arden answered.")
    snapshot = _snapshot_with_entities(_entity("ent3_a", "Arden"), _entity("ent3_b", "Arden"))
    working = _working(chapter, snapshot)
    config = _config(candidate_card_count_cap=1)
    request = _b1_request(chapter, working, config=config)

    result = working.apply_delta(
        request,
        {"new_entities": [_new_entity("Arden", "ch01_b002")], "new_glossary_items": [], "tickets": []},
    )

    assert result["entity_ids"] == []
    assert working.snapshot()["tickets"][0]["ticket_type"] == "candidate_overflow"


def test_probe_19b_candidate_overlap_without_identity_ticket_is_fatal() -> None:
    chapter = _chapter("Arden answered.")
    working = _working(chapter, _snapshot_with_entities(_entity("ent3_a", "Arden")))
    request = _b1_request(chapter, working)

    with pytest.raises(RegistryContractError, match="lacks an identity-review ticket"):
        working.apply_delta(
            request,
            {
                "new_entities": [_new_entity("Arden", "ch01_b002")],
                "new_glossary_items": [],
                "tickets": [],
            },
        )


def test_probe_20_foreign_candidate_or_block_is_fatal() -> None:
    chapter = _chapter("Arden answered.")
    working = _working(chapter, _snapshot_with_entities(_entity("ent3_a", "Arden")))
    request = _b1_request(chapter, working)
    response = {
        "new_entities": [],
        "new_glossary_items": [],
        "tickets": [
            _ticket(
                "same_name_collision", "Arden", "ch01_b002", candidate_entity_ids=["ent3_foreign"]
            )
        ],
    }
    with pytest.raises(RegistryContractError, match="foreign candidate entity"):
        working.apply_delta(request, response)

    working = _working(chapter)
    request = _b1_request(chapter, working)
    response = {
        "new_entities": [_new_entity("Arden", "foreign_block")],
        "new_glossary_items": [],
        "tickets": [],
    }
    with pytest.raises(RegistryContractError, match="foreign or context-only"):
        working.apply_delta(request, response)


def test_probes_21_22_23_sequential_revision_and_replay_contract() -> None:
    chapter = _chapter("Arden answered.")
    working = _working(chapter)
    request = _b1_request(chapter, working)
    first = working.apply_delta(request, _empty_delta())
    assert working.apply_delta(request, _empty_delta()) == first
    with pytest.raises(RegistryContractError, match="different response"):
        working.apply_delta(
            request,
            {"new_entities": [_new_entity("Arden", "ch01_b002")], "new_glossary_items": [], "tickets": []},
        )
    stale = replace(
        _b1_request(chapter, _working(chapter)), request_fingerprint="different-request"
    )
    with pytest.raises(RegistryStaleRevisionError):
        working.apply_delta(stale, _empty_delta())


def test_probes_24_25_targeted_recall_is_bounded_and_does_not_renumber_prior_ids() -> None:
    chapter = _chapter("Arden answered.", "Bell entered.")
    orientation = _orientation(
        chapter,
        [
            {
                "surface": "Bell",
                "block_id": "ch01_b003",
                "checklist_class": "stable_named_referent",
                "importance_note": "A salient stable name.",
            }
        ],
    )
    working = _working(chapter)
    request = _b1_request(chapter, working, orientation)
    first = working.apply_delta(
        request,
        {"new_entities": [_new_entity("Arden", "ch01_b002")], "new_glossary_items": [], "tickets": []},
    )["entity_ids"][0]
    windows = build_registry_windows(chapter, target_tokens=2, max_blocks=1, preceding_tail_k=0)
    scheduled = schedule_targeted_recall(
        orientation=orientation,
        working_snapshot=working.snapshot(),
        windows=windows,
        call_cap=2,
    )
    assert len(scheduled) == 1
    assert working.snapshot()["entities"][0]["entity_id"] == first
    with pytest.raises(RegistryBudgetError):
        schedule_targeted_recall(
            orientation=orientation,
            working_snapshot=working.snapshot(),
            windows=windows,
            call_cap=0,
        )


def test_probe_26_auditor_requires_exact_ticket_cover() -> None:
    chapter, _, working, pair = _unnamed_audit_fixture()
    request, response = pair
    response["ticket_dispositions"] = []
    with pytest.raises(RegistryContractError, match="exact-cover"):
        apply_audit_responses(
            working=working,
            request_response_pairs=[(request, response)],
            source_catalog={row["block_id"]: row["clean_text"] for row in chapter["blocks"]},
        )


def _clean_generation_fixture() -> tuple[dict[str, Any], Any, ChapterWorkingRegistryV3, Any]:
    chapter = _chapter("They greeted Arden in the hall.")
    config = _config()
    orientation = _orientation(chapter)
    working = _working(chapter)
    request = _b1_request(chapter, working, orientation, config)
    working.apply_delta(
        request,
        {"new_entities": [_new_entity("Arden", "ch01_b002")], "new_glossary_items": [], "tickets": []},
    )
    apply_audit_responses(working=working, request_response_pairs=[], source_catalog={})
    b0 = render_b0_request(chapter=chapter, design_doc=DESIGN_DOC, run_config=config)
    generation = build_registry_generation(
        working=working,
        orientation=orientation,
        b0_request=b0,
        source_catalog={row["block_id"]: row["clean_text"] for row in chapter["blocks"]},
        run_config=config,
        audit_decisions=[],
    )
    return chapter, orientation, working, generation


def test_probe_27_crash_before_pointer_switch_keeps_prior_generation(tmp_path: Path) -> None:
    _, _, _, generation = _clean_generation_fixture()
    store = ChapterRegistryStoreV3(tmp_path / "registry")

    with pytest.raises(RuntimeError, match="crash"):
        store.commit(
            generation,
            expected_parent=None,
            before_pointer_switch=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
        )

    assert store.current_generation_id("lineage-test") is None
    store.commit(generation, expected_parent=None)
    assert store.current_generation_id("lineage-test") == generation.generation_id


def test_probe_28_unregistered_alias_source_fails_prepublication_gate() -> None:
    chapter, orientation, working, _ = _clean_generation_fixture()
    working._state["aliases"].append(
        {
            "alias_id": "als3_unregistered",
            "surface": "Arden Vale",
            "name_class": "proper_name",
            "entity_id": working.snapshot()["entities"][0]["entity_id"],
            "support_block_ids": ["ch01_b002"],
            "status": "confirmed",
            "gate_outcome": "eligible_global_alias",
            "gate_record_hash": "missing",
            "revision_hash": "missing",
        }
    )
    with pytest.raises(RegistryContractError, match="registered gate record"):
        build_registry_generation(
            working=working,
            orientation=orientation,
            b0_request=render_b0_request(chapter=chapter, design_doc=DESIGN_DOC, run_config=_config()),
            source_catalog={row["block_id"]: row["clean_text"] for row in chapter["blocks"]},
            run_config=_config(),
            audit_decisions=[],
        )


def test_probe_31_frozen_database_hash_is_unchanged() -> None:
    assert file_sha256(FROZEN_DB).upper() == FROZEN_SHA256
