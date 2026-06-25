from __future__ import annotations

from pipeline.eval.ambiguous_assignment import (
    CandidateSpan,
    ProbeRow,
    _decision_correct,
    _position_decision,
    _region_decision,
    _sample_rows,
)
from pipeline.eval.region_align import EmbeddingCacheClient


class _Response:
    def __init__(self, inputs: list[str]) -> None:
        self.inputs = inputs

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "model": "text-embedding-labse",
            "data": [{"embedding": _vector(text)} for text in self.inputs],
        }


def _vector(text: str) -> list[float]:
    if "user" in text or "nguoi dung" in text:
        return [1.0, 0.0]
    return [0.0, 1.0]


def _post(_endpoint: str, *, json: dict, timeout: int) -> _Response:
    del timeout
    return _Response(list(json["input"]))


def test_region_narrow_assigns_unique_survivor_inside_top_sentence(tmp_path) -> None:
    client = EmbeddingCacheClient(cache_dir=tmp_path, post=_post, model_version="v")
    row = {
        "source_sentence": "associating that user's ID with the requested product's ID.",
        "target_text": "Mot khach hang bam nut. ID nguoi dung duoc lien ket.",
        "_candidate_spans": [
            {"start": 4, "end": 14, "surface": "khach hang"},
            {"start": 28, "end": 38, "surface": "nguoi dung"},
        ],
    }
    decision = _region_decision(row, client=client, k=1, reference="region_narrow@labse")
    assert decision.status == "assign"
    assert decision.surface == "nguoi dung"


def test_region_narrow_abstains_when_top_k_union_keeps_multiple_candidates(tmp_path) -> None:
    client = EmbeddingCacheClient(cache_dir=tmp_path, post=_post, model_version="v")
    row = {
        "source_sentence": "associating that user's ID with the requested product's ID.",
        "target_text": "Mot khach hang bam nut. ID nguoi dung duoc lien ket.",
        "_candidate_spans": [
            {"start": 4, "end": 14, "surface": "khach hang"},
            {"start": 28, "end": 38, "surface": "nguoi dung"},
        ],
    }
    decision = _region_decision(row, client=client, k=2, reference="region_narrow@labse")
    assert decision.status == "abstain"
    assert decision.reason == "top2_sentence_union;survivors=2"


def test_position_narrow_uses_relative_sentence_index() -> None:
    row = {
        "source_span": "28:32:user",
        "source_text": "Customer clicks. Then user ID is stored.",
        "target_text": "Khach hang bam nut. ID nguoi dung duoc luu.",
        "_candidate_spans": [
            {"start": 0, "end": 10, "surface": "Khach hang"},
            {"start": 23, "end": 33, "surface": "nguoi dung"},
        ],
    }
    decision = _position_decision(row, position_window=0)
    assert decision.status == "assign"
    assert decision.surface == "nguoi dung"


def test_position_narrow_rejects_when_no_candidate_survives_region() -> None:
    row = {
        "source_span": "28:32:user",
        "source_text": "Customer clicks. Then user ID is stored.",
        "target_text": "Khach hang bam nut. ID san pham duoc luu.",
        "_candidate_spans": [
            {"start": 0, "end": 10, "surface": "Khach hang"},
        ],
    }
    decision = _position_decision(row, position_window=0)
    assert decision.status == "reject"
    assert _decision_correct(decision, "not_rendered", None)


def test_sample_rows_keeps_canonical_b003_user_variant_case() -> None:
    mandatory = ProbeRow(
        probe_type="variant_stealing",
        block_id="d2l_introduction_index_b003",
        chapter="Preface",
        config="S1",
        source_term="user",
        source_term_id="registry:user",
        source_occ_id="canonical",
        source_start=0,
        source_end=4,
        source_surface="user",
        source_sentence="user sentence",
        source_text="user sentence",
        target_text="target",
        block_is_multi_sentence=True,
        candidate_spans=(CandidateSpan(0, 1, "x", "active", ("registry:user",), "x", True),),
        ev08_role="active_surplus",
    )
    filler = ProbeRow(
        probe_type="control",
        block_id="other",
        chapter="Preface",
        config="S1",
        source_term="other",
        source_term_id="registry:other",
        source_occ_id="control",
        source_start=0,
        source_end=5,
        source_surface="other",
        source_sentence="other sentence",
        source_text="other sentence",
        target_text="target",
        block_is_multi_sentence=False,
        candidate_spans=(CandidateSpan(0, 1, "x", "active", ("registry:other",), "x", True),),
        ev08_role="active_unique",
    )
    selected = _sample_rows([filler, mandatory], n=0, n_control=0)
    assert [row.source_occ_id for row in selected] == ["canonical"]
