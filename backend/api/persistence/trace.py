"""Persistence helpers for AWM trace events.

Trace is intentionally append-only. Business tables remain the source of truth;
trace rows explain how a user interaction, workflow, engine run, or advisory
event moved through the system.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional

from .core import _get_pool, _pg_json, _safe_getconn


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def create_trace_event(
    *,
    trace_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
    client_id: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    case_id: Optional[str] = None,
    case_type: Optional[str] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    event_type: str,
    event_name: Optional[str] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    status: str = "success",
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    tool_name: Optional[str] = None,
    prompt_name: Optional[str] = None,
    prompt_version: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    engine_name: Optional[str] = None,
    engine_version: Optional[str] = None,
    expert_product_version: Optional[str] = None,
    artifact_version: Optional[int] = None,
    input_summary: Optional[str] = None,
    output_summary: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    subjects: Optional[Iterable[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Append one trace event and optional subject links."""
    pool = _get_pool()
    if pool is None:
        return None


def create_trace_events(events: Iterable[Dict[str, Any]]) -> int:
    """Append trace events in one transaction to avoid per-row network latency."""
    items = [dict(event) for event in events if isinstance(event, dict)]
    if not items:
        return 0
    pool = _get_pool()
    if pool is None:
        return 0
    conn = _safe_getconn(pool)
    try:
        try:
            from psycopg2.extras import execute_values

            rows = []
            subject_rows = []
            for item in items:
                event_id = str(uuid.uuid4())
                trace_id = str(item.get("trace_id") or f"tr_{uuid.uuid4().hex}")
                rows.append(
                    (
                        event_id,
                        trace_id,
                        item.get("client_id"),
                        item.get("session_id"),
                        item.get("turn_id"),
                        item.get("source_type"),
                        item.get("event_type"),
                        item.get("event_name"),
                        item.get("actor_type"),
                        item.get("actor_id"),
                        item.get("status", "success"),
                        item.get("agent_name"),
                        item.get("tool_name"),
                        item.get("engine_name"),
                        item.get("input_summary"),
                        item.get("output_summary"),
                        _pg_json(item.get("payload") or {}),
                    )
                )
                for subject in item.get("subjects") or []:
                    subject_type = str(subject.get("subject_type") or "").strip()
                    subject_id = str(subject.get("subject_id") or "").strip()
                    if subject_type and subject_id:
                        subject_rows.append(
                            (
                                event_id,
                                subject_type,
                                subject_id,
                                str(subject.get("relation") or "referenced"),
                            )
                        )
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO trace_events
                        (id, trace_id, client_id, session_id, turn_id, source_type,
                         event_type, event_name, actor_type, actor_id, status,
                         agent_name, tool_name, engine_name, input_summary,
                         output_summary, payload)
                    VALUES %s
                    """,
                    rows,
                    template=(
                        "(%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
                    ),
                )
                if subject_rows:
                    execute_values(
                        cur,
                        """
                        INSERT INTO trace_event_subjects
                            (trace_event_id, subject_type, subject_id, relation)
                        VALUES %s
                        """,
                        subject_rows,
                        template="(%s::uuid,%s,%s,%s)",
                    )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
    except Exception as exc:
        print(f"[db] create_trace_events failed: {exc}", flush=True)
        return 0
    finally:
        pool.putconn(conn)
    trace_id = trace_id or f"tr_{uuid.uuid4().hex}"
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trace_events
                        (trace_id, parent_event_id, client_id, session_id, turn_id,
                         case_id, case_type, source_type, source_id,
                         event_type, event_name, actor_type, actor_id, status,
                         error_code, error_message, duration_ms,
                         model_provider, model_name, agent_name, tool_name,
                         prompt_name, prompt_version, prompt_hash,
                         engine_name, engine_version, expert_product_version,
                         artifact_version, input_summary, output_summary, payload)
                    VALUES
                        (%s, %s::uuid, %s, %s, %s,
                         %s, %s, %s, %s,
                         %s, %s, %s, %s, %s,
                         %s, %s, %s,
                         %s, %s, %s, %s,
                         %s, %s, %s,
                         %s, %s, %s,
                         %s, %s, %s, %s::jsonb)
                    RETURNING id, trace_id, event_type, event_time, status
                    """,
                    (
                        trace_id,
                        parent_event_id,
                        client_id,
                        session_id,
                        turn_id,
                        case_id,
                        case_type,
                        source_type,
                        source_id,
                        event_type,
                        event_name,
                        actor_type,
                        actor_id,
                        status,
                        error_code,
                        error_message,
                        duration_ms,
                        model_provider,
                        model_name,
                        agent_name,
                        tool_name,
                        prompt_name,
                        prompt_version,
                        prompt_hash,
                        engine_name,
                        engine_version,
                        expert_product_version,
                        artifact_version,
                        input_summary,
                        output_summary,
                        _pg_json(payload or {}),
                    ),
                )
                row = cur.fetchone()
                event_id = str(row[0])
                for subject in subjects or []:
                    subject_type = str(subject.get("subject_type") or "").strip()
                    subject_id = str(subject.get("subject_id") or "").strip()
                    if not subject_type or not subject_id:
                        continue
                    cur.execute(
                        """
                        INSERT INTO trace_event_subjects
                            (trace_event_id, subject_type, subject_id, relation)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            event_id,
                            subject_type,
                            subject_id,
                            str(subject.get("relation") or "referenced"),
                        ),
                    )
            conn.commit()
            return {
                "id": event_id,
                "trace_id": row[1],
                "event_type": row[2],
                "event_time": _iso(row[3]),
                "status": row[4],
            }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_trace_event failed: {exc}", flush=True)
        return None


def list_trace_events(
    *,
    trace_id: Optional[str] = None,
    client_id: Optional[str] = None,
    case_type: Optional[str] = None,
    case_id: Optional[str] = None,
    subject_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List trace events by interaction, client, case, or subject lineage."""
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 100), 500))
    where: List[str] = []
    params: List[Any] = []
    join = ""
    if subject_type and subject_id:
        join = "JOIN trace_event_subjects s ON s.trace_event_id = e.id"
        where.extend(["s.subject_type = %s", "s.subject_id = %s"])
        params.extend([subject_type, subject_id])
    if trace_id:
        where.append("e.trace_id = %s")
        params.append(trace_id)
    if client_id:
        where.append("e.client_id = %s")
        params.append(client_id)
    if case_type:
        where.append("e.case_type = %s")
        params.append(case_type)
    if case_id:
        where.append("e.case_id = %s")
        params.append(case_id)
    if status:
        where.append("e.status = %s")
        params.append(status)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT e.id, e.trace_id, e.parent_event_id, e.client_id,
                           e.session_id, e.turn_id, e.case_id, e.case_type,
                           e.source_type, e.source_id, e.event_type, e.event_name,
                           e.event_time, e.actor_type, e.actor_id, e.status,
                           e.error_code, e.error_message, e.duration_ms,
                           e.agent_name, e.tool_name, e.engine_name,
                           e.input_summary, e.output_summary, e.payload,
                           e.created_at
                    FROM trace_events e
                    {join}
                    {sql_where}
                    ORDER BY e.event_time ASC, e.created_at ASC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                return [_trace_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_trace_events failed: {exc}", flush=True)
        return []


def _trace_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "trace_id": row[1],
        "parent_event_id": str(row[2]) if row[2] else None,
        "client_id": row[3],
        "session_id": row[4],
        "turn_id": row[5],
        "case_id": row[6],
        "case_type": row[7],
        "source_type": row[8],
        "source_id": row[9],
        "event_type": row[10],
        "event_name": row[11],
        "event_time": _iso(row[12]),
        "actor_type": row[13],
        "actor_id": row[14],
        "status": row[15],
        "error_code": row[16],
        "error_message": row[17],
        "duration_ms": row[18],
        "agent_name": row[19],
        "tool_name": row[20],
        "engine_name": row[21],
        "input_summary": row[22],
        "output_summary": row[23],
        "payload": row[24] or {},
        "created_at": _iso(row[25]),
    }


__all__ = ["create_trace_event", "create_trace_events", "list_trace_events"]
