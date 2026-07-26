from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from pipeline.agents.llm_client import LLMUsage
from pipeline.eval.common_input_v1 import validate_translation_artifact
from pipeline.ingest.document_loader import load_document
from pipeline.memory.store_init import migrate_db
from pipeline.prepass import d2l_project_live_executor_v1 as live_executor
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    canonical_sha256,
    file_sha256,
    validate_component_manifest,
    validate_scoring_handoff_fragment,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import validate_stage_receipt
from pipeline.prepass.d2l_project_campaign_v2 import (
    load_campaign,
    load_project,
    prepare_campaign,
)
from pipeline.prepass.d2l_project_live_executor_v1 import (
    D2LProjectLiveExecutorError,
    _StageObservations,
    _mechanical_quality,
    execute_live_stage,
)
from pipeline.prepass.d2l_project_stage_runner_v1 import (
    _STAGE_PRODUCERS,
    build_component_plan,
    execute_stage,
)
from pipeline.prepass.d2l_repair_resume_v1 import (
    LEGACY_REPAIR_SCOPE_POLICY_ID,
    LEGACY_SCHEMA_VERSION,
    build_chain_repair_receipt,
    build_repair_receipt,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import D2LSharedClientResult
from pipeline.prepass.d2l_terminology_memory_delta_v1 import (
    validate_memory_delta_batch,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    run_from_plan_file,
)
from pipeline.tests.test_d2l_project_campaign_v2 import (
    CODE_REVISION,
    CREATED_AT,
    _fixture_job,
)
from pipeline.translate import d2l_translation_quality_auditor_v3 as quality_contract
from pipeline.translate import d2l_translation_semantic_repair_v1 as repair_contract


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


@pytest.mark.parametrize(
    "legacy_parent",
    [False, True],
    ids=["v4-parent", "v3-parent"],
)
def test_live_executor_accepts_only_fully_indexed_chained_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_parent: bool,
) -> None:
    root = tmp_path / "component"
    parent_ref = "runtime/repair_receipts/repair_a0005.json"
    current_ref = "runtime/repair_receipts/repair_chain_a0006.json"
    parent = build_repair_receipt(
        workflow_run_id="wf_chain",
        component_run_id="tr_chain",
        previous_component_attempt_id=4,
        stage_id="b1_candidate_discovery",
        checkpoint_ref="checkpoints/checkpoint_a4.json",
        checkpoint_sha256="A" * 64,
        reason_code="parent_fix",
        baseline_code_revision="1" * 40,
        effective_code_revision="2" * 40,
        semantic_contract_sha256="B" * 64,
        runner_plan_sha256="C" * 64,
        git_delta_sha256="D" * 64,
        changed_paths=[
            (
                "THESIS_RUNTIME_TOOL/pipeline/prepass/"
                "d2l_translation_component_runner_v1.py"
            )
        ],
        created_at="2026-07-25T00:00:00Z",
    )
    if legacy_parent:
        parent["schema_version"] = LEGACY_SCHEMA_VERSION
        parent["repair_scope_policy_id"] = LEGACY_REPAIR_SCOPE_POLICY_ID
        parent.pop("integrity", None)
        parent["integrity"] = {
            "payload_sha256": canonical_sha256(parent),
        }
    _write_json(root / parent_ref, parent)
    parent_sha = file_sha256(root / parent_ref)
    current = build_chain_repair_receipt(
        workflow_run_id="wf_chain",
        component_run_id="tr_chain",
        previous_component_attempt_id=5,
        stage_id="b1_candidate_discovery",
        checkpoint_ref="checkpoints/checkpoint_a5.json",
        checkpoint_sha256="E" * 64,
        reason_code="chain_fix",
        sealed_code_revision="1" * 40,
        baseline_code_revision="2" * 40,
        effective_code_revision="3" * 40,
        parent_repair_artifact_ref="art_component_repair_a0005",
        parent_repair_receipt_ref=parent_ref,
        parent_repair_receipt_sha256=parent_sha,
        parent_effective_code_revision="2" * 40,
        semantic_contract_sha256="B" * 64,
        runner_plan_sha256="C" * 64,
        git_delta_sha256="F" * 64,
        changed_paths=[
            "THESIS_RUNTIME_TOOL/app/backend/services/thesis_runs.py",
            (
                "THESIS_RUNTIME_TOOL/pipeline/prepass/"
                "d2l_project_live_executor_v1.py"
            ),
        ],
        created_at="2026-07-25T00:01:00Z",
    )
    _write_json(root / current_ref, current)
    current_sha = file_sha256(root / current_ref)
    artifacts = [
        {
            "artifact_ref": "art_component_repair_a0005",
            "artifact_kind": "d2l_component_repair_receipt",
            "schema_version": parent["schema_version"],
            "sha256": parent_sha,
            "sha256_kind": "physical",
            "component_attempt_id": 5,
            "relative_path": parent_ref,
            "parent_artifact_refs": [],
            "metadata": {
                "repair_kind": parent["repair_kind"],
                "baseline_code_revision": parent["baseline_code_revision"],
                "effective_code_revision": parent["effective_code_revision"],
            },
        },
        {
            "artifact_ref": "art_component_repair_chain_a0006",
            "artifact_kind": "d2l_component_repair_receipt",
            "schema_version": current["schema_version"],
            "sha256": current_sha,
            "sha256_kind": "physical",
            "component_attempt_id": 6,
            "relative_path": current_ref,
            "parent_artifact_refs": ["art_component_repair_a0005"],
            "metadata": {
                "repair_kind": current["repair_kind"],
                "baseline_code_revision": current["baseline_code_revision"],
                "effective_code_revision": current["effective_code_revision"],
                "parent_repair_artifact_ref": "art_component_repair_a0005",
            },
        },
    ]
    _write_json(root / "component_manifest.json", {"component_attempt_id": 6})
    _write_json(root / "artifact_index.json", {"artifacts": artifacts})
    monkeypatch.setattr(
        live_executor,
        "validate_component_manifest",
        lambda value: value,
    )
    monkeypatch.setattr(
        live_executor,
        "validate_artifact_index",
        lambda value, **_kwargs: value,
    )
    monkeypatch.setenv("THESIS_D2L_EFFECTIVE_CODE_REVISION", "3" * 40)
    monkeypatch.setenv("THESIS_D2L_REPAIR_RECEIPT_REF", current_ref)
    monkeypatch.setenv("THESIS_D2L_REPAIR_RECEIPT_SHA256", current_sha)
    campaign = {
        "config": {
            "workflow_run_id": "wf_chain",
            "component_run_id": "tr_chain",
            "code_revision": "1" * 40,
        }
    }
    assert live_executor._effective_code_revision(
        campaign=campaign,
        component_root=root,
    ) == "3" * 40

    artifacts[1]["parent_artifact_refs"] = []
    _write_json(root / "artifact_index.json", {"artifacts": artifacts})
    with pytest.raises(
        D2LProjectLiveExecutorError,
        match="artifact binding mismatch",
    ):
        live_executor._effective_code_revision(
            campaign=campaign,
            component_root=root,
        )


