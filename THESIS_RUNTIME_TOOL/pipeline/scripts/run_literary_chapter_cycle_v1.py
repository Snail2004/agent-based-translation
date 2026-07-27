from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.provider_profile import load_provider_profile
from pipeline.literary.chapter_cycle_live_executor_v1 import (
    ChapterCycleLiveExecutorV1,
)
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ApiCallPermit,
    ChapterCycleStage,
    StageExecutionResult,
    build_dynamic_stage_plan_v1,
    current_chapter_cycle_stage_v1,
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    load_chapter_cycle_state_v1,
    resume_chapter_cycle_run_v1,
    run_chapter_cycle_until_boundary_v1,
)
from pipeline.literary.chapter_cycle_profile_v1 import load_chapter_cycle_profile
from pipeline.literary.checkpoint import canonical_hash, write_checkpoint_atomic
from pipeline.literary.literary_pipeline_profile_v1 import (
    load_literary_pipeline_profile,
    public_stage_plan,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCUMENT = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_l2a0_wh_builder_scaffold"
    / "document.json"
)
DEFAULT_PIPELINE_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_pipeline_profile_v2.json"
)
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return value


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, indent=2))


def _document_chapter_ids(document: Mapping[str, Any]) -> list[str]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise SystemExit("document has no chapters")
    chapter_ids = [
        str(row.get("chapter_id") or "") if isinstance(row, Mapping) else ""
        for row in chapters
    ]
    if not all(chapter_ids) or len(chapter_ids) != len(set(chapter_ids)):
        raise SystemExit("document chapter ids are malformed")
    return chapter_ids


def _selected_chapters(
    *,
    document: Mapping[str, Any],
    chapter_ids: Sequence[str] | None,
    chapter_range: str | None,
    all_chapters: bool,
    required: bool,
) -> list[str] | None:
    document_ids = _document_chapter_ids(document)
    selected: list[str] | None = None
    modes = int(bool(chapter_ids)) + int(chapter_range is not None) + int(all_chapters)
    if modes > 1:
        raise SystemExit("choose only one chapter-selection mode")
    if all_chapters:
        selected = document_ids
    elif chapter_range is not None:
        parts = chapter_range.split(":")
        if len(parts) != 2 or not all(parts):
            raise SystemExit("--chapter-range must be START_ID:END_ID")
        try:
            start = document_ids.index(parts[0])
            end = document_ids.index(parts[1])
        except ValueError as exc:
            raise SystemExit("chapter range contains a foreign chapter id") from exc
        if start > end:
            raise SystemExit("chapter range must preserve document order")
        selected = document_ids[start : end + 1]
    elif chapter_ids:
        selected = list(chapter_ids)
    elif required:
        raise SystemExit("a chapter selection is required for a new run")

    if selected is None:
        return None
    if not selected or len(selected) != len(set(selected)):
        raise SystemExit("chapter selection is empty or repeats a chapter")
    positions: list[int] = []
    try:
        positions = [document_ids.index(chapter_id) for chapter_id in selected]
    except ValueError as exc:
        raise SystemExit("chapter selection contains a foreign chapter") from exc
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise SystemExit("chapter selection must be one contiguous document range")
    if positions[0] != 0:
        raise SystemExit(
            "pipeline V1 must start at the first document chapter; "
            "a future checkpoint-import contract will enable later starts"
        )
    return selected


def _selection_from_args(
    args: argparse.Namespace, *, required: bool
) -> tuple[dict[str, Any], list[str] | None]:
    document = _load_json(args.document, "document")
    selected = _selected_chapters(
        document=document,
        chapter_ids=getattr(args, "chapter_id", None),
        chapter_range=getattr(args, "chapter_range", None),
        all_chapters=bool(getattr(args, "all_chapters", False)),
        required=required,
    )
    return document, selected


def _has_selection_args(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "chapter_id", None)
        or getattr(args, "chapter_range", None)
        or getattr(args, "all_chapters", False)
    )


