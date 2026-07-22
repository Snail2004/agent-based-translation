from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError, canonical_sha256
from pipeline.eval.d2l_input_v1 import (
    D2L_CANONICAL_POLICY,
    seal_d2l_evaluation_input,
)
from pipeline.eval.terminology_occurrence_v1 import (
    build_terminology_occurrence_metrics_v1,
    persist_terminology_occurrence_metrics_v1,
    project_full_run_metric_rows_v1,
    seal_terminology_occurrence_metrics_v1,
    validate_terminology_occurrence_metrics_v1,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "evaluation_v1"
    / "d2l_input_valid.json"
)
COMMIT = "a" * 40


def test_scores_tc_occ_and_ta_occ_without_api() -> None:
    artifact = _build_artifact()

    assert artifact["arms"]["S0"]["tc_occ"]["numerator_majority"] == 2
    assert artifact["arms"]["S0"]["tc_occ"]["denominator_localized"] == 3
    assert artifact["arms"]["S0"]["tc_occ"]["value"] == pytest.approx(2 / 3)
    assert artifact["arms"]["S0"]["ta_occ"]["numerator_accepted"] == 2
    assert artifact["arms"]["S0"]["ta_occ"]["denominator_all_occurrences"] == 3
    assert artifact["arms"]["S1"]["tc_occ"]["value"] == 1
    assert artifact["arms"]["S1"]["ta_occ"]["value_lower"] == 1
    assert artifact["comparison"]["tc_occ_delta"] == pytest.approx(1 / 3)
    assert artifact["comparison"]["ta_occ_delta"] == pytest.approx(1 / 3)
    assert artifact["producer"]["workstream"] == "evaluation"


def test_not_rendered_is_excluded_from_tc_but_not_ta() -> None:
    package, cascades, descriptors = _inputs()
    row = cascades["S0"]["decisions"][2]
    row.update(
        {
            "resolved_by": "t3_llm",
            "decision": "not_rendered",
            "target_start": None,
            "target_end": None,
            "target_surface": "",
            "t3_code_score": {"adherence_label": "not_rendered"},
        }
    )
    artifact = _build(package, cascades, descriptors)

    summary = artifact["arms"]["S0"]
    assert summary["localized_occurrence_count"] == 2
    assert summary["not_rendered_occurrence_count"] == 1
    assert summary["tc_occ"]["value"] == 1
    assert summary["ta_occ"]["value_lower"] == pytest.approx(2 / 3)


def test_unresolved_adherence_is_disclosed_as_possible_upper() -> None:
    package, cascades, descriptors = _inputs()
    row = cascades["S0"]["decisions"][2]
    row["resolved_by"] = "t3_pending"
    row.pop("t3_code_score")
    artifact = _build(package, cascades, descriptors)

    summary = artifact["arms"]["S0"]["ta_occ"]
    assert summary["numerator_accepted"] == 2
    assert summary["unresolved_adherence_count"] == 1
    assert summary["value_lower"] == pytest.approx(2 / 3)
    assert summary["value_upper"] == 1


def test_rejects_cross_arm_occurrence_drop() -> None:
    package, cascades, descriptors = _inputs()
    cascades["S1"]["decisions"].pop()
    with pytest.raises(ContractValidationError, match="occurrence_exact_cover"):
        _build(package, cascades, descriptors)


def test_rejects_cross_arm_ruler_drift() -> None:
    package, cascades, descriptors = _inputs()
    cascades["S1"]["decisions"][0]["accepted_forms"] = ["foreign-form"]
    with pytest.raises(ContractValidationError, match="ruler_drift"):
        _build(package, cascades, descriptors)


def test_rejects_package_target_text_drift() -> None:
    package, cascades, descriptors = _inputs()
    cascades["S1"]["decisions"][0]["target_text"] += " drift"
    with pytest.raises(ContractValidationError, match="target_text_binding"):
        _build(package, cascades, descriptors)


