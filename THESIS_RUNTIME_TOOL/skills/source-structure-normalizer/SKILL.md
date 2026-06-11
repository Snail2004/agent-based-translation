---
name: source-structure-normalizer
description: Generate safe StructurePlan JSON for AI-LAB TXT/EPUB literary source cleanup before extraction. Use when Codex, Claude, or another LLM agent must normalize source structure from candidate_parts by selecting front/back matter drops, chapter headings, merge_parts, EPUB section roles, and needs_human_check flags without rewriting source text.
---

# Source Structure Normalizer

Use this skill to turn `candidate_parts` from an immutable TXT/EPUB source into a `StructurePlan`.

The agent does **not** rewrite source text. It only returns JSON decisions by `part_index` and, for EPUB, `spine_index`. The backend applies and validates the plan with `services/structure_normalizer.py`.

## Required Context

Before producing a plan, read:

- `guidelines/SOURCE_STRUCTURE_NORMALIZER_SPEC.md` for the authoritative rules.
- `references/STRUCTURE_PLAN_CONTRACT.md` for the output contract and checklist.
- `references/CANTERVILLE_EXAMPLE.md` when you need a concrete example.

## Candidate Identity And Storage

`candidate_parts` are source-specific. A StructurePlan is valid only for the exact
`doc_id`, `source_format`, and `source_fingerprint` in the candidate JSON.

When the web tool builds a candidate, it writes the current project candidate to:

```text
ailab_projects/<doc_id>/working/normalized/candidate_parts.json
```

The same JSON is also returned to the browser so the user can copy or download it.
When handing work to another agent, pass that exact candidate JSON or save a copy in
the manual scratch area below.

If the user provides an `agent_structure_plan` path, write the completed
StructurePlan JSON there:

```text
ailab_projects/<doc_id>/working/normalized/agent_structure_plan.json
```

The web tool can then load that draft directly with **Load agent plan** and validate
it against `candidate_parts.json`. Keep this draft separate from
`structure_plan.json`, which is the app-written plan after validation/import.

For offline/manual agent work, keep each format/project in its own scratch folder:

```text
AILAB_SOURCES_RAW/<work_slug>/normalizer_test/<doc_id>/
  candidate_parts.json
  structure_plan.json
  normalization_report.json
  normalized_document.json
```

Example:

```text
AILAB_SOURCES_RAW/canterville_ghost/normalizer_test/canterville_ghost_txt/
AILAB_SOURCES_RAW/canterville_ghost/normalizer_test/canterville_ghost_epub/
```

Never reuse a TXT plan for an EPUB candidate, or an EPUB plan for a TXT candidate.
If the fingerprint differs, rebuild the candidate and generate a new StructurePlan.

The web tool does not read plans from `normalizer_test/` automatically. To apply a
manual plan, paste/upload the relevant `structure_plan.json` in the tool, or copy it
to `working/normalized/agent_structure_plan.json` and use **Load agent plan**. The
backend then validates it against the project candidate in `working/normalized/`.

## Workflow

1. Receive `candidate_parts` from `build_candidate_parts(source)`.
2. Identify parts that are front/back matter.
3. Identify chapter heading parts in ascending order.
4. Add `merge_parts` only when adjacent parts are clearly one hard-wrapped paragraph.
5. For EPUB, classify whole spine sections with `epub_section_roles` when possible.
6. If no reliable chapter headings exist, leave `chapter_headings` empty and flag the source instead of inventing a heading.
7. Flag uncertain parts with `needs_human_check` instead of guessing.
8. Return one JSON object only, or write that single JSON object to the provided `agent_structure_plan` path.
9. Run `validate_plan`; if it fails, repair the JSON plan rather than editing source text.
10. Run `apply_plan` to create `normalized_document.json`.

## Hard Rules

- Do not translate.
- Do not annotate glossary, entity, summary, reference, discourse, or `block_type`.
- Do not rewrite, paraphrase, normalize, or invent source content.
- Do not split a part by character offset.
- Do not drop real body text to make the source look clean.
- Do not use a book title, author line, cover, title page, or other front matter as a chapter heading.
- If a one-section story has no real chapter heading, keep `chapter_headings` empty; the backend fallback will create one chapter.
- Drop a title page only when it is a separate non-body part. If a title is mixed with body text in one part, keep the part and flag it.
- Echo `source_fingerprint` exactly.
- Use only allowed enums and JSON fields from the contract.
- If uncertain, keep the part and flag it.

## Output

Return only `StructurePlan` JSON. No Markdown, no explanation, no prose around the JSON.

See `references/STRUCTURE_PLAN_CONTRACT.md` for the exact shape.
