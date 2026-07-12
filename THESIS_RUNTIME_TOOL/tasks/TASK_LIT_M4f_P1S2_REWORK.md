# TASK_LIT_M4f_P1S2_REWORK — close Sol's 6 findings on Builder-v3 Step-2

Status: **DRAFT (Claude → Terra/CodeX), 2026-07-12.** Rework of the merged Step-2 scaffold (commit `a8758f1` on `main`). Sol's independent code review returned **NEEDS-REWORK** (4 BLOCKER + 1 MAJOR + 1 MINOR); Claude reproduced all six on its own probes. Fix-forward on `main` (a new rework commit, no revert — the modules are additive/unwired so the bugs are latent). **Verify gate = Claude, re-run ADVERSARIALLY (probe every contract-table row, not just happy fixtures).**

Contract source unchanged: `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` §8, task `TASK_LIT_M4f_P1S2_schema_validators.md` rev3. Same constraints as Step-2: OFFLINE, no wiring, no prompt/orchestration change, no LLM/network, no frozen-DB access, inputs never mutated, `sk-` scan before commit, branch not `main` for the work then hand back.

**Global rule for every fix below:** each fix MUST ship with a regression probe that **FAILS on the current code and passes after** — a green happy-path fixture is not acceptable proof (that is exactly what let these ship). Enumerate the full table / delete each required field / call every reference path.

---

## BLOCKER-1 — two-axis eligibility table is inverted (builder_validators_v3.py `_endpoint_eligibility` ~:232)

Current bug (reproduced): `unknown+person` and `group+unknown` are flagged `flag_invalid_two_axis`; `narrator+animal` is treated `discourse_only` with no invalid flag. Contract §8 says `unknown` on EITHER axis is a *deferral* (never invalid), and `narrator/reader` are discourse-only ONLY with kind ∈ {person, unknown}.

**Fix — implement this exact ordered table (first match wins):**
1. `scope ∈ {narrator, reader}`: kind ∈ {person, unknown} → `discourse_only`; else → **invalid** (flag_invalid_two_axis, keep).
2. `scope == individual`: kind==person → `eligible`; kind ∈ {animal, nonhuman_character} → `route_out` (captured, NOT invalid); kind==unknown → `deferred` (NOT invalid); kind ∈ {place, object, group_reference} → **invalid** (flag, keep).
3. `scope == group`: kind ∈ {group_reference, unknown} → `route_out`/`deferred` (NOT invalid); else → **invalid** (flag, keep).
4. `scope == unknown`: → `deferred` (any kind, NOT invalid).
Invalid combos are always flag-and-keep (never drop). Enum-invalid scope/kind → route_out as today.

**Mandatory probes (full table):** assert eligibility+flags for at least: `individual+person`→eligible; `individual+animal`→route_out(no invalid); `individual+place`→invalid; `group+group_reference`→route_out; `group+person`→invalid; `group+unknown`→deferred(no invalid); `narrator+person`→discourse_only; `narrator+animal`→invalid; `reader+unknown`→discourse_only-or-deferred(no invalid); `unknown+person`→deferred(no invalid).

## BLOCKER-2 — mid-block frame boundaries are located but ignored by the tree math (~:794 locate, ~:862 math)

Current bug (reproduced): sibling-overlap, child-containment and deepest-leaf use BLOCK indices only, so two siblings in the same block (`[b001,b001]`) at different char offsets falsely overlap and the deepest-leaf is ambiguous per `block_id`.

**Fix:** represent every segment as an interval over `(block_order, char_offset)`: a segment with a mid-block `start_boundary`/`end_boundary` uses the located `start_anchor.char_start` / `end_anchor.char_end`; a segment without one spans the block fully (`char 0 .. len(nfc_block_string)`). Child-containment, sibling-non-overlap and **deepest-active-leaf** must all be computed on these `(block_order, char)` intervals, not a `block_id→segment` map. Deepest-active-leaf at a query point = the innermost segment whose interval contains that `(block_order, char)`.

**Mandatory probes:** two siblings in one block at disjoint char ranges → NO overlap; two siblings in one block at overlapping char ranges → overlap; a query point in the middle of a nested letter inside narration in one block → resolves to the letter (deepest), not the outer segment.

## BLOCKER-3 — `remap_references` misses B3 reference fields (source_anchor.py ~:306)

