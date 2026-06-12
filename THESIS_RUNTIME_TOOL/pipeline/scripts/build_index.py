from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from pipeline.agents.embedding_client import EmbeddingClient, load_embedding_config
from pipeline.memory.store_init import migrate_db
from pipeline.retrieval.chroma_store import (
    build_index,
    get_chroma_client,
    query_similar,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build thesis Chroma indexes from frozen memory."
    )
    parser.add_argument("--db", required=True, help="Frozen memory SQLite DB.")
    parser.add_argument("--chroma", required=True, help="Chroma persistent directory.")
    parser.add_argument(
        "--chapters",
        nargs="+",
        required=True,
        help="Chapter suffixes or full IDs to index (e.g. ch02 ch03).",
    )
    parser.add_argument(
        "--config-file",
        default="pipeline/configs/embedding.yaml",
        help="Embedding config YAML.",
    )
    parser.add_argument(
        "--cache",
        default="data/jobs/embedding_cache.sqlite3",
        help="Embedding replay cache SQLite path.",
    )
    parser.add_argument(
        "--out",
        default="data/reports/index_build_pilot.json",
        help="Output JSON report path.",
    )
    parser.add_argument(
        "--smoke-query",
        default="the old captain at the inn",
        help="Optional query_similar smoke query. Empty string disables it.",
    )
    args = parser.parse_args()

    _ensure_api_key()
    config = load_embedding_config(args.config_file)
    embedding_client = EmbeddingClient(config=config, cache_path=args.cache)
    db = migrate_db(args.db)
    try:
        doc_id = _resolve_doc_id(db)
        report = build_index(
            db,
            embedding_client,
            args.chroma,
            doc_id=doc_id,
            chapter_ids=args.chapters,
        )
    finally:
        db.close()

    report_payload = report.to_dict()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=== Index Report ===")
    print(f"passages:         {report.passages}")
    print(f"motifs:           {report.motifs}")
    print(f"translation_mem:  {report.tm}")
    print(f"model:            {report.model}")
    print(f"dimension:        {report.dimension}")
    print(f"embed_tokens:     {report.embed_tokens}")
    print(f"cost_usd:         ${report.cost_usd:.6f}")
    print(f"cache_hits:       {report.cache_hits}")
    print(f"existing_ids:     {report.skipped_existing}")
    print(f"Report written:   {out_path}")

    if args.smoke_query:
        chroma_client = get_chroma_client(args.chroma)
        hits = query_similar(
            chroma_client,
            args.smoke_query,
            embedding_client,
            k=5,
        )
        print("\n=== Smoke query_similar ===")
        print(f"query: {args.smoke_query}")
        for index, hit in enumerate(hits, start=1):
            metadata = hit["metadata"]
            preview = str(hit["document"]).replace("\n", " ")[:140]
            print(
                f"{index}. {hit['id']} "
                f"chapter={metadata.get('chapter_id')} "
                f"distance={hit['distance']:.6f} "
                f"text={preview}"
            )

    return 0


def _resolve_doc_id(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT doc_id FROM documents LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("No document found in DB")
    return str(row["doc_id"])


def _ensure_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    repo_root = Path(__file__).resolve().parents[3]
    key_path = repo_root / "API-KEY.txt"
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
            return
    raise SystemExit("OPENAI_API_KEY is not set and API-KEY.txt is missing or empty")


if __name__ == "__main__":
    raise SystemExit(main())
