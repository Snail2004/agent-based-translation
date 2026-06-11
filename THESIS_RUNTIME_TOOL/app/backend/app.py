from flask import Flask, jsonify, request

from config import HOST, PORT
from routes import register_blueprints
from services.workspace import ensure_seed_project


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

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
