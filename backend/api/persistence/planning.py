"""Durable planning refresh state and atomic artifact-set publication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, Optional
import uuid

from .core import _get_pool, _pg_json, _safe_getconn


_memory_lock = RLock()
_memory_refresh: Dict[str, Dict[str, Any]] = {}
_memory_sets: Dict[str, Dict[str, Any]] = {}
_memory_current: Dict[str, str] = {}


def request_planning_refresh_in_transaction(cur: Any, *, client_id: str, version: int) -> str:
    """Record refresh intent and its canonical input identity in the fact UoW."""

    cur.execute(
        "SELECT entity_id,fact_type,value,provenance FROM canonical_client_facts WHERE client_id=%s ORDER BY entity_id",
        (client_id,),
    )
    rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    ledger = [
        {"entity_id": row[0], "fact_type": row[1], "value": row[2], "provenance": row[3]}
        for row in rows
    ]
    fingerprint = financial_input_fingerprint(
        client_id=client_id,
        version=version,
        typed_facts=ledger,
    )
    cur.execute(
        """
        INSERT INTO planning_refresh_state
          (client_id,latest_requested_version,latest_requested_input_fingerprint,
           dirty,rerun_required,status,not_before,updated_at)
        VALUES (%s,%s,%s,TRUE,FALSE,
          CASE WHEN EXISTS(
            SELECT 1 FROM consultation_interactions i
            JOIN consultation_sessions c ON c.id=i.consultation_id
            WHERE c.client_id=%s AND i.status='active' AND i.lease_expires_at>NOW()
          ) THEN 'deferred' ELSE 'pending' END,
          NOW(),NOW())
        ON CONFLICT (client_id) DO UPDATE SET
          latest_requested_version=GREATEST(planning_refresh_state.latest_requested_version,EXCLUDED.latest_requested_version),
          latest_requested_input_fingerprint=CASE WHEN EXCLUDED.latest_requested_version>=planning_refresh_state.latest_requested_version THEN EXCLUDED.latest_requested_input_fingerprint ELSE planning_refresh_state.latest_requested_input_fingerprint END,
          dirty=TRUE,
          rerun_required=planning_refresh_state.running_version IS NOT NULL AND EXCLUDED.latest_requested_version>planning_refresh_state.running_version,
          status=CASE WHEN planning_refresh_state.running_version IS NOT NULL THEN planning_refresh_state.status WHEN planning_refresh_state.consultation_active THEN 'deferred' ELSE 'pending' END,
          not_before=NOW(),attempt_count=CASE WHEN EXCLUDED.latest_requested_version>planning_refresh_state.latest_requested_version THEN 0 ELSE planning_refresh_state.attempt_count END,
          updated_at=NOW()
        """,
        (client_id, int(version), fingerprint, client_id),
    )
    return fingerprint


def financial_input_fingerprint(
    *,
    client_id: str,
    version: int,
    typed_facts: list[Dict[str, Any]],
) -> str:
    """Use the same resolver identity at refresh request and publication."""

    from client_file.financial_position import resolve_financial_position

    position = resolve_financial_position(
        client_id=client_id,
        client_file={
            "client_file_version": int(version),
            "typed_facts": typed_facts,
        },
    )
    return str(position["source_input_fingerprint"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_planning_refresh_state(*, client_id: str) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            return dict(_memory_refresh.get(client_id) or {
                "client_id": client_id,
                "latest_requested_version": 0,
                "running_version": None,
                "published_version": 0,
                "consultation_active": False,
                "dirty": False,
                "rerun_required": False,
                "status": "idle",
                "active_job_id": None,
                "lease_expires_at": None,
                "attempt_count": 0,
                "last_error": None,
                "latest_requested_input_fingerprint": None,
                "running_input_fingerprint": None,
                "published_input_fingerprint": None,
            })
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT client_id, latest_requested_version, running_version,
                       published_version, consultation_active, dirty,
                       rerun_required, not_before, status, active_job_id, updated_at,
                       lease_expires_at, attempt_count, last_error, last_started_at,
                       last_finished_at, latest_requested_input_fingerprint,
                       running_input_fingerprint, published_input_fingerprint
                FROM planning_refresh_state WHERE client_id = %s
                """,
                (client_id,),
            )
            row = cur.fetchone()
            if row is None:
                return _empty_refresh(client_id)
            columns = [item[0] for item in cur.description]
            return dict(zip(columns, row))
    finally:
        pool.putconn(conn)


