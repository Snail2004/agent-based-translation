# TASK_LIT_M4e — B4 v2 REWORK SPEC: Identity Auditor + scale-boundedness (rev3, for Sol round 3)

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

---

# REV2 DELTA (2026-07-11, after Sol round 1 — ALL findings accepted; verification notes inline). Supersedes conflicting rev1 text. Status: **rev2 — for Sol round 2 (LOCK round).**

Claude verification notes: B1 confirmed directly on pilot alias data (ent_t_maister {T' maister/the owd man/the surly old man/our father} vs ent_the_master {the master/the old master/my master}; ent_cathy {Cathy} vs ent_catherine_earnshaw {Catherine */Linton}; two Jabez — zero shared exact surface/hint keys in every case → rev1 neighborhood rule provably blind to the real targets). M6 confirmed directionally (Claude's quick re-sums over-count due to resume-duplicated raws/nested estimates — exactly why the fix is a rendered call graph, not better hand math). Remaining findings are design-level and accepted on argument.

## A' — Auditor (supersedes rev1 §2 selection/gates/split/replay)

1. **Global roster + detailed neighborhood (B1):** every auditor call carries a LIGHTWEIGHT roster of the ENTIRE T2 as-of scope (entity_id, canonical, top-3 aliases, kind — compact; size measured at dry-run) so merge targets are always addressable; DETAILED cards (atoms, quotes) only for the candidate neighborhood. Verdict targeting an id outside the detailed set → **targeted confirmation call** with BOTH full cards before any gate evaluation.
2. **Destructive-verdict two-key rule (B2):** merge/split/reclassify NEVER applies from a single response. Path A: independent targeted confirmation call (both/all full cards, isolated question, no roster) must return the SAME verdict → gated apply. Path B: human approval. Disagreement or gate failure → `audit_pending_human`. Book canaries (two-Catherine) remain regression tests only — they protect nothing on other books and are not a gate.
3. **identity_conflict_ledger (B2):** state/checkpoint persist (a) normalized same/different_identity witnesses from ALL identity+auditor responses (pair-of-atom-sets, block, quote-ref), (b) mechanical-decision dependency records: every ladder composition (hint binding, mint-equality, duplicate-union, witness binding) logs rule + inputs. Gate-2 reads the ledger (no ad-hoc scans); an apply contradicting a witness → reject; an apply invalidating a recorded dependency → forces dependent replay.
4. **Split two-stage (B3):** general auditor emits `split_candidate` (no atom assignments). Targeted split call receives ALL member atoms (existing cap-sharding machinery) → exact-partition gate on the result. Direct split allowed ONLY when the entity is small enough that 100% of atoms were in the card (threshold = card atom budget, fixed at dry-run).
5. **Replay closure (B3):** ANY identity mutation replays ALL dependents — relation phases, facts, speaker turns, address-policy rows touching changed ids. Pure renames → mechanical rekey; collided/split-affected pair timelines → phase re-segmentation calls (bounded, counted).
6. **identity_audit_ledger (M7):** per entity: {scope, validator_contract_version, verdict, evidence_refs, status ∈ adjudicated | pending_human | not_audited}, checkpointed. Headlines MUST report the three buckets; "fully adjudicated" claimable only when pending_human = not_audited = 0 over T2. (R4 wording updated accordingly.)
7. Hybrid cadence (S1): in-loop per scope for witness-backed reclassify/merge; periodic arc-audit sweep (every K units, K fixed at dry-run) for cases lacking in-scope evidence.

## B' — Phase v3 (supersedes rev1 §3 disposition)

- Disposition per input pair PLUS **`considered_event_ids`**; validator requires set-equality with the pair's exact new-event-id set (mechanical). `no_change` needs no new quotes but full evidence accounting (B4 — closes latent evidence-loss).
- Closed phases immutable in normal incremental calls; the ONLY reopening path is a versioned `audit_replay` (post identity/frame correction) or human override (S3).

## C' — Typed frame join (supersedes rev1 §4 single rule)

Three data types, three rules (M5): (i) relation phases + address policies are FRAME-SCOPED — rows carry stable `frame_ref`; as-of queries filter by frame compatibility; an open phase crossing frames splits at the frame boundary at pack time (mechanical range algebra). (ii) Persistent relation facts are frame-INDEPENDENT — a fact narrated in retrospect stays visible in present queries; provenance keeps frame_ref. (iii) Aliases carry their own validity intervals; frame only via anchor provenance. Flat frames v1, no nesting (Sol concurs).

## D' — Unit migration contract (supersedes rev1 §5; B5)

- **unit_manifest** (config-hashed, checkpointed): {unit_id, block_range, parent_chapter, cut_reason ∈ author_chapter | scene_break | token_budget, source_hash, m1_checkpoint_refs}.
- **M1 is PROJECTED, not rerun:** M1 artifacts are block/mention-anchored → mechanical projection into units; chain keeps chapter-level M1 checkpoints referenced by each unit via the manifest. (M1 rerun cost — Sol cites ~413k mini tokens — is thereby avoided; if any layer is ever found non-projectable, its rerun cost must be declared BEFORE lock, not discovered.)
- **M2/B0 context discipline:** each unit call receives 1–2 CONTEXT_ONLY tail blocks from the previous unit; validator BANS evidence quotes located in context blocks (quotes must locate in own-unit blocks). B0 per-unit (S4); no chapter-aggregate layer in v1.
- Prefix validation moves to unit_id sequence; checkpoint schema v3 carries manifest + validator_contract_version.

## E' — Cost preflight (supersedes rev1 §6 envelope; M6)

The 190k envelope is **RETRACTED**. Rule: no headline budget before the dry-run renders the FULL call graph (every call, every model, prompt+completion upper bounds per model, `max_single_pair_tokens`, unit map). GPT-5.4 daily safety gate **225k/250k**; projected overflow → split across UTC days at unit boundaries. Mini and 5.4 accounted separately. Baseline reference (Sol, per-logical-call accounting): M2 43,458 + identity 87,860 + phase 34,539 ≈ 166k actuals for ch1–4 BEFORE auditor/units — treat as floor, not target.

## R4 wording fix (M7)
"100% T2 adjudicated" → "identity_audit_ledger shows pending_human = 0 and not_audited = 0 over final-scope T2, with per-entity verdict + evidence refs; headline reports all three buckets".

Round-2 ask to Sol: LOCK check only — (L1) does the two-key rule + ledger close B2 to your satisfaction; (L2) any hole in mechanical M1 projection; (L3) confirm frame-boundary phase-splitting semantics (C') don't create spurious phase fragments for Translator; (L4) approve the three-bucket honesty rule as the acceptance headline standard.

---

# REV3 DELTA (2026-07-11, after Sol round 2 — ALL 3 BLOCKERs + 4 MAJORs accepted; Claude verification notes inline). Supersedes conflicting rev1/rev2 text. Status: **rev3 — for Sol round 3.**

Claude gate notes (verified on real artifacts before acceptance, per standing rule):
- **B-D' (M1 projection) CONFIRMED:** `builder_pilot.py` B0 reads the whole chapter, `seed_entity_ledger_from_chapter_brief` runs BEFORE the window loop, and `registry_context_from_ledger` + `chapter_brief_text` are injected into every B1 window. Artifact `literary_m4_full/lexicon/wb_wh_ch04_001.json`: window = b001–b008 but the rendered prompt references b009…b045 and carries the full seeded cast (`ent_mr_earnshaw` et al., provenance `seeded:chapter_brief_cast`). Rev2's "M1 projected, not rerun" is INVALID for units smaller than a chapter — future-context influence cannot be filtered out of outputs.
- **B-split CONFIRMED:** ch4 checkpoint `state.m3_state.entities[ent_mr_heathcliff].member_atom_ids` = **61 atoms at ch4/34**; a single targeted full-atom call is both unbounded at 34ch scale and a single-verdict destructive decision — violates the two-key principle rev2 itself established.
- **B-replay CONFIRMED:** committed phase rows carry keys `{pair, phase_label, status, trigger_block, trigger_evidence, trigger_evidence_block, valid_from_block, valid_until_block}` — NO source event ids. Dependents of a split are not computable from published state; rev2's "replay closure of ALL dependents" was intent without a mechanism.
- **M-frame CONFIRMED:** ch1–4 bible `narration_frame_segments`: ch3 = one flat `frame_present` b002–b067 despite diary/dream content.
- **M-E' accepted:** confirmation/split/replay calls depend on Auditor output — a FULL call graph is not renderable pre-run by construction; and per [token-growth-halt] no hand-math substitutes.
- **M-denominator accepted:** merge/tombstone shrink final-scope T2 → survivor-only headline can reach 100% while hiding destroyed-entity errors.

## A'' — Split & merge confirmation (supersedes A' items 3–4)

1. **Exact partition = destructive decision → two keys apply to the PARTITION itself:** the targeted full-atom call's proposed partition must be confirmed by a second independent call (same atom set, order-shuffled, minimal card context) or human approval. Confirmation compares partitions as **set-of-sets equality**; mismatch → pending_human, no apply.
2. **Bounded partition at scale:** if member_atom_ids exceeds `max_partition_atoms_per_call` (measured cap, per-model), the partition runs as overlapping shards with shared **anchor atoms** (top-k earliest + top-k latest + all atoms cited in the split_candidate evidence) + a deterministic global reconcile step: shard-local assignments joined on anchors; any atom with conflicting assignments across shards → pending_human. Cap and overlap size are config-hashed.
3. **Merge verdict comparison is by equivalence class,** not literal direction: A→B and B→A confirm each other; survivor id stays deterministic (earliest first-appearance block) regardless of verdict direction.

## A''' — Dependency ledger & replay closure (supersedes A' item 5)

- **Schema (checkpointed, config-hashed):** `{decision_id, decision_type, depends_on: [decision_id|artifact_ref], source_artifact_hash, produced_row_ids, invalidated_by: decision_id|null}`. Every apply step (identity apply, phase apply, address apply, alias interval write) records produced_row_ids; every consumer records depends_on.
- **Replay = topological:** invalidating a decision invalidates the transitive closure of dependents; replay order is topo order over the ledger.
- **Canonical replay source = immutable M1/M2 evidence artifacts** (atom catalog, speaker turns, relation events as extracted), NEVER merged/published state. Split replays every turn/event/fact/phase whose lineage touches the split entity; **mechanical rekey is banned** for split (allowed for merge only where the rev2 merge rules already permit it, recorded in the ledger).
- **Migration note:** current committed phase rows lack event lineage → the ONE coordinated migration re-run (rev2 §6) must write the ledger from the start; no retrofit of old rows.

## B'' — Per-event disposition (supersedes B' set-equality-only)

`considered_event_ids` set-equality stays as the outer guard, but the unit of proof becomes a **per-event disposition table**: each event id maps to `{disposition ∈ supports_phase | supports_fact | no_state_change | blocked, phase_id?, fact_id?}` (id required when disposition supports one). Validator checks: exact coverage (bijection with the new-event set), every supports_* id exists in the same response, every blocked event carries a reason. `no_change` for the pair is only accepted when ALL its events are individually `no_state_change` — echoing ids without processing no longer validates.

## C'' — Frame boundary = query-time view slice (tightens C')

Frame boundaries **never mint or persist phase rows**. The committed phase row stays ONE row with its full block range; frame-scoped queries slice it at pack/query time (range intersection with the frame segment — pure view logic). If a pack must show a phase continuing across a frame boundary, the view row carries `inherited_from_phase_id` + frame provenance; nothing is written back. Persistent facts remain frame-independent (rev2 C' (ii) unchanged).

## D'' — Unit-local M1 validity (supersedes D' "projected, not rerun")

- **Projection is allowed ONLY when unit == whole author chapter** (byte-identical inputs mean artifacts valid by construction).
- For units cut inside a chapter: **B0 reruns per unit** (unit-local brief/cast, CONTEXT_ONLY tail per D'), and **B1/B2 rerun for any window whose rendered prompt is not byte-identical** under the unit-local roster/brief. Byte-identity is checked mechanically on rendered prompts (render both, compare); identical means cache replay at $0, different means fresh call.
- **M1 rerun cost goes back into the estimator** as a per-unit line item (mini-model accounted separately). The ~413k "avoided" figure is RETRACTED as a general claim; it holds only for the chapters that remain whole units.
- WH ch1–8 (all fit as whole-chapter units) are unaffected; the wall chapters (ch9/10/17/21) and all Gatsby units pay the rerun — priced BEFORE lock, per the declared-before-lock rule in D'.

## E'' — deterministic_base + contingent reserve (supersedes E' "FULL call graph")

Preflight per unit reports two numbers, both rendered not hand-summed: (1) **deterministic_base** — every call known before run (M2, identity partition shards, phase batches) with per-model prompt+completion upper bounds; (2) **worst_case_contingent_reserve** — max confirmations (= destructive-verdict cap per scope x 2 keys), max split shards (from member counts + `max_partition_atoms_per_call`), max replay calls (from dependency-ledger fanout upper bound). **A unit may start only if base + reserve fits under the 225k UTC safety gate**; reserve unspent rolls forward, never borrowed against.

## R4'' — Lineage-cohort denominator (supersedes R4 wording fix)

Audit denominator = **lineage cohort**: every entity that EVER entered T2 within the scope, including those later merged/tombstoned/reclassified. Headline reports: active T2 entities (three buckets as rev2) AND destructive outcomes (each source entity's terminal state + its two-key/human record). "100% adjudicated" is claimable only when every cohort member has a terminal record. Survivor-only accounting is banned.

Deferred with Sol's concurrence: human-review UI, cadence-K tuning, nested frames beyond flat v1, Translator style fields.

Round-3 ask to Sol: verify-only pass — the six rev3 mechanisms above are the locked answers to your round-2 findings (unit-local M1 validity, bounded two-key exact partition, dependency DAG + immutable replay source, per-event disposition, frame query-slice, lineage denominator + contingent reserve). Flag anything still unimplementable or unbounded; otherwise LOCK so prompt authoring (Claude) and implementation phasing (CodeX) can start.
