from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    OOD_POLICY_VERSION,
    SUPPORT_SET_VERSION,
)


def build_certificate_support_set(
    context_results: Sequence[Mapping[str, Any]],
    contrastive_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    scored_contexts = [
        {
            "context_id": str(row["context_id"]),
            "context_type": str(row["context_type"]),
            "raw_score": int(row["raw_score"]),
            "label": str(row["label"]),
            "source_sha256": str(row["source_sha256"]),
            "source_provenance": dict(row["source_provenance"]),
            "embedding_ref": None,
        }
        for row in context_results
    ]
    positive = [
        row for row in scored_contexts if row["label"] in {"PASS", "MINOR"}
    ]
    negative = [row for row in scored_contexts if row["label"] == "FAIL"]
    contrastive = [
        {
            "context_id": str(row["context_id"]),
            "tested_sense_id": str(row["tested_sense_id"]),
            "result": str(row["result"]),
            "source_provenance": dict(row["source_provenance"]),
            "embedding_ref": None,
        }
        for row in contrastive_results
    ]
    return {
        "positive_support_context_ids": [row["context_id"] for row in positive],
        "positive_support_contexts": positive,
        "negative_or_boundary_context_ids": [
            row["context_id"] for row in negative
        ],
        "negative_or_boundary_contexts": negative,
        "contrastive_context_ids": [row["context_id"] for row in contrastive],
        "contrastive_contexts": contrastive,
        "materialization_status": "CONTEXTS_ONLY",
        "embedding_model_version": None,
        "context_centroid_ref": None,
        "ood_policy_version": OOD_POLICY_VERSION,
        "support_set_version": SUPPORT_SET_VERSION,
        "runtime_tac_ready": False,
    }


