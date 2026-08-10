"""Journey run, event, evidence, lease, and policy persistence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn, _pg_json

def set_journey_run_progress_label(journey_id: str, label: str) -> bool:
    """Write a live progress_label into the journey run's metadata.

    Mirrors set_consultation_session_progress_label — enables the journey
    status endpoint to surface live progress from any Cloud Run worker,
    not just the one running the background thread.
    """
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE journey_runs
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object('progress_label', %s::text)
                    WHERE id = %s
                    """,
                    (label, journey_id),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to set journey progress label: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
def reconcile_orphaned_journeys(min_age_minutes: int = 5) -> int:
    """Mark journeys stuck in 'running' as abandoned. Returns count reclaimed.

    Intended to run once at process startup to clean up journeys whose
    workers were killed by a Cloud Run redeploy or scale-down event.
    """
    pool = _get_pool()
    if pool is None:
        return 0
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM journey_runs
                     WHERE state = 'running'
                       AND created_at < NOW() - INTERVAL '%s minutes'
                    """,
                    (min_age_minutes,),
                )
                ids = [row[0] for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] reconcile_orphaned_journeys query failed: {exc}", flush=True)
        return 0

    if not ids:
        return 0

    # Lazy import — state_machine.transition_journey_state imports back
    # from this module, so resolve at call time.
    from api.services.journey_lifecycle import (  # type: ignore
        InvalidStateTransition,
        transition_journey_state,
    )
    from datetime import datetime as _dt, timezone as _tz

    reclaimed = 0
    for jid in ids:
        try:
            transition_journey_state(
                str(jid),
                new_state="abandoned",
                reason="orphan_reconciliation",
                extra_cols={
                    "completed_at": _dt.now(_tz.utc).isoformat(),
                    "metadata": {
                        "error": "orphaned — process restarted while journey was running",
                    },
                },
            )
            reclaimed += 1
        except InvalidStateTransition as bad:
            print(f"[db] orphan reconcile skipped {jid}: {bad}", flush=True)
        except Exception as exc:
            print(f"[db] orphan reconcile failed for {jid}: {exc}", flush=True)
    return reclaimed


# Journey runs — investment journey lifecycle
# ---------------------------------------------------------------------------

def create_journey_run(
    client_id: str,
    journey_type: str = "investment",
    baseline_snapshot_version: Optional[int] = None,
    consultation_session_id: Optional[str] = None,
    companion_session_id: Optional[str] = None,
) -> Optional[str]:
    """Create a journey run. Returns journey ID on success.

    `companion_session_id` records the originating companion session per
    plan §5.5 (Phase 5A schema). When present, the soak duplicate-row
    monitoring SQL can distinguish a legitimately-started V2 journey
    (companion_session_id IS NOT NULL OR has events) from a stray
    `state='collecting'` row left behind by the legacy mobile path during
    the cutover window.
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        journey_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO journey_runs
                        (id, client_id, journey_type, baseline_snapshot_version,
                         consultation_session_id, companion_session_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        journey_id, client_id, journey_type,
                        baseline_snapshot_version, consultation_session_id,
                        companion_session_id,
                    ),
                )
            conn.commit()
            return journey_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to create journey run: {exc}", flush=True)
        return None


