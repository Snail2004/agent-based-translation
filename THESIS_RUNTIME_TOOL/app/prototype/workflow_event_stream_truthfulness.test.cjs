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

const context = {
  formatConsoleInt(value) {
    return Number(value).toLocaleString("en-US");
  },
};
vm.runInNewContext(
  `${extractFunction("consoleWorkflowProgress")}
   ${extractFunction("consoleLogicalRequestId")}
   ${extractFunction("consoleDirectWorkId")}
   ${extractFunction("consoleLogicalRequestWorkMap")}
   ${extractFunction("consoleHumanWorkLabel")}
   ${extractFunction("consoleTermLifecyclePresentationRows")}
   ${extractFunction("consoleProgressFacts")}
   ${extractFunction("consoleProgressStageKey")}
   ${extractFunction("consoleResumeProgressPresentationRows")}
   ${extractFunction("consoleMessageFor")}
   this.consoleLogicalRequestId = consoleLogicalRequestId;
   this.consoleDirectWorkId = consoleDirectWorkId;
   this.consoleLogicalRequestWorkMap = consoleLogicalRequestWorkMap;
   this.consoleHumanWorkLabel = consoleHumanWorkLabel;
   this.consoleTermLifecyclePresentationRows = consoleTermLifecyclePresentationRows;
   this.consoleResumeProgressPresentationRows = consoleResumeProgressPresentationRows;
   this.consoleMessageFor = consoleMessageFor;`,
  context,
);

function component(attemptIndex = 1) {
  return {
    componentId: "translation",
    componentRunId: "translation_run_1",
    attemptIndex,
  };
}

function exactWorkIdentityGate() {
  const request = {
    payload: {
      logical_request_id: "logical_2",
      work_id: "b1_w_d2l_preface_0002",
    },
  };
  const response = {
    payload: {
      usage: {
        logical_request_id: "logical_2",
      },
    },
  };
  const workByRequest = context.consoleLogicalRequestWorkMap([request, response]);
  const responseLogicalId = context.consoleLogicalRequestId(response.payload);
  assert.equal(workByRequest.get(responseLogicalId), "b1_w_d2l_preface_0002");
  assert.equal(
    context.consoleHumanWorkLabel(workByRequest.get(responseLogicalId)),
    "d2l_preface · window 2",
  );
  assert.equal(
    context.consoleMessageFor(
      { event: "response_received", payload: response.payload },
      { workLabel: "d2l_preface · window 2", win: 1 },
    ),
    "d2l_preface · window 2 · nhận kết quả",
    "an exact producer work identity must win over the legacy counter fallback",
  );

  const conflict = context.consoleLogicalRequestWorkMap([
    request,
    {
      payload: {
        logical_request_id: "logical_2",
        work_id: "b1_w_d2l_preface_0003",
      },
    },
  ]);
  assert.equal(conflict.has("logical_2"), false, "conflicting producer bindings must fail closed");
}

function termLifecycleCompactionGate() {
  const before = { key: "before", event: "work_progress", seq: 84, rawEventCount: 1 };
  const termRows = Array.from({ length: 13 }, (_, index) => ({
    key: `term-${index + 1}`,
    event: "term_lifecycle",
    seq: 85 + index,
    lineNo: 85 + index,
    stage: "translation.b1_candidate_discovery",
    componentId: "translation",
    componentRunId: "translation_run_1",
    rawEventCount: 1,
    payload: {
      summary: {
        observations: index === 12 ? 429 : index + 1,
        logical_terms: index === 12 ? 342 : index + 1,
      },
    },
  }));
  const after = { key: "after", event: "stage_start", seq: 98, rawEventCount: 1 };
  const raw = [before, ...termRows, after];
  const projected = context.consoleTermLifecyclePresentationRows(raw);

  assert.equal(raw.length, 15, "raw parent events remain untouched");
  assert.equal(projected.length, 3);
  assert.strictEqual(projected[0], before);
  assert.strictEqual(projected[2], after);
  assert.equal(projected[1].presentationBatchCount, 13);
  assert.equal(projected[1].rawEventCount, 13);
  assert.equal(projected[1].groupFirstSeq, 85);
  assert.equal(projected[1].groupLastSeq, 97);
  assert.equal(projected[1].payload.summary.observations, 429);
  assert.equal(projected[1].payload.summary.logical_terms, 342);
  assert.strictEqual(projected[1].payload, termRows[12].payload, "latest producer-sealed summary is displayed");

  const foreignStage = {
    ...termRows[12],
    key: "foreign-stage",
    seq: 98,
    stage: "translation.b2_admission_translation",
  };
  assert.equal(
    context.consoleTermLifecyclePresentationRows([...termRows, foreignStage]).length,
    2,
    "different stages must never be compacted together",
  );
}

