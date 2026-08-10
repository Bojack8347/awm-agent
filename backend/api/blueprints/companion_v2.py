"""AWM versioned Companion HTTP adapter."""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify, request

from api.services.companion_actions import parse_client_action
from api.services.companion_turn import (
    CompanionTurnRequest,
    CompanionTurnService,
)


def create_companion_v2_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    turn_service: CompanionTurnService,
) -> Blueprint:
    """Create the V2 response contract over the canonical turn service."""

    bp = Blueprint("companion_v2", __name__)

    @bp.route("/api/v2/companion/turn", methods=["POST"])
    @user_auth_decorator
    def companion_v2_turn(
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        message = str(
            body.get("message") or body.get("user_message") or ""
        ).strip()
        session_id = (
            str(body.get("session_id") or "").strip() or str(uuid.uuid4())
        )
        turn_type = str(
            body.get("turn_type")
            or ("user_message" if message else "app_entry")
        ).strip()
        channel = str(body.get("channel") or "text").strip()
        client_action = parse_client_action(body.get("client_action"))
        if body.get("client_action") is not None and client_action is None:
            return jsonify({"success": False, "error": "invalid client_action"}), 400
        if turn_type not in {"app_entry", "user_message"}:
            return jsonify(
                {
                    "success": False,
                    "error": "turn_type must be app_entry or user_message",
                }
            ), 400
        if channel not in {"text", "voice"}:
            return jsonify(
                {
                    "success": False,
                    "error": "channel must be text or voice",
                }
            ), 400
        try:
            outcome = turn_service.run_turn(
                CompanionTurnRequest(
                    client_id=auth_session["client_id"],
                    session_id=session_id,
                    user_message=message,
                    turn_type=turn_type,
                    channel=channel,
                    client_action=client_action,
                    persist=True,
                    trace_source_type="advisor_text",
                )
            )
        except Exception as exc:  # pylint: disable=broad-except
            return jsonify(
                {
                    "success": False,
                    "error": "advisor_runtime_failed",
                    "detail": str(exc),
                    "session_id": session_id,
                }
            ), 500

        payload = outcome.canonical_payload()
        payload["response"] = outcome.assistant_message
        return jsonify(payload), outcome.http_status

    return bp


__all__ = ["create_companion_v2_blueprint"]
