from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.prepass.persist import build_memory


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist pre-pass memory and optionally freeze it.")
    parser.add_argument("--source", required=True, help="Stripped source document.json.")
    parser.add_argument("--prepass", required=True, help="Pre-pass artifact directory.")
    parser.add_argument("--db", required=True, help="Output memory SQLite path.")
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="Freeze T1-T4 memory tables after a successful build.",
    )
    parser.add_argument(
        "--report",
        default="data/reports/memory_build_pilot.json",
        help="Tracked build report path.",
    )
    args = parser.parse_args()

    report = build_memory(
        db_path=args.db,
        document_json_path=args.source,
        prepass_dir=args.prepass,
        freeze=args.freeze,
    )
    payload = report.to_json_dict()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
