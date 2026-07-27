from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.builder_v3_pipeline import _build_request
from pipeline.literary.chapter_registry_v1 import (
    BUILDER_IDENTITY_MODE,
    DEFAULT_BUILDER_IDENTITY_MODE,
    ChapterRegistryStoreV1,
    ChapterWorkingSetV1,
    FakeRegistryToolBroker,
    PreparedChapterCommitV1,
    RegistryContractError,
    RegistryStaleParentError,
    SyntheticRegistryExecutor,
    build_registry_generation,
    chapter_block_views,
    empty_registry_snapshot,
    finalize_working_set,
    load_snapshot_from_handle,
    render_extract_request,
    render_resolution_request,
    run_synthetic_registry_chapter,
    select_candidate_cards,
    validate_extract_response,
    validate_resolution_response,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _chapter(chapter_id: str, texts: list[str]) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "blocks": [
            {
                "block_id": f"{chapter_id}_b{index:03d}",
                "block_type": "paragraph",
                "clean_text": text,
            }
            for index, text in enumerate(texts, start=1)
        ],
    }


def _windows(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    views = chapter_block_views(chapter)
    return [
        {
            "window_id": f"w{index:02d}",
            "active_blocks": [block],
            "context_only_tail": views[max(0, index - 2) : index - 1],
        }
        for index, block in enumerate(views, start=1)
    ]


def _orientation(
    chapter_id: str,
    *,
    checklist: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "gist": "Several people arrive, while an animal remains nearby.",
        "setting_notes": [],
        "narrator_hypotheses": [],
        "salient_surface_checklist": list(checklist or []),
    }


def _context_request(index: int, surface: str, block_id: str) -> dict[str, Any]:
    return {
        "mention_index": index,
        "decision_field": "identity_binding",
        "surface": surface,
        "reason": "No prior candidate safely establishes identity.",
        "block_id": block_id,
        "needed_evidence": ["candidate_entities"],
    }


def _pending_proposal() -> dict[str, Any]:
    return {
        "operation": "pending",
        "target_entity_id": None,
        "canonical_surface_candidate": None,
        "alias_surface": None,
        "reason_code": "ambiguous",
        "binding_evidence_quote": None,
        "binding_anchor_text": None,
        "binding_block_id": None,
        "binding_occurrence_hint": None,
        "retrieval_trace_ids": [],
    }


def _reinforce_proposal(
    entity_id: str, *, surface: str, block_id: str, evidence_quote: str
) -> dict[str, Any]:
    return {
        "operation": "reinforce_existing",
        "target_entity_id": entity_id,
        "canonical_surface_candidate": None,
        "alias_surface": None,
        "reason_code": "direct_name",
        "binding_evidence_quote": evidence_quote,
        "binding_anchor_text": surface,
        "binding_block_id": block_id,
        "binding_occurrence_hint": None,
        "retrieval_trace_ids": [],
    }


def _mention(
    *,
    surface: str,
    kind: str,
    block_id: str,
    evidence_quote: str,
    needs_context: bool = True,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "mention_type": "name" if surface[:1].isupper() else "descriptor",
        "referent_kind_claim": kind,
        "anchor_text": surface,
        "evidence_quote": evidence_quote,
        "block_id": block_id,
        "occurrence_hint": None,
        "decision_status": "needs_context" if needs_context else "decided",
        "identity_proposal": None if needs_context else _pending_proposal(),
        "context_requests": (
            [_context_request(0, surface, block_id)] if needs_context else []
        ),
    }


def _extract_response(
    request: Any,
    mentions: list[dict[str, Any]],
) -> dict[str, Any]:
    adjusted = []
    for index, mention in enumerate(mentions):
        row = dict(mention)
        row["context_requests"] = [
            {**item, "mention_index": index} for item in mention["context_requests"]
        ]
        adjusted.append(row)
    return {
        "chapter_id": request.chapter_id,
        "window_block_ids": [
            row["block_id"] for row in request.sections["active_window_blocks"]
        ],
        "context_only_used": False,
        "character_mentions": adjusted,
        "glossary_candidates": [],
    }


def _partition_each_resolver(request: Any) -> dict[str, Any]:
    evidence_by_occurrence = {
        str(row["occurrence_id"]): str(row["evidence_id"])
        for row in request.sections["evidence_items"]
    }
    trace_by_block = {
        str(row["arguments"]["block_id"]): str(row["trace_id"])
        for row in request.sections["tool_result_manifests"]
        if row["tool_name"] == "find_entity_candidates"
    }
    candidate_entity_ids = {
        str(row["entity_id"])
        for row in request.sections["candidate_cards"]
        if row.get("entity_id")
    }
    candidate_pending_ids = {
        str(row["pending_id"])
        for row in request.sections["open_pending_cards"]
        if row.get("pending_id")
    }
    for trace in request.sections["tool_result_manifests"]:
        for result in trace.get("results") or []:
            if result.get("entity_id"):
                candidate_entity_ids.add(str(result["entity_id"]))
            if result.get("pending_id"):
                candidate_pending_ids.add(str(result["pending_id"]))
    partitions = []
    for occurrence in request.sections["owned_occurrences"]:
        occurrence_id = str(occurrence["occurrence_id"])
        block_id = str(occurrence["anchor"]["block_id"])
        partitions.append(
            {
                "occurrence_ids": [occurrence_id],
                "referent_kind_claim": occurrence["referent_kind_claim"],
                "canonical_surface_candidate": occurrence["surface"],
                "alias_surfaces": [occurrence["surface"]],
                "reason_code": "no_candidate",
                "binding_evidence_refs": [evidence_by_occurrence[occurrence_id]],
                "retrieval_trace_ids": [trace_by_block[block_id]],
                "rejected_candidate_entity_ids": sorted(candidate_entity_ids),
                "rejected_pending_ids": sorted(candidate_pending_ids),
            }
        )
    return {
        "chapter_id": request.chapter_id,
        "request_id": request.request_key,
        "owned_occurrence_ids": [
            row["occurrence_id"] for row in request.sections["owned_occurrences"]
        ],
        "existing_attachments": [],
        "new_partitions": partitions,
        "pending": [],
        "context_requests": [],
    }


def _empty_prepared(lineage: str, chapter_id: str) -> PreparedChapterCommitV1:
    return PreparedChapterCommitV1(
        state_lineage_id=lineage,
        parent_generation_id=None,
        chapter_id=chapter_id,
        entity_revisions=(),
        alias_revisions=(),
        occurrence_records=(),
        presence_rows=(),
        pending_records=(),
    )


def _generation(lineage: str, chapter_id: str) -> Any:
    return build_registry_generation(
        prepared=_empty_prepared(lineage, chapter_id),
        source_manifest_hash=canonical_hash({"chapter": chapter_id}),
        b0_request_fingerprint=canonical_hash({"b0": chapter_id}),
        b1_request_fingerprints=[canonical_hash({"b1": chapter_id})],
        candidate_manifest_hashes=[canonical_hash({"candidates": chapter_id})],
        reconcile_request_fingerprints=[],
    )


def _entity_record(
    entity_id: str,
    *,
    canonical_surface: str,
    aliases: list[tuple[str, dict[str, int] | None, dict[str, int] | None]],
) -> dict[str, Any]:
    observed = {"chapter_order": 0, "block_order": 0, "char_offset": 0}
    return {
        "entity_id": entity_id,
        "referent_kind": "person",
        "runtime_eligibility": "eligible",
        "canonical_surface": canonical_surface,
        "canonical_surface_evidence_refs": [f"occ_{entity_id}"],
        "aliases": [
            {
                "surface": surface,
                "covered_occurrence_ids": [f"occ_{entity_id}_{index}"],
                "surface_observed_from": observed,
                "binding_disclosed_from": None,
                "world_valid_from": valid_from,
                "world_valid_until": valid_until,
                "used_by_entity_ids": None,
                "decision_revision_hash": f"rev_{entity_id}_{index}",
            }
            for index, (surface, valid_from, valid_until) in enumerate(aliases)
        ],
        "status": "active",
        "created_in_scope": "novel_ch00",
        "current_revision_hash": f"rev_{entity_id}",
    }


def _snapshot_with_entities(lineage: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    empty = empty_registry_snapshot(lineage)
    body = {key: value for key, value in empty.items() if key != "snapshot_hash"}
    body["entities"] = entities
    return body | {"snapshot_hash": canonical_hash(body)}


def test_phase_a_end_to_end_uses_frozen_snapshot_recall_and_no_surface_merge(
    tmp_path: Path,
) -> None:
    chapter = _chapter(
        "novel_ch01",
        [
            "A visitor called Rowan entered the hall.",
            "Far away, another child called Rowan waited by the gate.",
            "The old hound Bristle slept beside the fire.",
        ],
    )
    views = chapter_block_views(chapter)
    checklist = [
        {
            "surface": "Bristle",
            "salience_note": "The named hound is locally active.",
            "anchor_text": "Bristle",
            "evidence_quote": views[2]["text"],
            "block_id": views[2]["block_id"],
            "occurrence_hint": None,
        }
    ]

    def targeted(request: Any) -> dict[str, Any]:
        block = request.sections["active_window_blocks"][0]
        return _extract_response(
            request,
            [
                _mention(
                    surface="Bristle",
                    kind="animal",
                    block_id=block["block_id"],
                    evidence_quote=block["text"],
                )
            ],
        )

    scripts = {
        ("b0_orient", "b0:novel_ch01"): _orientation(
            "novel_ch01", checklist=checklist
        ),
        ("b1_extract", "b1:novel_ch01:w01"): lambda request: _extract_response(
            request,
            [
                _mention(
                    surface="Rowan",
                    kind="person",
                    block_id=views[0]["block_id"],
                    evidence_quote=views[0]["text"],
                )
            ],
        ),
        ("b1_extract", "b1:novel_ch01:w02"): lambda request: _extract_response(
            request,
            [
                _mention(
                    surface="Rowan",
                    kind="person",
                    block_id=views[1]["block_id"],
                    evidence_quote=views[1]["text"],
                )
            ],
        ),
        ("b1_extract", "b1:novel_ch01:w03"): lambda request: _extract_response(
            request, []
        ),
        ("b1_targeted", "*"): targeted,
        ("b1_resolve", "*"): _partition_each_resolver,
    }
    executor = SyntheticRegistryExecutor(scripts)
    broker = FakeRegistryToolBroker({}, result_cap=4)
    store = ChapterRegistryStoreV1(tmp_path / "registry")
    parent = empty_registry_snapshot("lineage_demo")
    result = run_synthetic_registry_chapter(
        builder_identity_mode=BUILDER_IDENTITY_MODE,
        design_doc=DESIGN_DOC,
        chapter=chapter,
        chapter_order=0,
        windows=_windows(chapter),
        parent_snapshot=parent,
        executor=executor,
        broker=broker,
        store=store,
        candidate_cap=8,
    )

    b1_calls = [
        row
        for row in result["executor_call_log"]
        if row["role"] in {"b1_extract", "b1_targeted"}
    ]
    assert {row["parent_snapshot_hash"] for row in b1_calls} == {
        parent["snapshot_hash"]
    }
    ordinary = [row for row in b1_calls if row["role"] == "b1_extract"]
    assert all(row["body"]["sections"]["candidate_cards"] == [] for row in ordinary)
    assert all(
        "salient_surface_checklist" not in canonical_json(row["body"])
        for row in ordinary
    )
    targeted_calls = [row for row in b1_calls if row["role"] == "b1_targeted"]
    assert len(targeted_calls) == 1
    target_evidence = targeted_calls[0]["body"]["sections"][
        "targeted_recall_evidence"
    ]
    assert target_evidence["surface"] == "Bristle"
    assert "salience_note" not in target_evidence
    assert not any("entity" in key for key in target_evidence)

    snapshot = result["snapshot"]
    assert len(snapshot["occurrences"]) == 3
    assert len(snapshot["entities"]) == 3
    rowan_entities = [
        row for row in snapshot["entities"] if row["canonical_surface"] == "Rowan"
    ]
    assert len(rowan_entities) == 2
    assert len({row["entity_id"] for row in rowan_entities}) == 2
    assert {row["referent_kind"] for row in snapshot["entities"]} == {
        "person",
        "animal",
    }
    for entity in snapshot["entities"]:
        for alias in entity["aliases"]:
            assert alias["surface_observed_from"] is not None
            assert alias["binding_disclosed_from"] is None
            assert alias["world_valid_from"] is None
            assert alias["world_valid_until"] is None

    replay = run_synthetic_registry_chapter(
        builder_identity_mode=BUILDER_IDENTITY_MODE,
        design_doc=DESIGN_DOC,
        chapter=chapter,
        chapter_order=0,
        windows=_windows(chapter),
        parent_snapshot=parent,
        executor=SyntheticRegistryExecutor(scripts),
        broker=FakeRegistryToolBroker({}, result_cap=4),
        store=ChapterRegistryStoreV1(tmp_path / "registry_replay"),
        candidate_cap=8,
    )
    assert canonical_json(replay["generation"]) == canonical_json(result["generation"])


def test_candidate_selection_is_story_as_of_and_manifest_is_auditable() -> None:
    expired_at_chapter_one = {
        "chapter_order": 1,
        "block_order": 0,
        "char_offset": 0,
    }
    active_from_chapter_one = {
        "chapter_order": 1,
        "block_order": 0,
        "char_offset": 0,
    }
    parent = _snapshot_with_entities(
        "lineage_as_of",
        [
            _entity_record(
                "ent_mira",
                canonical_surface="Mira",
                aliases=[
                    ("Old title", None, expired_at_chapter_one),
                    ("Current title", active_from_chapter_one, None),
                ],
            )
        ],
    )
    old_block = chapter_block_views(_chapter("novel_ch02", ["Old title entered."]))[0]
    current_block = chapter_block_views(
        _chapter("novel_ch02", ["Current title entered."])
    )[0]

    expired = select_candidate_cards(
        active_blocks=[old_block], snapshot=parent, chapter_order=1, cap=4
    )
    assert expired["candidate_cards"] == []
    selected = select_candidate_cards(
        active_blocks=[current_block], snapshot=parent, chapter_order=1, cap=4
    )
    manifest = selected["candidate_selection_manifest"]
    assert manifest["as_of_chapter_order"] == 1
    assert manifest["active_block_orders"] == [0]
    assert manifest["selection_universe_hash"] == canonical_hash(manifest["rows"])
    assert manifest["selected_entity_ids"] == ["ent_mira"]
    assert [row["surface"] for row in selected["candidate_cards"][0]["aliases"]] == [
        "Current title"
    ]

    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch02",
        chapter_order=1,
        request_key="b1:novel_ch02:as_of",
        window_id="as_of",
        active_blocks=[current_block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    assert request.body()["prompt_sha256"] == sha256(
        request.system_prompt.encode("utf-8")
    ).hexdigest()


def test_literal_candidate_scan_does_not_match_inside_another_name() -> None:
    parent = _snapshot_with_entities(
        "lineage_token_boundary",
        [_entity_record("ent_ann", canonical_surface="Ann", aliases=[])],
    )
    block = chapter_block_views(_chapter("novel_ch02", ["Anna entered."]))[0]
    selection = select_candidate_cards(
        active_blocks=[block], snapshot=parent, chapter_order=1, cap=4
    )
    assert selection["candidate_cards"] == []

    split_parent = _snapshot_with_entities(
        "lineage_block_boundary",
        [
            _entity_record(
                "ent_catherine",
                canonical_surface="Catherine Linton",
                aliases=[],
            )
        ],
    )
    split_blocks = chapter_block_views(
        _chapter("novel_ch02", ["Catherine", "Linton entered."])
    )
    split_selection = select_candidate_cards(
        active_blocks=split_blocks,
        snapshot=split_parent,
        chapter_order=1,
        cap=4,
    )
    assert split_selection["candidate_cards"] == []


def test_new_partition_requires_model_rejection_not_code_compatibility() -> None:
    chapter = _chapter("novel_ch01", ["Another Rowan entered."])
    block = chapter_block_views(chapter)[0]
    parent = _snapshot_with_entities(
        "lineage_semantic_reject",
        [_entity_record("ent_existing", canonical_surface="Rowan", aliases=[])],
    )
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=1,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    validated = validate_extract_response(
        _extract_response(
            request,
            [
                _mention(
                    surface="Rowan",
                    kind="person",
                    block_id=block["block_id"],
                    evidence_quote=block["text"],
                )
            ],
        ),
        request=request,
    )
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_semantic_reject",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=1,
    )
    working.stage_extract(request=request, validated=validated)
    occurrence_id = next(iter(working.staged_occurrences))
    broker = FakeRegistryToolBroker(
        {("find_entity_candidates", "Rowan"): [{"entity_id": "ent_existing"}]},
        result_cap=4,
    )
    trace = broker.execute(
        tool_name="find_entity_candidates",
        lookup_key="Rowan",
        arguments={"surface": "Rowan", "block_id": block["block_id"]},
        snapshot_hash=parent["snapshot_hash"],
        as_of_position=working.staged_occurrences[occurrence_id]["story_position"],
        allowed_entity_ids=["ent_existing"],
        allowed_pending_ids=[],
        round_no=1,
    )
    assert "compatible_result_count" not in trace
    resolution_request = render_resolution_request(
        design_doc=DESIGN_DOC,
        working_set=working,
        owned_occurrence_ids=[occurrence_id],
        candidate_cards=request.sections["candidate_cards"],
        pending_cards=[],
        tool_traces=[trace],
        remaining_tool_rounds=0,
    )
    assert resolution_request.sections["candidate_selection_manifests"]
    response = _partition_each_resolver(resolution_request)
    validated_resolution = validate_resolution_response(
        response, request=resolution_request
    )
    prepared = finalize_working_set(
        working_set=working,
        parent_snapshot=parent,
        resolution=validated_resolution,
    )
    assert len(prepared.entity_revisions) == 1
    assert prepared.entity_revisions[0]["entity_id"] != "ent_existing"

    missing_rejection = _partition_each_resolver(resolution_request)
    missing_rejection["new_partitions"][0]["rejected_candidate_entity_ids"] = []
    with pytest.raises(RegistryContractError, match="full entity candidate universe"):
        validate_resolution_response(missing_rejection, request=resolution_request)


def test_decided_pending_retains_candidates_that_were_actually_rendered() -> None:
    chapter = _chapter("novel_ch01", ["Rowan entered."])
    block = chapter_block_views(chapter)[0]
    parent = _snapshot_with_entities(
        "lineage_pending_provenance",
        [_entity_record("ent_existing", canonical_surface="Rowan", aliases=[])],
    )
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=1,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    validated = validate_extract_response(
        _extract_response(
            request,
            [
                _mention(
                    surface="Rowan",
                    kind="person",
                    block_id=block["block_id"],
                    evidence_quote=block["text"],
                    needs_context=False,
                )
            ],
        ),
        request=request,
    )
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_pending_provenance",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=1,
    )
    working.stage_extract(request=request, validated=validated)
    prepared = finalize_working_set(
        working_set=working, parent_snapshot=parent, resolution=None
    )
    assert prepared.pending_records[0]["candidate_entity_refs"] == ["ent_existing"]


def test_round_zero_reinforcement_must_bind_its_own_fresh_occurrence() -> None:
    chapter = _chapter("novel_ch01", ["Mira entered."])
    block = chapter_block_views(chapter)[0]
    parent = _snapshot_with_entities(
        "lineage_reinforce",
        [_entity_record("ent_mira", canonical_surface="Mira", aliases=[])],
    )
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=1,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    mention = _mention(
        surface="Mira",
        kind="person",
        block_id=block["block_id"],
        evidence_quote=block["text"],
        needs_context=False,
    )
    mention["identity_proposal"] = _reinforce_proposal(
        "ent_mira",
        surface="Mira",
        block_id=block["block_id"],
        evidence_quote=block["text"],
    )
    validated = validate_extract_response(
        _extract_response(request, [mention]), request=request
    )
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_reinforce",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=1,
    )
    working.stage_extract(request=request, validated=validated)
    prepared = finalize_working_set(
        working_set=working, parent_snapshot=parent, resolution=None
    )
    assert prepared.occurrence_records[0]["entity_or_pending_ref"] == "ent_mira"

    wrong_evidence = _extract_response(request, [mention])
    wrong_evidence["character_mentions"][0]["identity_proposal"][
        "binding_anchor_text"
    ] = "entered"
    with pytest.raises(RegistryContractError, match="does not own its mention"):
        validate_extract_response(wrong_evidence, request=request)


