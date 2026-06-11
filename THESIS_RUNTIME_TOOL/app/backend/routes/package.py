from pathlib import Path

from flask import Blueprint, request, send_file

from routes.common import error, ok
from services.package import export_project, export_project_with_previews, freeze_project
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("package", __name__)


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _user(payload: dict) -> str:
    return str(payload.get("user") or "local")


@bp.post("/projects/<doc_id>/export")
def export_route(doc_id: str):
    payload = _payload()
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(export_project(get_project_path(doc_id), _user(payload)), status=201)
    except ProjectError as exc:
        return error("export_error", str(exc), 400)


@bp.post("/projects/<doc_id>/export-with-previews")
def export_with_previews_route(doc_id: str):
    payload = _payload()
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(export_project_with_previews(get_project_path(doc_id), _user(payload)), status=201)
    except ProjectError as exc:
        return error("export_error", str(exc), 400)


@bp.get("/projects/<doc_id>/exports/<path:filename>")
def export_download_route(doc_id: str, filename: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name.endswith(".zip"):
            return error("invalid_export_name", "Invalid export filename.", 400)
        path = get_project_path(doc_id) / "exports" / safe_name
        if not path.exists():
            return error("missing_export", "Export file not found.", 404)
        return send_file(path, as_attachment=True, download_name=safe_name, mimetype="application/zip")
    except ProjectError as exc:
        return error("export_error", str(exc), 400)


@bp.post("/projects/<doc_id>/freeze")
def freeze_route(doc_id: str):
    payload = _payload()
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = freeze_project(get_project_path(doc_id), _user(payload))
        if not result.get("frozen"):
            return error("freeze_blocked", "Freeze blocked", 409, reasons=result.get("reasons", []), validation=result.get("validation"))
        return ok(result, status=201)
    except ProjectError as exc:
        return error("freeze_error", str(exc), 400)
