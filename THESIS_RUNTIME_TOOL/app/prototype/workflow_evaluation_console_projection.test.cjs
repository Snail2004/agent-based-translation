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

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function parentEvent(seq, componentSeq, sourceEventId, event, stageId) {
  return {
    seq,
    event_id: `workflow_event_${String(seq).padStart(8, "0")}`,
    event,
    stage_id: stageId,
    component: {
      component_id: "evaluation",
      component_run_id: "evaluation_run_1",
      component_attempt_id: "evalcomp_attempt_0001",
      component_attempt_index: 1,
      component_seq: componentSeq,
      source_event_id: sourceEventId,
    },
    integrity: {
      event_sha256: String(seq).repeat(64),
    },
  };
}

function detail(overrides = {}) {
  return {
    progress: null,
    validator: null,
    retry: null,
    checkpoint: null,
    usage_snapshot: null,
    resume: null,
    reason: null,
    outcome: null,
    ...overrides,
  };
}

function row({
  seed,
  event,
  severity,
  sourceEventIds,
  start,
  end,
  detailValue = detail(),
}) {
  return {
    row_id: `evalconsole_row_${seed.repeat(32)}`,
    event,
    severity,
    label_key: {
      component_started: "evaluation.component_started",
      retry_summary: "evaluation.retry_summary",
      stage_done: "evaluation.stage_done",
    }[event],
    source_event_ids: sourceEventIds,
    source_component_seq_start: start,
    source_component_seq_end: end,
    component_attempt_id: "evalcomp_attempt_0001",
    component_attempt_index: 1,
    ts: `2026-07-26T00:00:0${end}Z`,
    stage_id: event === "component_started" ? "__component__" : "score",
    agent: "evaluation_runner",
    detail: detailValue,
    integrity: {
      row_sha256: seed.repeat(64),
    },
  };
}

async function readModel(adapter) {
  const events = [
    parentEvent(2, 1, "eval_event_1", "component_started", null),
    parentEvent(3, 2, "eval_event_2", "retry", "evaluation.score"),
    parentEvent(4, 3, "eval_event_3", "stage_done", "evaluation.score"),
  ];
  const rows = [
    row({
      seed: "a",
      event: "component_started",
      severity: "info",
      sourceEventIds: ["eval_event_1"],
      start: 1,
      end: 1,
    }),
    row({
      seed: "b",
      event: "retry_summary",
      severity: "warning",
      sourceEventIds: ["eval_event_2", "eval_event_3"],
      start: 2,
      end: 3,
      detailValue: detail({
        retry: {
          retry_kind: "transport",
          logical_request_id: "evaluation.score.packet_1",
          retry_count: 1,
          physical_attempt_indexes: [1],
          reason_codes: ["provider_timeout"],
          outcome: "stage_succeeded",
        },
      }),
    }),
    row({
      seed: "c",
      event: "stage_done",
      severity: "info",
      sourceEventIds: ["eval_event_3"],
      start: 3,
      end: 3,
      detailValue: detail({ outcome: "succeeded" }),
    }),
  ];
  const projectionRef = "components/evaluation/evaluation_run_1/artifacts/console_projections/00000003_fixture.json";
  const artifactIndexSha256 = "d".repeat(64);
  const draft = {
    schema_id: "EvaluationConsoleReadV1",
    schema_version: "1.0.0",
    workflow_run_id: "workflow_fixture_1",
    component_id: "evaluation",
    component_run_id: "evaluation_run_1",
    projection_count: 3,
    through_parent_seq: 4,
    through_parent_event_id: "workflow_event_00000004",
    through_component_seq: 3,
    through_component_event_id: "eval_event_3",
    projection_ref: projectionRef,
    projection_sha256: "e".repeat(64),
    rows,
    state: {
      open_retry_groups: [],
      paused_incident_ids: [],
    },
    cumulative: {
      row_count: 3,
      row_chain_sha256: "f".repeat(64),
    },
    validation: {
      valid: true,
      authority: "evaluation_console_projection_v1_and_neutral_relay",
      artifact_index_sha256: artifactIndexSha256,
      parent_event_sha256: events.at(-1).integrity.event_sha256,
    },
  };
  const value = {
    ...draft,
    integrity: {
      read_sha256: await adapter.canonicalSha256(draft),
    },
  };
  const manifest = {
    workflow_run_id: "workflow_fixture_1",
    components: [{
      component_id: "evaluation",
      component_run_id: "evaluation_run_1",
    }],
    stages: [{
      stage_id: "evaluation.score",
      component_id: "evaluation",
      local_stage_id: "score",
      label: "Evaluation score",
    }],
  };
  const artifactRows = [{
    binding: {
      artifact_ref: projectionRef,
      artifact_kind: "evaluation_console_projection_v1",
      schema_version: "1.0.0",
    },
  }];
  const artifactIndex = {
    integrity: {
      artifact_index_sha256: artifactIndexSha256,
    },
  };
  return { value, manifest, events, artifactRows, artifactIndex };
}

