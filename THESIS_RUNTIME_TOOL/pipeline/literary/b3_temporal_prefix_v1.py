"""Content-addressed cross-chapter prefix for Literary B3 temporal memory."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, file_sha256


B3_TEMPORAL_PREFIX_SCHEMA_VERSION_V1 = "literary_b3_temporal_prefix_v1"
B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1 = (
    "literary_b3_temporal_chapter_artifact_v1"
)
B3_MODEL_HIDDEN_STATE_FIELDS_V1 = frozenset({"consolidated_state_ids"})
_BATCH_ARTIFACT_SCHEMA = "literary_b3_temporal_artifact_v1"


class B3TemporalPrefixError(RuntimeError):
    pass


def build_b3_temporal_prefix_v1(
    prior_chapter_roots: Sequence[Path],
) -> dict[str, Any]:
    """Fold authoritative projections while retaining pending rows separately."""

    effective: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    resolved: dict[str, dict[str, Any]] = {}
    source_rows: list[dict[str, Any]] = []
    seen_chapters: set[str] = set()

    for ordinal, raw_root in enumerate(prior_chapter_roots, 1):
        root = Path(raw_root).resolve()
        artifact, artifact_path = load_b3_temporal_chapter_artifact_v1(root)
        chapter_id = _required_text(artifact.get("chapter_id"), "B3 chapter_id")
        if chapter_id in seen_chapters:
            raise B3TemporalPrefixError("B3 prefix repeats a chapter")
        seen_chapters.add(chapter_id)

        for state_id in artifact.get("closed_prior_state_ids") or []:
            state_id = _required_text(state_id, "closed prior state id")
            effective.pop(state_id, None)
        for raw_state in artifact.get("effective_state_projection") or []:
            state = _effective_state(raw_state)
            state_id = state["state_id"]
            prior = effective.setdefault(state_id, state)
            if canonical_hash(prior) != canonical_hash(state):
                effective[state_id] = _accept_cumulative_effective_state_v1(
                    prior,
                    state,
                )
        artifact_pending_ids: set[str] = set()
        for raw_case in artifact.get("pending_cases") or []:
            case = _pending_case(raw_case)
            case_id = case["pending_case_id"]
            artifact_pending_ids.add(case_id)
            if case_id in resolved:
                raise B3TemporalPrefixError(
                    "terminal B3 case reappeared as pending"
                )
            prior = pending.get(case_id)
            if prior is None:
                pending[case_id] = case
            elif canonical_hash(prior) == canonical_hash(case):
                pass
            elif (
                _is_identity_adapter_park_transition(prior, case)
                or _is_typed_identity_review_reroute_transition(prior, case)
            ):
                pending[case_id] = case
            else:
                raise B3TemporalPrefixError("pending case id changed across chapters")
        for raw_case in artifact.get("resolved_cases") or []:
            case = _resolved_case(raw_case)
            case_id = case["pending_case_id"]
            if case_id in artifact_pending_ids:
                raise B3TemporalPrefixError(
                    "B3 artifact repeats a case across pending and resolved history"
                )
            pending.pop(case_id, None)
            prior = resolved.setdefault(case_id, case)
            if canonical_hash(prior) != canonical_hash(case):
                raise B3TemporalPrefixError(
                    "resolved case id changed across chapters"
                )

        source_rows.append(
            {
                "chapter_ordinal": ordinal,
                "chapter_id": chapter_id,
                "source_root": str(root),
                "source_tree_hash": _tree_hash(root),
                "artifact_path": artifact_path.relative_to(root).as_posix(),
                "artifact_hash": artifact["artifact_hash"],
            }
        )

    body = {
        "schema_version": B3_TEMPORAL_PREFIX_SCHEMA_VERSION_V1,
        "source_chapters": source_rows,
        "effective_open_states": sorted(
            effective.values(), key=lambda row: (row["semantic_key"], row["state_id"])
        ),
        "pending_cases": sorted(pending.values(), key=lambda row: row["pending_case_id"]),
        "resolved_cases": sorted(
            resolved.values(), key=lambda row: row["pending_case_id"]
        ),
        "authority_policy": {
            "effective_open_states": "authoritative_temporal_context",
            "pending_cases": "review_context_only",
            "resolved_cases": "terminal_review_history",
            "identity_inference_by_code": False,
        },
        "production_publish_performed": False,
    }
    return {**body, "prefix_hash": canonical_hash(body)}


def load_b3_temporal_prefix_v1(path: Path) -> dict[str, Any]:
    prefix = _read_object(Path(path), "B3 temporal prefix")
    expected = prefix.get("prefix_hash")
    unsigned = dict(prefix)
    unsigned.pop("prefix_hash", None)
    if expected != canonical_hash(unsigned):
        raise B3TemporalPrefixError("B3 temporal prefix hash mismatch")
    if prefix.get("schema_version") != B3_TEMPORAL_PREFIX_SCHEMA_VERSION_V1:
        raise B3TemporalPrefixError("foreign B3 temporal prefix schema")
    policy = prefix.get("authority_policy")
    current_policy = {
        "effective_open_states": "authoritative_temporal_context",
        "pending_cases": "review_context_only",
        "resolved_cases": "terminal_review_history",
        "identity_inference_by_code": False,
    }
    legacy_policy = dict(current_policy)
    legacy_policy.pop("resolved_cases")
    if policy != current_policy and policy != legacy_policy:
        raise B3TemporalPrefixError("B3 temporal prefix authority policy drifted")
    for row in prefix.get("effective_open_states") or []:
        _effective_state(row)
    for row in prefix.get("pending_cases") or []:
        _pending_case(row)
    for row in prefix.get("resolved_cases") or []:
        _resolved_case(row)
    overlap = {
        row["pending_case_id"] for row in prefix.get("pending_cases") or []
    } & {
        row["pending_case_id"] for row in prefix.get("resolved_cases") or []
    }
    if overlap:
        raise B3TemporalPrefixError(
            "B3 prefix repeats a case across pending and resolved history"
        )
    return prefix


def load_b3_temporal_chapter_artifact_v1(
    root: Path,
) -> tuple[dict[str, Any], Path]:
    source = Path(root).resolve()
    if not source.is_dir():
        raise B3TemporalPrefixError(f"B3 chapter root is absent: {source}")
    candidates = [
        source / "reconciled_temporal_artifact.json",
        source / "chapter_temporal_artifact.json",
        source / "b3_temporal_artifact.json",
    ]
    present = [path for path in candidates if path.is_file()]
    if len(present) != 1:
        raise B3TemporalPrefixError("B3 chapter root must contain exactly one final artifact")
    path = present[0]
    artifact = _verified_hashed_object(path, "artifact_hash", "B3 temporal artifact")
    if artifact.get("schema_version") not in {
        B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1,
        _BATCH_ARTIFACT_SCHEMA,
    }:
        raise B3TemporalPrefixError("foreign B3 temporal artifact schema")
    if artifact.get("identity_mutation_performed") is not False:
        raise B3TemporalPrefixError("B3 temporal artifact claims identity mutation")
    if artifact.get("production_publish_performed") is not False:
        raise B3TemporalPrefixError("B3 temporal artifact claims production publish")
    return artifact, path


def fold_b3_temporal_batch_artifact_v1(
    *,
    effective_states: Sequence[Mapping[str, Any]],
    pending_cases: Sequence[Mapping[str, Any]],
    batch_artifact: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply one accepted batch without granting pending rows authority."""

    artifact = deepcopy(dict(batch_artifact))
    if artifact.get("schema_version") != _BATCH_ARTIFACT_SCHEMA:
        raise B3TemporalPrefixError("cannot fold a foreign B3 batch artifact")
    expected = artifact.get("artifact_hash")
    unsigned = dict(artifact)
    unsigned.pop("artifact_hash", None)
    if expected != canonical_hash(unsigned):
        raise B3TemporalPrefixError("B3 batch artifact hash mismatch")
    effective = {
        row["state_id"]: row for row in (_effective_state(value) for value in effective_states)
    }
    pending = {
        row["pending_case_id"]: row
        for row in (_pending_case(value) for value in pending_cases)
    }
    for state_id in artifact.get("closed_prior_state_ids") or []:
        effective.pop(_required_text(state_id, "closed prior state id"), None)
    for raw_state in artifact.get("effective_state_projection") or []:
        state = _effective_state(raw_state)
        prior = effective.get(state["state_id"])
        if prior is None:
            effective[state["state_id"]] = state
            continue
        comparable = deepcopy(state)
        for field_name in B3_MODEL_HIDDEN_STATE_FIELDS_V1:
            if field_name in prior and field_name not in comparable:
                comparable[field_name] = deepcopy(prior[field_name])
        if canonical_hash(prior) != canonical_hash(comparable):
            raise B3TemporalPrefixError("B3 batch rewrote an immutable state row")
    for raw_case in artifact.get("pending_cases") or []:
        case = _pending_case(raw_case)
        prior = pending.setdefault(case["pending_case_id"], case)
        if canonical_hash(prior) != canonical_hash(case):
            raise B3TemporalPrefixError("B3 batch rewrote an immutable pending row")
    return (
        sorted(effective.values(), key=lambda row: (row["semantic_key"], row["state_id"])),
        sorted(pending.values(), key=lambda row: row["pending_case_id"]),
    )


