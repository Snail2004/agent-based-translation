# TASK_EVAL_D2L_PACKAGE_ADAPTER_V1

## Objective

Project a validated `D2LEvaluationInputV1` producer package into the existing
`CommonEvaluationInputV1` offline read model so Evaluation can consume the MLP
S0/S1 run without reopening the D2L database or inventing producer artifacts.

## Exact scope

- Add one read-only adapter.
- Add focused fixture tests.
- Do not change either public schema.
- Do not modify D2L producer files, App files, scorers, database, or runtime.
- Make zero API calls and zero database writes.

## Closed mappings

| Common field | D2L producer fact |
|---|---|
| `artifact_id` | `arms[].translation_artifact_id` |
| `artifact_sha256` | `arms[].translation_sha256` |
| `logical_run_id` | `identity.logical_run_id` |
| `attempt_run_id` | `identity.experiment_id` |
| `profile_id` | `runtime_profile.profile_id` |
| `profile_config_sha256` | SHA-256 of the referenced `runtime_profile` artifact |
| languages | `runtime_profile.source_language/target_language` |
| `passthrough` | Common status `preserved` |
| source `exclude` | Common status `excluded`, with no invented target text |

The adapter retains the explicit `legacy_d2l` source binding. It does not claim
that the package is a canonical DEC-017 source package and does not emit a
public `TranslationArtifactV1` on behalf of D2L.

## Acceptance

1. The public D2L validator runs before projection.
2. Input is not mutated.
3. Source and translation semantic order are deterministic.
4. Every arm exact-covers the common source universe, including mechanical
   `excluded` rows where D2L intentionally omits translations.
5. Translation text is byte-faithful.
6. Invalid experiment identity, artifact reference, or translation hash fails
   closed.
