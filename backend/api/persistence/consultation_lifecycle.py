"""Canonical consultation engagement and interaction-lease persistence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn
from .events import create_business_event


_ENGAGEMENTS: Dict[str, Dict[str, Any]] = {}
_INTERACTIONS: Dict[str, Dict[str, Any]] = {}
_LOCK = RLock()
_OPEN = {"active", "paused", "processing"}
_TERMINAL = {"completed", "abandoned", "superseded"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _copy(value: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(value, default=str))


def _engagement_row(row: Any) -> Dict[str, Any]:
    return {
        "consultation_engagement_id": str(row[0]), "id": str(row[0]),
        "client_id": row[1], "session_type": row[2], "journey_type": row[3],
        "journey_id": str(row[4]) if row[4] else None, "trigger_source": row[5],
        "baseline_snapshot_version": row[6], "companion_session_id": row[7],
        "status": row[8], "transcript": dict(row[9] or {}), "metadata": dict(row[10] or {}),
        "created_at": _iso(row[11]), "completed_at": _iso(row[12]),
        "updated_at": _iso(row[13]), "last_activity_at": _iso(row[14]),
        "paused_at": _iso(row[15]), "lifecycle_version": int(row[16]),
        "superseded_by": str(row[17]) if row[17] else None,
        "last_processing_status": row[18], "last_processing_error": row[19],
        "active_checkpoint_id": row[20], "checkpoint_version": row[21],
    }


_ENGAGEMENT_SELECT = """
SELECT id, client_id, session_type, journey_type, journey_id, trigger_source,
       baseline_snapshot_version, companion_session_id, status, transcript, metadata,
       created_at, completed_at, updated_at, last_activity_at, paused_at,
       lifecycle_version, superseded_by, last_processing_status,
       last_processing_error, active_checkpoint_id, checkpoint_version
FROM consultation_sessions
"""


def _interaction_row(row: Any) -> Dict[str, Any]:
    return {
        "interaction_id": str(row[0]), "consultation_engagement_id": str(row[1]),
        "client_interaction_id": str(row[2]), "companion_session_id": row[3],
        "channel": row[4], "status": row[5], "lease_expires_at": _iso(row[6]),
        "last_activity_at": _iso(row[7]), "started_at": _iso(row[8]),
        "ended_at": _iso(row[9]), "end_reason": row[10],
        "interaction_version": int(row[11]), "checkpoint_payload": dict(row[12] or {}) if row[12] else None,
        "checkpoint_client_turn_id": str(row[13]) if row[13] else None,
    }


_INTERACTION_SELECT = """
SELECT id, consultation_id, client_interaction_id, companion_session_id, channel,
       status, lease_expires_at, last_activity_at, started_at, ended_at, end_reason,
       interaction_version, checkpoint_payload, checkpoint_client_turn_id
