# TASK EV-D2L-07b-2 — Localizer T2 v3: blind generalized prompt + offset fully in code + occurrence anchor; re-score all runs on corrected gold

Status: REVIEW (Claude wrote sec 1-4; CodeX filled sec 5 and stopped, NO commit) -> Claude reviews sec 6 + commits.
Type: ENGINEERING HARDENING + DIAGNOSTIC-on-DEV (NOT a generalization claim).
Predecessor: EV-D2L-07b (cascade pilot, commit 8cde8be). Successor: EV-D2L-07c (held-out chapter = the only trust number).

## 1. Context and goal
The 8-case DEV pilot proved: model char-offsets are useless (0/8 valid, code re-anchored 100%); two failure modes are real (span over/under-extension, adjacent-unmarked-neighbor); reasoning_effort=low was REJECTED on structural grounds (eats output budget -> empty at max256/1024; ~16x cost at max4096; net no win). See `data/reports/localizer_cascade_dev*.json`.

This task hardens the T2 contract so the MODEL only does semantics (which Vietnamese string) and CODE owns all mechanics (where it is), generalizes the prompt with NO case-specific rules, fixes one occurrence-selection bug, then RE-SCORES every prior run on the corrected gold for one apples-to-apples comparison.

Goal = a cleaner, more general, deterministic T2 whose engineering correctness is demonstrable. The resulting X/8 is DIAGNOSTIC ONLY (see LOCK L1).

## 2. Changes to implement

### 2A. Prompt v3 (verbatim system prompt — single source of truth; no hardcoded case)
```
You are a span localizer. You do not translate, rewrite, or judge quality —
you only point to text that already exists in TARGET_WINDOW.

INPUT
- SOURCE_CONTEXT: one source sentence. Exactly one term is wrapped in
  [[TERM_START]] ... [[TERM_END]]. That marked term — and nothing else in the
  sentence — is the concept to localize.
- TARGET_WINDOW: the translation of the source. The rendering of the marked
  term is somewhere inside it.

TASK
Return the single contiguous substring of TARGET_WINDOW that renders exactly
the marked term.

PRINCIPLE OF EXACT CORRESPONDENCE (the core rule)
The span you return must correspond one-to-one to the marked words — no fewer,
no more, and not a different word:
- No fewer: if the marked term is a multi-word expression, return the whole
  phrase that renders it, never just a fragment of it.
- No more: never absorb the rendering of any word that lies OUTSIDE the
  markers — an adjacent word, an alternative or synonym, a modifier, or the
  term's grammatical object or complement.
- Not a neighbour: when the sentence offers more than one candidate, localize
  the one that renders the marked term, never an unmarked alternative.

EXTENT
Choose the smallest contiguous span that still fully expresses the marked
concept.
- Include a grammatical or classifier word only when it is part of expressing
  the marked term itself — i.e. it would still be present if the marked term
  were translated on its own.
- Exclude words that belong to the surrounding sentence rather than to the
  term: quantity/number determiners, prepositions that link to unmarked words,
  and the renderings of any unmarked source words.

EXTRACTION, NOT GENERATION
Copy the span verbatim from TARGET_WINDOW. Never translate it yourself,
reorder, normalize, correct, or invent text.

LEFT CONTEXT
Also copy the few words of TARGET_WINDOW that appear immediately before your
span (verbatim, or empty if the span is at the very start). This is only used
by the caller to disambiguate repeated renderings; it is not part of the span.

OFFSETS
Do not return character offsets. The caller locates your span by exact string
match using the span and the left context.

WHEN NOT TO ANSWER (prefer this over guessing)
- omitted: the marked concept has no rendering in the window.
- ambiguous: a rendering exists but you cannot tell which occurrence matches
  this source occurrence.
- not_found: you cannot locate any plausible rendering.

Output must conform exactly to the JSON schema.
```

