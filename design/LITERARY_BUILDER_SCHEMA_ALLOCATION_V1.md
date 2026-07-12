# LITERARY_BUILDER_SCHEMA_ALLOCATION_V1 — draft for Sol review

Status: **DRAFT rev2 (Claude), 2026-07-12.** Not LOCKED, NOT yet prompt-write-ready. rev2 folds the CodeX independent review (8 findings, all verified by Claude on artifacts). **Sol's independent pass is still OUTSTANDING (quota) — rev2 needs the Sol reconciliation before LOCK, per the §E9 dual-audit rule; do NOT rewrite prompts off rev2 alone.** Grounded on: v1 prompts (design/LITERARY_PROMPT_DESIGN.md :118/180/265/358), v2 prompts (:507/523/537/551), Story Bible field list (design §5.6), Canonical §2/§4/§5/§7, and the CURRENT runtime (builder_pilot.py, story_bible_v2.py — line-verified). No API calls; no code changed.

---

## 0. Why this document exists (and the rule that generates it)

The prompt rework surfaced defect classes that are **missing-design**, not wording:

- **F5 (silent v1 field loss):** v2 was rewritten from scratch, dropping v1 fields still consumed downstream.
- **F7 (half a contract):** only the SYSTEM half of each call was authored; the USER-PAYLOAD allowlist was never written, so old code injects registry/neighbors into B0 (`builder_pilot.py:1174`) while the new B0 prompt says "scene only."

**rev2 lesson (from CodeX F6):** a field is only "retained" if a REAL RUNTIME CONSUMER reads it. rev1 verified the PRODUCER side (v1 prompt has the field) but asserted CONSUMERS without checking the runtime — three fields (`utterance_gist`, `scene_summaries`, `termhood`) turned out to have **no literary consumer**. Consumer claims in §2 are now runtime-verified (grep of `pipeline/`, excluding the producer file and tests). This is the [[green-tests-can-hide-dead-integration]] / [[verify-on-committed-artifacts-not-reports]] discipline applied to schema.

**The generating rule:**
> A v1 field is **removed** only if EITHER (a) Canonical reassigns its authority to a later stage, OR (b) it has **no real runtime consumer** (verified, not assumed). A field is **RENAME/MIGRATE**, not KEEP, if the runtime currently reads the old name. Everything else is retained verbatim. Home layer, consumers, and each layer's input allowlist are fixed here BEFORE any prompt is rewritten.

Corollary: **v2 = v1 − (identity-decision fields Canonical reassigns) − (consumer-less fields, named) + (Canonical occurrence/witness/frame additions), keeping v1 field NAMES the runtime reads.** Surgical delta, never a rewrite.

---

## 1. Target artifact — Story Bible field inventory (design §5.6)

`scope`, `T1 glossary`, `T2 entities(alias valid_range)`, `T3 speaker_turns`, `T4 chapter_digests`, `entity_relations(phase intervals)`, `entity_state_intervals`, `address_policies(proposal)`, `narration_frame_segments`, `unresolved_threads`. Every Builder field traces to one of these, to the identity/overlay ground layer (Canonical §5), or is retired with a named dead consumer.

---

## 2. Field → producer → consumer matrix

Legend: `KEEP` = carried verbatim (name unchanged); `DROP-IDENTITY` = Canonical reassigns to B4/overlay; `ADD` = new Canonical requirement; `RESTORE` = in v1, wrongly dropped by first v2 pass; `RETIRE` = no real runtime consumer (verified); `CODE` = code-assembled/minted, model never emits; `MIGRATE` = rename touching runtime read-sites (needs a consumer-code change, not a silent KEEP).

### 2.1 B0 — chapter brief (`literary_chapter_brief`)

**Scope: chapter text IN; claims OUT at `surface × scene` granularity (reconciles Canonical §2c/§12 scene-local with a single chapter call — see §6-D). NO registry. No prior-chapter summary.**

