from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.benchmark_runner_v1 import BenchmarkChapterRuntimeV1
from pipeline.eval.benchmark_v1 import (
    augment_common_input_with_benchmark_overlays_v1,
    slice_common_input_chapter_v1,
    validate_benchmark_source_read_models_v1,
    validate_benchmark_manifest_v1,
    validate_benchmark_overlay_v1,
    validate_benchmark_preflight_v1,
)
from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    CommonTranslationV1,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_json,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_method,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input
from pipeline.eval.d2l_package_adapter_v1 import project_d2l_evaluation_package
from pipeline.eval.canonical_d2l_benchmark_bridge_v1 import (
    FinalizedCanonicalSourceArtifactsV1,
    build_canonical_d2l_common_input_v1,
)
from pipeline.eval.end_to_end_runner_v1 import LocalSfQeRuntimeV1
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
    validate_evaluation_workflow_settings_v1,
)
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.offline_orchestrator_v1 import validate_evaluation_run_config
from pipeline.eval.workflow_component_v1 import (
    ARM_IDS_V1,
    SOURCE_BINDING_ROLES_V1,
    validate_scoring_handoff_v1,
    validate_typed_artifact_binding_v1,
)
from pipeline.eval.workflow_executor_v1 import (
    EvaluationWorkflowExecutorRegistrationV1,
    PreparedEvaluationBenchmarkV1,
    RegisteredEvaluationWorkflowExecutorV1,
    materialize_registered_evaluation_settings_v1,
    validate_locked_evaluation_selection_v1,
)
from pipeline.llm_backend import SharedLlmAttemptLedger
from pipeline.workflow_replay.orchestrator_v1 import (
    load_workflow_runtime_registration_v1,
)


__all__ = [
    "EvaluationRuntimeObjectRegistryV1",
    "LoadedWorkflowScoringBaselineTemplateV1",
    "LoadedWorkflowScoringRuntimeV1",
    "WorkflowScoringBaselineTemplateSourcesV1",
    "WorkflowScoringRuntimeArtifactSourcesV1",
    "build_evaluation_workflow_registration_from_baseline_template_v1",
    "build_registered_evaluation_workflow_executor_v1",
    "load_workflow_scoring_baseline_template_from_workflow_runtime_v1",
    "load_workflow_scoring_baseline_template_v1",
    "load_workflow_scoring_runtime_bundle_v1",
    "materialize_workflow_scoring_baseline_template_v1",
    "materialize_workflow_scoring_runtime_bundle_v1",
    "validate_workflow_scoring_baseline_template_v1",
    "validate_workflow_scoring_runtime_bundle_v1",
]


SCHEMA_ID = "WorkflowScoringRuntimeBundleV1"
SCHEMA_VERSION = "1.0.0"
BUNDLE_FILE_NAME = "workflow_scoring_runtime_bundle_v1.json"
BASELINE_TEMPLATE_SCHEMA_ID = "WorkflowScoringBaselineTemplateV1"
BASELINE_TEMPLATE_SCHEMA_VERSION = "1.0.0"
BASELINE_TEMPLATE_FILE_NAME = "workflow_scoring_baseline_template_v1.json"
_SETTINGS_REF = "runtime/evaluation_workflow_settings_v1.json"
_LEGACY_D2L_INPUT_REF = "runtime/d2l_evaluation_input_v1.json"
_MANIFEST_REF = "runtime/benchmark_manifest_v1.json"
_PREFLIGHT_REF = "runtime/benchmark_preflight_v1.json"
_HANDOFF_REF = "handoffs/scoring_handoff.json"
_HASH_PATH = ("integrity", "bundle_sha256")
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("identity", "selected_chapter_ids"),
            ("identity", "selected_arm_ids"),
            ("identity", "selected_scorer_ids"),
            ("authority", "evaluation_profiles"),
            ("authority", "policy_profiles"),
            ("authority", "shared_selections"),
            ("authority", "chapter_ids"),
            ("authority", "arm_ids"),
            ("authority", "scorer_ids"),
            ("bindings", "handoff_artifacts"),
            ("bindings", "authority_artifacts"),
            ("bindings", "overlays"),
            ("bindings", "chapter_configs"),
            ("bindings", "file_bindings"),
            ("presentations", "arms"),
            ("presentations", "methods"),
            ("chapter_runtime_bindings",),
            ("caveats",),
            ("locked_selection", "selected_chapter_ids"),
            ("locked_selection", "selected_arm_ids"),
            ("locked_selection", "selected_scorer_ids"),
        }
    ),
)
_BASELINE_TEMPLATE_HASH_PATH = ("integrity", "template_sha256")
_BASELINE_TEMPLATE_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("supported_chapter_ids",),
            ("external_translation_inputs",),
            ("authority", "evaluation_profiles"),
            ("authority", "policy_profiles"),
            ("authority", "shared_selections"),
            ("authority", "chapter_ids"),
            ("authority", "arm_ids"),
            ("authority", "scorer_ids"),
            ("presentations", "arms"),
            ("presentations", "methods"),
            ("chapter_runtime_bindings",),
            ("file_bindings",),
            ("caveats",),
        }
    ),
)


@dataclass(frozen=True, slots=True)
class WorkflowScoringRuntimeArtifactSourcesV1:
    scoring_handoff: Path
    benchmark_manifest: Path
    benchmark_preflight: Path
    handoff_artifacts: Mapping[str, Path]
    authority_artifacts: Mapping[str, Path]
    overlays: Mapping[tuple[str, str], Path]
    chapter_configs: Mapping[str, Path]
    d2l_evaluation_input: Path | None = None


@dataclass(frozen=True, slots=True)
class WorkflowScoringBaselineTemplateSourcesV1:
    external_translation_inputs: Sequence[Mapping[str, Any]]
    external_translation_artifacts: Mapping[str, Path]
    authority_artifacts: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class EvaluationRuntimeObjectRegistryV1:
    local_sf_qe_runtimes: Mapping[str, LocalSfQeRuntimeV1]
    llm_role_runners: Mapping[str, SharedEvaluationRoleRunnerV1]
    shared_ledgers: Mapping[str, SharedLlmAttemptLedger]


@dataclass(frozen=True, slots=True)
class LoadedWorkflowScoringBaselineTemplateV1:
    template_root: Path
    template: Mapping[str, Any]
    settings_authority: EvaluationWorkflowSettingsAuthorityV1
    file_paths: Mapping[str, Path]

    @property
    def registered_option(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.template["registered_option"]))


@dataclass(frozen=True, slots=True)
class LoadedWorkflowScoringRuntimeV1:
    bundle_root: Path
    bundle: Mapping[str, Any]
    scoring_handoff: Mapping[str, Any]
    workflow_settings: Mapping[str, Any]
    settings_authority: EvaluationWorkflowSettingsAuthorityV1
    d2l_common_input: CommonEvaluationInputV1
    legacy_d2l_evaluation_input: Mapping[str, Any] | None
    benchmark_manifest: Mapping[str, Any]
    benchmark_preflight: Mapping[str, Any]
    benchmark_overlays: tuple[Mapping[str, Any], ...]
    chapter_configs: Mapping[str, Mapping[str, Any]]
    file_paths: Mapping[str, Path]

    @property
    def registered_option(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.bundle["registered_option"]))


class _FileBackedEvaluationBenchmarkInputProviderV1:
    def __init__(
        self,
        loaded: LoadedWorkflowScoringRuntimeV1,
        runtime_registry: EvaluationRuntimeObjectRegistryV1,
    ) -> None:
        self._loaded = loaded
        self._runtime_registry = runtime_registry

    def prepare(
        self,
        *,
        scoring_handoff: Mapping[str, Any],
        workflow_settings: Mapping[str, Any],
        output_root: Path,
    ) -> PreparedEvaluationBenchmarkV1:
        handoff = validate_scoring_handoff_v1(scoring_handoff)
        settings = validate_evaluation_workflow_settings_v1(
            workflow_settings,
            authority=self._loaded.settings_authority,
            scoring_handoff=handoff,
        )
        if handoff != self._loaded.scoring_handoff:
            raise ContractValidationError(
                "handoff_binding",
                "$.runtime_bundle",
                "provider received a handoff other than the file-backed handoff",
            )
        if settings != self._loaded.workflow_settings:
            raise ContractValidationError(
                "settings_binding",
                "$.runtime_bundle",
                "provider received settings other than the file-backed settings",
            )

        common = _normalize_d2l_arm_ids(self._loaded.d2l_common_input)
        selected_chapters = tuple(settings["selected_chapter_ids"])
        selected_arms = tuple(_benchmark_arm_id(row) for row in settings["selected_arm_ids"])
        selected_external = {
            arm_id for arm_id in selected_arms if arm_id not in {"S0", "S1"}
        }
        handoff_inputs = {
            row["arm_id"]: row for row in handoff["translation_inputs"]
        }
        arm_presentations = {
            row["arm_id"]: row for row in self._loaded.bundle["presentations"]["arms"]
        }
        runtime_bindings = {
            row["chapter_id"]: row
            for row in self._loaded.bundle["chapter_runtime_bindings"]
        }
        overlay_by_key = {
            (
                row["source"]["chapter_id"],
                row["arm"]["arm_id"],
            ): row
            for row in self._loaded.benchmark_overlays
        }
        root = Path(output_root).resolve()
        runtimes: dict[str, BenchmarkChapterRuntimeV1] = {}
        for ordinal, chapter_id in enumerate(selected_chapters):
            chapter_common = _select_common_arms(
                slice_common_input_chapter_v1(common, chapter_id),
                selected_arms,
            )
            external_overlays = [
                overlay_by_key[(chapter_id, arm_id)]
                for arm_id in selected_arms
                if arm_id in selected_external
            ]
            if external_overlays:
                chapter_common = augment_common_input_with_benchmark_overlays_v1(
                    chapter_common, external_overlays
                )
            child = root / "chapters" / f"{ordinal:02d}_{chapter_id}"
            input_relative = "input/scoring_handoff_v1.json"
            _copy_immutable(
                self._loaded.file_paths[_HANDOFF_REF],
                _contained_path(child, input_relative),
            )
            chapter_arm_presentations: list[dict[str, str]] = []
            for settings_arm_id in settings["selected_arm_ids"]:
                presentation = arm_presentations[settings_arm_id]
                handoff_input = handoff_inputs[settings_arm_id]
                relative = f"translations/{settings_arm_id}.json"
                source_ref = handoff_input["translation_artifact"]["artifact_ref"]
                _copy_immutable(
                    self._loaded.file_paths[source_ref],
                    _contained_path(child, relative),
                )
                chapter_arm_presentations.append(
                    {
                        "arm_id": _benchmark_arm_id(settings_arm_id),
                        "role": presentation["role"],
                        "kind": presentation["kind"],
                        "label": presentation["label"],
                        "relative_path": relative,
                    }
                )
            runtime_binding = runtime_bindings[chapter_id]
            runtimes[chapter_id] = BenchmarkChapterRuntimeV1(
                common_input=chapter_common,
                config_payload=copy.deepcopy(self._loaded.chapter_configs[chapter_id]),
                input_artifact={
                    "artifact_id": f"scoring-handoff-{chapter_id}",
                    "relative_path": input_relative,
                    "sha256": _physical_sha256(
                        self._loaded.file_paths[_HANDOFF_REF]
                    ),
                },
                arm_presentations=chapter_arm_presentations,
                method_presentations=copy.deepcopy(
                    self._loaded.bundle["presentations"]["methods"]
                ),
                local_sf_qe_runtime=_lookup_optional_runtime(
                    self._runtime_registry.local_sf_qe_runtimes,
                    runtime_binding["local_sf_qe_runtime_id"],
                    path=f"$.chapter_runtime_bindings[{chapter_id}].local_sf_qe_runtime_id",
                ),
                llm_roles=_lookup_optional_runtime(
                    self._runtime_registry.llm_role_runners,
                    runtime_binding["llm_roles_runtime_id"],
                    path=f"$.chapter_runtime_bindings[{chapter_id}].llm_roles_runtime_id",
                ),
                shared_ledger=_lookup_optional_runtime(
                    self._runtime_registry.shared_ledgers,
                    runtime_binding["shared_ledger_runtime_id"],
                    path=f"$.chapter_runtime_bindings[{chapter_id}].shared_ledger_runtime_id",
                ),
                shared_ledger_relative_path=runtime_binding[
                    "shared_ledger_relative_path"
                ],
                caveats=tuple(self._loaded.bundle["caveats"]),
            )
        return PreparedEvaluationBenchmarkV1(
            accepted_scoring_handoff=copy.deepcopy(handoff),
            accepted_workflow_settings=copy.deepcopy(settings),
            benchmark_manifest=copy.deepcopy(self._loaded.benchmark_manifest),
            benchmark_preflight=copy.deepcopy(self._loaded.benchmark_preflight),
            benchmark_overlays=copy.deepcopy(self._loaded.benchmark_overlays),
            chapter_runtimes=runtimes,
        )


