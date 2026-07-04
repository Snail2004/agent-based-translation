from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import torch
from comet import download_model, load_from_checkpoint


DEFAULT_WORKDB = Path("data/jobs/exp_s0s1_full/memory.sqlite3")
DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/sf_qe_cometkiwi_wmt22.json")
DEFAULT_EXPERIMENT = "exp_s0s1_builderv2_v1"
DEFAULT_MODEL = "Unbabel/wmt22-cometkiwi-da"
DEFAULT_CHAPTERS = "d2l_multilayer_perceptrons,d2l_preliminaries"
DEFAULT_CONFIGS = "S0,S1"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score SF-QE with reference-free CometKiwi on S0/S1 experiment outputs."
    )
    parser.add_argument("--db", default=str(DEFAULT_WORKDB))
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--chapters", default=DEFAULT_CHAPTERS)
    parser.add_argument("--configs", default=DEFAULT_CONFIGS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-token-log-threshold", type=int, default=512)
    args = parser.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    chapters = [item.strip() for item in str(args.chapters).split(",") if item.strip()]
    configs = [item.strip() for item in str(args.configs).split(",") if item.strip()]
    db_sha256_before = _sha256_file(db_path)

    rows = _load_segments(db_path, experiment=args.experiment, chapters=chapters, configs=configs)
    if not rows:
        raise SystemExit("No translation rows found for requested scope.")

    started_total = time.perf_counter()
    download_started = time.perf_counter()
    checkpoint = Path(download_model(args.model))
    download_elapsed = time.perf_counter() - download_started

    checkpoint_sha_started = time.perf_counter()
    checkpoint_sha256 = _sha256_file(checkpoint)
    checkpoint_sha_elapsed = time.perf_counter() - checkpoint_sha_started

    load_started = time.perf_counter()
    model = load_from_checkpoint(str(checkpoint))
    load_elapsed = time.perf_counter() - load_started

    token_audit = _token_audit(
        model,
        rows,
        threshold=args.max_token_log_threshold,
    )

    scoreable = [row for row in rows if row["source_text"].strip() and row["output_text"].strip()]
    empty_rows = [
        {
            "chapter_id": row["chapter_id"],
            "block_id": row["block_id"],
            "config": row["config"],
            "source_empty": not bool(row["source_text"].strip()),
            "output_empty": not bool(row["output_text"].strip()),
        }
        for row in rows
        if not row["source_text"].strip() or not row["output_text"].strip()
    ]
    data = [{"src": row["source_text"], "mt": row["output_text"]} for row in scoreable]

    warmup_elapsed = 0.0
    if data:
        warmup_started = time.perf_counter()
        model.predict([data[0]], batch_size=1, gpus=0, progress_bar=False)
        warmup_elapsed = time.perf_counter() - warmup_started

    predict_started = time.perf_counter()
    output = model.predict(data, batch_size=args.batch_size, gpus=0, progress_bar=False)
    predict_elapsed = time.perf_counter() - predict_started
    scores = [float(score) for score in output.scores]
    for row, score in zip(scoreable, scores):
        row["sf_qe_score"] = score

    score_by_key = {
        (row["chapter_id"], row["block_id"], row["config"]): row["sf_qe_score"]
        for row in scoreable
    }
    paired = _paired_deltas(rows, score_by_key, configs=configs)
    per_group = _per_chapter_arm(rows, score_by_key)
    elapsed_total = time.perf_counter() - started_total

    report = {
        "metric": "SF-QE",
        "metric_version": "sf_qe_cometkiwi_wmt22_v1",
        "model": args.model,
        "device": "cpu",
        "markdown_policy": "kept_as_is_symmetric_across_arms",
        "db": str(db_path),
        "db_sha256_before": db_sha256_before,
        "db_sha256_after": _sha256_file(db_path),
        "experiment": args.experiment,
        "chapters": chapters,
        "configs": configs,
        "rows_requested": len(rows),
        "rows_scored": len(scoreable),
        "empty_rows": empty_rows,
        "comet": {
            "package_version": _package_version("unbabel-comet"),
            "torch_version": torch.__version__,
            "torch_cuda_available": torch.cuda.is_available(),
            "checkpoint": str(checkpoint),
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_sha256_elapsed_sec": checkpoint_sha_elapsed,
            "download_elapsed_sec": download_elapsed,
            "load_elapsed_sec": load_elapsed,
            "warmup_elapsed_sec": warmup_elapsed,
            "predict_elapsed_sec": predict_elapsed,
            "predict_items_per_sec": len(scoreable) / predict_elapsed if predict_elapsed else 0.0,
            "batch_size": args.batch_size,
        },
        "token_audit": token_audit,
        "per_chapter_arm": per_group,
        "paired_delta": paired,
        "bottom10_by_chapter_arm": _bottom10(rows, score_by_key),
        "elapsed_total_sec": elapsed_total,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _load_segments(
    db_path: Path,
    *,
    experiment: str,
    chapters: list[str],
    configs: list[str],
) -> list[dict[str, Any]]:
    chapter_placeholders = ",".join("?" * len(chapters))
    config_placeholders = ",".join("?" * len(configs))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""
            SELECT
                b.chapter_id,
                b.block_id,
                b.order_index,
                COALESCE(NULLIF(b.original_text, ''), b.text) AS source_text,
                tr.config,
                tr.output_text
            FROM translation_runs tr
            JOIN blocks b ON b.block_id = tr.block_id
            WHERE tr.experiment_id = ?
              AND tr.stage = 'draft'
              AND b.chapter_id IN ({chapter_placeholders})
              AND tr.config IN ({config_placeholders})
            ORDER BY b.chapter_id, b.order_index, tr.config
            """,
            [experiment, *chapters, *configs],
        ).fetchall()
    return [
        {
            "chapter_id": str(row["chapter_id"]),
            "block_id": str(row["block_id"]),
            "order_index": int(row["order_index"]),
            "source_text": str(row["source_text"] or ""),
            "config": str(row["config"]),
            "output_text": str(row["output_text"] or ""),
        }
        for row in rows
    ]


def _per_chapter_arm(
    rows: list[dict[str, Any]],
    score_by_key: dict[tuple[str, str, str], float],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        key = (row["chapter_id"], row["block_id"], row["config"])
        if key in score_by_key:
            grouped[(row["chapter_id"], row["config"])].append(score_by_key[key])
    result: dict[str, Any] = {}
    for (chapter_id, config), scores in sorted(grouped.items()):
        result.setdefault(chapter_id, {})[config] = _score_stats(scores)
    return result


def _paired_deltas(
    rows: list[dict[str, Any]],
    score_by_key: dict[tuple[str, str, str], float],
    *,
    configs: list[str],
) -> dict[str, Any]:
    if len(configs) != 2:
        return {"error": "paired_delta_requires_exactly_two_configs"}
    a, b = configs
    block_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        block_rows.setdefault((row["chapter_id"], row["block_id"]), row)
    by_chapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_pairs: list[dict[str, Any]] = []
    for (chapter_id, block_id), row in sorted(block_rows.items(), key=lambda item: (item[0][0], item[1]["order_index"])):
        key_a = (chapter_id, block_id, a)
        key_b = (chapter_id, block_id, b)
        if key_a not in score_by_key or key_b not in score_by_key:
            continue
        score_a = score_by_key[key_a]
        score_b = score_by_key[key_b]
        pair = {
            "chapter_id": chapter_id,
            "block_id": block_id,
            "order_index": row["order_index"],
            f"{a}_score": score_a,
            f"{b}_score": score_b,
            "delta": score_b - score_a,
        }
        by_chapter[chapter_id].append(pair)
        all_pairs.append(pair)
    return {
        "direction": f"{b}-{a}",
        "overall": _delta_stats(all_pairs),
        "by_chapter": {chapter: _delta_stats(pairs) for chapter, pairs in sorted(by_chapter.items())},
        "pairs": all_pairs,
    }


def _score_stats(scores: list[float]) -> dict[str, float | int]:
    ordered = sorted(scores)
    return {
        "n": len(scores),
        "mean": statistics.mean(scores) if scores else 0.0,
        "median": statistics.median(scores) if scores else 0.0,
        "p25": _quantile(ordered, 0.25),
        "p75": _quantile(ordered, 0.75),
        "min": min(scores) if scores else 0.0,
        "max": max(scores) if scores else 0.0,
    }


def _delta_stats(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    deltas = [float(pair["delta"]) for pair in pairs]
    ordered = sorted(deltas)
    return {
        "n": len(deltas),
        "mean_delta": statistics.mean(deltas) if deltas else 0.0,
        "median_delta": statistics.median(deltas) if deltas else 0.0,
        "p25_delta": _quantile(ordered, 0.25),
        "p75_delta": _quantile(ordered, 0.75),
        "pct_s1_gt_s0": sum(delta > 0 for delta in deltas) / len(deltas) if deltas else 0.0,
        "pct_abs_delta_lt_0_01": sum(abs(delta) < 0.01 for delta in deltas) / len(deltas) if deltas else 0.0,
        "min_delta": min(deltas) if deltas else 0.0,
        "max_delta": max(deltas) if deltas else 0.0,
    }


def _bottom10(
    rows: list[dict[str, Any]],
    score_by_key: dict[tuple[str, str, str], float],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row["chapter_id"], row["block_id"], row["config"])
        if key not in score_by_key:
            continue
        grouped[(row["chapter_id"], row["config"])].append(
            {
                "block_id": row["block_id"],
                "order_index": row["order_index"],
                "score": score_by_key[key],
                "source_preview": _preview(row["source_text"]),
                "output_preview": _preview(row["output_text"]),
            }
        )
    result: dict[str, Any] = {}
    for (chapter_id, config), items in sorted(grouped.items()):
        result.setdefault(chapter_id, {})[config] = sorted(items, key=lambda item: item["score"])[:10]
    return result


def _token_audit(model: Any, rows: list[dict[str, Any]], *, threshold: int) -> dict[str, Any]:
    tokenizer = _find_tokenizer(model)
    if tokenizer is None:
        return {
            "available": False,
            "threshold": threshold,
            "truncated_count": 0,
            "truncated": [],
            "note": "No tokenizer found on COMET model object; scoring still used model defaults.",
        }
    truncated: list[dict[str, Any]] = []
    max_tokens = 0
    for row in rows:
        token_count = _pair_token_count(tokenizer, row["source_text"], row["output_text"])
        if token_count is None:
            return {
                "available": False,
                "threshold": threshold,
                "truncated_count": 0,
                "truncated": [],
                "note": "Tokenizer did not expose input_ids for source/MT pairs.",
            }
        max_tokens = max(max_tokens, token_count)
        if token_count > threshold:
            truncated.append(
                {
                    "chapter_id": row["chapter_id"],
                    "block_id": row["block_id"],
                    "config": row["config"],
                    "token_count": token_count,
                }
            )
    return {
        "available": True,
        "threshold": threshold,
        "max_pair_tokens": max_tokens,
        "truncated_count": len(truncated),
        "truncated": truncated,
    }


def _find_tokenizer(model: Any) -> Any | None:
    for candidate in [
        getattr(model, "tokenizer", None),
        getattr(getattr(model, "encoder", None), "tokenizer", None),
        getattr(getattr(model, "estimator", None), "tokenizer", None),
    ]:
        if candidate is not None:
            return candidate
    return None


def _pair_token_count(tokenizer: Any, source: str, mt: str) -> int | None:
    try:
        encoded = tokenizer(
            source,
            mt,
            truncation=False,
            add_special_tokens=True,
        )
    except TypeError:
        encoded = tokenizer.encode_plus(
            source,
            mt,
            truncation=False,
            add_special_tokens=True,
        )
    input_ids = encoded.get("input_ids") if isinstance(encoded, dict) else getattr(encoded, "input_ids", None)
    if input_ids is None:
        return None
    return len(input_ids)


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _preview(text: str, limit: int = 220) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