| Field | v1→v2 | Consumer (verified) | Note |
|---|---|---|---|
| `cast_claims[].surface` | KEEP | B4 adjudicator (§2c) | verbatim |
| `cast_claims[].surface_kind` | KEEP | B4 | proper_name\|descriptor |
| `cast_claims[].referent_kind_claim` | ADD (§4 ontology) | B4 kind-routing | person\|animal\|nonhuman_character\|place\|group_reference\|object\|unknown |
| `cast_claims[].role_hint` | KEEP | B4, audit | OBSERVED role only, never a relationship word |
| `cast_claims[].scene_range` | ADD (§2c) | B4 as-of, B2 projection | **each claim is scoped to ONE scene**; a surface recurring in 2 scenes = 2 claims, NEVER merged across scenes (prevents cross-scene hint contamination — CodeX F1) |
| `cast_claims[].source_block_ids` | KEEP | B4, as-of | blocks **within this claim's scene** only |
| `cast_claims[].quote` | ADD | B4 | one verbatim span in this scene |
| `cast_claims[].cast_claim_id`, `max_source_block` | CODE (§2c/§3) | B4 witness/as-of | code-minted post-parse |
| `setting{place,time_frame_hint,scene_shape}` | RESTORE | B2, B3 (both this-chapter) | non-identity; first v2 pass dropped it |
| `scenes_party_size[]{block_range,co_present_count,participants}` | RESTORE — **LOAD-BEARING** | **B2 vocative resolution**, scene projection | defines scene boundaries (co_present_count==2 → other = addressee). This IS the scene split; no separate segmenter |
| `neutral_premise` (≤40w) | RESTORE | **B3** (requires B0 brief in B3 allowlist — see §4) | leak-guarded |
| ~~registry entity_id preference~~ | **DROP-IDENTITY** | — | the pivot removal (the only deliberate B0 removal) |

### 2.2 B1 — lexicon (`literary_lexicon`)

**Scope: one WINDOW + read-only context-only tail. No registry, no entity ids.**

| Field | v1→v2 | Consumer (verified) | Note |
|---|---|---|---|
| `glossary_candidates[].source_term` | KEEP (NOT renamed) | Translator pack; runtime reads `source_term` @ story_bible_v2.py:2224 | rev1 wrongly renamed→surface = a migration; keep the name |
| `glossary_candidates[].proposed_target_vi` | RESTORE | Translator pack (56 read-sites) | full VN diacritics; propagate through `_glossary_as_of` |
| `glossary_candidates[].category` | RESTORE | glossary renderer | place\|object\|cultural\|other |
| `glossary_candidates[].do_not_translate` | RESTORE + **FIX BUG** | Translator (85 read-sites) | real consumer, but currently DROPPED by `_glossary_as_of` @ :2228 — must be propagated |
| `glossary_candidates[].termhood` | **RETIRE (from literary)** | none — all read-sites are D2L `prepass/*` | D2L-only field; literary Story Bible never keeps it. Lives in D2L separately |
| `glossary_candidates[].block_ids` | KEEP | as-of | |
| `character_mentions[].mention_id` | **CODE-MINT** (was model) | B2 mention_ref, B4 | code mints `m_<block>_<n>` from (block, within-block position); model emits at most an untrusted local key for intra-response cross-ref (CodeX B) |
| `character_mentions[].block_id` | KEEP | as-of, occurrence | one occurrence = one block |
| `character_mentions[].surface` | KEEP | B4 | verbatim |
| `character_mentions[].mention_type` | KEEP | B4 | name\|nickname\|descriptor |
| `character_mentions[].referent_kind_claim` | ADD (§4) | B4 kind-routing | ontology enum |
| `character_mentions[].quote` | ADD (§4) | B4 | full clause, kind-revealing |
| ~~`resolution_status`, `candidate_entity_ids`~~ | **DROP-IDENTITY** (§3/§4) | — | future hints excluded; **validator @ :3818 must drop these from `_require_item`** (currently still required) |
| `context_only_used` | RESTORE | audit | advisory flag |

### 2.3 B2 — narrative (`literary_narrative`)

**Scope: window + context-only tail + B0 CHAPTER_BRIEF (scene-intersecting projection) + B1 WINDOW_MENTIONS. No registry/ids.**

Endpoint object (`speaker`/`addressee`/`actor`/`target`) — **TWO kind axes, kept separate (CodeX F3):**

