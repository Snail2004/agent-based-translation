from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2 import (
    FailoverStructuredModel,
    GoogleRouteSettings,
    ProviderResponseLedger,
    build_support_set_freeze,
    context_substitution_to_measurements,
    evaluate_gold_cases,
    reviewed_support_to_context_substitution_input,
    run_d2l_context_substitution,
    validate_d2l_context_substitution_run,
    validate_reviewed_support_bundle,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration import (
    DEVELOPMENT_HEURISTIC_POLICY,
    frozen_validation_policy,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, adapt, and run Context Substitution V2.2"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("support-freeze")
    freeze.add_argument("--candidate-index", type=Path, required=True)
    freeze.add_argument("--glossary", type=Path, required=True)
    freeze.add_argument("--document", type=Path, required=True)
    freeze.add_argument("--candidate-artifact", type=Path, action="append", default=[])
    freeze.add_argument("--output-dir", type=Path, required=True)
    freeze.add_argument("--sample-size", type=int, default=150)
    freeze.add_argument("--candidates-per-sense", type=int, default=3)
    freeze.add_argument("--primary-context-count", type=int, default=5)
    freeze.add_argument("--backup-context-count", type=int, default=3)
    freeze.add_argument("--seed", default="d2l_context_support_freeze_v1")
    freeze.add_argument("--dataset-version", default="d2l_context_support_freeze_v1")
    freeze.add_argument("--created-at", required=True)

    validate = commands.add_parser("reviewed-support-validate")
    _add_reviewed_source_args(validate, require_split=False)

    adapt = commands.add_parser("reviewed-support-to-runtime")
    _add_reviewed_source_args(adapt, require_split=True)
    adapt.add_argument("--review-artifact", type=Path)
    adapt.add_argument("--output", type=Path, required=True)
    adapt.add_argument("--receipt", type=Path, required=True)

    run = commands.add_parser("context-run")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--routes", type=Path, required=True)
    run.add_argument("--ledger-root", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--allow-api", action="store_true")
    run.add_argument("--candidate-target-id", action="append")
    run.add_argument(
        "--target-role",
        action="append",
        choices=("canonical", "alternative", "rejected", "pending"),
    )
    run.add_argument(
        "--evaluation-mode",
        choices=("DEVELOPMENT", "FROZEN_TEST_SET"),
        default="DEVELOPMENT",
    )
    run.add_argument("--calibration-artifact", type=Path)
    run.add_argument("--calibration-file-sha256")

    projection = commands.add_parser("measurements-project")
    projection.add_argument("--run", type=Path, required=True)
    projection.add_argument("--output", type=Path, required=True)

    gold = commands.add_parser("gold-evaluate")
    gold.add_argument("--cases", type=Path, required=True)
    gold.add_argument("--output", type=Path, required=True)
    return parser


def _add_reviewed_source_args(
    parser: argparse.ArgumentParser,
    *,
    require_split: bool,
) -> None:
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--parent-v3", type=Path)
    parser.add_argument("--expected-zip-sha256")
    parser.add_argument("--expected-parent-zip-sha256")
    parser.add_argument(
        "--source-split",
        choices=("development", "validation", "test"),
        required=require_split,
    )


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, delete=False, prefix=f".{path.name}.", suffix=".tmp"
    ) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _reviewed_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "parent_v3_source": args.parent_v3,
        "expected_zip_sha256": args.expected_zip_sha256,
        "expected_parent_zip_sha256": args.expected_parent_zip_sha256,
    }


def _route_settings(value: Any) -> list[GoogleRouteSettings]:
    if not isinstance(value, Mapping) or set(value) != {"routes"}:
        raise ValueError("routes file must contain exactly one routes list")
    rows = value["routes"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("routes must be a nonempty list")
    result: list[GoogleRouteSettings] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise ValueError(f"routes[{index}] must be an object")
        allowed = {
            "route_id",
            "model_id",
            "api_key_env",
            "base_url",
            "timeout_seconds",
            "model_family",
            "independence_group",
        }
        if set(item) - allowed:
            raise ValueError(f"routes[{index}] contains unknown fields")
        for key in ("route_id", "model_id", "api_key_env"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"routes[{index}].{key} must be a nonempty string")
        env_name = str(item["api_key_env"])
        api_key = os.environ.get(env_name, "")
        if not api_key:
            raise ValueError(f"routes[{index}] environment variable is missing: {env_name}")
        result.append(
            GoogleRouteSettings(
                route_id=str(item["route_id"]),
                model_id=str(item["model_id"]),
                api_key=api_key,
                base_url=(None if item.get("base_url") is None else str(item["base_url"])),
                timeout_seconds=int(item.get("timeout_seconds", 120)),
                model_family=(
                    None if item.get("model_family") is None else str(item["model_family"])
                ),
                independence_group=(
                    None
                    if item.get("independence_group") is None
                    else str(item["independence_group"])
                ),
            )
        )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "support-freeze":
        summary = build_support_set_freeze(
            candidate_index_path=args.candidate_index,
            glossary_path=args.glossary,
            document_path=args.document,
            candidate_artifact_paths=args.candidate_artifact,
            output_dir=args.output_dir,
            sample_size=args.sample_size,
            candidates_per_sense=args.candidates_per_sense,
            primary_context_count=args.primary_context_count,
            backup_context_count=args.backup_context_count,
            seed=args.seed,
            dataset_version=args.dataset_version,
            created_at=args.created_at,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "reviewed-support-validate":
        summary = validate_reviewed_support_bundle(args.source, **_reviewed_kwargs(args))
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "reviewed-support-to-runtime":
        adapted = reviewed_support_to_context_substitution_input(
            args.source,
            source_split=args.source_split,
            review_artifact=args.review_artifact,
            **_reviewed_kwargs(args),
        )
        _write_json(args.output, adapted["input"])
        _write_json(args.receipt, adapted["receipt"])
        print(
            json.dumps(
                {
                    "input": str(args.output.resolve()),
                    "input_sha256": adapted["input"]["integrity"]["input_sha256"],
                    "receipt": str(args.receipt.resolve()),
                    "receipt_sha256": adapted["receipt"]["receipt_sha256"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "context-run":
        if not args.allow_api:
            raise SystemExit("context-run requires explicit --allow-api")
        input_payload = _load(args.input)
        settings = _route_settings(_load(args.routes))
        model = FailoverStructuredModel(
            [item.build() for item in settings],
            response_ledger=ProviderResponseLedger(args.ledger_root),
        )
        if args.evaluation_mode == "FROZEN_TEST_SET":
            if args.calibration_artifact is None:
                raise SystemExit("FROZEN_TEST_SET requires --calibration-artifact")
            threshold_policy = frozen_validation_policy(
                calibration_artifact=args.calibration_artifact,
                expected_physical_sha256=args.calibration_file_sha256,
            )
        else:
            if args.calibration_artifact is not None:
                raise SystemExit("development mode cannot claim a calibration artifact")
            threshold_policy = DEVELOPMENT_HEURISTIC_POLICY
        payload = run_d2l_context_substitution(
            input_payload,
            model,
            candidate_target_ids=args.candidate_target_id,
            include_target_roles=tuple(
                args.target_role or ("canonical", "alternative", "pending")
            ),
            threshold_policy=threshold_policy,
            evaluation_mode=args.evaluation_mode,
        )
        _write_json(args.output, payload)
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "run_sha256": payload["integrity"]["run_sha256"],
                    "usage": payload["usage"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "measurements-project":
        payload = validate_d2l_context_substitution_run(_load(args.run))
        measurements = context_substitution_to_measurements(payload)
        _write_json(args.output, measurements)
        print(json.dumps({"output": str(args.output.resolve())}, indent=2))
        return 0

    if args.command == "gold-evaluate":
        raw = _load(args.cases)
        cases = raw["cases"] if isinstance(raw, Mapping) and "cases" in raw else raw
        report = evaluate_gold_cases(cases)
        _write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
