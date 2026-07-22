# TASK_EVAL_PHASE_B_OFFLINE_ORCHESTRATOR_V1

Status: Phase B0-A COMPLETE; Phase B0-B COMPLETE (0-API)

Owner: Evaluation workstream

Base dependency: `33a9849134ef70d7e98faa266da7c90c78d3f7d0`

DEC-017 rework base: `8eb8405befc4ec010658a5568ebc2b686249bdff`

## 1. Objective

Build the common, deterministic planning boundary for the thesis evaluation
pipeline. The pipeline runs after translation and must work the same way for a
technical document or a literary work once an adapter has projected the sealed
source and translation artifacts into the common read model.

This task does not score translation quality. It proves that the system can:

1. validate a sealed source snapshot and separate translation artifacts;
2. preserve the canonical `chapter_id` / `block_id` sequence;
3. keep source blocks immutable and translations as versioned, content-addressed
   arm overlays;
4. preregister the selected arms, methods, context policy, comparisons,
   blinding seed, and retry ceiling;
5. generate the same evaluation units and jobs deterministically;
6. expose every unavailable translation as blocked coverage rather than silently
   dropping it;
7. avoid leaking D2L memory, glossary, gold, model identity, or expected answers
   into the common scorer input.

## 2. Architectural boundary

```text
Canonical source package (immutable)
             +
sealed TranslationArtifactV1 arm overlays
             |
profile adapter (D2L first; Literary later)
             |
CommonEvaluationInputV1 read model
             +
sealed EvaluationRunConfigV1
             |
deterministic offline planner
             |
EvaluationPlanV1 (in-memory in B0-A)
             |
later: checkpointed scorer executor -> aggregation -> FullRunReportV1
```

The common read model is internal. It does not replace `document.json`,
`D2LEvaluationInputV1`, `TranslationArtifactV1`, or a future Literary exporter.
It deliberately contains only fully bound identity, source blocks, arm metadata,
and translation rows needed by common quality scoring.

## 3. Locked responsibilities

### 3.1 Producing translation pipeline

The D2L or Literary pipeline owns source normalization and translation. It
produces sealed artifacts and never receives scores, gold, thresholds, judge
recommendations, or callbacks from Evaluation.

### 3.2 Profile adapter

An adapter validates the profile package with its public validator and projects
only the common source fields. The first compatibility adapter consumes
`D2LEvaluationInputV1` for fixture/offline planning; it does not claim producer
integration under DEC-017.

Each translated arm is supplied separately as `TranslationArtifactV1`. The
public artifact producer is closed to D2L or Literary. Evaluation validates and
joins these artifacts but cannot author a public translation input.

Canonical source authority is a closed `canonical_source_package_v1` binding
over the exact DEC-014 component identities:

- project and document IDs;
- document schema version and hash;
- structure schema version and hash;
- asset-manifest schema version and hash;
- admitted-projection schema version and payload hash;
- admission-policy ID, version, and hash.

There is no invented aggregate source-package hash. A join requires the entire
typed binding plus `block_id` to match; `block_id` alone is never authority.

The D2L fixture adapter retains a separate `legacy_d2l` compatibility binding
with honest `source_db_sha256` and `runtime_manifest_sha256` names. Public
`TranslationArtifactV1` validation rejects this kind. Only the explicitly
offline common-input path may consume it, and it cannot be relabeled or
substituted for canonical DEC-017 identity.

It must not expose these D2L-only fields to a common scorer:

- `runtime_terms`;
- `injection_rows`;
- glossary policy or memory state;
- TC-Occ / TA-Occ facts;
- any gold or reference authority.

Future Literary input must project to the same read model without changing the
planner.

### 3.3 Offline planner

The planner performs structural work only:

- select blocks by declared admission;
- attach bounded same-chapter neighbor block IDs;
- inspect translation status mechanically;
- create unary and explicitly requested pairwise jobs;
- deterministically counterbalance pairwise presentation order;
- count ready and blocked jobs.

It does not understand language, compute a score, choose a winner, infer a
missing translation, or retry a semantic answer.

### 3.4 Evaluation scorer and report writer (later phases)

Scorers will implement common methods such as SF-QE, SF-BT, and PJ. D2L-only
TC-Occ and TA-Occ remain outside this common pipeline and may later appear only
as separately persisted profile facts.

`FullRunReportV1` remains the sole full report projection. App UI and Console
are read-only consumers and must never recompute scores or usage.

## 4. Phase boundaries

### Phase B0-A - current task

- common immutable source/read model;
- D2L source compatibility adapter;
- `TranslationArtifactV1` validator and self-hash;
- sealed `EvaluationRunConfigV1`;
- deterministic in-memory planner;
- exact coverage and counterbalancing tests;
- 0 API calls, 0 DB access.

