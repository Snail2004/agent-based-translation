import json

from pipeline.lib.event_reader import read_jsonl_events


def test_read_jsonl_events_preserves_partial_line(tmp_path):
    path = tmp_path / "events.jsonl"
    first = json.dumps({"event": "stage_start", "seq": 1})
    second = json.dumps({"event": "stage_done", "seq": 2})
    path.write_bytes((first + "\n" + second[:10]).encode("utf-8"))

    result = read_jsonl_events(path, offset=0)

    assert [event["event"] for event in result["events"]] == ["stage_start"]
    assert result["partial_line"] is True
    assert result["offset"] == len((first + "\n").encode("utf-8"))

    with path.open("ab") as fh:
        fh.write((second[10:] + "\n").encode("utf-8"))

    result2 = read_jsonl_events(path, offset=result["offset"])

    assert [event["event"] for event in result2["events"]] == ["stage_done"]
    assert result2["partial_line"] is False


def test_read_jsonl_events_truncates_on_line_boundary(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = [json.dumps({"event": "item", "seq": idx, "payload": "x" * 120}) for idx in range(8)]
    path.write_bytes(("\n".join(rows) + "\n").encode("utf-8"))

    result = read_jsonl_events(path, offset=0, max_bytes=250)

    assert result["truncated"] is True
    assert result["events"]
    assert result["offset"] <= 250
    assert path.read_bytes()[result["offset"] - 1:result["offset"]] == b"\n"
