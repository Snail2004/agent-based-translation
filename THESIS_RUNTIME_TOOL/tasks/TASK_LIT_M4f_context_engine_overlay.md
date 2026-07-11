# TASK_LIT_M4f — Context Engine v2 + non-destructive identity overlay (PIVOT from M4e, rev2, for Sol round 2)

Status: **DRAFT rev2 — awaiting Sol critique round 2. Do NOT implement.**
Owner: Claude (spec + prompts + verify gate). Implementer: CodeX (after LOCK). Decision authority: user (pivot decided 2026-07-11).
Parent: TASK_LIT_M4e (rev4, FROZEN — see §1 for what survives). Grandparent: TASK_LIT_M4d (pilot ch1–4 evidence base).

## 0. Pivot decision & verified evidence base

**User decision (2026-07-11):** fix the ROOT CAUSE — the context selection/packaging machinery — instead of building ever-deeper corrective machinery for model verdicts made on bad input. The context design was locked on theory; the pilot produced the first real data, and that data indicts the context engine, not the model. M4e's destructive-auditor line (rev1–rev4, 4 Sol rounds, each round's blockers going deeper into transactional semantics) is FROZEN, not discarded.

**Error classification — every claim verified by Claude on artifacts before acceptance:**

| Case | Verified evidence | Classification |
|---|---|---|
| `ent_madam` published as person | atom `atom_m_wh_ch01_b019_03` quote_context starts mid-sentence, contains only "irritated madam, that she … leapt on my knees" — the referent-determining clause is absent. B0 ch1 cast ALREADY contains `{"surface": "the canine mother", "role_hint": "dog", "first_seen_block": "wh_ch01_b017"}` but this never reaches the B4 identity card. | **Context packaging failure** (evidence truncated + B0 knowledge not joined) |
| b004 master/mistress cross-person merge | atoms `..._b004_01/02` carry `hint_entity_id: ent_mrs_earnshaw` / `ent_mr_earnshaw` — hints minted by B1 under the whole-chapter brief (future leak verified in M4e round 2: window b001–b008 prompt references b035+/b045). The sentence itself cannot disambiguate without the Nelly/Thrushcross timeline. | **Context failure + wrong-answer bias injected** (not a fair test of the model) |
| Two Jabez, ghost-Catherine, epithet fragments | verified M4e rev2 round: fragment pairs (`T' maister`↔`the master`, `Cathy`↔`Catherine Earnshaw`, two Jabez) share ZERO exact surface/hint keys → prior-group selection never surfaced the true candidate to the model's shard. | **Retrieval/sharding failure** (hidden candidates) |
| `him`→`me` transposition (ch4 phase) | amendment #10: model had the evidence and mis-copied; validator caught it; pair quarantined. | **True model slip** (verbatim discipline, not comprehension) |
| Successes with adequate context | two Catherines separated; young/present Heathcliff unified; Heathcliff/Hindley/Earnshaw separated; Lockwood–Heathcliff change-points; `daughter_in_law_of`; King Lear → literary allusion. | Model performs when context is sufficient |

**Conclusion (now evidence-backed):** there is no strong case that GPT-5.4, given complete/clean/correctly-framed context, persistently misjudges identity. Fixing verdict-correction machinery before fixing input quality optimizes the wrong layer.

## 1. Disposition of M4e material