| Endpoint field | v1→v2 | Note |
|---|---|---|
| `surface` | KEEP | verbatim; may be a pronoun (endpoints allow pronouns) |
| `reference_kind` | **KEEP (do NOT collapse)** | discourse role: person\|group\|narrator\|reader\|unknown; only person → entity later. Canonical §5 ground endpoint keeps this field |
| `referent_kind_claim` | ADD (§4) | ontology: person\|animal\|…; the animal-detection axis (Juno defect). Distinct from reference_kind |
| `mention_ref` | ADD (§5 witness) | mention_id from WINDOW_MENTIONS or null |
| `endpoint_block_id`, `endpoint_position`, `resolution_span` | ADD (CodeX F3) | the endpoint's OWN evidence (the "said X" tag may sit outside the utterance; a pronoun needs its own span) — not the shared turn quote |
| `attribution_method` | KEEP | explicit_tag\|turn_alternation\|narrator_inference\|vocative |
| `confidence` | KEEP | low\|med\|high |
| ~~`resolution_status`, `candidate_entity_ids`~~ | **DROP-IDENTITY** | adjudicated later |
| `endpoint_id`, `resolution_evidence` | CODE (§5) | endpoint_id=(turn/event_id, role); resolution_evidence bundles mention_ref+attribution+resolution_span |

Turn / event level:

| Field | v1→v2 | Consumer | Note |
|---|---|---|---|
| `speaker_turns[].turn_id`, `relation_events[].event_id` | **CODE-MINT** (CodeX B/F5) | endpoint_id base | code mints from `position_key=(block_order, within_block_order)`; **validator @ :3922 currently checks presence only — a format/unique/order check must be ADDED**. position_key also lets two phase-changes in one block be ordered |
| `speaker_turns[].addressee` | **ADD nullable** (F8a) | — | monologue/narration-to-reader → null; never invent a listener |
| `speaker_turns[].utterance_quote` | KEEP | address/register scoring | verbatim ≤20w |
| `speaker_turns[].address_terms[]` | **scalar→list, turn-embedded** (F2/§6 E1) | address checker, VN xưng-hô | `[{surface, evidence_quote, position, addressee_ref, address_occurrence_id(CODE)}]`; each vocative its own disposition+checker. NOT a top-level bucket (recall lock, v1 line 335) |
| `speaker_turns[].register_cue` | RESTORE | VN register / address policy | first v2 pass dropped it |
| ~~`utterance_gist`~~ | **RETIRE** | none (0 real read-sites; 9 grep hits all validator/schema) | no downstream consumer — CodeX E |
| `relation_events[].event_type` | **RESTORE discipline** (F8b) | phase_leak gate | lower_snake_case observed action; FORBIDDEN relationship/phase labels; validator counts `#phase_leak` |
| `relation_events[].actor/target` | **ALLOW non-person + route-out** (CodeX F8) | narrative-action stream | do NOT ban animal/nonhuman actor at extraction (silent evidence drop of the Juno-acts scene). Capture with referent_kind_claim; **code routes non-person events OUT of the human relation-phase layer** (Canonical §4 "route out" = capture-then-exclude, not drop) |
| `relation_events[].evidence_quote` | KEEP | | ≤12w verbatim |

### 2.4 B3 — digest (`literary_digest`)

**Scope: full chapter + B0 brief + B1 roster (surfaces) + B2 events (compact) + prev rolling_summary.**

