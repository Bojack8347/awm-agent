"""Thin application-operation delegates used by blueprints and pipelines."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from api.services.companion import (
    sanitize_result as _companion_sanitize_result,
    ground_result_to_current_turn as _companion_ground_result,
)

from . import state
from .deps import get_companion_service, get_diagnosis_service, get_knowledge_service

def _is_diagnosis_material(facts: List[Dict[str, Any]]) -> bool:
    return get_diagnosis_service().is_material(facts)


def _carry_forward_section_summaries(
    client_id: str,
    invalidate_sections: Optional[set] = None,
) -> Optional[Dict[str, str]]:
    return get_knowledge_service().carry_forward_section_summaries(
        client_id, invalidate_sections=invalidate_sections
    )


def _rebuild_snapshot_targeted(
    client_id: str,
    changed_fact: Dict[str, Any],
    trigger_event: str,
    trigger_event_id: str,
) -> int:
    return get_knowledge_service().rebuild_snapshot_targeted(
        client_id=client_id,
        changed_fact=changed_fact,
        trigger_event=trigger_event,
        trigger_event_id=trigger_event_id,
    )


def _get_diagnosis_refresh_payload(client_id: str) -> Dict[str, Any]:
    return get_diagnosis_service().get_refresh_state(client_id)


def _queue_diagnosis_refresh_if_material(
    client_id: str,
    committed_facts: List[Dict[str, Any]],
    *,
    caller: str = "unknown",
) -> Dict[str, Any]:
    return get_diagnosis_service().queue_refresh_if_material(
        client_id, committed_facts, caller=caller
    )


def _refresh_diagnosis_if_material(
    client_id: str,
    committed_facts: List[Dict[str, Any]],
    caller: str = "unknown",
) -> Optional[int]:
    return get_diagnosis_service().refresh_if_material(
        client_id, committed_facts, caller
    )


def _commit_confirmed_pending(
    client_id: str,
    confirmation_id: str,
    confirmation: Dict[str, Any],
    caller: str = "unknown",
    refresh_diagnosis: bool = True,
    return_context: bool = False,
) -> Any:
    from advisor.pipelines.confirmation import CONFIRMATION_PIPELINE

    result = CONFIRMATION_PIPELINE.run(context={
        "client_id": client_id,
        "confirmation_id": confirmation_id,
        "confirmation": confirmation,
        "skip_diagnosis": not refresh_diagnosis,
    })
    if return_context:
        return result.context
    return result.context.get("diagnosis_version")


def _queue_confirmation_diagnosis_refresh(
    client_id: str,
    committed_fact: Dict[str, Any],
) -> bool:
    return get_diagnosis_service().queue_confirmation_refresh(client_id, committed_fact)


def _queue_consultation_diagnosis_refresh(
    session_id: str,
    client_id: str,
    committed_facts: List[Dict[str, Any]],
) -> bool:
    """Queue async diagnosis refresh after consultation; also updates task state."""
    if not committed_facts:
        return False
    result = get_diagnosis_service().queue_refresh_if_material(
        client_id=client_id,
        committed_facts=committed_facts,
        caller="consultation_background_refresh",
    )
    with state._TASK_LOCK:
        task = state._CONSULTATION_TASKS.get(session_id)
        if task is not None:
            task["diagnosis_status"] = result.get("status")
            task["diagnosis_requested_snapshot_version"] = result.get("requested_knowledge_snapshot_version")
            if result.get("diagnosis_snapshot_version"):
                task["diagnosis_snapshot_version"] = result.get("diagnosis_snapshot_version")
    return bool(result.get("queued"))


def _sanitize_companion_result(result: Dict[str, Any], user_message: str) -> Dict[str, Any]:
    return _companion_sanitize_result(result, user_message)


def _ground_companion_result_to_current_turn(
    result: Dict[str, Any],
    user_message: str,
) -> Dict[str, Any]:
    return _companion_ground_result(result, user_message)


def _apply_same_turn_companion_confirmations(
    client_id: str,
    pending_confirmations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return get_companion_service().apply_same_turn_confirmations(client_id, pending_confirmations)
