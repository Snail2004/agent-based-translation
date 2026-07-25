const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

function loadAdapter() {
  const source = fs.readFileSync(path.join(__dirname, "workflow_replay.js"), "utf8");
  const window = {
    crypto: globalThis.crypto || webcrypto,
    TextEncoder: globalThis.TextEncoder,
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(source, { window, console, TextEncoder: globalThis.TextEncoder });
  return window.WorkflowReplayAdapter;
}

function manifest() {
  return {
    stages: [
      {
        stage_id: "translation.b1_candidate_discovery",
        component_id: "translation",
        local_stage_id: "b1_candidate_discovery",
        label: "Manifest B1 label",
      },
      {
        stage_id: "translation.b2_admission_translation",
        component_id: "translation",
        local_stage_id: "b2_admission_translation",
        label: "Manifest B2 label",
      },
      {
        stage_id: "translation.auditor_morphology",
        component_id: "translation",
        local_stage_id: "auditor_morphology",
        label: "Manifest morphology label",
      },
    ],
  };
}

function evidence(attempt, seq, stage, seed) {
  return {
    evidence_kind: "work_journal",
    journal_ref: `runtime/work_items/${stage}.jsonl`,
    journal_seq: seq,
    entry_sha256: seed.repeat(64),
    producer_component_attempt_id: attempt,
    validation_event_id: `evt_translation_run_1_${String(seq).padStart(8, "0")}`,
    validation_component_attempt_id: attempt,
    validation_component_seq: seq,
  };
}

async function row(adapter, stageId, evidenceRow, values) {
  const value = {
    row_id: "",
    row_sha256: "",
    logical_term_id: values.logical_term_id || "term_gradient",
    state: values.state,
    lifecycle: values.state === "committed" ? "committed" : "provisional",
    authority: values.state === "committed" ? "glossary_commit" : "none",
    origin_component_attempt_id: evidenceRow.validation_component_attempt_id,
    origin_component_seq: evidenceRow.validation_component_seq,
    candidate_ids: ["cand_gradient"],
    member_ids: [],
    surfaces: ["gradient"],
    source_block_ids: ["block_1"],
    targets: values.targets || [],
    reason_codes: values.reason_codes || [],
    rationale: values.rationale ?? null,
    supersedes_row_ids: values.supersedes_row_ids || [],
    evidence_ref: evidenceRow.journal_ref,
    evidence_sha256: evidenceRow.entry_sha256,
  };
  const identity = {
    schema_version: "d2l_term_lifecycle_row_identity_v1",
    stage_id: stageId,
    state: value.state,
    logical_term_id: value.logical_term_id,
    candidate_ids: value.candidate_ids,
    member_ids: value.member_ids,
    surfaces: value.surfaces,
    source_block_ids: value.source_block_ids,
    targets: value.targets,
    evidence_ref: value.evidence_ref,
    evidence_sha256: value.evidence_sha256,
  };
  value.row_id = `tlr_${(await adapter.canonicalSha256(identity)).slice(0, 32)}`;
  const unsigned = { ...value };
  delete unsigned.row_sha256;
  value.row_sha256 = await adapter.canonicalSha256(unsigned);
  return value;
}

async function batch(adapter, stageId, evidenceRow, rows, summary, projectionMode = "live") {
  const orderedRows = [...rows].sort((left, right) => left.row_id.localeCompare(right.row_id));
  const identity = {
    schema_version: "d2l_term_lifecycle_batch_identity_v1",
    stage_id: stageId,
    evidence: evidenceRow,
    row_ids: orderedRows.map(item => item.row_id),
  };
  const value = {
    schema_version: "d2l_term_lifecycle_batch_v1",
    batch_id: `tlb_${(await adapter.canonicalSha256(identity)).slice(0, 32)}`,
    batch_sha256: "",
    projection_mode: projectionMode,
    timing_authority: projectionMode === "resume_backfill" ? "logical_order_only" : "recorded",
    origin_component_attempt_id: evidenceRow.validation_component_attempt_id,
    origin_component_seq: evidenceRow.validation_component_seq,
    evidence: evidenceRow,
    rows: orderedRows,
    summary,
  };
  const unsigned = { ...value };
  delete unsigned.batch_sha256;
  value.batch_sha256 = await adapter.canonicalSha256(unsigned);
  return value;
}

function event(seq, stageId, payload, attempt = 1, attemptIndex = attempt) {
  return {
    seq,
    event_id: `workflow_event_${String(seq).padStart(8, "0")}`,
    event: "term_lifecycle",
    stage_id: `translation.${stageId}`,
    component: {
      component_id: "translation",
      component_attempt_id: attempt,
      component_attempt_index: attemptIndex,
      component_seq: seq,
    },
    payload,
  };
}

async function validSequence(adapter) {
  const proposedEvidence = evidence(1, 1, "b1_candidate_discovery", "A");
  const proposed = await row(adapter, "b1_candidate_discovery", proposedEvidence, {
    state: "proposed",
    reason_codes: ["observed"],
  });
  const proposedBatch = await batch(adapter, "b1_candidate_discovery", proposedEvidence, [proposed], {
    observations: 1,
    unique_surfaces: 1,
    logical_terms: 1,
    state_counts: { proposed: 1 },
    completed: 1,
    total: 10,
    unit: "windows",
    through_work_id: "window_1",
  });

  const admittedEvidence = evidence(1, 2, "b2_admission_translation", "B");
  const admitted = await row(adapter, "b2_admission_translation", admittedEvidence, {
    state: "admitted",
    targets: [{ target_vi: "gradient", applicability: null, disposition: "canonical" }],
    reason_codes: ["admit"],
    supersedes_row_ids: [proposed.row_id],
  });
  const admittedBatch = await batch(adapter, "b2_admission_translation", admittedEvidence, [admitted], {
    observations: 2,
    unique_surfaces: 1,
    logical_terms: 1,
    state_counts: { admitted: 1, proposed: 1 },
    completed: 1,
    total: 1,
    unit: "packets",
    through_work_id: "packet_1",
  });
  return {
    proposed,
    proposedBatch,
    admitted,
    admittedBatch,
    events: [
      event(1, "b1_candidate_discovery", proposedBatch),
      event(2, "b2_admission_translation", admittedBatch),
    ],
  };
}

async function validatesAndFoldsAtCursor(adapter) {
  const fixture = await validSequence(adapter);
  const model = await adapter.validateTermLifecycleEvents(fixture.events, manifest());
  assert.equal(model.valid, true);
  assert.equal(model.batches.length, 2);

  const first = adapter.foldTermLifecycleCursor(model, 1);
  assert.equal(first.summary.observations, 1);
  assert.equal(first.rows.length, 1);
  assert.equal(first.stageLabel, "Manifest B1 label", "stage display must come from the parent manifest");
  assert.equal(first.rows[0].authority, "none");

  const second = adapter.foldTermLifecycleCursor(model, 2);
  assert.equal(second.summary.observations, 2);
  assert.equal(second.rows.length, 2);
  assert.equal(second.stageLabel, "Manifest B2 label");
  assert.equal(adapter.foldTermLifecycleCursor(model, 0), null, "an earlier Replay cursor must not see future lifecycle rows");
}

async function resumeDuplicateIsIdempotent(adapter) {
  const fixture = await validSequence(adapter);
  const backfillEvidence = evidence(1, 1, "b1_candidate_discovery", "C");
  const backfillRow = await row(adapter, "b1_candidate_discovery", backfillEvidence, {
    state: "proposed",
    reason_codes: ["backfill"],
  });
  const backfillBatch = await batch(adapter, "b1_candidate_discovery", backfillEvidence, [backfillRow], {
    observations: 1,
    unique_surfaces: 1,
    logical_terms: 1,
    state_counts: { proposed: 1 },
    completed: 1,
    total: 10,
    unit: "windows",
    through_work_id: "window_1",
  }, "resume_backfill");
  const model = await adapter.validateTermLifecycleEvents([
    event(1, "b1_candidate_discovery", backfillBatch, 1),
    event(2, "b1_candidate_discovery", JSON.parse(JSON.stringify(backfillBatch)), 2),
  ], manifest());
  assert.equal(model.valid, true);
  assert.equal(model.batches.length, 1, "same deterministic Resume batch must dedupe");
  assert.equal(adapter.foldTermLifecycleCursor(model, 2).rows.length, 1);

  const drift = JSON.parse(JSON.stringify(backfillBatch));
  drift.batch_sha256 = "0".repeat(64);
  const conflict = await adapter.validateTermLifecycleEvents([
    event(1, "b1_candidate_discovery", backfillBatch, 1),
    event(2, "b1_candidate_discovery", drift, 2),
  ], manifest());
  assert.equal(conflict.valid, false);
  assert.ok(conflict.errors.some(error => error.code === "term_batch_hash" || error.code === "term_batch_conflict"));
  assert.equal(adapter.foldTermLifecycleCursor(conflict, 2), null);
  assert.equal(fixture.proposed.authority, "none");
}

async function illegalTransitionFailsClosed(adapter) {
  const fixture = await validSequence(adapter);
  const rejectedEvidence = evidence(1, 2, "b2_admission_translation", "D");
  const rejected = await row(adapter, "b2_admission_translation", rejectedEvidence, {
    state: "rejected",
    reason_codes: ["reject"],
    supersedes_row_ids: [fixture.proposed.row_id],
  });
  const rejectedBatch = await batch(adapter, "b2_admission_translation", rejectedEvidence, [rejected], {
    observations: 2,
    unique_surfaces: 1,
    logical_terms: 1,
    state_counts: { proposed: 1, rejected: 1 },
    completed: 1,
    total: 1,
    unit: "packets",
    through_work_id: "packet_reject",
  });
  const morphologyEvidence = evidence(1, 3, "auditor_morphology", "E");
  const morphology = await row(adapter, "auditor_morphology", morphologyEvidence, {
    state: "morphology_resolved",
    reason_codes: ["resolved"],
    supersedes_row_ids: [rejected.row_id],
  });
  const morphologyBatch = await batch(adapter, "auditor_morphology", morphologyEvidence, [morphology], {
    observations: 3,
    unique_surfaces: 1,
    logical_terms: 1,
    state_counts: { morphology_resolved: 1, proposed: 1, rejected: 1 },
    completed: 1,
    total: 1,
    unit: "packets",
    through_work_id: "morphology_packet",
  });
  const model = await adapter.validateTermLifecycleEvents([
    event(1, "b1_candidate_discovery", fixture.proposedBatch),
    event(2, "b2_admission_translation", rejectedBatch),
    event(3, "auditor_morphology", morphologyBatch),
  ], manifest());
  assert.equal(model.valid, false);
  assert.ok(model.errors.some(error => error.code === "term_transition"));
  assert.equal(adapter.foldTermLifecycleCursor(model, 3), null);
}

async function futureEvidenceAndAuthorityFailClosed(adapter) {
  const fixture = await validSequence(adapter);
  const futureEvidence = evidence(2, 1, "b1_candidate_discovery", "F");
  const futureRow = await row(adapter, "b1_candidate_discovery", futureEvidence, {
    state: "proposed",
    reason_codes: ["future"],
  });
  const futureBatch = await batch(adapter, "b1_candidate_discovery", futureEvidence, [futureRow], {
    observations: 1,
    unique_surfaces: 1,
    logical_terms: 1,
    state_counts: { proposed: 1 },
    completed: 1,
    total: 10,
    unit: "windows",
    through_work_id: "window_future",
  });
  const future = await adapter.validateTermLifecycleEvents([
    event(1, "b1_candidate_discovery", futureBatch, 1, 2),
  ], manifest());
  assert.equal(future.valid, false);
  assert.ok(future.errors.some(error => error.code === "term_future_origin"));

  const authorityDrift = JSON.parse(JSON.stringify(fixture.proposedBatch));
  authorityDrift.rows[0].authority = "glossary_commit";
  const invalidAuthority = await adapter.validateTermLifecycleEvents([
    event(1, "b1_candidate_discovery", authorityDrift),
  ], manifest());
  assert.equal(invalidAuthority.valid, false);
  assert.ok(invalidAuthority.errors.some(error => error.code === "term_authority"));
}

async function main() {
  const adapter = loadAdapter();
  await validatesAndFoldsAtCursor(adapter);
  await resumeDuplicateIsIdempotent(adapter);
  await illegalTransitionFailsClosed(adapter);
  await futureEvidenceAndAuthorityFailClosed(adapter);
  console.log("workflow term lifecycle: 4/4 passed");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
