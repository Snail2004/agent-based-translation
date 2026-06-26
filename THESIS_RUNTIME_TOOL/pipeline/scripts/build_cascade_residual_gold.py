from __future__ import annotations

import argparse
import json

from pipeline.eval.cascade_localize import build_residual_gold


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build EV-D2L-10 residual gold worksheet from cascade reports."
    )
    parser.add_argument("--from", dest="from_paths", nargs="+", required=True)
    parser.add_argument("--reuse", nargs="*", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    report = build_residual_gold(
        report_paths=args.from_paths,
        reuse_paths=args.reuse,
        out_path=args.out,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
