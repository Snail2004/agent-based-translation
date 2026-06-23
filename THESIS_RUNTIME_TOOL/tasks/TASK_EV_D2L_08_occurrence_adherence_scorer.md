# TASK EV-D2L-08 — Occurrence-level adherence scorer (count + joint cross-term), highlight mirrors metric

Status: READY (Claude wrote sec 1-4) -> CodeX fills sec 5 + REVIEW + STOP (NO commit) -> Claude reviews sec 6 + commits.
Type: SCORER VALIDITY FIX (headline B metric). eval-only, deterministic, NO LLM.

## 1. Why
`B_tar_vs_gold.occurrence_weighted` in `d2l_translate_score.py` is mislabelled: `_score_pairs` does `ok = any(_has_vi(...))` (boolean presence) then `weighted_adherent += occurrences_in_block` (source count) — so source x3 + target form present once => credits 3/3. It is block-level any-match x source weight, not occurrence matching.

Verified on committed artifacts (4 chapters d2l_p3, DB d2l_p1, gold variants `data/eval/d2l_gold_variants.csv`), replicating the headline exactly (S0 occ_wt 0.740504, S1 0.802426):
- Correct count + joint cross-term -> **S0 0.6856, S1 0.7354**; inflation ~5.5pp (S0) / 6.7pp (S1); gap +0.0619 -> +0.0498 (~20% artifact, asymmetrically favouring S1). Claim survives, smaller.
- (Note: `_count_non_overlapping_forms` counts BOOLEAN per form, not occurrences — must be fixed to `len(spans)`.)

## 2. The metric algorithm (implement exactly)
Adherence = "what fraction of registry source occurrences were rendered with an accepted Vietnamese form." It is a COUNTING problem, not positional alignment.

Per scope block:
1. **Source count (denominator).** Joint-allocate ALL present terms' SOURCE forms over the source block (`allocate_spans(..., language="en")`, longest-span + non-overlap) so overlapping source terms (e.g. "machine learning" vs "learning") do not double count. `source_count[term]` = number of source spans the term owns.
2. **Target count (numerator pieces) — JOINT cross-term.** Build owners from ALL accepted VN forms (canonical + variants) of ALL terms present in the block, in ONE `allocate_spans(..., language="vi")` pass:
   - PyVi segmentation so a form matches only at word boundaries ("tập" NOT inside "tập trung"/"bài tập");
   - longest-span wins + non-overlap so "tập dữ liệu" is owned by *dataset*, not *set*.
   `target_count[term]` = number of spans owned by that term's forms (`len(spans)`, NOT boolean).
3. **Credit = `min(source_count, target_count)`** per (block, term). (Caps at source; handles ellipsis source2/target1 -> 1, and over-rendering.)
4. **Aggregate.** `adherence = sum(credit) / sum(source_count)` over all (block, term). Denominator must equal total source occurrences (the current `occurrences` = 3133 for the 4 chapters).
5. **Short-form residual (report separately, do NOT hide in headline).** When a single target span's form is an accepted form of >=2 present terms (collision) so allocation must assign it arbitrarily, flag those occurrences: report (a) their count, (b) the headline computed with and without them, so the uncertainty band is explicit. These are the only cases where positional alignment could help; they must be visible, not silently credited.

## 3. Highlight mirrors the metric (one source of truth)
Emit an audit HTML/CSV that highlights, per block, the SOURCE occurrences and the allocated TARGET spans **from the exact same `allocate_spans` calls used in sec 2** (same joint cross-term owners). What the user sees highlighted must equal what is counted. Do NOT add a separate per-term highlighting path. Mark residual/collision spans distinctly.

## 4. Re-score + transparency + locks
- Re-score the 4 chapters; KEEP the old `occurrence_weighted` in the report under its current key for transparency; add the corrected metric under a new key (e.g. `occurrence_adherence`); bump `metric_version`.
- Replicate guard: old `occurrence_weighted` must still print S0 0.740504 / S1 0.802426 (proves inputs unchanged); corrected must reproduce **S0 ~0.6856 / S1 ~0.7354**, gap ~0.0498, denom 3133.
- L1: eval-only; no re-translate; frozen DB SHA-256 first16 = DA0F687894090D43 unchanged; no LLM; deterministic.
- L2: gold/registry unchanged except already-applied membership fix; do NOT mutate frozen runtime memory.
- L3: update any doc/LEDGER text that cites 0.74/0.80 as adherence headline -> point to corrected metric (note both).
- L4: CodeX fills sec 5, sets REVIEW, STOPS. No commit/push. Claude reviews sec 6 + commits.
- L5: add targeted tests: ellipsis (src2/tgt1 -> 1), repeated (src2/tgt2 -> 2), substring ("tập" not matched in "tập trung"), drift ("bộ" -> 0), expansion/cross-term ("tập dữ liệu" -> dataset owns, set 0), collision residual reported. Full `pipeline/tests app/backend/tests` pass.

## 5. Implementation (CodeX)
<!-- CodeX: new scorer fn, source+target joint allocation, len(spans) count, min-cap, residual band, audit-from-same-allocation, re-scored numbers (old replicate + new), tests, frozen DB hash. Then REVIEW + STOP. -->

## 6. Review (Claude)
<!-- Claude: re-derive on disk — old occ_wt replicates 0.7405/0.8024; corrected ~0.6856/0.7354 gap ~0.050 denom 3133; confirm joint cross-term (set gets 0 on "tập dữ liệu"); confirm highlight uses same allocation; residual band reported; DB hash; run suite. -->