### 2B. Output contract (drop offsets, add left_context)
New JSON schema (strict) the model returns:
```
{ "occurrence_id": str, "status": "localized|omitted|ambiguous|not_found",
  "target_quote": str, "left_context": str }
```
REMOVE `start` and `end` from the schema entirely. Keep `occurrence_id` echo for validation.

### 2C. Code owns ALL offset location (rewrite `validate_t2_payload` locate logic)
Given status=localized, quote q, left_context lc, window W:
1. occurrences = all start indices of exact q in W.
2. len==0 -> `not_found` (offset_source="none").
3. len==1 -> use it. offset_source="unique_quote".
4. len>1 -> left-anchor: keep occurrences i where W[max(0,i-len(lc_stripped)):i] ends with lc_stripped (whitespace-normalized, lc empty => skip this step). If exactly 1 remains -> use it, offset_source="left_anchor".
5. still ambiguous -> existing `_position_reanchor` (source-relative ratio + margin guard). If it resolves -> offset_source="position". Else -> `ambiguous`/human_required, offset_source="none".
6. absolute = window.start + local.
Classification (`classify_localized_quote`) runs AFTER, unchanged.

### 2D. Bug fix (occurrence trust hole)
The current code trusts a model-provided offset when `W[start:end]==quote` even if the quote is non-unique -> can lock onto the wrong occurrence (observed in low/max4096 annotation:S0/S1). Since 2B removes model offsets, this branch must be DELETED, not kept. Confirm no code path can select an occurrence from a model-supplied integer.

### 2E. Version bumps (cache correctness)
`PROMPT_VERSION="d2l_localizer_t2_v3"`; `VALIDATOR_VERSION="code_anchor_v3"`. Result-cache key already includes both -> old v2 entries will not be reused (correct). Keep config: model gpt-5.4-mini-2026-03-17, reasoning_effort=none, temperature=0, seed=20260621, max_output_tokens=256.

## 3. Re-score + single comparison (DEV-8, corrected gold)
Gold precondition (already applied in working tree, verify the slice): `mt_membership_99ef7e9bc5:S0` gold = 526..537 = "sự thuộc về" (CodeX fix). Verify `target_text[526:537]=="sự thuộc về"`.

- Re-score EVERY prior run on the CORRECTED gold, 0-API, from the STORED decisions in each report JSON (compare stored start/end to new gold start/end). Do NOT re-call the API for prior runs.
- Run the NEW v3 (none/temp0, prompt v3 + 2C/2D) through preflight + confirm-token gate, then `--run-dev`. Expected ~8 calls / ~$0.002.
- Emit ONE comparison table `data/reports/localizer_t2_v3_comparison.json` (+ a small HTML), rows = {committed baseline v2, none/temp1 v2, low/temp1/max4096 v2, NEW v3}, columns = [label, prompt_version, reasoning, temp, max_out, exact_on_corrected_gold/8, cost, note]. Plus a per-case diff (v3 vs committed baseline): row_id, term, gold_span, v3_quote, v3_offset_source, correct?.
- Expected re-scored anchors (verify you reproduce these): committed baseline=5/8, none/temp1=6/8, low/max4096=4/8.

