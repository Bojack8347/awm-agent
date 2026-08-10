"""Persistence for server-owned Companion conversation identities."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn


_MEMORY: Dict[str, Dict[str, Any]] = {}
_LOCK = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "client_id": str(row[1]),
        "account_id": str(row[2]),
        "status": str(row[3]),
        "origin": str(row[4]),
        "launch_id": row[5],
        "previous_session_id": row[6],
        "continuation_request_id": row[7],
        "lifecycle_version": int(row[8]),
        "metadata": dict(row[9] or {}),
        "created_at": row[10].isoformat() if row[10] else None,
        "updated_at": row[11].isoformat() if row[11] else None,
        "last_activity_at": row[12].isoformat() if row[12] else None,
        "closed_at": row[13].isoformat() if row[13] else None,
    }


_SELECT = """
    SELECT id, client_id, account_id, status, origin, launch_id,
           previous_session_id, continuation_request_id, lifecycle_version,
           metadata, created_at, updated_at, last_activity_at, closed_at
    FROM companion_sessions
"""


def create_companion_session(
    *,
    client_id: str,
    account_id: str,
    origin: str,
    launch_id: Optional[str] = None,
    previous_session_id: Optional[str] = None,
    continuation_request_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create one opaque conversation, idempotent by launch/continuation key."""

    if origin not in {"cold_launch", "explicit_new", "continued", "legacy"}:
        raise ValueError("invalid_companion_session_origin")
    if origin == "cold_launch" and not launch_id:
        raise ValueError("launch_id_required")
    if origin == "continued" and (not previous_session_id or not continuation_request_id):
        raise ValueError("continuation_identity_required")

    pool = _get_pool()
    if pool is None:
        with _LOCK:
            for existing in _MEMORY.values():
                same_launch = launch_id and existing["account_id"] == account_id and existing.get("launch_id") == launch_id
                same_continue = continuation_request_id and existing["account_id"] == account_id and existing.get("continuation_request_id") == continuation_request_id
                if same_launch or same_continue:
                    if existing["client_id"] != client_id or existing["origin"] != origin or existing.get("previous_session_id") != previous_session_id:
                        raise ValueError("idempotency_conflict")
                    return dict(existing)
            session_id = str(uuid.uuid4())
            now = _now()
            row = {
                "id": session_id,
                "client_id": client_id,
                "account_id": account_id,
                "status": "archived" if origin == "legacy" else "active",
                "origin": origin,
                "launch_id": launch_id,
                "previous_session_id": previous_session_id,
                "continuation_request_id": continuation_request_id,
                "lifecycle_version": 1,
                "metadata": dict(metadata or {}),
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "closed_at": None,
            }
            _MEMORY[session_id] = row
            return dict(row)

    session_id = str(uuid.uuid4())
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                if previous_session_id:
                    cur.execute(
                        _SELECT + " WHERE id = %s FOR SHARE",
                        (previous_session_id,),
                    )
                    previous = cur.fetchone()
                    if previous is None:
                        raise LookupError("companion_session_not_found")
                    previous_row = _serialize_row(previous)
                    if previous_row["client_id"] != client_id or previous_row["account_id"] != account_id:
                        raise PermissionError("companion_session_forbidden")
                cur.execute(
                    """
                    INSERT INTO companion_sessions
                        (id, client_id, account_id, status, origin, launch_id,
                         previous_session_id, continuation_request_id, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        session_id,
                        client_id,
                        account_id,
                        "archived" if origin == "legacy" else "active",
                        origin,
                        launch_id,
                        previous_session_id,
                        continuation_request_id,
                        json.dumps(metadata or {}),
                    ),
                )
                inserted = cur.fetchone()
                if inserted is None:
                    if continuation_request_id:
                        cur.execute(
                            _SELECT + " WHERE account_id = %s AND continuation_request_id = %s",
                            (account_id, continuation_request_id),
                        )
                    else:
                        cur.execute(
                            _SELECT + " WHERE account_id = %s AND launch_id = %s",
                            (account_id, launch_id),
                        )
                    existing = cur.fetchone()
                    if existing is None:
                        raise RuntimeError("companion_session_create_conflict")
                    row = _serialize_row(existing)
                    if row["client_id"] != client_id or row["origin"] != origin or row.get("previous_session_id") != previous_session_id:
                        raise ValueError("idempotency_conflict")
                    conn.commit()
                    return row
                cur.execute(_SELECT + " WHERE id = %s", (session_id,))
                created = cur.fetchone()
            conn.commit()
            return _serialize_row(created)
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def get_companion_session(session_id: str) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = _MEMORY.get(session_id)
            return dict(row) if row else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE id = %s", (session_id,))
            row = cur.fetchone()
            return _serialize_row(row) if row else None
    finally:
        pool.putconn(conn)


def list_companion_sessions(*, client_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    bounded = max(1, min(int(limit), 100))
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            rows = [dict(row) for row in _MEMORY.values() if row["client_id"] == client_id]
        return sorted(rows, key=lambda row: row["last_activity_at"], reverse=True)[:bounded]
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE client_id = %s ORDER BY last_activity_at DESC LIMIT %s",
                (client_id, bounded),
            )
            return [_serialize_row(row) for row in cur.fetchall()]
    finally:
        pool.putconn(conn)


def touch_companion_session(*, session_id: str, client_id: str, account_id: str) -> bool:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = _MEMORY.get(session_id)
            if not row or row["client_id"] != client_id or row["account_id"] != account_id or row["status"] != "active":
                return False
            now = _now()
            row["updated_at"] = now
            row["last_activity_at"] = now
            return True
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE companion_sessions
                SET updated_at = NOW(), last_activity_at = NOW()
                WHERE id = %s AND client_id = %s AND account_id = %s AND status = 'active'
                """,
                (session_id, client_id, account_id),
            )
            changed = cur.rowcount == 1
        conn.commit()
        return changed
    finally:
        pool.putconn(conn)


def close_companion_session(
    *,
    session_id: str,
    client_id: str,
    account_id: str,
    expected_lifecycle_version: int,
) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = _MEMORY.get(session_id)
            if not row or row["client_id"] != client_id or row["account_id"] != account_id:
                return None
            if row["status"] == "closed":
                return dict(row)
            if row["status"] != "active" or row["lifecycle_version"] != expected_lifecycle_version:
                raise ValueError("companion_session_version_conflict")
            now = _now()
            row.update({"status": "closed", "lifecycle_version": row["lifecycle_version"] + 1, "updated_at": now, "closed_at": now})
            return dict(row)
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE companion_sessions
                SET status = 'closed', lifecycle_version = lifecycle_version + 1,
                    updated_at = NOW(), closed_at = NOW()
                WHERE id = %s AND client_id = %s AND account_id = %s
                  AND status = 'active' AND lifecycle_version = %s
                RETURNING id
                """,
                (session_id, client_id, account_id, expected_lifecycle_version),
            )
            changed = cur.fetchone()
            if changed is None:
                cur.execute(_SELECT + " WHERE id = %s AND client_id = %s AND account_id = %s", (session_id, client_id, account_id))
                current = cur.fetchone()
                if current and _serialize_row(current)["status"] == "closed":
                    conn.commit()
                    return _serialize_row(current)
                conn.rollback()
                if current:
                    raise ValueError("companion_session_version_conflict")
                return None
            cur.execute(_SELECT + " WHERE id = %s", (session_id,))
            row = cur.fetchone()
        conn.commit()
        return _serialize_row(row)
    finally:
        pool.putconn(conn)


def _reset_memory_companion_sessions_for_tests() -> None:
    with _LOCK:
        _MEMORY.clear()