def materialize_workflow_scoring_baseline_template_v1(
    output_root: Path,
    *,
    template_id: str,
    created_at: str,
    producer_code_commit: str,
    source_binding_sha256: str,
    settings_option_id: str,
    registered_option_sha256: str,
    evaluation_profile_id: str,
    evaluation_profile_ref: str,
    policy_profile_id: str | None,
    policy_profile_ref: str | None,
    shared_selection_ref: str,
    settings_authority: EvaluationWorkflowSettingsAuthorityV1,
    artifact_sources: WorkflowScoringBaselineTemplateSourcesV1,
    caveats: Sequence[str] = (),
) -> Path:
    """Write pre-run baseline authority without a run handoff or settings."""

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    authority = _authority_payload(settings_authority)
    registered_option = _validate_baseline_registered_option(
        {
            "settings_option_id": settings_option_id,
            "registered_option_sha256": registered_option_sha256,
            "evaluation_profile_id": evaluation_profile_id,
            "evaluation_profile_ref": _find_authority_binding(
                authority["evaluation_profiles"],
                evaluation_profile_ref,
                path="$.evaluation_profile_ref",
            ),
            "policy_profile_id": policy_profile_id,
            "policy_profile_ref": (
                None
                if policy_profile_ref is None
                else _find_authority_binding(
                    authority["policy_profiles"],
                    policy_profile_ref,
                    path="$.policy_profile_ref",
                )
            ),
            "shared_selection_ref": _find_authority_binding(
                authority["shared_selections"],
                shared_selection_ref,
                path="$.shared_selection_ref",
            ),
        },
        authority=authority,
    )
    if (registered_option["policy_profile_id"] is None) != (
        registered_option["policy_profile_ref"] is None
    ):
        raise ContractValidationError(
            "policy_profile_binding",
            "$.registered_option",
            "policy profile id and artifact reference must both be set or null",
        )
    external_inputs = _validate_external_translation_inputs(
        artifact_sources.external_translation_inputs
    )
    expected_external_refs = tuple(
        row["translation_artifact"]["artifact_ref"] for row in external_inputs
    )
    _require_exact_source_map(
        artifact_sources.external_translation_artifacts,
        expected_refs=expected_external_refs,
        path="$.artifact_sources.external_translation_artifacts",
    )
    authority_refs = _authority_file_bindings(authority)
    _require_exact_source_map(
        artifact_sources.authority_artifacts,
        expected_refs=tuple(row["artifact_ref"] for row in authority_refs),
        path="$.artifact_sources.authority_artifacts",
    )

    source_by_ref: dict[str, Path] = {}
    declared_by_ref = {
        row["translation_artifact"]["artifact_ref"]: row["translation_artifact"]
        for row in external_inputs
    }
    declared_by_ref.update({row["artifact_ref"]: row for row in authority_refs})
    for artifact_ref, source_path in artifact_sources.external_translation_artifacts.items():
        _insert_source(source_by_ref, artifact_ref, Path(source_path))
    for artifact_ref, source_path in artifact_sources.authority_artifacts.items():
        _insert_source(source_by_ref, artifact_ref, Path(source_path))

    file_bindings: list[dict[str, str]] = []
    for artifact_ref in sorted(source_by_ref):
        source_path = source_by_ref[artifact_ref].resolve()
        declared = declared_by_ref[artifact_ref]
        if declared["sha256_kind"] != "physical":
            raise ContractValidationError(
                "runtime_file_authority",
                artifact_ref,
                "pre-run template requires physical producer bindings",
            )
        if not source_path.is_file() or _physical_sha256(source_path) != declared["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                artifact_ref,
                "template source bytes differ from the producer binding",
            )
        destination = _contained_path(root, artifact_ref)
        if source_path != destination:
            _copy_immutable(source_path, destination)
        file_bindings.append(
            _physical_binding(
                artifact_ref,
                declared["artifact_kind"],
                destination,
                schema_version=declared["schema_version"],
            )
        )

    draft = {
        "schema_id": BASELINE_TEMPLATE_SCHEMA_ID,
        "schema_version": BASELINE_TEMPLATE_SCHEMA_VERSION,
        "template_id": require_string(template_id, path="$.template_id"),
        "created_at": require_rfc3339(created_at, path="$.created_at"),
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_scoring_baseline_template_v1",
            "component_version": BASELINE_TEMPLATE_SCHEMA_VERSION,
            "code_commit": require_commit(
                producer_code_commit, path="$.producer_code_commit"
            ),
        },
        "source_binding_sha256": require_sha256(
            source_binding_sha256, path="$.source_binding_sha256"
        ),
        "supported_chapter_ids": copy.deepcopy(authority["chapter_ids"]),
        "registered_option": registered_option,
        "authority": authority,
        "external_translation_inputs": external_inputs,
        "file_bindings": file_bindings,
        "caveats": [
            require_string(item, path=f"$.caveats[{index}]")
            for index, item in enumerate(caveats)
        ],
        "integrity": {"template_sha256": "0" * 64},
    }
    template = validate_workflow_scoring_baseline_template_v1(
        seal_payload(
            draft,
            policy=_BASELINE_TEMPLATE_POLICY,
            hash_path=_BASELINE_TEMPLATE_HASH_PATH,
        )
    )
    template_path = root / BASELINE_TEMPLATE_FILE_NAME
    _write_immutable(
        template_path,
        _json_bytes(template, policy=_BASELINE_TEMPLATE_POLICY),
    )
    return template_path


def validate_workflow_scoring_baseline_template_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = require_mapping(value, path="$template")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "template_id",
            "created_at",
            "producer",
            "source_binding_sha256",
            "supported_chapter_ids",
            "registered_option",
            "authority",
            "external_translation_inputs",
            "file_bindings",
            "caveats",
            "integrity",
        },
        path="$template",
    )
    authority = _validate_authority(row["authority"])
    supported_chapters = [
        require_string(item, path=f"$template.supported_chapter_ids[{index}]")
        for index, item in enumerate(
            require_list(
                row["supported_chapter_ids"],
                path="$template.supported_chapter_ids",
            )
        )
    ]
    if supported_chapters != authority["chapter_ids"]:
        raise ContractValidationError(
            "chapter_scope",
            "$template.supported_chapter_ids",
            "template chapter universe must equal the registered authority",
        )
    external_inputs = _validate_external_translation_inputs(
        row["external_translation_inputs"]
    )
    registered_option = _validate_baseline_registered_option(
        row["registered_option"], authority=authority
    )
    file_bindings = [
        validate_typed_artifact_binding_v1(
            item, path=f"$template.file_bindings[{index}]"
        )
        for index, item in enumerate(
            require_list(row["file_bindings"], path="$template.file_bindings")
        )
    ]
    require_unique(
        [item["artifact_ref"] for item in file_bindings],
        path="$template.file_bindings",
    )
    expected_refs = {
        *(row["translation_artifact"]["artifact_ref"] for row in external_inputs),
        *(row["artifact_ref"] for row in _authority_file_bindings(authority)),
    }
    if {row["artifact_ref"] for row in file_bindings} != expected_refs:
        raise ContractValidationError(
            "file_exact_cover",
            "$template.file_bindings",
            "template files must exact-cover baselines and settings authority",
        )
    integrity = require_mapping(row["integrity"], path="$template.integrity")
    require_exact_keys(
        integrity,
        required={"template_sha256"},
        path="$template.integrity",
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"],
            {BASELINE_TEMPLATE_SCHEMA_ID},
            path="$template.schema_id",
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {BASELINE_TEMPLATE_SCHEMA_VERSION},
            path="$template.schema_version",
        ),
        "template_id": require_string(
            row["template_id"], path="$template.template_id"
        ),
        "created_at": require_rfc3339(
            row["created_at"], path="$template.created_at"
        ),
        "producer": validate_producer(
            row["producer"], path="$template.producer", workstream="evaluation"
        ),
        "source_binding_sha256": require_sha256(
            row["source_binding_sha256"],
            path="$template.source_binding_sha256",
        ),
        "supported_chapter_ids": supported_chapters,
        "registered_option": registered_option,
        "authority": authority,
        "external_translation_inputs": external_inputs,
        "file_bindings": file_bindings,
        "caveats": [
            require_string(item, path=f"$template.caveats[{index}]")
            for index, item in enumerate(
                require_list(row["caveats"], path="$template.caveats")
            )
        ],
        "integrity": {
            "template_sha256": require_sha256(
                integrity["template_sha256"],
                path="$template.integrity.template_sha256",
            )
        },
    }
    if normalized["producer"]["component"] != "workflow_scoring_baseline_template_v1":
        raise ContractValidationError(
            "producer",
            "$template.producer.component",
            "unexpected baseline-template producer",
        )
    if not verify_payload_hash(
        normalized,
        policy=_BASELINE_TEMPLATE_POLICY,
        hash_path=_BASELINE_TEMPLATE_HASH_PATH,
    ):
        raise ContractValidationError(
            "template_hash",
            "$template.integrity.template_sha256",
            "baseline template hash drift",
        )
    result = canonicalize(normalized, policy=_BASELINE_TEMPLATE_POLICY)
    assert isinstance(result, dict)
    return result


def load_workflow_scoring_baseline_template_v1(
    template_path: Path,
) -> LoadedWorkflowScoringBaselineTemplateV1:
    path = Path(template_path).resolve()
    template = validate_workflow_scoring_baseline_template_v1(_read_json(path))
    file_paths = _validate_bundle_files(path.parent, template["file_bindings"])
    authority = _authority_object(template["authority"])
    _require_authority_file_bindings(
        template["authority"],
        declared=_authority_file_bindings(template["authority"]),
        file_paths=file_paths,
    )
    return LoadedWorkflowScoringBaselineTemplateV1(
        template_root=path.parent,
        template=template,
        settings_authority=authority,
        file_paths=file_paths,
    )


def load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
    job_root: Path,
    *,
    expected_job_id: str,
    expected_source_binding_sha256: str,
    selected_chapter_ids: Sequence[str],
) -> LoadedWorkflowScoringBaselineTemplateV1:
    """Load only the pre-run template registered in workflow_runtime_v1."""

    root = Path(job_root).resolve()
    registration = load_workflow_runtime_registration_v1(
        root,
        expected_job_id=expected_job_id,
        expected_source_binding_sha256=expected_source_binding_sha256,
        selected_chapter_ids=selected_chapter_ids,
    )
    baseline = registration["baseline_bundle"]
    if baseline["arm_ids"] != ["community", "google_nmt", "llm_lc"]:
        raise ContractValidationError(
            "runtime_baseline_arms",
            "$.workflow_runtime_v1.baseline_bundle.arm_ids",
            "pre-run template must register exactly three external baselines",
        )
    loaded = load_workflow_scoring_baseline_template_v1(
        _contained_path(root, baseline["artifact_ref"])
    )
    if (
        loaded.template["source_binding_sha256"]
        != expected_source_binding_sha256.lower()
    ):
        raise ContractValidationError(
            "source_binding",
            "$template.source_binding_sha256",
            "baseline template belongs to another canonical source",
        )
    selected = [
        require_string(item, path=f"$.selected_chapter_ids[{index}]")
        for index, item in enumerate(selected_chapter_ids)
    ]
    if any(
        chapter_id not in loaded.template["supported_chapter_ids"]
        for chapter_id in selected
    ):
        raise ContractValidationError(
            "chapter_scope",
            "$template.supported_chapter_ids",
            "baseline template does not support the selected chapter scope",
        )
    return loaded


