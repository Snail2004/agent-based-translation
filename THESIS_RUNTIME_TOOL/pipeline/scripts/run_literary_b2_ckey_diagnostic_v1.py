from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Sequence

from pipeline.literary.b2_ckey_diagnostic_v1 import (
    execute_b2_ckey_diagnostic_v1,
    prepare_b2_ckey_diagnostic_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ckey_diagnostic_profile_v1.json"
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
        description="Run sealed CKEY structured-output diagnostics for Literary B2."
    )
    parser.add_argument("mode", choices=("prepare", "execute", "run"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--credential-root", type=Path, required=True)
    parser.add_argument("--full-load-request", type=Path)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    head = _git_head()
    if args.mode in {"prepare", "run"}:
        if args.full_load_request is None:
            raise SystemExit("--full-load-request is required for prepare/run")
        sealed = prepare_b2_ckey_diagnostic_v1(
            output_root=args.output_root,
            profile_path=args.profile,
            credential_root=args.credential_root,
            full_load_request_path=args.full_load_request,
            frozen_db=args.frozen_db,
            current_git_head=head,
        )
        print(
            json.dumps(
                {
                    "status": "sealed",
                    "seal_hash": sealed["seal_hash"],
                    "model_id": sealed["model_id"],
                    "quota_bucket_id": sealed["quota_bucket_id"],
                    "max_calls": sealed["limits"]["max_calls"],
                    "hard_visible_token_cap": sealed["limits"][
                        "hard_visible_token_cap"
                    ],
                },
                indent=2,
            )
        )
    if args.mode in {"execute", "run"}:
        report = execute_b2_ckey_diagnostic_v1(
            output_root=args.output_root,
            profile_path=args.profile,
            credential_root=args.credential_root,
            frozen_db=args.frozen_db,
            current_git_head=head,
        )
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
