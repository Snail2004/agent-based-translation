# Literary Source-Main Integration Manifest V1

## Purpose

Project the complete Literary B0-B4 source tree onto the current source `main`
without wiring it into the App, Console, Workflow Relay, or shared orchestrator.

## Revisions

- Target base: `main` at `6286706be595ae670aeb053516d3066da4c384be`
- Literary source: `codex/literary-b4-integration-v1` at
  `158443d764aa679e7d775cd12c2290e16586ba9a`
- B0-B3 lineage included by the source tip:
  `763f341a1c18c980c1b07d157c29dffddbeb24d5`
- B4 result pipeline included by the source tip:
  `c8fbc441` plus the lint-corrected exporter fix `158443d7`

## Projected Paths

The projection contains 547 exact tracked source paths. Eight were already
byte-identical on `main`, leaving 539 projected changes plus this manifest in
the integration commit.

| Path set | Count | Rule |
|---|---:|---|
| Literary runtime | 174 | every tracked file under `THESIS_RUNTIME_TOOL/pipeline/literary/` at the source tip |
| Literary stage CLIs and research CLIs | 75 | every script changed on the Literary lineage and still present at the source tip |
| Literary configs | 160 | tracked `THESIS_RUNTIME_TOOL/pipeline/configs/literary*` files |
| Literary-owned tests and fixtures | 132 | tests changed on the Literary lineage, excluding Shared LLM backend tests/fixtures |
| Literary design contracts | 5 | prompt, style, schema allocation, temporal policy, and provider-profile design files |
| Provider profile dependency | 1 | `THESIS_RUNTIME_TOOL/pipeline/agents/provider_profile.py` |

The Git tree of the integration commit is the authoritative per-file manifest.
It can be enumerated with:

```text
git diff-tree --no-commit-id --name-only -r <integration-commit>
```

## Deliberately Excluded

- `THESIS_RUNTIME_TOOL/data/reports/**`
- all live/probe evidence and historical provider responses
- credentials and local environment files
- `THESIS_RUNTIME_TOOL/app/**`
- App backend routes and UI components
- Console and Workflow Relay integration
- source-main Shared LLM backend replacements
- unrelated D2L, Evaluation, ingest, and translation source

The Literary runner and replay adapter source may be present under the Literary
package, but this integration does not register or connect them to the App or
shared workflow runtime.

## Shared-Core Policy

`main` owns the current Shared LLM backend. The Literary branch's older
`pipeline/llm_backend/**` variants were not projected. This avoids regressing
D2L and Evaluation while keeping Literary's provider-neutral adapters.

## Verification

- Python compile check: passed for Literary modules, scripts, and provider
  profile dependency.
- B4 focused suite: `111 passed`.
- Broad Literary selection: `1083 passed`, `6 skipped`, `21 failed`.
  - 10 failures require the intentionally excluded frozen
    `THESIS_RUNTIME_TOOL/data/jobs/d2l_p1/memory.sqlite3`.
  - 11 stale tests reproduce unchanged on the Literary source tip itself
    (one fixture expectation, one stale capability hash, and nine tests blocked
    by a stale B2 interaction schema pin).
- No provider/API calls were made for this integration.

This commit integrates source for submission. It does not claim live-provider
qualification or production App wiring.
