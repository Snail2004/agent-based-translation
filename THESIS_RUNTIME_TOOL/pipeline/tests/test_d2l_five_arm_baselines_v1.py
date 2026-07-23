from __future__ import annotations

import hashlib

import pytest

from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonBlockV1,
    CommonSourceSnapshotV1,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_five_arm_baselines_v1 import (
    build_llm_lc_translation_artifact_v1,
)


def _source() -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id="CanonicalSourcePackageV1",
        source_schema_version="1.0.0",
        source_binding=CanonicalSourcePackageBindingV1(
            project_id="d2l",
            document_id="d2l",
            document=CanonicalComponentIdentityV1("1.5.0", "1" * 64),
            structure=CanonicalComponentIdentityV1("1.0.0", "2" * 64),
            asset_manifest=CanonicalComponentIdentityV1("1.0.0", "3" * 64),
            admitted_projection=CanonicalProjectionIdentityV1(
                "admitted_projection_v1", "4" * 64
            ),
            admission_policy=AdmissionPolicyIdentityV1(
                "canonical_source_admission", "1.0.0", "5" * 64
            ),
        ),
        blocks=(
            CommonBlockV1(
                "b1", "chapter", 0, "paragraph", "source one", "translate"
            ),
            CommonBlockV1(
                "b2", "chapter", 1, "paragraph", "keep exactly", "preserve"
            ),
            CommonBlockV1(
                "b3",
                "chapter",
                2,
                "paragraph",
                "needs review",
                "review_required",
            ),
        ),
    )


def test_full_marked_capture_uses_translation_but_preserves_canonical_rows():
    encoded = (
        "[[B0001]]\nban dich\n"
        "[[B0002]]\nmodel changed this\n"
        "[[B0003]]\nreview text\n"
    ).encode("utf-8")

    artifact = build_llm_lc_translation_artifact_v1(
        _source(),
        marked_markdown_bytes=encoded,
        expected_evidence_sha256=hashlib.sha256(encoded).hexdigest(),
        expected_marker_count=3,
        created_at="2026-07-24T00:00:00Z",
        producer_code_commit="a" * 40,
    )

    assert [row["status"] for row in artifact["translations"]] == [
        "translated",
        "preserved",
        "review_held",
    ]
    assert artifact["translations"][0]["target_text"] == "ban dich"
    assert artifact["translations"][1]["target_text"] == "keep exactly"
    assert artifact["translations"][2]["target_text"] is None


def test_short_marked_capture_cannot_claim_full_capture_marker_count():
    encoded = (
        "[[B0001]]\none\n"
        "[[B0002]]\ntwo\n"
        "[[B0003]]\nthree\n"
    ).encode("utf-8")

    with pytest.raises(
        ContractValidationError, match="exact-cover its declared"
    ):
        build_llm_lc_translation_artifact_v1(
            _source(),
            marked_markdown_bytes=encoded,
            expected_evidence_sha256=hashlib.sha256(encoded).hexdigest(),
            expected_marker_count=143,
            created_at="2026-07-24T00:00:00Z",
            producer_code_commit="a" * 40,
        )


def test_marked_capture_hash_drift_fails_before_projection():
    encoded = b"[[B0001]]\ntranslation\n"

    with pytest.raises(ContractValidationError, match="accepted full capture"):
        build_llm_lc_translation_artifact_v1(
            _source(),
            marked_markdown_bytes=encoded,
            expected_evidence_sha256="f" * 64,
            expected_marker_count=1,
            created_at="2026-07-24T00:00:00Z",
            producer_code_commit="a" * 40,
        )
