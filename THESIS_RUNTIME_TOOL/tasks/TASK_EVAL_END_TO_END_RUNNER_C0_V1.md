# TASK_EVAL_END_TO_END_RUNNER_C0_V1

Status: COMPLETE - 0 API

Owner: Evaluation workstream

## Objective

Build the first common semantic execution control plane after the existing
offline planner. The milestone must prove this deterministic path:

```text
CommonEvaluationInputV1 + EvaluationRunConfigV1
    -> EvaluationPlanV1
    -> one blinded EvaluationScorerInputPacketV1 per ready job
    -> injected method executor
    -> closed job observations
    -> denominator-aware method aggregates
    -> sealed EvaluationExecutionArtifactV1
```

This is the runner backbone, not a live evaluation. It does not allocate or
call an API, load a local model, produce FullRunReportV1, or publish a BETTER /
NOT_BETTER thesis claim.

## Locked scope

The runner:

1. reuses the existing common input validator, planner and packet builder;
2. accepts only the established common methods `sf_qe`, `sf_bt`, and `pj`;
3. sends only the already blinded scorer packet to an injected executor;
4. records blocked, succeeded and failed jobs without silently shrinking the
   denominator;
5. validates all semantic outputs with a closed method-specific shape;
6. aggregates unary scores by arm and PJ verdicts by the arm occupying the
   opaque candidate slot;
7. reports paired wins/ties/losses only over units with observations from both
   compared arms;
8. emits an explicit `INCONCLUSIVE` claim because the final claim policy and
   thresholds have not been frozen;
9. returns a canonical, self-hashed, detached artifact without mutating input.

## Executor boundary

The executor is pipeline-owned semantic machinery. It receives exactly the
validated blinded scorer packet. It never receives `EvaluationJobV1`, arm IDs,
baseline/candidate roles or the unblinding map.

It returns one closed observation:

- `succeeded` with a method-specific semantic output; or
- `failed` with a stable error code and no semantic output.

For future live execution:

- SF-QE uses the approved local COMET-QE adapter;
- SF-BT back-translation, SF-BT semantic judgment and PJ use the existing thin
  Evaluation adapters over `SharedLlmBackend.execute_one_attempt()`;
- any semantic retry is a newly sealed logical request;
- no provider/model/key/fallback/retry decision belongs in this runner.

## Aggregate semantics

### Unary (`sf_qe`, `sf_bt`)

- one expected observation per selected arm and eligible unit;
- arm value is the arithmetic mean of successful finite scores in `[0, 100]`;
- numerator is the sum of observed scores;
- denominator is the successful observation count;
- expected, observed and missing counts remain explicit;
- pairwise wins/ties/losses use only unit IDs observed for both the declared
  baseline and candidate.

### Pairwise (`pj`)

- `candidate_1` and `candidate_2` are mapped back through the plan's
  counterbalanced presentation, never through model-visible arm names;
- arm value is its overall win count;
- ties and missing judgments remain explicit;
- the candidate-view comparison reports wins, ties and losses over successful
  PJ jobs only.

## Hard exclusions

- no API/network/credential access;
- no provider or model hard-code;
- no DB/cache/checkpoint/App/Console mutation;
- no gold, oracle, human reference or expected-answer authority;
- no TC-Occ / TA-Occ calculation;
- no new prompt or scorer policy;
- no FullRunReportV1 or public contract change;
- no edit to held scorer/CLI files or `pipeline/eval/__init__.py`.

## Acceptance

- deterministic bytes for identical input and executor observations;
- unknown keys, non-finite/range-invalid scores and malformed PJ verdicts fail
  closed;
- blocked and failed jobs increase missing counts rather than disappearing;
- counterbalanced PJ outcomes map to the correct underlying arm;
- one-arm runs publish no fabricated comparison;
- caller input/config/executor output objects remain unmodified;
- focused and full `pipeline/tests` pass with 0 API calls.

## Result

- new sealed artifact: `EvaluationExecutionArtifactV1`;
- supported methods: `sf_qe`, `sf_bt`, and one or more declared `pj` pairs;
- injected executor sees only the blinded scorer packet, never the plan row or
  arm-role map;
- comparative claim remains `INCONCLUSIVE / claim_policy_not_frozen`;
- runner adversarial probes: `11 passed`;
- related Evaluation runner/packet/prompt/adapter/report tests: `147 passed`;
- full `pipeline/tests`: `1245 passed in 174.83s`;
- frozen DB source and temporary read-only mount stayed byte-identical at
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
  the temporary mount was removed;
- API/network/credential access: `0`; DB/cache/checkpoint/App mutation: `0`.

## Next milestone

Build method executors around the established local SF-QE path and the shared
LLM adapters, then add crash-safe persistence and FullRunReportV1 composition.
Concrete models, quota buckets and final comparative claim policy remain
separate approval gates.
