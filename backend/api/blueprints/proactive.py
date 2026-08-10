"""Proactive companion HTTP routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify, request


def create_proactive_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    proactive_service_factory: Callable[[], Any],
    queued_outbound_messages_factory: Callable[[str], Any],
    mark_outbound_message_delivered: Callable[[str], bool],
) -> Blueprint:
    """Create proactive companion routes with app-level dependency hooks."""
    bp = Blueprint("proactive", __name__)

    @bp.route("/api/v1/companion/proactive/evaluate", methods=["POST"])
    @user_auth_decorator
    def proactive_evaluate_self(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        session_id = body.get("session_id")
        if session_id is not None:
            session_id = str(session_id).strip() or None

        result = proactive_service_factory().evaluate_and_compose(
            client_id,
            session_id=session_id,
        )
        http_status = 200 if result.error is None else 500
        return jsonify({
            "success": result.error is None,
            "result": result.to_dict(),
        }), http_status

    @bp.route("/api/v1/companion/proactive/queue", methods=["GET"])
    @user_auth_decorator
    def proactive_get_queue(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        messages = queued_outbound_messages_factory(client_id)
        return jsonify({
            "success": True,
            "messages": messages,
        }), 200

    @bp.route("/api/v1/companion/proactive/queue/<message_id>/delivered", methods=["POST"])
    @user_auth_decorator
    def proactive_mark_delivered(message_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        ok = mark_outbound_message_delivered(message_id)
        if not ok:
            return jsonify({
                "success": False,
                "error": "Message not found or already delivered",
            }), 404
        return jsonify({"success": True}), 200

    return bp


__all__ = ["create_proactive_blueprint"]
