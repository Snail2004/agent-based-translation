from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.run_chapter_registry_v2_gemini import (
    BUCKET_IDS,
    GEMINI_RESPONSE_SCHEMAS,
    load_gemini_credentials,
)


def test_load_gemini_credentials_exact_covers_rows_without_exposing_values(tmp_path: Path) -> None:
    values = ["AIza" + str(index) * 35 for index in range(1, 6)]
    path = tmp_path / "keys.txt"
    path.write_text("\n".join(values) + "\n", encoding="utf-8")

    credentials, commitments = load_gemini_credentials(path)

    assert tuple(credentials) == BUCKET_IDS
    assert tuple(commitments) == BUCKET_IDS
    assert all(value not in str(commitments) for value in values)
    assert all(len(commitment) == 64 for commitment in commitments.values())


def test_load_gemini_credentials_rejects_wrong_row_count(tmp_path: Path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("AIza" + "x" * 35 + "\n", encoding="utf-8")

    with pytest.raises(Exception, match="exactly 5"):
        load_gemini_credentials(path)


def test_gemini_response_schemas_exactly_cover_locked_top_level_contracts() -> None:
    expected = {
        "b0": {"gist", "narrator_hypotheses", "salient_surface_checklist"},
        "b1": {"new_entities", "new_aliases", "new_glossary_items", "local_bindings", "tickets"},
        "auditor": {
            "entity_dispositions",
            "alias_dispositions",
            "glossary_dispositions",
            "local_binding_dispositions",
            "ticket_dispositions",
            "profile_revisions",
        },
    }

    assert set(GEMINI_RESPONSE_SCHEMAS) == set(expected)
    for role, keys in expected.items():
        schema = GEMINI_RESPONSE_SCHEMAS[role]
        assert set(schema["properties"]) == keys
        assert set(schema["required"]) == keys
        assert schema["additionalProperties"] is False


def test_gemini_response_schemas_keep_locked_nullable_and_optional_fields() -> None:
    narrator_surface = GEMINI_RESPONSE_SCHEMAS["b0"]["properties"][
        "narrator_hypotheses"
    ]["items"]["properties"]["surface"]
    ticket_surface = GEMINI_RESPONSE_SCHEMAS["b1"]["properties"]["tickets"][
        "items"
    ]["properties"]["surface"]
    new_entity = GEMINI_RESPONSE_SCHEMAS["b1"]["properties"]["new_entities"]["items"]

    assert {row["type"] for row in narrator_surface["anyOf"]} == {"string", "null"}
    assert {row["type"] for row in ticket_surface["anyOf"]} == {"string", "null"}
    assert "initial_aliases" in new_entity["properties"]
    assert "initial_aliases" not in new_entity["required"]