def _effective_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalPrefixError("effective B3 state must be an object")
    row = deepcopy(dict(value))
    required = {
        "state_id",
        "semantic_key",
        "state_domain",
        "subject_referent_refs",
        "counterpart_referent_refs",
        "state_value",
        "lifecycle_status",
        "authority_status",
    }
    if not required <= set(row):
        raise B3TemporalPrefixError("effective B3 state omits required fields")
    if row["authority_status"] != "effective" or row["lifecycle_status"] != "open":
        raise B3TemporalPrefixError("B3 prefix received a non-effective open state")
    _required_text(row["state_id"], "state_id")
    _required_text(row["semantic_key"], "semantic_key")
    _required_text(row["state_domain"], "state_domain")
    _required_text(row["state_value"], "state_value")
    row["subject_referent_refs"] = _refs(row["subject_referent_refs"])
    row["counterpart_referent_refs"] = _refs(row["counterpart_referent_refs"])
    if not row["subject_referent_refs"]:
        raise B3TemporalPrefixError("effective B3 state has no subject")
    return row


_CUMULATIVE_STATE_LIST_FIELDS = frozenset(
    {
        "source_block_ids",
        "source_event_ids",
        "source_turn_ids",
        "frame_segment_ids",
        "consolidated_state_ids",
        "corroborating_state_ids",
        "corroboration_layers",
        "observations",
    }
)


