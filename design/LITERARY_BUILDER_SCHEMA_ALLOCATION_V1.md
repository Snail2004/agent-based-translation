# LITERARY_BUILDER_SCHEMA_ALLOCATION_V1 — draft for Sol review

Status: **DRAFT (Claude), 2026-07-12.** Not LOCKED. Purpose = the single missing document that the M4f prompt rework depends on. Grounded on real artifacts: the four v1 prompts (design/LITERARY_PROMPT_DESIGN.md :118/180/265/358), the four v2 prompts (:507/523/537/551), the Story Bible field list (design §5.6), and Canonical §2–§8 (TASK_LIT_M4f_CANONICAL_V1.md). No API calls; no code changed.

---

## 0. Why this document exists (and the one rule that generates it)

The M4f prompt rework surfaced two defect classes that are NOT prompt-wording bugs — they are **missing-design** bugs:

- **F5 (silent field loss):** v2 was written as a REWRITE from scratch, so it dropped v1 fields (glossary targets, register, motifs, state-changes, threads, `scenes_party_size`) that downstream stages still consume. No document said "this field still has a consumer," so the loss was invisible until Sol read v1.
- **F7 (half a contract):** the v2 commit authored only the SYSTEM half of each call ("what the model must DO"). The USER-PAYLOAD half ("what the model is ALLOWED to SEE") was never written — so the old code still injects registry/neighbors into B0 (`builder_pilot.py:1174`) while the new B0 prompt says "you only have this scene." Prompt and code contradict each other, live.

**The generating rule (removes both classes by construction):**

> A Builder field is **removed** from v2 only if EITHER (a) Canonical explicitly reassigns its authority to a later stage (identity/overlay/adjudicator), OR (b) it has **no consumer** — and (b) requires *naming the dead consumer in this table*. Everything else is **retained verbatim from v1**. A field's home layer, its consumers, and each layer's input allowlist are all fixed here BEFORE any prompt is (re)written.

Corollary: **v2 = v1 − (identity-decision fields Canonical reassigns) + (Canonical occurrence/witness/frame additions).** It is a surgical delta, never a rewrite. F5 happened precisely because we rewrote instead of diffing.

---

## 1. Target artifact — the Story Bible field inventory (from design §5.6)

The final per-chapter/whole-book artifact carries these groups (§5.6 verbatim list): `scope`, `T1 glossary`, `T2 entities(alias valid_range)`, `T3 speaker_turns`, `T4 chapter_digests`, `entity_relations(phase intervals)`, `entity_state_intervals`, `address_policies(proposal)`, `narration_frame_segments`, `unresolved_threads`. Every Builder field below must trace to one of these, or to the identity/overlay ground layer (Canonical §5), or be retired with a named dead consumer.

---

## 2. Field → producer → consumer matrix (the core)

Legend for **v1→v2**: `KEEP` = carried verbatim; `DROP-IDENTITY` = removed because Canonical reassigns authority to B4/overlay (correct); `ADD` = new Canonical requirement; `RESTORE` = present in v1, wrongly dropped by the first v2 pass (an F5 instance); `CODE` = assembled post-parse by code, model never emits it.

### 2.1 B0 — chapter brief (`literary_chapter_brief`)

**Scope: ONE CHAPTER in (revert the v2 "one scene" error — see §6-D). No registry.**

| Field | v1→v2 | Consumer | Authority note |
|---|---|---|---|
| `cast_claims[].surface` | KEEP | B4 adjudicator (§2c untrusted claims) | verbatim, first appearance |
| `cast_claims[].surface_kind` (proper_name\|descriptor) | KEEP | B4 | |
| `cast_claims[].referent_kind_claim` | ADD (Canonical §4 ontology) | B4 kind-routing | person\|animal\|nonhuman_character\|place\|group_reference\|object\|unknown |
| `cast_claims[].role_hint` | KEEP | B4, human audit | OBSERVED social role only; NEVER a relationship word (v1 leak guard retained) |
| `cast_claims[].source_block_ids` | KEEP | B4, as-of | blocks in THIS chapter where the surface appears |
| `cast_claims[].quote` | ADD (kind-revealing) | B4 | one verbatim span tying the surface to a specific block |
| `cast_claims[].cast_claim_id` | CODE (§2c) | B4 witness addressing | code-minted; model emits no id |
| `cast_claims[].scene_range`, `max_source_block` | CODE (§2c, §3 as-of) | B4 as-of gate | derived from scenes_party_size + source blocks |
| `setting{place,time_frame_hint,scene_shape}` | RESTORE | B2 (scene context), B3 (setting facts) | dropped by first v2 pass; non-identity, keep |
| `scenes_party_size[]{block_range,co_present_count,participants}` | RESTORE — **LOAD-BEARING** | **B2 vocative resolution** | this IS the scene split; B2 uses `co_present_count==2 → other co-present = addressee`. Dropping it breaks B2's honorific disambiguation. Also answers "what is a scene": B0 OUTPUT, no separate segmenter needed. |
| `neutral_premise` (<=40w) | RESTORE | B3 rolling context, human audit | no relationship verdicts (leak guard) |
| ~~`Prefer entity_id from REGISTRY_SO_FAR`~~ | **DROP-IDENTITY** | — | the pivot removal: no registry into B0. This is the *only* deliberate B0 removal. |