FROM consultation_interactions
"""


def _event(*, event_type: str, client_id: str, engagement_id: str, event_key: str, payload: Dict[str, Any]) -> None:
    create_business_event(
        event_type=event_type, client_id=client_id,
        aggregate_type="consultation_session", aggregate_id=engagement_id,
        event_source="consultation_lifecycle", event_key=event_key, payload=payload,
    )


def ensure_open_consultation(
    *, client_id: str, session_type: str, journey_type: Optional[str] = None,
    journey_id: Optional[str] = None, trigger_source: Optional[str] = None,
    baseline_snapshot_version: Optional[int] = None,
    companion_session_id: Optional[str] = None, client_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    if session_type not in {"onboarding_understanding", "journey_preparation"}:
        raise ValueError("invalid_session_type")
    if session_type == "journey_preparation" and not journey_id:
        raise ValueError("journey_id_required")
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            existing = next((row for row in _ENGAGEMENTS.values() if row["client_id"] == client_id and row["session_type"] == session_type and row.get("journey_id") == journey_id and row["status"] in _OPEN), None)
            if existing:
                return {**_copy(existing), "created": False, "resumed": True, "disposition": "created_or_resumed"}
            now = _now().isoformat()
            engagement_id = str(uuid.uuid4())
            row = {
                "consultation_engagement_id": engagement_id, "id": engagement_id,
                "client_id": client_id, "session_type": session_type,
                "journey_type": journey_type, "journey_id": journey_id,
                "trigger_source": trigger_source, "baseline_snapshot_version": baseline_snapshot_version,
                "companion_session_id": companion_session_id, "status": "paused",
                "transcript": {}, "metadata": {"created_by_request_id": client_request_id} if client_request_id else {},
                "created_at": now, "completed_at": None, "updated_at": now,
                "last_activity_at": now, "paused_at": now, "lifecycle_version": 1,
                "superseded_by": None, "last_processing_status": None,
                "last_processing_error": None, "active_checkpoint_id": None,
                "checkpoint_version": None,
            }
            _ENGAGEMENTS[engagement_id] = row
        _event(event_type="consultation.lifecycle_changed", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation:{engagement_id}:1", payload={"from": None, "to": "paused", "reason": "created", "request_id": client_request_id, "lifecycle_version": 1})
        return {**_copy(row), "created": True, "resumed": False, "disposition": "created_or_resumed"}

    engagement_id = str(uuid.uuid4())
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                lock_key = f"consultation:{client_id}:{session_type}:{journey_id or ''}"
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
                query = _ENGAGEMENT_SELECT + " WHERE client_id = %s AND session_type = %s AND status IN ('active','paused','processing')"
                params: List[Any] = [client_id, session_type]
                if session_type == "journey_preparation":
                    query += " AND journey_id = %s"
                    params.append(journey_id)
                query += " ORDER BY last_activity_at DESC LIMIT 1 FOR UPDATE"
                cur.execute(query, tuple(params))
                found = cur.fetchone()
                if found:
                    conn.commit()
                    return {**_engagement_row(found), "created": False, "resumed": True, "disposition": "created_or_resumed"}
                cur.execute(
                    """
                    INSERT INTO consultation_sessions
                      (id, client_id, session_type, journey_type, journey_id, trigger_source,
                       baseline_snapshot_version, companion_session_id, status, metadata,
                       updated_at, last_activity_at, paused_at, lifecycle_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'paused',%s::jsonb,NOW(),NOW(),NOW(),1)
                    """,
                    (engagement_id, client_id, session_type, journey_type, journey_id,
                     trigger_source, baseline_snapshot_version, companion_session_id,
                     json.dumps({"created_by_request_id": client_request_id} if client_request_id else {})),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)
    _event(event_type="consultation.lifecycle_changed", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation:{engagement_id}:1", payload={"from": None, "to": "paused", "reason": "created", "request_id": client_request_id, "lifecycle_version": 1})
    return {**get_consultation_engagement(engagement_id=engagement_id, client_id=client_id), "created": True, "resumed": False, "disposition": "created_or_resumed"}  # type: ignore[arg-type]


def get_consultation_engagement(*, engagement_id: str, client_id: str) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = _ENGAGEMENTS.get(engagement_id)
            return _copy(row) if row and row["client_id"] == client_id else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(_ENGAGEMENT_SELECT + " WHERE id = %s AND client_id = %s", (engagement_id, client_id))
            row = cur.fetchone()
            return _engagement_row(row) if row else None
    finally:
        pool.putconn(conn)


def get_active_consultation(*, client_id: str, session_type: str, journey_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            rows = [row for row in _ENGAGEMENTS.values() if row["client_id"] == client_id and row["session_type"] == session_type and row.get("journey_id") == journey_id and row["status"] in _OPEN]
            return _copy(sorted(rows, key=lambda row: row["last_activity_at"], reverse=True)[0]) if rows else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            query = _ENGAGEMENT_SELECT + " WHERE client_id = %s AND session_type = %s AND status IN ('active','paused','processing')"
            params: List[Any] = [client_id, session_type]
            if session_type == "journey_preparation":
                query += " AND journey_id = %s"
                params.append(journey_id)
            query += " ORDER BY last_activity_at DESC LIMIT 1"
            cur.execute(query, tuple(params))
            row = cur.fetchone()
            return _engagement_row(row) if row else None
    finally:
        pool.putconn(conn)


def begin_or_renew_interaction(
    *, engagement_id: str, client_id: str, client_interaction_id: str,
    companion_session_id: str, channel: str, lease_seconds: int = 90,
) -> Dict[str, Any]:
    if channel not in {"voice", "text"}:
        raise ValueError("invalid_interaction_channel")
    uuid.UUID(str(client_interaction_id))
    expires = _now() + timedelta(seconds=max(30, min(int(lease_seconds), 300)))
    pool = _get_pool()
    transition = None
    if pool is None:
        with _LOCK:
            engagement = _owned_open_memory(engagement_id, client_id)
            existing = next((row for row in _INTERACTIONS.values() if row["consultation_engagement_id"] == engagement_id and row["client_interaction_id"] == client_interaction_id), None)
            if existing:
                if existing["companion_session_id"] != companion_session_id or existing["channel"] != channel:
                    raise ValueError("interaction_idempotency_conflict")
                if existing["status"] != "active":
                    return {**_copy(existing), "activity_changed": False, "engagement": _copy(engagement)}
                existing.update({"lease_expires_at": expires.isoformat(), "last_activity_at": _now().isoformat(), "interaction_version": existing["interaction_version"] + 1})
                interaction = existing
                event_kind = "renewed"
            else:
                interaction_id = str(uuid.uuid4())
                now = _now().isoformat()
                interaction = {"interaction_id": interaction_id, "consultation_engagement_id": engagement_id, "client_interaction_id": client_interaction_id, "companion_session_id": companion_session_id, "channel": channel, "status": "active", "lease_expires_at": expires.isoformat(), "last_activity_at": now, "started_at": now, "ended_at": None, "end_reason": None, "interaction_version": 1, "checkpoint_payload": None, "checkpoint_client_turn_id": None}
                _INTERACTIONS[interaction_id] = interaction
                event_kind = "started"
            changed = engagement["status"] != "active"
            if changed:
                transition = (engagement["status"], "active")
                engagement.update({"status": "active", "paused_at": None, "lifecycle_version": engagement["lifecycle_version"] + 1})
            engagement.update({"updated_at": _now().isoformat(), "last_activity_at": _now().isoformat()})
            result = {**_copy(interaction), "activity_changed": changed, "engagement": _copy(engagement)}
    else:
        result, transition, event_kind = _pg_begin_or_renew(pool, engagement_id, client_id, client_interaction_id, companion_session_id, channel, expires)
    interaction = result
    _event(event_type=f"consultation.interaction_{event_kind}", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation-interaction:{interaction['interaction_id']}:{interaction['interaction_version']}", payload={"interaction_id": interaction["interaction_id"], "interaction_version": interaction["interaction_version"], "other_live_interactions": False})
    if transition:
        version = result["engagement"]["lifecycle_version"]
        _event(event_type="consultation.lifecycle_changed", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation:{engagement_id}:{version}", payload={"from": transition[0], "to": transition[1], "reason": "interaction_active", "lifecycle_version": version})
    return result


def _pg_begin_or_renew(pool: Any, engagement_id: str, client_id: str, client_interaction_id: str, companion_session_id: str, channel: str, expires: datetime):
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(_ENGAGEMENT_SELECT + " WHERE id = %s AND client_id = %s FOR UPDATE", (engagement_id, client_id))
                raw = cur.fetchone()
                if not raw:
                    raise LookupError("consultation_not_found")
                engagement = _engagement_row(raw)
                if engagement["status"] not in _OPEN:
                    raise ValueError("consultation_terminal")
                cur.execute(_INTERACTION_SELECT + " WHERE consultation_id = %s AND client_interaction_id = %s FOR UPDATE", (engagement_id, client_interaction_id))
                current = cur.fetchone()
                if current:
                    interaction = _interaction_row(current)
                    if interaction["companion_session_id"] != companion_session_id or interaction["channel"] != channel:
                        raise ValueError("interaction_idempotency_conflict")
                    if interaction["status"] == "active":
                        cur.execute("UPDATE consultation_interactions SET lease_expires_at=%s,last_activity_at=NOW(),interaction_version=interaction_version+1 WHERE id=%s RETURNING id", (expires, interaction["interaction_id"]))
                        event_kind = "renewed"
                    else:
                        conn.commit()
                        return ({**interaction, "activity_changed": False, "engagement": engagement}, None, "ended")
                else:
                    interaction_id = str(uuid.uuid4())
                    cur.execute("INSERT INTO consultation_interactions (id,consultation_id,client_interaction_id,companion_session_id,channel,lease_expires_at) VALUES (%s,%s,%s,%s,%s,%s)", (interaction_id, engagement_id, client_interaction_id, companion_session_id, channel, expires))
                    event_kind = "started"
                old_status = engagement["status"]
                changed = old_status != "active"
                if changed:
                    cur.execute("UPDATE consultation_sessions SET status='active',paused_at=NULL,updated_at=NOW(),last_activity_at=NOW(),lifecycle_version=lifecycle_version+1 WHERE id=%s", (engagement_id,))
                else:
                    cur.execute("UPDATE consultation_sessions SET updated_at=NOW(),last_activity_at=NOW() WHERE id=%s", (engagement_id,))
                _set_pg_planning_projection(cur, client_id)
                cur.execute(_INTERACTION_SELECT + " WHERE consultation_id=%s AND client_interaction_id=%s", (engagement_id, client_interaction_id))
                interaction = _interaction_row(cur.fetchone())
                cur.execute(_ENGAGEMENT_SELECT + " WHERE id=%s", (engagement_id,))
                authoritative = _engagement_row(cur.fetchone())
            conn.commit()
            return ({**interaction, "activity_changed": changed, "engagement": authoritative}, (old_status, "active") if changed else None, event_kind)
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def heartbeat_interaction(*, engagement_id: str, client_id: str, interaction_id: str, expected_version: int, lease_seconds: int = 90) -> Dict[str, Any]:
    current = get_interaction(engagement_id=engagement_id, client_id=client_id, interaction_id=interaction_id)
    if not current:
        raise LookupError("interaction_not_found")
    if current["interaction_version"] != expected_version:
        raise ValueError("interaction_version_conflict")
    return begin_or_renew_interaction(engagement_id=engagement_id, client_id=client_id, client_interaction_id=current["client_interaction_id"], companion_session_id=current["companion_session_id"], channel=current["channel"], lease_seconds=lease_seconds)


def get_interaction(*, engagement_id: str, client_id: str, interaction_id: str) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            engagement = _ENGAGEMENTS.get(engagement_id)
            row = _INTERACTIONS.get(interaction_id)
            return _copy(row) if engagement and engagement["client_id"] == client_id and row and row["consultation_engagement_id"] == engagement_id else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM consultation_sessions WHERE id=%s AND client_id=%s", (engagement_id, client_id))
            if not cur.fetchone():
                return None
            cur.execute(_INTERACTION_SELECT + " WHERE id=%s AND consultation_id=%s", (interaction_id, engagement_id))
            row = cur.fetchone()
            return _interaction_row(row) if row else None
    finally:
        pool.putconn(conn)


def end_interaction(*, engagement_id: str, client_id: str, interaction_id: str, end_reason: str, expected_version: Optional[int] = None) -> Dict[str, Any]:
    pool = _get_pool()
    transition = None
    if pool is None:
        with _LOCK:
            engagement = _owned_open_memory(engagement_id, client_id)
            interaction = _INTERACTIONS.get(interaction_id)
            if not interaction or interaction["consultation_engagement_id"] != engagement_id:
                raise LookupError("interaction_not_found")
            if interaction["status"] != "active":
                return {**_copy(interaction), "activity_changed": False, "engagement": _copy(engagement)}
            if expected_version is not None and interaction["interaction_version"] != expected_version:
                raise ValueError("interaction_version_conflict")
            now = _now().isoformat()
            interaction.update({"status": "ended", "ended_at": now, "end_reason": end_reason, "last_activity_at": now, "interaction_version": interaction["interaction_version"] + 1})
            live = any(row["consultation_engagement_id"] == engagement_id and row["status"] == "active" and datetime.fromisoformat(row["lease_expires_at"]) > _now() for row in _INTERACTIONS.values())
            changed = not live and engagement["status"] == "active"
            if changed:
                transition = ("active", "paused")
                engagement.update({"status": "paused", "paused_at": now, "lifecycle_version": engagement["lifecycle_version"] + 1})
            engagement.update({"updated_at": now, "last_activity_at": now})
            result = {**_copy(interaction), "activity_changed": changed, "other_live_interactions": live, "engagement": _copy(engagement)}
    else:
        result, transition = _pg_end(pool, engagement_id, client_id, interaction_id, end_reason, expected_version, expired=False)
    _event(event_type="consultation.interaction_ended", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation-interaction:{interaction_id}:{result['interaction_version']}", payload={"interaction_id": interaction_id, "interaction_version": result["interaction_version"], "other_live_interactions": result["other_live_interactions"], "reason": end_reason})
    if transition:
        version = result["engagement"]["lifecycle_version"]
        _event(event_type="consultation.lifecycle_changed", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation:{engagement_id}:{version}", payload={"from": "active", "to": "paused", "reason": "final_interaction_ended", "lifecycle_version": version})
    return result


def _pg_end(pool: Any, engagement_id: str, client_id: str, interaction_id: str, reason: str, expected_version: Optional[int], *, expired: bool):
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(_ENGAGEMENT_SELECT + " WHERE id=%s AND client_id=%s FOR UPDATE", (engagement_id, client_id))
                raw_engagement = cur.fetchone()
                if not raw_engagement:
                    raise LookupError("consultation_not_found")
                engagement = _engagement_row(raw_engagement)
                cur.execute(_INTERACTION_SELECT + " WHERE id=%s AND consultation_id=%s FOR UPDATE", (interaction_id, engagement_id))
                raw_interaction = cur.fetchone()
                if not raw_interaction:
                    raise LookupError("interaction_not_found")
                interaction = _interaction_row(raw_interaction)
                if interaction["status"] != "active":
                    conn.commit()
                    return ({**interaction, "activity_changed": False, "other_live_interactions": False, "engagement": engagement}, None)
                if expected_version is not None and interaction["interaction_version"] != expected_version:
                    raise ValueError("interaction_version_conflict")
                status = "expired" if expired else "ended"
                cur.execute("UPDATE consultation_interactions SET status=%s,ended_at=NOW(),end_reason=%s,last_activity_at=NOW(),interaction_version=interaction_version+1 WHERE id=%s", (status, reason, interaction_id))
                cur.execute("SELECT EXISTS(SELECT 1 FROM consultation_interactions WHERE consultation_id=%s AND status='active' AND lease_expires_at>NOW())", (engagement_id,))
                live = bool(cur.fetchone()[0])
                changed = not live and engagement["status"] == "active"
                if changed:
                    cur.execute("UPDATE consultation_sessions SET status='paused',paused_at=NOW(),updated_at=NOW(),last_activity_at=NOW(),lifecycle_version=lifecycle_version+1 WHERE id=%s", (engagement_id,))
                else:
                    cur.execute("UPDATE consultation_sessions SET updated_at=NOW(),last_activity_at=NOW() WHERE id=%s", (engagement_id,))
                _set_pg_planning_projection(cur, client_id)
                cur.execute(_INTERACTION_SELECT + " WHERE id=%s", (interaction_id,))
                interaction = _interaction_row(cur.fetchone())
                cur.execute(_ENGAGEMENT_SELECT + " WHERE id=%s", (engagement_id,))
                authoritative = _engagement_row(cur.fetchone())
            conn.commit()
            return ({**interaction, "activity_changed": changed, "other_live_interactions": live, "engagement": authoritative}, ("active", "paused") if changed else None)
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def checkpoint_interaction(*, engagement_id: str, client_id: str, interaction_id: str, client_turn_id: str, reason: str, transcript: Dict[str, Any]) -> Dict[str, Any]:
    uuid.UUID(str(client_turn_id))
    pool = _get_pool()
    payload = {"reason": reason, "transcript": transcript}
    if pool is None:
        with _LOCK:
            _owned_open_memory(engagement_id, client_id)
            row = _INTERACTIONS.get(interaction_id)
            if not row or row["consultation_engagement_id"] != engagement_id:
                raise LookupError("interaction_not_found")
            if row.get("checkpoint_client_turn_id"):
                if row["checkpoint_client_turn_id"] != client_turn_id or row.get("checkpoint_payload") != payload:
                    raise ValueError("checkpoint_idempotency_conflict")
                return _copy(row)
            row.update({"checkpoint_client_turn_id": client_turn_id, "checkpoint_payload": payload, "interaction_version": row["interaction_version"] + 1})
            engagement = _ENGAGEMENTS[engagement_id]
            prior = list(engagement.get("transcript", {}).get("checkpoints") or [])
            prior.append({"interaction_id": interaction_id, "client_turn_id": client_turn_id, **payload})
            engagement["transcript"] = {"checkpoints": prior}
            return _copy(row)
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM consultation_sessions WHERE id=%s AND client_id=%s FOR UPDATE", (engagement_id, client_id))
                if not cur.fetchone():
                    raise LookupError("consultation_not_found")
                cur.execute(_INTERACTION_SELECT + " WHERE id=%s AND consultation_id=%s FOR UPDATE", (interaction_id, engagement_id))
                raw = cur.fetchone()
                if not raw:
                    raise LookupError("interaction_not_found")
                current = _interaction_row(raw)
                if current.get("checkpoint_client_turn_id"):
                    if current["checkpoint_client_turn_id"] != client_turn_id or current.get("checkpoint_payload") != payload:
                        raise ValueError("checkpoint_idempotency_conflict")
                    conn.commit()
                    return current
                cur.execute("UPDATE consultation_interactions SET checkpoint_payload=%s::jsonb,checkpoint_client_turn_id=%s,interaction_version=interaction_version+1,last_activity_at=NOW() WHERE id=%s", (json.dumps(payload), client_turn_id, interaction_id))
                cur.execute("UPDATE consultation_sessions SET transcript=jsonb_set(COALESCE(transcript,'{}'::jsonb),'{checkpoints}',COALESCE(transcript->'checkpoints','[]'::jsonb)||%s::jsonb,true),updated_at=NOW(),last_activity_at=NOW() WHERE id=%s", (json.dumps([{"interaction_id": interaction_id, "client_turn_id": client_turn_id, **payload}]), engagement_id))
                cur.execute(_INTERACTION_SELECT + " WHERE id=%s", (interaction_id,))
                result = _interaction_row(cur.fetchone())
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def complete_consultation_from_objective(*, engagement_id: str, client_id: str, onboarding_transition_ok: bool, objective_status: str) -> Dict[str, Any]:
    if not onboarding_transition_ok or objective_status != "complete":
        raise ValueError("onboarding_completion_not_authoritative")
    pool = _get_pool()
    transition = None
    if pool is None:
        with _LOCK:
            row = _ENGAGEMENTS.get(engagement_id)
            if not row or row["client_id"] != client_id:
                raise LookupError("consultation_not_found")
            if row["status"] == "completed":
                return _copy(row)
            if row["status"] not in _OPEN:
                raise ValueError("consultation_terminal")
            old = row["status"]
            now = _now().isoformat()
            row.update({"status": "completed", "completed_at": now, "updated_at": now, "last_activity_at": now, "lifecycle_version": row["lifecycle_version"] + 1})
            result = _copy(row)
            transition = old
    else:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(_ENGAGEMENT_SELECT + " WHERE id=%s AND client_id=%s FOR UPDATE", (engagement_id, client_id))
                raw = cur.fetchone()
                if not raw:
                    raise LookupError("consultation_not_found")
                row = _engagement_row(raw)
                if row["status"] == "completed":
                    conn.commit()
                    return row
                if row["status"] not in _OPEN:
                    raise ValueError("consultation_terminal")
                transition = row["status"]
                cur.execute("UPDATE consultation_interactions SET status='ended',ended_at=NOW(),end_reason='onboarding_completed',interaction_version=interaction_version+1 WHERE consultation_id=%s AND status='active'", (engagement_id,))
                cur.execute("UPDATE consultation_sessions SET status='completed',completed_at=NOW(),updated_at=NOW(),last_activity_at=NOW(),lifecycle_version=lifecycle_version+1 WHERE id=%s", (engagement_id,))
                _set_pg_planning_projection(cur, client_id)
                cur.execute(_ENGAGEMENT_SELECT + " WHERE id=%s", (engagement_id,))
                result = _engagement_row(cur.fetchone())
            conn.commit()
        finally:
            pool.putconn(conn)
    _event(event_type="consultation.lifecycle_changed", client_id=client_id, engagement_id=engagement_id, event_key=f"consultation:{engagement_id}:{result['lifecycle_version']}", payload={"from": transition, "to": "completed", "reason": "advisor_onboarding_complete", "lifecycle_version": result["lifecycle_version"]})
    return result


def expire_interaction_leases(*, limit: int = 100) -> List[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            targets = [(row["consultation_engagement_id"], _ENGAGEMENTS[row["consultation_engagement_id"]]["client_id"], row["interaction_id"]) for row in _INTERACTIONS.values() if row["status"] == "active" and datetime.fromisoformat(row["lease_expires_at"]) <= _now()][:limit]
    else:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT i.consultation_id,c.client_id,i.id FROM consultation_interactions i JOIN consultation_sessions c ON c.id=i.consultation_id WHERE i.status='active' AND i.lease_expires_at<=NOW() ORDER BY i.lease_expires_at LIMIT %s", (limit,))
                targets = [(str(row[0]), row[1], str(row[2])) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    results = []
    for engagement_id, client_id, interaction_id in targets:
        if pool is None:
            result = end_interaction(engagement_id=engagement_id, client_id=client_id, interaction_id=interaction_id, end_reason="lease_expired")
        else:
            result, _ = _pg_end(pool, engagement_id, client_id, interaction_id, "lease_expired", None, expired=True)
        results.append(result)
    return results


def _set_pg_planning_projection(cur: Any, client_id: str) -> None:
    cur.execute(
        """
        INSERT INTO planning_refresh_state (client_id, consultation_active)
        VALUES (%s, EXISTS(
          SELECT 1 FROM consultation_interactions i
          JOIN consultation_sessions c ON c.id=i.consultation_id
          WHERE c.client_id=%s AND i.status='active' AND i.lease_expires_at>NOW()
        ))
        ON CONFLICT (client_id) DO UPDATE SET consultation_active=EXCLUDED.consultation_active, updated_at=NOW()
        """,
        (client_id, client_id),
    )


def _owned_open_memory(engagement_id: str, client_id: str) -> Dict[str, Any]:
    row = _ENGAGEMENTS.get(engagement_id)
    if not row or row["client_id"] != client_id:
        raise LookupError("consultation_not_found")
    if row["status"] not in _OPEN:
        raise ValueError("consultation_terminal")
    return row


def _reset_memory_consultation_lifecycle_for_tests() -> None:
    with _LOCK:
        _ENGAGEMENTS.clear()
        _INTERACTIONS.clear()
