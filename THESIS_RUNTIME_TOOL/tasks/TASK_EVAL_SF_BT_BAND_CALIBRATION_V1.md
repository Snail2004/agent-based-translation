# TASK: SF-BT Semantic Band Calibration V1

Status: Phase A approved; Phase B 0-API live runner implemented and under gate

## 1. Question

The current SF-BT semantic judge emits one of five closed scores:

```text
0, 25, 50, 75, 100
```

The completed P2 context-ablation probe showed that the judge detects planted
omissions, but it did not establish that these five absolute bands are used
consistently or in the intended severity order.

This task measures the band behavior without changing the scorer prompt and
without declaring an unreviewed threshold to be correct.

## 2. Scope

Phase A adds only:

1. one book-neutral, eval-only fixture with 15 English-English comparison rows;
2. a closed fixture validator and deterministic content hash;
3. an oracle-free passage projection;
4. a deterministic analyzer for future model responses;
5. adversarial offline tests.

No API call, model selection, score threshold, App wiring, DB write, runtime
callback, translation mutation, or public report-contract change is allowed.

## 3. Fixture design

The fixture contains three independent ladders:

- experiment schedule;
- laboratory procedure;
- travel schedule.

Each ladder keeps one reference passage and supplies one candidate for each
expected band, ordered `100, 75, 50, 25, 0`. This makes within-ladder ordering
meaningful; comparisons across unrelated topics are not treated as equivalent
evidence.

The fixture is:

- book-neutral;
- `runtime_admission=forbidden`;
- bound to the unchanged `sf_bt_semantic_judge_v3_candidate` prompt hash;
- approved by an independent semantic review recorded in the Evaluation report
  tree. The review was relayed by the user; reviewer model/version was not
  supplied and must not be invented.

The model must never receive `expected_score`, `expected_primary_reason`, or
`author_note`. Projection exposes only two arbitrarily ordered passages and a
presentation identifier.

## 4. Measurements

The analyzer reports facts, not a fabricated pass/fail verdict:

- exact-band accuracy;
- within-one-band accuracy;
- mean absolute point error;
- mean band distance;
- per-band counts and error;
- full 5x5 confusion matrix;
- predicted-score distribution;
- strict monotonic pair rate within each ladder;
- non-inversion rate, ties, inversions, and severe inversions.

A severe inversion means the oracle bands differ by at least two steps while
the model ranks the lower-severity candidate above the higher-severity one.

## 5. Explicit non-goals

Phase A does not:

- claim the scorer is calibrated;
- convert five bands into false continuous precision such as 58 or 91;
- tune cases or prompts to make one model look good;
- run repeated model observations;
- measure orientation or provider variance;
- reuse the generic legacy human-rating calibration module;
- fabricate an SF-BT stage-1 result merely to satisfy a live packet lineage.

## 6. Known limitations from independent review

- `procedure_100` is the weakest 100-band case because "heat to" versus "keep
  at" may be read as a minor action distinction.
- `procedure_025` may attract 0 from a strict judge because it reverses a key
  relation; `travel_025` may attract 50 if the judge focuses only on the changed
  destination. These are structured one-band disagreements, not grounds to
  rewrite results after observation.
- The 75-band cases all exercise numeric hedging and the 50-band cases all
  exercise numeric substitution. Other rubric subtypes remain untested in V1.
- Presentation orientation is deterministic but each case is evaluated in only
  one direction. V1 does not claim orientation invariance.

## 7. Phase B 0-API runner

The calibration runner uses an honest calibration-specific packet. It binds the
approved fixture and exact passage bytes, but the rendered model prompt contains
only `passage_a` and `passage_b`. Case IDs, expected bands, reviewer reasons,
author notes, and presentation IDs remain outside every model message.

The preregistered call plan is fixed at 35 accepted semantic-judge calls:

1. all 15 cases once in their canonical deterministic orientation;
2. all 15 cases a second time in the same orientation with provider cache
   bypassed;
3. five reversed-orientation screens, with one deterministically selected case
   from each expected band.

The two full rounds measure repeatability. The five reversed calls are only a
screen: orientation effects and stochastic variation are not identified
separately by this design.

Every accepted call creates one content-bound checkpoint. A replay with exact
coverage makes no provider call. A failed call creates no accepted checkpoint;
the next invocation resumes only the missing calls. A different physical
credential row may distribute the remaining requests only when the logical run
and row-independent semantic contract are unchanged. Model, prompt, validator,
schema, route family, generation settings, and output mode cannot change during
resume.

Each invocation also seals a bounded `max_new_calls`. Reaching that cap pauses
cleanly with the remaining count and without manufacturing a provider failure.
This allows a 35-call experiment to respect per-row daily quotas while keeping
one immutable semantic experiment. The live CLI binds the selected physical row
to the same row in the qualified capability summary before it reads the external
credential.

The calibration profile bounds input at 4,096 tokens per call, versus the wider
generic scorer ceiling, because the authored passage-pair prompt is much smaller.
The model, prompt, response schema, output cap, temperature, and reasoning policy
remain unchanged. Each attempt records both per-call and aggregate prompt,
completion, and total-token maxima derived from that exact sealed profile.

The runner has no automatic retry, provider fallback, model fallback, or silent
key rotation. Cache mode is exactly `bypass`. Score values outside the closed
five bands, including pseudo-precise values such as 58, fail local validation.

The result reports both full-round analyses, per-case repeat deltas, the bounded
orientation screen, exact attempt lineage, and all accepted observations. Its
interpretation remains `measurement_only_not_a_calibration_pass`.

## 8. Gates before a live sample

Before any API call:

1. the honest packet and 35-call runner pass fake-transport, checkpoint, resume,
   semantic-drift, cache-bypass, oracle-leak, and tamper tests;
2. one model/profile is sealed for the logical run;
3. exact official-source capability evidence is valid for the selected model,
   response schema, and local validator;
4. call, token, retry, output-root, and quota caps are sealed;
5. the selected physical API row is recorded without exposing its secret.

The existing production scorer prompt and local validator remain the semantic
authority. A future live result remains development evidence and cannot enter
translation runtime memory or influence the translations being evaluated.

## 9. Acceptance for Phase A

- fixture validates as exactly 15 rows, three per band and five per ladder;
- prompt candidate ID and hash match the current scorer bytes;
- projection contains no oracle/reviewer metadata;
- missing, extra, duplicate, malformed, and unsupported-band observations fail
  closed;
- deterministic synthetic predictions produce exact expected measurements;
- validators and analyzers do not mutate caller input;
- focused and broad offline tests pass;
- worktree contains no generated runtime artifacts or credentials.

## 10. Acceptance for Phase B 0-API

- the plan has exactly 30 canonical repeat calls plus five reversed screens;
- one reversed case is selected per expected score band;
- all prompts are free of case IDs, expected reasons, author notes, and
  presentation IDs;
- a deterministic fake provider completes 35 checkpoints and replay performs
  zero additional calls;
- an interrupted run resumes only missing checkpoints, including when the
  physical row changes but the semantic contract remains identical;
- an invocation cap pauses without a failed provider request and is recorded in
  the attempt artifact;
- changing model, temperature, route family, or response schema during resume
  fails before transport;
- cache-enabled execution and out-of-contract scores fail closed;
- resealed plan, checkpoint, and nested-result tampering is detected;
- focused and broad Evaluation tests pass with 0 API and no source/DB mutation.