Current bug (reproduced): only `mention_ref`, `subject_ref`, `trigger_ref`, `endpoint_refs`, `event_ids` are remapped. UNMAPPED: `event_id` (singular, RelObs), `transition_hint.trigger_event_id`, `unresolved_threads[].subject_refs` (plural list).

**Fix:** cover EVERY reference field the B3 schema actually uses, by their real names:
- scalar → event map: `event_id`, `trigger_event_id`, `trigger_ref`.
- scalar → mention∪endpoint map: `subject_ref`, `mention_ref`.
- list → mention∪endpoint map: `subject_refs`, `endpoint_refs`.
- list → event map: `event_ids`.
Keep it a single source of truth (a field→map table) so no schema ref field can be silently uncovered.

**Mandatory probe:** one payload containing every ref-bearing shape (RelObs incl transition_hint, StateChange, Thread with subject_refs, Fact with event_ids+subject_ref) with OLD ids → assert ALL become NEW ids; add an assertion that every reference field name in the B3 schema appears in the remap field table (guard against future drift).

## BLOCKER-4 — typed schema is not actually enforced (validators only enum-check; ~:332)

Current bug (reproduced): deleting the required `CastClaim.role_hint` returns `ok=True, errors=[]`. Required-field / type / nullability of the dataclass shapes are not enforced, so `ValidationResult.payload` is NOT a trustworthy normalized typed payload — Step 3 cannot rely on it.

**Fix:** every row of every shape (B0/B1/B2/B3, including `Scene`, `AddressTerm`, `Glossary`, `RelObs`, `StateChange`, `Thread`, `Fact`, `FrameSegment`, endpoints) is validated for: required fields present (fatal if missing), type/shape correct, nullability respected, enums in range. Prefer constructing the typed dataclasses and catching `TypeError`/`ValueError` into `report.errors`, OR a per-shape required/type/nullable spec table — but it must be COMPLETE, not a hand-picked subset.

**Mandatory probes:** for EACH shape, a loop that deletes each required field in turn and asserts a fatal; a wrong-type case (e.g. `Scene.co_present_count="two"`) → fatal; a nullability case (a non-nullable field set null) → fatal.

## MAJOR-5 — `transition_hint` wrong type + unvalidated (builder_schema_v3.py ~:213; validator ~:911)

Current bug: declared `tuple[str,str]`; contract is an object `{trigger_event_id, note}`; validator never checks `trigger_event_id` (a nonexistent id passes).

**Fix:** `transition_hint: TransitionHint | None` where `TransitionHint = {trigger_event_id: str, note: str}`. Validator checks `trigger_event_id` references an existing `event_id` in the payload (same occurrence-grounding rule as the other refs).

**Mandatory probe:** `transition_hint` with a nonexistent `trigger_event_id` → fatal; with a valid one → ok and remapped by BLOCKER-3's fix.

## MINOR-6 — missing-parent miscounted as a cycle (~:841)

Current bug: `frame_missing_parent` is recorded but the frame is not skipped, so `_frame_depths` raises `KeyError` that is caught as `frame_cycle` — a phantom cycle on the audit.

**Fix:** a frame whose parent is missing is excluded from the depth/tree computation (or `_frame_depths` distinguishes missing-parent from a real back-edge). A missing parent emits ONLY `frame_missing_parent`; `frame_cycle` stays 0 unless there is a genuine cycle.

**Mandatory probe:** a missing-parent frame → `counts["frame_missing_parent"]==1 and counts["frame_cycle"]==0`; a real cycle → `frame_cycle>=1`.

## MINOR-7 (cleanup, since we're here) — `_all_spans` overlap semantics

`_all_spans` advances by +1, counting self-overlapping occurrences (`aa` in `aaa`). Not a live blocker (fail-closed still triggers), but lock **non-overlapping** occurrence semantics now (advance by `len(needle)`) so `occurrence_hint`/ordinal counting is correct if such a surface appears at scale. Add a probe on a self-overlapping surface.

---

## Handback (Claude re-verifies ADVERSARIALLY, no self-certify)

Return: the diff, the full fixture/probe run (all green), and — for BLOCKER-1/3/4 and MAJOR-5 — show the probe **failing on the pre-fix code and passing after** (or describe the failing output you observed pre-fix). Claude will independently re-probe the full two-axis table, delete each required field of each shape, call remap on every ref field, and build the two-frames-in-one-block case before accepting. Nothing wires into the live pipeline until Step-2 is ACCEPTED and Step-3 is specced.
