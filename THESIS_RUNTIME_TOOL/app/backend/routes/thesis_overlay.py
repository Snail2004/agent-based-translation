from flask import Blueprint, request

from routes.common import error, ok
from services.thesis_overlay import load_registry_overlay
from services.thesis_readmodel import ThesisReadModelError


bp = Blueprint("thesis_overlay", __name__)


@bp.get("/thesis/overlay/<job_id>")
def thesis_overlay(job_id: str):
    try:
        return ok(load_registry_overlay(
            job_id,
            experiment_id=request.args.get("experiment_id") or None,
            stage=request.args.get("stage") or None,
            block_id=request.args.get("block_id") or None,
            chapter_id=request.args.get("chapter_id") or None,
        ))
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)
