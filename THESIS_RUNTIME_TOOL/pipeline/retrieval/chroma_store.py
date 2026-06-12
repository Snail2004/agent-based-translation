from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.agents.embedding_client import EmbeddingClient


COLLECTION_METADATA = {"hnsw:space": "cosine"}
SIMILAR_PASSAGES = "similar_passages"
NARRATIVE_MOTIFS = "narrative_motifs"
TRANSLATION_MEMORY = "translation_memory"


@dataclass(frozen=True)
class IndexReport:
    passages: int
    motifs: int
    tm: int
    model: str
    dimension: int
    embed_tokens: int
    cost_usd: float
    cache_hits: int
    skipped_existing: int

    def to_dict(self) -> dict[str, int | float | str]:
        return asdict(self)


def get_chroma_client(chroma_path: str | Path):
    import chromadb
    from chromadb.config import Settings

    Path(chroma_path).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(chroma_path),
        settings=Settings(anonymized_telemetry=False),
    )


def build_index(
    conn: sqlite3.Connection,
    embedding_client: EmbeddingClient,
    chroma_path: str | Path,
    doc_id: str,
    chapter_ids: list[str],
) -> IndexReport:
    """Build the three locked Chroma collections from frozen SQLite memory."""

    embedding_client.reset_session_usage()
    _ensure_embedding_meta(
        conn,
        model=embedding_client.config.model,
        dimension=embedding_client.config.dimensions,
    )

    resolved_chapters = _resolve_chapter_ids(conn, doc_id, chapter_ids)
    client = get_chroma_client(chroma_path)
    passages = _collection(client, SIMILAR_PASSAGES)
    motifs = _collection(client, NARRATIVE_MOTIFS)
    tm = _collection(client, TRANSLATION_MEMORY)

    passage_rows = _load_passages(conn, doc_id, resolved_chapters)
    motif_rows = _load_motifs(conn, doc_id, resolved_chapters)

    skipped_existing = 0
    skipped_existing += _upsert_rows(
        passages,
        passage_rows,
        embedding_client,
    )
    skipped_existing += _upsert_rows(
        motifs,
        motif_rows,
        embedding_client,
    )

    usage = embedding_client.session_usage
    return IndexReport(
        passages=passages.count(),
        motifs=motifs.count(),
        tm=tm.count(),
        model=embedding_client.config.model,
        dimension=embedding_client.config.dimensions,
        embed_tokens=usage.input_tokens,
        cost_usd=usage.cost_usd,
        cache_hits=usage.cache_hits,
        skipped_existing=skipped_existing,
    )


def add_tm_entry(
    client_or_collection: Any,
    en_text: str,
    vi_text: str,
    metadata: dict[str, Any],
    embedding_client: EmbeddingClient,
    *,
    entry_id: str | None = None,
) -> str:
    """Add one translation-memory vector.

    CALLER must only add translations that have passed Critic. The embedding key
    is the English source side; the Vietnamese translation is metadata payload.
    """

    collection = _resolve_collection(client_or_collection, TRANSLATION_MEMORY)
    clean_metadata = _clean_metadata(
        {
            **metadata,
            "kind": "translation_memory",
            "vi_text": vi_text,
        }
    )
    if entry_id is None:
        block_id = str(clean_metadata.get("block_id") or "")
        config = str(clean_metadata.get("config") or "")
        if not block_id or not config:
            raise ValueError("metadata must include block_id and config for TM id")
        entry_id = f"tm_{block_id}_{config}"
    embedding = embedding_client.embed([en_text])[0]
    collection.upsert(
        ids=[entry_id],
        documents=[_normalize_text(en_text)],
        metadatas=[clean_metadata],
        embeddings=[embedding],
    )
    return entry_id