def test_rejects_unknown_input_artifact_field() -> None:
    package, cascades, descriptors = _inputs()
    descriptors["S0"]["path"] = "not-authoritative.json"
    with pytest.raises(ContractValidationError, match="unknown keys"):
        _build(package, cascades, descriptors)


def test_binds_exact_cascade_payload_beyond_physical_descriptor() -> None:
    package, cascades, descriptors = _inputs()
    original = _build(package, cascades, descriptors)
    cascades["S0"]["legacy_diagnostic"] = {"note": "changed but not scored"}
    changed = _build(package, cascades, descriptors)

    assert (
        original["inputs"]["S0"]["artifact_sha256"]
        == changed["inputs"]["S0"]["artifact_sha256"]
    )
    assert (
        original["inputs"]["S0"]["payload_sha256"]
        != changed["inputs"]["S0"]["payload_sha256"]
    )
    assert (
        original["integrity"]["artifact_sha256"]
        != changed["integrity"]["artifact_sha256"]
    )


def test_rejects_resealed_metric_arithmetic_tamper() -> None:
    artifact = _build_artifact()
    tampered = copy.deepcopy(artifact)
    tampered["arms"]["S0"]["tc_occ"]["numerator_majority"] = 3
    tampered["integrity"]["artifact_sha256"] = "0" * 64
    resealed = seal_terminology_occurrence_metrics_v1(tampered)
    with pytest.raises(ContractValidationError, match="metric_arithmetic"):
        validate_terminology_occurrence_metrics_v1(resealed)


def test_rejects_nonfinite_metric_even_when_resealed() -> None:
    artifact = _build_artifact()
    tampered = copy.deepcopy(artifact)
    tampered["arms"]["S0"]["tc_occ"]["value"] = float("nan")
    tampered["integrity"]["artifact_sha256"] = "0" * 64
    with pytest.raises(ContractValidationError, match="non_finite"):
        seal_terminology_occurrence_metrics_v1(tampered)


def test_projection_is_d2l_scoped_and_full_run_compatible() -> None:
    rows = project_full_run_metric_rows_v1(_build_artifact())

    assert [row["metric_id"] for row in rows] == ["tc_occ", "ta_occ"]
    assert all(row["profile_scope"] == "d2l" for row in rows)
    assert all(row["unit"] == "ratio" for row in rows)
    assert rows[0]["comparison"]["baseline_arm_id"] == "S0"
    assert rows[0]["comparison"]["candidate_arm_id"] == "S1"
    assert rows[1]["method"]["model_id"] is None


def test_persistence_is_content_addressed_and_create_only(tmp_path: Path) -> None:
    artifact = _build_artifact()
    first = persist_terminology_occurrence_metrics_v1(
        output_root=tmp_path, artifact_payload=artifact
    )
    second = persist_terminology_occurrence_metrics_v1(
        output_root=tmp_path, artifact_payload=artifact
    )

    assert first.reused is False
    assert second.reused is True
    assert first.path == second.path
    assert json.loads(first.path.read_text(encoding="utf-8")) == artifact


def _build_artifact() -> dict:
    package, cascades, descriptors = _inputs()
    return _build(package, cascades, descriptors)


def _build(package: dict, cascades: dict, descriptors: dict) -> dict:
    return build_terminology_occurrence_metrics_v1(
        package,
        cascades,
        descriptors,
        generated_at="2026-07-21T00:00:00Z",
        producer_code_commit=COMMIT,
    )