def test_broker_dispatch_honors_requested_evidence_and_keeps_candidate_search(
    tmp_path: Path,
) -> None:
    chapter = _chapter("novel_ch01", ["Mira entered."])
    block = chapter_block_views(chapter)[0]

    def extract(request: Any) -> dict[str, Any]:
        mention = _mention(
            surface="Mira",
            kind="person",
            block_id=block["block_id"],
            evidence_quote=block["text"],
        )
        mention["context_requests"][0]["needed_evidence"] = [
            "wider_source_context"
        ]
        return _extract_response(request, [mention])

    broker = FakeRegistryToolBroker({}, result_cap=4)
    result = run_synthetic_registry_chapter(
        builder_identity_mode=BUILDER_IDENTITY_MODE,
        design_doc=DESIGN_DOC,
        chapter=chapter,
        chapter_order=0,
        windows=_windows(chapter),
        parent_snapshot=empty_registry_snapshot("lineage_dispatch"),
        executor=SyntheticRegistryExecutor(
            {
                ("b0_orient", "*"): _orientation("novel_ch01"),
                ("b1_extract", "*"): extract,
                ("b1_resolve", "*"): _partition_each_resolver,
            }
        ),
        broker=broker,
        store=ChapterRegistryStoreV1(tmp_path / "registry"),
        candidate_cap=4,
    )
    assert result["tool_trace_count"] == 2
    assert {row["tool_name"] for row in broker.call_log} == {
        "find_entity_candidates",
        "get_source_context",
    }


