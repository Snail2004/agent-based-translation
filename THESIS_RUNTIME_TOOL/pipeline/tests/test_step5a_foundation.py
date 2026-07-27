from __future__ import annotations

from dataclasses import dataclass, replace
import json
import multiprocessing
from pathlib import Path
import shutil
import socket
from typing import Any, cast

import pytest

from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.step5_authority import (
    AuthorityError,
    BindOccurrenceActive,
    RemapReferenceActive,
    RetainUnchangedRow,
    classify_independence,
    fast_path_allowed,
    promote,
    qualified_for,
    stale_qualification_decisions,
    validate_blinding,
    validate_qualification_manifest,
)
from pipeline.literary.step5_boundary import (
    AccessLedger,
    BoundaryError,
    RuntimeSpy,
    assert_core_import_boundary,
)
from pipeline.literary.step5_budget import (
    AccountingTotals,
    BudgetContractError,
    BudgetExceededError,
    CallPlanEntry,
    UsageSnapshot,
    pre_apply_gate,
    preflight_gate,
    seal_call_plan,
)
from pipeline.literary.step5_preregister import (
    OracleGroup,
    PreregisterError,
    preregister_oracle_split,
    public_split_manifest,
)
from pipeline.literary.step5_store import (
    AuditRecord,
    HeldOutAccessError,
    HeldOutGateCapability,
    HeldOutVault,
    StaleParentError,
    Step5Store,
    StoreError,
    validate_safety_seed_shape,
)
from pipeline.literary.step5_support import (
    SupportedItem,
    build_support_reverse_index,
    compute_invalidation_cone,
)
from pipeline.literary.step5_types import (
    AgreementRecord,
    AuthorityIndependencePolicy,
    BlindingValidationRecord,
    CanonicalRecord,
    CheckerInputSection,
    DecisionQuestion,
    DecisionRevision,
    ExistingCandidateSignature,
    FullScopeChangeSet,
    Generation,
    HumanAuthorityRoute,
    HumanDecisionRecord,
    MetricRatio,
    MetricThreshold,
    ModelAuthorityRoute,
    ModelIdentity,
    QualificationBinding,
    QualificationManifest,
    QuarantineProposedRecord,
    SafetySeedChangeSet,
    Step5ContractError,
    SupportSet,
    allocate_candidate_handles,
    build_checker_input_section,
    build_checker_input_section_manifest,
    canonical_signature_hash,
    content_address,
    shuffle_candidate_handles,
    validate_field_ordering_classes,
    verify_content_address,
    with_content_address,
)


LINEAGE = "lineage_book_01"
CAPS = {key: 20 for key in ("R", "J", "Cs", "Cd", "F", "P", "A", "De", "Dc", "X")}


def _seal(record: CanonicalRecord) -> Any:
    return with_content_address(record)


def _support_index():
    return build_support_reverse_index(())


def _full_changeset(
    *, parent: str | None, view: dict[str, Any], suffix: str = "a"
) -> FullScopeChangeSet:
    return cast(
        FullScopeChangeSet,
        _seal(
            FullScopeChangeSet(
                state_lineage_id=LINEAGE,
                source_scope_id=f"bk_ch01_{suffix}",
                parent_generation_hash=parent,
                bundle_manifest_hash="bundle_01",
                validator_contract_hash="validator_01",
                decision_revisions=(),
                overlay_record_refs=frozenset(),
                quarantine_record_refs=frozenset(),
                invalidation_refs=(),
                retained_row_ids=frozenset(),
                materialized_view_hash=canonical_hash(view),
                estimated_apply_cost=1,
            )
        ),
    )


def _prepare_generation(
    store: Step5Store,
    *,
    parent: str | None,
    suffix: str,
    created_at: str = "t1",
) -> Generation:
    view = {"rows": [suffix]}
    semantic = {"state": suffix}
    changeset = _full_changeset(parent=parent, view=view, suffix=suffix)
    store.put_changeset(changeset)
    support_hash = store.put_support_index(_support_index())
    semantic_hash = store.put_semantic_state(semantic)
    view_hash = store.put_materialized_view(view)
    draft = Generation(
        parent_generation_hash=parent,
        state_lineage_id=LINEAGE,
        kind="full",
        generation_schema_version="s5a_v1",
        changeset_hash=changeset.changeset_hash,
        support_index_hash=support_hash,
        authority_policy_hash="authority_01",
        qualification_policy_hash="qualification_01",
        validator_contract_hash="validator_01",
        semantic_state_hash=semantic_hash,
        materialized_view_hash=view_hash,
        created_at_audit=created_at,
    )
    generation = Generation(
        **draft.to_canonical_payload(),
        generation_hash=draft.canonical_hash(),
        created_at_audit=created_at,
    )
    store.put_generation(generation)
    return generation


def _publish_initial(store: Step5Store) -> Generation:
    generation = _prepare_generation(store, parent=None, suffix="initial")
    store.cas_switch(
        state_lineage_id=LINEAGE,
        expected_current=None,
        new_generation_hash=generation.generation_hash,
    )
    return generation


