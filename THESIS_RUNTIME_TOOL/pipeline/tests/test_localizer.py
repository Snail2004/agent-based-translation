from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.eval.localizer import (
    LocalizerGoldRow,
    LocalizedSpan,
    build_localizer_gold,
    input_fingerprints,
    localize_first_match,
    localize_longest_match,
    rep_occ_valid,
    render_localizer_gold_html,
    run_localizers,
    score_localizer_bakeoff,
    validate_gold_occ_matches_scorer_rep_occ,
    validate_report_matches_inputs,
)


@dataclass(frozen=True)
class Block:
    block_id: str
    text: str


class FakeAligner:
    def get_word_aligns(self, src_sent: list[str], trg_sent: list[str]) -> dict[str, list[tuple[int, int]]]:
        del src_sent, trg_sent
        return {"itermax": [(0, 0), (0, 1)]}


class NeedleToGoldAligner:
    def get_word_aligns(self, src_sent: list[str], trg_sent: list[str]) -> dict[str, list[tuple[int, int]]]:
        src_index = src_sent.index("needle")
        trg_index = trg_sent.index("gold")
        return {"itermax": [(src_index, trg_index)]}


def _row(
    row_id: str,
    *,
    registry_class: str = "in",
    gold_start: int = 0,
    gold_end: int = 1,
    edge_kind: str = "",
) -> LocalizerGoldRow:
    return LocalizerGoldRow(
        row_id=row_id,
        item_id=row_id,
        config="S0",
        source_term="term",
        surface="term",
        block_id="b1",
        registry_class=registry_class,
        source_text="term",
        source_start="0",
        source_end="4",
        source_surface="term",
        target_text="thuật ngữ",
        candidate_1='{"start":0,"end":8,"surface":"thuật ngữ"}',
        candidate_2="",
        candidate_3="",
        candidate_4="",
        candidates_blind_json="[]",
        prefilled="auto",
        human_edited="",
        gold_target_start=str(gold_start),
        gold_target_end=str(gold_end),
        gold_target_span="",
        note="",
        edge_kind=edge_kind,
    )


def test_three_localizers_same_interface() -> None:
    text = "Bộ dữ liệu MNIST rất phổ biến."
    first = localize_first_match(text, "MNIST", candidate_surfaces=["bộ dữ liệu MNIST"])
    longest = localize_longest_match(text, "MNIST", candidate_surfaces=["bộ dữ liệu MNIST"])

    assert first is not None
    assert longest is not None
    assert isinstance(first, LocalizedSpan)
    assert isinstance(longest, LocalizedSpan)


def test_longest_fixes_substring() -> None:
    text = "Bộ dữ liệu MNIST với 60000 chữ số viết tay được xem là rất lớn."

    first = localize_first_match(text, "MNIST", candidate_surfaces=["bộ dữ liệu MNIST"])
    longest = localize_longest_match(text, "MNIST", candidate_surfaces=["bộ dữ liệu MNIST"])

    assert first is not None and first.surface == "MNIST"
    assert longest is not None and longest.surface == "Bộ dữ liệu MNIST"


def test_firstmatch_fails_membership() -> None:
    text = "Điều này thuộc một lớp lớn; sự thuộc của điểm được xác định sau."

    first = localize_first_match(text, "thuộc", candidate_surfaces=["sự thuộc"])
    longest = localize_longest_match(text, "thuộc", candidate_surfaces=["sự thuộc"])

    assert first is not None and first.surface.lower() == "thuộc"
    assert longest is not None and longest.surface == "sự thuộc"
    assert first.start != longest.start


def test_rep_occ_autocheck_flags_target() -> None:
    blocks = [Block("b1", "The target is clear.")]
    translations = {
        "S0": {"b1": "Mục tiêu rất rõ."},
        "S1": {"b1": "Mục tiêu rất rõ."},
    }

    ok, block_id, reason = rep_occ_valid(
        source_term="target",
        s0_surface="mục tiêu",
        s1_surface="mục tiêu",
        blocks=blocks,
        translations=translations,
    )

    assert ok is False
    assert block_id is None
    assert "genuinely different" in reason


def test_metricA_metricB_separated() -> None:
    rows = [
        _row("in1", registry_class="in", gold_start=0, gold_end=8),
        _row("out1", registry_class="out", gold_start=0, gold_end=8),
    ]
    proposals = {
        "first_match": {
            "in1": LocalizedSpan(0, 8, "thuật ngữ", "first_match"),
            "out1": None,
        },
        "longest_match": {
            "in1": LocalizedSpan(0, 8, "thuật ngữ", "longest_match"),
            "out1": None,
        },
        "simalign": {
            "in1": LocalizedSpan(0, 8, "thuật ngữ", "simalign"),
            "out1": LocalizedSpan(0, 8, "thuật ngữ", "simalign"),
        },
    }

    report = score_localizer_bakeoff(rows, proposals)

    assert report["metricA"]["first_match"]["metricA_n"] == 1
    assert report["metricB_out_of_registry"]["simalign"]["n"] == 1
    assert report["metricB_out_of_registry"]["first_match"]["exact"] == 0


