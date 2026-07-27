from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, cast

import pytest

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.step5_authority import validate_blinding
from pipeline.literary.step5_frame import (
    ADJUDICATOR_PROMPT_ID,
    CHECKER_PROMPT_ID,
    FrameCallConfig,
    FrameContractError,
    build_frame_call_plan,
    build_frame_selection_universe,
    checker_manifest_from_persisted_lineage,
    deepest_frame_at,
    load_confirmed_frame_view,
    persist_frame_request,
    render_frame_request,
    run_frame_confirmation,
    validate_frame_response,
    verify_confirmed_frame_view,
)
from pipeline.literary.step5_store import StaleParentError, Step5Store
from pipeline.literary.step5_types import (
    AuthorityIndependencePolicy,
    BlindingValidationRecord,
    MetricRatio,
    MetricThreshold,
    ModelAuthorityRoute,
    ModelIdentity,
    QualificationBinding,
    QualificationManifest,
    with_content_address,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


class DesignPromptProvider:
    def load_prompt(self, prompt_id: str) -> str:
        return load_system_prompt_from_design(DESIGN_DOC, prompt_id)


class ScriptedExecutor:
    def __init__(self, responses: Mapping[str, Mapping[str, Any]]) -> None:
        self.responses = {key: deepcopy(dict(value)) for key, value in responses.items()}
        self.requests: list[dict[str, Any]] = []

    def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        copied = deepcopy(dict(request))
        self.requests.append(copied)
        role = str((copied.get("metadata") or {}).get("role") or "")
        return {
            "response": deepcopy(self.responses[role]),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }


def _seal(value: Any) -> Any:
    return with_content_address(value)


def _model(family: str, model_id: str) -> ModelIdentity:
    return ModelIdentity(
        provider=f"provider-{family}",
        model_family_lineage_id=family,
        model_id=model_id,
        weights_version="weights-1",
    )


def _authority_fixture(
    *, checker_family: str = "family-b", qualification_kind: str = "frame"
) -> tuple[
    AuthorityIndependencePolicy,
    ModelAuthorityRoute,
    ModelAuthorityRoute,
    dict[str, QualificationManifest],
]:
    policy = cast(
        AuthorityIndependencePolicy,
        _seal(
            AuthorityIndependencePolicy(
                policy_version="authority-v1",
                qualification_policy_hash="qualification-policy-v1",
            )
        ),
    )
    checker_model = _model(checker_family, "checker-model")
    metric = MetricRatio(metric_id="frame_accuracy", numerator=1, denominator=1)
    threshold = MetricThreshold(
        metric_id="frame_accuracy",
        comparator=">=",
        threshold_numerator=1,
        threshold_denominator=1,
    )
    manifest = cast(
        QualificationManifest,
        _seal(
            QualificationManifest(
                model_identity=checker_model,
                decision_kind=cast(Any, qualification_kind),
                oracle_split_manifest_hash="oracle-split",
                qualify_group_ids=frozenset({"frame-group"}),
                independence_policy_hash=policy.policy_hash,
                qualification_policy_hash=policy.qualification_policy_hash,
                metric_schema_version="frame-metrics-v1",
                measured_metrics=(metric,),
                thresholds=(threshold,),
                qualified_result=True,
            )
        ),
    )
    adjudicator = ModelAuthorityRoute(
        authority_route_id="frame-adjudicator",
        role="adjudicator",
        model_identity=_model("family-a", "adjudicator-model"),
    )
    checker = ModelAuthorityRoute(
        authority_route_id="frame-checker",
        role="checker",
        model_identity=checker_model,
        qualification_bindings=(
            QualificationBinding(
                decision_kind=cast(Any, qualification_kind),
                qualification_manifest_hash=manifest.qualification_manifest_hash,
            ),
        ),
    )
    return (
        policy,
        adjudicator,
        checker,
        {manifest.qualification_manifest_hash: manifest},
    )


def _configs(
    adjudicator: ModelAuthorityRoute, checker: ModelAuthorityRoute
) -> tuple[FrameCallConfig, FrameCallConfig]:
    return (
        FrameCallConfig(
            route=adjudicator,
            reasoning_effort="none",
            verbosity="low",
            max_output_tokens=4096,
            prompt_token_cap=14000,
            execution_cost_class="local_compute",
            quota_bucket_id=None,
        ),
        FrameCallConfig(
            route=checker,
            reasoning_effort="none",
            verbosity="low",
            max_output_tokens=4096,
            prompt_token_cap=14000,
            execution_cost_class="local_compute",
            quota_bucket_id=None,
        ),
    )


def _anchor(text: str, surface: str, block_id: str) -> dict[str, Any]:
    start = text.index(surface)
    return {"block_id": block_id, "char_start": start, "char_end": start + len(surface)}


def _seal_bundle(body: dict[str, Any]) -> dict[str, Any]:
    copied = deepcopy(body)
    copied.pop("bundle_manifest_hash", None)
    copied["bundle_manifest_hash"] = canonical_hash(copied)
    return copied


def _bundle() -> dict[str, Any]:
    b0 = "bk_ch01_b000"
    b1 = "bk_ch01_b001"
    b2 = "bk_ch01_b002"
    b3 = "bk_ch01_b003"
    future = "bk_ch02_b001"
    text1 = "Aster opened the notebook and introduced Rowan as the diarist."
    text2 = "Before the entry, rain marked the glass. Entry begins now. I record the storm."
    text3 = "The entry ended. Aster closed the notebook."
    future_text = "FUTURE_FRAME_SECRET: a later narrator reveals another identity."
    source = [
        {
            "chapter_id": "bk_ch01",
            "block_id": b0,
            "order_index": 0,
            "block_type": "heading",
            "text": "Chapter One",
        },
        {
            "chapter_id": "bk_ch01",
            "block_id": b1,
            "order_index": 1,
            "block_type": "paragraph",
            "text": text1,
        },
        {
            "chapter_id": "bk_ch01",
            "block_id": b2,
            "order_index": 2,
            "block_type": "paragraph",
            "text": text2,
        },
        {
            "chapter_id": "bk_ch01",
            "block_id": b3,
            "order_index": 3,
            "block_type": "paragraph",
            "text": text3,
        },
        {
            "chapter_id": "bk_ch02",
            "block_id": future,
            "order_index": 4,
            "block_type": "paragraph",
            "text": future_text,
        },
    ]
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
            "scene_block_candidates": [{"block_id": b1}, {"block_id": b2}],
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
            "scene_block_candidates": [{"block_id": b1}, {"block_id": b2}],
        },
    }
    future_card = {
        "occurrence_id": "m_bk_ch02_b001_01",
        "occurrence_kind": "mention",
        "surface": "Mira",
        "referent_kind_claim": "person",
        "chapter_id": "bk_ch02",
        "block_id": future,
        "anchor": {"block_id": future, "char_start": 0, "char_end": 6},
        "context_universe": {
            "active_block": {"block_id": future},
            "scene_block_candidates": [{"block_id": future}],
        },
    }
    body = {
        "schema_version": "literary_b4_input_bundle_v2",
        "handoff_contract_version": "literary_b4_handoff_contract_v2",
        "state_lineage_id": "lineage_frame_fixture",
        "unit_manifest": [
            {
                "unit_id": "bk_ch01",
                "block_range": [b0, b3],
                "parent_chapter": "bk_ch01",
                "cut_reason": "author_chapter",
                "source_hash": "source-1",
                "m1_checkpoint_refs": ["m1-1"],
            },
            {
                "unit_id": "bk_ch02",
                "block_range": [future, future],
                "parent_chapter": "bk_ch02",
                "cut_reason": "author_chapter",
                "source_hash": "source-2",
                "m1_checkpoint_refs": ["m1-2"],
            },
        ],
        "source_block_catalog": source,
        "occurrence_cards": [aster, rowan, future_card],
        "occurrence_routing": {
            "person_occurrences": [aster, rowan, future_card],
            "non_person_occurrences": [],
            "discourse_only": [],
            "deferred": [],
            "invalid_flagged": [],
            "counts": {"person_occurrences": 3},
        },
        "ground_evidence": {
            "frame_claim_inputs": [
                {
                    "chapter_id": "bk_ch01",
                    "ground_item_id": "g_frame_bad",
                    "payload": {
                        "segment_id": "seg_B3_MUST_NOT_SURVIVE",
                        "frame_kind": "B3_FORBIDDEN_LABEL",
                    },
                },
                {
                    "chapter_id": "bk_ch02",
                    "ground_item_id": "g_future_frame",
                    "payload": {"frame_kind": "FUTURE_FRAME_SECRET"},
                },
            ],
            "frame_leaf_index": [
                {
                    "chapter_id": "bk_ch01",
                    "ground_item_id": "g_old_leaf",
                    "payload": {"segment_id": "seg_B3_MUST_NOT_SURVIVE"},
                }
            ],
        },
    }
    return _seal_bundle(body)


