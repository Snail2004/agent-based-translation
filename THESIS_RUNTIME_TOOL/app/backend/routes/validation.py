from flask import Blueprint, request

from routes.common import error, ok
from services.audit_log import log_event
from services.history import record_history
from services.schema_migration import SchemaMigrationError, migrate_project_to_schema_1_5
from services.validator import run_validator
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("validation", __name__)


@bp.post("/projects/<doc_id>/validate")
def validate_project(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        report = run_validator(project_path / "canonical")
        user = (request.get_json(silent=True) or {}).get("user", "local")
        log_event(project_path, "validate", {"ok": report.get("ok"), "exit_code": report.get("exit_code")}, user)
        return ok(report)
    except ProjectError as exc:
        return error("invalid_project", str(exc), 400)


@bp.post("/projects/<doc_id>/migrate-schema")
def migrate_schema_project(doc_id: str):
    try:
        if not has_project(doc_id):
            return error("missing_project", "Project not found", 404)
        project_path = get_project_path(doc_id)
        user = (request.get_json(silent=True) or {}).get("user", "local")
        result = record_history(
            project_path,
            action="migrate_schema",
            label="Migrate schema to 1.5.0",
            target={"doc_id": doc_id},
            user=user,
            operation=lambda: migrate_project_to_schema_1_5(project_path),
        )
        log_event(project_path, "migrate_schema", {
            "schema_version_before": result.get("schema_version_before"),
            "schema_version_after": result.get("schema_version_after"),
            "validator_ok": result.get("validation", {}).get("ok"),
        }, user)
        return ok(result)
    except SchemaMigrationError as exc:
        return error(exc.code, str(exc), exc.status)
    except ProjectError as exc:
        return error("invalid_project", str(exc), 400)
