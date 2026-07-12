# TASK_LIT_M4f_P1S3 - Builder v3 orchestration path - Phase 1, Step 3

Status: **DRAFT rev4 (Claude -> Terra/CodeX), 2026-07-12.** rev3 folds the second independent task review: stage-scoped sentinel policy, immutable/auditable executor boundary, separate request contract/fingerprint hashes, JSON-safe state, deterministic checkpoint identity, K=2 resume sourcing, complete reference coverage, and a real atomic generation publish. rev4 (Claude, on independent verify of rev3): pins the DETERMINISTIC `window_id` scheme + B2 request-section list order - a determinism gap that would have broken the request fingerprint / checkpoint identity / straight-vs-resume test. Builds on Step-2 (ACCEPTED, `main` @ `8edac45`). Implementer = Terra. Verify gate = Claude, adversarial + integration.

Contract source: `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` sections 1-5 and 8; `tasks/TASK_LIT_M4f_CANONICAL_V1.md` sections 2, 3, 9, and 10. This task is the implementation contract for Step 3. If any required wire shape or invariant below remains ambiguous, STOP and ask; do not invent a local policy.

---

## 0. Scope and fixed decisions

Build a parallel Builder-v3 orchestration path. Legacy Builder remains untouched and remains the only live/default path. Real v3 prompts, CLI/estimator/API routing, B4, and switching defaults remain Step 6 or later.

Step 3 is programmatic and 0-API. It uses a deterministic synthetic executor through the same semantic request/transport interface that the future LLM executor must implement.

### 0.1 Mode and window locks

- `knowledge_mode = "whole_book_frozen"` is the only accepted mode. Any other value is a hard error before rendering a request.
- Reuse the locked active-window configuration: `window_target_tokens=500`, `window_max_blocks=8`.
- In frozen mode, B1/B2 context tails are audited two-sided tails: up to `tail_k=2` previous and `tail_k=2` next blocks in source order. They are CONTEXT_ONLY and never valid evidence/output rows.
- `summary_k=2` is a separate setting for B3 prior rolling summaries. Do not conflate `tail_k` with `summary_k`.
- Active windows exact-cover the chapter's non-heading blocks (`block_type in {paragraph, dialogue}`): every such block belongs to exactly one active window. Heading/structural blocks may be carried in source views but are excluded from exact-cover accounting.

### 0.2 Exact programmatic APIs

Implement these public functions; do not add a CLI flag or modify legacy `run_m1`/`run_m2`:

```python
def run_m1_v3(
    document, chapters, *, executor, out_dir,
    knowledge_mode="whole_book_frozen",
    execution_mode="synthetic",
    window_target_tokens=500,
    window_max_blocks=8,
    tail_k=2,
    resume=False,
) -> dict: ...

def run_m2_v3(
    document, chapters, *, executor, out_dir, m1v3_dir,
    digest_context=None,
    knowledge_mode="whole_book_frozen",
    execution_mode="synthetic",
    summary_k=2,
    resume=False,
) -> dict: ...
```

For Step 3, both functions hard-reject `execution_mode != "synthetic"`. Checkpoint validation must nevertheless support an expected `execution_mode="llm"` so the synthetic-to-LLM cross-load rejection can be tested. Step 6 will implement the actual LLM executor and enable that mode.

Each report must contain at least: `milestone` (`M1V3` or `M2V3`), `status`, requested/selected/restored/ran chapters, execution and knowledge modes, contract versions, validation counters, request-manifest hashes, semantic-state hashes, checkpoint paths, and the exact stopping error when failed.

### 0.3 Hard non-scope

Do not edit prompts, legacy orchestration, legacy checkpoint behavior, CLI, estimator, API client, app-E12 files, or the frozen D2L DB (`64D989...`). No LLM/API/network. Do not call `seed_entity_ledger_from_chapter_brief`, `update_entity_ledger_from_lexicon`, `render_chapter_brief_for_injection`, or any entity/alias registry/roster helper from v3. An occurrence roster is allowed and is not an entity roster.

