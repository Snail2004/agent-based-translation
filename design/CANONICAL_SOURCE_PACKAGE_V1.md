# Canonical Source Package v1

Status: LOCKED FOR INPUT-NORMALIZATION IMPLEMENTATION

Integration status: downstream acknowledgement complete; P1 materialization and P2 offline reconstruction implemented

Owner: Khóa luận (Chuẩn hóa đầu vào)

Mode: additive, offline, 0-API

## 1. Decision

The translation runtime keeps the current stripped `document.json` contract:

- `schema_version = "1.5.0"`;
- `Document -> Chapter -> Block`;
- stable `chapter_id` and `block_id`;
- legacy `block_type` values remain readable by current loaders, windows,
  Builders, checkpoints and UI.

Source fidelity and reconstruction data are added in a sidecar
`asset_manifest.json`. No new rich `block_type` enum is introduced into
`document.json` in v1.

This is a compatibility decision, not a claim that the legacy block model fully
describes EPUB, HTML, Markdown or PDF. The runtime view remains intentionally
small; the preservation view carries the information needed to audit and
reconstruct the source.

## 2. Package layout

```text
<canonical-source-package>/
  document.json
  structure_manifest.json
  asset_manifest.json
  normalization_receipt.json
  assets/
    ...
```

The original source remains retained by the project/source store and is
identified by SHA-256. Copying it into the package is optional and belongs to
storage policy, not this schema.

### 2.1 `document.json`

Purpose: runtime text alignment.

- consumed by current SQLite loader and translation pipelines;
- contains text and stable identifiers;
- does not carry binary assets or layout-heavy source fragments;
- remains schema `1.5.0`.

### 2.2 `structure_manifest.json`

Purpose: source hierarchy and unit selection.

- maps every source block to one canonical block;
- records `unit_id -> chapter_id`;
- classifies units as content, front matter, back matter, container or unknown;
- carries `translate`, `preserve`, `exclude` or `review` decisions at unit level;
- proves exact ordered coverage.

Format-specific manifests remain valid. They are not replaced by one lossy
generic structure schema.

### 2.3 `asset_manifest.json`

Purpose: common preservation and reconstruction contract.

- binds every canonical block to its source semantic kind;
- records block-level translation policy;
- references preserved assets or source members;
- pins the exact `document.json` and `structure_manifest.json` payloads by hash;
- keeps format-specific source locators without exposing them to Translator.

Schema: `canonical_asset_manifest_v1`.

## 3. Two views of one source

The package deliberately separates two concerns:

| View | Main consumer | What it contains |
|---|---|---|
| Runtime text view | D2L/Literary Builders, Translator | legacy blocks, text, stable IDs |
| Preservation view | reviewer, exporter, UI structure inspector | rich source kind, asset, raw/source locator, placement policy |

The preservation view must never silently change the text view. A new sidecar
may be produced without invalidating existing translation checkpoints. Changing
`document.json`, `chapter_id`, `block_id` or source text still requires a new
normalization identity and downstream re-run.

## 4. Canonical semantic kinds

`block_bindings[].semantic_kind` uses a small closed set:

- `text`
- `caption`
- `table`
- `image`
- `equation`
- `code`
- `structural`
- `unknown`

Format-specific distinctions use nullable `semantic_subtype`; examples include
`chart`, `diagram`, `list_item`, `footnote`, `raw_html` and `license`.

Charts are represented as `semantic_kind = "image"` with
`semantic_subtype = "chart"`. This avoids a growing top-level enum while keeping
the distinction queryable.

`source_kind` retains the parser's original label. Code must not pretend that a
legacy `paragraph` block proves the source was ordinary prose.

## 5. Translation policy

The block-level policy is closed:

| Policy | Meaning |
|---|---|
| `translate` | ordinary text may enter a translation window |
| `translate_structured` | structure-aware translation is required, normally tables |
| `preserve` | retain source content verbatim and do not send it as prose |
| `exclude` | keep for provenance but omit from the translation run |
| `review` | user or a separately approved fallback must decide |

