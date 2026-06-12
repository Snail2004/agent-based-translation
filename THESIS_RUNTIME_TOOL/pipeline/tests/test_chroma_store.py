from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pipeline.agents.embedding_client import EmbeddingClient, EmbeddingConfig
from pipeline.memory.store_init import init_db
from pipeline.retrieval.chroma_store import (
    add_tm_entry,
    build_index,
    get_chroma_client,
    query_similar,
    query_tm,
)


class ControlledEmbeddingTransport:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, **kwargs):
        texts = list(kwargs["input"])
        dimensions = int(kwargs["dimensions"])
        self.calls.append(texts)
        return {
            "data": [
                {"index": index, "embedding": _controlled_vector(text, dimensions)}
                for index, text in enumerate(texts)
            ],
            "usage": {"prompt_tokens": len(texts) * 11},
        }


def _client(tmp_path: Path) -> EmbeddingClient:
    return EmbeddingClient(
        EmbeddingConfig(
            model="text-embedding-3-large",
            dimensions=4,
            batch_size=64,
            pricing={"input": 0.13},
        ),
        tmp_path / "embedding_cache.sqlite3",
        transport=ControlledEmbeddingTransport(),
    )


def _controlled_vector(text: str, dimensions: int) -> list[float]:
    lowered = text.lower()
    vector = [0.0] * dimensions
    if "captain" in lowered or "inn" in lowered:
        vector[0] = 1.0
    elif "rum" in lowered or "song" in lowered:
        vector[1] = 1.0
    elif "apple" in lowered or "barrel" in lowered:
        vector[2] = 1.0
    else:
        vector[3] = 1.0
    return vector


def _fixture_db(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "memory.sqlite3")
    conn.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('doc1', 'job1', 'source.txt', '{}')
        """
    )
    blocks = [
        ("b1", "doc1", 1, "book_ch01", "The old captain stayed at the inn."),
        ("b2", "doc1", 2, "book_ch01", "A sailor sang of rum and the sea."),
        ("b3", "doc1", 3, "book_ch02", "Jim hid by the apple barrel."),
    ]
    conn.executemany(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, chapter_id, text)
        VALUES (?, ?, ?, ?, ?)
        """,
        blocks,
    )
    memory_items = [
        (
            "mi1",
            "doc1",
            "motif",
            "chapter",
            "book_ch01",
            "b1",
            "The captain brings danger into the inn.",
            "approved",
        ),
        (
            "mi2",
            "doc1",
            "chapter_summary",
            "chapter",
            "book_ch02",
            "b3",
            "Jim listens near the apple barrel.",
            "approved",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO memory_items (
            memory_id, doc_id, memory_type, scope, chapter_id,
            block_start, content, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        memory_items,
    )
    conn.commit()
    return conn


def test_build_index_counts_and_metadata(tmp_path):
    conn = _fixture_db(tmp_path)
    client = _client(tmp_path)

    report = build_index(conn, client, tmp_path / "chroma", "doc1", ["ch01", "ch02"])
    chroma = get_chroma_client(tmp_path / "chroma")
    passages = chroma.get_collection("similar_passages")
    motifs = chroma.get_collection("narrative_motifs")
    tm = chroma.get_collection("translation_memory")
    row = passages.get(ids=["b1"])["metadatas"][0]

    assert report.passages == 3
    assert report.motifs == 2
    assert report.tm == 0
    assert passages.count() == 3
    assert motifs.count() == 2
    assert tm.count() == 0
    assert row == {
        "doc_id": "doc1",
        "chapter_id": "book_ch01",
        "block_id": "b1",
        "kind": "passage",
    }
    assert conn.execute(
        "SELECT value FROM memory_meta WHERE key = 'embedding_model'"
    ).fetchone()["value"] == "text-embedding-3-large"


def test_index_idempotent(tmp_path):
    conn = _fixture_db(tmp_path)
    client = _client(tmp_path)
    chroma_path = tmp_path / "chroma"

    first = build_index(conn, client, chroma_path, "doc1", ["ch01", "ch02"])
    second = build_index(conn, client, chroma_path, "doc1", ["ch01", "ch02"])
    chroma = get_chroma_client(chroma_path)

    assert first.passages == second.passages == 3
    assert first.motifs == second.motifs == 2
    assert second.cache_hits == 5
    assert second.skipped_existing == 5
    assert chroma.get_collection("similar_passages").count() == 3
    assert chroma.get_collection("narrative_motifs").count() == 2


def test_model_mismatch_raises(tmp_path):
    conn = _fixture_db(tmp_path)
    conn.execute(
        "INSERT INTO memory_meta(key, value) VALUES ('embedding_model', 'other-model')"
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="re-index"):
        build_index(conn, _client(tmp_path), tmp_path / "chroma", "doc1", ["ch01"])


def test_tm_add_and_scope_query(tmp_path):
    chroma = get_chroma_client(tmp_path / "chroma")
    client = _client(tmp_path)

    add_tm_entry(
        chroma,
        "The old captain stayed at the inn.",
        "Vi 1",
        {"doc_id": "doc1", "chapter_id": "book_ch01", "block_id": "b1", "config": "S3"},
        client,
    )
    add_tm_entry(
        chroma,
        "The captain shouted at the inn.",
        "Vi 2",
        {"doc_id": "doc1", "chapter_id": "book_ch02", "block_id": "b2", "config": "S3"},
        client,
    )
    add_tm_entry(
        chroma,
        "The captain watched the door.",
        "Vi 3",
        {"doc_id": "doc1", "chapter_id": "book_ch03", "block_id": "b3", "config": "S3"},
        client,
    )

    hits = query_tm(
        chroma,
        "captain at the inn",
        client,
        chapter_window=["book_ch02", "book_ch03"],
        k=5,
    )

    assert {hit["metadata"]["chapter_id"] for hit in hits} == {"book_ch02", "book_ch03"}
    assert {hit["metadata"]["vi_text"] for hit in hits} == {"Vi 2", "Vi 3"}


def test_query_similar_topk(tmp_path):
    conn = _fixture_db(tmp_path)
    client = _client(tmp_path)
    chroma_path = tmp_path / "chroma"
    build_index(conn, client, chroma_path, "doc1", ["ch01", "ch02"])

    hits = query_similar(
        get_chroma_client(chroma_path),
        "the old captain at the inn",
        client,
        k=2,
    )

    assert len(hits) == 2
    assert hits[0]["id"] == "b1"
    assert hits[0]["metadata"]["kind"] == "passage"