def _model(family: str, model_id: str, provider: str = "provider") -> ModelIdentity:
    return ModelIdentity(
        provider=provider,
        model_family_lineage_id=family,
        model_id=model_id,
        weights_version="weights-v1",
    )


def _policy() -> AuthorityIndependencePolicy:
    return cast(
        AuthorityIndependencePolicy,
        _seal(
            AuthorityIndependencePolicy(
                policy_version="independence-v1",
                qualification_policy_hash="qualification-policy-v1",
            )
        ),
    )


def _qualification(
    model: ModelIdentity,
    kind: str,
    policy: AuthorityIndependencePolicy,
    *,
    passed: bool = True,
    split: str = "split-v1",
) -> QualificationManifest:
    numerator = 9 if passed else 1
    draft = QualificationManifest(
        model_identity=model,
        decision_kind=kind,  # type: ignore[arg-type]
        oracle_split_manifest_hash=split,
        qualify_group_ids=frozenset({"g1", "g2"}),
        independence_policy_hash=policy.policy_hash,
        qualification_policy_hash=policy.qualification_policy_hash,
        metric_schema_version="metrics-v1",
        measured_metrics=(MetricRatio(metric_id="precision", numerator=numerator, denominator=10),),
        thresholds=(
            MetricThreshold(
                metric_id="precision",
                comparator=">=",
                threshold_numerator=8,
                threshold_denominator=10,
            ),
        ),
        qualified_result=passed,
    )
    return cast(QualificationManifest, _seal(draft))


def _route(
    route_id: str,
    role: str,
    model: ModelIdentity,
    manifests: tuple[QualificationManifest, ...] = (),
) -> ModelAuthorityRoute:
    return ModelAuthorityRoute(
        authority_route_id=route_id,
        role=role,  # type: ignore[arg-type]
        model_identity=model,
        qualification_bindings=tuple(
            QualificationBinding(
                decision_kind=manifest.decision_kind,
                qualification_manifest_hash=manifest.qualification_manifest_hash,
            )
            for manifest in manifests
        ),
    )


def _agreement(
    adjudicator: ModelAuthorityRoute,
    checker: ModelAuthorityRoute,
    *,
    question: DecisionQuestion,
    signature: str = "signature-1",
) -> tuple[AgreementRecord, dict[str, BlindingValidationRecord]]:
    section_manifest = build_checker_input_section_manifest(
        checker_request_fingerprint="request-b",
        checker_input_sections=(
            build_checker_input_section(
                section_id="checker-context",
                rendered_content="Occurrence-grounded checker context only.",
                source_artifact_hashes=frozenset({"bundle-artifact"}),
            ),
        ),
    )
    blind = BlindingValidationRecord(
        checker_request_fingerprint="request-b",
        checker_input_section_manifest_hash=(
            section_manifest.checker_input_section_manifest_hash
        ),
        checker_input_section_manifest=section_manifest,
        adjudicator_response_artifact_hash="response-a",
        validator_contract_hash="validator-1",
    )
    blind_hash = content_address(blind)
    agreement = AgreementRecord(
        proposal_record_id="proposal-1",
        question=question,
        request_fingerprint_a="request-a",
        request_fingerprint_b="request-b",
        canonical_signature_hash_a=signature,
        canonical_signature_hash_b=signature,
        blinding_validation_record_hash=blind_hash,
        adjudicator_route_id=adjudicator.authority_route_id,
        checker_route_id=checker.authority_route_id,
    )
    return agreement, {blind_hash: blind}


def _cas_worker(
    root: str,
    lineage: str,
    parent: str,
    generation_hash: str,
    barrier: Any,
    queue: Any,
) -> None:
    store = Step5Store(Path(root))
    barrier.wait()
    try:
        store.cas_switch(
            state_lineage_id=lineage,
            expected_current=parent,
            new_generation_hash=generation_hash,
        )
    except StaleParentError:
        queue.put("stale")
    else:
        queue.put("won")