def test_documented_np_expansion_limitation_does_not_block_longest_recommendation() -> None:
    rows = [
        {
            "row_id": "mt_mnist_dataset_923cfe4011:S1",
            "item_id": "mt_mnist_dataset_923cfe4011",
            "config": "S1",
            "source_term": "MNIST dataset",
            "surface": "MNIST",
            "block_id": "b1",
            "registry_class": "in",
            "source_text": "This is the MNIST dataset.",
            "source_start": "12",
            "source_end": "25",
            "source_surface": "MNIST dataset",
            "target_text": "tap du lieu MNIST",
            "gold_target_start": "0",
            "gold_target_end": "17",
            "gold_target_span": "tap du lieu MNIST",
            "edge_kind": "",
        }
    ]
    proposals = {
        "first_match": {"mt_mnist_dataset_923cfe4011:S1": LocalizedSpan(12, 17, "MNIST", "first_match")},
        "longest_match": {"mt_mnist_dataset_923cfe4011:S1": LocalizedSpan(12, 17, "MNIST", "longest_match")},
        "simalign": {"mt_mnist_dataset_923cfe4011:S1": None},
    }

    report = score_localizer_bakeoff(rows, proposals)

    assert report["recommendation"] == "longest_match"
    assert report["metricA"]["longest_match"]["regression_fail"] == []
    assert report["metricA"]["longest_match"]["documented_limitation_fail"] == [
        "mt_mnist_dataset_923cfe4011:S1"
    ]


def test_gold_is_localization_not_adequacy() -> None:
    row = _row("i1", gold_start=0, gold_end=8)

    assert row.gold_target_start == "0"
    assert row.gold_target_end == "8"
    assert not hasattr(row, "label")
    assert not hasattr(row, "better")


def test_simalign_deterministic() -> None:
    row = _row("i1", gold_start=0, gold_end=8)

    first = run_localizers([row], aligner_factory=FakeAligner)
    second = run_localizers([row], aligner_factory=FakeAligner)

    assert first["simalign"]["i1"] == second["simalign"]["i1"]
    assert first["simalign"]["i1"] is not None


def test_simalign_uses_full_target_block_when_sentence_counts_diverge() -> None:
    source = "One. Two. The needle appears here. Four."
    source_start = source.index("needle")
    target = "Sai mot. Cau dung co gold o day. Sai ba."
    gold_start = target.index("gold")
    row = LocalizerGoldRow(
        row_id="needle:S0",
        item_id="needle",
        config="S0",
        source_term="needle",
        surface="gold",
        block_id="b1",
        registry_class="in",
        source_text=source,
        source_start=str(source_start),
        source_end=str(source_start + len("needle")),
        source_surface="needle",
        target_text=target,
        candidate_1='{"start":16,"end":20,"surface":"gold"}',
        candidate_2="",
        candidate_3="",
        candidate_4="",
        candidates_blind_json="[]",
        prefilled="auto",
        human_edited="",
        gold_target_start=str(gold_start),
        gold_target_end=str(gold_start + len("gold")),
        gold_target_span="gold",
        note="source sentence index differs from target sentence index",
    )

    result = run_localizers([row], aligner_factory=NeedleToGoldAligner)
    span = result["simalign"]["needle:S0"]

    assert span is not None
    assert span.start == gold_start
    assert span.end == gold_start + len("gold")
    assert span.surface == "gold"


def test_no_raw_findspans_first_match_for_attribution() -> None:
    source = Path("pipeline/eval/memory_tradeoff.py").read_text(encoding="utf-8")

    assert "s0_spans[0]" not in source
    assert "s1_spans[0]" not in source
    assert "localizer_name" in source


def test_render_localizer_gold_html_exports_csv() -> None:
    row = _row("i1", gold_start=0, gold_end=8)

    html = render_localizer_gold_html([row])

    assert "Localizer Gold Worksheet" in html
    assert "Export localizer_gold.csv" in html
    assert "Dùng phần bôi đen" in html
    assert "selectedTextInBox" in html
    assert "download = \"localizer_gold.csv\"" in html
    assert "gold_target_start" in html


