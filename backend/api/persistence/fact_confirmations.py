"""Dedicated lifecycle store for prompt-bound fact confirmation sets."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn


_MEMORY: Dict[str, Dict[str, Any]] = {}
_LOCK = RLock()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _items_fingerprint(items: List[Dict[str, Any]]) -> str:
    return _fingerprint(
        sorted(
            [
                {
                    "confirmation_item_id": item.get("confirmation_item_id"),
                    "draft_id": item.get("draft_id"),
                    "field": item.get("field"),
                    "atomic_group_id": item.get("atomic_group_id"),
                    "resolution_mode": item.get("resolution_mode"),
                    "value_fingerprint": item.get("value_fingerprint"),
                }
                for item in items
            ],
            key=lambda item: str(item.get("confirmation_item_id") or ""),
        )
    )


def create_confirmation_set(
    *,
    client_id: str,
    companion_session_id: str,
    source_turn_id: str,
    client_file_version: int,
    items: List[Dict[str, Any]],
    ttl_minutes: int = 30,
) -> Dict[str, Any]:
    normalized = []
    for item in items:
        draft_id = str(item.get("draft_id") or "").strip()
        field = str(item.get("field") or "").strip()
        if not draft_id or not field or "value" not in item:
            raise ValueError("confirmation_item_invalid")
        normalized.append({
            "confirmation_item_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:{source_turn_id}:{draft_id}:{field}")),
            "draft_id": draft_id,
            "field": field,
            "atomic_group_id": item.get("atomic_group_id"),
            "resolution_mode": str(item.get("resolution_mode") or "independent"),
            "value": item["value"],
            "value_fingerprint": _fingerprint(item["value"]),
            "status": "pending",
        })
    if not normalized:
        raise ValueError("confirmation_items_required")
    set_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:{source_turn_id}:{_items_fingerprint(normalized)}"))
    now = datetime.now(timezone.utc)
    row = {
        "schema_version": "fact_confirmation_set.v1",
        "confirmation_set_id": set_id,
        "client_id": client_id,
        "companion_session_id": companion_session_id,
        "source_turn_id": source_turn_id,
        "prompt_message_id": None,
        "presented_item_ids": [],
        "presentation_fingerprint": None,
        "client_file_version": max(0, int(client_file_version or 0)),
        "status": "pending",
        "items": normalized,
        "lifecycle_version": 1,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=max(1, ttl_minutes))).isoformat(),
        "resolved_at": None,
        "resolution": None,
    }
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            existing = next((value for value in _MEMORY.values() if value["client_id"] == client_id and value["source_turn_id"] == source_turn_id), None)
            if existing:
                if _items_fingerprint(existing["items"]) != _items_fingerprint(normalized):
                    raise ValueError("confirmation_set_idempotency_conflict")
                return _copy(existing)
            _MEMORY[set_id] = row
            return _copy(row)
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO fact_confirmation_sets
                        (id, client_id, companion_session_id, source_turn_id,
                         client_file_version, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (client_id, source_turn_id) DO NOTHING
                    RETURNING id
                    """,
                    (set_id, client_id, companion_session_id, source_turn_id, row["client_file_version"], row["expires_at"]),
                )
                inserted = cur.fetchone()
                if inserted:
                    for item in normalized:
                        cur.execute(
                            """
                            INSERT INTO fact_confirmation_items
                                (id, confirmation_set_id, draft_id, canonical_field,
                                 atomic_group_id, resolution_mode, value_fingerprint, proposed_value)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                            """,
                            (item["confirmation_item_id"], set_id, item["draft_id"], item["field"], item.get("atomic_group_id"), item["resolution_mode"], item["value_fingerprint"], json.dumps(item["value"])),
                        )
                existing = get_confirmation_set(set_id=set_id, client_id=client_id, connection=conn)
                if existing is None:
                    cur.execute("SELECT id FROM fact_confirmation_sets WHERE client_id = %s AND source_turn_id = %s", (client_id, source_turn_id))
                    existing_id = str(cur.fetchone()[0])
                    existing = get_confirmation_set(set_id=existing_id, client_id=client_id, connection=conn)
                if _items_fingerprint(existing["items"]) != _items_fingerprint(normalized):
                    raise ValueError("confirmation_set_idempotency_conflict")
            conn.commit()
            return existing
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def bind_confirmation_prompt(
    *,
    set_id: str,
    client_id: str,
    companion_session_id: str,
    prompt_message_id: str,
    presented_item_ids: List[str],
) -> Dict[str, Any]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = _owned(set_id, client_id, companion_session_id)
            expected = {item["confirmation_item_id"] for item in row["items"]}
            if set(presented_item_ids) != expected:
                raise ValueError("confirmation_presentation_mismatch")
            fingerprint = _fingerprint({"prompt_message_id": prompt_message_id, "item_ids": presented_item_ids})
            if row["prompt_message_id"] and (row["prompt_message_id"] != prompt_message_id or row["presentation_fingerprint"] != fingerprint):
                raise ValueError("confirmation_prompt_already_bound")
            row.update({"prompt_message_id": prompt_message_id, "presented_item_ids": list(presented_item_ids), "presentation_fingerprint": fingerprint})
            for other in _MEMORY.values():
                if (
                    other["confirmation_set_id"] != set_id
                    and other["client_id"] == client_id
                    and other["companion_session_id"] == companion_session_id
                    and other["status"] == "pending"
                    and other.get("prompt_message_id")
                    and other["created_at"] < row["created_at"]
                ):
                    other["status"] = "expired"
                    other["lifecycle_version"] += 1
            return _copy(row)
    # PostgreSQL binding is a narrow CAS; readback verifies exact scope.
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            current = get_confirmation_set(set_id=set_id, client_id=client_id, connection=conn)
            if current is None or current["companion_session_id"] != companion_session_id:
                raise LookupError("confirmation_set_not_found")
            expected = {item["confirmation_item_id"] for item in current["items"]}
            if set(presented_item_ids) != expected:
                raise ValueError("confirmation_presentation_mismatch")
            fingerprint = _fingerprint({"prompt_message_id": prompt_message_id, "item_ids": presented_item_ids})
            cur.execute(
                """
                UPDATE fact_confirmation_sets
                SET prompt_message_id = %s, presentation_fingerprint = %s,
                    lifecycle_version = lifecycle_version + 1
                WHERE id = %s AND client_id = %s AND status = 'pending'
                  AND (prompt_message_id IS NULL OR (prompt_message_id = %s AND presentation_fingerprint = %s))
                """,
                (prompt_message_id, fingerprint, set_id, client_id, prompt_message_id, fingerprint),
            )
            if cur.rowcount != 1:
                raise ValueError("confirmation_prompt_already_bound")
            cur.execute(
                """
                UPDATE fact_confirmation_sets
                SET status = 'expired', lifecycle_version = lifecycle_version + 1
                WHERE client_id = %s
                  AND companion_session_id = %s
                  AND id <> %s
                  AND status = 'pending'
                  AND prompt_message_id IS NOT NULL
                  AND created_at < (
                      SELECT created_at FROM fact_confirmation_sets WHERE id = %s
                  )
                """,
                (client_id, companion_session_id, set_id, set_id),
            )
        conn.commit()
        return get_confirmation_set(set_id=set_id, client_id=client_id)  # type: ignore[return-value]
    finally:
        pool.putconn(conn)


