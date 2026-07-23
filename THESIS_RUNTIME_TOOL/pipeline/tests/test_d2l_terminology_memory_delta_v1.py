from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
)
from pipeline.prepass.d2l_terminology_memory_delta_v1 import (
    D2LTerminologyDeltaError,
    commit_glossary_draft,
    validate_commit_receipt,
    validate_memory_delta_batch,
    validate_sealed_glossary,
)


def _entry(
    entry_id: str,
    source: str,
    target: str,
    *,
    evidence: list[str] | None = None,
    candidates: list[str] | None = None,
) -> dict[str, object]:
    return {
        "entry_id": entry_id,
        "canonical_source": source,
        "canonical_target_vi": target,
        "alternative_targets": [],
        "surfaces": [source],
        "chapter_id": "d2l_multilayer_perceptrons",
        "status": "ready_draft",
        "directive": "translate",
        "canonical_applicability": None,
        "evidence_block_ids": evidence or [f"block_{entry_id}"],
        "evidence_complete": True,
        "source_member_candidate_ids": candidates or [f"candidate_{entry_id}"],
        "decision_rationale": "Stable technical term.",
        "pending_target_proposals": [],
        "rejected_target_proposals": [],
        "resolution": {
            "authority_kind": "deterministic_single_target",
            "authority_sha256": "A" * 64,
            "packet_id": None,
            "auditor_rationale": None,
            "auditor_cited_evidence_block_ids": [],
            "pending_reason": None,
        },
        "source_lineage": {
            "authority_kind": "source_b2_singleton",
            "authority_hash": "B" * 64,
            "source_index_sha256": "C" * 64,
            "source_member_candidate_ids": candidates or [f"candidate_{entry_id}"],
        },
    }


