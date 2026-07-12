# LITERARY_BUILDER_SCHEMA_ALLOCATION_V1 — draft for Sol review

Status: **DRAFT rev4 (Claude), 2026-07-12.** Not LOCKED — rev4 adds §8 locking Sol round-3's five implement-blockers; Phase 1 steps 1–2 are GO after a mechanical Sol confirm. rev3 folds **Sol Max round-2** (8 findings — 3 BLOCKER + 4 MAJOR + 1 MINOR — all verified by Claude on artifacts/source). **Not yet prompt-write-ready; needs Sol round-3 to confirm rev3.**

**REFRAME (round-2 consequence):** round-2 proved the leaks live in the **orchestration + as-of topology**, not just field naming — so this is no longer a "prompt rework", it is a **Builder-core rev**. Per Sol's locked sequence, **prompts are the LAST step (step 6)**, after schema/validators/code-mint, after dismantling the old identity-ledger, after the B1→B2/M2/B3 handoff and B4 ingest are rebuilt. Do NOT rewrite prompts off this doc.

Grounded on: v1/v2 prompts (design/LITERARY_PROMPT_DESIGN.md), Story Bible list (design §5.6), Canonical §2/§3/§4/§5/§7, L2A2 spec, and the CURRENT runtime (builder_pilot.py, story_bible_v2.py — line-verified). No API; no code changed.

---

## 0. Why this exists + what round-2 changed

Original defect classes: **F5** (silent v1 field loss) and **F7** (only the SYSTEM half of each call authored; the user-payload allowlist missing). rev2 fixed the field matrix. **Round-2 showed that is not enough:** the identity-hint contamination and the as-of leak are re-created by the ORCHESTRATION and by whole-chapter call topology, which a field matrix cannot fix. rev3 therefore also specifies (a) mode-dependent call topology, (b) occurrence-grounded downstream observations, (c) the dismantling of the entity-ledger machinery, (d) deterministic code-mint coordinates, (e) typed cross-layer projections. The field matrix (§2) stays but is now one part of a Builder-core contract.

**Two verification lessons now baked into the method:**
- (rev2/F6) a field is "kept" only if a REAL RUNTIME CONSUMER reads it — verify the consumer side, not just the producer.
- (rev3/round-2) a "de-identified" field can still ENCODE an identity decision (grouping events under a pair IS identity work); and removing one injection site (B0 registry) does not remove the hint channel if other orchestration paths re-seed it. Verify the *whole data path*, not the field label.

---

## 1. B0 call topology & as-of — mode-dependent (NEW, closes round-2 F1)

rev2's "B0 reads the chapter, emits surface×scene claims" is safe ONLY in `whole_book_frozen`. It FAILS `as_of_experiment` because (Canonical §3) `input_max_order` = max order over the WHOLE rendered request — so a whole-chapter B0 call stamps EVERY claim with the chapter-end cutoff, regardless of which blocks its evidence sits in. A claim about b002 then illegally carries chapter-end knowledge at an as-of query.

