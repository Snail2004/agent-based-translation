from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Sequence

from pipeline.literary.identity_reconciled_b1_snapshot_v1 import (
    materialize_identity_reconciled_b1_snapshot_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]


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
        description="Materialize a B2-readable B1 snapshot after Identity audit recovery."
    )
    parser.add_argument("--source-run-root", type=Path, required=True)
    parser.add_argument("--prepare-root", type=Path, required=True)
    parser.add_argument("--recovery-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = materialize_identity_reconciled_b1_snapshot_v1(
        source_run_root=args.source_run_root,
        prepare_root=args.prepare_root,
        recovery_root=args.recovery_root,
        output_root=args.output_root,
        current_git_head=_git_head(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
