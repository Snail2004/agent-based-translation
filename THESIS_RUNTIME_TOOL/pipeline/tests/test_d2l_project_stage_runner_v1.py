from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import validate_translation_artifact
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    validate_scoring_handoff_fragment,
    validate_translation_component_package,
)
from pipeline.prepass.d2l_project_campaign_v2 import prepare_campaign
from pipeline.prepass.d2l_project_stage_runner_v1 import (
    DRY_PROFILE_ID,
    D2LStageRunnerError,
    build_component_plan,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentRunnerError,
    D2LTranslationComponentRunner,
    run_from_plan_file,
)
from pipeline.tests.test_d2l_project_campaign_v2 import (
    CODE_REVISION,
    CREATED_AT,
    _fixture_job,
)


def _prepared(tmp_path: Path, *, chapters: list[str]) -> tuple[Path, Path, dict]:
    job = _fixture_job(tmp_path)
    campaign = tmp_path / "campaign"
    prepare_campaign(
        job_root=job,
        campaign_root=campaign,
        workflow_run_id="wf_stage_runner_fixture",
        component_run_id="tr_stage_runner_fixture",
        code_revision=CODE_REVISION,
        require_clean_code=False,
        chapter_ids=chapters,
        created_at=CREATED_AT,
    )
    plan = build_component_plan(
        campaign_root=campaign,
        job_root=job,
        code_root=Path(__file__).resolve().parents[2],
        dry_run=True,
    )
    return job, campaign, plan


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plan_is_exactly_eleven_stages_and_live_requires_sealed_paths(
    tmp_path: Path,
) -> None:
    job = _fixture_job(tmp_path)
    campaign = tmp_path / "campaign"
    prepare_campaign(
        job_root=job,
        campaign_root=campaign,
        workflow_run_id="wf_stage_plan_fixture",
        component_run_id="tr_stage_plan_fixture",
        code_revision=CODE_REVISION,
        require_clean_code=False,
        chapter_ids=["alpha_unit"],
        created_at=CREATED_AT,
    )
    with pytest.raises(D2LStageRunnerError, match="runtime_root"):
        build_component_plan(
            campaign_root=campaign,
            job_root=job,
            code_root=Path(__file__).resolve().parents[2],
            dry_run=False,
        )
    plan = build_component_plan(
        campaign_root=campaign,
        job_root=job,
        code_root=Path(__file__).resolve().parents[2],
        dry_run=True,
    )
    assert [row["stage_id"] for row in plan["stages"]] == list(STAGE_IDS)
    assert all("--dry-run" in row["command"] for row in plan["stages"])
    assert all(row["receipt_ref"] is None for row in plan["stages"])


def test_live_plan_seals_runtime_credentials_and_stage_receipts(tmp_path: Path) -> None:
    job = _fixture_job(tmp_path)
    campaign = tmp_path / "campaign_live"
    prepare_campaign(
        job_root=job,
        campaign_root=campaign,
        workflow_run_id="wf_stage_live_fixture",
        component_run_id="tr_stage_live_fixture",
        code_revision=CODE_REVISION,
        require_clean_code=False,
        chapter_ids=["alpha_unit"],
        created_at=CREATED_AT,
    )
    credential_files = {
        "credential.modelapi_shared_v1": tmp_path / "modelapi.key",
        "credential.shopaikey_gemini_proxy_v1": tmp_path / "shopapi.key",
    }
    plan = build_component_plan(
        campaign_root=campaign,
        job_root=job,
        code_root=Path(__file__).resolve().parents[2],
        dry_run=False,
        runtime_root=tmp_path / "runtime",
        credential_files=credential_files,
    )
    assert [row["stage_id"] for row in plan["stages"]] == list(STAGE_IDS)
    assert all("--live" in row["command"] for row in plan["stages"])
    assert all("--runtime-root" in row["command"] for row in plan["stages"])
    semantic = {
        "b1_candidate_discovery",
        "b2_admission_translation",
        "auditor_morphology",
        "auditor_target_collision",
        "auditor_multi_target",
        "translator",
        "translation_quality_audit",
    }
    for stage in plan["stages"]:
        if stage["stage_id"] in semantic:
            assert stage["receipt_ref"] == (
                f"artifacts/{stage['stage_id']}/stage_receipt.json"
            )
            assert any(
                spec["relative_path"] == stage["receipt_ref"]
                and spec["artifact_kind"] == "d2l_stage_event_receipt"
                for spec in stage["artifact_specs"]
            )
        else:
            assert stage["receipt_ref"] is None
    encoded = json.dumps(plan, ensure_ascii=False)
    assert "modelapi.key" in encoded
    assert "shopapi.key" in encoded


