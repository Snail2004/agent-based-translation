const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = __dirname;

function loadGraphModule() {
  const source = fs.readFileSync(path.join(root, "parts_graph.jsx"), "utf8");
  const window = {};
  vm.runInNewContext(source, { window, React: {}, console });
  return window;
}

function fixture() {
  return JSON.parse(fs.readFileSync(
    path.join(root, "fixtures", "story_bible_graph_v1", "full_ch6.json"),
    "utf8",
  ));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

const graph = loadGraphModule();
const validate = graph.validateStoryBibleGraphData;
assert.equal(typeof validate, "function");

const valid = validate(fixture());
assert.equal(valid.valid, true);
assert.equal(valid.maxChapter, 6);
assert.equal(valid.data.nodes.length, 76);
assert.equal(valid.data.edges.length, 39);
assert.equal(valid.data.pending.length, 13);
assert.equal(valid.data.edges.filter(edge => edge.c === 1).length, 3);

const orphan = fixture();
orphan.edges[0].t = "missing_effective_entity";
assert.equal(validate(orphan).code, "story_bible_graph_edge_invalid");

const duplicate = fixture();
duplicate.nodes[1].id = duplicate.nodes[0].id;
assert.equal(validate(duplicate).code, "story_bible_graph_node_invalid");

const invalidPending = fixture();
invalidPending.pending[0].q = "";
assert.equal(validate(invalidPending).code, "story_bible_graph_pending_invalid");

const source = fs.readFileSync(path.join(root, "parts_graph.jsx"), "utf8");
assert.equal(source.includes("BIBLE_SAMPLE_DATA"), false);
assert.equal(source.includes("setTheme("), false);
assert.equal(source.includes("setLang("), false);

const css = fs.readFileSync(path.join(root, "graph.css"), "utf8");
assert.equal(/\.agentconsole\s*\{\s*--c-bg:/m.test(css), false);
assert.equal(css.includes('[data-theme="light"]'), false);
assert.equal(css.includes("--c-bad"), false);

const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
assert.match(index, /graph\.css/);
assert.match(index, /parts_graph\.jsx/);

const invalidCopy = clone(valid.data);
invalidCopy.edges[0].ch = 0;
assert.equal(validate(invalidCopy).valid, false);

const b4Payload = {
  schema_version: "literary_b4_ui_story_graph_v2",
  book_id: "wuthering_heights",
  chapter_id: "wh_ch01",
  chapter_order: 1,
  artifact_hash: "a".repeat(64),
  story_bible_artifact_hash: "b".repeat(64),
  provider_calls: 0,
  nodes: [
    {
      node_id: "entity_a",
      label: "Entity A",
      kind: "person",
      record_class: "named_entity_candidate",
      member_chapters: ["wh_ch01"],
      surface_forms: ["Entity A"],
      claims: { role_or_occupation: "tenant" },
    },
    {
      node_id: "entity_b",
      label: "Entity B",
      kind: "place",
      record_class: "named_entity_candidate",
      member_chapters: ["wh_ch01"],
      surface_forms: ["Entity B"],
      claims: {},
    },
  ],
  edges: [{
    edge_id: "edge_ab",
    source_node_id: "entity_a",
    target_node_id: "entity_b",
    relation: "resides_at",
    relation_family: "resides_at",
    chapter_id: "wh_ch01",
    structurally_contested: false,
    contested_group_id: null,
    effective: true,
  }],
  pending: [{
    pending_id: "pending_a",
    pending_kind: "unresolved_address",
    established_in_chapter: "wh_ch01",
    effective_entity_ids: ["entity_a"],
  }],
};

const mappedB4 = graph.mapLiteraryB4StoryGraph(b4Payload);
assert.equal(mappedB4.valid, true);
assert.equal(mappedB4.data.nodes[0].id, "entity_a");
assert.equal(mappedB4.data.nodes[0].first, 1);
assert.equal(mappedB4.data.nodes[0].cl.role_or_occupation[0], "tenant");
assert.equal(mappedB4.data.edges[0].s, "entity_a");
assert.equal(mappedB4.data.pending[0].cond, "Entity A");
assert.equal(validate(mappedB4.data).valid, true);

const b4Orphan = clone(b4Payload);
b4Orphan.edges[0].target_node_id = "missing_entity";
assert.equal(graph.mapLiteraryB4StoryGraph(b4Orphan).code, "literary_b4_story_graph_edge_invalid");

const snapshotSet = graph.validateStoryBibleChapterSnapshots({ 1: mappedB4.data });
assert.equal(snapshotSet.valid, true);
assert.equal(snapshotSet.maxChapter, 1);
assert.equal(graph.validateStoryBibleChapterSnapshots({ 2: mappedB4.data }).code, "story_bible_snapshot_gap");

console.log("story_bible_graph.test.cjs: 9/9 passed");
