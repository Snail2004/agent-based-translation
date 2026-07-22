# TASK_EVAL_BASELINES_ALIGNMENT_V1

Status: SPEC LOCKED 2026-07-18 (user + CodeX)

Owner: Evaluation workstream

Purpose: lock the common benchmark arms, scoring scope, pairwise cost
controls, and alignment boundary before scorer prompts or live API work.

This task records decisions only. It does not authorize API calls, baseline
generation, public contract changes, App work, DB work, or scorer execution.

## 1. Thesis question

The common Evaluation pipeline must answer:

1. How good is each translation arm on its own?
2. Which arm is preferred when two translations cover the same source content?
3. How much source content did each arm preserve?
4. Does the proposed memory-enabled pipeline improve over its own no-memory
   ablation without relying on weak external competitors?

Evaluation must be honest and fair:

- the compared arms must cover the same declared source universe;
- missing content must remain visible;
- arm identities must be hidden from semantic judges;
- human translation is a candidate, not an infallible gold answer;
- no score, reference, or judge result may flow back into translation runtime.

## 2. Locked benchmark arms

The headline benchmark contains exactly five conceptual arms.

| Arm | Role | Required constraint |
|---|---|---|
| S0 | Pipeline ablation | Same translation stack as S1, but declared memory/context intervention disabled |
| S1 | Thesis system | Full declared memory/context pipeline |
| Human | External human translation | One preselected edition per document; never used to generate another arm |
| Google-NMT | Conventional machine translation | Google Cloud Translation standard NMT; no project glossary or custom adaptation |
| LLM-LC | Strong modern long-context competitor | One fixed strong model sees whole-document context but emits bounded chapter/block output |

The final artifact IDs may be producer-specific. The five names above are
benchmark roles, not permission to weaken existing `TranslationArtifactV1`
identity and hash bindings.

### 2.1 S0 and S1

S0 and S1 must differ only by the preregistered thesis intervention. They must
share source package, admitted universe, translation profile, model family,
and all other controls unless a separately declared experiment says otherwise.

### 2.2 Human

Use one human translation per document in v1. Do not add multiple publishers or
translators until the five-arm benchmark has completed.

Human text is evaluation-only. It must never enter Builder, Context Engine,
Translator, memory, retrieval, or baseline-generation prompts.

### 2.3 Google-NMT

The primary Google arm is a reproducible Google Cloud Translation API artifact,
not an unidentified capture from `translate.google.com`.

The upstream producer must preserve canonical block identity in its request and
response mapping. It must record the exact API/model identity, configuration,
timestamp, request hash, response hash, usage, and per-block status.

Evaluation does not call Google and does not author this translation artifact.

### 2.4 LLM-LC

LLM-LC is the only general-purpose chatbot/LLM baseline in v1. Do not create
separate GPT-Web, Gemini-Web, Claude-Web, or other model-zoo arms.

The model may see the whole admitted document as context, but output is requested
in bounded chapter or block groups with explicit source IDs. This tests the value
of long global context without relying on an unbounded one-response translation.

The LLM-LC prompt must not contain:

- Human text;
- gold/reference translations;
- S0 or S1 output;
- project memory or glossary created by S1;
- score thresholds, judge rubrics, or expected winners.

The exact model, version, prompt, generation settings, and context-cache policy
remain deferred until a sealed dry-render and API allocation.

## 3. Explicit exclusions

The following are outside the headline five-arm benchmark:

- a second human edition;
- separate Google Web and Google API arms;
- multiple long-context LLM providers;
- per-model web UI leaderboards;
- a whole-book one-shot response as a sixth arm;
- D2L-only TC-Occ and TA-Occ as common Evaluation metrics;
- any composite quality score whose weights were chosen after seeing results.

A whole-book one-shot translation may be run later as a diagnostic stress test.
Its main outputs are coverage, omission, truncation, and structural integrity.
It does not enter headline PJ ranking in v1.

## 4. Producer and Evaluation authority

