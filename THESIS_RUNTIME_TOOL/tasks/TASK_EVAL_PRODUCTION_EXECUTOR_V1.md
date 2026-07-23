# TASK_EVAL_PRODUCTION_EXECUTOR_V1

## Goal

Provide the smallest Evaluation-owned preparation and execution boundary
required by the neutral workflow orchestrator. The boundary separates:

1. a pre-run `WorkflowScoringBaselineTemplateV1` containing only registered
   Evaluation authority and the three external baselines; and
2. a run-specific `WorkflowScoringRuntimeBundleV1` materialized only after the
   exact five-arm `ScoringHandoffV1` and
   `EvaluationWorkflowSettingsV1@1.1.0` exist.

## Scope

- Add a structurally compatible `EvaluationExecutorV1` implementation.
- Load the pre-run template only through the exact
  `workflow_runtime_v1.baseline_bundle` binding, without directory scans.
- Materialize Settings 1.1 once from registered authority plus the locked
  selection, then require the executor to reuse the exact settings hash.
- Materialize and load a run-specific, content-addressed file bundle.
- Keep `workflow_run_id` and `component_run_id` stable across Resume.
- Verify the registered settings option and selection hash before preparing work.
- Require the input provider to echo the exact handoff and materialized settings.
- Reuse the existing runner for events, checkpoints, usage, reports, and receipt.
- Publish a nonterminal component snapshot before raising `WorkflowComponentPausedV1`.
- Integrate the accepted five-chapter Community alignment implementation.
- Accept the D2L producer's finalized Canonical Source Package plus exact S0/S1
  `TranslationArtifactV1` objects directly, while preserving explicit legacy
  `D2LEvaluationInputV1` read compatibility.
- Keep canonical and legacy source bindings distinct. Mixed, foreign, stale, or
  tampered bindings fail closed.
- Require every external baseline projection to exact-cover the same selected
  chapter/block universe before any scorer starts. In particular, the historical
  143-block failed GPT Web capture is evidence only and can never be registered
  as the `llm_lc` arm for the five-chapter benchmark.

## Boundaries

- Zero API calls in this task.
- No provider, model, credential, fallback, or retry selection.
- No directory scan or inference from filenames.
- No App, D2L, neutral relay, DB, source package, or live output edits.
- No scoring semantic, prompt, validator, aggregation, or report contract changes.
- No relabeling of canonical Source Package hashes as legacy
  `source_db_sha256` or `runtime_manifest_sha256`.

## Acceptance

- Exact handoff and Settings 1.1 bindings are fail-closed.
- Canonical Source Package and legacy D2L input modes both pass independently;
  cross-kind substitution and component-hash drift fail closed.
- A successful run emits a terminal Evaluation component package and report.
- A failed chapter emits a replayable halt/checkpoint and resumes under attempt 2
  without rerunning completed chapters.
- Selection hash drift, foreign workflow, and foreign provider echoes are rejected
  before semantic scoring.
- Focused and Evaluation regression tests pass with no API or shared-state writes.

## Remaining integration dependency

The canonical D2L bridge and file-backed executor are implemented here. The
runtime bundle deliberately binds runtime object IDs rather than serializing
live Python objects or credentials. The App integration layer must supply an
explicit server-owned `EvaluationRuntimeObjectRegistryV1` that resolves those
registered IDs to:

- the local SF-QE predictor;
- the already sealed Evaluation LLM role runners;
- their shared attempt ledgers.

That registry is an injected process-local dependency, not data inferred from a
directory and not credential material stored in the runtime bundle. A missing or
foreign runtime ID fails before scoring; no provider, model, credential, or
fallback is selected by this task.
