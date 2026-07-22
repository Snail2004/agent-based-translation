from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = BACKEND_ROOT.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from pipeline.ingest.admitted_projection import build_admitted_projection
from pipeline.ingest.canonical_source_package import (
    canonical_json_sha256,
    seal_asset_manifest,
)
from pipeline.ingest.unified_source_normalizer import (
    UnifiedNormalizationResult,
    normalize_source,
    validate_normalization_contract,
)
from pipeline.ingest.source_package_exporter import seal_translation_overlay


source_lifecycle = None
CANDIDATE_DIRECTORY = ""
DECISION_DIRECTORY = ""
FINALIZATION_DIRECTORY = ""
HIERARCHY_DIRECTORY = ""
STATE_FILENAME = ""
STATE_V2_FILENAME = ""
SourceLifecycleError = ValueError
apply_managed_source_corrections = None
apply_managed_source_hierarchy = None
ensure_legacy_extract_allowed = None
ensure_source_upload_allowed = None
finalize_managed_source_package = None
get_source_package_review = None
get_source_package_status = None
get_source_package_unit_blocks = None
import_d2l_presegmented_source_package = None
normalize_managed_source_package = None
source_lifecycle_mutation_guard = None


@pytest.fixture(scope="module", autouse=True)
def _load_source_lifecycle_after_test_collection():
    # test_api_smoke sets its project-root environment in setUpClass. Importing
    # config during collection would freeze a different root for that test class.
    global source_lifecycle
    global CANDIDATE_DIRECTORY, DECISION_DIRECTORY
    global FINALIZATION_DIRECTORY, HIERARCHY_DIRECTORY
    global STATE_FILENAME, STATE_V2_FILENAME, SourceLifecycleError
    global apply_managed_source_corrections, apply_managed_source_hierarchy
    global ensure_legacy_extract_allowed, ensure_source_upload_allowed
    global finalize_managed_source_package
    global get_source_package_review, get_source_package_unit_blocks
    global get_source_package_status, import_d2l_presegmented_source_package
    global normalize_managed_source_package
    global source_lifecycle_mutation_guard

    from services import source_lifecycle as module

    source_lifecycle = module
    CANDIDATE_DIRECTORY = module.CANDIDATE_DIRECTORY
    DECISION_DIRECTORY = module.DECISION_DIRECTORY
    FINALIZATION_DIRECTORY = module.FINALIZATION_DIRECTORY
    HIERARCHY_DIRECTORY = module.HIERARCHY_DIRECTORY
    STATE_FILENAME = module.STATE_FILENAME
    STATE_V2_FILENAME = module.STATE_V2_FILENAME
    SourceLifecycleError = module.SourceLifecycleError
    apply_managed_source_corrections = module.apply_managed_source_corrections
    apply_managed_source_hierarchy = module.apply_managed_source_hierarchy
    ensure_legacy_extract_allowed = module.ensure_legacy_extract_allowed
    ensure_source_upload_allowed = module.ensure_source_upload_allowed
    finalize_managed_source_package = module.finalize_managed_source_package
    get_source_package_review = module.get_source_package_review
    get_source_package_unit_blocks = module.get_source_package_unit_blocks
    get_source_package_status = module.get_source_package_status
    import_d2l_presegmented_source_package = (
        module.import_d2l_presegmented_source_package
    )
    normalize_managed_source_package = module.normalize_managed_source_package
    source_lifecycle_mutation_guard = module.source_lifecycle_mutation_guard
    try:
        yield
    finally:
        if sys.modules.get("services.source_lifecycle") is module:
            sys.modules.pop("services.source_lifecycle", None)
        sys.modules.pop("config", None)


FORMATS = {
    "txt": (".txt", b"CHAPTER I\n\nStory text.\n"),
    "markdown": (".md", b"# Chapter I\n\nStory text.\n"),
    "html": (".html", b"<main><h1>Chapter I</h1><p>Story text.</p></main>"),
    "epub": (".epub", b"synthetic epub bytes"),
    "pdf": (".pdf", b"%PDF-1.4 synthetic"),
}


def _project(tmp_path: Path, doc_id: str, source_format: str) -> tuple[Path, Path]:
    root = tmp_path / doc_id
    for name in ("raw", "canonical", "working", "logs", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    suffix, payload = FORMATS[source_format]
    source = root / "raw" / f"source{suffix}"
    source.write_bytes(payload)
    return root, source


def _fake_normalizer(
    calls: list[dict] | None = None,
    *,
    template_text: str = "CHAPTER I\n\nStory text.\n",
):
    def run(source_path: Path, **kwargs) -> UnifiedNormalizationResult:
        source = Path(source_path).resolve()
        if calls is not None:
            calls.append({"source": source, **kwargs})
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "template.txt"
            template.write_text(template_text, encoding="utf-8", newline="\n")
            base = normalize_source(
                template,
                doc_id=kwargs["doc_id"],
                pandoc_executable=None,
            )
        source_format = source_lifecycle.detect_source_format(source)
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        document = copy.deepcopy(base.document)
        structure = copy.deepcopy(base.structure_manifest)
        document["metadata"].update(
            {
                "source_format": source_format,
                "raw_sha256": source_sha256,
                "extraction_tool": "source-lifecycle-test-normalizer",
                "pipeline_version": "source-lifecycle-test-v1",
            }
        )
        structure["source"] = {
            "path": str(source),
            "sha256": source_sha256,
            "format": source_format,
        }
        structure["normalizer_version"] = "source-lifecycle-test-v1"
        structure["structure_sha256"] = canonical_json_sha256(
            {
                key: value
                for key, value in structure.items()
                if key != "structure_sha256"
            }
        )
        receipt = validate_normalization_contract(
            document,
            structure,
            expected_format=source_format,
        )
        return UnifiedNormalizationResult(
            document=document,
            structure_manifest=structure,
            receipt=receipt,
        )

    return run


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _d2l_upload_payloads(monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, bytes, bytes]:
    from pipeline.ingest import d2l_presegmented_adapter as adapter
    from pipeline.ingest.d2l_presegmented_adapter import D2lCaptureSeal

    texts = [
        "# Chapter One",
        "First prose.",
        ":label:chapter_one",
        "# Chapter Two",
        "$$x + y$$",
    ]
    kinds = ["heading", "prose", "label", "heading", "math_block"]
    chapters = ["d2l_ch1", "d2l_ch1", "d2l_ch1", "d2l_ch2", "d2l_ch2"]
    rows = []
    for index, (text, kind, chapter_id) in enumerate(
        zip(texts, kinds, chapters, strict=True)
    ):
        encoded = text.encode("utf-8")
        rows.append(
            {
                "marker": f"B{index + 1:04d}",
                "block_id": f"{chapter_id}_b{index + 1:03d}",
                "chapter_id": chapter_id,
                "order_index": index,
                "block_type": kind,
                "source_sha256": hashlib.sha256(encoded).hexdigest(),
                "source_utf8_bytes": len(encoded),
            }
        )
    source = (
        "\n\n".join(
            f"[[B{index + 1:04d}]]\n{text}" for index, text in enumerate(texts)
        )
        + "\n"
    ).encode("utf-8")
    block_map = {
        "schema_version": adapter.LEGACY_BLOCK_MAP_SCHEMA_VERSION,
        "document_id": "d2l",
        "rows": rows,
    }
    block_map_bytes = _json_bytes(block_map)
    manifest = {
        "block_count": len(rows),
        "block_map_file": "block_map.json",
        "block_map_sha256": hashlib.sha256(block_map_bytes).hexdigest(),
        "chapter_count": 2,
        "created_at": "2026-07-22T00:00:00Z",
        "document_id": "d2l",
        "encoding": "UTF-8 without BOM",
        "intended_mode": "chatgpt_web_single_chat_single_prompt_no_continue",
        "line_endings": "LF",
        "prompt_file": "prompt.txt",
        "prompt_sha256": "1" * 64,
        "schema_version": adapter.LEGACY_MANIFEST_SCHEMA_VERSION,
        "source_db_path": r"C:\evidence\memory.sqlite3",
        "source_db_sha256": "2" * 64,
        "source_text_utf8_bytes": sum(row["source_utf8_bytes"] for row in rows),
        "upload_file": "d2l_full_book_en_marked_v1.md",
        "upload_file_sha256": hashlib.sha256(source).hexdigest(),
        "upload_file_utf8_bytes": len(source),
    }
    manifest_bytes = _json_bytes(manifest)
    monkeypatch.setattr(
        adapter,
        "AUTHORITATIVE_D2L_CAPTURE",
        D2lCaptureSeal(
            document_id="d2l",
            source_file="d2l_full_book_en_marked_v1.md",
            source_sha256=hashlib.sha256(source).hexdigest(),
            source_utf8_bytes=len(source),
            source_text_utf8_bytes=manifest["source_text_utf8_bytes"],
            block_map_sha256=hashlib.sha256(block_map_bytes).hexdigest(),
            block_map_utf8_bytes=len(block_map_bytes),
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            manifest_utf8_bytes=len(manifest_bytes),
            source_db_sha256=manifest["source_db_sha256"],
            block_count=len(rows),
            chapter_count=2,
        ),
    )
    return source, block_map_bytes, manifest_bytes


def _fake_writer(
    result: UnifiedNormalizationResult,
    output_dir: Path,
    *,
    source_override: Path | None = None,
) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    document = copy.deepcopy(result.document)
    structure = copy.deepcopy(result.structure_manifest)
    if source_override is not None:
        structure["source"]["path"] = str(source_override.resolve())
        structure["structure_sha256"] = canonical_json_sha256(
            {
                key: value
                for key, value in structure.items()
                if key != "structure_sha256"
            }
        )
    bindings = [
        {
            "block_id": block["block_id"],
            "source_kind": "paragraph",
            "semantic_kind": "text",
            "semantic_subtype": "prose",
            "translation_policy": "translate",
            "asset_ids": [],
            "render_role": "text",
            "review_required": False,
        }
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    ]
    asset_manifest = seal_asset_manifest(
        document,
        structure,
        assets=[],
        block_bindings=bindings,
    )
    projection = build_admitted_projection(document, structure, asset_manifest)
    receipt = validate_normalization_contract(
        document,
        structure,
        expected_format=structure["source"]["format"],
    )
    for filename, payload in {
        "document.json": document,
        "structure_manifest.json": structure,
        "normalization_receipt.json": receipt,
        "asset_manifest.json": asset_manifest,
        "admitted_projection_v1.json": projection,
    }.items():
        _write_json(destination / filename, payload)


def _fake_review_writer(
    result: UnifiedNormalizationResult,
    output_dir: Path,
    *,
    source_override: Path | None = None,
) -> None:
    _fake_writer(result, output_dir, source_override=source_override)
    destination = Path(output_dir)
    document = json.loads(
        (destination / "document.json").read_text(encoding="utf-8")
    )
    structure = json.loads(
        (destination / "structure_manifest.json").read_text(encoding="utf-8")
    )
    asset_manifest = json.loads(
        (destination / "asset_manifest.json").read_text(encoding="utf-8")
    )
    bindings = copy.deepcopy(asset_manifest["block_bindings"])
    bindings[0]["review_required"] = True
    bindings[0]["translation_policy"] = "review"
    asset_manifest = seal_asset_manifest(
        document,
        structure,
        assets=copy.deepcopy(asset_manifest["assets"]),
        block_bindings=bindings,
    )
    projection = build_admitted_projection(document, structure, asset_manifest)
    _write_json(destination / "asset_manifest.json", asset_manifest)
    _write_json(destination / "admitted_projection_v1.json", projection)


def _lifecycle(root: Path) -> dict:
    return json.loads((root / "working" / STATE_FILENAME).read_text(encoding="utf-8"))


def _lifecycle_v2(root: Path) -> dict:
    return json.loads(
        (root / "working" / STATE_V2_FILENAME).read_text(encoding="utf-8")
    )


def _reseal_lifecycle(state: dict) -> None:
    state["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in state.items() if key != "integrity"}
    )


def _correction_request(review: dict, actions: list[dict]) -> dict:
    expected = review["expected"]
    return {
        "expected_state_sha256": expected["state_sha256"],
        "expected_candidate_tree_sha256": expected["candidate_tree_sha256"],
        "expected_report_sha256": expected["report_sha256"],
        "approved": True,
        "user": "reviewer_01",
        "actions": copy.deepcopy(actions),
    }


def _hierarchy_request(review: dict, actions: list[dict]) -> dict:
    expected = review["expected"]
    return {
        "expected_state_sha256": expected["state_sha256"],
        "expected_candidate_tree_sha256": expected["candidate_tree_sha256"],
        "expected_report_sha256": expected["report_sha256"],
        "approved": True,
        "user": "reviewer_01",
        "actions": copy.deepcopy(actions),
    }


def _finalization_request(review: dict) -> dict:
    expected = review["expected"]
    return {
        "expected_state_sha256": expected["state_sha256"],
        "expected_candidate_tree_sha256": expected["candidate_tree_sha256"],
        "expected_report_sha256": expected["report_sha256"],
        "expected_hierarchy_sha256": expected["hierarchy_sha256"],
        "approved": True,
        "user": "reviewer_01",
    }


def _package_file_bytes(root: Path, status: dict) -> dict[str, bytes]:
    candidate = root / Path(*status["candidate"]["relative_path"].split("/"))
    return {
        relative.as_posix(): path.read_bytes()
        for path in sorted(candidate.rglob("*"))
        if path.is_file()
        for relative in [path.relative_to(candidate)]
    }


def _finalized_managed_project(
    tmp_path: Path,
    doc_id: str,
    *,
    template_text: str = "CHAPTER I\n\nAlice arrived.\n\nBob waited.\n",
) -> tuple[Path, dict]:
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(template_text=template_text),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    finalized = finalize_managed_source_package(
        root,
        doc_id,
        _finalization_request(review),
    )
    return root, finalized


def _write_managed_runtime_manifest(
    root: Path,
    doc_id: str,
    jobs_root: Path,
    *,
    job_id: str,
) -> Path:
    context = source_lifecycle.get_managed_runtime_context(root, doc_id)
    manifest_path = jobs_root / job_id / "source_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        manifest_path,
        {
            "contract_version": source_lifecycle.MANAGED_RUNTIME_MANIFEST_VERSION,
            "project_id": doc_id,
            "job_id": job_id,
            "managed_source": context["managed_source"],
        },
    )
    return manifest_path


