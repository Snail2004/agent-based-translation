from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.scripts import run_literary_two_chapter_registry_v1 as runner
from pipeline.scripts.run_chapter_registry_v4_real import (
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
)


PROVIDER_PROFILE = (
    Path(__file__).resolve().parents[2]
    / "pipeline"
    / "configs"
    / "literary_provider_profile_v1.json"
)


def _chapter_ids() -> tuple[str, str]:
    document = json.loads(DEFAULT_DOCUMENT.read_text(encoding="utf-8"))
    return document["chapters"][0]["chapter_id"], document["chapters"][1][
        "chapter_id"
    ]


def _initialize(tmp_path: Path) -> tuple[Path, dict]:
    first, second = _chapter_ids()
    run_root = tmp_path / "two_chapter_run"
    state = runner.initialize_run(
        run_root=run_root,
        document_path=DEFAULT_DOCUMENT,
        design_doc=DEFAULT_DESIGN_DOC,
        frozen_db=DEFAULT_FROZEN_DB,
        first_chapter_id=first,
        second_chapter_id=second,
    )
    return run_root, state


def test_init_seals_whole_book_lineage_and_b0_dry_envelope(tmp_path: Path) -> None:
    run_root, initial = _initialize(tmp_path)
    document = json.loads(DEFAULT_DOCUMENT.read_text(encoding="utf-8"))
    plan = json.loads((run_root / "run_plan.json").read_text(encoding="utf-8"))
    expected_lineage = state_lineage_id_for_manifest(
        build_book_source_manifest(document)
    )
    assert plan["state_lineage_id"] == expected_lineage
    assert plan["production_publish_performed"] is False
    assert initial["current_stage"] == "ch1_b0"

    prepared = runner.prepare_current_stage(run_root)
    assert prepared["current_stage"] == "ch1_b0"
    assert len(prepared["prepared_envelope_hash"]) == 64
    assert prepared["stage_receipts"] == []
    assert (run_root / "stages" / "ch1_b0" / "dry" / "request.json").is_file()


