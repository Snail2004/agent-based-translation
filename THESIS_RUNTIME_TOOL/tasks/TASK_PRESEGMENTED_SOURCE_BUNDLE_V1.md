# Pre-segmented Source Bundle V1

## Purpose

This contract is an input boundary for sources whose block boundaries and
identities have already been established by an external producer. It prevents
the generic Markdown parser from treating exchange markers as prose. The
contract is deliberately independent from the existing EPUB, HTML, Markdown,
TXT and PDF normalizers.

The first planned consumer is the D2L marked-source capture. This phase does
not implement that adapter and does not import the historical D2L loader. A
later adapter must convert the legacy capture into this closed bundle shape
after an explicit reservation.

## Directory shape

```text
bundle/
  manifest.json
  source.md
  block_map.json
```

`manifest.json` is validated by
`pipeline/ingest/schemas/presegmented_source_bundle_v1.schema.json` and has
the following identity fields:

```json
{
  "schema_version": "presegmented_source_bundle_v1",
  "document_id": "example_book",
  "source_format": "markdown",
  "source_file": "source.md",
  "source_sha256": "...",
  "source_utf8_bytes": 123,
  "block_map_file": "block_map.json",
  "block_map_sha256": "...",
  "block_count": 2,
  "chapter_count": 1,
  "encoding": "UTF-8",
  "line_endings": "LF",
  "text_policy": "strip_outer_whitespace_v1",
  "marker_syntax": {"prefix": "[[", "suffix": "]]"}
}
```

`block_map.json` is a separate, byte-hashed component:

```json
{
  "schema_version": "presegmented_block_map_v1",
  "document_id": "example_book",
  "chapters": [
    {"chapter_id": "chapter_1", "order_index": 0, "title": "Chapter 1"}
  ],
  "rows": [
    {
      "marker": "M0001",
      "block_id": "example_book_ch01_b001",
      "chapter_id": "chapter_1",
      "order_index": 0,
      "block_type": "heading",
      "source_sha256": "...",
      "source_utf8_bytes": 9
    }
  ]
}
```

The marker is a whole line in the source:

```text
[[M0001]]
# Chapter 1

[[M0002]]
Paragraph text.
```

Marker names are intentionally generic. The parser has no D2L-specific `B`
prefix or block-id convention.

## Acceptance rules

The standalone parser accepts a bundle only when all of these hold:

1. The manifest, map and source are regular files confined below the bundle
   root. Absolute paths, traversal, backslashes, symlinks and component
   aliasing are rejected.
2. Manifest and map are strict UTF-8 JSON objects with no duplicate keys,
   unknown keys or missing required keys. Their recorded SHA-256 values and
   byte lengths must match the files.
3. Source bytes are strict UTF-8, contain no BOM or CR bytes, and have only LF
   line endings. Content before the first marker must be whitespace only.
4. Every structural marker is a complete `[[marker]]` line, appears exactly
   once, and matches the map rows in exact order. Unknown, missing, duplicate
   or malformed marker lines fail closed. Inline `[[...]]` text is treated as
   block content and is protected by that block's byte hash; this is required
   for code/math blocks that contain array notation.
5. Map row and chapter order indices are zero-based and contiguous. Blocks are
   grouped into non-empty, contiguous chapters; no block can be omitted,
   duplicated or interleaved across chapters.
6. Each block's canonical text is the UTF-8 encoding of the content between
   marker lines after the closed `strip_outer_whitespace_v1` policy. Its hash
   and byte count must equal the map row. No inferred text or repaired span is
   accepted.
7. Rich source kinds such as `code`, `math_block`, `image` and `table` are
   retained as source kinds. Mapping them to runtime block types, admission
   channels or assets is a later producer adapter decision.

The result exposes immutable block/chapter records and a deterministic
identity hash. It does not write `document.json`, SQLite, reports or a
canonical source package.

## Deliberate exclusions

- No ZIP extraction or upload route is included in this phase.
- No D2L legacy manifest conversion is included in this phase.
- No changes are made to existing normalizers or the locked document schema.
- No LLM, API, network, model download, shared backend or credential access is
  involved.
- No project lifecycle, App UI, D2L/Literary consumer, checkpoint or runtime
  wiring is involved.

The next reservation may add a D2L adapter and produce the normalizer's
canonical `document.json` plus additive structure/asset sidecars. That adapter
must preserve this bundle's source and map identities and must not route the
marked file through generic Markdown parsing.
