from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.literary.builder_pilot import RESPONSE_FORMAT_JSON, build_literary_windows
from pipeline.literary.builder_v3_pipeline import _block_lineage, _build_request
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.scripts import run_literary_builder_v3_d0 as d0


@pytest.fixture(scope="module")
def source_document() -> dict:
    document, _ = d0.load_wuthering_heights_epub(d0.DEFAULT_SOURCE)
    return document


@pytest.fixture()
def preflight(tmp_path: Path) -> dict:
    run_id = "d0_test_fixed"
    output_root = tmp_path / "literary_m4f_s5c_slice" / run_id
    return d0.build_d0_preflight(
        output_root=output_root,
        run_id=run_id,
        mini_quota_bucket_id="openai-key-1-mini",
        gpt_quota_bucket_id="openai-key-1-gpt54",
        created_at_utc="2026-07-13T01:02:03Z",
    )


def _budget(manifest: dict, model: str) -> dict:
    return next(row for row in manifest["budget"]["buckets"] if row["model"] == model)


def _observed_row(
    *,
    call_id: str,
    stage: str = "b1",
    model: str = "gpt-5.4-mini",
    cache_status: str = "fresh",
    retries: int = 0,
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
) -> dict:
    return {
        "audit_call_id": call_id,
        "stage": stage,
        "model": model,
        "cache_status": cache_status,
        "technical_retries": retries,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
        },
    }


def test_preflight_recomputes_locked_topology_and_hard_envelope(preflight: dict) -> None:
    topology = preflight["call_topology"]
    assert topology["stage_totals"] == {"b1": 49, "b2": 49, "b3": 4}
    assert "b0" not in d0.PROMPT_IDS
    assert topology["by_chapter"] == {
        "wh_ch01": {"b1": 7, "b2": 7, "b3": 1},
        "wh_ch02": {"b1": 13, "b2": 13, "b3": 1},
        "wh_ch03": {"b1": 17, "b2": 17, "b3": 1},
        "wh_ch04": {"b1": 12, "b2": 12, "b3": 1},
    }
    mini = _budget(preflight, "gpt-5.4-mini")
    gpt = _budget(preflight, "gpt-5.4")
    assert mini["base_hard_reserve"] == 1_513_512
    assert mini["retry_policy"]["worst_boundary_chapter"] == "wh_ch03"
    assert mini["retry_policy"]["max_retries_before_next_enforceable_halt"] == 38
    assert mini["retry_policy"]["technical_retry_reserve"] == 586_872
    assert mini["maximum_spend_before_next_enforceable_halt"] == 2_100_384
    assert gpt["base_hard_reserve"] == 105_152
    assert gpt["retry_policy"]["technical_retry_reserve"] == 26_288
    assert gpt["maximum_spend_before_next_enforceable_halt"] == 131_440
    assert len(preflight["contracts"]["runtime_file_hashes"]) == 7
    assert all(
        len(value) == 64 for value in preflight["contracts"]["runtime_file_hashes"].values()
    )
    assert preflight["approval_allowed"] is True


def test_preflight_renders_only_b1_exact_now_and_fits_prompt_cap(preflight: dict) -> None:
    status = preflight["request_token_status"]
    assert status["exact_now_stages"] == ["b1"]
    assert status["exact_now_call_count"] == 49
    assert set(status["not_yet_renderable"]) == {"b2", "b3"}
    assert status["exact_now_prompt_tokens_total"] == 79_400
    assert status["exact_now_prompt_tokens_max"] == 2_314
    assert status["all_exact_now_prompts_fit_mini_cap"] is True
    assert all(row["rendered_messages"] for row in status["exact_now_rows"])
    assert all(row["replay_status"] == "fresh_required" for row in status["exact_now_rows"])
    assert preflight["cache_reuse_status"] == {
        "policy": "full_request_fingerprint_match_only",
        "prior_with_b0_artifacts_eligible": False,
        "prior_approval_hash_prefix": "94e535",
        "stage_counts": {
            "b1": {
                "cache_hit": 0,
                "checkpoint_restored": 0,
                "fresh_required": 49,
                "reason": "request_contract_version_bumped",
            },
            "b2": {
                "cache_hit": 0,
                "checkpoint_restored": 0,
                "fresh_required": 49,
                "reason": "B0_scene_projection_removed",
            },
            "b3": {
                "cache_hit": 0,
                "checkpoint_restored": 0,
                "fresh_required": 4,
                "reason": "B0_typed_projection_removed",
            },
        },
        "total_cache_hit": 0,
        "total_checkpoint_restored": 0,
        "total_fresh_required": 102,
    }


