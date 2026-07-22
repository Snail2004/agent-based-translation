# TASK_EVAL_GOOGLE_TRANSLATE_BASELINE_V1

Status: MLP LIVE CAPTURE COMPLETE 2026-07-21

Owner: Evaluation workstream

## 1. Objective

Create a reproducible Google-NMT baseline for the sealed D2L Multilayer
Perceptrons chapter. The baseline uses the official Google Cloud Translation
Basic v2 endpoint with the standard `nmt` model, English to Vietnamese.

This is a bounded upstream capture, not a score and not a runtime callback.

## 2. Source and admission boundary

Input is the accepted `D2LEvaluationInputV1` MLP package with exact package,
source-DB, runtime-manifest, project, document, chapter and physical-file
hashes.

- `translate` rows are sent to Google.
- `preserve` rows are copied byte-identically by code.
- No source, S0, S1, glossary, memory, gold, Human translation or score is
  modified.
- Foreign chapters, duplicate IDs, unsupported admissions and source/hash drift
  fail before API use.

## 3. Request construction

Natural-language rows are grouped in source order. A preserve row closes the
current group so the runner never pretends that prose separated by omitted
source material was contiguous.

Each request uses one HTML payload containing bounded block envelopes. The
envelopes carry opaque technical markers; Google translates only their text.
Local parsing must recover every marker exactly once and in the same order.

Locked limits:

- recommended request maximum: 5,000 source characters including envelopes;
- hard run cap: 170,000 reserved source characters including failed or
  unknown-outcome physical attempts;
- one physical attempt per chunk;
- zero semantic retries;
- no provider, model, endpoint, key or mode fallback.

## 4. Checkpoint and failure semantics

The checkpoint is written before each network call. This makes a process death
after provider acceptance visible as `pending_unknown` rather than silently
retrying and double-spending.

- HTTP/locally known failures halt fail-closed.
- Timeout/network uncertainty halts as `pending_unknown`.
- An unresolved physical attempt is never retried automatically.
- A complete resume performs zero API calls.
- Resume requires byte-equivalent source, plan, model, profile, caps and key
  bucket identity.

## 5. Authority boundary

The output schema is private `GoogleTranslateBaselineCaptureV1` with explicit:

```text
artifact_kind = evaluation_private_baseline_capture
public_translation_artifact = false
requires_producer_promotion = true
```

It must not masquerade as public `TranslationArtifactV1` and must not identify
Evaluation as a D2L producer. Promotion into a public benchmark arm is a later,
separately authorized producer-boundary step.

## 6. Provenance and secrets

Persist:

- sealed plan/profile/config hashes;
- source and package identities;
- request payload/hash and reserved character count per chunk;
- sanitized provider response/hash;
- exact per-block target mapping;
- checkpoint, coverage and usage facts;
- `provider_reported_cost_usd = null` when Google does not return exact call
  cost.

Never persist the API key, query-string credential, raw environment or plaintext
secret. The key is supplied through a process-local environment variable and is
sent only in the `X-Goog-Api-Key` header.

## 7. Gates

1. Compile and focused 0-API tests.
2. Dry-render the real MLP package and confirm exact coverage/chunks/cap.
3. One small official-API HTML-envelope canary.
4. Full MLP run with checkpoint after every chunk.
5. Validate capture, exact cover, preserved byte equality, source/package
   immutability, usage, key scan and Git hygiene.
6. Commit only runner, tests and this task. Generated capture stays outside Git.

## 8. Non-goals

- Full-book Google translation.
- Google Web automation.
- Custom glossary, AutoML, Translation LLM or document translation.
- Evaluation scoring or report generation in the same milestone.
- Public contract or App/UI changes.

## 9. Completed MLP capture

Output root (generated outside Git):

```text
C:\work\agent-based-translation-baseline-captures\
  d2l_mlp_google_cloud_basic_v2_nmt_v1\
  run_20260721T093956Z
```

Sealed identities:

```text
plan_sha256    d44fa5c125d06ecddee4fba07849c9d566ff383e582467549831cb5c49eafacd
capture_sha256 dd4244a3d5c3c6d7de03ee33047e754f73357900bbf7f83667b34c54f2b675b8
package_sha256 0c159f15d8c6f7f31d306e379e1f7b48752f8911254da87c3507b479d68f860e
```

Final accounting:

- 641 source rows and 641 capture rows;
- 475 translated rows and 166 byte-identical preserved rows;
- 105 planned, completed, request and response chunks;
- 105 physical Google requests;
- 160,626 reserved and completed source characters;
- zero missing/failed rows, marker leaks, temporary files or plaintext-secret
  artifact hits;
- provider-reported exact cost remains `null` because Basic v2 returns no
  per-call monetary fact.

One local Windows sharing violation occurred after response 91 had already been
persisted but before the following checkpoint replacement. The initial process
stopped. Commit `463cead7dd2283797cc232b22e28e19b4510f61a` added bounded local
replace retry and deterministic recovery from a validated immutable response.
Resume recovered chunk 91 with zero provider calls and sent only the remaining
14 requests. The recovery receipt is retained under `recoveries/`.

Verification:

- focused runner tests: 8 passed;
- runner + D2L input + common input contracts: 42 passed;
- compile and diff checks passed;
- a broader `pipeline/tests -k evaluation` invocation exceeded its five-minute
  command timeout before producing a result, so it is not claimed as a passing
  gate.
