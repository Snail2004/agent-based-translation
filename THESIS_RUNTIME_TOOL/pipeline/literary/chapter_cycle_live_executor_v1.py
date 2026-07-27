"""Live/offline stage backend for the unified N-chapter Literary cycle.

The public Builder stage is B1. Historical prompt, source, and artifact IDs
remain ``b0``/``b0_prior`` and are recorded as implementation provenance.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.provider_profile import (
    ProviderProfile,
    ProviderRole,
    load_provider_profile,
    resolve_role_credential,
    resolve_role_credentials,
)
from pipeline.literary.b0_entity_conflict_auditor import (
    build_identity_conflict_manifest,
    entity_conflict_response_schema,
    normalize_source_boundary_violations,
    render_entity_conflict_request,
    validate_and_apply_conflict_response,
)
from pipeline.literary.b0_entity_inventory_experiment import (
    entity_inventory_response_schema,
    validate_entity_inventory_response,
)
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    EXPERIMENT_SCHEMA_VERSION as PRIOR_CHALLENGE_SCHEMA_VERSION,
    prior_challenge_response_schema,
    validate_prior_challenge_response,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    build_prior_claim_projection_v1,
    build_prior_claim_revision_ledger_v1,
    build_prior_claim_ticket_index_v1,
    dry_render_prior_claim_requests_v1,
    render_prior_claim_request_v1,
    prior_claim_response_schema_v1,
    validate_prior_claim_response_v1,
    verify_prior_claim_ticket_index_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_LEGACY,
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
)
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ApiCallPermit,
    ChapterCycleStage,
    StageExecutionResult,
)
from pipeline.literary.chapter_cycle_profile_v1 import (
    LiteraryChapterCycleProfile,
    load_chapter_cycle_profile,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    append_prefix_identity_uncertainties_v1,
    build_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    apply_claim_projection_to_prefix_bundle_v1,
    apply_glossary_dispositions_to_prefix_bundle_v1,
    build_chapter_prefix_prior_bundle_v1,
    extend_chapter_prefix_prior_bundle_v1,
    verify_chapter_prefix_prior_bundle_v1,
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
    normalize_surface_scope_action_coverage_v1,
    render_incremental_identity_request_v1,
    incremental_identity_response_schema_v1,
    validate_incremental_identity_response_v1,
    verify_incremental_identity_index_v1,
)
from pipeline.literary.review_case_ledger_v1 import (
    apply_identity_surface_decisions_to_review_cases_v1,
    build_review_case_ledger_v1,
    project_ready_cases_to_chapter_review_ledger_v1,
    verify_review_case_ledger_v1,
)
from pipeline.literary.semantic_candidate_leads_v1 import (
    apply_semantic_candidate_leads_to_prefix_v1,
    build_semantic_candidate_lead_index_from_profile_v1,
    compatible_prior_card_ids_from_challenge_v1,
    materialize_waiting_identity_occurrences_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    LiteraryStructuredOutputPolicy,
    StructuredOutputContract,
    load_literary_structured_output_policy,
    resolve_structured_output_contract,
    validate_structured_payload,
)
from pipeline.scripts.run_b0_entity_conflict_auditor_experiment import (
    run_dry as run_local_auditor_dry,
    run_live as run_local_auditor_live,
)
from pipeline.scripts.run_b0_inventory_gemini_comparison import (
    run_dry as run_b1_dry,
    run_live as run_b1_live,
)
from pipeline.scripts.run_b0_prior_challenge_experiment import (
    build_envelope as build_prior_challenge_envelope,
    run_dry as run_b1_prior_dry,
    run_live as run_b1_prior_live,
)
from pipeline.scripts.run_cross_chapter_claim_auditor_v1_live import (
    build_envelope as build_claim_live_envelope,
    run_live as run_claim_component_live,
)
from pipeline.scripts.run_incremental_identity_auditor_v1_live import (
    build_envelope as build_identity_live_envelope,
    run_live as run_identity_component_live,
)


class ChapterCycleLiveExecutorError(RuntimeError):
    pass


_SHARED_ROLE_BY_STAGE_ROLE = {
    "b0": "literary.b1.entity_inventory",
    "local_auditor": "literary.audit.local_conflict",
    "stable_claim_auditor": "literary.audit.stable_claim",
    "identity_auditor": "literary.audit.identity_surface",
}


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ChapterCycleLiveExecutorError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ChapterCycleLiveExecutorError(f"{label} must be an object")
    return value


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.is_file():
        if canonical_json(_load_json(target, "immutable artifact")) != canonical_json(
            payload
        ):
            raise ChapterCycleLiveExecutorError(
                f"immutable artifact differs: {target}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(target, dict(payload))


def _artifact_ref(path: Path, run_root: Path) -> dict[str, str]:
    target = Path(path).resolve()
    return {
        "path": target.relative_to(run_root.resolve()).as_posix(),
        "sha256": file_sha256(target),
    }


def _chapter(document: Mapping[str, Any], chapter_id: str) -> Mapping[str, Any]:
    rows = [
        row
        for row in document.get("chapters") or []
        if isinstance(row, Mapping) and row.get("chapter_id") == chapter_id
    ]
    if len(rows) != 1:
        raise ChapterCycleLiveExecutorError(
            f"document does not exact-cover chapter {chapter_id}"
        )
    return rows[0]


def _count_value(value: Any, *, key: str, accepted: set[str]) -> int:
    if isinstance(value, Mapping):
        own = int(str(value.get(key) or "") in accepted)
        return own + sum(
            _count_value(row, key=key, accepted=accepted) for row in value.values()
        )
    if isinstance(value, list):
        return sum(_count_value(row, key=key, accepted=accepted) for row in value)
    return 0


def _archive_retryable_output(output_dir: Path) -> None:
    output = Path(output_dir)
    if not output.exists() or not any(output.iterdir()):
        return
    if any((output / name).is_file() for name in ("experiment_failure.json", "failure.json")):
        archive = output.parent / "failed_attempts"
        archive.mkdir(parents=True, exist_ok=True)
        index = 1
        while (archive / f"attempt_{index:03d}").exists():
            index += 1
        output.rename(archive / f"attempt_{index:03d}")
        return
    raise ChapterCycleLiveExecutorError(
        f"live output exists without an accepted artifact or failure marker: {output}"
    )


def _response_request_fingerprint(path: Path) -> str:
    value = _load_json(path, "request").get("request_fingerprint")
    if not isinstance(value, str) or len(value) != 64:
        raise ChapterCycleLiveExecutorError("request fingerprint is malformed")
    return value


def _aggregate_execution_hash(paths: Sequence[Path]) -> str:
    return canonical_hash(
        [
            {"path": Path(path).name, "sha256": file_sha256(path)}
            for path in sorted({Path(row).resolve() for row in paths})
        ]
    )


def _aggregate_request_fingerprint(paths: Sequence[Path]) -> str:
    fingerprints = [
        _response_request_fingerprint(path) for path in sorted(set(paths))
    ]
    if len(fingerprints) == 1:
        return fingerprints[0]
    return canonical_hash(fingerprints)


def _public_payload(
    *,
    stage: ChapterCycleStage,
    artifacts: Mapping[str, Path],
    run_root: Path,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "chapter_id": stage.chapter_id,
        "chapter_ordinal": stage.chapter_ordinal,
        "public_stage_name": (
            "b1" if stage.stage_name in {"b0", "b0_prior"} else stage.stage_name
        ),
        "implementation_stage_name": stage.stage_name,
        "artifacts": {
            key: _artifact_ref(path, run_root) for key, path in artifacts.items()
        },
        "production_publish_performed": False,
    }
    payload.update(dict(extra or {}))
    return payload


def _candidate_review_queue(
    *,
    index: Mapping[str, Any],
    prefix: Mapping[str, Any],
    challenge: Mapping[str, Any],
) -> dict[str, Any]:
    challenge_body = deepcopy(dict(challenge))
    observed_hash = challenge_body.pop("prior_challenge_artifact_hash", None)
    if canonical_hash(challenge_body) != observed_hash:
        raise ChapterCycleLiveExecutorError("prior challenge artifact hash mismatch")
    if challenge.get("schema_version") != PRIOR_CHALLENGE_SCHEMA_VERSION:
        raise ChapterCycleLiveExecutorError("foreign prior challenge schema")
    raw_observations = challenge.get("candidate_only_observations") or []
    if not isinstance(raw_observations, list) or not all(
        isinstance(row, Mapping) for row in raw_observations
    ):
        raise ChapterCycleLiveExecutorError("candidate observations are malformed")
    allowed = {"new_claim_evidence", "possible_collision", "supports_continuity"}
    if any(row.get("observation") not in allowed for row in raw_observations):
        raise ChapterCycleLiveExecutorError("candidate observation route is foreign")
    candidate_by_id = {
        row["prior_card_id"]: row for row in prefix["candidate_only_context_cards"]
    }
    queued: list[dict[str, Any]] = []
    for source in raw_observations:
        row = deepcopy(dict(source))
        card_id = row.get("prior_card_id")
        candidate = candidate_by_id.get(card_id)
        if candidate is None:
            raise ChapterCycleLiveExecutorError(
                "candidate observation targets no prefix candidate"
            )
        pending_snapshot = None
        if row.get("observation") == "new_claim_evidence":
            pending_snapshot = next(
                (
                    deepcopy(dict(dispute))
                    for dispute in candidate["disputed_claims"]
                    if isinstance(dispute, Mapping)
                    and dispute.get("disputed_field") == row.get("disputed_field")
                ),
                None,
            )
            if pending_snapshot is None:
                raise ChapterCycleLiveExecutorError(
                    "candidate evidence targets no pending field"
                )
        evidence = {
            "state_lineage_id": index.get("state_lineage_id"),
            "prior_card_id": card_id,
            "observation": row.get("observation"),
            "disputed_field": row.get("disputed_field"),
            "chapter_id": challenge.get("chapter_id"),
            "source_block_ids": row.get("source_block_ids"),
        }
        queued.append(
            {
                **row,
                "evidence_manifest_hash": canonical_hash(evidence),
                "pending_claim_snapshot": pending_snapshot,
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
            for row in queued
            if row["observation"] in {"possible_collision", "supports_continuity"}
        ],
        "candidate_claim_evidence_queue": [
            row for row in queued if row["observation"] == "new_claim_evidence"
        ],
        "production_publish_performed": False,
    }
    return {**body, "queue_hash": canonical_hash(body)}


def _supplied_claim_cards(
    *, prefix: Mapping[str, Any], challenge: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = challenge.get("code_derived_prior_presence")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ChapterCycleLiveExecutorError("prior-presence rows are malformed")
    ids = [str(row.get("prior_card_id") or "") for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ChapterCycleLiveExecutorError("prior-presence ids are malformed")
    by_id = {row["prior_card_id"]: row for row in prefix["claim_cards"]}
    if not set(ids).issubset(by_id):
        raise ChapterCycleLiveExecutorError("challenge cites a foreign prior card")
    return [deepcopy(by_id[card_id]) for card_id in ids]


class ChapterCycleLiveExecutorV1:
    """Execute the sealed chapter-cycle plan without changing semantic policy."""

    def __init__(
        self,
        *,
        run_root: Path,
        plan: Mapping[str, Any],
        credential_root: Path | None,
        usage_roots: Sequence[Path] | None = None,
        backend_mode: str = BACKEND_MODE_LEGACY,
        shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
    ) -> None:
        self.run_root = Path(run_root).resolve()
        self.plan = dict(plan)
        self.document = _load_json(Path(self.plan["document_path"]), "document")
        self.design_doc = Path(self.plan["design_doc_path"])
        self.frozen_db = Path(self.plan["frozen_db_path"])
        self.cycle_profile: LiteraryChapterCycleProfile = load_chapter_cycle_profile(
            Path(self.plan["chapter_cycle_profile_path"])
        )
        self.provider_profile: ProviderProfile = load_provider_profile(
            Path(self.plan["provider_profile_path"])
        )
        policy_path = self.plan.get("structured_output_policy_path")
        self.structured_output_policy: LiteraryStructuredOutputPolicy | None = (
            load_literary_structured_output_policy(Path(policy_path))
            if policy_path is not None
            else None
        )
        self.credential_root = (
            Path(credential_root).resolve() if credential_root is not None else None
        )
        self.usage_roots = tuple(
            Path(row).resolve() for row in (usage_roots or (self.run_root,))
        )
        if backend_mode not in {BACKEND_MODE_LEGACY, BACKEND_MODE_SHARED_V1}:
            raise ChapterCycleLiveExecutorError(
                "LLM backend mode is outside the closed enum"
            )
        if backend_mode == BACKEND_MODE_SHARED_V1:
            if shared_runtime is None:
                raise ChapterCycleLiveExecutorError(
                    "shared_v1 mode requires an injected shared runtime"
                )
            if self.credential_root is not None:
                raise ChapterCycleLiveExecutorError(
                    "shared_v1 mode cannot receive a legacy credential root"
                )
        elif shared_runtime is not None:
            raise ChapterCycleLiveExecutorError(
                "legacy mode cannot receive a shared runtime"
            )
        self.backend_mode = backend_mode
        self.shared_runtime = shared_runtime

    def _chapter_root(self, ordinal: int) -> Path:
        return self.run_root / "artifacts" / "chapters" / f"ch{ordinal:03d}"

    def _paths(self, stage: ChapterCycleStage) -> dict[str, Path]:
        root = self._chapter_root(stage.chapter_ordinal)
        previous = self._chapter_root(stage.chapter_ordinal - 1)
        stage_root = self.run_root / "stages" / stage.stage_id
        return {
            "chapter_root": root,
            "stage_root": stage_root,
            "dry": stage_root / "dry",
            "live": stage_root / "live",
            "b1_inventory": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_{'b0' if stage.chapter_ordinal == 1 else 'b0_prior'}"
                / "live"
                / "inventory.json"
            ),
            "prior_artifact": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_b0_prior"
                / "live"
                / "prior_challenge_artifact.json"
            ),
            "delta_inventory": root / "delta_inventory.json",
            "audited_inventory": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_local_auditor"
                / "live"
                / "conflict_audited_inventory.json"
            ),
            "previous_final_prefix": previous / "final_prefix.json",
            "previous_review_ledger": previous / "final_review_ledger.json",
            "previous_identity_ledger": previous / "identity_ledger.json",
            "previous_semantic_leads": previous / "semantic_lead_index.json",
            "previous_review_case_ledger": previous / "final_review_case_ledger.json",
            "claim_prepared": root / "stable_claim" / "prepared",
            "claim_reconciled": root / "stable_claim" / "reconciled",
            "claim_component_live": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_stable_claim_components"
                / "live"
            ),
            "claim_component_dry": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_stable_claim_components"
                / "dry"
            ),
            "candidate_review_queue": root / "candidate_review_queue.json",
            "candidate_review_ledger": root / "candidate_review_ledger.json",
            "prefix_effective_prior": root / "prefix_effective_prior.json",
            "prefix_extended": root / "prefix_extended.json",
            "semantic_lead_index": root / "semantic_lead_index.json",
            "semantic_identity_occurrence_bridge": (
                root / "semantic_identity_occurrence_bridge.json"
            ),
            "prefix_semantic": root / "prefix_semantic.json",
            "review_ledger_semantic": root / "review_ledger_semantic.json",
            "review_case_ledger_pre_identity": (
                root / "review_case_ledger_pre_identity.json"
            ),
            "identity_prepared": root / "identity" / "prepared",
            "identity_component_live": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_identity_components"
                / "live"
            ),
            "identity_component_dry": (
                self.run_root
                / "stages"
                / f"ch{stage.chapter_ordinal:03d}_identity_components"
                / "dry"
            ),
            "identity_ledger": root / "identity_ledger.json",
            "final_prefix": root / "final_prefix.json",
            "final_review_ledger": root / "final_review_ledger.json",
            "final_review_case_ledger": root / "final_review_case_ledger.json",
            "chapter_report": root / "chapter_report.json",
        }

    def _role(self, stage_role: str) -> tuple[str, ProviderRole]:
        role_id = self.cycle_profile.role_bindings.get(stage_role)
        if role_id is None or role_id not in self.provider_profile.roles:
            raise ChapterCycleLiveExecutorError(
                f"sealed profile lacks role binding for {stage_role}"
            )
        role = self.provider_profile.roles[role_id]
        return role_id, role

    def _shared_role_id(self, stage_role: str) -> str:
        try:
            return _SHARED_ROLE_BY_STAGE_ROLE[stage_role]
        except KeyError as exc:
            raise ChapterCycleLiveExecutorError(
                f"stage role has no shared-v1 binding: {stage_role}"
            ) from exc

    def _model_id(self, stage_role: str) -> str:
        if self.backend_mode == BACKEND_MODE_SHARED_V1:
            return self._required_shared_runtime().role_preset_for(
                self._shared_role_id(stage_role)
            ).requested_model_id
        return self._role(stage_role)[1].model_id

    def _provider_id(self) -> str:
        if self.backend_mode != BACKEND_MODE_SHARED_V1:
            return self._role("b0")[1].provider
        runtime = self._required_shared_runtime()
        protocol = str(
            runtime.api_source_for(self._shared_role_id("b0")).get("protocol")
            or ""
        )
        if protocol == "openai_chat_completions":
            return "openai"
        if protocol == "google_genai_generate_content":
            return "google_genai"
        raise ChapterCycleLiveExecutorError(
            "shared source protocol is unsupported by the Literary renderer"
        )

    def _required_shared_runtime(self) -> LiterarySharedRunnerBindingsV1:
        if self.backend_mode != BACKEND_MODE_SHARED_V1 or self.shared_runtime is None:
            raise ChapterCycleLiveExecutorError(
                "shared execution was requested outside shared_v1 mode"
            )
        return self.shared_runtime

    def _write_shared_report(
        self,
        *,
        output_dir: Path,
        stage: ChapterCycleStage,
        role_id: str,
        request_fingerprint: str,
        semantic_output: Mapping[str, Any],
    ) -> Path:
        receipt_path = Path(output_dir) / "shared_attempt_receipt.json"
        receipt = _load_json(receipt_path, "shared attempt receipt")
        body = {
            "schema_version": "literary_shared_stage_execution_report_v1",
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "stage_id": stage.stage_id,
            "chapter_id": stage.chapter_id,
            "role_id": role_id,
            "request_fingerprint": request_fingerprint,
            "semantic_output_sha256": canonical_hash(semantic_output),
            "shared_attempt_receipt_sha256": file_sha256(receipt_path),
            "shared_seal_sha256": receipt["seal_sha256"],
            "model_actual": receipt["seal"]["primary"]["target"][
                "requested_model_id"
            ],
            "usage": receipt.get("usage"),
            "application_response_cache": "disabled",
            "production_publish_performed": False,
        }
        report = {**body, "report_hash": canonical_hash(body)}
        report_path = Path(output_dir) / "shared_execution_report.json"
        _write_immutable(report_path, report)
        return report_path

    def _structured_contract(
        self,
        *,
        stage_role: str,
        canonical_schema: Mapping[str, Any],
        quota_bucket_id: str | None = None,
    ) -> StructuredOutputContract | None:
        if self.backend_mode == BACKEND_MODE_SHARED_V1:
            return None
        if self.structured_output_policy is None:
            return None
        role_id, role = self._role(stage_role)
        bucket_id = quota_bucket_id or role.bucket_order[0]
        if bucket_id not in role.bucket_order:
            raise ChapterCycleLiveExecutorError(
                "structured-output bucket is outside the sealed role"
            )
        credential_ref = self.provider_profile.credentials.get(bucket_id)
        if credential_ref is None or credential_ref.provider != role.provider:
            raise ChapterCycleLiveExecutorError(
                "structured-output capability has no matching credential route"
            )
        return resolve_structured_output_contract(
            self.structured_output_policy,
            role_id=role_id,
            provider=role.provider,
            base_url=credential_ref.base_url,
            model_id=role.model_id,
            canonical_schema=canonical_schema,
        )

    def _credential_root(self) -> Path:
        if self.credential_root is None:
            raise ChapterCycleLiveExecutorError(
                "live execution requires a credential root"
            )
        return self.credential_root

    def _openai_key_paths(self, stage_role: str) -> tuple[ProviderRole, dict[str, Path]]:
        role_id, role = self._role(stage_role)
        rows = resolve_role_credentials(
            self.provider_profile,
            role_id=role_id,
            credential_root=self._credential_root(),
        )
        return role, {row.quota_bucket_id: row.source_path for row in rows}

    def _b1_credential(self) -> tuple[ProviderRole, Any]:
        role_id, role = self._role("b0")
        credential = resolve_role_credential(
            self.provider_profile,
            role_id=role_id,
            credential_root=self._credential_root(),
        )
        return role, credential

    def _ensure_delta(self, stage: ChapterCycleStage) -> Path:
        paths = self._paths(stage)
        if paths["delta_inventory"].is_file():
            return paths["delta_inventory"]
        artifact = _load_json(paths["prior_artifact"], "prior challenge artifact")
        delta = artifact.get("delta_inventory")
        if not isinstance(delta, Mapping):
            raise ChapterCycleLiveExecutorError(
                "prior challenge artifact lacks delta inventory"
            )
        _write_immutable(paths["delta_inventory"], delta)
        return paths["delta_inventory"]

    def _inventory_path(self, stage: ChapterCycleStage) -> Path:
        if stage.chapter_ordinal == 1:
            return self._paths(stage)["b1_inventory"]
        return self._ensure_delta(stage)

    def _ensure_dry_b1(self, stage: ChapterCycleStage) -> dict[str, Any]:
        paths = self._paths(stage)
        report_path = paths["dry"] / "dry_report.json"
        if report_path.is_file():
            return _load_json(report_path, "B1 dry report")
        _chapter(self.document, stage.chapter_id)
        if stage.stage_name == "b0":
            contract = self._structured_contract(
                stage_role="b0",
                canonical_schema=entity_inventory_response_schema(),
            )
            return run_b1_dry(
                stage="b0",
                document_path=Path(self.plan["document_path"]),
                design_doc=self.design_doc,
                chapter_id=stage.chapter_id,
                inventory_path=None,
                output_dir=paths["dry"],
                model_id=self._model_id("b0"),
                provider_id=self._provider_id(),
                structured_output_contract=contract,
            )
        contract = self._structured_contract(
            stage_role="b0",
            canonical_schema=prior_challenge_response_schema(),
        )
        return run_b1_prior_dry(
            document_path=Path(self.plan["document_path"]),
            design_doc=self.design_doc,
            chapter_id=stage.chapter_id,
            prior_cards_path=None,
            prior_bundle_path=paths["previous_final_prefix"],
            corruption_manifest_path=None,
            output_dir=paths["dry"],
            review_case_ledger_path=(
                paths["previous_review_case_ledger"]
                if paths["previous_review_case_ledger"].is_file()
                else None
            ),
            model_id=self._model_id("b0"),
            structured_output_contract=contract,
        )

    def dry_render_stage(self, stage: ChapterCycleStage) -> dict[str, Any]:
        """Render the current data-dependent request without a transport call."""

        if stage.stage_name in {"b0", "b0_prior"}:
            return self._ensure_dry_b1(stage)
        if stage.stage_name == "local_auditor":
            paths = self._paths(stage)
            inventory = self._inventory_path(stage)
            chapter = _chapter(self.document, stage.chapter_id)
            manifest = build_identity_conflict_manifest(
                _load_json(inventory, "inventory"), chapter
            )
            if not manifest["components"] and not manifest["glossary_review"][
                "candidate_cards"
            ]:
                return {
                    "status": "not_required_clean_code_path",
                    "component_count": 0,
                    "api_calls": 0,
                }
            report_path = paths["dry"] / "dry_report.json"
            if report_path.is_file():
                return _load_json(report_path, "local Auditor dry report")
            role = self._role("local_auditor")[1]
            contract = self._structured_contract(
                stage_role="local_auditor",
                canonical_schema=entity_conflict_response_schema(),
            )
            return run_local_auditor_dry(
                document_path=Path(self.plan["document_path"]),
                inventory_path=inventory,
                design_doc=self.design_doc,
                output_dir=paths["dry"],
                chapter_id=stage.chapter_id,
                bucket_order=role.bucket_order,
                model_id=self._model_id("local_auditor"),
                structured_output_contract=contract,
            )
        return {
            "status": "data_dependent_not_yet_renderable",
            "stage_id": stage.stage_id,
            "api_calls": 0,
        }

    def _model_result(
        self,
        *,
        stage: ChapterCycleStage,
        permit: ApiCallPermit,
        artifacts: Mapping[str, Path],
        request_paths: Sequence[Path],
        report_paths: Sequence[Path],
        model_id: str,
        pending_count: int = 0,
        extra: Mapping[str, Any] | None = None,
    ) -> StageExecutionResult:
        attempt_count = permit.attempt_count()
        if not request_paths:
            return StageExecutionResult(
                status="accepted",
                payload=_public_payload(
                    stage=stage,
                    artifacts=artifacts,
                    run_root=self.run_root,
                    extra=extra,
                ),
                call_disposition="not_required",
                attempt_count=attempt_count,
            )
        return StageExecutionResult(
            status="semantic_pending" if pending_count else "accepted",
            payload=_public_payload(
                stage=stage,
                artifacts=artifacts,
                run_root=self.run_root,
                extra=extra,
            ),
            call_disposition="called",
            request_fingerprint=_aggregate_request_fingerprint(request_paths),
            model_actual=model_id,
            resilience_report_hash=_aggregate_execution_hash(report_paths),
            attempt_count=attempt_count,
            semantic_pending_count=pending_count,
        )

    def _execute_b1(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        paths = self._paths(stage)
        dry = self._ensure_dry_b1(stage)
        request_path = paths["dry"] / "request.json"
        _role_id, role = self._role("b0")
        model_id = self._model_id("b0")
        if stage.stage_name == "b0":
            artifact = paths["live"] / "inventory.json"
        else:
            artifact = paths["live"] / "prior_challenge_artifact.json"
        if not artifact.is_file():
            if self.backend_mode == BACKEND_MODE_SHARED_V1:
                request = _load_json(request_path, "shared B1 request")
                response_schema = (
                    entity_inventory_response_schema()
                    if stage.stage_name == "b0"
                    else prior_challenge_response_schema()
                )
                request = {**request, "response_schema": response_schema}
                chapter = _chapter(self.document, stage.chapter_id)
                if stage.stage_name == "b0":
                    def validate_b1(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                        validate_structured_payload(raw, canonical_schema=response_schema)
                        return validate_entity_inventory_response(
                            raw,
                            chapter,
                            request_fingerprint=request["request_fingerprint"],
                        )

                    validator_ref = build_literary_code_ref_v1(
                        identifier="literary.b1.entity_inventory.validator",
                        revision="v1",
                        callables=(
                            validate_structured_payload,
                            validate_entity_inventory_response,
                        ),
                    )
                    application_contract_id = "literary.b1.inventory.apply_v1"
                    schema_name = "literary_b1_entity_inventory_v1"
                else:
                    (
                        _envelope,
                        rebuilt_request,
                        _chapter_row,
                        prior_cards,
                        candidate_only_cards,
                        glossary_cards,
                        relevant_review_cases,
                        _manifest,
                        _response_schema,
                    ) = build_prior_challenge_envelope(
                        document_path=Path(self.plan["document_path"]),
                        design_doc=self.design_doc,
                        chapter_id=stage.chapter_id,
                        prior_cards_path=None,
                        prior_bundle_path=paths["previous_final_prefix"],
                        corruption_manifest_path=None,
                        review_case_ledger_path=(
                            paths["previous_review_case_ledger"]
                            if paths["previous_review_case_ledger"].is_file()
                            else None
                        ),
                        model_id=model_id,
                        structured_output_contract=None,
                    )
                    if rebuilt_request.request_fingerprint != request["request_fingerprint"]:
                        raise ChapterCycleLiveExecutorError(
                            "shared B1 prior context no longer matches the rendered request"
                        )

                    def validate_b1(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                        validate_structured_payload(raw, canonical_schema=response_schema)
                        return validate_prior_challenge_response(
                            raw,
                            chapter=chapter,
                            prior_cards=prior_cards,
                            candidate_only_cards=candidate_only_cards,
                            glossary_cards=glossary_cards,
                            relevant_review_cases=relevant_review_cases,
                            request_fingerprint=request["request_fingerprint"],
                        )

                    validator_ref = build_literary_code_ref_v1(
                        identifier="literary.b1.prior_challenge.validator",
                        revision="v1",
                        callables=(
                            validate_structured_payload,
                            validate_prior_challenge_response,
                        ),
                    )
                    application_contract_id = "literary.b1.prior_challenge.apply_v1"
                    schema_name = "literary_b1_prior_challenge_v1"
                permit.reserve("b1_primary")
                result = self._required_shared_runtime().execute_accepted_request(
                    role_id=self._shared_role_id("b0"),
                    stage_id=stage.stage_id,
                    logical_request_id=(
                        f"{stage.stage_id}_{request['request_fingerprint'][:24]}"
                    ),
                    request=request,
                    schema_name=schema_name,
                    semantic_validator=validate_b1,
                    validator_ref=validator_ref,
                    application_contract_id=application_contract_id,
                    application_contract_revision="v1",
                    output_dir=paths["live"],
                    model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                    additional_input_bindings=(
                        {
                            "name": "literary_document",
                            "sha256": file_sha256(Path(self.plan["document_path"])),
                        },
                        {
                            "name": "literary_design_doc",
                            "sha256": file_sha256(self.design_doc),
                        },
                    ),
                )
                _write_immutable(artifact, result.semantic_payload)
                self._write_shared_report(
                    output_dir=paths["live"],
                    stage=stage,
                    role_id=self._shared_role_id("b0"),
                    request_fingerprint=request["request_fingerprint"],
                    semantic_output=result.semantic_payload,
                )
            else:
                _resolved_role, credential = self._b1_credential()
                contract = self._structured_contract(
                    stage_role="b0",
                    canonical_schema=(
                        entity_inventory_response_schema()
                        if stage.stage_name == "b0"
                        else prior_challenge_response_schema()
                    ),
                    quota_bucket_id=credential.quota_bucket_id,
                )
                _archive_retryable_output(paths["live"])
                permit.reserve("b1_primary")
                if stage.stage_name == "b0":
                    run_b1_live(
                        stage="b0",
                        document_path=Path(self.plan["document_path"]),
                        design_doc=self.design_doc,
                        frozen_db=self.frozen_db,
                        chapter_id=stage.chapter_id,
                        inventory_path=None,
                        output_dir=paths["live"],
                        approved_envelope_hash=str(dry["envelope_hash"]),
                        keys_file=None,
                        quota_bucket_id=credential.quota_bucket_id,
                        usage_roots=self.usage_roots,
                        gold_path=None,
                        resolved_credential=credential,
                        allowed_quota_bucket_ids=role.bucket_order,
                        provider_profile_hash=self.provider_profile.profile_hash,
                        model_id=role.model_id,
                        structured_output_contract=contract,
                    )
                else:
                    run_b1_prior_live(
                        document_path=Path(self.plan["document_path"]),
                        design_doc=self.design_doc,
                        frozen_db=self.frozen_db,
                        chapter_id=stage.chapter_id,
                        prior_cards_path=None,
                        prior_bundle_path=paths["previous_final_prefix"],
                        corruption_manifest_path=None,
                        output_dir=paths["live"],
                        review_case_ledger_path=(
                            paths["previous_review_case_ledger"]
                            if paths["previous_review_case_ledger"].is_file()
                            else None
                        ),
                        approved_envelope_hash=str(dry["envelope_hash"]),
                        keys_file=None,
                        quota_bucket_id=credential.quota_bucket_id,
                        usage_roots=self.usage_roots,
                        resolved_credential=credential,
                        allowed_quota_bucket_ids=role.bucket_order,
                        provider_profile_hash=self.provider_profile.profile_hash,
                        model_id=role.model_id,
                        structured_output_contract=contract,
                    )
        report = paths["live"] / (
            "shared_execution_report.json"
            if self.backend_mode == BACKEND_MODE_SHARED_V1
            else "experiment_report.json"
        )
        return self._model_result(
            stage=stage,
            permit=permit,
            artifacts={"b1_output": artifact},
            request_paths=[request_path],
            report_paths=[report],
            model_id=model_id,
            extra={
                "sealed_envelope_hash": dry["envelope_hash"],
                "backend_mode": self.backend_mode,
            },
        )

    def _execute_local_auditor(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        paths = self._paths(stage)
        inventory_path = self._inventory_path(stage)
        inventory = _load_json(inventory_path, "local Auditor inventory")
        chapter = _chapter(self.document, stage.chapter_id)
        manifest = build_identity_conflict_manifest(inventory, chapter)
        output = paths["audited_inventory"]
        if not manifest["components"] and not manifest["glossary_review"][
            "candidate_cards"
        ]:
            if not output.is_file():
                request = render_entity_conflict_request(
                    chapter=chapter,
                    inventory=inventory,
                    design_doc=self.design_doc,
                    model_id=self._role("local_auditor")[1].model_id,
                    reasoning_effort="none",
                    temperature=1.0,
                    seed=20260715,
                    max_output_tokens=4096,
                )
                audited = validate_and_apply_conflict_response(
                    {
                        "chapter_id": stage.chapter_id,
                        "component_decisions": [],
                        "glossary_dispositions": [],
                    },
                    chapter=chapter,
                    inventory=inventory,
                    request_fingerprint=request.request_fingerprint,
                )
                _write_immutable(output, audited)
            return StageExecutionResult(
                status="accepted",
                payload=_public_payload(
                    stage=stage,
                    artifacts={"audited_inventory": output},
                    run_root=self.run_root,
                    extra={"clean_code_path": True},
                ),
                call_disposition="not_required",
                attempt_count=permit.attempt_count(),
            )
        role = self._role("local_auditor")[1]
        model_id = self._model_id("local_auditor")
        contract = self._structured_contract(
            stage_role="local_auditor",
            canonical_schema=entity_conflict_response_schema(),
        )
        dry = self.dry_render_stage(stage)
        request_path = paths["dry"] / "request.json"
        if not output.is_file():
            if self.backend_mode == BACKEND_MODE_SHARED_V1:
                request = _load_json(request_path, "shared Local Auditor request")
                response_schema = entity_conflict_response_schema()
                request = {**request, "response_schema": response_schema}
                normalizations: list[dict[str, Any]] = []

                def validate_local(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                    normalized, rows = normalize_source_boundary_violations(
                        raw,
                        chapter=chapter,
                        inventory=inventory,
                    )
                    validate_structured_payload(
                        normalized, canonical_schema=response_schema
                    )
                    normalizations.extend(rows)
                    return validate_and_apply_conflict_response(
                        normalized,
                        chapter=chapter,
                        inventory=inventory,
                        request_fingerprint=request["request_fingerprint"],
                        source_boundary_normalizations=rows,
                    )

                validator_ref = build_literary_code_ref_v1(
                    identifier="literary.audit.local_conflict.validator",
                    revision="v1",
                    callables=(
                        normalize_source_boundary_violations,
                        validate_structured_payload,
                        validate_and_apply_conflict_response,
                    ),
                )
                permit.reserve("local_auditor_primary")
                result = self._required_shared_runtime().execute_accepted_request(
                    role_id=self._shared_role_id("local_auditor"),
                    stage_id=stage.stage_id,
                    logical_request_id=(
                        f"{stage.stage_id}_{request['request_fingerprint'][:24]}"
                    ),
                    request=request,
                    schema_name="literary_local_conflict_auditor_v1",
                    semantic_validator=validate_local,
                    validator_ref=validator_ref,
                    application_contract_id="literary.audit.local_conflict.apply_v1",
                    application_contract_revision="v1",
                    output_dir=paths["live"],
                    model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                    additional_input_bindings=(
                        {
                            "name": "literary_document",
                            "sha256": file_sha256(Path(self.plan["document_path"])),
                        },
                        {
                            "name": "literary_source_inventory",
                            "sha256": file_sha256(inventory_path),
                        },
                    ),
                )
                _write_immutable(
                    paths["live"] / "source_boundary_normalizations.json",
                    {
                        "schema_version": (
                            "local_auditor_source_boundary_normalizations_v1"
                        ),
                        "normalizations": normalizations,
                        "normalization_count": len(normalizations),
                    },
                )
                _write_immutable(output, result.semantic_payload)
                self._write_shared_report(
                    output_dir=paths["live"],
                    stage=stage,
                    role_id=self._shared_role_id("local_auditor"),
                    request_fingerprint=request["request_fingerprint"],
                    semantic_output=result.semantic_payload,
                )
            else:
                role_id, _resolved_role = self._role("local_auditor")
                credential = resolve_role_credential(
                    self.provider_profile,
                    role_id=role_id,
                    credential_root=self._credential_root(),
                )
                key_paths = {credential.quota_bucket_id: credential.source_path}
                _archive_retryable_output(paths["live"])
                permit.reserve("local_auditor_primary")
                run_local_auditor_live(
                    document_path=Path(self.plan["document_path"]),
                    inventory_path=inventory_path,
                    design_doc=self.design_doc,
                    frozen_db=self.frozen_db,
                    output_dir=paths["live"],
                    approved_envelope_hash=str(dry["envelope_hash"]),
                    key_paths=key_paths,
                    usage_roots=self.usage_roots,
                    gold_path=None,
                    chapter_id=stage.chapter_id,
                    bucket_order=role.bucket_order,
                    structured_output_contract=contract,
                    resolved_credential=credential,
                    provider_profile_hash=self.provider_profile.profile_hash,
                    model_id=role.model_id,
                )
        report_path = paths["live"] / (
            "shared_execution_report.json"
            if self.backend_mode == BACKEND_MODE_SHARED_V1
            else "experiment_report.json"
        )
        return self._model_result(
            stage=stage,
            permit=permit,
            artifacts={"audited_inventory": output},
            request_paths=[request_path],
            report_paths=[report_path],
            model_id=model_id,
            extra={"backend_mode": self.backend_mode},
        )

    def _execute_prefix(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        bundle = build_chapter_prefix_prior_bundle_v1(
            document=self.document,
            audited_inventory=_load_json(
                paths["audited_inventory"], "first chapter audited inventory"
            ),
            coverage_through_chapter_id=stage.chapter_id,
        )
        _write_immutable(paths["prefix_extended"], bundle)
        return StageExecutionResult(
            status="accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={"prefix": paths["prefix_extended"]},
                run_root=self.run_root,
            ),
            call_disposition="code_only",
            cumulative_hash_updates={"prefix_hash": bundle["prefix_bundle_hash"]},
        )

    def _execute_claim_prepare(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        prefix = verify_chapter_prefix_prior_bundle_v1(
            _load_json(paths["previous_final_prefix"], "previous final prefix"),
            document=self.document,
        )
        challenge = _load_json(paths["prior_artifact"], "prior challenge artifact")
        index = build_prior_claim_ticket_index_v1(
            document=self.document,
            prior_cards=_supplied_claim_cards(prefix=prefix, challenge=challenge),
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
                document=self.document,
                design_doc=self.design_doc,
            ),
        )
        component_ids: list[str] = []
        for component in index["claim_components"]:
            if component["overflow"]:
                continue
            rendered = render_prior_claim_request_v1(
                index=index,
                component_id=component["component_id"],
                document=self.document,
                design_doc=self.design_doc,
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
        cap = self.cycle_profile.stage_limits["stable_claim_auditor"].max_calls_per_chapter
        if len(component_ids) > cap:
            raise ChapterCycleLiveExecutorError(
                "stable-claim component count exceeds sealed cap"
            )
        queue = _candidate_review_queue(index=index, prefix=prefix, challenge=challenge)
        _write_immutable(paths["candidate_review_queue"], queue)
        return StageExecutionResult(
            status="semantic_pending" if any(row["overflow"] for row in index["claim_components"]) else "accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={
                    "ticket_index": prepared / "ticket_index.json",
                    "candidate_review_queue": paths["candidate_review_queue"],
                },
                run_root=self.run_root,
                extra={"eligible_component_ids": component_ids},
            ),
            call_disposition="code_only",
            semantic_pending_count=sum(
                bool(row["overflow"]) for row in index["claim_components"]
            ),
        )

    def _execute_claim_components(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        paths = self._paths(stage)
        index = verify_prior_claim_ticket_index_v1(
            _load_json(paths["claim_prepared"] / "ticket_index.json", "ticket index")
        )
        component_ids = [
            row["component_id"] for row in index["claim_components"] if not row["overflow"]
        ]
        if not component_ids:
            return self._model_result(
                stage=stage,
                permit=permit,
                artifacts={"ticket_index": paths["claim_prepared"] / "ticket_index.json"},
                request_paths=[],
                report_paths=[],
                model_id=self._model_id("stable_claim_auditor"),
            )
        role = self._role("stable_claim_auditor")[1]
        model_id = self._model_id("stable_claim_auditor")
        key_paths: dict[str, Path] | None = None
        request_paths: list[Path] = []
        report_paths: list[Path] = []
        decisions: dict[str, Path] = {}
        for component_id in component_ids:
            request_path = (
                paths["claim_prepared"]
                / "components"
                / component_id
                / "request.json"
            )
            request_paths.append(request_path)
            output = paths["claim_component_live"] / component_id
            decision = output / "decision.json"
            if not decision.is_file():
                if self.backend_mode == BACKEND_MODE_SHARED_V1:
                    request = _load_json(request_path, "shared stable-claim request")
                    response_schema = request["response_schema"]

                    def validate_claim(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                        validate_structured_payload(
                            raw, canonical_schema=response_schema
                        )
                        return validate_prior_claim_response_v1(
                            raw,
                            index=index,
                            request_fingerprint=request["request_fingerprint"],
                        )

                    validator_ref = build_literary_code_ref_v1(
                        identifier="literary.audit.stable_claim.validator",
                        revision="v1",
                        callables=(
                            validate_structured_payload,
                            validate_prior_claim_response_v1,
                        ),
                    )
                    permit.reserve("claim_" + canonical_hash(component_id)[:12])
                    result = self._required_shared_runtime().execute_accepted_request(
                        role_id=self._shared_role_id("stable_claim_auditor"),
                        stage_id=stage.stage_id,
                        logical_request_id=(
                            f"{stage.stage_id}_"
                            f"{canonical_hash({'component_id': component_id, 'request_fingerprint': request['request_fingerprint']})[:24]}"
                        ),
                        request=request,
                        schema_name="literary_stable_claim_auditor_v1",
                        semantic_validator=validate_claim,
                        validator_ref=validator_ref,
                        application_contract_id=(
                            "literary.audit.stable_claim.decision_v1"
                        ),
                        application_contract_revision="v1",
                        output_dir=output,
                        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                        additional_input_bindings=(
                            {
                                "name": "literary_claim_ticket_index",
                                "sha256": file_sha256(
                                    paths["claim_prepared"] / "ticket_index.json"
                                ),
                            },
                        ),
                    )
                    _write_immutable(decision, result.semantic_payload)
                    self._write_shared_report(
                        output_dir=output,
                        stage=stage,
                        role_id=self._shared_role_id("stable_claim_auditor"),
                        request_fingerprint=request["request_fingerprint"],
                        semantic_output=result.semantic_payload,
                    )
                else:
                    if key_paths is None:
                        _resolved_role, key_paths = self._openai_key_paths(
                            "stable_claim_auditor"
                        )
                    dry_dir = paths["claim_component_dry"] / component_id
                    contract = self._structured_contract(
                        stage_role="stable_claim_auditor",
                        canonical_schema=prior_claim_response_schema_v1(),
                    )
                    if contract is not None:
                        _write_immutable(
                            dry_dir / "structured_output_contract.json",
                            contract.to_payload(),
                        )
                    envelope, preflight, _index, _request = build_claim_live_envelope(
                        prepared_dir=paths["claim_prepared"],
                        frozen_db=self.frozen_db,
                        usage_roots=self.usage_roots,
                        exclude_root=dry_dir,
                        component_id=component_id,
                        model_id=role.model_id,
                        bucket_order=role.bucket_order,
                        structured_output_contract=contract,
                    )
                    _write_immutable(dry_dir / "run_envelope.json", envelope)
                    _write_immutable(dry_dir / "quota_preflight.json", preflight)
                    _archive_retryable_output(output)
                    permit.reserve("claim_" + canonical_hash(component_id)[:12])
                    run_claim_component_live(
                        prepared_dir=paths["claim_prepared"],
                        output_dir=output,
                        frozen_db=self.frozen_db,
                        usage_roots=self.usage_roots,
                        approved_envelope_hash=envelope["envelope_hash"],
                        key_paths=key_paths,
                        component_id=component_id,
                        model_id=role.model_id,
                        bucket_order=role.bucket_order,
                        structured_output_contract=contract,
                    )
            decisions[component_id] = decision
            report_paths.append(
                output
                / (
                    "shared_execution_report.json"
                    if self.backend_mode == BACKEND_MODE_SHARED_V1
                    else "live_report.json"
                )
            )
        pending = sum(
            _count_value(
                _load_json(path, "claim decision"),
                key="action",
                accepted={"pending", "refer_identity_conflict"},
            )
            for path in decisions.values()
        )
        return self._model_result(
            stage=stage,
            permit=permit,
            artifacts={f"decision_{key}": value for key, value in decisions.items()},
            request_paths=request_paths,
            report_paths=report_paths,
            model_id=model_id,
            pending_count=pending,
            extra={"backend_mode": self.backend_mode},
        )

    def _execute_claim_reconcile(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        index = verify_prior_claim_ticket_index_v1(
            _load_json(paths["claim_prepared"] / "ticket_index.json", "ticket index")
        )
        decision_paths = sorted(
            paths["claim_component_live"].glob("*/decision.json")
        )
        decisions = [_load_json(path, "claim decision") for path in decision_paths]
        ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=decisions)
        projection = build_prior_claim_projection_v1(
            prior_cards=index["prior_cards"], ledger=ledger
        )
        ledger_path = paths["claim_reconciled"] / "claim_revision_ledger.json"
        projection_path = paths["claim_reconciled"] / "claim_projection.json"
        _write_immutable(ledger_path, ledger)
        _write_immutable(projection_path, projection)
        previous_review = (
            _load_json(paths["previous_review_ledger"], "previous review ledger")
            if paths["previous_review_ledger"].is_file()
            else None
        )
        review = build_chapter_cycle_review_ledger_v1(
            state_lineage_id=index["state_lineage_id"],
            chapter_id=stage.chapter_id,
            candidate_review_queue=_load_json(
                paths["candidate_review_queue"], "candidate review queue"
            ),
            claim_revision_ledger=ledger,
            previous_ledger=previous_review,
        )
        _write_immutable(paths["candidate_review_ledger"], review)
        effective = apply_claim_projection_to_prefix_bundle_v1(
            bundle=_load_json(paths["previous_final_prefix"], "previous prefix"),
            projection=projection,
        )
        verify_chapter_prefix_prior_bundle_v1(effective, document=self.document)
        _write_immutable(paths["prefix_effective_prior"], effective)
        pending = _count_value(ledger, key="status", accepted={"pending"})
        return StageExecutionResult(
            status="semantic_pending" if pending else "accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={
                    "claim_ledger": ledger_path,
                    "claim_projection": projection_path,
                    "review_ledger": paths["candidate_review_ledger"],
                    "effective_prefix": paths["prefix_effective_prior"],
                },
                run_root=self.run_root,
            ),
            call_disposition="code_only",
            semantic_pending_count=pending,
            cumulative_hash_updates={
                "claim_ledger_hash": ledger["claim_ledger_hash"],
                "review_ledger_hash": review["review_ledger_hash"],
                "prefix_hash": effective["prefix_bundle_hash"],
            },
        )

    def _execute_prefix_extend(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        effective = apply_glossary_dispositions_to_prefix_bundle_v1(
            bundle=_load_json(paths["prefix_effective_prior"], "effective prefix"),
            challenge_artifact=_load_json(paths["prior_artifact"], "prior challenge"),
        )
        extended = extend_chapter_prefix_prior_bundle_v1(
            bundle=effective,
            document=self.document,
            audited_inventory=_load_json(
                paths["audited_inventory"], "current audited inventory"
            ),
            next_chapter_id=stage.chapter_id,
        )
        _write_immutable(paths["prefix_extended"], extended)
        return StageExecutionResult(
            status="accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={"prefix": paths["prefix_extended"]},
                run_root=self.run_root,
            ),
            call_disposition="code_only",
            cumulative_hash_updates={"prefix_hash": extended["prefix_bundle_hash"]},
        )

    def _execute_semantic_leads(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        prefix = _load_json(paths["prefix_extended"], "extended prefix")
        if stage.chapter_ordinal == 1:
            report_body = {
                "schema_version": "literary_first_chapter_semantic_leads_v1",
                "chapter_id": stage.chapter_id,
                "status": "not_applicable_without_prior_chapter",
                "production_publish_performed": False,
            }
            report = {**report_body, "report_hash": canonical_hash(report_body)}
            cases = build_review_case_ledger_v1(
                document=self.document,
                chapter_id=stage.chapter_id,
                prefix_bundle=prefix,
                audited_inventory=_load_json(
                    paths["audited_inventory"], "first chapter audited inventory"
                ),
            )
            _write_immutable(paths["semantic_lead_index"], report)
            _write_immutable(paths["prefix_semantic"], prefix)
            _write_immutable(paths["final_prefix"], prefix)
            _write_immutable(paths["review_case_ledger_pre_identity"], cases)
            _write_immutable(paths["final_review_case_ledger"], cases)
            return StageExecutionResult(
                status="accepted",
                payload=_public_payload(
                    stage=stage,
                    artifacts={
                        "semantic_report": paths["semantic_lead_index"],
                        "final_prefix": paths["final_prefix"],
                        "review_case_ledger": paths["final_review_case_ledger"],
                    },
                    run_root=self.run_root,
                ),
                call_disposition="code_only",
                cumulative_hash_updates={
                    "prefix_hash": prefix["prefix_bundle_hash"],
                    "semantic_lead_index_hash": report["report_hash"],
                    "review_case_ledger_hash": cases[
                        "review_case_ledger_hash"
                    ],
                },
            )
        previous_leads = (
            _load_json(paths["previous_semantic_leads"], "previous semantic leads")
            if paths["previous_semantic_leads"].is_file()
            and _load_json(paths["previous_semantic_leads"], "previous semantic leads").get(
                "lead_index_hash"
            )
            else None
        )
        lead_index = build_semantic_candidate_lead_index_from_profile_v1(
            document=self.document,
            prefix_bundle=prefix,
            current_chapter_id=stage.chapter_id,
            chapter_cycle_profile=self.cycle_profile,
            previous_lead_index=previous_leads,
        )
        _write_immutable(paths["semantic_lead_index"], lead_index)
        semantic_prefix = apply_semantic_candidate_leads_to_prefix_v1(
            prefix_bundle=prefix,
            lead_index=lead_index,
            continuity_confirmed_prior_card_ids=(
                compatible_prior_card_ids_from_challenge_v1(
                    _load_json(paths["prior_artifact"], "prior challenge artifact")
                )
            ),
        )
        semantic_prefix, occurrence_bridge = (
            materialize_waiting_identity_occurrences_v1(
                document=self.document,
                prefix_bundle=semantic_prefix,
                lead_index=lead_index,
            )
        )
        _write_immutable(
            paths["semantic_identity_occurrence_bridge"], occurrence_bridge
        )
        _write_immutable(paths["prefix_semantic"], semantic_prefix)
        review = append_prefix_identity_uncertainties_v1(
            ledger=_load_json(
                paths["candidate_review_ledger"], "candidate review ledger"
            ),
            prefix_bundle=semantic_prefix,
            chapter_id=stage.chapter_id,
        )
        previous_cases = (
            _load_json(
                paths["previous_review_case_ledger"],
                "previous review-case ledger",
            )
            if paths["previous_review_case_ledger"].is_file()
            else None
        )
        cases = build_review_case_ledger_v1(
            document=self.document,
            chapter_id=stage.chapter_id,
            prefix_bundle=semantic_prefix,
            audited_inventory=_load_json(
                paths["audited_inventory"], "current audited inventory"
            ),
            previous_ledger=previous_cases,
            prior_challenge_artifact=_load_json(
                paths["prior_artifact"], "prior challenge artifact"
            ),
        )
        review = project_ready_cases_to_chapter_review_ledger_v1(
            case_ledger=cases,
            chapter_review_ledger=review,
        )
        _write_immutable(paths["review_case_ledger_pre_identity"], cases)
        _write_immutable(paths["review_ledger_semantic"], review)
        pending = int(occurrence_bridge["counts"]["unresolved_count"]) + int(
            lead_index["counts"]["chapter_cap_deferred_count"]
        )
        return StageExecutionResult(
            status="semantic_pending" if pending else "accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={
                    "semantic_leads": paths["semantic_lead_index"],
                    "semantic_identity_occurrence_bridge": paths[
                        "semantic_identity_occurrence_bridge"
                    ],
                    "semantic_prefix": paths["prefix_semantic"],
                    "review_ledger": paths["review_ledger_semantic"],
                    "review_case_ledger": paths[
                        "review_case_ledger_pre_identity"
                    ],
                },
                run_root=self.run_root,
            ),
            call_disposition="code_only",
            semantic_pending_count=pending,
            cumulative_hash_updates={
                "prefix_hash": semantic_prefix["prefix_bundle_hash"],
                "review_ledger_hash": review["review_ledger_hash"],
                "semantic_lead_index_hash": lead_index["lead_index_hash"],
                "semantic_identity_occurrence_bridge_hash": occurrence_bridge[
                    "bridge_hash"
                ],
                "review_case_ledger_hash": cases["review_case_ledger_hash"],
            },
        )

    def _execute_identity_prepare(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        previous_identity = (
            _load_json(paths["previous_identity_ledger"], "previous identity ledger")
            if paths["previous_identity_ledger"].is_file()
            else None
        )
        index = build_incremental_identity_index_v1(
            document=self.document,
            prefix_bundle=_load_json(paths["prefix_semantic"], "semantic prefix"),
            review_ledger=_load_json(
                paths["review_ledger_semantic"], "semantic review ledger"
            ),
            previous_identity_ledger=previous_identity,
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
                document=self.document,
                design_doc=self.design_doc,
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
        cap = self.cycle_profile.stage_limits["identity_auditor"].max_calls_per_chapter
        if len(component_ids) > cap:
            raise ChapterCycleLiveExecutorError(
                "identity component count exceeds sealed cap"
            )
        pending = sum(bool(row["overflow"]) for row in index["components"])
        return StageExecutionResult(
            status="semantic_pending" if pending else "accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={"identity_index": prepared / "identity_index.json"},
                run_root=self.run_root,
                extra={"eligible_component_ids": component_ids},
            ),
            call_disposition="code_only",
            semantic_pending_count=pending,
        )

    def _execute_identity_components(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        paths = self._paths(stage)
        index = verify_incremental_identity_index_v1(
            _load_json(paths["identity_prepared"] / "identity_index.json", "identity index")
        )
        component_ids = [
            row["component_id"]
            for row in index["components"]
            if not row["overflow"] and row["trigger_state"] != "duplicate_suppressed"
        ]
        if not component_ids:
            return self._model_result(
                stage=stage,
                permit=permit,
                artifacts={"identity_index": paths["identity_prepared"] / "identity_index.json"},
                request_paths=[],
                report_paths=[],
                model_id=self._model_id("identity_auditor"),
            )
        role = self._role("identity_auditor")[1]
        model_id = self._model_id("identity_auditor")
        key_paths: dict[str, Path] | None = None
        request_paths: list[Path] = []
        report_paths: list[Path] = []
        decisions: dict[str, Path] = {}
        for component_id in component_ids:
            request_path = (
                paths["identity_prepared"]
                / "components"
                / component_id
                / "request.json"
            )
            request_paths.append(request_path)
            output = paths["identity_component_live"] / component_id
            decision = output / "decision.json"
            if not decision.is_file():
                if self.backend_mode == BACKEND_MODE_SHARED_V1:
                    request = _load_json(request_path, "shared identity request")
                    response_schema = request["response_schema"]
                    surface_scope_normalizations: list[dict[str, Any]] = []

                    def validate_identity(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                        validate_structured_payload(
                            raw, canonical_schema=response_schema
                        )
                        normalized, records = (
                            normalize_surface_scope_action_coverage_v1(
                                raw,
                                index=index,
                            )
                        )
                        surface_scope_normalizations[:] = records
                        return validate_incremental_identity_response_v1(
                            normalized,
                            index=index,
                            request_fingerprint=request["request_fingerprint"],
                        )

                    validator_ref = build_literary_code_ref_v1(
                        identifier="literary.audit.identity_surface.validator",
                        revision="v1",
                        callables=(
                            validate_structured_payload,
                            validate_incremental_identity_response_v1,
                        ),
                    )
                    permit.reserve("identity_" + canonical_hash(component_id)[:12])
                    result = self._required_shared_runtime().execute_accepted_request(
                        role_id=self._shared_role_id("identity_auditor"),
                        stage_id=stage.stage_id,
                        logical_request_id=(
                            f"{stage.stage_id}_"
                            f"{canonical_hash({'component_id': component_id, 'request_fingerprint': request['request_fingerprint']})[:24]}"
                        ),
                        request=request,
                        schema_name="literary_identity_surface_auditor_v1",
                        semantic_validator=validate_identity,
                        validator_ref=validator_ref,
                        application_contract_id=(
                            "literary.audit.identity_surface.decision_v1"
                        ),
                        application_contract_revision="v1",
                        output_dir=output,
                        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                        additional_input_bindings=(
                            {
                                "name": "literary_identity_index",
                                "sha256": file_sha256(
                                    paths["identity_prepared"]
                                    / "identity_index.json"
                                ),
                            },
                        ),
                    )
                    _write_immutable(decision, result.semantic_payload)
                    _write_immutable(
                        output / "surface_scope_normalizations.json",
                        {
                            "schema_version": (
                                "incremental_identity_surface_scope_normalizations_v1"
                            ),
                            "normalization_count": len(
                                surface_scope_normalizations
                            ),
                            "normalizations": surface_scope_normalizations,
                        },
                    )
                    self._write_shared_report(
                        output_dir=output,
                        stage=stage,
                        role_id=self._shared_role_id("identity_auditor"),
                        request_fingerprint=request["request_fingerprint"],
                        semantic_output=result.semantic_payload,
                    )
                else:
                    if key_paths is None:
                        _resolved_role, key_paths = self._openai_key_paths(
                            "identity_auditor"
                        )
                    dry_dir = paths["identity_component_dry"] / component_id
                    contract = self._structured_contract(
                        stage_role="identity_auditor",
                        canonical_schema=incremental_identity_response_schema_v1(),
                    )
                    if contract is not None:
                        _write_immutable(
                            dry_dir / "structured_output_contract.json",
                            contract.to_payload(),
                        )
                    envelope, preflight, _index, _request = build_identity_live_envelope(
                        prepared_dir=paths["identity_prepared"],
                        frozen_db=self.frozen_db,
                        usage_roots=self.usage_roots,
                        exclude_root=dry_dir,
                        component_id=component_id,
                        bucket_order=role.bucket_order,
                        structured_output_contract=contract,
                    )
                    _write_immutable(dry_dir / "run_envelope.json", envelope)
                    _write_immutable(dry_dir / "quota_preflight.json", preflight)
                    _archive_retryable_output(output)
                    permit.reserve("identity_" + canonical_hash(component_id)[:12])
                    run_identity_component_live(
                        prepared_dir=paths["identity_prepared"],
                        output_dir=output,
                        frozen_db=self.frozen_db,
                        usage_roots=self.usage_roots,
                        approved_envelope_hash=envelope["envelope_hash"],
                        key_paths=key_paths,
                        component_id=component_id,
                        bucket_order=role.bucket_order,
                        structured_output_contract=contract,
                    )
            decisions[component_id] = decision
            report_paths.append(
                output
                / (
                    "shared_execution_report.json"
                    if self.backend_mode == BACKEND_MODE_SHARED_V1
                    else "live_report.json"
                )
            )
        pending = sum(
            _count_value(
                _load_json(path, "identity decision"),
                key="status",
                accepted={"pending", "provisional_link"},
            )
            for path in decisions.values()
        )
        return self._model_result(
            stage=stage,
            permit=permit,
            artifacts={f"decision_{key}": value for key, value in decisions.items()},
            request_paths=request_paths,
            report_paths=report_paths,
            model_id=model_id,
            pending_count=pending,
            extra={"backend_mode": self.backend_mode},
        )

    def _execute_identity_reconcile(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        index = verify_incremental_identity_index_v1(
            _load_json(paths["identity_prepared"] / "identity_index.json", "identity index")
        )
        decisions = [
            _load_json(path, "identity decision")
            for path in sorted(
                paths["identity_component_live"].glob("*/decision.json")
            )
        ]
        previous_identity = (
            _load_json(paths["previous_identity_ledger"], "previous identity ledger")
            if paths["previous_identity_ledger"].is_file()
            else None
        )
        ledger = build_incremental_identity_ledger_v1(
            index=index,
            decisions=decisions,
            previous_identity_ledger=previous_identity,
        )
        prefix = apply_incremental_identity_ledger_to_prefix_v1(
            prefix_bundle=_load_json(paths["prefix_semantic"], "semantic prefix"),
            identity_ledger=ledger,
        )
        review = apply_incremental_identity_ledger_to_review_v1(
            review_ledger=_load_json(
                paths["review_ledger_semantic"], "semantic review ledger"
            ),
            identity_ledger=ledger,
        )
        review_cases = apply_identity_surface_decisions_to_review_cases_v1(
            case_ledger=_load_json(
                paths["review_case_ledger_pre_identity"],
                "pre-identity review-case ledger",
            ),
            chapter_review_ledger=_load_json(
                paths["review_ledger_semantic"], "semantic review ledger"
            ),
            identity_ledger=ledger,
        )
        _write_immutable(paths["identity_ledger"], ledger)
        _write_immutable(paths["final_prefix"], prefix)
        _write_immutable(paths["final_review_ledger"], review)
        _write_immutable(paths["final_review_case_ledger"], review_cases)
        pending = sum(
            row["status"] in {"pending", "provisional_link"}
            for row in ledger["component_states"]
        )
        return StageExecutionResult(
            status="semantic_pending" if pending else "accepted",
            payload=_public_payload(
                stage=stage,
                artifacts={
                    "identity_ledger": paths["identity_ledger"],
                    "final_prefix": paths["final_prefix"],
                    "final_review_ledger": paths["final_review_ledger"],
                    "final_review_case_ledger": paths[
                        "final_review_case_ledger"
                    ],
                },
                run_root=self.run_root,
            ),
            call_disposition="code_only",
            semantic_pending_count=pending,
            cumulative_hash_updates={
                "identity_ledger_hash": ledger["identity_ledger_hash"],
                "prefix_hash": prefix["prefix_bundle_hash"],
                "review_ledger_hash": review["review_ledger_hash"],
                "review_case_ledger_hash": review_cases[
                    "review_case_ledger_hash"
                ],
            },
        )

    def _execute_checkpoint(self, stage: ChapterCycleStage) -> StageExecutionResult:
        paths = self._paths(stage)
        prefix = verify_chapter_prefix_prior_bundle_v1(
            _load_json(paths["final_prefix"], "final chapter prefix"),
            document=self.document,
        )
        artifacts: dict[str, Path] = {"final_prefix": paths["final_prefix"]}
        report_body: dict[str, Any] = {
            "schema_version": "literary_unified_chapter_report_v1",
            "chapter_id": stage.chapter_id,
            "chapter_ordinal": stage.chapter_ordinal,
            "coverage_through_chapter_id": prefix["coverage_through_chapter_id"],
            "prefix_bundle_hash": prefix["prefix_bundle_hash"],
            "active_context_card_count": len(prefix["b0_context_cards"]),
            "candidate_only_card_count": len(prefix["candidate_only_context_cards"]),
            "glossary_context_card_count": len(prefix["glossary_context_cards"]),
            "public_builder_stage": "b1",
            "implementation_builder_stage": (
                "b0" if stage.chapter_ordinal == 1 else "b0_prior"
            ),
            "b2_enabled": False,
            "b2_ready": False,
            "production_publish_performed": False,
        }
        cumulative: dict[str, str] = {"prefix_hash": prefix["prefix_bundle_hash"]}
        for key, path, hash_field, cumulative_key in (
            (
                "final_review_ledger",
                paths["final_review_ledger"],
                "review_ledger_hash",
                "review_ledger_hash",
            ),
            (
                "identity_ledger",
                paths["identity_ledger"],
                "identity_ledger_hash",
                "identity_ledger_hash",
            ),
            (
                "final_review_case_ledger",
                paths["final_review_case_ledger"],
                "review_case_ledger_hash",
                "review_case_ledger_hash",
            ),
            (
                "semantic_lead_index",
                paths["semantic_lead_index"],
                "lead_index_hash",
                "semantic_lead_index_hash",
            ),
            (
                "semantic_identity_occurrence_bridge",
                paths["semantic_identity_occurrence_bridge"],
                "bridge_hash",
                "semantic_identity_occurrence_bridge_hash",
            ),
        ):
            if not path.is_file():
                continue
            artifacts[key] = path
            row = _load_json(path, key)
            observed = row.get(hash_field) or row.get("report_hash")
            if isinstance(observed, str) and len(observed) == 64:
                cumulative[cumulative_key] = observed
                report_body[hash_field] = observed
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write_immutable(paths["chapter_report"], report)
        artifacts["chapter_report"] = paths["chapter_report"]
        return StageExecutionResult(
            status="accepted",
            payload=_public_payload(
                stage=stage,
                artifacts=artifacts,
                run_root=self.run_root,
                extra={"b2_enabled": False, "b2_ready": False},
            ),
            call_disposition="code_only",
            cumulative_hash_updates=cumulative,
        )

    def __call__(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        dispatch = {
            "b0": lambda: self._execute_b1(stage, permit),
            "b0_prior": lambda: self._execute_b1(stage, permit),
            "local_auditor": lambda: self._execute_local_auditor(stage, permit),
            "prefix": lambda: self._execute_prefix(stage),
            "stable_claim_prepare": lambda: self._execute_claim_prepare(stage),
            "stable_claim_components": lambda: self._execute_claim_components(
                stage, permit
            ),
            "stable_claim_reconcile": lambda: self._execute_claim_reconcile(stage),
            "prefix_extend": lambda: self._execute_prefix_extend(stage),
            "semantic_leads": lambda: self._execute_semantic_leads(stage),
            "identity_prepare": lambda: self._execute_identity_prepare(stage),
            "identity_components": lambda: self._execute_identity_components(
                stage, permit
            ),
            "identity_reconcile": lambda: self._execute_identity_reconcile(stage),
            "checkpoint": lambda: self._execute_checkpoint(stage),
        }
        try:
            handler = dispatch[stage.stage_name]
        except KeyError as exc:
            raise ChapterCycleLiveExecutorError(
                f"no executor for stage {stage.stage_name}"
            ) from exc
        return handler()


__all__ = ["ChapterCycleLiveExecutorError", "ChapterCycleLiveExecutorV1"]
