# D2L Resumable Failure Recovery V1

## Objective

Keep one long D2L Translation component resumable across operational failures
without repeating already accepted semantic work or weakening the sealed
experiment identity.

## Same-run recovery

The following conditions pause and checkpoint the current component:

- provider or transport retry exhaustion;
- semantic response rejection after the sealed retry allowance;
- child-process launch, timeout, or non-zero exit;
- stage output contract failure;
- an unterminated observation-journal tail;
- a dead parent process that left a non-terminal component marked `running`.

Resume keeps `workflow_run_id` and `component_run_id`, increments
`component_attempt_id`, and appends `run_resumed`.

The component runner holds an OS-released single-writer lease for its complete
lifetime. Registry-wrapper PID death is not enough to authorize Resume: the App
also probes this lease, and the runner independently reacquires it before
reading or mutating the component package. A surviving child therefore blocks
a second writer even when its wrapper has already exited.

Accepted semantic work items are stored in an append-only, hash-chained journal.
The same work item is reused only when its input hash and semantic contract ID
match exactly. Translator window state uses a stable experiment identity while
physical provider attempts remain attempt-bound.

## Mechanical code repair

A code revision change is rejected before any Resume mutation unless the
operator supplies an explicit repair reason and the new clean Git revision
descends from the sealed baseline.

An accepted repair publishes
`d2l_component_repair_receipt_v2_mechanical_scope`, binding:

- baseline and effective Git revisions;
- checkpoint and component attempt lineage;
- runner-plan and semantic-contract hashes;
- Git delta hash and changed paths;
- an explicit `semantic_contract_unchanged` attestation.

The attestation is not sufficient on its own. The Git delta must also satisfy
the closed `d2l_mechanical_repair_paths_v1` policy. Only component
orchestration, recovery, replay-contract, App Resume boundary, related tests,
and task documentation paths are eligible. Prompt, model/profile, glossary,
validator, Translator, and semantic-executor paths are rejected before any
Resume package byte is changed.

The effective revision is propagated to new Translation and scoring artifacts.

## Crash evidence

- An incomplete final observation row is copied byte-for-byte to a quarantine
  file and bound by a recovery receipt before the authoritative journal is
  truncated to its last complete row.
- Existing stage outputs that were written but not published are moved to an
  attempt-scoped evidence directory and bound by a recovery receipt before the
  stage executes again.
- A stale `running` attempt may be recovered only through the explicit
  stale-recovery path. The App route first proves the previous registry process
  is dead and the component writer lease is no longer active.

## New-run boundary

Changing any model, prompt, glossary authority, semantic validator, source
selection, chapter selection, output policy, or semantic role contract requires
a new run. Recovery receipts must not be used to relabel such a change as a
mechanical repair.

Historical terminal `run_done` and `run_failed` streams remain immutable and
cannot be reopened.

## Safety

- No raw prompts, raw responses, credentials, gold, or reference translations
  are written to recovery receipts.
- Resume never changes the sealed runner plan.
- Hash or identity drift fails closed.
- No API call is required to validate this contract.
