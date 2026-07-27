from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.literary.b4_address_anchor_v1 import (
    build_address_anchor_artifact_v1,
    build_empty_address_anchor_artifact_v1,
    load_style_profile_v1,
    render_address_anchor_request_v1,
    validate_address_anchor_response_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or validate one B4 Address Anchor call"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--anchor-input", type=Path, required=True)
    prepare.add_argument("--style-design", type=Path, required=True)
    prepare.add_argument("--style-profile-version", required=True)
    prepare.add_argument("--measured-arm", action="store_true")
    prepare.add_argument("--out-dir", type=Path, required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--prepared-root", type=Path, required=True)
    validate.add_argument("--response", type=Path, required=True)
    validate.add_argument("--provider-receipt", type=Path)
    validate.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        result = _prepare(
            anchor_input_path=args.anchor_input,
            style_design=args.style_design,
            style_profile_version=args.style_profile_version,
            measured_arm=args.measured_arm,
            out_dir=args.out_dir,
        )
    else:
        result = _validate(
            prepared_root=args.prepared_root,
            response_path=args.response,
            provider_receipt_path=args.provider_receipt,
            out_dir=args.out_dir,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _prepare(
    *,
    anchor_input_path: Path,
    style_design: Path,
    style_profile_version: str,
    measured_arm: bool,
    out_dir: Path,
) -> dict[str, Any]:
    output = _fresh(out_dir)
    anchor_input = _read(anchor_input_path)
    style_profile = load_style_profile_v1(
        design_doc=style_design,
        style_profile_version=style_profile_version,
    )
    _write(output / "anchor_input.json", anchor_input)
    (output / "style_profile.txt").write_text(style_profile, encoding="utf-8")
    metadata = {
        "style_profile_version": style_profile_version,
        "measured_arm": measured_arm,
    }
    _write(output / "metadata.json", metadata)
    if not anchor_input.get("pairs"):
        artifact = build_empty_address_anchor_artifact_v1(
            anchor_input=anchor_input,
            style_profile_version=style_profile_version,
            measured_arm=measured_arm,
        )
        _write(output / "address_anchor.json", artifact)
        report = {
            "status": "complete_no_call",
            "chapter_id": artifact["chapter_id"],
            "pair_count": 0,
            "provider_calls": 0,
            "artifact_hash": artifact["artifact_hash"],
        }
        _write(output / "prepare_report.json", report)
        return report

    rendered = render_address_anchor_request_v1(
        anchor_input=anchor_input,
        style_profile=style_profile,
        style_profile_version=style_profile_version,
        measured_arm=measured_arm,
    )
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": rendered.response_schema,
        "request_fingerprint": rendered.request_fingerprint,
    }
    _write(output / "request.json", request)
    _write(output / "packet.json", rendered.packet)
    _write(output / "response_schema.json", rendered.response_schema)
    report = {
        "status": "ready",
        "chapter_id": anchor_input["chapter_id"],
        "pair_count": len(anchor_input["pairs"]),
        "estimated_prompt_tokens": estimate_prompt_tokens(
            rendered.messages, rendered.response_schema
        ),
        "cached_input_tokens": None,
        "provider_calls": 0,
        "request_fingerprint": rendered.request_fingerprint,
        "style_profile_version": style_profile_version,
        "measured_arm": measured_arm,
    }
    _write(output / "prepare_report.json", report)
    return report


def _validate(
    *,
    prepared_root: Path,
    response_path: Path,
    provider_receipt_path: Path | None,
    out_dir: Path,
) -> dict[str, Any]:
    prepared = Path(prepared_root).resolve()
    metadata = _read(prepared / "metadata.json")
    anchor_input = _read(prepared / "anchor_input.json")
    style_profile = (prepared / "style_profile.txt").read_text(encoding="utf-8")
    rendered = render_address_anchor_request_v1(
        anchor_input=anchor_input,
        style_profile=style_profile,
        style_profile_version=str(metadata["style_profile_version"]),
        measured_arm=bool(metadata["measured_arm"]),
    )
    expected_request = _read(prepared / "request.json")
    if expected_request.get("request_fingerprint") != rendered.request_fingerprint:
        raise SystemExit("prepared Address Anchor request fingerprint differs")
    validated = validate_address_anchor_response_v1(
        rendered=rendered,
        response=_read(response_path),
    )
    receipt = _read(provider_receipt_path) if provider_receipt_path else None
    artifact = build_address_anchor_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt=receipt,
        provider_called=receipt is not None,
    )
    output = _fresh(out_dir)
    _write(output / "validated_response.json", validated)
    _write(output / "address_anchor.json", artifact)
    report = {
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "pair_count": len(artifact["pair_decisions"]),
        "review_issue_count": len(artifact["review_issues"]),
        "cached_input_tokens": None,
        "provider_calls": 1 if receipt is not None else 0,
        "artifact_hash": artifact["artifact_hash"],
    }
    _write(output / "validation_report.json", report)
    return report


def _fresh(path: Path) -> Path:
    root = Path(path).resolve()
    if root.exists():
        raise SystemExit(f"output directory already exists: {root}")
    root.mkdir(parents=True)
    return root


def _read(path: Path | None) -> dict[str, Any]:
    if path is None:
        raise SystemExit("required JSON path is absent")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot load JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"JSON object required: {path}")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
