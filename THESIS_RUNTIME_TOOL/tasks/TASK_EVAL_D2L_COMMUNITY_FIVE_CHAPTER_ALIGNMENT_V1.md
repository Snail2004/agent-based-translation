# TASK_EVAL_D2L_COMMUNITY_FIVE_CHAPTER_ALIGNMENT_V1

Status: IMPLEMENTED - REAL MANUAL AUDIT PENDING

Owner: Evaluation workstream

## 1. Objective

Normalize the pinned D2L Vietnamese community translation against the finalized
five-chapter canonical source package without rewriting translation text or
pretending that Markdown block boundaries are semantic truth.

The result is an Evaluation-only alignment sidecar. It never enters translation
runtime, memory, prompts, or source artifacts.

## 2. Exact implementation scope

1. `THESIS_RUNTIME_TOOL/pipeline/eval/d2l_community_five_chapter_v1.py`
2. `THESIS_RUNTIME_TOOL/pipeline/tests/test_evaluation_d2l_community_five_chapter_v1.py`
3. `THESIS_RUNTIME_TOOL/tasks/TASK_EVAL_D2L_COMMUNITY_FIVE_CHAPTER_ALIGNMENT_V1.md`

No App, D2L runtime, input-normalization, shared LLM, database, or public report
contract is changed.

## 3. Frozen real inputs

Source:

- project: `d2l_run-5-chapter`;
- source lifecycle: `finalized_pre_run`;
- finalization payload SHA-256:
  `ca701a5ac371cff1122488d406518eb0a7a890d9f3268ef3cbf500db1aacf0c0`;
- candidate tree SHA-256:
  `c481f207b63bc6a7829738937531366a14ae6461c4194fba2ee274518e689ca6`.

Community target:

- repository commit:
  `c775d6b4998e6243ec5d11f950e67679555a2c74`;
- arm identity: `community_unverified`;
- every target segment is stored with its exact UTF-8 text hash.

Selected chapters, in order:

1. `d2l_preliminaries`
2. `d2l_linear_networks`
3. `d2l_multilayer_perceptrons`
4. `d2l_deep_learning_computation`
5. `d2l_convolutional_neural_networks`

## 4. Source and target counts

| Chapter | Eligible source blocks | Community target segments |
|---|---:|---:|
| Preliminaries | 348 | 350 |
| Linear Networks | 336 | 336 |
| Multilayer Perceptrons | 475 | 475 |
| Deep Learning Computation | 199 | 195 |
| Convolutional Neural Networks | 206 | 206 |
| **Total** | **1,564** | **1,562** |

Every selected source block equals the exact text parsed from its pinned
`_origin.md` sibling. There is no observed source-version drift.

## 5. Manual boundary decisions

Exactly four non-1:1 groups are required:

1. `d2l_preliminaries_lookup_api_b001` maps to target `t001+t002`.
   The source Markdown combines a heading and prose while the Vietnamese target
   separates them.
2. `d2l_preliminaries_ndarray_b103` maps to target `t103+t104` for the same
   heading-plus-prose boundary difference.
3. `d2l_deep_learning_computation_model_construction_b017..b019` maps to target
   `t017`. The target keeps the MXNet/TensorFlow tab wrapper and prose in one
   Markdown segment.
4. `d2l_deep_learning_computation_model_construction_b020..b022` maps to target
   `t018`. The target keeps the PyTorch tab wrapper and prose in one segment.

These answers are data in a sealed manual decision artifact. They are not
hard-coded into matching logic. Generic code validates cardinality, contiguous
order, exact IDs, source/target hashes, and exact cover.

## 6. Acceptance protocol

The four manual boundary mappings are `reviewed`.

The remaining 1,556 candidate mappings stay `review_required` until a
deterministic, method-neutral sample is manually checked:

- sample size: `max(30, ceil(population * 0.10))`;
- actual five-chapter target: 156 rows;
- first and last row of every section are mandatory;
- remaining rows are selected by SHA-256 rank over sealed manifest identities;
- no model output, score, expected winner, S0, or S1 enters selection.

If one sampled row fails, every regular mapping in that row's section stays
`review_required`. Other sections may be promoted to `auto_accepted` with
confidence `1.0`, meaning all preregistered alignment gates passed, not a
probability and not a translation-quality score.

## 7. Output

The writer emits a content-addressed Evaluation evidence root containing:

- `manual_decision.json`;
- `audit_plan.json`;
- `audit_record.json`;
- exact target snapshots for every chapter;
- one `AlignmentManifestV1` per chapter;
- `alignment_bundle.json` and physical file hashes.

Target text is copied byte-for-byte from the pinned Community repository. The
writer does not split, merge, rewrite, translate, or normalize Vietnamese text.

The bundle is not converted into a per-source-block benchmark overlay when a
mapping is `1:N` or `N:1`. That later bridge must use common aligned units or a
separately reviewed resegmentation; it may not fabricate block-local text.

## 8. Hard exclusions

- no API, embedding, semantic matcher, fuzzy fallback, or language heuristic;
- no use of S0/S1 to align Community;
- no mutation of source or Community repositories;
- no DB, checkpoint, App, or runtime write;
- no label claiming the Community arm is human truth;
- no accepted bundle before the manual sample record exists.

## 9. Offline gates

- closed self-hashed manual decision, audit plan, and audit record;
- source/component/finalization hash drift rejection;
- foreign, reordered, duplicate, or non-contiguous override rejection;
- source and target exact cover;
- Resume-independent deterministic sampling;
- failed sample holds the complete section;
- content-addressed output immutability;
- exact Community target text preservation.
