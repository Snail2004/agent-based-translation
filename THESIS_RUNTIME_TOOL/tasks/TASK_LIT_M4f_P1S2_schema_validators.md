# TASK_LIT_M4f_P1S2 — Builder typed schemas + validators + deterministic code-mint (Phase 1, Step 2)

Status: **DRAFT rev2 (Claude → Terra/CodeX), 2026-07-12.** rev2 folds Sol's task review (5 BLOCKER + 2 MAJOR, all verified on source). Task is DRAFT until Sol confirms rev2. Implementer = Terra (CodeX). **Verify gate = Claude, never delegated.**

Source of truth: `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` §8 (rev4 locks) + §2, Canonical §5/§7. **Every shape needed is transcribed IN FULL below — copy from here, not from prose elsewhere.**

---

## 0. Scope & non-scope (READ FIRST)

Builds NEW, self-contained, OFFLINE modules + tests. Does NOT wire into the live pipeline, does NOT change prompts, does NOT remove old orchestration.

OUT of scope (later steps): Step 3 orchestration removal (seed/update ledger, registry/roster injection, checkpoint bump); Steps 4–6 handoff/B4-ingest/prompt/dry-run. Old `builder_pilot.py` validators (:3818/:3922/:4050/:3727) stay running; the new validators live beside them, proven by fixtures first.

Hard constraints: **no LLM/API**, no network, do not touch the frozen D2L DB (`data/jobs/d2l_p1/memory.sqlite3`, SHA `64D989…`), `sk-` scan before commit, leave app-E12 files untouched, **inputs are never mutated** (validators return a new payload).

---

## A. Deliverables

1. `pipeline/literary/source_anchor.py` — `SourceAnchor`, NFC helper, locate/mint/remap.
2. `pipeline/literary/builder_schema_v3.py` — the typed schemas (dataclasses; match the frozen-dataclass style of `ValidationReport` at builder_pilot.py:158).
3. `pipeline/literary/builder_validators_v3.py` — validators returning **`ValidationResult[T]`** (§C).
4. `pipeline/tests/test_builder_schema_v3.py` — fixtures per §E.

---

## B. Anchor & ID model (fixes Sol BLOCKER-1, BLOCKER-2)

**Two distinct spans per occurrence-bearing item — do NOT conflate:**
- `anchor_text` = the exact surface substring being identified ("he", "Mira", "the master"). Mints the ID.
- `evidence_quote` = the clause/sentence proving the claim ("he said to Mira"). Proves attribution. NEVER the mint source.

**SourceAnchor** = `{block_id: str, char_start: int, char_end: int}`, half-open `[start,end)`, **Unicode code-point** offsets into `nfc(block.clean_text or block.source_text)`. (See §C for the NFC canonical-render contract.)

**locate(block, anchor_text, evidence_quote, occurrence_hint) -> SourceAnchor | FAILCLOSED:**
1. Locate `nfc(evidence_quote)` in the block string → `evidence_range` (0 or >1 with no hint → FAILCLOSED).
2. Locate `nfc(anchor_text)` **inside evidence_range**; exactly 1 → that span. 0 or >1 → retry over the whole block using `occurrence_hint` (1-based Nth). Still not unique → **FAILCLOSED** (drop + counter `fail_closed_locate`; never guess).

