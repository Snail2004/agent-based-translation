from __future__ import annotations

import hashlib
import json
import math
from typing import Any


FORMULA_CLUSTER_SCHEMA_VERSION = "pdf_formula_cluster_v1"
FORMULA_CLUSTER_ROLES = frozenset({"publication_visual", "duplicate_evidence"})

_CLUSTER_FIELDS = {
    "schema_version",
    "formula_cluster_id",
    "doc_id",
    "source_sha256",
    "normalizer_version",
    "page_number",
    "detector_region_ids",
    "members",
    "publication_block_id",
    "publication_bbox_pdf",
}
_MEMBER_FIELDS = {
    "block_id",
    "role",
    "bbox_pdf",
    "detector_region_ids",
}


class PdfFormulaClusterError(ValueError):
    pass


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonempty_string(value: Any, *, owner: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PdfFormulaClusterError(f"{owner} must be a non-empty string")
    return value


def _bbox(value: Any, *, owner: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PdfFormulaClusterError(f"{owner} must be a four-number PDF box")
    if not all(isinstance(item, (int, float)) for item in value):
        raise PdfFormulaClusterError(f"{owner} must be a four-number PDF box")
    result = [float(item) for item in value]
    if (
        not all(math.isfinite(item) for item in result)
        or result[2] <= result[0]
        or result[3] <= result[1]
    ):
        raise PdfFormulaClusterError(f"{owner} must have positive finite area")
    return result


def build_formula_cluster(
    *,
    doc_id: str,
    source_sha256: str,
    normalizer_version: str,
    page_number: int,
    detector_region_ids: list[str] | tuple[str, ...],
    members: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    publication_block_id: str,
    publication_bbox_pdf: list[float] | tuple[float, float, float, float],
) -> dict[str, Any]:
    doc_id = _nonempty_string(doc_id, owner="formula cluster doc_id")
    source_sha256 = _nonempty_string(
        source_sha256, owner="formula cluster source_sha256"
    )
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise PdfFormulaClusterError(
            "formula cluster source_sha256 must be lowercase SHA-256"
        )
    normalizer_version = _nonempty_string(
        normalizer_version, owner="formula cluster normalizer_version"
    )
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise PdfFormulaClusterError("formula cluster page_number must be positive")

    region_ids = [
        _nonempty_string(value, owner="formula cluster detector region id")
        for value in detector_region_ids
    ]
    if not region_ids or len(region_ids) != len(set(region_ids)):
        raise PdfFormulaClusterError(
            "formula cluster detector region ids must be unique and non-empty"
        )

    normalized_members: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict) or set(member) != _MEMBER_FIELDS:
            raise PdfFormulaClusterError(
                f"formula cluster member {index} has an invalid shape"
            )
        block_id = _nonempty_string(
            member.get("block_id"), owner=f"formula cluster member {index} block_id"
        )
        role = _nonempty_string(
            member.get("role"), owner=f"formula cluster member {index} role"
        )
        if role not in FORMULA_CLUSTER_ROLES:
            raise PdfFormulaClusterError(
                f"formula cluster member {index} has an unknown role"
            )
        member_region_ids = [
            _nonempty_string(
                value,
                owner=f"formula cluster member {index} detector region id",
            )
            for value in member.get("detector_region_ids") or []
        ]
        if (
            not member_region_ids
            or len(member_region_ids) != len(set(member_region_ids))
            or not set(member_region_ids).issubset(region_ids)
        ):
            raise PdfFormulaClusterError(
                f"formula cluster member {index} has invalid detector region ids"
            )
        normalized_members.append(
            {
                "block_id": block_id,
                "role": role,
                "bbox_pdf": _bbox(
                    member.get("bbox_pdf"),
                    owner=f"formula cluster member {index} bbox_pdf",
                ),
                "detector_region_ids": member_region_ids,
            }
        )

    member_ids = [member["block_id"] for member in normalized_members]
    if len(member_ids) != len(set(member_ids)):
        raise PdfFormulaClusterError("formula cluster member block ids must be unique")
    publication_members = [
        member
        for member in normalized_members
        if member["role"] == "publication_visual"
    ]
    if len(publication_members) != 1:
        raise PdfFormulaClusterError(
            "formula cluster must contain exactly one publication visual"
        )
    publication_block_id = _nonempty_string(
        publication_block_id, owner="formula cluster publication_block_id"
    )
    if publication_members[0]["block_id"] != publication_block_id:
        raise PdfFormulaClusterError(
            "formula cluster publication block disagrees with member roles"
        )
    publication_bbox = _bbox(
        publication_bbox_pdf, owner="formula cluster publication_bbox_pdf"
    )
    if publication_members[0]["bbox_pdf"] != publication_bbox:
        raise PdfFormulaClusterError(
            "formula cluster publication geometry disagrees with its member"
        )

    payload = {
        "schema_version": FORMULA_CLUSTER_SCHEMA_VERSION,
        "doc_id": doc_id,
        "source_sha256": source_sha256,
        "normalizer_version": normalizer_version,
        "page_number": page_number,
        "detector_region_ids": region_ids,
        "members": normalized_members,
        "publication_block_id": publication_block_id,
        "publication_bbox_pdf": publication_bbox,
    }
    return {
        **payload,
        "formula_cluster_id": "fcl_" + _canonical_sha256(payload)[:24],
    }


def validate_formula_cluster(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CLUSTER_FIELDS:
        raise PdfFormulaClusterError("formula cluster has an invalid shape")
    expected = build_formula_cluster(
        doc_id=value.get("doc_id"),
        source_sha256=value.get("source_sha256"),
        normalizer_version=value.get("normalizer_version"),
        page_number=value.get("page_number"),
        detector_region_ids=value.get("detector_region_ids") or [],
        members=value.get("members") or [],
        publication_block_id=value.get("publication_block_id"),
        publication_bbox_pdf=value.get("publication_bbox_pdf"),
    )
    if value != expected:
        raise PdfFormulaClusterError("formula cluster identity or payload was tampered")
    return expected


__all__ = [
    "FORMULA_CLUSTER_ROLES",
    "FORMULA_CLUSTER_SCHEMA_VERSION",
    "PdfFormulaClusterError",
    "build_formula_cluster",
    "validate_formula_cluster",
]
