from __future__ import annotations

import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT / "epub_src"
OUTPUT = ROOT / "source.epub"
FIXED_TIMESTAMP = (2026, 7, 17, 0, 0, 0)


def _write_member(
    archive: zipfile.ZipFile,
    relative_path: str,
    *,
    compression: int,
) -> None:
    payload = (SOURCE_ROOT / relative_path).read_bytes()
    if relative_path == "mimetype":
        payload = payload.rstrip(b"\r\n")
    info = zipfile.ZipInfo(relative_path, FIXED_TIMESTAMP)
    info.compress_type = compression
    info.create_system = 0
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


def build(output: Path = OUTPUT) -> Path:
    members = sorted(
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file() and path.name != "mimetype"
    )
    with zipfile.ZipFile(output, "w") as archive:
        _write_member(archive, "mimetype", compression=zipfile.ZIP_STORED)
        for member in members:
            _write_member(archive, member, compression=zipfile.ZIP_DEFLATED)
    return output


if __name__ == "__main__":
    print(build())
