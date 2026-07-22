# TASK_EVAL_FULL_RUN_REPORT_WRITER_C2B_V1

Status: COMPLETE

## Objective

Compose the accepted `FullRunReportV1` projection from a sealed Evaluation
execution and explicit provenance facts, then publish it at the App-owned fixed
read path:

```text
<run-root>/reports/full_run_report_v1.json
```

## Authority boundary

The composer may derive metric rows, coverage, comparison rows and report state
from `EvaluationExecutionArtifactV1`. It must not infer:

- baseline/candidate/reference labels or kinds;
- Evaluation input or translation artifact paths;
- provider/model/quota/credential usage facts;
- a BETTER or NOT_BETTER claim;
- stage timing that was not persisted.

Those facts are explicit inputs. Usage remains null when unavailable. Stage
timing remains null until a later persisted timing/usage projection supplies
it. One-arm reports publish `NOT_APPLICABLE`; multi-arm reports remain
`INCONCLUSIVE / claim_policy_not_frozen`.

One metric may map to multiple persisted stages. This preserves separate
back-translation and semantic-judge usage/model facts instead of collapsing
them into one invented model identity. A method-level model ID is accepted only
when persisted usage evidence names exactly that one model; multi-model methods
publish `model_id=null` and retain concrete model identities by stage.

## Scope

- `pipeline/eval/full_run_report_writer_v1.py`;
- `pipeline/tests/test_full_run_report_writer_v1.py`;
- this task record.

No public contract, shared backend, App, source package, translation runtime,
SQLite scorer, model profile, prompt, API, or claim policy is changed.

## Persistence

The writer first loads and validates the complete C2A execution bundle. Every
artifact declared `present` must exist beneath the same run root. The report is
canonical UTF-8/LF JSON and is atomically published with create-if-absent
semantics. Identical reruns reuse exact bytes; conflicting bytes fail closed.

## Verification

- focused report-composer/writer probes: `13 passed`;
- Evaluation/shared-contract regression: `401 passed`;
- full `pipeline/tests`: `1279 passed`;
- full regression used a temporary read-only frozen DB copy; SHA-256 before and
  after remained
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- temporary DB copy removed;
- API/network/credential calls: `0`;
- source/runtime DB writes: `0`.
