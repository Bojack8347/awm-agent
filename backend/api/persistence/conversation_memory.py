"""Durable storage and serialization for conversation summaries."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .core import _get_pool, _safe_getconn


_memory_summaries: List[Dict[str, Any]] = []
_memory_summaries_lock = RLock()
_memory_session_locks: Dict[str, RLock] = {}

_SUMMARY_COLUMNS = """
    id, session_id, client_id, tier,
    covered_from_message_id, covered_through_message_id,
    covered_from_created_at, covered_through_created_at,
    source_message_count, source_hash, source_summary_ids, summary,
    carried_artifact_references, model, prompt_version,
    superseded_by, created_at
"""

_RETRIEVAL_COLUMNS = "id, session_id, created_at, role, content"


def _iso(value: Any) -> Optional[str]:
    return (
        value.isoformat()
        if hasattr(value, "isoformat")
        else (str(value) if value else None)
    )


def _row_to_summary(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "session_id": str(row[1]),
        "client_id": str(row[2]),
        "tier": int(row[3]),
        "covered_from_message_id": str(row[4]),
        "covered_through_message_id": str(row[5]),
        "covered_from_created_at": _iso(row[6]),
        "covered_through_created_at": _iso(row[7]),
        "source_message_count": int(row[8]),
        "source_hash": str(row[9]),
        "source_summary_ids": list(row[10] or []),
        "summary": dict(row[11] or {}),
        "carried_artifact_references": list(row[12] or []),
        "model": row[13],
        "prompt_version": str(row[14]),
        "superseded_by": str(row[15]) if row[15] else None,
        "created_at": _iso(row[16]),
    }


def _reset_memory_conversation_summaries_for_tests() -> None:
    with _memory_summaries_lock:
        _memory_summaries.clear()
        _memory_session_locks.clear()


@contextmanager
def conversation_memory_transaction(session_id: str) -> Iterator[Any]:
    """Serialize compaction per session across processes when PostgreSQL exists."""
    pool = _get_pool()
    if pool is None:
        with _memory_summaries_lock:
            lock = _memory_session_locks.setdefault(session_id, RLock())
        with lock:
            with _memory_summaries_lock:
                snapshot = deepcopy(
                    [
                        row
                        for row in _memory_summaries
                        if row["session_id"] == session_id
                    ]
                )
            try:
                yield None
            except Exception:
                with _memory_summaries_lock:
                    _memory_summaries[:] = [
                        row
                        for row in _memory_summaries
                        if row["session_id"] != session_id
                    ]
                    _memory_summaries.extend(snapshot)
                raise
        return

    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("awm-conversation-memory", session_id),
            )
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def validate_session_ownership(
    client_id: str,
    session_id: str,
    *,
    connection: Any = None,
) -> bool:
    pool = _get_pool()
    if pool is None:
        from . import companion

        with companion._memory_messages_lock:  # pylint: disable=protected-access
            rows = list(
                companion._memory_messages.get(session_id, [])
            )  # pylint: disable=protected-access
        return bool(rows) and all(row.get("client_id") == client_id for row in rows)

    owns_connection = connection is None
    conn = connection or _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE client_id = %s),
                    COUNT(*) FILTER (WHERE client_id IS DISTINCT FROM %s)
                FROM ai_companion_messages
                WHERE session_id = %s
                """,
                (client_id, client_id, session_id),
            )
            row = cur.fetchone()
            return bool(row and int(row[0] or 0) > 0 and int(row[1] or 0) == 0)
    finally:
        if owns_connection:
            pool.putconn(conn)