def _fake_resume_manifest(resume_root: Path, execution_contract_hash: str) -> dict:
    body = {
        "schema_version": d0.M1_RESUME_SCHEMA_VERSION,
        "status": "verified",
        "root": d0._relative_path(resume_root),
        "execution_mode": d0.EXECUTION_MODE_REAL_API,
        "execution_contract_hash": execution_contract_hash,
        "chapters": [
            {
                "chapter_id": chapter_id,
                "checkpoint_hash": f"checkpoint-{chapter_id}",
                "checkpoint_identity_hash": f"identity-{chapter_id}",
                "semantic_state_hash": f"semantic-{chapter_id}",
            }
            for chapter_id in d0.CHAPTER_IDS
        ],
    }
    return {**body, "manifest_hash": canonical_hash(body)}


def test_verified_m1_resume_zeroes_mini_execution_and_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resume_root = tmp_path / "literary_m4f_s5c_slice" / "source"
    monkeypatch.setattr(
        d0,
        "_validated_m1_resume_manifest",
        lambda *, document, resume_root, execution_contract_hash: _fake_resume_manifest(
            resume_root, execution_contract_hash
        ),
    )
    manifest = d0.build_d0_preflight(
        output_root=tmp_path / "literary_m4f_s5c_slice" / "resumed",
        run_id="resumed",
        mini_quota_bucket_id="openai-key-2-mini",
        gpt_quota_bucket_id="openai-key-1-gpt54",
        m1v3_resume_root=resume_root,
        created_at_utc="2026-07-13T01:02:03Z",
    )
    topology = manifest["call_topology"]
    assert topology["stage_totals"] == {"b1": 49, "b2": 49, "b3": 4}
    assert topology["execution_stage_totals"] == {"b1": 0, "b2": 0, "b3": 4}
    assert topology["mini_call_count"] == 0
    assert _budget(manifest, "gpt-5.4-mini")["base_hard_reserve"] == 0
    assert _budget(manifest, "gpt-5.4-mini")[
        "maximum_spend_before_next_enforceable_halt"
    ] == 0
    assert manifest["cache_reuse_status"]["total_checkpoint_restored"] == 98
    assert manifest["cache_reuse_status"]["total_fresh_required"] == 4
    assert all(
        row["replay_status"] == "checkpoint_restored"
        for row in manifest["request_token_status"]["exact_now_rows"]
    )


def test_m1_resume_validation_failure_blocks_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        d0,
        "_load_m1_chain",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("tampered checkpoint chain")),
    )
    with pytest.raises(RuntimeError, match="tampered checkpoint chain"):
        d0.build_d0_preflight(
            output_root=tmp_path / "literary_m4f_s5c_slice" / "blocked",
            run_id="blocked",
            mini_quota_bucket_id="openai-key-2-mini",
            gpt_quota_bucket_id="openai-key-1-gpt54",
            m1v3_resume_root=tmp_path / "literary_m4f_s5c_slice" / "tampered",
        )


