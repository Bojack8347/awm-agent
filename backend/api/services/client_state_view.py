"""Client state view for advisor orchestration.

This is a read-only layer above the existing persistence facade. It does not
own lifecycle state; it derives a compact "client file" view and open loops
from the tables AWM already writes today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from api.client_identity import CASCADED_CLIENT_TABLES, DIRECT_CLIENT_TABLES
from api.persistence.core import client_file_connection_scope


SourceMap = Mapping[str, Callable[..., Any]]
SectionResolver = Union[str, Callable[[Mapping[str, Any]], Any]]


@dataclass(frozen=True)
class Section:
    """One declared Client File projection section."""

    source: Union[str, Tuple[str, ...]]
    shape: str
    value: SectionResolver
    key: str = "client_id"
    latest_by: Optional[str] = None

    @property
    def sources(self) -> Tuple[str, ...]:
        return (self.source,) if isinstance(self.source, str) else self.source

    def resolve(self, context: Mapping[str, Any]) -> Any:
        if callable(self.value):
            return self.value(context)
        return context[self.value]


def _policies_section(context: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "active": context["normalized_active"],
        "proposed": context["normalized_proposed"],
        "mvp": context["normalized_mvp_policies"],
        "writebacks": context["policy_artifact_writebacks"],
    }


def _artifacts_section(context: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "proposals": context["normalized_artifacts"],
        "projections": context["normalized_projections"],
        "plans": context["plan_artifact_writebacks"],
    }


def _linked_accounts_section(context: Mapping[str, Any]) -> List[Dict[str, Any]]:
    connections = _dicts(context.get("external_connections"))
    holdings = _dicts(context.get("linked_holdings"))
    by_connection: Dict[str, List[Dict[str, Any]]] = {}
    for holding in holdings:
        payload = _safe_dict(holding.get("payload"))
        connection_id = str(payload.get("source_connection_id") or "unmatched")
        by_connection.setdefault(connection_id, []).append(
            {
                "holding_id": holding.get("id"),
                "symbol": payload.get("symbol"),
                "title": payload.get("title"),
                "quantity": payload.get("quantity"),
                "market_value": payload.get("market_value"),
                "currency": payload.get("currency") or "USD",
                "value_kind": "dollar_valued_holding",
                "provenance": "provider_observed",
                "confirmed": False,
                "observed_at": payload.get("observed_at") or holding.get("updated_at") or holding.get("created_at"),
            }
        )
    accounts: List[Dict[str, Any]] = []
    for connection in connections:
        payload = _safe_dict(connection.get("payload"))
        connection_id = str(connection.get("id") or "")
        account_holdings = by_connection.pop(connection_id, [])
        accounts.append(
            {
                "connection_id": connection_id,
                "connection_type": connection.get("record_type"),
                "provider": payload.get("provider"),
                "institution": payload.get("institution"),
                "external_account_id": payload.get("external_account_id"),
                "status": connection.get("status"),
                "currency": account_holdings[0]["currency"] if account_holdings else payload.get("currency") or "USD",
                "observed_at": payload.get("observed_at") or connection.get("updated_at") or connection.get("created_at"),
                "provenance": "provider_observed",
                "confirmed": False,
                "dollar_valued_holdings": account_holdings,
                "percentage_allocation_weights": {},
            }
        )
    if by_connection.get("unmatched"):
        accounts.append(
            {
                "connection_id": None,
                "connection_type": "import",
                "provider": "external_import",
                "institution": None,
                "external_account_id": None,
                "status": "imported",
                "currency": by_connection["unmatched"][0]["currency"],
                "observed_at": None,
                "provenance": "provider_observed",
                "confirmed": False,
                "dollar_valued_holdings": by_connection["unmatched"],
                "percentage_allocation_weights": {},
            }
        )
    return accounts


def _summary_section(context: Mapping[str, Any]) -> Dict[str, Any]:
    advisor_agenda = context["advisor_agenda"]
    engagement_objectives = context["engagement_objectives"]
    normalized_onboarding = context["normalized_onboarding"]
    normalized_pools = context["normalized_pools"]
    normalized_active = context["normalized_active"]
    active_proposal_ids = context["active_proposal_ids"]
    normalized_proposed = context["normalized_proposed"]
    normalized_mvp_policies = context["normalized_mvp_policies"]
    normalized_artifacts = context["normalized_artifacts"]
    normalized_projections = context["normalized_projections"]
    investment_consultations = context["investment_consultations"]
    investment_assessments = context["investment_assessments"]
    investment_proposals = context["investment_proposals"]
    plan_artifact_writebacks = context["plan_artifact_writebacks"]
    policy_artifact_writebacks = context["policy_artifact_writebacks"]
    normalized_writebacks = context["normalized_writebacks"]
    draft_facts = context["draft_facts"]
    structured_facts = context["structured_facts"]
    consultation_checkpoints = context["consultation_checkpoints"]
    stale_impacts = context["stale_impacts"]
    open_loops = context["open_loops"]

    return {
        "open_loop_count": len(open_loops),
        "next_action": advisor_agenda[0]["next_action"] if advisor_agenda else None,
        "next_subject": advisor_agenda[0]["subject"] if advisor_agenda else None,
        "next_objective_id": (
            engagement_objectives[0]["id"] if engagement_objectives else None
        ),
        "next_objective": (
            engagement_objectives[0]["objective"] if engagement_objectives else None
        ),
        "onboarding_status": normalized_onboarding.get("advisor_onboarding_status"),
        "account_opening_status": normalized_onboarding.get("account_opening_status"),
        "money_pool_count": len(normalized_pools),
        "journey_active_policy_count": len(normalized_active),
        "active_policy_count": len(normalized_active) + len(active_proposal_ids),
        "proposed_policy_count": len(normalized_proposed),
        "mvp_policy_count": len(normalized_mvp_policies),
        "proposal_artifact_count": len(normalized_artifacts),
        "projection_artifact_count": len(normalized_projections),
        "investment_consultation_count": len(investment_consultations),
        "investment_assessment_count": len(investment_assessments),
        "investment_proposal_count": len(investment_proposals),
        "plan_artifact_writeback_count": len(plan_artifact_writebacks),
        "policy_artifact_writeback_count": len(policy_artifact_writebacks),
        "recent_writeback_count": len(normalized_writebacks),
        "draft_fact_count": len(draft_facts),
        "structured_fact_count": len(structured_facts),
        "consultation_checkpoint_count": len(consultation_checkpoints),
        "stale_impact_count": len(stale_impacts),
    }


# The insertion order is the stable serialized order of the public read model.
CLIENT_FILE_SECTIONS: Mapping[str, Section] = {
    "facts": Section(
        source=(
            "canonical_client_facts",
            "knowledge_facts",
            "knowledge_snapshots",
            "diagnosis_snapshots",
            "pending_confirmations",
        ),
        shape="object",
        value="base_facts",
    ),
    "typed_facts": Section(
        source="canonical_client_facts", shape="list", value="canonical_fact_rows"
    ),
    "client_file_version": Section(
        source="clients", shape="integer", value="client_file_version"
    ),
    "current_planning_set": Section(
        source="planning_artifact_sets",
        shape="object_or_null",
        value="current_planning_set",
    ),
    "planning_refresh": Section(
        source="planning_refresh_state", shape="object", value="planning_refresh"
    ),
    "linked_accounts": Section(
        source=("external_connections", "mvp_holdings"),
        shape="list",
        value=_linked_accounts_section,
    ),
    "external_data": Section(
        source="external_data_current_state", shape="object", value="external_data_state"
    ),
    "draft_facts": Section(
        source="business_events", shape="list", value="draft_facts"
    ),
    "structured_facts": Section(
        source="business_events", shape="object", value="structured_facts"
    ),
    "consultation_checkpoints": Section(
        source="business_events",
        shape="list",
        value="consultation_checkpoints",
    ),
    "active_consultation_checkpoint": Section(
        source="business_events",
        shape="object_or_null",
        value=lambda context: next(
            (
                checkpoint
                for checkpoint in context["consultation_checkpoints"]
                if str(checkpoint.get("status") or "").lower()
                not in {"deferred", "paused"}
            ),
            None,
        ),
    ),
    "objective_status_overrides": Section(
        source="business_events",
        shape="object",
        value="objective_status_overrides",
    ),
    "journey_status": Section(
        source=("journey_runs", "advisory_plans", "advisory_artifacts"),
        shape="object",
        value="journey_status",
    ),
    "onboarding": Section(
        source="client_onboarding_status",
        shape="object",
        value="normalized_onboarding",
    ),
    "onboarding_completeness": Section(
        source="canonical_client_facts",
        shape="object",
        value="onboarding_completeness",
    ),
    "money_pools": Section(
        source="money_pools",
        shape="list",
        value="normalized_pools",
    ),
    "investment_consultations": Section(
        source=("mvp_artifacts", "business_events"),
        shape="list",
        value="investment_consultations",
    ),
    "investment_assessments": Section(
        source=("mvp_artifacts", "business_events"),
        shape="list",
        value="investment_assessments",
    ),
    "investment_proposals": Section(
        source=("mvp_artifacts", "mvp_policies"),
        shape="list",
        value="investment_proposals",
    ),
    "policies": Section(
        source=(
            "journey_runs",
            "advisory_plans",
            "advisory_artifacts",
            "mvp_policies",
            "business_events",
        ),
        shape="object",
        value=_policies_section,
    ),
    "artifacts": Section(
        source=("mvp_artifacts", "business_events"),
        shape="object",
        value=_artifacts_section,
    ),
    "recent_writebacks": Section(
        source="business_events", shape="list", value="normalized_writebacks"
    ),
    "stale_impacts": Section(
        source="business_events", shape="list", value="stale_impacts"
    ),
    "open_loops": Section(
        source=(
            "client_onboarding_status",
            "money_pools",
            "journey_runs",
            "mvp_artifacts",
            "business_events",
        ),
        shape="list",
        value="open_loops",
    ),
    "advisor_agenda": Section(
        source="derived:open_loops", shape="list", value="advisor_agenda"
    ),
    "engagement_objectives": Section(
        source="derived:advisor_agenda",
        shape="list",
        value="engagement_objectives",
    ),
    "summary": Section(
        source="derived:sections", shape="object", value=_summary_section
    ),
}

CLIENT_FILE_SECTION_TABLES = frozenset(
    source
    for section in CLIENT_FILE_SECTIONS.values()
    for source in section.sources
    if not source.startswith("derived:")
    and source in (DIRECT_CLIENT_TABLES | CASCADED_CLIENT_TABLES.keys())
)

# Client-owned operational/security/history tables that are deliberately not
# copied into the bounded advisor-facing Client File projection.
CLIENT_FILE_TABLE_EXCLUSIONS = (
    (DIRECT_CLIENT_TABLES | CASCADED_CLIENT_TABLES.keys())
    - CLIENT_FILE_SECTION_TABLES
)


def build_client_state_view(
    client_id: str,
    *,
    sources: Optional[SourceMap] = None,
) -> Dict[str, Any]:
    """Build the Client File with one checkout for all default DB sources."""
    if sources is None:
        with client_file_connection_scope():
            return _build_client_state_view(client_id, sources=None)
    return _build_client_state_view(client_id, sources=sources)


def _build_client_state_view(
    client_id: str,
    *,
    sources: Optional[SourceMap],
) -> Dict[str, Any]:
    """Build a compact advisor-facing read model for one client.

    The shape is intentionally stable and conservative:
    - facts and policies are copied from existing read models
    - money pools are normalized to the fields the advisor needs
    - open_loops describe what still needs attention

    No inferred financial calculations are performed here.
    """
    unified_state = _call_source(sources, "get_unified_client_state", client_id, default={}) or {}
    onboarding_status = _call_source(sources, "get_onboarding_status", client_id, default=None)
    money_pools = _call_source(sources, "list_money_pools", client_id, default=[]) or []
    external_connections = _call_source(
        sources, "list_external_connections", client_id=client_id, default=[]
    ) or []
    from api.persistence.external_data import ExternalDataDecisionRepository
    external_data_state = ExternalDataDecisionRepository().get_current(client_id) or {
        "sharing_decision": "not_requested",
        "scopes": [],
        "workflow_state": "not_requested",
        "connection_status": "not_started",
    }
    linked_holdings = _call_source(
        sources, "list_holdings", client_id=client_id, policy_id=None, status=None, default=[]
    ) or []
    proposed_policies = _call_source(sources, "get_proposed_policies", client_id, default=[]) or []
    activated_policies = _call_source(sources, "get_activated_policies", client_id, default=[]) or []
    mvp_policies = _call_source(
        sources,
        "list_policies",
        client_id=client_id,
        status=None,
        default=[],
    ) or []
    proposal_artifacts = _call_source(
        sources,
        "list_artifacts",
        client_id=client_id,
        artifact_type="proposal",
        default=[],
    ) or []
    projection_artifacts = _call_source(
        sources,
        "list_artifacts",
        client_id=client_id,
        artifact_type="projection",
        default=[],
    ) or []
    investment_assessment_artifacts = _call_source(
        sources,
        "list_artifacts",
        client_id=client_id,
        artifact_type="investment_assessment",
        default=[],
    ) or []
    client_file_events = _call_source(
        sources,
        "list_business_events",
        client_id=client_id,
        event_type="client_file.writeback",
        limit=20,
        default=[],
    ) or []
    client_file_updated_events = _call_source(
        sources,
        "list_business_events",
        client_id=client_id,
        event_type="client_file.updated",
        limit=20,
        default=[],
    ) or []
    canonical_fact_rows = _call_source(
        sources,
        "list_canonical_client_facts",
        client_id=client_id,
        default=[],
    ) or []
    client_file_version = _call_source(
        sources, "get_client_file_version", client_id=client_id, default=0
    ) or 0
    current_planning_set = _call_source(
        sources, "get_current_planning_artifact_set", client_id=client_id, default=None
    )
    planning_refresh = _call_source(
        sources, "get_planning_refresh_state", client_id=client_id, default={}
    ) or {}

    proposal_artifact_rows = _dicts(proposal_artifacts)
    normalized_pools = [_normalize_money_pool(pool) for pool in _dicts(money_pools)]
    normalized_proposed = [_normalize_policy(policy, "proposed") for policy in _dicts(proposed_policies)]
    normalized_active = [_normalize_policy(policy, "active") for policy in _dicts(activated_policies)]
    normalized_mvp_policies = [_normalize_mvp_policy(policy) for policy in _dicts(mvp_policies)]
    normalized_artifacts = [_normalize_artifact(artifact) for artifact in proposal_artifact_rows]
    normalized_projections = [_normalize_projection(artifact) for artifact in _dicts(projection_artifacts)]
    normalized_investment_assessments = [
        normalized
        for artifact in _dicts(investment_assessment_artifacts)
        if (normalized := _normalize_investment_assessment_artifact(artifact))
    ]
    writeback_events_by_identity: Dict[str, Dict[str, Any]] = {}
    for index, event in enumerate(_dicts([*client_file_events, *client_file_updated_events])):
        identity = str(event.get("id") or event.get("event_key") or f"row:{index}:{event}")
        writeback_events_by_identity.setdefault(identity, event)
    normalized_writebacks = [
        _normalize_client_file_writeback(event)
        for event in writeback_events_by_identity.values()
    ]
    stale_impacts = _collect_stale_impacts(normalized_writebacks)
    draft_facts = _collect_draft_facts(normalized_writebacks)
    committed_fact_values = _collect_committed_fact_values(normalized_writebacks)
    structured_facts = _collect_structured_facts(normalized_writebacks)
    consultation_checkpoints = _collect_consultation_checkpoints(normalized_writebacks)
    objective_status_overrides = _collect_objective_status_overrides(normalized_writebacks)
    plan_artifact_writebacks = _collect_artifact_writebacks(normalized_writebacks, "client_file.plans")
    policy_artifact_writebacks = _collect_artifact_writebacks(normalized_writebacks, "client_file.policies")
    investment_assessments = _merge_investment_assessment_records(
        _project_investment_assessments(normalized_writebacks),
        normalized_investment_assessments,
    )
    investment_consultations = _project_investment_consultations(investment_assessments)
    investment_proposals = _project_investment_proposals(proposal_artifact_rows, normalized_mvp_policies)
    normalized_onboarding = _normalize_onboarding(onboarding_status)
    from api.services.onboarding_completeness import advisor_onboarding_completeness

    onboarding_completeness = advisor_onboarding_completeness(_dicts(canonical_fact_rows))
    active_proposal_ids = {
        str(policy.get("proposal_id"))
        for policy in normalized_mvp_policies
        if str(policy.get("status") or "").lower() in {"active", "executed"}
        and policy.get("proposal_id")
    }
    for artifact in normalized_artifacts:
        if str(artifact.get("id") or "") in active_proposal_ids:
            artifact["policy_source_status"] = "active"

    open_loops: List[Dict[str, Any]] = []
    open_loops.extend(_onboarding_open_loops(normalized_onboarding, onboarding_completeness))
    open_loops.extend(
        _planning_open_loops(
            client_file_version=client_file_version,
            current_planning_set=current_planning_set,
            planning_refresh=planning_refresh,
        )
    )
    open_loops.extend(_money_pool_open_loops(normalized_pools))
    open_loops.extend(_policy_open_loops(normalized_proposed))
    open_loops.extend(_stale_impact_open_loops(
        stale_impacts,
        policies=normalized_mvp_policies,
        proposals=normalized_artifacts,
    ))
    open_loops.extend(_proposal_artifact_open_loops(normalized_artifacts, active_proposal_ids=active_proposal_ids))
    advisor_agenda = _build_advisor_agenda(open_loops)
    engagement_objectives = _build_engagement_objectives(advisor_agenda, open_loops)
    engagement_objectives = _apply_objective_status_overrides(
        engagement_objectives,
        objective_status_overrides,
    )
    journey_status = build_journey_status_from_state(
        normalized_onboarding,
        normalized_pools,
        normalized_proposed,
        normalized_active,
    )

    knowledge = unified_state.get("knowledge", {}) if isinstance(unified_state, dict) else {}
    diagnosis = unified_state.get("diagnosis", {}) if isinstance(unified_state, dict) else {}
    summary = unified_state.get("summary", {}) if isinstance(unified_state, dict) else {}
    base_facts = {
        "knowledge_snapshot_version": summary.get("knowledge_snapshot_version"),
        "diagnosis_snapshot_version": summary.get("diagnosis_snapshot_version"),
        "pending_confirmation_count": summary.get("pending_confirmation_count", 0),
        "knowledge_summary": _safe_dict(knowledge.get("snapshot_data")),
        "diagnosis_summary": _safe_dict(diagnosis.get("diagnosis_data")),
    }
    base_facts.update(committed_fact_values)
    for typed_fact in _dicts(canonical_fact_rows):
        entity_id = str(typed_fact.get("entity_id") or "")
        value = _safe_dict(typed_fact.get("value"))
        if entity_id:
            base_facts[entity_id] = value.get("value", value)

    projection_context = locals()
    resolved_sections = {
        name: section.resolve(projection_context)
        for name, section in CLIENT_FILE_SECTIONS.items()
    }

    return {
        "version": "client_state_view.v1",
        "client_id": client_id,
        "client_file_store": {
            "spine": "client_file",
            "read_model": "client_state_view.v1",
            "write_model": "business_events.client_file.writeback",
            "physical_status": "projection_over_existing_stores",
            "projection_sources": [
                "unified_client_state.knowledge",
                "unified_client_state.diagnosis",
                "money_pools",
                "policies",
                "advisory_artifacts",
                "business_events.client_file.writeback",
            ],
        },
        **resolved_sections,
    }


def format_client_state_for_advisor(state: Dict[str, Any], *, max_open_loops: int = 5) -> str:
    """Return a concise deterministic context block for LLM prompts."""
    if not isinstance(state, dict):
        return ""

    lines = ["AWM client file:"]
    summary = _safe_dict(state.get("summary"))
    lines.append(
        "Summary: "
        f"{summary.get('open_loop_count', 0)} open loops, "
        f"{summary.get('money_pool_count', 0)} money pools, "
        f"{summary.get('active_policy_count', 0)} active policies, "
        f"{summary.get('proposed_policy_count', 0)} proposed policies."
    )
    planning_refresh = _safe_dict(state.get("planning_refresh"))
    current_planning_set = _safe_dict(state.get("current_planning_set"))
    client_file_version = state.get("client_file_version")
    if planning_refresh.get("status") == "failed":
        lines.append(
            "Planning availability: unavailable because the latest background refresh failed. "
            "State this limitation and do not rely on absent planning outputs."
        )
    elif (
        isinstance(client_file_version, int)
        and client_file_version > 0
        and current_planning_set.get("source_client_version") != client_file_version
    ):
        lines.append(
            "Planning availability: stale; a refresh is pending or running. "
            "Do not provide consequential advice from the older artifact set."
        )

    open_loops = [loop for loop in state.get("open_loops", []) if isinstance(loop, dict)]
    if open_loops:
        lines.append("Open loops:")
        for loop in open_loops[: max(1, max_open_loops)]:
            missing = loop.get("missing_fields") or []
            missing_text = f"; missing: {', '.join(missing)}" if missing else ""
            lines.append(
                f"- {loop.get('type')}: {loop.get('subject')}"
                f" ({loop.get('status')}){missing_text}; next: {loop.get('next_action')}"
            )

    agenda = [item for item in state.get("advisor_agenda", []) if isinstance(item, dict)]
    if agenda:
        lines.append("Advisor agenda:")
        for item in agenda[: max(1, max_open_loops)]:
            lines.append(
                f"- P{item.get('priority')}: {item.get('phase')} -> "
                f"{item.get('next_action')} for {item.get('subject')} "
                f"({item.get('reason')})"
            )

    objectives = [
        item for item in state.get("engagement_objectives", [])
        if isinstance(item, dict)
    ]
    if objectives:
        top = objectives[0]
        lines.append(
            "Top engagement objective: "
            f"{top.get('objective')} | ask: {top.get('ask')} | "
            f"writeback: {_format_target_writeback(top.get('target_writeback'))}"
        )
        lines.append("Engagement objectives:")
        for item in objectives[: max(1, max_open_loops)]:
            lines.append(
                f"- {item.get('id')}: {item.get('objective')}; "
                f"ask: {item.get('ask')}; "
                f"writeback: {_format_target_writeback(item.get('target_writeback'))}; "
                f"score: {item.get('score')}"
            )
    money_pool_loops = [
        loop for loop in open_loops
        if loop.get("type") == "money_pool_missing_fields"
    ]
    if money_pool_loops:
        lines.append(
            "Money pool follow-up rule: only ask the money-pool fields listed "
            "in missing_fields above. Funding source/source of funds is optional "
            "unless funding_source is explicitly listed."
        )
    else:
        lines.append(
            "Money pool follow-up rule: no required money-pool fields are missing. "
            "Do not ask for funding source/source of funds/where the money comes from."
        )

    pools = [pool for pool in state.get("money_pools", []) if isinstance(pool, dict)]
    if pools:
        lines.append("Money pools:")
        for pool in pools[:5]:
            amount = pool.get("amount")
            amount_text = f"${amount:,.0f}" if isinstance(amount, (int, float)) else "amount unknown"
            missing = pool.get("missing_fields") or []
            missing_text = f"; missing {', '.join(missing)}" if missing else "; inputs complete"
            lines.append(
                f"- {pool.get('label')}: {amount_text}, "
                f"horizon {pool.get('horizon_date') or pool.get('horizon_text') or 'unknown'}, "
                f"risk {pool.get('risk_tolerance') or 'unknown'}{missing_text}."
            )

    policies = _safe_dict(state.get("policies"))
    policy_rows = [
        row for row in (
            policies.get("active", [])
            + policies.get("proposed", [])
            + policies.get("mvp", [])
        )
        if isinstance(row, dict)
    ]
    if policy_rows:
        lines.append("Policies:")
        for policy in policy_rows[:5]:
            lines.extend(_format_record_summary(policy, label=str(policy.get("title") or "Policy")))

    artifacts = _safe_dict(state.get("artifacts"))
    proposals = [proposal for proposal in artifacts.get("proposals", []) if isinstance(proposal, dict)]
    if proposals:
        lines.append("Proposal artifacts:")
        for proposal in proposals[:3]:
            lines.extend(_format_record_summary(proposal, label=str(proposal.get("title") or "Proposal")))

    projections = [projection for projection in artifacts.get("projections", []) if isinstance(projection, dict)]
    if projections:
        lines.append("Projection artifacts:")
        for projection in projections[:2]:
            lines.extend(_format_record_summary(projection, label=str(projection.get("title") or "Projection")))

    writebacks = [item for item in state.get("recent_writebacks", []) if isinstance(item, dict)]
    if writebacks:
        lines.append("Recent Client File writebacks:")
        for item in writebacks[:3]:
            fields = item.get("fields") or []
            fields_text = ", ".join(str(field) for field in fields) if fields else "outcome"
            lines.append(
                f"- {item.get('record')}.{item.get('operation')} "
                f"for {item.get('subject') or item.get('subject_id')} "
                f"({fields_text}); source: {item.get('source_type')}"
            )

    stale_impacts = [item for item in state.get("stale_impacts", []) if isinstance(item, dict)]
    if stale_impacts:
        lines.append("Potential stale dependent work:")
        for item in stale_impacts[:4]:
            lines.append(
                f"- {item.get('record')}: {item.get('status')} because {item.get('reason')}"
            )

    return "\n".join(lines)


def _call_source(
    sources: Optional[SourceMap],
    name: str,
    *args: Any,
    default: Any,
    **kwargs: Any,
) -> Any:
    fn = sources.get(name) if sources else _default_source(name)
    if fn is None:
        return default
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[client-state-view] {name} failed: {exc}", flush=True)
        return default


def _default_source(name: str) -> Optional[Callable[..., Any]]:
    from api.persistence import (  # pylint: disable=import-outside-toplevel
        get_activated_policies,
        get_client_file_version,
        get_current_planning_artifact_set,
        get_planning_refresh_state,
        get_onboarding_status,
        get_proposed_policies,
        get_unified_client_state,
        list_business_events,
        list_artifacts,
        list_canonical_client_facts,
        list_external_connections,
        list_holdings,
        list_money_pools,
        list_policies,
    )

    return {
        "get_unified_client_state": get_unified_client_state,
        "get_client_file_version": get_client_file_version,
        "get_current_planning_artifact_set": get_current_planning_artifact_set,
        "get_planning_refresh_state": get_planning_refresh_state,
        "list_money_pools": list_money_pools,
        "get_proposed_policies": get_proposed_policies,
        "get_activated_policies": get_activated_policies,
        "get_onboarding_status": get_onboarding_status,
        "list_artifacts": list_artifacts,
        "list_business_events": list_business_events,
        "list_policies": list_policies,
        "list_canonical_client_facts": list_canonical_client_facts,
        "list_external_connections": list_external_connections,
        "list_holdings": list_holdings,
    }.get(name)


def _normalize_client_file_writeback(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = _safe_dict(event.get("payload"))
    source = _safe_dict(payload.get("source"))
    writeback = _safe_dict(payload.get("writeback"))
    return {
        "event_id": event.get("id"),
        "event_type": event.get("event_type"),
        "occurred_at": event.get("occurred_at"),
        "record": writeback.get("record"),
        "operation": writeback.get("operation"),
        "subject_id": writeback.get("subject_id"),
        "subject": writeback.get("subject"),
        "fields": writeback.get("fields") if isinstance(writeback.get("fields"), list) else [],
        "values": _safe_dict(writeback.get("values")),
        "source_type": source.get("engagement_type") or event.get("event_source"),
        "source_session_id": source.get("session_id"),
        "source_message_id": source.get("user_message_id"),
        "stale_impacts": [
            impact for impact in payload.get("stale_impacts", [])
            if isinstance(impact, dict)
        ],
    }


def _collect_stale_impacts(writebacks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for writeback in writebacks:
        for impact in writeback.get("stale_impacts") or []:
            if not isinstance(impact, dict):
                continue
            key = "|".join(
                str(impact.get(part) or "")
                for part in ("record", "status", "source_record", "source_id")
            )
            if key in seen:
                continue
            seen.add(key)
            row = dict(impact)
            row["source_writeback_event_id"] = writeback.get("event_id")
            row["source_writeback_at"] = writeback.get("occurred_at")
            out.append(row)
    return out[:20]


def _collect_draft_facts(writebacks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    drafts: List[Dict[str, Any]] = []
    seen: set[str] = set()
    resolved_draft_ids: set[str] = set()
    resolved_draft_items: Dict[str, set[str]] = {}
    # Only pre-identity events use subject/field matching.
    committed_fields: Dict[str, set[str]] = {}
    ordered = sorted(
        writebacks,
        key=lambda item: str(item.get("occurred_at") or ""),
        reverse=True,
    )
    for writeback in ordered:
        record = writeback.get("record")
        values = _safe_dict(writeback.get("values"))
        if record == "client_file.confirmation_decisions":
            decisions = values.get("decisions")
            if isinstance(decisions, list):
                for item in decisions:
                    if not isinstance(item, dict):
                        continue
                    decision = str(item.get("decision") or "").strip().lower()
                    draft_id = str(item.get("draft_id") or "").strip()
                    field = str(item.get("field") or "").strip()
                    if decision in {"rejected", "corrected"} and draft_id and field:
                        resolved_draft_items.setdefault(draft_id, set()).add(field)
                continue
            decision = str(values.get("decision") or "").strip().lower()
            database_action = _safe_dict(values.get("database_action"))
            retires_draft = decision == "rejected" or (
                decision == "corrected" and database_action.get("ok") is True
            )
            if retires_draft:
                resolved_draft_ids.update(
                    item.strip()
                    for item in str(values.get("draft_id") or "").split(",")
                    if item.strip()
                )
            continue
        subject_key = str(
            writeback.get("subject_id")
            or writeback.get("subject")
            or values.get("fact_type")
            or ""
        )
        if record == "client_file.facts":
            resolved = values.get("resolved_draft_ids")
            if isinstance(resolved, list):
                resolved_draft_ids.update(str(item) for item in resolved if item)
            resolved_items = values.get("resolved_draft_items")
            if isinstance(resolved_items, list):
                for item in resolved_items:
                    if not isinstance(item, dict):
                        continue
                    draft_id = str(item.get("draft_id") or "").strip()
                    field = str(item.get("field") or "").strip()
                    if draft_id and field:
                        resolved_draft_items.setdefault(draft_id, set()).add(field)
            nested_facts = values.get("facts")
            fields = {
                str(field)
                for field in (writeback.get("fields") or [])
                if field
            }
            if isinstance(nested_facts, dict):
                for fact_type, fact_values in nested_facts.items():
                    if isinstance(fact_values, dict):
                        committed_fields.setdefault(str(fact_type), set()).update(
                            str(field) for field in fact_values
                        )
            if isinstance(nested_facts, dict):
                fields.update(str(field) for field in nested_facts)
            if subject_key and fields:
                committed_fields.setdefault(subject_key, set()).update(fields)
            continue
        if record != "client_file.draft_facts":
            continue
        draft_id = str(
            values.get("draft_id")
            or writeback.get("event_id")
            or values.get("source_event_id")
            or ""
        )
        if draft_id and draft_id in resolved_draft_ids:
            continue
        retired_items = resolved_draft_items.get(draft_id, set())
        if retired_items:
            draft = dict(values)
            facts = _safe_dict(draft.get("facts"))
            draft["facts"] = {
                field: value
                for field, value in facts.items()
                if field not in retired_items
            }
            entities = draft.get("entities")
            if isinstance(entities, list):
                draft["entities"] = [
                    entity
                    for entity in entities
                    if not isinstance(entity, dict)
                    or str(entity.get("entity_id") or "") not in retired_items
                ]
            if not draft["facts"] and not draft.get("entities"):
                continue
            values = draft
        nested_facts = values.get("facts")
        draft_fields = (
            {str(field) for field in nested_facts}
            if isinstance(nested_facts, dict)
            else set()
        )
        resolved_fields = committed_fields.get(subject_key, set())
        if (
            not values.get("draft_id")
            and draft_fields
            and draft_fields.issubset(resolved_fields)
        ):
            continue
        key = draft_id or str(values.get("fact_type") or len(drafts))
        if key in seen:
            continue
        seen.add(key)
        draft = dict(values)
        draft.setdefault("source_event_id", writeback.get("event_id"))
        draft.setdefault("source_session_id", writeback.get("source_session_id"))
        draft.setdefault("draft_id", draft.get("source_event_id"))
        drafts.append(draft)
    return drafts[:20]

def _collect_committed_fact_values(writebacks: List[Dict[str, Any]]) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    for writeback in reversed(writebacks):
        if writeback.get("record") != "client_file.facts":
            continue
        values = _safe_dict(writeback.get("values"))
        nested_facts = values.get("facts")
        if isinstance(nested_facts, dict):
            facts.update(nested_facts)
        elif values:
            facts.update(values)
    return facts


def _collect_structured_facts(writebacks: List[Dict[str, Any]]) -> Dict[str, Any]:
    structured: Dict[str, Any] = {}
    for writeback in reversed(writebacks):
        if writeback.get("record") != "client_file.facts":
            continue
        values = _safe_dict(writeback.get("values"))
        nested = values.get("structured_facts")
        if isinstance(nested, dict):
            structured.update(nested)
    return structured


def _collect_consultation_checkpoints(writebacks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    signed_assessment_times = [
        str(writeback.get("occurred_at") or "")
        for writeback in writebacks
        if writeback.get("record") == "client_file.plans"
        and str(writeback.get("operation") or "") == "record_assessment_signoff"
        and _safe_dict(writeback.get("values")).get("signed_off") is True
    ]
    latest_by_thread: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(
        enumerate(writebacks),
        key=lambda item: (str(item[1].get("occurred_at") or ""), item[0]),
        reverse=True,
    )
    for _, writeback in ordered:
        if writeback.get("record") != "client_file.consultation_checkpoints":
            continue
        values = _safe_dict(writeback.get("values"))
        thread_key = "|".join(
            [
                str(values.get("session_id") or writeback.get("source_session_id") or ""),
                str(values.get("consultation_type") or values.get("id") or writeback.get("event_id") or ""),
            ]
        )
        if thread_key in latest_by_thread:
            continue
        checkpoint = dict(values)
        checkpoint.setdefault("source_event_id", writeback.get("event_id"))
        checkpoint.setdefault("source_session_id", writeback.get("source_session_id"))
        checkpoint.setdefault("occurred_at", writeback.get("occurred_at"))
        latest_by_thread[thread_key] = checkpoint
    active = [
        checkpoint for checkpoint in latest_by_thread.values()
        if str(checkpoint.get("status") or "").lower() not in {"complete", "completed", "resolved"}
        and str(checkpoint.get("lifecycle_stage") or "").lower() not in {"complete", "completed"}
        and not (
            str(checkpoint.get("consultation_type") or checkpoint.get("skill") or "").lower()
            == "investment-consult"
            and any(
                signed_at >= str(checkpoint.get("occurred_at") or "")
                for signed_at in signed_assessment_times
            )
        )
    ]
    return active[:20]


def _collect_objective_status_overrides(writebacks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    overrides: Dict[str, Dict[str, Any]] = {}
    for writeback in writebacks:
        if writeback.get("record") != "client_file.objectives":
            continue
        values = _safe_dict(writeback.get("values"))
        objective_id = values.get("objective_id")
        if not objective_id:
            continue
        overrides[str(objective_id)] = {
            **values,
            "source_event_id": writeback.get("event_id"),
            "occurred_at": writeback.get("occurred_at"),
        }
    return overrides


def _apply_objective_status_overrides(
    objectives: List[Dict[str, Any]],
    overrides: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not overrides:
        return objectives
    filtered: List[Dict[str, Any]] = []
    for objective in objectives:
        if not isinstance(objective, dict):
            continue
        objective_id = str(objective.get("id") or "")
        override = overrides.get(objective_id)
        if override:
            status = str(override.get("status") or "").lower()
            if status in {"deferred", "completed"}:
                continue
            merged = dict(objective)
            merged.update(
                {
                    "status": override.get("status", objective.get("status")),
                    "lifecycle_stage": override.get("lifecycle_stage", objective.get("lifecycle_stage")),
                    "status_reason": override.get("reason"),
                }
            )
            filtered.append(merged)
            continue
        filtered.append(objective)
    return filtered


def build_journey_status_from_state(
    onboarding: Dict[str, Any],
    money_pools: List[Dict[str, Any]],
    proposed_policies: List[Dict[str, Any]],
    active_policies: List[Dict[str, Any]],
) -> Dict[str, str]:
    return {
        "onboarding": str(onboarding.get("status") or "unknown"),
        "investment_consult": "complete" if money_pools else "pending",
        "regular_consult": "due" if active_policies else "not_started",
        "policy_review": "pending" if proposed_policies else ("active" if active_policies else "none"),
    }


def _collect_artifact_writebacks(writebacks: List[Dict[str, Any]], record: str) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    for writeback in writebacks:
        if writeback.get("record") != record:
            continue
        values = _safe_dict(writeback.get("values"))
        is_assessment = (
            writeback.get("operation")
            in {"create_investment_assessment", "record_assessment_signoff"}
            and bool(values.get("assessment_id"))
            and isinstance(values.get("assessment"), dict)
        )
        artifacts.append({
            "event_id": writeback.get("event_id"),
            "occurred_at": writeback.get("occurred_at"),
            "artifact_type": (
                "investment_assessment" if is_assessment else values.get("artifact_type")
            ),
            "payload": values if is_assessment else _safe_dict(values.get("payload")),
            "writeback_target": values.get("writeback_target") or record,
            "source_session_id": writeback.get("source_session_id"),
        })
    return artifacts[:20]


def _project_investment_assessments(writebacks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    assessments: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(
        enumerate(writebacks),
        key=lambda item: (str(item[1].get("occurred_at") or ""), item[0]),
    )
    for _, writeback in ordered:
        record = writeback.get("record")
        operation = str(writeback.get("operation") or "")
        values = _safe_dict(writeback.get("values"))
        payloads: List[Dict[str, Any]] = []
        if operation == "subagent_artifact" and record == "client_file.plans":
            payload = _safe_dict(values.get("payload"))
            if _is_investment_assessment_payload(payload):
                payloads.append(payload)
        elif operation == "record_assessment_signoff" and record == "client_file.plans":
            assessment = _safe_dict(values.get("assessment"))
            basis = _safe_dict(values.get("consultation_basis")) or _safe_dict(assessment.get("basis"))
            status = "signed_off" if values.get("signed_off") is True else "signoff_declined"
            payloads.append({
                "schema_version": "investment_assessment.v1",
                "artifact_type": "investment_assessment",
                "investment_consultation_id": values.get("investment_consultation_id") or basis.get("investment_consultation_id"),
                "assessment_id": values.get("assessment_id"),
                "assessment_version": values.get("assessment_version"),
                "money_pool_id": values.get("money_pool_id") or basis.get("money_pool_id"),
                "pool_label": values.get("pool_label") or basis.get("pool_label"),
                "status": status,
                "assessment_status": status,
                "signed_off_at": values.get("signed_off_at"),
                "consultation_basis": basis,
                "assessment": assessment,
            })

        for payload in payloads:
            assessment = _investment_assessment_record(payload, writeback)
            assessment_id = str(assessment.get("assessment_id") or "")
            if not assessment_id:
                continue
            previous = assessments.get(assessment_id, {})
            assessments[assessment_id] = {**previous, **assessment}
    return sorted(
        assessments.values(),
        key=lambda item: str(item.get("signed_off_at") or item.get("occurred_at") or ""),
        reverse=True,
    )[:20]


def _merge_investment_assessment_records(
    projected: List[Dict[str, Any]],
    durable: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge projected writebacks with durable assessment artifacts.

    Durable artifacts carry Tian's full content fingerprint/version contract and
    should win over the lightweight event projection for the same assessment.
    """

    merged: Dict[str, Dict[str, Any]] = {}
    for record in [*projected, *durable]:
        if not isinstance(record, dict):
            continue
        key = _investment_assessment_merge_key(record)
        if not key:
            continue
        previous = merged.get(key, {})
        merged[key] = {**previous, **record}
    return sorted(
        merged.values(),
        key=lambda item: str(
            item.get("signed_off_at")
            or item.get("declined_at")
            or item.get("updated_at")
            or item.get("created_at")
            or item.get("occurred_at")
            or ""
        ),
        reverse=True,
    )[:20]


