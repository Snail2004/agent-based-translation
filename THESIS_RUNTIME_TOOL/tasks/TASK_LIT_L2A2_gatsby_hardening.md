# TASK_LIT_L2A2b — Gatsby-probe hardening (make operational clean-pass, no overfit)

Status: SPEC. Follows the Gatsby ch1 generality probe. Do NOT open M4 until Gatsby ch1 M1 is clean
(hard-fail mode) AND WH ch1 does not regress.

## Result framing (adopted, use verbatim in reports)
- **prompt semantic generality: PASS** — the neutral prompt reads Gatsby correctly (B0 cast
  Nick/Tom/Daisy/Jordan/Gatsby; setting West/East Egg; scene_shape few_scenes; party_size 1→4→2;
  the two resolution rules fire on Gatsby dialogue; phase_leak=0). NOT overfit to WH.
- **operational clean-pass: INCOMPLETE** — 5/24 windows failed on GENERAL robustness (not book
  specificity). This task closes them.

## The 5 failures and their fix owner

| Window | Error | Class | Owner |
|---|---|---|---|
| lex 001 | extracted glossary/mentions from CONTEXT_ONLY tail (b005/b006) | window discipline | Claude prompt (DONE) + validator keeps hard-fail |
| lex 003 | coined `char_nick_father` | invent-id | Claude prompt (DONE) + B0 seed |
| nar 006 | `candidate` with empty candidate_entity_ids | cold-start ledger | B0 seed (CodeX) + Claude prompt (DONE) |
| nar 015 | cited `ent_nick` not in ledger | cold-start ledger | B0 seed (CodeX) |
| nar 022 | `attribution_method:"candidate"` (enum) | model field slip | Claude prompt (DONE) + validator normalize |

## Claude prompt reinforcements — ALREADY APPLIED to design/LITERARY_PROMPT_DESIGN.md
(verified via the real loader; all 4 prompts still parse and are WH-name-free)
- B1 (literary_lexicon_v1): CONTEXT_ONLY tail is READ-ONLY — never extract items or cite block_ids
  from it; output only from the ACTIVE window. If no REGISTRY_CONTEXT_PACK entry matches, leave
  candidate_entity_ids [] and use named/unknown — never coin `char_<name>`/`ent_<name>`.
- B2 (literary_narrative_v1): candidate REQUIRES a non-empty id copied from CHAPTER_ROSTER_ON_STAGE;
  if the roster has no id (incl. the first-person narrator before being named on the page) use
  `unknown` with [] not `candidate`; never invent. `attribution_method` is HOW you attributed and is
  NEVER a resolution_status value (no named/candidate/unknown in that field).

## CodeX work

### 1. Seed the known-entity ledger from the B0 chapter brief (the cold-start fix — highest value)
Before running B1/B2 for a chapter, mint entity ids from B0 `cast_on_stage`, but ONLY for NAMED
cast (proper names). This gives the first-person narrator ("I"→Nick) and named characters an id that
B1/B2 can cite, closing nar006/nar015/lex003.

**PROVENANCE — HARD RULES (do not fabricate position):**
- Seed only cast entries whose surface is a PROPER NAME (Nick, Tom Buchanan, Daisy, Jordan Baker,
  Gatsby, Miss Baker). Do NOT seed descriptors ("his father", "the young man", "the butler") as
  stable entities.
- A seeded ledger row is:
  ```
  entity_id: ent_<slug>
  source: chapter_brief_cast
  evidence_scope: chapter_level
  surface_evidence_block: null   # fill ONLY when a real ACTIVE-window block containing the surface is found
  ```
- B0's `first_seen_block` is B0's chapter-level INFERENCE, NOT a verified surface location — never
  copy it into an alias `valid_from_block`. A seeded id may be CITED by B1/B2 as a candidate, but it
  gets NO alias/`valid_from` interval until a real surface occurrence is seen in an active window.
  (Keeps the interval-valued memory discipline: positions must have real evidence.)

