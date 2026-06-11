from flask import Blueprint, request

from routes.common import error, ok
from services.history import HistoryError, history_state, redo, undo
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("history", __name__)


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _user(payload: dict) -> str:
    return str(payload.get("user") or "local")


def _project(doc_id: str):
    if not has_project(doc_id):
        raise HistoryError("missing_project", "Project not found", 404)
    return get_project_path(doc_id)


def _handle(exc: Exception):
    if isinstance(exc, HistoryError):
        return error(exc.code, str(exc), exc.status)
    if isinstance(exc, ProjectError):
        return error("project_error", str(exc), 400)
    raise exc


@bp.get("/projects/<doc_id>/history")
def get_history(doc_id: str):
    try:
        return ok(history_state(_project(doc_id)))
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/undo")
def undo_route(doc_id: str):
    payload = _payload()
    try:
        return ok(undo(_project(doc_id), _user(payload)))
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/redo")
def redo_route(doc_id: str):
    payload = _payload()
    try:
        return ok(redo(_project(doc_id), _user(payload)))
    except Exception as exc:
        return _handle(exc)