---

## A. Deliverables and pinned versions

Create new files beside the legacy path:

1. `pipeline/literary/builder_v3_pipeline.py`
2. `pipeline/literary/checkpoint_v3.py`
3. `pipeline/tests/test_builder_v3_pipeline.py`
4. Optional JSON fixtures only under `pipeline/tests/fixtures/builder_v3/`

Do not modify `builder_pilot.py` or generic `checkpoint.py` for routing. Import their read-only helpers where appropriate. If a generic helper truly must change, STOP and return the reason for gate review first.

Pin these exact constants in the new modules:

```text
BUILDER_SCHEMA_V3 = "v3"
M1_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m1_checkpoint_v3"
M2_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m2_checkpoint_v3"
VALIDATOR_CONTRACT_VERSION = "literary_builder_v3_validator_contract_v1"
SOURCE_ANCHOR_VERSION = "literary_source_anchor_v1"
CONTEXT_POLICY_VERSION = "literary_builder_v3_context_policy_v1"
REQUEST_CONTRACT_VERSION = "literary_builder_v3_request_contract_v1"
SYNTHETIC_EXECUTOR_VERSION = "literary_builder_v3_synthetic_executor_v1"
```

---

## B. Canonical wire DTOs (JSON primitives only)

All values written to requests, audit records, state, artifacts, or checkpoints must contain only JSON primitives. Never persist tuple dict keys, dataclass instances, `Path`, `SourceAnchor`, or `SourceInterval` objects. Convert anchors/intervals with the Step-2 wire helpers first.

### B.1 Common input views

```text
SourceAnchorWire = {block_id:str, char_start:int, char_end:int}
SourceIntervalWire = {start:{block_order:int,char_offset:int}, end:{block_order:int,char_offset:int}}
BlockView = {block_id:str, order_index:int, block_type:str, text:str}
ContextBlockView = BlockView + {context_only:true, direction:"previous"|"next"}
```

`BlockView.text` is NFC(`clean_text` fallback `source_text`), exactly matching the Step-2 anchor string. Block lists are canonical source order. Unknown extra block fields are not copied into a request.

```text
WindowSpec = {
  window_id:str,
  active_block_ids:[str],
  previous_tail_block_ids:[str],
  next_tail_block_ids:[str],
  first_active_order:int,
}
```

Window specs are ordered by `(first_active_order, window_id)`.

`window_id` is minted **deterministically** as `w_<chapter_id>_<NN>`, where `NN` is the 1-based index of the window in `first_active_order` order within the chapter, zero-padded to >=2 digits. It MUST be identical across straight and resume runs and never derived from a uuid, timestamp, run id, or generation id - it enters the request fingerprint, the checkpoint identity hash, and the SyntheticStageExecutor script key, so a non-deterministic window_id breaks the straight-vs-resume identity equality (H.8) and makes the synthetic fixture unwritable.

### B.2 B0 -> B2 projection

Select a claim when the inclusive non-heading expansion of its validated `scene_range` intersects the active non-heading block-id set of the window. Never compare block-id strings lexically. Preserve the B0 request lineage: projecting fewer claims does not lower the transitive `input_max_order` of the B2 request.

```text
B0SceneClaimView = {
  cast_claim_id:str,
  surface:str,
  surface_kind:str,
  referent_kind_claim:str,
  role_hint:str,
  source_block_ids:[str],
  anchor:SourceAnchorWire,
  scene_range:[start_block_id:str,end_block_id:str],
  trust:"untrusted",
}
```

This is an allowed untrusted claim channel, not a witness and not an auto-resolution input. Step 3 must prove only that no code path converts it into an identity decision. Prompt-level handling of `trust="untrusted"` is gated in Step 6.

```text
WindowMentionView = {
  mention_id:str,
  surface:str,
  mention_type:str,
  referent_kind_claim:str,
  block_id:str,
  anchor:SourceAnchorWire,
}
```