def build_evaluation_workflow_registration_from_baseline_template_v1(
    loaded_template: LoadedWorkflowScoringBaselineTemplateV1,
    *,
    scoring_handoff: Mapping[str, Any],
    locked_selection: Mapping[str, Any],
    workflow_run_id: str,
    component_run_id: str,
    output_root: Path,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
) -> EvaluationWorkflowExecutorRegistrationV1:
    """Materialize exact Settings 1.1 after Translation and before scoring."""

    if not isinstance(loaded_template, LoadedWorkflowScoringBaselineTemplateV1):
        raise TypeError(
            "loaded_template must be LoadedWorkflowScoringBaselineTemplateV1"
        )
    handoff = validate_scoring_handoff_v1(scoring_handoff)
    if handoff["workflow_run_id"] != workflow_run_id:
        raise ContractValidationError(
            "workflow_binding",
            "$handoff.workflow_run_id",
            "handoff belongs to another workflow",
        )
    external = [
        row for row in handoff["translation_inputs"] if row["arm_id"] not in {"s0", "s1"}
    ]
    if external != loaded_template.template["external_translation_inputs"]:
        raise ContractValidationError(
            "baseline_binding",
            "$handoff.translation_inputs",
            "run handoff does not echo the exact registered external baselines",
        )
    option = loaded_template.template["registered_option"]
    settings = materialize_registered_evaluation_settings_v1(
        scoring_handoff=handoff,
        settings_authority=loaded_template.settings_authority,
        locked_selection=locked_selection,
        settings_option_id=option["settings_option_id"],
        registered_option_sha256=option["registered_option_sha256"],
        evaluation_profile_ref=option["evaluation_profile_ref"]["artifact_ref"],
        policy_profile_ref=(
            None
            if option["policy_profile_ref"] is None
            else option["policy_profile_ref"]["artifact_ref"]
        ),
        shared_selection_ref=option["shared_selection_ref"]["artifact_ref"],
    )
    return EvaluationWorkflowExecutorRegistrationV1(
        workflow_run_id=workflow_run_id,
        component_run_id=component_run_id,
        output_root=Path(output_root),
        generated_at=generated_at,
        producer_code_commit=producer_code_commit,
        evaluation_logical_run_id=evaluation_logical_run_id,
        evaluation_attempt_run_id=evaluation_attempt_run_id,
        evaluation_profile_id=option["evaluation_profile_id"],
        evaluation_profile_ref=option["evaluation_profile_ref"]["artifact_ref"],
        policy_profile_id=option["policy_profile_id"],
        policy_profile_ref=(
            None
            if option["policy_profile_ref"] is None
            else option["policy_profile_ref"]["artifact_ref"]
        ),
        shared_selection_ref=option["shared_selection_ref"]["artifact_ref"],
        settings_option_id=option["settings_option_id"],
        registered_option_sha256=option["registered_option_sha256"],
        locked_selection=copy.deepcopy(locked_selection),
        settings_authority=loaded_template.settings_authority,
        materialized_workflow_settings=settings,
    )


def materialize_workflow_scoring_runtime_bundle_v1(
    output_root: Path,
    *,
    registration: EvaluationWorkflowExecutorRegistrationV1,
    artifact_sources: WorkflowScoringRuntimeArtifactSourcesV1,
    arm_presentations: Sequence[Mapping[str, Any]],
    method_presentations: Sequence[Mapping[str, Any]],
    chapter_runtime_bindings: Sequence[Mapping[str, Any]],
    caveats: Sequence[str] = (),
) -> Path:
    """Write one immutable, explicit runtime bundle without scanning directories."""

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    handoff = validate_scoring_handoff_v1(_read_json(artifact_sources.scoring_handoff))
    expected_settings = materialize_registered_evaluation_settings_v1(
        scoring_handoff=handoff,
        settings_authority=registration.settings_authority,
        locked_selection=registration.locked_selection,
        settings_option_id=registration.settings_option_id,
        registered_option_sha256=registration.registered_option_sha256,
        evaluation_profile_ref=registration.evaluation_profile_ref,
        policy_profile_ref=registration.policy_profile_ref,
        shared_selection_ref=registration.shared_selection_ref,
    )
    supplied_settings = validate_evaluation_workflow_settings_v1(
        registration.materialized_workflow_settings,
        authority=registration.settings_authority,
        scoring_handoff=handoff,
    )
    if supplied_settings != expected_settings:
        raise ContractValidationError(
            "settings_materialization",
            "$.registration.materialized_workflow_settings",
            "bundle may only persist the deterministic registered settings",
        )
    if handoff["workflow_run_id"] != registration.workflow_run_id:
        raise ContractValidationError(
            "workflow_binding",
            "$.scoring_handoff.workflow_run_id",
            "runtime registration belongs to another workflow",
        )
    locked = validate_locked_evaluation_selection_v1(
        registration.locked_selection,
        expected_settings_option_id=registration.settings_option_id,
        expected_registered_option_sha256=registration.registered_option_sha256,
    )
    authority = _authority_payload(registration.settings_authority)
    manifest = validate_benchmark_manifest_v1(
        _read_json(artifact_sources.benchmark_manifest)
    )
    preflight = validate_benchmark_preflight_v1(
        _read_json(artifact_sources.benchmark_preflight)
    )
    handoff_refs = _handoff_file_bindings(handoff)
    _require_exact_source_map(
        artifact_sources.handoff_artifacts,
        expected_refs=tuple(row["artifact_ref"] for row in handoff_refs),
        path="$.artifact_sources.handoff_artifacts",
    )
    _require_source_bytes(
        handoff_refs,
        artifact_sources.handoff_artifacts,
        path="$.artifact_sources.handoff_artifacts",
    )
    d2l_common, legacy_d2l_input, d2l_input_binding = _load_d2l_input_sources(
        handoff=handoff,
        selected_chapter_ids=tuple(locked["selected_chapter_ids"]),
        handoff_artifacts=artifact_sources.handoff_artifacts,
        legacy_d2l_evaluation_input=artifact_sources.d2l_evaluation_input,
    )
    overlays = _load_overlay_sources(
        artifact_sources.overlays,
        selected_chapters=tuple(locked["selected_chapter_ids"]),
        selected_arms=tuple(_benchmark_arm_id(row) for row in locked["selected_arm_ids"]),
    )
    configs = _load_config_sources(
        artifact_sources.chapter_configs,
        selected_chapters=tuple(locked["selected_chapter_ids"]),
    )
    _validate_runtime_scope(
        handoff=handoff,
        settings=supplied_settings,
        d2l_common=d2l_common,
        legacy_d2l_input=legacy_d2l_input,
        manifest=manifest,
        preflight=preflight,
        overlays=overlays,
        configs=configs,
    )
    normalized_arms = _validate_arm_presentations(
        arm_presentations, selected_arm_ids=locked["selected_arm_ids"]
    )
    normalized_methods = _validate_method_presentations(
        method_presentations, selected_scorer_ids=locked["selected_scorer_ids"]
    )
    normalized_runtime_bindings = _validate_chapter_runtime_bindings(
        chapter_runtime_bindings,
        selected_chapter_ids=locked["selected_chapter_ids"],
        selected_scorer_ids=locked["selected_scorer_ids"],
    )
    caveat_rows = [
        require_string(row, path=f"$.caveats[{index}]")
        for index, row in enumerate(caveats)
    ]

    authority_refs = _authority_file_bindings(authority)
    _require_exact_source_map(
        artifact_sources.authority_artifacts,
        expected_refs=tuple(row["artifact_ref"] for row in authority_refs),
        path="$.artifact_sources.authority_artifacts",
    )

    source_by_ref: dict[str, Path] = {}
    for artifact_ref, source_path in (
        (_HANDOFF_REF, artifact_sources.scoring_handoff),
        (_MANIFEST_REF, artifact_sources.benchmark_manifest),
        (_PREFLIGHT_REF, artifact_sources.benchmark_preflight),
    ):
        _insert_source(source_by_ref, artifact_ref, Path(source_path))
    if artifact_sources.d2l_evaluation_input is not None:
        _insert_source(
            source_by_ref,
            _LEGACY_D2L_INPUT_REF,
            Path(artifact_sources.d2l_evaluation_input),
        )
    for artifact_ref, source_path in artifact_sources.handoff_artifacts.items():
        _insert_source(source_by_ref, artifact_ref, Path(source_path))
    for artifact_ref, source_path in artifact_sources.authority_artifacts.items():
        _insert_source(source_by_ref, artifact_ref, Path(source_path))
    runtime_schema_versions = {
        _HANDOFF_REF: handoff["schema_version"],
        _SETTINGS_REF: supplied_settings["schema_version"],
        _MANIFEST_REF: manifest["schema_version"],
        _PREFLIGHT_REF: preflight["schema_version"],
    }
    if legacy_d2l_input is not None:
        runtime_schema_versions[_LEGACY_D2L_INPUT_REF] = legacy_d2l_input[
            "schema_version"
        ]
    overlay_by_key = {
        (row["source"]["chapter_id"], row["arm"]["arm_id"]): row
        for row in overlays
    }
    overlay_rows: list[dict[str, Any]] = []
    for (chapter_id, arm_id), source_path in artifact_sources.overlays.items():
        artifact_ref = f"runtime/overlays/{arm_id}/{chapter_id}.json"
        _insert_source(source_by_ref, artifact_ref, Path(source_path))
        overlay = overlay_by_key[(chapter_id, arm_id)]
        runtime_schema_versions[artifact_ref] = overlay["schema_version"]
        overlay_rows.append(
            {
                "chapter_id": chapter_id,
                "arm_id": arm_id,
                "artifact": _physical_binding(
                    artifact_ref,
                    "evaluation_benchmark_overlay_v1",
                    source_path,
                    schema_version=overlay["schema_version"],
                ),
            }
        )
    config_rows: list[dict[str, Any]] = []
    for chapter_id, source_path in artifact_sources.chapter_configs.items():
        artifact_ref = f"runtime/configs/{chapter_id}.json"
        _insert_source(source_by_ref, artifact_ref, Path(source_path))
        runtime_schema_versions[artifact_ref] = configs[chapter_id][
            "schema_version"
        ]
        config_rows.append(
            {
                "chapter_id": chapter_id,
                "artifact": _physical_binding(
                    artifact_ref,
                    "evaluation_run_config_v1",
                    source_path,
                    schema_version=configs[chapter_id]["schema_version"],
                ),
            }
        )
    settings_bytes = _json_bytes(supplied_settings)
    settings_path = _contained_path(root, _SETTINGS_REF)
    _write_immutable(settings_path, settings_bytes)
    _insert_source(source_by_ref, _SETTINGS_REF, settings_path)

    expected_declared = {
        row["artifact_ref"]: row for row in [*handoff_refs, *authority_refs]
    }
    file_bindings: list[dict[str, str]] = []
    for artifact_ref in sorted(source_by_ref):
        source_path = Path(source_by_ref[artifact_ref]).resolve()
        if not source_path.is_file():
            raise ContractValidationError(
                "missing_artifact", str(source_path), "runtime source file is absent"
            )
        declared = expected_declared.get(artifact_ref)
        if declared is not None:
            if declared["sha256_kind"] != "physical":
                raise ContractValidationError(
                    "runtime_file_authority",
                    artifact_ref,
                    "file-backed runtime requires physical producer bindings",
                )
            if _physical_sha256(source_path) != declared["sha256"]:
                raise ContractValidationError(
                    "artifact_hash",
                    artifact_ref,
                    "runtime source bytes differ from the producer binding",
                )
        destination = _contained_path(root, artifact_ref)
        if source_path != destination:
            _copy_immutable(source_path, destination)
        file_bindings.append(
            _physical_binding(
                artifact_ref,
                (
                    declared["artifact_kind"]
                    if declared is not None
                    else _runtime_artifact_kind(artifact_ref)
                ),
                destination,
                schema_version=(
                    declared["schema_version"]
                    if declared is not None
                    else runtime_schema_versions[artifact_ref]
                ),
            )
        )

    draft = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "created_at": require_rfc3339(
            registration.generated_at, path="$.registration.generated_at"
        ),
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_runtime_bundle_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": require_commit(
                registration.producer_code_commit,
                path="$.registration.producer_code_commit",
            ),
        },
        "identity": {
            "workflow_run_id": require_string(
                registration.workflow_run_id,
                path="$.registration.workflow_run_id",
            ),
            "component_run_id": require_string(
                registration.component_run_id,
                path="$.registration.component_run_id",
            ),
            "evaluation_logical_run_id": require_string(
                registration.evaluation_logical_run_id,
                path="$.registration.evaluation_logical_run_id",
            ),
            "evaluation_attempt_run_id": require_string(
                registration.evaluation_attempt_run_id,
                path="$.registration.evaluation_attempt_run_id",
            ),
            "evaluation_profile_id": require_string(
                registration.evaluation_profile_id,
                path="$.registration.evaluation_profile_id",
            ),
            "policy_profile_id": require_nullable_string(
                registration.policy_profile_id,
                path="$.registration.policy_profile_id",
            ),
            "input_set_sha256": handoff["input_set_sha256"],
            "settings_sha256": supplied_settings["settings_sha256"],
            "selected_chapter_ids": copy.deepcopy(locked["selected_chapter_ids"]),
            "selected_arm_ids": copy.deepcopy(locked["selected_arm_ids"]),
            "selected_scorer_ids": copy.deepcopy(locked["selected_scorer_ids"]),
        },
        "registered_option": {
            "settings_option_id": registration.settings_option_id,
            "registered_option_sha256": registration.registered_option_sha256,
            "benchmark_preset_ref": copy.deepcopy(authority["benchmark_preset"]),
            "evaluation_config_ref": copy.deepcopy(authority["evaluation_config"]),
            "scorer_set_ref": copy.deepcopy(authority["scorer_set"]),
            "evaluation_profile_ref": copy.deepcopy(
                supplied_settings["evaluation_profile_ref"]
            ),
            "policy_profile_ref": copy.deepcopy(
                supplied_settings["policy_profile_ref"]
            ),
            "shared_selection_ref": copy.deepcopy(
                supplied_settings["shared_selection_ref"]
            ),
            "selection_sha256": locked["selection_sha256"],
            "workflow_settings_sha256": supplied_settings["settings_sha256"],
        },
        "authority": authority,
        "locked_selection": copy.deepcopy(locked),
        "bindings": {
            "scoring_handoff": _physical_binding(
                _HANDOFF_REF,
                "scoring_handoff_v1",
                _contained_path(root, _HANDOFF_REF),
                schema_version=handoff["schema_version"],
            ),
            "workflow_settings": _physical_binding(
                _SETTINGS_REF,
                "evaluation_workflow_settings_v1",
                settings_path,
                schema_version=supplied_settings["schema_version"],
            ),
            "d2l_input": d2l_input_binding,
            "benchmark_manifest": _physical_binding(
                _MANIFEST_REF,
                "evaluation_benchmark_manifest_v1",
                _contained_path(root, _MANIFEST_REF),
                schema_version=manifest["schema_version"],
            ),
            "benchmark_preflight": _physical_binding(
                _PREFLIGHT_REF,
                "evaluation_benchmark_preflight_v1",
                _contained_path(root, _PREFLIGHT_REF),
                schema_version=preflight["schema_version"],
            ),
            "handoff_artifacts": copy.deepcopy(handoff_refs),
            "authority_artifacts": copy.deepcopy(authority_refs),
            "overlays": sorted(
                overlay_rows,
                key=lambda row: (
                    locked["selected_chapter_ids"].index(row["chapter_id"]),
                    tuple(_benchmark_arm_id(item) for item in locked["selected_arm_ids"]).index(
                        row["arm_id"]
                    ),
                ),
            ),
            "chapter_configs": sorted(
                config_rows,
                key=lambda row: locked["selected_chapter_ids"].index(
                    row["chapter_id"]
                ),
            ),
            "file_bindings": file_bindings,
        },
        "presentations": {
            "arms": normalized_arms,
            "methods": normalized_methods,
        },
        "chapter_runtime_bindings": normalized_runtime_bindings,
        "caveats": caveat_rows,
        "integrity": {"bundle_sha256": "0" * 64},
    }
    bundle = validate_workflow_scoring_runtime_bundle_v1(
        seal_payload(draft, policy=_POLICY, hash_path=_HASH_PATH)
    )
    bundle_path = root / BUNDLE_FILE_NAME
    _write_immutable(bundle_path, _json_bytes(bundle, policy=_POLICY))
    return bundle_path


