from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_LEGACY,
    BACKEND_MODE_SHARED_V1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.literary_context_pipeline_v1 import (
    CHECKPOINT_SCHEMA_VERSION_SHARED,
    LiteraryContextPipelineError,
    _run_b2_chapter,
    build_context_chapter_checkpoint_v1,
    initialize_context_pipeline_run_v1,
    load_context_pipeline_state_v1,
    run_context_pipeline_live_v1,
    tree_hash_v1,
    verify_context_chapter_checkpoint_v1,
)
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256
from pipeline.tests.test_literary_context_pipeline_v1 import (
    PROFILE_PATH,
    _fake_b1_run,
    _fake_b2_and_recovery,
    _write_json,
)


class _RuntimeIdentity:
    def __init__(self, marker: str = "runtime-a") -> None:
        self.marker = marker

    def identity_payload(self) -> dict[str, Any]:
        return {
            "backend_kind": "shared_v1",
            "source_id": "source.fake",
            "source_revision": "source.fake.v1",
            "physical_quota_bucket_id": "bucket.fake",
            "run_id": "run.fake",
            "attempt_run_id": self.marker,
        }


def _rehash(path: Path, field: str, **updates: Any) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = dict(payload)
    body.pop(field)
    body.update(updates)
    result = {**body, field: canonical_hash(body)}
    _write_json(path, result)
    return result


def test_shared_plan_propagates_one_runtime_and_rejects_legacy_resume_before_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _RuntimeIdentity()
    document = {
        "document_id": "book",
        "chapters": [
            {
                "chapter_id": "book_ch01",
                "blocks": [
                    {
                        "block_id": "book_ch01_b001",
                        "order_index": 0,
                        "block_type": "paragraph",
                        "clean_text": "Robin entered the house.",
                    }
                ],
            }
        ],
    }
    document_path = tmp_path / "document.json"
    _write_json(document_path, document)
    frozen = tmp_path / "frozen.sqlite3"
    frozen.write_bytes(b"synthetic frozen fixture")
    real_hash = file_sha256

    def fake_file_sha256(path: Path) -> str:
        return (
            FROZEN_DB_SHA256
            if Path(path).resolve() == frozen.resolve()
            else real_hash(path)
        )

    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1.file_sha256",
        fake_file_sha256,
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "initialize_chapter_cycle_run_v1",
        lambda **_: None,
    )
    run_root = tmp_path / "run"
    initialize_context_pipeline_run_v1(
        run_root=run_root,
        document_path=document_path,
        profile_path=PROFILE_PATH,
        frozen_db=frozen,
        ordered_chapter_ids=["book_ch01"],
        current_git_head="head",
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,  # type: ignore[arg-type]
    )
    plan = json.loads((run_root / "run_plan.json").read_text(encoding="utf-8"))
    assert plan["backend_mode"] == BACKEND_MODE_SHARED_V1
    assert plan["shared_runtime_identity_hash"] == canonical_hash(
        runtime.identity_payload()
    )

    observed: list[tuple[str, str, object]] = []

    def capture(stage: str, result: object):
        def inner(**kwargs: Any) -> object:
            observed.append(
                (stage, kwargs["backend_mode"], kwargs["shared_runtime"])
            )
            return result

        return inner

    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "_run_b1_through_chapter",
        capture("b1", None),
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "write_literary_run_summary_v1",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "snapshot_completed_b1_prefix_v1",
        lambda **_: {},
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "generate_chapter_runtime_profiles_v1",
        lambda **_: {
            "b2_profile_path": str(tmp_path / "b2_profile.json"),
            "recovery_profile_path": str(tmp_path / "recovery_profile.json"),
        },
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1._run_b2_chapter",
        capture("b2", tmp_path / "b2"),
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1._run_recovery_chapter",
        capture("recovery", tmp_path / "recovery"),
    )

    def fake_checkpoint(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["backend_mode"] == BACKEND_MODE_SHARED_V1
        assert kwargs["shared_runtime_identity"] == runtime.identity_payload()
        body = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION_SHARED,
            "chapter_id": kwargs["chapter_id"],
            "backend": {
                "backend_mode": BACKEND_MODE_SHARED_V1,
                "shared_runtime_identity_hash": canonical_hash(
                    runtime.identity_payload()
                ),
            },
        }
        return {**body, "checkpoint_hash": canonical_hash(body)}

    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "build_context_chapter_checkpoint_v1",
        fake_checkpoint,
    )
    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "write_context_pipeline_summary_v1",
        lambda _root: {"status": "complete"},
    )
    summary = run_context_pipeline_live_v1(
        run_root=run_root,
        credential_root=None,
        current_git_head="head",
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,  # type: ignore[arg-type]
    )
    assert summary == {"status": "complete"}
    assert observed == [
        ("b1", BACKEND_MODE_SHARED_V1, runtime),
        ("b2", BACKEND_MODE_SHARED_V1, runtime),
        ("recovery", BACKEND_MODE_SHARED_V1, runtime),
    ]

    state_before = (run_root / "run_state.json").read_bytes()
    with pytest.raises(LiteraryContextPipelineError, match="plan inputs changed"):
        run_context_pipeline_live_v1(
            run_root=run_root,
            credential_root=tmp_path,
            current_git_head="head",
            backend_mode=BACKEND_MODE_LEGACY,
            shared_runtime=None,
        )
    assert (run_root / "run_state.json").read_bytes() == state_before


