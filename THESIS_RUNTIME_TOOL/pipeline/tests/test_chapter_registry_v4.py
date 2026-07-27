from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.chapter_registry_prompts_v4 import load_registry_prompt_v4
from pipeline.literary.chapter_registry_schema_v4 import (
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
    RunConfigV4,
    VALIDATOR_VERSION,
    response_json_schema,
)
from pipeline.literary.chapter_registry_v4 import (
    ChapterRegistryStoreV4,
    ChapterWorkingRegistryV4,
    apply_audit_responses,
    build_attention_packets,
    build_b2_candidate_manifest,
    build_exception_components,
    build_registry_generation,
    build_registry_windows,
    chapter_source_manifest_hash,
    empty_registry_snapshot_v4,
    render_auditor_requests,
    render_b0_request,
    render_b1_request,
    route_alias_for_commit,
    select_candidate_packets,
    validate_audit_response,
    validate_orientation_response,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
FROZEN_SHA256 = "64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715"


def _config(**overrides: Any) -> RunConfigV4:
    values: dict[str, Any] = {
        "b0_model_id": "offline-orient",
        "b0_reasoning_effort": "none",
        "b0_temperature": 0.0,
        "b0_seed": 1,
        "b0_output_token_cap": 2048,
        "b1_model_id": "offline-registry",
        "b1_reasoning_effort": "none",
        "b1_temperature": 0.0,
        "b1_seed": 2,
        "b1_output_token_cap": 4096,
        "auditor_model_id": "offline-auditor",
        "auditor_reasoning_effort": "none",
        "auditor_temperature": 0.0,
        "auditor_seed": 3,
        "auditor_output_token_cap": 8192,
        "b0_attention_context_mode": "advisory_active_window",
        "b0_input_token_cap": 18000,
        "b1_input_token_cap": 14000,
        "active_window_source_token_target": 500,
        "active_window_max_blocks": 8,
        "preceding_tail_block_cap": 2,
        "attention_packet_cap_per_window": 16,
        "known_surface_packet_cap_per_window": 32,
        "candidate_cards_total_cap_per_window": 16,
        "candidate_context_token_cap": 3500,
        "recency_neighbor_distance_blocks": 8,
        "candidate_overflow_policy": "ticket",
        "auditor_tickets_per_component_cap": 16,
        "auditor_calls_per_chapter_cap": 32,
        "auditor_neighbor_blocks_each_side": 0,
        "auditor_input_token_cap": 12000,
        "provider_quota_policy_hash": "offline-quota-v1",
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
    return RunConfigV4(**values)


def _chapter() -> dict[str, Any]:
    return {
        "chapter_id": "ch01",
        "blocks": [
            {
                "block_id": "ch01_b000",
                "order_index": 0,
                "block_type": "heading",
                "clean_text": "Chapter One",
            },
            {
                "block_id": "ch01_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr. Arden entered North Hall. Arden watched a silver hound.",
            },
            {
                "block_id": "ch01_b002",
                "order_index": 2,
                "block_type": "paragraph",
                "clean_text": "The hound followed Arden. The steward called it madam.",
            },
            {
                "block_id": "ch01_b003",
                "order_index": 3,
                "block_type": "paragraph",
                "clean_text": "Mrs. Arden crossed North Hall. the master waited.",
            },
            {
                "block_id": "ch01_b004",
                "order_index": 4,
                "block_type": "paragraph",
                "clean_text": "Aster bloom marked the local rite.",
            },
            {
                "block_id": "ch01_b005",
                "order_index": 5,
                "block_type": "paragraph",
                "clean_text": "The silver hound guarded the gate.",
            },
        ],
    }


def _catalog(chapter: Mapping[str, Any] | None = None) -> dict[str, str]:
    return {
        str(row["block_id"]): str(row["clean_text"])
        for row in (chapter or _chapter())["blocks"]
    }


def _order(chapter: Mapping[str, Any] | None = None) -> dict[str, int]:
    return {
        str(row["block_id"]): int(row["order_index"])
        for row in (chapter or _chapter())["blocks"]
    }


def _orientation(
    *,
    attention: list[dict[str, Any]] | None = None,
    draft: str | None = None,
    narrative_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return validate_orientation_response(
        {
            "orientation_draft": draft
            or "A visitor enters a named hall and observes an individualized animal.",
            "narrative_context": narrative_context
            or {
                "mode": "third_person_external",
                "note": "A chapter-level external narrative voice is apparent.",
                "support_block_ids": ["ch01_b001"],
            },
            "attention_items": attention
            if attention is not None
            else [
                {
                    "surface": "silver hound",
                    "source_block_ids": ["ch01_b001"],
                    "why_noticed": "It materially affects a named participant.",
                }
            ],
        },
        _chapter(),
        b0_request_fingerprint="b0-request-fingerprint",
    )


def _entity(
    entity_id: str,
    surface: str,
    *,
    name_class: str | None = "proper_name",
    kind: str = "person",
    blocks: tuple[str, ...] = ("ch01_b001",),
    gender: str | None = None,
    status: str = "confirmed",
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "name_class": name_class,
        "referent_kind": kind,
        "identity_summary": f"A stable {kind} identified as {surface}.",
        "referential_gender": gender,
        "referential_gender_support_block_ids": list(blocks) if gender else [],
        "created_from_block_ids": [blocks[0]],
        "support_block_ids": list(blocks),
        "latest_profile_revision_id": None,
        "created_by_request_fingerprint": "seed-request",
        "source_text_manifest_hash": "seed-source-hash",
        "status": status,
        "revision_hash": canonical_hash({"entity_id": entity_id, "surface": surface}),
    }


def _snapshot_with(
    *,
    entities: list[Mapping[str, Any]] | None = None,
    aliases: list[Mapping[str, Any]] | None = None,
    locals_: list[Mapping[str, Any]] | None = None,
    glossary: list[Mapping[str, Any]] | None = None,
    tickets: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = empty_registry_snapshot_v4("lineage-test")
    snapshot["entities"] = [dict(row) for row in entities or []]
    snapshot["global_aliases"] = [dict(row) for row in aliases or []]
    snapshot["block_local_references"] = [dict(row) for row in locals_ or []]
    snapshot["glossary_items"] = [dict(row) for row in glossary or []]
    snapshot["tickets"] = [dict(row) for row in tickets or []]
    snapshot["snapshot_hash"] = canonical_hash(
        {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    )
    return snapshot


def _working(snapshot: Mapping[str, Any] | None = None) -> ChapterWorkingRegistryV4:
    working = ChapterWorkingRegistryV4.create(
        state_lineage_id="lineage-test",
        chapter_id="ch01",
        source_manifest_hash=chapter_source_manifest_hash(_chapter()),
        parent_snapshot=snapshot or empty_registry_snapshot_v4("lineage-test"),
    )
    return working


def _b1_request(
    working: ChapterWorkingRegistryV4,
    *,
    active_ids: tuple[str, ...] = ("ch01_b001", "ch01_b002"),
    orientation: Mapping[str, Any] | None = None,
    config: RunConfigV4 | None = None,
    tail_ids: tuple[str, ...] = (),
):
    chapter = _chapter()
    rows = {str(row["block_id"]): row for row in chapter["blocks"]}
    return render_b1_request(
        chapter_id="ch01",
        window_id="w4_ch01_test",
        orientation=orientation or _orientation(),
        active_blocks=[rows[value] for value in active_ids],
        context_only_tail=[rows[value] for value in tail_ids],
        working=working,
        block_order=_order(chapter),
        design_doc=DESIGN_DOC,
        run_config=config or _config(),
    )


def _empty_delta() -> dict[str, Any]:
    return {
        "new_entities": [],
        "new_glossary_items": [],
        "surface_updates": [],
        "tickets": [],
    }


def _new_entity(
    surface: str,
    block_id: str,
    **changes: Any,
) -> dict[str, Any]:
    row = {
        "surface": surface,
        "name_class": "proper_name",
        "referent_kind_claim": "person",
        "identity_summary": f"A stable person identified as {surface}.",
        "referential_gender_claim": None,
        "source_block_ids": [block_id],
        "initial_surface_updates": [],
    }
    row.update(changes)
    return row


def _ticket(
    ticket_type: str,
    surface: str | None,
    block_id: str,
    *,
    candidate_entity_ids: list[str] | None = None,
    candidate_glossary_ids: list[str] | None = None,
    kind: str | None = None,
    summary: str | None = None,
    gender: str | None = None,
) -> dict[str, Any]:
    return {
        "ticket_type": ticket_type,
        "surface": surface,
        "source_block_ids": [block_id],
        "candidate_entity_ids": candidate_entity_ids or [],
        "candidate_glossary_ids": candidate_glossary_ids or [],
        "referent_kind_claim": kind,
        "proposed_identity_summary": summary,
        "proposed_referential_gender": gender,
        "reason": "Fresh source evidence requires review.",
    }


def _finalize_without_audit(working: ChapterWorkingRegistryV4) -> None:
    apply_audit_responses(
        working=working, requests=[], responses=[], source_catalog=_catalog()
    )


def test_probe_01_b0_attention_has_no_typed_answer() -> None:
    row = _orientation()["attention_ledger"][0]
    assert not {"referent_kind_claim", "entity_id", "expected_action"} & set(row)
    dropped = _orientation(
        attention=[
            {
                "surface": "silver hound",
                "source_block_ids": ["ch01_b001"],
                "why_noticed": "referent_kind_claim=animal",
            }
        ]
    )
    assert dropped["attention_ledger"] == []
    assert dropped["attention_validation_report"]["dropped_by_reason"][
        "typed_answer_marker"
    ] == 1


def test_probe_01a_b0_narrative_context_is_one_chapter_level_note() -> None:
    orientation = _orientation(
        narrative_context={
            "mode": "mixed_or_nested",
            "note": "A primary voice contains a nested account; transitions remain unresolved.",
            "support_block_ids": ["ch01_b001", "ch01_b003"],
        }
    )
    assert orientation["narrative_context"] == {
        "mode": "mixed_or_nested",
        "note": "A primary voice contains a nested account; transitions remain unresolved.",
        "support_block_ids": ["ch01_b001", "ch01_b003"],
    }
    assert "surface" not in orientation["narrative_context"]
    assert "block_range" not in orientation["narrative_context"]


def test_probe_01b_b0_narrative_context_rejects_foreign_support() -> None:
    with pytest.raises(RegistryContractError, match="narrative context cites foreign block"):
        _orientation(
            narrative_context={
                "mode": "third_person_external",
                "note": "An external voice is apparent.",
                "support_block_ids": ["foreign-block"],
            }
        )


def test_probe_01c_unlocatable_attention_is_dropped_without_losing_orientation() -> None:
    orientation = _orientation(
        attention=[
            {
                "surface": "not present",
                "source_block_ids": ["ch01_b001"],
                "why_noticed": "The expression might deserve closer reading.",
            }
        ]
    )
    assert orientation["orientation_draft"]
    assert orientation["attention_ledger"] == []
    assert orientation["attention_validation_report"] == {
        "input_count": 1,
        "accepted_count": 0,
        "dropped_count": 1,
        "dropped_by_reason": {
            "foreign_block": 0,
            "surface_not_located": 1,
            "reason_too_long": 0,
            "typed_answer_marker": 0,
        },
    }


def test_probe_02_attention_persists_source_lineage() -> None:
    row = _orientation()["attention_ledger"][0]
    assert row["source_block_ids"] == ["ch01_b001"]
    assert row["source_text_manifest_hash"]
    assert row["b0_request_fingerprint"] == "b0-request-fingerprint"


def test_probe_03_b1_can_ignore_attention_with_four_empty_lists() -> None:
    working = _working()
    request = _b1_request(working)
    record = working.apply_b1_response(request=request, response=_empty_delta())
    assert record["ticket_ids"] == []


def test_probe_04_ignored_attention_creates_no_workflow_debt() -> None:
    working = _working()
    working.install_attention_ledger(_orientation()["attention_ledger"])
    request = _b1_request(working)
    working.apply_b1_response(request=request, response=_empty_delta())
    assert working.snapshot()["tickets"] == []
    assert build_exception_components(working)["component_count"] == 0


def test_probe_05_b1_discovers_entity_absent_from_attention() -> None:
    working = _working()
    request = _b1_request(working)
    response = _empty_delta()
    response["new_entities"] = [_new_entity("Arden", "ch01_b001")]
    record = working.apply_b1_response(request=request, response=response)
    assert len(record["entity_ids"]) == 1


def test_probe_06_attention_for_other_block_is_not_packaged() -> None:
    attention = _orientation(
        attention=[
            {
                "surface": "Aster bloom",
                "source_block_ids": ["ch01_b004"],
                "why_noticed": "It may affect translation.",
            }
        ]
    )
    request = _b1_request(_working(), active_ids=("ch01_b001",), orientation=attention)
    assert request.sections["advisory_attention_for_active_blocks"] == []


def test_probe_07_repeated_attention_is_one_packet() -> None:
    orientation = _orientation(
        attention=[
            {
                "surface": "silver hound",
                "source_block_ids": ["ch01_b001"],
                "why_noticed": "It affects a participant.",
            },
            {
                "surface": "silver hound",
                "source_block_ids": ["ch01_b005"],
                "why_noticed": "It recurs later.",
            },
        ]
    )
    packets = build_attention_packets(
        orientation["attention_ledger"],
        active_block_ids=["ch01_b001", "ch01_b005"],
        block_order=_order(),
        packet_cap=16,
    )
    assert len(packets) == 1
    assert packets[0]["source_block_ids"] == ["ch01_b001", "ch01_b005"]
    assert len(packets[0]["observations"]) == 2


def test_probe_08_repeated_surface_has_one_candidate_card() -> None:
    snapshot = _snapshot_with(entities=[_entity("ent_a", "Arden")])
    working = _working(snapshot)
    request = _b1_request(
        working, active_ids=("ch01_b001", "ch01_b002", "ch01_b003")
    )
    packets = request.sections["known_surface_hits"]
    arden = [row for row in packets if row["source_surface"].casefold() == "arden"]
    assert len(arden) == 1
    assert arden[0]["matched_block_ids"] == ["ch01_b001", "ch01_b002", "ch01_b003"]
    assert len(arden[0]["candidate_entity_cards"]) == 1


def test_probe_09_candidate_packet_is_prejoined() -> None:
    request = _b1_request(_working(_snapshot_with(entities=[_entity("ent_a", "Arden")])))
    assert "known_surface_hits" in request.sections
    assert "candidate_links" not in request.sections
    packet = request.sections["known_surface_hits"][0]
    assert {"source_surface", "matched_block_ids", "candidate_entity_cards"} <= set(packet)


def test_probe_10_one_candidate_is_not_authoritative() -> None:
    request = _b1_request(_working(_snapshot_with(entities=[_entity("ent_a", "Arden")])))
    packet = request.sections["known_surface_hits"][0]
    assert len(packet["candidate_entity_cards"]) == 1
    assert not {"chosen_entity_id", "authoritative_binding", "confidence"} & set(packet)


def test_probe_11_same_surface_returns_two_deterministic_cards() -> None:
    snapshot = _snapshot_with(
        entities=[_entity("ent_b", "Arden"), _entity("ent_a", "Arden")]
    )
    first = _b1_request(_working(snapshot)).sections["known_surface_hits"]
    second = _b1_request(_working(snapshot)).sections["known_surface_hits"]
    cards = first[0]["candidate_entity_cards"]
    assert [row["entity_id"] for row in cards] == ["ent_a", "ent_b"]
    assert first == second


def test_probe_12_existing_compatible_entity_needs_no_output() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    record = working.apply_b1_response(request=_b1_request(working), response=_empty_delta())
    assert record["entity_ids"] == []
    assert record["glossary_ids"] == []
    assert record["alias_ids"] == []
    assert record["local_reference_ids"] == []
    assert record["ticket_ids"] == []
    assert len(working.snapshot()["entities"]) == 1


def test_probe_13_fresh_stable_name_is_clean_provisional_then_confirmed() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [_new_entity("Arden", "ch01_b001")]
    working.apply_b1_response(request=_b1_request(working), response=response)
    assert working.snapshot()["entities"][0]["status"] == "provisional"
    _finalize_without_audit(working)
    assert working.snapshot()["entities"][0]["status"] == "confirmed"


def test_probe_14_same_name_conflict_requires_distinct_row_and_ticket() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "Arden",
            "ch01_b001",
            identity_summary="A distinct stable person introduced with the same source name.",
        )
    ]
    response["tickets"] = [
        _ticket(
            "same_name_collision",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a"],
            kind="person",
        )
    ]
    record = working.apply_b1_response(request=_b1_request(working), response=response)
    assert len(record["entity_ids"]) == 1
    assert any(row["ticket_type"] == "same_name_collision" for row in working.snapshot()["tickets"])


def test_probe_15_stable_alternate_name_can_propose_global_alias() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["surface_updates"] = [
        {
            "update_kind": "global_name_alias",
            "surface": "Mr. Arden",
            "target_entity_id": "ent_a",
            "name_class": "title_plus_name",
            "source_block_ids": ["ch01_b001"],
            "reason": "A stable title-plus-name form is explicit.",
        }
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    assert working.snapshot()["global_aliases"][0]["entity_id"] == "ent_a"


def test_probe_16_foreign_alias_target_is_fatal() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["surface_updates"] = [
        {
            "update_kind": "global_name_alias",
            "surface": "Mr. Arden",
            "target_entity_id": "ent_foreign",
            "name_class": "title_plus_name",
            "source_block_ids": ["ch01_b001"],
            "reason": "A proposed name.",
        }
    ]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=_b1_request(working), response=response)


def test_probe_17_contextual_expression_cannot_be_global_alias() -> None:
    gate = route_alias_for_commit(
        surface="madam",
        name_class="stable_nickname",
        target_entity_id="ent_a",
        source_block_ids=["ch01_b002"],
        source_catalog=_catalog(),
        source_decision_lineage={"test": 17},
    )
    assert gate["outcome"] != "eligible_global_alias"


def test_probe_18_contextual_expression_can_be_block_local() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["surface_updates"] = [
        {
            "update_kind": "block_local_reference",
            "surface": "madam",
            "target_entity_id": "ent_a",
            "name_class": None,
            "source_block_ids": ["ch01_b002"],
            "reason": "The local context supports this candidate link.",
        }
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    _finalize_without_audit(working)
    row = working.snapshot()["block_local_references"][0]
    assert row["valid_block_ids"] == ["ch01_b002"]


def test_probe_19_local_reference_does_not_inherit_to_another_block() -> None:
    local = {
        "local_reference_id": "local_a",
        "surface": "madam",
        "entity_id": "ent_a",
        "valid_block_ids": ["ch01_b002"],
        "created_by_request_fingerprint": "seed",
        "source_text_manifest_hash": "seed",
        "status": "confirmed",
        "revision_hash": "seed",
    }
    snapshot = _snapshot_with(entities=[_entity("ent_a", "Arden")], locals_=[local])
    manifest = build_b2_candidate_manifest(
        chapter_id="ch01",
        active_blocks=[_chapter()["blocks"][3]],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    assert not any(row["candidate_source"] == "block_local_reference" for row in manifest["candidate_links"])


def test_probe_20_important_unnamed_entity_has_zero_global_aliases() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "silver hound",
            "ch01_b001",
            name_class=None,
            referent_kind_claim="animal",
            identity_summary="An individualized animal that affects a named participant.",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    _finalize_without_audit(working)
    assert working.snapshot()["entities"][0]["name_class"] is None
    assert working.snapshot()["global_aliases"] == []


def _surface_ticket_case() -> tuple[ChapterWorkingRegistryV4, Any]:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["tickets"] = [
        _ticket(
            "surface_class_review",
            "madam",
            "ch01_b002",
            candidate_entity_ids=["ent_a"],
            kind="person",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    requests = render_auditor_requests(
        working=working,
        orientation=_orientation(),
        source_catalog=_catalog(),
        block_order=_order(),
        design_doc=DESIGN_DOC,
        run_config=_config(),
    )
    return working, requests[0]


def _remain_pending_response(request: Any) -> dict[str, Any]:
    ticket_id = request.sections["owned_tickets"][0]["ticket_id"]
    return {
        "ticket_dispositions": [
            {
                "ticket_id": ticket_id,
                "action": "remain_pending",
                "source_entity_id": None,
                "target_entity_id": None,
                "source_glossary_id": None,
                "target_glossary_id": None,
                "resolved_referent_kind": None,
                "name_class": None,
                "valid_block_ids": [],
                "resolution_note": "Evidence is insufficient for a stable decision.",
            }
        ],
        "profile_revisions": [],
    }


def _clean_generation():
    chapter = _chapter()
    config = _config()
    b0_request = render_b0_request(chapter=chapter, design_doc=DESIGN_DOC, run_config=config)
    orientation = _orientation()
    working = _working()
    working.install_attention_ledger(orientation["attention_ledger"])
    b1_request = _b1_request(working, orientation=orientation)
    working.apply_b1_response(request=b1_request, response=_empty_delta())
    _finalize_without_audit(working)
    generation = build_registry_generation(
        working=working,
        orientation=orientation,
        b0_request=b0_request,
        source_catalog=_catalog(),
        run_config=config,
        audit_decisions=[],
    )
    return generation


def test_probe_21_b2_retrieves_unnamed_by_support_and_local_channels() -> None:
    entity = _entity(
        "ent_hound",
        "silver hound",
        name_class=None,
        kind="animal",
        blocks=("ch01_b001",),
    )
    local = {
        "local_reference_id": "local_hound",
        "surface": "madam",
        "entity_id": "ent_hound",
        "valid_block_ids": ["ch01_b002"],
        "created_by_request_fingerprint": "seed",
        "source_text_manifest_hash": "seed",
        "status": "confirmed",
        "revision_hash": "seed",
    }
    snapshot = _snapshot_with(entities=[entity], locals_=[local])
    manifest = build_b2_candidate_manifest(
        chapter_id="ch01",
        active_blocks=[_chapter()["blocks"][1], _chapter()["blocks"][2]],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    channels = {row["candidate_source"] for row in manifest["candidate_links"]}
    assert {"support_block", "block_local_reference"} <= channels


def test_probe_22_b2_candidate_channels_are_non_authoritative() -> None:
    snapshot = _snapshot_with(
        entities=[
            _entity(
                "ent_hound",
                "silver hound",
                name_class=None,
                kind="animal",
                blocks=("ch01_b001",),
            )
        ]
    )
    manifest = build_b2_candidate_manifest(
        chapter_id="ch01",
        active_blocks=[_chapter()["blocks"][1]],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    assert manifest["authoritative_bindings"] == []
    assert all(row["authoritative"] is False for row in manifest["candidate_links"])


def test_probe_23_temporary_profile_content_is_rejected() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity("Arden", "ch01_b001", identity_summary="mood: briefly unhappy")
    ]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=_b1_request(working), response=response)


def test_probe_24_profile_enrichment_is_a_ticket_not_direct_rewrite() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["tickets"] = [
        _ticket(
            "profile_enrichment",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a"],
            summary="A stable office-holder identified by the source.",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    entity = working.snapshot()["entities"][0]
    assert entity["identity_summary"].startswith("A stable person")
    assert working.snapshot()["tickets"][0]["proposed_identity_summary"]


def test_probe_25_named_place_is_entity_and_common_noun_is_not_auto_glossary() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "North Hall",
            "ch01_b001",
            referent_kind_claim="place",
            identity_summary="A named place used as a stable setting.",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    assert working.snapshot()["entities"][0]["referent_kind"] == "place"
    assert working.snapshot()["glossary_items"] == []


def test_probe_26_same_surface_cannot_be_entity_and_glossary() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "North Hall",
            "ch01_b001",
            referent_kind_claim="place",
            identity_summary="A named place.",
        )
    ]
    response["new_glossary_items"] = [
        {
            "surface": "North Hall",
            "category_claim": "place_name",
            "short_description": "A translation-sensitive place name.",
            "source_block_ids": ["ch01_b001"],
        }
    ]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=_b1_request(working), response=response)


def test_probe_27_unlocatable_surface_becomes_code_ticket_without_row() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [_new_entity("Absent Name", "ch01_b001")]
    record = working.apply_b1_response(request=_b1_request(working), response=response)
    assert record["entity_ids"] == []
    assert working.snapshot()["tickets"][0]["ticket_type"] == "unlocatable_surface"


def test_probe_28_candidate_overflow_blocks_authoritative_update() -> None:
    snapshot = _snapshot_with(
        entities=[_entity("ent_a", "Arden"), _entity("ent_b", "Arden")]
    )
    working = _working(snapshot)
    request = _b1_request(
        working, config=_config(candidate_cards_total_cap_per_window=1)
    )
    assert request.sections["known_surface_hits"][0]["candidate_overflow"] is True
    supplied = request.sections["known_surface_hits"][0]["candidate_entity_cards"][0]["entity_id"]
    response = _empty_delta()
    response["surface_updates"] = [
        {
            "update_kind": "global_name_alias",
            "surface": "Arden",
            "target_entity_id": supplied,
            "name_class": "proper_name",
            "source_block_ids": ["ch01_b001"],
            "reason": "A proposed stable name.",
        }
    ]
    working.apply_b1_response(request=request, response=response)
    assert working.snapshot()["global_aliases"] == []
    assert any(row["ticket_type"] == "candidate_overflow" for row in working.snapshot()["tickets"])


def test_probe_29_stale_working_parent_is_fatal() -> None:
    working = _working()
    stale_request = _b1_request(working)
    current_request = _b1_request(working)
    working.apply_b1_response(request=current_request, response=_empty_delta())
    with pytest.raises(RegistryStaleRevisionError):
        working.apply_b1_response(
            request=replace(stale_request, request_fingerprint="different-request"),
            response=_empty_delta(),
        )


def test_probe_30_same_request_same_response_is_idempotent() -> None:
    working = _working()
    request = _b1_request(working)
    first = working.apply_b1_response(request=request, response=_empty_delta())
    first_snapshot = working.snapshot()
    second = working.apply_b1_response(request=request, response=_empty_delta())
    assert first == second
    assert working.snapshot() == first_snapshot


def test_probe_31_same_request_different_response_is_fatal() -> None:
    working = _working()
    request = _b1_request(working)
    working.apply_b1_response(request=request, response=_empty_delta())
    changed = _empty_delta()
    changed["new_entities"] = [_new_entity("Arden", "ch01_b001")]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=request, response=changed)


def test_probe_32_auditor_must_exact_cover_tickets_once() -> None:
    _, request = _surface_ticket_case()
    good = _remain_pending_response(request)
    validate_audit_response(request, good)
    bad = _remain_pending_response(request)
    bad["ticket_dispositions"] = []
    with pytest.raises(RegistryContractError):
        validate_audit_response(request, bad)


def test_probe_33_auditor_request_excludes_attention_inventory() -> None:
    _, request = _surface_ticket_case()
    encoded = str(request.sections).casefold()
    assert "attention_ledger" not in encoded
    assert "why_noticed" not in encoded


def test_probe_34_generation_rejects_alias_without_unified_gate_record() -> None:
    generation = _clean_generation()
    assert generation.generation_id.startswith("reggen4_")
    working = _working(
        _snapshot_with(
            entities=[_entity("ent_a", "Arden")],
            aliases=[
                {
                    "alias_id": "alias_bad",
                    "surface": "Mr. Arden",
                    "name_class": "title_plus_name",
                    "entity_id": "ent_a",
                    "support_block_ids": ["ch01_b001"],
                    "created_by_request_fingerprint": "seed",
                    "source_text_manifest_hash": "seed",
                    "status": "confirmed",
                    "gate_outcome": "eligible_global_alias",
                    "gate_record_hash": "missing",
                    "revision_hash": "seed",
                }
            ],
        )
    )
    b0 = render_b0_request(chapter=_chapter(), design_doc=DESIGN_DOC, run_config=_config())
    with pytest.raises(RegistryContractError):
        build_registry_generation(
            working=working,
            orientation=_orientation(),
            b0_request=b0,
            source_catalog=_catalog(),
            run_config=_config(),
            audit_decisions=[],
        )


def test_probe_35_crash_before_pointer_switch_keeps_prior_generation(tmp_path: Path) -> None:
    store = ChapterRegistryStoreV4(tmp_path / "store")
    generation = _clean_generation()

    def crash() -> None:
        raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        store.commit(generation, expected_parent=None, before_pointer_switch=crash)
    assert store.current_generation_id("lineage-test") is None


def test_probe_36_runtime_prompts_are_book_neutral() -> None:
    text = "\n".join(load_registry_prompt_v4(DESIGN_DOC, role) for role in PROMPT_IDS)
    for fixture_surface in ("Arden", "North Hall", "silver hound"):
        assert fixture_surface not in text


def test_probe_37_v3_path_still_imports_and_keeps_its_identity() -> None:
    from pipeline.literary.chapter_registry_v3 import empty_registry_snapshot_v3

    assert empty_registry_snapshot_v3("lineage-test")["schema_version"] == "chapter_registry_v3"


def test_probe_38_frozen_database_hash_is_unchanged() -> None:
    assert FROZEN_DB.is_file()
    assert file_sha256(FROZEN_DB).upper() == FROZEN_SHA256


def test_probe_39_nested_local_update_attaches_after_code_mints_entity_id() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "silver hound",
            "ch01_b001",
            name_class=None,
            referent_kind_claim="animal",
            identity_summary="An individualized animal important to the chapter.",
            initial_surface_updates=[
                {
                    "update_kind": "block_local_reference",
                    "surface": "silver hound",
                    "name_class": None,
                    "source_block_ids": ["ch01_b001"],
                    "reason": "A local descriptive reference.",
                }
            ],
        )
    ]
    record = working.apply_b1_response(request=_b1_request(working), response=response)
    entity_id = record["entity_ids"][0]
    assert working.snapshot()["block_local_references"][0]["entity_id"] == entity_id


def test_probe_40_same_surface_new_rows_keep_nested_updates_separate() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "Arden",
            "ch01_b001",
            identity_summary="The first distinct stable referent.",
            initial_surface_updates=[
                {
                    "update_kind": "block_local_reference",
                    "surface": "Mr. Arden",
                    "name_class": None,
                    "source_block_ids": ["ch01_b001"],
                    "reason": "Local evidence for the first referent.",
                }
            ],
        ),
        _new_entity(
            "Arden",
            "ch01_b002",
            identity_summary="The second distinct stable referent.",
            initial_surface_updates=[
                {
                    "update_kind": "block_local_reference",
                    "surface": "Arden",
                    "name_class": None,
                    "source_block_ids": ["ch01_b002"],
                    "reason": "Local evidence for the second referent.",
                }
            ],
        ),
    ]
    record = working.apply_b1_response(request=_b1_request(working), response=response)
    assert len(set(record["entity_ids"])) == 2
    assert {row["entity_id"] for row in working.snapshot()["block_local_references"]} == set(
        record["entity_ids"]
    )


def _two_profile_ticket_case() -> tuple[ChapterWorkingRegistryV4, Any]:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["tickets"] = [
        _ticket(
            "profile_enrichment",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a"],
            summary="A stable office-holder identified by the source.",
        ),
        _ticket(
            "profile_enrichment",
            "Arden",
            "ch01_b002",
            candidate_entity_ids=["ent_a"],
            gender="masculine",
        ),
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    requests = render_auditor_requests(
        working=working,
        orientation=_orientation(),
        source_catalog=_catalog(),
        block_order=_order(),
        design_doc=DESIGN_DOC,
        run_config=_config(),
    )
    assert len(requests) == 1
    return working, requests[0]


def _profile_audit_response(request: Any, *, reject_second: bool = False) -> dict[str, Any]:
    tickets = request.sections["owned_tickets"]
    dispositions = []
    revise_ids = []
    for ticket in tickets:
        action = (
            "reject_noise"
            if reject_second and ticket["proposed_referential_gender"] is not None
            else "revise_profile"
        )
        if action == "revise_profile":
            revise_ids.append(ticket["ticket_id"])
        dispositions.append(
            {
                "ticket_id": ticket["ticket_id"],
                "action": action,
                "source_entity_id": None,
                "target_entity_id": "ent_a" if action == "revise_profile" else None,
                "source_glossary_id": None,
                "target_glossary_id": None,
                "resolved_referent_kind": None,
                "name_class": None,
                "valid_block_ids": [],
                "resolution_note": "The source-grounded proposal was adjudicated once.",
            }
        )
    revision = {
        "target_entity_id": "ent_a",
        "source_ticket_ids": revise_ids,
        "referent_kind_update": None,
        "identity_summary_update": "A stable office-holder identified by the source.",
        "referential_gender_update": None if reject_second else "masculine",
        "resolution_note": "Accepted stable fields are consolidated.",
    }
    return {"ticket_dispositions": dispositions, "profile_revisions": [revision]}


def test_probe_41_unnamed_canonical_surface_is_not_global_lexical_key() -> None:
    snapshot = _snapshot_with(
        entities=[
            _entity(
                "ent_hound",
                "silver hound",
                name_class=None,
                kind="animal",
                blocks=("ch01_b001",),
            )
        ]
    )
    request = _b1_request(_working(snapshot), active_ids=("ch01_b001",))
    assert request.sections["known_surface_hits"] == []


def test_probe_42_local_reference_only_matches_declared_blocks() -> None:
    entity = _entity(
        "ent_hound", "silver hound", name_class=None, kind="animal"
    )
    local = {
        "local_reference_id": "local_hound",
        "surface": "madam",
        "entity_id": "ent_hound",
        "valid_block_ids": ["ch01_b002"],
        "created_by_request_fingerprint": "seed",
        "source_text_manifest_hash": "seed",
        "status": "confirmed",
        "revision_hash": "seed",
    }
    snapshot = _snapshot_with(entities=[entity], locals_=[local])
    in_scope = _b1_request(_working(snapshot), active_ids=("ch01_b002",))
    out_scope = _b1_request(_working(snapshot), active_ids=("ch01_b003",))
    assert len(in_scope.sections["known_surface_hits"]) == 1
    assert out_scope.sections["known_surface_hits"] == []


def test_probe_43_ambiguous_same_surface_ticket_does_not_choose_subject() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity("Arden", "ch01_b001", identity_summary="The first stable referent."),
        _new_entity("Arden", "ch01_b002", identity_summary="The second stable referent."),
    ]
    response["tickets"] = [
        _ticket("same_name_collision", "Arden", "ch01_b001", kind="person")
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    ambiguous = [
        row
        for row in working.snapshot()["tickets"]
        if row["ticket_type"] == "ambiguous_new_subject"
    ]
    assert ambiguous
    assert any(len(row["subject_entity_ids"]) != 1 for row in ambiguous)


def test_probe_44_b1_request_excludes_prior_ticket_narratives() -> None:
    pending_ticket = {
        "ticket_id": "tick_prior",
        "ticket_type": "possible_alias",
        "surface": "Arden",
        "source_block_ids": ["ch01_b001"],
        "subject_entity_ids": [],
        "subject_glossary_ids": [],
        "candidate_entity_ids": ["ent_a"],
        "candidate_glossary_ids": [],
        "referent_kind_claim": None,
        "proposed_identity_summary": None,
        "proposed_referential_gender": None,
        "reason": "PRIVATE PRIOR DISPUTE BODY",
        "status": "carried",
        "opened_by_request_fingerprint": "seed",
        "source_text_manifest_hash": "seed",
        "resolution_action": "remain_pending",
        "resolution_note": "seed",
        "revision_hash": "seed",
    }
    snapshot = _snapshot_with(
        entities=[_entity("ent_a", "Arden")], tickets=[pending_ticket]
    )
    request = _b1_request(_working(snapshot))
    assert "PRIVATE PRIOR DISPUTE BODY" not in str(request.sections)


def test_probe_45_auditor_cites_each_owned_block_once_in_author_order() -> None:
    _, request = _two_profile_ticket_case()
    blocks = request.sections["cited_source_blocks"]
    assert [row["block_id"] for row in blocks] == ["ch01_b001", "ch01_b002"]
    assert len(blocks) == len({row["block_id"] for row in blocks})


def test_probe_46_auditor_never_truncates_cited_evidence_to_fit_cap() -> None:
    working, _ = _surface_ticket_case()
    with pytest.raises(RegistryBudgetError):
        render_auditor_requests(
            working=working,
            orientation=_orientation(),
            source_catalog=_catalog(),
            block_order=_order(),
            design_doc=DESIGN_DOC,
            run_config=_config(auditor_input_token_cap=1),
        )


def test_probe_47_b0_chapter_over_cap_halts_without_sharding() -> None:
    with pytest.raises(RegistryBudgetError):
        render_b0_request(
            chapter=_chapter(), design_doc=DESIGN_DOC, run_config=_config(b0_input_token_cap=1)
        )


def test_probe_48_orientation_event_prose_creates_no_registry_row() -> None:
    orientation = _orientation(
        draft=(
            "An unnamed animal materially affects a participant during a major observable event, "
            "which is orientation only."
        )
    )
    assert "entities" not in orientation
    assert "referent_kind_claim" not in orientation


def test_probe_49_b1_can_create_unnamed_entity_when_b0_omitted_it() -> None:
    working = _working()
    orientation = _orientation(attention=[])
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "silver hound",
            "ch01_b001",
            name_class=None,
            referent_kind_claim="animal",
            identity_summary="An individualized animal that affects a participant.",
        )
    ]
    working.apply_b1_response(
        request=_b1_request(working, orientation=orientation), response=response
    )
    assert working.snapshot()["entities"][0]["canonical_surface"] == "silver hound"


