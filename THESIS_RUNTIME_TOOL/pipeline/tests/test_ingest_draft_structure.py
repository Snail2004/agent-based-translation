from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pipeline.ingest.admitted_projection import validate_admitted_projection
from pipeline.ingest.canonical_source_package import (
    canonical_json_sha256,
    validate_canonical_source_package,
)
from pipeline.ingest.document_contract import validate_locked_document
from pipeline.ingest.draft_structure import (
    CORRECTION_PLAN_VERSION,
    DRAFT_PROJECT_STATE_VERSION,
    HIERARCHY_OVERLAY_VERSION,
    HIERARCHY_PLAN_VERSION,
    DraftStructureError,
    DraftStructureFrozenError,
    GlobalStructurePolicy,
    apply_correction_plan,
    apply_hierarchy_plan,
    build_correction_plan,
    build_draft_structure_report,
    build_plan_with_executor,
    build_hierarchy_plan,
    validate_hierarchy_overlay,
    validate_global_structure_skeleton,
    write_experimental_draft_package,
)
from pipeline.ingest.source_package_materializer import materialize_source_package
from pipeline.ingest.unified_source_normalizer import (
    normalize_source,
    validate_normalization_contract,
    write_unified_normalization,
)


PANDOC_AVAILABLE = shutil.which("pandoc") is not None
RICH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "canonical_source_rich_v1"
)
FORMAT_SOURCE = {
    "txt": "source.txt",
    "markdown": "source.md",
    "html": "source.html",
    "epub": "source.epub",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_blocks(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    ]


def _block_evidence(
    document: dict[str, Any],
) -> list[tuple[str, str, str]]:
    return [
        (block["block_id"], block["source_text"], block["clean_text"])
        for block in _flatten_blocks(document)
    ]


def _canonical_package_file_hashes(package_root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((package_root / name).read_bytes()).hexdigest()
        for name in (
            "document.json",
            "structure_manifest.json",
            "asset_manifest.json",
            "admitted_projection_v1.json",
        )
    }


def _state(
    doc_id: str,
    *,
    lifecycle: str = "draft",
    run_count: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": DRAFT_PROJECT_STATE_VERSION,
        "doc_id": doc_id,
        "lifecycle": lifecycle,
        "pipeline_run_count": run_count,
    }


def _package(
    tmp_path: Path,
    source_format: str,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if source_format == "epub" and not PANDOC_AVAILABLE:
        pytest.skip("Pandoc is required for the EPUB contract probe")
    source = RICH_FIXTURE_ROOT / FORMAT_SOURCE[source_format]
    result = normalize_source(
        source,
        doc_id=f"draft_{source_format}",
        pandoc_executable="pandoc" if source_format == "epub" else None,
    )
    package_root = tmp_path / f"package_{source_format}"
    write_unified_normalization(result, package_root)
    return (
        package_root,
        _load_json(package_root / "document.json"),
        _load_json(package_root / "structure_manifest.json"),
        _load_json(package_root / "asset_manifest.json"),
        _load_json(package_root / "admitted_projection_v1.json"),
    )


def _report(
    package: tuple[
        Path,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ],
) -> tuple[dict[str, Any], dict[str, Any]]:
    root, document, structure, assets, projection = package
    project_state = _state(document["doc_id"])
    return (
        build_draft_structure_report(
            document,
            structure,
            assets,
            projection,
            project_state,
            package_root=root,
        ),
        project_state,
    )


def _update_action(
    unit_id: str,
    *,
    title: str | None = None,
    classification: str | None = None,
) -> dict[str, Any]:
    return {
        "action_type": "update_unit",
        "unit_id": unit_id,
        "new_title": title,
        "classification": classification,
    }


def _sealed_plan(
    report: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    kind: str = "human",
) -> dict[str, Any]:
    return build_correction_plan(
        report,
        actions,
        proposer={"kind": kind, "identifier": f"fixture-{kind}"},
    )


@pytest.mark.parametrize("source_format", ["txt", "markdown", "html", "epub"])
def test_all_formats_produce_deterministic_draft_report_and_bounded_plan(
    source_format: str,
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, source_format)
    root, document, structure, assets, projection = package
    report, project_state = _report(package)
    assert report == build_draft_structure_report(
        document,
        structure,
        assets,
        projection,
        project_state,
        package_root=root,
    )
    assert report["integrity"]["payload_sha256"] == canonical_json_sha256(
        {key: value for key, value in report.items() if key != "integrity"}
    )

    first = report["units"][0]["unit_id"]
    last = report["units"][-1]["unit_id"]
    first_action = _update_action(first, title="Reviewed opening")
    last_action = _update_action(last, classification="review")
    forward = _sealed_plan(report, [first_action, last_action])
    reverse = _sealed_plan(report, [last_action, first_action])
    assert forward == reverse
    assert forward["schema_version"] == CORRECTION_PLAN_VERSION

    before_document = copy.deepcopy(document)
    before_structure = copy.deepcopy(structure)
    before_assets = copy.deepcopy(assets)
    before_projection = copy.deepcopy(projection)
    result = apply_correction_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        forward,
        package_root=root,
    )
    assert document == before_document
    assert structure == before_structure
    assert assets == before_assets
    assert projection == before_projection
    assert _block_evidence(result.document) == _block_evidence(document)
    assert result.document["chapters"][0]["title"] == "Reviewed opening"
    validate_locked_document(result.document)
    validate_normalization_contract(
        result.document,
        result.structure_manifest,
        expected_format=source_format,
    )


def test_split_and_merge_only_change_unit_boundaries_and_identity(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, "markdown")
    root, document, structure, assets, projection = package
    report, project_state = _report(package)
    content = next(unit for unit in report["units"] if len(unit["block_ids"]) >= 2)
    split_plan = _sealed_plan(
        report,
        [
            {
                "action_type": "split_unit",
                "unit_id": content["unit_id"],
                "at_block_id": content["block_ids"][1],
                "left_title": "Part A",
                "right_title": "Part B",
                "left_classification": "translate",
                "right_classification": "review",
            }
        ],
    )
    split = apply_correction_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        split_plan,
        package_root=root,
    )
    assert len(split.document["chapters"]) == len(document["chapters"]) + 1
    assert _block_evidence(split.document) == _block_evidence(document)
    split_chapters = [
        chapter
        for chapter in split.document["chapters"]
        if chapter["title"] in {"Part A", "Part B"}
    ]
    assert len(split_chapters) == 2
    assert split_chapters[0]["blocks"][-1]["block_id"] == content["block_ids"][0]
    assert split_chapters[1]["blocks"][0]["block_id"] == content["block_ids"][1]
    assert all(
        chapter["blocks"][0]["is_chapter_opening"] is True
        for chapter in split_chapters
    )

    content_units = [
        unit
        for unit in report["units"]
        if unit["translation_policy"] == "translate"
    ]
    left, right = content_units[:2]
    merge_plan = _sealed_plan(
        report,
        [
            {
                "action_type": "merge_adjacent_units",
                "left_unit_id": left["unit_id"],
                "right_unit_id": right["unit_id"],
                "new_title": "Combined narrative",
                "classification": "translate",
            }
        ],
    )
    merged = apply_correction_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        merge_plan,
        package_root=root,
    )
    assert len(merged.document["chapters"]) == len(document["chapters"]) - 1
    assert _block_evidence(merged.document) == _block_evidence(document)
    combined = next(
        chapter
        for chapter in merged.document["chapters"]
        if chapter["title"] == "Combined narrative"
    )
    assert [block["block_id"] for block in combined["blocks"]] == (
        left["block_ids"] + right["block_ids"]
    )


