# Annotation Candidate Contract

## Input

The agent receives one chapter at a time:

```json
{
  "doc_id": "frankenstein_epub",
  "chapter_id": "frankenstein_epub_ch01",
  "blocks": [
    {
      "block_id": "frankenstein_epub_ch01_b004",
      "clean_text": "You will rejoice...",
      "block_type": "paragraph"
    }
  ],
  "known_entities": [
    {
      "entity_id": "e_001",
      "canonical_source": "Margaret Saville",
      "aliases_source": ["Mrs. Saville", "Margaret"]
    }
  ],
  "known_terms": [
    {
      "term_id": "g_001",
      "source_term": "enterprise",
      "expected_target": "dự án"
    }
  ]
}
```

Input may also include `known_relations[]` with `relation_id`, `source_entity_id`, `target_entity_id`, `relation_type`, optional phase fields, and `address_policy`.

Use `known_entities`, `known_terms`, and `known_relations` to preserve consistency across chapters. Create a new candidate only when the item is not already represented.

## Output

Return exactly one JSON object:

```json
{
  "doc_id": "frankenstein_epub",
  "chapter_id": "frankenstein_epub_ch01",
  "entity_candidates": [
    {
      "existing_entity_id": null,
      "entity_key": "margaret_saville",
      "canonical_source": "Margaret Saville",
      "suggested_canonical_target": "Margaret Saville",
      "entity_type": "person",
      "gender": "female",
      "aliases_source": ["Mrs. Saville", "my dear sister", "Margaret"],
      "aliases_target": ["bà Saville", "người chị thân yêu", "Margaret"],
      "pronoun_policy": "bà / chị / chị ấy tùy văn cảnh thư từ",
      "mentions": [
        {
          "block_id": "frankenstein_epub_ch01_b002",
          "surface": "Mrs. Saville",
          "left_context": "To ",
          "right_context": ", England."
        }
      ],
      "reason": "addressee of Walton's letters; addressed by aliases across the chapter",
      "confidence": 0.95
    }
  ],
  "glossary_candidates": [
    {
      "existing_term_id": null,
      "term_key": "enterprise",
      "source_term": "enterprise",
      "suggested_expected_target": "dự án",
      "suggested_allowed_variants": ["công cuộc", "cuộc viễn chinh"],
      "suggested_forbidden_variants": ["doanh nghiệp"],
      "occurrences": [
        {
          "block_id": "frankenstein_epub_ch01_b004",
          "surface": "enterprise",
          "left_context": "commencement of an ",
          "right_context": " which you"
        }
      ],
      "reason": "recurring literary term needing consistent translation",
      "confidence": 0.9
    }
  ],
  "discourse_candidates": [
    {
      "block_id": "frankenstein_epub_ch01_b004",
      "speaker_ref": "walton",
      "addressee_ref": "margaret_saville",
      "reason": "first-person letter from Walton to Margaret",
      "confidence": 0.9
    }
  ],
  "relation_candidates": [
    {
      "existing_relation_id": null,
      "relation_key": "walton_margaret_sibling",
      "source_ref": "walton",
      "target_ref": "margaret_saville",
      "relation_type": "sibling",
      "suggested_address_policy": {
        "source_to_target": {"self_term": "anh", "address_term": "em"},
        "target_to_source": {"self_term": "em", "address_term": "anh"}
      },
      "state_label": null,
      "valid_from_block_id": null,
      "valid_to_block_id": null,
      "evidence": [
        {
          "block_id": "frankenstein_epub_ch01_b002",
          "surface": "my dear sister",
          "left_context": "To Mrs. Saville, ",
          "right_context": ", England."
        }
      ],
      "reason": "Walton addresses Margaret as his sister; the sibling pair drives Vietnamese address",
      "confidence": 0.82
    }
  ],
  "summary_candidate": {
    "summary_source": "Letter I introduces Walton writing to Margaret Saville from St. Petersburgh about his northern voyage.",
    "characters_present_refs": ["walton", "margaret_saville"],
    "key_events": ["Walton departs from St. Petersburgh toward the northern pole"],
    "setting": "St. Petersburgh and the imagined northern route toward the pole",
    "emotional_tone": "aspiring, intimate, anxious",
    "motifs": ["ambition", "exploration", "sibling correspondence"],
    "open_threads": ["Whether the voyage will reach the pole and what it will cost"],
    "confidence": 0.85
  },
  "warnings": [
    {
      "block_id": "frankenstein_epub_ch01_b005",
      "issue": "ambiguous_surface",
      "note": "A pronoun-like surface appears more than once; no candidate emitted."
    }
  ],
  "confidence": 0.86
}
```