def _nested_response(*, checker_keys: bool = False) -> dict[str, Any]:
    outer = "checker_outer" if checker_keys else "outer"
    child = "checker_child" if checker_keys else "diary"
    segments = [
        {
            "local_segment_key": outer,
            "parent_local_key": None,
            "block_range": ["bk_ch01_b001", "bk_ch01_b003"],
            "start_boundary": None,
            "end_boundary": None,
            "frame_kind": "primary_narration",
            "story_time_label": "frame_present",
            "narrator_occurrence_ref": "m_bk_ch01_b001_01",
            "narrator_surface": "Aster",
            "evidence": [
                {
                    "block_id": "bk_ch01_b001",
                    "evidence_quote": "Aster opened the notebook and introduced Rowan as the diarist.",
                }
            ],
        },
        {
            "local_segment_key": child,
            "parent_local_key": outer,
            "block_range": ["bk_ch01_b002", "bk_ch01_b002"],
            "start_boundary": {
                "anchor_text": "Entry begins now.",
                "evidence_quote": "Entry begins now. I record the storm.",
            },
            "end_boundary": None,
            "frame_kind": "diary",
            "story_time_label": "retrospective_past",
            "narrator_occurrence_ref": "m_bk_ch01_b001_02",
            "narrator_surface": "Rowan",
            "evidence": [
                {
                    "block_id": "bk_ch01_b002",
                    "evidence_quote": "I record the storm.",
                }
            ],
        },
    ]
    if checker_keys:
        segments.reverse()
    return {"response_status": "proposed", "segments": segments}


