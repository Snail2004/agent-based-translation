from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Any

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.eval.judge import METRIC_VERSION, gemba, mattr, pairwise
from pipeline.eval.judge_calibration import (
    calibration_summary,
    load_human_ratings,
)
from pipeline.memory.store_init import migrate_db


DEFAULT_CONFIG = Path(__file__).parents[1] / "configs" / "judge_gemini.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Gemini judge pairwise/GEMBA/MATTR for two translation configs."
    )
    parser.add_argument("--db", required=True, help="Runtime memory SQLite DB.")
    parser.add_argument("--experiment", required=True, help="Experiment ID.")
    parser.add_argument("--compare", required=True, help="Config pair, e.g. S0:S1.")
    parser.add_argument("--chapters", nargs="+", required=True, help="Chapter ids/labels.")
    parser.add_argument("--out", required=True, help="Report JSON path.")
    parser.add_argument("--human", help="Optional human ratings CSV.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Judge YAML config.")
    parser.add_argument("--cache", help="Judge replay cache path.")
    args = parser.parse_args()

    left_config, right_config = _parse_compare(args.compare)
    db_path = Path(args.db)
    connection = migrate_db(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = _load_rows(
            connection,
            experiment_id=args.experiment,
            left_config=left_config,
            right_config=right_config,
            chapters=args.chapters,
        )
        judge_config = load_llm_config(args.config)
        cache_path = Path(args.cache) if args.cache else db_path.with_name("judge_cache.sqlite3")
        client = JudgeClient(judge_config, cache_path)
        report = run_judge(
            connection=connection,
            client=client,
            rows=rows,
            left_config=left_config,
            right_config=right_config,
            human_path=args.human,
        )
        report["experiment_id"] = args.experiment
        report["compare"] = f"{left_config}:{right_config}"
        report["chapters"] = args.chapters
        report["judge_model"] = judge_config.model
        report["cache_path"] = str(cache_path)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        connection.close()

    _print_summary(report)
    print(f"\nReport written: {args.out}")
    return 0


def run_judge(
    *,
    connection: sqlite3.Connection,
    client: JudgeClient,
    rows: list[dict[str, Any]],
    left_config: str,
    right_config: str,
    human_path: str | None = None,
) -> dict[str, Any]:
    ablation_label = f"{left_config}_vs_{right_config}"
    pairwise_items = []
    for row in rows:
        result = pairwise(
            client,
            source=row["source_text"],
            vi_a=row["left_text"],
            vi_b=row["right_text"],
            scope_id=row["block_id"],
        )
        pairwise_items.append({"scope": "block", "scope_id": row["block_id"], **result.to_dict()})
        _persist_eval(
            connection,
            run_id=None,
            scope="block",
            scope_id=row["block_id"],
            metric_name="pairwise_winner",
            metric_value=_winner_value(result.winner),
            judge_model=client.config.model,
            judge_rationale=result.rationale,
            ablation_label=ablation_label,
        )

    chapter_items = _chapter_pairwise(
        connection=connection,
        client=client,
        rows=rows,
        ablation_label=ablation_label,
    )
    pairwise_items.extend(chapter_items)

    gemba_items = []
    for row in rows:
        for side, config, text, run_id in [
            ("left", left_config, row["left_text"], row["left_run_id"]),
            ("right", right_config, row["right_text"], row["right_run_id"]),
        ]:
            result = gemba(
                client,
                source=row["source_text"],
                vi=text,
                scope_id=row["block_id"],
                label=config,
            )
            item = {
                "scope": "block",
                "scope_id": row["block_id"],
                "side": side,
                "config": config,
                **result.to_dict(),
            }
            gemba_items.append(item)
            for metric, value in [
                ("gemba_adequacy", result.adequacy),
                ("gemba_fluency", result.fluency),
                ("gemba_style_voice", result.style_voice),
                ("gemba_fidelity", result.fidelity_no_adddrop),
            ]:
                _persist_eval(
                    connection,
                    run_id=run_id,
                    scope="block",
                    scope_id=row["block_id"],
                    metric_name=metric,
                    metric_value=value,
                    judge_model=client.config.model,
                    judge_rationale=result.rationale,
                    ablation_label=ablation_label,
                )

    mattr_items = _mattr_by_chapter(
        connection=connection,
        rows=rows,
        left_config=left_config,
        right_config=right_config,
        judge_model=client.config.model,
        ablation_label=ablation_label,
    )
    connection.commit()

    human = load_human_ratings(human_path) if human_path else []
    calibration = _build_calibration(pairwise_items, gemba_items, human)
    summary = _summary(pairwise_items, gemba_items, mattr_items, left_config, right_config)
    return {
        "metric_version": METRIC_VERSION,
        "calibrated": bool(calibration.get("calibrated")),
        "calibration": calibration,
        "warning": (
            "Judge numbers are diagnostic until calibrated against human ratings."
            if not calibration.get("calibrated")
            else ""
        ),
        "summary": summary,
        "pairwise": pairwise_items,
        "gemba": gemba_items,
        "mattr": mattr_items,
    }


def _chapter_pairwise(
    *,
    connection: sqlite3.Connection,
    client: JudgeClient,
    rows: list[dict[str, Any]],
    ablation_label: str,
) -> list[dict[str, Any]]:
    items = []
    for chapter in sorted({row["chapter_id"] for row in rows}):
        chapter_rows = [row for row in rows if row["chapter_id"] == chapter]
        source = "\n\n".join(row["source_text"] for row in chapter_rows)
        left = "\n\n".join(row["left_text"] for row in chapter_rows)
        right = "\n\n".join(row["right_text"] for row in chapter_rows)
        result = pairwise(client, source=source, vi_a=left, vi_b=right, scope_id=chapter)
        items.append({"scope": "chapter", "scope_id": chapter, **result.to_dict()})
        _persist_eval(
            connection,
            run_id=None,
            scope="chapter",
            scope_id=chapter,
            metric_name="pairwise_winner",
            metric_value=_winner_value(result.winner),
            judge_model=client.config.model,
            judge_rationale=result.rationale,
            ablation_label=ablation_label,
        )
    return items


def _mattr_by_chapter(
    *,
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    left_config: str,
    right_config: str,
    judge_model: str,
    ablation_label: str,
) -> list[dict[str, Any]]:
    items = []
    for chapter in sorted({row["chapter_id"] for row in rows}):
        chapter_rows = [row for row in rows if row["chapter_id"] == chapter]
        for side, config, key in [
            ("left", left_config, "left_text"),
            ("right", right_config, "right_text"),
        ]:
            text = "\n\n".join(row[key] for row in chapter_rows)
            value = mattr(text)
            items.append(
                {
                    "scope": "chapter",
                    "scope_id": chapter,
                    "side": side,
                    "config": config,
                    "value": value,
                }
            )
            _persist_eval(
                connection,
                run_id=None,
                scope="chapter",
                scope_id=chapter,
                metric_name="mattr",
                metric_value=value,
                judge_model=judge_model,
                judge_rationale="deterministic MATTR",
                ablation_label=ablation_label,
            )
    return items


def _load_rows(
    connection: sqlite3.Connection,
    *,
    experiment_id: str,
    left_config: str,
    right_config: str,
    chapters: list[str],
) -> list[dict[str, Any]]:
    normalized = {_chapter_label(item) for item in chapters}
    rows = connection.execute(
        """
        SELECT b.block_id, b.chapter_id, b.text AS source_text, b.order_index,
               l.run_id AS left_run_id, l.output_text AS left_text,
               r.run_id AS right_run_id, r.output_text AS right_text
        FROM blocks b
        JOIN translation_runs l
          ON l.block_id = b.block_id
         AND l.experiment_id = ?
         AND l.config = ?
         AND l.stage = 'draft'
        JOIN translation_runs r
          ON r.block_id = b.block_id
         AND r.experiment_id = ?
         AND r.config = ?
         AND r.stage = 'draft'
        ORDER BY b.order_index
        """,
        (experiment_id, left_config, experiment_id, right_config),
    ).fetchall()
    result = []
    for row in rows:
        chapter = _chapter_label(str(row["chapter_id"] or row["block_id"]))
        if chapter not in normalized:
            continue
        result.append(
            {
                "block_id": str(row["block_id"]),
                "chapter_id": chapter,
                "source_text": str(row["source_text"] or ""),
                "left_run_id": str(row["left_run_id"]),
                "left_text": str(row["left_text"] or ""),
                "right_run_id": str(row["right_run_id"]),
                "right_text": str(row["right_text"] or ""),
            }
        )
    if not result:
        raise RuntimeError("No paired translation rows found for judge comparison")
    return result


def _persist_eval(
    connection: sqlite3.Connection,
    *,
    run_id: str | None,
    scope: str,
    scope_id: str,
    metric_name: str,
    metric_value: float,
    judge_model: str,
    judge_rationale: str,
    ablation_label: str,
) -> None:
    run_part = run_id or "comparison"
    eval_id = f"ev_{ablation_label}_{scope}_{scope_id}_{run_part}_{metric_name}"
    connection.execute(
        """
        INSERT OR REPLACE INTO evaluation_runs (
          eval_id, run_id, scope, scope_id, metric_name, metric_value,
          metric_version, judge_model, judge_rationale, ablation_label
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eval_id,
            run_id,
            scope,
            scope_id,
            metric_name,
            metric_value,
            METRIC_VERSION,
            judge_model,
            judge_rationale,
            ablation_label,
        ),
    )


def _build_calibration(
    pairwise_items: list[dict[str, Any]],
    gemba_items: list[dict[str, Any]],
    human: list[Any],
) -> dict[str, Any]:
    if not human:
        return calibration_summary()
    pair_by_scope = {
        str(item["scope_id"]): item["winner"]
        for item in pairwise_items
        if item["scope"] == "block"
    }
    gemba_by_scope = {}
    for item in gemba_items:
        if item["side"] != "right":
            continue
        gemba_by_scope[str(item["scope_id"])] = mean(
            [
                float(item["adequacy"]),
                float(item["fluency"]),
                float(item["style_voice"]),
                float(item["fidelity_no_adddrop"]),
            ]
        )

    judge_scores = []
    human_scores = []
    judge_verdicts = []
    human_verdicts = []
    for row in human:
        if row.human_score is not None and row.scope_id in gemba_by_scope:
            judge_scores.append(gemba_by_scope[row.scope_id])
            human_scores.append(row.human_score)
        if row.human_winner is not None and row.scope_id in pair_by_scope:
            judge_verdicts.append(pair_by_scope[row.scope_id])
            human_verdicts.append(row.human_winner)

    return calibration_summary(
        judge_scores=judge_scores,
        human_scores=human_scores,
        judge_verdicts=judge_verdicts,
        human_verdicts=human_verdicts,
    )


def _summary(
    pairwise_items: list[dict[str, Any]],
    gemba_items: list[dict[str, Any]],
    mattr_items: list[dict[str, Any]],
    left_config: str,
    right_config: str,
) -> dict[str, Any]:
    block_items = [item for item in pairwise_items if item["scope"] == "block"]
    total = len(block_items)
    wins_left = sum(1 for item in block_items if item["winner"] == "a")
    wins_right = sum(1 for item in block_items if item["winner"] == "b")
    ties = sum(1 for item in block_items if item["winner"] == "tie")
    return {
        "pairwise": {
            left_config: _ratio(wins_left, total),
            right_config: _ratio(wins_right, total),
            "tie": _ratio(ties, total),
            "n": total,
            "counts": {left_config: wins_left, right_config: wins_right, "tie": ties},
        },
        "gemba": _gemba_summary(gemba_items),
        "mattr": _mattr_summary(mattr_items),
    }


def _gemba_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_config: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_config.setdefault(str(item["config"]), []).append(item)
    result = {}
    for config, values in by_config.items():
        result[config] = {
            "adequacy": mean(float(item["adequacy"]) for item in values),
            "fluency": mean(float(item["fluency"]) for item in values),
            "style_voice": mean(float(item["style_voice"]) for item in values),
            "fidelity_no_adddrop": mean(float(item["fidelity_no_adddrop"]) for item in values),
            "n": len(values),
        }
    return result


def _mattr_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_config: dict[str, list[float]] = {}
    for item in items:
        by_config.setdefault(str(item["config"]), []).append(float(item["value"]))
    return {
        config: {"mean": mean(values), "n": len(values)}
        for config, values in by_config.items()
    }


def _parse_compare(value: str) -> tuple[str, str]:
    parts = [part.strip().upper() for part in value.split(":", 1)]
    if len(parts) != 2 or not all(parts):
        raise ValueError("--compare must look like S0:S1")
    return parts[0], parts[1]


def _chapter_label(value: str) -> str:
    import re

    match = re.search(r"(?:^|_)ch(\d+)", value, re.IGNORECASE)
    if match:
        return f"ch{int(match.group(1)):02d}"
    return value


def _winner_value(winner: str) -> float:
    if winner == "a":
        return -1.0
    if winner == "b":
        return 1.0
    return 0.0


def _ratio(count: int, total: int) -> float:
    return count / total if total else 0.0


def _print_summary(report: dict[str, Any]) -> None:
    pairwise_summary = report["summary"]["pairwise"]
    print("\n=== Pairwise ===")
    print(f"  n:    {pairwise_summary['n']}")
    for key, count in pairwise_summary["counts"].items():
        print(f"  {key}: {count} ({pairwise_summary[key]:.4f})")
    print(f"  calibrated: {report['calibrated']}")

    print("\n=== GEMBA ===")
    for config, values in sorted(report["summary"]["gemba"].items()):
        print(
            f"  {config}: adequacy={values['adequacy']:.1f} "
            f"fluency={values['fluency']:.1f} "
            f"style={values['style_voice']:.1f} "
            f"fidelity={values['fidelity_no_adddrop']:.1f} n={values['n']}"
        )

    print("\n=== MATTR ===")
    for config, values in sorted(report["summary"]["mattr"].items()):
        print(f"  {config}: mean={values['mean']:.4f} n={values['n']}")


if __name__ == "__main__":
    raise SystemExit(main())