def _rename_first_unit(review: dict, *, suffix: str = " revised") -> dict:
    unit = review["report"]["units"][0]
    return {
        "action_type": "update_unit",
        "unit_id": unit["unit_id"],
        "new_title": f"{unit['title']}{suffix}",
        "classification": None,
    }


@pytest.mark.parametrize("source_format", tuple(FORMATS))
def test_managed_normalization_supports_all_five_formats_and_reuses_exact_candidate(
    tmp_path: Path,
    source_format: str,
) -> None:
    doc_id = f"managed_{source_format}"
    root, _source = _project(tmp_path, doc_id, source_format)
    calls: list[dict] = []

    first = normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(calls),
        write_fn=_fake_writer,
    )
    assert first["created"] is True
    assert first["reused"] is False
    assert first["source"]["format"] == source_format
    assert calls[0]["pandoc_executable"] == source_lifecycle.THESIS_PANDOC_EXE
    assert calls[0]["pdf_formula_detector_mode"] == "disabled"
    state_bytes = (root / "working" / STATE_FILENAME).read_bytes()
    candidate = root / Path(*first["candidate"]["relative_path"].split("/"))
    candidate_files = sorted(
        path.relative_to(candidate).as_posix()
        for path in candidate.rglob("*")
        if path.is_file()
    )
    assert candidate_files == sorted(source_lifecycle.REQUIRED_PACKAGE_FILES)

    second = normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=lambda *_args, **_kwargs: pytest.fail("normalizer must not rerun"),
        write_fn=lambda *_args, **_kwargs: pytest.fail("writer must not rerun"),
    )
    assert second["created"] is False
    assert second["reused"] is True
    assert second["candidate"] == first["candidate"]
    assert (root / "working" / STATE_FILENAME).read_bytes() == state_bytes
    candidates = root / "working" / CANDIDATE_DIRECTORY
    assert [path.name for path in candidates.iterdir()] == [first["candidate"]["candidate_id"]]


def test_candidate_tamper_fails_closed_in_status_and_reuse(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "tampered", "txt")
    created = normalize_managed_source_package(
        root,
        "tampered",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    candidate = root / Path(*created["candidate"]["relative_path"].split("/"))
    document_path = candidate / "document.json"
    document_path.write_bytes(document_path.read_bytes() + b"\n")

    with pytest.raises(SourceLifecycleError) as status_error:
        get_source_package_status(root, "tampered")
    assert status_error.value.code in {
        "source_lifecycle_stale",
        "source_package_invalid",
        "draft_structure_report_stale",
    }
    with pytest.raises(SourceLifecycleError):
        normalize_managed_source_package(root, "tampered")


def test_validation_cache_reuses_evidence_and_hashes_tree_once_per_request(
    tmp_path: Path,
) -> None:
    doc_id = "validation_cache_reuse"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nAlice arrived.\n\nBob waited.\n"
        ),
        write_fn=_fake_review_writer,
    )
    source_lifecycle._candidate_validation_cache_clear()

    original_uncached = source_lifecycle._validate_candidate_uncached
    original_tree_identity = source_lifecycle._tree_identity
    with (
        patch.object(
            source_lifecycle,
            "_validate_candidate_uncached",
            wraps=original_uncached,
        ) as uncached,
        patch.object(
            source_lifecycle,
            "_tree_identity",
            wraps=original_tree_identity,
        ) as tree_identity,
    ):
        status = get_source_package_status(root, doc_id)
        review = get_source_package_review(root, doc_id)
        expected = {
            name: review["expected"][name]
            for name in source_lifecycle.SOURCE_PACKAGE_REVIEW_BINDING_FIELDS
        }
        page = get_source_package_unit_blocks(
            root,
            doc_id,
            review["report"]["units"][0]["unit_id"],
            expected=expected,
            limit=1,
        )

    assert (
        status["candidate"]["tree_sha256"]
        == review["expected"]["candidate_tree_sha256"]
    )
    assert page["pagination"]["returned"] == 1
    assert uncached.call_count == 1
    assert tree_identity.call_count == 3


