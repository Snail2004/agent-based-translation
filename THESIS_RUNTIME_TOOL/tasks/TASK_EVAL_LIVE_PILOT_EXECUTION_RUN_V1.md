# TASK_EVAL_LIVE_PILOT_EXECUTION_RUN_V1

Status: LIVE PILOT HALTED FAIL-CLOSED; QUOTA-SAFE FOLLOW-UP REQUIRED

## Objective

Join the already sealed D2L package, four-unit preflight, qualified official
Google capabilities, pinned local COMET checkpoint, shared backend, and
Evaluation method executor into one restart-safe calibration run.

## Runtime boundary

- The CLI requires `--execute-live`; importing or rendering configuration calls
  no provider.
- Provider, physical row, credential reference, model, schema, validator,
  prompt, and limits come from exact supplied records. The runner does not
  select or rotate them.
- The current official-Google profile uses `required` only with exact qualified
  native Structured Output evidence. A future third-party source must use a
  versioned `prompt_validated` or `disabled` profile and the same canonical
  local validators; it cannot reuse this capability evidence.
- Local validators remain semantic authority. Native Structured Output only
  constrains the wire shape.
- The shared backend owns one physical attempt, quota lease, usage/error rows,
  content-addressed response, and exact response cache. Evaluation owns scorer
  prompts, semantic validation, observations, and the calibration artifact.
- No transport retry, semantic retry, provider fallback, model fallback, key
  rotation, score callback, source/translation mutation, gold, or reference.
- Local COMET may shard its sealed request across bounded fresh CPU workers to
  cap peak memory. Sharding preserves row order, checkpoint, model, inputs and
  exact score-set binding; it does not alter scorer semantics.

## Persistence and replay

The output root is closed to:

- `profile.json`;
- `local_sf_qe_binding.json`;
- `execution.json`;
- `halt.json` for a terminally failed attempt;
- shared state under `_state/`;
- optional committed evidence metadata.

Each public JSON artifact is create-or-byte-equal. A complete execution is
validated and reused without COMET or provider calls. A partial run recomputes
the pinned local COMET batch and may reuse only exact accepted shared-cache
artifacts. A persisted attempt without its trusted cache cannot be called again
under the same lineage and therefore fails closed. Any exception after local
SF-QE preparation now seals a machine-readable `halt.json` with reconciled
attempt counts, known usage, terminal error identity and `publishable=false`.
A halted root is terminal and is rejected before COMET or provider access.

## Claim boundary

The four-unit output remains
`calibration_only / INCONCLUSIVE / pilot_not_headline_evidence`. It cannot be
published as a full-run winner or `FullRunReportV1`.

## Gate

Before a live call:

1. fake-transport success and exact replay;
2. foreign/mixed output-root rejection;
3. profile/input/capability substitution rejection;
4. local COMET exact-cover and checkpoint binding;
5. no secret in persisted public artifacts;
6. focused Evaluation and shared-backend regressions;
7. clean diff and committed Evaluation-only milestone.

## Verification

- New Google-envelope fake-transport probes: 6 passed. They exercised exact
  `responseJsonSchema`, `thinkingBudget=0`, 20 selected jobs, 24 physical
  attempts, zero-call complete replay, foreign-root rejection, path containment
  and no silent retry after a failed physical attempt.
- COMET subprocess, pilot runner and local-SFQE focused gate: 28 passed.
- Evaluation regression: 372 passed, 1,030 deselected.
- Full `pipeline/tests`: 1,402 passed in 285.39 seconds.
- The full gate used a temporary read-only mirror of frozen DB SHA-256
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
  its before/after hashes matched and the mirror was removed.
- Real accepted D2L four-unit local scorer gate: 8/8 rows, CPU,
  `unbabel-comet` 2.2.7, Python 3.11.9, checkpoint SHA-256
  `4F357AA38B0737DCD502F166238C99711FF3419D7B5C8CDF9CDE08525A8E7858`.
  The original one-process eight-row attempt exceeded the available CPU-memory
  envelope; one-row worker sharding completed the same sealed row set without
  changing model, inputs, score transform or exact score-set binding.
- API/network/provider calls during this implementation gate: zero.

## First live result

- Root:
  `data/reports/evaluation_v1/d2l_mlp_live_pilot_20260720T011727Z_row1_v2`.
- The local COMET stage completed all 8 rows.
- Provider attempts: 22 recorded, 21 succeeded, 1 failed with HTTP 429, and 2
  planned calls were never attempted.
- Successful provider usage: 18,624 prompt tokens, 1,001 completion tokens and
  19,625 total tokens. Cost remains unknown/null.
- The failed attempt was the SF-BT semantic judge for the first arm of the
  fourth selected unit. The runner halted without retry, row rotation, provider
  fallback, model fallback or execution publication.
- Three units have complete diagnostic PJ/SF-BT responses. SF-BT returned 100
  for all six completed arm observations, which is a calibration saturation
  finding rather than evidence of a winner.
- This result remains partial and non-publishable. See the root-local
  `RUN_HALTED.md` for the exact forensic summary.
- Post-halt hardening: 8 focused runner tests, 373 Evaluation regression tests,
  and 1,404 full `pipeline/tests` passed. The full gate used a temporary
  read-only frozen DB mirror; before/after SHA-256 remained
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`
  and the mirror was removed. No API call was made during the hardening gates.