def validate_workflow_scoring_runtime_bundle_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = require_mapping(value, path="$bundle")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "identity",
            "registered_option",
            "authority",
            "locked_selection",
            "bindings",
            "presentations",
            "chapter_runtime_bindings",
            "caveats",
            "integrity",
        },
        path="$bundle",
    )
    identity = _validate_identity(row["identity"])
    authority = _validate_authority(row["authority"])
    locked = validate_locked_evaluation_selection_v1(
        row["locked_selection"],
        expected_settings_option_id=require_string(
            require_mapping(
                row["registered_option"], path="$bundle.registered_option"
            ).get("settings_option_id"),
            path="$bundle.registered_option.settings_option_id",
        ),
        expected_registered_option_sha256=require_sha256(
            require_mapping(
                row["registered_option"], path="$bundle.registered_option"
            ).get("registered_option_sha256"),
            path="$bundle.registered_option.registered_option_sha256",
        ),
    )
    if (
        identity["selected_chapter_ids"] != locked["selected_chapter_ids"]
        or identity["selected_arm_ids"] != locked["selected_arm_ids"]
        or identity["selected_scorer_ids"] != locked["selected_scorer_ids"]
    ):
        raise ContractValidationError(
            "selection_binding",
            "$bundle.identity",
            "bundle identity and locked selection differ",
        )
    registered_option = _validate_registered_option(
        row["registered_option"], authority=authority, locked_selection=locked
    )
    if registered_option["workflow_settings_sha256"] != identity["settings_sha256"]:
        raise ContractValidationError(
            "settings_binding",
            "$bundle.registered_option.workflow_settings_sha256",
            "registered option names another settings artifact",
        )
    bindings = _validate_bindings(
        row["bindings"],
        selected_chapter_ids=identity["selected_chapter_ids"],
        selected_arm_ids=identity["selected_arm_ids"],
    )
    presentations = {
        "arms": _validate_arm_presentations(
            require_mapping(row["presentations"], path="$bundle.presentations").get(
                "arms"
            ),
            selected_arm_ids=identity["selected_arm_ids"],
        ),
        "methods": _validate_method_presentations(
            require_mapping(row["presentations"], path="$bundle.presentations").get(
                "methods"
            ),
            selected_scorer_ids=identity["selected_scorer_ids"],
        ),
    }
    require_exact_keys(
        require_mapping(row["presentations"], path="$bundle.presentations"),
        required={"arms", "methods"},
        path="$bundle.presentations",
    )
    runtimes = _validate_chapter_runtime_bindings(
        row["chapter_runtime_bindings"],
        selected_chapter_ids=identity["selected_chapter_ids"],
        selected_scorer_ids=identity["selected_scorer_ids"],
    )
    caveats = [
        require_string(item, path=f"$bundle.caveats[{index}]")
        for index, item in enumerate(
            require_list(row["caveats"], path="$bundle.caveats")
        )
    ]
    integrity = require_mapping(row["integrity"], path="$bundle.integrity")
    require_exact_keys(
        integrity, required={"bundle_sha256"}, path="$bundle.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {SCHEMA_ID}, path="$bundle.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"], {SCHEMA_VERSION}, path="$bundle.schema_version"
        ),
        "created_at": require_rfc3339(
            row["created_at"], path="$bundle.created_at"
        ),
        "producer": validate_producer(
            row["producer"], path="$bundle.producer", workstream="evaluation"
        ),
        "identity": identity,
        "registered_option": registered_option,
        "authority": authority,
        "locked_selection": locked,
        "bindings": bindings,
        "presentations": presentations,
        "chapter_runtime_bindings": runtimes,
        "caveats": caveats,
        "integrity": {
            "bundle_sha256": require_sha256(
                integrity["bundle_sha256"],
                path="$bundle.integrity.bundle_sha256",
            )
        },
    }
    if normalized["producer"]["component"] != "workflow_runtime_bundle_v1":
        raise ContractValidationError(
            "producer",
            "$bundle.producer.component",
            "unexpected runtime bundle producer",
        )
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_HASH_PATH):
        raise ContractValidationError(
            "bundle_hash",
            "$bundle.integrity.bundle_sha256",
            "runtime bundle hash drift",
        )
    result = canonicalize(normalized, policy=_POLICY)
    assert isinstance(result, dict)
    return result


def load_workflow_scoring_runtime_bundle_v1(
    bundle_path: Path,
) -> LoadedWorkflowScoringRuntimeV1:
    path = Path(bundle_path).resolve()
    bundle = validate_workflow_scoring_runtime_bundle_v1(_read_json(path))
    root = path.parent
    file_paths = _validate_bundle_files(root, bundle["bindings"]["file_bindings"])
    handoff = validate_scoring_handoff_v1(
        _read_json(file_paths[bundle["bindings"]["scoring_handoff"]["artifact_ref"]])
    )
    if (
        handoff["workflow_run_id"] != bundle["identity"]["workflow_run_id"]
        or handoff["input_set_sha256"] != bundle["identity"]["input_set_sha256"]
    ):
        raise ContractValidationError(
            "handoff_binding",
            "$bundle.identity",
            "file-backed handoff differs from bundle identity",
        )
    _require_handoff_file_bindings(
        handoff,
        declared=bundle["bindings"]["handoff_artifacts"],
        file_paths=file_paths,
    )
    authority = _authority_object(bundle["authority"])
    _require_authority_file_bindings(
        bundle["authority"],
        declared=bundle["bindings"]["authority_artifacts"],
        file_paths=file_paths,
    )
    settings = validate_evaluation_workflow_settings_v1(
        _read_json(file_paths[bundle["bindings"]["workflow_settings"]["artifact_ref"]]),
        authority=authority,
        scoring_handoff=handoff,
    )
    if settings["settings_sha256"] != bundle["identity"]["settings_sha256"]:
        raise ContractValidationError(
            "settings_binding",
            "$bundle.identity.settings_sha256",
            "file-backed settings differ from bundle identity",
        )
    expected_settings = materialize_registered_evaluation_settings_v1(
        scoring_handoff=handoff,
        settings_authority=authority,
        locked_selection=bundle["locked_selection"],
        settings_option_id=bundle["registered_option"]["settings_option_id"],
        registered_option_sha256=bundle["registered_option"][
            "registered_option_sha256"
        ],
        evaluation_profile_ref=bundle["registered_option"][
            "evaluation_profile_ref"
        ]["artifact_ref"],
        policy_profile_ref=(
            None
            if bundle["registered_option"]["policy_profile_ref"] is None
            else bundle["registered_option"]["policy_profile_ref"]["artifact_ref"]
        ),
        shared_selection_ref=bundle["registered_option"]["shared_selection_ref"][
            "artifact_ref"
        ],
    )
    if settings != expected_settings:
        raise ContractValidationError(
            "settings_materialization",
            bundle["bindings"]["workflow_settings"]["artifact_ref"],
            "file-backed settings are not the deterministic registered settings",
        )
    d2l_common, legacy_d2l_input = _load_bundled_d2l_input(
        handoff=handoff,
        selected_chapter_ids=tuple(bundle["identity"]["selected_chapter_ids"]),
        d2l_input_binding=bundle["bindings"]["d2l_input"],
        file_paths=file_paths,
    )
    manifest = validate_benchmark_manifest_v1(
        _read_json(file_paths[bundle["bindings"]["benchmark_manifest"]["artifact_ref"]])
    )
    preflight = validate_benchmark_preflight_v1(
        _read_json(file_paths[bundle["bindings"]["benchmark_preflight"]["artifact_ref"]])
    )
    overlays = tuple(
        validate_benchmark_overlay_v1(
            _read_json(file_paths[row["artifact"]["artifact_ref"]])
        )
        for row in bundle["bindings"]["overlays"]
    )
    configs = {
        row["chapter_id"]: validate_evaluation_run_config(
            _read_json(file_paths[row["artifact"]["artifact_ref"]])
        )
        for row in bundle["bindings"]["chapter_configs"]
    }
    _validate_runtime_scope(
        handoff=handoff,
        settings=settings,
        d2l_common=d2l_common,
        legacy_d2l_input=legacy_d2l_input,
        manifest=manifest,
        preflight=preflight,
        overlays=overlays,
        configs=configs,
    )
    return LoadedWorkflowScoringRuntimeV1(
        bundle_root=root,
        bundle=bundle,
        scoring_handoff=handoff,
        workflow_settings=settings,
        settings_authority=authority,
        d2l_common_input=d2l_common,
        legacy_d2l_evaluation_input=legacy_d2l_input,
        benchmark_manifest=manifest,
        benchmark_preflight=preflight,
        benchmark_overlays=overlays,
        chapter_configs=configs,
        file_paths=file_paths,
    )


