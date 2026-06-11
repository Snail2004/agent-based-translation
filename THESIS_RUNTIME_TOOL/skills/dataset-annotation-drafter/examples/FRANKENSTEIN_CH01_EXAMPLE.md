# Frankenstein Chapter 1 Example

This example shows the intended candidate shape for `frankenstein_epub_ch01` (`Letter I`).

It is a gold-style reference for evaluating an annotation drafter. It intentionally fixes two common mistakes:

- `North Pacific Ocean` is an entity place, not a glossary term.
- `characters_present_refs` contains only person entities.

## Good Entity Candidates

```json
[
  {
    "existing_entity_id": null,
    "entity_key": "walton",
    "canonical_source": "R. Walton",
    "suggested_canonical_target": "R. Walton",
    "entity_type": "person",
    "gender": "male",
    "aliases_source": ["Walton", "your affectionate brother", "the narrator"],
    "aliases_target": ["Walton", "người anh trai", "người kể thư"],
    "pronoun_policy": "giữ ngôi tôi trong thư; khi nói ngoài lời thư có thể dùng ông/anh ấy",
    "mentions": [
      {
        "block_id": "frankenstein_epub_ch01_b013",
        "surface": "brother",
        "left_context": "affectionate ",
        "right_context": ", R. Walton."
      },
      {
        "block_id": "frankenstein_epub_ch01_b014",
        "surface": "R. Walton",
        "left_context": "",
        "right_context": ""
      }
    ],
    "reason": "first-person letter writer and signature",
    "confidence": 1.0
  },
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
      },
      {
        "block_id": "frankenstein_epub_ch01_b004",
        "surface": "my dear sister",
        "left_context": "assure ",
        "right_context": " of my welfare"
      },
      {
        "block_id": "frankenstein_epub_ch01_b005",
        "surface": "Margaret",
        "left_context": "There, ",
        "right_context": ", the sun"
      }
    ],
    "reason": "letter recipient, sister, and addressee",
    "confidence": 1.0
  },
  {
    "existing_entity_id": null,
    "entity_key": "petersburgh",
    "canonical_source": "Petersburgh",
    "suggested_canonical_target": "Petersburg",
    "entity_type": "place",
    "gender": null,
    "aliases_source": ["St. Petersburgh", "Petersburgh"],
    "aliases_target": ["Petersburg", "St. Petersburg"],
    "pronoun_policy": "",
    "mentions": [
      {
        "block_id": "frankenstein_epub_ch01_b003",
        "surface": "Petersburgh",
        "left_context": "St. ",
        "right_context": ", Dec."
      }
    ],
    "reason": "letter dateline and journey location",
    "confidence": 1.0
  },
  {
    "existing_entity_id": null,
    "entity_key": "north_pacific_ocean",
    "canonical_source": "North Pacific Ocean",
    "suggested_canonical_target": "Bắc Thái Bình Dương",
    "entity_type": "place",
    "gender": null,
    "aliases_source": [],
    "aliases_target": ["vùng Bắc Thái Bình Dương"],
    "pronoun_policy": "",
    "mentions": [
      {
        "block_id": "frankenstein_epub_ch01_b006",
        "surface": "North Pacific Ocean",
        "left_context": "arriving at the ",
        "right_context": " through the seas"
      }
    ],
    "reason": "geographic destination, modeled as place entity",
    "confidence": 1.0
  }
]
```

## Good Glossary Candidates

```json
[
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
      },
      {
        "block_id": "frankenstein_epub_ch01_b008",
        "surface": "enterprise",
        "left_context": "my present ",
        "right_context": "."
      }
    ],
    "reason": "recurring expedition term with risk of business mistranslation",
    "confidence": 0.95
  },
  {
    "existing_term_id": null,
    "term_key": "undertaking",
    "source_term": "undertaking",
    "suggested_expected_target": "công cuộc",
    "suggested_allowed_variants": ["sự nghiệp", "việc lớn", "cuộc mạo hiểm"],
    "suggested_forbidden_variants": ["cam kết kinh doanh"],
    "occurrences": [
      {
        "block_id": "frankenstein_epub_ch01_b004",
        "surface": "undertaking",
        "left_context": "success of my ",
        "right_context": "."
      },
      {
        "block_id": "frankenstein_epub_ch01_b005",
        "surface": "undertaking",
        "left_context": "an ",
        "right_context": " such as mine"
      }
    ],
    "reason": "recurring term for Walton's expedition and ambition",
    "confidence": 0.9
  }
]
```

## Good Discourse Candidate

```json
[
  {
    "block_id": "frankenstein_epub_ch01_b004",
    "speaker_ref": "walton",
    "addressee_ref": "margaret_saville",
    "reason": "Walton writes the letter to Margaret",
    "confidence": 0.95
  }
]
```

## Good Relation Candidate

```json
[
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
        "block_id": "frankenstein_epub_ch01_b004",
        "surface": "my dear sister",
        "left_context": "assure ",
        "right_context": " of my welfare"
      },
      {
        "block_id": "frankenstein_epub_ch01_b013",
        "surface": "your affectionate brother",
        "left_context": "",
        "right_context": ", R. Walton."
      }
    ],
    "reason": "Walton addresses Margaret as his sister and signs as her brother; the sibling pair drives Vietnamese address",
    "confidence": 0.9
  }
]
```

Note: relations have no spans. Use `surface` + context for evidence; the backend resolves and writes `entity_relations.jsonl`. For a relationship that changes over the story, emit separate candidates with `state_label` (+ optional `valid_from_block_id` / `valid_to_block_id`).

## Good Summary Candidate

```json
{
  "summary_source": "Letter I introduces Robert Walton writing to Margaret Saville from St. Petersburgh. He reassures her about his safety, describes his northern journey, and frames the voyage as an ambitious enterprise driven by discovery and longing.",
  "characters_present_refs": ["walton", "margaret_saville"],
  "key_events": ["Walton departs St. Petersburgh and frames the polar voyage as his enterprise"],
  "setting": "St. Petersburgh and the imagined northern route toward the pole",
  "emotional_tone": "aspiring, intimate, anxious",
  "motifs": ["ambition", "exploration", "sibling correspondence"],
  "open_threads": ["Whether the voyage will reach the pole and at what cost"],
  "confidence": 0.9
}
```

## Bad Patterns

Do not do this:

```json
{
  "term_key": "north_pacific_ocean",
  "source_term": "North Pacific Ocean"
}
```

`North Pacific Ocean` is a place entity, not a glossary term.

Do not do this:

```json
{
  "characters_present_refs": ["walton", "margaret_saville", "petersburgh"]
}
```

`Petersburgh` is a place and belongs in entity mentions or `setting`, not in `characters_present_refs`.