def _boundary_variant() -> dict[str, Any]:
    value = _nested_response(checker_keys=True)
    child = next(row for row in value["segments"] if row["frame_kind"] == "diary")
    child["start_boundary"] = {
        "anchor_text": "I record the storm.",
        "evidence_quote": "Entry begins now. I record the storm.",
    }
    return value


def _flat_response() -> dict[str, Any]:
    outer = deepcopy(_nested_response()["segments"][0])
    return {"response_status": "proposed", "segments": [outer]}


def _uncertain_response() -> dict[str, Any]:
    return {"response_status": "uncertain", "segments": []}


def _run(
    tmp_path: Path,
    *,
    adjudicator_response: Mapping[str, Any] | None = None,
    checker_response: Mapping[str, Any] | None = None,
    checker_family: str = "family-b",
    qualification_kind: str = "frame",
    store: Step5Store | None = None,
    bundle: Mapping[str, Any] | None = None,
) -> tuple[Any, ScriptedExecutor, Step5Store]:
    policy, adjudicator, checker, manifests = _authority_fixture(
        checker_family=checker_family, qualification_kind=qualification_kind
    )
    adjudicator_config, checker_config = _configs(adjudicator, checker)
    executor = ScriptedExecutor(
        {
            "adjudicator": adjudicator_response or _nested_response(),
            "checker": checker_response or _nested_response(checker_keys=True),
        }
    )
    actual_store = store or Step5Store(tmp_path / "store")
    result = run_frame_confirmation(
        bundle=bundle or _bundle(),
        unit_id="bk_ch01",
        prompt_provider=DesignPromptProvider(),
        executor=executor,
        store=actual_store,
        adjudicator_config=adjudicator_config,
        checker_config=checker_config,
        authority_policy=policy,
        qualification_manifests=manifests,
    )
    return result, executor, actual_store


def _manual_prompt(prompt_id: str) -> str:
    text = DESIGN_DOC.read_text(encoding="utf-8")
    marker = f"- Prompt version: {prompt_id}."
    marker_at = text.index(marker)
    start = text.rfind("\n>", 0, marker_at) + 1
    end = text.find("\n### ", marker_at)
    if end < 0:
        end = len(text)
    lines: list[str] = []
    for line in text[start:end].splitlines():
        if line.startswith("> "):
            lines.append(line[2:])
        elif line == ">":
            lines.append("")
        elif lines:
            break
    return "\n".join(lines).strip()


