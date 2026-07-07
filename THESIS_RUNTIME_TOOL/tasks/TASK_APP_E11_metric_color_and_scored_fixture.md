# TASK_APP_E11 — per-metric PASS/WARN color (gap F) + scored console_dev fixture (gap D)

**Owner:** CodeX (implement) · **Gate:** Claude (independent verify on real artifacts)
**Value:** Polish. F makes the RESULTS panel readable at a glance; D lets
`console_dev.html` demo the headline/gap/consistency/watchlist panels with no backend.
**Cost:** LOW. Two small, independent parts — may land as one commit.

---

## Part F — per-metric color, honestly

### Problem
`report-summary` metrics carry `status: null` (`_score_run_metrics` in
routes/thesis_runs.py), so the RESULTS panel shows values with no PASS/WARN tint;
only the overall verdict has a state. The real `stage_gate` invariants (booleans) are
computed but never surfaced.

### Constraint — do NOT invent absolute thresholds
`score_run_final.json` has **no** per-metric numeric gate; `stage_gate` is boolean
invariants only. Absolute cutoffs on TC/TA are dangerous here: e.g. TA's strict-metric
floor (~0.63) is a known measurement artifact, not a failure
([[gold-is-style-guide-tiered-recall]]). So color **relatively**, not by magic numbers.

### Fix
1. **Relative per-metric status (compare runs)** — backend `_score_run_metrics`:
   for each metric that has both an S0 and S1 value (pair by stripping the `_S0`/`_S1`
   suffix — TC, TA, and any others), set the **S1** metric's `status`:
   - `"good"` if `S1 >= S0 - eps` (memory did not hurt; eps ~1e-6)
   - `"warn"` if `S1 < S0` (a real regression vs baseline — e.g. TA's injection
     precision cost)
   Leave `status: null` for the S0 rows and for single-arm runs (no principled signal
   → no color, do not fabricate). Real check on `run_e79867ab0ec9`:
   TC_S1 1.0 ≥ TC_S0 0.778 → good; TA_S1 0.705 < TA_S0 0.747 → warn.
2. **Surface the real gate** — add `stage_gate` (or a compact `{passed, total, all_ok}`
   digest of its booleans) into the report-summary `final` block, so the panel can show
   the genuine invariant gate rather than only the overall verdict.
3. **Frontend (console.jsx RESULTS panel):** tint each metric row by `metric.status`
   (`kv-good`/`kv-warn`, null → current neutral). Add a small gates line, e.g.
   `gates  5/5 ✓` (kv-good if all_ok, else kv-bad naming the failed invariant).
   Keep the existing gap TC/TA and verdict rows.

### F acceptance
- On `run_e79867ab0ec9`: TC_S1 tinted good, TA_S1 tinted warn (regression vs S0),
  gates shows 5/5 ✓. Single-arm fixture/run: metrics stay neutral (no fabricated color).
- Backend tests updated; `status` values asserted for a compare fixture and a
  single-arm fixture. `pytest app/backend/tests/test_thesis_runs.py -q` green.

---

## Part D — scored console_dev fixture

### Problem
`console_dev.html` loads only events, block_preview, and watchlist from
`fixtures/one_button_preface_golden/` and passes **no `reportSummary`** — so RESULTS,
gap TC/TA, and the new consistency panel are empty in the dev harness. It also
**flattens** watchlist to `{term, vi}` (loadFixture, ~lines 33-41), which strips the
fields E10's watchlist-depth panel needs. The fixture has no `score_run_final.json`.

### Fix
1. **Add `fixtures/one_button_preface_golden/report_summary.json`** in the exact shape
   the API returns (`{phase_1, final, compare, consistency}` incl. the F additions).
   Derive it from a real scored S0+S1 run so it is representative, not fabricated:
   capture `GET /api/thesis/runs/run_e79867ab0ec9/report-summary` (or build from that
   run's `score_run_final.json`). Mark provenance in the fixture README as
   "illustrative / representative real numbers".
2. **console_dev.html:** fetch `report_summary.json` in `loadFixture` and pass it as
   `reportSummary={data.reportSummary}` to `AgentConsoleView`. Stop flattening the
   watchlist — pass the **raw entries** (with `audit_label`, `candidates`, etc.) so the
   E10 depth panel renders in the harness too (keep a guard for the string-only shape).
3. Optionally also drop the source `reports/score_run_final.json` into the fixture for
   completeness, but the console consumes `report_summary.json`, so that is the
   required artifact.

### D acceptance
- Opening `console_dev.html` (static, no backend) shows: RESULTS with TC/TA values +
  F coloring, gap TC/TA, the consistency panel (mandatory 7/9 → 9/9, memory-fixed
  terms, residual soft drift), and the E10 watchlist depth — all from the fixture.
- No console/Babel errors; frozen hash unchanged (no pipeline run).

---

## Sequencing note
E11's console.jsx RESULTS/gates tinting is independent of E10's watchlist panel, but D
depends on **E10** landing (so the dev fixture exercises the depth panel) and on **F**
(so `report_summary.json` includes metric `status` + gate digest). Implement E11 after
E10; regenerate `report_summary.json` after F so the fixture carries the new fields.

## Reviewer notes (Claude)
- Verify F coloring is relative (S1 vs S0), never an absolute cutoff; confirm single-arm
  stays neutral. Recompute TC/TA S0-vs-S1 from `score_run_final.json`.
- Verify D by opening `console_dev.html` via the preview server and checking the panels
  render from the fixture (natural load, not synthetic events — E06 lesson); confirm the
  fixture `report_summary.json` matches a real run's projection.
- Keep the honesty line: TA_S1 < TA_S0 is the expected injection precision cost
  ([[memory-injection-precision-cost]]), shown as warn (not failure); it is not a bug.
