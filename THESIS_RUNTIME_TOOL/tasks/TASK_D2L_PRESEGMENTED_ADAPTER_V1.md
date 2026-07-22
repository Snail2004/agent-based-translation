# TASK: D2L Presegmented Adapter V1

## Status

DEC-059 implementation candidate. This task is 0-API and new-file-only.

## Purpose

Convert the sealed D2L marked-source capture into the frozen
`presegmented_source_bundle_v1` contract without segmenting Markdown again.
The adapter is a deterministic provenance bridge, not a document parser or a
translation stage.

## Authoritative input

The accepted legacy capture contains:

- `d2l_full_book_en_marked_v1.md`
- `block_map.json`
- `manifest.json`

`prompt.txt` is Web-baseline evidence and is not adapter input.

The production adapter accepts only the sealed D2L capture identity:

- source SHA-256:
  `ebc05ffca36036b5ac2b9b1e6c6daa62d8449232a22b102653e1e028ef6d62d2`
- legacy map SHA-256:
  `980ae6a472eef0c2a29ebc39007475c1413b92be4510b935f7258d0bb303afbe`
- legacy manifest SHA-256:
  `84d4eb1a63c481f9c464b5ca908f03f92541b6d6187fa7d22fed2eeeff411504`
- frozen DB provenance SHA-256:
  `64d98965f8859869931152b2aa814fb03afbf15e6a9853532fd0ef28b555c715`
- 22 contiguous chapters and 8,803 ordered blocks
- marker range `B0001` through `B8803`

The adapter never opens the SQLite database. The database hash is provenance
evidence copied from the physically sealed historical manifest.

## Conversion

1. Verify physical source, map, and manifest bytes against the sealed inventory.
2. Parse both JSON files with duplicate-key rejection and exact closed fields.
3. Verify UTF-8 without BOM, LF-only source, exact whole-line markers, row hashes,
   byte counts, IDs, kinds, order, and noninterleaved chapter runs.
4. Copy the marked source bytes unchanged.
5. Copy all legacy row fields and values mechanically into
   `presegmented_block_map_v1`.
6. Derive `chapters[]` from contiguous `chapter_id` runs. Every run must begin
   with a heading. Its title is the first source line after removing the leading
   Markdown `#` characters and surrounding whitespace.
7. Emit canonical deterministic map and manifest JSON.
8. Validate the completed directory with the frozen
   `load_presegmented_bundle()` implementation.
9. Emit a deterministic receipt and validate it against
   `d2l_presegmented_adapter_receipt_v1.schema.json`.

The output directory contains exactly:

```text
manifest.json
block_map.json
d2l_full_book_en_marked_v1.md
d2l_presegmented_adapter_receipt_v1.json
```

The receipt binds upstream physical hashes, the historical DB provenance hash,
converted artifact hashes, counts, and the frozen bundle identity. Its own
SHA-256 must be bound by the caller when validating the output.

## Fail-closed rules

Reject before publication when any of these conditions occurs:

- source, map, or manifest differs from the sealed physical inventory;
- duplicate, unknown, missing, reordered, malformed, or non-whole-line marker;
- duplicate block ID or marker;
- row hash, byte count, order, document ID, or block kind mismatch;
- BOM, CRLF, invalid UTF-8, empty block, or pre-marker content;
- interleaved chapters or a chapter without a Markdown heading title;
- output root already exists, overlaps input, contains extra files, or is
  symlinked;
- receipt, bundle, or output bytes do not match their bound identities.

Temporary output is removed after failure. Conversion never mutates the legacy
capture.

## Explicit exclusions

- no modification to the frozen generic parser or schema;
- no generic Markdown parsing or `d2l_markdown_loader.py`;
- no SQLite read or write;
- no prompt, Web output, gold translation, or reference translation input;
- no canonical runtime package, project, checkpoint, or report write;
- no ZIP, App backend, App UI, D2L consumer, or source-main wiring;
- no LLM, API, network, credential, model, or transport dependency.

## Acceptance gates

- Draft 2020-12 receipt-schema validation;
- focused success and adversarial tests;
- two byte-identical conversions from the same capture;
- real read-only D2L canary: 22 chapters, 8,803 blocks, B0001..B8803;
- frozen generic parser and unified-normalizer regression;
- complete `pipeline/tests` regression;
- exact four-file scope, compile, diff-check, and credential scan.
