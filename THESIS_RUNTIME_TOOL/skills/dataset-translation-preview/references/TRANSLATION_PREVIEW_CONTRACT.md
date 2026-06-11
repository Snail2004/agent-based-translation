# Translation Preview Contract

## Purpose

Produce one read-only Vietnamese translation preview for one chapter. The preview consumes AI-LAB dataset metadata and records which context ids influenced each translated block. It is not a canonical dataset file and is not a human reference translation.

## Input

The agent receives one chapter:

```json
{
  "doc_id": "gold_demo_01",
  "chapter_id": "gold_demo_01_ch01",
  "blocks": [
    {
      "block_id": "gold_demo_01_ch01_b004",
      "block_type": "dialogue",
      "clean_text": "\"Clockkeeper,\" Mira called, \"why does dawn arrive late here?\"",
      "discourse": {
        "speaker_entity_id": "e_001",
        "addressee_entity_id": "e_002"
      }
    }
  ],
  "known_entities": [
    {
      "entity_id": "e_001",
      "canonical_source": "Mira",
      "canonical_target": "Mira",
      "aliases_target": [],
      "pronoun_policy": "girl / she; in Vietnamese use cháu when speaking to elders"
    },
    {
      "entity_id": "e_002",
      "canonical_source": "the Clockkeeper",
      "canonical_target": "Người Giữ Đồng Hồ",
      "aliases_target": ["ông lão"],
      "pronoun_policy": "ông / ông ấy"
    }
  ],
  "known_terms": [
    {
      "term_id": "g_001",
      "source_term": "Turning",
      "expected_target": "Khúc Chuyển",
      "allowed_variants": ["khúc chuyển"],
      "forbidden_variants": ["sự xoay", "vòng quay"]
    }
  ],
  "known_relations": [
    {
      "relation_id": "rel_001",
      "source_entity_id": "e_002",
      "target_entity_id": "e_001",
      "relation_type": "elder_and_child",
      "address_policy": {
        "source_to_target": {"self_term": "ta", "address_term": "cháu"},
        "target_to_source": {"self_term": "cháu", "address_term": "ông"}
      },
      "state_label": null,
      "valid_from_block_id": null,
      "valid_to_block_id": null
    }
  ],
  "chapter_summary": {
    "summary_source": "Mira enters the Hollow and meets the Clockkeeper before the Turning.",
    "emotional_tone": "wistful",
    "motifs": ["time", "awakening"],
    "key_events": ["Mira asks why dawn comes slowly in the Hollow."],
    "open_threads": ["Why the Turning changes how time is felt."],
    "translation_notes": "Keep Turning consistent as Khuc Chuyen."
  }
}
```

## Output

Return exactly one JSON object:

```json
{
  "doc_id": "gold_demo_01",
  "chapter_id": "gold_demo_01_ch01",
  "blocks": [
    {
      "block_id": "gold_demo_01_ch01_b004",
      "target_text": "\"Ông Giữ Đồng Hồ ơi,\" Mira gọi, \"sao bình minh ở đây lại đến chậm thế ạ?\"",
      "mentions": [
        {
          "entity_id": "e_001",
          "source_surface": "Mira",
          "target_surface": "Mira"
        },
        {
          "entity_id": "e_002",
          "source_surface": "Clockkeeper",
          "target_surface": "Ông Giữ Đồng Hồ"
        }
      ],
      "address_applied": {
        "pair": "e_001->e_002",
        "self_term": "cháu",
        "address_term": "ông",
        "relation_id": "rel_001"
      },
      "used_context": ["e_001", "e_002", "rel_001", "gold_demo_01_ch01"],
      "notes": "Dialogue uses Mira-to-Clockkeeper address policy."
    }
  ]
}
```

## Output Field Rules

- `doc_id` / `chapter_id`: echo input exactly.
- `blocks`: output one item per input block, in the same order.
- `target_text`: Vietnamese translation of the block only.
- `mentions`: include entity/term surfaces that materially appear in `target_text`.
  - Entity mention item: `entity_id`, `source_surface`, `target_surface`.
  - Term mention item: `term_id`, `source_surface`, `target_surface`.
  - `target_surface` must appear in `target_text`.
- `address_applied`: use only for dialogue when a speaker/addressee pair can be resolved.
  - `pair`: `"speaker_entity_id->addressee_entity_id"`.
  - `self_term` / `address_term`: terms used in the Vietnamese output.
  - `relation_id`: include when a relation supplied the policy; otherwise omit or set null.
- `used_context`: ids that actually influenced the translation. Use entity ids, term ids, relation ids, and `chapter_id` when `chapter_summary` influenced the block.
- `notes`: short explanation for non-obvious choices, especially when a glossary-like surface is not used as the glossary concept.
- `chapter_summary`: may include `summary_source`, `emotional_tone`, `motifs`, `key_events`, `open_threads`, `setting`, `characters_present`, and `translation_notes`. Use these as soft context only; do not explain them inside the translation.

## Translation Rules

- Entity constraints are hard: keep `canonical_target` / target aliases consistent for entity mentions.
- Glossary constraints are hard only when the term is being used as that concept. Use `expected_target`; never use `forbidden_variants`.
- Do not blind-replace same-surface words. Example: if the glossary term `Turning` means a named event, translate `turning his hourglass` as a local verb phrase such as `lật chiếc đồng hồ cát`, not `Khúc Chuyển chiếc đồng hồ cát`.
- For dialogue, first resolve speaker/addressee from block `discourse`.
- Apply relation address policy when a relation matches the pair and the current block is within its optional `valid_from_block_id` / `valid_to_block_id` range.
- If no relation applies, fall back to entity `pronoun_policy`; if still unclear, use a neutral literary Vietnamese choice and note the uncertainty.
- Preserve the chapter tone from `chapter_summary` without explaining it inside the translation.

## Prohibited Output

Do not emit:

```text
span
start
end
reference_vi
draft_vi
canonical writes
manual_reference_subset writes
```

This preview is not gold and must not be promoted to canonical dataset files.
