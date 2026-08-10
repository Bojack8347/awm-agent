"""Pending confirmation resolution pipeline.

Chain: pending confirmation -> canonical fact commit -> rebuilt snapshot -> diagnosis

Expected context keys (input):
    client_id, confirmation_id, confirmation (the confirmation dict from DB)

Produced context keys (output):
    committed_fact, snapshot_version, diagnosis_version
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from advisor.runtime.pipeline import Pipeline, PipelineStep

try:
    from api.persistence import (
        update_knowledge_fact as db_update_knowledge_fact,
        get_knowledge_fact as db_get_knowledge_fact,
    )
except ImportError:
    db_update_knowledge_fact = lambda *a, **kw: False
    db_get_knowledge_fact = lambda *a, **kw: None


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def _write_confirmed_value(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Write the proposed value into the canonical knowledge fact."""
    confirmation = ctx["confirmation"]
    fact_id = confirmation.get("fact_id")
    change_type = confirmation.get("change_type", "new_fact")
    proposed_value = confirmation.get("proposed_value")
    now_iso = datetime.now(timezone.utc).isoformat()

    if not fact_id:
        ctx["_skip_remaining"] = True
        return ctx

    if change_type == "update_value" and proposed_value is not None:
        db_update_knowledge_fact(
            fact_id,
            value=proposed_value,
            status="confirmed",
            last_confirmed_at=now_iso,
        )
    elif change_type == "new_fact":
        db_update_knowledge_fact(
            fact_id,
            status="confirmed",
            last_confirmed_at=now_iso,
        )
    elif change_type == "update_status" and proposed_value is not None:
        target_status = proposed_value if isinstance(proposed_value, str) else "confirmed"
        db_update_knowledge_fact(
            fact_id,
            status=target_status,
            last_confirmed_at=now_iso,
        )
    else:
        ctx["_skip_remaining"] = True
        return ctx

    ctx["committed_fact"] = db_get_knowledge_fact(fact_id)
    return ctx


def _rebuild_snapshot(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild snapshot, regenerating only the affected section."""
    from api.server import get_knowledge_service

    committed_fact = ctx.get("committed_fact")
    if not committed_fact:
        ctx["_skip_remaining"] = True
        return ctx

    ctx["snapshot_version"] = get_knowledge_service().rebuild_snapshot_targeted(
        client_id=ctx["client_id"],
        changed_fact=committed_fact,
        trigger_event="confirmation_resolved",
        trigger_event_id=ctx["confirmation_id"],
    )
    return ctx


def _refresh_diagnosis(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Materiality-gated diagnosis refresh."""
    from api.server import get_diagnosis_service

    committed_fact = ctx.get("committed_fact")
    if not committed_fact:
        return ctx

    ctx["diagnosis_version"] = get_diagnosis_service().refresh_if_material(
        client_id=ctx["client_id"],
        committed_facts=[committed_fact],
        caller="confirmation_pipeline",
    )
    return ctx


# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------

CONFIRMATION_PIPELINE = Pipeline("confirmation", [
    PipelineStep("write_confirmed_value", _write_confirmed_value),
    PipelineStep(
        "rebuild_snapshot",
        _rebuild_snapshot,
        condition=lambda ctx: not ctx.get("_skip_remaining"),
    ),
    PipelineStep(
        "refresh_diagnosis",
        _refresh_diagnosis,
        condition=lambda ctx: not ctx.get("_skip_remaining") and not ctx.get("skip_diagnosis"),
        critical=False,
    ),
])
