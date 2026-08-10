"""Consultation ingest, pipeline, and consultation-session persistence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn

_memory_thread_annotations: Dict[str, List[Dict[str, Any]]] = {}
_memory_thread_annotations_lock = RLock()


def _reset_memory_thread_annotations_for_tests() -> None:
    """Clear process-local thread annotations. Intended only for tests."""
    with _memory_thread_annotations_lock:
        _memory_thread_annotations.clear()


# ---------------------------------------------------------------------------
# Consultation ingests
# ---------------------------------------------------------------------------

def store_consultation_ingest(ingest_payload: Dict[str, Any]) -> bool:
    """Store consultation ingest in the database. Returns True on success."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO consultation_ingests
                        (id, session_id, client_id, created_at, transcript, agent_preview, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        client_id = EXCLUDED.client_id,
                        transcript = EXCLUDED.transcript,
                        agent_preview = EXCLUDED.agent_preview
                    """,
                    (
                        ingest_payload.get("ingest_id"),
                        ingest_payload.get("session_id", ""),
                        ingest_payload.get("client_id"),
                        ingest_payload.get("created_at", datetime.now(timezone.utc).isoformat()),
                        json.dumps(ingest_payload.get("transcript", {})),
                        json.dumps(ingest_payload.get("agent_preview", {})),
                        json.dumps(ingest_payload.get("transcript", {}).get("metadata", {})),
                    ),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store ingest: {exc}", flush=True)
        return False


def get_consultation_ingest(ingest_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a consultation ingest by ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, session_id, created_at, transcript, agent_preview, metadata
                    FROM consultation_ingests WHERE id = %s
                    """,
                    (ingest_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "ingest_id": str(row[0]),
                    "session_id": row[1],
                    "created_at": row[2].isoformat() if row[2] else None,
                    "transcript": row[3] if isinstance(row[3], dict) else {},
                    "agent_preview": row[4] if isinstance(row[4], dict) else {},
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get ingest: {exc}", flush=True)
        return None


def get_latest_consultation_ingest() -> Optional[Dict[str, Any]]:
    """Retrieve the most recent consultation ingest."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, session_id, created_at, transcript, agent_preview, metadata
                    FROM consultation_ingests ORDER BY created_at DESC LIMIT 1
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "ingest_id": str(row[0]),
                    "session_id": row[1],
                    "created_at": row[2].isoformat() if row[2] else None,
                    "transcript": row[3] if isinstance(row[3], dict) else {},
                    "agent_preview": row[4] if isinstance(row[4], dict) else {},
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get latest ingest: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Pipeline runs — read-only access for inspecting past runs
# ---------------------------------------------------------------------------

def get_pipeline_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a pipeline run by ID with all step outputs."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, consultation_ingest_id, session_id, created_at, completed_at,
                           status, client_profile_input, client_profile_output,
                           step1_policy, step1_model_metadata,
                           ui_transform_input, ui_transform_output,
                           final_ui_policy, advisor_request, model_metadata, error
                    FROM pipeline_runs WHERE id = %s
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": str(row[0]),
                    "consultation_ingest_id": str(row[1]) if row[1] else None,
                    "session_id": row[2],
                    "created_at": row[3].isoformat() if row[3] else None,
                    "completed_at": row[4].isoformat() if row[4] else None,
                    "status": row[5],
                    "client_profile_input": row[6] or {},
                    "client_profile_output": row[7] or {},
                    "step1_policy": row[8] or {},
                    "step1_model_metadata": row[9] or {},
                    "ui_transform_input": row[10] or {},
                    "ui_transform_output": row[11] or {},
                    "final_ui_policy": row[12] or {},
                    "advisor_request": row[13] or "",
                    "model_metadata": row[14] or {},
                    "error": row[15],
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get pipeline run: {exc}", flush=True)
        return None


def get_latest_pipeline_run(session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve the most recent pipeline run, optionally filtered by session."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if session_id:
                    cur.execute(
                        """
                        SELECT id FROM pipeline_runs
                        WHERE session_id = %s ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id,),
                    )
                else:
                    cur.execute(
                        "SELECT id FROM pipeline_runs ORDER BY created_at DESC LIMIT 1"
                    )
                row = cur.fetchone()
                if row is None:
                    return None
                return get_pipeline_run(str(row[0]))
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get latest pipeline run: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Pipeline checkpoints — resumable intermediate state
# ---------------------------------------------------------------------------