def get_journey_run(journey_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a journey run by ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, journey_type, state,
                           baseline_snapshot_version, consultation_session_id,
                           solution_output, write_back_candidates,
                           write_back_committed, metadata,
                           created_at, completed_at,
                           activated_at, activation_snapshot_version
                    FROM journey_runs WHERE id = %s
                    """,
                    (journey_id,),
                )
                r = cur.fetchone()
                if r is None:
                    return None
                state = r[3]
                legacy_status = {
                    "collecting": "consulting",
                    "ready": "consulting",
                    "running": "running",
                    "completed": "completed",
                    "activated": "completed",
                    "abandoned": "failed",
                    "declined": "completed",
                }.get(state, state)
                policy_status = {
                    "activated": "activated",
                    "declined": "declined",
                }.get(state, "proposed")
                journey = {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "journey_type": r[2],
                    "state": state,
                    "status": legacy_status,
                    "baseline_snapshot_version": r[4],
                    "consultation_session_id": str(r[5]) if r[5] else None,
                    "solution_output": r[6] or {},
                    "write_back_candidates": r[7] or [],
                    "write_back_committed": r[8],
                    "metadata": r[9] or {},
                    "created_at": r[10].isoformat() if r[10] else None,
                    "completed_at": r[11].isoformat() if r[11] else None,
                    "policy_status": policy_status,
                    "activated_at": r[12].isoformat() if r[12] else None,
                    "activation_snapshot_version": r[13],
                }
                _attach_advisory_metadata([journey])
                return journey
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get journey run: {exc}", flush=True)
        return None


def update_journey_run(journey_id: str, **columns: Any) -> bool:
    """Update one or more columns on a journey run."""
    pool = _get_pool()
    if pool is None or not columns:
        return False

    allowed = {
        "consultation_session_id", "solution_output",
        "write_back_candidates", "write_back_committed",
        "metadata", "reasoning_cache", "completed_at", "baseline_snapshot_version",
        "activated_at", "activation_snapshot_version",
    }
    filtered = {k: v for k, v in columns.items() if k in allowed}
    if not filtered:
        return False

    set_clauses = []
    values = []
    for col, val in filtered.items():
        set_clauses.append(f"{col} = %s")
        if col in ("solution_output", "write_back_candidates", "metadata", "reasoning_cache"):
            values.append(_pg_json(val))
        else:
            values.append(val)
    values.append(journey_id)

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE journey_runs SET {', '.join(set_clauses)} WHERE id = %s",
                    values,
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to update journey run: {exc}", flush=True)
        return False


def get_active_journey_runs(client_id: str) -> List[Dict[str, Any]]:
    """Retrieve active journey runs for a client."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, journey_type, state,
                           baseline_snapshot_version, consultation_session_id,
                           metadata, created_at
                    FROM journey_runs
                    WHERE client_id = %s
                      AND state IN ('collecting', 'ready', 'running')
                    ORDER BY created_at DESC
                    """,
                    (client_id,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "journey_type": r[2],
                        "status": r[3],
                        "state": r[3],
                        "baseline_snapshot_version": r[4],
                        "consultation_session_id": str(r[5]) if r[5] else None,
                        "metadata": r[6] or {},
                        "created_at": r[7].isoformat() if r[7] else None,
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get active journey runs: {exc}", flush=True)
        return []


def get_recent_journey_events(client_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    """Return recent journey events for companion context.

    This is the bridge from sub-river activity back to the main-river agent:
    taps, system milestones, and other journey events are summarized in the
    companion prompt instead of being converted into hard-coded chat bubbles.
    """
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 12), 50))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.id, e.journey_id, r.journey_type, r.state,
                           e.event_type, e.source, e.actor, e.payload,
                           e.applied_state, e.idempotency_key, e.request_id,
                           e.recorded_at
                    FROM journey_events e
                    JOIN journey_runs r ON r.id = e.journey_id
                    WHERE r.client_id = %s
                    ORDER BY e.recorded_at DESC
                    LIMIT %s
                    """,
                    (client_id, limit),
                )
                rows = cur.fetchall()
                events = [
                    {
                        "id": str(r[0]),
                        "journey_id": str(r[1]),
                        "journey_type": r[2],
                        "journey_state": r[3],
                        "event_type": r[4],
                        "source": r[5],
                        "actor": r[6],
                        "payload": r[7] or {},
                        "applied_state": r[8],
                        "idempotency_key": r[9],
                        "request_id": r[10],
                        "recorded_at": r[11].isoformat() if r[11] else None,
                    }
                    for r in rows
                ]
                return list(reversed(events))
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get recent journey events: {exc}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Reasoning tasks — async financial planning lifecycle
# ---------------------------------------------------------------------------

