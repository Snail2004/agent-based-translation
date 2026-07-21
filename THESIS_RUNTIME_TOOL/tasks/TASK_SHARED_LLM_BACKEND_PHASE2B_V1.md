# TASK_SHARED_LLM_BACKEND_PHASE2B_V1

Status: COMPLETE

Author/Implementer: CodeX

## 1. Objective

Implement the offline-verifiable shared execution substrate behind the accepted
Phase 2A profile and seal contracts. The substrate is common to Input
Normalization, D2L/Terminology, Literary and Evaluation, while prompts,
generation values, semantic validators, semantic retries and publication remain
pipeline-owned.

## 2. Included

- opaque credential resolution with commitment verification and redacted values;
- one atomic lease per physical quota bucket;
- protocol-specific transport envelopes and normalized provider responses;
- exactly one physical attempt per backend invocation, with no hidden retry or
  fallback;
- append-only SQLite ledger for seals, usage, errors, cache observations and
  reusable-artifact receipts;
- content-addressed artifact storage with hash verification;
- application-response cache lookup/store bound to the exact producer receipt;
- adversarial, injected-transport tests with zero network/API use.

## 3. Excluded

- pipeline migration or runtime cutover;
- semantic prompt construction and response validation;
- automatic transport or semantic retry loops;
- provider/model/key rotation or inferred fallback;
- UI/backend routes, memory SQLite, pipeline checkpoints or publication;
- plaintext credential persistence in files, ledgers, exceptions or repr output.

## 4. Authority Boundary

One invocation receives an already validated `ResolvedLlmRunSealV1`, one logical
request identity and one pipeline-built request body. The shared layer verifies
that the transport source/protocol/route/model match the seal, resolves only the
seal's opaque credential reference, obtains the seal's physical-bucket lease,
performs at most one injected transport call and records exactly one physical
attempt. The caller decides whether a later invocation is allowed by the sealed
retry policy.

A reusable response or checkpoint requires a content-addressed artifact receipt
whose producer seal, role, stage, profile, inputs, request lineage and cache
namespace are compatible with the consumer. A well-formed hash without a
trusted ledger record is insufficient.

## 5. Acceptance

- no API/network call in tests;
- secrets are resolved only in memory and are redacted from repr/errors/ledger;
- duplicate physical-bucket acquisition fails closed;
- stale, corrupted or cross-seal artifacts fail closed;
- append-only ledger accepts exact idempotent replay and rejects identity reuse
  with different bytes;
- one backend invocation calls the injected sender at most once;
- all physical usage, errors and cache facts pass Phase 2A relational validation;
- compile, focused/full tests, diff and credential scans pass;
- no pipeline runtime, App UI, memory DB or checkpoint is modified.

## 6. Delivered

- `credentials_v1.py`: opaque in-memory credential resolution with exact
  commitment checks and non-serializable redacted values.
- `scheduler_v1.py`: process-independent, fail-closed physical-quota leases.
- `transport_v1.py`: sealed one-shot envelopes and normalized usage for OpenAI
  Chat Completions, OpenAI Responses, Google GenAI and local in-process calls.
- `artifact_store_v1.py` and `cache_v1.py`: content-addressed response bytes and
  exact-lineage application-response reuse.
- `ledger_v1.py`: append-only immutable SQLite evidence records, separate from
  every pipeline memory/checkpoint database.
- `backend_v1.py`: one invocation equals zero calls on a trusted cache hit or
  exactly one injected transport call; retry/fallback remains caller-owned.
- Persisted timestamps and `latency_ms` share one canonical millisecond
  precision, and HTTP 402/408 transport classes match the closed error contract.
- Explicit retry certification reserves unknown failed-request token usage at
  the sealed per-call maxima; evidence remains null and insufficient aggregate
  budget still fails closed.
- Phase 2A was tightened with trusted producer-seal and reusable-artifact
  receipts so a hash-shaped foreign artifact cannot bridge role, stage,
  profile, input or cache namespaces.

## 7. Verification

- focused shared-contract/backend suite: `116 passed`;
- complete `pipeline/tests`: `1390 passed`;
- the complete suite used a temporary read-only byte-identical frozen-DB copy,
  removed after the run;
- frozen DB SHA-256 before and after:
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- zero API/network calls and zero pipeline migration;
- compile, diff, ownership and credential scans passed.

## 8. Phase 3 Adoption Hardening

The neutral shared-core branch closed three consumer-found defects without
changing pipeline-owned profiles: canonical millisecond latency, HTTP 402/408
error classification, and conservative token reservation across an explicit
transport retry.

- focused shared-contract/backend suite: `121 passed`;
- complete neutral-anchor `pipeline/tests`: `848 passed`;
- the complete suite used a temporary read-only byte-identical frozen-DB copy,
  removed immediately after the run;
- frozen DB SHA-256:
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- zero API/network calls and zero pipeline runtime migration.