def test_probe_50_b0_rejects_long_or_typed_orientation_answer() -> None:
    with pytest.raises(RegistryContractError):
        _orientation(draft="word " * 221)
    with pytest.raises(RegistryContractError):
        _orientation(draft="The typed answer is entity_id: ent_example.")


def test_probe_51_known_surface_scan_uses_active_blocks_only() -> None:
    snapshot = _snapshot_with(entities=[_entity("ent_aster", "Aster bloom")])
    request = _b1_request(
        _working(snapshot), active_ids=("ch01_b001",), tail_ids=("ch01_b004",)
    )
    assert all(row["source_surface"] != "Aster bloom" for row in request.sections["known_surface_hits"])


def test_probe_52_title_plus_name_derives_retrieval_only_base() -> None:
    snapshot = _snapshot_with(
        entities=[
            _entity("ent_a", "Mr. Arden", name_class="title_plus_name")
        ]
    )
    request = _b1_request(_working(snapshot), active_ids=("ch01_b001",))
    arden_packet = next(
        row for row in request.sections["known_surface_hits"] if row["source_surface"] == "Arden"
    )
    evidence = arden_packet["candidate_entity_cards"][0]["retrieval_evidence"]
    assert "title_base" in evidence["match_kinds"]
    assert _working(snapshot).snapshot()["global_aliases"] == []


