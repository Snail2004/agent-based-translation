# TASK_LIT_L2A2 — Chapter Pre-Read (B0) + general reference-resolution rules

Status: SCAFFOLD WIRED (CodeX, 0-API) / WAITING API PARITY RUN + CLAUDE GATE. Precedes M4.
Do NOT run M4 until this lands and Claude re-gates.

Implementation note (2026-07-09): B0 prompt extraction, `validate_chapter_brief`,
`CHAPTER_BRIEF` injection into B1/B2/B3, bounded `neighbor_summaries` K=2, and
M1 estimate plumbing are wired. `python -m pytest pipeline\tests` = 326 passed.
No API-backed WH/Gatsby parity run has been performed in this step.

Division of labor (locked): **Claude owns all prompt text + window-injection design (thesis core).**
CodeX builds scaffold/CLI/validator/injection plumbing and may edit prompt text ONLY on a serious
run-error, and must report back. The prompt blocks marked `COPY VERBATIM` below are authoritative —
copy them exactly into the pipeline; do not paraphrase or "improve".

Motivation (from M3 gate, WH ch1): the honest pipeline surfaced that 4/18 dialogue turns were left
`addressee unknown` not for lack of context but because the B2 prompt has no rule for (a) resolving a
GENERIC honorific vocative ("sir") by turn-taking, and (b) distinguishing the person spoken TO from
the thing spoken ABOUT. The model itself returned `candidate_entity_ids: []` — so the fix belongs in
the PROMPT (general, all books), never in M3 code (book-specific). We also add a cheap chapter
pre-read pass (B0) so detailed passes are framed by cast + setting, and we lock the cross-chapter
memory to a BOUNDED shape.

---

## 0. Memory model per chapter (bounded — this is the thesis contribution)

Chapter N's Builder passes are injected with a memory whose size is bounded by **how many entities
are on stage now + a fixed recency window K**, NOT by N (the number of chapters so far). Three parts,
with a HARD SPLIT between the GIST layer and the FACT layer:

- **B0 chapter brief** — of the CURRENT chapter only. Always exactly one. Regenerated per chapter.
- **neighbor_summaries (GIST layer)** — the per-chapter `chapter_rolling_summary` of the **last K
  chapters** (pilot **K=2**: chapters N-2 and N-1), each kept as its OWN clean summary — NOT merged,
  NOT rewritten. Sliding window: older chapters' summaries are simply DROPPED from injection, never
  compressed into a cumulative blur. No extra LLM call: B3 already emits each chapter's own summary,
  so this is pure retrieval of the last K — deterministic, no re-compression drift. K is a MEASURED
  knob (sweep K=1/2/3 in eval), not a fixed truth. Rationale: narrative gist is recency-dominated (a
  pronoun/scene in ch N usually continues ch N-1), and this layer carries ONLY "mood / where-are-we",
  never facts — so dropping distant summaries loses nothing important.
- **registry_pack (FACT layer)** — entities/aliases/relation-phases/address-policy FILTERED to those
  whose surface/alias literally appears in the current window (keyed lookup into the Story-Bible-so-
  far), bounded (~15-20 lines). THIS is what carries facts, and it is bounded by scene size (2-4
  people on stage), not by chapter count — so ch34's window pulls only the as-of state of the few
  people present, never "a summary of 33 chapters".

Long-range narrative dependencies (a character/thread seeded in ch4 that pays off in ch30) are NOT
the summary's job: identities/relations live in the FACT layer (queried as-of), and open setups live
in the Story Bible's `unresolved_threads`, surfaced only when they become relevant to the current
window. The sliding gist window is safe ONLY because of this backstop — never let the gist layer
become the fact store.

CodeX MUST add a test asserting that chapter N's injected context contains AT MOST K per-chapter
summaries (not N), and that injected-context size does NOT grow with N (compare ch3 vs a synthetic
ch12 fixture — token count within a small constant, not ~4x).

---

## 1. NEW PASS — B0 "Chapter Pre-Read" (`literary_chapter_brief_v1`)

Runs FIRST for each chapter, before B1. Reads raw chapter text (+ the bounded cross-chapter carry
for chapters >1). Output is injected as `CHAPTER_BRIEF` into the B1, B2, and B3 windows of the SAME
chapter.

### 1.1 System prompt — COPY VERBATIM

