from flask import Blueprint, jsonify, request

from routes.common import error, ok
from services.annotation_flow import (
    AnnotationError,
    apply_annotation_candidate,
    build_annotation_input,
    load_annotation_candidate,
    resolve_annotation_candidate,
)
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("annotation", __name__)


def _project_path_or_error(doc_id: str):
    if not has_project(doc_id):
        return None, error("missing_project", "Project not found", 404)
    return get_project_path(doc_id), None


@bp.post("/projects/<doc_id>/annotate/input")
def annotation_input(doc_id: str):
    payload = request.get_json(silent=True) or {}
    chapter_id = str(payload.get("chapter_id") or "")
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(build_annotation_input(project_path, doc_id, chapter_id), status=201)
    except AnnotationError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.get("/projects/<doc_id>/annotate/candidate")
def annotation_candidate(doc_id: str):
    chapter_id = str(request.args.get("chapter_id") or "")
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(load_annotation_candidate(project_path, doc_id, chapter_id))
    except AnnotationError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.post("/projects/<doc_id>/annotate/resolve")
def annotation_resolve(doc_id: str):
    payload = request.get_json(silent=True) or {}
    candidate = payload.get("candidate", payload)
    user = str(payload.get("user") or "local") if isinstance(payload, dict) else "local"
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(resolve_annotation_candidate(project_path, doc_id, candidate, user=user), status=201)
    except AnnotationError as exc:
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.post("/projects/<doc_id>/annotate/apply")
def annotation_apply(doc_id: str):
    payload = request.get_json(silent=True) or {}
    user = str(payload.get("user") or "local")
    try:
        project_path, failure = _project_path_or_error(doc_id)
        if failure:
            return failure
        return ok(apply_annotation_candidate(project_path, doc_id, payload, user=user), status=201)
    except AnnotationError as exc:
        if exc.code == "validation_failed":
            return jsonify({
                "ok": False,
                "data": None,
                "errors": exc.details.get("validation", {}).get("errors", [{"code": exc.code, "message": str(exc)}]),
                "warnings": exc.details.get("validation", {}).get("warnings", []),
            }), exc.status
        return error(exc.code, str(exc), exc.status, **exc.details)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)