def query_similar(
    client_or_collection: Any,
    text: str,
    embedding_client: EmbeddingClient,
    *,
    k: int = 5,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    collection = _resolve_collection(client_or_collection, SIMILAR_PASSAGES)
    return _query_collection(collection, text, embedding_client, k=k, where=where)


def query_motifs(
    client_or_collection: Any,
    text: str,
    embedding_client: EmbeddingClient,
    *,
    k: int = 5,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    collection = _resolve_collection(client_or_collection, NARRATIVE_MOTIFS)
    return _query_collection(collection, text, embedding_client, k=k, where=where)


def query_tm(
    client_or_collection: Any,
    text: str,
    embedding_client: EmbeddingClient,
    *,
    chapter_window: list[str],
    k: int = 5,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    collection = _resolve_collection(client_or_collection, TRANSLATION_MEMORY)
    scope_where = {"chapter_id": {"$in": chapter_window}} if chapter_window else None
    return _query_collection(
        collection,
        text,
        embedding_client,
        k=k,
        where=_and_where(where, scope_where),
    )


def _collection(client: Any, name: str):
    return client.get_or_create_collection(name=name, metadata=COLLECTION_METADATA)


def _resolve_collection(client_or_collection: Any, name: str):
    if hasattr(client_or_collection, "query") and hasattr(client_or_collection, "upsert"):
        return client_or_collection
    if hasattr(client_or_collection, "get_or_create_collection"):
        return _collection(client_or_collection, name)
    raise TypeError("Expected a Chroma client or collection")


def _query_collection(
    collection: Any,
    text: str,
    embedding_client: EmbeddingClient,
    *,
    k: int,
    where: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if k <= 0:
        return []
    embedding = embedding_client.embed([text])[0]
    kwargs: dict[str, Any] = {
        "query_embeddings": [embedding],
        "n_results": k,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where
    result = collection.query(**kwargs)
    return _flatten_query_result(result)


def _flatten_query_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    ids = (result.get("ids") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    rows: list[dict[str, Any]] = []
    for index, item_id in enumerate(ids):
        rows.append(
            {
                "id": item_id,
                "document": documents[index] if index < len(documents) else "",
                "metadata": metadatas[index] if index < len(metadatas) else {},
                "distance": distances[index] if index < len(distances) else None,
            }
        )
    return rows


def _upsert_rows(
    collection: Any,
    rows: list[dict[str, Any]],
    embedding_client: EmbeddingClient,
) -> int:
    if not rows:
        return 0
    ids = [str(row["id"]) for row in rows]
    documents = [_normalize_text(str(row["text"])) for row in rows]
    metadatas = [_clean_metadata(dict(row["metadata"])) for row in rows]
    existing_ids = set((collection.get(ids=ids) or {}).get("ids") or [])
    embeddings = embedding_client.embed(documents)
    collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        embeddings=embeddings,
    )
    return len(existing_ids)


def _load_passages(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(chapter_ids))
    rows = conn.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, text
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
        ORDER BY order_index
        """,
        [doc_id] + chapter_ids,
    ).fetchall()
    return [
        {
            "id": str(row["block_id"]),
            "text": str(row["text"] or ""),
            "metadata": {
                "doc_id": str(row["doc_id"]),
                "chapter_id": str(row["chapter_id"]),
                "block_id": str(row["block_id"]),
                "kind": "passage",
            },
        }
        for row in rows
    ]


def _load_motifs(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(chapter_ids))
    rows = conn.execute(
        f"""
        SELECT memory_id, doc_id, chapter_id, block_start, memory_type, content
        FROM memory_items
        WHERE doc_id = ?
          AND chapter_id IN ({placeholders})
          AND memory_type IN ('motif', 'chapter_summary')
        ORDER BY chapter_id, memory_type, memory_id
        """,
        [doc_id] + chapter_ids,
    ).fetchall()
    result = []
    for row in rows:
        block_start = str(row["block_start"] or "")
        memory_type = str(row["memory_type"])
        result.append(
            {
                "id": str(row["memory_id"]),
                "text": str(row["content"] or ""),
                "metadata": {
                    "doc_id": str(row["doc_id"]),
                    "chapter_id": str(row["chapter_id"]),
                    "block_id": block_start,
                    "kind": memory_type,
                    "memory_id": str(row["memory_id"]),
                },
            }
        )
    return result


def _resolve_chapter_ids(
    conn: sqlite3.Connection,
    doc_id: str,
    requested_chapter_ids: list[str],
) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT chapter_id FROM blocks WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    db_chapters = sorted(
        {str(row["chapter_id"]) for row in rows},
        key=lambda value: ([int(n) for n in re.findall(r"\d+", value)], value),
    )
    if not requested_chapter_ids:
        return db_chapters

    resolved: list[str] = []
    for requested in requested_chapter_ids:
        matches = [
            chapter
            for chapter in db_chapters
            if chapter == requested
            or chapter.endswith("_" + requested)
            or chapter.endswith(requested)
        ]
        if not matches:
            raise ValueError(f"Chapter not found for request: {requested}")
        for match in matches:
            if match not in resolved:
                resolved.append(match)
    return resolved


def _ensure_embedding_meta(
    conn: sqlite3.Connection,
    *,
    model: str,
    dimension: int,
) -> None:
    current_model = _memory_meta(conn, "embedding_model")
    current_dimension = _memory_meta(conn, "embedding_dimension")
    if current_model is not None and current_model != model:
        raise RuntimeError(
            "Embedding model mismatch; change of model requires full re-index: "
            f"{current_model} != {model}"
        )
    if current_dimension is not None and int(current_dimension) != int(dimension):
        raise RuntimeError(
            "Embedding dimension mismatch; change of dimension requires full re-index: "
            f"{current_dimension} != {dimension}"
        )
    conn.execute(
        """
        INSERT INTO memory_meta(key, value)
        VALUES ('embedding_model', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (model,),
    )
    conn.execute(
        """
        INSERT INTO memory_meta(key, value)
        VALUES ('embedding_dimension', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(dimension),),
    )
    conn.commit()


def _memory_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return str(row["value"])
    return str(row[0])


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", str(text))


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    clean: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if value is None:
            clean[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            clean[key] = value
        else:
            clean[key] = str(value)
    return clean


def _and_where(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if left and right:
        return {"$and": [left, right]}
    return left or right
