# TASK_EVAL_SOURCE_INTEGRATION_V1

Status: READY_FOR_REVIEW

## Objective

Integrate the accepted Evaluation pipeline implementation into a clean branch
based on source-main without merging the mixed Evaluation branch history.

## Inputs

- source-main base: `78e0552823cce5209492c24ad3fee2a78aedc6d7`
- accepted Evaluation tree: `f36d644d799e3d0cf235deeb285299a31b22626c`
- neutral workflow replay relay already present in source-main:
  `42a75ba67a62e430c51c30c9b20f620c6d3b886e`

## Included Scope

- additive Evaluation runtime modules under `pipeline/eval/`;
- Evaluation-owned CLI entry points under `pipeline/scripts/`;
- deterministic fixtures under `data/eval/` and `pipeline/tests/fixtures/`;
- Evaluation tests and historical task specifications needed to preserve the
  implemented contract lineage;
- one test-only repair that replaces a dependency on a committed live report
  root with synthetic source and capability records.

## Excluded Scope

- historical live API reports, raw responses, ledgers, checkpoints, and SQLite
  files under `data/reports/`;
- App routes, buttons, modal wiring, or workflow launcher behavior;
- D2L, Literary, Input Normalization, source-package, translation, or memory
  runtime changes;
- shared LLM backend changes or version downgrades;
- API calls, credential reads, provider qualification, and live scoring.

## Integration Rules

1. Source-main remains untouched until this branch passes independent review.
2. Evaluation branch history is not merged because it contains unrelated
   workstream ancestry. Only the explicitly owned file tree is imported.
3. Existing source-main contracts and shared LLM backend files remain
   authoritative when byte-identical or newer.
4. Report roots are runtime artifacts and must not be used as test fixtures.
5. This milestone makes the Evaluation backend available in source history; it
   does not claim that the App can launch the five-arm workflow.

## Verification

- Evaluation tests excluding the separately executed CometKiwi subprocess
  contract: `514 passed`;
- CometKiwi subprocess contract: `11 passed`;
- workflow relay plus Evaluation component/benchmark replay: `34 passed`;
- no API/network/credential/DB/source/runtime mutation.

## Remaining Integration Gate

Coordinator reviews this clean branch and commit before any merge into
source-main. App launcher wiring remains a separately owned shared boundary.
