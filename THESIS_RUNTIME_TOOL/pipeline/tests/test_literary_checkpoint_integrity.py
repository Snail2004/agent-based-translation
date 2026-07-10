from __future__ import annotations

from pathlib import Path

from pipeline.literary.checkpoint import artifact_manifest, build_checkpoint, validate_checkpoint


def test_checkpoint_rejects_each_identity_field_and_ignores_abandoned_tmp(tmp_path: Path) -> None:
    artifact = tmp_path / "brief" / "bk_ch01.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    checkpoint = build_checkpoint(
        {
            "stage": "m1",
            "chapter_id": "bk_ch01",
            "source_hash": "source-a",
            "prompt_hashes": {"brief": "prompt-a"},
            "config_hash": "config-a",
            "schema_version": "schema-a",
            "parent_checkpoint_hash": None,
            "artifact_manifest": artifact_manifest([artifact], root=tmp_path),
            "state": {},
        }
    )
    (tmp_path / "checkpoints" / "m1").mkdir(parents=True)
    (tmp_path / "checkpoints" / "m1" / ".bk_ch01.json.crash.tmp").write_text(
        "incomplete",
        encoding="utf-8",
    )
    expected = {
        "stage": "m1",
        "chapter_id": "bk_ch01",
        "source_hash": "source-a",
        "prompt_hashes": {"brief": "prompt-a"},
        "config_hash": "config-a",
        "schema_version": "schema-a",
        "parent_checkpoint_hash": None,
    }
    assert validate_checkpoint(checkpoint, root=tmp_path, expected=expected) == []

    for field, changed in {
        "source_hash": "source-b",
        "prompt_hashes": {"brief": "prompt-b"},
        "config_hash": "config-b",
        "schema_version": "schema-b",
        "parent_checkpoint_hash": "parent-b",
    }.items():
        candidate = dict(expected)
        candidate[field] = changed
        assert field in validate_checkpoint(checkpoint, root=tmp_path, expected=candidate)
