# TASK_LIT_M4f - Phase 1 Step 4 - complete B4 ground-evidence handoff (deterministic, 0-API) - DRAFT rev3

Status: **DRAFT rev3 (Claude-verified rev2 + endpoint-routing correction). NEEDS FINAL CLAUDE CONFIRMATION; NOT ready for Terra until P0 passes.**

Review verdict on rev1 scope: **GO with a required correction.** The reader/assembler versus call-layer split is sound, and Step 4 must remain prompt-free, disclosure-free, and identity-decision-free. However, Step 4 must assemble the **complete B4 ground-evidence input**, not only identity cards + phase observations + frame surfaces. Three upstream contract gaps in P0 must be fixed and re-gated before this task can be implemented.

Contract sources, in precedence order:

1. `tasks/TASK_LIT_M4f_CANONICAL_V1.md` sections 0-9 and 12.
2. `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` sections 2.5, 3, and 8, especially the complete B4 consume-list and endpoint routing table.
3. Accepted Step-2 implementation (`builder_schema_v3.py`, `builder_validators_v3.py`, `source_anchor.py`).
4. Accepted Step-3 implementation (`builder_v3_pipeline.py`, `checkpoint_v3.py`, CodeX commit `3359869`).

If this file conflicts with Canonical, Canonical wins. If a required upstream field is absent, STOP at P0; never synthesize an empty channel to make acceptance green.

---

## 0. Scope boundary - decision for Claude/Sol

### 0.1 Scope verdict

The topology split is valid, with one citation correction:

- Legacy reader/assembler begins at `load_m3_v2_input_chain` (not the nonexistent `_verified_as_of_inputs`), then `build_identity_atoms_as_of` and `_digest_payloads_as_of`.
- Legacy call layer begins at `build_identity_messages` and `build_phase_messages`.
- **Step 4 = verified reader + complete ground-evidence assembler.**
- **Step 5 = retrieval -> adjudicator -> independent checker, decision ladder, code mint after matching ID-free signatures, overlay/quarantine apply, phase/disposition, address/frame/disclosure checkers, and persistence.**

Two rev1 decisions are confirmed:

1. **No disclosure filtering in Step 4.** This is an internal whole-book-frozen B4 input, never a Translator/Narrative pack. Disclosure applies only after Step-5 decisions produce typed items for `DisclosureFilteredContext`.
2. **No entity id, candidate id, binding, overlay record, or decision state is created by Step 4.** Code may mint deterministic `ground_item_id` values for provenance; those are evidence-row ids, never identity decisions.

Required correction:

- Step 5 must consume **only** the Step-4 bundle for upstream Builder evidence. It must not reopen M1V3/M2V3 checkpoints to recover omitted glossary/address/fact/thread/summary data. Otherwise Step 4 is a dead partial handoff and its fingerprint cannot prove the real B4 input.

### 0.2 Internal-only type boundary

`B4InputBundle` is whole-book internal knowledge. No Translator, Narrative Brief, glossary renderer, context-pack renderer, or vector-pack renderer may import or consume it directly. A conformance test pins this import boundary. The later disclosure-filtered view is a different type and a different task.

---

## P0. Upstream prerequisites - BLOCKING, separate fix-forward before Step 4

These are confirmed on the accepted Step-2/Step-3 code and a fresh three-chapter synthetic artifact. Do not hide them inside `b4_handoff_v3.py`.

### P0-A - restore B3 motifs

The accepted B3 schema/state currently has no `motifs`, while the Builder allocation complete consume-list and Canonical disclosure channels require motifs.

Add to B3 output:

```text
Motif = {
  note: str,
  block_ids: [block_id],
  subject_refs: [mention_id|endpoint_id] | null,
}
Digest.motifs: [Motif]  # required list, empty allowed
```

Validator requirements: all `block_ids` belong to the chapter; every non-null `subject_ref` resolves to an allowed occurrence; invalid rows are reported under the existing fatal/flag/drop taxonomy. Bump the validator contract identity so old checkpoints cannot silently satisfy the new shape. Add a fail-before/pass-after fixture.

### P0-B - bind each B3 observation to its actual B2 event endpoints

Current `validate_digest_v3` checks only that `event_id` and each `endpoint_ref` exist in global allowed sets. A response can combine event A with actor/target endpoints from event B and still pass.

