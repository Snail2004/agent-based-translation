from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.standalone_pack_preflight_v1 import preflight_d2l_evaluation_zip_v1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preflight a standalone Evaluation handoff")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight-pack")
    preflight.add_argument("--input-zip", type=Path, required=True)
    preflight.add_argument("--chapter-id", action="append", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "preflight-pack":
        report = preflight_d2l_evaluation_zip_v1(
            args.input_zip,
            expected_chapter_ids=args.chapter_id,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