## 4. LOCKs / constraints
- L1 (TUNING-ON-TEST): the v3 X/8 and the comparison are DIAGNOSTIC, NOT a quality/trust metric. Do NOT iterate the prompt or config to raise the 8-number. The trust number comes ONLY from EV-07c (held-out chapter). Label every number "DEV diagnostic, not generalization".
- L2 (NO HARDCODE): the prompt must contain NO case-specific pattern, surface form, or term. If you believe a rule is needed, it must be a general linguistic principle.
- L3 (DETERMINISM): keep reasoning_effort=none, temperature=0, seed pinned. No temp=1, no reasoning>none (rejected: see EV-07b-2 sec 1).
- L4 (MODEL DOES NOT DO OFFSETS): no code path may derive an offset from model output other than by exact string match of quote/left_context in the window.
- L5 (SHARED GOLD GUARD): the gold CSV is shared with EV-07a. After the gold fix, re-run `score_localizer_bakeoff` and assert recommendation=="longest_match" and longest accuracy unchanged (0.9310, 108/116); assert `legacy_longest_failures(rows)==8`. Report both. (Do not modify EV-07a artifacts.)
- L6: eval-only. No re-translate. No write to frozen memory DB. Frozen DB SHA-256 first16 must stay DA0F687894090D43. No D consistency headline change.
- L7: API keys env-first then file fallback ["OPENAI-KEY-2.txt","OPENAI-KEY-1.txt","API-KEY.txt"] at repo root; never log keys. SQLite caches stay gitignored.
- L8: CodeX fills sec 5, sets REVIEW, STOPS. No commit / no push. Claude reviews sec 6 and commits.
- L9: targeted tests for the new locate logic (unique / left_anchor / position / ambiguous, and a non-unique-quote case that previously could mis-select) + full suite must pass.

## 5. Implementation (CodeX)

### 5.1 Files changed

- `data/eval/localizer_gold.csv`
  - Corrected `mt_membership_99ef7e9bc5:S0` from `sự thuộc` to `sự thuộc về`.
  - Verified slice: `target_text[526:537] == "sự thuộc về"`.
- `pipeline/eval/localizer_cascade.py`
  - Bumped `PROMPT_VERSION` to `d2l_localizer_t2_v3`.
  - Bumped `VALIDATOR_VERSION` to `code_anchor_v3`.
  - Replaced the T2 system prompt with the v3 generalized prompt.
  - Removed `start`/`end` from the strict model schema; model now returns only `occurrence_id`, `status`, `target_quote`, and `left_context`.
  - Reworked `validate_t2_payload` so the model never supplies offsets. Code locates the returned quote by exact match, optional left-context disambiguation, then position re-anchor.
- `pipeline/tests/test_localizer_cascade.py`
  - Updated tests for the new no-offset schema.
  - Added coverage for left-anchor disambiguation.
  - Added regression coverage that model-supplied offset-like fields are ignored even when they point to a wrong occurrence.
- `pipeline/scripts/localizer_t2_v3_compare.py`
  - New comparison/report script. It re-scores prior reports on the corrected gold and writes one comparison JSON/HTML.

Generated artifacts:

- `data/reports/localizer_cascade_preflight_v3.json`
- `data/reports/localizer_cascade_dev_v3.json`
- `data/eval/localizer_cascade/audit_dev_v3.csv`
- `data/eval/localizer_cascade/audit_dev_v3.html`
- `data/reports/localizer_t2_v3_comparison.json`
- `data/reports/localizer_t2_v3_comparison.html`

### 5.2 Offset-location algorithm

For a localized model payload, the validator now does:

1. Reject empty quote or quote not found in the current target window.
2. If the quote occurs once, use that occurrence (`offset_source=unique_quote`).
3. If the quote occurs multiple times and `left_context` is non-empty, normalize whitespace and choose the single occurrence whose preceding window text ends with that left context (`offset_source=left_anchor`).
4. If still ambiguous, fall back to position re-anchor (`offset_source=position`).
5. If no unique occurrence remains, return `ambiguous` instead of guessing.

No code path reads `payload.get("start")` or `payload.get("end")`. Verification command:

```
rg -n --fixed-strings 'payload.get("start"' pipeline/eval/localizer_cascade.py
rg -n --fixed-strings 'payload.get("end"' pipeline/eval/localizer_cascade.py
```

Both returned no matches. The remaining `offset_source` values are produced by code after quote location.

### 5.3 API preflight and run

Preflight:

```
python -m pipeline.scripts.localizer_cascade --preflight --dev `
  --preflight-out data/reports/localizer_cascade_preflight_v3.json `
  --out data/reports/localizer_cascade_dev_v3.json `
  --audit-csv data/eval/localizer_cascade/audit_dev_v3.csv `
  --audit-html data/eval/localizer_cascade/audit_dev_v3.html
```

