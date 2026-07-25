const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "console.jsx"), "utf8");

function extractFunction(name) {
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

const context = {};
vm.runInNewContext(
  `${extractFunction("consoleUsageSummaryTotal")}
   ${extractFunction("consoleLatestWorkProgressRows")}
   this.consoleUsageSummaryTotal = consoleUsageSummaryTotal;
   this.consoleLatestWorkProgressRows = consoleLatestWorkProgressRows;`,
  context,
);

function producerTotal(overrides = {}) {
  return {
    componentId: "translation",
    componentRunId: "translation_run_1",
    snapshotSeq: 13,
    sha256: "C".repeat(64),
    promptTokens: 25395,
    completionTokens: 49568,
    totalTokens: 74963,
    binding: null,
    ...overrides,
  };
}

function usage(overrides = {}) {
  return {
    present: true,
    validation: {
      valid: true,
      authority: "producer_snapshots_and_neutral_relay",
    },
    componentTotals: [producerTotal()],
    workflowTotal: null,
    ...overrides,
  };
}

function usageAuthorityGates() {
  const selected = context.consoleUsageSummaryTotal(usage(), true);
  assert.equal(selected.authority, "component");
  assert.equal(selected.total.totalTokens, 74963);
  assert.equal(selected.total.promptTokens, 25395);
  assert.equal(selected.total.completionTokens, 49568);

  const workflowTotal = {
    totalTokens: 90000,
    binding: { authority: "relay_sealed" },
  };
  assert.equal(
    context.consoleUsageSummaryTotal(usage({ workflowTotal }), true).authority,
    "workflow",
    "relay-sealed workflow total must retain priority",
  );
  assert.equal(
    context.consoleUsageSummaryTotal(
      usage({ workflowTotal: { ...workflowTotal, binding: { authority: "wrong" } } }),
      true,
    ).authority,
    null,
    "an invalid workflow total must fail closed instead of falling back",
  );

  const rejected = [
    context.consoleUsageSummaryTotal(usage(), false),
    context.consoleUsageSummaryTotal(usage({
      validation: { valid: false, authority: "producer_snapshots_and_neutral_relay" },
    }), true),
    context.consoleUsageSummaryTotal(usage({
      validation: { valid: true, authority: "producer_only" },
    }), true),
    context.consoleUsageSummaryTotal(usage({
      componentTotals: [producerTotal({ snapshotSeq: 0 })],
    }), true),
    context.consoleUsageSummaryTotal(usage({
      componentTotals: [producerTotal({ snapshotSeq: "13" })],
    }), true),
    context.consoleUsageSummaryTotal(usage({
      componentTotals: [producerTotal({ sha256: "not-a-hash" })],
    }), true),
    context.consoleUsageSummaryTotal(usage({
      componentTotals: [producerTotal(), producerTotal({ componentId: "evaluation" })],
    }), true),
  ];
  rejected.forEach(result => assert.equal(result.authority, null));
}

function progressPresentationGates() {
  const start = { event: "stage_start", stage: "translation.b1", seq: 1 };
  const first = { event: "work_progress", stage: "translation.b1", seq: 2 };
  const latest = { event: "work_progress", stage: "translation.b1", seq: 3 };
  const other = { event: "work_progress", stage: "translation.b2", seq: 4 };
  const missingStage = { event: "work_progress", stage: "", seq: 5 };
  const rows = [start, first, latest, other, missingStage];

  assert.strictEqual(
    context.consoleLatestWorkProgressRows(rows, "all"),
    rows,
    "All must retain the untouched raw row array",
  );
  assert.deepEqual(
    Array.from(context.consoleLatestWorkProgressRows(rows, "important"), row => row.seq),
    [1, 3, 4],
    "Important keeps historical stage_start and only the latest progress per stage",
  );
}

usageAuthorityGates();
progressPresentationGates();
assert.match(source, /"work_started", "work_progress", "work_completed"/);
assert.match(source, /consoleLatestWorkProgressRows\(retryPresentationRows, eventPreset\)/);
console.log("workflow live progress + usage: 2/2 passed");
