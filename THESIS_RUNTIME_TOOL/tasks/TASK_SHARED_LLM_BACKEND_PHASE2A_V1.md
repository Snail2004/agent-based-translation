# TASK_SHARED_LLM_BACKEND_PHASE2A_V1

Status: READY_FOR_REVIEW

Author/Implementer: CodeX

## 1. Objective

Create the provider-neutral, pipeline-neutral contract and deterministic resolver
defined by coordination DEC-034 Phase 2A. The package gives Input
Normalization, D2L/Terminology, Literary and Evaluation one backend/profile
shape while keeping each role's prompt, context, generation values, semantic
validator, authority and checkpoint behavior pipeline-owned.

## 2. Scope

In scope:

- closed records for API source, capability evidence, pipeline profile, physical
  attempt usage, errors and cache observations;
- credential-free profile-to-run-seal resolution;
- canonical serialization and SHA-256 identities;
- offline fixtures and adversarial tests.

Out of scope:

- provider/API calls or network access;
- plaintext credential loading;
- physical quota scheduling;
- durable usage/cache/checkpoint/report writes;
- pipeline adapters, semantic migration or runtime cutover;
- App UI and `app/backend/**`.

## 3. Contract

The implementation is additive under `pipeline/llm_backend/**`. Every record is
closed and rejects unknown fields, nonfinite values, secret-shaped content and
runtime authority fields. Evaluation role identities additionally reject
gold/oracle/human-reference and callback authority.

`PipelineProfileV1` standardizes field names but does not share values across
workstreams. Each role seals its exact source, model, Structured Output
capability, prompt/schema/validator hashes, semantic-extension hash, retry
limits, token/cost caps and isolated output/cache/checkpoint namespaces.

`ResolvedLlmRunSealV1` embeds the validated profile, role, source and capability
records. It contains only an opaque credential reference and commitment, never
the bearer itself. Input bindings are ordered and material to the seal hash.

Usage accounting stores one row per physical request. Reasoning tokens are a
subset of completion where reported that way, and unknown cost remains null.
Provider prompt cache is distinct from application response cache and never
claims that the provider request was avoided.

The post-review contract also binds usage, error and cache rows relationally to
one resolved run seal and one immutable logical request. Every physical attempt
has a seal-derived identity, semantic-attempt index and transport-retry ordinal.
A row with a valid standalone shape is still rejected if its source revision,
requested/observed model, physical quota bucket, request lineage, retry policy,
cache namespace or seal identity differs. Physical attempts and provider
request IDs are unique; semantic and transport retry sequences must be
contiguous and explicitly authorized by their predecessor error.

Finite token or cost caps cannot be certified from unbounded unknown provider
facts. Unknown values remain null and are never silently treated as zero. For a
failed request that may have reached the provider, token-limit certification
uses the sealed per-call input/output maxima as a conservative reservation; the
retry succeeds only when the aggregate cap covers that worst case. Unknown cost
still fails a finite cost gate because no monetary per-call reservation exists.
Source and Structured Output capability records are pinned by their canonical
hashes in the pipeline-owned target binding, so a reused source/capability
revision cannot change bytes under a resealed envelope.

Writable namespaces are derived from the complete profile, role, ordered input
binding and stage identities plus the run/attempt identity. Changing input,
stage, profile/preset revision or any material generation setting therefore
cannot reuse stale output, checkpoint or cache state. API-source aliases may
share one physical bucket, but adapter/protocol labels cannot make the same
endpoint/route/credential commitment look like an independent quota bucket.

Cache keys include the sealed input/profile/stage namespace and logical request
lineage. Provider prompt-cache observations bind the exact physical usage row
and cached-token facts. Application-response and checkpoint hits bind the
reused artifact plus producer seal/input lineage and never fabricate a new
attempt. Retrieval context cache remains non-authoritative for provider-call
avoidance.

`required` means qualified native Structured Output. `prompt_validated` records
the separate JSON-object plus exact local-validator path used by legacy/current
roles that do not have native schema authority. The fixture preset IDs are
explicitly `contract_fixture_v1`; they do not claim parity with runtime
`recommended_v1` presets.

## 4. Acceptance

- Focused contract and resolver tests pass.
- Source/model/route/schema/capability mismatches fail closed.
- Hidden fallback and cross-role namespace reuse fail closed.
- Cross-stage, cross-input and stale-attempt/cache reuse fail closed.
- Physical quota aliases cannot be created by renaming an adapter or protocol.
- Retry counts and order are enforced per logical request and physical attempt.
- Secret/gold/authority and nonfinite probes fail closed.
- Unknown token/cost facts and reasoning-token accounting remain truthful.
- Same validated input produces byte-identical canonical JSON and seal hash.
- Input objects are unchanged after validation/resolution.
- Full applicable `pipeline/tests` regression passes.
- Exact write-set, compile, diff and credential scans pass.
- No API/network/DB/cache/checkpoint/report write occurs.

## 5. Implementation Evidence

Implementation files:

- `pipeline/llm_backend/contracts_v1.py`
- `pipeline/llm_backend/resolver_v1.py`
- `pipeline/llm_backend/__init__.py`

Verification evidence is recorded in the clean milestone commit handback. Phase
2B transport, credential resolver, scheduler and durable attempt ledger remain
closed until all four pipeline consumer reviews accept this contract.

Executed gates after consolidating both independent consumer-review rounds:

- focused contract/resolver suite: `87 passed`;
- final full `pipeline/tests`: `1361 passed` after mounting a byte-identical,
  read-only frozen DB fixture;
- pre-final full regression before the last cache-miss edge-case probe:
  `1360 passed` with the same read-only fixture;
- frozen DB SHA-256 before/after:
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- temporary DB fixture removed after the run;
- exact write set: 10 observed, 0 foreign;
- compile, diff, forbidden-import and credential scans: clean;
- provider/API/network calls and writable runtime state: none.