```
- Prompt version: literary_chapter_brief_v1.
- You are doing a FAST PRE-READ of ONE chapter BEFORE any detailed extraction. Return only valid JSON matching the Required JSON shape. No text outside JSON.
- Purpose: give the later passes a factual frame of WHO is on stage, WHERE, and roughly WHAT happens, so they can resolve pronouns and vocatives. You are NOT extracting evidence and NOT judging relationships.
- HARD LIMIT (leak guard): do NOT output relationship states, alliances, feelings, trust, phases, or emotional outcomes; do NOT say two characters "become" anything. Report only observable roles and neutral actions. Relationship conclusions are decided far later from the whole timeline — asserting them here would corrupt that.
- cast_on_stage: only persons who are physically present and act or speak in THIS chapter. Exclude persons merely named in passing, and exclude historical / portrait / inscribed / remembered names of people not present. For each: surface (verbatim, as first named), role_hint (a plain OBSERVED social role only — e.g. host, visitor, servant, child, innkeeper, traveller, soldier; NEVER a relationship word like friend, enemy, rival, lover), first_seen_block.
- setting: {place (verbatim if named, else a short description), time_frame_hint (one of: frame_present, past_recollection, unclear), scene_shape (one of: single_scene_one_location, few_scenes, many_scenes_or_travel)}.
- scenes_party_size: split the chapter into contiguous scenes; for each {block_range, co_present_count (how many named persons are together and could be addressed in that scene), participants (their surfaces)}. This is the signal the detailed pass uses to tell whether a bare vocative has exactly one possible addressee.
- neutral_premise: <=40 words, what happens at a plain factual level (who goes where, who meets whom, what is done). No inner-relationship verdicts, no spoilers of emotional arcs.
- Prefer an entity_id (ent_*) from REGISTRY_SO_FAR when a name clearly matches; else use the clean surface.
- Every block_id MUST be a marker that literally appears in this chapter's text.

Required JSON shape:
{
  "chapter_id": "...",
  "cast_on_stage": [
    {"surface": "the innkeeper", "role_hint": "innkeeper", "first_seen_block": "bk_ch01_b004"},
    {"surface": "Ravel", "role_hint": "traveller", "first_seen_block": "bk_ch01_b002"}
  ],
  "setting": {"place": "a roadside inn at dusk", "time_frame_hint": "frame_present", "scene_shape": "single_scene_one_location"},
  "scenes_party_size": [
    {"block_range": ["bk_ch01_b002", "bk_ch01_b016"], "co_present_count": 2, "participants": ["Ravel", "the innkeeper"]}
  ],
  "neutral_premise": "A tired traveller reaches an inn at nightfall, is questioned by the innkeeper about his business, and takes a room for the night."
}
```

