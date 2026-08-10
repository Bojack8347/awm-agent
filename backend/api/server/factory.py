"""Flask app factory and blueprint registration."""

from __future__ import annotations

import logging
import sys
from typing import Any

from flask import Flask, make_response, request

from .registration import register_blueprints

logger = logging.getLogger("awm.api")


def _server_deps() -> Any:
    package_name = __package__
    return sys.modules.get(package_name) or sys.modules.get("server") or sys.modules[__name__]


def create_app() -> Flask:
    app = Flask(__name__)
    _install_cors(app)
    register_blueprints(app, _server_deps())
    return app


def _install_cors(app: Flask) -> None:
    """Allow the local Expo UI to call the API during MVP web testing."""

    @app.before_request
    def _cors_preflight() -> Any:
        if request.method == "OPTIONS":
            return make_response("", 204)
        return None

    @app.after_request
    def _cors_headers(response: Any) -> Any:
        origin = request.headers.get("Origin", "")
        if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Api-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        return response


app = create_app()