def _upgrade_fixture_runtime_db(job: Path) -> None:
    database = job / "memory.sqlite3"
    database.unlink()
    load_document(database, job / "source_package_snapshot" / "document.json")
    migrate_db(database)
    manifest_path = job / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    body = dict(manifest)
    body.pop("manifest_payload_sha256")
    body["initial_runtime_db_sha256"] = file_sha256(database).lower()
    body["manifest_payload_sha256"] = canonical_sha256(body).lower()
    _write_json(manifest_path, body)


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    values: list[dict] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


class _FakeClient:
    uses_shared_backend = True

    def __init__(self, transport: "_FakeTransport", role_id: str) -> None:
        self.transport = transport
        self.role_id = role_id
        self.preset = SimpleNamespace(role_id=role_id)
        self.transport_identity = f"fake-transport-{role_id}"
        self.config = SimpleNamespace(
            model=transport.models[role_id],
            temperature=0.0,
            seed=7,
            reasoning_effort="none",
            verbosity="low",
            max_output_tokens=4096,
            daily_token_cap=1_000_000,
        )

    def call(
        self,
        messages,
        *,
        response_format=None,
        tag="",
        semantic_attempt_index=1,
        **_kwargs,
    ):
        self.transport.call_count += 1
        self.transport.calls.append(
            {
                "role_id": self.role_id,
                "tag": tag,
                "response_format": response_format,
                "messages": deepcopy(messages),
                "semantic_attempt_index": semantic_attempt_index,
            }
        )
        payload = self.transport.response(self.role_id, messages, tag)
        finish_reason = "stop"
        if payload is _TRUNCATED_JSON:
            parsed = None
            text = '{"chapter_id":"truncated'
            json_error = "synthetic_truncated_json"
            finish_reason = "length"
        elif payload is _INVALID_JSON:
            parsed = None
            text = "not-json"
            json_error = "synthetic_invalid_json"
        elif isinstance(payload, _FencedJsonPayload):
            parsed = None
            text = (
                "```json\n"
                + json.dumps(payload.value, ensure_ascii=False)
                + "\n```"
            )
            json_error = None
            payload = payload.value
        else:
            parsed = payload
            text = json.dumps(payload, ensure_ascii=False)
            json_error = None
        return D2LSharedClientResult(
            text=text,
            parsed_json=parsed,
            json_error=json_error,
            model=self.config.model,
            system_fingerprint="fake-fingerprint",
            usage=LLMUsage(
                prompt_tokens=100,
                cached_tokens=0,
                completion_tokens=25,
                reasoning_tokens=0,
            ),
            cost_usd=0.001,
            cost_status="provider_actual",
            latency_ms=5,
            from_cache=False,
            cache_key=f"fake-{self.transport.call_count}",
            seal_sha256="a" * 64,
            artifact_sha256="b" * 64,
            response_payload=parsed or {},
            logical_request_id=f"lr_fake_{self.transport.call_count:06d}",
            physical_attempt_index=1,
            provider_id="fake_provider",
            source_id="fake_source",
            masked_quota_bucket="fake-bucket",
            finish_reason=finish_reason,
            cache_status="miss",
            cache_mechanism="local_exact_cache",
            provider_cached_input_tokens=0,
        )


_INVALID_JSON = object()
_TRUNCATED_JSON = object()


class _FencedJsonPayload:
    def __init__(self, value: dict) -> None:
        self.value = value


