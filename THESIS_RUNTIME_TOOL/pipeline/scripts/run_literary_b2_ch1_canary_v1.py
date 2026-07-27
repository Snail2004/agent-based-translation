from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Sequence

from pipeline.literary.b2_live_canary_v1 import (
    execute_b2_frame_live_v1,
    execute_b2_interactions_live_v1,
    prepare_b2_ch1_canary_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_canary_profile_v1.json"
)
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=RUNTIME_ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a sealed, non-publishing Literary B2 Ch1 canary."
    )
    parser.add_argument(
        "mode", choices=("prepare", "frame", "interactions", "run")
    )
    parser.add_argument("--source-run-root", type=Path)
    parser.add_argument("--prior-b2-root", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--credential-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    head = _git_head()
    if args.mode in {"prepare", "run"}:
        if args.source_run_root is None:
            raise SystemExit("--source-run-root is required for prepare/run")
        value = prepare_b2_ch1_canary_v1(
            source_run_root=args.source_run_root,
            output_root=args.output_root,
            canary_profile_path=args.profile,
            credential_root=args.credential_root,
            frozen_db=args.frozen_db,
            current_git_head=head,
            prior_b2_root=args.prior_b2_root,
        )
        print(
            json.dumps(
                {
                    "status": "sealed",
                    "seal_hash": value["seal_hash"],
                    "chapter_id": value["chapter_id"],
                    "certification_eligible": value["certification_eligible"],
                    "conservative_total_token_reserve": value["limits"][
                        "conservative_total_token_reserve"
                    ],
                },
                indent=2,
            )
        )
    if args.mode in {"frame", "run"}:
        value = execute_b2_frame_live_v1(
            output_root=args.output_root,
            credential_root=args.credential_root,
            frozen_db=args.frozen_db,
            current_git_head=head,
        )
        print(
            json.dumps(
                {
                    "status": "frame_accepted",
                    "artifact_hash": value["artifact_hash"],
                    "frame_segments": len(value["frame_segments"]),
                    "review_requests": len(value["review_requests"]),
                },
                indent=2,
            )
        )
    if args.mode in {"interactions", "run"}:
        value = execute_b2_interactions_live_v1(
            output_root=args.output_root,
            credential_root=args.credential_root,
            frozen_db=args.frozen_db,
            current_git_head=head,
        )
        print(
            json.dumps(
                {
                    "status": "complete",
                    "artifact_hash": value["artifact_hash"],
                    "speaker_turns": len(value["speaker_turns"]),
                    "interaction_events": len(value["interaction_events"]),
                    "review_requests": len(value["review_requests"]),
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
