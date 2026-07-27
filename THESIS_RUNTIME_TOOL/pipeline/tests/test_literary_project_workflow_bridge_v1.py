from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

from pipeline.literary.chapter_loop_component_contract_v1 import (
    LiteraryChapterLoopComponentError,
    build_literary_app_run_registration_v1,
    build_literary_workflow_handoff_v1,
    validate_literary_app_run_registration_v1,
    validate_literary_chapter_loop_component_v1,
)
from pipeline.literary.chapter_loop_workflow_replay_adapter_v1 import (
    sync_literary_chapter_loop_replay_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.project_source_bridge_v1 import (
    LiteraryProjectSourceBridgeError,
    prepare_literary_project_source_v1,
)
from pipeline.scripts import run_literary_chapter_loop_v1 as chapter_loop
from pipeline.scripts import run_literary_project_chapter_loop_v1 as project_loop
from pipeline.scripts.run_literary_project_chapter_loop_v1 import _run_paths


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = RUNTIME_ROOT / "pipeline" / "configs"
PROFILE = CONFIG_ROOT / "literary_chapter_loop_profile_v1.json"
STAGE_BINDINGS = CONFIG_ROOT / "literary_chapter_loop_stage_bindings_v1.json"
REAL_PROJECT_ROOT = Path(
    os.environ.get("LITERARY_REAL_PROJECT_ROOT", "__project_not_configured__")
)
SOURCE_MAIN_RUNTIME = Path(
    os.environ.get(
        "LITERARY_SOURCE_MAIN_RUNTIME_ROOT",
        "__source_main_not_configured__",
    )
)
SOURCE_MAIN_PIPELINE = SOURCE_MAIN_RUNTIME / "pipeline"


def test_project_projection_selects_only_translatable_chapters_without_mutation(
    tmp_path: Path,
) -> None:
    job_root = _project_job(tmp_path)
    before = _tree_hashes(job_root)

    report = prepare_literary_project_source_v1(
        job_root=job_root,
        output_root=tmp_path / "projection",
    )

    projected = _read(Path(report["document_path"]))
    assert [row["chapter_id"] for row in projected["chapters"]] == [
        "fixture_ch01",
        "fixture_ch02",
    ]
    assert projected["chapters"][0]["blocks"][0]["clean_text"] == "Chapter 1."
    assert report["canonical_project_mutated"] is False
    assert _tree_hashes(job_root) == before


def test_project_projection_rejects_noncontiguous_or_foreign_selection(
    tmp_path: Path,
) -> None:
    job_root = _project_job(tmp_path, chapter_count=3)

    with pytest.raises(
        LiteraryProjectSourceBridgeError, match="must be contiguous"
    ):
        prepare_literary_project_source_v1(
            job_root=job_root,
            output_root=tmp_path / "projection_a",
            chapter_ids=["fixture_ch01", "fixture_ch03"],
        )
    with pytest.raises(
        LiteraryProjectSourceBridgeError, match="foreign chapter"
    ):
        prepare_literary_project_source_v1(
            job_root=job_root,
            output_root=tmp_path / "projection_b",
            chapter_ids=["another_book_ch01"],
        )


@pytest.mark.skipif(not REAL_PROJECT_ROOT.is_dir(), reason="real App project absent")
def test_real_wuthering_heights_project_projects_34_canonical_chapters(
    tmp_path: Path,
) -> None:
    before = _tree_hashes(REAL_PROJECT_ROOT)

    report = prepare_literary_project_source_v1(
        job_root=REAL_PROJECT_ROOT,
        output_root=tmp_path / "wuthering_heights_projection",
    )

    document = _read(Path(report["document_path"]))
    assert report["chapter_count"] == 34
    assert len(document["chapters"]) == 34
    assert len(document["chapters"][0]["blocks"]) == 28
    assert len(document["chapters"][1]["blocks"]) == 92
    assert document["chapters"][0]["title"] == "CHAPTER I"
    assert document["chapters"][-1]["title"] == "CHAPTER XXXIV"
    assert _tree_hashes(REAL_PROJECT_ROOT) == before


def test_component_contract_and_handoff_are_project_bound_and_relay_shaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_root = _project_job(tmp_path)
    projection = prepare_literary_project_source_v1(
        job_root=job_root,
        output_root=tmp_path / "projection",
    )
    runtime_bindings = _runtime_bindings(tmp_path)
    run_root = tmp_path / "component"
    monkeypatch.setattr(chapter_loop, "_clean_head", lambda: "a" * 40)
    args = argparse.Namespace(
        document=Path(projection["document_path"]),
        run_root=run_root,
        run_id="literary_fixture_run",
        frozen_db=Path(projection["frozen_db_path"]),
        runtime_bindings=runtime_bindings,
        project_binding=Path(projection["project_binding_path"]),
        profile=PROFILE,
        stage_bindings=STAGE_BINDINGS,
        stop_after_chapter_count=2,
        chapter_id=None,
        chapter_range=None,
        all_chapters=True,
    )
    chapter_loop._initialize(args)

    validation = validate_literary_chapter_loop_component_v1(run_root)
    handoff = build_literary_workflow_handoff_v1(
        component_root=run_root,
        component_root_ref="_work/literary/fixture_job/literary_fixture_run/component",
    )

    assert validation["manifest"]["component_id"] == "translation"
    assert validation["events"][0]["component_seq"] == 1
    assert "seq" not in validation["events"][0]
    assert handoff["job_id"] == "fixture_job"
    assert handoff["project_id"] == "fixture_project"
    assert len(handoff["stage_definitions"]) == 32
    assert handoff["stage_definitions"][0]["stage_id"] == (
        "translation.ch001_b1_scan"
    )
    assert handoff["stage_definitions"][-1]["local_stage_id"] == (
        "ch002_checkpoint"
    )


def test_component_contract_rejects_parent_seq_and_artifact_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _initialized_component(tmp_path, monkeypatch)
    events_path = run_root / "events.jsonl"
    row = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    row["seq"] = 1
    events_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(
        LiteraryChapterLoopComponentError, match="cannot assign parent seq"
    ):
        validate_literary_chapter_loop_component_v1(run_root)

    run_root = _initialized_component(
        tmp_path / "artifact_case", monkeypatch
    )
    history = chapter_loop.LiteraryChapterLoopHistoryV1(
        run_root=run_root, run_id="literary_fixture_run"
    )
    stage_root = run_root / "artifacts" / "chapters" / "ch001" / "b1_scan"
    _write(
        stage_root / "b1_scan_artifact.json",
        {
            "schema_version": "fixture_scan_v1",
            "entity_observations": [{"observation_id": "obs1"}],
        },
    )
    report = _write(stage_root / "canary_report.json", {"provider_calls": 0})
    history.record_stage(
        stage_id="ch001_b1_scan",
        stage_name="b1_scan",
        chapter_id="fixture_ch01",
        stage_root=stage_root,
        output_names=("b1_scan_artifact.json", "canary_report.json"),
        parent_artifact_refs=(),
        report_path=report,
        status="accepted",
    )
    (stage_root / "b1_scan_artifact.json").write_text(
        '{"tampered":true}\n', encoding="utf-8"
    )
    with pytest.raises(
        LiteraryChapterLoopComponentError, match="physical hash drifted"
    ):
        validate_literary_chapter_loop_component_v1(run_root)


def test_project_runner_paths_are_deterministic_and_project_neutral(
    tmp_path: Path,
) -> None:
    paths = _run_paths(
        jobs_root=tmp_path,
        job_id="project_alpha",
        run_id="literary_run_07",
    )

    assert paths["component_root"] == (
        tmp_path
        / "_work"
        / "literary"
        / "project_alpha"
        / "literary_run_07"
        / "component"
    )
    assert "wuthering" not in str(paths["component_root"]).lower()
    assert paths["workflow_replay_root"] == (
        tmp_path
        / "_work"
        / "workflow_replay"
        / "project_alpha"
        / "literary_run_07"
    )


def test_project_runner_forwards_capability_override_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[str] = []

    def fake_main(argv: list[str]) -> int:
        forwarded.extend(argv)
        return 0

    monkeypatch.setattr(project_loop.chapter_loop, "main", fake_main)
    result = project_loop.main(
        [
            "resume",
            "--jobs-root",
            str(tmp_path),
            "--job-id",
            "project_alpha",
            "--run-id",
            "literary_run_07",
            "--credential-file",
            str(tmp_path / "credential.txt"),
            "--scheduler-root",
            str(tmp_path / "scheduler"),
            "--capability-override",
            "speaker_recovery.default=C:/probe/current",
            "--runtime-profile-override",
            "b0_summary=C:/profiles/b0-capacity.json",
        ]
    )

    assert result == 0
    selector = forwarded.index("--capability-override")
    assert forwarded[selector + 1] == (
        "speaker_recovery.default=C:/probe/current"
    )
    profile_selector = forwarded.index("--runtime-profile-override")
    assert forwarded[profile_selector + 1] == (
        "b0_summary=C:/profiles/b0-capacity.json"
    )


def test_replay_defaults_to_the_lineage_origin_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _initialized_component(tmp_path, monkeypatch)
    session_path = run_root / "chapter_loop_session.json"
    session = _read(session_path)
    session_body = dict(session)
    session_body.pop("session_hash")
    session_body["active_code_revision"] = "b" * 40
    session_body["code_revision_history"] = ["a" * 40, "b" * 40]
    session_body["session_hash"] = canonical_hash(session_body)
    _write(session_path, session_body)

    assert project_loop._initial_code_revision(run_root) == "a" * 40


def test_project_runner_can_seal_an_explicit_frozen_db_without_mutating_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "jobs"
    job_root = _project_job(jobs_root)
    before = _tree_hashes(job_root)
    runtime_bindings = _runtime_bindings(tmp_path)
    frozen_db = tmp_path / "accepted-frozen.sqlite3"
    frozen_db.write_bytes(b"accepted-frozen-baseline")
    monkeypatch.setattr(chapter_loop, "_clean_head", lambda: "a" * 40)

    result = project_loop.main(
        [
            "init",
            "--jobs-root",
            str(jobs_root),
            "--job-id",
            "fixture_job",
            "--run-id",
            "literary_fixture_frozen_override",
            "--runtime-bindings",
            str(runtime_bindings),
            "--frozen-db",
            str(frozen_db),
            "--chapter-count",
            "1",
        ]
    )

    assert result == 0
    paths = _run_paths(
        jobs_root=jobs_root,
        job_id="fixture_job",
        run_id="literary_fixture_frozen_override",
    )
    plan = _read(paths["component_root"] / "run_plan.json")
    assert plan["frozen_db_path"] == str(frozen_db.resolve())
    assert _tree_hashes(job_root) == before


def test_app_registration_handoff_is_project_bound_and_non_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _initialized_component(tmp_path, monkeypatch)
    registration = build_literary_app_run_registration_v1(
        component_root=run_root,
        component_root_ref=(
            "_work/literary/fixture_job/literary_fixture_run/component"
        ),
        workflow_replay_root_ref=(
            "_work/workflow_replay/fixture_job/literary_fixture_run"
        ),
    )

    validated = validate_literary_app_run_registration_v1(registration)
    assert validated["project_id"] == "fixture_project"
    assert validated["job_id"] == "fixture_job"
    assert validated["run_id"] == "literary_fixture_run"
    assert validated["workflow_run_id"] == "literary_fixture_run"
    assert validated["selected_chapter_ids"] == [
        "fixture_ch01",
        "fixture_ch02",
    ]
    assert validated["launch_authority"] == "none"
    assert not Path(validated["component_root_ref"]).is_absolute()

    tampered = dict(registration)
    tampered["job_id"] = "another_job"
    with pytest.raises(
        LiteraryChapterLoopComponentError,
        match="registration hash drifted",
    ):
        validate_literary_app_run_registration_v1(tampered)


@pytest.mark.skipif(
    not (SOURCE_MAIN_PIPELINE / "workflow_replay" / "relay_v1.py").is_file(),
    reason="source-main WorkflowRelayV1 absent",
)
def test_project_runner_syncs_to_app_discovery_path_and_writes_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "jobs"
    _project_job(jobs_root)
    runtime_bindings = _runtime_bindings(tmp_path)
    monkeypatch.setattr(chapter_loop, "_clean_head", lambda: "a" * 40)
    result = project_loop.main(
        [
            "init",
            "--jobs-root",
            str(jobs_root),
            "--job-id",
            "fixture_job",
            "--run-id",
            "literary_fixture_app_run",
            "--runtime-bindings",
            str(runtime_bindings),
            "--chapter-count",
            "1",
        ]
    )
    assert result == 0
    bindings_path = _write(
        tmp_path / "source_package_bindings.json",
        _source_bindings(),
    )

    result = project_loop.main(
        [
            "sync-replay",
            "--jobs-root",
            str(jobs_root),
            "--job-id",
            "fixture_job",
            "--run-id",
            "literary_fixture_app_run",
            "--source-package-bindings",
            str(bindings_path),
            "--workflow-runtime-root",
            str(SOURCE_MAIN_PIPELINE.parent),
            "--code-commit",
            "a" * 40,
        ]
    )

    assert result == 0
    paths = _run_paths(
        jobs_root=jobs_root,
        job_id="fixture_job",
        run_id="literary_fixture_app_run",
    )
    assert (paths["workflow_replay_root"] / "workflow_manifest.json").is_file()
    registration = validate_literary_app_run_registration_v1(
        _read(paths["registration_path"])
    )
    assert registration["workflow_replay_root_ref"] == (
        "_work/workflow_replay/fixture_job/literary_fixture_app_run"
    )
    run_manifest = _read(paths["run_manifest_path"])
    assert run_manifest["app_run_registration_ref"] == (
        "_work/literary/fixture_job/literary_fixture_app_run/"
        "literary_app_run_registration.json"
    )


@pytest.mark.skipif(
    not (SOURCE_MAIN_PIPELINE / "workflow_replay" / "relay_v1.py").is_file(),
    reason="source-main WorkflowRelayV1 absent",
)
def test_literary_adapter_ingests_into_existing_workflow_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _initialized_component(tmp_path, monkeypatch)
    history = chapter_loop.LiteraryChapterLoopHistoryV1(
        run_root=run_root, run_id="literary_fixture_run"
    )
    stage_root = run_root / "artifacts" / "chapters" / "ch001" / "b1_scan"
    _write(
        stage_root / "b1_scan_artifact.json",
        {
            "schema_version": "fixture_scan_v1",
            "entity_observations": [{"observation_id": "obs1"}],
        },
    )
    report_path = _write(
        stage_root / "canary_report.json", {"provider_calls": 0}
    )
    history.record_stage(
        stage_id="ch001_b1_scan",
        stage_name="b1_scan",
        chapter_id="fixture_ch01",
        stage_root=stage_root,
        output_names=("b1_scan_artifact.json", "canary_report.json"),
        parent_artifact_refs=(),
        report_path=report_path,
        status="accepted",
    )
    handoff = build_literary_workflow_handoff_v1(
        component_root=run_root,
        component_root_ref="_work/literary/fixture_job/literary_fixture_run/component",
    )
    handoff_path = _write(
        run_root / "literary_workflow_handoff.json", handoff
    )

    report = sync_literary_chapter_loop_replay_v1(
        component_root=run_root,
        handoff_path=handoff_path,
        relay_root=tmp_path / "workflow_relay",
        source_package_bindings=_source_bindings(),
        code_commit="a" * 40,
        workflow_runtime_root=SOURCE_MAIN_PIPELINE.parent,
    )

    assert report["workflow_run_id"] == "literary_fixture_run"
    assert report["event_count"] == 5
    assert report["artifact_count"] == 3
    assert report["workflow_status"] == "running"
    parent_manifest = _read(
        tmp_path / "workflow_relay" / "workflow_manifest.json"
    )
    assert parent_manifest["components"][0]["component_id"] == "translation"
    assert parent_manifest["stages"][0]["stage_id"] == (
        "translation.ch001_b1_scan"
    )

    first_index = _read(run_root / "artifact_index.json")
    enrich_root = (
        run_root / "artifacts" / "chapters" / "ch001" / "b1_enrich"
    )
    _write(
        enrich_root / "b1_enrich_artifact.json",
        {
            "schema_version": "fixture_enrich_v1",
            "entities": [{"entity_id": "entity_1"}],
        },
    )
    enrich_report = _write(
        enrich_root / "canary_report.json", {"provider_calls": 0}
    )
    history.record_stage(
        stage_id="ch001_b1_enrich",
        stage_name="b1_enrich",
        chapter_id="fixture_ch01",
        stage_root=enrich_root,
        output_names=("b1_enrich_artifact.json", "canary_report.json"),
        parent_artifact_refs=(
            first_index["artifacts"][0]["artifact_ref"],
        ),
        report_path=enrich_report,
        status="accepted",
    )
    second_report = sync_literary_chapter_loop_replay_v1(
        component_root=run_root,
        handoff_path=handoff_path,
        relay_root=tmp_path / "workflow_relay",
        source_package_bindings=_source_bindings(),
        code_commit="a" * 40,
        workflow_runtime_root=SOURCE_MAIN_PIPELINE.parent,
    )
    assert second_report["event_count"] == 9
    assert second_report["artifact_count"] == 6


def _initialized_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    job_root = _project_job(tmp_path)
    projection = prepare_literary_project_source_v1(
        job_root=job_root,
        output_root=tmp_path / "projection",
    )
    run_root = tmp_path / "component"
    monkeypatch.setattr(chapter_loop, "_clean_head", lambda: "a" * 40)
    chapter_loop._initialize(
        argparse.Namespace(
            document=Path(projection["document_path"]),
            run_root=run_root,
            run_id="literary_fixture_run",
            frozen_db=Path(projection["frozen_db_path"]),
            runtime_bindings=_runtime_bindings(tmp_path),
            project_binding=Path(projection["project_binding_path"]),
            profile=PROFILE,
            stage_bindings=STAGE_BINDINGS,
            stop_after_chapter_count=1,
            chapter_id=None,
            chapter_range=None,
            all_chapters=True,
        )
    )
    return run_root


def _project_job(tmp_path: Path, *, chapter_count: int = 2) -> Path:
    job_root = tmp_path / "fixture_job"
    snapshot = job_root / "source_package_snapshot"
    chapters = []
    manifest_chapters = [
        {
            "chapter_id": "fixture_front",
            "order_index": 1,
            "title": "Front",
            "block_count": 1,
            "translation_policy": "preserve",
        }
    ]
    for ordinal in range(1, chapter_count + 1):
        chapter_id = f"fixture_ch{ordinal:02d}"
        chapters.append(
            {
                "chapter_id": chapter_id,
                "order_index": ordinal + 1,
                "title": f"Chapter {ordinal}",
                "blocks": [
                    {
                        "block_id": f"{chapter_id}_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": f"Chapter {ordinal}.",
                    }
                ],
            }
        )
        manifest_chapters.append(
            {
                "chapter_id": chapter_id,
                "order_index": ordinal + 1,
                "title": f"Chapter {ordinal}",
                "block_count": 1,
                "translation_policy": "translate",
            }
        )
    document = {
        "schema_version": "fixture_document_v1",
        "doc_id": "fixture_document",
        "metadata": {},
        "chapters": [
            {
                "chapter_id": "fixture_front",
                "order_index": 1,
                "title": "Front",
                "blocks": [
                    {
                        "block_id": "fixture_front_b001",
                        "order_index": 1,
                        "block_type": "heading",
                        "clean_text": "Front.",
                    }
                ],
            },
            *chapters,
        ],
    }
    _write(snapshot / "document.json", document)
    _write(
        snapshot / "structure_manifest.json",
        {
            "translatable_chapter_ids": [
                row["chapter_id"] for row in chapters
            ],
            "units": [
                {
                    "chapter_id": row["chapter_id"],
                    "translation_policy": "translate",
                }
                for row in chapters
            ],
        },
    )
    _write(
        job_root / "source_manifest.json",
        {
            "contract_version": "project_runtime_source_v2",
            "job_id": "fixture_job",
            "project_id": "fixture_project",
            "document_doc_id": "fixture_document",
            "source_document": "source_package_snapshot/document.json",
            "source_identity_sha256": "1" * 64,
            "profiles": ["literary_v1"],
            "chapters": manifest_chapters,
        },
    )
    (job_root / "memory.sqlite3").write_bytes(b"frozen-fixture")
    return job_root


def _runtime_bindings(tmp_path: Path) -> Path:
    evidence = _write(
        tmp_path / "capability_evidence.json",
        {
            "verdict": "qualified",
            "source_id": "fixture-source",
            "requested_model_id": "gpt-fixture",
            "observed_model_id": "gpt-fixture",
        },
    )
    names = {
        "b1_scan": ("default",),
        "b1_enrich": ("default",),
        "b1_local_auditor": ("default",),
        "xchapter_hearing": ("identity", "stable_claim"),
        "b2_frame_interaction": ("frame", "interaction"),
        "speaker_recovery": ("default",),
        "b3_temporal": ("default",),
        "b3_auditor": ("default",),
        "b0_summary": ("default",),
    }
    return _write(
        tmp_path / "runtime_bindings.json",
        {
            "schema_version": "literary_chapter_loop_runtime_bindings_v1",
            "binding_id": "fixture-runtime",
            "stages": {
                stage: {
                    "runtime_profile": None,
                    "context_profile": None,
                    "capabilities": {
                        key: str(evidence) for key in capability_names
                    },
                    "source_id": "fixture-source",
                    "model_id": "gpt-fixture",
                }
                for stage, capability_names in names.items()
            },
        },
    )


def _source_bindings() -> list[dict[str, Any]]:
    roles = (
        "document",
        "structure_manifest",
        "asset_manifest",
        "admitted_projection",
        "normalization_receipt",
        "package_seal",
    )
    return [
        {
            "role": role,
            "binding": {
                "artifact_ref": f"source/{role}.json",
                "artifact_kind": role,
                "schema_version": "fixture_v1",
                "sha256": f"{index:x}".rjust(64, "0"),
                "sha256_kind": "physical",
            },
        }
        for index, role in enumerate(roles, start=1)
    ]


def _tree_hashes(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