def test_preflight_token_render_matches_real_request_shape(source_document: dict) -> None:
    selected = d0.select_chapters(source_document, list(d0.CHAPTER_IDS))
    chapter = selected[0]
    windows = build_literary_windows(chapter, target_tokens=500, max_blocks=8)
    mini = d0.load_llm_config(d0.DEFAULT_MINI_CONFIG)
    prompts, specs = d0._prompt_artifacts(d0.DEFAULT_DESIGN_DOC, mini, d0.load_llm_config(d0.DEFAULT_GPT_CONFIG))
    window = windows[0]
    tails = [
        *[d0._context_block_view(row, "previous") for row in window.previous_tail],
        *[d0._context_block_view(row, "next") for row in window.next_tail],
    ]
    tails.sort(key=lambda row: (row["order_index"], row["block_id"]))
    sections = {
        "active_window_blocks": [d0._block_view(row) for row in window.blocks],
        "context_only_tail": tails,
    }
    request = _build_request(
        stage="b1",
        chapter_id="wh_ch01",
        window_id=str(window.window_id),
        allowlisted_sections=sections,
        lineage_manifest=[
            *_block_lineage(window.blocks, "active_window_block"),
            *_block_lineage(
                [*window.previous_tail, *window.next_tail], "context_only_tail_block"
            ),
        ],
        execution_mode=d0.EXECUTION_MODE_REAL_API,
        real_spec=specs["b1"],
    )
    real_messages = request.body()["rendered_messages"]
    preflight_messages = d0._model_input_messages(
        prompt_text=prompts["b1"],
        stage="b1",
        chapter_id="wh_ch01",
        window_id=str(window.window_id),
        allowlisted_sections=sections,
    )
    assert canonical_json(preflight_messages) == canonical_json(real_messages)
    matching = next(
        row
        for row in d0._renderable_token_rows([chapter], {"wh_ch01": windows}, prompts)
        if row["window_id"] == str(window.window_id)
    )
    assert canonical_json(matching["rendered_messages"]) == canonical_json(real_messages)
    assert estimate_prompt_tokens(preflight_messages, RESPONSE_FORMAT_JSON) == estimate_prompt_tokens(
        real_messages, RESPONSE_FORMAT_JSON
    )


def test_manifest_hash_is_deterministic_and_covers_operator_usage(tmp_path: Path) -> None:
    kwargs = {
        "output_root": tmp_path / "literary_m4f_s5c_slice" / "deterministic",
        "run_id": "deterministic",
        "mini_quota_bucket_id": "openai-key-1-mini",
        "gpt_quota_bucket_id": "openai-key-1-gpt54",
        "created_at_utc": "2026-07-13T01:02:03Z",
    }
    first = d0.build_d0_preflight(**kwargs)
    second = d0.build_d0_preflight(**kwargs)
    assert canonical_json(first) == canonical_json(second)
    changed = d0.build_d0_preflight(**kwargs, mini_used_today=1)
    assert changed["approval_manifest_hash"] != first["approval_manifest_hash"]


def test_preflight_is_append_only(preflight: dict, tmp_path: Path) -> None:
    output_root = tmp_path / "literary_m4f_s5c_slice" / "append_only"
    manifest = deepcopy(preflight)
    manifest["run_id"] = "append_only"
    manifest["output_root"] = d0._relative_path(output_root)
    body = dict(manifest)
    body.pop("approval_manifest_hash")
    manifest["approval_manifest_hash"] = canonical_hash(body)
    path = d0.write_d0_preflight(output_root, manifest)
    assert path.is_file()
    with pytest.raises(FileExistsError):
        d0.write_d0_preflight(output_root, manifest)


def test_stale_approval_halts_before_key_or_client_path(preflight: dict, tmp_path: Path) -> None:
    output_root = Path(preflight["output_root"])
    path = d0.write_d0_preflight(output_root, preflight)
    os.environ.pop("OPENAI_API_KEY_MINI", None)
    os.environ.pop("OPENAI_API_KEY_GPT54", None)
    constructed: list[str] = []
    with pytest.raises(d0.D0ContractError, match="stale user-approved"):
        d0.run_d0_real(
            preflight_path=path,
            supplied_approval_hash="94e535" + "0" * 58,
            client_factory=lambda *_args: constructed.append("client"),
        )
    assert constructed == []


