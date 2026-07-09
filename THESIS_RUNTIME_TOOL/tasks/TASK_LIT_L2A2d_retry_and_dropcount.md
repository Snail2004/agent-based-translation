# TASK_LIT_L2A2d — Validator drop-and-count + edit-based retry (stop retries destroying evidence)

Status: SPEC. Config stays 500/8 (locked). This task does NOT change window size. It removes needless
whole-response REGENERATION, which we proved is lossy (a retry regenerated 11 events → 9, final kept 8).
Goal: fewer retries, and when a retry is truly needed it EDITS instead of re-deriving, so evidence is
not dropped. Do NOT commit. Do NOT open/advance M4 on this.

## Evidence this is real (verified by Claude on the 1500 artifacts, mechanism is config-independent)
- `_call_json_validated` retries by REGENERATING the whole JSON, feeding back only the first 4000
  chars of the bad output. For long responses the tail is lost from the repair context.
- `wb_wh_ch01_002`: attempt-1 = 11 turns / 11 events but ok=False (schema violation); attempt-2
  regenerated to 9 events; final kept 8 → 3 real events lost to a MECHANICALLY-FIXABLE schema nit.
- 3 of 5 retries in the 1500 run were the B1 pronoun contradiction (now fixed at the prompt level,
  see item 0); the other 2 were the B2 `named`+`candidate_entity_ids` contradiction.

## 0. Prompt fix — ALREADY DONE by Claude (for CodeX's awareness; do NOT re-edit)
`design/LITERARY_PROMPT_DESIGN.md` B1 (literary_lexicon_v1): the self-contradiction is removed.
- line ~186: "record every NON-PRONOMINAL surface … A bare pronoun (he/she/I/they…) is NOT a mention
  surface here … skip it entirely; resolved later in the narrative pass."
- line ~193: "Use candidate when the surface is a descriptor or title (never a bare pronoun — pronouns
  are not extracted in this pass at all) …".
Verified: 4/4 prompts load clean of both corpora; 26 tests pass. If you must touch a prompt, REPORT it
(division of labor); do not silently edit.

## 1. B1 validator: pronoun mention → DROP + COUNT, never fail/retry the window
A `character_mention` whose normalized surface is a plain pronoun (closed set:
i,me,my,mine,you,your,yours,he,him,his,she,her,hers,we,us,our,ours,they,them,their,theirs — the
ALLOWED kind of universal wordlist, not book-specific) is dropped as an ENTRY and counted
`pronoun_dropped`. The window stays ok=True if nothing else is wrong. This aligns with the existing
drop-and-count design (§1) — the current retry-on-pronoun VIOLATES it. Do NOT regenerate the response
for this.

## 2. B2 validator: `named` + non-empty `candidate_entity_ids` → mechanical normalize in place, count
Contradiction: schema says `named ⇒ ids empty`. Fix the offending SLOT only, keep the item and every
sibling turn/event, count it, do NOT regenerate. Two mechanical branches (no language judgment):
- surface is a plain pronoun (closed set above) → it cannot be `named`: set
  `resolution_status="candidate"`, KEEP the ids if they are valid roster/pack ids (else `unknown`,[]).
  Count `named_pronoun_downgraded`.
- surface is NOT a pronoun → enforce the contract literally: clear `candidate_entity_ids=[]`, keep
  `named`. (Lossless: a named surface is re-linked by name at consolidation via honorific-strip.)
  Count `named_ids_cleared`.
Any id that is not in roster/pack still HARD-FAILS separately (unchanged) — this normalize is only for
the status/ids CONTRADICTION, not for invalid ids.

## 3. Retry engine: when a retry IS still needed, make it EDIT-based, not REGENERATE-based
For any residual genuine failure (parse error, or a validation error not covered by 1–2):
- feed back the FULL prior assistant output (remove the 4000-char truncation, or raise it well above
  the largest observed response), plus the specific errors, plus this instruction:
  "Return the SAME items you already produced. Correct ONLY the fields named in the errors. Do NOT
  drop, merge, or add any turn / event / mention." 
- Keep the single-retry cap.
Rationale: the failure mode we measured is the model re-deriving a shorter answer. Editing preserves
item count.

