"""Blueprint registration and dependency wiring for the Flask app."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from api.blueprints.advisory import create_advisory_blueprint
from api.blueprints.assumptions_admin import create_assumptions_admin_blueprint
from api.blueprints.auth import create_auth_blueprint
from api.blueprints.companion import create_companion_blueprint
from api.blueprints.companion_v2 import create_companion_v2_blueprint
from api.blueprints.client_state import create_client_state_blueprint
from api.blueprints.consultation import create_consultation_blueprint
from api.blueprints.consultation_sessions import create_consultation_sessions_blueprint
from api.blueprints.events import create_events_blueprint
from api.blueprints.health import create_health_blueprint
from api.blueprints.journeys import create_journeys_blueprint
from api.blueprints.knowledge import create_knowledge_blueprint
from api.blueprints.mvp_ui import create_mvp_ui_blueprint
from api.blueprints.proactive import create_proactive_blueprint
from api.blueprints.tasks import create_tasks_blueprint
from api.blueprints.trace import create_trace_blueprint
from api.services.client_state_view import build_client_state_view
from api.services.companion_turn import (
    CompanionTurnDependencies,
    CompanionTurnService,
)
from api.services.companion_sessions import CompanionSessionService
from api.services.companion_turn_runs import CompanionTurnRunService
from api.services.consultation_lifecycle import ConsultationLifecycleService
from api.services.registration import RegistrationService
from api.persistence.assumptions import get_assumption_repository
from api.persistence.fact_confirmations import bind_confirmation_prompt


def _build_regular_consult_task(deps: Any) -> Any:
    """Build the scheduled regular-consult worker with production deps."""
    from client_file.repository import build_production_client_file_repository
    from advisor.proactive.objectives import ScheduledRegularConsultTask

    repository = build_production_client_file_repository()
    return ScheduledRegularConsultTask(
        runtime_factory=lambda: deps.get_advisor_runtime(),
        client_file_reader=repository.reader,
        active_client_ids_factory=lambda: deps.db_get_active_client_ids(),
        publish_event=lambda **kwargs: deps.db_create_business_event(**kwargs),
        list_events=lambda **kwargs: deps.db_list_business_events(**kwargs),
    )


def _validate_companion_dependencies(deps: Any) -> None:
    required = (
        "get_advisor_runtime",
        "db_store_companion_message",
        "db_store_companion_message_bubbles",
        "db_list_artifacts",
        "db_save_artifact",
        "db_update_artifact",
        "db_list_policies",
        "db_save_policy",
        "db_update_policy",
        "db_save_asset_allocation_proposal_bundle",
        "db_get_cashflow_analysis_snapshot",
        "db_get_latest_knowledge_snapshot",
    )
    missing = [
        name
        for name in required
        if not callable(getattr(deps, name, None))
    ]
    if missing:
        raise RuntimeError(
            "Missing Companion turn dependencies: "
            + ", ".join(sorted(missing))
        )


def register_blueprints(app: Any, deps: Any) -> None:
    """Register all route blueprints against ``app``.

    ``deps`` is the importing ``api.server`` package. Lambdas intentionally
    resolve through it at request time so tests can monkeypatch server globals.
    """
    _validate_companion_dependencies(deps)
    companion_turn_service = CompanionTurnService(
        CompanionTurnDependencies(
            get_advisor_runtime=lambda: deps.get_advisor_runtime(),
            db_store_companion_message=lambda *args, **kwargs: deps.db_store_companion_message(*args, **kwargs),
            db_store_companion_message_bubbles=lambda *args, **kwargs: deps.db_store_companion_message_bubbles(*args, **kwargs),
            db_list_artifacts=lambda *args, **kwargs: deps.db_list_artifacts(*args, **kwargs),
            db_save_artifact=lambda *args, **kwargs: deps.db_save_artifact(*args, **kwargs),
            db_update_artifact=lambda *args, **kwargs: deps.db_update_artifact(*args, **kwargs),
            db_list_policies=lambda *args, **kwargs: deps.db_list_policies(*args, **kwargs),
            db_save_policy=lambda *args, **kwargs: deps.db_save_policy(*args, **kwargs),
            db_update_policy=lambda *args, **kwargs: deps.db_update_policy(*args, **kwargs),
            db_save_asset_allocation_proposal_bundle=lambda *args, **kwargs: deps.db_save_asset_allocation_proposal_bundle(*args, **kwargs),
            build_client_state_view=lambda *args, **kwargs: build_client_state_view(*args, **kwargs),
            db_get_cashflow_analysis_snapshot=lambda *args, **kwargs: deps.db_get_cashflow_analysis_snapshot(*args, **kwargs),
            db_get_latest_knowledge_snapshot=lambda *args, **kwargs: deps.db_get_latest_knowledge_snapshot(*args, **kwargs),
            schedule_conversation_compaction=lambda **kwargs: deps.schedule_conversation_compaction(**kwargs),
            bind_fact_confirmation_prompt=lambda **kwargs: bind_confirmation_prompt(**kwargs),
        ),
        turn_runs=CompanionTurnRunService(),
    )
    companion_session_service = CompanionSessionService()
    consultation_lifecycle_service = ConsultationLifecycleService(
        companion_sessions=companion_session_service,
        planning_coordinator_factory=lambda: deps.get_planning_refresh_coordinator(),
    )

    app.register_blueprint(create_tasks_blueprint(
        diagnosis_service_factory=lambda: deps.get_diagnosis_service(),
        proactive_service_factory=lambda: deps.get_proactive_service(),
        business_event_worker_factory=lambda: deps.get_business_event_worker(),
        regular_consult_task_factory=lambda: _build_regular_consult_task(deps),
        advisor_runtime_factory=lambda: deps.get_advisor_runtime(),
        planning_refresh_coordinator_factory=lambda: deps.get_planning_refresh_coordinator(),
    ))

    app.register_blueprint(create_proactive_blueprint(
        user_auth_decorator=deps.require_user_auth,
        proactive_service_factory=lambda: deps.get_proactive_service(),
        queued_outbound_messages_factory=lambda client_id: deps.db_get_queued_outbound_messages(client_id),
        mark_outbound_message_delivered=lambda message_id: deps.db_mark_outbound_message_delivered(message_id),
    ))

    app.register_blueprint(create_advisory_blueprint(
        user_auth_decorator=deps.require_user_auth,
        api_key_auth_decorator=deps.require_api_key_auth,
        deps=SimpleNamespace(
            db_create_advisory_holding=lambda *args, **kwargs: deps.db_create_advisory_holding(*args, **kwargs),
            db_create_advisory_event=lambda *args, **kwargs: deps.db_create_advisory_event(*args, **kwargs),
            db_create_business_event=lambda *args, **kwargs: deps.db_create_business_event(*args, **kwargs),
            db_create_expert_product=lambda *args, **kwargs: deps.db_create_expert_product(*args, **kwargs),
            db_create_expert_product_version=lambda *args, **kwargs: deps.db_create_expert_product_version(*args, **kwargs),
            db_create_raw_external_event=lambda *args, **kwargs: deps.db_create_raw_external_event(*args, **kwargs),
            db_create_trace_event=lambda *args, **kwargs: deps.db_create_trace_event(*args, **kwargs),
            db_derive_advisory_events_from_external_event=lambda *args, **kwargs: deps.db_derive_advisory_events_from_external_event(*args, **kwargs),
            db_get_advisory_plan=lambda *args, **kwargs: deps.db_get_advisory_plan(*args, **kwargs),
            db_get_advisory_events=lambda *args, **kwargs: deps.db_get_advisory_events(*args, **kwargs),
            db_list_advisory_artifacts=lambda *args, **kwargs: deps.db_list_advisory_artifacts(*args, **kwargs),
            db_list_advisory_holdings=lambda *args, **kwargs: deps.db_list_advisory_holdings(*args, **kwargs),
            db_list_advisory_plans=lambda *args, **kwargs: deps.db_list_advisory_plans(*args, **kwargs),
            db_list_engine_runs=lambda *args, **kwargs: deps.db_list_engine_runs(*args, **kwargs),
            db_list_expert_products=lambda *args, **kwargs: deps.db_list_expert_products(*args, **kwargs),
            db_list_expert_product_versions=lambda *args, **kwargs: deps.db_list_expert_product_versions(*args, **kwargs),
            db_record_advisory_decision=lambda *args, **kwargs: deps.db_record_advisory_decision(*args, **kwargs),
            db_update_advisory_holding_status=lambda *args, **kwargs: deps.db_update_advisory_holding_status(*args, **kwargs),
            db_update_advisory_plan_status=lambda *args, **kwargs: deps.db_update_advisory_plan_status(*args, **kwargs),
            db_update_advisory_event_status=lambda *args, **kwargs: deps.db_update_advisory_event_status(*args, **kwargs),
            compose_advisory_event_outreach=(
                lambda event, **kwargs: deps.get_proactive_service().compose_advisory_event(event, **kwargs)
            ),
        ),
    ))

    app.register_blueprint(create_client_state_blueprint(
        user_auth_decorator=deps.require_user_auth,
        deps=SimpleNamespace(
            db_get_unified_client_state=lambda *args, **kwargs: deps.db_get_unified_client_state(*args, **kwargs),
            build_client_state_view=lambda *args, **kwargs: build_client_state_view(*args, **kwargs),
        ),
    ))

    app.register_blueprint(create_trace_blueprint(
        api_key_auth_decorator=deps.require_api_key_auth,
        deps=SimpleNamespace(
            db_create_trace_event=lambda *args, **kwargs: deps.db_create_trace_event(*args, **kwargs),
            db_list_trace_events=lambda *args, **kwargs: deps.db_list_trace_events(*args, **kwargs),
        ),
    ))

    app.register_blueprint(create_assumptions_admin_blueprint(
        api_key_auth_decorator=deps.require_api_key_auth,
        repository_factory=get_assumption_repository,
        reviewer_identity_factory=lambda: (
            os.getenv("AWM_ASSUMPTION_REVIEWER_ID", "").strip()
            if os.getenv("ADVISOR_API_KEY", "").strip()
            else ""
        ),
    ))

    app.register_blueprint(create_mvp_ui_blueprint(
        user_auth_decorator=deps.require_user_auth,
        api_key_auth_decorator=deps.require_api_key_auth,
        deps=SimpleNamespace(
            db_add_conversation_message=lambda *args, **kwargs: deps.db_add_conversation_message(*args, **kwargs),
            db_create_business_event=lambda *args, **kwargs: deps.db_create_business_event(*args, **kwargs),
            db_create_conversation=lambda *args, **kwargs: deps.db_create_conversation(*args, **kwargs),
            db_create_trace_event=lambda *args, **kwargs: deps.db_create_trace_event(*args, **kwargs),
            db_get_artifact=lambda *args, **kwargs: deps.db_get_artifact(*args, **kwargs),
            db_get_conversation=lambda *args, **kwargs: deps.db_get_conversation(*args, **kwargs),
            db_get_execution=lambda *args, **kwargs: deps.db_get_execution(*args, **kwargs),
            db_get_latest_knowledge_snapshot=lambda *args, **kwargs: deps.db_get_latest_knowledge_snapshot(*args, **kwargs),
            db_get_onboarding_status=lambda *args, **kwargs: deps.db_get_onboarding_status(*args, **kwargs),
            db_get_policy=lambda *args, **kwargs: deps.db_get_policy(*args, **kwargs),
            db_list_artifacts=lambda *args, **kwargs: deps.db_list_artifacts(*args, **kwargs),
            db_list_conversation_messages=lambda *args, **kwargs: deps.db_list_conversation_messages(*args, **kwargs),
            db_list_holdings=lambda *args, **kwargs: deps.db_list_holdings(*args, **kwargs),
            db_list_policies=lambda *args, **kwargs: deps.db_list_policies(*args, **kwargs),
            db_mark_onboarding_completed=lambda *args, **kwargs: deps.db_mark_onboarding_completed(*args, **kwargs),
            db_save_artifact=lambda *args, **kwargs: deps.db_save_artifact(*args, **kwargs),
            db_save_consent=lambda *args, **kwargs: deps.db_save_consent(*args, **kwargs),
            db_save_data_permission=lambda *args, **kwargs: deps.db_save_data_permission(*args, **kwargs),
            db_save_execution=lambda *args, **kwargs: deps.db_save_execution(*args, **kwargs),
            db_save_external_connection=lambda *args, **kwargs: deps.db_save_external_connection(*args, **kwargs),
            db_save_holding=lambda *args, **kwargs: deps.db_save_holding(*args, **kwargs),
            db_save_identity_profile=lambda *args, **kwargs: deps.db_save_identity_profile(*args, **kwargs),
            db_save_policy=lambda *args, **kwargs: deps.db_save_policy(*args, **kwargs),
            db_update_artifact=lambda *args, **kwargs: deps.db_update_artifact(*args, **kwargs),
            db_update_holding=lambda *args, **kwargs: deps.db_update_holding(*args, **kwargs),
            db_update_policy=lambda *args, **kwargs: deps.db_update_policy(*args, **kwargs),
            db_upsert_onboarding_status=lambda *args, **kwargs: deps.db_upsert_onboarding_status(*args, **kwargs),
            assumption_repository_factory=get_assumption_repository,
        ),
    ))

    app.register_blueprint(create_events_blueprint(
        api_key_auth_decorator=deps.require_api_key_auth,
        deps=SimpleNamespace(
            db_create_business_event=lambda *args, **kwargs: deps.db_create_business_event(*args, **kwargs),
            db_get_business_event=lambda *args, **kwargs: deps.db_get_business_event(*args, **kwargs),
            db_list_business_events=lambda *args, **kwargs: deps.db_list_business_events(*args, **kwargs),
            db_list_trace_events=lambda *args, **kwargs: deps.db_list_trace_events(*args, **kwargs),
            db_reset_business_event_for_retry=lambda *args, **kwargs: deps.db_reset_business_event_for_retry(*args, **kwargs),
        ),
    ))

    app.register_blueprint(create_health_blueprint(
        task_profiles_factory=lambda: deps.list_task_profiles(),
    ))

    app.register_blueprint(create_consultation_blueprint(
        api_key_auth_decorator=deps.require_api_key_auth,
        ingest_lock=deps._INGEST_LOCK,
        in_memory_ingests=deps._CONSULTATION_INGESTS,
        append_ingest_to_disk=lambda payload: deps._append_ingest_to_disk(payload),
        get_ingested_consultation=lambda ingest_id: deps._get_ingested_consultation(ingest_id),
        db_available_factory=lambda: deps.db_available(),
        store_ingest=lambda payload: deps.db_store_ingest(payload),
        get_latest_ingest=lambda: deps.db_get_latest_ingest(),
        get_ingest=lambda ingest_id: deps.db_get_ingest(ingest_id),
        get_pipeline_run=lambda run_id: deps.db_get_pipeline_run(run_id),
        get_latest_pipeline_run=lambda session_id: deps.db_get_latest_pipeline_run(session_id),
    ))

    app.register_blueprint(create_companion_blueprint(
        user_auth_decorator=deps.require_user_auth,
        turn_service=companion_turn_service,
        db_get_companion_messages=lambda *args, **kwargs: deps.db_get_companion_messages(*args, **kwargs),
        db_count_companion_messages=lambda *args, **kwargs: deps.db_count_companion_messages(*args, **kwargs),
        expected_companion_session_id=lambda auth_session: deps._expected_companion_session_id(auth_session),
        session_service=companion_session_service,
    ))

    app.register_blueprint(create_companion_v2_blueprint(
        user_auth_decorator=deps.require_user_auth,
        turn_service=companion_turn_service,
    ))

    app.register_blueprint(create_auth_blueprint(
        db_available_factory=lambda: deps.db_available(),
        validate_credentials=lambda email, password: deps._validate_credentials(email, password),
        validate_login_credentials=lambda email, password: deps._validate_login_credentials(email, password),
        validate_registration_invite=lambda invite_number: deps._validate_registration_invite(invite_number),
        normalize_email=lambda email: deps._normalize_email(email),
        serialize_auth_payload=lambda account, session: deps._serialize_auth_payload(account, session),
        user_auth_decorator=deps.require_user_auth,
        bearer_token_getter=lambda: deps._extract_bearer_token(),
        get_account_by_email=lambda email: deps.db_get_auth_account_by_email(email),
        create_account=lambda **kwargs: deps.db_create_auth_account(**kwargs),
        create_session=lambda account_id: deps.db_create_auth_session(account_id),
        revoke_session=lambda token: deps.db_revoke_auth_session(token),
        delete_account=lambda account_id, client_id: deps.db_delete_auth_account(account_id, client_id),
        registration_service_factory=lambda: RegistrationService(
            register_transaction=deps.db_register_auth_account_transaction,
        ),
    ))

    app.register_blueprint(create_knowledge_blueprint(
        user_auth_decorator=deps.require_user_auth,
        deps=SimpleNamespace(
            db_get_knowledge_facts=lambda *args, **kwargs: deps.db_get_knowledge_facts(*args, **kwargs),
            db_get_latest_knowledge_snapshot=lambda *args, **kwargs: deps.db_get_latest_knowledge_snapshot(*args, **kwargs),
            db_get_knowledge_fact=lambda *args, **kwargs: deps.db_get_knowledge_fact(*args, **kwargs),
            db_update_knowledge_fact=lambda *args, **kwargs: deps.db_update_knowledge_fact(*args, **kwargs),
            db_get_latest_diagnosis_snapshot=lambda *args, **kwargs: deps.db_get_latest_diagnosis_snapshot(*args, **kwargs),
            db_get_pending_confirmations=lambda *args, **kwargs: deps.db_get_pending_confirmations(*args, **kwargs),
            db_get_pending_confirmation=lambda *args, **kwargs: deps.db_get_pending_confirmation(*args, **kwargs),
            db_resolve_pending_confirmation=lambda *args, **kwargs: deps.db_resolve_pending_confirmation(*args, **kwargs),
            db_bulk_upsert_knowledge_facts=lambda *args, **kwargs: deps.db_bulk_upsert_knowledge_facts(*args, **kwargs),
            db_get_current_snapshot_version=lambda *args, **kwargs: deps.db_get_current_snapshot_version(*args, **kwargs),
            db_store_knowledge_snapshot=lambda *args, **kwargs: deps.db_store_knowledge_snapshot(*args, **kwargs),
            rebuild_snapshot_targeted=lambda **kwargs: deps._rebuild_snapshot_targeted(**kwargs),
            get_diagnosis_refresh_payload=lambda client_id: deps._get_diagnosis_refresh_payload(client_id),
            commit_confirmed_pending=lambda **kwargs: deps._commit_confirmed_pending(**kwargs),
            queue_confirmation_diagnosis_refresh=lambda **kwargs: deps._queue_confirmation_diagnosis_refresh(**kwargs),
            section_titles=lambda: deps._SECTION_TITLES,
            category_to_section=lambda: deps._CATEGORY_TO_SECTION,
            get_client_profile_extractor=lambda: deps.get_client_profile_extractor(),
            get_knowledge_updater=lambda: deps.get_knowledge_updater(),
            carry_forward_section_summaries=lambda client_id: deps._carry_forward_section_summaries(client_id),
        ),
    ))

    app.register_blueprint(create_consultation_sessions_blueprint(
        user_auth_decorator=deps.require_user_auth,
        deps=SimpleNamespace(
            db_get_current_snapshot_version=lambda *args, **kwargs: deps.db_get_current_snapshot_version(*args, **kwargs),
            db_create_consultation_session=lambda *args, **kwargs: deps.db_create_consultation_session(*args, **kwargs),
            db_get_consultation_session=lambda *args, **kwargs: deps.db_get_consultation_session(*args, **kwargs),
            db_complete_consultation_session=lambda *args, **kwargs: deps.db_complete_consultation_session(*args, **kwargs),
            db_finalize_consultation_session=lambda *args, **kwargs: deps.db_finalize_consultation_session(*args, **kwargs),
            db_transition_advisor_onboarding_status=lambda *args, **kwargs: deps.db_transition_advisor_onboarding_status(*args, **kwargs),
            db_get_active_journey_runs=lambda *args, **kwargs: deps.db_get_active_journey_runs(*args, **kwargs),
            db_get_latest_knowledge_snapshot=lambda *args, **kwargs: deps.db_get_latest_knowledge_snapshot(*args, **kwargs),
            task_lock=lambda: deps._TASK_LOCK,
            consultation_tasks=lambda: deps._CONSULTATION_TASKS,
            journey_runtime_v2_enabled=lambda: deps._journey_runtime_v2_enabled(),
            get_diagnosis_refresh_payload=lambda client_id: deps._get_diagnosis_refresh_payload(client_id),
            get_advisor_runtime=lambda: deps.get_advisor_runtime(),
            get_planning_refresh_coordinator=lambda: deps.get_planning_refresh_coordinator(),
            consultation_lifecycle_service=consultation_lifecycle_service,
            companion_turn_service=companion_turn_service,
        ),
    ))

    app.register_blueprint(create_journeys_blueprint(
        user_auth_decorator=deps.require_user_auth,
        api_key_auth_decorator=deps.require_api_key_auth,
        deps=SimpleNamespace(
            db_get_current_snapshot_version=lambda *args, **kwargs: deps.db_get_current_snapshot_version(*args, **kwargs),
            db_create_journey_run=lambda *args, **kwargs: deps.db_create_journey_run(*args, **kwargs),
            db_update_journey_run=lambda *args, **kwargs: deps.db_update_journey_run(*args, **kwargs),
            db_get_journey_run=lambda *args, **kwargs: deps.db_get_journey_run(*args, **kwargs),
            db_get_knowledge_facts=lambda *args, **kwargs: deps.db_get_knowledge_facts(*args, **kwargs),
            db_load_checkpoint=lambda *args, **kwargs: deps.db_load_checkpoint(*args, **kwargs),
            db_delete_checkpoint=lambda *args, **kwargs: deps.db_delete_checkpoint(*args, **kwargs),
            db_get_activated_policies=lambda *args, **kwargs: deps.db_get_activated_policies(*args, **kwargs),
            journey_runtime_v2_enabled=lambda: deps._journey_runtime_v2_enabled(),
            v2_disabled_response=lambda: deps._v2_disabled_response(),
            activation_pipeline=lambda: deps.ACTIVATION_PIPELINE,
            pipeline_checkpoint=lambda: deps._pipeline_checkpoint,
            require_authenticated_account=lambda: deps._require_authenticated_account(),
            require_api_key=lambda: deps.require_api_key(),
        ),
    ))


__all__ = ["register_blueprints"]
