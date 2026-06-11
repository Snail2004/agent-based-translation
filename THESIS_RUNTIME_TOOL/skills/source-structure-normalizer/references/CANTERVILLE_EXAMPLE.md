# Canterville Ghost Example

This example shows why the skill outputs a plan instead of rewriting text.

## Problem

`canterville_ghost/source.txt` is a clean literary source, but the direct extractor misses the chapter structure because chapters are bare Roman numerals:

```text
I
II
III
IV
V
VI
VII
```

Direct TXT extraction result:

```text
1 chapter: Chapter 1
toc_source: none
fallback_used: true
```

## Candidate Parts

Important part indexes from `build_candidate_parts(source.txt)`:

```text
0     Project Gutenberg note
1-5   title page / author / illustrator
6-7   imprint / year
8-25  illustration list
26    I
27-48 body for I
49    II
50-55 body for II
56    III
57-68 body for III
69    IV
70-79 body for IV
80    V
81-116 body for V
117   VI
118-131 body for VI
132   VII
133-150 body for VII
```

## StructurePlan

The agent should return a plan like this:

```json
{
  "doc_id": "canterville_ghost",
  "source_fingerprint": "<copy from candidate_parts>",
  "drop_parts": [
    { "part_index": 0, "reason": "front_matter" },
    { "part_index": 1, "reason": "title_page" },
    { "part_index": 2, "reason": "title_page" },
    { "part_index": 3, "reason": "title_page" },
    { "part_index": 4, "reason": "title_page" },
    { "part_index": 5, "reason": "title_page" },
    { "part_index": 6, "reason": "imprint" },
    { "part_index": 7, "reason": "imprint" },
    { "part_index": 8, "reason": "front_matter" },
    { "part_index": 9, "reason": "front_matter" },
    { "part_index": 10, "reason": "front_matter" },
    { "part_index": 11, "reason": "front_matter" },
    { "part_index": 12, "reason": "front_matter" },
    { "part_index": 13, "reason": "front_matter" },
    { "part_index": 14, "reason": "front_matter" },
    { "part_index": 15, "reason": "front_matter" },
    { "part_index": 16, "reason": "front_matter" },
    { "part_index": 17, "reason": "front_matter" },
    { "part_index": 18, "reason": "front_matter" },
    { "part_index": 19, "reason": "front_matter" },
    { "part_index": 20, "reason": "front_matter" },
    { "part_index": 21, "reason": "front_matter" },
    { "part_index": 22, "reason": "front_matter" },
    { "part_index": 23, "reason": "front_matter" },
    { "part_index": 24, "reason": "front_matter" },
    { "part_index": 25, "reason": "front_matter" }
  ],
  "chapter_headings": [
    { "part_index": 26, "title": "I" },
    { "part_index": 49, "title": "II" },
    { "part_index": 56, "title": "III" },
    { "part_index": 69, "title": "IV" },
    { "part_index": 80, "title": "V" },
    { "part_index": 117, "title": "VI" },
    { "part_index": 132, "title": "VII" }
  ],
  "merge_parts": [],
  "epub_section_roles": [],
  "flags": [],
  "confidence": 0.9,
  "notes": "Bare Roman numerals are chapter headings; front matter is title and illustration material."
}
```

## Expected Result

After `validate_plan` and `apply_plan`, the normalized source has:

```text
7 chapters: I, II, III, IV, V, VI, VII
```

The text of each block is copied from original candidate parts by code. The agent never rewrites the story.