@pytest.mark.parametrize(
    ("lifecycle", "run_count"),
    [
        ("active", 0),
        ("completed", 0),
        ("draft", 1),
        ("active", 3),
    ],
)
def test_active_or_completed_project_cannot_enter_draft_structure(
    lifecycle: str,
    run_count: int,
    tmp_path: Path,
) -> None:
    root, document, structure, assets, projection = _package(tmp_path, "txt")
    with pytest.raises(
        DraftStructureFrozenError,
        match="only allowed for a draft project with no pipeline run",
    ):
        build_draft_structure_report(
            document,
            structure,
            assets,
            projection,
            _state(
                document["doc_id"],
                lifecycle=lifecycle,
                run_count=run_count,
            ),
            package_root=root,
        )


def test_unknown_and_non_human_actions_cannot_mutate_structure(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, "txt")
    root, document, structure, assets, projection = package
    report, project_state = _report(package)

    unknown_plan = _sealed_plan(
        report,
        [_update_action("u_missing", title="Do not apply")],
    )
    assert unknown_plan["actions"][0]["status"] == "review_required"
    assert unknown_plan["actions"][0]["reason"] == "unknown_unit"
    unknown = apply_correction_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        unknown_plan,
        package_root=root,
    )
    assert unknown.document == document
    assert unknown.structure_manifest == structure

    unit_id = report["units"][1]["unit_id"]
    synthetic_plan = _sealed_plan(
        report,
        [_update_action(unit_id, title="Synthetic guess")],
        kind="synthetic",
    )
    assert synthetic_plan["actions"][0]["reason"] == "non_human_requires_review"
    synthetic = apply_correction_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        synthetic_plan,
        package_root=root,
    )
    assert all(
        chapter["title"] != "Synthetic guess"
        for chapter in synthetic.document["chapters"]
    )
    changed = next(
        unit
        for unit in synthetic.structure_manifest["units"]
        if unit["unit_id"] == unit_id
    )
    assert changed["translation_policy"] == "review"
    assert changed["review_required"] is True


