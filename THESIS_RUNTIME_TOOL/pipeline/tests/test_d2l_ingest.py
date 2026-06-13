from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.ingest.d2l_glossary import parse_glossary, store_glossary_gold
from pipeline.ingest.d2l_markdown_loader import (
    classify_block,
    load_d2l_markdown,
    parse_d2l_markdown,
)
from pipeline.memory.store_init import init_db
from pipeline.retrieval.context_builder import build_context_pack, plan_anchors
from pipeline.scripts.ingest_d2l import build_coverage
from pipeline.translate.windower import Window


def _write_sample_d2l(root: Path) -> Path:
    root.mkdir()
    (root / "index.md").write_text(
        """
```toc
:maxdepth: 2

chapter_demo/index
```
""".strip(),
        encoding="utf-8",
    )
    chapter = root / "chapter_demo"
    chapter.mkdir()
    (chapter / "index_origin.md").write_text(
        """
# Demo Chapter
:label:`chap_demo`

This agent builds a model from data.

```toc
:maxdepth: 2

section-one
```
""".strip(),
        encoding="utf-8",
    )
    (chapter / "section-one_origin.md").write_text(
        """
## Section One

Here is a machine learning model.

:label:`sec_one`

```python
x = 1

y = x + 1
```

$$
y = x + 1
$$

![A figure.](../img/demo.svg)

Final prose mentions agent again.
""".strip(),
        encoding="utf-8",
    )
    glossary = root / "glossary.md"
    glossary.write_text(
        """
# Bảng thuật ngữ

## A
| English | Tiếng Việt | Thảo luận tại |
|---|---|---|
| agent | tác nhân | |
| argument (in programming) | đối số | https://example.test/arg |

## M
| English | Tiếng Việt | Thảo luận tại |
|---|---|---|
| machine learning | học máy | |
| multi word term | thuật ngữ nhiều từ | |
""".strip(),
        encoding="utf-8",
    )
    return root


def _blocks(conn: sqlite3.Connection, block_ids: list[str]) -> list[dict]:
    placeholders = ",".join("?" * len(block_ids))
    rows = conn.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        block_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def test_parse_glossary_table_rows(tmp_path):
    root = _write_sample_d2l(tmp_path / "d2l")

    entries = parse_glossary(root / "glossary.md")

    pairs = {(entry.source_term, entry.target_term) for entry in entries}
    assert ("agent", "tác nhân") in pairs
    assert ("argument (in programming)", "đối số") in pairs
    assert ("machine learning", "học máy") in pairs
    assert len(entries) == 4
    assert all(not entry.source_term.startswith("---") for entry in entries)


def test_parse_origin_markdown_block_types(tmp_path):
    root = _write_sample_d2l(tmp_path / "d2l")

    blocks, manifest, warnings = parse_d2l_markdown(root)

    assert warnings == []
    assert len(manifest) == 2
    block_types = [block.block_type for block in blocks]
    assert block_types.count("heading") == 2
    assert "prose" in block_types
    assert "label" in block_types
    assert "code" in block_types
    assert "math_block" in block_types
    assert "image" in block_types
    code = next(block for block in blocks if block.block_type == "code")
    assert "\n\n" in code.text
    assert code.translation_mode == "passthrough"
    assert all(not block.source_path.endswith(".md") or block.source_path.endswith("_origin.md") for block in blocks)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Heading", "heading"),
        ("```python\nx = 1\n```", "code"),
        ("$$\nx = y\n$$", "math_block"),
        ("![x](x.svg)", "image"),
        (":label:`x`", "label"),
        ("Regular prose with $x$ inline.", "prose"),
    ],
)
def test_classify_block(text, expected):
    assert classify_block(text) == expected


def test_ingest_idempotent_and_provenance(tmp_path):
    root = _write_sample_d2l(tmp_path / "d2l")
    db_path = tmp_path / "memory.sqlite3"

    conn = init_db(db_path)
    report1 = load_d2l_markdown(conn, root, source_commit="abc123")
    entries = parse_glossary(root / "glossary.md")
    count1 = store_glossary_gold(conn, "d2l", entries, source_path=root / "glossary.md", source_commit="abc123")
    conn.commit()

    block_count1 = conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id = 'd2l'").fetchone()[0]
    gold_count1 = conn.execute("SELECT COUNT(*) FROM eval_glossary_gold WHERE doc_id = 'd2l'").fetchone()[0]

    report2 = load_d2l_markdown(conn, root, source_commit="abc123")
    count2 = store_glossary_gold(conn, "d2l", entries, source_path=root / "glossary.md", source_commit="abc123")
    conn.commit()

    block_count2 = conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id = 'd2l'").fetchone()[0]
    gold_count2 = conn.execute("SELECT COUNT(*) FROM eval_glossary_gold WHERE doc_id = 'd2l'").fetchone()[0]
    row = conn.execute("SELECT metadata_json FROM documents WHERE doc_id = 'd2l'").fetchone()
    metadata = json.loads(row["metadata_json"])

    assert report1.blocks == report2.blocks == block_count1 == block_count2
    assert count1 == count2 == gold_count1 == gold_count2 == 4
    assert metadata["source_commit"] == "abc123"
    assert metadata["manifest"][0]["sha256"]
    conn.close()


def test_gold_eval_only_not_injected_into_context(tmp_path):
    root = _write_sample_d2l(tmp_path / "d2l")
    conn = init_db(tmp_path / "memory.sqlite3")
    load_d2l_markdown(conn, root, source_commit="abc123")
    entries = parse_glossary(root / "glossary.md")
    store_glossary_gold(conn, "d2l", entries, source_path=root / "glossary.md", source_commit="abc123")
    conn.commit()

    registry_count = conn.execute("SELECT COUNT(*) FROM glossary_entries WHERE doc_id = 'd2l'").fetchone()[0]
    gold_count = conn.execute("SELECT COUNT(*) FROM eval_glossary_gold WHERE doc_id = 'd2l'").fetchone()[0]
    assert registry_count == 0
    assert gold_count == 4

    row = conn.execute(
        """
        SELECT block_id FROM blocks
        WHERE doc_id = 'd2l' AND block_type = 'prose' AND text LIKE '%agent%'
        ORDER BY order_index LIMIT 1
        """
    ).fetchone()
    blocks = _blocks(conn, [row["block_id"]])
    anchors = plan_anchors(conn, blocks)
    pack = build_context_pack(conn, Window("w_d2l_demo_001", [row["block_id"]], 10), anchors)

    assert anchors.term_counts == {}
    assert pack.glossary_lines == []
    conn.close()


def test_build_coverage_counts_terms(tmp_path):
    root = _write_sample_d2l(tmp_path / "d2l")
    conn = init_db(tmp_path / "memory.sqlite3")
    load_d2l_markdown(conn, root, source_commit="abc123")
    entries = parse_glossary(root / "glossary.md")

    coverage = build_coverage(conn, "d2l", entries)

    assert len(coverage) == 1
    chapter = coverage[0]
    assert chapter.chapter_id == "d2l_demo"
    assert chapter.glossary_terms_total == 4
    assert chapter.terms_present_in_chapter >= 2
    assert chapter.term_occurrences_total >= 3
    assert chapter.has_agent_term is True
    conn.close()
