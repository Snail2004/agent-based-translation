# TASK_LIT_M4f_P1S2 — Builder typed schemas + validators + deterministic code-mint (Phase 1, Step 2)

Status: **DRAFT (Claude → Terra/CodeX), 2026-07-12.** Task is DRAFT until Sol confirms it. Implementer = Terra (CodeX). **Verify gate = Claude, never delegated.**

Source of truth for every schema: `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` **§8 (rev4 locks) + §2 matrix**, and Canonical §5/§7. **Copy the final shapes from §8 only** — ignore any earlier sentence that still says "scene-local OR…", "synthetic root OR forest", "text field", or keeps `confidence`; those are superseded by §8.

---

## 0. Scope & non-scope (READ FIRST)

**This task builds NEW, self-contained, OFFLINE modules + tests. It does NOT wire them into the live pipeline and does NOT change any prompt.**

IN scope:
- New typed schema definitions for B0/B1/B2/B3 outputs (post-parse ground layer) + the frame tree.
- The `SourceAnchor` type + deterministic locate/mint/remap functions.
- New validators (pure functions) for each stage per §8.
- Unit tests / fixtures exercising every rule, including the fail-closed and reject cases.

OUT of scope (later steps — do NOT touch):
- Step 3: removing `seed_entity_ledger_from_chapter_brief`, `update_entity_ledger_from_lexicon`, registry/roster injection, checkpoint schema bump.
- Steps 4–6: B1→B2/M2/B3 handoff rewiring, B4 ingest, prompt rewrites, dry-run.
- The existing `builder_pilot.py` validators (:3818/:3922/:4050/:3727) stay AS-IS and running; the new validators live beside them (new module), proven by fixtures first. Do not delete or repoint the old path in this task.

Hard constraints: **no LLM/API calls** (everything here is pure/deterministic); **no network**; do **not** access or modify the frozen D2L DB (`THESIS_RUNTIME_TOOL/data/jobs/d2l_p1/memory.sqlite3`, SHA `64D989…`); scan staged diffs for `sk-` before committing; keep the working tree's app-E12 files untouched.

---

## A. Deliverables

1. `pipeline/literary/builder_schema_v3.py` — the typed schemas (dataclasses matching repo style; if the repo uses TypedDict/pydantic elsewhere in literary/, match that).
2. `pipeline/literary/source_anchor.py` — `SourceAnchor` + locate/mint/remap.
3. `pipeline/literary/builder_validators_v3.py` — pure validators returning `(ok, errors, warnings, counts)` (same return contract as the existing `_validate_*` for easy later swap).
4. `pipeline/tests/test_builder_schema_v3.py` — fixtures + assertions for every rule in §C–§E.

No other files change.

---

## B. Locked schemas (transcribe from §8; do not redesign)

### SourceAnchor (LOCK-2)
`SourceAnchor = {block_id: str, char_start: int, char_end: int}`.
- Indexes the block's **`clean_text` (fallback `source_text`)** field, **NFC-normalized** — the exact string the model is shown via `render_block_markers` (builder_pilot.py:416). NOT `text`.
- Offsets are **Unicode code points**, **half-open `[char_start, char_end)`**.

### B0 cast_claim (surface × scene)
`{surface, surface_kind ∈ {proper_name, descriptor}, referent_kind_claim (ontology enum below), role_hint, scene_range: [block_id, block_id], source_block_ids: [block_id…], quote}` — model output; **code adds** `cast_claim_id`, `evidence_max_order`, `max_source_block`. A surface recurring in two scenes = **two claims** (never merged across scenes).

### B1 character_mention
Model: `{surface, mention_type ∈ {name,nickname,descriptor}, referent_kind_claim, quote (verbatim span), occurrence_hint?: int, block_id}` + `glossary_candidates[]` = `{source_term, proposed_target_vi, category ∈ {place,object,cultural,other}, do_not_translate: bool, block_ids}` (keep names `source_term`; **no** `termhood`). **Code adds** `mention_id`, `source_anchor`. `resolution_status`/`candidate_entity_ids` are GONE.

### B2 endpoint (two axes, LOCK-3) — used by speaker/addressee/actor/target
Model: `{surface, reference_scope ∈ {individual,group,narrator,reader,unknown}, referent_kind_claim (ontology), mention_ref: mention_id|null, attribution_method ∈ {explicit_tag,turn_alternation,narrator_inference,vocative}, quote}`. **No `confidence`.** **Code adds** `endpoint_id`, `source_anchor`, `resolution_evidence`.
- `speaker_turns[]`: `{speaker: endpoint, addressee: endpoint|null, utterance_quote, address_terms: [{surface, evidence_quote, quote, occurrence_hint?}…], register_cue, block_id}` + code adds `turn_id`, per-address `address_occurrence_id`.
- `relation_events[]`: `{actor: endpoint, target: endpoint, event_type (lower_snake_case action; FORBIDDEN relationship/phase labels), evidence_quote, block_id}` + code adds `event_id`. `actor/target` MAY be non-person (route-out, not drop).

### B3 observations (occurrence-grounded, LOCK-4) — NO `pair`, NO identity
- `relation_observations[]`: `{event_id, endpoint_refs: [endpoint_id, endpoint_id], observed_valence_hint ∈ {positive,negative,mixed,unclear}, block_id, evidence_quote, transition_hint?: {trigger_event_id, note}}`.
- `character_state_changes[]`: `{subject_ref: endpoint_id|mention_id, attribute ∈ {social_status,alias_or_title,life_status,residence}, from_value, to_value, trigger_ref: event_id|block_id, evidence_quote}`.
- `unresolved_threads[]`: `{thread_local_id, description, opened_block, kind ∈ {mystery,pending_transition,question}, subject_refs?: [endpoint_id|mention_id]}`.
- `translator_relevant_facts[]`: `{fact_type ∈ {narrator,register,speech_style,status,setting}, fact, block_evidence, inference_basis ∈ {stated,derived}, subject_ref?, event_ids?}`.
- `narration_frame_segments[]` (frame tree, LOCK-5): `{local_segment_key, parent_local_key, narrator_surface, frame_kind ∈ {primary_narration,embedded_document,letter,diary,dream,vision,tale_told_aloud,quoted_report}, story_time_label ∈ {frame_present,retrospective_past,anterior_past}, block_range, status ∈ {proposed,uncertain}, evidence_quote}` + code adds `segment_id`, `version`.
- `chapter_rolling_summary` (string).

