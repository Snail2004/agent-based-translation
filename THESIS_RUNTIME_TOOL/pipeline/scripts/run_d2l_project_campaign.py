from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Mapping

from pipeline.prepass.d2l_project_campaign_v2 import (
    ensure_resume_transport_attempt_seals,
    prepare_campaign,
)
from pipeline.prepass.d2l_project_stage_runner_v1 import (
    build_component_plan,
    execute_stage,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import (
    D2LTransportRetriesExhausted,
    TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import run_from_plan_file


class _FileCredentialProvider:
    """Resolve only explicitly mapped external credential files."""

    def __init__(self, paths: Mapping[str, Path]) -> None:
        self._paths = dict(paths)

    def resolve(self, credential_ref: str) -> str | None:
        path = self._paths.get(credential_ref)
        if path is None:
            return None
        value = path.read_text(encoding="utf-8").strip()
        if not value or any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError(f"credential file is invalid: {credential_ref}")
        return value


def _credential_files(values: list[str] | None) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values or []:
        credential_ref, separator, path_text = raw.partition("=")
        if not separator or not credential_ref or not path_text:
            raise ValueError("--credential-file must be CREDENTIAL_REF=ABSOLUTE_PATH")
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            raise ValueError("--credential-file paths must be absolute")
        if credential_ref in result:
            raise ValueError(f"duplicate credential ref: {credential_ref}")
        result[credential_ref] = path.resolve()
    return result


def _prepare_resume_transport_attempt(
    campaign_root: Path,
    component_attempt_id: int,
) -> None:
    ensure_resume_transport_attempt_seals(
        campaign_root,
        component_attempt_id=component_attempt_id,
    )


def _execution_mode(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--runtime-root")
    parser.add_argument("--credential-file", action="append", default=[])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a sealed D2L project campaign")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--job-root", required=True)
    prepare.add_argument("--campaign-root", required=True)
    prepare.add_argument("--workflow-run-id", required=True)
    prepare.add_argument("--component-run-id", required=True)
    scope = prepare.add_mutually_exclusive_group(required=True)
    scope.add_argument("--chapter-id", action="append", dest="chapter_ids")
    scope.add_argument("--all-chapters", action="store_true")
    scope.add_argument("--chapter-range", nargs=2, metavar=("START", "END"))
    prepare.add_argument("--reserved-cost-cap-usd")
    prepare.add_argument("--hard-total-token-cap", type=int)
    prepare.add_argument("--allow-dirty-code", action="store_true")

    plan = subparsers.add_parser("build-plan")
    plan.add_argument("--job-root", required=True)
    plan.add_argument("--campaign-root", required=True)
    plan.add_argument("--code-root", default=str(Path(__file__).resolve().parents[2]))
    _execution_mode(plan)

    execute = subparsers.add_parser("execute-stage")
    execute.add_argument("--job-root", required=True)
    execute.add_argument("--campaign-root", required=True)
    execute.add_argument("--stage-id", required=True)
    _execution_mode(execute)

    run = subparsers.add_parser("run-component")
    run.add_argument("--campaign-root", required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--stop-after-stage")
    run.add_argument("--pause-file")
    run.add_argument("--code-root", default=str(Path(__file__).resolve().parents[2]))
    run.add_argument("--repair-reason")
    run.add_argument("--recover-stale", action="store_true")

    app_run = subparsers.add_parser(
        "app-run",
        help="Prepare, bind and execute one server-owned D2L campaign.",
    )
    app_run.add_argument("--job-root", required=True)
    app_run.add_argument("--campaign-root", required=True)
    app_run.add_argument("--workflow-run-id")
    app_run.add_argument("--component-run-id")
    scope = app_run.add_mutually_exclusive_group()
    scope.add_argument("--chapter-id", action="append", dest="chapter_ids")
    scope.add_argument("--all-chapters", action="store_true")
    scope.add_argument("--chapter-range", nargs=2, metavar=("START", "END"))
    app_run.add_argument("--reserved-cost-cap-usd")
    app_run.add_argument("--hard-total-token-cap", type=int)
    app_run.add_argument("--code-root", default=str(Path(__file__).resolve().parents[2]))
    app_run.add_argument("--resume", action="store_true")
    app_run.add_argument("--repair-reason")
    app_run.add_argument("--recover-stale", action="store_true")
    app_run.add_argument("--allow-dirty-code", action="store_true")
    _execution_mode(app_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        chapter_range = args.chapter_range or (None, None)
        result = prepare_campaign(
            job_root=args.job_root,
            campaign_root=args.campaign_root,
            workflow_run_id=args.workflow_run_id,
            component_run_id=args.component_run_id,
            require_clean_code=not args.allow_dirty_code,
            chapter_ids=args.chapter_ids,
            start_chapter=chapter_range[0],
            end_chapter=chapter_range[1],
            all_chapters=args.all_chapters,
            reserved_cost_cap_usd=args.reserved_cost_cap_usd,
            hard_total_token_cap=args.hard_total_token_cap,
        )
    elif args.command == "build-plan":
        credential_files = _credential_files(args.credential_file)
        result = build_component_plan(
            campaign_root=args.campaign_root,
            job_root=args.job_root,
            code_root=args.code_root,
            dry_run=args.dry_run,
            runtime_root=args.runtime_root,
            credential_files=credential_files or None,
        )
    elif args.command == "execute-stage":
        credential_files = _credential_files(args.credential_file)
        provider = _FileCredentialProvider(credential_files) if args.live else None
        result = execute_stage(
            campaign_root=args.campaign_root,
            job_root=args.job_root,
            stage_id=args.stage_id,
            dry_run=args.dry_run,
            runtime_root=args.runtime_root,
            credential_provider=provider,
        )
    elif args.command == "run-component":
        campaign_root = Path(args.campaign_root).resolve()
        result = run_from_plan_file(
            campaign_root / "component_plan.json",
            campaign_root / "component",
            resume=args.resume,
            stop_after_stage=args.stop_after_stage,
            pause_file=args.pause_file or campaign_root / "PAUSE",
            repair_code_root=args.code_root,
            repair_reason=args.repair_reason,
            recover_stale=args.recover_stale,
            resume_attempt_preparer=lambda component_attempt_id: (
                _prepare_resume_transport_attempt(
                    campaign_root,
                    component_attempt_id,
                )
            ),
        )
    else:
        campaign_root = Path(args.campaign_root).resolve()
        if args.resume:
            if args.workflow_run_id or args.component_run_id:
                raise ValueError("resume does not accept new workflow/component ids")
            # The server removes this marker before a legal Resume.  Clearing
            # it here also keeps the standalone CLI from immediately pausing
            # again after it has resumed the sealed component.
            pause_file = campaign_root / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()
            result = run_from_plan_file(
                campaign_root / "component_plan.json",
                campaign_root / "component",
                resume=True,
                pause_file=pause_file,
                repair_code_root=args.code_root,
                repair_reason=args.repair_reason,
                recover_stale=args.recover_stale,
                resume_attempt_preparer=lambda component_attempt_id: (
                    _prepare_resume_transport_attempt(
                        campaign_root,
                        component_attempt_id,
                    )
                ),
            )
        else:
            if not args.workflow_run_id or not args.component_run_id:
                raise ValueError("fresh app-run requires workflow and component ids")
            if not (args.chapter_ids or args.all_chapters or args.chapter_range):
                raise ValueError("fresh app-run requires a chapter selection")
            chapter_range = args.chapter_range or (None, None)
            prepare_campaign(
                job_root=args.job_root,
                campaign_root=campaign_root,
                workflow_run_id=args.workflow_run_id,
                component_run_id=args.component_run_id,
                code_root=args.code_root,
                require_clean_code=not args.allow_dirty_code,
                chapter_ids=args.chapter_ids,
                start_chapter=chapter_range[0],
                end_chapter=chapter_range[1],
                all_chapters=args.all_chapters,
                reserved_cost_cap_usd=args.reserved_cost_cap_usd,
                hard_total_token_cap=args.hard_total_token_cap,
            )
            credential_files = _credential_files(args.credential_file)
            build_component_plan(
                campaign_root=campaign_root,
                job_root=args.job_root,
                code_root=args.code_root,
                dry_run=args.dry_run,
                runtime_root=args.runtime_root,
                credential_files=credential_files or None,
            )
            result = run_from_plan_file(
                campaign_root / "component_plan.json",
                campaign_root / "component",
                pause_file=campaign_root / "PAUSE",
                repair_code_root=args.code_root,
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except D2LTransportRetriesExhausted as exc:
        print(
            json.dumps(
                {
                    "status": "paused_transport_retry_exhausted",
                    "logical_request_id": exc.logical_request_id,
                    "retry_count": exc.retry_summary["retry_count"],
                    "reason_codes": exc.retry_summary["reason_codes"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE)
