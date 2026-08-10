"""ProactiveService — orchestrates the proactive engagement pipeline.

Coordinates: ProactivePlanner (deterministic trigger evaluation) →
MessageComposer (LLM message generation) → persistence (outreach log,
escalation tracking, outbound queue).

Extracted as a standalone service (no Flask dependencies) following the same
pattern as CompanionService, DiagnosisService, and KnowledgeService.

The service exposes two main entry points:
  - evaluate_and_compose(client_id) — full pipeline for one client
  - evaluate_all_clients() — batch evaluation for Cloud Scheduler / cron
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("awm.services.proactive")

try:
    from api.persistence import (
        upsert_escalation_tracking as db_upsert_escalation_tracking,
        mark_escalation_backed_off as db_mark_escalation_backed_off,
        store_proactive_outreach as db_store_proactive_outreach,
        enqueue_outbound_message as db_enqueue_outbound_message,
        store_companion_message as db_store_companion_message,
        get_last_companion_interaction_at as db_get_last_companion_interaction_at,
        get_account_id_for_client_id as db_get_account_id_for_client_id,
    )
except ImportError:
    db_upsert_escalation_tracking = lambda *a, **kw: None  # noqa: E731
    db_mark_escalation_backed_off = lambda *a, **kw: False  # noqa: E731
    db_store_proactive_outreach = lambda *a, **kw: None  # noqa: E731
    db_enqueue_outbound_message = lambda *a, **kw: None  # noqa: E731
    db_store_companion_message = lambda *a, **kw: None  # noqa: E731
    db_get_last_companion_interaction_at = lambda *a, **kw: None  # noqa: E731
    db_get_account_id_for_client_id = lambda *a, **kw: None  # noqa: E731

from api.services.proactive_planner import (
    ProactivePlanner,
    TriggerResult,
    MAX_ESCALATION_MENTIONS,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class ProactiveResult:
    """Result of a proactive evaluation + composition for one client."""

    __slots__ = (
        "client_id", "triggered", "trigger_results",
        "bubbles", "push_preview",
        "outreach_log_ids", "queue_entry_id",
        "error",
    )

    def __init__(
        self,
        client_id: str,
        triggered: bool = False,
        trigger_results: Optional[List[TriggerResult]] = None,
        bubbles: Optional[List[str]] = None,
        push_preview: Optional[str] = None,
        outreach_log_ids: Optional[List[str]] = None,
        queue_entry_id: Optional[str] = None,
        error: Optional[str] = None,
    ):
        self.client_id = client_id
        self.triggered = triggered
        self.trigger_results = trigger_results or []
        self.bubbles = bubbles
        self.push_preview = push_preview
        self.outreach_log_ids = outreach_log_ids or []
        self.queue_entry_id = queue_entry_id
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "triggered": self.triggered,
            "triggers": [
                {
                    "trigger_type": t.trigger_type,
                    "trigger_class": t.trigger_class,
                    "guidance_mode": t.guidance_mode,
                }
                for t in self.trigger_results
            ],
            "trigger_count": len(self.trigger_results),
            "bubbles": self.bubbles,
            "push_preview": self.push_preview,
            "outreach_log_ids": self.outreach_log_ids,
            "queue_entry_id": self.queue_entry_id,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# ProactiveService
# ---------------------------------------------------------------------------

class ProactiveService:
    """Orchestrates proactive companion engagement.

    Instantiate once and share across the process (lazy singleton pattern).

    Usage in app.py::

        _PROACTIVE_SERVICE: Optional[ProactiveService] = None

        def get_proactive_service() -> ProactiveService:
            global _PROACTIVE_SERVICE
            if _PROACTIVE_SERVICE is None:
                _PROACTIVE_SERVICE = ProactiveService(
                    get_composer_fn=get_message_composer,
                    get_active_client_ids_fn=get_active_client_ids,
                )
            return _PROACTIVE_SERVICE
    """

    def __init__(
        self,
        get_composer_fn: Callable,
        get_active_client_ids_fn: Optional[Callable] = None,
    ) -> None:
        self._get_composer = get_composer_fn
        self._get_active_client_ids = get_active_client_ids_fn or (lambda: [])
        self._planner = ProactivePlanner()

    def evaluate_and_compose(
        self,
        client_id: str,
        session_id: Optional[str] = None,
    ) -> ProactiveResult:
        """Full proactive pipeline for a single client.

        1. Evaluate all eligible triggers (deterministic, no LLM)
        2. Compose ONE cohesive digest message from all triggers (LLM)
        3. Persist: escalation tracking per trigger, one outreach log, one queue entry
        4. Return the result for the caller to handle delivery

        Args:
            client_id: The client to evaluate.
            session_id: Optional companion session ID to associate the outbound
                        message with (for in-app chat thread insertion).

        Returns:
            ProactiveResult with all details of what happened.
        """
        # Step 1: Evaluate — collect all eligible triggers
        try:
            triggers = self._planner.evaluate(client_id)
        except Exception as exc:
            logger.error(
                "[proactive] Trigger evaluation failed for client=%s: %s",
                client_id, exc,
            )
            return ProactiveResult(
                client_id=client_id,
                error=f"Trigger evaluation failed: {exc}",
            )

        if not triggers:
            logger.info("[proactive] No triggers for client=%s", client_id)
            return ProactiveResult(client_id=client_id, triggered=False)

        logger.info(
            "[proactive] %d trigger(s) fired for client=%s: %s",
            len(triggers),
            client_id,
            ", ".join(f"{t.trigger_class}/{t.trigger_type}" for t in triggers),
        )

        # Step 2: Compose a single digest from all triggers
        try:
            composer = self._get_composer()
            trigger_specs = [
                {
                    "trigger_class": t.trigger_class,
                    "trigger_type": t.trigger_type,
                    "trigger_reason": t.trigger_reason,
                    "guidance_mode": t.guidance_mode,
                    "objective": t.objective,
                    "grounding_facts": t.grounding_facts,
                    "escalation_level": t.escalation_level,
                    "allowed_cta": t.allowed_cta,
                }
                for t in triggers
            ]
            composed = composer.compose(triggers=trigger_specs)
        except Exception as exc:
            logger.error(
                "[proactive] Message composition failed for client=%s: %s",
                client_id, exc,
            )
            return ProactiveResult(
                client_id=client_id,
                triggered=True,
                trigger_results=triggers,
                error=f"Message composition failed: {exc}",
            )

        bubbles = composed.get("bubbles", [])
        push_preview = composed.get("push_preview", "")

        # Step 3: Persist
        # 3a. Update escalation tracking for every trigger independently
        outreach_log_ids: List[str] = []
        first_tracking_id = None
        for trigger in triggers:
            tracking_id = self._update_escalation(
                client_id=client_id,
                trigger=trigger,
            )
            if first_tracking_id is None:
                first_tracking_id = tracking_id

            # 3b. Store one outreach log entry per trigger (for per-topic history)
            outreach_log_id = db_store_proactive_outreach(
                client_id=client_id,
                trigger_class=trigger.trigger_class,
                trigger_type=trigger.trigger_type,
                trigger_reason=trigger.trigger_reason,
                guidance_mode=trigger.guidance_mode,
                escalation_level=trigger.escalation_level,
                objective=trigger.objective,
                bubbles=bubbles,
                grounding_fact_ids=trigger.grounding_fact_ids,
                diagnosis_snapshot_version=trigger.diagnosis_snapshot_version,
                knowledge_snapshot_version=trigger.knowledge_snapshot_version,
                push_preview_text=push_preview,
                allowed_cta=trigger.allowed_cta,
                escalation_tracking_id=tracking_id,
            )
            if outreach_log_id:
                outreach_log_ids.append(outreach_log_id)

        # 3c. Enqueue ONE outbound message (the digest) — linked to the first log entry
        queue_entry_id = db_enqueue_outbound_message(
            client_id=client_id,
            outreach_log_id=outreach_log_ids[0] if outreach_log_ids else "",
            bubbles=bubbles,
            session_id=session_id,
            push_preview_text=push_preview,
        )

        # 3d. Store bubbles as assistant messages in companion chat history.
        # The canonical companion thread ID is companion-{account_id} (UUID),
        # not companion-{client_id}. When no explicit session_id is given (e.g.
        # Cloud Scheduler batch path), look up the account_id from client_id so
        # messages land in the thread the app actually reads on history refresh.
        persist_session_id = session_id
        if not persist_session_id:
            account_id = db_get_account_id_for_client_id(client_id)
            if account_id:
                persist_session_id = f"companion-{account_id}"
            else:
                logger.warning(
                    "[proactive] Could not resolve account_id for client=%s — "
                    "skipping companion history write (bubbles are still queued)",
                    client_id,
                )

        if persist_session_id:
            for bubble in bubbles:
                db_store_companion_message(
                    session_id=persist_session_id,
                    client_id=client_id,
                    role="assistant",
                    content=bubble,
                    state={"proactive": True, "outreach_log_ids": outreach_log_ids},
                )

        logger.info(
            "[proactive] Digest created for client=%s: %d trigger(s), %d bubbles, "
            "outreach_logs=%s, queue=%s",
            client_id, len(triggers), len(bubbles), outreach_log_ids, queue_entry_id,
        )

        return ProactiveResult(
            client_id=client_id,
            triggered=True,
            trigger_results=triggers,
            bubbles=bubbles,
            push_preview=push_preview,
            outreach_log_ids=outreach_log_ids,
            queue_entry_id=queue_entry_id,
        )

    def compose_advisory_event(
        self,
        event: Dict[str, Any],
        *,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create proactive user outreach for one RM/system advisory event.

        Advisory events are explicit triggers: an RM or monitoring job has
        decided something changed enough to contact the user. Reuse the
        existing proactive rails so the message has audit history, a delivery
        queue entry, and a companion chat record.
        """
        client_id = str(event.get("client_id") or "").strip()
        event_id = str(event.get("id") or "").strip()
        event_type = str(event.get("event_type") or "advisory_event").strip()
        if not client_id or not event_id:
            return {
                "success": False,
                "error": "event must include client_id and id",
            }

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        trigger_reason = _advisory_event_reason(event, payload)
        trigger_spec = {
            "trigger_class": "advisory",
            "trigger_type": event_type,
            "trigger_reason": trigger_reason,
            "guidance_mode": "decision_support",
            "objective": (
                "Explain what changed, why it matters, how it affects the "
                "current recommendation, and invite the user to review or ask "
                "questions before making a decision."
            ),
            "grounding_facts": [{
                "advisory_event_id": event_id,
                "advisory_plan_id": event.get("advisory_plan_id"),
                "source_type": event.get("source_type"),
                "source_id": event.get("source_id"),
                "event_time": event.get("event_time"),
                "effective_at": event.get("effective_at"),
                "payload": payload,
            }],
            "escalation_level": 0,
            "allowed_cta": "review the plan or ask V for the reasoning",
        }

        try:
            composed = self._get_composer().compose(triggers=[trigger_spec])
        except Exception as exc:
            logger.error(
                "[proactive] Advisory event composition failed event=%s: %s",
                event_id, exc,
            )
            return {
                "success": False,
                "error": f"Message composition failed: {exc}",
                "event_id": event_id,
            }

        bubbles = composed.get("bubbles", [])
        if not isinstance(bubbles, list) or not bubbles:
            bubbles = [_fallback_advisory_event_bubble(event, payload)]
        bubbles = [str(b).strip() for b in bubbles[:3] if str(b).strip()]
        push_preview = str(
            composed.get("push_preview") or "V has an update about your plan"
        ).strip()

        outreach_log_id = db_store_proactive_outreach(
            client_id=client_id,
            trigger_class="advisory",
            trigger_type=event_type,
            trigger_reason=trigger_reason,
            guidance_mode="decision_support",
            escalation_level=0,
            objective=trigger_spec["objective"],
            bubbles=bubbles,
            grounding_fact_ids=[event_id],
            push_preview_text=push_preview,
            allowed_cta=trigger_spec["allowed_cta"],
        )
        queue_entry_id = db_enqueue_outbound_message(
            client_id=client_id,
            outreach_log_id=outreach_log_id or "",
            bubbles=bubbles,
            session_id=session_id,
            push_preview_text=push_preview,
        )

        persist_session_id = session_id
        if not persist_session_id:
            account_id = db_get_account_id_for_client_id(client_id)
            if account_id:
                persist_session_id = f"companion-{account_id}"

        companion_message_ids: List[str] = []
        if persist_session_id:
            for bubble in bubbles:
                message_id = db_store_companion_message(
                    session_id=persist_session_id,
                    client_id=client_id,
                    role="assistant",
                    content=bubble,
                    state={
                        "proactive": True,
                        "advisory_event_id": event_id,
                        "advisory_plan_id": event.get("advisory_plan_id"),
                        "ui_directive": "open_advisory_event",
                    },
                    metadata={
                        "message_kind": "advisory_event",
                        "advisory_event_id": event_id,
                        "advisory_plan_id": event.get("advisory_plan_id"),
                        "source_type": event.get("source_type"),
                        "outreach_log_id": outreach_log_id,
                        "queue_entry_id": queue_entry_id,
                    },
                )
                if message_id:
                    companion_message_ids.append(message_id)

        return {
            "success": True,
            "event_id": event_id,
            "client_id": client_id,
            "bubbles": bubbles,
            "push_preview": push_preview,
            "outreach_log_id": outreach_log_id,
            "queue_entry_id": queue_entry_id,
            "session_id": persist_session_id,
            "companion_message_ids": companion_message_ids,
            "model_used": composed.get("model_used"),
            "provider_used": composed.get("provider_used"),
        }

    def evaluate_all_clients(self) -> List[ProactiveResult]:
        """Batch evaluation for all active clients.

        Called by Cloud Scheduler / cron (every 24h).
        Returns list of results — one per client.
        """
        client_ids = self._get_active_client_ids()
        results: List[ProactiveResult] = []

        for client_id in client_ids:
            try:
                result = self.evaluate_and_compose(client_id)
                results.append(result)
            except Exception as exc:
                logger.error(
                    "[proactive] Unexpected error for client=%s: %s",
                    client_id, exc,
                )
                results.append(ProactiveResult(
                    client_id=client_id,
                    error=f"Unexpected error: {exc}",
                ))

        triggered_count = sum(1 for r in results if r.triggered)
        logger.info(
            "[proactive] Batch evaluation complete: %d clients, %d triggered",
            len(results), triggered_count,
        )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _update_escalation(
        client_id: str,
        trigger: TriggerResult,
    ) -> Optional[str]:
        """Update escalation tracking and mark backed-off if at max."""
        # Derive topic key from trigger
        topic_key = _derive_topic_key(trigger)

        tracking_id = db_upsert_escalation_tracking(
            client_id=client_id,
            topic_key=topic_key,
            trigger_class=trigger.trigger_class,
        )

        # Check if we've hit the max and should back off
        # escalation_level is 0-indexed count of prior mentions;
        # after this mention, total = escalation_level + 1
        if trigger.escalation_level + 1 >= MAX_ESCALATION_MENTIONS:
            db_mark_escalation_backed_off(client_id, topic_key)
            logger.info(
                "[proactive] Topic '%s' for client=%s has reached max mentions — backing off",
                topic_key, client_id,
            )

        return tracking_id