**Canonical order of B2 request-section lists (determinism):** `b0_scene_projection` is ordered by `(min order_index over source_block_ids, cast_claim_id)`; `window_mentions` is ordered by `(block_order, anchor.char_start, mention_id)`. Every list placed in `allowlisted_sections` MUST have a pinned canonical order so the request fingerprint is reproducible.

### B.3 B0/B1/B2 -> B3 projections

```text
B0TypedProjection = {
  setting:dict,
  neutral_premise:{value:str, trust:"gist_only", evidence_eligible:false},
}

OccurrenceRosterRow = {
  id:str,
  occurrence_kind:"mention"|"endpoint",
  surface:str,
  referent_kind_claim:str,
  reference_scope:str|null,
  block_id:str,
  anchor:SourceAnchorWire,
}

EndpointCompact = {
  endpoint_id:str,
  surface:str,
  reference_scope:str,
  referent_kind_claim:str,
  mention_ref:str|null,
  attribution_method:str,
  block_id:str,
  anchor:SourceAnchorWire,
}

B2EventCompact = {
  event_id:str,
  event_type:str,
  block_id:str,
  evidence_quote:str,
  actor:EndpointCompact,
  target:EndpointCompact,
}

RollingSummaryView = {
  chapter_id:str,
  chapter_rolling_summary:str,
  source_m2v3_identity_hash:str,
  input_max_order:int,
}

RollingSummaryProvenance = RollingSummaryView + {
  source_m2v3_checkpoint_hash:str,
}
```

Occurrence rows are ordered by `(block_order, anchor.char_start, occurrence_kind, id)`. Event compact rows are ordered by their code-owned position/event id. Rolling summaries are ordered by absolute chapter order and must be exactly the preceding `summary_k` chapters that exist.

---

## C. Request contract, lineage, and executor boundary

### C.1 Static contract hash vs per-call fingerprint

Keep three separate identities:

1. `request_contract_hashes: {b0:hash,b1:hash,b2:hash,b3:hash}` - each is `canonical_hash(REQUEST_SHAPE_CONTRACT[stage])`, where the shape contract contains the stage's exact allowed section names, field schemas, required/nullability rules, canonical ordering rules, and `REQUEST_CONTRACT_VERSION`. It contains no runtime values and never contains its own hash.
2. `request_fingerprint` - per call, hash of the canonical full `StageRequestBody` below. It changes when any selected value, lineage row, contract version, execution mode/config, prompt ref, or upstream checkpoint identity hash changes.
3. `request_manifest_hash` - per chapter/stage checkpoint, hash of the canonical ordered list of all request fingerprints that produced the state.

Request-manifest order is fixed: M1V3 uses B0 first, then each window in `WindowSpec` order with B1 before B2; M2V3 contains its single B3 request. Do not sort fingerprints by hash.

### C.2 Stage request body and transitive lineage

```text
LineageRow = {
  source_channel:str,
  source_item_id:str,
  source_sha256:str,
  order_indices:[int],
  upstream_checkpoint_identity_hash:str|null,
}

StageRequestBody = {
  stage:"b0"|"b1"|"b2"|"b3",
  chapter_id:str,
  window_id:str|null,
  system_prompt_ref:str|null,
  execution_mode:"synthetic"|"llm",
  executor_contract_version:str,
  transport_config:dict,
  knowledge_mode:"whole_book_frozen",
  input_max_order:int,
  contract_versions:{builder_schema,validator,source_anchor,context_policy,request_contract},
  request_contract_hash:str,
  upstream_checkpoint_identity_hashes:{source_role:str -> checkpoint_identity_hash:str},
  allowlisted_sections:dict,
  lineage_manifest:[LineageRow],
}
```

`lineage_manifest` is sorted by `(source_channel, source_item_id, source_sha256)`. `input_max_order` is code-computed as the maximum order index over the transitive lineage of every rendered section, including upstream B0 claims and prior summaries. Under frozen mode it is audit metadata, not a usage gate. Rename the rev2 field `as_of_max_order` to `input_max_order`; do not introduce a second order concept.