def test_stage_observations_emit_truthful_transport_retry_summary(
    tmp_path: Path,
) -> None:
    campaign = {
        "config": {
            "workflow_run_id": "wf_transport_observation",
            "component_run_id": "component_transport_observation",
        }
    }
    observations = _StageObservations(
        campaign=campaign,
        component_root=tmp_path,
        component_attempt_id=1,
        stage_id="b2_admission_translation",
        agent="b2",
        work_kind="packet",
        work_id="packet_1",
    )
    observe = observations.transport_observer(work_id="packet_1")
    observe(
        "attempt_failed",
        {
            "attempt_usage_id": "usage_failed_1",
            "logical_request_id": "request_transport_1",
            "semantic_attempt_index": 1,
            "transport_retry_ordinal": 0,
            "physical_attempt_index": 1,
            "provider_id": "provider",
            "model_id": "model",
            "source_id": "source",
            "source_revision": "source_v1",
            "masked_quota_bucket": "bucket-***",
            "latency_ms": 10,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "cost_status": "unknown",
            "reason_code": "http_500",
            "retry_class": "server_unavailable",
            "retry_disposition": "transport_retry_allowed",
        },
    )
    observe(
        "retry_scheduled",
        {
            "logical_request_id": "request_transport_1",
            "retry_index": 1,
            "retry_max": 2,
            "reason_code": "server_unavailable",
        },
    )
    observations.response(
        result=D2LSharedClientResult(
            text='{"ok":true}',
            parsed_json={"ok": True},
            json_error=None,
            model="model",
            system_fingerprint="fingerprint",
            usage=LLMUsage(
                prompt_tokens=10,
                cached_tokens=0,
                completion_tokens=2,
                reasoning_tokens=0,
            ),
            cost_usd=0.001,
            cost_status="provider_actual",
            latency_ms=12,
            from_cache=False,
            cache_key="cache-key",
            seal_sha256="a" * 64,
            artifact_sha256="b" * 64,
            response_payload={"ok": True},
            logical_request_id="request_transport_1",
            physical_attempt_index=2,
            provider_id="provider",
            source_id="source",
            masked_quota_bucket="bucket-***",
            finish_reason="stop",
            cache_status="miss",
            cache_mechanism="local_exact_cache",
            attempt_usage_id="usage_success_2",
            semantic_attempt_index=1,
            transport_retry_ordinal=1,
            provider_called=True,
            source_revision="source_v1",
            provider_cached_input_tokens=None,
            transport_retry_summary={
                "logical_request_id": "request_transport_1",
                "retry_count": 1,
                "outcome": "recovered",
                "reason_codes": ["server_unavailable"],
            },
        ),
        work_id="packet_1",
    )
    receipt = observations.receipt(
        campaign=campaign,
        component_attempt_id=1,
        producer="b2",
        work_id="packet_1",
    )

    assert [row["event"] for row in receipt["observations"]] == [
        "request_sent",
        "transport_attempt_failed",
        "retry",
        "request_sent",
        "response_received",
        "usage_snapshot",
        "retry_summary",
        "cost_snapshot",
    ]
    cost = receipt["observations"][-1]["payload"]
    assert cost["physical_attempt_count"] == 2
    assert cost["cached_input_tokens"] is None
    assert cost["cost_usd"] is None
    assert cost["cost_status"] == "unknown"
    response_usage = receipt["observations"][4]["payload"]["usage"]
    snapshot_usage = receipt["observations"][5]["payload"]["accepted_usage"]["usage"]
    assert response_usage["cached_input_tokens"] is None
    assert snapshot_usage["cached_input_tokens"] is None


class _FakeTransport:
    def __init__(
        self,
        source_by_block: dict[str, str],
        *,
        invalid_b1_once: bool = False,
        fenced_b1_once: bool = False,
        truncated_b1_once: bool = False,
    ):
        self.source_by_block = dict(source_by_block)
        self.invalid_b1_once = invalid_b1_once
        self.fenced_b1_once = fenced_b1_once
        self.truncated_b1_once = truncated_b1_once
        self.call_count = 0
        self.calls: list[dict] = []
        self.models = {
            "d2l.candidate_discovery": "gemini-3.5-flash",
            "d2l.b2.admission": "gpt-5.4",
            "d2l.b2.morphology": "gpt-5.5",
            "d2l.b2.target_collision": "gpt-5.5",
            "d2l.b2.multi_target": "gpt-5.5",
            "d2l.translator.s0": "gpt-5.4",
            "d2l.translator.s1": "gpt-5.4",
            "d2l.translator.s0.semantic_repair": "gpt-5.4",
            "d2l.translator.s1.semantic_repair": "gpt-5.4",
            "d2l.translator.quality_auditor": "gemini-3.5-flash",
        }

    def build_client(self, role_id: str, **_kwargs):
        return _FakeClient(self, role_id)

    def response(self, role_id: str, messages: list[dict], tag: str):
        user = "\n".join(
            str(row.get("content") or "") for row in messages if row.get("role") == "user"
        )
        if role_id == "d2l.candidate_discovery":
            if self.truncated_b1_once:
                self.truncated_b1_once = False
                return _TRUNCATED_JSON
            if self.invalid_b1_once:
                self.invalid_b1_once = False
                return _INVALID_JSON
            chapter_id = user.split("CHAPTER_ID\n", 1)[1].split("\n", 1)[0]
            window_id = user.split("WINDOW_ID\n", 1)[1].split("\n", 1)[0]
            block_id = next(
                block_id
                for block_id, text in self.source_by_block.items()
                if "technical definition" in text
            )
            response = {
                "chapter_id": chapter_id,
                "window_id": window_id,
                "candidate_observations": [
                    {
                        "source_surface": "technical definition",
                        "anchor_block_ids": [block_id],
                    }
                ],
            }
            if self.fenced_b1_once:
                self.fenced_b1_once = False
                return _FencedJsonPayload(response)
            return response
        if role_id == "d2l.b2.admission":
            packet = next(
                value
                for value in _json_objects(user)
                if "packet_id" in value and "candidates" in value
            )
            decisions = []
            for candidate in packet["candidates"]:
                decisions.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "decision": "admit",
                        "canonical_source": candidate["surfaces"][0],
                        "directive": "translate",
                        "primary_target_vi": "định nghĩa kỹ thuật",
                        "primary_use": None,
                        "alternates": [],
                        "evidence_block_ids": [candidate["evidence_block_ids"][0]],
                        "rationale": "The expression denotes a reusable technical concept.",
                    }
                )
            return {"packet_id": packet["packet_id"], "decisions": decisions}
        if role_id.startswith("d2l.b2."):
            raise AssertionError(f"clean singleton unexpectedly called {role_id}")
        if role_id in {"d2l.translator.s0", "d2l.translator.s1"}:
            translations = {}
            for line in user.splitlines():
                match = re.match(r"^\[(T\d+)\]\s?(.*)$", line)
                if match:
                    translations[match.group(1)] = _fake_translation(match.group(2))
            return {"translations": translations}
        if role_id == "d2l.translator.quality_auditor":
            packet = next(
                value
                for value in _json_objects(user)
                if "window_id" in value and "blocks" in value
            )
            return {
                "contract_version": quality_contract.RESPONSE_CONTRACT_VERSION,
                "window_id": packet["window_id"],
                "audited_block_ids": [row["block_id"] for row in packet["blocks"]],
                "findings": [],
            }
        raise AssertionError(f"unexpected fake role: {role_id} ({tag})")


