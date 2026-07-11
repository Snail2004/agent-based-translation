# TASK_LIT_M4f — Context Engine v2 + non-destructive identity overlay (PIVOT from M4e, rev5, for Sol round 5)

Status: **DRAFT rev5 — awaiting Sol critique round 5. User confirmations recorded (whole_book_frozen + zero-human). Do NOT implement.**
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

---

# M4f REV3 DELTA (2026-07-11, after Sol round 2 — ALL 5 BLOCKERs + 4 MAJORs + 1 MINOR accepted; Claude verification notes inline. Supersedes conflicting rev1/rev2 text.) Status: **rev3 — for Sol round 3.**

Claude gate notes:
- **B-endpoint CONFIRMED on real artifacts:** event endpoints in M1 narrative output are `{surface: "I"|"he"|"sir", reference_kind, resolution_status, candidate_entity_ids, attribution_method, confidence}` — **no source_atom_id** (verified on `literary_m4_full` narrative artifacts + `_event_index` builder :531–555, which copies only actor/target dicts). Pronoun endpoints have no atom linkage at all; atom-level partition cannot assign them after a split. Rev2 §4' was incomplete exactly as Sol says.
- **B-±K ACCEPTED — second own-goal acknowledged:** rev2's scene-bounded "adjacent blocks" (and my symmetric ±K fallback) re-opens future leak under a new name; a CONTEXT_ONLY label does not stop the model from using next-blocks for identity. Same error class as the b004 hint leak I confirmed two rounds ago — I proposed it anyway. Recorded.
- **B-B0-self-declared ACCEPTED:** `max_source_block` is model-declared output; trust-the-request-not-the-claim is the same principle as the fingerprint rule (M4e round-4 B1). If B0's request contained future blocks, every claim it emits is future-informed regardless of the quoted block.
- **B-overlay-temporal ACCEPTED:** a ch30 decision applied to a ch5 pack is future leak at the overlay layer — as-of must hold across ALL layers, overlay included.
- **B-frame-verified ACCEPTED:** LLM self-issued `verified` would launder a wrong frame into ground truth; pilot ch3 is the standing proof that B3 frame output can be wrong.
- **M-candidate-bound / M-immutable-request / M-AB-topology / M-timeline-pin / MINOR-ablation ACCEPTED** on design logic.

## §2-B0' — B0 causal scope proven by request, not claimed (closes B1; supersedes rev2 §2' provenance trust)

- Context Audit for every B0 call records **b0_input_block_ids / input_max_block from the actual rendered request**.
- A cast claim is usable in **online-as-of** context ONLY if its whole B0 request satisfied `input_max_block ≤ as_of_block`. Claims from B0 calls whose request saw beyond as_of are **offline-reconciled only** (never delivered to online identity/phase calls, never causal evidence for canaries or as-of Translator packs).
- Practical consequence: B0-per-scene calls are naturally as-of when scenes are processed in order; the audit proves it per call instead of assuming it.

## §2-K' — Two context modes, no next-tail online (closes B2; supersedes rev2 §2'''' expansion)

- **online_as_of:** full active block + up to K blocks strictly BEFORE the mention (config-hashed K), bounded by scene start. **No next-tail, ever.** CONTEXT_ONLY labels are not an as-of mechanism and are dropped from the online path.
- **offline_reconcile:** two-sided context allowed; every output carries `knowledge_mode: offline` and is excluded from causal canary evidence and from as-of Translator packs. (Offline mode exists for end-of-book reconciliation reports, not for the online pipeline.)

## §4-E' — Endpoint ground layer (closes B3; supersedes rev2 §4' atom-only partition)

- New immutable ground row per event/turn endpoint: `{endpoint_id, event_id|turn_id, block_id, surface, source_atom_ids?, resolution_evidence}` — written by the migration run for all M1 narrative events (mechanical projection from existing actor/target dicts + block ids; missing atom linkage stays empty, never fabricated).
- Overlay resolution covers **both atoms and endpoints**. A split's exact partition assigns atoms; endpoints resolve through their `source_atom_ids` witnesses or an explicit LLM/human endpoint-resolution record. **Endpoint without a unique witness → every dependent fact/phase/view goes stale/blocked** — it never silently inherits the pre-split entity.

## §4-T' — Overlay temporal semantics (closes B4; extends rev2 §4'')

