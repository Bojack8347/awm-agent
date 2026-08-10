"""Core PostgreSQL connection helpers for API persistence."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

def _pg_json(value: Any) -> str:
    """Serialize value to a JSON string safe for PostgreSQL TEXT/JSONB columns.

    PostgreSQL rejects \\u0000 (null byte) in JSON/text columns. LLM responses
    can occasionally contain them, causing the entire DB write to fail and
    leaving journey/consultation rows stuck as 'processing' forever.
    """
    return json.dumps(value, ensure_ascii=True, default=str).replace("\\u0000", "").replace("\x00", "")


_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_pool = None
_client_file_pool: ContextVar[Any] = ContextVar(
    "awm_client_file_connection_pool",
    default=None,
)


class _BorrowedConnectionPool:
    """Pool facade that keeps nested persistence reads on one connection."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def getconn(self) -> Any:
        return self.connection

    def putconn(self, connection: Any, close: bool = False) -> None:
        if connection is not self.connection or close:
            raise RuntimeError("Borrowed Client File connection cannot be replaced")


def database_mode() -> str:
    """Return the configured persistence policy.

    ``auto`` prefers PostgreSQL and lets persistence modules use their
    process-local fallbacks when it is unavailable. ``required`` fails fast
    instead of silently losing durability. ``off`` is the explicit,
    database-free development mode.
    """
    mode = os.getenv("AWM_DATABASE_MODE", "auto").strip().lower()
    if mode not in {"auto", "required", "off"}:
        raise RuntimeError(
            "AWM_DATABASE_MODE must be one of: auto, required, off"
        )
    return mode


def _pool_max_connections() -> int:
    try:
        configured = int(os.getenv("AWM_DB_POOL_MAX_CONNECTIONS", "12") or "12")
    except ValueError:
        configured = 12
    return max(2, min(configured, 50))


def _get_pool():
    """Lazily initialize connection pool."""
    global _pool, _DATABASE_URL
    borrowed_pool = _client_file_pool.get()
    if borrowed_pool is not None:
        return borrowed_pool
    mode = database_mode()
    if mode == "off":
        return None
    if _pool is not None:
        return _pool
    # Re-read in case bootstrap/dotenv loaded after this module was first imported.
    if not _DATABASE_URL:
        _DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
    if not _DATABASE_URL:
        if mode == "required":
            raise RuntimeError(
                "PostgreSQL is required but DATABASE_URL is not configured"
            )
        return None
    try:
        import psycopg2
        from psycopg2 import pool as pg_pool

        # The API serves bootstrap requests concurrently, so its shared pool
        # must be thread-safe and large enough for one app launch.
        _pool = pg_pool.ThreadedConnectionPool(
            1,
            _pool_max_connections(),
            _DATABASE_URL,
        )
        _ensure_schema(_pool)
        return _pool
    except Exception as exc:
        message = f"Failed to connect to PostgreSQL: {exc}"
        if mode == "required":
            raise RuntimeError(message) from exc
        print(f"[db] {message}; using process-local persistence", flush=True)
        return None


def _safe_getconn(pool):
    """Get a connection from the pool, verifying it is alive first.

    Cloud SQL and other managed Postgres services silently close idle SSL
    connections. psycopg2 pools have no built-in health check, so a
    stale connection surfaces as ``SSL SYSCALL error: EOF detected`` on the
    next query.  This wrapper sends a lightweight ``SELECT 1`` probe and, if
    the connection is dead, discards it and creates a fresh one.
    """
    if isinstance(pool, _BorrowedConnectionPool):
        return pool.connection

    import psycopg2

    def acquire():
        last_error = None
        for _ in range(2):
            try:
                return pool.getconn()
            except Exception as exc:
                last_error = exc
        raise psycopg2.OperationalError(
            f"Failed to acquire a DB connection: {last_error}"
        ) from last_error

    conn = acquire()
    try:
        # Quick probe — must complete within 5 s.
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        # Connection is dead.  Close it so the pool slot is freed, then
        # put it back as bad and request a new one.
        try:
            conn.close()
        except Exception:
            pass
        pool.putconn(conn, close=True)
        # Get a fresh connection (the pool will create one).
        fresh = acquire()
        try:
            fresh.cursor().execute("SELECT 1")
        except Exception as exc:
            pool.putconn(fresh, close=True)
            raise psycopg2.OperationalError(
                f"Failed to establish a healthy DB connection: {exc}"
            ) from exc
        return fresh


@contextmanager
def client_file_connection_scope():
    """Share one checked-out connection across a complete Client File read.

    Persistence readers keep their existing APIs and still call ``putconn``;
    the borrowed facade turns those nested returns into no-ops.  The outer
    scope closes the read transaction and returns the real connection once.
    """

    existing = _client_file_pool.get()
    if existing is not None:
        yield
        return

    pool = _get_pool()
    if pool is None:
        yield
        return

    conn = _safe_getconn(pool)
    borrowed = _BorrowedConnectionPool(conn)
    token = _client_file_pool.set(borrowed)
    try:
        yield
    finally:
        _client_file_pool.reset(token)
        try:
            conn.rollback()
        finally:
            pool.putconn(conn)


def _ensure_schema(pool) -> None:
    """Retired: schema is now managed by Alembic migrations.

    Run `alembic upgrade head` from backend/api/ before starting the server.
    This function is kept as a no-op so existing call sites don't break during
    the transition. It will be removed in a future cleanup pass.
    """


def is_available() -> bool:
    """Check if database is available."""
    return _get_pool() is not None
