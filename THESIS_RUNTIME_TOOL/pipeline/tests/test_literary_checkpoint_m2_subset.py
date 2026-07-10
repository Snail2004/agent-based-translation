from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.literary.builder_pilot import estimate_m2, run_m1, run_m2
from pipeline.literary.checkpoint import build_checkpoint, canonical_hash, read_checkpoint, write_checkpoint_atomic
from pipeline.tests.test_literary_checkpoint import DESIGN_DOC, FakeLiteraryClient, _config


def _document_four() -> dict:
    return {
        "chapters": [
            {
                "chapter_id": f"bk_ch{index:02d}",
                "blocks": [
                    {
                        "block_id": f"bk_ch{index:02d}_b001",
                        "order_index": index,
                        "clean_text": f"Chapter {index} narrative text.",
                    }
                ],
            }
            for index in range(1, 5)
        ]
    }


def _chapter_ids() -> list[str]:
    return [f"bk_ch{index:02d}" for index in range(1, 5)]


def _prepare_full_m1(tmp_path: Path) -> tuple[dict, Path]:
    document = _document_four()
    m1_dir = tmp_path / "m1"
    run_m1(
        document,
        _chapter_ids(),
        design_doc=DESIGN_DOC,
        config=_config(),
        client=FakeLiteraryClient(),
        out_dir=m1_dir,
        confirm_usd=10.0,
    )
    return document, m1_dir


def _neighbor_section(prompt: str) -> str:
    start = "NEIGHBOR_SUMMARIES_GIST_ONLY\n"
    end = "\n\nCHAPTER_BRIEF\n"
    return prompt.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_suffix_selection_validates_absolute_m1_chain(tmp_path: Path) -> None:
    document, m1_dir = _prepare_full_m1(tmp_path)
    context_dir = tmp_path / "context"
    run_m2(
        document,
        ["bk_ch01", "bk_ch02"],
        design_doc=DESIGN_DOC,
        config=_config(),
        client=FakeLiteraryClient(),
        out_dir=context_dir,
        m1_dir=m1_dir,
        confirm_usd=10.0,
    )

    estimate = estimate_m2(
        document,
        ["bk_ch03", "bk_ch04"],
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
        digest_context=context_dir,
    )

    assert estimate["calls"] == 2
    assert estimate["chapters_selected"] == ["bk_ch03", "bk_ch04"]
    assert [row["chapter_id"] for row in estimate["neighbor_source"]["bk_ch03"]] == [
        "bk_ch01",
        "bk_ch02",
    ]


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_estimate_marks_in_run_neighbors_as_sizing_placeholders(tmp_path: Path) -> None:
    document, m1_dir = _prepare_full_m1(tmp_path)

    estimate = estimate_m2(
        document,
        _chapter_ids(),
        design_doc=DESIGN_DOC,
        config=_config(),
        m1_dir=m1_dir,
    )

    assert estimate["zero_api"] is True
    assert estimate["neighbor_source"]["bk_ch01"] == []
    for chapter_id in _chapter_ids()[1:]:
        assert all(
            row["source"] == "in_run_estimate_placeholder"
            for row in estimate["neighbor_source"][chapter_id]
        )
    assert all(
        call["neighbor_summary_count"] == min(2, index)
        for index, call in enumerate(estimate["call_estimates"])
    )


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_suffix_rejects_broken_absolute_ancestor_chain(tmp_path: Path) -> None:
    document, m1_dir = _prepare_full_m1(tmp_path)
    checkpoint_path = m1_dir / "checkpoints" / "m1" / "bk_ch02.json"
    checkpoint = read_checkpoint(checkpoint_path)
    checkpoint["parent_checkpoint_hash"] = "broken-ancestor-hash"
    write_checkpoint_atomic(checkpoint_path, build_checkpoint(checkpoint))

    with pytest.raises(ValueError, match="Invalid M1 as-of checkpoint bk_ch02") as exc_info:
        estimate_m2(
            document,
            ["bk_ch04"],
            design_doc=DESIGN_DOC,
            config=_config(),
            m1_dir=m1_dir,
        )
    assert "parent_checkpoint_hash" in str(exc_info.value)


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_context_neighbors_match_in_loop_canonically(tmp_path: Path) -> None:
    document, m1_dir = _prepare_full_m1(tmp_path)
    in_loop_dir = tmp_path / "in_loop"
    in_loop_client = FakeLiteraryClient()
    run_m2(
        document,
        _chapter_ids(),
        design_doc=DESIGN_DOC,
        config=_config(),
        client=in_loop_client,
        out_dir=in_loop_dir,
        m1_dir=m1_dir,
        confirm_usd=10.0,
    )

    subset_dir = tmp_path / "subset"
    subset_client = FakeLiteraryClient()
    subset = run_m2(
        document,
        ["bk_ch03", "bk_ch04"],
        design_doc=DESIGN_DOC,
        config=_config(),
        client=subset_client,
        out_dir=subset_dir,
        m1_dir=m1_dir,
        digest_context=in_loop_dir,
        confirm_usd=10.0,
    )

    for chapter_id in ["bk_ch03", "bk_ch04"]:
        assert canonical_hash(_neighbor_section(in_loop_client.digest_prompts[chapter_id])) == canonical_hash(
            _neighbor_section(subset_client.digest_prompts[chapter_id])
        )
    assert subset["neighbor_source"]["bk_ch03"][0]["source"].startswith(
        "digest_context_artifact:"
    )
    assert subset["neighbor_source"]["bk_ch04"][1]["source"] == "in_run"


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_subset_requires_missing_prior_digest_context(tmp_path: Path) -> None:
    document, m1_dir = _prepare_full_m1(tmp_path)

    with pytest.raises(FileNotFoundError, match="Missing required digest neighbor bk_ch01 for bk_ch03"):
        estimate_m2(
            document,
            ["bk_ch03"],
            design_doc=DESIGN_DOC,
            config=_config(),
            m1_dir=m1_dir,
        )
