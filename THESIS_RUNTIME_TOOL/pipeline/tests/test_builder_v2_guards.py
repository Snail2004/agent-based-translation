from __future__ import annotations

from pipeline.prepass.builder_v2_guards import apply_surface_ownership_guard


def test_surface_ownership_detaches_headword_owned_variants_and_quarantines_zero_occurrence() -> None:
    notebook = {
        "entries": [
            {
                "concept_key": "model",
                "canonical_source_term": "model",
                "canonical_target_vi": "mo hinh",
                "occurrences_total": 13,
                "source_variants": [
                    {"surface": "model", "occurrence_count": 10, "evidence_block_ids": ["b1"]},
                    {"surface": "models", "occurrence_count": 2, "evidence_block_ids": ["b2"]},
                    {"surface": "deep neural networks", "occurrence_count": 1, "evidence_block_ids": ["b3"]},
                    {"surface": "phantom phrase", "occurrence_count": 0, "evidence_block_ids": ["b4"]},
                ],
            },
            {
                "concept_key": "deep neural network",
                "canonical_source_term": "deep neural network",
                "canonical_target_vi": "mang no ron sau",
                "occurrences_total": 1,
                "source_variants": [
                    {"surface": "deep neural network", "occurrence_count": 1, "evidence_block_ids": ["b3"]},
                ],
            },
        ]
    }

    guarded, report = apply_surface_ownership_guard(notebook)
    entries = {entry["concept_key"]: entry for entry in guarded["entries"]}

    assert {item["surface"] for item in entries["model"]["source_variants"]} == {"model", "models"}
    assert entries["model"]["occurrences_total"] == 12
    assert entries["deep neural network"]["source_variants"][0]["surface"] == "deep neural network"
    assert report["detached_count"] == 1
    assert report["detached_surfaces"][0]["owner_entry_id"] == "deep neural network"
    assert report["quarantined_count"] == 1
    assert report["surface_quarantine"][0]["surface"] == "phantom phrase"
