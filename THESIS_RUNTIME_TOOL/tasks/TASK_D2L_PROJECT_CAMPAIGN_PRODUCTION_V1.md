# D2L Project Campaign Production V1

## 1. Objective

Integrate the current terminology and translation pipeline into the managed App
without importing the historical D2L branch ancestry.

The production flow is:

```text
B1 candidate discovery
-> deterministic Candidate Index
-> B2 term admission and translation proposals
-> morphology / target-collision / multi-target auditors
-> immutable glossary seal
-> Translator S0 and S1
-> non-blocking Translation Quality Audit
-> TranslationArtifactV1 S0/S1
-> scoring_handoff_fragment_v1
```

The final handoff is packaging only. D2L does not calculate TA, TC,
TA-Registry, benchmark scores, gates, verdicts, or comparisons with community,
Google, or Web translations. Evaluation owns those decisions.

## 2. Runtime Authority

- App script ID: `run_d2l_project_campaign`.
- Legacy `run_one_button` remains byte-compatible and is not the production
  D2L entrypoint.
- The campaign accepts an explicit chapter allowlist and preserves source
  order. It never infers scope from a project name.
- One campaign seals source/package identity, selected block universe, code
  revision, semantic role/model/profile values, output mode, hard limits, and
  component plan before the first live request.
- A model/profile/semantic-setting change requires a new campaign. A physical
  API source change is allowed only as a new transport-attempt seal supporting
  the already sealed model and capability.
- Third-party API routes use prompt-validated JSON and the unchanged local
  validator. They never send or claim authoritative native JSON Schema.
- Source project packages and their production SQLite are read-only. Work DB,
  response cache, checkpoint, output, and component package use isolated roots.
- D2L writes only its component event stream. The neutral relay is the only
  parent workflow/global-sequence writer.

## 3. Stage Responsibilities

1. `preflight`: validate the finalized project package, chapter allowlist,
   admitted projection, exact source order, code revision, campaign seal, and
   token/call/cost limits.
2. `b1_candidate_discovery`: scan semantic source windows and return exact
   source surfaces only. B1 does not decide final termhood.
3. `candidate_index`: code exact-locates occurrences, deduplicates observations,
   and emits proposal-timeline rows. Code does not decide language meaning.
4. `b2_admission_translation`: decide term admission and propose Vietnamese
   target candidates from bounded source evidence.
5. `auditor_morphology`: resolve source-form and morphological components.
6. `auditor_target_collision`: resolve unsafe target collisions.
7. `auditor_multi_target`: retain only justified context-sensitive target
   variants.
8. `glossary_seal`: code publishes an immutable run-local glossary and durable
   terminology delta receipt.
9. `translator`: produce exact-cover S0 and S1 TranslationArtifactV1 outputs;
   structured/preserve/review-held blocks follow admission policy.
10. `translation_quality_audit`: detect source/target defects and publish
    non-blocking issue observations. It does not score the benchmark.
11. `scoring_handoff_fragment`: package only the D2L-owned S0/S1 bindings for
    the relay and Evaluation. UI label: `Ban giao sang cham diem`.

## 4. Console Replay

- Raw B1 proposals are observable as proposal artifacts/events, not committed
  glossary changes.
- B2 and Auditor decisions are observable with request, response, validation,
  usage, retry, and artifact receipts.
- The memory ledger shows only durable glossary changes from `glossary_seal`.
- Translation previews and quality issues are indexed by stage and block.
- Resume keeps `component_run_id`, increments `component_attempt_id`, validates
  the complete runner-plan hash, and appends one exact `run_resumed` boundary.

## 5. Exact File Manifest Before First Commit

### 5.1 Accepted semantic and replay contracts projected as committed bytes

```text
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_candidate_discovery_v2.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_candidate_proposal_timeline_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_2.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_3.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_4.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_5.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_6.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consistency_contract_v3_7.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_packet_contract_v2.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_packet_plan_v2.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consolidation_contract_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_consolidation_plan_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_target_collision_plan_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_target_collision_apply_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_multi_target_contract_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_b2_multi_target_plan_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_console_replay_contract_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_component_stage_receipt_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_terminology_memory_delta_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_translation_component_runner_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_shared_llm_adapter_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_shared_llm_profiles_v1.py
THESIS_RUNTIME_TOOL/pipeline/scripts/build_d2l_console_replay_fixtures_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_latex_markup_line_protected_spans_v4.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_latex_markup_protected_spans_v3.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_latex_protected_spans_v2.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_prompt_json_envelope_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_prompt_json_envelope_v2.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_protected_span_policies.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_protected_spans_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_quality_gates_v2.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_soft_glossary_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_quality_auditor_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_quality_auditor_v2.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_quality_observation_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_quality_state_v1.py
THESIS_RUNTIME_TOOL/pipeline/translate/d2l_translation_slots_v1.py
```

These modules are contracts and deterministic transforms only. Historical
provider-specific runners, probes, canaries, report roots, scorer modules, and
gold/evaluation data are excluded.

