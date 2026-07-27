from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Sequence

from pipeline.literary.b2_context_v1 import load_b2_phase_a_profile
from pipeline.literary.b2_phase_a_v1 import dry_render_real_b1_run_v1


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DEFAULT_SOURCE_RUN = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_review_lifecycle_wh_ch01_ch04_recert_20260717_073923"
)
DEFAULT_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_phase_a_profile_v1.json"
)


def _current_git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise SystemExit("cannot determine current Git HEAD")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the Literary B2 Phase-A request set without API calls."
    )
    parser.add_argument(
        "--source-run-root",
        type=Path,
        default=DEFAULT_SOURCE_RUN,
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--current-git-head", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = load_b2_phase_a_profile(args.profile)
    report = dry_render_real_b1_run_v1(
        source_run_root=args.source_run_root,
        output_root=args.output_root,
        profile=profile,
        current_git_head=args.current_git_head or _current_git_head(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
