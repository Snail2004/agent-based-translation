# TASK_EVAL_LOCAL_SF_QE_C2C_V1

Status: COMPLETE

## Objective

Replace the unversioned callable placeholder at the SF-QE boundary with a
batch-prepared, content-addressed local scoring adapter for
`Unbabel/wmt22-cometkiwi-da`.

This is a 0-API milestone. It neither downloads nor loads a checkpoint during
tests. A real COMET loader remains an injected local dependency.

## Locked behavior

1. The model-facing batch contains only `src` and `mt` strings.
2. Ready SF-QE jobs are prepared once in canonical plan order.
3. Model identity, checkpoint hash, package version, device and batch size are
   explicit persisted facts.
4. Native COMET scores are validated in `[0, 1]` and retained.
5. Report scores use the named, invertible transform
   `comet_native_0_1_times_100_v1`; no clamp, threshold or winner policy is
   applied.
6. The prepared callable accepts only the next exact source/target hash pair.
   Missing, extra or reordered use fails closed.
7. Evidence is canonical JSON, self-hashed and immutable create-only.

## Non-goals

- no provider/API call or credential access;
- no model download;
- no DB or source-package write;
- no gold, oracle, human reference, prior score or report verdict in model
  input;
- no change to `FullRunReportV1`, `D2LEvaluationInputV1`, App or shared LLM
  core;
- no import or edit of the held historical SQLite scorer CLI.

## Gate

- batch payload and score-transform tests;
- NaN/out-of-range/count mismatch rejection;
- plan-order and exact-cover rejection;
- closed schema, self-hash and immutable persistence probes;
- related Evaluation regression and full `pipeline/tests` gate.

## Verification

- focused local adapter: `10 passed`;
- adapter + method executor + execution runner: `33 passed`;
- Python compilation and `git diff --check`: passed;
- live API/network/credential calls: `0`;
- model downloads and local checkpoint loads: `0`;
- DB, source package, translation artifact and App mutations: `0`.

The full `pipeline/tests` gate is executed at the enclosing end-to-end
milestone so the same frozen-DB mount covers the remaining usage and runner
work once rather than repeatedly copying the database.