def test_invalid_boundaries_conflicts_and_nonadjacent_merge_are_review_only(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, "txt")
    report, _project_state = _report(package)
    first, second, third, last = (
        report["units"][0],
        report["units"][1],
        report["units"][2],
        report["units"][-1],
    )
    plan = _sealed_plan(
        report,
        [
            {
                "action_type": "split_unit",
                "unit_id": second["unit_id"],
                "at_block_id": second["block_ids"][0],
                "left_title": "Left",
                "right_title": "Right",
                "left_classification": "translate",
                "right_classification": "translate",
            },
            {
                "action_type": "merge_adjacent_units",
                "left_unit_id": first["unit_id"],
                "right_unit_id": third["unit_id"],
                "new_title": "Invalid merge",
                "classification": "review",
            },
            _update_action(last["unit_id"], title="Last edit"),
            _update_action(last["unit_id"], classification="exclude"),
        ],
    )
    reasons = {action["reason"] for action in plan["actions"]}
    assert {
        "invalid_split_boundary",
        "units_not_adjacent",
        "conflicting_actions",
    }.issubset(reasons)
    assert all(action["status"] == "review_required" for action in plan["actions"])


def test_report_plan_and_action_tampering_fail_closed(tmp_path: Path) -> None:
    package = _package(tmp_path, "html")
    root, document, structure, assets, projection = package
    report, project_state = _report(package)
    first = report["units"][0]["unit_id"]
    second = report["units"][1]["unit_id"]
    plan = _sealed_plan(
        report,
        [
            _update_action(first, title="First"),
            _update_action(second, title="Second"),
        ],
    )

    tampered_report = copy.deepcopy(report)
    tampered_report["units"][0]["title"] = "tampered"
    with pytest.raises(DraftStructureError, match="stale or tampered"):
        apply_correction_plan(
            document,
            structure,
            assets,
            projection,
            project_state,
            tampered_report,
            plan,
            package_root=root,
        )

    tampered_status = copy.deepcopy(plan)
    tampered_status["actions"][0]["status"] = "review_required"
    tampered_status["actions"][0]["reason"] = "forced"
    tampered_status["integrity"]["payload_sha256"] = canonical_json_sha256(
        {
            key: value
            for key, value in tampered_status.items()
            if key != "integrity"
        }
    )
    with pytest.raises(DraftStructureError, match="code-sealed plan"):
        apply_correction_plan(
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            tampered_status,
            package_root=root,
        )

    reordered = copy.deepcopy(plan)
    reordered["actions"].reverse()
    reordered["integrity"]["payload_sha256"] = canonical_json_sha256(
        {
            key: value
            for key, value in reordered.items()
            if key != "integrity"
        }
    )
    with pytest.raises(DraftStructureError, match="code-sealed plan"):
        apply_correction_plan(
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            reordered,
            package_root=root,
        )