def get_confirmation_set(*, set_id: str, client_id: str, connection=None) -> Optional[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        with _LOCK:
            row = _MEMORY.get(set_id)
            return _copy(row) if row and row["client_id"] == client_id else None
    own_connection = connection is None
    conn = connection or _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, client_id, companion_session_id, source_turn_id,
                       prompt_message_id, presentation_fingerprint, client_file_version,
                       status, lifecycle_version, created_at, expires_at, resolved_at, resolution
                FROM fact_confirmation_sets WHERE id = %s AND client_id = %s
                """,
                (set_id, client_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """
                SELECT id, draft_id, canonical_field, atomic_group_id, resolution_mode,
                       value_fingerprint, proposed_value, status, final_value, decision
                FROM fact_confirmation_items WHERE confirmation_set_id = %s ORDER BY id
                """,
                (set_id,),
            )
            items = [_item_from_row(item) for item in cur.fetchall()]
            return {
                "schema_version": "fact_confirmation_set.v1", "confirmation_set_id": str(row[0]),
                "client_id": row[1], "companion_session_id": row[2], "source_turn_id": row[3],
                "prompt_message_id": str(row[4]) if row[4] else None,
                "presented_item_ids": [item["confirmation_item_id"] for item in items] if row[4] else [],
                "presentation_fingerprint": row[5], "client_file_version": int(row[6]),
                "status": row[7], "lifecycle_version": int(row[8]),
                "created_at": row[9].isoformat(), "expires_at": row[10].isoformat(),
                "resolved_at": row[11].isoformat() if row[11] else None,
                "resolution": dict(row[12] or {}) if row[12] else None, "items": items,
            }
    finally:
        if own_connection:
            pool.putconn(conn)


def get_latest_bound_confirmation_set(
    *,
    client_id: str,
    companion_session_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the newest live prompt-bound set owned by this session."""

    pool = _get_pool()
    if pool is None:
        with _LOCK:
            now = datetime.now(timezone.utc)
            rows = [
                row
                for row in _MEMORY.values()
                if row["client_id"] == client_id
                and row["companion_session_id"] == companion_session_id
                and row["status"] == "pending"
                and row.get("prompt_message_id")
                and datetime.fromisoformat(row["expires_at"]) > now
            ]
            latest = max(rows, key=lambda row: row["created_at"], default=None)
            return _copy(latest) if latest else None
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM fact_confirmation_sets
                WHERE client_id = %s
                  AND companion_session_id = %s
                  AND status = 'pending'
                  AND prompt_message_id IS NOT NULL
                  AND expires_at > NOW()
                  AND prompt_message_id = (
                      SELECT id
                      FROM ai_companion_messages
                      WHERE client_id = %s
                        AND session_id = %s
                        AND role = 'assistant'
                      ORDER BY created_at DESC
                      LIMIT 1
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    client_id,
                    companion_session_id,
                    client_id,
                    companion_session_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                return None
            return get_confirmation_set(
                set_id=str(row[0]),
                client_id=client_id,
                connection=conn,
            )
    finally:
        pool.putconn(conn)


def resolve_confirmation_set(
    *,
    set_id: str,
    client_id: str,
    companion_session_id: str,
    prompt_message_id: str,
    decisions: List[Dict[str, Any]],
    resolution_result: Dict[str, Any],
) -> Dict[str, Any]:
    pool = _get_pool()
    with _LOCK:
        if pool is None:
            row = _owned(set_id, client_id, companion_session_id)
            if row["status"] != "pending":
                if row.get("resolution") == resolution_result:
                    return _copy(row)
                raise ValueError("confirmation_set_terminal")
            if row["prompt_message_id"] != prompt_message_id or not row["presentation_fingerprint"]:
                raise ValueError("confirmation_prompt_mismatch")
            by_id = {item["confirmation_item_id"]: item for item in row["items"]}
            _apply_decisions(by_id, decisions)
            resolved = [item for item in row["items"] if item["status"] != "pending"]
            row["status"] = "resolved" if len(resolved) == len(row["items"]) else "partially_resolved"
            row["resolution"] = dict(resolution_result)
            row["resolved_at"] = _now_iso()
            row["lifecycle_version"] += 1
            return _copy(row)
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT companion_session_id, prompt_message_id, presentation_fingerprint,
                           status, expires_at, resolution
                    FROM fact_confirmation_sets
                    WHERE id = %s AND client_id = %s
                    FOR UPDATE
                    """,
                    (set_id, client_id),
                )
                current = cur.fetchone()
                if not current or current[0] != companion_session_id:
                    raise LookupError("confirmation_set_not_found")
                stored_resolution = dict(current[5] or {}) if current[5] else None
                if current[3] != "pending":
                    if stored_resolution == resolution_result:
                        conn.commit()
                        return get_confirmation_set(set_id=set_id, client_id=client_id)  # type: ignore[return-value]
                    raise ValueError("confirmation_set_terminal")
                if current[4] <= datetime.now(timezone.utc):
                    cur.execute(
                        "UPDATE fact_confirmation_sets SET status = 'expired', lifecycle_version = lifecycle_version + 1 WHERE id = %s",
                        (set_id,),
                    )
                    raise ValueError("confirmation_set_expired")
                if str(current[1] or "") != prompt_message_id or not current[2]:
                    raise ValueError("confirmation_prompt_mismatch")

                cur.execute(
                    """
                    SELECT id, draft_id, canonical_field, atomic_group_id, resolution_mode,
                           value_fingerprint, proposed_value, status, final_value, decision
                    FROM fact_confirmation_items
                    WHERE confirmation_set_id = %s
                    FOR UPDATE
                    """,
                    (set_id,),
                )
                items = [_item_from_row(item) for item in cur.fetchall()]
                by_id = {item["confirmation_item_id"]: item for item in items}
                _apply_decisions(by_id, decisions)
                for decision in decisions:
                    item = by_id[str(decision["confirmation_item_id"])]
                    cur.execute(
                        """
                        UPDATE fact_confirmation_items
                        SET status = %s, decision = %s, final_value = %s::jsonb
                        WHERE id = %s AND confirmation_set_id = %s AND status = 'pending'
                        """,
                        (
                            item["status"],
                            item["decision"],
                            json.dumps(item.get("final_value")),
                            item["confirmation_item_id"],
                            set_id,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("confirmation_item_terminal")
                remaining = len(items) - len(decisions)
                status = "resolved" if remaining == 0 else "partially_resolved"
                cur.execute(
                    """
                    UPDATE fact_confirmation_sets
                    SET status = %s, resolution = %s::jsonb, resolved_at = NOW(),
                        lifecycle_version = lifecycle_version + 1
                    WHERE id = %s AND status = 'pending'
                    """,
                    (status, json.dumps(resolution_result), set_id),
                )
                if cur.rowcount != 1:
                    raise ValueError("confirmation_set_terminal")
            conn.commit()
            return get_confirmation_set(set_id=set_id, client_id=client_id)  # type: ignore[return-value]
        except Exception:
            conn.rollback()
            raise
    finally:
        pool.putconn(conn)


def _apply_decisions(by_id: Dict[str, Dict[str, Any]], decisions: List[Dict[str, Any]]) -> None:
    seen = set()
    allowed = {"confirmed", "corrected", "rejected", "ambiguous", "deferred"}
    for decision in decisions:
        item_id = str(decision.get("confirmation_item_id") or "")
        outcome = str(decision.get("decision") or "")
        if item_id not in by_id or item_id in seen or outcome not in allowed:
            raise ValueError("confirmation_decision_invalid")
        seen.add(item_id)
        item = by_id[item_id]
        item["decision"] = outcome
        item["status"] = outcome
        item["final_value"] = decision.get("corrected_value") if outcome == "corrected" else item["value"] if outcome == "confirmed" else None


def _owned(set_id: str, client_id: str, session_id: str) -> Dict[str, Any]:
    row = _MEMORY.get(set_id)
    if not row or row["client_id"] != client_id or row["companion_session_id"] != session_id:
        raise LookupError("confirmation_set_not_found")
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        row["status"] = "expired"
        raise ValueError("confirmation_set_expired")
    return row


def _copy(row: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(row, default=str))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_from_row(row: Any) -> Dict[str, Any]:
    return {"confirmation_item_id": str(row[0]), "draft_id": row[1], "field": row[2], "atomic_group_id": row[3], "resolution_mode": row[4], "value_fingerprint": row[5], "value": row[6], "status": row[7], "final_value": row[8], "decision": row[9]}


def _reset_memory_fact_confirmations_for_tests() -> None:
    with _LOCK:
        _MEMORY.clear()