Ontology enum (referent_kind_claim) everywhere: `person | animal | nonhuman_character | place | group_reference | object | unknown`.

---

## C. SourceAnchor locate + code-mint (deterministic; LOCK-2)

1. `normalize(block) -> str`: NFC of `clean_text or source_text`; if both empty → block yields NO anchors (its items fail-closed).
2. `locate(block, quote, occurrence_hint) -> SourceAnchor | FAILCLOSED`: exact substring search of NFC(quote) in the normalized block string. 0 matches → FAILCLOSED. 1 match → that span. >1 → use `occurrence_hint` (1-based Nth); missing/out-of-range hint → FAILCLOSED. Never guess.
3. Derived ids (code only; model never emits ids):
   - `occurrence_ordinal` = rank of same-`surface` anchors in the block by `char_start` (1-based).
   - `mention_id = m_<block_id>_<occurrence_ordinal>`.
   - `position_key = (block_order, char_start, local_ordinal)` where `block_order` = index of block in the chapter's **non-heading** sequence (`block_type ∈ {paragraph,dialogue}`); **`local_ordinal`** breaks ties among turns/events sharing `(block, char_start)`: speaker_turns before relation_events, then model array order — canonicalized by code (position_key without it is NOT unique).
   - `turn_id = t_<block>_<position_key ordinal>`, `event_id = e_…`, `endpoint_id = (turn_id|event_id, role)`.
4. FAILCLOSED items are dropped WITH a counter (`fail_closed_locate`), never emitted with a guessed anchor.

---

## D. Validator requirements (pure functions; per stage)

Return `(ok: bool, errors: list, warnings: list, counts: dict)`. Rules:

- **Enums**: every enum field checked; out-of-enum → error (do NOT drop the whole item — normalize/flag per [[validator-fix-field-keep-item-not-drop]]).
- **B0 scenes**: `scenes_party_size` ranges must be **exact-cover, non-overlapping** over non-heading blocks — reject overlap or gap (counter `scene_overlap`, `scene_gap`). (This is the check the current validator lacks — Gatsby ch1 b152 appears in two scenes.)
- **B1/B2 ids**: every code-minted id **unique**; `mention_ref` (if non-null) must reference an existing `mention_id` from the same window; anchors present (or item is fail-closed).
- **B2 two-axis validity (LOCK-3)**: entity-eligible IFF `reference_scope=individual AND referent_kind_claim=person`; route-out classes tagged (`individual+{animal,nonhuman_character}`, `group+group_reference`); discourse-only tagged (`narrator/reader+{person,unknown}`); **invalid combos FLAGGED (counter, kept for review), never dropped** (`individual+{place,object,group_reference}`, `group+non-group_reference`, `narrator/reader+other`).
- **B2 event_type discipline**: lower_snake_case; reject relationship/phase labels → counter `phase_leak` (hard gate, must be 0).
- **B3 occurrence-grounding**: every observation/state/thread/fact reference is an existing `event_id`/`endpoint_id`/`mention_id` (or block_id where allowed) — **reject any clean identity surface / entity_id** in these fields.
- **Frame tree (LOCK-5)**: build the tree from `local_segment_key`+`parent_local_key` under a **synthetic root**; validate: keys unique, every parent exists, **acyclic**, each child block_range ⊂ parent, **siblings non-overlapping**, **leaves exact-cover** the non-heading blocks. Reject on any violation (counters). Deepest active leaf is the rendered frame (record it).

---

## E. Fixtures / acceptance (must all pass)

Synthetic inputs (no real book text needed; may reuse block ids like `bk_ch01_b004`):
1. **Happy path** each stage → valid, ids minted deterministically, stable across two runs.
2. **Ambiguous locate**: a quote occurring twice in a block with no `occurrence_hint` → item fail-closed + counter, NOT emitted.
3. **Same-anchor tie-break**: a turn and an event at the same `(block, char_start)` → distinct ids via `local_ordinal`.
4. **Scene overlap reject**: two scenes both containing one block → `scene_overlap` error (the Gatsby case).
5. **Invalid two-axis combo**: `reference_scope=individual + referent_kind_claim=place` → flagged, item KEPT.
6. **event_type phase leak**: `event_type="enemy"` → `phase_leak` counter, rejected value.
7. **Frame tree**: a nested letter-inside-narration → valid tree, deepest-leaf render correct; a sibling-overlap and a cycle → rejected.
8. **Occurrence-grounding reject**: a `character_state_changes.subject_ref` set to a bare surface / `ent_*` → error.
9. **Field retirement**: presence of `confidence`/`utterance_gist`/`termhood`/`resolution_status` in input → dropped/flagged, not carried into the typed output.

---

## F. Report back to Claude (verify gate)

Return: the four new files (diff), the fixture run output (all green), the mint-determinism proof (same input → same ids twice), and the counter names emitted. **Do not** self-certify — Claude re-runs the fixtures + spot-checks the locate/mint determinism and the scene/frame validators on the artifacts before this task is accepted. Nothing wires into the live pipeline until Step 3.
