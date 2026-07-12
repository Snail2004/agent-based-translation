# TASK_LIT_M4f_P1S3 — Builder v3 orchestration path — Phase 1, Step 3

Status: **DRAFT rev2 (Claude → Terra/CodeX), 2026-07-12.** rev2 folds Sol's task review (7 BLOCKER + 3 MAJOR, all verified by Claude on the task + code). Builds on Step-2 (ACCEPTED, `main` @ `8edac45`). Implementer = Terra. **Verify gate = Claude, adversarial + integration.**

Contract source: `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` §1–§5 + §8, Canonical §2/§3/§9/§10. **All shapes needed are pinned below — do not invent architecture; if a shape here is ambiguous, STOP and ask, do not decide.**

---

## 0. Scope, reframe, non-scope

Builds a **parallel v3 orchestration path** using Step-2 modules, identity-hint injections removed **inside the v3 path only**; **legacy untouched and DEFAULT**. Real prompts still emit legacy schema (Step 6), so the v3 path is driven by a deterministic executor stub — but through the SAME request interface the Step-6 LLM executor will use (§D), so it is not a throwaway.

**Mode lock (Sol B1):** Step-3 supports **`whole_book_frozen` ONLY**; it **hard-rejects any other `knowledge_mode`**. (`as_of_experiment` applies to B4 identity, not to B0–B3 extraction, so it is out of scope here.) This dissolves the B0-projection / prev-tail-only / sentinel contradictions.

IN scope: the v3 path (`run_m1_v3`/`run_m2_v3`, programmatic only), the typed `OccurrenceGroundState`, `V3StageRequest`+`StageExecutor`, M1V3/M2V3 checkpoints with full identity + hard-reject, the B0→B1→B2→B3 handoff with provenance-preserving projections, and the integration + adversarial tests.

OUT of scope: prompts (Step 6), CLI/estimator/API routing (Step 6), deleting legacy, switching default, real LLM runs, B4. Do NOT touch frozen D2L DB (`64D989…`), prompts, app-E12. No LLM/API/network. `sk-` scan. Branch + hand back; no self-certify.

---

## A. Deliverables

1. `pipeline/literary/builder_v3_pipeline.py` — `V3StageRequest`, `StageExecutor` protocol, `SyntheticStageExecutor`, `OccurrenceGroundState`, `run_m1_v3`, `run_m2_v3`, the B0→B3 handoff + projections.
2. `pipeline/literary/checkpoint_v3.py` (or additions to `checkpoint.py` that do NOT change legacy) — M1V3/M2V3 versioned checkpoint build/validate with the full identity in §E.
3. Version constants: `BUILDER_SCHEMA_V3 = "v3"`, `M1_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m1_checkpoint_v3"`, `M2_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m2_checkpoint_v3"`, plus `VALIDATOR_CONTRACT_VERSION`, `SOURCE_ANCHOR_VERSION`, `CONTEXT_POLICY_VERSION` (string constants, bumped independently).
4. `pipeline/tests/test_builder_v3_pipeline.py` — §F.

Legacy driver, legacy checkpoint versions, and default behavior are byte-unchanged.

---

## B. StageExecutor interface (Sol B4 — kills dead-integration)

The pipeline ALWAYS builds, renders, and PERSISTS a request, then hands it to a transport. Transport is the only thing synthetic vs LLM differ on.

- `V3StageRequest` (frozen): `{stage ∈ {b0,b1,b2,b3}, chapter_id, window_id|null, system_prompt_ref, allowlisted_sections: dict, knowledge_mode="whole_book_frozen", as_of_max_order, contract_versions: {validator, source_anchor, context_policy, builder_schema}, request_contract_hash}`. `allowlisted_sections` is the ONLY payload the executor sees; building it is where the allowlist (§C) is enforced.
- `class StageExecutor(Protocol): def execute(self, request: V3StageRequest) -> dict  # raw stage payload (pre-validation)`.
- `SyntheticStageExecutor(scripted: dict[key -> raw_payload])` returns the scripted raw payload for the request's `(stage, chapter_id, window_id)` — it MUST receive the fully-built request and may not bypass it. Step-6's LLM executor implements the same `execute`.
- The pipeline records every built request (rendered) into the ground-state BEFORE calling execute, so a stub cannot skip renderer/handoff/audit and still pass.

---

## C. v3 path per-stage: allowlist + provenance-preserving projections

Each stage: build `V3StageRequest.allowlisted_sections` → execute → `validate_*_v3` → store normalized `ValidationResult.payload` (fatal ⇒ mark window/chapter failed, never emit). No stage calls `seed_entity_ledger_from_chapter_brief`, `update_entity_ledger_from_lexicon`, `render_chapter_brief_for_injection`, or any registry/roster injection.

- **B0** allowed: `{chapter_blocks}` only. → `validate_chapter_brief_v3`.
- **B1** allowed: `{active_window_blocks, context_only_tail (read-only, previous blocks only)}`. → `validate_lexicon_v3`, code mints `mention_id`.
- **B2** allowed: `{active_window_blocks, context_only_tail, b0_scene_projection, window_mentions}`.
  - **`b0_scene_projection`** (Sol B2 — closed shape, provenance kept, NOT bare surfaces): the list of B0 cast_claims whose `scene_range` intersects this window, each `{cast_claim_id, surface, surface_kind, referent_kind_claim, role_hint, source_block_ids, anchor, scene_range, trust:"untrusted"}`. Selection is mechanical scene∩window.
  - **`window_mentions`**: `{mention_id, surface, block_id}` (code-minted ids).
  - → `validate_narrative_v3`, code mints turn/event/endpoint ids.