class _SplitB1Transport(_FakeTransport):
    def __init__(self, source_by_block: dict[str, str]) -> None:
        super().__init__(source_by_block)
        self.full_window_truncated = True

    def response(self, role_id: str, messages: list[dict], tag: str):
        if role_id != "d2l.candidate_discovery":
            return super().response(role_id, messages, tag)
        user = "\n".join(
            str(row.get("content") or "")
            for row in messages
            if row.get("role") == "user"
        )
        window_id = user.split("WINDOW_ID\n", 1)[1].split("\n", 1)[0]
        if self.full_window_truncated and ".part_" not in window_id:
            self.full_window_truncated = False
            return _TRUNCATED_JSON
        marker = next(
            line
            for line in user.splitlines()
            if line.startswith("[") and "] " in line
        )
        block_id, _source = marker[1:].split("] ", 1)
        return {
            "chapter_id": user.split("CHAPTER_ID\n", 1)[1].split("\n", 1)[0],
            "window_id": window_id,
            "candidate_observations": [
                {
                    "source_surface": "dense-term",
                    "anchor_block_ids": [block_id],
                }
            ],
        }


class _SemanticRepairTransport(_FakeTransport):
    def response(self, role_id: str, messages: list[dict], tag: str):
        user = "\n".join(
            str(row.get("content") or "")
            for row in messages
            if row.get("role") == "user"
        )
        if (
            role_id == "d2l.translator.s0"
            and "A technical definition." in user
        ):
            payload = super().response(role_id, messages, tag)
            slot_id = _slot_id_for_source(user, "A technical definition.")
            payload["translations"][slot_id] = "Nội dung sai."
            return payload
        if role_id == "d2l.translator.quality_auditor":
            packet = next(
                value
                for value in _json_objects(user)
                if "window_id" in value and "blocks" in value
            )
            bad = next(
                (
                    row
                    for row in packet["blocks"]
                    if row["target_full_text"] == "Nội dung sai."
                ),
                None,
            )
            return {
                "contract_version": quality_contract.RESPONSE_CONTRACT_VERSION,
                "window_id": packet["window_id"],
                "audited_block_ids": [
                    row["block_id"] for row in packet["blocks"]
                ],
                "findings": (
                    [
                        {
                            "block_id": bad["block_id"],
                            "issue_type": "meaning_omission",
                            "severity": "major",
                            "source_evidence": bad["source_full_text"],
                            "target_evidence": "",
                            "reason": "The candidate omits the technical definition.",
                        }
                    ]
                    if bad is not None
                    else []
                ),
            }
        if role_id.endswith(".semantic_repair"):
            packet = next(
                value
                for value in _json_objects(user)
                if value.get("contract_version")
                == repair_contract.INPUT_CONTRACT_VERSION
            )
            return {
                "contract_version": repair_contract.RESPONSE_CONTRACT_VERSION,
                "window_id": packet["window_id"],
                "repairs": [
                    {
                        "block_id": block_id,
                        "repaired_target_protected_text": _fake_translation(
                            self.source_by_block[block_id]
                        ),
                    }
                    for block_id in packet["output_block_ids"]
                ],
            }
        return super().response(role_id, messages, tag)


class _RetryConsumedSemanticMajorTransport(_SemanticRepairTransport):
    def __init__(self, source_by_block: dict[str, str]) -> None:
        super().__init__(source_by_block)
        self.initial_s0_calls = 0

    def response(self, role_id: str, messages: list[dict], tag: str):
        user = "\n".join(
            str(row.get("content") or "")
            for row in messages
            if row.get("role") == "user"
        )
        payload = super().response(role_id, messages, tag)
        if (
            role_id == "d2l.translator.s0"
            and "A technical definition." in user
        ):
            self.initial_s0_calls += 1
            if self.initial_s0_calls == 1:
                slot_id = _slot_id_for_source(user, "A technical definition.")
                payload["translations"][slot_id] = "هنوز"
        return payload


def _slot_id_for_source(user: str, source: str) -> str:
    for line in user.splitlines():
        match = re.match(r"^\[(T\d+)\]\s?(.*)$", line)
        if match and source in match.group(2):
            return match.group(1)
    raise AssertionError(f"source is absent from translator slots: {source!r}")


def _fake_translation(value: str) -> str:
    replacements = {
        "Alpha": "An-pha",
        "A technical definition.": "Một định nghĩa kỹ thuật.",
        "A long explanation.": "Một lời giải thích dài.",
        "Structured table text.": "Văn bản bảng có cấu trúc.",
        "Ordinary prose.": "Văn xuôi thông thường.",
    }
    result = str(value)
    for source, target in replacements.items():
        result = result.replace(source, target)
    return result


