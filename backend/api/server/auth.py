"""Authentication helper functions shared by API route registration."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from flask import jsonify


def normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def normalize_invite_number(invite_number: Any) -> str:
    return str(invite_number or "").strip()


def validate_credentials(email: Any, password: Any) -> Optional[Tuple[Any, int]]:
    normalized_email = normalize_email(email)
    raw_password = str(password or "")

    if not normalized_email or "@" not in normalized_email:
        return jsonify({"success": False, "error": "A valid email is required"}), 400

    if len(raw_password) < 8:
        return jsonify({
            "success": False,
            "error": "Password must be at least 8 characters long",
        }), 400

    return None


def validate_login_credentials(email: Any, password: Any) -> Optional[Tuple[Any, int]]:
    normalized_email = normalize_email(email)
    raw_password = str(password or "")

    if not normalized_email or "@" not in normalized_email:
        return jsonify({"success": False, "error": "A valid email is required"}), 400

    if not raw_password:
        return jsonify({"success": False, "error": "Password is required"}), 400

    return None


def validate_registration_invite(invite_number: Any) -> Optional[Tuple[Any, int]]:
    expected_invite_number = normalize_invite_number(
        os.getenv("REGISTRATION_INVITE_NUMBER", ""),
    )
    provided_invite_number = normalize_invite_number(invite_number)

    if not expected_invite_number:
        return jsonify({
            "success": False,
            "error": "Registration is temporarily unavailable. Invite configuration is missing.",
        }), 503

    if not provided_invite_number:
        return jsonify({
            "success": False,
            "error": "Invite number is required",
        }), 400

    if provided_invite_number != expected_invite_number:
        return jsonify({
            "success": False,
            "error": "Invite number is invalid",
        }), 403

    return None


def serialize_auth_payload(
    account: Dict[str, Any],
    session: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the shared auth response payload returned to the mobile app."""
    return {
        "success": True,
        "token": session["token"],
        "session": {
            "id": session["session_id"],
            "expires_at": session["expires_at"],
        },
        "account": {
            "id": account["id"],
            "email": account["email"],
            "client_id": account["client_id"],
            "onboarding_completed": bool(account.get("onboarding_completed")),
        },
    }


def expected_companion_session_id(auth_session: Dict[str, Any]) -> str:
    """Build the single persistent companion thread ID for an account."""
    return f"companion-{auth_session['id']}"


__all__ = [
    "expected_companion_session_id",
    "normalize_email",
    "normalize_invite_number",
    "serialize_auth_payload",
    "validate_credentials",
    "validate_login_credentials",
    "validate_registration_invite",
]
