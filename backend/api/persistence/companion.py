"""AI companion message and reasoning-task persistence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, Iterator, List, Optional

from .core import _get_pool, _safe_getconn

# ---------------------------------------------------------------------------
# AI Companion messages
# ---------------------------------------------------------------------------

_memory_messages: Dict[str, List[Dict[str, Any]]] = {}
_memory_messages_lock = RLock()


def _store_memory_message(
    session_id: str,
    role: str,
    content: str,
    *,
    state: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    created_at: Optional[datetime] = None,
    client_id: Optional[str] = None,
) -> str:
    """Persist a companion message for the process-local no-database demo."""
    msg_id = str(uuid.uuid4())
    row = {
        "id": msg_id,
        "session_id": session_id,
        "client_id": client_id,
        "created_at": created_at or datetime.now(timezone.utc),
        "role": role,
        "content": content,
        "state": dict(state or {}),
        "metadata": dict(metadata or {}),
    }
    with _memory_messages_lock:
        _memory_messages.setdefault(session_id, []).append(row)
    return msg_id


def _get_memory_messages(
    session_id: str,
    *,
    limit: int,
    before_cursor: Optional[str] = None,
) -> List[Dict[str, Any]]:
    with _memory_messages_lock:
        rows = list(_memory_messages.get(session_id, []))

    if before_cursor:
        anchor_index = next(
            (index for index, row in enumerate(rows) if row["id"] == before_cursor),
            None,
        )
        if anchor_index is None:
            return []
        rows = rows[:anchor_index]

    rows = rows[-max(0, limit):] if limit else []
    return [
        {
            **row,
            "created_at": row["created_at"].isoformat(),
            "state": dict(row["state"]),
            "metadata": dict(row["metadata"]),
        }
        for row in rows
    ]


def _reset_memory_companion_messages_for_tests() -> None:
    """Clear process-local messages. Intended only for test isolation."""
    with _memory_messages_lock:
        _memory_messages.clear()


def store_companion_message(
    session_id: str,
    role: str,
    content: str,
    state: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    client_id: Optional[str] = None,
) -> Optional[str]:
    """Store a single AI companion message. Returns message ID on success."""
    pool = _get_pool()
    if pool is None:
        return _store_memory_message(
            session_id,
            role,
            content,
            state=state,
            metadata=metadata,
            client_id=client_id,
        )
    try:
        msg_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai_companion_messages
                        (id, session_id, client_id, role, content, state, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        msg_id,
                        session_id,
                        client_id,
                        role,
                        content,
                        json.dumps(state or {}),
                        json.dumps(metadata or {}),
                    ),
                )
            conn.commit()
            return msg_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store companion message: {exc}", flush=True)
        return None


def store_companion_message_bubbles(
    session_id: str,
    role: str,
    content: str,
    state: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    client_id: Optional[str] = None,
) -> Optional[str]:
    """Split *content* on '|||' and store each bubble as a separate row.

    Each bubble is assigned an explicit ``created_at`` timestamp with a
    1 ms offset per bubble index so that the history query (ORDER BY
    created_at ASC) always returns them in the correct display order,
    even when all INSERTs land in the same wall-clock millisecond.

    Returns the ID of the **last** bubble (the one the client uses for
    persist-ID reconciliation), or ``None`` when the DB is unavailable.

    If *content* contains no '|||' separator this behaves identically to
    :func:`store_companion_message`.
    """
    bubbles = [b.strip() for b in content.split("|||") if b.strip()]
    if not bubbles:
        # Empty content — store one empty row so the caller always gets an id.
        bubbles = [""]

    pool = _get_pool()
    if pool is None:
        last_id: Optional[str] = None
        base_ts = datetime.now(timezone.utc)
        for i, bubble in enumerate(bubbles):
            bubble_metadata = dict(metadata or {})
            if len(bubbles) > 1:
                bubble_metadata["bubble_index"] = i
                bubble_metadata["bubble_total"] = len(bubbles)
            last_id = _store_memory_message(
                session_id,
                role,
                bubble,
                state=state,
                metadata=bubble_metadata,
                created_at=base_ts + timedelta(milliseconds=i),
                client_id=client_id,
            )
        return last_id

    last_id: Optional[str] = None
    base_ts = datetime.now(timezone.utc)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                for i, bubble in enumerate(bubbles):
                    msg_id = str(uuid.uuid4())
                    bubble_metadata = dict(metadata or {})
                    if len(bubbles) > 1:
                        bubble_metadata["bubble_index"] = i
                        bubble_metadata["bubble_total"] = len(bubbles)
                    # Offset each bubble by 1 ms so ORDER BY created_at ASC
                    # always produces the original left-to-right display order.
                    bubble_ts = base_ts + timedelta(milliseconds=i)
                    cur.execute(
                        """
                        INSERT INTO ai_companion_messages
                            (id, session_id, client_id, role, content, state, metadata, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            msg_id,
                            session_id,
                            client_id,
                            role,
                            bubble,
                            json.dumps(state or {}),
                            json.dumps(bubble_metadata),
                            bubble_ts,
                        ),
                    )
                    last_id = msg_id
            conn.commit()
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store companion message bubbles: {exc}", flush=True)
        return None

    return last_id