def build_registered_evaluation_workflow_executor_v1(
    bundle_path: Path,
    *,
    evaluation_output_root: Path,
    runtime_registry: EvaluationRuntimeObjectRegistryV1,
) -> tuple[
    RegisteredEvaluationWorkflowExecutorV1,
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
]:
    """Create the concrete executor and return its sealed handoff/settings/read model."""

    loaded = load_workflow_scoring_runtime_bundle_v1(bundle_path)
    return _build_registered_executor_from_loaded(
        loaded,
        evaluation_output_root=evaluation_output_root,
        runtime_registry=runtime_registry,
    )


def _build_registered_executor_from_loaded(
    loaded: LoadedWorkflowScoringRuntimeV1,
    *,
    evaluation_output_root: Path,
    runtime_registry: EvaluationRuntimeObjectRegistryV1,
) -> tuple[
    RegisteredEvaluationWorkflowExecutorV1,
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
]:
    identity = loaded.bundle["identity"]
    option = loaded.bundle["registered_option"]
    registration = EvaluationWorkflowExecutorRegistrationV1(
        workflow_run_id=identity["workflow_run_id"],
        component_run_id=identity["component_run_id"],
        output_root=Path(evaluation_output_root),
        generated_at=loaded.bundle["created_at"],
        producer_code_commit=loaded.bundle["producer"]["code_commit"],
        evaluation_logical_run_id=identity["evaluation_logical_run_id"],
        evaluation_attempt_run_id=identity["evaluation_attempt_run_id"],
        evaluation_profile_id=identity["evaluation_profile_id"],
        evaluation_profile_ref=option["evaluation_profile_ref"]["artifact_ref"],
        policy_profile_id=identity["policy_profile_id"],
        policy_profile_ref=(
            None
            if option["policy_profile_ref"] is None
            else option["policy_profile_ref"]["artifact_ref"]
        ),
        shared_selection_ref=option["shared_selection_ref"]["artifact_ref"],
        settings_option_id=option["settings_option_id"],
        registered_option_sha256=option["registered_option_sha256"],
        locked_selection=loaded.bundle["locked_selection"],
        settings_authority=loaded.settings_authority,
        materialized_workflow_settings=loaded.workflow_settings,
    )
    provider = _FileBackedEvaluationBenchmarkInputProviderV1(
        loaded, runtime_registry
    )
    return (
        RegisteredEvaluationWorkflowExecutorV1(registration, provider),
        copy.deepcopy(loaded.scoring_handoff),
        copy.deepcopy(loaded.workflow_settings),
        loaded.registered_option,
    )


def _validate_identity(value: Any) -> dict[str, Any]:
    row = require_mapping(value, path="$bundle.identity")
    require_exact_keys(
        row,
        required={
            "workflow_run_id",
            "component_run_id",
            "evaluation_logical_run_id",
            "evaluation_attempt_run_id",
            "evaluation_profile_id",
            "policy_profile_id",
            "input_set_sha256",
            "settings_sha256",
            "selected_chapter_ids",
            "selected_arm_ids",
            "selected_scorer_ids",
        },
        path="$bundle.identity",
    )
    return {
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$bundle.identity.workflow_run_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$bundle.identity.component_run_id"
        ),
        "evaluation_logical_run_id": require_string(
            row["evaluation_logical_run_id"],
            path="$bundle.identity.evaluation_logical_run_id",
        ),
        "evaluation_attempt_run_id": require_string(
            row["evaluation_attempt_run_id"],
            path="$bundle.identity.evaluation_attempt_run_id",
        ),
        "evaluation_profile_id": require_string(
            row["evaluation_profile_id"],
            path="$bundle.identity.evaluation_profile_id",
        ),
        "policy_profile_id": require_nullable_string(
            row["policy_profile_id"], path="$bundle.identity.policy_profile_id"
        ),
        "input_set_sha256": require_sha256(
            row["input_set_sha256"], path="$bundle.identity.input_set_sha256"
        ),
        "settings_sha256": require_sha256(
            row["settings_sha256"], path="$bundle.identity.settings_sha256"
        ),
        "selected_chapter_ids": _ordered_strings(
            row["selected_chapter_ids"],
            path="$bundle.identity.selected_chapter_ids",
        ),
        "selected_arm_ids": _ordered_strings(
            row["selected_arm_ids"], path="$bundle.identity.selected_arm_ids"
        ),
        "selected_scorer_ids": _ordered_strings(
            row["selected_scorer_ids"],
            path="$bundle.identity.selected_scorer_ids",
        ),
    }


def _authority_payload(
    value: EvaluationWorkflowSettingsAuthorityV1,
) -> dict[str, Any]:
    return _validate_authority(
        {
            "benchmark_preset": copy.deepcopy(value.benchmark_preset),
            "evaluation_config": copy.deepcopy(value.evaluation_config),
            "scorer_set": copy.deepcopy(value.scorer_set),
            "evaluation_profiles": copy.deepcopy(list(value.evaluation_profiles)),
            "policy_profiles": copy.deepcopy(list(value.policy_profiles)),
            "shared_selections": copy.deepcopy(list(value.shared_selections)),
            "chapter_ids": copy.deepcopy(list(value.chapter_ids)),
            "arm_ids": copy.deepcopy(list(value.arm_ids)),
            "scorer_ids": copy.deepcopy(list(value.scorer_ids)),
            "aggregation_policy_id": value.aggregation_policy_id,
            "report_policy_id": value.report_policy_id,
            "verdict_policy_id": value.verdict_policy_id,
        }
    )


def _validate_authority(value: Any) -> dict[str, Any]:
    row = require_mapping(value, path="$bundle.authority")
    require_exact_keys(
        row,
        required={
            "benchmark_preset",
            "evaluation_config",
            "scorer_set",
            "evaluation_profiles",
            "policy_profiles",
            "shared_selections",
            "chapter_ids",
            "arm_ids",
            "scorer_ids",
            "aggregation_policy_id",
            "report_policy_id",
            "verdict_policy_id",
        },
        path="$bundle.authority",
    )
    return {
        "benchmark_preset": _physical_authority_binding(
            row["benchmark_preset"], path="$bundle.authority.benchmark_preset"
        ),
        "evaluation_config": _physical_authority_binding(
            row["evaluation_config"], path="$bundle.authority.evaluation_config"
        ),
        "scorer_set": _physical_authority_binding(
            row["scorer_set"], path="$bundle.authority.scorer_set"
        ),
        "evaluation_profiles": _authority_catalog(
            row["evaluation_profiles"],
            path="$bundle.authority.evaluation_profiles",
        ),
        "policy_profiles": _authority_catalog(
            row["policy_profiles"], path="$bundle.authority.policy_profiles"
        ),
        "shared_selections": _authority_catalog(
            row["shared_selections"], path="$bundle.authority.shared_selections"
        ),
        "chapter_ids": _ordered_strings(
            row["chapter_ids"], path="$bundle.authority.chapter_ids"
        ),
        "arm_ids": _ordered_strings(
            row["arm_ids"], path="$bundle.authority.arm_ids"
        ),
        "scorer_ids": _ordered_strings(
            row["scorer_ids"], path="$bundle.authority.scorer_ids"
        ),
        "aggregation_policy_id": require_string(
            row["aggregation_policy_id"],
            path="$bundle.authority.aggregation_policy_id",
        ),
        "report_policy_id": require_string(
            row["report_policy_id"], path="$bundle.authority.report_policy_id"
        ),
        "verdict_policy_id": require_string(
            row["verdict_policy_id"], path="$bundle.authority.verdict_policy_id"
        ),
    }


def _authority_object(
    value: Mapping[str, Any],
) -> EvaluationWorkflowSettingsAuthorityV1:
    authority = _validate_authority(value)
    return EvaluationWorkflowSettingsAuthorityV1(
        benchmark_preset=authority["benchmark_preset"],
        evaluation_config=authority["evaluation_config"],
        scorer_set=authority["scorer_set"],
        evaluation_profiles=authority["evaluation_profiles"],
        policy_profiles=authority["policy_profiles"],
        shared_selections=authority["shared_selections"],
        chapter_ids=authority["chapter_ids"],
        arm_ids=authority["arm_ids"],
        scorer_ids=authority["scorer_ids"],
        aggregation_policy_id=authority["aggregation_policy_id"],
        report_policy_id=authority["report_policy_id"],
        verdict_policy_id=authority["verdict_policy_id"],
    )


def _find_authority_binding(
    values: Sequence[Mapping[str, Any]],
    artifact_ref: str,
    *,
    path: str,
) -> dict[str, str]:
    ref = require_relative_path(artifact_ref, path=path)
    for value in values:
        binding = _physical_authority_binding(value, path=path)
        if binding["artifact_ref"] == ref:
            return binding
    raise ContractValidationError(
        "authority_binding", path, "artifact reference is not registered"
    )


def _validate_baseline_registered_option(
    value: Any,
    *,
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    path = "$template.registered_option"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "settings_option_id",
            "registered_option_sha256",
            "evaluation_profile_id",
            "evaluation_profile_ref",
            "policy_profile_id",
            "policy_profile_ref",
            "shared_selection_ref",
        },
        path=path,
    )
    normalized = {
        "settings_option_id": require_string(
            row["settings_option_id"], path=f"{path}.settings_option_id"
        ),
        "registered_option_sha256": require_sha256(
            row["registered_option_sha256"],
            path=f"{path}.registered_option_sha256",
        ),
        "evaluation_profile_id": require_string(
            row["evaluation_profile_id"],
            path=f"{path}.evaluation_profile_id",
        ),
        "evaluation_profile_ref": _physical_authority_binding(
            row["evaluation_profile_ref"],
            path=f"{path}.evaluation_profile_ref",
        ),
        "policy_profile_id": require_nullable_string(
            row["policy_profile_id"], path=f"{path}.policy_profile_id"
        ),
        "policy_profile_ref": (
            None
            if row["policy_profile_ref"] is None
            else _physical_authority_binding(
                row["policy_profile_ref"], path=f"{path}.policy_profile_ref"
            )
        ),
        "shared_selection_ref": _physical_authority_binding(
            row["shared_selection_ref"], path=f"{path}.shared_selection_ref"
        ),
    }
    if (normalized["policy_profile_id"] is None) != (
        normalized["policy_profile_ref"] is None
    ):
        raise ContractValidationError(
            "policy_profile_binding",
            path,
            "policy profile id and artifact reference must both be set or null",
        )
    _require_catalog_member(
        normalized["evaluation_profile_ref"],
        authority["evaluation_profiles"],
        path=f"{path}.evaluation_profile_ref",
    )
    if normalized["policy_profile_ref"] is not None:
        _require_catalog_member(
            normalized["policy_profile_ref"],
            authority["policy_profiles"],
            path=f"{path}.policy_profile_ref",
        )
    _require_catalog_member(
        normalized["shared_selection_ref"],
        authority["shared_selections"],
        path=f"{path}.shared_selection_ref",
    )
    return normalized


