from flask import Blueprint, request

from routes.common import error, ok
from services.history import record_history
from services.mutations import MutationError
from services.references import lock_reference, review_reference, save_reference_draft
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("references", __name__)


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _user(payload: dict) -> str:
    return str(payload.get("user") or "local")


def _project(doc_id: str):
    if not has_project(doc_id):
        raise MutationError("missing_project", "Project not found", 404)
    return get_project_path(doc_id)


def _history(project_path, *, action: str, label: str, target: dict, user: str, operation):
    return record_history(project_path, action=action, label=label, target=target, user=user, operation=operation)


def _handle(exc: Exception):
    if isinstance(exc, MutationError):
        return error(exc.code, str(exc), exc.status, **getattr(exc, "details", {}))
    if isinstance(exc, ProjectError):
        return error("project_error", str(exc), 400)
    raise exc


@bp.post("/projects/<doc_id>/references/draft")
def create_reference_draft(doc_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        block_id = str(payload.get("block_id") or "")
        reference_id = str(payload.get("reference_id") or "")
        return ok(_history(
            project_path,
            action="reference_draft",
            label=f"Save reference draft {reference_id or block_id}",
            target={"doc_id": doc_id, "reference_id": reference_id, "block_id": block_id},
            user=user,
            operation=lambda: save_reference_draft(project_path, payload, user),
        ), status=201)
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/references/<reference_id>/review")
def review_reference_route(doc_id: str, reference_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="reference_review",
            label=f"Review reference {reference_id}",
            target={"doc_id": doc_id, "reference_id": reference_id},
            user=user,
            operation=lambda: review_reference(project_path, reference_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/references/<reference_id>/lock")
def lock_reference_route(doc_id: str, reference_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="reference_lock",
            label=f"Lock reference {reference_id}",
            target={"doc_id": doc_id, "reference_id": reference_id},
            user=user,
            operation=lambda: lock_reference(project_path, reference_id, user),
        ))
    except Exception as exc:
        return _handle(exc)
