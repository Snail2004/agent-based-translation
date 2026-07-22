# TASK_EVAL_SF_BT_P2_LIVE_PROBE_V1

Status: IMPLEMENTATION - OFFLINE GATE FIRST

Owner: Evaluation workstream

## Purpose

Test whether the current SF-BT scorer detects deliberately omitted source
content. The targeted probe uses only the ten approved
`P2_omission_control` rows under both existing context profiles.

This task does not change a scorer prompt, score band, semantic validator,
provider, model, source translation, or public report contract.

## Workload

The closed workload is:

```text
10 cases
x 2 context profiles (no_context, bounded_neighbors)
x 2 LLM stages (back translation, semantic judge)
= 40 accepted semantic calls
```

The approved fixture and its oracle metadata remain Evaluation-only.
`planted_marker`, `measurement`, and `author_note` are never rendered into a
model prompt.

## Recovery contract

- Persist one validated checkpoint for every accepted
  `case x context_profile x stage` result.
- A new invocation uses a new sealed attempt ID but the same logical run,
  fixture and immutable semantic contract.
- The semantic contract locks each stage's exact model, prompt, response
  schema, local validator, generation parameters, reasoning mode, token/call
  limits, retry policy, Structured Output mode, and provider route family.
- An explicit resume may use another qualified physical credential row on the
  same provider route. That row receives its own profile, source, capability,
  quota bucket, attempt ID, and attempt-scoped execution record. This is
  transport distribution, not a semantic change.
- Accepted checkpoints are validated and reused; their provider calls are not
  repeated.
- A crash after provider success but before checkpoint publication may recover
  only through the shared content-addressed response cache.
- HTTP 429 pauses the run. It is never retried blindly.
- HTTP 408, HTTP 503, or a transport timeout may consume at most one additional
  explicitly sealed attempt supplied to the invocation.
- Semantic rejection is terminal for the invocation. It is not silently
  retried or reinterpreted.
- No provider, model, output mode, prompt, validator, generation setting, or
  scoring parameter switch is permitted within a logical probe. Any such
  change starts a new logical run.
- Credential-row changes are never automatic fallback or silent rotation.
  They require a new invocation and attempt ID; accepted checkpoints remain
  immutable and are not repeated.
- The live wrapper seals an explicit minimum inter-call interval. The default
  is 4.2 seconds for a source whose recorded ceiling is 15 RPM; changing it
  changes the persisted runtime binding rather than silently changing pacing.

Every invocation has an immutable attempt record and exactly one terminal
`complete` or `halt` record. The logical root publishes `result.json` only
after exact cover of all 40 accepted stage checkpoints.

The live wrapper publishes the immutable initial `runtime_binding.json`, one
`semantic_contract.json`, and an `execution_binding.json` under each new
attempt. Together they distinguish stable scorer semantics from the physical
row that served each attempt without storing a plaintext credential.

## Measurement

For each context profile:

```text
omission_detected = stage-2 score < 100
omission_detection_rate = omission_detected / 10
coverage_mismatch_rate = rows carrying coverage_mismatch / 10
```

The preregistered interpretation is:

- at least 90% omission detection in both profiles:
  `not_blind_to_planted_omission`;
- below 90% in either profile:
  `insensitive_to_planted_omission`;
- incomplete exact cover: no result and no semantic conclusion.

Passing this probe demonstrates only sensitivity to the planted omission
control. It does not establish calibration, general reliability, or a useful
absolute 0-100 interpretation.

## Offline gate

Before a live call:

- closed deterministic packet/checkpoint/result validation;
- fixture hash and P2 exact-cover checks;
- no oracle field appears in rendered prompts or public checkpoints;
- fail-at-N then resume without duplicate successful requests;
- 503/408 consumes at most one new sealed attempt;
- 429 halts and remains resumable;
- resume on another physical row reuses completed checkpoints and records both
  execution profiles;
- model, generation setting, provider route, schema, or validator drift is
  rejected before transport;
- foreign/tampered checkpoint, profile, source, fixture, or attempt fails
  closed;
- result cannot publish at 39/40;
- fake transport completes 40 accepted calls;
- 0 API, 0 source/runtime DB mutation, and no secret in output.

## Deferred

- the other four SF-BT planted strata;
- all PJ planted cases;
- chapter-scale scoring;
- human calibration;
- new LLM metrics or scorer prompt changes;
- a broad mechanical G0 score.
