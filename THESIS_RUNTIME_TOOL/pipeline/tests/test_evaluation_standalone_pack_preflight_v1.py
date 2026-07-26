from __future__ import annotations

import copy
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError, canonical_sha256
from pipeline.eval.d2l_input_v1 import D2L_CANONICAL_POLICY, seal_d2l_evaluation_input
from pipeline.eval.standalone_pack_preflight_v1 import (
    REQUIRED_PACKAGE_FILES,
    preflight_d2l_evaluation_zip_v1,
)


FIXTURE = Path(__file__).parent / "fixtures" / "evaluation_v1" / "d2l_input_valid.json"


def _two_arm_package() -> dict:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["arms"].append(
        {
            "arm_id": "s0",
            "role": "baseline",
            "label": "S0 no memory",
            "translation_artifact_id": "artifact-s0",
            "translation_sha256": "8" * 64,
        }
    )
    payload["artifacts"].append(
        {
            "artifact_id": "artifact-s0",
            "kind": "translation",
            "relative_path": "translations/s0.json",
            "sha256": "8" * 64,
            "size_bytes": 180,
        }
    )
    for row in copy.deepcopy(payload["translations"]):
        row["arm_id"] = "s0"
        row["source_artifact_id"] = "artifact-s0"
        payload["translations"].append(row)
    payload["integrity"]["artifact_set_sha256"] = canonical_sha256(
        {"artifacts": payload["artifacts"]}, policy=D2L_CANONICAL_POLICY
    )
    return seal_d2l_evaluation_input(payload)


def _zip(path: Path, package: dict, *, prefix: str = "handoff/") -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name in REQUIRED_PACKAGE_FILES:
            payload = package if name == "package.json" else {"fixture": name}
            archive.writestr(prefix + name, json.dumps(payload, separators=(",", ":")))


def test_preflight_accepts_wrapped_two_arm_package_without_extracting(tmp_path: Path) -> None:
    path = tmp_path / "handoff.zip"
    _zip(path, _two_arm_package())

    report = preflight_d2l_evaluation_zip_v1(
        path,
        expected_chapter_ids=("chapter-intro",),
    )

    assert report["status"] == "accepted"
    assert report["arm_ids"] == ["s0", "s1"]
    assert report["block_count"] == 2
    assert report["translation_row_count"] == 4
    assert report["zip_root_prefix"] == "handoff/"
    assert list(tmp_path.iterdir()) == [path]


def test_preflight_rejects_wrong_chapter_or_missing_required_file(tmp_path: Path) -> None:
    path = tmp_path / "handoff.zip"
    _zip(path, _two_arm_package())
    with pytest.raises(ContractValidationError, match="chapter_selection"):
        preflight_d2l_evaluation_zip_v1(path, expected_chapter_ids=("foreign",))

    missing = tmp_path / "missing.zip"
    with ZipFile(missing, "w") as archive:
        archive.writestr("package.json", json.dumps(_two_arm_package()))
    with pytest.raises(ContractValidationError, match="zip_files"):
        preflight_d2l_evaluation_zip_v1(
            missing, expected_chapter_ids=("chapter-intro",)
        )


def test_preflight_rejects_zip_slip_and_package_tamper(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.zip"
    with ZipFile(unsafe, "w") as archive:
        archive.writestr("../package.json", "{}")
    with pytest.raises(ContractValidationError, match="zip_path"):
        preflight_d2l_evaluation_zip_v1(
            unsafe, expected_chapter_ids=("chapter-intro",)
        )

    package = _two_arm_package()
    package["runtime_profile"]["domain"] = "tampered"
    tampered = tmp_path / "tampered.zip"
    _zip(tampered, package, prefix="")
    with pytest.raises(ContractValidationError, match="package_hash"):
        preflight_d2l_evaluation_zip_v1(
            tampered, expected_chapter_ids=("chapter-intro",)
        )