def test_live_stage_requires_exact_prepared_hash_and_records_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, _initial = _initialize(tmp_path)
    prepared = runner.prepare_current_stage(run_root)

    with pytest.raises(runner.TwoChapterRegistryRunError, match="approved envelope"):
        runner.run_current_live_stage(
            run_root=run_root,
            approved_envelope_hash="0" * 64,
            gemini_keys_file=tmp_path / "keys.txt",
            gemini_bucket_id="test-bucket",
            openai_key_1=None,
            openai_key_2=None,
        )

    def fake_b0_live(**kwargs: object) -> dict:
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = {
            "schema_version": "test_inventory_v1",
            "chapter_id": _chapter_ids()[0],
        }
        (output_dir / "inventory.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
        (output_dir / "raw_result.json").write_text(
            json.dumps({"from_cache": False}), encoding="utf-8"
        )
        return artifact

    monkeypatch.setattr(runner, "run_b0_live", fake_b0_live)
    advanced = runner.run_current_live_stage(
        run_root=run_root,
        approved_envelope_hash=prepared["prepared_envelope_hash"],
        gemini_keys_file=tmp_path / "keys.txt",
        gemini_bucket_id="test-bucket",
        openai_key_1=None,
        openai_key_2=None,
    )
    assert advanced["current_stage"] == "ch1_local_auditor"
    assert advanced["stage_receipts"][-1]["api_calls"] == 1
    assert advanced["stage_receipts"][-1]["artifact_path"].endswith(
        "inventory.json"
    )
    assert len(list((run_root / "state_generations").glob("*.json"))) == 3


def test_failed_live_attempt_is_archived_before_checkpoint_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, _initial = _initialize(tmp_path)
    prepared = runner.prepare_current_stage(run_root)
    failed_output = run_root / "stages" / "ch1_b0" / "live"
    failed_output.mkdir(parents=True)
    (failed_output / "experiment_failure.json").write_text(
        json.dumps({"status": "halted_fail_closed"}), encoding="utf-8"
    )

    def fake_b0_live(**kwargs: object) -> dict:
        output_dir = Path(kwargs["output_dir"])
        assert not output_dir.exists()
        output_dir.mkdir(parents=True)
        artifact = {
            "schema_version": "test_inventory_v1",
            "chapter_id": _chapter_ids()[0],
        }
        (output_dir / "inventory.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
        (output_dir / "raw_result.json").write_text(
            json.dumps({"from_cache": False}), encoding="utf-8"
        )
        return artifact

    monkeypatch.setattr(runner, "run_b0_live", fake_b0_live)
    advanced = runner.run_current_live_stage(
        run_root=run_root,
        approved_envelope_hash=prepared["prepared_envelope_hash"],
        gemini_keys_file=tmp_path / "keys.txt",
        gemini_bucket_id="test-bucket",
        openai_key_1=None,
        openai_key_2=None,
    )
    archived = (
        run_root
        / "stages"
        / "ch1_b0"
        / "failed_attempts"
        / "attempt_001"
        / "experiment_failure.json"
    )
    assert archived.is_file()
    assert advanced["current_stage"] == "ch1_local_auditor"


def test_retry_refuses_to_archive_unclassified_live_output(tmp_path: Path) -> None:
    output = tmp_path / "live"
    output.mkdir()
    (output / "raw_result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        runner.TwoChapterRegistryRunError,
        match="without a retryable failure marker",
    ):
        runner._archive_failed_live_attempt(output)


def test_sealed_profile_supplies_ckey_route_without_direct_key_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _chapter_ids()
    run_root = tmp_path / "profile_run"
    runner.initialize_run(
        run_root=run_root,
        document_path=DEFAULT_DOCUMENT,
        design_doc=DEFAULT_DESIGN_DOC,
        frozen_db=DEFAULT_FROZEN_DB,
        first_chapter_id=first,
        second_chapter_id=second,
        provider_profile_path=PROVIDER_PROFILE,
    )
    prepared = runner.prepare_current_stage(run_root)
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    (credential_root / "CKEY.txt").write_text(
        "sk-" + "x" * 64, encoding="utf-8"
    )

    def fake_b0_live(**kwargs: object) -> dict:
        resolved = kwargs["resolved_credential"]
        assert kwargs["keys_file"] is None
        assert resolved.quota_bucket_id == "ckey-account-v1"
        assert resolved.base_url == "https://api.xah.io"
        assert resolved.request_timeout_ms == 120_000
        assert kwargs["model_id"] == "vuduythanh2023/gemini-3.5-flash"
        assert "ckey-account-v1" in kwargs["allowed_quota_bucket_ids"]
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True)
        artifact = {"schema_version": "test_inventory_v1", "chapter_id": first}
        (output_dir / "inventory.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
        (output_dir / "raw_result.json").write_text(
            json.dumps({"from_cache": False}), encoding="utf-8"
        )
        return artifact

    monkeypatch.setattr(runner, "run_b0_live", fake_b0_live)
    advanced = runner.run_current_live_stage(
        run_root=run_root,
        approved_envelope_hash=prepared["prepared_envelope_hash"],
        gemini_keys_file=None,
        gemini_bucket_id=None,
        openai_key_1=None,
        openai_key_2=None,
        credential_root=credential_root,
    )
    assert advanced["current_stage"] == "ch1_local_auditor"


def test_stable_claim_components_get_distinct_dry_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, initial = _initialize(tmp_path)
    forced = runner._transition(
        run_root,
        initial,
        next_stage="stable_claim_components",
        extra={"pending_claim_component_ids": ["cmp_a", "cmp_b"]},
    )

    def fake_envelope(**kwargs: object) -> tuple[dict, dict, dict, dict]:
        component_id = str(kwargs["component_id"])
        envelope_body = {"component_id": component_id}
        envelope = {
            **envelope_body,
            "envelope_hash": runner.canonical_hash(envelope_body),
        }
        return envelope, {"component_id": component_id}, {}, {}

    monkeypatch.setattr(runner, "build_claim_live_envelope", fake_envelope)
    first = runner.prepare_current_stage(run_root)
    assert first["prepared_component_id"] == "cmp_a"
    assert (
        run_root
        / "stages"
        / "stable_claim_components"
        / "dry"
        / "cmp_a"
        / "run_envelope.json"
    ).is_file()

    reset = runner._transition(
        run_root,
        first,
        next_stage="stable_claim_components",
        extra={"pending_claim_component_ids": ["cmp_b"]},
    )
    assert reset["prepared_envelope_hash"] is None
    second = runner.prepare_current_stage(run_root)
    assert second["prepared_component_id"] == "cmp_b"
    assert (
        run_root
        / "stages"
        / "stable_claim_components"
        / "dry"
        / "cmp_b"
        / "run_envelope.json"
    ).is_file()


def test_sealed_document_drift_is_fatal_before_a_stage_runs(tmp_path: Path) -> None:
    document_copy = tmp_path / "document.json"
    document_copy.write_bytes(DEFAULT_DOCUMENT.read_bytes())
    first, second = _chapter_ids()
    run_root = tmp_path / "sealed_drift"
    runner.initialize_run(
        run_root=run_root,
        document_path=document_copy,
        design_doc=DEFAULT_DESIGN_DOC,
        frozen_db=DEFAULT_FROZEN_DB,
        first_chapter_id=first,
        second_chapter_id=second,
    )
    document = json.loads(document_copy.read_text(encoding="utf-8"))
    document["chapters"][0]["blocks"][0]["source_text"] += " changed"
    document_copy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(runner.TwoChapterRegistryRunError, match="document changed"):
        runner.prepare_current_stage(run_root)


def test_sealed_chapter_cycle_profile_drift_is_fatal(tmp_path: Path) -> None:
    profile_copy = tmp_path / "chapter_cycle_profile.json"
    profile_copy.write_bytes(runner.DEFAULT_CHAPTER_CYCLE_PROFILE.read_bytes())
    first, second = _chapter_ids()
    run_root = tmp_path / "profile_drift"
    runner.initialize_run(
        run_root=run_root,
        document_path=DEFAULT_DOCUMENT,
        design_doc=DEFAULT_DESIGN_DOC,
        frozen_db=DEFAULT_FROZEN_DB,
        first_chapter_id=first,
        second_chapter_id=second,
        chapter_cycle_profile_path=profile_copy,
    )
    profile = json.loads(profile_copy.read_text(encoding="utf-8"))
    profile["semantic_leads"]["max_leads_per_chapter"] -= 1
    profile_copy.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(runner.TwoChapterRegistryRunError, match="profile changed"):
        runner.prepare_current_stage(run_root)


def test_semantic_lead_stage_projects_before_identity_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, initial = _initialize(tmp_path)
    paths = runner._paths(run_root)
    paths["prefix_ch2"].parent.mkdir(parents=True, exist_ok=True)
    paths["prefix_ch2"].write_text(json.dumps({"prefix": "raw"}), encoding="utf-8")
    paths["candidate_review_ledger"].write_text(
        json.dumps({"ledger": "base"}), encoding="utf-8"
    )
    runner._transition(run_root, initial, next_stage="semantic_leads")

    lead_index = {"lead_index_hash": "a" * 64, "counts": {"lead_count": 1}}
    semantic_prefix = {"prefix": "semantic"}
    final_review = {"ledger": "semantic"}

    def fake_build(**kwargs: object) -> dict:
        assert kwargs["current_chapter_id"] == _chapter_ids()[1]
        assert kwargs["prefix_bundle"] == {"prefix": "raw"}
        assert kwargs["chapter_cycle_profile"].profile_id == (
            "literary_context_cycle_recommended_v1"
        )
        return lead_index

    def fake_apply(**kwargs: object) -> dict:
        assert kwargs == {
            "prefix_bundle": {"prefix": "raw"},
            "lead_index": lead_index,
        }
        return semantic_prefix

    def fake_append(**kwargs: object) -> dict:
        assert kwargs["ledger"] == {"ledger": "base"}
        assert kwargs["prefix_bundle"] == semantic_prefix
        return final_review

    def fake_prepare(run_root_arg: Path, state: dict) -> dict:
        observed_paths = runner._paths(run_root_arg)
        assert json.loads(
            observed_paths["prefix_ch2_semantic"].read_text(encoding="utf-8")
        ) == semantic_prefix
        assert json.loads(
            observed_paths["candidate_review_ledger_final"].read_text(
                encoding="utf-8"
            )
        ) == final_review
        return runner._transition(
            run_root_arg,
            state,
            next_stage="incremental_identity_components",
        )

    monkeypatch.setattr(
        runner, "build_semantic_candidate_lead_index_from_profile_v1", fake_build
    )
    monkeypatch.setattr(
        runner, "apply_semantic_candidate_leads_to_prefix_v1", fake_apply
    )
    monkeypatch.setattr(
        runner, "append_prefix_identity_uncertainties_v1", fake_append
    )
    monkeypatch.setattr(runner, "_prepare_identity_components", fake_prepare)

    state = runner.advance_code_stages(run_root)
    assert state["current_stage"] == "incremental_identity_components"
    assert json.loads(paths["semantic_lead_index"].read_text(encoding="utf-8")) == (
        lead_index
    )
    assert state["stage_receipts"][-1]["stage"] == "semantic_leads"


def test_candidate_observations_route_to_non_authoritative_review_queues() -> None:
    challenge_body = {
        "schema_version": runner.PRIOR_CHALLENGE_SCHEMA_VERSION,
        "chapter_id": "bk_ch02",
        "candidate_only_observations": [
            {
                "prior_card_id": "card_claim",
                "observation": "new_claim_evidence",
                "disputed_field": "referential_gender",
                "source_block_ids": ["bk_ch02_b001"],
                "reason": "New source evidence exists.",
            },
            {
                "prior_card_id": "card_identity",
                "observation": "possible_collision",
                "disputed_field": None,
                "source_block_ids": ["bk_ch02_b002"],
                "reason": "The current source may describe another referent.",
            },
        ],
    }
    challenge = {
        **challenge_body,
        "prior_challenge_artifact_hash": runner.canonical_hash(challenge_body),
    }
    queue = runner._build_candidate_review_queue(
        index={
            "ticket_index_hash": "a" * 64,
            "state_lineage_id": "b" * 64,
            "identity_referrals": [],
            "uncertainty_rows": [],
        },
        prefix={
            "prefix_identity_uncertainties": [],
            "candidate_only_context_cards": [
                {
                    "prior_card_id": "card_claim",
                    "disputed_claims": [
                        {
                            "disputed_field": "referential_gender",
                            "status": "pending",
                        }
                    ],
                },
                {
                    "prior_card_id": "card_identity",
                    "disputed_claims": [
                        {
                            "disputed_field": "identity_membership",
                            "status": "pending",
                        }
                    ],
                },
            ],
        },
        challenge=challenge,
    )
    assert [
        row["prior_card_id"] for row in queue["candidate_claim_evidence_queue"]
    ] == ["card_claim"]
    assert queue["candidate_claim_evidence_queue"][0][
        "pending_claim_snapshot"
    ]["disputed_field"] == "referential_gender"
    assert [
        row["prior_card_id"] for row in queue["candidate_identity_observations"]
    ] == ["card_identity"]
    assert queue["production_publish_performed"] is False
    assert all(
        len(row["evidence_manifest_hash"]) == 64
        for row in [
            *queue["candidate_claim_evidence_queue"],
            *queue["candidate_identity_observations"],
        ]
    )
    assert len(queue["queue_hash"]) == 64


def test_pending_identity_count_includes_card_level_disputes() -> None:
    prefix = {
        "prefix_identity_uncertainties": [
            {"uncertainty_id": "prefix_uncertainty_1"}
        ],
        "candidate_only_context_cards": [
            {
                "prior_card_id": "card_1",
                "disputed_claims": [
                    {
                        "uncertainty_id": "card_uncertainty_1",
                        "disputed_field": "identity_membership",
                    },
                    {
                        "disputed_field": "identity_summary",
                        "status": "pending",
                    },
                ],
            }
        ],
    }
    assert runner._pending_identity_uncertainty_count(prefix) == 2


def test_stable_claim_index_receives_only_cards_actually_supplied_to_b0() -> None:
    first = {"prior_card_id": "card_first", "canonical_surface": "First"}
    second = {"prior_card_id": "card_second", "canonical_surface": "Second"}
    selected = runner._supplied_claim_cards(
        prefix={"claim_cards": [first, second]},
        challenge={
            "code_derived_prior_presence": [
                {"prior_card_id": "card_second", "current_surface_hits": []}
            ]
        },
    )
    assert selected == [second]
    assert selected[0] is not second
