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
- Expose one high-level production preparation call that accepts only the
  registered template, exact five-arm handoff, explicit producer file map,
  locked selection, run identities, output roots, and server runtime config.
- Derive the benchmark manifest, preflight, five arm overlays, chapter configs,
  presentations, pairwise plan, and runtime bindings inside Evaluation.
- Build the process-local runtime registry from the registered local SF-QE
  runtime and sealed shared-LLM profile/source/capability/credential references.
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
- When PJ is selected for all five arms, derive the complete unordered
  round-robin of ten arm pairs; do not silently reduce the comparison to
  S1-versus-all.

## Boundaries

- Zero API calls in this task.
- No provider, model, credential, fallback, or retry selection.
- No credential bytes in templates, bundles, identities, reports, or tests of
  persisted output. Credentials remain behind the server-owned resolver.
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
- High-level preparation is byte-stable on Resume and reuses the same settings
  hash and runtime identity without invoking a provider.
- The caller is not required or allowed to synthesize Evaluation-owned manifests,
  preflight rows, overlays, chapter configs, presentations, or runtime bindings.
- Focused and Evaluation regression tests pass with no API or shared-state writes.

## Production callable

`prepare_evaluation_production_runtime_v1(...)` is the production boundary used
after Translation. It:

1. loads the baseline template only through `workflow_runtime_v1`;
2. validates the exact parent handoff and explicit producer files;
3. materializes and reuses `EvaluationWorkflowSettingsV1@1.1.0`;
4. derives all Evaluation-owned benchmark inputs;
5. materializes the run-specific runtime bundle; and
6. returns a concrete executor runtime ready for the existing benchmark runner.

The server supplies `EvaluationServerRuntimeConfigV1`: the registered local
SF-QE predictor, sealed Evaluation LLM profile, exact API source and capability
records, external credential resolver, sender, and cache/clock dependencies.
Evaluation constructs `EvaluationRuntimeObjectRegistryV1` and attempt ledgers
itself. Missing, foreign, or expanded runtime authority fails before a provider
call. The returned bundle stores only IDs and commitments, never live objects or
credential material.