def _profile_preview(
    *, pipeline_profile_path: Path, document: Mapping[str, Any], selected: list[str]
) -> dict[str, Any]:
    pipeline_profile = load_literary_pipeline_profile(pipeline_profile_path)
    cycle = load_chapter_cycle_profile(pipeline_profile.chapter_cycle_profile_path)
    provider = load_provider_profile(cycle.provider_profile_path())
    stage_plan = build_dynamic_stage_plan_v1(
        document=document, ordered_chapter_ids=selected
    )
    runtime_rows: list[dict[str, Any]] = []
    for row in public_stage_plan(pipeline_profile, stage_plan):
        projected = dict(row)
        if row["stage_role"] == "code":
            projected["execution"] = {"provider": "code", "api_calls": 0}
        else:
            role_id = cycle.role_bindings[str(row["stage_role"])]
            role = provider.roles[role_id]
            limits = cycle.stage_limits[str(row["stage_role"])]
            projected["execution"] = {
                "role_id": role_id,
                "provider": role.provider,
                "model_id": role.model_id,
                "quota_bucket_ids": list(role.bucket_order),
                "limits": asdict(limits),
            }
        runtime_rows.append(projected)
    return {
        "schema_version": "literary_pipeline_plan_preview_v1",
        "pipeline_profile_id": pipeline_profile.profile_id,
        "pipeline_profile_hash": pipeline_profile.profile_hash,
        "selected_chapter_ids": selected,
        "stop_after_default": min(
            int(cycle.orchestration["default_stop_after_chapter_count"]),
            len(selected),
        ),
        "resilience": dict(cycle.resilience),
        "orchestration": dict(cycle.orchestration),
        "semantic_leads": dict(cycle.semantic_leads),
        "console_controls": dict(pipeline_profile.console_controls),
        "structured_output": (
            None
            if pipeline_profile.structured_output_policy is None
            else {
                "policy_id": pipeline_profile.structured_output_policy.policy_id,
                "policy_hash": pipeline_profile.structured_output_policy.policy_hash,
                "roles": {
                    role_id: {
                        "mode": row.mode,
                        "format_repair_cap": row.format_repair_cap,
                    }
                    for role_id, row in (
                        pipeline_profile.structured_output_policy.role_policies.items()
                    )
                },
            }
        ),
        "usage_baseline": {
            "baseline_id": pipeline_profile.usage_baseline.baseline_id,
            "quota_bucket_id": pipeline_profile.usage_baseline.quota_bucket_id,
            "credential_revision": (
                pipeline_profile.usage_baseline.credential_revision
            ),
            "provider_counter_baselines": dict(
                pipeline_profile.usage_baseline.provider_counter_baselines
            ),
            "remaining_quota_must_not_be_inferred": True,
        },
        "public_stage_contract": {
            key: {
                "enabled": value.enabled,
                "implementation_stage_names": list(
                    value.implementation_stage_names
                ),
            }
            for key, value in pipeline_profile.public_stages.items()
        },
        "stage_plan": runtime_rows,
        "production_publish_enabled": False,
    }


def _initialize_from_args(
    args: argparse.Namespace, *, require_empty: bool
) -> dict[str, Any]:
    run_root = Path(args.run_root).resolve()
    if not require_empty and (run_root / "run_plan.json").is_file():
        plan = load_chapter_cycle_plan_v1(run_root)
        if _has_selection_args(args):
            selected = _selected_chapters(
                document=_load_json(Path(plan["document_path"]), "sealed document"),
                chapter_ids=getattr(args, "chapter_id", None),
                chapter_range=getattr(args, "chapter_range", None),
                all_chapters=bool(getattr(args, "all_chapters", False)),
                required=True,
            )
            if selected != plan["ordered_chapter_ids"]:
                raise SystemExit("existing run uses a different chapter selection")
        return load_chapter_cycle_state_v1(run_root)
    document, selected = _selection_from_args(args, required=True)
    assert selected is not None
    pipeline_profile = load_literary_pipeline_profile(args.pipeline_profile)
    _ = document
    return initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=args.document,
        profile_path=pipeline_profile.chapter_cycle_profile_path,
        frozen_db_path=args.frozen_db,
        ordered_chapter_ids=selected,
        stop_after_chapter_count=args.stop_after_chapter_count,
        pipeline_profile_path=pipeline_profile.source_path,
    )


def _live_executor(args: argparse.Namespace) -> ChapterCycleLiveExecutorV1:
    plan = load_chapter_cycle_plan_v1(args.run_root)
    usage_roots = tuple(args.usage_root or ()) or None
    return ChapterCycleLiveExecutorV1(
        run_root=args.run_root,
        plan=plan,
        credential_root=args.credential_root,
        usage_roots=usage_roots,
    )


def _status_payload(run_root: Path, *, show_plan: bool) -> dict[str, Any]:
    state = load_chapter_cycle_state_v1(run_root)
    plan = load_chapter_cycle_plan_v1(run_root)
    pipeline_profile = load_literary_pipeline_profile(
        Path(plan["pipeline_profile_path"])
    )
    current = current_chapter_cycle_stage_v1(run_root)
    payload: dict[str, Any] = {
        "state": state,
        "current_public_stage": (
            None
            if current is None
            else {
                **current.to_payload(),
                "implementation_stage_name": current.stage_name,
                "public_stage_name": pipeline_profile.public_stage_name(
                    current.stage_name
                ),
            }
        ),
        "b2": {"enabled": False, "ready": False},
    }
    if state["completed_chapter_ids"]:
        ordinal = len(state["completed_chapter_ids"])
        report = (
            Path(run_root)
            / "artifacts"
            / "chapters"
            / f"ch{ordinal:03d}"
            / "chapter_report.json"
        )
        if report.is_file():
            payload["latest_chapter_report"] = _load_json(
                report, "latest chapter report"
            )
    if show_plan:
        payload["plan"] = {
            **plan,
            "stage_plan": public_stage_plan(
                pipeline_profile, list(plan["stage_plan"])
            ),
        }
    return payload