def test_probe_01_source_catalog_required_b3_claims_not_required() -> None:
    good = _bundle()
    selection = build_frame_selection_universe(good, unit_id="bk_ch01")
    assert len(selection["ordered_source_blocks"]) == 4
    no_claims = deepcopy(good)
    no_claims["ground_evidence"]["frame_claim_inputs"] = []
    no_claims["ground_evidence"]["frame_leaf_index"] = []
    no_claims = _seal_bundle(no_claims)
    no_claim_selection = build_frame_selection_universe(no_claims, unit_id="bk_ch01")
    assert no_claim_selection["ordered_source_blocks"] == selection["ordered_source_blocks"]
    assert no_claim_selection["occurrence_choices"] == selection["occurrence_choices"]
    assert no_claim_selection["b3_audit_input_hash"] != selection["b3_audit_input_hash"]
    no_source = deepcopy(good)
    no_source["source_block_catalog"] = []
    no_source = _seal_bundle(no_source)
    with pytest.raises(FrameContractError, match="absent"):
        build_frame_selection_universe(no_source, unit_id="bk_ch01")


def test_probe_02_requests_get_source_and_no_prior_semantic_answer() -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    _policy, adjudicator, checker, _manifests = _authority_fixture()
    adj_config, check_config = _configs(adjudicator, checker)
    provider = DesignPromptProvider()
    adj = render_frame_request(
        selection=selection,
        prompt_provider=provider,
        config=adj_config,
        role="adjudicator",
    )
    check = render_frame_request(
        selection=selection,
        prompt_provider=provider,
        config=check_config,
        role="checker",
    )
    for request in (adj, check):
        rendered = str(request.api_request)
        assert "Aster opened the notebook" in rendered
        assert "B3_FORBIDDEN_LABEL" not in rendered
        assert "seg_B3_MUST_NOT_SURVIVE" not in rendered
    assert adj.api_request != check.api_request


def test_probe_03_prompt_loader_bytes_and_book_neutrality() -> None:
    forbidden = ("heathcliff", "lockwood", "wuthering", "gatsby", "catherine")
    for prompt_id in (ADJUDICATOR_PROMPT_ID, CHECKER_PROMPT_ID):
        loaded = load_system_prompt_from_design(DESIGN_DOC, prompt_id)
        assert loaded == _manual_prompt(prompt_id)
        lowered = loaded.casefold()
        assert all(value not in lowered for value in forbidden)


def test_probe_04_future_unit_never_enters_current_request() -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    _policy, adjudicator, _checker, _manifests = _authority_fixture()
    config = _configs(adjudicator, _authority_fixture()[2])[0]
    rendered = render_frame_request(
        selection=selection,
        prompt_provider=DesignPromptProvider(),
        config=config,
        role="adjudicator",
    )
    assert "FUTURE_FRAME_SECRET" not in str(rendered.api_request)


def test_probe_05_synthetic_path_persists_request_lineage_response_and_view(tmp_path: Path) -> None:
    result, executor, store = _run(tmp_path)
    assert result.executed_call_count == 2
    assert len(executor.requests) == 2
    for artifact in result.request_artifact_hashes:
        payload = store.load_content_artifact("requests", artifact)
        assert payload["api_request"]["messages"]
        assert payload["rendered_sections"]
    for artifact in result.response_artifact_hashes:
        assert store.load_content_artifact("responses", artifact)["raw_response"]
    assert result.frame_view_handle["published_generation_hash"] == result.generation_hash


def test_probe_06_malformed_tree_range_and_boundary_fail_closed() -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    cases: list[tuple[dict[str, Any], str]] = []
    missing_parent = _nested_response()
    missing_parent["segments"][1]["parent_local_key"] = "missing"
    cases.append((missing_parent, "missing parent"))
    cycle = _nested_response()
    cycle["segments"][0]["parent_local_key"] = "diary"
    cases.append((cycle, "cycle"))
    outside = _nested_response()
    outside["segments"][0]["block_range"] = ["bk_ch01_b002", "bk_ch01_b003"]
    outside["segments"][0]["evidence"] = [
        {
            "block_id": "bk_ch01_b003",
            "evidence_quote": "The entry ended. Aster closed the notebook.",
        }
    ]
    outside["segments"][1]["block_range"] = ["bk_ch01_b001", "bk_ch01_b003"]
    outside["segments"][1]["start_boundary"] = None
    cases.append((outside, "outside its parent"))
    sibling_overlap = _flat_response()
    overlapping = deepcopy(sibling_overlap["segments"][0])
    overlapping["local_segment_key"] = "overlap"
    sibling_overlap["segments"].append(overlapping)
    cases.append((sibling_overlap, "siblings overlap"))
    ambiguous = _nested_response()
    ambiguous["segments"][1]["start_boundary"] = {
        "anchor_text": "the",
        "evidence_quote": "Before the entry, rain marked the glass. Entry begins now. I record the storm.",
    }
    cases.append((ambiguous, "failed closed"))
    gap = _flat_response()
    gap["segments"][0]["block_range"] = ["bk_ch01_b002", "bk_ch01_b003"]
    gap["segments"][0]["evidence"] = [
        {
            "block_id": "bk_ch01_b003",
            "evidence_quote": "The entry ended. Aster closed the notebook.",
        }
    ]
    cases.append((gap, "uncovered"))
    wrong_type = _nested_response()
    wrong_type["segments"][0]["narrator_surface"] = 7
    cases.append((wrong_type, "narrator surface must be a string"))
    for value, expected in cases:
        with pytest.raises(FrameContractError, match=expected):
            validate_frame_response(value, selection=selection)


