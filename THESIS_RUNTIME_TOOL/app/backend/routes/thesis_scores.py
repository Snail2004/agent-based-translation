from flask import Blueprint

from routes.common import error, ok
from services.thesis_readmodel import ThesisReadModelError
from services.thesis_scores import export_report_bundle, load_scores


bp = Blueprint("thesis_scores", __name__)


@bp.get("/thesis/scores/<job_id>")
def thesis_scores(job_id: str):
    try:
        return ok(load_scores(job_id))
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/scores/<job_id>/export")
def thesis_scores_export(job_id: str):
    try:
        return ok(export_report_bundle(job_id))
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)