Only deterministic checkpoint identity hashes enter `StageRequestBody` and its fingerprint. Concrete operational checkpoint hashes and paths are stored in the append-only audit/provenance record, not in model-visible sections and not in the semantic request fingerprint.

For Step 3 synthetic calls, `system_prompt_ref=null`, `executor_contract_version=SYNTHETIC_EXECUTOR_VERSION`, and `transport_config={"executor_version": SYNTHETIC_EXECUTOR_VERSION}`. Step 6 must supply a content-addressed prompt ref and the full LLM transport configuration (model, reasoning effort, verbosity, temperature, seed, max output, prompt cap, response format, and cache policy); that necessarily changes the request fingerprint and execution mode, so synthetic checkpoints remain non-reusable.

`V3StageRequest` is a frozen wrapper containing only `{canonical_request_json:str, request_fingerprint:str}`. The canonical JSON is UTF-8/NFC, sorted-key compact JSON of `StageRequestBody` and excludes `request_fingerprint`. The executor parses this immutable string; it never receives the pipeline's mutable request dict.

### C.3 Executor result and append-only attempt audit

```text
StageAttemptResult = {
  raw_payload:dict|null,
  raw_text:str|null,
  usage:{prompt_tokens:int,completion_tokens:int,cached_tokens:int,reasoning_tokens:int,cost_usd:number},
  from_cache:bool,
  execution_mode:"synthetic"|"llm",
  transport_meta:dict,
  error:{type:str,message:str}|null,
}

class StageExecutor(Protocol):
    def execute(
        self, request: V3StageRequest, *, attempt_no:int, bypass_cache:bool=False
    ) -> StageAttemptResult: ...
```

The orchestration owns attempt numbering/retry policy; an executor performs exactly one transport attempt and never hides prior raw attempts. Step 3 performs exactly attempt 1 and no retry. `SyntheticStageExecutor` returns scripted payloads keyed by `(stage, chapter_id, window_id)`, zero usage/cost, `from_cache=false`, `error=null`, and its pinned executor version in `transport_meta`. A transport/parse failure is represented by `raw_payload=null` plus a non-null error; the pipeline still persists the result before halting. Step 6 may add bounded retries by repeatedly calling this same one-attempt interface and persisting each attempt separately.

For every logical request, persist append-only audit files under `builder_v3/audit/<run_id>/<call_seq>_<stage>_<chapter>_<window-or-chapter>/`; every attempt gets its own exclusive-create subdirectory `attempt_<nn>/`:

1. `request.json` and SHA - before calling the executor.
2. `attempt_<nn>/raw_result.json` and SHA - before validation, including raw payload and transport metadata.
3. `attempt_<nn>/validation.json` and SHA - ValidationReport plus normalized-payload SHA and disposition.

A failed/crashed call keeps the files already written. Never overwrite an earlier attempt on resume. Normalized state is applied only after `ValidationResult.report.ok` is true. A fatal stage halts the chapter and the sequential run; no state generation/checkpoint pointer is published for that chapter. Audit files remain available. Flagged/dropped-but-valid rows remain represented by the raw result and ValidationReport counters.

---

## D. Stage allowlists and stage-scoped sentinel matrix

Every `allowlisted_sections` object rejects unknown top-level section keys. Build request -> persist -> execute -> persist raw -> `validate_*_v3` -> persist validation -> copy normalized payload into write-once state.

### D.1 Allowed sections

- **B0:** `{chapter_blocks:[BlockView]}` only.
- **B1:** `{active_window_blocks:[BlockView], context_only_tail:[ContextBlockView]}`. Tail contains up to two previous and two next blocks.
- **B2:** `{active_window_blocks, context_only_tail, b0_scene_projection:[B0SceneClaimView], window_mentions:[WindowMentionView]}`.
- **B3:** `{chapter_blocks, b0_typed_projection:B0TypedProjection, occurrence_roster:[OccurrenceRosterRow], prior_rolling_summaries:[RollingSummaryView], b2_events_compact:[B2EventCompact]}`.