### 2. Validator: normalize the enum slip (nar022)
If `attribution_method` carries a non-enum value that is actually a resolution_status
(named/candidate/unknown), do NOT fail the whole window — drop the offending event/turn + count it
(e.g. `attribution_enum_dropped`), keep the rest. Rationale: it is a stray model slip, not a window
break. (Context-only leak and invent-id stay HARD-FAIL during the re-proof — see sequencing.)

## Sequencing discipline (do not let drop-and-count mask the fix)
Re-run in HARD-FAIL mode (except the narrow enum-normalize above). We must SEE that seeding + prompt
reinforcement actually make the windows pass, not hide failures behind row-drops. Only after Gatsby
is clean do we discuss drop-and-count as a production policy.

## Re-run acceptance (Claude gates on real artifacts, both books)
1. **Gatsby ch1 M1 clean**: 0 final-window failures from the 5 classes above (or only intentional,
   counted enum-drops). "I"→Nick resolves as candidate citing the seeded `ent_nick`. No invented ids.
   No context-only citations.
2. **WH ch1 no regression**: re-run WH ch1 M1; the 4 resolved turns (sir×3, dog) stay resolved, the 2
   narration turns stay unknown/omitted, no new failures introduced by the seed.
3. **Seed provenance clean**: no seeded id carries a `valid_from_block`/surface it did not earn from a
   real active-window occurrence.
4. phase_leak=0, brief_leak=0, frozen D2L hash unchanged, focused tests green.

## B4 watch-item (record, does NOT block M1)
B0 lists both "Miss Baker" and "Jordan Baker" = the same person → seeding mints two ids
(ent_miss_baker, ent_jordan_baker). Honorific-strip will NOT auto-merge them. M3/B4 identity
consolidation must merge this alias pair. Flag now, resolve at M3.

## Review tightenings (Claude ratifies CodeX's review; adds one anti-hardcode guard)

CodeX's review is accepted in full — none of it is result-cosmetics; several points actively guard
AGAINST masking. Deltas to apply:

1. **[DONE, Claude] B1 example no longer coins an id.** The `["char_alden"]` candidate example
   (which contradicted the new "never coin char_<name>" rule and could reproduce char_nick_father)
   is changed to the SAFE fallback `resolution_status:"unknown", candidate_entity_ids:[]`. Verified:
   grep of all four extracted prompts shows zero `char_*` coined ids.

