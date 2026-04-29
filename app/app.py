"""Flask application factory."""

import os
import logging
from flask import Flask
from app.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def create_app(config_class=None):
    """Application factory."""
    app = Flask(__name__)
    app.config.from_object(config_class or Config)

    # Ensure report directory exists
    os.makedirs(app.config["REPORT_DIR"], exist_ok=True)

    # Register blueprints
    from app.routes import web_bp
    app.register_blueprint(web_bp)

    from app.api import api_bp, init_registries
    app.register_blueprint(api_bp, url_prefix="/api")

    # Load persisted registry credentials from disk
    with app.app_context():
        init_registries()

    # Cache-Control for static files
    @app.after_request
    def add_cache_headers(response):
        from flask import request
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    return app
