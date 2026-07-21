from __future__ import annotations

import copy
import json
import math
import shutil
from pathlib import Path
from typing import Any

import pytest

from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.draft_structure import (
    DRAFT_PROJECT_STATE_VERSION,
    DraftStructureError,
    DraftStructureFrozenError,
    build_draft_structure_report,
    validate_global_structure_skeleton,
)
from pipeline.ingest.draft_structure_llm import (
    BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
    BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    GLOBAL_RESPONSE_VERSION,
    HIERARCHY_RESPONSE_VERSION,
    RESPONSE_VERSION,
    StructureContextBudget,
    boundary_repair_contract_identities,
    build_boundary_expansion,
    build_global_structure_context_packs,
    build_hierarchy_context_pack,
    build_structure_context_packs,
    parse_structure_response_json,
    render_global_structure_prompt,
    render_hierarchy_prompt,
    render_structure_prompt,
    run_global_structure_assistant,
    run_hierarchy_assistant,
    run_structure_assistant,
    validate_global_structure_response,
    validate_hierarchy_context_pack,
    validate_hierarchy_response,
    validate_structure_response,
)
from pipeline.ingest.unified_source_normalizer import (
    normalize_source,
    write_unified_normalization,
)


PANDOC_AVAILABLE = shutil.which("pandoc") is not None
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "canonical_source_rich_v1"
FORMAT_SOURCE = {
    "txt": "source.txt",
    "markdown": "source.md",
    "html": "source.html",
    "epub": "source.epub",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _package(
    tmp_path: Path,
    source_format: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if source_format == "epub" and not PANDOC_AVAILABLE:
        pytest.skip("Pandoc is required for the EPUB context-pack probe")
    result = normalize_source(
        FIXTURE_ROOT / FORMAT_SOURCE[source_format],
        doc_id=f"structure_llm_{source_format}",
        pandoc_executable="pandoc" if source_format == "epub" else None,
    )
    package_root = tmp_path / f"package_{source_format}"
    write_unified_normalization(result, package_root)
    document = _load_json(package_root / "document.json")
    structure = _load_json(package_root / "structure_manifest.json")
    assets = _load_json(package_root / "asset_manifest.json")
    projection = _load_json(package_root / "admitted_projection_v1.json")
    report = build_draft_structure_report(
        document,
        structure,
        assets,
        projection,
        {
            "schema_version": DRAFT_PROJECT_STATE_VERSION,
            "doc_id": document["doc_id"],
            "lifecycle": "draft",
            "pipeline_run_count": 0,
        },
        package_root=package_root,
    )
    return package_root, document, report


def _global_authority(package_root: Path) -> dict[str, Any]:
    return {
        "structure_manifest": _load_json(
            package_root / "structure_manifest.json"
        ),
        "asset_manifest": _load_json(package_root / "asset_manifest.json"),
        "admitted_projection": _load_json(
            package_root / "admitted_projection_v1.json"
        ),
        "package_root": package_root,
    }


def _response(
    report: dict[str, Any],
    pack: dict[str, Any],
    *,
    actions: list[dict[str, Any]],
    abstained_unit_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": RESPONSE_VERSION,
        "report_sha256": report["integrity"]["payload_sha256"],
        "context_pack_sha256": pack["integrity"]["payload_sha256"],
        "actions": copy.deepcopy(actions),
        "abstentions": [
            {"unit_id": unit_id, "reason": "no_change"}
            for unit_id in abstained_unit_ids
        ],
    }


def _reseal_context_pack(pack: dict[str, Any]) -> dict[str, Any]:
    resealed = copy.deepcopy(pack)
    body = {key: value for key, value in resealed.items() if key != "integrity"}
    serialized = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    resealed["integrity"] = {
        "payload_sha256": canonical_json_sha256(body),
        "serialized_char_count": len(serialized),
        "estimated_token_count": math.ceil(len(serialized) / 4),
    }
    return resealed


def _reseal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    resealed = copy.deepcopy(payload)
    body = {key: value for key, value in resealed.items() if key != "integrity"}
    resealed["integrity"]["payload_sha256"] = canonical_json_sha256(body)
    return resealed


def _with_flagged_unit(
    report: dict[str, Any],
    *,
    unit_index: int,
) -> dict[str, Any]:
    flagged = copy.deepcopy(report)
    unit = flagged["units"][unit_index]
    unit["issue_codes"] = ["unit_low_confidence"]
    issue_payload = {
        "code": "unit_low_confidence",
        "scope": "unit",
        "target_id": unit["unit_id"],
        "evidence": [f"chapter_id:{unit['chapter_id']}"],
    }
    flagged["issues"] = [
        {
            "issue_id": f"amb_{canonical_json_sha256(issue_payload)[:20]}",
            **issue_payload,
        }
    ]
    flagged["integrity"] = {
        "unit_count": len(flagged["units"]),
        "issue_count": len(flagged["issues"]),
        "payload_sha256": canonical_json_sha256(
            {key: value for key, value in flagged.items() if key != "integrity"}
        ),
    }
    return flagged


@pytest.mark.parametrize("source_format", ["txt", "markdown", "html", "epub"])
def test_context_pack_is_deterministic_bounded_and_book_neutral(
    source_format: str,
    tmp_path: Path,
) -> None:
    _root, document, report = _package(tmp_path, source_format)
    before = copy.deepcopy(document)
    budget = StructureContextBudget(
        max_prompt_chars=48_000,
        max_focus_units_per_pack=2,
    )
    first = build_structure_context_packs(
        report,
        document,
        budget=budget,
        include_all_units=True,
    )
    second = build_structure_context_packs(
        report,
        document,
        budget=budget,
        include_all_units=True,
    )

    assert first == second
    assert document == before
    assert first
    assert [pack["batch_index"] for pack in first] == list(
        range(1, len(first) + 1)
    )
    for pack in first:
        assert pack["batch_count"] == len(first)
        assert len(render_structure_prompt(pack)) <= budget.max_prompt_chars
        assert pack["document_sha256"] == report["inputs"]["document"]["sha256"]
        assert pack["outline"]
        assert pack["response_contract"]["additional_fields_allowed"] is False
        assert set(pack["response_contract"]["action_shapes"]) == {
            "update_unit",
            "split_unit",
            "merge_adjacent_units",
        }
        assert set(pack["focus_unit_ids"]) <= {
            row["unit_id"] for row in pack["outline"]
        }
        assert "Alice" not in render_structure_prompt(pack)


def test_default_pack_only_focuses_reported_ambiguity(tmp_path: Path) -> None:
    _root, document, report = _package(tmp_path, "html")
    report = _with_flagged_unit(report, unit_index=1)
    expected = sorted(unit["unit_id"] for unit in report["units"][:3])
    packs = build_structure_context_packs(report, document)
    assert sorted(
        unit_id for pack in packs for unit_id in pack["focus_unit_ids"]
    ) == expected
    assert expected
    assert len(
        {
            case["boundary_id"]
            for pack in packs
            for case in pack["boundary_cases"]
        }
    ) == sum(len(pack["boundary_cases"]) for pack in packs)


class _FakeExecutor:
    def __init__(self, *, mutate: bool = False) -> None:
        self.calls = 0
        self.mutate = mutate

    def complete(
        self,
        prompt: str,
        *,
        context_pack: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        assert "CONTEXT_PACK_JSON" in prompt
        focus = list(context_pack["focus_unit_ids"])
        if self.mutate:
            context_pack["outline"][0]["title"] = "mutated"
        first = focus[0]
        action = {
            "action_type": "update_unit",
            "unit_id": first,
            "new_title": "Reviewed structure",
            "classification": "review",
        }
        return {
            "schema_version": RESPONSE_VERSION,
            "report_sha256": context_pack["report_sha256"],
            "context_pack_sha256": context_pack["integrity"]["payload_sha256"],
            "actions": [action],
            "abstentions": [
                {"unit_id": unit_id, "reason": "no_change"}
                for unit_id in focus[1:]
            ],
        }


def test_fake_executor_produces_advisory_plan_without_auto_promotion(
    tmp_path: Path,
) -> None:
    _root, document, report = _package(tmp_path, "html")
    executor = _FakeExecutor()
    result = run_structure_assistant(
        executor,
        report,
        document,
        model_identifier="fake-structure-model-v1",
        include_all_units=True,
    )
    assert executor.calls == len(result["context_packs"])
    assert result["correction_plan"]["proposer"] == {
        "kind": "llm",
        "identifier": "fake-structure-model-v1",
    }
    assert result["correction_plan"]["actions"]
    assert {
        action["status"] for action in result["correction_plan"]["actions"]
    } == {"review_required"}
    assert {
        action["reason"] for action in result["correction_plan"]["actions"]
    } == {"non_human_requires_review"}


def test_executor_cannot_mutate_context_pack(tmp_path: Path) -> None:
    _root, document, report = _package(tmp_path, "html")
    with pytest.raises(DraftStructureError, match="mutated its context pack"):
        run_structure_assistant(
            _FakeExecutor(mutate=True),
            report,
            document,
            model_identifier="fake-structure-model-v1",
            include_all_units=True,
        )


def test_response_identity_scope_and_coverage_are_fail_closed(tmp_path: Path) -> None:
    _root, document, report = _package(tmp_path, "html")
    pack = build_structure_context_packs(
        report,
        document,
        include_all_units=True,
    )[0]
    focus = list(pack["focus_unit_ids"])
    valid = _response(
        report,
        pack,
        actions=[],
        abstained_unit_ids=focus,
    )
    assert validate_structure_response(valid, report, pack) == []

    tampered = copy.deepcopy(valid)
    tampered["context_pack_sha256"] = "0" * 64
    with pytest.raises(DraftStructureError, match="context pack identity differs"):
        validate_structure_response(tampered, report, pack)

    missing = copy.deepcopy(valid)
    missing["abstentions"].pop()
    with pytest.raises(DraftStructureError, match="cover every focus unit"):
        validate_structure_response(missing, report, pack)

    out_of_scope = _response(
        report,
        pack,
        actions=[
            {
                "action_type": "split_unit",
                "unit_id": focus[0],
                "at_block_id": "foreign_block",
                "left_title": "Left",
                "right_title": "Right",
                "left_classification": "translate",
                "right_classification": "translate",
            }
        ],
        abstained_unit_ids=focus[1:],
    )
    with pytest.raises(DraftStructureError, match="outside exposed scope"):
        validate_structure_response(out_of_scope, report, pack)


def test_raw_response_parser_rejects_prose_and_duplicate_keys() -> None:
    valid = '{"schema_version":"draft_structure_llm_response_v1"}'
    assert parse_structure_response_json(valid)["schema_version"] == RESPONSE_VERSION
    with pytest.raises(DraftStructureError, match="not one strict JSON value"):
        parse_structure_response_json(f"Here is the result: {valid}")
    with pytest.raises(DraftStructureError, match="repeats JSON key"):
        parse_structure_response_json('{"actions":[],"actions":[]}')


def test_boundary_expansion_is_bounded_deterministic_and_identity_bound(
    tmp_path: Path,
) -> None:
    _root, document, report = _package(tmp_path, "html")
    budget = StructureContextBudget(max_expansion_blocks_per_side=4)
    pack = build_structure_context_packs(
        report,
        document,
        budget=budget,
        include_all_units=True,
    )[0]
    boundary_id = pack["boundary_cases"][0]["boundary_id"]
    first = build_boundary_expansion(
        document,
        pack,
        boundary_id,
        left_blocks=4,
        right_blocks=4,
        budget=budget,
    )
    second = build_boundary_expansion(
        document,
        pack,
        boundary_id,
        left_blocks=4,
        right_blocks=4,
        budget=budget,
    )
    assert first == second
    assert len(first["left_blocks"]) <= 4
    assert len(first["right_blocks"]) <= 4
    assert first["context_pack_sha256"] == pack["integrity"]["payload_sha256"]

    changed = copy.deepcopy(document)
    changed["chapters"][0]["blocks"][0]["clean_text"] += " changed"
    with pytest.raises(DraftStructureError, match="document differs"):
        build_boundary_expansion(
            changed,
            pack,
            boundary_id,
            left_blocks=1,
            right_blocks=1,
            budget=budget,
        )
    with pytest.raises(DraftStructureError, match="exceeds expansion limit"):
        build_boundary_expansion(
            document,
            pack,
            boundary_id,
            left_blocks=5,
            right_blocks=1,
            budget=budget,
        )


def test_prompt_budget_halts_instead_of_silently_dropping_context(
    tmp_path: Path,
) -> None:
    _root, document, report = _package(tmp_path, "html")
    with pytest.raises(DraftStructureError, match="exceeds max_prompt_chars"):
        build_structure_context_packs(
            report,
            document,
            budget=StructureContextBudget(max_prompt_chars=4_000),
            include_all_units=True,
        )


def _global_response(
    report: dict[str, Any],
    pack: dict[str, Any],
    *,
    actions: list[dict[str, Any]] | None = None,
    abstained_candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": GLOBAL_RESPONSE_VERSION,
        "report_sha256": report["integrity"]["payload_sha256"],
        "skeleton_sha256": pack["skeleton_sha256"],
        "context_pack_sha256": pack["integrity"]["payload_sha256"],
        "actions": copy.deepcopy(actions or []),
        "abstentions": [
            {"candidate_id": candidate_id, "reason": "no_change"}
            for candidate_id in (abstained_candidate_ids or [])
        ],
    }


def test_global_context_packs_exact_cover_inventory_without_truncation(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "txt")
    authority = _global_authority(root)
    budget = StructureContextBudget(
        max_prompt_chars=48_000,
        max_global_candidates_per_pack=1,
    )
    first = build_global_structure_context_packs(
        report, document, budget=budget, **authority
    )
    second = build_global_structure_context_packs(
        report, document, budget=budget, **authority
    )
    expected = [
        row["candidate_id"] for row in report["global_skeleton"]["candidates"]
    ]
    actual = [
        candidate_id
        for pack in first
        for candidate_id in pack["assigned_candidate_ids"]
    ]
    assert first == second
    assert actual == expected
    assert len(actual) == len(set(actual))
    assert len(first) == len(expected)
    assert all(
        len(
            render_global_structure_prompt(
                pack,
                report=report,
                document=document,
                **authority,
            )
        )
        <= budget.max_prompt_chars
        for pack in first
    )

    tampered = copy.deepcopy(first[0])
    tampered["assigned_candidate_ids"][0] = "foreign_candidate"
    with pytest.raises(DraftStructureError, match="payload hash differs"):
        render_global_structure_prompt(
            tampered,
            report=report,
            document=document,
            **authority,
        )


def test_resealed_foreign_navigation_fails_authoritative_lineage(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "epub")
    authority = _global_authority(root)
    tampered_report = copy.deepcopy(report)
    navigation = tampered_report["global_skeleton"]["navigation"]
    row = next(
        item
        for item in navigation
        if item["resolution_status"] in {"source_exact", "title_exact_unique"}
    )
    foreign_block_id = "foreign_block_not_in_document"
    row["source_mapped_block_id"] = foreign_block_id
    row["candidate_block_ids"] = [foreign_block_id]
    row["resolved_block_id"] = foreign_block_id
    tampered_report["global_skeleton"] = _reseal_payload(
        tampered_report["global_skeleton"]
    )
    tampered_report = _reseal_payload(tampered_report)

    with pytest.raises(DraftStructureError, match="semantic lineage differs"):
        validate_global_structure_skeleton(
            tampered_report["global_skeleton"],
            authoritative_document=document,
            authoritative_structure_manifest=authority["structure_manifest"],
            authoritative_asset_manifest=authority["asset_manifest"],
            authoritative_admitted_projection=(
                authority["admitted_projection"]
            ),
            package_root=root,
        )
    with pytest.raises(DraftStructureError, match="semantic lineage differs"):
        build_global_structure_context_packs(
            tampered_report,
            document,
            budget=StructureContextBudget(max_global_candidates_per_pack=1),
            **authority,
        )


def test_global_response_is_candidate_scoped_and_fail_closed(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "html")
    authority = _global_authority(root)
    packs = build_global_structure_context_packs(
        report,
        document,
        budget=StructureContextBudget(max_global_candidates_per_pack=1),
        **authority,
    )
    pack = packs[0]
    candidate_id = pack["assigned_candidate_ids"][0]
    scope = pack["allowed_scope"][0]
    if scope["merge_boundaries"]:
        boundary = scope["merge_boundaries"][0]
        proposal = {
            "action_type": "merge_adjacent_units",
            **boundary,
            "new_title": "Combined unit",
            "classification": "translate",
        }
    elif scope["split_boundaries"]:
        boundary = scope["split_boundaries"][0]
        proposal = {
            "action_type": "split_unit",
            **boundary,
            "left_title": "Left unit",
            "right_title": "Right unit",
            "left_classification": "translate",
            "right_classification": "translate",
        }
    else:
        pytest.skip("fixture did not expose an actionable global candidate")
    valid = _global_response(
        report,
        pack,
        actions=[{"candidate_id": candidate_id, "proposal": proposal}],
    )
    assert validate_global_structure_response(
        valid,
        report,
        pack,
        document=document,
        **authority,
    ) == [proposal]

    missing = copy.deepcopy(valid)
    missing["actions"] = []
    with pytest.raises(DraftStructureError, match="exactly once"):
        validate_global_structure_response(
            missing, report, pack, document=document, **authority
        )

    duplicate = copy.deepcopy(valid)
    duplicate["abstentions"] = [
        {"candidate_id": candidate_id, "reason": "no_change"}
    ]
    with pytest.raises(DraftStructureError, match="exactly once"):
        validate_global_structure_response(
            duplicate, report, pack, document=document, **authority
        )

    foreign = copy.deepcopy(valid)
    foreign["actions"][0]["candidate_id"] = "foreign_candidate"
    with pytest.raises(DraftStructureError, match="not assigned"):
        validate_global_structure_response(
            foreign, report, pack, document=document, **authority
        )

    outside = copy.deepcopy(valid)
    outside["actions"][0]["proposal"] = {
        "action_type": "split_unit",
        "unit_id": "foreign_unit",
        "at_block_id": "foreign_block",
        "left_title": "Left",
        "right_title": "Right",
        "left_classification": "translate",
        "right_classification": "translate",
    }
    with pytest.raises(DraftStructureError, match="outside candidate scope"):
        validate_global_structure_response(
            outside, report, pack, document=document, **authority
        )


def test_resealed_global_scope_tamper_is_rederived_from_skeleton(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "html")
    authority = _global_authority(root)
    pack = build_global_structure_context_packs(
        report,
        document,
        budget=StructureContextBudget(max_global_candidates_per_pack=1),
        **authority,
    )[0]
    tampered = copy.deepcopy(pack)
    tampered["allowed_scope"][0]["update_unit_ids"] = ["foreign_unit"]
    tampered = _reseal_context_pack(tampered)
    response = _global_response(
        report,
        tampered,
        abstained_candidate_ids=[tampered["assigned_candidate_ids"][0]],
    )
    with pytest.raises(DraftStructureError, match="allowed scope differs"):
        validate_global_structure_response(
            response,
            report,
            tampered,
            document=document,
            **authority,
        )


class _GlobalAbstainingExecutor:
    def __init__(self, *, mutate: bool = False) -> None:
        self.calls = 0
        self.mutate = mutate

    def complete(
        self,
        prompt: str,
        *,
        context_pack: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        assert "GLOBAL_CONTEXT_PACK_JSON" in prompt
        response = {
            "schema_version": GLOBAL_RESPONSE_VERSION,
            "report_sha256": context_pack["report_sha256"],
            "skeleton_sha256": context_pack["skeleton_sha256"],
            "context_pack_sha256": context_pack["integrity"]["payload_sha256"],
            "actions": [],
            "abstentions": [
                {"candidate_id": candidate_id, "reason": "no_change"}
                for candidate_id in context_pack["assigned_candidate_ids"]
            ],
        }
        if self.mutate:
            context_pack["candidates"][0]["signals"].append("mutated")
        return response


def test_global_fake_executor_is_offline_deterministic_and_non_mutating(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "markdown")
    authority = _global_authority(root)
    executor = _GlobalAbstainingExecutor()
    result = run_global_structure_assistant(
        executor,
        report,
        document,
        model_identifier="fake-offline-v1",
        budget=StructureContextBudget(max_global_candidates_per_pack=1),
        **authority,
    )
    assert executor.calls == len(result["context_packs"])
    assert result["correction_plan"]["actions"] == []
    assert result == run_global_structure_assistant(
        _GlobalAbstainingExecutor(),
        report,
        document,
        model_identifier="fake-offline-v1",
        budget=StructureContextBudget(max_global_candidates_per_pack=1),
        **authority,
    )
    with pytest.raises(DraftStructureError, match="mutated its context pack"):
        run_global_structure_assistant(
            _GlobalAbstainingExecutor(mutate=True),
            report,
            document,
            model_identifier="fake-offline-v1",
            budget=StructureContextBudget(max_global_candidates_per_pack=1),
            **authority,
        )


def _hierarchy_state(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": DRAFT_PROJECT_STATE_VERSION,
        "doc_id": document["doc_id"],
        "lifecycle": "draft",
        "pipeline_run_count": 0,
    }


def _hierarchy_response(
    context_pack: dict[str, Any],
    *,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    acted = {str(row["child_unit_id"]) for row in actions}
    return {
        "schema_version": HIERARCHY_RESPONSE_VERSION,
        "report_sha256": context_pack["report_sha256"],
        "skeleton_sha256": context_pack["skeleton_sha256"],
        "context_pack_sha256": context_pack["integrity"]["payload_sha256"],
        "actions": copy.deepcopy(actions),
        "abstentions": [
            {"child_unit_id": unit_id, "reason": "no_change"}
            for unit_id in context_pack["allowed_unit_ids"]
            if unit_id not in acted
        ],
    }


class _HierarchyExecutor:
    def __init__(
        self,
        actions: list[dict[str, Any]],
        *,
        mutate: bool = False,
    ) -> None:
        self.actions = copy.deepcopy(actions)
        self.mutate = mutate
        self.calls = 0

    def complete(
        self,
        prompt: str,
        *,
        context_pack: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        assert "proposal-only" in prompt
        assert "HIERARCHY_CONTEXT_PACK_JSON" in prompt
        response = _hierarchy_response(
            context_pack,
            actions=self.actions,
        )
        if self.mutate:
            context_pack["outline"][0]["title"] = "mutated by executor"
        return response


def test_hierarchy_prompt_and_fake_executor_are_bound_and_non_mutating(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "html")
    authority = _global_authority(root)
    project_state = _hierarchy_state(document)
    context_pack = build_hierarchy_context_pack(
        report,
        document,
        project_state=project_state,
        **authority,
    )
    assert context_pack == build_hierarchy_context_pack(
        report,
        document,
        project_state=project_state,
        **authority,
    )
    assert context_pack["allowed_unit_ids"] == [
        row["unit_id"] for row in report["global_skeleton"]["outline"]
    ]
    prompt = render_hierarchy_prompt(
        context_pack,
        report=report,
        document=document,
        project_state=project_state,
        **authority,
    )
    assert len(prompt) <= context_pack["context_budget"]["max_prompt_chars"]

    units = context_pack["allowed_unit_ids"]
    actions = [
        {
            "action_type": "set_parent",
            "child_unit_id": units[1],
            "parent_unit_id": units[0],
        }
    ]
    executor = _HierarchyExecutor(actions)
    first = run_hierarchy_assistant(
        executor,
        report,
        document,
        project_state=project_state,
        model_identifier="fake-hierarchy-v1",
        **authority,
    )
    second = run_hierarchy_assistant(
        _HierarchyExecutor(actions),
        report,
        document,
        project_state=project_state,
        model_identifier="fake-hierarchy-v1",
        **authority,
    )
    assert executor.calls == 1
    assert first == second
    assert first["hierarchy_plan"]["actions"][0]["status"] == (
        "review_required"
    )
    assert first["hierarchy_plan"]["actions"][0]["reason"] == (
        "non_human_requires_review"
    )

    with pytest.raises(DraftStructureError, match="mutated its context pack"):
        run_hierarchy_assistant(
            _HierarchyExecutor(actions, mutate=True),
            report,
            document,
            project_state=project_state,
            model_identifier="fake-hierarchy-v1",
            **authority,
        )


def test_hierarchy_response_rejects_unknown_forward_and_incomplete_cover(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "markdown")
    authority = _global_authority(root)
    project_state = _hierarchy_state(document)
    context_pack = build_hierarchy_context_pack(
        report,
        document,
        project_state=project_state,
        **authority,
    )
    units = context_pack["allowed_unit_ids"]
    valid = _hierarchy_response(
        context_pack,
        actions=[
            {
                "action_type": "set_parent",
                "child_unit_id": units[1],
                "parent_unit_id": units[0],
            }
        ],
    )
    assert validate_hierarchy_response(
        valid,
        context_pack,
        report=report,
        document=document,
        project_state=project_state,
        **authority,
    ) == valid["actions"]

    foreign_child = copy.deepcopy(valid)
    foreign_child["actions"][0]["child_unit_id"] = "future_unit"
    with pytest.raises(DraftStructureError, match="unknown child"):
        validate_hierarchy_response(
            foreign_child,
            context_pack,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )

    foreign_parent = copy.deepcopy(valid)
    foreign_parent["actions"][0]["parent_unit_id"] = "removed_unit"
    with pytest.raises(DraftStructureError, match="unknown parent"):
        validate_hierarchy_response(
            foreign_parent,
            context_pack,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )

    forward_parent = _hierarchy_response(
        context_pack,
        actions=[
            {
                "action_type": "set_parent",
                "child_unit_id": units[0],
                "parent_unit_id": units[1],
            }
        ],
    )
    with pytest.raises(DraftStructureError, match="before child"):
        validate_hierarchy_response(
            forward_parent,
            context_pack,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )

    missing = copy.deepcopy(valid)
    missing["abstentions"].pop()
    with pytest.raises(DraftStructureError, match="exact-cover"):
        validate_hierarchy_response(
            missing,
            context_pack,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )

    duplicate = copy.deepcopy(valid)
    duplicate["abstentions"].append(
        {"child_unit_id": units[1], "reason": "no_change"}
    )
    with pytest.raises(DraftStructureError, match="exact-cover"):
        validate_hierarchy_response(
            duplicate,
            context_pack,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )


def test_hierarchy_context_fails_closed_on_tamper_stale_frozen_and_overflow(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "html")
    authority = _global_authority(root)
    project_state = _hierarchy_state(document)
    context_pack = build_hierarchy_context_pack(
        report,
        document,
        project_state=project_state,
        **authority,
    )

    tampered = copy.deepcopy(context_pack)
    tampered["outline"][0]["title"] = "resealed foreign outline"
    tampered = _reseal_context_pack(tampered)
    with pytest.raises(DraftStructureError, match="authoritative report"):
        validate_hierarchy_context_pack(
            tampered,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )

    response = _hierarchy_response(context_pack, actions=[])
    response["report_sha256"] = "0" * 64
    with pytest.raises(DraftStructureError, match="report identity"):
        validate_hierarchy_response(
            response,
            context_pack,
            report=report,
            document=document,
            project_state=project_state,
            **authority,
        )

    frozen_state = {
        **project_state,
        "lifecycle": "active",
        "pipeline_run_count": 1,
    }
    with pytest.raises(DraftStructureFrozenError):
        build_hierarchy_context_pack(
            report,
            document,
            project_state=frozen_state,
            **authority,
        )

    with pytest.raises(DraftStructureError, match="may not be truncated"):
        build_hierarchy_context_pack(
            report,
            document,
            project_state=project_state,
            budget=StructureContextBudget(max_prompt_chars=4_000),
            **authority,
        )


def test_hierarchy_non_contiguous_graph_stays_review_required(
    tmp_path: Path,
) -> None:
    root, document, report = _package(tmp_path, "txt")
    authority = _global_authority(root)
    project_state = _hierarchy_state(document)
    context_pack = build_hierarchy_context_pack(
        report,
        document,
        project_state=project_state,
        **authority,
    )
    units = context_pack["allowed_unit_ids"]
    response = _hierarchy_response(
        context_pack,
        actions=[
            {
                "action_type": "set_parent",
                "child_unit_id": units[2],
                "parent_unit_id": units[0],
            },
            {
                "action_type": "set_parent",
                "child_unit_id": units[3],
                "parent_unit_id": units[1],
            },
        ],
    )

    class _CrossingExecutor:
        def complete(
            self,
            prompt: str,
            *,
            context_pack: dict[str, Any],
        ) -> dict[str, Any]:
            assert "proposal-only" in prompt
            return copy.deepcopy(response)

    result = run_hierarchy_assistant(
        _CrossingExecutor(),
        report,
        document,
        project_state=project_state,
        model_identifier="fake-crossing-v1",
        **authority,
    )
    assert {row["status"] for row in result["hierarchy_plan"]["actions"]} == {
        "review_required"
    }
    assert {row["reason"] for row in result["hierarchy_plan"]["actions"]} == {
        "non_contiguous_or_crossing_subtree"
    }


def test_boundary_repair_contract_dialects_are_versioned_and_deterministic(
    tmp_path: Path,
) -> None:
    _root, document, raw_report = _package(tmp_path, "html")
    report = _with_flagged_unit(raw_report, unit_index=1)
    focus_id = report["units"][1]["unit_id"]
    legacy = boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
    )
    active = boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V2
    )

    assert legacy == boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
    )
    assert active == boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V2
    )
    assert legacy["prompt"]["revision"] == "v1"
    assert active["prompt"]["revision"] == "v2"
    assert legacy["prompt"]["sha256"] != active["prompt"]["sha256"]
    assert legacy["response_schema"]["sha256"] != (
        active["response_schema"]["sha256"]
    )

    packs = build_structure_context_packs(
        report,
        document,
        focus_unit_ids=[focus_id],
        response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    )
    assert len(packs) == 1
    assert packs[0]["response_contract"]["coverage_policy"] == {
        "target_set": "focus_unit_ids",
        "exactly_once_across": ["actions", "abstentions"],
        "actions_and_abstentions_are_mutually_exclusive": True,
        "omissions_allowed": False,
        "duplicates_allowed": False,
    }
    prompt = render_structure_prompt(
        packs[0],
        response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    )
    assert "cover every focus_unit_id exactly once" in " ".join(prompt.split())
    with pytest.raises(DraftStructureError, match="differs from requested dialect"):
        render_structure_prompt(
            packs[0],
            response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
        )


def test_boundary_repair_v2_rejects_duplicate_or_missing_focus_coverage(
    tmp_path: Path,
) -> None:
    _root, document, raw_report = _package(tmp_path, "html")
    report = _with_flagged_unit(raw_report, unit_index=1)
    focus_id = report["units"][1]["unit_id"]
    pack = build_structure_context_packs(
        report,
        document,
        focus_unit_ids=[focus_id],
        response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    )[0]
    action = {
        "action_type": "update_unit",
        "unit_id": focus_id,
        "new_title": None,
        "classification": "review",
    }
    valid = _response(
        report,
        pack,
        actions=[action],
        abstained_unit_ids=[],
    )
    assert validate_structure_response(valid, report, pack) == [action]

    duplicate = _response(
        report,
        pack,
        actions=[action],
        abstained_unit_ids=[focus_id],
    )
    with pytest.raises(DraftStructureError, match="exactly once"):
        validate_structure_response(duplicate, report, pack)

    missing = _response(
        report,
        pack,
        actions=[],
        abstained_unit_ids=[],
    )
    with pytest.raises(DraftStructureError, match="exactly once"):
        validate_structure_response(missing, report, pack)


def test_context_build_rejects_resealed_stale_report_shape(tmp_path: Path) -> None:
    _root, document, raw_report = _package(tmp_path, "html")
    report = _with_flagged_unit(raw_report, unit_index=1)
    report["units"][1]["foreign_field"] = "resealed-but-invalid"
    report = _reseal_payload(report)

    with pytest.raises(DraftStructureError, match="fields differ"):
        build_structure_context_packs(
            report,
            document,
            focus_unit_ids=[report["units"][1]["unit_id"]],
            response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
        )