def test_probe_53_stored_bare_name_matches_title_plus_name_source() -> None:
    snapshot = _snapshot_with(entities=[_entity("ent_a", "Arden")])
    request = _b1_request(_working(snapshot), active_ids=("ch01_b001",))
    packets = request.sections["known_surface_hits"]
    assert any(
        card["entity_id"] == "ent_a"
        for packet in packets
        for card in packet["candidate_entity_cards"]
    )


def test_probe_54_bare_name_returns_both_title_variants() -> None:
    snapshot = _snapshot_with(
        entities=[
            _entity("ent_mr", "Mr. Arden", name_class="title_plus_name"),
            _entity("ent_mrs", "Mrs. Arden", name_class="title_plus_name"),
        ]
    )
    request = _b1_request(_working(snapshot), active_ids=("ch01_b001",))
    packet = next(
        row for row in request.sections["known_surface_hits"] if row["source_surface"] == "Arden"
    )
    assert {row["entity_id"] for row in packet["candidate_entity_cards"]} == {
        "ent_mr",
        "ent_mrs",
    }


def test_probe_55_generic_token_overlap_is_disabled() -> None:
    snapshot = _snapshot_with(
        entities=[
            _entity("ent_n", "North Warden"),
            _entity("ent_s", "South Warden"),
        ]
    )
    selection = select_candidate_packets(
        snapshot=snapshot,
        working_revision_hash="work-test",
        active_blocks=[
            {
                "block_id": "ch01_b999",
                "order_index": 999,
                "block_type": "paragraph",
                "clean_text": "Warden waited.",
            }
        ],
        context_only_tail=[],
        block_order={"ch01_b999": 999},
        recency_distance=0,
        card_count_cap=8,
        card_token_cap=2000,
        packet_count_cap=8,
    )
    assert selection["known_surface_hits"] == []


