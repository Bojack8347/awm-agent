"""Atomic acceptance and lifecycle persistence for Companion turns."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from .companion import store_companion_message
from .core import _get_pool, _safe_getconn


_MEMORY: Dict[str, Dict[str, Any]] = {}
_LOCK = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_key(client_id: str, client_turn_id: str) -> str:
    return f"companion-turn:{client_id}:{client_turn_id}"


def _validate_client_turn_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_client_turn_id") from exc


def _public(row: Dict[str, Any], *, created: Optional[bool] = None) -> Dict[str, Any]:
    payload = dict(row.get("payload") or {})
    result = {
        "turn_id": str(row["id"]),
        "client_turn_id": payload.get("client_turn_id"),
        "companion_session_id": payload.get("companion_session_id"),
        "status": row.get("status"),
        "stage": payload.get("stage"),
        "user_message_id": payload.get("user_message_id"),
        "client_action_ref": payload.get("client_action_ref"),
        "assistant_message_id": payload.get("assistant_message_id"),
        "assistant_message": payload.get("assistant_message"),
        "trace_id": payload.get("trace_id"),
        "advisor_turn_id": payload.get("advisor_turn_id"),
        "specialist_job_ids": list(payload.get("specialist_job_ids") or []),
        "artifact_ids": list(payload.get("artifact_ids") or []),
        "started_at": payload.get("started_at") or row.get("occurred_at"),
        "completed_at": payload.get("completed_at"),
        "attempts": int(row.get("attempts") or payload.get("attempts") or 1),
        "retryable": bool(payload.get("retryable")),
        "retry_stage": payload.get("retry_stage"),
        "error": payload.get("error") or row.get("last_error"),
    }
    if created is not None:
        result["created"] = created
    return result


def accept_companion_turn(
    *,
    client_id: str,
    companion_session_id: str,
    client_turn_id: str,
    turn_type: str,
    user_message: str,
    client_action: Optional[Dict[str, Any]],
    input_source: Any,
    channel: str,
) -> Dict[str, Any]:
    """Atomically claim a client turn and its one input record."""

    normalized_turn_id = _validate_client_turn_id(client_turn_id)
    key = _event_key(client_id, normalized_turn_id)
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            existing = _MEMORY.get(key)
            if existing is not None:
                payload = existing["payload"]
                if payload["companion_session_id"] != companion_session_id or payload.get("input_fingerprint") != _input_fingerprint(user_message, client_action):
                    raise ValueError("client_turn_id_conflict")
                return _public(existing, created=False)
            user_message_id = None
            action_ref = None
            if user_message:
                user_message_id = store_companion_message(
                    session_id=companion_session_id,
                    client_id=client_id,
                    role="user",
                    content=user_message,
                    metadata={"source": input_source, "runtime": "advisor_runtime", "client_turn_id": normalized_turn_id},
                )
            elif client_action:
                action_ref = str(uuid.uuid4())
            now = _now()
            server_turn_id = str(uuid.uuid4())
            payload = {
                "client_turn_id": normalized_turn_id,
                "companion_session_id": companion_session_id,
                "turn_type": turn_type,
                "input_fingerprint": _input_fingerprint(user_message, client_action),
                "user_message_id": user_message_id,
                "client_action_ref": action_ref,
                "client_action": dict(client_action or {}) if action_ref else None,
                "assistant_message_id": None,
                "assistant_message": None,
                "trace_id": None,
                "advisor_turn_id": None,
                "stage": "accepted",
                "specialist_job_ids": [],
                "artifact_ids": [],
                "started_at": now,
                "completed_at": None,
                "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "attempts": 1,
                "retryable": False,
                "retry_stage": None,
                "error": None,
            }
            row = {
                "id": server_turn_id,
                "event_key": key,
                "client_id": client_id,
                "aggregate_type": "companion_turn",
                "aggregate_id": server_turn_id,
                "event_type": "agent.companion_turn",
                "status": "running",
                "payload": payload,
                "attempts": 1,
                "last_error": None,
                "occurred_at": now,
            }
            _MEMORY[key] = row
            return _public(row, created=True)

    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, payload, attempts, last_error, occurred_at FROM business_events WHERE event_key = %s FOR UPDATE",
                    (key,),
                )
                existing = cur.fetchone()
                if existing:
                    row = _row(existing)
                    payload = row["payload"]
                    if payload.get("companion_session_id") != companion_session_id or payload.get("input_fingerprint") != _input_fingerprint(user_message, client_action):
                        raise ValueError("client_turn_id_conflict")
                    conn.commit()
                    return _public(row, created=False)

                user_message_id: Optional[str] = None
                action_ref: Optional[str] = None
                if user_message:
                    user_message_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO ai_companion_messages
                            (id, session_id, client_id, role, content, state, metadata)
                        VALUES (%s, %s, %s, 'user', %s, '{}'::jsonb, %s::jsonb)
                        """,
                        (user_message_id, companion_session_id, client_id, user_message, json.dumps({"source": input_source, "runtime": "advisor_runtime", "client_turn_id": normalized_turn_id})),
                    )
                elif client_action:
                    action_ref = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO companion_client_actions
                            (id, client_id, companion_session_id, client_turn_id, action_type, payload)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (action_ref, client_id, companion_session_id, normalized_turn_id, str(client_action.get("type") or "unknown"), json.dumps(client_action)),
                    )
                server_turn_id = str(uuid.uuid4())
                payload = {
                    "client_turn_id": normalized_turn_id,
                    "companion_session_id": companion_session_id,
                    "turn_type": turn_type,
                    "input_fingerprint": _input_fingerprint(user_message, client_action),
                    "user_message_id": user_message_id,
                    "client_action_ref": action_ref,
                    "assistant_message_id": None,
                    "assistant_message": None,
                    "trace_id": None,
                    "advisor_turn_id": None,
                    "stage": "accepted",
                    "specialist_job_ids": [],
                    "artifact_ids": [],
                    "started_at": _now(),
                    "completed_at": None,
                    "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    "attempts": 1,
                    "retryable": False,
                    "retry_stage": None,
                    "error": None,
                }
                cur.execute(
                    """
                    INSERT INTO business_events
                        (id, event_key, client_id, aggregate_type, aggregate_id,
                         event_type, event_source, status, payload, attempts)
                    VALUES (%s, %s, %s, 'companion_turn', %s,
                            'agent.companion_turn', 'companion_api', 'running', %s::jsonb, 1)
                    RETURNING id, status, payload, attempts, last_error, occurred_at
                    """,
                    (server_turn_id, key, client_id, server_turn_id, json.dumps(payload)),
                )
                row = _row(cur.fetchone())
            conn.commit()
            return _public(row, created=True)
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def update_companion_turn(
    turn_id: str,
    *,
    client_id: str,
    status: Optional[str] = None,
    stage: Optional[str] = None,
    payload_patch: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    patch = dict(payload_patch or {})
    if stage:
        patch["stage"] = stage
    if error is not None:
        patch["error"] = error
    if status in {"done", "failed", "cancelled"}:
        patch.setdefault("completed_at", _now())
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = next((item for item in _MEMORY.values() if item["id"] == turn_id and item["client_id"] == client_id), None)
            if row is None:
                return None
            row["payload"].update(patch)
            if status:
                row["status"] = status
            if error is not None:
                row["last_error"] = error
            return _public(row)
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE business_events
                SET status = COALESCE(%s, status),
                    payload = payload || %s::jsonb,
                    last_error = COALESCE(%s, last_error),
                    consumed_at = CASE WHEN %s = 'done' THEN NOW() ELSE consumed_at END,
                    updated_at = NOW()
                WHERE id = %s AND client_id = %s AND event_type = 'agent.companion_turn'
                RETURNING id, status, payload, attempts, last_error, occurred_at
                """,
                (status, json.dumps(patch), error, status, turn_id, client_id),
            )
            raw = cur.fetchone()
        conn.commit()
        return _public(_row(raw)) if raw else None
    finally:
        pool.putconn(conn)


def get_companion_turn(*, turn_id: str, client_id: str, companion_session_id: str) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = next((item for item in _MEMORY.values() if item["id"] == turn_id and item["client_id"] == client_id and item["payload"]["companion_session_id"] == companion_session_id), None)
            return _public(row) if row else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, payload, attempts, last_error, occurred_at
                FROM business_events
                WHERE id = %s AND client_id = %s AND event_type = 'agent.companion_turn'
                  AND payload->>'companion_session_id' = %s
                """,
                (turn_id, client_id, companion_session_id),
            )
            raw = cur.fetchone()
            return _public(_row(raw)) if raw else None
    finally:
        pool.putconn(conn)