def _validate_external_translation_inputs(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$template.external_translation_inputs")
    expected_arms = ("community", "google_nmt", "llm_lc")
    if len(rows) != len(expected_arms):
        raise ContractValidationError(
            "external_arm_exact_cover",
            "$template.external_translation_inputs",
            "template requires exactly community, google_nmt, and llm_lc",
        )
    normalized: list[dict[str, Any]] = []
    for index, expected_arm in enumerate(expected_arms):
        path = f"$template.external_translation_inputs[{index}]"
        row = require_mapping(rows[index], path=path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "translation_artifact",
                "producer",
                "coverage",
                "source_binding",
            },
            path=path,
        )
        arm_id = require_enum(row["arm_id"], {expected_arm}, path=f"{path}.arm_id")
        producer = require_mapping(row["producer"], path=f"{path}.producer")
        require_exact_keys(
            producer,
            required={"component_id", "component_run_id"},
            path=f"{path}.producer",
        )
        component_id = require_string(
            producer["component_id"], path=f"{path}.producer.component_id"
        )
        if component_id in {"evaluation", "neutral_relay", "translation"}:
            raise ContractValidationError(
                "producer_authority",
                f"{path}.producer.component_id",
                "external baseline must be authored outside Translation, Evaluation, and relay",
            )
        coverage_row = require_mapping(row["coverage"], path=f"{path}.coverage")
        count_fields = (
            "translated_block_count",
            "preserved_block_count",
            "excluded_block_count",
            "review_held_block_count",
            "missing_block_count",
            "failed_block_count",
        )
        require_exact_keys(
            coverage_row,
            required={"expected_block_count", "block_universe_sha256", *count_fields},
            path=f"{path}.coverage",
        )
        expected_count = require_int(
            coverage_row["expected_block_count"],
            path=f"{path}.coverage.expected_block_count",
            minimum=1,
        )
        counts = {
            field: require_int(
                coverage_row[field],
                path=f"{path}.coverage.{field}",
                minimum=0,
            )
            for field in count_fields
        }
        if sum(counts.values()) != expected_count:
            raise ContractValidationError(
                "coverage_accounting",
                f"{path}.coverage",
                "coverage statuses must exact-cover admitted blocks",
            )
        normalized.append(
            {
                "arm_id": arm_id,
                "translation_artifact": validate_typed_artifact_binding_v1(
                    row["translation_artifact"],
                    path=f"{path}.translation_artifact",
                ),
                "producer": {
                    "component_id": component_id,
                    "component_run_id": require_string(
                        producer["component_run_id"],
                        path=f"{path}.producer.component_run_id",
                    ),
                },
                "coverage": {
                    "expected_block_count": expected_count,
                    "block_universe_sha256": require_sha256(
                        coverage_row["block_universe_sha256"],
                        path=f"{path}.coverage.block_universe_sha256",
                    ),
                    **counts,
                },
                "source_binding": validate_typed_artifact_binding_v1(
                    row["source_binding"], path=f"{path}.source_binding"
                ),
            }
        )
    require_unique(
        [row["translation_artifact"]["artifact_ref"] for row in normalized],
        path="$template.external_translation_inputs",
    )
    first = normalized[0]
    for index, row in enumerate(normalized[1:], start=1):
        if (
            row["source_binding"] != first["source_binding"]
            or row["coverage"]["block_universe_sha256"]
            != first["coverage"]["block_universe_sha256"]
            or row["coverage"]["expected_block_count"]
            != first["coverage"]["expected_block_count"]
        ):
            raise ContractValidationError(
                "baseline_universe",
                f"$template.external_translation_inputs[{index}]",
                "external baselines must share one source and admitted universe",
            )
    return normalized


def _validate_registered_option(
    value: Any,
    *,
    authority: Mapping[str, Any],
    locked_selection: Mapping[str, Any],
) -> dict[str, Any]:
    path = "$bundle.registered_option"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "settings_option_id",
            "registered_option_sha256",
            "benchmark_preset_ref",
            "evaluation_config_ref",
            "scorer_set_ref",
            "evaluation_profile_ref",
            "policy_profile_ref",
            "shared_selection_ref",
            "selection_sha256",
            "workflow_settings_sha256",
        },
        path=path,
    )
    normalized = {
        "settings_option_id": require_string(
            row["settings_option_id"], path=f"{path}.settings_option_id"
        ),
        "registered_option_sha256": require_sha256(
            row["registered_option_sha256"],
            path=f"{path}.registered_option_sha256",
        ),
        "benchmark_preset_ref": _physical_authority_binding(
            row["benchmark_preset_ref"], path=f"{path}.benchmark_preset_ref"
        ),
        "evaluation_config_ref": _physical_authority_binding(
            row["evaluation_config_ref"], path=f"{path}.evaluation_config_ref"
        ),
        "scorer_set_ref": _physical_authority_binding(
            row["scorer_set_ref"], path=f"{path}.scorer_set_ref"
        ),
        "evaluation_profile_ref": _physical_authority_binding(
            row["evaluation_profile_ref"], path=f"{path}.evaluation_profile_ref"
        ),
        "policy_profile_ref": (
            None
            if row["policy_profile_ref"] is None
            else _physical_authority_binding(
                row["policy_profile_ref"], path=f"{path}.policy_profile_ref"
            )
        ),
        "shared_selection_ref": _physical_authority_binding(
            row["shared_selection_ref"], path=f"{path}.shared_selection_ref"
        ),
        "selection_sha256": require_sha256(
            row["selection_sha256"], path=f"{path}.selection_sha256"
        ),
        "workflow_settings_sha256": require_sha256(
            row["workflow_settings_sha256"],
            path=f"{path}.workflow_settings_sha256",
        ),
    }
    if normalized["selection_sha256"] != locked_selection["selection_sha256"]:
        raise ContractValidationError(
            "selection_binding",
            f"{path}.selection_sha256",
            "registered option names another locked selection",
        )
    expected = {
        "benchmark_preset_ref": authority["benchmark_preset"],
        "evaluation_config_ref": authority["evaluation_config"],
        "scorer_set_ref": authority["scorer_set"],
    }
    for field, binding in expected.items():
        if normalized[field] != binding:
            raise ContractValidationError(
                "authority_binding", f"{path}.{field}", "authority binding drift"
            )
    _require_catalog_member(
        normalized["evaluation_profile_ref"],
        authority["evaluation_profiles"],
        path=f"{path}.evaluation_profile_ref",
    )
    if normalized["policy_profile_ref"] is not None:
        _require_catalog_member(
            normalized["policy_profile_ref"],
            authority["policy_profiles"],
            path=f"{path}.policy_profile_ref",
        )
    _require_catalog_member(
        normalized["shared_selection_ref"],
        authority["shared_selections"],
        path=f"{path}.shared_selection_ref",
    )
    return normalized


def _validate_bindings(
    value: Any,
    *,
    selected_chapter_ids: Sequence[str],
    selected_arm_ids: Sequence[str],
) -> dict[str, Any]:
    path = "$bundle.bindings"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "scoring_handoff",
            "workflow_settings",
            "d2l_input",
            "benchmark_manifest",
            "benchmark_preflight",
            "handoff_artifacts",
            "authority_artifacts",
            "overlays",
            "chapter_configs",
            "file_bindings",
        },
        path=path,
    )
    singles = {
        field: _physical_binding_value(row[field], path=f"{path}.{field}")
        for field in (
            "scoring_handoff",
            "workflow_settings",
            "benchmark_manifest",
            "benchmark_preflight",
        )
    }
    d2l_input = _validate_d2l_input_binding(
        row["d2l_input"], path=f"{path}.d2l_input"
    )
    if singles["scoring_handoff"]["artifact_ref"] != _HANDOFF_REF:
        raise ContractValidationError(
            "handoff_binding",
            f"{path}.scoring_handoff.artifact_ref",
            "runtime handoff must use the parent-owned fixed reference",
        )
    handoff_artifacts = _physical_binding_list(
        row["handoff_artifacts"], path=f"{path}.handoff_artifacts"
    )
    authority_artifacts = _physical_binding_list(
        row["authority_artifacts"], path=f"{path}.authority_artifacts"
    )
    overlay_rows = []
    for index, item in enumerate(
        require_list(row["overlays"], path=f"{path}.overlays")
    ):
        item_path = f"{path}.overlays[{index}]"
        item_row = require_mapping(item, path=item_path)
        require_exact_keys(
            item_row,
            required={"chapter_id", "arm_id", "artifact"},
            path=item_path,
        )
        overlay_rows.append(
            {
                "chapter_id": require_string(
                    item_row["chapter_id"], path=f"{item_path}.chapter_id"
                ),
                "arm_id": require_string(
                    item_row["arm_id"], path=f"{item_path}.arm_id"
                ),
                "artifact": _physical_binding_value(
                    item_row["artifact"], path=f"{item_path}.artifact"
                ),
            }
        )
    expected_overlay_keys = [
        (chapter_id, _benchmark_arm_id(arm_id))
        for chapter_id in selected_chapter_ids
        for arm_id in selected_arm_ids
    ]
    if [
        (item["chapter_id"], item["arm_id"]) for item in overlay_rows
    ] != expected_overlay_keys:
        raise ContractValidationError(
            "overlay_exact_cover",
            f"{path}.overlays",
            "runtime overlays must exact-cover the selected chapter/arm matrix",
        )
    config_rows = []
    for index, item in enumerate(
        require_list(row["chapter_configs"], path=f"{path}.chapter_configs")
    ):
        item_path = f"{path}.chapter_configs[{index}]"
        item_row = require_mapping(item, path=item_path)
        require_exact_keys(
            item_row, required={"chapter_id", "artifact"}, path=item_path
        )
        config_rows.append(
            {
                "chapter_id": require_string(
                    item_row["chapter_id"], path=f"{item_path}.chapter_id"
                ),
                "artifact": _physical_binding_value(
                    item_row["artifact"], path=f"{item_path}.artifact"
                ),
            }
        )
    if [item["chapter_id"] for item in config_rows] != list(selected_chapter_ids):
        raise ContractValidationError(
            "config_exact_cover",
            f"{path}.chapter_configs",
            "runtime configs must exact-cover selected chapters in order",
        )
    file_bindings = _physical_binding_list(
        row["file_bindings"], path=f"{path}.file_bindings"
    )
    refs = [item["artifact_ref"] for item in file_bindings]
    if refs != sorted(refs):
        raise ContractValidationError(
            "file_order",
            f"{path}.file_bindings",
            "runtime file bindings must be ordered by artifact_ref",
        )
    required_refs = {
        item["artifact_ref"]
        for item in [
            *singles.values(),
            *(
                []
                if d2l_input["legacy_artifact"] is None
                else [d2l_input["legacy_artifact"]]
            ),
            *handoff_artifacts,
            *authority_artifacts,
            *(item["artifact"] for item in overlay_rows),
            *(item["artifact"] for item in config_rows),
        ]
    }
    if set(refs) != required_refs:
        raise ContractValidationError(
            "file_exact_cover",
            f"{path}.file_bindings",
            "file table must exact-cover every runtime artifact reference",
        )
    return {
        **singles,
        "d2l_input": d2l_input,
        "handoff_artifacts": handoff_artifacts,
        "authority_artifacts": authority_artifacts,
        "overlays": overlay_rows,
        "chapter_configs": config_rows,
        "file_bindings": file_bindings,
    }


def _validate_arm_presentations(
    value: Any, *, selected_arm_ids: Sequence[str]
) -> list[dict[str, str]]:
    rows = []
    for index, item in enumerate(
        require_list(list(value), path="$.presentations.arms")
    ):
        path = f"$.presentations.arms[{index}]"
        row = require_mapping(item, path=path)
        require_exact_keys(
            row, required={"arm_id", "role", "kind", "label"}, path=path
        )
        rows.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"),
                "role": require_enum(
                    row["role"],
                    {"baseline", "candidate", "reference", "external_baseline"},
                    path=f"{path}.role",
                ),
                "kind": require_enum(
                    row["kind"],
                    {"system", "human_reference", "machine_baseline"},
                    path=f"{path}.kind",
                ),
                "label": require_string(row["label"], path=f"{path}.label"),
            }
        )
    if [row["arm_id"] for row in rows] != list(selected_arm_ids):
        raise ContractValidationError(
            "arm_exact_cover",
            "$.presentations.arms",
            "arm presentations must follow the selected settings arm order",
        )
    return rows


def _validate_method_presentations(
    value: Any, *, selected_scorer_ids: Sequence[str]
) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(
        require_list(list(value), path="$.presentations.methods")
    ):
        path = f"$.presentations.methods[{index}]"
        row = require_mapping(item, path=path)
        require_exact_keys(row, required={"display_name", "method"}, path=path)
        rows.append(
            {
                "display_name": require_string(
                    row["display_name"], path=f"{path}.display_name"
                ),
                "method": validate_method(row["method"], path=f"{path}.method"),
            }
        )
    if [row["method"]["method_id"] for row in rows] != list(selected_scorer_ids):
        raise ContractValidationError(
            "method_exact_cover",
            "$.presentations.methods",
            "method presentations must follow selected scorer order",
        )
    return rows