def test_probe_07_synthetic_root_is_code_owned() -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    value = _flat_response()
    extra = deepcopy(value["segments"][0])
    extra["local_segment_key"] = "second"
    extra["block_range"] = ["bk_ch01_b003", "bk_ch01_b003"]
    extra["evidence"] = [
        {
            "block_id": "bk_ch01_b003",
            "evidence_quote": "The entry ended. Aster closed the notebook.",
        }
    ]
    value["segments"][0]["block_range"] = ["bk_ch01_b001", "bk_ch01_b002"]
    value["segments"].append(extra)
    tree = validate_frame_response(value, selection=selection)
    assert sum(node["parent_canonical_node_key"] is None for node in tree.nodes) == 2
    bad = _flat_response()
    bad["segments"][0]["local_segment_key"] = "synthetic_root_model"
    with pytest.raises(FrameContractError, match="reserved"):
        validate_frame_response(bad, selection=selection)


def test_probe_08_local_key_and_output_order_do_not_change_signature() -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    first = validate_frame_response(_nested_response(), selection=selection)
    second = validate_frame_response(
        _nested_response(checker_keys=True), selection=selection
    )
    assert first.canonical_signature_hash == second.canonical_signature_hash


def test_probe_09_semantic_tree_difference_yields_unknown_without_retry(tmp_path: Path) -> None:
    result, executor, _store = _run(tmp_path, checker_response=_flat_response())
    assert result.frame_view["status"] == "unknown"
    assert result.executed_call_count == 2
    assert len(executor.requests) == 2


def test_probe_10_nested_diary_remains_distinct(tmp_path: Path) -> None:
    result, _executor, _store = _run(tmp_path)
    assert result.frame_view["status"] == "active"
    assert {row["frame_kind"] for row in result.frame_view["segments"]} == {
        "primary_narration",
        "diary",
    }


def test_probe_11_narrator_occurrence_is_grounded_but_may_be_outside_child(tmp_path: Path) -> None:
    result, _executor, _store = _run(tmp_path)
    diary = next(row for row in result.frame_view["segments"] if row["frame_kind"] == "diary")
    assert diary["narrator_occurrence_ref"] == "m_bk_ch01_b001_02"
    assert diary["narrator_entity_id"] is None
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    foreign = _nested_response()
    foreign["segments"][1]["narrator_occurrence_ref"] = "m_foreign"
    with pytest.raises(FrameContractError, match="foreign"):
        validate_frame_response(foreign, selection=selection)
    nullable = _nested_response()
    nullable["segments"][1]["narrator_occurrence_ref"] = None
    assert validate_frame_response(nullable, selection=selection)


def test_probe_12_b3_segment_ids_never_become_active_ids(tmp_path: Path) -> None:
    result, _executor, store = _run(tmp_path)
    rendered = str(result.frame_view)
    assert "seg_B3_MUST_NOT_SURVIVE" not in rendered
    assert all(row["segment_id"].startswith("frm_") for row in result.frame_view["segments"])
    generation = store.load_generation(result.generation_hash)
    semantic = store.load_semantic_state(str(generation["semantic_state_hash"]))
    assert "seg_B3_MUST_NOT_SURVIVE" not in str(semantic)


