"""Knowledge facts, snapshots, diagnoses, and pending confirmations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn, _pg_json

# ---------------------------------------------------------------------------
# Knowledge facts — canonical facts for "What AWM Knows"
# ---------------------------------------------------------------------------

def store_knowledge_fact(
    client_id: str,
    domain: str,
    category: str,
    label: str,
    value: Any,
    status: str = "inferred",
    confidence: float = 0.7,
    source_event_ids: Optional[List[str]] = None,
    snapshot_version: int = 1,
) -> Optional[str]:
    """Store a single knowledge fact. Returns fact_id on success."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        fact_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO knowledge_facts
                        (id, client_id, domain, category, label, value,
                         status, confidence, source_event_ids, snapshot_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fact_id, client_id, domain, category, label,
                        json.dumps(value),
                        status, confidence,
                        json.dumps(source_event_ids or []),
                        snapshot_version,
                    ),
                )
            conn.commit()
            return fact_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store knowledge fact: {exc}", flush=True)
        return None


def get_knowledge_facts(
    client_id: str,
    domain: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve knowledge facts for a client, optionally filtered by domain/status."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            conditions = ["client_id = %s", "is_deleted = FALSE"]
            params: list = [client_id]
            if domain:
                conditions.append("domain = %s")
                params.append(domain)
            if status:
                conditions.append("status = %s")
                params.append(status)
            where = " AND ".join(conditions)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, domain, category, label, value,
                           status, confidence, source_event_ids, snapshot_version,
                           created_at, last_updated_at, last_confirmed_at
                    FROM knowledge_facts
                    WHERE {where}
                    ORDER BY domain, category, created_at
                    """,
                    params,
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "domain": r[2],
                        "category": r[3],
                        "label": r[4],
                        "value": r[5],
                        "status": r[6],
                        "confidence": r[7],
                        "source_event_ids": r[8] or [],
                        "snapshot_version": r[9],
                        "created_at": r[10].isoformat() if r[10] else None,
                        "last_updated_at": r[11].isoformat() if r[11] else None,
                        "last_confirmed_at": r[12].isoformat() if r[12] else None,
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get knowledge facts: {exc}", flush=True)
        return []


def get_knowledge_fact(fact_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single knowledge fact by ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, domain, category, label, value,
                           status, confidence, source_event_ids, snapshot_version,
                           created_at, last_updated_at, last_confirmed_at
                    FROM knowledge_facts WHERE id = %s
                    """,
                    (fact_id,),
                )
                r = cur.fetchone()
                if r is None:
                    return None
                return {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "domain": r[2],
                    "category": r[3],
                    "label": r[4],
                    "value": r[5],
                    "status": r[6],
                    "confidence": r[7],
                    "source_event_ids": r[8] or [],
                    "snapshot_version": r[9],
                    "created_at": r[10].isoformat() if r[10] else None,
                    "last_updated_at": r[11].isoformat() if r[11] else None,
                    "last_confirmed_at": r[12].isoformat() if r[12] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get knowledge fact: {exc}", flush=True)
        return None


def update_knowledge_fact(fact_id: str, **columns: Any) -> bool:
    """Update one or more columns on a knowledge fact."""
    pool = _get_pool()
    if pool is None or not columns:
        return False

    allowed = {
        "value", "status", "confidence", "source_event_ids",
        "snapshot_version", "last_updated_at", "last_confirmed_at", "is_deleted",
    }
    filtered = {k: v for k, v in columns.items() if k in allowed}
    if not filtered:
        return False

    set_clauses = []
    values = []
    for col, val in filtered.items():
        set_clauses.append(f"{col} = %s")
        if col in ("value", "source_event_ids"):
            values.append(json.dumps(val))
        else:
            values.append(val)
    # Always bump last_updated_at
    if "last_updated_at" not in filtered:
        set_clauses.append("last_updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
    values.append(fact_id)

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE knowledge_facts SET {', '.join(set_clauses)} WHERE id = %s",
                    values,
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to update knowledge fact: {exc}", flush=True)
        return False


def bulk_upsert_knowledge_facts(
    client_id: str,
    facts: List[Dict[str, Any]],
) -> List[str]:
    """Insert or update multiple knowledge facts. Returns list of fact IDs."""
    pool = _get_pool()
    if pool is None or not facts:
        return []
    try:
        conn = _safe_getconn(pool)
        fact_ids = []
        try:
            with conn.cursor() as cur:
                for fact in facts:
                    fact_id = fact.get("id") or str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO knowledge_facts
                            (id, client_id, domain, category, label, value,
                             status, confidence, source_event_ids, snapshot_version)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            value = EXCLUDED.value,
                            status = EXCLUDED.status,
                            confidence = EXCLUDED.confidence,
                            source_event_ids = EXCLUDED.source_event_ids,
                            snapshot_version = EXCLUDED.snapshot_version,
                            last_updated_at = NOW()
                        """,
                        (
                            fact_id,
                            client_id,
                            fact["domain"],
                            fact["category"],
                            fact["label"],
                            json.dumps(fact["value"]),
                            fact.get("status", "inferred"),
                            fact.get("confidence", 0.7),
                            json.dumps(fact.get("source_event_ids", [])),
                            fact.get("snapshot_version", 1),
                        ),
                    )
                    fact_ids.append(fact_id)
            conn.commit()
            return fact_ids
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to bulk upsert knowledge facts: {exc}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Knowledge snapshots — versioned compact client model
# ---------------------------------------------------------------------------

def get_current_snapshot_version(client_id: str) -> int:
    """Return the latest snapshot version for a client, or 0 if none exist."""
    pool = _get_pool()
    if pool is None:
        return 0
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM knowledge_snapshots WHERE client_id = %s",
                    (client_id,),
                )
                row = cur.fetchone()
                return row[0] if row else 0
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get snapshot version: {exc}", flush=True)
        return 0


def store_knowledge_snapshot(
    client_id: str,
    version: int,
    snapshot_data: Dict[str, Any],
    fact_ids: Optional[List[str]] = None,
    trigger_event: Optional[str] = None,
    trigger_event_id: Optional[str] = None,
) -> Optional[str]:
    """Store a knowledge snapshot. Returns snapshot ID on success."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        snapshot_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO knowledge_snapshots
                        (id, client_id, version, snapshot_data, fact_ids,
                         trigger_event, trigger_event_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        snapshot_id, client_id, version,
                        _pg_json(snapshot_data),
                        _pg_json(fact_ids or []),
                        trigger_event, trigger_event_id,
                    ),
                )
            conn.commit()
            return snapshot_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store knowledge snapshot: {exc}", flush=True)
        return None


def get_latest_knowledge_snapshot(client_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the latest knowledge snapshot for a client."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, version, snapshot_data, fact_ids,
                           trigger_event, trigger_event_id, created_at
                    FROM knowledge_snapshots
                    WHERE client_id = %s
                    ORDER BY version DESC LIMIT 1
                    """,
                    (client_id,),
                )
                r = cur.fetchone()
                if r is None:
                    return None
                return {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "version": r[2],
                    "snapshot_data": r[3] or {},
                    "fact_ids": r[4] or [],
                    "trigger_event": r[5],
                    "trigger_event_id": r[6],
                    "created_at": r[7].isoformat() if r[7] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get latest knowledge snapshot: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Diagnosis snapshots — versioned financial/health assessment
# ---------------------------------------------------------------------------

def store_diagnosis_snapshot(
    client_id: str,
    version: int,
    knowledge_snapshot_version: int,
    diagnosis_data: Dict[str, Any],
    model_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Store a diagnosis snapshot. Returns snapshot ID on success."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        snapshot_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO diagnosis_snapshots
                        (id, client_id, version, knowledge_snapshot_version,
                         diagnosis_data, model_metadata)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        snapshot_id, client_id, version,
                        knowledge_snapshot_version,
                        _pg_json(diagnosis_data),
                        _pg_json(model_metadata or {}),
                    ),
                )
            conn.commit()
            return snapshot_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store diagnosis snapshot: {exc}", flush=True)
        return None


def get_latest_diagnosis_snapshot(client_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the latest diagnosis snapshot for a client."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, version, knowledge_snapshot_version,
                           diagnosis_data, model_metadata, created_at
                    FROM diagnosis_snapshots
                    WHERE client_id = %s
                    ORDER BY version DESC LIMIT 1
                    """,
                    (client_id,),
                )
                r = cur.fetchone()
                if r is None:
                    return None
                return {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "version": r[2],
                    "knowledge_snapshot_version": r[3],
                    "diagnosis_data": r[4] or {},
                    "model_metadata": r[5] or {},
                    "created_at": r[6].isoformat() if r[6] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get latest diagnosis snapshot: {exc}", flush=True)
        return None


def upsert_diagnosis_refresh_state(client_id: str, state: Dict[str, Any]) -> bool:
    """Persist async diagnosis refresh state for a client."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO diagnosis_refresh_state (client_id, state, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (client_id)
                    DO UPDATE SET
                        state = EXCLUDED.state,
                        updated_at = NOW()
                    """,
                    (client_id, json.dumps(state)),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to upsert diagnosis refresh state: {exc}", flush=True)
        return False


def get_diagnosis_refresh_state(client_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve async diagnosis refresh state for a client."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT client_id, state, updated_at
                    FROM diagnosis_refresh_state
                    WHERE client_id = %s
                    """,
                    (client_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "client_id": row[0],
                    "state": row[1] or {},
                    "updated_at": row[2].isoformat() if row[2] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get diagnosis refresh state: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Pending confirmations — material changes needing user approval
# ---------------------------------------------------------------------------

def store_pending_confirmation(
    client_id: str,
    fact_id: Optional[str] = None,
    change_type: str = "new_fact",
    proposed_value: Any = None,
    previous_value: Any = None,
    evidence: Optional[str] = None,
    source_event_id: Optional[str] = None,
) -> Optional[str]:
    """Store a pending confirmation. Returns confirmation ID on success."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conf_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_confirmations
                        (id, client_id, fact_id, change_type, proposed_value,
                         previous_value, evidence, source_event_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        conf_id, client_id, fact_id, change_type,
                        json.dumps(proposed_value) if proposed_value is not None else None,
                        json.dumps(previous_value) if previous_value is not None else None,
                        evidence, source_event_id,
                    ),
                )
            conn.commit()
            return conf_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store pending confirmation: {exc}", flush=True)
        return None


def get_pending_confirmations(
    client_id: str,
    status: str = "pending",
) -> List[Dict[str, Any]]:
    """Retrieve pending confirmations for a client."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, fact_id, change_type, proposed_value,
                           previous_value, evidence, source_event_id, status,
                           created_at, resolved_at
                    FROM pending_confirmations
                    WHERE client_id = %s AND status = %s
                    ORDER BY created_at DESC
                    """,
                    (client_id, status),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "client_id": r[1],
                        "fact_id": str(r[2]) if r[2] else None,
                        "change_type": r[3],
                        "proposed_value": r[4],
                        "previous_value": r[5],
                        "evidence": r[6],
                        "source_event_id": r[7],
                        "status": r[8],
                        "created_at": r[9].isoformat() if r[9] else None,
                        "resolved_at": r[10].isoformat() if r[10] else None,
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get pending confirmations: {exc}", flush=True)
        return []


def get_pending_confirmation(confirmation_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single pending confirmation by ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, fact_id, change_type, proposed_value,
                           previous_value, evidence, source_event_id, status,
                           created_at, resolved_at
                    FROM pending_confirmations
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (confirmation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": str(row[0]),
                    "client_id": row[1],
                    "fact_id": str(row[2]) if row[2] else None,
                    "change_type": row[3],
                    "proposed_value": row[4],
                    "previous_value": row[5],
                    "evidence": row[6],
                    "source_event_id": row[7],
                    "status": row[8],
                    "created_at": row[9].isoformat() if row[9] else None,
                    "resolved_at": row[10].isoformat() if row[10] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get pending confirmation: {exc}", flush=True)
        return None


def resolve_pending_confirmation(confirmation_id: str, status: str) -> bool:
    """Resolve a pending confirmation (confirmed / dismissed / expired)."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_confirmations
                    SET status = %s, resolved_at = NOW()
                    WHERE id = %s
                    """,
                    (status, confirmation_id),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to resolve pending confirmation: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Consultation sessions — typed sessions (onboarding / journey preparation)