def _validate_chapter_runtime_bindings(
    value: Any,
    *,
    selected_chapter_ids: Sequence[str],
    selected_scorer_ids: Sequence[str],
) -> list[dict[str, Any]]:
    rows = []
    needs_local = "sf_qe" in selected_scorer_ids
    needs_llm = any(item in {"sf_bt", "pj"} for item in selected_scorer_ids)
    for index, item in enumerate(
        require_list(list(value), path="$.chapter_runtime_bindings")
    ):
        path = f"$.chapter_runtime_bindings[{index}]"
        row = require_mapping(item, path=path)
        require_exact_keys(
            row,
            required={
                "chapter_id",
                "local_sf_qe_runtime_id",
                "llm_roles_runtime_id",
                "shared_ledger_runtime_id",
                "shared_ledger_relative_path",
            },
            path=path,
        )
        normalized = {
            "chapter_id": require_string(
                row["chapter_id"], path=f"{path}.chapter_id"
            ),
            "local_sf_qe_runtime_id": require_nullable_string(
                row["local_sf_qe_runtime_id"],
                path=f"{path}.local_sf_qe_runtime_id",
            ),
            "llm_roles_runtime_id": require_nullable_string(
                row["llm_roles_runtime_id"],
                path=f"{path}.llm_roles_runtime_id",
            ),
            "shared_ledger_runtime_id": require_nullable_string(
                row["shared_ledger_runtime_id"],
                path=f"{path}.shared_ledger_runtime_id",
            ),
            "shared_ledger_relative_path": (
                None
                if row["shared_ledger_relative_path"] is None
                else require_relative_path(
                    row["shared_ledger_relative_path"],
                    path=f"{path}.shared_ledger_relative_path",
                )
            ),
        }
        if needs_local and normalized["local_sf_qe_runtime_id"] is None:
            raise ContractValidationError(
                "runtime_binding",
                f"{path}.local_sf_qe_runtime_id",
                "selected SF-QE requires a registered local runtime",
            )
        llm_values = (
            normalized["llm_roles_runtime_id"],
            normalized["shared_ledger_runtime_id"],
            normalized["shared_ledger_relative_path"],
        )
        if needs_llm and any(item is None for item in llm_values):
            raise ContractValidationError(
                "runtime_binding",
                path,
                "selected SF-BT/PJ requires role runner, ledger, and ledger path",
            )
        if not needs_llm and any(item is not None for item in llm_values):
            raise ContractValidationError(
                "runtime_binding",
                path,
                "unused LLM runtime authority must not be attached",
            )
        rows.append(normalized)
    if [row["chapter_id"] for row in rows] != list(selected_chapter_ids):
        raise ContractValidationError(
            "runtime_exact_cover",
            "$.chapter_runtime_bindings",
            "runtime bindings must exact-cover selected chapters in order",
        )
    return rows


def _load_d2l_input_sources(
    *,
    handoff: Mapping[str, Any],
    selected_chapter_ids: Sequence[str],
    handoff_artifacts: Mapping[str, Path],
    legacy_d2l_evaluation_input: Path | None,
) -> tuple[
    CommonEvaluationInputV1,
    Mapping[str, Any] | None,
    dict[str, Any],
]:
    if legacy_d2l_evaluation_input is not None:
        legacy = validate_d2l_evaluation_input(
            _read_json(legacy_d2l_evaluation_input)
        )
        return (
            project_d2l_evaluation_package(legacy),
            legacy,
            {
                "mode": "legacy_d2l_evaluation_input_v1",
                "legacy_artifact": _physical_binding(
                    _LEGACY_D2L_INPUT_REF,
                    "d2l_evaluation_input_v1",
                    legacy_d2l_evaluation_input,
                    schema_version=legacy["schema_version"],
                ),
            },
        )
    common = _build_canonical_common_from_handoff(
        handoff=handoff,
        selected_chapter_ids=selected_chapter_ids,
        file_paths=handoff_artifacts,
    )
    return (
        common,
        None,
        {
            "mode": "canonical_source_package_v1",
            "legacy_artifact": None,
        },
    )


def _load_bundled_d2l_input(
    *,
    handoff: Mapping[str, Any],
    selected_chapter_ids: Sequence[str],
    d2l_input_binding: Mapping[str, Any],
    file_paths: Mapping[str, Path],
) -> tuple[CommonEvaluationInputV1, Mapping[str, Any] | None]:
    binding = _validate_d2l_input_binding(
        d2l_input_binding, path="$bundle.bindings.d2l_input"
    )
    if binding["mode"] == "canonical_source_package_v1":
        return (
            _build_canonical_common_from_handoff(
                handoff=handoff,
                selected_chapter_ids=selected_chapter_ids,
                file_paths=file_paths,
            ),
            None,
        )
    legacy_artifact = binding["legacy_artifact"]
    assert legacy_artifact is not None
    legacy = validate_d2l_evaluation_input(
        _read_json(file_paths[legacy_artifact["artifact_ref"]])
    )
    return project_d2l_evaluation_package(legacy), legacy


def _build_canonical_common_from_handoff(
    *,
    handoff: Mapping[str, Any],
    selected_chapter_ids: Sequence[str],
    file_paths: Mapping[str, Path],
) -> CommonEvaluationInputV1:
    source_by_role = {
        row["role"]: file_paths[row["binding"]["artifact_ref"]]
        for row in handoff["source_package_bindings"]
    }
    if tuple(source_by_role) != SOURCE_BINDING_ROLES_V1:
        raise ContractValidationError(
            "source_binding_exact_cover",
            "$handoff.source_package_bindings",
            "canonical source package role order drift",
        )
    translation_by_arm = {
        row["arm_id"]: file_paths[row["translation_artifact"]["artifact_ref"]]
        for row in handoff["translation_inputs"]
        if row["arm_id"] in {"s0", "s1"}
    }
    if tuple(translation_by_arm) != ("s0", "s1"):
        raise ContractValidationError(
            "d2l_arm_scope",
            "$handoff.translation_inputs",
            "canonical D2L bridge requires exact ordered s0 and s1 inputs",
        )
    return build_canonical_d2l_common_input_v1(
        source_artifacts=FinalizedCanonicalSourceArtifactsV1(
            document=source_by_role["document"],
            structure_manifest=source_by_role["structure_manifest"],
            asset_manifest=source_by_role["asset_manifest"],
            admitted_projection=source_by_role["admitted_projection"],
            package_seal=source_by_role["package_seal"],
        ),
        s0_translation_artifact=translation_by_arm["s0"],
        s1_translation_artifact=translation_by_arm["s1"],
        selected_chapter_ids=selected_chapter_ids,
    )


def _validate_d2l_input_binding(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"mode", "legacy_artifact"}, path=path)
    mode = require_enum(
        row["mode"],
        {"canonical_source_package_v1", "legacy_d2l_evaluation_input_v1"},
        path=f"{path}.mode",
    )
    if mode == "canonical_source_package_v1":
        if row["legacy_artifact"] is not None:
            raise ContractValidationError(
                "mixed_source_binding",
                f"{path}.legacy_artifact",
                "canonical mode cannot carry a legacy D2L input",
            )
        legacy_artifact = None
    else:
        if row["legacy_artifact"] is None:
            raise ContractValidationError(
                "missing_artifact",
                f"{path}.legacy_artifact",
                "legacy mode requires an explicit D2L input artifact",
            )
        legacy_artifact = _physical_binding_value(
            row["legacy_artifact"], path=f"{path}.legacy_artifact"
        )
        if (
            legacy_artifact["artifact_ref"] != _LEGACY_D2L_INPUT_REF
            or legacy_artifact["artifact_kind"] != "d2l_evaluation_input_v1"
        ):
            raise ContractValidationError(
                "legacy_input_binding",
                f"{path}.legacy_artifact",
                "legacy D2L input must use the fixed runtime artifact identity",
            )
    return {"mode": mode, "legacy_artifact": legacy_artifact}


def _validate_runtime_scope(
    *,
    handoff: Mapping[str, Any],
    settings: Mapping[str, Any],
    d2l_common: CommonEvaluationInputV1,
    legacy_d2l_input: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
    preflight: Mapping[str, Any],
    overlays: Sequence[Mapping[str, Any]],
    configs: Mapping[str, Mapping[str, Any]],
) -> None:
    chapters = list(settings["selected_chapter_ids"])
    benchmark_arms = [_benchmark_arm_id(item) for item in settings["selected_arm_ids"]]
    common_chapters = list(dict.fromkeys(row.chapter_id for row in d2l_common.blocks))
    if common_chapters != chapters:
        raise ContractValidationError(
            "chapter_scope",
            "$.d2l_input.blocks",
            "D2L input and Evaluation settings select different chapters",
        )
    d2l_arms = {_benchmark_arm_id(row.arm_id) for row in d2l_common.arms}
    expected_d2l_arms = {item for item in benchmark_arms if item in {"S0", "S1"}}
    if d2l_arms != {"S0", "S1"} or not expected_d2l_arms.issubset(d2l_arms):
        raise ContractValidationError(
            "d2l_arm_scope",
            "$.d2l_input.arms",
            "D2L input must contain S0/S1 and cover every selected D2L arm",
        )
    if legacy_d2l_input is not None:
        handoff_inputs = {
            row["arm_id"]: row for row in handoff["translation_inputs"]
        }
        package_arms = {
            _settings_arm_id(row["arm_id"]): row
            for row in legacy_d2l_input["arms"]
        }
        for arm_id in ("s0", "s1"):
            if arm_id not in settings["selected_arm_ids"]:
                continue
            if (
                package_arms[arm_id]["translation_sha256"]
                != handoff_inputs[arm_id]["translation_artifact"]["sha256"]
            ):
                raise ContractValidationError(
                    "translation_binding",
                    f"$.d2l_input.arms[{arm_id}]",
                    "legacy D2L input and scoring handoff bind different translation bytes",
                )
    source_snapshots = tuple(
        CommonSourceSnapshotV1(
            source_schema_id=d2l_common.source_schema_id,
            source_schema_version=d2l_common.source_schema_version,
            source_binding=d2l_common.source_binding,
            blocks=tuple(
                row for row in d2l_common.blocks if row.chapter_id == chapter_id
            ),
        )
        for chapter_id in chapters
    )
    validate_benchmark_source_read_models_v1(manifest, source_snapshots)
    if [row["chapter_id"] for row in manifest["chapters"]] != chapters:
        raise ContractValidationError(
            "manifest_scope",
            "$.benchmark_manifest.chapters",
            "benchmark manifest and settings select different chapters",
        )
    if [row["arm_id"] for row in manifest["arm_contracts"]] != benchmark_arms:
        raise ContractValidationError(
            "manifest_scope",
            "$.benchmark_manifest.arm_contracts",
            "benchmark manifest and settings select different arms",
        )
    if preflight["benchmark_manifest_sha256"] != manifest["integrity"]["manifest_sha256"]:
        raise ContractValidationError(
            "preflight_binding",
            "$.benchmark_preflight",
            "preflight belongs to another manifest",
        )
    if preflight["status"] != "ready":
        raise ContractValidationError(
            "preflight_status",
            "$.benchmark_preflight.status",
            "production Evaluation bundle requires a ready preflight",
        )
    expected_overlay_keys = [
        (chapter_id, arm_id)
        for chapter_id in chapters
        for arm_id in benchmark_arms
    ]
    observed_overlay_keys = [
        (row["source"]["chapter_id"], row["arm"]["arm_id"]) for row in overlays
    ]
    if observed_overlay_keys != expected_overlay_keys:
        raise ContractValidationError(
            "overlay_exact_cover",
            "$.benchmark_overlays",
            "overlays must exact-cover selected chapters and arms in order",
        )
    if list(configs) != chapters:
        raise ContractValidationError(
            "config_exact_cover",
            "$.chapter_configs",
            "configs must exact-cover selected chapters in order",
        )
    selected_scorers = list(settings["selected_scorer_ids"])
    for chapter_id, config in configs.items():
        configured_scorers = [row["method_id"] for row in config["methods"]]
        if set(configured_scorers) != set(selected_scorers):
            raise ContractValidationError(
                "scorer_scope",
                f"$.chapter_configs[{chapter_id}].methods",
                "chapter config must exact-cover the scorers selected by settings",
            )