async function validatesAndFoldsAtGlobalReplayCursor() {
  const adapter = loadAdapter();
  const fixture = await readModel(adapter);
  const surface = await adapter.validateEvaluationConsoleReadModel(
    fixture.value,
    fixture.manifest,
    fixture.events,
    fixture.artifactRows,
    fixture.artifactIndex,
  );
  assert.equal(surface.present, true);
  assert.equal(surface.valid, true);
  assert.deepEqual(
    Array.from(surface.rows, rowValue => rowValue.event),
    ["component_started", "retry_summary", "stage_done"],
  );

  const atFirst = adapter.foldEvaluationConsoleCursor(
    surface,
    fixture.events,
    fixture.manifest,
    2,
  );
  assert.deepEqual(
    Array.from(atFirst.rows, rowValue => rowValue.event),
    ["component_started"],
    "global cursor 2 must not expose rows sealed through component seq 3",
  );
  const beforeClose = adapter.foldEvaluationConsoleCursor(
    surface,
    fixture.events,
    fixture.manifest,
    3,
  );
  assert.deepEqual(
    Array.from(beforeClose.rows, rowValue => rowValue.event),
    ["component_started"],
    "an open retry remains raw-only until the producer seals its summary",
  );
  const atClose = adapter.foldEvaluationConsoleCursor(
    surface,
    fixture.events,
    fixture.manifest,
    4,
  );
  assert.deepEqual(
    Array.from(atClose.rows, rowValue => rowValue.event),
    ["component_started", "retry_summary", "stage_done"],
  );
  assert.equal(atClose.rows[1].globalStageId, "evaluation.score");
  assert.equal(atClose.rows[1].parentSeq, 4);
}

async function invalidOptionalSurfaceStaysIsolated() {
  const adapter = loadAdapter();
  const fixture = await readModel(adapter);
  const tampered = clone(fixture.value);
  tampered.rows[1].severity = "error";
  const invalid = await adapter.validateEvaluationConsoleReadModel(
    tampered,
    fixture.manifest,
    fixture.events,
    fixture.artifactRows,
    fixture.artifactIndex,
  );
  assert.equal(invalid.present, true);
  assert.equal(invalid.valid, false);
  assert.equal(invalid.rows.length, 0);
  assert.ok(invalid.errors.some(error => error.code === "evaluation_console_read_hash"));

  const absent = await adapter.validateEvaluationConsoleReadModel(
    null,
    fixture.manifest,
    fixture.events,
    fixture.artifactRows,
    fixture.artifactIndex,
  );
  assert.equal(absent.present, false);
  assert.equal(absent.valid, true);
}

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  let quote = "";
  let escaped = false;
  for (let index = bodyStart; index < source.length; index += 1) {
    const char = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === quote) quote = "";
      continue;
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char;
      continue;
    }
    if (char === "{") depth += 1;
    if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

function importantUsesProjectionWhileAllKeepsRawAudit() {
  const source = fs.readFileSync(path.join(__dirname, "console.jsx"), "utf8");
  const context = {};
  vm.runInNewContext(
    `${extractFunction(source, "consoleEvaluationPresentationRows")}
     this.project = consoleEvaluationPresentationRows;`,
    context,
  );
  const raw = [
    { key: "translation", componentId: "translation", seq: 1 },
    { key: "evaluation-1", componentId: "evaluation", seq: 2 },
    { key: "evaluation-2", componentId: "evaluation", seq: 3 },
    { key: "evaluation-3", componentId: "evaluation", seq: 4 },
  ];
  const projection = [
    { key: "projected-1", parentSeq: 2, severity: "info" },
    { key: "projected-summary", parentSeq: 4, severity: "warning" },
    { key: "projected-done", parentSeq: 4, severity: "info" },
  ];
  assert.deepEqual(
    Array.from(
      context.project(raw, projection, "important", true),
      item => item.key,
    ),
    ["translation", "projected-1", "projected-summary", "projected-done"],
  );
  const productionShapeProjection = projection.map(item => ({
    ...item,
    seq: item.parentSeq,
    parentSeq: undefined,
  }));
  assert.deepEqual(
    Array.from(
      context.project(raw, productionShapeProjection, "important", true),
      item => item.key,
    ),
    ["translation", "projected-1", "projected-summary", "projected-done"],
    "production projection rows use the Console seq anchor",
  );
  assert.deepEqual(
    Array.from(context.project(raw, projection, "all", true), item => item.key),
    raw.map(item => item.key),
    "All must preserve every physical parent event",
  );
  assert.deepEqual(
    Array.from(context.project(raw, [], "important", false), item => item.key),
    raw.map(item => item.key),
    "invalid optional projection must leave raw parent audit visible",
  );
}

Promise.resolve()
  .then(validatesAndFoldsAtGlobalReplayCursor)
  .then(invalidOptionalSurfaceStaysIsolated)
  .then(importantUsesProjectionWhileAllKeepsRawAudit)
  .then(() => {
    console.log("workflow_evaluation_console_projection 3/3");
  })
  .catch(error => {
    console.error(error);
    process.exitCode = 1;
  });
