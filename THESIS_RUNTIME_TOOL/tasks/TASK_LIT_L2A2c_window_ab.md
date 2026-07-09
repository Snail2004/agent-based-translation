# TASK_LIT_L2A2c — Window-size A/B (target_tokens 500 vs 1500)

Goal: measure, not guess, whether a larger active window cuts calls/tokens WITHOUT losing
extraction recall. Baseline 500 already exists (the v3 runs). This task produces the 1500 arm.
Do NOT commit. Do NOT open/advance M4 on the basis of this — it is a config probe.

## Why (one line)
In a real B2 call only ~18% of the prompt is the source window; ~82% is fixed overhead (system
prompt alone = 60%), repeated every call. Fewer/larger windows pay that overhead fewer times — IF
recall holds. This project's D2L work found long input can DROP recall, so we measure both.

## CodeX work

### 1. Parameterize the window caps (do NOT edit-and-revert the constant)
`builder_pilot.py:341` hardcodes `target_tokens=500, max_blocks=8` inside `build_literary_windows`.
Thread both as parameters from a new CLI flag on `run_literary_builder_pilot.py`, e.g.
`--window-target-tokens 1500 --window-max-blocks 24`, defaulting to 500/8 (so existing behavior and
the 26 tests are unchanged). Reason for max_blocks=24: at 1500 tokens the 8-block cap would bind in
small-block regions and you'd not actually get a bigger window — let the TOKEN cap be the binding one.

### 2. Run the 1500 arm on BOTH books, fresh dirs, hard-fail, same prompt/seed/validator
- WH ch1  -> `data/reports/literary_l2a2_wh_ch1_win1500/`
- Gatsby ch1 -> `data/reports/literary_l2a2_gatsby_ch1_win1500/`
Everything else identical to v3 (same design doc, same OpenAI key2, same hard-fail mode). The v3 dirs
(`*_hardened_v3`) are the 500 baseline — do not touch them.

### 3. Report per run
calls, cost_usd, prompt_tokens, completion_tokens, **cached_tokens**, window count, lexicon_ok/failed,
narrative_ok/failed, phase_leak, and any hard-fail. Keep the prompt-sample dump habit if cheap.

### 4. Side-observation (not a gate): prompt caching
Note the `cached_tokens` value. The 60%-of-prompt system message is identical across calls; if caching
is 0 again, say so — that is a separate, recall-safe optimization we may pursue regardless of window
size. Do not restructure messages for caching in THIS task; just report the number.

## Claude gate (on real artifacts, via compare_windows.py 3-axis diff)
1. **Calls/cost/tokens**: 1500 should cut call count and input tokens materially. Record the %.
2. **Recall (the decisive axis)**: evidence keyed by (block_id, surface) — window-invariant. 1500 must
   NOT drop mentions/turns/events that 500 caught. A big window that silently extracts fewer items
   still passes every validator, so this diff — not pass/fail — is the verdict.
   **Pre-registered floor: 1500 keeps >=95% of 500's mentions AND turns AND events, with zero NEW
   mis-resolutions on shared turns.** Gains (1500 resolves something 500 left unknown, e.g. a
   reference whose antecedent was in the read-only tail at 500 but in-window at 1500) are a plus.
3. **Resolution correctness**: sir/he/dog cases in WH must stay correctly resolved; Gatsby narrator
   (ent_nick_carraway) and seeded ids unchanged; no placeholder ids appear.

Adopt 1500 as the new default ONLY if calls/tokens drop AND the recall floor holds. If recall drops,
500 stays and we pursue prompt-caching instead (the recall-safe lever).