- **Separate two orders:** `input_max_order` (from request topology, code-computed, gates as-of usage) vs `evidence_max_order` (from the claim's own evidence blocks). rev2 conflated them into `max_source_block`.
- **B0 topology by mode:** `whole_book_frozen` = one whole-chapter call is permitted (final decision, not as-of). `as_of_experiment` = B0 must be scene-local (or its claims are barred from as-of usage), so `input_max_order ≤ order(mention)` can hold.
- **scenes_party_size is NOT a scene authority.** Verified on real data: Gatsby ch1 has two scenes both containing b152 (overlap), and counts "Miss Baker" and "Jordan Baker" as two people in one scene (`data/reports/literary_l2a2_gatsby_ch1_hardened_v3/brief/gg_ch01.json`). The current validator (builder_pilot.py:~3727) checks only that range endpoints exist — no overlap/gap/participant-consistency check. So: scene ranges must be **exact-cover, non-overlapping** (validated), and **`co_present_count` is an UNTRUSTED claim, never a hard rule** for B2 identity resolution. B2 may use it as one weak signal routed through adjudication, never as "co_present==2 ⇒ the other person."

---

## 2. Field → producer → consumer matrix

Legend: `KEEP`/`DROP-IDENTITY`/`ADD`/`RESTORE`/`RETIRE`/`CODE`/`MIGRATE` as rev2. Consumer column now carries a **provenance tag** (round-2 MINOR): `[rt]` current runtime read-site · `[cp]` Canonical-planned (not yet in runtime) · `[audit]` audit/validator only.

### 2.1 B0 — chapter brief

Fields as rev2 §2.1 (cast_claims surface×scene, setting, scenes_party_size, neutral_premise; registry DROP-IDENTITY), with round-2 changes:
- `scenes_party_size[].block_range` — **exact-cover non-overlapping** (validator ADD); `co_present_count` tagged **untrusted claim** `[cp]`, not a B2 hard rule.
- `cast_claims` carry `evidence_max_order` (code) distinct from the call's `input_max_order`; scene ranges from the exact-cover partition.

### 2.2 B1 — lexicon

Fields as rev2 §2.2 (keep `source_term`; RESTORE proposed_target_vi `[rt]`/category `[rt]`/do_not_translate `[rt]` + fix `_glossary_as_of` drop; RETIRE termhood; DROP-IDENTITY resolution_status/candidate_entity_ids), plus round-2 F6:
- **Deterministic code-mint needs coordinates.** `character_mentions[]` MUST carry a `source_span` (char offset within block) or an `occurrence_ordinal` (Nth occurrence of this surface in this block) so code can mint `m_<block>_<ordinal>` deterministically — a quote can appear twice in a block. If the model's span/quote+surface does not locate uniquely → **fail-closed** (drop with a counter, never guess an ordinal). Model emits no canonical id.

### 2.3 B2 — narrative

Endpoint object — round-2 F4 splits the two axes cleanly:
- **`reference_scope` = individual | group | narrator | reader | unknown** (REPLACES rev2's `reference_kind=person|group|narrator|reader`). The discourse/scope axis: is this ONE addressable individual, a group, the narrator's voice, the reader, or unknown. A dog is `reference_scope=individual` + `referent_kind_claim=animal`.
- **`referent_kind_claim` = person | animal | nonhuman_character | place | group_reference | object | unknown** (ontology). **Runtime entity-eligibility reads ontology/checker (is it a person?), NEVER the scope axis.** A locked valid-combination table governs (e.g. narrator/reader scope ⇒ not an entity).
- `mention_ref`, `attribution_method` KEEP; `confidence` **RETIRED** (LOCK-3). Endpoint carries its OWN `endpoint_block_id` + `source_position` + `resolution_span` (F3/round-1) — a pronoun or an off-utterance "said X" tag needs its own evidence, not the shared turn quote.
- `endpoint_id`, `resolution_evidence` = CODE.

Turn/event:
- `turn_id`/`event_id` = **CODE-MINT from `position_key=(block_order, within_block_order)`** — the model schema must expose within-block source position so this is deterministic. Validator (currently :3922 presence-only) gains format/unique/order + position_key checks.
- `addressee` nullable (F8a); `utterance_quote` KEEP; `address_terms[]` turn-embedded list w/ code id + disposition (C); `register_cue` RESTORE `[rt]`; `utterance_gist` **RETIRE** (0 consumer).
- `event_type` RESTORE lower_snake_case discipline + `#phase_leak` gate (F8b).
- `actor/target` **ALLOW non-person + route-out** (F8) — capture the animal-acts scene with referent_kind_claim, code routes it out of the human relation-phase layer; never silent-drop.

### 2.4 B3 — digest — **OCCURRENCE-GROUNDED, no identity (round-2 F2, the deepest change)**

rev2's "de-identify pair to surfaces" was insufficient: grouping many events under ONE pair still decides "he"="the landlord"="Heathcliff" — identity work before B4, and runtime consumes that pair directly for phase input (story_bible_v2.py:571). rev3:

- **B3 emits OBSERVATIONS keyed by `event_id` / `endpoint_id` / mention-occurrence, and does NOT canonicalize pairs.** `relation_event_summary` becomes a list of per-event observations referencing B2 `event_id`s; **code groups events into final pairs ONLY AFTER B4 produces active endpoint bindings.**
- `character_state_changes`, `unresolved_threads`, and character-facts must point to an **occurrence/endpoint reference, never a clean identity surface** (v1 used `entity_ref` — barred).
- **Nested frame tree — full invariants (round-2 F5):** model emits `local_segment_key` + `parent_local_key`; code validates then remaps to `segment_id`. Locked invariants: synthetic root OR forest; every child block-range **within** its parent; **siblings non-overlapping**; leaves **exact-cover** the narrative blocks; ancestor overlap only via containment; **deepest active leaf decides the rendered frame**; every local key unique, every parent exists, **graph acyclic**. A frame boundary **inside a block** needs a **source position**, not just block range. The current flat-partition validator (:4050) is replaced by this tree validator.
- **Rolling summary = K=2 SEPARATE prior summaries, not cumulative** (L2A2 spec — rev2's "prev rolling_summary" singular/rewrite was wrong). Consumer = next chapter's B3 `[rt]`, not B0.
- KEEP `event_ids` name (runtime reads it @ :571); RESTORE motifs `[cp]`, character_state_changes `[rt→L4]`, unresolved_threads `[cp]`; RETIRE scene_summaries (test stub only).

### 2.5 B4 — identity + phase — complete consume-list (round-2 F8)

**B4 CONSUMES:** B0 cast_claims (untrusted, scene-scoped) · B1 mentions **+ full glossary (source_term/proposed_target_vi/category/do_not_translate)** · B2 endpoints + relation_events + register_cue + address occurrences · B3 occurrence-grounded observations + character_state_changes + facts + frames **+ motifs + unresolved_threads + the K rolling summaries**. Each item enters via a **universal envelope** (Canonical §6) — no envelope, no entry.
**B4 PRODUCES (B0–B3 never fill):** entities, alias valid_range, phase intervals, address_policies, entity_state_intervals, overlay/disclosure records — and only here are events grouped into final pairs.

---

## 3. Fill order & hand-offs (unchanged shape; corrected wiring)

B0→B1→B2(needs B0 scene-projection + B1 mentions)→B3(needs B0 typed projection + B1 roster + B2 events + K prior summaries)→B4(needs all, groups pairs). Corrected: rolling_summary→next-B3 (not B0); B3 gets a **typed projection** of B0 (not the full brief — see §4); phase pairs formed post-B4.

---

## 4. Per-layer INPUT contract — typed projections + sentinel test (round-2 F7)

The conformance test = typed payload allowlist + a unique **sentinel injected into every forbidden source**, asserted absent from the FULL rendered request + a **lineage-fingerprint** that no forbidden-source order_index entered.

| Layer | ALLOWED | FORBIDDEN (sentinel-asserted) | tail/as-of |
|---|---|---|---|
| B0 | chapter block text only | REGISTRY_SO_FAR, neighbor summaries, prior-chapter identity/summary | mode topology §1 |
| B1 | active window + read-only tail | registry, entity ids, B0 hints-as-authority | tail by mode (online=prev-only; frozen=±K audited) |
| B2 | window + tail + B0 **scene projection** + B1 WINDOW_MENTIONS | registry, candidate ids, future windows, non-intersecting scenes | mode |
| B3 | full chapter + **typed B0 projection (setting + neutral_premise as GIST_ONLY)** + B1 roster + B2 events + K prior summaries | **the full brief renderer** (`render_chapter_brief_for_injection` carries cast/role/participant = the B0→downstream bias M4f removes), future chapters, entity ids | chapter-local |

**Two live contradictions to fix:** (1) `builder_pilot.py:1174` injects registry+neighbor into B0. (2) handing B3 the full brief reopens the cast/role hint channel — B3 must get a typed projection where `neutral_premise` is GIST_ONLY (never evidence for a fact/frame/relation) and cast/role are sentinel-tested OUT.

---

## 5. Phase 1 sequence (Sol-locked — prompts LAST)

1. **Lock rev3 schema:** mode-specific B0 topology, source positions (B1 span/ordinal, B2 position_key, in-block frame position), occurrence-grounded B3, nesting-tree invariants.
2. **Typed schemas + validators + code-mint/remap** written and tested BEFORE any prompt: drop old required fields (:3818/:3922), add id format/unique/order, nesting-tree validator (:4050), scene exact-cover validator (:3727), deterministic mint.
3. **Dismantle the old identity path:** remove `seed_entity_ledger_from_chapter_brief`, `update_entity_ledger_from_lexicon`, `render_chapter_brief_for_injection` (as an injection source), registry→B1 and roster→B2 injection; replace M1 entity-ledger with occurrence/claim ground state; convert M2 roster to occurrence rows; **bump M1/M2 checkpoint schema**.
4. **Normalized handoffs:** B1→B2 (code-minted ids in WINDOW_MENTIONS), M2 occurrence roster, B3 tree/event schema.
5. **B4 ingest/persistence:** full glossary handoff (fix `_glossary_as_of` drop), motifs/threads/T4, universal envelopes.
6. **THEN** rewrite the four prompts, the token estimator, the sentinel conformance test, and a dry-run rendered-call-graph.

---

## 6. A–E status + round-2 resolutions

- A (split frame_kind/story_time): still a Canonical §7 change; now bundled with the §2.4 nesting-tree spec.
- B (code-mint ids): confirmed + now needs the source-position coordinates (§2.2/§2.3) to be deterministic.
- C (address_terms turn-embedded): unchanged, PASS.
- D (B0 scope): **superseded by §1** — not simple chapter-scope; topology is mode-dependent + scene exact-cover + co_present_count untrusted.
- E (retirement): 3 fields retired (verified). Consumer provenance now tagged `[rt]/[cp]/[audit]`; **`confidence`** retire unless Context Audit actually persists/reads it (currently no literary read-site, not in Canonical ground endpoint); **`neutral_premise`** kept only as GIST_ONLY to B3 and must prove value beyond a self-created consumer.

No new `other_new_class` vs §E9 (Sol concurs) — findings map to context_packaging / hint_bias / schema_contract / provenance_loss / wiring, all within the locked taxonomy.

---

## 7. What this unblocks

rev3 is LOCK-eligible only after **Sol round-3** confirms: mode topology, occurrence-grounded B3, nesting invariants, source-position mint, typed B3 projection, complete B4 list. Then Phase 1 runs steps 1–5 (schema/validators/orchestration) and only step 6 rewrites prompts + sentinel conformance + dry-run. The prompt rewrite is the last 10%, not the first move.

---

## 8. FIVE IMPLEMENTATION LOCKS (rev4 — resolves Sol round-3 NO-GO; GO after this)

Sol round-3 verdict: NO-GO with exactly five BLOCKER-to-implement decisions an implementer would otherwise invent, and **no dependency-order blocker**. Each is now locked to a single choice. After §8, Phase 1 steps 1–2 are GO.

### LOCK-1 — B0 topology & coverage universe (one choice)
- **B0 is ALWAYS a single whole-chapter call.** It is NOT made scene-local. The as_of problem is resolved not by changing B0's topology but by its **consumption**: B0 cast_claims are `whole_book_frozen`-only. **In `as_of_experiment` mode B0 output is NOT consumed at all** — the canary identity call relies on scene text + as-of-filtered retrieval, and B0 claims are already barred from witness authority (Canonical §4), so excluding them costs nothing and removes the chapter-end `input_max_order` leak by construction. This dissolves the chicken-and-egg (no pre-B0 scene segmenter needed).
- **Coverage universe for exact-cover = non-heading blocks only** (`block_type ∈ {paragraph, dialogue}`; `heading`/structural excluded). Verified: `block_type` already tags heading/dialogue/paragraph (builder_pilot.py:4284/4313) and frame coverage already filters heading (:4063). Scenes and frame leaves exact-cover this universe.
- `co_present_count` stays an **untrusted claim** (never a hard B2 rule).

### LOCK-2 — one shared `SourceAnchor` (kills the coordinate divergence)
Every source coordinate in the Builder uses ONE type:
- **Shape:** `SourceAnchor = {block_id, char_start, char_end}`.
- **Canonical source string:** the block's stored `text` field **NFC-normalized**, i.e. the exact string the model is shown. Offsets index THIS string.
- **Offset unit:** Unicode **code points** (not bytes, not UTF-16). **Half-open `[char_start, char_end)`** — consistent with the §8 interval convention.
- **Model never emits offsets.** The model emits a **verbatim quote span** (+ an optional `occurrence_hint` = "the Nth time this surface appears in the block"). **Code locates** the span by exact substring search in the NFC block text → `{char_start, char_end}`; if it occurs more than once, code uses `occurrence_hint`, else **fail-closed** (drop with a counter, never guess). This is the deterministic-mint foundation (model does verbatim copy = language; code does the arithmetic = mechanical).
- **Derived keys (all code-computed from SourceAnchor):** `occurrence_ordinal` = rank of same-surface anchors in the block by `char_start`; `position_key = (block_order, char_start)` where `block_order` = index of the block in the chapter's non-heading sequence; `mention_id = m_<block>_<occurrence_ordinal>`; `turn_id/event_id/endpoint_id` from `position_key` + role. An in-block frame boundary is a `SourceAnchor` too.

### LOCK-3 — endpoint two-axis validity + `confidence` retired
- **Two orthogonal fields** on every endpoint: `reference_scope ∈ {individual, group, narrator, reader, unknown}` (discourse) and `referent_kind_claim ∈ {person, animal, nonhuman_character, place, group_reference, object, unknown}` (ontology).
- **Validity/routing rules (the "table", as rules not a 35-cell grid):**
  - **Entity-eligible (→ person consolidation) IFF** `reference_scope=individual AND referent_kind_claim=person`.
  - **Captured, routed OUT of person-consolidation:** `individual + {animal, nonhuman_character}` (acting/personified beings); `group + group_reference`.
  - **Discourse-only, never an entity:** `narrator + {person, unknown}`; `reader + {person, unknown}`.
  - **`unknown` scope OR `unknown` kind:** allowed with anything (defers that axis; not yet eligible).
  - **Invalid (validator FLAGS, does not drop — [[validator-fix-field-keep-item-not-drop]]):** `individual + {place, object, group_reference}`; `group + non-group_reference`; `narrator/reader + a kind other than person/unknown`. Flag → route to review, keep the row.
  - **Runtime eligibility reads the ontology axis + checker, NEVER the word "person" in the scope axis.**
- **`confidence` is RETIRED** from v2 endpoints. Verified: no literary decision-consumer reads its value (emit + validator-enum only, builder_pilot.py:546/554/562/4443/4475); `attribution_method` is the trust signal (v1's own statement). Resolves the rev3:51↔115 contradiction.

### LOCK-4 — B3 occurrence-grounded typed schema
B3 emits observations that reference B2 occurrences; **no `pair`, no identity canonicalization** — code groups into final pairs only after B4 active bindings.
- **`relation_observations[]`** (replaces `relation_event_summary`): required `{event_id (from B2), endpoint_refs: [endpoint_id, endpoint_id], observed_valence_hint ∈ {positive,negative,mixed,unclear}, block_id, evidence_quote}`; optional `transition_hint: {trigger_event_id, note}` (references an event, never a pair; evidence-flag, not a phase).
- **`character_state_changes[]`**: required `{subject_ref: endpoint_id | mention_id, attribute ∈ {social_status, alias_or_title, life_status, residence}, from_value, to_value, trigger_ref: event_id | block_id, evidence_quote}`. Points to an occurrence, never a clean identity surface.
- **`unresolved_threads[]`**: required `{thread_local_id, description, opened_block, kind ∈ {mystery, pending_transition, question}}`; optional `subject_refs: [endpoint_id | mention_id]` when about characters.
- **`translator_relevant_facts[]`**: required `{fact_type ∈ {narrator, register, speech_style, status, setting}, fact, block_evidence, inference_basis ∈ {stated, derived}}`; optional `subject_ref` (occurrence) + `event_ids` for character facts.
- `chapter_rolling_summary` unchanged; K=2 separate priors on the input side (LOCK not here — §2.4).

### LOCK-5 — frame tree: synthetic root + `frame_kind` split (amends Canonical §7)
- **Choose SYNTHETIC ROOT** (not forest): one virtual root spanning the whole non-heading chapter; every model segment descends from it (parent defaults to root). Single tree → root coverage = whole chapter; invariants (child⊂parent, siblings non-overlap, leaves exact-cover, deepest active leaf renders, unique local keys, every parent exists, acyclic) are checked against this one tree.
- **`frame_kind` IS added**, orthogonal to `story_time_label`:
  - `frame_kind ∈ {primary_narration, embedded_document, letter, diary, dream, vision, tale_told_aloud, quoted_report}`
  - `story_time_label ∈ {frame_present, retrospective_past, anterior_past}`
  - A letter read aloud = `frame_kind=letter` + `story_time_label=retrospective_past` (the case rev1-A raised). Both required per segment.
- **This amends Canonical §7** (which had only `story_time_label`). The §7 text is updated in the same commit so Canonical and this doc do not drift.

**Post-§8 verdict:** the five invent-decisions are closed; per Sol, no other dependency-order blocker. **Phase 1 steps 1–2 (typed schemas + validators + code-mint/remap) are GO.** rev4 status = LOCK-eligible pending a Sol GO-confirm read (mechanical, not a new round).