def _empty_refresh(client_id: str) -> Dict[str, Any]:
    return {
        "client_id": client_id,
        "latest_requested_version": 0,
        "running_version": None,
        "published_version": 0,
        "consultation_active": False,
        "dirty": False,
        "rerun_required": False,
        "status": "idle",
        "active_job_id": None,
        "lease_expires_at": None,
        "attempt_count": 0,
        "last_error": None,
        "latest_requested_input_fingerprint": None,
        "running_input_fingerprint": None,
        "published_input_fingerprint": None,
    }


def request_planning_refresh(*, client_id: str, version: int, source_input_fingerprint: Optional[str] = None) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            prior_version = int(state["latest_requested_version"])
            state["latest_requested_version"] = max(int(version), prior_version)
            if int(version) >= prior_version:
                existing = state.get("latest_requested_input_fingerprint")
                if int(version) == prior_version and existing and source_input_fingerprint and existing != source_input_fingerprint:
                    raise ValueError("planning_input_fingerprint_conflict")
                state["latest_requested_input_fingerprint"] = source_input_fingerprint or existing
            state["dirty"] = True
            if (
                state.get("running_version") is not None
                and int(version) > int(state.get("running_version") or 0)
            ):
                state["rerun_required"] = True
            elif not state.get("consultation_active"):
                state["status"] = "deferred" if state.get("consultation_active") else "pending"
            state["updated_at"] = _now()
            return dict(state)
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO planning_refresh_state
                    (client_id, latest_requested_version, latest_requested_input_fingerprint,
                     dirty, status, not_before)
                VALUES (%s, %s, %s, TRUE, 'pending', NOW())
                ON CONFLICT (client_id) DO UPDATE SET
                    latest_requested_version = GREATEST(
                        planning_refresh_state.latest_requested_version,
                        EXCLUDED.latest_requested_version
                    ),
                    dirty = TRUE,
                    latest_requested_input_fingerprint = CASE
                        WHEN EXCLUDED.latest_requested_version >= planning_refresh_state.latest_requested_version
                        THEN COALESCE(EXCLUDED.latest_requested_input_fingerprint, planning_refresh_state.latest_requested_input_fingerprint)
                        ELSE planning_refresh_state.latest_requested_input_fingerprint END,
                    rerun_required = planning_refresh_state.running_version IS NOT NULL
                        AND GREATEST(
                            planning_refresh_state.latest_requested_version,
                            EXCLUDED.latest_requested_version
                        ) > planning_refresh_state.running_version,
                    status = CASE
                        WHEN planning_refresh_state.running_version IS NULL
                        THEN CASE WHEN planning_refresh_state.consultation_active THEN 'deferred' ELSE 'pending' END
                        ELSE planning_refresh_state.status
                    END,
                    not_before = NOW(),
                    updated_at = NOW()
                RETURNING client_id, latest_requested_version, running_version,
                          published_version, consultation_active, dirty,
                          rerun_required, status, active_job_id
                """,
                (client_id, int(version), source_input_fingerprint),
            )
            row = cur.fetchone()
            columns = [item[0] for item in cur.description]
        conn.commit()
        return dict(zip(columns, row))
    finally:
        pool.putconn(conn)


def set_planning_consultation_active(*, client_id: str, active: bool) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            state["consultation_active"] = bool(active)
            if not active and state.get("dirty") and state.get("running_version") is None:
                state["status"] = "pending"
            state["updated_at"] = _now()
            return dict(state)
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO planning_refresh_state (client_id, consultation_active)
                VALUES (%s, %s)
                ON CONFLICT (client_id) DO UPDATE SET
                    consultation_active = EXCLUDED.consultation_active,
                    status = CASE
                        WHEN NOT EXCLUDED.consultation_active
                             AND planning_refresh_state.dirty
                             AND planning_refresh_state.running_version IS NULL THEN 'pending'
                        ELSE planning_refresh_state.status
                    END,
                    updated_at = NOW()
                RETURNING client_id, latest_requested_version, running_version,
                          published_version, consultation_active, dirty,
                          rerun_required, status, active_job_id
                """,
                (client_id, bool(active)),
            )
            row = cur.fetchone()
            columns = [item[0] for item in cur.description]
        conn.commit()
        return dict(zip(columns, row))
    finally:
        pool.putconn(conn)