Baseline translation generation is upstream of Evaluation.

```text
Canonical Source Package
  -> D2L or Literary translation producer
  -> sealed TranslationArtifactV1
  -> read-only Evaluation
```

Evaluation must not:

- translate source text;
- repair or complete a baseline;
- rewrite Human text;
- mutate a producer artifact;
- silently retry because a translation or score looks undesirable;
- emit recommendations back into a translation registry.

Current public `TranslationArtifactV1` producer authority remains exactly
`d2l|literary`. A future generic benchmark producer requires an explicit public
contract decision and is not implied by this task.

## 5. Locked scoring matrix

| Method | Arms | Corpus scope |
|---|---|---|
| Coverage / omission | All five | Full admitted source universe |
| SF-QE | All five | Full available common evaluation universe |
| SF-BT | All five | Full available common evaluation universe |
| PJ | All five | Preregistered stratified source sample |
| TC-Occ / TA-Occ | D2L profile only | Outside the common pipeline |

SF-QE and SF-BT are unary: each arm receives its own result.

PJ is comparative. It must not be implemented as "S1 versus everyone" only.
With five arms, the complete unordered round robin has:

```text
C(5, 2) = 10 pairs
```

All ten pairs are included in the PJ design so Human, Google-NMT, LLM-LC, S0,
and S1 can be placed in one connected comparison graph.

## 6. PJ scope and cost controls

PJ does not run over every source block. Its source units are selected by a
preregistered stratified sample that is independent of arm outputs and scores.

Candidate strata include:

- document/profile: technical or literary;
- chapter;
- source length;
- structural/content type;
- difficulty proxy fixed before semantic judging.

Exact sample size and stratum quotas are set only after an offline corpus
inventory and dry-render cost table.

### 6.1 Presentation-order control

Do not run both A/B orientations for every pair and every unit by default.

Instead:

- assign one deterministic orientation per pair/unit;
- balance A-first and B-first counts globally for each pair;
- run the reversed orientation on a preregistered calibration subset;
- route orientation-discordant or low-confidence cases to audit.

This estimates position bias without doubling the complete PJ workload.

### 6.2 Judge count

One primary judge covers the sealed PJ sample.

A second judge is limited to:

- the preregistered random audit subset;
- ties or low-confidence results;
- reversed-order disagreement;
- response-contract anomalies that survive transport retry.

The second judge must not be invoked selectively because S1 lost.

### 6.3 Aggregate ranking

The ten-pair result may be summarized by a connected Bradley-Terry-style or Elo
presentation, but the exact estimator, uncertainty method, and display scale
must be locked before live scoring.

Per-pair wins, losses, ties, sample counts, missing counts, and judge agreement
remain primary auditable facts. A single ranking number never replaces them.

## 7. Why an alignment layer is required

S0, S1, Google-NMT, and controlled LLM-LC can preserve canonical source IDs at
generation time. Human translations and uncontrolled whole-output captures may
split, merge, omit, or add paragraphs.

Evaluation cannot assume:

```text
one source block == one Human paragraph
```

It needs an evaluation-only alignment sidecar. The proposed internal identity is
`AlignmentManifestV1`; this name is not yet a public producer contract.

The manifest maps immutable source spans to immutable target spans without
rewriting either text.

Supported mapping kinds:

- `1:1`;
- `1:N`;
- `N:1`;
- `N:M`;
- `missing`;
- `added`;
- `ambiguous`.

Supported decision states:

- `exact_id`;
- `auto_accepted`;
- `review_required`;
- `reviewed`;
- `missing`;
- `added`;
- `ambiguous`.

## 8. Alignment method boundary

The aligner may use:

- chapter and heading boundaries;
- monotonic source/target order;
- names, numbers, equations, and other stable anchors;
- multilingual embedding similarity;
- source/target length compatibility;
- bounded adjacent span combinations;
- monotonic dynamic programming.

It must not use:

