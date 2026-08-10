"""Persistence helpers for MVP UI business objects.

The MVP layer stores structured UI-facing records while external integrations
are still mocked. IDs are text so mobile can safely deep-link to prefixed
objects such as ``proposal_...`` and ``policy_...``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .core import _get_pool, _pg_json, _safe_getconn


class MvpPersistenceError(RuntimeError):
    """Typed failure raised when an MVP write cannot be persisted."""

    code = "unknown_persistence_failure"


class ClientIdentityMissingError(MvpPersistenceError):
    code = "client_identity_missing"


class MvpPersistenceValidationError(MvpPersistenceError):
    code = "persistence_validation_failed"


class MvpDatabaseUnavailableError(MvpPersistenceError):
    code = "database_unavailable"


class MvpSchemaOutdatedError(MvpPersistenceError):
    code = "schema_outdated"


def _typed_write_error(exc: Exception, *, operation: str, table: str) -> MvpPersistenceError:
    """Classify structural PostgreSQL failures instead of swallowing them."""

    code = str(getattr(exc, "pgcode", "") or "")
    name = exc.__class__.__name__
    constraint = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
    message = str(exc)
    if code == "23503" and (
        constraint.endswith("_client") or 'not present in table "clients"' in message
    ):
        error: MvpPersistenceError = ClientIdentityMissingError(
            f"client_identity_missing: {operation} {table} requires an existing clients row"
        )
    elif code == "42P01" or name == "UndefinedTable":
        error = MvpSchemaOutdatedError(f"schema_outdated: {table} is unavailable")
    elif code.startswith("23") or name in {
        "CheckViolation",
        "ForeignKeyViolation",
        "IntegrityError",
        "NotNullViolation",
        "UniqueViolation",
    }:
        error = MvpPersistenceValidationError(
            f"persistence_validation_failed: {operation} {table} violated a constraint"
        )
    elif code.startswith("08") or name in {"InterfaceError", "OperationalError"}:
        error = MvpDatabaseUnavailableError(
            f"database_unavailable: {operation} {table} could not reach PostgreSQL"
        )
    else:
        error = MvpPersistenceError(
            f"unknown_persistence_failure: {operation} {table} failed"
        )
    return error


def _raise_if_client_identity_missing(
    exc: Exception,
    *,
    operation: str,
    table: str,
) -> None:
    typed = _typed_write_error(exc, operation=operation, table=table)
    if isinstance(typed, ClientIdentityMissingError):
        raise typed from exc


_MEMORY: Dict[str, Any] = {
    "onboarding": {},
    "conversations": {},
    "messages": {},
    "client_identity_profiles": {},
    "client_consents": {},
    "external_connections": {},
    "data_permissions": {},
    "mvp_artifacts": {},
    "mvp_executions": {},
    "mvp_policies": {},
    "mvp_holdings": {},
}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def upsert_onboarding_status(*, client_id: str, current_step: str, status: str, completed_steps: List[str] | None = None) -> Dict[str, Any] | None:
    pool = _get_pool()
    if pool is None:
        existing = _MEMORY["onboarding"].get(client_id) or {}
        row = {
            "client_id": client_id,
            "current_step": current_step,
            "status": status,
            "account_opening_status": status,
            "advisor_onboarding_status": existing.get("advisor_onboarding_status") or "not_started",
            "completed_steps": completed_steps or [],
            "created_at": None,
            "updated_at": None,
        }
        _MEMORY["onboarding"][client_id] = row
        return row
    try:
        conn = _safe_getconn(pool)
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                    """
                    INSERT INTO client_onboarding_status
                        (client_id, current_step, status, account_opening_status,
                         completed_steps, updated_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                    ON CONFLICT (client_id) DO UPDATE SET
                        current_step = EXCLUDED.current_step,
                        status = EXCLUDED.status,
                        account_opening_status = EXCLUDED.account_opening_status,
                        completed_steps = EXCLUDED.completed_steps,
                        updated_at = NOW()
                    RETURNING client_id, current_step, status, completed_steps, created_at,
                              updated_at, account_opening_status, advisor_onboarding_status
                    """,
                    (client_id, current_step, status, status, _pg_json(completed_steps or [])),
                    )
                    row = cur.fetchone()
            except Exception as exc:
                conn.rollback()
                if getattr(exc, "pgcode", None) != "42703":
                    raise
                # Rolling deploy compatibility until the additive migration lands.
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO client_onboarding_status
                            (client_id, current_step, status, completed_steps, updated_at)
                        VALUES (%s, %s, %s, %s::jsonb, NOW())
                        ON CONFLICT (client_id) DO UPDATE SET
                            current_step = EXCLUDED.current_step,
                            status = EXCLUDED.status,
                            completed_steps = EXCLUDED.completed_steps,
                            updated_at = NOW()
                        RETURNING client_id, current_step, status, completed_steps, created_at, updated_at
                        """,
                        (client_id, current_step, status, _pg_json(completed_steps or [])),
                    )
                    row = cur.fetchone()
            conn.commit()
            return _onboarding_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="upsert",
            table="client_onboarding_status",
        )
        print(f"[db] upsert_onboarding_status failed: {exc}", flush=True)
        return None


