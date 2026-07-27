from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.literary.b4_address_anchor_v1 import load_style_profile_v1
from pipeline.literary.b4_translator_v1 import (
    assemble_translation_chapter_v1,
    assert_reference_scoring_allowed_v1,
    build_translation_window_artifact_v1,
    render_translation_window_request_v1,
    validate_translation_window_response_v1,
)
from pipeline.literary.chapter_source_document_v1 import (
    chapter_from_document_v1,
    load_literary_source_document_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, validate, or assemble B4 Translator windows"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare-window")
    prepare.add_argument("--translator-pack", type=Path, required=True)
    prepare.add_argument("--address-anchor", type=Path, required=True)
    prepare.add_argument("--window-slice", type=Path, required=True)
    prepare.add_argument("--document", type=Path, required=True)
    prepare.add_argument("--style-design", type=Path, required=True)
    prepare.add_argument("--style-profile-version", required=True)
    prepare.add_argument("--measured-arm", action="store_true")
    prepare.add_argument("--accepted-tail", type=Path)
    prepare.add_argument("--out-dir", type=Path, required=True)

    validate = commands.add_parser("validate-window")
    validate.add_argument("--prepared-root", type=Path, required=True)
    validate.add_argument("--response", type=Path, required=True)
    validate.add_argument("--provider-receipt", type=Path)
    validate.add_argument("--out-dir", type=Path, required=True)

    assemble = commands.add_parser("assemble-chapter")
    assemble.add_argument("--translator-pack", type=Path, required=True)
    assemble.add_argument("--address-anchor", type=Path, required=True)
    assemble.add_argument("--window-plan", type=Path, required=True)
    assemble.add_argument("--window-artifact", type=Path, action="append", required=True)
    assemble.add_argument("--document", type=Path, required=True)
    assemble.add_argument("--assert-reference-scoreable", action="store_true")
    assemble.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare-window":
        result = _prepare_window(
            pack_path=args.translator_pack,
            anchor_path=args.address_anchor,
            window_path=args.window_slice,
            document_path=args.document,
            style_design=args.style_design,
            style_profile_version=args.style_profile_version,
            measured_arm=args.measured_arm,
            accepted_tail_path=args.accepted_tail,
            out_dir=args.out_dir,
        )
    elif args.command == "validate-window":
        result = _validate_window(
            prepared_root=args.prepared_root,
            response_path=args.response,
            provider_receipt_path=args.provider_receipt,
            out_dir=args.out_dir,
        )
    else:
        result = _assemble_chapter(
            pack_path=args.translator_pack,
            anchor_path=args.address_anchor,
            window_plan_path=args.window_plan,
            window_artifact_paths=args.window_artifact,
            document_path=args.document,
            assert_scoreable=args.assert_reference_scoreable,
            out_dir=args.out_dir,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _prepare_window(
    *,
    pack_path: Path,
    anchor_path: Path,
    window_path: Path,
    document_path: Path,
    style_design: Path,
    style_profile_version: str,
    measured_arm: bool,
    accepted_tail_path: Path | None,
    out_dir: Path,
) -> dict[str, Any]:
    pack_bytes = Path(pack_path).read_bytes()
    anchor_bytes = Path(anchor_path).read_bytes()
    window_bytes = Path(window_path).read_bytes()
    window = _json_bytes(window_bytes, "window slice")
    document = load_literary_source_document_v1(document_path)
    chapter = chapter_from_document_v1(document, str(window["chapter_id"]))
    style_profile = load_style_profile_v1(
        design_doc=style_design,
        style_profile_version=style_profile_version,
    )
    accepted_tail = _read(accepted_tail_path) if accepted_tail_path else {}
    rendered = render_translation_window_request_v1(
        style_profile=style_profile,
        style_profile_version=style_profile_version,
        measured_arm=measured_arm,
        translator_pack_bytes=pack_bytes,
        address_anchor_bytes=anchor_bytes,
        window_slice_bytes=window_bytes,
        chapter=chapter,
        accepted_tail_translations={
            str(key): str(value) for key, value in accepted_tail.items()
        },
    )
    output = _fresh(out_dir)
    (output / "translator_pack.json").write_bytes(pack_bytes)
    (output / "address_anchor.json").write_bytes(anchor_bytes)
    (output / "window_slice.json").write_bytes(window_bytes)
    (output / "style_profile.txt").write_text(style_profile, encoding="utf-8")
    _write(output / "chapter.json", chapter)
    _write(output / "accepted_tail.json", accepted_tail)
    _write(
        output / "metadata.json",
        {
            "style_profile_version": style_profile_version,
            "measured_arm": measured_arm,
        },
    )
    _write(
        output / "request.json",
        {
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": rendered.response_schema,
            "request_fingerprint": rendered.request_fingerprint,
            "stable_prefix_sha256": rendered.stable_prefix_sha256,
        },
    )
    report = {
        "status": "ready",
        "chapter_id": rendered.window_slice["chapter_id"],
        "window_id": rendered.window_slice["window_id"],
        "active_block_count": len(rendered.window_slice["active_block_ids"]),
        "tail_block_count": len(
            rendered.window_slice["preceding_tail_block_ids"]
        ),
        "request_fingerprint": rendered.request_fingerprint,
        "stable_prefix_sha256": rendered.stable_prefix_sha256,
        "estimated_prompt_tokens": estimate_prompt_tokens(
            rendered.messages, rendered.response_schema
        ),
        "cached_input_tokens": None,
        "provider_calls": 0,
    }
    _write(output / "prepare_report.json", report)
    return report


def _validate_window(
    *,
    prepared_root: Path,
    response_path: Path,
    provider_receipt_path: Path | None,
    out_dir: Path,
) -> dict[str, Any]:
    root = Path(prepared_root).resolve()
    metadata = _read(root / "metadata.json")
    rendered = render_translation_window_request_v1(
        style_profile=(root / "style_profile.txt").read_text(encoding="utf-8"),
        style_profile_version=str(metadata["style_profile_version"]),
        measured_arm=bool(metadata["measured_arm"]),
        translator_pack_bytes=(root / "translator_pack.json").read_bytes(),
        address_anchor_bytes=(root / "address_anchor.json").read_bytes(),
        window_slice_bytes=(root / "window_slice.json").read_bytes(),
        chapter=_read(root / "chapter.json"),
        accepted_tail_translations={
            str(key): str(value)
            for key, value in _read(root / "accepted_tail.json").items()
        },
    )
    prepared_request = _read(root / "request.json")
    if (
        prepared_request.get("request_fingerprint")
        != rendered.request_fingerprint
        or prepared_request.get("stable_prefix_sha256")
        != rendered.stable_prefix_sha256
    ):
        raise SystemExit("prepared Translator request differs")
    validated = validate_translation_window_response_v1(
        rendered=rendered,
        response=_read(response_path),
    )
    receipt = _read(provider_receipt_path) if provider_receipt_path else None
    artifact = build_translation_window_artifact_v1(
        validated_response=validated,
        provider_receipt=receipt,
        provider_called=receipt is not None,
    )
    output = _fresh(out_dir)
    _write(output / "validated_response.json", validated)
    _write(output / "translation_window.json", artifact)
    report = {
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "window_id": artifact["window_id"],
        "translated_block_count": len(artifact["blocks"]),
        "translator_output_contract": artifact["translator_output_contract"],
        "address_metadata_collected": artifact["address_metadata_collected"],
        "cached_input_tokens": None,
        "provider_calls": 1 if receipt is not None else 0,
        "artifact_hash": artifact["artifact_hash"],
    }
    _write(output / "validation_report.json", report)
    return report


def _assemble_chapter(
    *,
    pack_path: Path,
    anchor_path: Path,
    window_plan_path: Path,
    window_artifact_paths: Sequence[Path],
    document_path: Path,
    assert_scoreable: bool,
    out_dir: Path,
) -> dict[str, Any]:
    pack = _read(pack_path)
    anchor = _read(anchor_path)
    plan = _read(window_plan_path)
    document = load_literary_source_document_v1(document_path)
    chapter = chapter_from_document_v1(document, str(pack["chapter_id"]))
    artifact = assemble_translation_chapter_v1(
        translator_pack=pack,
        address_anchor=anchor,
        window_plan=plan,
        window_artifacts=[_read(path) for path in window_artifact_paths],
        chapter=chapter,
    )
    if assert_scoreable:
        assert_reference_scoring_allowed_v1(artifact)
    output = _fresh(out_dir)
    _write(output / f"translation_{artifact['chapter_id']}.json", artifact)
    report = {
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "translated_block_count": len(artifact["blocks"]),
        "window_count": len(artifact["window_artifact_hashes"]),
        "translator_output_contract": artifact["translator_output_contract"],
        "address_metadata_collected": artifact["address_metadata_collected"],
        "style_profile_version": artifact["style_profile_version"],
        "measured_arm": artifact["measured_arm"],
        "reference_based_scoring_allowed": artifact["measured_arm"] is True,
        "artifact_hash": artifact["artifact_hash"],
    }
    _write(output / "assembly_report.json", report)
    return report


def _fresh(path: Path) -> Path:
    root = Path(path).resolve()
    if root.exists():
        raise SystemExit(f"output directory already exists: {root}")
    root.mkdir(parents=True)
    return root


def _read(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot load JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"JSON object required: {path}")
    return dict(value)


def _json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"{label} is not UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"{label} must be a JSON object")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