def test_same_exact_occurrence_deduplicates_and_keeps_both_request_provenances() -> None:
    chapter = _chapter("novel_ch01", ["Mira entered. Mira smiled."])
    block = chapter_block_views(chapter)[0]
    parent = empty_registry_snapshot("lineage_dedupe")
    requests = [
        render_extract_request(
            design_doc=DESIGN_DOC,
            chapter_id="novel_ch01",
            chapter_order=0,
            request_key=f"b1:novel_ch01:w{index}",
            window_id=f"w{index}",
            active_blocks=[block],
            context_only_tail=[],
            snapshot=parent,
            candidate_cap=4,
        )
        for index in (1, 2)
    ]
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_dedupe",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=0,
    )
    for request in requests:
        response = _extract_response(
            request,
            [
                _mention(
                    surface="Mira",
                    kind="person",
                    block_id=block["block_id"],
                    evidence_quote="Mira entered.",
                )
            ],
        )
        validated = validate_extract_response(response, request=request)
        working.stage_extract(request=request, validated=validated)
    assert len(working.staged_occurrences) == 1
    occurrence = next(iter(working.staged_occurrences.values()))
    assert occurrence["source_window_ids"] == ["w1", "w2"]
    assert len(occurrence["source_request_fingerprints"]) == 2