### Phase B0-B - current runner milestone

This phase is authorized as an independent Evaluation-only runner milestone:

- persist a self-hashed wrapper around `EvaluationPlanV1`;
- checkpoint/resume by `(config hash, input hash, plan hash, job_id)`;
- execute deterministic fixture jobs without language judgment;
- retry only declared transport or response-contract failures;
- persist immutable request, response, result, usage, and attempt manifests;
- write attempt manifests last so interrupted directories remain detectable;
- count incomplete attempts conservatively toward the retry cap;
- rebuild a missing checkpoint from validated immutable attempt manifests;
- reject checkpoint, manifest, referenced-artifact, config, plan, and runner
  identity tampering;
- support a bounded `max_jobs` pause without changing the sealed plan.
- require one active writer per run root; concurrent writers are unsupported
  and fail closed if an attempt directory has already been claimed.

The fixture request uses opaque candidate slots and contains no arm identity.
The fixture result contains no score, winner, quality judgment, prompt, or
rubric. Those remain Phase B1 decisions.

### Phase B0-C - deferred

- deterministic aggregation by method;
- missing/failed denominator reporting;
- confidence interval hooks where the method supports them;
- compose and seal `FullRunReportV1` without changing its public schema.

### Phase B1 - requires explicit API allocation

- author and freeze scorer-specific prompts;
- configure provider/model/quota bucket;
- dry-render and cost preflight;
- live SF-QE, SF-BT, and PJ calls.

## 5. EvaluationRunConfigV1

The config is a content-addressed preregistration artifact. Its closed root
contains:

- schema identity and version;
- config ID, creation time, and Evaluation producer identity;
- exact typed source binding, canonical or explicitly offline legacy;
- selected arm IDs plus exact translation artifact, attempt, and profile-config
  hashes;
- method rows;
- explicit comparison pairs;
- block unit/context policy;
- blinding mode and seed;
- transport retry ceiling;
- config self-hash.

Each method declares:

- `method_id` and `method_version`;
- `scorer_kind`: `unary` or `pairwise`;
- `profile_scope`: `common` in this task;
- eligible admissions, limited to `translate` and `translate_structured`.

Pairwise jobs are generated only for declared comparison pairs. The planner
never expands N arms into every possible pair by itself.

## 6. Unit and context policy

The scoring unit in v1 is one canonical source block. Neighbor context is
selected mechanically within the same chapter using configured counts before
and after the active block.

The active block remains explicit. Neighbor blocks do not become additional
scored units and cannot cross a chapter boundary.

The planner records block IDs only. A later scorer renderer will retrieve the
source and arm-specific target text from the immutable common read model.

## 7. TranslationArtifactV1 and coverage semantics

`TranslationArtifactV1` contains one row for every source block in the sealed
admitted universe. Its closed statuses are:

- `translated`;
- `preserved`;
- `excluded`;
- `review_held`;
- `missing`;
- `failed`.

Its coverage object records eligible count, all six outcome counts, and total
source rows. Validation
recomputes these facts and rejects duplicate/foreign IDs, source or admission
hash drift, unknown arm/attempt identity, wrong source order, invalid text/error
combinations, and totals that do not reconcile.

Admission and status must agree mechanically:

- `translate` / `translate_structured` -> `translated`, `missing`, or `failed`;
- `preserve` -> `preserved`, with target text byte-equal to source;
- `exclude` -> `excluded`;
- `review_required` -> `review_held`.

Only status `translated` is ready for common semantic scoring. Missing and
failed eligible rows become blocked jobs; preserved, excluded, and review-held
rows remain visible coverage but are not semantic-score units.

One-arm input may produce unary jobs. It cannot produce a pairwise job or a
comparison claim.

Public producer authority is exactly `d2l|literary`. Resealing an artifact after
changing its producer to `evaluation` must still fail validation. Public
artifacts require `canonical_source_package_v1`; legacy D2L artifacts remain
offline compatibility data and cannot pass the public validator.

## 8. Blinding and fairness

Pairwise methods use opaque candidate slots. The mapping from real arm IDs to
candidate A/B is internal and deterministic.

For each comparison pair, eligible units alternate orientation. The seed only
chooses the first orientation. Therefore the A/B count differs by at most one,
instead of relying on an unbalanced random hash sample.

The following are forbidden:

- scorer-visible system/model/provider labels;
- automatically treating a human translation as infallible gold;
- retrying because a score or verdict is undesirable;
- silently removing invalid or missing rows;
- changing block selection after seeing results;
- combining metric values into a composite before weights are preregistered.

## 9. Deferred decisions

This task intentionally does not decide:

