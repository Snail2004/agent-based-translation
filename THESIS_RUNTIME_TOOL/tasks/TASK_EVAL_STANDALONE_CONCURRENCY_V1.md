# TASK_EVAL_STANDALONE_CONCURRENCY_V1

## Scope

Prepare the standalone Evaluation path for a sealed D2LEvaluationInputV1 ZIP
without invoking App, workflow relay, provider API, or source/runtime databases.

The selected chapters remain the previously locked five, in this exact order:

1. `d2l_preliminaries`
2. `d2l_linear_networks`
3. `d2l_multilayer_perceptrons`
4. `d2l_deep_learning_computation`
5. `d2l_convolutional_neural_networks`

## Locked execution lanes

- `local`: local SF-QE and deterministic metric work.
- `shop`: one active physical ShopAPIKey quota bucket for SF-BT reverse work.
- `ckey`: one active physical CKEY quota bucket for SF-BT semantic and MTQ-5.

Distinct lanes may overlap. One physical quota bucket never has more than one
active call. Adding a second physical key requires a new sealed lane and
assignment hash; it is not silent rotation.

## Checkpoint contract

Each work item binds its scorer input and exact execution assignment by SHA-256.
Workers return locally validated scorer artifact bytes. A single scheduler
writer persists immutable per-attempt artifacts and receipts. Resume reuses
accepted receipts, retries only failed/pending work, and rejects profile, plan,
input, or artifact drift before executor work.

The scheduler does not claim that a process crash after an external response but
before a validated artifact return prevents a duplicate provider charge. Shared
backend attempt evidence remains the authority for physical-call accounting.

## ZIP preflight

`pipeline.scripts.run_evaluation_standalone_v1 preflight-pack` verifies the ZIP
path structure, required nine files, D2LEvaluationInputV1 self-hash, exact S0/S1
arms, and the caller-declared ordered chapter selection without extracting it.

## Deferred until the handoff arrives

- Bind the ZIP source/package hash to the already captured Community, Google
  NMT, and LLM-LC baselines.
- Derive the exact five-chapter common five-arm universe.
- Seal the five-chapter sample/metric plan and concrete source/model profiles.
- Execute `--dry-run` before any separately authorized live scoring run.