Root fix: Step 3 passes an `event_endpoint_map` into `validate_digest_v3`; for every `relation_observation` require:

```text
observation.event_id == source_event.event_id
observation.endpoint_refs == [source_event.actor.endpoint_id, source_event.target.endpoint_id]
observation.block_id == source_event.block_id
```

Role order is actor then target; set equality is insufficient for directed relations. Mismatch is fatal. Bump validator contract identity and add a swapped-endpoint/adversarial-cross-event fixture.

### P0-C - occurrence-ground narrator claims

Current frame schema carries bare `narrator_surface` only, while Canonical section 7 requires `narrator_ref` provenance.

Add nullable:

```text
FrameSegment.narrator_ref: mention_id|endpoint_id|null
```

When non-null, validate against the chapter occurrence universe. `narrator_surface` remains advisory model text and never becomes an identity witness. Null is legal for an implicit/unresolved narrator and routes to the later frame checker. Bump validator contract identity and add valid/null/foreign fixtures.

P0 acceptance: Step-2/Step-3 focused suites, three-chapter straight/resume identity equality, cross-contract stale rejection, full suite, frozen DB hash, and key scan all remain clean. Only after Claude accepts P0 may Terra implement the rest of this task.

---

## 1. Step-4 deliverables and hard non-scope

After P0 passes, create exactly:

- `pipeline/literary/b4_handoff_v3.py`
- `pipeline/tests/test_b4_handoff_v3.py`

Do not modify prompts, CLI, estimator, API client, legacy Builder/Story-Bible code, app-E12 files, or the frozen D2L DB. No LLM/API/network. P0 changes are a separately reviewed upstream batch, not opportunistic edits in these two files.

---

## 2. Verified input loader - exact contract

### 2.1 Public API

```python
def load_verified_builder_v3_inputs(
    document,
    chapters,
    *,
    m1v3_dir,
    m2v3_dir,
    knowledge_mode="whole_book_frozen",
    execution_mode="synthetic",
    window_target_tokens=500,
    window_max_blocks=8,
    tail_k=2,
    summary_k=2,
) -> VerifiedBuilderV3Inputs: ...
```

Step 4 currently supports synthetic Step-3 checkpoints only. Later LLM enablement changes `execution_mode` and therefore checkpoint/request identity; it is not silently cross-loaded.

### 2.2 Chapter selection

- `chapters` must equal the exact document prefix from chapter 1 through the highest selected chapter. No suffix and no holes.
- Stamp `knowledge_cutoff_scope = highest selected chapter` and `scope_complete_book = (selected == all document chapters)`.
- A ch1-4 pilot over a 34-chapter document is a frozen **pilot prefix**, not a production whole-book build; the bundle must say so explicitly.

### 2.3 Chain validation

For every selected chapter, read only through `checkpoint_v3.read_current_checkpoint` and `read_state_from_checkpoint`; never glob/scan generation directories.

Expected M1/M2 fields are pinned from the function arguments and code constants: stage/schema, builder schema, absolute chapter index, full prefix, v3 source hash over `{block_id,order_index,block_type,NFC text}`, knowledge/execution modes, contract versions, request-contract hashes, window config, summary K, parent identity, and matching M1 input identity for M2.

Then:

- recompute each restored M1/M2 semantic-state hash using the exact Step-3 semantic projection rules: M1 removes `request_manifest` and `semantic_state_hash`; M2 removes `request_manifest`, `semantic_state_hash`, and operational `input_m1v3_checkpoint_hash`, then removes `source_m2v3_checkpoint_hash` from every prior-summary provenance row;
- validate full M1 and M2 parent identity chains;
- require `M2.input_m1v3_identity_hash == matching M1.checkpoint_identity_hash`;
- verify artifact manifests before reading payloads;
- deep-copy all returned data;
- any mismatch is fatal before any bundle row is emitted.

Do not import a private helper without naming it in the task. The Step-4 module may implement these small deterministic checks locally from the pinned rules above; it must not invent a different projection.

### 2.4 Verified input wire shapes

