"""Routes for thesis run control (APP-C01)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Blueprint, request

from routes.common import error, ok
from services.thesis_runs import (
    RunControlError,
    RunRegistry,
    build_argv,
    build_resume_argv_from_entry,
    cancel_run,
    generate_estimate_preview,
    generate_prompt_preview,
    issue_estimate_token_for_argv,
    one_button_paths,
    read_events,
    read_log,
    resolve_job_db,
    spawn_run,
    validate_api_gate,
    validate_job_id,
    validate_run_id,
    validate_script,
)


bp = Blueprint("thesis_runs", __name__)

_registry: RunRegistry | None = None


def _get_registry() -> RunRegistry:
    global _registry
    if _registry is None:
        from config import THESIS_JOBS_ROOT

        _registry = RunRegistry(runs_root=THESIS_JOBS_ROOT)
    return _registry


def set_registry(registry: RunRegistry) -> None:
    """Allow tests to inject a registry."""
    global _registry
    _registry = registry


@bp.post("/thesis/runs")
def create_run():
    try:
        body = request.get_json(force=True) or {}
        script = validate_script(body.get("script", ""))
        allow_api = bool(body.get("allow_api", False))
        job_id = validate_job_id(body.get("job_id"))
        python_exe = _python_exe()
        tool_root = _tool_root()
        jobs_root = _jobs_root()
        registry = _get_registry()
        db = resolve_job_db(db=body.get("db"), job_id=job_id, jobs_root=jobs_root)
        planned_run_id = validate_run_id(body.get("planned_run_id"))
        extra_args = _body_list(body, "extra_args")
        event_log_path = None
        run_dir = None
        manifest_path = None
        workdb = body.get("workdb")
        if script == "run_translate" and allow_api and job_id:
            if planned_run_id is None:
                raise RunControlError(
                    "planned_run_id_required",
                    "run_translate allow_api=true requires planned_run_id from prompt-preview.",
                    403,
                )
            event_log_path = jobs_root / "run_events" / f"{planned_run_id}.jsonl"
        if script == "run_one_button":
            job_id = validate_job_id(job_id, required=True)
            if allow_api and planned_run_id is None:
                raise RunControlError(
                    "planned_run_id_required",
                    "run_one_button allow_api=true requires planned_run_id from estimate-preview.",
                    403,
                )
            if planned_run_id is None:
                planned_run_id = registry.new_run_id()
            paths = one_button_paths(jobs_root=jobs_root, job_id=job_id, run_id=planned_run_id)
            event_log_path = paths["event_log_path"]
            run_dir = paths["run_dir"]
            manifest_path = paths["manifest_path"]
            workdb = workdb or str(paths["workdb"])

        argv = build_argv(
            script=script,
            python_exe=python_exe,
            job_id=job_id,
            db=db,
            source=body.get("source"),
            doc_id=body.get("doc_id"),
            chapters=_body_list(body, "chapters"),
            configs=_body_list(body, "configs"),
            config=body.get("config"),
            config_file=body.get("config_file") or body.get("llm_config"),
            profile=body.get("profile"),
            experiment=body.get("experiment"),
            cache=body.get("cache"),
            report=body.get("report"),
            workdb=workdb,
            budget_cap_usd=_body_float(body, "budget_cap_usd"),
            with_s0=bool(body.get("with_s0", False)),
            out=body.get("out"),
            prepass=body.get("prepass"),
            mode=body.get("mode"),
            compare=body.get("compare"),
            human=body.get("human"),
            project=body.get("project"),
            chroma=body.get("chroma"),
            gold_variants=body.get("gold_variants"),
            context_budget=body.get("context_budget"),
            extra_args=extra_args,
            allow_api=allow_api,
            freeze=bool(body.get("freeze", False)),
            memory_report=body.get("memory_report"),
            smoke_query=body.get("smoke_query"),
            event_log=str(event_log_path) if event_log_path else None,
            run_id=planned_run_id,
        )

        consumed_token = validate_api_gate(
            allow_api=allow_api,
            script=script,
            confirm_token=body.get("confirm_token"),
            job_id=job_id,
            argv=argv,
        )

        entry = registry.create_run(
            script=script,
            argv=argv,
            cwd=str(tool_root),
            config=body.get("config"),
            configs=_body_list(body, "configs"),
            seed=body.get("seed"),
            model=body.get("model"),
            prompt_version=body.get("prompt_version"),
            cache_path=body.get("cache"),
            job_id=job_id,
            experiment=body.get("experiment"),
            allow_api=allow_api,
            prompt_preview_token=consumed_token,
            dry_run_policy=(
                "api_enabled_confirmed"
                if allow_api
                else "preflight_only_for_api_scripts_where_available"
            ),
            run_id=planned_run_id,
            event_log_path=str(event_log_path) if event_log_path else None,
            run_dir=str(run_dir) if run_dir else None,
            manifest_path=str(manifest_path) if manifest_path else None,
        )
        spawn_run(registry, entry["run_id"])
        return ok(
            {
                "run_id": entry["run_id"],
                "status": entry["status"],
                "run_dir": entry.get("run_dir"),
                "manifest_path": entry.get("manifest_path"),
            },
            status=201,
        )
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs")
def list_runs():
    return ok(_get_registry().list_runs())


@bp.get("/thesis/runs/prompt-preview")
def prompt_preview():
    try:
        preview = generate_prompt_preview(
            job_id=request.args.get("job_id", ""),
            script=request.args.get("script", "run_translate"),
            db=request.args.get("db"),
            chapters=_query_list("chapters"),
            configs=_query_list("configs"),
            config=request.args.get("config"),
            profile=request.args.get("profile"),
            experiment=request.args.get("experiment"),
            cache=request.args.get("cache"),
            report=request.args.get("report"),
            context_budget=_query_int("context_budget"),
            extra_args=_query_list("extra_args"),
            python_exe=_python_exe(),
            tool_root=_tool_root(),
            jobs_root=_jobs_root(),
        )
        return ok(preview)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/estimate-preview")
def estimate_preview():
    try:
        resume_run_id = validate_run_id(request.args.get("resume_run_id"))
        if resume_run_id:
            registry = _get_registry()
            entry = registry.get_run(resume_run_id)
            if entry is None:
                raise RunControlError("run_not_found", f"Run {resume_run_id} not found.", 404)
            argv = build_resume_argv_from_entry(entry)
            job_id = validate_job_id(entry.get("job_id"), required=True)
            token = issue_estimate_token_for_argv(
                job_id=job_id,
                script="run_one_button",
                argv=argv,
                preview_kind="resume_estimate_only",
            )
            return ok(
                {
                    "preview_kind": "resume_estimate_only",
                    "job_id": job_id,
                    "script": "run_one_button",
                    "resume_run_id": resume_run_id,
                    "confirm_token": token,
                    "confirm_token_ttl_seconds": 30 * 60,
                    "argv_preview": argv,
                    "estimate_argv_preview": [*argv, "--estimate-only"],
                    "estimate_by_stage": _manifest_estimates(entry),
                    "read_only": True,
                    "policy": {
                        "confirm_token_binding": "job_id + script + exact resume argv digest",
                        "resume_note": "Resume uses the original argv, removes --estimate-only, and appends --resume.",
                    },
                }
            )
        preview = generate_estimate_preview(
            job_id=request.args.get("job_id", ""),
            script=request.args.get("script", ""),
            db=request.args.get("db"),
            chapters=_query_list("chapters"),
            configs=_query_list("configs"),
            config=request.args.get("config"),
            profile=request.args.get("profile"),
            experiment=request.args.get("experiment"),
            cache=request.args.get("cache"),
            report=request.args.get("report"),
            context_budget=_query_int("context_budget"),
            extra_args=_query_list("extra_args"),
            planned_run_id=request.args.get("planned_run_id"),
            workdb=request.args.get("workdb"),
            budget_cap_usd=_query_float("budget_cap_usd"),
            with_s0=_query_bool("with_s0"),
            python_exe=_python_exe(),
            tool_root=_tool_root(),
            jobs_root=_jobs_root(),
        )
        return ok(preview)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>")
def run_detail(run_id: str):
    entry = _get_registry().get_run(run_id)
    if entry is None:
        return error("run_not_found", f"Run {run_id} not found.", 404)
    return ok(entry)


@bp.get("/thesis/runs/<run_id>/log")
def run_log(run_id: str):
    try:
        offset = int(request.args.get("offset", "0"))
        return ok(read_log(_get_registry(), run_id, offset=offset))
    except ValueError:
        return error("invalid_offset", "offset must be an integer.", 400)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/events")
def run_events(run_id: str):
    try:
        offset = int(request.args.get("offset", "0"))
        max_bytes = int(request.args.get("max_bytes", str(256 * 1024)))
        return ok(
            read_events(
                _get_registry(),
                run_id,
                offset=offset,
                max_bytes=max_bytes,
                jobs_root=_jobs_root(),
            )
        )
    except ValueError:
        return error("invalid_offset", "offset and max_bytes must be integers.", 400)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.post("/thesis/runs/<run_id>/cancel")
def cancel_thesis_run(run_id: str):
    try:
        entry = cancel_run(_get_registry(), run_id)
        return ok({"run_id": entry["run_id"], "status": entry["status"]})
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.post("/thesis/runs/<run_id>/pause")
def pause_thesis_run(run_id: str):
    try:
        pause_path = _pause_file_for_run(run_id)
        pause_path.parent.mkdir(parents=True, exist_ok=True)
        pause_path.write_text("paused_by_user\n", encoding="utf-8", newline="\n")
        return ok({"run_id": run_id, "paused": True, "pause_file": str(pause_path)})
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.delete("/thesis/runs/<run_id>/pause")
def unpause_thesis_run(run_id: str):
    try:
        pause_path = _pause_file_for_run(run_id)
        if pause_path.exists():
            pause_path.unlink()
        return ok({"run_id": run_id, "paused": False, "pause_file": str(pause_path)})
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/manifest")
def run_manifest(run_id: str):
    try:
        manifest_path = _manifest_path_for_run(run_id)
        if not manifest_path.exists():
            raise RunControlError("manifest_not_found", f"Manifest not found for run {run_id}.", 404)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return ok(
            {
                "run_id": run_id,
                "manifest_path": str(manifest_path),
                "status": manifest.get("status"),
                "attempt": manifest.get("attempt"),
                "stages": manifest.get("stages") or [],
                "estimate_by_stage": manifest.get("estimate_by_stage") or [],
                "paused_at_stage_boundary_before": manifest.get("paused_at_stage_boundary_before"),
                "error": manifest.get("error"),
                "manifest": manifest,
            }
        )
    except json.JSONDecodeError as exc:
        return error("manifest_invalid_json", f"Manifest is not valid JSON: {exc}", 500)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.post("/thesis/runs/<run_id>/resume")
def resume_thesis_run(run_id: str):
    try:
        body = request.get_json(force=True) or {}
        registry = _get_registry()
        entry = registry.get_run(run_id)
        if entry is None:
            raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
        manifest_path = _manifest_path_for_entry(entry)
        manifest = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        argv = build_resume_argv_from_entry(entry)
        job_id = validate_job_id(entry.get("job_id"), required=True)
        consumed_token = validate_api_gate(
            allow_api=True,
            script="run_one_button",
            confirm_token=body.get("confirm_token"),
            job_id=job_id,
            argv=argv,
        )
        pause_path = _pause_file_from_entry(entry)
        if pause_path.exists():
            pause_path.unlink()
        new_run_id = registry.new_run_id()
        attempt_index = int(manifest.get("attempt", entry.get("attempt_index") or 0) or 0) + 1
        new_log_path = registry.runs_root / "run_logs" / f"{new_run_id}.log"
        new_entry = registry.create_run(
            script="run_one_button",
            argv=argv,
            cwd=entry.get("cwd"),
            job_id=job_id,
            experiment=entry.get("experiment"),
            allow_api=True,
            prompt_preview_token=consumed_token,
            dry_run_policy="api_enabled_confirmed_resume",
            event_log_path=entry.get("event_log_path"),
            run_dir=entry.get("run_dir"),
            manifest_path=entry.get("manifest_path"),
            run_id=new_run_id,
            attempt_index=attempt_index,
            resumed_from=run_id,
            attempt_log_path=str(new_log_path),
        )
        spawn_run(registry, new_entry["run_id"])
        return ok(
            {
                "run_id": new_entry["run_id"],
                "resumed_from": run_id,
                "status": new_entry["status"],
                "attempt_index": attempt_index,
                "manifest_path": new_entry.get("manifest_path"),
            },
            status=201,
        )
    except json.JSONDecodeError as exc:
        return error("manifest_invalid_json", f"Manifest is not valid JSON: {exc}", 500)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


def _body_list(body: dict, key: str) -> list[str]:
    value = body.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [item for part in value.split(",") for item in part.split() if item]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _query_list(key: str) -> list[str]:
    values = request.args.getlist(key)
    result: list[str] = []
    for value in values:
        result.extend(item for part in str(value).split(",") for item in part.split() if item)
    return result


def _query_int(key: str) -> int | None:
    value = request.args.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RunControlError("invalid_int", f"{key} must be an integer.", 400) from exc


def _query_float(key: str) -> float | None:
    value = request.args.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise RunControlError("invalid_float", f"{key} must be a number.", 400) from exc


def _body_float(body: dict, key: str) -> float | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RunControlError("invalid_float", f"{key} must be a number.", 400) from exc


def _query_bool(key: str) -> bool:
    value = request.args.get(key)
    if value in (None, ""):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _manifest_estimates(entry: dict) -> list[dict]:
    try:
        path = _manifest_path_for_entry(entry)
    except RunControlError:
        return []
    if not path.exists():
        return []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return manifest.get("estimate_by_stage") or []


def _manifest_path_for_run(run_id: str) -> Path:
    entry = _get_registry().get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
    return _manifest_path_for_entry(entry)


def _manifest_path_for_entry(entry: dict) -> Path:
    raw_path = entry.get("manifest_path")
    if not raw_path:
        raise RunControlError("run_manifest_unavailable", "Run does not have a manifest_path.", 404)
    return _path_under_jobs(raw_path, "manifest_path")


def _pause_file_for_run(run_id: str) -> Path:
    entry = _get_registry().get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
    return _pause_file_from_entry(entry)


def _pause_file_from_entry(entry: dict) -> Path:
    raw_dir = entry.get("run_dir")
    if not raw_dir:
        raise RunControlError("run_pause_unavailable", "Run does not have a run_dir.", 404)
    return _path_under_jobs(raw_dir, "run_dir") / "PAUSE"


def _path_under_jobs(raw_path: str | Path, field: str) -> Path:
    root = _jobs_root()
    path = Path(str(raw_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RunControlError(
            f"invalid_{field}",
            f"{field} is outside THESIS_JOBS_ROOT.",
            500,
        ) from exc
    return path


def _tool_root() -> Path:
    from config import THESIS_TOOL_ROOT

    return Path(THESIS_TOOL_ROOT).resolve()


def _jobs_root() -> Path:
    from config import THESIS_JOBS_ROOT

    return Path(THESIS_JOBS_ROOT).resolve()


def _python_exe() -> str:
    from config import THESIS_PYTHON_EXE

    return THESIS_PYTHON_EXE or sys.executable
