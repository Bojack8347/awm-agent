"""Internal trace query and append routes."""

from __future__ import annotations

from typing import Any, Callable, Tuple

from flask import Blueprint, jsonify, request


def create_trace_blueprint(
    *,
    api_key_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    bp = Blueprint("trace", __name__)

    @bp.route("/internal/admin/traces", methods=["GET"])
    @api_key_auth_decorator
    def list_traces() -> Tuple[Any, int]:
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            limit = 100
        events = deps.db_list_trace_events(
            trace_id=request.args.get("trace_id"),
            client_id=request.args.get("client_id"),
            case_type=request.args.get("case_type"),
            case_id=request.args.get("case_id"),
            subject_type=request.args.get("subject_type"),
            subject_id=request.args.get("subject_id"),
            status=request.args.get("status"),
            limit=limit,
        )
        return jsonify({"success": True, "events": events}), 200

    @bp.route("/internal/admin/traces", methods=["POST"])
    @api_key_auth_decorator
    def create_trace_event() -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        event_type = str(body.get("event_type") or "").strip()
        if not event_type:
            return jsonify({"success": False, "error": "event_type is required"}), 400
        event = deps.db_create_trace_event(
            trace_id=body.get("trace_id"),
            parent_event_id=body.get("parent_event_id"),
            client_id=body.get("client_id"),
            session_id=body.get("session_id"),
            turn_id=body.get("turn_id"),
            case_id=body.get("case_id"),
            case_type=body.get("case_type"),
            source_type=body.get("source_type"),
            source_id=body.get("source_id"),
            event_type=event_type,
            event_name=body.get("event_name"),
            actor_type=body.get("actor_type"),
            actor_id=body.get("actor_id"),
            status=str(body.get("status") or "success"),
            error_code=body.get("error_code"),
            error_message=body.get("error_message"),
            duration_ms=body.get("duration_ms"),
            agent_name=body.get("agent_name"),
            tool_name=body.get("tool_name"),
            engine_name=body.get("engine_name"),
            input_summary=body.get("input_summary"),
            output_summary=body.get("output_summary"),
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
            subjects=body.get("subjects") if isinstance(body.get("subjects"), list) else [],
        )
        if not event:
            return jsonify({"success": False, "error": "Failed to create trace event"}), 500
        return jsonify({"success": True, "event": event}), 201

    return bp


__all__ = ["create_trace_blueprint"]

