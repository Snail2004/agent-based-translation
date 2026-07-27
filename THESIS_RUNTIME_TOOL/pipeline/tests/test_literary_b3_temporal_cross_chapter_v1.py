from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.llm_backend import RawTransportResponse, canonical_json, canonical_sha256
from pipeline.literary import b3_temporal_chapter_runner_v1 as runner_module
from pipeline.literary.b3_temporal_chapter_runner_v1 import (
    B3TemporalChapterRunnerError,
    _verify_b3_resume_semantic_identity_v1,
    bind_b3_runtime_call_budget_v1,
    execute_b3_temporal_chapter_run_v1,
    prepare_b3_temporal_chapter_run_v1,
)
from pipeline.literary.b3_temporal_context_v1 import load_b3_temporal_profile_v1
from pipeline.literary.b3_temporal_context_v1 import build_b3_temporal_components_v1
from pipeline.literary.b3_temporal_context_v3 import (
    build_b3_temporal_cross_chapter_bundle_v3,
    render_b3_temporal_sequential_batch_v3,
)
from pipeline.literary.b3_temporal_context_v4 import (
    build_b3_temporal_cross_chapter_bundle_v4,
)
from pipeline.literary.b3_temporal_context_v6 import (
    _candidate_bins_v6,
    render_b3_temporal_sequential_batch_v6,
)
from pipeline.literary.b3_temporal_contract_v1 import B3TemporalContractError
from pipeline.literary.b3_temporal_contract_v3 import (
    validate_b3_temporal_request_v3,
)
from pipeline.literary.b3_temporal_contract_v4 import (
    validate_b3_temporal_request_v4,
)
from pipeline.literary.b3_temporal_contract_v6 import (
    validate_b3_temporal_request_v6,
)
from pipeline.literary.b3_temporal_prompts_v6 import b3_temporal_response_schema_v6
from pipeline.literary.b3_temporal_prompts_v7 import b3_temporal_response_schema_v7
from pipeline.literary.b3_temporal_prefix_v1 import (
    B3TemporalPrefixError,
    build_b3_temporal_prefix_v1,
)
from pipeline.literary.b3_temporal_capability_contract_v4 import (
    b3_validator_ref_v4,
)
from pipeline.literary.model_ref_transport_v1 import (
    bind_model_ref_validator_v1,
    project_capability_probe_request_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.semantic_run_identity_v1 import (
    LiterarySemanticIdentityError,
    build_literary_semantic_stage_identity_v1,
    verify_literary_semantic_stage_identity_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.tests.test_literary_b3_temporal_live_v1 import (
    _qualified_evidence,
    _runtime,
)
from pipeline.tests.test_literary_b3_temporal_v1 import _prior_state, _temporal_input


class _SequentialSender:
    def __init__(self) -> None:
        self.calls = 0
        self.second_prior_states: list[dict] | None = None
        self.second_pending: list[dict] | None = None

    def send(self, request):
        self.calls += 1
        body = json.loads(request.body)
        payload = json.loads(body["messages"][-1]["content"])
        component = payload["components"][0]
        if self.calls == 1:
            turn = component["speaker_turns"][0]
            frame_id = next(
                row["frame_segment_id"]
                for row in payload["frame_packets"]
                if component["component_id"] in row["component_ids"]
            )
            actions = [
                {
                    "operation": "open_state",
                    "state_domain": "relationship",
                    "subject_referent_refs": [component["referent_refs"][0]],
                    "counterpart_referent_refs": [component["referent_refs"][1]],
                    "state_value": "trusted companions",
                    "event_status": "occurred",
                    "temporal_position": "current_progression",
                    "source_event_ids": [],
                    "source_turn_ids": [turn["speaker_turn_id"]],
                    "source_block_ids": [turn["block_id"]],
                    "frame_segment_ids": [frame_id],
                    "reason": "The grounded exchange establishes a durable relation.",
                }
            ]
            disposition = "state_actions_proposed"
        else:
            self.second_prior_states = [
                deepcopy(row["state"])
                for row in payload["prior_state_packets"]
                if component["component_id"] in row["component_ids"]
            ]
            self.second_pending = [
                deepcopy(row["pending_case"])
                for row in payload["prior_pending_packets"]
                if component["component_id"] in row["component_ids"]
            ]
            actions = []
            disposition = "no_durable_change"
        semantic = {
            "schema_version": "literary_b3_temporal_response_v3",
            "chapter_id": payload["chapter_id"],
            "batch_id": payload["batch_id"],
            "component_results": [
                {
                    "component_id": component["component_id"],
                    "disposition": disposition,
                    "state_actions": actions,
                    "pending_route": "none",
                    "pending_reason": None,
                    "inherited_parked_identities": [],
                }
            ],
        }
        provider = {
            "id": f"b3-cross-{self.calls}",
            "model": "gpt-5.4-2026-03-05",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": canonical_json(semantic)},
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": f"b3-cross-{self.calls}"},
            body=canonical_json(provider).encode("utf-8"),
            request_id=f"b3-cross-{self.calls}",
        )


def _profile(tmp_path: Path, *, max_components: int = 1):
    source = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "literary_b3_temporal_phase_a_v1.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["profile_id"] = "literary_b3_temporal_cross_chapter_test_v1"
    payload["batching"]["max_components_per_request"] = max_components
    payload["batching"]["max_requests_per_chapter"] = 2
    target = tmp_path / "b3_profile.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return load_b3_temporal_profile_v1(target)


def _two_component_input() -> dict:
    data = deepcopy(_temporal_input())
    data["source_b2_artifact_hash"] = "1" * 64
    data["source_prefix_bundle_hash"] = "2" * 64
    event = data["salient_events"][0]
    event["event_kind"] = "identity_or_role_change"
    event["event_status"] = "occurred"
    event["memory_role"] = "relationship_evidence"
    unsigned = dict(data)
    unsigned.pop("input_hash", None)
    data["input_hash"] = canonical_hash(unsigned)
    return data


def _pending_case() -> dict:
    return {
        "pending_case_id": "b3pend1_prior",
        "request_fingerprint": "a" * 64,
        "chapter_id": "book_ch00",
        "batch_id": "prior_batch",
        "component_id": "prior_component",
        "review_route": "temporal_review",
        "reason_codes": ["model_requested_review"],
        "reason": "Earlier evidence did not establish the relation.",
        "proposed_action": {
            "operation": "reveal_only",
            "state_domain": "relationship",
            "subject_referent_refs": ["ref_a"],
            "counterpart_referent_refs": ["ref_b"],
            "state_value": "possible companions",
            "event_status": "uncertain",
            "temporal_position": "unknown",
            "source_event_ids": [],
            "source_turn_ids": ["prior_turn"],
            "source_block_ids": ["book_ch00_b001"],
            "frame_segment_ids": ["prior_frame"],
            "reason": "The earlier report was ambiguous.",
        },
        "authority_status": "pending_review",
    }


def _prior_root(tmp_path: Path) -> Path:
    root = tmp_path / "prior_b3"
    root.mkdir()
    body = {
        "schema_version": "literary_b3_temporal_artifact_v1",
        "request_fingerprint": "a" * 64,
        "chapter_id": "book_ch00",
        "batch_id": "prior_batch",
        "source_b2_artifact_hash": "b" * 64,
        "source_prefix_bundle_hash": "c" * 64,
        "component_results": [],
        "new_state_rows": [],
        "transition_rows": [],
        "reinforcement_rows": [],
        "historical_observations": [],
        "non_effective_observations": [],
        "pending_cases": [_pending_case()],
        "effective_state_projection": [],
        "closed_prior_state_ids": [],
        "raw_response_sha256": "d" * 64,
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    artifact = {**body, "artifact_hash": canonical_hash(body)}
    (root / "b3_temporal_artifact.json").write_text(
        canonical_json(artifact) + "\n", encoding="utf-8"
    )
    return root


def test_prefix_keeps_pending_separate_from_authority(tmp_path: Path) -> None:
    prefix = build_b3_temporal_prefix_v1([_prior_root(tmp_path)])
    assert prefix["effective_open_states"] == []
    assert [row["pending_case_id"] for row in prefix["pending_cases"]] == [
        "b3pend1_prior"
    ]
    assert prefix["authority_policy"]["identity_inference_by_code"] is False


def test_prefix_carries_terminal_case_history_without_reopening(
    tmp_path: Path,
) -> None:
    root = _prior_root(tmp_path)
    path = root / "b3_temporal_artifact.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    resolved = artifact["pending_cases"].pop()
    resolved.update(
        {
            "authority_status": "resolved_terminal",
            "disposition": "origin_unknown",
            "unknowable_window": {
                "from_chapter": "book_ch00",
                "to_chapter": "book_ch00",
                "blocker": "onset_not_stated_in_source",
            },
        }
    )
    artifact["resolved_cases"] = [resolved]
    unsigned = dict(artifact)
    unsigned.pop("artifact_hash")
    artifact["artifact_hash"] = canonical_hash(unsigned)
    path.write_text(canonical_json(artifact) + "\n", encoding="utf-8")

    prefix = build_b3_temporal_prefix_v1([root])

    assert prefix["pending_cases"] == []
    assert [row["pending_case_id"] for row in prefix["resolved_cases"]] == [
        "b3pend1_prior"
    ]
    assert (
        prefix["authority_policy"]["resolved_cases"]
        == "terminal_review_history"
    )


def test_tampered_prior_artifact_fails_closed(tmp_path: Path) -> None:
    root = _prior_root(tmp_path)
    path = root / "b3_temporal_artifact.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pending_cases"][0]["authority_status"] = "effective"
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    with pytest.raises(B3TemporalPrefixError, match="hash mismatch"):
        build_b3_temporal_prefix_v1([root])


def test_v3_context_exposes_pending_as_review_only(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    bundle = build_b3_temporal_cross_chapter_bundle_v3(
        temporal_input=_two_component_input(),
        profile=profile,
        prior_states=[],
        prior_pending_cases=[_pending_case()],
    )
    assert bundle["plan"]["request_count"] == 2
    for request in bundle["initial_requests"]:
        _body, payload = validate_b3_temporal_request_v3(request)
        assert payload["components"][0]["prior_pending_cases"][0][
            "authority_status"
        ] == "pending_review"
    bad = deepcopy(bundle["initial_requests"][0])
    payload = json.loads(bad["messages"][1]["content"])
    payload["components"][0]["prior_pending_cases"][0][
        "authority_status"
    ] = "effective"
    bad["messages"][1]["content"] = canonical_json(payload)
    unsigned = dict(bad)
    unsigned.pop("request_fingerprint")
    bad["request_fingerprint"] = canonical_hash(unsigned)
    with pytest.raises(B3TemporalContractError, match="claims authority"):
        validate_b3_temporal_request_v3(bad)


def test_v3_does_not_render_a_v1_intermediate(
    tmp_path: Path, monkeypatch,
) -> None:
    profile = _profile(tmp_path)
    components = build_b3_temporal_components_v1(
        temporal_input=_two_component_input(),
        profile=profile,
        prior_states=[],
        prior_pending_cases=[_pending_case()],
    )
    monkeypatch.setattr(
        "pipeline.literary.b3_temporal_context_v1."
        "render_b3_temporal_phase_a_batch_v1",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("V1 renderer called")),
    )
    rendered = render_b3_temporal_sequential_batch_v3(
        temporal_input=_two_component_input(),
        components=components,
        profile=profile,
        batch_ordinal=1,
    )
    assert rendered["schema_version"] == "literary_b3_temporal_request_v3"


def test_v4_packets_repeated_prior_context_once(tmp_path: Path) -> None:
    profile = _profile(tmp_path, max_components=2)
    prior_state = _prior_state("b3state1_survivor")
    prior_state["consolidated_state_ids"] = ["b3state1_absorbed"]
    bundle = build_b3_temporal_cross_chapter_bundle_v4(
        temporal_input=_two_component_input(),
        profile=profile,
        prior_states=[prior_state],
        prior_pending_cases=[_pending_case()],
    )
    assert bundle["plan"]["request_count"] == 1
    request = bundle["initial_requests"][0]
    _body, expanded = validate_b3_temporal_request_v4(request)
    raw = json.loads(request["messages"][1]["content"])
    assert all("prior_pending_cases" not in row for row in raw["components"])
    assert len(raw["prior_state_packets"]) == 1
    assert "consolidated_state_ids" not in raw["prior_state_packets"][0]["state"]
    assert prior_state["consolidated_state_ids"] == ["b3state1_absorbed"]
    assert len(raw["prior_pending_packets"]) == 1
    assert len(raw["prior_pending_packets"][0]["component_ids"]) == 2
    assert all(
        row["prior_pending_cases"][0]["pending_case_id"] == "b3pend1_prior"
        for row in expanded["components"]
    )

    bad = deepcopy(request)
    payload = json.loads(bad["messages"][1]["content"])
    payload["prior_pending_packets"][0]["component_ids"] = ["foreign_component"]
    bad["messages"][1]["content"] = canonical_json(payload)
    unsigned = dict(bad)
    unsigned.pop("request_fingerprint")
    bad["request_fingerprint"] = canonical_hash(unsigned)
    with pytest.raises(B3TemporalContractError, match="component relevance"):
        validate_b3_temporal_request_v4(bad)

    bad_authority = deepcopy(request)
    payload = json.loads(bad_authority["messages"][1]["content"])
    payload["prior_pending_packets"][0]["pending_case"][
        "authority_status"
    ] = "effective"
    bad_authority["messages"][1]["content"] = canonical_json(payload)
    unsigned = dict(bad_authority)
    unsigned.pop("request_fingerprint")
    bad_authority["request_fingerprint"] = canonical_hash(unsigned)
    with pytest.raises(B3TemporalContractError, match="wrong authority"):
        validate_b3_temporal_request_v4(bad_authority)


def test_v6_packets_repeated_reviews_losslessly(tmp_path: Path) -> None:
    profile = _profile(tmp_path, max_components=2)
    temporal_input = _two_component_input()
    components = build_b3_temporal_components_v1(
        temporal_input=temporal_input,
        profile=profile,
        prior_states=[],
        prior_pending_cases=[],
    )
    assert len(components) == 2
    candidate_ids = sorted(
        {
            card["candidate_card_id"]
            for component in components
            for card in component["candidate_cards"]
        }
    )
    expected: dict[str, list[dict]] = {}
    for component in components:
        row = {
            "review_id": "shared_review",
            "origin": "model",
            "origin_stage": "interaction",
            "review_kind": "event_significance",
            "blocking_kind": "timeline_pending",
            "source_block_ids": [component["source_blocks"][0]["block_id"]],
            "referent_refs": sorted(component["referent_refs"]),
            "candidate_card_ids": candidate_ids,
            "reason": "The same review applies to both bounded components.",
        }
        component["b2_review_requests"] = [row]
        expected[component["component_id"]] = [deepcopy(row)]

    request = render_b3_temporal_sequential_batch_v6(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
        batch_ordinal=1,
    )
    _body, expanded = validate_b3_temporal_request_v6(request)
    payload = json.loads(request["messages"][1]["content"])
    assert len(payload["b2_review_packets"]) == 1
    assert len(payload["b2_review_packets"][0]["component_bindings"]) == 2
    assert all("b2_review_requests" not in row for row in payload["components"])
    assert all(row["review_ids"] == ["shared_review"] for row in payload["components"])
    assert {
        row["component_id"]: row["b2_review_requests"]
        for row in expanded["components"]
    } == expected
    projected = project_capability_probe_request_v1(request)
    projected_payload = json.loads(projected["messages"][1]["content"])
    projected_review_id = projected_payload["b2_review_packets"][0]["review_id"]
    assert projected_review_id.startswith("R")
    assert all(
        row["review_ids"] == [projected_review_id]
        for row in projected_payload["components"]
    )


def test_v6_review_packet_rejects_tampered_binding(tmp_path: Path) -> None:
    profile = _profile(tmp_path, max_components=2)
    temporal_input = _two_component_input()
    components = build_b3_temporal_components_v1(
        temporal_input=temporal_input,
        profile=profile,
        prior_states=[],
        prior_pending_cases=[],
    )
    candidate_ids = sorted(
        {
            card["candidate_card_id"]
            for component in components
            for card in component["candidate_cards"]
        }
    )
    for component in components:
        component["b2_review_requests"] = [
            {
                "review_id": "shared_review",
                "origin": "model",
                "origin_stage": "interaction",
                "review_kind": "event_significance",
                "blocking_kind": "timeline_pending",
                "source_block_ids": [component["source_blocks"][0]["block_id"]],
                "referent_refs": sorted(component["referent_refs"]),
                "candidate_card_ids": candidate_ids,
                "reason": "The same review applies to both bounded components.",
            }
        ]
    request = render_b3_temporal_sequential_batch_v6(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
        batch_ordinal=1,
    )
    payload = json.loads(request["messages"][1]["content"])
    payload["b2_review_packets"][0]["component_bindings"][0][
        "source_block_ids"
    ] = ["foreign_block"]
    request["messages"][1]["content"] = canonical_json(payload)
    unsigned = dict(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)
    with pytest.raises(B3TemporalContractError, match="binding relevance"):
        validate_b3_temporal_request_v6(request)


def test_v6_batching_co_locates_shared_reviews(tmp_path: Path) -> None:
    profile = _profile(tmp_path, max_components=2)
    temporal_input = {
        "chapter_id": "book_ch01",
        "input_hash": "1" * 64,
        "source_b2_artifact_hash": "2" * 64,
        "source_prefix_bundle_hash": "3" * 64,
    }

    def component(
        ordinal: int, *, review_id: str
    ) -> dict:
        component_id = f"component_{ordinal}"
        block_id = f"book_ch01_b{ordinal:03d}"
        body = {
            "component_id": component_id,
            "component_hash": str(ordinal) * 64,
            "component_kind": "dialogue_dyad",
            "domain_hints": [],
            "referent_refs": [],
            "candidate_cards": [],
            "speaker_turns": [
                {
                    "speaker_turn_id": f"turn_{ordinal}",
                    "block_id": block_id,
                }
            ],
            "salient_events": [],
            "source_blocks": [{"block_id": block_id, "text": f"Text {ordinal}"}],
            "frame_segments": [],
            "prior_open_states": [],
            "prior_pending_cases": [],
            "b2_review_requests": [
                {
                    "review_id": review_id,
                    "origin": "model",
                    "origin_stage": "interaction",
                    "review_kind": "event_significance",
                    "blocking_kind": "timeline_pending",
                    "source_block_ids": [block_id],
                    "referent_refs": [],
                    "candidate_card_ids": [],
                    "reason": f"Shared review {review_id}.",
                }
            ],
            "component_ordinal": ordinal,
        }
        return body

    components = [
        component(1, review_id="review_shared"),
        component(2, review_id="review_shared"),
        component(3, review_id="review_c"),
        component(4, review_id="review_d"),
    ]
    batches = _candidate_bins_v6(
        temporal_input=temporal_input,
        weighted=components,
        profile=profile,
        bin_count=2,
    )
    memberships = [{row["component_id"] for row in batch} for batch in batches]
    assert {"component_1", "component_2"} in memberships


def test_sequential_runner_updates_batch_two_and_resumes_without_calls(
    tmp_path: Path, monkeypatch
) -> None:
    evidence = _qualified_evidence(tmp_path / "capability")
    sender = _SequentialSender()
    chapter_runtime_profile = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "literary_shared_llm_runtime_openai_b3_temporal_chapter_v1.json"
    )
    runtime = _runtime(
        tmp_path / "runtime",
        evidence,
        sender,
        runtime_profile_path=chapter_runtime_profile,
        capability_schema=b3_temporal_response_schema_v7(),
        capability_validator_ref=bind_model_ref_validator_v1(b3_validator_ref_v4()),
    )
    temporal_input = _two_component_input()
    temporal_input["speaker_recovery_binding"] = {
        "speaker_recovery_artifact_hash": "d" * 64,
    }
    source_root = tmp_path / "b2_source"
    source_root.mkdir()
    (source_root / "immutable.json").write_text("{}\n", encoding="utf-8")
    recovery_root = tmp_path / "speaker_recovery"
    recovery_root.mkdir()
    (recovery_root / "immutable.json").write_text("{}\n", encoding="utf-8")

    def load_temporal(_root, *, speaker_recovery_root=None):
        assert speaker_recovery_root == recovery_root.resolve()
        return deepcopy(temporal_input)

    monkeypatch.setattr(
        runner_module,
        "load_b2_temporal_input_v1",
        load_temporal,
    )
    output = tmp_path / "b3_chapter"
    profile = _profile(tmp_path)
    prepare_b3_temporal_chapter_run_v1(
        b2_run_root=source_root,
        speaker_recovery_root=recovery_root,
        prior_b3_roots=[_prior_root(tmp_path)],
        output_root=output,
        profile=profile,
        shared_runtime=runtime,
        current_git_head="a" * 40,
        max_calls=2,
    )
    report = execute_b3_temporal_chapter_run_v1(
        output_root=output,
        profile=profile,
        shared_runtime=runtime,
        current_git_head="a" * 40,
    )
    assert sender.calls == 2
    assert report["api_calls_performed"] == 2
    assert sender.second_prior_states
    assert sender.second_pending
    artifact = json.loads(
        (output / "chapter_temporal_artifact.json").read_text(encoding="utf-8")
    )
    assert artifact["source_b2_speaker_recovery_artifact_hash"] == "d" * 64
    assert len(artifact["effective_state_projection"]) == 1
    assert "b3pend1_prior" in artifact["carried_prior_pending_case_ids"]
    again = execute_b3_temporal_chapter_run_v1(
        output_root=output,
        profile=profile,
        shared_runtime=runtime,
        current_git_head="a" * 40,
    )
    assert again == report
    assert sender.calls == 2


def test_semantic_identity_ignores_transport_but_rejects_model_change() -> None:
    class Preset:
        preset_id = "preset"
        preset_revision = "v1"
        requested_model_id = "gpt-5.4"
        generation = {"temperature": 1, "max_output_tokens": 100}
        limits = {"max_calls": 1}
        transport_retry = {"max_retries": 0}
        semantic_retry = {"max_retries": 0}

    class Runtime:
        def __init__(self, source: str, model: str = "gpt-5.4") -> None:
            self.source = source
            self.preset = Preset()
            self.preset.requested_model_id = model

        def role_preset_for(self, _role_id):
            return self.preset

    kwargs = {
        "role_id": "literary.b3.temporal_state",
        "prompt_id": "prompt",
        "prompt_sha256": "a" * 64,
        "response_schema_sha256": "b" * 64,
        "validator_ref": {"id": "validator", "revision": "v1", "sha256": "c" * 64},
        "application_contract_id": "apply",
        "application_contract_revision": "v1",
        "context_contract": {"hash": "d" * 64},
    }
    first = build_literary_semantic_stage_identity_v1(
        shared_runtime=Runtime("source-a"), **kwargs
    )
    replacement = build_literary_semantic_stage_identity_v1(
        shared_runtime=Runtime("source-b"), **kwargs
    )
    verify_literary_semantic_stage_identity_v1(
        expected=first, observed=replacement
    )
    changed = build_literary_semantic_stage_identity_v1(
        shared_runtime=Runtime("source-b", model="gpt-5.5"), **kwargs
    )
    with pytest.raises(LiterarySemanticIdentityError, match="changed"):
        verify_literary_semantic_stage_identity_v1(
            expected=first, observed=changed
        )


def test_b3_resume_identity_allows_only_upward_capacity() -> None:
    expected = {
        "schema_version": "literary_semantic_stage_identity_v1",
        "requested_model_id": "gpt-5.4",
        "prompt": {"id": "prompt", "sha256": "a" * 64},
        "response_contract": {"canonical_schema_sha256": "b" * 64},
        "context_contract": {
            "profile_id": "capacity-profile",
            "profile_hash": "c" * 64,
            "profile_sha256": "d" * 64,
        },
        "generation": {"max_input_tokens": 20_000, "max_output_tokens": 8_000},
        "limits": {
            "max_calls": 5,
            "max_prompt_tokens": 100_000,
            "max_completion_tokens": 40_000,
            "max_total_tokens": 140_000,
        },
    }
    expected["semantic_identity_hash"] = canonical_hash(expected)
    observed = deepcopy(expected)
    observed["context_contract"]["profile_hash"] = "e" * 64
    observed["context_contract"]["profile_sha256"] = "f" * 64
    observed["generation"]["max_input_tokens"] = 24_000
    observed["limits"]["max_prompt_tokens"] = 120_000
    observed["limits"]["max_total_tokens"] = 160_000
    observed.pop("semantic_identity_hash")
    observed["semantic_identity_hash"] = canonical_hash(observed)

    _verify_b3_resume_semantic_identity_v1(
        expected=expected,
        observed=observed,
    )

    lowered = deepcopy(observed)
    lowered["generation"]["max_input_tokens"] = 19_999
    lowered.pop("semantic_identity_hash")
    lowered["semantic_identity_hash"] = canonical_hash(lowered)
    with pytest.raises(B3TemporalChapterRunnerError, match="not upward"):
        _verify_b3_resume_semantic_identity_v1(
            expected=expected,
            observed=lowered,
        )

    changed_model = deepcopy(observed)
    changed_model["requested_model_id"] = "gpt-5.5"
    changed_model.pop("semantic_identity_hash")
    changed_model["semantic_identity_hash"] = canonical_hash(changed_model)
    with pytest.raises(B3TemporalChapterRunnerError, match="semantic identity changed"):
        _verify_b3_resume_semantic_identity_v1(
            expected=expected,
            observed=changed_model,
        )


def test_chapter_runtime_profile_seals_two_call_aggregate() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "literary_shared_llm_runtime_openai_b3_temporal_chapter_v1.json"
    )
    profile = load_literary_shared_runtime_profile_v2(
        path,
        expected_role_ids={"literary.b3.temporal_state"},
    )
    preset = profile.role_presets["literary.b3.temporal_state"]
    assert preset.requested_model_id == "gpt-5.4"
    assert preset.limits["max_calls"] == 2
    assert preset.limits["max_total_tokens"] == 56_000


