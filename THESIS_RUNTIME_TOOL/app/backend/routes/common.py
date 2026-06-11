from typing import Any

from flask import jsonify


def ok(data: Any = None, warnings: list[dict[str, Any]] | None = None, status: int = 200):
    return jsonify({
        "ok": True,
        "data": data if data is not None else {},
        "errors": [],
        "warnings": warnings or [],
    }), status


def error(code: str, message: str, status: int = 400, **extra):
    item = {
        "code": code,
        "message": message,
        "file": extra.pop("file", None),
        "location": extra.pop("location", None),
    }
    item.update(extra)
    return jsonify({
        "ok": False,
        "data": None,
        "errors": [item],
        "warnings": [],
    }), status
