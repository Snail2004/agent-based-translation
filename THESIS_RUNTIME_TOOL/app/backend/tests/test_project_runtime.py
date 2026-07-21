from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = BACKEND_ROOT.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))


def _document(doc_id: str, *, extra_block: bool = False) -> dict:
    blocks = [
        {
            "block_id": f"{doc_id}_ch01_b001",
            "order_index": 1,
            "block_type": "heading",
            "is_chapter_opening": True,
            "source_text": "Chapter One",
            "clean_text": "Chapter One",
            "annotations": {"review": "must not enter runtime"},
        },
        {
            "block_id": f"{doc_id}_ch01_b002",
            "order_index": 2,
            "block_type": "paragraph",
            "is_chapter_opening": False,
            "source_text": "Alice arrived.",
            "clean_text": "Alice arrived.",
            "annotations": {"entity": "gold-only"},
        },
    ]
    if extra_block:
        blocks.append({
            "block_id": f"{doc_id}_ch01_b003",
            "order_index": 3,
            "block_type": "paragraph",
            "is_chapter_opening": False,
            "source_text": "Bob waited.",
            "clean_text": "Bob waited.",
            "annotations": {},
        })
    return {
        "schema_version": "1.5.0",
        "doc_id": doc_id,
        "metadata": {"title": "Runtime fixture", "source_language": "en", "target_language": "vi"},
        "chapters": [{
            "chapter_id": f"{doc_id}_ch01",
            "order_index": 1,
            "title": "Chapter One",
            "blocks": blocks,
        }],
    }


