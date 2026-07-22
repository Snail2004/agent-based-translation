# TASK_EVAL_TERMINOLOGY_OCCURRENCE_MIGRATION_V1

Status: COMPLETE
Owner: Evaluation
Mode: 0-API, read-only inputs

## Objective

Move the existing D2L TC-Occ and TA-Occ measurement logic behind an
Evaluation-owned, deterministic interface. Preserve the measured semantics of
the historical scorer while separating it from D2L database loaders, runtime
state, and report scripts.

## Scope

This milestone may add only:

- `pipeline/eval/terminology_occurrence_v1.py`
- `pipeline/scripts/run_terminology_occurrence_v1.py`
- `pipeline/tests/test_evaluation_terminology_occurrence_v1.py`
- this task file
- content-addressed Evaluation evidence produced by the regression run

The existing mixed `d2l_translate_score.py`, D2L runtime/exporter, SQLite,
translation artifacts, `FullRunReportV1`, App UI, and Literary pipeline remain
unchanged.

## Metric semantics

### TC-Occ

For each term and arm, normalize every non-empty localized rendering, count
the most frequent rendering, then sum those maxima over terms:

`TC-Occ = sum(max rendering count per term) / localized occurrences`

`not_rendered` and unresolved localizations are excluded from this denominator
and disclosed separately. A tied maximum does not change the numerator; the
tie is retained in audit facts.

### TA-Occ

For each arm, count source occurrences classified as adherent to the declared
ruler:

`TA-Occ lower = adherent occurrences / all source occurrences`

Unresolved adherence remains non-credit in the conservative headline and also
produces a disclosed possible upper bound. `not_rendered` remains in the
denominator.

The first adapter preserves the historical D2L cascade interpretation:

- `t2_credit + rendered` is adherent;
- T3 `adherence_label=adherent` is adherent;
- `target_surface`, then `target_quote_clean`, then `target_quote` supplies the
  localized rendering;
- `not_rendered` is excluded only from TC-Occ, not TA-Occ.

## Authority and isolation

- This metric is `profile_scope=d2l`; Literary is not assigned a zero score.
- The D2L package and localization artifacts are read-only inputs.
- Gold/reference rulers, when introduced later, must be separate Evaluation
  artifacts. This milestone labels the embedded runtime-glossary ruler
  honestly and never calls it external correctness.
- No score, verdict, or override flows back to Builder, registry, retrieval,
  Translator, source package, or translation overlay.
- The scorer performs no API, model, DB, cache, or checkpoint operation.

## Required gates

1. Closed, immutable, content-addressed metrics artifact.
2. D2L package hash, arm IDs, source text, target text, block IDs, and source
   spans must match the supplied localization evidence.
3. Multiple arms must exact-cover the same source occurrence universe and use
   the same embedded accepted-form ruler.
4. Unknown keys, duplicate occurrences, foreign arms, source/target drift,
   non-finite values, and self-hash tampering fail closed.
5. FullRunReport-compatible metric projection is available, but this milestone
   does not change or write `FullRunReportV1`.
6. Regression against the existing MLP S0/S1 cascade artifacts must reproduce:
   - TC-Occ S0 `2145/2482`, S1 `2296/2485`;
   - TA-Occ S0 `1876/2487`, S1 `2173/2487`.

## Deferred

- Rebuilding occurrence localization directly from `D2LEvaluationInputV1`.
- External/community ruler adapter and fragment-filter contract.
- Automatic inclusion in `FullRunReportV1` and App rendering.
- Any D2L producer/exporter schema change.

## Completion evidence

- Implementation commit: `019ed62e9d32eeab27f1f52733c4d952086b5c81`.
- Focused scorer and D2L-input integration gate: `35 passed`.
- The committed MLP regression sidecar reproduces all four required
  numerator/denominator pairs and both historical deltas exactly.
- API calls: `0`; source/runtime/translation writes: `0`.