def get_activated_policies(client_id: str) -> List[Dict[str, Any]]:
    """Retrieve activated journey policies for a client.

    These are policies the client has actually executed, not just proposals.
    """
    advisory = _get_advisory_policies(client_id=client_id, statuses=["accepted", "activated"])
    pool = _get_pool()
    if pool is None:
        return advisory
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, journey_type, state,
                           solution_output, activated_at,
                           activation_snapshot_version, created_at, completed_at
                    FROM journey_runs
                    WHERE client_id = %s AND state = 'activated'
                    ORDER BY activated_at DESC
                    """,
                    (client_id,),
                )
                rows = cur.fetchall()
                policies = [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "journey_type": r[2],
                        "state": r[3],
                        "status": r[3],
                        "policy_status": "activated",
                        "solution_output": r[4] or {},
                        "activated_at": r[5].isoformat() if r[5] else None,
                        "activation_snapshot_version": r[6],
                        "created_at": r[7].isoformat() if r[7] else None,
                        "completed_at": r[8].isoformat() if r[8] else None,
                    }
                    for r in rows
                ]
                _attach_advisory_metadata(policies)
                return _merge_policy_lists(advisory, policies)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get activated policies: {exc}", flush=True)
        return advisory


# =============================================================================
# Proactive engagement — escalation tracking, outreach log, outbound queue
# =============================================================================

def get_journey_state(journey_id: str) -> Optional[Dict[str, Any]]:
    """Return the new-schema journey row fields needed by JourneyRuntime.

    Returns dict with `id, client_id, journey_type, state, collected_fields,
    last_event_id, last_input_source, paused_at, resumed_at, definition_version,
    task_lease_expires_at, lease_owner_token, companion_session_id, solution_output,
    write_back_committed`. Returns None if no row matches or the new
    columns aren't present (pre-migration environment).
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                         WHERE table_name = 'journey_runs' AND column_name = 'state'
                    )
                    """
                )
                if not cur.fetchone()[0]:
                    return None

                cur.execute(
                    """
                    SELECT id, client_id, journey_type, state, collected_fields,
                           last_event_id, last_input_source, paused_at,
                           resumed_at, definition_version, task_lease_expires_at,
                           lease_owner_token, companion_session_id,
                           solution_output, write_back_committed,
                           created_at, completed_at
                    FROM journey_runs WHERE id = %s
                    """,
                    (journey_id,),
                )
                r = cur.fetchone()
                if r is None:
                    return None
                row = {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "journey_type": r[2],
                    "state": r[3],
                    "collected_fields": r[4] or {},
                    "last_event_id": str(r[5]) if r[5] else None,
                    "last_input_source": r[6],
                    "paused_at": r[7].isoformat() if r[7] else None,
                    "resumed_at": r[8].isoformat() if r[8] else None,
                    "definition_version": r[9],
                    "task_lease_expires_at": r[10].isoformat() if r[10] else None,
                    "lease_owner_token": r[11],
                    "companion_session_id": str(r[12]) if r[12] else None,
                    "solution_output": r[13] or {},
                    "write_back_committed": r[14],
                    "created_at": r[15].isoformat() if r[15] else None,
                    "completed_at": r[16].isoformat() if r[16] else None,
                }
                advisory = _get_advisory_plan_for_journey(str(r[0]))
                if advisory:
                    row["advisory_plan_id"] = advisory.get("id")
                    row["advisory_artifact_id"] = advisory.get("current_artifact_id")
                    row["engine_run_id"] = advisory.get("engine_run_id")
                    row["advisory_status"] = advisory.get("status")
                    row["artifact_version"] = advisory.get("artifact_version")
                    artifact_payload = advisory.get("artifact_payload")
                    if isinstance(artifact_payload, dict) and artifact_payload:
                        row["solution_output"] = artifact_payload
                return row
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_journey_state failed: {exc}", flush=True)
        return None


def reserve_journey_event(
    *,
    journey_id: str,
    event_type: str,
    source: str,
    actor: str,
    payload: Dict[str, Any],
    applied_state: str,
    idempotency_key: str,
    request_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim an idempotency-keyed event slot.

    Used by JourneyRuntime.apply_event to fix the concurrent-idempotency
    bug (audit §"Bug 3"): reserve the (journey_id, idempotency_key) row
    BEFORE running the handler, so concurrent retries with the same key
    cannot all pass a pre-check and each write evidence.

    Returns:
      {"event_id": str, "inserted": True}   — caller owns the key, run handler
      {"event_id": str, "inserted": False, "applied_state": str}
                                            — another caller already owns it,
                                              return its cached result
      None                                  — DB unavailable

    Relies on the partial unique index `idx_je_idem` on
    (journey_id, idempotency_key) WHERE idempotency_key IS NOT NULL.
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO journey_events
                        (journey_id, event_type, source, actor, payload,
                         idempotency_key, request_id, applied_state)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (journey_id, idempotency_key)
                        WHERE idempotency_key IS NOT NULL
                        DO NOTHING
                    RETURNING id
                    """,
                    (
                        journey_id, event_type, source, actor,
                        _pg_json(payload), idempotency_key, request_id,
                        applied_state,
                    ),
                )
                inserted_row = cur.fetchone()
                if inserted_row is not None:
                    event_id = str(inserted_row[0])
                    cur.execute(
                        "UPDATE journey_runs SET last_event_id = %s WHERE id = %s",
                        (event_id, journey_id),
                    )
                    conn.commit()
                    return {"event_id": event_id, "inserted": True}

                # Conflict: another concurrent caller already reserved this key.
                # Read its row so we can return the same applied_state.
                cur.execute(
                    """
                    SELECT id, applied_state FROM journey_events
                     WHERE journey_id = %s AND idempotency_key = %s
                     LIMIT 1
                    """,
                    (journey_id, idempotency_key),
                )
                existing = cur.fetchone()
                conn.commit()
                if existing is None:
                    return None
                return {
                    "event_id": str(existing[0]),
                    "inserted": False,
                    "applied_state": existing[1],
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] reserve_journey_event failed: {exc}", flush=True)
        return None


