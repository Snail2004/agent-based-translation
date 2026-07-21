from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.ingest.normalization_adapters import (
    AdapterUnavailableError,
    _walk_pandoc_blocks,
    run_app_current,
    run_docling,
    run_pandoc,
)
from pipeline.ingest.normalization_benchmark import (
    SourceSpec,
    benchmark_source,
    observation_manifest,
    pairwise_metrics,
)
from pipeline.ingest.normalization_docling_worker import _ascii_stage
from pipeline.ingest.normalization_ir import (
    AdapterResult,
    ObservedBlock,
    segment_units,
)
from pipeline.ingest.normalization_recommendation import recommend_benchmark_toolchain


def _result(adapter: str, texts: list[str], *, duration: float = 0.0) -> AdapterResult:
    blocks = tuple(
        ObservedBlock(ordinal=index, kind="paragraph", text=text)
        for index, text in enumerate(texts)
    )
    return AdapterResult(
        adapter=adapter,
        adapter_version="1",
        source_path=f"C:/{adapter}.txt",
        source_format="txt",
        blocks=blocks,
        units=segment_units(blocks),
        duration_seconds=duration,
    )


def test_segment_units_uses_repeated_heading_level() -> None:
    blocks = (
        ObservedBlock(0, "heading", "Book title", 1),
        ObservedBlock(1, "heading", "One", 2),
        ObservedBlock(2, "paragraph", "Alpha"),
        ObservedBlock(3, "heading", "Two", 2),
        ObservedBlock(4, "paragraph", "Beta"),
    )
    units = segment_units(blocks)
    assert [unit.unit_kind for unit in units] == ["front_matter", "chapter_candidate", "chapter_candidate"]
    assert [unit.title for unit in units] == ["Book title", "One", "Two"]
    assert units[1].boundary_level == 2


def test_segment_units_without_repeated_boundary_does_not_invent_chapter() -> None:
    blocks = (
        ObservedBlock(0, "heading", "A report", 1),
        ObservedBlock(1, "paragraph", "Only one continuous unit."),
    )
    units = segment_units(blocks)
    assert len(units) == 1
    assert units[0].unit_kind == "document_unit"
    assert units[0].title == "A report"


def test_stable_hash_excludes_runtime_and_absolute_path() -> None:
    first = _result("same", ["Alpha beta"], duration=1.0)
    second = AdapterResult(
        adapter=first.adapter,
        adapter_version=first.adapter_version,
        source_path="D:/different/location.txt",
        source_format=first.source_format,
        blocks=first.blocks,
        units=first.units,
        duration_seconds=99.0,
    )
    assert first.output_sha256() == second.output_sha256()


def test_pairwise_metrics_are_directional() -> None:
    left = _result("left", ["one two three four five six"])
    right = _result("right", ["one two three four five"])
    metrics = pairwise_metrics(left, right)
    assert metrics["token_coverage_left_by_right"] == pytest.approx(5 / 6, abs=1e-6)
    assert metrics["token_coverage_right_by_left"] == 1.0
    assert metrics["ordered_shingle_coverage_right_by_left"] == 1.0


def test_observation_manifest_never_persists_source_text() -> None:
    manifest = observation_manifest(_result("safe", ["Copyrighted source sentence"] ))
    encoded = json.dumps(manifest)
    assert "Copyrighted source sentence" not in encoded
    assert manifest["blocks"][0]["text_characters"] == 27
    assert len(manifest["blocks"][0]["text_sha256"]) == 64