def _load_overlay_sources(
    values: Mapping[tuple[str, str], Path],
    *,
    selected_chapters: Sequence[str],
    selected_arms: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    expected = [
        (chapter_id, arm_id)
        for chapter_id in selected_chapters
        for arm_id in selected_arms
    ]
    if list(values) != expected:
        raise ContractValidationError(
            "overlay_source_exact_cover",
            "$.artifact_sources.overlays",
            "explicit overlay source map must follow the selected matrix",
        )
    rows = tuple(
        validate_benchmark_overlay_v1(_read_json(values[key])) for key in expected
    )
    if [
        (row["source"]["chapter_id"], row["arm"]["arm_id"]) for row in rows
    ] != expected:
        raise ContractValidationError(
            "overlay_source_binding",
            "$.artifact_sources.overlays",
            "overlay file identities differ from explicit source-map keys",
        )
    return rows


def _load_config_sources(
    values: Mapping[str, Path], *, selected_chapters: Sequence[str]
) -> dict[str, Mapping[str, Any]]:
    if list(values) != list(selected_chapters):
        raise ContractValidationError(
            "config_source_exact_cover",
            "$.artifact_sources.chapter_configs",
            "explicit config source map must follow selected chapter order",
        )
    return {
        chapter_id: validate_evaluation_run_config(_read_json(values[chapter_id]))
        for chapter_id in selected_chapters
    }


def _handoff_file_bindings(
    handoff: Mapping[str, Any],
) -> list[dict[str, str]]:
    rows = [
        item["binding"] for item in handoff["source_package_bindings"]
    ]
    rows.extend(
        item
        for item in handoff["optional_bindings"].values()
        if item is not None
    )
    rows.extend(
        item["translation_artifact"] for item in handoff["translation_inputs"]
    )
    normalized = [
        _physical_binding_value(item, path="$.scoring_handoff.artifacts")
        for item in rows
    ]
    require_unique(
        [item["artifact_ref"] for item in normalized],
        path="$.scoring_handoff.artifacts",
    )
    return normalized


def _authority_file_bindings(
    authority: Mapping[str, Any],
) -> list[dict[str, str]]:
    rows = [
        authority["benchmark_preset"],
        authority["evaluation_config"],
        authority["scorer_set"],
        *authority["evaluation_profiles"],
        *authority["policy_profiles"],
        *authority["shared_selections"],
    ]
    require_unique(
        [row["artifact_ref"] for row in rows], path="$.authority.artifacts"
    )
    return copy.deepcopy(rows)


def _require_handoff_file_bindings(
    handoff: Mapping[str, Any],
    *,
    declared: Sequence[Mapping[str, Any]],
    file_paths: Mapping[str, Path],
) -> None:
    expected = _handoff_file_bindings(handoff)
    if list(declared) != expected:
        raise ContractValidationError(
            "handoff_artifact_binding",
            "$bundle.bindings.handoff_artifacts",
            "bundle must echo every handoff file binding exactly",
        )
    for binding in expected:
        if _physical_sha256(file_paths[binding["artifact_ref"]]) != binding["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                binding["artifact_ref"],
                "handoff artifact bytes drifted",
            )


def _require_authority_file_bindings(
    authority: Mapping[str, Any],
    *,
    declared: Sequence[Mapping[str, Any]],
    file_paths: Mapping[str, Path],
) -> None:
    expected = _authority_file_bindings(authority)
    if list(declared) != expected:
        raise ContractValidationError(
            "authority_artifact_binding",
            "$bundle.bindings.authority_artifacts",
            "bundle must echo every authority file binding exactly",
        )
    for binding in expected:
        if _physical_sha256(file_paths[binding["artifact_ref"]]) != binding["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                binding["artifact_ref"],
                "authority artifact bytes drifted",
            )


def _validate_bundle_files(
    root: Path, bindings: Sequence[Mapping[str, Any]]
) -> dict[str, Path]:
    result = {}
    for binding in bindings:
        normalized = _physical_binding_value(binding, path="$.file_bindings")
        path = _contained_path(root, normalized["artifact_ref"])
        if not path.is_file():
            raise ContractValidationError(
                "missing_artifact", str(path), "runtime bundle file is absent"
            )
        if _physical_sha256(path) != normalized["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                normalized["artifact_ref"],
                "runtime bundle file hash drift",
            )
        result[normalized["artifact_ref"]] = path
    return result


def _normalize_d2l_arm_ids(
    common: CommonEvaluationInputV1,
) -> CommonEvaluationInputV1:
    arm_map = {arm.arm_id: _benchmark_arm_id(arm.arm_id) for arm in common.arms}
    if len(set(arm_map.values())) != len(arm_map):
        raise ContractValidationError(
            "d2l_arm_scope", "$.d2l_evaluation_input.arms", "duplicate S0/S1 identity"
        )
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
        arms=tuple(
            CommonArmV1(
                artifact_id=arm.artifact_id,
                artifact_sha256=arm.artifact_sha256,
                logical_run_id=arm.logical_run_id,
                attempt_run_id=arm.attempt_run_id,
                arm_id=arm_map[arm.arm_id],
                profile_id=arm.profile_id,
                profile_config_sha256=arm.profile_config_sha256,
                source_language=arm.source_language,
                target_language=arm.target_language,
            )
            for arm in common.arms
        ),
        translations=tuple(
            CommonTranslationV1(
                arm_id=arm_map[row.arm_id],
                block_id=row.block_id,
                status=row.status,
                target_text=row.target_text,
                error_code=row.error_code,
            )
            for row in common.translations
        ),
    )


def _select_common_arms(
    common: CommonEvaluationInputV1, selected_arm_ids: Sequence[str]
) -> CommonEvaluationInputV1:
    selected = set(selected_arm_ids)
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
        arms=tuple(arm for arm in common.arms if arm.arm_id in selected),
        translations=tuple(
            row for row in common.translations if row.arm_id in selected
        ),
    )


def _physical_authority_binding(value: Any, *, path: str) -> dict[str, str]:
    binding = validate_typed_artifact_binding_v1(value, path=path)
    if binding["sha256_kind"] != "physical":
        raise ContractValidationError(
            "runtime_file_authority",
            f"{path}.sha256_kind",
            "file-backed authority requires a physical hash",
        )
    return binding


def _authority_catalog(value: Any, *, path: str) -> list[dict[str, str]]:
    rows = [
        _physical_authority_binding(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]
    require_unique([item["artifact_ref"] for item in rows], path=path)
    return rows


def _require_catalog_member(
    binding: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
    *,
    path: str,
) -> None:
    if binding not in catalog:
        raise ContractValidationError(
            "authority_binding", path, "binding is absent from registered catalog"
        )


def _physical_binding_value(value: Any, *, path: str) -> dict[str, str]:
    binding = validate_typed_artifact_binding_v1(value, path=path)
    if binding["sha256_kind"] != "physical":
        raise ContractValidationError(
            "runtime_file_authority",
            f"{path}.sha256_kind",
            "runtime file binding must use a physical hash",
        )
    return binding


def _physical_binding_list(value: Any, *, path: str) -> list[dict[str, str]]:
    rows = [
        _physical_binding_value(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]
    require_unique([item["artifact_ref"] for item in rows], path=path)
    return rows


def _physical_binding(
    artifact_ref: str,
    artifact_kind: str,
    source_path: Path,
    *,
    schema_version: str,
) -> dict[str, str]:
    path = Path(source_path).resolve()
    return {
        "artifact_ref": require_relative_path(
            artifact_ref, path="$.artifact_ref"
        ),
        "artifact_kind": require_string(
            artifact_kind, path="$.artifact_kind"
        ),
        "schema_version": require_string(
            schema_version, path="$.schema_version"
        ),
        "sha256": _physical_sha256(path),
        "sha256_kind": "physical",
    }


def _require_exact_source_map(
    values: Mapping[str, Path], *, expected_refs: Sequence[str], path: str
) -> None:
    if list(values) != list(expected_refs):
        raise ContractValidationError(
            "source_map_exact_cover",
            path,
            "explicit source paths must exact-cover declared refs in order",
        )


def _require_source_bytes(
    bindings: Sequence[Mapping[str, Any]],
    values: Mapping[str, Path],
    *,
    path: str,
) -> None:
    for index, raw_binding in enumerate(bindings):
        binding = _physical_binding_value(
            raw_binding, path=f"{path}.bindings[{index}]"
        )
        source_path = Path(values[binding["artifact_ref"]]).resolve()
        if not source_path.is_file():
            raise ContractValidationError(
                "missing_artifact",
                str(source_path),
                "explicit runtime source file is absent",
            )
        if _physical_sha256(source_path) != binding["sha256"]:
            raise ContractValidationError(
                "artifact_hash",
                binding["artifact_ref"],
                "explicit runtime source bytes differ from the producer binding",
            )


def _insert_source(values: dict[str, Path], artifact_ref: str, path: Path) -> None:
    if artifact_ref in values:
        raise ContractValidationError(
            "duplicate_artifact_ref", artifact_ref, "runtime artifact ref reused"
        )
    values[artifact_ref] = path


def _runtime_artifact_kind(artifact_ref: str) -> str:
    if artifact_ref == _HANDOFF_REF:
        return "scoring_handoff_v1"
    if artifact_ref == _SETTINGS_REF:
        return "evaluation_workflow_settings_v1"
    if artifact_ref == _LEGACY_D2L_INPUT_REF:
        return "d2l_evaluation_input_v1"
    if artifact_ref == _MANIFEST_REF:
        return "evaluation_benchmark_manifest_v1"
    if artifact_ref == _PREFLIGHT_REF:
        return "evaluation_benchmark_preflight_v1"
    if artifact_ref.startswith("runtime/overlays/"):
        return "evaluation_benchmark_overlay_v1"
    if artifact_ref.startswith("runtime/configs/"):
        return "evaluation_run_config_v1"
    return "runtime_artifact"


def _settings_arm_id(value: str) -> str:
    lowered = value.lower()
    if lowered not in {"s0", "s1"}:
        raise ContractValidationError(
            "d2l_arm_scope", "$.d2l_evaluation_input.arms", f"unexpected D2L arm {value}"
        )
    return lowered


def _benchmark_arm_id(value: str) -> str:
    lowered = require_string(value, path="$.arm_id").lower()
    if lowered == "s0":
        return "S0"
    if lowered == "s1":
        return "S1"
    if value in {"community", "google_nmt", "llm_lc"}:
        return value
    raise ContractValidationError("arm_id", "$.arm_id", f"unsupported arm {value}")


def _ordered_strings(value: Any, *, path: str) -> list[str]:
    rows = [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]
    if not rows:
        raise ContractValidationError("empty_array", path, "at least one value required")
    require_unique(rows, path=path)
    return rows


def _lookup_optional_runtime(
    values: Mapping[str, Any], runtime_id: str | None, *, path: str
) -> Any:
    if runtime_id is None:
        return None
    try:
        return values[runtime_id]
    except KeyError as exc:
        raise ContractValidationError(
            "runtime_registration",
            path,
            f"runtime object {runtime_id} is not registered",
        ) from exc


def _contained_path(root: Path, relative_path: str) -> Path:
    normalized = require_relative_path(relative_path, path="$.artifact_ref")
    base = Path(root).resolve()
    path = (base / Path(*normalized.split("/"))).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape", relative_path, "runtime artifact escapes bundle root"
        ) from exc
    return path


def _physical_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise ContractValidationError(
            "missing_artifact", str(source), "required JSON artifact is absent"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "invalid_json", str(source), "artifact is not canonical UTF-8 JSON"
        ) from exc
    return dict(require_mapping(value, path=str(source)))


def _json_bytes(
    value: Mapping[str, Any], *, policy: CanonicalPolicy | None = None
) -> bytes:
    if policy is not None:
        return (canonical_json(dict(value), policy=policy) + "\n").encode("utf-8")
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or target.read_bytes() != payload:
            raise ContractValidationError(
                "immutable_artifact",
                str(target),
                "existing runtime artifact differs",
            )
        return
    target.write_bytes(payload)


def _copy_immutable(source: Path, destination: Path) -> None:
    _write_immutable(destination, Path(source).read_bytes())