def test_validation_cache_hashes_bytes_and_does_not_cache_failure(
    tmp_path: Path,
) -> None:
    doc_id = "validation_cache_same_size_tamper"
    root, _source = _project(tmp_path, doc_id, "txt")
    status = normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    get_source_package_status(root, doc_id)
    candidate = root / Path(*status["candidate"]["relative_path"].split("/"))
    document_path = candidate / "document.json"
    original = document_path.read_bytes()
    changed = original.replace(b"Story text.", b"St0ry text.", 1)
    assert len(changed) == len(original)
    assert changed != original
    before = document_path.stat()
    document_path.write_bytes(changed)
    os.utime(document_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = document_path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns

    original_uncached = source_lifecycle._validate_candidate_uncached
    with patch.object(
        source_lifecycle,
        "_validate_candidate_uncached",
        wraps=original_uncached,
    ) as uncached:
        for _attempt in range(2):
            with pytest.raises(SourceLifecycleError) as captured:
                get_source_package_status(root, doc_id)
            assert captured.value.status == 409
        assert uncached.call_count == 2


@pytest.mark.parametrize("mutation", ("add", "delete", "replace"))
def test_validation_cache_rejects_candidate_file_set_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    doc_id = f"validation_cache_{mutation}"
    root, _source = _project(tmp_path, doc_id, "txt")
    status = normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    get_source_package_status(root, doc_id)
    candidate = root / Path(*status["candidate"]["relative_path"].split("/"))
    if mutation == "add":
        (candidate / "unexpected.bin").write_bytes(b"unexpected")
    elif mutation == "delete":
        (candidate / "draft_structure_report.json").unlink()
    else:
        document_path = candidate / "document.json"
        replacement_path = candidate / "document.replacement"
        original = document_path.read_bytes()
        changed = original.replace(b"Story text.", b"St0ry text.", 1)
        assert changed != original
        replacement_path.write_bytes(changed)
        os.replace(replacement_path, document_path)

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_status(root, doc_id)
    assert captured.value.status == 409
    assert captured.value.code in {
        "source_package_file_set_invalid",
        "source_package_incomplete",
        "source_package_invalid",
        "draft_structure_report_stale",
    }


def test_validation_cache_isolated_by_project_source_and_state(tmp_path: Path) -> None:
    projects: list[tuple[Path, Path, str]] = []
    for suffix in ("a", "b"):
        doc_id = f"validation_cache_isolation_{suffix}"
        root, source = _project(tmp_path, doc_id, "txt")
        normalize_managed_source_package(
            root,
            doc_id,
            normalize_fn=_fake_normalizer(),
            write_fn=_fake_writer,
        )
        projects.append((root, source, doc_id))
    source_lifecycle._candidate_validation_cache_clear()

    original_uncached = source_lifecycle._validate_candidate_uncached
    with patch.object(
        source_lifecycle,
        "_validate_candidate_uncached",
        wraps=original_uncached,
    ) as uncached:
        for root, _source, doc_id in projects:
            get_source_package_status(root, doc_id)
        for root, _source, doc_id in projects:
            get_source_package_status(root, doc_id)
        assert uncached.call_count == 2

        root, source, doc_id = projects[0]
        state = _lifecycle(root)
        state["package"]["document"]["sha256"] = "0" * 64
        _reseal_lifecycle(state)
        _write_json(root / "working" / STATE_FILENAME, state)
        with pytest.raises(SourceLifecycleError) as state_error:
            get_source_package_status(root, doc_id)
        assert state_error.value.code == "source_lifecycle_stale"
        assert uncached.call_count == 3

        source.write_bytes(source.read_bytes() + b"changed")
        with pytest.raises(SourceLifecycleError) as source_error:
            get_source_package_status(root, doc_id)
        assert source_error.value.code == "source_package_source_changed"
        assert uncached.call_count == 3


def test_validation_cache_is_bounded_and_deduplicates_concurrent_cold_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_lifecycle, "_CANDIDATE_VALIDATION_CACHE_MAX_ENTRIES", 2)
    roots: list[tuple[Path, str]] = []
    for index in range(3):
        doc_id = f"validation_cache_bound_{index}"
        root, _source = _project(tmp_path, doc_id, "txt")
        normalize_managed_source_package(
            root,
            doc_id,
            normalize_fn=_fake_normalizer(),
            write_fn=_fake_writer,
        )
        roots.append((root, doc_id))
    assert len(source_lifecycle._candidate_validation_cache) <= 2

    root, doc_id = roots[-1]
    source_lifecycle._candidate_validation_cache_clear()
    original_uncached = source_lifecycle._validate_candidate_uncached
    entered = threading.Event()

    def slow_uncached(*args, **kwargs):
        entered.set()
        time.sleep(0.1)
        return original_uncached(*args, **kwargs)

    results: list[dict] = []
    failures: list[BaseException] = []

    def read_status() -> None:
        try:
            results.append(get_source_package_status(root, doc_id))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    with patch.object(
        source_lifecycle,
        "_validate_candidate_uncached",
        side_effect=slow_uncached,
    ) as uncached:
        threads = [threading.Thread(target=read_status) for _ in range(4)]
        for thread in threads:
            thread.start()
        assert entered.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=5)

    assert failures == []
    assert len(results) == 4
    assert all(not thread.is_alive() for thread in threads)
    assert uncached.call_count == 1


