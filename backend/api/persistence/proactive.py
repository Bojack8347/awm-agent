"""Proactive engagement and outbound-message persistence."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn

def get_escalation_tracking(
    client_id: str,
    topic_key: str,
) -> Optional[Dict[str, Any]]:
    """Get escalation tracking for a specific client+topic."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, topic_key, trigger_class,
                           times_surfaced, first_surfaced_at, last_surfaced_at,
                           user_acknowledged, backed_off, created_at, updated_at
                    FROM proactive_escalation_tracking
                    WHERE client_id = %s AND topic_key = %s
                    """,
                    (client_id, topic_key),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "topic_key": r[2],
                    "trigger_class": r[3],
                    "times_surfaced": r[4],
                    "first_surfaced_at": r[5].isoformat() if r[5] else None,
                    "last_surfaced_at": r[6].isoformat() if r[6] else None,
                    "user_acknowledged": r[7],
                    "backed_off": r[8],
                    "created_at": r[9].isoformat() if r[9] else None,
                    "updated_at": r[10].isoformat() if r[10] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get escalation tracking: {exc}", flush=True)
        return None


def get_client_escalation_trackings(
    client_id: str,
    backed_off: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Get all escalation trackings for a client, optionally filtered by backed_off."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if backed_off is not None:
                    cur.execute(
                        """
                        SELECT id, client_id, topic_key, trigger_class,
                               times_surfaced, first_surfaced_at, last_surfaced_at,
                               user_acknowledged, backed_off, created_at, updated_at
                        FROM proactive_escalation_tracking
                        WHERE client_id = %s AND backed_off = %s
                        ORDER BY last_surfaced_at DESC
                        """,
                        (client_id, backed_off),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, client_id, topic_key, trigger_class,
                               times_surfaced, first_surfaced_at, last_surfaced_at,
                               user_acknowledged, backed_off, created_at, updated_at
                        FROM proactive_escalation_tracking
                        WHERE client_id = %s
                        ORDER BY last_surfaced_at DESC
                        """,
                        (client_id,),
                    )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "topic_key": r[2],
                        "trigger_class": r[3],
                        "times_surfaced": r[4],
                        "first_surfaced_at": r[5].isoformat() if r[5] else None,
                        "last_surfaced_at": r[6].isoformat() if r[6] else None,
                        "user_acknowledged": r[7],
                        "backed_off": r[8],
                        "created_at": r[9].isoformat() if r[9] else None,
                        "updated_at": r[10].isoformat() if r[10] else None,
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get client escalation trackings: {exc}", flush=True)
        return []


def upsert_escalation_tracking(
    client_id: str,
    topic_key: str,
    trigger_class: str,
) -> Optional[str]:
    """Create or increment escalation tracking. Returns tracking ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO proactive_escalation_tracking
                        (client_id, topic_key, trigger_class,
                         times_surfaced, first_surfaced_at, last_surfaced_at)
                    VALUES (%s, %s, %s, 1, NOW(), NOW())
                    ON CONFLICT (client_id, topic_key)
                    DO UPDATE SET
                        times_surfaced = proactive_escalation_tracking.times_surfaced + 1,
                        last_surfaced_at = NOW(),
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (client_id, topic_key, trigger_class),
                )
                row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to upsert escalation tracking: {exc}", flush=True)
        return None


def mark_escalation_acknowledged(
    client_id: str,
    topic_key: str,
) -> bool:
    """Mark a topic as acknowledged by the user."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE proactive_escalation_tracking
                    SET user_acknowledged = TRUE, updated_at = NOW()
                    WHERE client_id = %s AND topic_key = %s
                    """,
                    (client_id, topic_key),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to mark escalation acknowledged: {exc}", flush=True)
        return False


def mark_escalation_backed_off(
    client_id: str,
    topic_key: str,
) -> bool:
    """Mark a topic as backed off (hit max mentions)."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE proactive_escalation_tracking
                    SET backed_off = TRUE, updated_at = NOW()
                    WHERE client_id = %s AND topic_key = %s
                    """,
                    (client_id, topic_key),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to mark escalation backed off: {exc}", flush=True)
        return False


def store_proactive_outreach(
    client_id: str,
    trigger_class: str,
    trigger_type: str,
    trigger_reason: str,
    guidance_mode: str,
    escalation_level: int,
    objective: str,
    bubbles: List[str],
    grounding_fact_ids: Optional[List[str]] = None,
    diagnosis_snapshot_version: Optional[int] = None,
    knowledge_snapshot_version: Optional[int] = None,
    push_preview_text: Optional[str] = None,
    allowed_cta: Optional[str] = None,
    escalation_tracking_id: Optional[str] = None,
) -> Optional[str]:
    """Store a proactive outreach log entry. Returns outreach ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        outreach_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO proactive_outreach_log
                        (id, client_id, trigger_class, trigger_type, trigger_reason,
                         guidance_mode, escalation_level, objective,
                         grounding_fact_ids, diagnosis_snapshot_version,
                         knowledge_snapshot_version, push_preview_text, bubbles,
                         allowed_cta, escalation_tracking_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        outreach_id, client_id, trigger_class, trigger_type,
                        trigger_reason, guidance_mode, escalation_level, objective,
                        json.dumps(grounding_fact_ids or []),
                        diagnosis_snapshot_version, knowledge_snapshot_version,
                        push_preview_text, json.dumps(bubbles),
                        allowed_cta, escalation_tracking_id,
                    ),
                )
            conn.commit()
            return outreach_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store proactive outreach: {exc}", flush=True)
        return None