def _prepared(tmp_path: Path) -> tuple[Path, Path, dict, object, list[dict], dict]:
    job = _fixture_job(tmp_path)
    _upgrade_fixture_runtime_db(job)
    campaign_root = tmp_path / "campaign"
    prepare_campaign(
        job_root=job,
        campaign_root=campaign_root,
        workflow_run_id="wf_live_executor_fixture",
        component_run_id="tr_live_executor_fixture",
        code_revision=CODE_REVISION,
        require_clean_code=False,
        chapter_ids=["alpha_unit", "gamma_unit"],
        created_at=CREATED_AT,
    )
    plan = build_component_plan(
        campaign_root=campaign_root,
        job_root=job,
        code_root=Path(__file__).resolve().parents[2],
        dry_run=False,
        runtime_root=tmp_path / "runtime",
        credential_files={
            "credential.modelapi_shared_v1": tmp_path / "modelapi.key",
            "credential.shopaikey_gemini_proxy_v1": tmp_path / "shopapi.key",
        },
    )
    campaign = load_campaign(campaign_root)
    project = load_project(job, verify_tree=True)
    rows = [
        dict(row)
        for row in project.block_rows
        if row["chapter_id"] in campaign["config"]["selected_chapter_ids"]
    ]
    transport = _FakeTransport(
        {str(row["block_id"]): str(row["clean_text"] or row["source_text"]) for row in rows},
        invalid_b1_once=True,
    )
    return job, campaign_root, campaign, project, rows, {"plan": plan, "transport": transport}


def test_b1_accepts_one_whole_response_json_fence_without_semantic_retry(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    stage = next(
        item for item in plan.stages if item.stage_id == "b1_candidate_discovery"
    )
    transport = support["transport"]
    transport.invalid_b1_once = False
    transport.fenced_b1_once = True

    payloads = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=1,
        producer=stage.producer,
        work_id=stage.work_id,
    )

    candidate_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.candidate_discovery"
    ]
    assert candidate_calls
    assert all(row["tag"].endswith(".semantic_1") for row in candidate_calls)
    assert "art_b1_candidate_discovery" in payloads
    receipt = next(
        value for value in payloads.values() if "observations" in value
    )
    assert not any(
        row["event"] == "retry" for row in receipt["observations"]
    )


def test_b1_truncated_response_retries_once_with_bounded_complete_output(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    stage = next(
        item for item in plan.stages if item.stage_id == "b1_candidate_discovery"
    )
    transport = support["transport"]
    transport.invalid_b1_once = False
    transport.truncated_b1_once = True

    payloads = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=1,
        producer=stage.producer,
        work_id=stage.work_id,
    )

    candidate_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.candidate_discovery"
    ]
    assert candidate_calls[0]["tag"].endswith(".semantic_1")
    assert candidate_calls[1]["tag"].endswith(".semantic_2")
    retry_prompt = candidate_calls[1]["messages"][-1]["content"]
    assert "reached the output limit and was truncated" in retry_prompt
    assert "candidate_observations to at most 48" in retry_prompt
    assert "Omit lower-priority items" in retry_prompt

    receipt = next(
        value for value in payloads.values() if "observations" in value
    )
    retries = [
        row for row in receipt["observations"] if row["event"] == "retry"
    ]
    assert len(retries) == 1
    assert retries[0]["payload"]["reason_code"] == "response_truncated"


def test_b1_truncation_falls_back_to_internal_parts_and_seals_original_work(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    first_window = campaign["universe"]["window_estimates"]["b1"]["windows"][0]
    first_ids = {str(value) for value in first_window["block_ids"]}
    for row in rows:
        if str(row["block_id"]) in first_ids:
            row["clean_text"] = "dense-term " + ("dense source text " * 220)
    first_window["estimated_source_tokens"] = 1500

    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    stage = next(
        item
        for item in plan.stages
        if item.stage_id == "b1_candidate_discovery"
    )
    transport = _SplitB1Transport(
        {
            str(row["block_id"]): str(row["clean_text"] or row["source_text"])
            for row in rows
        }
    )

    payloads = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=1,
        producer=stage.producer,
        work_id=stage.work_id,
    )

    candidate_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.candidate_discovery"
    ]
    original_prefix = "b1_" + first_window["window_id"]
    assert candidate_calls[0]["tag"] == f"{original_prefix}.semantic_1"
    assert any(".part_001.semantic_1" in row["tag"] for row in candidate_calls)
    assert not any(
        row["tag"] == f"{original_prefix}.semantic_2"
        for row in candidate_calls
    )
    receipt = next(
        value for value in payloads.values() if "observations" in value
    )
    retries = [
        row for row in receipt["observations"] if row["event"] == "retry"
    ]
    assert [row["payload"]["reason_code"] for row in retries] == [
        "response_truncated_split"
    ]
    assert any(
        row["event"] == "validation_passed"
        and row["payload"]["subject_ref"] == original_prefix
        and "internal_window_split" in row["payload"]["reason_codes"]
        for row in receipt["observations"]
    )


def test_b1_resume_after_terminal_truncation_starts_with_internal_part(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    first_window = campaign["universe"]["window_estimates"]["b1"]["windows"][0]
    first_ids = {str(value) for value in first_window["block_ids"]}
    for row in rows:
        if str(row["block_id"]) in first_ids:
            row["clean_text"] = "dense-term " + ("dense source text " * 220)
    first_window["estimated_source_tokens"] = 1500

    component_root = campaign_root / "component"
    component_root.mkdir()
    stage = next(
        item
        for item in ComponentPlan.from_mapping(support["plan"]).stages
        if item.stage_id == "b1_candidate_discovery"
    )
    original_tag = f"b1_{first_window['window_id']}"
    prior = _StageObservations(
        campaign=campaign,
        component_root=component_root,
        component_attempt_id=1,
        stage_id=stage.stage_id,
        agent=stage.producer,
        work_kind="windows",
        work_id=stage.work_id,
    )
    prior.validation(
        passed=False,
        validator_id=live_executor.DISCOVERY_VALIDATOR_VERSION,
        subject_ref=original_tag,
        reason_codes=["DiscoveryContractError", "response_truncated"],
        retryable=False,
    )
    transport = _SplitB1Transport(
        {
            str(row["block_id"]): str(row["clean_text"] or row["source_text"])
            for row in rows
        }
    )

    execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=2,
        producer=stage.producer,
        work_id=stage.work_id,
    )

    candidate_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.candidate_discovery"
    ]
    assert candidate_calls[0]["tag"].endswith(".part_001.semantic_1")
    assert not any(
        row["tag"].startswith(f"{original_tag}.semantic_")
        for row in candidate_calls
    )


