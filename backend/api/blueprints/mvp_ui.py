"""MVP UI business APIs for the revised AWM mobile flow."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from api.services.events import safe_publish_event
from api.services.monitoring_report_adapter import monitoring_report_from_policy_sources
from api.services.mvp_mock_providers import (
    DeterministicAgentProvider,
    MockBrokerProvider,
    MockExternalAccountProvider,
    MockKycProvider,
    MockMarketDataProvider,
)
from api.services.mvp_real_providers import RealModelAgentProvider
from api.services.projection_inputs import (
    projection_cashflow_input,
    projection_input_fingerprint,
)
from api.services.external_data_decision import ExternalDataDecisionService

_MVP_NOTIFICATION_MEMORY: Dict[str, Dict[str, Dict[str, Any]]] = {}
_MVP_ENTRYPOINT_MEMORY: Dict[str, Dict[str, str]] = {}


def _proposal_artifact_mobile_view(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Strip heavy proposal fields the APP does not render.

    ProposalView primarily consumes ``mobile_section_5b``. Shipping full
    ``sections[]``, engine outputs, and writeback provenance on every GET makes
    "Check proposal" open slowly on mobile.
    """
    if not isinstance(artifact, dict):
        return artifact
    record_type = str(artifact.get("record_type") or artifact.get("artifact_type") or "")
    if record_type and record_type != "proposal":
        return artifact
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
    mobile_5b = payload.get("mobile_section_5b")
    if not isinstance(mobile_5b, dict):
        return artifact
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    engine_run = payload.get("engine_run") if isinstance(payload.get("engine_run"), dict) else {}
    engine_inputs = engine_run.get("inputs") if isinstance(engine_run.get("inputs"), dict) else {}
    slim_policy = {
        key: policy[key]
        for key in ("scope_of_purpose", "horizon_years", "id", "name")
        if key in policy
    }
    slim_inputs = {
        key: engine_inputs[key]
        for key in (
            "investment_amount",
            "capital",
            "amount",
            "funding_source",
            "fundingSource",
            "horizon",
            "journey_id",
        )
        if key in engine_inputs
    }
    slim_payload: Dict[str, Any] = {
        "title": payload.get("title") or artifact.get("title"),
        "mobile_section_5b": mobile_5b,
        "artifact_type": "proposal",
    }
    if payload.get("money_pool") is not None:
        slim_payload["money_pool"] = payload.get("money_pool")
    if slim_policy:
        slim_payload["policy"] = slim_policy
    if engine_run:
        slim_engine: Dict[str, Any] = {}
        if slim_inputs:
            slim_engine["inputs"] = slim_inputs
        for key in ("status", "engine_name", "engine_version"):
            if key in engine_run:
                slim_engine[key] = engine_run[key]
        if slim_engine:
            slim_payload["engine_run"] = slim_engine
    if payload.get("schema_version") is not None:
        slim_payload["schema_version"] = payload.get("schema_version")
    return {
        "id": artifact.get("id"),
        "client_id": artifact.get("client_id"),
        "record_type": artifact.get("record_type") or "proposal",
        "artifact_type": "proposal",
        "status": artifact.get("status"),
        "related_type": artifact.get("related_type"),
        "related_id": artifact.get("related_id"),
        "secondary_id": artifact.get("secondary_id"),
        "sections": [],
        "section_ids": [],
        "payload": slim_payload,
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
    }


