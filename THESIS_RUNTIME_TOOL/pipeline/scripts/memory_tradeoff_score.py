from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.memory_tradeoff import (
    read_human_json,
    read_jsonl,
    read_key_json,
    read_worksheet_jsonl,
    score_memory_tradeoff,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Score memory tradeoff judgments after the blind is complete.")
    parser.add_argument("--workdir", default="data/eval/memory_tradeoff")
    parser.add_argument("--out", default="data/reports/memory_tradeoff_59_overrides.json")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    worksheet_path = workdir / "worksheet.jsonl"
    key_path = workdir / "KEY" / "worksheet_KEY.json"
    gemini_path = workdir / "judgments_gemini.jsonl"
    human_path = workdir / "judgments_human.json"
    missing = [
        str(path)
        for path in [worksheet_path, gemini_path, human_path, key_path]
        if not path.exists()
    ]
    if missing:
        raise SystemExit("blind not complete: missing " + ", ".join(missing))

    report = score_memory_tradeoff(
        worksheet_rows=read_worksheet_jsonl(worksheet_path),
        key_rows=read_key_json(key_path),
        gemini_rows=read_jsonl(gemini_path),
        human_rows=read_human_json(human_path),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "n": report["n"], "summary": report["summary"], "iaa": report["iaa"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
