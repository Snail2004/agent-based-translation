# TASK_EVAL_LIVE_PILOT_CAPABILITY_RUNNER_V1

## Objective

Provide a bounded Evaluation-owned runner for qualifying the exact live-pilot
response schemas before any real scoring call is allowed.

## Scope

- Reuse `SharedLlmCapabilityProbe`; do not implement another transport.
- Run exactly these roles in order:
  1. `evaluation.sf_bt.back_translator`
  2. `evaluation.sf_bt.semantic_judge`
  3. `evaluation.pj.judge`
- Use one explicitly selected physical API row and one external credential.
- Permit no hidden retry, provider/model/row switch, fallback or cache reuse.
- Halt after the first failed capability result.
- Persist request hash material, probe seal, receipt and capability evidence in
  an isolated run root without plaintext credentials.
- Expose qualified evidence only when all three roles pass.

## Live policy

- Native Structured Output is legal only on a direct official Google or
  OpenAI source qualified for the exact source revision, route, model, schema
  and local validator.
- The first planned run uses one official Gemini Free physical row. Row/model
  values are CLI inputs, not production defaults.
- At most three physical calls are possible. Every role has one call and zero
  retry.
- Gold, oracle, human reference and evaluation results never enter the prompt,
  source binding or capability evidence.

## Gate

Before live execution:

- clean Evaluation worktree and committed implementation binding;
- fake-transport success, fail-closed and secret-scan tests pass;
- selected key row is read only in memory and matches the sealed commitment;
- output root is absent or empty;
- no active lease exists for the selected physical bucket.

Live success means three exact `qualified` evidence rows. It does not yet mean
the four-unit scoring pilot is complete; local COMET readiness and the sealed
live profile remain separate gates.