def test_b1_reuses_durable_work_results_after_process_resume(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    stage = next(
        item
        for item in ComponentPlan.from_mapping(support["plan"]).stages
        if item.stage_id == "b1_candidate_discovery"
    )
    transport = support["transport"]
    transport.invalid_b1_once = False
    first = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=1,
        producer=stage.producer,
        work_id=stage.work_id,
    )
    call_count = transport.call_count

    resumed = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=2,
        producer=stage.producer,
        work_id=stage.work_id,
    )

    assert transport.call_count == call_count
    assert (
        resumed["art_b1_candidate_discovery"]["candidates"]
        == first["art_b1_candidate_discovery"]["candidates"]
    )
    receipt = next(
        value for value in resumed.values() if "observations" in value
    )
    assert any(
        row["event"] == "validation_passed"
        and row["payload"]["reason_codes"] == ["durable_work_item_reused"]
        for row in receipt["observations"]
    )


def test_live_executor_full_stage_chain_is_gold_free_and_exact_cover(tmp_path: Path) -> None:
    job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    source_db_before = file_sha256(job / "memory.sqlite3")
    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    transport = support["transport"]
    by_stage = {stage.stage_id: stage for stage in plan.stages}

    for stage_id in [stage.stage_id for stage in plan.stages]:
        stage = by_stage[stage_id]
        payloads = execute_live_stage(
            campaign=campaign,
            project=project,
            rows=rows,
            stage_id=stage_id,
            component_root=component_root,
            work_db=campaign_root / "state" / "work.sqlite3",
            transport=transport if stage_id in {
                "b1_candidate_discovery",
                "b2_admission_translation",
                "auditor_morphology",
                "auditor_target_collision",
                "auditor_multi_target",
                "translator",
                "translation_quality_audit",
            } else None,
            component_attempt_id=1,
            producer=stage.producer,
            work_id=stage.work_id,
        )
        assert set(payloads) == {spec["artifact_ref"] for spec in stage.artifact_specs}
        for spec in stage.artifact_specs:
            _write_json(
                component_root / spec["relative_path"],
                payloads[spec["artifact_ref"]],
            )

    s0 = validate_translation_artifact(
        json.loads((component_root / "artifacts/translator/s0.json").read_text(encoding="utf-8"))
    )
    s1 = validate_translation_artifact(
        json.loads((component_root / "artifacts/translator/s1.json").read_text(encoding="utf-8"))
    )
    assert s0["coverage"] == s1["coverage"]
    assert s0["coverage"]["source_block_count"] == 4
    assert s0["coverage"]["translated_count"] == 3
    assert s0["coverage"]["review_held_count"] == 1
    fragment = validate_scoring_handoff_fragment(
        json.loads((component_root / "scoring_handoff_fragment.json").read_text(encoding="utf-8"))
    )
    assert [row["arm_id"] for row in fragment["translation_inputs"]] == ["s0", "s1"]
    glossary = json.loads(
        (component_root / "artifacts/glossary_seal/glossary.json").read_text(encoding="utf-8")
    )
    assert glossary["ready_record_count"] == 1
    assert glossary["records"][0]["value"]["canonical_source"] == "technical definition"
    assert re.fullmatch(
        r"[0-9A-F]{64}",
        glossary["records"][0]["value"]["resolution"]["authority_sha256"],
    )
    delta = validate_memory_delta_batch(
        json.loads(
            (component_root / "artifacts/glossary_seal/memory_delta.json").read_text(
                encoding="utf-8"
            )
        )
    )
    assert delta["counts"]["total"] == 1
    quality_state = json.loads(
        (component_root / "artifacts/quality/state.json").read_text(encoding="utf-8")
    )
    assert quality_state["continue_to_scoring"] is True
    assert not any(call["role_id"].startswith("d2l.b2.") and call["role_id"] != "d2l.b2.admission" for call in transport.calls)
    b1_receipt = json.loads(
        (component_root / "artifacts/b1_candidate_discovery/stage_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert any(row["event"] == "retry" for row in b1_receipt["observations"])
    b1_calls = [
        row for row in transport.calls if row["role_id"] == "d2l.candidate_discovery"
    ]
    assert len(b1_calls) >= 2
    assert all(row["semantic_attempt_index"] == 1 for row in b1_calls)
    assert any(row["tag"].endswith(".semantic_2") for row in b1_calls)
    serialized = json.dumps(
        {
            "glossary": glossary,
            "fragment": fragment,
            "quality": quality_state,
            "receipt": b1_receipt,
        },
        ensure_ascii=False,
    ).casefold()
    for forbidden in ("gold", "oracle", "reference_text", "raw_prompt", "raw_response"):
        assert forbidden not in serialized
    assert file_sha256(job / "memory.sqlite3") == source_db_before


def test_quality_major_uses_one_translator_repair_without_second_audit(
    tmp_path: Path,
) -> None:
    job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    transport = _SemanticRepairTransport(
        {
            str(row["block_id"]): str(row["clean_text"] or row["source_text"])
            for row in rows
        }
    )
    for stage in plan.stages:
        payloads = execute_live_stage(
            campaign=campaign,
            project=project,
            rows=rows,
            stage_id=stage.stage_id,
            component_root=component_root,
            work_db=campaign_root / "state" / "work.sqlite3",
            transport=(
                transport
                if stage.stage_id in {
                    "b1_candidate_discovery",
                    "b2_admission_translation",
                    "auditor_morphology",
                    "auditor_target_collision",
                    "auditor_multi_target",
                    "translator",
                    "translation_quality_audit",
                }
                else None
            ),
            component_attempt_id=1,
            producer=stage.producer,
            work_id=stage.work_id,
        )
        for spec in stage.artifact_specs:
            _write_json(
                component_root / spec["relative_path"],
                payloads[spec["artifact_ref"]],
            )

    draft = validate_translation_artifact(
        json.loads(
            (component_root / "artifacts/translator/s0.json").read_text(
                encoding="utf-8"
            )
        )
    )
    final = validate_translation_artifact(
        json.loads(
            (component_root / "artifacts/quality/s0_final.json").read_text(
                encoding="utf-8"
            )
        )
    )
    draft_by_id = {row["block_id"]: row for row in draft["translations"]}
    final_by_id = {row["block_id"]: row for row in final["translations"]}
    state = json.loads(
        (component_root / "artifacts/quality/state.json").read_text(
            encoding="utf-8"
        )
    )
    quality_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.translator.quality_auditor"
    ]
    repair_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.translator.s0.semantic_repair"
    ]

    assert draft_by_id["alpha_b002"]["target_text"] == "Nội dung sai."
    assert final_by_id["alpha_b002"]["target_text"] == "Một định nghĩa kỹ thuật."
    assert len(repair_calls) == 1
    assert len(quality_calls) == len(
        campaign["universe"]["window_estimates"]["translator"]["windows"]
    ) * 2
    assert state["status"] == "completed_after_semantic_repair_unverified"
    assert state["semantic_repair_applied_count"] == 1


