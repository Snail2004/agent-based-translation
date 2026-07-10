from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.literary.builder_pilot import run_m1, run_m2
from pipeline.tests.test_literary_checkpoint import DESIGN_DOC, FakeLiteraryClient, _config, _document


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_resume_keeps_its_own_checkpoint_namespace(tmp_path: Path) -> None:
    document = _document()
    config = _config()
    continuous_dir = tmp_path / "continuous"
    resumed_dir = tmp_path / "resumed"
    for out_dir in [continuous_dir, resumed_dir]:
        run_m1(
            document,
            ["bk_ch01", "bk_ch02"],
            design_doc=DESIGN_DOC,
            config=config,
            client=FakeLiteraryClient(),
            out_dir=out_dir,
            confirm_usd=10.0,
        )

    continuous = run_m2(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=continuous_dir,
        m1_dir=continuous_dir,
        confirm_usd=10.0,
    )
    run_m2(
        document,
        ["bk_ch01"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=resumed_dir,
        m1_dir=resumed_dir,
        confirm_usd=10.0,
    )
    resumed = run_m2(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=config,
        client=FakeLiteraryClient(),
        out_dir=resumed_dir,
        m1_dir=resumed_dir,
        confirm_usd=10.0,
        resume=True,
    )

    assert continuous["chapter_summaries"] == resumed["chapter_summaries"]
    assert resumed["resume"]["resumed_from_checkpoint"] == ["bk_ch01"]
    assert resumed["resume"]["ran"] == ["bk_ch02"]
    assert (resumed_dir / "checkpoints" / "m1" / "bk_ch02.json").is_file()
    assert (resumed_dir / "checkpoints" / "m2" / "bk_ch02.json").is_file()
