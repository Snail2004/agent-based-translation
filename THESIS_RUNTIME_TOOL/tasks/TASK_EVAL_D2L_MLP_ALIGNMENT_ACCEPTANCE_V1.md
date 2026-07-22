# TASK_EVAL_D2L_MLP_ALIGNMENT_ACCEPTANCE_V1

Status: COMPLETE - 0 API; SEALED D2L INPUT AND AUDIT RECORD PENDING

Owner: Evaluation workstream

Parent: `TASK_EVAL_D2L_MLP_ALIGNMENT_PILOT_V1.md`

## 1. Objective

Preregister the minimum evidence required before the exact-structure MLP
alignment may move from `review_required` to `auto_accepted`.

This policy validates alignment only. It does not judge translation quality.

## 2. Exact write set

1. `THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_D2L_MLP_ALIGNMENT_ACCEPTANCE_V1.md`
2. `THESIS_RUNTIME_TOOL/pipeline/eval/d2l_community_alignment_v1.py`
3. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_d2l_community_alignment_v1.py`

## 3. Acceptance gates

All gates are mandatory:

1. The community repository commit is pinned.
2. Every selected source block matches the exact UTF-8 text parsed from the
   sibling `_origin.md` file at the same section and block position.
3. All eligible source and target rows have identical chapter, section,
   original block position, and block type.
4. The full source and target structural sequences are equal.
5. A deterministic audit sample passes manual alignment review.

Failure of gates 1-4 keeps the entire chapter `review_required`. A failed audit
row keeps every mapping in that row's section `review_required`; unaffected
sections may be reconsidered only in a new sealed manifest.

## 4. Audit sampling policy

Policy ID: `d2l_structural_alignment_audit_v1`

- population: every structural `1:1` mapping in the sealed MLP manifest;
- target sample size: `max(30, ceil(population * 0.10))`;
- mandatory rows: first and last mapping of every section;
- remaining rows: lowest SHA-256 ranks over the sealed source/target identities,
  policy ID, and mapping ID;
- no score, translation quality, arm identity, or expected winner enters sample
  selection;
- repeated selection over identical inputs must be byte-identical.

For the observed population of 475 mappings, the preregistered sample size is
48 mappings.

## 5. Auto-accept boundary

Passing structure plus sample audit permits `decision_state=auto_accepted` only
for alignment. Confidence `1.0` means all deterministic paired-repository gates
passed; it is not a probability and not a translation-quality score.

The audit record remains separate evaluation evidence. It must identify the
exact manifest, source read-model hash, target segment-set hash, policy version,
origin file-set hash, sample IDs, and pass/fail result before an accepted
manifest is emitted.

## 6. Hard exclusions

- no API, model, embedding, scorer, or DB write;
- no fuzzy or semantic fallback;
- no use of S0/S1 output text for alignment acceptance;
- no gold/reference leakage into translation runtime;
- no edit to public Evaluation contracts or App files;
- no acceptance before the audit packet is reviewed.

## 7. Verification record

Completed on 2026-07-18:

- focused origin/alignment/audit probes: `10 passed`;
- Evaluation contract/alignment/orchestrator group: `99 passed`;
- applicable repository suite: `991 passed, 1 skipped, 2 deselected`; the two
  deselected tests require the absent frozen SQLite fixture already recorded by
  the parent pilot;
- the pinned real MLP snapshot contains `11` target files, `11` sibling origin
  files, `475` comparable target segments, and `475` comparable origin rows;
- exact target file-set SHA-256 remains
  `8dab821df9a38d39bbd053233f2114b0be5f16676efa80cf64ca4a17029f2657`;
- exact origin file-set SHA-256 is
  `ad789714edc5bd12a401b97d3a121a26802e4abb3562929f627d85afa5a62c6b`;
- the sample planner is deterministic, exact-cover audited, and routes a failed
  sample to its complete source section;
- no accepted alignment manifest or audit result was emitted. Final sample IDs
  remain intentionally unsealed until D2L supplies the exact
  `D2LEvaluationInputV1` source package.
