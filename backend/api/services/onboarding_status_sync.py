"""Drive client_onboarding_status from durable Client File writebacks.

Companion chat used to update only business_events / Client File projections while
GET /api/v1/onboarding/status kept reading the MVP table. These helpers keep that
table monotonic with fact commits, onboarding objective completion, and real
projection artifact persistence.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


_TERMINAL_STATUSES = frozenset({"advice_ready", "completed", "complete", "done"})
_FACT_OPERATIONS = frozenset({"commit_facts", "save_fact"})
_OBJECTIVE_OPERATION = "update_objective_status"
_PROJECTION_OPERATION = "projection_artifact_persisted"


def is_onboarding_objective_id(objective_id: Any) -> bool:
    text = str(objective_id or "").strip().lower()
    if not text:
        return False
    return (
        "onboarding" in text
        or text in {"complete_onboarding", "onboarding", "onboarding:complete"}
    )


def is_terminal_onboarding_status(status: Any) -> bool:
    return str(status or "").strip().lower() in _TERMINAL_STATUSES


def is_completed_objective_status(status: Any) -> bool:
    return str(status or "").strip().lower() in {
        "completed",
        "complete",
        "done",
        "resolved",
        "closed",
        "advice_ready",
    }


def is_active_objective_status(status: Any) -> bool:
    return str(status or "").strip().lower() in {
        "in_progress",
        "active",
        "started",
        "resumed",
        "open",
    }


def plan_onboarding_status_transition(
    *,
    current: Optional[Dict[str, Any]],
    operation: str,
    values: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return upsert kwargs for the next onboarding row, or None if no change."""

    current = current if isinstance(current, dict) else {}
    current_status = str(current.get("status") or "not_started").strip().lower()
    current_step = str(current.get("current_step") or "identity").strip() or "identity"
    completed_steps = (
        list(current.get("completed_steps") or [])
        if isinstance(current.get("completed_steps"), list)
        else []
    )
    values = values if isinstance(values, dict) else {}
    operation = str(operation or "").strip()

    if is_terminal_onboarding_status(current_status):
        return None

    if operation in _FACT_OPERATIONS:
        if current_status != "not_started":
            return None
        return {
            "current_step": "discovery" if current_step == "identity" else current_step,
            "status": "in_progress",
            "completed_steps": completed_steps,
            "mark_account_completed": False,
        }

    if operation == _OBJECTIVE_OPERATION:
        objective_id = values.get("objective_id")
        if not is_onboarding_objective_id(objective_id):
            return None
        objective_status = values.get("status")
        if is_completed_objective_status(objective_status):
            return {
                "current_step": "advice_ready",
                "status": "advice_ready",
                "completed_steps": completed_steps or ["discovery"],
                "mark_account_completed": True,
            }
        if is_active_objective_status(objective_status) and current_status == "not_started":
            return {
                "current_step": "discovery" if current_step == "identity" else current_step,
                "status": "in_progress",
                "completed_steps": completed_steps,
                "mark_account_completed": False,
            }
        return None

    if operation == _PROJECTION_OPERATION:
        if current_status not in {"not_started", "in_progress"}:
            return None
        return {
            "current_step": "advice_ready",
            "status": "advice_ready",
            "completed_steps": completed_steps or ["discovery"],
            "mark_account_completed": True,
        }

    return None


def sync_onboarding_status_from_writeback(
    client_id: str,
    *,
    operation: str,
    values: Optional[Dict[str, Any]] = None,
    get_status: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    upsert_status: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    mark_completed: Optional[Callable[[str], bool]] = None,
) -> Optional[Dict[str, Any]]:
    """Apply one writeback-driven onboarding transition. Failures are swallowed."""

    if not client_id:
        return None
    try:
        if get_status is None or upsert_status is None:
            from api.persistence import (  # pylint: disable=import-outside-toplevel
                get_onboarding_status,
                upsert_onboarding_status,
            )

            get_status = get_status or get_onboarding_status
            upsert_status = upsert_status or upsert_onboarding_status
        if mark_completed is None:
            from api.persistence import mark_onboarding_completed  # pylint: disable=import-outside-toplevel

            mark_completed = mark_onboarding_completed

        current = get_status(client_id)
        planned = plan_onboarding_status_transition(
            current=current if isinstance(current, dict) else None,
            operation=operation,
            values=values,
        )
        if not planned:
            return None
        mark_account = bool(planned.pop("mark_account_completed", False))
        updated = upsert_status(client_id=client_id, **planned)
        if mark_account and callable(mark_completed):
            mark_completed(client_id)
        return updated if isinstance(updated, dict) else planned
    except Exception as exc:  # pragma: no cover - never break writebacks
        print(f"[onboarding_status_sync] failed for {client_id}: {exc}", flush=True)
        return None


def sync_onboarding_status_from_client_file_event(
    client_id: str,
    *,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    **hooks: Any,
) -> Optional[Dict[str, Any]]:
    return sync_onboarding_status_from_writeback(
        client_id,
        operation=str(event_type or ""),
        values=payload if isinstance(payload, dict) else {},
        **hooks,
    )