def _project(tmp_path: Path, doc_id: str, *, extra_block: bool = False) -> Path:
    root = tmp_path / "projects" / doc_id
    canonical = root / "canonical"
    canonical.mkdir(parents=True)
    (canonical / "document.json").write_text(
        json.dumps(_document(doc_id, extra_block=extra_block), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def _canonical_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_structure_manifest(project: Path, doc_id: str, *, epub_file: str = "chapter.xhtml") -> dict:
    document_path = project / "canonical" / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document.setdefault("metadata", {})["raw_sha256"] = "a" * 64
    document_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    chapter = document["chapters"][0]
    block_ids = [block["block_id"] for block in chapter["blocks"]]
    units = [{
        "unit_id": "u0001_chapter-one",
        "chapter_id": chapter["chapter_id"],
        "order_index": 0,
        "title": chapter["title"],
        "block_range": [0, len(block_ids)],
        "role": "content_unit",
        "translation_policy": "translate",
        "parent_unit_id": None,
        "source_target": {"file": epub_file, "anchor": None},
        "confidence": 1.0,
        "evidence": ["fixture"],
        "review_required": False,
    }]
    source_map = [
        {
            "block_id": block_id,
            "epub_file": epub_file,
            "epub_anchor": None,
            "pandoc_path": [index],
            "provenance_precision": "fixture",
        }
        for index, block_id in enumerate(block_ids)
    ]
    manifest = {
        "schema_version": "epub_structure_manifest_v1",
        "normalizer_version": "fixture_v1",
        "doc_id": doc_id,
        "source": {"path": "source.epub", "sha256": "a" * 64, "format": "epub"},
        "units": units,
        "translatable_chapter_ids": [chapter["chapter_id"]],
        "review_required_chapter_ids": [],
        "exact_cover": {
            "expected_blocks": len(block_ids),
            "covered_blocks": len(block_ids),
            "overlap_count": 0,
            "missing_count": 0,
            "coverage": 1.0,
        },
        "source_map": source_map,
    }
    manifest["structure_sha256"] = _canonical_hash({
        "normalizer_version": manifest["normalizer_version"],
        "source_sha256": manifest["source"]["sha256"],
        "units": units,
        "source_map": source_map,
    })
    (project / "canonical" / "structure_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _reseal_structure_manifest(manifest: dict) -> None:
    manifest["structure_sha256"] = _canonical_hash({
        "normalizer_version": manifest["normalizer_version"],
        "source_sha256": manifest["source"]["sha256"],
        "units": manifest["units"],
        "source_map": manifest["source_map"],
    })


def _managed_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doc_id: str,
) -> tuple[Path, Path, object, object]:
    from services import project_runtime, source_lifecycle

    root = tmp_path / "projects" / doc_id
    for name in ("raw", "canonical", "working", "logs", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "raw" / "source.txt").write_text(
        "CHAPTER I\n\nAlice arrived.\n\nBob waited.\n",
        encoding="utf-8",
        newline="\n",
    )
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(
        project_runtime,
        "get_project_path",
        lambda value: root if value == doc_id else None,
    )
    monkeypatch.setattr(project_runtime, "THESIS_JOBS_ROOT", jobs)
    monkeypatch.setattr(source_lifecycle, "THESIS_JOBS_ROOT", jobs)
    source_lifecycle.normalize_managed_source_package(root, doc_id)
    return root, jobs, project_runtime, source_lifecycle


def test_managed_runtime_requires_finalization_and_snapshots_complete_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "managed_runtime_v2"
    root, jobs, project_runtime, source_lifecycle = _managed_project(
        tmp_path, monkeypatch, doc_id
    )

    draft = project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert draft["contract_version"] == "project_runtime_source_v2"
    assert draft["lifecycle"] == "draft"
    assert draft["prepare_allowed"] is False
    assert draft["job_id"] is None
    with pytest.raises(project_runtime.ProjectRuntimeError) as not_finalized:
        project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert not_finalized.value.code == "source_package_not_finalized"

    review = source_lifecycle.get_source_package_review(root, doc_id)
    finalized = source_lifecycle.finalize_managed_source_package(
        root,
        doc_id,
        {
            "expected_state_sha256": review["expected"]["state_sha256"],
            "expected_candidate_tree_sha256": review["expected"][
                "candidate_tree_sha256"
            ],
            "expected_report_sha256": review["expected"]["report_sha256"],
            "expected_hierarchy_sha256": review["expected"]["hierarchy_sha256"],
            "approved": True,
            "user": "runtime_reviewer",
        },
    )
    prepared = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)

    assert prepared["created"] is True
    assert prepared["prepared"] is True
    assert prepared["contract_version"] == "project_runtime_source_v2"
    assert prepared["lifecycle"] == "finalized_pre_run"
    assert prepared["managed_source"]["state_sha256"] == finalized["state_sha256"]
    job_root = jobs / prepared["job_id"]
    manifest = json.loads(
        (job_root / "source_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["managed_source"] == prepared["managed_source"]
    assert manifest["source_package_snapshot"]["tree_sha256"] == finalized[
        "candidate"
    ]["tree_sha256"]
    assert manifest["lifecycle_snapshot"]["payload_sha256"] == finalized[
        "state_sha256"
    ]
    assert manifest["finalization_snapshot"]["payload_sha256"] == finalized[
        "revision"
    ]["finalization"]["sha256"]
    assert manifest["hierarchy_snapshot"] is None
    assert (job_root / "source_package_snapshot" / "asset_manifest.json").is_file()
    assert (job_root / "source_package_snapshot" / "admitted_projection_v1.json").is_file()
    assert (job_root / "lifecycle_snapshot" / "finalization.json").is_file()

    reused = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert reused["created"] is False
    assert reused["job_id"] == prepared["job_id"]
    source_status = source_lifecycle.get_source_package_status(root, doc_id)
    assert source_status["lifecycle"] == "finalized_pre_run"


def test_managed_runtime_first_run_freeze_is_idempotent_and_permanent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "managed_runtime_freeze"
    root, jobs, project_runtime, source_lifecycle = _managed_project(
        tmp_path, monkeypatch, doc_id
    )
    review = source_lifecycle.get_source_package_review(root, doc_id)
    source_lifecycle.finalize_managed_source_package(
        root,
        doc_id,
        {
            "expected_state_sha256": review["expected"]["state_sha256"],
            "expected_candidate_tree_sha256": review["expected"][
                "candidate_tree_sha256"
            ],
            "expected_report_sha256": review["expected"]["report_sha256"],
            "expected_hierarchy_sha256": review["expected"]["hierarchy_sha256"],
            "approved": True,
            "user": "runtime_reviewer",
        },
    )
    prepared = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)

    first = project_runtime.freeze_managed_runtime_for_run(
        prepared["job_id"], "run_first", jobs_root=jobs
    )
    assert first is not None
    assert first["lifecycle"] == "run_started_frozen"
    assert first["pipeline_run_count"] == 1
    assert first["run_start_created"] is True

    repeated = project_runtime.freeze_managed_runtime_for_run(
        prepared["job_id"], "run_first", jobs_root=jobs
    )
    assert repeated is not None
    assert repeated["run_start_reused"] is True

    with pytest.raises(project_runtime.ProjectRuntimeError) as alternate:
        project_runtime.freeze_managed_runtime_for_run(
            prepared["job_id"], "run_other", jobs_root=jobs
        )
    assert alternate.value.code == "source_package_already_frozen"

    frozen = project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert frozen["prepared"] is True
    assert frozen["prepare_allowed"] is False
    assert frozen["lifecycle"] == "run_started_frozen"
    assert frozen["pipeline_run_count"] == 1
    prepared_again = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert prepared_again["created"] is False
    assert prepared_again["job_id"] == prepared["job_id"]

    (jobs / prepared["job_id"] / "source_manifest.json").unlink()
    with pytest.raises(project_runtime.ProjectRuntimeError) as missing:
        project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert missing.value.code in {
        "source_package_runtime_missing",
        "managed_runtime_missing_after_freeze",
    }


def test_prepare_runtime_is_content_addressed_and_strips_annotations(tmp_path, monkeypatch):
    from services import project_runtime

    doc_id = "imported_story"
    project = _project(tmp_path, doc_id)
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: project if value == doc_id else None)

    before = project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert before["prepared"] is False

    created = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert created["created"] is True
    assert created["prepared"] is True
    assert created["chapter_count"] == 1
    assert created["block_count"] == 2
    assert created["profiles"] == ["technical_d2l_v1", "literary_v1"]

    job_dir = jobs / created["job_id"]
    snapshot = json.loads((job_dir / "source_snapshot" / "document.json").read_text(encoding="utf-8"))
    assert all(block["annotations"] == {} for chapter in snapshot["chapters"] for block in chapter["blocks"])

    with sqlite3.connect(job_dir / "memory.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

    from services.thesis_readmodel import list_thesis_datasets

    assert list_thesis_datasets(jobs_root=jobs) == []

    reused = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert reused["created"] is False
    assert reused["job_id"] == created["job_id"]


def test_source_change_creates_a_new_runtime_identity(tmp_path, monkeypatch):
    from services import project_runtime

    doc_id = "changing_story"
    project = _project(tmp_path, doc_id)
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: project if value == doc_id else None)

    first = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    (project / "canonical" / "document.json").write_text(
        json.dumps(_document(doc_id, extra_block=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    status = project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert status["prepared"] is False
    assert status["job_id"] != first["job_id"]

    second = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert second["job_id"] != first["job_id"]
    assert second["block_count"] == 3
    assert (jobs / first["job_id"] / "memory.sqlite3").is_file()
    assert (jobs / second["job_id"] / "memory.sqlite3").is_file()


def test_structure_manifest_is_snapshotted_and_changes_runtime_identity(tmp_path, monkeypatch):
    from services import project_runtime

    doc_id = "structured_story"
    project = _project(tmp_path, doc_id)
    _write_structure_manifest(project, doc_id)
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: project if value == doc_id else None)

    first = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    assert first["translatable_chapter_ids"] == [f"{doc_id}_ch01"]
    assert first["chapters"][0]["unit_role"] == "content_unit"
    snapshot = jobs / first["job_id"] / "source_snapshot" / "structure_manifest.json"
    assert snapshot.is_file()

    _write_structure_manifest(project, doc_id, epub_file="renamed-chapter.xhtml")
    status = project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert status["prepared"] is False
    assert status["job_id"] != first["job_id"]


@pytest.mark.parametrize(
    ("review_required", "review_chapter_ids"),
    [
        (0, []),
        ("false", ["structured_invalid_review_ch01"]),
    ],
)
def test_structure_manifest_rejects_non_boolean_review_required(
    tmp_path,
    review_required,
    review_chapter_ids,
):
    from services.structure_manifest import validate_structure_manifest

    doc_id = "structured_invalid_review"
    project = _project(tmp_path, doc_id)
    manifest = _write_structure_manifest(project, doc_id)
    document = json.loads(
        (project / "canonical" / "document.json").read_text(encoding="utf-8")
    )
    manifest["units"][0]["review_required"] = review_required
    manifest["review_required_chapter_ids"] = review_chapter_ids
    _reseal_structure_manifest(manifest)

    with pytest.raises(ValueError, match="review_required must be a boolean"):
        validate_structure_manifest(document, manifest)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("units", "Every structure manifest unit must be an object"),
        ("source_map", "Every structure manifest source_map row must be an object"),
    ],
)
def test_structure_manifest_rejects_null_rows_as_value_error(tmp_path, field, message):
    from services.structure_manifest import validate_structure_manifest

    doc_id = "structured_null_row"
    project = _project(tmp_path, doc_id)
    manifest = _write_structure_manifest(project, doc_id)
    document = json.loads(
        (project / "canonical" / "document.json").read_text(encoding="utf-8")
    )
    manifest[field] = [None]
    _reseal_structure_manifest(manifest)

    with pytest.raises(ValueError, match=message):
        validate_structure_manifest(document, manifest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source", None, "Structure manifest source must be an object"),
        ("exact_cover", [], "Structure manifest exact_cover must be an object"),
        ("units", {}, "Structure manifest units must be a list"),
        ("source_map", {}, "Structure manifest source_map must be a list"),
    ],
)
def test_structure_manifest_rejects_invalid_container_shapes(
    tmp_path,
    field,
    value,
    message,
):
    from services.structure_manifest import validate_structure_manifest

    doc_id = "structured_invalid_container"
    project = _project(tmp_path, doc_id)
    manifest = _write_structure_manifest(project, doc_id)
    document = json.loads(
        (project / "canonical" / "document.json").read_text(encoding="utf-8")
    )
    manifest[field] = value

    with pytest.raises(ValueError, match=message):
        validate_structure_manifest(document, manifest)


