# TASK_EVAL_D2L_MLP_S0S1_DRY_PLAN_V1

Status: COMPLETE - 0 API

Owner: Evaluation workstream

## Objective

Materialize the first real, deterministic Evaluation plan over the sealed D2L
MLP S0/S1 package. This milestone proves unit selection, job allocation,
coverage, blinding, checkpoint, and retry mechanics. It does not render a
prompt or produce a quality score.

## Locked pilot configuration

- selected arms: `S0`, `S1`;
- scoring universe: admissions `translate|translate_structured`;
- unit: one canonical source block;
- bounded context: one preceding and one following same-chapter block;
- planned unary methods: `sf_qe`, `sf_bt`;
- planned pairwise method: `pj`;
- explicit comparison pair: `S0` versus `S1` only;
- pair presentation: opaque and deterministically counterbalanced;
- transport attempts: at most `2`;
- all method versions: `planning-v1` until prompts and model profiles are
  independently frozen.

## Expected real counts

- source blocks: `641`;
- eligible units: `475`;
- not-applicable preserved units: `166`;
- SF-QE jobs: `950`;
- SF-BT jobs: `950`;
- PJ jobs: `475`;
- total ready jobs: `2,375`;
- blocked jobs: `0`.

## Fixture canary

Run the existing crash-safe fixture executor against the real sealed plan for
three jobs. Exercise one transport retry, one response-contract retry, and one
first-attempt success. Fixture outputs contain no score or semantic judgment.

## Hard boundaries

- no API, provider, model, prompt, scorer, or token estimate;
- no database read/write;
- no community target in this S0/S1 plan;
- no TC/TA recomputation;
- no App or public-contract edit;
- no score, winner, recommendation, or accepted-quality claim.

## Next gate

After this dry plan is verified, author and review scorer-specific prompt and
response contracts. Only then may Evaluation dry-render tokens and request an
API allocation.

## Result

- plan ID: `plan-c71603fc543364bea1e4da4b`;
- plan SHA-256:
  `E9956B875E8AF3A596FC1F8067ACEEFC89C803CFB8EC30717AE39EC06399FBA5`;
- ready jobs: `2,375`; blocked jobs: `0`;
- PJ presentation balance: `S0,S1 = 238`, `S1,S0 = 237`;
- fixture canary: `3` succeeded jobs from `5` persisted attempts, with both
  injected failure classes recovered on attempt 2;
- Evaluation test gate: `111 passed`.
