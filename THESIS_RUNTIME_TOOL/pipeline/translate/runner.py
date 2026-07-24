from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any

from pipeline.agents.llm_client import LLMResult, estimate_prompt_tokens
from pipeline.translate.d2l_soft_glossary_v1 import (
    OVERRIDE_MATCH_RULE_ID,
    POLICY_ID as D2L_SOFT_GLOSSARY_POLICY_ID,
    TERM_OVERRIDES_KEY,
    injected_override_preferences,
    injected_override_sources,
    split_term_override_metadata,
)
from pipeline.translate.d2l_protected_span_policies import (
    ProtectedSpanPolicy,
    get_protected_span_policy,
)
from pipeline.translate.d2l_prompt_json_envelope_v1 import (
    POLICY_ID as D2L_PROMPT_JSON_ENVELOPE_V1_POLICY_ID,
    normalize_prompt_json_envelope as normalize_prompt_json_envelope_v1,
)
from pipeline.translate.d2l_prompt_json_envelope_v2 import (
    POLICY_ID as D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    normalize_prompt_json_envelope as normalize_prompt_json_envelope_v2,
)
from pipeline.translate.d2l_translation_slots_v1 import (
    GLOSSARY_REVIEW_MATCH_RULE_ID,
    GLOSSARY_REVIEW_POLICY_ID,
    POLICY_ID as D2L_TRANSLATION_SLOTS_POLICY_ID,
    PROMPT_VERSION as D2L_TRANSLATION_SLOTS_PROMPT_VERSION,
    PROTECTED_LEXICAL_GLOSSARY_REVIEW_MATCH_RULE_ID,
    PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID,
    TRANSLATIONS_KEY,
    build_slot_map,
    extract_slot_translations,
    glossary_review_rows,
    glossary_review_summary,
    invert_slot_map,
    parse_slot_json_text,
    slot_reask_note,
    slotize_blocks,
)
from pipeline.translate.hygiene import (
    HygieneIssue,
    detect_hygiene_issues,
    hygiene_reask_note,
)
from pipeline.translate.d2l_translation_integrity_v1 import (
    inspect_translations,
    render_retry_note as integrity_reask_note,
    retry_findings as integrity_retry_findings,
    warning_findings as integrity_warning_findings,
)
from pipeline.translate.prompt import (
    build_messages,
    extract_translations,
    prompt_version_for_config,
)
from pipeline.translate.profiles import get_profile
from pipeline.translate.run_events import NullEventSink, emit_event


@dataclass(frozen=True)
class WindowRunReport:
    window_id: str
    status: str
    calls: int
    block_count: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float | None
    incremental_cost_usd: float | None
    from_cache: bool
    system_fingerprint: str | None
    errors: list[str]
    term_overrides: int = 0
    glossary_reviews: int = 0


@dataclass(frozen=True)
class TranslateReport:
    experiment_id: str
    config: str
    windows_total: int
    windows_translated: int
    windows_failed: int
    windows_skipped: int
    blocks_translated: int
    blocks_failed: int
    json_fail_rate: float
    total_usage: dict[str, int | float | None | str]
    context_stats: dict[str, int]
    hygiene: dict[str, Any]
    model: str
    seed: int
    system_fingerprint: str | None
    reports: list[WindowRunReport]
    transport_identity: str | None = None
    terminology: dict[str, Any] | None = None
    protected_spans: dict[str, Any] | None = None
    translation_output: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "experiment_id": self.experiment_id,
            "config": self.config,
            "windows_total": self.windows_total,
            "windows_translated": self.windows_translated,
            "windows_failed": self.windows_failed,
            "windows_skipped": self.windows_skipped,
            "blocks_translated": self.blocks_translated,
            "blocks_failed": self.blocks_failed,
            "json_fail_rate": self.json_fail_rate,
            "total_usage": self.total_usage,
            "context_stats": self.context_stats,
            "hygiene": self.hygiene,
            "model": self.model,
            "seed": self.seed,
            "system_fingerprint": self.system_fingerprint,
            "reports": [
                _window_report_payload(
                    report,
                    include_terminology=self.terminology is not None,
                    include_translation_output=self.translation_output is not None,
                )
                for report in self.reports
            ],
        }
        if self.transport_identity is not None:
            payload["shared_backend_used"] = True
            payload["transport_identity"] = self.transport_identity
        if self.terminology is not None:
            payload["terminology"] = self.terminology
        if self.protected_spans is not None:
            payload["protected_spans"] = self.protected_spans
        if self.translation_output is not None:
            payload["translation_output"] = self.translation_output
        return payload


def _window_report_payload(
    report: WindowRunReport,
    *,
    include_terminology: bool,
    include_translation_output: bool,
) -> dict[str, Any]:
    payload = asdict(report)
    if not include_terminology:
        payload.pop("term_overrides", None)
    if not include_translation_output:
        payload.pop("glossary_reviews", None)
    return payload


