from flask import Blueprint, request

from routes.common import error, ok
from services.thesis_readmodel import ThesisReadModelError, list_thesis_datasets, load_thesis_dataset


bp = Blueprint("thesis_dataset", __name__)


@bp.get("/thesis/datasets")
def thesis_datasets():
    return ok(list_thesis_datasets())


@bp.get("/thesis/datasets/<job_id>")
def thesis_dataset(job_id: str):
    try:
        data = load_thesis_dataset(
            job_id,
            experiment_id=request.args.get("experiment_id") or None,
            stage=request.args.get("stage") or None,
        )
        return ok(data)
    except ThesisReadModelError as exc:
        return error(exc.code, exc.message, exc.status)