def test_probe_56_code_does_not_persist_or_promote_title_base() -> None:
    snapshot = _snapshot_with(
        entities=[_entity("ent_a", "Mr. Arden", name_class="title_plus_name")]
    )
    before = canonical_hash(snapshot)
    _b1_request(_working(snapshot), active_ids=("ch01_b001",))
    assert canonical_hash(snapshot) == before
    assert snapshot["global_aliases"] == []


def test_probe_57_each_candidate_card_owns_its_retrieval_evidence() -> None:
    snapshot = _snapshot_with(
        entities=[
            _entity("ent_mr", "Mr. Arden", name_class="title_plus_name"),
            _entity("ent_mrs", "Mrs. Arden", name_class="title_plus_name"),
        ]
    )
    packet = next(
        row
        for row in _b1_request(_working(snapshot), active_ids=("ch01_b001",)).sections[
            "known_surface_hits"
        ]
        if row["source_surface"] == "Arden"
    )
    for card in packet["candidate_entity_cards"]:
        assert card["retrieval_evidence"]["matched_registry_surfaces"]
        assert "retrieval_evidence" in card


def test_probe_58_gender_claim_keeps_exact_facet_support() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "Arden",
            "ch01_b001",
            referential_gender_claim={
                "value": "masculine",
                "support_block_ids": ["ch01_b001"],
            },
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    entity = working.snapshot()["entities"][0]
    assert entity["referential_gender"] == "masculine"
    assert entity["referential_gender_support_block_ids"] == ["ch01_b001"]


