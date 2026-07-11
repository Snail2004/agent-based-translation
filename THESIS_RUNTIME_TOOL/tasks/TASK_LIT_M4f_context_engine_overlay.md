# TASK_LIT_M4f — Context Engine v2 + non-destructive identity overlay (PIVOT from M4e, rev1 for Sol round 1)

Status: **DRAFT rev1 — awaiting Sol critique round 1. Do NOT implement.**
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