- **B3** allowed: `{chapter_blocks, b0_typed_projection, occurrence_roster, prior_rolling_summaries (K=2, separate), b2_events_compact}`.
  - **`b0_typed_projection`**: `{setting, neutral_premise}` with `neutral_premise` marked GIST_ONLY (never evidence). NOT the full brief renderer (no cast/role list to B3).
  - **`occurrence_roster`** (Sol B3 — occurrence rows WITH ids, not surfaces, not an entity roster): `[{id: mention_id|endpoint_id, surface, referent_kind_claim, block_id, anchor}]`. This id set is passed to `validate_digest_v3(mention_ids=…, endpoint_ids=…, event_ids=…)` so occurrence-grounding can be checked.
  - → `validate_digest_v3`.

**Sentinel semantics (Sol B1 — split by legitimacy):** FORBIDDEN sources = {registry, entity/alias roster, full-brief cast/role, neighbor SUMMARIES other than the K=2 prior rolling summaries, any future window/chapter}. The sentinel test asserts a planted sentinel in each FORBIDDEN source is ABSENT from the rendered request. The legitimately-present `context_only_tail` and the K=2 prior summaries are NOT forbidden — for them the check is "present as read-only context, never cited as evidence/authority" (no code path treats them as a grounding source).

---

## D. OccurrenceGroundState (Sol B7 — typed, not prose)

`OccurrenceGroundState` (per chapter, owned by the v3 pipeline):
- `payloads: dict[(stage, window_id|"chapter") -> normalized_payload]` (the validated v3 outputs).
- `windows: [WindowSpec]` with **active-window exact-cover** of the chapter's non-heading blocks (validated: no gap/overlap).
- `reference_index: [{id, kind ∈ {mention,turn,event,endpoint}, owner_stage, window_id|null, block_id, anchor}]` — canonical-ordered by `(block_order, char_start, id)`; **duplicate id ⇒ fatal**.
- `rendered_requests: [V3StageRequest]` (every request built, for audit + the sentinel/golden tests).
- `semantic_state_hash`: content hash over `{payloads, reference_index, contract_versions}` (order-independent), used as the M-checkpoint semantic identity.
Ownership: the pipeline is the sole writer; stages append; nothing mutates a stored payload after write.

---

## E. Checkpoint v3 identity + hard-reject (Sol B6)

M1V3/M2V3 checkpoints live under a **separate namespace** `checkpoints/m1v3|m2v3` and a **separate artifact out-dir namespace**; they never share paths with legacy. `_checkpoint_expected` for v3 must include: `schema_version=*_v3`, `validator_contract_version`, `source_anchor_version`, `context_policy_version`, `request_contract_hash`, `window_target_tokens`/`window_max_blocks`/`K`, `execution_mode ∈ {synthetic, llm}`, `builder_schema="v3"`, `parent_checkpoint_hash`, `input_m1v3_checkpoint_hash` (for M2V3), `artifact_manifest`, and use **atomic publish** (staged work-dir + pointer switch, like legacy). **Hard-reject (no silent migrate):** loading fails if ANY identity field mismatches — including a `_v1/_v2` checkpoint under v3 expectations (and vice-versa) AND an `execution_mode=synthetic` checkpoint under an `llm` expectation. A synthetic checkpoint can NEVER resume a real LLM run (Step-6). Old ch1–4 artifacts stay a comparison baseline.

---

## F. Verify / acceptance (Claude re-runs adversarially)

1. **Legacy byte-for-byte (Sol M10):** a golden test asserts flag-unset == explicit-legacy on: rendered requests, report semantic hash, and checkpoint `expected`/config hash. Plus the full suite green + frozen DB hash unchanged.
2. **v3 integration, THREE-chapter fixture (Sol M9):** `SyntheticStageExecutor` drives ch1–ch3; assert (a) a straight ch1→ch3 run and a ch1→crash→resume ch2–3 run produce the **same canonical OccurrenceGroundState + checkpoint hashes**; (b) ch3's B3 request contains **exactly the K=2 prior rolling summaries**; (c) a digest whose required context is missing is **fail-closed** (chapter fails, not silently degraded).
3. **Reference closure = forward-only, mutation probe (Sol M8):** ids flow forward already-minted (B2 uses B1 `mention_id`, B3 uses B2 ids); a **foreign/stray id is FATAL** (no remap in the handoff). Probe: mutate one id in a stored payload after render → `validate_*_v3` / the closure check REJECTS. (Do NOT add a "remap old→new" step; Step-2's `remap_references` is for overlay/checkpoint-rewrite only, not forward handoff.)
4. **No-injection (sentinel, §C semantics):** planted sentinel in each FORBIDDEN source absent from every v3 rendered request; a call-log/spy asserts the v3 path never calls `seed_entity_ledger_*` / `update_entity_ledger_*` / roster injection.
5. **Checkpoint hard-reject:** v3↔legacy cross-load rejected; synthetic↔llm execution_mode cross-load rejected. No silent migrate.
6. **Default-off + programmatic-only:** flag unset ⇒ identical to today; `run_m1_v3`/`run_m2_v3` are programmatic; no CLI/estimator/API path added in Step-3.

Handback: diff, the three-chapter integration + adversarial runs (green), sentinel-absence + call-log proof, closure/mutation proof, cross-version+cross-mode rejection proof, and the legacy golden diff. Claude independently re-runs the golden legacy diff, the crash/resume canonical-state equality, the mutation probe, and the cross-mode checkpoint rejection before ACCEPT.