def translate_windows(
    db: sqlite3.Connection,
    windows: list,
    client: Any,
    experiment_id: str,
    config: str = "S0",
    context_builder: Any | None = None,
    context_budget_tokens: int = 1500,
    profile_name: str = "literary_v1",
    event_sink: Any | None = None,
    protected_spans_policy: str | None = None,
    translation_output_policy: str | None = None,
    response_envelope_policy: str | None = None,
    max_attempts_per_window: int = 2,
) -> TranslateReport:
    """Run translation over a list of Window objects.

    Persists to translation_runs (1 row/block) and memory_packs (1 row/window).
    Resume: windows where every block already has a draft run are skipped.
    """

    reports: list[WindowRunReport] = []
    translated = 0
    failed = 0
    skipped = 0
    all_results: list[LLMResult] = []
    context_stats = {
        "windows_with_context": 0,
        "windows_low_context": 0,
        "dropped_by_budget": 0,
    }
    sink = event_sink or NullEventSink()
    config = config.upper()
    if max_attempts_per_window not in {1, 2}:
        raise ValueError("Translator max attempts per window must be one or two")
    hygiene_stats = _empty_hygiene_stats(config)
    profile = get_profile(profile_name)
    protected_policy = get_protected_span_policy(protected_spans_policy)
    protected_spans_active = protected_policy is not None
    if protected_spans_active and not (
        config in {"S0", "S1"} and profile.name == "technical_d2l_v1"
    ):
        raise ValueError(
            "Protected spans are only supported for technical D2L S0/S1"
        )
    slot_output_active = (
        translation_output_policy == D2L_TRANSLATION_SLOTS_POLICY_ID
    )
    if translation_output_policy is not None and not slot_output_active:
        raise ValueError(
            f"Unknown translation output policy: {translation_output_policy}"
        )
    if slot_output_active and not (
        config in {"S0", "S1"}
        and profile.name == "technical_d2l_v1"
        and protected_spans_active
    ):
        raise ValueError(
            "Translation slots require technical D2L S0/S1 with protected spans"
        )
    if response_envelope_policy not in {
        None,
        D2L_PROMPT_JSON_ENVELOPE_V1_POLICY_ID,
        D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    }:
        raise ValueError(
            f"Unknown Translator response-envelope policy: {response_envelope_policy}"
        )
    if response_envelope_policy is not None and not slot_output_active:
        raise ValueError(
            "Prompt-JSON envelope normalization requires translation slots"
        )
    if (
        protected_policy is not None
        and protected_policy.hides_source_bytes
        and not slot_output_active
    ):
        raise ValueError(
            "Opaque LaTeX protection requires the translation-slot output policy"
        )
    prompt_version = (
        (
            protected_policy.translation_slots_prompt_version
            if protected_policy is not None
            else D2L_TRANSLATION_SLOTS_PROMPT_VERSION
        )
        if slot_output_active
        else (
            protected_policy.prompt_version
            if protected_policy is not None
            else prompt_version_for_config(config, profile.name)
        )
    )
    transport_identity = _transport_identity(client, config)
    soft_glossary_active = config == "S1" and profile.name == "technical_d2l_v1"
    legacy_override_active = soft_glossary_active and not slot_output_active
    protected_lexical_active = bool(
        protected_policy is not None
        and protected_policy.lexical_source_blocks is not None
    )
    glossary_review_policy_id = (
        PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID
        if protected_lexical_active
        else GLOSSARY_REVIEW_POLICY_ID
    )
    glossary_review_match_rule_id = (
        PROTECTED_LEXICAL_GLOSSARY_REVIEW_MATCH_RULE_ID
        if protected_lexical_active
        else None
    )
    terminology_stats = _empty_terminology_stats(legacy_override_active)
    protected_span_stats = _empty_protected_span_stats(
        protected_policy,
        effective_prompt_version=prompt_version,
    )
    translation_output_stats = _empty_translation_output_stats(
        slot_output_active,
        glossary_review_policy_id=glossary_review_policy_id,
        response_envelope_policy=response_envelope_policy,
    )

    try:
        for window in windows:
            window_id = window.window_id
            block_ids = list(window.block_ids)
            pack_id = _pack_id(
                experiment_id=experiment_id,
                config=config,
                window_id=window_id,
                transport_identity=transport_identity,
            )
            emit_event(
                sink,
                "window_started",
                experiment_id=experiment_id,
                config=config,
                profile=profile.name,
                window_id=window_id,
                block_ids=block_ids,
                estimated_source_tokens=int(getattr(window, "est_src_tokens", 0) or 0),
            )

            # --- Resume check ---
            already_done = _blocks_already_run(
                db,
                experiment_id,
                config,
                block_ids,
                expected_pack_id=pack_id,
                expected_transport_identity=transport_identity,
                expected_prompt_version=prompt_version,
                expected_terminology_policy=(
                    D2L_SOFT_GLOSSARY_POLICY_ID if soft_glossary_active else None
                ),
                expected_override_match_rule=(
                    OVERRIDE_MATCH_RULE_ID if legacy_override_active else None
                ),
                expected_protected_spans_policy=(
                    protected_policy.policy_id if protected_policy is not None else None
                ),
                expected_translation_output_policy=(
                    D2L_TRANSLATION_SLOTS_POLICY_ID
                    if slot_output_active
                    else None
                ),
                expected_glossary_review_policy=(
                    glossary_review_policy_id if slot_output_active else None
                ),
                expected_response_envelope_policy=(
                    response_envelope_policy if slot_output_active else None
                ),
            )
            if already_done == len(block_ids):
                emit_event(
                    sink,
                    "window_skipped",
                    experiment_id=experiment_id,
                    config=config,
                    window_id=window_id,
                    block_ids=block_ids,
                    reason="resume_all_blocks_present",
                    committed=True,
                )
                reports.append(
                    WindowRunReport(
                        window_id=window_id,
                        status="skipped",
                        calls=0,
                        block_count=len(block_ids),
                        prompt_tokens=0,
                        completion_tokens=0,
                        reasoning_tokens=0,
                        cost_usd=0.0,
                        incremental_cost_usd=0.0,
                        from_cache=False,
                        system_fingerprint=None,
                        errors=[],
                    )
                )
                skipped += 1
                continue

            # --- Fetch block data ---
            block_rows = _fetch_blocks(db, block_ids)
            block_map = {str(row["block_id"]): dict(row) for row in block_rows}
            blocks_for_prompt = [block_map[bid] for bid in block_ids if bid in block_map]
            protection_plan: Any | None = None
            context_blocks = blocks_for_prompt
            lexical_blocks = blocks_for_prompt
            slot_to_block: dict[str, str] = {}
            prompt_blocks = blocks_for_prompt
            unchanged_allowed_block_ids: set[str] = set()
            fixed_only_protected_translations: dict[str, str] = {}
            if protected_spans_active:
                assert protected_policy is not None
                protection_plan = protected_policy.protect_blocks(blocks_for_prompt)
                prompt_blocks = protection_plan.protected_blocks
                if protected_policy.context_source_blocks is not None:
                    context_blocks = protected_policy.context_source_blocks(
                        protection_plan
                    )
                if protected_policy.lexical_source_blocks is not None:
                    lexical_blocks = protected_policy.lexical_source_blocks(
                        protection_plan
                    )
                if protected_policy.fixed_only_block_ids is not None:
                    unchanged_allowed_block_ids = (
                        protected_policy.fixed_only_block_ids(protection_plan)
                    )
                if (
                    protected_policy.fixed_only_protected_translations
                    is not None
                ):
                    fixed_only_protected_translations = (
                        protected_policy.fixed_only_protected_translations(
                            protection_plan
                        )
                    )
                emit_event(
                    sink,
                    "protected_spans_prepared",
                    experiment_id=experiment_id,
                    config=config,
                    window_id=window_id,
                    block_ids=block_ids,
                    metadata=protection_plan.metadata(),
                    committed=False,
                )
            if slot_output_active:
                slot_to_block = build_slot_map(block_ids)
                prompt_blocks = slotize_blocks(prompt_blocks, slot_to_block)

            context_pack = None
            if config == "S1":
                context_pack = _build_context_pack_for_window(
                    db,
                    window,
                    context_blocks,
                    context_builder=context_builder,
                    budget_tokens=context_budget_tokens,
                    profile_name=profile.name,
                )
                context_stats["windows_with_context"] += 1
                if bool(getattr(context_pack, "low_context", False)):
                    context_stats["windows_low_context"] += 1
                context_stats["dropped_by_budget"] += len(
                    getattr(context_pack, "dropped_by_budget", []) or []
                )
                _raise_if_context_dropped_by_budget(window_id, context_pack)
            allowed_override_sources = (
                injected_override_sources(context_pack)
                if soft_glossary_active
                else set()
            )
            allowed_override_preferences = (
                injected_override_preferences(context_pack)
                if soft_glossary_active
                else {}
            )

            messages = build_messages(
                prompt_blocks,
                prompt_version=prompt_version,
                config=config,
                context_pack=context_pack,
                profile_name=profile.name,
                protected_span_legend=(
                    protection_plan.prompt_legend(
                        invert_slot_map(slot_to_block)
                        if slot_output_active
                        else None
                    )
                    if protection_plan is not None
                    else None
                ),
                translation_output_policy=translation_output_policy,
            )
            emit_event(
                sink,
                "prompt_built",
                experiment_id=experiment_id,
                config=config,
                profile=profile.name,
                window_id=window_id,
                block_ids=block_ids,
                prompt_version=prompt_version,
                prompt_hash=_messages_hash(messages),
                prompt_tokens_est=estimate_prompt_tokens(
                    messages, response_format={"type": "json_object"}
                ),
                messages_summary=_messages_summary(messages),
                context_summary=_context_pack_summary(context_pack),
                pack_summary=_pack_summary_for_event(
                    context_pack,
                    terminology_policy=(
                        D2L_SOFT_GLOSSARY_POLICY_ID
                        if soft_glossary_active
                        else None
                    ),
                ),
                committed=False,
            )

            # --- Call with re-ask ---
            (
                result,
                status,
                errors,
                hygiene_issues,
                call_results,
                hygiene_summary,
                protected_summary,
                deterministic_summary,
            ) = _call_with_reask(
                client,
                messages,
                window_id,
                block_ids,
                config,
                blocks_for_prompt=blocks_for_prompt,
                event_sink=sink,
                allow_term_overrides=legacy_override_active,
                allowed_override_sources=allowed_override_sources,
                allowed_override_preferences=allowed_override_preferences,
                protection_plan=protection_plan,
                protected_span_policy=protected_policy,
                unchanged_allowed_block_ids=unchanged_allowed_block_ids,
                fixed_only_protected_translations=(
                    fixed_only_protected_translations
                ),
                slot_to_block=slot_to_block or None,
                response_envelope_policy=response_envelope_policy,
                max_attempts=max_attempts_per_window,
            )
            all_results.extend(call_results)
            _record_response_envelope_stats(
                translation_output_stats,
                call_results,
                response_envelope_policy=response_envelope_policy,
            )
            _merge_hygiene_stats(hygiene_stats, config, hygiene_summary)
            _merge_protected_span_stats(
                protected_span_stats,
                plan=protection_plan,
                summary=protected_summary,
                status=status,
            )

            (
                translations,
                term_overrides,
                override_reporting_present,
                parse_errors,
            ) = _extract_translator_output(
                result.parsed_json,
                block_ids,
                allow_term_overrides=legacy_override_active,
                allowed_override_sources=allowed_override_sources,
                allowed_override_preferences=allowed_override_preferences,
                slot_to_block=slot_to_block or None,
            )
            _record_terminology_stats(
                terminology_stats,
                reporting_present=override_reporting_present,
                overrides=term_overrides,
            )
            glossary_reviews = (
                glossary_review_rows(
                    lexical_blocks,
                    translations,
                    context_pack,
                    policy_id=glossary_review_policy_id,
                    match_rule_id=(
                        glossary_review_match_rule_id
                        or GLOSSARY_REVIEW_MATCH_RULE_ID
                    ),
                )
                if slot_output_active and status == "translated"
                else []
            )
            _record_translation_output_stats(
                translation_output_stats,
                glossary_reviews,
            )
            emit_event(
                sink,
                "json_parsed",
                experiment_id=experiment_id,
                config=config,
                window_id=window_id,
                block_ids=block_ids,
                status=status,
                translated_blocks=sorted(translations.keys()),
                term_override_count=len(term_overrides),
                term_override_reporting_present=override_reporting_present,
                glossary_review_count=len(glossary_reviews),
                errors=errors or parse_errors,
                committed=False,
            )

            if status == "failed":
                failed += 1
            else:
                translated += 1
                emit_event(
                    sink,
                    "window_preview_available",
                    experiment_id=experiment_id,
                    config=config,
                    window_id=window_id,
                    block_ids=block_ids,
                    translations=_bounded_translations(translations),
                    committed=False,
                )

            # --- Persist ---
            model_name = str(getattr(client.config, "model", "") if hasattr(client, "config") else "")
            temperature = float(getattr(client.config, "temperature", 0.3) if hasattr(client, "config") else 0.3)
            seed = int(getattr(client.config, "seed", 0) if hasattr(client, "config") else 0)

            _persist_pack(
                db,
                pack_id,
                window_id,
                block_ids,
                config,
                messages,
                result,
                experiment_id=experiment_id,
                prompt_version=prompt_version,
                context_pack=context_pack,
                blocks_for_prompt=prompt_blocks,
                profile_name=profile.name,
                transport_identity=transport_identity,
                term_overrides=term_overrides,
                term_override_reporting_present=override_reporting_present,
                terminology_policy=(
                    D2L_SOFT_GLOSSARY_POLICY_ID if soft_glossary_active else None
                ),
                term_override_match_rule=(
                    OVERRIDE_MATCH_RULE_ID if legacy_override_active else None
                ),
                protected_span_metadata=(
                    protection_plan.metadata()
                    if protection_plan is not None
                    else None
                ),
                translation_output_policy=(
                    D2L_TRANSLATION_SLOTS_POLICY_ID
                    if slot_output_active
                    else None
                ),
                slot_map=slot_to_block or None,
                glossary_review_policy=(
                    glossary_review_policy_id if slot_output_active else None
                ),
                glossary_reviews=glossary_reviews,
                response_envelope_policy=(
                    response_envelope_policy if slot_output_active else None
                ),
                call_count=len(call_results),
                deterministic_quality=deterministic_summary,
            )

            persisted_blocks: list[str] = []
            if status == "translated":
                for block_id, translation in translations.items():
                    run_id = _translation_run_id(
                        experiment_id=experiment_id,
                        config=config,
                        block_id=block_id,
                        transport_identity=transport_identity,
                    )
                    _persist_run(
                        db, run_id, experiment_id, block_id, config, "draft",
                        window_id, pack_id, translation,
                        model_name, prompt_version, temperature, seed, result,
                    )
                    _persist_hygiene_issues(
                        db,
                        run_id,
                        block_id,
                        [issue for issue in hygiene_issues if issue.block_id == block_id],
                    )
                    persisted_blocks.append(block_id)

            emit_event(
                sink,
                "persist_buffered",
                experiment_id=experiment_id,
                config=config,
                window_id=window_id,
                block_ids=block_ids,
                pack_id=pack_id,
                persisted_blocks=persisted_blocks,
                committed=False,
            )

            reports.append(
                WindowRunReport(
                    window_id=window_id,
                    status=status,
                    calls=len(call_results),
                    block_count=len(block_ids),
                    prompt_tokens=sum(r.usage.prompt_tokens for r in call_results),
                    completion_tokens=sum(r.usage.completion_tokens for r in call_results),
                    reasoning_tokens=sum(r.usage.reasoning_tokens for r in call_results),
                    cost_usd=_sum_cost(call_results),
                    incremental_cost_usd=_sum_cost(
                        call_results, incremental_only=True
                    ),
                    from_cache=all(r.from_cache for r in call_results),
                    system_fingerprint=_last_fingerprint(call_results),
                    errors=errors,
                    term_overrides=len(term_overrides),
                    glossary_reviews=len(glossary_reviews),
                )
            )

        total_windows = len(windows)
        model_name = str(getattr(client.config, "model", "") if hasattr(client, "config") else "")
        seed = int(getattr(client.config, "seed", 0) if hasattr(client, "config") else 0)

        report = TranslateReport(
            experiment_id=experiment_id,
            config=config,
            windows_total=total_windows,
            windows_translated=translated,
            windows_failed=failed,
            windows_skipped=skipped,
            blocks_translated=sum(r.block_count for r in reports if r.status == "translated"),
            blocks_failed=sum(r.block_count for r in reports if r.status == "failed"),
            json_fail_rate=failed / total_windows if total_windows else 0.0,
            total_usage=_total_usage(all_results),
            context_stats=context_stats,
            hygiene=hygiene_stats,
            model=model_name,
            seed=seed,
            system_fingerprint=_last_fingerprint(all_results),
            reports=reports,
            transport_identity=transport_identity,
            terminology=(
                terminology_stats if legacy_override_active else None
            ),
            protected_spans=(
                protected_span_stats if protected_spans_active else None
            ),
            translation_output=(
                translation_output_stats if slot_output_active else None
            ),
        )

        db.commit()
        emit_event(
            sink,
            "run_committed",
            experiment_id=experiment_id,
            config=config,
            profile=profile.name,
            committed=True,
            report=report.to_json_dict(),
        )
        return report
    except Exception as exc:
        emit_event(
            sink,
            "run_failed",
            experiment_id=experiment_id,
            config=config,
            profile=profile.name,
            error_type=type(exc).__name__,
            error=str(exc),
            committed=False,
        )
        raise


