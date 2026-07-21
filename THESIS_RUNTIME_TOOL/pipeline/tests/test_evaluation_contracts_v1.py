from __future__ import annotations

import copy
import math

import pytest

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_json,
    canonical_sha256,
    canonicalize,
    require_relative_path,
    require_exact_keys,
    seal_payload,
    verify_payload_hash,
)


POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("tags",)}),
    semantic_sequence_paths=frozenset({("steps",)}),
)


def test_canonicalization_sorts_only_declared_set_like_arrays():
    first = {"tags": ["z", "a"], "steps": ["first", "second"]}
    second = {"steps": ["first", "second"], "tags": ["a", "z"]}

    assert canonical_json(first, policy=POLICY) == canonical_json(second, policy=POLICY)
    assert canonicalize(first, policy=POLICY)["steps"] == ["first", "second"]

    reversed_steps = {"tags": ["a", "z"], "steps": ["second", "first"]}
    assert canonical_sha256(first, policy=POLICY) != canonical_sha256(
        reversed_steps, policy=POLICY
    )


def test_canonicalizer_rejects_unclassified_and_duplicate_set_items():
    with pytest.raises(ContractValidationError, match="ordering_policy_unclassified"):
        canonical_json({"unknown": []}, policy=POLICY)

    with pytest.raises(ContractValidationError, match="duplicate_set_item"):
        canonical_json({"tags": ["same", "same"], "steps": []}, policy=POLICY)


def test_ordering_table_rejects_double_classification():
    with pytest.raises(ValueError, match="classified twice"):
        CanonicalPolicy(
            set_like_paths=frozenset({("rows",)}),
            semantic_sequence_paths=frozenset({("rows",)}),
        )


def test_closed_schema_rejects_non_string_keys_with_contract_error():
    with pytest.raises(ContractValidationError, match="non_string_key"):
        require_exact_keys({"name": "value", 1: "invalid"}, required={"name"}, path="$")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonicalizer_rejects_non_finite_numbers(value):
    with pytest.raises(ContractValidationError, match="non_finite"):
        canonical_json({"tags": [], "steps": [value]}, policy=POLICY)


def test_validation_and_canonicalization_do_not_mutate_input():
    payload = {"tags": ["z", "a"], "steps": ["one", "two"]}
    before = copy.deepcopy(payload)

    result = canonicalize(payload, policy=POLICY)

    assert payload == before
    assert result is not payload
    assert result["tags"] == ["a", "z"]


def test_nested_self_hash_explicitly_excludes_only_the_hash_field():
    policy = CanonicalPolicy(
        set_like_paths=frozenset({("tags",)}),
        semantic_sequence_paths=frozenset(),
    )
    source = {"name": "fixture", "tags": ["b", "a"], "integrity": {}}
    sealed = seal_payload(
        source, policy=policy, hash_path=("integrity", "payload_sha256")
    )

    assert "payload_sha256" not in source["integrity"]
    assert verify_payload_hash(
        sealed, policy=policy, hash_path=("integrity", "payload_sha256")
    )
    sealed["name"] = "tampered"
    assert not verify_payload_hash(
        sealed, policy=policy, hash_path=("integrity", "payload_sha256")
    )


@pytest.mark.parametrize(
    "value",
    [
        "../escape.json",
        "/absolute.json",
        "C:/drive.json",
        "folder\\file.json",
        ".",
        "folder/./file.json",
        "folder//file.json",
    ],
)
def test_relative_path_guard_rejects_escape_and_platform_specific_paths(value):
    with pytest.raises(ContractValidationError, match="unsafe_path"):
        require_relative_path(value, path="$.path")