def list_active_summaries(
    client_id: str,
    session_id: str,
    *,
    connection: Any = None,
) -> List[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _memory_summaries_lock:
            rows = [
                dict(row)
                for row in _memory_summaries
                if row["client_id"] == client_id
                and row["session_id"] == session_id
                and row.get("superseded_by") is None
            ]
        return sorted(rows, key=lambda row: (row["covered_from_created_at"], row["id"]))

    owns_connection = connection is None
    conn = connection or _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SUMMARY_COLUMNS}
                FROM conversation_context_summaries
                WHERE client_id = %s
                  AND session_id = %s
                  AND superseded_by IS NULL
                ORDER BY covered_from_created_at ASC, id ASC
                """,
                (client_id, session_id),
            )
            return [_row_to_summary(row) for row in cur.fetchall()]
    finally:
        if owns_connection:
            pool.putconn(conn)


def insert_summary(
    *,
    client_id: str,
    session_id: str,
    tier: int,
    covered_from_message_id: str,
    covered_through_message_id: str,
    covered_from_created_at: str,
    covered_through_created_at: str,
    source_message_count: int,
    source_hash: str,
    source_summary_ids: Sequence[str],
    summary: Dict[str, Any],
    carried_artifact_references: Sequence[Dict[str, Any]],
    model: Optional[str],
    prompt_version: str,
    connection: Any = None,
) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        if not validate_session_ownership(client_id, session_id):
            raise PermissionError(
                "conversation session does not belong to the current client"
            )
        with _memory_summaries_lock:
            existing = next(
                (
                    row
                    for row in _memory_summaries
                    if row["client_id"] == client_id
                    and row["session_id"] == session_id
                    and row["source_hash"] == source_hash
                ),
                None,
            )
            if existing is not None:
                return dict(existing)
            row = {
                "id": str(uuid.uuid4()),
                "session_id": session_id,
                "client_id": client_id,
                "tier": int(tier),
                "covered_from_message_id": covered_from_message_id,
                "covered_through_message_id": covered_through_message_id,
                "covered_from_created_at": covered_from_created_at,
                "covered_through_created_at": covered_through_created_at,
                "source_message_count": int(source_message_count),
                "source_hash": source_hash,
                "source_summary_ids": list(source_summary_ids),
                "summary": dict(summary),
                "carried_artifact_references": [
                    dict(item) for item in carried_artifact_references
                ],
                "model": model,
                "prompt_version": prompt_version,
                "superseded_by": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _memory_summaries.append(row)
            return dict(row)

    owns_connection = connection is None
    conn = connection or _safe_getconn(pool)
    try:
        if not validate_session_ownership(client_id, session_id, connection=conn):
            raise PermissionError(
                "conversation session does not belong to the current client"
            )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO conversation_context_summaries (
                    session_id, client_id, tier,
                    covered_from_message_id, covered_through_message_id,
                    covered_from_created_at, covered_through_created_at,
                    source_message_count, source_hash, source_summary_ids, summary,
                    carried_artifact_references, model, prompt_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (client_id, session_id, source_hash) DO NOTHING
                RETURNING {_SUMMARY_COLUMNS}
                """,
                (
                    session_id,
                    client_id,
                    tier,
                    covered_from_message_id,
                    covered_through_message_id,
                    covered_from_created_at,
                    covered_through_created_at,
                    source_message_count,
                    source_hash,
                    json.dumps(list(source_summary_ids)),
                    json.dumps(summary),
                    json.dumps(list(carried_artifact_references)),
                    model,
                    prompt_version,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""
                    SELECT {_SUMMARY_COLUMNS}
                    FROM conversation_context_summaries
                    WHERE client_id = %s AND session_id = %s AND source_hash = %s
                    """,
                    (client_id, session_id, source_hash),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("conversation summary insert returned no row")
        if owns_connection:
            conn.commit()
        return _row_to_summary(row)
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if owns_connection:
            pool.putconn(conn)


def supersede_summaries(
    *,
    client_id: str,
    session_id: str,
    summary_ids: Sequence[str],
    superseded_by: str,
    connection: Any = None,
) -> int:
    ids = [str(item) for item in summary_ids if str(item)]
    if not ids:
        return 0
    pool = _get_pool()
    if pool is None:
        if not validate_session_ownership(client_id, session_id):
            raise PermissionError(
                "conversation session does not belong to the current client"
            )
        updated = 0
        with _memory_summaries_lock:
            for row in _memory_summaries:
                if (
                    row["id"] in ids
                    and row["client_id"] == client_id
                    and row["session_id"] == session_id
                    and row.get("superseded_by") is None
                ):
                    row["superseded_by"] = superseded_by
                    updated += 1
        return updated

    owns_connection = connection is None
    conn = connection or _safe_getconn(pool)
    try:
        if not validate_session_ownership(client_id, session_id, connection=conn):
            raise PermissionError(
                "conversation session does not belong to the current client"
            )
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE conversation_context_summaries
                SET superseded_by = %s
                WHERE client_id = %s
                  AND session_id = %s
                  AND id = ANY(%s::uuid[])
                  AND superseded_by IS NULL
                """,
                (superseded_by, client_id, session_id, ids),
            )
            updated = int(cur.rowcount or 0)
        if owns_connection:
            conn.commit()
        return updated
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if owns_connection:
            pool.putconn(conn)