def test_probe_58a_omitted_optional_gender_normalizes_to_absence() -> None:
    schema = response_json_schema("b1")
    entity_schema = schema["properties"]["new_entities"]["items"]
    assert "referential_gender_claim" in entity_schema["properties"]
    assert "referential_gender_claim" not in entity_schema["required"]

    working = _working()
    response = _empty_delta()
    entity = _new_entity(
        "Arden",
        "ch01_b001",
        name_class="proper_name",
        referent_kind_claim="place",
        identity_summary="A named residence.",
    )
    entity.pop("referential_gender_claim")
    response["new_entities"] = [entity]

    working.apply_b1_response(request=_b1_request(working), response=response)
    stored = working.snapshot()["entities"][0]
    assert stored["referential_gender"] is None
    assert stored["referential_gender_support_block_ids"] == []


def test_probe_59_invalid_or_tail_only_gender_support_cannot_apply() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "Arden",
            "ch01_b001",
            referential_gender_claim={
                "value": "feminine",
                "support_block_ids": ["ch01_b003"],
            },
        )
    ]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=_b1_request(working), response=response)


def test_probe_60_missing_profile_evidence_creates_no_conflict() -> None:
    working = _working(
        _snapshot_with(entities=[_entity("ent_a", "Arden", gender="masculine")])
    )
    record = working.apply_b1_response(request=_b1_request(working), response=_empty_delta())
    assert record["ticket_ids"] == []
    assert working.snapshot()["entities"][0]["referential_gender"] == "masculine"


