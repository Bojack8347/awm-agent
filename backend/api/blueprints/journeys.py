"""Journey lifecycle HTTP routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from api.services.journey_activation import activate_journey_policy as activate_journey_policy_workflow


def create_journeys_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    api_key_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    """Create journey routes with app-level dependency hooks."""
    bp = Blueprint("journeys", __name__)

    def _authorize_voice_tool() -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Any, int]]]:
        """Authorize Main Agent journey tool calls.

        The Main Agent adapter calls these endpoints from the app,
        where the user bearer token is available but the advisor API key should
        not be required. Existing API-key access is retained for legacy
        server-to-server voice tool callers.
        """
        auth_session, auth_error = deps.require_authenticated_account()
        if not auth_error and auth_session:
            return auth_session, None

        ok, api_key_error = deps.require_api_key()
        if ok:
            return None, None

        return None, (jsonify(api_key_error), 401)

    @bp.route("/advisor/api/v1/voice/pool/upsert", methods=["POST"])
    def voice_pool_upsert() -> Tuple[Any, int]:
        """Main Agent tool: record or update a money pool the client described.

        Upserts by (client_id, label) so filling a pool in over several turns
        updates it rather than duplicating; the persistence layer derives the
        lifecycle state and logs the change.
        """
        auth_session, auth_error = _authorize_voice_tool()
        if auth_error:
            return auth_error
        if auth_session is None:
            return jsonify({"success": False, "error": "Authentication required"}), 401

        from api.persistence import upsert_money_pool as db_upsert_money_pool  # type: ignore

        body = request.get_json(silent=True) or {}
        label = str(body.get("label") or "").strip()
        if not label:
            return jsonify({"success": False, "error": "label required"}), 400

        pool = db_upsert_money_pool(auth_session["client_id"], body)
        if pool is None:
            return jsonify({"success": False, "error": "Failed to save money pool"}), 500
        return jsonify({
            "success": True,
            "pool": {
                "id": pool["id"],
                "label": pool["label"],
                "state": pool["state"],
                "amount": pool["amount"],
                "purpose_type": pool["purpose_type"],
                "risk_tolerance": pool["risk_tolerance"],
                "horizon_date": pool["horizon_date"],
            },
        }), 200

    @bp.route("/api/v1/policies/proposed", methods=["GET"])
    @user_auth_decorator
    def list_proposed_policies(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if not deps.journey_runtime_v2_enabled():
            return deps.v2_disabled_response()

        from api.persistence import get_proposed_policies as db_get_proposed_policies  # type: ignore

        policies = db_get_proposed_policies(auth_session["client_id"])
        return jsonify({"success": True, "policies": policies}), 200

    @bp.route("/api/v1/journeys/<journey_id>/activate", methods=["POST"])
    @user_auth_decorator
    def activate_journey_policy(journey_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        payload, status = activate_journey_policy_workflow(
            deps,
            journey_id=journey_id,
            auth_session=auth_session,
        )
        return jsonify(payload), status

    @bp.route("/api/v1/policies/activated", methods=["GET"])
    @user_auth_decorator
    def get_activated_policies_endpoint(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policies = deps.db_get_activated_policies(auth_session["client_id"])
        return jsonify({
            "success": True,
            "policies": policies,
        }), 200

    return bp


__all__ = ["create_journeys_blueprint"]
