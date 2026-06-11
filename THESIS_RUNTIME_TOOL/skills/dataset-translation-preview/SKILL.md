---
name: dataset-translation-preview
description: Draft read-only structured English-to-Vietnamese translation preview JSON for one AI-LAB literary chapter. Use when Codex, Claude, or another LLM agent must consume an already validated AI-LAB dataset chapter with known entities, glossary terms, entity relations/address policy, and chapter summary, then return per-block Vietnamese preview text plus the dataset context ids it used. This skill is experimental and must not write canonical dataset files, gold reference files, or app storage.
---

# Dataset Translation Preview

Use this skill to produce a **read-only translation preview** for one AI-LAB chapter. The output demonstrates how dataset metadata can guide translation, especially names, terms, relations, and Vietnamese address/xung-ho.

This skill does **not** create gold data. It must not write to `canonical/`, `manual_reference_subset.jsonl`, `working/drafts.json`, scratch outputs, or any project storage. Return JSON only.

## Required Context

Before drafting, read:

- `references/TRANSLATION_PREVIEW_CONTRACT.md` for input/output shape and hard rules.
- `examples/GOLD_DEMO_01_CH01_EXAMPLE.md` when you need a concrete expected pattern.

## Workflow

1. Read one chapter input JSON only. Do not translate a whole book in one call.
2. For each block, translate `clean_text` into Vietnamese literary prose.
3. Apply hard dataset context that is relevant to that block:
   - entity mentions: use `canonical_target` / target aliases consistently.
   - glossary terms: use `expected_target` and avoid `forbidden_variants`.
   - dialogue: use `discourse` speaker/addressee plus `known_relations.address_policy` when available.
   - chapter memory: use `chapter_summary` for tone, key events, open threads, and continuity, without adding explanation.
4. Distinguish glossary concepts from ordinary same-surface verbs or phrases. Do not blindly replace every matching string.
5. For each block, list `mentions`, `address_applied`, and `used_context` ids that were actually used.
6. Return exactly one JSON object matching the contract.

## Hard Rules

- Read-only preview only. Never write or promote gold/reference output.
- Do not emit spans, offsets, or annotation edits.
- Do not invent entity ids, term ids, relation ids, or summary ids not present in input.
- If a relation does not apply to the current speaker/addressee pair or phase, do not force it.
- If a term surface is being used as an ordinary verb/phrase rather than the glossary concept, translate the local meaning and explain briefly in `notes`.
- `target_surface` in `mentions` must be text that appears in `target_text`.
- `used_context` must contain only ids that influenced the block.
- Raw output is for comparison/debugging only and is not an AI-LAB dataset artifact.

## Output

Return only the preview JSON object. Do not wrap it in Markdown. Do not add prose before or after the JSON.

Always read the input file and (if you write one) the output file as **UTF-8 without BOM**. Never use the OS default encoding / ANSI / cp1252 — on Windows that replaces every Vietnamese diacritic with `?` and destroys the text. In Python use `open(path, encoding="utf-8")` to read and `open(path, "w", encoding="utf-8")` to write; never use PowerShell `Out-File`/`Set-Content` without `-Encoding utf8`. After writing, confirm the file still contains real Vietnamese letters (e.g. `ông`, `ngài`), not `?`.
