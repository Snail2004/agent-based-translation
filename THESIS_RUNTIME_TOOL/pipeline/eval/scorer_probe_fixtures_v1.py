from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_string,
    require_string,
    require_unique,
)


__all__ = [
    "DEFAULT_SCORER_PROBE_FIXTURE_PATH",
    "PJ_REQUIRED_CATEGORIES",
    "SF_BT_STRATA",
    "load_default_scorer_probe_fixture_set",
    "planted_marker_present",
    "scorer_probe_fixture_sha256",
    "validate_scorer_probe_fixture_set",
]


DEFAULT_SCORER_PROBE_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "eval" / "scorer_planted_probe_v1.json"
)

_SCHEMA_ID = "EvaluationScorerPlantedProbeV1"
_SCHEMA_VERSION = "1.0.0"
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("sf_bt_context_ablation",),
            ("pj_cases",),
        }
    ),
)

SF_BT_STRATA = frozenset(
    {
        "P1_context_repair_risk",
        "P2_omission_control",
        "P3_anaphora_false_alarm",
        "P4_ambiguity_resolution",
        "P5_context_import_bait",
    }
)
_SF_BT_MEASUREMENTS = {
    "P1_context_repair_risk": "context_repair_import",
    "P2_omission_control": "omission_detection_control",
    "P3_anaphora_false_alarm": "anaphora_false_alarm",
    "P4_ambiguity_resolution": "ambiguity_false_alarm",
    "P5_context_import_bait": "context_only_import",
}
_DIRECTION_BALANCED_STRATA = frozenset(
    {
        "P1_context_repair_risk",
        "P4_ambiguity_resolution",
        "P5_context_import_bait",
    }
)
PJ_REQUIRED_CATEGORIES = frozenset(
    {
        "identical",
        "meaning_reversal",
        "omission_addition",
        "numeric",
        "negation",
        "grammar",
        "terminology_only",
        "tone_register",
        "formatting",
        "short_fragment",
        "naturalness",
        "word_choice",
    }
)
_PJ_EXPECTED = frozenset({"a", "b", "tie"})
_PJ_TAGS = frozenset(
    {
        "grammar",
        "naturalness",
        "word_choice",
        "terminology",
        "meaning",
        "omission_addition",
        "formatting",
        "tone_voice",
    }
)


def load_default_scorer_probe_fixture_set() -> dict[str, Any]:
    try:
        raw = json.loads(DEFAULT_SCORER_PROBE_FIXTURE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "fixture_file",
            str(DEFAULT_SCORER_PROBE_FIXTURE_PATH),
            "default scorer probe fixture is unreadable or malformed",
        ) from exc
    return validate_scorer_probe_fixture_set(raw)