## Sequencing / anti-cosmetic guards
- The mechanical treatments in 1–2 are NARROW: ONLY the pronoun-mention and the named/ids
  contradiction. ANY other validation error keeps its current behavior (hard-fail / retry) — do not
  let drop-and-count become a catch-all that hides a new prompt bug.
- All new counters (`pronoun_dropped`, `named_pronoun_downgraded`, `named_ids_cleared`) MUST appear in
  the m1_report validation_counts and per-window counts, so the reviewer SEES what was auto-fixed —
  no silent swallowing. A high count is a signal to inspect, not to celebrate.
- Keep the 26 tests green; add a unit test for each of 1, 2 (both branches), 3.

## Re-run to MEASURE (fresh dirs, 500/8, hard-fail otherwise)
- WH ch1 → `data/reports/literary_l2a2_wh_ch1_retryfix/`  (required — this is the analyzed baseline)
- Gatsby ch1 → `data/reports/literary_l2a2_gatsby_ch1_retryfix/`  (recommended — exercises narrator "I")
Report per run: calls, retries, cost, the three new counters, lexicon_ok/failed, narrative_ok/failed,
phase_leak, parse_fail. Keep 500/8 (do NOT change window caps here).

## Claude gate (on real artifacts, vs the v3 500 baseline, via compare_windows.py)
1. Retries DOWN (expect the 3 pronoun retries → 0; the 2 named/ids retries → 0, handled mechanically).
2. Recall NOT worse than v3 500 baseline on all three axes (mentions/turns/events, keyed by
   block+surface). Any item that v3 lost to regeneration but this run keeps = a win, report it.
3. No regression: WH sir×3 + dog(b018) stay resolved; Gatsby ent_nick_carraway + seeds intact; zero
   placeholder ids; zero coined ids; phase_leak=0; frozen d2l hash 64D98965…B555C715 unchanged.
4. Verify item-preservation directly: for any window that still needed a retry, attempt-2 item count
   >= attempt-1 (edit-based repair must never shrink the set on a passing repair).

## v1 gate (Claude, 2026-07-09): retry-fix WORKS, but gate BLOCKED by a pre-existing bug — one more fix needed

Verified on `literary_l2a2_wh_ch1_retryfix` vs v3 500 baseline (compare_windows.py):
- Retry-fix mechanics PASS: retries 5→2, cost −9%, edit-based repair preserved item counts
  (B1 6→6, B2 2t/5e→2t/5e), named_ids_cleared=14 + pronoun_dropped=3 worked. Good.
- Gate BLOCKED: turns 18→14. ROOT CAUSE (verified, NOT variance): window 007 attempt-1 PASSED with 6
  turns; the validator then DROPPED 4 real dialogue turns (b023–b026) because ONE secondary field —
  `attribution_method` — held "unknown" (a resolution_status value) in the speaker OR addressee slot.
  The load-bearing fields (surface/status/candidate_ids/quote) were all fine. attribution_enum_dropped=7
  (4 turns + 3 events) = the bulk of the recall regression. This is the SAME drop-vs-normalize mistake
  we fixed for named+ids, not yet applied to attribution_method. (My earlier gatsby-hardening spec said
  "drop the offending turn/event" for this class — that was wrong.)

### FIX 4 (add): attribution_method enum slip → NORMALIZE the field, KEEP the item, count
When `attribution_method` (in speaker/addressee/actor/target) holds a resolution_status string
(named|candidate|unknown), do NOT drop the turn/event. Replace ONLY that field with a neutral honest
sentinel (null or a new "unspecified" enum member — do NOT fabricate a specific method like
"nearby_context", which would invent a trust signal), keep every other field and the item itself,
count `attribution_enum_normalized`. Same NARROW guard: only the three resolution_status strings get
this treatment; any OTHER out-of-enum value in attribution_method still hard-fails. Retire the
drop-whole-item behavior (`attribution_enum_dropped`) for this class.
Expected effect: recovers the 4 turns + 3 events → turn recall returns to ~baseline; gate should pass.
Prompt already warns against this ("attribution_method is NEVER a resolution_status value") and the
model still slips, so a validator normalize — not more prompt text — is the robust fix.

### Re-run: same dir policy, `literary_l2a2_wh_ch1_retryfix2` (fresh). Claude re-gates with the same 4
criteria; add: attribution_enum_normalized replaces attribution_enum_dropped, turns ≈ baseline (18).
