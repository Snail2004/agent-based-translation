# TASK: Canonical Source Package v1

Status: P1/P2 IMPLEMENTED; P3 COORDINATED INTEGRATION PENDING

Owner: Khóa luận (Chuẩn hóa đầu vào)

Mode: offline, 0-API, additive

## 0. Goal

Preserve enough source structure and non-prose content to reconstruct a readable
translated document without changing the current runtime `document.json` schema
or forcing Literary/D2L migration.

Source of truth:

- `design/CANONICAL_SOURCE_PACKAGE_V1.md`
- `pipeline/ingest/schemas/canonical_asset_manifest_v1.schema.json`
- `pipeline/ingest/canonical_source_package.py`

## 1. Hard decisions

1. `document.json` remains schema `1.5.0`.
2. Rich source semantics live in additive `asset_manifest.json`.
3. `block_bindings` exact-cover every canonical block in document order.
4. Materialized assets live under `assets/` and are content-hashed.
5. Existing pipelines may ignore the new sidecar without behavior changes.
6. No app/backend/windower/memory wiring occurs before coordination ACK.

## 2. Phase P0: contract foundation

Deliver:

- design contract;
- closed JSON schema;
- offline sealer and cross-artifact validator;
- book-neutral conformance fixture;
- adversarial tests for stale hashes, missing bindings, foreign asset refs,
  unsafe paths and physical hash mismatch.

Acceptance:

- current `document.json` loader reads the fixture;
- schema and validator agree;
- no current normalizer output changes;
- focused tests pass;
- full ingest test suite passes.

## 3. Phase P1: adapter materialization

Implement only after Literary, D2L and App UI acknowledge the contract.

### TXT

- emit a manifest with text/structural bindings;
- normally emit zero assets;
- preserve source range provenance.

### Markdown

- materialize code fences, math, tables and raw HTML fragments;
- copy or source-reference local images;
- retain original Markdown fragment for deterministic reconstruction.

### HTML

- materialize table, code/math and relevant raw DOM fragments;
- copy or source-reference local image resources;
- retain DOM path and source line evidence.

### EPUB

- materialize embedded images/media and structured fragments;
- retain OPF/spine/nav/XHTML source-member locators;
- do not flatten recoverable assets into plain prose only.

Implementation result:

- all four existing writers emit `asset_manifest.json` without changing their
  return tuple;
- materialized payloads are content-hashed below `assets/`;
- mixed text/asset blocks retain a raw placement template plus child assets;
- source drift before or during materialization fails closed;
- authored and synthetic units preserve the existing `chapter_id` runtime
  mapping, while `unit_id` stays additive.

Acceptance:

- every format emits `asset_manifest.json`;
- all blocks are bound exactly once;
- rich blocks have recoverable assets or visible review state;
- existing `document.json` bytes and IDs remain unchanged for the same
  normalizer version;
- no book-specific rules.

Gate result:

- 78 focused normalizer/package tests passed;
- wider applicable pipeline run: 817 passed, 1 skipped, 2 frozen-DB existence
  probes deselected; an unfiltered run confirmed those missing-fixture probes
  were the only two failures in this isolated worktree.
- real EPUB canary: 12 units, 235 blocks, 54 materialized assets, 0 missing
  assets, 0 review bindings and 7 translation-eligible content units.

## 4. Phase P2: reconstruction probe

Build an offline exporter, not an app feature:

- HTML output: primary readable artifact;
- Markdown output: primary audit artifact;
- translated text is supplied by `block_id`;
- preserved assets are reinserted;
- missing/review rows are visible and fail closed;
- deterministic output hash for identical inputs.

Implementation result:

- `pipeline/ingest/source_package_exporter.py` validates the complete source
  package and a versioned `canonical_translation_overlay_v1` before writing;
- ordinary translations carry text only, while `translate_structured` rows
  carry HTML and Markdown fragments plus exact `{{asset:<asset_id>}}`
  placeholders for protected inline assets;
- missing, foreign, duplicate and stale translation rows fail before output;
- unresolved source rows fail by default; explicit `review_mode=markers` emits
  visible HTML/Markdown warnings and records them in the export manifest;
- HTML is the readable artifact, Markdown retains block/unit audit comments,
  and both outputs plus copied assets are content-hashed;
- output is written through a new sibling directory and atomically published;
  an existing output directory or an output below the source package is never
  overwritten.

Offline CLI:

```powershell
python -m pipeline.scripts.export_source_package `
  --package <canonical-source-package> `
  --translations <translation-overlay.json> `
  --output-dir <new-output-directory>
```

Gate result:

- 18 adversarial exporter probes passed, including the primary rich EPUB;
- 134 ingest/package/loader tests passed;
- full applicable pipeline suite: 841 passed, 1 skipped and 2 deselected;
  the two deselections are the pre-existing frozen-DB existence probes because
  `data/jobs/d2l_p1/memory.sqlite3` is intentionally absent from this isolated
  Input Normalization worktree.

## 5. Phase P3: coordinated integration

After P1/P2 gates:

- backend exposes package/asset data read-only;
- App UI adds one structure review surface rather than per-block flag clutter;
- D2L/Literary admit blocks by unit and block policy;
- shared files are changed only on a dedicated integration branch.

## 6. Out of scope

- no PDF adapter in this task;
- no native PDF reconstruction;
- no LLM structure inference;
- no changes to Builder, Translator, Context Engine or governance;
- no source-specific expected chapter counts in runtime code;
- no UI edits by Input Normalization.
