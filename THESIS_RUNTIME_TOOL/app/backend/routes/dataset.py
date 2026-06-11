from flask import Blueprint

from routes.common import error, ok
from services.dataset_io import read_dataset
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("dataset", __name__)


@bp.get("/projects/<doc_id>/dataset")
def project_dataset(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(read_dataset(get_project_path(doc_id)))
    except ProjectError as exc:
        return error("invalid_project", str(exc), 400)
    except FileNotFoundError as exc:
        return error("missing_file", str(exc), 404)
    except ValueError as exc:
        return error("invalid_dataset", str(exc), 400)
