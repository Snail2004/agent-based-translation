# TASK_EVAL_FIVE_CHAPTER_END_TO_END_V1

Status: OFFLINE IMPLEMENTATION COMPLETE; REAL INPUT PREFLIGHT PENDING

Owner: Evaluation workstream

## 1. Narrow benchmark scope

This task implements the user-approved, narrow D2L benchmark over exactly five
contiguous source chapters:

1. `d2l_preliminaries`
2. `d2l_linear_networks`
3. `d2l_multilayer_perceptrons`
4. `d2l_deep_learning_computation`
5. `d2l_convolutional_neural_networks`

The required arms are exactly `S0`, `S1`, `community`, `google_nmt`, and
`llm_lc`. The existing GPT-5.6 Web whole-book Markdown is retained byte-for-byte
as evidence; only blocks belonging to these five chapters enter scoring.

This is a narrow experiment. The Web one-shot arm remains a diagnostic
long-context baseline and does not silently alter the locked headline benchmark
policy in `TASK_EVAL_BASELINES_ALIGNMENT_V1.md`.

## 2. Authority boundary

Evaluation never becomes a translation producer. Imported Google, Web, and
community material is represented by `EvaluationBenchmarkArmOverlayV1`, an
evaluation-only, read-only projection with
`public_translation_artifact=false`. Original evidence bytes remain immutable.

No gold, score, threshold, verdict, or recommendation may flow back into D2L,
Literary, memory, retrieval, or translation runtime.

## 3. Source and evidence binding

Every chapter keeps its own honest source artifact and runtime-manifest hash.
The implementation must not invent one aggregate runtime manifest merely because
the five chapters share the same frozen source DB.

Every source chapter is bound by:

- exact project/document/chapter identity;
- frozen source DB hash;
- its own runtime-manifest hash;
- exact source artifact hash;
- content-addressed source read-model hash;
- exact block IDs and source order.

## 4. Mandatory preflight gate

No scorer may run until all 25 arm/chapter cells pass exact cover.

For each source row, alignment is checked independently from translation
quality. An exact marker/ID with non-empty payload is `aligned`; missing,
foreign, reordered, failed, or `review_held` rows block scoring. A baseline that
modifies a source row admitted as `preserve` remains aligned but receives the
non-blocking `preserve_violation` finding. That finding remains visible for
structural-quality reporting; it must not be misclassified as an alignment
failure merely to remove a weak baseline from comparison.

Duplicate, foreign, reordered, missing, or source-drifted block IDs fail closed.
The preflight report preserves per-arm/per-chapter counts and bounded samples of
non-ready block IDs. It never shrinks the denominator silently.

The community repository may be present while alignment is still
`review_held`; this is reported as an explicit blocker, not promoted to Human
truth.

## 5. Runner lifecycle

The second implementation milestone adds:

- deterministic stage schedule;
- immutable accepted role-call checkpoints;
- per-job checkpoints;
- explicit halt records for failed physical/semantic calls;
- resume under the exact same semantic contract;
- no repeated accepted call after restart;
- model/prompt/schema/validator/generation settings locked for one run;
- physical credential redistribution permitted only as a newly sealed attempt
  under the unchanged semantic contract;
- append-only status events plus a current status projection for later Console
  polling.

Changing model or semantic profile starts a new run. There is no silent
provider/model/key rotation, fallback, sample replacement, or scorer retry.

## 6. Scoring

Common methods remain `SF-QE`, `SF-BT`, and `PJ`. The D2L technical profile may
also report `TC-Occ` and `TA-Occ`. Existing scorer prompts, validators, method
versions, aggregation, and `FullRunReportV1` remain unchanged by the import gate.

## 7. Current expected state

The five Google captures and the full GPT-5.6 Web file are available. The D2L
five-chapter S0/S1 producer package is still an upstream dependency. Community
translation files exist, but each chapter must pass the accepted alignment gate.
Therefore the first real preflight is expected to be `blocked` until those
dependencies arrive. A truthful blocked report is a successful gate result.

## 8. Implemented coordinator boundary

`benchmark_runner_v1` accepts the sealed benchmark manifest, its preflight,
the exact overlay artifacts referenced by that preflight, and one chapter
runtime for each locked chapter. A ready run rejects any mismatch between the
25 approved overlays and the translations actually supplied to a scorer.

The parent run persists:

- an immutable run manifest;
- append-only, hash-chained lifecycle events;
- one immutable checkpoint per completed chapter;
- an atomic current-status projection for polling;
- stable child roots that let completed chapter and accepted role-call work be
  reused after interruption;
- a final `EvaluationBenchmarkRunReportV1` only after all five chapter reports
  and executions validate.

On reopen, the coordinator audits checkpoint order and self-hashes. A completed
parent report also reopens every referenced child report and execution, checks
root containment, and verifies their sealed hashes and run bindings before it
is reused.

For the DEC-057 Evaluation component boundary, the same runner can now receive
an optional `EvaluationWorkflowRunContextV1`. When supplied, it writes an
Evaluation-owned replay package at the benchmark output root:

- `component_manifest.json` plus immutable `manifest_revisions/`;
- immutable `event_records/` and the Console-readable `events.jsonl` projection;
- `artifact_index.json` with physical hashes and parent references;
- `scoring_receipt.json` echoing the exact accepted five-arm handoff;
- content-addressed `checkpoints/` for chapter interruption and Resume.

The component package keeps `component_run_id` stable across Resume and
increments only its own `component_attempt_id`. It never writes the parent
workflow manifest, parent event sequence, or neutral relay files. A package
cannot be retrofitted onto a benchmark that already started without this
recorder; the runner fails closed instead of fabricating replay history.

## 9. Aggregation policy

Benchmark aggregation reuses validated per-job observations. Numerators and
denominators are summed across all five chapters before division; chapter means
are never averaged. A source job may contribute to only one aggregate row for
its method/version. Method-version drift across chapters fails closed.

No cross-method composite or headline winner policy is defined in this task.
The benchmark report therefore publishes the per-method measurements and an
explicit `INCONCLUSIVE` claim with `no_cross_method_composite`.

## 10. Offline verification state

The fixture gate covers:

- denominator-weighted five-chapter aggregation;
- blocked preflight with zero scorer calls;
- interruption in chapter three followed by reuse of chapters one and two;
- cross-chapter scoring-contract drift before any scorer call;
- tampered parent report rejection;
- preflight-approved overlay versus scoring-runtime mismatch;
- a validly resealed child execution that differs from its parent reference;
- malformed checkpoint ordinal rejection during resume.
- a complete five-chapter Evaluation component package with exact handoff
  receipt echo, contiguous component event hash-chain, and artifact hashes;
- component-level halt checkpoint creation and Resume with stable component
  identity and a second manifest revision;
- tampered `events.jsonl` rejection against immutable event records.

No live scorer call is authorized or needed to complete this implementation
milestone. Real scoring remains gated by an exact-cover five-chapter producer
package and accepted community alignment.
