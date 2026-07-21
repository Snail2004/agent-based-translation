# TASK: Unified Input Normalization Benchmark v1

Status: COMPLETE
Owner: Khóa luận (Chuẩn hóa đầu vào)
Mode: offline, 0-API, no UI/backend wiring

## 0. Objective

Measure multiple deterministic parsers on the same EPUB, Markdown, HTML and TXT
sources before selecting a production input-normalization stack. This task does
not replace the live extractor and does not change the canonical dataset schema.

## 1. Arms

1. `app_current`: exact parser currently used by the thesis app, invoked read-only.
2. `pandoc`: Pandoc JSON AST converted into a common observation model.
3. `docling`: Docling local conversion traversed through `DoclingDocument`.

An unavailable arm is reported as unavailable; it must never silently fall back
to another parser under the same name.

## 2. Common Observation Model

Each arm emits ordered blocks with:

- semantic kind observed by that parser;
- normalized text;
- heading level when available;
- source pointer or structural pointer;
- whether native source provenance exists.

Repeated headings may form `chapter_candidate` units. If no trustworthy repeated
boundary exists, the result is one `document_unit`; the benchmark must not invent
`Chapter 1` and claim that the source contained a chapter.

This model is benchmark-only. It is not a new live schema and must not be imported
by D2L or Literary runtime code.

## 3. Metrics

Per arm and source:

- runtime and deterministic output hash;
- unit, block and heading counts;
- block-kind distribution;
- extracted character and lexical-token counts;
- empty, duplicate, replacement-character and oversized-block rates;
- structural-pointer and native-provenance coverage;
- fallback-to-document-unit flag.

Pairwise arms:

- token multiset coverage in both directions;
- ordered token-shingle overlap;
- unit-count and heading-count deltas.

These metrics expose disagreement. They do not automatically declare one arm
semantically correct.

## 4. Corpus

Minimum empirical matrix:

- Wuthering Heights EPUB;
- The Great Gatsby EPUB;
- The Canterville Ghost EPUB, HTML and TXT;
- one D2L Markdown chapter or bounded section set;
- synthetic adversarial Markdown/HTML/TXT fixtures for exact assertions.

Book-specific expected counts belong only in evaluation configuration or reports,
never in parser runtime code.

## 5. Safety And Ownership

- New code only under `pipeline/ingest/**`, `pipeline/scripts/ingest_*`,
  `pipeline/tests/test_ingest_*`, and this task.
- Do not edit app extraction, dataset schema, D2L, Literary, memory or windower.
- Do not call an LLM, provider API or network service while parsing.
- External source texts are read-only and are not copied into Git.
- Docling may stage a temporary ASCII-path copy on Windows, but provenance must
  retain the original source path and the temporary file must be deleted.

## 6. Acceptance

1. Unit tests cover hierarchy, no-heading fallback, nested Pandoc blocks,
   unavailable adapters, deterministic hashing and pairwise metrics.
2. All three arms run locally on at least one real EPUB.
3. Pandoc and current-app arms run on Markdown, HTML and TXT.
4. The report records tool versions and any failed/unsupported arm explicitly.
5. Running the same deterministic arm twice produces the same payload hash.
6. A recommendation is written only after the empirical report exists.

## 7. Non-goals

- No PDF/DOCX support in this milestone.
- No semantic chapter inference by LLM.
- No canonical ID migration.
- No backend endpoint or UI integration.
- No automatic production-parser selection.

## 8. Result

The empirical report is stored under
`data/reports/input_normalization_benchmark_v1/`. The selected direction is a
hybrid stack:

- current app parser for high-confidence EPUB and compact HTML extraction;
- Pandoc for generic Markdown, TXT, and fallback/cross-check when EPUB/HTML
  structure or content coverage is weak;
- the existing D2L-specific Markdown loader remains authoritative for the known
  D2L source layout;
- Docling is deferred to the PDF/DOCX milestone rather than used as the default
  for these four text-centric formats.

TXT remains an unsegmented `document_unit`; the benchmark does not authorize a
fake chapter or LLM chapter inference.
