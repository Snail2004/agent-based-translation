from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

import pytest

from pipeline.translate.d2l_translation_quality_auditor_v1 import (
    AuditContractError,
    AuditPacketCaps,
    AUDIT_REASK_POLICY_ID,
    AUDIT_USER_PROMPT_TEMPLATE,
    INPUT_CONTRACT_VERSION,
    PROMPT_ID,
    RESPONSE_CONTRACT_VERSION,
    RESPONSE_JSON_SCHEMA,
    SYSTEM_PROMPT,
    TARGETED_REPAIR_PROMPT_TEMPLATE,
    audit_reask_note,
    audit_reask_policy_sha256,
    build_audit_packet,
    build_semantic_contract_manifest,
    canonical_json,
    content_sha256,
    mint_finding_ids,
    parse_audit_json,
    prompt_sha256,
    response_schema_sha256,
    render_audit_messages,
    render_targeted_repair_note,
    targeted_repair_prompt_sha256,
    validate_audit_packet,
    validate_audit_response,
    validate_semantic_contract_manifest,
)
from pipeline.translate.d2l_quality_gates_v2 import DEFAULT_POLICY
from pipeline.translate.d2l_translation_quality_state_v1 import POLICY_ID as STATE_POLICY_ID


def _finding(block_id: str = "b1") -> dict:
    return {
        "block_id": block_id,
        "issue_type": "unexpected_output_script",
        "severity": "major",
        "evidence_source": "The value remains unknown.",
        "evidence_target": "Gia tri loi.",
        "details": {"script": "Arabic", "surface": "bad", "start": 9, "end": 12},
    }


def _blocks() -> list[dict]:
    return [
        {
            "block_id": "b1",
            "block_type": "prose",
            "source_audit_text": "The value remains unknown.",
            "target_audit_text": "Gia tri loi.",
            "applicable_glossary_refs": ["g1"],
            "deterministic_findings": [_finding()],
        },
        {
            "block_id": "b2",
            "block_type": "heading",
            "source_audit_text": "Probability Distributions",
            "target_audit_text": "Phan phoi xac suat",
            "applicable_glossary_refs": [],
            "deterministic_findings": [],
        },
    ]


def _cards() -> list[dict]:
    return [
        {
            "glossary_ref": "g1",
            "source_term": "value",
            "allowed_target_variants": ["gia tri"],
            "policy": "context_sensitive",
        }
    ]


def _receipt() -> dict:
    return {
        "policy_id": "protected_v1",
        "exact_cover": True,
        "source_target_ref_order_equal": True,
        "raw_protected_payload_visible": False,
    }


def _packet(**overrides) -> dict:
    values = {
        "window_id": "w1",
        "blocks": _blocks(),
        "glossary_cards": _cards(),
        "protected_receipt": _receipt(),
        "caps": AuditPacketCaps(4, 10_000),
        "glossary_token_counter": len,
    }
    values.update(overrides)
    return build_audit_packet(**values)


def _clean_response() -> dict:
    return {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "window_id": "w1",
        "audited_block_ids": ["b1", "b2"],
        "findings": [],
    }


def test_packet_is_blind_bounded_and_canonical() -> None:
    packet = _packet(
        context_only_neighbors=[
            {
                "block_id": "b0",
                "position": "preceding",
                "source_audit_text": "Earlier context.",
                "target_audit_text": "Ngu canh truoc.",
            }
        ]
    )
    validate_audit_packet(
        packet,
        caps=AuditPacketCaps(4, 10_000),
        glossary_token_counter=len,
    )
    serialized = canonical_json(packet)

    assert packet["contract_version"] == INPUT_CONTRACT_VERSION
    assert [row["block_id"] for row in packet["blocks"]] == ["b1", "b2"]
    assert "arm_id" not in packet
    assert "model" not in packet
    assert "provider" not in packet
    assert "translation_retry_used" not in packet
    assert "S0" not in serialized and "S1" not in serialized
    assert packet["protected_receipt"]["raw_protected_payload_visible"] is False


def test_rendered_prompt_is_book_neutral_and_packet_terminated() -> None:
    packet = _packet()
    messages = render_audit_messages(
        packet,
        caps=AuditPacketCaps(4, 10_000),
        glossary_token_counter=len,
    )

    assert PROMPT_ID == "d2l_translation_quality_audit_v1"
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert "You are the D2L" not in SYSTEM_PROMPT
    assert "You are the Technical Translation Quality Auditor" in SYSTEM_PROMPT
    assert "Do not provide replacement Vietnamese wording" in SYSTEM_PROMPT
    assert messages[1]["content"].endswith(canonical_json(packet))
    assert "{{packet_json}}" in AUDIT_USER_PROMPT_TEMPLATE
    assert prompt_sha256() == prompt_sha256()
    assert len(prompt_sha256()) == 64