def get_onboarding_status(client_id: str) -> Dict[str, Any] | None:
    pool = _get_pool()
    if pool is None:
        return _MEMORY["onboarding"].get(client_id)
    try:
        conn = _safe_getconn(pool)
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                    """
                    SELECT client_id, current_step, status, completed_steps, created_at,
                           updated_at, account_opening_status, advisor_onboarding_status
                    FROM client_onboarding_status WHERE client_id = %s
                    """,
                    (client_id,),
                    )
                    row = cur.fetchone()
            except Exception as exc:
                conn.rollback()
                if getattr(exc, "pgcode", None) != "42703":
                    raise
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT client_id, current_step, status, completed_steps, created_at, updated_at
                        FROM client_onboarding_status WHERE client_id = %s
                        """,
                        (client_id,),
                    )
                    row = cur.fetchone()
            return _onboarding_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_onboarding_status failed: {exc}", flush=True)
        return None


_ADVISOR_ONBOARDING_TRANSITIONS = {
    "not_started": {"in_progress"},
    "in_progress": {"in_progress", "complete"},
    "complete": {"complete", "in_progress"},
}


def transition_advisor_onboarding_status(
    *,
    client_id: str,
    target_status: str,
    allow_reonboarding: bool = False,
) -> Dict[str, Any]:
    """Apply the advisor-discovery state machine used by objective updates."""

    target = str(target_status or "").strip().lower()
    if target == "completed":
        target = "complete"
    current_row = get_onboarding_status(client_id) or {}
    current = str(current_row.get("advisor_onboarding_status") or "not_started").lower()
    if target not in _ADVISOR_ONBOARDING_TRANSITIONS.get(current, set()):
        return {"ok": False, "error": "advisor_onboarding_transition_invalid", "from": current, "to": target}
    if current == "complete" and target == "in_progress" and not allow_reonboarding:
        return {"ok": False, "error": "explicit_reonboarding_required", "from": current, "to": target}

    pool = _get_pool()
    if pool is None:
        row = dict(current_row)
        row.update(
            {
                "client_id": client_id,
                "status": row.get("account_opening_status") or row.get("status") or "not_started",
                "account_opening_status": row.get("account_opening_status") or row.get("status") or "not_started",
                "advisor_onboarding_status": target,
                "current_step": row.get("current_step"),
                "completed_steps": row.get("completed_steps") or [],
            }
        )
        _MEMORY["onboarding"][client_id] = row
        return {"ok": True, "from": current, "to": target, "onboarding": row}

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO client_onboarding_status
                        (client_id, advisor_onboarding_status, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (client_id) DO UPDATE SET
                        advisor_onboarding_status = EXCLUDED.advisor_onboarding_status,
                        updated_at = NOW()
                    RETURNING client_id, current_step, status, completed_steps, created_at,
                              updated_at, account_opening_status, advisor_onboarding_status
                    """,
                    (client_id, target),
                )
                row = cur.fetchone()
            conn.commit()
            return {"ok": True, "from": current, "to": target, "onboarding": _onboarding_row(row)}
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="transition",
            table="client_onboarding_status",
        )
        print(f"[db] transition_advisor_onboarding_status failed: {exc}", flush=True)
        return {"ok": False, "error": "advisor_onboarding_persistence_failed", "detail": str(exc)}


def save_identity_profile(*, client_id: str, identity: Dict[str, Any], kyc_result: Dict[str, Any]) -> Dict[str, Any] | None:
    return _insert_record(
        "client_identity_profiles",
        "identity",
        client_id=client_id,
        record_type="identity_profile",
        status=kyc_result.get("status") or "pending",
        payload={"identity": identity, "kyc": kyc_result},
    )


def save_consent(*, client_id: str, consent_type: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    return _insert_record(
        "client_consents",
        "consent",
        client_id=client_id,
        record_type=consent_type,
        status="accepted",
        payload=payload,
    )


def save_data_permission(*, client_id: str, scopes: List[str], payload: Dict[str, Any]) -> Dict[str, Any] | None:
    return _insert_record(
        "data_permissions",
        "perm",
        client_id=client_id,
        record_type="data_permission",
        status="active",
        payload={"scopes": scopes, **payload},
    )


def save_external_connection(*, client_id: str, connection_type: str, provider_result: Dict[str, Any]) -> Dict[str, Any] | None:
    observed_result = dict(provider_result)
    observed_result.setdefault("observed_at", datetime.now(timezone.utc).isoformat())
    return _insert_record(
        "external_connections",
        "conn",
        client_id=client_id,
        record_type=connection_type,
        status=observed_result.get("status") or "connected",
        payload=observed_result,
    )


def list_external_connections(*, client_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return durable provider connections for Client File projection."""

    return _list_records("external_connections", client_id=client_id, status=status)


def create_conversation(*, client_id: str, scope: str, context: Dict[str, Any]) -> Dict[str, Any] | None:
    pool = _get_pool()
    conversation_id = _id("conv")
    if pool is None:
        row = {
            "id": conversation_id,
            "conversation_id": conversation_id,
            "client_id": client_id,
            "scope": scope,
            "context": context,
            "status": "open",
            "created_at": None,
            "updated_at": None,
        }
        _MEMORY["conversations"][conversation_id] = row
        return row
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (id, client_id, scope, context)
                    VALUES (%s, %s, %s, %s::jsonb)
                    RETURNING id, client_id, scope, context, status, created_at, updated_at
                    """,
                    (conversation_id, client_id, scope, _pg_json(context)),
                )
                row = cur.fetchone()
            conn.commit()
            return _conversation_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="insert",
            table="conversations",
        )
        print(f"[db] create_conversation failed: {exc}", flush=True)
        return None