- evaluation scores;
- an expected arm ranking;
- S0/S1 as an answer key for Human alignment;
- manual rewriting of target text;
- non-monotonic cross-chapter matching without an explicit reviewed exception.

High-confidence mappings may be accepted mechanically. Low-confidence mappings
go to a bounded review queue. A reviewer approves mapping only; the reviewer does
not edit translation content.

The alignment manifest is sealed before semantic scoring. It cannot be changed
after seeing scores without producing a new version and a disclosed rerun.

## 9. Common evaluation units

The alignment layer produces an internal common read model, provisionally named
`CommonEvaluationUnitV1`.

Example:

```text
unit U07
source span: b010 + b011
S0:          S0[b010] + S0[b011]
S1:          S1[b010] + S1[b011]
Google-NMT:  G[b010]  + G[b011]
LLM-LC:      L[b010]  + L[b011]
Human:       h007
```

The source span is the authority. Target concatenation does not mutate the
underlying artifacts.

For headline cross-arm results, all methods consume the same sealed common unit
manifest. Optional machine-only per-block diagnostics must be labeled separately
and cannot replace the common-unit result.

## 10. Coverage and missingness

Quality and coverage are always reported separately.

```text
quality_on_available_units
coverage_of_preregistered_source_universe
```

Missing, failed, review-held, excluded, preserved, added, and ambiguous content
must remain visible. They cannot be silently removed from denominators.

PJ is executed only when both compared arms have an eligible translation for the
sampled unit. The source unit remains in the sample ledger, and pair-specific
availability is reported so an arm cannot obtain a high score by translating only
easy content.

## 11. Fairness locks

Before semantic scoring:

1. Freeze the source package and admitted universe.
2. Freeze all five arm artifacts.
3. Freeze Human alignment and common evaluation units.
4. Freeze the PJ sample and pair list.
5. Freeze scorer model, prompt, method version, and cost envelope.
6. Blind arm/provider/system labels in scorer requests.

After scoring begins:

- no arm repair;
- no sample replacement;
- no threshold tuning on headline results;
- no prompt change under the same method version;
- no retry based on winner or score;
- no reference/gold leakage into runtime.

## 12. Deferred decisions

The following are intentionally not locked by this task:

- exact Human edition and extraction artifact per document;
- exact LLM-LC provider/model;
- exact SF-QE, SF-BT, and PJ scorer models;
- PJ sample size and stratum quotas;
- reverse-order calibration share;
- second-judge audit share;
- alignment embedding model and confidence threshold;
- Bradley-Terry/Elo estimator and uncertainty method;
- whether `AlignmentManifestV1` later becomes a public contract;
- Console or Cockpit presentation.

Each decision requires an offline fixture, corpus inventory, dry-render, or
explicit API allocation. Deferral is not permission to choose after seeing
headline results.

## 13. Next milestone

The next Evaluation milestone is 0-API and fixture-only:

1. define a closed internal `AlignmentManifestV1` draft;
2. validate monotonic `1:1`, `1:N`, `N:1`, and `N:M` mappings;
3. preserve exact source and target bytes by reference/hash;
4. reject overlap, duplicate IDs, foreign IDs, reversed order, and hash drift;
5. build deterministic `CommonEvaluationUnitV1` projections;
6. represent missing, added, and ambiguous rows explicitly;
7. test one Human-style merged paragraph fixture;
8. test one omitted paragraph and one added paragraph fixture;
9. perform no semantic model call, DB write, App change, or public contract edit.

Only after that milestone should the workstream inventory real Human artifacts
and measure how much alignment can be automated versus manually reviewed.

## 14. References

- Google Cloud Translation overview:
  `https://docs.cloud.google.com/translate/docs/overview`
- Google Cloud batch translation:
  `https://docs.cloud.google.com/translate/docs/advanced/batch-translation`
- Gemini long-context guidance:
  `https://ai.google.dev/gemini-api/docs/long-context`
- Gemini token and input/output-limit guidance:
  `https://ai.google.dev/gemini-api/docs/tokens`