def test_probe_61_incompatible_gender_uses_ticket_and_cannot_overwrite() -> None:
    working = _working(
        _snapshot_with(entities=[_entity("ent_a", "Arden", gender="masculine")])
    )
    response = _empty_delta()
    response["tickets"] = [
        _ticket(
            "profile_conflict",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a"],
            gender="feminine",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    assert working.snapshot()["entities"][0]["referential_gender"] == "masculine"


def test_probe_62_distinct_same_surface_uses_collision_not_profile_overwrite() -> None:
    working = _working(
        _snapshot_with(entities=[_entity("ent_a", "Arden", gender="masculine")])
    )
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "Arden",
            "ch01_b001",
            identity_summary="A distinct stable person with the same name.",
            referential_gender_claim={
                "value": "feminine",
                "support_block_ids": ["ch01_b001"],
            },
        )
    ]
    response["tickets"] = [
        _ticket(
            "same_name_collision",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a"],
            kind="person",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    entities = working.snapshot()["entities"]
    assert len(entities) == 2
    assert next(row for row in entities if row["entity_id"] == "ent_a")["referential_gender"] == "masculine"


def test_probe_63_profile_tickets_group_once_per_entity() -> None:
    working, request = _two_profile_ticket_case()
    assert build_exception_components(working)["component_count"] == 1
    response = _profile_audit_response(request)
    validated = validate_audit_response(request, response)
    assert len(validated["profile_revisions"]) == 1


def test_probe_64_rejected_profile_ticket_does_not_contribute_support() -> None:
    working, request = _two_profile_ticket_case()
    response = _profile_audit_response(request, reject_second=True)
    apply_audit_responses(
        working=working,
        requests=[request],
        responses=[response],
        source_catalog=_catalog(),
    )
    revision = working.snapshot()["profile_revisions"][0]
    assert revision["identity_summary_support_block_ids"] == ["ch01_b001"]
    assert revision["referential_gender_support_block_ids"] == []


def test_probe_65_profile_patch_applies_once_with_field_specific_support() -> None:
    working, request = _two_profile_ticket_case()
    response = _profile_audit_response(request)
    apply_audit_responses(
        working=working,
        requests=[request],
        responses=[response],
        source_catalog=_catalog(),
    )
    snapshot = working.snapshot()
    assert len(snapshot["profile_revisions"]) == 1
    revision = snapshot["profile_revisions"][0]
    assert revision["identity_summary_support_block_ids"] == ["ch01_b001"]
    assert revision["referential_gender_support_block_ids"] == ["ch01_b002"]
    assert snapshot["entities"][0]["latest_profile_revision_id"] == revision["profile_revision_id"]


def test_probe_66_neutral_is_explicit_while_null_is_unknown() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity("Arden", "ch01_b001"),
        _new_entity(
            "silver hound",
            "ch01_b001",
            name_class=None,
            referent_kind_claim="animal",
            identity_summary="An individualized animal.",
            referential_gender_claim={
                "value": "neutral",
                "support_block_ids": ["ch01_b001"],
            },
        ),
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    by_surface = {row["canonical_surface"]: row for row in working.snapshot()["entities"]}
    assert by_surface["Arden"]["referential_gender"] is None
    assert by_surface["silver hound"]["referential_gender"] == "neutral"


def test_probe_67_age_and_scene_state_are_outside_stable_profile() -> None:
    working = _working()
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity("Arden", "ch01_b001", identity_summary="age_band: young adult")
    ]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=_b1_request(working), response=response)


