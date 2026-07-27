from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.chapter_registry_schema_v2 import (
    ALIAS_SCOPE_POLICY_VERSION,
    PROMPT_IDS,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RegistryContractError,
    RegistryStaleParentError,
    RegistryStaleRevisionError,
    RunConfigV2,
)
from pipeline.literary.chapter_registry_v2 import (
    ChapterRegistryStoreV2,
    ChapterWorkingRegistryV2,
    _exception_manifest_subset,
    build_b2_candidate_manifest,
    build_exception_manifest,
    build_registry_generation,
    build_registry_windows,
    chapter_source_manifest_hash,
    empty_registry_snapshot_v2,
    estimate_registry_prompt_tokens,
    render_auditor_request,
    render_auditor_requests,
    render_b0_request,
    render_b1_request,
    route_surface_for_commit,
    schedule_targeted_recall,
    select_candidate_cards,
    validate_audit_decision,
    validate_orientation_response,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _config(**overrides: Any) -> RunConfigV2:
    base = RunConfigV2(
        b0_model_id="gpt-5.4",
        b0_reasoning_effort="none",
        b0_temperature=1.0,
        b0_seed=20260612,
        b0_verbosity="low",
        b0_output_cap=2048,
        b1_model_id="gpt-5.4-mini",
        b1_reasoning_effort="none",
        b1_temperature=1.0,
        b1_seed=20260612,
        b1_verbosity="low",
        b1_output_cap=2048,
        auditor_model_id="gpt-5.4",
        auditor_reasoning_effort="none",
        auditor_temperature=1.0,
        auditor_seed=20260612,
        auditor_verbosity="low",
        auditor_output_cap=4096,
        b1_window_target_tokens=80,
        b1_window_max_blocks=2,
        context_only_tail_k=1,
        recency_k=8,
        candidate_card_count_cap=16,
        candidate_card_token_cap=8000,
        targeted_recall_call_cap=4,
        auditor_component_cap=8,
        auditor_input_token_cap=20000,
        auditor_exception_share_cap=1.0,
        b0_input_cap=20000,
        b1_input_cap=20000,
        pricing_usd_per_million={
            "b0": {"input": None, "cached_input": None, "output": None},
            "b1": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
            "auditor": {"input": None, "cached_input": None, "output": None},
        },
        quota_gates={
            "gpt-primary": {
                "quota_bucket_id": "openai-primary",
                "model_id": "gpt-5.4",
                "rpm": 5,
                "tpm": 225000,
                "rpd": 500,
                "internal_utc_day_token_cap": 225000,
            },
            "mini-primary": {
                "quota_bucket_id": "openai-primary",
                "model_id": "gpt-5.4-mini",
                "rpm": 15,
                "tpm": 250000,
                "rpd": 500,
                "internal_utc_day_token_cap": 2250000,
            },
        },
        role_quota_gate_ids={
            "b0": ("gpt-primary",),
            "b1": ("mini-primary",),
            "auditor": ("gpt-primary",),
        },
        prompt_versions=PROMPT_IDS,
        schema_versions={"registry": REGISTRY_SCHEMA_VERSION},
        validator_version="chapter_registry_validator_v2_2",
        policy_versions={
            "candidate_selection": "registry_candidate_selection_v3_prejoined",
            "clean_commit": "clean_commit_eligibility_v1",
            "b2_rescan": "b2_candidate_rescan_v1",
            "alias_scope": ALIAS_SCOPE_POLICY_VERSION,
        },
    )
    return replace(base, **overrides)


@pytest.fixture
def chapter() -> dict[str, Any]:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "b001",
                "order_index": 1,
                "block_type": "heading",
                "clean_text": "Chapter One",
            },
            {
                "block_id": "b002",
                "order_index": 2,
                "block_type": "paragraph",
                "clean_text": "Mr Heathcliff met Mrs Heathcliff. The master called Pilot.",
            },
            {
                "block_id": "b003",
                "order_index": 3,
                "block_type": "paragraph",
                "clean_text": "Pilot, a dog, followed Heathcliff to Thrushcross Grange.",
            },
            {
                "block_id": "b004",
                "order_index": 4,
                "block_type": "paragraph",
                "clean_text": "The master returned. Catherine Linton arrived.",
            },
            {
                "block_id": "b005",
                "order_index": 5,
                "block_type": "paragraph",
                "clean_text": "He watched the parlour door.",
            },
        ],
    }


def _entity(
    entity_id: str,
    surface: str,
    *,
    support: str = "b001",
    kind: str = "person",
    status: str = "confirmed",
) -> dict[str, Any]:
    body = {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "referent_kind": kind,
        "identity_summary": f"The stable referent named {surface}.",
        "created_from_block_ids": [support],
        "support_block_ids": [support],
        "status": status,
    }
    return {**body, "revision_hash": canonical_hash(body)}


def _alias(alias_id: str, surface: str, entity_id: str, support: str = "b001") -> dict[str, Any]:
    body = {
        "alias_id": alias_id,
        "surface": surface,
        "alias_type": "name",
        "entity_id": entity_id,
        "support_block_ids": [support],
        "status": "confirmed",
    }
    return {**body, "revision_hash": canonical_hash(body)}


def _binding(
    binding_id: str, surface: str, entity_id: str, block_id: str
) -> dict[str, Any]:
    body = {
        "binding_id": binding_id,
        "surface": surface,
        "block_id": block_id,
        "target_ref": entity_id,
        "status": "confirmed",
        "support_block_ids": [block_id],
    }
    return {**body, "revision_hash": canonical_hash(body)}


def _alias_scope_chapter() -> dict[str, Any]:
    return {
        "chapter_id": "bk_scope",
        "blocks": [
            {
                "block_id": "b101",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr Alden greeted a female pointer.",
            },
            {
                "block_id": "b102",
                "order_index": 2,
                "block_type": "paragraph",
                "clean_text": (
                    "The visitor addressed the female pointer as honored guest and called "
                    "her a wretched cur."
                ),
            },
            {
                "block_id": "b103",
                "order_index": 3,
                "block_type": "paragraph",
                "clean_text": "The visitor entered. Alden followed.",
            },
            {
                "block_id": "b104",
                "order_index": 4,
                "block_type": "paragraph",
                "clean_text": "A harsh cry of mother echoed without naming anyone.",
            },
            {
                "block_id": "b105",
                "order_index": 5,
                "block_type": "paragraph",
                "clean_text": "Dr. Bela arrived before noon.",
            },
        ],
    }


