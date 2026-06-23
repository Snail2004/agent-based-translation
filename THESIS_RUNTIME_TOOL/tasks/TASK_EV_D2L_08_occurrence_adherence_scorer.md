# TASK EV-D2L-08 — Occurrence-level adherence scorer (B-gold + A-registry), highlight + UI mirror metric

Status: REVIEW (CodeX implementation complete; NO commit/push) -> Claude reviews sec 6 + commits.
Type: SCORER VALIDITY FIX + consumer update. eval-only, deterministic, NO LLM.

## 1. Why
`B_tar_vs_gold.occurrence_weighted` (and `A_tar_vs_registry`, SAME `_score_pairs` at line 387) is mislabelled: `ok = any(_has_vi(...))` (boolean presence) then `weighted_adherent += occurrences_in_block` (source count). Source x3 + form present once => 3/3. Block-level any-match x source weight, not occurrence matching. Both B (0.7405/0.8024) and A (0.9299) are inflated. The app headline reads exactly this key.

Probe (replicates headline S0 0.740504 / S1 0.802426): a count + cross-term variant gave ~S0 0.686 / S1 0.735 — **a PROBE, not a target** (it used per-term source counting + no shadow owners; the real metric below may differ).

## 2. Metric contract (implement exactly — supersedes the probe)
Adherence = fraction of registry SOURCE occurrences rendered with an accepted VN form. A COUNTING problem, not positional alignment.

1. **Source joint allocation** → `source_count[term]`. Allocate ALL ruler source forms over the source block (`allocate_spans(language="en")`, longest-span + non-overlap) so nested terms ("learning" inside "machine learning") do not double count.
2. **Target joint allocation** over the translation (`allocate_spans(language="vi")`, PyVi segmentation + longest-span + non-overlap), owners =
   - **active owners**: accepted VN forms of terms whose SOURCE is present in the block — these can EARN credit;
   - **shadow owners**: every other ruler form (esp. longer ones, e.g. dataset→"tập dữ liệu") — participate ONLY to block subspan-stealing, NEVER earn credit.
   `active_target_count[term]` = `len(spans)` owned by that term's active forms.
3. **Collision detection BEFORE allocation** (because `allocate_spans` silently tie-breaks by owner_id): a target form string that is an accepted form of >=2 active terms in the block is a collision. Flag the occurrences; do NOT let the arbitrary tie-break decide a headline.
4. `confirmed_credit[term] = min(source_count, active_target_count)` (collision forms excluded from confirmed).
5. **Denominator = total source occurrences, FIXED, re-derived from step 1's joint allocation.** Do NOT force it to 3133. (Legacy per-term denominator must still replicate 3133 — see sec 4 — but the corrected joint denominator may legitimately differ; if so, report the cause, e.g. nested-term de-duplication.)
6. **Headline = lower bound**: `adherence_lower = sum(confirmed_credit) / denominator` (collision credit = 0). **Upper bound** = `adherence_lower + residual_capacity/denominator` (residual capacity = collision occurrences that COULD adhere). Report both; resolved-only (post-tie-break) numbers are diagnostic, never the headline.
7. **Same engine for BOTH metrics**: emit `B_gold_occurrence_adherence` (S0, S1) and `A_registry_occurrence_adherence` (S1; S0 optional for symmetry).

## 3. Highlight + UI mirror the metric (one source of truth)
- Audit HTML/CSV highlights source occurrences + allocated target spans from the EXACT `allocate_spans` calls in sec 2 (active vs shadow vs collision marked distinctly). No separate per-term highlight path.
- **Update the consumer (APP-D01 read-model)**: headline reads `*_occurrence_adherence` (lower bound) + shows `metric_version` + upper-bound band; legacy `occurrence_weighted` stays viewable but NOT labelled headline; update app tests that currently assert headline == old occurrence_weighted (e.g. `== 0.7639`). Otherwise the fix never reaches the user.

## 4. Re-score, transparency, locks
- Re-score 4 chapters (d2l_p3, DB d2l_p1, gold variants `data/eval/d2l_gold_variants.csv`). Keep old `occurrence_weighted` under its key for transparency; add corrected keys; bump `metric_version`.
- **Replicate guard**: legacy `occurrence_weighted` must still print S0 0.740504 / S1 0.802426 (proves inputs unchanged). The corrected numbers are whatever the correct logic yields — verify the LOGIC (sec 2), do NOT tune toward 0.686/0.735.
- L1: eval-only; no re-translate; frozen DB SHA-256 first16 = DA0F687894090D43 unchanged; no LLM; deterministic.
- L2: do not mutate frozen runtime memory; gold/registry unchanged except the already-applied membership fix.
- L3: CodeX fills sec 5, sets REVIEW, STOPS. No commit/push. Claude reviews sec 6 + commits.
- L4: tests — ellipsis (src2/tgt1->1), repeated (src2/tgt2->2), substring ("tập" not in "tập trung"), drift ("bộ"->0), shadow/expansion ("tập dữ liệu"->dataset shadow blocks set->0 EVEN WHEN dataset source absent), collision (two active terms share a form -> lower=0, upper=+capacity), A and B both via the engine, app-test headline reads new key. Full `pipeline/tests app/backend/tests` pass.

Note: collision is NOT the only case needing audit — generic short forms can also false-match independently; the audit view must make all such spans inspectable.

## 5. Implementation (CodeX)

### 5.1 Implementation

- Added `pipeline/eval/occurrence_adherence.py` as the shared deterministic
  engine for B-gold and A-registry.
- Source-side denominator is produced by one EN joint allocation across all
  ruler terms. Target-side allocation uses active, shadow, and collision owners
  in one VI allocation. A shadow owner can block a shorter active form but can
  never earn credit.
