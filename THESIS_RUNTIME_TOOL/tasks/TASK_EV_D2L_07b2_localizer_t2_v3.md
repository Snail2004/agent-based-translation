# TASK EV-D2L-07b-2 — Localizer T2 v3: blind generalized prompt + offset fully in code + occurrence anchor; re-score all runs on corrected gold

Status: READY (Claude wrote sec 1-4) -> CodeX fills sec 5 + set REVIEW + STOP (NO commit) -> Claude reviews sec 6 + commits.
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
<!-- CodeX fills: files changed, exact locate algorithm, re-scored table, new v3 per-case, costs, test results, frozen DB hash, shared-gold guard output. Then set REVIEW and STOP. -->

## 6. Review (Claude)
<!-- Claude: re-derive v3 X/8 + comparison on corrected gold from on-disk artifacts; confirm no model-offset path; confirm prompt has no hardcoded case; confirm L5 shared-gold guard; confirm DB hash; run suite. -->
