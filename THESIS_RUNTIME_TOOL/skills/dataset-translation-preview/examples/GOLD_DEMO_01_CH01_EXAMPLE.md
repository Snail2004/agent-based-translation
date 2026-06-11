# Gold Demo 01 Chapter 1 Example

This example shows the expected behavior for a translation preview over `gold_demo_01` chapter 1. It is a preview target, not gold data.

## Expected Constraint Use

- Entity `e_001`: `Mira` -> `Mira`.
- Entity `e_002`: `the Clockkeeper` -> `Người Giữ Đồng Hồ`.
- Entity `e_003`: `the Hollow` -> `Thung Lũng Trống`.
- Term `g_001`: `Turning` -> `Khúc Chuyển` when it is the named event/concept.
- Term `g_002`: `hourglass` -> `đồng hồ cát`.
- Relation `rel_001`: Clockkeeper elder / Mira child.
  - Mira -> Clockkeeper: self `cháu`, address `ông`.
  - Clockkeeper -> Mira: self `ta`, address `cháu`.

## Good Preview Blocks

```json
{
  "doc_id": "gold_demo_01",
  "chapter_id": "gold_demo_01_ch01",
  "blocks": [
    {
      "block_id": "gold_demo_01_ch01_b004",
      "target_text": "\"Ông Giữ Đồng Hồ ơi,\" Mira gọi, \"sao bình minh ở đây lại đến chậm thế ạ?\"",
      "mentions": [
        {"entity_id": "e_002", "source_surface": "Clockkeeper", "target_surface": "Ông Giữ Đồng Hồ"},
        {"entity_id": "e_001", "source_surface": "Mira", "target_surface": "Mira"}
      ],
      "address_applied": {
        "pair": "e_001->e_002",
        "self_term": "cháu",
        "address_term": "ông",
        "relation_id": "rel_001"
      },
      "used_context": ["e_001", "e_002", "rel_001", "gold_demo_01_ch01"],
      "notes": "Mira addresses the elder Clockkeeper with ông/cháu."
    },
    {
      "block_id": "gold_demo_01_ch01_b005",
      "target_text": "\"Khúc Chuyển không chậm đâu,\" Người Giữ Đồng Hồ nói, tay lật chiếc đồng hồ cát. \"Chỉ là cháu đến sớm thôi.\"",
      "mentions": [
        {"term_id": "g_001", "source_surface": "Turning", "target_surface": "Khúc Chuyển"},
        {"entity_id": "e_002", "source_surface": "the Clockkeeper", "target_surface": "Người Giữ Đồng Hồ"},
        {"term_id": "g_002", "source_surface": "hourglass", "target_surface": "đồng hồ cát"}
      ],
      "address_applied": {
        "pair": "e_002->e_001",
        "self_term": "ta",
        "address_term": "cháu",
        "relation_id": "rel_001"
      },
      "used_context": ["g_001", "g_002", "e_001", "e_002", "rel_001", "gold_demo_01_ch01"],
      "notes": "Named event Turning uses Khúc Chuyển; ordinary action turning his hourglass is translated as lật chiếc đồng hồ cát."
    }
  ]
}
```

## Bad Patterns

Do not translate the named event as an ordinary verb:

```json
{"source_surface": "Turning", "target_surface": "sự xoay"}
```

Do not force the glossary event into a local verb phrase:

```text
Khúc Chuyển chiếc đồng hồ cát
```

Do not omit the context trace:

```json
{"used_context": []}
```

Every translated block should name the metadata ids it actually used.