Unit policy and block policy are distinct:

- unit policy decides whether a chapter/unit participates in a run;
- block policy decides how each selected block is handled.

The admission rule is conjunctive. A block is sent to Translator only when its
unit is selected and its block policy permits translation.

## 6. Asset records

Every asset has:

- stable `asset_id`;
- kind and media type;
- translation policy;
- availability;
- source locator;
- optional package-relative path and content hash;
- review flag and free-form metadata.

Availability is one of:

- `materialized`: a file exists below `assets/` and its SHA-256 is verified;
- `source_reference`: the source archive/member is sufficient to recover it;
- `missing`: recovery is not currently possible and review is mandatory.

Absolute paths and `..` traversal are forbidden. A materialized asset must be
content-addressed by SHA-256.

`source_locator` is audit data, not an instruction to open an arbitrary path.
Exporters may dereference only a project-owned source archive or member that has
already passed the package source-identity check.

## 7. Block bindings

`block_bindings` exactly covers all canonical block IDs once and in document
order. Each row records:

- source kind;
- semantic kind/subtype;
- translation policy;
- referenced asset IDs;
- render role;
- review requirement.

Rich semantic kinds (`image`, `table`, `equation`, `code`) must reference an
asset unless the binding is explicitly `review_required`.

This exact-cover rule prevents a successful text extraction from hiding a lost
figure, table or equation.

## 8. Format mapping

| Source | Runtime text view | Preservation view |
|---|---|---|
| TXT | headings/dialogue/paragraph text | normally no assets; source ranges only |
| Markdown | text and stable placeholders | code fences, math, tables, images, raw HTML fragments |
| HTML | visible text and placeholders | DOM path, table/raw fragment, image URI/file, code/math fragment |
| EPUB | spine-ordered text and placeholders | OPF/spine/nav locator, XHTML fragment, embedded image/media |
| PDF (future) | extracted reading-order text | page, bbox, table/image/equation asset from layout parser |

The current milestone covers TXT, Markdown, HTML and EPUB. PDF remains a future
adapter but uses the same package boundary.

## 9. Reconstruction

Reconstruction is deterministic:

1. read translated text keyed by `block_id`;
2. traverse units and blocks in source order;
3. render translated text only for `translate` blocks;
4. render a structured translator result for `translate_structured` blocks;
5. reinsert `preserve` assets/fragments at their bound positions;
6. omit `exclude` rows from the reading output while retaining audit records;
7. halt or visibly mark unresolved `review`/`missing` rows.

Primary v1 targets:

- HTML for a readable, inspectable output;
- Markdown for research/audit output.

EPUB export may follow for literature. Native PDF reconstruction is not promised
by this contract.

## 10. Compatibility lock

The following are explicitly unchanged in v1:

- `document.json` schema and legacy block types;
- SQLite block table;
- Literary and D2L window construction;
- Builder prompts, validators and checkpoints;
- `chapter_id` as the current runtime transport key;
- App UI and backend routes.

`unit_id` is the source-structure identity. `chapter_id` remains the compatibility
mapping used by current pipelines. A later migration may allow direct unit
selection, but it is outside this lock.

## 11. Fallback decisions

The deterministic normalizer may return `review_required`; it must not invent
missing hierarchy or asset semantics.

A future human/LLM fallback may propose:

- unit boundary changes;
- unit role/policy changes;
- unknown semantic-kind classification.

Code must validate and apply the proposal. The fallback does not directly mutate
canonical artifacts, IDs or source text.

## 12. Ownership and rollout

Phase order:

1. lock schema, validator and conformance fixture;
2. obtain coordination acknowledgement from Literary, D2L and App UI;
3. materialize manifests/assets in TXT, Markdown, HTML and EPUB adapters;
4. add an offline HTML/Markdown reconstruction probe;
5. integrate backend and UI after the artifact contract passes;
6. add downstream admission filters only in coordinated shared-file changes.

Owned by Input Normalization:

