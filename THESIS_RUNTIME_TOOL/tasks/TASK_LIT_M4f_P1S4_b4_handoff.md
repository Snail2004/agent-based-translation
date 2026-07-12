# TASK_LIT_M4f — Phase 1 Step 4 — B4 input handoff (deterministic, 0-API) — DRAFT rev1

Status: **DRAFT rev1 (Claude). NOT ready for Terra.** First gate is a Sol GO/NO-GO on the Step-4/Step-5 scope boundary in §0, because that boundary is derived from the code topology below, not from a locked document. Do not implement until Sol confirms §0.

Contract source: `tasks/TASK_LIT_M4f_CANONICAL_V1.md` §2 (Context Contract), §3 (as-of), §4 (identity roles/witness/kind axes), §5 (occurrence overlay), §8 (interval & coverage), §9 (fingerprint/provenance). Upstream implementation already ACCEPTED: Step-2 (`source_anchor.py`, `builder_schema_v3.py`, `builder_validators_v3.py`) and Step-3 (`builder_v3_pipeline.py`, `checkpoint_v3.py`, commit `8fd3778`). If any wire shape or invariant below is ambiguous, STOP and ask; do not invent a local policy.

---

## 0. Scope boundary (Sol GO/NO-GO gate — resolve FIRST)

The Phase-1 engineering sequence is: (1) rev-schema, (2) typed schemas+validators+code-mint [DONE], (3) v3 orchestration [DONE], **(4) handoff wiring [THIS]**, (5) B4 ingest, (6) prompts+CLI. "Handoff wiring" is not defined verbatim in the Canonical; it is derived from the legacy topology:

- Legacy `story_bible_v2.py` splits the B4 (identity+phase) stage into two layers: a **reader/assembler** layer (`_verified_as_of_inputs` :192, `build_identity_atoms_as_of` :294, `_digest_payloads_as_of` :362) that turns validated M1/M2 checkpoints into B4's input material, and a **call layer** (`build_identity_messages` :485, `build_phase_messages` :592) that renders the LLM calls.
- **Step 4 = the v3 reader/assembler layer only.** It reads verified M1V3 + M2V3 checkpoints (whole_book_frozen) and produces the typed, deterministic, fingerprinted **B4 input bundle**: occurrence cards, person-eligible candidate roster, phase-observation inputs, frame/narrator context.
- **Step 5 = the v3 call layer:** identity retrieval → adjudicator → independent checker (§4), ID-free minting, decision-state ladder (proposed→corroborated→active), occurrence overlay apply (§5), phase + phase-disposition, address-term/frame checkers.

Explicit boundary calls that Sol must confirm or overrule:
- **B4 input carries FULL knowledge, no disclosure filtering.** Under whole_book_frozen the identity engine is allowed to see the whole book (§0.5/§1). Disclosure (§6, knowing≠rendering) governs the **Translator pack**, which is a *different* handoff built *after* the overlay exists — NOT in Step 4. If Sol wants the translator-pack handoff folded into Step 4, say so now.
- **No entity ids, no candidate ids, no decision states are produced in Step 4.** The bundle is occurrence-grounded only. Retrieval's candidate narrowing is a Step-5 call, not a Step-4 reader.
- **This is 0-API and prompt-free**, so it fits "prompts last" and is fully conformance-testable offline on the spoiler fixture.

If §0 is wrong, everything below is scoped wrong. NO-GO here costs one relay; a wrong scope costs a full implement+verify round.

---

## 1. Deliverable

Create exactly one new module and its test file:
- `pipeline/literary/b4_handoff_v3.py`
- `pipeline/tests/test_b4_handoff_v3.py`

Do not modify `builder_pilot.py`, `story_bible_v2.py`, `checkpoint.py`, `checkpoint_v3.py`, `builder_v3_pipeline.py`, `builder_validators_v3.py`, `source_anchor.py`, CLI, estimator, API client, app-E12 files, or the frozen D2L DB (`64D989...`). Import read-only helpers from Step-2/Step-3 modules where appropriate. If a shared helper truly must change, STOP and return the reason for gate review first. No LLM / API / network.

---

## 2. Inputs it reads (pin every shape to Step-3 output)

Read ONLY through the validated checkpoint chain, never by scanning directories.

M1V3 checkpoint state (per chapter), keys as published by Step-3:
`schema_version, chapter_id, contract_versions, windows, b0_payload, b1_by_window, b2_by_window, reference_index, request_manifest, semantic_state_hash`.