def finalize_journey_event_state(
    *,
    event_id: str,
    applied_state: str,
) -> bool:
    """Update a previously-reserved journey_events row's applied_state.

    Used after the handler+transition logic completes so the row records
    the post-event state. Idempotent — safe to call multiple times.
    """
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE journey_events SET applied_state = %s WHERE id = %s",
                    (applied_state, event_id),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] finalize_journey_event_state failed: {exc}", flush=True)
        return False


def append_journey_event(
    *,
    journey_id: str,
    event_type: str,
    source: str,
    actor: str,
    payload: Dict[str, Any],
    applied_state: str,
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Optional[str]:
    """Append a row to journey_events. Returns the event id (UUID) or None.

    Idempotency: if `idempotency_key` is provided and a row already exists
    with that key for this journey, returns the existing row's id (no
    duplicate). The partial unique index `idx_je_idem` enforces this at
    the DB level.
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if idempotency_key:
                    cur.execute(
                        """
                        SELECT id FROM journey_events
                         WHERE journey_id = %s AND idempotency_key = %s
                         LIMIT 1
                        """,
                        (journey_id, idempotency_key),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        return str(existing[0])

                cur.execute(
                    """
                    INSERT INTO journey_events
                        (journey_id, event_type, source, actor, payload,
                         idempotency_key, request_id, applied_state)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        journey_id, event_type, source, actor,
                        _pg_json(payload), idempotency_key, request_id,
                        applied_state,
                    ),
                )
                event_id = str(cur.fetchone()[0])

                # Mirror onto journey_runs.last_event_id for fast access.
                cur.execute(
                    "UPDATE journey_runs SET last_event_id = %s WHERE id = %s",
                    (event_id, journey_id),
                )
            conn.commit()
            return event_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] append_journey_event failed: {exc}", flush=True)
        return None


