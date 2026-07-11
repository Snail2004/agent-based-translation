# L2A2 Generality Probe - Gatsby Chapter 1

Run date: 2026-07-09

## Scope

- Book: The Great Gatsby
- Source: `reference/literary/great_gatsby/en/great_gatsby_gutenberg_64317_epub3_images.epub`
- Chapter: `gg_ch01`
- Pipeline: M1 = B0 chapter brief + B1 lexicon + B2 narrative evidence
- Prompt source: `design/LITERARY_PROMPT_DESIGN.md`
- Model/config: `gpt-5.4-mini`, `pipeline/configs/llm_prepass_gatsby_probe.yaml`
- Cache: `data/cache/literary_l2a2_api_openai_key2.sqlite3`
- API key: OpenAI key 2 via environment, value not logged.

## Loader / 0-API Checks

- Added generic Gutenberg EPUB loader support for multi-chapter XHTML files with TOC anchors.
- Gatsby EPUB ingest result:
  - chapters_total: 9
  - selected: `gg_ch01`
  - blocks_selected: 153
  - windows_selected: 24
- 0-API tests: `21 passed`
- Estimate with default 6000 prompt-token cap halted because B0 full chapter was ~9,580 prompt tokens.
- Probe config raised prompt_token_cap to 12,000 for this Gatsby stress test.

## Estimate

- Logical calls: 49
- Windows: 24
- Estimated prompt tokens: 111,286
- Max prompt tokens: 9,580
- Upper-bound total tokens: 412,342
- Cost cap: `$0.6299335`
- token_growth_halt: false under the Gatsby probe config.

## Actual Run

- Runtime observed by command wall clock: about 6m53s.
- Actual attempts/calls: 81
- Cache hits: 0
- Actual cost: `$0.1821082`
- Prompt tokens: 226,532
- Completion tokens: 70,168
- Reasoning tokens: 0
- Frozen D2L DB SHA256 after run: `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`

## Validation Summary

```json
{
  "brief_ok": 1,
  "brief_failed": 0,
  "lexicon_ok": 22,
  "lexicon_failed": 2,
  "narrative_ok": 21,
  "narrative_failed": 3,
  "parse_fail": 0,
  "phase_leak": 0,
  "context_only_used_true": 9,
  "brief_leak_tokens_dropped": 0
}
```

Positive signals:

- B0 chapter brief passed on first chapter-level run.
- No parse failures.
- `phase_leak = 0`; B2 did not mint relationship phases.
- Most windows passed on a structurally different book: 43/48 B1/B2 window calls passed after retries.

Blocking signals:

- Gatsby leg is not yet a PASS because 5 final window artifacts failed validation:
  - 2 lexicon failures.
  - 3 narrative failures.

## Failed Windows

1. `wb_gg_ch01_001` / lexicon
   - Core issue: model extracted entries from context-only tail blocks `gg_ch01_b005` and `gg_ch01_b006` while the active window was `gg_ch01_b001..b004`.
   - This is exactly the context-only boundary contract the generality probe was meant to test.

2. `wb_gg_ch01_003` / lexicon
   - Core issue: model invented candidate id `char_nick_father`; validator does not know this id.
   - Likely needs either stricter prompt wording for unknown/candidate ids or a validator rule that drops unknown candidate ids instead of failing the whole window.

3. `wb_gg_ch01_006` / narrative
   - Core issue: candidate addressee/target "Nick" had empty `candidate_entity_ids`.
   - This is a schema-contract failure, not a phase leak.

4. `wb_gg_ch01_015` / narrative
   - Core issue: candidate id `ent_nick` was unknown to the current known-entity set.
   - The text-level interpretation looks plausible, but cross-window identity wiring is not ready enough for Gatsby.

5. `wb_gg_ch01_022` / narrative
   - Core issue: `attribution_method: "candidate"` was emitted, but the allowed enum does not include `candidate`.
   - This is likely a prompt/schema wording issue.

## Gate Verdict

WH ch1 leg remains useful and passed earlier. Gatsby ch1 proves the loader and API path work on a second book, but the generality gate is still INCOMPLETE/FAIL until the five validation failures are fixed and revalidated or rerun.

Recommended next action for Claude/CodeX review:

- Do not open M4 yet.
- Decide whether the right fix is prompt-only, validator-granularity, or known-entity ledger/injection for Gatsby.
- In particular, inspect context-only extraction in `wb_gg_ch01_001`; that is the cleanest evidence that book-neutral prompt still leaks across the active-window boundary under Gatsby.
