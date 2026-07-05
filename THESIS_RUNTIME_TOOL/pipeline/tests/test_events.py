from __future__ import annotations

import json

from pipeline.lib.events import EventEmitter


def test_event_emitter_flushes_v1_envelope_and_severity(tmp_path):
    path = tmp_path / "run_events" / "run_demo.jsonl"
    emitter = EventEmitter(path, run_id="run_demo", attempt_id=2, flush_every=20)

    emitter.emit(
        "stage_start",
        stage="translator",
        script="run_translate",
        agent="Translator",
        payload={"progress": {"done": 0, "total": 10, "total_known": True}},
    )
    emitter.emit(
        "warning",
        stage="translator",
        script="run_translate",
        agent="Translator",
        payload={"message": "fixture warning"},
    )
    emitter.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in rows] == [1, 2]
    assert rows[0]["v"] == 1
    assert rows[0]["schema"] == "one_button_event_v1"
    assert rows[0]["run_id"] == "run_demo"
    assert rows[0]["attempt_id"] == 2
    assert rows[0]["stage"] == "translator"
    assert rows[0]["event_type"] == "stage_start"
    assert rows[1]["severity"] == "warning"
    assert rows[1]["event_id"] != rows[0]["event_id"]


def test_event_emitter_is_best_effort(tmp_path):
    emitter = EventEmitter(tmp_path / "missing" / "events.jsonl", run_id="run_demo")
    emitter.path = tmp_path / "\0bad"
    emitter.emit("error", stage="report", script="report", agent="Reporter", payload={})
    emitter.close()

