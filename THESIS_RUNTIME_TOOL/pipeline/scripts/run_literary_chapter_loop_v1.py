"""Generic resumable runner for the current Literary chapter loop."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ChapterCycleOrchestratorError,
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    load_chapter_cycle_state_v1,
    resume_chapter_cycle_run_v1,
    run_chapter_cycle_until_boundary_v1,
)
from pipeline.literary.chapter_loop_bindings_v1 import (
    ChapterLoopRuntimeBindingsV1,
    RuntimeStageBindingV1,
    load_runtime_bindings_v1,
    load_stage_bindings_v1,
)
from pipeline.literary.chapter_loop_current_executor_v1 import (
    LiteraryChapterLoopExecutorV1,
    write_chapter_bridge_files_v1,
)
from pipeline.literary.chapter_loop_observability_v1 import (
    LiteraryChapterLoopHistoryV1,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.project_source_bridge_v1 import (
    validate_literary_project_binding_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DEFAULT_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_chapter_loop_profile_v1.json"
)
DEFAULT_STAGE_BINDINGS = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_chapter_loop_stage_bindings_v1.json"
)
SESSION_SCHEMA = "literary_chapter_loop_session_v2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a project-neutral Literary chapter loop with receipts, "
            "checkpoint/resume, and Console history."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--run-root", type=Path, required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--document", type=Path, required=True)
    init.add_argument("--frozen-db", type=Path, required=True)
    init.add_argument("--runtime-bindings", type=Path, required=True)
    init.add_argument("--project-binding", type=Path)
    init.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    init.add_argument("--stage-bindings", type=Path, default=DEFAULT_STAGE_BINDINGS)
    init.add_argument("--stop-after-chapter-count", type=int)
    _add_selection(init)

    dry = commands.add_parser("dry-run")
    dry.add_argument("--run-root", type=Path, required=True)
    dry.add_argument(
        "--runtime-profile-override",
        action="append",
        default=[],
        metavar="STAGE=PATH",
    )
    _add_capacity_overrides(dry)

    run = commands.add_parser("run")
    _add_execution(run)

    resume = commands.add_parser("resume")
    _add_execution(resume)
    resume.add_argument("--stop-after-chapter-count", type=int)

    bind_roots = commands.add_parser("bind-effective-roots")
    bind_roots.add_argument("--run-root", type=Path, required=True)
    bind_roots.add_argument(
        "--stage-root",
        action="append",
        required=True,
        metavar="STAGE_ID=PATH",
    )
    bind_roots.add_argument("--replace-existing-stage", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--run-root", type=Path, required=True)
    status.add_argument("--show-plan", action="store_true")
    return parser


def _add_selection(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chapter-id", action="append")
    group.add_argument("--chapter-range", help="inclusive START_ID:END_ID")
    group.add_argument("--all-chapters", action="store_true")


def _add_execution(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--scheduler-root", type=Path, required=True)
    parser.add_argument(
        "--runtime-profile-override",
        action="append",
        default=[],
        metavar="STAGE=PATH",
    )
    parser.add_argument(
        "--capability-override",
        action="append",
        default=[],
        metavar="STAGE.KEY=PATH",
    )
    _add_capacity_overrides(parser)


def _add_capacity_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stage-call-cap-override",
        action="append",
        default=[],
        metavar="ROLE=COUNT",
        help=(
            "Raise a per-chapter logical call cap without changing request "
            "or batch size."
        ),
    )
    parser.add_argument(
        "--context-profile-override",
        action="append",
        default=[],
        metavar="STAGE=PATH",
        help=(
            "Raise an aggregate context-request ceiling without changing "
            "per-request packing."
        ),
    )
    parser.add_argument("--max-api-calls-per-chapter", type=int)
    parser.add_argument("--max-api-calls-per-run", type=int)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        _print(_initialize(args))
        return 0
    if args.command == "dry-run":
        context = _load_context(
            args.run_root,
            runtime_profile_overrides=args.runtime_profile_override,
            context_profile_overrides=args.context_profile_override,
            stage_call_cap_overrides=args.stage_call_cap_override,
            max_api_calls_per_chapter=args.max_api_calls_per_chapter,
            max_api_calls_per_run=args.max_api_calls_per_run,
            for_live=False,
        )
        report = context["executor"].dry_run_plan()
        target = Path(args.run_root).resolve() / "dry_run_plan.json"
        _write_atomic(target, report)
        _print(report)
        return 0
    if args.command == "bind-effective-roots":
        context = _load_context(args.run_root, for_live=False)
        roots = _parse_effective_stage_roots(args.stage_root)
        manifest = context["executor"].bind_effective_stage_roots(
            roots,
            replace_existing=args.replace_existing_stage,
        )
        _print(
            {
                "status": "bound",
                "manifest_path": str(
                    Path(args.run_root).resolve()
                    / "corrections"
                    / "effective_stage_roots.json"
                ),
                "manifest_hash": manifest["manifest_hash"],
                "override_count": len(manifest["overrides"]),
                "provider_calls": 0,
            }
        )
        return 0
    if args.command == "status":
        state = load_chapter_cycle_state_v1(args.run_root)
        payload: dict[str, Any] = {"state": state}
        if args.show_plan:
            payload["plan"] = load_chapter_cycle_plan_v1(args.run_root)
        _print(payload)
        return 0
    if args.command in {"run", "resume"}:
        context = _load_context(
            args.run_root,
            credential_file=args.credential_file,
            scheduler_root=args.scheduler_root,
            capability_overrides=args.capability_override,
            runtime_profile_overrides=args.runtime_profile_override,
            context_profile_overrides=args.context_profile_override,
            stage_call_cap_overrides=args.stage_call_cap_override,
            max_api_calls_per_chapter=args.max_api_calls_per_chapter,
            max_api_calls_per_run=args.max_api_calls_per_run,
            for_live=True,
        )
        history: LiteraryChapterLoopHistoryV1 = context["history"]
        state = load_chapter_cycle_state_v1(args.run_root)
        if args.command == "resume":
            if state["status"] == "complete":
                _print(_run_summary(args.run_root))
                return 0
            if state["status"] != "running":
                state = resume_chapter_cycle_run_v1(
                    run_root=args.run_root,
                    stop_after_chapter_count=args.stop_after_chapter_count,
                )
                history.emit(
                    "component_resumed",
                    stage="__component__",
                    agent="system",
                    script=Path(__file__).name,
                    payload={
                        "next_stage": state["current_stage"],
                        "stop_after_chapter_count": state[
                            "stop_after_chapter_count"
                        ],
                    },
                )
        elif state["status"] != "running":
            raise SystemExit(
                "run is not active; use resume to extend or continue a stopped run"
            )
        try:
            state = run_chapter_cycle_until_boundary_v1(
                run_root=args.run_root,
                executor=context["executor"],
                permit_plan=context["effective_plan"],
            )
        except Exception as exc:
            history.set_status("paused", reason=f"{type(exc).__name__}:{exc}")
            history.emit(
                "component_halted",
                stage="__component__",
                agent="system",
                script=Path(__file__).name,
                severity="error",
                payload={"failure_type": type(exc).__name__, "reason": str(exc)},
            )
            raise
        if state["status"] == "complete":
            history.set_status("succeeded")
            history.emit(
                "component_done",
                stage="__component__",
                agent="system",
                script=Path(__file__).name,
                payload={"completed_chapter_ids": state["completed_chapter_ids"]},
            )
        elif state["status"] == "stopped":
            history.set_status("paused", reason="stop_after_chapter_boundary")
            history.emit(
                "checkpoint",
                stage="__component__",
                agent="system",
                script=Path(__file__).name,
                payload={
                    "completed_chapter_ids": state["completed_chapter_ids"],
                    "resume_available": True,
                },
            )
        elif state["status"] == "paused":
            history.set_status("paused", reason=str(state["halt_reason"]))
            history.emit(
                "component_halted",
                stage=str(state["current_stage"]),
                agent="system",
                script=Path(__file__).name,
                severity="warning",
                payload={
                    "failure_class": state["halt_failure_class"],
                    "reason": state["halt_reason"],
                    "resume_available": True,
                },
            )
        _print(_run_summary(args.run_root))
        return 0 if state["status"] in {"complete", "stopped"} else 2
    raise SystemExit("unknown command")


def _initialize(args: argparse.Namespace) -> dict[str, Any]:
    document_path = Path(args.document).resolve()
    document = load_literary_source_document_v1(document_path)
    selected = _selected_chapters(
        document=document,
        chapter_ids=args.chapter_id,
        chapter_range=args.chapter_range,
        all_chapters=args.all_chapters,
    )
    stage_binding_path = Path(args.stage_bindings).resolve()
    runtime_binding_path = Path(args.runtime_bindings).resolve()
    stage_bindings = load_stage_bindings_v1(stage_binding_path)
    runtime_bindings = load_runtime_bindings_v1(runtime_binding_path)
    _ = stage_bindings
    root = Path(args.run_root).resolve()
    state = initialize_chapter_cycle_run_v1(
        run_root=root,
        document_path=document_path,
        profile_path=Path(args.profile).resolve(),
        frozen_db_path=Path(args.frozen_db).resolve(),
        ordered_chapter_ids=selected,
        stop_after_chapter_count=args.stop_after_chapter_count,
    )
    plan = load_chapter_cycle_plan_v1(root)
    code_revision = _clean_head()
    project_binding: dict[str, Any] | None = None
    project_binding_path: Path | None = None
    if args.project_binding is not None:
        source_binding = validate_literary_project_binding_v1(
            _read_object(Path(args.project_binding).resolve())
        )
        project_binding_path = root / "project" / "literary_project_binding.json"
        _write_atomic(project_binding_path, source_binding)
        project_binding = validate_literary_project_binding_v1(
            _read_object(project_binding_path)
        )
    write_chapter_bridge_files_v1(
        run_root=root,
        document=document,
        ordered_chapter_ids=selected,
    )
    body = {
        "schema_version": SESSION_SCHEMA,
        "run_id": _required_id(args.run_id, "run_id"),
        "plan_hash": plan["plan_hash"],
        "code_revision": code_revision,
        "active_code_revision": code_revision,
        "code_revision_history": [code_revision],
        "stage_binding_path": str(stage_binding_path),
        "stage_binding_sha256": file_sha256(stage_binding_path),
        "runtime_binding_path": str(runtime_binding_path),
        "runtime_binding_sha256": file_sha256(runtime_binding_path),
        "runtime_binding_hash": runtime_bindings.binding_hash,
        "project_binding_path": (
            str(project_binding_path) if project_binding_path is not None else None
        ),
        "project_binding_sha256": (
            file_sha256(project_binding_path)
            if project_binding_path is not None
            else None
        ),
        "project_binding_hash": (
            project_binding["binding_hash"] if project_binding is not None else None
        ),
        "selected_chapter_ids": selected,
        "production_publish_performed": False,
    }
    session = {**body, "session_hash": canonical_hash(body)}
    _write_atomic(root / "chapter_loop_session.json", session)
    history = LiteraryChapterLoopHistoryV1(run_root=root, run_id=session["run_id"])
    history.initialize(
        plan_hash=plan["plan_hash"],
        selected_chapter_ids=selected,
        code_revision=code_revision,
        binding_hash=file_sha256(stage_binding_path),
        runtime_binding_hash=runtime_bindings.binding_hash,
        project_binding=project_binding,
        project_binding_ref=(
            project_binding_path.relative_to(root).as_posix()
            if project_binding_path is not None
            else None
        ),
    )
    return {
        "status": "initialized",
        "run_id": session["run_id"],
        "run_root": str(root),
        "chapter_count": len(selected),
        "stop_after_chapter_count": state["stop_after_chapter_count"],
        "next_stage": state["current_stage"],
        "provider_calls": 0,
    }


def _load_context(
    run_root: Path,
    *,
    credential_file: Path | None = None,
    scheduler_root: Path | None = None,
    capability_overrides: Sequence[str] = (),
    runtime_profile_overrides: Sequence[str] = (),
    context_profile_overrides: Sequence[str] = (),
    stage_call_cap_overrides: Sequence[str] = (),
    max_api_calls_per_chapter: int | None = None,
    max_api_calls_per_run: int | None = None,
    for_live: bool,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    session = _read_object(root / "chapter_loop_session.json")
    body = dict(session)
    observed_hash = body.pop("session_hash", None)
    if session.get("schema_version") != SESSION_SCHEMA or canonical_hash(body) != observed_hash:
        raise SystemExit("chapter-loop session seal is invalid")
    plan = load_chapter_cycle_plan_v1(root)
    if session["plan_hash"] != plan["plan_hash"]:
        raise SystemExit("chapter-loop session belongs to another plan")
    persisted_capacity_overrides = _validated_capacity_overrides(
        session.get("capacity_overrides"),
        plan=plan,
    )
    requested_capacity_overrides = _requested_capacity_overrides(
        stage_call_cap_overrides,
        max_api_calls_per_chapter=max_api_calls_per_chapter,
        max_api_calls_per_run=max_api_calls_per_run,
        plan=plan,
        current=persisted_capacity_overrides,
    )
    if requested_capacity_overrides is not None:
        persisted_capacity_overrides = requested_capacity_overrides
        session_body = dict(session)
        session_body.pop("session_hash", None)
        session_body["capacity_overrides"] = persisted_capacity_overrides
        session = {
            **session_body,
            "session_hash": canonical_hash(session_body),
        }
        _write_atomic(root / "chapter_loop_session.json", session)
    effective_plan = _plan_with_capacity_overrides(
        plan,
        persisted_capacity_overrides,
    )
    if file_sha256(Path(session["stage_binding_path"])) != session["stage_binding_sha256"]:
        raise SystemExit("stage binding table changed")
    if file_sha256(Path(session["runtime_binding_path"])) != session["runtime_binding_sha256"]:
        raise SystemExit("runtime binding file changed")
    project_binding: dict[str, Any] | None = None
    project_binding_path = session.get("project_binding_path")
    if project_binding_path is not None:
        path = Path(str(project_binding_path)).resolve()
        if (
            not path.is_file()
            or file_sha256(path) != session.get("project_binding_sha256")
        ):
            raise SystemExit("project binding file changed")
        project_binding = validate_literary_project_binding_v1(
            _read_object(path)
        )
        if project_binding["binding_hash"] != session.get(
            "project_binding_hash"
        ):
            raise SystemExit("project binding identity changed")
    revision_history = _session_revision_history(session)
    if for_live:
        _require_clean_tracked_worktree()
        current_head = _head()
        if current_head != revision_history[-1]:
            revision_history = [*revision_history, current_head]
            session_body = dict(session)
            session_body.pop("session_hash", None)
            session_body["active_code_revision"] = current_head
            session_body["code_revision_history"] = revision_history
            session = {
                **session_body,
                "session_hash": canonical_hash(session_body),
            }
            _write_atomic(root / "chapter_loop_session.json", session)
    stage_bindings = load_stage_bindings_v1(Path(session["stage_binding_path"]))
    runtime_bindings = load_runtime_bindings_v1(Path(session["runtime_binding_path"]))
    if runtime_bindings.binding_hash != session["runtime_binding_hash"]:
        raise SystemExit("runtime capability/profile binding changed")
    persisted_overrides = _validated_capability_overrides(
        session.get("runtime_capability_overrides"),
        runtime_bindings=runtime_bindings,
    )
    requested_overrides = _parse_capability_overrides(
        capability_overrides,
        runtime_bindings=runtime_bindings,
    )
    if requested_overrides:
        persisted_overrides.update(requested_overrides)
        session_body = dict(session)
        session_body.pop("session_hash", None)
        session_body["runtime_capability_overrides"] = persisted_overrides
        session = {
            **session_body,
            "session_hash": canonical_hash(session_body),
        }
        _write_atomic(root / "chapter_loop_session.json", session)
    runtime_bindings = _runtime_bindings_with_capability_overrides(
        runtime_bindings,
        persisted_overrides,
    )
    requested_context_overrides = _parse_context_profile_overrides(
        context_profile_overrides,
        runtime_bindings=runtime_bindings,
    )
    raw_context_overrides = session.get("context_profile_overrides")
    if raw_context_overrides is None:
        raw_context_overrides = {}
    if not isinstance(raw_context_overrides, Mapping):
        raise SystemExit("context profile overrides are malformed")
    merged_context_overrides = dict(raw_context_overrides)
    merged_context_overrides.update(requested_context_overrides)
    persisted_context_overrides = _validated_context_profile_overrides(
        merged_context_overrides,
        runtime_bindings=runtime_bindings,
    )
    if requested_context_overrides:
        session_body = dict(session)
        session_body.pop("session_hash", None)
        session_body["context_profile_overrides"] = persisted_context_overrides
        session = {
            **session_body,
            "session_hash": canonical_hash(session_body),
        }
        _write_atomic(root / "chapter_loop_session.json", session)
    runtime_bindings = _runtime_bindings_with_context_profile_overrides(
        runtime_bindings,
        persisted_context_overrides,
    )
    requested_profile_overrides = _parse_runtime_profile_overrides(
        runtime_profile_overrides,
        runtime_bindings=runtime_bindings,
    )
    raw_profile_overrides = session.get("runtime_profile_overrides")
    if raw_profile_overrides is None:
        raw_profile_overrides = {}
    if not isinstance(raw_profile_overrides, Mapping):
        raise SystemExit("runtime profile overrides are malformed")
    merged_profile_overrides = dict(raw_profile_overrides)
    merged_profile_overrides.update(requested_profile_overrides)
    persisted_profile_overrides = _validated_runtime_profile_overrides(
        merged_profile_overrides,
        runtime_bindings=runtime_bindings,
    )
    if requested_profile_overrides:
        session_body = dict(session)
        session_body.pop("session_hash", None)
        session_body["runtime_profile_overrides"] = persisted_profile_overrides
        session = {
            **session_body,
            "session_hash": canonical_hash(session_body),
        }
        _write_atomic(root / "chapter_loop_session.json", session)
    runtime_bindings = _runtime_bindings_with_profile_overrides(
        runtime_bindings,
        persisted_profile_overrides,
    )
    history = LiteraryChapterLoopHistoryV1(run_root=root, run_id=session["run_id"])
    history.initialize(
        plan_hash=plan["plan_hash"],
        selected_chapter_ids=plan["ordered_chapter_ids"],
        code_revision=session["code_revision"],
        binding_hash=session["stage_binding_sha256"],
        runtime_binding_hash=session["runtime_binding_hash"],
        project_binding=project_binding,
        project_binding_ref=(
            Path(str(project_binding_path)).resolve().relative_to(root).as_posix()
            if project_binding_path is not None
            else None
        ),
    )
    history.synchronize_code_revisions(revision_history)
    if requested_capacity_overrides is not None:
        history.emit(
            "capacity_override_updated",
            stage="__component__",
            agent="system",
            script=Path(__file__).name,
            payload={
                "logical_call_caps_by_role": effective_plan[
                    "logical_call_caps_by_role"
                ],
                "max_api_calls_per_chapter": effective_plan[
                    "max_api_calls_per_chapter"
                ],
                "max_api_calls_per_run": effective_plan[
                    "max_api_calls_per_run"
                ],
                "context_profile_overrides": persisted_context_overrides,
            },
        )
    return {
        "session": session,
        "plan": plan,
        "effective_plan": effective_plan,
        "capacity_overrides": persisted_capacity_overrides,
        "context_profile_overrides": persisted_context_overrides,
        "history": history,
        "executor": LiteraryChapterLoopExecutorV1(
            run_root=root,
            plan=effective_plan,
            stage_bindings=stage_bindings,
            runtime_bindings=runtime_bindings,
            credential_file=credential_file,
            scheduler_root=scheduler_root,
            history=history,
        ),
    }


def _parse_capability_overrides(
    values: Sequence[str],
    *,
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise SystemExit(
                "capability override must use STAGE.KEY=PATH"
            )
        selector, raw_path = raw.split("=", 1)
        if "." not in selector:
            raise SystemExit(
                "capability override must use STAGE.KEY=PATH"
            )
        stage_name, capability_key = selector.split(".", 1)
        stage = runtime_bindings.stages.get(stage_name)
        if stage is None or capability_key not in stage.capabilities:
            raise SystemExit(f"unknown capability override: {selector}")
        path = Path(raw_path).resolve()
        evidence = path / "capability_evidence.json"
        if not path.is_dir() or not evidence.is_file():
            raise SystemExit(
                f"capability override evidence is absent: {selector}"
            )
        result[selector] = {
            "path": str(path),
            "capability_evidence_sha256": file_sha256(evidence),
        }
    return result


def _parse_effective_stage_roots(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        stage_id, separator, path_text = raw.partition("=")
        if not separator or not stage_id or not path_text:
            raise SystemExit(
                "effective stage root must use STAGE_ID=PATH"
            )
        if stage_id in result:
            raise SystemExit(f"effective stage root repeats {stage_id}")
        result[stage_id] = Path(path_text).resolve()
    return result


def _base_capacity_overrides(plan: Mapping[str, Any]) -> dict[str, Any]:
    roles = plan.get("logical_call_caps_by_role")
    if not isinstance(roles, Mapping) or not roles:
        raise SystemExit("chapter-loop plan has no logical call caps")
    normalized_roles: dict[str, int] = {}
    for role, value in roles.items():
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            raise SystemExit("chapter-loop plan logical call caps are malformed")
        normalized_roles[role] = int(value)
    chapter_cap = plan.get("max_api_calls_per_chapter")
    run_cap = plan.get("max_api_calls_per_run")
    if (
        not isinstance(chapter_cap, int)
        or isinstance(chapter_cap, bool)
        or chapter_cap < 1
        or not isinstance(run_cap, int)
        or isinstance(run_cap, bool)
        or run_cap < chapter_cap
    ):
        raise SystemExit("chapter-loop plan global call caps are malformed")
    return {
        "schema_version": "literary_chapter_loop_capacity_overrides_v1",
        "logical_call_caps_by_role": normalized_roles,
        "max_api_calls_per_chapter": int(chapter_cap),
        "max_api_calls_per_run": int(run_cap),
    }


def _validated_capacity_overrides(
    value: Any,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    base = _base_capacity_overrides(plan)
    if value is None:
        return base
    if not isinstance(value, Mapping):
        raise SystemExit("capacity overrides are malformed")
    expected_keys = set(base)
    if set(value) != expected_keys or value.get("schema_version") != base[
        "schema_version"
    ]:
        raise SystemExit("capacity override schema is malformed")
    roles = value.get("logical_call_caps_by_role")
    if not isinstance(roles, Mapping) or set(roles) != set(
        base["logical_call_caps_by_role"]
    ):
        raise SystemExit("capacity override role set is malformed")
    for role, original in base["logical_call_caps_by_role"].items():
        current = roles.get(role)
        if (
            not isinstance(current, int)
            or isinstance(current, bool)
            or current < original
        ):
            raise SystemExit(
                f"capacity override must only raise role cap: {role}"
            )
    for key in ("max_api_calls_per_chapter", "max_api_calls_per_run"):
        current = value.get(key)
        if (
            not isinstance(current, int)
            or isinstance(current, bool)
            or current < base[key]
        ):
            raise SystemExit(
                f"capacity override must only raise global cap: {key}"
            )
    if value["max_api_calls_per_run"] < value["max_api_calls_per_chapter"]:
        raise SystemExit("run capacity cap must cover the chapter capacity cap")
    return {
        "schema_version": base["schema_version"],
        "logical_call_caps_by_role": {
            str(role): int(value)
            for role, value in roles.items()
        },
        "max_api_calls_per_chapter": int(value["max_api_calls_per_chapter"]),
        "max_api_calls_per_run": int(value["max_api_calls_per_run"]),
    }


def _requested_capacity_overrides(
    values: Sequence[str],
    *,
    max_api_calls_per_chapter: int | None,
    max_api_calls_per_run: int | None,
    plan: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any] | None:
    if (
        not values
        and max_api_calls_per_chapter is None
        and max_api_calls_per_run is None
    ):
        return None
    result = _validated_capacity_overrides(current, plan=plan)
    roles = dict(result["logical_call_caps_by_role"])
    base_roles = plan["logical_call_caps_by_role"]
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise SystemExit(
                "stage call cap override must use ROLE=COUNT"
            )
        role, raw_count = raw.split("=", 1)
        if role not in base_roles:
            raise SystemExit(f"unknown stage call cap role: {role}")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"stage call cap is not an integer: {role}") from exc
        if count < int(roles[role]):
            raise SystemExit(
                f"stage call cap can only increase: {role} "
                f"(current={roles[role]}, requested={count})"
            )
        roles[role] = count
    if max_api_calls_per_chapter is not None:
        if (
            not isinstance(max_api_calls_per_chapter, int)
            or isinstance(max_api_calls_per_chapter, bool)
            or max_api_calls_per_chapter < result["max_api_calls_per_chapter"]
        ):
            raise SystemExit("chapter API call cap can only increase")
        result["max_api_calls_per_chapter"] = int(max_api_calls_per_chapter)
    if max_api_calls_per_run is not None:
        if (
            not isinstance(max_api_calls_per_run, int)
            or isinstance(max_api_calls_per_run, bool)
            or max_api_calls_per_run < result["max_api_calls_per_run"]
        ):
            raise SystemExit("run API call cap can only increase")
        result["max_api_calls_per_run"] = int(max_api_calls_per_run)
    if result["max_api_calls_per_run"] < result["max_api_calls_per_chapter"]:
        raise SystemExit("run API call cap must cover chapter API call cap")
    result["logical_call_caps_by_role"] = roles
    return _validated_capacity_overrides(result, plan=plan)


def _plan_with_capacity_overrides(
    plan: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(plan)
    result["logical_call_caps_by_role"] = dict(
        overrides["logical_call_caps_by_role"]
    )
    result["max_api_calls_per_chapter"] = int(
        overrides["max_api_calls_per_chapter"]
    )
    result["max_api_calls_per_run"] = int(overrides["max_api_calls_per_run"])
    return result


def _validated_capability_overrides(
    value: Any,
    *,
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SystemExit("runtime capability overrides are malformed")
    result: dict[str, dict[str, str]] = {}
    for selector, raw in value.items():
        if not isinstance(selector, str) or "." not in selector:
            raise SystemExit("runtime capability override selector is malformed")
        stage_name, capability_key = selector.split(".", 1)
        stage = runtime_bindings.stages.get(stage_name)
        if stage is None or capability_key not in stage.capabilities:
            raise SystemExit(f"unknown persisted capability override: {selector}")
        if not isinstance(raw, Mapping) or set(raw) != {
            "path",
            "capability_evidence_sha256",
        }:
            raise SystemExit("runtime capability override row is malformed")
        path = Path(str(raw["path"])).resolve()
        evidence = path / "capability_evidence.json"
        if (
            not evidence.is_file()
            or file_sha256(evidence)
            != raw["capability_evidence_sha256"]
        ):
            raise SystemExit(f"runtime capability override drifted: {selector}")
        result[selector] = {
            "path": str(path),
            "capability_evidence_sha256": str(
                raw["capability_evidence_sha256"]
            ),
        }
    return result


def _runtime_bindings_with_capability_overrides(
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
    overrides: Mapping[str, Mapping[str, str]],
) -> ChapterLoopRuntimeBindingsV1:
    stages = dict(runtime_bindings.stages)
    for selector, row in overrides.items():
        stage_name, capability_key = selector.split(".", 1)
        stage = stages[stage_name]
        capabilities = dict(stage.capabilities)
        capabilities[capability_key] = Path(row["path"]).resolve()
        stages[stage_name] = RuntimeStageBindingV1(
            stage_name=stage.stage_name,
            runtime_profile=stage.runtime_profile,
            context_profile=stage.context_profile,
            capabilities=capabilities,
            source_id=stage.source_id,
            model_id=stage.model_id,
        )
    return ChapterLoopRuntimeBindingsV1(
        source_path=runtime_bindings.source_path,
        binding_id=runtime_bindings.binding_id,
        stages=stages,
        binding_hash=runtime_bindings.binding_hash,
    )


def _parse_context_profile_overrides(
    values: Sequence[str],
    *,
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise SystemExit("context profile override must use STAGE=PATH")
        stage_name, raw_path = raw.split("=", 1)
        stage = runtime_bindings.stages.get(stage_name)
        if stage is None or stage.context_profile is None:
            raise SystemExit(
                f"unknown context profile override: {stage_name}"
            )
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise SystemExit(
                f"context profile override is absent: {stage_name}"
            )
        _validate_context_profile_identity(path=path, stage=stage)
        result[stage_name] = {
            "path": str(path),
            "file_sha256": file_sha256(path),
        }
    return result


def _validated_context_profile_overrides(
    value: Any,
    *,
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SystemExit("context profile overrides are malformed")
    result: dict[str, dict[str, str]] = {}
    for stage_name, raw in value.items():
        stage = runtime_bindings.stages.get(stage_name)
        if (
            not isinstance(stage_name, str)
            or stage is None
            or stage.context_profile is None
        ):
            raise SystemExit(
                f"unknown persisted context profile override: {stage_name}"
            )
        if not isinstance(raw, Mapping) or set(raw) != {
            "path",
            "file_sha256",
        }:
            raise SystemExit("context profile override row is malformed")
        path = Path(str(raw["path"])).resolve()
        if not path.is_file() or file_sha256(path) != raw["file_sha256"]:
            raise SystemExit(
                f"context profile override drifted: {stage_name}"
            )
        _validate_context_profile_identity(path=path, stage=stage)
        result[stage_name] = {
            "path": str(path),
            "file_sha256": str(raw["file_sha256"]),
        }
    return result


def _runtime_bindings_with_context_profile_overrides(
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
    overrides: Mapping[str, Mapping[str, str]],
) -> ChapterLoopRuntimeBindingsV1:
    stages = dict(runtime_bindings.stages)
    for stage_name, row in overrides.items():
        stage = stages[stage_name]
        stages[stage_name] = RuntimeStageBindingV1(
            stage_name=stage.stage_name,
            runtime_profile=stage.runtime_profile,
            context_profile=Path(row["path"]).resolve(),
            capabilities=stage.capabilities,
            source_id=stage.source_id,
            model_id=stage.model_id,
        )
    return ChapterLoopRuntimeBindingsV1(
        source_path=runtime_bindings.source_path,
        binding_id=runtime_bindings.binding_id,
        stages=stages,
        binding_hash=runtime_bindings.binding_hash,
    )


def _context_profile_non_capacity_signature(payload: Mapping[str, Any]) -> str:
    normalized = json.loads(json.dumps(payload))
    normalized.pop("profile_id", None)
    normalized.pop("profile_revision", None)
    schema_version = normalized.get("schema_version")
    if schema_version == "literary_b3_temporal_phase_a_profile_v1":
        batching = normalized.get("batching")
        if not isinstance(batching, dict):
            raise SystemExit("B3 context profile batching is malformed")
        batching.pop("max_requests_per_chapter", None)
        token_caps = normalized.get("token_caps")
        if not isinstance(token_caps, dict):
            raise SystemExit("B3 context profile token caps are malformed")
        token_caps.pop("prompt_tokens_per_request", None)
    else:
        raise SystemExit(
            "context capacity override is unsupported for this profile"
        )
    return canonical_hash(normalized)


def _context_profile_capacity(payload: Mapping[str, Any]) -> int:
    batching = payload.get("batching")
    if not isinstance(batching, Mapping):
        raise SystemExit("context profile batching is malformed")
    value = batching.get("max_requests_per_chapter")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        raise SystemExit("context profile request ceiling is malformed")
    return int(value)


def _context_profile_prompt_capacity(payload: Mapping[str, Any]) -> int:
    token_caps = payload.get("token_caps")
    if not isinstance(token_caps, Mapping):
        raise SystemExit("B3 context profile token caps are malformed")
    value = token_caps.get("prompt_tokens_per_request")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        raise SystemExit("B3 context profile prompt ceiling is malformed")
    return int(value)


def _validate_context_profile_identity(
    *,
    path: Path,
    stage: RuntimeStageBindingV1,
) -> None:
    payload = _read_object(path)
    original = _read_object(stage.context_profile)
    if payload.get("schema_version") == "literary_b2_ch1_canary_profile_v4":
        _validate_b2_context_profile_capacity_override(
            path=path,
            payload=payload,
            original_path=stage.context_profile,
            original=original,
            stage_name=stage.stage_name,
        )
        return
    if _context_profile_non_capacity_signature(payload) != (
        _context_profile_non_capacity_signature(original)
    ):
        raise SystemExit(
            f"context profile override changes non-capacity fields: "
            f"{stage.stage_name}"
        )
    if _context_profile_capacity(payload) < _context_profile_capacity(original):
        raise SystemExit(
            f"context profile override lowers request ceiling: "
            f"{stage.stage_name}"
        )
    if _context_profile_prompt_capacity(payload) < (
        _context_profile_prompt_capacity(original)
    ):
        raise SystemExit(
            f"context profile override lowers prompt ceiling: "
            f"{stage.stage_name}"
        )


def _validate_b2_context_profile_capacity_override(
    *,
    path: Path,
    payload: Mapping[str, Any],
    original_path: Path,
    original: Mapping[str, Any],
    stage_name: str,
) -> None:
    if original.get("schema_version") != "literary_b2_ch1_canary_profile_v4":
        raise SystemExit(
            f"context profile schema differs: {stage_name}"
        )
    candidate_canary = json.loads(json.dumps(payload))
    original_canary = json.loads(json.dumps(original))
    candidate_canary.pop("profile_id", None)
    original_canary.pop("profile_id", None)
    candidate_dependency = candidate_canary.pop("b2_profile", None)
    original_dependency = original_canary.pop("b2_profile", None)
    candidate_limits = candidate_canary.get("limits")
    original_limits = original_canary.get("limits")
    if not isinstance(candidate_limits, dict) or not isinstance(original_limits, dict):
        raise SystemExit("B2 context profile limits are malformed")
    candidate_hard_cap = candidate_limits.pop("hard_visible_token_cap", None)
    original_hard_cap = original_limits.pop("hard_visible_token_cap", None)
    if (
        not isinstance(candidate_hard_cap, int)
        or isinstance(candidate_hard_cap, bool)
        or not isinstance(original_hard_cap, int)
        or isinstance(original_hard_cap, bool)
        or candidate_hard_cap < original_hard_cap
    ):
        raise SystemExit(
            f"context profile override lowers aggregate token ceiling: {stage_name}"
        )
    if canonical_hash(candidate_canary) != canonical_hash(original_canary):
        raise SystemExit(
            f"context profile override changes non-capacity fields: {stage_name}"
        )
    if not isinstance(candidate_dependency, str) or not candidate_dependency:
        raise SystemExit("B2 context profile dependency is malformed")
    if not isinstance(original_dependency, str) or not original_dependency:
        raise SystemExit("B2 original context profile dependency is malformed")
    candidate_phase_path = (path.parent / candidate_dependency).resolve()
    original_phase_path = (
        Path(original_path).resolve().parent / original_dependency
    ).resolve()
    if not candidate_phase_path.is_file():
        raise SystemExit("B2 context profile dependency is absent")
    candidate_phase = _read_object(candidate_phase_path)
    original_phase = _read_object(original_phase_path)
    candidate_normalized = json.loads(json.dumps(candidate_phase))
    original_normalized = json.loads(json.dumps(original_phase))
    candidate_normalized.pop("profile_id", None)
    original_normalized.pop("profile_id", None)
    candidate_context_caps = candidate_normalized.pop("context_caps", None)
    original_context_caps = original_normalized.pop("context_caps", None)
    candidate_caps = candidate_normalized.pop("token_caps", None)
    original_caps = original_normalized.pop("token_caps", None)
    if canonical_hash(candidate_normalized) != canonical_hash(original_normalized):
        raise SystemExit(
            f"context profile override changes non-capacity fields: {stage_name}"
        )
    if (
        not isinstance(candidate_context_caps, Mapping)
        or not isinstance(original_context_caps, Mapping)
        or set(candidate_context_caps) != set(original_context_caps)
    ):
        raise SystemExit("B2 context profile candidate caps are malformed")
    for key, original_value in original_context_caps.items():
        candidate_value = candidate_context_caps[key]
        if (
            not isinstance(original_value, int)
            or isinstance(original_value, bool)
            or original_value < 1
            or not isinstance(candidate_value, int)
            or isinstance(candidate_value, bool)
            or candidate_value < original_value
        ):
            raise SystemExit(
                f"context profile override lowers candidate ceiling: {stage_name}"
            )
    if (
        not isinstance(candidate_caps, Mapping)
        or not isinstance(original_caps, Mapping)
        or set(candidate_caps) != set(original_caps)
    ):
        raise SystemExit("B2 context profile token caps are malformed")
    for key, original_value in original_caps.items():
        candidate_value = candidate_caps[key]
        if (
            not isinstance(original_value, int)
            or isinstance(original_value, bool)
            or original_value < 1
            or not isinstance(candidate_value, int)
            or isinstance(candidate_value, bool)
            or candidate_value < original_value
        ):
            raise SystemExit(
                f"context profile override lowers request ceiling: {stage_name}"
            )
def _parse_runtime_profile_overrides(
    values: Sequence[str],
    *,
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise SystemExit("runtime profile override must use STAGE=PATH")
        stage_name, raw_path = raw.split("=", 1)
        stage = runtime_bindings.stages.get(stage_name)
        if stage is None or stage.runtime_profile is None:
            raise SystemExit(f"unknown runtime profile override: {stage_name}")
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise SystemExit(
                f"runtime profile override is absent: {stage_name}"
            )
        _validate_runtime_profile_identity(path=path, stage=stage)
        result[stage_name] = {
            "path": str(path),
            "file_sha256": file_sha256(path),
        }
    return result


def _validated_runtime_profile_overrides(
    value: Any,
    *,
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SystemExit("runtime profile overrides are malformed")
    result: dict[str, dict[str, str]] = {}
    for stage_name, raw in value.items():
        stage = runtime_bindings.stages.get(stage_name)
        if (
            not isinstance(stage_name, str)
            or stage is None
            or stage.runtime_profile is None
        ):
            raise SystemExit(
                f"unknown persisted runtime profile override: {stage_name}"
            )
        if not isinstance(raw, Mapping) or set(raw) != {
            "path",
            "file_sha256",
        }:
            raise SystemExit("runtime profile override row is malformed")
        path = Path(str(raw["path"])).resolve()
        if not path.is_file() or file_sha256(path) != raw["file_sha256"]:
            raise SystemExit(
                f"runtime profile override drifted: {stage_name}"
            )
        _validate_runtime_profile_identity(path=path, stage=stage)
        result[stage_name] = {
            "path": str(path),
            "file_sha256": str(raw["file_sha256"]),
        }
    return result


def _runtime_bindings_with_profile_overrides(
    runtime_bindings: ChapterLoopRuntimeBindingsV1,
    overrides: Mapping[str, Mapping[str, str]],
) -> ChapterLoopRuntimeBindingsV1:
    stages = dict(runtime_bindings.stages)
    for stage_name, row in overrides.items():
        stage = stages[stage_name]
        stages[stage_name] = RuntimeStageBindingV1(
            stage_name=stage.stage_name,
            runtime_profile=Path(row["path"]).resolve(),
            context_profile=stage.context_profile,
            capabilities=stage.capabilities,
            source_id=stage.source_id,
            model_id=stage.model_id,
        )
    return ChapterLoopRuntimeBindingsV1(
        source_path=runtime_bindings.source_path,
        binding_id=runtime_bindings.binding_id,
        stages=stages,
        binding_hash=runtime_bindings.binding_hash,
    )


def _validate_runtime_profile_identity(
    *,
    path: Path,
    stage: RuntimeStageBindingV1,
) -> None:
    payload = _read_object(path)
    original = _read_object(stage.runtime_profile)
    source_ids = {
        row.get("source_id")
        for row in payload.get("sources") or []
        if isinstance(row, Mapping)
    }
    model_ids = {
        row.get("requested_model_id")
        for row in payload.get("roles") or []
        if isinstance(row, Mapping)
    }
    if source_ids != {stage.source_id}:
        raise SystemExit(
            f"runtime profile override source_id differs: {stage.stage_name}"
        )
    if model_ids != {stage.model_id}:
        raise SystemExit(
            f"runtime profile override model differs: {stage.stage_name}"
        )
    if _non_capacity_profile_signature(payload) != (
        _non_capacity_profile_signature(original)
    ):
        raise SystemExit(
            f"runtime profile override changes non-capacity fields: "
            f"{stage.stage_name}"
        )
    _validate_runtime_profile_capacity_is_upward(
        payload,
        original,
        stage_name=stage.stage_name,
    )


def _non_capacity_profile_signature(
    payload: Mapping[str, Any],
) -> str:
    normalized = json.loads(json.dumps(payload))
    normalized.pop("profile_id", None)
    normalized.pop("profile_revision", None)
    for role in normalized.get("roles") or []:
        if not isinstance(role, dict):
            continue
        role.pop("preset_id", None)
        role.pop("preset_revision", None)
        role.pop("namespaces", None)
        generation = role.get("generation")
        if isinstance(generation, dict):
            for key in (
                "context_window_tokens",
                "max_input_tokens",
                "max_output_tokens",
                "memory_token_budget",
            ):
                generation.pop(key, None)
        limits = role.get("limits")
        if isinstance(limits, dict):
            for key in (
                "max_calls",
                "max_prompt_tokens",
                "max_completion_tokens",
                "max_total_tokens",
            ):
                limits.pop(key, None)
    return canonical_hash(normalized)


def _validate_runtime_profile_capacity_is_upward(
    payload: Mapping[str, Any],
    original: Mapping[str, Any],
    *,
    stage_name: str,
) -> None:
    before = {
        str(row.get("role_id")): row
        for row in original.get("roles") or []
        if isinstance(row, Mapping)
    }
    after = {
        str(row.get("role_id")): row
        for row in payload.get("roles") or []
        if isinstance(row, Mapping)
    }
    if set(before) != set(after):
        raise SystemExit(
            f"runtime profile role set changed: {stage_name}"
        )
    capacity_paths = (
        ("generation", "max_input_tokens"),
        ("generation", "max_output_tokens"),
        ("generation", "memory_token_budget"),
        ("limits", "max_calls"),
        ("limits", "max_prompt_tokens"),
        ("limits", "max_completion_tokens"),
        ("limits", "max_total_tokens"),
    )
    for role_id, old_role in before.items():
        new_role = after[role_id]
        for section, key in capacity_paths:
            old_section = old_role.get(section)
            new_section = new_role.get(section)
            old_value = old_section.get(key) if isinstance(old_section, Mapping) else None
            new_value = new_section.get(key) if isinstance(new_section, Mapping) else None
            if old_value is None:
                continue
            if (
                not isinstance(old_value, int)
                or isinstance(old_value, bool)
                or not isinstance(new_value, int)
                or isinstance(new_value, bool)
                or new_value < old_value
            ):
                raise SystemExit(
                    f"runtime profile capacity is not upward: "
                    f"{stage_name}:{role_id}:{section}.{key}"
                )


def _selected_chapters(
    *,
    document: Mapping[str, Any],
    chapter_ids: Sequence[str] | None,
    chapter_range: str | None,
    all_chapters: bool,
) -> list[str]:
    document_ids = [
        str(row["chapter_id"])
        for row in document["chapters"]
        if isinstance(row, Mapping)
    ]
    if all_chapters:
        selected = document_ids
    elif chapter_range:
        parts = chapter_range.split(":")
        if len(parts) != 2 or not all(parts):
            raise SystemExit("--chapter-range must be START_ID:END_ID")
        try:
            start = document_ids.index(parts[0])
            end = document_ids.index(parts[1])
        except ValueError as exc:
            raise SystemExit("chapter range contains a foreign chapter") from exc
        if start > end:
            raise SystemExit("chapter range reverses document order")
        selected = document_ids[start : end + 1]
    else:
        selected = list(chapter_ids or [])
    if not selected:
        raise SystemExit("chapter selection is empty")
    positions = [document_ids.index(chapter_id) for chapter_id in selected]
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise SystemExit("chapter selection must be contiguous")
    if positions[0] != 0:
        raise SystemExit(
            "a new lineage must start at the document's first chapter"
        )
    return selected


def _run_summary(run_root: Path) -> dict[str, Any]:
    state = load_chapter_cycle_state_v1(run_root)
    return {
        "schema_version": "literary_chapter_loop_run_summary_v1",
        "run_root": str(Path(run_root).resolve()),
        "status": state["status"],
        "current_stage": state["current_stage"],
        "completed_chapter_ids": state["completed_chapter_ids"],
        "run_api_call_count": state["run_api_call_count"],
        "chapter_api_call_counts": state["chapter_api_call_counts"],
        "semantic_pending_count": state["semantic_pending_count"],
        "halt_failure_class": state["halt_failure_class"],
        "halt_reason": state["halt_reason"],
        "resume_command": (
            _resume_command(run_root) if state["status"] in {"paused", "stopped"} else None
        ),
        "production_publish_performed": False,
    }


def _resume_command(run_root: Path) -> str:
    return (
        "python pipeline/scripts/run_literary_chapter_loop_v1.py resume "
        f"--run-root \"{Path(run_root).resolve()}\" "
        "--credential-file <explicit-credential-file> "
        "--scheduler-root <explicit-scheduler-root>"
    )


def _clean_head() -> str:
    _require_clean_tracked_worktree()
    return _head()


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _require_clean_tracked_worktree() -> None:
    dirty = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("live chapter-loop execution requires a clean tracked worktree")


def _required_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{label} must be a non-empty string")
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in value):
        raise SystemExit(f"{label} must use lowercase safe characters")
    return value


def _session_revision_history(session: Mapping[str, Any]) -> list[str]:
    initial = session.get("code_revision")
    if not isinstance(initial, str) or not initial:
        raise SystemExit("chapter-loop session lacks its initial code revision")
    raw_history = session.get("code_revision_history")
    if raw_history is None:
        history = [initial]
    elif (
        not isinstance(raw_history, list)
        or not raw_history
        or not all(isinstance(value, str) and value for value in raw_history)
    ):
        raise SystemExit("chapter-loop code revision history is malformed")
    else:
        history = list(raw_history)
    if history[0] != initial:
        raise SystemExit("chapter-loop code revision history lost its lineage origin")
    active = session.get("active_code_revision", history[-1])
    if active != history[-1]:
        raise SystemExit("chapter-loop active code revision is stale")
    return history


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"cannot load JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return value


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
