# TASK_EVAL_SCORER_INPUT_PACKETS_V1

Status: COMPLETE - 0 API

Owner: Evaluation workstream

## Objective

Materialize closed, content-addressed, model-facing input packets from an
existing `CommonEvaluationInputV1` plus `EvaluationPlanV1`. This milestone
defines what a scorer may see without selecting a model, writing a prompt, or
producing a score.

## Method views

### SF-QE

One packet contains only the active English source block and one opaque
Vietnamese candidate. Neighbor context is not sent because the locked QE
instrument scores a source/translation segment pair.

### SF-BT

The initial packet is the back-translation view. It contains one opaque
Vietnamese candidate and its bounded Vietnamese neighbor context. It contains
no English source text. A later comparison packet may be created only after a
back-translation artifact exists; that later contract is out of scope here.

### PJ

One packet contains the active English source plus bounded English context and
two Vietnamese candidates in the exact opaque presentation order already
sealed by the plan. The packet contains no arm identity.

## Packet contract

`EvaluationScorerInputPacketV1` binds:

- exact plan, config, input-set, job, method, and unit identities;
- source and target language codes;
- a method stage;
- ordered source/candidate block views;
- an Evaluation producer commit;
- a deterministic packet ID and packet self-hash.

Block views retain canonical `block_id`, relative role
`preceding|active|following`, mechanical availability status, and exact text.
Unavailable neighbor text remains explicit as `null`; it is never fabricated
or silently borrowed from another arm.

## Hard boundaries

- no API, provider, model, prompt, response, score, winner, or aggregation;
- no gold, oracle, human reference, terminology memory, glossary, or runtime
  context;
- no arm ID, logical run ID, profile ID, or producer label in model-facing
  packets;
- no source text in an SF-BT back-translation packet;
- no DB, cache, checkpoint mutation, App, Console, or Cockpit edit;
- no change to `D2LEvaluationInputV1`, `CommonEvaluationInputV1`,
  `EvaluationRunConfigV1`, `EvaluationPlanV1`, or `FullRunReportV1`.

## Acceptance

1. Same common input, plan, job, timestamp, and producer commit yield the same
   packet bytes and hash.
2. Input objects are not mutated.
3. SF-QE has one active source row and one active opaque candidate row.
4. SF-BT contains target-side context but no source object or English source
   text copied by the packet builder.
5. PJ contains two opaque slots and preserves the plan's presentation order
   without exposing the arm mapping.
6. Context remains in the active chapter and in canonical source order.
7. Active candidates must be translated; missing/failed neighbors remain
   explicit.
8. Unknown keys, duplicate slots/blocks, invalid roles/statuses, non-finite or
   malformed hashes, source leakage, and method/cardinality mismatches fail
   closed.
9. Recursive forbidden evaluation authority cannot be smuggled into a packet.
10. Existing Evaluation tests remain green.

## Next gate

After these input packets are verified on the real MLP plan, separately design
and review method-specific prompt and response contracts. Model selection and
API allocation remain later decisions.

## Result

- fixture probes: `21 passed`;
- real MLP packets reconstructed and bound-validated: `2,375 / 2,375`;
- packet byte sizes before prompt rendering:
  - SF-QE: median `1,765`, p95 `3,148`, max `5,370`;
  - SF-BT: median `2,468`, p95 `3,584`, max `5,355`;
  - PJ: median `5,186`, p95 `8,233`, max `12,841`;
- real packet arm/model-key scan: `0` forbidden identity keys;
- every SF-BT packet had `source=null`.
- full applicable runtime gate: `1,021 passed`, `1 skipped`, `2 deselected`
  (the two known Literary frozen-SQLite probes unavailable in this worktree).

These byte counts are transport measurements, not model-token estimates.