### 5.2 New production D2L files

```text
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_project_campaign_v2.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_project_live_executor_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_project_stage_runner_v1.py
THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_project_transport_v1.py
THESIS_RUNTIME_TOOL/pipeline/scripts/run_d2l_project_campaign.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_project_campaign_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_project_live_executor_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_project_stage_runner_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_project_transport_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_project_campaign_observability_v1.py
THESIS_RUNTIME_TOOL/tasks/TASK_D2L_PROJECT_CAMPAIGN_PRODUCTION_V1.md
```

### 5.3 Accepted contract regressions projected as committed bytes or portable clean-rebase deltas

```text
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consistency_contract_v3.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consistency_contract_v3_4.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consistency_contract_v3_5.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consistency_contract_v3_6.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consistency_contract_v3_7.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_packet_contract_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_packet_plan_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consolidation_contract_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_consolidation_plan_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_target_collision_plan_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_target_collision_apply_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_b2_multi_target_stage3_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_console_replay_contract_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_component_stage_receipt_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_terminology_memory_delta_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_component_runner_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_component_observability_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_observation_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_shared_llm_adapter_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_shared_llm_profiles_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_latex_markup_line_protected_spans_v4.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_latex_markup_protected_spans_v3.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_latex_protected_spans_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_prompt_json_envelope_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_prompt_json_envelope_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_protected_spans_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_quality_gates_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_soft_glossary_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_auditor_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_auditor_v2.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_quality_state_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_slots_v1.py
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/artifact_index.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/artifacts/glossary.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/artifacts/translation_s0.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/artifacts/translation_s1.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/component_manifest.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/events.jsonl
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/manifest_revisions/11ED8F35CE98054B86EDF446CDDD78C2BEE15B0125CFB4FD718D6EC8A8FC8C08.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/manifest_revisions/A6F1C23F7E938A1FDDAEC0F4B5B95BC1D9BBE627E5D176F8FECB627D05113626.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/scoring_handoff_fragment.json
THESIS_RUNTIME_TOOL/pipeline/tests/fixtures/d2l_console_replay_v1/translation_component/validation.json
THESIS_RUNTIME_TOOL/tasks/TASK_D2L_TRANSLATOR_QUALITY_AUDITOR_V1.md
```

The consolidation-plan test skips its optional historical live-evidence probe
when report artifacts are not distributed. The replay fixture comparison
canonicalizes CRLF to LF so Git checkout policy cannot falsify deterministic
JSON bytes. All synthetic and adversarial assertions remain active.

### 5.4 DEC-062 shared-path reservation

```text
THESIS_RUNTIME_TOOL/pipeline/translate/runner.py
THESIS_RUNTIME_TOOL/pipeline/translate/profiles.py
THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py
THESIS_RUNTIME_TOOL/pipeline/scripts/run_translate.py
THESIS_RUNTIME_TOOL/pipeline/retrieval/context_builder.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_translate_runner.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_run_translate_script.py
THESIS_RUNTIME_TOOL/pipeline/tests/test_context_builder.py
THESIS_RUNTIME_TOOL/app/backend/services/thesis_runs.py
THESIS_RUNTIME_TOOL/app/backend/routes/thesis_runs.py
THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_runs.py
THESIS_RUNTIME_TOOL/app/prototype/app.jsx
```

Any additional path is a mandatory stop and requires a manifest/reservation
amendment before it is edited or committed.

## 6. Required Gates

1. Exact manifest and accepted-blob provenance check.
2. Prompt/schema/local-validator regressions for B1, B2, Auditors, Translator,
   and Translation Quality Audit.
3. Fake transport: success, invalid JSON, semantic retry, transport failure,
   cache hit, stale seal, foreign provider/model, and hard-cap halt.
4. Five-chapter dry run with exact 2,355-block source order and admission
   coverage, including 28 `review_required` blocks held explicitly.
5. Full eleven-stage synthetic component with proposal timeline, committed
   glossary delta, S0/S1 artifacts, quality issues, and packaging-only handoff.
   Synthetic translation artifacts use the explicit
   `dry_no_api_identity_projection_v1` profile in an isolated campaign. They
   test mechanics only and are never publication- or Evaluation-authoritative.
6. Pause/resume after every stage boundary; argv and material-plan drift must
   fail before mutation or child execution.
7. App API tests prove server-owned argv and isolated roots. Legacy
   `run_one_button` remains unchanged.
8. Browser QA verifies chapter selection, forecast versus reserve labels,
   progress/replay, proposal/watchlist/committed glossary views, translation
   preview, quality issues, and handoff status without overlapping UI.
9. Full App/pipeline/D2L/Evaluation relay regressions, diff check, credential
   scan, and frozen source/project DB hash before/after.

## 7. Live Boundary

This task remains 0-API through implementation and replay gates. A live
five-chapter campaign is a separate user-scheduled action after the production
runner, App wiring, and dry component package pass.