def _build_context_pack_for_window(
    db: sqlite3.Connection,
    window: Any,
    blocks_for_prompt: list[dict[str, Any]],
    *,
    context_builder: Any | None,
    budget_tokens: int,
    profile_name: str,
) -> Any:
    if context_builder is not None:
        return context_builder(db, window, blocks_for_prompt)

    from pipeline.retrieval.context_builder import build_context_pack, plan_anchors

    anchors = plan_anchors(db, blocks_for_prompt, profile_name=profile_name)
    return build_context_pack(db, window, anchors, budget_tokens=budget_tokens)


def _raise_if_context_dropped_by_budget(window_id: str, context_pack: Any | None) -> None:
    dropped = _context_dropped_by_budget(context_pack)
    if not dropped:
        return
    sample = ", ".join(
        f"{item.get('item_id') or '?'}:{item.get('line') or ''}"[:160]
        for item in dropped[:8]
    )
    extra = "" if len(dropped) <= 8 else f"; +{len(dropped) - 8} more"
    raise RuntimeError(
        f"Context budget fuse tripped for window {window_id}: "
        f"{len(dropped)} dropped_by_budget item(s): {sample}{extra}"
    )


def _call_with_reask(
    client: Any,
    messages: list[dict],
    window_id: str,
    block_ids: list[str],
    config: str,
    *,
    blocks_for_prompt: list[dict[str, Any]],
    event_sink: Any | None = None,
    allow_term_overrides: bool = False,
    allowed_override_sources: set[str] | None = None,
    allowed_override_preferences: dict[str, set[str]] | None = None,
    protection_plan: Any | None = None,
    protected_span_policy: ProtectedSpanPolicy | None = None,
    unchanged_allowed_block_ids: set[str] | None = None,
    fixed_only_protected_translations: dict[str, str] | None = None,
    slot_to_block: dict[str, str] | None = None,
    response_envelope_policy: str | None = None,
    max_attempts: int = 2,
) -> tuple[
    LLMResult,
    str,
    list[str],
    list[HygieneIssue],
    list[LLMResult],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Call LLM; re-ask once on JSON or hygiene validation failure."""
    if max_attempts not in {1, 2}:
        raise ValueError("Translator call attempts must be one or two")
    call_results: list[LLMResult] = []
    hygiene_flagged_blocks: set[str] = set()
    hygiene_reasked_blocks: set[str] = set()
    final_hygiene_issues: list[HygieneIssue] = []
    protected_flagged_blocks: set[str] = set()
    protected_reasked_blocks: set[str] = set()
    final_protected_issues: list[Any] = []
    deterministic_flagged_blocks: set[str] = set()
    deterministic_reasked_blocks: set[str] = set()
    deterministic_warnings: list[Any] = []
    final_deterministic_issues: list[Any] = []
    retry_history: list[dict[str, Any]] = []
    errors: list[str] = []
    for attempt in range(max_attempts):
        emit_event(
            event_sink,
            "request_sent",
            config=config,
            window_id=window_id,
            block_ids=block_ids,
            attempt=attempt + 1,
            tag=f"{config}_{window_id}",
            prompt_hash=_messages_hash(messages),
            prompt_tokens_est=estimate_prompt_tokens(
                messages, response_format={"type": "json_object"}
            ),
            committed=False,
        )
        result = client.call(
            messages,
            response_format={"type": "json_object"},
            tag=f"{config}_{window_id}",
        )
        call_results.append(result)
        emit_event(
            event_sink,
            "response_received",
            config=config,
            window_id=window_id,
            block_ids=block_ids,
            attempt=attempt + 1,
            cache_key=result.cache_key,
            from_cache=result.from_cache,
            model=result.model,
            system_fingerprint=result.system_fingerprint,
            usage={
                "prompt_tokens": result.usage.prompt_tokens,
                "cached_tokens": result.usage.cached_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "reasoning_tokens": result.usage.reasoning_tokens,
            },
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            json_error=result.json_error,
            committed=False,
        )
        response_text_for_validation = result.text
        envelope_normalized = False
        response_envelope_normalizer = _response_envelope_normalizer(
            response_envelope_policy
        )
        if response_envelope_normalizer is not None:
            response_text_for_validation, envelope_normalized = (
                response_envelope_normalizer(result.text)
            )
            if envelope_normalized:
                emit_event(
                    event_sink,
                    "response_envelope_normalized",
                    config=config,
                    window_id=window_id,
                    block_ids=block_ids,
                    attempt=attempt + 1,
                    policy_id=response_envelope_policy,
                    committed=False,
                )
        translations, _, _, parse_errors = _extract_translator_output(
            result.parsed_json,
            block_ids,
            allow_term_overrides=allow_term_overrides,
            allowed_override_sources=allowed_override_sources or set(),
            allowed_override_preferences=allowed_override_preferences or {},
            slot_to_block=slot_to_block,
            raw_text=response_text_for_validation,
        )
        for block_id, protected_target in (
            fixed_only_protected_translations or {}
        ).items():
            if block_id in translations:
                translations[block_id] = protected_target
        if envelope_normalized and not parse_errors:
            normalized_payload, normalized_errors = parse_slot_json_text(
                response_text_for_validation
            )
            if not normalized_errors:
                result = replace(
                    result,
                    parsed_json=normalized_payload,
                    json_error=None,
                )

        if not parse_errors and len(translations) == len(block_ids):
            if protection_plan is not None:
                if protected_span_policy is None:
                    raise RuntimeError(
                        "Protection plan is missing its versioned policy handlers"
                    )
                restored, protected_issues = protected_span_policy.restore_translations(
                    translations,
                    protection_plan,
                )
                if protected_issues:
                    issue_blocks = {
                        issue.block_id for issue in protected_issues
                    }
                    protected_flagged_blocks.update(issue_blocks)
                    if attempt + 1 < max_attempts:
                        protected_reasked_blocks.update(issue_blocks)
                        retry_history.extend(
                            _mechanical_retry_history(
                                "protected_content", protected_issues
                            )
                        )
                        emit_event(
                            event_sink,
                            "protected_spans_flagged",
                            config=config,
                            window_id=window_id,
                            block_ids=block_ids,
                            flagged_blocks=sorted(issue_blocks),
                            issues=[
                                issue.to_dict() for issue in protected_issues
                            ],
                            reask=True,
                            committed=False,
                        )
                        messages = [
                            *messages,
                            {"role": "assistant", "content": result.text},
                            {
                                "role": "user",
                                "content": protected_span_policy.reask_note(
                                    protected_issues,
                                    invert_slot_map(slot_to_block)
                                    if slot_to_block
                                    else None,
                                ),
                            },
                        ]
                        continue

                    final_protected_issues = protected_issues
                    errors = [
                        f"protected_span:{issue.block_id}:{issue.issue_type}"
                        for issue in final_protected_issues
                    ]
                    emit_event(
                        event_sink,
                        "protected_spans_still_bad",
                        config=config,
                        window_id=window_id,
                        block_ids=block_ids,
                        flagged_blocks=sorted(issue_blocks),
                        issues=[
                            issue.to_dict() for issue in final_protected_issues
                        ],
                        committed=False,
                    )
                    return (
                        result,
                        "failed",
                        errors,
                        [],
                        call_results,
                        _hygiene_call_summary(
                            hygiene_flagged_blocks,
                            hygiene_reasked_blocks,
                            [],
                            [],
                        ),
                        _protected_span_call_summary(
                            protected_flagged_blocks,
                            protected_reasked_blocks,
                            final_protected_issues,
                        ),
                        _deterministic_call_summary(
                            deterministic_flagged_blocks,
                            deterministic_reasked_blocks,
                            deterministic_warnings,
                            final_deterministic_issues,
                            retry_history,
                        ),
                    )

                result = _result_with_restored_translations(
                    result,
                    restored,
                    slot_to_block=slot_to_block,
                )
                translations = restored

            deterministic_findings = inspect_translations(
                blocks_for_prompt,
                translations,
            )
            deterministic_major = integrity_retry_findings(
                deterministic_findings
            )
            unchanged_allowed = unchanged_allowed_block_ids or set()
            deterministic_major = [
                finding
                for finding in deterministic_major
                if not (
                    finding.block_id in unchanged_allowed
                    and finding.issue_type
                    in {"target_equals_source", "untranslated_heading"}
                )
            ]
            deterministic_warnings = integrity_warning_findings(
                deterministic_findings
            )
            if deterministic_major:
                issue_blocks = {
                    issue.block_id for issue in deterministic_major
                }
                deterministic_flagged_blocks.update(issue_blocks)
                if attempt + 1 < max_attempts:
                    deterministic_reasked_blocks.update(issue_blocks)
                    retry_history.extend(
                        _mechanical_retry_history(
                            "deterministic_integrity", deterministic_major
                        )
                    )
                    emit_event(
                        event_sink,
                        "deterministic_quality_flagged",
                        config=config,
                        window_id=window_id,
                        block_ids=block_ids,
                        flagged_blocks=sorted(issue_blocks),
                        findings=[
                            issue.to_dict() for issue in deterministic_findings
                        ],
                        reask=True,
                        committed=False,
                    )
                    messages = [
                        *messages,
                        {"role": "assistant", "content": result.text},
                        {
                            "role": "user",
                            "content": integrity_reask_note(
                                deterministic_major,
                                block_to_slot=(
                                    invert_slot_map(slot_to_block)
                                    if slot_to_block
                                    else None
                                ),
                            ),
                        },
                    ]
                    continue
                final_deterministic_issues = deterministic_major
                errors = [
                    f"deterministic:{issue.block_id}:{issue.issue_type}"
                    for issue in deterministic_major
                ]
                emit_event(
                    event_sink,
                    "deterministic_quality_still_bad",
                    config=config,
                    window_id=window_id,
                    block_ids=block_ids,
                    flagged_blocks=sorted(issue_blocks),
                    findings=[
                        issue.to_dict() for issue in deterministic_findings
                    ],
                    committed=False,
                )
                return (
                    result,
                    "failed",
                    errors,
                    [],
                    call_results,
                    _hygiene_call_summary(
                        hygiene_flagged_blocks,
                        hygiene_reasked_blocks,
                        [],
                        [],
                    ),
                    _protected_span_call_summary(
                        protected_flagged_blocks,
                        protected_reasked_blocks,
                        final_protected_issues,
                    ),
                    _deterministic_call_summary(
                        deterministic_flagged_blocks,
                        deterministic_reasked_blocks,
                        deterministic_warnings,
                        final_deterministic_issues,
                        retry_history,
                    ),
                )

            issues = detect_hygiene_issues(blocks_for_prompt, translations)
            if not issues:
                fixed_blocks = sorted(hygiene_reasked_blocks)
                return (
                    result,
                    "translated",
                    [],
                    [],
                    call_results,
                    _hygiene_call_summary(
                        hygiene_flagged_blocks,
                        hygiene_reasked_blocks,
                        fixed_blocks,
                        [],
                    ),
                    _protected_span_call_summary(
                        protected_flagged_blocks,
                        protected_reasked_blocks,
                        [],
                    ),
                    _deterministic_call_summary(
                        deterministic_flagged_blocks,
                        deterministic_reasked_blocks,
                        deterministic_warnings,
                        [],
                        retry_history,
                    ),
                )

            issue_blocks = {issue.block_id for issue in issues}
            hygiene_flagged_blocks.update(issue_blocks)
            if attempt + 1 < max_attempts:
                hygiene_reasked_blocks.update(issue_blocks)
                retry_history.extend(
                    _mechanical_retry_history("output_hygiene", issues)
                )
                emit_event(
                    event_sink,
                    "hygiene_flagged",
                    config=config,
                    window_id=window_id,
                    block_ids=block_ids,
                    flagged_blocks=sorted(issue_blocks),
                    issues=[issue.to_dict() for issue in issues],
                    reask=True,
                    committed=False,
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": result.text},
                    {
                        "role": "user",
                        "content": (
                            _slot_hygiene_reask_note(issues, slot_to_block)
                            if slot_to_block
                            else hygiene_reask_note(issues)
                        ),
                    },
                ]
                continue

            final_hygiene_issues = issues
            final_blocks = {issue.block_id for issue in final_hygiene_issues}
            fixed_blocks = sorted(hygiene_reasked_blocks - final_blocks)
            errors = [
                f"hygiene:{issue.block_id}:{issue.script}:{issue.surface}"
                for issue in final_hygiene_issues
            ]
            emit_event(
                event_sink,
                "hygiene_still_bad",
                config=config,
                window_id=window_id,
                block_ids=block_ids,
                flagged_blocks=sorted(final_blocks),
                issues=[issue.to_dict() for issue in final_hygiene_issues],
                committed=False,
            )
            return (
                result,
                "translated",
                errors,
                final_hygiene_issues,
                call_results,
                _hygiene_call_summary(
                    hygiene_flagged_blocks,
                    hygiene_reasked_blocks,
                    fixed_blocks,
                    final_hygiene_issues,
                ),
                _protected_span_call_summary(
                    protected_flagged_blocks,
                    protected_reasked_blocks,
                    final_protected_issues,
                ),
                _deterministic_call_summary(
                    deterministic_flagged_blocks,
                    deterministic_reasked_blocks,
                    deterministic_warnings,
                    final_deterministic_issues,
                    retry_history,
                ),
            )

        errors = list(parse_errors)

        if attempt + 1 < max_attempts:
            retry_history.extend(
                {
                    "source": "response_contract",
                    "scope": "window",
                    "block_id": None,
                    "issue_type": "invalid_response_contract",
                    "evidence": " ".join(str(error).split())[:240],
                }
                for error in parse_errors[:12]
            )
            messages = [
                *messages,
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": (
                        slot_reask_note(parse_errors, slot_to_block)
                        if slot_to_block
                        else (
                            f"Output errors: {'; '.join(parse_errors[:5])}. "
                            "Return a valid JSON object with all block_ids as keys and "
                            "Vietnamese translations as string values. "
                            + (
                                f"Only optional {TERM_OVERRIDES_KEY} metadata is allowed."
                                if allow_term_overrides
                                else "No extra keys."
                            )
                        )
                    ),
                },
            ]

    return (
        result,
        "failed",
        errors,
        final_hygiene_issues,
        call_results,
        _hygiene_call_summary(hygiene_flagged_blocks, hygiene_reasked_blocks, [], []),
        _protected_span_call_summary(
            protected_flagged_blocks,
            protected_reasked_blocks,
            final_protected_issues,
        ),
        _deterministic_call_summary(
            deterministic_flagged_blocks,
            deterministic_reasked_blocks,
            deterministic_warnings,
            final_deterministic_issues,
            retry_history,
        ),
    )


def _result_with_restored_translations(
    result: LLMResult,
    translations: dict[str, str],
    *,
    slot_to_block: dict[str, str] | None = None,
) -> LLMResult:
    if slot_to_block:
        payload = {
            TRANSLATIONS_KEY: {
                slot_id: translations[block_id]
                for slot_id, block_id in slot_to_block.items()
                if block_id in translations
            }
        }
        return replace(result, parsed_json=payload, json_error=None)
    payload = dict(result.parsed_json or {})
    payload.update(translations)
    return replace(result, parsed_json=payload)


def _slot_hygiene_reask_note(
    issues: list[HygieneIssue],
    slot_to_block: dict[str, str],
) -> str:
    block_to_slot = invert_slot_map(slot_to_block)
    samples = "; ".join(
        f"{block_to_slot.get(issue.block_id, issue.block_id)}:"
        f"{issue.script}:{issue.surface}"
        for issue in issues[:5]
    )
    extra = "" if len(issues) <= 5 else f"; +{len(issues) - 5} more"
    return (
        "Your previous translations contained output-only non-Vietnamese scripts "
        f"({samples}{extra}). Retranslate the same window. Keep source placeholders, "
        "names, and symbols. Return only the same translations object with exactly "
        "the same short slots and no metadata."
    )


def _deterministic_call_summary(
    flagged_blocks: set[str],
    reasked_blocks: set[str],
    warnings: list[Any],
    final_issues: list[Any],
    retry_history: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "flagged_blocks": sorted(flagged_blocks),
        "reasked_blocks": sorted(reasked_blocks),
        "warnings": [issue.to_dict() for issue in warnings],
        "final_issues": [issue.to_dict() for issue in final_issues],
        "retry_history": list(retry_history),
    }


def _mechanical_retry_history(
    source: str,
    issues: list[Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for issue in issues:
        payload = issue.to_dict() if hasattr(issue, "to_dict") else {}
        block_id = payload.get("block_id")
        issue_type = payload.get("issue_type")
        if not issue_type and source == "output_hygiene":
            issue_type = "unexpected_output_script"
        evidence = (
            payload.get("evidence_target")
            or payload.get("surface")
            or payload.get("observed")
            or ""
        )
        rows.append(
            {
                "source": source,
                "scope": "block" if block_id else "window",
                "block_id": None if block_id is None else str(block_id),
                "issue_type": str(issue_type or "mechanical_contract_error"),
                "evidence": " ".join(str(evidence).split())[:240],
            }
        )
    return rows


def _protected_span_call_summary(
    flagged_blocks: set[str],
    reasked_blocks: set[str],
    final_issues: list[Any],
) -> dict[str, Any]:
    return {
        "flagged_blocks": sorted(flagged_blocks),
        "reasked_blocks": sorted(reasked_blocks),
        "final_issues": [issue.to_dict() for issue in final_issues],
    }


def _blocks_already_run(
    db: sqlite3.Connection,
    experiment_id: str,
    config: str,
    block_ids: list[str],
    *,
    expected_pack_id: str,
    expected_transport_identity: str | None = None,
    expected_prompt_version: str,
    expected_terminology_policy: str | None,
    expected_override_match_rule: str | None,
    expected_protected_spans_policy: str | None,
    expected_translation_output_policy: str | None,
    expected_glossary_review_policy: str | None,
    expected_response_envelope_policy: str | None,
) -> int:
    if not block_ids:
        return 0
    placeholders = ",".join("?" * len(block_ids))
    rows = db.execute(
        f"""
        SELECT tr.run_id, tr.block_id, tr.pack_id,
               tr.prompt_version AS run_prompt_version,
               mp.prompt_version AS pack_prompt_version,
               mp.payload_json
        FROM translation_runs AS tr
        LEFT JOIN memory_packs AS mp ON mp.pack_id = tr.pack_id
        WHERE tr.experiment_id = ? AND tr.config = ? AND tr.stage = 'draft'
          AND tr.block_id IN ({placeholders})
        """,
        [experiment_id, config] + block_ids,
    ).fetchall()
    prompt_or_policy_conflicts = sorted(
        str(row["block_id"])
        for row in rows
        if (
            str(row["run_prompt_version"] or "") != expected_prompt_version
            or (
                row["pack_id"] is not None
                and str(row["pack_prompt_version"] or "") != expected_prompt_version
            )
            or _stored_terminology_policy(row["payload_json"])
            != expected_terminology_policy
            or _stored_protected_spans_policy(row["payload_json"])
            != expected_protected_spans_policy
            or _stored_override_match_rule(row["payload_json"])
            != expected_override_match_rule
            or _stored_translation_output_policy(row["payload_json"])
            != expected_translation_output_policy
            or _stored_glossary_review_policy(row["payload_json"])
            != expected_glossary_review_policy
            or _stored_response_envelope_policy(row["payload_json"])
            != expected_response_envelope_policy
        )
    )
    if prompt_or_policy_conflicts:
        raise RuntimeError(
            "Translator resume prompt/policy conflicts with existing rows: "
            + ", ".join(prompt_or_policy_conflicts)
        )
    if expected_transport_identity is None:
        shared_rows = sorted(
            str(row["block_id"])
            for row in rows
            if _stored_transport_identity(row["payload_json"]) is not None
        )
        if shared_rows:
            raise RuntimeError(
                "Legacy Translator cannot resume shared-backend rows: "
                + ", ".join(shared_rows)
            )

    unscoped_legacy_rows = sorted(
        str(row["block_id"])
        for row in rows
        if (
            expected_transport_identity is None
            and (
                str(row["run_id"] or "")
                != _translation_run_id(
                    experiment_id=experiment_id,
                    config=config,
                    block_id=str(row["block_id"]),
                    transport_identity=None,
                )
                or str(row["pack_id"] or "") != expected_pack_id
            )
        )
    )
    if unscoped_legacy_rows:
        raise RuntimeError(
            "Translator historical unscoped resume rows cannot be reused: "
            + ", ".join(unscoped_legacy_rows)
        )

    conflicting = sorted(
        str(row["block_id"])
        for row in rows
        if (
            str(row["run_id"] or "")
            != _translation_run_id(
                experiment_id=experiment_id,
                config=config,
                block_id=str(row["block_id"]),
                transport_identity=expected_transport_identity,
            )
            or str(row["pack_id"] or "") != expected_pack_id
            or _stored_transport_identity(row["payload_json"])
            != expected_transport_identity
            or (
                _stored_experiment_id(row["payload_json"]) is not None
                and _stored_experiment_id(row["payload_json"]) != experiment_id
            )
        )
    )
    if conflicting:
        raise RuntimeError(
            "Translator resume identity conflicts with existing rows: "
            + ", ".join(conflicting)
        )
    return len({str(row["block_id"]) for row in rows})


def _extract_translator_output(
    parsed_json: dict[str, Any] | None,
    expected_block_ids: list[str],
    *,
    allow_term_overrides: bool,
    allowed_override_sources: set[str],
    allowed_override_preferences: dict[str, set[str]],
    slot_to_block: dict[str, str] | None = None,
    raw_text: str | None = None,
) -> tuple[dict[str, str], list[dict[str, str]], bool, list[str]]:
    if slot_to_block is not None:
        if allow_term_overrides:
            raise ValueError("Translation slots cannot allow model-authored overrides")
        slot_payload = parsed_json
        if raw_text is not None:
            slot_payload, raw_errors = parse_slot_json_text(raw_text)
            if raw_errors:
                return {}, [], False, raw_errors
        translations, errors = extract_slot_translations(
            slot_payload,
            slot_to_block,
        )
        return translations, [], False, errors
    if not allow_term_overrides:
        translations, errors = extract_translations(parsed_json, expected_block_ids)
        return translations, [], False, errors
    payload, overrides, reporting_present, metadata_errors = (
        split_term_override_metadata(
            parsed_json,
            expected_block_ids=expected_block_ids,
            allowed_source_terms=allowed_override_sources,
            allowed_preferred_targets=allowed_override_preferences,
        )
    )
    translations, translation_errors = extract_translations(payload, expected_block_ids)
    errors = [*translation_errors, *metadata_errors]
    return (
        translations,
        [] if metadata_errors else overrides,
        reporting_present,
        errors,
    )


def _empty_terminology_stats(active: bool) -> dict[str, Any]:
    if not active:
        return {}
    return {
        "policy_id": D2L_SOFT_GLOSSARY_POLICY_ID,
        "windows_reporting_present": 0,
        "windows_reporting_omitted": 0,
        "windows_with_overrides": 0,
        "overrides_total": 0,
        "reason_counts": {},
    }


def _record_terminology_stats(
    stats: dict[str, Any],
    *,
    reporting_present: bool,
    overrides: list[dict[str, str]],
) -> None:
    if not stats:
        return
    key = (
        "windows_reporting_present"
        if reporting_present
        else "windows_reporting_omitted"
    )
    stats[key] = int(stats[key]) + 1
    if overrides:
        stats["windows_with_overrides"] = int(stats["windows_with_overrides"]) + 1
    stats["overrides_total"] = int(stats["overrides_total"]) + len(overrides)
    reasons = stats["reason_counts"]
    for row in overrides:
        reason = row["reason_code"]
        reasons[reason] = int(reasons.get(reason, 0)) + 1


def _empty_translation_output_stats(
    active: bool,
    *,
    glossary_review_policy_id: str = GLOSSARY_REVIEW_POLICY_ID,
    response_envelope_policy: str | None = None,
) -> dict[str, Any]:
    if not active:
        return {}
    result = {
        "policy_id": D2L_TRANSLATION_SLOTS_POLICY_ID,
        "glossary_review_policy_id": glossary_review_policy_id,
        "windows_with_reviews": 0,
        "review_rows_total": 0,
        "review_blocks": 0,
    }
    if response_envelope_policy is not None:
        result["response_envelope_policy_id"] = response_envelope_policy
        result["responses_envelope_normalized"] = 0
    return result


def _record_translation_output_stats(
    stats: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    if not stats:
        return
    summary = glossary_review_summary(rows)
    if rows:
        stats["windows_with_reviews"] = int(stats["windows_with_reviews"]) + 1
    stats["review_rows_total"] = int(stats["review_rows_total"]) + int(
        summary["review_rows"]
    )
    stats["review_blocks"] = int(stats["review_blocks"]) + int(
        summary["review_blocks"]
    )


def _record_response_envelope_stats(
    stats: dict[str, Any],
    results: list[LLMResult],
    *,
    response_envelope_policy: str | None,
) -> None:
    if not stats or response_envelope_policy is None:
        return
    normalizer = _response_envelope_normalizer(response_envelope_policy)
    if normalizer is None:
        raise ValueError(
            f"Unknown Translator response-envelope policy: {response_envelope_policy}"
        )
    normalized = sum(
        1
        for result in results
        if normalizer(result.text)[1]
    )
    stats["responses_envelope_normalized"] = int(
        stats.get("responses_envelope_normalized") or 0
    ) + normalized


def _response_envelope_normalizer(response_envelope_policy: str | None):
    if response_envelope_policy == D2L_PROMPT_JSON_ENVELOPE_V1_POLICY_ID:
        return normalize_prompt_json_envelope_v1
    if response_envelope_policy == D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID:
        return normalize_prompt_json_envelope_v2
    return None


def _stored_transport_identity(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    identity = payload.get("transport_identity")
    return str(identity) if isinstance(identity, str) and identity else None


def _stored_experiment_id(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    experiment_id = payload.get("experiment_id")
    return (
        str(experiment_id)
        if isinstance(experiment_id, str) and experiment_id
        else None
    )


def _stored_terminology_policy(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    policy = payload.get("terminology_policy")
    return str(policy) if isinstance(policy, str) and policy else None


def _stored_override_match_rule(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    rule = payload.get("term_override_match_rule")
    return str(rule) if isinstance(rule, str) and rule else None


def _stored_protected_spans_policy(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    protected = payload.get("protected_spans")
    if not isinstance(protected, dict):
        return None
    policy = protected.get("policy_id")
    return str(policy) if isinstance(policy, str) and policy else None


def _stored_translation_output_policy(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    policy = payload.get("translation_output_policy")
    return str(policy) if isinstance(policy, str) and policy else None


def _stored_glossary_review_policy(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    policy = payload.get("glossary_review_policy")
    return str(policy) if isinstance(policy, str) and policy else None


def _stored_response_envelope_policy(payload_json: Any) -> str | None:
    payload = _stored_pack_payload(payload_json)
    policy = payload.get("response_envelope_policy")
    return str(policy) if isinstance(policy, str) and policy else None


def _stored_pack_payload(payload_json: Any) -> dict[str, Any]:
    if payload_json is None:
        return {}
    try:
        payload = json.loads(str(payload_json or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Translator memory-pack payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Translator memory-pack payload must be a JSON object")
    return payload


def load_window_attempt_state(
    db: sqlite3.Connection,
    *,
    experiment_id: str,
) -> dict[str, dict[str, Any]]:
    """Read durable per-window Translator attempt state for later quality repair."""

    result: dict[str, dict[str, Any]] = {}
    rows = db.execute(
        "SELECT pack_id, payload_json FROM memory_packs ORDER BY pack_id"
    ).fetchall()
    for row in rows:
        payload = _stored_pack_payload(row["payload_json"])
        if payload.get("experiment_id") != experiment_id:
            continue
        window_id = str(payload.get("window_id") or "")
        if not window_id or window_id in result:
            raise RuntimeError(
                "Translator attempt-state window identity is missing or duplicated"
            )
        raw_count = payload.get("translator_attempt_count")
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count not in {1, 2}
        ):
            raise RuntimeError(
                f"Translator attempt count is unavailable for {window_id}"
            )
        deterministic = payload.get("deterministic_quality")
        if not isinstance(deterministic, dict):
            raise RuntimeError(
                f"Translator deterministic state is unavailable for {window_id}"
            )
        retry_history = deterministic.get("retry_history")
        if not isinstance(retry_history, list) or any(
            not isinstance(item, dict) for item in retry_history
        ):
            raise RuntimeError(
                f"Translator retry history is unavailable for {window_id}"
            )
        context_pack = payload.get("context_pack")
        if context_pack is not None and not isinstance(context_pack, dict):
            raise RuntimeError(
                f"Translator context pack is invalid for {window_id}"
            )
        result[window_id] = {
            "window_id": window_id,
            "pack_id": str(row["pack_id"]),
            "attempt_count": raw_count,
            "retry_consumed": raw_count >= 2,
            "deterministic_quality": deterministic,
            "retry_history": list(retry_history),
            "context_pack": context_pack,
        }
    if not result:
        raise RuntimeError(
            f"Translator attempt state is absent for experiment {experiment_id}"
        )
    return result


def _fetch_blocks(db: sqlite3.Connection, block_ids: list[str]) -> list:
    if not block_ids:
        return []
    placeholders = ",".join("?" * len(block_ids))
    return db.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        block_ids,
    ).fetchall()


def _persist_pack(
    db: sqlite3.Connection,
    pack_id: str,
    window_id: str,
    block_ids: list[str],
    config: str,
    messages: list[dict],
    result: LLMResult,
    *,
    experiment_id: str,
    prompt_version: str,
    context_pack: Any | None,
    blocks_for_prompt: list[dict[str, Any]],
    profile_name: str,
    transport_identity: str | None,
    term_overrides: list[dict[str, str]],
    term_override_reporting_present: bool,
    terminology_policy: str | None,
    term_override_match_rule: str | None,
    protected_span_metadata: dict[str, Any] | None,
    translation_output_policy: str | None,
    slot_map: dict[str, str] | None,
    glossary_review_policy: str | None,
    glossary_reviews: list[dict[str, Any]],
    response_envelope_policy: str | None,
    call_count: int,
    deterministic_quality: dict[str, Any],
) -> None:
    # Store window context in payload_json (existing column).
    # config is stored via _add_column_if_missing during migration 005.
    zones = _zone_estimates(messages, blocks_for_prompt, context_pack)
    payload = {
        "experiment_id": experiment_id,
        "window_id": window_id,
        "block_ids": block_ids,
        "config": config,
        "zones": zones,
        "prompt_version": prompt_version,
        "profile": profile_name,
        "anchors_count": _context_anchors_count(context_pack),
        "dropped_by_budget": _context_dropped_by_budget(context_pack),
        "low_context": bool(getattr(context_pack, "low_context", False))
        if context_pack is not None
        else False,
        "translator_attempt_count": int(call_count),
        "deterministic_quality": deterministic_quality,
    }
    if transport_identity is not None:
        payload["transport_identity"] = transport_identity
    if terminology_policy is not None:
        payload["terminology_policy"] = terminology_policy
    if term_override_match_rule is not None:
        payload["term_override_match_rule"] = term_override_match_rule
        payload["term_override_reporting_present"] = bool(
            term_override_reporting_present
        )
        payload["term_overrides"] = term_overrides
    if protected_span_metadata is not None:
        payload["protected_spans"] = protected_span_metadata
    if translation_output_policy is not None:
        payload["translation_output_policy"] = translation_output_policy
        payload["slot_map"] = slot_map or {}
        payload["glossary_review_policy"] = glossary_review_policy
        payload["glossary_reviews"] = glossary_reviews
        if response_envelope_policy is not None:
            payload["response_envelope_policy"] = response_envelope_policy
    if context_pack is not None and hasattr(context_pack, "to_dict"):
        payload["context_pack"] = context_pack.to_dict()
    pack_hash = sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]

    existing = db.execute(
        "SELECT pack_id, prompt_version, payload_json FROM memory_packs WHERE pack_id = ?",
        (pack_id,),
    ).fetchone()
    if existing:
        existing_payload = _stored_pack_payload(existing["payload_json"])
        if existing_payload.get("experiment_id") != experiment_id:
            raise RuntimeError("Translator memory-pack experiment identity mismatch")
        if (
            existing_payload.get("window_id") != window_id
            or existing_payload.get("block_ids") != block_ids
            or existing_payload.get("config") != config
        ):
            raise RuntimeError("Translator memory-pack scoped ID collision")
        if str(existing["prompt_version"] or "") != prompt_version:
            raise RuntimeError("Translator memory-pack prompt version mismatch")
        if _stored_terminology_policy(existing["payload_json"]) != terminology_policy:
            raise RuntimeError("Translator memory-pack terminology policy mismatch")
        if (
            _stored_override_match_rule(existing["payload_json"])
            != term_override_match_rule
        ):
            raise RuntimeError("Translator memory-pack override match rule mismatch")
        if existing_payload.get("protected_spans") != protected_span_metadata:
            raise RuntimeError("Translator memory-pack protected-span policy mismatch")
        if (
            existing_payload.get("translation_output_policy")
            != translation_output_policy
            or existing_payload.get("slot_map") != (slot_map or None)
            or existing_payload.get("glossary_review_policy")
            != glossary_review_policy
            or existing_payload.get("response_envelope_policy")
            != response_envelope_policy
        ):
            raise RuntimeError("Translator memory-pack output policy mismatch")
        if transport_identity is not None:
            if existing_payload.get("transport_identity") != transport_identity:
                raise RuntimeError("Shared Translator memory-pack identity mismatch")
        return

    doc_id = ""
    first_block = ""
    if block_ids:
        row = db.execute(
            "SELECT doc_id, block_id FROM blocks WHERE block_id = ?", (block_ids[0],)
        ).fetchone()
        if row:
            doc_id = str(row["doc_id"])
            first_block = str(row["block_id"])

    db.execute(
        """
        INSERT INTO memory_packs (
          pack_id, doc_id, block_id, pack_hash,
          prompt_version, estimated_tokens, payload_json, config
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            doc_id,
            first_block,
            pack_hash,
            prompt_version,
            result.usage.prompt_tokens + result.usage.completion_tokens,
            json.dumps(payload, ensure_ascii=False),
            config,
        ),
    )