def test_modelapi_call_budget_materializes_three_call_aggregate() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "literary_shared_llm_runtime_modelapi_b3_temporal_chapter_v1.json"
    )
    baseline = load_literary_shared_runtime_profile_v2(
        path,
        expected_role_ids={"literary.b3.temporal_state"},
    )

    effective = bind_b3_runtime_call_budget_v1(
        baseline,
        max_calls=3,
    )

    assert baseline.role_presets["literary.b3.temporal_state"].limits[
        "max_calls"
    ] == 1
    single_call = bind_b3_runtime_call_budget_v1(
        baseline,
        max_calls=1,
    )
    assert single_call.profile_sha256 == baseline.profile_sha256
    assert effective.role_presets["literary.b3.temporal_state"].limits == {
        "max_calls": 3,
        "max_prompt_tokens": 60_000,
        "max_completion_tokens": 24_000,
        "max_total_tokens": 84_000,
        "max_cost_usd": None,
        "request_timeout_ms": 300_000,
    }
    assert effective.profile_sha256 != baseline.profile_sha256
    public = effective.public_payload()
    body = {key: value for key, value in public.items() if key != "profile_sha256"}
    assert canonical_sha256(body) == effective.profile_sha256


@pytest.mark.parametrize("max_calls", [0, -1, True])
def test_b3_call_budget_rejects_invalid_caps(max_calls) -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "literary_shared_llm_runtime_modelapi_b3_temporal_chapter_v1.json"
    )
    profile = load_literary_shared_runtime_profile_v2(
        path,
        expected_role_ids={"literary.b3.temporal_state"},
    )

    with pytest.raises(B3TemporalChapterRunnerError, match="positive integer"):
        bind_b3_runtime_call_budget_v1(
            profile,
            max_calls=max_calls,
        )


def test_chapter_runner_rejects_call_cap_above_context_ceiling(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "b2_source"
    source_root.mkdir()

    with pytest.raises(
        B3TemporalChapterRunnerError,
        match=r"sealed=3, ceiling=2",
    ):
        prepare_b3_temporal_chapter_run_v1(
            b2_run_root=source_root,
            prior_b3_roots=[],
            output_root=tmp_path / "output",
            profile=_profile(tmp_path),
            shared_runtime=None,
            current_git_head="a" * 40,
            max_calls=3,
        )


def test_chapter_runner_reports_required_and_sealed_call_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "b2_source"
    source_root.mkdir()
    monkeypatch.setattr(
        runner_module,
        "load_b2_temporal_input_v1",
        lambda _root: _two_component_input(),
    )

    with pytest.raises(
        B3TemporalChapterRunnerError,
        match=r"required=2, sealed=1",
    ):
        prepare_b3_temporal_chapter_run_v1(
            b2_run_root=source_root,
            prior_b3_roots=[],
            output_root=tmp_path / "output",
            profile=_profile(tmp_path),
            shared_runtime=None,
            current_git_head="a" * 40,
            max_calls=1,
        )
