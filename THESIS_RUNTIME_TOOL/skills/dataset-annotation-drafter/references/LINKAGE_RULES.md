# Linkage Rules

Use this file to preserve graph-style metadata links.

## One Identity, Many Mentions

One real-world or story-world identity must use one `entity_key` or one `existing_entity_id`.

Example:

```text
Mrs. Saville
my dear sister
Margaret
```

All three surfaces refer to the same entity:

```text
entity_key: margaret_saville
```

Do not create three separate entity candidates for the same person.

## References By Key Or Id

All later fields must reference entities by key/id, not raw names:

- `mentions[].entity_key` is implicit through the containing entity candidate.
- `discourse_candidates[].speaker_ref`
- `discourse_candidates[].addressee_ref`
- `summary_candidate.characters_present_refs`

References may use:

- an `entity_key` emitted in the current JSON, or
- an `existing_entity_id` from input.

## Discourse Links

Speaker and addressee must refer to person entities.

Use discourse candidates when there is strong evidence:

- Dialogue speaker labels.
- Letter narrator and recipient.
- Explicit addressee in the surrounding chapter.

If the evidence is weak, add a warning instead of guessing.

## Characters Present

`characters_present_refs` means people/characters present or directly active in the chapter context.

Include:

- person entities, such as `walton`, `margaret_saville`.

Do not include:

- places, such as `petersburgh`, `london`, `russia`.
- organizations.
- glossary terms.
- motifs.

Places belong in entity mentions and `setting`, not in `characters_present_refs`.

## Soft Context

These are advisory fields, not hard id links:

- `motifs`
- `emotional_tone`
- `setting`
- block-level `implicit_meaning`
- block-level `narrative_note`

Use them only when there is textual evidence. Do not invent symbolic readings to fill fields.

## Ambiguous Surfaces

If a surface appears multiple times in one block, include enough context.

Good:

```json
{
  "surface": "Her",
  "left_context": "he wrote to ",
  "right_context": " with affection"
}
```

If context cannot disambiguate the occurrence, put the case in `warnings` and do not emit a mention.
