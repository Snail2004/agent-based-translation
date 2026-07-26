# Evaluation Run Recovery Phase B

## Purpose

Allow a halted Evaluation component to continue after a closed-scope
implementation or operational repair without discarding accepted work. This
phase is operational recovery only. It does not change a scorer, prompt,
model, rubric, input, settings, schema, or semantic validator.

## Sealed repair contract

`EvaluationWorkflowRepairPlanV1` is created only from the current recovery
ledger and a server-authorized exact `affected_work_ids` list. The plan binds:

- source and target component attempts;
- the immutable assignment and semantic-input hashes;
- source and repair code commits;
- authorization identity and timestamp;
- the exact superseded, rerun, prior-accepted, and unaffected sets;
- the pre-repair ledger/checkpoint hashes.

Accepted work is never edited or deleted. An accepted work may be superseded
once, and a later accepted result becomes the single current contribution for
that semantic `work_id`. Unknown, terminally rejected, already superseded, or
otherwise unpartitionable work fails closed.

`EvaluationWorkflowRepairReceiptV1` is written only after every sealed rerun
work item has an accepted result. It echoes the exact work partition, preserves
unaffected artifact bindings byte-for-byte, and binds each rerun result to its
prior artifact (or explicit null for previously pending/halted work). Current
accepted artifacts must be exactly `unaffected + rerun`, with no duplicate
semantic work IDs.

## Resume behavior

Repair output is append-only under:

```text
chapters/<ordinal>_<chapter_id>/repairs/<repair_id>/
```

The repair plan and terminal receipt remain under the component-local:

```text
recovery/repairs/<repair_id>/
```

If a repair halts after one or more chapters have been accepted, the recovery
ledger and the already sealed chapter report/execution artifact pair are
validated and reused on the next attempt. Only the remaining affected work is
run. A missing, foreign, or hash-drifted artifact fails closed before a
provider/scorer call. Operational attempts get new component/physical
identities; no attempt ID is reused.

The public workflow event/package authority remains the existing
`EvaluationWorkflowComponentV1` writer. Recovery lineage is additive and does
not create a parallel public event schema.

## Failure policy

- `component_halted` remains resumable.
- `component_failed` is reserved for unverifiable integrity or lineage.
- Incidents are redacted and internal; repeated equivalent failures on
  different component attempts receive distinct immutable incident IDs.
- No provider fallback, silent model/key/route rotation, or semantic drift is
  permitted.

## Verification

The Phase B gate is 0-API and uses fake providers. It covers:

- exact work-set partitioning and foreign/tampered plan rejection;
- unchanged unaffected artifacts and duplicate-current rejection;
- selective repair of accepted plus halted work;
- repair interruption followed by Resume without replaying the accepted repair
  prefix;
- recovery package, receipt, hash, and attempt-lineage validation.

Phase C (live repair, App/relay integration, and provider behavior) remains
closed until separately authorized.
