import os
from pathlib import Path


HANDOFF_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = HANDOFF_ROOT / "app"
BACKEND_ROOT = APP_ROOT / "backend"

DATASET_SPEC_ROOT = HANDOFF_ROOT / "dataset_spec"
SCHEMA_DIR = DATASET_SPEC_ROOT / "schema"
VALIDATOR_SCRIPT = DATASET_SPEC_ROOT / "tools" / "validate.py"
SAMPLE_ROOT = DATASET_SPEC_ROOT / "sample"
TEMPLATE_ROOT = DATASET_SPEC_ROOT / "templates"
TRANSLATION_REVIEW_TEMPLATE = TEMPLATE_ROOT / "translation_review_log.csv"

PROJECTS_ROOT = Path(os.environ.get("THESIS_TOOL_PROJECTS_ROOT", HANDOFF_ROOT / "projects")).resolve()
THESIS_JOBS_ROOT = Path(os.environ.get("THESIS_JOBS_ROOT", HANDOFF_ROOT / "data" / "jobs")).resolve()
THESIS_REPORTS_ROOT = Path(os.environ.get("THESIS_REPORTS_ROOT", HANDOFF_ROOT / "data" / "reports")).resolve()
THESIS_TOOL_ROOT = Path(os.environ.get("THESIS_TOOL_ROOT", HANDOFF_ROOT)).resolve()
THESIS_PYTHON_EXE = os.environ.get("THESIS_PYTHON_EXE", "").strip()
THESIS_APP_MODE = os.environ.get("THESIS_APP_MODE", "legacy").strip().lower()
HOST = os.environ.get("AILAB_BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("AILAB_BACKEND_PORT", "5000"))

DATASET_FILES = {
    "document": "document.json",
    "glossary": "glossary.jsonl",
    "entities": "entities.jsonl",
    "chapter_summaries": "chapter_summaries.jsonl",
    "manual_reference_subset": "manual_reference_subset.jsonl",
    "entity_relations": "entity_relations.jsonl",
}

PROJECT_SUBDIRS = ("raw", "canonical", "working", "logs", "exports")
ALLOWED_SOURCE_EXTENSIONS = {".txt", ".epub"}
SCHEMA_VERSION = "1.5.0"
PIPELINE_VERSION = "0.3.3"
EXTRACTION_TOOL = "ailab-backend-extractor"