M2V3 checkpoint state (per chapter):
`schema_version, chapter_id, input_m1v3_checkpoint_hash, input_m1v3_identity_hash, digest_payload, occurrence_roster, digest_reference_index, prior_summary_provenance, request_manifest, semantic_state_hash`.

`occurrence_roster` row shape (from `builder_v3_pipeline._occurrence_roster`):
`{id, occurrence_kind: "mention"|"endpoint", surface, referent_kind_claim, reference_scope, block_id, anchor:{block_id,char_start,char_end}}`.

`digest_payload` (B3) fields: `chapter_rolling_summary, narration_frame_segments[], relation_observations[] (event_id + endpoint_refs, occurrence-grounded — NOT pairs), character_state_changes[], unresolved_threads[], translator_relevant_facts[]`.

Verification on read (mirror Step-3 discipline exactly):
- Use `checkpoint_v3.read_current_checkpoint` with the full `expected` identity, per chapter, chaining `parent_checkpoint_identity_hash`. Missing / foreign / stale (incl. source-topology change per Step-3 Finding-1) → **fatal**, no partial bundle.
- Require the full selected-prefix chain for BOTH M1V3 and M2V3; require each M2V3's `input_m1v3_identity_hash` to equal the matching M1V3 `checkpoint_identity_hash`. Any break → fatal.
- Deep-copy every payload on read; never expose a checkpoint/state for mutation.

---

## 3. Public functions and output shapes

```
load_verified_builder_v3_inputs(document, chapters, *, m1v3_dir, m2v3_dir) -> VerifiedBuilderV3Inputs
build_identity_occurrence_cards(inputs) -> list[OccurrenceCard]
build_person_candidate_roster(inputs)   -> RosterView
build_phase_observation_inputs(inputs)  -> list[PhaseObservationInput]
build_frame_narrator_context(inputs)    -> FrameNarratorContext
assemble_b4_input_bundle(document, chapters, *, m1v3_dir, m2v3_dir) -> B4InputBundle
```

`OccurrenceCard` (one per occurrence roster row, mention AND endpoint):
`{occurrence_id, occurrence_kind, surface, referent_kind_claim, reference_scope, chapter_id, block_id, block_order, anchor:{block_id,char_start,char_end}, evidence_quote, scene_window}` — and NOTHING else. Specifically it carries **no** entity id, candidate id, hint id, or resolution field.
- `evidence_quote` = the exact anchored clause from the ground state (the SourceAnchor span the model emitted, located verbatim in `nfc(clean_text||source_text)`), NOT a fixed-length regex window.
- `scene_window` = a code-expanded context window taken by **sentence boundary** per §8 (mechanical ±N sentences around the anchor block, typographic only), never truncating mid-clause and never a casefold character slice. It MUST contain `evidence_quote` verbatim.

`RosterView`:
`{person_candidates: list[OccurrenceCard], non_person: list[OccurrenceCard], counts: {...}}`.
- `person_candidates` = occurrences the model claimed `referent_kind_claim == "person"` AND `reference_scope in {individual, unknown}` are eligible per §4's routing (do NOT add a "resolved requires known kind" rule; the axes are independent). Mechanical enum routing only — no wordlist, no person-hood inference.
- `non_person` = everything routed out (place/object/animal/nonhuman_character/group_reference). **Routed, not dropped.** Juno-the-dog (`animal`/`group_reference`) lands here with full evidence, available to the Step-5 non-person tracks.
- Ordering: `(block_order, anchor.char_start, occurrence_kind, occurrence_id)` — identical to Step-3's roster sort.

