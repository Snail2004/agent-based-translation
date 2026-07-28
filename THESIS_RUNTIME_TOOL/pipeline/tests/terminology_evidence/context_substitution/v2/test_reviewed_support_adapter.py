from __future__ import annotations

import json
import hashlib
import zipfile
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.reviewed_support import (
    reviewed_support_to_context_substitution_input,
    validate_reviewed_support_bundle,
    validate_reviewed_support_receipt,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.runtime_adapter import (
    freeze_to_context_substitution_input,
)
from pipeline.scripts.terminology_evidence.context_substitution.v2.run import main


RUNTIME_ROOT = Path(__file__).resolve().parents[5]
DATASET_ROOT = RUNTIME_ROOT / "pipeline" / "eval" / "terminology_evidence" / "dataset"
V3 = DATASET_ROOT / "d2l_context_support_set_validation_ready_v3"
PILOT = DATASET_ROOT / "pilot_dev_only_v1_1"
PENDING_REVIEW = DATASET_ROOT / "pilot_normalized_review_pack_v1_4"


def test_real_v3_bundle_validates_all_source_rows() -> None:
    result = validate_reviewed_support_bundle(V3)
    assert result["status"] == "PASS"
    assert result["counts"] == {
        "term_senses": 150,
        "candidate_instances": 450,
        "contexts": 1340,
    }
    assert result["provider_call_count"] == 0
    assert result["final_glossary_decision"] is None


def test_v3_zip_selects_supported_root_without_confusing_nested_manifest(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "v3.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(V3.rglob("*")):
            if source.is_file():
                archive.write(source, (Path("bundle") / source.relative_to(V3)).as_posix())
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    result = validate_reviewed_support_bundle(
        archive_path, expected_zip_sha256=expected
    )
    assert result["status"] == "PASS"
    assert result["counts"]["term_senses"] == 150


def test_zip_traversal_and_unbound_zip_bytes_reject(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../manifest.json", "{}")
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with pytest.raises(ContractValidationError, match="path is not canonical"):
        validate_reviewed_support_bundle(
            archive_path, expected_zip_sha256=expected
        )
    with pytest.raises(ContractValidationError, match="physical ZIP hash mismatch"):
        validate_reviewed_support_bundle(
            archive_path, expected_zip_sha256="f" * 64
        )


def test_real_v3_requires_explicit_split_and_adapts_development() -> None:
    with pytest.raises(ContractValidationError, match="explicit development"):
        reviewed_support_to_context_substitution_input(V3)
    adapted = reviewed_support_to_context_substitution_input(
        V3, source_split="development"
    )
    payload = adapted["input"]
    assert len(payload["terms"]) == 100
    assert sum(len(row["candidate_targets"]) for row in payload["terms"]) == 300
    assert sum(len(row["contexts"]) for row in payload["terms"]) == 894
    assert payload["selection_contract"]["selector_mode"] == (
        "MODEL_CLASSIFICATION_DEVELOPMENT"
    )
    assert all(
        binding["ref"].startswith("artifact://")
        for binding in payload["source_artifacts"].values()
    )
    assert validate_reviewed_support_receipt(adapted["receipt"]) == adapted["receipt"]


def test_real_pilot_requires_and_binds_exact_v3_parent() -> None:
    with pytest.raises(ContractValidationError, match="exact V3 parent"):
        validate_reviewed_support_bundle(PILOT)
    result = validate_reviewed_support_bundle(PILOT, parent_v3_source=V3)
    assert result["counts"] == {
        "term_senses": 5,
        "candidate_instances": 15,
        "contexts": 38,
    }
    assert result["parent_dataset_manifest_sha256"] == (
        "258ebe5d907a0a108a1b80a1ec1aad3c6e265ed1a8edbd5701cc128e273122ce"
    )


def test_real_pilot_runtime_adapter_dispatches_without_api() -> None:
    payload = freeze_to_context_substitution_input(PILOT, parent_v3_source=V3)
    assert len(payload["terms"]) == 5
    assert sum(len(row["candidate_targets"]) for row in payload["terms"]) == 15
    assert sum(len(row["contexts"]) for row in payload["terms"]) == 38
    assert payload["input_origin"]["kind"] == "DEVELOPMENT_PILOT_V1_1"


def test_adapter_receipt_rejects_validly_shaped_tamper() -> None:
    receipt = reviewed_support_to_context_substitution_input(
        PILOT, parent_v3_source=V3
    )["receipt"]
    forged = dict(receipt)
    forged["provider_call_count"] = 1
    with pytest.raises(ContractValidationError, match="zero-API"):
        validate_reviewed_support_receipt(forged)


def test_pending_human_review_pack_cannot_claim_frozen_authority() -> None:
    with pytest.raises(ContractValidationError, match="STAGE_A_HUMAN_REVIEW_PENDING"):
        reviewed_support_to_context_substitution_input(
            PILOT,
            parent_v3_source=V3,
            review_artifact=PENDING_REVIEW,
        )


def test_cli_validates_and_materializes_real_pilot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(
        [
            "reviewed-support-validate",
            "--source",
            str(PILOT),
            "--parent-v3",
            str(V3),
        ]
    ) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "PASS"

    output = tmp_path / "input.json"
    receipt = tmp_path / "receipt.json"
    assert main(
        [
            "reviewed-support-to-runtime",
            "--source",
            str(PILOT),
            "--parent-v3",
            str(V3),
            "--source-split",
            "development",
            "--output",
            str(output),
            "--receipt",
            str(receipt),
        ]
    ) == 0
    assert output.is_file() and receipt.is_file()
    validate_reviewed_support_receipt(json.loads(receipt.read_text(encoding="utf-8")))
