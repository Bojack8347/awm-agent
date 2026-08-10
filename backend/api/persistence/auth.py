"""Authentication account and session persistence."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from werkzeug.security import check_password_hash

from .core import _get_pool, _safe_getconn


# ---------------------------------------------------------------------------
# Authentication — one account maps to one persistent client_id
# ---------------------------------------------------------------------------

_DEFAULT_AUTH_SESSION_DAYS = max(1, int(os.getenv("AUTH_SESSION_TTL_DAYS", "30") or "30"))


class AuthSessionLookupUnavailable(RuntimeError):
    """The session could not be checked because persistence was unavailable."""


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _build_client_id() -> str:
    return f"client-{uuid.uuid4().hex[:24]}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_auth_account(email: str, password_hash: str) -> Optional[Dict[str, Any]]:
    """Create a new auth account and its linked persistent client_id."""
    pool = _get_pool()
    if pool is None:
        return None

    normalized_email = _normalize_email(email)
    account_id = str(uuid.uuid4())
    client_id = _build_client_id()
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO clients (client_id, status) VALUES (%s, 'active')", (client_id,))
                cur.execute(
                    "INSERT INTO auth_accounts (id, email, password_hash, client_id) VALUES (%s, %s, %s, %s)",
                    (account_id, normalized_email, password_hash, client_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
        return get_auth_account_by_id(account_id)
    except Exception as exc:
        print(f"[db] Failed to create auth account: {exc}", flush=True)
        return None


def register_auth_account_transaction(
    *, request_id: str, normalized_email: str, password: str, password_hash: str,
    invitation_hash: str, identity_fingerprint: str,
) -> Dict[str, Any]:
    """Create receipt, invitation redemption, identity and session in one commit."""
    pool = _get_pool()
    if pool is None:
        return {"error": "registration_unavailable"}
    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.identity_fingerprint, r.status, r.account_id, a.email,
                          a.password_hash, a.client_id, a.onboarding_completed
                   FROM registration_requests r
                   LEFT JOIN auth_accounts a ON a.id = r.account_id
                   WHERE r.client_registration_request_id = %s FOR UPDATE OF r""",
                (request_id,),
            )
            replay = cur.fetchone()
            if replay:
                if replay[0] != identity_fingerprint:
                    conn.rollback(); return {"error": "idempotency_conflict"}
                if replay[1] == "committed":
                    if not replay[4] or not check_password_hash(replay[4], password):
                        conn.rollback(); return {"error": "idempotency_conflict"}
                    conn.rollback()
                    return {
                        "disposition": "committed_reauthentication_required",
                        "client_registration_request_id": request_id,
                        "account": {"id": str(replay[2]), "email": replay[3], "client_id": replay[5], "onboarding_completed": bool(replay[6])},
                    }
            configured_code = os.getenv("REGISTRATION_INVITE_NUMBER", "").strip()
            if configured_code and hashlib.sha256(configured_code.encode("utf-8")).hexdigest() == invitation_hash:
                fixture_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"awm-registration-invite:{invitation_hash}"))
                cur.execute(
                    """INSERT INTO registration_invitations(invitation_id, token_hash, status, max_uses, metadata)
                       VALUES (%s, %s, 'active', 1, '{"source":"environment_fixture"}'::jsonb)
                       ON CONFLICT (token_hash) DO NOTHING""",
                    (fixture_id, invitation_hash),
                )
            cur.execute(
                """SELECT invitation_id, status, max_uses, use_count, expires_at
                   FROM registration_invitations WHERE token_hash = %s FOR UPDATE""",
                (invitation_hash,),
            )
            invitation = cur.fetchone()
            if not invitation:
                conn.rollback(); return {"error": "invitation_invalid"}
            if invitation[1] != "active" or invitation[3] >= invitation[2]:
                conn.rollback(); return {"error": "invitation_redeemed"}
            if invitation[4] and invitation[4] <= datetime.now(timezone.utc):
                conn.rollback(); return {"error": "invitation_expired"}
            cur.execute("SELECT 1 FROM auth_accounts WHERE email = %s", (normalized_email,))
            if cur.fetchone():
                conn.rollback(); return {"error": "duplicate_email"}
            account_id, client_id = str(uuid.uuid4()), _build_client_id()
            session_id, raw_token = str(uuid.uuid4()), secrets.token_urlsafe(48)
            expires_at = datetime.now(timezone.utc) + timedelta(days=_DEFAULT_AUTH_SESSION_DAYS)
            cur.execute(
                """INSERT INTO registration_requests(client_registration_request_id, identity_fingerprint, normalized_email, invitation_id, status)
                   VALUES (%s, %s, %s, %s, 'in_progress')""",
                (request_id, identity_fingerprint, normalized_email, invitation[0]),
            )
            cur.execute("INSERT INTO clients(client_id, status) VALUES (%s, 'active')", (client_id,))
            cur.execute(
                "INSERT INTO auth_accounts(id, email, password_hash, client_id) VALUES (%s, %s, %s, %s)",
                (account_id, normalized_email, password_hash, client_id),
            )
            cur.execute(
                "INSERT INTO auth_sessions(id, account_id, token_hash, expires_at, last_seen_at) VALUES (%s, %s, %s, %s, NOW())",
                (session_id, account_id, _hash_token(raw_token), expires_at),
            )
            cur.execute(
                """UPDATE registration_invitations SET use_count = use_count + 1,
                     status = CASE WHEN use_count + 1 >= max_uses THEN 'redeemed' ELSE status END,
                     redeemed_account_id = %s, redeemed_at = NOW() WHERE invitation_id = %s""",
                (account_id, invitation[0]),
            )
            cur.execute(
                """UPDATE registration_requests SET status='committed', account_id=%s, client_id=%s,
                     initial_auth_session_id=%s, completed_at=NOW(), updated_at=NOW()
                   WHERE client_registration_request_id=%s""",
                (account_id, client_id, session_id, request_id),
            )
        conn.commit()
        return {
            "disposition": "created",
            "client_registration_request_id": request_id,
            "token": raw_token,
            "session": {"id": session_id, "expires_at": expires_at.isoformat()},
            "account": {"id": account_id, "email": normalized_email, "client_id": client_id, "onboarding_completed": False},
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

def get_auth_account_by_id(account_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve an auth account by ID."""
    pool = _get_pool()
    if pool is None:
        return None

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, client_id, status,
                           onboarding_completed, created_at, updated_at, last_login_at
                    FROM auth_accounts
                    WHERE id = %s
                    """,
                    (account_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": str(row[0]),
                    "email": row[1],
                    "password_hash": row[2],
                    "client_id": row[3],
                    "status": row[4],
                    "onboarding_completed": bool(row[5]),
                    "created_at": row[6].isoformat() if row[6] else None,
                    "updated_at": row[7].isoformat() if row[7] else None,
                    "last_login_at": row[8].isoformat() if row[8] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get auth account by id: {exc}", flush=True)
        return None


def get_auth_account_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve an auth account by email address."""
    pool = _get_pool()
    if pool is None:
        return None

    normalized_email = _normalize_email(email)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, client_id, status,
                           onboarding_completed, created_at, updated_at, last_login_at
                    FROM auth_accounts
                    WHERE email = %s
                    """,
                    (normalized_email,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": str(row[0]),
                    "email": row[1],
                    "password_hash": row[2],
                    "client_id": row[3],
                    "status": row[4],
                    "onboarding_completed": bool(row[5]),
                    "created_at": row[6].isoformat() if row[6] else None,
                    "updated_at": row[7].isoformat() if row[7] else None,
                    "last_login_at": row[8].isoformat() if row[8] else None,
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get auth account by email: {exc}", flush=True)
        return None


def get_account_id_for_client_id(client_id: str) -> Optional[str]:
    """Return the auth account UUID for a given client_id, or None if not found."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM auth_accounts WHERE client_id = %s LIMIT 1",
                    (client_id,),
                )
                row = cur.fetchone()
                return str(row[0]) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to get account_id for client_id={client_id}: {exc}", flush=True)
        return None


def create_auth_session(account_id: str) -> Optional[Dict[str, Any]]:
    """Create a new persistent auth session and return the raw bearer token once."""
    pool = _get_pool()
    if pool is None:
        return None

    session_id = str(uuid.uuid4())
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=_DEFAULT_AUTH_SESSION_DAYS)

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO auth_sessions
                        (id, account_id, token_hash, expires_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    """,
                    (session_id, account_id, token_hash, expires_at.isoformat()),
                )
                cur.execute(
                    """
                    UPDATE auth_accounts
                    SET last_login_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (account_id,),
                )
            conn.commit()
            return {
                "session_id": session_id,
                "token": raw_token,
                "expires_at": expires_at.isoformat(),
            }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to create auth session: {exc}", flush=True)
        return None


def get_auth_session_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Resolve a bearer token to an active auth session and linked account."""
    pool = _get_pool()
    if pool is None or not token:
        return None

    token_hash = _hash_token(token)
    last_error: Optional[Exception] = None
    for _attempt in range(2):
        conn = None
        try:
            conn = _safe_getconn(pool)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.id, s.account_id, s.status, s.created_at, s.updated_at,
                           s.expires_at, s.last_seen_at,
                           a.id, a.email, a.client_id, a.status, a.onboarding_completed
                    FROM auth_sessions s
                    JOIN auth_accounts a ON a.id = s.account_id
                    WHERE s.token_hash = %s
                      AND s.status = 'active'
                      AND a.status = 'active'
                      AND s.expires_at > NOW()
                    LIMIT 1
                    """,
                    (token_hash,),
                )
                row = cur.fetchone()
            pool.putconn(conn)
            conn = None
            if row is None:
                return None
            return {
                    "session_id": str(row[0]),
                    "account_id": str(row[1]),
                    "session_status": row[2],
                    "session_created_at": row[3].isoformat() if row[3] else None,
                    "session_updated_at": row[4].isoformat() if row[4] else None,
                    "expires_at": row[5].isoformat() if row[5] else None,
                    "last_seen_at": row[6].isoformat() if row[6] else None,
                    "id": str(row[7]),
                    "email": row[8],
                    "client_id": row[9],
                    "status": row[10],
                    "onboarding_completed": bool(row[11]),
                }
        except Exception as exc:
            last_error = exc
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass

    print(f"[db] Failed to get auth session: {last_error}", flush=True)
    raise AuthSessionLookupUnavailable("Authentication persistence is temporarily unavailable") from last_error


def touch_auth_session(session_id: str) -> bool:
    """Refresh auth session heartbeat so active sessions stay warm."""
    pool = _get_pool()
    if pool is None or not session_id:
        return False

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_sessions
                    SET last_seen_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (session_id,),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to touch auth session: {exc}", flush=True)
        return False


def revoke_auth_session(token: str) -> bool:
    """Revoke the current auth session token."""
    pool = _get_pool()
    if pool is None or not token:
        return False

    token_hash = _hash_token(token)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_sessions
                    SET status = 'revoked', updated_at = NOW()
                    WHERE token_hash = %s
                    """,
                    (token_hash,),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to revoke auth session: {exc}", flush=True)
        return False


def delete_auth_account(account_id: str, client_id: str) -> bool:
    """Hard-delete an account and its client identity graph.

    The auth account is removed first because its client FK intentionally does
    not cascade.  Deleting the client root then lets database constraints erase
    every directly and indirectly owned row in one transaction.
    """
    pool = _get_pool()
    if pool is None or not account_id or not client_id:
        return False

    try:
        conn = _safe_getconn(pool)
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM auth_accounts
                        WHERE id = %s AND client_id = %s
                        RETURNING client_id
                        """,
                        (account_id, client_id),
                    )
                    owner = cur.fetchone()
                    if owner is None:
                        conn.rollback()
                        return False

                    cur.execute(
                        """
                        DELETE FROM clients
                        WHERE client_id = %s
                        RETURNING client_id
                        """,
                        (str(owner[0]),),
                    )
                    if cur.fetchone() is None:
                        raise RuntimeError(
                            f"Client identity {owner[0]} was not deleted"
                        )

                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to delete auth account {account_id}: {exc}", flush=True)
        return False


def mark_onboarding_completed(client_id: str) -> bool:
    """Mark the account linked to a client profile as fully onboarded."""
    pool = _get_pool()
    if pool is None or not client_id:
        return False

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_accounts
                    SET onboarding_completed = TRUE, updated_at = NOW()
                    WHERE client_id = %s
                    """,
                    (client_id,),
                )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] Failed to mark onboarding completed: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Consultation ingests
