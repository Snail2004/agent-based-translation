# TASK_EVAL_RUN_RECOVERY_V1

Status: IMPLEMENTED_PENDING_GATE

## 1. Objective

Allow an interrupted Evaluation run to continue from durable accepted work
without changing the translation or scoring architecture and without forcing a
new run for ordinary transport or implementation failures.

The milestone is fixture-only and `0-API`.

## 2. Operational Policy

- Recovery is scoped to one Evaluation component package.
- The recovery path never scans Git, repository files, UI files, or unrelated
  pipeline files. An unrelated source-tree change cannot block Resume.
- The immutable journal is authoritative. `work_ledger.json` is only a derived
  projection and may be rebuilt after the exact journal-to-ledger crash window.
- A stable `work_id` identifies semantic work. Every physical provider attempt
  has its own immutable `physical_attempt_id`; an ID is never reused with
  different sealed bytes.
- Accepted work, usage facts, artifacts, checkpoints, and attempt outcomes are
  retained across Resume. Unknown external outcomes remain unknown.
- Transport and operational failures publish one warning-level
  `component_halted` event with `resume_available=true`. Redacted diagnostics
  remain internal and do not flood the Console with false validation errors.
- `validation_failed` remains reserved for a real validation result.
- Integrity drift remains terminal because accepted lineage can no longer be
  proven.

## 3. Semantic Boundary

Resume is allowed only when these experiment-defining bindings are unchanged:

- exact input set;
- Evaluation settings and profile;
- stage plan and sample selection;
- prompt/schema/validator semantic contract.

Changing translations, model semantics, prompt, rubric, schema, validator,
sampling, or accepted artifact bytes requires a new run. Operational code
revision is diagnostic lineage only and is not a repository-wide allowlist.

## 4. Durable Records

The component owns only:

- `recovery/assignment.json`;
- append-only `recovery/journal_records/*.json`;
- derived `recovery/work_ledger.json`;
- content-addressed `recovery/checkpoints/*.json`;
- redacted `recovery/diagnostics/*.json`.

No database, shared writable state, source package, translation artifact, or
provider credential is modified.

## 5. Acceptance

- Injected crash after intent, physical seal, usage/error, response,
  validation, artifact, acceptance, ledger projection, or checkpoint can
  reopen and finish exactly once.
- Stable work IDs survive Resume; physical attempt IDs cannot be resealed with
  different bytes.
- A stale projection caused by the last durable journal write is repaired, but
  arbitrary projection, assignment, checkpoint, artifact, or hash drift fails
  closed.
- Previously accepted work is reused and usage is not double-counted.
- A recoverable interruption emits exactly one warning-level halt, a Resume
  checkpoint, and no synthetic `validation_failed` event.
- Internal incident text is path/credential redacted.
- Existing component replay, terminal behavior, and semantic validators remain
  compatible.
- Focused and broad tests, compilation, diff check, and secret scan pass with
  zero API calls.

## 6. Non-Goals

- Selective semantic repair or replacement of accepted scorer outputs.
- Changes to models, prompts, rubrics, scoring algorithms, source inputs, or
  translations.
- App/backend/relay/UI changes.
- Live provider calls or production-readiness claims.
