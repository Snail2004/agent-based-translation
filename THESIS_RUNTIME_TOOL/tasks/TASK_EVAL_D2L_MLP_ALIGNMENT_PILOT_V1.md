# TASK_EVAL_D2L_MLP_ALIGNMENT_PILOT_V1

Status: COMPLETE - 0 API; SEALED D2L INPUT PENDING

Owner: Evaluation workstream

Depends on:

- `TASK_EVAL_BASELINES_ALIGNMENT_V1.md`;
- `TASK_EVAL_ALIGNMENT_PHASE_A_V1.md`;
- a sealed D2L producer package for `d2l_multilayer_perceptrons`.

## 1. Objective

Prepare a structural, review-held alignment pilot for one D2L chapter:
`d2l_multilayer_perceptrons`.

The pilot reads the Vietnamese Markdown snapshot at the same pinned repository
commit as the English source, preserves exact target bytes, and produces only
candidate `1:1` mappings. It does not claim that structural equality proves
semantic alignment.

The Vietnamese repository output is labeled `community_unverified`. It is not a
Human gold reference and must not be presented as one.

## 2. Exact write set

Only these files may be added:

1. `THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_D2L_MLP_ALIGNMENT_PILOT_V1.md`
2. `THESIS_RUNTIME_TOOL/pipeline/eval/d2l_community_alignment_v1.py`
3. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_d2l_community_alignment_v1.py`

## 3. Hard boundaries

- fixture and local-file tests only;
- no API, model, embedding, DB write, checkpoint, scorer, or App change;
- no D2L producer artifact is authored by Evaluation;
- no edit to public Evaluation contracts or `pipeline/eval/__init__.py`;
- no semantic auto-acceptance threshold;
- no source or target text rewriting;
- no dynamic-programming or fuzzy fallback in this milestone.

## 4. Target snapshot

The adapter:

1. reads one exact chapter directory;
2. follows section order declared by `index.md`;
3. splits Markdown deterministically while preserving fenced code and display
   math blocks;
4. retains only target blocks eligible for translation comparison: headings and
   prose;
5. hashes every exact UTF-8 target string and every source file;
6. builds `AlignmentTargetSnapshotV1` with arm
   `community_unverified`.

The adapter does not inspect S0, S1, scores, gold, or expected winners.

## 5. Structural candidate planner

The planner accepts an already validated `CommonEvaluationInputV1` plus the
target read model. It derives only the documented D2L block-address structure:

```text
<chapter_id>_<section_slug>_bNNN
```

For every eligible source block, the target row must have the same:

- chapter;
- section;
- original block position inside that section;
- structural block type (`heading` or `prose`).

When every row matches, the planner emits exact-cover `1:1` mappings with
`decision_state=review_required` and no semantic confidence. It then validates
the resulting `AlignmentManifestV1` against the exact source and target hashes.

Any missing section, added eligible target row, count mismatch, type mismatch,
foreign ID, duplicate ID, or order mismatch fails closed. Preliminaries is
therefore intentionally deferred because two sections contain structural
insertions.

## 6. Acceptance

1. Equal MLP-like structure produces deterministic review-held mappings.
2. Exact source and target text bytes remain unchanged.
3. Target artifact and segment hashes change after byte drift.
4. Added target prose fails closed instead of being guessed into a mapping.
5. Type, section, and block-position drift fail closed.
6. All mappings remain unavailable to semantic scoring until reviewed or an
   independently preregistered auto-accept policy is approved.
7. Existing Evaluation tests remain green.

## 7. Input still required from D2L

Evaluation cannot run the real common-unit projection until D2L exports a sealed
`D2LEvaluationInputV1` package for the MLP chapter containing the immutable
source universe and the S0/S1 translation artifacts. Reading the legacy SQLite
for inventory does not grant Evaluation producer authority.

## 8. Verification record

Completed on 2026-07-18:

- focused adapter/planner probes: `6 passed`;
- Evaluation contract/alignment/orchestrator group: `78 passed`;
- applicable repository suite: `987 passed, 1 skipped, 2 deselected`; the two
  deselected probes require the absent frozen SQLite fixture already recorded
  by the preceding phase;
- real target snapshot read `11` pinned community Markdown files and retained
  exactly `475` comparable target segments;
- read-only comparison against the existing S1 MLP rows found `475` eligible
  source blocks and exact equality of the complete
  `(section_slug, block_order_in_section, block_type)` sequence;
- exact target identities:
  - artifact SHA-256:
    `8227f7b1b1d29b847db32b1e1ac2ed2c04a016327ff0cd97f9d78ffedd9892bb`;
  - segment-set SHA-256:
    `6f60321a41b612f54329e88bf6f5f921df6ec681ce4778d303c2bf12556a8de1`;
  - file-set SHA-256:
    `8dab821df9a38d39bbd053233f2114b0be5f16676efa80cf64ca4a17029f2657`;
- no semantic mapping was accepted and no API or DB write occurred.