- provider, model, prompt, or quota bucket;
- SF-QE / SF-BT / PJ prompt content;
- composite score or cross-metric weights;
- confidence interval method;
- human-reference calibration policy;
- Console or Cockpit presentation.

These decisions require scorer evidence and/or API allocation. Deferring them
does not block the offline planner.

## 10. Exact write sets

### Phase B0-A

Only these files may be added:

1. `THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_PHASE_B_OFFLINE_ORCHESTRATOR_V1.md`
2. `THESIS_RUNTIME_TOOL/pipeline/eval/common_input_v1.py`
3. `THESIS_RUNTIME_TOOL/pipeline/eval/offline_orchestrator_v1.py`
4. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_common_input_v1.py`
5. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_offline_orchestrator_v1.py`
6. `THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/evaluation_v1/evaluation_run_config_valid.json`

### Phase B0-B

Only these additional changes are authorized:

1. add `THESIS_RUNTIME_TOOL/pipeline/eval/offline_runner_v1.py`;
2. add `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_offline_runner_v1.py`;
3. update this task ledger.

## 11. Hard exclusions

Do not edit:

- `pipeline/eval/contracts_v1.py`;
- `pipeline/eval/d2l_input_v1.py`;
- `pipeline/eval/full_run_report_v1.py`;
- `pipeline/eval/__init__.py`;
- held scorer/CLI files including `builder_gold.py`,
  `occurrence_adherence.py`, `term_policy.py`, `d2l_translate_score.py`, and
  `score_run.py`;
- D2L or Literary runtime/exporter code;
- Input Normalization, App/backend, UI, Console, or Cockpit files;
- SQLite, checkpoint, cache, or live report data.

## 12. Acceptance

Phase B0-A passes only if tests prove:

1. D2L projection validates first and does not mutate the input.
2. Common input preserves source block order and exact translation text.
3. D2L terms/injection data cannot enter the common read model.
4. Translation artifacts and configs are closed, reject invalid data, and
   self-hash.
5. Config list canonicalization distinguishes set-like fields from semantic
   sequences.
6. Stale source/admission/artifact hashes, duplicate or foreign block IDs, and
   unknown arm/attempt references fail closed.
7. Same input plus config yields byte-equivalent plan projections and hash.
8. Neighbor context stays in chapter and preserves source order.
9. One-arm input creates unary jobs and no pairwise jobs.
10. Pairwise jobs use only explicit pairs and A/B orientation is balanced.
11. Missing/failed eligible translations become blocked jobs; preserved,
    excluded, and review-held rows remain explicit coverage.
12. Source input, config input, and returned structures are not mutated.
13. No API, provider SDK, DB, App, scorer, gold, or runtime callback import is
    introduced.
14. An Evaluation-authored artifact remains invalid after a correct reseal.
15. Legacy and canonical bindings reject cross-kind relabel/substitution.
16. Resealed drift in each canonical component hash fails the exact source join.

Phase B0-B additionally passes only if:

17. A paused run resumes without repeating a successful job.
18. Repeating a complete run is byte-idempotent.
19. Blocked jobs create no scorer attempt.
20. Only transport and response-contract failures consume retries.
21. One exhausted job does not stop independent jobs.
22. Missing checkpoints rebuild from attempt manifests without re-execution.
23. Incomplete attempts are preserved and count toward the retry cap.
24. Checkpoint, result artifact, config, plan, and runner identity tampering fail
    closed.
25. Fixture requests are arm-blind and fixture results contain no score.
26. Attempt directories exact-cover a contiguous sequence from one and cannot
    exceed the sealed retry cap.
27. Resealing a manifest around semantically altered fixture artifacts still
    fails the closed artifact contract.

## 13. Milestone and integration note

This task adds Evaluation-side validation for `TranslationArtifactV1` and a new
preregistration artifact, but does not change `D2LEvaluationInputV1` or
`FullRunReportV1` and does not wire a producer. At a clean commit milestone,
Evaluation reports both identities and planner behavior once for cross-workstream
review. D2L and Literary remain owners of their eventual artifact writers.

The DEC-017 rework retains the Phase B0-A six-file write set. Phase B0-B adds
only the three files declared in section 10 and leaves accepted
`D2LEvaluationInputV1` and `FullRunReportV1` semantics unchanged.

## 14. Verification

- Phase B0-B runner probes: `19 passed`.
- Combined Evaluation contract/planner/runner gate: `95 passed`.
- Full runtime suite: `970 passed, 1 skipped`, plus two known failures because
  untracked frozen DB `data/jobs/d2l_p1/memory.sqlite3` is absent from this
  isolated worktree.
- Applicable full suite excluding only those two absent-file probes:
  `970 passed, 1 skipped, 2 deselected`.
- `py_compile`, line-length scan, key/import scan, whitespace, and exact
  write-set checks pass.