def test_valid_approval_recomputes_current_bytes(preflight: dict, tmp_path: Path) -> None:
    output_root = Path(preflight["output_root"])
    path = d0.write_d0_preflight(output_root, preflight)
    verified = d0.validate_real_run_approval(
        preflight_path=path,
        supplied_approval_hash=preflight["approval_manifest_hash"],
    )
    assert verified["approval_manifest_hash"] == preflight["approval_manifest_hash"]


def test_capability_and_bucket_labels_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(d0.D0ContractError, match="escapes capability"):
        d0.build_d0_preflight(
            output_root=tmp_path / "outside" / "run",
            run_id="run",
            mini_quota_bucket_id="openai-key-1-mini",
            gpt_quota_bucket_id="openai-key-1-gpt54",
        )
    with pytest.raises(d0.D0ContractError, match="credential material"):
        d0.build_d0_preflight(
            output_root=tmp_path / "literary_m4f_s5c_slice" / "run",
            run_id="run",
            mini_quota_bucket_id="s" + "k-secret",
            gpt_quota_bucket_id="openai-key-1-gpt54",
        )


def test_insufficient_declared_headroom_blocks_approval(tmp_path: Path) -> None:
    base = d0.build_d0_preflight(
        output_root=tmp_path / "literary_m4f_s5c_slice" / "base",
        run_id="base",
        mini_quota_bucket_id="openai-key-1-mini",
        gpt_quota_bucket_id="openai-key-1-gpt54",
        created_at_utc="2026-07-13T01:02:03Z",
    )
    mini = _budget(base, "gpt-5.4-mini")
    used = mini["utc_day_internal_cap"] - mini["maximum_spend_before_next_enforceable_halt"] + 1
    manifest = d0.build_d0_preflight(
        output_root=tmp_path / "literary_m4f_s5c_slice" / "headroom",
        run_id="headroom",
        mini_quota_bucket_id="openai-key-1-mini",
        gpt_quota_bucket_id="openai-key-1-gpt54",
        mini_used_today=used,
        created_at_utc="2026-07-13T01:02:03Z",
    )
    assert _budget(manifest, "gpt-5.4-mini")["fits_internal_headroom"] is False
    assert manifest["approval_allowed"] is False


def test_retry_rate_over_ten_percent_halts_and_cache_hits_do_not_dilute(preflight: dict) -> None:
    rows = [
        *[_observed_row(call_id=f"fresh-{index}") for index in range(9)],
        _observed_row(call_id="retry", retries=1),
        *[
            _observed_row(call_id=f"cache-{index}", cache_status="hit")
            for index in range(20)
        ],
    ]
    assert d0._observed_stats(rows)["gpt-5.4-mini"]["fresh_technical_retry_rate"] == 0.1
    d0._enforce_observed_boundary(rows, preflight)
    rows.append(_observed_row(call_id="retry-2", retries=1))
    with pytest.raises(d0.D0ContractError, match="retry rate exceeded"):
        d0._enforce_observed_boundary(rows, preflight)


def test_observed_prompt_and_daily_usage_halt(preflight: dict) -> None:
    with pytest.raises(d0.D0ContractError, match="prompt exceeded cap"):
        d0._enforce_observed_boundary(
            [_observed_row(call_id="too-large", prompt_tokens=9301)], preflight
        )
    mutated = deepcopy(preflight)
    mini = _budget(mutated, "gpt-5.4-mini")
    mini["declared_prompt_plus_completion_used_today"] = mini["utc_day_internal_cap"] - 50
    with pytest.raises(d0.D0ContractError, match="UTC-day cap"):
        d0._enforce_observed_boundary(
            [_observed_row(call_id="daily", prompt_tokens=100, completion_tokens=0)],
            mutated,
        )


def test_fresh_call_count_cannot_exceed_approved_topology(preflight: dict) -> None:
    rows = [
        {**_observed_row(call_id=f"b1-{index}"), "stage": "b1"}
        for index in range(50)
    ]
    with pytest.raises(d0.D0ContractError, match="call count exceeded"):
        d0._enforce_observed_boundary(rows, preflight)