def test_shared_checkpoint_binds_b2_and_recovery_to_exact_runtime(
    tmp_path: Path,
) -> None:
    runtime = _RuntimeIdentity()
    identity = runtime.identity_payload()
    b1_root = _fake_b1_run(tmp_path, source_head="head")
    b2_root, recovery_root = _fake_b2_and_recovery(
        tmp_path,
        b1_root=b1_root,
        chapter_id="book_ch01",
        source_head="head",
    )
    _rehash(
        b2_root / "run_seal.json",
        "seal_hash",
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime_identity=identity,
    )
    _rehash(
        b2_root / "live_report.json",
        "report_hash",
        backend_mode=BACKEND_MODE_SHARED_V1,
    )
    _rehash(
        recovery_root / "run_seal.json",
        "seal_hash",
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime_identity=identity,
        source_tree_hash=tree_hash_v1(b2_root),
    )
    _rehash(
        recovery_root / "live_report.json",
        "report_hash",
        backend_mode=BACKEND_MODE_SHARED_V1,
    )
    checkpoint = build_context_chapter_checkpoint_v1(
        plan_hash="plan",
        chapter_id="book_ch01",
        chapter_ordinal=1,
        b1_root=b1_root,
        b2_root=b2_root,
        recovery_root=recovery_root,
        current_git_head="head",
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime_identity=identity,
    )
    path = tmp_path / "checkpoint.json"
    _write_json(path, checkpoint)
    verified = verify_context_chapter_checkpoint_v1(
        path,
        current_git_head="head",
        expected_backend_mode=BACKEND_MODE_SHARED_V1,
        expected_shared_runtime_identity_hash=canonical_hash(identity),
    )
    assert verified["backend"]["shared_runtime_identity_hash"] == canonical_hash(
        identity
    )
    with pytest.raises(LiteraryContextPipelineError, match="backend mode differs"):
        verify_context_chapter_checkpoint_v1(
            path,
            current_git_head="head",
            expected_backend_mode=BACKEND_MODE_LEGACY,
        )


def test_shared_b2_resume_rejects_a_legacy_attempt_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "b2" / "ch001" / "attempt_001"
    attempt.mkdir(parents=True)
    seal_body = {"chapter_id": "book_ch01"}
    _write_json(
        attempt / "run_seal.json",
        {**seal_body, "seal_hash": canonical_hash(seal_body)},
    )

    def forbidden(**_kwargs: Any) -> None:
        raise AssertionError("mixed-backend attempt reached B2 execution")

    monkeypatch.setattr(
        "pipeline.literary.literary_context_pipeline_v1."
        "execute_b2_frame_live_v1",
        forbidden,
    )
    with pytest.raises(LiteraryContextPipelineError, match="backend mode differs"):
        _run_b2_chapter(
            root=tmp_path,
            chapter_ordinal=1,
            snapshot_root=tmp_path / "snapshot",
            b2_profile_path=tmp_path / "profile.json",
            credential_root=None,
            frozen_db=tmp_path / "frozen.sqlite3",
            current_git_head="head",
            max_attempts=1,
            backend_mode=BACKEND_MODE_SHARED_V1,
            shared_runtime=_RuntimeIdentity(),  # type: ignore[arg-type]
        )
