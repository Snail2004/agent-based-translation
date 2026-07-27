from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.provider_profile import (
    ProviderProfile,
    load_provider_profile,
    resolve_role_credential,
    resolve_role_credentials,
)

from pipeline.literary.b0_entity_conflict_auditor import (
    build_identity_conflict_manifest,
    render_entity_conflict_request,
    validate_and_apply_conflict_response,
)
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    EXPERIMENT_SCHEMA_VERSION as PRIOR_CHALLENGE_SCHEMA_VERSION,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    build_prior_claim_projection_v1,
    build_prior_claim_revision_ledger_v1,
    build_prior_claim_ticket_index_v1,
    dry_render_prior_claim_requests_v1,
    render_prior_claim_request_v1,
    verify_prior_claim_ticket_index_v1,
)
from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    apply_claim_projection_to_prefix_bundle_v1,
    apply_glossary_dispositions_to_prefix_bundle_v1,
    build_chapter_prefix_prior_bundle_v1,
    extend_chapter_prefix_prior_bundle_v1,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    append_prefix_identity_uncertainties_v1,
    build_chapter_cycle_review_ledger_v1,
    verify_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.chapter_cycle_profile_v1 import load_chapter_cycle_profile
from pipeline.literary.chapter_priority_review_v1 import (
    build_chapter_priority_review_index_v1,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.incremental_identity_auditor_v1 import (
    apply_incremental_identity_ledger_to_prefix_v1,
    apply_incremental_identity_ledger_to_review_v1,
    build_incremental_identity_index_v1,
    build_incremental_identity_ledger_v1,
    render_incremental_identity_request_v1,
    verify_incremental_identity_index_v1,
    verify_incremental_identity_ledger_v1,
)
from pipeline.literary.semantic_candidate_leads_v1 import (
    apply_semantic_candidate_leads_to_prefix_v1,
    build_semantic_candidate_lead_index_from_profile_v1,
    verify_semantic_candidate_lead_index_v1,
)
from pipeline.scripts.run_b0_entity_conflict_auditor_experiment import (
    run_dry as run_local_auditor_dry,
    run_live as run_local_auditor_live,
)
from pipeline.scripts.run_b0_inventory_gemini_comparison import (
    run_dry as run_b0_dry,
    run_live as run_b0_live,
)
from pipeline.scripts.run_b0_prior_challenge_experiment import (
    run_dry as run_prior_b0_dry,
    run_live as run_prior_b0_live,
)
from pipeline.scripts.run_chapter_registry_v4_real import (
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
    RUNTIME_ROOT,
    _load_document,
    _verify_frozen_db,
)
from pipeline.scripts.run_cross_chapter_claim_auditor_v1_live import (
    build_envelope as build_claim_live_envelope,
    run_live as run_claim_component_live,
)
from pipeline.scripts.run_incremental_identity_auditor_v1_live import (
    build_envelope as build_identity_live_envelope,
    run_live as run_identity_component_live,
)


RUN_SCHEMA_VERSION = "literary_two_chapter_registry_run_v1"
STATE_SCHEMA_VERSION = "literary_two_chapter_registry_state_v1"
PLAN_SCHEMA_VERSION = "literary_two_chapter_registry_plan_v1"
DEFAULT_CHAPTER_CYCLE_PROFILE = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_chapter_cycle_profile_v1.json"
)
STATIC_STAGES = (
    "ch1_b0",
    "ch1_local_auditor",
    "ch1_prefix_adapter",
    "ch2_b0_prior",
    "ch2_local_auditor",
    "stable_claim_prepare",
    "stable_claim_components",
    "stable_claim_reconcile",
    "extend_prefix",
    "semantic_leads",
    "incremental_identity_prepare",
    "incremental_identity_components",
    "incremental_identity_reconcile",
    "finalize",
    "complete",
)
LIVE_FAILURE_MARKERS = ("experiment_failure.json", "failure.json")
PROVIDER_ROLE_B0 = "literary_b0"
PROVIDER_ROLE_LOCAL_AUDITOR = "literary_local_conflict_auditor"
PROVIDER_ROLE_STABLE_CLAIM = "literary_stable_claim_auditor"
PROVIDER_ROLE_INCREMENTAL_IDENTITY = "literary_incremental_identity_auditor"