```text
VerifiedChapterInput = {
  chapter_id: str,
  absolute_chapter_index: int,
  source_blocks: [BlockView],
  m1v3_identity_hash: str,
  m2v3_identity_hash: str,
  m1_state: deep-copied M1V3GroundState,
  m2_state: deep-copied M2V3DigestState,
}

VerifiedBuilderV3Inputs = {
  schema_version: "literary_builder_v3_verified_inputs_v1",
  knowledge_mode: "whole_book_frozen",
  execution_mode: "synthetic",
  selected_chapters: [chapter_id],
  knowledge_cutoff_scope: chapter_id,
  scope_complete_book: bool,
  chapters: [VerifiedChapterInput],
}
```

Lists use absolute chapter order. `BlockView.text` is the same NFC source string used by SourceAnchor.

---

## 3. Occurrence cards and context universe

Step 4 must join every `occurrence_roster` row back to exactly one normalized B1/B2 owner row by id. The roster alone is intentionally too small and must not be treated as the full B4 card.

### 3.1 Discriminated union

```text
OccurrenceCardCommon = {
  occurrence_id: str,
  occurrence_kind: "mention"|"endpoint",
  surface: str,
  referent_kind_claim: str,
  chapter_id: str,
  window_id: str,
  block_id: str,
  block_order: int,                 # document block order_index
  anchor: SourceAnchorWire,
  evidence_quote: str,
  source_ref: {owner_stage, owner_id, owner_role|null},
  context_universe: ContextUniverse,
}

MentionCard = OccurrenceCardCommon + {
  occurrence_kind: "mention",
  reference_scope: null,
  mention_type: "name"|"nickname"|"descriptor",
}

EndpointCard = OccurrenceCardCommon + {
  occurrence_kind: "endpoint",
  reference_scope: "individual"|"group"|"narrator"|"reader"|"unknown",
  mention_ref: mention_id|null,
  attribution_method: str,
  runtime_eligibility: "eligible"|"route_out"|"discourse_only"|"deferred"|"invalid",
  resolution_evidence: str,
  owner_id: turn_id|event_id,
  owner_role: "speaker"|"addressee"|"actor"|"target",
}
```

Never infer owner or role by parsing an id prefix. Join through the normalized B2 payload structure and verify its id against the reference index.

### 3.2 Evidence quote

- Mention card: copy normalized B1 `evidence_quote`.
- Endpoint card: copy normalized B2 `resolution_evidence` (which remains separately auditable from `evidence_quote`).
- Enumerate all exact occurrences of the NFC-normalized quote in the NFC source block, then select the **unique** half-open quote span that contains the already-validated `SourceAnchor` span. Zero or more than one containing quote span is fatal. Do not use a first-match fallback or mint from the evidence span.
- Require the card anchor to lie inside that selected quote span. Missing/ambiguous quote or owner join is fatal.
- SourceAnchor identifies the anchored surface; it does **not** by itself represent a full clause. Do not call the anchor span an evidence clause.

### 3.3 Context universe - fixes the madam-class defect without sentence guessing

```text
ContextUniverse = {
  active_block: BlockView,          # always the complete block
  scene_block_candidates: [BlockView],
  scene_range: [start_block_id,end_block_id],
  source: "b0_scene_partition"|"active_block_fallback",
}
```

- The complete active block is the minimum unit and must contain `evidence_quote` verbatim.
- If exactly one validated B0 `scenes_party_size` range contains the block, include that full scene's blocks in source order as candidates; otherwise use active-block-only fallback.
- Step 4 does not run a regex sentence splitter and does not cut a character window. Canonical section 2 says full block + scene/adjacent-block context; Canonical section 8 is interval algebra, not a sentence-boundary contract.
- Prompt token-cap selection from this universe belongs to Step 5 and must produce a selection manifest. Step 4 never silently truncates the candidate universe.

---

## 4. Mechanical occurrence routing - exact cover, no semantic inference

Rename rev1 `build_person_candidate_roster` to:

```python
build_occurrence_routing_view(inputs, occurrence_cards) -> OccurrenceRoutingView
```

This is not Canonical's active-entity roster. Step 5 builds that entity roster only from ACTIVE decisions.

```text
OccurrenceRoutingView = {
  person_occurrences: [OccurrenceCard],
  non_person_occurrences: [OccurrenceCard],
  discourse_only: [OccurrenceCard],
  deferred: [OccurrenceCard],
  invalid_flagged: [OccurrenceCard],
  counts: {total,person,non_person,discourse_only,deferred,invalid_flagged},
}
```