- Every overlay record adds `{decided_at_scope, knowledge_available_from_scope}`; every query/pack carries a **query mode**:
  - **online-as-of:** only records with `knowledge_available_from_scope ≤ scope` apply — a ch30 discovery never rewrites a ch5 pack;
  - **offline-final:** full retroactive reconciliation, output labeled as such.
- Translation reports pin **overlay_version + knowledge_cutoff_scope** (both in config_hash).

## §5-F' — Frame verification authority (closes B5; supersedes rev2 §5' status semantics)

- B3 emits frame claims as **proposed/uncertain only** — the model cannot self-issue `verified`.
- `verified` requires human approval (H1 contract) or an independent confirmation call (two-key, same rule class as destructive verdicts). The mechanical validator may only reject malformed ranges — it never promotes status.
- Unverified coverage ⇒ identity/phase context receives frame = **unknown**.

## §2-C' — Bounded candidate adjudication (closes M-candidate; extends rev2 §2''')

- If the retrieval union exceeds the dossier cap: adjudicate in **candidate batches** (full cards per batch), then one **bounded comparison call** over per-batch group summaries; no convergence → human. **No silent rank-cut** at any step.
- Context Audit reports: candidate recall pool (ids + hash), batch coverage map, and the comparison outcome.

## §3-R' — Immutable rendered-request artifacts (closes M-immutable; extends rev2 §3')

- Every call's canonical rendered request body is persisted **append-only, content-addressed** (path + SHA in the checkpoint manifest). Hashes prove equality; the stored body lets a reviewer verify evidence bytes even if referenced files later change.
- Audit adds the **exact selection-universe hash** (the full pool retrieval selected from), proving which candidates could have been dropped pre-retrieval.

## §6-AB' — Clean causal isolation (closes M-AB; supersedes rev2 §6' arm design)

- **Both arms run the NEW topology** (two-step protocol, same system prompts, validator, model/config). The ONLY difference: Arm A renders context per the OLD data policy (character-slice, B1 hints present, no cast claims); Arm B per the NEW policy. Overlay disabled in both arms.
- Fresh replicates (bypass_cache, rev2 rule) are **interleaved** across arms to neutralize backend-time drift; occurrence-level ground truth is **preregistered** before any run ([dont-tune-intervention-on-test] discipline).
- **MINOR adopted — madam ablation ladder:** old-policy → full-block-only → full-block + B0-claims, measuring the marginal value of each context component separately.

## §2-P' — Timeline rows pinned (closes M-timeline; extends rev2 §2''''')

Compact timeline rows carry `{overlay_version, frame_ref, source_event_ids}`; any mismatch with the current view's overlay/frame versions ⇒ the row is blocked and the timeline rebuilt before injection — stale identity/frame data never reaches a phase call.

## Round-3 ask to Sol

