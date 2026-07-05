from __future__ import annotations

import argparse
import json
import subprocess

from pipeline.scripts import preflight_check


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": [
                {"id": "google/gemma-4-12b"},
                {"id": "gpustack/bge-m3-GGUF:Q8_0"},
            ]
        }


def test_preflight_check_passes_without_logging_key_values(tmp_path, monkeypatch) -> None:
    (tmp_path / "OPENAI-KEY-2.txt").write_text("dummy-openai-secret\n", encoding="utf-8")
    (tmp_path / "GEMINI-KEY.txt").write_text("gemini-secret\n", encoding="utf-8")
    event_log = tmp_path / "events" / "health.jsonl"

    monkeypatch.setattr(preflight_check.requests, "get", lambda *args, **kwargs: _Response())

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({"python": "3.11.9", "executable": "python"}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(preflight_check.subprocess, "run", fake_run)
    args = argparse.Namespace(
        lmstudio_endpoint="http://127.0.0.1:1234",
        gemma_model="gemma-4-12b",
        bge_model="bge-m3",
        repo_root=str(tmp_path),
        python="python",
        event_log=str(event_log),
        run_id="run_health",
        attempt_id=1,
    )

    report = preflight_check.run_checks(args)

    assert report["status"] == "pass"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "dummy-openai-secret" not in serialized
    assert "gemini-secret" not in serialized
    assert [item["id"] for item in report["checks"]] == [
        "lmstudio_gemma",
        "lmstudio_bge",
        "openai_key",
        "gemini_key",
        "cometkiwi_import",
    ]
    events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 5
    assert {event["event"] for event in events} == {"health_check"}


def test_preflight_check_fails_when_model_missing(tmp_path, monkeypatch) -> None:
    class MissingResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"id": "some-other-model"}]}

    monkeypatch.setattr(preflight_check.requests, "get", lambda *args, **kwargs: MissingResponse())
    monkeypatch.setattr(
        preflight_check.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="no comet"),
    )
    args = argparse.Namespace(
        lmstudio_endpoint="http://127.0.0.1:1234",
        gemma_model="gemma-4-12b",
        bge_model="bge-m3",
        repo_root=str(tmp_path),
        python="python",
        event_log="",
        run_id="run_health",
        attempt_id=1,
    )

    report = preflight_check.run_checks(args)

    assert report["status"] == "fail"
    assert not report["checks"][0]["ok"]
    assert not report["checks"][-1]["ok"]