def _investment_assessment_merge_key(record: Dict[str, Any]) -> str:
    assessment_id = str(record.get("assessment_id") or record.get("id") or "").strip()
    if not assessment_id:
        return ""
    version = str(record.get("assessment_version") or record.get("version") or "1").strip()
    pool_id = str(record.get("money_pool_id") or "").strip()
    return f"{assessment_id}:{version}:{pool_id}"


def _project_investment_consultations(assessments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    consultations: Dict[str, Dict[str, Any]] = {}
    for assessment in assessments:
        consultation_id = str(assessment.get("investment_consultation_id") or "")
        if not consultation_id:
            continue
        status = "assessment_signed" if assessment.get("status") == "signed_off" else "assessment_pending_signoff"
        consultations[consultation_id] = {
            "schema_version": "investment_consultation.v1",
            "investment_consultation_id": consultation_id,
            "money_pool_id": assessment.get("money_pool_id"),
            "status": status,
            "consultation_basis": _safe_dict(assessment.get("consultation_basis")),
            "assessment_id": assessment.get("assessment_id"),
            "assessment_version": assessment.get("assessment_version"),
            "proposal_id": None,
            "source_event_id": assessment.get("source_event_id"),
            "source_session_id": assessment.get("source_session_id"),
            "occurred_at": assessment.get("occurred_at"),
        }
    return list(consultations.values())[:20]


def _project_investment_proposals(
    proposal_artifacts: List[Dict[str, Any]],
    policies: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []
    for artifact in proposal_artifacts:
        payload = _safe_dict(artifact.get("payload"))
        source_assessment = _safe_dict(payload.get("source_assessment"))
        money_pool = _safe_dict(payload.get("money_pool"))
        proposal_id = str(
            payload.get("proposal_id")
            or payload.get("source_advisor_artifact_id")
            or payload.get("id")
            or artifact.get("id")
            or ""
        )
        if not proposal_id:
            continue
        proposals.append({
            "schema_version": "investment_policy_proposal.v1",
            "proposal_id": proposal_id,
            "investment_consultation_id": (
                payload.get("investment_consultation_id")
                or source_assessment.get("investment_consultation_id")
            ),
            "assessment_id": payload.get("assessment_id") or source_assessment.get("assessment_id"),
            "assessment_version": payload.get("assessment_version") or source_assessment.get("assessment_version"),
            "money_pool_id": payload.get("money_pool_id") or money_pool.get("id"),
            "status": artifact.get("status") or payload.get("status"),
            "title": artifact.get("title") or payload.get("title"),
            "source_artifact_id": artifact.get("id"),
            "policy_source_status": next(
                (
                    policy.get("status")
                    for policy in policies
                    if str(policy.get("proposal_id") or "") == str(artifact.get("id") or "")
                ),
                None,
            ),
        })
    return proposals[:20]


def _investment_assessment_record(payload: Dict[str, Any], writeback: Dict[str, Any]) -> Dict[str, Any]:
    assessment = _safe_dict(payload.get("assessment"))
    basis = _safe_dict(payload.get("consultation_basis")) or _safe_dict(payload.get("basis")) or _safe_dict(assessment.get("basis"))
    assessment_id = str(payload.get("assessment_id") or payload.get("id") or "").strip()
    assessment_status = str(payload.get("assessment_status") or "").strip()
    status = str(payload.get("status") or assessment_status or "").strip()
    if status == "ready" and assessment_status:
        status = assessment_status
    if payload.get("signed_off") is True:
        status = "signed_off"
    investment_consultation_id = str(
        payload.get("investment_consultation_id")
        or basis.get("investment_consultation_id")
        or (f"consult-{assessment_id}" if assessment_id else "")
    ).strip()
    return {
        "schema_version": "investment_assessment.v1",
        "artifact_type": "investment_assessment",
        "investment_consultation_id": investment_consultation_id,
        "assessment_id": assessment_id,
        "assessment_version": payload.get("assessment_version") or payload.get("version") or 1,
        "money_pool_id": payload.get("money_pool_id") or basis.get("money_pool_id"),
        "pool_label": payload.get("pool_label") or basis.get("pool_label"),
        "status": status or "pending_client_signoff",
        "assessment_status": status or "pending_client_signoff",
        "signed_off_at": payload.get("signed_off_at"),
        "consultation_basis": basis,
        "assessment": assessment,
        "source_event_id": writeback.get("event_id"),
        "source_session_id": writeback.get("source_session_id"),
        "occurred_at": writeback.get("occurred_at"),
    }


def _is_investment_assessment_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("schema_version") == "investment_assessment.v1"
        or payload.get("artifact_type") == "investment_assessment"
        or payload.get("analysis_type") == "internal_investment_assessment"
    )


def _normalize_money_pool(pool: Dict[str, Any]) -> Dict[str, Any]:
    label = str(pool.get("label") or "Unnamed pool").strip() or "Unnamed pool"
    payload = _safe_dict(pool.get("payload"))
    constraints = _safe_dict(pool.get("constraints"))
    horizon_years = (
        pool.get("horizon_years")
        if pool.get("horizon_years") not in (None, "")
        else payload.get("horizon_years")
    )
    horizon_text = payload.get("horizon_text") or pool.get("horizon_text")
    if not horizon_text and horizon_years not in (None, ""):
        horizon_text = f"{horizon_years} years"
    normalized = {
        "id": pool.get("id"),
        "label": label,
        "state": pool.get("state"),
        "purpose_type": pool.get("purpose_type"),
        "amount": pool.get("amount"),
        "currency": pool.get("currency") or "USD",
        "horizon_date": pool.get("horizon_date"),
        "horizon_text": horizon_text,
        "horizon_years": horizon_years,
        "risk_tolerance": pool.get("risk_tolerance"),
        "objective": pool.get("objective"),
        "beneficiary": pool.get("beneficiary"),
        "funding_source": pool.get("funding_source"),
        "constraints": constraints,
        "priority": pool.get("priority"),
        "updated_at": pool.get("updated_at"),
    }
    for key in (
        "liquidity_needs",
        "complexity_preference",
        "asset_class_preferences",
        "exclusions",
        "special_considerations",
        "tax_considerations",
    ):
        value = pool.get(key, constraints.get(key))
        if value not in (None, "", []):
            normalized[key] = value
    normalized["missing_fields"] = _missing_money_pool_fields(normalized)
    return normalized


def _missing_money_pool_fields(pool: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if pool.get("amount") in (None, ""):
        missing.append("amount")
    if not (
        pool.get("horizon_date")
        or pool.get("horizon_text")
        or pool.get("horizon_years") not in (None, "")
    ):
        missing.append("horizon")
    if not pool.get("risk_tolerance"):
        missing.append("risk_tolerance")
    if not pool.get("purpose_type"):
        missing.append("purpose")
    return missing


def _normalize_policy(policy: Dict[str, Any], fallback_status: str) -> Dict[str, Any]:
    payload = _safe_dict(policy.get("solution_output") or policy.get("payload"))
    title = (
        policy.get("title")
        or policy.get("name")
        or payload.get("title")
        or payload.get("policy_name")
        or policy.get("journey_type")
        or "Investment policy"
    )
    return {
        "id": policy.get("id"),
        "title": title,
        "status": policy.get("policy_status") or policy.get("status") or fallback_status,
        "journey_type": policy.get("journey_type") or payload.get("journey_type"),
        "advisory_plan_id": policy.get("advisory_plan_id"),
        "advisory_artifact_id": policy.get("advisory_artifact_id"),
        "created_at": policy.get("created_at"),
        "completed_at": policy.get("completed_at"),
        "activated_at": policy.get("activated_at"),
        "payload_summary": _payload_summary(payload),
    }


def _normalize_mvp_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    payload = _safe_dict(policy.get("payload"))
    title = policy.get("title") or payload.get("title") or "Active Policy"
    money_pool = payload.get("money_pool") if isinstance(payload.get("money_pool"), dict) else None
    return {
        "id": policy.get("id"),
        "title": title,
        "status": policy.get("status"),
        "proposal_id": payload.get("proposal_id") or policy.get("related_id"),
        "money_pool": _compact_money_pool_ref(money_pool),
        "created_at": policy.get("created_at"),
        "updated_at": policy.get("updated_at"),
        "payload_summary": _payload_summary(payload),
    }


def _normalize_onboarding(onboarding: Any) -> Dict[str, Any]:
    if not isinstance(onboarding, dict):
        return {
            "status": "not_started",
            "account_opening_status": "not_started",
            "advisor_onboarding_status": "not_started",
            "current_step": None,
            "completed_steps": [],
            "updated_at": None,
        }
    completed_steps = onboarding.get("completed_steps")
    return {
        # `status` remains the account-opening compatibility alias.
        "status": onboarding.get("account_opening_status") or onboarding.get("status") or "not_started",
        "account_opening_status": onboarding.get("account_opening_status") or onboarding.get("status") or "not_started",
        "advisor_onboarding_status": onboarding.get("advisor_onboarding_status") or "not_started",
        "current_step": onboarding.get("current_step"),
        "completed_steps": completed_steps if isinstance(completed_steps, list) else [],
        "updated_at": onboarding.get("updated_at"),
    }


def _normalize_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    payload = _safe_dict(artifact.get("payload"))
    title = (
        artifact.get("title")
        or payload.get("title")
        or payload.get("proposal_name")
        or payload.get("name")
        or "Proposal"
    )
    return {
        "id": artifact.get("id"),
        "title": title,
        "status": artifact.get("status"),
        "related_id": artifact.get("related_id"),
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
        "money_pool": _compact_money_pool_ref(payload.get("money_pool")),
        "payload_summary": _payload_summary(payload),
    }


def _normalize_projection(artifact: Dict[str, Any]) -> Dict[str, Any]:
    payload = _safe_dict(artifact.get("payload"))
    return {
        "id": artifact.get("id"),
        "title": artifact.get("title") or payload.get("title") or "Projection",
        "status": artifact.get("status"),
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
        "payload_summary": _payload_summary(payload),
    }


def _normalize_investment_assessment_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Expose the full durable assessment contract, not only recent event previews."""

    payload = _safe_dict(artifact.get("payload"))
    if not payload.get("assessment_id") or not isinstance(payload.get("assessment"), dict):
        return {}
    normalized = dict(payload)
    normalized["durable_artifact_id"] = artifact.get("id") or payload.get("durable_artifact_id")
    return normalized


def _payload_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    sections = _sections_by_id(payload)

    for section_id, fields in {
        "hero": (
            "headline",
            "summary",
            "status",
            "risk_profile",
            "net_worth_today",
            "base_case_10y",
        ),
        "scope": (
            "objective",
            "constraints",
            "excluded_asset_classes",
            "target_volatility",
        ),
        "portfolio_analytics": (
            "expected_return",
            "expected_volatility",
            "tracking_error",
            "manager_count",
            "asset_class_count",
            "source",
        ),
        "simulated_projection": (
            "base_case_10y",
            "downside_10y",
            "upside_10y",
            "success_probability",
            "terminal_value_percentiles",
            "ending_balance",
            "source",
        ),
        "fixed_income": (
            "weight",
            "estimated_notional",
            "included_asset_classes",
            "duration_years",
            "yield_to_maturity",
            "role",
        ),
        "cash_flow": (
            "monthly_income",
            "monthly_spending",
            "savings_rate",
        ),
        "resilience": (
            "runway_months",
            "downside_10y",
            "liquidity_score",
        ),
    }.items():
        section_payload = sections.get(section_id)
        if isinstance(section_payload, dict):
            picked = _pick_fields(section_payload, fields)
            if picked:
                summary[section_id] = picked

    allocation = _first_non_empty(
        _pick_fields(sections.get("allocation"), ("chart",)),
        _pick_fields(payload, ("asset_allocation", "target_allocation")),
    )
    if allocation:
        summary["allocation"] = _compact_allocation(allocation)

    securities = _extract_securities(sections.get("recommended_securities"))
    if securities:
        summary["recommended_securities"] = securities

    engine_run = _safe_dict(payload.get("engine_run"))
    if engine_run:
        engine_summary = _pick_fields(engine_run, ("engine_name", "engine_version", "status"))
        outputs = _pick_fields(
            _safe_dict(engine_run.get("outputs")),
            ("expected_return", "expected_volatility", "security_count", "section_ids"),
        )
        inputs = _pick_fields(
            _safe_dict(engine_run.get("inputs")),
            ("total_investment", "target_volatility", "excluded_asset_classes"),
        )
        if outputs:
            engine_summary["outputs"] = outputs
        if inputs:
            engine_summary["inputs"] = inputs
        if engine_summary:
            summary["engine_run"] = engine_summary

    broker = _safe_dict(payload.get("broker"))
    orders = broker.get("orders")
    if isinstance(orders, list):
        summary["broker"] = {
            "status": broker.get("status"),
            "orders": _compact_orders(orders),
        }

    money_pool = _compact_money_pool_ref(payload.get("money_pool"))
    if money_pool:
        summary["money_pool"] = money_pool

    for key in (
        "title",
        "stale_review",
        "review_outcome",
        "horizon",
        "expected_return",
        "expected_annual_return",
        "expected_volatility",
        "expected_risk",
        "horizon_years",
        "risk_profile",
        "risk",
        "asset_allocation",
        "target_allocation",
        "recommendation",
        "base_case_10y",
        "downside_10y",
        "upside_10y",
        "net_worth_today",
    ):
        if key in payload and payload.get(key) not in (None, "", [], {}):
            summary[key] = payload.get(key)
    return summary


def _sections_by_id(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or section.get("id") or "").strip()
        section_payload = section.get("payload")
        if section_id and isinstance(section_payload, dict):
            out[section_id] = section_payload
    return out


def _pick_fields(value: Any, keys: Iterable[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    picked: Dict[str, Any] = {}
    for key in keys:
        item = value.get(key)
        if item not in (None, "", [], {}):
            picked[key] = _compact_value(item)
    return picked


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _compact_value(v)
            for k, v in list(value.items())[:12]
            if v not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:8]]
    if isinstance(value, str):
        return value[:500]
    return value


def _compact_allocation(value: Dict[str, Any]) -> Any:
    chart = value.get("chart")
    if isinstance(chart, list):
        return [
            {"label": row.get("label"), "value": row.get("value")}
            for row in chart[:8] if isinstance(row, dict)
        ]
    for key in ("asset_allocation", "target_allocation"):
        allocation = value.get(key)
        if isinstance(allocation, dict):
            return _compact_value(allocation)
        if isinstance(allocation, list):
            return _compact_value(allocation)
    return _compact_value(value)


def _extract_securities(value: Any) -> List[Dict[str, Any]]:
    payload = _safe_dict(value)
    securities = payload.get("securities")
    if not isinstance(securities, list):
        return []
    out: List[Dict[str, Any]] = []
    for security in securities[:8]:
        if not isinstance(security, dict):
            continue
        row = _pick_fields(
            security,
            ("symbol", "ticker", "name", "asset_class", "weight", "notional", "target_value"),
        )
        if row:
            out.append(row)
    return out


def _compact_orders(orders: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for order in orders[:8]:
        if not isinstance(order, dict):
            continue
        row = _pick_fields(order, ("symbol", "side", "status", "notional", "filled_quantity"))
        if row:
            out.append(row)
    return out


def _compact_money_pool_ref(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    picked = _pick_fields(
        value,
        ("id", "label", "purpose_type", "amount", "horizon_date", "horizon_text", "risk_tolerance", "objective"),
    )
    return picked or None


def _first_non_empty(*values: Dict[str, Any]) -> Dict[str, Any]:
    for value in values:
        if value:
            return value
    return {}


def _format_record_summary(record: Dict[str, Any], *, label: str) -> List[str]:
    summary = _safe_dict(record.get("payload_summary"))
    status = record.get("status") or "unknown"
    if record.get("policy_source_status"):
        status = f"{status}; source_for_{record['policy_source_status']}_policy"
    lines = [f"- {label} ({status})"]
    highlights = _summary_highlights(summary)
    if highlights:
        for item in highlights[:8]:
            lines.append(f"  - {item}")
    return lines


def _summary_highlights(summary: Dict[str, Any]) -> List[str]:
    highlights: List[str] = []
    hero = _safe_dict(summary.get("hero"))
    for key in ("summary", "headline", "net_worth_today", "base_case_10y"):
        if key in hero:
            highlights.append(f"{key}: {_format_value(hero[key])}")

    analytics = _safe_dict(summary.get("portfolio_analytics"))
    for key in ("expected_return", "expected_volatility", "tracking_error"):
        if key in analytics:
            highlights.append(f"{key}: {_format_value(analytics[key])}")

    projection = _safe_dict(summary.get("simulated_projection"))
    for key in ("base_case_10y", "downside_10y", "upside_10y", "success_probability", "ending_balance"):
        if key in projection:
            highlights.append(f"{key}: {_format_value(projection[key])}")

    if "allocation" in summary:
        highlights.append(f"allocation: {_format_value(summary['allocation'])}")
    if "recommended_securities" in summary:
        highlights.append(f"securities: {_format_value(summary['recommended_securities'])}")

    engine = _safe_dict(summary.get("engine_run"))
    outputs = _safe_dict(engine.get("outputs"))
    for key in ("expected_return", "expected_volatility", "security_count"):
        if key in outputs:
            highlights.append(f"engine_{key}: {_format_value(outputs[key])}")

    broker = _safe_dict(summary.get("broker"))
    if broker:
        highlights.append(f"broker: {_format_value(broker)}")

    stale_review = _safe_dict(summary.get("stale_review"))
    if stale_review:
        highlights.append(f"stale_review: {_format_value(stale_review)}")

    for key in (
        "expected_return",
        "expected_annual_return",
        "expected_volatility",
        "horizon",
        "horizon_years",
        "risk_profile",
        "net_worth_today",
        "base_case_10y",
    ):
        if key in summary:
            highlights.append(f"{key}: {_format_value(summary[key])}")
    return highlights


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        if -1.0 < value < 1.0 and value != 0:
            return f"{value * 100:.2f}%"
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        parts = []
        for item in value[:5]:
            if isinstance(item, dict):
                label = item.get("symbol") or item.get("ticker") or item.get("label") or item.get("name")
                weight = item.get("weight", item.get("value"))
                if label and weight is not None:
                    parts.append(f"{label} { _format_value(weight) }")
                elif label:
                    parts.append(str(label))
                else:
                    parts.append(str(_compact_value(item)))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:6]:
            parts.append(f"{key}={_format_value(item)}")
        return ", ".join(parts)
    return str(value)


def _build_advisor_agenda(open_loops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    agenda: List[Dict[str, Any]] = []
    for loop in open_loops:
        if not isinstance(loop, dict):
            continue
        loop_type = str(loop.get("type") or "")
        meta = _agenda_meta(loop_type)
        agenda.append({
            "priority": meta["priority"],
            "phase": meta["phase"],
            "reason": meta["reason"],
            "loop_id": loop.get("id"),
            "loop_type": loop_type,
            "status": loop.get("status"),
            "subject": loop.get("subject"),
            "subject_id": loop.get("subject_id"),
            "next_action": loop.get("next_action"),
        })
    return sorted(agenda, key=lambda item: (item["priority"], str(item.get("subject") or "")))


def _build_engagement_objectives(
    agenda: List[Dict[str, Any]],
    open_loops: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Derive task-focused proactive objectives from the advisor agenda.

    This is still read-only: it does not persist lifecycle state. The purpose is
    to give chat and proactive delivery a single objective contract instead of
    asking the LLM to infer what should happen next from loose open-loop text.
    """
    loops_by_id = {
        str(loop.get("id")): loop
        for loop in open_loops
        if isinstance(loop, dict) and loop.get("id") is not None
    }
    objectives: List[Dict[str, Any]] = []
    for item in agenda:
        if not isinstance(item, dict):
            continue
        loop = loops_by_id.get(str(item.get("loop_id"))) or {}
        objective = _objective_from_agenda_item(item, loop)
        if objective:
            objectives.append(objective)
    return sorted(
        objectives,
        key=lambda row: (-float(row.get("score") or 0), int(row.get("priority") or 999)),
    )


def _objective_from_agenda_item(
    item: Dict[str, Any],
    loop: Dict[str, Any],
) -> Dict[str, Any]:
    loop_type = str(item.get("loop_type") or "")
    subject = item.get("subject") or "this item"
    subject_id = item.get("subject_id")
    missing = loop.get("missing_fields") or []
    if not isinstance(missing, list):
        missing = []

    table = {
        "onboarding_incomplete": {
            "generator": "progression",
            "objective": "complete_onboarding",
            "ask": "continue the current onboarding step",
            "target_writeback": {
                "record": "onboarding_status",
                "operation": "advance_step",
                "fields": ["current_step", "completed_steps"],
            },
            "urgency": 0.75,
            "client_value": 0.85,
            "readiness": 1.0,
            "retire_when": "onboarding.status is completed",
        },
        "planning_refresh_pending": {
            "generator": "planning_refresh",
            "objective": "wait_for_current_planning_artifacts",
            "ask": "explain that current planning analysis is still refreshing",
            "target_writeback": {
                "record": "planning_refresh_state",
                "operation": "none",
                "fields": [],
            },
            "urgency": 0.45,
            "client_value": 0.8,
            "readiness": 0.0,
            "retire_when": "the current planning set matches client_file_version",
        },
        "money_pool_missing_fields": {
            "generator": "interest_escalation",
            "objective": "complete_investment_inputs",
            "ask": _money_pool_objective_ask(missing),
            "target_writeback": {
                "record": "money_pool",
                "operation": "upsert",
                "subject_id": subject_id,
                "fields": missing,
            },
            "urgency": 0.8,
            "client_value": 0.95,
            "readiness": 0.95,
            "retire_when": "money_pool.missing_fields is empty",
        },
        "policy_decision_pending": {
            "generator": "progression",
            "objective": "review_policy_decision",
            "ask": "review the proposed policy and choose approve, refine, or defer",
            "target_writeback": {
                "record": "policy_decision",
                "operation": "record_client_decision",
                "subject_id": subject_id,
                "fields": ["decision", "rationale"],
            },
            "urgency": 0.78,
            "client_value": 0.92,
            "readiness": 1.0,
            "retire_when": "policy status is approved, declined, refined, or deferred",
        },
        "stale_policy_review": {
            "generator": "stale_dependency_review",
            "objective": "review_stale_policy",
            "ask": "review whether the affected policy still fits the updated client facts",
            "target_writeback": {
                "record": "policy_review",
                "operation": "record_review_outcome",
                "subject_id": subject_id,
                "fields": ["review_outcome", "rationale"],
            },
            "urgency": 0.62,
            "client_value": 0.82,
            "readiness": 0.85,
            "retire_when": "affected policy is reviewed, refreshed, or explicitly left unchanged",
        },
        "stale_proposal_review": {
            "generator": "stale_dependency_review",
            "objective": "review_stale_proposal",
            "ask": "review whether the affected proposal still fits the updated client facts",
            "target_writeback": {
                "record": "proposal_review",
                "operation": "record_review_outcome",
                "subject_id": subject_id,
                "fields": ["review_outcome", "rationale"],
            },
            "urgency": 0.58,
            "client_value": 0.78,
            "readiness": 0.85,
            "retire_when": "affected proposal is reviewed, refreshed, or explicitly left unchanged",
        },
        "proposal_review": {
            "generator": "progression",
            "objective": "review_proposal",
            "ask": "review the proposal and decide whether to refine it or turn it into policy",
            "target_writeback": {
                "record": "proposal",
                "operation": "record_review_outcome",
                "subject_id": subject_id,
                "fields": ["review_outcome"],
            },
            "urgency": 0.62,
            "client_value": 0.82,
            "readiness": 0.9,
            "retire_when": "proposal is converted, archived, or superseded",
        },
    }
    meta = table.get(loop_type)
    if not meta:
        meta = {
            "generator": "data_hygiene",
            "objective": "resolve_open_client_item",
            "ask": str(item.get("next_action") or "resolve the open item"),
            "target_writeback": {
                "record": "client_state",
                "operation": "resolve_open_loop",
                "subject_id": subject_id,
                "fields": [],
            },
            "urgency": 0.4,
            "client_value": 0.5,
            "readiness": 0.8,
            "retire_when": "open loop no longer appears",
        }
    receptivity = 1.0
    score = (
        float(meta["urgency"])
        * float(meta["client_value"])
        * float(meta["readiness"])
        * receptivity
    )
    return {
        "id": f"{loop_type}:{subject_id or item.get('loop_id') or subject}",
        "status": "open",
        "lifecycle_stage": "active",
        "generator": meta["generator"],
        "source": "proactive_engine",
        "source_view": "client_state_view.v1",
        "priority": item.get("priority"),
        "phase": item.get("phase"),
        "objective": meta["objective"],
        "subject": subject,
        "subject_id": subject_id,
        "ask": meta["ask"],
        "target_writeback": meta["target_writeback"],
        "retire_when": meta["retire_when"],
        "readiness": meta["readiness"],
        "receptivity": receptivity,
        "urgency": meta["urgency"],
        "client_value": meta["client_value"],
        "score": round(score, 4),
        "source_loop_id": item.get("loop_id"),
        "source_loop_type": loop_type,
    }


def _money_pool_objective_ask(missing: List[Any]) -> str:
    clean = [str(field) for field in missing if field]
    if not clean:
        return "confirm the remaining investment inputs"
    if len(clean) == 1:
        return f"confirm {clean[0]}"
    if len(clean) == 2:
        return f"confirm {clean[0]} and {clean[1]}"
    return "confirm " + ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _format_target_writeback(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "unknown")
    record = value.get("record") or "unknown"
    operation = value.get("operation") or "update"
    fields = value.get("fields") or []
    fields_text = ",".join(str(field) for field in fields) if fields else "outcome"
    return f"{record}.{operation}({fields_text})"


def _agenda_meta(loop_type: str) -> Dict[str, Any]:
    table = {
        "onboarding_incomplete": {
            "priority": 10,
            "phase": "understand_client",
            "reason": "The advisor needs core profile and risk context before pushing planning decisions.",
        },
        "planning_refresh_pending": {
            "priority": 15,
            "phase": "refresh_planning_analysis",
            "reason": "Planning artifacts are being refreshed for the latest confirmed Client File version.",
        },
        "money_pool_missing_fields": {
            "priority": 20,
            "phase": "complete_investment_intent",
            "reason": "A money pool needs amount, horizon, purpose, and risk before a reliable proposal.",
        },
        "policy_decision_pending": {
            "priority": 30,
            "phase": "review_policy",
            "reason": "A proposed policy is ready for explanation, refinement, or client decision.",
        },
        "stale_policy_review": {
            "priority": 35,
            "phase": "review_policy_fit",
            "reason": "A client fact changed, so an affected policy should be checked before relying on it.",
        },
        "stale_proposal_review": {
            "priority": 38,
            "phase": "review_proposal_fit",
            "reason": "A client fact changed, so an affected proposal should be checked before relying on it.",
        },
        "proposal_review": {
            "priority": 40,
            "phase": "review_proposal_artifact",
            "reason": "A proposal artifact exists and should be explained or refined if it is still actionable.",
        },
    }
    return table.get(loop_type, {
        "priority": 90,
        "phase": "follow_up",
        "reason": "An open client item needs follow-up.",
    })


def _money_pool_open_loops(pools: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    loops: List[Dict[str, Any]] = []
    closed_states = {"archived", "cancelled", "deployed", "closed"}
    for pool in pools:
        missing = pool.get("missing_fields") or []
        state = str(pool.get("state") or "").lower()
        if not missing or state in closed_states:
            continue
        first_missing = missing[0]
        loops.append({
            "id": f"money_pool:{pool.get('id') or pool.get('label')}",
            "type": "money_pool_missing_fields",
            "status": "collecting_inputs",
            "subject": pool.get("label"),
            "subject_id": pool.get("id"),
            "missing_fields": missing,
            "next_action": f"ask_for_{first_missing}",
        })
    return loops


def _onboarding_open_loops(
    onboarding: Dict[str, Any],
    completeness: Dict[str, Any],
) -> List[Dict[str, Any]]:
    status = str(onboarding.get("advisor_onboarding_status") or "not_started").lower()
    if status in {"completed", "complete", "done"} or completeness.get("complete") is True:
        return []
    current_step = onboarding.get("current_step") or "start"
    return [{
        "id": "onboarding",
        "type": "onboarding_incomplete",
        "status": status,
        "subject": "Client onboarding",
        "subject_id": None,
        "missing_fields": list(completeness.get("missing_areas") or []),
        "next_action": f"continue_{current_step}",
    }]


def _planning_open_loops(
    *,
    client_file_version: Any,
    current_planning_set: Any,
    planning_refresh: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(client_file_version, int) or client_file_version <= 0:
        return []
    current = current_planning_set if isinstance(current_planning_set, dict) else {}
    if (
        current.get("status") == "ready"
        and current.get("source_client_version") == client_file_version
    ):
        return []
    refresh = planning_refresh if isinstance(planning_refresh, dict) else {}
    return [{
        "id": "planning_refresh",
        "type": "planning_refresh_pending",
        "status": str(refresh.get("status") or "stale"),
        "subject": "Financial planning analysis",
        "subject_id": None,
        "missing_fields": [],
        "next_action": "wait_for_planning_refresh",
    }]


def _policy_open_loops(policies: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    loops: List[Dict[str, Any]] = []
    for policy in policies:
        loops.append({
            "id": f"policy:{policy.get('id')}",
            "type": "policy_decision_pending",
            "status": "awaiting_client_review",
            "subject": policy.get("title"),
            "subject_id": policy.get("id"),
            "next_action": "explain_or_confirm_policy",
        })
    return loops


def _proposal_artifact_open_loops(
    artifacts: Iterable[Dict[str, Any]],
    *,
    active_proposal_ids: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    loops: List[Dict[str, Any]] = []
    active_proposal_ids = active_proposal_ids or set()
    actionable_statuses = {"ready", "draft", "proposed", "pending_review"}
    for artifact in artifacts:
        artifact_id = str(artifact.get("id") or "")
        if artifact_id and artifact_id in active_proposal_ids:
            continue
        status = str(artifact.get("status") or "").lower()
        payload_summary = _safe_dict(artifact.get("payload_summary"))
        if payload_summary.get("review_outcome"):
            continue
        if status and status not in actionable_statuses:
            continue
        loops.append({
            "id": f"proposal:{artifact.get('id')}",
            "type": "proposal_review",
            "status": "awaiting_client_review" if status != "draft" else "drafting",
            "subject": artifact.get("title"),
            "subject_id": artifact.get("id"),
            "next_action": "explain_or_refine_proposal",
        })
    return loops


def _stale_impact_open_loops(
    impacts: Iterable[Dict[str, Any]],
    *,
    policies: Iterable[Dict[str, Any]],
    proposals: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    loops: List[Dict[str, Any]] = []
    seen: set[str] = set()
    policy_rows = [item for item in policies if isinstance(item, dict)]
    proposal_rows = [item for item in proposals if isinstance(item, dict)]
    reviewed_policy_sources = _reviewed_stale_sources(policy_rows)
    reviewed_proposal_sources = _reviewed_stale_sources(proposal_rows)
    for impact in impacts:
        record = str(impact.get("record") or "").lower()
        if record not in {"policy", "proposal"}:
            continue
        source_record = str(impact.get("source_record") or "").lower()
        if (
            source_record == "client_file.facts"
            and record == "policy"
            and not policy_rows
        ):
            continue
        if (
            source_record == "client_file.facts"
            and record == "proposal"
            and not proposal_rows
        ):
            continue
        source_id = impact.get("source_id")
        if record == "policy" and str(source_id or "") in reviewed_policy_sources:
            continue
        if record == "proposal" and str(source_id or "") in reviewed_proposal_sources:
            continue
        loop_type = f"stale_{record}_review"
        loop_id = f"{loop_type}:{source_id or impact.get('source_writeback_event_id') or record}"
        if loop_id in seen:
            continue
        seen.add(loop_id)
        subject = (
            f"{record.title()} affected by {impact.get('source_record') or 'updated client facts'}"
        )
        loops.append({
            "id": loop_id,
            "type": loop_type,
            "status": impact.get("status") or "needs_review",
            "subject": subject,
            "subject_id": source_id,
            "source_record": impact.get("source_record"),
            "source_id": source_id,
            "reason": impact.get("reason"),
            "next_action": f"review_{record}_fit",
        })
    return loops


def _reviewed_stale_sources(records: Iterable[Dict[str, Any]]) -> set[str]:
    reviewed: set[str] = set()
    for record in records:
        summary = _safe_dict(record.get("payload_summary"))
        stale_review = _safe_dict(summary.get("stale_review"))
        review_outcome = _safe_dict(summary.get("review_outcome"))
        source_id = (
            stale_review.get("source_id")
            or _safe_dict(review_outcome.get("source")).get("source_id")
        )
        if source_id is None:
            source_id = stale_review.get("source_subject")
        if not source_id:
            continue
        stale_status = str(stale_review.get("status") or "").lower()
        if review_outcome or stale_status in {"resolved", "needs_revision"}:
            reviewed.add(str(source_id))
    return reviewed


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


__all__ = [
    "CLIENT_FILE_SECTIONS",
    "CLIENT_FILE_SECTION_TABLES",
    "CLIENT_FILE_TABLE_EXCLUSIONS",
    "Section",
    "build_client_state_view",
    "format_client_state_for_advisor",
]