2. **[Claude — the key anti-hardcode guard] The name-vs-descriptor judgment for seeding belongs to
   B0 (the LLM), NOT to a code heuristic or a name list.** Add one field to
   `literary_chapter_brief_v1`: each `cast_on_stage` entry gets `surface_kind` ∈ {proper_name,
   descriptor} (the LLM observes whether the surface is a real name like "Nick"/"Tom Buchanan" vs a
   descriptor like "his father"/"the butler"). The seed code then mechanically seeds ONLY
   `surface_kind == "proper_name"`. HARD PROHIBITION: the seed must NOT contain an enumerated cast
   list, a per-book name set, or a code-side "is this a proper name" language heuristic — that would
   be exactly the book-specific / code-does-language-work hardcode we removed from consolidation.
   Code stays mechanical (filter on the LLM's flag); judgment stays in B0.

3. **[CodeX] Seeded id is citable but NOT an alias — make it mechanically explicit.** The seeded id
   MUST be added to the known-entity set the validator checks AND rendered into
   CHAPTER_ROSTER_ON_STAGE / REGISTRY_CONTEXT_PACK so B1/B2 can cite it — otherwise the validator
   still rejects `ent_nick`. It must NOT create an alias or `valid_from_block`; do not copy B0's
   inferred `first_seen_block`. Two opposite failures to avoid: (a) id not in the known set →
   still rejected; (b) code "fixes" by minting a fake alias at B0's inferred block → fabricated
   position.

4. **[CodeX] Seed transparency (anti-cosmetic).** The report MUST list `seeded_cast` and
   `seed_skipped_cast` with a reason per entry (e.g. skipped: surface_kind=descriptor). Reviewer
   must be able to see who was seeded and who was skipped — no silent seeding of descriptors.

5. **[CodeX] Enum-normalize stays NARROW.** Only the three resolution_status strings
   (named/candidate/unknown) misplaced into attribution_method are drop-and-counted. ANY other
   out-of-enum value still HARD-FAILS — do not let the normalize become a catch-all that hides a new
   prompt error.

6. **[CodeX] Cap the accepted drops.** Gatsby "clean" allows ONLY the nar022-class enum-drops, each
   listed per window/turn in the report. It does NOT permit dropping a spread of new failures to
   force a pass. If any non-enum failure remains, Gatsby is NOT clean.

7. **[CodeX] Fresh output dirs.** Re-run into NEW dirs (e.g. `literary_l2a2_gatsby_ch1_hardened`,
   `literary_l2a2_wh_ch1_hardened`) so fixed artifacts are never mixed with the old failing ones
   (stale-artifact hygiene).

## v2 gate result (Claude, 2026-07-09) — WH PASS; Gatsby REJECTED as evidence; v3 required

**WH ch1 v2: PASS, no regression.** sir b005/b006 resolved both directions, dog b018 → Mr. Lockwood,
extra coverage gained (b012/b018/b021 addressees), 7/7 + 7/7 windows, phase_leak=0, enum_dropped=0.
Seeds Mr. Heathcliff + Joseph with clean provenance (surface_evidence_block=null, skip report honest).

**CodeX self-fixes: ACCEPTED (verified in source).**
- (a) `prompt.replace("bk_ch01", chapter_id)` — mechanical block-id render, book-neutral, keeps the
  design-doc-is-live-prompt invariant modulo a mechanical substitution. OK.
- (b) Seed filter = B0 `surface_kind` flag + first_seen_block-in-chapter + dedup, with transparent
  seeded_cast/seed_skipped_cast. No name lists, no code-side language heuristic. OK.
- (c) NOT normalizing `ent_unknown` was the RIGHT call — that is the invent-id class under test.

**Gatsby ch1 v2: REJECTED as generality evidence — two independent reasons.**
1. **CONTAMINATION (gate blocker, found by Claude diff vs last-verified prompt state).** Unreported
   CodeX prompt edits placed Gatsby ch1 cast surfaces INSIDE two shipped blockquotes:
   - B0 surface_kind rule: `("Nick", "Tom Buchanan", "Miss Baker")` / `("his father", "the butler",
     "the young man")` — literally the probe chapter's seeding answers.
   - B1 descriptor-examples line: `"my father", "her host", "the butler", "the young man", "the
     nurse"` — all five lifted from gg_ch01 text.
   Effect: B0's proper_name/descriptor judgment (the input to seeding) was handed the answer key =
   tuning-on-test. The v2 Gatsby seeded_cast cannot prove prompt generality, regardless of pass rate.
   **FIXED by Claude**: neutral-world replacements (Mira / Mr. Alden / Miss Rook; his aunt / the
   innkeeper / the coachman / the old gardener). Verified via the real loader: all 4 prompts CLEAN of
   BOTH corpora (WH + Gatsby token sets); the ban-grep set now permanently includes Gatsby tokens.
2. **Remaining hard-fail wb_gg_ch01_001** (placeholder `ent_unknown` for he/my father, both attempts).
   **FIXED at prompt level by Claude**: B1 + B2 now state "There is NO placeholder id
   (ent_unknown/ent_unnamed/ent_narrator); 'I do not know who this is' is expressed ONLY as
   resolution_status=unknown with []". 26/26 tests still pass.

**Process rule reaffirmed:** CodeX must report EVERY blockquote edit in the design doc (division of
labor: Claude owns prompts; CodeX may patch on run errors but always reports back). Mechanical code
renders are fine; silent prompt-content edits are not — this one invalidated the run it enabled.

**v3 re-run (CodeX): fresh dirs `literary_l2a2_gatsby_ch1_hardened_v3` + `literary_l2a2_wh_ch1_hardened_v3`.**
Hard-fail mode unchanged. Acceptance = previous list PLUS: zero placeholder ids anywhere; B0 Gatsby
must make the proper_name/descriptor call WITHOUT the answers in the prompt (watch whether it still
seeds Tom Buchanan/Daisy/Gatsby/Jordan Baker on its own — that is the actual generality datapoint).

**Non-blocking watch items → M3:**
- Possessive/compound surfaces minted as ids (ent_daisy_s, ent_tom_s, ent_tom_and_daisy,
  ent_the_tom_buchanans): consider a universal mechanical possessive-strip ('s) + no-conjunction rule
  at consolidation. Mechanical, book-neutral — allowed kind.
- First-person-narrator rule did not fire on EITHER book (both briefs kept "I" descriptor even though
  Nick/Lockwood are named on-page). Consequence is only honest unknowns in early windows; monitor at
  M4 scale before escalating.
- attribution_enum_dropped=19 on Gatsby (allowed class, counted per window) — high; monitor.

## v3 gate result (Claude, 2026-07-09) — PASS. M4 UNLOCKED.

Independently verified on real v3 artifacts (not the report numbers):
- **Prompt**: diff vs my last-verified copy = NO change (CodeX did not re-edit). Loader extraction of all
  4 prompts CLEAN of BOTH corpora (WH + Gatsby ban set).
- **Placeholder/invented ids**: walked every `candidate_entity_ids` in all lexicon+narrative parsed
  outputs — Gatsby 203 cited ids, WH 45 — ZERO placeholder (ent_unknown/unnamed/narrator), ZERO coined
  (char_*), ZERO ids outside the ledger. The old hard-fail wb_gg_ch01_001 is now clean (0 turns/events,
  no placeholder).
- **Generality datapoint (the real test)**: with the answer key REMOVED from the prompt, B0 Gatsby
  self-classified surface_kind correctly — proper_name: Nick Carraway, Tom Buchanan, Daisy Buchanan,
  Jordan Baker, Gatsby; descriptor: "I", Miss Baker. Seeded exactly the 5 named; skipped the 2
  descriptors. This is prompt generality shown, not tuning-on-test.
- **Cold-start closed**: `ent_nick_carraway` is now seeded → "he/my father" no longer needs a
  placeholder → the window that failed v1/v2 passes.
- **WH no-regression (improved)**: sir resolves correctly both directions in the 2-person scenes
  (b005→Heathcliff, b006→Lockwood, b022→Heathcliff, b027→Lockwood); Joseph/Mr. Heathcliff/Mr. Lockwood
  named; seed provenance clean (surface_evidence_block=null; honest skips: I, the canine mother, the
  lusty dame). 7/7 + 7/7.
- phase_leak=0 both; frozen d2l hash 64D98965…B555C715 unchanged; 26/26 tests.

**CodeX's raw-grep caveat is correct and handled**: `ent_unknown` appears in the raw PROMPT (the
prohibition text) but NOT in any parsed output — I scanned parsed_json only.

### Carry into M3/M4 (non-blocking watch items)
- Identity merge still owed at B4: Miss Baker = Jordan Baker (v3 avoided double-seeding by classing
  "Miss Baker" descriptor, but the alias union is a B4 job); possessive/compound id hygiene
  (ent_*_s / ent_*_and_*) via a universal mechanical strip.
- First-person-narrator rule only half-fired on Gatsby (kept a redundant "I" row alongside "Nick
  Carraway"); harmless now (I skipped, Nick seeded) but watch at M4 scale.
- Gatsby attribution_enum_dropped=17, nonperson_dropped=25 — allowed classes, counted per window;
  monitor for prompt drift at scale, not a blocker.

**Verdict: both books clean; M4 (ch2–ch4 consolidation → partial Story Bible) may open.**