def get_recent_outreach(
    client_id: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Get recent proactive outreach for a client."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, trigger_class, trigger_type,
                           trigger_reason, guidance_mode, escalation_level,
                           objective, bubbles, push_preview_text,
                           delivered_at, user_acknowledged, created_at
                    FROM proactive_outreach_log
                    WHERE client_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (client_id, limit),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "trigger_class": r[2],
                        "trigger_type": r[3],
                        "trigger_reason": r[4],
                        "guidance_mode": r[5],
                        "escalation_level": r[6],
                        "objective": r[7],
                        "bubbles": r[8] if isinstance(r[8], list) else [],
                        "push_preview_text": r[9],
                        "delivered_at": r[10].isoformat() if r[10] else None,
                        "user_acknowledged": r[11],
                        "created_at": r[12].isoformat() if r[12] else None,
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get recent outreach: {exc}", flush=True)
        return []


def enqueue_outbound_message(
    client_id: str,
    outreach_log_id: str,
    bubbles: List[str],
    session_id: Optional[str] = None,
    push_preview_text: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> Optional[str]:
    """Enqueue a proactive message for delivery. Returns queue entry ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        entry_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO outbound_message_queue
                        (id, client_id, outreach_log_id, session_id,
                         bubbles, push_preview_text, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        entry_id, client_id, outreach_log_id, session_id,
                        json.dumps(bubbles), push_preview_text,
                        expires_at,
                    ),
                )
            conn.commit()
            return entry_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to enqueue outbound message: {exc}", flush=True)
        return None


def get_queued_outbound_messages(
    client_id: str,
) -> List[Dict[str, Any]]:
    """Get all queued (undelivered) outbound messages for a client."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, outreach_log_id, session_id,
                           bubbles, push_preview_text, status,
                           created_at, expires_at
                    FROM outbound_message_queue
                    WHERE client_id = %s AND status = 'queued'
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY created_at ASC
                    """,
                    (client_id,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "outreach_log_id": str(r[2]) if r[2] else None,
                        "session_id": r[3],
                        "bubbles": r[4] if isinstance(r[4], list) else [],
                        "push_preview_text": r[5],
                        "status": r[6],
                        "created_at": r[7].isoformat() if r[7] else None,
                        "expires_at": r[8].isoformat() if r[8] else None,
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get queued outbound messages: {exc}", flush=True)
        return []


def mark_outbound_message_delivered(
    message_id: str,
) -> bool:
    """Mark an outbound message as delivered."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbound_message_queue
                    SET status = 'delivered', delivered_at = NOW()
                    WHERE id = %s AND status = 'queued'
                    """,
                    (message_id,),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to mark outbound message delivered: {exc}", flush=True)
        return False


def get_last_companion_interaction_at(
    client_id: str,
) -> Optional[str]:
    """Get timestamp of the last companion message (user or assistant) for a client.

    Scans ai_companion_messages for the most recent message across all sessions
    belonging to this client. Used by the proactive planner to determine
    days since last interaction.
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
                    SELECT MAX(m.created_at)
                    FROM ai_companion_messages m
                    WHERE m.session_id IN (
                        SELECT DISTINCT session_id
                        FROM ai_companion_messages
                        WHERE session_id LIKE %s
                    )
                    """,
                    (f"{client_id}%",),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0].isoformat()
                return None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get last companion interaction: {exc}", flush=True)
        return None


def get_active_client_ids() -> List[str]:
    """Get all client IDs with completed onboarding.

    Used by the proactive engagement batch evaluator (Cloud Scheduler) to
    enumerate which clients to evaluate each day. Only returns clients who
    have progressed past initial onboarding — i.e., they exist in auth_accounts
    with a non-null client_id.
    """
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT client_id
                    FROM auth_accounts
                    WHERE client_id IS NOT NULL
                    """
                )
                rows = cur.fetchall()
                return [str(r[0]) for r in rows if r[0]]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get active client ids: {exc}", flush=True)
        return []


# =============================================================================
# Phase 5B helpers — Main River / Sub-River runtime
# Read journey state, append events, record evidence. All helpers accept the
# Phase 5A schema; environments that haven't run the migration yet (no `state`
# column) fall through to a None / no-op return so the runtime degrades to the
# legacy `status` column gracefully.
# =============================================================================