def get_conversation(*, conversation_id: str, client_id: Optional[str] = None) -> Dict[str, Any] | None:
    pool = _get_pool()
    if pool is None:
        row = _MEMORY["conversations"].get(conversation_id)
        if row and (not client_id or row.get("client_id") == client_id):
            return row
        return None
    params: List[Any] = [conversation_id]
    client_clause = ""
    if client_id:
        client_clause = "AND client_id = %s"
        params.append(client_id)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, scope, context, status, created_at, updated_at
                    FROM conversations WHERE id = %s {client_clause}
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                return _conversation_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_conversation failed: {exc}", flush=True)
        return None


def add_conversation_message(
    *,
    conversation_id: str,
    client_id: str,
    role: str,
    message_type: str,
    text: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    payload: Optional[Dict[str, Any]] = None,
    event_id: Optional[str] = None,
    run_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, Any] | None:
    pool = _get_pool()
    message_id = _id("msg")
    if pool is None:
        row = {
            "id": message_id,
            "message_id": message_id,
            "conversation_id": conversation_id,
            "client_id": client_id,
            "role": role,
            "message_type": message_type,
            "text": text,
            "attachments": attachments or [],
            "payload": payload or {},
            "event_id": event_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "created_at": None,
        }
        _MEMORY["messages"].setdefault(conversation_id, []).append(row)
        return row
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversation_messages
                        (id, conversation_id, client_id, role, message_type, text, attachments, payload, event_id, run_id, trace_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    RETURNING id, conversation_id, client_id, role, message_type, text, attachments, payload, event_id, run_id, trace_id, created_at
                    """,
                    (
                        message_id,
                        conversation_id,
                        client_id,
                        role,
                        message_type,
                        text,
                        _pg_json(attachments or []),
                        _pg_json(payload or {}),
                        event_id,
                        run_id,
                        trace_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _message_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="insert",
            table="conversation_messages",
        )
        print(f"[db] add_conversation_message failed: {exc}", flush=True)
        return None


def list_conversation_messages(*, conversation_id: str, client_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        return [
            message for message in _MEMORY["messages"].get(conversation_id, [])
            if message.get("client_id") == client_id
        ][:max(1, min(int(limit or 100), 500))]
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, conversation_id, client_id, role, message_type, text, attachments, payload, event_id, run_id, trace_id, created_at
                    FROM conversation_messages
                    WHERE conversation_id = %s AND client_id = %s
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (conversation_id, client_id, max(1, min(int(limit or 100), 500))),
                )
                return [_message_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_conversation_messages failed: {exc}", flush=True)
        return []


def save_artifact(
    *,
    client_id: str,
    artifact_type: str,
    title: str,
    payload: Dict[str, Any],
    related_type: Optional[str] = None,
    related_id: Optional[str] = None,
    status: str = "ready",
    artifact_id: Optional[str] = None,
) -> Dict[str, Any] | None:
    return _insert_record(
        "mvp_artifacts",
        {"proposal": "proposal", "monitoring_report": "report", "policy_update": "update", "projection": "projection"}.get(artifact_type, "artifact"),
        client_id=client_id,
        record_type=artifact_type,
        status=status,
        payload={**payload, "title": title},
        related_type=related_type,
        related_id=related_id,
        record_id=artifact_id,
    )


def get_artifact(*, artifact_id: str, client_id: Optional[str] = None) -> Dict[str, Any] | None:
    return _get_record("mvp_artifacts", artifact_id, client_id)


def update_artifact(*, artifact_id: str, client_id: str, status: str, payload_patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any] | None:
    return _update_record_payload("mvp_artifacts", record_id=artifact_id, client_id=client_id, status=status, payload_patch=payload_patch or {})


def list_artifacts(*, client_id: str, artifact_type: Optional[str] = None, related_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return _list_records("mvp_artifacts", client_id=client_id, record_type=artifact_type, related_id=related_id)


def upsert_cashflow_analysis(*, client_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist an immutable, deterministic cash-flow analysis snapshot."""

    analysis_id = str(payload.get("analysis_id") or "").strip()
    if not analysis_id.startswith("cashflow_"):
        return {"ok": False, "error": "cashflow_analysis_id_invalid"}
    record_payload = {**payload, "title": "Validated cash-flow analysis"}
    session_id = str(payload.get("session_id") or "").strip() or None
    pool = _get_pool()
    if pool is None:
        existing = _MEMORY["mvp_artifacts"].get(analysis_id)
        if existing is not None:
            if existing.get("client_id") != client_id:
                return {"ok": False, "error": "cashflow_analysis_id_collision"}
            return {"ok": True, "idempotent_replay": True, "artifact": existing}
        artifact = {
            "id": analysis_id,
            "client_id": client_id,
            "record_type": "cashflow_analysis",
            "status": "ready",
            "related_type": "companion_session" if session_id else None,
            "related_id": session_id,
            "secondary_id": None,
            "payload": record_payload,
            "created_at": None,
            "updated_at": None,
        }
        _MEMORY["mvp_artifacts"][analysis_id] = artifact
        return {"ok": True, "idempotent_replay": False, "artifact": artifact}
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mvp_artifacts
                        (id, client_id, record_type, status, related_type, related_id, payload)
                    VALUES (%s, %s, 'cashflow_analysis', 'ready', %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        analysis_id,
                        client_id,
                        "companion_session" if session_id else None,
                        session_id,
                        _pg_json(record_payload),
                    ),
                )
                inserted = cur.rowcount == 1
                cur.execute(
                    """
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_artifacts
                    WHERE id = %s AND client_id = %s AND record_type = 'cashflow_analysis'
                    """,
                    (analysis_id, client_id),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                return {"ok": False, "error": "cashflow_analysis_id_collision"}
            return {
                "ok": True,
                "idempotent_replay": not inserted,
                "artifact": _record_row(row),
            }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="upsert",
            table="mvp_artifacts",
        )
        print(f"[db] upsert cashflow analysis failed: {exc}", flush=True)
        return {"ok": False, "error": "cashflow_analysis_save_failed", "detail": str(exc)}


