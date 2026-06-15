from flask import Blueprint

from routes.common import error, ok
from services.thesis_observability import (
    load_call_detail,
    load_observability,
    load_observability_calls,
)
from services.thesis_readmodel import ThesisReadModelError


bp = Blueprint("thesis_observability", __name__)


@bp.get("/thesis/observability/<job_id>")
def thesis_observability(job_id: str):
    try:
        return ok(load_observability(job_id))
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/observability/<job_id>/calls")
def thesis_observability_calls(job_id: str):
    try:
        return ok(load_observability_calls(job_id))
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/observability/<job_id>/calls/<path:call_id>")
def thesis_observability_call_detail(job_id: str, call_id: str):
    try:
        return ok(load_call_detail(job_id, call_id))
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)
