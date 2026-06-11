from flask import Blueprint, request

from routes.common import error, ok
from services.history import record_history
from services.mutations import (
    MutationError,
    add_entity_from_selection,
    add_glossary_from_selection,
    create_entity_relation,
    delete_entity,
    delete_entity_relation,
    delete_glossary,
    patch_block,
    patch_block_notes,
    patch_block_review,
    patch_entity,
    patch_entity_relation,
    patch_glossary,
    patch_metadata,
    patch_summary,
)
from services.workspace import ProjectError, get_project_path, has_project


bp = Blueprint("edits", __name__)


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
        return error("invalid_project", str(exc), 400)
    if isinstance(exc, FileNotFoundError):
        return error("missing_file", str(exc), 404)
    if isinstance(exc, ValueError):
        return error("invalid_request", str(exc), 400)
    raise exc


@bp.patch("/projects/<doc_id>/metadata")
def update_metadata(doc_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="patch_metadata",
            label="Edit metadata",
            target={"doc_id": doc_id, "fields": sorted(set(payload) & {"title", "author", "domain", "genre", "source_format", "license", "source_url", "contamination_risk"})},
            user=user,
            operation=lambda: patch_metadata(project_path, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/blocks/<block_id>")
def update_block(doc_id: str, block_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        fields = sorted(set(payload) & {"clean_text", "block_type", "is_chapter_opening", "quality_flags", "discourse"})
        return ok(_history(
            project_path,
            action="patch_block",
            label=f"Edit {', '.join(fields) or 'block'} on {block_id}",
            target={"doc_id": doc_id, "block_id": block_id, "fields": fields},
            user=user,
            operation=lambda: patch_block(project_path, block_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/blocks/<block_id>/notes")
def update_block_notes(doc_id: str, block_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        fields = sorted(set(payload) & {"motifs", "tone", "implicit_meaning", "narrative_note"})
        return ok(_history(
            project_path,
            action="patch_block_notes",
            label=f"Edit narrative notes on {block_id}",
            target={"doc_id": doc_id, "block_id": block_id, "fields": fields},
            user=user,
            operation=lambda: patch_block_notes(project_path, block_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/review/blocks/<block_id>")
def update_block_review(doc_id: str, block_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        reviewed = bool(payload.get("reviewed"))
        return ok(_history(
            project_path,
            action="patch_block_review",
            label=("Mark reviewed " if reviewed else "Mark unreviewed ") + block_id,
            target={"doc_id": doc_id, "block_id": block_id},
            user=user,
            operation=lambda: patch_block_review(project_path, block_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/glossary/from-selection")
def create_glossary_from_selection(doc_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        block_id = str(payload.get("block_id") or "")
        return ok(_history(
            project_path,
            action="add_glossary_from_selection",
            label=f"Add glossary term on {block_id}",
            target={"doc_id": doc_id, "block_id": block_id, "kind": "glossary"},
            user=user,
            operation=lambda: add_glossary_from_selection(project_path, payload, user),
        ), status=201)
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/glossary/<term_id>")
def update_glossary(doc_id: str, term_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        fields = sorted(set(payload) & {"source_term", "expected_target", "allowed_variants", "forbidden_variants", "status", "domain", "chapter_scope", "annotated_by", "confidence"})
        return ok(_history(
            project_path,
            action="patch_glossary",
            label=f"Edit glossary {term_id}",
            target={"doc_id": doc_id, "term_id": term_id, "fields": fields},
            user=user,
            operation=lambda: patch_glossary(project_path, term_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.delete("/projects/<doc_id>/glossary/<term_id>")
def remove_glossary(doc_id: str, term_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="delete_glossary",
            label=f"Delete glossary {term_id}",
            target={"doc_id": doc_id, "term_id": term_id},
            user=user,
            operation=lambda: delete_glossary(project_path, term_id, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/entities/from-selection")
def create_entity_from_selection(doc_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        block_id = str(payload.get("block_id") or "")
        return ok(_history(
            project_path,
            action="add_entity_from_selection",
            label=f"Add entity mention on {block_id}",
            target={"doc_id": doc_id, "block_id": block_id, "kind": "entity"},
            user=user,
            operation=lambda: add_entity_from_selection(project_path, payload, user),
        ), status=201)
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/entities/<entity_id>")
def update_entity(doc_id: str, entity_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        fields = sorted(set(payload) & {"canonical_source", "canonical_target", "aliases_source", "aliases_target", "pronoun_policy", "gender", "entity_type", "annotated_by", "confidence"})
        return ok(_history(
            project_path,
            action="patch_entity",
            label=f"Edit entity {entity_id}",
            target={"doc_id": doc_id, "entity_id": entity_id, "fields": fields},
            user=user,
            operation=lambda: patch_entity(project_path, entity_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.delete("/projects/<doc_id>/entities/<entity_id>")
def remove_entity(doc_id: str, entity_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="delete_entity",
            label=f"Delete entity {entity_id}",
            target={"doc_id": doc_id, "entity_id": entity_id},
            user=user,
            operation=lambda: delete_entity(project_path, entity_id, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.post("/projects/<doc_id>/relations")
def create_relation(doc_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="create_entity_relation",
            label="Add entity relation",
            target={"doc_id": doc_id, "kind": "entity_relation"},
            user=user,
            operation=lambda: create_entity_relation(project_path, payload, user),
        ), status=201)
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/relations/<relation_id>")
def update_relation(doc_id: str, relation_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        fields = sorted(set(payload) & {
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            "state_label",
            "valid_from_block_id",
            "valid_to_block_id",
            "trigger_event_id",
            "address_policy",
            "evidence",
            "confidence",
            "notes",
        })
        return ok(_history(
            project_path,
            action="patch_entity_relation",
            label=f"Edit relation {relation_id}",
            target={"doc_id": doc_id, "relation_id": relation_id, "fields": fields},
            user=user,
            operation=lambda: patch_entity_relation(project_path, relation_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.delete("/projects/<doc_id>/relations/<relation_id>")
def remove_relation(doc_id: str, relation_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        return ok(_history(
            project_path,
            action="delete_entity_relation",
            label=f"Delete relation {relation_id}",
            target={"doc_id": doc_id, "relation_id": relation_id},
            user=user,
            operation=lambda: delete_entity_relation(project_path, relation_id, user),
        ))
    except Exception as exc:
        return _handle(exc)


@bp.patch("/projects/<doc_id>/summary/<chapter_id>")
def update_summary(doc_id: str, chapter_id: str):
    payload = _payload()
    try:
        project_path = _project(doc_id)
        user = _user(payload)
        fields = sorted(set(payload) & {"summary_source", "source", "characters_present", "key_events", "setting", "emotional_tone", "motifs", "summary_target", "open_threads", "translation_notes", "confidence"})
        return ok(_history(
            project_path,
            action="patch_summary",
            label=f"Edit summary {chapter_id}",
            target={"doc_id": doc_id, "chapter_id": chapter_id, "fields": fields},
            user=user,
            operation=lambda: patch_summary(project_path, chapter_id, payload, user),
        ))
    except Exception as exc:
        return _handle(exc)
