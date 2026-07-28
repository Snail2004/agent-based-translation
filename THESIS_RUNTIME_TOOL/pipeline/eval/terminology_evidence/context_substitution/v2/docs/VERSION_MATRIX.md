# Context Substitution V2.2 Version Matrix

| Layer | Active identity | Notes |
|---|---|---|
| Package path | `context_substitution/v2` | Stable major namespace; C-owned only |
| Runtime run schema | `D2LContextSubstitutionRunV2` / `2.2.0` | Selection authority, raw replay, and calibration bindings |
| Runtime input schema | `D2LContextSubstitutionInputV2` / `2.2.0` | V3/Pilot origin plus reviewed-selection contract |
| Reviewed adapter | `D2LContextSubstitutionReviewedSupportAdapterV1` / `1.0.0` | Reads V3 and Pilot V1.1 directories or ZIPs |
| Adapter receipt | `D2LContextSubstitutionAdapterReceiptV1` / `1.0.0` | Self-hashed, zero-API, decision-neutral |
| Support freeze schema | `D2LContextSupportSetFreezeV1` / `1.0.0` | Legacy immutable preparation contract remains supported |
| Gold evaluation schema | `D2LContextSubstitutionGoldEvaluationV2` / `2.0.0` | Evaluation-only; unchanged |
| Calibration artifact | `CSTCalibrationArtifactV1` / `1.0.0` | Nonzero dataset/gold hashes and measured precision floor |
| Selector | `d2l_context_selector_v2_1` | Model-classified development or frozen human-reviewed rows |
| Trial translator | `d2l_context_trial_translator_v2_1` | Literal/expansion surface binding |
| Trial quality gate | `d2l_trial_translation_quality_gate_v2_1` | External-error separation |
| Context Judge | `d2l_context_judge_v2_1` | Pinned model identity; no Gemini-only restriction |
| Aggregation | `d2l_context_aggregate_normalized_v2_1` | Missing contrastive/type coverage cannot become eligible |
| Raw replay | `CONTENT_ADDRESSED_V1` | Required for frozen execution |
| Development threshold policy | `d2l_context_status_development_heuristic_v2_1` | Cannot authorize frozen test-set evidence |
| Frozen threshold policy | Calibration-artifact-selected version | Requires exact calibration artifact ref/hash |
| CLI module | `pipeline.scripts.terminology_evidence.context_substitution.v2.run` | Freeze, validate, adapt, run, project, and gold-evaluate |

Legacy V2.1 payloads are not silently reinterpreted as V2.2. Legacy term-evidence
V1 remains a compatibility projection and rejects lossy merged two-judge
evidence.