def test_tampered_runtime_structure_snapshot_fails_closed(tmp_path, monkeypatch):
    from services import project_runtime

    doc_id = "structured_tamper"
    project = _project(tmp_path, doc_id)
    _write_structure_manifest(project, doc_id)
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: project if value == doc_id else None)

    prepared = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    snapshot = jobs / prepared["job_id"] / "source_snapshot" / "structure_manifest.json"
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(project_runtime.ProjectRuntimeError) as caught:
        project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert caught.value.code == "runtime_structure_tampered"


def test_concurrent_publish_reuses_validated_destination_on_windows(tmp_path, monkeypatch):
    from services import project_runtime

    doc_id = "concurrent_story"
    project = _project(tmp_path, doc_id)
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: project if value == doc_id else None)
    real_replace = project_runtime.os.replace

    def publish_then_report_permission_error(source, target):
        source_path = Path(source)
        is_directory_publish = source_path.is_dir()
        real_replace(source, target)
        if is_directory_publish:
            raise PermissionError("simulated Windows destination race")

    monkeypatch.setattr(project_runtime.os, "replace", publish_then_report_permission_error)
    prepared = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)

    assert prepared["created"] is False
    assert prepared["prepared"] is True
    assert (jobs / prepared["job_id"] / "source_manifest.json").is_file()


def test_tampered_or_incomplete_runtime_fails_closed(tmp_path, monkeypatch):
    from services import project_runtime

    doc_id = "tamper_story"
    project = _project(tmp_path, doc_id)
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: project if value == doc_id else None)
    prepared = project_runtime.prepare_project_runtime(doc_id, jobs_root=jobs)
    snapshot = jobs / prepared["job_id"] / "source_snapshot" / "document.json"
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(project_runtime.ProjectRuntimeError) as caught:
        project_runtime.get_project_runtime_status(doc_id, jobs_root=jobs)
    assert caught.value.code == "runtime_snapshot_tampered"

    other_id = "incomplete_story"
    other_project = _project(tmp_path, other_id)
    monkeypatch.setattr(project_runtime, "get_project_path", lambda value: other_project if value == other_id else None)
    pending = project_runtime.get_project_runtime_status(other_id, jobs_root=jobs)
    (jobs / pending["job_id"]).mkdir(parents=True)
    with pytest.raises(project_runtime.ProjectRuntimeError) as incomplete:
        project_runtime.get_project_runtime_status(other_id, jobs_root=jobs)
    assert incomplete.value.code == "runtime_incomplete"