def _retrieved_message(row: Any, *, rank: Optional[float] = None) -> Dict[str, Any]:
    if isinstance(row, dict):
        result = {
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "created_at": _iso(row.get("created_at")),
            "role": str(row["role"]),
            "content": str(row["content"]),
        }
    else:
        result = {
            "id": str(row[0]),
            "session_id": str(row[1]),
            "created_at": _iso(row[2]),
            "role": str(row[3]),
            "content": str(row[4]),
        }
    if rank is not None:
        result["rank"] = float(rank)
    return result


def _retrieval_mode(
    *,
    message_ids: Optional[Sequence[str]],
    from_message_id: Optional[str],
    through_message_id: Optional[str],
    query: Optional[str],
) -> str:
    has_ids = bool(message_ids)
    has_range = bool(from_message_id or through_message_id)
    has_query = bool(str(query or "").strip())
    if has_range and not (from_message_id and through_message_id):
        raise ValueError(
            "from_message_id and through_message_id must be supplied together"
        )
    if sum((has_ids, has_range, has_query)) != 1:
        raise ValueError(
            "supply exactly one of message_ids, a complete range, or query"
        )
    return "message_ids" if has_ids else ("range" if has_range else "query")


def _bounded_retrieval_values(max_results: int, context_window: int) -> tuple[int, int]:
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= 100
    ):
        raise ValueError("max_results must be between 1 and 100")
    if (
        not isinstance(context_window, int)
        or isinstance(context_window, bool)
        or not 0 <= context_window <= 10
    ):
        raise ValueError("context_window must be between 0 and 10")
    return max_results, context_window


def _memory_retrieval(
    *,
    client_id: str,
    session_id: str,
    mode: str,
    message_ids: Sequence[str],
    from_message_id: Optional[str],
    through_message_id: Optional[str],
    query: str,
    max_results: int,
    context_window: int,
) -> Dict[str, Any]:
    from . import companion

    with companion._memory_messages_lock:  # pylint: disable=protected-access
        rows = list(
            companion._memory_messages.get(session_id, [])
        )  # pylint: disable=protected-access
    rows.sort(
        key=lambda row: (_iso(row.get("created_at")) or "", str(row.get("id") or ""))
    )
    if not rows or any(row.get("client_id") != client_id for row in rows):
        raise PermissionError(
            "conversation session does not belong to the current client"
        )

    truncated = False
    if mode == "message_ids":
        requested = set(message_ids)
        all_matches = [row for row in rows if str(row["id"]) in requested]
        matches = all_matches[:max_results]
        truncated = len(all_matches) > max_results
    elif mode == "range":
        positions = {str(row["id"]): index for index, row in enumerate(rows)}
        if from_message_id not in positions or through_message_id not in positions:
            raise ValueError(
                "conversation range boundary was not found in this session"
            )
        start = positions[str(from_message_id)]
        end = positions[str(through_message_id)]
        if start > end:
            raise ValueError("conversation range boundaries are reversed")
        all_matches = rows[start : end + 1]
        matches = all_matches[:max_results]
        truncated = len(all_matches) > max_results
    else:
        terms = re.findall(r"[\w']+", query.casefold())
        if not terms:
            raise ValueError("query must contain searchable text")
        scored = []
        for row in rows:
            content = str(row.get("content") or "").casefold()
            if all(term in content for term in terms):
                scored.append((sum(content.count(term) for term in terms), row))
        scored.sort(
            key=lambda item: (
                item[0],
                _iso(item[1].get("created_at")) or "",
                str(item[1].get("id") or ""),
            ),
            reverse=True,
        )
        truncated = len(scored) > max_results
        matches = [row for _score, row in scored[:max_results]]

    positions = {str(row["id"]): index for index, row in enumerate(rows)}
    match_ids = {str(row["id"]) for row in matches}
    context_indexes: set[int] = set()
    for message_id in match_ids:
        position = positions[message_id]
        context_indexes.update(
            range(
                max(0, position - context_window),
                min(len(rows), position + context_window + 1),
            )
        )
    context_rows = [
        rows[index]
        for index in sorted(context_indexes)
        if str(rows[index]["id"]) not in match_ids
    ]
    if mode == "query":
        score_by_id = {str(row["id"]): score for score, row in scored[:max_results]}
        public_matches = [
            _retrieved_message(row, rank=score_by_id[str(row["id"])]) for row in matches
        ]
    else:
        public_matches = [_retrieved_message(row) for row in matches]
    return {
        "mode": mode,
        "matches": public_matches,
        "context_messages": [_retrieved_message(row) for row in context_rows],
        "truncated": truncated,
        "missing_message_ids": (
            sorted(set(message_ids) - {str(row["id"]) for row in rows})
            if mode == "message_ids"
            else []
        ),
    }