def test_benchmark_reports_one_failed_arm_without_hiding_success(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("content", encoding="utf-8")

    def good(_: Path) -> AdapterResult:
        return _result("good", ["content"])

    def bad(_: Path) -> AdapterResult:
        raise RuntimeError("deliberate")

    report, results = benchmark_source(
        SourceSpec("sample", source),
        {"good": good, "bad": bad},
        repeat=2,
    )
    assert report["arms"]["good"]["deterministic"] is True
    assert report["arms"]["bad"]["status"] == "error"
    assert "manifest" not in report["arms"]["good"]
    assert "source_path" not in report
    assert len(report["source_sha256"]) == 64
    assert set(results) == {"good"}


def test_benchmark_manifest_is_explicit_opt_in(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("content", encoding="utf-8")

    report, _ = benchmark_source(
        SourceSpec("sample", source),
        {"good": lambda _: _result("good", ["content"])},
        include_manifest=True,
    )
    assert report["arms"]["good"]["manifest"]["blocks"][0]["text_characters"] == 7


def test_pandoc_walker_preserves_nested_heading_and_code() -> None:
    ast = [
        {
            "t": "Div",
            "c": [
                ["chapter", [], []],
                [
                    {"t": "Header", "c": [2, ["", [], []], [{"t": "Str", "c": "One"}]]},
                    {"t": "CodeBlock", "c": [["", ["python"], []], "print(1)"]},
                ],
            ],
        }
    ]
    observed = list(_walk_pandoc_blocks(ast))
    assert [(kind, text, level) for kind, text, level, _ in observed] == [
        ("heading", "One", 2),
        ("code", "print(1)", None),
    ]


def test_pandoc_and_app_current_parse_structured_markdown(tmp_path: Path) -> None:
    source = tmp_path / "sample.md"
    source.write_text(
        "# Book\n\n## One\n\nAlpha.\n\n```python\nprint(1)\n```\n\n## Two\n\nBeta.\n",
        encoding="utf-8",
    )
    pandoc = run_pandoc(source)
    current = run_app_current(source)
    assert [unit.title for unit in pandoc.units if unit.unit_kind == "chapter_candidate"] == ["One", "Two"]
    assert any(block.kind == "code" for block in pandoc.blocks)
    assert len(current.units) == 2
    assert current.metadata["extraction_report"]["structure"]["chapter_boundary_level"] == 2


def test_pandoc_unavailable_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "sample.md"
    source.write_text("# Sample", encoding="utf-8")
    with pytest.raises(AdapterUnavailableError, match="Pandoc unavailable"):
        run_pandoc(source, executable=str(tmp_path / "missing-pandoc.exe"))


def test_docling_unavailable_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "sample.md"
    source.write_text("# Sample", encoding="utf-8")
    with pytest.raises(AdapterUnavailableError, match="does not exist"):
        run_docling(source, python_executable=tmp_path / "missing-python.exe")


def test_docling_ascii_stage_is_temporary_and_byte_exact(tmp_path: Path) -> None:
    source_dir = tmp_path / "Tài liệu"
    source_dir.mkdir()
    source = source_dir / "mẫu.md"
    source.write_bytes(b"# Exact\n")
    with _ascii_stage(source) as staged:
        assert staged.read_bytes() == source.read_bytes()
        assert all(ord(character) < 128 for character in str(staged))
        staged_path = staged
    assert not staged_path.exists()


def _arm(
    *,
    warnings: list[str] | None = None,
    document_unit_fallback: bool = False,
    block_kinds: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "status": "ok",
        "metrics": {
            "warnings": warnings or [],
            "document_unit_fallback": document_unit_fallback,
            "block_kinds": block_kinds or {},
        },
    }


def test_recommendation_keeps_high_confidence_epub_fast_path() -> None:
    recommendation = recommend_benchmark_toolchain(
        {
            "source_format": "epub",
            "arms": {
                "app_current": _arm(),
                "pandoc": _arm(),
            },
            "pairwise": [
                {
                    "left": "app_current",
                    "right": "pandoc",
                    "token_coverage_left_by_right": 1.0,
                    "token_coverage_right_by_left": 0.96,
                    "ordered_shingle_coverage_left_by_right": 1.0,
                    "ordered_shingle_coverage_right_by_left": 0.95,
                }
            ],
        }
    )
    assert recommendation["primary"] == "app_current"
    assert recommendation["fallback"] == "pandoc"
    assert recommendation["review_required"] is False


def test_recommendation_rejects_low_confidence_epub_fast_path() -> None:
    recommendation = recommend_benchmark_toolchain(
        {
            "source_format": "epub",
            "arms": {
                "app_current": _arm(warnings=["toc_low_confidence"]),
                "pandoc": _arm(),
            },
            "pairwise": [
                {
                    "left": "app_current",
                    "right": "pandoc",
                    "token_coverage_left_by_right": 1.0,
                    "token_coverage_right_by_left": 0.73,
                    "ordered_shingle_coverage_left_by_right": 1.0,
                    "ordered_shingle_coverage_right_by_left": 0.74,
                }
            ],
        }
    )
    assert recommendation["primary"] == "pandoc"
    assert recommendation["review_required"] is True


def test_recommendation_keeps_plain_text_unsegmented() -> None:
    recommendation = recommend_benchmark_toolchain(
        {
            "source_format": "txt",
            "arms": {"pandoc": _arm(document_unit_fallback=True)},
            "pairwise": [],
        }
    )
    assert recommendation == {
        "primary": "pandoc",
        "fallback": None,
        "structure_status": "document_unit_unsegmented",
        "review_required": True,
        "reasons": [
            "Plain text has no reliable structural metadata.",
            "Pandoc preserves the text as blocks without claiming that a chapter exists.",
        ],
    }
