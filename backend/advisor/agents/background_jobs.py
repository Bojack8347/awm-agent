"""Durable background specialist execution backed by business events."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import uuid
from concurrent.futures import CancelledError, Executor, Future
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from typing import Any, Dict, List, Optional

from agents import gen_trace_id

from advisor.agents.context import AwmAgentContext
from advisor.tools.subagent_tools.common.interfaces import SubAgentRequest
from advisor.tracing.tracing import new_span_id, persistent_safe_tool_results


SPECIALIST_JOB_EVENT_TYPE = "agent.specialist_run"
SPECIALIST_JOB_AGGREGATE_TYPE = "agent_specialist_job"
ACTIVE_JOB_STATUSES = {"running"}
TERMINAL_JOB_STATUSES = {"done", "failed", "cancelled"}

logger = logging.getLogger(__name__)

_EXECUTOR: Optional[Executor] = None
_FUTURES: Dict[str, Future[Any]] = {}
_CANCELLATION_EVENTS: Dict[str, Event] = {}
_FUTURES_LOCK = Lock()


class SpecialistJobCancelled(RuntimeError):
    """Raised before a specialist tool call after cancellation was requested."""


def set_specialist_job_executor(executor: Executor) -> None:
    """Inject the API-owned background executor.

    Reconfiguration is allowed only while no jobs are registered, which keeps
    test isolation possible without moving executor ownership back into this
    advisor-layer module.
    """

    if executor is None:
        raise ValueError("specialist job executor must not be None")
    global _EXECUTOR
    with _FUTURES_LOCK:
        if _FUTURES:
            raise RuntimeError(
                "Cannot replace the specialist job executor while jobs are running"
            )
        _EXECUTOR = executor


def _specialist_job_executor() -> Executor:
    if _EXECUTOR is None:
        raise RuntimeError(
            "Specialist job executor is not configured; "
            "the API layer must call set_specialist_job_executor at startup"
        )
    return _EXECUTOR


def _persistence():
    from api import persistence

    return persistence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_expires_at(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(1.0, seconds))
    ).isoformat()


def _fingerprint(client_file: Dict[str, Any]) -> str:
    encoded = json.dumps(
        client_file or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _client_file_version(client_file: Dict[str, Any]) -> Optional[int]:
    for key in ("client_file_version", "version", "snapshot_version", "knowledge_snapshot_version"):
        value = client_file.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _job_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def list_specialist_jobs(
    *,
    client_id: str,
    specialist_key: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    events = _persistence().list_business_events(
        client_id=client_id,
        event_type=SPECIALIST_JOB_EVENT_TYPE,
        limit=limit,
    )
    if specialist_key is None:
        return events
    return [
        event
        for event in events
        if _job_payload(event).get("specialist_key") == specialist_key
    ]


def jobs_for_prompt(
    *,
    client_id: str,
    current_client_file: Dict[str, Any],
) -> List[Dict[str, Any]]:
    _expire_stale_jobs(client_id=client_id)
    current_fingerprint = _fingerprint(current_client_file)
    visible: List[Dict[str, Any]] = []
    for event in list_specialist_jobs(client_id=client_id, limit=12):
        status = str(event.get("status") or "")
        if status not in ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES:
            continue
        payload = _job_payload(event)
        visible.append(
            {
                "job_id": str(event.get("id") or ""),
                "specialist_key": payload.get("specialist_key"),
                "objective": payload.get("objective"),
                "status": status,
                "started_at": payload.get("started_at"),
                "completed_at": payload.get("completed_at"),
                "result": payload.get("result") if status == "done" else None,
                "error": payload.get("error") if status == "failed" else None,
                "cancel_reason": (
                    payload.get("cancel_reason") if status == "cancelled" else None
                ),
                "artifact_ids": payload.get("artifact_ids") or [],
                "client_file_stale": (
                    status == "done"
                    and bool(payload.get("client_file_fingerprint_at_start"))
                    and payload.get("client_file_fingerprint_at_start")
                    != current_fingerprint
                ),
                "client_file_version_at_start": payload.get(
                    "client_file_version_at_start"
                ),
            }
        )
    return visible


def specialist_job_cancellation_requested(
    job_id: str,
    *,
    cancellation_event: Optional[Event] = None,
) -> bool:
    """Return whether local or durable state requests cooperative cancellation."""

    if cancellation_event is not None and cancellation_event.is_set():
        return True
    event = _persistence().get_business_event(job_id)
    return bool(event and str(event.get("status") or "") == "cancelled")


def cancel_specialist_job(
    job_id: str,
    *,
    reason: str,
    client_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    event = _persistence().get_business_event(job_id)
    if event and client_id is not None and event.get("client_id") != client_id:
        return None
    if not event or str(event.get("status") or "") not in ACTIVE_JOB_STATUSES:
        return event
    updated = _persistence().update_business_event(
        job_id,
        status="cancelled",
        payload_patch={
            "completed_at": _now(),
            "cancel_reason": reason,
        },
    )
    with _FUTURES_LOCK:
        future = _FUTURES.get(job_id)
        cancellation_event = _CANCELLATION_EVENTS.get(job_id)
    if cancellation_event is not None:
        cancellation_event.set()
    if future is not None:
        future.cancel()
    return updated


def dispatch_specialist_job(
    *,
    specialist_key: str,
    objective: str,
    source_context: AwmAgentContext,
    agent: Any,
    max_turns: int,
    run_config: Any = None,
    supersede: bool = False,
) -> Dict[str, Any]:
    normalized_objective = " ".join(str(objective or "").split())
    if not normalized_objective:
        raise ValueError("Background specialist objective must not be empty")

    _expire_stale_jobs(client_id=source_context.client_id)
    active = [
        event
        for event in list_specialist_jobs(
            client_id=source_context.client_id,
            specialist_key=specialist_key,
        )
        if str(event.get("status") or "") in ACTIVE_JOB_STATUSES
    ]
    if active:
        if supersede:
            _persistence().update_business_event(
                str(active[0].get("id") or ""),
                payload_patch={"rerun_required": True},
            )
        return _receipt(active[0], duplicate=True)

    request = SubAgentRequest(
        client_id=source_context.client_id,
        objective={
            "request": normalized_objective,
            "session_id": source_context.session_id,
            "turn_id": source_context.turn_id,
        },
        client_file=copy.deepcopy(source_context.client_file),
    )
    event = _persistence().create_business_event(
        event_type=SPECIALIST_JOB_EVENT_TYPE,
        client_id=source_context.client_id,
        aggregate_type=SPECIALIST_JOB_AGGREGATE_TYPE,
        aggregate_id=f"{source_context.client_id}:{specialist_key}",
        event_source="awm_main_advisor",
        event_key=(
            f"specialist:{source_context.client_id}:{specialist_key}:"
            f"{source_context.turn_id}"
        ),
        status="running",
        payload={
            "specialist_key": specialist_key,
            "client_id": source_context.client_id,
            "session_id": source_context.session_id,
            "turn_id": source_context.turn_id,
            "channel": source_context.channel,
            "objective": normalized_objective,
            "request": request.model_dump(),
            "started_at": _now(),
            "lease_expires_at": _lease_expires_at(
                float(getattr(agent, "_arc_timeout_seconds", 180.0)) + 60.0
            ),
            "attempts": 1,
            "completed_at": None,
            "result": None,
            "error": None,
            "artifact_ids": [],
            "client_file_version_at_start": _client_file_version(
                source_context.client_file
            ),
            "client_file_fingerprint_at_start": _fingerprint(
                source_context.client_file
            ),
        },
    )
    if event is None:
        raise RuntimeError("Unable to create durable specialist job")
    job_id = str(event["id"])
    if specialist_key in {"investment_solution", "financial_planning"} and _cloud_tasks_enabled():
        try:
            if specialist_key == "investment_solution":
                _enqueue_investment_solution_cloud_task(job_id)
            else:
                _enqueue_specialist_cloud_task(job_id, specialist_key=specialist_key)
        except Exception as exc:
            logger.warning(
                "Cloud Tasks enqueue failed for specialist job %s; "
                "falling back to the shared in-process executor: %s",
                job_id,
                exc,
            )
            _submit_specialist_job(
                job_id=job_id,
                request=request,
                source_context=source_context,
                agent=agent,
                max_turns=max_turns,
                run_config=run_config,
            )
    else:
        _submit_specialist_job(
            job_id=job_id,
            request=request,
            source_context=source_context,
            agent=agent,
            max_turns=max_turns,
            run_config=run_config,
        )
    return _receipt(event, duplicate=False)


def _cloud_tasks_enabled() -> bool:
    return os.getenv("CLOUD_TASKS_ENABLED", "").strip().lower() == "true"


def _enqueue_specialist_cloud_task(job_id: str, *, specialist_key: str) -> None:
    """Enqueue a durable specialist worker request."""

    from google.cloud import tasks_v2  # lazy import — not installed in dev

    project = os.getenv("GCP_PROJECT_ID", "").strip()
    location = os.getenv("GCP_REGION", "asia-southeast1").strip()
    queue = (
        os.getenv(
            "CLOUD_TASKS_FINANCIAL_PLANNING_QUEUE"
            if specialist_key == "financial_planning"
            else "CLOUD_TASKS_INVESTMENT_SOLUTION_QUEUE"
        )
        or os.getenv("CLOUD_TASKS_DIAGNOSIS_QUEUE")
        or "awm-diagnosis-refresh"
    ).strip()
    service_url = os.getenv("CLOUD_RUN_SERVICE_URL", "").rstrip("/")
    internal_secret = os.getenv("CLOUD_TASKS_INTERNAL_SECRET", "")
    if not project or not service_url:
        raise RuntimeError(
            "CLOUD_TASKS_ENABLED=true but GCP_PROJECT_ID or "
            "CLOUD_RUN_SERVICE_URL is not set"
        )

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project, location, queue)
    payload = json.dumps({"specialist_job_id": job_id}).encode("utf-8")
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{service_url}/internal/tasks/{specialist_key.replace('_', '-')}",
            "headers": {
                "Content-Type": "application/json",
                "X-Internal-Secret": internal_secret,
            },
            "body": payload,
        }
    }
    client.create_task(request={"parent": parent, "task": task})
    logger.info("Cloud Task enqueued for %s job %s", specialist_key, job_id)


def _enqueue_investment_solution_cloud_task(job_id: str) -> None:
    """Backward-compatible wrapper used by existing tests."""

    _enqueue_specialist_cloud_task(job_id, specialist_key="investment_solution")


def _submit_specialist_job(
    *,
    job_id: str,
    request: SubAgentRequest,
    source_context: AwmAgentContext,
    agent: Any,
    max_turns: int,
    run_config: Any,
) -> None:
    cancellation_event = Event()
    future = _specialist_job_executor().submit(
        _run_specialist_job,
        job_id=job_id,
        request=request,
        source_context=source_context,
        agent=agent,
        max_turns=max_turns,
        run_config=run_config,
        cancellation_event=cancellation_event,
    )
    with _FUTURES_LOCK:
        _FUTURES[job_id] = future
        _CANCELLATION_EVENTS[job_id] = cancellation_event
    future.add_done_callback(lambda _future: _forget_future(job_id))


def dispatch_background_callable(
    *,
    specialist_key: str,
    client_id: str,
    objective: str,
    callback: Any,
    event_source: str,
    client_file_version_at_start: Optional[int] = None,
) -> Dict[str, Any]:
    _expire_stale_jobs(client_id=client_id)
    active = [
        event
        for event in list_specialist_jobs(
            client_id=client_id,
            specialist_key=specialist_key,
        )
        if str(event.get("status") or "") in ACTIVE_JOB_STATUSES
    ]
    if active:
        return _receipt(active[0], duplicate=True)
    event = _persistence().create_business_event(
        event_type=SPECIALIST_JOB_EVENT_TYPE,
        client_id=client_id,
        aggregate_type=SPECIALIST_JOB_AGGREGATE_TYPE,
        aggregate_id=f"{client_id}:{specialist_key}",
        event_source=event_source,
        event_key=f"specialist:{client_id}:{specialist_key}:{uuid.uuid4().hex}",
        status="running",
        payload={
            "specialist_key": specialist_key,
            "client_id": client_id,
            "session_id": None,
            "turn_id": None,
            "objective": objective,
            "started_at": _now(),
            "lease_expires_at": _lease_expires_at(900.0),
            "attempts": 1,
            "completed_at": None,
            "result": None,
            "error": None,
            "artifact_ids": [],
            "client_file_version_at_start": client_file_version_at_start,
            "client_file_fingerprint_at_start": None,
        },
    )
    if event is None:
        raise RuntimeError("Unable to create durable specialist job")
    job_id = str(event["id"])
    cancellation_event = Event()
    future = _specialist_job_executor().submit(
        _run_callable_job,
        job_id=job_id,
        callback=callback,
    )
    with _FUTURES_LOCK:
        _FUTURES[job_id] = future
        _CANCELLATION_EVENTS[job_id] = cancellation_event
    future.add_done_callback(lambda _future: _forget_future(job_id))
    return _receipt(event, duplicate=False)


def create_external_specialist_job(
    *,
    specialist_key: str,
    client_id: str,
    objective: str,
    event_source: str,
    client_file_version_at_start: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a durable job that an external worker, such as Cloud Tasks, owns."""

    _expire_stale_jobs(client_id=client_id)
    active = [
        event
        for event in list_specialist_jobs(
            client_id=client_id,
            specialist_key=specialist_key,
        )
        if str(event.get("status") or "") in ACTIVE_JOB_STATUSES
    ]
    if active:
        return _receipt(active[0], duplicate=True)
    event = _persistence().create_business_event(
        event_type=SPECIALIST_JOB_EVENT_TYPE,
        client_id=client_id,
        aggregate_type=SPECIALIST_JOB_AGGREGATE_TYPE,
        aggregate_id=f"{client_id}:{specialist_key}",
        event_source=event_source,
        event_key=f"specialist:{client_id}:{specialist_key}:{uuid.uuid4().hex}",
        status="running",
        payload={
            "specialist_key": specialist_key,
            "client_id": client_id,
            "session_id": None,
            "turn_id": None,
            "objective": objective,
            "started_at": _now(),
            "lease_expires_at": _lease_expires_at(900.0),
            "attempts": 1,
            "completed_at": None,
            "result": None,
            "error": None,
            "artifact_ids": [],
            "client_file_version_at_start": client_file_version_at_start,
            "client_file_fingerprint_at_start": None,
        },
    )
    if event is None:
        raise RuntimeError("Unable to create durable external specialist job")
    return _receipt(event, duplicate=False)


