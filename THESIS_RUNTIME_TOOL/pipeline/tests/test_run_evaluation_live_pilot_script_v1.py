from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from pipeline.scripts.run_evaluation_live_pilot_v1 import (
    _parser,
    _require_current_checkout_commit,
    _require_source_credential_binding,
)


def test_runner_accepts_only_explicit_required_or_prompt_validated_modes() -> None:
    parser = _parser()
    action = next(
        row for row in parser._actions if row.dest == "structured_output_mode"
    )
    assert action.choices == ("prompt_validated", "required")


def test_runner_requires_exact_capability_credential_reference() -> None:
    source = {"credential_ref": "shared.ckey.account-v1"}
    _require_source_credential_binding(
        source,
        expected_credential_ref="shared.ckey.account-v1",
    )
    with pytest.raises(ValueError, match="capability source binding"):
        _require_source_credential_binding(
            source,
            expected_credential_ref="shared.google.gemini_free.row1",
        )


def test_runner_requires_exact_current_git_head_before_execution() -> None:
    runtime_root = Path(__file__).resolve().parents[2]
    current = subprocess.run(
        ["git", "-C", str(runtime_root.parent), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    ).stdout.strip()

    assert _require_current_checkout_commit(
        runtime_root.parent,
        expected_commit=current,
    ) == current
    with pytest.raises(ValueError, match="differs from current Git HEAD"):
        _require_current_checkout_commit(
            runtime_root.parent,
            expected_commit="0" * 40,
        )
    with pytest.raises(ValueError, match="full lowercase Git SHA"):
        _require_current_checkout_commit(
            runtime_root.parent,
            expected_commit=current[:7],
        )