def record_journey_evidence(
    *,
    journey_id: str,
    field_key: str,
    value: Any,
    source: str,
    confidence: Optional[float] = None,
    evidence_text: Optional[str] = None,
    agent_turn_id: Optional[str] = None,
) -> Optional[str]:
    """Append a journey_evidence row and update collected_fields JSONB.

    Returns the new evidence id (UUID) or None on failure / no DB.
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO journey_evidence
                        (journey_id, field_key, value, source, confidence,
                         evidence_text, agent_turn_id)
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        journey_id, field_key, _pg_json(value), source,
                        confidence, evidence_text, agent_turn_id,
                    ),
                )
                evidence_id = str(cur.fetchone()[0])

                # Update derived collected_fields JSONB (latest-wins).
                cur.execute(
                    """
                    UPDATE journey_runs
                       SET collected_fields = COALESCE(collected_fields, '{}'::jsonb)
                                                || jsonb_build_object(%s, %s::jsonb)
                     WHERE id = %s
                    """,
                    (field_key, _pg_json(value), journey_id),
                )
            conn.commit()
            return evidence_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] record_journey_evidence failed: {exc}", flush=True)
        return None


def acquire_journey_lease(journey_id: str, lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
    """Atomically inspect a journey row and decide whether to take/extend its
    Cloud-Tasks completion lease.

    Returns a dict describing the outcome:
      {"action": "run", "state": "ready", "lease_owner_token": "..."}
        — caller should drive ready -> running and start the pipeline
      {"action": "reclaim", "state": "running", "lease_owner_token": "..."}
        — previous lease expired; caller takes over and runs
      {"action": "running", "state": "running"}
        — another worker holds a fresh lease; caller returns 409 retryable
      {"action": "noop", "state": "completed" | "activated"}
        — work already done; caller returns 200
      {"action": "skip", "state": <other>}
        — state isn't valid for completion; caller returns 200 (no-op) or
          a state-shaped error
      None
        — DB unavailable or row missing
    """
    pool = _get_pool()
    if pool is None:
        return None
    lease_owner_token = uuid.uuid4().hex
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state, task_lease_expires_at, lease_owner_token
                      FROM journey_runs
                     WHERE id = %s
                       FOR UPDATE
                    """,
                    (journey_id,),
                )
                r = cur.fetchone()
                if r is None:
                    conn.commit()
                    return None
                state, lease = r[0], r[1]

                if state in ("completed", "activated"):
                    conn.commit()
                    return {"action": "noop", "state": state}

                if state == "running":
                    if lease is not None:
                        # Compare in DB so timezone-naive vs aware doesn't bite.
                        cur.execute("SELECT NOW() < %s", (lease,))
                        live = bool(cur.fetchone()[0])
                    else:
                        live = False
                    if live:
                        conn.commit()
                        return {"action": "running", "state": state}
                    # Expired lease — reclaim.
                    cur.execute(
                        """
                        UPDATE journey_runs
                           SET task_lease_expires_at = NOW() + (%s::text || ' seconds')::interval,
                               lease_owner_token = %s
                         WHERE id = %s
                        """,
                        (str(lease_seconds), lease_owner_token, journey_id),
                    )
                    conn.commit()
                    return {"action": "reclaim", "state": state, "lease_owner_token": lease_owner_token}

                if state == "ready":
                    # Caller transitions ready -> running; we set the lease
                    # in advance so the worker is protected even before the
                    # transition commits in the same TX.
                    cur.execute(
                        """
                        UPDATE journey_runs
                           SET task_lease_expires_at = NOW() + (%s::text || ' seconds')::interval,
                               lease_owner_token = %s
                         WHERE id = %s
                        """,
                        (str(lease_seconds), lease_owner_token, journey_id),
                    )
                    conn.commit()
                    return {"action": "run", "state": state, "lease_owner_token": lease_owner_token}

                conn.commit()
                return {"action": "skip", "state": state}
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] acquire_journey_lease failed: {exc}", flush=True)
        return None