def test_foreign_candidate_is_fatal_and_overflow_cannot_be_decided() -> None:
    chapter = _chapter("novel_ch01", ["Rowan entered."])
    block = chapter_block_views(chapter)[0]
    parent = empty_registry_snapshot("lineage_foreign")
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=0,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    mention = _mention(
        surface="Rowan",
        kind="person",
        block_id=block["block_id"],
        evidence_quote=block["text"],
        needs_context=False,
    )
    mention["identity_proposal"] = _reinforce_proposal(
        "ent_foreign",
        surface="Rowan",
        block_id=block["block_id"],
        evidence_quote=block["text"],
    )
    with pytest.raises(RegistryContractError, match="foreign candidate"):
        validate_extract_response(_extract_response(request, [mention]), request=request)

    def entity(entity_id: str) -> dict[str, Any]:
        position = {"chapter_order": 0, "block_order": 0, "char_offset": 0}
        return {
            "entity_id": entity_id,
            "referent_kind": "person",
            "runtime_eligibility": "eligible",
            "canonical_surface": "Rowan",
            "canonical_surface_evidence_refs": [f"occ_{entity_id}"],
            "aliases": [
                {
                    "surface": "Rowan",
                    "covered_occurrence_ids": [f"occ_{entity_id}"],
                    "surface_observed_from": position,
                    "binding_disclosed_from": None,
                    "world_valid_from": None,
                    "world_valid_until": None,
                    "used_by_entity_ids": None,
                    "decision_revision_hash": f"rev_{entity_id}",
                }
            ],
            "status": "active",
            "created_in_scope": "novel_ch00",
            "current_revision_hash": f"rev_{entity_id}",
        }

    overflow_body = {
        **{key: value for key, value in parent.items() if key != "snapshot_hash"},
        "entities": [entity("ent_a"), entity("ent_b")],
    }
    overflow = overflow_body | {"snapshot_hash": canonical_hash(overflow_body)}
    selection = select_candidate_cards(
        active_blocks=[block], snapshot=overflow, chapter_order=1, cap=1
    )
    assert selection["candidate_selection_manifest"]["overflow"] is True
    overflow_request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=0,
        request_key="b1:novel_ch01:overflow",
        window_id="overflow",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=overflow,
        candidate_cap=1,
    )
    overflow_mention = dict(mention)
    overflow_mention["identity_proposal"] = _reinforce_proposal(
        "ent_a",
        surface="Rowan",
        block_id=block["block_id"],
        evidence_quote=block["text"],
    )
    with pytest.raises(RegistryContractError, match="overflowed candidates"):
        validate_extract_response(
            _extract_response(overflow_request, [overflow_mention]),
            request=overflow_request,
        )


