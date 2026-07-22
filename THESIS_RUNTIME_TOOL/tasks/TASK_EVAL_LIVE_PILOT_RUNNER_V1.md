# TASK_EVAL_LIVE_PILOT_RUNNER_V1

Status: COMPLETE (0-API)

## Objective

Execute exactly the calibration jobs sealed by
`EvaluationLivePilotPreflightV1` without invoking the full-plan runner or
publishing a headline score.

## Scope

- Validate the preflight against the exact common input and run config.
- Reconstruct every selected scorer packet and require its hash to match the
  preflight.
- Invoke an injected Evaluation job executor only for the selected jobs, in
  canonical plan order.
- Validate every method observation using the same SF-QE, SF-BT, and PJ output
  contracts used by the full execution runner.
- Bind the result to project/document/config/input/plan/preflight identity and
  the logical run, attempt, concrete profile ID, and profile hash exposed by
  the executor that actually performs the calls. The caller cannot supply a
  different display profile for the pilot artifact.
- Bind the local SF-QE checkpoint/package/runtime, selected packet set, and
  native score set. Resealing a report after changing a checkpoint or SF-QE
  score cannot pass external binding validation.
- Persist no score authority: every pilot artifact is
  `calibration_only / INCONCLUSIVE / pilot_not_headline_evidence`.

## Non-goals

- No `FullRunReportV1`, winner claim, full-plan aggregate, App/UI wiring,
  source/translation/memory mutation, gold/reference access, or score callback.
- No provider/model/source selection, credential loading, retry, fallback,
  quota scheduling, cache policy, or transport implementation. The existing
  shared backend and `EvaluationMethodExecutorV1` remain the only live path.
- No API call in this phase. Live use remains closed until the user assigns one
  exact Gemini physical row and model mapping.

## Failure semantics

- Foreign/reordered/missing jobs, stale packet hashes, unknown keys, altered
  profile/run identity, nonfinite scores, invalid method outputs, and stale
  preflight/config/input bindings fail closed.
- A provider-backed method-level semantic failure is recorded as a failed
  selected job; it does not become a preference verdict and does not silently
  trigger a retry. Invalid, missing, reordered, or partially consumed local
  SF-QE evidence aborts the artifact because its exact-cover lineage is broken.
- Exceptions before artifact completion publish no pilot execution artifact.

## Resume semantics

The pilot does not introduce a second checkpoint format. Every provider-backed
stage already has an exact sealed request and content-addressed application
response cache in the shared backend. Re-running the same preflight with the
same run/profile/input identities reuses accepted stage outputs and performs
no duplicate provider call. A complete prepared local SF-QE batch can be reset
and reused in memory; a new process may recompute that pinned local batch, but
must reproduce and bind its packet/score hashes.

## Verification

- Pilot/COMET/provenance focused probes: 30 passed.
- Evaluation regression: 327 passed.
- Final full `pipeline/tests` regression: 1,340 passed in 336.55 seconds against a
  temporary read-only mirror of frozen DB SHA-256
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
  the mirror was removed and the source hash remained unchanged.
- Shared-backend integration fixture: 40 selected jobs, 48 provider attempts
  on the first pass, zero additional provider attempts on exact replay, 48
  usage rows, and 96 cache observations across the two passes.
- Accepted D2L MLP package, read-only fake-transport run: 40/40 jobs succeeded;
  46 provider attempts and 46 usage rows. PJ used 14 attempts because one of
  the eight selected source units was a mechanical S0/S1 tie. The result
  remained `calibration_only / INCONCLUSIVE`.
- API calls, credential reads, source/translation/memory writes, report winner
  claims, and runtime callbacks: zero.