def test_full_dry_component_exact_covers_admission_and_is_not_publishable(
    tmp_path: Path,
) -> None:
    _job, campaign, _plan = _prepared(
        tmp_path, chapters=["alpha_unit", "beta_unit", "gamma_unit"]
    )
    result = run_from_plan_file(
        campaign / "component_plan.json", campaign / "component"
    )
    assert result["terminal_event"] == "run_done"
    assert result["artifact_count"] == 15
    package = validate_translation_component_package(campaign / "component")
    assert package["component_attempt_id"] == 1

    for arm_id in ("s0", "s1"):
        artifact = validate_translation_artifact(
            _load(campaign / "component" / "artifacts" / "translator" / f"{arm_id}.json")
        )
        assert artifact["run_identity"]["profile_id"] == DRY_PROFILE_ID
        assert artifact["coverage"] == {
            "eligible_count": 5,
            "excluded_count": 0,
            "failed_count": 0,
            "missing_count": 0,
            "preserved_count": 1,
            "review_held_count": 1,
            "source_block_count": 7,
            "translated_count": 5,
        }
        assert [row["block_id"] for row in artifact["translations"]] == [
            "alpha_b001",
            "alpha_b002",
            "beta_b001",
            "beta_b002",
            "beta_b003",
            "gamma_b001",
            "gamma_b002",
        ]
        assert artifact["translations"][5]["status"] == "review_held"
        assert artifact["translations"][5]["target_text"] is None

    fragment = validate_scoring_handoff_fragment(
        _load(campaign / "component" / "scoring_handoff_fragment.json")
    )
    assert fragment["admitted_universe"]["block_count"] == 6
    assert [row["arm_id"] for row in fragment["translation_inputs"]] == ["s0", "s1"]
    assert all(row["profile_id"] == DRY_PROFILE_ID for row in fragment["translation_inputs"])


def test_resume_after_translator_keeps_artifact_attempt_lineage(tmp_path: Path) -> None:
    _job, campaign, _plan = _prepared(
        tmp_path, chapters=["alpha_unit", "beta_unit"]
    )
    paused = run_from_plan_file(
        campaign / "component_plan.json",
        campaign / "component",
        stop_after_stage="translator",
    )
    assert paused["component_attempt_id"] == 1
    resumed = run_from_plan_file(
        campaign / "component_plan.json",
        campaign / "component",
        resume=True,
    )
    assert resumed["component_attempt_id"] == 2
    fragment = _load(campaign / "component" / "scoring_handoff_fragment.json")
    assert fragment["translation_component_attempt_id"] == 2
    assert {
        row["producer_component_attempt_id"] for row in fragment["translation_inputs"]
    } == {1}


def test_resume_rejects_material_plan_drift_before_component_mutation(
    tmp_path: Path,
) -> None:
    _job, campaign, plan = _prepared(tmp_path, chapters=["alpha_unit", "beta_unit"])
    run_from_plan_file(
        campaign / "component_plan.json",
        campaign / "component",
        stop_after_stage="candidate_index",
    )
    tracked = [
        campaign / "component" / "component_manifest.json",
        campaign / "component" / "artifact_index.json",
        campaign / "component" / "events.jsonl",
    ]
    before = [path.read_bytes() for path in tracked]
    drifted = deepcopy(plan)
    drifted["stages"][-1]["command"].append("--foreign-plan-value")
    with pytest.raises(ComponentRunnerError, match="runner plan hash mismatch"):
        D2LTranslationComponentRunner(drifted, campaign / "component").run(resume=True)
    assert [path.read_bytes() for path in tracked] == before
