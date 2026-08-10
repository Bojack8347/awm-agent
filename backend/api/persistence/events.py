"""Persistence helpers for business events / outbox.

Business events are the durable trigger stream for asynchronous product work.
They are separate from trace events: trace explains what happened, while
business_events says what downstream components may need to react to.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from .core import _get_pool, _pg_json, _safe_getconn

_memory_business_events: List[Dict[str, Any]] = []
_memory_business_events_lock = RLock()


def _reset_memory_business_events_for_tests() -> None:
    """Clear process-local business events. Intended only for test isolation."""
    with _memory_business_events_lock:
        _memory_business_events.clear()


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def create_business_event(
    *,
    event_type: str,
    client_id: Optional[str] = None,
    aggregate_type: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    event_source: Optional[str] = None,
    event_key: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    status: str = "pending",
) -> Optional[Dict[str, Any]]:
    """Create one event. ``event_key`` makes producer retries idempotent."""
    pool = _get_pool()
    if pool is None:
        with _memory_business_events_lock:
            if event_key:
                existing = next(
                    (
                        item
                        for item in _memory_business_events
                        if item.get("event_key") == event_key
                    ),
                    None,
                )
                if existing is not None:
                    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(existing)
            now = datetime.now(timezone.utc).isoformat()
            event = {
                "id": str(uuid.uuid4()),
                "event_key": event_key,
                "event_type": event_type,
                "client_id": client_id,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_source": event_source,
                "status": status,
                "occurred_at": now,
                "created_at": now,
                "updated_at": now,
                "payload": dict(payload or {}),
                "attempts": 0,
                "last_error": None,
                "available_at": now,
                "consumed_at": now if status == "consumed" else None,
            }
            _memory_business_events.append(event)
            return dict(event)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO business_events
                        (event_key, client_id, aggregate_type, aggregate_id,
                         event_type, event_source, status, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (event_key) WHERE event_key IS NOT NULL
                    DO UPDATE SET
                        updated_at = NOW()
                    RETURNING id, event_key, event_type, client_id, aggregate_type,
                              aggregate_id, status, occurred_at, payload
                    """,
                    (
                        event_key,
                        client_id,
                        aggregate_type,
                        aggregate_id,
                        event_type,
                        event_source,
                        status,
                        _pg_json(payload or {}),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _event_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_business_event failed: {exc}", flush=True)
        return None


def list_business_events(
    *,
    client_id: Optional[str] = None,
    aggregate_type: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    event_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List business events for admin/debug consumers."""
    pool = _get_pool()
    if pool is None:
        limit = max(1, min(int(limit or 100), 500))
        with _memory_business_events_lock:
            rows = [
                dict(item)
                for item in _memory_business_events
                if (not client_id or item.get("client_id") == client_id)
                and (
                    not aggregate_type
                    or item.get("aggregate_type") == aggregate_type
                )
                and (
                    not aggregate_id
                    or item.get("aggregate_id") == aggregate_id
                )
                and (not event_type or item.get("event_type") == event_type)
                and (not status or item.get("status") == status)
            ]
        rows.sort(
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                str(item.get("created_at") or ""),
            ),
            reverse=True,
        )
        return rows[:limit]
    limit = max(1, min(int(limit or 100), 500))
    where: List[str] = []
    params: List[Any] = []
    if client_id:
        where.append("client_id = %s")
        params.append(client_id)
    if aggregate_type:
        where.append("aggregate_type = %s")
        params.append(aggregate_type)
    if aggregate_id:
        where.append("aggregate_id = %s")
        params.append(aggregate_id)
    if event_type:
        where.append("event_type = %s")
        params.append(event_type)
    if status:
        where.append("status = %s")
        params.append(status)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, event_key, event_type, client_id, aggregate_type,
                           aggregate_id, status, occurred_at, payload, attempts,
                           last_error, available_at, consumed_at
                    FROM business_events
                    {sql_where}
                    ORDER BY occurred_at DESC, created_at DESC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                return [_event_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_business_events failed: {exc}", flush=True)
        return []


def get_business_event(event_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one business event by id."""
    pool = _get_pool()
    if pool is None:
        with _memory_business_events_lock:
            event = next(
                (
                    item
                    for item in _memory_business_events
                    if str(item.get("id")) == str(event_id)
                ),
                None,
            )
        return dict(event) if event is not None else None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, event_key, event_type, client_id, aggregate_type,
                           aggregate_id, status, occurred_at, payload, attempts,
                           last_error, available_at, consumed_at
                    FROM business_events
                    WHERE id = %s
                    """,
                    (event_id,),
                )
                row = cur.fetchone()
                return _event_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_business_event failed: {exc}", flush=True)
        return None


def update_business_event(
    event_id: str,
    *,
    status: Optional[str] = None,
    payload_patch: Optional[Dict[str, Any]] = None,
    last_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update mutable job state while preserving the event identity."""

    pool = _get_pool()
    patch = dict(payload_patch or {})
    if pool is None:
        with _memory_business_events_lock:
            event = next(
                (
                    item
                    for item in _memory_business_events
                    if str(item.get("id")) == str(event_id)
                ),
                None,
            )
            if event is None:
                return None
            if status is not None:
                event["status"] = status
            event["payload"] = {
                **dict(event.get("payload") or {}),
                **patch,
            }
            event["last_error"] = last_error
            event["updated_at"] = datetime.now(timezone.utc).isoformat()
            if status in {"done", "failed", "cancelled", "consumed"}:
                event["consumed_at"] = event["updated_at"]
            return dict(event)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE business_events
                    SET status = COALESCE(%s, status),
                        payload = payload || %s::jsonb,
                        last_error = %s,
                        consumed_at = CASE
                            WHEN %s IN ('done', 'failed', 'cancelled', 'consumed')
                            THEN NOW()
                            ELSE consumed_at
                        END,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, event_key, event_type, client_id, aggregate_type,
                              aggregate_id, status, occurred_at, payload, attempts,
                              last_error, available_at, consumed_at
                    """,
                    (
                        status,
                        _pg_json(patch),
                        last_error[:1000] if last_error else None,
                        status,
                        event_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _event_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] update_business_event failed: {exc}", flush=True)
        return None


def claim_pending_business_events(
    *,
    limit: int = 20,
    event_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Claim a small batch of pending events for one worker."""
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 20), 100))
    event_type_clause = "AND event_type = %s" if event_type else ""
    params: List[Any] = [limit] if not event_type else [event_type, limit]
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH picked AS (
                        SELECT id
                        FROM business_events
                        WHERE status = 'pending'
                          AND available_at <= NOW()
                          {event_type_clause}
                        ORDER BY occurred_at ASC, created_at ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE business_events b
                    SET status = 'processing',
                        attempts = attempts + 1,
                        updated_at = NOW()
                    FROM picked
                    WHERE b.id = picked.id
                    RETURNING b.id, b.event_key, b.event_type, b.client_id,
                              b.aggregate_type, b.aggregate_id, b.status,
                              b.occurred_at, b.payload, b.attempts
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
            conn.commit()
            return [_event_row(row) for row in rows]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] claim_pending_business_events failed: {exc}", flush=True)
        return []


