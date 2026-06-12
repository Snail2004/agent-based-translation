from __future__ import annotations

import unicodedata

from pipeline.eval.consistency import count_term_matches, score_consistency


def test_tar_basic():
    # Hand calculation: 4 (block, term) pairs.
    # t1 uses expected, t2 uses allowed variant, t3 preserves source for do_not_translate,
    # t4 misses expected/allowed forms. TAR = 3/4 = 0.75.
    report = score_consistency(
        project="fixture",
        terms={
            "t1": {"source_term": "rum", "expected_target": "rượu rum"},
            "t2": {
                "source_term": "doctor",
                "expected_target": "bác sĩ",
                "allowed_variants": ["thầy thuốc"],
            },
            "t3": {
                "source_term": "Hispaniola",
                "expected_target": "",
                "do_not_translate": True,
            },
            "t4": {"source_term": "map", "expected_target": "bản đồ"},
        },
        entities={},
        term_occurrences_by_block={
            "ch01_b001": ["t1", "t2"],
            "ch01_b002": ["t3"],
            "ch01_b003": ["t4"],
        },
        entity_mentions_by_block={},
        translations_by_block={
            "ch01_b001": "Ông gọi rượu rum cho thầy thuốc.",
            "ch01_b002": "Tàu Hispaniola đã neo ở bến.",
            "ch01_b003": "Tôi nhìn tờ giấy cũ.",
        },
        block_chapters={
            "ch01_b001": "ch01",
            "ch01_b002": "ch01",
            "ch01_b003": "ch01",
        },
    )

    assert report["tar"]["pairs"] == 4
    assert report["tar"]["overall"] == 0.75
    assert report["tar"]["per_chapter"] == {"ch01": 0.75}


def test_tar_variant_not_verbatim():
    report = score_consistency(
        project="fixture",
        terms={
            "t1": {
                "source_term": "doctor",
                "expected_target": "bác sĩ",
                "allowed_variants": ["thầy thuốc"],
            }
        },
        entities={},
        term_occurrences_by_block={"b1": ["t1"]},
        entity_mentions_by_block={},
        translations_by_block={"b1": "Vị thầy thuốc bước vào."},
        block_chapters={"b1": "ch01"},
    )

    assert report["tar"]["overall"] == 1.0


def test_fvr_detects():
    report = score_consistency(
        project="fixture",
        terms={
            "t1": {
                "source_term": "rum",
                "expected_target": "rượu rum",
                "forbidden_variants": ["rượu vang"],
            }
        },
        entities={},
        term_occurrences_by_block={"b1": ["t1"]},
        entity_mentions_by_block={},
        translations_by_block={"b1": "Lão đòi rượu vang ngay lập tức."},
        block_chapters={"b1": "ch01"},
    )

    assert report["fvr"]["overall"] == 1.0
    assert report["fvr"]["violations"][0]["block_id"] == "b1"
    assert report["fvr"]["violations"][0]["term"] == "rum"
    assert report["fvr"]["violations"][0]["variant"] == "rượu vang"


def test_ecs_pronoun_excluded():
    # b1 and b3 are name mentions; b2 is a pronoun and must not enter denominator.
    # b1 uses alias_target "Jim", b3 misses approved forms. ECS = 1/2.
    report = score_consistency(
        project="fixture",
        terms={},
        entities={
            "e1": {
                "canonical_source": "Jim Hawkins",
                "canonical_target": "Jim Hawkins",
                "aliases_source": ["Jim"],
                "aliases_target": ["Jim"],
                "mentions": [
                    {"block_id": "b1", "surface": "Jim"},
                    {"block_id": "b2", "surface": "he"},
                    {"block_id": "b3", "surface": "Jim Hawkins"},
                ],
            }
        },
        term_occurrences_by_block={},
        entity_mentions_by_block={"b1": ["e1"], "b2": ["e1"], "b3": ["e1"]},
        translations_by_block={
            "b1": "Jim bước vào quán.",
            "b2": "Cậu ấy im lặng.",
            "b3": "Cậu bé nhìn ra biển.",
        },
        block_chapters={"b1": "ch01", "b2": "ch01", "b3": "ch01"},
    )

    assert report["ecs"]["overall"] == 0.5
    assert report["ecs"]["entities_scored"] == 1
    assert report["ecs"]["per_entity"][0]["name_mention_blocks"] == 2
    assert report["ecs"]["per_entity"][0]["forms_used"]["Jim"] == 1


def test_ecs_skips_unfilled_entity():
    report = score_consistency(
        project="fixture",
        terms={},
        entities={
            "e1": {
                "canonical_source": "Silver",
                "canonical_target": "",
                "aliases_source": ["Long John Silver"],
                "aliases_target": [],
                "mentions": [{"block_id": "b1", "surface": "Silver"}],
            }
        },
        term_occurrences_by_block={},
        entity_mentions_by_block={"b1": ["e1"]},
        translations_by_block={"b1": "Silver cười."},
        block_chapters={"b1": "ch01"},
    )

    assert report["ecs"]["entities_skipped"] == 1
    assert report["ecs"]["entities_scored"] == 0


def test_word_boundary():
    assert count_term_matches("The rum was gone.", "rum") == 1
    assert count_term_matches("A rumor spread.", "rum") == 0
    assert count_term_matches("RƯỢU RUM!", "rượu rum") == 1

    decomposed = "rượu rum"
    assert unicodedata.normalize("NFD", "rượu rum") != unicodedata.normalize(
        "NFC", "rượu rum"
    )
    assert count_term_matches(decomposed, "rượu rum") == 1