No v3 stage calls legacy identity/ledger/registry/roster renderers. `occurrence_roster` is built directly from validated M1V3 reference records and payloads.

### D.2 Sentinel assertions are per stage, never global

| Stage | Legitimately present | Must be absent from this stage request |
|---|---|---|
| B0 | full current chapter | identity/alias registry, any summary, any future chapter |
| B1 | active window + its audited previous/next tails | B0 brief/claims, identity registry, summaries, current-chapter blocks outside active+tail, future chapters |
| B2 | active+tail, intersecting B0 claims, current-window mentions | non-intersecting B0 claims, full brief renderer, identity registry, summaries, blocks outside active+tail, future chapters |
| B3 | full current chapter, typed setting/gist, occurrence rows, B2 compact events, exact K prior summaries | any B0 cast/role list, full brief renderer, identity registry, summaries outside exact K, future chapters |

Therefore:

- A sentinel in a future active window is allowed in B0/B3 full-chapter requests but forbidden in unrelated B1/B2 requests.
- A sentinel in `role_hint` is allowed only when carried inside an intersecting untrusted B0 claim to B2; it is forbidden in B1/B3 and for non-intersecting B2 claims.
- Sentinels in legitimate tails/K summaries may appear in requests. The proof is instead that validators accept evidence/anchors only from active/current-chapter source IDs and never accept tail/summary IDs as grounding evidence.
- Store the lineage manifest and assert forbidden source rows are absent even when their text was transformed, so the conformance test does not rely only on string search.

---

## E. JSON-safe state and complete reference closure

Do not use tuple keys. Persist these two separate wire states:

```text
M1V3GroundState = {
  schema_version:"literary_m1_ground_state_v3",
  chapter_id:str,
  contract_versions:dict,
  windows:[WindowSpec],
  b0_payload:dict,
  b1_by_window:[{window_id:str,payload:dict}],
  b2_by_window:[{window_id:str,payload:dict}],
  reference_index:[ReferenceRecord],
  request_manifest:[RequestAuditRef],
  semantic_state_hash:str,
}

M2V3DigestState = {
  schema_version:"literary_m2_digest_state_v3",
  chapter_id:str,
  input_m1v3_checkpoint_hash:str,
  input_m1v3_identity_hash:str,
  digest_payload:dict,
  occurrence_roster:[OccurrenceRosterRow],
  digest_reference_index:[ReferenceRecord],
  prior_summary_provenance:[RollingSummaryProvenance],
  request_manifest:[RequestAuditRef],
  semantic_state_hash:str,
}
```

`RequestAuditRef` is the JSON shape `{call_seq,stage,chapter_id,window_id,request_fingerprint,request_path,request_sha256,attempts:[{attempt_no,raw_path|null,raw_sha256|null,validation_path|null,validation_sha256|null}],operational_upstream_checkpoint_hashes:dict}`. It contains paths/hashes, never dataclass objects. Its paths, attempt records, and operational hashes are provenance only and are excluded from `semantic_state_hash`.

```text
ReferenceRecord = {
  id:str,
  kind:"cast_claim"|"mention"|"turn"|"event"|"endpoint"|"address_occurrence"|"frame_segment",
  owner_stage:"b0"|"b1"|"b2"|"b3",
  chapter_id:str,
  window_id:str|null,
  block_id:str|null,
  anchor:SourceAnchorWire|null,
  source_interval:SourceIntervalWire|null,
}
```

Exactly one of `anchor` or `source_interval` is non-null. Reference rows sort by source start `(block_order, char_start, kind, id)`; frame intervals use their start coordinate. Duplicate IDs are fatal. Index all code-owned IDs, including cast claims, address occurrences, and frame segments; do not infer kinds from ID prefixes.

Reference closure is forward-only:

- B2 `mention_ref` must resolve to a B1 mention in the same active window.
- B3 occurrence/event fields must resolve to the corresponding M1V3 index and the exact allowed sets passed into `validate_digest_v3`.
- No automatic old-to-new remap exists in this handoff. A foreign ID is fatal.

