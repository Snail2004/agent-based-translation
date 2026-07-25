const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = __dirname;

function loadValidator() {
  const source = fs.readFileSync(path.join(root, "parts_graph.jsx"), "utf8");
  const window = {};
  vm.runInNewContext(source, { window, React: {}, console });
  return window.validateStoryBibleGraphData;
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

const validate = loadValidator();
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
invalidPending.pending[0].cond = "";
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

console.log("story_bible_graph.test.cjs: 5/5 passed");