def test_probe_13_authority_ladder_same_model_and_cross_kind_stop_at_unknown(tmp_path: Path) -> None:
    active, _executor, _store = _run(tmp_path / "active")
    assert active.decision_state == "active"
    same, _executor, _store = _run(
        tmp_path / "same", checker_family="family-a"
    )
    assert same.decision_state == "corroborated"
    assert same.frame_view["status"] == "unknown"
    cross, _executor, _store = _run(
        tmp_path / "cross", qualification_kind="identity"
    )
    assert cross.decision_state == "corroborated"
    assert cross.frame_view["status"] == "unknown"


def test_probe_14_blinding_uses_exact_persisted_section_lineage(tmp_path: Path) -> None:
    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    _policy, _adj, checker, _manifests = _authority_fixture()
    config = _configs(_authority_fixture()[1], checker)[1]
    store = Step5Store(tmp_path / "store")
    rendered = render_frame_request(
        selection=selection,
        prompt_provider=DesignPromptProvider(),
        config=config,
        role="checker",
    )
    persisted = persist_frame_request(store, rendered)
    manifest = checker_manifest_from_persisted_lineage(
        store, persisted.lineage_artifact_hash
    )
    clean = BlindingValidationRecord(
        checker_request_fingerprint=rendered.request_fingerprint,
        checker_input_section_manifest_hash=manifest.checker_input_section_manifest_hash,
        checker_input_section_manifest=manifest,
        adjudicator_response_artifact_hash="adjudicator-response",
        validator_contract_hash="validator",
    )
    assert validate_blinding(clean) == "valid"

    lineage = store.load_content_artifact(
        "request_lineage", persisted.lineage_artifact_hash
    )
    lineage["sections"][0]["transitive_source_artifact_hashes"].append(
        "adjudicator-response"
    )
    contaminated_hash = store.put_content_artifact("request_lineage", lineage)
    contaminated_manifest = checker_manifest_from_persisted_lineage(
        store, contaminated_hash
    )
    contaminated = replace(
        clean,
        checker_input_section_manifest_hash=(
            contaminated_manifest.checker_input_section_manifest_hash
        ),
        checker_input_section_manifest=contaminated_manifest,
    )
    assert validate_blinding(contaminated) == "invalid"

    incomplete = deepcopy(lineage)
    incomplete["sections"] = incomplete["sections"][:-1]
    incomplete_hash = store.put_content_artifact("request_lineage", incomplete)
    with pytest.raises(FrameContractError, match="exact section cover"):
        checker_manifest_from_persisted_lineage(store, incomplete_hash)


def test_probe_15_uncertain_is_unknown_and_skips_checker(tmp_path: Path) -> None:
    result, executor, _store = _run(
        tmp_path, adjudicator_response=_uncertain_response()
    )
    assert result.frame_view["status"] == "unknown"
    assert result.executed_call_count == 1
    assert len(executor.requests) == 1