def _inputs() -> tuple[dict, dict, dict]:
    package = _two_arm_package()
    source = package["blocks"][0]["source_text"]
    source_spans = _spans(source, "tensor")
    targets = {
        "S0": "Tensor luu mot tensor va mot ma tran.",
        "S1": "Tensor luu mot tensor va mot tensor.",
    }
    cascades: dict[str, dict] = {}
    for arm_id, target in targets.items():
        tensor_spans = _spans(target, "tensor")
        matrix_span = _spans(target, "ma tran")
        decisions = []
        for index, (source_start, source_end) in enumerate(source_spans):
            if arm_id == "S0" and index == 2:
                target_start, target_end = matrix_span[0]
                rendered = target[target_start:target_end]
                resolved_by = "t3_llm"
                t3_score = {"adherence_label": "off_glossary"}
            else:
                target_start, target_end = tensor_spans[index]
                rendered = target[target_start:target_end]
                resolved_by = "t2_credit"
                t3_score = None
            decision = {
                "occ_id": f"{arm_id}:b001:term-tensor:{index}",
                "config": arm_id,
                "block_id": "b001",
                "chapter_id": "chapter-intro",
                "source_term": "tensor",
                "term_id": "term-tensor",
                "source_start": source_start,
                "source_end": source_end,
                "source_surface": source[source_start:source_end],
                "source_text": source,
                "target_text": target,
                "resolved_by": resolved_by,
                "decision": "rendered" if resolved_by == "t2_credit" else "localized",
                "target_start": target_start,
                "target_end": target_end,
                "target_surface": rendered,
                "accepted_forms": ["tensor", "ten-xo"],
            }
            if t3_score is not None:
                decision["t3_code_score"] = t3_score
            decisions.append(decision)
        cascades[arm_id] = {"config": arm_id, "decisions": decisions}
    descriptors = {
        "S0": {"artifact_id": "cascade-s0", "artifact_sha256": "8" * 64},
        "S1": {"artifact_id": "cascade-s1", "artifact_sha256": "9" * 64},
    }
    return package, cascades, descriptors


def _two_arm_package() -> dict:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["identity"]["experiment_id"] = "exp-terminology-occurrence"
    payload["blocks"][0]["source_text"] = "A tensor stores a tensor beside a tensor."
    payload["arms"] = [
        {
            "arm_id": "S0",
            "role": "baseline",
            "label": "S0",
            "translation_artifact_id": "artifact-s0",
            "translation_sha256": "6" * 64,
        },
        {
            "arm_id": "S1",
            "role": "candidate",
            "label": "S1",
            "translation_artifact_id": "artifact-s1",
            "translation_sha256": "5" * 64,
        },
    ]
    payload["translations"] = [
        {
            "arm_id": "S0",
            "block_id": "b001",
            "status": "translated",
            "target_text": "Tensor luu mot tensor va mot ma tran.",
            "error_code": None,
            "source_artifact_id": "artifact-s0",
        },
        {
            "arm_id": "S0",
            "block_id": "b002",
            "status": "passthrough",
            "target_text": "tensor.shape",
            "error_code": None,
            "source_artifact_id": "artifact-s0",
        },
        {
            "arm_id": "S1",
            "block_id": "b001",
            "status": "translated",
            "target_text": "Tensor luu mot tensor va mot tensor.",
            "error_code": None,
            "source_artifact_id": "artifact-s1",
        },
        {
            "arm_id": "S1",
            "block_id": "b002",
            "status": "passthrough",
            "target_text": "tensor.shape",
            "error_code": None,
            "source_artifact_id": "artifact-s1",
        },
    ]
    payload["injection_rows"] = []
    payload["artifacts"] = [
        row for row in payload["artifacts"] if row["artifact_id"] != "artifact-s1"
    ] + [
        {
            "artifact_id": "artifact-s0",
            "kind": "translation",
            "relative_path": "translations/s0.json",
            "sha256": "6" * 64,
            "size_bytes": 100,
        },
        {
            "artifact_id": "artifact-s1",
            "kind": "translation",
            "relative_path": "translations/s1.json",
            "sha256": "5" * 64,
            "size_bytes": 100,
        },
    ]
    payload["integrity"]["artifact_set_sha256"] = canonical_sha256(
        {"artifacts": payload["artifacts"]}, policy=D2L_CANONICAL_POLICY
    )
    payload["integrity"]["package_sha256"] = "0" * 64
    return seal_d2l_evaluation_input(payload)


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    result = []
    cursor = 0
    folded = text.casefold()
    while True:
        start = folded.find(needle.casefold(), cursor)
        if start < 0:
            return result
        result.append((start, start + len(needle)))
        cursor = start + len(needle)
