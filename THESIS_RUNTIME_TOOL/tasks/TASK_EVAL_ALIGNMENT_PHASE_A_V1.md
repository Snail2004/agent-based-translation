# TASK_EVAL_ALIGNMENT_PHASE_A_V1

Status: COMPLETE - 0 API

Owner: Evaluation workstream

Parent decision:
`THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_BASELINES_ALIGNMENT_V1.md`

## 1. Objective

Build the internal, fixture-only alignment boundary required to compare
block-addressed machine translations with an external Human translation that
may split, merge, add, or omit paragraphs.

This phase validates structure only. It does not infer semantic alignment.

## 2. Deliverables

1. Closed, self-hashed `AlignmentManifestV1`.
2. Immutable target-segment snapshot with exact-text hashes.
3. Exact source read-model binding.
4. Deterministic `CommonEvaluationUnitV1` projection.
5. Explicit `1:1`, `1:N`, `N:1`, `N:M`, `missing`, `added`, and
   `ambiguous` fixture coverage.
6. Fail-closed tests for overlap, foreign IDs, non-monotonic order, hash drift,
   duplicate IDs, inconsistent counts, non-finite values, and input mutation.

## 3. Exact write set

Only these files may be added:

1. `THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_ALIGNMENT_PHASE_A_V1.md`
2. `THESIS_RUNTIME_TOOL/pipeline/eval/alignment_manifest_v1.py`
3. `THESIS_RUNTIME_TOOL/pipeline/eval/common_units_v1.py`
4. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_alignment_manifest_v1.py`
5. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_common_units_v1.py`
6. `THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/evaluation_v1/alignment_mixed_case.json`

## 4. Hard exclusions

Do not edit:

- `pipeline/eval/contracts_v1.py`;
- `pipeline/eval/common_input_v1.py`;
- `pipeline/eval/full_run_report_v1.py`;
- `pipeline/eval/offline_orchestrator_v1.py`;
- `pipeline/eval/offline_runner_v1.py`;
- `pipeline/eval/__init__.py`;
- scorer, prompt, provider, D2L, Literary, Input Normalization, App, DB, or UI
  files.

No API, model, embedding, DB, checkpoint, baseline generation, or Human text
rewriting is allowed.

## 5. Structural invariants

1. Source authority is the exact Evaluation common read model, not `block_id`
   alone.
2. Target authority includes artifact identity plus exact ordered segment hashes.
3. Eligible source blocks and target segments are each covered exactly once.
4. Source and target spans are contiguous and monotonic within one chapter.
5. `missing` consumes source only; `added` consumes target only.
6. Accepted/review/ambiguous mappings consume both source and target.
7. Cardinality must match the declared mapping kind.
8. Manifest validation and unit projection do not mutate caller data.
9. Text is retained as exact immutable parts; projection never rewrites it.
10. Added target content remains coverage evidence and never becomes a source
    scoring unit.

## 6. Acceptance

The phase passes only if:

1. all seven mapping kinds/states in the mixed fixture validate;
2. source and target tampering fail even after manifest resealing;
3. source overlap, target overlap, foreign IDs, and order reversal fail;
4. unknown fields and non-finite confidence fail;
5. declared coverage is recomputed and checked;
6. common units preserve source order and exact source/target text parts;
7. machine-arm missing/failed states remain explicit;
8. Human missing/review/ambiguous states are not score-ready;
9. repeated projection is deterministic;
10. focused and applicable full tests pass with no new regression.

## 7. Deferred

- semantic alignment algorithm;
- multilingual embedding model;
- auto-accept confidence threshold;
- Human review UI or review artifact writer;
- real D2L and Literary alignment;
- public alignment contract;
- scorer integration and report projection.

After this phase, inventory the two translated D2L chapters and build a separate
real-data pilot without changing this frozen fixture contract.

## 8. Verification record

Completed on 2026-07-18:

- focused alignment tests: `11 passed`;
- Evaluation contract/orchestrator/runner group: `72 passed`;
- full repository suite: `981 passed, 1 skipped, 2 failed`;
- both full-suite failures require the absent frozen fixture
  `THESIS_RUNTIME_TOOL/data/jobs/d2l_p1/memory.sqlite3` and are not caused by
  this write set;
- the applicable-suite rerun exposed one unrelated Windows multiprocessing
  lock cleanup race; its exact test passed immediately when rerun alone;
- Python compilation, long-line scan, credential scan, whitespace scan, and
  exact-write-set inspection passed;
- no API, model, DB, checkpoint, public contract, scorer, runtime, or App path
  was used or changed.
