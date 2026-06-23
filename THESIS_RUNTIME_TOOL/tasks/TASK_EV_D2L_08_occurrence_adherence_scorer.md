# TASK EV-D2L-08 — Occurrence-level adherence scorer (B-gold + A-registry), highlight + UI mirror metric

Status: READY (REWORK v2 after CodeX review — Claude revised sec 1-4) -> CodeX fills sec 5 + REVIEW + STOP (NO commit) -> Claude reviews sec 6 + commits.
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
<!-- CodeX: engine (source+target joint allocation, active/shadow owners, pre-allocation collision detect, min-cap, lower/upper band), B+A via same engine, re-derived denominator (+cause if !=3133), legacy replicate 3133, audit-from-same-allocation, APP-D01 consumer+tests updated, frozen DB hash, suite. Then REVIEW + STOP. -->

## 6. Review (Claude)
<!-- Claude: verify LOGIC not target numbers — legacy replicates 0.7405/0.8024; corrected re-derived (whatever it is) with lower/upper band; shadow blocks set on "tập dữ liệu" even when dataset absent; collision lower=0; A fixed via same engine; APP-D01 headline reads new key + app tests updated; highlight uses same allocation; DB hash; suite. -->