State payloads are deep-copied on apply and never exposed for mutation. Compute `semantic_state_hash` only when finalizing a successful state. Its canonical semantic projection excludes paths, timestamps, usage, attempts, raw audit bytes, operational checkpoint hashes, and the hash field itself; it retains deterministic checkpoint identity hashes. Dict keys are sorted/NFC. Lists preserve their contract-defined order; do not claim generic order-independence. Window lists, per-window payload lists, rosters, and reference indexes use the explicit orders above.

---

## F. Checkpoint generations, deterministic identity, and resume

### F.1 Namespace and true atomic publish

All v3 data lives below `v3_root = out_dir / "builder_v3"`; it cannot collide with legacy paths.

For each successful chapter/stage:

1. Write immutable artifacts and checkpoint into a new generation directory `builder_v3/generations/<m1v3|m2v3>/<chapter_id>/<generation_id>/`.
2. Verify its artifact manifest and checkpoint integrity.
3. Write a tiny pointer JSON to a temporary file and atomically `os.replace` it onto `builder_v3/current/<m1v3|m2v3>/<chapter_id>.json`.

Readers follow only the current pointer. A crash before step 3 leaves an orphan generation and the previous pointer intact. A failed chapter never switches a pointer. Do not describe legacy's per-file promotion as a generation-level atomic operation; v3 owns this stronger contract.

### F.2 Operational hash vs deterministic identity hash

- `checkpoint_hash` is the full operational integrity hash and may include `created_at`; it is chained within one concrete run and is not expected to match an independently executed run.
- `checkpoint_identity_hash` is deterministic and excludes operational metadata (`created_at`, paths, accounting, run id, checkpoint hash, concrete parent/input checkpoint hashes, and generation-relative paths). It includes deterministic parent/input identity hashes and all semantic/contract/source/request identities below; it is the value compared between straight vs crash/resume experiments.

Every v3 checkpoint identity includes:

```text
stage, chapter_id, absolute chapter_index, chapter_sequence_prefix,
source_hash, schema_version, builder_schema, knowledge_mode, execution_mode,
validator/source_anchor/context_policy/request contract versions,
request_contract_hashes, request_manifest_hash,
window_target_tokens, window_max_blocks, tail_k, summary_k,
semantic_state_hash,
parent_checkpoint_identity_hash,
input_m1v3_identity_hash (M2V3 only),
artifact_content_manifest_hash.
```

The full operational checkpoint additionally stores concrete `parent_checkpoint_hash`, `input_m1v3_checkpoint_hash`, generation paths, and the ordinary artifact manifest for integrity/audit. That manifest covers every state artifact plus every request/raw/validation file referenced by `RequestAuditRef`; missing audit evidence invalidates the checkpoint. `artifact_content_manifest_hash` is deterministic over logical artifact role/name + SHA + size with run/generation-directory prefixes removed.

Any mismatch is a hard reject. Cross-load tests cover legacy<->v3 and synthetic<->llm. Synthetic checkpoints can never resume an LLM run.

### F.3 Absolute chains and K=2 summary sourcing

- M1V3 resume requires the full document prefix and restores the longest valid M1V3 pointer/checkpoint prefix.
- M2V3 validates the complete absolute M1V3 ancestor chain through the highest selected chapter, including parent and input identity hashes. A selected suffix is allowed only after this validation.
- For chapter N, required neighbor ids are the preceding `summary_k` chapter ids in that absolute chain.
- Resolve each summary first from a successful in-run M2V3 state; otherwise from a validated M2V3 checkpoint under `digest_context`. Send the deterministic `RollingSummaryView` to B3 and store concrete checkpoint hash + identity hash + input max order in `RollingSummaryProvenance`.
- Missing, stale, empty, out-of-order, or wrong-contract required summaries are fatal. Never synthesize `(summary unavailable)`.
- On resume, rebuild the in-run summary map from the restored M2V3 prefix before rendering the next B3 request. A crash before pointer publish contributes no summary.

