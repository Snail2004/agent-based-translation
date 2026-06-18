from __future__ import annotations

import pytest

from pipeline.eval import surface_match
from pipeline.eval.surface_match import (
    SegmenterUnavailable,
    SurfaceOwner,
    allocate_spans,
    find_spans,
    segmenter_version,
)


def test_en_source_subsumption_machine_learning() -> None:
    text = "Use machine learning here, then discuss learning alone."
    allocated = allocate_spans(
        text,
        [
            SurfaceOwner("learning", "learning"),
            SurfaceOwner("machine_learning", "machine learning"),
        ],
        language="en",
    )

    assert [span.surface for span in allocated["machine_learning"]] == ["machine learning"]
    assert [span.surface for span in allocated["learning"]] == ["learning"]


def test_vi_compounds_reduce_syllable_collision() -> None:
    text = "Chúng ta tập trung vào bài tập và thực tập, rồi xét tập hợp."

    tap_spans = find_spans(text, "tập", language="vi")
    tap_hop_spans = find_spans(text, "tập hợp", language="vi")

    assert [span[2] for span in tap_spans] == []
    assert [span[2] for span in tap_hop_spans] == ["tập hợp"]


def test_vi_subsumption_hoc_may() -> None:
    text = "Học máy giúp quá trình học ổn định."
    allocated = allocate_spans(
        text,
        [
            SurfaceOwner("learning:hoc", "học"),
            SurfaceOwner("machine_learning:hoc_may", "học máy"),
        ],
        language="vi",
    )

    assert [span.surface for span in allocated["machine_learning:hoc_may"]] == ["Học máy"]
    assert [span.surface for span in allocated["learning:hoc"]] == ["học"]


def test_offsets_map_to_original_and_mask_url() -> None:
    text = "Visit https://discuss.d2l.ai/ for AI help."

    spans = find_spans(text, "AI", language="en")

    assert len(spans) == 1
    start, end, surface = spans[0]
    assert surface == "AI"
    assert text[start:end] == "AI"


def test_vi_offsets_map_to_original() -> None:
    text = "Mô hình dùng tập hợp dữ liệu."

    spans = find_spans(text, "tập hợp", language="vi")

    assert len(spans) == 1
    start, end, surface = spans[0]
    assert surface == "tập hợp"
    assert text[start:end] == "tập hợp"


def test_case_sensitive_acronym() -> None:
    text = "ai AI Ai"

    assert [span[2] for span in find_spans(text, "AI", language="en", case_sensitive=True)] == ["AI"]
    assert [span[2] for span in find_spans(text, "AI", language="en", case_sensitive=False)] == ["ai", "AI", "Ai"]


def test_missing_segmenter_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    segmenter_version.cache_clear()

    def missing_version(name: str) -> str:
        raise surface_match.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(surface_match.metadata, "version", missing_version)

    with pytest.raises(SegmenterUnavailable):
        segmenter_version()

    segmenter_version.cache_clear()
