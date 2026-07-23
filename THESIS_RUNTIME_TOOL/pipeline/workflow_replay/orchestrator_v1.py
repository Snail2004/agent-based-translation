from __future__ import annotations

import copy
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from .adapters_v1 import (
    D2LTranslationComponentAdapterV1,
    EvaluationComponentAdapterV1,
    PublicationComponentAdapterV1,
)
from .contracts_v1 import (
    ARM_IDS_V1,
    WorkflowReplayContractError,
    canonical_json_bytes,
    canonical_sha256,
    normalize_d2l_scoring_fragment_v1,
    physical_sha256,
)
from .relay_v1 import (
    ValidatedComponentAdapterV1,
    WorkflowRelayV1,
    validate_workflow_parent_package_v1,
)


AdapterFactoryV1 = Callable[[bool], ValidatedComponentAdapterV1]
SnapshotObserverV1 = Callable[[Path, bool], None]
WORKFLOW_LAUNCH_SELECTION_FILENAME_V1 = "workflow_launch_selection_v1.json"


class WorkflowOrchestratorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkflowComponentPausedV1(RuntimeError):
    def __init__(self, component_id: str, message: str = "component paused") -> None:
        super().__init__(message)
        self.component_id = component_id


def materialize_workflow_launch_selection_v1(
    parent_root: str | Path,
    *,
    evaluation_selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist the server-sealed Evaluation selection beside the parent package."""

    root = Path(parent_root).resolve()
    manifest = validate_workflow_parent_package_v1(root)
    selection = _validate_evaluation_selection_v1(evaluation_selection)
    parent_chapter_ids = {
        row["local_stage_id"].removeprefix("chapter_")
        for row in manifest["stages"]
        if row["component_id"] == "evaluation"
        and row["local_stage_id"].startswith("chapter_")
    }
    if parent_chapter_ids and not set(
        selection["selected_chapter_ids"]
    ).issubset(parent_chapter_ids):
        raise WorkflowOrchestratorError(
            "workflow_evaluation_selection_chapters",
            "Evaluation selection contains chapters outside the parent workflow.",
        )
    payload = {
        "schema_id": "WorkflowLaunchSelectionV1",
        "schema_version": "1.0.0",
        "workflow_run_id": manifest["workflow_run_id"],
        "job_id": manifest["job_id"],
        "source_package_bindings_sha256": canonical_sha256(
            manifest["source_package_bindings"]
        ),
        "evaluation_selection": selection,
    }
    row = {
        **payload,
        "integrity": {
            "launch_selection_sha256": canonical_sha256(payload),
        },
    }
    encoded = canonical_json_bytes(row) + b"\n"
    path = root / WORKFLOW_LAUNCH_SELECTION_FILENAME_V1
    _write_immutable_bytes(path, encoded)
    return load_workflow_launch_selection_v1(root)


def load_workflow_launch_selection_v1(
    parent_root: str | Path,
) -> dict[str, Any]:
    """Load and rebind the immutable launch selection to the parent package."""

    root = Path(parent_root).resolve()
    manifest = validate_workflow_parent_package_v1(root)
    path = root / WORKFLOW_LAUNCH_SELECTION_FILENAME_V1
    if not path.is_file() or path.is_symlink():
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_missing",
            "Workflow launch selection is missing.",
        )
    try:
        encoded = path.read_bytes()
        row = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_invalid",
            "Workflow launch selection is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(row, Mapping):
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_invalid",
            "Workflow launch selection must be an object.",
        )
    if encoded != canonical_json_bytes(row) + b"\n":
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_noncanonical",
            "Workflow launch selection bytes are not canonical.",
        )
    required = {
        "schema_id",
        "schema_version",
        "workflow_run_id",
        "job_id",
        "source_package_bindings_sha256",
        "evaluation_selection",
        "integrity",
    }
    if set(row) != required:
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_invalid",
            "Workflow launch selection fields differ from V1.",
        )
    if (
        row["schema_id"] != "WorkflowLaunchSelectionV1"
        or row["schema_version"] != "1.0.0"
        or row["workflow_run_id"] != manifest["workflow_run_id"]
        or row["job_id"] != manifest["job_id"]
        or _sha256(
            row["source_package_bindings_sha256"],
            "source_package_bindings_sha256",
        )
        != canonical_sha256(manifest["source_package_bindings"])
    ):
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_identity",
            "Workflow launch selection belongs to another parent package.",
        )
    selection = _validate_evaluation_selection_v1(row["evaluation_selection"])
    integrity = row["integrity"]
    if (
        not isinstance(integrity, Mapping)
        or set(integrity) != {"launch_selection_sha256"}
    ):
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_integrity",
            "Workflow launch selection integrity fields differ from V1.",
        )
    payload = copy.deepcopy(dict(row))
    payload.pop("integrity")
    if _sha256(
        integrity["launch_selection_sha256"],
        "launch_selection_sha256",
    ) != canonical_sha256(payload):
        raise WorkflowOrchestratorError(
            "workflow_launch_selection_hash",
            "Workflow launch selection hash drifted.",
        )
    accepted = copy.deepcopy(dict(row))
    accepted["evaluation_selection"] = selection
    return accepted


class TranslationExecutorV1(Protocol):
    def execute(self, observer: SnapshotObserverV1) -> Path: ...


class BaselineInputProviderV1(Protocol):
    def translation_inputs(
        self,
        *,
        workflow_run_id: str,
        selected_chapter_ids: Sequence[str],
        admitted_projection_binding: Mapping[str, Any],
        expected_block_count: int,
        block_universe_sha256: str,
    ) -> Sequence[Mapping[str, Any]]: ...


class EvaluationExecutorV1(Protocol):
    def execute(
        self,
        scoring_handoff: Mapping[str, Any],
        observer: SnapshotObserverV1,
    ) -> Path: ...


class EvaluationSettingsMaterializerV1(Protocol):
    def materialize_settings(
        self,
        scoring_handoff: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class PublicationExecutorV1(Protocol):
    def execute(
        self,
        *,
        scoring_handoff: Mapping[str, Any],
        selected_translation_input: Mapping[str, Any],
        selected_translation_path: Path,
        selected_chapter_ids: Sequence[str],
        observer: SnapshotObserverV1,
    ) -> Path: ...


@dataclass(frozen=True, slots=True)
class WorkflowOrchestratorResultV1:
    parent_root: Path
    manifest: dict[str, Any]
    scoring_handoff: dict[str, Any]
    scoring_receipt: dict[str, Any]
    translation_component_root: Path
    evaluation_component_root: Path
    publication_component_root: Path


@dataclass(frozen=True, slots=True)
class WorkflowTranslationResultV1:
    parent_root: Path
    manifest: dict[str, Any]
    translation_component_root: Path


@dataclass(frozen=True, slots=True)
class WorkflowScoringResultV1:
    parent_root: Path
    manifest: dict[str, Any]
    scoring_handoff: dict[str, Any]
    scoring_receipt: dict[str, Any]
    translation_component_root: Path
    evaluation_component_root: Path


@dataclass(frozen=True, slots=True)
class WorkflowScoringPreparationV1:
    parent_root: Path
    manifest: dict[str, Any]
    scoring_handoff: dict[str, Any]
    evaluation_settings: dict[str, Any]
    translation_component_root: Path


class ExistingTranslationComponentExecutorV1:
    """Reuse one already-terminal D2L component without launching a child."""

    def __init__(self, component_root: str | Path) -> None:
        self.component_root = Path(component_root).resolve()

    def execute(self, observer: SnapshotObserverV1) -> Path:
        observer(self.component_root, True)
        return self.component_root


class StaticBaselineInputProviderV1:
    """Provide three already-validated external baseline rows."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = tuple(copy.deepcopy(list(rows)))

    def translation_inputs(
        self,
        *,
        workflow_run_id: str,
        selected_chapter_ids: Sequence[str],
        admitted_projection_binding: Mapping[str, Any],
        expected_block_count: int,
        block_universe_sha256: str,
    ) -> Sequence[Mapping[str, Any]]:
        del workflow_run_id, selected_chapter_ids
        rows = copy.deepcopy(list(self._rows))
        if [row.get("arm_id") for row in rows if isinstance(row, Mapping)] != [
            "community",
            "google_nmt",
            "llm_lc",
        ]:
            raise WorkflowOrchestratorError(
                "baseline_arm_order",
                "Baseline provider must return community, google_nmt and llm_lc.",
            )
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise WorkflowOrchestratorError(
                    "baseline_input_shape",
                    f"Baseline row {index} must be an object.",
                )
            if row.get("source_binding") != admitted_projection_binding:
                raise WorkflowOrchestratorError(
                    "baseline_source_binding",
                    f"Baseline arm {row.get('arm_id')} binds another source.",
                )
            coverage = row.get("coverage")
            if (
                not isinstance(coverage, Mapping)
                or coverage.get("expected_block_count") != expected_block_count
                or str(coverage.get("block_universe_sha256") or "").lower()
                != block_universe_sha256.lower()
                or coverage.get("missing_block_count") != 0
                or coverage.get("failed_block_count") != 0
                or coverage.get("review_held_block_count") != 0
            ):
                raise WorkflowOrchestratorError(
                    "baseline_coverage",
                    f"Baseline arm {row.get('arm_id')} is not an exact admitted-universe input.",
                )
        return rows


class WorkflowOrchestratorV1:
    """Advance the single-writer parent through translation, scoring, and export."""

    def __init__(
        self,
        parent_root: str | Path,
        *,
        translation_executor: TranslationExecutorV1,
        selected_chapter_ids: Sequence[str],
        baseline_provider: BaselineInputProviderV1 | None = None,
        evaluation_executor: EvaluationExecutorV1 | None = None,
        evaluation_settings_materializer: (
            EvaluationSettingsMaterializerV1 | None
        ) = None,
        publication_executor: PublicationExecutorV1 | None = None,
        product_arm_id: str = "s1",
        translation_adapter_factory: AdapterFactoryV1 | None = None,
        evaluation_adapter_factory: AdapterFactoryV1 | None = None,
        publication_adapter_factory: AdapterFactoryV1 | None = None,
    ) -> None:
        self.parent_root = Path(parent_root).resolve()
        self.relay = WorkflowRelayV1.open_existing(self.parent_root)
        self.translation_executor = translation_executor
        self.baseline_provider = baseline_provider
        self.evaluation_executor = evaluation_executor
        self.evaluation_settings_materializer = (
            evaluation_settings_materializer
        )
        self.publication_executor = publication_executor
        self.selected_chapter_ids = _chapter_ids(selected_chapter_ids)
        if product_arm_id not in {"s0", "s1"}:
            raise WorkflowOrchestratorError(
                "publication_arm",
                "Publication V1 requires the D2L S0 or S1 product arm.",
            )
        self.product_arm_id = product_arm_id
        self.translation_adapter_factory = (
            translation_adapter_factory
            or (
                lambda terminal: D2LTranslationComponentAdapterV1(
                    require_terminal=terminal
                )
            )
        )
        self.evaluation_adapter_factory = (
            evaluation_adapter_factory
            or (
                lambda terminal: EvaluationComponentAdapterV1(
                    self._load_scoring_handoff(),
                    require_terminal=terminal,
                )
            )
        )
        self.publication_adapter_factory = (
            publication_adapter_factory
            or (
                lambda terminal: PublicationComponentAdapterV1(
                    require_terminal=terminal
                )
            )
        )

    def run_translation(self) -> WorkflowTranslationResultV1:
        translation_root = self._execute_translation()
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        translation = next(
            (
                row
                for row in manifest["components"]
                if row["component_id"] == "translation"
            ),
            None,
        )
        if translation is None or translation["status"] != "succeeded":
            raise WorkflowOrchestratorError(
                "translation_not_terminal",
                "Translation phase did not produce a terminal successful component.",
            )
        return WorkflowTranslationResultV1(
            parent_root=self.parent_root,
            manifest=manifest,
            translation_component_root=translation_root,
        )

    def run(self) -> WorkflowOrchestratorResultV1:
        translation_root = self.run_translation().translation_component_root
        scoring = self.run_scoring(translation_root)
        return self._run_publication(scoring)

    def run_scoring(
        self,
        translation_component_root: str | Path,
    ) -> WorkflowScoringResultV1:
        """Publish the five-arm handoff and run Evaluation, without Publication."""

        translation_root = Path(translation_component_root).resolve()
        if self.baseline_provider is None or self.evaluation_executor is None:
            raise WorkflowOrchestratorError(
                "workflow_runtime_incomplete",
                "Scoring requires registered baseline and Evaluation executors.",
            )
        handoff = self._publish_scoring_handoff(translation_root)
        if self.evaluation_settings_materializer is not None:
            settings = self.evaluation_settings_materializer.materialize_settings(
                handoff
            )
            self.relay.publish_evaluation_settings(settings)

        evaluation_root = self._execute_evaluation(handoff)
        receipt = _read_json(
            evaluation_root / "scoring_receipt.json",
            owner="Evaluation scoring receipt",
        )
        accepted_receipt = self.relay.accept_scoring_receipt(receipt)
        if accepted_receipt["status"] != "accepted":
            raise WorkflowOrchestratorError(
                "evaluation_rejected",
                "Evaluation rejected the exact five-arm scoring handoff.",
            )
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        return WorkflowScoringResultV1(
            parent_root=self.parent_root,
            manifest=manifest,
            scoring_handoff=handoff,
            scoring_receipt=accepted_receipt,
            translation_component_root=translation_root,
            evaluation_component_root=evaluation_root,
        )

    def run_prepared_scoring(
        self,
        *,
        expected_scoring_handoff: Mapping[str, Any],
        expected_evaluation_settings: Mapping[str, Any],
    ) -> WorkflowScoringResultV1:
        """Run Evaluation from already-published, file-backed scoring authority."""

        if self.evaluation_executor is None:
            raise WorkflowOrchestratorError(
                "workflow_runtime_incomplete",
                "Prepared scoring requires a registered Evaluation executor.",
            )
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        translation = next(
            (
                row
                for row in manifest["components"]
                if row["component_id"] == "translation"
            ),
            None,
        )
        if translation is None or translation["status"] != "succeeded":
            raise WorkflowOrchestratorError(
                "translation_not_terminal",
                "Prepared scoring requires a terminal successful Translation component.",
            )
        handoff = self._load_scoring_handoff()
        settings = _read_json(
            self.parent_root
            / "handoffs"
            / "evaluation_workflow_settings.json",
            owner="parent Evaluation workflow settings",
        )
        if handoff != expected_scoring_handoff:
            raise WorkflowOrchestratorError(
                "prepared_scoring_handoff_drift",
                "Parent scoring handoff differs from the registered runtime bundle.",
            )
        if settings != expected_evaluation_settings:
            raise WorkflowOrchestratorError(
                "prepared_scoring_settings_drift",
                "Parent Evaluation settings differ from the registered runtime bundle.",
            )

        evaluation_root = self._execute_evaluation(handoff)
        receipt = _read_json(
            evaluation_root / "scoring_receipt.json",
            owner="Evaluation scoring receipt",
        )
        accepted_receipt = self.relay.accept_scoring_receipt(receipt)
        if accepted_receipt["status"] != "accepted":
            raise WorkflowOrchestratorError(
                "evaluation_rejected",
                "Evaluation rejected the exact five-arm scoring handoff.",
            )
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        translation_root = (
            self.parent_root
            / "components"
            / "translation"
            / translation["component_run_id"]
        )
        return WorkflowScoringResultV1(
            parent_root=self.parent_root,
            manifest=manifest,
            scoring_handoff=handoff,
            scoring_receipt=accepted_receipt,
            translation_component_root=translation_root,
            evaluation_component_root=evaluation_root,
        )

    def prepare_scoring(
        self,
        translation_component_root: str | Path,
    ) -> WorkflowScoringPreparationV1:
        """Prepare exact scoring authority without running any scorer."""

        if (
            self.baseline_provider is None
            or self.evaluation_settings_materializer is None
        ):
            raise WorkflowOrchestratorError(
                "workflow_runtime_incomplete",
                "Scoring preparation requires registered baselines and settings authority.",
            )
        translation_root = Path(translation_component_root).resolve()
        handoff = self._publish_scoring_handoff(translation_root)
        settings = self.relay.publish_evaluation_settings(
            self.evaluation_settings_materializer.materialize_settings(
                handoff
            )
        )
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        return WorkflowScoringPreparationV1(
            parent_root=self.parent_root,
            manifest=manifest,
            scoring_handoff=handoff,
            evaluation_settings=settings,
            translation_component_root=translation_root,
        )

    def _publish_scoring_handoff(
        self,
        translation_root: Path,
    ) -> dict[str, Any]:
        if self.baseline_provider is None:
            raise WorkflowOrchestratorError(
                "workflow_runtime_incomplete",
                "Scoring handoff requires a registered baseline provider.",
            )
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        translation = next(
            (
                row
                for row in manifest["components"]
                if row["component_id"] == "translation"
            ),
            None,
        )
        if translation is None or translation["status"] != "succeeded":
            raise WorkflowOrchestratorError(
                "translation_not_terminal",
                "Scoring requires a terminal successful Translation component.",
            )
        fragment = _read_json(
            translation_root / "scoring_handoff_fragment.json",
            owner="D2L scoring handoff fragment",
        )
        ref_map = self._translation_artifact_ref_map()
        projected = normalize_d2l_scoring_fragment_v1(
            fragment,
            artifact_ref_map=ref_map,
        )
        if projected["workflow_run_id"] != self.relay.workflow_run_id:
            raise WorkflowOrchestratorError(
                "translation_workflow_identity",
                "D2L scoring fragment belongs to another parent workflow.",
            )
        if fragment["selected_chapter_ids"] != self.selected_chapter_ids:
            raise WorkflowOrchestratorError(
                "translation_chapter_binding",
                "D2L selected chapters differ from the parent launch.",
            )
        d2l_inputs = projected["translation_inputs"]
        admitted = projected["source_package_bindings"][3]["binding"]
        expected_count = d2l_inputs[0]["coverage"]["expected_block_count"]
        block_universe = d2l_inputs[0]["coverage"]["block_universe_sha256"]
        if any(
            row["coverage"]["expected_block_count"] != expected_count
            or row["coverage"]["block_universe_sha256"] != block_universe
            for row in d2l_inputs
        ):
            raise WorkflowOrchestratorError(
                "translation_arm_coverage",
                "D2L S0/S1 coverage identities differ.",
            )
        baseline_inputs = list(
            self.baseline_provider.translation_inputs(
                workflow_run_id=self.relay.workflow_run_id,
                selected_chapter_ids=self.selected_chapter_ids,
                admitted_projection_binding=admitted,
                expected_block_count=expected_count,
                block_universe_sha256=block_universe,
            )
        )
        all_inputs = [*d2l_inputs, *baseline_inputs]
        if [row.get("arm_id") for row in all_inputs] != list(ARM_IDS_V1):
            raise WorkflowOrchestratorError(
                "scoring_arm_order",
                "Workflow scoring inputs must use the exact five-arm order.",
            )
        handoff = self.relay.publish_scoring_handoff(
            handoff_id=f"handoff_{self.relay.workflow_run_id}_v1",
            source_package_bindings=projected["source_package_bindings"],
            optional_bindings=projected["optional_bindings"],
            translation_inputs=all_inputs,
            created_at=self.relay.created_at,
        )
        return handoff

    def _run_publication(
        self,
        scoring: WorkflowScoringResultV1,
    ) -> WorkflowOrchestratorResultV1:
        if self.publication_executor is None:
            raise WorkflowOrchestratorError(
                "workflow_runtime_incomplete",
                "Publication executor is not registered.",
        )
        handoff = scoring.scoring_handoff
        translation_root = scoring.translation_component_root
        fragment = _read_json(
            translation_root / "scoring_handoff_fragment.json",
            owner="D2L scoring handoff fragment",
        )
        selected_input = next(
            row for row in handoff["translation_inputs"]
            if row["arm_id"] == self.product_arm_id
        )
        selected_path = _component_artifact_path(
            translation_root,
            fragment,
            arm_id=self.product_arm_id,
        )
        publication_root = self._execute_publication(
            handoff,
            selected_input=selected_input,
            selected_path=selected_path,
        )
        manifest = validate_workflow_parent_package_v1(self.parent_root)
        if manifest["status"] != "succeeded":
            raise WorkflowOrchestratorError(
                "workflow_not_terminal",
                "Parent workflow is not terminal after Publication.",
            )
        return WorkflowOrchestratorResultV1(
            parent_root=self.parent_root,
            manifest=manifest,
            scoring_handoff=handoff,
            scoring_receipt=scoring.scoring_receipt,
            translation_component_root=translation_root,
            evaluation_component_root=scoring.evaluation_component_root,
            publication_component_root=publication_root,
        )

    def _execute_translation(self) -> Path:
        def observe(root: Path, terminal: bool) -> None:
            self.relay.ingest_component(
                root,
                adapter=self.translation_adapter_factory(terminal),
            )

        try:
            root = self.translation_executor.execute(observe).resolve()
        except WorkflowComponentPausedV1:
            raise
        observe(root, True)
        return root

    def _execute_evaluation(self, handoff: Mapping[str, Any]) -> Path:
        def observe(root: Path, terminal: bool) -> None:
            self.relay.ingest_component(
                root,
                adapter=self.evaluation_adapter_factory(terminal),
            )

        try:
            if self.evaluation_executor is None:
                raise WorkflowOrchestratorError(
                    "workflow_runtime_incomplete",
                    "Evaluation executor is not registered.",
                )
            root = self.evaluation_executor.execute(handoff, observe).resolve()
        except WorkflowComponentPausedV1:
            raise
        observe(root, True)
        return root

    def _execute_publication(
        self,
        handoff: Mapping[str, Any],
        *,
        selected_input: Mapping[str, Any],
        selected_path: Path,
    ) -> Path:
        def observe(root: Path, terminal: bool) -> None:
            self.relay.ingest_component(
                root,
                adapter=self.publication_adapter_factory(terminal),
            )

        if self.publication_executor is None:
            raise WorkflowOrchestratorError(
                "workflow_runtime_incomplete",
                "Publication executor is not registered.",
            )
        root = self.publication_executor.execute(
            scoring_handoff=handoff,
            selected_translation_input=selected_input,
            selected_translation_path=selected_path,
            selected_chapter_ids=self.selected_chapter_ids,
            observer=observe,
        ).resolve()
        observe(root, True)
        return root

    def _translation_artifact_ref_map(self) -> dict[str, str]:
        index = _read_json(
            self.parent_root / "artifact_index.json",
            owner="parent artifact index",
        )
        result = {}
        for row in index["artifacts"]:
            producer = row.get("producer") or {}
            component_ref = row.get("component_artifact_ref")
            if (
                producer.get("component_id") == "translation"
                and isinstance(component_ref, str)
            ):
                result[component_ref] = row["binding"]["artifact_ref"]
        if not result:
            raise WorkflowOrchestratorError(
                "translation_artifact_import",
                "Parent relay contains no imported D2L artifacts.",
            )
        return result

    def _load_scoring_handoff(self) -> dict[str, Any]:
        return _read_json(
            self.parent_root / "handoffs" / "scoring_handoff.json",
            owner="parent scoring handoff",
        )


def validate_workflow_runtime_registration_v1(
    value: Mapping[str, Any],
    *,
    expected_job_id: str,
    expected_source_binding_sha256: str,
    selected_chapter_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate the server-owned readiness record used by App preflight."""

    if not isinstance(value, Mapping):
        raise WorkflowOrchestratorError(
            "runtime_registration_shape",
            "Workflow runtime registration must be an object.",
        )
    required = {
        "schema_id",
        "schema_version",
        "job_id",
        "source_binding_sha256",
        "translation_executor_id",
        "baseline_bundle",
        "evaluation_executor_id",
        "publication_executor_id",
        "supported_chapter_ids",
        "status",
        "blockers",
        "integrity",
    }
    if set(value) != required:
        raise WorkflowOrchestratorError(
            "runtime_registration_shape",
            "Workflow runtime registration fields differ from V1.",
        )
    if (
        value["schema_id"] != "WorkflowRuntimeRegistrationV1"
        or value["schema_version"] != "1.0.0"
        or value["job_id"] != expected_job_id
        or str(value["source_binding_sha256"]).lower()
        != expected_source_binding_sha256.lower()
    ):
        raise WorkflowOrchestratorError(
            "runtime_registration_identity",
            "Workflow runtime registration belongs to another job/source.",
        )
    if value["translation_executor_id"] != "d2l_project_campaign_v1":
        raise WorkflowOrchestratorError(
            "runtime_translation_executor",
            "Registered translation executor is unsupported.",
        )
    if value["evaluation_executor_id"] != "evaluation_five_arm_benchmark_v1":
        raise WorkflowOrchestratorError(
            "runtime_evaluation_executor",
            "Registered Evaluation executor is unsupported.",
        )
    if value["publication_executor_id"] != "selected_chapter_publication_v1":
        raise WorkflowOrchestratorError(
            "runtime_publication_executor",
            "Registered Publication executor is unsupported.",
        )
    baseline = value["baseline_bundle"]
    if (
        not isinstance(baseline, Mapping)
        or set(baseline)
        != {
            "arm_ids",
            "artifact_ref",
            "sha256",
            "sha256_kind",
            "status",
        }
        or baseline["arm_ids"] != ["community", "google_nmt", "llm_lc"]
        or baseline["sha256_kind"] != "physical"
    ):
        raise WorkflowOrchestratorError(
            "runtime_baseline_bundle",
            "Registered baseline bundle shape/order is invalid.",
        )
    _sha256(baseline["sha256"], "baseline_bundle.sha256")
    supported = _chapter_ids(value["supported_chapter_ids"])
    requested = (
        _chapter_ids(selected_chapter_ids)
        if selected_chapter_ids is not None
        else None
    )
    if requested is not None and any(item not in supported for item in requested):
        raise WorkflowOrchestratorError(
            "runtime_chapter_support",
            "Workflow runtime does not support every selected chapter.",
        )
    blockers = value["blockers"]
    if not isinstance(blockers, list) or any(
        not isinstance(item, str) or not item for item in blockers
    ):
        raise WorkflowOrchestratorError(
            "runtime_blockers",
            "Workflow runtime blockers must be string codes.",
        )
    if value["status"] not in {"ready", "blocked"}:
        raise WorkflowOrchestratorError(
            "runtime_status",
            "Workflow runtime status must be ready or blocked.",
        )
    if (value["status"] == "ready") != (
        not blockers and baseline["status"] == "ready"
    ):
        raise WorkflowOrchestratorError(
            "runtime_status",
            "Workflow runtime readiness disagrees with blockers/baselines.",
        )
    integrity = value["integrity"]
    if not isinstance(integrity, Mapping) or set(integrity) != {
        "registration_sha256"
    }:
        raise WorkflowOrchestratorError(
            "runtime_registration_integrity",
            "Workflow runtime registration integrity shape is invalid.",
        )
    payload = copy.deepcopy(dict(value))
    payload["integrity"].pop("registration_sha256")
    if canonical_sha256(payload) != str(
        integrity["registration_sha256"]
    ).lower():
        raise WorkflowOrchestratorError(
            "runtime_registration_hash",
            "Workflow runtime registration hash drifted.",
        )
    return copy.deepcopy(dict(value))


def load_workflow_runtime_registration_v1(
    job_root: str | Path,
    *,
    expected_job_id: str,
    expected_source_binding_sha256: str,
    selected_chapter_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    root = Path(job_root).resolve()
    path = _resolve_file(root, "workflow_runtime_v1.json")
    value = _read_json(path, owner="workflow runtime registration")
    registration = validate_workflow_runtime_registration_v1(
        value,
        expected_job_id=expected_job_id,
        expected_source_binding_sha256=expected_source_binding_sha256,
        selected_chapter_ids=selected_chapter_ids,
    )
    baseline = registration["baseline_bundle"]
    baseline_path = _resolve_file(root, baseline["artifact_ref"])
    if physical_sha256(baseline_path.read_bytes()) != baseline["sha256"].lower():
        raise WorkflowOrchestratorError(
            "runtime_baseline_hash",
            "Registered baseline bundle bytes drifted.",
        )
    return registration


def _component_artifact_path(
    component_root: Path,
    fragment: Mapping[str, Any],
    *,
    arm_id: str,
) -> Path:
    raw_input = next(
        row for row in fragment["translation_inputs"] if row["arm_id"] == arm_id
    )
    component_ref = raw_input["artifact"]["artifact_ref"]
    index = _read_json(
        component_root / "artifact_index.json",
        owner="D2L artifact index",
    )
    row = next(
        (
            item
            for item in index["artifacts"]
            if item["artifact_ref"] == component_ref
        ),
        None,
    )
    if row is None:
        raise WorkflowOrchestratorError(
            "translation_artifact_missing",
            f"D2L artifact index does not contain {arm_id}.",
        )
    path = _resolve_file(component_root, row["relative_path"])
    if (
        row["sha256_kind"] != "physical"
        or physical_sha256(path.read_bytes()) != str(row["sha256"]).lower()
    ):
        raise WorkflowOrchestratorError(
            "translation_artifact_hash",
            f"D2L {arm_id} artifact bytes drifted.",
        )
    return path


def _read_json(path: Path, *, owner: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise WorkflowOrchestratorError(
            "workflow_artifact_missing",
            f"{owner} is missing.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowOrchestratorError(
            "workflow_artifact_json",
            f"{owner} is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowOrchestratorError(
            "workflow_artifact_shape",
            f"{owner} must be an object.",
        )
    return value


def _resolve_file(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or "\\" in relative
    ):
        raise WorkflowOrchestratorError(
            "workflow_path",
            "Workflow artifact path is unsafe.",
        )
    path = (root / Path(*pure.parts)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkflowOrchestratorError(
            "workflow_path",
            "Workflow artifact path escapes its root.",
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise WorkflowOrchestratorError(
            "workflow_artifact_missing",
            f"Workflow artifact is missing: {relative}.",
        )
    return path


def _chapter_ids(values: Sequence[str] | None) -> list[str]:
    if (
        values is None
        or not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
    ):
        raise WorkflowOrchestratorError(
            "workflow_chapters",
            "Selected chapter IDs must be an array.",
        )
    result = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 160
            or not all(character.isalnum() or character in "_.:-" for character in value)
        ):
            raise WorkflowOrchestratorError(
                "workflow_chapters",
                "Selected chapter ID is invalid.",
            )
        result.append(value)
    if not result or len(result) != len(set(result)):
        raise WorkflowOrchestratorError(
            "workflow_chapters",
            "Selected chapter IDs must be non-empty and duplicate-free.",
        )
    return result


def _validate_evaluation_selection_v1(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowOrchestratorError(
            "workflow_evaluation_selection",
            "Evaluation selection must be an object.",
        )
    basis_keys = {
        "settings_option_id",
        "selected_chapter_ids",
        "selected_arm_ids",
        "selected_scorer_ids",
        "highlight_pair",
        "registered_option_sha256",
    }
    if set(value) != basis_keys | {"selection_sha256"}:
        raise WorkflowOrchestratorError(
            "workflow_evaluation_selection",
            "Evaluation selection fields differ from V1.",
        )
    option_id = value["settings_option_id"]
    if (
        not isinstance(option_id, str)
        or not option_id
        or len(option_id) > 160
    ):
        raise WorkflowOrchestratorError(
            "workflow_evaluation_selection",
            "Evaluation settings option ID is invalid.",
        )
    chapters = _chapter_ids(value["selected_chapter_ids"])
    arms = _workflow_ids(
        value["selected_arm_ids"],
        owner="selected_arm_ids",
        minimum=2,
    )
    scorers = _workflow_ids(
        value["selected_scorer_ids"],
        owner="selected_scorer_ids",
        minimum=1,
    )
    highlight = value["highlight_pair"]
    if highlight is not None:
        if (
            not isinstance(highlight, Mapping)
            or set(highlight) != {"baseline_arm_id", "candidate_arm_id"}
            or highlight["baseline_arm_id"] not in arms
            or highlight["candidate_arm_id"] not in arms
            or highlight["baseline_arm_id"] == highlight["candidate_arm_id"]
        ):
            raise WorkflowOrchestratorError(
                "workflow_evaluation_selection",
                "Evaluation highlight pair must contain two selected arms.",
            )
        highlight = dict(highlight)
    basis = {
        "settings_option_id": option_id,
        "selected_chapter_ids": chapters,
        "selected_arm_ids": arms,
        "selected_scorer_ids": scorers,
        "highlight_pair": highlight,
        "registered_option_sha256": _sha256(
            value["registered_option_sha256"],
            "registered_option_sha256",
        ),
    }
    if _sha256(value["selection_sha256"], "selection_sha256") != canonical_sha256(
        basis
    ):
        raise WorkflowOrchestratorError(
            "workflow_evaluation_selection_hash",
            "Evaluation selection hash drifted.",
        )
    return {**basis, "selection_sha256": canonical_sha256(basis)}


def _workflow_ids(
    value: Any,
    *,
    owner: str,
    minimum: int,
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) != len(set(value))
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 160
            or not all(
                character.isalnum() or character in "_.:-"
                for character in item
            )
            for item in value
        )
    ):
        raise WorkflowOrchestratorError(
            "workflow_evaluation_selection",
            f"{owner} is invalid.",
        )
    return list(value)


def _write_immutable_bytes(path: Path, encoded: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise WorkflowOrchestratorError(
                "workflow_launch_selection_collision",
                "Workflow launch selection already exists with different bytes.",
            )
        return
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_bytes() != encoded
            ):
                raise WorkflowOrchestratorError(
                    "workflow_launch_selection_collision",
                    "Workflow launch selection raced with different bytes.",
                )
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(value: Any, owner: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise WorkflowOrchestratorError(
            "workflow_sha256",
            f"{owner} must be SHA-256.",
        )
    return value.lower()


__all__ = [
    "BaselineInputProviderV1",
    "EvaluationExecutorV1",
    "EvaluationSettingsMaterializerV1",
    "ExistingTranslationComponentExecutorV1",
    "PublicationExecutorV1",
    "SnapshotObserverV1",
    "StaticBaselineInputProviderV1",
    "TranslationExecutorV1",
    "WorkflowComponentPausedV1",
    "WorkflowOrchestratorError",
    "WorkflowOrchestratorResultV1",
    "WorkflowScoringPreparationV1",
    "WorkflowScoringResultV1",
    "WorkflowTranslationResultV1",
    "WorkflowOrchestratorV1",
    "load_workflow_launch_selection_v1",
    "load_workflow_runtime_registration_v1",
    "materialize_workflow_launch_selection_v1",
    "validate_workflow_runtime_registration_v1",
]
