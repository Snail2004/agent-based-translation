from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from pipeline.eval.live_pilot_capability_probe_v1 import (
    build_clean_evaluation_probe_implementation_binding_v1,
    build_evaluation_json_object_capability_probe_plan_v1,
)
from pipeline.eval.live_pilot_capability_run_v1 import (
    run_evaluation_capability_probes_v1,
)
from pipeline.eval.llm_profiles_v1 import EVALUATION_LLM_ROLE_IDS
from pipeline.llm_backend import (
    MappingCredentialProvider,
    UrllibTransportSender,
    canonical_json,
    credential_commitment,
    validate_api_source,
)


DEFAULT_CKEY_OPENAI_BASE_URL = "https://api.xah.io/v1"
DEFAULT_CKEY_GOOGLE_BASE_URL = "https://api.xah.io/v1beta"
_TRANSPORT_PROTOCOLS = frozenset(
    {"openai_chat_completions", "google_genai_generate_content"}
)


def load_selected_credential_row_v1(
    path: Path, *, physical_row: int, expected_row_count: int
) -> str:
    if expected_row_count < 1:
        raise ValueError("expected_row_count must be positive")
    if physical_row < 1 or physical_row > expected_row_count:
        raise ValueError("physical_row is outside the declared credential file")
    values = [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if len(values) != expected_row_count:
        raise ValueError(
            f"credential file must contain exactly {expected_row_count} non-empty rows"
        )
    credential = values[physical_row - 1]
    if any(character.isspace() for character in credential) or len(credential) < 20:
        raise ValueError("selected credential row is malformed")
    return credential


def build_ckey_openai_compatible_source_v1(
    *,
    source_id: str,
    source_revision: str,
    credential_ref: str,
    physical_quota_bucket_id: str,
    credential: str,
    base_url: str = DEFAULT_CKEY_OPENAI_BASE_URL,
) -> dict:
    return validate_api_source(
        {
            "schema_version": "api_source_v1",
            "source_id": source_id,
            "source_revision": source_revision,
            "source_class": "remote_api",
            "adapter_id": "openai_compatible_chat_v1",
            "protocol": "openai_chat_completions",
            "route_id": "chat_completions",
            "endpoint_class": "remote",
            "base_url": base_url,
            "credential_ref": credential_ref,
            "credential_commitment": credential_commitment(credential),
            "physical_quota_bucket_id": physical_quota_bucket_id,
            "enabled": True,
        }
    )


def build_ckey_google_compatible_source_v1(
    *,
    source_id: str,
    source_revision: str,
    credential_ref: str,
    physical_quota_bucket_id: str,
    credential: str,
    base_url: str = DEFAULT_CKEY_GOOGLE_BASE_URL,
) -> dict:
    return validate_api_source(
        {
            "schema_version": "api_source_v1",
            "source_id": source_id,
            "source_revision": source_revision,
            "source_class": "remote_api",
            "adapter_id": "google_genai_rest_v1",
            "protocol": "google_genai_generate_content",
            "route_id": "models_generate_content",
            "endpoint_class": "remote",
            "base_url": base_url,
            "credential_ref": credential_ref,
            "credential_commitment": credential_commitment(credential),
            "physical_quota_bucket_id": physical_quota_bucket_id,
            "enabled": True,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.execute_live:
        parser.error("--execute-live is required; this runner has no implicit live mode")

    credential = load_selected_credential_row_v1(
        args.credential_file,
        physical_row=args.physical_row,
        expected_row_count=args.expected_row_count,
    )
    base_url = args.base_url or (
        DEFAULT_CKEY_GOOGLE_BASE_URL
        if args.transport_protocol == "google_genai_generate_content"
        else DEFAULT_CKEY_OPENAI_BASE_URL
    )
    source_builder = (
        build_ckey_google_compatible_source_v1
        if args.transport_protocol == "google_genai_generate_content"
        else build_ckey_openai_compatible_source_v1
    )
    source = source_builder(
        source_id=args.source_id,
        source_revision=args.source_revision,
        credential_ref=args.credential_ref,
        physical_quota_bucket_id=args.physical_quota_bucket_id,
        credential=credential,
        base_url=base_url,
    )
    accepted_models = sorted(
        set([args.model_id, *args.accepted_observed_model])
    )
    models = {role_id: args.model_id for role_id in EVALUATION_LLM_ROLE_IDS}
    accepted = {
        role_id: accepted_models for role_id in EVALUATION_LLM_ROLE_IDS
    }
    binding = build_clean_evaluation_probe_implementation_binding_v1()
    summary = run_evaluation_capability_probes_v1(
        source=source,
        requested_models_by_role=models,
        accepted_observed_models_by_role=accepted,
        credential_provider=MappingCredentialProvider(
            {args.credential_ref: credential}
        ),
        output_root=args.output_root,
        probe_run_prefix=args.probe_run_prefix,
        issued_at_utc=args.issued_at_utc,
        implementation_binding=binding,
        sender=UrllibTransportSender(),
        plan_builder=build_evaluation_json_object_capability_probe_plan_v1,
    )
    public_result = {
        "status": summary["status"],
        "summary_path": str((args.output_root / "run_summary.json").resolve()),
        "attempted_roles": [
            {
                "role_id": row["role_id"],
                "requested_model_id": row["requested_model_id"],
                "status": row["status"],
            }
            for row in summary["attempted_roles"]
        ],
        "halt": summary["halt"],
    }
    print(canonical_json(public_result))
    return 0 if summary["status"] == "qualified" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify Evaluation JSON-object syntax plus local validators on a "
            "sealed CKEY third-party source."
        )
    )
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--physical-row", type=int, default=1)
    parser.add_argument("--expected-row-count", type=int, default=1)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--credential-ref", required=True)
    parser.add_argument("--physical-quota-bucket-id", required=True)
    parser.add_argument(
        "--transport-protocol",
        choices=sorted(_TRANSPORT_PROTOCOLS),
        default="openai_chat_completions",
    )
    parser.add_argument("--base-url")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--accepted-observed-model", action="append", default=[])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--probe-run-prefix", required=True)
    parser.add_argument("--issued-at-utc", required=True)
    parser.add_argument("--execute-live", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
