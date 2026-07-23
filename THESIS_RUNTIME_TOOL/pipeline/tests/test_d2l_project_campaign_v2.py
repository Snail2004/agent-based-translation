from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    canonical_sha256,
    file_sha256,
)
from pipeline.prepass.d2l_project_campaign_v2 import (
    D2LCampaignError,
    TRANSPORT_SEAL_SCHEMA,
    bind_component_plan,
    build_transport_attempt_seal,
    initial_transport_sources,
    load_campaign,
    load_project,
    prepare_campaign,
    select_chapters,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import RUNNER_SCHEMA


CREATED_AT = "2026-07-23T00:00:00Z"
CODE_REVISION = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _package_rows(root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": file_sha256(path).lower(),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    ]


def _block(block_id: str, order: int, block_type: str, text: str) -> dict[str, object]:
    return {
        "annotations": {},
        "block_id": block_id,
        "block_type": block_type,
        "clean_text": text,
        "is_chapter_opening": order == 0,
        "order_index": order,
        "page_ids": [],
        "quality_flags": [],
        "sentences": [],
        "source_text": text,
    }


def _fixture_job(tmp_path: Path) -> Path:
    root = tmp_path / "job"
    package = root / "source_package_snapshot"
    lifecycle = root / "lifecycle_snapshot"
    package.mkdir(parents=True)
    lifecycle.mkdir()
    chapters = [
        {
            "chapter_id": "alpha_unit",
            "order_index": 0,
            "title": "Alpha",
            "blocks": [
                _block("alpha_b001", 0, "heading", "# Alpha"),
                _block("alpha_b002", 1, "paragraph", "A technical definition."),
            ],
        },
        {
            "chapter_id": "beta_unit",
            "order_index": 1,
            "title": "Beta",
            "blocks": [
                _block("beta_b001", 0, "paragraph", "A long explanation."),
                _block("beta_b002", 1, "paragraph", "Structured table text."),
                _block("beta_b003", 2, "code", "print('keep')"),
            ],
        },
        {
            "chapter_id": "gamma_unit",
            "order_index": 2,
            "title": "Gamma",
            "blocks": [
                _block("gamma_b001", 0, "paragraph", "Needs review."),
                _block("gamma_b002", 1, "paragraph", "Ordinary prose."),
            ],
        },
    ]
    global_order = 0
    for chapter in chapters:
        for block in chapter["blocks"]:
            block["order_index"] = global_order
            global_order += 1
    document = {
        "schema_version": "1.5.0",
        "doc_id": "fixture_doc",
        "metadata": {},
        "chapters": chapters,
    }
    structure = {
        "schema_version": "fixture_structure_v1",
        "doc_id": "fixture_doc",
        "units": [row["chapter_id"] for row in chapters],
    }
    assets = {
        "schema_version": "fixture_assets_v1",
        "doc_id": "fixture_doc",
        "assets": [],
    }
    receipt = {
        "schema_version": "fixture_receipt_v1",
        "doc_id": "fixture_doc",
        "status": "accepted",
    }
    for name, value in (
        ("document.json", document),
        ("structure_manifest.json", structure),
        ("asset_manifest.json", assets),
        ("normalization_receipt.json", receipt),
    ):
        _write_json(package / name, value)
    channels = [
        "semantic_text",
        "semantic_text",
        "semantic_text",
        "structured_translate",
        "preserve_only",
        "review_required",
        "semantic_text",
    ]
    flat_blocks = [block for chapter in chapters for block in chapter["blocks"]]
    flat_chapters = [
        chapter["chapter_id"] for chapter in chapters for _block_row in chapter["blocks"]
    ]
    projection_body = {
        "schema_version": "admitted_projection_v1",
        "doc_id": "fixture_doc",
        "inputs": {
            "document": {
                "schema_version": "1.5.0",
                "sha256": canonical_sha256(document),
            },
            "structure": {
                "schema_version": "fixture_structure_v1",
                "sha256": canonical_sha256(structure),
            },
            "asset_manifest": {
                "schema_version": "fixture_assets_v1",
                "sha256": canonical_sha256(assets),
            },
        },
        "policy": {"policy_id": "fixture", "policy_version": "1"},
        "rows": [
            {
                "chapter_id": chapter_id,
                "block_id": block["block_id"],
                "channel": channel,
            }
            for chapter_id, block, channel in zip(
                flat_chapters, flat_blocks, channels, strict=True
            )
        ],
    }
    projection = {
        **projection_body,
        "integrity": {
            "payload_sha256": canonical_sha256(projection_body),
            "row_count": len(flat_blocks),
        },
    }
    _write_json(package / "admitted_projection_v1.json", projection)
    logical = {
        "document": canonical_sha256(document),
        "structure": canonical_sha256(structure),
        "asset_manifest": canonical_sha256(assets),
        "admitted_projection": canonical_sha256(projection),
        "normalization_receipt": canonical_sha256(receipt),
    }
    finalization_body = {
        "schema_version": "source_package_finalization_v1",
        "doc_id": "fixture_doc",
        "lifecycle": "finalized_pre_run",
        "pipeline_run_count": 0,
        "source": {
            "filename": "fixture.md",
            "format": "markdown",
            "sha256": "b" * 64,
        },
        "package": {
            key: {
                "schema_version": {
                    "document": "1.5.0",
                    "structure": "fixture_structure_v1",
                    "asset_manifest": "fixture_assets_v1",
                    "admitted_projection": "admitted_projection_v1",
                    "normalization_receipt": "fixture_receipt_v1",
                }[key],
                "sha256": value,
            }
            for key, value in logical.items()
        },
    }
    finalization_payload_sha = canonical_sha256(finalization_body)
    finalization = {
        **finalization_body,
        "integrity": {"payload_sha256": finalization_payload_sha},
    }
    finalization_path = lifecycle / "finalization.json"
    _write_json(finalization_path, finalization)
    database = root / "memory.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO fixture(value) VALUES ('source')")
    connection.commit()
    connection.close()
    package_rows = _package_rows(package)
    manifest_body = {
        "contract_version": "project_runtime_source_v2",
        "job_id": "fixture_job",
        "project_id": "fixture_project",
        "document_doc_id": "fixture_doc",
        "source_document": "source_package_snapshot/document.json",
        "source_snapshot": "source_package_snapshot/document.json",
        "original_sha256": logical["document"].lower(),
        "source_identity_sha256": "c" * 64,
        "stripped_sha256": file_sha256(package / "document.json").lower(),
        "initial_runtime_db_sha256": file_sha256(database).lower(),
        "created_at": CREATED_AT,
        "profiles": ["technical_d2l_v1"],
        "chapters": [
            {
                "chapter_id": chapter["chapter_id"],
                "order_index": chapter["order_index"],
                "title": chapter["title"],
                "block_count": len(chapter["blocks"]),
                "translation_policy": "translate",
                "review_required": False,
            }
            for chapter in chapters
        ],
        "chapter_count": len(chapters),
        "block_count": len(flat_blocks),
        "translatable_chapter_ids": [chapter["chapter_id"] for chapter in chapters],
        "review_required_chapter_ids": [],
        "source_package_snapshot": {
            "path": "source_package_snapshot",
            "tree_sha256": canonical_sha256(package_rows).lower(),
            "file_count": len(package_rows),
            "rows": package_rows,
        },
        "finalization_snapshot": {
            "path": "lifecycle_snapshot/finalization.json",
            "sha256": file_sha256(finalization_path).lower(),
            "payload_sha256": finalization_payload_sha.lower(),
        },
    }
    manifest = {
        **manifest_body,
        "manifest_payload_sha256": canonical_sha256(manifest_body).lower(),
    }
    _write_json(root / "source_manifest.json", manifest)
    return root


def _prepare(tmp_path: Path, *, chapters: list[str] | None = None) -> tuple[Path, Path]:
    job = _fixture_job(tmp_path)
    campaign = tmp_path / "campaign"
    prepare_campaign(
        job_root=job,
        campaign_root=campaign,
        workflow_run_id="wf_fixture_campaign_v1",
        component_run_id="tr_fixture_campaign_v1",
        code_revision=CODE_REVISION,
        require_clean_code=False,
        chapter_ids=chapters or ["alpha_unit", "beta_unit"],
        created_at=CREATED_AT,
    )
    return job, campaign


def _plan(campaign: Path) -> dict[str, object]:
    loaded = load_campaign(campaign)
    config = loaded["config"]
    stages = [
        {
            "stage_id": stage_id,
            "producer": stage_id,
            "command": None,
            "cwd": None,
            "artifact_specs": [],
            "total": 0,
            "unit": "items",
            "work_id": f"work_{stage_id}",
            "mode": "reused",
            "timeout_seconds": None,
            "receipt_ref": None,
        }
        for stage_id in STAGE_IDS
    ]
    return {
        "schema": RUNNER_SCHEMA,
        "workflow_run_id": config["workflow_run_id"],
        "component_run_id": config["component_run_id"],
        "pipeline_id": config["pipeline_id"],
        "pipeline_version": config["pipeline_version"],
        "source_binding": config["source_binding"],
        "config_sha256": config["integrity"]["payload_sha256"],
        "code_revision": config["code_revision"],
        "selected_chapter_ids": config["selected_chapter_ids"],
        "stages": stages,
        "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
    }


def test_lists_arbitrary_chapters_and_supports_three_selection_modes(tmp_path: Path) -> None:
    project = load_project(_fixture_job(tmp_path))
    assert [row["chapter_id"] for row in project.chapter_rows] == [
        "alpha_unit",
        "beta_unit",
        "gamma_unit",
    ]
    assert select_chapters(project, chapter_ids=["alpha_unit", "gamma_unit"]) == (
        "explicit",
        ("alpha_unit", "gamma_unit"),
    )
    assert select_chapters(
        project, start_chapter="alpha_unit", end_chapter="beta_unit"
    ) == ("range", ("alpha_unit", "beta_unit"))
    assert select_chapters(project, all_chapters=True) == (
        "all",
        ("alpha_unit", "beta_unit", "gamma_unit"),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chapter_ids": ["alpha_unit", "alpha_unit"]},
        {"chapter_ids": ["missing_unit"]},
        {"chapter_ids": ["gamma_unit", "alpha_unit"]},
        {"start_chapter": "gamma_unit", "end_chapter": "alpha_unit"},
        {"start_chapter": "alpha_unit"},
        {},
    ],
)
def test_rejects_ambiguous_or_invalid_selection(tmp_path: Path, kwargs: dict[str, object]) -> None:
    project = load_project(_fixture_job(tmp_path))
    with pytest.raises(D2LCampaignError):
        select_chapters(project, **kwargs)


