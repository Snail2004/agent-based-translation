# TASK_EVAL_SHARED_LLM_ADAPTER_PHASE3_V1

Status: COMPLETE, 0-API

Author/Implementer: CodeX

## 1. Objective

Adopt the neutral shared LLM backend for Evaluation's established semantic LLM
roles without moving deterministic scoring, packet construction, aggregation,
reporting, or semantic authority into the shared layer.

The shared backend executes one physical attempt. Evaluation renders the
existing prompt, validates the response locally, and decides semantic
acceptance. No adapter performs a retry, fallback, key rotation, provider
switch, or model switch.

## 2. Canonical Git dependency

- accepted neutral anchor:
  `a5377513795eebad147715ba969d16701e4558a5`;
- anchor parent:
  `1d72e195e75d3a3d78a09f22c251bb25e16f17fc`;
- anchor delta: exactly 19 shared-core/test/task files;
- all 19 blobs match the accepted source core at `8cd92b5`;
- rejected local merge:
  `84daecc4289f1f24c797b5870706f25e6928b90d`.

The rejected merge has correct selected bytes but false Literary ancestry. It
must not enter integration history.

## 3. Migrated semantic roles

| Role ID | Evaluation responsibility | Shared responsibility |
|---|---|---|
| `evaluation.sf_bt.back_translator` | Existing reverse prompt and strict one-field response validation | One sealed physical attempt and transport evidence |
| `evaluation.sf_bt.semantic_judge` | Existing blind semantic prompt and closed score/flag/note validator | One sealed physical attempt and transport evidence |
| `evaluation.pj.judge` | Existing blind counterbalanced PJ prompt and closed verdict/tag/note validator | One sealed physical attempt and transport evidence |

The following are deliberately not shared-backend roles:

- `SF-QE`: local COMET scorer;
- SF-BT embedding diagnostic: local embedding model;
- scorer packet builders;
- plan builders and checkpoint reconciliation;
- metric calculators and aggregators;
- FullRunReportV1 composition;
- D2L-specific held scorers and CLIs under DEC-009.

## 4. Profile boundary

`llm_profiles_v1.py` owns pipeline-specific role values:

- role and recommended preset IDs;
- prompt IDs and accepted prompt hashes;
- closed response schemas and local validator bindings;
- context, input, output, timeout, and aggregate token caps;
- role-specific output/checkpoint/cache namespaces;
- no fallback and no in-seal retry.

It does not choose or discover a provider, model, base URL, credential, quota
bucket, or capability. Callers must supply a complete source/capability target
whose exact records are later resolved by the shared core.

Evaluation API allocation remains UNALLOCATED. There is no live profile in
this task.

## 5. Adapter boundary

`llm_adapter_v1.py`:

1. verifies the sealed role, prompt, response schema, validator, packet hash,
   rendered prompt hash, and transport request body hash;
2. builds the protocol request body from the sealed source/capability and
   Evaluation-owned role values;
3. invokes `SharedLlmBackend.execute_one_attempt()` exactly once with primary
   target index zero and retry ordinals fixed at their first value;
4. extracts the semantic response from the provider envelope;
5. applies the existing local role validator;
6. returns either `accepted` or `semantic_rejected`.

A semantic rejection never triggers another call. Any retry requires a newly
resolved seal and a new logical request. A valid low score or tie is an
accepted result, not a retry condition.

The adapter exposes explicit cache modes: `bypass`, `read_only`, or
`read_write`. It does not own a separate cache, ledger, quota scheduler,
credential loader, output directory, or checkpoint.

## 6. Provider-neutrality and hard-code removal

The migrated path contains no bearer token, API key, base URL, physical quota
bucket, concrete model ID, retry loop, key file lookup, environment lookup,
SQLite cache, or fallback policy.

Protocol request envelopes cover the shared core's declared protocols:

- OpenAI-compatible Chat Completions;
- OpenAI Responses;
- Google GenerateContent;
- injected local in-process transport.

Legacy `score_sf_bt.py`, `score_pj.py`, and their direct clients remain
untouched historical evidence. They are not the migrated adapter and are not
authorized as the future shared-backend execution path.

## 7. Gold and runtime isolation

- No gold, oracle, human reference, expected answer, result callback, score
  override, or Evaluation result may enter a shared runtime profile, source,
  capability, or input binding.
- Prompt renderers continue to hide candidate producer identity and artifact
  metadata.
- Canonical source and TranslationArtifact inputs remain immutable.
- Evaluation outputs remain one-way and never mutate translation memory.

## 8. First gate

The gate is strictly offline with injected transport:

- all three role outputs accepted;
- protocol-specific request bodies deterministic and secret-free;
- transport failure persisted once, with no hidden retry;
- exact cache hit avoids the second physical call;
- duplicate physical attempt rejected when cache is bypassed;
- stale seal and foreign prompt rejected before transport;
- semantic schema failure returned as `semantic_rejected`;
- incomplete finish reason returned as `semantic_rejected`;
- non-finite cost rejected before transport;
- non-finite provider usage rejected and recorded as transport failure;
- forbidden human-reference input binding rejected by the shared seal.

No API call, credential read, source/translation mutation, memory DB write, App
edit, or pipeline checkpoint migration is permitted.

## 9. Owned implementation files

- `pipeline/eval/llm_profiles_v1.py`
- `pipeline/eval/llm_adapter_v1.py`
- `pipeline/tests/test_evaluation_llm_adapter_v1.py`
- `tasks/TASK_EVAL_SHARED_LLM_ADAPTER_PHASE3_V1.md`

No shared-core file is edited after the neutral anchor merge.

## 10. Verification

- focused adapter/shared-core/prompt-contract gate: `186 passed`;
- complete `pipeline/tests` gate: `1229 passed`;
- the complete gate used a temporary read-only byte-identical frozen DB mount;
- frozen DB SHA-256 before and after:
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- the temporary DB copy was removed after the gate;
- API/network calls: 0;
- source, translation, memory DB, checkpoint, App, and shared-core edits: 0.