def test_runtime_system_prompt_is_byte_identical_to_reviewed_spec() -> None:
    runtime_root = Path(__file__).resolve().parents[2]
    task = (
        runtime_root / "tasks" / "TASK_D2L_TRANSLATOR_QUALITY_AUDITOR_V1.md"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"### 10\.1 System prompt:.*?\n\n```text\n(.*?)\n```",
        task,
        re.DOTALL,
    )
    assert match is not None
    assert match.group(1) == SYSTEM_PROMPT

    repair_match = re.search(
        r"Exact repair-note template.*?\n\n```text\n(.*?)\n```",
        task,
        re.DOTALL,
    )
    assert repair_match is not None
    assert repair_match.group(1) == TARGETED_REPAIR_PROMPT_TEMPLATE


def test_response_schema_is_closed_and_local_clean_response_passes() -> None:
    packet = _packet()
    validated = validate_audit_response(_clean_response(), packet)

    assert RESPONSE_JSON_SCHEMA["additionalProperties"] is False
    assert validated == _clean_response()


def test_valid_omission_and_addition_evidence_exceptions() -> None:
    packet = _packet()
    response = _clean_response()
    response["findings"] = [
        {
            "block_id": "b1",
            "issue_type": "meaning_omission",
            "severity": "major",
            "source_evidence": "remains unknown",
            "target_evidence": "",
            "reason": "The uncertainty statement is absent.",
        },
        {
            "block_id": "b2",
            "issue_type": "unsupported_addition",
            "severity": "major",
            "source_evidence": "",
            "target_evidence": "xac suat",
            "reason": "The target adds an unsupported qualification.",
        },
    ]

    assert len(validate_audit_response(response, packet)["findings"]) == 2


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.update(extra="bad"), "fields mismatch"),
        (lambda row: row.update(window_id="foreign"), "window_id mismatch"),
        (
            lambda row: row.update(audited_block_ids=["b2", "b1"]),
            "exact-cover",
        ),
        (
            lambda row: row.update(audited_block_ids=["b1"]),
            "exact-cover",
        ),
    ],
)
def test_response_rejects_extra_foreign_missing_or_reordered_contract(
    mutate, message: str
) -> None:
    packet = _packet()
    response = _clean_response()
    mutate(response)

    with pytest.raises(AuditContractError, match=message):
        validate_audit_response(response, packet)


def test_response_rejects_nonexact_evidence_and_foreign_block() -> None:
    packet = _packet()
    base = {
        "block_id": "b1",
        "issue_type": "terminology_context_error",
        "severity": "major",
        "source_evidence": "value",
        "target_evidence": "Gia tri",
        "reason": "The local technical sense is wrong.",
    }

    for key, value, message in [
        ("source_evidence", "not in source", "source evidence"),
        ("target_evidence", "not in target", "target evidence"),
        ("block_id", "foreign", "foreign block_id"),
    ]:
        response = _clean_response()
        finding = dict(base)
        finding[key] = value
        response["findings"] = [finding]
        with pytest.raises(AuditContractError, match=message):
            validate_audit_response(response, packet)


def test_response_rejects_unknown_type_style_major_and_unbounded_semantic_other() -> None:
    packet = _packet()
    base = {
        "block_id": "b1",
        "issue_type": "terminology_context_error",
        "severity": "major",
        "source_evidence": "value",
        "target_evidence": "Gia tri",
        "reason": "A concrete problem.",
    }
    mutations = [
        ({"issue_type": "rewrite_it"}, "unknown issue_type"),
        (
            {"issue_type": "style_or_fluency_advisory", "severity": "major"},
            "must be advisory",
        ),
        (
            {
                "issue_type": "semantic_other",
                "source_evidence": "",
                "target_evidence": "Gia tri",
            },
            "requires source evidence",
        ),
    ]
    for changes, message in mutations:
        response = _clean_response()
        finding = dict(base)
        finding.update(changes)
        response["findings"] = [finding]
        with pytest.raises(AuditContractError, match=message):
            validate_audit_response(response, packet)


