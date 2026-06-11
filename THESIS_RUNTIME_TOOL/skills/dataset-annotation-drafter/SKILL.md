---
name: dataset-annotation-drafter
description: Draft safe linked annotation candidate JSON for one AI-LAB literary chapter. Use when Codex, Claude, or another LLM agent must propose entity, glossary, character-relation (Vietnamese address/xưng hô), discourse, and chapter-summary metadata from extracted clean_text while preserving identity links, avoiding offsets, avoiding dual-tagging, and keeping AI output as draft only.
---

# Dataset Annotation Drafter

Use this skill to draft linked annotation candidates for **one chapter** of an AI-LAB dataset project.

The agent does **not** write dataset files, compute spans, translate the chapter, or mark anything as gold. It returns one candidate JSON object that a later backend/apply step can validate, resolve to spans, and present for human review.

## Required Context

Before drafting candidates, read:

- `references/ANNOTATION_CANDIDATE_CONTRACT.md` for input/output shape.
- `references/ENTITY_GLOSSARY_DECISION_RULES.md` before choosing entity vs glossary.
- `references/LINKAGE_RULES.md` before linking mentions, discourse, and summary.
- `examples/FRANKENSTEIN_CH01_EXAMPLE.md` when you need a concrete gold-style example.

## Workflow

1. Read the input chapter blocks and any `known_entities` / `known_terms` / `known_relations`.
2. Identify entity candidates first. Reuse `existing_entity_id` when a known entity clearly matches; otherwise create a stable `entity_key`.
3. Identify glossary candidates only for terms that need translation consistency. Do not dual-tag a surface already modeled as an entity.
4. Draft discourse candidates for speaker/addressee only when the block evidence is clear.
5. Draft relation candidates for character pairs with clear text evidence — reuse `existing_relation_id` from `known_relations` when a relation already exists; otherwise include `source_ref`, `target_ref`, `relation_type`, `suggested_address_policy`, and `evidence`; only person–person, never every pair (no N×N). This drives Vietnamese address/xưng hô.
6. Draft one chapter summary candidate with soft context and `characters_present_refs` linked to person entities only.
7. Add warnings for ambiguity instead of guessing.
8. Return exactly one JSON object matching the contract.

## Hard Rules

- Do not return `span`, `start`, or `end`; only return `surface` plus `left_context` / `right_context`.
- `surface` must be copied verbatim from `clean_text`.
- Use `entity_key` or `existing_entity_id` consistently across entity mentions, discourse, and `characters_present_refs`.
- Do not set `status` on entity candidates. Entity schema has no `status`.
- Glossary target/status are drafts only; the apply step may set glossary status to `candidate`.
- Do not dual-tag the same surface as both entity and glossary.
- Do not put places, organizations, concepts, or terms into `characters_present_refs`; use person entities only.
- Relation candidates: `source` IS the `relation_type` of `target`; emit only with evidence and only for narratively relevant pairs; `suggested_address_policy` (xưng/gọi) is a draft for human approval; for relationships that change over the story emit separate candidates with `state_label`; momentary outbursts are NOT relations (use discourse/tone).
- Never re-create a relation that already exists. Before emitting a relation, scan `known_relations` for the same directed pair (`source_entity_id` → `target_entity_id`): if it is already there with the same phase, set `existing_relation_id` to that `relation_id` (only add evidence or refine the policy) instead of emitting a new candidate. Emit a brand-new relation ONLY for a genuinely new pair, or for the same pair in a genuinely new phase (different `state_label`). The apply step rejects same-pair/same-phase relations that omit `existing_relation_id`, so duplicates waste a review cycle.
- Do not tag ordinary pronouns, stylistic adjectives/adverbs, or one-off decorative phrases unless they are narratively important and explicitly justified.
- Do not rewrite, paraphrase, translate, or annotate reference translations.
- Raw AI output is never gold; all candidates require validation and human review.

## Output

Return only the candidate JSON object. Do not wrap it in Markdown. Do not add prose before or after the JSON.

See `references/ANNOTATION_CANDIDATE_CONTRACT.md` for the exact shape.