The four open root contracts from your verdict are closed as: B0 causal scope proven from rendered requests (§2-B0'), strict no-next-tail online mode (§2-K'), endpoint-level ground layer + witness rule (§4-E'), overlay temporal as-of with knowledge scopes (§4-T'), plus frame verification authority (§5-F'). Verify-only pass requested; flag residual holes, otherwise LOCK — prompt authoring (Claude) and CodeX phasing start at LOCK.

---

# M4f ADDENDUM R (2026-07-11, retrieval-channel scope — agreed Claude+Sol during rev3 wait; folds into the LOCK)

Context (verified on repo): architecture lock = "GraphRAG-shaped offline, keyed-lookup online" + hybrid retrieval (THESIS_ARCHITECTURE_LOCK.md:340, Neo4j rejected :561); Chroma 3 collections (similar_passages / narrative_motifs / translation_memory) implemented + preflighted (TASK_P4_01 DONE) but runtime hybrid retrieval + D6 NOT validated (design/LITERARY_RECONCILE_V1.md matrix); live pack path is SQL-only (`build_context_pack(conn: sqlite3.Connection, ...)`).

**R1 — Staging decision (answers "is it vector time?"):** NO vector in M4f canaries. The A/B (§6-AB') isolates the context-packaging variable; adding a retrieval channel would confound it. Order: (1) M4f graph/keyed as-of retrieval fixed + canaries pass → (2) vector added as a SEPARATE retrieval channel with its own ablation arm → (3) D6 gate (hard recall, low_context, Recall@5/MRR, precision-noise) → (4) only then full hybrid pack for Narrative/Translator. The SQLite temporal store IS the graph — no new DB.

**R2 — Candidate channel provenance in Context Audit (Sol's addendum, adopted):** every candidate/context item records its retrieval channel: `source_channel ∈ exact_alias | graph_neighbor | fts | vector_passage` (+ rank/score per channel, already in §3'). Without this, a wrong verdict cannot be attributed to the failing channel.

**R3 — As-of applies to EVERY retrieval channel, vector included (Claude addition):** the Chroma index spans the whole indexed corpus, so `similar_passages` hits can come from FUTURE blocks — a vector query is a future-leak channel by default. Rule: in online_as_of mode, every retrieval channel (exact/graph/FTS/vector) filters hits to `block_id ≤ as_of_block` BEFORE ranking; the Context Audit records pre-filter and post-filter hit lists per channel so leak-by-retrieval is provable/refutable. Offline_reconcile mode may search two-sided under its non-causal label. Vector results are candidates only — never identity authority (cosine similarity never concludes "same person"; adjudication stays with the LLM under §2''').

**R4 — Collection timing:** `translation_memory` only fills at Translator stage (Critic-passed pairs); `narrative_motifs` indexable post-M2; no entity-card collection exists — if B4-style identity retrieval ever needs vector, the path is scene → similar_passages → block_ids → SQLite mentions/endpoints → LLM adjudication, subject to R3.

---

# M4f REV4 DELTA (2026-07-12, after Sol round 3 — ALL 4 BLOCKERs + 5 MAJORs + 1 MINOR accepted; two items resolved by realigning to the GVHD architecture LOCK, flagged for user confirmation. Supersedes conflicting rev1–rev3 text.) Status: **rev4 — for Sol round 4.**

Claude gate notes:
- **B-architecture CONFIRMED on LOCK:** §0.5 (GVHD, non-negotiable) = "whole-book pre-pass then FREEZE before translating (V1)". Rev3's online_as_of-as-pipeline-mode contradicted the locked production architecture — the as-of work of the last three rounds conflated two axes that must be separate (below). §0.6+§0.1 likewise confirm human = future work, pipeline automatic from zero → rev3's human-only overlay activation violated the LOCK. Both are realignments, not new design: the LOCK wins.
- **B-B0-scene CONFIRMED by construction:** a mention early in a scene still sits inside a request that saw the scene end; and B0 requests also carry REGISTRY_SO_FAR + neighbor summaries (builder_pilot.py:1191) → provenance must be transitive over every section, not just visible block markers.
- **B-endpoint-role CONFIRMED on my own rev3 text:** §4-E' schema lacks the role; real artifacts hold TWO role-bearing dicts (actor/target) per event — directional facts cannot remap after split without the role.
- **M-chroma CONFIRMED on code:** passage metadata = {block_id, doc_id, chapter_id, text} — no canonical order; where-filter pre-ANN is unimplementable today (deferred to vector arm, contract recorded now).
- Remaining findings accepted on design logic.

## §K'' — Two axes: knowledge_mode × story_validity_as_of (closes B-architecture; supersedes rev3 §2-K'/§4-T' single-axis framing)

- **Axis 1 — knowledge_mode** (what the system knew when deciding): `whole_book_frozen` (production V1 per LOCK §0.5) | `as_of_experiment` (causal canary mode) | `streaming` (future work).
- **Axis 2 — story_validity_as_of** (what a pack may RENDER at story block X): relation/phase/address/alias validity intervals are ALWAYS filtered by story time, in every knowledge_mode. A ch30 marriage is never rendered active in a ch5 pack; but under whole_book_frozen the system legitimately KNOWS ch30 identity when adjudicating.
- Production V1 flow: whole-book pre-pass → identity adjudication with full frozen evidence → FREEZE → translate with story-valid packs. **online_as_of (strictly-prior-K etc.) applies to the canary A/B and any future streaming mode; it is NOT the production pipeline mode.**
- The b004/hint class remains a defect in BOTH modes — not because future knowledge is forbidden in production, but because unadjudicated B1 hints were presented as authority. The fix (cast claims + roster + adjudication protocol) is mode-independent; the EXCLUSION rules of §2'' bind the as_of_experiment mode.
- Every artifact/report stamps both axes. **USER/GVHD confirmation requested (expected: no change — LOCK already decides production = whole_book_frozen).**

## §O'' — Overlay activation: automatic fail-closed quarantine (closes B-human-dependency; supersedes rev3 human-only activation)

- **Auto-active class 1 (additive):** records that only ADD visibility — alias→entity binding with a unique mechanical witness — activate automatically after mechanical validation.
- **Auto-active class 2 (destructive proposals → quarantine):** LLM-proposed merge/split/reclassify NEVER rewrite mappings automatically; instead they auto-activate as **quarantine**: affected entities/pairs/views become blocked_for_runtime (excluded from packs, retained in state) — the same fail-closed semantics as the locked §5.4 pair quarantine. Wrong identity is thus never silently used AND never silently rewritten, with zero human in the loop.
- **Human approval = optional upgrade** (quarantine → applied mapping), explicitly future work per LOCK §0.6. Scale never depends on it; reports carry quarantine counts and the scale gate sets thresholds on them.

## §B0'' — Transitive request lineage (closes B-B0-scene; supersedes rev3 §2-B0' block-marker provenance)

- `input_max_order` of a B0 (or any) call = **max canonical order_index over the transitive lineage of EVERY rendered section**: text blocks + registry snapshot rows (each row carries its own lineage) + neighbor summaries (lineage = their source chapters) + any joined artifact. Computed by code from the rendered request; never model-declared (extends the round-2 trust-the-request rule).
- In as_of_experiment mode: claims usable at mention M only if `input_max_order ≤ order(M)` — current-scene B0 claims therefore do NOT qualify for early-scene mentions; the identity call's K-prior full blocks carry that local evidence instead (madam: b017 sits within K of b019). Prefix/window B0 snapshots are the upgrade if K-prior proves insufficient — measured, not assumed.
- In whole_book_frozen mode lineage is still stamped (audit + reproducibility), it just doesn't gate usage.

## §E'' — Endpoint schema completed (closes B-endpoint-role; supersedes rev3 §4-E' row shape)

Ground row key = `(event_or_turn_id, endpoint_role)`, `endpoint_role ∈ actor | target | speaker | addressee`. Row preserves verbatim: `reference_kind, resolution_status, candidate_entity_ids, attribution_method`, base entity binding, `source_artifact_hash`, plus rev3 fields (block_id, surface, source_atom_ids?, resolution_evidence). Directional facts remap through (id, role) after split; a role row without a unique witness → dependents stale/blocked (rev3 rule unchanged).

## §T'' — knowledge_available_from_scope computed, never declared (closes M-self-declared)

`knowledge_available_from_scope` = code-computed canonical **order_index** = max(evidence lineage per §B0'', decision-time scope, approval-time scope). String chapter-id comparison banned; LLM-returned values for this field are ignored by contract.

## §V'' — Overlay dependency stamps per entity/pair (closes M-global-version)

- Timeline/view rows stamp `{overlay_dependency: {entity_ids, pair}, overlay_version_at_build}`; an overlay change invalidates ONLY rows whose dependency intersects the changed records — no global stale.
- View rebuilds are mechanical (base + overlay). **Phase re-derivation (LLM) triggers only when a row's INPUT set actually changed**, and those calls are priced in the estimator's contingent reserve (M4e E''' carries over). If a global version bump is ever chosen operationally, its full rebuild cost must be declared in the preflight — no silent global invalidation.

## §AB'' — Arm sourcing fixed + held-out generality set (closes M-generality)

- **Arm A regenerates** old-policy context through the SAME new topology (not replayed old artifacts) — the experiment tests the DATA POLICY, not artifact staleness; both arms' raw responses stored content-addressed.
- Canaries (madam, b004, two Jabez) are **DEV/regression only** — they shaped the intervention ([dont-tune-intervention-on-test] discipline). Before the 34-chapter gate: a **held-out set** untouched by design — at least one unused WH chapter (+ Gatsby ch1 slice when the unit abstraction lands), preregistered occurrence-level ground truth, same three gates.

## §F'' — Frame confirmation protocol + coverage economics (closes M-frame-confirmation)

- Independent confirmation = second call with **independently rendered request**: shuffled evidence/section order, no first-call output visible, same locked prompt version; agreement on `(narrator_ref, story_time_label, block_range)` → verified; disagreement → uncertain (stays unknown in context).
- New scale metrics: `verified_frame_coverage` (% blocks under verified frame), confirmation call count + cost in preflight; **halt threshold before 34-ch scale** (coverage floor TBD at dry-run — measured, not guessed). This bounds the "everything unknown → phase coverage collapses" risk Sol flagged.

## §U'' — Selection-universe manifest (closes MINOR)

Universe persisted as content-addressed **manifest**: `[{id, source_channel, score/rank, source_row_hash}]` — no full-text dump, but a reviewer can reconstruct exactly which candidates existed at selection time even if the store has since changed.

## §R' — Chroma as-of contract (records M-chroma; defers implementation to vector arm)

Vector metadata gains canonical `order_index` at index-build time; as-of filtering = `where` constraint applied **pre-ANN** (never post-hoc top-k filtering, which lets future hits crowd out valid candidates). Not a canary blocker; contract recorded now to avoid retrofit.

## Round-4 ask to Sol

B1–B4 closed by §K''/§O''/§B0''/§E''; M5–M9 by §T''/§V''/§AB''/§F''+§R'; MINOR by §U''. §K'' and §O'' are LOCK realignments — user/GVHD confirmation of production mode = whole_book_frozen is being requested in parallel (expected: confirm). Verify-only pass; flag residual holes, otherwise LOCK.

## USER CONFIRMATION (2026-07-12) — production mode & human role CHỐT

- **knowledge_mode production = whole_book_frozen** per LOCK §0.5 — confirmed, no change.
- **Zero-human pipeline confirmed as the ideal**; human review = an OPTIONAL app toggle, never a dependency (§O' stands as designed).
- **User rationale (design-relevant, recorded):** the app's target user is translating a book they have NOT read and may lack literary domain knowledge — they cannot adjudicate identity/continuity questions themselves (same reason the user could not judge D2L term correctness). A human gate therefore has LOW epistemic value for the intended audience; fail-closed quarantine + transparent counters is the correct default, and any human-review UI is a power-user option, not a quality mechanism the architecture may rely on.

---

# M4f REV5 DELTA (2026-07-12, after Sol round 4 — ALL 3 BLOCKERs + 5 MAJORs + 1 MINOR accepted; the three blockers are consequences of the two freshly-confirmed decisions, caught before implementation. Supersedes conflicting rev1–rev4 text.) Status: **rev5 — for Sol round 5.**

Claude gate notes:
- **B-disclosure CONFIRMED on code:** `_entity_items` (context_builder.py:417–440) renders `canonical_source -> canonical_target (ALL aliases)` with zero as-of filtering — a ch5 pack discloses ch30 identity. Sol's distinction is the missing piece of §K'': knowing ≠ rendering, and rev4 only filtered relations/phases, not the identity VIEW itself.
- **B-quarantine-DoS ACCEPTED:** rev4's §O'' let ONE unvalidated LLM proposal blank the protagonist (61 atoms at ch4/34 → whole-pair/view fanout) out of every pack. Fail-closed at the wrong granularity is its own failure mode.
- **B-dossier-growth CONFIRMED (data already on file):** ent_mr_heathcliff = 61 atoms at 4/34 chapters; "FULL candidate cards" is unbounded under whole_book_frozen; rev3 batching only bounds candidate COUNT, not per-candidate evidence.
- **M-R3-realign ACCEPTED with honesty note:** Addendum R3 was written pre-two-axis and overgeneralized — correct for as_of_experiment mode, wrong as a production rule.
- Remaining findings accepted on design logic.

## §D''' — Identity disclosure view (closes B-disclosure; extends §K'' axis 2 to identity itself)

- **internal_entity_id ≠ renderable identity.** Internal ids are stable, whole-book, system-only. Packs render `renderable_identity_view(as_of)`:
  - aliases shown = aliases with `valid_from ≤ as_of` (leverages the locked aliases-own-intervals rule);
  - display canonical = the canonical form among as-of-known aliases (the identity as the READER knows it at story time), never the whole-book canonical;
  - future aliases/canonical names appear only when their disclosure interval opens.
- **Soft internal support without disclosure:** Translator may receive constraint fields (grammatical gender/number, person-vs-nonperson, register class) derived from whole-book knowledge — as CONSTRAINTS, never as names/identity statements. This preserves pronoun/xưng-hô correctness without spoiling.
- Relation/phase/address/alias rendering rules from §K'' unchanged; this section closes the identity-name channel they missed.
- context_builder `_entity_items` is thereby declared non-conformant (implementation item, not a design open).

## §O''' — Evidence-gated, minimal-dependency quarantine (closes B-quarantine-DoS; supersedes rev4 §O'' blanket fanout)

- **Evidence gate before ANY quarantine:** a destructive proposal triggers quarantine only if it passes mechanical validation — every evidence quote locates verbatim in its cited block AND cites atoms actually belonging to the subject entity/pair. Proposals failing the gate are recorded as `proposed_invalid`, affect nothing.
- **Minimal dependency scope:** quarantine covers ONLY the disputed unit — the specific mapping/alias binding/fact/pair rows implicated by the proposal's evidence. Occurrence-local atoms, uncontested canonical facts, and unrelated pairs of the same entity REMAIN renderable. A split proposal on Heathcliff quarantines the contested boundary rows, not Heathcliff.
- **Closed definition of unique mechanical witness (auto-bind class 1):** unique = exact surface match (casefold/punct-fold) + kind agreement + **no competing entity carries that surface anywhere in the frozen book** (two Catherine Lintons ⇒ not unique ⇒ no auto-bind, goes to adjudication). Surface equality alone is explicitly insufficient.

## §C'''' — Within-dossier evidence sharding (closes B-dossier-growth; extends §2'''/§2-C')

- Per-candidate dossier over `max_dossier_tokens` (per-model, measured) shards its EVIDENCE with an **exact-cover manifest**: every member atom in exactly one shard + shared anchor atoms (earliest/latest/dispute-cited) across shards; validator checks cover exactly.
- **Bounded reconciliation:** shard-level verdicts join over anchors in ONE reconciliation call fed by shard summaries + anchor rows; non-convergence → quarantine per §O''' (minimal scope). **No silent truncation or rank-cut of evidence anywhere** — every omission is a manifest entry.

## §AB''' — Two experiments, not one (closes M-AB-isolation; supersedes rev4 §AB'' single design)

1. **Payload-ablation (canary):** BOTH arms consume the SAME frozen upstream evidence (the pilot's existing B0/B1 artifacts, content-addressed); arms differ ONLY in pack policy (old character-slice+hints vs new contract). This reproduces the b004 mechanism exactly — nothing upstream regenerates.
2. **Production-mode pilot:** whole_book_frozen end-to-end on ch1–4-equivalent units under the new engine — validates the mode we actually ship.
- **Held-out chapter ID is preregistered BEFORE implementation** (not merely before API runs), recorded in the task file at LOCK.

## §R'' — Vector policy realigned to two axes (closes M-R3; supersedes Addendum R3 blanket rule)

- `retrieval_knowledge_mode`: in whole_book_frozen production, vector search MAY retrieve future passages as internal evidence for identity adjudication (legitimate under LOCK §0.5).
- `render_story_validity`: raw future passages/motifs NEVER flow into Narrative/Translator packs; anything vector-sourced that reaches a pack passes the same disclosure/story-validity filters as §D'''/§K''.
- as_of_experiment mode keeps the strict pre-ANN `order_index ≤ as_of` filter from R3. Channel provenance + pre/post-filter audit lists unchanged.

## §V''' — semantic_input_hash (closes M-dependency-stamp; extends rev4 §V'')

Every phase/view row stores `semantic_input_hash` = hash over {endpoint bindings, source event ids, frame claim version, active overlay records intersecting the row}. **Reuse permitted only on hash equality** — entity/pair id stability alone proves nothing (witness/frame/disposition can change under stable ids).

## §F''' — corroborated vs verified (closes M-frame-confirmation; supersedes rev4 §F'' status semantics)

- Same-model shuffled-render agreement ⇒ **corroborated** (not verified — correlated failure modes remain).
- **verified** requires: independent evidence source, OR a different checker model/prompt, OR human.
- Runtime contract declares explicitly which statuses feed context: v1 = corroborated frames usable for phase/identity context WITH their status label carried into the Context Audit; unverified stays unknown. verified_frame_coverage metric (rev4) now reports per-status.

## §S''' — Scale thresholds locked on DEV, weighted (closes M-threshold)

- Thresholds locked on DEV (ch1–4 lineage) BEFORE held-out runs; never adjusted on held-out ([dont-tune-intervention-on-test]).
- Metrics are **exposure-weighted**, not raw entity counts: % occurrences quarantined, % dialogue turns affected, % relation events blocked, % packs degraded. One quarantined Heathcliff outweighs ten one-shot allusions by construction.

## §U''' — Immutable store snapshot (closes MINOR; extends §U'')

Selection-universe manifests reference a **pinned immutable store snapshot** (content-addressed source-row blobs or snapshot hash of the frozen SQLite/Chroma state). Rows remain reconstructable after store mutation; manifests stay reference-only.

## Round-5 ask to Sol

B1–B3 closed by §D'''/§O'''/§C''''; M4–M8 by §AB'''/§R''/§V'''/§F'''/§S'''; MINOR by §U'''. The disclosure view (§D''') is the one genuinely new mechanism — please stress it specifically: any channel we missed where whole-book identity can still reach a pack (address policies? phase labels naming entities? glossary lines?). Otherwise LOCK.
