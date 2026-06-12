from __future__ import annotations

import argparse
import json

from pipeline.ingest.document_loader import load_document


def main() -> int:
    parser = argparse.ArgumentParser(description="Load stripped document.json into SQLite.")
    parser.add_argument("--source", required=True, help="Stripped document.json path.")
    parser.add_argument("--db", required=True, help="Runtime memory.sqlite3 path.")
    args = parser.parse_args()

    report = load_document(args.db, args.source)
    print(json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