- `pipeline/ingest/**`;
- ingest-only tests, fixtures, scripts and tasks;
- this design contract.

Shared hotspots requiring advance coordination:

- `pipeline/translate/windower.py`;
- `pipeline/memory/**`;
- `pipeline/retrieval/**`;
- `app/backend/routes/**`;
- `THESIS_ARCHITECTURE_LOCK.md`.

App UI continues to own `app/prototype/**`.

## 13. Conformance gate

Before format adapters emit this package:

- JSON schema is Draft 2020-12 valid;
- cross-artifact IDs and hashes match;
- block bindings exact-cover document blocks;
- all referenced assets exist or have an honest availability state;
- materialized assets stay under `assets/` and pass SHA-256 verification;
- the legacy document loader still loads `document.json`;
- no source/book-specific rule appears in runtime code;
- existing normalization output remains unchanged until the coordinated adapter
  implementation lands.

## 14. Evidence

The architecture follows the thesis requirement that preprocessing retain
hierarchy and locate figures, broadly including images, diagrams, tables and
equations, then reinsert those elements during document reconstruction. See
`reference/AMT_paper_extracted_research.md`, sections 3.1 and 3.4.

## 15. P1 implementation record

The coordinated P1 implementation now emits `asset_manifest.json` and
materialized `assets/` from the existing TXT, Markdown, HTML and EPUB writers.
Writer return shapes and `document.json` schema `1.5.0` remain unchanged.

Load-bearing implementation details:

- block bindings exact-cover the legacy runtime blocks in document order;
- text mixed with inline images or equations keeps both a raw source template
  and its child assets under `translate_structured`;
- HTML images, including empty-alt images, produce a deterministic source block
  so placement is bound to document order rather than retained only as an
  unplaced inventory asset;
- EPUB preserves OPF resources and recoverable XHTML fragments;
- source SHA-256 is checked before and after asset reads;
- authored chapters and synthetic review units both retain their existing
  `chapter_id` mapping; `unit_id` remains sidecar-only.

Gate evidence at implementation time:

- 78 focused normalizer/package tests passed;
- the wider applicable pipeline suite reached 817 passed and 1 skipped;
- two frozen-DB existence probes were explicitly deselected because
  `data/jobs/d2l_p1/memory.sqlite3` is intentionally absent from the isolated
  Input Normalization worktree; an unfiltered run confirmed those were the only
  two failures.

An offline canary on the retained Canterville EPUB produced 12 units, 235
blocks and 54 materialized assets with zero missing assets or review bindings;
7 content units remained eligible for translation.

## 16. P2 implementation record

P2 adds an offline exporter without changing the runtime document, downstream
pipelines, backend or UI. Its inputs are one validated Canonical Source Package
and one closed `canonical_translation_overlay_v1` keyed by `block_id`.

Overlay rules are intentionally strict:

- `doc_id` and the canonical `document.json` hash must match;
- every effective `translate`/`translate_structured` block appears exactly once;
- ordinary rows contain translated text only;
- structured rows contain readable HTML and auditable Markdown fragments;
- protected inline image/equation/code assets appear exactly once in each
  structured fragment as `{{asset:<asset_id>}}`; exporter code performs the
  final reinsertion;
- rows for preserved, excluded, review or foreign blocks are rejected.

The exporter traverses source units and blocks in canonical order, applies the
conjunctive unit/block policy, copies verified materialized assets, and writes:

```text
<export>/
  document.html
  document.md
  export_manifest.json
  assets/
    ...
```

`review_mode=error` is the default and produces no partial output.
`review_mode=markers` is an explicit audit mode: unresolved rows remain visible
in both artifacts and in `export_manifest.json`; they are never represented as
successful translation. Unsafe raw source/translator HTML is rejected rather
than executed by the reconstructed artifact.

Identical package and overlay bytes produce identical HTML, Markdown, copied
asset and export-manifest bytes. P2 passed 18 adversarial probes, the 134-test
ingest gate, and an applicable full-pipeline gate of 841 passed, 1 skipped and
2 intentionally deselected frozen-DB existence probes in the isolated worktree.