def _snapshot(
    *,
    entities: list[dict[str, Any]] | None = None,
    aliases: list[dict[str, Any]] | None = None,
    glossary: list[dict[str, Any]] | None = None,
    bindings: list[dict[str, Any]] | None = None,
    tickets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = empty_registry_snapshot_v2("lineage-test")
    snapshot["entities"] = entities or []
    snapshot["aliases"] = aliases or []
    snapshot["glossary_items"] = glossary or []
    snapshot["local_bindings"] = bindings or []
    snapshot["tickets"] = tickets or []
    snapshot["snapshot_hash"] = canonical_hash(
        {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    )
    return snapshot


def _working(chapter: dict[str, Any], snapshot: dict[str, Any] | None = None) -> ChapterWorkingRegistryV2:
    return ChapterWorkingRegistryV2.create(
        state_lineage_id="lineage-test",
        chapter_id=str(chapter["chapter_id"]),
        source_manifest_hash=chapter_source_manifest_hash(chapter),
        parent_snapshot=snapshot or _snapshot(),
    )


def _request(
    chapter: dict[str, Any],
    working: ChapterWorkingRegistryV2,
    *,
    block_ids: tuple[str, ...] = ("b002",),
    config: RunConfigV2 | None = None,
    window_id: str = "w-test",
) -> Any:
    by_id = {str(row["block_id"]): row for row in chapter["blocks"]}
    return render_b1_request(
        chapter_id=str(chapter["chapter_id"]),
        window_id=window_id,
        b0_gist="A visitor encounters a guarded household.",
        active_blocks=[by_id[block_id] for block_id in block_ids],
        context_only_tail=[],
        working=working,
        block_order={str(row["block_id"]): int(row["order_index"]) for row in chapter["blocks"]},
        design_doc=DESIGN_DOC,
        run_config=config or _config(),
    )


def _empty_delta() -> dict[str, list[Any]]:
    return {
        "new_entities": [],
        "new_aliases": [],
        "new_glossary_items": [],
        "local_bindings": [],
        "tickets": [],
    }


def test_b1_total_input_cap_halts_before_execution(chapter: dict[str, Any]) -> None:
    working = _working(chapter)

    with pytest.raises(RegistryBudgetError, match="B1 input"):
        _request(chapter, working, config=_config(b1_input_cap=1))


def test_model_decoding_contract_changes_config_and_request_fingerprint(
    chapter: dict[str, Any]
) -> None:
    working = _working(chapter)
    base = _config()
    changed = replace(base, b1_seed=base.b1_seed + 1)

    assert base.config_hash != changed.config_hash
    assert _request(chapter, working, config=base).request_fingerprint != _request(
        chapter, working, config=changed
    ).request_fingerprint


def _entity_delta(
    surface: str = "Mr Heathcliff",
    *,
    block_id: str = "b002",
    kind: str = "person",
    aliases: list[dict[str, Any]] | None = None,
    tickets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = _empty_delta()
    result["new_entities"] = [
        {
            "surface": surface,
            "mention_type": "name",
            "referent_kind_claim": kind,
            "short_description": "A specifically named household member.",
            "created_from_block_id": block_id,
            "support_block_ids": [block_id],
            "initial_aliases": aliases or [],
        }
    ]
    result["tickets"] = tickets or []
    return result


def _audit_response(
    manifest: dict[str, Any],
    *,
    entity_action: str = "confirm",
    ticket_action: str = "resolve",
) -> dict[str, Any]:
    ids = manifest["exception_ids"]
    return {
        "entity_dispositions": [
            {
                "entity_id": entity_id,
                "action": entity_action,
                "merge_target_entity_id": None,
                "revised_identity_summary": None,
            }
            for entity_id in ids["entities"]
        ],
        "alias_dispositions": [
            {"alias_id": alias_id, "action": "confirm"} for alias_id in ids["aliases"]
        ],
        "glossary_dispositions": [
            {"glossary_id": glossary_id, "action": "confirm"}
            for glossary_id in ids["glossary_items"]
        ],
        "local_binding_dispositions": [
            {"binding_id": binding_id, "action": "confirm"}
            for binding_id in ids["local_bindings"]
        ],
        "ticket_dispositions": [
            {
                "ticket_id": ticket_id,
                "action": ticket_action,
                "resolution_note": "The bounded exception was reviewed.",
            }
            for ticket_id in ids["tickets"]
        ],
        "profile_revisions": [],
    }


def _clean_generation(
    chapter: dict[str, Any], working: ChapterWorkingRegistryV2
) -> Any:
    manifest = build_exception_manifest(working)
    assert not any(manifest["exception_ids"].values())
    return build_registry_generation(
        chapter=chapter,
        working=working,
        b0_request_fingerprint="b0-test",
        exception_manifest=manifest,
        audit_request_fingerprints=[],
        audit_decision=None,
    )


def test_probe_01_b0_is_orientation_only(chapter: dict[str, Any]) -> None:
    valid = {
        "gist": "A guarded visit unfolds.",
        "narrator_hypotheses": [{"surface": None, "note": "External narrator.", "block_ids": ["b002"]}],
        "salient_surface_checklist": [{"surface": "Pilot", "block_id": "b002"}],
    }
    parsed = validate_orientation_response(valid, chapter)
    assert set(parsed) == {
        "gist",
        "narrator_hypotheses",
        "salient_surface_checklist",
        "code_audit_rows",
    }
    with pytest.raises(RegistryContractError):
        validate_orientation_response({**valid, "scene_range": ["b002", "b004"]}, chapter)


def test_probe_02_ordinary_b1_sees_gist_not_checklist(chapter: dict[str, Any]) -> None:
    request = _request(chapter, _working(chapter))
    assert request.sections["b0_gist"]
    assert "salient_surface_checklist" not in request.sections
    assert "narrator_hypotheses" not in request.sections


def test_probe_03_b1_is_strictly_sequential(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    first = _request(chapter, working, window_id="w1")
    stale = _request(chapter, working, window_id="w2")
    working.apply_delta(first, _empty_delta())
    with pytest.raises(RegistryStaleRevisionError):
        working.apply_delta(stale, _empty_delta())


def test_probe_04_known_clean_entity_can_emit_empty_delta(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(entities=[_entity("ent2_h", "Mr Heathcliff")])
    working = _working(chapter, snapshot)
    before = len(working.snapshot()["entities"])
    working.apply_delta(_request(chapter, working), _empty_delta())
    assert len(working.snapshot()["entities"]) == before


def test_probe_05_identical_retry_is_idempotent(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    request = _request(chapter, working)
    first = working.apply_delta(request, _entity_delta())
    second = working.apply_delta(request, _entity_delta())
    assert first == second
    assert len(working.snapshot()["entities"]) == 1


def test_probe_06_descriptor_cannot_create_global_entity(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    response = _entity_delta("The master")
    response["new_entities"][0]["mention_type"] = "descriptor"
    with pytest.raises(RegistryContractError):
        working.apply_delta(_request(chapter, working), response)


def test_probe_07_pronoun_cannot_enter_global_or_local_tables(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    response = _entity_delta("He", block_id="b005")
    with pytest.raises(RegistryContractError):
        working.apply_delta(_request(chapter, working, block_ids=("b005",)), response)


def test_probe_08_same_surface_multimap_is_ticketed_not_forced_merged(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(
        entities=[_entity("ent2_mr", "Mr Heathcliff"), _entity("ent2_mrs", "Mrs Heathcliff")],
        aliases=[
            _alias("als2_a", "Heathcliff", "ent2_mr"),
            _alias("als2_b", "Heathcliff", "ent2_mrs"),
        ],
    )
    working = _working(chapter, snapshot)
    assert len([row for row in working.snapshot()["aliases"] if row["surface"] == "Heathcliff"]) == 2
    working.apply_delta(_request(chapter, working), _entity_delta("Mr Heathcliff"))
    manifest = build_exception_manifest(working)
    assert manifest["exception_ids"]["entities"]
    assert any(row["ticket_type"] in {"same_name_collision", "possible_duplicate"} for row in manifest["tickets"])


def test_probe_09_descriptor_can_bind_different_entities_per_block(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(
        entities=[_entity("ent2_a", "Mr Heathcliff"), _entity("ent2_b", "Mrs Heathcliff")]
    )
    working = _working(chapter, snapshot)
    for block_id, target, window_id in (("b002", "ent2_a", "w1"), ("b004", "ent2_b", "w2")):
        response = _empty_delta()
        response["local_bindings"] = [
            {
                "surface": "The master",
                "block_id": block_id,
                "target_entity_id": target,
                "support_block_ids": [block_id],
            }
        ]
        working.apply_delta(
            _request(chapter, working, block_ids=(block_id,), window_id=window_id), response
        )
    bindings = working.snapshot()["local_bindings"]
    assert {(row["block_id"], row["target_ref"]) for row in bindings} == {
        ("b002", "ent2_a"),
        ("b004", "ent2_b"),
    }


def test_probe_10_local_binding_does_not_leak_to_other_block(chapter: dict[str, Any]) -> None:
    entity = _entity("ent2_a", "Mr Heathcliff")
    binding = {
        "binding_id": "bind2_a",
        "surface": "The master",
        "block_id": "b002",
        "target_ref": "ent2_a",
        "status": "confirmed",
        "support_block_ids": ["b002"],
        "revision_hash": "r",
    }
    snapshot = _snapshot(entities=[entity], bindings=[binding])
    snapshot["generation_id"] = "reggen2_" + "a" * 20
    b004 = next(row for row in chapter["blocks"] if row["block_id"] == "b004")
    manifest = build_b2_candidate_manifest(
        chapter_id="bk_ch01",
        active_blocks=[b004],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    assert "bind2_a" not in manifest["local_binding_ids"]


def test_probe_11_candidate_uniqueness_never_mutates_registry(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(entities=[_entity("ent2_h", "Mr Heathcliff")])
    working = _working(chapter, snapshot)
    request = _request(chapter, working)
    packets = request.sections["surface_candidate_packets"]
    assert len(packets) == 1
    assert len(packets[0]["candidates"]) == 1
    working.apply_delta(request, _empty_delta())
    assert len(working.snapshot()["entities"]) == 1


def test_probe_12_matched_overflow_is_explicit_and_blocks_delta(chapter: dict[str, Any]) -> None:
    entities = [_entity(f"ent2_{i}", "Heathcliff") for i in range(3)]
    working = _working(chapter, _snapshot(entities=entities))
    config = _config(candidate_card_count_cap=1)
    request = _request(chapter, working, config=config)
    assert request.sections["candidate_selection_manifest"]["overflow"]
    response = _entity_delta("Heathcliff")
    result = working.apply_delta(request, response)
    assert not result["applied_ids"]["new_entities"]
    assert result["code_ticket_ids"]


def test_probe_13_recency_overflow_records_exclusions(chapter: dict[str, Any]) -> None:
    entities = [_entity(f"ent2_{i}", f"Person {i}", support="b003") for i in range(3)]
    selection = select_candidate_cards(
        snapshot=_snapshot(entities=entities),
        working_revision_hash="work",
        active_blocks=[next(row for row in chapter["blocks"] if row["block_id"] == "b004")],
        context_only_tail=[],
        block_order={str(row["block_id"]): int(row["order_index"]) for row in chapter["blocks"]},
        recency_k=2,
        card_count_cap=1,
        card_token_cap=9999,
    )
    manifest = selection["candidate_selection_manifest"]
    assert manifest["overflow"]
    assert len(manifest["excluded_recency_row_hashes"]) == 2


def test_probe_14_unlocatable_surface_becomes_code_ticket(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    result = working.apply_delta(_request(chapter, working), _entity_delta("Ghost"))
    assert not result["applied_ids"]["new_entities"]
    assert any(row["ticket_type"] == "unlocatable_surface" for row in working.snapshot()["tickets"])


def test_probe_15_importance_review_can_reject_noise(chapter: dict[str, Any], tmp_path: Path) -> None:
    working = _working(chapter)
    response = _entity_delta(
        "Pilot",
        tickets=[
            {
                "ticket_type": "importance_review",
                "surface": "Pilot",
                "block_id": "b002",
                "related_entity_ids": [],
                "note": "Could be incidental.",
            }
        ],
    )
    working.apply_delta(_request(chapter, working), response)
    manifest = build_exception_manifest(working)
    decision = _audit_response(manifest, entity_action="reject_noise")
    generation = build_registry_generation(
        chapter=chapter,
        working=working,
        b0_request_fingerprint="b0",
        exception_manifest=manifest,
        audit_request_fingerprints=["audit"],
        audit_decision=decision,
    )
    store = ChapterRegistryStoreV2(tmp_path / "store")
    store.commit(generation, expected_parent=None)
    assert not store.snapshot("lineage-test")["entities"]
    assert response["new_entities"]


def test_probe_16_auditor_must_exact_cover_all_exception_types(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    working.apply_delta(
        _request(chapter, working),
        _entity_delta(
            "Pilot",
            tickets=[
                {
                    "ticket_type": "importance_review",
                    "surface": "Pilot",
                    "block_id": "b002",
                    "related_entity_ids": [],
                    "note": "Review.",
                }
            ],
        ),
    )
    manifest = build_exception_manifest(working)
    decision = _audit_response(manifest)
    decision["ticket_dispositions"] = []
    with pytest.raises(RegistryContractError):
        validate_audit_decision(decision, exception_manifest=manifest, working=working)


def test_probe_17_auditor_cannot_merge_prior_confirmed_entity(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(entities=[_entity("ent2_prior", "Mr Heathcliff")])
    working = _working(chapter, snapshot)
    working.apply_delta(_request(chapter, working), _entity_delta("Mr Heathcliff"))
    manifest = build_exception_manifest(working)
    decision = _audit_response(manifest)
    decision["entity_dispositions"].append(
        {
            "entity_id": "ent2_prior",
            "action": "merge_provisional",
            "merge_target_entity_id": manifest["exception_ids"]["entities"][0],
            "revised_identity_summary": None,
        }
    )
    with pytest.raises(RegistryContractError):
        validate_audit_decision(decision, exception_manifest=manifest, working=working)


def test_probe_18_unknown_cannot_be_confirmed(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    working.apply_delta(_request(chapter, working), _entity_delta(kind="unknown"))
    manifest = build_exception_manifest(working)
    with pytest.raises(RegistryContractError):
        validate_audit_decision(
            _audit_response(manifest), exception_manifest=manifest, working=working
        )


def test_probe_19_checklist_miss_schedules_bounded_targeted_recall(chapter: dict[str, Any]) -> None:
    windows = build_registry_windows(chapter, target_tokens=30, max_blocks=1, preceding_tail_k=1)
    plan = schedule_targeted_recall(
        orientation={
            "salient_surface_checklist": [
                {"surface": "Pilot", "block_id": "b002"},
                {"surface": "Mr Heathcliff", "block_id": "b002"},
            ]
        },
        working_snapshot=_snapshot(),
        windows=windows,
        call_cap=2,
    )
    assert len(plan) == 1
    assert len(plan[0]["missing_surfaces"]) == 2


def test_probe_20_late_profile_is_rescanned_for_earlier_block(chapter: dict[str, Any]) -> None:
    entity = _entity("ent2_h", "Heathcliff", support="b004")
    snapshot = _snapshot(entities=[entity])
    snapshot["generation_id"] = "reggen2_" + "b" * 20
    b002 = next(row for row in chapter["blocks"] if row["block_id"] == "b002")
    manifest = build_b2_candidate_manifest(
        chapter_id="bk_ch01",
        active_blocks=[b002],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    assert manifest["candidate_entity_ids"] == ["ent2_h"]


def test_probe_21_bare_surname_returns_both_people(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(
        entities=[_entity("ent2_mr", "Mr Heathcliff"), _entity("ent2_mrs", "Mrs Heathcliff")]
    )
    snapshot["generation_id"] = "reggen2_" + "c" * 20
    block = {"block_id": "x", "order_index": 1, "clean_text": "Heathcliff entered."}
    manifest = build_b2_candidate_manifest(
        chapter_id="bk_ch01",
        active_blocks=[block],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    assert set(manifest["candidate_entity_ids"]) == {"ent2_mr", "ent2_mrs"}


def test_probe_22_b2_rescan_overflow_is_not_exhaustive(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(entities=[_entity(f"ent2_{i}", "Heathcliff") for i in range(3)])
    snapshot["generation_id"] = "reggen2_" + "d" * 20
    block = {"block_id": "x", "order_index": 1, "clean_text": "Heathcliff entered."}
    manifest = build_b2_candidate_manifest(
        chapter_id="bk_ch01",
        active_blocks=[block],
        registry_snapshot=snapshot,
        candidate_count_cap=1,
    )
    assert manifest["overflow"] is True
    assert manifest["pre_cap_count"] == 3
    assert manifest["selected_count"] == 1
    assert len(manifest["excluded_row_hashes"]) == 2


def test_probe_23_cas_crash_keeps_parent_and_one_writer_wins(chapter: dict[str, Any], tmp_path: Path) -> None:
    working_a = _working(chapter)
    working_a.apply_delta(_request(chapter, working_a), _entity_delta())
    generation_a = _clean_generation(chapter, working_a)
    store = ChapterRegistryStoreV2(tmp_path / "store")

    def crash() -> None:
        raise RuntimeError("crash before pointer")

    with pytest.raises(RuntimeError):
        store.commit(generation_a, expected_parent=None, before_pointer_switch=crash)
    assert store.current_generation_id("lineage-test") is None
    store.commit(generation_a, expected_parent=None)
    assert store.current_generation_id("lineage-test") == generation_a.generation_id

    working_b = _working(chapter)
    working_b.apply_delta(_request(chapter, working_b, window_id="other"), _entity_delta())
    generation_b = _clean_generation(chapter, working_b)
    with pytest.raises(RegistryStaleParentError):
        store.commit(generation_b, expected_parent=None)


def test_probe_24_v1_snapshot_hard_fails_as_v2(chapter: dict[str, Any]) -> None:
    foreign = _snapshot()
    foreign["schema_version"] = "chapter_registry_v1"
    with pytest.raises(RegistryContractError):
        _working(chapter, foreign)


def test_probe_25_clean_row_bypasses_auditor_and_has_record(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    working.apply_delta(_request(chapter, working), _entity_delta())
    manifest = build_exception_manifest(working)
    assert not any(manifest["exception_ids"].values())
    assert manifest["clean_commit_eligibility_records"][0]["eligible"] is True
    assert render_auditor_request(
        chapter=chapter,
        b0_gist="A visit.",
        working=working,
        exception_manifest=manifest,
        design_doc=DESIGN_DOC,
        run_config=_config(),
    ) is None


def test_probe_26_initial_alias_gets_code_id_and_target(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    response = _entity_delta(
        aliases=[
            {"surface": "Heathcliff", "alias_type": "name", "support_block_ids": ["b002"]}
        ]
    )
    result = working.apply_delta(_request(chapter, working), response)
    entity_id = result["applied_ids"]["new_entities"][0]
    alias = working.snapshot()["aliases"][0]
    assert alias["alias_id"].startswith("als2_")
    assert alias["entity_id"] == entity_id


def test_probe_26b_missing_empty_initial_aliases_is_code_normalized(
    chapter: dict[str, Any],
) -> None:
    working = _working(chapter)
    response = _entity_delta()
    response["new_entities"][0].pop("initial_aliases")

    result = working.apply_delta(_request(chapter, working), response)

    assert len(result["applied_ids"]["new_entities"]) == 1
    assert result["normalization_counts"] == {
        "missing_initial_aliases_to_empty": 1,
        "repeated_open_ticket_ignored": 0,
    }


def test_repeated_supplied_open_ticket_is_ignored_idempotently(
    chapter: dict[str, Any],
) -> None:
    entity = _entity("ent_heathcliff", "Mr Heathcliff", support="b002")
    ticket_body = {
        "ticket_id": "tick_existing",
        "ticket_type": "possible_duplicate",
        "surface": "Mrs Heathcliff",
        "block_id": "b002",
        "related_entity_ids": ["ent_heathcliff"],
        "note": "Existing collision to resolve later.",
        "status": "open",
        "opened_by_request_fingerprint": "request-prior",
    }
    ticket = {**ticket_body, "revision_hash": canonical_hash(ticket_body)}
    working = _working(chapter, _snapshot(entities=[entity], tickets=[ticket]))
    request = _request(chapter, working, block_ids=("b003",))
    assert request.sections["open_tickets"][0]["ticket_id"] == "tick_existing"
    response = _empty_delta()
    response["tickets"] = [
        {
            "ticket_type": "possible_duplicate",
            "surface": "Mrs Heathcliff",
            "block_id": "b002",
            "related_entity_ids": ["ent_heathcliff"],
            "note": "Rephrased copy of the already supplied ticket.",
        }
    ]

    result = working.apply_delta(request, response)

    assert result["normalization_counts"]["repeated_open_ticket_ignored"] == 1
    assert len(working.snapshot()["tickets"]) == 1


def test_new_ticket_from_non_active_block_remains_fatal(chapter: dict[str, Any]) -> None:
    entity = _entity("ent_heathcliff", "Mr Heathcliff", support="b002")
    working = _working(chapter, _snapshot(entities=[entity]))
    request = _request(chapter, working, block_ids=("b003",))
    response = _empty_delta()
    response["tickets"] = [
        {
            "ticket_type": "possible_duplicate",
            "surface": "Mrs Heathcliff",
            "block_id": "b002",
            "related_entity_ids": ["ent_heathcliff"],
            "note": "This is not a supplied open ticket.",
        }
    ]

    with pytest.raises(RegistryContractError, match="must cite an active block"):
        working.apply_delta(request, response)


def test_probe_27_glossary_duplicate_routes_to_exception(chapter: dict[str, Any]) -> None:
    item = {
        "glossary_id": "gls2_old",
        "surface": "Thrushcross Grange",
        "category_claim": "place_name",
        "short_description": "A named estate.",
        "created_from_block_ids": ["b003"],
        "support_block_ids": ["b003"],
        "status": "confirmed",
        "revision_hash": "r",
    }
    working = _working(chapter, _snapshot(glossary=[item]))
    response = _empty_delta()
    response["new_glossary_items"] = [
        {
            "surface": "Thrushcross Grange",
            "category_claim": "institution_name",
            "short_description": "A household institution.",
            "created_from_block_id": "b003",
            "support_block_ids": ["b003"],
        }
    ]
    working.apply_delta(_request(chapter, working, block_ids=("b003",)), response)
    manifest = build_exception_manifest(working)
    assert manifest["exception_ids"]["glossary_items"]
    assert any(row["ticket_type"] == "glossary_collision" for row in manifest["tickets"])


def test_probe_28_ticket_routes_entity_and_initial_alias_together(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    response = _entity_delta(
        aliases=[
            {"surface": "Heathcliff", "alias_type": "name", "support_block_ids": ["b002"]}
        ],
        tickets=[
            {
                "ticket_type": "importance_review",
                "surface": "Heathcliff",
                "block_id": "b002",
                "related_entity_ids": [],
                "note": "Review this identity component.",
            }
        ],
    )
    working.apply_delta(_request(chapter, working), response)
    manifest = build_exception_manifest(working)
    assert len(manifest["exception_ids"]["entities"]) == 1
    assert len(manifest["exception_ids"]["aliases"]) == 1


def test_probe_29_targeted_recall_cap_halts_without_dropping(chapter: dict[str, Any]) -> None:
    windows = build_registry_windows(chapter, target_tokens=10, max_blocks=1, preceding_tail_k=0)
    with pytest.raises(RegistryBudgetError):
        schedule_targeted_recall(
            orientation={
                "salient_surface_checklist": [
                    {"surface": "Mr Heathcliff", "block_id": "b002"},
                    {"surface": "Pilot", "block_id": "b003"},
                ]
            },
            working_snapshot=_snapshot(),
            windows=windows,
            call_cap=1,
        )


def test_probe_30_empty_exception_manifest_never_renders_auditor(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    working.apply_delta(_request(chapter, working), _empty_delta())
    manifest = build_exception_manifest(working)
    assert render_auditor_request(
        chapter=chapter,
        b0_gist="A visit.",
        working=working,
        exception_manifest=manifest,
        design_doc=DESIGN_DOC,
        run_config=_config(),
    ) is None


def test_probe_31_v1_and_v2_prompt_blocks_remain_distinct() -> None:
    v1 = load_system_prompt_from_design(DESIGN_DOC, "literary_chapter_orient_v1")
    v2 = load_system_prompt_from_design(DESIGN_DOC, PROMPT_IDS["b0"])
    assert v1 != v2
    assert "scene_range" not in v2
    assert load_system_prompt_from_design(DESIGN_DOC, "literary_registry_extract_v1")


def test_hardening_b1_does_not_dump_unrelated_open_tickets(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(entities=[_entity("ent2_h", "Mr Heathcliff")])
    snapshot["tickets"] = [
        {
            "ticket_id": "tick2_unrelated",
            "ticket_type": "importance_review",
            "surface": "Unrelated Surface",
            "block_id": "b999",
            "related_entity_ids": [],
            "note": "Unrelated prior ticket.",
            "status": "carried",
            "opened_by_request_fingerprint": "old",
            "revision_hash": "r",
        }
    ]
    request = _request(chapter, _working(chapter, snapshot))
    assert request.sections["open_tickets"] == []


def test_hardening_candidate_link_uses_literal_source_surface(chapter: dict[str, Any]) -> None:
    snapshot = _snapshot(
        entities=[_entity("ent2_mr", "Mr Heathcliff"), _entity("ent2_mrs", "Mrs Heathcliff")]
    )
    block = {"block_id": "x", "order_index": 1, "clean_text": "Heathcliff entered."}
    selection = select_candidate_cards(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=[block],
        context_only_tail=[],
        block_order={"x": 1},
        recency_k=0,
        card_count_cap=8,
        card_token_cap=9999,
    )
    packets = selection["surface_candidate_packets"]
    assert [row["source_surface"] for row in packets] == ["Heathcliff"]
    assert {row["candidate_card"]["entity_id"] for row in packets[0]["candidates"]} == {
        "ent2_mr",
        "ent2_mrs",
    }


def test_hardening_candidate_match_respects_word_boundaries() -> None:
    snapshot = _snapshot(entities=[_entity("ent2_arden", "Arden")])
    garden = {"block_id": "x", "order_index": 1, "clean_text": "The garden was empty."}
    exact = {"block_id": "y", "order_index": 2, "clean_text": "ARDEN returned."}

    absent = select_candidate_cards(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=[garden],
        context_only_tail=[],
        block_order={"x": 1},
        recency_k=0,
        card_count_cap=8,
        card_token_cap=9999,
    )
    present = select_candidate_cards(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=[exact],
        context_only_tail=[],
        block_order={"y": 2},
        recency_k=0,
        card_count_cap=8,
        card_token_cap=9999,
    )

    assert absent["surface_candidate_packets"] == []
    assert present["surface_candidate_packets"][0]["source_surface"] == "ARDEN"
    assert [
        row["candidate_card"]["entity_id"]
        for row in present["surface_candidate_packets"][0]["candidates"]
    ] == ["ent2_arden"]


def test_prejoined_context_aggregates_blocks_and_embeds_card_once() -> None:
    snapshot = _snapshot(entities=[_entity("ent2_arden", "Arden", support="x")])
    selection = select_candidate_cards(
        snapshot=snapshot,
        working_revision_hash="work",
        active_blocks=[
            {"block_id": "x", "order_index": 1, "clean_text": "Arden entered."},
            {"block_id": "y", "order_index": 2, "clean_text": "Arden replied."},
        ],
        context_only_tail=[],
        block_order={"x": 1, "y": 2},
        recency_k=0,
        card_count_cap=8,
        card_token_cap=9999,
    )

    packet = selection["surface_candidate_packets"][0]
    assert packet["source_surface"] == "Arden"
    assert packet["source_block_ids"] == ["x", "y"]
    assert len(packet["candidates"]) == 1
    assert packet["candidates"][0]["candidate_card"]["entity_id"] == "ent2_arden"
    manifest = selection["candidate_selection_manifest"]
    assert manifest["packet_candidate_count"] == 1
    assert manifest["prejoined_context_bytes"] < manifest["legacy_separate_context_bytes"]


def test_prejoined_singleton_is_not_identity_authority(chapter: dict[str, Any]) -> None:
    working = _working(
        chapter,
        _snapshot(entities=[_entity("ent2_existing", "Mr Heathcliff", support="b002")]),
    )
    request = _request(chapter, working)
    packet = request.sections["surface_candidate_packets"][0]
    assert len(packet["candidates"]) == 1

    result = working.apply_delta(request, _entity_delta("Mr Heathcliff"))

    assert len(working.snapshot()["entities"]) == 2
    assert result["applied_ids"]["new_entities"]
    tickets = [
        row
        for row in working.snapshot()["tickets"]
        if row["ticket_type"] in {"same_name_collision", "possible_duplicate"}
    ]
    assert tickets
    assert "ent2_existing" in tickets[0]["related_entity_ids"]


def test_prejoined_request_retires_separate_arrays_without_manifest_duplication(
    chapter: dict[str, Any],
) -> None:
    working = _working(chapter, _snapshot(entities=[_entity("ent2_h", "Mr Heathcliff")]))
    request = _request(chapter, working)

    assert {
        "entity_candidate_cards",
        "glossary_candidate_cards",
        "candidate_links",
    }.isdisjoint(request.sections)
    assert "candidate_links" not in request.sections["candidate_selection_manifest"]
    assert request.sections["surface_candidate_packets"]


def test_prejoined_packet_tamper_is_fatal_before_apply(chapter: dict[str, Any]) -> None:
    working = _working(chapter, _snapshot(entities=[_entity("ent2_h", "Mr Heathcliff")]))
    request = _request(chapter, working)
    packets = list(request.sections["surface_candidate_packets"])
    packets[0] = {**packets[0], "candidate_overflow": True}
    tampered = replace(
        request,
        sections={**request.sections, "surface_candidate_packets": packets},
    )

    with pytest.raises(RegistryContractError, match="packet manifest mismatch"):
        working.apply_delta(tampered, _empty_delta())
    assert len(working.snapshot()["entities"]) == 1


def test_prejoined_recency_only_card_remains_a_permitted_target(
    chapter: dict[str, Any],
) -> None:
    working = _working(
        chapter,
        _snapshot(entities=[_entity("ent2_arden", "Arden", support="b003")]),
    )
    request = _request(
        chapter,
        working,
        block_ids=("b004",),
        config=_config(recency_k=2),
    )
    assert request.sections["surface_candidate_packets"] == []
    assert [
        row["candidate_card"]["entity_id"]
        for row in request.sections["unmatched_recency_cards"]
    ] == ["ent2_arden"]
    response = _empty_delta()
    response["local_bindings"] = [
        {
            "surface": "The master",
            "block_id": "b004",
            "target_entity_id": "ent2_arden",
            "support_block_ids": ["b004"],
        }
    ]

    result = working.apply_delta(request, response)

    assert result["applied_ids"]["local_bindings"]


def test_hardening_auditor_splits_only_by_disconnected_component(chapter: dict[str, Any]) -> None:
    working = _working(chapter)
    for surface, block_id, window_id in (
        ("Mr Heathcliff", "b002", "w-a"),
        ("Pilot", "b003", "w-b"),
    ):
        response = _entity_delta(
            surface,
            block_id=block_id,
            tickets=[
                {
                    "ticket_type": "importance_review",
                    "surface": surface,
                    "block_id": block_id,
                    "related_entity_ids": [],
                    "note": "Independent review component.",
                }
            ],
        )
        working.apply_delta(
            _request(chapter, working, block_ids=(block_id,), window_id=window_id), response
        )
    manifest = build_exception_manifest(working)
    assert len(manifest["components"]) == 2
    high = _config(auditor_input_token_cap=20000)
    full = render_auditor_request(
        chapter=chapter,
        b0_gist="A visit.",
        working=working,
        exception_manifest=manifest,
        design_doc=DESIGN_DOC,
        run_config=high,
        enforce_input_cap=False,
    )
    assert full is not None
    component_tokens = []
    for component in manifest["components"]:
        request = render_auditor_request(
            chapter=chapter,
            b0_gist="A visit.",
            working=working,
            exception_manifest=_exception_manifest_subset(manifest, component),
            design_doc=DESIGN_DOC,
            run_config=high,
            enforce_input_cap=False,
        )
        assert request is not None
        component_tokens.append(
            estimate_registry_prompt_tokens(request.messages)
        )
    full_tokens = estimate_registry_prompt_tokens(full.messages)
    assert max(component_tokens) < full_tokens
    requests = render_auditor_requests(
        chapter=chapter,
        b0_gist="A visit.",
        working=working,
        exception_manifest=manifest,
        design_doc=DESIGN_DOC,
        run_config=_config(auditor_input_token_cap=max(component_tokens)),
    )
    assert len(requests) == 2


def test_hardening_run_config_keeps_per_key_model_quota_gates() -> None:
    config = _config(
        quota_gates={
            "gpt-primary": {
                "quota_bucket_id": "openai-primary",
                "model_id": "gpt-5.4",
                "rpm": None,
                "tpm": None,
                "rpd": None,
                "internal_utc_day_token_cap": 225000,
            },
            "mini-primary": {
                "quota_bucket_id": "openai-primary",
                "model_id": "gpt-5.4-mini",
                "rpm": 15,
                "tpm": 250000,
                "rpd": 500,
                "internal_utc_day_token_cap": 2250000,
            },
        }
    )
    assert config.unknown_provider_limit_gate_ids == ("gpt-primary",)
    assert config.role_quota_gate_ids["b0"] == ("gpt-primary",)


def test_alias_scope_merge_downscopes_every_non_name_surface(tmp_path: Path) -> None:
    chapter = _alias_scope_chapter()
    working = _working(chapter)
    first = working.apply_delta(
        _request(chapter, working, block_ids=("b101",), window_id="w-scope-1"),
        _entity_delta("female pointer", block_id="b101", kind="animal"),
    )
    animal_id = first["applied_ids"]["new_entities"][0]

    second_response = _entity_delta(
        "honored guest",
        block_id="b102",
        kind="animal",
        tickets=[
            {
                "ticket_type": "possible_duplicate",
                "surface": "honored guest",
                "block_id": "b102",
                "related_entity_ids": [animal_id],
                "note": "The active source may refer to the supplied animal.",
            }
        ],
    )
    second_response["new_aliases"] = [
        {
            "surface": "wretched cur",
            "alias_type": "nickname",
            "target_entity_id": animal_id,
            "support_block_ids": ["b102"],
        }
    ]
    second = working.apply_delta(
        _request(chapter, working, block_ids=("b102",), window_id="w-scope-2"),
        second_response,
    )
    merged_id = second["applied_ids"]["new_entities"][0]
    manifest = build_exception_manifest(working)
    decision = _audit_response(manifest)
    for row in decision["entity_dispositions"]:
        if row["entity_id"] == merged_id:
            row["action"] = "merge_provisional"
            row["merge_target_entity_id"] = animal_id
        else:
            row["action"] = "confirm"

    generation = build_registry_generation(
        chapter=chapter,
        working=working,
        b0_request_fingerprint="b0-scope",
        exception_manifest=manifest,
        audit_request_fingerprints=["audit-scope"],
        audit_decision=decision,
    )
    repeated = build_registry_generation(
        chapter=chapter,
        working=working,
        b0_request_fingerprint="b0-scope",
        exception_manifest=manifest,
        audit_request_fingerprints=["audit-scope"],
        audit_decision=decision,
    )
    assert generation.to_dict() == repeated.to_dict()

    store = ChapterRegistryStoreV2(tmp_path / "scope-store")
    store.commit(generation, expected_parent=None)
    snapshot = store.snapshot("lineage-test")
    assert [(row["entity_id"], row["referent_kind"]) for row in snapshot["entities"]] == [
        (animal_id, "animal")
    ]
    assert snapshot["aliases"] == []
    assert {
        (row["surface"], row["block_id"], row["target_ref"], row["status"])
        for row in snapshot["local_bindings"]
    } == {
        ("honored guest", "b102", animal_id, "confirmed"),
        ("wretched cur", "b102", animal_id, "confirmed"),
    }
    assert {
        row["outcome"] for row in generation.to_dict()["surface_commit_gate_records"]
    } == {"downscope_local"}


def test_alias_scope_stable_proper_name_remains_global() -> None:
    chapter = _alias_scope_chapter()
    working = _working(chapter)
    working.apply_delta(
        _request(chapter, working, block_ids=("b101",)),
        _entity_delta(
            "Mr Alden",
            block_id="b101",
            aliases=[
                {"surface": "Alden", "alias_type": "name", "support_block_ids": ["b101"]}
            ],
        ),
    )
    generation = _clean_generation(chapter, working).to_dict()
    assert [row["surface"] for row in generation["alias_revisions"]] == ["Alden"]
    gate = generation["surface_commit_gate_records"]
    assert len(gate) == 1
    assert gate[0]["outcome"] == "global_alias_candidate"
    assert gate[0]["emitted_global_alias_id"] == generation["alias_revisions"][0]["alias_id"]
    assert generation["local_binding_revisions"] == []


def test_alias_scope_sentence_initial_name_is_local_and_reviewable() -> None:
    chapter = _alias_scope_chapter()
    working = _working(chapter)
    working.apply_delta(
        _request(chapter, working, block_ids=("b103",)),
        _entity_delta(
            "The visitor",
            block_id="b103",
            aliases=[
                {"surface": "Alden", "alias_type": "name", "support_block_ids": ["b103"]}
            ],
        ),
    )
    generation = _clean_generation(chapter, working).to_dict()
    assert generation["alias_revisions"] == []
    assert [row["surface"] for row in generation["local_binding_revisions"]] == ["Alden"]
    assert any(
        row["ticket_type"] == "surface_scope_review"
        for row in generation["ticket_revisions"]
    )
    assert generation["surface_commit_gate_records"][0]["sentence_initial_only"]


def test_alias_scope_name_form_after_abbreviated_token_is_not_sentence_initial_only() -> None:
    chapter = _alias_scope_chapter()
    working = _working(chapter)
    working.apply_delta(
        _request(chapter, working, block_ids=("b105",)),
        _entity_delta(
            "Dr. Bela",
            block_id="b105",
            aliases=[
                {"surface": "Bela", "alias_type": "name", "support_block_ids": ["b105"]}
            ],
        ),
    )
    generation = _clean_generation(chapter, working).to_dict()
    assert [row["surface"] for row in generation["alias_revisions"]] == ["Bela"]
    gate = generation["surface_commit_gate_records"]
    assert gate[0]["proper_name_signal"]
    assert not gate[0]["sentence_initial_only"]


def test_alias_scope_capitalization_never_upgrades_pending_authority() -> None:
    plan = route_surface_for_commit(
        surface="Captain",
        alias_type="title",
        target_entity_id="ent2_example",
        support_block_ids=["b900"],
        located_source_spans=[
            {"block_id": "b900", "char_start": 12, "char_end": 19, "sentence_initial": False}
        ],
        source_status="pending",
        source_origin="auditor_alias",
    )
    assert plan["outcome"] == "global_alias_candidate"
    assert plan["source_status"] == "pending"


def test_alias_scope_same_surface_can_bind_different_entities_by_block() -> None:
    snapshot = _snapshot(
        entities=[_entity("ent_a", "Alden"), _entity("ent_b", "Bela")],
        bindings=[
            _binding("bind_a", "the steward", "ent_a", "x"),
            _binding("bind_b", "the steward", "ent_b", "y"),
        ],
    )
    snapshot["generation_id"] = "reggen2_" + "0" * 20
    x = build_b2_candidate_manifest(
        chapter_id="bk",
        active_blocks=[{"block_id": "x", "clean_text": "The steward entered."}],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    y = build_b2_candidate_manifest(
        chapter_id="bk",
        active_blocks=[{"block_id": "y", "clean_text": "The steward entered."}],
        registry_snapshot=snapshot,
        candidate_count_cap=8,
    )
    assert x["candidate_entity_ids"] == ["ent_a"]
    assert y["candidate_entity_ids"] == ["ent_b"]


def test_alias_scope_prompt_is_neutral_and_rejects_nonreferential_noise() -> None:
    b1 = load_system_prompt_from_design(DESIGN_DOC, PROMPT_IDS["b1"])
    auditor = load_system_prompt_from_design(DESIGN_DOC, PROMPT_IDS["auditor"])
    combined = f"{b1}\n{auditor}".casefold()
    for source_specific in (
        "heathcliff",
        "catherine",
        "earnshaw",
        "lockwood",
        "wuthering",
        "thrushcross",
        "juno",
    ):
        assert source_specific not in combined
    assert "Non-referential profanity or kinship vocabulary creates no entity" in b1
    assert "shared identity does not prove" in auditor.casefold()


def test_alias_scope_old_validator_or_missing_policy_is_rejected(
    chapter: dict[str, Any],
) -> None:
    with pytest.raises(RegistryContractError, match="validator contract mismatch"):
        _request(
            chapter,
            _working(chapter),
            config=replace(_config(), validator_version="chapter_registry_validator_v2"),
        )
    policies = dict(_config().policy_versions)
    policies.pop("alias_scope")
    with pytest.raises(RegistryContractError, match="alias_scope"):
        _request(
            chapter,
            _working(chapter),
            config=replace(_config(), policy_versions=policies),
        )


def test_alias_scope_source_catalog_mismatch_is_fatal() -> None:
    chapter = _alias_scope_chapter()
    working = _working(chapter)
    drifted = _alias_scope_chapter()
    drifted["blocks"][0]["clean_text"] += " Changed."
    manifest = build_exception_manifest(working)
    with pytest.raises(RegistryContractError, match="source catalog"):
        build_registry_generation(
            chapter=drifted,
            working=working,
            b0_request_fingerprint="b0",
            exception_manifest=manifest,
            audit_request_fingerprints=[],
            audit_decision=None,
        )


def test_alias_scope_malformed_unclassified_alias_is_fatal() -> None:
    chapter = _alias_scope_chapter()
    entity = _entity("ent_a", "Alden", support="b101")
    working = _working(chapter, _snapshot(entities=[entity]))
    malformed = _alias("als_bad", "Alden", "ent_a", support="b101")
    malformed.pop("alias_type")
    working._state["aliases"].append(malformed)
    manifest = build_exception_manifest(working)
    with pytest.raises(RegistryContractError, match="commit-time alias candidate"):
        build_registry_generation(
            chapter=chapter,
            working=working,
            b0_request_fingerprint="b0",
            exception_manifest=manifest,
            audit_request_fingerprints=[],
            audit_decision=None,
        )