def test_candidate_overflow_can_finish_as_pending_and_broker_is_bounded() -> None:
    chapter = _chapter("novel_ch01", ["Rowan entered."])
    block = chapter_block_views(chapter)[0]
    parent = empty_registry_snapshot("lineage_overflow")
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=0,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=1,
    )
    validated = validate_extract_response(
        _extract_response(
            request,
            [
                _mention(
                    surface="Rowan",
                    kind="person",
                    block_id=block["block_id"],
                    evidence_quote=block["text"],
                )
            ],
        ),
        request=request,
    )
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_overflow",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=0,
    )
    working.stage_extract(request=request, validated=validated)
    occurrence_id = next(iter(working.staged_occurrences))
    broker = FakeRegistryToolBroker(
        {
            ("find_entity_candidates", "Rowan"): [
                {"entity_id": "ent_a"},
                {"entity_id": "ent_b"},
            ]
        },
        result_cap=1,
    )
    trace = broker.execute(
        tool_name="find_entity_candidates",
        lookup_key="Rowan",
        arguments={"surface": "Rowan"},
        snapshot_hash=parent["snapshot_hash"],
        as_of_position={"chapter_order": 0, "block_order": 0, "char_offset": 0},
        allowed_entity_ids=["ent_a", "ent_b"],
        allowed_pending_ids=[],
        round_no=1,
    )
    assert trace["overflow"] is True and trace["complete_search"] is False
    assert trace["as_of_position"] == {
        "chapter_order": 0,
        "block_order": 0,
        "char_offset": 0,
    }
    with pytest.raises(RegistryContractError, match="round exceeds"):
        broker.execute(
            tool_name="find_entity_candidates",
            lookup_key="Rowan",
            arguments={},
            snapshot_hash=parent["snapshot_hash"],
            as_of_position={"chapter_order": 0, "block_order": 0, "char_offset": 0},
            allowed_entity_ids=["ent_a", "ent_b"],
            allowed_pending_ids=[],
            round_no=3,
        )
    with pytest.raises(RegistryContractError, match="non-allowlisted"):
        broker.execute(
            tool_name="sql",
            lookup_key="Rowan",
            arguments={},
            snapshot_hash=parent["snapshot_hash"],
            as_of_position={"chapter_order": 0, "block_order": 0, "char_offset": 0},
            allowed_entity_ids=[],
            allowed_pending_ids=[],
            round_no=1,
        )
    foreign_broker = FakeRegistryToolBroker(
        {("find_entity_candidates", "Rowan"): [{"entity_id": "ent_foreign"}]},
        result_cap=2,
    )
    with pytest.raises(RegistryContractError, match="foreign entity"):
        foreign_broker.execute(
            tool_name="find_entity_candidates",
            lookup_key="Rowan",
            arguments={"surface": "Rowan"},
            snapshot_hash=parent["snapshot_hash"],
            as_of_position={"chapter_order": 0, "block_order": 0, "char_offset": 0},
            allowed_entity_ids=[],
            allowed_pending_ids=[],
            round_no=1,
        )
    resolution_request = render_resolution_request(
        design_doc=DESIGN_DOC,
        working_set=working,
        owned_occurrence_ids=[occurrence_id],
        candidate_cards=[],
        pending_cards=[],
        tool_traces=[trace],
        remaining_tool_rounds=0,
    )
    evidence_id = resolution_request.sections["evidence_items"][0]["evidence_id"]
    response = {
        "chapter_id": "novel_ch01",
        "request_id": resolution_request.request_key,
        "owned_occurrence_ids": [occurrence_id],
        "existing_attachments": [],
        "new_partitions": [],
        "pending": [
            {
                "occurrence_id": occurrence_id,
                "reason_code": "reconcile_cap",
                "evidence_refs": [evidence_id],
                "retrieval_trace_ids": [trace["trace_id"]],
            }
        ],
        "context_requests": [],
    }
    validated_resolution = validate_resolution_response(
        response, request=resolution_request
    )
    assert validated_resolution["pending"][0]["reason_code"] == "reconcile_cap"


