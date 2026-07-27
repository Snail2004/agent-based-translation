"""Version-dispatched Literary Shared LLM runtime-profile loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from pipeline.literary.shared_runtime_profile_v1 import (
    PROFILE_SCHEMA_VERSION as PROFILE_SCHEMA_VERSION_V1,
    LiterarySharedRuntimeProfileV1,
    load_literary_shared_runtime_profile_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    DEFAULT_PROFILE_V2_PATH,
    PROFILE_SCHEMA_VERSION_V2,
    LiterarySharedRuntimeProfileV2,
    load_literary_shared_runtime_profile_v2,
)


DEFAULT_RECOMMENDED_PROFILE_PATH = DEFAULT_PROFILE_V2_PATH
LiterarySharedRuntimeProfile = Union[
    LiterarySharedRuntimeProfileV1, LiterarySharedRuntimeProfileV2
]


def load_literary_shared_runtime_profile(
    path: Path = DEFAULT_RECOMMENDED_PROFILE_PATH,
) -> LiterarySharedRuntimeProfile:
    source = Path(path).resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"cannot load Literary runtime profile: {source}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Literary runtime profile must be an object")
    schema_version = raw.get("schema_version")
    if schema_version == PROFILE_SCHEMA_VERSION_V1:
        return load_literary_shared_runtime_profile_v1(source)
    if schema_version == PROFILE_SCHEMA_VERSION_V2:
        return load_literary_shared_runtime_profile_v2(source)
    raise ValueError("foreign Literary runtime profile schema")


__all__ = [
    "DEFAULT_RECOMMENDED_PROFILE_PATH",
    "LiterarySharedRuntimeProfile",
    "load_literary_shared_runtime_profile",
]
