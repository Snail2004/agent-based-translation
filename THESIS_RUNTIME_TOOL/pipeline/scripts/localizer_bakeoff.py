from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.eval.d2l_translate_score import (
    _load_translations,
    _resolve_chapters,
    _scope_blocks,
)
from pipeline.eval.localizer import (
    LOCALIZER_NAMES,
    build_localizer_gold,
    read_gold_csv,
    render_localizer_gold_html,
    run_localizers,
    score_localizer_bakeoff,
    input_fingerprints,
    validate_gold_occ_matches_scorer_rep_occ,
    validate_report_matches_inputs,
    write_bakeoff_report,
    write_gold_csv,
)
from pipeline.eval.memory_tradeoff import (
    DEFAULT_CHAPTERS,
    DEFAULT_DOC_ID,
    DEFAULT_EXPERIMENT_ID,
    DEFAULT_PROFILE,
    DEFAULT_SEED,
    build_judge_worksheet,
    build_override_set,
    render_worksheet_html,
    validate_dummy_not_real,
    write_key_json,
    write_override_csv,
    write_worksheet_jsonl,
)
from pipeline.eval.occ_align import make_simalign_aligner
from pipeline.translate.profiles import get_profile


DEFAULT_OVERRIDE = Path("data/eval/memory_tradeoff/KEY/override_set.csv")
FALLBACK_OVERRIDE = Path("data/eval/memory_tradeoff/override_set.csv")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and score EV-D2L-07a localizer bake-off artifacts.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build-gold", action="store_true")
    mode.add_argument("--score", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--render-gold-html", action="store_true")
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--report", default="data/reports/d2l_translation_metrics_v2.json")
    parser.add_argument("--override", default=str(DEFAULT_OVERRIDE))
    parser.add_argument("--gold", default="data/eval/localizer_gold.csv")
    parser.add_argument("--html", default="data/eval/localizer_gold.html")
    parser.add_argument("--out", default="data/reports/localizer_bakeoff.json")
    parser.add_argument("--out-dir", default="data/eval/memory_tradeoff")
    parser.add_argument("--simalign-cache-dir", default="data/eval/localizer_simalign_cache")
    parser.add_argument("--chapters", nargs="+", default=list(DEFAULT_CHAPTERS))
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--doc-id", default=DEFAULT_DOC_ID)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--localizer", choices=LOCALIZER_NAMES, default="longest_match")
    parser.add_argument("--with-simalign", action="store_true", help="Instantiate real SimAlign CPU aligner for scoring.")
    args = parser.parse_args()

    if args.build_gold:
        return _build_gold(args)
    if args.score:
        return _score(args)
    if args.render_gold_html:
        return _render_gold_html(args)
    return _apply(args)


