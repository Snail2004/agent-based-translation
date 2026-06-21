# TASK EV-D2L-07b-3 — Locator fix: position-primary, remove left_anchor decider

Status: READY (Claude wrote this) -> CodeX implements + sets REVIEW + STOP (NO commit) -> Claude reviews + commits.
Type: CODE-CORRECTNESS FIX on FIXED model outputs (deterministic replay). NOT a model/prompt change. Follow-up to EV-D2L-07b-2 (commit 5cc9c2b).

## 1. Problem (proven)
v3 locator picks the WRONG occurrence for non-unique quotes via `left_anchor`, while `position` alone is correct:
- `annotation:S1` "chú giải" (4 occ): left_anchor->[510,518] WRONG; position-alone->[410,418]==gold.
- `elementwise:S0` "theo từng phần tử" (3 occ): left_anchor->[407,424] WRONG; position-alone->[260,277]==gold.
The model's `left_context` hint is unreliable (misled both); `position` is source-anchored and independent. The model's QUOTES are right for 7/8 — the only thing wrong is the code's occurrence selection.

## 2. Change (locator only; do NOT touch the prompt or model behavior)
1. `_locate_model_quote`: new order = `unique_quote` -> `position` (existing `_position_reanchor` + margin guard) -> `ambiguous`. **Remove the `left_anchor` branch from the decision path.** Delete `_left_anchor` (and `_normalize_ws` if it becomes unused).
2. **Keep prompt v3 and RESULT_SCHEMA UNCHANGED this round** (model still returns `left_context`; it stays in the audit but the locator IGNORES it). This keeps the API replay cache valid so the model's quotes are byte-identical and the change is isolated to the locator. (Pruning `left_context` from prompt/schema is a separate optional cleanup that would require a fresh non-replay run — do NOT do it here.)
3. Bump `VALIDATOR_VERSION = "code_anchor_v3_position"` (keep `PROMPT_VERSION = "d2l_localizer_t2_v3"`). This invalidates the result-cache so decisions are re-validated with the new locator, while `client.call` still hits the prompt-keyed API replay cache.
4. Re-run `--preflight --dev` then `--run-dev --dev` with the confirm token. **This MUST be 0 new API calls (all api_cache_hit=true).** If any real API call happens, STOP and report (means a cache key drifted unexpectedly). Regenerate `localizer_cascade_dev_v3.json`, `audit_dev_v3.*`, and `localizer_t2_v3_comparison.{json,html}`.

## 3. Tests
- The existing `test_t2_left_anchor_resolves_repeated_quote` is now obsolete -> replace with `test_t2_position_resolves_repeated_quote` (non-unique quote resolved by source position).
- ADD `test_t2_misleading_left_context_does_not_mislocate`: a non-unique quote where `left_context` uniquely matches the WRONG occurrence MUST still be located by `position` to the correct occurrence (or abstain) — encodes exactly this bug.
- Full `pipeline/tests app/backend/tests` must pass.

## 4. LOCKs
- L1: still DEV diagnostic. Report v3 X/8 as DEV diagnostic, NOT generalization. Trust number = EV-07c (held-out).
- L-PURE: code-correctness fix on FIXED model outputs. No prompt/schema/model-behavior change. Re-run must be 0 new API (replay). Halt if not.
- L-GUARD: shared-gold guard unchanged (longest 108/116=0.9310, legacy=8); frozen DB SHA-256 first16 = DA0F687894090D43; eval-only; no re-translate; no D headline change.
- L-FLOW: CodeX fills sec 5, sets REVIEW, STOPS. No commit/push. Claude reviews sec 6 + commits.

Expected (verify, do not force): v3 = 7/8 — `annotation:S1` + `elementwise:S0` recovered via position; `elementwise_multiplication:S0` still over-extends (genuine semantic residual -> T3); the other 5 unchanged.

## 5. Implementation (CodeX)
<!-- CodeX: locator diff, version bump, 0-new-API proof (api_cache_hit all true), re-derived v3 table, test changes, shared-gold guard output, DB hash, suite result. Then REVIEW + STOP. -->

## 6. Review (Claude)
<!-- Claude: re-derive v3 X/8 from on-disk; confirm locator no longer consults left_context; confirm 0 new API; confirm guards + DB hash; run suite. -->