def get_companion_messages(
    session_id: str,
    limit: int = 100,
    latest_first_window: bool = False,
    before_cursor: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve AI companion chat history for a session.

    Behaviour:
    - ``latest_first_window=True`` (used by the companion runtime to build the
      prompt context window): fetch the most recent ``limit`` messages and
      re-sort oldest-first. Unchanged.
    - ``before_cursor`` provided: fetch ``limit`` messages strictly older than
      the message with that id, ordered oldest-first (for infinite-scroll
      backfill).
    - Default: fetch the most recent ``limit`` messages ordered oldest-first.
      This replaces the previous "earliest ``limit``" behaviour, which silently
      hid recent messages once a conversation exceeded the cap.
    """
    pool = _get_pool()
    if pool is None:
        return _get_memory_messages(
            session_id,
            limit=limit,
            before_cursor=before_cursor,
        )
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if before_cursor:
                    cur.execute(
                        """
                        SELECT id, session_id, created_at, role, content, state, metadata
                        FROM (
                            SELECT m.id, m.session_id, m.created_at, m.role, m.content, m.state, m.metadata
                            FROM ai_companion_messages m
                            JOIN ai_companion_messages anchor
                              ON anchor.id = %s AND anchor.session_id = m.session_id
                            WHERE m.session_id = %s
                              AND m.created_at < anchor.created_at
                            ORDER BY m.created_at DESC
                            LIMIT %s
                        ) older
                        ORDER BY created_at ASC
                        """,
                        (before_cursor, session_id, limit),
                    )
                else:
                    # Both default and latest_first_window take the same shape
                    # now: most recent N, re-sorted ascending for display/prompt.
                    cur.execute(
                        """
                        SELECT id, session_id, created_at, role, content, state, metadata
                        FROM (
                            SELECT id, session_id, created_at, role, content, state, metadata
                            FROM ai_companion_messages
                            WHERE session_id = %s
                            ORDER BY created_at DESC
                            LIMIT %s
                        ) recent_messages
                        ORDER BY created_at ASC
                        """,
                        (session_id, limit),
                    )
                rows = cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "session_id": r[1],
                        "created_at": r[2].isoformat() if r[2] else None,
                        "role": r[3],
                        "content": r[4],
                        "state": r[5] or {},
                        "metadata": r[6] or {},
                    }
                    for r in rows
                ]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get companion messages: {exc}", flush=True)
        return []


def iter_companion_messages_since(
    session_id: str,
    after_message_id: Optional[str] = None,
    page_size: int = 500,
    client_id: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield a session's messages chronologically without a hard row cap."""
    normalized_page_size = max(1, int(page_size or 500))
    pool = _get_pool()
    if pool is None:
        with _memory_messages_lock:
            rows = list(_memory_messages.get(session_id, []))
        if client_id is not None:
            rows = [row for row in rows if row.get("client_id") == client_id]
        if after_message_id:
            anchor_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row["id"] == after_message_id
                ),
                None,
            )
            if anchor_index is None:
                return
            rows = rows[anchor_index + 1 :]
        for start in range(0, len(rows), normalized_page_size):
            for row in rows[start : start + normalized_page_size]:
                yield {
                    **row,
                    "created_at": row["created_at"].isoformat(),
                    "state": dict(row["state"]),
                    "metadata": dict(row["metadata"]),
                }
        return

    conn = None
    try:
        conn = _safe_getconn(pool)
        cursor_created_at = None
        cursor_id = None
        with conn.cursor() as cur:
            if after_message_id:
                cur.execute(
                    """
                    SELECT created_at, id
                    FROM ai_companion_messages
                    WHERE session_id = %s
                      AND (%s IS NULL OR client_id = %s)
                      AND id = %s
                    """,
                    (session_id, client_id, client_id, after_message_id),
                )
                anchor = cur.fetchone()
                if not anchor:
                    return
                cursor_created_at, cursor_id = anchor

            while True:
                if cursor_created_at is None:
                    cur.execute(
                        """
                        SELECT id, session_id, created_at, role, content, state, metadata
                        FROM ai_companion_messages
                        WHERE session_id = %s
                          AND (%s IS NULL OR client_id = %s)
                        ORDER BY created_at ASC, id ASC
                        LIMIT %s
                        """,
                        (session_id, client_id, client_id, normalized_page_size),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, session_id, created_at, role, content, state, metadata
                        FROM ai_companion_messages
                        WHERE session_id = %s
                          AND (%s IS NULL OR client_id = %s)
                          AND (created_at, id) > (%s, %s)
                        ORDER BY created_at ASC, id ASC
                        LIMIT %s
                        """,
                        (
                            session_id,
                            client_id,
                            client_id,
                            cursor_created_at,
                            cursor_id,
                            normalized_page_size,
                        ),
                    )
                rows = cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    yield {
                        "id": str(row[0]),
                        "session_id": row[1],
                        "created_at": row[2].isoformat() if row[2] else None,
                        "role": row[3],
                        "content": row[4],
                        "state": row[5] or {},
                        "metadata": row[6] or {},
                    }
                cursor_created_at, cursor_id = rows[-1][2], rows[-1][0]
                if len(rows) < normalized_page_size:
                    break
    finally:
        if conn is not None:
            pool.putconn(conn)


def iter_companion_messages_before(
    session_id: str,
    after_message_id: Optional[str] = None,
    before_message_id: Optional[str] = None,
    page_size: int = 200,
    client_id: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield messages newest-first between exclusive message boundaries."""
    normalized_page_size = max(1, int(page_size or 200))
    pool = _get_pool()
    if pool is None:
        with _memory_messages_lock:
            rows = list(_memory_messages.get(session_id, []))
        if client_id is not None:
            rows = [row for row in rows if row.get("client_id") == client_id]
        rows.sort(key=lambda row: (row["created_at"], row["id"]))

        lower_index = -1
        if after_message_id:
            lower_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row["id"] == after_message_id
                ),
                -2,
            )
            if lower_index == -2:
                return
        upper_index = len(rows)
        if before_message_id:
            upper_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row["id"] == before_message_id
                ),
                -1,
            )
            if upper_index < 0:
                return
        rows = list(reversed(rows[lower_index + 1 : upper_index]))
        for start in range(0, len(rows), normalized_page_size):
            for row in rows[start : start + normalized_page_size]:
                yield {
                    **row,
                    "created_at": row["created_at"].isoformat(),
                    "state": dict(row["state"]),
                    "metadata": dict(row["metadata"]),
                }
        return

    conn = None
    try:
        conn = _safe_getconn(pool)
        with conn.cursor() as cur:
            def _anchor(message_id: str) -> Optional[tuple[Any, Any]]:
                client_clause = "AND client_id = %s" if client_id is not None else ""
                params: List[Any] = [session_id]
                if client_id is not None:
                    params.append(client_id)
                params.append(message_id)
                cur.execute(
                    f"""
                    SELECT created_at, id
                    FROM ai_companion_messages
                    WHERE session_id = %s
                      {client_clause}
                      AND id = %s
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else None

            lower = _anchor(after_message_id) if after_message_id else None
            if after_message_id and lower is None:
                return
            cursor = _anchor(before_message_id) if before_message_id else None
            if before_message_id and cursor is None:
                return

            while True:
                where = ["session_id = %s"]
                params: List[Any] = [session_id]
                if client_id is not None:
                    where.append("client_id = %s")
                    params.append(client_id)
                if lower is not None:
                    where.append("(created_at, id) > (%s, %s)")
                    params.extend(lower)
                if cursor is not None:
                    where.append("(created_at, id) < (%s, %s)")
                    params.extend(cursor)
                params.append(normalized_page_size)
                cur.execute(
                    f"""
                    SELECT id, session_id, created_at, role, content, state, metadata
                    FROM ai_companion_messages
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    yield {
                        "id": str(row[0]),
                        "session_id": row[1],
                        "created_at": row[2].isoformat() if row[2] else None,
                        "role": row[3],
                        "content": row[4],
                        "state": row[5] or {},
                        "metadata": row[6] or {},
                    }
                cursor = (rows[-1][2], rows[-1][0])
                if len(rows) < normalized_page_size:
                    break
    finally:
        if conn is not None:
            pool.putconn(conn)


def count_companion_messages(session_id: str) -> int:
    """Return the total number of messages stored for a companion session."""
    pool = _get_pool()
    if pool is None:
        with _memory_messages_lock:
            return len(_memory_messages.get(session_id, []))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM ai_companion_messages WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to count companion messages: {exc}", flush=True)
        return 0


def store_policy_ready_companion_message_once(
    *,
    session_id: str,
    journey_id: str,
    policy_title: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Optional[str]:
    """Store the one-shot policy-ready companion bubble for a journey.

    Journey completion can be retried by Cloud Tasks, so this helper checks
    for an existing policy-ready message with the same journey metadata before
    inserting. It returns the existing or newly-created message id.
    """
    if not session_id or not journey_id:
        return None

    title = (policy_title or "your investment policy").strip()
    content = f"Your policy is ready to review: {title}."
    metadata = {
        "message_kind": "policy_ready",
        "journey_id": journey_id,
    }
    state = {
        "proactive": True,
        "journey_id": journey_id,
        "ui_directive": "open_policy_proposal",
    }

    pool = _get_pool()
    if pool is None:
        with _memory_messages_lock:
            existing = next(
                (
                    row["id"]
                    for row in _memory_messages.get(session_id, [])
                    if row["role"] == "assistant"
                    and row["metadata"].get("message_kind") == "policy_ready"
                    and row["metadata"].get("journey_id") == journey_id
                ),
                None,
            )
        if existing:
            return existing
        return _store_memory_message(
            session_id,
            "assistant",
            content,
            state=state,
            metadata=metadata,
            client_id=client_id,
        )
    try:
        msg_id = str(uuid.uuid4())
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if not client_id:
                    cur.execute(
                        "SELECT client_id FROM journey_runs WHERE id = %s",
                        (journey_id,),
                    )
                    owner = cur.fetchone()
                    client_id = str(owner[0]) if owner else None
                cur.execute(
                    """
                    SELECT id
                    FROM ai_companion_messages
                    WHERE session_id = %s
                      AND role = 'assistant'
                      AND metadata->>'message_kind' = 'policy_ready'
                      AND metadata->>'journey_id' = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (session_id, journey_id),
                )
                existing = cur.fetchone()
                if existing:
                    conn.commit()
                    return str(existing[0])

                cur.execute(
                    """
                    INSERT INTO ai_companion_messages
                        (id, session_id, client_id, role, content, state, metadata)
                    VALUES (%s, %s, %s, 'assistant', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        msg_id,
                        session_id,
                        client_id,
                        content,
                        json.dumps(state),
                        json.dumps(metadata),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else msg_id
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to store policy-ready companion message: {exc}", flush=True)
        return None


def list_companion_turns_for_client(client_id: str, max_turns: int = 200) -> List[Dict[str, Any]]:
    """Return companion chat turns for the APP history channel.

    Companion API probes and the in-app chat both write ``ai_companion_messages``.
    The consultation history endpoint historically only read
    ``consultation_sessions.transcript`` + ``thread_annotations``, so API-only
    journeys looked empty in the UI. This bridges that gap.
    """

    pool = _get_pool()
    if pool is None or not client_id:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.role, m.content, m.created_at
                    FROM ai_companion_messages m
                    JOIN companion_sessions cs
                      ON m.session_id = cs.id::text
                    WHERE cs.client_id = %s
                    ORDER BY m.created_at ASC
                    """,
                    (client_id,),
                )
                rows = cur.fetchall()
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to list companion turns for client: {exc}", flush=True)
        return []

    turns: List[Dict[str, Any]] = []
    for role, content, created_at in rows:
        text = str(content or "").strip()
        if not text:
            continue
        role_s = str(role or "").strip().lower()
        if role_s not in {"user", "assistant"}:
            role_s = "assistant" if role_s in {"agent", "advisor", "rm"} else "user"
        ts = int(created_at.timestamp() * 1000) if created_at else 0
        turns.append({"role": role_s, "text": text, "ts": ts})
    if len(turns) > max_turns:
        turns = turns[-max_turns:]
    return turns
