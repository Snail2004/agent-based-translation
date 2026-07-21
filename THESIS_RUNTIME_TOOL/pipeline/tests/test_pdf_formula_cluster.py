from __future__ import annotations

import copy

import pytest

from pipeline.ingest.pdf_formula_cluster import (
    PdfFormulaClusterError,
    build_formula_cluster,
    validate_formula_cluster,
)


def _cluster() -> dict:
    return build_formula_cluster(
        doc_id="fixture_pdf",
        source_sha256="a" * 64,
        normalizer_version="pdf_normalizer_v3",
        page_number=2,
        detector_region_ids=["reg_formula", "reg_caption"],
        members=[
            {
                "block_id": "fixture_b001",
                "role": "duplicate_evidence",
                "bbox_pdf": [100.0, 200.0, 300.0, 220.0],
                "detector_region_ids": ["reg_formula"],
            },
            {
                "block_id": "fixture_b002",
                "role": "duplicate_evidence",
                "bbox_pdf": [310.0, 200.0, 350.0, 220.0],
                "detector_region_ids": ["reg_caption"],
            },
            {
                "block_id": "fixture_b003",
                "role": "publication_visual",
                "bbox_pdf": [90.0, 190.0, 360.0, 230.0],
                "detector_region_ids": ["reg_formula", "reg_caption"],
            },
        ],
        publication_block_id="fixture_b003",
        publication_bbox_pdf=[90.0, 190.0, 360.0, 230.0],
    )


def test_formula_cluster_identity_is_deterministic_and_closed() -> None:
    first = _cluster()
    second = _cluster()

    assert first == second
    assert validate_formula_cluster(first) == first


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: row.update(formula_cluster_id="fcl_" + "0" * 24),
        lambda row: row["members"].reverse(),
        lambda row: row["members"][2]["bbox_pdf"].__setitem__(2, 359.0),
        lambda row: row["members"][1].update(block_id="fixture_b001"),
        lambda row: row["detector_region_ids"].reverse(),
        lambda row: row.update(publication_block_id="fixture_b001"),
    ],
)
def test_formula_cluster_rejects_identity_geometry_order_and_cover_tamper(
    mutator,
) -> None:
    cluster = copy.deepcopy(_cluster())
    mutator(cluster)

    with pytest.raises(PdfFormulaClusterError):
        validate_formula_cluster(cluster)
