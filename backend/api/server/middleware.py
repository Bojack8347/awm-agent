"""Request parsing and authentication decorators for API routes."""

from __future__ import annotations

import functools
import os
import sys
from typing import Any, Dict, Optional, Tuple

from flask import jsonify, request

from api.persistence.auth import AuthSessionLookupUnavailable

from .persistence import db_available, db_get_auth_session_by_token, db_touch_auth_session
from .demo_auth import is_demo_auth_enabled


def _server_deps() -> Any:
    package_name = __package__
    return sys.modules.get(package_name) or sys.modules.get("server") or sys.modules[__name__]

def _parse_nonempty_json_body() -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Any, int]]]:
    """Parse and validate that request body is a non-empty JSON object."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body:
        return None, (jsonify({"success": False, "error": "Request JSON body is required"}), 400)
    return body, None
def require_api_key() -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Validate optional advisor API key if configured."""
    configured_key = os.getenv("ADVISOR_API_KEY", "").strip()
    if not configured_key:
        return True, None

    received_key = request.headers.get("X-Api-Key", "").strip()
    if received_key != configured_key:
        return False, {"success": False, "error": "Invalid or missing advisor API key"}

    return True, None


def _extract_bearer_token() -> str:
    """Read a bearer token from Authorization header."""
    authorization = request.headers.get("Authorization", "").strip()
    if not authorization.lower().startswith("bearer "):
        return ""
    return authorization[7:].strip()


def _get_authenticated_account() -> Optional[Dict[str, Any]]:
    """Resolve the current bearer token to an active account session."""
    token = _extract_bearer_token()
    if not token:
        return None
    if token == "demo-mvp-token" and is_demo_auth_enabled():
        return {
            "session_id": "demo-mvp-session",
            "account_id": "demo-mvp-account",
            "expires_at": None,
            "id": "demo-mvp-account",
            "email": "demo@awm.local",
            "client_id": "client-demo-mvp",
            "status": "active",
            "onboarding_completed": False,
        }

    auth_session = db_get_auth_session_by_token(token)
    if auth_session:
        db_touch_auth_session(auth_session["session_id"])
    return auth_session


def _require_authenticated_account() -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Any, int]]]:
    """Require a valid logged-in account for client-bound app endpoints."""
    token = _extract_bearer_token()
    if token == "demo-mvp-token" and is_demo_auth_enabled():
        auth_session = _get_authenticated_account()
        if auth_session:
            return auth_session, None

    if not db_available():
        return None, (jsonify({
            "success": False,
            "error": "Authentication requires a configured database",
        }), 503)

    try:
        auth_session = _get_authenticated_account()
    except AuthSessionLookupUnavailable:
        return None, (jsonify({
            "success": False,
            "error": "Authentication service is temporarily unavailable",
        }), 503)
    if not auth_session:
        return None, (jsonify({
            "success": False,
            "error": "Authentication required",
        }), 401)

    return auth_session, None


def require_user_auth(fn: Any) -> Any:
    """Decorator: require a valid user bearer token.

    Injects ``auth_session`` as a keyword argument into the wrapped route
    function. Returns 401/503 if auth fails. Replaces the manual two-liner::

        auth_session, error = _require_authenticated_account()
        if error:
            return error

    Usage::

        @app.route("/api/v1/foo")
        @require_user_auth
        def foo(auth_session):
            client_id = auth_session["client_id"]
            ...
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        auth_session, error = _server_deps()._require_authenticated_account()
        if error:
            return error
        return fn(*args, auth_session=auth_session, **kwargs)

    return wrapper


def require_api_key_auth(fn: Any) -> Any:
    """Decorator: require a valid ADVISOR_API_KEY header (machine-to-machine).

    Returns 401 if the key is wrong. Replaces the manual two-liner::

        ok, error = require_api_key()
        if not ok:
            return jsonify(error), 401

    Usage::

        @app.route("/advisor/api/v1/foo", methods=["POST"])
        @require_api_key_auth
        def foo():
            ...
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ok, error = _server_deps().require_api_key()
        if not ok:
            return jsonify(error), 401
        return fn(*args, **kwargs)

    return wrapper