def test_resealed_foreign_candidate_path_is_rejected(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "foreign_path", "txt")
    normalize_managed_source_package(
        root,
        "foreign_path",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    state = _lifecycle(root)
    state["candidate"]["relative_path"] = "../foreign"
    _reseal_lifecycle(state)
    _write_json(root / "working" / STATE_FILENAME, state)

    with pytest.raises(SourceLifecycleError, match="immutable managed location") as captured:
        get_source_package_status(root, "foreign_path")
    assert captured.value.code == "source_lifecycle_invalid"


def test_foreign_source_binding_is_rejected_before_publication(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "foreign_source", "txt")
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("foreign", encoding="utf-8")

    def writer(result, output_dir):
        _fake_writer(result, output_dir, source_override=foreign)

    with pytest.raises(SourceLifecycleError) as captured:
        normalize_managed_source_package(
            root,
            "foreign_source",
            normalize_fn=_fake_normalizer(),
            write_fn=writer,
        )
    assert captured.value.code == "source_package_foreign_source"
    assert not (root / "working" / STATE_FILENAME).exists()
    assert list((root / "working" / CANDIDATE_DIRECTORY).iterdir()) == []


def test_source_mutation_during_normalization_rolls_back(tmp_path: Path) -> None:
    root, source = _project(tmp_path, "source_mutation", "txt")

    def writer(result, output_dir):
        _fake_writer(result, output_dir)
        source.write_text("changed after normalization", encoding="utf-8")

    with pytest.raises(SourceLifecycleError) as captured:
        normalize_managed_source_package(
            root,
            "source_mutation",
            normalize_fn=_fake_normalizer(),
            write_fn=writer,
        )
    assert captured.value.code == "source_changed_during_normalization"
    assert not (root / "working" / STATE_FILENAME).exists()
    assert list((root / "working" / CANDIDATE_DIRECTORY).iterdir()) == []


def test_partial_writer_failure_rolls_back_without_state(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "writer_failure", "txt")

    def broken_writer(_result, output_dir):
        Path(output_dir, "document.json").write_text("{}", encoding="utf-8")
        raise OSError("synthetic writer failure")

    with pytest.raises(SourceLifecycleError) as captured:
        normalize_managed_source_package(
            root,
            "writer_failure",
            normalize_fn=_fake_normalizer(),
            write_fn=broken_writer,
        )
    assert captured.value.code == "source_package_normalization_failed"
    assert not (root / "working" / STATE_FILENAME).exists()
    assert list((root / "working" / CANDIDATE_DIRECTORY).iterdir()) == []


def test_pointer_write_failure_leaves_reusable_immutable_candidate(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "pointer_failure", "txt")
    original_write = source_lifecycle._atomic_json_write
    failed = False

    def fail_first_pointer(path: Path, payload: dict) -> None:
        nonlocal failed
        if Path(path).name == STATE_FILENAME and not failed:
            failed = True
            raise OSError("synthetic lifecycle pointer failure")
        original_write(Path(path), payload)

    with patch.object(source_lifecycle, "_atomic_json_write", side_effect=fail_first_pointer):
        with pytest.raises(SourceLifecycleError) as captured:
            normalize_managed_source_package(
                root,
                "pointer_failure",
                normalize_fn=_fake_normalizer(),
                write_fn=_fake_writer,
            )
    assert captured.value.code == "source_package_normalization_failed"
    assert not (root / "working" / STATE_FILENAME).exists()
    candidates = list((root / "working" / CANDIDATE_DIRECTORY).iterdir())
    assert len(candidates) == 1
    orphan_name = candidates[0].name

    recovered = normalize_managed_source_package(
        root,
        "pointer_failure",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    assert recovered["created"] is False
    assert recovered["reused"] is True
    assert recovered["candidate"]["candidate_id"] == orphan_name
    assert [path.name for path in (root / "working" / CANDIDATE_DIRECTORY).iterdir()] == [
        orphan_name
    ]


def test_normalization_waits_for_same_project_mutation_guard(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "serialized_mutation", "txt")
    started = threading.Event()
    completed = threading.Event()
    result: dict = {}

    def worker() -> None:
        started.set()
        result.update(
            normalize_managed_source_package(
                root,
                "serialized_mutation",
                normalize_fn=_fake_normalizer(),
                write_fn=_fake_writer,
            )
        )
        completed.set()

    thread = threading.Thread(target=worker, daemon=True)
    with source_lifecycle_mutation_guard(root):
        thread.start()
        assert started.wait(timeout=1)
        assert completed.wait(timeout=0.1) is False
    thread.join(timeout=5)

    assert completed.is_set()
    assert result["created"] is True


def test_mutation_guard_is_reentrant_and_serializes_another_process(
    tmp_path: Path,
) -> None:
    root, _source = _project(tmp_path, "serialized_process", "txt")
    ready = tmp_path / "child-lock-ready"
    script = f"""
import sys
import time
from pathlib import Path
sys.path.insert(0, {str(BACKEND_ROOT)!r})
sys.path.insert(0, {str(TOOL_ROOT)!r})
from services.source_lifecycle import source_lifecycle_mutation_guard
root = Path(sys.argv[1])
ready = Path(sys.argv[2])
with source_lifecycle_mutation_guard(root):
    ready.write_text('ready', encoding='utf-8')
    time.sleep(0.8)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), process.communicate(timeout=5)

    started = time.monotonic()
    with source_lifecycle_mutation_guard(root):
        with source_lifecycle_mutation_guard(root):
            pass
    elapsed = time.monotonic() - started
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, (stdout, stderr)
    assert elapsed >= 0.45


def test_mutation_guard_recovers_after_lock_owner_exits_without_cleanup(
    tmp_path: Path,
) -> None:
    root, _source = _project(tmp_path, "abandoned_process_lock", "txt")
    ready = tmp_path / "abandoned-lock-ready"
    script = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(BACKEND_ROOT)!r})
sys.path.insert(0, {str(TOOL_ROOT)!r})
from services.source_lifecycle import source_lifecycle_mutation_guard
root = Path(sys.argv[1])
ready = Path(sys.argv[2])
with source_lifecycle_mutation_guard(root):
    ready.write_text('ready', encoding='utf-8')
    os._exit(17)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    stdout, stderr = process.communicate(timeout=5)

    assert ready.exists(), (stdout, stderr)
    assert process.returncode == 17, (stdout, stderr)
    started = time.monotonic()
    with source_lifecycle_mutation_guard(root):
        pass
    assert time.monotonic() - started < 1


def test_reparse_source_is_rejected_without_reading_bytes(tmp_path: Path) -> None:
    root, source = _project(tmp_path, "reparse_source", "txt")
    original = source_lifecycle._is_reparse_point

    def flagged(path: Path) -> bool:
        return Path(path) == source or original(Path(path))

    with patch.object(source_lifecycle, "_is_reparse_point", side_effect=flagged):
        with pytest.raises(SourceLifecycleError) as captured:
            normalize_managed_source_package(root, "reparse_source")
    assert captured.value.code == "source_package_path_unsafe"
    assert not (root / "working" / STATE_FILENAME).exists()


def test_reparse_candidate_is_rejected_before_artifact_read(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "reparse_candidate", "txt")
    created = normalize_managed_source_package(
        root,
        "reparse_candidate",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    candidate = root / Path(*created["candidate"]["relative_path"].split("/"))
    original = source_lifecycle._is_reparse_point

    def flagged(path: Path) -> bool:
        return Path(path) == candidate or original(Path(path))

    with patch.object(source_lifecycle, "_is_reparse_point", side_effect=flagged):
        with pytest.raises(SourceLifecycleError) as captured:
            get_source_package_status(root, "reparse_candidate")
    assert captured.value.code == "source_package_path_unsafe"


@pytest.mark.parametrize("relative", ("working", f"working/{CANDIDATE_DIRECTORY}"))
def test_reparse_managed_parent_is_rejected_before_artifact_read(
    tmp_path: Path,
    relative: str,
) -> None:
    root, _source = _project(tmp_path, "reparse_parent", "txt")
    normalize_managed_source_package(
        root,
        "reparse_parent",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    flagged_path = root / Path(*relative.split("/"))
    original = source_lifecycle._is_reparse_point

    def flagged(path: Path) -> bool:
        return Path(path) == flagged_path or original(Path(path))

    with patch.object(source_lifecycle, "_is_reparse_point", side_effect=flagged):
        with pytest.raises(SourceLifecycleError) as captured:
            get_source_package_status(root, "reparse_parent")
    assert captured.value.code == "source_package_path_unsafe"


def test_boolean_pipeline_run_count_is_rejected_even_when_resealed(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "boolean_run_count", "txt")
    normalize_managed_source_package(
        root,
        "boolean_run_count",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    state = _lifecycle(root)
    state["pipeline_run_count"] = False
    _reseal_lifecycle(state)
    _write_json(root / "working" / STATE_FILENAME, state)

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_status(root, "boolean_run_count")
    assert captured.value.code == "source_lifecycle_invalid"


def test_managed_state_blocks_upload_and_legacy_extract(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "route_lock", "txt")
    normalize_managed_source_package(
        root,
        "route_lock",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    with pytest.raises(SourceLifecycleError) as upload_error:
        ensure_source_upload_allowed(root)
    assert upload_error.value.code == "managed_source_overwrite_forbidden"
    with pytest.raises(SourceLifecycleError) as extract_error:
        ensure_legacy_extract_allowed(root)
    assert extract_error.value.code == "managed_source_legacy_extract_forbidden"


def test_pdf_legacy_extract_has_dedicated_error(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "pdf_route", "pdf")
    with pytest.raises(SourceLifecycleError) as captured:
        ensure_legacy_extract_allowed(root)
    assert captured.value.code == "pdf_requires_source_package_route"


def test_existing_canonical_project_remains_legacy_only(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "legacy", "txt")
    (root / "canonical" / "document.json").write_text("{}", encoding="utf-8")
    status = get_source_package_status(root, "legacy")
    assert status["mode"] == "legacy_only"
    assert status["normalize_allowed"] is False
    with pytest.raises(SourceLifecycleError) as captured:
        normalize_managed_source_package(root, "legacy")
    assert captured.value.code == "legacy_project_not_adoptable"


def test_any_canonical_entry_blocks_managed_normalization(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "legacy_sidecar", "txt")
    (root / "canonical" / "entities.jsonl").write_text(
        '{"entity_id":"legacy"}\n',
        encoding="utf-8",
        newline="\n",
    )

    status = get_source_package_status(root, "legacy_sidecar")
    assert status["mode"] == "legacy_only"
    assert status["evidence"] == ["canonical_data"]
    with pytest.raises(SourceLifecycleError) as captured:
        normalize_managed_source_package(root, "legacy_sidecar")
    assert captured.value.code == "legacy_project_not_adoptable"


@pytest.mark.parametrize(
    ("relative_path", "payload", "evidence"),
    (
        ("working/jobs/extract_legacy.json", "{}\n", "project_legacy_job"),
        ("working/extraction_report.json", "{}\n", "project_extraction_report"),
        (
            "working/normalized/structure_plan.json",
            "{}\n",
            "project_normalizer_state",
        ),
    ),
)
def test_project_local_legacy_evidence_blocks_managed_normalization(
    tmp_path: Path,
    relative_path: str,
    payload: str,
    evidence: str,
) -> None:
    doc_id = f"legacy_local_{evidence}"
    root, _source = _project(tmp_path, doc_id, "txt")
    path = root / Path(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")

    status = get_source_package_status(root, doc_id)
    assert status["mode"] == "legacy_only"
    assert evidence in status["evidence"]
    with pytest.raises(SourceLifecycleError) as captured:
        normalize_managed_source_package(root, doc_id)
    assert captured.value.code == "legacy_project_not_adoptable"


def test_external_runtime_and_registered_run_require_exact_server_bindings(
    tmp_path: Path,
) -> None:
    doc_id = "runtime_owned_project"
    root, _source = _project(tmp_path, doc_id, "txt")
    jobs_root = tmp_path / "thesis_jobs"
    matching_job = jobs_root / "src_runtime_owned_123"
    matching_job.mkdir(parents=True)
    _write_json(
        matching_job / "source_manifest.json",
        {
            "contract_version": "project_runtime_source_v1",
            "project_id": doc_id,
            "job_id": matching_job.name,
        },
    )
    foreign_job = jobs_root / "src_foreign_456"
    foreign_job.mkdir(parents=True)
    _write_json(
        foreign_job / "source_manifest.json",
        {
            "contract_version": "project_runtime_source_v1",
            "project_id": "another_project",
            "job_id": foreign_job.name,
        },
    )
    (jobs_root / "thesis_runs.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"run_id": "run_exact", "job_id": matching_job.name}),
                json.dumps({"run_id": "run_foreign", "job_id": foreign_job.name}),
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with patch.object(source_lifecycle, "THESIS_JOBS_ROOT", jobs_root):
        status = get_source_package_status(root, doc_id)
        assert status["mode"] == "legacy_only"
        assert status["evidence"] == ["prepared_runtime", "registered_run"]
        with pytest.raises(SourceLifecycleError) as captured:
            normalize_managed_source_package(root, doc_id)
    assert captured.value.code == "legacy_project_not_adoptable"


def test_foreign_runtime_and_run_do_not_occupy_project(tmp_path: Path) -> None:
    doc_id = "runtime_unrelated_project"
    root, _source = _project(tmp_path, doc_id, "txt")
    jobs_root = tmp_path / "foreign_jobs"
    foreign_job = jobs_root / "src_foreign_789"
    foreign_job.mkdir(parents=True)
    _write_json(
        foreign_job / "source_manifest.json",
        {
            "contract_version": "project_runtime_source_v1",
            "project_id": "foreign_project",
            "job_id": foreign_job.name,
        },
    )
    (jobs_root / "thesis_runs.jsonl").write_text(
        json.dumps({"run_id": "run_foreign", "job_id": foreign_job.name}) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with patch.object(source_lifecycle, "THESIS_JOBS_ROOT", jobs_root):
        status = get_source_package_status(root, doc_id)
    assert status["mode"] == "unmanaged_draft"
    assert status["normalize_allowed"] is True


def test_managed_status_fails_when_legacy_evidence_appears_later(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "managed_conflict", "txt")
    normalize_managed_source_package(
        root,
        "managed_conflict",
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    (root / "canonical" / "entities.jsonl").write_text(
        '{"entity_id":"late"}\n',
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_status(root, "managed_conflict")
    assert captured.value.code == "managed_source_legacy_conflict"
    with pytest.raises(SourceLifecycleError) as reuse_error:
        normalize_managed_source_package(root, "managed_conflict")
    assert reuse_error.value.code == "managed_source_legacy_conflict"


def test_managed_status_fails_when_attributed_runtime_appears_later(
    tmp_path: Path,
) -> None:
    doc_id = "managed_runtime_conflict"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    jobs_root = tmp_path / "late_runtime_jobs"
    job = jobs_root / "src_managed_runtime_123"
    job.mkdir(parents=True)
    _write_json(
        job / "source_manifest.json",
        {
            "contract_version": "project_runtime_source_v1",
            "project_id": doc_id,
            "job_id": job.name,
        },
    )

    with patch.object(source_lifecycle, "THESIS_JOBS_ROOT", jobs_root):
        with pytest.raises(SourceLifecycleError) as captured:
            get_source_package_status(root, doc_id)
        assert captured.value.code == "managed_source_legacy_conflict"
        with pytest.raises(SourceLifecycleError) as reuse_error:
            normalize_managed_source_package(root, doc_id)
    assert reuse_error.value.code == "managed_source_legacy_conflict"


def test_matching_runtime_manifest_requires_exact_registered_job_id(
    tmp_path: Path,
) -> None:
    doc_id = "runtime_job_id_mismatch"
    root, _source = _project(tmp_path, doc_id, "txt")
    jobs_root = tmp_path / "mismatched_runtime_jobs"
    job = jobs_root / "src_runtime_mismatch_123"
    job.mkdir(parents=True)
    _write_json(
        job / "source_manifest.json",
        {
            "contract_version": "project_runtime_source_v1",
            "project_id": doc_id,
            "job_id": "src_different_job_456",
        },
    )

    with patch.object(source_lifecycle, "THESIS_JOBS_ROOT", jobs_root):
        with pytest.raises(SourceLifecycleError) as captured:
            get_source_package_status(root, doc_id)
    assert captured.value.code == "legacy_runtime_evidence_invalid"


def test_unmanaged_status_does_not_create_lifecycle_state(tmp_path: Path) -> None:
    root, _source = _project(tmp_path, "status_only", "html")
    status = get_source_package_status(root, "status_only")
    assert status["mode"] == "unmanaged_draft"
    assert status["source"]["format"] == "html"
    assert not (root / "working" / STATE_FILENAME).exists()


def test_review_exposes_sealed_issue_queue_and_authoritative_unit_blocks(
    tmp_path: Path,
) -> None:
    doc_id = "review_authoritative_blocks"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nAlice arrived.\n\nBob waited.\n"
        ),
        write_fn=_fake_review_writer,
    )

    review = get_source_package_review(root, doc_id)
    expected = review["expected"]
    assert expected["document_sha256"] == review["report"]["inputs"][
        "document"
    ]["sha256"]
    assert expected["structure_sha256"] == review["report"]["inputs"][
        "structure"
    ]["sha256"]
    queue = review["issue_queue"]
    assert queue["schema_version"] == "source_package_issue_queue_v1"
    assert queue["inputs"] == expected
    assert queue["integrity"]["row_count"] == len(queue["rows"])
    assert queue["integrity"]["payload_sha256"] == canonical_json_sha256(
        {key: value for key, value in queue.items() if key != "integrity"}
    )
    assert [row["order_index"] for row in queue["rows"]] == list(
        range(len(queue["rows"]))
    )
    assert {row["issue_id"] for row in queue["rows"]} == {
        row["issue_id"] for row in review["report"]["issues"]
    }
    block_issue = next(
        row for row in queue["rows"] if row["code"] == "block_requires_review"
    )
    unit = review["report"]["units"][0]
    assert block_issue["target_unit_id"] == unit["unit_id"]
    assert block_issue["target_block_id"] == unit["block_ids"][0]
    assert block_issue["navigation"]["unit_id"] == unit["unit_id"]
    assert block_issue["navigation"]["block_id"] == unit["block_ids"][0]

    bindings = {
        name: expected[name]
        for name in source_lifecycle.SOURCE_PACKAGE_REVIEW_BINDING_FIELDS
    }
    page = get_source_package_unit_blocks(
        root,
        doc_id,
        unit["unit_id"],
        expected=bindings,
        offset=0,
        limit=1,
    )
    status = get_source_package_status(root, doc_id)
    candidate = root / Path(*status["candidate"]["relative_path"].split("/"))
    document = json.loads(
        (candidate / "document.json").read_text(encoding="utf-8")
    )
    authoritative_block = document["chapters"][0]["blocks"][0]
    assert page["expected"] == bindings
    assert page["blocks"] == [
        {
            "block_id": authoritative_block["block_id"],
            "order_index": authoritative_block["order_index"],
            "block_type": authoritative_block["block_type"],
            "source_text": authoritative_block["source_text"],
        }
    ]
    assert page["pagination"]["returned"] == 1
    assert page["pagination"]["total"] == len(unit["block_ids"])
    assert page["integrity"]["payload_sha256"] == canonical_json_sha256(
        {key: value for key, value in page.items() if key != "integrity"}
    )

    stale = {**bindings, "report_sha256": "0" * 64}
    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_unit_blocks(
            root,
            doc_id,
            unit["unit_id"],
            expected=stale,
        )
    assert captured.value.code == "source_package_review_stale"

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_unit_blocks(
            root,
            doc_id,
            unit["unit_id"],
            expected={"state_sha256": bindings["state_sha256"]},
        )
    assert captured.value.code == "source_package_review_binding_required"

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_unit_blocks(
            root,
            doc_id,
            "foreign_unit",
            expected=bindings,
        )
    assert captured.value.code == "source_package_review_unit_missing"


def test_review_and_correction_publish_sealed_child_without_mutating_bootstrap(
    tmp_path: Path,
) -> None:
    doc_id = "correction_happy"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    bootstrap_bytes = (root / "working" / STATE_FILENAME).read_bytes()
    review = get_source_package_review(root, doc_id)
    request = _correction_request(review, [_rename_first_unit(review)])

    result = apply_managed_source_corrections(root, doc_id, request)

    assert result["decision_created"] is True
    assert result["mode"] == "managed_draft"
    assert result["revision"]["load_bearing"] is False
    assert result["revision"]["hierarchy"]["sha256"] is None
    assert result["revision"]["finalization"]["sha256"] is None
    assert (root / "working" / STATE_FILENAME).read_bytes() == bootstrap_bytes
    state_v2 = _lifecycle_v2(root)
    decision_sha = state_v2["latest_decision"]["sha256"]
    decision = root / "working" / DECISION_DIRECTORY / f"srcdec_{decision_sha}.json"
    assert decision.is_file()
    child = root / Path(*state_v2["candidate"]["relative_path"].split("/"))
    child_files = sorted(
        path.relative_to(child).as_posix()
        for path in child.rglob("*")
        if path.is_file()
    )
    assert child_files == sorted(source_lifecycle.REQUIRED_PACKAGE_FILES)
    assert not any("plan" in name or "decision" in name for name in child_files)
    corrected_review = get_source_package_review(root, doc_id)
    assert corrected_review["report"]["units"][0]["title"].endswith(" revised")

    retried = apply_managed_source_corrections(root, doc_id, request)
    assert retried["decision_created"] is False
    assert retried["decision_reused"] is True
    assert len(list((root / "working" / DECISION_DIRECTORY).iterdir())) == 1

    second_review = get_source_package_review(root, doc_id)
    second_request = _correction_request(
        second_review,
        [_rename_first_unit(second_review, suffix=" twice revised")],
    )
    second = apply_managed_source_corrections(root, doc_id, second_request)
    assert second["decision_created"] is True
    second_state = _lifecycle_v2(root)
    second_decision_sha = second_state["latest_decision"]["sha256"]
    second_decision = json.loads(
        (
            root
            / "working"
            / DECISION_DIRECTORY
            / f"srcdec_{second_decision_sha}.json"
        ).read_text(encoding="utf-8")
    )
    assert second_decision["parent"]["decision_sha256"] == decision_sha
    assert len(list((root / "working" / DECISION_DIRECTORY).iterdir())) == 2
    final_review = get_source_package_review(root, doc_id)
    assert final_review["report"]["units"][0]["title"].endswith(
        " twice revised"
    )


def test_correction_rejects_noop_and_review_required_plan_atomically(
    tmp_path: Path,
) -> None:
    doc_id = "correction_rejected"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    unit = review["report"]["units"][0]
    noop = {
        "action_type": "update_unit",
        "unit_id": unit["unit_id"],
        "new_title": unit["title"],
        "classification": None,
    }
    with pytest.raises(SourceLifecycleError) as captured:
        apply_managed_source_corrections(
            root,
            doc_id,
            _correction_request(review, [noop]),
        )
    assert captured.value.code == "source_package_correction_noop"

    mixed = [
        _rename_first_unit(review),
        {
            "action_type": "update_unit",
            "unit_id": "foreign_unit",
            "new_title": "Foreign",
            "classification": None,
        },
    ]
    with pytest.raises(SourceLifecycleError) as captured:
        apply_managed_source_corrections(
            root,
            doc_id,
            _correction_request(review, mixed),
        )
    assert captured.value.code == "source_package_correction_review_required"
    assert not (root / "working" / STATE_V2_FILENAME).exists()
    assert not (root / "working" / DECISION_DIRECTORY).exists()


def test_correction_rejects_resealed_child_with_changed_asset_bytes(
    tmp_path: Path,
) -> None:
    doc_id = "correction_asset_tamper"
    root, source = _project(tmp_path, doc_id, "html")
    source.write_text(
        "<main><h1>Chapter I</h1><p>Story text.</p>"
        "<table><tr><th>A</th></tr><tr><td>1</td></tr></table></main>",
        encoding="utf-8",
        newline="\n",
    )
    normalize_managed_source_package(root, doc_id)
    review = get_source_package_review(root, doc_id)
    request = _correction_request(review, [_rename_first_unit(review)])
    original_materialize = source_lifecycle.materialize_source_package
    materialize_calls = 0

    def materialize_with_resealed_asset_tamper(
        document: dict,
        structure: dict,
        output_dir: Path,
    ):
        nonlocal materialize_calls
        materialize_calls += 1
        result = original_materialize(document, structure, output_dir)
        if materialize_calls != 1:
            return result
        manifest = json.loads(
            result.asset_manifest_path.read_text(encoding="utf-8")
        )
        asset = next(
            row for row in manifest["assets"]
            if row["availability"] == "materialized"
        )
        asset_path = Path(output_dir) / Path(
            *Path(asset["package_path"]).parts
        )
        asset_path.write_bytes(asset_path.read_bytes() + b"\nchanged")
        assets = copy.deepcopy(manifest["assets"])
        for row in assets:
            if row["asset_id"] == asset["asset_id"]:
                row["sha256"] = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        resealed = seal_asset_manifest(
            document,
            structure,
            assets=assets,
            block_bindings=manifest["block_bindings"],
        )
        _write_json(result.asset_manifest_path, resealed)
        _write_json(
            result.admitted_projection_path,
            build_admitted_projection(document, structure, resealed),
        )
        return result

    with patch.object(
        source_lifecycle,
        "materialize_source_package",
        side_effect=materialize_with_resealed_asset_tamper,
    ):
        with pytest.raises(SourceLifecycleError) as captured:
            apply_managed_source_corrections(root, doc_id, request)
    assert captured.value.code == "source_package_decision_invalid"
    assert not (root / "working" / STATE_V2_FILENAME).exists()


@pytest.mark.parametrize(
    "tamper_mode",
    [
        "asset_metadata",
        "asset_source_locator",
        "asset_semantic",
        "binding_semantic",
        "binding_policy_review",
    ],
)
def test_correction_rejects_fully_resealed_child_sidecar_drift(
    tmp_path: Path,
    tamper_mode: str,
) -> None:
    doc_id = f"correction_sidecar_{tamper_mode}"
    root, source = _project(tmp_path, doc_id, "html")
    source.write_text(
        "<main><h1>Chapter I</h1><p>Story text.</p>"
        "<table><tr><th>A</th></tr><tr><td>1</td></tr></table></main>",
        encoding="utf-8",
        newline="\n",
    )
    normalize_managed_source_package(root, doc_id)
    review = get_source_package_review(root, doc_id)
    request = _correction_request(review, [_rename_first_unit(review)])
    original_materialize = source_lifecycle.materialize_source_package
    materialize_calls = 0

    def materialize_with_resealed_sidecar_drift(
        document: dict,
        structure: dict,
        output_dir: Path,
    ):
        nonlocal materialize_calls
        materialize_calls += 1
        result = original_materialize(document, structure, output_dir)
        if materialize_calls != 1:
            return result
        manifest = json.loads(
            result.asset_manifest_path.read_text(encoding="utf-8")
        )
        assets = copy.deepcopy(manifest["assets"])
        bindings = copy.deepcopy(manifest["block_bindings"])
        materialized = next(
            row for row in assets if row["availability"] == "materialized"
        )
        if tamper_mode == "asset_metadata":
            materialized["metadata"]["tampered"] = True
        elif tamper_mode == "asset_source_locator":
            materialized["source_locator"]["html_path"] = "/foreign[1]"
        elif tamper_mode == "asset_semantic":
            materialized["kind"] = "raw_fragment"
        elif tamper_mode == "binding_semantic":
            bindings[0]["source_kind"] = "paragraph"
            bindings[0]["semantic_subtype"] = "tampered_semantic_subtype"
        else:
            binding = next(row for row in bindings if not row["asset_ids"])
            binding["translation_policy"] = "review"
            binding["review_required"] = True
        resealed = seal_asset_manifest(
            document,
            structure,
            assets=assets,
            block_bindings=bindings,
        )
        _write_json(result.asset_manifest_path, resealed)
        _write_json(
            result.admitted_projection_path,
            build_admitted_projection(document, structure, resealed),
        )
        return result

    with patch.object(
        source_lifecycle,
        "materialize_source_package",
        side_effect=materialize_with_resealed_sidecar_drift,
    ):
        with pytest.raises(SourceLifecycleError) as captured:
            apply_managed_source_corrections(root, doc_id, request)
    assert captured.value.code == "source_package_decision_invalid"
    assert "deterministic correction materialization" in str(captured.value)
    assert materialize_calls == 2
    assert not (root / "working" / STATE_V2_FILENAME).exists()


def test_v2_tamper_fails_closed_without_falling_back_to_v1(tmp_path: Path) -> None:
    doc_id = "correction_v2_tamper"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    apply_managed_source_corrections(
        root,
        doc_id,
        _correction_request(review, [_rename_first_unit(review)]),
    )
    state_v2 = _lifecycle_v2(root)
    state_v2["latest_decision"]["sha256"] = "0" * 64
    state_v2["latest_decision"]["relative_path"] = (
        f"working/{DECISION_DIRECTORY}/srcdec_{'0' * 64}.json"
    )
    _reseal_lifecycle(state_v2)
    _write_json(root / "working" / STATE_V2_FILENAME, state_v2)

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_status(root, doc_id)
    assert captured.value.code == "source_package_decision_missing"
    assert (root / "working" / STATE_FILENAME).is_file()


def test_correction_pointer_failure_reuses_orphan_candidate_and_decision(
    tmp_path: Path,
) -> None:
    doc_id = "correction_pointer_recovery"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    request = _correction_request(review, [_rename_first_unit(review)])
    original_write = source_lifecycle._atomic_json_write

    def fail_v2_pointer(path: Path, payload: dict) -> None:
        if Path(path).name == STATE_V2_FILENAME:
            raise OSError("synthetic v2 pointer failure")
        original_write(path, payload)

    with patch.object(source_lifecycle, "_atomic_json_write", side_effect=fail_v2_pointer):
        with pytest.raises(SourceLifecycleError) as captured:
            apply_managed_source_corrections(root, doc_id, request)
    assert captured.value.code == "source_package_correction_failed"
    assert not (root / "working" / STATE_V2_FILENAME).exists()
    assert len(list((root / "working" / DECISION_DIRECTORY).iterdir())) == 1

    recovered = apply_managed_source_corrections(root, doc_id, request)
    assert recovered["candidate_reused"] is True
    assert recovered["decision_reused"] is True
    assert (root / "working" / STATE_V2_FILENAME).is_file()


def test_hierarchy_and_finalization_preserve_package_and_are_idempotent(
    tmp_path: Path,
) -> None:
    doc_id = "hierarchy_finalize_happy"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalized = normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nOne.\n\nCHAPTER II\n\nTwo.\n"
        ),
        write_fn=_fake_writer,
    )
    bootstrap_bytes = (root / "working" / STATE_FILENAME).read_bytes()
    package_before = _package_file_bytes(root, normalized)
    review = get_source_package_review(root, doc_id)
    unit_ids = [row["unit_id"] for row in review["report"]["units"]]
    assert len(unit_ids) == 2
    hierarchy_request = _hierarchy_request(
        review,
        [
            {
                "action_type": "set_parent",
                "child_unit_id": unit_ids[1],
                "parent_unit_id": unit_ids[0],
            }
        ],
    )

    hierarchy = apply_managed_source_hierarchy(root, doc_id, hierarchy_request)

    assert hierarchy["mode"] == "managed_draft"
    assert hierarchy["lifecycle"] == "draft"
    assert hierarchy["revision"]["hierarchy"]["sha256"] is not None
    assert hierarchy["revision"]["finalization"]["sha256"] is None
    assert _package_file_bytes(root, hierarchy) == package_before
    assert (root / "working" / STATE_FILENAME).read_bytes() == bootstrap_bytes
    hierarchy_files = {
        path.name
        for path in (root / "working" / HIERARCHY_DIRECTORY).iterdir()
    }
    assert any(name.startswith("hplan_") for name in hierarchy_files)
    assert any(name.startswith("hoverlay_") for name in hierarchy_files)

    hierarchy_retry = apply_managed_source_hierarchy(
        root, doc_id, hierarchy_request
    )
    assert hierarchy_retry["hierarchy_reused"] is True
    assert hierarchy_retry["decision_reused"] is True

    final_review = get_source_package_review(root, doc_id)
    final_request = _finalization_request(final_review)
    finalized = finalize_managed_source_package(root, doc_id, final_request)

    assert finalized["mode"] == "managed_finalized_pre_run"
    assert finalized["lifecycle"] == "finalized_pre_run"
    assert finalized["pipeline_run_count"] == 0
    assert finalized["revision"]["hierarchy"] == hierarchy["revision"]["hierarchy"]
    assert finalized["revision"]["finalization"]["sha256"] is not None
    assert finalized["corrections_allowed"] is True
    assert finalized["hierarchy_allowed"] is True
    assert finalized["finalization_allowed"] is False
    assert _package_file_bytes(root, finalized) == package_before
    finalization_sha = finalized["revision"]["finalization"]["sha256"]
    assert (
        root
        / "working"
        / FINALIZATION_DIRECTORY
        / f"srcfin_{finalization_sha}.json"
    ).is_file()

    finalized_retry = finalize_managed_source_package(root, doc_id, final_request)
    assert finalized_retry["finalization_reused"] is True
    assert finalized_retry["decision_reused"] is True
    assert _package_file_bytes(root, finalized_retry) == package_before


def test_hierarchy_change_after_finalization_returns_to_draft(
    tmp_path: Path,
) -> None:
    doc_id = "hierarchy_after_finalize"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nOne.\n\nCHAPTER II\n\nTwo.\n"
        ),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    unit_ids = [row["unit_id"] for row in review["report"]["units"]]
    hierarchy = apply_managed_source_hierarchy(
        root,
        doc_id,
        _hierarchy_request(
            review,
            [
                {
                    "action_type": "set_parent",
                    "child_unit_id": unit_ids[1],
                    "parent_unit_id": unit_ids[0],
                }
            ],
        ),
    )
    package_before = _package_file_bytes(root, hierarchy)
    finalize_managed_source_package(
        root, doc_id, _finalization_request(get_source_package_review(root, doc_id))
    )
    finalized_review = get_source_package_review(root, doc_id)

    revised = apply_managed_source_hierarchy(
        root,
        doc_id,
        _hierarchy_request(
            finalized_review,
            [
                {
                    "action_type": "clear_parent",
                    "child_unit_id": unit_ids[1],
                }
            ],
        ),
    )

    assert revised["mode"] == "managed_draft"
    assert revised["lifecycle"] == "draft"
    assert revised["revision"]["finalization"]["sha256"] is None
    assert revised["revision"]["hierarchy"]["sha256"] is not None
    assert _package_file_bytes(root, revised) == package_before


def test_first_run_freezes_exact_runtime_and_blocks_all_later_structure_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "managed_first_run_freeze"
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setattr(source_lifecycle, "THESIS_JOBS_ROOT", jobs_root)
    root, finalized = _finalized_managed_project(tmp_path, doc_id)
    review = get_source_package_review(root, doc_id)
    correction_request = _correction_request(review, [_rename_first_unit(review)])
    hierarchy_request = _hierarchy_request(
        review,
        [
            {
                "action_type": "clear_parent",
                "child_unit_id": review["report"]["units"][0]["unit_id"],
            }
        ],
    )
    finalization_request = _finalization_request(review)
    job_id = "managed_job_v2"
    manifest_path = _write_managed_runtime_manifest(
        root, doc_id, jobs_root, job_id=job_id
    )

    frozen = source_lifecycle.freeze_managed_source_for_run(
        root,
        doc_id,
        job_id=job_id,
        run_id="run_first",
        runtime_manifest_path=manifest_path,
    )

    assert frozen["mode"] == "managed_run_started_frozen"
    assert frozen["lifecycle"] == "run_started_frozen"
    assert frozen["pipeline_run_count"] == 1
    assert frozen["run_start_created"] is True
    assert frozen["candidate"] == finalized["candidate"]
    frozen_review = get_source_package_review(root, doc_id)
    assert frozen_review["pipeline_run_count"] == 1
    assert frozen_review["supported_actions"] == []
    assert frozen_review["supported_hierarchy_actions"] == []
    assert frozen_review["experimental"]["load_bearing"] is True

    reused = source_lifecycle.freeze_managed_source_for_run(
        root,
        doc_id,
        job_id=job_id,
        run_id="run_first",
        runtime_manifest_path=manifest_path,
    )
    assert reused["run_start_created"] is False
    assert reused["run_start_reused"] is True

    with pytest.raises(SourceLifecycleError) as alternate:
        source_lifecycle.freeze_managed_source_for_run(
            root,
            doc_id,
            job_id=job_id,
            run_id="run_alternate",
            runtime_manifest_path=manifest_path,
        )
    assert alternate.value.code == "source_package_already_frozen"

    blocked_operations = (
        lambda: apply_managed_source_corrections(root, doc_id, correction_request),
        lambda: apply_managed_source_hierarchy(root, doc_id, hierarchy_request),
        lambda: finalize_managed_source_package(root, doc_id, finalization_request),
    )
    for operation in blocked_operations:
        with pytest.raises(SourceLifecycleError) as blocked:
            operation()
        assert blocked.value.code == "source_package_frozen"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tampered"] = True
    _write_json(manifest_path, manifest)
    with pytest.raises(SourceLifecycleError) as tampered:
        get_source_package_status(root, doc_id)
    assert tampered.value.code == "source_package_run_start_invalid"


def test_publication_requires_frozen_exact_cover_and_reuses_content_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "managed_publication"
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setattr(source_lifecycle, "THESIS_JOBS_ROOT", jobs_root)
    monkeypatch.setattr(source_lifecycle, "THESIS_PANDOC_EXE", None)
    root, finalized = _finalized_managed_project(tmp_path, doc_id)
    package_before = _package_file_bytes(root, finalized)
    candidate_root = root / Path(*finalized["candidate"]["relative_path"].split("/"))
    document = json.loads(
        (candidate_root / "document.json").read_text(encoding="utf-8")
    )
    translations = [
        {
            "block_id": block["block_id"],
            "text": f"VI::{block['block_id']}",
            "html": None,
            "markdown": None,
        }
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    ]
    overlay = seal_translation_overlay(document, translations)

    with pytest.raises(SourceLifecycleError) as before_run:
        source_lifecycle.publish_managed_translation(root, doc_id, overlay)
    assert before_run.value.code == "source_package_publication_not_frozen"

    manifest_path = _write_managed_runtime_manifest(
        root, doc_id, jobs_root, job_id="publication_job"
    )
    source_lifecycle.freeze_managed_source_for_run(
        root,
        doc_id,
        job_id="publication_job",
        run_id="run_publication",
        runtime_manifest_path=manifest_path,
    )

    incomplete = seal_translation_overlay(document, translations[:-1])
    with pytest.raises(SourceLifecycleError) as missing:
        source_lifecycle.publish_managed_translation(root, doc_id, incomplete)
    assert missing.value.code == "source_package_publication_invalid"

    first = source_lifecycle.publish_managed_translation(root, doc_id, overlay)
    second = source_lifecycle.publish_managed_translation(root, doc_id, overlay)

    assert first["created"] is True
    assert first["reused"] is False
    assert second["created"] is False
    assert second["reused"] is True
    assert second["publication_id"] == first["publication_id"]
    publication_root = root / Path(*first["relative_path"].split("/"))
    assert (publication_root / "document.html").is_file()
    assert (publication_root / "document.md").is_file()
    assert _package_file_bytes(root, get_source_package_status(root, doc_id)) == package_before


def test_finalize_and_correction_race_across_processes_has_one_winner(
    tmp_path: Path,
) -> None:
    doc_id = "finalize_correction_race"
    root, source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    requests = {
        "finalize": _finalization_request(review),
        "correct": _correction_request(review, [_rename_first_unit(review)]),
    }
    go = tmp_path / "race-go"
    script = f"""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, {str(BACKEND_ROOT)!r})
sys.path.insert(0, {str(TOOL_ROOT)!r})
from services.source_lifecycle import (
    SourceLifecycleError,
    apply_managed_source_corrections,
    finalize_managed_source_package,
)
operation = sys.argv[1]
root = Path(sys.argv[2])
doc_id = sys.argv[3]
request_path = Path(sys.argv[4])
ready = Path(sys.argv[5])
go = Path(sys.argv[6])
output = Path(sys.argv[7])
ready.write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 5
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
payload = json.loads(request_path.read_text(encoding='utf-8'))
try:
    if operation == 'finalize':
        result = finalize_managed_source_package(root, doc_id, payload)
    else:
        result = apply_managed_source_corrections(root, doc_id, payload)
    record = {{'ok': True, 'lifecycle': result['lifecycle']}}
except SourceLifecycleError as exc:
    record = {{'ok': False, 'code': exc.code}}
output.write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
"""
    processes: list[subprocess.Popen[str]] = []
    outputs: dict[str, Path] = {}
    ready_paths: list[Path] = []
    for operation, payload in requests.items():
        request_path = tmp_path / f"{operation}-request.json"
        ready_path = tmp_path / f"{operation}-ready"
        output_path = tmp_path / f"{operation}-output.json"
        _write_json(request_path, payload)
        ready_paths.append(ready_path)
        outputs[operation] = output_path
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    operation,
                    str(root),
                    doc_id,
                    str(request_path),
                    str(ready_path),
                    str(go),
                    str(output_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    deadline = time.monotonic() + 5
    while not all(path.exists() for path in ready_paths) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(path.exists() for path in ready_paths)
    go.write_text("go", encoding="utf-8")
    diagnostics = [process.communicate(timeout=20) for process in processes]
    assert all(process.returncode == 0 for process in processes), diagnostics

    results = {
        operation: json.loads(path.read_text(encoding="utf-8"))
        for operation, path in outputs.items()
    }
    winners = [name for name, result in results.items() if result["ok"]]
    losers = [result for result in results.values() if not result["ok"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["code"] in {
        "source_package_correction_stale",
        "source_package_finalization_stale",
    }
    status = get_source_package_status(root, doc_id)
    assert status["lifecycle"] in {"draft", "finalized_pre_run"}
    assert status["pipeline_run_count"] == 0
    assert source.read_bytes() == FORMATS["txt"][1]


@pytest.mark.parametrize(
    "action_builder",
    [
        lambda units: {
            "action_type": "set_parent",
            "child_unit_id": "foreign_unit",
            "parent_unit_id": units[0],
        },
        lambda units: {
            "action_type": "set_parent",
            "child_unit_id": units[0],
            "parent_unit_id": units[0],
        },
        lambda units: {
            "action_type": "set_parent",
            "child_unit_id": units[0],
            "parent_unit_id": units[1],
        },
    ],
    ids=["unknown-unit", "self-parent", "child-before-parent"],
)
def test_hierarchy_invalid_actions_fail_without_publishing_state(
    tmp_path: Path,
    action_builder,
) -> None:
    doc_id = "hierarchy_invalid"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nOne.\n\nCHAPTER II\n\nTwo.\n"
        ),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    unit_ids = [row["unit_id"] for row in review["report"]["units"]]

    with pytest.raises(SourceLifecycleError) as captured:
        apply_managed_source_hierarchy(
            root,
            doc_id,
            _hierarchy_request(review, [action_builder(unit_ids)]),
        )

    assert captured.value.code == "source_package_hierarchy_invalid"
    assert not (root / "working" / STATE_V2_FILENAME).exists()
    assert not (root / "working" / HIERARCHY_DIRECTORY).exists()


def test_finalization_rejects_stale_or_tampered_identity(tmp_path: Path) -> None:
    doc_id = "finalization_stale"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    request = _finalization_request(get_source_package_review(root, doc_id))
    request["expected_candidate_tree_sha256"] = "0" * 64

    with pytest.raises(SourceLifecycleError) as captured:
        finalize_managed_source_package(root, doc_id, request)

    assert captured.value.code == "source_package_finalization_stale"
    assert not (root / "working" / STATE_V2_FILENAME).exists()
    assert not (root / "working" / FINALIZATION_DIRECTORY).exists()


def test_fully_resealed_hierarchy_drift_is_rejected_by_lineage(tmp_path: Path) -> None:
    doc_id = "hierarchy_resealed_drift"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nOne.\n\nCHAPTER II\n\nTwo.\n"
        ),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    units = [row["unit_id"] for row in review["report"]["units"]]
    apply_managed_source_hierarchy(
        root,
        doc_id,
        _hierarchy_request(
            review,
            [
                {
                    "action_type": "set_parent",
                    "child_unit_id": units[1],
                    "parent_unit_id": units[0],
                }
            ],
        ),
    )
    state = _lifecycle_v2(root)
    old_overlay_sha = state["hierarchy"]["sha256"]
    overlay_path = (
        root
        / "working"
        / HIERARCHY_DIRECTORY
        / f"hoverlay_{old_overlay_sha}.json"
    )
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    overlay["rows"][1]["parent_unit_id"] = None
    overlay["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in overlay.items() if key != "integrity"}
    )
    new_overlay_sha = overlay["integrity"]["payload_sha256"]
    _write_json(
        root
        / "working"
        / HIERARCHY_DIRECTORY
        / f"hoverlay_{new_overlay_sha}.json",
        overlay,
    )

    old_decision_sha = state["latest_decision"]["sha256"]
    event_path = (
        root
        / "working"
        / DECISION_DIRECTORY
        / f"srcdec_{old_decision_sha}.json"
    )
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["hierarchy"]["sha256"] = new_overlay_sha
    event["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in event.items() if key != "integrity"}
    )
    new_decision_sha = event["integrity"]["payload_sha256"]
    _write_json(
        root
        / "working"
        / DECISION_DIRECTORY
        / f"srcdec_{new_decision_sha}.json",
        event,
    )
    state["hierarchy"]["sha256"] = new_overlay_sha
    state["latest_decision"] = {
        "schema_version": source_lifecycle.SOURCE_PACKAGE_REVISION_VERSION,
        "sha256": new_decision_sha,
        "relative_path": (
            f"working/{DECISION_DIRECTORY}/srcdec_{new_decision_sha}.json"
        ),
    }
    _reseal_lifecycle(state)
    _write_json(root / "working" / STATE_V2_FILENAME, state)

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_status(root, doc_id)
    assert captured.value.code == "source_package_decision_invalid"


def test_fully_resealed_finalization_drift_is_rejected_by_lineage(
    tmp_path: Path,
) -> None:
    doc_id = "finalization_resealed_drift"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(),
        write_fn=_fake_writer,
    )
    finalize_managed_source_package(
        root, doc_id, _finalization_request(get_source_package_review(root, doc_id))
    )
    state = _lifecycle_v2(root)
    old_finalization_sha = state["finalization"]["sha256"]
    finalization_path = (
        root
        / "working"
        / FINALIZATION_DIRECTORY
        / f"srcfin_{old_finalization_sha}.json"
    )
    finalization = json.loads(finalization_path.read_text(encoding="utf-8"))
    finalization["approved_by"]["identifier"] = "forged_reviewer"
    finalization["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in finalization.items() if key != "integrity"}
    )
    new_finalization_sha = finalization["integrity"]["payload_sha256"]
    _write_json(
        root
        / "working"
        / FINALIZATION_DIRECTORY
        / f"srcfin_{new_finalization_sha}.json",
        finalization,
    )

    old_decision_sha = state["latest_decision"]["sha256"]
    event_path = (
        root
        / "working"
        / DECISION_DIRECTORY
        / f"srcdec_{old_decision_sha}.json"
    )
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["finalization"]["sha256"] = new_finalization_sha
    event["integrity"]["payload_sha256"] = canonical_json_sha256(
        {key: value for key, value in event.items() if key != "integrity"}
    )
    new_decision_sha = event["integrity"]["payload_sha256"]
    _write_json(
        root
        / "working"
        / DECISION_DIRECTORY
        / f"srcdec_{new_decision_sha}.json",
        event,
    )
    state["finalization"]["sha256"] = new_finalization_sha
    state["latest_decision"] = {
        "schema_version": source_lifecycle.SOURCE_PACKAGE_REVISION_VERSION,
        "sha256": new_decision_sha,
        "relative_path": (
            f"working/{DECISION_DIRECTORY}/srcdec_{new_decision_sha}.json"
        ),
    }
    _reseal_lifecycle(state)
    _write_json(root / "working" / STATE_V2_FILENAME, state)

    with pytest.raises(SourceLifecycleError) as captured:
        get_source_package_status(root, doc_id)
    assert captured.value.code == "source_package_decision_invalid"


def test_hierarchy_pointer_failure_reuses_orphan_artifacts(tmp_path: Path) -> None:
    doc_id = "hierarchy_pointer_recovery"
    root, _source = _project(tmp_path, doc_id, "txt")
    normalize_managed_source_package(
        root,
        doc_id,
        normalize_fn=_fake_normalizer(
            template_text="CHAPTER I\n\nOne.\n\nCHAPTER II\n\nTwo.\n"
        ),
        write_fn=_fake_writer,
    )
    review = get_source_package_review(root, doc_id)
    units = [row["unit_id"] for row in review["report"]["units"]]
    request = _hierarchy_request(
        review,
        [
            {
                "action_type": "set_parent",
                "child_unit_id": units[1],
                "parent_unit_id": units[0],
            }
        ],
    )
    original_write = source_lifecycle._atomic_json_write

    def fail_v2_pointer(path: Path, payload: dict) -> None:
        if Path(path).name == STATE_V2_FILENAME:
            raise OSError("synthetic hierarchy pointer failure")
        original_write(path, payload)

    with patch.object(source_lifecycle, "_atomic_json_write", side_effect=fail_v2_pointer):
        with pytest.raises(SourceLifecycleError) as captured:
            apply_managed_source_hierarchy(root, doc_id, request)
    assert captured.value.code == "source_package_hierarchy_failed"

    assert not (root / "working" / STATE_V2_FILENAME).exists()
    assert len(list((root / "working" / HIERARCHY_DIRECTORY).iterdir())) == 2
    assert len(list((root / "working" / DECISION_DIRECTORY).iterdir())) == 1

    recovered = apply_managed_source_hierarchy(root, doc_id, request)
    assert recovered["hierarchy_reused"] is True
    assert recovered["decision_reused"] is True


def test_d2l_presegmented_import_preserves_upstream_identity_and_reuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "project_d2l_import"
    root = tmp_path / doc_id
    for name in ("raw", "canonical", "working", "logs", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    source, block_map, manifest = _d2l_upload_payloads(monkeypatch)

    first = import_d2l_presegmented_source_package(
        root,
        doc_id,
        source_bytes=source,
        block_map_bytes=block_map,
        manifest_bytes=manifest,
    )
    assert first["created"] is True
    candidate = root / Path(*Path(first["candidate"]["relative_path"]).parts)
    document = json.loads((candidate / "document.json").read_text("utf-8"))
    structure = json.loads((candidate / "structure_manifest.json").read_text("utf-8"))
    assets = json.loads((candidate / "asset_manifest.json").read_text("utf-8"))
    projection = json.loads(
        (candidate / "admitted_projection_v1.json").read_text("utf-8")
    )
    assert document["doc_id"] == doc_id
    assert len(document["chapters"]) == 2
    assert [
        block["block_id"]
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    ] == [
        "d2l_ch1_b001",
        "d2l_ch1_b002",
        "d2l_ch1_b003",
        "d2l_ch2_b004",
        "d2l_ch2_b005",
    ]
    provenance = structure["source"]["provenance"]
    assert provenance["upstream_document_id"] == "d2l"
    assert provenance["upstream_source_db_sha256"] == "2" * 64
    assert provenance["capture_relative_path"].startswith(
        "working/source_package_captures/d2lps_"
    )
    label_asset = next(
        row
        for row in assets["block_bindings"]
        if row["block_id"] == "d2l_ch1_b003"
    )
    label_projection = next(
        row for row in projection["rows"] if row["block_id"] == "d2l_ch1_b003"
    )
    assert label_asset["semantic_kind"] == "structural"
    assert label_projection["channel"] == "preserve_only"

    first_tree = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    second = import_d2l_presegmented_source_package(
        root,
        doc_id,
        source_bytes=source,
        block_map_bytes=block_map,
        manifest_bytes=manifest,
    )
    assert second["created"] is False
    assert second["reused"] is True
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == first_tree


def test_d2l_presegmented_invalid_upload_leaves_no_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "project_d2l_invalid"
    root = tmp_path / doc_id
    for name in ("raw", "canonical", "working", "logs", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    source, block_map, manifest = _d2l_upload_payloads(monkeypatch)

    with pytest.raises(SourceLifecycleError) as captured:
        import_d2l_presegmented_source_package(
            root,
            doc_id,
            source_bytes=source,
            block_map_bytes=block_map + b" ",
            manifest_bytes=manifest,
        )
    assert captured.value.code == "d2l_presegmented_bundle_invalid"
    assert list((root / "raw").iterdir()) == []
    assert not (root / "working" / STATE_FILENAME).exists()
    assert not (root / "working" / STATE_V2_FILENAME).exists()
    assert not (root / "working" / "source_package_captures").exists()
    assert not (root / "working" / CANDIDATE_DIRECTORY).exists()


def test_d2l_presegmented_reuse_rejects_stale_source_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = "project_d2l_stale"
    root = tmp_path / doc_id
    for name in ("raw", "canonical", "working", "logs", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    source, block_map, manifest = _d2l_upload_payloads(monkeypatch)
    imported = import_d2l_presegmented_source_package(
        root,
        doc_id,
        source_bytes=source,
        block_map_bytes=block_map,
        manifest_bytes=manifest,
    )
    state_before = (root / "working" / STATE_FILENAME).read_bytes()
    candidate_before = imported["candidate"]["tree_sha256"]
    (root / "raw" / "source.md").write_bytes(source + b"stale")

    with pytest.raises(SourceLifecycleError) as captured:
        import_d2l_presegmented_source_package(
            root,
            doc_id,
            source_bytes=source,
            block_map_bytes=block_map,
            manifest_bytes=manifest,
        )
    assert captured.value.code in {
        "source_package_source_changed",
        "source_lifecycle_stale",
    }
    assert (root / "working" / STATE_FILENAME).read_bytes() == state_before
    assert json.loads(state_before)["candidate"]["tree_sha256"] == candidate_before
