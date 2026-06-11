from flask import Blueprint, request

from routes.common import error, ok
from services.translation_preview import (
    TranslationPreviewError,
    build_translation_input,
    import_agent_translation_preview,
    import_translation_preview,
    list_translation_previews,
    load_translation_input,
    load_translation_preview,
)
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("translation_preview", __name__)


def _project_path_or_error(doc_id: str):
    if not has_project(doc_id):
        return None, error("missing_project", "Project not found", 404)
    return get_project_path(doc_id), None


@bp.post("/projects/<doc_id>/translation-preview/runs")
def translation_preview_import(doc_id: str):
    payload = request.get_json(silent=True) or {}
    preview = payload.get("preview", payload) if isinstance(payload, dict) else payload
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        result = import_translation_preview(project_path, doc_id, preview)
        return ok(result, warnings=result.get("warnings", []), status=201)
    except TranslationPreviewError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.post("/projects/<doc_id>/translation-preview/runs/agent-output")
def translation_preview_import_agent_output(doc_id: str):
    payload = request.get_json(silent=True) or {}
    chapter_id = str(payload.get("chapter_id") or request.args.get("chapter_id") or "")
    if not chapter_id:
        return error("missing_chapter_id", "chapter_id is required.", 400)
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        result = import_agent_translation_preview(project_path, doc_id, chapter_id)
        return ok(result, warnings=result.get("warnings", []), status=201)
    except TranslationPreviewError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.get("/projects/<doc_id>/translation-preview/input")
def translation_preview_input(doc_id: str):
    chapter_id = str(request.args.get("chapter_id") or "")
    if not chapter_id:
        return error("missing_chapter_id", "chapter_id is required.", 400)
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(build_translation_input(project_path, doc_id, chapter_id))
    except TranslationPreviewError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.get("/projects/<doc_id>/translation-preview/input/saved")
def translation_preview_saved_input(doc_id: str):
    chapter_id = str(request.args.get("chapter_id") or "")
    if not chapter_id:
        return error("missing_chapter_id", "chapter_id is required.", 400)
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(load_translation_input(project_path, doc_id, chapter_id))
    except TranslationPreviewError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.get("/projects/<doc_id>/translation-preview/runs")
def translation_preview_list(doc_id: str):
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(list_translation_previews(project_path))
    except TranslationPreviewError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.get("/projects/<doc_id>/translation-preview/runs/<run_id>")
def translation_preview_load(doc_id: str, run_id: str):
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(load_translation_preview(project_path, run_id))
    except TranslationPreviewError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)
