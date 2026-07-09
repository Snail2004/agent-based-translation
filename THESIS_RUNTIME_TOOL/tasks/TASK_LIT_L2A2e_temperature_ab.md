# TASK_LIT_L2A2e — Builder temperature A/B (1.0 vs 0.2), single variable  [rev3]

Status: SPEC (rev3 — CodeX minors folded in + decision-logic incoherence fixed). Measure whether
lowering Builder temperature tightens run-to-run reproducibility and/or reduces retries WITHOUT hurting
recall. Do NOT commit. Single variable = temperature only.

## Rationale (neutral)
`llm_prepass.yaml` runs temp=1.0; extraction spec was 0.2, Translator LOCKED 0.3+none. gpt-5.4-mini
accepts custom temp only at reasoning_effort=none (Builder is already none). Hypothesis, stated
NEUTRALLY: temperature 1.0 is a CANDIDATE cause of run-to-run variation and schema-slip retries; this
A/B tests it. The numbers decide — do not pre-label it the cause.

## Single variable + config
Change ONLY temperature. Keep identical: gpt-5.4-mini, reasoning_effort=none, seed=20260612, current
prompt+validator (post FIX-4), window 500/8, WH ch1. New file `pipeline/configs/llm_prepass_temp02.yaml`
= copy of llm_prepass.yaml with temperature=0.2, passed via --config. Do NOT edit the default; do NOT
add a bypass_cache CLI flag (code change breaks single-variable).

## Runs — 3 per arm, INTERLEAVED, separate cache file each
Exactly 3 runs/arm (2 gives only one pairwise gap). Interleave to balance backend/time drift:
temp10_r1 → temp02_r1 → temp10_r2 → temp02_r2 → temp10_r3 → temp02_r3.
Fresh calls via a SEPARATE --cache file per run (temp10_r1.sqlite3 … temp02_r3.sqlite3), NOT
bypass_cache. Freshness check = local cache_hits=0. Do NOT require outputs to differ — identical output
at temp=0.2 is a GOOD (reproducible) result; only a local replay is invalid, and cache_hits=0 rules
that out. Report local cache_hits SEPARATELY from provider cached_tokens (different things).
Budget: ~$0.19 expected; total cap $0.30; abort if any single run exceeds $0.06.

## Provenance per run (drift guard)
Log system_fingerprint, returned model id, prompt hash, builder_pilot.py version/hash, config, AND the
B0 cast_on_stage + seeded_cast (temperature hits B0 too; variance can originate there and cascade).
system_fingerprint=None is ALLOWED — record "unavailable", do not treat as failure. If fingerprint is
present AND changes between arms → mark the result confounded (backend changed) and do not adopt.

## Normalization for all set keys (define once, apply everywhere)
norm(s) = NFC → casefold → trim + collapse internal whitespace → map curly quotes/apostrophes
(“ ” ‘ ’) to straight (" '). Do NOT drop words. Keys:
- mentions: (block_id, surface_norm)
- turns: (block_id, quote_norm)
- events: (block_id, evidence_norm)
candidate_entity_ids compared as a SET (order-independent).

## Metrics
PRIMARY — reproducibility by MEMBERSHIP (18 turns here ≠ the same 18 turns there). Within each arm,
mean pairwise Jaccard over the 3 runs on the THREE extraction axes (mentions/turns/events).
GUARD (separate, NOT part of the 2/3 rule) — resolution agreement: on turns shared across a run pair,
fraction with identical speaker+addressee resolution_status AND candidate_entity_ids (set-equal).
SECONDARY (supporting column) — count spread (min/max per axis across the 3 runs).
RETRY/COST — first-pass success = (#outputs ok at attempt 1)/total; retry count; cost; tokens; the
normalize/drop counters.
RECALL (NOT from raw counts — extra items may be false-positives):
- consensus set per arm = items in ≥2/3 runs of that arm.
- critical cases: sir×3, dog b018, narrator.
- audit every item appearing in only ONE arm (real evidence vs noise).

## DECISION (a tree, not "all-thresholds" — rev2 was self-contradictory: seed-dominates made Jaccard
## flat → failed an "all hold" gate even when retry improved. Reconciled here.)

**Gate 0 — recall safety (mandatory, blocks adoption regardless of everything else):**
temp0.2 critical cases resolve correctly in 100% of its 3 runs, AND temp0.2 consensus set keeps ≥95%
of temp1.0's consensus set on EACH of mentions/turns/events. (temp1.0 critical cases are a comparator
only — reported honestly; temp1.0 failing a case that temp0.2 passes is EVIDENCE FOR 0.2, not a
disqualifier.) If Gate 0 fails → keep 1.0, stop.

**Then read the Jaccard to pick the scenario:**
- (a) temp0.2 clearly more reproducible: mean pairwise Jaccard across the 3 extraction axes is higher
  at 0.2 by ≥0.03, with ≥2/3 axes up and NO axis down by >0.02 → ADOPT 0.2 (reproducibility win).
- (b) both arms reproduce ~equally (seed dominates, Jaccard ≈ tie): temperature is NOT the variance
  driver; the historical spread was prompt/code-change driven. Adopt 0.2 ONLY if it independently cuts
  retries (median retry/run lower by ≥1 OR first-pass success +≥5 percentage points). Else keep 1.0.
- (c) both arms vary: seed weak here; adopt 0.2 only if it meets the (a) Jaccard margin; else keep 1.0.

**Guards on whichever adoption path (all must hold to actually switch the default):**
median cost increase ≤10%; frozen d2l hash 64D989…C715 unchanged; fingerprint not changed mid-run.
Adoption = update llm_prepass.yaml to 0.2 (reproducibility posture: low temp + fixed seed).

## Interpretation & caveats
seed=20260612 is best-effort; a NULL result (temperature exonerated, scenario b with no retry gain) is
valid and reportable — do not force temp into being the culprit. n=3 gives NO formal significance; the
0.03 / 95% / 5pp figures are pre-registered heuristics, not a significance test. Single chapter, ~15
outputs/run, small item counts → Jaccard is noisy → conclusion is DIRECTIONAL. If the decision lands
within ~one threshold's noise of the line, escalate to more chapters/runs before locking — do NOT
round a borderline number up into a verdict. Single variable only; don't touch window/prompt/validator/
FIX-4; don't commit.
