# TASK_EVAL_D2L_MLP_CKEY_LIVE_PILOT_V1

Status: LIVE_V3_COMPLETE_MEASUREMENT_ONLY

## Objective

Run the accepted D2L MLP S0/S1 evaluation sample through the unchanged
SF-QE, SF-BT and PJ semantics while using the already-qualified CKEY Google
compatible route for the provider-backed roles.

The pilot is bounded evidence about scorer behavior. It cannot publish a
headline winner or mutate source, translations, runtime memory or gold data.

## Sealed Inputs

- D2L package: accepted `D2LEvaluationInputV1` MLP package.
- Preflight selection: eight source-only selected units.
- Physical work: 16 local SF-QE rows, 16 SF-BT back translations, 16 SF-BT
  semantic judgments and 14 PJ calls, for 46 provider calls total.
- Local SF-QE: `Unbabel/wmt22-cometkiwi-da`, CPU, exact cached checkpoint.
- Provider source: `ckey-xah-google-evaluation-v1` revision
  `ckey-xah-google-evaluation-20260720-v7`.
- Physical bucket: `ckey-account-v1`; credential remains external through
  `shared.ckey.account-v1`.
- Requested model for all provider-backed roles:
  `vuduythanh2023/gemini-3.5-flash`; accepted observed model:
  `gemini-3.5-flash`.
- Output mode: `prompt_validated` over qualified `json_object` syntax.
  Native schema parameters are forbidden on this third-party route.

## Resource Boundary

- Prompt, rubric, canonical response schemas and local validators are
  unchanged.
- Back translator generation and completion certification remain 4,096.
- Semantic and PJ generation remain capped at 512 output tokens.
- Their prompt-validated completion accounting cap is 4,096 because the
  route may report hidden completion/reasoning usage beyond generated JSON.
- The profile workload must reserve this usage envelope explicitly; it may
  exceed the preflight generation envelope but can never be smaller.
- No retry, fallback, provider switch, credential rotation or model change is
  allowed inside the sealed attempt. Provider cost remains `null` if the
  source supplies no authoritative cost fact.

## Offline Gate

Before any call:

1. `prompt_validated` accepts only exact `json_object` capability evidence;
   `required` accepts only exact native capability evidence.
2. Capability source credential-ref must equal the explicitly expected
   external credential-ref before credential loading or scoring.
3. CKEY request bodies contain JSON MIME guidance and the common JSON-only
   output instruction, but no `responseJsonSchema`.
4. Local validators remain the sole semantic authority.
5. A fake-transport end-to-end run covers all three provider roles and local
   SF-QE under the mode-specific usage envelope.
6. The local COMETKiwi runtime and checkpoint hash are verified without a
   download.
7. Code, focused tests, diff checks and secret scans are clean and committed.

## Live Gate

- Use a fresh logical run, attempt, profile revision and output root.
- Validate the exact capability summary before loading the credential.
- Halt on the first transport, quota, usage, parse or semantic-validation
  anomaly. Do not relabel partial evidence as a completed evaluation.
- A complete execution remains `calibration_only / INCONCLUSIVE` until its
  measurements are inspected independently.
- Report exact call coverage, local checkpoint identity, provider usage,
  errors, cache facts and all SF-QE/SF-BT/PJ observations.

## Non-goals

- No App UI, FullRunReport publication, production scoring certification,
  baseline generation, gold/reference access, score feedback into translation,
  model comparison, scorer prompt change or aggregation-policy redesign.

## Offline Verification

- Focused profile/adapter/runner tests: 51 passed.
- Broad Evaluation plus Shared LLM Backend regression: 584 passed, 905
  deselected.
- Full `pipeline/tests`: 1,486 passed and one skipped; its only two failures
  were the expected absent gitignored frozen-DB mount. Both exact DB probes
  then passed against a temporary read-only byte-identical mirror, which was
  removed after the test. Source and mirror SHA-256 remained
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`.
- Prompt-validated fake transport completed all three provider-backed roles
  and local SF-QE with zero failed jobs and no native schema parameter.
- Local Python 3.11 runtime reported `unbabel-comet 2.2.7`, CPU and checkpoint
  SHA-256
  `4F357AA38B0737DCD502F166238C99711FF3419D7B5C8CDF9CDE08525A8E7858`.
- API calls: zero for this offline gate.

## Live V1 Finding

The first sealed live root stopped after 11 successful physical attempts and
zero provider error rows. A PJ response reported 2,178
completion-accounting tokens, exceeding the 2,048 prompt-validated
certification cap. No execution result was published. The root is preserved as
terminal partial evidence at
`data/reports/evaluation_v1/d2l_mlp_live_pilot_20260720T172152Z_ckey_v1`.

The next profile must use a fresh run, attempt and output root. Increasing the
finite prompt-validated completion-accounting cap to 4,096 is permitted only
as a new profile revision; generation for semantic and PJ JSON remains 512.

The v2 focused gate passed 61 tests, including a fake provider response with
2,178 completion-accounting tokens. The real-input dry seal covers 46 calls
and reserves 552,000 prompt, 188,416 completion-accounting and 740,416 total
tokens. These are hard certification maxima, not expected usage.

The first v2 invocation was rejected because its supplied producer commit was
a nonexistent manual expansion of a short hash. One child request completed
before process termination; the root is preserved separately as
`REJECTED_PROVENANCE` and cannot contribute to any result. Before v3, the CLI
must verify the supplied commit against exact current Git HEAD before local or
provider execution.

The v3 CLI now performs that comparison before loading package data,
credentials, local SF-QE or provider transport. Short, uppercase, nonexistent
or stale commit values fail closed.

## Live V3 Result

The fresh v3 root completed all 16 local SF-QE rows and all 46 sealed provider
calls with zero failed jobs and zero ledger error rows. The execution remains
`calibration_only / INCONCLUSIVE / pilot_not_headline_evidence`.

The committed findings are at
`data/reports/evaluation_v1/d2l_mlp_live_pilot_20260720T173435Z_ckey_v3/FINDINGS.md`.
They record three load-bearing limitations: SF-BT scored every real row at
100, the SF-QE mean delta is concentrated in one heading, and PJ is based on
only eight units despite order counterbalancing. No winner is published.
