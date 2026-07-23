from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pipeline.prepass.d2l_project_campaign_v2 import prepare_campaign
from pipeline.prepass.d2l_project_stage_runner_v1 import (
    build_component_plan,
    execute_stage,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import run_from_plan_file


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
    plan.add_argument("--dry-run", action="store_true", required=True)

    execute = subparsers.add_parser("execute-stage")
    execute.add_argument("--job-root", required=True)
    execute.add_argument("--campaign-root", required=True)
    execute.add_argument("--stage-id", required=True)
    execute.add_argument("--dry-run", action="store_true", required=True)

    run = subparsers.add_parser("run-component")
    run.add_argument("--campaign-root", required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--stop-after-stage")
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
        result = build_component_plan(
            campaign_root=args.campaign_root,
            job_root=args.job_root,
            code_root=args.code_root,
            dry_run=args.dry_run,
        )
    elif args.command == "execute-stage":
        result = execute_stage(
            campaign_root=args.campaign_root,
            job_root=args.job_root,
            stage_id=args.stage_id,
            dry_run=args.dry_run,
        )
    else:
        campaign_root = Path(args.campaign_root).resolve()
        result = run_from_plan_file(
            campaign_root / "component_plan.json",
            campaign_root / "component",
            resume=args.resume,
            stop_after_stage=args.stop_after_stage,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
