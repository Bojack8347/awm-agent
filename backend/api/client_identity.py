"""Authoritative ownership classification for the AWM database schema."""

from __future__ import annotations

from typing import Final


DIRECT_CLIENT_TABLES: Final[frozenset[str]] = frozenset({
    "auth_accounts",
    "knowledge_facts",
    "knowledge_snapshots",
    "diagnosis_snapshots",
    "diagnosis_refresh_state",
    "pending_confirmations",
    "consultation_sessions",
    "journey_runs",
    "proactive_escalation_tracking",
    "proactive_outreach_log",
    "outbound_message_queue",
    "engine_runs",
    "advisory_plans",
    "advisory_events",
    "advisory_decisions",
    "advisory_holdings",
    "trace_events",
    "trace_roots",
    "business_events",
    "client_onboarding_status",
    "conversations",
    "conversation_messages",
    "client_identity_profiles",
    "client_consents",
    "external_connections",
    "data_permissions",
    "mvp_artifacts",
    "mvp_executions",
    "mvp_policies",
    "mvp_holdings",
    "thread_annotations",
    "money_pools",
    "money_pool_events",
    "consultation_ingests",
    "generated_policies",
    "pipeline_runs",
    "pipeline_checkpoints",
    "ai_companion_messages",
    "conversation_context_summaries",
    "canonical_client_facts",
    "planning_artifact_sets",
    "planning_refresh_state",
    "companion_sessions",
    "companion_client_actions",
    "consultation_interactions",
    "fact_confirmation_sets",
    "client_file_confirmation_actions",
    "external_data_decisions",
    "external_data_current_state",
})

# These tables have an existing, unambiguous ON DELETE CASCADE path from a
# directly owned parent and do not need a duplicated client_id.
CASCADED_CLIENT_TABLES: Final[dict[str, tuple[str, str]]] = {
    "auth_sessions": ("auth_accounts", "account_id"),
    "fact_confirmation_items": ("fact_confirmation_sets", "confirmation_set_id"),
    "journey_events": ("journey_runs", "journey_id"),
    "journey_evidence": ("journey_runs", "journey_id"),
    "advisory_artifacts": ("advisory_plans", "advisory_plan_id"),
    "trace_event_subjects": ("trace_events", "trace_event_id"),
}

# Shared/system reference and transport data.  Additions require an explicit
# review because this set is the only allowed escape hatch from client erasure.
NON_CLIENT_TABLES: Final[dict[str, str]] = {
    "user_sessions": "legacy anonymous/system session registry",
    "expert_products": "shared expert-product reference data",
    "expert_product_versions": "shared versioned reference data",
    "raw_external_events": "shared provider transport/deduplication log",
    "assumption_artifacts": "shared governed assumption reference data",
    "assumption_decisions": "shared governed assumption decision data",
    "registration_invitations": "server-issued account-opening invitation inventory",
    "registration_requests": "registration idempotency and recovery receipts",
}

IDENTITY_ROOT_TABLES: Final[frozenset[str]] = frozenset({"clients"})

CLASSIFIED_TABLES: Final[frozenset[str]] = frozenset(
    DIRECT_CLIENT_TABLES
    | CASCADED_CLIENT_TABLES.keys()
    | NON_CLIENT_TABLES.keys()
    | IDENTITY_ROOT_TABLES
)


__all__ = [
    "CASCADED_CLIENT_TABLES",
    "CLASSIFIED_TABLES",
    "DIRECT_CLIENT_TABLES",
    "IDENTITY_ROOT_TABLES",
    "NON_CLIENT_TABLES",
]
