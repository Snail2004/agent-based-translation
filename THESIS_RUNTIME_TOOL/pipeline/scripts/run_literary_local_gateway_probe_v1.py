"""CLI for the sealed Literary Local GPT Gateway capability probe."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from pipeline.literary.local_gateway_capability_probe_v1 import (
    execute_local_gateway_probe_v1,
    prepare_local_gateway_probe_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "execute"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--credential-root", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, required=True)
    parser.add_argument("--git-head", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    common = {
        "output_root": args.output_root,
        "profile_path": args.profile,
        "credential_root": args.credential_root,
        "frozen_db": args.frozen_db,
        "current_git_head": args.git_head,
    }
    if args.mode == "prepare":
        result = prepare_local_gateway_probe_v1(**common)
    else:
        result = execute_local_gateway_probe_v1(**common)
    print(result.get("seal_hash") or result.get("report_hash"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
