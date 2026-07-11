# §E9 AUDIT — Claude independent pass v1 (2026-07-12, 0-API, artifacts at 7d25fa3/911cfad)

Method: mechanical Python sweep over the full lineage (script in session scratchpad `e9_audit.py`), human adjudication only on semantically ambiguous rows. Independent of Sol's parallel pass — no Sol audit output was read.

## 1. COVERAGE MANIFEST (submitted first, per contract; formula C5: coverage = (found_verified + recovered_verified) / expected)

| Stage | expected | found_on_disk | recovered_from_cache | missing | not_auditable | coverage | exhaustive? |
|---|---|---|---|---|---|---|---|
| M1 briefs (B0) | 4 | 4 | 0 | 0 | 0 | 100% | YES |
| M1 lexicon windows (B1) | 49 | 49 | 0 | 0 | 0 | 100% | YES |
| M1 narrative windows (B2) | 49 | 49 | 0 | 0 | 0 | 100% | YES |
| M1 extracted items (retained): mentions 257, glossary 80, turns 203, events 163 | — | all itemized | — | 0 | 0 | 100% | YES |
| M1 validator-DROPPED items | 130 (counters: pronoun 27, outside_window_neighbor 37, nonperson_event 51, seed_skipped_cast 15) | counters only | 0 | 0 | **130 (item-level)** | count-level only | **NO** — drops are counted, not itemized; per-item content unrecoverable from reports |
| M2 digests (B3) | 4 | 4 | 0 | 0 | 0 | 100% | YES |
| M3 v2 final logical calls (raw) | 10 | 10 (all sha256+cache_key stamped in checkpoints) | 10/10 cache rows exist | 0 | 0 | 100% | YES |
| M3 v2 historical attempts (pre-append-only era) | unknown (≥1 destroyed: ch1 phase attempt_01 empty-payload, M4d:471) | 28 attempt/resume files | 0 verified (cache request_json LACKS max_output_tokens/verbosity → full C2 fingerprint unverifiable) | ≥1 | ≥1, true count unknown | <100% | **NO** — per C2, bare cache hits don't verify config; early attempts not_auditable |
| M3 state rows (ch4 cumulative): 42 T2 entities, 139 aliases, 258 atoms, 21 phases/17 pairs, 4 facts, 203 turns, 163 events, 21 address, 1 blocked, 20 review_only, 5 frames | — | all on disk | — | 0 | 0 | 100% | YES |
| rendered_pack_lines | 0 produced (no bible-v2 pack renderer wired yet — Translator never consumed bible v2) | — | — | — | stage not exercisable | N/A | **N/A** — disclosure defects assessed on renderable rows (status published, not blocked) instead |

**Coverage verdict:** state/bible/final-call layers are fully auditable (100%). The audit is NOT exhaustive for (a) item-level content of M1 validator drops, (b) pre-fix historical raw attempts. Both gaps are historical-forensics only — they do not affect the defect sweep over what the pipeline actually published.

## 2. DEFECT TABLE (20 rows; root-cause enum per §E9 contract)

