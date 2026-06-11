# Golden Sample: `gold_demo_01`

This folder is a **synthetic demo sample** for the AIL-202 dataset spec kit. It is not a real research source and must not be counted as collected dataset content.

Purpose:

- demonstrate the required file layout;
- provide a small end-to-end example that passes schema version `1.5.0`;
- give the AI-LAB team a concrete reference for annotation and web-tool development.

## Files

```text
gold_demo_01/
  document.json
  glossary.jsonl
  entities.jsonl
  manual_reference_subset.jsonl
  chapter_summaries.jsonl
  entity_relations.jsonl
  README.md
```

## Field Notes

- `doc_id`: stable source id. Every file in this folder uses `gold_demo_01`.
- `schema_version`: must be `1.5.0` for `document.json`.
- `chapter_id`: stable chapter id, e.g. `gold_demo_01_ch01`.
- `block_id`: stable translation/annotation unit id, e.g. `gold_demo_01_ch01_b002`.
- `source_text`: raw/source-like block text. It is required and must be non-empty.
- `clean_text`: normalized block text used for annotation offsets. It is required and must be non-empty.
- `block_type`: one of the literary schema enum values: `heading`, `paragraph`, `dialogue`, or `footnote`.
- `is_chapter_opening`: marks blocks used to test chapter-opening handling.
- `annotations.term_occurrences`: array of `term_id` pointers into `glossary.jsonl`.
- `annotations.entity_mentions`: array of `entity_id` pointers into `entities.jsonl`.
- `reference_translation_id`: optional pointer into `manual_reference_subset.jsonl`; the reference must belong to the same `block_id`.
- `chapter_summaries.jsonl`: optional chapter-level context sidecar. Only `summary_source` and `source` are required per line; richer fields are optional.
- `entity_relations.jsonl`: optional sidecar for directed character relations and Vietnamese address policy. Each line needs `relation_id`, `doc_id`, `source_entity_id`, `target_entity_id`, `relation_type`; `address_policy` carries the four address terms.
- `span`: `[start, end]` character offsets into the relevant `clean_text`; `end` is exclusive and must be greater than `start`.
- `quality_flags`: simple extraction/QC flags. This sample uses `["ok"]`.

## Chapter Summary Notes

`chapter_summaries.jsonl` demonstrates the chapter-level context layer. It is a sidecar rather than a field inside `document.json`, so the structural extraction record and interpretive annotation layer do not drift.

- Required for each summary line: `doc_id`, `chapter_id`, `summary_source`, `source`.
- `source` must be `human` or `ai_assisted_verified`; unverified AI drafts are not valid freeze data.
- `characters_present` uses `entity_id` values from `entities.jsonl`; the validator checks those references when present.
- Optional fields such as `key_events`, `setting`, `emotional_tone`, `motifs`, `open_threads`, and `translation_notes` are hints for downstream reuse. They are not required for the AI-LAB task.
- Chapter `emotional_tone` means the full-chapter tone; block `annotations.tone` means local block tone.

## Covered Cases

- chapter-opening blocks: `gold_demo_01_ch01_b002`, `gold_demo_01_ch02_b009`;
- headings: `b001`, `b008`;
- literary block types: heading, paragraph, and dialogue;
- dialogue with `speaker_entity_id` and `addressee_entity_id`;
- entity aliases and `pronoun_policy`;
- a character relation with `address_policy` (`entity_relations.jsonl`: Clockkeeper <-> Mira);
- repeated entities for entity consistency checks;
- glossary terms with `allowed_variants` and `forbidden_variants`;
- motif, tone, implicit meaning, and narrative notes;
- manual reference subset for a few blocks only, not a full parallel translation;
- chapter summary sidecar for two chapters;
- provenance correction example in `gold_demo_01_ch02_b011`.

This sample intentionally does not cover `footnote`; it can be added in later samples if a real literary source needs it. `formula`, `table_cell`, and `list_item` are not part of the 1.5.0 literary block type set.

## Correct Example

`document.json` block:

```json
{
  "block_id": "gold_demo_01_ch01_b002",
  "block_type": "paragraph",
  "source_text": "Mira woke before the Turning, while the valley still slept.",
  "clean_text": "Mira woke before the Turning, while the valley still slept.",
  "annotations": {
    "term_occurrences": ["g_001"],
    "entity_mentions": ["e_001"]
  },
  "reference_translation_id": "ref_001"
}
```

Matching `glossary.jsonl` occurrence:

```json
{"term_id":"g_001","occurrences":[{"block_id":"gold_demo_01_ch01_b002","span":[21,28]}]}
```

`[21,28]` points to `Turning` in the block `clean_text`.

## Common Wrong Examples

Wrong: empty clean text.

```json
{"block_id":"x","source_text":"Some text","clean_text":""}
```

Expected validator error: `clean_text` should be non-empty.

Wrong: invalid span.

```json
{"block_id":"gold_demo_01_ch01_b002","span":[28,21]}
```

Expected validator error: `invalid span (end<=start)`.

Wrong: duplicate term id.

```json
{"term_id":"g_001", ...}
{"term_id":"g_001", ...}
```

Expected validator error: `duplicate term_id`.

Wrong: reference points to another block.

```json
{
  "block_id": "gold_demo_01_ch02_b009",
  "reference_translation_id": "ref_001"
}
```

Expected validator error: `reference_translation_id ref_001 points to a different block_id`.

## Validate

From the repository root:

```powershell
python dataset_spec/tools/validate.py --dataset dataset_spec/sample/gold_demo_01 --schema dataset_spec/schema
```

Expected result:

```text
RESULT: PASS - no errors
```

This golden sample is the **pilot Phase 1 spec kit**. Team members should use it as the format reference before editing or creating larger dataset files.
