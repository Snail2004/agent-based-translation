# TASK_EVAL_SCORER_PLANTED_PROBE_V1

Status: EXTERNAL SEMANTIC REVIEW APPROVED - READY FOR DRY-RENDER - 0 API

Owner: Evaluation workstream

Depends on:

- `TASK_EVAL_SCORER_PROMPTS_V3.md`, reverse candidate revision `v3_1`;
- `EvaluationScorerInputPacketV1`;
- `SFBackTranslationResultV1`;
- `SFBTSemanticJudgeInputPacketV1`.

## Purpose

Create a reviewable, book-neutral planted fixture set for testing scorer
behavior before any real chapter-scale evaluation. The fixture is Evaluation
oracle data. It is never translation runtime input and is never exposed to a
scoring model except for the source/candidate fields explicitly selected by
the probe runner.

This task does not authorize an API call, choose a model, set a pass result, or
alter any public producer/report contract.

## Owned files

- `pipeline/eval/scorer_probe_fixtures_v1.py`
- `pipeline/tests/test_evaluation_scorer_probe_fixtures_v1.py`
- `data/eval/scorer_planted_probe_v1.json`
- this task

No App, DB, runtime, D2L/Literary producer, public input, or report file is in
scope.

## Root contract

The fixture root is a closed object:

```text
schema_id
schema_version
fixture_set_id
review_status
book_neutral
runtime_admission
sf_bt_context_ablation
pj_cases
```

Required fixed values:

```text
schema_id = EvaluationScorerPlantedProbeV1
schema_version = 1.0.0
review_status = approved_external_review
book_neutral = true
runtime_admission = forbidden
```

The loader validates without mutating input and computes a deterministic
canonical JSON SHA-256. The fixture does not carry a self-hash; the approved
canonical hash is pinned below and in tests. Any later semantic edit must
return to external review and produce a new pinned hash before API use.

## SF-BT context ablation

There are exactly 50 cases, ten in each closed stratum:

1. `P1_context_repair_risk`
2. `P2_omission_control`
3. `P3_anaphora_false_alarm`
4. `P4_ambiguity_resolution`
5. `P5_context_import_bait`

Every row carries:

```text
case_id
stratum
source_active_en
target_preceding_vi
target_active_vi
target_following_vi
planted_marker
measurement
author_note
```

`target_preceding_vi` and `target_following_vi` are nullable. ACTIVE source and
target are mandatory.

`author_note`, `planted_marker`, and every `expected_*` field are fixture
oracle metadata. They are available to offline reviewers and mechanical
measurement only. Prompt renderers must never expose them to a model.

Mechanical marker locks:

- P1: marker occurs in canonical English ACTIVE and Vietnamese context, but
  not Vietnamese ACTIVE. Importing it into back-translation can hide a real
  omission.
- P2: marker occurs only in canonical English ACTIVE. It establishes omission
  detection when context cannot repair the missing content.
- P3: marker names the referent in canonical English ACTIVE and Vietnamese
  context, while Vietnamese ACTIVE uses an anaphor. It measures false alarms
  caused by withholding context.
- P4: marker is the intended English sense of an ambiguous Vietnamese ACTIVE
  expression; context supplies the disambiguating evidence. These cases need
  semantic review and are not scored by naive string matching.
- P5: marker occurs only in Vietnamese context, not either ACTIVE. Importing it
  is a context-only hallucination.

The closed `measurement` values are:

```text
context_repair_import
omission_detection_control
anaphora_false_alarm
ambiguity_false_alarm
context_only_import
```

### Preregistered measurements

Profile A is `no_context`; profile B is `bounded_neighbors`. Every fixture row
is rendered under both profiles with the same static prompt revision.

`planted_marker_present(text, marker)` is an exact mechanical check after
Unicode NFC normalization and case folding, with Unicode word boundaries at
alphanumeric marker edges. It performs no stemming, translation, fuzzy match,
or semantic inference. This prevents a marker such as `14` from matching
`214`.

- P1 and P5 stage-1 import rate is the fraction of back-translations containing
  the planted marker. Profile-A hits are a contamination guard because the
  marker is structurally unavailable there. The policy threshold uses the 20
  pooled P1+P5 profile-B rows.
- P1 and P2 omission detection is the fraction of stage-2 responses whose
  `score` is below 100. `coverage_mismatch` rate is reported separately and is
  never OR-combined with the score to improve detection.
- P3 reference-resolution rate is the fraction of stage-1 back-translations
  containing the named marker. P3 false-alarm rate is the fraction of stage-2
  responses whose `score` is below 100.
- P4 false-alarm rate is the fraction of stage-2 responses whose `score` is
  below 100. P4 sense correctness is also reviewed blind: the reviewer sees
  canonical English ACTIVE plus back-translation, but not profile, context,
  marker, author note, or model identity. Reviewer identity and raw labels are
  persisted; indeterminate labels remain indeterminate rather than being
  forced correct.
- Stage-2 flags and notes are diagnostics. They never replace the declared
  score-based rates.

