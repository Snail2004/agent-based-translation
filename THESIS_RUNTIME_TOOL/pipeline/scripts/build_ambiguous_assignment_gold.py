from __future__ import annotations

import argparse
import json

from pipeline.eval.ambiguous_assignment import build_gold_stub


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build human-label CSV for EV-D2L-09 ambiguous assignment probe."
    )
    parser.add_argument("--report", required=True, help="EV-08 metrics report JSON.")
    parser.add_argument("--db", required=True, help="Frozen memory SQLite DB.")
    parser.add_argument("--experiment", default="d2l_p3", help="Experiment id.")
    parser.add_argument("--n", type=int, default=80, help="Ambiguous rows to sample.")
    parser.add_argument("--n-control", type=int, default=20, help="Control rows to sample.")
    parser.add_argument("--out", required=True, help="Output CSV path.")
    args = parser.parse_args()

    summary = build_gold_stub(
        db_path=args.db,
        report_path=args.report,
        experiment_id=args.experiment,
        n=args.n,
        n_control=args.n_control,
        out_path=args.out,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