B0 removes exactly ONE thing (registry preference); it does NOT lose setting/scenes/premise. The first v2 pass lost all three — F5.

### 2.2 B1 — lexicon (`literary_lexicon`)

**Scope: one WINDOW + read-only context-only tail. No registry, no entity ids.**

| Field | v1→v2 | Consumer | Authority note |
|---|---|---|---|
| `glossary_candidates[].surface` (was source_term) | KEEP | Translator pack, glossary renderer | |
| `glossary_candidates[].proposed_target_vi` | RESTORE | Translator pack | full VN diacritics; F5 instance — translator-facing, wrongly dropped |
| `glossary_candidates[].category` (place\|object\|cultural\|other) | RESTORE | glossary renderer | |
| `glossary_candidates[].do_not_translate` | RESTORE | Translator pack | load-bearing translation control |
| `glossary_candidates[].termhood` | RESTORE | Auditor precision (§5.6) | one-line justification |
| `glossary_candidates[].block_ids` | KEEP | as-of | |
| `character_mentions[].mention_id` (m_<block>_<n>) | KEEP | B2 mention_ref, B4 occurrence set | model-minted positional id (established pattern) |
| `character_mentions[].block_id` | KEEP (singular) | as-of, occurrence | occurrence-level = one block |
| `character_mentions[].surface` | KEEP | B4 | verbatim |
| `character_mentions[].mention_type` (name\|nickname\|descriptor) | KEEP | B4 | |
| `character_mentions[].referent_kind_claim` | ADD (§4) | B4 kind-routing | replaces v1's identity-laden fields |
| `character_mentions[].quote` | ADD (§4) | B4 | full clause, kind-revealing, never truncated |
| ~~`resolution_status`~~ | **DROP-IDENTITY** (§4) | — | identity adjudicated later; correct removal |
| ~~`candidate_entity_ids`~~ | **DROP-IDENTITY** (§3, §4) | — | future hints excluded, not demoted; correct removal |
| `context_only_used` | RESTORE | audit | advisory flag; F5 instance |

### 2.3 B2 — narrative (`literary_narrative`)

**Scope: one WINDOW + context-only tail + B0 CHAPTER_BRIEF + B1 WINDOW_MENTIONS. No entity registry/ids.**

Endpoint object (`speaker`/`addressee`/`actor`/`target`) — the v1→v2 delta applies to ALL four:

| Endpoint field | v1→v2 | Note |
|---|---|---|
| `surface` | KEEP | verbatim, may be a pronoun (endpoints allow pronouns, unlike mentions) |
| `referent_kind_claim` (was reference_kind) | ADD/rename (§4) | person\|group\|narrator\|reader\|... ; only person → entity later |
| `mention_ref` | ADD (§5 witness) | mention_id from WINDOW_MENTIONS or null; the occurrence witness |
| `attribution_method` (explicit_tag\|turn_alternation\|narrator_inference\|vocative) | KEEP | the real trust signal |
| `confidence` (low\|med\|high) | KEEP | |
| ~~`resolution_status`~~, ~~`candidate_entity_ids`~~ | **DROP-IDENTITY** | adjudicated later |
| `endpoint_id` | CODE (§5) | = (turn_or_event_id, endpoint_role); code-minted post-parse |
| `resolution_evidence` | CODE (§5) | = {mention_ref, attribution_method, evidence_quote}; the witness bundle |

Turn / event level:

