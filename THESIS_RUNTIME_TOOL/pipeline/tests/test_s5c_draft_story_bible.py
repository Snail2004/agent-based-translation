from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.literary.b4_handoff_v3 import (
    _input_identity_projection,
    assemble_b4_input_bundle,
    build_book_source_manifest,
)
from pipeline.literary.builder_v3_pipeline import SyntheticStageExecutor, run_m1_v3, run_m2_v3
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.draft_story_bible import (
    DraftBibleError,
    assert_draft_output_root,
    bind_endpoints,
    build_eyeball_manifest,
    build_final_pair_batches,
    build_identity_target_manifest,
    build_preflight_report,
    consolidate_diagnostic_draft,
    execute_phase_draft_request,
    gate_preflight_budget,
    load_phase_draft_prompt,
    plan_identity_proposal_shards,
    plan_identity_retrieval_shards,
    plan_phase_draft_calls,
    reconcile_identity_claims,
    render_phase_draft_request,
    run_recorded_draft_apply,
    select_output_cap,
    validate_phase_draft_response,
    write_draft_artifacts,
)
from pipeline.literary.step5c_slice import (
    IDENTITY_PROPOSAL_PROMPT_ID,
    IDENTITY_RETRIEVAL_PROMPT_ID,
    load_slice_prompt,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
E9_DEFECTS = RUNTIME_ROOT / "data" / "reports" / "literary_m4f_e9_audit" / "defect_reconciliation_v1.md"


def _chapter(number: int) -> dict[str, Any]:
    chapter_id = f"bk_ch{number:02d}"
    text = (
        "The canine mother, known here as Madam, remained an animal. "
        "Aster greeted Rowan beside the chamber door. Later Aster said, \"Rowan, stay.\""
    )
    return {
        "chapter_id": chapter_id,
        "chapter_label": f"Chapter {number}",
        "blocks": [
            {
                "block_id": f"{chapter_id}_b001",
                "block_type": "paragraph",
                "order_index": number * 100 + 1,
                "clean_text": text,
                "source_text": text,
            },
            {
                "block_id": f"{chapter_id}_b002",
                "block_type": "paragraph",
                "order_index": number * 100 + 2,
                "clean_text": "Aster and Rowan left the room.",
                "source_text": "Aster and Rowan left the room.",
            },
        ],
    }


def _endpoint(surface: str, evidence: str, mention_ref: str, occurrence_hint: int) -> dict[str, Any]:
    return {
        "surface": surface,
        "reference_scope": "individual",
        "referent_kind_claim": "person",
        "mention_ref": mention_ref,
        "attribution_method": "explicit_tag",
        "anchor_text": surface,
        "evidence_quote": evidence,
        "occurrence_hint": occurrence_hint,
    }


def _script(number: int) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    chapter_id = f"bk_ch{number:02d}"
    block = f"{chapter_id}_b001"
    block2 = f"{chapter_id}_b002"
    dog_quote = "The canine mother, known here as Madam, remained an animal."
    event_quote = "Aster greeted Rowan beside the chamber door."
    turn_quote = 'Later Aster said, "Rowan, stay."'
    aster_mention = f"m_{block}_02"
    rowan_mention = f"m_{block}_03"
    event_id = f"e_{block}_01"
    return {
        ("b0", chapter_id, None): {
            "chapter_id": chapter_id,
            "cast_claims": [
                {
                    "surface": "The canine mother",
                    "surface_kind": "descriptor",
                    "referent_kind_claim": "animal",
                    "role_hint": "animal in the room",
                    "scene_range": [block, block2],
                    "source_block_ids": [block],
                    "anchor_text": "The canine mother",
                    "evidence_quote": dog_quote,
                }
            ],
            "setting": {
                "place": "an unnamed room",
                "time_frame_hint": "frame_present",
                "scene_shape": "single_scene_one_location",
            },
            "scenes_party_size": [
                {"block_range": [block, block2], "co_present_count": 3, "participants": ["Aster", "Rowan", "Madam"]}
            ],
            "neutral_premise": "Two visitors meet in a room.",
        },
        ("b1", chapter_id, f"w_{chapter_id}_01"): {
            "chapter_id": chapter_id,
            "window_block_ids": [block, block2],
            "context_only_used": False,
            "character_mentions": [
                {
                    "surface": "Madam",
                    "mention_type": "descriptor",
                    "referent_kind_claim": "animal",
                    "anchor_text": "Madam",
                    "evidence_quote": dog_quote,
                    "block_id": block,
                },
                {
                    "surface": "Aster",
                    "mention_type": "name",
                    "referent_kind_claim": "person",
                    "anchor_text": "Aster",
                    "evidence_quote": event_quote,
                    "block_id": block,
                    "occurrence_hint": 1,
                },
                {
                    "surface": "Rowan",
                    "mention_type": "name",
                    "referent_kind_claim": "person",
                    "anchor_text": "Rowan",
                    "evidence_quote": event_quote,
                    "block_id": block,
                    "occurrence_hint": 1,
                },
                {
                    "surface": "chamber door",
                    "mention_type": "descriptor",
                    "referent_kind_claim": "object",
                    "anchor_text": "chamber door",
                    "evidence_quote": event_quote,
                    "block_id": block,
                },
            ],
            "glossary_candidates": [
                {
                    "source_term": "chamber door",
                    "proposed_target_vi": "cua phong",
                    "category": "object",
                    "do_not_translate": False,
                    "block_ids": [block],
                }
            ],
        },
        ("b2", chapter_id, f"w_{chapter_id}_01"): {
            "chapter_id": chapter_id,
            "window_block_ids": [block, block2],
            "context_only_used": False,
            "speaker_turns": [
                {
                    "speaker": _endpoint("Aster", turn_quote, aster_mention, 2),
                    "addressee": _endpoint("Rowan", turn_quote, rowan_mention, 2),
                    "utterance_quote": turn_quote,
                    "address_terms": [
                        {"anchor_text": "Rowan", "evidence_quote": '"Rowan, stay."', "addressee_ref": "addressee"}
                    ],
                    "register_cue": "neutral",
                    "block_id": block,
                }
            ],
            "relation_events": [
                {
                    "actor": _endpoint("Aster", event_quote, aster_mention, 1),
                    "target": _endpoint("Rowan", event_quote, rowan_mention, 1),
                    "event_type": "greets",
                    "evidence_quote": event_quote,
                    "block_id": block,
                }
            ],
        },
        ("b3", chapter_id, None): {
            "chapter_id": chapter_id,
            "chapter_rolling_summary": "Aster greets Rowan.",
            "narration_frame_segments": [
                {
                    "local_segment_key": "primary",
                    "parent_local_key": None,
                    "narrator_surface": "Aster",
                    "narrator_ref": aster_mention,
                    "frame_kind": "primary_narration",
                    "story_time_label": "frame_present",
                    "block_range": [block, block2],
                    "start_boundary": None,
                    "end_boundary": None,
                    "status": "proposed",
                    "evidence_quote": event_quote,
                }
            ],
            "relation_observations": [
                {
                    "event_id": event_id,
                    "endpoint_refs": [f"{event_id}#actor", f"{event_id}#target"],
                    "observed_valence_hint": "positive",
                    "block_id": block,
                    "evidence_quote": event_quote,
                    "transition_hint": {"trigger_event_id": event_id, "note": "The greeting opens a cordial phase."},
                }
            ],
            "character_state_changes": [
                {
                    "subject_ref": aster_mention,
                    "attribute": "social_status",
                    "from_value": "visitor",
                    "to_value": "guest",
                    "trigger_ref": event_id,
                    "evidence_quote": event_quote,
                }
            ],
            "unresolved_threads": [
                {
                    "thread_local_id": f"thread_{number}",
                    "description": "The purpose of the visit is unresolved.",
                    "opened_block": block,
                    "kind": "question",
                    "subject_refs": [rowan_mention],
                }
            ],
            "translator_relevant_facts": [
                {
                    "fact_type": "status",
                    "fact": "Aster is treated as a guest.",
                    "block_evidence": [block],
                    "inference_basis": "stated",
                    "subject_ref": aster_mention,
                    "event_ids": [event_id],
                }
            ],
            "motifs": [{"note": "Greetings recur.", "block_ids": [block], "subject_refs": [f"{event_id}#actor"]}],
        },
    }


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("s5c-draft-bundle")
    document = {"document_id": "s5c-draft-fixture", "chapters": [_chapter(1), _chapter(2)]}
    scripts: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    scripts.update(_script(1))
    scripts.update(_script(2))
    chapters = ["bk_ch01", "bk_ch02"]
    executor = SyntheticStageExecutor(scripts)
    assert run_m1_v3(document, chapters, executor=executor, out_dir=root)["status"] == "complete"
    assert run_m2_v3(document, chapters, executor=executor, out_dir=root, m1v3_dir=root)["status"] == "complete"
    return assemble_b4_input_bundle(
        document,
        chapters,
        book_source_manifest=build_book_source_manifest(document),
        m1v3_dir=root,
        m2v3_dir=root,
    )


def _reseal(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    result["input_identity_manifest_hash"] = canonical_hash(_input_identity_projection(result))
    result.pop("bundle_manifest_hash", None)
    result["bundle_manifest_hash"] = canonical_hash(result)
    return result


def _target_groups(bundle: dict[str, Any]) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    manifest = build_identity_target_manifest(bundle)
    cards = {row["occurrence_id"]: row for row in bundle["occurrence_cards"]}
    groups: dict[str, list[str]] = {}
    for row in manifest["owned_targets"]:
        occurrence_id = row["occurrence_id"]
        key = str(cards[occurrence_id].get("surface") or "").casefold()
        groups.setdefault(key, []).append(occurrence_id)
    proposals: list[dict[str, Any]] = []
    all_ids = set(value for values in groups.values() for value in values)
    for surface, values in groups.items():
        kind = "animal" if surface == "madam" else "person"
        for target in values:
            different = sorted(all_ids - set(values))[:1]
            proposals.append(
                {
                    "target_occurrence_id": target,
                    "status": "proposed",
                    "same_referent_occurrence_ids": list(values),
                    "different_referent_occurrence_ids": different,
                    "referent_kind": kind,
                    "canonical_surface_guess": cards[target]["surface"],
                    "evidence_refs": [],
                }
            )
    return groups, proposals


def _phase_response(pair_batch: dict[str, Any]) -> dict[str, Any]:
    events = pair_batch["events"]
    first = events[0]
    dispositions = [
        {"event_id": first["event_id"], "outcomes": ["phase_support"]},
        *[
            {"event_id": row["event_id"], "outcomes": ["no_change"]}
            for row in events[1:]
        ],
    ]
    return {
        "pair": pair_batch["pair"],
        "considered_event_ids": pair_batch["event_ids"],
        "event_dispositions": dispositions,
        "relation_phases": [
            {
                "phase_label": "friendly",
                "valid_from_block": first["block_id"],
                "valid_until_block": None,
                "status": "open",
                "trigger_event_id": first["event_id"],
                "trigger_evidence_quote": first["evidence_quote"],
            }
        ],
        "relation_facts": [],
    }


def _identity_pipeline(bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _groups, proposals = _target_groups(bundle)
    identity = reconcile_identity_claims(bundle, proposal_rows=proposals)
    endpoints = bind_endpoints(bundle, identity)
    pairs = build_final_pair_batches(bundle, endpoints)
    return identity, endpoints, pairs


def test_probe_01_prompt_loader_locks_new_contract() -> None:
    prompt = load_phase_draft_prompt(DESIGN_DOC)
    assert "ACCOUNT FOR EVERY EVENT" in prompt
    assert "inference_basis" in prompt


def test_probe_02_target_manifest_exact_cover_includes_deferred(bundle: dict[str, Any]) -> None:
    changed = deepcopy(bundle)
    object_card = next(row for row in changed["occurrence_cards"] if row["referent_kind_claim"] == "object")
    non_person = changed["occurrence_routing"]["non_person_occurrences"]
    non_person[:] = [row for row in non_person if row["occurrence_id"] != object_card["occurrence_id"]]
    changed["occurrence_routing"]["deferred"].append(object_card)
    changed = _reseal(changed)
    manifest = build_identity_target_manifest(changed)
    targets = {row["occurrence_id"] for row in manifest["owned_targets"]}
    assert object_card["occurrence_id"] in targets
    assert manifest["counts"]["owned_mentions"] > 0 and manifest["counts"]["owned_endpoints"] > 0

    retired = deepcopy(bundle)
    retired["ground_evidence"]["cast_claim_inputs"] = []
    retired = _reseal(retired)
    with pytest.raises(DraftBibleError, match="retired cast_claim_inputs"):
        build_identity_target_manifest(retired)


def test_probe_03_identity_shards_are_ordered_exact_cover(bundle: dict[str, Any]) -> None:
    prompt = load_slice_prompt(DESIGN_DOC, IDENTITY_RETRIEVAL_PROMPT_ID)
    plans = plan_identity_retrieval_shards(
        bundle,
        prompt_text=prompt,
        provider="recorded",
        model_config={"model": "gpt-5.4", "max_output_tokens": 12288},
        prompt_token_cap=9000,
    )
    expected = [row["occurrence_id"] for row in build_identity_target_manifest(bundle)["owned_targets"]]
    assert [target for plan in plans for target in plan.target_ids] == expected
    assert all(plan.max_output_tokens >= plan.response_floor_tokens for plan in plans)
    assert all(plan.request_body["model_config"]["max_output_tokens"] == plan.max_output_tokens for plan in plans)
    retrieval = {
        "targets": [
            {
                "target_occurrence_id": target,
                "candidate_occurrence_ids": [],
                "status": "selected",
                "evidence_refs": [],
            }
            for target in expected
        ]
    }
    proposal_plans = plan_identity_proposal_shards(
        bundle,
        retrieval=retrieval,
        prompt_text=load_slice_prompt(DESIGN_DOC, IDENTITY_PROPOSAL_PROMPT_ID),
        provider="recorded",
        model_config={"model": "gpt-5.4", "max_output_tokens": 12288},
        prompt_token_cap=9000,
        retrieval_response_hash=canonical_hash(retrieval),
    )
    assert [target for plan in proposal_plans for target in plan.target_ids] == expected
    assert all(plan.request_body["model_config"]["max_output_tokens"] == plan.max_output_tokens for plan in proposal_plans)


def test_probe_04_small_output_cap_fails_instead_of_truncating() -> None:
    huge = {"targets": [{"target_occurrence_id": "x" * 20000, "status": "unknown"}]}
    with pytest.raises(DraftBibleError, match="exceeds allowed"):
        select_output_cap(huge, allowed_caps=(256, 512, 3072))


def test_probe_05_clean_reconciliation_is_stable_and_code_minted(bundle: dict[str, Any]) -> None:
    groups, proposals = _target_groups(bundle)
    bundle_hash = canonical_hash(bundle)
    proposal_hash = canonical_hash(proposals)
    first = reconcile_identity_claims(bundle, proposal_rows=proposals)
    second = reconcile_identity_claims(bundle, proposal_rows=deepcopy(proposals))
    assert first == second
    assert canonical_hash(bundle) == bundle_hash and canonical_hash(proposals) == proposal_hash
    assert all(row["entity_id"].startswith("entd_") for row in first["entities"])
    assert len(first["occurrence_to_entity"]) == sum(len(values) for values in groups.values())


def test_probe_06_internal_negative_edge_conflicts(bundle: dict[str, Any]) -> None:
    groups, proposals = _target_groups(bundle)
    values = groups["aster"]
    row = next(item for item in proposals if item["target_occurrence_id"] == values[1])
    row["same_referent_occurrence_ids"] = [values[1]]
    row["different_referent_occurrence_ids"] = [values[0]]
    result = reconcile_identity_claims(bundle, proposal_rows=proposals)
    assert any("internal_different_identity_edge" in row["reasons"] for row in result["identity_conflicts"])


def test_probe_07_unknown_member_cannot_be_pulled_into_entity(bundle: dict[str, Any]) -> None:
    groups, proposals = _target_groups(bundle)
    unknown = groups["aster"][1]
    row = next(item for item in proposals if item["target_occurrence_id"] == unknown)
    row.update(status="unknown", same_referent_occurrence_ids=[], different_referent_occurrence_ids=[], evidence_refs=[])
    result = reconcile_identity_claims(bundle, proposal_rows=proposals)
    assert any("unknown_member_claimed_same" in row["reasons"] for row in result["identity_conflicts"])


def test_probe_08_incompatible_kinds_conflict(bundle: dict[str, Any]) -> None:
    groups, proposals = _target_groups(bundle)
    row = next(item for item in proposals if item["target_occurrence_id"] == groups["aster"][0])
    row["referent_kind"] = "animal"
    result = reconcile_identity_claims(bundle, proposal_rows=proposals)
    assert any("incompatible_referent_kinds" in row["reasons"] for row in result["identity_conflicts"])


def test_probe_09_same_surface_can_map_to_two_entities(bundle: dict[str, Any]) -> None:
    groups, proposals = _target_groups(bundle)
    values = groups["aster"]
    for row in proposals:
        if row["target_occurrence_id"] in values:
            row["same_referent_occurrence_ids"] = [row["target_occurrence_id"]]
            row["different_referent_occurrence_ids"] = [value for value in values if value != row["target_occurrence_id"]]
    result = reconcile_identity_claims(bundle, proposal_rows=proposals)
    aster_entities = [row for row in result["entities"] if row["display_surface"] == "Aster"]
    assert len(aster_entities) == len(values)
    assert not result["identity_conflicts"]


def test_probe_10_endpoint_binding_and_event_pair_closure(bundle: dict[str, Any]) -> None:
    identity, endpoints, pairs = _identity_pipeline(bundle)
    assert endpoints["coverage"]["total"] > 0
    assert pairs["coverage"]["input_events"] == pairs["coverage"]["routed_events"]
    assert not pairs["blocked_events"]
    assert all(row["actor_entity_id"] != row["target_entity_id"] for batch in pairs["pair_batches"] for row in batch["events"])


def test_probe_11_unresolved_endpoint_blocks_event_without_loss(bundle: dict[str, Any]) -> None:
    groups, proposals = _target_groups(bundle)
    cards = {row["occurrence_id"]: row for row in bundle["occurrence_cards"]}
    endpoint = next(value for value in groups["aster"] if cards[value]["occurrence_kind"] == "endpoint")
    row = next(item for item in proposals if item["target_occurrence_id"] == endpoint)
    row.update(status="unknown", same_referent_occurrence_ids=[], different_referent_occurrence_ids=[], evidence_refs=[])
    for other in proposals:
        if endpoint in other["same_referent_occurrence_ids"]:
            other["same_referent_occurrence_ids"].remove(endpoint)
    identity = reconcile_identity_claims(bundle, proposal_rows=proposals)
    endpoints = bind_endpoints(bundle, identity)
    pairs = build_final_pair_batches(bundle, endpoints)
    assert pairs["coverage"]["input_events"] == pairs["coverage"]["routed_events"] + pairs["coverage"]["blocked_events"]
    assert pairs["blocked_events"]


def test_probe_12_phase_validator_accounts_all_events(bundle: dict[str, Any], tmp_path: Path) -> None:
    _identity, _endpoints, pairs = _identity_pipeline(bundle)
    batch = pairs["pair_batches"][0]
    good = _phase_response(batch)
    assert validate_phase_draft_response(good, pair_batch=batch, bundle=bundle)["considered_event_ids"] == batch["event_ids"]
    plans = plan_phase_draft_calls(
        pairs,
        prompt_text=load_phase_draft_prompt(DESIGN_DOC),
        provider="recorded",
        model_config={"model": "gpt-5.4", "max_output_tokens": 12288},
        upstream_lineage_hashes={"bundle_manifest_hash": bundle["bundle_manifest_hash"]},
        prompt_token_cap=14000,
    )
    assert plans and plans[0]["event_ids"] == batch["event_ids"]
    assert plans[0]["request_body"]["model_config"]["max_output_tokens"] == plans[0]["max_output_tokens"]
    request = render_phase_draft_request(
        batch,
        prompt_text=load_phase_draft_prompt(DESIGN_DOC),
        provider="recorded",
        model_config={"model": "gpt-5.4", "max_output_tokens": plans[0]["max_output_tokens"]},
        upstream_lineage_hashes={"bundle_manifest_hash": bundle["bundle_manifest_hash"]},
    )
    callback_calls = []

    def callback(_messages: list[dict[str, Any]], _meta: dict[str, Any], bypass: bool) -> dict[str, Any]:
        callback_calls.append(bypass)
        return {
            "response": good,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0},
            "provider": "recorded",
            "model": "gpt-5.4",
            "cache_key": "recorded-phase",
        }

    reports = tmp_path / "reports"
    executed = execute_phase_draft_request(
        request,
        pair_batch=batch,
        bundle=bundle,
        request_llm=callback,
        out_dir=reports / "literary_m4f_s5c_draft_bible" / "phase-good",
        reports_root=reports,
    )
    assert executed["normalized_response"]["considered_event_ids"] == batch["event_ids"]
    assert callback_calls == [False]
    bad = deepcopy(good)
    bad["considered_event_ids"] = bad["considered_event_ids"][:-1]
    with pytest.raises(DraftBibleError, match="set-equal"):
        validate_phase_draft_response(bad, pair_batch=batch, bundle=bundle)
    semantic_calls = []

    def semantic_bad(_messages: list[dict[str, Any]], _meta: dict[str, Any], bypass: bool) -> dict[str, Any]:
        semantic_calls.append(bypass)
        return {
            "response": bad,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0},
            "provider": "recorded",
            "model": "gpt-5.4",
            "cache_key": "recorded-phase-bad",
        }

    with pytest.raises(DraftBibleError, match="set-equal"):
        execute_phase_draft_request(
            request,
            pair_batch=batch,
            bundle=bundle,
            request_llm=semantic_bad,
            out_dir=reports / "literary_m4f_s5c_draft_bible" / "phase-bad",
            reports_root=reports,
        )
    assert semantic_calls == [False]


def test_probe_13_phase_support_and_inference_basis_are_load_bearing(bundle: dict[str, Any]) -> None:
    _identity, _endpoints, pairs = _identity_pipeline(bundle)
    batch = pairs["pair_batches"][0]
    bad = _phase_response(batch)
    bad["event_dispositions"][0]["outcomes"] = ["no_change"]
    with pytest.raises(DraftBibleError, match="phase_support"):
        validate_phase_draft_response(bad, pair_batch=batch, bundle=bundle)
    fact = _phase_response(batch)
    event = batch["events"][0]
    fact["event_dispositions"][0]["outcomes"] = ["phase_support", "fact_support"]
    fact["relation_facts"] = [
        {
            "subject_ref": batch["pair"]["a_entity_id"],
            "predicate_code": "guest_of",
            "object_ref": batch["pair"]["b_entity_id"],
            "source_event_id": event["event_id"],
            "evidence_quote": event["evidence_quote"],
            "inference_basis": "derived",
        }
    ]
    validated = validate_phase_draft_response(fact, pair_batch=batch, bundle=bundle)
    assert validated["relation_facts"][0]["inference_basis"] == "derived"


def test_probe_14_retain_history_and_model_omitted_pair(bundle: dict[str, Any]) -> None:
    identity, endpoints, pairs = _identity_pipeline(bundle)
    batch = pairs["pair_batches"][0]
    response = validate_phase_draft_response(_phase_response(batch), pair_batch=batch, bundle=bundle)
    first = consolidate_diagnostic_draft(bundle, identity=identity, endpoint_bindings=endpoints, pair_batches=pairs, phase_responses=[response])
    second = consolidate_diagnostic_draft(
        bundle,
        identity=identity,
        endpoint_bindings=endpoints,
        pair_batches=pairs,
        phase_responses=[],
        prior_draft=first,
    )
    assert second["relation_phases"] == first["relation_phases"]
    assert any(row["disposition"] == "model_omitted_pair" for row in second["pair_dispositions"])


def test_probe_15_overlapping_replay_surfaces_conflict(bundle: dict[str, Any]) -> None:
    identity, endpoints, pairs = _identity_pipeline(bundle)
    batch = pairs["pair_batches"][0]
    response = validate_phase_draft_response(_phase_response(batch), pair_batch=batch, bundle=bundle)
    first = consolidate_diagnostic_draft(bundle, identity=identity, endpoint_bindings=endpoints, pair_batches=pairs, phase_responses=[response])
    changed = deepcopy(response)
    changed.pop("validated_response_hash")
    changed["relation_phases"][0]["phase_label"] = "allied"
    changed = validate_phase_draft_response(changed, pair_batch=batch, bundle=bundle)
    second = consolidate_diagnostic_draft(
        bundle,
        identity=identity,
        endpoint_bindings=endpoints,
        pair_batches=pairs,
        phase_responses=[changed],
        prior_draft=first,
    )
    assert len(second["relation_phases"]) == 2
    assert second["relation_conflicts"]
    tampered = deepcopy(first)
    tampered["relation_phases"][0]["phase_label"] = "hostile"
    tampered.pop("artifact_hash")
    tampered["artifact_hash"] = canonical_hash(tampered)
    with pytest.raises(DraftBibleError, match="item id mismatch"):
        consolidate_diagnostic_draft(
            bundle,
            identity=identity,
            endpoint_bindings=endpoints,
            pair_batches=pairs,
            phase_responses=[],
            prior_draft=tampered,
        )


def test_probe_16_writer_is_isolated_and_artifact_is_nonruntime(bundle: dict[str, Any], tmp_path: Path) -> None:
    identity, endpoints, pairs = _identity_pipeline(bundle)
    artifact = consolidate_diagnostic_draft(bundle, identity=identity, endpoint_bindings=endpoints, pair_batches=pairs, phase_responses=[])
    assert artifact["runtime_consumable"] is False
    reports = tmp_path / "reports"
    out = reports / "literary_m4f_s5c_draft_bible" / "run-1"
    manifest = write_draft_artifacts(artifact, bundle=bundle, out_dir=out, reports_root=reports)
    assert manifest["runtime_consumable"] is False
    with pytest.raises(DraftBibleError, match="dedicated"):
        assert_draft_output_root(reports / "production_story_bible" / "run-1", reports_root=reports)


def test_probe_17_preflight_separates_exact_upper_and_reserve() -> None:
    report = build_preflight_report(
        [
            {"call_id": "b0", "stage": "b0", "quota_bucket_id": "mini-key-1", "prompt_tokens_exact_now": 100, "deterministic_prompt_upper": 120, "completion_reserve": 50},
            {"call_id": "b4", "stage": "identity", "quota_bucket_id": "gpt-key-1", "deterministic_prompt_upper": 800, "completion_reserve": 3072},
        ]
    )
    assert report["calls"][0]["estimate_class"] == "exact_now"
    assert report["calls"][1]["estimate_class"] == "deterministic_upper"
    assert report["bucket_totals"]["gpt-key-1"]["reserved_total"] == 3872
    gate = gate_preflight_budget(
        report,
        used_today_by_bucket={"mini-key-1": 0, "gpt-key-1": 100},
        daily_cap_by_bucket={"mini-key-1": 2_250_000, "gpt-key-1": 225_000},
    )
    assert gate["allowed"] is True
    with pytest.raises(DraftBibleError, match="quota gate rejected"):
        gate_preflight_budget(
            report,
            used_today_by_bucket={"mini-key-1": 0, "gpt-key-1": 224_000},
            daily_cap_by_bucket={"mini-key-1": 2_250_000, "gpt-key-1": 225_000},
        )
    with pytest.raises(DraftBibleError, match="exceed"):
        build_preflight_report([{"call_id": "x", "stage": "x", "quota_bucket_id": "k", "prompt_tokens_exact_now": 20, "deterministic_prompt_upper": 10, "completion_reserve": 1}])


def test_probe_18_recorded_apply_writes_full_diagnostic_and_e9_manifest(bundle: dict[str, Any], tmp_path: Path) -> None:
    _groups, proposals = _target_groups(bundle)
    identity = reconcile_identity_claims(bundle, proposal_rows=proposals)
    endpoints = bind_endpoints(bundle, identity)
    pairs = build_final_pair_batches(bundle, endpoints)
    responses = [_phase_response(batch) for batch in pairs["pair_batches"]]
    reports = tmp_path / "reports"
    out = reports / "literary_m4f_s5c_draft_bible" / "recorded"
    result = run_recorded_draft_apply(
        bundle,
        proposal_rows=proposals,
        phase_response_rows=responses,
        out_dir=out,
        reports_root=reports,
        defect_reconciliation_path=E9_DEFECTS,
        untrusted_frame_proposal={"trust": "untrusted", "response_status": "proposed", "segments": []},
        preflight_report=build_preflight_report(
            [{"call_id": "recorded", "stage": "recorded", "quota_bucket_id": "none", "prompt_tokens_exact_now": 0, "completion_reserve": 0}]
        ),
    )
    repeat = run_recorded_draft_apply(
        bundle,
        proposal_rows=deepcopy(proposals),
        phase_response_rows=deepcopy(responses),
        out_dir=reports / "literary_m4f_s5c_draft_bible" / "recorded-repeat",
        reports_root=reports,
        defect_reconciliation_path=E9_DEFECTS,
        untrusted_frame_proposal={"trust": "untrusted", "response_status": "proposed", "segments": []},
        preflight_report=build_preflight_report(
            [{"call_id": "recorded", "stage": "recorded", "quota_bucket_id": "none", "prompt_tokens_exact_now": 0, "completion_reserve": 0}]
        ),
    )
    assert repeat["artifact"]["artifact_hash"] == result["artifact"]["artifact_hash"]
    assert result["artifact"]["artifact_status"] == "diagnostic_draft"
    assert len(result["eyeball_manifest"]["rows"]) == 27
    assert len(result["run_manifest"]["chapter_artifacts"]) == 2
    for channel in ("glossary", "translator_facts", "motifs", "unresolved_threads", "observed_address_evidence"):
        assert result["artifact"][channel], channel
    assert (out / "book_diagnostic_draft.json").exists()
    chapter_one = json.loads((out / "bk_ch01_diagnostic_draft.json").read_text(encoding="utf-8"))
    assert {row["chapter_id"] for row in chapter_one["relation_events"]} == {"bk_ch01"}
    assert "bk_ch02" not in canonical_json(chapter_one["relation_events"])
    cards = {row["occurrence_id"]: row for row in bundle["occurrence_cards"]}
    assert all(
        cards[claim["source_occurrence_id"]]["chapter_id"] == "bk_ch01"
        for entity in chapter_one["entities"]
        for claim in entity["canonical_surface_guesses_advisory"]
    )