Routing rules:

- Mention routing is the first mechanical route for B1 mentions because B1 has no `runtime_eligibility`: `referent_kind_claim=person -> person`; `{animal,nonhuman_character,place,group_reference,object} -> non_person`; `unknown -> deferred`. Mention scope is null by design and is not interpreted as endpoint `unknown`.
- Endpoint routing **must not re-derive** the two-axis table. Step 2 already computes and locks the total result in normalized `runtime_eligibility`; map it bijectively: `eligible -> person`, `route_out -> non_person`, `discourse_only -> discourse_only`, `deferred -> deferred`, `invalid -> invalid_flagged`.
- A missing value or any value outside the closed `RuntimeEligibility` enum is fatal checkpoint/contract corruption. Do not fall back to the raw axes. Preserve `reference_scope` and `referent_kind_claim` on the card only as auditable source claims for Step 5, not as a second routing authority.
- Every occurrence appears in exactly one bucket. Counts sum exactly to roster cardinality. Routed rows are never dropped.

---

## 5. Complete B4 ground-evidence consume-list

Step 4 emits every accepted Builder field that B4/its later checkers consume. Step 5 is forbidden from reopening checkpoints to recover a missing channel.

Each non-occurrence row is wrapped as:

```text
EvidenceRef = {
  ref_kind: "block"|"mention"|"endpoint"|"turn"|"event"|"address_occurrence"|"frame_segment",
  ref_id: str,
  role: "speaker"|"addressee"|"actor"|"target"|null,
}

GroundEvidenceRow = {
  ground_item_id: str,              # deterministic code id, not entity id
  kind: closed enum,
  chapter_id: str,
  evidence_refs: [EvidenceRef],
  source_checkpoint_identity_hash: str,
  payload: exact typed normalized row,
}
```

`EvidenceRef` ordering is source position, then the closed `ref_kind` order shown above, then `ref_id`, then role. A role is required only for endpoint references owned by a turn/event and is null otherwise. Never infer a reference kind or role from an id prefix.

Mint exactly:

```text
ground_item_id = "g_" + kind + "_" + chapter_id + "_" +
                 sha256(canonical_json({kind,chapter_id,evidence_refs,payload}))[:20]
```

Use the existing `checkpoint.canonical_json` semantics (sorted mapping keys; list order preserved). The id never includes operational path, checkpoint hash, timestamp, or identity conclusion. Duplicate ids with unequal canonical bodies are fatal; equal canonical bodies dedupe with a counter. There is no ordinal/hash implementation choice.

Required channels:

1. `cast_claim_inputs`: B0 cast claims, scene-scoped, explicitly `trust="untrusted"`.
2. `occurrence_cards`: all B1 mentions and B2 endpoints.
3. `glossary_inputs`: full B1 `{source_term,proposed_target_vi,category,do_not_translate,block_ids}`.
4. `dialogue_turn_inputs`: B2 turn id, speaker/addressee endpoint ids, utterance quote, register cue, and address occurrences with their anchors/evidence/addressee role.
5. `relation_event_inputs`: B2 event id/type, actor/target endpoint ids in directed role order, position, block, and evidence.
6. `phase_observation_inputs`: B3 relation observations, cross-joined to the exact B2 event under P0-B.
7. `state_change_inputs`: B3 occurrence-grounded state changes.
8. `unresolved_thread_inputs`: B3 threads with optional occurrence refs.
9. `translator_fact_inputs`: B3 facts preserving `inference_basis` and all evidence/event refs.
10. `motif_inputs`: P0-A B3 motifs.
11. `rolling_summary_inputs`: each chapter's current rolling summary plus deterministic source identity; prior-summary provenance remains audit metadata, not duplicate prose rows.
12. `frame_claim_inputs`: complete frame rows including parent, source interval, status/version, nullable `narrator_ref`, and advisory `narrator_surface`.
13. `frame_leaf_index`: the validator-derived `deepest_active_leaf_spans` and `deepest_active_leaf_by_block`, cross-checked against the frame tree rather than recomputed from prose.

Ordering is chapter index, source position, source array ordinal, stable id. Never sort by hash.

### 5.1 Event-observation integrity

For each phase observation, require exact equality with its source B2 event:

- endpoint roles/order;
- block id;
- event existence and chapter ownership.

