from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b4_address_anchor_v1 import (
    ARTIFACT_SCHEMA_VERSION as ADDRESS_ARTIFACT_SCHEMA_VERSION,
    load_style_profile_v1,
    render_address_anchor_request_v1,
)
from pipeline.literary.b4_live_modelapi_v1 import (
    estimate_b4_transport_request_v1,
)
from pipeline.literary.b4_translator_pack_v1 import (
    B4TranslatorPackError,
    DEFAULT_DORMANCY_WINDOW_CHAPTERS,
    PROJECTION_STRATEGIES,
    ProjectedTranslatorPackV1,
    project_translator_pack_tiered_v2,
    project_translator_pack_v1,
    seal_translator_pack_v1,
)
from pipeline.literary.b4_translator_v1 import (
    RESPONSE_SCHEMA_VERSION,
    ROLE_ID as TRANSLATOR_ROLE_ID,
    render_translation_window_request_v1,
    translator_window_prompt_view_v1,
)
from pipeline.literary.chapter_source_document_v1 import (
    chapter_from_document_v1,
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a budgeted B4 Translator Pack without provider calls"
    )
    parser.add_argument("--story-bible", type=Path, required=True)
    anchor = parser.add_mutually_exclusive_group(required=True)
    anchor.add_argument("--address-anchor", type=Path)
    anchor.add_argument("--anchor-input", type=Path)
    parser.add_argument("--window-slice", type=Path, action="append", required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--style-design", type=Path, required=True)
    parser.add_argument("--style-profile-version", required=True)
    parser.add_argument("--capability-root", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--headroom-tokens", type=int, default=4_000)
    parser.add_argument(
        "--projection-strategy",
        choices=sorted(PROJECTION_STRATEGIES),
        default="tiered_v2",
    )
    parser.add_argument(
        "--dormancy-window-chapters",
        type=int,
        default=DEFAULT_DORMANCY_WINDOW_CHAPTERS,
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = _fresh(args.out_dir)
    try:
        result = build_translator_pack_v1(
            story_bible=_read(args.story_bible),
            address_anchor=(
                _read(args.address_anchor) if args.address_anchor else None
            ),
            anchor_input=_read(args.anchor_input) if args.anchor_input else None,
            window_slices=[_read(path) for path in args.window_slice],
            document_path=args.document,
            style_design=args.style_design,
            style_profile_version=args.style_profile_version,
            capability_evidence=_read(
                args.capability_root / "capability_evidence.json"
            ),
            calibration_path=args.calibration,
            headroom_tokens=args.headroom_tokens,
            dormancy_window_chapters=args.dormancy_window_chapters,
            projection_strategy=args.projection_strategy,
        )
    except B4TranslatorPackError as exc:
        failure = exc.report or {
            "schema_version": "literary_b4_translator_pack_failure_v1",
            "status": "failed",
            "reason": str(exc),
            "provider_calls": 0,
        }
        _write(output / "translator_pack_failure.json", failure)
        raise
    _write(output / "translator_pack_as_of_chNN.json", result["pack"])
    _write(output / "translator_pack_report.json", result["report"])
    if result["planning_address_anchor"] is not None:
        _write(
            output / "planning_address_anchor.json",
            result["planning_address_anchor"],
        )
    if result["migrated_address_anchor"] is not None:
        _write(
            output / "migrated_address_anchor.json",
            result["migrated_address_anchor"],
        )
    for window in result["planning_window_slices"]:
        order = int(window["window_order"])
        _write(
            output / f"planning_window_slice_w{order:02d}.json",
            window,
        )
    print(json.dumps(result["report"], ensure_ascii=False, indent=2))
    return 0


def build_translator_pack_v1(
    *,
    story_bible: Mapping[str, Any],
    address_anchor: Mapping[str, Any] | None,
    anchor_input: Mapping[str, Any] | None,
    window_slices: Sequence[Mapping[str, Any]],
    document_path: Path,
    style_design: Path,
    style_profile_version: str,
    capability_evidence: Mapping[str, Any],
    calibration_path: Path,
    headroom_tokens: int,
    dormancy_window_chapters: int,
    projection_strategy: str = "tiered_v2",
) -> dict[str, Any]:
    if (address_anchor is None) == (anchor_input is None):
        raise B4TranslatorPackError(
            "exactly one Address Anchor or planning anchor input is required"
        )
    if not isinstance(headroom_tokens, int) or isinstance(headroom_tokens, bool):
        raise B4TranslatorPackError("headroom_tokens must be an integer")
    if headroom_tokens < 0:
        raise B4TranslatorPackError("headroom_tokens must not be negative")
    if projection_strategy not in PROJECTION_STRATEGIES:
        raise B4TranslatorPackError(
            f"unsupported projection strategy: {projection_strategy}"
        )

    style_profile = load_style_profile_v1(
        design_doc=style_design,
        style_profile_version=style_profile_version,
    )
    planning_only = address_anchor is None
    planning_anchor = None
    migrated_anchor = None
    windows = sorted(
        (deepcopy(dict(row)) for row in window_slices),
        key=lambda row: int(row.get("window_order", 0)),
    )
    planning_window_slices: list[dict[str, Any]] = []
    window_migrations: list[dict[str, Any]] = []
    if planning_only:
        windows, planning_window_slices, window_migrations = (
            _current_planning_window_slices_v1(windows)
        )
    planning_anchor_supplemented_pair_count = 0
    if address_anchor is None:
        planning_anchor, planning_anchor_supplemented_pair_count = (
            _planning_address_anchor_v1(
                anchor_input=anchor_input or {},
                window_slices=windows,
                style_profile=style_profile,
                style_profile_version=style_profile_version,
            )
        )
        address_anchor = planning_anchor
    else:
        address_anchor, migrated = _current_address_anchor_v1(address_anchor)
        if migrated:
            migrated_anchor = address_anchor
    projector = (
        project_translator_pack_tiered_v2
        if projection_strategy == "tiered_v2"
        else project_translator_pack_v1
    )
    projected = projector(
        story_bible=story_bible,
        address_anchor=address_anchor,
        window_slices=windows,
        dormancy_window_chapters=dormancy_window_chapters,
        planning_only=planning_only,
    )
    provisional_pack = _temporary_pack(projected)
    empty_pack = _temporary_pack(_empty_projection(projected))

    document = load_literary_source_document_v1(document_path)
    chapter_id = str(story_bible.get("chapter_id"))
    chapter = chapter_from_document_v1(document, chapter_id)
    source_by_id = {
        str(row["block_id"]): str(row.get("clean_text") or "")
        for row in chapter.get("blocks") or []
        if isinstance(row, Mapping)
    }
    window_reports = []
    for window in windows:
        tail_ids = [str(row) for row in window.get("preceding_tail_block_ids") or []]
        accepted_tail = {
            block_id: source_by_id[block_id]
            for block_id in tail_ids
            if block_id in source_by_id
        }
        if set(accepted_tail) != set(tail_ids):
            raise B4TranslatorPackError(
                "window tail contains a block absent from the source document"
            )
        full = _render_request(
            pack=provisional_pack,
            anchor=address_anchor,
            window=window,
            chapter=chapter,
            accepted_tail=accepted_tail,
            style_profile=style_profile,
            style_profile_version=style_profile_version,
        )
        empty = _render_request(
            pack=empty_pack,
            anchor=address_anchor,
            window=window,
            chapter=chapter,
            accepted_tail=accepted_tail,
            style_profile=style_profile,
            style_profile_version=style_profile_version,
        )
        base = deepcopy(empty)
        base["messages"][-1]["content"] = canonical_json(
            {
                "window_slice": {},
                "preceding_tail": [],
                "active_source_blocks": [],
            }
        )
        base["request_fingerprint"] = canonical_hash(
            {
                "messages": base["messages"],
                "response_schema": base["response_schema"],
            }
        )
        legacy_request = deepcopy(full)
        legacy_request["messages"][1]["content"] = (
            "[STORY_BIBLE]\n" + canonical_json(story_bible)
        )
        estimates = {
            name: estimate_b4_transport_request_v1(
                role_id=TRANSLATOR_ROLE_ID,
                request=request,
                schema_name=RESPONSE_SCHEMA_VERSION,
                capability_evidence=capability_evidence,
                calibration_path=calibration_path,
            )
            for name, request in (
                ("base", base),
                ("empty", empty),
                ("pack", full),
                ("story_bible", legacy_request),
            )
        }
        profile_anchor_instructions = _upper(estimates["base"])
        window_contribution = (
            _upper(estimates["empty"]) - profile_anchor_instructions
        )
        pack_contribution = (
            _upper(estimates["pack"]) - _upper(estimates["empty"])
        )
        if window_contribution < 0 or pack_contribution < 0:
            raise B4TranslatorPackError(
                "transport decomposition produced a negative component"
            )
        prompt_window = translator_window_prompt_view_v1(
            window=window,
            anchor=address_anchor,
        )
        window_reports.append(
            {
                "window_id": window.get("window_id"),
                "window_order": window.get("window_order"),
                "tail_estimation_basis": "source_text_proxy",
                "empty_upper_bound_tokens": _upper(estimates["empty"]),
                "pack_upper_bound_tokens": _upper(estimates["pack"]),
                "story_bible_upper_bound_tokens": _upper(
                    estimates["story_bible"]
                ),
                "profile_anchor_instructions_upper_bound_tokens": (
                    profile_anchor_instructions
                ),
                "window_contribution_tokens": window_contribution,
                "pack_contribution_tokens": pack_contribution,
                "story_bible_contribution_tokens": max(
                    0,
                    _upper(estimates["story_bible"])
                    - _upper(estimates["empty"]),
                ),
                "safety_multiplier": estimates["pack"]["safety_multiplier"],
                "calibration_artifact_hash": estimates["pack"][
                    "calibration_artifact_hash"
                ],
                "translator_cap_tokens": estimates["pack"]["max_input_tokens"],
                "source_window_slice_utf8_bytes": len(
                    canonical_json(window).encode("utf-8")
                ),
                "translator_window_prompt_utf8_bytes": len(
                    canonical_json(prompt_window).encode("utf-8")
                ),
            }
        )

    caps = {int(row["translator_cap_tokens"]) for row in window_reports}
    multipliers = {float(row["safety_multiplier"]) for row in window_reports}
    calibration_hashes = {
        str(row["calibration_artifact_hash"]) for row in window_reports
    }
    if len(caps) != 1 or len(multipliers) != 1 or len(calibration_hashes) != 1:
        raise B4TranslatorPackError("window budget inputs are inconsistent")
    cap = next(iter(caps))
    fixed = max(int(row["empty_upper_bound_tokens"]) for row in window_reports)
    pack_estimated = max(
        int(row["pack_contribution_tokens"]) for row in window_reports
    )
    full_upper = max(
        int(row["pack_upper_bound_tokens"]) for row in window_reports
    )
    bible_estimated = max(
        int(row["story_bible_contribution_tokens"]) for row in window_reports
    )
    worst_window = max(
        window_reports,
        key=lambda row: int(row["pack_upper_bound_tokens"]),
    )
    worst_decomposition = {
        "window_id": worst_window["window_id"],
        "window_order": worst_window["window_order"],
        "pack_tokens": int(worst_window["pack_contribution_tokens"]),
        "window_tokens": int(worst_window["window_contribution_tokens"]),
        "profile_anchor_instructions_tokens": int(
            worst_window["profile_anchor_instructions_upper_bound_tokens"]
        ),
        "full_upper_bound_tokens": int(worst_window["pack_upper_bound_tokens"]),
        "basis": (
            "same calibrated transport estimator; profile component includes "
            "schema, envelope, and empty JSON containers"
        ),
    }
    if (
        worst_decomposition["pack_tokens"]
        + worst_decomposition["window_tokens"]
        + worst_decomposition["profile_anchor_instructions_tokens"]
        != worst_decomposition["full_upper_bound_tokens"]
    ):
        raise B4TranslatorPackError(
            "worst-window transport decomposition does not sum to full"
        )
    budget_report = {
        "translator_cap_tokens": cap,
        "headroom_tokens": headroom_tokens,
        "fixed_prompt_upper_bound_tokens": fixed,
        "pack_budget_tokens": cap - headroom_tokens - fixed,
        "pack_estimated_tokens": pack_estimated,
        "full_story_bible_estimated_tokens": bible_estimated,
        "max_full_prompt_upper_bound_tokens": full_upper,
        "safety_multiplier": next(iter(multipliers)),
        "calibration_artifact_hash": next(iter(calibration_hashes)),
        "dormancy_window_chapters": dormancy_window_chapters,
        "capacity_workaround": {
            "translator_cap_tokens": cap,
            "classification": "capacity_workaround_sized_from_measured_growth",
            "substitute_for_filter": False,
        },
        "tail_estimation_basis": "source_text_proxy",
        "window_reports": deepcopy(window_reports),
        "worst_window_decomposition": deepcopy(worst_decomposition),
    }
    pack = seal_translator_pack_v1(
        projected=projected,
        budget_report=budget_report,
    )
    report_body = {
        "schema_version": "literary_b4_translator_pack_report_v1",
        "status": "complete",
        "book_id": pack["book_id"],
        "chapter_id": pack["chapter_id"],
        "chapter_order": pack["chapter_order"],
        "planning_only": planning_only,
        "projection_strategy": projection_strategy,
        "projection_metrics": deepcopy(pack.get("projection_metrics") or {}),
        "memory_tier_counts": {
            tier: sum(
                row.get("memory_tier") == tier
                for row in pack.get("entities") or []
                if isinstance(row, Mapping)
            )
            for tier in ("chapter_context", "core_identity")
        },
        "story_bible_artifact_hash": pack["story_bible_artifact_hash"],
        "address_anchor_artifact_hash": pack["address_anchor_artifact_hash"],
        "translator_pack_artifact_hash": pack["artifact_hash"],
        "story_bible_utf8_bytes": len(canonical_json(story_bible).encode("utf-8")),
        "translator_pack_utf8_bytes": len(canonical_json(pack).encode("utf-8")),
        "full_story_bible_estimated_tokens": bible_estimated,
        "translator_pack_estimated_tokens": pack_estimated,
        "pack_budget_tokens": budget_report["pack_budget_tokens"],
        "fixed_prompt_upper_bound_tokens": fixed,
        "max_full_prompt_upper_bound_tokens": full_upper,
        "omitted_count": pack["pack_budget"]["omitted_count"],
        "omitted_by_reason": deepcopy(pack["pack_budget"]["omitted_by_reason"]),
        "omitted_by_section": deepcopy(pack["pack_budget"]["omitted_by_section"]),
        "source_counts": deepcopy(projected.source_counts),
        "kept_counts": deepcopy(projected.kept_counts),
        "window_reports": window_reports,
        "worst_window_decomposition": worst_decomposition,
        "provider_calls": 0,
        "address_anchor_migrated_from_v2": migrated_anchor is not None,
        "legacy_window_migrations": window_migrations,
        "legacy_window_migration_count": len(window_migrations),
        "legacy_window_pair_migration_count": sum(
            int(row["migrated_pair_count"]) for row in window_migrations
        ),
        "planning_anchor_supplemented_pair_count": (
            planning_anchor_supplemented_pair_count
        ),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    return {
        "pack": pack,
        "report": report,
        "planning_address_anchor": planning_anchor,
        "migrated_address_anchor": migrated_anchor,
        "planning_window_slices": planning_window_slices,
    }


def _planning_address_anchor_v1(
    *,
    anchor_input: Mapping[str, Any],
    window_slices: Sequence[Mapping[str, Any]],
    style_profile: str,
    style_profile_version: str,
) -> tuple[dict[str, Any], int]:
    rendered = render_address_anchor_request_v1(
        anchor_input=anchor_input,
        style_profile=style_profile,
        style_profile_version=style_profile_version,
        measured_arm=False,
    )
    source_pair_ids = set(rendered.pair_ref_to_id.values())
    window_pair_ids = {
        str(pair["pair_id"])
        for window in window_slices
        for pair in window.get("address_pairs") or []
        if isinstance(pair, Mapping) and pair.get("pair_id") is not None
    }
    supplemented_pair_ids = sorted(window_pair_ids - source_pair_ids)
    all_pair_ids = sorted(source_pair_ids | window_pair_ids)
    body = {
        "schema_version": ADDRESS_ARTIFACT_SCHEMA_VERSION,
        "book_id": rendered.anchor_input["book_id"],
        "chapter_id": rendered.anchor_input["chapter_id"],
        "style_profile_version": style_profile_version,
        "measured_arm": False,
        "story_bible_artifact_hash": rendered.anchor_input[
            "story_bible_artifact_hash"
        ],
        "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
        "request_fingerprint": canonical_hash(
            {
                "planning_only": True,
                "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
            }
        ),
        "pair_decisions": [
            {
                "pair_id": pair_id,
                "pronoun_pair": None,
                "vocative_options": [],
                "register_shifts": [],
                "evidence_refs": [],
                "model_confidence": "low",
                "not_anchored": {
                    "reason": "Planning only; no model decision is available."
                },
            }
            for pair_id in all_pair_ids
        ],
        "review_issues": [
            {
                "issue_kind": "planning_pair_absent_from_legacy_anchor_input",
                "pair_id": pair_id,
            }
            for pair_id in supplemented_pair_ids
        ],
        "normalization_observations": [],
        "provider_called": False,
        "provider_receipt": None,
        "translation_performed": False,
        "semantic_record_mutation_performed": False,
    }
    return (
        {**body, "artifact_hash": canonical_hash(body)},
        len(supplemented_pair_ids),
    )


def _current_address_anchor_v1(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    source = deepcopy(dict(value))
    body = deepcopy(source)
    observed_hash = body.pop("artifact_hash", None)
    if observed_hash != canonical_hash(body):
        raise B4TranslatorPackError("Address Anchor hash mismatch")
    if source.get("schema_version") == ADDRESS_ARTIFACT_SCHEMA_VERSION:
        return source, False
    if source.get("schema_version") != "literary_b4_address_anchor_artifact_v2":
        raise B4TranslatorPackError("unsupported Address Anchor artifact schema")
    for decision in source.get("pair_decisions") or []:
        if not isinstance(decision, Mapping):
            raise B4TranslatorPackError("legacy Address Anchor decision is malformed")
        baseline = decision.get("pronoun_pair")
        if any(
            isinstance(shift, Mapping)
            and baseline is not None
            and shift.get("pronoun_pair") == baseline
            for shift in decision.get("register_shifts") or []
        ):
            raise B4TranslatorPackError(
                "legacy Address Anchor contains a no-op shift and requires replay"
            )
    body["schema_version"] = ADDRESS_ARTIFACT_SCHEMA_VERSION
    body["normalization_observations"] = []
    return {**body, "artifact_hash": canonical_hash(body)}, True


def _current_planning_window_slices_v1(
    values: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    migrated: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for value in values:
        source = deepcopy(dict(value))
        body = deepcopy(source)
        observed_hash = body.pop("artifact_hash", None)
        if observed_hash != canonical_hash(body):
            raise B4TranslatorPackError("window slice hash mismatch")
        pairs = body.get("address_pairs")
        if not isinstance(pairs, list):
            raise B4TranslatorPackError("window address_pairs must be a list")
        migrated_pair_count = 0
        for pair in pairs:
            if not isinstance(pair, dict):
                raise B4TranslatorPackError("window address pair is malformed")
            has_pair_id = "pair_id" in pair
            has_unanchored = "unanchored" in pair
            if has_pair_id != has_unanchored:
                raise B4TranslatorPackError(
                    "legacy window address pair has a partial pair-id migration"
                )
            if has_pair_id:
                continue
            speaker = _optional_nonempty_text(
                pair.get("speaker_effective_entity_id")
            )
            addressee = _optional_nonempty_text(
                pair.get("addressee_effective_entity_id")
            )
            pair_id = _resolved_address_pair_id_v1(speaker, addressee)
            pair["pair_id"] = pair_id
            pair["unanchored"] = pair_id is None
            migrated_pair_count += 1
        if migrated_pair_count:
            upgraded = {**body, "artifact_hash": canonical_hash(body)}
            migrated.append(upgraded)
            reports.append(
                {
                    "window_id": upgraded.get("window_id"),
                    "window_order": upgraded.get("window_order"),
                    "source_artifact_hash": observed_hash,
                    "migrated_artifact_hash": upgraded["artifact_hash"],
                    "migrated_pair_count": migrated_pair_count,
                    "migration": "add_pair_id_and_unanchored_from_resolved_endpoints",
                }
            )
            current.append(upgraded)
        else:
            current.append(source)
    return current, migrated, reports


def _resolved_address_pair_id_v1(
    speaker_effective_entity_id: str | None,
    addressee_effective_entity_id: str | None,
) -> str | None:
    if not speaker_effective_entity_id or not addressee_effective_entity_id:
        return None
    return canonical_hash(
        {
            "speaker_effective_entity_id": speaker_effective_entity_id,
            "addressee_effective_entity_id": addressee_effective_entity_id,
        }
    )[:24]


def _optional_nonempty_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _temporary_pack(projected: ProjectedTranslatorPackV1) -> dict[str, Any]:
    return seal_translator_pack_v1(
        projected=projected,
        budget_report={
            "translator_cap_tokens": 1_000_000_000,
            "headroom_tokens": 0,
            "fixed_prompt_upper_bound_tokens": 0,
            "pack_budget_tokens": 1_000_000_000,
            "pack_estimated_tokens": 0,
            "max_full_prompt_upper_bound_tokens": 0,
            "safety_multiplier": 1.0,
            "calibration_artifact_hash": "0" * 64,
        },
    )


def _empty_projection(
    projected: ProjectedTranslatorPackV1,
) -> ProjectedTranslatorPackV1:
    body = deepcopy(projected.body)
    for field in ("entities", "relations", "states", "idiolect"):
        body[field] = []
    body["open_questions"] = {
        key: [] for key in body["open_questions"]
    }
    return ProjectedTranslatorPackV1(
        body=body,
        omissions=(),
        source_counts={},
        kept_counts={},
        relevant_entity_ids=(),
        current_speaker_entity_ids=(),
    )


def _render_request(
    *,
    pack: Mapping[str, Any],
    anchor: Mapping[str, Any],
    window: Mapping[str, Any],
    chapter: Mapping[str, Any],
    accepted_tail: Mapping[str, str],
    style_profile: str,
    style_profile_version: str,
) -> dict[str, Any]:
    rendered = render_translation_window_request_v1(
        style_profile=style_profile,
        style_profile_version=style_profile_version,
        measured_arm=False,
        translator_pack_bytes=_bytes(pack),
        address_anchor_bytes=_bytes(anchor),
        window_slice_bytes=_bytes(window),
        chapter=chapter,
        accepted_tail_translations=accepted_tail,
        allow_planning_only=bool(pack.get("planning_only")),
    )
    return {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }


def _upper(value: Mapping[str, Any]) -> int:
    result = value.get("conservative_prompt_token_upper_bound")
    if not isinstance(result, int) or isinstance(result, bool):
        raise B4TranslatorPackError("transport estimate lacks an upper bound")
    return result


def _bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _fresh(path: Path) -> Path:
    output = Path(path).resolve()
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    return output


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot load JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"JSON object required: {path}")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
