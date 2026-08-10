"""Reusable truth-update subchain.

This is the universal persist sequence that appears after every
KnowledgeUpdaterAgent.update_knowledge() call:

    committed facts → DB bulk upsert
    snapshot_data   → DB store snapshot
    pending confs   → DB store each confirmation

Five+ call sites in app.py previously duplicated this ~25-line block.
"""

from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_API_DIR = Path(__file__).resolve().parents[2] / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

try:
    from api.persistence import (
        bulk_upsert_knowledge_facts as db_bulk_upsert_knowledge_facts,
        get_current_snapshot_version as db_get_current_snapshot_version,
        store_knowledge_snapshot as db_store_knowledge_snapshot,
        store_pending_confirmation as db_store_pending_confirmation,
    )
except ImportError:
    db_bulk_upsert_knowledge_facts = lambda *a, **kw: []
    db_get_current_snapshot_version = lambda *a, **kw: 0
    db_store_knowledge_snapshot = lambda *a, **kw: None
    db_store_pending_confirmation = lambda *a, **kw: None


@dataclass
class TruthUpdateResult:
    """Result of a truth-update commit."""
    committed: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_version: int = 0
    snapshot_data: Dict[str, Any] = field(default_factory=dict)
    pending_confirmations: List[Dict[str, Any]] = field(default_factory=list)


def _patch_snapshot_fact_metadata(
    snapshot_data: Dict[str, Any],
    committed_facts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fill committed fact IDs/status back into the stored snapshot."""
    patched = deepcopy(snapshot_data)
    fact_lookup = {
        (fact.get("domain"), fact.get("category"), fact.get("label")): fact
        for fact in committed_facts
        if fact.get("domain") and fact.get("category") and fact.get("label")
    }

    for domain, domain_data in patched.items():
        if domain == "cashflow_state" or not isinstance(domain_data, dict):
            continue
        for category, category_data in domain_data.items():
            if category == "_section_summaries" or not isinstance(category_data, dict):
                continue
            for label, payload in category_data.items():
                if not isinstance(payload, dict) or "value" not in payload:
                    continue
                committed = fact_lookup.get((domain, category, label))
                if not committed:
                    continue
                payload["fact_id"] = committed.get("id")
                if committed.get("status"):
                    payload["status"] = committed.get("status")
                if committed.get("confidence") is not None:
                    payload["confidence"] = committed.get("confidence")

    return patched


def commit_truth_update(
    client_id: str,
    update_result: Dict[str, Any],
    trigger_event: str,
    trigger_event_id: str,
) -> TruthUpdateResult:
    """Persist the output of KnowledgeUpdaterAgent.update_knowledge().

    This is the single function that replaces the duplicated persist block:
      1. Bulk-upsert committed facts
      2. Store a new knowledge snapshot
      3. Store any pending confirmations

    Args:
        client_id: The client whose truth is being updated.
        update_result: The dict returned by KnowledgeUpdaterAgent.update_knowledge().
        trigger_event: Event name for audit trail (e.g. "consultation_complete").
        trigger_event_id: Event ID for audit trail (e.g. session_id or journey_id).

    Returns:
        TruthUpdateResult with committed facts, new snapshot version, and pending confs.
    """
    committed = update_result.get("committed_facts", [])
    if committed:
        committed_ids = db_bulk_upsert_knowledge_facts(client_id, committed)
        for fact, fact_id in zip(committed, committed_ids):
            if fact_id:
                fact["id"] = fact_id

    snapshot_data = _patch_snapshot_fact_metadata(
        update_result.get("snapshot_data", {}),
        committed,
    )

    new_version = db_get_current_snapshot_version(client_id) + 1
    db_store_knowledge_snapshot(
        client_id=client_id,
        version=new_version,
        snapshot_data=snapshot_data,
        fact_ids=[f.get("id") for f in committed if f.get("id")],
        trigger_event=trigger_event,
        trigger_event_id=trigger_event_id,
    )

    pending = update_result.get("pending_confirmations", [])
    for pc in pending:
        confirmation_id = db_store_pending_confirmation(
            client_id=client_id,
            fact_id=pc.get("fact_id"),
            change_type=pc.get("change_type", "new_fact"),
            proposed_value=pc.get("proposed_value"),
            previous_value=pc.get("previous_value"),
            evidence=pc.get("evidence"),
            source_event_id=trigger_event_id,
        )
        if confirmation_id:
            pc["id"] = confirmation_id

    return TruthUpdateResult(
        committed=committed,
        snapshot_version=new_version,
        snapshot_data=snapshot_data,
        pending_confirmations=pending,
    )