---

## G. Synthetic executor and failure behavior

The synthetic fixture contains three chapters and scripted B0/B1/B2/B3 raw payloads. The executor must parse the canonical request JSON it receives and verify the expected stage/chapter/window key before returning its scripted result. A test-only executor that returns a payload without receiving the request is forbidden.

The pipeline must persist request -> raw result -> validation in that order. `report.ok=false`, transport error, missing script key, reference-closure error, checkpoint mismatch, or missing summary halts the sequential run at that chapter. Earlier published pointers remain valid; the failing chapter has audit records but no new current pointer.

---

## H. Verify and acceptance (adversarial, 0-API)

1. **Legacy non-regression:** Step 3 does not add a legacy flag/router and does not edit legacy modules. Pin a representative legacy fixture at baseline `8edac45` and compare deterministic rendered-message/config/checkpoint-expected projections against checked-in golden hashes. Exclude operational timestamps/paths/accounting. Run the full suite and verify the frozen DB hash.
2. **Request-contract tests:** changing any allowed section name/field/nullability/order rule changes only that stage's `request_contract_hash`; changing any runtime section value, lineage row, upstream hash, prompt ref, or execution config changes `request_fingerprint`; unknown section keys are rejected.
3. **Immutable executor boundary:** request bytes persisted before execute equal the bytes seen by the executor; an executor that mutates its parsed local copy cannot alter persisted bytes or fingerprint. Raw and validation records remain after a forced validation failure.
4. **Stage-scoped sentinel matrix:** probe every allowed/forbidden cell in section D. Assert both string absence where applicable and lineage-row absence. Intersecting untrusted B0 claims retain provenance+`trust`; no code auto-resolves them.
5. **JSON round-trip:** serialize/reload M1V3 and M2V3 states and prove exact type/semantic equality; no tuple-key stringification or dataclass leakage.
6. **Complete reference index:** every code-owned `cast_claim_id`, `mention_id`, `turn_id`, `event_id`, `endpoint_id`, `address_occurrence_id`, and `segment_id` appears exactly once with the correct kind/owner/position.
7. **Forward closure:** B2 uses minted B1 IDs; B3 uses minted B2 IDs. Apply a foreign-ID mutation to a deep-copied candidate payload (not the stored immutable state) and assert validation/closure fails. Tampering with a persisted state artifact fails manifest/checkpoint validation.
8. **Three-chapter straight vs resume:** compare M1/M2 semantic-state hashes and deterministic checkpoint-identity hashes between a straight ch1-ch3 run and ch1 -> simulated crash -> resume ch2-ch3. Do not compare operational checkpoint hashes. Ch3 B3 receives exactly ch1+ch2 summaries with matching provenance.
9. **K=2 failure probes:** missing/empty/stale/wrong-contract/out-of-order summary each halts before execute and publishes no chapter pointer.
10. **Checkpoint rejection:** legacy<->v3 and synthetic<->llm fail; changing any contract version, request manifest, semantic state, window config, K, parent, or M1 input identity fails.
11. **Atomic generation crash simulation:** crash after writing a generation but before pointer switch; reader still resolves the prior valid generation. Resume may publish a new generation without overwriting audit history.
12. **No identity injection:** spies prove no legacy seed/update/registry/entity-roster/full-brief helper is called. Occurrence-roster construction from validated reference records is allowed.
13. **Failure atomicity:** a fatal B1/B2/B3 payload leaves raw+validation audit evidence but no normalized chapter checkpoint/current pointer and no later chapter runs.

Handback to Claude: the isolated diff; focused and full-suite outputs; all adversarial probes above; golden legacy hashes; request/lineage manifests; straight-vs-resume semantic and identity comparisons; atomic crash proof; key scan; and frozen DB hash. Claude independently reruns items 2-13 before ACCEPT. No prompt, CLI, estimator, or API work starts until this task passes.
