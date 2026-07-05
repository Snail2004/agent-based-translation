from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from pipeline.lib.events import EventEmitter, NullEventEmitter


DEFAULT_LMSTUDIO_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_GEMMA_MODEL = "google/gemma-4-12b"
DEFAULT_BGE_MODEL = "bge-m3"


def main() -> int:
    parser = argparse.ArgumentParser(description="One-button environment preflight health check.")
    parser.add_argument("--lmstudio-endpoint", default=DEFAULT_LMSTUDIO_ENDPOINT)
    parser.add_argument("--gemma-model", default=DEFAULT_GEMMA_MODEL)
    parser.add_argument("--bge-model", default=DEFAULT_BGE_MODEL)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--event-log", default="")
    parser.add_argument("--run-id", default="preflight_check")
    parser.add_argument("--attempt-id", type=int, default=1)
    args = parser.parse_args()

    report = run_checks(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 2


def run_checks(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    checks = [
        _check_lmstudio_model(
            endpoint=str(args.lmstudio_endpoint),
            expected_model=str(args.gemma_model),
            check_id="lmstudio_gemma",
        ),
        _check_lmstudio_model(
            endpoint=str(args.lmstudio_endpoint),
            expected_model=str(args.bge_model),
            check_id="lmstudio_bge",
        ),
        _check_key_presence(
            key_name="openai",
            env_names=["OPENAI_API_KEY"],
            filenames=["OPENAI-KEY-2.txt", "OPENAI-KEY-1.txt", "API-KEY.txt"],
            repo_root=repo_root,
        ),
        _check_key_presence(
            key_name="gemini",
            env_names=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            filenames=["GEMINI-KEY.txt"],
            repo_root=repo_root,
        ),
        _check_python_import(
            python_exe=str(args.python),
            module="comet",
            check_id="cometkiwi_import",
        ),
    ]
    status = "pass" if all(item["ok"] for item in checks) else "fail"
    report = {
        "schema": "one_button_preflight_check_v1",
        "status": status,
        "zero_api": True,
        "checks": checks,
    }
    emitter = _event_emitter(args)
    for item in checks:
        emitter.emit(
            "health_check",
            stage="report",
            script="preflight_check",
            agent="PreflightCheck",
            severity="info" if item["ok"] else "warning",
            payload=item,
        )
    emitter.close()
    return report


def _check_lmstudio_model(*, endpoint: str, expected_model: str, check_id: str) -> dict[str, Any]:
    url = endpoint.rstrip("/") + "/v1/models"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else []
        ids = [str(item.get("id") or "") for item in rows if isinstance(item, dict)]
        matched = [model_id for model_id in ids if _model_matches(model_id, expected_model)]
        return {
            "id": check_id,
            "ok": bool(matched),
            "endpoint": endpoint,
            "expected_model": expected_model,
            "matched_models": matched,
            "loaded_model_count": len(ids),
        }
    except Exception as exc:
        return {
            "id": check_id,
            "ok": False,
            "endpoint": endpoint,
            "expected_model": expected_model,
            "error": f"{type(exc).__name__}",
        }


def _model_matches(model_id: str, expected: str) -> bool:
    left = _normalize_model_id(model_id)
    right = _normalize_model_id(expected)
    return bool(left and right and (left in right or right in left))


def _normalize_model_id(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _check_key_presence(
    *,
    key_name: str,
    env_names: list[str],
    filenames: list[str],
    repo_root: Path,
) -> dict[str, Any]:
    for env_name in env_names:
        if os.getenv(env_name):
            return {
                "id": f"{key_name}_key",
                "ok": True,
                "source": f"env:{env_name}",
            }
    search_roots = [repo_root, repo_root / "THESIS_RUNTIME_TOOL", repo_root.parent]
    for root in search_roots:
        for filename in filenames:
            path = root / filename
            try:
                if path.exists() and path.read_text(encoding="utf-8").strip():
                    return {
                        "id": f"{key_name}_key",
                        "ok": True,
                        "source": f"file:{path.name}",
                    }
            except OSError:
                continue
    return {
        "id": f"{key_name}_key",
        "ok": False,
        "source": "missing",
        "filenames_checked": filenames,
    }


def _check_python_import(*, python_exe: str, module: str, check_id: str) -> dict[str, Any]:
    code = (
        "import importlib, json, platform, sys; "
        f"importlib.import_module({module!r}); "
        "print(json.dumps({'python': sys.version.split()[0], 'executable': sys.executable}))"
    )
    try:
        # Cold CometKiwi import pulls in torch and can exceed 60s on this
        # machine; 20s produced a false FAIL against the correct py3.11 env.
        result = subprocess.run(
            [python_exe, "-c", code],
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        detail: dict[str, Any] = {}
        if result.stdout.strip():
            try:
                detail = json.loads(result.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                detail = {"stdout": result.stdout.strip()[:200]}
        return {
            "id": check_id,
            "ok": result.returncode == 0,
            "module": module,
            "python": detail.get("python", ""),
            "executable": detail.get("executable", python_exe),
            "error": "" if result.returncode == 0 else (result.stderr.strip()[:300] or "import_failed"),
        }
    except Exception as exc:
        return {
            "id": check_id,
            "ok": False,
            "module": module,
            "python": "",
            "executable": python_exe,
            "error": f"{type(exc).__name__}",
        }


def _event_emitter(args: argparse.Namespace) -> EventEmitter | NullEventEmitter:
    if not getattr(args, "event_log", ""):
        return NullEventEmitter()
    return EventEmitter(
        Path(args.event_log),
        run_id=str(getattr(args, "run_id", "") or "preflight_check"),
        attempt_id=int(getattr(args, "attempt_id", 1) or 1),
    )


if __name__ == "__main__":
    raise SystemExit(main())