def test_probe_68_profile_ticket_with_multiple_candidates_is_rejected() -> None:
    snapshot = _snapshot_with(
        entities=[_entity("ent_a", "Arden"), _entity("ent_b", "Arden")]
    )
    working = _working(snapshot)
    response = _empty_delta()
    response["tickets"] = [
        _ticket(
            "profile_enrichment",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a", "ent_b"],
            summary="A proposed stable profile addition.",
        )
    ]
    with pytest.raises(RegistryContractError):
        working.apply_b1_response(request=_b1_request(working), response=response)


def test_hardening_rendered_candidate_context_respects_actual_token_cap() -> None:
    aliases = []
    for index, surface in enumerate(("Mr. Arden", "Arden Prime", "Arden North"), 1):
        aliases.append(
            {
                "alias_id": f"alias_{index}",
                "surface": surface,
                "name_class": "title_plus_name" if index == 1 else "proper_name",
                "entity_id": "ent_a",
                "support_block_ids": ["ch01_b001"],
                "created_by_request_fingerprint": "seed",
                "source_text_manifest_hash": "seed",
                "status": "confirmed",
                "gate_outcome": "eligible_global_alias",
                "gate_record_hash": f"gate_{index}",
                "revision_hash": f"revision_{index}",
            }
        )
    snapshot = _snapshot_with(entities=[_entity("ent_a", "Arden")], aliases=aliases)
    selection = select_candidate_packets(
        snapshot=snapshot,
        working_revision_hash="work-test",
        active_blocks=[_chapter()["blocks"][1]],
        context_only_tail=[],
        block_order=_order(),
        recency_distance=0,
        card_count_cap=8,
        card_token_cap=550,
        packet_count_cap=16,
    )
    assert selection["candidate_selection_manifest"]["selected_token_estimate"] <= 550


