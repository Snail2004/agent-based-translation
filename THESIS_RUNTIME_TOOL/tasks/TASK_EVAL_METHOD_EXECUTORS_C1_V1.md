# TASK_EVAL_METHOD_EXECUTORS_C1_V1

Status: COMPLETE

## Objective

Connect the common Evaluation execution runner to its real method boundaries
without allocating or calling a live provider:

- SF-QE remains a local, injected reference-free scorer;
- SF-BT back-translation and semantic comparison use the existing Evaluation
  prompts, validators, stage contracts, and thin shared-backend adapter;
- PJ uses the existing blinded prompt in both candidate orders;
- deterministic scorers, aggregation, and report policy remain code-only.

This milestone is a `0-API` fake-transport integration gate. It does not choose
models, credentials, prices, thresholds, or a headline claim.

## Scope

Owned implementation:

- `pipeline/eval/method_executors_v1.py`
- `pipeline/tests/test_evaluation_method_executors_v1.py`
- this task record

No changes are permitted to shared LLM core, App UI, D2L/Literary runtime,
SQLite, held scorers, `pipeline/eval/__init__.py`, public Evaluation input/report
contracts, source packages, or translation artifacts.

## Execution boundary

`SharedEvaluationRoleRunnerV1` receives a concrete Evaluation profile and the
exact source/capability records referenced by that profile. For one role call it:

1. renders the protocol-specific request through the established adapter;
2. seals the scorer packet, rendered prompt, transport body, and optional stage
   dependency hashes;
3. resolves exactly one shared-backend run seal;
4. executes exactly one physical attempt;
5. applies the existing local semantic validator.

It performs no retry, provider/model fallback, key rotation, model selection,
or credential discovery.

## Method behavior

### SF-QE

The executor passes only active source and active target text to an injected
local scorer. The returned raw score is not rescaled or interpreted. Non-finite
or out-of-range observations fail the job and stay in the denominator.

The historical COMET SQLite script is not imported because it owns legacy data
loading, model download, batching, and report writing. A later local adapter may
reuse its pinned model behavior without importing those side effects into the
common runner.

### SF-BT

1. The back-translator sees Vietnamese target context only.
2. The accepted reverse response is bound to its shared attempt/cache lineage.
3. Code constructs the existing stage-2 packet from the original English active
   block and the back-translation.
4. A deterministic packet-hash bit balances which passage occupies slot A.
5. The semantic judge returns the existing closed score-band result.

An exact response-cache hit re-derives the producer physical-attempt identity
from the seal. It does not mint a new pseudo-attempt ID or change the stage-1
result hash.

### PJ

- Mechanically equal displayed sequences produce a tie with zero model calls.
- Non-equal sequences are judged in canonical and reversed candidate order.
- Reversed verdicts are mapped back to the original opaque slots.
- Agreement yields the agreed result.
- Mixed tie or opposite winners resolve conservatively to tie.
- Missing or invalid evidence from either leg fails the job; it is never
  fabricated as a tie.

Per-presentation provenance remains in the shared attempt ledger. Publishing
the detailed order-origin diagnostics in a report is deferred to the report
milestone; this C1 artifact still keeps the global claim `INCONCLUSIVE`.

## Fail-closed gates

- foreign/stale scorer packets are rejected before scoring;
- source text is absent from the SF-BT reverse prompt;
- provider transport failure performs one attempt and halts, with no hidden
  retry or fallback;
- semantic schema rejection becomes a failed denominator row;
- invalid local scores do not become zero or a tie;
- exact cache reuse preserves stage lineage;
- prompts expose neither arm IDs nor producer identities.

## Deferred

- live API allocation and model choice;
- current COMET checkpoint verification and a batch local adapter;
- reverse-order SF-BT audit sampling and publication gate;
- detailed PJ decision-origin persistence;
- checkpoint/resume and atomic artifact writer;
- `FullRunReportV1` composition and claim policy;
- App/Console/Cockpit wiring.

## Verification result

- method-executor fake-transport and adversarial probes: `12 passed`;
- related runner, packet, prompt, SF-BT stage, shared adapter, planner and
  shared-backend tests: `147 passed`;
- full `pipeline/tests`: `1257 passed in 174.39s`;
- temporary read-only frozen DB hash before and after:
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`;
- temporary DB mount removed after the gate;
- Python compilation and `git diff --check` passed; `ruff` was unavailable;
- API/network/credential calls: `0`; source/runtime DB writes: `0`; App,
  checkpoint and source-package mutations: `0`.