def test_pending_survives_commit_and_is_visible_to_next_chapter_selection(
    tmp_path: Path,
) -> None:
    chapter = _chapter("novel_ch01", ["the stranger waited by the road."])
    block = chapter_block_views(chapter)[0]
    scripts = {
        ("b0_orient", "*"): _orientation("novel_ch01"),
        ("b1_extract", "*"): lambda request: _extract_response(
            request,
            [
                _mention(
                    surface="the stranger",
                    kind="unknown",
                    block_id=block["block_id"],
                    evidence_quote=block["text"],
                    needs_context=False,
                )
            ],
        ),
    }
    store = ChapterRegistryStoreV1(tmp_path / "registry")
    result = run_synthetic_registry_chapter(
        builder_identity_mode=BUILDER_IDENTITY_MODE,
        design_doc=DESIGN_DOC,
        chapter=chapter,
        chapter_order=0,
        windows=_windows(chapter),
        parent_snapshot=empty_registry_snapshot("lineage_pending"),
        executor=SyntheticRegistryExecutor(scripts),
        broker=FakeRegistryToolBroker({}, result_cap=2),
        store=store,
        candidate_cap=4,
    )
    snapshot = result["snapshot"]
    assert len(snapshot["pending_records"]) == 1
    assert snapshot["pending_records"][0]["status"] == "open"
    next_block = chapter_block_views(
        _chapter("novel_ch02", ["Again, the stranger waited."])
    )[0]
    selection = select_candidate_cards(
        active_blocks=[next_block], snapshot=snapshot, chapter_order=1, cap=4
    )
    assert selection["candidate_selection_manifest"]["selected_pending_ids"] == [
        snapshot["pending_records"][0]["pending_id"]
    ]


