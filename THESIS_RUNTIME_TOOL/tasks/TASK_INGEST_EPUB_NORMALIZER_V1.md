# TASK: Unified EPUB Normalizer v1

Status: IMPLEMENTED; APP AND DOWNSTREAM WIRING PENDING

## Goal

Convert an EPUB into the current translation tool's stripped `document.json`
shape while preserving enough structural evidence for technical and literary
pipelines to choose only genuine translatable units.

This task is input normalization only. It does not edit or invoke the D2L
Builder, literary Builder, Translator, Context Engine, app UI, or any LLM.

## Contract

The normalizer emits two artifacts:

1. `document.json`, schema `1.5.0`, loadable by
   `pipeline.ingest.document_loader`.
2. `structure_manifest.json`, schema `epub_structure_manifest_v1`, containing
   source/package hashes, native EPUB navigation evidence, unit hierarchy,
   source map, exact-cover proof and routing policy.

Every extracted block belongs to exactly one flat unit. No source content is
silently deleted. Every unit has exactly one role:

- `front_matter`
- `content_unit`
- `container`
- `back_matter`
- `unknown`

Only `content_unit` is listed in `translatable_chapter_ids`. `unknown` is kept
and marked `review_required`; it is never guessed into a chapter. Containers
such as Part/Volume remain in the manifest and preserve parent-child links but
are not sent to Translator as ordinary chapters.

## Evidence priority

1. EPUB3 `nav`, landmarks and package/spine semantic types.
2. EPUB2 NCX and OPF guide.
3. Pandoc's complete ordered AST and heading tree.
4. Deterministic repeated-heading evidence as a fallback.

Pandoc is the content-preserving AST backbone, not a chapter classifier.
Book-specific names, expected chapter counts and answer-shaped rules are
forbidden in runtime code.

## Provenance

Each canonical block is mapped to:

- native EPUB file;
- nearest EPUB/unit anchor when available;
- deterministic Pandoc AST path.

The manifest declares this precision honestly. It does not invent page or
character offsets that the EPUB does not contain.

## Acceptance

- Pandoc completes every EPUB in the existing 11-source corpus.
- Ordered extracted blocks have exact coverage: 100%, no overlap, no missing.
- The five existing normalized references expose the same boundary families:
  Canterville 7 chapters, Christmas Carol 5 staves, Yellow Wallpaper 1 story
  unit, Frankenstein 29 narrative units, Treasure Island 6 containers and 34
  narrative chapters.
- Output is deterministic for identical source bytes and tool versions.
- `document.json` passes the current stripped-document loader contract.
- Ambiguity remains visible as review work.

## Integration boundary

The literary and technical sessions may consume
`metadata.normalization.translatable_chapter_ids` or the manifest after this
task lands. This task does not change their iteration logic. App wiring is a
separate workstream.

## Verification result

- Focused ingest/loader suite: 26 passed.
- Full pipeline suite: 744 passed, 1 skipped, 2 environment failures because
  this isolated worktree intentionally does not contain the frozen
  `data/jobs/d2l_p1/memory.sqlite3` fixture.
- Eleven-source corpus, repeat 2: 11/11 deterministic structure hashes, exact
  block coverage 1.0 for every source.
- CLI smoke: Canterville output loaded into the current SQLite schema with 12
  retained units, 235 blocks and zero loader warnings; 7 content units were
  eligible for translation.
