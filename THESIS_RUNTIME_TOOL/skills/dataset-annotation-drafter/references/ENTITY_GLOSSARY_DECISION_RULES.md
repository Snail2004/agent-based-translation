# Entity vs Glossary Decision Rules

Use this file before deciding whether a surface belongs in `entity_candidates` or `glossary_candidates`.

## Entity

Use an entity for:

- People and characters.
- Places and geographic names.
- Organizations, institutions, ships, houses, named objects, or other narrative actors.
- Concepts that behave like a named thing in the story world.

Examples:

```text
Margaret Saville -> entity person
Mrs. Saville -> same entity as Margaret Saville
Petersburgh -> entity place
North Pacific Ocean -> entity place
```

## Glossary

Use glossary for:

- Common nouns or phrases that need consistent translation.
- Repeated literary terms.
- Culturally or narratively important concepts that are not named entities.
- Terms where a forbidden target prevents a recurring mistranslation.

Prefer glossary candidates when:

- The term appears at least twice in the chapter or nearby context.
- The term is a named concept that must stay consistent.
- The term has a high risk of wrong literal translation.

Examples:

```text
enterprise -> glossary, if it recurs as Walton's undertaking/expedition
undertaking -> glossary, if it recurs as a motif of ambition
```

## Do Not Tag

Do not tag:

- Ordinary pronouns by themselves (`he`, `she`, `her`, `I`) unless the task explicitly asks for pronoun mention candidates and context is unambiguous.
- Ordinary adjectives/adverbs (`strange`, `quietly`, `curious`) unless they are named motifs in the guideline.
- One-off decorative phrases with no consistency need.
- A surface already modeled as an entity.

## No Dual-Tagging

A surface must not be both entity and glossary in the same candidate set.

Bad:

```text
North Pacific Ocean -> entity place
North Pacific Ocean -> glossary term
```

Good:

```text
North Pacific Ocean -> entity place only
```

When in doubt between entity and glossary:

1. If it is a proper noun, choose entity.
2. If it is a place/person/organization/named object, choose entity.
3. If it is a common term requiring target consistency, choose glossary.
4. If uncertain, emit a warning and do not tag it.

## Draft Targets

The agent may suggest Vietnamese targets:

- `suggested_canonical_target` for entities.
- `suggested_expected_target`, `suggested_allowed_variants`, and `suggested_forbidden_variants` for glossary.

These are drafts. Human review decides the final target.