| # | artifact_ref | wrong_value | expected | root_cause | mechanism evidence |
|---|---|---|---|---|---|
| 1 | ch1 T2 `ent_madam` (b019) | person/resolved | nonperson (dog) | context_packaging | quote_context slice starts mid-sentence, cuts "the ruffianly bitch…" (full b019 contains it); B0 cast row `the canine mother|dog` (b017) never joined to identity card |
| 2 | ch4 T2 `ent_the_mistress` (3 atoms) | one entity | Catherine(b004) ≠ Mrs. Earnshaw(b037/b040) | hint_bias + context_packaging | all atoms carry future-derived `hint_entity_id: ent_mrs_earnshaw` minted under whole-chapter brief |
| 3 | ch4 T2 `ent_the_master` (13 atoms) | one entity | Edgar(b004) ≠ old Earnshaw(b036+) | hint_bias + retrieval_sharding | b004 atom hint `ent_mr_earnshaw` (wrong, future-derived); b036+ genuinely Earnshaw-era; alias interval anchors at the WRONG occurrence (b004) |
| 4 | ch4 T2 `ent_the_master_5c39e4f4a3` (1 atom, uncertain) | duplicate 2nd "the master" entity | merge-or-adjudicate with #3's correct partition | consolidation_code | hash-suffix mint on canonical-key collision instead of adjudication — NEW (mechanical sweep) |
| 5 | ch4 T2 `ent_my_son` (3) + `ent_my_son_b8bccd0d2e` (1) | two entities, same surface | adjudicated identity/split by referent | consolidation_code | same hash-suffix mint pattern — NEW |
| 6 | ch3 T2 `ent_my_human_fixture` (2 atoms, uncertain) | fragment entity | = Nelly Dean (`ent_mrs_dean` exists from ch4) | retrieval_sharding | descriptor surface, zero shared key with any Nelly card; hint was `ent_zillah` (wrong) — NEW |
| 7 | T2 `ent_jabez_branderham` + `ent_the_reverend_jabez_branderham` | two entities | one person | retrieval_sharding | known: no exact key overlap between surface forms |
| 8 | ch3 T2 `ent_my_chamber_door` | referent_kind=place entity minted from character_mention stream | never a character entity | schema_contract | B1 rule "mention MUST refer to a person" violated; validator did not enforce → junk flowed to T2 — NEW |
| 9 | ch4 T2 `ent_the_late_mrs_linton_s` | canonical carries possessive "'s" | clean surface | consolidation_code | canonical selection keeps raw possessive form — NEW minor |
| 10 | ch1 T2 `ent_the_villain` | referent_kind=unknown but status=resolved | resolved requires a kind | schema_contract | status/kind contract never asserted — NEW minor |
| 11 | facts `servant_of`/`master_of` (all 4 bibles) | inference rendered as fact | derived (command "Joseph, take…" ≠ statement) | schema_contract (no explicit/derived axis in v1) | predicate_note self-admits "indicating"; rev9 §P7/§P8 is the fix |
| 12 | ch3 `narration_frame_segments` = single frame_present b002–b067 | flat | diary/dream segments distinct | frame_error | known; M2/B3 granularity; drives §5-F'/F''' |
| 13 | ch4 blocked pair (mr_heathcliff, the_master) | "take it home with me" vs source "…with him at once" | verbatim | model_slip (contained) | validator caught; §5.4 quarantine worked as designed |
| 14 | 2 phase rows: `valid_until_block == next.valid_from_block` (joseph/lockwood b021; heathcliff/lockwood b047) | boundary-touch, inclusive/exclusive undefined | disjoint or defined semantics | schema_contract | mechanical overlap sweep — NEW; Canonical v1 must define interval semantics |
| 15 | M1 endpoints: 326/326 lack any atom linkage; final T3: 57% `candidate`, 10–16% `unknown` | bindings rest on unadjudicated candidate ids | witness-bearing bindings | provenance_loss (systemic) | quantifies rev9 §4-E'/§W7 necessity — NEW quantification |
| 16 | 14 atoms carry `hint_entity_id` referencing nonexistent entities (ent_heathcliff 6, ent_mr_earnshaw 5, ent_catherine 2, ent_the_young_man 1) | dangling hint namespace | lineage-resolvable refs | provenance_loss | hint namespace ≠ final namespace; breaks mechanical lineage — NEW minor |
| 17 | address_policies: 21/21 `runtime_usable:false`, observed_terms mostly [] | (by locked design, fail-closed) | — | not a defect; quantifies §M6 | address-channel pack support = 0% today — baseline for missing-support gate |
| 18 | 3 atoms with WRONG hints correctly overridden (her master→hint hindley→bound heathcliff; the young lady / your amiable lady→hint zillah→bound mrs_heathcliff) | — | — | hint_bias (resisted) | evidence that local evidence CAN beat bad hints — useful prior for the A/B arms |
| 19 | fact channel: 31 M2 fact claims → 4 unique T3 facts | severe funnel narrowing | uncertain (may be correct conservatism) | uncertain — flagged for joint review | needs Sol cross-check; if real recall loss → context_packaging |
| 20 | historical: raw overwrite (fixed) + cache key omits cap/verbosity | — | — | provenance_loss + schema_contract | both already have locked fixes (append-only; full-request fingerprint) |

## 3. SPEC-COVERAGE CHECK (does rev1–10 cover every observed class?)

- Mixing (2,3) → §O4/O6/W7-8 adjudication + §D4-6 disclosure + §Q7-8 dispute. ✔
- Fragments/duplicates (4,5,6,7) → global roster + two-step adjudication + §D8 signatures (duplicate-canonical collision → adjudication, never hash-suffix mint; **Canonical v1 must state this explicitly** — it is implied but not written). ✔ (with wording TODO)
- Junk extraction (8) + kind contract (10) → §D8 referent_kind agreement + §M6 gates; **Canonical v1: add validator assertion person-rule at B1 and kind-required-for-resolved**. ✔ (contract TODO)
- explicit/derived (11) → §P7/P8. ✔  Frames (12) → §5-F'/F'''. ✔  Model slip (13) → quarantine doctrine. ✔
- Interval semantics (14) → **Canonical v1 TODO: define closed-open [from, until) convention**. ✔ (contract TODO)
- Endpoint/witness (15) → §4-E'/§W7-8. ✔  Dangling hints (16) → §W6/W8 barred-from-witness + lineage stamps. ✔
- Missing-support baselines (17,19) → §M6 denominators. ✔
- **other_new_class: NONE** — every defect maps to the existing enum; no spec reopen triggered. Three wording TODOs go into Canonical v1 (duplicate-collision rule, B1 person-rule validator, interval convention).

## 4. Reconciliation ask (vs Sol's parallel audit)

Rows to cross-check hardest: #4/#5 (hash-suffix duplicates — did Sol find them?), #19 (fact funnel 31→4 — real loss or correct conservatism?), #14 (interval semantics), and any row Sol found that this pass missed.