def get_cashflow_analysis_snapshot(
    *,
    client_id: str,
    analysis_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any] | None:
    """Load one client-owned snapshot, defaulting to the latest in the session."""

    clean_id = str(analysis_id or "").strip()
    clean_session = str(session_id or "").strip()
    pool = _get_pool()
    if pool is None:
        rows = list(_MEMORY["mvp_artifacts"].values())
        for row in reversed(rows):
            if row.get("client_id") != client_id or row.get("record_type") != "cashflow_analysis":
                continue
            if clean_id and row.get("id") != clean_id:
                continue
            if not clean_id and clean_session and row.get("related_id") != clean_session:
                continue
            return row
        return None
    where = ["client_id = %s", "record_type = 'cashflow_analysis'"]
    params: List[Any] = [client_id]
    if clean_id:
        where.append("id = %s")
        params.append(clean_id)
    elif clean_session:
        where.append("related_id = %s")
        params.append(clean_session)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_artifacts
                    WHERE {" AND ".join(where)}
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                return _record_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get cashflow analysis failed: {exc}", flush=True)
        return None


def upsert_asset_allocation_analysis(
    *,
    client_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist an immutable, deterministic asset-allocation analysis snapshot."""

    analysis_id = str(payload.get("analysis_id") or "").strip()
    if not analysis_id.startswith("allocation_"):
        return {"ok": False, "error": "asset_allocation_analysis_id_invalid"}
    record_payload = {**payload, "title": "Validated asset-allocation analysis"}
    session_id = str(payload.get("session_id") or "").strip() or None
    pool = _get_pool()
    if pool is None:
        existing = _MEMORY["mvp_artifacts"].get(analysis_id)
        if existing is not None:
            if existing.get("client_id") != client_id:
                return {
                    "ok": False,
                    "error": "asset_allocation_analysis_id_collision",
                }
            return {"ok": True, "idempotent_replay": True, "artifact": existing}
        artifact = {
            "id": analysis_id,
            "client_id": client_id,
            "record_type": "asset_allocation_analysis",
            "status": "ready",
            "related_type": "companion_session" if session_id else None,
            "related_id": session_id,
            "secondary_id": None,
            "payload": record_payload,
            "created_at": None,
            "updated_at": None,
        }
        _MEMORY["mvp_artifacts"][analysis_id] = artifact
        return {"ok": True, "idempotent_replay": False, "artifact": artifact}
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mvp_artifacts
                        (id, client_id, record_type, status, related_type, related_id, payload)
                    VALUES (%s, %s, 'asset_allocation_analysis', 'ready', %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        analysis_id,
                        client_id,
                        "companion_session" if session_id else None,
                        session_id,
                        _pg_json(record_payload),
                    ),
                )
                inserted = cur.rowcount == 1
                cur.execute(
                    """
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_artifacts
                    WHERE id = %s AND client_id = %s
                      AND record_type = 'asset_allocation_analysis'
                    """,
                    (analysis_id, client_id),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                return {
                    "ok": False,
                    "error": "asset_allocation_analysis_id_collision",
                }
            return {
                "ok": True,
                "idempotent_replay": not inserted,
                "artifact": _record_row(row),
            }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="upsert",
            table="mvp_artifacts",
        )
        print(f"[db] upsert asset allocation analysis failed: {exc}", flush=True)
        return {
            "ok": False,
            "error": "asset_allocation_analysis_save_failed",
            "detail": str(exc),
        }


def get_asset_allocation_analysis_snapshot(
    *,
    client_id: str,
    analysis_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any] | None:
    """Load one client-owned allocation snapshot, defaulting to the latest in-session.

    The caller may supply the immutable analysis id or an exact allocation,
    assessment, or money-pool reference. Supporting those server-owned aliases
    keeps model-authored routing identifiers out of the quantitative boundary.
    """

    clean_id = str(analysis_id or "").strip()
    clean_session = str(session_id or "").strip()
    exact_analysis_id = clean_id if clean_id.startswith("allocation_") else ""
    reference_id = clean_id if clean_id and not exact_analysis_id else ""
    pool = _get_pool()
    if pool is None:
        rows = list(_MEMORY["mvp_artifacts"].values())
        for row in reversed(rows):
            if (
                row.get("client_id") != client_id
                or row.get("record_type") != "asset_allocation_analysis"
            ):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            assessment_ref = (
                payload.get("assessment_ref")
                if isinstance(payload.get("assessment_ref"), dict)
                else {}
            )
            reference_values = {
                str(row.get("id") or "").strip(),
                str(payload.get("allocation_id") or "").strip(),
                str(assessment_ref.get("assessment_id") or "").strip(),
                str(assessment_ref.get("money_pool_id") or "").strip(),
            }
            if exact_analysis_id and row.get("id") != exact_analysis_id:
                continue
            if reference_id and reference_id not in reference_values:
                continue
            if not exact_analysis_id and clean_session and row.get("related_id") != clean_session:
                continue
            return row
        return None
    where = ["client_id = %s", "record_type = 'asset_allocation_analysis'"]
    params: List[Any] = [client_id]
    if exact_analysis_id:
        where.append("id = %s")
        params.append(exact_analysis_id)
    elif reference_id:
        where.append(
            "(id = %s OR payload->>'allocation_id' = %s "
            "OR payload->'assessment_ref'->>'assessment_id' = %s "
            "OR payload->'assessment_ref'->>'money_pool_id' = %s)"
        )
        params.extend([reference_id, reference_id, reference_id, reference_id])
        if clean_session:
            where.append("related_id = %s")
            params.append(clean_session)
    elif clean_session:
        where.append("related_id = %s")
        params.append(clean_session)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_artifacts
                    WHERE {" AND ".join(where)}
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                return _record_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get asset allocation analysis failed: {exc}", flush=True)
        return None


def save_execution(*, client_id: str, policy_id: Optional[str], proposal_id: str, status: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    return _insert_record(
        "mvp_executions",
        "exec",
        client_id=client_id,
        record_type="execution",
        status=status,
        payload=payload,
        related_type="proposal",
        related_id=proposal_id,
        secondary_id=policy_id,
    )


def get_execution(*, execution_id: str, client_id: Optional[str] = None) -> Dict[str, Any] | None:
    return _get_record("mvp_executions", execution_id, client_id)


def save_policy(
    *,
    client_id: str,
    status: str,
    payload: Dict[str, Any],
    proposal_id: Optional[str] = None,
    policy_id: Optional[str] = None,
) -> Dict[str, Any] | None:
    return _insert_record(
        "mvp_policies",
        "policy",
        client_id=client_id,
        record_type="investment_policy",
        status=status,
        payload=payload,
        related_type="proposal",
        related_id=proposal_id,
        record_id=policy_id,
    )


def upsert_investment_assessment(
    *,
    client_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Durably store one assessment version and allow terminal client decisions."""

    assessment_id = str(payload.get("assessment_id") or "").strip()
    version = payload.get("assessment_version")
    money_pool_id = str(payload.get("money_pool_id") or "").strip()
    status = _investment_assessment_status(payload)
    if (
        not assessment_id
        or not money_pool_id
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        return {"ok": False, "error": "assessment_ref_invalid"}
    if status not in {"pending_client_signoff", "signed_off", "declined"}:
        return {"ok": False, "error": "assessment_status_invalid"}
    digest = hashlib.sha256(
        f"{client_id}:{assessment_id}:{version}".encode("utf-8")
    ).hexdigest()[:24]
    artifact_id = f"assessment_{digest}"
    record_payload = dict(payload)
    record_payload["durable_artifact_id"] = artifact_id
    pool = _get_pool()
    if pool is None:
        existing = _MEMORY["mvp_artifacts"].get(artifact_id)
        if existing is None:
            artifact = _memory_record(
                record_id=artifact_id,
                client_id=client_id,
                record_type="investment_assessment",
                status=status,
                payload=record_payload,
                related_type="money_pool",
                related_id=money_pool_id,
            )
            _MEMORY["mvp_artifacts"][artifact_id] = artifact
            return {"ok": True, "idempotent_replay": False, "artifact": artifact}
        transition_error = _assessment_transition_error(
            existing.get("payload") or {},
            record_payload,
        )
        if transition_error:
            return {"ok": False, "error": transition_error, "artifact_id": artifact_id}
        if _canonical_json(existing.get("payload") or {}) == _canonical_json(record_payload):
            return {"ok": True, "idempotent_replay": True, "artifact": existing}
        existing["status"] = status
        existing["payload"] = record_payload
        return {"ok": True, "idempotent_replay": False, "artifact": existing}

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mvp_artifacts
                        (id, client_id, record_type, status, related_type, related_id, payload)
                    VALUES (%s, %s, 'investment_assessment', %s, 'money_pool', %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (artifact_id, client_id, status, money_pool_id, _pg_json(record_payload)),
                )
                inserted = cur.rowcount == 1
                cur.execute(
                    """
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_artifacts
                    WHERE id = %s AND client_id = %s
                    FOR UPDATE
                    """,
                    (artifact_id, client_id),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("assessment upsert did not produce a durable row")
                artifact = _record_row(row)
                transitioned = False
                if not inserted:
                    transition_error = _assessment_transition_error(
                        artifact.get("payload") or {},
                        record_payload,
                    )
                    if transition_error:
                        conn.rollback()
                        return {
                            "ok": False,
                            "error": transition_error,
                            "artifact_id": artifact_id,
                        }
                    if _canonical_json(artifact.get("payload") or {}) != _canonical_json(record_payload):
                        cur.execute(
                            """
                            UPDATE mvp_artifacts
                            SET status = %s, payload = %s::jsonb, updated_at = NOW()
                            WHERE id = %s AND client_id = %s
                            RETURNING id, client_id, record_type, status, related_type,
                                      related_id, secondary_id, payload, created_at, updated_at
                            """,
                            (status, _pg_json(record_payload), artifact_id, client_id),
                        )
                        artifact = _record_row(cur.fetchone())
                        transitioned = True
            conn.commit()
            return {
                "ok": True,
                "idempotent_replay": not inserted and not transitioned,
                "artifact": artifact,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="upsert",
            table="mvp_artifacts",
        )
        print(f"[db] upsert investment assessment failed: {exc}", flush=True)
        return {"ok": False, "error": "assessment_save_failed", "detail": str(exc)}


def mark_investment_assessments_requires_revalidation(
    *,
    client_id: str,
    source_event_id: Optional[str],
    reason: str,
) -> Dict[str, Any]:
    """Persist assessment invalidation so it cannot age out of recent events."""

    patch = {
        "requires_revalidation": True,
        "stale": True,
        "revalidation_reason": str(reason or "Client File facts changed."),
        "revalidation_source_event_id": source_event_id,
    }
    pool = _get_pool()
    if pool is None:
        updated = 0
        for row in _MEMORY["mvp_artifacts"].values():
            if row.get("client_id") != client_id or row.get("record_type") != "investment_assessment":
                continue
            row["payload"] = {**(row.get("payload") or {}), **patch}
            updated += 1
        return {"ok": True, "updated_count": updated}
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mvp_artifacts
                    SET payload = payload || %s::jsonb, updated_at = NOW()
                    WHERE client_id = %s AND record_type = 'investment_assessment'
                    """,
                    (_pg_json(patch), client_id),
                )
                updated = cur.rowcount
            conn.commit()
            return {"ok": True, "updated_count": updated}
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] mark investment assessments stale failed: {exc}", flush=True)
        return {
            "ok": False,
            "error": "assessment_revalidation_mark_failed",
            "detail": str(exc),
        }


def _investment_assessment_status(payload: Dict[str, Any]) -> str:
    status = str(payload.get("assessment_status") or payload.get("status") or "").strip().lower()
    if payload.get("signed_off") is True or status in {"signed_off", "approved", "confirmed"}:
        return "signed_off"
    if status in {"declined", "rejected", "cancelled", "canceled"}:
        return "declined"
    if status in {"pending_client_signoff", "pending_signoff", "awaiting_signoff", "ready_for_signoff"}:
        return "pending_client_signoff"
    return status


def _assessment_transition_error(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Optional[str]:
    for key in ("assessment_id", "assessment_version", "money_pool_id", "valid_until"):
        if existing.get(key) != incoming.get(key):
            return "assessment_version_collision"
    if _canonical_json(existing.get("assessment") or {}) != _canonical_json(incoming.get("assessment") or {}):
        return "assessment_version_collision"
    existing_status = _investment_assessment_status(existing)
    incoming_status = _investment_assessment_status(incoming)
    if existing_status == incoming_status:
        return None
    if existing_status == "pending_client_signoff" and incoming_status in {"signed_off", "declined"}:
        return None
    return "assessment_status_regression"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def save_asset_allocation_proposal_bundle(
    *,
    client_id: str,
    idempotency_key: str,
    artifact_title: str,
    artifact_payload: Dict[str, Any],
    policy_payload: Dict[str, Any],
    money_pool_id: Optional[str],
) -> Dict[str, Any]:
    """Atomically persist one proposal/policy pair under deterministic IDs.

    The key must already include client, assessment identity/version, and the
    engine/input contract version. Repeating the same key returns the same pair.
    """
    clean_key = str(idempotency_key or "").strip()
    if not clean_key:
        return {"ok": False, "error": "idempotency_key_required"}
    digest = hashlib.sha256(f"{client_id}:{clean_key}".encode("utf-8")).hexdigest()[:24]
    artifact_id = f"proposal_{digest}"
    policy_id = f"policy_{digest}"
    artifact_data = {
        **artifact_payload,
        "title": artifact_title,
        "idempotency_key": clean_key,
    }
    policy_data = {
        **policy_payload,
        "proposal_id": artifact_id,
        "idempotency_key": clean_key,
    }
    pool = _get_pool()
    if pool is None:
        existing_artifact = _MEMORY["mvp_artifacts"].get(artifact_id)
        existing_policy = _MEMORY["mvp_policies"].get(policy_id)
        if existing_artifact or existing_policy:
            if not existing_artifact or not existing_policy:
                return {
                    "ok": False,
                    "error": "idempotency_partial_state",
                    "artifact_id": artifact_id,
                    "policy_id": policy_id,
                }
            return {
                "ok": True,
                "idempotent_replay": True,
                "artifact": existing_artifact,
                "policy": existing_policy,
            }
        artifact = _memory_record(
            record_id=artifact_id,
            client_id=client_id,
            record_type="proposal",
            status="ready",
            payload=artifact_data,
            related_type="money_pool" if money_pool_id else None,
            related_id=money_pool_id,
        )
        policy = _memory_record(
            record_id=policy_id,
            client_id=client_id,
            record_type="investment_policy",
            status="proposed",
            payload=policy_data,
            related_type="proposal",
            related_id=artifact_id,
        )
        _MEMORY["mvp_artifacts"][artifact_id] = artifact
        _MEMORY["mvp_policies"][policy_id] = policy
        return {
            "ok": True,
            "idempotent_replay": False,
            "artifact": artifact,
            "policy": policy,
        }

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mvp_artifacts
                        (id, client_id, record_type, status, related_type, related_id, payload)
                    VALUES (%s, %s, 'proposal', 'ready', %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        artifact_id,
                        client_id,
                        "money_pool" if money_pool_id else None,
                        money_pool_id,
                        _pg_json(artifact_data),
                    ),
                )
                artifact_inserted = cur.rowcount == 1
                cur.execute(
                    """
                    INSERT INTO mvp_policies
                        (id, client_id, record_type, status, related_type, related_id, payload)
                    VALUES (%s, %s, 'investment_policy', 'proposed', 'proposal', %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (policy_id, client_id, artifact_id, _pg_json(policy_data)),
                )
                policy_inserted = cur.rowcount == 1
                if artifact_inserted != policy_inserted:
                    raise RuntimeError("idempotency partial state detected")
                cur.execute(
                    """
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_artifacts WHERE id = %s AND client_id = %s
                    """,
                    (artifact_id, client_id),
                )
                artifact_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM mvp_policies WHERE id = %s AND client_id = %s
                    """,
                    (policy_id, client_id),
                )
                policy_row = cur.fetchone()
                if not artifact_row or not policy_row:
                    raise RuntimeError("proposal bundle insert did not produce both records")
                artifact = _record_row(artifact_row)
                policy = _record_row(policy_row)
                if (
                    (artifact.get("payload") or {}).get("idempotency_key") != clean_key
                    or (policy.get("payload") or {}).get("idempotency_key") != clean_key
                ):
                    raise RuntimeError("idempotency key collision")
            conn.commit()
            return {
                "ok": True,
                "idempotent_replay": not artifact_inserted,
                "artifact": artifact,
                "policy": policy,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    except Exception as exc:
        _raise_if_client_identity_missing(
            exc,
            operation="insert",
            table="mvp_artifacts,mvp_policies",
        )
        print(f"[db] save asset allocation proposal bundle failed: {exc}", flush=True)
        return {"ok": False, "error": "proposal_bundle_save_failed", "detail": str(exc)}


def _memory_record(
    *,
    record_id: str,
    client_id: str,
    record_type: str,
    status: str,
    payload: Dict[str, Any],
    related_type: Optional[str],
    related_id: Optional[str],
) -> Dict[str, Any]:
    record = {
        "id": record_id,
        "client_id": client_id,
        "record_type": record_type,
        "status": status,
        "related_type": related_type,
        "related_id": related_id,
        "secondary_id": None,
        "payload": payload,
        "created_at": None,
        "updated_at": None,
    }
    if record_type == "proposal":
        record.update(
            {
                "artifact_type": "proposal",
                "sections": payload.get("sections", []),
                "section_ids": payload.get("section_ids", []),
            }
        )
    return record


def update_policy(*, policy_id: str, client_id: str, status: str, payload_patch: Dict[str, Any]) -> Dict[str, Any] | None:
    return _update_record_payload("mvp_policies", record_id=policy_id, client_id=client_id, status=status, payload_patch=payload_patch)


def get_policy(*, policy_id: str, client_id: Optional[str] = None) -> Dict[str, Any] | None:
    return _get_record("mvp_policies", policy_id, client_id)


def list_policies(*, client_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    return _list_records("mvp_policies", client_id=client_id, status=status)


def save_holding(*, client_id: str, policy_id: Optional[str], status: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    payload = dict(payload)
    if status == "imported":
        payload.setdefault("observed_at", datetime.now(timezone.utc).isoformat())
    record = _insert_record(
        "mvp_holdings",
        "holding",
        client_id=client_id,
        record_type="holding",
        status=status,
        payload=payload,
        related_type="policy",
        related_id=policy_id,
    )
    if record is not None and status == "imported":
        from api.persistence.canonical_client_file import write_canonical_client_file_update

        write_canonical_client_file_update(
            client_id=client_id,
            event_type="linked_holding_imported",
            payload={"facts": {}},
            source={
                "source": "provider_observation",
                "connection_id": payload.get("source_connection_id"),
                "holding_id": record.get("id"),
            },
            writeback={
                "record": "client_file.linked_accounts",
                "operation": "linked_holding_imported",
                "subject_id": record.get("id"),
                "subject": payload.get("symbol") or payload.get("title") or "holding",
                "fields": ["dollar_valued_holdings"],
                "values": {"holding": record},
            },
        )
    return record


def update_holding(*, holding_id: str, client_id: str, status: str, payload_patch: Dict[str, Any]) -> Dict[str, Any] | None:
    return _update_record_payload("mvp_holdings", record_id=holding_id, client_id=client_id, status=status, payload_patch=payload_patch)


def list_holdings(*, client_id: str, policy_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    return _list_records("mvp_holdings", client_id=client_id, related_id=policy_id, status=status)


def _insert_record(
    table: str,
    prefix: str,
    *,
    client_id: str,
    record_type: str,
    status: str,
    payload: Dict[str, Any],
    related_type: Optional[str] = None,
    related_id: Optional[str] = None,
    secondary_id: Optional[str] = None,
    record_id: Optional[str] = None,
) -> Dict[str, Any] | None:
    pool = _get_pool()
    resolved_record_id = record_id or _id(prefix)
    if pool is None:
        existing = _MEMORY[table].get(resolved_record_id)
        if existing is not None:
            return existing if existing.get("client_id") == client_id else None
        record = {
            "id": resolved_record_id,
            "client_id": client_id,
            "record_type": record_type,
            "status": status,
            "related_type": related_type,
            "related_id": related_id,
            "secondary_id": secondary_id,
            "payload": payload,
            "created_at": None,
            "updated_at": None,
        }
        if record_type in {"proposal", "monitoring_report", "policy_update", "projection"}:
            record.update({
                "artifact_type": record_type,
                "sections": payload.get("sections", []),
                "section_ids": payload.get("section_ids", []),
            })
        _MEMORY[table][resolved_record_id] = record
        return record
    conn = None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {table} (id, client_id, record_type, status, related_type, related_id, secondary_id, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        resolved_record_id,
                        client_id,
                        record_type,
                        status,
                        related_type,
                        related_id,
                        secondary_id,
                        _pg_json(payload),
                    ),
                )
                cur.execute(
                    f"""
                    SELECT id, client_id, record_type, status, related_type, related_id,
                           secondary_id, payload, created_at, updated_at
                    FROM {table}
                    WHERE id = %s AND client_id = %s AND record_type = %s
                    """,
                    (resolved_record_id, client_id, record_type),
                )
                row = cur.fetchone()
            conn.commit()
            return _record_row(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    except Exception as exc:
        raise _typed_write_error(exc, operation="insert", table=table) from exc


def _get_record(table: str, record_id: str, client_id: Optional[str] = None) -> Dict[str, Any] | None:
    pool = _get_pool()
    if pool is None:
        row = _MEMORY[table].get(record_id)
        if row and (not client_id or row.get("client_id") == client_id):
            return row
        return None
    params: List[Any] = [record_id]
    client_clause = ""
    if client_id:
        client_clause = "AND client_id = %s"
        params.append(client_id)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, record_type, status, related_type, related_id, secondary_id, payload, created_at, updated_at
                    FROM {table}
                    WHERE id = %s {client_clause}
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                return _record_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get {table} failed: {exc}", flush=True)
        return None


def _list_records(
    table: str,
    *,
    client_id: str,
    record_type: Optional[str] = None,
    related_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    pool = _get_pool()
    if pool is None:
        rows = [
            row for row in _MEMORY[table].values()
            if row.get("client_id") == client_id
            and (not record_type or row.get("record_type") == record_type)
            and (not related_id or row.get("related_id") == related_id)
            and (not status or row.get("status") == status)
        ]
        return rows[:max(1, min(int(limit or 100), 500))]
    where = ["client_id = %s"]
    params: List[Any] = [client_id]
    if record_type:
        where.append("record_type = %s")
        params.append(record_type)
    if related_id:
        where.append("related_id = %s")
        params.append(related_id)
    if status:
        where.append("status = %s")
        params.append(status)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, record_type, status, related_type, related_id, secondary_id, payload, created_at, updated_at
                    FROM {table}
                    WHERE {" AND ".join(where)}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (*params, max(1, min(int(limit or 100), 500))),
                )
                return [_record_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list {table} failed: {exc}", flush=True)
        return []


def _update_record_payload(table: str, *, record_id: str, client_id: str, status: str, payload_patch: Dict[str, Any]) -> Dict[str, Any] | None:
    pool = _get_pool()
    if pool is None:
        row = _MEMORY[table].get(record_id)
        if not row or row.get("client_id") != client_id:
            return None
        row["status"] = status
        row["payload"] = {**(row.get("payload") or {}), **payload_patch}
        return row
    conn = None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET status = %s, payload = payload || %s::jsonb, updated_at = NOW()
                    WHERE id = %s AND client_id = %s
                    RETURNING id, client_id, record_type, status, related_type, related_id, secondary_id, payload, created_at, updated_at
                    """,
                    (status, _pg_json(payload_patch), record_id, client_id),
                )
                row = cur.fetchone()
            conn.commit()
            return _record_row(row) if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    except Exception as exc:
        raise _typed_write_error(exc, operation="update", table=table) from exc


def _onboarding_row(row: Any) -> Dict[str, Any]:
    return {
        "client_id": row[0],
        "current_step": row[1],
        "status": row[2],
        "completed_steps": row[3] or [],
        "created_at": _iso(row[4]),
        "updated_at": _iso(row[5]),
        "account_opening_status": row[6] if len(row) > 6 else row[2],
        "advisor_onboarding_status": row[7] if len(row) > 7 else "not_started",
    }


def _conversation_row(row: Any) -> Dict[str, Any]:
    return {
        "id": row[0],
        "conversation_id": row[0],
        "client_id": row[1],
        "scope": row[2],
        "context": row[3] or {},
        "status": row[4],
        "created_at": _iso(row[5]),
        "updated_at": _iso(row[6]),
    }


def _message_row(row: Any) -> Dict[str, Any]:
    return {
        "id": row[0],
        "message_id": row[0],
        "conversation_id": row[1],
        "client_id": row[2],
        "role": row[3],
        "message_type": row[4],
        "text": row[5],
        "attachments": row[6] or [],
        "payload": row[7] or {},
        "event_id": row[8],
        "run_id": row[9],
        "trace_id": row[10],
        "created_at": _iso(row[11]),
    }


def _record_row(row: Any) -> Dict[str, Any]:
    payload = row[7] or {}
    return {
        "id": row[0],
        "client_id": row[1],
        "record_type": row[2],
        "status": row[3],
        "related_type": row[4],
        "related_id": row[5],
        "secondary_id": row[6],
        "payload": payload,
        "created_at": _iso(row[8]),
        "updated_at": _iso(row[9]),
        **({"artifact_type": row[2], "sections": payload.get("sections", []), "section_ids": payload.get("section_ids", [])} if row[2] in {"proposal", "monitoring_report", "policy_update", "projection"} else {}),
    }


__all__ = [
    "add_conversation_message",
    "create_conversation",
    "get_artifact",
    "get_asset_allocation_analysis_snapshot",
    "get_cashflow_analysis_snapshot",
    "get_conversation",
    "get_execution",
    "get_onboarding_status",
    "get_policy",
    "list_artifacts",
    "list_conversation_messages",
    "list_holdings",
    "list_external_connections",
    "list_policies",
    "mark_investment_assessments_requires_revalidation",
    "save_artifact",
    "save_consent",
    "save_data_permission",
    "save_execution",
    "save_external_connection",
    "save_holding",
    "save_identity_profile",
    "save_policy",
    "update_holding",
    "update_policy",
    "upsert_investment_assessment",
    "upsert_asset_allocation_analysis",
    "upsert_cashflow_analysis",
    "upsert_onboarding_status",
]
