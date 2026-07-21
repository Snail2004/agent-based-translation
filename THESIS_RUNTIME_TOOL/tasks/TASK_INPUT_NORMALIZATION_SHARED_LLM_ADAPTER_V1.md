# TASK_INPUT_NORMALIZATION_SHARED_LLM_ADAPTER_V1

Status: IMPLEMENTED_0_API

Author/Implementer: CodeX

## Objective

Bind only `input_normalization.structure_draft.boundary_repair` to the frozen
Shared LLM Backend. Deterministic parsing, normalization, admission, formula
detection, export, Global Skeleton and hierarchy planning remain code-owned or
unbound.

## Ownership Boundary

The shared backend owns source and credential resolution, quota leasing, one
physical transport attempt, normalized provider usage/error evidence and the
content-addressed response receipt. Input Normalization owns the context pack,
prompt, response contract, local parser, semantic validator, correction-plan
authority and output namespace.

The adapter receives `ApiSourceV1` and `CapabilityEvidenceV1` records. It does
not contain a base URL, bearer, credential loader or physical provider choice.
There is no fallback, provider rotation, model rotation, transport retry or
semantic retry.

## Semantic Contract

- Presets `recommended_v1` and `recommended_v2` preserve response dialect v1.
- Presets `recommended_v3` and `recommended_v4` use response dialect v2.
- Dialect v2 requires every `focus_unit_id` exactly once across the union of
  actions and abstentions; omission, duplication and action-plus-abstention are
  invalid.
- Third-party routes use `prompt_validated` plus `json_object` only as a JSON
  syntax aid. The complete local validator is the sole semantic authority.
- Output remains proposal-only and requires explicit human approval before the
  existing Draft Structure apply gate.

## Runtime Limits

- exactly one context pack and one physical call;
- 12,000 prompt-token cap, 8,000 completion-token cap and 20,000 total-token
  cap;
- explicit UTF-8 prompt-byte preflight from the selected preset;
- unknown provider cost remains `null` with unknown provenance;
- application response-cache reads and writes are always disabled;
- Global Skeleton and hierarchy model roles remain disabled and unbound.

## Fail-Closed Rules

Profile, source, capability, model, prompt, report, context-pack, scope, preset,
implementation and run identities are sealed before transport. A stale report,
foreign capability, changed prompt/context, reused output root, malformed JSON,
invalid semantics, model drift, 401/402/429 or usage above a finite cap cannot
produce an accepted proposal. Physical evidence already observed remains
truthful even when semantic validation fails.

## Acceptance Gate

The milestone is 0-API and uses injected fake transport. Tests cover semantic
parity, strict focus coverage, source/capability binding, cache-disabled
behavior, one-attempt failures, stale/tampered input, output-root identity,
unknown cost and canonical source immutability. No App, consumer, schema,
SQLite, checkpoint, report/live-evidence or shared-core file is changed.
