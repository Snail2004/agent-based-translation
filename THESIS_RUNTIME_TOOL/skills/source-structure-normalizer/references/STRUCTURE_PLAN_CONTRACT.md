# StructurePlan Contract

## Input

The agent receives one JSON object:

```json
{
  "doc_id": "book_id",
  "source_format": "txt",
  "source_fingerprint": "151:abcdef1234567890",
  "parts": [
    {
      "index": 0,
      "text": "THE CANTERVILLE GHOST",
      "n_lines": 1,
      "is_heading_candidate": true
    }
  ]
}
```

`doc_id`, `source_format`, and `source_fingerprint` identify one exact source
candidate. If the same work is tested as both TXT and EPUB, each format must use a
different candidate JSON and a different StructurePlan. A plan for one fingerprint
must not be applied to another fingerprint.

EPUB parts may include:

```json
{
  "spine_index": 1,
  "nav_title": "I",
  "doc_type_tokens": ["bodymatter"],
  "heading_level": 2,
  "source_ref": { "href": "OEBPS/body.xhtml", "anchor": "ch1" }
}
```

## Output

Return only this JSON object:

```json
{
  "doc_id": "<echo input doc_id>",
  "source_fingerprint": "<echo input source_fingerprint exactly>",
  "drop_parts": [
    { "part_index": 0, "reason": "front_matter" }
  ],
  "chapter_headings": [
    { "part_index": 26, "title": "I" }
  ],
  "merge_parts": [
    { "part_indices": [31, 32], "reason": "hard_wrapped_paragraph", "separator": " " }
  ],
  "epub_section_roles": [
    { "spine_index": 0, "role": "front_matter", "reason": "title_page" }
  ],
  "flags": [
    { "part_index": 88, "flag": "needs_human_check", "note": "possible joined paragraphs" }
  ],
  "confidence": 0.85,
  "notes": "short note"
}
```

## Allowed Values

`drop_parts[].reason`:

```text
title_page
copyright
imprint
gutenberg_license
uncopyright
toc_repeat
colophon
front_matter
back_matter
```

`merge_parts[].separator`:

```text
" "
"\n\n"
```

`epub_section_roles[].role`:

```text
body
front_matter
back_matter
```

## Decision Rules

- Drop separate title pages, author/illustrator credit lines, imprint/year lines, repeated TOC, illustration lists, Project Gutenberg notes, license blocks, colophon, and publisher ads.
- Keep narrative body text unless there is clear evidence it is non-body matter.
- Mark chapter headings as short standalone chapter-start parts: `CHAPTER I`, `Chapter 3`, bare Roman numerals (`I`, `II`, `III`), bare numbers, or short section titles.
- Do not mark a book title, author line, cover, title page, or other front matter as a chapter heading.
- Do not list body parts. The applier assigns non-dropped body parts to the nearest preceding heading.
- Merge only adjacent parts that are clearly one paragraph split by hard wrapping. Use separator `" "`.
- Use separator `"\n\n"` only when preserving a paragraph boundary is intended.
- If a part before the first heading is not front matter, keep it and flag `needs_human_check`.
- If no reliable chapter headings are found, return an empty `chapter_headings` list with low confidence and flags. For a one-section story, this is correct: the applier will fall back to one chapter.
- If a title page is mixed with real body text in the same part, keep the part and flag `needs_human_check` instead of dropping it.

## Title Rules

The only generated text allowed is `chapter_headings[].title`.

The title must be derivable from the heading part text by:

- Unicode NFC
- whitespace normalization
- case change
- trailing punctuation removal
- removing a leading chapter label such as `Chapter 3`, `III.`, or `Part Two`

Never invent words that do not appear in the heading. If the heading is only `I`, keep `I`.

## Self-Check

Before returning:

- The plan was produced from one candidate JSON only, not by mixing TXT and EPUB parts.
- `source_fingerprint` is exactly copied.
- All indexes exist in input.
- No part is both dropped and a chapter heading.
- No book title, author line, cover, title page, or front matter part is used as a chapter heading.
- Chapter headings are in ascending order.
- Drop reasons and separators use allowed values.
- Doubtful body text is flagged, not dropped.
- Confidence is honest. If confidence is below `0.75`, expect human review.
