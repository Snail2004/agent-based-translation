from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256
from pipeline.prepass.d2l_stage_work_journal_v1 import (
    D2LStageWorkJournal,
    D2LStageWorkJournalError,
    read_work_journal,
    work_journal_state,
)


def _journal(
    path: Path,
    *,
    attempt: int,
) -> D2LStageWorkJournal:
    return D2LStageWorkJournal(
        path=path,
        workflow_run_id="wf_resume",
        component_run_id="tr_resume",
        component_attempt_id=attempt,
        stage_id="b1_candidate_discovery",
    )


def test_work_result_is_reused_across_component_attempts(tmp_path: Path) -> None:
    path = tmp_path / "b1.jsonl"
    input_sha = canonical_sha256({"messages": ["source"]})
    result = {"window_id": "w1", "candidate_observations": []}
    first = _journal(path, attempt=1)
    entry = first.append(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=input_sha,
        result=result,
    )

    resumed = _journal(path, attempt=2)
    assert resumed.lookup(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=input_sha,
    ) == result
    assert entry["component_attempt_id"] == 1
    assert work_journal_state(resumed.entries) == {
        "entry_count": 1,
        "last_entry_sha256": entry["entry_sha256"],
    }


def test_work_item_identity_drift_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "b1.jsonl"
    journal = _journal(path, attempt=1)
    journal.append(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=canonical_sha256({"messages": ["source"]}),
        result={"window_id": "w1"},
    )

    with pytest.raises(
        D2LStageWorkJournalError,
        match="input or semantic contract drift",
    ):
        _journal(path, attempt=2).lookup(
            work_item_id="w1",
            work_contract_id="candidate_v1",
            input_sha256=canonical_sha256({"messages": ["changed"]}),
        )


def test_duplicate_append_is_idempotent_but_conflict_rejects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "b1.jsonl"
    journal = _journal(path, attempt=1)
    input_sha = canonical_sha256({"messages": ["source"]})
    first = journal.append(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=input_sha,
        result={"window_id": "w1"},
    )
    assert journal.append(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=input_sha,
        result={"window_id": "w1"},
    ) == first

    with pytest.raises(
        D2LStageWorkJournalError,
        match="different result",
    ):
        journal.append(
            work_item_id="w1",
            work_contract_id="candidate_v1",
            input_sha256=input_sha,
            result={"window_id": "w1", "changed": True},
        )


def test_hash_tamper_and_unterminated_tail_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "b1.jsonl"
    journal = _journal(path, attempt=1)
    journal.append(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=canonical_sha256({"messages": ["source"]}),
        result={"window_id": "w1"},
    )
    row = json.loads(path.read_text(encoding="utf-8"))
    row["result"]["window_id"] = "tampered"
    path.write_text(
        json.dumps(row, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(D2LStageWorkJournalError, match="result hash drift"):
        read_work_journal(path)

    path.write_text('{"partial":', encoding="utf-8", newline="")
    with pytest.raises(
        D2LStageWorkJournalError,
        match="unterminated final row",
    ):
        read_work_journal(path)
    assert read_work_journal(path, allow_incomplete_tail=True) == []


def test_foreign_component_or_stage_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "b1.jsonl"
    _journal(path, attempt=1).append(
        work_item_id="w1",
        work_contract_id="candidate_v1",
        input_sha256=canonical_sha256({"messages": ["source"]}),
        result={"window_id": "w1"},
    )

    with pytest.raises(
        D2LStageWorkJournalError,
        match="foreign run or stage",
    ):
        D2LStageWorkJournal(
            path=path,
            workflow_run_id="wf_resume",
            component_run_id="other_component",
            component_attempt_id=2,
            stage_id="b1_candidate_discovery",
        )