Preflight output:

- Confirm token: `LOCALIZE-909FCB602A82`
- Prompt version: `d2l_localizer_t2_v3`
- Estimated prompt tokens: `7712`
- Estimated max output tokens: `2048`
- Estimated total tokens: `9760`
- Estimated cost: `$0.006024`
- Worst-case three-stage tokens: `29280`
- Worst-case cost: `$0.018072`

Run:

```
python -m pipeline.scripts.localizer_cascade --run-dev --dev `
  --confirm-token LOCALIZE-909FCB602A82 `
  --api-cache data/eval/localizer_cascade/api_cache_v3.sqlite3 `
  --result-cache data/eval/localizer_cascade/result_cache_v3.sqlite3 `
  --preflight-out data/reports/localizer_cascade_preflight_v3.json `
  --out data/reports/localizer_cascade_dev_v3.json `
  --audit-csv data/eval/localizer_cascade/audit_dev_v3.csv `
  --audit-html data/eval/localizer_cascade/audit_dev_v3.html
```

Actual v3 usage:

- Prompt tokens: `6934`
- Completion tokens: `366`
- Cost: `$0.0024655`
- API cache hits: `0`
- Result cache hits: `0`

### 5.4 Re-scored comparison on corrected gold

Command:

```
python -m pipeline.scripts.localizer_t2_v3_compare `
  --out data/reports/localizer_t2_v3_comparison.json `
  --html data/reports/localizer_t2_v3_comparison.html
