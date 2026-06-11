from flask import Flask

from routes.annotation import bp as annotation_bp
from routes.dataset import bp as dataset_bp
from routes.edits import bp as edits_bp
from routes.history import bp as history_bp
from routes.package import bp as package_bp
from routes.projects import bp as projects_bp
from routes.references import bp as references_bp
from routes.translation_preview import bp as translation_preview_bp
from routes.validation import bp as validation_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(projects_bp, url_prefix="/api")
    app.register_blueprint(dataset_bp, url_prefix="/api")
    app.register_blueprint(validation_bp, url_prefix="/api")
    app.register_blueprint(edits_bp, url_prefix="/api")
    app.register_blueprint(references_bp, url_prefix="/api")
    app.register_blueprint(package_bp, url_prefix="/api")
    app.register_blueprint(history_bp, url_prefix="/api")
    app.register_blueprint(annotation_bp, url_prefix="/api")
    app.register_blueprint(translation_preview_bp, url_prefix="/api")