**Carried into M4f (survives regardless of auditor scope — all verified as real defects):**
- **Full request fingerprint reuse** (M4e round-4 B1): fingerprint = hash of canonical FULL API request (incl. reasoning_effort, verbosity, max_output_tokens, prompt cap) + separate `validator_contract_hash` (validator change ⇒ cache replay + revalidate, not fresh spend). Also fixes the latent `llm_client.py` cache-key gap (poisoned-replay class from amendment #6).
- **Atomic publish** (round-4 B3, minimal form): story bible + checkpoint written as staged generation + single pointer switch; readers only see pointer-confirmed generations.
- **Multi-outcome event disposition** (B''' + round-4 MINOR exclusivity: `blocked_reason ≠ null ⇒ outcomes=[]`; outcomes unique per (kind,id); ids must belong to the pair and current response).
- **Default-deny frame visibility** (C''': phase visible only in origin frame; cross-frame needs explicit authorization — folded into the Context Contract as a *view rule*, no destructive machinery needed).
- **Unit manifest + per-model token budgets** (D''/consolidation-unit agenda) — unchanged, still pre-scale.
- **Budget gates** (E''' formula `used_today + base + reserve ≤ 225k`) — simpler here: without destructive replay the contingent classes shrink to escalation calls.
- **Human-decision contract H1** — becomes the ONLY path to destructive change (see §4).

**FROZEN (recorded as future work, not implemented):** two-key partition confirmation, dual global sharded partitions, dependency DAG + topological replay closure, revision-bound re-audit — all exist only to make LLM-initiated destructive edits safe. The overlay design (§4) removes LLM-initiated destructive edits entirely.

## 2. Context Contract v1 (per call class — the core deliverable)

Locked schema per call, enforced by validator; prompts stay Claude-authored blockquotes.

**Identity call receives:**
1. the mention/atom under decision (verbatim quote + block id),
2. **sentence-complete local scene**: quote_context expanded mechanically to sentence boundaries ± N sentences (N measured, config-hashed) — never a mid-sentence character slice; the expansion is typographic (sentence/block boundaries), no language judgment in code,
3. **B0 entity-kind join**: the B0 cast rows (surface, surface_kind, role_hint, first_seen_block) valid as-of the mention's block — closes the `madam` gap,
4. narrator / frame / story-time for the mention's block (from M2, versioned),
5. **global lightweight roster** (every T2 entity: id, canonical surface, kind, one-line note — from M4e A', survives) so no candidate is hidden by sharding,
6. **detailed candidate cards** for neighborhood + any roster entity the model flags (two full dossiers when merge/split is on the table),
7. prior evidence with provenance (block-anchored quotes only).

**Hint discipline (b004 fix):** B1 mention hints minted under whole-chapter context are DEMOTED to `candidate_surface_match` — presented as neutral candidates in the roster, never as `hint_entity_id` anchors. As-of validity: nothing derived from blocks > mention block may bias the call (CONTEXT_ONLY tails excepted and labeled).

**Phase call receives:** current pair, open phases for that pair, events NEW since last checkpoint (per-event disposition contract), related durable facts, compatible-frame views only. Never the accumulated full pair history (bounded-state goal from M4d BLOCKER 4 is satisfied by construction).

## 3. Context Audit artifact (per call, checkpointed)

`{call_id, mode, included: [...], excluded: [{item, reason}], candidate_entities: [...], frame_ref, as_of_block, token_count, why_included: {...}}` — written for EVERY call, referenced from the run report. Purpose: when a verdict is wrong, we can prove whether the model had the information (model failure) or not (engine failure). This artifact is also thesis material: it makes the memory-injection design measurable (quality-per-token per call).

## 4. Non-destructive identity overlay (replaces destructive auditor)

- **Base layer is immutable:** published identity groups/atoms are never edited in place.
- **Overlay = versioned mapping records:** `{overlay_id, kind ∈ merge_into | split | reclassify, subject, target/partition, evidence_refs, proposer ∈ llm | human, status ∈ proposed | confirmed | human_approved | rejected, created_at_scope}` — append-only.
- **Render/query-time resolution:** bible rendering and pack building resolve identities THROUGH the active overlay (mechanical map application). Downstream stages consume resolved views; base artifacts stay valid forever, so **no dependency DAG, no replay closure, no revision re-audit**.
- **Destructive apply exists only as a human action** (H1 contract artifact) and even then is executed as a new generation (staged + pointer switch), never in-place.
- LLM's role shrinks to PROPOSING overlay records with evidence — consistent with fail-closed doctrine and code-never-does-language-work.

## 5. Escalation ladder (token discipline)

L1 atom + local scene → L2 + global roster/candidate cards → L3 + wide scene/chapter slice → L4 human. Per-level token budgets measured per model; only unresolved cases climb; every escalation is a new fingerprinted call recorded in the Context Audit. No full-book prompts.

## 6. Acceptance — canary re-run (same model, only context changes)

Controlled experiment, GPT-5.4 UNCHANGED:
1. **madam**: with sentence-complete scene + B0 kind join → expected nonperson (or pending_human, never auto-person).
2. **b004 master/mistress**: with hints demoted + as-of discipline → expected Catherine/Edgar linkage or explicit uncertainty; NEVER silent Earnshaw merge.
3. **Two Jabez + T' maister/Cathy**: with global roster → correct candidates appear in-context; merge/keep verdicts with evidence.

Pass = all three flip from their pilot failures with context as the only changed variable (fingerprint pass proves prompt deltas). Then pilot ch1–4 re-run under the new contract, R4-style acceptance vs corrected ground truth, THEN the 34-chapter scale decision (user's call). If any canary still fails on clean context, that becomes the first real evidence of model limitation — only then do we discuss prompt/model changes.

## 7. Questions for Sol (round 1)

- S1: Does the overlay model (base-immutable + render-time resolution) satisfy your round-2/3 concerns (partition safety, replay closure) by construction, or do you see residual cases where overlay resolution is ambiguous (e.g., overlapping splits)?
- S2: Sentence-complete expansion — is typographic sentence-boundary ± N the right mechanical rule, or do you want scene-break-bounded windows?
- S3: Hint demotion to neutral candidates — enough to kill anchor bias, or should surface-match candidates be withheld entirely from the first call and only enter at L2?
- S4: Context Audit schema — anything missing to make engine-vs-model attribution decidable in one artifact?
- S5: Canary pass criteria — agree that pass requires flip-with-context-only-change, and that a canary passing via pending_human counts as engine success (fail-closed), not failure?

---

# M4f REV2 DELTA (2026-07-11, after Sol round 1 — ALL 4 BLOCKERs + 5 MAJORs accepted; Claude verification notes + 2 independent Claude findings. Supersedes conflicting rev1 text.) Status: **rev2 — for Sol round 2.**

Claude gate notes (verified before acceptance):
- **B1 (B0 join) CONFIRMED + own-goal acknowledged:** B0 cast surface `the canine mother` (b017) vs atom surface `madam` (b019) share NO exact key — any code join is language work, which rev1 §2 item 3 implicitly required. Rev1 violated the locked code-never-does-language-work principle; Sol caught it. Also confirmed: B0 reads the whole chapter, so `role_hint` provenance is un-scoped (max_source_block unknown) — the join input itself is future-contaminated.
- **B2 (demote insufficient) ACCEPTED:** anchoring bias operates by presence, not by label; a wrong answer shown "neutrally" still anchors.
- **B3 (overlay "by construction" overstated) CONFIRMED:** phase rows store no source event ids (verified round M4e-3, render at story_bible_v2.py:2263); facts/phases key on final entity ids → merge collides pairs, split cannot assign children. Rev1's claim that render-time entity mapping removes replay was WRONG at the data level.
- **M6 (sentence ±N) CONFIRMED on source:** full b019 (1,024 chars) contains "the ruffianly bitch … irritated madam" — the referent-determining clause was IN THE SAME BLOCK, destroyed by the character-slice. Full active block alone disambiguates madam; b017→b018→b019 shows adjacent-block chains matter too. (Side lesson recorded: earlier source grep returned empty because block text lives in `clean_text`, not `text` — empty grep ≠ absence.)
- **B4, M5, M7, M8, M9, M10 ACCEPTED** on design logic (M10 additionally grounded: pilot ch3 frame is wrong-by-omission, verified in M4e round 2).

## §2' — B0 cast claims with provenance (supersedes §2 item 3 "B0 kind join")

- B0 runs **unit/scene-local** (folds into the B0-per-unit rule from M4e D''). Every cast row becomes a **cast claim**: `{cast_claim_id, surface, surface_kind, role_hint, source_block_ids, scene_range, max_source_block, quote}`.
- Identity call receives ALL cast claims whose scene_range intersects the mention's scene, presented as **untrusted claims**; the LLM does the madam↔canine-mother linking. **Code never joins surfaces.**
- As-of validity: a claim enters context only if `max_source_block ≤ as_of_block` (CONTEXT_ONLY-labeled tails excepted).

## §2'' — Future-derived context is EXCLUDED, not demoted (supersedes rev1 hint demotion)

- Any claim/hint with `max_source_block > as_of_block`, unknown provenance, or full-chapter-derived lineage is **excluded entirely** from the rendered context and logged in the Context Audit as `{item, reason}`.
- Existing B1 `hint_entity_id` values (all minted under the whole-chapter brief) are therefore ALL excluded from identity calls. Candidate surface matches are **recomputed from clean as-of evidence** by mechanical exact/casefold surface-and-alias match against the registry-as-of — no fuzzy matching (that would be language work); non-exact linking happens only inside the LLM call via cast claims + roster.

## §2''' — Two-step identity protocol (supersedes rev1 single-call items 5–6)

1. **Candidate-retrieval call:** local context + lightweight global roster → returns candidate entity IDs (+ "none/new"). Roster over token cap → sharded retrieval, **union** of candidate IDs across shards; never silent truncation.
2. **Adjudication call:** local scene + FULL candidate cards (two complete dossiers when merge/split is on the table) → verdict.
- Cost impact goes into deterministic_base (identity calls ×2 worst case); measured, not assumed.

## §2'''' — Context unit = full block, scene-bounded (supersedes sentence ±N)

- Minimum context = the **full active block**. Expansion = adjacent blocks up to scene boundary or token cap, whichever first. Sentence spans are used ONLY to highlight evidence inside the delivered blocks, never to bound what is delivered.
- **Scene boundary fallback (Claude independent point, needs Sol confirmation):** WH chapters often lack typographic scene breaks; when no scene break exists, the deterministic fallback is a block-neighborhood cap (±K blocks, config-hashed, measured). Detection stays typographic (blank line / asterism / chapter edge) per the locked scene-break rule.

## §2''''' — Bounded phase timeline in phase calls (supersedes "open phases + new events only")

Phase call context includes a **compact committed timeline** for the pair: `[{phase_label, interval, trigger_event_ids}]` — labels/intervals/ids only, no quotes. Reconciliation/recurrence (friend→enemy→friend) stays visible; token cost stays flat.

## §3' — Context Audit v2 (supersedes rev1 schema)

Add per call: `request_fingerprint` (full canonical API request hash, per M4e round-4 B1), `system_prompt_hash`, rendered **section hashes/text refs** with per-section token counts, `source_block_ids` + `max_source_block` per section, `candidate_pool_hash`, retrieval policy id+version, candidate ranks/scores, truncation decisions, output verdict + validator result. `excluded` stores counts + ids/hashes (no unbounded dumps). Attribution rule: engine-vs-model is decided ONLY when the audit proves the evidence bytes were in the rendered request.

## §4' — Occurrence-level overlay (supersedes rev1 entity-level "by construction" claim)

- **Evidence base at occurrence level:** immutable atom/mention/turn/event-endpoint IDs are the ground layer for everything downstream. Facts/phases/views carry event/atom provenance from the migration run onward (M4e A''''' migration note carries over: no retrofit of old rows — the coordinated re-run writes provenance from the start).
- Overlay **split = exact partition over the entity's atom IDs**; derived pair views remap from event endpoints, not from entity ids. Pair collision after a merge, or provenance too thin to reassign → the affected view rows become **stale/blocked** (excluded from runtime render, kept in state) — never mechanically re-keyed.
- **No generic DAG**, but two mechanisms stay: overlay-version lineage (each generation knows its parent) and **materialized-view invalidation** (bible/pack renders are stamped with overlay_version; version bump invalidates renders, which rebuild mechanically from base + overlay).

## §4'' — Overlay conflict & activation semantics (new, closes B4)

- Overlay generations form a chain: `{overlay_version, parent_overlay_version, records[]}`. **Only human_approved records are ACTIVE**; LLM proposals live in proposed/rejected and never affect rendering. (`confirmed` status dropped — it was ambiguous.)
- Per-generation validator: applying active records to the base must yield a **canonical partition** — no cycles, no overlapping subjects (an entity may be subject of at most one active merge/split lineage), merge-after-split and reclassify-over-merge resolve through the partition or the generation is rejected. **Any conflict blocks the WHOLE generation from publishing.**
- Every translation/render run **pins its overlay_version** in config_hash + report.

## §5' — M2 frame/story-time as versioned claims (new, closes M10)

`narration_frame_segments` become claims: `{segment, source_block_ids, status ∈ verified | uncertain, version}`. Identity/phase context receives frame = **unknown** when the covering claim is uncertain — a wrong frame is never delivered as ground truth (pilot ch3 is the proof case). B3 v2 embedded-document segmentation (diary/dream) remains the upgrade path from uncertain → verified.

## §6' — Causal A/B acceptance (supersedes rev1 canary criteria)

- **Held constant:** system prompt, validator, model, temperature/seed policy, response_format, caps. **Only the context payload changes** between arms.
- **≥2–3 fresh replicates per arm.** (Claude independent point: replicates MUST use `bypass_cache` — the local replay cache is keyed on the request, so identical replicates would replay one response and measure nothing; replicate cost enters the estimator explicitly.)
- **Three gates, reported separately:**
  1. **context-delivery** — audit proves the clean evidence bytes are in the rendered request;
  2. **safety** — no wrong auto-publish; pending_human COUNTS as pass;
  3. **resolution-quality** — correct referent; pending_human does NOT count as pass.
- Only a **stable resolution flip** (consistent across replicates) confirms the root-cause hypothesis. Safety-only success = fail-closed works, context question stays open.

## Round-2 ask to Sol

The three lock items from your verdict map to §2'/§2'' (B0 claims as-of with provenance + hard exclusion), §4'/§4'' (occurrence-level overlay + conflict/activation semantics), §6' (three-gate A/B). Plus two Claude additions for your check: scene-boundary fallback ±K when no typographic break exists (§2''''), and bypass_cache requirement on replicates (§6'). Flag residual holes; otherwise LOCK so prompt authoring and CodeX phasing start.