Step 4 rechecks this defensively, but P0-B fixes the producing validator so bad data never becomes a valid checkpoint.

### 5.2 Retain history

Assembly is an append-only ordered union across selected chapters. It has no `replace_pairs`, no latest-chapter replacement, and no resolved pair key. Duplicate event/ground ids with equal payloads dedupe; conflicting duplicates are fatal.

This closes **input evidence loss** underlying audit #21. It does not by itself guarantee that Step-5 relation-fact output retains history; Step 5 must carry a separate acceptance that landlord/tenant facts remain after later scopes.

### 5.3 Frame authority

Step 4 preserves frame claims; it does not promote them. `narrator_surface` is advisory and never a witness. Null/unverified narrator/frame information remains explicit and routes to the Step-5 independent frame checker. Do not emit a derived canonical `narrator_surfaces` list that could look authoritative.

---

## 6. Public functions and final bundle

```python
load_verified_builder_v3_inputs(...) -> VerifiedBuilderV3Inputs
build_occurrence_cards(inputs) -> list[OccurrenceCard]
build_occurrence_routing_view(inputs, cards) -> OccurrenceRoutingView
build_complete_ground_evidence(inputs, cards) -> CompleteGroundEvidence
assemble_b4_input_bundle(document, chapters, *, m1v3_dir, m2v3_dir, ...) -> B4InputBundle
```

```text
B4InputBundle = {
  schema_version: "literary_b4_input_bundle_v1",
  handoff_contract_version: "literary_b4_handoff_contract_v1",
  knowledge_mode: "whole_book_frozen",
  execution_mode: "synthetic",
  selected_chapters: [chapter_id],
  knowledge_cutoff_scope: chapter_id,
  scope_complete_book: bool,
  occurrence_cards: [...],
  occurrence_routing: OccurrenceRoutingView,
  ground_evidence: {
    cast_claim_inputs, glossary_inputs, dialogue_turn_inputs,
    relation_event_inputs, phase_observation_inputs,
    state_change_inputs, unresolved_thread_inputs,
    translator_fact_inputs, motif_inputs, rolling_summary_inputs,
    frame_claim_inputs, frame_leaf_index,
  },
  provenance: [{chapter_id,m1v3_identity_hash,m2v3_identity_hash}],
  input_identity_manifest_hash: str,
  bundle_manifest_hash: str,
}
```

`input_identity_manifest_hash` hashes the ordered provenance identities + handoff/contract/config identity. `bundle_manifest_hash` hashes the complete deterministic semantic bundle excluding only its own hash and operational paths/hashes. It includes `knowledge_cutoff_scope`, scope completeness, routing, every channel, source identities, and handoff version.

The assembly API returns an immutable/deep-copied JSON-safe value. Step 4 does not persist a new checkpoint; Step 5 persists its requests and decision state. Reassembly must be deterministic and auditable from the two identity hashes.

---

## 7. Locked invariants

- **I1 No authority smuggling:** no hint/candidate/base binding or entity decision in any semantic field. Cast claims remain untrusted.
- **I2 Full-block context:** active block is complete NFC source text; no fixed-char or sentence-regex slice.
- **I3 Complete occurrence provenance:** every occurrence joins to exactly one owner row and carries role/method/evidence needed downstream.
- **I4 Routing exact-cover:** mention and endpoint rules are distinct; unknown is deferred, not person-authorized.
- **I5 Complete B4 consume-list:** all 13 channels are present and tested with non-empty sentinels; empty upstream where fixture expects data is setup failure.
- **I6 Event topology:** B3 observation endpoints are the directed endpoints of its own B2 event.
- **I7 Retain input history:** no replacement by pair/latest scope; conflicting duplicates fatal.
- **I8 Frame non-authority:** proposed/uncertain frame/narrator claims are preserved but not promoted.
- **I9 Frozen internal boundary:** no disclosure filtering here, but no pack renderer can consume this type.
- **I10 Determinism/provenance:** complete config + identity + channel content is hashed; no paths/timestamps/usage in semantic hash.
- **I11 Fail closed/non-destructive:** stale chain, bad join, missing required channel, or topology mismatch yields no partial bundle and mutates no source file/state.

---

## 8. Forbidden