def _draft(entries: list[dict[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {
        "draft_version": "d2l_b2_glossary_draft_v2",
        "chapter_ids": ["d2l_multilayer_perceptrons"],
        "source_index_sha256": "C" * 64,
        "source_multi_target_draft_sha256": "D" * 64,
        "source_multi_target_plan_sha256": "E" * 64,
        "source_run_manifest_sha256": "F" * 64,
        "source_stage2_plan_sha256": "1" * 64,
        "ready_entries": entries,
        "pending_entries": [],
        "production_published": False,
        "counts": {
            "ready_entries": len(entries),
            "pending_entries": 0,
            "admitted_exact_cover": len(entries),
            "deterministic_single_target_entries": len(entries),
            "multi_target_audited_entries": 0,
            "current_admitted_entries": len(entries),
        },
    }
    value["draft_sha256"] = canonical_sha256(value)
    return value


def _commit(
    root: Path,
    draft: dict[str, object],
    *,
    previous: dict[str, object] | None = None,
    attempt: int = 1,
) -> dict[str, object]:
    return commit_glossary_draft(
        draft=draft,
        output_root=root,
        workflow_run_id="workflow_test",
        component_run_id="translation_test",
        component_attempt_id=attempt,
        stage_id="glossary_seal",
        source_refs=["artifact:glossary_draft"],
        created_at=f"2026-07-22T00:00:0{attempt}.000Z",
        previous_glossary=previous,
    )


def test_initial_commit_emits_only_committed_added_deltas(tmp_path: Path) -> None:
    result = _commit(
        tmp_path / "generation_1",
        _draft(
            [
                _entry("term_gradient", "gradient", "gradient"),
                _entry("term_example", "example", "vi du"),
            ]
        ),
    )

    glossary = validate_sealed_glossary(result["sealed_glossary"])
    batch = validate_memory_delta_batch(result["memory_delta_batch"])
    receipt = validate_commit_receipt(result["commit_receipt"])

    assert glossary["state_generation"] == 1
    assert {row["lifecycle"] for row in glossary["records"]} == {"committed"}
    assert batch["counts"] == {"added": 2, "reinforced": 0, "revised": 0, "total": 2}
    assert {row["operation"] for row in batch["deltas"]} == {"added"}
    assert receipt["committed_state_sha256"] == glossary["state_sha256"]
    assert receipt["memory_delta_batch_sha256"] == batch["batch_sha256"]
    manifest = result["artifact_manifest"]
    for row in manifest["artifacts"]:
        assert file_sha256(tmp_path / "generation_1" / row["relative_path"]) == row["sha256"]


def test_next_commit_classifies_added_reinforced_revised_and_unchanged(
    tmp_path: Path,
) -> None:
    initial_entries = [
        _entry("term_unchanged", "activation", "kich hoat"),
        _entry("term_reinforced", "gradient", "gradient"),
        _entry("term_revised", "example", "mau"),
    ]
    first = _commit(tmp_path / "generation_1", _draft(initial_entries))
    next_entries = copy.deepcopy(initial_entries)
    next_entries[1]["evidence_block_ids"].append("block_gradient_second")
    next_entries[1]["source_member_candidate_ids"].append("candidate_gradient_second")
    next_entries[2]["canonical_target_vi"] = "vi du"
    next_entries.append(_entry("term_new", "absolute error", "sai so tuyet doi"))

    second = _commit(
        tmp_path / "generation_2",
        _draft(next_entries),
        previous=first["sealed_glossary"],
        attempt=2,
    )

    batch = validate_memory_delta_batch(second["memory_delta_batch"])
    assert batch["counts"] == {"added": 1, "reinforced": 1, "revised": 1, "total": 3}
    by_id = {row["record_id"]: row for row in batch["deltas"]}
    assert "term_unchanged" not in by_id
    assert by_id["term_reinforced"]["operation"] == "reinforced"
    assert by_id["term_revised"]["operation"] == "revised"
    assert by_id["term_new"]["operation"] == "added"
    records = {row["record_id"]: row for row in second["sealed_glossary"]["records"]}
    assert records["term_unchanged"]["revision"] == 1
    assert records["term_reinforced"]["revision"] == 2
    assert records["term_revised"]["revision"] == 2
    assert records["term_new"]["revision"] == 1


def test_commit_is_idempotent_but_rejects_changed_existing_artifact(tmp_path: Path) -> None:
    root = tmp_path / "generation_1"
    draft = _draft([_entry("term_gradient", "gradient", "gradient")])
    first = _commit(root, draft)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    second = _commit(root, draft)

    assert second == first
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before

    (root / "memory_delta_v1.json").write_text("{}", encoding="utf-8")
    with pytest.raises(D2LTerminologyDeltaError, match="immutable artifact already differs"):
        _commit(root, draft)


def test_commit_rejects_removed_record_or_evidence(tmp_path: Path) -> None:
    entries = [
        _entry(
            "term_gradient",
            "gradient",
            "gradient",
            evidence=["block_one", "block_two"],
            candidates=["candidate_one", "candidate_two"],
        ),
        _entry("term_example", "example", "vi du"),
    ]
    first = _commit(tmp_path / "generation_1", _draft(entries))

    with pytest.raises(D2LTerminologyDeltaError, match="cannot silently remove"):
        _commit(
            tmp_path / "removed_record",
            _draft(entries[:1]),
            previous=first["sealed_glossary"],
            attempt=2,
        )

    evidence_removed = copy.deepcopy(entries)
    evidence_removed[0]["evidence_block_ids"] = ["block_one"]
    with pytest.raises(D2LTerminologyDeltaError, match="evidence cannot be silently removed"):
        _commit(
            tmp_path / "removed_evidence",
            _draft(evidence_removed),
            previous=first["sealed_glossary"],
            attempt=2,
        )


def test_draft_hash_counts_duplicates_and_forbidden_payload_fail_closed(
    tmp_path: Path,
) -> None:
    entry = _entry("term_gradient", "gradient", "gradient")
    drifted = _draft([entry])
    drifted["chapter_ids"] = ["foreign_chapter"]
    with pytest.raises(D2LTerminologyDeltaError, match="hash drift"):
        _commit(tmp_path / "hash_drift", drifted)

    duplicate = _draft([entry, copy.deepcopy(entry)])
    with pytest.raises(D2LTerminologyDeltaError, match="entry_id values must be unique"):
        _commit(tmp_path / "duplicate", duplicate)

    forbidden = _draft([entry])
    forbidden["raw_prompt"] = "do not persist"
    forbidden.pop("draft_sha256")
    forbidden["draft_sha256"] = canonical_sha256(forbidden)
    with pytest.raises(D2LTerminologyDeltaError, match="forbidden key"):
        _commit(tmp_path / "forbidden", forbidden)


def test_tampered_committed_artifacts_fail_local_validation(tmp_path: Path) -> None:
    result = _commit(
        tmp_path / "generation_1",
        _draft([_entry("term_gradient", "gradient", "gradient")]),
    )

    glossary = copy.deepcopy(result["sealed_glossary"])
    glossary["records"][0]["value"]["canonical_target_vi"] = "changed"
    with pytest.raises(D2LTerminologyDeltaError, match="record hash drift"):
        validate_sealed_glossary(glossary)

    batch = copy.deepcopy(result["memory_delta_batch"])
    batch["counts"]["total"] = 0
    with pytest.raises(D2LTerminologyDeltaError, match="counts mismatch"):
        validate_memory_delta_batch(batch)

    receipt = copy.deepcopy(result["commit_receipt"])
    receipt["stage_id"] = "foreign_stage"
    with pytest.raises(D2LTerminologyDeltaError, match="receipt hash drift"):
        validate_commit_receipt(receipt)

    assert json.loads((tmp_path / "generation_1" / "sealed_glossary.json").read_text())
    assert (tmp_path / "generation_1" / "sealed_glossary.json").read_bytes() == canonical_json_bytes(
        result["sealed_glossary"]
    )


def test_previous_state_must_share_run_lineage_and_not_come_from_future_attempt(
    tmp_path: Path,
) -> None:
    draft = _draft([_entry("term_gradient", "gradient", "gradient")])
    first = _commit(tmp_path / "generation_1", draft, attempt=2)

    with pytest.raises(D2LTerminologyDeltaError, match="future component attempt"):
        _commit(
            tmp_path / "future_attempt",
            draft,
            previous=first["sealed_glossary"],
            attempt=1,
        )

    foreign = copy.deepcopy(first["sealed_glossary"])
    foreign["workflow_run_id"] = "foreign_workflow"
    unsigned = dict(foreign)
    unsigned.pop("state_sha256")
    foreign["state_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(D2LTerminologyDeltaError, match="another workflow"):
        _commit(
            tmp_path / "foreign_workflow",
            draft,
            previous=foreign,
            attempt=2,
        )