def list_companion_turns(*, client_id: str, companion_session_id: str, active_only: bool, limit: int) -> List[Dict[str, Any]]:
    bounded = max(1, min(int(limit), 100))
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            rows = [item for item in _MEMORY.values() if item["client_id"] == client_id and item["payload"]["companion_session_id"] == companion_session_id and (not active_only or item["status"] == "running")]
        return [_public(row) for row in sorted(rows, key=lambda item: item["occurred_at"], reverse=True)[:bounded]]
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, payload, attempts, last_error, occurred_at
                FROM business_events
                WHERE client_id = %s AND event_type = 'agent.companion_turn'
                  AND payload->>'companion_session_id' = %s
                  AND (%s = FALSE OR status = 'running')
                ORDER BY occurred_at DESC LIMIT %s
                """,
                (client_id, companion_session_id, active_only, bounded),
            )
            return [_public(_row(raw)) for raw in cur.fetchall()]
    finally:
        pool.putconn(conn)


def _input_fingerprint(message: str, action: Optional[Dict[str, Any]]) -> str:
    import hashlib
    canonical = json.dumps({"message": message, "action": action or None}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row(raw: Any) -> Dict[str, Any]:
    return {"id": str(raw[0]), "status": raw[1], "payload": dict(raw[2] or {}), "attempts": int(raw[3] or 0), "last_error": raw[4], "occurred_at": raw[5].isoformat() if raw[5] else None}


def _reset_memory_companion_turn_runs_for_tests() -> None:
    with _LOCK:
        _MEMORY.clear()