def validate_scorer_probe_fixture_set(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "fixture_set_id",
            "review_status",
            "book_neutral",
            "runtime_admission",
            "sf_bt_context_ablation",
            "pj_cases",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(root["schema_id"], {_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "fixture_set_id": require_string(
            root["fixture_set_id"], path="$.fixture_set_id"
        ),
        "review_status": require_enum(
            root["review_status"],
            {"approved_external_review"},
            path="$.review_status",
        ),
        "book_neutral": _require_true(root["book_neutral"], path="$.book_neutral"),
        "runtime_admission": require_enum(
            root["runtime_admission"], {"forbidden"}, path="$.runtime_admission"
        ),
        "sf_bt_context_ablation": _validate_sf_bt_cases(
            root["sf_bt_context_ablation"]
        ),
        "pj_cases": _validate_pj_cases(root["pj_cases"]),
    }
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical scorer probe fixture must remain an object")
    return canonical


def scorer_probe_fixture_sha256(payload: Mapping[str, Any]) -> str:
    validated = validate_scorer_probe_fixture_set(payload)
    return canonical_sha256(validated, policy=_POLICY)


def planted_marker_present(text: str, marker: str) -> bool:
    normalized_text = unicodedata.normalize("NFC", text).casefold()
    normalized_marker = unicodedata.normalize("NFC", marker).casefold()
    if not normalized_marker:
        return False
    escaped = re.escape(normalized_marker)
    left = r"(?<!\w)" if normalized_marker[0].isalnum() else ""
    right = r"(?!\w)" if normalized_marker[-1].isalnum() else ""
    return re.search(f"{left}{escaped}{right}", normalized_text) is not None


def _validate_sf_bt_cases(value: Any) -> list[dict[str, Any]]:
    path = "$.sf_bt_context_ablation"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, value in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value, path=row_path)
        require_exact_keys(
            row,
            required={
                "case_id",
                "stratum",
                "source_active_en",
                "target_preceding_vi",
                "target_active_vi",
                "target_following_vi",
                "planted_marker",
                "measurement",
                "author_note",
            },
            path=row_path,
        )
        stratum = require_enum(
            row["stratum"], SF_BT_STRATA, path=f"{row_path}.stratum"
        )
        normalized = {
            "case_id": require_string(row["case_id"], path=f"{row_path}.case_id"),
            "stratum": stratum,
            "source_active_en": require_string(
                row["source_active_en"], path=f"{row_path}.source_active_en"
            ),
            "target_preceding_vi": require_nullable_string(
                row["target_preceding_vi"],
                path=f"{row_path}.target_preceding_vi",
            ),
            "target_active_vi": require_string(
                row["target_active_vi"], path=f"{row_path}.target_active_vi"
            ),
            "target_following_vi": require_nullable_string(
                row["target_following_vi"],
                path=f"{row_path}.target_following_vi",
            ),
            "planted_marker": require_string(
                row["planted_marker"], path=f"{row_path}.planted_marker"
            ),
            "measurement": require_enum(
                row["measurement"],
                {_SF_BT_MEASUREMENTS[stratum]},
                path=f"{row_path}.measurement",
            ),
            "author_note": require_string(
                row["author_note"], path=f"{row_path}.author_note", maximum=320
            ),
        }
        _validate_marker_placement(normalized, path=row_path)
        result.append(normalized)

    require_unique([row["case_id"] for row in result], path=f"{path}.case_id")
    counts = {
        stratum: sum(row["stratum"] == stratum for row in result)
        for stratum in SF_BT_STRATA
    }
    if any(count != 10 for count in counts.values()) or len(result) != 50:
        raise ContractValidationError(
            "stratum_count",
            path,
            "SF-BT fixture must contain exactly ten rows per P1-P5 stratum",
        )
    _validate_context_direction_balance(result, path=path)
    return result


def _validate_marker_placement(row: Mapping[str, Any], *, path: str) -> None:
    marker = row["planted_marker"]
    source = row["source_active_en"]
    active = row["target_active_vi"]
    contexts = tuple(
        value
        for value in (row["target_preceding_vi"], row["target_following_vi"])
        if value is not None
    )
    in_source = planted_marker_present(source, marker)
    in_active = planted_marker_present(active, marker)
    in_context = any(planted_marker_present(value, marker) for value in contexts)
    stratum = row["stratum"]

    valid = True
    if stratum == "P1_context_repair_risk":
        valid = in_source and not in_active and in_context
    elif stratum == "P2_omission_control":
        valid = in_source and not in_active and not in_context
    elif stratum == "P3_anaphora_false_alarm":
        valid = in_source and not in_active and in_context
    elif stratum == "P4_ambiguity_resolution":
        valid = in_source and not in_active and bool(contexts)
    elif stratum == "P5_context_import_bait":
        valid = not in_source and not in_active and in_context
    if not valid:
        raise ContractValidationError(
            "marker_placement",
            f"{path}.planted_marker",
            "planted marker does not satisfy its stratum placement rule",
        )


def _validate_context_direction_balance(
    rows: list[dict[str, Any]], *, path: str
) -> None:
    for stratum in _DIRECTION_BALANCED_STRATA:
        selected = [row for row in rows if row["stratum"] == stratum]
        preceding_only = sum(
            row["target_preceding_vi"] is not None
            and row["target_following_vi"] is None
            for row in selected
        )
        following_only = sum(
            row["target_preceding_vi"] is None
            and row["target_following_vi"] is not None
            for row in selected
        )
        if preceding_only != 5 or following_only != 5:
            raise ContractValidationError(
                "context_direction_balance",
                path,
                f"{stratum} must contain five preceding-only and five following-only rows",
            )


def _validate_pj_cases(value: Any) -> list[dict[str, Any]]:
    path = "$.pj_cases"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, value in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value, path=row_path)
        require_exact_keys(
            row,
            required={
                "case_id",
                "category",
                "block_type",
                "source_en",
                "candidate_a_vi",
                "candidate_b_vi",
                "expected_overall",
                "expected_style",
                "expected_primary_tag",
                "author_note",
            },
            path=row_path,
        )
        expected_tag = row["expected_primary_tag"]
        if expected_tag is not None:
            expected_tag = require_enum(
                expected_tag, _PJ_TAGS, path=f"{row_path}.expected_primary_tag"
            )
        result.append(
            {
                "case_id": require_string(
                    row["case_id"], path=f"{row_path}.case_id"
                ),
                "category": require_enum(
                    row["category"],
                    PJ_REQUIRED_CATEGORIES,
                    path=f"{row_path}.category",
                ),
                "block_type": require_string(
                    row["block_type"], path=f"{row_path}.block_type"
                ),
                "source_en": require_string(
                    row["source_en"], path=f"{row_path}.source_en"
                ),
                "candidate_a_vi": require_string(
                    row["candidate_a_vi"], path=f"{row_path}.candidate_a_vi"
                ),
                "candidate_b_vi": require_string(
                    row["candidate_b_vi"], path=f"{row_path}.candidate_b_vi"
                ),
                "expected_overall": require_enum(
                    row["expected_overall"],
                    _PJ_EXPECTED,
                    path=f"{row_path}.expected_overall",
                ),
                "expected_style": require_enum(
                    row["expected_style"],
                    _PJ_EXPECTED,
                    path=f"{row_path}.expected_style",
                ),
                "expected_primary_tag": expected_tag,
                "author_note": require_string(
                    row["author_note"], path=f"{row_path}.author_note", maximum=320
                ),
            }
        )
    require_unique([row["case_id"] for row in result], path=f"{path}.case_id")
    covered = {row["category"] for row in result}
    if covered != PJ_REQUIRED_CATEGORIES:
        missing = ", ".join(sorted(PJ_REQUIRED_CATEGORIES - covered))
        raise ContractValidationError(
            "category_coverage",
            path,
            f"PJ planted set is missing required categories: {missing}",
        )
    return result


def _require_true(value: Any, *, path: str) -> bool:
    if value is not True:
        raise ContractValidationError("literal", path, "value must be true")
    return True