## 17. HTML semantic-format fidelity hardening

HTML reconstruction preserves semantic layout, not source-site pixel styling.
The HTML adapter and exporter therefore use these book-neutral rules:

- every visible image is an ordered source block; when `alt` and `title` are
  empty, its source filename is the deterministic non-semantic locator;
- `class=poem|poetry|verse|stanza|song` retains authored `<br>` boundaries and
  blank stanza separators;
- `<pre><code>` and explicit code/listing classes remain code assets;
- other `<pre>` content is translatable preformatted text whose indentation and
  line breaks survive round-trip reconstruction;
- exporter CSS supplies a readable neutral presentation and does not claim to
  reproduce the source website's fonts, margins or responsive stylesheet.

The online Alice HTML canary was re-normalized from the public source into 14
units and 853 blocks, with 12 translatable content units and no review units.
The reconstructed HTML contains the bound cover image, 15 verse blocks, two
preformatted blocks and the retained contents table. Visual probes confirmed
the shaped Mouse's Tail text and the Chapter X verse line/stanza structure.
The focused HTML/materializer/exporter gate passed 44 tests; the complete ingest
gate passed 136 tests. The unfiltered pipeline suite reached 843 passed and 1
skipped; its only two failures were the known frozen-DB existence probes in the
isolated Input Normalization worktree.

## 18. Markdown semantic-format fidelity hardening

Markdown uses two views of the same source block. `source_text` retains native
Markdown syntax for reconstruction and provenance, while `clean_text` exposes
only the readable content needed by runtime models. In particular, headings do
not send their `#` markers or attribute syntax to a model, and quote/footnote
markers are removed from runtime text without being discarded from the source
package.

The adapter and exporter apply these book-neutral rules:

- ATX and Setext heading levels are retained; authored Markdown heading anchors
  are reconstructed in both Markdown and HTML;
- lists and footnotes use `translate_structured`, so their readable content can
  be translated without flattening list/footnote shape;
- fenced code, directives and standalone math remain preserved source
  structures;
- inline `$...$` and display `$$...$$` equations have distinct asset metadata
  and are reinserted with their original inline/block role;
- display-math fences may open or close on the same line as equation content;
  decorated closers such as `$$**]` are not reinterpreted as a new opener;
- exporter-generated block comments are audit metadata only and do not claim
  byte-for-byte source reproduction.

A real D2L Linear Algebra Markdown canary produced 207 canonical blocks and 318
assets. Its pass-through reconstruction retained the exact 17-heading sequence,
180 code-fence delimiters, 34 display-math delimiters, 16 list items and 19
directives. Pandoc plain-text comparison reported full source-token coverage,
and the known multiline matrix equations retained the prose between adjacent
equation blocks. This is semantic round-trip evidence, not a claim that
whitespace or source-file bytes are identical.

## 19. Offline publication-HTML rendering

The canonical package continues to store authored TeX bytes as equation assets;
MathML is an export projection, not a replacement source representation. Before
writing `document.html`, the exporter sends all distinct TeX equations through
one local Pandoc batch with `--mathml`. Duplicate equation assets share the
same in-memory conversion result. Generated MathML retains an
`application/x-tex` annotation, so the rendered equation remains auditable
against its source expression.

Rendering is fail-safe per equation. If Pandoc is unavailable or cannot convert
one macro, that equation remains visible as escaped TeX with
`data-render-status=tex-fallback`; other valid equations still render as MathML.
The export manifest records the rendering engine and exact MathML/fallback asset
counts. No provider, LLM, CDN or network request is involved.

Publication HTML also removes the outer Markdown fence from displayed code while
preserving the untouched code asset and a safe language CSS class. Standalone
`label`/`eqlabel` directives become invisible HTML anchors. A `begin_tab` block
keeps its readable body and tab identity instead of exposing wrapper syntax;
unknown directives remain visibly escaped rather than being discarded.
