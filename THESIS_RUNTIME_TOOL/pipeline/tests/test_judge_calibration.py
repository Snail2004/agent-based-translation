from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.eval.judge_calibration import (
    calibration_summary,
    load_human_ratings,
    pairwise_agreement,
    spearman,
)


def test_calibration_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2], [2, 2, 3]) == pytest.approx(1.0)


def test_pairwise_agreement():
    assert pairwise_agreement(["a", "b", "tie"], ["a", "tie", "tie"]) == pytest.approx(2 / 3)


def test_load_human_ratings_and_uncalibrated(tmp_path: Path):
    path = tmp_path / "human.csv"
    path.write_text(
        "scope_id,comparison,human_winner,human_score\n"
        "b1,S0:S1,b,4\n"
        "b2,S0:S1,tie,3\n",
        encoding="utf-8",
    )

    rows = load_human_ratings(path)
    assert len(rows) == 2
    assert rows[0].scope_id == "b1"
    assert rows[0].human_winner == "b"
    assert rows[0].human_score == 4

    missing = load_human_ratings(tmp_path / "missing.csv")
    assert missing == []
    summary = calibration_summary()
    assert summary["calibrated"] is False
    assert summary["spearman_rho"] is None