## Field Rules

- `doc_id` and `chapter_id`: echo the input exactly.
- `existing_entity_id`: set when the candidate clearly matches a known entity; otherwise use `null`.
- `entity_key`: required when `existing_entity_id` is `null`. Use a stable lowercase slug, e.g. `margaret_saville`.
- `entity_type`: use only `person`, `place`, `org`, or `concept`. Named objects/products/artifacts are `concept`; do not emit `named_object`.
- `existing_term_id`: set when the candidate clearly matches a known term; otherwise use `null`.
- `term_key`: required when `existing_term_id` is `null`. Use a stable lowercase slug based on `source_term`.
- `surface`: copy exact text from `clean_text`.
- `left_context` / `right_context`: provide enough local context to distinguish duplicate surfaces. Prefer 5-40 characters on each side.
- `speaker_ref`, `addressee_ref`, and `characters_present_refs`: reference either an `entity_key` emitted in this JSON or an `existing_entity_id` from input.
- `summary_candidate.key_events` / `open_threads`: optional soft hints (short free-text list). Fill only when clearly supported by the chapter; they are not graded. Map directly to `chapter_summaries.jsonl` `key_events` / `open_threads`.
- `source_ref` / `target_ref` (relations): reference an `entity_key` emitted here or an `existing_entity_id` from input. Convention: `source` IS the `relation_type` of `target` (e.g. `relation_type=parent` => source is the parent of target).
- `existing_relation_id`: set when the candidate matches a known relation; otherwise `null`. `relation_key` is required when `null` (stable lowercase slug).
- `relation_type`: free-text with recommended values (sibling/parent/child/spouse/friend/master/servant/mentor/stranger/rival/guardian).
- `suggested_address_policy`: draft Vietnamese address only; each direction is `{self_term, address_term}` (how that side refers to itself / addresses the other). A human reviewer must approve.
- Emit a relation candidate only with text evidence and only for narratively relevant pairs (prefer person–person); never create a relation for every pair (no N×N). Momentary emotional outbursts are NOT relations — use discourse/tone for those.
- Relationship change over the story: emit multiple relation candidates for the same pair distinguished by `state_label` (+ optional `valid_from_block_id` / `valid_to_block_id`, which are block ids, never offsets).
- `suggested_*`: draft translation targets only. A human reviewer must approve them.
- `confidence`: number from 0 to 1.

## Prohibited Fields

Do not emit:

```text
span
start
end
entity.status
glossary.status
reference_vi
draft_vi
```

Entity schema has no `status`. Glossary status is assigned by the apply/review workflow, not by the agent.

## Resolve/Apply Contract For Future Backend

The backend apply step should:

1. Resolve or create entities first, mapping `entity_key` to `entity_id`.
2. Resolve glossary terms after entity decisions, preventing dual-tagged surfaces.
3. Resolve spans by exact `surface` plus `left_context` / `right_context`.
4. Assert `clean_text[start:end] == surface`.
5. Flag zero-match or multi-match cases for human selection.
6. Apply discourse and summary only after referenced entities are resolved.
7. Resolve relations after entities: map `source_ref` / `target_ref` to `entity_id`; resolve `valid_from_block_id` / `valid_to_block_id` to existing blocks; keep `suggested_address_policy` as a draft for human approval; warn on overlapping phase ranges for the same pair. Relation stays in candidate/review state.
8. Preserve provenance (`ai_model`, `prompt_id`, `annotated_by`) and keep candidates in review state.

If a referenced entity candidate is rejected by review, discourse and summary links pointing to it must be flagged, not silently dropped.