def test_duplicate_findings_and_duplicate_json_keys_fail_closed() -> None:
    packet = _packet()
    finding = {
        "block_id": "b1",
        "issue_type": "terminology_context_error",
        "severity": "major",
        "source_evidence": "value",
        "target_evidence": "Gia tri",
        "reason": "A concrete problem.",
    }
    response = _clean_response()
    response["findings"] = [finding, dict(finding)]
    with pytest.raises(AuditContractError, match="duplicates"):
        validate_audit_response(response, packet)
    with pytest.raises(AuditContractError, match="Duplicate audit JSON keys"):
        parse_audit_json(
            '{"contract_version":"x","window_id":"w1","window_id":"w2"}'
        )
    with pytest.raises(AuditContractError, match="non-finite number"):
        parse_audit_json('{"value":NaN}')


def test_finding_ids_are_stable_and_packet_material() -> None:
    packet = _packet()
    response = _clean_response()
    response["findings"] = [
        {
            "block_id": "b1",
            "issue_type": "meaning_omission",
            "severity": "major",
            "source_evidence": "remains unknown",
            "target_evidence": "",
            "reason": "The uncertainty statement is absent.",
        }
    ]
    first = mint_finding_ids(packet, response)
    second = mint_finding_ids(packet, response)
    changed = deepcopy(packet)
    changed["blocks"][0]["target_audit_text"] += " Them."
    third = mint_finding_ids(changed, response)

    assert first == second
    assert first[0]["finding_id"].startswith("dqaf_")
    assert first[0]["finding_id"] != third[0]["finding_id"]
    assert content_sha256(packet) != content_sha256(changed)


def test_glossary_card_and_token_caps_fail_without_truncation() -> None:
    with pytest.raises(AuditContractError, match="Glossary card cap exceeded"):
        _packet(caps=AuditPacketCaps(0, 10_000))
    with pytest.raises(AuditContractError, match="Glossary token cap exceeded"):
        _packet(caps=AuditPacketCaps(4, 1))


def test_mutated_packet_cannot_bypass_caps_at_prompt_render() -> None:
    packet = _packet()
    packet["glossary_cards"].append(
        {
            "glossary_ref": "g2",
            "source_term": "extra",
            "allowed_target_variants": ["them"],
            "policy": "advisory",
        }
    )
    with pytest.raises(AuditContractError, match="sealed glossary card cap"):
        render_audit_messages(
            packet,
            caps=AuditPacketCaps(1, 10_000),
            glossary_token_counter=len,
        )


def test_packet_rejects_unknown_glossary_ref_bad_receipt_and_neighbor_growth() -> None:
    blocks = _blocks()
    blocks[0]["applicable_glossary_refs"] = ["foreign"]
    with pytest.raises(AuditContractError, match="unknown glossary"):
        _packet(blocks=blocks)

    receipt = _receipt()
    receipt["raw_protected_payload_visible"] = True
    with pytest.raises(AuditContractError, match="must not be visible"):
        _packet(protected_receipt=receipt)

    neighbors = [
        {
            "block_id": f"n{i}",
            "position": "preceding" if i == 0 else "following",
            "source_audit_text": "Source.",
            "target_audit_text": "Target.",
        }
        for i in range(3)
    ]
    with pytest.raises(AuditContractError, match="At most one preceding"):
        _packet(context_only_neighbors=neighbors)


def test_deterministic_finding_must_stay_on_its_block() -> None:
    blocks = _blocks()
    blocks[0]["deterministic_findings"][0]["block_id"] = "b2"
    with pytest.raises(AuditContractError, match="block_id mismatch"):
        _packet(blocks=blocks)


def test_deterministic_finding_type_and_severity_are_closed() -> None:
    blocks = _blocks()
    blocks[0]["deterministic_findings"][0]["issue_type"] = "suggested_answer"
    with pytest.raises(AuditContractError, match="outside the closed enum"):
        _packet(blocks=blocks)

    blocks = _blocks()
    blocks[0]["deterministic_findings"][0].update(
        issue_type="gross_length_anomaly",
        severity="major",
    )
    with pytest.raises(AuditContractError, match="severity does not match"):
        _packet(blocks=blocks)


def test_deterministic_details_cannot_leak_unsealed_metadata_or_wrong_types() -> None:
    blocks = _blocks()
    blocks[0]["deterministic_findings"][0]["details"]["model"] = "hidden-model"
    with pytest.raises(AuditContractError, match="details fields mismatch"):
        _packet(blocks=blocks)

    blocks = _blocks()
    blocks[0]["deterministic_findings"][0]["details"]["start"] = True
    with pytest.raises(AuditContractError, match="must be nonnegative integer"):
        _packet(blocks=blocks)