def complete_external_specialist_job(
    job_id: str,
    *,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = _persistence().get_business_event(job_id)
    if not current or str(current.get("status") or "") not in ACTIVE_JOB_STATUSES:
        return current
    status = "failed" if error else "done"
    return _persistence().update_business_event(
        job_id,
        status=status,
        payload_patch={
            "completed_at": _now(),
            "result": result if error is None else None,
            "error": error,
        },
        last_error=error,
    )


def run_external_investment_solution_job(
    job_id: str,
    *,
    tool_executor: Any,
) -> Optional[Dict[str, Any]]:
    """Run one durable Investment Solution job in a Cloud Tasks request."""

    from advisor.agents.agents import build_investment_solution_specialist
    from advisor.agents.catalog import INVESTMENT_SOLUTION_SPECIALIST

    event = _persistence().get_business_event(job_id)
    if event is None:
        raise ValueError(f"Unknown specialist job: {job_id}")
    payload = _job_payload(event)
    if payload.get("specialist_key") != "investment_solution":
        raise ValueError(f"Job {job_id} is not an Investment Solution job")
    if str(event.get("status") or "") not in ACTIVE_JOB_STATUSES:
        return event
    request_payload = payload.get("request")
    if not isinstance(request_payload, dict):
        raise ValueError(f"Specialist job {job_id} has no durable request payload")

    request = SubAgentRequest.model_validate(request_payload)
    source_context = AwmAgentContext(
        client_id=request.client_id,
        session_id=str(request.objective.get("session_id") or ""),
        user_message=str(request.objective.get("request") or ""),
        client_file=copy.deepcopy(request.client_file),
        tool_executor=tool_executor,
        trace_id=gen_trace_id(),
        turn_id=str(request.objective.get("turn_id") or ""),
        root_span_id=new_span_id("specialist_cloud_task"),
        channel=str(payload.get("channel") or "text"),
    )
    agent = build_investment_solution_specialist()
    _run_specialist_job(
        job_id=job_id,
        request=request,
        source_context=source_context,
        agent=agent,
        max_turns=INVESTMENT_SOLUTION_SPECIALIST.max_turns,
        run_config=None,
        cancellation_event=None,
    )
    return _persistence().get_business_event(job_id)


def run_external_financial_planning_job(
    job_id: str,
    *,
    tool_executor: Any,
) -> Optional[Dict[str, Any]]:
    """Run one durable Financial Planning job in a Cloud Tasks request."""

    from advisor.agents.agents import build_financial_planning_specialist
    from advisor.agents.catalog import FINANCIAL_PLANNING_SPECIALIST

    event = _persistence().get_business_event(job_id)
    if event is None:
        raise ValueError(f"Unknown specialist job: {job_id}")
    payload = _job_payload(event)
    if payload.get("specialist_key") != "financial_planning":
        raise ValueError(f"Job {job_id} is not a Financial Planning job")
    if str(event.get("status") or "") not in ACTIVE_JOB_STATUSES:
        return event
    request_payload = payload.get("request")
    if not isinstance(request_payload, dict):
        raise ValueError(f"Specialist job {job_id} has no durable request payload")
    request = SubAgentRequest.model_validate(request_payload)
    source_context = AwmAgentContext(
        client_id=request.client_id,
        session_id=str(request.objective.get("session_id") or ""),
        user_message=str(request.objective.get("request") or ""),
        client_file=copy.deepcopy(request.client_file),
        tool_executor=tool_executor,
        trace_id=gen_trace_id(),
        turn_id=str(request.objective.get("turn_id") or ""),
        root_span_id=new_span_id("specialist_cloud_task"),
        channel=str(payload.get("channel") or "text"),
    )
    _run_specialist_job(
        job_id=job_id,
        request=request,
        source_context=source_context,
        agent=build_financial_planning_specialist(),
        max_turns=FINANCIAL_PLANNING_SPECIALIST.max_turns,
        run_config=None,
        cancellation_event=None,
    )
    return _persistence().get_business_event(job_id)


def recover_abandoned_specialist_jobs() -> Dict[str, int]:
    """Re-enqueue expired durable Cloud Tasks jobs after process restart."""

    recovered = 0
    failed = 0
    now = datetime.now(timezone.utc)
    events = _persistence().list_business_events(
        event_type=SPECIALIST_JOB_EVENT_TYPE,
        limit=500,
    )
    for event in events:
        if str(event.get("status") or "") != "running":
            continue
        payload = _job_payload(event)
        lease = str(payload.get("lease_expires_at") or "")
        try:
            expires_at = datetime.fromisoformat(lease.replace("Z", "+00:00"))
        except ValueError:
            expires_at = now - timedelta(seconds=1)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now:
            continue
        specialist_key = str(payload.get("specialist_key") or "")
        job_id = str(event.get("id") or "")
        if specialist_key in {"financial_planning", "investment_solution"} and _cloud_tasks_enabled():
            _persistence().update_business_event(
                job_id,
                status="running",
                payload_patch={
                    "lease_expires_at": _lease_expires_at(900.0),
                    "attempts": int(payload.get("attempts") or 1) + 1,
                    "error": None,
                    "recovered_at": _now(),
                },
                last_error=None,
            )
            _enqueue_specialist_cloud_task(job_id, specialist_key=specialist_key)
            recovered += 1
        else:
            _persistence().update_business_event(
                job_id,
                status="failed",
                payload_patch={"completed_at": _now(), "error": "Specialist job was interrupted before completion."},
                last_error="Specialist job lease expired before completion.",
            )
            failed += 1
    return {"recovered": recovered, "failed": failed}


def _run_callable_job(*, job_id: str, callback: Any) -> None:
    try:
        result = callback()
        current = _persistence().get_business_event(job_id)
        if current and str(current.get("status") or "") == "cancelled":
            return
        _persistence().update_business_event(
            job_id,
            status="done",
            payload_patch={
                "completed_at": _now(),
                "result": result,
            },
        )
    except BaseException as exc:
        current = _persistence().get_business_event(job_id)
        if current and str(current.get("status") or "") == "cancelled":
            return
        _persistence().update_business_event(
            job_id,
            status="failed",
            payload_patch={
                "completed_at": _now(),
                "error": str(exc),
            },
            last_error=str(exc),
        )


def _run_specialist_job(
    *,
    job_id: str,
    request: SubAgentRequest,
    source_context: AwmAgentContext,
    agent: Any,
    max_turns: int,
    run_config: Any,
    cancellation_event: Optional[Event],
) -> None:
    from advisor.agents.runtime import AwmRunHooks, run_agent_streamed_sync

    context = AwmAgentContext(
        client_id=request.client_id,
        session_id=str(request.objective.get("session_id") or ""),
        user_message=str(request.objective.get("request") or ""),
        client_file=copy.deepcopy(request.client_file),
        tool_executor=source_context.tool_executor,
        trace_id=gen_trace_id(),
        turn_id=f"background_{uuid.uuid4().hex[:12]}",
        root_span_id=new_span_id("specialist_job"),
        channel=source_context.channel,
    )
    try:
        if specialist_job_cancellation_requested(
            job_id,
            cancellation_event=cancellation_event,
        ):
            raise SpecialistJobCancelled(f"Specialist job {job_id} was cancelled")
        result = run_agent_streamed_sync(
            agent=agent,
            input_items=request.objective["request"],
            context=context,
            hooks=AwmRunHooks(
                specialist_job_id=job_id,
                cancellation_event=cancellation_event,
            ),
            max_turns=max_turns,
            run_config=run_config,
            timeout_seconds=getattr(agent, "_arc_timeout_seconds", None),
        )
        current = _persistence().get_business_event(job_id)
        if current and str(current.get("status") or "") == "cancelled":
            return
        artifact_ids = sorted(
            {
                str(
                    artifact.get("artifact_id")
                    or artifact.get("analysis_id")
                    or artifact.get("id")
                )
                for artifact in context.subagent_artifacts
                if isinstance(artifact, dict)
                and (
                    artifact.get("artifact_id")
                    or artifact.get("analysis_id")
                    or artifact.get("id")
                )
            }
        )
        final_output = getattr(result, "final_output", result)
        if hasattr(final_output, "model_dump"):
            final_output = final_output.model_dump()
        _persistence().update_business_event(
            job_id,
            status="done",
            payload_patch={
                "completed_at": _now(),
                "result": final_output,
                "artifact_ids": artifact_ids,
                "tool_results": persistent_safe_tool_results(
                    context.tool_results
                ),
                "trace_id": context.trace_id,
            },
        )
    except SpecialistJobCancelled:
        return
    except BaseException as exc:
        current = _persistence().get_business_event(job_id)
        if current and str(current.get("status") or "") == "cancelled":
            return
        _persistence().update_business_event(
            job_id,
            status="failed",
            payload_patch={
                "completed_at": _now(),
                "error": str(exc),
            },
            last_error=str(exc),
        )


def _receipt(event: Dict[str, Any], *, duplicate: bool) -> Dict[str, Any]:
    payload = _job_payload(event)
    return {
        "ok": True,
        "background": True,
        "duplicate": duplicate,
        "job_id": str(event.get("id") or ""),
        "specialist_key": payload.get("specialist_key"),
        "status": event.get("status"),
        "objective": payload.get("objective"),
        "message": (
            "Specialist work is already in progress."
            if duplicate
            else "Specialist work started. Present the result only after the job is done."
        ),
    }


def _forget_future(job_id: str) -> None:
    with _FUTURES_LOCK:
        _FUTURES.pop(job_id, None)
        _CANCELLATION_EVENTS.pop(job_id, None)


def _expire_stale_jobs(*, client_id: str) -> None:
    now = datetime.now(timezone.utc)
    for event in list_specialist_jobs(client_id=client_id, limit=100):
        if str(event.get("status") or "") != "running":
            continue
        lease = str(_job_payload(event).get("lease_expires_at") or "")
        if not lease:
            continue
        try:
            expires_at = datetime.fromisoformat(lease.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now:
            continue
        _persistence().update_business_event(
            str(event.get("id") or ""),
            status="failed",
            payload_patch={
                "completed_at": _now(),
                "error": "Specialist job was interrupted before completion.",
            },
            last_error="Specialist job lease expired before completion.",
        )


def wait_for_specialist_jobs(timeout: float = 10.0) -> None:
    """Wait for currently submitted jobs; intended for deterministic tests."""

    with _FUTURES_LOCK:
        futures = list(_FUTURES.values())
    for future in futures:
        try:
            future.result(timeout=timeout)
        except CancelledError:
            continue