def test_resolver_requires_exact_cover_and_does_not_accept_model_partition_id() -> None:
    chapter = _chapter("novel_ch01", ["Mira entered."])
    block = chapter_block_views(chapter)[0]
    parent = empty_registry_snapshot("lineage_cover")
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=0,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    validated = validate_extract_response(
        _extract_response(
            request,
            [
                _mention(
                    surface="Mira",
                    kind="person",
                    block_id=block["block_id"],
                    evidence_quote=block["text"],
                )
            ],
        ),
        request=request,
    )
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_cover",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=0,
    )
    working.stage_extract(request=request, validated=validated)
    occurrence_id = next(iter(working.staged_occurrences))
    broker = FakeRegistryToolBroker({}, result_cap=2)
    trace = broker.execute(
        tool_name="find_entity_candidates",
        lookup_key="Mira",
        arguments={"surface": "Mira", "block_id": block["block_id"]},
        snapshot_hash=parent["snapshot_hash"],
        as_of_position={"chapter_order": 0, "block_order": 0, "char_offset": 0},
        allowed_entity_ids=[],
        allowed_pending_ids=[],
        round_no=1,
    )
    resolution_request = render_resolution_request(
        design_doc=DESIGN_DOC,
        working_set=working,
        owned_occurrence_ids=[occurrence_id],
        candidate_cards=[],
        pending_cards=[],
        tool_traces=[trace],
        remaining_tool_rounds=0,
    )
    response = _partition_each_resolver(resolution_request)
    response["new_partitions"][0]["local_partition_id"] = "model_minted"
    with pytest.raises(RegistryContractError, match="fields mismatch"):
        validate_resolution_response(response, request=resolution_request)
    response = _partition_each_resolver(resolution_request)
    response["pending"] = [
        {
            "occurrence_id": occurrence_id,
            "reason_code": "insufficient_evidence",
            "evidence_refs": [
                resolution_request.sections["evidence_items"][0]["evidence_id"]
            ],
            "retrieval_trace_ids": [trace["trace_id"]],
        }
    ]
    with pytest.raises(RegistryContractError, match="exact-cover"):
        validate_resolution_response(response, request=resolution_request)


def test_resolver_cannot_swap_evidence_between_owned_occurrences() -> None:
    chapter = _chapter("novel_ch01", ["Mira entered.", "Ravel waited."])
    blocks = chapter_block_views(chapter)
    parent = empty_registry_snapshot("lineage_evidence")
    working = ChapterWorkingSetV1(
        state_lineage_id="lineage_evidence",
        chapter_id="novel_ch01",
        parent_generation_id=None,
        parent_snapshot_hash=parent["snapshot_hash"],
        chapter_order=0,
    )
    for index, (surface, block) in enumerate(zip(("Mira", "Ravel"), blocks)):
        request = render_extract_request(
            design_doc=DESIGN_DOC,
            chapter_id="novel_ch01",
            chapter_order=0,
            request_key=f"b1:novel_ch01:w{index}",
            window_id=f"w{index}",
            active_blocks=[block],
            context_only_tail=[],
            snapshot=parent,
            candidate_cap=4,
        )
        validated = validate_extract_response(
            _extract_response(
                request,
                [
                    _mention(
                        surface=surface,
                        kind="person",
                        block_id=block["block_id"],
                        evidence_quote=block["text"],
                    )
                ],
            ),
            request=request,
        )
        working.stage_extract(request=request, validated=validated)
    traces = []
    for occurrence in working.staged_occurrences.values():
        traces.append(
            FakeRegistryToolBroker({}, result_cap=2).execute(
                tool_name="find_entity_candidates",
                lookup_key=str(occurrence["surface"]),
                arguments={
                    "surface": occurrence["surface"],
                    "block_id": occurrence["anchor"]["block_id"],
                },
                snapshot_hash=parent["snapshot_hash"],
                as_of_position=occurrence["story_position"],
                allowed_entity_ids=[],
                allowed_pending_ids=[],
                round_no=1,
            )
        )
    request = render_resolution_request(
        design_doc=DESIGN_DOC,
        working_set=working,
        owned_occurrence_ids=sorted(working.staged_occurrences),
        candidate_cards=[],
        pending_cards=[],
        tool_traces=traces,
        remaining_tool_rounds=0,
    )
    response = _partition_each_resolver(request)
    first_refs = response["new_partitions"][0]["binding_evidence_refs"]
    second_refs = response["new_partitions"][1]["binding_evidence_refs"]
    response["new_partitions"][0]["binding_evidence_refs"] = second_refs
    response["new_partitions"][1]["binding_evidence_refs"] = first_refs
    with pytest.raises(RegistryContractError, match="own occurrences"):
        validate_resolution_response(response, request=request)