**mint (code only; model emits NO ids):**
- `mention_id = m_<block_id>_<ordinal>` where **`ordinal` is block-GLOBAL over ALL mention anchors in the block, ranked by the stable tuple `(char_start, char_end, surface)`** — NOT per-surface (per-surface collides: Alice#1 and Bob#1 would both be `_01`).
- `position_key = (block_order, char_start, local_ordinal)`; `block_order` = index in the chapter's **non-heading** block sequence (`block_type ∈ {paragraph,dialogue}`); `local_ordinal` breaks ties among turns/events sharing `(block, char_start)`: speaker_turns before relation_events, then model array order.
- `turn_id = t_<block>_<seq>`, `event_id = e_<block>_<seq>` (seq from position_key), `endpoint_id = "<turn_id|event_id>#<role>"`, `address_occurrence_id = "<turn_id>#addr<n>"`.

---

## C. NFC contract (fixes Sol MAJOR-6) + validator API (fixes Sol BLOCKER-3)

- **NFC canonical render:** the current renderer (`render_block_markers`, builder_pilot.py:414) emits raw `clean_text or source_text` **without NFC**. This module provides `nfc_block_string(block) -> str`. **Contract note for the Step-4 wiring task:** `render_block_markers` MUST be repointed to `nfc_block_string` so the model sees exactly the string anchors index. Until then, a fixture asserts `render == anchor_string` byte-for-byte after NFC on a decomposed-Unicode block.
- **Validator return type — matches runtime:** validators return **`ValidationResult[T] = {payload: T, report: ValidationReport}`** where `ValidationReport` is the existing frozen dataclass (`{name, ok, errors, warnings, counts}`, builder_pilot.py:158). `payload` is the NORMALIZED output (fail-closed items removed, flagged items retained, retired fields stripped) — this is what proves "item not emitted." Input is not mutated. Three disposition classes, all counted: **fatal** (→ `report.ok=False`), **flagged-retained** (kept in `payload` + `counts[flag_*]`), **dropped** (removed from `payload` + `counts[dropped_*]`).

---

## D. Typed schemas — FULL (fixes Sol BLOCKER-4, BLOCKER-5). Copy verbatim.

Notation: `field: type` — `!` required, `?` optional, `|null` nullable. `[code]` = code-filled post-parse (model never emits). Ontology enum `RKC = person|animal|nonhuman_character|place|group_reference|object|unknown`.

### B0 output `ChapterBrief`
- `chapter_id: str!`
- `cast_claims: [CastClaim]!`
- `setting: {place: str!, time_frame_hint ∈ {frame_present,past_recollection,unclear}!, scene_shape ∈ {single_scene_one_location,few_scenes,many_scenes_or_travel}!}!`
- `scenes_party_size: [Scene]!`
- `neutral_premise: str!` (≤40 words; GIST_ONLY downstream)
- `[code] input_max_order: int` (call-level, from request topology)

`CastClaim`: `surface: str!`, `surface_kind ∈ {proper_name,descriptor}!`, `referent_kind_claim: RKC!`, `role_hint: str!`, `scene_range: [block_id, block_id]!`, `anchor_text: str!`, `evidence_quote: str!`, `occurrence_hint: int?`, `[code] cast_claim_id: str`, `[code] anchor: SourceAnchor`, `[code] evidence_max_order: int`. (No `max_source_block` — removed; call-level `input_max_order` replaces it.)

`Scene`: `block_range: [block_id, block_id]!`, `co_present_count: int!`, `participants: [str]!`. (Untrusted claim; see validator exact-cover.)

### B1 output `Lexicon`
- `chapter_id: str!`, `window_block_ids: [block_id]!`, `context_only_used: bool!`
- `character_mentions: [Mention]!`, `glossary_candidates: [Glossary]!`

`Mention`: `surface: str!`, `mention_type ∈ {name,nickname,descriptor}!`, `referent_kind_claim: RKC!`, `anchor_text: str!`, `evidence_quote: str!`, `occurrence_hint: int?`, `block_id: block_id!`, `[code] mention_id: str`, `[code] anchor: SourceAnchor`.
`Glossary`: `source_term: str!`, `proposed_target_vi: str!`, `category ∈ {place,object,cultural,other}!`, `do_not_translate: bool!`, `block_ids: [block_id]!`. (Keep name `source_term`; NO `termhood`.)

### B2 output `Narrative`
- `chapter_id: str!`, `window_block_ids: [block_id]!`, `context_only_used: bool!`
- `speaker_turns: [Turn]!`, `relation_events: [Event]!`

`Endpoint`: `surface: str!`, `reference_scope ∈ {individual,group,narrator,reader,unknown}!`, `referent_kind_claim: RKC!`, `mention_ref: mention_id|null!`, `attribution_method ∈ {explicit_tag,turn_alternation,nearby_context,narrator_inference,vocative}!`, `anchor_text: str!`, `evidence_quote: str!`, `occurrence_hint: int?`, `[code] endpoint_id`, `[code] anchor: SourceAnchor`, `[code] resolution_evidence`. **(attribution_method = v1 set {explicit_tag,turn_alternation,nearby_context,narrator_inference} PLUS `vocative`, additive migration — nearby_context is KEPT, it is live in runtime. NO `confidence` field.)**
`Turn`: `speaker: Endpoint!`, `addressee: Endpoint|null!`, `utterance_quote: str!`, `address_terms: [AddressTerm]!`, `register_cue ∈ {neutral,intimate,deferential,paternal,hostile,mocking}!`, `block_id: block_id!`, `[code] turn_id`.
`AddressTerm` (ONE shape): `anchor_text: str!`, `evidence_quote: str!`, `occurrence_hint: int?`, `addressee_ref: "speaker"|"addressee"!` (which turn endpoint this vocative addresses — restores rev2's addressee_ref), `[code] address_occurrence_id`, `[code] anchor: SourceAnchor`.
`Event`: `actor: Endpoint!`, `target: Endpoint!`, `event_type: str!` (lower_snake_case action; FORBIDDEN relationship/phase labels), `evidence_quote: str!`, `block_id: block_id!`, `[code] event_id`. (actor/target MAY be non-person → route-out, not drop.)

### B3 output `Digest`
- `chapter_id: str!`, `chapter_rolling_summary: str!`
- `narration_frame_segments: [FrameSegment]!`, `relation_observations: [RelObs]!`, `character_state_changes: [StateChange]!`, `unresolved_threads: [Thread]!`, `translator_relevant_facts: [Fact]!`

`FrameSegment`: `local_segment_key: str!`, `parent_local_key: str|null!` (null = child of synthetic root), `narrator_surface: str!`, `frame_kind ∈ {primary_narration,embedded_document,letter,diary,dream,vision,tale_told_aloud,quoted_report}!`, `story_time_label ∈ {frame_present,retrospective_past,anterior_past}!`, `block_range: [block_id, block_id]!`, `start_anchor: SourceAnchor?`, `end_anchor: SourceAnchor?` (present when a boundary is mid-block), `status ∈ {proposed,uncertain}!`, `evidence_quote: str!`, `[code] segment_id`, `[code] version`.
`RelObs`: `event_id!`, `endpoint_refs: [endpoint_id, endpoint_id]!`, `observed_valence_hint ∈ {positive,negative,mixed,unclear}!`, `block_id!`, `evidence_quote!`, `transition_hint: {trigger_event_id, note}?`. (NO `pair`.)
`StateChange`: `subject_ref: endpoint_id|mention_id!`, `attribute ∈ {social_status,alias_or_title,life_status,residence}!`, `from_value: str!`, `to_value: str!`, `trigger_ref: event_id|block_id!`, `evidence_quote!`.
`Thread`: `thread_local_id: str!`, `description: str!`, `opened_block: block_id!`, `kind ∈ {mystery,pending_transition,question}!`, `subject_refs: [endpoint_id|mention_id]?`.
`Fact`: `fact_type ∈ {narrator,register,speech_style,status,setting}!`, `fact: str!`, `block_evidence: [block_id]!`, `inference_basis ∈ {stated,derived}!`, `subject_ref: endpoint_id|mention_id?`, `event_ids: [event_id]?`.

---

## E. Validators + fixtures (fixes Sol MAJOR-7 — every rule needs a subcase)

Validator rules (per §D + §8): enum-check (out-of-enum = fatal on required, normalize+flag where a secondary field); id uniqueness; mention_ref resolves to a same-window mention; anchors present or item fail-closed; **B0 scenes exact-cover + non-overlap** over non-heading blocks; **B2 two-axis validity** (eligible iff individual+person; route-out classes tagged; invalid combos flagged-not-dropped); **event_type phase_leak = 0** (hard); **B3 occurrence-grounding** (refs must be existing ids, reject clean surface/`ent_*`); **frame tree** (synthetic root; unique keys; parent exists; acyclic; child⊂parent; siblings non-overlap; leaves exact-cover; record deepest-active-leaf).

Fixtures (grouped; each group MUST include the listed subcases):
1. **Happy path** per stage + **mint determinism** (same input twice → identical ids).
2. **Locate**: (a) ambiguous anchor, no hint → fail-closed; (b) anchor unique only inside evidence_range; (c) `occurrence_hint` disambiguates.
3. **mention_id collision**: two different surfaces (Alice, Bob) in one block → distinct ids (this is the BLOCKER-1 regression test).
4. **same-anchor tie-break**: a turn + an event at one `(block, char_start)` → distinct ids via local_ordinal.
5. **Scenes**: overlap → `scene_overlap`; **gap** → `scene_gap`.
6. **Two-axis**: invalid `individual+place` → flagged+kept; `narrator+person` → discourse-only tag.
7. **event_type**: `"enemy"` → `phase_leak`.
8. **Frame tree**: nested letter-in-narration valid + deepest-leaf render; **missing parent** → fatal; **sibling overlap** → fatal; **leaf coverage gap** → fatal; **cycle** → fatal.
9. **Occurrence-grounding**: `StateChange.subject_ref="Heathcliff"` (bare surface) or `ent_*` → fatal.
10. **Field retirement**: input carrying `confidence`/`utterance_gist`/`termhood`/`resolution_status`/`candidate_entity_ids` → stripped from `payload` (proven via the returned payload).
11. **Unicode**: a decomposed (NFD) block → `nfc_block_string` yields the render string byte-for-byte; anchors index correctly.
12. **non-person route-out**: an `Event` with `actor.referent_kind_claim=animal` → captured + tagged route-out, NOT dropped.

---

## F. Report back to Claude (verify gate)

Return the four files (diff), the fixture run (all green), the determinism proof (same input → same ids twice), and the emitted counter names. **No self-certify** — Claude re-runs the fixtures and spot-checks locate/mint determinism + the scene/frame/two-axis validators before acceptance. Nothing wires into the live pipeline until Step 3.
