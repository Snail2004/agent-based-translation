# TASK_EVAL_END_TO_END_RUNNER_C3_V1

Status: COMPLETE

## Objective

Provide one Evaluation-owned orchestration entry point that turns a validated
`CommonEvaluationInputV1` plus sealed `EvaluationRunConfigV1` into a persisted
`FullRunReportV1` without choosing a provider, model, credential, prompt, retry
or scoring policy.

```text
common input + sealed config + pre-materialized source/translation artifacts
  -> local SF-QE batch when configured
  -> SF-BT/PJ through an injected sealed shared-backend role runner
  -> EvaluationExecutionArtifactV1 + immutable execution manifest
  -> EvaluationUsageArtifactV1
  -> reports/full_run_report_v1.json (final commit marker)
```

## Authority boundaries

- the runner controls ordering, preflight, persistence, resume and exact joins;
- the local SF-QE runtime supplies its already-approved predictor and pinned
  checkpoint/package/device/batch facts;
- `SharedEvaluationRoleRunnerV1` supplies already-resolved LLM roles;
- the shared backend remains the only owner of physical LLM attempt transport,
  quota, cache and usage evidence;
- report presentations and artifact paths are explicit caller facts;
- the comparative claim remains `INCONCLUSIVE` until a separate claim policy
  is frozen.

## Resume semantics

1. A valid existing FullRunReportV1 is revalidated and reused with zero scorer
   calls.
2. A committed execution with no report is never recomputed; persisted local
   evidence and the shared ledger are used to finish usage/report publication.
3. Local SF-QE evidence committed before execution can be reused after a crash.
4. Shared LLM attempt rows without a committed execution halt fail-closed. The
   runner does not risk duplicate provider calls or invent hidden recovery.
5. A committed execution missing its local SF-QE evidence halts; it is never
   silently rescored.

## Preflight

- input and translation artifact paths must exist inside the Evaluation run
  root before any scorer call;
- arm and method presentations exact-cover the sealed input/config;
- baseline/candidate identities are either both absent or both valid and
  distinct;
- ready SF-BT jobs require an injected LLM role runner and attempt ledger;
- PJ rows that are mechanically equal remain code-only and require no LLM;
- all foreign config/input/profile/report reuse fails closed.

## Hard exclusions

- no API/network call in this milestone;
- no provider/model/key/base-URL default or fallback;
- no source/translation mutation or producer callback;
- no gold/oracle/threshold/recommendation in scorer inputs;
- no public report/common-input/shared-core schema change;
- no App, D2L, Literary, Input Normalization, held scorer or CLI edit.

## Acceptance

- fresh fake-transport run persists local evidence, execution, usage and report;
- complete rerun performs zero local or shared semantic calls;
- committed execution resumes without repeating models;
- missing artifacts and partial uncommitted LLM attempts halt before scoring;
- mechanical PJ ties execute with zero LLM runtime;
- usage and report preserve unknown cost as `null`;
- focused/adjacent/full tests pass with API/network/credential count 0.

## Verification

- runner adversarial probes: `9 passed`;
- adjacent execution/local-SF-QE/usage/report chain: `69 passed`;
- complete `pipeline/tests`: `1303 passed in 245.59s`;
- frozen DB source and temporary read-only copy remained byte-identical before
  and after at
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- temporary DB copy removed;
- API/network/credential calls: 0;
- source/runtime DB writes: 0.
