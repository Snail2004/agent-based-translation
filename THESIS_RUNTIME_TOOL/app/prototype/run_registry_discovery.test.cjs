const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadAdapter() {
  const source = fs.readFileSync(path.join(__dirname, "workflow_replay.js"), "utf8");
  const window = {
    setTimeout,
    clearTimeout,
    crypto: globalThis.crypto,
    TextEncoder: globalThis.TextEncoder,
  };
  vm.runInNewContext(source, { window, console, TextEncoder: globalThis.TextEncoder });
  return window.WorkflowReplayAdapter;
}

class FakeClock {
  constructor() {
    this.nextId = 1;
    this.tasks = new Map();
  }

  setTimeout = (callback, delay) => {
    const id = this.nextId++;
    this.tasks.set(id, { callback, delay });
    return id;
  };

  clearTimeout = (id) => {
    this.tasks.delete(id);
  };

  async runNext() {
    const next = [...this.tasks.entries()].sort((left, right) => (
      left[1].delay - right[1].delay || left[0] - right[0]
    ))[0];
    assert.ok(next, "expected a scheduled registry poll");
    this.tasks.delete(next[0]);
    next[1].callback();
    await settle();
  }
}

async function settle() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

async function externalRunAppearsAfterOpen(adapter) {
  const clock = new FakeClock();
  const responses = [
    [],
    [{ run_id: "run_external", job_id: "job_1", status: "running" }],
  ];
  const context = { selectedRunId: "", replayActive: false, sourceMode: "", manualHistoricalRunId: "" };
  const selections = [];
  let apiCalls = 0;
  let eventTailStarts = 0;
  const poller = adapter.createRunRegistryPoller({
    fetchRuns: async () => responses[Math.min(apiCalls++, responses.length - 1)],
    onSelect: (runId) => {
      context.selectedRunId = runId;
      selections.push(runId);
      eventTailStarts += 1;
    },
    getContext: () => context,
    seenRunIds: new Set(),
    intervalMs: 4000,
    setTimer: clock.setTimeout,
    clearTimer: clock.clearTimeout,
  });

  poller.start();
  await settle();
  assert.equal(apiCalls, 1);
  assert.deepEqual(selections, []);
  assert.equal(clock.tasks.size, 1);
  await clock.runNext();
  assert.deepEqual(selections, ["run_external"]);
  assert.equal(eventTailStarts, 1, "selection must hand off once to the existing event tail");
  await clock.runNext();
  assert.deepEqual(selections, ["run_external"], "the same active run must not be selected twice");
  poller.stop();
}

async function terminalSelectionSwitchesOnlyOutsideReplay(adapter) {
  const active = { run_id: "run_new_active", job_id: "job_1", status: "pending" };
  const terminal = { run_id: "run_old_done", job_id: "job_1", status: "failed" };

  for (const replayActive of [false, true]) {
    const clock = new FakeClock();
    const context = {
      selectedRunId: terminal.run_id,
      replayActive,
      sourceMode: "",
      manualHistoricalRunId: "",
    };
    const selections = [];
    const poller = adapter.createRunRegistryPoller({
      fetchRuns: async () => [active, terminal],
      onSelect: runId => selections.push(runId),
      getContext: () => context,
      seenRunIds: new Set([terminal.run_id]),
      setTimer: clock.setTimeout,
      clearTimer: clock.clearTimeout,
    });
    poller.start();
    await settle();
    assert.deepEqual(
      selections,
      replayActive ? [] : [active.run_id],
      replayActive ? "client replay must not be hijacked" : "an automatically selected terminal run should yield to a new active run",
    );
    poller.stop();
  }
}

async function manualHistoryAndRecordedReplayStayPinned(adapter) {
  const rows = [{ run_id: "run_new_active", status: "running" }];
  const blockedContexts = [
    {
      selectedRunId: "run_manual_history",
      replayActive: false,
      sourceMode: "",
      manualHistoricalRunId: "run_manual_history",
    },
    {
      selectedRunId: "run_recorded",
      replayActive: false,
      sourceMode: "replay",
      manualHistoricalRunId: "",
    },
  ];

  for (const context of blockedContexts) {
    const selections = [];
    const poller = adapter.createRunRegistryPoller({
      fetchRuns: async () => rows,
      onSelect: runId => selections.push(runId),
      getContext: () => context,
      seenRunIds: new Set(),
      setTimer: setTimeout,
      clearTimer: clearTimeout,
    });
    await poller.refresh({ explicit: true });
    assert.deepEqual(selections, [], "manual history and replay remain pinned even on explicit refresh");
  }
}

async function pollingAndRefreshShareOneRequest(adapter) {
  const clock = new FakeClock();
  let resolveFetch;
  let apiCalls = 0;
  const pendingFetch = new Promise(resolve => { resolveFetch = resolve; });
  const poller = adapter.createRunRegistryPoller({
    fetchRuns: () => {
      apiCalls += 1;
      return pendingFetch;
    },
    onSelect: () => {},
    getContext: () => ({}),
    seenRunIds: new Set(),
    setTimer: clock.setTimeout,
    clearTimer: clock.clearTimeout,
  });

  poller.start();
  poller.start();
  const refreshOne = poller.refresh({ explicit: true });
  const refreshTwo = poller.refresh({ explicit: true });
  assert.equal(apiCalls, 1, "start and refresh must reuse the in-flight registry request");
  resolveFetch([]);
  await Promise.all([refreshOne, refreshTwo]);
  await settle();
  assert.equal(clock.tasks.size, 1, "only one follow-up poll may be scheduled");
  await poller.refresh({ explicit: true });
  await settle();
  assert.equal(apiCalls, 2);
  assert.equal(clock.tasks.size, 1, "explicit refresh must replace, not duplicate, the scheduled poll");
  poller.stop();
  assert.equal(clock.tasks.size, 0, "stop must clear the sole timer");

  const stoppedClock = new FakeClock();
  let resolveStoppedFetch;
  const stoppedSelections = [];
  const stoppedPoller = adapter.createRunRegistryPoller({
    fetchRuns: () => new Promise(resolve => { resolveStoppedFetch = resolve; }),
    onSelect: runId => stoppedSelections.push(runId),
    getContext: () => ({}),
    seenRunIds: new Set(),
    setTimer: stoppedClock.setTimeout,
    clearTimer: stoppedClock.clearTimeout,
  });
  stoppedPoller.start();
  await settle();
  stoppedPoller.stop();
  resolveStoppedFetch([{ run_id: "run_after_unmount", status: "running" }]);
  await settle();
  assert.deepEqual(stoppedSelections, [], "a registry response arriving after unmount must be ignored");
  assert.equal(stoppedClock.tasks.size, 0);
}

async function main() {
  const adapter = loadAdapter();
  await externalRunAppearsAfterOpen(adapter);
  await terminalSelectionSwitchesOnlyOutsideReplay(adapter);
  await manualHistoryAndRecordedReplayStayPinned(adapter);
  await pollingAndRefreshShareOneRequest(adapter);
  console.log("run registry discovery: 4/4 passed");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
