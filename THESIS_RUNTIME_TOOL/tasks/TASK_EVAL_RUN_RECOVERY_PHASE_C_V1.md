# Evaluation Run Recovery Phase C

## Purpose

Publish a producer-owned, replayable Console projection for an Evaluation
component without changing the immutable component events, recovery journal,
diagnostics, scorers, prompts, models, inputs, or settings.

This milestone is fixture-only and `0-API`.

## Projection contract

`EvaluationConsoleProjectionV1` is a content-addressed cumulative chain with
exactly one projection artifact per immutable component event. Each projection
binds:

- the exact component event prefix through `through_component_seq`;
- monotonic recovery-journal and redacted-diagnostic prefixes;
- the previous projection hash;
- newly visible Console rows and their cumulative row-chain hash;
- the component attempt and producer code revision.

The artifact index must exactly cover the projection directory. Every artifact
is bound to its publishing component event and the preceding projection. The
component package validator deterministically rederives the complete chain and
rejects missing, extra, reordered, foreign, future-prefix, hash-drifted, or
path-escaping artifacts.

## Console policy

- Raw component events remain authoritative and immutable.
- Individual retry events are retained for audit but are not projected as
  separate user-facing rows.
- Equivalent retries are collapsed by stage, retry kind, and logical request
  when the stage or component closes. The summary records retry count,
  physical-attempt indexes, reason codes, and the observed outcome.
- A recoverable `component_halted` produces one warning-level pause row per
  sealed incident identity.
- An unverifiable terminal `component_failed` produces one error-level row.
- Validation failures remain visible only when the producer actually emitted a
  `validation_failed` event.
- Internal exception text, stack traces, local paths, credentials, and raw
  diagnostics never enter public rows.

## Replay and Resume

The component sequence remains continuous across component attempts. Reopening
or resuming a component validates the existing projection prefix before
appending another immutable projection. Existing artifacts are never rewritten.
The same accepted prefixes always produce byte-identical projection bytes.

## Acceptance

- Projection count exactly matches component-event count.
- Reopening is byte-identical and Resume preserves the stable component run and
  continuous component sequence.
- Two raw retries collapse into one producer-sealed warning row.
- A redacted internal incident produces one amber pause row.
- An integrity-terminal event produces one red row.
- Projection, recovery-prefix, diagnostic-prefix, artifact-index, parent,
  foreign-component, and future-prefix tampering fail closed.
- Existing writer and benchmark behavior remains compatible.
- Focused and broad offline tests, compilation, diff checks, and secret scans
  pass with zero API calls.

## Non-goals

- App, backend, relay, or workflow-parent integration.
- New public runtime settings or UI labels.
- Changes to recovery authority, repair semantics, provider behavior, models,
  prompts, rubrics, schemas, validators, source inputs, or scoring algorithms.
- Provider calls, live five-chapter runs, database writes, or report-root
  publication.