No LLM/API/network. No entity/candidate/binding ids, identity grouping, overlay, disclosure, phase labels, address policy, frame promotion, or decision state. No registry/ledger helper. No direct legacy reader. No hash sorting. No directory glob. No hidden Step-5 checkpoint reads. No modification outside the two deliverables after P0 is accepted.

---

## 9. Acceptance - adversarial 0-API probes

Every behavioral probe must fail against the naive/rev1 implementation and pass here.

1. **P0 contract probes:** motif present/validated; narrator_ref valid/null/foreign; cross-event and swapped endpoint refs fatal; stale old validator checkpoints rejected.
2. **Complete-channel fixture:** seed non-empty cast, mention, endpoint, glossary, turn/register/address, relation event, observation, state, thread, fact, motif, summary, and frame channels; assert every ground id/evidence ref appears once. Empty channel is fixture setup failure.
3. **Occurrence owner join:** mention gets mention_type; endpoint gets owner id/role, mention_ref, attribution, eligibility, and resolution evidence. Missing/duplicate owner is fatal.
4. **Madam-class:** the active block is >360 chars and contains both defining clause and later surface; the complete block survives byte-for-byte and contains evidence. No sentence/char slicing helper exists.
5. **Routing table:** cover mention person/nonperson/unknown; for endpoints cover each of the five `RuntimeEligibility` enum values and assert the one-to-one bucket mapping. Include `narrator+unknown` and `reader+unknown` fixtures whose normalized value is `discourse_only`; assert Step 4 does not re-route either to deferred. Missing/foreign eligibility is fatal. Each row appears in exactly one bucket and counts reconcile.
6. **No-authority structured scan:** recursively reject forbidden key names and identity-id values in identifier/decision fields. Do not scan opaque source prose for the substring `ent_`, which can create false failures.
7. **Event topology:** event A + endpoints from event B, reversed directed endpoints, wrong block, and foreign endpoint each fail before bundle creation.
8. **Retain history:** ch1 observations/facts/evidence remain in ch1-4 assembly; equal duplicates dedupe/count; conflicting duplicate id is fatal. Record that Step-5 still owns output fact-retention acceptance.
9. **Frame claims:** status/version/tree/source interval/narrator_ref survive; advisory narrator surface never enters an identity slot; unresolved frame remains unresolved.
10. **Prefix/knowledge scope:** suffix/hole rejected; ch1-4 over a longer document stamps `scope_complete_book=false`; whole document stamps true; non-frozen mode rejected.
11. **Ancestry/state integrity:** missing, stale, foreign-contract, wrong parent, wrong M1 input identity, semantic-state mismatch, and source-topology change each fail before any output row.
12. **Determinism:** two assemblies are byte/semantic equal with identical hashes and ground ids; changing one accepted evidence field changes its ground id and bundle hash; changing only operational path/checkpoint hash does not. Repeated evidence text is resolved only by the unique quote span containing the SourceAnchor; first-match behavior is a failing probe.
13. **Internal-only boundary:** renderer modules cannot import `B4InputBundle`/handoff module; no disclosure fields are fabricated in Step 4.
14. **Non-destructive:** all source checkpoint/state/audit files hash-identical before and after; candidate input dicts remain equal.
15. **Regression:** Step-2/Step-3 focused suites and full pipeline suite green; frozen DB hash unchanged; `git diff --check` and key scan clean.

---

## 10. Claude confirmation gate and handback

Claude must explicitly confirm these before Terra starts:

1. Scope correction: Step 4 is the **complete** B4 ground-evidence reader, while no-disclosure/no-identity-decision remains locked.
2. P0-A/B/C upstream fixes and validator-contract invalidation are accepted as root fixes, not hidden Step-4 normalization.
3. Occurrence routing uses a first-time mention-kind rule, but endpoint routing consumes the Step-2-derived `runtime_eligibility` bijectively and never re-derives the two-axis table; both paths feed five exact-cover buckets.
4. Step 5 is forbidden from reopening M1/M2 for omitted channels.

After confirmation + P0 PASS, Terra implements on a separate branch. Handback reports: branch/commit; only the two Step-4 files; P0 commit dependency; probe-by-probe results; focused/full suite counts; straight/resume hashes; frozen DB before/after; key scan; and any additional root-cause finding. Commit body must identify `Author/Implementer: CodeX` for CodeX implementation commits.