def test_prepare_seals_routes_models_limits_and_isolated_state(tmp_path: Path) -> None:
    job = _fixture_job(tmp_path)
    source_db_before = file_sha256(job / "memory.sqlite3")
    source_manifest_before = file_sha256(job / "source_manifest.json")
    campaign = tmp_path / "campaign"
    result = prepare_campaign(
        job_root=job,
        campaign_root=campaign,
        workflow_run_id="wf_fixture_campaign_v1",
        component_run_id="tr_fixture_campaign_v1",
        code_revision=CODE_REVISION,
        require_clean_code=False,
        start_chapter="alpha_unit",
        end_chapter="gamma_unit",
        created_at=CREATED_AT,
    )
    assert result["status"] == "ready_0_api"
    loaded = load_campaign(campaign)
    universe = loaded["universe"]
    assert universe["block_count"] == 7
    assert universe["channel_counts"] == {
        "semantic_text": 4,
        "structured_translate": 1,
        "preserve_only": 1,
        "review_required": 1,
    }
    assert universe["routing"]["b1_b2_block_count"] == 4
    assert universe["routing"]["translator_llm_block_count"] == 5
    assert universe["routing"]["review_held_block_count"] == 1
    assert universe["routing"]["review_required_sent_to_llm"] is False
    llm_window_block_ids = {
        block_id
        for family in ("b1", "translator")
        for window in universe["window_estimates"][family]["windows"]
        for block_id in window["block_ids"]
    }
    assert "gamma_b001" not in llm_window_block_ids
    assert all(
        len({window["chapter_id"]}) == 1
        for window in universe["window_estimates"]["translator"]["windows"]
    )
    roles = {row["role_id"]: row for row in loaded["config"]["semantic_roles"]}
    assert roles["d2l.candidate_discovery"]["model_id"] == "gemini-3.5-flash"
    assert roles["d2l.b2.admission"]["model_id"] == "gpt-5.4"
    assert roles["d2l.b2.morphology"]["model_id"] == "gpt-5.5"
    assert roles["d2l.translator.quality_auditor"]["model_id"] == "gpt-5.5"
    assert all(
        row["output_contract"]["structured_output_mode"] == "disabled"
        and row["output_contract"]["native_schema_parameter_sent"] is False
        for row in roles.values()
    )
    assert loaded["config"]["limits"]["forecast_cost_usd"] is None
    assert loaded["config"]["limits"]["reserved_cost_cap_usd"] is None
    assert loaded["config"]["limits"]["hard_total_token_cap"] > 0
    assert (
        loaded["config"]["limits"]["hard_total_token_cap"]
        <= loaded["config"]["limits"]["theoretical_role_reserve_tokens"]
    )
    assert loaded["config"]["limits"]["forecast_total_tokens"] > 0
    assert (
        loaded["config"]["limits"]["forecast_token_range"]["low"]
        < loaded["config"]["limits"]["forecast_total_tokens"]
        < loaded["config"]["limits"]["forecast_token_range"]["high"]
    )
    sources = loaded["config"]["transport_sources"]
    assert set(sources) == {
        "shopaikey_gemini_proxy_v2",
        "modelapi_shared_v1",
    }
    assert all(
        row["output_mode"] == "prompt_generated_json"
        and row["native_schema_parameter_sent"] is False
        for row in sources.values()
    )
    assert file_sha256(campaign / "state" / "work.sqlite3") == source_db_before
    assert file_sha256(job / "memory.sqlite3") == source_db_before
    assert file_sha256(job / "source_manifest.json") == source_manifest_before
    attempt_files = list((campaign / "transport_attempts").rglob("attempt_0001.json"))
    assert len(attempt_files) == len(roles)