def test_probe_16_active_ids_and_resume_are_deterministic(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    first, _executor, _store = _run(tmp_path / "first", store=store)
    second, _executor, _store = _run(tmp_path / "second", store=store)
    straight, _executor, _store = _run(tmp_path / "straight")
    assert first.generation_hash == second.generation_hash
    assert first.frame_view == second.frame_view
    assert first.frame_view_handle == second.frame_view_handle
    assert first.generation_hash == straight.generation_hash
    assert first.frame_view == straight.frame_view
    tampered = deepcopy(first.frame_view)
    tampered["segments"][0]["frame_kind"] = "dream"
    with pytest.raises(FrameContractError, match="content hash"):
        verify_confirmed_frame_view(tampered)


def test_probe_17_nonactive_tree_mints_no_frame_ids(tmp_path: Path) -> None:
    result, _executor, store = _run(
        tmp_path, checker_family="family-a"
    )
    assert result.frame_view["segments"] == []
    assert all(value is None for value in result.frame_view["deepest_frame_by_block"].values())
    overlay_root = store.root / "overlay"
    assert not overlay_root.exists() or not list(overlay_root.glob("*.json"))

    shared = Step5Store(tmp_path / "replacement-store")
    active, _executor, _store = _run(tmp_path / "before", store=shared)
    old_refs = {
        row["overlay_record_ref"] for row in active.frame_view["segments"]
    }
    replaced, _executor, _store = _run(
        tmp_path / "after", store=shared, checker_family="family-a"
    )
    assert replaced.frame_view["status"] == "unknown"
    generation = shared.load_generation(replaced.generation_hash)
    semantic = shared.load_semantic_state(str(generation["semantic_state_hash"]))
    assert old_refs.isdisjoint(semantic["overlay_record_refs"])
    assert all((shared.root / "overlay" / f"{value}.json").is_file() for value in old_refs)


class CrashBeforeSwitchStore(Step5Store):
    def cas_switch(self, **kwargs: Any) -> None:  # type: ignore[override]
        raise RuntimeError("crash-before-pointer-switch")


def test_probe_18_crash_before_pointer_switch_preserves_bundle_and_pointer(tmp_path: Path) -> None:
    bundle = _bundle()
    before = deepcopy(bundle)
    store = CrashBeforeSwitchStore(tmp_path / "store")
    with pytest.raises(RuntimeError, match="crash-before"):
        _run(tmp_path, store=store, bundle=bundle)
    assert bundle == before
    assert store.current_generation_hash("lineage_frame_fixture") is None


def test_probe_19_stale_parent_is_rejected_without_rebase(tmp_path: Path) -> None:
    result, _executor, store = _run(tmp_path)
    with pytest.raises(StaleParentError):
        store.cas_switch(
            state_lineage_id="lineage_frame_fixture",
            expected_current="foreign-parent",
            new_generation_hash=result.generation_hash,
        )
    assert store.current_generation_hash("lineage_frame_fixture") == result.generation_hash


def test_probe_20_s5c_can_query_frame_and_remap_narrator_without_bundle(tmp_path: Path) -> None:
    result, _executor, store = _run(tmp_path)
    loaded_view = load_confirmed_frame_view(
        store,
        state_lineage_id="lineage_frame_fixture",
        handle=result.frame_view_handle,
    )
    assert loaded_view == result.frame_view
    b2 = next(
        row for row in _bundle()["source_block_catalog"] if row["block_id"] == "bk_ch01_b002"
    )
    offset = b2["text"].index("I record the storm.")
    segment_id = deepest_frame_at(
        loaded_view, block_id="bk_ch01_b002", char_offset=offset
    )
    diary = next(row for row in loaded_view["segments"] if row["segment_id"] == segment_id)
    active_bindings = {"m_bk_ch01_b001_02": "ent_rowan"}
    assert active_bindings[diary["narrator_occurrence_ref"]] == "ent_rowan"


def test_probe_21_coverage_denominator_is_code_owned(tmp_path: Path) -> None:
    active, _executor, _store = _run(tmp_path / "active")
    assert active.frame_view["verified_frame_coverage"] == {
        "numerator": 3,
        "denominator": 3,
    }
    unknown, _executor, _store = _run(
        tmp_path / "unknown", checker_response=_flat_response()
    )
    assert unknown.frame_view["verified_frame_coverage"] == {
        "numerator": 0,
        "denominator": 3,
    }


def test_probe_22_call_plan_has_two_base_calls_and_retry_reserve(tmp_path: Path) -> None:
    result, _executor, _store = _run(tmp_path)
    assert len(result.call_plan.entries) == 2
    assert {row.call_kind for row in result.call_plan.entries} == {"J", "Cd"}
    assert all(row.technical_retry_cap == 1 for row in result.call_plan.entries)
    assert all(row.retry_reserve > 0 for row in result.call_plan.entries)

    selection = build_frame_selection_universe(_bundle(), unit_id="bk_ch01")
    _policy, adjudicator, checker, _manifests = _authority_fixture()
    adj_config, checker_config = _configs(adjudicator, checker)
    adj_request = render_frame_request(
        selection=selection,
        prompt_provider=DesignPromptProvider(),
        config=adj_config,
        role="adjudicator",
    )
    checker_request = render_frame_request(
        selection=selection,
        prompt_provider=DesignPromptProvider(),
        config=checker_config,
        role="checker",
    )
    with pytest.raises(FrameContractError, match="split the unit"):
        build_frame_call_plan(
            adjudicator_request=adj_request,
            checker_request=checker_request,
            adjudicator_config=replace(adj_config, prompt_token_cap=1),
            checker_config=checker_config,
        )


def test_probe_23_valid_boundary_disagreement_is_visible_and_unknown(tmp_path: Path) -> None:
    result, _executor, _store = _run(
        tmp_path, checker_response=_boundary_variant()
    )
    assert result.frame_view["status"] == "unknown"
    assert result.valid_boundary_disagreement_units == 1
    assert len(result.response_artifact_hashes) == 2
