# Task: Neutral Workflow Live/Replay Relay V1

## Scope

Implement the Coordinator-owned, 0-API relay for the parent workflow:

```text
translation -> evaluation -> publication
```

The relay is the sole writer of parent `workflow_manifest.json`, `events.jsonl`,
`artifact_index.json`, global `seq`, scoring handoff and scoring receipt index.
It does not execute a pipeline, call an LLM, infer pipeline semantics, or write
inside any producer's component root.

## Authority boundary

- D2L, Evaluation and Publication retain their component validators, local
  `component_seq`, attempt lineage, checkpoints and artifact authority.
- A trusted component adapter must run the owning validator before producing a
  `ComponentSnapshotV1`; the relay independently checks common identity,
  sequence, exact source bytes, safe public payloads and physical artifact bytes.
- App UI is read-only and consumes only the projected parent package.
- Shared LLM Backend remains transport/attempt authority and is not imported.

## Required behavior

1. Bind one App-minted `workflow_run_id`, job, six-part Source Package identity,
   declared parent stage order and full 40-character relay implementation commit.
2. Copy validated component snapshots byte-identically into immutable
   content-addressed snapshot roots.
3. Under an OS-backed single-writer lock, accept an immutable import record and
   atomically regenerate parent projections. Recovery must converge from import
   records after interruption.
4. Assign contiguous parent sequence numbers. Exact source-event replay is
   idempotent; unequal event-ID reuse, component gaps, foreign workflow, unknown
   stage/artifact and append-after-terminal fail closed.
5. Parent events expose schema/version, hash chain, component attempt and source
   event hash. They exclude raw prompts/responses, credentials, references and
   oversized payloads. Unknown cost remains null.
6. Reconstruction uses declared stage order plus component sequence and declares
   `reconstructed=true`, `timing_authority=logical_order_only`.
7. Convert only D2L-owned S0/S1 evidence, combine it with separately owned
   community/Google-NMT/LLM-LC rows, map every D2L-owned ref to its exact
   imported parent artifact, and emit exact ordered `ScoringHandoffV1`.
8. Accept `ScoringReceiptV1` only when it exactly echoes the parent-owned
   `handoffs/scoring_handoff.json` binding, all five rows and input-set hash.
9. Re-open the parent package read-only and rederive its manifest, event chain,
   artifact index, component snapshots and current manifest revision from the
   immutable relay imports; physical drift and artifact-parent cycles fail closed.

## Exclusions

- No API, credentials, SQLite, checkpoints, pipeline runtime, App backend/UI,
  Source Package, D2L, Literary or Evaluation edits.
- No long live run authorization.
- No directory-scan inference of scoring arms or component status.
- No invented timing, usage, retry, cache, model, provider or cost facts.

## Gate

- Focused relay/contract tests with duplicate, drift, Resume, terminal, private
  payload, crash-recovery and reconstruction probes.
- D2L fragment to exact five-arm Evaluation handoff and exact receipt echo.
- Cross-worktree synthetic replay through accepted D2L and Evaluation validators.
- Compile, diff, secret and exact-scope scans; 0 API.