def reserve_planning_refreshes(
    *, limit: int = 20, client_id: Optional[str] = None, lease_seconds: int = 60,
) -> list[Dict[str, Any]]:
    """Claim eligible or expired durable refresh work as queued jobs."""

    bounded = max(1, min(int(limit), 100))
    now = datetime.now(timezone.utc)
    pool = _get_pool()
    if pool is None:
        reservations = []
        with _memory_lock:
            states = [state for key, state in _memory_refresh.items() if client_id is None or key == client_id]
            for state in states:
                if len(reservations) >= bounded:
                    break
                expiry = state.get("lease_expires_at")
                expired = bool(expiry and datetime.fromisoformat(str(expiry)) <= now)
                eligible = state.get("status") in {"pending", "deferred"} or (state.get("status") in {"queued", "running"} and expired)
                if not state.get("dirty") or state.get("consultation_active") or not eligible:
                    if state.get("consultation_active") and state.get("dirty") and state.get("status") in {"pending", "deferred"}:
                        state["status"] = "deferred"
                    continue
                if state.get("status") == "running" and expired:
                    for artifact_set in _memory_sets.values():
                        if artifact_set.get("active_job_id") == state.get("active_job_id") and artifact_set.get("status") == "running":
                            artifact_set.update(status="failed", error="planning_refresh_lease_expired")
                job_id = str(uuid.uuid4())
                state.update(
                    status="queued", running_version=int(state.get("latest_requested_version") or 0),
                    active_job_id=job_id, lease_expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                    rerun_required=False, last_error=None, updated_at=now.isoformat(),
                )
                reservations.append({"client_id": state["client_id"], "source_client_version": state["running_version"], "active_job_id": job_id, "lease_expires_at": state["lease_expires_at"]})
        return reservations

    conn = _safe_getconn(pool)
    try:
        try:
            reservations = []
            with conn.cursor() as cur:
                # Repair the cached activity projection before claiming. The
                # interaction lease rows, not consultation status, are authoritative.
                condition = "AND state.client_id = %s" if client_id else ""
                params: list[Any] = [client_id] if client_id else []
                cur.execute(
                    f"""
                    UPDATE planning_refresh_state state SET
                      consultation_active = EXISTS(
                        SELECT 1 FROM consultation_interactions i
                        JOIN consultation_sessions c ON c.id=i.consultation_id
                        WHERE c.client_id=state.client_id AND i.status='active' AND i.lease_expires_at>NOW()
                      ),
                      status = CASE WHEN state.dirty AND NOT EXISTS(
                        SELECT 1 FROM consultation_interactions i
                        JOIN consultation_sessions c ON c.id=i.consultation_id
                        WHERE c.client_id=state.client_id AND i.status='active' AND i.lease_expires_at>NOW()
                      ) AND state.status='deferred' THEN 'pending' ELSE state.status END,
                      updated_at=NOW()
                    WHERE state.dirty {condition}
                    """,
                    tuple(params),
                )
                cur.execute(
                    f"""
                    SELECT client_id, status, active_job_id
                    FROM planning_refresh_state
                    WHERE dirty=TRUE AND consultation_active=FALSE
                      AND (status IN ('pending','deferred') OR
                           (status IN ('queued','running') AND lease_expires_at<=NOW()))
                      AND (not_before IS NULL OR not_before<=NOW())
                      {"AND client_id = %s" if client_id else ""}
                    ORDER BY updated_at
                    FOR UPDATE SKIP LOCKED LIMIT %s
                    """,
                    tuple(([client_id] if client_id else []) + [bounded]),
                )
                candidates = cur.fetchall()
                for candidate_client_id, prior_status, prior_job_id in candidates:
                    if prior_status == "running" and prior_job_id:
                        cur.execute("UPDATE planning_artifact_sets SET status='failed',error='planning_refresh_lease_expired',updated_at=NOW() WHERE active_job_id=%s AND status='running'", (prior_job_id,))
                    job_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        UPDATE planning_refresh_state SET
                          status='queued', running_version=latest_requested_version,
                          active_job_id=%s, lease_expires_at=NOW()+(%s * INTERVAL '1 second'),
                          rerun_required=FALSE, last_error=NULL, updated_at=NOW()
                        WHERE client_id=%s
                        RETURNING running_version, lease_expires_at
                        """,
                        (job_id, max(15, int(lease_seconds)), candidate_client_id),
                    )
                    version, lease_expires_at = cur.fetchone()
                    reservations.append({"client_id": candidate_client_id, "source_client_version": int(version), "active_job_id": job_id, "lease_expires_at": lease_expires_at.isoformat()})
            conn.commit()
            return reservations
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def begin_planning_refresh(*, client_id: str, version: int, job_id: str, lease_seconds: int = 300) -> Dict[str, Any]:
    """CAS queued -> running; duplicate deliveries never rerun models."""

    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            if state.get("active_job_id") != job_id or int(state.get("running_version") or 0) != int(version):
                return {**state, "started": False, "reason": "stale_job"}
            if state.get("status") == "running":
                return {**state, "started": False, "reason": "already_running"}
            if state.get("status") != "queued":
                return {**state, "started": False, "reason": "not_queued"}
            state.update(status="running", attempt_count=int(state.get("attempt_count") or 0) + 1, last_started_at=_now(), lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(), running_input_fingerprint=state.get("latest_requested_input_fingerprint"), updated_at=_now())
            return {**state, "started": True}
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE planning_refresh_state SET status='running',
                  attempt_count=attempt_count+1, last_started_at=NOW(),
                  lease_expires_at=NOW()+(%s * INTERVAL '1 second'),
                  running_input_fingerprint=latest_requested_input_fingerprint,
                  updated_at=NOW()
                WHERE client_id=%s AND running_version=%s AND active_job_id=%s AND status='queued'
                RETURNING running_version, attempt_count, running_input_fingerprint
                """,
                (max(30, int(lease_seconds)), client_id, int(version), job_id),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT status,active_job_id,running_version FROM planning_refresh_state WHERE client_id=%s", (client_id,))
                current = cur.fetchone()
                conn.rollback()
                return {"started": False, "reason": "already_running" if current and current[0] == "running" and current[1] == job_id else "stale_job"}
        conn.commit()
        return {"started": True, "version": int(row[0]), "attempt_count": int(row[1]), "source_input_fingerprint": row[2]}
    finally:
        pool.putconn(conn)