def write_literary_run_summary_v1(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    state = load_chapter_cycle_state_v1(root)
    plan = load_chapter_cycle_plan_v1(root)
    pipeline_profile = load_literary_pipeline_profile(
        Path(plan["pipeline_profile_path"])
    )
    current = current_chapter_cycle_stage_v1(root)
    chapter_reports: list[dict[str, Any]] = []
    for ordinal, chapter_id in enumerate(state["completed_chapter_ids"], start=1):
        path = (
            root
            / "artifacts"
            / "chapters"
            / f"ch{ordinal:03d}"
            / "chapter_report.json"
        )
        if path.is_file():
            report = _load_json(path, f"chapter report {chapter_id}")
            chapter_reports.append(
                {
                    "chapter_id": chapter_id,
                    "path": path.relative_to(root).as_posix(),
                    "report_hash": report["report_hash"],
                    "prefix_bundle_hash": report["prefix_bundle_hash"],
                }
            )
    body = {
        "schema_version": "literary_pipeline_run_summary_v1",
        "pipeline_profile_id": pipeline_profile.profile_id,
        "pipeline_profile_hash": pipeline_profile.profile_hash,
        "plan_hash": plan["plan_hash"],
        "state_hash": state["state_hash"],
        "state_generation": state["generation"],
        "status": state["status"],
        "completed_chapter_ids": list(state["completed_chapter_ids"]),
        "sealed_chapter_ids": list(plan["ordered_chapter_ids"]),
        "current_public_stage": (
            None
            if current is None
            else pipeline_profile.public_stage_name(current.stage_name)
        ),
        "current_implementation_stage": (
            None if current is None else current.stage_name
        ),
        "run_api_call_count": state["run_api_call_count"],
        "semantic_pending_count": state["semantic_pending_count"],
        "cumulative_hashes": dict(state["cumulative_hashes"]),
        "chapter_reports": chapter_reports,
        "b2": {"enabled": False, "ready": False},
        "production_publish_performed": False,
    }
    summary = {**body, "summary_hash": canonical_hash(body)}
    write_checkpoint_atomic(root / "run_summary.json", summary)
    return summary


def _resume_existing_run_for_run_command(
    args: argparse.Namespace, state: Mapping[str, Any]
) -> dict[str, Any]:
    if state["status"] in {"running", "complete"}:
        return dict(state)
    completed = len(state["completed_chapter_ids"])
    requested = args.stop_after_chapter_count
    if requested is None and state["status"] == "paused":
        requested = int(state["stop_after_chapter_count"])
    if requested is None or requested <= completed:
        raise SystemExit(
            "existing stopped run needs --stop-after-chapter-count beyond its "
            "completed checkpoint; use resume for an explicit continuation"
        )
    return resume_chapter_cycle_run_v1(
        run_root=args.run_root,
        stop_after_chapter_count=requested,
    )


def _fixture_executor(fixture: Mapping[str, Any]):
    def execute(
        stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        raw = fixture.get(stage.stage_id)
        if not isinstance(raw, Mapping):
            raise SystemExit(f"offline fixture lacks stage: {stage.stage_id}")
        logical_call_ids = raw.get("logical_call_ids") or []
        if not isinstance(logical_call_ids, list):
            raise SystemExit(f"logical_call_ids must be a list: {stage.stage_id}")
        for logical_call_id in logical_call_ids:
            permit.reserve(str(logical_call_id))
        call_disposition = str(raw.get("call_disposition") or "")
        request_fingerprint = raw.get("request_fingerprint")
        model_actual = raw.get("model_actual")
        resilience_report_hash = raw.get("resilience_report_hash")
        if call_disposition in {"called", "cache_replay"}:
            request_fingerprint = request_fingerprint or canonical_hash(
                {"stage_id": stage.stage_id, "fixture": "offline"}
            )
            model_actual = model_actual or "offline-fixture-model"
            resilience_report_hash = resilience_report_hash or canonical_hash(
                {"stage_id": stage.stage_id, "report": "offline"}
            )
        return StageExecutionResult(
            status=str(raw.get("status") or "accepted"),
            payload=dict(raw.get("payload") or {}),
            call_disposition=call_disposition,
            request_fingerprint=(
                str(request_fingerprint) if request_fingerprint is not None else None
            ),
            model_actual=str(model_actual) if model_actual is not None else None,
            resilience_report_hash=(
                str(resilience_report_hash)
                if resilience_report_hash is not None
                else None
            ),
            attempt_count=permit.attempt_count(),
            retry_count=int(raw.get("retry_count") or 0),
            fallback_count=int(raw.get("fallback_count") or 0),
            semantic_pending_count=int(raw.get("semantic_pending_count") or 0),
            cumulative_hash_updates=dict(raw.get("cumulative_hash_updates") or {}),
        )

    return execute


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--chapter-id", action="append")
    group.add_argument("--chapter-range", help="inclusive START_ID:END_ID")
    group.add_argument("--all-chapters", action="store_true")


def _add_initialization_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument(
        "--pipeline-profile", type=Path, default=DEFAULT_PIPELINE_PROFILE
    )
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--stop-after-chapter-count", type=int)
    _add_selection_arguments(parser)


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-root", type=Path, required=True)
    parser.add_argument("--usage-root", type=Path, action="append")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the checkpointed, non-production Literary context cycle"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="preview the sealed public/API plan")
    plan.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    plan.add_argument(
        "--pipeline-profile", type=Path, default=DEFAULT_PIPELINE_PROFILE
    )
    _add_selection_arguments(plan)

    init = subparsers.add_parser("init", help="seal a new N-chapter run")
    _add_initialization_arguments(init)

    run = subparsers.add_parser(
        "run", help="initialize when needed and run to the configured boundary"
    )
    _add_initialization_arguments(run)
    _add_live_arguments(run)

    dry = subparsers.add_parser(
        "dry-run", help="initialize when needed and render only the current stage"
    )
    _add_initialization_arguments(dry)

    status = subparsers.add_parser("status", help="verify and print current state")
    status.add_argument("--run-root", type=Path, required=True)
    status.add_argument("--show-plan", action="store_true")

    resume = subparsers.add_parser("resume", help="resume the exact current stage")
    resume.add_argument("--run-root", type=Path, required=True)
    resume.add_argument("--stop-after-chapter-count", type=int)
    _add_live_arguments(resume)

    offline = subparsers.add_parser(
        "run-offline-fixture",
        help="exercise orchestration with sealed synthetic stage results",
    )
    offline.add_argument("--run-root", type=Path, required=True)
    offline.add_argument("--fixture", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        document, selected = _selection_from_args(args, required=True)
        assert selected is not None
        _print_json(
            _profile_preview(
                pipeline_profile_path=args.pipeline_profile,
                document=document,
                selected=selected,
            )
        )
        return 0
    if args.command == "init":
        _print_json(_initialize_from_args(args, require_empty=True))
        return 0
    if args.command == "run":
        state = _initialize_from_args(args, require_empty=False)
        state = _resume_existing_run_for_run_command(args, state)
        if state["status"] == "complete":
            _print_json(write_literary_run_summary_v1(args.run_root))
            return 0
        state = run_chapter_cycle_until_boundary_v1(
            run_root=args.run_root, executor=_live_executor(args)
        )
        _ = state
        _print_json(write_literary_run_summary_v1(args.run_root))
        return 0
    if args.command == "dry-run":
        _initialize_from_args(args, require_empty=False)
        stage = current_chapter_cycle_stage_v1(args.run_root)
        if stage is None:
            raise SystemExit("completed run has no current stage to render")
        plan = load_chapter_cycle_plan_v1(args.run_root)
        executor = ChapterCycleLiveExecutorV1(
            run_root=args.run_root,
            plan=plan,
            credential_root=None,
        )
        _print_json(executor.dry_render_stage(stage))
        return 0
    if args.command == "status":
        _print_json(_status_payload(args.run_root, show_plan=args.show_plan))
        return 0
    if args.command == "resume":
        state = load_chapter_cycle_state_v1(args.run_root)
        if state["status"] == "complete":
            _print_json(write_literary_run_summary_v1(args.run_root))
            return 0
        if state["status"] != "running":
            resume_chapter_cycle_run_v1(
                run_root=args.run_root,
                stop_after_chapter_count=args.stop_after_chapter_count,
            )
        state = run_chapter_cycle_until_boundary_v1(
            run_root=args.run_root, executor=_live_executor(args)
        )
        _ = state
        _print_json(write_literary_run_summary_v1(args.run_root))
        return 0
    if args.command == "run-offline-fixture":
        fixture = _load_json(args.fixture, "offline fixture")
        state = run_chapter_cycle_until_boundary_v1(
            run_root=args.run_root,
            executor=_fixture_executor(fixture),
        )
        _ = state
        _print_json(write_literary_run_summary_v1(args.run_root))
        return 0
    raise SystemExit("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