def test_preflight_does_not_construct_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        d0,
        "_openai_transport",
        lambda _key: (_ for _ in ()).throw(AssertionError("transport constructed")),
    )
    manifest = d0.build_d0_preflight(
        output_root=tmp_path / "literary_m4f_s5c_slice" / "zero_api",
        run_id="zero_api",
        mini_quota_bucket_id="openai-key-1-mini",
        gpt_quota_bucket_id="openai-key-1-gpt54",
        created_at_utc="2026-07-13T01:02:03Z",
    )
    assert manifest["zero_api"] is True


def test_phase_a_target_manifest_is_explicitly_pending_by_default(preflight: dict) -> None:
    assert preflight["phase_a_dependency"] == {
        "identity_target_manifest_status": "pending_phase_a_gate",
        "phase_a_target_gate_accepted": False,
        "duplicate_target_universe_logic_forbidden": True,
    }


def test_sealed_generation_is_atomic_idempotent_and_conflict_closed(tmp_path: Path) -> None:
    output_root = tmp_path / "literary_m4f_s5c_slice" / "sealed"
    bundle = {"bundle_manifest_hash": "a" * 64, "payload": [1, 2, 3]}
    book = {"manifest_hash": "book"}
    first = d0._persist_sealed_generation(
        output_root=output_root,
        book_manifest=book,
        bundle=bundle,
        target_manifest=None,
    )
    second = d0._persist_sealed_generation(
        output_root=output_root,
        book_manifest=book,
        bundle=bundle,
        target_manifest=None,
    )
    assert first == second
    assert (first / "seal_manifest.json").is_file()
    with pytest.raises(d0.D0ContractError, match="conflicts"):
        d0._persist_sealed_generation(
            output_root=output_root,
            book_manifest={"manifest_hash": "changed"},
            bundle=bundle,
            target_manifest=None,
        )


def test_real_orchestrator_runs_chapter_prefixes_and_clears_keys(
    monkeypatch: pytest.MonkeyPatch, preflight: dict, tmp_path: Path
) -> None:
    output_root = Path(preflight["output_root"])
    preflight_path = d0.write_d0_preflight(output_root, preflight)
    calls: list[tuple[str, tuple[str, ...]]] = []
    resume_flags: list[bool] = []

    class DummyClient:
        def __init__(self, config, cache_path):
            self.config = config
            self.cache_path = cache_path
            self.max_retries = 0

    class DummyExecutor:
        def __init__(self, clients, *, slice_cache_root):
            self.clients = clients
            self.slice_cache_root = slice_cache_root

    def fake_m1(_document, chapters, **kwargs):
        calls.append(("m1", tuple(chapters)))
        resume_flags.append(bool(kwargs.get("resume")))
        return {"status": "complete"}

    def fake_m2(_document, chapters, **kwargs):
        calls.append(("m2", tuple(chapters)))
        resume_flags.append(bool(kwargs.get("resume")))
        return {"status": "complete"}

    bundle = {
        "state_lineage_id": "lineage",
        "input_identity_manifest_hash": "input",
        "bundle_manifest_hash": "b" * 64,
    }
    monkeypatch.setattr(d0, "RealStageExecutor", DummyExecutor)
    monkeypatch.setattr(d0, "run_m1_v3", fake_m1)
    monkeypatch.setattr(d0, "run_m2_v3", fake_m2)
    monkeypatch.setattr(d0, "assemble_b4_input_bundle", lambda *_a, **_k: bundle)
    monkeypatch.setattr(d0, "verify_b4_input_bundle_identity", lambda _bundle: None)
    monkeypatch.setattr(d0, "_audit_rows", lambda *_a, **_k: [])
    os.environ["OPENAI_API_KEY_MINI"] = "not-a-real-key-mini"
    os.environ["OPENAI_API_KEY_GPT54"] = "not-a-real-key-gpt"
    result = d0.run_d0_real(
        preflight_path=preflight_path,
        supplied_approval_hash=preflight["approval_manifest_hash"],
        client_factory=lambda config, cache, _key: DummyClient(config, cache),
    )
    assert calls == [
        *(('m1', tuple(d0.CHAPTER_IDS[:index])) for index in range(1, 5)),
        *(('m2', tuple(d0.CHAPTER_IDS[:index])) for index in range(1, 5)),
    ]
    assert result["identity_target_manifest_status"] == "pending_phase_a_gate"
    assert "OPENAI_API_KEY_MINI" not in os.environ
    assert "OPENAI_API_KEY_GPT54" not in os.environ
    sealed = output_root / result["sealed_generation"]
    assert (sealed / "b4_input_bundle.json").is_file()
    assert resume_flags == [True] * 8

    # A completed generation is replay-safe and does not touch keys or clients.
    replay = d0.run_d0_real(
        preflight_path=preflight_path,
        supplied_approval_hash=preflight["approval_manifest_hash"],
        client_factory=lambda *_args: (_ for _ in ()).throw(AssertionError("client rebuilt")),
    )
    assert replay == result


