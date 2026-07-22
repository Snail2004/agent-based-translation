# TASK_EVAL_USAGE_PROJECTION_C2D_V1

Status: COMPLETE

## Objective

Persist one report-ready `EvaluationUsageArtifactV1` from exact local SF-QE
metering and shared-backend attempt evidence. App and report consumers relay the
persisted projection; they never rescan SQLite or recalculate usage.

## Authority boundary

- local COMET evidence supplies local invocation count, model and duration;
- `SharedLlmAttemptLedger` supplies physical LLM attempts, tokens, cache facts,
  quota bucket, observed model and cost facts;
- unknown provider facts remain `null`;
- no tariff, credential family, hidden retry or provider identity is inferred;
- semantic method status comes from sealed Evaluation execution;
- no score, winner, threshold or recommendation is computed here.

## Stage map

- `sf_qe.local_scorer` -> `sf_qe`;
- `sf_bt.back_translation` -> `sf_bt`;
- `sf_bt.semantic_judge` -> `sf_bt`;
- `pj.judge` -> `pj`.

The projector validates each selected shared seal with the shared contract and
filters by exact Evaluation logical run and attempt. Foreign-run ledger rows are
not included. Cache-only reuse is recorded as zero new provider attempts;
missing evidence remains unavailable rather than guessed.

## Non-goals

- no API/network or credential load;
- no shared-core, App, report-schema or producer-runtime edit;
- no DB mutation except reading the dedicated shared attempt ledger;
- no gold/reference/oracle data in runtime bindings.

## Verification

- 40 focused and adjacent Evaluation tests passed with the pytest cache disabled;
- shared fake-transport evidence projects 18 physical requests and 936 tokens;
- missing cost remains `null` and makes aggregate usage `partial`;
- foreign run/attempt rows do not enter the projection;
- PJ mechanical ties are `not_applicable` rather than fabricated model calls;
- canonical self-hash, create-only persistence, unknown-key, non-finite and
  source-binding tamper probes pass;
- API/network/credential calls: 0.
