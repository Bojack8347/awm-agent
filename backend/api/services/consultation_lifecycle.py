"""Single application boundary for consultation lifecycle transitions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from api.persistence.consultation_lifecycle import (
    begin_or_renew_interaction,
    checkpoint_interaction,
    complete_consultation_from_objective,
    end_interaction,
    ensure_open_consultation,
    expire_interaction_leases,
    get_active_consultation,
    get_consultation_engagement,
    heartbeat_interaction,
)


class ConsultationLifecycleService:
    def __init__(self, *, companion_sessions: Any, planning_coordinator_factory=None) -> None:
        self._companion_sessions = companion_sessions
        self._planning_coordinator_factory = planning_coordinator_factory

    def ensure_open(self, *, auth_session: Dict[str, Any], companion_session_id: str, **kwargs: Any) -> Dict[str, Any]:
        self._authorize_conversation(auth_session, companion_session_id)
        return ensure_open_consultation(
            client_id=str(auth_session["client_id"]),
            companion_session_id=companion_session_id,
            **kwargs,
        )

    def begin_or_renew(self, *, auth_session: Dict[str, Any], engagement_id: str, companion_session_id: str, **kwargs: Any) -> Dict[str, Any]:
        self._authorize_conversation(auth_session, companion_session_id)
        result = begin_or_renew_interaction(
            engagement_id=engagement_id,
            client_id=str(auth_session["client_id"]),
            companion_session_id=companion_session_id,
            **kwargs,
        )
        self._signal_if_changed(auth_session, result)
        return result

    def heartbeat(self, *, auth_session: Dict[str, Any], engagement_id: str, interaction_id: str, expected_version: int) -> Dict[str, Any]:
        result = heartbeat_interaction(
            engagement_id=engagement_id, client_id=str(auth_session["client_id"]),
            interaction_id=interaction_id, expected_version=expected_version,
        )
        self._signal_if_changed(auth_session, result)
        return result

    def end(self, *, auth_session: Dict[str, Any], engagement_id: str, interaction_id: str, end_reason: str, expected_version: Optional[int] = None) -> Dict[str, Any]:
        result = end_interaction(
            engagement_id=engagement_id, client_id=str(auth_session["client_id"]),
            interaction_id=interaction_id, end_reason=end_reason,
            expected_version=expected_version,
        )
        self._signal_if_changed(auth_session, result)
        return result

    def checkpoint(self, *, auth_session: Dict[str, Any], engagement_id: str, interaction_id: str, client_turn_id: str, reason: str, transcript: Dict[str, Any]) -> Dict[str, Any]:
        return checkpoint_interaction(
            engagement_id=engagement_id, client_id=str(auth_session["client_id"]),
            interaction_id=interaction_id, client_turn_id=client_turn_id,
            reason=reason, transcript=transcript,
        )

    def get_active(self, *, auth_session: Dict[str, Any], session_type: str, journey_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return get_active_consultation(
            client_id=str(auth_session["client_id"]), session_type=session_type,
            journey_id=journey_id,
        )

    def get_owned(self, *, auth_session: Dict[str, Any], engagement_id: str) -> Optional[Dict[str, Any]]:
        return get_consultation_engagement(
            engagement_id=engagement_id, client_id=str(auth_session["client_id"]),
        )

    def complete_from_objective(self, *, client_id: str, engagement_id: str, onboarding_transition_ok: bool, objective_status: str) -> Dict[str, Any]:
        return complete_consultation_from_objective(
            engagement_id=engagement_id, client_id=client_id,
            onboarding_transition_ok=onboarding_transition_ok,
            objective_status=objective_status,
        )

    def expire_leases(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        return expire_interaction_leases(limit=limit)

    def _authorize_conversation(self, auth_session: Dict[str, Any], session_id: str) -> None:
        if not session_id:
            raise ValueError("companion_session_id_required")
        if self._companion_sessions.get_owned(
            auth_session=auth_session, session_id=session_id, require_active=True,
        ) is None:
            raise PermissionError("companion_session_forbidden")

    def _signal_if_changed(self, auth_session: Dict[str, Any], result: Dict[str, Any]) -> None:
        # PostgreSQL updates the compatibility projection in the lease transaction.
        # The callback wakes pending planning work and supplies the same projection
        # for process-local tests/development.
        if not result.get("activity_changed") or self._planning_coordinator_factory is None:
            return
        engagement = result.get("engagement") or {}
        self._planning_coordinator_factory().consultation_state_changed(
            client_id=str(auth_session["client_id"]),
            active=engagement.get("status") == "active",
        )
