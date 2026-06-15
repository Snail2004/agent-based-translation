"""Tests for APP-C01 RunControl.

These tests stay 0-API.  They do launch one real frozen pipeline script
(`snapshot_runs`) to prove module invocation and cwd are wired correctly.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = BACKEND_ROOT.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_doc_db(tmp_path: Path) -> Path:
    from pipeline.ingest.document_loader import load_document
    from pipeline.memory.store_init import migrate_db

    doc = {
        "doc_id": "ti",
        "metadata": {"source_language": "en", "target_language": "vi"},
        "chapters": [
            {
                "chapter_id": "ti_ch02",
                "blocks": [
                    {
                        "block_id": "ch02_b001",
                        "order_index": 0,
                        "block_type": "paragraph",
                        "clean_text": "Hello, Jim.",
                        "source_text": "Hello, Jim.",
                        "annotations": {},
                    },
                    {
                        "block_id": "ch02_b002",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Good day, captain.",
                        "source_text": "Good day, captain.",
                        "annotations": {},
                    },
                ],
            }
        ],
    }
    doc_path = tmp_path / "document.json"
    db_path = tmp_path / "memory.sqlite3"
    _write_json(doc_path, doc)
    load_document(db_path, doc_path)
    conn = migrate_db(db_path)
    conn.close()
    return db_path


def _wait_for_status(registry, run_id: str, done_statuses=("done", "failed", "error")):
    for _ in range(80):
        time.sleep(0.1)
        entry = registry.get_run(run_id)
        if entry and entry["status"] in done_statuses:
            return entry
    return registry.get_run(run_id)


def _reset_app_modules() -> None:
    for name in list(sys.modules):
        if (
            name == "app"
            or name == "config"
            or name == "routes"
            or name.startswith("routes.")
            or name.startswith("services.thesis_")
        ):
            sys.modules.pop(name, None)


def test_build_argv_uses_real_module_invocation_and_no_job_arg(tmp_path):
    from services.thesis_runs import build_argv

    db = tmp_path / "memory.sqlite3"
    argv = build_argv(
        script="run_translate",
        python_exe=sys.executable,
        db=str(db),
        chapters=["ch02"],
        configs=["S0", "S1"],
        profile="literary_v1",
        experiment="ti_test",
        cache=str(tmp_path / "cache.sqlite3"),
        allow_api=True,
    )

    assert argv[:3] == [sys.executable, "-m", "pipeline.scripts.run_translate"]
    assert "--job" not in argv
    assert "--db" in argv
    assert "--preflight-only" not in argv


def test_run_translate_dry_run_forces_preflight_only(tmp_path):
    from services.thesis_runs import build_argv

    argv = build_argv(
        script="run_translate",
        python_exe=sys.executable,
        db=str(tmp_path / "memory.sqlite3"),
        chapters=["ch02"],
        configs=["S0"],
        allow_api=False,
    )
    assert argv[:3] == [sys.executable, "-m", "pipeline.scripts.run_translate"]
    assert "--preflight-only" in argv


def test_api_capable_script_without_safe_dry_run_is_rejected(tmp_path):
    from services.thesis_runs import RunControlError, build_argv

    try:
        build_argv(
            script="run_judge",
            python_exe=sys.executable,
            db=str(tmp_path / "memory.sqlite3"),
            experiment="x",
            compare="S0:S1",
            chapters=["ch02"],
            out=str(tmp_path / "judge.json"),
            allow_api=False,
        )
        assert False, "expected RunControlError"
    except RunControlError as exc:
        assert exc.code == "dry_run_not_supported"


def test_real_pipeline_module_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "pipeline.scripts.snapshot_runs", "--help"],
        cwd=TOOL_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert "--db" in result.stdout
    assert "--out" in result.stdout


def test_run_lifecycle_create_running_done_log_tail(tmp_path):
    from services.thesis_runs import RunRegistry, read_log, spawn_run

    registry = RunRegistry(runs_root=tmp_path)
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "print('line1'); print('line2')"],
        config="S1",
        seed=42,
        model="gpt-test",
        prompt_version="s1_v1",
        cache_path=str(tmp_path / "cache.sqlite3"),
        job_id="test_job",
        experiment="exp_test",
        allow_api=False,
    )

    spawn_run(registry, entry["run_id"])
    final = _wait_for_status(registry, entry["run_id"])
    assert final["status"] == "done"
    assert final["exit_code"] == 0
    assert final["pid"] is not None

    log_result = read_log(registry, entry["run_id"], offset=0)
    assert "line1" in log_result["log"]
    assert "line2" in log_result["log"]
    assert log_result["running"] is False
    assert log_result["exit_code"] == 0

    log_result2 = read_log(registry, entry["run_id"], offset=log_result["offset"])
    assert log_result2["log"] == ""


def test_run_nonzero_exit_code_marked_failed(tmp_path):
    from services.thesis_runs import RunRegistry, spawn_run

    registry = RunRegistry(runs_root=tmp_path)
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
    )
    spawn_run(registry, entry["run_id"])
    final = _wait_for_status(registry, entry["run_id"])
    assert final["status"] == "failed"
    assert final["exit_code"] == 1


def test_run_registry_persists_to_jsonl(tmp_path):
    from services.thesis_runs import RunRegistry

    registry1 = RunRegistry(runs_root=tmp_path)
    entry = registry1.create_run(script="score_run", argv=[sys.executable, "-c", "pass"])
    registry1.update_run(entry["run_id"], status="done", exit_code=0)

    registry2 = RunRegistry(runs_root=tmp_path)
    reloaded = registry2.get_run(entry["run_id"])
    assert reloaded["status"] == "done"
    assert reloaded["exit_code"] == 0


def test_script_outside_allowlist_returns_400():
    from services.thesis_runs import RunControlError, validate_script

    try:
        validate_script("rm_rf_everything")
        assert False, "Expected RunControlError"
    except RunControlError as exc:
        assert exc.status == 400
        assert exc.code == "script_not_allowed"


def test_arg_with_shell_meta_returns_400_but_windows_path_allowed():
    from services.thesis_runs import RunControlError, validate_args

    validate_args([r"C:\tmp\memory.sqlite3", "data/jobs/x.sqlite3"])
    for bad_arg in ["foo;bar", "a|b", "a&&b", "$(evil)", "a`cmd`b", "a>out"]:
        try:
            validate_args(["safe", bad_arg])
            assert False, f"Expected RunControlError for {bad_arg!r}"
        except RunControlError as exc:
            assert exc.status == 400
            assert exc.code == "invalid_arg"


def test_allow_api_true_requires_job_id_and_preview_token(tmp_path):
    from services.thesis_runs import RunControlError, build_argv, validate_api_gate

    argv = build_argv(
        script="run_translate",
        db=str(tmp_path / "memory.sqlite3"),
        chapters=["ch02"],
        configs=["S1"],
        allow_api=True,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_translate",
            confirm_token="anything",
            job_id=None,
            argv=argv,
        )
        assert False, "Expected job_id_required"
    except RunControlError as exc:
        assert exc.code == "job_id_required"


def test_prompt_preview_renders_real_translate_prompt_and_token_is_one_time(tmp_path):
    from services.thesis_runs import generate_prompt_preview, validate_api_gate

    db_path = _make_doc_db(tmp_path)
    cache_path = tmp_path / "translate_cache.sqlite3"
    preview = generate_prompt_preview(
        job_id="preview_job",
        script="run_translate",
        db=str(db_path),
        chapters=["ch02"],
        configs=["S1"],
        profile="literary_v1",
        cache=str(cache_path),
        tool_root=TOOL_ROOT,
        jobs_root=tmp_path,
    )

    assert preview["preview_kind"] == "real_translate_prompt"
    assert preview["confirm_token"]
    assert preview["representative_prompt"]["messages"]
    assert preview["representative_prompt"]["prompt_tokens_est"] > 0
    assert preview["token_estimate"]["configs"]["S1"]["windows"] >= 1
    assert "N/A" not in json.dumps(preview["token_estimate"], ensure_ascii=False)

    argv = preview["argv_preview"]
    token = preview["confirm_token"]
    validate_api_gate(
        allow_api=True,
        script="run_translate",
        confirm_token=token,
        job_id="preview_job",
        argv=argv,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_translate",
            confirm_token=token,
            job_id="preview_job",
            argv=argv,
        )
        assert False, "Expected one-time token rejection"
    except Exception as exc:
        assert getattr(exc, "code", "") == "confirm_token_invalid"


def test_confirm_token_must_match_exact_argv(tmp_path):
    from services.thesis_runs import RunControlError, build_argv, generate_prompt_preview, validate_api_gate

    db_path = _make_doc_db(tmp_path)
    preview = generate_prompt_preview(
        job_id="preview_job",
        script="run_translate",
        db=str(db_path),
        chapters=["ch02"],
        configs=["S1"],
        profile="literary_v1",
        tool_root=TOOL_ROOT,
        jobs_root=tmp_path,
    )
    mismatched = build_argv(
        script="run_translate",
        db=str(db_path),
        chapters=["ch02"],
        configs=["S0"],
        profile="literary_v1",
        allow_api=True,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_translate",
            confirm_token=preview["confirm_token"],
            job_id="preview_job",
            argv=mismatched,
        )
        assert False, "Expected mismatch"
    except RunControlError as exc:
        assert exc.code == "confirm_token_mismatch"


def test_route_real_snapshot_script_zero_api(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    out_path = tmp_path / "snapshot.json"
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    resp = client.post(
        "/api/thesis/runs",
        json={"script": "snapshot_runs", "db": str(db_path), "out": str(out_path)},
    )
    assert resp.status_code == 201
    run_id = resp.get_json()["data"]["run_id"]

    for _ in range(80):
        time.sleep(0.1)
        detail = client.get(f"/api/thesis/runs/{run_id}").get_json()["data"]
        if detail["status"] in {"done", "failed", "error"}:
            break
    assert detail["status"] == "done"
    assert detail["exit_code"] == 0
    assert out_path.exists()

    log = client.get(f"/api/thesis/runs/{run_id}/log?offset=0").get_json()["data"]
    assert "Snapshot written" in log["log"]


def test_route_allow_api_without_token_rejected(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    resp = client.post(
        "/api/thesis/runs",
        json={
            "script": "run_translate",
            "job_id": "preview_job",
            "db": str(db_path),
            "chapters": ["ch02"],
            "configs": ["S1"],
            "allow_api": True,
        },
    )
    assert resp.status_code == 403
    assert resp.get_json()["errors"][0]["code"] == "confirm_token_required"


def test_route_allow_api_without_job_id_rejected(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    resp = client.post(
        "/api/thesis/runs",
        json={
            "script": "run_translate",
            "db": str(db_path),
            "chapters": ["ch02"],
            "configs": ["S1"],
            "allow_api": True,
            "confirm_token": "anything",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["code"] == "job_id_required"


def test_runs_endpoint_separate_from_readmodels(tmp_path, monkeypatch):
    from tests.test_thesis_observability import create_observability_fixture
    from tests.test_thesis_scores import _create_d2l_fixture

    create_observability_fixture(tmp_path)
    _create_d2l_fixture(tmp_path / "reports")
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_REPORTS_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()

    runs_response = client.get("/api/thesis/runs")
    assert runs_response.status_code == 200
    runs_data = runs_response.get_json()["data"]
    assert isinstance(runs_data, list)
    assert "blocks" not in runs_response.get_json()
    assert "calls" not in runs_response.get_json()
    assert "headline" not in runs_response.get_json()
    assert "drift" not in runs_response.get_json()

    assert client.get("/api/thesis/datasets/fixture_job").status_code == 200
    assert client.get("/api/thesis/scores/d2l_p1").status_code == 200
    assert client.get("/api/thesis/observability/fixture_job").status_code == 200


def test_d2l_scope_warning_when_job_id_differs(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_d2l_fixture

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)
    assert "scope_warning" in data["meta"]
    assert "d2l_p1" in data["meta"]["scope_warning"]
    assert "d2l_p3" in data["meta"]["scope_warning"]


def test_d2l_no_scope_warning_when_matching(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_d2l_fixture

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p3", reports_root=tmp_path)
    assert "scope_warning" not in data["meta"]


def test_ti_scope_warning_when_job_id_differs(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_ti_fixture

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)
    assert "scope_warning" in data["meta"]


def test_ti_drift_has_status_source_and_target_term_kind(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_ti_fixture

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)
    assert data["drift"]
    for item in data["drift"]:
        assert item["status_source"] == "derived_from_coverage"
        assert item["target_term_kind"] == "entity_id"


def test_d2l_per_chapter_no_dead_d_branch(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_d2l_fixture

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)
    assert "B_S0" in data["per_chapter"]
    assert "B_S1" in data["per_chapter"]
    for key in data["per_chapter"]:
        assert key.startswith("B_")