def test_gold_occ_matches_scorer_rep_occ() -> None:
    annotation_text = (
        "Annotation can be automated. "
        "We can annotate text. "
        "The parser uses grammatical assumptions to get some annotation."
    )
    annotation_start = annotation_text.rfind("annotation")
    set_text = "We set parameters first. Linear algebra gives us a powerful set of techniques."
    set_start = set_text.rfind("set")
    rows = [
        {
            "row_id": "annotation:S0",
            "item_id": "annotation_item",
            "source_term": "annotation",
            "block_id": "b_annotation",
            "source_text": annotation_text,
            "source_start": str(annotation_start),
            "source_end": str(annotation_start + len("annotation")),
            "source_surface": "annotation",
        },
        {
            "row_id": "set:S0",
            "item_id": "set_item",
            "source_term": "set",
            "block_id": "b_set",
            "source_text": set_text,
            "source_start": str(set_start),
            "source_end": str(set_start + len("set")),
            "source_surface": "set",
        },
    ]
    override_rows = [
        {
            "item_id": "annotation_item",
            "rep_block_id": "b_annotation",
            "en_sentence": "The parser uses grammatical assumptions to get some annotation.",
        },
        {
            "item_id": "set_item",
            "rep_block_id": "b_set",
            "en_sentence": "Linear algebra gives us a powerful set of techniques.",
        },
    ]

    report = validate_gold_occ_matches_scorer_rep_occ(rows, override_rows)

    assert report["checked"] == 2

    bad_rows = [dict(row) for row in rows]
    bad_rows[0]["source_start"] = "0"
    bad_rows[0]["source_end"] = str(len("Annotation"))
    bad_rows[0]["source_surface"] = "Annotation"

    with pytest.raises(ValueError, match="annotation"):
        validate_gold_occ_matches_scorer_rep_occ(bad_rows, override_rows)


def test_build_gold_uses_override_rep_block_id_and_sentence() -> None:
    override_rows = [
        {
            "item_id": "needle_item",
            "source_term": "needle",
            "rep_block_id": "b2",
            "en_sentence": "The right needle is here.",
            "s0_surface": "kim",
            "s1_surface": "cay kim",
        }
    ]
    blocks_by_id = {
        "b1": "The wrong needle is here.",
        "b2": "The right needle is here.",
    }
    translations = {
        "S0": {"b2": "Cau dung co kim o day."},
        "S1": {"b2": "Cau dung co cay kim o day."},
    }

    rows = build_localizer_gold(
        override_rows,
        blocks_by_id=blocks_by_id,
        translations=translations,
        include_edges=False,
    )

    assert {row.block_id for row in rows} == {"b2"}
    assert {row.source_text for row in rows} == {"The right needle is here."}

    bad_rows = [dict(override_rows[0], en_sentence="The wrong needle is here.")]
    with pytest.raises(ValueError, match="en_sentence"):
        build_localizer_gold(
            bad_rows,
            blocks_by_id=blocks_by_id,
            translations=translations,
            include_edges=False,
        )


def test_stale_report_rejected_by_hash_and_reconciliation(tmp_path: Path) -> None:
    source = "The selected needle is here."
    start = source.index("needle")
    gold_rows = [
        {
            "row_id": "needle:S0",
            "item_id": "needle_item",
            "source_term": "needle",
            "block_id": "b1",
            "source_text": source,
            "source_start": str(start),
            "source_end": str(start + len("needle")),
            "source_surface": "needle",
        }
    ]
    override_rows = [
        {
            "item_id": "needle_item",
            "rep_block_id": "b1",
            "en_sentence": "The selected needle is here.",
        }
    ]
    gold_path = tmp_path / "gold.csv"
    override_path = tmp_path / "override.csv"
    gold_path.write_text("row_id,item_id\nneedle:S0,needle_item\n", encoding="utf-8")
    override_path.write_text("item_id,rep_block_id,en_sentence\nneedle_item,b1,The selected needle is here.\n", encoding="utf-8")
    report = {
        "input_fingerprints": input_fingerprints(gold_path=gold_path, override_path=override_path),
        "gold_occ_reconciliation": validate_gold_occ_matches_scorer_rep_occ(gold_rows, override_rows),
    }

    assert validate_report_matches_inputs(
        report,
        gold_path=gold_path,
        override_path=override_path,
        gold_rows=gold_rows,
        override_rows=override_rows,
    )

    stale_report = {**report, "gold_occ_reconciliation": {"checked": 0, "edge_checked": 1, "failures": []}}
    with pytest.raises(ValueError, match="reconciliation"):
        validate_report_matches_inputs(
            stale_report,
            gold_path=gold_path,
            override_path=override_path,
            gold_rows=gold_rows,
            override_rows=override_rows,
        )

    gold_path.write_text("row_id,item_id\nneedle:S0,changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_report_matches_inputs(
            report,
            gold_path=gold_path,
            override_path=override_path,
            gold_rows=gold_rows,
            override_rows=override_rows,
        )
