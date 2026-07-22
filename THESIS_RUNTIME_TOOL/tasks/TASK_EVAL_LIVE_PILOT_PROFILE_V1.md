# TASK_EVAL_LIVE_PILOT_PROFILE_V1

Status: IMPLEMENTED (0-API gate passed)

## Objective

Bind one sealed Evaluation pilot to externally supplied shared-backend source
and capability records without copying credentials or choosing a provider/model
inside the runner.

## Contract

- Exactly one enabled `ApiSourceV1` supplies the physical quota bucket.
- Exactly one qualified `CapabilityEvidenceV1` is required for each remote
  Evaluation role: SF-BT back-translator, SF-BT semantic judge, and PJ judge.
- Capabilities must match the exact source/adapter/route, native Structured
  Output schema hash, and local validator hash for their role.
- Different role models are allowed, but every mapping is explicit and all
  roles remain on the one selected physical source row. No fallback exists.
- The pipeline-owned prompt, generation settings, token limits, retry policy,
  schema, validator, and namespaces remain in `EvaluationLlmProfileV1`.
- The artifact binds project/document/config/input/plan/preflight, logical run,
  attempt, profile/source hashes, physical bucket, isolated output root, cache
  mode, per-role calls, per-model calls, and hard token reservations.
- Gold/reference/oracle data, scores, verdicts, raw model output, plaintext
  credentials, source text, and translations are forbidden.

## Non-goals

- No credential resolver, provider call, capability probe, quota counter,
  model selection, checkpoint/cache/DB write, report publication, App/UI
  wiring, shared-core change, or source/translation mutation.

## Verification

- Dedicated contract probes: 12 passed.
- Evaluation live-pilot/profile/adapter/local-scorer integration: 110 passed.
- Full `pipeline/tests` regression from the runtime root: 1,352 passed in
  278.66 seconds.
- The full gate used a temporary read-only mirror of frozen DB SHA-256
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
  the hash was unchanged before/after and the mirror was removed.
- `py_compile`, `git diff --check`, and forbidden-authority/secret scans passed.
- Adversarial coverage includes foreign source/capability rows, source/model
  substitution after valid resealing, unknown fields, unsafe output roots, and
  forbidden Evaluation authority identifiers.
- API/network calls, credential resolution, provider fallback, DB writes,
  cache/checkpoint mutation, and source/translation mutation: 0.
