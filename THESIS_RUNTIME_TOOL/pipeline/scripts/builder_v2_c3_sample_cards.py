#!/usr/bin/env python3
"""Stage C3: render review-only term cards for d2l_term_audit_v1.

This reproducer is intentionally 0-API and read-only. It uses the same card
builder as the C3 auditor driver so reviewers inspect the production schema,
not a hand-built preview.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.prepass.builder_v2_audit import build_card, load_notebook_entries


DEFAULT_TERMS = [
    "norm",
    "shape",
    "gradient",
    "one",
    "example",
    "arange",
    "linalg.norm",
    "circle",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notebook", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", default="-")
    parser.add_argument("--terms", nargs="*", default=DEFAULT_TERMS)
    args = parser.parse_args()

    entries = load_notebook_entries(Path(args.notebook))
    by_term = {
        str(entry.get("canonical_source_term") or "").casefold(): entry
        for entry in entries
    }
    conn = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cards, missing = [], []
        for term in args.terms:
            entry = by_term.get(term.casefold())
            if entry is None:
                missing.append(term)
                continue
            cards.append(build_card(cur, entry))
    finally:
        conn.close()

    output = json.dumps(cards, ensure_ascii=False, indent=1)
    if args.out == "-":
        print(output)
    else:
        Path(args.out).write_text(output + "\n", encoding="utf-8")
        print(f"wrote {len(cards)} cards -> {args.out}")
    if missing:
        print("ABSENT in notebook:", missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