(Illustrative names — innkeeper / Ravel / an inn — are INVENTED and belong to no target book, on
purpose: the schema teaches FORMAT without teaching any book's content.)

### 1.2 Validator `validate_chapter_brief` (CodeX, mechanical only)
- Structural/parse failure → fail pass. Entry-level problems → DROP the entry + count, keep pass.
- Checks: `time_frame_hint` in {frame_present, past_recollection, unclear}; `scene_shape` in
  {single_scene_one_location, few_scenes, many_scenes_or_travel}; every block_id appears in the
  chapter; each cast entry has non-empty surface + first_seen_block; `co_present_count` is an int.
- LEAK GUARD (mechanical, general — NOT a book wordlist): reject/drop any `role_hint` or
  `neutral_premise` containing a relationship-verdict token from this FIXED general set:
  {friend, friends, enemy, enemies, rival, lover, ally, allies, betray, reconcile, hate, love,
  trust, distrust}. Count `leak_tokens_dropped`. (This is a universal-English guard list, like the
  honorific list — not character names, so it generalizes across books.)

---

## 2. B2 upgrade — two general resolution rules (`literary_narrative_v1`)

### 2.1 Add to the B2 system prompt — COPY VERBATIM

```
- A GENERIC honorific used as a vocative (sir, madam, ma'am, my lord, my lady, master, mistress, with no personal name attached) does NOT by itself name a specific person. Resolve it by turn-taking using CHAPTER_BRIEF: if the current scene in scenes_party_size has co_present_count == 2, the addressee is the OTHER co-present person (the one who is not the speaker); set resolution_status=candidate, candidate_entity_ids to that person, attribution_method=turn_alternation, confidence=medium. If co_present_count >= 3 and no other cue points to exactly one person, leave the addressee unknown — never force a guess.
- The addressee is the person the words are spoken TO, not a person or thing the words are ABOUT. In "You had better let the dog alone," the addressee is the listener being warned, not "the dog". Never record a non-person (animal, object) as an addressee: if the only surface available is such a thing, resolve the addressee from turn-taking / scene participants instead, or leave it unknown.
```

### 2.2 Replace the current WH-sourced examples — COPY VERBATIM
The current B2 `Required JSON shape` uses WH ch4 material ("wife" -> Mrs Earnshaw, Hindley,
Heathcliff). Replace the example objects with these INVENTED, book-neutral ones so the prompt is not
tuned to any target book:

```
  "speaker_turns": [
    {"turn_id": "t_bk_ch01_b006_01",
     "speaker": {"surface": "the innkeeper", "reference_kind": "person", "resolution_status": "named", "candidate_entity_ids": [], "attribution_method": "explicit_tag", "confidence": "high"},
     "addressee": {"surface": "sir", "reference_kind": "person", "resolution_status": "candidate", "candidate_entity_ids": ["ent_ravel"], "attribution_method": "turn_alternation", "confidence": "medium"},
     "utterance_quote": "And what brings you so far north, sir?",
     "address_term_used": "sir", "register_cue": "neutral", "utterance_gist": "asks the traveller his business", "block_id": "bk_ch01_b006"}
  ],
  "relation_events": [
    {"event_id": "e_bk_ch01_b006_01",
     "actor": {"surface": "the innkeeper", "reference_kind": "person", "resolution_status": "named", "candidate_entity_ids": [], "attribution_method": "explicit_tag", "confidence": "high"},
     "target": {"surface": "sir", "reference_kind": "person", "resolution_status": "candidate", "candidate_entity_ids": ["ent_ravel"], "attribution_method": "turn_alternation", "confidence": "medium"},
     "event_type": "questions", "evidence_quote": "what brings you so far north", "block_id": "bk_ch01_b006"}
  ]
```

(Optionally mirror the same neutral names into the B1/B3 example shapes if they also carry WH names —
CodeX: grep the prompt module for `Earnshaw|Hindley|Heathcliff|Nelly` and report every example hit
so Claude can supply a neutral replacement. Do NOT invent replacements yourself.)

---

## 3. Injection plumbing (CodeX)

- Run order per chapter: **B0 -> B1 -> B2 -> B3 -> (B4 at consolidation).**
- Inject B0 output into B1/B2/B3 windows as a `CHAPTER_BRIEF` section: render `cast_on_stage`
  (surface | role_hint) and `scenes_party_size` (block_range | co_present_count | participants).
  B2's new rules reference these fields by name, so the rendering must keep those field names.
- Keep the existing `CHAPTER_ROSTER_ON_STAGE`, previous/next tail context, and the **last-K
  neighbor_summaries** (§0, pilot K=2), each as its own labelled block. B0 augments; it does not
  replace them.
- Enforce the bounded carry from §0 (≤K summaries; size flat in N). Add the bounded-size test.

---

## 4. Generality verification (this is the acceptance bar — run BOTH books, same prompts)

Run the identical B0+B1+B2 prompts, with NO per-book changes, on:
- **Wuthering Heights ch1** (target), and
- **one The Great Gatsby chapter** (control — different narrator, era, register).

Pass conditions:
1. **WH ch1 fixes**: the 3 "sir" turns (b005, b006, b027) and the "dog" turn (b018) now resolve
   addressee as `candidate` via `turn_alternation`; the 2 narration/thought turns (b009 interior
   reflection, b011 narration-to-reader) STAY `unknown` (correct — no in-scene addressee).
2. **Gatsby runs clean**: the same prompts produce a valid brief + turns; vocatives resolve
   sensibly; NO book-specific prompt or code was needed. If only WH improves, the prompt is overfit
   — reject.
3. **Bounded memory**: chapter-N injected context holds AT MOST K neighbor summaries (pilot K=2), not
   N, and its size is flat in N (proven by the ch3-vs-synthetic-ch12 test). Facts come from the
   filtered registry_pack, bounded by scene size.
4. **No leak**: no B0 output contains a relationship-verdict token; `leak_tokens_dropped` visible in
   the report.
5. **No hardcode**: `grep` of the prompt module and pipeline shows zero target-book character names
   in rules/logic (canary test-oracle + universal honorific/leak lists are the only allowed lists).

## 5. Claude gate (never delegated)
Claude independently re-runs on-disk artifacts for BOTH books and reads the raw turns, not the report
counts: confirms the 4 WH turns resolved + 2 stayed unknown, Gatsby parity, bounded size, zero leak,
zero hardcode. Only then does M4 open.

## 6. Cost
B0 = one light pass per chapter (cast+setting+premise, small output). Pilot scope = WH ch1 + 1
Gatsby chapter. Estimate/confirm-usd gate as in M1/M2 before any spend.
