# TASK_SHARED_LLM_CAPABILITY_PROBE_V1

Status: IMPLEMENTED_OFFLINE

Author/Implementer: CodeX

## 1. Objective

Break the capability bootstrap loop without weakening normal Shared LLM run
resolution. A newly declared source/model/schema binding may execute one
bounded capability-only request before it is qualified. Normal pipeline runs
continue to require qualified `CapabilityEvidenceV1`.

## 2. Authority Boundary

The probe seal binds the exact API source bytes, credential commitment,
physical quota bucket, adapter/protocol/route, requested and accepted observed
model IDs, schema bytes/hash, local validator ID/hash, request body hash and
hard limits. It also binds the exact shared-core Git revision and consumer
revision/implementation hash. It has `authority=capability_only` and cannot
create a normal run seal, response-cache entry, checkpoint, memory mutation or
pipeline output.

The executor reserves the probe in the append-only ledger before transport,
obtains one physical-bucket lease, performs at most one sender call and returns
no provider payload. A crash after reservation requires a new probe identity.
The same capability revision cannot be probed again after terminal evidence;
the caller must publish a new revision.

## 3. Qualification

`qualified` requires all of the following:

- the provider completes one response with an accepted exact model identity;
- prompt, completion and total token usage are present, consistent and inside
  the sealed caps;
- response content is a JSON object;
- the exact sealed pipeline validator accepts the object;
- the receipt, raw-response artifact and final capability evidence validate as
  one immutable bundle.

HTTP success alone is insufficient. Transport, model identity, truncation,
usage, JSON or local-validator failures produce immutable `failed` evidence.
There is no retry, fallback, key/model rotation or cache reuse.

## 4. Stability Model

The probe mechanism is shared and reusable. Evidence remains scoped to the
exact source revision, route, model, capability kind, schema hash and validator
hash. A later accepted schema-subset profile may reduce the number of probes,
but V1 does not infer capability across unrelated schemas or validators.

## 5. Excluded

- live provider calls in this implementation milestone;
- automatic capability discovery or provider fallback;
- application-response cache publication;
- pipeline semantic output, DB/checkpoint writes or UI work;
- migration of D2L, Literary, Input Normalization or Evaluation call sites.

## 6. Verification

- injected fake transport only, zero API/network;
- unknown capability rejected by the normal resolver before probing;
- successful probe evidence accepted by the unchanged normal resolver;
- duplicate probe, source/schema/request/validator tamper, foreign model,
  malformed JSON, local-validator rejection, HTTP failure and token-cap breach
  fail closed;
- no normal usage/cache records or response bytes are exposed by the probe.
