from __future__ import annotations

import json

from pipeline.prepass.span_resolver import resolve_spans


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _document():
    return {
        "schema_version": "1.5.0",
        "doc_id": "mini_doc",
        "metadata": {"source_language": "en", "target_language": "vi"},
        "chapters": [
            {
                "chapter_id": "mini_ch01",
                "blocks": [
                    {
                        "block_id": "mini_ch01_b001",
                        "clean_text": "Rum, “rum”, and rượu Rum. A rumor is not rum.",
                        "source_text": "",
                        "annotations": {},
                    },
                    {
                        "block_id": "mini_ch01_b002",
                        "clean_text": "Jim Hawkins met Jim by the cove.",
                        "source_text": "",
                        "annotations": {},
                    },
                ],
            }
        ],
    }


def _artifact():
    return {
        "chapter_id": "mini_ch01",
        "glossary_candidates": [
            {
                "source_term": "rum",
                "proposed_target_vi": "rượu rum",
                "do_not_translate": False,
                "category": "cultural",
                "block_ids": ["mini_ch01_b001"],
            }
        ],
        "entities": [
            {
                "entity_id": "ent_jim",
                "canonical_source": "Jim Hawkins",
                "aliases_source": ["Jim"],
                "entity_type": "person",
                "proposed_target_vi": "Jim Hawkins",
                "aliases_target_vi": ["Jim"],
            }
        ],
        "relations": [],
        "mention_surfaces": [
            {"entity_id": "ent_jim", "surfaces": ["Jim Hawkins", "Jim"]}
        ],
        "chapter_summary_vi": "Jim xuất hiện gần vịnh.",
        "motifs": [],
    }


def test_resolver_word_boundary_offsets(tmp_path):
    document_path = _write_json(tmp_path / "document.json", _document())
    artifact_path = _write_json(tmp_path / "mini_ch01.json", _artifact())

    resolved = resolve_spans(document_path, [artifact_path])
    rum_hits = [item for item in resolved.term_occurrences if item.source_term == "rum"]
    text = _document()["chapters"][0]["blocks"][0]["clean_text"]

    # Hand-check target spans: 0:3 "Rum", 6:9 "rum", 21:24 "Rum", 41:44 "rum".
    assert [(item.char_start, item.char_end) for item in rum_hits] == [
        (0, 3),
        (6, 9),
        (21, 24),
        (41, 44),
    ]
    assert all(text[item.char_start : item.char_end].casefold() == "rum" for item in rum_hits)
    assert "rumor" not in [text[item.char_start : item.char_end] for item in rum_hits]


def test_resolver_longest_surface_wins(tmp_path):
    document_path = _write_json(tmp_path / "document.json", _document())
    artifact_path = _write_json(tmp_path / "mini_ch01.json", _artifact())

    resolved = resolve_spans(document_path, [artifact_path])
    mentions = [
        item
        for item in resolved.entity_mentions
        if item.entity_id == "ent_jim" and item.block_id == "mini_ch01_b002"
    ]

    assert [(item.surface, item.char_start, item.char_end) for item in mentions] == [
        ("Jim Hawkins", 0, 11),
        ("Jim", 16, 19),
    ]


def test_resolver_coverage_flags(tmp_path):
    document = _document()
    artifact = _artifact()
    artifact["glossary_candidates"].append(
        {
            "source_term": "phantom term",
            "proposed_target_vi": "thuật ngữ bịa",
            "do_not_translate": False,
            "category": "other",
            "block_ids": ["mini_ch01_b001"],
        }
    )
    artifact["entities"].append(
        {
            "entity_id": "ent_ghost",
            "canonical_source": "Ghost",
            "aliases_source": ["Ghost"],
            "entity_type": "person",
            "proposed_target_vi": "Bóng ma",
            "aliases_target_vi": [],
        }
    )
    artifact["mention_surfaces"].append(
        {"entity_id": "ent_ghost", "surfaces": ["Ghost"]}
    )
    document_path = _write_json(tmp_path / "document.json", document)
    artifact_path = _write_json(tmp_path / "mini_ch01.json", artifact)

    resolved = resolve_spans(document_path, [artifact_path])

    assert resolved.coverage["terms_zero_occurrence"] == ["phantom term"]
    assert resolved.coverage["entities_zero_mention"] == ["ent_ghost"]
