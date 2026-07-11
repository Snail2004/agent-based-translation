# TASK_LIT_M4e — B4 v2 REWORK SPEC: Identity Auditor + scale-boundedness (rev1, for Sol critique round)

Status: **DRAFT rev1 — awaiting Sol (xhigh/max) critique round 1. Do NOT implement.**
Owner: Claude (spec + prompts + verify gate). Implementer: CodeX (after LOCK). Decision authority on scale unlock: user.
Parent: TASK_LIT_M4d (pilot ch1–4 COMPLETE; Sol dual review verdict NEEDS-REWORK, verified & accepted — see M4d gate records for the evidence chain).
Prereqs already dispatched (NOT in this spec): R1 pair-coverage fail-closed (code-only), R2 validator_contract_version (both CodeX, no prompt changes, caches intact).

## 0. Evidence base (all measured on pilot artifacts — no theoretical claims)

- Cross-person merges published as resolved T2: `ent_the_mistress` = Catherine(b004) + Mrs.Earnshaw(b037/b040); `ent_the_master` = Edgar(b004) + old Earnshaw(b035–b044). Both contaminations anchor at wh_ch04_b004 (Nelly's frame-present retrospective sentence).
- Non-person published as person: `ent_madam` (ch1 b019 = the dog), status resolved, in T2 since ch1.
- Under-merge: ~8 PURE fragment clusters / ~10 fragment entities in 4 chapters (old Earnshaw ×2, Edgar ×1(+contamination), daughter ×2, mother ×2(+minx review), Hindley ×2, Mrs.Earnshaw ×2–3, Jabez ×2). Pattern: **epithet/title canonical surfaces mint fragments; named surfaces reuse correctly**. Facts/phases split across alias-entities (daughter_in_law_of sits on ent_mrs_heathcliff only; two Heathcliff–Catherine phases on two different pairs).
- Boundedness: phase prompt ch4 = 12,699 / 14,000 cap (91% at chapter 4/34); `_shard_for_cap` cannot split a single pair-history item. Identity ch3+ needs ≥2 shards; output cap already raised once (6,144→12,288 at ch3).
- Frame: ch4 narrator switch segmented correctly (Lockwood frame_present + mrs_dean retrospective_past); ch3 diary b005–b018 + dream/ghost NOT segmented (M2/B3 emitted one frame_present segment) → diary-time phase (catherine↔heathcliff friendly from b018) is indistinguishable from present-time state at pack-query time.
- Latent wipe (R1 scope, listed for coherence): model-omitted pair loses accumulated history silently; empty response bypasses the all-blocked guard.

## 1. Non-negotiables (locked principles, carried over)

1. Code never does language work: identity/segment/label judgments are LLM-only; code does contract enforcement, unique-mapping normalization, composition of RECORDED judgments, and gated apply.
2. Fail-closed: unverifiable claims quarantine or halt; no regenerate-on-semantic-reject; technical retry = 1 with bypass_cache.
3. Prompts are book-neutral, loaded from design-doc blockquotes by version marker, authored by Claude, verified via the real loader.
4. Every run declares rendered prompts + real usage; every optimization is gated by quality-equivalence; upper bounds estimated pre-flight, measured post-flight.
5. Stable IDs: survivor-id rules are deterministic; supersession is recorded, never silent.
6. All prompt-affecting changes in this rework land in ONE coordinated migration (single cache invalidation, single re-run of ch1–4) — no piecemeal reruns.

## 2. Area A — Identity Auditor stage (§5.6 made concrete)

### Position
A new LLM logical stage `literary_identity_audit_v1` (gpt-5.4) runs **per scope, after identity apply, BEFORE phase batch construction**. Rationale for in-loop placement: (a) phase pairs are keyed by entity ids — auditing first prevents paying phase calls on fragments that will merge; (b) the blocked pair (heathcliff↔the_master) exists only because fragments reached the phase layer.

### Audit candidate selection (code, mechanical — selection is not judgment)
Audit set per scope = union of:
- every entity MINTED in this scope;
- every entity REUSED in this scope whose reuse came through ladder rules 2–4 (hint composition / mint-equality / duplicate-union) — i.e., every id that code touched mechanically;
- every entity sharing an alias surface-key or hint key with any of the above (collision neighborhood);
- every review_only person-kind group of this scope (madam-class re-check).
Over-selection is safe (more review); under-selection is the risk. NOT selected: entities untouched in this scope with no key overlap. Estimated pilot size: ch1=6, ch2≈8, ch3≈14, ch4≈16 entities/call — one auditor call per scope fits comfortably under caps (entity cards are compact: canonical, aliases, member atom quotes ≤2 each, block ids).

### Auditor verdict schema (per audited entity)
`{entity_id, verdict: keep | merge_into | split | reclassify, target_entity_id?, atom_assignments?, referent_kind?, evidence: [{block_id, quote, source_atom_ids}], confidence: high|low}`
- `merge_into`: this entity is the same person as target → supersession.
- `split`: atoms listed per side with evidence (the b004 case: ent_the_mistress splits b004 → target Catherine-entity, b037/b040 stay).
- `reclassify`: person → non_person/group_reference/literary_allusion (madam case) → leaves T2.
- Every verdict REQUIRES verbatim block-anchored evidence (substring-checked, punct-fold, same as identity stage).

### Gated apply (code, §29-precedent three-gate, extended)
Apply a merge/split/reclassify ONLY if ALL hold:
1. evidence quotes verify against source blocks (existing quote gate);
2. **no-conflict**: no different_identity evidence row (from ANY prior identity response in scope chain) links the two sides being merged; kind-consistent; no atom ends up in two entities (exact-partition preserved);
3. **no-new-collision**: post-apply alias/hint key map must not make a previously-unique binding ambiguous in a way that would have changed a PRIOR mechanical decision (guard against retroactively invalidating ladder compositions);
4. low-confidence verdicts and any verdict failing 1–3 → recorded as `audit_pending_human`, entity stays AS-IS but flagged; NEVER silently dropped.
Survivor id = the id with the EARLIEST first-appearance block (deterministic); loser id → `supersedes_entity_ids` on survivor + tombstone entry `superseded_by` so as-of queries on old ids still resolve. Counters: `audit_merges/splits/reclassifies/pending`, all surfaced in report.

### Post-audit replay (mechanical + bounded LLM)
- Id-rekey of phase/fact/alias rows to survivor ids: mechanical substitution.
- Pairs whose timelines COLLIDE after rekey (two fragment-pairs → one pair) → re-segmentation call for those pairs only (merged history, same phase prompt) — the collapsed-pair machinery already built handles batching. Bounded: only collided pairs pay.
- Republish bible for the scope after audit apply (bible is derived state; checkpoint written after the FULL scope pipeline incl. audit).

### OPEN-A (for Sol)
A1. In-loop per scope vs end-of-arc batch audit — cost/quality tradeoff. A2. Should the auditor also see the CURRENT chapter digest for context, or entity cards only (leak/attention tradeoff)? A3. Is `split` v1 or deferred (only merge+reclassify v1, split → human)? Claude leans: split IN v1 — the b004 case is real and blocking re-acceptance.

## 3. Area B — Per-pair bounded phase state (BLOCKER 4)

### Position: committed-timeline incremental (mirror of identity's frontier-incremental)
Phase prompt v2 (`literary_phase_segment_v3` version string) receives per pair: (a) the COMMITTED timeline so far (compact phase rows only — label, from, until, trigger quote), (b) NEW evidence rows of this scope only, (c) an explicit revision contract:
- CLOSED phases are immutable (belief revision on closed intervals is Auditor/human territory, not per-scope churn);
- the model may: extend/keep the OPEN phase, close it (with evidence), open a new phase, or revise ONLY the open phase's label/from-block with evidence;
- **explicit disposition REQUIRED per input pair**: `updated | no_change | blocked_for_review` (folds BLOCKER 1's schema fix into the same prompt change — one invalidation). Omission of any input pair → response rejected (coverage exact).
Estimator reports `max_single_pair_tokens`; projected over-cap single pair → pre-flight halt (never truncate a history silently).

### Rejected alternative
History sharding + LLM synthesis of partial timelines: extra calls, synthesis is judgment-on-judgment, ordering hazards. Kept as fallback only if a single pair's COMMITTED timeline itself outgrows caps (not plausible: timelines are compact).

### Cost note
Re-sending committed timeline ≈ O(phases) not O(events): ch4's 12,699-token prompt (full history) becomes ~timeline(≤20 rows) + current-scope events. Measured at dry-run before lock.

### OPEN-B (for Sol)
B1. Is frozen-closed-phases too rigid — does WH ch9+ (Nelly's long retrospective) need reopening closed phases, and if so under what gate? B2. Disposition enum sufficient, or does `no_change` need evidence too (cost vs laziness-risk: model rubber-stamping no_change)?

## 4. Area C — Frame/story-time (MAJOR 5)

### Position: two-part, sequenced to avoid double re-digest
1. **Pack-join rule (mechanical, NOW, no prompt change):** story_time of a phase/fact/alias interval = frame segment containing its anchor block (range intersection). As-of pack queries MUST carry `story_time` on every row whose anchor falls in a non-present frame; Translator-facing lookups default to frame_present rows unless explicitly asked for retrospective state. Tests: ch4 tale rows join to retrospective_past; a synthetic as-of query at ch4-end must NOT return young-Heathcliff-era register as current.
2. **B3 digest prompt upgrade (bundled into the ONE migration):** narration_frame_segments must segment embedded documents (diary/letters), dreams, and tale-within-tale, with `story_time_label` from the existing enum + `frame_kind` (narration | document | dream | tale). Book-neutral wording. This fixes ch3 (diary + dream) at the digest layer where it belongs — B4 renders faithfully.
No new B4 schema fields unless Sol shows the join is insufficient.

### OPEN-C
C1. Does address-policy need story_time at POLICY level, or is pack-time join enough? C2. ch3 dream (Jabez chapel, ghost at window) — one embedded segment or nested? Claude leans: flat segments, no nesting in v1 — nesting is a modeling rabbit hole; the block-interval algebra stays simple.

## 5. Area D — Consolidation-unit abstraction (pre-scale, from the locked agenda)

Fold the design already recorded (memory: consolidation-unit-token-budget-design) into this migration:
- chain keyed by `unit_id`; `chapter_id` → metadata; cut ladder = author chapter > scene break (typographic only) > block boundary, never mid-block; deterministic + config-hashed;
- per-model measured budgets: B0/M1 (mini) vs M2/M3 (5.4) thresholds measured, not guessed; initial unit budget hypothesis ~4–6k tok for 5.4-digest, VERIFIED on WH ch3 (6.7k, known-hard) + ch9/ch10 dry-runs before lock;
- checkpoint schema → v3 including `validator_contract_version` (R2) + unit fields; prefix rebuild from cache where prompts unchanged.

### OPEN-D
D1. Unit overlap: none (as-of chain) — prior-group injection is the compensation; does Sol see a recall hole at unit boundaries that windows' overlap used to cover at M1 scale? D2. B0 brief per unit or per chapter (brief is cheap; chapter-level brief may aid unit digests as shared context — but that reintroduces a chapter-swallower at mini's LOW threshold)?

## 6. Migration & cost plan (ONE invalidation, ONE re-run)

Prompt/layer changes bundled: B3 digest v2 (frames + units) + phase v3 (incremental + disposition) + auditor v1 (new) + unit-keyed chain (schema v3). Identity partition prompt: UNCHANGED in wording, but its rendered inputs (unit-scoped digest hints) change → cache keys change → identity re-billed. Therefore:
- Order: LOCK all specs → implement → dry-run scaffold (0 API) with rendered prompts + `max_single_pair_tokens` + unit map for Claude gate → single re-run ch1–4-equivalent units with auditor in loop → R4 re-acceptance.
- Cost envelope (upper bound, to be re-estimated at dry-run): M2 re-digest ~50k; identity ~70k; auditor ~35k; phase (incremental, should SHRINK) ~35k; total ≈ 190k ≈ within one UTC day of Key-2 quota (250k). Replay-cache reuse: zero for changed prompts (honest accounting) — budget assumes full fresh.
- Frozen D2L DB untouched; key discipline unchanged.

## 7. R4 re-acceptance (after migration run)

Corrected ground truth: b004 master = Edgar; b004 mistress = Catherine; b026 = Heathcliff (link or explicit audit_pending); madam = non-person. New checks added to the locked M4d set:
1. No cross-person entity in T2 (adjudicate every T2 entity of the final scope against source — the "1 mis-atom" claim class now requires FULL adjudication before any headline number, per the M4d lesson);
2. Fragment count materially reduced WITH provenance (supersession records), two-Catherine canary re-verified atom-by-atom post-merge;
3. `max_single_pair_tokens` reported and under cap at 34-chapter extrapolation (linear fit on units, shown in report);
4. ch3 diary + dream frames present; pack as-of join test proves childhood register ≠ current register;
5. Pair-coverage counters exact (dispositions cover 100% of input pairs);
6. Blocked pair (heathcliff↔the_master) resolved through audit path (merge → replay) or explicitly audit_pending_human — not silently carried.

## 8. Guiding questions for Sol (round 1)

S1. Auditor placement (in-loop vs end-of-arc) and candidate-selection rule — any identity-correctness hole in the selection heuristic? S2. Gated-apply conditions 1–4: sufficient against a WRONG auditor merge (the M4c honorific disaster, now with an LLM instead of code — what stops auditor over-merge of the two Catherines? Claude's answer: gate 2 different_identity conflict + acceptance canary; is that enough or does the auditor need a mandatory two-Catherine-style hard test per book?). S3. Frozen-closed-phases contract (B1 above). S4. Unit-boundary recall hole (D1). S5. Split-verdict in v1 (A3). S6. Anything in the cost envelope that breaks the 34-ch extrapolation.