def renew_planning_refresh_lease(*, client_id: str, version: int, job_id: str, lease_seconds: int = 300) -> bool:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.get(client_id)
            if not state or state.get("status") != "running" or state.get("active_job_id") != job_id or int(state.get("running_version") or 0) != int(version):
                return False
            state["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
            return True
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE planning_refresh_state SET lease_expires_at=NOW()+(%s * INTERVAL '1 second'),updated_at=NOW() WHERE client_id=%s AND running_version=%s AND active_job_id=%s AND status='running'", (max(30, int(lease_seconds)), client_id, int(version), job_id))
            changed = cur.rowcount == 1
        conn.commit()
        return changed
    finally:
        pool.putconn(conn)


def release_planning_refresh_claim(*, client_id: str, version: int, job_id: str, error: str, retryable: bool = True, blocked: bool = False) -> Dict[str, Any]:
    pool = _get_pool()
    target_status = "blocked" if blocked else "pending" if retryable else "failed"
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            if state.get("active_job_id") != job_id or int(state.get("running_version") or 0) != int(version):
                return {**state, "released": False}
            newer = int(state.get("latest_requested_version") or 0) > int(version)
            state.update(status="pending" if newer else target_status, running_version=None, active_job_id=None, lease_expires_at=None, running_input_fingerprint=None, last_error=error, last_finished_at=_now(), dirty=True, rerun_required=newer, updated_at=_now())
            return {**state, "released": True}
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE planning_refresh_state SET
                  status=CASE WHEN latest_requested_version>%s THEN 'pending' ELSE %s END,
                  running_version=NULL,active_job_id=NULL,lease_expires_at=NULL,
                  running_input_fingerprint=NULL,last_error=%s,last_finished_at=NOW(),
                  dirty=TRUE,rerun_required=latest_requested_version>%s,updated_at=NOW()
                WHERE client_id=%s AND running_version=%s AND active_job_id=%s
                RETURNING status
                """,
                (int(version), target_status, error, int(version), client_id, int(version), job_id),
            )
            row = cur.fetchone()
        conn.commit()
        return {"released": row is not None, "status": row[0] if row else None}
    finally:
        pool.putconn(conn)


def try_start_planning_refresh(*, client_id: str, job_id: Optional[str] = None) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            if state.get("consultation_active"):
                return {**state, "started": False, "reason": "consultation_active"}
            if state.get("running_version") is not None:
                state["rerun_required"] = True
                return {**state, "started": False, "reason": "already_running"}
            version = int(state.get("latest_requested_version") or 0)
            if not state.get("dirty") or version <= int(state.get("published_version") or 0):
                return {**state, "started": False, "reason": "not_dirty"}
            state.update(
                running_version=version,
                rerun_required=False,
                status="running",
                active_job_id=job_id,
                updated_at=_now(),
            )
            return {**state, "started": True, "version": version}
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT consultation_active, running_version, dirty, latest_requested_version, published_version FROM planning_refresh_state WHERE client_id = %s FOR UPDATE", (client_id,))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return {**_empty_refresh(client_id), "started": False, "reason": "not_dirty"}
            active, running, dirty, latest, published = row
            if active:
                conn.rollback()
                return {"started": False, "reason": "consultation_active", "latest_requested_version": int(latest)}
            if running is not None:
                cur.execute("UPDATE planning_refresh_state SET rerun_required = TRUE, updated_at = NOW() WHERE client_id = %s", (client_id,))
                conn.commit()
                return {"started": False, "reason": "already_running", "latest_requested_version": int(latest)}
            if not dirty or int(latest) <= int(published):
                conn.rollback()
                return {"started": False, "reason": "not_dirty", "latest_requested_version": int(latest)}
            cur.execute("UPDATE planning_refresh_state SET running_version = latest_requested_version, rerun_required = FALSE, status = 'running', active_job_id = %s, updated_at = NOW() WHERE client_id = %s RETURNING running_version", (job_id, client_id))
            version = int(cur.fetchone()[0])
        conn.commit()
        return {"started": True, "version": version, "latest_requested_version": version}
    finally:
        pool.putconn(conn)


def finish_planning_refresh(
    *,
    client_id: str,
    version: int,
    published: bool,
    error: Optional[str] = None,
    blocked: bool = False,
) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            if published:
                state["published_version"] = max(int(version), int(state.get("published_version") or 0))
            state["running_version"] = None
            rerun = int(state.get("latest_requested_version") or 0) > int(version)
            state["rerun_required"] = rerun
            state["dirty"] = rerun or (not published and not blocked)
            state["status"] = (
                "pending" if rerun else "blocked" if blocked else "failed" if error else "idle"
            )
            state["active_job_id"] = None
            state["error"] = error
            state["updated_at"] = _now()
            return {**state, "rerun": rerun}
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE planning_refresh_state SET
                    published_version = CASE WHEN %s THEN GREATEST(published_version, %s) ELSE published_version END,
                    running_version = NULL,
                    rerun_required = latest_requested_version > %s,
                    dirty = (latest_requested_version > %s) OR (NOT %s AND NOT %s),
                    status = CASE WHEN latest_requested_version > %s THEN 'pending' WHEN %s THEN 'blocked' WHEN %s IS NOT NULL THEN 'failed' ELSE 'idle' END,
                    active_job_id = NULL,
                    updated_at = NOW()
                WHERE client_id = %s
                RETURNING latest_requested_version, published_version, rerun_required, dirty, status
                """,
                (
                    published,
                    version,
                    version,
                    version,
                    published,
                    blocked,
                    version,
                    blocked,
                    error,
                    client_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return {
            "latest_requested_version": int(row[0]),
            "published_version": int(row[1]),
            "rerun_required": bool(row[2]),
            "dirty": bool(row[3]),
            "status": row[4],
            "rerun": bool(row[2]),
        }
    finally:
        pool.putconn(conn)


def create_planning_artifact_set(
    *, client_id: str, source_version: int, active_job_id: Optional[str] = None,
    source_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    set_id = str(uuid.uuid4())
    record = {
        "id": set_id,
        "client_id": client_id,
        "source_client_version": int(source_version),
        "status": "pending",
        "created_at": _now(),
        "active_job_id": active_job_id,
        "source_snapshot_id": (source_snapshot or {}).get("snapshot_id"),
        "source_input_fingerprint": (source_snapshot or {}).get("source_input_fingerprint"),
        "source_provider_revisions": list((source_snapshot or {}).get("source_provider_revisions") or []),
        "resolver_policy_version": (source_snapshot or {}).get("resolver_policy_version"),
        "source_operand_ledger": list((source_snapshot or {}).get("resolved_operands") or []),
    }
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            existing = next((item for item in _memory_sets.values() if item["client_id"] == client_id and int(item["source_client_version"]) == int(source_version) and item["status"] in {"pending", "running", "ready", "blocked"}), None)
            if existing:
                return dict(existing)
            record["status"] = "running" if active_job_id else "pending"
            _memory_sets[set_id] = record
        return dict(record)
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO planning_artifact_sets
                  (id,client_id,source_client_version,status,active_job_id,
                   source_snapshot_id,source_input_fingerprint,source_provider_revisions,
                   resolver_policy_version,source_operand_ledger)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                ON CONFLICT (client_id, source_client_version)
                  WHERE status IN ('pending','running','ready','blocked') DO NOTHING
                RETURNING id
                """,
                (set_id, client_id, int(source_version), "running" if active_job_id else "pending",
                 active_job_id, record["source_snapshot_id"], record["source_input_fingerprint"],
                 _pg_json(record["source_provider_revisions"]), record["resolver_policy_version"],
                 _pg_json(record["source_operand_ledger"])),
            )
            if cur.fetchone() is None:
                cur.execute("SELECT id,status,active_job_id,source_input_fingerprint FROM planning_artifact_sets WHERE client_id=%s AND source_client_version=%s AND status IN ('pending','running','ready','blocked')", (client_id, int(source_version)))
                existing = cur.fetchone()
                conn.commit()
                return {**record, "id": str(existing[0]), "status": existing[1], "active_job_id": existing[2], "source_input_fingerprint": existing[3]}
        conn.commit()
        return record
    finally:
        pool.putconn(conn)


def fail_planning_artifact_set(*, set_id: str, error: str) -> None:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            if set_id in _memory_sets:
                _memory_sets[set_id].update(status="failed", error=error)
        return
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE planning_artifact_sets SET status = 'failed', error = %s WHERE id = %s", (error, set_id))
        conn.commit()
    finally:
        pool.putconn(conn)


def block_planning_artifact_set(*, set_id: str, question: str) -> None:
    """Keep an input-conflicted set unpublished without recording a failed job."""

    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            if set_id in _memory_sets:
                _memory_sets[set_id].update(status="blocked", error=question)
        return
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE planning_artifact_sets SET status = 'blocked', error = %s WHERE id = %s",
                (question, set_id),
            )
        conn.commit()
    finally:
        pool.putconn(conn)


def publish_planning_artifact_set(*, set_id: str, artifacts: Dict[str, Any]) -> Dict[str, Any]:
    required = {"knowledge", "diagnosis", "projection"}
    if set(artifacts) != required or any(not isinstance(artifacts[key], dict) for key in required):
        raise ValueError("planning_artifact_set_incomplete")
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            record = _memory_sets[set_id]
            client_id = record["client_id"]
            source_version = int(record["source_client_version"])
            state = _memory_refresh.setdefault(client_id, _empty_refresh(client_id))
            record["artifacts"] = {key: dict(value) for key, value in artifacts.items()}
            if int(state.get("latest_requested_version") or 0) != source_version:
                record["status"] = "stale"
                return {**record, "published": False}
            record.update(status="ready", published_at=_now())
            _memory_current[client_id] = set_id
            return {**record, "published": True}

    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT client_id, source_client_version FROM planning_artifact_sets WHERE id = %s FOR UPDATE", (set_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError("planning_artifact_set_not_found")
            client_id, source_version = str(row[0]), int(row[1])
            cur.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_snapshots WHERE client_id = %s", (client_id,))
            knowledge_version = int(cur.fetchone()[0])
            knowledge_id = str(uuid.uuid4())
            cur.execute("INSERT INTO knowledge_snapshots (id, client_id, version, snapshot_data, trigger_event, planning_set_id, source_client_version) VALUES (%s, %s, %s, %s::jsonb, 'planning_refresh', %s, %s)", (knowledge_id, client_id, knowledge_version, _pg_json(artifacts["knowledge"]), set_id, source_version))
            cur.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM diagnosis_snapshots WHERE client_id = %s", (client_id,))
            diagnosis_version = int(cur.fetchone()[0])
            diagnosis_id = str(uuid.uuid4())
            cur.execute("INSERT INTO diagnosis_snapshots (id, client_id, version, knowledge_snapshot_version, diagnosis_data, planning_set_id, source_client_version) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)", (diagnosis_id, client_id, diagnosis_version, knowledge_version, _pg_json(artifacts["diagnosis"]), set_id, source_version))
            projection_id = f"planning_projection_{uuid.uuid4().hex}"
            cur.execute("INSERT INTO mvp_artifacts (id, client_id, record_type, status, payload, planning_set_id, source_client_version) VALUES (%s, %s, 'projection', 'ready', %s::jsonb, %s, %s)", (projection_id, client_id, _pg_json(artifacts["projection"]), set_id, source_version))
            cur.execute("SELECT latest_requested_version FROM planning_refresh_state WHERE client_id = %s FOR UPDATE", (client_id,))
            latest_row = cur.fetchone()
            latest_version = int(latest_row[0]) if latest_row else source_version
            if latest_version != source_version:
                cur.execute("UPDATE planning_artifact_sets SET status = 'stale', knowledge_snapshot_id = %s, diagnosis_snapshot_id = %s, projection_artifact_id = %s WHERE id = %s", (knowledge_id, diagnosis_id, projection_id, set_id))
                conn.commit()
                return {"id": set_id, "status": "stale", "published": False, "source_client_version": source_version}
            cur.execute("UPDATE planning_artifact_sets SET status = 'ready', knowledge_snapshot_id = %s, diagnosis_snapshot_id = %s, projection_artifact_id = %s, published_at = NOW() WHERE id = %s", (knowledge_id, diagnosis_id, projection_id, set_id))
            cur.execute("UPDATE clients SET current_planning_set_id = %s WHERE client_id = %s", (set_id, client_id))
        conn.commit()
        return {"id": set_id, "status": "ready", "published": True, "source_client_version": source_version}
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def publish_planning_refresh(
    *, client_id: str, version: int, job_id: str, set_id: str,
    source_input_fingerprint: Optional[str], artifacts: Dict[str, Any],
) -> Dict[str, Any]:
    """Atomically publish all children and finish the exact leased refresh."""

    required = {"knowledge", "diagnosis", "projection"}
    if set(artifacts) != required or any(not isinstance(artifacts[key], dict) for key in required):
        raise ValueError("planning_artifact_set_incomplete")
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            state = _memory_refresh.get(client_id)
            record = _memory_sets.get(set_id)
            if not state or not record:
                raise ValueError("planning_refresh_not_found")
            owns = state.get("status") == "running" and state.get("active_job_id") == job_id and int(state.get("running_version") or 0) == int(version)
            if not owns:
                return {"id": set_id, "published": False, "status": "stale", "reason": "stale_job"}
            current_fingerprint = state.get("latest_requested_input_fingerprint")
            stale = int(state.get("latest_requested_version") or 0) != int(version) or bool(current_fingerprint and source_input_fingerprint and current_fingerprint != source_input_fingerprint)
            if stale:
                record.update(status="stale", error="planning_inputs_superseded")
                state.update(status="pending", running_version=None, active_job_id=None, lease_expires_at=None, running_input_fingerprint=None, rerun_required=True, dirty=True, updated_at=_now())
                return {"id": set_id, "published": False, "status": "stale", "source_client_version": version}
            evidence = {"source_snapshot_id": record.get("source_snapshot_id"), "source_input_fingerprint": source_input_fingerprint, "source_provider_revisions": record.get("source_provider_revisions") or []}
            record["artifacts"] = {key: {**dict(value), **evidence, "planning_set_id": set_id, "source_client_version": version} for key, value in artifacts.items()}
            record.update(status="ready", published_at=_now())
            _memory_current[client_id] = set_id
            state.update(published_version=version, published_input_fingerprint=source_input_fingerprint, running_version=None, active_job_id=None, lease_expires_at=None, running_input_fingerprint=None, rerun_required=False, dirty=False, status="idle", last_error=None, last_finished_at=_now(), updated_at=_now())
            return {**dict(record), "published": True}

    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT latest_requested_version,latest_requested_input_fingerprint,
                           status,running_version,active_job_id
                    FROM planning_refresh_state WHERE client_id=%s FOR UPDATE
                    """,
                    (client_id,),
                )
                state = cur.fetchone()
                if not state:
                    raise ValueError("planning_refresh_not_found")
                owns = state[2] == "running" and int(state[3] or 0) == int(version) and state[4] == job_id
                if not owns:
                    conn.rollback()
                    return {"id": set_id, "published": False, "status": "stale", "reason": "stale_job"}
                cur.execute("SELECT source_input_fingerprint,source_snapshot_id,source_provider_revisions FROM planning_artifact_sets WHERE id=%s AND client_id=%s AND source_client_version=%s AND active_job_id=%s FOR UPDATE", (set_id, client_id, int(version), job_id))
                artifact_set = cur.fetchone()
                if not artifact_set:
                    raise ValueError("planning_artifact_set_not_found")
                latest_fingerprint = state[1]
                stale = int(state[0]) != int(version) or bool(latest_fingerprint and source_input_fingerprint and latest_fingerprint != source_input_fingerprint)
                if stale:
                    cur.execute("UPDATE planning_artifact_sets SET status='stale',error='planning_inputs_superseded',updated_at=NOW() WHERE id=%s", (set_id,))
                    cur.execute("UPDATE planning_refresh_state SET status='pending',running_version=NULL,active_job_id=NULL,lease_expires_at=NULL,running_input_fingerprint=NULL,rerun_required=TRUE,dirty=TRUE,updated_at=NOW() WHERE client_id=%s AND active_job_id=%s", (client_id, job_id))
                    conn.commit()
                    return {"id": set_id, "published": False, "status": "stale", "source_client_version": version}
                evidence = {"planning_set_id": set_id, "source_client_version": int(version), "source_input_fingerprint": source_input_fingerprint, "source_snapshot_id": artifact_set[1], "source_provider_revisions": list(artifact_set[2] or [])}
                documents = {key: {**dict(value), **evidence} for key, value in artifacts.items()}
                cur.execute("SELECT COALESCE(MAX(version),0)+1 FROM knowledge_snapshots WHERE client_id=%s", (client_id,))
                knowledge_version = int(cur.fetchone()[0])
                knowledge_id = str(uuid.uuid4())
                cur.execute("INSERT INTO knowledge_snapshots (id,client_id,version,snapshot_data,trigger_event,planning_set_id,source_client_version) VALUES (%s,%s,%s,%s::jsonb,'planning_refresh',%s,%s)", (knowledge_id, client_id, knowledge_version, _pg_json(documents["knowledge"]), set_id, int(version)))
                cur.execute("SELECT COALESCE(MAX(version),0)+1 FROM diagnosis_snapshots WHERE client_id=%s", (client_id,))
                diagnosis_version = int(cur.fetchone()[0])
                diagnosis_id = str(uuid.uuid4())
                cur.execute("INSERT INTO diagnosis_snapshots (id,client_id,version,knowledge_snapshot_version,diagnosis_data,planning_set_id,source_client_version) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)", (diagnosis_id, client_id, diagnosis_version, knowledge_version, _pg_json(documents["diagnosis"]), set_id, int(version)))
                projection_id = f"planning_projection_{uuid.uuid4().hex}"
                cur.execute("INSERT INTO mvp_artifacts (id,client_id,record_type,status,payload,planning_set_id,source_client_version) VALUES (%s,%s,'projection','ready',%s::jsonb,%s,%s)", (projection_id, client_id, _pg_json(documents["projection"]), set_id, int(version)))
                cur.execute("UPDATE planning_artifact_sets SET status='ready',knowledge_snapshot_id=%s,diagnosis_snapshot_id=%s,projection_artifact_id=%s,published_at=NOW(),updated_at=NOW() WHERE id=%s", (knowledge_id, diagnosis_id, projection_id, set_id))
                cur.execute("UPDATE clients SET current_planning_set_id=%s WHERE client_id=%s", (set_id, client_id))
                cur.execute(
                    """
                    UPDATE planning_refresh_state SET published_version=%s,
                      published_input_fingerprint=%s,running_version=NULL,active_job_id=NULL,
                      lease_expires_at=NULL,running_input_fingerprint=NULL,rerun_required=FALSE,
                      dirty=FALSE,status='idle',last_error=NULL,last_finished_at=NOW(),updated_at=NOW()
                    WHERE client_id=%s AND active_job_id=%s AND running_version=%s
                    """,
                    (int(version), source_input_fingerprint, client_id, job_id, int(version)),
                )
            conn.commit()
            return {"id": set_id, "status": "ready", "published": True, "source_client_version": int(version), "source_input_fingerprint": source_input_fingerprint}
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def get_current_planning_artifact_set(*, client_id: str) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            set_id = _memory_current.get(client_id)
            return dict(_memory_sets[set_id]) if set_id else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT artifact_set.id, artifact_set.source_client_version, artifact_set.status, artifact_set.published_at, artifact_set.source_snapshot_id, artifact_set.source_input_fingerprint, artifact_set.source_provider_revisions FROM clients client JOIN planning_artifact_sets artifact_set ON artifact_set.id = client.current_planning_set_id WHERE client.client_id = %s AND artifact_set.status = 'ready'", (client_id,))
            row = cur.fetchone()
            return None if row is None else {"id": str(row[0]), "source_client_version": int(row[1]), "status": row[2], "published_at": str(row[3]), "source_snapshot_id": row[4], "source_input_fingerprint": row[5], "source_provider_revisions": list(row[6] or [])}
    finally:
        pool.putconn(conn)
