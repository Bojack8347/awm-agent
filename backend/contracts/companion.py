"""Companion API response contracts."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from contracts.base import AwmContractModel


CompanionActionType = Literal[
    "chat",
    "confirm_fact",
    "recommend_journey",
    "open_consultation",
    "handoff_to_journey",
    "analyze_financial_question",
]


class PersistedMessageContract(AwmContractModel):
    """Canonical IDs returned to mobile for optimistic-message reconciliation."""

    user_message_id: str
    assistant_message_id: Optional[str] = None
    status: str


class CompanionMessageResponseContract(AwmContractModel):
    """Response contract for the non-streaming companion turn endpoint."""

    success: bool
    assistant_message: Optional[str] = None
    action_type: Optional[CompanionActionType] = None
    ui_directive: Optional[str] = None
    proposed_fact_changes: Optional[List[Dict[str, Any]]] = None
    pending_confirmation_ids: Optional[List[str]] = None
    recommended_journey: Optional[Dict[str, Any]] = None
    next_session_type: Optional[str] = None
    reasoning_analysis: Optional[Any] = None
    reasoning_task_id: Optional[str] = None
    journey_call: Optional[Dict[str, Any]] = None
    journey_dispatch: Optional[Dict[str, Any]] = None
    client_file_stale_marks: Optional[List[Dict[str, Any]]] = None
    session_id: Optional[str] = None
    response_id: Optional[str] = None
    persisted: Optional[PersistedMessageContract] = None
    error: Optional[str] = None


def validate_companion_message_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate companion response shape while preserving the original dict."""
    CompanionMessageResponseContract.model_validate(payload)
    return payload
