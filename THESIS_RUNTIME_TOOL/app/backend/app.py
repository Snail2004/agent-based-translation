import subprocess

from flask import Flask, jsonify, request

from config import HANDOFF_ROOT, HOST, PORT
from routes import register_blueprints
from services.workspace import ensure_seed_project


def read_app_version() -> str:
    try:
        return (HANDOFF_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def read_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=HANDOFF_ROOT,
            text=True,
            capture_output=True,
            timeout=2,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def create_app() -> Flask:
    app = Flask(__name__)
    ensure_seed_project()
    register_blueprints(app)

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
        return response

    @app.before_request
    def handle_options():
        if request.method == "OPTIONS":
            return jsonify({"ok": True, "data": {}, "errors": [], "warnings": []})
        return None

    @app.get("/api/version")
    def version():
        app_version = read_app_version()
        return jsonify({
            "ok": True,
            "data": {
                "version": app_version,
                "backend_version": app_version,
                "git_sha": read_git_sha(),
                "event_schema": "one_button_event_v1",
            },
            "errors": [],
            "warnings": [],
        })

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