def _policy_mobile_view(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Slim policy detail payloads for the APP ProposalView.

    Policy detail reuses the proposal renderer and only needs title, proposal id,
    money pool, and ``mobile_section_5b`` — not duplicated section trees.
    """
    if not isinstance(policy, dict):
        return policy
    payload = policy.get("payload") if isinstance(policy.get("payload"), dict) else {}
    mobile_5b = payload.get("mobile_section_5b") or payload.get("mobileSection5b")
    if not isinstance(mobile_5b, dict):
        return policy
    slim_payload: Dict[str, Any] = {
        "title": payload.get("title") or "AWM Policy",
        "mobile_section_5b": mobile_5b,
    }
    proposal_id = payload.get("proposal_id") or policy.get("related_id")
    if proposal_id is not None:
        slim_payload["proposal_id"] = proposal_id
    if payload.get("money_pool") is not None:
        slim_payload["money_pool"] = payload.get("money_pool")
    if payload.get("broker") is not None:
        slim_payload["broker"] = payload.get("broker")
    return {
        "id": policy.get("id"),
        "client_id": policy.get("client_id"),
        "record_type": policy.get("record_type") or "policy",
        "status": policy.get("status"),
        "related_type": policy.get("related_type"),
        "related_id": policy.get("related_id"),
        "secondary_id": policy.get("secondary_id"),
        "payload": slim_payload,
        "created_at": policy.get("created_at"),
        "updated_at": policy.get("updated_at"),
    }


def _projection_artifact_mobile_view(projection: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the native_projection section the APP Projection screen reads."""
    if not isinstance(projection, dict):
        return projection
    payload = projection.get("payload") if isinstance(projection.get("payload"), dict) else {}
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else None
    if not isinstance(sections, list):
        sections = projection.get("sections") if isinstance(projection.get("sections"), list) else []
    native = next(
        (
            section
            for section in sections
            if isinstance(section, dict) and str(section.get("section_id") or "") == "native_projection"
        ),
        None,
    )
    if not isinstance(native, dict):
        return projection
    slim_payload: Dict[str, Any] = {
        "title": payload.get("title") or "Projection",
        "artifact_type": "projection",
        "sections": [native],
        "section_ids": ["native_projection"],
    }
    for key in ("input_fingerprint", "knowledge_snapshot_version", "schema_version"):
        if key in payload:
            slim_payload[key] = payload[key]
    return {
        "id": projection.get("id"),
        "client_id": projection.get("client_id"),
        "record_type": projection.get("record_type") or "projection",
        "artifact_type": "projection",
        "status": projection.get("status"),
        "related_type": projection.get("related_type"),
        "related_id": projection.get("related_id"),
        "secondary_id": projection.get("secondary_id"),
        "sections": [native],
        "section_ids": ["native_projection"],
        "payload": slim_payload,
        "created_at": projection.get("created_at"),
        "updated_at": projection.get("updated_at"),
    }


def _policy_proposal_id(policy: Dict[str, Any]) -> str:
    payload = policy.get("payload") or {}
    return str(payload.get("proposal_id") or policy.get("related_id") or "").strip()


def _is_executed_policy(policy: Dict[str, Any]) -> bool:
    return str(policy.get("status") or "").strip().lower() in {"active", "executed", "filled"}


def create_mvp_ui_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    api_key_auth_decorator: Callable[[Any], Any],
    deps: Any,
    external_account_provider: Optional[Any] = None,
    external_data_decision_service: Optional[ExternalDataDecisionService] = None,
) -> Blueprint:
    bp = Blueprint("mvp_ui", __name__)
    agent = DeterministicAgentProvider()
    kyc = MockKycProvider()
    external_accounts = external_account_provider or MockExternalAccountProvider()
    external_decisions = external_data_decision_service or ExternalDataDecisionService()
    broker = MockBrokerProvider()
    market = MockMarketDataProvider()

    @bp.route("/api/v1/onboarding/status", methods=["GET"])
    @user_auth_decorator
    def onboarding_status(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        status = deps.db_get_onboarding_status(client_id) or deps.db_upsert_onboarding_status(
            client_id=client_id,
            current_step="identity",
            status="not_started",
            completed_steps=[],
        )
        return jsonify({"success": True, "onboarding": status}), 200

    @bp.route("/api/v1/notifications", methods=["GET"])
    @user_auth_decorator
    def list_notifications(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 100))
        except (TypeError, ValueError):
            limit = 50
        sorted_items = _list_projected_notifications(deps, client_id, limit)
        unread_count = sum(1 for item in sorted_items if item.get("status") != "read")
        return jsonify({"success": True, "notifications": sorted_items, "unread_count": unread_count}), 200

    @bp.route("/api/v1/notifications/<notification_id>/read", methods=["POST"])
    @user_auth_decorator
    def mark_notification_read(notification_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        notification = (_MVP_NOTIFICATION_MEMORY.get(client_id) or {}).get(notification_id)
        if notification:
            notification["status"] = "read"
            return jsonify({"success": True, "notification": notification}), 200
        return jsonify({"success": True, "notification": {"id": notification_id, "status": "read"}}), 200

    @bp.route("/api/v1/conversations/awm-chat/entrypoint", methods=["GET"])
    @user_auth_decorator
    def awm_chat_entrypoint(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        entrypoint = _resolve_arc_chat_entrypoint(deps, agent, client_id)
        _emit(
            deps,
            client_id,
            "conversation.entrypoint_resolved",
            "conversation",
            {"id": entrypoint.get("conversation", {}).get("id")},
            {
                "trigger_type": entrypoint.get("trigger_type"),
                "conversation_id": entrypoint.get("conversation", {}).get("id"),
                "source": entrypoint.get("source"),
            },
        )
        return jsonify({"success": True, **entrypoint}), 200

    @bp.route("/api/v1/onboarding/identity", methods=["POST"])
    @user_auth_decorator
    def save_identity(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        identity = {
            "legal_name": body.get("legal_name"),
            "date_of_birth": body.get("date_of_birth"),
            "tax_id_last4": body.get("tax_id_last4") or body.get("ssn_last4"),
            "address": body.get("address") if isinstance(body.get("address"), dict) else {},
        }
        result = kyc.verify(identity)
        profile = deps.db_save_identity_profile(client_id=client_id, identity=identity, kyc_result=result)
        status = deps.db_upsert_onboarding_status(
            client_id=client_id,
            current_step="consents" if result["status"] == "verified" else "identity_review",
            status="identity_verified" if result["status"] == "verified" else "identity_pending",
            completed_steps=["identity"] if result["status"] == "verified" else [],
        )
        _emit(deps, client_id, "onboarding.identity_saved", "identity_profile", profile, {"kyc": result})
        return jsonify({"success": True, "identity_profile": profile, "kyc": result, "onboarding": status}), 200

    @bp.route("/api/v1/onboarding/consents", methods=["POST"])
    @user_auth_decorator
    def save_consents(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        consent_type = str(body.get("consent_type") or "advice_agreement")
        consent = deps.db_save_consent(client_id=client_id, consent_type=consent_type, payload=body)
        status = deps.db_upsert_onboarding_status(
            client_id=client_id,
            current_step="external_connections",
            status="consents_accepted",
            completed_steps=["identity", "consents"],
        )
        _emit(deps, client_id, "onboarding.consent_recorded", "consent", consent, {"consent_type": consent_type})
        return jsonify({"success": True, "consent": consent, "onboarding": status}), 201

    @bp.route("/api/v1/onboarding/data-permissions", methods=["POST"])
    @user_auth_decorator
    def save_data_permissions(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        scopes = body.get("scopes") if isinstance(body.get("scopes"), list) else []
        permission = deps.db_save_data_permission(client_id=client_id, scopes=scopes, payload=body)
        status = deps.db_upsert_onboarding_status(
            client_id=client_id,
            current_step="complete",
            status="data_permissions_granted",
            completed_steps=["identity", "consents", "external_connections", "data_permissions"],
        )
        _emit(deps, client_id, "onboarding.data_permission_saved", "data_permission", permission, {"scopes": scopes})
        return jsonify({"success": True, "data_permission": permission, "onboarding": status}), 201

    @bp.route("/api/v1/onboarding/external-data-decision", methods=["PUT"])
    @user_auth_decorator
    def external_data_decision(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        try:
            record = external_decisions.decide(
                client_id=auth_session["client_id"],
                decision=str(body.get("decision") or ""),
                scopes=body.get("scopes") if isinstance(body.get("scopes"), list) else [],
                consent_text_version=str(body.get("consent_text_version") or ""),
                client_request_id=str(body.get("client_request_id") or ""),
                grant_reference=str(body.get("grant_reference") or "") or None,
            )
        except ValueError as exc:
            code = str(exc)
            return jsonify({"success": False, "error": code}), 409 if code in {"idempotency_conflict", "grant_reference_invalid"} else 400
        current = external_decisions.current(auth_session["client_id"])
        completed = ["identity", "consents", "external_connections"] if current["sharing_decision"] == "declined" else ["identity", "consents", "data_permissions"]
        status = deps.db_upsert_onboarding_status(
            client_id=auth_session["client_id"],
            current_step="complete" if current["sharing_decision"] == "declined" else "external_connections",
            status=current["workflow_state"],
            completed_steps=completed,
        )
        return jsonify({"success": True, "external_data_decision": record, "onboarding": status}), 201

    @bp.route("/api/v1/onboarding/external-data-decision", methods=["GET"])
    @user_auth_decorator
    def get_external_data_decision(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        return jsonify({"success": True, "external_data_decision": external_decisions.current(auth_session["client_id"])}), 200

    @bp.route("/api/v1/onboarding/external-connections", methods=["POST"])
    @user_auth_decorator
    def create_external_connection(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        connection_type = str(body.get("connection_type") or "brokerage")
        requested_scopes = body.get("scopes") if isinstance(body.get("scopes"), list) else ["account_identity", "balances", "holdings", "transactions"]
        try:
            external_decisions.require_grant(client_id, requested_scopes)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 403
        result = external_accounts.connect(connection_type, body)
        connection = deps.db_save_external_connection(client_id=client_id, connection_type=connection_type, provider_result=result)
        for holding in result.get("holdings") or []:
            deps.db_save_holding(client_id=client_id, policy_id=None, status="imported", payload={**holding, "source_connection_id": connection["id"] if connection else None})
        external_decisions.repository.set_connection_status(
            client_id,
            "connected_with_data" if result.get("holdings") else "connected_no_holdings",
        )
        status = deps.db_upsert_onboarding_status(
            client_id=client_id,
            current_step="data_permissions",
            status="external_connection_created",
            completed_steps=["identity", "consents", "external_connections"],
        )
        _emit(deps, client_id, "onboarding.external_connection_created", "external_connection", connection, result)
        return jsonify({"success": True, "external_connection": connection, "provider_result": result, "onboarding": status}), 201

    @bp.route("/api/v1/onboarding/complete", methods=["POST"])
    @user_auth_decorator
    def complete_onboarding(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        status = deps.db_upsert_onboarding_status(
            client_id=client_id,
            current_step="advice_ready",
            status="advice_ready",
            completed_steps=["identity", "consents", "data_permissions", "external_connections"],
        )
        if hasattr(deps, "db_mark_onboarding_completed"):
            deps.db_mark_onboarding_completed(client_id)
        _emit(deps, client_id, "onboarding.completed", "onboarding", {"id": client_id}, {})
        return jsonify({"success": True, "onboarding": status}), 200

    @bp.route("/api/v1/onboarding/orchestrator/messages", methods=["POST"])
    @user_auth_decorator
    def post_onboarding_orchestrator_message(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        user_text = str(body.get("text") or body.get("message") or "")
        state = str(body.get("state") or "onboarding_understanding")
        known_context = body.get("known_context") if isinstance(body.get("known_context"), dict) else {}
        selected_action = body.get("selected_action") if isinstance(body.get("selected_action"), dict) else {}
        result = _orchestrate_onboarding_turn(user_text, state, known_context, selected_action)
        conversation_id = str(body.get("conversation_id") or "")
        conversation = deps.db_get_conversation(conversation_id=conversation_id, client_id=client_id) if conversation_id and hasattr(deps, "db_get_conversation") else None
        if not conversation:
            conversation = deps.db_create_conversation(
                client_id=client_id,
                scope="awm_chat",
                context={"onboarding_orchestrator": {"state": result["state"]}},
            )
        user_message = None
        if user_text:
            user_message = deps.db_add_conversation_message(
                conversation_id=conversation["id"],
                client_id=client_id,
                role="user",
                message_type="text",
                text=user_text,
                payload={"state": state, "known_context": known_context},
            )
        assistant_message = deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="assistant",
            message_type="decision_prompt" if result.get("next_actions") else "text",
            text=result["assistant_message"],
            payload={
                "title": result.get("title") or "Next best step",
                "description": result["assistant_message"],
                "actions": result.get("next_actions") or [],
                "state": result["state"],
                "detected_intents": result.get("detected_intents") or [],
                "detected_risks": result.get("detected_risks") or [],
                "artifact_suggestion": result.get("artifact_suggestion"),
            },
        )
        _emit(
            deps,
            client_id,
            "onboarding.orchestrator_turn",
            "conversation",
            conversation,
            {
                "conversation_id": conversation.get("id"),
                "state": result["state"],
                "detected_intents": result.get("detected_intents") or [],
                "detected_risks": result.get("detected_risks") or [],
            },
        )
        result_payload = {key: value for key, value in result.items() if key != "assistant_message"}
        return jsonify({
            "success": True,
            "conversation": conversation,
            "message": user_message,
            "assistant_message": assistant_message,
            "assistant_text": result["assistant_message"],
            **result_payload,
        }), 201

    @bp.route("/api/v1/conversations", methods=["POST"])
    @user_auth_decorator
    def create_conversation(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        conversation = deps.db_create_conversation(
            client_id=auth_session["client_id"],
            scope=str(body.get("scope") or "general"),
            context=body.get("context") if isinstance(body.get("context"), dict) else {},
        )
        _emit(deps, auth_session["client_id"], "conversation.created", "conversation", conversation, conversation or {})
        return jsonify({"success": True, "conversation": conversation}), 201

    @bp.route("/api/v1/conversations/<conversation_id>/messages", methods=["GET"])
    @user_auth_decorator
    def list_messages(conversation_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        messages = deps.db_list_conversation_messages(conversation_id=conversation_id, client_id=auth_session["client_id"])
        return jsonify({"success": True, "messages": messages}), 200

    @bp.route("/api/v1/conversations/<conversation_id>/messages", methods=["POST"])
    @user_auth_decorator
    def post_message(conversation_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        user_message = deps.db_add_conversation_message(
            conversation_id=conversation_id,
            client_id=client_id,
            role="user",
            message_type="text",
            text=str(body.get("text") or body.get("message") or ""),
            attachments=body.get("attachments") if isinstance(body.get("attachments"), list) else [],
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
        )
        context = deps.db_get_conversation(conversation_id=conversation_id, client_id=client_id) or {}
        answer_text = "I can help with that AWM workflow."
        artifact_id = (context.get("context") or {}).get("artifact_id")
        section_id = (context.get("context") or {}).get("section_id")
        payload = {}
        if artifact_id and section_id:
            artifact = deps.db_get_artifact(artifact_id=artifact_id, client_id=client_id)
            payload = agent.answer_question((artifact or {}).get("payload") or {}, section_id, user_message.get("text") if user_message else "")
            answer_text = payload["answer"]
        assistant_message = deps.db_add_conversation_message(
            conversation_id=conversation_id,
            client_id=client_id,
            role="assistant",
            message_type="text",
            text=answer_text,
            attachments=payload.get("citations", []),
            payload=payload,
        )
        _emit(deps, client_id, "conversation.message_posted", "conversation", {"id": conversation_id}, {"user_message_id": (user_message or {}).get("id"), "assistant_message_id": (assistant_message or {}).get("id")})
        return jsonify({"success": True, "message": user_message, "assistant_message": assistant_message}), 201

    @bp.route("/api/v1/journeys/<journey_id>/messages", methods=["POST"])
    @user_auth_decorator
    def post_journey_message(journey_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        conversation = deps.db_create_conversation(
            client_id=client_id,
            scope="journey",
            context={"journey_id": journey_id},
        )
        user_message = deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="user",
            message_type="text",
            text=str(body.get("text") or body.get("message") or ""),
            payload={"journey_id": journey_id},
        )
        assistant_message = deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="assistant",
            message_type="task_status",
            text="Journey field collection is in progress.",
            payload={
                "journey_id": journey_id,
                "state": "collecting",
                "missing_fields": ["amount", "horizon", "risk", "funding_source"],
            },
        )
        _emit(deps, client_id, "journey.message_recorded", "journey", {"id": journey_id}, {"conversation_id": conversation["id"]})
        return jsonify({
            "success": True,
            "journey_id": journey_id,
            "conversation": conversation,
            "message": user_message,
            "assistant_message": assistant_message,
            "state": "collecting",
        }), 201

    @bp.route("/api/v1/conversations/from-artifact-section", methods=["POST"])
    @user_auth_decorator
    def conversation_from_artifact(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        artifact_id = str(body.get("artifact_id") or "")
        section_id = str(body.get("section_id") or "")
        artifact = deps.db_get_artifact(artifact_id=artifact_id, client_id=client_id)
        if not artifact:
            return jsonify({"success": False, "error": "Artifact not found"}), 404
        conversation = deps.db_create_conversation(
            client_id=client_id,
            scope=str(body.get("scope") or artifact.get("record_type") or "artifact"),
            context={"artifact_id": artifact_id, "section_id": section_id, "related_id": artifact.get("related_id")},
        )
        citation = {"artifact_id": artifact_id, "section_id": section_id}
        deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="assistant",
            message_type="attachment_context",
            text=f"Context attached: {section_id}",
            attachments=[citation],
            payload={"artifact_type": artifact.get("record_type"), "section_id": section_id},
        )
        return jsonify({"success": True, "conversation": conversation}), 201

    @bp.route("/api/v1/advisory/artifacts/<artifact_id>/review-conversation", methods=["POST"])
    @user_auth_decorator
    def create_artifact_review_conversation(artifact_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        artifact = deps.db_get_artifact(artifact_id=artifact_id, client_id=client_id)
        if not artifact:
            return jsonify({"success": False, "error": "Artifact not found"}), 404
        conversation, messages = _create_artifact_review_conversation(
            deps,
            client_id,
            artifact,
            mode=str(body.get("mode") or "voice_guided"),
            conversation_id=str(body.get("conversation_id") or ""),
        )
        _emit(
            deps,
            client_id,
            "artifact.review_conversation_created",
            "conversation",
            conversation,
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact.get("record_type"),
                "message_count": len(messages),
                "mode": str(body.get("mode") or "voice_guided"),
            },
        )
        return jsonify({"success": True, "conversation": conversation, "messages": messages}), 201

    @bp.route("/api/v1/conversations/<conversation_id>/events", methods=["POST"])
    @user_auth_decorator
    def insert_conversation_event(conversation_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        message_type = str(body.get("message_type") or "business_card")
        if message_type not in {"text", "attachment_context", "business_card", "task_status", "chart_card", "decision_prompt"}:
            return jsonify({"success": False, "error": "Unsupported message_type"}), 400
        message = deps.db_add_conversation_message(
            conversation_id=conversation_id,
            client_id=auth_session["client_id"],
            role=str(body.get("role") or "assistant"),
            message_type=message_type,
            text=body.get("text"),
            attachments=body.get("attachments") if isinstance(body.get("attachments"), list) else [],
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
            event_id=body.get("event_id"),
            run_id=body.get("run_id"),
            trace_id=body.get("trace_id"),
        )
        return jsonify({"success": True, "message": message}), 201

    @bp.route("/api/v1/conversations/<conversation_id>/actions", methods=["POST"])
    @user_auth_decorator
    def run_conversation_action(conversation_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        action = body.get("action") if isinstance(body.get("action"), dict) else body
        if not isinstance(action, dict):
            return jsonify({"success": False, "error": "Action body must be an object"}), 400
        conversation = deps.db_get_conversation(conversation_id=conversation_id, client_id=client_id)
        if not conversation:
            return jsonify({"success": False, "error": "Conversation not found"}), 404
        label = str(action.get("label") or action.get("action_id") or "Review action")
        user_message = deps.db_add_conversation_message(
            conversation_id=conversation_id,
            client_id=client_id,
            role="user",
            message_type="text",
            text=label,
            payload={"action": action},
        )
        result = _execute_review_action(deps, agent, client_id, conversation_id, action)
        _emit(
            deps,
            client_id,
            "conversation.action_selected",
            "conversation",
            conversation,
            {
                "conversation_id": conversation_id,
                "action": action,
                "user_message_id": (user_message or {}).get("id"),
                "assistant_message_ids": [message.get("id") for message in result.get("messages", [])],
            },
        )
        return jsonify({"success": True, "message": user_message, **result}), 201

    @bp.route("/api/v1/journeys/<journey_id>/complete-fields", methods=["POST"])
    @user_auth_decorator
    def complete_journey_fields(journey_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else body
        artifact = agent.proposal_artifact({"id": journey_id, "collected_fields": fields})
        pool_label = str(((artifact.get("money_pool") or {}) if isinstance(artifact.get("money_pool"), dict) else {}).get("label") or "").strip().lower()
        # One proposal per money intent: re-preparing the same journey/pool updates
        # the existing draft in place instead of stacking a new stub each time. A
        # different pool (or an already finalized proposal) keeps its own artifact.
        existing_draft = None
        if hasattr(deps, "db_list_artifacts"):
            try:
                prior = deps.db_list_artifacts(client_id=auth_session["client_id"], artifact_type="proposal") or []
            except Exception:  # pylint: disable=broad-except
                prior = []
            for cand in prior:
                if str(cand.get("status") or "") != "draft" or str(cand.get("related_id") or "") != journey_id:
                    continue
                cpool = cand.get("payload") or {}
                clabel = str(((cpool.get("money_pool") or {}) if isinstance(cpool.get("money_pool"), dict) else {}).get("label") or "").strip().lower()
                if clabel == pool_label:
                    existing_draft = cand
                    break
        if existing_draft and hasattr(deps, "db_update_artifact"):
            proposal = deps.db_update_artifact(artifact_id=existing_draft["id"], client_id=auth_session["client_id"], status="draft", payload_patch={**artifact, "title": artifact["title"]}) or existing_draft
        else:
            proposal = deps.db_save_artifact(client_id=auth_session["client_id"], artifact_type="proposal", title=artifact["title"], payload=artifact, related_type="journey", related_id=journey_id, status="draft")
        _emit(deps, auth_session["client_id"], "journey.fields_completed", "journey", {"id": journey_id}, {"fields": fields, "proposal_artifact_id": proposal["id"] if proposal else None})
        return jsonify({"success": True, "journey_id": journey_id, "state": "ready", "collected_fields": fields, "proposal_artifact": proposal}), 200

    @bp.route("/api/v1/projection", methods=["GET"])
    @user_auth_decorator
    def get_projection(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        _, snapshot_version = projection_cashflow_input(
            deps.db_get_latest_knowledge_snapshot,
            client_id,
        )
        input_fingerprint = projection_input_fingerprint(snapshot_version)
        readiness = _projection_input_readiness(deps, client_id)
        existing = _projection_artifacts_newest_first(deps, client_id)
        matching_projection = _find_ready_projection(existing, input_fingerprint=input_fingerprint)
        prior_real_projection = _find_ready_projection(existing, input_fingerprint=None)

        # Never let an older matching fingerprint hide a newer persisted engine
        # result. Knowledge snapshots can be republished by non-financial
        # writebacks after a projection run; in that case an old fingerprint may
        # become current again even though the newest artifact is the correct
        # latest view. Serve the newest result as stale and let a future explicit
        # cashflow run refresh it. GET remains read-only.
        matching_is_newest = bool(
            matching_projection
            and prior_real_projection
            and matching_projection.get("id") == prior_real_projection.get("id")
        )
        if matching_projection and (prior_real_projection is None or matching_is_newest):
            artifact = matching_projection
            if str(request.args.get("view") or "").strip().lower() == "mobile":
                artifact = _projection_artifact_mobile_view(artifact)
            return jsonify(_projection_ready_response(
                artifact,
                status="ready",
                input_fingerprint=input_fingerprint,
                snapshot_version=snapshot_version,
            )), 200

        if prior_real_projection:
            artifact = prior_real_projection
            if str(request.args.get("view") or "").strip().lower() == "mobile":
                artifact = _projection_artifact_mobile_view(artifact)
            return jsonify(_projection_ready_response(
                artifact,
                status="stale",
                input_fingerprint=input_fingerprint,
                snapshot_version=snapshot_version,
                warning="Showing the latest saved real projection. The current Client File may need a refreshed projection run.",
            )), 200

        if not readiness["ready"]:
            return jsonify({
                "success": True,
                "status": "not_available",
                "projection": None,
                "reason": "insufficient_client_data",
                "missingFields": readiness["missing_fields"],
            }), 200

        return jsonify({
            "success": True,
            "status": "not_available",
            "projection": None,
            "reason": "projection_not_generated",
            "missingFields": [],
        }), 200

    @bp.route("/api/v1/journeys/<journey_id>/generate-proposal", methods=["POST"])
    @user_auth_decorator
    def generate_proposal(journey_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
        # Personalize from the client's real onboarding facts: snapshot-derived
        # fields take precedence over anything the caller passed (e.g. fixtures).
        snapshot_fields = _proposal_fields_from_snapshot(deps, auth_session["client_id"])
        if snapshot_fields:
            fields = {**fields, **snapshot_fields}
            print(f"[generate_proposal] personalized fields from snapshot: {snapshot_fields}", flush=True)
        # If a specific money pool is named, propose for THAT pot — its own
        # amount/horizon/risk/constraints win over the whole-picture snapshot.
        pool_marker = None
        pool_ref = body.get("pool") or body.get("pool_id") or body.get("pool_label")
        # One proposal per money pool. Once the client has bucketed money into
        # pools, EVERY proposal must belong to one — never mint a pool-less "whole
        # portfolio" proposal alongside them (it lingers un-executed and shows as a
        # duplicate card the client can still "execute"). When the caller names no
        # pool but pools exist, bind to the pool the client is most likely working
        # on: the most recently updated pool that has no finalized proposal yet.
        if not pool_ref:
            try:
                from api.persistence import list_money_pools  # type: ignore
                pools = list_money_pools(auth_session["client_id"]) or []
            except Exception:  # pylint: disable=broad-except
                pools = []
            if pools:
                try:
                    _existing = deps.db_list_artifacts(client_id=auth_session["client_id"], artifact_type="proposal") or [] if hasattr(deps, "db_list_artifacts") else []
                except Exception:  # pylint: disable=broad-except
                    _existing = []
                _proposed = {
                    str((((p.get("payload") or {}).get("money_pool")) or {}).get("label") or "").strip().lower()
                    for p in _existing if str(p.get("status") or "") != "draft"
                }
                _unproposed = [p for p in pools if str(p.get("label") or "").strip().lower() not in _proposed]
                _chosen = sorted(_unproposed or pools, key=lambda p: str(p.get("updated_at") or ""), reverse=True)[0]
                pool_ref = _chosen.get("label")
                print(f"[generate_proposal] no pool named but pools exist -> binding to '{pool_ref}'", flush=True)
        # Idempotency per (client, pool): re-preparing the SAME pool (or the
        # whole-portfolio proposal when no pool is named) reuses the existing
        # final proposal instead of stacking a duplicate — this is what stops the
        # RM's reconnect re-prepare from producing endless cards. Different pools
        # stay fully independent (matched by the pool label). Pass force=true to
        # deliberately regenerate.
        if not (body.get("force") or body.get("regenerate")) and hasattr(deps, "db_list_artifacts"):
            want_pool = str(pool_ref or "").strip().lower()
            try:
                prior_proposals = deps.db_list_artifacts(client_id=auth_session["client_id"], artifact_type="proposal") or []
            except Exception:  # pylint: disable=broad-except
                prior_proposals = []
            for candidate in prior_proposals:
                if str(candidate.get("status") or "") == "draft":
                    continue  # drafts are the pre-generation stub, never reuse them
                cpayload = candidate.get("payload") or {}
                cpool = cpayload.get("money_pool") if isinstance(cpayload.get("money_pool"), dict) else None
                if str((cpool or {}).get("label") or "").strip().lower() == want_pool:
                    return jsonify({"success": True, "journey_id": journey_id, "state": "proposal_ready", "artifact": candidate, "reused": True}), 200
        if pool_ref:
            pool_fields = _proposal_fields_from_pool(deps, auth_session["client_id"], str(pool_ref))
            pool_marker = pool_fields.pop("_pool", None)
            if pool_fields:
                fields = {**fields, **pool_fields}
                print(f"[generate_proposal] per-pool fields for {pool_marker}: {pool_fields}", flush=True)
        try:
            artifact = _build_proposal_artifact(deps, agent, journey_id, fields)
            if pool_marker and isinstance(artifact, dict):
                artifact["money_pool"] = pool_marker
        except Exception as exc:  # pylint: disable=broad-except
            _emit(
                deps,
                auth_session["client_id"],
                "proposal.generation_failed",
                "journey",
                {"id": journey_id},
                {"error": str(exc), "provider": _advisory_provider_mode(deps)},
            )
            return jsonify({
                "success": False,
                "error": "Proposal generation failed",
                "details": str(exc),
                "provider": _advisory_provider_mode(deps),
            }), 502
        # Advance the journey's existing draft stub into this ready proposal
        # (one artifact, draft -> ready) instead of leaving an orphaned pool-less
        # [draft] behind. complete-fields created that draft moments ago.
        proposal = None
        try:
            _drafts = [
                a for a in (deps.db_list_artifacts(client_id=auth_session["client_id"], artifact_type="proposal", related_id=journey_id) or [])
                if str(a.get("status") or "") == "draft"
            ] if hasattr(deps, "db_list_artifacts") else []
        except Exception:  # pylint: disable=broad-except
            _drafts = []
        if _drafts and hasattr(deps, "db_update_artifact"):
            proposal = deps.db_update_artifact(artifact_id=_drafts[0]["id"], client_id=auth_session["client_id"], status="ready", payload_patch={**artifact, "title": artifact["title"]})
        if proposal is None:
            proposal = deps.db_save_artifact(client_id=auth_session["client_id"], artifact_type="proposal", title=artifact["title"], payload=artifact, related_type="journey", related_id=journey_id)
        review_conversation, review_messages = _create_artifact_review_conversation(
            deps,
            auth_session["client_id"],
            proposal,
            mode="voice_guided",
        )
        guidance = _proactive_guidance_payload(proposal, conversation=review_conversation, messages=review_messages)
        notification = _notification(
            "proposal.ready",
            "Investment proposal is ready",
            guidance["opening_script"],
            "business_card",
            {
                "artifact_id": proposal["id"] if proposal else None,
                "journey_id": journey_id,
                "conversation_id": review_conversation.get("id"),
                "review_message_ids": [message.get("id") for message in review_messages],
                "primary_action": "review_with_arc",
                "guidance": guidance,
            },
        )
        _emit(deps, auth_session["client_id"], "proposal.generated", "artifact", proposal, {"journey_id": journey_id, "engine_run": artifact.get("engine_run")})
        _emit(deps, auth_session["client_id"], "proposal.ready_notification", "notification", {"id": notification["id"]}, notification)
        return jsonify({"success": True, "journey_id": journey_id, "state": "proposal_ready", "artifact": proposal, "engine_run": artifact.get("engine_run"), "notification": notification}), 201

    @bp.route("/api/v1/advisory/artifacts/<artifact_id>", methods=["GET"])
    @user_auth_decorator
    def get_artifact(artifact_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        artifact = deps.db_get_artifact(artifact_id=artifact_id, client_id=auth_session["client_id"])
        if not artifact:
            return jsonify({"success": False, "error": "Artifact not found"}), 404
        view = str(request.args.get("view") or "").strip().lower()
        if view == "mobile":
            artifact = _proposal_artifact_mobile_view(artifact)
        return jsonify({"success": True, "artifact": artifact}), 200

    @bp.route("/api/v1/advisory/artifacts/<artifact_id>/questions", methods=["POST"])
    @user_auth_decorator
    def ask_artifact_question(artifact_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        return _artifact_question_response(deps, agent, auth_session["client_id"], artifact_id, body)

    @bp.route("/api/v1/executions/authorize", methods=["POST"])
    @user_auth_decorator
    def authorize_execution(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        proposal_id = str(body.get("proposal_id") or body.get("artifact_id") or "")
        proposal = deps.db_get_artifact(artifact_id=proposal_id, client_id=auth_session["client_id"])
        if not proposal:
            return jsonify({"success": False, "error": "Proposal artifact not found"}), 404
        execution = deps.db_save_execution(client_id=auth_session["client_id"], policy_id=None, proposal_id=proposal_id, status="authorized", payload={"proposal_id": proposal_id, "authorization": body})
        _emit(deps, auth_session["client_id"], "execution.authorized", "execution", execution, {"proposal_id": proposal_id})
        return jsonify({"success": True, "execution": execution}), 201

    @bp.route("/api/v1/executions/mock-submit", methods=["POST"])
    @user_auth_decorator
    def mock_submit_execution(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        proposal_id = str(body.get("proposal_id") or body.get("artifact_id") or "")
        proposal = deps.db_get_artifact(artifact_id=proposal_id, client_id=auth_session["client_id"])
        if not proposal:
            return jsonify({"success": False, "error": "Proposal artifact not found"}), 404
        # Idempotency: if this proposal was already executed, return the existing
        # policy instead of opening a duplicate one. Submitting the same proposal
        # twice (double tap, retry, reconnect) must not create two active policies.
        if hasattr(deps, "db_list_policies"):
            try:
                prior_policies = deps.db_list_policies(client_id=auth_session["client_id"], status=None) or []
            except Exception:  # pylint: disable=broad-except
                prior_policies = []
            existing_policy = next(
                (p for p in prior_policies
                 if _policy_proposal_id(p) == proposal_id and _is_executed_policy(p)),
                None,
            )
            if existing_policy:
                return jsonify({
                    "success": True,
                    "execution": None,
                    "policy": existing_policy,
                    "broker_result": (existing_policy.get("payload") or {}).get("broker"),
                    "reused": True,
                }), 200
        proposal_payload = proposal.get("payload") or {}
        # Carry the money pool (if this proposal was for a specific pool) onto the
        # policy, so the policy belongs to that pot and can be shown under it.
        # generate_proposal writes the marker at the artifact top level, while older
        # paths put it in the payload — read both so the pool reliably advances.
        pool = proposal_payload.get("money_pool")
        if not isinstance(pool, dict):
            pool = proposal.get("money_pool")
        if not isinstance(pool, dict):
            pool = None
        broker_result = broker.submit_orders({"id": proposal_id, **proposal_payload})
        policy = deps.db_save_policy(client_id=auth_session["client_id"], status="active", proposal_id=proposal_id, payload={"proposal_id": proposal_id, "broker": broker_result, "title": proposal_payload.get("title", "Active Policy"), "money_pool": pool})
        for order in broker_result.get("orders") or []:
            deps.db_save_holding(client_id=auth_session["client_id"], policy_id=policy["id"], status="active", payload=order)
        execution = deps.db_save_execution(client_id=auth_session["client_id"], policy_id=policy["id"], proposal_id=proposal_id, status="filled", payload={"proposal_id": proposal_id, "policy_id": policy["id"], "broker": broker_result})
        # Mark the proposal executed so it is no longer offered for execution or
        # re-announced as a "ready" proposal when the client re-enters the chat.
        if hasattr(deps, "db_update_artifact"):
            try:
                deps.db_update_artifact(artifact_id=proposal_id, client_id=auth_session["client_id"], status="executed")
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[mock_submit_execution] mark proposal executed failed: {exc}", flush=True)
        _emit(deps, auth_session["client_id"], "execution.filled", "execution", execution, {"proposal_id": proposal_id, "policy_id": policy["id"]})
        # Advance the pool's lifecycle to active now that it's funded.
        if pool and pool.get("label"):
            try:
                from api.persistence import upsert_money_pool as _upsert_pool  # type: ignore
                _upsert_pool(auth_session["client_id"], {"label": pool["label"], "state": "active"})
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[mock_submit_execution] pool advance failed: {exc}", flush=True)
        return jsonify({"success": True, "execution": execution, "policy": policy, "broker_result": broker_result}), 201

    @bp.route("/api/v1/money-pools", methods=["GET"])
    @user_auth_decorator
    def list_client_money_pools(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        """The client's money pools (each with its lifecycle state), so the app
        can present money organized by pool."""
        from api.persistence import list_money_pools  # type: ignore
        pools = list_money_pools(auth_session["client_id"])
        return jsonify({"success": True, "pools": pools}), 200

    @bp.route("/api/v1/executions/<execution_id>", methods=["GET"])
    @user_auth_decorator
    def get_execution(execution_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        execution = deps.db_get_execution(execution_id=execution_id, client_id=auth_session["client_id"])
        if not execution:
            return jsonify({"success": False, "error": "Execution not found"}), 404
        return jsonify({"success": True, "execution": execution}), 200

    @bp.route("/api/v1/policies", methods=["GET"])
    @user_auth_decorator
    def list_policies(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policies = deps.db_list_policies(client_id=auth_session["client_id"], status=request.args.get("status"))
        # Execution creates an active policy while retaining the original
        # proposed policy for audit. Do not expose both as customer-facing rows.
        active_proposal_ids = {
            _policy_proposal_id(policy)
            for policy in policies
            if _is_executed_policy(policy) and _policy_proposal_id(policy)
        }
        policies = [
            policy
            for policy in policies
            if not (
                str(policy.get("status") or "").lower() == "proposed"
                and _policy_proposal_id(policy) in active_proposal_ids
            )
        ]
        return jsonify({"success": True, "policies": policies}), 200

    @bp.route("/api/v1/policies/<policy_id>/lifecycle", methods=["GET"])
    @user_auth_decorator
    def get_policy_lifecycle(policy_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        policy = deps.db_get_policy(policy_id=policy_id, client_id=client_id)
        if not policy:
            return jsonify({"success": False, "error": "Policy not found"}), 404
        proposal_id = _policy_proposal_id(policy)
        siblings = deps.db_list_policies(client_id=client_id, status=None) or []
        active_policy = next(
            (
                sibling
                for sibling in siblings
                if _is_executed_policy(sibling)
                and (
                    sibling.get("id") == policy_id
                    or (proposal_id and _policy_proposal_id(sibling) == proposal_id)
                )
            ),
            None,
        )
        artifact = (
            deps.db_get_artifact(artifact_id=proposal_id, client_id=client_id)
            if proposal_id
            else None
        )
        artifact_status = str((artifact or {}).get("status") or "").lower()
        status = (
            "active"
            if active_policy
            else "executed"
            if artifact_status == "executed"
            else str(policy.get("status") or "proposed").lower()
        )
        return jsonify(
            {
                "success": True,
                "lifecycle": {
                    "policy_id": policy_id,
                    "proposal_id": proposal_id or None,
                    "status": status,
                    "active_policy_id": (active_policy or {}).get("id"),
                    "artifact_status": artifact_status or None,
                    "updated_at": (active_policy or policy).get("updated_at"),
                },
            }
        ), 200

    @bp.route("/api/v1/policies/<policy_id>", methods=["GET"])
    @user_auth_decorator
    def get_policy(policy_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policy = deps.db_get_policy(policy_id=policy_id, client_id=auth_session["client_id"])
        if not policy:
            return jsonify({"success": False, "error": "Policy not found"}), 404
        if str(request.args.get("view") or "").strip().lower() == "mobile":
            policy = _policy_mobile_view(policy)
        holdings = deps.db_list_holdings(client_id=auth_session["client_id"], policy_id=policy_id)
        return jsonify({"success": True, "policy": policy, "holdings": holdings}), 200

    @bp.route("/api/v1/holdings/import", methods=["POST"])
    @user_auth_decorator
    def import_holdings(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        result = external_accounts.connect("brokerage", body)
        holdings = [deps.db_save_holding(client_id=auth_session["client_id"], policy_id=body.get("policy_id"), status="imported", payload=holding) for holding in result.get("holdings") or []]
        _emit(deps, auth_session["client_id"], "holdings.imported", "holding", {"id": body.get("policy_id") or "external"}, {"count": len(holdings)})
        return jsonify({"success": True, "holdings": holdings, "provider_result": result}), 201

    @bp.route("/api/v1/holdings/<holding_id>", methods=["PATCH"])
    @user_auth_decorator
    def patch_holding(holding_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        holding = deps.db_update_holding(holding_id=holding_id, client_id=auth_session["client_id"], status=str(body.get("status") or "active"), payload_patch=body)
        if not holding:
            return jsonify({"success": False, "error": "Holding not found"}), 404
        return jsonify({"success": True, "holding": holding}), 200

    @bp.route("/api/v1/advisory/external-events", methods=["POST"])
    @user_auth_decorator
    def inject_market_event(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        client_id = auth_session["client_id"]
        policy_id = str(body.get("policy_id") or "")
        policy = deps.db_get_policy(policy_id=policy_id, client_id=client_id) if policy_id else (deps.db_list_policies(client_id=client_id, status="active") or [None])[0]
        if not policy:
            return jsonify({"success": False, "error": "Active policy not found"}), 404
        market_event = market.market_drop_event(body)
        proposal = deps.db_get_artifact(
            artifact_id=(policy.get("related_id") or (policy.get("payload") or {}).get("proposal_id")),
            client_id=client_id,
        ) if (policy.get("related_id") or (policy.get("payload") or {}).get("proposal_id")) else None
        holdings = deps.db_list_holdings(client_id=client_id, policy_id=policy["id"]) if hasattr(deps, "db_list_holdings") else []
        report_payload = monitoring_report_from_policy_sources(
            policy=policy,
            proposal=proposal,
            holdings=holdings or [],
            market_event=market_event,
        )
        report = deps.db_save_artifact(client_id=client_id, artifact_type="monitoring_report", title=report_payload["title"], payload=report_payload, related_type="policy", related_id=policy["id"])
        update_payload = agent.policy_update_artifact(policy, report)
        update = deps.db_save_artifact(client_id=client_id, artifact_type="policy_update", title=update_payload["title"], payload=update_payload, related_type="policy", related_id=policy["id"])
        alert = report_payload.get("risk_alert") or {}
        review_conversation, review_messages = _create_artifact_review_conversation(
            deps,
            client_id,
            report,
            mode="voice_guided",
        )
        guidance = _proactive_guidance_payload(
            report,
            conversation=review_conversation,
            messages=review_messages,
            related_artifacts=[update] if update else [],
        )
        update_review_conversation, update_review_messages = _create_artifact_review_conversation(
            deps,
            client_id,
            update,
            mode="voice_guided",
            conversation_id=str(review_conversation.get("id") or ""),
        )
        update_guidance = _proactive_guidance_payload(
            update,
            conversation=update_review_conversation,
            messages=update_review_messages,
            related_artifacts=[report] if report else [],
        )
        update["payload"]["guidance"] = update_guidance
        notification = _notification(
            "monitoring.risk_alert",
            "Market drop detected",
            guidance["opening_script"],
            "business_card",
            {
                "policy_id": policy["id"],
                "report_id": report["id"] if report else None,
                "policy_update_id": update["id"] if update else None,
                "severity": alert.get("severity"),
                "conversation_id": review_conversation.get("id"),
                "review_message_ids": [message.get("id") for message in [*review_messages, *update_review_messages]],
                "primary_action": "review_with_arc",
                "guidance": guidance,
                "policy_update_guidance": update_guidance,
            },
        )
        _emit(deps, client_id, "monitoring.report_generated", "monitoring_report", report, {"policy_id": policy["id"], "policy_update_id": update["id"] if update else None, "risk_alert": alert})
        _emit(deps, client_id, "monitoring.risk_alert_notification", "notification", {"id": notification["id"]}, notification)
        return jsonify({"success": True, "market_event": market_event, "report": report, "policy_update": update, "risk_alert": alert, "notification": notification}), 201

    @bp.route("/api/v1/monitoring/reports/<report_id>", methods=["GET"])
    @user_auth_decorator
    def get_report(report_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        report = deps.db_get_artifact(artifact_id=report_id, client_id=auth_session["client_id"])
        if not report:
            return jsonify({"success": False, "error": "Report not found"}), 404
        return jsonify({"success": True, "report": report}), 200

    @bp.route("/api/v1/monitoring/reports/<report_id>/questions", methods=["POST"])
    @user_auth_decorator
    def ask_report_question(report_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        return _artifact_question_response(deps, agent, auth_session["client_id"], report_id, body, not_found="Report not found")

    @bp.route("/api/v1/policy-updates/<update_id>", methods=["GET"])
    @user_auth_decorator
    def get_policy_update(update_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        update = deps.db_get_artifact(artifact_id=update_id, client_id=auth_session["client_id"])
        if not update:
            return jsonify({"success": False, "error": "Policy update not found"}), 404
        return jsonify({"success": True, "policy_update": update}), 200

    @bp.route("/api/v1/policy-updates/<update_id>/decisions", methods=["POST"])
    @user_auth_decorator
    def decide_policy_update(update_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        update = deps.db_get_artifact(artifact_id=update_id, client_id=auth_session["client_id"])
        if not update:
            return jsonify({"success": False, "error": "Policy update not found"}), 404
        decision = str(body.get("decision") or "discuss")
        policy = None
        revised_proposal = None
        if decision == "apply" and update.get("related_id"):
            policy = deps.db_update_policy(
                policy_id=update["related_id"],
                client_id=auth_session["client_id"],
                status="active",
                payload_patch={
                    "last_applied_policy_update_id": update_id,
                    "last_policy_update_decision": "apply",
                },
            )
        if decision == "revise" and update.get("related_id"):
            policy = deps.db_get_policy(policy_id=update["related_id"], client_id=auth_session["client_id"])
            if policy:
                revised_payload = agent.revised_proposal_artifact(policy, update, body)
                revised_proposal = deps.db_save_artifact(
                    client_id=auth_session["client_id"],
                    artifact_type="proposal",
                    title=revised_payload["title"],
                    payload=revised_payload,
                    related_type="policy_update",
                    related_id=update_id,
                    status="revision_ready",
                )
        _emit(deps, auth_session["client_id"], f"policy_update.{decision}", "policy_update", update, {"decision": decision, "revised_proposal_id": revised_proposal["id"] if revised_proposal else None})
        return jsonify({
            "success": True,
            "policy_update": update,
            "policy": policy,
            "revised_proposal": revised_proposal,
            "decision": {"decision": decision, "status": "recorded"},
        }), 201

    @bp.route("/api/v1/policies/<policy_id>/deactivation-request", methods=["POST"])
    @user_auth_decorator
    def request_deactivation(policy_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policy = deps.db_update_policy(policy_id=policy_id, client_id=auth_session["client_id"], status="deactivation_requested", payload_patch={"deactivation_requested": True})
        if not policy:
            return jsonify({"success": False, "error": "Policy not found"}), 404
        _emit(deps, auth_session["client_id"], "policy.deactivation_requested", "policy", policy, {})
        return jsonify({"success": True, "policy": policy}), 200

    @bp.route("/api/v1/policies/<policy_id>/deactivation-preview", methods=["GET"])
    @user_auth_decorator
    def deactivation_preview(policy_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policy = deps.db_get_policy(policy_id=policy_id, client_id=auth_session["client_id"])
        if not policy:
            return jsonify({"success": False, "error": "Policy not found"}), 404
        return jsonify({"success": True, "preview": {"policy_id": policy_id, "estimated_cash_after_exit": 98250, "tax_note": "Mock estimate only", "settlement": "immediate_mock"}}), 200

    @bp.route("/api/v1/policies/<policy_id>/deactivate", methods=["POST"])
    @user_auth_decorator
    def deactivate_policy(policy_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policy = deps.db_update_policy(policy_id=policy_id, client_id=auth_session["client_id"], status="closed", payload_patch={"closed_reason": (request.get_json(silent=True) or {}).get("reason"), "settlement_status": "settled"})
        if not policy:
            return jsonify({"success": False, "error": "Policy not found"}), 404
        _emit(deps, auth_session["client_id"], "policy.closed", "policy", policy, {"settlement_status": "settled"})
        return jsonify({"success": True, "policy": policy, "deactivation_status": "closed"}), 200

    @bp.route("/api/v1/policies/<policy_id>/deactivation-status", methods=["GET"])
    @user_auth_decorator
    def deactivation_status(policy_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        policy = deps.db_get_policy(policy_id=policy_id, client_id=auth_session["client_id"])
        if not policy:
            return jsonify({"success": False, "error": "Policy not found"}), 404
        return jsonify({"success": True, "policy_id": policy_id, "status": policy.get("status"), "settlement_status": (policy.get("payload") or {}).get("settlement_status")}), 200

    return bp


def _emit(deps: Any, client_id: str, event_type: str, aggregate_type: str, record: Dict[str, Any] | None, payload: Dict[str, Any]) -> None:
    aggregate_id = (record or {}).get("id") or payload.get("id")
    _remember_notification(client_id, event_type, aggregate_type, aggregate_id, payload)
    safe_publish_event(
        deps,
        event_key=f"{event_type}:{aggregate_id}",
        client_id=client_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        event_source="mvp_ui",
        payload=payload,
    )
    if hasattr(deps, "db_create_trace_event"):
        deps.db_create_trace_event(
            trace_id=f"tr_{aggregate_id}" if aggregate_id else None,
            client_id=client_id,
            case_id=aggregate_id,
            case_type=aggregate_type,
            source_type="mvp_ui",
            source_id=aggregate_id,
            event_type=event_type,
            event_name=event_type.replace(".", " "),
            actor_type="user_or_system",
            output_summary=str(payload)[:500],
            payload=payload,
            subjects=[{"subject_type": aggregate_type, "subject_id": aggregate_id, "relation": "updated"}] if aggregate_id else [],
        )


def _notification(event_type: str, title: str, body: str, notification_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    notification_id = _stable_notification_id(event_type, payload)
    return {
        "id": notification_id,
        "event_type": event_type,
        "notification_type": notification_type,
        "title": title,
        "body": body,
        "payload": payload,
        "status": "ready",
    }


def _stable_notification_id(event_type: str, payload: Dict[str, Any]) -> str:
    basis = f"{event_type}:{payload.get('artifact_id') or payload.get('report_id') or payload.get('policy_update_id') or payload.get('policy_id') or payload.get('id') or payload}"
    return f"notif_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:16]}"


def _remember_notification(client_id: str, event_type: str, aggregate_type: str, aggregate_id: Any, payload: Dict[str, Any]) -> None:
    event = {
        "id": _stable_notification_id(event_type, {"id": aggregate_id, **(payload or {})}),
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "payload": payload or {},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    notification = _notification_from_event(event)
    if not notification:
        return
    _MVP_NOTIFICATION_MEMORY.setdefault(client_id, {})[notification["id"]] = notification


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _preferred_language(user_text: str, known_context: Dict[str, Any]) -> str:
    existing = str(known_context.get("preferred_language") or "").lower()
    lower = user_text.lower()
    if existing in {"zh", "zh-cn", "chinese"} or _has_cjk(user_text) or "中文" in user_text or "chinese" in lower:
        return "zh"
    return "en"


def _is_language_request(user_text: str) -> bool:
    lower = user_text.lower()
    return any(term in lower for term in ["chinese", "mandarin"]) or any(term in user_text for term in ["中文", "中国话", "普通话"])


def _is_short_ack(user_text: str) -> bool:
    normalized = user_text.strip().lower().strip("。.!！?？,， ")
    return normalized in {
        "ok", "okay", "yes", "yep", "sure", "sounds good", "go ahead",
        "好的", "好", "可以", "行", "嗯", "嗯嗯", "对", "没错", "明白", "了解", "知道了",
    }


def _actions(kind: str, lang: str) -> list[Dict[str, str]]:
    if kind == "start":
        if lang == "zh":
            return [
                {"action_id": "start_household", "label": "先说家庭情况", "intent": "continue_onboarding", "suggested_response": "我们家有孩子，教育规划比较重要。"},
                {"action_id": "talk_investment", "label": "聊投资需求", "intent": "investment_discovery", "suggested_response": "我想看看一笔现金或股票收益怎么投资。"},
            ]
        return [
            {"action_id": "start_household", "label": "Start with household", "intent": "continue_onboarding", "suggested_response": "Family of four, with college planning becoming important."},
            {"action_id": "talk_investment", "label": "Talk investment", "intent": "investment_discovery", "suggested_response": "I may want to invest RSU or brokerage proceeds."},
        ]
    if kind == "household":
        if lang == "zh":
            return [
                {"action_id": "share_family", "label": "家庭和孩子", "intent": "goal_discovery", "suggested_response": "我们家有两个孩子，一个教育目标大概十年后开始。"},
                {"action_id": "share_cashflow", "label": "收入和现金流", "intent": "risk_deep_dive", "suggested_response": "我们每月大概能稳定结余一部分钱。"},
                {"action_id": "talk_investment", "label": "直接聊投资", "intent": "investment_discovery", "suggested_response": "我想用一笔闲置资金做投资规划。"},
            ]
        return [
            {"action_id": "share_family", "label": "Family and kids", "intent": "goal_discovery", "suggested_response": "We have kids, and one education goal starts in about ten years."},
            {"action_id": "share_cashflow", "label": "Income and cash flow", "intent": "risk_deep_dive", "suggested_response": "We have some stable monthly surplus."},
            {"action_id": "talk_investment", "label": "Talk investment", "intent": "investment_discovery", "suggested_response": "I want to plan an investment with idle cash."},
        ]
    if kind == "education":
        if lang == "zh":
            return [
                {"action_id": "detail_education", "label": "细化教育目标", "intent": "goal_discovery", "suggested_response": "大概十年后开始，每月可以存六百美元左右。"},
                {"action_id": "check_cashflow", "label": "先看现金流", "intent": "risk_deep_dive", "suggested_response": "先看现金流能不能支撑这个目标。"},
            ]
        return [
            {"action_id": "detail_education", "label": "Detail education goal", "intent": "goal_discovery", "suggested_response": "College starts in about 10 years, and we save around $600 per month."},
            {"action_id": "check_cashflow", "label": "Check cash flow", "intent": "risk_deep_dive", "suggested_response": "Let's check whether cash flow can support that goal."},
        ]
    if kind == "investment":
        if lang == "zh":
            return [
                {"action_id": "moderate_guardrails", "label": "中等风险，保留现金", "intent": "provide_guardrails", "suggested_response": "中等风险，应急备用金不要动。"},
                {"action_id": "lower_risk", "label": "风险低一点", "intent": "risk_preference", "suggested_response": "我希望风险低一点，分批投入。"},
                {"action_id": "pause_proposal", "label": "先不生成方案", "intent": "save_context", "suggested_response": "先把这些作为背景保存。"},
            ]
        return [
            {"action_id": "moderate_guardrails", "label": "Moderate risk, keep cash", "intent": "provide_guardrails", "suggested_response": "Moderate risk. Keep the emergency reserve untouched."},
            {"action_id": "lower_risk", "label": "Lower risk", "intent": "risk_preference", "suggested_response": "I would rather keep risk lower and invest gradually."},
            {"action_id": "pause_proposal", "label": "Not ready yet", "intent": "save_context", "suggested_response": "Let's save this as context for now."},
        ]
    if kind == "proposal":
        if lang == "zh":
            return [
                {"action_id": "prepare_draft_proposal", "label": "生成方案草稿", "intent": "prepare_proposal", "suggested_response": "生成一个方案草稿。"},
                {"action_id": "adjust_risk_first", "label": "先调低风险", "intent": "risk_preference", "suggested_response": "生成前先把风险调低，或者分批投入。"},
                {"action_id": "save_context", "label": "先保存", "intent": "save_context", "suggested_response": "先不生成，保存当前信息。"},
            ]
        return [
            {"action_id": "prepare_draft_proposal", "label": "Prepare draft proposal", "intent": "prepare_proposal", "suggested_response": "Prepare a draft proposal."},
            {"action_id": "adjust_risk_first", "label": "Adjust risk first", "intent": "risk_preference", "suggested_response": "Before drafting, let's lower the risk or stage the investment."},
            {"action_id": "save_context", "label": "Not now", "intent": "save_context", "suggested_response": "Not now. Save this context."},
        ]
    return []


def _orchestrate_onboarding_turn(
    user_text: str,
    state: str,
    known_context: Dict[str, Any],
    selected_action: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    selected_action = selected_action or {}
    selected_action_id = str(selected_action.get("action_id") or "")
    selected_intent = str(selected_action.get("intent") or "")
    lang = _preferred_language(user_text, known_context)
    text = user_text.lower()
    turns = int(known_context.get("turn_count") or 0)
    has_investment = any(term in text for term in ["invest", "rsu", "brokerage", "portfolio", "taxable", "$", "150,000", "stock"]) or any(term in user_text for term in ["投资", "股票", "基金", "理财", "组合", "闲置资金"])
    has_education = any(term in text for term in ["kid", "kids", "college", "education", "tuition", "school"]) or any(term in user_text for term in ["孩子", "教育", "大学", "学费", "学校"])
    has_liquidity = any(term in text for term in ["cash", "emergency", "reserve", "runway", "liquidity"]) or any(term in user_text for term in ["现金", "应急", "备用金", "流动性", "不要动", "不动"])
    has_risk = any(term in text for term in ["risk", "moderate", "conservative", "aggressive", "drawdown", "volatile", "volatility"]) or any(term in user_text for term in ["风险", "稳健", "保守", "激进", "波动", "回撤", "中等"])
    if selected_intent == "investment_discovery":
        has_investment = True
    if selected_intent == "goal_discovery":
        has_education = True
    detected_intents: list[str] = []
    detected_risks: list[str] = []
    if has_investment:
        detected_intents.append("investment_discovery")
    if has_education:
        detected_intents.append("education_goal")
    if has_liquidity:
        detected_risks.append("liquidity_guardrail")
    if "rsu" in text or "stock" in text:
        detected_risks.append("concentration_risk")
    if has_risk:
        detected_risks.append("risk_preference")

    merged = {
        **known_context,
        "has_investment_intent": bool(known_context.get("has_investment_intent") or has_investment),
        "has_education_goal": bool(known_context.get("has_education_goal") or has_education),
        "has_liquidity_guardrail": bool(known_context.get("has_liquidity_guardrail") or has_liquidity),
        "has_risk_preference": bool(known_context.get("has_risk_preference") or has_risk),
        "preferred_language": lang,
        "turn_count": turns + (1 if user_text else 0),
    }

    if not user_text:
        return {
            "state": "onboarding_understanding",
            "title": "Let us start with context",
            "assistant_message": "I will first understand your household, goals, cash needs, and risk boundaries. If an investment need shows up, I will ask the extra questions before preparing anything.",
            "detected_intents": [],
            "detected_risks": [],
            "known_context": merged,
            "next_actions": _actions("start", lang),
            "artifact_suggestion": None,
        }

    if _is_language_request(user_text):
        return {
            "state": state or "onboarding_understanding",
            "title": "Language preference updated",
            "assistant_message": "好的，接下来我用中文和你聊。我们先不用急着生成方案，我会像真人顾问一样先把家庭、现金流、目标和风险边界问清楚。你先简单说一下：家里几口人，最重要的目标是什么？",
            "detected_intents": detected_intents,
            "detected_risks": detected_risks,
            "known_context": merged,
            "next_actions": _actions("household", "zh"),
            "artifact_suggestion": None,
        }

    if selected_action_id in {"start_household", "continue_household"} and _is_short_ack(user_text):
        return {
            "state": "household_discovery",
            "title": "Household discovery",
            "assistant_message": (
                "好，我们先从家庭情况开始。你不用一次说很完整，先告诉我家里几口人、有没有孩子、未来一两个最重要的目标是什么。"
                if lang == "zh"
                else "Good. Let us start with the household. You do not need to make it perfect: tell me who is in the household, whether kids are part of the plan, and the one or two goals that matter most."
            ),
            "detected_intents": detected_intents,
            "detected_risks": detected_risks,
            "known_context": merged,
            "next_actions": _actions("household", lang),
            "artifact_suggestion": None,
        }

    if merged["has_investment_intent"] and not (merged["has_liquidity_guardrail"] and merged["has_risk_preference"]):
        missing = []
        if not merged["has_liquidity_guardrail"]:
            missing.append("what money should stay untouched")
        if not merged["has_risk_preference"]:
            missing.append("how much volatility feels acceptable")
        return {
            "state": "investment_discovery",
            "title": "Investment intent detected",
            "assistant_message": (
                "听起来已经进入投资规划了。先不急着出方案，我需要再确认两件事：哪些钱不能动，以及你能接受多大的波动。"
                if lang == "zh"
                else f"That sounds like an investment planning thread. Before I draft anything, I need to pin down {' and '.join(missing)}."
            ),
            "detected_intents": detected_intents or ["investment_discovery"],
            "detected_risks": detected_risks,
            "known_context": merged,
            "next_actions": _actions("investment", lang),
            "artifact_suggestion": None,
        }

    if merged["has_investment_intent"] and merged["has_liquidity_guardrail"] and merged["has_risk_preference"]:
        return {
            "state": "proposal_ready_to_prepare",
            "title": "Proposal draft is ready to prepare",
            "assistant_message": (
                "现在已经够生成一个方案草稿了。我会把应急现金排除在外，把可投资资金作为资金来源，风险先按中等偏稳处理，生成后我们再逐段讨论。"
                if lang == "zh"
                else "I have enough to prepare a draft proposal. I would keep emergency cash untouched, use the RSU or brokerage proceeds as the funding source, and keep the risk near moderate unless you tell me otherwise."
            ),
            "detected_intents": detected_intents or ["investment_discovery"],
            "detected_risks": list(dict.fromkeys(detected_risks + ["liquidity_guardrail", "risk_preference"])),
            "known_context": merged,
            "next_actions": _actions("proposal", lang),
            "artifact_suggestion": {"type": "proposal", "readiness": "ready"},
        }

    if merged["has_education_goal"]:
        return {
            "state": "goal_discovery",
            "title": "Education goal detected",
            "assistant_message": (
                "我听到教育规划这个目标了。下一步我想把三个点问清楚：大概几年后用钱、每月能稳定投入多少、这个目标要不要反过来限制投资风险。"
                if lang == "zh"
                else "I hear an education planning need. Next I want to clarify the timing, monthly savings capacity, and whether this goal should constrain investment risk."
            ),
            "detected_intents": detected_intents or ["education_goal"],
            "detected_risks": detected_risks,
            "known_context": merged,
            "next_actions": _actions("education", lang),
            "artifact_suggestion": None,
        }

    return {
        "state": "risk_discovery",
        "title": "Continue discovery",
        "assistant_message": (
            "现在还不用急着推方案。我们先继续补齐家庭、现金流和目标，我会边聊边判断最大的风险点或规划缺口在哪里。"
            if lang == "zh"
            else "I do not need to push a proposal yet. Based on what we know, I would keep learning the household context and look for the biggest risk or planning gap before recommending anything."
        ),
        "detected_intents": detected_intents,
        "detected_risks": detected_risks,
        "known_context": merged,
        "next_actions": _actions("household", lang),
        "artifact_suggestion": None,
    }


def _list_projected_notifications(deps: Any, client_id: str, limit: int) -> list[Dict[str, Any]]:
    events = []
    if hasattr(deps, "db_list_business_events"):
        events = deps.db_list_business_events(client_id=client_id, limit=max(limit * 2, 10)) or []
    notifications = [_notification_from_event(event) for event in events]
    notifications = [item for item in notifications if item]
    memory_items = list((_MVP_NOTIFICATION_MEMORY.get(client_id) or {}).values())
    by_id = {item["id"]: item for item in [*notifications, *memory_items]}
    return sorted(
        by_id.values(),
        key=lambda item: str(item.get("occurred_at") or ""),
        reverse=True,
    )[:limit]


def _resolve_arc_chat_entrypoint(
    deps: Any,
    agent: DeterministicAgentProvider,
    client_id: str,
) -> Dict[str, Any]:
    notifications = _list_projected_notifications(deps, client_id, 12)
    event_notification = next((item for item in notifications if (item.get("payload") or {}).get("conversation_id")), None)
    if event_notification:
        conversation_id = str((event_notification.get("payload") or {}).get("conversation_id"))
        conversation = deps.db_get_conversation(conversation_id=conversation_id, client_id=client_id)
        if conversation:
            messages = deps.db_list_conversation_messages(conversation_id=conversation_id, client_id=client_id)
            guidance = (event_notification.get("payload") or {}).get("guidance")
            opening = str((guidance or {}).get("opening_script") or event_notification.get("body") or "I found something worth reviewing with you.")
            return {
                "trigger_type": "event",
                "source": {
                    "notification_id": event_notification.get("id"),
                    "event_type": event_notification.get("event_type"),
                    "route": event_notification.get("route"),
                },
                "conversation": conversation,
                "messages": messages,
                "opening": opening,
                "next_best_question": str((guidance or {}).get("next_best_question") or "Do you want the quick readout or the details?"),
            }

    fallback = _resolve_risk_or_check_in_entrypoint(deps, agent, client_id)
    return fallback


def _resolve_risk_or_check_in_entrypoint(
    deps: Any,
    agent: DeterministicAgentProvider,
    client_id: str,
) -> Dict[str, Any]:
    artifact = _latest_entrypoint_artifact(deps, client_id)
    if artifact:
        artifact_id = str(artifact.get("id") or "")
        cache_key = f"risk:{artifact_id}"
        cached = (_MVP_ENTRYPOINT_MEMORY.get(client_id) or {}).get(cache_key)
        conversation_id = cached or ""
        conversation = deps.db_get_conversation(conversation_id=conversation_id, client_id=client_id) if conversation_id else None
        messages = deps.db_list_conversation_messages(conversation_id=conversation_id, client_id=client_id) if conversation else []
        if not conversation or not messages:
            conversation, messages = _create_artifact_review_conversation(
                deps,
                client_id,
                artifact,
                mode="entrypoint_risk_review",
                conversation_id=conversation_id,
            )
        _MVP_ENTRYPOINT_MEMORY.setdefault(client_id, {})[cache_key] = str(conversation.get("id") or "")
        guidance = _proactive_guidance_payload(artifact, conversation=conversation, messages=messages)
        artifact_type = str(artifact.get("record_type") or (artifact.get("payload") or {}).get("artifact_type") or "artifact")
        return {
            "trigger_type": "risk",
            "source": {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "route": _review_route(artifact_type),
            },
            "conversation": conversation,
            "messages": deps.db_list_conversation_messages(conversation_id=conversation["id"], client_id=client_id),
            "opening": guidance.get("opening_script"),
            "next_best_question": guidance.get("next_best_question"),
        }

    cache_key = "check_in:default"
    cached = (_MVP_ENTRYPOINT_MEMORY.get(client_id) or {}).get(cache_key)
    conversation = deps.db_get_conversation(conversation_id=cached, client_id=client_id) if cached else None
    if not conversation:
        conversation = deps.db_create_conversation(
            client_id=client_id,
            scope="awm_chat",
            context={"entrypoint": {"trigger_type": "check_in"}},
        )
        _MVP_ENTRYPOINT_MEMORY.setdefault(client_id, {})[cache_key] = str(conversation.get("id") or "")
        deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="assistant",
            message_type="text",
            text="Good to see you. I do not see anything that needs an urgent decision right now, so I can either review the plan, check risk, or answer whatever is on your mind.",
            payload={"trigger_type": "check_in"},
        )
        deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="assistant",
            message_type="decision_prompt",
            text="Where should we start?",
            payload={
                "title": "Where should we start?",
                "description": "I can do a quick health check or follow your lead.",
                "actions": [
                    {"action_id": "review_plan", "label": "Review plan", "intent": "discuss"},
                    {"action_id": "check_risk", "label": "Check risk", "intent": "discuss"},
                    {"action_id": "ask_anything", "label": "Ask anything", "intent": "discuss"},
                ],
            },
        )
    return {
        "trigger_type": "check_in",
        "source": {"route": "/monitoring"},
        "conversation": conversation,
        "messages": deps.db_list_conversation_messages(conversation_id=conversation["id"], client_id=client_id),
        "opening": "Good to see you. I do not see anything urgent right now, but I can still help you review the plan.",
        "next_best_question": "Do you want a plan review, a risk check, or something else?",
    }


def _latest_entrypoint_artifact(deps: Any, client_id: str) -> Dict[str, Any] | None:
    if not hasattr(deps, "db_list_artifacts"):
        return None
    for artifact_type in ("monitoring_report", "policy_update", "proposal"):
        artifacts = deps.db_list_artifacts(client_id=client_id, artifact_type=artifact_type) or []
        if artifact_type == "proposal":
            # Only a finalized, not-yet-executed proposal is worth re-announcing as
            # "ready to review". Skip pre-generation draft stubs and proposals that
            # have already been executed (those became an active policy).
            artifacts = [a for a in artifacts if str(a.get("status") or "") == "ready"]
        if artifacts:
            return artifacts[0]
    return None


def _projection_assumption_shadow(
    deps: Any,
    cashflow_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build non-blocking, opt-in audit metadata without altering model input."""

    from api.services.assumption_shadow import (
        build_cashflow_assumption_shadow,
        unavailable_cashflow_assumption_shadow,
    )
    from advisor.assumptions.resolver import assumption_integration_mode

    try:
        mode = assumption_integration_mode()
    except Exception:  # pylint: disable=broad-except
        return unavailable_cashflow_assumption_shadow(
            cashflow_payload=cashflow_payload,
            error_code="integration_mode_invalid",
        )
    if mode.value == "off":
        return None

    repository_factory = getattr(
        deps,
        "assumption_repository_factory",
        None,
    )
    preflight = None
    try:
        if not callable(repository_factory):
            return unavailable_cashflow_assumption_shadow(
                cashflow_payload=cashflow_payload,
                error_code="assumption_repository_unavailable",
            )
        repository = repository_factory()
        from api.services.assumption_preflight import (
            run_cashflow_assumption_preflight,
        )

        preflight = run_cashflow_assumption_preflight(
            cashflow_payload=cashflow_payload,
            repository=repository,
        ).model_dump(mode="json")
        report = build_cashflow_assumption_shadow(
            cashflow_payload=cashflow_payload,
            repository=repository,
            mode=mode,
        )
        if report is not None:
            report["preflight"] = preflight
        return report
    except Exception:  # pylint: disable=broad-except
        return unavailable_cashflow_assumption_shadow(
            cashflow_payload=cashflow_payload,
            preflight=preflight,
        )


def _projection_with_assumption_shadow(
    deps: Any,
    *,
    client_id: str,
    projection: Dict[str, Any],
    report: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist shadow metadata on a matching artifact when possible."""

    if report is None:
        return projection
    payload = (
        projection.get("payload")
        if isinstance(projection.get("payload"), dict)
        else {}
    )
    from advisor.assumptions.resolver import attach_shadow_assumption_report

    updated_payload = attach_shadow_assumption_report(payload, report)
    if updated_payload == payload:
        return projection
    local_projection = {**projection, "payload": updated_payload}
    updater = getattr(deps, "db_update_artifact", None)
    if not callable(updater) or not projection.get("id"):
        return local_projection
    try:
        persisted = updater(
            artifact_id=projection["id"],
            client_id=client_id,
            status=str(projection.get("status") or "ready"),
            payload_patch={
                "awm_input_contract": updated_payload["awm_input_contract"],
            },
        )
    except Exception:  # pylint: disable=broad-except
        return local_projection
    return persisted if isinstance(persisted, dict) else local_projection


def _projection_input_readiness(deps: Any, client_id: str) -> Dict[str, Any]:
    """Check Projection inputs before defaults can make missing facts look real."""
    sources: List[Dict[str, Any]] = []
    getter = getattr(deps, "db_get_latest_knowledge_snapshot", None)
    if callable(getter):
        try:
            snapshot = getter(client_id) or {}
        except Exception:  # pylint: disable=broad-except
            snapshot = {}
        snapshot_data = snapshot.get("snapshot_data") if isinstance(snapshot.get("snapshot_data"), dict) else {}
        cashflow_state = snapshot_data.get("cashflow_state")
        if isinstance(cashflow_state, dict):
            sources.append(cashflow_state)
    try:
        from api.services.client_state_view import build_client_state_view

        state_view = build_client_state_view(client_id)
        if isinstance(state_view, dict):
            sources.append(state_view)
    except Exception:  # pylint: disable=broad-except
        pass

    missing = []
    if not any(_projection_has_age(source) for source in sources):
        missing.append("age_or_date_of_birth")
    if not any(_projection_has_retirement_age(source) for source in sources):
        missing.append("retirement_age_or_assumption")
    if not any(_projection_has_income(source) for source in sources):
        missing.append("current_income")
    if not any(_projection_has_spending(source) for source in sources):
        missing.append("current_spending")
    if not any(_projection_has_balance_sheet(source) for source in sources):
        missing.append("balance_sheet_observation")
    return {"ready": not missing, "missing_fields": missing}


def _projection_has_age(source: Dict[str, Any]) -> bool:
    return _projection_number_at(source, ("client_profile", "age")) is not None or _projection_value_at(source, ("client_profile", "date_of_birth")) is not None or _projection_fact_number(source, ("age", "current_age", "client_age")) is not None


def _projection_has_retirement_age(source: Dict[str, Any]) -> bool:
    return _projection_number_at(source, ("client_profile", "retirement_age")) is not None or _projection_fact_number(source, ("retirement_age", "client_retirement_age")) is not None


def _projection_has_income(source: Dict[str, Any]) -> bool:
    return _projection_number_at(source, ("income", "salary")) is not None or _projection_fact_number(source, ("annual_income", "household_income", "income", "salary")) is not None


def _projection_has_spending(source: Dict[str, Any]) -> bool:
    return _projection_number_at(source, ("expenses", "base_spending")) is not None or _projection_fact_number(source, ("annual_spending", "base_spending")) is not None


def _projection_has_balance_sheet(source: Dict[str, Any]) -> bool:
    accounts = source.get("accounts") if isinstance(source.get("accounts"), dict) else {}
    for entries in accounts.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and _projection_number(entry.get("balance")) is not None:
                return True
    liabilities = source.get("liabilities") if isinstance(source.get("liabilities"), dict) else {}
    if any(_projection_number(value) is not None for value in liabilities.values()):
        return True
    return _projection_fact_number(
        source,
        (
            "cash",
            "cash_balance",
            "taxable_brokerage",
            "brokerage_accounts",
            "brokerage",
            "brokerage_balance",
            "retirement_accounts",
            "retirement_balance",
            "college_529",
            "mortgage",
            "mortgage_balance",
        ),
    ) is not None


def _projection_fact_number(source: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    for container_name in ("facts", "structured_facts"):
        container = source.get(container_name) if isinstance(source.get(container_name), dict) else {}
        for key in keys:
            number = _projection_number(container.get(key))
            if number is not None:
                return number
    for key in keys:
        number = _projection_number(source.get(key))
        if number is not None:
            return number
    return None


def _projection_number_at(source: Dict[str, Any], path: Tuple[str, str]) -> Optional[float]:
    parent = source.get(path[0]) if isinstance(source.get(path[0]), dict) else {}
    return _projection_number(parent.get(path[1]))


def _projection_value_at(source: Dict[str, Any], path: Tuple[str, str]) -> Any:
    parent = source.get(path[0]) if isinstance(source.get(path[0]), dict) else {}
    value = parent.get(path[1])
    return value if value not in (None, "") else None


def _projection_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _projection_artifacts_newest_first(deps: Any, client_id: str) -> List[Dict[str, Any]]:
    if not hasattr(deps, "db_list_artifacts"):
        return []
    try:
        artifacts = deps.db_list_artifacts(client_id=client_id, artifact_type="projection") or []
    except Exception:  # pylint: disable=broad-except
        return []
    return sorted(
        [item for item in artifacts if isinstance(item, dict)],
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("id") or ""),
        reverse=True,
    )


def _find_ready_projection(
    artifacts: List[Dict[str, Any]],
    *,
    input_fingerprint: Optional[str],
) -> Optional[Dict[str, Any]]:
    for artifact in artifacts:
        if not _projection_artifact_is_ready(artifact, require_fingerprint=input_fingerprint is not None):
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        if input_fingerprint is None or payload.get("input_fingerprint") == input_fingerprint:
            return artifact
    return None


def _projection_artifact_is_ready(record: Dict[str, Any], *, require_fingerprint: bool) -> bool:
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return False
    if payload.get("generation_source") != "real_cashflow_engine":
        return False
    if require_fingerprint and not payload.get("input_fingerprint"):
        return False
    if payload.get("schema_version") != "cashflow_projection.v3":
        return False
    native = _projection_native_payload(payload)
    if not native or native.get("status") == "not_available":
        return False
    # Reject sparse legacy mappings where Assets collapsed to cash-only while NW is larger.
    net_worth = native.get("netWorth") if isinstance(native.get("netWorth"), dict) else {}
    balance = native.get("balance") if isinstance(native.get("balance"), dict) else {}
    try:
        nw = float(net_worth.get("todayValue") or 0)
        assets = float(balance.get("assetsValue") or 0)
    except (TypeError, ValueError):
        nw = 0.0
        assets = 0.0
    if nw > 0 and assets > 0 and assets + 1 < nw * 0.5:
        return False
    ages = net_worth.get("ages") if isinstance(net_worth.get("ages"), list) else []
    if ages and len(set(int(a) for a in ages if a is not None)) <= 1 and len(ages) > 2:
        return False
    return bool(native.get("netWorth") and native.get("cashFlow"))


def _projection_native_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    for section in payload.get("sections") or []:
        if isinstance(section, dict) and section.get("section_id") == "native_projection":
            native = section.get("payload")
            return native if isinstance(native, dict) else {}
    return {}


def _projection_ready_response(
    projection: Dict[str, Any],
    *,
    status: str,
    input_fingerprint: str,
    snapshot_version: Any,
    warning: Optional[str] = None,
) -> Dict[str, Any]:
    payload = projection.get("payload") if isinstance(projection.get("payload"), dict) else {}
    generated_at = str(projection.get("updated_at") or projection.get("created_at") or "")
    response = {
        "success": True,
        "status": status,
        "projection": projection,
        "source": "real_cashflow_engine",
        "inputFingerprint": input_fingerprint,
        "knowledgeSnapshotVersion": snapshot_version,
        "generatedAt": generated_at,
    }
    if warning:
        response["warning"] = warning
    elif status == "stale":
        response["warning"] = "Showing the latest saved real projection because refresh failed."
    if payload.get("knowledge_snapshot_version"):
        response["knowledgeSnapshotVersion"] = payload["knowledge_snapshot_version"]
    return response


def _projection_failure_response(
    prior_projection: Optional[Dict[str, Any]],
    *,
    error_code: str,
    input_fingerprint: str,
    snapshot_version: Any,
    warning: Optional[str] = None,
) -> Dict[str, Any]:
    if prior_projection:
        payload = prior_projection.get("payload") if isinstance(prior_projection.get("payload"), dict) else {}
        return _projection_ready_response(
            prior_projection,
            status="stale",
            input_fingerprint=str(payload.get("input_fingerprint") or input_fingerprint),
            snapshot_version=payload.get("knowledge_snapshot_version") or snapshot_version,
            warning=warning,
        )
    return {
        "success": False,
        "status": "error",
        "projection": None,
        "errorCode": error_code,
        "retryable": error_code in {"engine_timeout", "engine_failed"},
    }

def _proposal_fields_from_snapshot(deps: Any, client_id: str) -> Dict[str, Any]:
    """Derive proposal inputs from the client's latest knowledge snapshot.

    The proposal must reflect what onboarding actually learned, not a fixture.
    We read the well-known wealth facts (amount, horizon, risk, constraints,
    funding source) and return only the keys the snapshot actually carries, so
    anything missing falls back to whatever the caller passed.
    """
    getter = getattr(deps, "db_get_latest_knowledge_snapshot", None)
    if not callable(getter):
        return {}
    try:
        snapshot = getter(client_id)
    except Exception:  # pylint: disable=broad-except
        return {}
    if not snapshot:
        return {}
    wealth = (snapshot.get("snapshot_data") or {}).get("wealth") or {}
    facts: Dict[str, Any] = {}
    for _category, group in wealth.items():
        if isinstance(group, dict):
            for label, fact in group.items():
                if isinstance(fact, dict) and "value" in fact:
                    facts[label] = fact["value"]

    fields: Dict[str, Any] = {}
    if facts.get("Amount to invest") is not None:
        fields["amount"] = facts["Amount to invest"]
    horizon = facts.get("Investment time horizon")
    if horizon is not None:
        fields["horizon"] = f"{horizon} years" if isinstance(horizon, (int, float)) else str(horizon)
    if facts.get("Investment risk tolerance"):
        fields["risk"] = str(facts["Investment risk tolerance"])

    constraints = []
    complexity = facts.get("Product complexity preference")
    if isinstance(complexity, str) and ("no complex" in complexity.lower() or "crypto" in complexity.lower()):
        constraints.append("Avoid crypto and complex products")
    if facts.get("Emergency reserve preference") or facts.get("Emergency cash balance"):
        constraints.append("Keep emergency reserve untouched")
    if constraints:
        fields["constraints"] = constraints

    if facts.get("Net worth growth dependence on RSUs") or facts.get("Company stock RSUs"):
        fields["funding_source"] = "vested RSU sale"

    return fields


def _proposal_fields_from_pool(deps: Any, client_id: str, pool_ref: str) -> Dict[str, Any]:
    """Derive proposal inputs from one money pool (matched by id or label), so a
    proposal can be generated for that specific pot of money — its own amount,
    horizon, risk, and constraints — rather than the client's whole picture.

    Returns the fields plus a `_pool` marker {id,label} the caller uses to tag
    the artifact. Empty dict if no pool matches.
    """
    ref = str(pool_ref or "").strip()
    if not ref:
        return {}
    getter = getattr(deps, "db_list_money_pools", None)
    if not callable(getter):
        try:
            from api.persistence import list_money_pools as getter  # type: ignore
        except Exception:  # pylint: disable=broad-except
            return {}
    try:
        pools = getter(client_id)
    except Exception:  # pylint: disable=broad-except
        return {}
    ref_l = ref.lower()
    match = next(
        (p for p in pools if str(p.get("id")) == ref or str(p.get("label", "")).strip().lower() == ref_l),
        None,
    )
    if not match:
        return {}

    fields: Dict[str, Any] = {"_pool": {"id": match.get("id"), "label": match.get("label")}}
    if match.get("amount") is not None:
        fields["amount"] = match["amount"]
    if match.get("risk_tolerance"):
        fields["risk"] = str(match["risk_tolerance"])
    horizon_date = match.get("horizon_date")
    if horizon_date:
        fields["horizon"] = str(horizon_date)
    elif isinstance((match.get("payload") or {}), dict) and match["payload"].get("horizon_text"):
        fields["horizon"] = str(match["payload"]["horizon_text"])
    constraints = (match.get("constraints") or {}) if isinstance(match.get("constraints"), dict) else {}
    excl = constraints.get("exclusions")
    cons_list = []
    if isinstance(excl, list) and excl:
        cons_list.append(f"Avoid {', '.join(str(e) for e in excl)}")
    if constraints.get("liquidity_floor"):
        cons_list.append("Keep a liquidity reserve")
    if cons_list:
        fields["constraints"] = cons_list
    if match.get("funding_source"):
        fields["funding_source"] = str(match["funding_source"])
    return fields


def _build_proposal_artifact(
    deps: Any,
    mock_agent: DeterministicAgentProvider,
    journey_id: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    provider = _proposal_provider(deps, mock_agent)
    return provider.proposal_artifact({"id": journey_id, "collected_fields": fields})


def _proposal_provider(deps: Any, mock_agent: DeterministicAgentProvider) -> Any:
    factory = getattr(deps, "mvp_proposal_provider_factory", None)
    if callable(factory):
        return factory()
    if _advisory_provider_mode(deps) == "real":
        return RealModelAgentProvider()
    return mock_agent


def _advisory_provider_mode(deps: Any) -> str:
    raw = getattr(deps, "advisory_provider_mode", None) or os.getenv("AWM_ADVISORY_PROVIDER", "mock")
    mode = str(raw or "mock").strip().lower()
    return "real" if mode in {"real", "asset_allocation", "model", "models"} else "mock"


def _create_artifact_review_conversation(
    deps: Any,
    client_id: str,
    artifact: Dict[str, Any],
    *,
    mode: str,
    conversation_id: str = "",
) -> Tuple[Dict[str, Any], list[Dict[str, Any]]]:
    artifact_id = str(artifact.get("id") or "")
    artifact_type = str(artifact.get("record_type") or (artifact.get("payload") or {}).get("artifact_type") or "artifact")
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
    title = str(payload.get("title") or artifact.get("title") or _review_title(artifact_type))
    focus_sections = _review_focus_sections(payload, artifact_type)
    review_context = {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "focus_sections": focus_sections,
        "mode": mode,
    }
    conversation = None
    if conversation_id and hasattr(deps, "db_get_conversation"):
        conversation = deps.db_get_conversation(conversation_id=conversation_id, client_id=client_id)
    if not conversation:
        conversation = deps.db_create_conversation(
            client_id=client_id,
            scope="awm_chat",
            context={"active_review": review_context},
        )
    messages: list[Dict[str, Any]] = []

    def add_message(message_type: str, text: str | None, payload_patch: Dict[str, Any], attachments: list[Dict[str, Any]] | None = None) -> None:
        messages.append(deps.db_add_conversation_message(
            conversation_id=conversation["id"],
            client_id=client_id,
            role="assistant",
            message_type=message_type,
            text=text,
            attachments=attachments or [],
            payload=payload_patch,
        ))

    add_message(
        "business_card",
        _review_intro_text(artifact_type, title),
        {
            "title": title,
            "body": _review_body(payload, artifact_type),
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "primary_action": "review_with_arc",
            "route": _review_route(artifact_type),
            "guidance": _proactive_guidance_payload(artifact, conversation=conversation, messages=[]),
        },
    )
    add_message(
        "attachment_context",
        "I'll use the highlighted sections as context while we talk this through.",
        {"artifact_type": artifact_type, "section_ids": focus_sections},
        [{"artifact_id": artifact_id, "section_id": section_id} for section_id in focus_sections],
    )
    chart_payload = _review_chart_payload(payload, artifact_type)
    if chart_payload:
        add_message("chart_card", chart_payload.get("title"), chart_payload)
    add_message(
        "decision_prompt",
        "What would you like to do first?",
        {
            "title": _review_decision_title(artifact_type),
            "description": _review_decision_description(artifact_type),
            "actions": _review_actions(artifact_type, artifact_id=artifact_id),
        },
    )
    return conversation, messages


def _execute_review_action(
    deps: Any,
    agent: DeterministicAgentProvider,
    client_id: str,
    conversation_id: str,
    action: Dict[str, Any],
) -> Dict[str, Any]:
    intent = str(action.get("intent") or "").strip()
    action_id = str(action.get("action_id") or "").strip()
    artifact_id = str(action.get("artifact_id") or "").strip()
    section_id = str(action.get("section_id") or "hero").strip() or "hero"
    messages: list[Dict[str, Any]] = []

    def add(message_type: str, text: str, payload: Dict[str, Any] | None = None, attachments: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        message = deps.db_add_conversation_message(
            conversation_id=conversation_id,
            client_id=client_id,
            role="assistant",
            message_type=message_type,
            text=text,
            attachments=attachments or [],
            payload=payload or {},
        )
        messages.append(message)
        return message

    artifact = deps.db_get_artifact(artifact_id=artifact_id, client_id=client_id) if artifact_id else None
    if intent == "explain" and artifact:
        answer = agent.answer_question(
            {"id": artifact_id, **(artifact.get("payload") or {})},
            section_id,
            f"Explain {section_id} in plain English.",
        )
        citations = answer.get("citations") if isinstance(answer.get("citations"), list) else []
        for citation in citations:
            if isinstance(citation, dict) and not citation.get("artifact_id"):
                citation["artifact_id"] = artifact_id
        add(
            "text",
            _humanize_explanation(answer.get("answer") or "", action, artifact),
            {"action": action, "answer": answer},
            citations,
        )
        add(
            "decision_prompt",
            "The next useful step is to decide whether we should simply keep watching or inspect the prepared update.",
            {
                "title": "What should we do next?",
                "description": "I can open the update, compare choices, or leave the policy unchanged for now.",
                "actions": _follow_up_actions_after_explain(artifact, action),
            },
        )
        return {"messages": messages, "action_result": {"status": "explained", "artifact_id": artifact_id, "section_id": section_id}}

    if intent == "execute":
        add(
            "text",
            "That is the right moment to slow down and confirm. I can take you to authorization, and before anything is submitted you will still see the order summary.",
            {"action": action, "route": "/execution"},
        )
        return {"messages": messages, "action_result": {"status": "ready_for_execution", "route": "/execution"}}

    if intent == "revise" and (action_id in {"lower_risk", "invest_gradually", "revise_update"} or artifact):
        constraint = str(action.get("constraint") or action_id)
        if action_id == "revise_update":
            constraint = "client_review_revision"
        revised_proposal = _create_revised_proposal_from_action(
            deps,
            agent,
            client_id,
            artifact,
            action,
            constraint,
        )
        add(
            "task_status",
            "I treated that as a revision request, not an approval.",
            {
                "action": action,
                "state": "revision_ready" if revised_proposal else "revision_requested",
                "constraint": constraint,
                "revised_proposal_id": (revised_proposal or {}).get("id"),
                "steps": [
                    {"label": "Keep the original goal and funding source", "status": "completed"},
                    {"label": "Apply your new preference", "status": "completed"},
                    {"label": "Prepare a revised proposal", "status": "completed" if revised_proposal else "pending"},
                ],
            },
        )
        add(
            "text",
            (
                "I prepared a revised proposal to compare against the current one. "
                "I kept the same household guardrails, but adjusted the recommendation around your new preference."
                if revised_proposal
                else "I would revise the proposal before moving toward execution. I will keep the same household guardrails and make the next version easier to compare against this one."
            ),
            {
                "action": action,
                "revision_constraint": constraint,
                "revised_proposal_id": (revised_proposal or {}).get("id"),
                "route": "/proposal" if revised_proposal else None,
            },
        )
        if revised_proposal:
            add(
                "business_card",
                "I put the revised version in the proposal area so we can compare it cleanly before you approve anything.",
                {
                    "title": revised_proposal.get("title") or (revised_proposal.get("payload") or {}).get("title") or "Revised proposal",
                    "body": "This is a revision for comparison, not an executed change.",
                    "primary_action": "open_revised_proposal",
                    "route": "/proposal",
                    "artifact_id": revised_proposal.get("id"),
                    "artifact_type": "proposal",
                },
            )
        return {
            "messages": messages,
            "action_result": {
                "status": "revision_ready" if revised_proposal else "revision_requested",
                "constraint": constraint,
                "revised_proposal_id": (revised_proposal or {}).get("id"),
            },
            "revised_proposal": revised_proposal,
        }

    if intent == "apply_policy_update" and artifact:
        decision = _apply_policy_update_action(deps, client_id, artifact_id, artifact)
        add(
            "task_status",
            "I recorded that decision against the active policy.",
            {
                "action": action,
                "state": "policy_update_applied",
                "steps": [
                    {"label": "Confirm the update artifact", "status": "completed"},
                    {"label": "Write the policy decision", "status": "completed"},
                    {"label": "Keep monitoring the same guardrails", "status": "completed"},
                ],
            },
        )
        add(
            "text",
            "I applied the update to the active policy record. I would still keep watching the same guardrails and will bring this back up if the signal changes again.",
            {"action": action, "decision": decision},
        )
        return {"messages": messages, "action_result": {"status": "applied", "decision": decision}}

    if intent == "defer":
        add(
            "task_status",
            "I left the policy unchanged and kept the monitoring watch active.",
            {
                "action": action,
                "state": "watching",
                "steps": [
                    {"label": "No policy change made", "status": "completed"},
                    {"label": "Alert remains on watch", "status": "completed"},
                    {"label": "Bring this back if guardrails tighten", "status": "completed"},
                ],
            },
        )
        add(
            "text",
            "That is reasonable. I will leave the policy as-is and keep monitoring. If this becomes more than noise, I will bring it back with the relevant context.",
            {"action": action},
        )
        return {"messages": messages, "action_result": {"status": "deferred"}}

    if intent == "open_related_update":
        related_update = _related_policy_update_for_report(deps, client_id, artifact)
        if related_update:
            add(
                "text",
                "Yes. I pulled the prepared update into this same conversation so we can review it without losing the market context.",
                {"action": action, "related_policy_update_id": related_update.get("id")},
            )
            _, review_messages = _create_artifact_review_conversation(
                deps,
                client_id,
                related_update,
                mode="conversation_action_follow_up",
                conversation_id=conversation_id,
            )
            messages.extend(review_messages)
            return {
                "messages": messages,
                "action_result": {
                    "status": "related_update_opened",
                    "route": "/policy-update",
                    "policy_update_id": related_update.get("id"),
                },
            }
        add(
            "text",
            "I do not see a separate update artifact for this report yet. We can still talk through whether to keep watching or prepare a revision.",
            {"action": action},
        )
        return {"messages": messages, "action_result": {"status": "related_update_missing"}}

    if intent == "discuss":
        add(
            "text",
            "Let us talk it through before taking action. I can compare the current policy, the proposed update, and the choice to wait.",
            {"action": action},
        )
        add(
            "decision_prompt",
            "Which comparison would help most?",
            {
                "title": "Compare the choices",
                "description": "We can inspect the update, keep watching, or revise the plan if the risk no longer feels right.",
                "actions": _follow_up_actions_after_explain(artifact, action),
            },
        )
        return {"messages": messages, "action_result": {"status": "discussion_ready"}}

    add(
        "text",
        "I can help with that. Let us keep the current proposal and policy context on the table while we work through the decision.",
        {"action": action},
    )
    return {"messages": messages, "action_result": {"status": "recorded"}}


def _apply_policy_update_action(
    deps: Any,
    client_id: str,
    update_id: str,
    update: Dict[str, Any],
) -> Dict[str, Any]:
    policy = None
    if update.get("related_id"):
        policy = deps.db_update_policy(
            policy_id=update["related_id"],
            client_id=client_id,
            status="active",
            payload_patch={
                "last_applied_policy_update_id": update_id,
                "last_policy_update_decision": "apply",
            },
        )
    _emit(
        deps,
        client_id,
        "policy_update.apply",
        "policy_update",
        update,
        {"decision": "apply", "source": "conversation_action"},
    )
    return {"decision": "apply", "policy_id": (policy or {}).get("id"), "status": "recorded"}


def _related_policy_update_for_report(deps: Any, client_id: str, report: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not report or not hasattr(deps, "db_list_artifacts"):
        return None
    policy_id = report.get("related_id")
    if not policy_id:
        return None
    updates = deps.db_list_artifacts(client_id=client_id, artifact_type="policy_update", related_id=policy_id) or []
    return updates[0] if updates else None


def _follow_up_actions_after_explain(artifact: Dict[str, Any] | None, action: Dict[str, Any]) -> list[Dict[str, Any]]:
    artifact = artifact or {}
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
    artifact_type = str(artifact.get("record_type") or payload.get("artifact_type") or "")
    artifact_id = str(artifact.get("id") or action.get("artifact_id") or "")
    if artifact_type == "monitoring_report":
        return [
            {"action_id": "open_update", "label": "Open update", "intent": "open_related_update", "artifact_id": artifact_id},
            {"action_id": "discuss_options", "label": "Compare options", "intent": "discuss", "artifact_id": artifact_id},
            {"action_id": "keep_watching", "label": "Keep watching", "intent": "defer", "artifact_id": artifact_id},
        ]
    if artifact_type == "policy_update":
        return [
            {"action_id": "apply_update", "label": "Apply update", "intent": "apply_policy_update", "artifact_id": artifact_id},
            {"action_id": "revise_update", "label": "Revise", "intent": "revise", "artifact_id": artifact_id},
            {"action_id": "keep_watching", "label": "Keep watching", "intent": "defer", "artifact_id": artifact_id},
        ]
    if artifact_type == "proposal":
        return [
            {"action_id": "lower_risk", "label": "Lower risk", "intent": "revise", "artifact_id": artifact_id, "constraint": "lower_risk"},
            {"action_id": "invest_gradually", "label": "Invest gradually", "intent": "revise", "artifact_id": artifact_id, "constraint": "staged_execution"},
            {"action_id": "authorize", "label": "Authorize", "intent": "execute", "artifact_id": artifact_id},
        ]
    return [{"action_id": "discuss_options", "label": "Discuss options", "intent": "discuss", "artifact_id": artifact_id}]


def _create_revised_proposal_from_action(
    deps: Any,
    agent: DeterministicAgentProvider,
    client_id: str,
    artifact: Dict[str, Any] | None,
    action: Dict[str, Any],
    constraint: str,
) -> Dict[str, Any] | None:
    if not artifact:
        return None
    artifact_type = str(artifact.get("record_type") or (artifact.get("payload") or {}).get("artifact_type") or "")
    if artifact_type == "proposal":
        source_policy = {
            "id": (artifact.get("related_id") or artifact.get("id")),
            "related_id": artifact.get("related_id") or artifact.get("id"),
            "payload": {"title": (artifact.get("payload") or {}).get("title")},
        }
        source_update = {"id": f"conversation_action:{action.get('action_id') or constraint}"}
    elif artifact_type == "policy_update" and artifact.get("related_id"):
        source_policy = deps.db_get_policy(policy_id=artifact["related_id"], client_id=client_id)
        source_update = artifact
    else:
        return None
    if not source_policy:
        return None
    instruction = _revision_instruction_from_action(action, constraint)
    revised_payload = agent.revised_proposal_artifact(
        source_policy,
        source_update,
        {"instruction": instruction, "source_action": action},
    )
    revised_payload["revision"]["source_action"] = action
    revised_payload["revision"]["constraint"] = constraint
    _apply_revision_constraint_to_payload(revised_payload, constraint)
    revised = deps.db_save_artifact(
        client_id=client_id,
        artifact_type="proposal",
        title=revised_payload["title"],
        payload=revised_payload,
        related_type="conversation_action",
        related_id=str(action.get("action_id") or constraint),
        status="revision_ready",
    )
    _emit(
        deps,
        client_id,
        "proposal.revised_from_conversation_action",
        "artifact",
        revised,
        {
            "source_artifact_id": artifact.get("id"),
            "constraint": constraint,
            "action": action,
        },
    )
    return revised


def _revision_instruction_from_action(action: Dict[str, Any], constraint: str) -> str:
    if constraint == "lower_risk":
        return "Client asked to lower risk before approval."
    if constraint == "staged_execution":
        return "Client asked to invest gradually instead of all at once."
    return str(action.get("label") or constraint)


def _apply_revision_constraint_to_payload(revised_payload: Dict[str, Any], constraint: str) -> None:
    sections = revised_payload.get("sections")
    if not isinstance(sections, list):
        return
    if constraint == "staged_execution":
        staged_section = {
            "section_id": "execution_schedule",
            "title": "Execution Schedule",
            "payload": {
                "mode": "staged",
                "summary": "Invest gradually instead of all at once.",
                "tranches": [
                    {"label": "Initial tranche", "weight": 0.25, "timing": "after authorization"},
                    {"label": "Second tranche", "weight": 0.25, "timing": "30 days later"},
                    {"label": "Third tranche", "weight": 0.25, "timing": "60 days later"},
                    {"label": "Final tranche", "weight": 0.25, "timing": "90 days later"},
                ],
                "pause_conditions": [
                    "market drawdown breaches policy guardrail",
                    "client changes liquidity preference",
                    "major household goal change",
                ],
            },
        }
        sections.append(staged_section)
        revised_payload["section_ids"] = [section["section_id"] for section in sections if isinstance(section, dict)]
        for section in sections:
            if isinstance(section, dict) and section.get("section_id") == "hero":
                payload = section.get("payload") if isinstance(section.get("payload"), dict) else {}
                payload["summary"] = "Updated to invest gradually while keeping the original household guardrails."
                section["payload"] = payload
        return

    if constraint == "lower_risk":
        for section in sections:
            if not isinstance(section, dict):
                continue
            if section.get("section_id") == "portfolio_analytics":
                payload = section.get("payload") if isinstance(section.get("payload"), dict) else {}
                expected_volatility = payload.get("expected_volatility")
                if isinstance(expected_volatility, (int, float)):
                    payload["expected_volatility"] = round(float(expected_volatility) * 0.85, 6)
                payload["revision_note"] = "Risk reduced per client request before approval."
                section["payload"] = payload
            if section.get("section_id") == "governance":
                payload = section.get("payload") if isinstance(section.get("payload"), dict) else {}
                triggers = payload.get("review_triggers") if isinstance(payload.get("review_triggers"), list) else []
                payload["review_triggers"] = [*triggers, "client-requested lower risk posture"]
                section["payload"] = payload


def _humanize_explanation(answer: str, action: Dict[str, Any], artifact: Dict[str, Any] | None = None) -> str:
    label = str(action.get("label") or "that part").lower()
    section_id = str(action.get("section_id") or "")
    payload = (artifact or {}).get("payload") if isinstance((artifact or {}).get("payload"), dict) else {}
    artifact_type = str((artifact or {}).get("record_type") or payload.get("artifact_type") or "")
    if artifact_type == "monitoring_report" and section_id == "risk_stats":
        risk = _payload_section(payload, "risk_stats")
        current = risk.get("current_drawdown")
        guardrail = risk.get("guardrail")
        if isinstance(current, (int, float)) and isinstance(guardrail, (int, float)):
            return (
                f"The portfolio is down about {abs(float(current)) * 100:.1f}% in this scenario, "
                f"while the guardrail allows up to {abs(float(guardrail)) * 100:.1f}%. "
                "So I would treat this as a watch item, not an automatic reason to abandon the policy."
            )
    if artifact_type == "policy_update" and section_id == "expected_impact":
        impact = _payload_section(payload, "expected_impact")
        summary = impact.get("summary") or impact.get("description")
        if summary:
            return f"The practical point is this: {summary} I would talk through that trade-off before asking you to approve anything."
    if not answer:
        return f"Let me unpack {label} in plain English."
    clean = answer.replace("the MVP answer is deterministic:", "").replace("The MVP answer is deterministic:", "")
    clean = clean.replace(" is explained by the section payload and current policy context.", " depends on the current policy context.")
    return f"Here is how I would explain {label}: {clean}"


def _proactive_guidance_payload(
    artifact: Dict[str, Any] | None,
    *,
    conversation: Dict[str, Any] | None,
    messages: list[Dict[str, Any]],
    related_artifacts: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    artifact = artifact or {}
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
    artifact_id = str(artifact.get("id") or "")
    artifact_type = str(artifact.get("record_type") or payload.get("artifact_type") or "artifact")
    title = str(payload.get("title") or artifact.get("title") or _review_title(artifact_type))
    focus_sections = _review_focus_sections(payload, artifact_type)
    source_refs = [{"type": "artifact", "id": artifact_id, "section_id": section_id} for section_id in focus_sections]
    for related in related_artifacts or []:
        related_payload = related.get("payload") if isinstance(related.get("payload"), dict) else {}
        source_refs.append({
            "type": "artifact",
            "id": related.get("id"),
            "artifact_type": related.get("record_type") or related_payload.get("artifact_type"),
        })
    return {
        "opening_script": _review_intro_text(artifact_type, title),
        "why_now": _review_why_now(payload, artifact_type),
        "next_best_question": _review_next_best_question(artifact_type),
        "focus_sections": focus_sections,
        "source_refs": source_refs,
        "structured_actions": _review_actions(artifact_type, artifact_id=artifact_id),
        "conversation_id": (conversation or {}).get("id"),
        "review_message_ids": [message.get("id") for message in messages],
        "idempotency_key": f"artifact_review:{artifact_id}:{artifact_type}:{payload.get('schema_version') or artifact.get('updated_at') or 'current'}",
        "voice_mode": "brief_guided_review",
    }


def _review_focus_sections(payload: Dict[str, Any], artifact_type: str) -> list[str]:
    available = payload.get("section_ids") if isinstance(payload.get("section_ids"), list) else []
    preferred = {
        "proposal": ["hero", "allocation", "portfolio_analytics", "governance"],
        "policy_update": ["signals", "trade_recommendations", "allocation_shift", "expected_impact"],
        "monitoring_report": ["hero", "risk_stats", "recommendation"],
    }.get(artifact_type, ["hero"])
    return [section_id for section_id in preferred if section_id in available] or [str(item) for item in available[:3]]


def _review_intro_text(artifact_type: str, title: str) -> str:
    if artifact_type == "proposal":
        return f"I have your proposal ready. Before you decide anything, I can walk you through the allocation, the trade-offs, and what I would watch."
    if artifact_type == "policy_update":
        return "I prepared an update we should talk through. It is not urgent to accept blindly; I can explain what changed, what I would adjust, and what happens if we wait."
    if artifact_type == "monitoring_report":
        return "I noticed a market move that touched your active policy, so I pulled together the report. Let me help you separate signal from noise."
    return f"I have {title} ready. I can help you review the important parts."


def _review_body(payload: Dict[str, Any], artifact_type: str) -> str:
    hero = _payload_section(payload, "hero")
    if hero.get("summary"):
        return str(hero.get("summary"))
    if hero.get("headline"):
        return str(hero.get("headline"))
    if artifact_type == "proposal":
        return "I focused on the amount, risk level, and guardrails we have been discussing, then turned that into an investable proposal."
    if artifact_type == "policy_update":
        return "The update is meant to respond to the monitoring signal without losing the original policy objective."
    return "I prepared this for review."


def _review_chart_payload(payload: Dict[str, Any], artifact_type: str) -> Dict[str, Any] | None:
    if artifact_type == "proposal":
        analytics = _payload_section(payload, "portfolio_analytics")
        current = analytics.get("expected_volatility")
        if isinstance(current, (int, float)):
            return {"title": "Expected risk", "current": round(float(current) * 100.0, 2), "limit": 20, "unit": "%"}
    if artifact_type == "monitoring_report":
        risk = _payload_section(payload, "risk_stats")
        current = risk.get("current_drawdown")
        guardrail = risk.get("guardrail")
        if isinstance(current, (int, float)) and isinstance(guardrail, (int, float)):
            return {"title": "Drawdown guardrail", "current": -round(float(current) * 100.0, 2), "limit": -round(float(guardrail) * 100.0, 2), "unit": "%"}
    return None


def _payload_section(payload: Dict[str, Any], section_id: str) -> Dict[str, Any]:
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    for section in sections:
        if isinstance(section, dict) and section.get("section_id") == section_id:
            section_payload = section.get("payload")
            return section_payload if isinstance(section_payload, dict) else {}
    return {}


def _review_actions(artifact_type: str, *, artifact_id: str = "") -> list[Dict[str, Any]]:
    if artifact_type == "proposal":
        return [
            {"action_id": "explain_allocation", "label": "Explain allocation", "intent": "explain", "artifact_id": artifact_id, "section_id": "allocation"},
            {"action_id": "lower_risk", "label": "Lower risk", "intent": "revise", "artifact_id": artifact_id, "constraint": "lower_risk"},
            {"action_id": "invest_gradually", "label": "Invest gradually", "intent": "revise", "artifact_id": artifact_id, "constraint": "staged_execution"},
            {"action_id": "authorize", "label": "Authorize", "intent": "execute", "artifact_id": artifact_id},
        ]
    if artifact_type == "policy_update":
        return [
            {"action_id": "explain_changes", "label": "Explain changes", "intent": "explain", "artifact_id": artifact_id, "section_id": "expected_impact"},
            {"action_id": "apply_update", "label": "Apply update", "intent": "apply_policy_update", "artifact_id": artifact_id},
            {"action_id": "revise_update", "label": "Revise", "intent": "revise", "artifact_id": artifact_id},
            {"action_id": "keep_watching", "label": "Keep watching", "intent": "defer", "artifact_id": artifact_id},
        ]
    if artifact_type == "monitoring_report":
        return [
            {"action_id": "explain_risk", "label": "Explain risk", "intent": "explain", "artifact_id": artifact_id, "section_id": "risk_stats"},
            {"action_id": "open_update", "label": "Open update", "intent": "open_related_update", "artifact_id": artifact_id},
            {"action_id": "discuss_options", "label": "Discuss options", "intent": "discuss", "artifact_id": artifact_id},
        ]
    return [
        {"action_id": "explain", "label": "Explain", "intent": "explain", "artifact_id": artifact_id},
        {"action_id": "discuss", "label": "Discuss", "intent": "discuss", "artifact_id": artifact_id},
        {"action_id": "save_for_later", "label": "Save for later", "intent": "defer", "artifact_id": artifact_id},
    ]


def _review_why_now(payload: Dict[str, Any], artifact_type: str) -> str:
    hero = _payload_section(payload, "hero")
    if artifact_type == "proposal":
        return str(hero.get("summary") or "You asked about putting money to work while preserving the household guardrails.")
    if artifact_type == "monitoring_report":
        return str(hero.get("summary") or hero.get("headline") or "A market move affected the active policy enough that it is worth reviewing together.")
    if artifact_type == "policy_update":
        return "The monitoring report found something actionable enough to prepare an update, but the decision should still be yours."
    return "There is a new item worth reviewing together."


def _review_next_best_question(artifact_type: str) -> str:
    if artifact_type == "proposal":
        return "Would it help if I explain the allocation first, make it more conservative, stage the investment, or move toward authorization?"
    if artifact_type == "monitoring_report":
        return "Do you want the quick risk readout, the proposed update, or a plain-English comparison of our options?"
    if artifact_type == "policy_update":
        return "Should I explain the changes, apply them, revise them, or keep watching for now?"
    return "Do you want me to explain this, discuss options, or leave it for later?"


def _review_decision_title(artifact_type: str) -> str:
    if artifact_type == "proposal":
        return "How would you like to review this?"
    if artifact_type == "policy_update":
        return "What should we do with this update?"
    if artifact_type == "monitoring_report":
        return "What would be most useful right now?"
    return "What would you like to do?"


def _review_decision_description(artifact_type: str) -> str:
    if artifact_type == "proposal":
        return "We can slow down and unpack the allocation, adjust the risk, stage the investment, or continue only when you are comfortable."
    if artifact_type == "policy_update":
        return "We can apply it, revise it, or simply keep monitoring. I can explain the trade-off before you choose."
    if artifact_type == "monitoring_report":
        return "The report is here so we can decide whether this is just noise or something that should change the policy."
    return "I can explain it, compare options, or save it for later."


def _review_title(artifact_type: str) -> str:
    return {
        "proposal": "Investment proposal",
        "policy_update": "Policy update",
        "monitoring_report": "Monitoring report",
    }.get(artifact_type, "AWM artifact")


def _review_route(artifact_type: str) -> str:
    return {
        "proposal": "/proposal",
        "policy_update": "/policy-update",
        "monitoring_report": "/market-report",
    }.get(artifact_type, "/monitoring")


def _notification_from_event(event: Dict[str, Any]) -> Dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if isinstance(payload.get("payload"), dict) and payload.get("notification_type"):
        payload = {
            **payload.get("payload", {}),
            "notification_id": payload.get("id"),
            "title": payload.get("title"),
            "body": payload.get("body"),
            "notification_type": payload.get("notification_type"),
            "status": payload.get("status"),
        }
    aggregate_id = event.get("aggregate_id")
    occurred_at = event.get("occurred_at")
    mapping = {
        "proposal.ready_notification": {
            "event_type": "proposal.ready",
            "title": "Investment proposal is ready",
            "body": "AWM finished the solution run and prepared the proposal.",
            "notification_type": "business_card",
            "route": "/proposal",
        },
        "execution.filled": {
            "event_type": "execution.filled",
            "title": "Policy activated",
            "body": "Orders were submitted through the mock broker and monitoring is active.",
            "notification_type": "task_status",
            "route": "/monitoring",
        },
        "monitoring.risk_alert_notification": {
            "event_type": "monitoring.risk_alert",
            "title": "Market drop detected",
            "body": "AWM generated a monitoring report and a policy update proposal.",
            "notification_type": "business_card",
            "route": "/market-report",
        },
        "policy_update.apply": {
            "event_type": "policy_update.applied",
            "title": "Policy update applied",
            "body": "AWM recorded the accepted policy update.",
            "notification_type": "task_status",
            "route": "/monitoring",
        },
        "policy_update.revise": {
            "event_type": "proposal.revised",
            "title": "Revised proposal is ready",
            "body": "AWM generated a revised proposal from the policy update.",
            "notification_type": "business_card",
            "route": "/proposal",
        },
        "policy.closed": {
            "event_type": "policy.closed",
            "title": "Policy closed",
            "body": "AWM recorded the policy closure and mock settlement.",
            "notification_type": "task_status",
            "route": "/exit",
        },
        "regular_consult.notification": {
            "event_type": "regular_consult.ready",
            "title": "AWM has a check-in ready",
            "body": "AWM found something worth reviewing with you.",
            "notification_type": "business_card",
            "route": "/advisor",
        },
    }
    spec = mapping.get(event_type)
    if not spec:
        return None
    if payload.get("title"):
        spec = {**spec, "title": str(payload.get("title"))}
    if payload.get("body"):
        spec = {**spec, "body": str(payload.get("body"))}
    notification_payload = {
        **payload,
        "aggregate_id": aggregate_id,
        "source_event_type": event_type,
        "route": spec["route"],
    }
    return {
        "id": _stable_notification_id(spec["event_type"], {"id": aggregate_id, **notification_payload}),
        "event_id": event.get("id"),
        "event_type": spec["event_type"],
        "notification_type": spec["notification_type"],
        "title": spec["title"],
        "body": spec["body"],
        "payload": notification_payload,
        "route": spec["route"],
        "status": "unread",
        "occurred_at": occurred_at,
    }


def _artifact_question_response(
    deps: Any,
    agent: DeterministicAgentProvider,
    client_id: str,
    artifact_id: str,
    body: Dict[str, Any],
    *,
    not_found: str = "Artifact not found",
) -> Tuple[Any, int]:
    artifact = deps.db_get_artifact(artifact_id=artifact_id, client_id=client_id)
    if not artifact:
        return jsonify({"success": False, "error": not_found}), 404
    answer = agent.answer_question(
        artifact.get("payload") or {},
        str(body.get("section_id") or "hero"),
        str(body.get("question") or ""),
    )
    _emit(deps, client_id, "artifact.question_answered", "artifact", artifact, answer)
    return jsonify({"success": True, **answer}), 200


__all__ = ["create_mvp_ui_blueprint"]
