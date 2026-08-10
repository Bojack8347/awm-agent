"""Admin routes for business events / outbox."""

from __future__ import annotations

from typing import Any, Callable, Tuple

from flask import Blueprint, jsonify, request


def create_events_blueprint(
    *,
    api_key_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    """Create internal business-event inspection routes."""
    bp = Blueprint("events", __name__)

    @bp.route("/internal/admin/business-events", methods=["GET"])
    @api_key_auth_decorator
    def business_events_list() -> Tuple[Any, int]:
        try:
            limit = int(request.args.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        events = deps.db_list_business_events(
            client_id=request.args.get("client_id"),
            aggregate_type=request.args.get("aggregate_type"),
            aggregate_id=request.args.get("aggregate_id"),
            event_type=request.args.get("event_type"),
            status=request.args.get("status"),
            limit=limit,
        )
        return jsonify({"success": True, "events": events}), 200

    @bp.route("/internal/admin/business-events", methods=["POST"])
    @api_key_auth_decorator
    def business_events_create() -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        event_type = str(body.get("event_type") or "").strip()
        if not event_type:
            return jsonify({"success": False, "error": "event_type is required"}), 400
        event = deps.db_create_business_event(
            event_type=event_type,
            client_id=body.get("client_id"),
            aggregate_type=body.get("aggregate_type"),
            aggregate_id=body.get("aggregate_id"),
            event_source=body.get("event_source") or "admin_api",
            event_key=body.get("event_key"),
            payload=body.get("payload") or {},
            status=body.get("status") or "pending",
        )
        return jsonify({"success": True, "event": event}), 201

    @bp.route("/internal/admin/business-events/<event_id>", methods=["GET"])
    @api_key_auth_decorator
    def business_events_get(event_id: str) -> Tuple[Any, int]:
        event = deps.db_get_business_event(event_id)
        if not event:
            return jsonify({"success": False, "error": "business event not found"}), 404

        traces = []
        if hasattr(deps, "db_list_trace_events"):
            traces = deps.db_list_trace_events(
                trace_id=f"tr_event_{event_id}",
                limit=100,
            )
        return jsonify({"success": True, "event": event, "traces": traces}), 200

    @bp.route("/internal/admin/business-events/<event_id>/retry", methods=["POST"])
    @api_key_auth_decorator
    def business_events_retry(event_id: str) -> Tuple[Any, int]:
        event = deps.db_reset_business_event_for_retry(event_id)
        if not event:
            return jsonify({
                "success": False,
                "error": "business event not found or cannot be retried",
            }), 404
        if hasattr(deps, "db_create_trace_event"):
            deps.db_create_trace_event(
                trace_id=f"tr_event_{event_id}",
                client_id=event.get("client_id"),
                source_type="admin_api",
                source_id=event_id,
                event_type="business_event.retry_requested",
                event_name="Business event retry requested",
                actor_type="admin",
                status="success",
                payload={"business_event_id": event_id},
            )
        return jsonify({"success": True, "event": event}), 200

    return bp


__all__ = ["create_events_blueprint"]
