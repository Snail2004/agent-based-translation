const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const reportsRoot = process.argv[2];
const runSuffix = process.argv[3] || "20260726_compact_v4";
if (!reportsRoot) {
  throw new Error("Usage: node story_bible_b4_real.test.cjs <B4 reports root> [run suffix]");
}

const source = fs.readFileSync(path.join(__dirname, "parts_graph.jsx"), "utf8");
const window = {};
vm.runInNewContext(source, { window, React: {}, console });

const snapshots = {};
for (let chapter = 1; chapter <= 6; chapter += 1) {
  const chapterToken = String(chapter).padStart(2, "0");
  const reportDir = `literary_b4_story_bible_wh_ch${chapterToken}_${runSuffix}`;
  assert.ok(
    fs.existsSync(path.join(reportsRoot, reportDir)),
    `missing B4 chapter ${chapterToken}`,
  );

  const artifactPath = path.join(
    reportsRoot,
    reportDir,
    `story_graph_as_of_ch${chapterToken}.json`,
  );
  const payload = JSON.parse(fs.readFileSync(artifactPath, "utf8"));
  const mapped = window.mapLiteraryB4StoryGraph(payload);
  assert.equal(mapped.valid, true, `chapter ${chapterToken}: ${mapped.code}`);
  snapshots[chapter] = mapped.data;
}

const snapshotValidation = window.validateStoryBibleChapterSnapshots(snapshots);
assert.equal(snapshotValidation.valid, true, snapshotValidation.code);
assert.equal(snapshotValidation.maxChapter, 6);

const latest = snapshots[6];
const nodeIds = new Set(latest.nodes.map(node => node.id));
assert.equal(latest.nodes.length, 76);
assert.equal(latest.edges.length, 39);
assert.equal(latest.pending.length, 57);
assert.equal(latest.edges.filter(edge => edge.c).length, 3);
assert.equal(
  latest.edges.filter(edge => !nodeIds.has(edge.s) || !nodeIds.has(edge.t)).length,
  0,
);

console.log("story_bible_b4_real.test.cjs: Ch1-Ch6 real B4 snapshots passed");