class TwoChapterRegistryRunError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise TwoChapterRegistryRunError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise TwoChapterRegistryRunError(f"{label} must be a JSON object")
    return value


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_file():
        observed = _load_json(path, "existing immutable artifact")
        if canonical_json(observed) != canonical_json(payload):
            raise TwoChapterRegistryRunError(f"immutable artifact differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(path, dict(payload))


def _state_body(state: Mapping[str, Any]) -> dict[str, Any]:
    body = deepcopy(dict(state))
    body.pop("state_hash", None)
    return body


def _verify_state(state: Mapping[str, Any]) -> dict[str, Any]:
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise TwoChapterRegistryRunError("foreign two-chapter state schema")
    body = _state_body(state)
    if canonical_hash(body) != state.get("state_hash"):
        raise TwoChapterRegistryRunError("two-chapter state hash mismatch")
    if state.get("current_stage") not in STATIC_STAGES:
        raise TwoChapterRegistryRunError("two-chapter state has a foreign stage")
    if not isinstance(state.get("stage_receipts"), list):
        raise TwoChapterRegistryRunError("two-chapter receipts must be a list")
    return deepcopy(dict(state))


def _load_state(run_root: Path) -> dict[str, Any]:
    return _verify_state(_load_json(run_root / "run_state.json", "run state"))


def _save_state(run_root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    body = _state_body(state)
    body["updated_at"] = _now()
    result = {**body, "state_hash": canonical_hash(body)}
    generation = int(result["generation"])
    _write_immutable(
        run_root
        / "state_generations"
        / f"{generation:03d}_{result['state_hash'][:12]}.json",
        result,
    )
    write_checkpoint_atomic(run_root / "run_state.json", result)
    return result


def _transition(
    run_root: Path,
    state: Mapping[str, Any],
    *,
    next_stage: str,
    receipt: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if next_stage not in STATIC_STAGES:
        raise TwoChapterRegistryRunError("transition targets a foreign stage")
    body = _state_body(state)
    body["parent_state_hash"] = state["state_hash"]
    body["generation"] = int(state["generation"]) + 1
    body["current_stage"] = next_stage
    body["prepared_envelope_hash"] = None
    body["prepared_component_id"] = None
    if receipt is not None:
        body["stage_receipts"] = [*body["stage_receipts"], deepcopy(dict(receipt))]
    if extra:
        body.update(deepcopy(dict(extra)))
    return _save_state(run_root, body)


def _artifact_receipt(
    *, stage: str, path: Path, status: str, api_calls: int
) -> dict[str, Any]:
    if not path.is_file():
        raise TwoChapterRegistryRunError(f"stage artifact is absent: {path}")
    return {
        "stage": stage,
        "status": status,
        "artifact_path": str(path.resolve()),
        "artifact_sha256": file_sha256(path),
        "api_calls": api_calls,
        "recorded_at": _now(),
    }


def _actual_model_api_calls(*, stage: str, output_dir: Path) -> int:
    if stage in {"ch1_b0", "ch2_b0_prior"}:
        raw = _load_json(output_dir / "raw_result.json", f"{stage} raw result")
        return 0 if raw.get("from_cache") is True else 1
    if stage in {"ch1_local_auditor", "ch2_local_auditor"}:
        report = _load_json(
            output_dir / "experiment_report.json", f"{stage} live report"
        )
        usage = report.get("usage")
        if not isinstance(usage, Mapping):
            raise TwoChapterRegistryRunError("Local Auditor report lacks usage")
        api_calls = usage.get("api_calls")
        if not isinstance(api_calls, int) or api_calls not in {0, 1}:
            raise TwoChapterRegistryRunError("Local Auditor API-call count is invalid")
        return api_calls
    if stage == "stable_claim_components":
        report = _load_json(output_dir / "live_report.json", "claim live report")
        return 0 if report.get("from_cache") is True else 1
    if stage == "incremental_identity_components":
        report = _load_json(output_dir / "live_report.json", "identity live report")
        return 0 if report.get("from_cache") is True else 1
    raise TwoChapterRegistryRunError("cannot derive API calls for a code-only stage")


def initialize_run(
    *,
    run_root: Path,
    document_path: Path,
    design_doc: Path,
    frozen_db: Path,
    first_chapter_id: str,
    second_chapter_id: str,
    max_stable_claim_calls: int = 8,
    max_incremental_identity_calls: int = 4,
    provider_profile_path: Path | None = None,
    chapter_cycle_profile_path: Path = DEFAULT_CHAPTER_CYCLE_PROFILE,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    if run_root.exists() and any(run_root.iterdir()):
        raise TwoChapterRegistryRunError("run root must be absent or empty")
    run_root.mkdir(parents=True, exist_ok=True)
    document = _load_json(document_path, "document")
    chapters = document.get("chapters")
    if not isinstance(chapters, list):
        raise TwoChapterRegistryRunError("document has no chapter list")
    chapter_ids = [str(row.get("chapter_id") or "") for row in chapters]
    if first_chapter_id not in chapter_ids or second_chapter_id not in chapter_ids:
        raise TwoChapterRegistryRunError("selected chapters are absent from document")
    if chapter_ids.index(second_chapter_id) != chapter_ids.index(first_chapter_id) + 1:
        raise TwoChapterRegistryRunError("selected chapters must be contiguous")
    source_manifest = build_book_source_manifest(document)
    frozen_hash = _verify_frozen_db(frozen_db)
    provider_profile = (
        load_provider_profile(provider_profile_path)
        if provider_profile_path is not None
        else None
    )
    chapter_cycle_profile = load_chapter_cycle_profile(chapter_cycle_profile_path)
    b0_model_id = "gemini-3.5-flash"
    if provider_profile is not None:
        b0_role = provider_profile.roles.get(PROVIDER_ROLE_B0)
        if (
            b0_role is None
            or b0_role.provider != "google_genai"
            or b0_role.model_id.rsplit("/", 1)[-1] != "gemini-3.5-flash"
        ):
            raise TwoChapterRegistryRunError(
                f"provider profile role differs from run contract: {PROVIDER_ROLE_B0}"
            )
        b0_model_id = b0_role.model_id
        expected_roles = {
            PROVIDER_ROLE_LOCAL_AUDITOR: ("openai", "gpt-5.4"),
            PROVIDER_ROLE_STABLE_CLAIM: ("openai", "gpt-5.4"),
            PROVIDER_ROLE_INCREMENTAL_IDENTITY: ("openai", "gpt-5.4"),
        }
        for role_id, expected in expected_roles.items():
            role = provider_profile.roles.get(role_id)
            if role is None or (role.provider, role.model_id) != expected:
                raise TwoChapterRegistryRunError(
                    f"provider profile role differs from run contract: {role_id}"
                )
    plan_body = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "document_path": str(document_path.resolve()),
        "document_sha256": file_sha256(document_path),
        "design_doc_path": str(design_doc.resolve()),
        "design_doc_sha256": file_sha256(design_doc),
        "frozen_db_path": str(frozen_db.resolve()),
        "frozen_db_sha256": frozen_hash,
        "first_chapter_id": first_chapter_id,
        "second_chapter_id": second_chapter_id,
        "book_source_manifest_hash": source_manifest["manifest_hash"],
        "state_lineage_id": state_lineage_id_for_manifest(source_manifest),
        "model_roles": {
            "b0": b0_model_id,
            "local_conflict_auditor": "gpt-5.4",
            "stable_claim_auditor": "gpt-5.4",
            "incremental_identity_auditor": "gpt-5.4",
        },
        "provider_profile_path": (
            str(provider_profile.source_path)
            if provider_profile is not None
            else None
        ),
        "provider_profile_sha256": (
            file_sha256(provider_profile.source_path)
            if provider_profile is not None
            else None
        ),
        "provider_profile_id": (
            provider_profile.profile_id if provider_profile is not None else None
        ),
        "provider_profile_hash": (
            provider_profile.profile_hash if provider_profile is not None else None
        ),
        "chapter_cycle_profile_path": str(chapter_cycle_profile.source_path),
        "chapter_cycle_profile_sha256": file_sha256(chapter_cycle_profile.source_path),
        "chapter_cycle_profile_id": chapter_cycle_profile.profile_id,
        "semantic_lead_limits": dict(chapter_cycle_profile.semantic_leads),
        "call_caps": {
            "b0": 2,
            "local_conflict_auditor": 2,
            "stable_claim_auditor": max_stable_claim_calls,
            "incremental_identity_auditor": max_incremental_identity_calls,
        },
        "semantic_failures_are_nonfatal": True,
        "production_publish_performed": False,
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    _write_immutable(run_root / "run_plan.json", plan)
    initial_body = {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_schema_version": RUN_SCHEMA_VERSION,
        "plan_hash": plan["plan_hash"],
        "generation": 0,
        "parent_state_hash": None,
        "current_stage": "ch1_b0",
        "prepared_envelope_hash": None,
        "prepared_component_id": None,
        "pending_claim_component_ids": [],
        "completed_claim_component_ids": [],
        "pending_identity_component_ids": [],
        "completed_identity_component_ids": [],
        "stage_receipts": [],
        "created_at": _now(),
        "updated_at": _now(),
        "production_publish_performed": False,
    }
    initial = {**initial_body, "state_hash": canonical_hash(initial_body)}
    _write_immutable(
        run_root / "state_generations" / f"000_{initial['state_hash'][:12]}.json",
        initial,
    )
    write_checkpoint_atomic(run_root / "run_state.json", initial)
    return initial


def _plan(run_root: Path) -> dict[str, Any]:
    plan = _load_json(run_root / "run_plan.json", "run plan")
    body = dict(plan)
    observed = body.pop("plan_hash", None)
    if canonical_hash(body) != observed:
        raise TwoChapterRegistryRunError("run plan hash mismatch")
    document_path = Path(plan["document_path"])
    design_doc_path = Path(plan["design_doc_path"])
    frozen_db_path = Path(plan["frozen_db_path"])
    if file_sha256(document_path) != plan["document_sha256"]:
        raise TwoChapterRegistryRunError("run document changed after plan sealing")
    if file_sha256(design_doc_path) != plan["design_doc_sha256"]:
        raise TwoChapterRegistryRunError("run design document changed after plan sealing")
    if _verify_frozen_db(frozen_db_path) != plan["frozen_db_sha256"]:
        raise TwoChapterRegistryRunError("frozen database changed after plan sealing")
    profile_path_value = plan.get("provider_profile_path")
    if profile_path_value is not None:
        profile_path = Path(str(profile_path_value))
        if file_sha256(profile_path) != plan.get("provider_profile_sha256"):
            raise TwoChapterRegistryRunError(
                "provider profile changed after plan sealing"
            )
        profile = load_provider_profile(profile_path)
        if (
            profile.profile_id != plan.get("provider_profile_id")
            or profile.profile_hash != plan.get("provider_profile_hash")
        ):
            raise TwoChapterRegistryRunError("provider profile identity drifted")
    chapter_cycle_profile_path = Path(str(plan["chapter_cycle_profile_path"]))
    if file_sha256(chapter_cycle_profile_path) != plan.get(
        "chapter_cycle_profile_sha256"
    ):
        raise TwoChapterRegistryRunError(
            "chapter-cycle profile changed after plan sealing"
        )
    chapter_cycle_profile = load_chapter_cycle_profile(chapter_cycle_profile_path)
    if (
        chapter_cycle_profile.profile_id != plan.get("chapter_cycle_profile_id")
        or dict(chapter_cycle_profile.semantic_leads)
        != plan.get("semantic_lead_limits")
    ):
        raise TwoChapterRegistryRunError(
            "chapter-cycle semantic lead limits drifted"
        )
    source_manifest = build_book_source_manifest(
        _load_json(document_path, "sealed run document")
    )
    if source_manifest["manifest_hash"] != plan["book_source_manifest_hash"]:
        raise TwoChapterRegistryRunError("book source manifest changed after plan sealing")
    if state_lineage_id_for_manifest(source_manifest) != plan["state_lineage_id"]:
        raise TwoChapterRegistryRunError("book lineage changed after plan sealing")
    return plan


def _paths(run_root: Path) -> dict[str, Path]:
    return {
        "ch1_inventory": run_root / "stages" / "ch1_b0" / "live" / "inventory.json",
        "ch1_audited": run_root
        / "stages"
        / "ch1_local_auditor"
        / "live"
        / "conflict_audited_inventory.json",
        "prefix_ch1": run_root / "artifacts" / "chapter_prefix_ch1.json",
        "ch2_prior_artifact": run_root
        / "stages"
        / "ch2_b0_prior"
        / "live"
        / "prior_challenge_artifact.json",
        "ch2_delta": run_root / "artifacts" / "chapter2_delta_inventory.json",
        "ch2_audited": run_root
        / "stages"
        / "ch2_local_auditor"
        / "live"
        / "conflict_audited_inventory.json",
        "claim_prepared": run_root / "stages" / "stable_claim" / "prepared",
        "claim_reconciled": run_root / "stages" / "stable_claim" / "reconciled",
        "prefix_effective_ch1": run_root
        / "artifacts"
        / "chapter_prefix_ch1_effective.json",
        "prefix_effective_ch1_glossary": run_root
        / "artifacts"
        / "chapter_prefix_ch1_effective_glossary.json",
        "prefix_ch2": run_root / "artifacts" / "chapter_prefix_ch2.json",
        "semantic_lead_index": run_root
        / "artifacts"
        / "semantic_candidate_lead_index_ch2.json",
        "prefix_ch2_semantic": run_root
        / "artifacts"
        / "chapter_prefix_ch2_semantic.json",
        "candidate_review_queue": run_root
        / "artifacts"
        / "candidate_review_queue.json",
        "candidate_review_ledger": run_root
        / "artifacts"
        / "candidate_review_ledger.json",
        "candidate_review_ledger_final": run_root
        / "artifacts"
        / "candidate_review_ledger_final.json",
        "identity_prepared": run_root
        / "stages"
        / "incremental_identity"
        / "prepared",
        "identity_reconciled": run_root
        / "stages"
        / "incremental_identity"
        / "reconciled",
        "identity_ledger": run_root
        / "stages"
        / "incremental_identity"
        / "reconciled"
        / "identity_ledger.json",
        "prefix_ch2_reviewed": run_root
        / "artifacts"
        / "chapter_prefix_ch2_identity_reviewed.json",
        "candidate_review_ledger_reviewed": run_root
        / "artifacts"
        / "candidate_review_ledger_identity_reviewed.json",
        "priority_review_index": run_root
        / "artifacts"
        / "chapter_priority_review_index_ch2.json",
        "final_report": run_root / "final_report.json",
    }


def _model_stage_artifact(run_root: Path, stage: str) -> Path:
    paths = _paths(run_root)
    return {
        "ch1_b0": paths["ch1_inventory"],
        "ch1_local_auditor": paths["ch1_audited"],
        "ch2_b0_prior": paths["ch2_prior_artifact"],
        "ch2_local_auditor": paths["ch2_audited"],
    }[stage]


def _ensure_ch2_delta(run_root: Path) -> Path:
    paths = _paths(run_root)
    artifact = _load_json(paths["ch2_prior_artifact"], "chapter-2 prior artifact")
    delta = artifact.get("delta_inventory")
    if not isinstance(delta, Mapping):
        raise TwoChapterRegistryRunError("chapter-2 prior artifact lacks delta inventory")
    _write_immutable(paths["ch2_delta"], delta)
    return paths["ch2_delta"]


def _auto_local_audit_if_clean(
    *, run_root: Path, state: Mapping[str, Any], chapter_id: str, inventory_path: Path
) -> dict[str, Any] | None:
    plan = _plan(run_root)
    document, chapter = _load_document(Path(plan["document_path"]), chapter_id)
    del document
    inventory = _load_json(inventory_path, "local-auditor inventory")
    manifest = build_identity_conflict_manifest(inventory, chapter)
    if manifest["components"] or manifest["glossary_review"]["candidate_cards"]:
        return None
    request = render_entity_conflict_request(
        chapter=chapter,
        inventory=inventory,
        design_doc=Path(plan["design_doc_path"]),
        model_id="gpt-5.4",
        reasoning_effort="none",
        temperature=1.0,
        seed=20260715,
        max_output_tokens=4096,
    )
    audited = validate_and_apply_conflict_response(
        {
            "chapter_id": chapter_id,
            "component_decisions": [],
            "glossary_dispositions": [],
        },
        chapter=chapter,
        inventory=inventory,
        request_fingerprint=request.request_fingerprint,
    )
    stage = state["current_stage"]
    output = (
        run_root
        / "stages"
        / stage
        / "live"
        / "conflict_audited_inventory.json"
    )
    _write_immutable(output, audited)
    next_stage = "ch1_prefix_adapter" if stage.startswith("ch1") else "stable_claim_prepare"
    receipt = _artifact_receipt(
        stage=stage, path=output, status="accepted_clean_code_path", api_calls=0
    )
    return _transition(run_root, state, next_stage=next_stage, receipt=receipt)


def prepare_current_stage(run_root: Path) -> dict[str, Any]:
    run_root = run_root.resolve()
    state = _load_state(run_root)
    plan = _plan(run_root)
    stage = state["current_stage"]
    paths = _paths(run_root)
    if stage in {
        "ch1_prefix_adapter",
        "stable_claim_prepare",
        "stable_claim_reconcile",
        "extend_prefix",
        "incremental_identity_prepare",
        "incremental_identity_reconcile",
        "finalize",
        "complete",
    }:
        return advance_code_stages(run_root)
    if state.get("prepared_envelope_hash"):
        return state
    dry_dir = run_root / "stages" / stage / "dry"
    if stage == "ch1_b0":
        report = run_b0_dry(
            stage="b0",
            document_path=Path(plan["document_path"]),
            design_doc=Path(plan["design_doc_path"]),
            chapter_id=plan["first_chapter_id"],
            inventory_path=None,
            output_dir=dry_dir,
            model_id=plan["model_roles"]["b0"],
        )
        component_id = None
    elif stage in {"ch1_local_auditor", "ch2_local_auditor"}:
        inventory_path = (
            paths["ch1_inventory"]
            if stage == "ch1_local_auditor"
            else _ensure_ch2_delta(run_root)
        )
        auto = _auto_local_audit_if_clean(
            run_root=run_root,
            state=state,
            chapter_id=(
                plan["first_chapter_id"]
                if stage == "ch1_local_auditor"
                else plan["second_chapter_id"]
            ),
            inventory_path=inventory_path,
        )
        if auto is not None:
            return auto
        report = run_local_auditor_dry(
            document_path=Path(plan["document_path"]),
            inventory_path=inventory_path,
            design_doc=Path(plan["design_doc_path"]),
            output_dir=dry_dir,
            chapter_id=(
                plan["first_chapter_id"]
                if stage == "ch1_local_auditor"
                else plan["second_chapter_id"]
            ),
        )
        component_id = None
    elif stage == "ch2_b0_prior":
        report = run_prior_b0_dry(
            document_path=Path(plan["document_path"]),
            design_doc=Path(plan["design_doc_path"]),
            chapter_id=plan["second_chapter_id"],
            prior_cards_path=None,
            prior_bundle_path=paths["prefix_ch1"],
            corruption_manifest_path=None,
            output_dir=dry_dir,
            model_id=plan["model_roles"]["b0"],
        )
        component_id = None
    elif stage == "stable_claim_components":
        pending = list(state["pending_claim_component_ids"])
        if not pending:
            return _transition(
                run_root, state, next_stage="stable_claim_reconcile"
            )
        component_id = pending[0]
        dry_dir = dry_dir / component_id
        dry_dir.mkdir(parents=True, exist_ok=False)
        envelope, preflight, _index, _request = build_claim_live_envelope(
            prepared_dir=paths["claim_prepared"],
            frozen_db=Path(plan["frozen_db_path"]),
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            exclude_root=dry_dir,
            component_id=component_id,
            model_id="gpt-5.4",
        )
        _write_immutable(dry_dir / "run_envelope.json", envelope)
        _write_immutable(dry_dir / "quota_preflight.json", preflight)
        report = {"envelope_hash": envelope["envelope_hash"]}
    elif stage == "incremental_identity_components":
        pending = list(state["pending_identity_component_ids"])
        if not pending:
            return _transition(
                run_root, state, next_stage="incremental_identity_reconcile"
            )
        component_id = pending[0]
        dry_dir = dry_dir / component_id
        dry_dir.mkdir(parents=True, exist_ok=False)
        envelope, preflight, _index, _request = build_identity_live_envelope(
            prepared_dir=paths["identity_prepared"],
            frozen_db=Path(plan["frozen_db_path"]),
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            exclude_root=dry_dir,
            component_id=component_id,
        )
        _write_immutable(dry_dir / "run_envelope.json", envelope)
        _write_immutable(dry_dir / "quota_preflight.json", preflight)
        report = {"envelope_hash": envelope["envelope_hash"]}
    else:
        raise TwoChapterRegistryRunError(f"cannot prepare stage {stage}")
    body = _state_body(state)
    body["parent_state_hash"] = state["state_hash"]
    body["generation"] = int(state["generation"]) + 1
    body["prepared_envelope_hash"] = report["envelope_hash"]
    body["prepared_component_id"] = component_id
    return _save_state(run_root, body)


def _archive_failed_live_attempt(output_dir: Path) -> Path | None:
    """Preserve a failed provider attempt before retrying the same checkpoint."""
    output = output_dir.resolve()
    if not output.exists() or not any(output.iterdir()):
        return None
    failure_marker = next(
        (output / name for name in LIVE_FAILURE_MARKERS if (output / name).is_file()),
        None,
    )
    if failure_marker is None:
        raise TwoChapterRegistryRunError(
            f"live output exists without a retryable failure marker: {output}"
        )
    archive_root = output.parent / "failed_attempts"
    archive_root.mkdir(parents=True, exist_ok=True)
    stem = "attempt" if output.name == "live" else f"{output.name}_attempt"
    attempt = 1
    while (archive_root / f"{stem}_{attempt:03d}").exists():
        attempt += 1
    destination = archive_root / f"{stem}_{attempt:03d}"
    output.rename(destination)
    return destination


def _sealed_provider_profile(plan: Mapping[str, Any]) -> ProviderProfile | None:
    value = plan.get("provider_profile_path")
    return load_provider_profile(Path(str(value))) if value is not None else None


def _credential_root(value: Path | None) -> Path:
    selected = value or (
        Path(os.environ["THESIS_CREDENTIAL_ROOT"])
        if os.environ.get("THESIS_CREDENTIAL_ROOT")
        else None
    )
    if selected is None:
        raise TwoChapterRegistryRunError(
            "provider profile live mode needs --credential-root or "
            "THESIS_CREDENTIAL_ROOT"
        )
    return selected.resolve()


def _profile_openai_key_paths(
    profile: ProviderProfile, *, role_id: str, credential_root: Path
) -> dict[str, Path]:
    resolved = resolve_role_credentials(
        profile, role_id=role_id, credential_root=credential_root
    )
    paths = {row.quota_bucket_id: row.source_path for row in resolved}
    if set(paths) != {"openai-row1", "openai-row2"}:
        raise TwoChapterRegistryRunError(
            f"{role_id} must exact-cover openai-row1 and openai-row2"
        )
    if any(row.nonempty_line != 1 for row in resolved):
        raise TwoChapterRegistryRunError(
            f"{role_id} OpenAI files must each contain the key on row 1"
        )
    return paths


def run_current_live_stage(
    *,
    run_root: Path,
    approved_envelope_hash: str,
    gemini_keys_file: Path | None,
    gemini_bucket_id: str | None,
    openai_key_1: Path | None,
    openai_key_2: Path | None,
    credential_root: Path | None = None,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    state = _load_state(run_root)
    plan = _plan(run_root)
    profile = _sealed_provider_profile(plan)
    if profile is not None and any(
        row is not None for row in (gemini_keys_file, openai_key_1, openai_key_2)
    ):
        raise TwoChapterRegistryRunError(
            "do not mix a sealed provider profile with direct key-file arguments"
        )
    paths = _paths(run_root)
    stage = state["current_stage"]
    if state.get("prepared_envelope_hash") != approved_envelope_hash:
        raise TwoChapterRegistryRunError("approved envelope differs from prepared stage")
    output = run_root / "stages" / stage / "live"
    if stage == "ch1_b0":
        resolved_gemini = None
        if profile is not None:
            resolved_gemini = resolve_role_credential(
                profile,
                role_id=PROVIDER_ROLE_B0,
                credential_root=_credential_root(credential_root),
                quota_bucket_id=gemini_bucket_id,
            )
            gemini_bucket_id = resolved_gemini.quota_bucket_id
        if (gemini_keys_file is None and resolved_gemini is None) or gemini_bucket_id is None:
            raise TwoChapterRegistryRunError("B0 live stage needs Gemini credentials")
        _archive_failed_live_attempt(output)
        run_b0_live(
            stage="b0",
            document_path=Path(plan["document_path"]),
            design_doc=Path(plan["design_doc_path"]),
            frozen_db=Path(plan["frozen_db_path"]),
            chapter_id=plan["first_chapter_id"],
            inventory_path=None,
            output_dir=output,
            approved_envelope_hash=approved_envelope_hash,
            keys_file=gemini_keys_file,
            quota_bucket_id=gemini_bucket_id,
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            gold_path=None,
            resolved_credential=resolved_gemini,
            allowed_quota_bucket_ids=(
                profile.provider_bucket_ids("google_genai")
                if profile is not None
                else None
            ),
            provider_profile_hash=(
                profile.profile_hash if profile is not None else None
            ),
            model_id=plan["model_roles"]["b0"],
        )
        next_stage = "ch1_local_auditor"
    elif stage == "ch2_b0_prior":
        resolved_gemini = None
        if profile is not None:
            resolved_gemini = resolve_role_credential(
                profile,
                role_id=PROVIDER_ROLE_B0,
                credential_root=_credential_root(credential_root),
                quota_bucket_id=gemini_bucket_id,
            )
            gemini_bucket_id = resolved_gemini.quota_bucket_id
        if (gemini_keys_file is None and resolved_gemini is None) or gemini_bucket_id is None:
            raise TwoChapterRegistryRunError("B0 live stage needs Gemini credentials")
        _archive_failed_live_attempt(output)
        run_prior_b0_live(
            document_path=Path(plan["document_path"]),
            design_doc=Path(plan["design_doc_path"]),
            frozen_db=Path(plan["frozen_db_path"]),
            chapter_id=plan["second_chapter_id"],
            prior_cards_path=None,
            prior_bundle_path=paths["prefix_ch1"],
            corruption_manifest_path=None,
            output_dir=output,
            approved_envelope_hash=approved_envelope_hash,
            keys_file=gemini_keys_file,
            quota_bucket_id=gemini_bucket_id,
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            resolved_credential=resolved_gemini,
            allowed_quota_bucket_ids=(
                profile.provider_bucket_ids("google_genai")
                if profile is not None
                else None
            ),
            provider_profile_hash=(
                profile.profile_hash if profile is not None else None
            ),
            model_id=plan["model_roles"]["b0"],
        )
        next_stage = "ch2_local_auditor"
    elif stage in {"ch1_local_auditor", "ch2_local_auditor"}:
        profile_key_paths = (
            _profile_openai_key_paths(
                profile,
                role_id=PROVIDER_ROLE_LOCAL_AUDITOR,
                credential_root=_credential_root(credential_root),
            )
            if profile is not None
            else None
        )
        if profile_key_paths is not None:
            openai_key_1 = profile_key_paths["openai-row1"]
            openai_key_2 = profile_key_paths["openai-row2"]
        if openai_key_1 is None or openai_key_2 is None:
            raise TwoChapterRegistryRunError("Local Auditor needs two OpenAI key paths")
        _archive_failed_live_attempt(output)
        inventory_path = (
            paths["ch1_inventory"]
            if stage == "ch1_local_auditor"
            else _ensure_ch2_delta(run_root)
        )
        run_local_auditor_live(
            document_path=Path(plan["document_path"]),
            inventory_path=inventory_path,
            design_doc=Path(plan["design_doc_path"]),
            frozen_db=Path(plan["frozen_db_path"]),
            output_dir=output,
            approved_envelope_hash=approved_envelope_hash,
            key_paths={"openai-row1": openai_key_1, "openai-row2": openai_key_2},
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            gold_path=None,
            chapter_id=(
                plan["first_chapter_id"]
                if stage == "ch1_local_auditor"
                else plan["second_chapter_id"]
            ),
        )
        next_stage = (
            "ch1_prefix_adapter"
            if stage == "ch1_local_auditor"
            else "stable_claim_prepare"
        )
    elif stage == "stable_claim_components":
        profile_key_paths = (
            _profile_openai_key_paths(
                profile,
                role_id=PROVIDER_ROLE_STABLE_CLAIM,
                credential_root=_credential_root(credential_root),
            )
            if profile is not None
            else None
        )
        if profile_key_paths is not None:
            openai_key_1 = profile_key_paths["openai-row1"]
            openai_key_2 = profile_key_paths["openai-row2"]
        if openai_key_1 is None or openai_key_2 is None:
            raise TwoChapterRegistryRunError("Stable-Claim Auditor needs OpenAI keys")
        component_id = state.get("prepared_component_id")
        component_output = output / str(component_id)
        _archive_failed_live_attempt(component_output)
        run_claim_component_live(
            prepared_dir=paths["claim_prepared"],
            output_dir=component_output,
            frozen_db=Path(plan["frozen_db_path"]),
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            approved_envelope_hash=approved_envelope_hash,
            key_paths={"openai-row1": openai_key_1, "openai-row2": openai_key_2},
            component_id=str(component_id),
            model_id="gpt-5.4",
        )
        pending = list(state["pending_claim_component_ids"])
        if not pending or pending[0] != component_id:
            raise TwoChapterRegistryRunError("claim component queue drifted")
        completed = [*state["completed_claim_component_ids"], pending.pop(0)]
        receipt = _artifact_receipt(
            stage=f"stable_claim_component:{component_id}",
            path=component_output / "decision.json",
            status="accepted_model_decision",
            api_calls=_actual_model_api_calls(
                stage="stable_claim_components", output_dir=component_output
            ),
        )
        return _transition(
            run_root,
            state,
            next_stage=(
                "stable_claim_components" if pending else "stable_claim_reconcile"
            ),
            receipt=receipt,
            extra={
                "pending_claim_component_ids": pending,
                "completed_claim_component_ids": completed,
            },
        )
    elif stage == "incremental_identity_components":
        profile_key_paths = (
            _profile_openai_key_paths(
                profile,
                role_id=PROVIDER_ROLE_INCREMENTAL_IDENTITY,
                credential_root=_credential_root(credential_root),
            )
            if profile is not None
            else None
        )
        if profile_key_paths is not None:
            openai_key_1 = profile_key_paths["openai-row1"]
            openai_key_2 = profile_key_paths["openai-row2"]
        if openai_key_1 is None or openai_key_2 is None:
            raise TwoChapterRegistryRunError("Identity Auditor needs OpenAI keys")
        component_id = state.get("prepared_component_id")
        component_output = output / str(component_id)
        _archive_failed_live_attempt(component_output)
        run_identity_component_live(
            prepared_dir=paths["identity_prepared"],
            output_dir=component_output,
            frozen_db=Path(plan["frozen_db_path"]),
            usage_roots=[run_root, RUNTIME_ROOT / "data"],
            approved_envelope_hash=approved_envelope_hash,
            key_paths={"openai-row1": openai_key_1, "openai-row2": openai_key_2},
            component_id=str(component_id),
        )
        pending = list(state["pending_identity_component_ids"])
        if not pending or pending[0] != component_id:
            raise TwoChapterRegistryRunError("identity component queue drifted")
        completed = [*state["completed_identity_component_ids"], pending.pop(0)]
        receipt = _artifact_receipt(
            stage=f"incremental_identity_component:{component_id}",
            path=component_output / "decision.json",
            status="accepted_model_decision",
            api_calls=_actual_model_api_calls(
                stage="incremental_identity_components", output_dir=component_output
            ),
        )
        return _transition(
            run_root,
            state,
            next_stage=(
                "incremental_identity_components"
                if pending
                else "incremental_identity_reconcile"
            ),
            receipt=receipt,
            extra={
                "pending_identity_component_ids": pending,
                "completed_identity_component_ids": completed,
            },
        )
    else:
        raise TwoChapterRegistryRunError(f"stage {stage} is not a live model stage")
    artifact = _model_stage_artifact(run_root, stage)
    receipt = _artifact_receipt(
        stage=stage,
        path=artifact,
        status="accepted_model_output",
        api_calls=_actual_model_api_calls(stage=stage, output_dir=output),
    )
    return _transition(run_root, state, next_stage=next_stage, receipt=receipt)


def _build_candidate_review_queue(
    *,
    index: Mapping[str, Any],
    prefix: Mapping[str, Any],
    challenge: Mapping[str, Any],
) -> dict[str, Any]:
    challenge_body = deepcopy(dict(challenge))
    observed_artifact_hash = challenge_body.pop("prior_challenge_artifact_hash", None)
    if canonical_hash(challenge_body) != observed_artifact_hash:
        raise TwoChapterRegistryRunError("prior challenge artifact hash mismatch")
    if challenge.get("schema_version") != PRIOR_CHALLENGE_SCHEMA_VERSION:
        raise TwoChapterRegistryRunError("foreign prior challenge artifact schema")
    raw_observations = challenge.get("candidate_only_observations") or []
    if not isinstance(raw_observations, list) or not all(
        isinstance(row, Mapping) for row in raw_observations
    ):
        raise TwoChapterRegistryRunError("candidate-only observations are malformed")
    observations = [deepcopy(dict(row)) for row in raw_observations]
    allowed = {"new_claim_evidence", "possible_collision", "supports_continuity"}
    if any(row.get("observation") not in allowed for row in observations):
        raise TwoChapterRegistryRunError("candidate-only observation has a foreign route")
    candidate_by_id = {
        row["prior_card_id"]: row for row in prefix["candidate_only_context_cards"]
    }
    queued_observations: list[dict[str, Any]] = []
    for row in observations:
        prior_card_id = row.get("prior_card_id")
        candidate = candidate_by_id.get(prior_card_id)
        if candidate is None:
            raise TwoChapterRegistryRunError(
                "candidate-only observation targets no prefix candidate"
            )
        pending_claim_snapshot = None
        if row.get("observation") == "new_claim_evidence":
            pending_by_field = {
                dispute.get("disputed_field"): deepcopy(dict(dispute))
                for dispute in candidate["disputed_claims"]
                if isinstance(dispute, Mapping)
            }
            pending_claim_snapshot = pending_by_field.get(row.get("disputed_field"))
            if pending_claim_snapshot is None:
                raise TwoChapterRegistryRunError(
                    "candidate claim evidence targets no pending prefix field"
                )
        evidence_basis = {
            "state_lineage_id": index.get("state_lineage_id"),
            "prior_card_id": prior_card_id,
            "observation": row.get("observation"),
            "disputed_field": row.get("disputed_field"),
            "chapter_id": challenge.get("chapter_id"),
            "source_block_ids": row.get("source_block_ids"),
        }
        queued_observations.append(
            {
                **row,
                "evidence_manifest_hash": canonical_hash(evidence_basis),
                "pending_claim_snapshot": pending_claim_snapshot,
            }
        )
    body = {
        "schema_version": "two_chapter_candidate_review_queue_v1",
        "ticket_index_hash": index["ticket_index_hash"],
        "identity_referrals": deepcopy(index["identity_referrals"]),
        "pending_identity_reviews": deepcopy(index["uncertainty_rows"]),
        "prefix_identity_uncertainties": deepcopy(
            prefix["prefix_identity_uncertainties"]
        ),
        "candidate_identity_observations": [
            row
            for row in queued_observations
            if row["observation"] in {"possible_collision", "supports_continuity"}
        ],
        "candidate_claim_evidence_queue": [
            row
            for row in queued_observations
            if row["observation"] == "new_claim_evidence"
        ],
        "production_publish_performed": False,
    }
    return {**body, "queue_hash": canonical_hash(body)}


def _pending_identity_uncertainty_count(prefix: Mapping[str, Any]) -> int:
    keys = {
        "prefix:" + str(row.get("uncertainty_id") or canonical_hash(row))
        for row in prefix.get("prefix_identity_uncertainties") or []
        if isinstance(row, Mapping)
    }
    for card in prefix.get("candidate_only_context_cards") or []:
        if not isinstance(card, Mapping):
            continue
        for dispute in card.get("disputed_claims") or []:
            if not isinstance(dispute, Mapping):
                continue
            if dispute.get("disputed_field") != "identity_membership":
                continue
            keys.add(
                "card:"
                + str(card.get("prior_card_id"))
                + ":"
                + str(dispute.get("uncertainty_id") or canonical_hash(dispute))
            )
    return len(keys)


def _supplied_claim_cards(
    *, prefix: Mapping[str, Any], challenge: Mapping[str, Any]
) -> list[dict[str, Any]]:
    supplied_rows = challenge.get("code_derived_prior_presence")
    if not isinstance(supplied_rows, list) or not all(
        isinstance(row, Mapping) for row in supplied_rows
    ):
        raise TwoChapterRegistryRunError("challenge prior-presence rows are malformed")
    supplied_ids = [str(row.get("prior_card_id") or "") for row in supplied_rows]
    if not all(supplied_ids) or len(supplied_ids) != len(set(supplied_ids)):
        raise TwoChapterRegistryRunError("challenge prior-presence ids are malformed")
    claim_by_id = {row["prior_card_id"]: row for row in prefix["claim_cards"]}
    if not set(supplied_ids).issubset(claim_by_id):
        raise TwoChapterRegistryRunError("challenge cites a foreign prior claim card")
    return [deepcopy(claim_by_id[card_id]) for card_id in supplied_ids]


def _prepare_claim_components(run_root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    plan = _plan(run_root)
    paths = _paths(run_root)
    document = _load_json(Path(plan["document_path"]), "document")
    prefix = verify_chapter_prefix_prior_bundle_v1(
        _load_json(paths["prefix_ch1"], "chapter-1 prefix"), document=document
    )
    challenge = _load_json(paths["ch2_prior_artifact"], "chapter-2 challenge")
    supplied_claim_cards = _supplied_claim_cards(prefix=prefix, challenge=challenge)
    index = build_prior_claim_ticket_index_v1(
        document=document,
        prior_cards=supplied_claim_cards,
        challenge_artifacts=[challenge],
        registry_generation_hash=prefix["prefix_bundle_hash"],
        chapter_gists=None,
    )
    prepared = paths["claim_prepared"]
    _write_immutable(prepared / "ticket_index.json", index)
    _write_immutable(
        prepared / "dry_render_report.json",
        dry_render_prior_claim_requests_v1(
            index=index,
            document=document,
            design_doc=Path(plan["design_doc_path"]),
        ),
    )
    component_ids: list[str] = []
    for component in index["claim_components"]:
        if component["overflow"]:
            continue
        rendered = render_prior_claim_request_v1(
            index=index,
            component_id=component["component_id"],
            document=document,
            design_doc=Path(plan["design_doc_path"]),
        )
        request = {
            "component_id": rendered.component_id,
            "request_fingerprint": rendered.request_fingerprint,
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": rendered.response_schema,
            "semantic_payload": rendered.semantic_payload,
        }
        _write_immutable(
            prepared / "components" / component["component_id"] / "request.json",
            request,
        )
        component_ids.append(component["component_id"])
    stable_call_cap = int(plan["call_caps"]["stable_claim_auditor"])
    if len(component_ids) > stable_call_cap:
        raise TwoChapterRegistryRunError(
            "stable-claim component count exceeds the sealed pre-API call cap"
        )
    _write_immutable(
        paths["candidate_review_queue"],
        _build_candidate_review_queue(
            index=index,
            prefix=prefix,
            challenge=challenge,
        ),
    )
    receipt = _artifact_receipt(
        stage="stable_claim_prepare",
        path=prepared / "ticket_index.json",
        status="prepared_no_api",
        api_calls=0,
    )
    return _transition(
        run_root,
        state,
        next_stage=(
            "stable_claim_components" if component_ids else "stable_claim_reconcile"
        ),
        receipt=receipt,
        extra={
            "pending_claim_component_ids": component_ids,
            "completed_claim_component_ids": [],
        },
    )


def _reconcile_claims(run_root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    plan = _plan(run_root)
    paths = _paths(run_root)
    index = verify_prior_claim_ticket_index_v1(
        _load_json(paths["claim_prepared"] / "ticket_index.json", "claim index")
    )
    decisions = [
        _load_json(
            run_root
            / "stages"
            / "stable_claim_components"
            / "live"
            / component_id
            / "decision.json",
            "claim decision",
        )
        for component_id in state["completed_claim_component_ids"]
    ]
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=decisions)
    projection = build_prior_claim_projection_v1(
        prior_cards=index["prior_cards"], ledger=ledger
    )
    _write_immutable(paths["claim_reconciled"] / "claim_revision_ledger.json", ledger)
    _write_immutable(paths["claim_reconciled"] / "claim_projection.json", projection)
    review_ledger = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=index["state_lineage_id"],
        chapter_id=plan["second_chapter_id"],
        candidate_review_queue=_load_json(
            paths["candidate_review_queue"], "candidate review queue"
        ),
        claim_revision_ledger=ledger,
    )
    _write_immutable(paths["candidate_review_ledger"], review_ledger)
    document = _load_json(Path(plan["document_path"]), "document")
    effective = apply_claim_projection_to_prefix_bundle_v1(
        bundle=_load_json(paths["prefix_ch1"], "chapter-1 prefix"),
        projection=projection,
    )
    verify_chapter_prefix_prior_bundle_v1(effective, document=document)
    _write_immutable(paths["prefix_effective_ch1"], effective)
    receipt = _artifact_receipt(
        stage="stable_claim_reconcile",
        path=paths["prefix_effective_ch1"],
        status="reconciled_no_api",
        api_calls=0,
    )
    return _transition(run_root, state, next_stage="extend_prefix", receipt=receipt)


def _prepare_identity_components(
    run_root: Path, state: Mapping[str, Any]
) -> dict[str, Any]:
    plan = _plan(run_root)
    paths = _paths(run_root)
    document = _load_json(Path(plan["document_path"]), "document")
    index = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=_load_json(
            paths["prefix_ch2_semantic"], "semantic-projected chapter-2 prefix"
        ),
        review_ledger=_load_json(
            paths["candidate_review_ledger_final"], "final candidate review ledger"
        ),
    )
    prepared = paths["identity_prepared"]
    _write_immutable(prepared / "identity_index.json", index)
    component_ids: list[str] = []
    for component in index["components"]:
        if component["overflow"] or component["trigger_state"] == "duplicate_suppressed":
            continue
        rendered = render_incremental_identity_request_v1(
            index=index,
            component_id=component["component_id"],
            document=document,
            design_doc=Path(plan["design_doc_path"]),
        )
        request = {
            "component_id": rendered.component_id,
            "request_fingerprint": rendered.request_fingerprint,
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": rendered.response_schema,
            "semantic_payload": rendered.semantic_payload,
        }
        _write_immutable(
            prepared / "components" / component["component_id"] / "request.json",
            request,
        )
        component_ids.append(component["component_id"])
    call_cap = int(plan["call_caps"]["incremental_identity_auditor"])
    if len(component_ids) > call_cap:
        raise TwoChapterRegistryRunError(
            "identity component count exceeds the sealed pre-API call cap"
        )
    receipt = _artifact_receipt(
        stage="incremental_identity_prepare",
        path=prepared / "identity_index.json",
        status="prepared_no_api",
        api_calls=0,
    )
    return _transition(
        run_root,
        state,
        next_stage=(
            "incremental_identity_components"
            if component_ids
            else "incremental_identity_reconcile"
        ),
        receipt=receipt,
        extra={
            "pending_identity_component_ids": component_ids,
            "completed_identity_component_ids": [],
        },
    )


def _reconcile_identity_components(
    run_root: Path, state: Mapping[str, Any]
) -> dict[str, Any]:
    paths = _paths(run_root)
    index = verify_incremental_identity_index_v1(
        _load_json(paths["identity_prepared"] / "identity_index.json", "identity index")
    )
    decisions = [
        _load_json(
            run_root
            / "stages"
            / "incremental_identity_components"
            / "live"
            / component_id
            / "decision.json",
            "identity decision",
        )
        for component_id in state["completed_identity_component_ids"]
    ]
    ledger = build_incremental_identity_ledger_v1(index=index, decisions=decisions)
    reviewed_prefix = apply_incremental_identity_ledger_to_prefix_v1(
        prefix_bundle=_load_json(
            paths["prefix_ch2_semantic"], "semantic-projected chapter-2 prefix"
        ),
        identity_ledger=ledger,
    )
    reviewed_review = apply_incremental_identity_ledger_to_review_v1(
        review_ledger=_load_json(
            paths["candidate_review_ledger_final"], "final review ledger"
        ),
        identity_ledger=ledger,
    )
    _write_immutable(paths["identity_ledger"], ledger)
    _write_immutable(paths["prefix_ch2_reviewed"], reviewed_prefix)
    _write_immutable(paths["candidate_review_ledger_reviewed"], reviewed_review)
    receipt = _artifact_receipt(
        stage="incremental_identity_reconcile",
        path=paths["identity_ledger"],
        status="reconciled_no_api",
        api_calls=0,
    )
    return _transition(run_root, state, next_stage="finalize", receipt=receipt)


def advance_code_stages(run_root: Path) -> dict[str, Any]:
    run_root = run_root.resolve()
    while True:
        state = _load_state(run_root)
        plan = _plan(run_root)
        paths = _paths(run_root)
        stage = state["current_stage"]
        if stage == "ch1_prefix_adapter":
            document = _load_json(Path(plan["document_path"]), "document")
            bundle = build_chapter_prefix_prior_bundle_v1(
                document=document,
                audited_inventory=_load_json(paths["ch1_audited"], "chapter-1 audit"),
                coverage_through_chapter_id=plan["first_chapter_id"],
            )
            _write_immutable(paths["prefix_ch1"], bundle)
            state = _transition(
                run_root,
                state,
                next_stage="ch2_b0_prior",
                receipt=_artifact_receipt(
                    stage=stage,
                    path=paths["prefix_ch1"],
                    status="accepted_code_projection",
                    api_calls=0,
                ),
            )
            continue
        if stage == "stable_claim_prepare":
            state = _prepare_claim_components(run_root, state)
            continue
        if stage == "stable_claim_reconcile":
            state = _reconcile_claims(run_root, state)
            continue
        if stage == "extend_prefix":
            document = _load_json(Path(plan["document_path"]), "document")
            glossary_effective = apply_glossary_dispositions_to_prefix_bundle_v1(
                bundle=_load_json(
                    paths["prefix_effective_ch1"], "effective chapter-1 prefix"
                ),
                challenge_artifact=_load_json(
                    paths["ch2_prior_artifact"], "chapter-2 prior artifact"
                ),
            )
            _write_immutable(paths["prefix_effective_ch1_glossary"], glossary_effective)
            extended = extend_chapter_prefix_prior_bundle_v1(
                bundle=_load_json(
                    paths["prefix_effective_ch1_glossary"],
                    "glossary-effective chapter-1 prefix",
                ),
                document=document,
                audited_inventory=_load_json(paths["ch2_audited"], "chapter-2 audit"),
                next_chapter_id=plan["second_chapter_id"],
            )
            _write_immutable(paths["prefix_ch2"], extended)
            state = _transition(
                run_root,
                state,
                next_stage="semantic_leads",
                receipt=_artifact_receipt(
                    stage=stage,
                    path=paths["prefix_ch2"],
                    status="accepted_code_extension",
                    api_calls=0,
                ),
            )
            continue
        if stage == "semantic_leads":
            document = _load_json(Path(plan["document_path"]), "document")
            chapter_cycle_profile = load_chapter_cycle_profile(
                Path(plan["chapter_cycle_profile_path"])
            )
            lead_index = build_semantic_candidate_lead_index_from_profile_v1(
                document=document,
                prefix_bundle=_load_json(paths["prefix_ch2"], "chapter-2 prefix"),
                current_chapter_id=plan["second_chapter_id"],
                chapter_cycle_profile=chapter_cycle_profile,
            )
            _write_immutable(paths["semantic_lead_index"], lead_index)
            semantic_prefix = apply_semantic_candidate_leads_to_prefix_v1(
                prefix_bundle=_load_json(paths["prefix_ch2"], "chapter-2 prefix"),
                lead_index=lead_index,
            )
            _write_immutable(paths["prefix_ch2_semantic"], semantic_prefix)
            extended_review_ledger = append_prefix_identity_uncertainties_v1(
                ledger=_load_json(
                    paths["candidate_review_ledger"], "candidate review ledger"
                ),
                prefix_bundle=semantic_prefix,
                chapter_id=plan["second_chapter_id"],
            )
            _write_immutable(
                paths["candidate_review_ledger_final"], extended_review_ledger
            )
            state = _transition(
                run_root,
                state,
                next_stage="incremental_identity_prepare",
                receipt=_artifact_receipt(
                    stage=stage,
                    path=paths["semantic_lead_index"],
                    status="accepted_code_semantic_projection",
                    api_calls=0,
                ),
            )
            continue
        if stage == "incremental_identity_prepare":
            state = _prepare_identity_components(run_root, state)
            continue
        if stage == "incremental_identity_reconcile":
            state = _reconcile_identity_components(run_root, state)
            continue
        if stage == "finalize":
            document = _load_json(Path(plan["document_path"]), "document")
            final_bundle = verify_chapter_prefix_prior_bundle_v1(
                _load_json(paths["prefix_ch2_reviewed"], "identity-reviewed prefix"),
                document=document,
            )
            review_queue = _load_json(
                paths["candidate_review_queue"], "candidate review queue"
            )
            review_ledger = verify_chapter_cycle_review_ledger_v1(
                _load_json(
                    paths["candidate_review_ledger_reviewed"],
                    "identity-reviewed candidate review ledger",
                )
            )
            identity_ledger = verify_incremental_identity_ledger_v1(
                _load_json(paths["identity_ledger"], "incremental identity ledger")
            )
            semantic_lead_index = verify_semantic_candidate_lead_index_v1(
                _load_json(paths["semantic_lead_index"], "semantic lead index")
            )
            identity_status_counts = {
                status: sum(
                    row["status"] == status
                    for row in identity_ledger["component_states"]
                )
                for status in ("resolved_distinct", "provisional_link", "pending")
            }
            ch1_audited = _load_json(paths["ch1_audited"], "chapter-1 audit")
            ch2_audited = _load_json(paths["ch2_audited"], "chapter-2 audit")
            ch2_prior_artifact = _load_json(
                paths["ch2_prior_artifact"], "chapter-2 prior artifact"
            )
            priority_review_index = build_chapter_priority_review_index_v1(
                document=document,
                priority_artifacts={
                    plan["first_chapter_id"]: _load_json(
                        paths["ch1_inventory"], "chapter-1 priority artifact"
                    ),
                    plan["second_chapter_id"]: ch2_prior_artifact,
                },
                final_prefix_bundle=final_bundle,
            )
            _write_immutable(paths["priority_review_index"], priority_review_index)
            glossary_lifecycle_counts = {
                lifecycle: sum(
                    row.get("lifecycle_state") == lifecycle
                    for row in final_bundle["glossary_context_cards"]
                )
                for lifecycle in (
                    "chapter_confirmed",
                    "pending_evidence",
                    "rejected_dormant",
                )
            }
            prior_glossary_disposition_counts = {
                verdict: sum(
                    row.get("verdict") == verdict
                    for row in ch2_prior_artifact.get(
                        "prior_glossary_dispositions", []
                    )
                )
                for verdict in ("compatible", "challenge", "uncertain")
            }
            total_calls = sum(int(row["api_calls"]) for row in state["stage_receipts"])
            call_cap = sum(int(value) for value in plan["call_caps"].values())
            if total_calls > call_cap:
                raise TwoChapterRegistryRunError("run exceeded its sealed API call cap")
            report_body = {
                "schema_version": "literary_two_chapter_registry_report_v3",
                "status": "complete_nonproduction_prefix",
                "plan_hash": plan["plan_hash"],
                "state_lineage_id": plan["state_lineage_id"],
                "coverage_through_chapter_id": final_bundle[
                    "coverage_through_chapter_id"
                ],
                "prefix_bundle_hash": final_bundle["prefix_bundle_hash"],
                "active_context_card_count": len(final_bundle["b0_context_cards"]),
                "candidate_only_card_count": len(
                    final_bundle["candidate_only_context_cards"]
                ),
                "glossary_context_card_count": len(
                    final_bundle["glossary_context_cards"]
                ),
                "glossary_lifecycle_counts": glossary_lifecycle_counts,
                "prior_glossary_disposition_counts": (
                    prior_glossary_disposition_counts
                ),
                "chapter_priority_orders": {
                    plan["first_chapter_id"]: list(
                        ch1_audited.get("chapter_priority_order") or []
                    ),
                    plan["second_chapter_id"]: list(
                        ch2_prior_artifact.get("chapter_priority_order")
                        or ch2_audited.get("chapter_priority_order")
                        or []
                    ),
                },
                "pending_identity_uncertainty_count": (
                    _pending_identity_uncertainty_count(final_bundle)
                ),
                "semantic_lead_counts": dict(semantic_lead_index["counts"]),
                "semantic_lead_index_hash": semantic_lead_index[
                    "lead_index_hash"
                ],
                "priority_review_counts": dict(priority_review_index["counts"]),
                "priority_review_index_hash": priority_review_index[
                    "priority_review_index_hash"
                ],
                "candidate_identity_observation_count": len(
                    review_queue["candidate_identity_observations"]
                ),
                "candidate_claim_evidence_count": len(
                    review_queue["candidate_claim_evidence_queue"]
                ),
                "open_review_item_count": sum(
                    row["lifecycle_state"] in {"queued", "book_end_pending"}
                    for row in review_ledger["review_items"]
                ),
                "candidate_review_ledger_hash": review_ledger[
                    "review_ledger_hash"
                ],
                "incremental_identity_ledger_hash": identity_ledger[
                    "identity_ledger_hash"
                ],
                "incremental_identity_status_counts": identity_status_counts,
                "api_call_count": total_calls,
                "api_call_cap": call_cap,
                "frozen_db_sha256_after": _verify_frozen_db(
                    Path(plan["frozen_db_path"])
                ),
                "production_publish_performed": False,
                "completed_at": _now(),
            }
            report = {**report_body, "report_hash": canonical_hash(report_body)}
            _write_immutable(paths["final_report"], report)
            state = _transition(
                run_root,
                state,
                next_stage="complete",
                receipt=_artifact_receipt(
                    stage=stage,
                    path=paths["final_report"],
                    status="complete",
                    api_calls=0,
                ),
            )
            continue
        return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a checkpointed two-chapter literary registry pilot"
    )
    parser.add_argument("mode", choices=("init", "prepare", "live", "advance", "status"))
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--first-chapter-id")
    parser.add_argument("--second-chapter-id")
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--gemini-keys-file", type=Path)
    parser.add_argument("--gemini-bucket-id")
    parser.add_argument("--openai-key-1", type=Path)
    parser.add_argument("--openai-key-2", type=Path)
    parser.add_argument("--provider-profile", type=Path)
    parser.add_argument(
        "--chapter-cycle-profile",
        type=Path,
        default=DEFAULT_CHAPTER_CYCLE_PROFILE,
    )
    parser.add_argument("--credential-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "init":
        if not args.first_chapter_id or not args.second_chapter_id:
            raise TwoChapterRegistryRunError(
                "init requires first and second chapter ids"
            )
        result = initialize_run(
            run_root=args.run_root,
            document_path=args.document,
            design_doc=args.design_doc,
            frozen_db=args.frozen_db,
            first_chapter_id=args.first_chapter_id,
            second_chapter_id=args.second_chapter_id,
            provider_profile_path=args.provider_profile,
            chapter_cycle_profile_path=args.chapter_cycle_profile,
        )
    elif args.mode == "prepare":
        result = prepare_current_stage(args.run_root)
    elif args.mode == "advance":
        result = advance_code_stages(args.run_root)
    elif args.mode == "live":
        if not args.approved_envelope_hash:
            raise TwoChapterRegistryRunError("live requires approved envelope hash")
        result = run_current_live_stage(
            run_root=args.run_root,
            approved_envelope_hash=args.approved_envelope_hash,
            gemini_keys_file=args.gemini_keys_file,
            gemini_bucket_id=args.gemini_bucket_id,
            openai_key_1=args.openai_key_1,
            openai_key_2=args.openai_key_2,
            credential_root=args.credential_root,
        )
    else:
        result = _load_state(args.run_root)
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