`PhaseObservationInput` (from M2 `digest_payload.relation_observations`):
`{event_id, endpoint_refs, chapter_id, block_id, evidence_quote, observed_valence_hint}` — occurrence-grounded via endpoint_refs, **never a resolved pair**. Retain-history: an observation seen in an earlier chapter stays present in later-chapter assembly (no replace, no wipe — the #21 fact-wipe fix).

`FrameNarratorContext`: `{frame_segments: [...], narrator_surfaces: [...]}` from `digest_payload.narration_frame_segments`, occurrence/anchor-grounded per §7.

`B4InputBundle`:
`{schema_version, knowledge_mode: "whole_book_frozen", selected_chapters, occurrence_cards, roster, phase_observation_inputs, frame_narrator_context, provenance:{per-chapter m1v3_identity_hash + m2v3_identity_hash}, bundle_manifest_hash}`.
- `bundle_manifest_hash` = canonical hash over the deterministic semantic projection (exclude paths, timestamps, usage, operational hashes, and the hash field itself; retain deterministic identity hashes). Sorts pinned; lists preserve contract order; no hash-sorting.

---

## 4. Invariants (each tied to a verified defect — these are the point of the step)

- **I1 [b004]** No B4 input row may act as an identity witness or carry a binding hint. Occurrence cards/roster/phase inputs contain **no** `ent_*` id, `candidate_entity_ids`, or `hint_entity_id`. This is the exact defect legacy `build_identity_atoms_as_of` reintroduced (`hint_entity_id = candidate_entity_ids[0]`, :319). v3 ground state has no such field; the handoff must never synthesize one.
- **I2 [madam]** Every card's `evidence_quote` preserves the full identifying clause the model anchored; `scene_window` expands by sentence boundary and always contains `evidence_quote` verbatim. Never the legacy `_quote_context` 360-char casefold regex slice (:283) that dropped "the canine mother" defining clause.
- **I3 [door/juno]** `referent_kind_claim` routing is honored mechanically: non-person occurrences never enter `person_candidates`; animals/nonhuman are routed to `non_person`, not dropped. No wordlist, no code person-hood judgment (code-never-does-language-work).
- **I4 [#21 fact-wipe]** All reads are non-destructive and retain-history; phase/relation inputs never replace prior rows; the handoff mutates no checkpoint or state (assert byte-identical before/after).
- **I5 [#5 endpoint-null]** Endpoints remain occurrence-refs; the handoff fabricates no binding for a single-candidate endpoint. Binding is a Step-5 ACTIVE decision only.
- **I6 [frozen-only]** `knowledge_mode` accepts `whole_book_frozen` only; refuse anything else. Roster spans all selected chapters. **No disclosure filtering** in this step (§0).
- **I7 [determinism §9]** Two assemblies over identical verified inputs yield an identical `bundle_manifest_hash`. A source-topology change upstream re-derives (consistency with Step-3 Finding-1). No timestamps/paths/usage in the hashed view.
- **I8 [fail-closed]** Missing / foreign / stale / incomplete-ancestry checkpoint → fatal, no partial bundle. Input never mutated.

---

## 5. Forbidden

No LLM/API/network. Do not: introduce entity ids or decision states; apply disclosure; call `seed_entity_ledger_from_chapter_brief`, `update_entity_ledger_from_lexicon`, `render_chapter_brief_for_injection`, legacy `build_identity_atoms_as_of`, or any entity/alias/registry/roster helper; sort by hash; scan directories outside the validated manifest; modify any file listed in §1; touch the frozen DB or app-E12 files.

---

## 6. Acceptance — adversarial probes (spoiler fixture + synthetic; the verify bar)

Each probe must FAIL on a naive implementation and pass here:
1. **No-witness/no-hint (I1):** serialize the bundle; assert absence of `ent_`, `hint_entity_id`, `candidate_entity_ids` as substrings AND as structured fields; feed a synthetic upstream that (illegally) carries a hint and assert it is neither read nor emitted.
2. **Madam-class (I2):** build an occurrence whose defining clause sits >360 chars from a later surface repeat; assert the card's `evidence_quote` + `scene_window` retain the full clause verbatim (the exact §E9 madam case), and that no casefold/length-limited slice path exists.
3. **Door/Juno routing (I3):** a `place`/`object` occurrence must be absent from `person_candidates`; a `person` present; an `animal`/`group_reference` (Juno) present in `non_person`, never dropped. Counts reconcile to the roster total.
4. **Fact-retain across chapters (I4):** a ch1 relation observation is still present in the ch1–ch4 assembled `phase_observation_inputs` (no wipe).
5. **Non-destructive (I4):** hash M1V3/M2V3 checkpoint files before and after assembly; assert byte-identical.
6. **Ancestry-fatal (I8):** delete / foreign-stamp / stale one M1V3 and one M2V3 checkpoint; assert fatal with no partial bundle each time.
7. **Determinism (I7):** assemble twice → identical `bundle_manifest_hash`; then flip one upstream block's `block_type` and assert the ancestry read goes fatal/stale before any bundle is produced.
8. **Frozen-only (I6):** `knowledge_mode="online_as_of"` (or any non-frozen) → refuse.
9. **Full-suite regression:** Step-2/Step-3 suites stay green; frozen DB hash unchanged; `git diff --check` clean; key scan clean.

---

## 7. Handback

Report: branch + commit; the two new files only; suite counts (this file + full); frozen DB hash before/after; probe-by-probe results for §6; any finding CodeX found and fixed beyond this task. Leave the branch unmerged for Claude's independent adversarial verify (re-run §6 probes on the artifact) before ACCEPT.