def _derive_topic_key(trigger: TriggerResult) -> str:
    """Derive a stable topic key from a trigger result.

    The topic key is used for escalation tracking — it must be consistent
    across evaluations for the same underlying issue.
    """
    if trigger.trigger_type == "incomplete_onboarding":
        return "incomplete_onboarding"
    if trigger.trigger_type == "stale_pending_confirmation":
        # Extract confirmation ID from objective
        if trigger.objective.startswith("confirm_pending:"):
            return f"stale_confirmation:{trigger.objective.split(':', 1)[1]}"
        return f"stale_confirmation:{trigger.objective}"
    if trigger.trigger_type == "journey_abandonment":
        if trigger.objective.startswith("resume_journey:"):
            return f"journey_abandonment:{trigger.objective.split(':', 1)[1]}"
        return f"journey_abandonment:{trigger.objective}"
    if trigger.trigger_type == "diagnosis_gap":
        if trigger.objective.startswith("surface_diagnosis:"):
            return f"diagnosis_gap:{trigger.objective.split(':', 1)[1]}"
        return f"diagnosis_gap:{trigger.objective}"
    if trigger.trigger_type == "reengagement":
        return "reengagement"
    return f"{trigger.trigger_type}:{trigger.objective}"


def _advisory_event_reason(event: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """Return a concise reason string for audit logs and composer grounding."""
    for key in ("reason", "summary", "title", "impact", "recommendation", "next_step"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    event_type = str(event.get("event_type") or "advisory_event").replace("_", " ")
    source_type = str(event.get("source_type") or "system")
    return f"{source_type} triggered {event_type}"


def _fallback_advisory_event_bubble(
    event: Dict[str, Any],
    payload: Dict[str, Any],
) -> str:
    """Deterministic fallback if the composer cannot return usable bubbles."""
    reason = _advisory_event_reason(event, payload)
    return (
        "I have an update about your plan: "
        f"{reason}. We should review what changed and decide whether your "
        "current recommendation still fits."
    )
