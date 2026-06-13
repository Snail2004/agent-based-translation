from __future__ import annotations

import argparse
import json

from pipeline.eval.builder_gold import write_builder_vs_gold_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Score D2L Builder registry against eval-only glossary gold.")
    parser.add_argument("--db", required=True, help="Runtime memory SQLite path.")
    parser.add_argument("--chapters", nargs="+", required=True, help="Chapter suffixes or full ids.")
    parser.add_argument("--out", required=True, help="Output JSON report path.")
    parser.add_argument("--doc-id", default="d2l", help="Document id.")
    args = parser.parse_args()

    report = write_builder_vs_gold_report(
        args.db,
        doc_id=args.doc_id,
        chapters=args.chapters,
        out_path=args.out,
    )
    payload = report.to_json_dict()
    print(json.dumps({
        "doc_id": payload["doc_id"],
        "chapters": payload["chapters"],
        "gold_terms_present": payload["gold_terms_present"],
        "builder_terms": payload["builder_terms"],
        "matched_terms": payload["matched_terms"],
        "agreement_terms": payload["agreement_terms"],
        "recall": payload["recall"],
        "agreement": payload["agreement"],
        "missing_terms": len(payload["missing_terms"]),
        "conflicts": len(payload["conflicts"]),
        "extra_terms": len(payload["extra_terms"]),
        "report": args.out,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