def test_manifest_and_package_hash_drift_fail_closed(tmp_path: Path) -> None:
    job = _fixture_job(tmp_path)
    manifest = json.loads((job / "source_manifest.json").read_text(encoding="utf-8"))
    manifest["project_id"] = "tampered_project"
    _write_json(job / "source_manifest.json", manifest)
    with pytest.raises(D2LCampaignError, match="manifest payload hash drift"):
        load_project(job)

    job2 = _fixture_job(tmp_path / "second")
    document = job2 / "source_package_snapshot" / "document.json"
    document.write_text(document.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(D2LCampaignError, match="file tree drift"):
        load_project(job2)


def test_projection_missing_duplicate_and_foreign_rows_are_rejected(tmp_path: Path) -> None:
    for label, mutate in (
        ("missing", lambda rows: rows.pop()),
        ("duplicate", lambda rows: rows.__setitem__(1, deepcopy(rows[0]))),
        ("foreign", lambda rows: rows[0].__setitem__("block_id", "foreign_b001")),
    ):
        root = tmp_path / label
        job = _fixture_job(root)
        projection_path = job / "source_package_snapshot" / "admitted_projection_v1.json"
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        mutate(projection["rows"])
        projection_body = dict(projection)
        projection_body.pop("integrity")
        projection["integrity"] = {
            "payload_sha256": canonical_sha256(projection_body),
            "row_count": len(projection["rows"]),
        }
        _write_json(projection_path, projection)
        # The outer package hash is expected to catch the mutation even before
        # semantic exact-cover checks. Either path is correctly fail-closed.
        with pytest.raises(D2LCampaignError):
            load_project(job)


def test_campaign_artifact_mutation_and_root_reuse_fail_closed(tmp_path: Path) -> None:
    job, campaign = _prepare(tmp_path)
    config_path = campaign / "campaign_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["semantic_roles"][0]["model_id"] = "different-model"
    _write_json(config_path, config)
    with pytest.raises(D2LCampaignError, match="artifact hash drift"):
        load_campaign(campaign)
    with pytest.raises(D2LCampaignError, match="fresh"):
        prepare_campaign(
            job_root=job,
            campaign_root=campaign,
            workflow_run_id="wf_second",
            component_run_id="tr_second",
            code_revision=CODE_REVISION,
            require_clean_code=False,
            chapter_ids=["alpha_unit"],
        )


def test_model_prompt_validator_cap_and_chapter_are_seal_material(tmp_path: Path) -> None:
    _job, campaign = _prepare(tmp_path)
    config = load_campaign(campaign)["config"]
    original = config["integrity"]["payload_sha256"]
    mutations = []
    for field, value in (
        ("model_id", "different-model"),
        ("validator_id", "different-validator"),
    ):
        row = deepcopy(config)
        row.pop("integrity")
        row["semantic_roles"][0][field] = value
        mutations.append(canonical_sha256(row))
    prompt = deepcopy(config)
    prompt.pop("integrity")
    prompt["semantic_roles"][0]["prompt"]["sha256"] = "f" * 64
    mutations.append(canonical_sha256(prompt))
    cap = deepcopy(config)
    cap.pop("integrity")
    cap["limits"]["hard_total_token_cap"] -= 1
    mutations.append(canonical_sha256(cap))
    chapter = deepcopy(config)
    chapter.pop("integrity")
    chapter["selected_chapter_ids"] = ["alpha_unit"]
    mutations.append(canonical_sha256(chapter))
    assert all(digest != original for digest in mutations)


def test_fake_component_manifest_cannot_bypass_work_db_seed_check(tmp_path: Path) -> None:
    _job, campaign = _prepare(tmp_path)
    with (campaign / "state" / "work.sqlite3").open("ab") as handle:
        handle.write(b"tamper")
    _write_json(campaign / "component" / "component_manifest.json", {"status": "running"})
    with pytest.raises(D2LCampaignError, match="component manifest is invalid"):
        load_campaign(campaign)


def test_transport_change_keeps_model_and_rejects_secret_or_model_drift(tmp_path: Path) -> None:
    _job, campaign = _prepare(tmp_path)
    source = deepcopy(initial_transport_sources()["modelapi_shared_v1"])
    source["source_id"] = "alternate_modelapi_route_v1"
    source["source_revision"] = "alternate_revision_v1"
    source["physical_quota_bucket_id"] = "alternate-physical-bucket-v1"
    accepted = build_transport_attempt_seal(
        campaign,
        role_id="d2l.b2.morphology",
        source_record=source,
        component_attempt_id=2,
        transport_attempt_index=2,
        created_at=CREATED_AT,
    )
    assert accepted["schema_version"] == TRANSPORT_SEAL_SCHEMA
    assert accepted["model_id"] == "gpt-5.5"
    assert accepted["source"]["physical_quota_bucket_id"] == "alternate-physical-bucket-v1"

    wrong_model = deepcopy(source)
    wrong_model["supported_model_ids"] = ["gpt-5.4"]
    with pytest.raises(D2LCampaignError, match="does not support"):
        build_transport_attempt_seal(
            campaign,
            role_id="d2l.b2.morphology",
            source_record=wrong_model,
            component_attempt_id=2,
            transport_attempt_index=3,
            created_at=CREATED_AT,
        )
    leaked = deepcopy(source)
    leaked["api_key"] = "plaintext"
    with pytest.raises(D2LCampaignError, match="forbidden key"):
        build_transport_attempt_seal(
            campaign,
            role_id="d2l.b2.morphology",
            source_record=leaked,
            component_attempt_id=2,
            transport_attempt_index=4,
            created_at=CREATED_AT,
        )


def test_component_plan_binds_only_exact_campaign_identity(tmp_path: Path) -> None:
    _job, campaign = _prepare(tmp_path)
    plan = _plan(campaign)
    result = bind_component_plan(campaign, plan)
    assert result["status"] == "bound"
    assert bind_component_plan(campaign, plan)["status"] == "already_bound"
    drift = deepcopy(plan)
    drift["selected_chapter_ids"] = ["alpha_unit"]
    with pytest.raises(D2LCampaignError, match="does not match"):
        bind_component_plan(campaign, drift)


def test_campaign_root_must_not_overlap_source_project(tmp_path: Path) -> None:
    job = _fixture_job(tmp_path)
    with pytest.raises(D2LCampaignError, match="overlap"):
        prepare_campaign(
            job_root=job,
            campaign_root=job / "campaign",
            workflow_run_id="wf_overlap",
            component_run_id="tr_overlap",
            code_revision=CODE_REVISION,
            require_clean_code=False,
            chapter_ids=["alpha_unit"],
        )


def test_prepare_rejects_declared_revision_that_is_not_runtime_head(tmp_path: Path) -> None:
    job = _fixture_job(tmp_path)
    with pytest.raises(D2LCampaignError, match="does not match runtime Git HEAD"):
        prepare_campaign(
            job_root=job,
            campaign_root=tmp_path / "campaign",
            workflow_run_id="wf_bad_revision",
            component_run_id="tr_bad_revision",
            code_revision="f" * 40,
            require_clean_code=False,
            chapter_ids=["alpha_unit"],
        )
