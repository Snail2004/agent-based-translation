# TASK_APP_E09 — Console: bridge injection policy → consistency (the thesis payoff, gap C)

**Owner:** CodeX (implement) · **Gate:** Claude (independent verify on real artifacts)
**Value:** HIGHEST remaining thesis-value item. Tells the core causal chain
"memory injection ⟹ term consistency" that the two existing panels (pack shows
*injected*, metrics show *TC=1.0*) currently leave disconnected.
**Cost:** LOW–MEDIUM. **No re-run, no re-score** — all data already exists in
`score_run_final.json`.

## The story to tell (verified on real run `run_e79867ab0ec9`)
`D_registry_consistency.{S0,S1}` already carries per-term + per-tier consistency.
Real numbers from this run:
- **S0 (naive, no injection):** hard/mandatory **7/9** consistent. Two HARD terms
  drift:
  - `Automatic Speech Recognition` → "nhận dạng **giọng nói** tự động" ×1 vs
    "nhận dạng **tiếng nói** tự động" ×1
  - `optimization algorithms` → "các thuật toán tối ưu" ×1 vs "thuật toán tối ưu **hóa**" ×1
- **S1 (memory injected):** hard/mandatory **9/9** — those exact two terms are now
  consistent (injected as mandatory). The only remaining S1 drift is
  `models` at the **soft** tier ("các mô hình" ×6 vs "mô hình" ×2), which is
  policy-allowed ("do not force").

That is the payoff: **injection fixed exactly the terms that drifted without it**,
and the residual S1 "drift" is an intentionally-permitted soft deviation.

## Data source (verified — do NOT invent fields)
`reports/score_run_final.json` → `D_registry_consistency.<config>`:
- `by_tier`: `{ hard|soft|preserve|entity|ignore_for_consistency: { overall,
  terms, consistent_terms, drift_terms, undetected_terms } }`
- `overall`, `consistent_terms`, `terms`
- `terms_all`: list of `{ source_term, target_term, constraint_strength (=tier),
  status ("consistent"|"drift"|"undetected"), forms_used ({surface: count}),
  source_blocks }`

`ignore_for_consistency` terms are excluded from the metric by design (e.g.
`example`, `set`) — treat them as NOT part of the memory story (exclude from the
"drift/fixed" highlights; may show dimmed as "not counted" or omit).

## Backend (routes/thesis_runs.py — the only wiring gap)
`_build_report_summary` currently exposes only scalar metrics; add a
`consistency` projection so the Console can read it via the existing
`report-summary` poll (no app.jsx change needed — it stores the whole object).

Add `summary["consistency"]` when `final` present and any config has
`D_registry_consistency`:
```
"consistency": {
  "present": true,
  "configs": [...present configs...],
  "overall": { "<cfg>": <overall float> , ... },
  "by_tier": { "<cfg>": <the by_tier dict> , ... },
  "notable_terms": [  # joined across arms by source_term; story-relevant only
    {
      "source_term": ...,
      "tier": <constraint_strength>,        # from S1 if present else S0
      "by_config": { "<cfg>": { "status":..., "forms": {surface:count},
                                "target_term":... } , ... },
      "fixed_by_injection": <bool>          # drift/undetected in S0 AND consistent in S1
                                            # AND tier in {hard,soft,preserve} (injected)
    }, ...
  ]
}
```
Rules:
- Build `notable_terms` from the union of terms whose `status != "consistent"` in
  ANY present config, **excluding** `constraint_strength == "ignore_for_consistency"`.
  Join S0/S1 entries by `source_term`.
- `forms` = the term's `forms_used` map (surface → count) verbatim.
- `fixed_by_injection` true iff S0 exists and that term is non-consistent in S0 but
  consistent in S1 (an injected tier). This is the payoff flag.
- **Bound size:** cap `notable_terms` (e.g. ≤ 50) — always keep all
  `fixed_by_injection` and all drift terms first; `by_tier`/`overall` are small and
  always included. Consistent terms are represented by tier counts only (do not list
  each).
- Single-arm run (S1 only): `overall`/`by_tier` for S1, `notable_terms` may be empty
  and `fixed_by_injection` is always false (needs S0). Must not error.

## Frontend (console.jsx — new panel)
Add a panel, styled per console (`section-label`, `kv-row`, `watch-row`,
`kv-good/warn/bad/dim`), e.g. `:: consistency (memory ⟹ render)`, shown when
`reportSummary.consistency?.present`:
1. **Per-tier headline, S0→S1 contrast** (from `by_tier`): one row per injected tier
   (hard, soft, preserve; skip entity if 0). Show `S0 c/t → S1 c/t`. Color S1 by
   consistency (kv-good if all consistent). Example row:
   `mandatory (hard)   7/9 → 9/9 ✓  (+2 memory-fixed)`.
   If only one arm, show that arm alone.
2. **Fixed-by-injection list** (`notable_terms` where `fixed_by_injection`): name the
   term + S0 competing forms (drift) vs S1 form (consistent), e.g.
   `optimization algorithms — S0: "…tối ưu" / "…tối ưu hóa"  →  S1: "…tối ưu hóa" ✓`.
   This is the headline proof; make it visually primary (kv-good).
3. **Residual drift** (`notable_terms` non-consistent in S1): name term + tier + forms.
   Mark **soft**-tier drift as allowed: `models [soft] — lệch được phép (do-not-force)`
   in a neutral/warn tone, NOT bad.
4. Empty/degraded states: no `consistency` → hide panel; single-arm → show tiers,
   hide the S0→S1 contrast and fixed list.

## Acceptance (verify on REAL data, not just green tests)
1. Backend unit/route test on a fixture with S0+S1 `terms_all` (mirror the real
   shape): `report-summary` returns `consistency.by_tier`, `consistency.overall`,
   and `notable_terms` with a `fixed_by_injection:true` entry; `ignore_for_consistency`
   terms are excluded; single-arm fixture doesn't error and yields no fixed entries.
2. Live on `run_e79867ab0ec9` (already scored): the panel shows
   **mandatory 7/9 → 9/9**, lists **`optimization algorithms`** and
   **`Automatic Speech Recognition`** as memory-fixed (with their S0 competing forms
   vs the single S1 form), and shows **`models` [soft]** as an allowed residual drift.
   `example`/`set` (ignore tier) do NOT appear as failures.
3. Reviewer (Claude) will recompute these counts from `score_run_final.json` and
   confirm the panel matches (no invented numbers), via the natural selection path.
4. Backend `pytest app/backend/tests/test_thesis_runs.py -q` stays green; frontend
   compiles clean (no Babel errors).

## Notes for the reviewer (Claude)
- Verify the S0→S1 tier counts and the fixed-by-injection set against
  `D_registry_consistency` on disk; do not trust the report.
- Confirm on the natural selection path (not synthetic `<select>` events — E06 lesson).
- Keep the payoff honest: soft-tier residual drift is *by design*, frame it as allowed,
  not as a consistency failure ([[memory-injection-precision-cost]],
  [[translator-pack-one-form-anchor]]).