| Field | v1→v2 | Consumer (verified) | Note |
|---|---|---|---|
| `chapter_rolling_summary` | KEEP | **next chapter's B3** (NOT B0 — B0 forbids prior summary; rev1 mislabeled) | spoiler-free w.r.t. unseen chapters |
| `narration_frame_segments[].local_segment_key` | ADD (model) | code remap → segment_id | **model-local key** (seg_1…) so it can reference a parent BEFORE code mints the canonical id (CodeX F4 chicken-egg) |
| `...parent_local_key` | ADD (F4b) | nesting tree | references a sibling local key; code validates a well-formed nesting TREE then remaps. **Current validator @ :4050 enforces flat contiguous cover — must be replaced with a nesting-tree validator** |
| `...segment_id`, `version` | CODE (§7) | frame checker | minted after nesting-tree validation |
| `...narrator_surface` | KEEP, de-identify | frame view | surface not entity id |
| `...block_range` | KEEP | | |
| `...story_time_label` | KEEP | frame view | (§6-A proposes splitting `frame_kind` off — a Canonical §7 change, not done unilaterally) |
| `...status` (proposed\|uncertain) | RESTORE (Canonical §7) | frame checker | §7 REQUIRES it; first v2 pass dropped it (non-conformance) |
| `...evidence_quote` | ADD | frame checker | boundary cue |
| ~~"chapter usually NOT one segment"~~ | **REMOVE prior** (F4) | — | over-segmentation bias + book-neutrality leak (assumes WH structure). Replace: segment only at a cued shift; ambiguous cue → status=uncertain |
| ~~`scene_summaries`~~ | **RETIRE** (unless named) | none (only a test stub @ test_literary_checkpoint.py:136) | rev1 claimed "Brief/context" — unverified/false. Retire unless a real literary consumer is named — CodeX E |
| `character_state_changes[]` | RESTORE | **entity_state_intervals (L4)** | design line 480; first v2 pass dropped it |
| `relation_event_summary[].pair` | KEEP, de-identify | phase input | surfaces not ids |
| `relation_event_summary[].event_ids` | KEEP (NOT renamed) | phase per-event disposition; runtime reads `event_ids` @ story_bible_v2.py:571 | rev1 wrongly renamed→source_event_ids (0 read-sites) = a migration. Keep the name — this IS the F3 lineage, already present in v1 |
| `...observed_valence_hint`, `candidate_transition`, `status=evidence_only` | KEEP | phase (evidence, not label) | |
| `unresolved_threads[]` | RESTORE | transition pointers | design line 88 |
| `motifs[]` | RESTORE | motif renderer/pack | first v2 pass dropped it |
| `translator_relevant_facts[].{fact_type,fact,block_evidence}` | KEEP | Translator pack, relation_facts | MAX 8/chapter |
| `translator_relevant_facts[].inference_basis` | ADD | overlay/disclosure | stated\|derived |
| `translator_relevant_facts[].event_ids` | ADD | lineage | when a fact derives from a B2 event (same name as relation_event_summary) |

### 2.5 B4 — identity + phase (NOT batch 1; consume/produce contract)

**B4 CONSUMES (rev1 list was incomplete — CodeX F2):** B0 cast_claims (untrusted, scene-scoped) · B1 mentions · B2 endpoints **+ relation_events + register_cue + address occurrences** · B3 **relation_event_summary + character_state_changes + translator_relevant_facts +** frames.
**B4 PRODUCES (and B0–B3 must NEVER fill):** entity ids, alias valid_range, **phase intervals** (needs B2 events + B3 summaries), **address_policies** (needs B2 address/register), **entity_state_intervals** (needs B3 state changes), overlay/disclosure records. The absence of identity fields in B0–B3 is the design.

---

## 3. Fill order & hand-off payloads

```
B0 (per chapter)  ─ cast_claims(surface×scene) + scenes_party_size + setting + neutral_premise ─┐
                                                                                                ▼
B1 (per window)   ─ WINDOW_MENTIONS (code-minted m_ ids, surface, block) ───────────────────────┤
                                                                                                ▼
B2 (per window)   needs B0 brief (scene-intersecting projection: scenes_party_size + cast) + B1 WINDOW_MENTIONS
                  ─ speaker_turns + relation_events (with mention_ref + own endpoint evidence) ─┐
                                                                                               ▼
B3 (per chapter)  needs B0 brief (neutral_premise/setting) + B1 roster + B2 events + prev rolling_summary
                  ─ frames(nesting tree) + facts + relation_event_summary(event_ids) + state_changes ─┐
                                                                                                      ▼
B4 (identity/phase) needs B0 + B1 + B2(events/register/address) + B3(summaries/state/facts/frames)
                  ─ entities, phases, address_policies, entity_state_intervals, overlay/disclosure ─▶ checkers
```

Load-bearing hand-offs the first v2 pass broke (now restored): **B0.scenes_party_size → B2 vocative**; **B2.event_id → B3.relation_event_summary.event_ids → phase per-event disposition**; **B0.neutral_premise/setting → B3** (requires B0 brief in B3 allowlist); **B3.rolling_summary → next B3** (not B0).

---

## 4. Per-layer INPUT contract — the missing half (closes F7)