| Field | v1→v2 | Consumer | Note |
|---|---|---|---|
| `speaker_turns[].turn_id` (t_<block>_<n>) | KEEP | endpoint_id base | model-minted positional; validator enforces uniqueness + narration order |
| `speaker_turns[].addressee` | **ADD nullable** (F8a) | — | monologue/soliloquy/narration-to-reader → addressee=null; never invent a listener |
| `speaker_turns[].utterance_quote` (<=20w) | KEEP | address/register scoring | verbatim evidence |
| `speaker_turns[].address_terms[]` | **CHANGE scalar→list** (F2, §6 E1) | address checker, VN xưng-hô | **turn-embedded list** (NOT a top-level bucket — recall lock, v1 line 335): `[{surface, evidence_quote, position, addressee_ref, address_occurrence_id(CODE)}]`. A turn may hold 2 vocatives; each gets its own disposition + checker. |
| `speaker_turns[].register_cue` | RESTORE | VN register / address policy | F5 instance |
| `speaker_turns[].utterance_gist` | KEEP (optional, validator-only) | — | per literary-schema-freeze; prompt untouched |
| `relation_events[].event_id` (e_<block>_<n>) | KEEP | endpoint_id base | model-minted positional |
| `relation_events[].event_type` | **RESTORE discipline** (F8b) | phase_leak gate | MUST be lower_snake_case observed-action verb; FORBIDDEN: relationship/phase labels (ally, enemy, betrayal, *_phase). Validator counts `#phase_leak`. |
| `relation_events[].evidence_quote` (<=12w) | KEEP | | actor/target = person or narrator only, never object/animal |

### 2.4 B3 — digest (`literary_digest`)

**Scope: full chapter + B1 roster (surfaces) + B2 events (compact) + prev rolling_summary.**

| Field | v1→v2 | Consumer | Note |
|---|---|---|---|
| `chapter_rolling_summary` | KEEP | next chapter B0/context | spoiler-free w.r.t. unseen chapters |
| `narration_frame_segments[].segment_id` | CODE (§7 versioned) | frame checker addressing | |
| `...narrator_surface` (was narrator_ref) | KEEP, de-identify | frame view | surface not entity id (identity later) |
| `...block_range` | KEEP | | |
| `...story_time_label` | KEEP | frame view | (see §6-A: F4c proposes splitting off frame_kind) |
| `...status` (proposed\|uncertain) | RESTORE (§7) | frame checker | **Canonical §7 requires it**; first v2 pass dropped it (non-conformance, not just improvement) |
| `...evidence_quote` | ADD | frame checker | boundary cue |
| `...parent_segment_ref` | ADD (F4b) | nesting | explicit nesting > implicit range-containment |
| ~~"a chapter is usually NOT one segment"~~ | **REMOVE prior** (F4) | — | over-segmentation bias AND a book-neutrality leak (assumes WH multi-frame structure). Replace: segment only at a genuine cued shift; ambiguous cue → status=uncertain, don't force. |
| `scene_summaries[]` | RESTORE | Brief/context | F5 instance |
| `character_state_changes[]` | RESTORE | **entity_state_intervals (L4)** | design line 480 — L4 needs social_status source; F5 instance |
| `relation_event_summary[].pair_surfaces` | KEEP, de-identify | phase input | surfaces not ids |
| `relation_event_summary[].source_event_ids` (was event_ids) | RESTORE (F3) | per-event disposition, set-equality | **v1 HAD `event_ids`**; first v2 pass dropped it → F3. Lineage B2→phase restored. |
| `relation_event_summary[].observed_valence_hint`, `candidate_transition`, `status=evidence_only` | KEEP | phase (evidence, not label) | never a phase label on its own |
| `unresolved_threads[]` | RESTORE | transition pointers | design line 88; F5 instance |
| `motifs[]` | RESTORE | motif renderer/pack | F5 instance |
| `translator_relevant_facts[].{fact_type,fact,block_evidence}` | KEEP | Translator pack, relation_facts | MAX 8/chapter |
| `translator_relevant_facts[].inference_basis` (stated\|derived) | ADD (my v2, good) | overlay/disclosure | derived facts default-undisclosed (§6) |
| `translator_relevant_facts[].source_event_ids` | ADD (F3) | lineage | when a fact derives from a B2 event |

### 2.5 B4 — identity + phase (NOT batch 1; shown for the "deliberately empty" contract)

B4 CONSUMES: B0 cast_claims (untrusted), B1 mentions (occurrence set), B2 endpoints (witness material), B3 frames. B4 PRODUCES (and B0–B3 must NEVER fill): entity ids, alias valid_range, phase intervals, address_policies, entity_state_intervals, overlay/disclosure records. **The absence of identity fields in B0–B3 is the design, not an omission** — it is what the pivot bought.

---

## 3. Fill order & hand-off payloads (dependency chain)

```
B0 (per chapter)  ──cast_claims + scenes_party_size + setting──┐
                                                               ▼
B1 (per window)   ──WINDOW_MENTIONS (mention_id,surface,block)─┤
                                                               ▼
B2 (per window)   needs B0 brief (scenes_party_size for vocative) + B1 WINDOW_MENTIONS
                  ──speaker_turns + relation_events (with mention_ref witnesses)──┐
                                                                                  ▼
B3 (per chapter)  needs B1 roster (surfaces) + B2 events (compact) + prev summary
                  ──frames + facts + relation_event_summary(source_event_ids)──┐
                                                                               ▼
B4 (identity/phase) needs B0 claims + B1 mentions + B2 endpoints + B3 frames
                  ──entities, phases, address_policies, overlay/disclosure──▶ checkers
```

