"""Client File read/write interfaces for the advisor runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Optional, Protocol


@dataclass(frozen=True)
class ClientFileSnapshot:
    """Stable read model handed to the Main Agent for one turn."""

    client_id: str
    payload: Dict[str, Any]


class ClientFileReader(Protocol):
    """Reads the current Client File view for a client."""

    def read(self, client_id: str) -> ClientFileSnapshot:
        raise NotImplementedError


class ClientFileWriter(Protocol):
    """Writes sourced events back to Client File through deterministic services."""

    def write_event(
        self,
        client_id: str,
        *,
        event_type: str,
        payload: Dict[str, Any],
        source: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class ClientStateViewReader:
    """Client File reader backed by the app's client-state projection."""

    def __init__(self, *, sources: Optional[Dict[str, Any]] = None):
        self.sources = sources

    def read(self, client_id: str) -> ClientFileSnapshot:
        from api.services.client_state_view import build_client_state_view

        payload = build_client_state_view(client_id, sources=self.sources)
        return ClientFileSnapshot(client_id=client_id, payload=payload)


class InMemoryClientFileWriter:
    """Test/development writer that records sourced Client File events in memory."""

    def __init__(self) -> None:
        self.events: list[Dict[str, Any]] = []

    def write_event(
        self,
        client_id: str,
        *,
        event_type: str,
        payload: Dict[str, Any],
        source: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        writeback = _build_writeback(event_type=event_type, payload=payload)
        event = {
            "client_id": client_id,
            "event_type": event_type,
            "payload": payload,
            "source": source or {},
        }
        self.events.append(event)
        return {"ok": True, "event": event, "writeback": writeback}


class BusinessEventClientFileWriter:
    """Production writer that records Client File changes as durable events.

    The advisor architecture treats Client File as the spine. At this stage we
    avoid creating a second store: writebacks are appended to the existing
    business event stream and projected by `services.client_state_view`.
    """

    def write_event(
        self,
        client_id: str,
        *,
        event_type: str,
        payload: Dict[str, Any],
        source: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from api.services.events import safe_publish_event

        source_payload = source or {}
        writeback = _build_writeback(event_type=event_type, payload=payload)
        onboarding_lifecycle: Optional[Dict[str, Any]] = None
        if event_type == "update_objective_status" and str(
            payload.get("objective_id") or ""
        ).startswith("onboarding_incomplete:"):
            onboarding_lifecycle = _write_onboarding_objective_lifecycle(
                client_id=client_id,
                payload=payload,
            )
            if onboarding_lifecycle.get("ok") is not True:
                return {
                    "ok": False,
                    "event": None,
                    "event_type": "client_file.writeback",
                    "writeback": writeback,
                    "onboarding_lifecycle": onboarding_lifecycle,
                }
        if event_type in {"commit_facts", "save_fact"}:
            from api.persistence import write_canonical_client_file_update

            event_result = write_canonical_client_file_update(
                client_id=client_id,
                event_type=event_type,
                payload=payload,
                source=source_payload,
                writeback=writeback,
            )
            event = event_result.get("event")
        else:
            event_result = None
        event_payload = {
            "source": source_payload,
            "writeback": writeback,
            "stale_impacts": _stale_impacts_for_writeback(writeback),
        }
        if event_result is None:
            event = safe_publish_event(
                event_type="client_file.writeback",
                client_id=client_id,
                aggregate_type="client_file",
                aggregate_id=client_id,
                event_source="advisor_runtime",
                event_key=_event_key(client_id=client_id, event_type=event_type, payload=payload, source=source_payload),
                payload=event_payload,
                status="consumed",
            )
        invalidation_result: Optional[Dict[str, Any]] = None
        if event_type in {"commit_facts", "save_fact"} and event is not None:
            from api.persistence import (  # pylint: disable=import-outside-toplevel
                mark_investment_assessments_requires_revalidation,
            )

            invalidation_result = mark_investment_assessments_requires_revalidation(
                client_id=client_id,
                source_event_id=(event.get("id") if isinstance(event, dict) else None),
                reason="Confirmed Client File facts changed after assessment creation.",
            )
        invalidation_ok = (
            bool(invalidation_result.get("ok"))
            if isinstance(invalidation_result, dict)
            else True
        )
        return {
            "ok": event is not None and invalidation_ok,
            "event": event,
            "event_type": "client_file.writeback",
            "writeback": writeback,
            "assessment_invalidation": invalidation_result,
            "onboarding_lifecycle": onboarding_lifecycle,
            **(
                {"client_file_version": event_result.get("client_file_version")}
                if event_result is not None
                else {}
            ),
        }


def _write_onboarding_objective_lifecycle(
    *,
    client_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply advisor-onboarding state only through objective writebacks."""

    requested = str(payload.get("status") or "").strip().lower()
    if requested in {"in_progress", "active", "started", "reactivated"}:
        target = "in_progress"
    elif requested in {"complete", "completed"}:
        target = "complete"
        from api.persistence import list_canonical_client_facts
        from api.services.onboarding_completeness import advisor_onboarding_completeness

        completeness = advisor_onboarding_completeness(
            list_canonical_client_facts(client_id=client_id)
        )
        if not completeness["complete"]:
            return {
                "ok": False,
                "error": "advisor_onboarding_incomplete",
                "missing_areas": completeness["missing_areas"],
            }
    else:
        return {"ok": True, "transition": "unchanged"}

    from api.persistence import transition_advisor_onboarding_status

    transition = transition_advisor_onboarding_status(
        client_id=client_id,
        target_status=target,
        allow_reonboarding=(
            requested == "reactivated" and bool(payload.get("explicit_reonboarding"))
        ),
    )
    if transition.get("ok") is not True:
        return transition

    consultation = None
    if target == "complete":
        from api.persistence.consultation_lifecycle import (
            complete_consultation_from_objective,
            get_active_consultation,
        )
        active = get_active_consultation(
            client_id=client_id,
            session_type="initial_consultation",
        )
        if active:
            consultation = complete_consultation_from_objective(
                engagement_id=str(active["engagement_id"]),
                client_id=client_id,
                onboarding_transition_ok=True,
                objective_status="complete",
            )

    try:
        from api.server.deps import get_planning_refresh_coordinator

        refresh = get_planning_refresh_coordinator().consultation_state_changed(
            client_id=client_id,
            active=target != "complete",
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "planning_consultation_state_failed",
            "detail": str(exc),
            "transition": transition,
        }
    return {"ok": True, "transition": transition, "consultation": consultation, "planning_refresh": refresh}


def _record_for_event(event_type: str) -> str:
    if event_type == "save_consultation_checkpoint":
        return "client_file.consultation_checkpoints"
    if event_type == "subagent_artifact":
        return "client_file.artifacts"
    if event_type == "draft_fact":
        return "client_file.draft_facts"
    if event_type in {"commit_facts", "save_fact"}:
        return "client_file.facts"
    if event_type == "confirmation_decision":
        return "client_file.confirmation_decisions"
    if event_type == "update_objective_status":
        return "client_file.objectives"
    if event_type == "deterministic_service_outcome":
        return "client_file.services"
    if event_type in {"create_investment_assessment", "record_assessment_signoff"}:
        return "client_file.plans"
    return "client_file"


def _build_writeback(*, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    record = _record_for_event(event_type)
    if event_type == "subagent_artifact" and isinstance(payload.get("writeback_target"), str):
        record = str(payload["writeback_target"])
    return {
        "record": record,
        "operation": event_type,
        "subject_id": (
            payload.get("id")
            or payload.get("assessment_id")
            or payload.get("investment_consultation_id")
            or payload.get("money_pool_id")
            or payload.get("fact_type")
        ),
        "subject": (
            payload.get("fact_type")
            or payload.get("pool_label")
            or payload.get("label")
            or payload.get("assessment_id")
            or event_type
        ),
        "fields": sorted(payload.get("facts", {}).keys())
        if isinstance(payload.get("facts"), dict)
        else sorted(payload.keys()),
        "values": payload,
    }


def _stale_impacts_for_writeback(writeback: Dict[str, Any]) -> list[Dict[str, Any]]:
    values = writeback.get("values") if isinstance(writeback, dict) else {}
    values = values if isinstance(values, dict) else {}
    if writeback.get("operation") not in {"commit_facts", "save_fact"}:
        return []
    fields = writeback.get("fields") if isinstance(writeback.get("fields"), list) else []
    if not fields:
        return []
    source_id = writeback.get("subject_id")
    return [
        {
            "record": record,
            "status": "needs_review",
            "source_record": writeback.get("record"),
            "source_id": source_id,
            "reason": "Client File facts changed.",
            "fields": fields,
        }
        for record in (
            "financial_plan",
            "investment_assessment",
            "policy",
            "proposal",
        )
    ]


def _event_key(
    *,
    client_id: str,
    event_type: str,
    payload: Dict[str, Any],
    source: Dict[str, Any],
) -> str:
    raw = json.dumps(
        {
            "client_id": client_id,
            "event_type": event_type,
            "payload": payload,
            "source": source,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"advisor_runtime:{client_id}:{event_type}:{digest}"
