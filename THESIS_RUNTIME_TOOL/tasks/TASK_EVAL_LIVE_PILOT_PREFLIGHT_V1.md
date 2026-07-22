# TASK_EVAL_LIVE_PILOT_PREFLIGHT_V1

Status: COMPLETE (0-API gate; live provider run remains separately sealed)

## Objective

Prepare a bounded, auditable live Evaluation pilot before any provider call.
The preflight selects a small real-source slice, renders every prompt that can
exist before upstream model output, and computes the physical-call and hard
token reservations from pipeline-owned role budgets.

This artifact is internal execution evidence. It is not a score, verdict,
`FullRunReportV1`, runtime-memory input, or public producer contract.

## Locked pilot shape

- Input: one sealed `CommonEvaluationInputV1` plus one sealed
  `EvaluationRunConfigV1` with exactly two arms, methods `sf_qe`, `sf_bt`, and
  `pj`, and one comparison pair.
- Default slice: eight fully ready translated blocks.
- Selection: four source-length strata, deterministic hash selection within
  each stratum, then canonical source order.
- Selection authority: source metadata only. Gold, human reference, scores,
  model outputs, arm quality, and translation length/content cannot influence
  which units are selected.
- Context: reuse the bounded neighboring blocks declared by the run config.
- SF-QE: local model, zero provider calls.
- SF-BT: one back-translation call and one semantic-judge call per arm/block.
  The second prompt remains explicitly deferred until the first output exists.
- PJ: two order-balanced calls unless candidate sequences are mechanically
  equal, in which case code records zero PJ calls.
- Qualification probes: separately capped at three calls. They are not counted
  as scorer calls.

For eight non-equal units this gives 40 planned jobs and at most 48 scorer API
calls: 16 SF-BT back-translations, 16 SF-BT semantic judgments, and 16 PJ
presentations. The hard role reservation is 576,000 prompt tokens plus 81,920
completion tokens. These are safety maxima, not predicted usage. Cost remains
`null` until an authoritative provider tariff or provider-reported cost exists.

## Stored evidence

`EvaluationLivePilotPreflightV1` stores:

- exact project/document/config/input/plan/arm bindings;
- selected unit and block IDs, source order, source character count, and
  length stratum;
- job, packet, prompt-template, and rendered-prompt hashes;
- rendered/deferred prompt counts;
- per-role physical-call counts;
- a rough UTF-8 byte-based prompt-token estimate for rendered prompts;
- hard role-owned prompt/completion reservations;
- a deterministic artifact self-hash.

It does not store prompt text, source text, translations, gold/reference data,
scores, verdicts, provider credentials, or model outputs.

## Fail-closed rules

- Reject unknown keys, stale self-hashes, foreign input/config bindings,
  unsupported methods/arm counts, blocked jobs, insufficient ready units,
  duplicate IDs, noncanonical source order, inconsistent exact-cover counts,
  altered role budgets, or malformed rendered/deferred evidence.
- Validators and builders do not mutate caller input.
- No API, credential lookup, DB write, cache mutation, checkpoint mutation,
  report publication, App wiring, or scorer-policy migration occurs here.

## Verification

- Dedicated adversarial tests cover deterministic stratification, exact call
  and token envelopes, zero-call mechanical PJ ties, closed schemas, resealed
  binding substitution, insufficient ready-unit rejection, and immutability.
- Adjacent planner, packet, prompt, adapter, and method-executor regressions must
  pass before a real-package dry render.
- A live call remains closed until the user assigns one exact physical Gemini
  row and exact model route. No silent row/model/provider fallback is allowed.

## Real-package result

The accepted D2L MLP package was projected read-only and dry-rendered at code
commit `0319a4a1f8109685f89607a8e6d989355f71c2aa`.

- Fully ready source units: 475.
- Selected units: 8, exactly two from each source-length stratum.
- Planned jobs: 40 (16 SF-QE, 16 SF-BT, 8 PJ).
- Physical scorer calls: 46 (16 back-translations, 16 semantic judgments,
  14 order-balanced PJ calls). One selected unit was a mechanical S0/S1 tie,
  so its two PJ calls were omitted by code.
- Immediately rendered prompts: 30; deferred semantic prompts: 16.
- Estimated tokens for immediately rendered prompt/schema bytes: 35,798.
- Hard role reservation: 552,000 input + 80,896 output = 632,896 tokens.
- Qualification probes remain a separate maximum of three calls.
- API calls, credential reads, model outputs, scores, DB writes, runtime
  callbacks, and published reports: zero.

The sealed artifact is under
`data/reports/evaluation_v1/d2l_mlp_live_pilot_preflight_v1/104ef09cfe22d8a873b733c9a408d6a0419898a9e64afea80e5c2cc9f697eb38/`.
