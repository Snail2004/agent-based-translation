from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.agents.judge_client import JudgeClient, JudgeConfigError
from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.judge import gemba, mattr, pairwise
from pipeline.memory.store_init import init_db
from pipeline.scripts.run_judge import run_judge


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("no fake response left")
        response = self.responses.pop(0)
        if callable(response):
            return response(kwargs)
        return response


def _config(model: str = "gemini-2.5-flash") -> LLMConfig:
    return LLMConfig(
        model=model,
        temperature=0.0,
        seed=20260612,
        reasoning_effort="none",
        verbosity=None,
        max_output_tokens=128,
        daily_token_cap=1_000_000,
        pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
    )


def _response(obj: dict) -> dict:
    return {
        "text": json.dumps(obj, ensure_ascii=False),
        "usage_metadata": {"prompt_token_count": 10, "candidates_token_count": 5},
    }


def test_judge_cache_hit(tmp_path: Path):
    transport = FakeTransport([_response({"winner": "tie", "rationale": "same"})])
    client = JudgeClient(_config(), tmp_path / "judge.sqlite3", transport=transport)
    messages = [{"role": "user", "content": "Judge this."}]

    first = client.call(messages, response_format={"type": "json_object"}, tag="x")
    second = client.call(messages, response_format={"type": "json_object"}, tag="x")

    assert len(transport.calls) == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.parsed_json == first.parsed_json


def test_cross_provider_guard(tmp_path: Path):
    with pytest.raises(JudgeConfigError):
        JudgeClient(
            _config(model="gpt-5.4-mini"),
            tmp_path / "judge.sqlite3",
            transport=FakeTransport([]),
        )


def test_pairwise_blind_no_system_label(tmp_path: Path):
    transport = FakeTransport(
        [
            _response({"winner": "tie", "rationale": "both acceptable"}),
            _response({"winner": "tie", "rationale": "both acceptable"}),
        ]
    )
    client = JudgeClient(_config(), tmp_path / "judge.sqlite3", transport=transport)

    result = pairwise(
        client,
        source="A sailor sang.",
        vi_a="Một thủy thủ hát.",
        vi_b="Một người đi biển hát.",
        scope_id="b1",
    )

    assert result.winner == "tie"
    content = "\n".join(
        message["content"]
        for call in transport.calls
        for message in call["messages"]
    )
    assert "S0" not in content
    assert "S1" not in content
    assert "oracle" not in content.casefold()
    assert "Bản 1" in content
    assert "Bản 2" in content


def test_pairwise_swap_detects_position_bias(tmp_path: Path):
    transport = FakeTransport(
        [
            _response({"winner": "1", "rationale": "first is better"}),
            _response({"winner": "1", "rationale": "first is better"}),
        ]
    )
    client = JudgeClient(_config(), tmp_path / "judge.sqlite3", transport=transport)

    result = pairwise(
        client,
        source="Source",
        vi_a="A translation",
        vi_b="B translation",
        scope_id="b1",
    )

    assert result.winner == "tie"
    assert result.confidence == "low"
    assert [item.mapped_winner for item in result.verdicts] == ["a", "b"]


def test_pairwise_consistent_win(tmp_path: Path):
    def choose_good(kwargs):
        user = kwargs["messages"][1]["content"]
        winner = "1" if "Bản 1\nGOOD" in user else "2"
        return _response({"winner": winner, "rationale": "GOOD preserves meaning"})

    transport = FakeTransport([choose_good, choose_good])
    client = JudgeClient(_config(), tmp_path / "judge.sqlite3", transport=transport)

    result = pairwise(
        client,
        source="Source",
        vi_a="GOOD translation",
        vi_b="Weak translation",
        scope_id="b1",
    )

    assert result.winner == "a"
    assert result.confidence == "high"


def test_gemba_4_criteria_parsed(tmp_path: Path):
    transport = FakeTransport(
        [
            _response(
                {
                    "adequacy": 91,
                    "fluency": 88,
                    "style_voice": 77,
                    "fidelity_no_adddrop": 93,
                    "rationale": "faithful",
                }
            )
        ]
    )
    client = JudgeClient(_config(), tmp_path / "judge.sqlite3", transport=transport)

    result = gemba(client, source="Source", vi="Bản dịch", scope_id="b1")

    assert result.adequacy == 91
    assert result.fluency == 88
    assert result.style_voice == 77
    assert result.fidelity_no_adddrop == 93
    assert result.rationale == "faithful"


def test_mattr_handcomputed():
    # Sliding windows of 3 over "a a b b c c":
    # a a b, a b b, b b c, b c c all have 2 unique / 3.
    assert mattr("a a b b c c", window=3) == pytest.approx(2 / 3)


def test_persist_evaluation_runs(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO documents (doc_id, job_id, source_lang, target_lang) VALUES ('doc', 'job', 'en', 'vi')"
    )
    conn.executemany(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, chapter_id, text)
        VALUES (?, 'doc', ?, 'doc_ch02', ?)
        """,
        [("b1", 1, "The captain sang."), ("b2", 2, "Jim waited.")],
    )
    for config in ["S0", "S1"]:
        for block_id in ["b1", "b2"]:
            conn.execute(
                """
                INSERT INTO translation_runs (
                  run_id, experiment_id, doc_id, block_id, config, output_text, model
                )
                VALUES (?, 'exp', 'doc', ?, ?, ?, 'gpt-5.4-mini')
                """,
                (
                    f"tr_{config}_{block_id}",
                    block_id,
                    config,
                    f"{config} translation {block_id}",
                ),
            )
    conn.commit()

    responses = []
    # Two block pairwise calls * swap + one chapter pairwise * swap.
    responses.extend([_response({"winner": "tie", "rationale": "same"}) for _ in range(6)])
    # Two blocks * two configs GEMBA.
    responses.extend(
        [
            _response(
                {
                    "adequacy": 80,
                    "fluency": 81,
                    "style_voice": 82,
                    "fidelity_no_adddrop": 83,
                    "rationale": "ok",
                }
            )
            for _ in range(4)
        ]
    )
    client = JudgeClient(
        _config(),
        tmp_path / "judge.sqlite3",
        transport=FakeTransport(responses),
    )

    report = run_judge(
        connection=conn,
        client=client,
        rows=[
            {
                "block_id": "b1",
                "chapter_id": "ch02",
                "source_text": "The captain sang.",
                "left_run_id": "tr_S0_b1",
                "left_text": "S0 translation b1",
                "right_run_id": "tr_S1_b1",
                "right_text": "S1 translation b1",
            },
            {
                "block_id": "b2",
                "chapter_id": "ch02",
                "source_text": "Jim waited.",
                "left_run_id": "tr_S0_b2",
                "left_text": "S0 translation b2",
                "right_run_id": "tr_S1_b2",
                "right_text": "S1 translation b2",
            },
        ],
        left_config="S0",
        right_config="S1",
    )

    rows = conn.execute(
        """
        SELECT scope, metric_name, judge_model, ablation_label
        FROM evaluation_runs
        ORDER BY metric_name
        """
    ).fetchall()
    metric_names = {row["metric_name"] for row in rows}
    assert "pairwise_winner" in metric_names
    assert "gemba_adequacy" in metric_names
    assert "mattr" in metric_names
    assert all(row["judge_model"] == "gemini-2.5-flash" for row in rows)
    assert all(row["ablation_label"] == "S0_vs_S1" for row in rows)
    assert report["calibrated"] is False
    assert report["summary"]["pairwise"]["n"] == 2
