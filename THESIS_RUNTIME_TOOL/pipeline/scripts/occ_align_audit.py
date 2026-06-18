from __future__ import annotations

import argparse
import json

from pipeline.eval.occ_align import audit_occ_align


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit occurrence-alignment proposals against human gold.")
    parser.add_argument("--proposals", nargs="+", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    report = audit_occ_align(args.proposals, args.gold, out_path=args.out)
    print(json.dumps(report.get("gate", {}), ensure_ascii=False, indent=2, sort_keys=True))
    print(f"Report written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