def store_pipeline_checkpoint(
    run_id: str,
    pipeline_name: str,
    step_name: str,
    context: Dict[str, Any],
) -> bool:
    """Upsert a pipeline checkpoint after a successful step.

    Uses INSERT ... ON CONFLICT to create or update the single checkpoint
    row for this (run_id, pipeline_name) pair.
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
                    INSERT INTO pipeline_checkpoints
                        (run_id, pipeline_name, step_name, context, client_id, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (run_id, pipeline_name) DO UPDATE SET
                        step_name  = EXCLUDED.step_name,
                        context    = EXCLUDED.context,
                        client_id  = EXCLUDED.client_id,
                        updated_at = NOW()
                    """,
                    (
                        run_id,
                        pipeline_name,
                        step_name,
                        json.dumps(context, default=str),
                        context.get("client_id"),
                    ),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store pipeline checkpoint: {exc}", flush=True)
        return False


def load_pipeline_checkpoint(
    run_id: str,
    pipeline_name: str,
) -> Optional[Dict[str, Any]]:
    """Load the most recent checkpoint for a pipeline run.

    Returns:
        {"step_name": str, "context": dict, "updated_at": str} or None.
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
                    SELECT step_name, context, updated_at
                    FROM pipeline_checkpoints
                    WHERE run_id = %s AND pipeline_name = %s
                    """,
                    (run_id, pipeline_name),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "step_name": row[0],
                    "context": row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}"),
                    "updated_at": row[2].isoformat() if row[2] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to load pipeline checkpoint: {exc}", flush=True)
        return None


def delete_pipeline_checkpoint(run_id: str, pipeline_name: str) -> bool:
    """Remove a checkpoint after a pipeline completes successfully."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM pipeline_checkpoints WHERE run_id = %s AND pipeline_name = %s",
                    (run_id, pipeline_name),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to delete pipeline checkpoint: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# AI Companion messages
# ---------------------------------------------------------------------------

def create_consultation_session(
    client_id: str,
    session_type: str,
    journey_type: Optional[str] = None,
    journey_id: Optional[str] = None,
    trigger_source: Optional[str] = None,
    baseline_snapshot_version: Optional[int] = None,
    companion_session_id: Optional[str] = None,
) -> Optional[str]:
    """Create a consultation session. Returns session ID on success."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        session_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO consultation_sessions
                        (id, client_id, session_type, journey_type, journey_id,
                         trigger_source, baseline_snapshot_version, companion_session_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session_id, client_id, session_type, journey_type,
                        journey_id, trigger_source, baseline_snapshot_version,
                        companion_session_id,
                    ),
                )
            conn.commit()
            return session_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to create consultation session: {exc}", flush=True)
        return None