Each layer's user payload is an **allowlist**. The conformance test is stronger than a marker grep (CodeX F7): a **typed payload allowlist** + a unique **sentinel injected into every forbidden source** asserted absent from the FULL rendered request + a **lineage-fingerprint** check that no forbidden-source order_index entered the request.

| Layer | ALLOWED | FORBIDDEN (sentinel-asserted absent) | tail / as-of |
|---|---|---|---|
| **B0** | this chapter's block text only | REGISTRY_SO_FAR, neighbor summaries, prior-chapter identity/summary | n/a |
| **B1** | active window + read-only CONTEXT_ONLY tail | registry, entity ids, B0 hints-as-authority | **tail by mode: online_as_of = previous-blocks tail ONLY; whole_book_frozen = ±K both-sided but audited** (Canonical §2/§3). Never cite a context-only block_id |
| **B2** | active window + tail + B0 brief (scene-intersecting projection) + B1 WINDOW_MENTIONS | entity registry, candidate ids, future windows, non-intersecting scenes' claims | same mode rule |
| **B3** | full chapter + **B0 brief** + B1 roster (surfaces) + B2 events (compact) + prev rolling_summary | future chapters, entity ids as authority, whole-book vector | chapter-local |

**Live contradiction to fix (F7):** `builder_pilot.py:1174` injects registry+neighbor into B0 while the B0 prompt says "scene text only." The B0 contract above is authority; the code must change and the sentinel test must assert `REGISTRY_SO_FAR` is absent from the B0 rendered request.

---

## 5. Token budget per layer (MEASURED at dry-run per §10, not estimated)

| Layer | Cadence | Dominant payload | Lever |
|---|---|---|---|
| B0 | 1 / chapter | chapter text (~8–9.4k tok WH) | one call; cheap |
| B1 | N windows / ch | window + tail | **window LOCKED 500/8** (1500 fails recall — measured) |
| B2 | N windows / ch | window + B0 scene projection + WINDOW_MENTIONS | projection = only intersecting scenes (not whole chapter) keeps it small |
| B3 | 1 / chapter | full chapter + B0 brief + compact roster/events | roster = surfaces only |

---

## 6. A–E — reconciled with CodeX (Sol pass still pending)

- **A — split `frame_kind` from `story_time_label`:** AGREE, but it is a **Canonical §7 change** (§7 has only story_time_label) AND the **nesting representation (F4) must be defined first**. Amend §7 + adopt the local-key→remap nesting scheme, then split.
- **B — occurrence id minting:** **Claude concedes to CodeX** — CODE canonicalizes/mints occurrence ids from position; if the model emits a local key it is an untrusted intra-response cross-ref hint only, and a format/unique/order validator (currently absent, :3818/:3922) must be added. Withdraws rev1's "positional ids stay model-minted."
- **C — address_terms:** AGREE — per-turn list, each occurrence its own id + disposition, turn-embedded (never a third bucket).
- **D — B0 scope:** AGREE with CodeX's condition — NOT simple chapter-scope. B0 reads the chapter but emits **surface × scene** claims (never merged across scenes) and B2 receives only the **scene-intersecting projection**. This reconciles the single efficient B0 call with Canonical's scene-local intent.
- **E — retirement:** **Claude concedes to CodeX (verified):** RETIRE `utterance_gist` (0 consumer), `scene_summaries` (test stub only), `termhood` (D2L-only, no literary consumer). KEEP + propagate `proposed_target_vi`, `category`, `do_not_translate` (real consumers; fix the `_glossary_as_of` drop). "Retire none" (rev1) is withdrawn.

No new `other_new_class` vs §E9 (CodeX concurs) — the fixes are all already-locked; rev1 merely expressed them inconsistently.

---

## 7. What this unblocks

Once **Sol's independent pass reconciles with this rev2** (dual-audit, §E9), the prompt rework is mechanical fill-to-contract: rewrite the four blockquotes as v1 − DROP-IDENTITY − RETIRE + ADD/RESTORE per §2 (keeping runtime field names), add the §4 input contracts + validator changes (drop old required fields; add id format/unique/order; nesting-tree frame validator; propagate do_not_translate), then verify via the real loader + book-neutrality grep + the sentinel conformance test. F5/F7 become structurally impossible. **Do not start the rewrite until Sol's pass is folded.**