def _database_context_rows(
    cur: Any,
    *,
    client_id: str,
    session_id: str,
    target_ids: Sequence[str],
    context_window: int,
) -> List[Dict[str, Any]]:
    if not target_ids or context_window == 0:
        return []
    cur.execute(
        f"""
        WITH ordered_messages AS (
            SELECT {_RETRIEVAL_COLUMNS},
                   ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) AS row_number
            FROM ai_companion_messages
            WHERE client_id = %s AND session_id = %s
        ), target_positions AS (
            SELECT row_number
            FROM ordered_messages
            WHERE id = ANY(%s::uuid[])
        )
        SELECT DISTINCT {_RETRIEVAL_COLUMNS}
        FROM ordered_messages message
        WHERE message.id <> ALL(%s::uuid[])
          AND EXISTS (
              SELECT 1
              FROM target_positions target
              WHERE message.row_number BETWEEN
                    target.row_number - %s AND target.row_number + %s
          )
        ORDER BY created_at ASC, id ASC
        """,
        (
            client_id,
            session_id,
            list(target_ids),
            list(target_ids),
            context_window,
            context_window,
        ),
    )
    return [_retrieved_message(row) for row in cur.fetchall()]


def retrieve_conversation_history(
    client_id: str,
    session_id: str,
    *,
    message_ids: Optional[Sequence[str]] = None,
    from_message_id: Optional[str] = None,
    through_message_id: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 20,
    context_window: int = 2,
    connection: Any = None,
) -> Dict[str, Any]:
    """Read exact original chat rows with strict client/session isolation."""
    normalized_ids = tuple(
        dict.fromkeys(
            str(item).strip() for item in (message_ids or []) if str(item).strip()
        )
    )
    normalized_from = str(from_message_id or "").strip() or None
    normalized_through = str(through_message_id or "").strip() or None
    normalized_query = str(query or "").strip()
    mode = _retrieval_mode(
        message_ids=normalized_ids,
        from_message_id=normalized_from,
        through_message_id=normalized_through,
        query=normalized_query,
    )
    max_results, context_window = _bounded_retrieval_values(max_results, context_window)
    if len(normalized_ids) > 100 or any(len(item) > 100 for item in normalized_ids):
        raise ValueError("message_ids must contain at most 100 valid ids")
    if normalized_from and len(normalized_from) > 100:
        raise ValueError("from_message_id is too long")
    if normalized_through and len(normalized_through) > 100:
        raise ValueError("through_message_id is too long")
    if len(normalized_query) > 500:
        raise ValueError("query must contain at most 500 characters")

    pool = _get_pool()
    if pool is None:
        return _memory_retrieval(
            client_id=client_id,
            session_id=session_id,
            mode=mode,
            message_ids=normalized_ids,
            from_message_id=normalized_from,
            through_message_id=normalized_through,
            query=normalized_query,
            max_results=max_results,
            context_window=context_window,
        )

    for message_id in (*normalized_ids, normalized_from, normalized_through):
        if message_id is None:
            continue
        try:
            uuid.UUID(message_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("conversation message ids must be UUIDs") from exc

    owns_connection = connection is None
    conn = connection or _safe_getconn(pool)
    try:
        if not validate_session_ownership(client_id, session_id, connection=conn):
            raise PermissionError(
                "conversation session does not belong to the current client"
            )
        with conn.cursor() as cur:
            truncated = False
            missing_message_ids: List[str] = []
            if mode == "message_ids":
                cur.execute(
                    f"""
                    SELECT {_RETRIEVAL_COLUMNS}
                    FROM ai_companion_messages
                    WHERE client_id = %s AND session_id = %s
                      AND id = ANY(%s::uuid[])
                    ORDER BY created_at ASC, id ASC
                    """,
                    (client_id, session_id, list(normalized_ids)),
                )
                all_rows = cur.fetchall()
                found_ids = {str(row[0]) for row in all_rows}
                missing_message_ids = sorted(set(normalized_ids) - found_ids)
                truncated = len(all_rows) > max_results
                match_rows = all_rows[:max_results]
                matches = [_retrieved_message(row) for row in match_rows]
            elif mode == "range":
                cur.execute(
                    """
                    SELECT id, created_at
                    FROM ai_companion_messages
                    WHERE client_id = %s AND session_id = %s
                      AND id = ANY(%s::uuid[])
                    """,
                    (client_id, session_id, [normalized_from, normalized_through]),
                )
                boundaries = {str(row[0]): row[1] for row in cur.fetchall()}
                if (
                    normalized_from not in boundaries
                    or normalized_through not in boundaries
                ):
                    raise ValueError(
                        "conversation range boundary was not found in this session"
                    )
                start_key = (boundaries[normalized_from], normalized_from)
                end_key = (boundaries[normalized_through], normalized_through)
                if start_key > end_key:
                    raise ValueError("conversation range boundaries are reversed")
                cur.execute(
                    f"""
                    SELECT {_RETRIEVAL_COLUMNS}
                    FROM ai_companion_messages
                    WHERE client_id = %s AND session_id = %s
                      AND (created_at, id) >= (%s, %s::uuid)
                      AND (created_at, id) <= (%s, %s::uuid)
                    ORDER BY created_at ASC, id ASC
                    LIMIT %s
                    """,
                    (
                        client_id,
                        session_id,
                        boundaries[normalized_from],
                        normalized_from,
                        boundaries[normalized_through],
                        normalized_through,
                        max_results + 1,
                    ),
                )
                match_rows = cur.fetchall()
                truncated = len(match_rows) > max_results
                match_rows = match_rows[:max_results]
                matches = [_retrieved_message(row) for row in match_rows]
            else:
                cur.execute(
                    f"""
                    SELECT {_RETRIEVAL_COLUMNS},
                           ts_rank_cd(
                               to_tsvector('english', content),
                               plainto_tsquery('english', %s)
                           ) AS rank
                    FROM ai_companion_messages
                    WHERE client_id = %s AND session_id = %s
                      AND to_tsvector('english', content)
                          @@ plainto_tsquery('english', %s)
                    ORDER BY rank DESC, created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (
                        normalized_query,
                        client_id,
                        session_id,
                        normalized_query,
                        max_results + 1,
                    ),
                )
                match_rows = cur.fetchall()
                truncated = len(match_rows) > max_results
                match_rows = match_rows[:max_results]
                matches = [_retrieved_message(row, rank=row[5]) for row in match_rows]

            target_ids = [str(row[0]) for row in match_rows]
            context_messages = _database_context_rows(
                cur,
                client_id=client_id,
                session_id=session_id,
                target_ids=target_ids,
                context_window=context_window,
            )
            return {
                "mode": mode,
                "matches": matches,
                "context_messages": context_messages,
                "truncated": truncated,
                "missing_message_ids": missing_message_ids,
            }
    finally:
        if owns_connection:
            pool.putconn(conn)