def test_m1_resume_skips_m1_and_never_constructs_mini_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resume_root = tmp_path / "literary_m4f_s5c_slice" / "m1-source"
    output_root = tmp_path / "literary_m4f_s5c_slice" / "m2-only"
    monkeypatch.setattr(
        d0,
        "_validated_m1_resume_manifest",
        lambda *, document, resume_root, execution_contract_hash: _fake_resume_manifest(
            resume_root, execution_contract_hash
        ),
    )
    preflight = d0.build_d0_preflight(
        output_root=output_root,
        run_id="m2-only",
        mini_quota_bucket_id="openai-key-2-mini",
        gpt_quota_bucket_id="openai-key-1-gpt54",
        m1v3_resume_root=resume_root,
        created_at_utc="2026-07-13T01:02:03Z",
    )
    preflight_path = d0.write_d0_preflight(output_root, preflight)
    calls: list[tuple[str, tuple[str, ...], Path]] = []
    constructed_models: list[str] = []

    class DummyClient:
        def __init__(self, config, cache_path):
            self.config = config
            self.cache_path = cache_path
            self.max_retries = 0

    class DummyExecutor:
        def __init__(self, clients, *, slice_cache_root):
            assert set(clients) == {"b3"}
            self.clients = clients
            self.slice_cache_root = slice_cache_root

    def fake_m2(_document, chapters, **kwargs):
        calls.append(("m2", tuple(chapters), Path(kwargs["m1v3_dir"])))
        return {"status": "complete"}

    bundle = {
        "state_lineage_id": "lineage",
        "input_identity_manifest_hash": "input",
        "bundle_manifest_hash": "c" * 64,
    }
    monkeypatch.setattr(d0, "RealStageExecutor", DummyExecutor)
    monkeypatch.setattr(
        d0,
        "run_m1_v3",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("M1 reran")),
    )
    monkeypatch.setattr(d0, "run_m2_v3", fake_m2)
    monkeypatch.setattr(d0, "assemble_b4_input_bundle", lambda *_a, **_k: bundle)
    monkeypatch.setattr(d0, "verify_b4_input_bundle_identity", lambda _bundle: None)
    monkeypatch.setattr(d0, "_audit_rows", lambda *_a, **_k: [])
    os.environ.pop("OPENAI_API_KEY_MINI", None)
    os.environ["OPENAI_API_KEY_GPT54"] = "not-a-real-key-gpt"

    def client_factory(config, cache_path, _key):
        constructed_models.append(config.model)
        return DummyClient(config, cache_path)

    result = d0.run_d0_real(
        preflight_path=preflight_path,
        supplied_approval_hash=preflight["approval_manifest_hash"],
        client_factory=client_factory,
    )
    assert constructed_models == ["gpt-5.4"]
    assert calls == [
        ("m2", tuple(d0.CHAPTER_IDS[:index]), resume_root.resolve())
        for index in range(1, 5)
    ]
    assert result["m1_resume_source"]["manifest_hash"] == preflight[
        "m1_resume_source"
    ]["manifest_hash"]
    assert "OPENAI_API_KEY_MINI" not in os.environ
    assert "OPENAI_API_KEY_GPT54" not in os.environ
