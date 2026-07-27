from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pipeline.llm_backend import canonical_json, canonical_sha256
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.modelapi_b0_chapter_summary_capability_probe_v1 import (
    build_probe_plan_v1 as build_b0_plan,
)
from pipeline.literary.modelapi_b1_enrich_capability_probe_v1 import (
    build_probe_plan_v1 as build_enrich_plan,
)
from pipeline.literary.modelapi_b1_enrich_local_auditor_capability_probe_v1 import (
    build_probe_plan_v1 as build_local_audit_plan,
)
from pipeline.literary.modelapi_b2_json_object_capability_probe_v1 import (
    build_modelapi_b2_probe_plan_v1,
)
from pipeline.literary.modelapi_b2_speaker_recovery_capability_probe_v1 import (
    build_probe_plan_v1 as build_speaker_recovery_plan,
)
from pipeline.literary.modelapi_b3_json_object_capability_probe_v1 import (
    build_probe_plan_v1 as build_b3_plan,
)
from pipeline.literary.modelapi_b3_temporal_auditor_capability_probe_v1 import (
    build_probe_plan_v1 as build_b3_audit_plan,
)
from pipeline.literary.openai_b1_scan_capability_probe_v1 import (
    MODELAPI_PROFILE_PATH,
    MODELAPI_RUNTIME_PROFILE_PATH,
    build_probe_plan_v1 as build_scan_plan,
)


BINDING = {
    "shared_core_revision": "1" * 40,
    "consumer_revision": "2" * 40,
    "consumer_implementation_sha256": "3" * 64,
}
COMMITMENT = "d" * 64
ISSUED_AT = "2026-07-21T00:00:00Z"


def _common(builder: Callable[..., Any], name: str, **kwargs: Any) -> Any:
    return builder(
        probe_run_id=f"model_ref_probe_{name}",
        credential_commitment_sha256=COMMITMENT,
        issued_at_utc=ISSUED_AT,
        implementation_binding=BINDING,
        **kwargs,
    )


def _plans() -> list[Any]:
    return [
        _common(
            build_scan_plan,
            "scan",
            profile_path=MODELAPI_PROFILE_PATH,
            runtime_profile_path=MODELAPI_RUNTIME_PROFILE_PATH,
        ),
        _common(build_enrich_plan, "enrich"),
        _common(build_local_audit_plan, "local_audit"),
        _common(
            build_modelapi_b2_probe_plan_v1,
            "b2_frame",
            probe_name="frame",
        ),
        _common(
            build_modelapi_b2_probe_plan_v1,
            "b2_interaction",
            probe_name="interaction",
        ),
        _common(build_speaker_recovery_plan, "speaker_recovery"),
        _common(build_b3_plan, "b3"),
        _common(build_b3_audit_plan, "b3_audit"),
        _common(build_b0_plan, "b0"),
    ]


@pytest.mark.parametrize("plan", _plans(), ids=lambda plan: plan.seal["role_id"])
def test_active_probe_seals_the_production_local_reference_envelope(plan) -> None:
    assert plan.request["model_reference_mode"] == MODEL_REF_MODE_CLASSIFIED_V1
    assert plan.seal["response_schema"] == plan.response_schema
    assert plan.seal["request_body_sha256"] == canonical_sha256(plan.request_body)
    assert plan.seal["capability_intent"]["schema_sha256"] == canonical_sha256(
        plan.response_schema
    )
    system_text = "\n".join(
        str(row.get("content", ""))
        for row in plan.request_body["messages"]
        if row.get("role") == "system"
    )
    assert "Transport reference rule:" in system_text

    model_bytes = canonical_json(plan.request_body)
    for row in plan.request["model_ref_map"]["entries"]:
        assert row["persistent_id"] not in model_bytes


def test_local_auditor_probe_no_longer_seals_code_owned_manifest_hash() -> None:
    plan = _common(build_local_audit_plan, "local_audit_manifest")
    assert "manifest_hash" not in plan.response_schema["properties"]
    assert "manifest_hash" not in plan.response_schema["required"]
    assert plan.seal["capability_intent"]["schema_sha256"].startswith("d9e08c5d")