def get_consultation_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a consultation session by ID."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, session_type, journey_type, journey_id,
                           trigger_source, baseline_snapshot_version, companion_session_id,
                           status, transcript, metadata, created_at, completed_at
                    FROM consultation_sessions WHERE id = %s
                    """,
                    (session_id,),
                )
                r = cur.fetchone()
                if r is None:
                    return None
                return {
                    "id": str(r[0]),
                    "client_id": r[1],
                    "session_type": r[2],
                    "journey_type": r[3],
                    "journey_id": str(r[4]) if r[4] else None,
                    "trigger_source": r[5],
                    "baseline_snapshot_version": r[6],
                    "companion_session_id": r[7],
                    "status": r[8],
                    "transcript": r[9] or {},
                    "metadata": r[10] or {},
                    "created_at": r[11].isoformat() if r[11] else None,
                    "completed_at": r[12].isoformat() if r[12] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get consultation session: {exc}", flush=True)
        return None


def get_client_conversation_history(client_id: str, max_turns: int = 200) -> List[Dict[str, Any]]:
    """Flatten every completed consultation transcript for a client into one
    chronological list of turns, so the single window can be rehydrated on
    re-login as one continuous conversation.

    Returns turns as {"role": "assistant"|"user", "text": str, "ts": int},
    sorted by their original start time and capped to the most recent max_turns.
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
                    SELECT transcript, created_at
                    FROM consultation_sessions
                    WHERE client_id = %s AND transcript IS NOT NULL
                    ORDER BY created_at ASC
                    """,
                    (client_id,),
                )
                rows = cur.fetchall()
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get client conversation history: {exc}", flush=True)
        return []

    turns: List[Dict[str, Any]] = []
    for idx, (transcript, created_at) in enumerate(rows):
        if not isinstance(transcript, dict):
            continue
        session_turns = transcript.get("turns")
        if not isinstance(session_turns, list):
            continue
        base_ms = int(created_at.timestamp() * 1000) if created_at else 0
        for order, turn in enumerate(session_turns):
            if not isinstance(turn, dict):
                continue
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            speaker = str(turn.get("speaker") or "").lower()
            role = "assistant" if speaker in {"agent", "assistant", "advisor", "rm"} else "user"
            ts = turn.get("ts_start_ms")
            # Fall back to session time + ordering when a turn lacks a timestamp,
            # so cross-session ordering stays stable.
            ts = int(ts) if isinstance(ts, (int, float)) and ts else base_ms + order
            turns.append({"role": role, "text": text, "ts": ts})

    turns.sort(key=lambda t: t["ts"])
    if len(turns) > max_turns:
        turns = turns[-max_turns:]
    return turns


def add_thread_annotation(
    client_id: str,
    kind: str,
    payload: Dict[str, Any],
    client_ts: int = 0,
    role: str = "assistant",
) -> bool:
    """Persist a non-text conversation card (a raised event or a pulled proposal
    section) so the single window can rehydrate those cards on re-login."""
    pool = _get_pool()
    if pool is None:
        row = {
            "kind": kind,
            "role": role,
            "payload": dict(payload or {}),
            "client_ts": int(client_ts or 0),
        }
        with _memory_thread_annotations_lock:
            _memory_thread_annotations.setdefault(client_id, []).append(row)
        return True
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO thread_annotations (client_id, kind, role, payload, client_ts)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (client_id, kind, role, json.dumps(payload or {}), int(client_ts or 0)),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to add thread annotation: {exc}", flush=True)
        return False


def get_thread_annotations(client_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Return the client's thread annotations (event/section cards), oldest first."""
    pool = _get_pool()
    if pool is None:
        with _memory_thread_annotations_lock:
            memory_rows = list(_memory_thread_annotations.get(client_id, []))
        memory_rows.sort(key=lambda row: row["client_ts"])
        rows = [
            (
                row["kind"],
                row["role"],
                dict(row["payload"]),
                row["client_ts"],
            )
            for row in memory_rows[:limit]
        ]
    else:
        try:
            conn = _safe_getconn(pool)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT kind, role, payload, client_ts
                        FROM thread_annotations
                        WHERE client_id = %s
                        ORDER BY client_ts ASC
                        LIMIT %s
                        """,
                        (client_id, limit),
                    )
                    rows = cur.fetchall()
            finally:
                pool.putconn(conn)
        except Exception as exc:
            print(f"[db] Failed to get thread annotations: {exc}", flush=True)
            return []
    out: List[Dict[str, Any]] = []
    for kind, role, payload, client_ts in rows:
        p = payload if isinstance(payload, dict) else {}
        out.append({
            "kind": kind,
            "role": role or "assistant",
            "ts": int(client_ts or 0),
            "title": p.get("title", ""),
            "summary": p.get("summary", ""),
            "artifactType": p.get("artifactType", ""),
            "text": p.get("text", ""),
            "artifactId": p.get("artifactId", ""),
            "durationSeconds": int(p.get("durationSeconds") or 0),
            "assessmentId": p.get("assessmentId", ""),
            "assessmentVersion": p.get("assessmentVersion"),
            "investmentConsultationId": p.get("investmentConsultationId", ""),
            "moneyPoolId": p.get("moneyPoolId", ""),
            "poolLabel": p.get("poolLabel", ""),
            "subtitle": p.get("subtitle", ""),
            "paragraphs": p.get("paragraphs") if isinstance(p.get("paragraphs"), list) else [],
            "decision": p.get("decision", ""),
            "proposalId": p.get("proposalId", ""),
        })
    return out


def complete_consultation_session(
    session_id: str,
    transcript: Dict[str, Any],
) -> bool:
    """Store transcript and mark a consultation session as processing."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE consultation_sessions
                    SET status = 'processing',
                        transcript = %s,
                        completed_at = NULL
                    WHERE id = %s
                    """,
                    (json.dumps(transcript), session_id),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to complete consultation session: {exc}", flush=True)
        return False


def finalize_consultation_session(
    session_id: str,
    *,
    status: str = "completed",
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Finalize a consultation session after async processing finishes."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE consultation_sessions
                    SET status = %s,
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                        completed_at = CASE
                            WHEN %s = 'completed' THEN NOW()
                            ELSE completed_at
                        END
                    WHERE id = %s
                    """,
                    (
                        status,
                        json.dumps(metadata or {}),
                        status,
                        session_id,
                    ),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to finalize consultation session: {exc}", flush=True)
        return False


def set_consultation_session_progress_label(session_id: str, label: str) -> bool:
    """Write a live progress_label into the session's metadata so any worker can serve it."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE consultation_sessions
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object('progress_label', %s::text)
                    WHERE id = %s
                    """,
                    (label, session_id),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to set consultation progress label: {exc}", flush=True)
        return False