- Equal normalized forms owned by two or more active terms become collision
  owners before allocation. Lower-bound credit is zero for those spans. Upper
  bound uses a deterministic capacity-limited bipartite matching against the
  remaining source slots, so one ambiguous span cannot credit two terms.
- Corrected credit is `min(source_count, active_target_count)` per term. The
  denominator remains the jointly allocated source occurrence count.
- `d2l_translate_score.py` now emits:
  - `B_gold_occurrence_adherence` for S0/S1, flat and recurring;
  - `A_registry_occurrence_adherence` for S1;
  - the unchanged legacy `B_tar_vs_gold` and `A_tar_vs_registry` sections;
  - one CSV/HTML audit generated from the exact allocations used by both new
    metrics.
- Bumped metric version to `d2l_translate_score_v3_occurrence`.
- APP-D01 reads corrected lower bound as the B/A headline, exposes the upper
  bound, residual capacity, method, denominator, and legacy value. Per-chapter
  B now also reads corrected lower/upper values, with legacy fallback for old
  reports.
- CLI summary prints corrected ranges and legacy values side by side.

### 5.2 Production re-score (0 API)

Command:

```powershell
python -m pipeline.scripts.score_run `
  --db data\jobs\d2l_p1\memory.sqlite3 `
  --experiment d2l_p3 `
  --chapters introduction preliminaries linear_networks multilayer_perceptrons `
  --profile technical_d2l_v1 `
  --gold-variants data\eval\d2l_gold_variants.csv `
  --out data\reports\d2l_translation_metrics_v2.json
```

Observed runtime: `153.51s`. No LLM/API call and no re-translation.

| Metric | Legacy | Corrected lower | Corrected upper | Corrected denominator |
|---|---:|---:|---:|---:|
| B S0 | 0.740504 | 0.717223 | 0.717223 | 2967 |
| B S1 | 0.802426 | 0.771149 | 0.771149 | 2967 |
| A S1 | 0.929901 | 0.813351 | 0.918395 | 8149 |

- Corrected B gap: `+0.053926` (S1 remains above S0, but the prior scorer
  overstated both arms).
- Legacy B input guard is unchanged: both S0/S1 use `3133` legacy source
  occurrences and exactly reproduce `0.740504 / 0.802426`.
- Corrected B denominator is `2967`, not forced to `3133`: EN joint allocation
  removes `166` nested/overlapping source occurrences that the legacy per-term
  counting double-counted.
- B has no target-form collision in these four chapters, so lower=upper.
- A has `6628` confirmed occurrences and `856` residual capacity from ambiguous
  shared target forms; therefore it is honestly reported as the range
  `0.813351..0.918395`, not as one tie-broken headline.
- Audit artifacts contain `32,280` parsed CSV records and the report records the
  same count. CSV size is `9,993,001` bytes; HTML size is `12,437,808` bytes.
  Physical CSV line count is larger because quoted context cells contain
  embedded newlines; record parity was verified with `csv.DictReader`.

### 5.3 Locks and verification

- Frozen DB SHA-256 before and after production scoring:
  `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8`.
- Required edge cases are covered: repeated source/target counts, substring
  segmentation, drift, absent-source shadow owner, and active-form collision.
- Targeted scorer/read-model/run tests:

```text
49 passed in 21.35s
```

- Full offline suite:

```text
332 passed in 127.63s
```

- `git diff --check` reports no whitespace error (only Windows LF-to-CRLF
  notices). No commit or push performed.

## 6. Review (Claude) — VERDICT: PASS

Verified the LOGIC (not target numbers) by independently re-deriving the headline from the on-disk audit CSV (the SAME allocation that feeds the highlight, 32,280 records):
- **Legacy replicates** S0 0.740504 / S1 0.802426 → inputs unchanged.
- **B-gold re-derived from audit**: S0 = 2128/2967 = 0.717223, S1 = 2288/2967 = 0.771149, gap +0.0539. **A-registry**: 6628/8149 confirmed, 884 collision spans, lower 0.8134, upper (6628+856)/8149 = 0.9184. All match the report exactly.
- **Denominator NOT forced**: joint source allocation re-derives 2967 (gold) / 8149 (registry); legacy per-term still replicates 3133. The drop is legitimate nested-term de-duplication, as flagged in REWORK point 5.
- **min-cap works**: target active rows 2372 (gold S0) > credited 2128 → 244 surplus renderings correctly not credited.
- **Shadow + collision**: target shadow rows present (5354); collisions (884) excluded from the lower-bound headline; lower/upper band arithmetic verified.
- **Highlight mirrors metric**: `occurrence_audit.source = same_allocation_as_occurrence_adherence`; audit aggregates == headline → what the user sees highlighted == what is counted.
- **Consumer (APP-D01)**: headline value = `adherence_lower`, legacy `occurrence_weighted` kept for trace, `metric_version` surfaced, scorer_metric renamed; app tests updated.
- **Guards**: frozen DB SHA-256 first16 = DA0F687894090D43 unchanged; full `pipeline/tests app/backend/tests` → 332 passed (re-ran); no API, no re-translate.

All 5 REWORK points implemented (shadow owners, residual lower/upper, A fixed via same engine, consumer+tests updated, numbers re-derived not forced). Corrected 4-chapter headline: **B S0 0.717 / S1 0.771 (gap +0.054)**, **A S1 band [0.813, 0.918]**. Thesis claim S1>S0 survives. Still surface-anchored (registry synonyms = undetected by design). Audit CSV/HTML (22 MB, deterministic/regenerable) intentionally NOT committed; report JSON carries the summary + audit metadata.