def mark_business_event_consumed(event_id: str) -> bool:
    """Mark one processing event as consumed."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE business_events
                    SET status = 'consumed',
                        consumed_at = NOW(),
                        updated_at = NOW(),
                        last_error = NULL
                    WHERE id = %s
                    """,
                    (event_id,),
                )
                ok = cur.rowcount > 0
            conn.commit()
            return ok
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] mark_business_event_consumed failed: {exc}", flush=True)
        return False


def reset_business_event_for_retry(event_id: str) -> Optional[Dict[str, Any]]:
    """Put a failed/processing event back to pending for manual retry."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE business_events
                    SET status = 'pending',
                        available_at = NOW(),
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                      AND status IN ('failed', 'processing', 'pending')
                    RETURNING id, event_key, event_type, client_id, aggregate_type,
                              aggregate_id, status, occurred_at, payload, attempts,
                              last_error, available_at, consumed_at
                    """,
                    (event_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return _event_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] reset_business_event_for_retry failed: {exc}", flush=True)
        return None


def mark_business_event_failed(
    event_id: str,
    *,
    error: str,
    retry: bool = True,
) -> bool:
    """Mark an event as failed or return it to pending for retry."""
    pool = _get_pool()
    if pool is None:
        return False
    status = "pending" if retry else "failed"
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE business_events
                    SET status = %s,
                        last_error = %s,
                        available_at = CASE
                            WHEN %s THEN NOW() + INTERVAL '5 minutes'
                            ELSE available_at
                        END,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (status, error[:1000], retry, event_id),
                )
                ok = cur.rowcount > 0
            conn.commit()
            return ok
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] mark_business_event_failed failed: {exc}", flush=True)
        return False


def _event_row(row: Any) -> Dict[str, Any]:
    payload = {
        "id": str(row[0]),
        "event_key": row[1],
        "event_type": row[2],
        "client_id": row[3],
        "aggregate_type": row[4],
        "aggregate_id": row[5],
        "status": row[6],
        "occurred_at": _iso(row[7]),
        "payload": row[8] or {},
    }
    if len(row) > 9:
        payload["attempts"] = row[9]
    if len(row) > 10:
        payload["last_error"] = row[10]
    if len(row) > 11:
        payload["available_at"] = _iso(row[11])
    if len(row) > 12:
        payload["consumed_at"] = _iso(row[12])
    return payload


__all__ = [
    "claim_pending_business_events",
    "create_business_event",
    "get_business_event",
    "list_business_events",
    "mark_business_event_consumed",
    "mark_business_event_failed",
    "reset_business_event_for_retry",
    "update_business_event",
]
