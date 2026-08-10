"""Health HTTP routes."""

from __future__ import annotations

from typing import Any, Callable, Dict

from flask import Blueprint, jsonify


def create_health_blueprint(
    *,
    task_profiles_factory: Callable[[], Dict[str, Any]],
) -> Blueprint:
    """Create health routes with dependencies supplied by ``api.server``."""
    bp = Blueprint("health", __name__)

    @bp.route("/health", methods=["GET"])
    def health() -> Any:
        stage_rows: Dict[str, Dict[str, Any]] = {
            profile.stage_name: profile.as_dict()
            for profile in task_profiles_factory().values()
        }
        return jsonify(
            {
                "status": "healthy",
                "service": "awm-api",
                "llm_stages": stage_rows,
            }
        ), 200

    return bp


__all__ = ["create_health_blueprint"]