def extend_journey_lease(
    journey_id: str,
    *,
    lease_owner_token: str,
    lease_seconds: int = 120,
) -> bool:
    """Heartbeat: extend the lease only if this worker still owns it."""
    pool = _get_pool()
    if pool is None or not lease_owner_token:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE journey_runs
                       SET task_lease_expires_at = NOW() + (%s::text || ' seconds')::interval
                     WHERE id = %s
                       AND lease_owner_token = %s
                    """,
                    (str(lease_seconds), journey_id, lease_owner_token),
                )
                updated = cur.rowcount
            conn.commit()
            return updated == 1
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] extend_journey_lease failed: {exc}", flush=True)
        return False


def clear_journey_lease(
    journey_id: str,
    *,
    lease_owner_token: Optional[str] = None,
) -> bool:
    """Clear the lease without touching a lease reclaimed by another worker."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if lease_owner_token:
                    cur.execute(
                        """
                        UPDATE journey_runs
                           SET task_lease_expires_at = NULL,
                               lease_owner_token = NULL
                         WHERE id = %s
                           AND lease_owner_token = %s
                        """,
                        (journey_id, lease_owner_token),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE journey_runs
                           SET task_lease_expires_at = NULL,
                               lease_owner_token = NULL
                         WHERE id = %s
                        """,
                        (journey_id,),
                    )
                updated = cur.rowcount
            conn.commit()
            return updated == 1
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] clear_journey_lease failed: {exc}", flush=True)
        return False


def get_proposed_policies(client_id: str) -> List[Dict[str, Any]]:
    """Return journeys in `state='completed'` (proposed policy, not yet
    activated). Mirrors get_activated_policies but for the proposed half.
    """
    advisory = _get_advisory_policies(client_id=client_id, statuses=["proposed"])
    pool = _get_pool()
    if pool is None:
        return advisory
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, journey_type, state,
                           solution_output, activated_at,
                           activation_snapshot_version, created_at, completed_at
                    FROM journey_runs
                    WHERE client_id = %s AND state = 'completed'
                    ORDER BY completed_at DESC NULLS LAST, created_at DESC
                    """,
                    (client_id,),
                )
                rows = cur.fetchall()
                policies = [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "journey_type": r[2],
                        "state": r[3],
                        "status": r[3],
                        "policy_status": "proposed",
                        "solution_output": r[4] or {},
                        "activated_at": r[5].isoformat() if r[5] else None,
                        "activation_snapshot_version": r[6],
                        "created_at": r[7].isoformat() if r[7] else None,
                        "completed_at": r[8].isoformat() if r[8] else None,
                    }
                    for r in rows
                ]
                _attach_advisory_metadata(policies)
                return _merge_policy_lists(advisory, policies)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_proposed_policies failed: {exc}", flush=True)
        return advisory


def _get_advisory_policies(*, client_id: str, statuses: List[str]) -> List[Dict[str, Any]]:
    """Read policy rows from the new advisory model when available."""
    try:
        from api.persistence.advisory import list_advisory_policies  # type: ignore

        return list_advisory_policies(client_id=client_id, statuses=statuses)
    except Exception as exc:
        print(f"[db] advisory policy read skipped: {exc}", flush=True)
        return []


def _merge_policy_lists(
    advisory: List[Dict[str, Any]],
    legacy: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Prefer advisory-backed rows and append legacy rows not yet mirrored."""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in advisory + legacy:
        row_id = str(row.get("id") or "")
        if row_id and row_id in seen:
            continue
        if row_id:
            seen.add(row_id)
        merged.append(row)
    return merged


def _attach_advisory_metadata(policies: List[Dict[str, Any]]) -> None:
    """Attach advisory mirror IDs without changing the legacy policy shape."""
    if not policies:
        return
    try:
        from api.persistence.advisory import get_advisory_plan_for_journey  # type: ignore
    except Exception:
        return
    for policy in policies:
        journey_id = str(policy.get("id") or "")
        if not journey_id:
            continue
        advisory = get_advisory_plan_for_journey(journey_id)
        if not advisory:
            continue
        policy["advisory_plan_id"] = advisory.get("id")
        policy["advisory_artifact_id"] = advisory.get("current_artifact_id")
        policy["engine_run_id"] = advisory.get("engine_run_id")
        policy["advisory_status"] = advisory.get("status")
        policy["artifact_version"] = advisory.get("artifact_version")


def _get_advisory_plan_for_journey(journey_id: str) -> Optional[Dict[str, Any]]:
    try:
        from api.persistence.advisory import get_advisory_plan_for_journey  # type: ignore

        return get_advisory_plan_for_journey(journey_id)
    except Exception:
        return None