def _accept_cumulative_effective_state_v1(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept a cumulative projection update without allowing a rewrite.

    Chapter artifacts intentionally carry the complete effective projection
    through that chapter. A state therefore keeps its stable identity while
    accumulating observations and provenance. Semantic identity fields remain
    immutable; every prior evidence item must still be present in the newer
    projection.
    """

    keys = set(previous) | set(current)
    for key in keys - _CUMULATIVE_STATE_LIST_FIELDS - {"observation_count"}:
        if previous.get(key) != current.get(key):
            raise B3TemporalPrefixError(
                "effective state id changed across chapters"
            )

    for field in _CUMULATIVE_STATE_LIST_FIELDS:
        prior_values = previous.get(field)
        current_values = current.get(field)
        if prior_values is None:
            continue
        if not isinstance(prior_values, list) or not isinstance(
            current_values, list
        ):
            raise B3TemporalPrefixError(
                "effective state cumulative evidence is malformed"
            )
        if field == "observations":
            prior_hashes = {canonical_hash(item) for item in prior_values}
            current_hashes = {canonical_hash(item) for item in current_values}
        else:
            prior_hashes = {canonical_hash(item) for item in prior_values}
            current_hashes = {canonical_hash(item) for item in current_values}
        if not prior_hashes <= current_hashes:
            raise B3TemporalPrefixError(
                "effective state cumulative evidence was removed"
            )

    previous_count = previous.get("observation_count")
    current_count = current.get("observation_count")
    if previous_count is not None:
        if not isinstance(previous_count, int) or isinstance(previous_count, bool):
            raise B3TemporalPrefixError(
                "effective state observation count is malformed"
            )
        if not isinstance(current_count, int) or isinstance(current_count, bool):
            raise B3TemporalPrefixError(
                "effective state observation count is malformed"
            )
        if current_count < previous_count:
            raise B3TemporalPrefixError(
                "effective state cumulative observation count decreased"
            )
    observations = current.get("observations")
    if observations is not None and current_count != len(observations):
        raise B3TemporalPrefixError(
            "effective state observation count differs from observations"
        )
    return deepcopy(dict(current))


def _pending_case(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalPrefixError("pending B3 case must be an object")
    row = deepcopy(dict(value))
    for field in ("pending_case_id", "chapter_id", "review_route", "reason"):
        _required_text(row.get(field), field)
    if row.get("authority_status") != "pending_review":
        raise B3TemporalPrefixError("pending B3 case claims authority")
    if not isinstance(row.get("reason_codes"), list) or not all(
        isinstance(item, str) and item for item in row["reason_codes"]
    ):
        raise B3TemporalPrefixError("pending B3 reason codes are malformed")
    action = row.get("proposed_action")
    if action is not None and not isinstance(action, Mapping):
        raise B3TemporalPrefixError("pending B3 proposed action is malformed")
    return row


def _resolved_case(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalPrefixError("resolved B3 case must be an object")
    row = deepcopy(dict(value))
    for field in ("pending_case_id", "chapter_id", "disposition"):
        _required_text(row.get(field), field)
    if row.get("authority_status") != "resolved_terminal":
        raise B3TemporalPrefixError("resolved B3 case lacks terminal authority")
    if row.get("disposition") == "origin_unknown":
        window = row.get("unknowable_window")
        if not isinstance(window, Mapping):
            raise B3TemporalPrefixError(
                "origin_unknown case lacks an unknowable_window"
            )
        _required_text(window.get("from_chapter"), "unknowable from_chapter")
        _required_text(window.get("to_chapter"), "unknowable to_chapter")
        if window.get("blocker") != "onset_not_stated_in_source":
            raise B3TemporalPrefixError(
                "origin_unknown case has a foreign unknowable blocker"
            )
    return row


def _is_identity_adapter_park_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    expected = deepcopy(dict(previous))
    expected.update(
        {
            "lifecycle_state": "awaiting_identity_adapter",
            "parked_reason": "no implemented consumer; see B.3 amendment",
        }
    )
    return canonical_hash(expected) == canonical_hash(current)


def _is_typed_identity_review_reroute_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    if previous.get("review_route") != "identity_review":
        return False
    reasons = previous.get("reason_codes")
    if not isinstance(reasons, list) or not all(
        isinstance(value, str) and value for value in reasons
    ):
        return False
    reason_set = set(reasons)
    if reason_set == {"referent_identity_not_confirmed"}:
        expected_route = "temporal_review"
    elif reason_set == {
        "referent_identity_not_confirmed",
        "stable_claim_domain_requires_review",
    }:
        expected_route = "stable_claim_review"
    else:
        return False
    expected = deepcopy(dict(previous))
    expected["review_route"] = expected_route
    return canonical_hash(expected) == canonical_hash(current)


def _refs(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise B3TemporalPrefixError("B3 referent refs are malformed")
    if len(value) != len(set(value)):
        raise B3TemporalPrefixError("B3 referent refs are duplicated")
    return sorted(value)


def _verified_hashed_object(path: Path, field: str, label: str) -> dict[str, Any]:
    row = _read_object(path, label)
    expected = row.get(field)
    unsigned = dict(row)
    unsigned.pop(field, None)
    if expected != canonical_hash(unsigned):
        raise B3TemporalPrefixError(f"{label} hash mismatch")
    return row


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise B3TemporalPrefixError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B3TemporalPrefixError(f"{label} must be an object")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B3TemporalPrefixError(f"{label} must be a non-empty string")
    return value


def _tree_hash(root: Path) -> str:
    rows = [
        {"path": path.relative_to(root).as_posix(), "sha256": file_sha256(path)}
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    ]
    return canonical_hash(rows)


__all__ = [
    "B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1",
    "B3_TEMPORAL_PREFIX_SCHEMA_VERSION_V1",
    "B3TemporalPrefixError",
    "build_b3_temporal_prefix_v1",
    "fold_b3_temporal_batch_artifact_v1",
    "load_b3_temporal_chapter_artifact_v1",
    "load_b3_temporal_prefix_v1",
]
