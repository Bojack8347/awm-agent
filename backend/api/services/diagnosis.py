"""DiagnosisService — business logic for financial diagnosis refresh.

Extracted from app.py so it can be imported and tested independently of
Flask. Contains no HTTP layer dependencies (no request, no jsonify).

The service is instantiated once by the API dependency layer (lazy singleton
via get_diagnosis_service()) and receives a diagnosis runner callable so there
is no import cycle.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from api.services.tracing import safe_trace_event
from advisor.agents.background_jobs import (
    complete_external_specialist_job,
    create_external_specialist_job,
    dispatch_background_callable,
)

logger = logging.getLogger("awm.services.diagnosis")

try:
    from api.persistence import (
        get_latest_knowledge_snapshot as db_get_latest_knowledge_snapshot,
        get_latest_diagnosis_snapshot as db_get_latest_diagnosis_snapshot,
        store_diagnosis_snapshot as db_store_diagnosis_snapshot,
        upsert_diagnosis_refresh_state as db_upsert_diagnosis_refresh_state,
        get_diagnosis_refresh_state as db_get_diagnosis_refresh_state,
    )
except ImportError:
    db_get_latest_knowledge_snapshot = lambda *a, **kw: None  # noqa: E731
    db_get_latest_diagnosis_snapshot = lambda *a, **kw: None  # noqa: E731
    db_store_diagnosis_snapshot = lambda *a, **kw: None  # noqa: E731
    db_upsert_diagnosis_refresh_state = lambda *a, **kw: False  # noqa: E731
    db_get_diagnosis_refresh_state = lambda *a, **kw: None  # noqa: E731


# ---------------------------------------------------------------------------
# Materiality filter — which fact domains/categories warrant re-diagnosis
# ---------------------------------------------------------------------------

# None as a value means every category in that domain is material.
DIAGNOSIS_MATERIAL_DOMAINS: Dict[str, Optional[set]] = {
    "wealth": None,
    "people": {"dependents", "employment", "household"},
    "healthcare": {"coverage", "insurance"},
}


class DiagnosisService:
    """Manages all diagnosis refresh logic for a single deployment.

    Instantiate once and share across the process (lazy singleton pattern).
    Pass run_diagnosis_fn so the service never imports from app.py directly.

    Usage in app.py::

        _DIAGNOSIS_SERVICE: Optional[DiagnosisService] = None

        def get_diagnosis_service() -> DiagnosisService:
            global _DIAGNOSIS_SERVICE
            if _DIAGNOSIS_SERVICE is None:
                _DIAGNOSIS_SERVICE = DiagnosisService(run_diagnosis_fn)
            return _DIAGNOSIS_SERVICE
    """

    def __init__(self, run_diagnosis_fn: Callable[..., Dict[str, Any]]) -> None:
        self._run_diagnosis = run_diagnosis_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_material(self, facts: List[Dict[str, Any]]) -> bool:
        """Return True if any of these facts would change the diagnosis."""
        for fact in facts:
            domain = fact.get("domain", "")
            category = fact.get("category", "")
            if domain in DIAGNOSIS_MATERIAL_DOMAINS:
                allowed = DIAGNOSIS_MATERIAL_DOMAINS[domain]
                if allowed is None or category in allowed:
                    return True
        return False

    def get_refresh_state(self, client_id: str) -> Dict[str, Any]:
        """Load persisted diagnosis refresh state, merged with defaults."""
        record = db_get_diagnosis_refresh_state(client_id) or {}
        state = record.get("state") or {}
        payload = self._default_state()
        payload.update(state)
        return payload

    def queue_refresh_if_material(
        self,
        client_id: str,
        committed_facts: List[Dict[str, Any]],
        *,
        caller: str = "unknown",
    ) -> Dict[str, Any]:
        """Queue async diagnosis refresh if any committed facts are material.

        Returns a status dict. The caller is responsible for any task-level
        bookkeeping (e.g. updating _CONSULTATION_TASKS in app.py).
        """
        if not committed_facts or not self.is_material(committed_facts):
            state = self._store_state(client_id, {
                **self.get_refresh_state(client_id),
                "status": "not_required",
                "last_caller": caller,
                "last_error": None,
            })
            return self._refresh_result(queued=False, state=state)

        snapshot = db_get_latest_knowledge_snapshot(client_id)
        if not snapshot:
            state = self._store_state(client_id, {
                **self.get_refresh_state(client_id),
                "status": "failed",
                "last_caller": caller,
                "last_error": "No knowledge snapshot available for queued diagnosis refresh",
                "completed_at": _now(),
            })
            return self._refresh_result(queued=False, state=state)

        requested_version = int(snapshot.get("version", 0) or 0)
        current = self.get_refresh_state(client_id)
        next_requested = max(
            requested_version,
            int(current.get("requested_knowledge_snapshot_version") or 0),
        )
        state = self._store_state(client_id, {
            **current,
            "status": "pending_refresh",
            "requested_knowledge_snapshot_version": next_requested,
            "superseded_by_snapshot_version": None,
            "last_caller": caller,
            "last_error": None,
            "queued_at": _now(),
        })
        safe_trace_event(
            trace_id=f"tr_diagnosis_{client_id}_{next_requested}",
            client_id=client_id,
            source_type="diagnosis_service",
            source_id=caller,
            event_type="diagnosis.refresh.queued",
            event_name="Diagnosis refresh queued",
            actor_type="system",
            status="queued",
            output_summary=f"Queued diagnosis refresh for knowledge snapshot v{next_requested}.",
            payload={
                "caller": caller,
                "requested_knowledge_snapshot_version": next_requested,
                "committed_fact_count": len(committed_facts),
            },
        )
        self._schedule_refresh(client_id)
        return self._refresh_result(queued=True, state=state)

    def refresh_if_material(
        self,
        client_id: str,
        committed_facts: List[Dict[str, Any]],
        caller: str = "unknown",
    ) -> Optional[int]:
        """Synchronously refresh diagnosis if facts are material.

        Returns the new diagnosis version, or None if not needed / failed.
        Used by synchronous pipeline paths (consultation_complete).
        """
        if not committed_facts or not self.is_material(committed_facts):
            return None

        try:
            snap = db_get_latest_knowledge_snapshot(client_id)
            if not snap:
                return None

            diag_result = self._run_for_snapshot(
                client_id=client_id,
                snapshot_data=snap.get("snapshot_data", {}),
                snapshot_version=snap.get("version", 0),
                caller=caller,
            )
            diag_ver = (db_get_latest_diagnosis_snapshot(client_id) or {}).get("version", 0) + 1
            model_metadata = dict(diag_result.get("model_metadata", {}) or {})
            model_metadata["refresh_caller"] = caller
            db_store_diagnosis_snapshot(
                client_id=client_id,
                version=diag_ver,
                knowledge_snapshot_version=snap.get("version", 0),
                diagnosis_data=diag_result.get("diagnosis_data", {}),
                model_metadata=model_metadata,
            )
            safe_trace_event(
                trace_id=f"tr_diagnosis_{client_id}_{snap.get('version', 0)}",
                client_id=client_id,
                source_type="diagnosis_service",
                source_id=caller,
                event_type="diagnosis.refresh.completed",
                event_name="Diagnosis refresh completed",
                actor_type="agent",
                agent_name="DiagnosisAgent",
                status="success",
                output_summary=f"Stored diagnosis snapshot v{diag_ver}.",
                payload={
                    "caller": caller,
                    "knowledge_snapshot_version": snap.get("version", 0),
                    "diagnosis_snapshot_version": diag_ver,
                },
                subjects=[
                    {"subject_type": "knowledge_snapshot", "subject_id": str(snap.get("version", 0)), "relation": "input"},
                    {"subject_type": "diagnosis_snapshot", "subject_id": str(diag_ver), "relation": "output"},
                ],
            )
            self._store_state(client_id, {
                **self.get_refresh_state(client_id),
                "status": "complete",
                "requested_knowledge_snapshot_version": snap.get("version", 0),
                "latest_completed_knowledge_snapshot_version": snap.get("version", 0),
                "latest_diagnosis_version": diag_ver,
                "running_knowledge_snapshot_version": None,
                "superseded_by_snapshot_version": None,
                "last_error": None,
                "completed_at": _now(),
                "last_caller": caller,
            })
            return diag_ver
        except Exception as exc:
            self._store_state(client_id, {
                **self.get_refresh_state(client_id),
                "status": "failed",
                "running_knowledge_snapshot_version": None,
                "last_error": str(exc),
                "completed_at": _now(),
                "last_caller": caller,
            })
            safe_trace_event(
                trace_id=f"tr_diagnosis_{client_id}_sync_failed",
                client_id=client_id,
                source_type="diagnosis_service",
                source_id=caller,
                event_type="diagnosis.refresh.failed",
                event_name="Diagnosis refresh failed",
                actor_type="agent",
                agent_name="DiagnosisAgent",
                status="failed",
                error_message=str(exc)[:500],
                payload={"caller": caller},
            )
            print(f"[{caller}] Diagnosis refresh failed (non-fatal): {exc}", flush=True)
            return None

    def queue_confirmation_refresh(
        self,
        client_id: str,
        committed_fact: Dict[str, Any],
    ) -> bool:
        """Queue async refresh after a confirmation is resolved."""
        if not committed_fact:
            return False
        result = self.queue_refresh_if_material(
            client_id=client_id,
            committed_facts=[committed_fact],
            caller="resolve_confirmation_background",
        )
        return bool(result.get("queued"))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _default_state(self) -> Dict[str, Any]:
        return {
            "status": "idle",
            "requested_knowledge_snapshot_version": 0,
            "running_knowledge_snapshot_version": None,
            "latest_completed_knowledge_snapshot_version": 0,
            "latest_diagnosis_version": 0,
            "superseded_by_snapshot_version": None,
            "last_error": None,
            "last_caller": None,
            "queued_at": None,
            "started_at": None,
            "completed_at": None,
        }

    def _store_state(self, client_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._default_state()
        payload.update(state or {})
        db_upsert_diagnosis_refresh_state(client_id, payload)
        return payload

    def _refresh_result(self, *, queued: bool, state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "queued": queued,
            "status": state["status"],
            "requested_knowledge_snapshot_version": state.get("requested_knowledge_snapshot_version"),
            "diagnosis_snapshot_version": state.get("latest_diagnosis_version"),
        }

    def _run_for_snapshot(
        self,
        client_id: str,
        snapshot_data: Dict[str, Any],
        snapshot_version: int,
        *,
        caller: str,
    ) -> Dict[str, Any]:
        return self._run_diagnosis(
            client_id=client_id,
            knowledge_snapshot=snapshot_data,
            knowledge_snapshot_version=snapshot_version,
        )

    def run_queued_refresh(self, client_id: str) -> Dict[str, Any]:
        """Execute one diagnosis refresh cycle.

        Called by the Cloud Tasks HTTP handler (or directly in tests). Each
        invocation handles exactly one snapshot version. If a newer snapshot
        arrived while running (superseded), a follow-up task is scheduled.

        Returns a dict with {"status": "complete"|"superseded"|"failed"|"already_complete"}.
        """
        state = self.get_refresh_state(client_id)
        requested = int(state.get("requested_knowledge_snapshot_version") or 0)
        completed = int(state.get("latest_completed_knowledge_snapshot_version") or 0)

        if requested <= completed:
            self._store_state(client_id, {
                **state,
                "status": "complete" if completed else "idle",
                "running_knowledge_snapshot_version": None,
                "superseded_by_snapshot_version": None,
                "last_error": None,
            })
            return {"status": "already_complete"}

        snapshot = db_get_latest_knowledge_snapshot(client_id)
        if not snapshot:
            self._store_state(client_id, {
                **state,
                "status": "failed",
                "running_knowledge_snapshot_version": None,
                "last_error": "No knowledge snapshot available for diagnosis refresh",
                "completed_at": _now(),
            })
            return {"status": "failed", "error": "no_snapshot"}

        latest_snap_ver = int(snapshot.get("version", 0) or 0)
        if latest_snap_ver > requested:
            # Snapshot advanced since we were enqueued — bump target, re-schedule.
            state = self._store_state(client_id, {
                **state,
                "status": "superseded",
                "requested_knowledge_snapshot_version": latest_snap_ver,
                "superseded_by_snapshot_version": latest_snap_ver,
                "running_knowledge_snapshot_version": None,
                "queued_at": _now(),
            })
            self._schedule_refresh(client_id)
            return {"status": "superseded", "re_queued": True}

        state = self._store_state(client_id, {
            **state,
            "status": "running",
            "running_knowledge_snapshot_version": requested,
            "started_at": _now(),
            "last_error": None,
        })
        safe_trace_event(
            trace_id=f"tr_diagnosis_{client_id}_{requested}",
            client_id=client_id,
            source_type="diagnosis_service",
            source_id=str(state.get("last_caller") or "diagnosis_task"),
            event_type="diagnosis.refresh.started",
            event_name="Queued diagnosis refresh started",
            actor_type="system",
            status="running",
            payload={"knowledge_snapshot_version": requested},
        )

        try:
            diag_result = self._run_for_snapshot(
                client_id=client_id,
                snapshot_data=snapshot.get("snapshot_data", {}),
                snapshot_version=requested,
                caller=str(state.get("last_caller") or "diagnosis_task"),
            )
            # Check whether a newer snapshot arrived while we were running.
            latest_after = db_get_latest_knowledge_snapshot(client_id) or {}
            latest_after_ver = int(latest_after.get("version", requested) or requested)
            current = self.get_refresh_state(client_id)
            desired = max(
                latest_after_ver,
                int(current.get("requested_knowledge_snapshot_version") or 0),
            )
            superseded_by = desired if desired > requested else None

            diag_ver = (db_get_latest_diagnosis_snapshot(client_id) or {}).get("version", 0) + 1
            model_metadata = dict(diag_result.get("model_metadata", {}) or {})
            model_metadata["refresh_caller"] = str(state.get("last_caller") or "diagnosis_task")
            if superseded_by is not None:
                model_metadata["superseded"] = True
                model_metadata["superseded_by_snapshot_version"] = superseded_by

            db_store_diagnosis_snapshot(
                client_id=client_id,
                version=diag_ver,
                knowledge_snapshot_version=requested,
                diagnosis_data=diag_result.get("diagnosis_data", {}),
                model_metadata=model_metadata,
            )
            safe_trace_event(
                trace_id=f"tr_diagnosis_{client_id}_{requested}",
                client_id=client_id,
                source_type="diagnosis_service",
                source_id=str(state.get("last_caller") or "diagnosis_task"),
                event_type="diagnosis.refresh.completed",
                event_name="Queued diagnosis refresh completed",
                actor_type="agent",
                agent_name="DiagnosisAgent",
                status="success",
                output_summary=f"Stored diagnosis snapshot v{diag_ver}.",
                payload={
                    "knowledge_snapshot_version": requested,
                    "diagnosis_snapshot_version": diag_ver,
                    "superseded_by_snapshot_version": superseded_by,
                },
                subjects=[
                    {"subject_type": "knowledge_snapshot", "subject_id": str(requested), "relation": "input"},
                    {"subject_type": "diagnosis_snapshot", "subject_id": str(diag_ver), "relation": "output"},
                ],
            )
            self._store_state(client_id, {
                **current,
                "status": "superseded" if superseded_by is not None else "complete",
                "requested_knowledge_snapshot_version": desired,
                "running_knowledge_snapshot_version": None,
                "latest_completed_knowledge_snapshot_version": requested,
                "latest_diagnosis_version": diag_ver,
                "superseded_by_snapshot_version": superseded_by,
                "completed_at": _now(),
                "last_error": None,
            })

            if superseded_by is not None:
                self._schedule_refresh(client_id)
                return {"status": "superseded", "re_queued": True, "diagnosis_version": diag_ver}

            return {"status": "complete", "diagnosis_version": diag_ver}

        except Exception as exc:
            self._store_state(client_id, {
                **self.get_refresh_state(client_id),
                "status": "failed",
                "running_knowledge_snapshot_version": None,
                "completed_at": _now(),
                "last_error": str(exc),
            })
            safe_trace_event(
                trace_id=f"tr_diagnosis_{client_id}_{requested}",
                client_id=client_id,
                source_type="diagnosis_service",
                source_id=str(state.get("last_caller") or "diagnosis_task"),
                event_type="diagnosis.refresh.failed",
                event_name="Queued diagnosis refresh failed",
                actor_type="agent",
                agent_name="DiagnosisAgent",
                status="failed",
                error_message=str(exc)[:500],
                payload={"knowledge_snapshot_version": requested},
            )
            logger.error("[diagnosis_task] Diagnosis refresh failed: %s", exc)
            return {"status": "failed", "error": str(exc)}

    def _schedule_refresh(self, client_id: str) -> None:
        """Schedule a diagnosis refresh.

        Uses Cloud Tasks when configured; otherwise uses the durable shared
        specialist-job executor.
        """
        if os.getenv("CLOUD_TASKS_ENABLED", "").lower() == "true":
            try:
                self._enqueue_cloud_task(client_id)
                return
            except Exception as exc:
                logger.warning(
                    "Cloud Tasks enqueue failed for client=%s, falling back to durable job: %s",
                    client_id, exc,
                )
        self._ensure_worker(client_id)

    def _enqueue_cloud_task(self, client_id: str) -> None:
        """Create a Cloud Tasks HTTP task targeting /internal/tasks/diagnosis-refresh."""
        from google.cloud import tasks_v2  # lazy import — not installed in dev

        project = os.getenv("GCP_PROJECT_ID", "")
        location = os.getenv("GCP_REGION", "asia-southeast1")
        queue = os.getenv("CLOUD_TASKS_DIAGNOSIS_QUEUE", "awm-diagnosis-refresh")
        service_url = os.getenv("CLOUD_RUN_SERVICE_URL", "").rstrip("/")
        internal_secret = os.getenv("CLOUD_TASKS_INTERNAL_SECRET", "")

        if not project or not service_url:
            raise RuntimeError(
                "CLOUD_TASKS_ENABLED=true but GCP_PROJECT_ID or CLOUD_RUN_SERVICE_URL not set"
            )

        state = self.get_refresh_state(client_id)
        requested = int(state.get("requested_knowledge_snapshot_version") or 0)
        receipt = create_external_specialist_job(
            specialist_key="diagnosis",
            client_id=client_id,
            objective=f"Refresh diagnosis for knowledge snapshot {requested}.",
            event_source="diagnosis_cloud_task",
            client_file_version_at_start=requested or None,
        )
        if receipt.get("duplicate"):
            return
        job_id = str(receipt["job_id"])
        tasks_client = tasks_v2.CloudTasksClient()
        parent = tasks_client.queue_path(project, location, queue)
        payload = json.dumps(
            {"client_id": client_id, "specialist_job_id": job_id}
        ).encode()

        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{service_url}/internal/tasks/diagnosis-refresh",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Internal-Secret": internal_secret,
                },
                "body": payload,
            }
        }
        try:
            tasks_client.create_task(request={"parent": parent, "task": task})
        except Exception as exc:
            complete_external_specialist_job(job_id, error=str(exc))
            raise
        logger.info("Cloud Task enqueued for diagnosis refresh: client=%s", client_id)

    def _ensure_worker(self, client_id: str) -> bool:
        """Dispatch one deduplicated durable diagnosis specialist job."""

        state = self.get_refresh_state(client_id)
        requested = int(state.get("requested_knowledge_snapshot_version") or 0)
        receipt = dispatch_background_callable(
            specialist_key="diagnosis",
            client_id=client_id,
            objective=f"Refresh diagnosis for knowledge snapshot {requested}.",
            callback=lambda: self._drain_durable_queue(client_id),
            event_source="diagnosis_service",
            client_file_version_at_start=requested or None,
        )
        return not bool(receipt.get("duplicate"))

    def _drain_durable_queue(self, client_id: str) -> Dict[str, Any]:
        """Run queued refreshes until the latest requested snapshot is covered."""

        result = self.run_queued_refresh(client_id)
        while result.get("status") == "superseded":
            result = self.run_queued_refresh(client_id)
        return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
