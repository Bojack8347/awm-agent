"""Business event outbox worker.

The outbox gives AWM a simple event-driven backbone without introducing a
message broker yet. Producers write durable business_events; this worker claims
pending rows and invokes the first downstream handlers.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from api.services.tracing import safe_trace_event

logger = logging.getLogger("awm.services.business_events")
MAX_EVENT_ATTEMPTS = 5

try:
    from api.persistence import (
        claim_pending_business_events as db_claim_pending_business_events,
        create_advisory_event as db_create_advisory_event,
        get_advisory_events as db_get_advisory_events,
        get_advisory_plan as db_get_advisory_plan,
        mark_business_event_consumed as db_mark_business_event_consumed,
        mark_business_event_failed as db_mark_business_event_failed,
    )
except ImportError:
    db_claim_pending_business_events = lambda *a, **kw: []  # noqa: E731
    db_create_advisory_event = lambda *a, **kw: None  # noqa: E731
    db_get_advisory_events = lambda *a, **kw: []  # noqa: E731
    db_get_advisory_plan = lambda *a, **kw: None  # noqa: E731
    db_mark_business_event_consumed = lambda *a, **kw: False  # noqa: E731
    db_mark_business_event_failed = lambda *a, **kw: False  # noqa: E731


class BusinessEventWorker:
    """Drain pending business events with conservative first handlers."""

    def __init__(
        self,
        *,
        diagnosis_service_factory: Any,
        proactive_service_factory: Any,
        planning_coordinator_factory: Any = None,
    ) -> None:
        self._diagnosis_service_factory = diagnosis_service_factory
        self._proactive_service_factory = proactive_service_factory
        self._planning_coordinator_factory = planning_coordinator_factory
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "diagnosis.refresh_requested": self._handle_diagnosis_refresh_requested,
            "advisory.event_derived": self._handle_advisory_event_derived,
            "advisory.decision_recorded": self._handle_advisory_decision_recorded,
            "advisory.holding_imported": self._handle_advisory_holding_imported,
            "advisory.plan_status_changed": self._handle_advisory_plan_status_changed,
        }
        if planning_coordinator_factory is not None:
            self._handlers["client_file.updated"] = self._handle_client_file_updated

    def drain(self, *, limit: int = 20, event_type: Optional[str] = None) -> Dict[str, Any]:
        """Claim and process up to ``limit`` pending events."""
        events = db_claim_pending_business_events(limit=limit, event_type=event_type)
        results: List[Dict[str, Any]] = []
        for event in events:
            results.append(self._process_one(event))
        return {
            "claimed": len(events),
            "event_type": event_type,
            "processed": len(results),
            "consumed": sum(1 for r in results if r.get("status") == "consumed"),
            "failed": sum(1 for r in results if r.get("status") == "failed"),
            "results": results,
        }

    def _process_one(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(event.get("id") or "")
        event_type = str(event.get("event_type") or "")
        client_id = str(event.get("client_id") or "")
        try:
            handler = self._handlers.get(event_type)
            if handler is None:
                result = {
                    "action": "skipped",
                    "reason": "no worker handler registered",
                }
            else:
                result = handler(event)

            db_mark_business_event_consumed(event_id)
            safe_trace_event(
                trace_id=f"tr_event_{event_id}",
                client_id=client_id or None,
                source_type="business_event_worker",
                source_id=event_id,
                event_type="business_event.consumed",
                event_name="Business event consumed",
                actor_type="system",
                status="success",
                output_summary=f"{event_type}: {result.get('action')}",
                payload={"business_event": event, "result": result},
            )
            return {
                "id": event_id,
                "event_type": event_type,
                "status": "consumed",
                **result,
            }
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("[business_events] event=%s failed: %s", event_id, exc)
            attempts = int(event.get("attempts") or 1)
            retry = attempts < MAX_EVENT_ATTEMPTS
            db_mark_business_event_failed(event_id, error=str(exc), retry=retry)
            safe_trace_event(
                trace_id=f"tr_event_{event_id}",
                client_id=client_id or None,
                source_type="business_event_worker",
                source_id=event_id,
                event_type="business_event.failed",
                event_name="Business event processing failed",
                actor_type="system",
                status="failed",
                error_message=str(exc)[:500],
                payload={"business_event": event},
            )
            return {
                "id": event_id,
                "event_type": event_type,
                "status": "failed",
                "error": str(exc),
                "retry": retry,
                "attempts": attempts,
            }

    def _handle_diagnosis_refresh_requested(self, event: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(event.get("client_id") or "")
        if not client_id:
            return {"action": "skipped", "reason": "missing client_id"}
        result = self._diagnosis_service_factory().run_queued_refresh(client_id)
        return {
            "action": "diagnosis_refresh_run",
            "diagnosis_result": result,
        }

    def _handle_client_file_updated(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return self._planning_coordinator_factory().handle_client_file_updated(event)

    def _handle_advisory_event_derived(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        advisory_event_id = str(payload.get("advisory_event_id") or event.get("aggregate_id") or "")
        client_id = str(event.get("client_id") or "")
        if not advisory_event_id or not client_id:
            return {"action": "skipped", "reason": "missing advisory_event_id or client_id"}

        matches = db_get_advisory_events(client_id=client_id, status=None, limit=100)
        advisory_event: Optional[Dict[str, Any]] = None
        for candidate in matches:
            if str(candidate.get("id")) == advisory_event_id:
                advisory_event = candidate
                break
        if not advisory_event:
            return {"action": "skipped", "reason": "advisory_event_not_found"}

        outreach = self._proactive_service_factory().compose_advisory_event(advisory_event)
        return {
            "action": "advisory_outreach_composed",
            "outreach": outreach,
        }

    def _handle_advisory_decision_recorded(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        client_id = str(event.get("client_id") or "")
        plan_id = str(payload.get("advisory_plan_id") or event.get("aggregate_id") or "")
        decision = str(payload.get("decision") or "").lower()
        if not client_id or not plan_id:
            return {"action": "skipped", "reason": "missing client_id or advisory_plan_id"}

        if decision not in {"accept", "accepted", "activate", "activated", "defer", "deferred"}:
            return {"action": "decision_recorded_no_followup", "decision": decision}

        plan = db_get_advisory_plan(advisory_plan_id=plan_id, client_id=client_id)
        advisory_event = db_create_advisory_event(
            client_id=client_id,
            advisory_plan_id=plan_id,
            event_type=(
                "advisory.plan_accepted_next_steps"
                if decision in {"accept", "accepted", "activate", "activated"}
                else "advisory.plan_deferred_followup"
            ),
            source_type="business_event_worker",
            source_id=str(event.get("id") or ""),
            dedupe_key=f"business:{event.get('id')}:decision_followup",
            payload={
                "business_event_id": event.get("id"),
                "decision": decision,
                "advisory_plan_id": plan_id,
                "plan_title": (plan or {}).get("title"),
                "reason_code": payload.get("reason_code"),
            },
        )
        outreach = None
        if advisory_event:
            outreach = self._proactive_service_factory().compose_advisory_event(advisory_event)
        return {
            "action": "decision_followup_created",
            "decision": decision,
            "advisory_event_id": advisory_event.get("id") if advisory_event else None,
            "outreach": outreach,
        }

    def _handle_advisory_holding_imported(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        client_id = str(event.get("client_id") or "")
        holding_id = str(payload.get("holding_id") or event.get("aggregate_id") or "")
        if not client_id or not holding_id:
            return {"action": "skipped", "reason": "missing client_id or holding_id"}

        advisory_event = db_create_advisory_event(
            client_id=client_id,
            advisory_plan_id=payload.get("advisory_plan_id"),
            event_type="holding.imported_review_needed",
            source_type="business_event_worker",
            source_id=str(event.get("id") or ""),
            dedupe_key=f"business:{event.get('id')}:holding_review",
            payload={
                "business_event_id": event.get("id"),
                "holding_id": holding_id,
                "advisory_plan_id": payload.get("advisory_plan_id"),
                "symbol": payload.get("symbol"),
                "holding_type": payload.get("holding_type"),
                "reason": "User imported or created a holding; review fit, risk, and plan alignment.",
            },
        )
        outreach = None
        if advisory_event:
            outreach = self._proactive_service_factory().compose_advisory_event(advisory_event)
        return {
            "action": "holding_review_event_created",
            "advisory_event_id": advisory_event.get("id") if advisory_event else None,
            "outreach": outreach,
        }

    def _handle_advisory_plan_status_changed(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        status = str(payload.get("status") or "").lower()
        client_id = str(event.get("client_id") or "")
        plan_id = str(payload.get("advisory_plan_id") or event.get("aggregate_id") or "")
        if not client_id or not plan_id:
            return {"action": "skipped", "reason": "missing client_id or advisory_plan_id"}
        if status not in {"accepted", "active", "cancelled"}:
            return {"action": "plan_status_observed", "status": status}

        advisory_event = db_create_advisory_event(
            client_id=client_id,
            advisory_plan_id=plan_id,
            event_type=f"advisory.plan_status_{status}",
            source_type="business_event_worker",
            source_id=str(event.get("id") or ""),
            dedupe_key=f"business:{event.get('id')}:plan_status",
            payload={
                "business_event_id": event.get("id"),
                "advisory_plan_id": plan_id,
                "status": status,
            },
        )
        return {
            "action": "plan_status_event_created",
            "status": status,
            "advisory_event_id": advisory_event.get("id") if advisory_event else None,
        }


__all__ = ["BusinessEventWorker"]
