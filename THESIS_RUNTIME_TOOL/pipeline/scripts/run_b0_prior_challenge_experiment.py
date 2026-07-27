from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.agents.provider_profile import ResolvedCredential
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    evaluate_hidden_corruption,
    prior_challenge_response_schema,
    render_prior_challenge_request,
    select_prior_cards_for_chapter,
    validate_prior_cards,
    validate_prior_challenge_response,
)
from pipeline.literary.transport_json import (
    LiteraryTransportJsonError,
    parse_structured_response,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    StructuredOutputContract,
    gemini_response_json_schema,
    validate_structured_payload,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    b0_inputs_from_prefix_bundle_v1,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.openai_compatible_structured_call_v1 import (
    call_openai_compatible_structured_v1,
)
from pipeline.literary.review_case_ledger_v1 import (
    select_relevant_review_cases_v1,
    verify_review_case_ledger_v1,
)
from pipeline.scripts.run_b0_inventory_gemini_comparison import (
    INTERNAL_UTC_DAY_TOKEN_CAP,
    RPD,
    RPM,
    TPM,
    scan_current_utc_gemini_usage,
)
from pipeline.scripts.run_chapter_registry_v2_gemini import (
    BUCKET_IDS,
    _gemini_transport,
    load_gemini_credentials,
)
from pipeline.scripts.run_chapter_registry_v2_real import RESPONSE_FORMAT_JSON
from pipeline.scripts.run_chapter_registry_v4_real import (
    DEFAULT_CHAPTER_ID,
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
    RUNTIME_ROOT,
    _ensure_empty_output,
    _git_head,
    _load_document,
    _redacted_error,
    _verify_frozen_db,
    _write_json,
)


MODEL_ID = "gemini-3.5-flash"
RUN_SCHEMA_VERSION = "b0_prior_challenge_gemini_run_v2"
INPUT_TOKEN_CAP = 20_000
OUTPUT_TOKEN_CAP = 4_096


class PriorChallengeRunError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PriorChallengeRunError(f"cannot load {label}") from exc


def _load_prior_cards(path: Path) -> list[dict[str, Any]]:
    raw = _load_json(path, "prior cards")
    if isinstance(raw, Mapping):
        raw = raw.get("prior_cards")
    if not isinstance(raw, list):
        raise PriorChallengeRunError("prior cards file must contain a list")
    return validate_prior_cards(raw)


def _load_prior_inputs(
    *,
    prior_cards_path: Path | None,
    prior_bundle_path: Path | None,
    document: Mapping[str, Any],
    chapter: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    if (prior_cards_path is None) == (prior_bundle_path is None):
        raise PriorChallengeRunError(
            "provide exactly one of prior cards or a chapter-prefix prior bundle"
        )
    if prior_bundle_path is None:
        cards = _load_prior_cards(Path(prior_cards_path))
        provenance = {
            "input_kind": "standalone_prior_cards",
            "input_sha256": file_sha256(Path(prior_cards_path)),
            "input_hash": canonical_hash(cards),
        }
        return cards, [], [], provenance
    raw = _load_json(prior_bundle_path, "chapter-prefix prior bundle")
    if not isinstance(raw, Mapping):
        raise PriorChallengeRunError("chapter-prefix prior bundle must be an object")
    verified = verify_chapter_prefix_prior_bundle_v1(raw, document=document)
    derived = b0_inputs_from_prefix_bundle_v1(verified)
    cards = select_prior_cards_for_chapter(
        chapter=chapter,
        prior_cards=derived["prior_cards"],
    )
    provenance = {
        "input_kind": "chapter_prefix_prior_bundle",
        "input_sha256": file_sha256(prior_bundle_path),
        "input_hash": verified["prefix_bundle_hash"],
        "b0_inputs_hash": derived["b0_inputs_hash"],
        "state_lineage_id": verified["state_lineage_id"],
    }
    return (
        cards,
        derived["candidate_only_context_cards"],
        derived["glossary_context_cards"],
        provenance,
    )


def _load_corruption_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    raw = _load_json(path, "hidden corruption manifest")
    if not isinstance(raw, dict):
        raise PriorChallengeRunError("hidden corruption manifest must be an object")
    if raw.get("hidden_from_model") is not True:
        raise PriorChallengeRunError("corruption manifest is not sealed as hidden")
    return raw


def _load_relevant_review_cases(
    *,
    review_case_ledger_path: Path | None,
    prior_bundle_path: Path | None,
    document: Mapping[str, Any],
    chapter: Mapping[str, Any],
) -> dict[str, Any] | None:
    if review_case_ledger_path is None:
        return None
    if prior_bundle_path is None:
        raise PriorChallengeRunError(
            "review-case selection requires a chapter-prefix prior bundle"
        )
    raw_ledger = _load_json(review_case_ledger_path, "review-case ledger")
    raw_prefix = _load_json(prior_bundle_path, "chapter-prefix prior bundle")
    if not isinstance(raw_ledger, Mapping) or not isinstance(raw_prefix, Mapping):
        raise PriorChallengeRunError("review-case inputs must be objects")
    ledger = verify_review_case_ledger_v1(raw_ledger)
    prefix = verify_chapter_prefix_prior_bundle_v1(raw_prefix, document=document)
    return select_relevant_review_cases_v1(
        ledger=ledger,
        chapter=chapter,
        prefix_bundle=prefix,
    )


def build_envelope(
    *,
    document_path: Path,
    design_doc: Path,
    chapter_id: str,
    prior_cards_path: Path | None,
    prior_bundle_path: Path | None,
    corruption_manifest_path: Path | None,
    review_case_ledger_path: Path | None = None,
    model_id: str = MODEL_ID,
    structured_output_contract: StructuredOutputContract | None = None,
) -> tuple[
    dict[str, Any],
    Any,
    Mapping[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
    Mapping[str, Any],
]:
    provider = (
        structured_output_contract.provider
        if structured_output_contract is not None
        else "google_genai"
    )
    document, chapter = _load_document(document_path, chapter_id)
    prior_cards, candidate_only_cards, glossary_cards, prior_input = _load_prior_inputs(
        prior_cards_path=prior_cards_path,
        prior_bundle_path=prior_bundle_path,
        document=document,
        chapter=chapter,
    )
    corruption_manifest = _load_corruption_manifest(corruption_manifest_path)
    relevant_review_cases = _load_relevant_review_cases(
        review_case_ledger_path=review_case_ledger_path,
        prior_bundle_path=prior_bundle_path,
        document=document,
        chapter=chapter,
    )
    if corruption_manifest is not None:
        supplied_hash = canonical_hash(prior_cards)
        if corruption_manifest.get("supplied_prior_cards_hash") != supplied_hash:
            raise PriorChallengeRunError(
                "corruption manifest does not bind the supplied prior cards"
            )
    request = render_prior_challenge_request(
        chapter=chapter,
        prior_cards=prior_cards,
        candidate_only_cards=candidate_only_cards,
        glossary_cards=glossary_cards,
        relevant_review_cases=relevant_review_cases,
        design_doc=design_doc,
        model_id=model_id,
        reasoning_effort="none",
        temperature=1.0,
        seed=20260715,
        max_output_tokens=OUTPUT_TOKEN_CAP,
    )
    response_schema = prior_challenge_response_schema()
    reserve = structured_prompt_reserve_v1(
        messages=request.messages,
        response_schema=response_schema,
        output_token_cap=OUTPUT_TOKEN_CAP,
        include_schema_transport_overhead=(
            structured_output_contract is None
            or structured_output_contract.native_enforcement
        ),
    )
    if reserve.prompt_token_reserve > INPUT_TOKEN_CAP:
        raise PriorChallengeRunError(
            f"prompt reserve {reserve.prompt_token_reserve} exceeds cap "
            f"{INPUT_TOKEN_CAP}"
        )
    if (
        structured_output_contract is not None
        and structured_output_contract.canonical_schema_hash
        != canonical_hash(response_schema)
    ):
        raise PriorChallengeRunError(
            "structured-output contract differs from the prior-aware B1 schema"
        )
    rendered = "\n".join(str(row.get("content") or "") for row in request.messages)
    for forbidden in (
        "mutation_id",
        "changed_prior_card_id",
        "changed_card_fields",
        "expected_issue_code",
        "expected_disputed_field",
        "corruption_manifest_hash",
    ):
        if forbidden in rendered:
            raise PriorChallengeRunError("hidden corruption metadata leaked into request")
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "provider": provider,
        "chapter_id": chapter_id,
        "model_contract": {
            "model_id": model_id,
            "temperature": 1.0,
            "seed": 20260715,
            "thinking_budget": 0,
            "max_output_tokens": OUTPUT_TOKEN_CAP,
            "input_token_cap": INPUT_TOKEN_CAP,
        },
        "quota_contract": {
            "rpm": RPM,
            "tpm": TPM,
            "rpd_per_physical_key": RPD,
            "internal_utc_day_token_cap_per_physical_key": (
                INTERNAL_UTC_DAY_TOKEN_CAP
            ),
        },
        "document_sha256": file_sha256(document_path),
        "design_doc_sha256": file_sha256(design_doc),
        "prior_input": prior_input,
        "prior_cards_hash": canonical_hash(prior_cards),
        "candidate_only_cards_hash": canonical_hash(candidate_only_cards),
        "candidate_only_card_count": len(candidate_only_cards),
        "glossary_cards_hash": canonical_hash(glossary_cards),
        "glossary_card_count": len(glossary_cards),
        "review_case_ledger_sha256": (
            file_sha256(review_case_ledger_path)
            if review_case_ledger_path is not None
            else None
        ),
        "relevant_review_case_count": len(
            (relevant_review_cases or {}).get("packets") or []
        ),
        "relevant_review_case_manifest_hash": (
            relevant_review_cases.get("review_case_manifest_hash")
            if relevant_review_cases is not None
            else request.sections["review_case_manifest_hash"]
        ),
        "hidden_corruption_manifest_sha256": (
            file_sha256(corruption_manifest_path)
            if corruption_manifest_path is not None
            else None
        ),
        "prompt_id": request.prompt_id,
        "prompt_sha256": request.prompt_sha256,
        "response_schema_hash": canonical_hash(response_schema),
        "structured_output_contract": (
            structured_output_contract.to_payload()
            if structured_output_contract is not None
            else {
                "schema_version": "literary_structured_output_legacy_v1",
                "effective_mode": "legacy_native_schema_unsealed",
                "local_validation_required": True,
            }
        ),
        "request_fingerprint": request.request_fingerprint,
        "estimated_prompt_tokens": reserve.message_token_estimate,
        "response_schema_utf8_bytes": reserve.response_schema_utf8_bytes,
        "transport_prompt_token_reserve": reserve.prompt_token_reserve,
        "reserved_tokens": reserve.total_token_reserve,
        "git_head": _git_head(),
        "hidden_manifest_sent_to_model": False,
        "production_publish_performed": False,
    }
    return (
        {**body, "envelope_hash": canonical_hash(body)},
        request,
        chapter,
        prior_cards,
        candidate_only_cards,
        glossary_cards,
        relevant_review_cases,
        corruption_manifest,
        response_schema,
    )


def run_dry(
    *,
    document_path: Path,
    design_doc: Path,
    chapter_id: str,
    prior_cards_path: Path | None,
    prior_bundle_path: Path | None,
    corruption_manifest_path: Path | None,
    output_dir: Path,
    review_case_ledger_path: Path | None = None,
    model_id: str = MODEL_ID,
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    (
        envelope,
        request,
        _chapter,
        prior_cards,
        candidate_only_cards,
        glossary_cards,
        _relevant_review_cases,
        manifest,
        _schema,
    ) = build_envelope(
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        prior_cards_path=prior_cards_path,
        prior_bundle_path=prior_bundle_path,
        corruption_manifest_path=corruption_manifest_path,
        review_case_ledger_path=review_case_ledger_path,
        model_id=model_id,
        structured_output_contract=structured_output_contract,
    )
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "supplied_prior_cards.json", {"prior_cards": prior_cards})
    _write_json(
        output / "supplied_candidate_only_cards.json",
        {"candidate_only_context_cards": candidate_only_cards},
    )
    _write_json(
        output / "supplied_glossary_cards.json",
        {"glossary_context_cards": glossary_cards},
    )
    _write_json(
        output / "supplied_relevant_review_cases.json",
        request.sections["relevant_review_cases"],
    )
    if manifest is not None:
        _write_json(output / "hidden_corruption_manifest.json", manifest)
    report = {
        "schema_version": "b0_prior_challenge_dry_report_v1",
        "status": "dry_rendered_no_api",
        "chapter_id": chapter_id,
        "model_id": model_id,
        "prior_card_count": len(prior_cards),
        "candidate_only_card_count": len(candidate_only_cards),
        "glossary_card_count": len(glossary_cards),
        "relevant_review_case_count": envelope["relevant_review_case_count"],
        "has_hidden_corruption": manifest is not None,
        "envelope_hash": envelope["envelope_hash"],
        "request_fingerprint": request.request_fingerprint,
        "estimated_prompt_tokens": envelope["estimated_prompt_tokens"],
        "response_schema_utf8_bytes": envelope["response_schema_utf8_bytes"],
        "transport_prompt_token_reserve": envelope[
            "transport_prompt_token_reserve"
        ],
        "reserved_tokens": envelope["reserved_tokens"],
        "hidden_manifest_sent_to_model": False,
        "structured_output_contract": envelope["structured_output_contract"],
    }
    _write_json(output / "dry_report.json", report)
    return report


def run_live(
    *,
    document_path: Path,
    design_doc: Path,
    frozen_db: Path,
    chapter_id: str,
    prior_cards_path: Path | None,
    prior_bundle_path: Path | None,
    corruption_manifest_path: Path | None,
    output_dir: Path,
    approved_envelope_hash: str,
    keys_file: Path | None,
    quota_bucket_id: str,
    usage_roots: Sequence[Path],
    review_case_ledger_path: Path | None = None,
    resolved_credential: ResolvedCredential | None = None,
    allowed_quota_bucket_ids: Sequence[str] | None = None,
    provider_profile_hash: str | None = None,
    model_id: str = MODEL_ID,
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    frozen_hash = _verify_frozen_db(frozen_db)
    provider = (
        resolved_credential.provider
        if resolved_credential is not None
        else "google_genai"
    )
    (
        envelope,
        request,
        chapter,
        prior_cards,
        candidate_only_cards,
        glossary_cards,
        relevant_review_cases,
        manifest,
        response_schema,
    ) = build_envelope(
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        prior_cards_path=prior_cards_path,
        prior_bundle_path=prior_bundle_path,
        corruption_manifest_path=corruption_manifest_path,
        review_case_ledger_path=review_case_ledger_path,
        model_id=model_id,
        structured_output_contract=structured_output_contract,
    )
    if approved_envelope_hash != envelope["envelope_hash"]:
        raise PriorChallengeRunError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    if resolved_credential is not None:
        if resolved_credential.quota_bucket_id != quota_bucket_id:
            raise PriorChallengeRunError("resolved credential bucket does not match")
        if resolved_credential.provider not in {"google_genai", "openai"}:
            raise PriorChallengeRunError("resolved credential provider is unsupported")
        if (
            structured_output_contract is not None
            and structured_output_contract.provider != resolved_credential.provider
        ):
            raise PriorChallengeRunError(
                "structured-output contract provider does not match credential"
            )
        provider = resolved_credential.provider
        credentials = {quota_bucket_id: resolved_credential.secret}
        commitments = {quota_bucket_id: resolved_credential.commitment}
        credential_revision = resolved_credential.credential_revision
    else:
        if keys_file is None:
            raise PriorChallengeRunError("Gemini key source is required")
        credentials, commitments = load_gemini_credentials(keys_file)
        credential_revision = quota_bucket_id
    if quota_bucket_id not in credentials:
        raise PriorChallengeRunError("requested quota bucket is not configured")
    quota = scan_current_utc_gemini_usage(
        roots=usage_roots,
        allowed_bucket_ids=allowed_quota_bucket_ids,
    )
    if quota["unknown_bucket_rows"]:
        raise PriorChallengeRunError("quota scan found unknown physical buckets")
    if provider == "google_genai":
        prior_tokens = int(quota["usage_by_bucket"].get(quota_bucket_id, 0))
        prior_calls = int(
            quota["calls_by_bucket_model"].get(f"{quota_bucket_id}|{model_id}", 0)
        )
        if (
            prior_tokens + int(envelope["reserved_tokens"])
            > INTERNAL_UTC_DAY_TOKEN_CAP
        ):
            raise PriorChallengeRunError("internal UTC-day token gate would be exceeded")
        if prior_calls >= RPD:
            raise PriorChallengeRunError("Gemini per-key RPD gate would be exceeded")
        if int(envelope["reserved_tokens"]) > TPM:
            raise PriorChallengeRunError("per-request TPM reserve is too large")

    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "supplied_prior_cards.json", {"prior_cards": prior_cards})
    _write_json(
        output / "supplied_candidate_only_cards.json",
        {"candidate_only_context_cards": candidate_only_cards},
    )
    _write_json(
        output / "supplied_glossary_cards.json",
        {"glossary_context_cards": glossary_cards},
    )
    _write_json(output / "quota_preflight.json", quota)
    if manifest is not None:
        _write_json(output / "hidden_corruption_manifest.json", manifest)
    run_manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "running",
        "chapter_id": chapter_id,
        "model_id": model_id,
        "provider": provider,
        "quota_bucket_id": quota_bucket_id,
        "credential_revision": credential_revision,
        "credential_commitment": commitments[quota_bucket_id],
        "provider_profile_hash": provider_profile_hash,
        "structured_output_contract": envelope["structured_output_contract"],
        "frozen_db_sha256_before": frozen_hash,
        "hidden_manifest_sent_to_model": False,
        "production_publish_performed": False,
        "started_at": _now(),
    }
    _write_json(output / "run_manifest.json", run_manifest)

    observed_usage: dict[str, int] | None = None
    finish_reason: str | None = None
    try:
        if provider == "openai":
            if resolved_credential is None or structured_output_contract is None:
                raise PriorChallengeRunError(
                    "OpenAI-compatible prior-aware B1 requires a resolved credential "
                    "and structured output"
                )
            direct = call_openai_compatible_structured_v1(
                credential=resolved_credential,
                model_id=model_id,
                messages=request.messages,
                contract=structured_output_contract,
                schema_name="literary_registry_b0_prior_v1",
                cache_path=(
                    output
                    / "cache"
                    / quota_bucket_id
                    / "b0_prior_challenge.sqlite3"
                ),
                tag=f"b0_prior_challenge:{chapter_id}",
                prompt_token_cap=INPUT_TOKEN_CAP,
                max_output_tokens=OUTPUT_TOKEN_CAP,
                temperature=1.0,
                seed=20260715,
                reasoning_effort="none",
            )
            parsed_json = direct.parsed_json
            transport_normalization = "openai_strict_json_schema"
            parse_error = None
            observed_usage = dict(direct.usage)
            finish_reason = "openai_strict_json_schema"
            raw_model = direct.model
            response_text = direct.response_text
            strict_json_error = direct.json_error
            safe_transport_metadata: Mapping[str, Any] = {}
            thinking_budget: int | None = None
            latency_ms = direct.latency_ms
            cost_usd = direct.cost_usd
            from_cache = direct.from_cache
            cache_key = direct.cache_key
        else:
            transport = _gemini_transport(
                api_key=credentials[quota_bucket_id],
                response_json_schema=(
                    response_schema
                    if structured_output_contract is None
                    else gemini_response_json_schema(structured_output_contract)
                ),
                timeout_ms=(
                    resolved_credential.request_timeout_ms
                    if resolved_credential is not None
                    else 120_000
                ),
                base_url=(
                    resolved_credential.base_url if resolved_credential else None
                ),
            )
            client = JudgeClient(
                LLMConfig(
                    model=model_id,
                    temperature=1.0,
                    seed=20260715,
                    reasoning_effort="none",
                    verbosity=None,
                    max_output_tokens=OUTPUT_TOKEN_CAP,
                    daily_token_cap=INTERNAL_UTC_DAY_TOKEN_CAP,
                    prompt_token_cap=INPUT_TOKEN_CAP,
                    pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
                ),
                output / "cache" / quota_bucket_id / "b0_prior_challenge.sqlite3",
                transport=transport,
                max_retries=0,
            )
            judged = client.call(
                [dict(row) for row in request.messages],
                response_format=RESPONSE_FORMAT_JSON,
                tag=f"b0_prior_challenge:{chapter_id}",
                bypass_cache=True,
            )
            parse_error = None
            try:
                parsed_json, transport_normalization = parse_structured_response(
                    judged.text
                )
            except LiteraryTransportJsonError as exc:
                parsed_json = None
                transport_normalization = "rejected"
                parse_error = exc
            observed_usage = asdict(judged.usage)
            finish_reason = str(
                transport.last_metadata.get("gemini_finish_reason") or "unknown"
            )
            raw_model = judged.model
            response_text = judged.text
            strict_json_error = judged.json_error
            safe_transport_metadata = dict(transport.last_metadata)
            thinking_budget = 0
            latency_ms = judged.latency_ms
            cost_usd = judged.cost_usd
            from_cache = judged.from_cache
            cache_key = judged.cache_key
        raw = {
            "schema_version": "b0_prior_challenge_gemini_raw_v1",
            "chapter_id": chapter_id,
            "model": raw_model,
            "provider": provider,
            "quota_bucket_id": quota_bucket_id,
            "credential_revision": credential_revision,
            "credential_commitment": commitments[quota_bucket_id],
            "completed_at": _now(),
            "from_cache": from_cache,
            "cache_key": cache_key,
            "response_text": response_text,
            "parsed_json": parsed_json,
            "json_error": None,
            "strict_json_error": strict_json_error,
            "transport_normalization": transport_normalization,
            "usage": observed_usage,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "safe_transport_metadata": dict(safe_transport_metadata),
            "thinking_budget": thinking_budget,
            "structured_output_contract": envelope["structured_output_contract"],
        }
        _write_json(output / "raw_result.json", raw)
        if parse_error is not None:
            raise PriorChallengeRunError(str(parse_error)) from parse_error
        validate_structured_payload(
            parsed_json,
            canonical_schema=response_schema,
        )
        artifact = validate_prior_challenge_response(
            parsed_json,
            chapter=chapter,
            prior_cards=prior_cards,
            candidate_only_cards=candidate_only_cards,
            glossary_cards=glossary_cards,
            relevant_review_cases=relevant_review_cases,
            request_fingerprint=request.request_fingerprint,
        )
        _write_json(output / "prior_challenge_artifact.json", artifact)
        evaluation = None
        if manifest is not None:
            evaluation = evaluate_hidden_corruption(artifact, manifest)
            _write_json(output / "hidden_corruption_evaluation.json", evaluation)
        report = {
            "schema_version": "b0_prior_challenge_live_report_v1",
            "status": "accepted_one_call_experiment",
            "chapter_id": chapter_id,
            "model_id": model_id,
            "quota_bucket_id": quota_bucket_id,
            "envelope_hash": envelope["envelope_hash"],
            "prior_card_count": len(prior_cards),
            "candidate_only_card_count": len(candidate_only_cards),
            "glossary_card_count": len(glossary_cards),
            "has_hidden_corruption": manifest is not None,
            "validation_report": artifact["validation_report"],
            "conflict_ticket_count": len(artifact["prior_conflict_tickets"]),
            "expected_ticket_detected": (
                evaluation.get("expected_ticket_detected") if evaluation else None
            ),
            "unrelated_ticket_count": (
                evaluation.get("unrelated_ticket_count") if evaluation else None
            ),
            "usage": observed_usage,
            "finish_reason": finish_reason,
            "transport_normalization": transport_normalization,
            "thinking_budget": 0,
            "structured_output_contract": envelope["structured_output_contract"],
            "hidden_manifest_sent_to_model": False,
            "production_publish_performed": False,
            "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
            "completed_at": _now(),
        }
        _write_json(output / "experiment_report.json", report)
        _write_json(
            output / "run_manifest.json",
            {**run_manifest, "status": "accepted", "completed_at": report["completed_at"]},
        )
        return report
    except Exception as exc:
        failure = {
            "schema_version": "b0_prior_challenge_failure_v1",
            "status": "halted_fail_closed",
            "chapter_id": chapter_id,
            "model_id": model_id,
            "quota_bucket_id": quota_bucket_id,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "usage": observed_usage,
            "finish_reason": finish_reason,
            "production_publish_performed": False,
            "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
            "failed_at": _now(),
        }
        _write_json(output / "experiment_failure.json", failure)
        _write_json(
            output / "run_manifest.json",
            {**run_manifest, "status": "halted", "failed_at": failure["failed_at"]},
        )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded B0 prior-card challenge")
    parser.add_argument("mode", choices=("dry", "live"))
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    prior_group = parser.add_mutually_exclusive_group(required=True)
    prior_group.add_argument("--prior-cards", type=Path)
    prior_group.add_argument("--prior-bundle", type=Path)
    parser.add_argument("--corruption-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--keys-file", type=Path)
    parser.add_argument("--quota-bucket-id", choices=BUCKET_IDS)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "dry":
        report = run_dry(
            document_path=args.document,
            design_doc=args.design_doc,
            chapter_id=args.chapter_id,
            prior_cards_path=args.prior_cards,
            prior_bundle_path=args.prior_bundle,
            corruption_manifest_path=args.corruption_manifest,
            output_dir=args.output_dir,
        )
    else:
        if not args.approved_envelope_hash or not args.keys_file or not args.quota_bucket_id:
            raise PriorChallengeRunError(
                "live mode requires approved envelope, keys file, and quota bucket"
            )
        report = run_live(
            document_path=args.document,
            design_doc=args.design_doc,
            frozen_db=args.frozen_db,
            chapter_id=args.chapter_id,
            prior_cards_path=args.prior_cards,
            prior_bundle_path=args.prior_bundle,
            corruption_manifest_path=args.corruption_manifest,
            output_dir=args.output_dir,
            approved_envelope_hash=args.approved_envelope_hash,
            keys_file=args.keys_file,
            quota_bucket_id=args.quota_bucket_id,
            usage_roots=args.usage_root or [RUNTIME_ROOT / "data"],
        )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