function resumeProgressPresentationGate() {
  const prior = {
    key: "progress-81",
    event: "work_progress",
    seq: 81,
    stage: "translation.b1_candidate_discovery",
    progress: { completed: 13, total: 179, unit: "windows" },
    ...component(1),
  };
  const resumedStage = {
    key: "stage-104",
    event: "stage_start",
    seq: 104,
    stage: prior.stage,
    stageLabel: "B1 Candidate Discovery",
    progress: { completed: 0, total: 179, unit: "windows" },
    payload: { progress: { completed: 0, total: 179, unit: "windows" } },
    ...component(4),
  };
  const resumedWork = {
    key: "work-105",
    event: "work_started",
    seq: 105,
    stage: prior.stage,
    stageLabel: "B1 Candidate Discovery",
    workId: "work_b1_candidate_discovery",
    progress: { completed: 0, total: 179, unit: "windows" },
    payload: { progress: { completed: 0, total: 179, unit: "windows" } },
    ...component(4),
  };
  const raw = [prior, resumedStage, resumedWork];
  const projected = context.consoleResumeProgressPresentationRows(raw);

  assert.strictEqual(projected[0], prior);
  assert.equal(projected[1].message, "B1 Candidate Discovery tiếp tục từ checkpoint");
  assert.deepEqual(
    { ...projected[1].displayProgress },
    { completed: 13, total: 179, unit: "windows" },
  );
  assert.equal(projected[1].progress.completed, 0, "the display projection retains raw row progress");
  assert.equal(projected[1].resumeProgressSourceSeq, 81);
  assert.equal(projected[1].resumeProgressSourceAttempt, 1);
  assert.equal(projected[2].message, "work_b1_candidate_discovery tiếp tục từ checkpoint");
  assert.equal(projected[2].displayProgress.completed, 13);
  assert.equal(projected[2].progress.completed, 0);
  assert.equal(raw[1].seq, 104);
  assert.equal(raw[1].payload.progress.completed, 0, "the producer-sealed raw Resume payload remains 0/179");
  assert.equal(raw[2].payload.progress.completed, 0);

  const noPrior = context.consoleResumeProgressPresentationRows([resumedStage])[0];
  assert.equal(noPrior.progress.completed, 0);
  assert.equal(noPrior.displayProgress, null);
  assert.equal(noPrior.suppressProgressBadge, true);
  assert.equal(noPrior.resumeProgressOmitted, true);
  assert.equal(noPrior.payload.progress.completed, 0);

  const foreignStagePrior = {
    ...prior,
    stage: "translation.b2_admission_translation",
  };
  const foreignStageProjection = context.consoleResumeProgressPresentationRows([
    foreignStagePrior,
    resumedStage,
  ])[1];
  assert.equal(foreignStageProjection.displayProgress, null, "foreign stage progress must not bind");
  assert.equal(foreignStageProjection.suppressProgressBadge, true);

  const foreignRunPrior = {
    ...prior,
    componentRunId: "translation_run_other",
  };
  const foreignRunProjection = context.consoleResumeProgressPresentationRows([
    foreignRunPrior,
    resumedStage,
  ])[1];
  assert.equal(foreignRunProjection.displayProgress, null, "foreign component-run progress must not bind");
  assert.equal(foreignRunProjection.suppressProgressBadge, true);

  const firstAttemptStart = { ...resumedStage, attemptIndex: 1 };
  assert.strictEqual(
    context.consoleResumeProgressPresentationRows([firstAttemptStart])[0],
    firstAttemptStart,
    "an initial stage start remains byte-for-byte untouched",
  );
}

exactWorkIdentityGate();
termLifecycleCompactionGate();
resumeProgressPresentationGate();
assert.match(source, /const logicalRequestWorkIds = consoleLogicalRequestWorkMap\(events\)/);
assert.match(source, /workLabel: exactWorkId \? consoleHumanWorkLabel\(exactWorkId\) : ""/);
assert.match(source, /consoleTermLifecyclePresentationRows\(resumePresentationRows\)/);
assert.match(source, /consoleResumeProgressPresentationRows\(displayRows\)/);
assert.match(
  source,
  /formatConsoleInt\(rendered\.length\).*formatConsoleInt\(filtered\.length\).*formatConsoleInt\(filteredRawEventCount\)/s,
  "the accepted displayed/filtered/raw counter UI must remain unchanged",
);
console.log("workflow event stream truthfulness: 3/3 passed");