class _FakeExecutor:
    def __init__(self, action: dict[str, Any], *, mutate: bool = False) -> None:
        self._action = action
        self._mutate = mutate

    def propose(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        if self._mutate:
            report["units"][0]["title"] = "mutated"
        return [copy.deepcopy(self._action)]


def test_fake_executor_is_bounded_and_cannot_mutate_report(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, "txt")
    report, _project_state = _report(package)
    action = _update_action(report["units"][0]["unit_id"], title="Human title")
    plan = build_plan_with_executor(
        _FakeExecutor(action),
        report,
        proposer={"kind": "human", "identifier": "manual-fixture"},
    )
    assert plan["actions"][0]["status"] == "candidate"

    with pytest.raises(DraftStructureError, match="mutated its report"):
        build_plan_with_executor(
            _FakeExecutor(action, mutate=True),
            report,
            proposer={"kind": "human", "identifier": "manual-fixture"},
        )


@pytest.mark.parametrize("source_format", ["txt", "markdown", "html", "epub"])
def test_writer_is_deterministic_non_destructive_and_revalidates_package(
    source_format: str,
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, source_format)
    root, document, structure, assets, projection = package
    report, project_state = _report(package)
    target = report["units"][1]["unit_id"]
    plan = _sealed_plan(
        report,
        [_update_action(target, title="Reviewed Chapter", classification="translate")],
    )
    original_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }
    source_path = Path(structure["source"]["path"])
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

    outputs = []
    for name in ("corrected_a", "corrected_b"):
        outputs.append(
            write_experimental_draft_package(
                document,
                structure,
                assets,
                projection,
                project_state,
                report,
                plan,
                tmp_path / name,
                package_root=root,
            )
        )
    for filename in (
        "document.json",
        "structure_manifest.json",
        "asset_manifest.json",
        "admitted_projection_v1.json",
        "normalization_receipt.json",
        "draft_structure_correction_receipt_v1.json",
    ):
        assert (outputs[0].output_dir / filename).read_bytes() == (
            outputs[1].output_dir / filename
        ).read_bytes()

    corrected_document = _load_json(outputs[0].document_path)
    corrected_structure = _load_json(outputs[0].structure_manifest_path)
    corrected_assets = _load_json(outputs[0].output_dir / "asset_manifest.json")
    corrected_projection = _load_json(outputs[0].admitted_projection_path)
    validate_locked_document(corrected_document)
    validate_canonical_source_package(
        corrected_document,
        corrected_structure,
        corrected_assets,
        package_root=outputs[0].output_dir,
    )
    validate_admitted_projection(
        corrected_projection,
        corrected_document,
        corrected_structure,
        corrected_assets,
    )
    assert corrected_assets["assets"] == assets["assets"]
    assert _block_evidence(corrected_document) == _block_evidence(document)
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    assert original_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }


def test_base_exact_cover_text_and_asset_tampering_fail_before_plan(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, "html")
    root, document, structure, assets, projection = package
    project_state = _state(document["doc_id"])

    projection_variants = []
    dropped = copy.deepcopy(projection)
    dropped["rows"].pop()
    projection_variants.append(dropped)
    duplicated = copy.deepcopy(projection)
    duplicated["rows"].append(copy.deepcopy(duplicated["rows"][0]))
    projection_variants.append(duplicated)
    reordered = copy.deepcopy(projection)
    reordered["rows"].reverse()
    projection_variants.append(reordered)
    for tampered_projection in projection_variants:
        with pytest.raises(DraftStructureError):
            build_draft_structure_report(
                document,
                structure,
                assets,
                tampered_projection,
                project_state,
                package_root=root,
            )

    tampered_document = copy.deepcopy(document)
    tampered_document["chapters"][1]["blocks"][0]["source_text"] += " changed"
    with pytest.raises(DraftStructureError):
        build_draft_structure_report(
            tampered_document,
            structure,
            assets,
            projection,
            project_state,
            package_root=root,
        )

    reordered_document = copy.deepcopy(document)
    reordered_document["chapters"][1]["blocks"].reverse()
    with pytest.raises(DraftStructureError):
        build_draft_structure_report(
            reordered_document,
            structure,
            assets,
            projection,
            project_state,
            package_root=root,
        )

    tampered_assets = copy.deepcopy(assets)
    tampered_assets["assets"][0]["metadata"]["tampered"] = True
    with pytest.raises(DraftStructureError):
        build_draft_structure_report(
            document,
            structure,
            tampered_assets,
            projection,
            project_state,
            package_root=root,
        )


def test_writer_rejects_existing_destination_and_source_drift(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, "txt")
    root, document, structure, assets, projection = package
    report, project_state = _report(package)
    plan = _sealed_plan(
        report,
        [_update_action(report["units"][0]["unit_id"], title="Renamed")],
    )
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(DraftStructureError, match="must not already exist"):
        write_experimental_draft_package(
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            plan,
            existing,
            package_root=root,
        )

    drifted_structure = copy.deepcopy(structure)
    drifted_structure["source"]["sha256"] = "0" * 64
    with pytest.raises(DraftStructureError):
        write_experimental_draft_package(
            document,
            drifted_structure,
            assets,
            projection,
            project_state,
            report,
            plan,
            tmp_path / "drifted",
            package_root=root,
        )