P1, P4, and P5 each contain five preceding-only and five following-only rows.
The fixture validator enforces this balance. P3 remains preceding-only because
it tests ordinary backward anaphora rather than rarer cataphora.

Known sensitivity limits accepted by external review:

- `P4_08_session` and `P4_09_mouse` may produce a neutral English hypernym
  (`session` or `mouse`) without committing to the intended sense. Their
  per-case outputs are reported and must not be overinterpreted.
- `P4_01_lock` contains a mild contextual cue inside ACTIVE (`before leaving`);
  it remains useful but is not treated as one of the strongest P4 cases.

### Preregistered context-profile decision

Profile B is selected only when all conditions hold:

1. pooled P1+P5 profile-B import rate is at most 5% (at most one of 20);
2. P2 omission detection is at least 90% in each profile and differs by no
   more than 10 percentage points between profiles;
3. P1 omission detection under B is no more than 10 percentage points below
   P1 under A;
4. B lowers P3 false-alarm rate by at least 20 percentage points versus A;
5. B lowers P4 false-alarm rate by at least 20 percentage points versus A.

If any condition fails, select profile A. A failed probe is reported as
evidence; it is not retried or reinterpreted into profile B.

## PJ planted cases

PJ cases are source-aware, book-neutral, and one-axis-one-error where feasible.
Every row carries:

```text
case_id
category
block_type
source_en
candidate_a_vi
candidate_b_vi
expected_overall
expected_style
expected_primary_tag
author_note
```

Expected winners are `a`, `b`, or `tie`. The primary tag is nullable or one
value from the prompt taxonomy. At minimum the set covers:

```text
identical
meaning_reversal
omission_addition
numeric
negation
grammar
terminology_only
tone_register
formatting
short_fragment
naturalness
word_choice
```

Expected labels are used only by the offline probe scorer. They are never
rendered into a PJ request.

Overall and style verdicts are exact probe expectations. Primary-tag accuracy
is diagnostic and reported separately from verdict accuracy. `PJ_07` records
`meaning` as an adjacent taxonomy label to expected `terminology`;
`PJ_12` records `naturalness` as adjacent to expected `word_choice`. Adjacent
labels are reported as near misses, not silently counted as exact passes and
not used alone to fail an otherwise correct overall/style verdict.

## Fairness locks

- No row may be copied from the selected MLP chapter, D2L community gold,
  Wuthering Heights, Gatsby, or a runtime translation output.
- No expected answer appears in any runtime prompt template.
- A failed semantic probe is evidence about the scorer/model, not a reason to
  rewrite fixtures after seeing outputs.
- Any semantic edit after review changes the fixture hash and requires a new
  review record.
- The same frozen fixture bytes and presentation policy are used for every
  candidate model arm.
- Mechanical marker metrics and LLM semantic judgments are reported
  separately.

## Offline acceptance

Before external semantic review:

- closed-schema validation passes;
- unknown keys, duplicate IDs, bad strata/categories, missing markers, and
  malformed expected labels fail closed;
- there are exactly 10 SF-BT rows per stratum;
- required PJ categories are covered;
- canonical hash is deterministic and input objects remain unchanged;
- book-specific term scan is clean;
- 0 provider calls, 0 DB reads/writes, and 0 runtime imports occur.

The fixture content is externally approved. API allocation and model probing
remain a separate explicitly approved milestone.

## Approved handback

Approved canonical SHA-256:

```text
2d34aa5cd08885316a538af7b340e184184d43cde2341f9f6cbf80a03d4f56b0
```

Contents:

- 50 SF-BT context-ablation rows, exactly ten per P1-P5 stratum;
- 12 PJ rows, one for every required category;
- synthetic neutral prose only; no selected MLP, D2L gold, Wuthering Heights,
  Gatsby, or runtime translation row.

Offline verification:

- fixture contract/adversarial/dry-render tests: 19 passed;
- all `test_evaluation_*.py` tests with the fixture included: 180 passed;
- all 50 SF-BT rows rendered under both context profiles;
- the identical PJ row took the zero-call path and all 11 differing rows
  produced both presentation orders;
- the complete fixture root cannot validate as
  `EvaluationScorerInputPacketV1`;
- no provider call, database access, runtime import, App wiring, or public
  contract change occurred.

External semantic review must inspect:

1. whether each P1-P5 pair isolates the intended context effect;
2. whether each PJ pair remains one-axis-one-error and its overall/style label
   is defensible;
3. whether the synthetic language is natural enough that probe failures measure
   the scorer rather than awkward fixture wording.

External semantic review of committed candidate `b60ea5c` completed with
verdict `ACCEPT` on 2026-07-18. The reviewer independently recomputed the draft
fixture and prompt hashes, inspected all P1-P5 and PJ rows, and accepted the
declared measurement rules. Approval changes no semantic fixture row; it only
advances the closed review status, records the accepted limitations above, and
adds a defensive empty-marker guard.

This hash identifies the externally approved fixture. It may be placed in a
dry-render or explicitly approved canary run config, but it remains
`runtime_admission=forbidden` for translation runtime and supplies no authority
to call an API by itself.