def _persist_run(
    db: sqlite3.Connection,
    run_id: str,
    experiment_id: str,
    block_id: str,
    config: str,
    stage: str,
    window_id: str,
    pack_id: str,
    output_text: str,
    model: str,
    prompt_version: str,
    temperature: float,
    seed: int,
    result: LLMResult,
) -> None:
    row = db.execute(
        "SELECT doc_id FROM blocks WHERE block_id = ?", (block_id,)
    ).fetchone()
    doc_id = str(row["doc_id"]) if row else ""

    existing = db.execute(
        """
        SELECT experiment_id, block_id, config, stage, pack_id
        FROM translation_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if existing and (
        str(existing["experiment_id"] or "") != experiment_id
        or str(existing["block_id"] or "") != block_id
        or str(existing["config"] or "") != config
        or str(existing["stage"] or "") != stage
        or str(existing["pack_id"] or "") != pack_id
    ):
        raise RuntimeError("Translator scoped run ID collision")

    db.execute(
        """
        INSERT OR REPLACE INTO translation_runs (
          run_id, experiment_id, doc_id, block_id, config, stage,
          window_id, pack_id, output_text, model,
          prompt_version, temperature, seed,
          system_fingerprint, cost, latency_ms
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, experiment_id, doc_id, block_id, config, stage,
            window_id, pack_id, output_text, model,
            prompt_version, temperature, seed,
            result.system_fingerprint, result.cost_usd, result.latency_ms,
        ),
    )