def test_global_skeleton_detects_internal_duplicate_restart_and_starvation(
    tmp_path: Path,
) -> None:
    markdown_source = tmp_path / "ambiguous.md"
    markdown_source.write_text(
        """# PART ONE

## CHAPTER I

Alpha text.

## CHAPTER II

Beta text.

# PART TWO

## CHAPTER I

Gamma text.
""",
        encoding="utf-8",
    )
    markdown_result = normalize_source(markdown_source, doc_id="ambiguous_markdown")
    markdown_root = tmp_path / "ambiguous_markdown_package"
    write_unified_normalization(markdown_result, markdown_root)
    load_markdown = lambda name: _load_json(markdown_root / name)
    document, structure, assets, projection = map(
        load_markdown,
        [
            "document.json",
            "structure_manifest.json",
            "asset_manifest.json",
            "admitted_projection_v1.json",
        ],
    )
    report = build_draft_structure_report(
        document,
        structure,
        assets,
        projection,
        _state(document["doc_id"]),
        package_root=markdown_root,
    )
    codes = {row["code"] for row in report["global_skeleton"]["issues"]}
    assert {
        "global_structure_duplicate_title_group",
        "global_structure_internal_heading",
        "global_structure_numbering_restart",
    } <= codes

    txt_source = tmp_path / "signal_starvation.txt"
    txt_source.write_text(
        "\n\n".join(
            f"Ordinary running prose paragraph {index} contains no structural marker."
            for index in range(12)
        ),
        encoding="utf-8",
    )
    txt_result = normalize_source(txt_source, doc_id="signal_starvation")
    txt_root = tmp_path / "signal_starvation_package"
    write_unified_normalization(txt_result, txt_root)
    load_txt = lambda name: _load_json(txt_root / name)
    txt_document, txt_structure, txt_assets, txt_projection = map(
        load_txt,
        [
            "document.json",
            "structure_manifest.json",
            "asset_manifest.json",
            "admitted_projection_v1.json",
        ],
    )
    txt_report = build_draft_structure_report(
        txt_document,
        txt_structure,
        txt_assets,
        txt_projection,
        _state(txt_document["doc_id"]),
        package_root=txt_root,
    )
    assert [
        row["code"] for row in txt_report["global_skeleton"]["issues"]
    ] == ["global_structure_signal_starvation"]


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_navigation_mapping_is_unique_or_explicitly_unresolved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate-navigation.md"
    source.write_text(
        """# BOOK

## CHAPTER I

Alpha.

## CHAPTER II

Beta.

## CHAPTER I

Gamma.

## CHAPTER III

Delta.
""",
        encoding="utf-8",
    )
    epub = tmp_path / "duplicate-navigation.epub"
    subprocess.run(
        ["pandoc", str(source), "-o", str(epub)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = normalize_source(
        epub,
        doc_id="duplicate_navigation",
        pandoc_executable="pandoc",
    )
    navigation = result.structure_manifest["package_structure"]["navigation"]
    assert len(navigation) >= 4
    for row in navigation:
        row["mapped_block"] = None
        row["target"]["file"] = ""
    missing = copy.deepcopy(navigation[0])
    missing["entry_id"] = "nav_missing_fixture"
    missing["title"] = "MISSING CHAPTER FIXTURE"
    missing["mapped_block"] = None
    missing["target"]["file"] = ""
    missing["target"]["anchor"] = "missing-fixture"
    navigation.append(missing)
    wrong_scope = copy.deepcopy(
        next(
            row
            for row in navigation
            if str(row["title"]).strip().casefold() == "chapter ii"
        )
    )
    wrong_scope["entry_id"] = "nav_wrong_scope_fixture"
    wrong_scope["mapped_block"] = None
    wrong_scope["target"]["file"] = "missing-scope.xhtml"
    wrong_scope["target"]["anchor"] = "wrong-scope-fixture"
    navigation.append(wrong_scope)
    package_root = tmp_path / "duplicate_navigation_package"
    write_unified_normalization(result, package_root)
    load = lambda name: _load_json(package_root / name)
    document, structure, assets, projection = map(
        load,
        [
            "document.json",
            "structure_manifest.json",
            "asset_manifest.json",
            "admitted_projection_v1.json",
        ],
    )
    report = build_draft_structure_report(
        document,
        structure,
        assets,
        projection,
        _state(document["doc_id"]),
        package_root=package_root,
    )
    nav_rows = report["global_skeleton"]["navigation"]
    duplicate_rows = [
        row for row in nav_rows if row["normalized_title"] == "chapter i"
    ]
    assert len(duplicate_rows) == 2
    assert all(
        row["resolution_status"] == "unresolved_multiple_match"
        and len(row["candidate_block_ids"]) == 2
        and row["resolved_block_id"] is None
        for row in duplicate_rows
    )
    missing_row = next(
        row for row in nav_rows if row["entry_id"] == "nav_missing_fixture"
    )
    assert missing_row["resolution_status"] == "unresolved_zero_match"
    assert missing_row["candidate_block_ids"] == []
    assert missing_row["resolved_block_id"] is None
    wrong_scope_row = next(
        row
        for row in nav_rows
        if row["entry_id"] == "nav_wrong_scope_fixture"
    )
    assert wrong_scope_row["normalized_title"]
    assert wrong_scope_row["resolution_status"] == "unresolved_zero_match"
    assert wrong_scope_row["candidate_block_ids"] == []
    assert wrong_scope_row["resolved_block_id"] is None
    assert "global_structure_high_navigation_mismatch" in {
        row["code"] for row in report["global_skeleton"]["issues"]
    }


def test_global_skeleton_overflow_is_reported_without_inventory_truncation(
    tmp_path: Path,
) -> None:
    root, document, structure, assets, projection = _package(tmp_path, "txt")
    report = build_draft_structure_report(
        document,
        structure,
        assets,
        projection,
        _state(document["doc_id"]),
        package_root=root,
        global_policy=GlobalStructurePolicy(candidate_overflow_threshold=1),
    )
    skeleton = report["global_skeleton"]
    assert len(skeleton["candidates"]) > 1
    assert skeleton["statistics"]["candidate_count"] == len(
        skeleton["candidates"]
    )
    assert "global_structure_candidate_overflow" in {
        row["code"] for row in skeleton["issues"]
    }
    validate_global_structure_skeleton(skeleton)
    tampered = copy.deepcopy(skeleton)
    tampered["candidates"][0]["signals"].append("tampered")
    with pytest.raises(DraftStructureError, match="candidate ID differs"):
        validate_global_structure_skeleton(tampered)


def _hierarchy_spec(
    child_unit_id: str,
    parent_unit_id: str | None,
) -> dict[str, Any]:
    if parent_unit_id is None:
        return {
            "action_type": "clear_parent",
            "child_unit_id": child_unit_id,
        }
    return {
        "action_type": "set_parent",
        "child_unit_id": child_unit_id,
        "parent_unit_id": parent_unit_id,
    }


def _parented_package(
    tmp_path: Path,
) -> tuple[
    Path,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    _root, document, structure, _assets, _projection = _package(
        tmp_path / "parent_source", "txt"
    )
    parented = copy.deepcopy(structure)
    parented["units"][1]["parent_unit_id"] = parented["units"][0]["unit_id"]
    parented.pop("structure_sha256", None)
    parented["structure_sha256"] = canonical_json_sha256(parented)
    package_root = tmp_path / "parented_package"
    write_result = materialize_source_package(document, parented, package_root)
    assets = _load_json(write_result.asset_manifest_path)
    projection = _load_json(write_result.admitted_projection_path)
    project_state = _state(document["doc_id"])
    report = build_draft_structure_report(
        document,
        parented,
        assets,
        projection,
        project_state,
        package_root=package_root,
    )
    return (
        package_root,
        document,
        parented,
        assets,
        projection,
        project_state,
        report,
    )


def test_hierarchy_overlay_valid_nesting_is_deterministic_and_non_mutating(
    tmp_path: Path,
) -> None:
    root, document, structure, assets, projection = _package(tmp_path, "txt")
    report, project_state = _report((root, document, structure, assets, projection))
    unit_ids = [row["unit_id"] for row in report["units"]]
    specs = [
        _hierarchy_spec(unit_ids[1], unit_ids[0]),
        _hierarchy_spec(unit_ids[2], unit_ids[0]),
    ]
    plan = build_hierarchy_plan(
        report,
        specs,
        proposer={"kind": "human", "identifier": "reviewer-a2"},
    )
    assert plan["schema_version"] == HIERARCHY_PLAN_VERSION
    assert plan == build_hierarchy_plan(
        report,
        list(reversed(specs)),
        proposer={"kind": "human", "identifier": "reviewer-a2"},
    )
    assert {row["status"] for row in plan["actions"]} == {"candidate"}

    canonical_before = copy.deepcopy(
        [document, structure, assets, projection, project_state]
    )
    package_files_before = _canonical_package_file_hashes(root)
    source_path = Path(structure["source"]["path"])
    source_before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    overlay = apply_hierarchy_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        plan,
        package_root=root,
    )
    assert overlay["schema_version"] == HIERARCHY_OVERLAY_VERSION
    assert overlay == apply_hierarchy_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        plan,
        package_root=root,
    )
    assert [row["child_unit_id"] for row in overlay["rows"]] == unit_ids
    assert [row["parent_unit_id"] for row in overlay["rows"]] == [
        None,
        unit_ids[0],
        unit_ids[0],
        None,
    ]
    assert [document, structure, assets, projection, project_state] == (
        canonical_before
    )
    assert _canonical_package_file_hashes(root) == package_files_before
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_before
    validate_hierarchy_overlay(
        overlay,
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        plan,
        package_root=root,
    )

    tampered = copy.deepcopy(overlay)
    tampered["rows"][2]["parent_unit_id"] = None
    tampered["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in tampered.items() if key != "integrity"}
    )
    with pytest.raises(
        DraftStructureError,
        match="differs from authoritative approved plan",
    ):
        validate_hierarchy_overlay(
            tampered,
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            plan,
            package_root=root,
        )

    hash_tampered = copy.deepcopy(overlay)
    hash_tampered["integrity"]["payload_sha256"] = "0" * 64
    with pytest.raises(DraftStructureError, match="payload hash differs"):
        validate_hierarchy_overlay(
            hash_tampered,
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            plan,
            package_root=root,
        )

    identity_tampered = copy.deepcopy(overlay)
    identity_tampered["inputs"]["report"]["sha256"] = "f" * 64
    identity_tampered["integrity"]["payload_sha256"] = canonical_json_sha256(
        {
            key: value
            for key, value in identity_tampered.items()
            if key != "integrity"
        }
    )
    with pytest.raises(DraftStructureError, match="input identities differ"):
        validate_hierarchy_overlay(
            identity_tampered,
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            plan,
            package_root=root,
        )


def test_hierarchy_overlay_can_clear_an_existing_canonical_parent(
    tmp_path: Path,
) -> None:
    (
        root,
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
    ) = _parented_package(tmp_path)
    child_id = report["units"][1]["unit_id"]
    plan = build_hierarchy_plan(
        report,
        [_hierarchy_spec(child_id, None)],
        proposer={"kind": "human", "identifier": "clear-root-reviewer"},
    )
    assert plan["actions"][0]["before_parent_unit_id"] == report["units"][0][
        "unit_id"
    ]
    overlay = apply_hierarchy_plan(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        plan,
        package_root=root,
    )
    assert overlay["rows"][1]["parent_unit_id"] is None
    assert structure["units"][1]["parent_unit_id"] is not None


def test_hierarchy_invalid_graphs_and_foreign_ids_remain_review_required(
    tmp_path: Path,
) -> None:
    root, document, structure, assets, projection = _package(tmp_path, "txt")
    report, project_state = _report((root, document, structure, assets, projection))
    unit_ids = [row["unit_id"] for row in report["units"]]
    cases = [
        ([_hierarchy_spec("future_unit", None)], "unknown_child_unit"),
        ([_hierarchy_spec(unit_ids[1], "removed_unit")], "unknown_parent_unit"),
        ([_hierarchy_spec(unit_ids[1], unit_ids[1])], "self_parent"),
        ([_hierarchy_spec(unit_ids[0], unit_ids[1])], "child_before_parent"),
        (
            [
                _hierarchy_spec(unit_ids[0], unit_ids[1]),
                _hierarchy_spec(unit_ids[1], unit_ids[0]),
            ],
            "cycle",
        ),
        (
            [
                _hierarchy_spec(unit_ids[2], unit_ids[0]),
                _hierarchy_spec(unit_ids[3], unit_ids[1]),
            ],
            "non_contiguous_or_crossing_subtree",
        ),
        (
            [
                _hierarchy_spec(unit_ids[1], unit_ids[0]),
                _hierarchy_spec(unit_ids[1], None),
            ],
            "conflicting_actions",
        ),
    ]
    for specs, expected_reason in cases:
        plan = build_hierarchy_plan(
            report,
            specs,
            proposer={"kind": "human", "identifier": "adversarial-reviewer"},
        )
        assert expected_reason in {row["reason"] for row in plan["actions"]}
        assert {row["status"] for row in plan["actions"]} == {
            "review_required"
        }
        with pytest.raises(DraftStructureError, match="still require review"):
            apply_hierarchy_plan(
                document,
                structure,
                assets,
                projection,
                project_state,
                report,
                plan,
                package_root=root,
            )


def test_hierarchy_rejects_nonhuman_mixed_stale_and_foreign_lineage(
    tmp_path: Path,
) -> None:
    root, document, structure, assets, projection = _package(tmp_path, "txt")
    report, project_state = _report((root, document, structure, assets, projection))
    unit_ids = [row["unit_id"] for row in report["units"]]
    spec = _hierarchy_spec(unit_ids[1], unit_ids[0])
    llm_plan = build_hierarchy_plan(
        report,
        [spec],
        proposer={"kind": "llm", "identifier": "fake-model"},
    )
    assert llm_plan["actions"][0]["reason"] == "non_human_requires_review"
    with pytest.raises(DraftStructureError, match="explicit human approval"):
        apply_hierarchy_plan(
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            build_hierarchy_plan(
                report,
                [],
                proposer={"kind": "synthetic", "identifier": "fixture"},
            ),
            package_root=root,
        )

    mixed_a1_plan = _sealed_plan(
        report,
        [_update_action(unit_ids[0], title="A1 title")],
    )
    with pytest.raises(DraftStructureError, match="hierarchy plan fields differ"):
        apply_hierarchy_plan(
            document,
            structure,
            assets,
            projection,
            project_state,
            report,
            mixed_a1_plan,
            package_root=root,
        )

    stale_report = copy.deepcopy(report)
    stale_report["units"][0]["title"] = "resealed but foreign title"
    stale_report["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in stale_report.items() if key != "integrity"}
    )
    stale_plan = build_hierarchy_plan(
        stale_report,
        [spec],
        proposer={"kind": "human", "identifier": "stale-reviewer"},
    )
    with pytest.raises(DraftStructureError, match="stale or tampered"):
        apply_hierarchy_plan(
            document,
            structure,
            assets,
            projection,
            project_state,
            stale_report,
            stale_plan,
            package_root=root,
        )

    (
        foreign_root,
        foreign_document,
        foreign_structure,
        foreign_assets,
        foreign_projection,
        foreign_state,
        _foreign_report,
    ) = _parented_package(tmp_path / "foreign")
    valid_plan = build_hierarchy_plan(
        report,
        [spec],
        proposer={"kind": "human", "identifier": "valid-reviewer"},
    )
    with pytest.raises(DraftStructureError, match="stale or tampered"):
        apply_hierarchy_plan(
            foreign_document,
            foreign_structure,
            foreign_assets,
            foreign_projection,
            foreign_state,
            report,
            valid_plan,
            package_root=foreign_root,
        )

    with pytest.raises(DraftStructureFrozenError):
        apply_hierarchy_plan(
            document,
            structure,
            assets,
            projection,
            _state(document["doc_id"], lifecycle="active", run_count=1),
            report,
            valid_plan,
            package_root=root,
        )


def test_hierarchy_must_rebind_after_an_a1_boundary_package_is_materialized(
    tmp_path: Path,
) -> None:
    root, document, structure, assets, projection = _package(tmp_path, "txt")
    report, project_state = _report((root, document, structure, assets, projection))
    old_units = [row["unit_id"] for row in report["units"]]
    stale_hierarchy_plan = build_hierarchy_plan(
        report,
        [_hierarchy_spec(old_units[1], old_units[0])],
        proposer={"kind": "human", "identifier": "pre-boundary-reviewer"},
    )

    a1_plan = _sealed_plan(
        report,
        [_update_action(old_units[0], title="Human approved opening")],
    )
    write_result = write_experimental_draft_package(
        document,
        structure,
        assets,
        projection,
        project_state,
        report,
        a1_plan,
        tmp_path / "post_boundary_package",
        package_root=root,
    )
    new_root = write_result.output_dir
    new_document = _load_json(write_result.document_path)
    new_structure = _load_json(write_result.structure_manifest_path)
    new_assets = _load_json(new_root / "asset_manifest.json")
    new_projection = _load_json(write_result.admitted_projection_path)
    new_state = _state(new_document["doc_id"])
    new_report = build_draft_structure_report(
        new_document,
        new_structure,
        new_assets,
        new_projection,
        new_state,
        package_root=new_root,
    )

    with pytest.raises(DraftStructureError, match="stale or tampered"):
        apply_hierarchy_plan(
            new_document,
            new_structure,
            new_assets,
            new_projection,
            new_state,
            report,
            stale_hierarchy_plan,
            package_root=new_root,
        )

    new_units = [row["unit_id"] for row in new_report["units"]]
    rebound_plan = build_hierarchy_plan(
        new_report,
        [_hierarchy_spec(new_units[1], new_units[0])],
        proposer={"kind": "human", "identifier": "post-boundary-reviewer"},
    )
    overlay = apply_hierarchy_plan(
        new_document,
        new_structure,
        new_assets,
        new_projection,
        new_state,
        new_report,
        rebound_plan,
        package_root=new_root,
    )
    assert overlay["inputs"]["report"]["sha256"] == canonical_json_sha256(
        new_report
    )
