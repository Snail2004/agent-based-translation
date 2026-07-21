from flask import Blueprint, jsonify, request

from config import THESIS_APP_MODE
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
from services.project_runtime import (
    ProjectRuntimeError,
    get_project_runtime_status,
    prepare_project_runtime,
)
from services.source_lifecycle import (
    SOURCE_PACKAGE_REVIEW_BINDING_FIELDS,
    SourceLifecycleError,
    apply_managed_source_corrections,
    apply_managed_source_hierarchy,
    ensure_legacy_extract_allowed,
    ensure_legacy_normalizer_allowed,
    ensure_source_upload_allowed,
    finalize_managed_source_package,
    get_source_package_review,
    get_source_package_status,
    get_source_package_unit_blocks,
    normalize_managed_source_package,
    publish_managed_translation,
    source_lifecycle_mutation_guard,
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


def cockpit_quarantined(feature: str):
    if THESIS_APP_MODE == "cockpit":
        return error(
            "legacy_feature_quarantined",
            f"{feature} is quarantined in thesis cockpit mode.",
            404,
            feature=feature,
        )
    return None


@bp.get("/health")
def health():
    return ok({"status": "ready", "app_mode": THESIS_APP_MODE})


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


@bp.get("/projects/<doc_id>/runtime")
def project_runtime_status(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(get_project_runtime_status(doc_id))
    except ProjectRuntimeError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("project_runtime_error", str(exc), 400)


@bp.post("/projects/<doc_id>/runtime/prepare")
def project_runtime_prepare(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = prepare_project_runtime(doc_id)
        return ok(result, status=201 if result.get("created") else 200)
    except ProjectRuntimeError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("project_runtime_error", str(exc), 400)


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
        project_path = get_project_path(doc_id)
        if "file" not in request.files:
            return error("missing_file", "Upload field 'file' is required.", 400)
        overwrite = str(request.form.get("overwrite", "")).lower() in {"1", "true", "yes"}
        file = request.files["file"]
        data = file.read()
        with source_lifecycle_mutation_guard(project_path):
            ensure_source_upload_allowed(project_path)
            path = save_source_file(
                project_path,
                file.filename or "source.txt",
                data,
                overwrite=overwrite,
            )
        return ok({"filename": path.name, "size": len(data), "path": str(path)}, status=201)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_upload_error", str(exc), 400)


@bp.post("/projects/<doc_id>/extract")
def extract(doc_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        with source_lifecycle_mutation_guard(project_path):
            ensure_legacy_extract_allowed(project_path)
            job = extract_project(
                project_path,
                doc_id,
                overwrite=bool(payload.get("overwrite")),
                force=bool(payload.get("force")),
                user=str(payload.get("user") or "local"),
            )
        return ok(job, status=201)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        message = str(exc)
        if "annotations_present" in message:
            code = "annotations_present"
        elif "Confirm overwrite" in message:
            code = "confirm_overwrite_required"
        else:
            code = "extract_error"
        return error(code, str(exc), 400)


@bp.get("/projects/<doc_id>/source-package")
def source_package_status(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(get_source_package_status(get_project_path(doc_id), doc_id))
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_status_error", str(exc), 400)


@bp.post("/projects/<doc_id>/source-package/normalize")
def source_package_normalize(doc_id: str):
    if not request.is_json:
        return error(
            "source_package_options_invalid",
            "Request body must be exactly one empty JSON object.",
            400,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error(
            "source_package_options_invalid",
            "Request body must be exactly one empty JSON object.",
            400,
        )
    if payload:
        return error(
            "source_package_options_forbidden",
            "Source-package normalization uses server-owned configuration and accepts no client options.",
            400,
        )
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = normalize_managed_source_package(get_project_path(doc_id), doc_id)
        return ok(result, status=201 if result.get("created") else 200)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_normalize_error", str(exc), 400)


@bp.get("/projects/<doc_id>/source-package/review")
def source_package_review(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        return ok(get_source_package_review(get_project_path(doc_id), doc_id))
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_review_error", str(exc), 400)


@bp.get("/projects/<doc_id>/source-package/review/units/<unit_id>/blocks")
def source_package_review_unit_blocks(doc_id: str, unit_id: str):
    allowed_query = {
        *SOURCE_PACKAGE_REVIEW_BINDING_FIELDS,
        "offset",
        "limit",
    }
    unknown = sorted(set(request.args) - allowed_query)
    if unknown:
        return error(
            "source_package_review_query_invalid",
            f"Unsupported review query field: {unknown[0]}",
            400,
        )
    for name in SOURCE_PACKAGE_REVIEW_BINDING_FIELDS:
        values = request.args.getlist(name)
        if len(values) != 1 or not values[0].strip():
            return error(
                "source_package_review_binding_required",
                f"Review binding {name} is required exactly once.",
                400,
            )
    try:
        offset_values = request.args.getlist("offset")
        limit_values = request.args.getlist("limit")
        if len(offset_values) > 1 or len(limit_values) > 1:
            raise ValueError
        offset = int(offset_values[0]) if offset_values else 0
        limit = int(limit_values[0]) if limit_values else 200
    except ValueError:
        return error(
            "source_package_review_pagination_invalid",
            "offset and limit must be decimal integers.",
            400,
        )
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        expected = {
            name: request.args[name]
            for name in SOURCE_PACKAGE_REVIEW_BINDING_FIELDS
        }
        return ok(
            get_source_package_unit_blocks(
                get_project_path(doc_id),
                doc_id,
                unit_id,
                expected=expected,
                offset=offset,
                limit=limit,
            )
        )
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_review_error", str(exc), 400)


@bp.post("/projects/<doc_id>/source-package/corrections")
def source_package_corrections(doc_id: str):
    if not request.is_json:
        return error(
            "source_package_correction_invalid",
            "Correction request must be a JSON object.",
            400,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error(
            "source_package_correction_invalid",
            "Correction request must be a JSON object.",
            400,
        )
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = apply_managed_source_corrections(
            get_project_path(doc_id),
            doc_id,
            payload,
        )
        return ok(result, status=201 if result.get("decision_created") else 200)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_correction_error", str(exc), 400)


@bp.post("/projects/<doc_id>/source-package/hierarchy")
def source_package_hierarchy(doc_id: str):
    if not request.is_json:
        return error(
            "source_package_hierarchy_invalid",
            "Hierarchy request must be a JSON object.",
            400,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error(
            "source_package_hierarchy_invalid",
            "Hierarchy request must be a JSON object.",
            400,
        )
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = apply_managed_source_hierarchy(
            get_project_path(doc_id), doc_id, payload
        )
        return ok(result, status=201 if result.get("decision_created") else 200)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_hierarchy_error", str(exc), 400)


@bp.post("/projects/<doc_id>/source-package/finalize")
def source_package_finalize(doc_id: str):
    if not request.is_json:
        return error(
            "source_package_finalization_invalid",
            "Finalization request must be a JSON object.",
            400,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error(
            "source_package_finalization_invalid",
            "Finalization request must be a JSON object.",
            400,
        )
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = finalize_managed_source_package(
            get_project_path(doc_id), doc_id, payload
        )
        return ok(result, status=201 if result.get("decision_created") else 200)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_finalization_error", str(exc), 400)


@bp.post("/projects/<doc_id>/source-package/publications")
def source_package_publish(doc_id: str):
    if not request.is_json:
        return error(
            "source_package_publication_invalid",
            "Publication request must be a canonical translation overlay JSON object.",
            400,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error(
            "source_package_publication_invalid",
            "Publication request must be a canonical translation overlay JSON object.",
            400,
        )
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        result = publish_managed_translation(
            get_project_path(doc_id), doc_id, payload
        )
        return ok(result, status=201 if result.get("created") else 200)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("source_package_publication_error", str(exc), 400)


@bp.post("/projects/<doc_id>/normalize/candidate-parts")
def normalize_candidate_parts(doc_id: str):
    blocked = cockpit_quarantined("structure_normalizer")
    if blocked:
        return blocked
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        with source_lifecycle_mutation_guard(project_path):
            ensure_legacy_normalizer_allowed(project_path)
            candidate = dict(build_project_candidate_parts(project_path, doc_id))
            candidate["paths"] = normalizer_paths(project_path)
            candidate["normalizer"] = normalizer_status(project_path)
        return ok(candidate, status=201)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("normalize_candidate_error", str(exc), 400)


@bp.get("/projects/<doc_id>/normalize/agent-plan")
def normalize_agent_plan(doc_id: str):
    blocked = cockpit_quarantined("structure_normalizer")
    if blocked:
        return blocked
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        with source_lifecycle_mutation_guard(project_path):
            ensure_legacy_normalizer_allowed(project_path)
            result = load_agent_structure_plan(project_path, doc_id)
        return ok(result)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("normalize_agent_plan_error", str(exc), 400)


@bp.post("/projects/<doc_id>/normalize/plan")
def normalize_plan(doc_id: str):
    blocked = cockpit_quarantined("structure_normalizer")
    if blocked:
        return blocked
    payload = request.get_json(silent=True) or {}
    plan = payload.get("plan", payload)
    if not isinstance(plan, dict):
        return error("invalid_structure_plan", "StructurePlan JSON object is required.", 400)
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        with source_lifecycle_mutation_guard(project_path):
            ensure_legacy_normalizer_allowed(project_path)
            result = import_structure_plan(project_path, doc_id, plan)
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
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("normalize_plan_error", str(exc), 400)


@bp.post("/projects/<doc_id>/normalize/apply")
def normalize_apply(doc_id: str):
    blocked = cockpit_quarantined("structure_normalizer")
    if blocked:
        return blocked
    payload = request.get_json(silent=True) or {}
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        with source_lifecycle_mutation_guard(project_path):
            ensure_legacy_normalizer_allowed(project_path)
            job = apply_normalized_document(
                project_path,
                doc_id,
                approved=bool(payload.get("approved")),
                overwrite=bool(payload.get("overwrite")),
                force=bool(payload.get("force")),
                user=str(payload.get("user") or "local"),
            )
        return ok(job, status=201)
    except SourceLifecycleError as exc:
        return error(exc.code, str(exc), exc.status)
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