Two hand-offs are load-bearing and were broken by the first v2 pass:
- **B0.scenes_party_size → B2 vocative resolution** (restored in §2.1/§2.3).
- **B2.event_id → B3.relation_event_summary.source_event_ids → phase per-event disposition** (restored in §2.4).

---

## 4. Per-layer INPUT contract — the missing half (closes F7)

Each layer's user payload is an **allowlist**. A conformance test (Phase 1 gate) asserts the FORBIDDEN sections never appear in the rendered request. This is where "contamination removed by construction" is actually earned — not by the system prompt alone.

| Layer | ALLOWED in payload | FORBIDDEN (assert-absent) | as-of |
|---|---|---|---|
| **B0** | this chapter's block text only | REGISTRY_SO_FAR, neighbor summaries, any prior-chapter identity/entity ids | n/a (chapter-local) |
| **B1** | active window + read-only CONTEXT_ONLY tail | registry, entity ids, B0 hints-as-authority | never cite context-only block_id |
| **B2** | active window + context-only tail + B0 CHAPTER_BRIEF (setting + scenes_party_size) + B1 WINDOW_MENTIONS (mention_id, surface, block) | entity registry, candidate_entity_ids, future windows | window-local |
| **B3** | full chapter + B1 roster (surfaces only) + B2 events (compact) + prev rolling_summary | future chapters, entity ids as authority, whole-book vector | chapter-local |

**Immediate contradiction to fix (F7 sharpened):** `builder_pilot.py:1174` currently injects registry+neighbor into B0 while the B0 prompt says "scene text only." The B0 input contract above (chapter text only, registry FORBIDDEN) is the authority; the code must be changed to match, and the conformance test must assert `REGISTRY_SO_FAR` is absent from the B0 rendered request.

---

## 5. Token budget per layer (rough, to be MEASURED at dry-run per §10)

| Layer | Cadence | Dominant payload | Budget lever |
|---|---|---|---|
| B0 | 1 / chapter | chapter text (~8–9.4k tok WH) | one call; cheap |
| B1 | N windows / chapter | window 500 blocks + tail | **window LOCKED 500/8** (1500 fails recall floor — measured) |
| B2 | N windows / chapter | window + B0 brief + WINDOW_MENTIONS | mentions add ~small; brief is compact |
| B3 | 1 / chapter | full chapter + compact roster/events | roster = surfaces only (no full registry dump) |

Numbers are placeholders; §10 preflight MEASURES per-model before scale (do not estimate — [[audit-real-rendered-prompts-not-design-theory]]).

---

## 6. Open decisions for Sol (must chốt before prompt rewrite)

- **A — F4c (split `frame_kind` from `story_time_label`):** Canonical §7 currently has ONLY `story_time_label`. Sol's split (a letter is both `letter` kind AND `retrospective_past` time) is a genuine improvement but is a **Canonical §7 schema CHANGE**, not conformance. Claude recommends: accept the split (VN tense/aspect needs the time axis; register/framing needs the kind axis) and amend §7. Confirm.
- **B — F1 id minting:** confirm positional occurrence ids (`m_`/`t_`/`e_`) stay **model-minted** (established since B1) with a validator uniqueness+narration-order check; **code mints only** identity-layer ids (entity/binding/dispute) + `endpoint_id`/`resolution_evidence`/`address_occurrence_id`/`cast_claim_id`/`segment_id`. The "model mints ids contradicts code-mints-ids" framing is withdrawn on this basis. Confirm.
- **C — F2 turn-embedded:** confirm `address_terms[]` stays a field ON the turn, never a third top-level bucket (recall lock, v1 line 335). Confirm.
- **D — B0 scope:** confirm B0 reverts to **chapter-scoped input** (the v2 "one scene in" was an over-correction that created an undefined scene unit and dropped scenes_party_size). `scenes_party_size` = the scene split, produced as OUTPUT. The pivot only requires removing the REGISTRY injection, not changing B0's scope.
- **E — retirement:** proposed retired fields = **NONE**. Every v1 field above has a named consumer. If Sol wants any retired, name its dead consumer here.

---

## 7. What this unblocks

Once §6 is chốt, the prompt rework is mechanical "fill to the contract": rewrite the four v2 blockquotes as v1 − DROP-IDENTITY + ADD/RESTORE per §2, add the §4 input contracts, then verify via the real loader + book-neutrality grep (as batch 1) + a new conformance test asserting FORBIDDEN payload sections absent. F5 and F7 become structurally impossible, not reviewer-caught.
