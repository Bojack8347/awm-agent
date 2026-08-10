"""Unified client state routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify


def create_client_state_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    bp = Blueprint("client_state", __name__)

    @bp.route("/api/v1/client-state", methods=["GET"])
    @user_auth_decorator
    def get_my_client_state(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        build_client_file = getattr(deps, "build_client_state_view", None)
        if not callable(build_client_file):
            return jsonify({
                "success": False,
                "error": "client_file_reader_unavailable",
            }), 503
        try:
            client_file = build_client_file(client_id)
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[client-state] client_file failed: {exc}", flush=True)
            return jsonify({
                "success": False,
                "error": "client_file_read_failed",
            }), 500
        return jsonify({
            "success": True,
            "client_file": client_file,
        }), 200

    return bp


__all__ = ["create_client_state_blueprint"]