def _persist_hygiene_issues(
    db: sqlite3.Connection,
    run_id: str,
    block_id: str,
    issues: list[HygieneIssue],
) -> None:
    if not issues:
        return
    row = db.execute(
        "SELECT doc_id FROM blocks WHERE block_id = ?", (block_id,)
    ).fetchone()
    doc_id = str(row["doc_id"]) if row else ""
    for issue in issues:
        digest = sha256(
            f"{run_id}:{issue.script}:{issue.start}:{issue.end}:{issue.surface}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        issue_id = f"qa_hygiene_{digest}"
        db.execute(
            """
            INSERT OR REPLACE INTO qa_issues (
              issue_id, doc_id, run_id, block_id, tier, rule_or_subtype,
              severity, evidence_source, evidence_target, suggestion,
              fixed, retry_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue_id,
                doc_id,
                run_id,
                block_id,
                "tier1",
                f"hygiene_foreign_script:{issue.script}",
                "major",
                issue.evidence_source,
                issue.evidence_target,
                "Retranslate the marked non-source-script span into Vietnamese.",
                0,
                1,
            ),
        )


def _empty_hygiene_stats(config: str) -> dict[str, Any]:
    empty = {"flagged_blocks": 0, "reasked": 0, "fixed": 0, "still_bad": 0}
    return {**empty, "by_config": {config.upper(): dict(empty)}}


def _merge_hygiene_stats(
    stats: dict[str, Any],
    config: str,
    summary: dict[str, int],
) -> None:
    config = config.upper()
    if config not in stats["by_config"]:
        stats["by_config"][config] = {
            "flagged_blocks": 0,
            "reasked": 0,
            "fixed": 0,
            "still_bad": 0,
        }
    for key in ("flagged_blocks", "reasked", "fixed", "still_bad"):
        value = int(summary.get(key, 0))
        stats[key] = int(stats.get(key, 0)) + value
        stats["by_config"][config][key] = int(stats["by_config"][config].get(key, 0)) + value


def _hygiene_call_summary(
    flagged_blocks: set[str],
    reasked_blocks: set[str],
    fixed_blocks: list[str],
    still_bad_issues: list[HygieneIssue],
) -> dict[str, int]:
    return {
        "flagged_blocks": len(flagged_blocks),
        "reasked": len(reasked_blocks),
        "fixed": len(set(fixed_blocks)),
        "still_bad": len({issue.block_id for issue in still_bad_issues}),
    }


def _empty_protected_span_stats(
    policy: ProtectedSpanPolicy | None,
    *,
    effective_prompt_version: str,
) -> dict[str, Any]:
    if policy is None:
        return {}
    stats = {
        "policy_id": policy.policy_id,
        # V1 telemetry is a published compatibility surface. Its prompt field
        # names the protected-span prompt even when translation slots select a
        # different effective generation prompt.
        "prompt_version": (
            effective_prompt_version
            if policy.hides_source_bytes
            else policy.prompt_version
        ),
        "windows": 0,
        "blocks": 0,
        "spans": 0,
        "windows_reasked": 0,
        "blocks_flagged": 0,
        "windows_failed": 0,
        "final_issue_count": 0,
    }
    if policy.hides_source_bytes:
        stats["latex_visible_to_model"] = False
    return stats


def _merge_protected_span_stats(
    stats: dict[str, Any],
    *,
    plan: Any | None,
    summary: dict[str, Any],
    status: str,
) -> None:
    if not stats or plan is None:
        return
    stats["windows"] = int(stats["windows"]) + 1
    stats["blocks"] = int(stats["blocks"]) + len(plan.protected_blocks)
    stats["spans"] = int(stats["spans"]) + plan.protected_span_count
    if summary.get("reasked_blocks"):
        stats["windows_reasked"] = int(stats["windows_reasked"]) + 1
    stats["blocks_flagged"] = int(stats["blocks_flagged"]) + len(
        summary.get("flagged_blocks") or []
    )
    final_issues = summary.get("final_issues") or []
    stats["final_issue_count"] = int(stats["final_issue_count"]) + len(
        final_issues
    )
    if status == "failed":
        stats["windows_failed"] = int(stats["windows_failed"]) + 1


def _total_usage(
    results: list[LLMResult],
) -> dict[str, int | float | None | str]:
    payload: dict[str, int | float | None | str] = {
        "prompt_tokens": sum(r.usage.prompt_tokens for r in results),
        "completion_tokens": sum(r.usage.completion_tokens for r in results),
        "reasoning_tokens": sum(r.usage.reasoning_tokens for r in results),
        "cost_usd": _sum_cost(results),
        "incremental_cost_usd": _sum_cost(results, incremental_only=True),
        "calls": len(results),
        "cache_hits": sum(1 for r in results if r.from_cache),
    }
    if payload["cost_usd"] is None or payload["incremental_cost_usd"] is None:
        payload["cost_status"] = "unknown"
    return payload


def _sum_cost(
    results: list[Any], *, incremental_only: bool = False
) -> float | None:
    selected = [
        row for row in results if not incremental_only or not row.from_cache
    ]
    if any(row.cost_usd is None for row in selected):
        return None
    return round(sum(float(row.cost_usd) for row in selected), 12)


def _transport_identity(client: Any, config: str) -> str | None:
    if not bool(getattr(client, "uses_shared_backend", False)):
        return None
    preset = getattr(client, "preset", None)
    expected_role = f"d2l.translator.{config.casefold()}"
    if getattr(preset, "role_id", None) != expected_role:
        raise RuntimeError(
            f"Shared Translator {config} requires role {expected_role}"
        )
    identity = getattr(client, "transport_identity", None)
    if not isinstance(identity, str) or not identity:
        raise RuntimeError("Shared Translator client lacks transport identity")
    return identity


def _pack_id(
    *,
    experiment_id: str,
    config: str,
    window_id: str,
    transport_identity: str | None,
) -> str:
    experiment_hash = sha256(experiment_id.encode("utf-8")).hexdigest()[:12]
    if transport_identity is None:
        return f"pk_{config}_{window_id}_{experiment_hash}"
    return (
        f"pk_{config}_{window_id}_{experiment_hash}_"
        f"{transport_identity[:20]}"
    )


def _translation_run_id(
    *,
    experiment_id: str,
    config: str,
    block_id: str,
    transport_identity: str | None,
) -> str:
    experiment_hash = sha256(experiment_id.encode("utf-8")).hexdigest()[:12]
    if transport_identity is None:
        return f"tr_{config}_{block_id}_{experiment_hash}"
    return (
        f"tr_{config}_{block_id}_{experiment_hash}_"
        f"{transport_identity[:20]}"
    )


def _last_fingerprint(results: list[LLMResult]) -> str | None:
    for result in reversed(results):
        if result.system_fingerprint:
            return result.system_fingerprint
    return None


def _zone_estimates(
    messages: list[dict],
    blocks_for_prompt: list[dict[str, Any]],
    context_pack: Any | None,
) -> dict[str, int]:
    system_text = str(messages[0].get("content", "")) if messages else ""
    hard_tokens = int(getattr(context_pack, "token_estimate", 0) or 0)
    source_text = "\n".join(
        str(block.get("clean_text") or block.get("source_text") or "")
        for block in blocks_for_prompt
    )
    return {
        "system_tokens": _estimate_tokens(system_text),
        "hard_constraints_tokens": hard_tokens,
        "source_tokens": _estimate_tokens(source_text),
    }


def _context_anchors_count(context_pack: Any | None) -> dict[str, int]:
    if context_pack is None:
        return {"terms": 0, "entities": 0, "address_policies": 0}
    anchors = getattr(context_pack, "anchors", None)
    count = getattr(anchors, "count_by_type", {"terms": 0, "entities": 0})
    return {
        "terms": int(count.get("terms", 0)),
        "entities": int(count.get("entities", 0)),
        "address_policies": len(getattr(context_pack, "address_lines", []) or []),
    }


def _context_dropped_by_budget(context_pack: Any | None) -> list[dict[str, str]]:
    if context_pack is None:
        return []
    result = []
    for item in getattr(context_pack, "dropped_by_budget", []) or []:
        if hasattr(item, "to_dict"):
            result.append(item.to_dict())
        else:
            result.append(dict(item))
    return result


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _messages_hash(messages: list[dict]) -> str:
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _messages_summary(messages: list[dict]) -> list[dict[str, int | str]]:
    summary = []
    for message in messages:
        content = str(message.get("content", ""))
        summary.append(
            {
                "role": str(message.get("role", "")),
                "chars": len(content),
                "tokens_est": _estimate_tokens(content),
                "sha256": sha256(content.encode("utf-8")).hexdigest()[:16],
            }
        )
    return summary


def _context_pack_summary(context_pack: Any | None) -> dict[str, Any]:
    if context_pack is None:
        return {
            "included_count": 0,
            "dropped_by_budget_count": 0,
            "anchors_count": {"terms": 0, "entities": 0, "address_policies": 0},
            "low_context": False,
        }

    dropped = _context_dropped_by_budget(context_pack)
    included_count = (
        len(getattr(context_pack, "glossary_lines", []) or [])
        + len(getattr(context_pack, "preserve_lines", []) or [])
        + len(getattr(context_pack, "context_sensitive_lines", []) or [])
        + len(getattr(context_pack, "entity_lines", []) or [])
        + len(getattr(context_pack, "address_lines", []) or [])
    )
    return {
        "included_count": included_count,
        "dropped_by_budget_count": len(dropped),
        "anchors_count": _context_anchors_count(context_pack),
        "low_context": bool(getattr(context_pack, "low_context", False)),
        "token_estimate": int(getattr(context_pack, "token_estimate", 0) or 0),
        "dropped_by_budget_sample": dropped[:3],
    }


def _pack_summary_for_event(
    context_pack: Any | None,
    *,
    terminology_policy: str | None = None,
) -> dict[str, Any] | None:
    if context_pack is None:
        return None
    summary = _context_pack_summary(context_pack)
    soft_glossary = terminology_policy == D2L_SOFT_GLOSSARY_POLICY_ID
    pack_summary: dict[str, Any] = {
        "injected": int(summary.get("included_count") or 0),
        "mandatory": len(getattr(context_pack, "entity_lines", []) or [])
        if soft_glossary
        else len(getattr(context_pack, "glossary_lines", []) or [])
        + len(getattr(context_pack, "entity_lines", []) or []),
        "soft": len(getattr(context_pack, "context_sensitive_lines", []) or []),
        "preserve": len(getattr(context_pack, "preserve_lines", []) or []),
        "quarantine": len(getattr(context_pack, "repair_queue", []) or []),
        "address": len(getattr(context_pack, "address_lines", []) or []),
        "dropped_by_budget": int(summary.get("dropped_by_budget_count") or 0),
        "est_tokens": int(summary.get("token_estimate") or 0),
    }
    if soft_glossary:
        pack_summary["preferred"] = len(
            getattr(context_pack, "glossary_lines", []) or []
        )
        pack_summary["terminology_policy"] = terminology_policy
    pack_summary["sample"] = _pack_summary_sample(
        context_pack,
        soft_glossary=soft_glossary,
    )
    pack_summary["more"] = _pack_summary_more(
        pack_summary["sample"],
        context_pack,
        soft_glossary=soft_glossary,
    )
    return pack_summary


def _pack_summary_sample(
    context_pack: Any,
    *,
    limit: int = 6,
    soft_glossary: bool = False,
) -> dict[str, list[str]]:
    buckets = {
        "mandatory": list(getattr(context_pack, "entity_lines", []) or [])
        if soft_glossary
        else list(getattr(context_pack, "glossary_lines", []) or [])
        + list(getattr(context_pack, "entity_lines", []) or []),
        "soft": list(getattr(context_pack, "context_sensitive_lines", []) or []),
        "preserve": list(getattr(context_pack, "preserve_lines", []) or []),
        "address": list(getattr(context_pack, "address_lines", []) or []),
    }
    if soft_glossary:
        buckets["preferred"] = list(
            getattr(context_pack, "glossary_lines", []) or []
        )
    sample = {key: [str(line) for line in lines[:limit]] for key, lines in buckets.items() if lines}
    repair_queue = getattr(context_pack, "repair_queue", []) or []
    if repair_queue:
        sample["quarantine"] = [
            str(item.get("source_term") or item.get("glossary_id") or item)
            for item in repair_queue[:limit]
            if isinstance(item, dict)
        ]
    return sample


def _pack_summary_more(
    sample: dict[str, list[str]],
    context_pack: Any,
    *,
    limit: int = 6,
    soft_glossary: bool = False,
) -> dict[str, int]:
    totals = {
        "mandatory": len(getattr(context_pack, "entity_lines", []) or [])
        if soft_glossary
        else len(getattr(context_pack, "glossary_lines", []) or [])
        + len(getattr(context_pack, "entity_lines", []) or []),
        "soft": len(getattr(context_pack, "context_sensitive_lines", []) or []),
        "preserve": len(getattr(context_pack, "preserve_lines", []) or []),
        "address": len(getattr(context_pack, "address_lines", []) or []),
        "quarantine": len(getattr(context_pack, "repair_queue", []) or []),
    }
    if soft_glossary:
        totals["preferred"] = len(getattr(context_pack, "glossary_lines", []) or [])
    return {
        key: max(0, count - len(sample.get(key, [])))
        for key, count in totals.items()
        if count > limit
    }


def _bounded_translations(translations: dict[str, str], *, limit: int = 8) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for block_id, text in list(translations.items())[:limit]:
        value = str(text)
        result[str(block_id)] = {
            "chars": len(value),
            "preview": value[:240],
        }
    return result
