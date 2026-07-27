from __future__ import annotations

import json
from pathlib import Path

from pipeline.scripts import run_literary_b1_registry_to_b2_input_v1 as runner


def test_runner_forwards_explicit_reconciled_projection(monkeypatch, tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    (registry_root / "chapter_registry.json").write_text("{}", encoding="utf-8")
    projection_path = tmp_path / "reconciled_projection.json"
    projection_path.write_text(
        json.dumps({"projection_hash": "p" * 64}), encoding="utf-8"
    )
    captured = {}

    monkeypatch.setattr(runner, "_load_document", lambda *_args: ({"chapters": []}, {}))
    monkeypatch.setattr(runner, "_git_head", lambda: "head-test")

    def build(**kwargs):
        captured.update(kwargs)
        return {
            "package_hash": "k" * 64,
            "source_document_sha256": "d" * 64,
            "ordered_chapter_ids": [],
        }

    monkeypatch.setattr(runner, "build_b2_registry_input_package_v1", build)
    monkeypatch.setattr(
        runner,
        "write_b2_registry_input_package_v1",
        lambda *, output_root, package: Path(output_root) / "b2_registry_input.json",
    )

    out = tmp_path / "out"
    out.mkdir()
    assert runner.main(
        [
            "--registry-root",
            str(registry_root),
            "--output-root",
            str(out),
            "--reconciled-projection",
            str(projection_path),
        ]
    ) == 0
    assert captured["reconciled_projection"] == {"projection_hash": "p" * 64}
    assert captured["document"]["document_id"] == "wuthering_heights"
