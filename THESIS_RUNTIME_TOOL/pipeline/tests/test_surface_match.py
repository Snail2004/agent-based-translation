from __future__ import annotations

from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, find_spans


def test_source_subsumption_machine_learning() -> None:
    text = "Use machine learning here, then discuss learning alone."
    allocated = allocate_spans(
        text,
        [
            SurfaceOwner("learning", "learning"),
            SurfaceOwner("machine_learning", "machine learning"),
        ],
    )

    assert [span.surface for span in allocated["machine_learning"]] == ["machine learning"]
    assert [span.surface for span in allocated["learning"]] == ["learning"]


def test_target_subsumption_hoc_may() -> None:
    text = "Học máy giúp quá trình học ổn định."
    allocated = allocate_spans(
        text,
        [
            SurfaceOwner("learning:hoc", "học"),
            SurfaceOwner("machine_learning:hoc_may", "học máy"),
        ],
    )

    assert [span.surface for span in allocated["machine_learning:hoc_may"]] == ["Học máy"]
    assert [span.surface for span in allocated["learning:hoc"]] == ["học"]


def test_offsets_map_to_original_and_mask_url() -> None:
    text = "Visit https://discuss.d2l.ai/ for AI help."

    spans = find_spans(text, "AI")

    assert len(spans) == 1
    start, end, surface = spans[0]
    assert surface == "AI"
    assert text[start:end] == "AI"


def test_case_sensitive_acronym() -> None:
    text = "ai AI Ai"

    assert [span[2] for span in find_spans(text, "AI", case_sensitive=True)] == ["AI"]
    assert [span[2] for span in find_spans(text, "AI", case_sensitive=False)] == ["ai", "AI", "Ai"]
