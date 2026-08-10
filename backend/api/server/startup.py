"""Startup hooks for the API server process."""

from __future__ import annotations

import os
import threading
from threading import Event

from .deps import get_advisor_runtime, get_client_profile_extractor
from .persistence import db_available, db_reconcile_orphaned_journeys

_LOCAL_EVENT_DRAIN_STOP = Event()
_LOCAL_EVENT_DRAIN_THREAD: threading.Thread | None = None


def _eager_init_agents() -> None:
    """Pre-initialize latency-sensitive agents at startup to avoid cold-start penalties."""
    eager = os.getenv("ADVISOR_EAGER_INIT", "true").strip().lower()
    if eager in ("0", "false", "no", "off"):
        return
    try:
        print("[startup] Eagerly initializing advisor runtime...", flush=True)
        get_advisor_runtime()
        print("[startup] Eagerly initializing client profile extractor...", flush=True)
        get_client_profile_extractor()
        print("[startup] Eager init complete.", flush=True)
    except Exception as exc:
        print(f"[startup] Eager init failed (non-fatal): {exc}", flush=True)


def _reconcile_orphaned_journeys() -> None:
    """Mark any journey stuck in 'running' as failed on startup."""
    try:
        if not db_available():
            return
        count = db_reconcile_orphaned_journeys(min_age_minutes=5)
        if count:
            print(f"[startup] Reconciled {count} orphaned journey(s).", flush=True)
    except Exception as exc:
        print(f"[startup] Journey reconciliation failed (non-fatal): {exc}", flush=True)


def _warn_if_internal_task_auth_unset() -> None:
    if not os.getenv("CLOUD_TASKS_INTERNAL_SECRET", "").strip():
        print(
            "[startup] WARNING: CLOUD_TASKS_INTERNAL_SECRET is not set. "
            "/internal/tasks/diagnosis-refresh is open to unauthenticated callers. "
            "Set this secret before deploying to production.",
            flush=True,
        )


def run_startup_hooks() -> None:
    from .demo_auth import validate_demo_auth_environment
    validate_demo_auth_environment()
    _eager_init_agents()
    _reconcile_orphaned_journeys()
    _warn_if_internal_task_auth_unset()
    _recover_abandoned_specialist_jobs()
    _start_local_business_event_drain()
    _maybe_run_v2_regular_consult_scheduler()


def _recover_abandoned_specialist_jobs() -> None:
    try:
        from advisor.agents.background_jobs import recover_abandoned_specialist_jobs

        result = recover_abandoned_specialist_jobs()
        if result["recovered"] or result["failed"]:
            print(f"[startup] Specialist job recovery: {result}", flush=True)
    except Exception as exc:
        print(f"[startup] Specialist job recovery failed (non-fatal): {exc}", flush=True)


def _start_local_business_event_drain() -> None:
    """Start the local outbox loop; production uses Cloud Scheduler on the task endpoint."""

    enabled = os.getenv("AWM_LOCAL_EVENT_DRAIN_ENABLED", "").strip().lower()
    if not enabled:
        enabled = (
            os.getenv("FLASK_ENV", "").strip().lower() == "development"
            or os.getenv("AWM_ENV", "").strip().lower() == "local"
            or os.getenv("LOCAL_DEV", "").strip().lower() in {"1", "true", "yes", "on"}
        )
    else:
        enabled = enabled in {"1", "true", "yes", "on"}
    if not enabled:
        return
    global _LOCAL_EVENT_DRAIN_THREAD
    if _LOCAL_EVENT_DRAIN_THREAD is not None and _LOCAL_EVENT_DRAIN_THREAD.is_alive():
        return
    interval = max(0.25, float(os.getenv("AWM_LOCAL_EVENT_DRAIN_INTERVAL_SECONDS", "2")))

    def drain_loop() -> None:
        from .deps import get_business_event_worker, get_planning_refresh_coordinator

        while not _LOCAL_EVENT_DRAIN_STOP.wait(interval):
            try:
                get_business_event_worker().drain(limit=20)
                # Durable refresh state is an independent source of unfinished
                # work, so sweep even when the outbox has no pending event.
                get_planning_refresh_coordinator().sweep(limit=20)
            except Exception as exc:
                print(f"[startup] Local business-event drain failed: {exc}", flush=True)

    _LOCAL_EVENT_DRAIN_THREAD = threading.Thread(
        target=drain_loop,
        name="awm-business-event-drain",
        daemon=True,
    )
    _LOCAL_EVENT_DRAIN_THREAD.start()


def _maybe_run_v2_regular_consult_scheduler() -> None:
    try:
        from advisor.proactive.objectives import RegularConsultScheduler
        from advisor.proactive.objectives import regular_consult_schedule_from_env

        schedule = regular_consult_schedule_from_env(env_getter=os.getenv)
        delivery = RegularConsultScheduler(runtime_factory=get_advisor_runtime).run(schedule)
        if delivery["status"] == "delivered":
            print(
                f"[startup] advisor regular-consult scheduler delivered {delivery['delivered_count']} session(s).",
                flush=True,
            )
    except Exception as exc:
        print(f"[startup] advisor regular-consult scheduler failed (non-fatal): {exc}", flush=True)
