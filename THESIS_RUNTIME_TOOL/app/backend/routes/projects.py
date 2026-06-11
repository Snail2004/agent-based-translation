from flask import Blueprint, jsonify, request

from routes.common import error, ok
from services.extraction import extract_project, read_job
from services.normalize_flow import (
    apply_normalized_document,
    build_project_candidate_parts,
    import_structure_plan,
    load_agent_structure_plan,
    normalizer_paths,
    normalizer_status,
)
from services.workspace import (
    ProjectError,
    create_project_shell,
    delete_project,
    get_project_path,
    has_project,
    list_projects,
    project_file_state,
    save_source_file,
    update_project_settings,
)


bp = Blueprint("projects", __name__)


@bp.get("/health")
def health():
    return ok({"status": "ready"})


@bp.get("/projects")
def projects_index():
    try:
        projects = list_projects()
        for project in projects:
            try:
                project["normalizer"] = normalizer_status(get_project_path(project["doc_id"]))
            except ProjectError:
                project["normalizer"] = {}
        return ok(projects)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.post("/projects")
def projects_create():
    payload = request.get_json(silent=True) or {}
    doc_id = str(payload.get("doc_id") or "")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    try:
        return ok(create_project_shell(doc_id, metadata), status=201)
    except ProjectError as exc:
        return error("project_error", str(exc), 400)


@bp.get("/projects/<doc_id>")
def project_detail(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        state = project_file_state(doc_id)
        state["root"] = str(get_project_path(doc_id))
        state["normalizer"] = normalizer_status(get_project_path(doc_id))
        return ok(state)
    except ProjectError as exc:
        return error("invalid_project", str(exc), 400)


@bp.patch("/projects/<doc_id>")
def project_update(doc_id: str):
    payload = request.get_json(silent=True) or {}
    allowed = {"note"}
    unknown = sorted(set(payload) - allowed - {"user"})
    if unknown:
        return error("read_only_or_unknown_field", f"Field is not editable here: {unknown[0]}", 400)
    try:
        return ok(update_project_settings(doc_id, payload))
    except ProjectError as exc:
        return error("project_update_error", str(exc), 400)


@bp.delete("/projects/<doc_id>")
def project_delete(doc_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return ok(delete_project(doc_id, confirm_doc_id=payload.get("confirm_doc_id")))
    except ProjectError as exc:
        return error("project_delete_error", str(exc), 400)


@bp.post("/projects/<doc_id>/source")
def upload_source(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        if "file" not in request.files:
            return error("missing_file", "Upload field 'file' is required.", 400)
        overwrite = str(request.form.get("overwrite", "")).lower() in {"1", "true", "yes"}
        file = request.files["file"]
        data = file.read()
        path = save_source_file(get_project_path(doc_id), file.filename or "source.txt", data, overwrite=overwrite)
        return ok({"filename": path.name, "size": len(data), "path": str(path)}, status=201)
    except ProjectError as exc:
        return error("source_upload_error", str(exc), 400)


@bp.post("/projects/<doc_id>/extract")
def extract(doc_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        job = extract_project(
            get_project_path(doc_id),
            doc_id,
            overwrite=bool(payload.get("overwrite")),
            force=bool(payload.get("force")),
            user=str(payload.get("user") or "local"),
        )
        return ok(job, status=201)
    except ProjectError as exc:
        message = str(exc)
        if "annotations_present" in message:
            code = "annotations_present"
        elif "Confirm overwrite" in message:
            code = "confirm_overwrite_required"
        else:
            code = "extract_error"
        return error(code, str(exc), 400)


@bp.post("/projects/<doc_id>/normalize/candidate-parts")
def normalize_candidate_parts(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        candidate = dict(build_project_candidate_parts(project_path, doc_id))
        candidate["paths"] = normalizer_paths(project_path)
        candidate["normalizer"] = normalizer_status(project_path)
        return ok(candidate, status=201)
    except ProjectError as exc:
        return error("normalize_candidate_error", str(exc), 400)


@bp.get("/projects/<doc_id>/normalize/agent-plan")
def normalize_agent_plan(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(load_agent_structure_plan(get_project_path(doc_id), doc_id))
    except ProjectError as exc:
        return error("normalize_agent_plan_error", str(exc), 400)


@bp.post("/projects/<doc_id>/normalize/plan")
def normalize_plan(doc_id: str):
    payload = request.get_json(silent=True) or {}
    plan = payload.get("plan", payload)
    if not isinstance(plan, dict):
        return error("invalid_structure_plan", "StructurePlan JSON object is required.", 400)
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = import_structure_plan(get_project_path(doc_id), doc_id, plan)
        if not result.get("ok"):
            validation = result.get("validation", {"errors": [], "warnings": []})
            return jsonify({
                "ok": False,
                "data": {
                    "source_fingerprint": result.get("source_fingerprint"),
                    "validation": validation,
                },
                "errors": validation.get("errors", []),
                "warnings": validation.get("warnings", []),
            }), 400
        return ok(result, status=201)
    except ProjectError as exc:
        return error("normalize_plan_error", str(exc), 400)


@bp.post("/projects/<doc_id>/normalize/apply")
def normalize_apply(doc_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        job = apply_normalized_document(
            get_project_path(doc_id),
            doc_id,
            approved=bool(payload.get("approved")),
            overwrite=bool(payload.get("overwrite")),
            force=bool(payload.get("force")),
            user=str(payload.get("user") or "local"),
        )
        return ok(job, status=201)
    except ProjectError as exc:
        message = str(exc)
        if "annotations_present" in message:
            code = "annotations_present"
        elif "Confirm overwrite" in message or "Confirm" in message:
            code = "confirm_overwrite_required"
        else:
            code = "normalize_apply_error"
        return error(code, str(exc), 400)


@bp.get("/projects/<doc_id>/jobs/<job_id>")
def job_detail(doc_id: str, job_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(read_job(get_project_path(doc_id), job_id))
    except ProjectError as exc:
        return error("job_error", str(exc), 404)
