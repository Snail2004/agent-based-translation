from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pipeline.eval.localizer import read_gold_csv, run_localizers, score_localizer_bakeoff
from pipeline.eval.localizer_cascade import legacy_longest_failures, load_dev_cases


REPORT_SPECS = [
    (
        "committed baseline v2",
        "data/reports/localizer_cascade_dev.json",
        "existing v2 report; re-scored on corrected gold",
    ),
    (
        "none/temp1 v2",
        "data/reports/localizer_cascade_dev_reasoning_none_temp1.json",
        "prior temp1 diagnostic; not reproducibility default",
    ),
    (
        "low/temp1/max4096 v2",
        "data/reports/localizer_cascade_dev_reasoning_low_temp1_max4096.json",
        "prior rejected reasoning diagnostic",
    ),
    (
        "NEW v3",
        "data/reports/localizer_cascade_dev_v3.json",
        "prompt v3 + code-owned offsets; DEV diagnostic only",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare T2 localizer reports on corrected DEV gold.")
    parser.add_argument("--gold", default="data/eval/localizer_gold.csv")
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--out", default="data/reports/localizer_t2_v3_comparison.json")
    parser.add_argument("--html", default="data/reports/localizer_t2_v3_comparison.html")
    args = parser.parse_args()

    rows, cases = load_dev_cases(args.gold, args.db)
    gold_rows = read_gold_csv(args.gold)
    legacy_ids = set(legacy_longest_failures(rows))
    pilot_cases = [case for case in cases if case.row_id in legacy_ids]
    case_by_row = {case.row_id: case for case in pilot_cases}

    shared_guard = _shared_gold_guard(gold_rows, rows, args.db)
    reports = []
    report_by_label: dict[str, dict[str, Any]] = {}
    for label, path, note in REPORT_SPECS:
        report_path = Path(path)
        if not report_path.exists():
            reports.append({"label": label, "path": path, "missing": True, "note": note})
            continue
        data = json.loads(report_path.read_text(encoding="utf-8"))
        scored = _score_report(label, path, note, data, case_by_row)
        reports.append(scored)
        report_by_label[label] = scored

    comparison = {
        "dataset_role": "DEV diagnostic, not held-out generalization",
        "gold": {
            "path": args.gold,
            "rows": len(gold_rows),
            "membership_fix": _membership_fix_status(gold_rows),
        },
        "frozen_db": _db_hash(args.db),
        "shared_gold_guard": shared_guard,
        "reports": reports,
        "per_case_diff_v3_vs_committed_baseline": _per_case_diff(
            Path("data/reports/localizer_cascade_dev.json"),
            Path("data/reports/localizer_cascade_dev_v3.json"),
            case_by_row,
        ),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html = Path(args.html)
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text(_render_html(comparison), encoding="utf-8")
    print(json.dumps({
        "out": str(out),
        "html": str(html),
        "reports": [
            {
                "label": item["label"],
                "exact": item.get("exact_on_corrected_gold"),
                "cost_usd": item.get("cost_usd"),
                "missing": item.get("missing", False),
            }
            for item in reports
        ],
        "shared_gold_guard": shared_guard,
        "frozen_db": comparison["frozen_db"],
    }, ensure_ascii=False, indent=2))
    return 0


def _score_report(
    label: str,
    path: str,
    note: str,
    report: dict[str, Any],
    case_by_row: dict[str, Any],
) -> dict[str, Any]:
    decisions = {str(item["row_id"]): item for item in report.get("decisions", [])}
    exact = 0
    per_case = []
    for row_id, case in case_by_row.items():
        decision = decisions.get(row_id)
        ok = bool(decision and decision.get("start") == case.gold_start and decision.get("end") == case.gold_end)
        exact += int(ok)
        per_case.append({
            "row_id": row_id,
            "term": case.source_term,
            "gold_span": case.gold_quote,
            "quote": decision.get("quote") if decision else None,
            "start": decision.get("start") if decision else None,
            "end": decision.get("end") if decision else None,
            "offset_source": decision.get("offset_source") if decision else None,
            "correct": ok,
        })
    model = _model_block(report)
    return {
        "label": label,
        "path": path,
        "prompt_version": report.get("prompt_version"),
        "reasoning": report.get("reasoning_effort") or model.get("reasoning_effort"),
        "temp": report.get("temperature") if report.get("temperature") is not None else model.get("temperature"),
        "max_out": report.get("max_output_tokens") or model.get("max_output_tokens"),
        "exact_on_corrected_gold": {
            "exact": exact,
            "n": len(case_by_row),
            "accuracy": exact / len(case_by_row) if case_by_row else None,
        },
        "cost_usd": _cost(report),
        "note": note,
        "per_case": per_case,
    }


def _model_block(report: dict[str, Any]) -> dict[str, Any]:
    preflight = report.get("preflight") if isinstance(report.get("preflight"), dict) else {}
    model = preflight.get("model") if isinstance(preflight.get("model"), dict) else {}
    return dict(model)


def _cost(report: dict[str, Any]) -> float | None:
    for key in ("usage_actual_api_cache_total", "usage", "usage_this_run_excludes_result_cache_hits"):
        usage = report.get(key)
        if isinstance(usage, dict) and usage.get("cost_usd") is not None:
            return float(usage["cost_usd"])
    return None


def _per_case_diff(baseline_path: Path, v3_path: Path, case_by_row: dict[str, Any]) -> list[dict[str, Any]]:
    if not baseline_path.exists() or not v3_path.exists():
        return []
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    v3 = json.loads(v3_path.read_text(encoding="utf-8"))
    baseline_decisions = {str(item["row_id"]): item for item in baseline.get("decisions", [])}
    v3_decisions = {str(item["row_id"]): item for item in v3.get("decisions", [])}
    result = []
    for row_id, case in case_by_row.items():
        base = baseline_decisions.get(row_id, {})
        new = v3_decisions.get(row_id, {})
        result.append({
            "row_id": row_id,
            "term": case.source_term,
            "gold_span": case.gold_quote,
            "baseline_quote": base.get("quote"),
            "baseline_correct": bool(base.get("start") == case.gold_start and base.get("end") == case.gold_end),
            "v3_quote": new.get("quote"),
            "v3_offset_source": new.get("offset_source"),
            "v3_correct": bool(new.get("start") == case.gold_start and new.get("end") == case.gold_end),
        })
    return result


def _shared_gold_guard(gold_rows: list[dict[str, Any]], cascade_rows: list[dict[str, Any]], db_path: str) -> dict[str, Any]:
    proposals = run_localizers(gold_rows)
    bakeoff = score_localizer_bakeoff(gold_rows, proposals)
    longest = bakeoff["metricA"]["longest_match"]
    legacy = legacy_longest_failures(cascade_rows)
    return {
        "recommendation": bakeoff["recommendation"],
        "longest_exact": longest["metricA_exact"],
        "longest_n": longest["metricA_n"],
        "longest_accuracy": longest["metricA_accuracy"],
        "legacy_longest_failures": len(legacy),
        "legacy_longest_failure_rows": legacy,
        "pass": (
            bakeoff["recommendation"] == "longest_match"
            and longest["metricA_exact"] == 108
            and longest["metricA_n"] == 116
            and len(legacy) == 8
        ),
    }


def _membership_fix_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row = next(item for item in rows if item["row_id"] == "mt_membership_99ef7e9bc5:S0")
    start = int(row["gold_target_start"])
    end = int(row["gold_target_end"])
    target_text = str(row["target_text"])
    return {
        "row_id": row["row_id"],
        "start": start,
        "end": end,
        "span": row["gold_target_span"],
        "slice": target_text[start:end],
        "pass": target_text[start:end] == "sự thuộc về",
    }


def _db_hash(path: str) -> dict[str, str]:
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return {"path": path, "sha256_first16": digest[:16].upper(), "sha256": digest}


def _render_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang="vi"><meta charset="utf-8">
<title>Localizer T2 v3 comparison</title>
<style>body{{font:14px system-ui;margin:24px;max-width:1100px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px 8px}}.bad{{color:#b42318}}.ok{{color:#087830}}pre{{white-space:pre-wrap}}</style>
<h1>Localizer T2 v3 comparison</h1>
<p>DEV diagnostic only; not held-out generalization.</p>
<div id="app"></div>
<script>
const payload={data};
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
const rows=payload.reports.map(r=>`<tr><td>${{esc(r.label)}}</td><td>${{esc(r.prompt_version)}}</td><td>${{esc(r.reasoning)}}</td><td>${{esc(r.temp)}}</td><td>${{esc(r.max_out)}}</td><td>${{r.exact_on_corrected_gold ? `${{r.exact_on_corrected_gold.exact}}/${{r.exact_on_corrected_gold.n}}` : 'missing'}}</td><td>${{esc(r.cost_usd)}}</td><td>${{esc(r.note)}}</td></tr>`).join('');
const diff=payload.per_case_diff_v3_vs_committed_baseline.map(r=>`<tr><td>${{esc(r.row_id)}}</td><td>${{esc(r.term)}}</td><td>${{esc(r.gold_span)}}</td><td class="${{r.baseline_correct?'ok':'bad'}}">${{esc(r.baseline_quote)}}</td><td class="${{r.v3_correct?'ok':'bad'}}">${{esc(r.v3_quote)}} / ${{esc(r.v3_offset_source)}}</td></tr>`).join('');
app.innerHTML=`<h2>Summary</h2><table><tr><th>label</th><th>prompt</th><th>reasoning</th><th>temp</th><th>max</th><th>exact</th><th>cost</th><th>note</th></tr>${{rows}}</table><h2>v3 vs baseline</h2><table><tr><th>row</th><th>term</th><th>gold</th><th>baseline</th><th>v3</th></tr>${{diff}}</table><h2>Guards</h2><pre>${{esc(JSON.stringify({{gold:payload.gold, frozen_db:payload.frozen_db, shared_gold_guard:payload.shared_gold_guard}}, null, 2))}}</pre>`;
</script></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