def test_quality_uses_independent_semantic_repair_after_mechanical_retry(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    transport = _RetryConsumedSemanticMajorTransport(
        {
            str(row["block_id"]): str(row["clean_text"] or row["source_text"])
            for row in rows
        }
    )
    for stage in plan.stages:
        payloads = execute_live_stage(
            campaign=campaign,
            project=project,
            rows=rows,
            stage_id=stage.stage_id,
            component_root=component_root,
            work_db=campaign_root / "state" / "work.sqlite3",
            transport=(
                transport
                if stage.stage_id in {
                    "b1_candidate_discovery",
                    "b2_admission_translation",
                    "auditor_morphology",
                    "auditor_target_collision",
                    "auditor_multi_target",
                    "translator",
                    "translation_quality_audit",
                }
                else None
            ),
            component_attempt_id=1,
            producer=stage.producer,
            work_id=stage.work_id,
        )
        for spec in stage.artifact_specs:
            _write_json(
                component_root / spec["relative_path"],
                payloads[spec["artifact_ref"]],
            )

    draft = validate_translation_artifact(
        json.loads(
            (component_root / "artifacts/translator/s0.json").read_text(
                encoding="utf-8"
            )
        )
    )
    final = validate_translation_artifact(
        json.loads(
            (component_root / "artifacts/quality/s0_final.json").read_text(
                encoding="utf-8"
            )
        )
    )
    draft_by_id = {row["block_id"]: row for row in draft["translations"]}
    final_by_id = {row["block_id"]: row for row in final["translations"]}
    state = json.loads(
        (component_root / "artifacts/quality/state.json").read_text(
            encoding="utf-8"
        )
    )
    s0_state = next(row for row in state["arms"] if row["arm_id"] == "s0")
    repair_calls = [
        row
        for row in transport.calls
        if row["role_id"] == "d2l.translator.s0.semantic_repair"
    ]

    assert transport.initial_s0_calls == 2
    assert all(
        row["semantic_attempt_index"] == 1
        for row in transport.calls
        if row["role_id"] in {"d2l.translator.s0", "d2l.translator.s1"}
    )
    assert len(repair_calls) == 1
    assert final_by_id["alpha_b002"]["target_text"] != draft_by_id[
        "alpha_b002"
    ]["target_text"]
    assert s0_state["semantic_repair_attempt_count"] == 1
    assert s0_state["mechanical_retry_before_semantic_repair_count"] == 1
    repair = s0_state["repairs"][0]
    assert repair["status"] == "repair_applied_unverified_semantically"
    assert repair["mechanical_retry_consumed"] is True
    assert repair["resolved_integrity_history_count"] >= 1
    repair_user = "\n".join(
        str(row.get("content") or "")
        for row in repair_calls[0]["messages"]
        if row.get("role") == "user"
    )
    packet = next(
        value
        for value in _json_objects(repair_user)
        if value.get("contract_version") == repair_contract.INPUT_CONTRACT_VERSION
    )
    assert [row["block_id"] for row in packet["context_blocks"]] == [
        "alpha_b001",
        "alpha_b002",
    ]
    assert packet["output_block_ids"] == ["alpha_b002"]
    assert packet["active_semantic_findings"][0]["block_id"] == "alpha_b002"
    assert packet["resolved_integrity_history"][0]["block_id"] == "alpha_b002"


def test_quality_resume_reads_translator_attempt_one_and_publishes_attempt_two(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    plan = ComponentPlan.from_mapping(support["plan"])
    transport = support["transport"]
    by_stage = {stage.stage_id: stage for stage in plan.stages}
    for stage in plan.stages:
        if stage.stage_id == "translation_quality_audit":
            break
        payloads = execute_live_stage(
            campaign=campaign,
            project=project,
            rows=rows,
            stage_id=stage.stage_id,
            component_root=component_root,
            work_db=campaign_root / "state" / "work.sqlite3",
            transport=(
                transport
                if stage.stage_id in {
                    "b1_candidate_discovery",
                    "b2_admission_translation",
                    "auditor_morphology",
                    "auditor_target_collision",
                    "auditor_multi_target",
                    "translator",
                }
                else None
            ),
            component_attempt_id=1,
            producer=stage.producer,
            work_id=stage.work_id,
        )
        for spec in stage.artifact_specs:
            _write_json(
                component_root / spec["relative_path"],
                payloads[spec["artifact_ref"]],
            )

    quality_stage = by_stage["translation_quality_audit"]
    payloads = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id=quality_stage.stage_id,
        component_root=component_root,
        work_db=campaign_root / "state" / "work.sqlite3",
        transport=transport,
        component_attempt_id=2,
        producer=quality_stage.producer,
        work_id=quality_stage.work_id,
    )

    assert payloads["art_translation_s0_final"]["artifact_id"].endswith("a0002")
    assert payloads["art_translation_s1_final"]["artifact_id"].endswith("a0002")


def test_live_stage_runner_pauses_before_api_and_validates_fake_b1_receipt(
    tmp_path: Path,
) -> None:
    job, campaign_root, _campaign, _project, rows, support = _prepared(tmp_path)
    source_db_before = file_sha256(job / "memory.sqlite3")
    run_from_plan_file(
        campaign_root / "component_plan.json",
        campaign_root / "component",
        stop_after_stage="preflight",
    )
    assert json.loads(
        (campaign_root / "component/component_manifest.json").read_text(
            encoding="utf-8"
        )
    )["status"] == "paused"
    transport = support["transport"]
    result = execute_stage(
        campaign_root=campaign_root,
        job_root=job,
        stage_id="b1_candidate_discovery",
        dry_run=False,
        transport=transport,
    )
    assert result["execution_mode"] == "live_sealed"
    manifest = validate_component_manifest(
        json.loads(
            (campaign_root / "component/component_manifest.json").read_text(
                encoding="utf-8"
            )
        )
    )
    receipt = json.loads(
        (
            campaign_root
            / "component/artifacts/b1_candidate_discovery/stage_receipt.json"
        ).read_text(encoding="utf-8")
    )
    validated = validate_stage_receipt(
        receipt,
        manifest=manifest,
        stage_id="b1_candidate_discovery",
        producer=_STAGE_PRODUCERS["b1_candidate_discovery"],
        work_id="work_b1_candidate_discovery",
        start_component_seq=0,
    )
    assert any(row["event"] == "request_sent" for row in validated["observations"])
    assert any(row["event"] == "retry" for row in validated["observations"])
    assert file_sha256(job / "memory.sqlite3") == source_db_before


def test_mechanical_quality_detects_math_drift_without_language_judgment() -> None:
    safe, reasons = _mechanical_quality(
        block_id="b_math",
        source=r"The value is $\mathbf{x}$.",
        target=r"Giá trị là $\mathbf{y}$.",
    )
    assert safe is False
    assert "math_bytes_or_order_changed" in reasons


def test_mechanical_quality_allows_only_code_owned_unchanged_blocks() -> None:
    source = r"(**$$f'(x) = \frac{df}{dx},$$**) :eqlabel:`eq_derivative`"
    safe, reasons = _mechanical_quality(
        block_id="b_math_only",
        source=source,
        target=source,
        allow_unchanged=True,
    )
    assert safe is True
    assert reasons == []

    safe, reasons = _mechanical_quality(
        block_id="b_prose",
        source="Translate this sentence.",
        target="Translate this sentence.",
        allow_unchanged=False,
    )
    assert safe is False
    assert "target_equals_source" in reasons


def test_live_executor_b2_invalid_output_fails_closed_without_artifact(
    tmp_path: Path,
) -> None:
    _job, campaign_root, campaign, project, rows, support = _prepared(tmp_path)
    component_root = campaign_root / "component"
    component_root.mkdir()
    transport = support["transport"]
    b1 = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id="b1_candidate_discovery",
        component_root=component_root,
        work_db=campaign_root / "state/work.sqlite3",
        transport=transport,
        component_attempt_id=1,
        producer=_STAGE_PRODUCERS["b1_candidate_discovery"],
        work_id="work_b1_candidate_discovery",
    )
    _write_json(
        component_root / "artifacts/b1_candidate_discovery/candidates.json",
        b1["art_b1_candidate_discovery"],
    )
    index = execute_live_stage(
        campaign=campaign,
        project=project,
        rows=rows,
        stage_id="candidate_index",
        component_root=component_root,
        work_db=campaign_root / "state/work.sqlite3",
        transport=None,
        component_attempt_id=1,
        producer=_STAGE_PRODUCERS["candidate_index"],
        work_id="work_candidate_index",
    )
    _write_json(
        component_root / "artifacts/candidate_index/index.json",
        index["art_candidate_index"],
    )

    original = transport.response
    transport.response = lambda role_id, messages, tag: (
        _INVALID_JSON if role_id == "d2l.b2.admission" else original(role_id, messages, tag)
    )
    with pytest.raises(D2LProjectLiveExecutorError, match="failed local validation"):
        execute_live_stage(
            campaign=campaign,
            project=project,
            rows=rows,
            stage_id="b2_admission_translation",
            component_root=component_root,
            work_db=campaign_root / "state/work.sqlite3",
            transport=transport,
            component_attempt_id=1,
            producer=_STAGE_PRODUCERS["b2_admission_translation"],
            work_id="work_b2_admission_translation",
        )
    assert not (component_root / "artifacts/b2_admission_translation/decisions.json").exists()