def test_audit_reask_contains_validator_errors_without_answer() -> None:
    note = audit_reask_note(["foreign block_id", "missing field"])
    assert AUDIT_REASK_POLICY_ID == "d2l_translation_quality_audit_reask_v1"
    assert RESPONSE_CONTRACT_VERSION in note
    assert "foreign block_id" in note
    assert "replacement translations" in note
    assert "correct Vietnamese" not in note
    assert len(audit_reask_policy_sha256()) == 64


def test_targeted_repair_note_contains_only_closed_issue_evidence() -> None:
    note = render_targeted_repair_note(
        ["b1"],
        [
            {
                "block_id": "b1",
                "issue_type": "meaning_omission",
                "source_evidence": "remains unknown",
                "target_evidence": "",
            }
        ],
    )

    assert note.startswith(
        "QUALITY REPAIR REQUEST — d2l_translation_targeted_repair_v1\n\n"
    )
    assert '"b1"' in note
    assert "remains unknown" in note
    assert "replacement translations" in note
    assert "reason" not in note
    assert "corrected_translation" not in note
    assert len(targeted_repair_prompt_sha256()) == 64


def test_targeted_repair_note_rejects_foreign_missing_duplicate_or_free_form_rows() -> None:
    valid = {
        "block_id": "b1",
        "issue_type": "meaning_omission",
        "source_evidence": "source",
        "target_evidence": "",
    }
    cases = [
        (["b1"], [{**valid, "block_id": "foreign"}], "foreign block_id"),
        (["b1", "b2"], [valid], "lack issue evidence"),
        (["b1"], [valid, dict(valid)], "duplicated"),
        (["b1"], [{**valid, "reason": "write this answer"}], "fields mismatch"),
    ]
    for writable, rows, message in cases:
        with pytest.raises(AuditContractError, match=message):
            render_targeted_repair_note(writable, rows)


def test_semantic_contract_manifest_binds_prompt_schema_policy_caps_and_counter() -> None:
    caps = AuditPacketCaps(12, 900)
    manifest = build_semantic_contract_manifest(
        deterministic_policy_id=DEFAULT_POLICY.policy_id,
        deterministic_policy_sha256=DEFAULT_POLICY.sha256(),
        state_policy_id=STATE_POLICY_ID,
        caps=caps,
        glossary_token_counter_id="provider_tokenizer_exact_v1",
    )

    assert manifest["prompt_sha256"] == prompt_sha256()
    assert manifest["response_schema_sha256"] == response_schema_sha256()
    assert manifest["audit_reask_policy_sha256"] == audit_reask_policy_sha256()
    assert (
        manifest["targeted_repair_prompt_sha256"]
        == targeted_repair_prompt_sha256()
    )
    assert manifest["packet_caps"] == {
        "max_glossary_cards_per_window": 12,
        "max_glossary_tokens_per_window": 900,
    }
    validate_semantic_contract_manifest(manifest, deepcopy(manifest))

    for path, replacement in [
        (("prompt_sha256",), "0" * 64),
        (("response_schema_sha256",), "1" * 64),
        (("deterministic_policy_sha256",), "2" * 64),
        (("local_validator_id",), "foreign_validator"),
        (("audit_reask_policy_sha256",), "3" * 64),
        (("targeted_repair_prompt_sha256",), "4" * 64),
        (("packet_caps", "max_glossary_cards_per_window"), 13),
    ]:
        changed = deepcopy(manifest)
        owner = changed
        for key in path[:-1]:
            owner = owner[key]
        owner[path[-1]] = replacement
        with pytest.raises(AuditContractError, match="manifest drift"):
            validate_semantic_contract_manifest(changed, manifest)


def test_semantic_contract_manifest_rejects_unsealed_hash_or_counter() -> None:
    values = {
        "deterministic_policy_id": DEFAULT_POLICY.policy_id,
        "deterministic_policy_sha256": DEFAULT_POLICY.sha256(),
        "state_policy_id": STATE_POLICY_ID,
        "caps": AuditPacketCaps(12, 900),
        "glossary_token_counter_id": "counter_v1",
    }
    for key, replacement in [
        ("deterministic_policy_sha256", "not-a-hash"),
        ("glossary_token_counter_id", ""),
    ]:
        changed = dict(values)
        changed[key] = replacement
        with pytest.raises(AuditContractError):
            build_semantic_contract_manifest(**changed)