def test_probe_01_crash_before_pointer_switch_keeps_prior_generation(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    first = _publish_initial(store)
    second = _prepare_generation(store, parent=first.generation_hash, suffix="second")
    with pytest.raises(RuntimeError, match="crash"):
        store.cas_switch(
            state_lineage_id=LINEAGE,
            expected_current=first.generation_hash,
            new_generation_hash=second.generation_hash,
            before_pointer_switch=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
        )
    assert store.current_generation_hash(LINEAGE) == first.generation_hash
    assert store.load_generation(first.generation_hash)["semantic_state_hash"]


def test_probe_02_straight_resume_canonical_state_equal(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "straight")
    generation = _publish_initial(store)
    straight = store.load_generation(generation.generation_hash)
    copied = tmp_path / "resume"
    shutil.copytree(tmp_path / "straight", copied)
    resumed_store = Step5Store(copied)
    resumed = resumed_store.load_generation(
        resumed_store.current_generation_hash(LINEAGE) or ""
    )
    assert straight == resumed
    broken = dict(resumed)
    broken["semantic_state_hash"] = "foreign"
    assert broken != straight


def test_probe_03_support_alternatives_preserve_then_invalidate() -> None:
    item = SupportedItem(
        item_id="fact-1",
        support_alternatives=(
            SupportSet(support_set_id="set-a", member_ids=frozenset({"a", "b"})),
            SupportSet(support_set_id="set-c", member_ids=frozenset({"c"})),
        ),
    )
    index = build_support_reverse_index((item,))
    assert compute_invalidation_cone(index, unavailable_support_ids=frozenset({"a"})) == ()
    assert compute_invalidation_cone(
        index, unavailable_support_ids=frozenset({"a", "c"})
    ) == ("fact-1",)


def test_probe_04_forbidden_semantic_read_is_fatal_and_logged(tmp_path: Path) -> None:
    allowed = tmp_path / "step5"
    denied = tmp_path / "m1v3"
    allowed.mkdir()
    denied.mkdir()
    (allowed / "ok.txt").write_text("ok", encoding="utf-8")
    (denied / "state.json").write_text("{}", encoding="utf-8")
    ledger = AccessLedger()
    with RuntimeSpy(allowed_roots=[allowed], denied_paths=[denied], ledger=ledger):
        assert (allowed / "ok.txt").read_text(encoding="utf-8") == "ok"
        with pytest.raises(BoundaryError):
            (denied / "state.json").read_text(encoding="utf-8")
        with pytest.raises(BoundaryError, match="network unavailable"):
            socket.socket().connect(("127.0.0.1", 9))
    assert any(not row.allowed and "state.json" in row.target for row in ledger.events)
    core = Path(__file__).parents[1] / "literary"
    assert_core_import_boundary(
        [
            core / "step5_types.py",
            core / "step5_authority.py",
            core / "step5_support.py",
            core / "step5_budget.py",
            core / "step5_preregister.py",
        ]
    )


def test_probe_05_path_clock_independence(tmp_path: Path) -> None:
    first_store = Step5Store(tmp_path / "a")
    one = _prepare_generation(first_store, parent=None, suffix="same", created_at="t1")
    second_store = Step5Store(tmp_path / "b")
    two = _prepare_generation(second_store, parent=None, suffix="same", created_at="t2")
    assert one.generation_hash == two.generation_hash
    bad = replace(two, validator_contract_hash="different", generation_hash="")
    assert bad.canonical_hash() != one.generation_hash


def test_probe_06_collection_ordering_classes() -> None:
    validate_field_ordering_classes()
    left = ExistingCandidateSignature(
        candidate_handle="cand_01",
        covered_occurrence_ids=frozenset({"o2", "o1"}),
        referent_kind="person",
    )
    right = ExistingCandidateSignature(
        candidate_handle="cand_01",
        covered_occurrence_ids=frozenset({"o1", "o2"}),
        referent_kind="person",
    )
    assert left.canonical_hash() == right.canonical_hash()
    support_left = SupportedItem(
        item_id="item",
        support_alternatives=(
            SupportSet(support_set_id="z", member_ids=frozenset({"a"})),
            SupportSet(support_set_id="a", member_ids=frozenset({"z"})),
        ),
    )
    support_right = replace(
        support_left,
        support_alternatives=tuple(reversed(support_left.support_alternatives)),
    )
    assert support_left.canonical_hash() == support_right.canonical_hash()
    view = {"rows": []}
    first = _full_changeset(parent=None, view=view)
    second = replace(first, invalidation_refs=("b", "a"), changeset_hash="")
    third = replace(first, invalidation_refs=("a", "b"), changeset_hash="")
    assert second.canonical_hash() != third.canonical_hash()

    @dataclass(frozen=True, slots=True)
    class UnknownCollection(CanonicalRecord):
        mystery_rows: tuple[str, ...]

    @dataclass(frozen=True, slots=True)
    class UnknownMapping(CanonicalRecord):
        mystery_map: dict[str, str]

    with pytest.raises(Step5ContractError, match="unknown collection"):
        UnknownCollection(("x",)).to_canonical_payload()
    with pytest.raises(Step5ContractError, match="unknown collection"):
        UnknownMapping({"x": "y"}).to_canonical_payload()
    with pytest.raises(Step5ContractError, match="multiple ordering classes"):
        validate_field_ordering_classes(
            set_like=frozenset({"duplicate"}),
            sequence_like=frozenset({"duplicate"}),
            mapping_like=frozenset(),
        )


def test_probe_07_content_address_projection_and_tamper(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    generation = _publish_initial(store)
    verify_content_address(generation)
    with pytest.raises((TypeError, ValueError)):
        canonical_hash(generation)
    path = store.root / "generations" / f"{generation.generation_hash}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["validator_contract_hash"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StoreError, match="generation content hash"):
        store.load_generation(generation.generation_hash)
    foreign = replace(
        generation,
        generation_hash="",
        state_lineage_id="another_lineage",
    )
    foreign = cast(Generation, _seal(foreign))
    with pytest.raises(StoreError, match="lineage disagree"):
        store.put_generation(foreign)


def test_probe_08_pairwise_independence_and_unqualified_distinct() -> None:
    policy = _policy()
    adjudicator = _route("adj", "adjudicator", _model("family-a", "large"))
    same = _route("same", "checker", _model("family-a", "small", "other-provider"))
    distinct = _route("distinct", "checker", _model("family-b", "checker"))
    assert classify_independence(adjudicator, same, policy) == "same_model_lineage"
    assert classify_independence(adjudicator, distinct, policy) == "distinct_model_lineage"
    question = DecisionQuestion(semantic_question_hash="q", selection_universe_hash="u")
    agreement, blind = _agreement(adjudicator, distinct, question=question)
    assert promote(
        adjudicator_route=adjudicator,
        checker_route=distinct,
        agreement_records=(agreement,),
        decision_kind="identity",
        decision_question=question,
        canonical_signature_hash_value="signature-1",
        proposal_record_id_value="proposal-1",
        adjudicator_response_artifact_hash="response-a",
        authority_policy=policy,
        qualification_manifests={},
        blinding_records=blind,
        persisted_agreement_hashes=frozenset({content_address(agreement)}),
    ) == "corroborated"
    with pytest.raises(AuthorityError, match="roles"):
        classify_independence(same, distinct, policy)


def test_probe_09_cross_kind_qualification_cannot_activate() -> None:
    policy = _policy()
    model = _model("family-b", "checker")
    frame = _qualification(model, "frame", policy)
    identity = _qualification(model, "identity", policy)
    adjudicator = _route("adj", "adjudicator", _model("family-a", "adj"))
    checker_frame = _route("check", "checker", model, (frame,))
    question = DecisionQuestion(semantic_question_hash="q", selection_universe_hash="u")
    agreement, blind = _agreement(adjudicator, checker_frame, question=question)
    assert promote(
        adjudicator_route=adjudicator,
        checker_route=checker_frame,
        agreement_records=(agreement,),
        decision_kind="identity",
        decision_question=question,
        canonical_signature_hash_value="signature-1",
        proposal_record_id_value="proposal-1",
        adjudicator_response_artifact_hash="response-a",
        authority_policy=policy,
        qualification_manifests={frame.qualification_manifest_hash: frame},
        blinding_records=blind,
        persisted_agreement_hashes=frozenset({content_address(agreement)}),
    ) == "corroborated"
    checker_identity = _route("check", "checker", model, (identity,))
    assert promote(
        adjudicator_route=adjudicator,
        checker_route=checker_identity,
        agreement_records=(agreement,),
        decision_kind="identity",
        decision_question=question,
        canonical_signature_hash_value="signature-1",
        proposal_record_id_value="proposal-1",
        adjudicator_response_artifact_hash="response-a",
        authority_policy=policy,
        qualification_manifests={identity.qualification_manifest_hash: identity},
        blinding_records=blind,
        persisted_agreement_hashes=frozenset({content_address(agreement)}),
    ) == "active"


def test_probe_10_manifest_binding_and_forced_result_fail() -> None:
    policy = _policy()
    model_a = _model("family-a", "a")
    model_b = _model("family-b", "b")
    manifest_a = _qualification(model_a, "identity", policy)
    validate_qualification_manifest(manifest_a, policy=policy)
    route_b = _route("b", "checker", model_b, (manifest_a,))
    with pytest.raises(AuthorityError, match="model identity"):
        qualified_for(
            route_b,
            {manifest_a.qualification_manifest_hash: manifest_a},
            policy=policy,
        )
    false_metrics = _qualification(model_a, "identity", policy, passed=False)
    forged = cast(
        QualificationManifest,
        _seal(replace(false_metrics, qualified_result=True, qualification_manifest_hash="")),
    )
    with pytest.raises(AuthorityError, match="contradicts"):
        validate_qualification_manifest(forged, policy=policy)
    changed_policy = cast(
        AuthorityIndependencePolicy,
        _seal(
            AuthorityIndependencePolicy(
                policy_version="v2", qualification_policy_hash="other"
            )
        ),
    )
    with pytest.raises(AuthorityError, match="policy mismatch"):
        validate_qualification_manifest(manifest_a, policy=changed_policy)


def test_probe_11_multiple_kinds_and_duplicate_binding() -> None:
    policy = _policy()
    model = _model("family-b", "b")
    frame = _qualification(model, "frame", policy)
    identity = _qualification(model, "identity", policy)
    route = _route("checker", "checker", model, (frame, identity))
    manifests = {
        frame.qualification_manifest_hash: frame,
        identity.qualification_manifest_hash: identity,
    }
    assert qualified_for(route, manifests, policy=policy) == frozenset({"frame", "identity"})
    duplicate = replace(
        route,
        qualification_bindings=route.qualification_bindings
        + (route.qualification_bindings[0],),
    )
    with pytest.raises(AuthorityError, match="duplicate"):
        qualified_for(duplicate, manifests, policy=policy)


def test_probe_12_human_route_exact_record_only() -> None:
    policy = _policy()
    question = DecisionQuestion(semantic_question_hash="q", selection_universe_hash="u")
    record = HumanDecisionRecord(
        reviewer_id="reviewer",
        recorded_at_audit="2026-07-13T00:00:00Z",
        decision_kind="identity",
        decision_question_hash=question.canonical_hash(),
        canonical_signature_hash="signature-1",
        verdict="approve",
        evidence_refs=frozenset({"b1"}),
        entity_or_item_revision_hash="revision-1",
    )
    record_hash = content_address(record)
    route = HumanAuthorityRoute(
        authority_route_id="human", human_decision_record_hash=record_hash
    )
    adjudicator = _route("adj", "adjudicator", _model("family-a", "a"))
    assert promote(
        adjudicator_route=adjudicator,
        checker_route=route,
        agreement_records=(),
        decision_kind="identity",
        decision_question=question,
        canonical_signature_hash_value="signature-1",
        proposal_record_id_value="proposal-1",
        adjudicator_response_artifact_hash="response-a",
        authority_policy=policy,
        qualification_manifests={},
        blinding_records={},
        human_records={record_hash: record},
        entity_or_item_revision_hash="revision-1",
    ) == "active"
    with pytest.raises(AuthorityError, match="question mismatch"):
        promote(
            adjudicator_route=adjudicator,
            checker_route=route,
            agreement_records=(),
            decision_kind="identity",
            decision_question=DecisionQuestion(
                semantic_question_hash="other", selection_universe_hash="u"
            ),
            canonical_signature_hash_value="signature-1",
            proposal_record_id_value="proposal-1",
            adjudicator_response_artifact_hash="response-a",
            authority_policy=policy,
            qualification_manifests={},
            blinding_records={},
            human_records={record_hash: record},
            entity_or_item_revision_hash="revision-1",
        )
    with pytest.raises(AuthorityError, match="revision mismatch"):
        promote(
            adjudicator_route=adjudicator,
            checker_route=route,
            agreement_records=(),
            decision_kind="identity",
            decision_question=question,
            canonical_signature_hash_value="signature-1",
            proposal_record_id_value="proposal-1",
            adjudicator_response_artifact_hash="response-a",
            authority_policy=policy,
            qualification_manifests={},
            blinding_records={},
            human_records={record_hash: record},
            entity_or_item_revision_hash="revision-2",
        )


def test_probe_13_agreement_forgery_cannot_promote() -> None:
    policy = _policy()
    model = _model("family-b", "b")
    manifest = _qualification(model, "identity", policy)
    adjudicator = _route("adj", "adjudicator", _model("family-a", "a"))
    checker = _route("check", "checker", model, (manifest,))
    question = DecisionQuestion(semantic_question_hash="q", selection_universe_hash="u")
    agreement, blind = _agreement(adjudicator, checker, question=question)
    good = dict(
        adjudicator_route=adjudicator,
        checker_route=checker,
        agreement_records=(agreement,),
        decision_kind="identity",
        decision_question=question,
        canonical_signature_hash_value="signature-1",
        proposal_record_id_value="proposal-1",
        adjudicator_response_artifact_hash="response-a",
        authority_policy=policy,
        qualification_manifests={manifest.qualification_manifest_hash: manifest},
        blinding_records=blind,
        persisted_agreement_hashes=frozenset({content_address(agreement)}),
    )
    assert promote(**good) == "active"
    with pytest.raises(AuthorityError, match="not persisted"):
        promote(**(good | {"persisted_agreement_hashes": frozenset()}))
    base_handles = allocate_candidate_handles(("entity-a", "entity-b", "entity-c"))
    checker_a = shuffle_candidate_handles(base_handles, checker_fingerprint="checker-a")
    checker_b = shuffle_candidate_handles(base_handles, checker_fingerprint="checker-b")
    assert dict(checker_a) != dict(checker_b)
    signature = ExistingCandidateSignature(
        candidate_handle=checker_a[0][0],
        covered_occurrence_ids=frozenset({"o1"}),
        referent_kind="person",
    )
    assert canonical_signature_hash(signature, handle_map=dict(checker_a))
    for forged in (
        replace(agreement, canonical_signature_hash_b="other"),
        replace(agreement, request_fingerprint_b="request-a"),
        replace(agreement, checker_route_id="foreign"),
    ):
        with pytest.raises(AuthorityError):
            promote(**(good | {"agreement_records": (forged,)}))
    with pytest.raises(AuthorityError, match="proposal"):
        promote(**(good | {"proposal_record_id_value": "other-proposal"}))
    with pytest.raises(AuthorityError, match="artifact"):
        promote(**(good | {"adjudicator_response_artifact_hash": "other-response"}))
    with pytest.raises(AuthorityError, match="lacks a content-addressed"):
        promote(**(good | {"blinding_records": {}}))


def test_probe_14_qualification_staleness_emits_invalidation() -> None:
    support = (SupportSet(support_set_id="s", member_ids=frozenset({"e"})),)
    decision = DecisionRevision(
        decision_id="decision-1",
        proposal_record_id="proposal-1",
        decision_kind="identity",
        state="active",
        question_hash="q",
        canonical_signature_hash="sig",
        support_sets=support,
        decided_at_scope="bk_ch01",
        qualification_manifest_hashes=frozenset({"manifest-old"}),
        authority_evidence_record_hashes=frozenset({"agreement"}),
        authority_policy_hash="policy",
        validator_contract_hash="validator",
    )
    assert stale_qualification_decisions(
        (decision,),
        current_binding_manifest_hashes={"decision-1": frozenset({"manifest-old"})},
    ) == ()
    assert stale_qualification_decisions(
        (decision,),
        current_binding_manifest_hashes={"decision-1": frozenset({"manifest-new"})},
    ) == ("decision-1",)


def test_probe_15_safety_seed_shape_is_quarantine_only() -> None:
    record = QuarantineProposedRecord(
        record_id="q1",
        proposal_record_id="p1",
        seed_occurrence_ids=frozenset({"o1"}),
        evidence_refs=frozenset({"b1"}),
        decided_at_scope="bk_ch01",
    )
    draft = SafetySeedChangeSet(
        state_lineage_id=LINEAGE,
        source_scope_id="bk_ch01",
        parent_generation_hash=None,
        bundle_manifest_hash="bundle",
        validator_contract_hash="validator",
        quarantine_records=(record,),
        materialized_view_hash="view",
        estimated_apply_cost=1,
    )
    sealed = cast(SafetySeedChangeSet, _seal(draft))
    payload = sealed.to_canonical_payload() | {"changeset_hash": sealed.changeset_hash}
    validate_safety_seed_shape(payload)
    with pytest.raises(StoreError, match="forbidden"):
        validate_safety_seed_shape(payload | {"decision_revisions": []})
    with pytest.raises(StoreError, match="non-local"):
        validate_safety_seed_shape(
            payload | {"quarantine_records": [{"state": "active"}]}
        )
    smuggled = dict(payload)
    smuggled["quarantine_records"] = [
        sealed.quarantine_records[0].to_canonical_payload() | {"overlay_ref": "bad"}
    ]
    with pytest.raises(StoreError, match="non-local"):
        validate_safety_seed_shape(smuggled)


def test_probe_16_stale_parent_never_switches_or_rebases(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    first = _publish_initial(store)
    child = _prepare_generation(store, parent=first.generation_hash, suffix="child")
    with pytest.raises(StaleParentError):
        store.cas_switch(
            state_lineage_id=LINEAGE,
            expected_current="foreign-parent",
            new_generation_hash=child.generation_hash,
        )
    assert store.current_generation_hash(LINEAGE) == first.generation_hash


def test_probe_17_two_processes_one_lineage_one_winner(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    first = _publish_initial(store)
    left = _prepare_generation(store, parent=first.generation_hash, suffix="left")
    right = _prepare_generation(store, parent=first.generation_hash, suffix="right")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_cas_worker,
            args=(
                str(store.root),
                LINEAGE,
                first.generation_hash,
                generation.generation_hash,
                barrier,
                queue,
            ),
        )
        for generation in (left, right)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = sorted(queue.get(timeout=2) for _ in processes)
    assert outcomes == ["stale", "won"]
    assert store.current_generation_hash(LINEAGE) in {
        left.generation_hash,
        right.generation_hash,
    }


def test_probe_18_content_write_idempotent_only_for_same_bytes(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    first = AuditRecord(kind="request", payload_hash="payload", operational_timestamp="t1")
    sealed = cast(AuditRecord, _seal(first))
    store.put_audit(sealed)
    store.put_audit(sealed)
    with pytest.raises(StoreError, match="different bytes"):
        store.put_audit(replace(sealed, operational_timestamp="t2"))


def test_probe_19_fast_path_is_closed_tagged_union() -> None:
    assert fast_path_allowed(
        RetainUnchangedRow(row_id="row", active_revision_hash="active")
    )
    assert fast_path_allowed(
        BindOccurrenceActive(
            occurrence_id="occ", active_binding_revision_hash="active"
        )
    )
    assert fast_path_allowed(
        RemapReferenceActive(
            reference_id="ref",
            source_ref="a",
            target_ref="b",
            active_witness_revision_hash="active",
        )
    )
    with pytest.raises(AuthorityError):
        fast_path_allowed("retain")
    with pytest.raises(AuthorityError, match="collision"):
        fast_path_allowed(
            RemapReferenceActive(
                reference_id="ref",
                source_ref="a",
                target_ref="b",
                active_witness_revision_hash="active",
                collision=True,
            )
        )


def _entry(
    entry_id: str,
    *,
    cost_class: str,
    bucket: str | None,
    model: str,
    prompt: int,
    output: int,
    retry: int = 0,
) -> CallPlanEntry:
    return CallPlanEntry(
        call_plan_entry_id=entry_id,
        call_kind="R",
        decision_kind="identity",
        shard_id=None,
        authority_route_id="route",
        execution_cost_class=cost_class,  # type: ignore[arg-type]
        quota_bucket_id=bucket,
        model_id=model,
        prompt_tokens_estimate=prompt,
        max_output_tokens=output,
        technical_retry_cap=retry,  # type: ignore[arg-type]
    )


def _empty_plan():
    return seal_call_plan(entries=(), finite_caps=CAPS)


def test_probe_20_remote_quota_counts_prompt_completion_retry_per_bucket() -> None:
    plan = seal_call_plan(
        entries=(
            _entry(
                "remote-a",
                cost_class="remote_quota",
                bucket="key-1",
                model="model-a",
                prompt=10_000,
                output=5_000,
                retry=1,
            ),
        ),
        finite_caps=CAPS,
    )
    usage = (
        UsageSnapshot(
            quota_bucket_id="key-1",
            model_id="model-a",
            utc_day="2026-07-13",
            prompt_plus_completion_used=195_000,
        ),
    )
    assert preflight_gate(
        utc_day="2026-07-13",
        usage=usage,
        deterministic_base=plan,
        contingent_reserve=_empty_plan(),
    ).passed
    with pytest.raises(BudgetExceededError):
        preflight_gate(
            utc_day="2026-07-13",
            usage=(replace(usage[0], prompt_plus_completion_used=195_001),),
            deterministic_base=plan,
            contingent_reserve=_empty_plan(),
        )


def test_probe_21_local_checker_has_no_remote_bucket_or_quota() -> None:
    local = seal_call_plan(
        entries=(
            _entry(
                "local",
                cost_class="local_compute",
                bucket=None,
                model="local-family",
                prompt=500_000,
                output=500_000,
            ),
        ),
        finite_caps=CAPS,
    )
    report = preflight_gate(
        utc_day="2026-07-13",
        usage=(),
        deterministic_base=local,
        contingent_reserve=_empty_plan(),
    )
    assert report.remote_totals == {}
    assert report.local_compute_totals == {"local-family": 1_000_000}
    with pytest.raises(BudgetContractError, match="cannot carry"):
        _entry(
            "bad-local",
            cost_class="local_compute",
            bucket="fake-key",
            model="local",
            prompt=1,
            output=1,
        )


def test_probe_22_preapply_budget_failure_leaves_pointer_and_accounting_separate(tmp_path: Path) -> None:
    store = Step5Store(tmp_path / "store")
    first = _publish_initial(store)
    actual = seal_call_plan(
        entries=(
            _entry(
                "actual",
                cost_class="remote_quota",
                bucket="key-1",
                model="model-a",
                prompt=225_000,
                output=1,
            ),
        ),
        finite_caps=CAPS,
    )
    with pytest.raises(BudgetExceededError):
        pre_apply_gate(
            utc_day="2026-07-13",
            usage=(),
            deterministic_base=actual,
            contingent_reserve=_empty_plan(),
        )
    assert store.current_generation_hash(LINEAGE) == first.generation_hash
    assert AccountingTotals(
        restored_tokens=10,
        cache_tokens=20,
        this_attempt_tokens=30,
        combined_tokens=60,
    ).combined_tokens == 60
    with pytest.raises(BudgetContractError, match="combined"):
        AccountingTotals(
            restored_tokens=10,
            cache_tokens=20,
            this_attempt_tokens=30,
            combined_tokens=59,
        )


def _oracle_fixture():
    groups = (
        OracleGroup(
            oracle_group_id="g-qual", member_row_ids=frozenset({"r1"}), group_key="case-a"
        ),
        OracleGroup(
            oracle_group_id="g-dev", member_row_ids=frozenset({"r2"}), group_key="case-b"
        ),
        OracleGroup(
            oracle_group_id="g-held", member_row_ids=frozenset({"r3"}), group_key="case-c"
        ),
    )
    rows = ({"row_id": "r3", "answer": "sealed"},)
    return groups, rows


def test_probe_23_oracle_group_isolation_and_shared_key_reject() -> None:
    groups, rows = _oracle_fixture()
    split, _sealed = preregister_oracle_split(
        groups=groups,
        qualify_group_ids=frozenset({"g-qual"}),
        dev_eval_group_ids=frozenset({"g-dev"}),
        held_out_group_ids=frozenset({"g-held"}),
        held_out_rows=rows,
    )
    assert set(public_split_manifest(split)) == {
        "qualify_groups",
        "dev_eval_groups",
        "held_out_commitment_hash",
        "split_manifest_hash",
    }
    overlapping = replace(groups[1], group_key="case-a")
    with pytest.raises(PreregisterError, match="straddles"):
        preregister_oracle_split(
            groups=(groups[0], overlapping, groups[2]),
            qualify_group_ids=frozenset({"g-qual"}),
            dev_eval_group_ids=frozenset({"g-dev"}),
            held_out_group_ids=frozenset({"g-held"}),
            held_out_rows=rows,
        )
    with pytest.raises(PreregisterError, match="overlap"):
        preregister_oracle_split(
            groups=groups,
            qualify_group_ids=frozenset({"g-qual", "g-dev"}),
            dev_eval_group_ids=frozenset({"g-dev"}),
            held_out_group_ids=frozenset({"g-held"}),
            held_out_rows=rows,
        )


def test_probe_24_heldout_vault_capability_and_commitment(tmp_path: Path) -> None:
    groups, rows = _oracle_fixture()
    split, sealed = preregister_oracle_split(
        groups=groups,
        qualify_group_ids=frozenset({"g-qual"}),
        dev_eval_group_ids=frozenset({"g-dev"}),
        held_out_group_ids=frozenset({"g-held"}),
        held_out_rows=rows,
    )
    vault = HeldOutVault(tmp_path / "vault")
    vault.seal(sealed)
    capability = vault.create_gate_capability()
    assert vault.open(split.held_out_commitment_hash, capability=capability) == rows
    with pytest.raises((HeldOutAccessError, BoundaryError)):
        vault.open(
            split.held_out_commitment_hash,
            capability=HeldOutGateCapability("forged"),
        )
    path = tmp_path / "vault" / f"{split.held_out_commitment_hash}.json"
    path.write_text('[{"row_id":"r3","answer":"tampered"}]', encoding="utf-8")
    with pytest.raises(StoreError, match="commitment"):
        vault.open(split.held_out_commitment_hash, capability=capability)


def test_probe_25_blinding_is_computed_from_section_membership() -> None:
    policy = _policy()
    checker_model = _model("family-b", "checker")
    qualification = _qualification(checker_model, "identity", policy)
    adjudicator = _route("adj", "adjudicator", _model("family-a", "adj"))
    checker = _route("check", "checker", checker_model, (qualification,))
    question = DecisionQuestion(semantic_question_hash="q", selection_universe_hash="u")
    agreement, clean_records = _agreement(adjudicator, checker, question=question)
    clean_record = next(iter(clean_records.values()))
    assert validate_blinding(clean_record) == "valid"
    with pytest.raises(TypeError, match="status"):
        BlindingValidationRecord(
            checker_request_fingerprint="request-b",
            checker_input_section_manifest_hash=(
                clean_record.checker_input_section_manifest_hash
            ),
            checker_input_section_manifest=(
                clean_record.checker_input_section_manifest
            ),
            adjudicator_response_artifact_hash="response-a",
            validator_contract_hash="validator-1",
            status="valid",  # type: ignore[call-arg]
        )

    contaminated_manifest = build_checker_input_section_manifest(
        checker_request_fingerprint="request-b",
        checker_input_sections=(
            build_checker_input_section(
                section_id="checker-context",
                rendered_content="Checker context contaminated by an upstream response.",
                source_artifact_hashes=frozenset(
                    {"bundle-artifact", "response-a"}
                ),
            ),
        ),
    )
    contaminated_record = BlindingValidationRecord(
        checker_request_fingerprint="request-b",
        checker_input_section_manifest_hash=(
            contaminated_manifest.checker_input_section_manifest_hash
        ),
        checker_input_section_manifest=contaminated_manifest,
        adjudicator_response_artifact_hash="response-a",
        validator_contract_hash="validator-1",
    )
    assert validate_blinding(contaminated_record) == "invalid"
    contaminated_hash = content_address(contaminated_record)
    contaminated_agreement = replace(
        agreement,
        blinding_validation_record_hash=contaminated_hash,
    )
    with pytest.raises(AuthorityError, match="blinding validation is invalid"):
        promote(
            adjudicator_route=adjudicator,
            checker_route=checker,
            agreement_records=(contaminated_agreement,),
            decision_kind="identity",
            decision_question=question,
            canonical_signature_hash_value="signature-1",
            proposal_record_id_value="proposal-1",
            adjudicator_response_artifact_hash="response-a",
            authority_policy=policy,
            qualification_manifests={
                qualification.qualification_manifest_hash: qualification
            },
            blinding_records={contaminated_hash: contaminated_record},
            persisted_agreement_hashes=frozenset(
                {content_address(contaminated_agreement)}
            ),
        )

    direct_manifest = build_checker_input_section_manifest(
        checker_request_fingerprint="request-b",
        checker_input_sections=(
            CheckerInputSection(
                section_id="direct-response",
                section_content_hash="response-a",
                source_artifact_hashes=frozenset(),
            ),
        ),
    )
    direct_record = replace(
        clean_record,
        checker_input_section_manifest_hash=(
            direct_manifest.checker_input_section_manifest_hash
        ),
        checker_input_section_manifest=direct_manifest,
    )
    assert validate_blinding(direct_record) == "invalid"
    with pytest.raises(AuthorityError, match="manifest hash mismatch"):
        validate_blinding(
            replace(clean_record, checker_input_section_manifest_hash="tampered")
        )
