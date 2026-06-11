"""Evaluate the current extractor on the AILAB multi-book corpus.

This script is intentionally stdlib-only. It verifies the already-downloaded raw
corpus, creates local app projects through the backend service layer, extracts
without modifying extractor code, validates each generated dataset, writes raw
JSON results, and renders a Markdown report from those raw results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HANDOFF_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = HANDOFF_ROOT / "app"
BACKEND_ROOT = APP_ROOT / "backend"
RAW_ROOT = HANDOFF_ROOT.parent / "AILAB_SOURCES_RAW"
REPORTS_ROOT = APP_ROOT / "reports"
GROUND_TRUTH_PATH = REPORTS_ROOT / "ground_truth_chapters.json"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from config import PIPELINE_VERSION, PROJECTS_ROOT  # noqa: E402
from services.dataset_io import read_dataset, read_json, write_json_atomic  # noqa: E402
from services.extraction import extract_project  # noqa: E402
from services.validator import run_validator  # noqa: E402
from services.workspace import (  # noqa: E402
    ProjectError,
    create_project_shell,
    ensure_working_files,
    get_project_path,
    has_project,
    save_source_file,
)


RESULTS_PATH = REPORTS_ROOT / f"eval_results_v{PIPELINE_VERSION}.json"
REPORT_PATH = REPORTS_ROOT / f"EXTRACTOR_EVAL_v{PIPELINE_VERSION}.md"
BASELINE_VERSION = ".".join(
    PIPELINE_VERSION.split(".")[:-1] + [str(max(0, int(PIPELINE_VERSION.split(".")[-1]) - 1))]
) if PIPELINE_VERSION.split(".")[-1].isdigit() else "0.3.2"
BASELINE_RESULTS_PATH = REPORTS_ROOT / f"eval_results_v{BASELINE_VERSION}.json"


EVAL_MARKER_NAME = "eval_corpus_project.json"
HIGH_CONFIDENCE_MATCH_RATE = 0.8


@dataclass(frozen=True)
class CorpusItem:
    doc_id: str
    source_folder: str
    source_file: str
    title: str
    author: str
    source_name: str
    source_page: str
    source_url: str
    license: str
    license_url: str
    genre: str
    role: str


CORPUS: list[CorpusItem] = [
    CorpusItem(
        doc_id="jekyll_txt",
        source_folder="jekyll_hyde",
        source_file="source.txt",
        title="The Strange Case of Dr. Jekyll and Mr. Hyde",
        author="Robert Louis Stevenson",
        source_name="Project Gutenberg",
        source_page="https://www.gutenberg.org/ebooks/42",
        source_url="https://www.gutenberg.org/cache/epub/42/pg42.txt",
        license="Project Gutenberg License; public domain in the United States unless otherwise noted",
        license_url="https://www.gutenberg.org/policy/license.html",
        genre="gothic fiction",
        role="tune_anchor",
    ),
    CorpusItem(
        doc_id="jekyll_epub",
        source_folder="jekyll_hyde",
        source_file="source.epub",
        title="The Strange Case of Dr. Jekyll and Mr. Hyde",
        author="Robert Louis Stevenson",
        source_name="Project Gutenberg",
        source_page="https://www.gutenberg.org/ebooks/42",
        source_url="https://www.gutenberg.org/ebooks/42.epub.images",
        license="Project Gutenberg License; public domain in the United States unless otherwise noted",
        license_url="https://www.gutenberg.org/policy/license.html",
        genre="gothic fiction",
        role="tune_anchor",
    ),
    CorpusItem(
        doc_id="gatsby_txt",
        source_folder="gatsby",
        source_file="source.txt",
        title="The Great Gatsby",
        author="F. Scott Fitzgerald",
        source_name="Project Gutenberg",
        source_page="https://www.gutenberg.org/ebooks/64317",
        source_url="https://www.gutenberg.org/cache/epub/64317/pg64317.txt",
        license="Project Gutenberg License; public domain in the United States unless otherwise noted",
        license_url="https://www.gutenberg.org/policy/license.html",
        genre="novel",
        role="tune",
    ),
    CorpusItem(
        doc_id="gatsby_epub",
        source_folder="gatsby",
        source_file="source.epub",
        title="The Great Gatsby",
        author="F. Scott Fitzgerald",
        source_name="Project Gutenberg",
        source_page="https://www.gutenberg.org/ebooks/64317",
        source_url="https://www.gutenberg.org/ebooks/64317.epub.images",
        license="Project Gutenberg License; public domain in the United States unless otherwise noted",
        license_url="https://www.gutenberg.org/policy/license.html",
        genre="novel",
        role="tune",
    ),
    CorpusItem(
        doc_id="wizard_oz_epub",
        source_folder="wizard_oz",
        source_file="source.epub",
        title="The Wonderful Wizard of Oz",
        author="L. Frank Baum",
        source_name="Standard Ebooks",
        source_page="https://standardebooks.org/ebooks/l-frank-baum/the-wonderful-wizard-of-oz",
        source_url="https://standardebooks.org/ebooks/l-frank-baum/the-wonderful-wizard-of-oz/downloads/l-frank-baum_the-wonderful-wizard-of-oz.epub?source=download",
        license="CC0 1.0 Universal (Standard Ebooks edition)",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        genre="fantasy adventure",
        role="held_out",
    ),
    CorpusItem(
        doc_id="alice_epub",
        source_folder="alice_wonderland",
        source_file="source.epub",
        title="Alice’s Adventures in Wonderland",
        author="Lewis Carroll",
        source_name="Global Grey",
        source_page="https://www.globalgreyebooks.com/alices-adventures-in-wonderland-ebook.html",
        source_url="https://www.globalgreyebooks.com/ebooks/lewis-carroll_alices-adventures-in-wonderland.epub",
        license="Global Grey public-domain ebook edition; verify source page before freeze",
        license_url="https://www.globalgreyebooks.com/alices-adventures-in-wonderland-ebook.html",
        genre="children's fantasy",
        role="held_out",
    ),
    CorpusItem(
        doc_id="time_machine_epub",
        source_folder="time_machine",
        source_file="source.epub",
        title="The Time Machine",
        author="H. G. Wells",
        source_name="Standard Ebooks",
        source_page="https://standardebooks.org/ebooks/h-g-wells/the-time-machine",
        source_url="https://standardebooks.org/ebooks/h-g-wells/the-time-machine/downloads/h-g-wells_the-time-machine.epub?source=download",
        license="CC0 1.0 Universal (Standard Ebooks edition)",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        genre="science fiction",
        role="held_out",
    ),
    CorpusItem(
        doc_id="frankenstein_epub",
        source_folder="frankenstein",
        source_file="source.epub",
        title="Frankenstein",
        author="Mary Shelley",
        source_name="Standard Ebooks",
        source_page="https://standardebooks.org/ebooks/mary-shelley/frankenstein",
        source_url="https://standardebooks.org/ebooks/mary-shelley/frankenstein/downloads/mary-shelley_frankenstein.epub?source=download",
        license="CC0 1.0 Universal (Standard Ebooks edition)",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        genre="gothic science fiction",
        role="held_out_hard",
    ),
    CorpusItem(
        doc_id="call_wild_epub",
        source_folder="call_wild",
        source_file="source.epub",
        title="The Call of the Wild",
        author="Jack London",
        source_name="Standard Ebooks",
        source_page="https://standardebooks.org/ebooks/jack-london/the-call-of-the-wild",
        source_url="https://standardebooks.org/ebooks/jack-london/the-call-of-the-wild/downloads/jack-london_the-call-of-the-wild.epub?source=download",
        license="CC0 1.0 Universal (Standard Ebooks edition)",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        genre="adventure",
        role="held_out",
    ),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(HANDOFF_ROOT).as_posix()
    except ValueError:
        return path.name


def normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return text


def safe_snippet(text: str, max_len: int = 180) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) > max_len:
        return value[: max_len - 1] + "…"
    return value


def load_ground_truth(allow_draft: bool = False) -> dict[str, Any]:
    if not GROUND_TRUTH_PATH.exists():
        raise RuntimeError(f"Missing ground truth file: {GROUND_TRUTH_PATH}")
    data = json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    missing = [item.doc_id for item in CORPUS if item.doc_id not in data.get("books", {})]
    empty = [
        doc_id
        for doc_id, item in data.get("books", {}).items()
        if not item.get("expected_chapters")
    ]
    if missing:
        raise RuntimeError(f"MISSING_GROUND_TRUTH: {', '.join(missing)}")
    if empty:
        raise RuntimeError(f"EMPTY_GROUND_TRUTH: {', '.join(empty)}")
    if data.get("status") != "approved" and not allow_draft:
        raise RuntimeError(
            "Ground truth is not approved. Review ambiguities, set status to 'approved', "
            "or re-run with --allow-draft-ground-truth."
        )
    return data


def checksum_entry(checksum_data: dict[str, Any], file_name: str) -> dict[str, Any] | None:
    for item in checksum_data.get("files", []):
        if item.get("filename") == file_name:
            return item
    return None


def verify_epub(path: Path) -> dict[str, Any]:
    result = {"is_zip": False, "has_mimetype": False, "has_container": False, "opf_title": None}
    if not zipfile.is_zipfile(path):
        return result
    result["is_zip"] = True
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        result["has_mimetype"] = "mimetype" in names and zf.read("mimetype") == b"application/epub+zip"
        result["has_container"] = "META-INF/container.xml" in names
        if result["has_container"]:
            try:
                from xml.etree import ElementTree as ET

                container = ET.fromstring(zf.read("META-INF/container.xml"))
                rootfile = container.find(".//{*}rootfile")
                if rootfile is not None and rootfile.attrib.get("full-path") in names:
                    opf = ET.fromstring(zf.read(rootfile.attrib["full-path"]))
                    title = opf.find(".//{*}metadata/{*}title")
                    if title is not None and title.text:
                        result["opf_title"] = title.text.strip()
            except Exception as exc:  # pragma: no cover - diagnostic only
                result["opf_title_error"] = str(exc)
    return result


def verify_raw_corpus() -> dict[str, Any]:
    manifest_path = RAW_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    items: dict[str, Any] = {}
    ok = True
    for item in CORPUS:
        folder = RAW_ROOT / item.source_folder
        source_path = folder / item.source_file
        checksum_path = folder / "checksums.json"
        errors: list[str] = []
        checksum_data: dict[str, Any] = {}
        if not source_path.exists():
            errors.append(f"missing raw file: {source_path}")
        if not checksum_path.exists():
            errors.append(f"missing checksums.json: {checksum_path}")
        else:
            checksum_data = json.loads(checksum_path.read_text(encoding="utf-8"))
        entry = checksum_entry(checksum_data, item.source_file) if checksum_data else None
        actual_bytes = source_path.stat().st_size if source_path.exists() else None
        actual_sha = sha256_file(source_path) if source_path.exists() else None
        if entry is None:
            errors.append(f"missing checksum entry for {item.source_file}")
        else:
            if actual_bytes != entry.get("bytes"):
                errors.append(f"bytes mismatch: actual={actual_bytes} expected={entry.get('bytes')}")
            if actual_sha != entry.get("sha256"):
                errors.append("sha256 mismatch")
        epub = verify_epub(source_path) if source_path.suffix.lower() == ".epub" and source_path.exists() else None
        if epub and not (epub.get("is_zip") and epub.get("has_mimetype") and epub.get("has_container")):
            errors.append("invalid epub container")
        if errors:
            ok = False
        items[item.doc_id] = {
            "source_folder": item.source_folder,
            "source_file": item.source_file,
            "path": str(source_path),
            "bytes": actual_bytes,
            "sha256": actual_sha,
            "checksum_entry": entry,
            "epub": epub,
            "errors": errors,
        }
    return {
        "ok": ok,
        "raw_root": str(RAW_ROOT),
        "manifest_present": manifest_path.exists(),
        "manifest_books": len(manifest.get("books", [])) if isinstance(manifest, dict) else 0,
        "items": items,
    }


def metadata_for(item: CorpusItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "author": item.author,
        "domain": "literature",
        "genre": item.genre,
        "source_format": Path(item.source_file).suffix.lower().lstrip("."),
        "license": item.license,
        "license_url": item.license_url,
        "source_url": item.source_url,
        "contamination_risk": "high",
    }


def eval_marker(project_path: Path) -> Path:
    return project_path / "working" / EVAL_MARKER_NAME


def prepare_project(item: CorpusItem, overwrite_eval_projects: bool = False) -> tuple[Path | None, str | None]:
    project_path = get_project_path(item.doc_id)
    raw_path = RAW_ROOT / item.source_folder / item.source_file
    marker_path = eval_marker(project_path)
    if project_path.exists():
        if marker_path.exists() and overwrite_eval_projects:
            shutil.rmtree(project_path)
        elif marker_path.exists():
            return project_path, None
        else:
            return None, f"PROJECT_EXISTS_NOT_EVAL: {item.doc_id}"
    create_project_shell(item.doc_id, metadata_for(item))
    project_path = get_project_path(item.doc_id)
    ensure_working_files(project_path)
    save_source_file(project_path, raw_path.name, raw_path.read_bytes(), overwrite=True)
    write_json_atomic(marker_path, {
        "generated_by": "eval_extractor_corpus.py",
        "doc_id": item.doc_id,
        "source_folder": item.source_folder,
        "source_file": item.source_file,
        "created_at": now_iso(),
    })
    return project_path, None


def count_replacement_chars(document: dict[str, Any]) -> int:
    total = 0
    for chapter in document.get("chapters", []):
        total += str(chapter.get("title") or "").count("\ufffd")
        for block in chapter.get("blocks", []):
            total += str(block.get("source_text") or "").count("\ufffd")
            total += str(block.get("clean_text") or "").count("\ufffd")
    return total


BOILERPLATE_RE = re.compile(
    r"(\bproject gutenberg\b|\bfull project gutenberg\b|\bgutenberg license\b|"
    r"\b(?:start|end) of (?:the|this) project gutenberg\b|"
    r"\bwww\.gutenberg\.org\b|"
    r"^(?:titlepage|uncopyright|colophon|imprint)\b)",
    re.IGNORECASE,
)
FRONT_MATTER_TITLE_RE = re.compile(
    r"^(titlepage|imprint|contents|table of contents|dedication|epigraph|preface|"
    r"introduction|colophon|uncopyright|endnotes|the full project gutenberg.*license)$",
    re.IGNORECASE,
)
AUTHORIAL_FRONT_MATTER_TITLE_RE = re.compile(r"^(introduction|preface)$", re.IGNORECASE)


def block_count_map(document: dict[str, Any]) -> dict[str, int]:
    return {
        chapter.get("chapter_id", ""): len(chapter.get("blocks", []))
        for chapter in document.get("chapters", [])
    }


def extract_chapter_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for chapter in document.get("chapters", []):
        rows.append({
            "chapter_id": chapter.get("chapter_id"),
            "title": chapter.get("title") or "",
            "block_count": len(chapter.get("blocks", [])),
        })
    return rows


def diff_chapters(expected: list[dict[str, Any]], extracted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    max_len = max(len(expected), len(extracted))
    for idx in range(max_len):
        exp = expected[idx] if idx < len(expected) else None
        got = extracted[idx] if idx < len(extracted) else None
        exp_title = exp.get("title") if exp else None
        got_title = got.get("title") if got else None
        if exp is None:
            status = "extra"
        elif got is None:
            status = "missing"
        elif normalize_title(exp_title) == normalize_title(got_title):
            status = "match"
        else:
            status = "mismatch"
        rows.append({
            "index": idx + 1,
            "expected_title": exp_title,
            "extracted_title": got_title,
            "extracted_chapter_id": got.get("chapter_id") if got else None,
            "block_count": got.get("block_count") if got else None,
            "status": status,
        })
    return rows


def collect_defects(
    doc_id: str,
    document: dict[str, Any] | None,
    validation: dict[str, Any],
    extraction_report: dict[str, Any],
    chapter_diff: list[dict[str, Any]],
    expected_titles: list[str],
    nested_sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    defects: list[dict[str, Any]] = []

    if not validation.get("ok"):
        for error in validation.get("errors", []):
            defects.append({
                "severity": "critical",
                "type": "validation_error",
                "location": error.get("location") or error.get("file") or "-",
                "evidence": error.get("message") or "",
                "flagged_by_report": True,
            })

    for row in chapter_diff:
        if row["status"] != "match":
            defects.append({
                "severity": "major",
                "type": f"chapter_{row['status']}",
                "location": row.get("extracted_chapter_id") or f"index {row['index']}",
                "evidence": f"expected={row.get('expected_title')!r}; extracted={row.get('extracted_title')!r}",
                "flagged_by_report": bool(extraction_report.get("toc", {}).get("low_confidence")),
            })

    if document is None:
        return defects

    expected_keys = {normalize_title(title) for title in expected_titles}
    extracted_title_keys = {
        normalize_title(chapter.get("title") or "")
        for chapter in document.get("chapters", [])
    }
    for chapter in document.get("chapters", []):
        title = chapter.get("title") or ""
        chapter_id = chapter.get("chapter_id") or "-"
        if AUTHORIAL_FRONT_MATTER_TITLE_RE.match(title):
            defects.append({
                "severity": "major",
                "type": "front_matter_authorial_chapter",
                "location": chapter_id,
                "evidence": f"{title} (authorial front matter)",
                "flagged_by_report": bool(extraction_report.get("toc", {}).get("low_confidence")),
            })
        elif FRONT_MATTER_TITLE_RE.match(title):
            defects.append({
                "severity": "major",
                "type": "front_matter_chapter",
                "location": chapter_id,
                "evidence": title,
                "flagged_by_report": bool(extraction_report.get("toc", {}).get("low_confidence")),
            })
        if re.search(r"\.(x?html?|htm)$|_?h-\d+", title, re.IGNORECASE):
            defects.append({
                "severity": "major",
                "type": "filename_chapter_title",
                "location": chapter_id,
                "evidence": title,
                "flagged_by_report": bool(extraction_report.get("toc", {}).get("low_confidence")),
            })
        if len(chapter.get("blocks", [])) <= 1:
            defects.append({
                "severity": "minor",
                "type": "very_short_chapter",
                "location": chapter_id,
                "evidence": f"title={title!r}; block_count={len(chapter.get('blocks', []))}",
                "flagged_by_report": False,
            })
        for block in chapter.get("blocks", []):
            block_id = block.get("block_id") or chapter_id
            text = block.get("clean_text") or block.get("source_text") or ""
            if BOILERPLATE_RE.search(text):
                defects.append({
                    "severity": "major",
                    "type": "boilerplate_block",
                    "location": block_id,
                    "evidence": safe_snippet(text),
                    "flagged_by_report": bool(extraction_report.get("skipped")),
                })
            if block.get("block_type") != "heading" and normalize_title(text) in expected_keys:
                defects.append({
                    "severity": "major",
                    "type": "heading_as_body_block",
                    "location": block_id,
                    "evidence": safe_snippet(text),
                    "flagged_by_report": False,
                })
            quote_count = text.count('"') + text.count("“") + text.count("”")
            if block.get("block_type") == "dialogue" and quote_count >= 2 and len(text.split()) > 80:
                defects.append({
                    "severity": "minor",
                    "type": "possible_dialogue_overclassification",
                    "location": block_id,
                    "evidence": safe_snippet(text),
                    "flagged_by_report": False,
                })

    replacement_count = count_replacement_chars(document)
    if replacement_count:
        defects.append({
            "severity": "major",
            "type": "mojibake_replacement_char",
            "location": "document",
            "evidence": f"U+FFFD count={replacement_count}",
            "flagged_by_report": False,
        })

    for nested in nested_sections:
        title = nested.get("title") or ""
        parent_index = nested.get("parent_index")
        split_as_top_level = normalize_title(title) in extracted_title_keys
        defects.append({
            "severity": "major" if split_as_top_level else "info",
            "type": "nested_toc_anchor_split_as_top_level" if split_as_top_level else "nested_toc_anchor_deferred",
            "location": f"parent_index={parent_index}",
            "evidence": (
                f"{title!r} was extracted as a top-level chapter; Level 1 ground truth keeps it nested."
                if split_as_top_level else
                f"{title!r} is a separate TOC anchor kept nested in ground truth; Level 1 does not split it (deferred Level 2)."
            ),
            "flagged_by_report": True,
        })

    if not defects:
        defects.append({
            "severity": "info",
            "type": "none",
            "location": doc_id,
            "evidence": "No automated defects detected.",
            "flagged_by_report": True,
        })
    return defects


def decide_verdict(validation: dict[str, Any], extraction_report: dict[str, Any], defects: list[dict[str, Any]]) -> str:
    if not validation.get("ok"):
        return "STRUCTURAL-FAIL"
    serious = [d for d in defects if d.get("severity") in {"critical", "major"}]
    if not serious:
        return "PASS"
    toc = extraction_report.get("toc", {})
    flagged = bool(toc.get("low_confidence")) or toc.get("match_rate", 0.0) < HIGH_CONFIDENCE_MATCH_RATE
    return "FLAGGED-OK" if flagged else "FAIL"


def audit_nested_sections(nested_sections: list[dict[str, Any]], extracted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    extracted_keys = {normalize_title(row.get("title") or "") for row in extracted}
    rows: list[dict[str, Any]] = []
    for nested in nested_sections:
        title = nested.get("title") or ""
        split_as_top_level = normalize_title(title) in extracted_keys
        rows.append({
            "title": title,
            "parent_index": nested.get("parent_index"),
            "split_as_top_level": split_as_top_level,
            "level_1_expected": "deferred_nested_anchor",
            "note": nested.get("note") or "",
        })
    return rows


def evaluate_project(item: CorpusItem, ground_truth: dict[str, Any], overwrite_eval_projects: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "doc_id": item.doc_id,
        "source_folder": item.source_folder,
        "source_file": item.source_file,
        "title": item.title,
        "author": item.author,
        "source_name": item.source_name,
        "source_page": item.source_page,
        "source_url": item.source_url,
        "license": item.license,
        "license_url": item.license_url,
        "role": item.role,
        "started_at": now_iso(),
    }
    project_path, prepare_error = prepare_project(item, overwrite_eval_projects=overwrite_eval_projects)
    if prepare_error or project_path is None:
        result.update({
            "status": "skipped",
            "verdict": "STRUCTURAL-FAIL",
            "prepare_error": prepare_error,
            "finished_at": now_iso(),
        })
        return result

    try:
        job = extract_project(project_path, item.doc_id, overwrite=True, force=True, user="eval-script")
        result["extract_job"] = job
    except Exception as exc:
        result.update({
            "status": "extract_failed",
            "verdict": "STRUCTURAL-FAIL",
            "extract_error": str(exc),
            "finished_at": now_iso(),
        })
        return result

    validation = run_validator(project_path / "canonical")
    document: dict[str, Any] | None = None
    extraction_report: dict[str, Any] = {}
    dataset_load_ok = False
    dataset_error = None
    try:
        dataset = read_dataset(project_path)
        document = dataset["document"]
        dataset_load_ok = True
    except Exception as exc:
        dataset_error = str(exc)
        try:
            document = read_json(project_path / "canonical" / "document.json")
        except Exception:
            document = None
    try:
        extraction_report = read_json(project_path / "working" / "extraction_report.json")
    except Exception:
        extraction_report = {}

    book_ground_truth = ground_truth["books"][item.doc_id]
    expected = book_ground_truth["expected_chapters"]
    nested_sections = book_ground_truth.get("nested_sections", [])
    extracted = extract_chapter_rows(document) if document else []
    chapter_diff = diff_chapters(expected, extracted)
    expected_titles = [entry["title"] for entry in expected]
    defects = collect_defects(item.doc_id, document, validation, extraction_report, chapter_diff, expected_titles, nested_sections)
    nested_section_audit = audit_nested_sections(nested_sections, extracted)
    verdict = decide_verdict(validation, extraction_report, defects)
    result.update({
        "status": "done",
        "project_path": str(project_path),
        "dataset_load_ok": dataset_load_ok,
        "dataset_error": dataset_error,
        "validation": validation,
        "extraction_report": extraction_report,
        "expected_count": len(expected),
        "extracted_count": len(extracted),
        "extracted_chapters": extracted,
        "nested_sections": nested_sections,
        "nested_section_audit": nested_section_audit,
        "chapter_diff": chapter_diff,
        "defects": defects,
        "verdict": verdict,
        "pass_fake_audit": pass_fake_audit(verdict, extraction_report, defects),
        "finished_at": now_iso(),
    })
    return result


def pass_fake_audit(verdict: str, extraction_report: dict[str, Any], defects: list[dict[str, Any]]) -> dict[str, Any]:
    toc = extraction_report.get("toc", {})
    serious = [d for d in defects if d.get("severity") in {"critical", "major"}]
    high_confidence = (not toc.get("low_confidence")) and toc.get("match_rate", 0.0) >= HIGH_CONFIDENCE_MATCH_RATE
    silent = bool(serious and high_confidence)
    return {
        "silent_fail": silent,
        "conclusion": "YES" if silent else "NO",
        "evidence": (
            "Serious defects present while low_confidence=False and match_rate is high."
            if silent else
            "No serious silent high-confidence defect detected by automated audit."
        ),
        "verdict": verdict,
    }


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |")
    return "\n".join(out)


def load_baseline_results(current_version: str | None) -> dict[str, Any] | None:
    if current_version == BASELINE_VERSION or not BASELINE_RESULTS_PATH.exists():
        return None
    try:
        return json.loads(BASELINE_RESULTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def render_before_after(results: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    if not baseline:
        return ""
    baseline_version = baseline.get("pipeline_version") or BASELINE_VERSION
    current_version = results.get("pipeline_version")
    baseline_by_doc = {item["doc_id"]: item for item in baseline.get("items", []) if item.get("doc_id")}
    rows: list[list[Any]] = []
    for item in results.get("items", []):
        old = baseline_by_doc.get(item.get("doc_id"), {})
        old_toc = old.get("extraction_report", {}).get("toc", {})
        new_toc = item.get("extraction_report", {}).get("toc", {})
        rows.append([
            item.get("doc_id"),
            old.get("verdict", "-"),
            item.get("verdict", "-"),
            old.get("extracted_count", "-"),
            item.get("extracted_count", "-"),
            old_toc.get("low_confidence", "-"),
            new_toc.get("low_confidence", "-"),
            old_toc.get("match_rate", "-"),
            new_toc.get("match_rate", "-"),
        ])
    return markdown_table(
        [
            "doc_id",
            f"v{baseline_version} verdict",
            f"v{current_version} verdict",
            f"extracted {baseline_version}",
            f"extracted {current_version}",
            f"low_conf {baseline_version}",
            f"low_conf {current_version}",
            f"match_rate {baseline_version}",
            f"match_rate {current_version}",
        ],
        rows,
    )


def defect_type_counts(results: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results.get("items", []):
        for defect in item.get("defects", []):
            defect_type = defect.get("type", "unknown")
            if defect_type != "none":
                counts[defect_type] = counts.get(defect_type, 0) + 1
    return counts


def render_defect_before_after(results: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    if not baseline:
        return ""
    baseline_version = baseline.get("pipeline_version") or BASELINE_VERSION
    current_version = results.get("pipeline_version")
    old_counts = defect_type_counts(baseline)
    new_counts = defect_type_counts(results)
    defect_types = sorted(set(old_counts) | set(new_counts))
    if not defect_types:
        return "No automated defects in either run."
    return markdown_table(
        ["defect_type", f"v{baseline_version}", f"v{current_version}", "delta"],
        [
            [
                defect_type,
                old_counts.get(defect_type, 0),
                new_counts.get(defect_type, 0),
                new_counts.get(defect_type, 0) - old_counts.get(defect_type, 0),
            ]
            for defect_type in defect_types
        ],
    )


def render_report(results: dict[str, Any]) -> str:
    lines: list[str] = []
    baseline = load_baseline_results(results.get("pipeline_version"))
    version = results.get("pipeline_version")
    lines.append(f"# EXTRACTOR_EVAL v{version}")
    lines.append("")
    lines.append(f"Generated at: `{results['generated_at']}`")
    lines.append(f"Pipeline version: `{version}`")
    lines.append("")
    lines.append("## Overview")
    matrix_rows = []
    for item in results["items"]:
        toc = item.get("extraction_report", {}).get("toc", {})
        matrix_rows.append([
            item["doc_id"],
            item.get("source_name"),
            item.get("source_file"),
            toc.get("toc_source", "-"),
            toc.get("toc_items", "-"),
            item.get("extracted_count", "-"),
            item.get("expected_count", "-"),
            toc.get("match_rate", "-"),
            toc.get("low_confidence", "-"),
            toc.get("fallback_used", "-"),
            item.get("verdict", "-"),
        ])
    lines.append(markdown_table(
        ["book", "source", "format", "toc_source", "toc_items", "extracted", "expected", "match_rate", "low_confidence", "fallback", "verdict"],
        matrix_rows,
    ))
    lines.append("")
    before_after = render_before_after(results, baseline)
    if before_after:
        lines.append("## Phase 3 Before/After")
        lines.append("")
        lines.append(before_after)
        lines.append("")
        defect_before_after = render_defect_before_after(results, baseline)
        if defect_before_after:
            lines.append("## Defect Count Before/After")
            lines.append("")
            lines.append(defect_before_after)
            lines.append("")
    lines.append("## Raw Corpus Verification")
    raw = results.get("raw_verification", {})
    lines.append(f"- Raw root: `{raw.get('raw_root')}`")
    lines.append(f"- Manifest present: `{raw.get('manifest_present')}`; manifest books: `{raw.get('manifest_books')}`")
    lines.append(f"- Raw corpus OK: `{raw.get('ok')}`")
    lines.append("")
    lines.append("## Per-Book Results")
    for item in results["items"]:
        lines.append(f"### {item['doc_id']} — {item.get('title')}")
        lines.append("")
        lines.append(f"- Verdict: **{item.get('verdict', '-')}**")
        lines.append(f"- Dataset load OK: `{item.get('dataset_load_ok', False)}`")
        toc = item.get("extraction_report", {}).get("toc", {})
        lines.append(f"- TOC report: `source={toc.get('toc_source')}`, `items={toc.get('toc_items')}`, `matched={toc.get('chapters_matched')}`, `match_rate={toc.get('match_rate')}`, `low_confidence={toc.get('low_confidence')}`, `ambiguous={toc.get('ambiguous_titles')}`")
        skipped_files = item.get("extraction_report", {}).get("skipped", [])
        trims = item.get("extraction_report", {}).get("gutenberg", {}).get("epub_body_trimmed", [])
        heuristic_files = toc.get("heuristic_front_back_files", [])
        cleanup_bits = [f"skipped={len(skipped_files)}"]
        if trims:
            cleanup_bits.append(
                "epub_body_trimmed="
                + ", ".join(f"{trim.get('file')} ({trim.get('blocks_removed')} blocks)" for trim in trims)
            )
        if heuristic_files:
            cleanup_bits.append(
                "heuristic_front_back="
                + ", ".join(f"{item.get('file')}:{item.get('reason')}" for item in heuristic_files)
            )
        lines.append("- Cleanup report: " + "; ".join(cleanup_bits))
        nested_audit = item.get("nested_section_audit", [])
        if nested_audit:
            nested_summary = "; ".join(
                f"{row.get('title')} -> {'top-level extracted' if row.get('split_as_top_level') else 'deferred nested anchor'}"
                for row in nested_audit
            )
            lines.append(f"- Nested TOC anchors: {nested_summary}.")
        lines.append("")
        if nested_audit:
            lines.append("#### Nested TOC Anchor Audit")
            lines.append(markdown_table(
                ["title", "parent_index", "split_as_top_level", "Level 1 expected", "note"],
                [
                    [
                        row.get("title"),
                        row.get("parent_index"),
                        row.get("split_as_top_level"),
                        row.get("level_1_expected"),
                        safe_snippet(row.get("note", ""), 160),
                    ]
                    for row in nested_audit
                ],
            ))
            lines.append("")
        lines.append("#### Chapter Diff")
        diff_rows = []
        for row in item.get("chapter_diff", []):
            diff_rows.append([
                row["index"],
                row.get("expected_title") or "",
                row.get("extracted_title") or "",
                row.get("block_count") if row.get("block_count") is not None else "",
                row.get("status"),
            ])
        lines.append(markdown_table(["#", "Expected title", "Extracted title", "block_count", "Match?"], diff_rows))
        lines.append("")
        lines.append("#### Defect Log")
        defect_rows = [
            [
                d.get("severity"),
                d.get("type"),
                d.get("location"),
                safe_snippet(d.get("evidence", ""), 140),
                d.get("flagged_by_report"),
            ]
            for d in item.get("defects", [])
        ]
        lines.append(markdown_table(["severity", "type", "location", "evidence", "report flagged?"], defect_rows))
        lines.append("")
        audit = item.get("pass_fake_audit", {})
        lines.append("#### Pass-Fake Audit")
        lines.append(f"Extractor sai mà report vẫn high-confidence? **{audit.get('conclusion', '-')}**. {audit.get('evidence', '')}")
        lines.append("")
    lines.append("## Cross-Format Consistency")
    for pair_name, left, right in [
        ("Jekyll TXT vs EPUB", "jekyll_txt", "jekyll_epub"),
        ("Gatsby TXT vs EPUB", "gatsby_txt", "gatsby_epub"),
    ]:
        left_item = next((item for item in results["items"] if item["doc_id"] == left), {})
        right_item = next((item for item in results["items"] if item["doc_id"] == right), {})
        left_titles = [row.get("extracted_title") for row in left_item.get("chapter_diff", []) if row.get("extracted_title")]
        right_titles = [row.get("extracted_title") for row in right_item.get("chapter_diff", []) if row.get("extracted_title")]
        consistent = [normalize_title(t) for t in left_titles] == [normalize_title(t) for t in right_titles]
        lines.append(f"- **{pair_name}**: count `{len(left_titles)}` vs `{len(right_titles)}`, titles equivalent: `{consistent}`.")
    lines.append("")
    lines.append("## Aggregate Defects")
    agg: dict[str, int] = {}
    for item in results["items"]:
        for defect in item.get("defects", []):
            if defect.get("type") != "none":
                agg[defect.get("type", "unknown")] = agg.get(defect.get("type", "unknown"), 0) + 1
    if agg:
        lines.append(markdown_table(["defect_type", "count"], [[k, v] for k, v in sorted(agg.items())]))
    else:
        lines.append("No automated defects detected.")
    lines.append("")
    lines.append("## Phase 3 Before/After Notes")
    proposal_rows = []
    nested_items = [
        item
        for item in results["items"]
        if item.get("nested_section_audit")
    ]
    if agg:
        for defect_type, count in sorted(agg.items()):
            proposal_rows.append([
                defect_type,
                "review residual defect evidence before changing extractor again",
                f"affects {count} observation(s)",
                "risk of overfitting held-out corpus",
                "deferred unless it is a general bug",
            ])
    if nested_items:
        proposal_rows.append([
            "nested_toc_anchor_deferred",
            "TOC exposes anchors below a top-level chapter",
            ", ".join(item["doc_id"] for item in nested_items),
            "splitting nested anchors can change chapter count and review workflow",
            "deferred to Level 2; report surfaces anchors instead of silently ignoring them",
        ])
    if not agg and not nested_items:
        proposal_rows.append(["none", "no extractor change proposed", "-", "-", "-"])
    lines.append(markdown_table(["issue", "likely cause", "scope", "regression risk", "expected before/after"], proposal_rows))
    lines.append("")
    lines.append("## Provenance")
    prov_rows = []
    for item in results["items"]:
        raw_item = results.get("raw_verification", {}).get("items", {}).get(item["doc_id"], {})
        prov_rows.append([
            item["doc_id"],
            item.get("source_url"),
            raw_item.get("bytes"),
            raw_item.get("sha256"),
            item.get("license"),
            item.get("license_url"),
        ])
    lines.append(markdown_table(["doc_id", "url", "bytes", "sha256", "license", "license_url"], prov_rows))
    lines.append("")
    lines.append("## Re-Verify Commands")
    lines.append("```powershell")
    lines.append("cd app\\reports")
    lines.append("python eval_extractor_corpus.py --overwrite-eval-projects")
    lines.append("cd ..\\..")
    lines.append("python -m unittest discover app\\backend\\tests")
    lines.append("```")
    lines.append("")
    lines.append("## Conclusion")
    verdict_counts: dict[str, int] = {}
    for item in results["items"]:
        verdict_counts[item.get("verdict", "unknown")] = verdict_counts.get(item.get("verdict", "unknown"), 0) + 1
    lines.append(f"Verdict counts: `{verdict_counts}`.")
    lines.append("Extractor readiness should be decided from the PASS/FLAGGED-OK/FAIL mix above and the residual defect logs.")
    lines.append("")
    return "\n".join(lines)


def run_eval(args: argparse.Namespace) -> dict[str, Any]:
    ground_truth = load_ground_truth(allow_draft=args.allow_draft_ground_truth)
    raw_verification = verify_raw_corpus()
    if not raw_verification["ok"]:
        if args.verify_raw_only:
            return {
                "generated_at": now_iso(),
                "pipeline_version": PIPELINE_VERSION,
                "raw_verification": raw_verification,
                "items": [],
                "ground_truth_status": ground_truth.get("status"),
            }
        raise RuntimeError("Raw corpus verification failed. Inspect raw_verification in output.")
    if args.verify_raw_only:
        return {
            "generated_at": now_iso(),
            "pipeline_version": PIPELINE_VERSION,
            "raw_verification": raw_verification,
            "items": [],
            "ground_truth_status": ground_truth.get("status"),
        }

    items = [
        evaluate_project(item, ground_truth, overwrite_eval_projects=args.overwrite_eval_projects)
        for item in CORPUS
    ]
    return {
        "generated_at": now_iso(),
        "pipeline_version": PIPELINE_VERSION,
        "projects_root": str(PROJECTS_ROOT),
        "raw_verification": raw_verification,
        "ground_truth_status": ground_truth.get("status"),
        "ground_truth_path": str(GROUND_TRUTH_PATH),
        "items": items,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-draft-ground-truth", action="store_true", help="Allow extraction while ground truth status is not approved.")
    parser.add_argument("--verify-raw-only", action="store_true", help="Verify raw files and write JSON, but do not create/extract projects.")
    parser.add_argument("--overwrite-eval-projects", action="store_true", help="Delete and recreate projects that were previously generated by this eval script.")
    parser.add_argument("--results", default=str(RESULTS_PATH), help="Path for raw JSON results.")
    parser.add_argument("--report", default=str(REPORT_PATH), help="Path for Markdown report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        results = run_eval(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    results_path = Path(args.results)
    report_path = Path(args.report)
    write_json(results_path, results)
    if not args.verify_raw_only:
        report_path.write_text(render_report(results), encoding="utf-8", newline="\n")
    print(f"Wrote {display_path(results_path)}")
    if not args.verify_raw_only:
        print(f"Wrote {display_path(report_path)}")
    ok = results.get("raw_verification", {}).get("ok", False)
    if not ok:
        return 1
    if any(item.get("verdict") == "STRUCTURAL-FAIL" for item in results.get("items", [])):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