def test_model_anchor_must_equal_the_verbatim_surface() -> None:
    chapter = _chapter("novel_ch01", ["Mira entered."])
    block = chapter_block_views(chapter)[0]
    parent = empty_registry_snapshot("lineage_anchor")
    request = render_extract_request(
        design_doc=DESIGN_DOC,
        chapter_id="novel_ch01",
        chapter_order=0,
        request_key="b1:novel_ch01:w1",
        window_id="w1",
        active_blocks=[block],
        context_only_tail=[],
        snapshot=parent,
        candidate_cap=4,
    )
    mention = _mention(
        surface="Mira",
        kind="person",
        block_id=block["block_id"],
        evidence_quote=block["text"],
    )
    mention["anchor_text"] = "entered"
    with pytest.raises(RegistryContractError, match="must equal surface"):
        validate_extract_response(_extract_response(request, [mention]), request=request)


def test_registry_cas_has_one_winner_and_crash_before_swap_keeps_parent(
    tmp_path: Path,
) -> None:
    store = ChapterRegistryStoreV1(tmp_path / "cas")
    first = _generation("lineage_cas", "novel_ch01")
    second = _generation("lineage_cas", "novel_ch02")
    store.commit(first, expected_parent=None)
    with pytest.raises(RegistryStaleParentError):
        store.commit(second, expected_parent=None)
    assert store.current_generation_id("lineage_cas") == first.generation_id

    crash_store = ChapterRegistryStoreV1(tmp_path / "crash")
    crash_generation = _generation("lineage_crash", "novel_ch01")

    def crash() -> None:
        raise RuntimeError("simulated crash before pointer swap")

    with pytest.raises(RuntimeError, match="simulated crash"):
        crash_store.commit(
            crash_generation,
            expected_parent=None,
            before_pointer_switch=crash,
        )
    assert crash_store.current_generation_id("lineage_crash") is None
    assert (tmp_path / "crash" / "generations" / f"{crash_generation.generation_id}.json").is_file()

    tamper_store = ChapterRegistryStoreV1(tmp_path / "tamper")
    tamper_generation = _generation("lineage_tamper", "novel_ch01")
    tamper_store.commit(tamper_generation, expected_parent=None)
    generation_path = (
        tmp_path
        / "tamper"
        / "generations"
        / f"{tamper_generation.generation_id}.json"
    )
    payload = json.loads(generation_path.read_text(encoding="utf-8"))
    payload["model_note"] = "untrusted extra field"
    generation_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryContractError, match="fields mismatch"):
        tamper_store.load_generation(tamper_generation.generation_id)


def test_generation_is_deterministic_foreign_checkpoint_fails_and_default_v3_stays_closed() -> None:
    first = _generation("lineage_deterministic", "novel_ch01")
    second = _generation("lineage_deterministic", "novel_ch01")
    assert canonical_json(first.to_dict()) == canonical_json(second.to_dict())
    with pytest.raises(RegistryContractError, match="fields mismatch|foreign"):
        load_snapshot_from_handle(
            {
                "builder_schema": "v3",
                "checkpoint_hash": "legacy",
            }
        )
    assert DEFAULT_BUILDER_IDENTITY_MODE == "v3_b0less"
    legacy_request = _build_request(
        stage="b1",
        chapter_id="novel_ch01",
        window_id="w01",
        allowlisted_sections={
            "active_window_blocks": [],
            "context_only_tail": [],
        },
        lineage_manifest=[],
    ).body()
    assert set(legacy_request["allowlisted_sections"]) == {
        "active_window_blocks",
        "context_only_tail",
    }
    assert "candidate_cards" not in legacy_request["allowlisted_sections"]
    assert "builder_identity_mode" not in legacy_request


def test_feature_flag_is_required_before_any_synthetic_registry_execution(
    tmp_path: Path,
) -> None:
    chapter = _chapter("novel_ch01", ["Mira entered."])
    with pytest.raises(RegistryContractError, match="explicit feature flag"):
        run_synthetic_registry_chapter(
            builder_identity_mode=DEFAULT_BUILDER_IDENTITY_MODE,
            design_doc=DESIGN_DOC,
            chapter=chapter,
            chapter_order=0,
            windows=_windows(chapter),
            parent_snapshot=empty_registry_snapshot("lineage_flag"),
            executor=SyntheticRegistryExecutor({}),
            broker=FakeRegistryToolBroker({}, result_cap=2),
            store=ChapterRegistryStoreV1(tmp_path / "registry"),
            candidate_cap=4,
        )