def _build_gold(args: argparse.Namespace) -> int:
    override_path = _resolve_override_path(args.override)
    override_rows = _read_csv(override_path)
    blocks_by_id, translations = _load_context(
        db_path=args.db,
        chapters=args.chapters,
        experiment_id=args.experiment,
        profile_name=args.profile,
        doc_id=args.doc_id,
    )
    rows = build_localizer_gold(
        override_rows,
        blocks_by_id=blocks_by_id,
        translations=translations,
        include_edges=True,
    )
    write_gold_csv(args.gold, rows)
    Path(args.html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.html).write_text(render_localizer_gold_html(rows), encoding="utf-8")
    summary = {
        "mode": "build_gold",
        "gold": args.gold,
        "html": args.html,
        "override": str(override_path),
        "rows": len(rows),
        "prefilled": dict(Counter(row.prefilled for row in rows)),
        "registry_class": dict(Counter(row.registry_class for row in rows)),
        "note": "Rows with prefilled=human_required need human gold_target_start/gold_target_end before scoring.",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _render_gold_html(args: argparse.Namespace) -> int:
    rows = read_gold_csv(args.gold)
    Path(args.html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.html).write_text(render_localizer_gold_html(rows), encoding="utf-8")
    print(json.dumps({
        "mode": "render_gold_html",
        "gold": args.gold,
        "html": args.html,
        "rows": len(rows),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _score(args: argparse.Namespace) -> int:
    rows = read_gold_csv(args.gold)
    override_path = _resolve_override_path(args.override)
    reconciliation = validate_gold_occ_matches_scorer_rep_occ(rows, _read_csv(override_path))
    aligner_factory = (lambda: make_simalign_aligner(model="bert", device="cpu")) if args.with_simalign else None
    proposals = run_localizers(
        rows,
        aligner_factory=aligner_factory,
        simalign_cache_dir=args.simalign_cache_dir if args.with_simalign else None,
    )
    report = score_localizer_bakeoff(rows, proposals)
    report["input_fingerprints"] = input_fingerprints(
        gold_path=args.gold,
        override_path=override_path,
    )
    report["gold_occ_reconciliation"] = reconciliation
    report["simalign"] = {
        "real_aligner_used": bool(args.with_simalign),
        "cache_dir": args.simalign_cache_dir if args.with_simalign else None,
        "note": "Without --with-simalign, simalign rows are reported as missing; unit tests use a fake aligner.",
    }
    write_bakeoff_report(args.out, report)
    print(json.dumps({
        "mode": "score",
        "gold": args.gold,
        "out": args.out,
        "recommendation": report.get("recommendation"),
        "simalign_real_aligner_used": bool(args.with_simalign),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _apply(args: argparse.Namespace) -> int:
    bakeoff_report = json.loads(Path(args.out).read_text(encoding="utf-8"))
    recommendation = str(bakeoff_report.get("recommendation") or "")
    if not recommendation:
        raise ValueError("bakeoff report has no eligible localizer recommendation; rerun/fix --score before --apply")
    if args.localizer != recommendation:
        raise ValueError(f"--localizer {args.localizer!r} does not match report recommendation {recommendation!r}")
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    override_path = _resolve_override_path(args.override)
    gold_rows = read_gold_csv(args.gold)
    validate_report_matches_inputs(
        bakeoff_report,
        gold_path=args.gold,
        override_path=override_path,
        gold_rows=gold_rows,
        override_rows=_read_csv(override_path),
    )
    items = build_override_set(
        report,
        args.db,
        chapters=args.chapters,
        experiment_id=args.experiment,
        profile_name=args.profile,
        doc_id=args.doc_id,
        localizer_name=args.localizer,
        gold_rows=gold_rows,
    )
    rows, key_rows = build_judge_worksheet(items, seed=args.seed)
    validate_dummy_not_real(rows)

    out_dir = Path(args.out_dir)
    key_dir = out_dir / "KEY"
    out_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    write_override_csv(out_dir / "override_set.csv", items)
    write_override_csv(key_dir / "override_set.csv", items)
    write_worksheet_jsonl(out_dir / "worksheet.jsonl", rows)
    (out_dir / "worksheet.html").write_text(render_worksheet_html(rows), encoding="utf-8")
    write_key_json(key_dir / "worksheet_KEY.json", key_rows, seed=args.seed)
    (key_dir / "README.md").write_text(
        "DO NOT OPEN until judgments_human.json and judgments_gemini.jsonl are complete.\n"
        "Opening worksheet_KEY.json reveals S0/S1 provenance and breaks the blind.\n",
        encoding="utf-8",
    )
    unresolved = [item for item in items if not item.rep_resolved]
    summary = {
        "mode": "apply",
        "localizer": args.localizer,
        "override_items": len(items),
        "resolved_items": len(rows),
        "unresolved_items": len(unresolved),
        "unresolved_reasons": dict(Counter(item.skip_reason for item in unresolved)),
        "out_dir": str(out_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _load_context(
    *,
    db_path: str,
    chapters: list[str],
    experiment_id: str,
    profile_name: str,
    doc_id: str,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        profile = get_profile(profile_name)
        resolved = _resolve_chapters(conn, doc_id, chapters)
        blocks = _scope_blocks(conn, doc_id, resolved, profile)
        translations = {
            config: _load_translations(conn, experiment_id, config)
            for config in ("S0", "S1")
        }
    finally:
        conn.close()
    return {block.block_id: block.text for block in blocks}, translations


def _resolve_override_path(value: str) -> Path:
    requested = Path(value)
    if requested.exists():
        return requested
    if requested == DEFAULT_OVERRIDE and FALLBACK_OVERRIDE.exists():
        return FALLBACK_OVERRIDE
    raise FileNotFoundError(f"override CSV not found: {requested}")


def _read_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


if __name__ == "__main__":
    raise SystemExit(main())
