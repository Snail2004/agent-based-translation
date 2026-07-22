# TASK_EVAL_QUOTA_SAFE_CANARY_V1

## Status

Implemented offline. No provider call is authorized or performed by this task.

## Purpose

The first four-unit live calibration pilot planned 24 API calls and halted
fail-closed after 21 successful calls when one official-Google request returned
HTTP 429. The historical root is terminal evidence and must not be resumed.

This task adds a smaller, explicitly versioned calibration contract. It reduces
the next fresh canary to 18 API calls without changing prompts, methods, models,
validators, score semantics, retry semantics, or publication authority.

## Contract

`EvaluationLivePilotPreflightV1` retains schema version `1.0.0`:

- minimum four units;
- selection algorithm `source_length_quartile_hash_v1`;
- existing artifacts and validators remain valid.

The quota-safe canary uses schema version `1.1.0`:

- exactly three units;
- selection algorithm `source_length_tertile_hash_v1`;
- one deterministic unit from each source-length tertile;
- canonical source order is preserved after selection;
- producer component `live_pilot_canary_preflight_v1`;
- workload remains two-arm `SF-QE + SF-BT + PJ`.

The v1.1 workload is closed at:

- 3 selected units;
- 15 plan jobs;
- 6 local SF-QE rows;
- 6 back-translation calls;
- 6 SF-BT semantic-judge calls;
- 6 PJ judge calls;
- 18 total API calls.

## Authority And Safety

- The canary is calibration-only and must publish `INCONCLUSIVE` even when all
  calls succeed.
- It cannot support a headline quality claim or final winner.
- `1.0.0` still rejects a three-unit request.
- Schema versions cannot be relabelled and resealed across their distinct
  selection algorithms or count constraints.
- Binding validation reconstructs the exact versioned selection from source,
  config, seed, and producer commit.
- No retry, provider/model/row rotation, output-mode fallback, cache salvage, or
  continuation of a terminal root is introduced.
- A future live canary requires a fresh root, run identity, profile revision,
  exact capability evidence, and a user-assigned physical quota bucket.
- The official-Google live runner requires an explicit
  `--structured-output-mode required`. It cannot start a new load-bearing run
  with historical `preferred` mode or silently downgrade the output envelope.
- Mode-specific role preset revision `v2-required` distinguishes the new
  profile bytes from historical `preferred` evidence.

## Scope

Owned changes are limited to Evaluation preflight logic, Evaluation tests, and
this task record. Shared LLM backend, App UI, source/runtime memory, translation
artifacts, FullRunReportV1, D2LEvaluationInputV1, deterministic scorers, and
provider credentials are unchanged.

## Acceptance

1. All legacy preflight tests remain green.
2. A v1.1 canary selects exactly one short, one middle, and one long unit.
3. The derived workload reports exactly 18 API calls.
4. Fake-transport end-to-end execution completes 15 jobs and remains
   `calibration_only / INCONCLUSIVE`.
5. Legacy v1.0 rejects three units.
6. Cross-version relabelling fails closed after a valid reseal.
7. No API, credential read, DB mutation, or source/translation mutation occurs
   during implementation and verification.
8. A fake official-Google execution seals `required` for every semantic role;
   unknown output-mode labels fail closed.