```

Comparison rows:

| Label | Prompt | Config | Exact on corrected gold | Cost |
|---|---|---|---:|---:|
| committed baseline v2 | `d2l_localizer_t2_v2` | none/temp0/max256 | 5/8 = 0.625 | `$0.0000000` |
| none/temp1 v2 | `d2l_localizer_t2_v2` | none/temp1/max256 | 6/8 = 0.750 | `$0.0016875` |
| low/temp1/max4096 v2 | `d2l_localizer_t2_v2` | low/temp1/max4096 | 4/8 = 0.500 | `$0.0276275` |
| NEW v3 | `d2l_localizer_t2_v3` | none/temp0/max256 | 5/8 = 0.625 | `$0.0024655` |

This is DEV diagnostic only, not a held-out generalization metric.

### 5.5 New v3 per-case notes

V3 exact cases:

- `mt_mnist_dataset_923cfe4011:S1` -> `tập dữ liệu MNIST`, `unique_quote`, exact.
- `mt_annotation_2e76f54422:S0` -> `chú thích`, `left_anchor`, exact.
- `mt_membership_99ef7e9bc5:S0` -> `sự thuộc về`, `unique_quote`, exact after corrected gold.
- `mt_tangent_line_fc8da8d99b:S0` -> `đường thẳng tiếp tuyến`, `unique_quote`, exact.
- `mt_target_0e8a3ad980:S0` -> `mục tiêu`, `unique_quote`, exact.

V3 remaining failures:

- `mt_annotation_2e76f54422:S1`: model returned the right quote string `chú giải`, but that quote occurs more than once. Left-context selected `[510,518]`; gold is `[410,418]`. This is another occurrence-disambiguation failure.
- `mt_elementwise_1d6feafbec:S0`: model returned the right quote string `theo từng phần tử`, but that quote occurs more than once. Left-context selected the later occurrence `[407,424]`; gold is `[260,277]`. This is an occurrence-disambiguation failure, not a semantic quote failure.
- `mt_elementwise_multiplication_0ebdbc19e3:S0`: model returned `phép nhân theo từng phần tử của hai ma trận`, over-extending beyond gold `phép nhân theo từng phần tử`.

### 5.6 Shared-gold guard and DB guard

Shared-gold guard from `localizer_t2_v3_comparison.json`:

- Recommendation: `longest_match`
- Longest exact: `108/116`
- Longest accuracy: `0.9310344827586207`
- Legacy longest failures: `8`
- Pass: `true`

Frozen DB:

- Path: `data/jobs/d2l_p1/memory.sqlite3`
- SHA-256 first16: `DA0F687894090D43`
- Full SHA-256: `da0f687894090d43b75a3ae52ba71ec1edf85dab3198c9f86039879365d464b8`

### 5.7 Tests

Targeted localizer cascade tests:

```
python -m pytest pipeline/tests/test_localizer_cascade.py -q
13 passed in 2.28s
```

Combined localizer tests:

```
python -m pytest pipeline/tests/test_localizer.py pipeline/tests/test_localizer_cascade.py -q --basetemp D:\temp\pytest-localizer-v3-verbatim
27 passed in 2.18s
```

Full THESIS_RUNTIME_TOOL pipeline suite:

```
python -m pytest pipeline/tests -q --basetemp D:\temp\pytest-thesis-runtime-v3-verbatim
195 passed in 72.75s (0:01:12)
```

One attempted full-suite command from the repository parent failed collection with `ModuleNotFoundError: No module named 'pipeline'`; rerunning from `THESIS_RUNTIME_TOOL` passed as above.

## 6. Review (Claude) — VERDICT: PASS (faithful diagnostic; honest reporting)

Re-derived on on-disk artifacts ([[verify-on-committed-artifacts-not-reports]]):
- **V3 = 5/8** (recomputed from `localizer_cascade_dev_v3.json` vs corrected gold). Per-case OK: mnist, annotation:S0 (position), membership (unique), tangent, target (unique). BAD: annotation:S1, elementwise:S0, elementwise_multiplication:S0.
- **L2 no hardcode**: prompt v3 is verbatim my spec, contains no case/surface/term. PASS.
- **L4 model does NOT do offsets**: `start`/`end` removed from schema AND from `validate_t2_payload`; offsets come only from `_locate_model_quote` (string match). No code path reads a model integer offset. PASS.
- **L5 shared-gold guard**: `legacy_longest_failures=8`; EV-07a `recommendation=longest_match`, longest `108/116=0.9310` UNCHANGED. PASS.
- **L6**: frozen DB SHA-256 first16 = `DA0F687894090D43` unchanged. PASS.
- **L9**: re-ran full `pipeline/tests app/backend/tests` → **328 passed** (326 + 2 new). PASS.

**KEY FINDING — `left_anchor` (Claude's proposal) is net-harmful, proven by counterfactual.** In BOTH regressed cases the quote is non-unique and `left_anchor` confidently picked the WRONG occurrence, pre-empting `position` which alone is correct:
- annotation:S1 "chú giải" (4 occ): left_anchor→[510,518] WRONG; **position-alone→[410,418]==gold**.
- elementwise:S0 "theo từng phần tử" (3 occ): left_anchor→[407,424] WRONG; **position-alone→[260,277]==gold**.
`position` is source-anchored and independent of model text quality; `left_anchor` depends on the model returning a discriminating context and, when it doesn't, overrides a working signal. My left_anchor idea did not pan out — I own that.

The v3 PROMPT itself is a real gain (fixed membership + target via a general rule, 0 hardcode). The one genuine semantic residual is `elementwise_multiplication:S0` (unique-quote over-extension "của hai ma trận") → T3.

**DECISION carried to EV-07c (a-priori, measured ONLY on held-out — NOT re-measured on these 8):** adopt v3 prompt + offsets-in-code; **DROP `left_anchor`** → `position`-reanchor primary; on non-unique-and-too-close → abstain → T3. The counterfactual above is mechanistic justification for dropping left_anchor, NOT an 8-case quality claim. This task's artifacts are kept internally consistent (v3+left_anchor = 5/8); the left_anchor removal is deferred to EV-07c so it is measured cleanly rather than retro-fitted here.
