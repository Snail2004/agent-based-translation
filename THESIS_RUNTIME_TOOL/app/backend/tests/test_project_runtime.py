from __future__ import annotations

import json
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
