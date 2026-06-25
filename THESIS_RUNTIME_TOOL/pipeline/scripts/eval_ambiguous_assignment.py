from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.ambiguous_assignment import evaluate_probe
from pipeline.eval.region_align import parse_model_specs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate EV-D2L-09 ambiguous assignment probe on labeled gold."
    )
    parser.add_argument("--gold", required=True, help="Fully labeled collision_assignment_gold.csv.")
    parser.add_argument("--db", required=True, help="Frozen memory SQLite DB.")
    parser.add_argument("--experiment", default="d2l_p3", help="Experiment id.")
    parser.add_argument("--k", type=int, default=3, help="Top-k target sentences.")
    parser.add_argument(
        "--embed-endpoint",
        default="http://localhost:1234/v1/embeddings",
        help="OpenAI-compatible embedding endpoint.",
    )
    parser.add_argument("--embed-model", default="", help="Deprecated single embedding model id.")
    parser.add_argument(
        "--models",
        default=(
            "labse=text-embedding-labse@ChristianAzinn/labse-gguf:Q8_0,"
            "bge-m3=text-embedding-bge-m3@gpustack/bge-m3-GGUF:Q8_0,"
            "e5=text-embedding-multilingual-e5-large-instruct@Ralriki/multilingual-e5-large-instruct-GGUF:Q8_0"
        ),
        help="Comma-separated alias=endpoint-model@version entries.",
    )
    parser.add_argument(
        "--position-window",
        type=int,
        default=0,
        help="Primary position_narrow sentence window. Use 0; window=1 is diagnostic.",
    )
    parser.add_argument(
        "--cache-dir",
        default="data/eval/embed_cache",
        help="On-disk embedding cache directory.",
    )
    parser.add_argument("--out", required=True, help="Output JSON report path.")
    args = parser.parse_args()

    model_specs = parse_model_specs(args.models if args.models else args.embed_model)
    report = evaluate_probe(
        gold_path=args.gold,
        db_path=args.db,
        experiment_id=args.experiment,
        k=args.k,
        embed_endpoint=args.embed_endpoint,
        model_configs=model_specs,
        position_window=args.position_window,
        cache_dir=Path(args.cache_dir),
        out_path=args.out,
    )
    print(json.dumps({
        "rows": report["rows"],
        "row_counts": report["row_counts"],
        "elapsed_seconds": report["elapsed_seconds"],
        "models": report["models"],
        "embedding": report["embedding"],
        "frozen_db_sha256_first16": report["frozen_db_sha256_first16"],
        "out": args.out,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
