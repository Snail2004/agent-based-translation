from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline.lib.events import EventEmitter


STAGES = ["builder", "auditor", "translator", "cascade", "sf_qe", "sf_bt", "pj", "report"]
AGENTS = {
    "builder": "Builder",
    "auditor": "Auditor",
    "translator": "Translator",
    "cascade": "Localizer",
    "sf_qe": "Evaluator",
    "sf_bt": "Evaluator",
    "pj": "Evaluator",
    "report": "Reporter",
}
EVENT_TYPES = [
    "run_start",
    "health_check",
    "stage_start",
    "llm_call",
    "tool_call",
    "block_done",
    "artifact_created",
    "retry",
    "gate_pause",
    "cost_snapshot",
    "checkpoint",
    "warning",
    "error",
    "stage_skipped",
    "stage_done",
    "heartbeat",
    "run_resumed",
    "run_cancelled",
    "run_failed",
    "run_done",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one-button UI event fixtures.")
    parser.add_argument("--source", default="data/reports/exp_s0s1_builderv2_v1/run_preliminaries_events.jsonl")
    parser.add_argument("--out-dir", default="data/reports/one_button_ui1")
    parser.add_argument("--run-id", default="replay_one_button_ui1")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    converted = out_dir / "run_preliminaries_events_v1.jsonl"
    synthetic = out_dir / "synthetic_events_v1.jsonl"

    convert_legacy(Path(args.source), converted, run_id=f"{args.run_id}_converted")
    write_synthetic(synthetic, run_id=f"{args.run_id}_synthetic")
    print(json.dumps({
        "converted": str(converted),
        "synthetic": str(synthetic),
    }, ensure_ascii=False, indent=2))
    return 0


def convert_legacy(source: Path, out: Path, *, run_id: str) -> None:
    if not source.exists():
        write_synthetic(out, run_id=run_id, count=120)
        return
    with EventEmitter(out, run_id=run_id) as emitter:
        emitter.emit(
            "run_start",
            stage="translator",
            script="run_translate",
            agent="Translator",
            payload={"message": "converted legacy run_event_v1 fixture"},
        )
        with source.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    legacy = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_type = _legacy_event_type(str(legacy.get("event") or "tool_call"))
                payload = _compact_payload(legacy)
                emitter.emit(
                    event_type,
                    stage="translator",
                    script="run_translate",
                    agent="Translator",
                    severity=_severity_for(event_type),
                    payload=payload,
                )
        emitter.emit(
            "run_done",
            stage="translator",
            script="run_translate",
            agent="Translator",
            payload={"message": "converted fixture complete"},
        )


def write_synthetic(out: Path, *, run_id: str, count: int = 500) -> None:
    with EventEmitter(out, run_id=run_id) as emitter:
        for i in range(count):
            stage = STAGES[(i // 60) % len(STAGES)]
            event_type = EVENT_TYPES[i % len(EVENT_TYPES)]
            payload: dict[str, Any] = {
                "message": f"synthetic {event_type} {i}",
                "unit": "block" if event_type == "block_done" else "call",
                "scope": {"chapter_id": "d2l_multilayer_perceptrons", "config": "S1" if i % 2 else "S0"},
                "progress": {"done": min(i + 1, count), "total": count, "total_known": True},
                "cost_delta_usd": round(0.0001 * (i % 5), 6),
                "cache_hit": i % 3 == 0,
            }
            if event_type == "artifact_created":
                payload.update({
                    "artifact_path": f"data/reports/one_button_ui1/artifact_{i}.json",
                    "artifact_type": "json",
                    "artifact_sha": f"sha{i:04d}",
                })
            if event_type == "gate_pause":
                payload.update({"budget_cap_usd": 3.0, "message": "waiting for user approval"})
            if event_type in {"warning", "error", "run_failed"}:
                payload.update({"error_code": f"synthetic_{event_type}"})
            emitter.emit(
                event_type,
                stage=stage,
                script=f"script_{stage}",
                agent=AGENTS[stage],
                severity=_severity_for(event_type),
                payload=payload,
            )


def _legacy_event_type(value: str) -> str:
    if value in {"run_committed"}:
        return "stage_done"
    if value in {"window_started", "prompt_built", "request_sent", "response_received"}:
        return "llm_call" if value in {"request_sent", "response_received"} else "tool_call"
    if value in {"window_skipped"}:
        return "stage_skipped"
    if value in {"window_preview_available", "persist_buffered", "json_parsed"}:
        return "block_done"
    if value in {"run_failed"}:
        return "run_failed"
    if "hygiene" in value:
        return "warning"
    return "tool_call"


def _severity_for(event_type: str) -> str:
    if event_type in {"error", "run_failed"}:
        return "error"
    if event_type in {"warning", "gate_pause"}:
        return "warning"
    return "info"


def _compact_payload(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "config",
        "window_id",
        "block_ids",
        "prompt_version",
        "prompt_hash",
        "prompt_tokens_est",
        "cache_key",
        "from_cache",
        "model",
        "cost_usd",
        "latency_ms",
        "errors",
        "status",
        "reason",
    }
    payload = {key: row[key] for key in allowed if key in row}
    payload["message"] = f"converted legacy event {row.get('event')}"
    return payload


if __name__ == "__main__":
    raise SystemExit(main())

