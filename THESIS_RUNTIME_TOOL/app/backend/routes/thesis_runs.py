"""Routes for thesis run control (APP-C01)."""
from __future__ import annotations

import sys
from pathlib import Path

from flask import Blueprint, request

from routes.common import error, ok
from services.thesis_runs import (
    RunControlError,
    RunRegistry,
    build_argv,
    generate_prompt_preview,
    read_log,
    resolve_job_db,
    spawn_run,
    validate_api_gate,
    validate_job_id,
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
        db = resolve_job_db(db=body.get("db"), job_id=job_id, jobs_root=jobs_root)

        argv = build_argv(
            script=script,
            python_exe=python_exe,
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
            out=body.get("out"),
            prepass=body.get("prepass"),
            mode=body.get("mode"),
            compare=body.get("compare"),
            human=body.get("human"),
            project=body.get("project"),
            chroma=body.get("chroma"),
            gold_variants=body.get("gold_variants"),
            context_budget=body.get("context_budget"),
            extra_args=_body_list(body, "extra_args"),
            allow_api=allow_api,
            freeze=bool(body.get("freeze", False)),
            memory_report=body.get("memory_report"),
            smoke_query=body.get("smoke_query"),
        )

        consumed_token = validate_api_gate(
            allow_api=allow_api,
            script=script,
            confirm_token=body.get("confirm_token"),
            job_id=job_id,
            argv=argv,
        )

        registry = _get_registry()
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
        )
        spawn_run(registry, entry["run_id"])
        return ok({"run_id": entry["run_id"], "status": entry["status"]}, status=201)
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


def _tool_root() -> Path:
    from config import THESIS_TOOL_ROOT

    return Path(THESIS_TOOL_ROOT).resolve()


def _jobs_root() -> Path:
    from config import THESIS_JOBS_ROOT

    return Path(THESIS_JOBS_ROOT).resolve()


def _python_exe() -> str:
    from config import THESIS_PYTHON_EXE

    return THESIS_PYTHON_EXE or sys.executable
