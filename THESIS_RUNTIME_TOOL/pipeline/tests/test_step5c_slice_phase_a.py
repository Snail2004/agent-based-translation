from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import ValidationReport, load_system_prompt_from_design
from pipeline.literary.builder_validators_v3 import ValidationResult
from pipeline.literary.builder_v3_pipeline import (
    EXECUTION_MODE_REAL_API,
    EXECUTION_MODE_SYNTHETIC,
    RealStageExecutor,
    RealStageSpec,
    SyntheticStageExecutor,
    V3RunHalt,
    _AuditSession,
    _build_request,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.step5_frame import build_frame_selection_universe
from pipeline.literary.step5c_slice import (
    IDENTITY_PROPOSAL_PROMPT_ID,
    IDENTITY_RETRIEVAL_PROMPT_ID,
    QuotaBucket,
    SliceContractError,
    assert_book_neutral,
    assert_slice_output_root,
    build_lightweight_roster,
    build_proposal_request_payload,
    build_public_heldout_manifest,
    build_retrieval_request_payload,
    build_untrusted_frame_proposal,
    classify_attribution,
    context_only_pair_diff,
    frame_raw_agreement,
    gate_budget,
    load_slice_prompt,
    private_label_commitment,
    prompt_manifest,
    run_recorded_identity_batch,
    score_extraction_presence,
    split_target_batches,
    validate_proposal_response,
    validate_retrieval_response,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _anchor(text: str, surface: str, block_id: str) -> dict[str, Any]:
    start = text.index(surface)
    return {"block_id": block_id, "char_start": start, "char_end": start + len(surface)}


def _seal_bundle(body: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(body)
    value.pop("bundle_manifest_hash", None)
    value["bundle_manifest_hash"] = canonical_hash(value)
    return value


def _bundle() -> dict[str, Any]:
    b1, b2, b3 = "bk_ch01_b001", "bk_ch01_b002", "bk_ch01_b003"
    text1 = "Aster opened the notebook and introduced Rowan as the diarist."
    text2 = "Before the entry, rain marked the glass. Entry begins now. I record the storm."
    text3 = "The entry ended. Aster closed the notebook."
    aster = {
        "occurrence_id": "m_bk_ch01_b001_01",
        "occurrence_kind": "mention",
        "surface": "Aster",
        "referent_kind_claim": "person",
        "chapter_id": "bk_ch01",
        "block_id": b1,
        "anchor": _anchor(text1, "Aster", b1),
        "context_universe": {
            "active_block": {"block_id": b1},
            "scene_block_candidates": [{"block_id": b1}],
        },
    }
    rowan = {
        "occurrence_id": "m_bk_ch01_b001_02",
        "occurrence_kind": "mention",
        "surface": "Rowan",
        "referent_kind_claim": "person",
        "chapter_id": "bk_ch01",
        "block_id": b1,
        "anchor": _anchor(text1, "Rowan", b1),
        "context_universe": {
            "active_block": {"block_id": b1},
            "scene_block_candidates": [{"block_id": b1}],
        },
    }
    narrator = {
        "occurrence_id": "ep_bk_ch01_b002_01",
        "occurrence_kind": "endpoint",
        "surface": "I",
        "referent_kind_claim": "person",
        "runtime_eligibility": "discourse_only",
        "chapter_id": "bk_ch01",
        "block_id": b2,
        "anchor": _anchor(text2, "I", b2),
        "context_universe": {
            "active_block": {"block_id": b2},
            "scene_block_candidates": [{"block_id": b2}],
        },
    }
    evidence = {
        "ground_item_id": "g_relation_aster",
        "chapter_id": "bk_ch01",
        "payload": {"event_type": "introduces", "actor_surface": "Aster"},
        "evidence_refs": [{"ref_kind": "block", "ref_id": b1}],
    }
    body = {
        "schema_version": "literary_b4_input_bundle_v3",
        "handoff_contract_version": "literary_b4_handoff_contract_v3",
        "state_lineage_id": "lineage-s5c",
        "unit_manifest": [
            {
                "unit_id": "bk_ch01",
                "block_range": [b1, b3],
                "parent_chapter": "bk_ch01",
                "cut_reason": "author_chapter",
                "source_hash": "source-1",
                "m1_checkpoint_refs": ["m1-1"],
            }
        ],
        "source_block_catalog": [
            {"chapter_id": "bk_ch01", "block_id": b1, "order_index": 1, "block_type": "paragraph", "text": text1},
            {"chapter_id": "bk_ch01", "block_id": b2, "order_index": 2, "block_type": "paragraph", "text": text2},
            {"chapter_id": "bk_ch01", "block_id": b3, "order_index": 3, "block_type": "paragraph", "text": text3},
        ],
        "occurrence_cards": [aster, rowan, narrator],
        "occurrence_routing": {
            "person_occurrences": [aster, rowan],
            "non_person_occurrences": [],
            "discourse_only": [narrator],
            "deferred": [],
            "invalid_flagged": [],
            "counts": {"total": 3},
        },
        "ground_evidence": {
            "relation_event_inputs": [evidence],
            "frame_claim_inputs": [],
            "frame_leaf_index": [],
        },
    }
    return _seal_bundle(body)


def _frame_response(*, shifted: bool = False) -> dict[str, Any]:
    child_boundary = (
        {"anchor_text": "I record the storm.", "evidence_quote": "Entry begins now. I record the storm."}
        if shifted
        else {"anchor_text": "Entry begins now.", "evidence_quote": "Entry begins now. I record the storm."}
    )
    return {
        "response_status": "proposed",
        "segments": [
            {
                "local_segment_key": "outer",
                "parent_local_key": None,
                "block_range": ["bk_ch01_b001", "bk_ch01_b003"],
                "start_boundary": None,
                "end_boundary": None,
                "frame_kind": "primary_narration",
                "story_time_label": "frame_present",
                "narrator_occurrence_ref": "m_bk_ch01_b001_01",
                "narrator_surface": "Aster",
                "evidence": [{"block_id": "bk_ch01_b001", "evidence_quote": "Aster opened the notebook and introduced Rowan as the diarist."}],
            },
            {
                "local_segment_key": "diary",
                "parent_local_key": "outer",
                "block_range": ["bk_ch01_b002", "bk_ch01_b002"],
                "start_boundary": child_boundary,
                "end_boundary": None,
                "frame_kind": "diary",
                "story_time_label": "retrospective_past",
                "narrator_occurrence_ref": "ep_bk_ch01_b002_01",
                "narrator_surface": "I",
                "evidence": [{"block_id": "bk_ch01_b002", "evidence_quote": "I record the storm."}],
            },
        ],
    }


def _identity_payloads() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    bundle = _bundle()
    selection = build_frame_selection_universe(bundle, unit_id="bk_ch01")
    frame = build_untrusted_frame_proposal(_frame_response(), selection=selection)
    retrieval_request = build_retrieval_request_payload(
        bundle,
        target_occurrence_ids=["m_bk_ch01_b001_01"],
        frame=frame,
    )
    retrieval = validate_retrieval_response(
        {
            "targets": [
                {
                    "target_occurrence_id": "m_bk_ch01_b001_01",
                    "candidate_occurrence_ids": ["m_bk_ch01_b001_02"],
                    "status": "selected",
                    "evidence_refs": ["g_relation_aster"],
                }
            ]
        },
        request_payload=retrieval_request,
    )
    proposal_request = build_proposal_request_payload(bundle, retrieval=retrieval, frame=frame)
    return retrieval_request, retrieval, proposal_request


def _source_universe() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chapter in range(1, 5):
        for ordinal in range(1, 10):
            kind = "endpoint" if ordinal >= 7 else "mention"
            mention_type = "name" if ordinal <= 3 else "descriptor"
            rows.append(
                {
                    "occurrence_id": f"o_ch{chapter}_{ordinal}",
                    "chapter_id": f"bk_ch{chapter:02d}",
                    "block_id": f"bk_ch{chapter:02d}_b{ordinal:03d}",
                    "occurrence_kind": kind,
                    "mention_type": mention_type,
                    "source_anchor": {"block_id": f"bk_ch{chapter:02d}_b{ordinal:03d}", "char_start": ordinal, "char_end": ordinal + 1},
                }
            )
    return rows


def _config() -> LLMConfig:
    return LLMConfig(model="gpt-5.4-mini-2026-06-01", max_output_tokens=64)


class _Usage:
    prompt_tokens = 12
    cached_tokens = 3
    completion_tokens = 4
    reasoning_tokens = 1


class _FakeClient:
    def __init__(
        self,
        *,
        cache_path: Path,
        failures: int = 0,
        model: str | None = None,
    ) -> None:
        self.config = _config()
        self.max_retries = 0
        self.cache_path = cache_path
        self.failures = failures
        self.calls: list[dict[str, Any]] = []
        self.model = model or self.config.model

    def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": deepcopy(messages), **deepcopy(kwargs)})
        if len(self.calls) <= self.failures:
            raise RuntimeError("transient")
        return SimpleNamespace(
            parsed_json={"chapter_id": "bk_ch01"},
            text='{"chapter_id":"bk_ch01"}',
            usage=_Usage(),
            from_cache=False,
            model=self.model,
            system_fingerprint="fp",
            cache_key="slice-cache-key",
            latency_ms=1,
            cost_usd=0.001,
        )


def _real_request() -> tuple[Any, RealStageSpec]:
    prompt = load_slice_prompt(DESIGN_DOC, "literary_lexicon_v3")
    spec = RealStageSpec.create(stage="b1", prompt_id="literary_lexicon_v3", prompt_text=prompt, config=_config())
    request = _build_request(
        stage="b1",
        chapter_id="bk_ch01",
        window_id="w_bk_ch01_01",
        allowlisted_sections={"active_window_blocks": [], "context_only_tail": []},
        lineage_manifest=[],
        execution_mode=EXECUTION_MODE_REAL_API,
        real_spec=spec,
    )
    return request, spec


def _real_executor(request: Any, client: _FakeClient | None = None) -> tuple[RealStageExecutor, _FakeClient]:
    root = Path("D:/temp/literary_m4f_s5c_slice/test-cache")
    actual = client or _FakeClient(
        cache_path=root / f"b1_{request.body()['model_config_hash'][:16]}.sqlite3"
    )
    return RealStageExecutor({"b1": actual}, slice_cache_root=root), actual


def test_probe_01_v3_prompts_conform_and_v2_drift_is_not_reused() -> None:
    prompts = {name: load_slice_prompt(DESIGN_DOC, name) for name in [
        "literary_lexicon_v3", "literary_narrative_v3", "literary_digest_v3"
    ]}
    assert "Do not emit mention_id" in prompts["literary_lexicon_v3"]
    assert "Do not emit turn_id" in prompts["literary_narrative_v3"]
    assert '"motifs"' in prompts["literary_digest_v3"]
    stale = load_system_prompt_from_design(DESIGN_DOC, "literary_lexicon_v2")
    assert "mention_id" in stale and "Do not emit mention_id" not in stale


def test_probe_02_prompt_bytes_hash_and_book_neutrality() -> None:
    manifest = prompt_manifest(DESIGN_DOC)
    assert len(manifest["prompts"]) == 5
    text = load_slice_prompt(DESIGN_DOC, IDENTITY_RETRIEVAL_PROMPT_ID)
    assert sha256(text.encode()).hexdigest() == next(row["sha256"] for row in manifest["prompts"] if row["prompt_id"] == IDENTITY_RETRIEVAL_PROMPT_ID)
    assert_book_neutral([load_slice_prompt(DESIGN_DOC, row["prompt_id"]) for row in manifest["prompts"]], ["Heathcliff", "Catherine", "Jabez"])
    with pytest.raises(SliceContractError):
        assert_book_neutral([text + " Heathcliff"], ["Heathcliff"])


def test_probe_03_synthetic_flag_keeps_legacy_request_shape() -> None:
    request = _build_request(stage="b1", chapter_id="bk_ch01", window_id="w_bk_ch01_01", allowlisted_sections={"active_window_blocks": [], "context_only_tail": []}, lineage_manifest=[], execution_mode=EXECUTION_MODE_SYNTHETIC)
    body = request.body()
    assert "rendered_messages" not in body and "model_config_hash" not in body
    with pytest.raises(ValueError):
        _build_request(stage="b1", chapter_id="bk_ch01", window_id="w_bk_ch01_01", allowlisted_sections={"active_window_blocks": [], "context_only_tail": []}, lineage_manifest=[], execution_mode=EXECUTION_MODE_REAL_API)
    with pytest.raises(ValueError, match="unsupported Builder-v3 request stage"):
        _build_request(stage="b0", chapter_id="bk_ch01", window_id=None, allowlisted_sections={"chapter_blocks": []}, lineage_manifest=[], execution_mode=EXECUTION_MODE_SYNTHETIC)


def test_probe_04_real_request_identity_rejects_prompt_and_config_drift() -> None:
    request, spec = _real_request()
    changed = RealStageSpec.create(stage="b1", prompt_id=spec.prompt_id, prompt_text=spec.prompt_text + "\n", config=_config())
    assert changed.prompt_sha256 != spec.prompt_sha256
    body = request.body()
    body["model_config_hash"] = "tampered"
    with pytest.raises(ValueError):
        _real_executor(request)[0].execute(type(request)(request.canonical_request_json, canonical_hash(body)), attempt_no=1)


def test_probe_05_real_executor_sends_persisted_messages_and_rejects_tamper() -> None:
    request, _ = _real_request()
    executor, client = _real_executor(request)
    result = executor.execute(request, attempt_no=1)
    assert client.calls[0]["messages"] == request.body()["rendered_messages"]
    assert result.raw_payload == {"chapter_id": "bk_ch01"}
    with pytest.raises(ValueError):
        executor.execute(type(request)(request.canonical_request_json, "bad"), attempt_no=1)


def test_probe_06_real_usage_and_model_metadata_are_audit_checked(tmp_path: Path) -> None:
    request, _ = _real_request()
    audit = _AuditSession.create(tmp_path)
    report = ValidationReport("test", True, [], [], {})
    payload, _, ref, _ = audit.execute(
        request=request,
        executor=_real_executor(request)[0],
        validator=lambda value: ValidationResult(dict(value), report),
        stage="b1", chapter_id="bk_ch01", window_id="w_bk_ch01_01",
    )
    assert payload["chapter_id"] == "bk_ch01"
    assert ref["usage"]["prompt_tokens"] == 12 and ref["usage"]["completion_tokens"] == 4
    bad_audit = _AuditSession.create(tmp_path / "bad")
    with pytest.raises(V3RunHalt):
        bad = _FakeClient(cache_path=Path("D:/temp/literary_m4f_s5c_slice/test-cache") / f"b1_{request.body()['model_config_hash'][:16]}.sqlite3", model="wrong")
        bad_audit.execute(request=request, executor=_real_executor(request, bad)[0], validator=lambda value: ValidationResult(dict(value), report), stage="b1", chapter_id="bk_ch01", window_id="w_bk_ch01_01")


def test_probe_07_one_transport_retry_and_no_semantic_retry(tmp_path: Path) -> None:
    request, _ = _real_request()
    client = _FakeClient(cache_path=Path("D:/temp/literary_m4f_s5c_slice/test-cache") / f"b1_{request.body()['model_config_hash'][:16]}.sqlite3", failures=1)
    result = _real_executor(request, client)[0].execute(request, attempt_no=1)
    assert len(client.calls) == 2 and client.calls[1]["bypass_cache"] is True
    audit = _AuditSession.create(tmp_path)
    rejected = ValidationReport("test", False, ["semantic"], [], {})
    with pytest.raises(V3RunHalt):
        audit.execute(request=request, executor=_real_executor(request)[0], validator=lambda value: ValidationResult(dict(value), rejected), stage="b1", chapter_id="bk_ch01", window_id="w_bk_ch01_01")


def test_probe_08_slice_root_and_proposal_only_import_boundary() -> None:
    root = RUNTIME_ROOT / "data" / "reports"
    assert_slice_output_root(root / "literary_m4f_s5c_slice" / "run-a", reports_root=root)
    with pytest.raises(SliceContractError):
        assert_slice_output_root(root / "literary_m4d_b4v2", reports_root=root)
    request, _ = _real_request()
    escaped = _FakeClient(
        cache_path=Path("D:/temp/legacy-cache")
        / f"b1_{request.body()['model_config_hash'][:16]}.sqlite3"
    )
    with pytest.raises(ValueError):
        RealStageExecutor(
            {"b1": escaped},
            slice_cache_root=Path("D:/temp/literary_m4f_s5c_slice/test-cache"),
        )
    source = (RUNTIME_ROOT / "pipeline" / "literary" / "step5c_slice.py").read_text(encoding="utf-8")
    for forbidden in ("promote(", "Step5Store", "quarantine_closure", "overlay_apply"):
        assert forbidden not in source


def test_probe_09_heldout_manifest_is_deterministic_label_free_and_committed() -> None:
    first = build_public_heldout_manifest(_source_universe(), seed="seed-1")
    second = build_public_heldout_manifest(_source_universe(), seed="seed-1")
    assert first == second and len(first["targets"]) == 24
    assert "expected" not in str(first).lower()
    changed = build_public_heldout_manifest(_source_universe(), seed="seed-2")
    assert changed["manifest_hash"] != first["manifest_hash"]


def test_probe_10_missing_heldout_occurrence_stays_in_denominator() -> None:
    manifest = build_public_heldout_manifest(_source_universe(), seed="seed")
    extracted = {row["occurrence_id"] for row in manifest["targets"][1:]}
    scored = score_extraction_presence(manifest, extracted)
    assert len(scored) == 24 and scored[0]["outcome"] == "upstream_occurrence_missing"


def test_probe_11_retrieval_exact_cover_and_complete_roster() -> None:
    request, retrieval, _ = _identity_payloads()
    assert len(request["global_light_roster"]) == 3
    assert retrieval["targets"][0]["candidate_occurrence_ids"] == ["m_bk_ch01_b001_02"]
    bad = {"targets": []}
    with pytest.raises(SliceContractError):
        validate_retrieval_response(bad, request_payload=request)
    foreign = {"targets": [{"target_occurrence_id": "m_bk_ch01_b001_01", "candidate_occurrence_ids": ["foreign"], "status": "selected", "evidence_refs": []}]}
    with pytest.raises(SliceContractError):
        validate_retrieval_response(foreign, request_payload=request)
    retired = _bundle()
    retired["ground_evidence"]["cast_claim_inputs"] = []
    retired = _seal_bundle(retired)
    with pytest.raises(SliceContractError, match="retired cast_claim_inputs"):
        build_retrieval_request_payload(
            retired,
            target_occurrence_ids=["m_bk_ch01_b001_01"],
            frame=None,
        )


def test_probe_12_proposal_exact_cover_refs_and_no_semantic_ids() -> None:
    _, _, request = _identity_payloads()
    good = {"proposals": [{"target_occurrence_id": "m_bk_ch01_b001_01", "status": "proposed", "same_referent_occurrence_ids": ["m_bk_ch01_b001_01"], "different_referent_occurrence_ids": ["m_bk_ch01_b001_02"], "referent_kind": "person", "canonical_surface_guess": "Aster", "evidence_refs": ["g_relation_aster"]}]}
    assert validate_proposal_response(good, request_payload=request)["proposals"]
    bad = deepcopy(good); bad["proposals"][0]["entity_id"] = "ent_aster"
    with pytest.raises(SliceContractError):
        validate_proposal_response(bad, request_payload=request)


def test_probe_13_batching_only_splits_targets_and_halts_single_overcap() -> None:
    shared = {"roster": list(range(100))}
    render = lambda ids: {**shared, "targets": list(ids)}
    batches = split_target_batches(["a", "b", "c"], render_payload=render, prompt_token_cap=1, estimate_tokens=lambda payload: len(payload["targets"]))
    assert batches == [["a"], ["b"], ["c"]]
    assert all(render(batch)["roster"] == shared["roster"] for batch in batches)
    with pytest.raises(SliceContractError):
        split_target_batches(["a"], render_payload=render, prompt_token_cap=0)


def test_probe_14_frame_adapter_is_validated_untrusted_and_read_only() -> None:
    bundle = _bundle(); selection = build_frame_selection_universe(bundle, unit_id="bk_ch01")
    frame = build_untrusted_frame_proposal(_frame_response(), selection=selection)
    assert frame.trust == "untrusted" and frame.response_status == "proposed"
    assert "generation" not in frame.payload() and "active" not in frame.payload()
    bad = _frame_response(); bad["segments"][0]["frame_kind"] = "foreign"
    with pytest.raises(Exception):
        build_untrusted_frame_proposal(bad, selection=selection)


def test_probe_15_frame_metrics_are_raw_unqualified_only() -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    primary = build_untrusted_frame_proposal(_frame_response(), selection=selection)
    shifted = build_untrusted_frame_proposal(_frame_response(shifted=True), selection=selection)
    metrics = frame_raw_agreement(primary, shifted)
    assert metrics["qualification_status"] == "unqualified"
    assert metrics["raw_unqualified_frame_exact_agreement"] is False
    assert metrics["raw_unqualified_frame_relaxed_agreement"] is True
    assert "verified_frame_coverage" not in metrics


def test_probe_16_context_only_pair_diff_rejects_noncontext_drift() -> None:
    base = {"prompt_sha256": "p", "model_config_hash": "m", "target_ids": ["a"], "output_schema_hash": "s", "context_sections": {"hints": [1], "source": [2]}}
    arm = deepcopy(base); arm["context_sections"]["hints"] = []
    assert context_only_pair_diff(base, arm, allowed_context_sections={"hints"})["changed_context_sections"] == ["hints"]
    arm["model_config_hash"] = "changed"
    with pytest.raises(SliceContractError):
        context_only_pair_diff(base, arm, allowed_context_sections={"hints"})


def test_probe_17_model_error_requires_every_delivery_gate() -> None:
    kwargs = dict(outcome="wrong", evidence_delivered=True, known_noise_absent=True, retrieval_delivered=True, prompt_gate_passed=True, validator_preserved_response=True)
    assert classify_attribution(**kwargs) == "model_error"
    kwargs["evidence_delivered"] = False
    assert classify_attribution(**kwargs) == "context_missing"


def test_probe_18_upstream_frame_error_requires_preregistered_counterfactual() -> None:
    base = dict(outcome="wrong", evidence_delivered=True, known_noise_absent=True, retrieval_delivered=True, prompt_gate_passed=True, validator_preserved_response=True, frame_load_bearing=True, frame_reference_agreed=True, delivered_frame_wrong=True)
    assert classify_attribution(**base, no_frame_pair_flipped_correct=False) != "upstream_frame_error"
    assert classify_attribution(**base, no_frame_pair_flipped_correct=True) == "upstream_frame_error"


def test_probe_19_private_labels_are_commitments_not_request_fields() -> None:
    labels = {"o_ch1_1": {"referent": "person-a", "frame_dependency": "load_bearing"}}
    commitment = private_label_commitment(labels, reviewer="codex")
    assert commitment == private_label_commitment(labels, reviewer="codex")
    manifest = build_public_heldout_manifest(_source_universe(), seed="seed")
    assert commitment not in str(manifest) and "person-a" not in str(manifest)


def test_probe_20_budget_uses_utc_prompt_plus_completion_and_rpd() -> None:
    bucket = QuotaBucket("gpt54-key-1", "2026-07-13", 10, 20, 100, used_prompt_tokens=20, used_completion_tokens=10, used_requests=2)
    result = gate_budget(bucket, next_prompt_tokens=10, next_completion_reserve=10, remaining_token_reserve=20, remaining_call_reserve=2)
    assert result["required_tokens_with_reserve"] == 70
    with pytest.raises(SliceContractError):
        gate_budget(bucket, next_prompt_tokens=50, next_completion_reserve=30, remaining_token_reserve=0, remaining_call_reserve=0)


def test_probe_21_recorded_real_shape_crosses_persist_validate_without_api(tmp_path: Path) -> None:
    bundle = _bundle()
    selection = build_frame_selection_universe(bundle, unit_id="bk_ch01")
    frame = build_untrusted_frame_proposal(_frame_response(), selection=selection)
    seen_messages: list[list[dict[str, Any]]] = []

    def callback(messages: list[dict[str, Any]], meta: Mapping[str, Any], bypass: bool) -> Mapping[str, Any]:
        assert bypass is False
        seen_messages.append(deepcopy(messages))
        role = __import__("json").loads(messages[1]["content"])["role"]
        response: dict[str, Any]
        if role == "retrieval":
            response = {"targets": [{"target_occurrence_id": "m_bk_ch01_b001_01", "candidate_occurrence_ids": ["m_bk_ch01_b001_02"], "status": "selected", "evidence_refs": ["g_relation_aster"]}]}
        else:
            response = {"proposals": [{"target_occurrence_id": "m_bk_ch01_b001_01", "status": "proposed", "same_referent_occurrence_ids": ["m_bk_ch01_b001_01"], "different_referent_occurrence_ids": ["m_bk_ch01_b001_02"], "referent_kind": "person", "canonical_surface_guess": "Aster", "evidence_refs": ["g_relation_aster"]}]}
        return {"response": response, "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cached_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0}, "provider": "recorded-provider", "model": "recorded-model", "cache_key": f"recorded-{role}"}

    reports_root = tmp_path / "reports"
    out_dir = reports_root / "literary_m4f_s5c_slice" / "recorded-run"
    artifact = run_recorded_identity_batch(
        bundle,
        target_occurrence_ids=["m_bk_ch01_b001_01"],
        frame=frame,
        retrieval_prompt=load_slice_prompt(DESIGN_DOC, IDENTITY_RETRIEVAL_PROMPT_ID),
        proposal_prompt=load_slice_prompt(DESIGN_DOC, IDENTITY_PROPOSAL_PROMPT_ID),
        provider="recorded-provider",
        model_config={"model": "recorded-model", "max_output_tokens": 128},
        request_llm=callback,
        out_dir=out_dir,
        reports_root=reports_root,
    )
    assert artifact["proposal_only"] is True
    assert artifact["target_results"][0]["slice_status"] == "proposed"
    assert artifact["usage"] == {"prompt_tokens": 20, "completion_tokens": 6}
    assert len(seen_messages) == 2
    request_files = sorted(out_dir.glob("identity_calls/*/request.json"))
    assert len(request_files) == 2
    persisted = [__import__("json").loads(path.read_text(encoding="utf-8"))["rendered_messages"] for path in request_files]
    assert all(messages in persisted for messages in seen_messages)


def test_probe_22_existing_foundation_is_not_imported_or_mutated() -> None:
    source = (RUNTIME_ROOT / "pipeline" / "literary" / "step5c_slice.py").read_text(encoding="utf-8")
    assert "step5_authority" not in source and "step5_store" not in source
    frozen = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
    assert frozen.is_file()
