# TASK EV-D2L-07b-3 — Locator fix: position-primary, remove left_anchor decider

Status: REVIEW (CodeX implemented; no commit) -> Claude reviews + commits.
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

### 5.1 Locator change

- Kept `PROMPT_VERSION="d2l_localizer_t2_v3"` and `RESULT_SCHEMA` byte-unchanged.
- Bumped `VALIDATOR_VERSION` from `code_anchor_v3` to `code_anchor_v3_position`.
- Changed repeated-quote resolution to `unique_quote -> position -> ambiguous`.
- Deleted `_left_anchor` and its now-dead `_normalize_ws` helper. `left_context` remains in the model contract/audit, but `_locate_model_quote` names it `_left_context` and never reads it.
- Preserved `_position_reanchor`'s existing margin guard; position remains a hint that may abstain rather than an unconditional nearest-match rule.

### 5.2 Tests

- Replaced the obsolete left-anchor test with `test_t2_position_resolves_repeated_quote`.
- Added `test_t2_misleading_left_context_does_not_mislocate`: the supplied left context uniquely points at the wrong occurrence, while source position selects the correct occurrence.
- Retained the regression proving model-supplied `start`/`end` fields are ignored; it now resolves through `position`.

Verification:

```text
python -m pytest pipeline/tests/test_localizer_cascade.py -q --basetemp D:\temp\pytest-localizer-cascade-v3-position
13 passed in 8.94s

python -m pytest pipeline/tests app/backend/tests -q --basetemp D:\temp\pytest-ev-d2l-07b3-final
328 passed in 110.16s
```

The first combined-suite attempt was invalid because two accidentally concurrent pytest processes shared one `--basetemp`, producing only Windows `WinError 32` setup errors. It was not counted; after the stray process finished, the suite was rerun serially with a fresh temp directory and passed 328/328.

### 5.3 Replay-only proof

Preflight remained unchanged: confirm token `LOCALIZE-909FCB602A82`, 8 DEV cases, estimated 9,760 total tokens / `$0.006024` only if uncached.

The DEV replay used the final verbatim-v3 API cache (`api_cache_v3_verbatim.sqlite3`) with `OPENAI_BASE_URL=http://127.0.0.1:9/v1` and a dummy key. Therefore a cache miss could not reach OpenAI. The run succeeded:

```text
api_cache_hits=8
result_cache_hits=0
prompt_tokens=0
completion_tokens=0
cost_usd=0.0
```

The API-cache SHA-256 and mtime were identical before/after:

```text
B26B52E3D9B90886DD63D3D2C166F0CEEC3EB700A578326F574F698BB196D003
mtime UTC: 2026-06-20 21:07:06 (unchanged)
```

This proves validator re-execution on fixed model outputs with zero new API calls.

### 5.4 Re-derived DEV result

`localizer_cascade_dev_v3.json` and `localizer_t2_v3_comparison.{json,html}` were regenerated. V3 is **7/8 (0.875)**, still DEV diagnostic only:

| Row | Locator | Exact |
|---|---|---:|
| `mt_mnist_dataset_923cfe4011:S1` | unique_quote | yes |
| `mt_annotation_2e76f54422:S0` | position | yes |
| `mt_annotation_2e76f54422:S1` | position | yes |
| `mt_elementwise_1d6feafbec:S0` | position | yes |
| `mt_elementwise_multiplication_0ebdbc19e3:S0` | unique_quote | **no** |
| `mt_membership_99ef7e9bc5:S0` | unique_quote | yes |
| `mt_tangent_line_fc8da8d99b:S0` | unique_quote | yes |
| `mt_target_0e8a3ad980:S0` | unique_quote | yes |

The sole residual is semantic boundary over-extension: model quote `phép nhân theo từng phần tử của hai ma trận` vs gold `phép nhân theo từng phần tử`. Locator code correctly locates the quote it was given; this remains T3/held-out work, not a reason to tune this DEV task.

### 5.5 Guards

- Shared-gold guard unchanged: `longest_match=108/116=0.9310344827586207`, legacy failures `8`, recommendation `longest_match`, PASS.
- Frozen DB SHA-256 unchanged: `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8` (first16 `DA0F687894090D43`).
- No API call, re-translation, prompt/schema change, DB mutation, or D-headline change.
- Trust/generalization remains deferred to EV-07c held-out.

## 6. Review (Claude) — VERDICT: PASS

Re-derived on on-disk artifacts:
- **V3 = 7/8** (recomputed from `localizer_cascade_dev_v3.json` vs corrected gold). The two previously-regressed cases recovered via `position`: annotation:S1 [410,418], elementwise:S0 [260,277]. Sole residual = elementwise_multiplication:S0 over-extension (genuine semantic, unique_quote) → T3.
- **L-PURE satisfied**: `api_cache_hits=8/8`, new tokens=0, cost=0 → pure replay; model quotes byte-identical (prompt/schema unchanged). Code-correctness fix only.
- **Locator no longer consults left_context**: diff removes the `left_anchor` branch + `_left_anchor`/`_normalize_ws`; param renamed `_left_context` (unused). `offset_source` values across the 8 = {position, unique_quote} only — no `left_anchor`. VALIDATOR_VERSION bumped to `code_anchor_v3_position`.
- **Guards**: shared-gold longest 108/116 + legacy 8 unchanged; frozen DB SHA-256 first16 = DA0F687894090D43 unchanged.
- **Tests**: full `pipeline/tests app/backend/tests` → 328 passed (re-ran myself).

The locator now correctly locates the model's (unchanged) quotes; the 7→8 gap is purely the elementwise_multiplication semantic over-extension. DEV diagnostic only; generalization deferred to EV-07c.