def test_hardening_auditor_cannot_confirm_candidate_as_new_subject() -> None:
    working = _working(_snapshot_with(entities=[_entity("ent_a", "Arden")]))
    response = _empty_delta()
    response["new_entities"] = [
        _new_entity(
            "Arden",
            "ch01_b001",
            identity_summary="A distinct stable person with the same name.",
        )
    ]
    response["tickets"] = [
        _ticket(
            "same_name_collision",
            "Arden",
            "ch01_b001",
            candidate_entity_ids=["ent_a"],
            kind="person",
        )
    ]
    working.apply_b1_response(request=_b1_request(working), response=response)
    request = render_auditor_requests(
        working=working,
        orientation=_orientation(),
        source_catalog=_catalog(),
        block_order=_order(),
        design_doc=DESIGN_DOC,
        run_config=_config(),
    )[0]
    ticket_id = request.sections["owned_tickets"][0]["ticket_id"]
    invalid = {
        "ticket_dispositions": [
            {
                "ticket_id": ticket_id,
                "action": "confirm_distinct_entity",
                "source_entity_id": "ent_a",
                "target_entity_id": None,
                "source_glossary_id": None,
                "target_glossary_id": None,
                "resolved_referent_kind": None,
                "name_class": None,
                "valid_block_ids": [],
                "resolution_note": "Invalidly treats a prior candidate as the new subject.",
            }
        ],
        "profile_revisions": [],
    }
    with pytest.raises(RegistryContractError):
        validate_audit_response(request, invalid)


def test_hardening_candidate_overflow_tickets_even_for_empty_model_delta() -> None:
    snapshot = _snapshot_with(
        entities=[_entity("ent_a", "Arden"), _entity("ent_b", "Arden")]
    )
    working = _working(snapshot)
    request = _b1_request(
        working, config=_config(candidate_cards_total_cap_per_window=1)
    )
    assert request.sections["candidate_selection_manifest"]["overflow_records"]

    working.apply_b1_response(request=request, response=_empty_delta())
    assert any(
        row["ticket_type"] == "candidate_overflow"
        for row in working.snapshot()["tickets"]
    )
