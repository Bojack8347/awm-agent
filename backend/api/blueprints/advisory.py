"""Advisory event HTTP routes.

These endpoints provide the first RM/system trigger surface for the new
advisory lifecycle. They intentionally keep the contract small: create an
event, list open events for the signed-in user, and mark an event handled.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify, request
from api.services.events import safe_publish_event


def create_advisory_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    api_key_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    """Create advisory routes with app-level dependency hooks."""
    bp = Blueprint("advisory", __name__)

    @bp.route("/api/v1/advisory/events", methods=["GET"])
    @user_auth_decorator
    def list_my_advisory_events(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        status = request.args.get("status", "open")
        if status == "all":
            status = None
        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            limit = 20
        events = deps.db_get_advisory_events(
            client_id=auth_session["client_id"],
            status=status,
            limit=limit,
        )
        return jsonify({"success": True, "events": events}), 200

    @bp.route("/api/v1/advisory/plans", methods=["GET"])
    @user_auth_decorator
    def list_my_advisory_plans(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        status = request.args.get("status")
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            limit = 50
        plans = deps.db_list_advisory_plans(
            client_id=auth_session["client_id"],
            status=status,
            limit=limit,
        )
        return jsonify({"success": True, "plans": plans}), 200

    @bp.route("/api/v1/advisory/plans/<plan_id>", methods=["GET"])
    @user_auth_decorator
    def get_my_advisory_plan(plan_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        plan = deps.db_get_advisory_plan(
            advisory_plan_id=plan_id,
            client_id=auth_session["client_id"],
        )
        if not plan:
            return jsonify({"success": False, "error": "Plan not found"}), 404
        return jsonify({"success": True, "plan": plan}), 200

    @bp.route("/api/v1/advisory/plans/<plan_id>/artifacts", methods=["GET"])
    @user_auth_decorator
    def list_my_advisory_artifacts(plan_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        artifacts = deps.db_list_advisory_artifacts(
            advisory_plan_id=plan_id,
            client_id=auth_session["client_id"],
        )
        return jsonify({"success": True, "artifacts": artifacts}), 200

    @bp.route("/api/v1/advisory/plans/<plan_id>/decisions", methods=["POST"])
    @user_auth_decorator
    def record_my_advisory_decision(plan_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        decision = str(body.get("decision") or "").strip()
        if not decision:
            return jsonify({"success": False, "error": "decision is required"}), 400
        record = deps.db_record_advisory_decision(
            advisory_plan_id=plan_id,
            client_id=auth_session["client_id"],
            decision=decision,
            reason=body.get("reason"),
            reason_code=body.get("reason_code"),
            free_text_reason=body.get("free_text_reason"),
            revisit_at=body.get("revisit_at"),
            source_type=str(body.get("source_type") or "mobile"),
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
        )
        if not record:
            return jsonify({"success": False, "error": "Plan not found"}), 404
        _trace_if_available(
            deps,
            client_id=auth_session["client_id"],
            case_id=plan_id,
            case_type="advisory_plan",
            event_type="advisory_decision.recorded",
            output_summary=f"decision={decision}",
            subjects=[
                {"subject_type": "advisory_plan", "subject_id": plan_id, "relation": "updated"},
                {"subject_type": "advisory_decision", "subject_id": record["id"], "relation": "created"},
            ],
        )
        safe_publish_event(
            deps,
            event_key=f"advisory.decision_recorded:{record['id']}",
            client_id=auth_session["client_id"],
            aggregate_type="advisory_plan",
            aggregate_id=plan_id,
            event_type="advisory.decision_recorded",
            event_source="mobile",
            payload={
                "advisory_plan_id": plan_id,
                "advisory_decision_id": record["id"],
                "decision": decision,
                "reason_code": body.get("reason_code"),
            },
        )
        return jsonify({"success": True, "decision": record}), 201

    @bp.route("/api/v1/advisory/plans/<plan_id>/status", methods=["POST"])
    @user_auth_decorator
    def update_my_advisory_plan_status(plan_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        status = str(body.get("status") or "").strip()
        if not status:
            return jsonify({"success": False, "error": "status is required"}), 400
        plan = deps.db_update_advisory_plan_status(
            advisory_plan_id=plan_id,
            client_id=auth_session["client_id"],
            status=status,
            metadata_patch=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        if not plan:
            return jsonify({"success": False, "error": "Plan not found"}), 404
        _trace_if_available(
            deps,
            client_id=auth_session["client_id"],
            case_id=plan_id,
            case_type="advisory_plan",
            event_type="advisory_plan.status_changed",
            output_summary=f"status={status}",
            subjects=[{"subject_type": "advisory_plan", "subject_id": plan_id, "relation": "updated"}],
        )
        safe_publish_event(
            deps,
            event_key=f"advisory.plan_status_changed:{plan_id}:{status}",
            client_id=auth_session["client_id"],
            aggregate_type="advisory_plan",
            aggregate_id=plan_id,
            event_type="advisory.plan_status_changed",
            event_source="mobile",
            payload={"advisory_plan_id": plan_id, "status": status},
        )
        return jsonify({"success": True, "plan": plan}), 200

    @bp.route("/api/v1/advisory/holdings", methods=["GET"])
    @user_auth_decorator
    def list_my_advisory_holdings(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        status = request.args.get("status")
        holdings = deps.db_list_advisory_holdings(
            client_id=auth_session["client_id"],
            status=status,
        )
        return jsonify({"success": True, "holdings": holdings}), 200

    @bp.route("/api/v1/advisory/holdings", methods=["POST"])
    @user_auth_decorator
    def create_my_advisory_holding(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        holding = deps.db_create_advisory_holding(
            client_id=auth_session["client_id"],
            advisory_plan_id=body.get("advisory_plan_id"),
            domain=str(body.get("domain") or "investment"),
            holding_type=str(body.get("holding_type") or "external"),
            status=str(body.get("status") or "active"),
            title=body.get("title"),
            institution=body.get("institution"),
            symbol=body.get("symbol"),
            quantity=body.get("quantity"),
            market_value=body.get("market_value"),
            currency=str(body.get("currency") or "USD"),
            acquisition_source=str(body.get("acquisition_source") or "manual_import"),
            acquired_at=body.get("acquired_at"),
            effective_at=body.get("effective_at"),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        if not holding:
            return jsonify({"success": False, "error": "Failed to create holding"}), 500
        _trace_if_available(
            deps,
            client_id=auth_session["client_id"],
            case_id=holding["id"],
            case_type="advisory_holding",
            event_type="holding.imported",
            output_summary=holding.get("title") or holding.get("symbol") or "holding created",
            subjects=[
                {"subject_type": "advisory_holding", "subject_id": holding["id"], "relation": "created"},
            ],
        )
        safe_publish_event(
            deps,
            event_key=f"advisory.holding_imported:{holding['id']}",
            client_id=auth_session["client_id"],
            aggregate_type="advisory_holding",
            aggregate_id=holding["id"],
            event_type="advisory.holding_imported",
            event_source=str(body.get("acquisition_source") or "manual_import"),
            payload={
                "holding_id": holding["id"],
                "advisory_plan_id": holding.get("advisory_plan_id"),
                "symbol": holding.get("symbol"),
                "holding_type": holding.get("holding_type"),
            },
        )
        return jsonify({"success": True, "holding": holding}), 201

    @bp.route("/api/v1/advisory/holdings/<holding_id>/status", methods=["POST"])
    @user_auth_decorator
    def update_my_advisory_holding_status(
        holding_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        status = str(body.get("status") or "").strip()
        if not status:
            return jsonify({"success": False, "error": "status is required"}), 400
        holding = deps.db_update_advisory_holding_status(
            holding_id=holding_id,
            client_id=auth_session["client_id"],
            status=status,
            metadata_patch=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        if not holding:
            return jsonify({"success": False, "error": "Holding not found"}), 404
        _trace_if_available(
            deps,
            client_id=auth_session["client_id"],
            case_id=holding_id,
            case_type="advisory_holding",
            event_type="holding.status_changed",
            output_summary=f"status={status}",
            subjects=[{"subject_type": "advisory_holding", "subject_id": holding_id, "relation": "updated"}],
        )
        return jsonify({"success": True, "holding": holding}), 200

    @bp.route("/api/v1/advisory/events/<event_id>/handled", methods=["POST"])
    @user_auth_decorator
    def mark_my_advisory_event_handled(
        event_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        event = deps.db_update_advisory_event_status(
            event_id=event_id,
            client_id=auth_session["client_id"],
            status="handled",
        )
        if not event:
            return jsonify({"success": False, "error": "Event not found"}), 404
        return jsonify({"success": True, "event": event}), 200

    @bp.route("/internal/admin/advisory/events", methods=["POST"])
    @api_key_auth_decorator
    def create_advisory_event() -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        client_id = str(body.get("client_id") or "").strip()
        event_type = str(body.get("event_type") or "").strip()
        source_type = str(body.get("source_type") or "rm").strip()
        if not client_id or not event_type:
            return jsonify({
                "success": False,
                "error": "client_id and event_type are required",
            }), 400

        event = deps.db_create_advisory_event(
            client_id=client_id,
            advisory_plan_id=body.get("advisory_plan_id"),
            event_type=event_type,
            source_type=source_type,
            source_id=body.get("source_id"),
            status=str(body.get("status") or "open"),
            event_time=body.get("event_time"),
            detected_at=body.get("detected_at"),
            effective_at=body.get("effective_at"),
            dedupe_key=body.get("dedupe_key"),
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
        )
        if not event:
            return jsonify({"success": False, "error": "Failed to create event"}), 500
        _trace_if_available(
            deps,
            client_id=client_id,
            case_id=event.get("advisory_plan_id") or event["id"],
            case_type="advisory_event",
            event_type="advisory_event.created",
            actor_type=source_type,
            actor_id=body.get("source_id"),
            output_summary=f"{event_type} created",
            subjects=[
                {"subject_type": "advisory_event", "subject_id": event["id"], "relation": "created"},
            ] + ([{
                "subject_type": "advisory_plan",
                "subject_id": event["advisory_plan_id"],
                "relation": "referenced",
            }] if event.get("advisory_plan_id") else []),
        )

        outreach = None
        if bool(body.get("notify_user")) and hasattr(deps, "compose_advisory_event_outreach"):
            session_id = body.get("session_id")
            if session_id is not None:
                session_id = str(session_id).strip() or None
            outreach = deps.compose_advisory_event_outreach(
                event,
                session_id=session_id,
            )
        return jsonify({
            "success": True,
            "event": event,
            "outreach": outreach,
        }), 201

    @bp.route("/internal/admin/advisory/events/<event_id>/status", methods=["POST"])
    @api_key_auth_decorator
    def update_advisory_event_status(event_id: str) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        status = str(body.get("status") or "").strip()
        if not status:
            return jsonify({"success": False, "error": "status is required"}), 400
        event = deps.db_update_advisory_event_status(
            event_id=event_id,
            status=status,
        )
        if not event:
            return jsonify({"success": False, "error": "Event not found"}), 404
        return jsonify({"success": True, "event": event}), 200

    @bp.route("/internal/admin/advisory/expert-products", methods=["GET"])
    @api_key_auth_decorator
    def list_expert_products() -> Tuple[Any, int]:
        products = deps.db_list_expert_products(
            domain=request.args.get("domain"),
            status=request.args.get("status"),
        )
        return jsonify({"success": True, "products": products}), 200

    @bp.route("/internal/admin/advisory/expert-products", methods=["POST"])
    @api_key_auth_decorator
    def create_expert_product() -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        domain = str(body.get("domain") or "").strip()
        name = str(body.get("name") or "").strip()
        if not domain or not name:
            return jsonify({"success": False, "error": "domain and name are required"}), 400
        product = deps.db_create_expert_product(
            domain=domain,
            name=name,
            status=str(body.get("status") or "draft"),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        if not product:
            return jsonify({"success": False, "error": "Failed to create expert product"}), 500
        return jsonify({"success": True, "product": product}), 201

    @bp.route("/internal/admin/advisory/expert-products/<product_id>/versions", methods=["GET"])
    @api_key_auth_decorator
    def list_expert_product_versions(product_id: str) -> Tuple[Any, int]:
        versions = deps.db_list_expert_product_versions(
            expert_product_id=product_id,
            status=request.args.get("status"),
        )
        return jsonify({"success": True, "versions": versions}), 200

    @bp.route("/internal/admin/advisory/expert-products/<product_id>/versions", methods=["POST"])
    @api_key_auth_decorator
    def create_expert_product_version(product_id: str) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        version_value = body.get("version")
        try:
            version = int(version_value) if version_value is not None else None
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "version must be an integer"}), 400
        product_version = deps.db_create_expert_product_version(
            expert_product_id=product_id,
            version=version,
            status=str(body.get("status") or "draft"),
            source_summary=body.get("source_summary") if isinstance(body.get("source_summary"), dict) else {},
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
            effective_at=body.get("effective_at"),
            valid_from=body.get("valid_from"),
            valid_to=body.get("valid_to"),
        )
        if not product_version:
            return jsonify({"success": False, "error": "Failed to create expert product version"}), 500
        _trace_if_available(
            deps,
            case_id=product_id,
            case_type="expert_product",
            event_type="expert_product.versioned",
            output_summary=f"version={product_version['version']}",
            subjects=[
                {"subject_type": "expert_product", "subject_id": product_id, "relation": "updated"},
                {
                    "subject_type": "expert_product_version",
                    "subject_id": product_version["id"],
                    "relation": "created",
                },
            ],
        )
        return jsonify({"success": True, "version": product_version}), 201

    @bp.route("/internal/admin/advisory/engine-runs", methods=["GET"])
    @api_key_auth_decorator
    def list_engine_runs() -> Tuple[Any, int]:
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            limit = 50
        runs = deps.db_list_engine_runs(
            client_id=request.args.get("client_id"),
            journey_id=request.args.get("journey_id"),
            expert_product_version_id=request.args.get("expert_product_version_id"),
            status=request.args.get("status"),
            limit=limit,
        )
        return jsonify({"success": True, "engine_runs": runs}), 200

    @bp.route("/internal/admin/advisory/external-events", methods=["POST"])
    @api_key_auth_decorator
    def create_raw_external_event() -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        source_system = str(body.get("source_system") or "").strip()
        event_type = str(body.get("event_type") or "").strip()
        if not source_system or not event_type:
            return jsonify({"success": False, "error": "source_system and event_type are required"}), 400
        event = deps.db_create_raw_external_event(
            source_system=source_system,
            source_event_id=body.get("source_event_id"),
            event_type=event_type,
            dedupe_key=body.get("dedupe_key"),
            status=str(body.get("status") or "received"),
            occurred_at=body.get("occurred_at"),
            normalized_payload=(
                body.get("normalized_payload")
                if isinstance(body.get("normalized_payload"), dict) else {}
            ),
            raw_payload=body.get("raw_payload") if isinstance(body.get("raw_payload"), dict) else body,
        )
        if not event:
            return jsonify({"success": False, "error": "Failed to create external event"}), 500
        _trace_if_available(
            deps,
            case_id=event["id"],
            case_type="raw_external_event",
            event_type="external_event.received",
            actor_type=source_system,
            actor_id=body.get("source_event_id"),
            output_summary=f"{event_type} received from {source_system}",
            subjects=[
                {"subject_type": "raw_external_event", "subject_id": event["id"], "relation": "created"},
            ],
        )
        safe_publish_event(
            deps,
            event_key=f"external.received:{event['id']}",
            aggregate_type="raw_external_event",
            aggregate_id=event["id"],
            event_type="external.event_received",
            event_source=source_system,
            payload={
                "raw_external_event_id": event["id"],
                "source_system": source_system,
                "source_event_id": body.get("source_event_id"),
                "event_type": event_type,
            },
        )
        derived = []
        if bool(body.get("derive_advisory_events")):
            derived = deps.db_derive_advisory_events_from_external_event(
                raw_external_event_id=event["id"],
                event_type=str(body.get("derived_event_type") or "holding.external_event_detected"),
            )
            for advisory_event in derived:
                safe_publish_event(
                    deps,
                    event_key=f"advisory.event_derived:{event['id']}:{advisory_event.get('id')}",
                    client_id=advisory_event.get("client_id"),
                    aggregate_type="advisory_event",
                    aggregate_id=advisory_event.get("id"),
                    event_type="advisory.event_derived",
                    event_source=source_system,
                    payload={
                        "raw_external_event_id": event["id"],
                        "advisory_event_id": advisory_event.get("id"),
                        "advisory_plan_id": advisory_event.get("advisory_plan_id"),
                        "source_event_type": event_type,
                    },
                )
        return jsonify({"success": True, "event": event, "derived_advisory_events": derived}), 201

    return bp


def _trace_if_available(deps: Any, **kwargs: Any) -> None:
    if not hasattr(deps, "db_create_trace_event"):
        return
    try:
        deps.db_create_trace_event(**kwargs)
    except Exception as exc:  # pragma: no cover - trace must not break product flow
        print(f"[trace] advisory route trace failed: {exc}", flush=True)


__all__ = ["create_advisory_blueprint"]
