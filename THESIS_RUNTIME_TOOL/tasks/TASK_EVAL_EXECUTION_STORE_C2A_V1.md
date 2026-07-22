# TASK_EVAL_EXECUTION_STORE_C2A_V1

Status: COMPLETE

## Objective

Persist the sealed Evaluation config and `EvaluationExecutionArtifactV1`
atomically and immutably before report composition. This closes crash/restart
integrity without inventing source-artifact or usage metadata that is no longer
present in `CommonEvaluationInputV1`.

## Scope

- `pipeline/eval/execution_store_v1.py`
- `pipeline/tests/test_evaluation_execution_store_v1.py`
- this task record

No API, model, credential, DB, source package, App, shared-core, scorer-policy,
translation artifact, public input or FullRunReport contract is changed.

## Bundle layout

```text
<evaluation-run-root>/
  manifest.json
  config/<config_sha256>.json
  execution/<execution_artifact_sha256>.json
```

The manifest binds project/document, config, input set, plan and execution
identities. Config and execution paths are content-addressed. Immutable files
are written to a same-directory temporary file, flushed, fsynced and atomically
renamed. The manifest is written last, so an interrupted pre-manifest write can
resume safely.

Existing identical bytes are reused. Existing conflicting content, stale
bindings, foreign config/execution pairs, unsafe paths, self-hash drift and
tampering fail closed; no file is overwritten to make a run appear complete.
Publishing uses an atomic create-if-absent hard link rather than a replacing
rename, so a concurrent writer cannot overwrite an immutable artifact. Once
`manifest.json` exists it is the commit marker: a rerun must load the complete
bundle and match that manifest before it may report reuse.

## Deliberate report boundary

`FullRunReportV1` is not emitted in C2A. A truthful report still needs explicit
persisted facts that the common read-model does not carry:

- source Evaluation-input artifact path/hash;
- exact translation artifact path/hash and arm role/kind/label;
- persisted shared-attempt usage projection, including unknown values;
- Evaluation attempt/stage timing identity.

Those facts must enter a bounded report-writer input contract or be supplied by
producer adapters. They must not be guessed from arm labels, recomputed by App,
or fabricated from expected call counts.

## Verification

- focused execution-store probes: `9 passed`;
- Evaluation/shared-contract regression: `388 passed`;
- full `pipeline/tests`: `1266 passed`;
- full regression used a temporary read-only frozen DB copy; SHA-256 before and
  after remained
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- temporary DB copy removed;
- API/network/credential calls: `0`;
- source/runtime DB writes: `0`.
