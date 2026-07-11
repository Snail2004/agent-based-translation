# §E9 defect reconciliation — Claude (20 rows, 2589c24) × Sol (29 rows), 2026-07-12

Both passes independent (Claude committed before receiving Sol's). Joint verdict: **other_new_class = NONE in either pass** → no spec reopen; M4f rev1–10 covers every observed root cause. ~34 unique defects; both agree NEEDS-REWORK-before-scale and run-again must reconcile ALL findings, not just the 3 canaries.

## A. Found by BOTH (high confidence → straight into oracle)
| theme | Claude# | Sol# | root_cause |
|---|---|---|---|
| madam = dog, mid-sentence slice + B0 join missed | 1 | 2,10 | context_packaging |
| the_master b004=Edgar vs b036 old Earnshaw (two people) | 3 | 12 | hint_bias |
| the_mistress b004=Catherine vs b037 Mrs Earnshaw | 2 | 13 | hint_bias |
| future-hint leak into early window (b004) | 2,3 | 1 | hint_bias |
| systemic under-merge (Catherine×3, Hindley×3, Jabez×2, Edgar/Heathcliff/Nelly/Zillah fragments) | 6,7 | 18 | retrieval_sharding |
| servant_of/master_of = inference published as fact | 11 | 20 | schema_contract |
| ch3 frame flattened (diary/dream) | 12 | 3 | frame_error |
| him→me phase slip (correctly quarantined) | 13 | 24 | model_slip (contained) |
| endpoints carry no atom link / witness | 15 | 6 | provenance/schema |
| rendered pack 0/4 + classic renderer no disclosure filter | manifest | 23 | schema_contract |
| Heathcliff 61 atoms / ch3 over cap = unbounded dossier | (via §C''') | 29 | retrieval_sharding |
| exact request contexts not persisted, cache capless | recon | 26 | provenance_loss |

## B. Claude-ONLY (Sol did not list → Sol should confirm)
| Claude# | finding | root_cause |
|---|---|---|
| 4,5 | **hash-suffix DUPLICATE entities** `ent_the_master_5c39e4f4a3`, `ent_my_son_b8bccd0d2e` — consolidation mints a NEW entity on canonical-key collision instead of adjudicating | consolidation_code |
| 8 | `ent_my_chamber_door` — a DOOR minted as a person entity; B1 "must refer to a person" rule not enforced by validator | schema_contract |
| 10 | `ent_the_villain` referent_kind=unknown but status=resolved | schema_contract |
| 14 | 2 phase rows with valid_until == next.valid_from (interval inclusive/exclusive undefined) | schema_contract |

## C. Sol-ONLY (Claude missed → VERIFIED by Claude on artifacts just now)
| Sol# | finding | Claude verification | root_cause |
|---|---|---|---|
| 21 | **ch1 landlord_of/tenant_of (Lockwood–Heathcliff) DISAPPEAR in ch2+** — replace_pairs wipes prior rows of a pair even when the new response omits it | CONFIRMED: ch1 facts = [servant,master,landlord,tenant]; ch2/3/4 = [servant,master,father_in_law,daughter_in_law] — landlord/tenant gone | consolidation_code |
| 5 | **244+ person endpoints with exactly ONE candidate but final id = null** — resolve step ignores "candidate already IS the final id" | CONFIRMED: 424 person-ish endpoints, single-candidate-no-final | consolidation_code |
| 11 | `ent_hareton_earnshaw` merges the "1500 / Hareton Earnshaw" inscription (ancestor) with the living Hareton | CONFIRMED: b012 inscription atom + b053 "My name is Hareton Earnshaw" in one entity | schema_contract |
| 16 | `ent_mr_heathcliff` contains "your guest, sir" b046 = Lockwood | CONFIRMED: b046 "It is only your guest, sir, I called out" bound as a Heathcliff atom | context_packaging |
| 7 | b044 validator drops Heathcliff mention + vocatives; next window doesn't recover them | Sol V (M1 counters) | validator_loss |
| 8,9,25 | address_term_used / vocative occurrence errors (b041/b042/b013/b044); 52 turns have address term, only 12 have both endpoints, only 5 surfaced | Sol V | model_slip + consolidation_code |
| 15 | `ent_t_maister` mixes old Earnshaw with "the surly old man" b015 = Joseph | Sol V | context_packaging |
| 17 | group-phrase entities `ent_my_son`/`ent_my_children` mix distinct referent sets | Sol V | schema_contract |
| 22 | 3 phase rows without disposition; address policies emitted for removed pairs | Sol V | provenance_loss |
| 27 | baseline M3 checkpoints do NOT pin validator_contract_version (R2 fix uncommitted) | Sol V | schema_contract |
| 28 | api_format fragility: missing "json"→400; ch3 3 raws cut at exactly 6144; 17 short ids, 6 suffix repairs, 1 fabricated out-of-shard atom | Sol V (counters) | api_format |

## D. Numeric deltas (count basis, not defects)
- #5 endpoint-null: Sol 244 vs Claude 424 (filter breadth) — same direction.
- nonperson_event drop 51 (global counter) vs 52 (Sol per-item) — resolve with per-chapter pointer.

## E. Canonical v1 must add (data-grounded, beyond the generic oracle)
1. **(Sol)** address_term_used / vocative = occurrence-level evidence with its OWN disposition + checker; occurrence errors never silently become derived address hints.
2. **(Sol)** context-only / window drop is recall-safe ONLY after a global coverage check; good evidence must not be lost because the owning window didn't re-extract it (generalizes the b044 loss).
3. **(Claude)** canonical-key collision → adjudication, NEVER a hash-suffix mint (#4,5).
4. **(Claude)** B1 person-rule + kind-required-for-resolved enforced by validator (#8,10).
5. **(Claude)** relation interval convention defined (closed-open [from,until)) (#14).
6. **(Sol #21, elevated)** cross-scope: facts/phases/address for a pair NOT addressed by the new response are RETAINED with disposition — never silently replaced. Generalizes M4d R1 (model_omitted_pair) from phases to ALL relation rows. **This is the highest-severity newly-quantified defect.**

## Status
Coverage manifest (reconciled) + defect tables complete. No other_new_class → proceed to Canonical v1 incorporating E1–E6. Run-again acceptance reconciles all ~34 findings.
