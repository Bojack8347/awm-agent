"""Unified client state read model.

This module intentionally does not introduce a new source-of-truth table. It is
a stable read facade that gathers the current user facts, diagnosis, journeys,
plans, holdings, events, and pending confirmations into one shape for agents,
screens, and admin tools.
"""

from __future__ import annotations

from typing import Any, Dict

from .core import client_file_connection_scope
from .advisory import (
    get_advisory_events,
    list_advisory_holdings,
    list_advisory_plans,
)
from .journeys import get_active_journey_runs, get_proposed_policies
from .knowledge import (
    get_knowledge_facts,
    get_latest_diagnosis_snapshot,
    get_latest_knowledge_snapshot,
    get_pending_confirmations,
)


def get_unified_client_state(client_id: str) -> Dict[str, Any]:
    """Return the current aggregate state using one connection checkout."""
    with client_file_connection_scope():
        return _get_unified_client_state(client_id)


def _get_unified_client_state(client_id: str) -> Dict[str, Any]:
    """Assemble the legacy internal building block within an active scope."""
    knowledge_snapshot = get_latest_knowledge_snapshot(client_id) or {}
    diagnosis_snapshot = get_latest_diagnosis_snapshot(client_id) or {}
    pending_confirmations = get_pending_confirmations(client_id) or []
    facts = get_knowledge_facts(client_id) or []
    active_journeys = get_active_journey_runs(client_id) or []
    proposed_policies = get_proposed_policies(client_id) or []
    advisory_plans = list_advisory_plans(client_id=client_id, limit=50)
    holdings = list_advisory_holdings(client_id=client_id, limit=100)
    open_events = get_advisory_events(client_id=client_id, status="open", limit=50)

    return {
        "client_id": client_id,
        "knowledge": {
            "latest_snapshot": knowledge_snapshot,
            "snapshot_data": knowledge_snapshot.get("snapshot_data", {}),
            "facts": facts,
            "pending_confirmations": pending_confirmations,
        },
        "diagnosis": {
            "latest_snapshot": diagnosis_snapshot,
            "diagnosis_data": diagnosis_snapshot.get("diagnosis_data", {}),
        },
        "journeys": {
            "active": active_journeys,
            "proposed_policies_legacy": proposed_policies,
        },
        "advisory": {
            "plans": advisory_plans,
            "holdings": holdings,
            "open_events": open_events,
        },
        "summary": {
            "knowledge_snapshot_version": knowledge_snapshot.get("snapshot_version"),
            "diagnosis_snapshot_version": diagnosis_snapshot.get("snapshot_version"),
            "pending_confirmation_count": len(pending_confirmations),
            "active_journey_count": len(active_journeys),
            "advisory_plan_count": len(advisory_plans),
            "holding_count": len(holdings),
            "open_advisory_event_count": len(open_events),
        },
    }


__all__ = ["get_unified_client_state"]
