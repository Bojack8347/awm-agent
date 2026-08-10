"""Authentication HTTP routes."""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any, Callable, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from api.server.demo_auth import is_demo_auth_enabled
from api.services.registration import RegistrationError


ResponseTuple = Tuple[Any, int]


def create_auth_blueprint(
    *,
    db_available_factory: Callable[[], bool],
    validate_credentials: Callable[[Any, Any], Optional[ResponseTuple]],
    validate_login_credentials: Callable[[Any, Any], Optional[ResponseTuple]],
    validate_registration_invite: Callable[[Any], Optional[ResponseTuple]],
    normalize_email: Callable[[Any], str],
    serialize_auth_payload: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]],
    user_auth_decorator: Callable[[Any], Any],
    bearer_token_getter: Callable[[], str],
    get_account_by_email: Callable[[str], Optional[Dict[str, Any]]],
    create_account: Callable[..., Optional[Dict[str, Any]]],
    create_session: Callable[[str], Optional[Dict[str, Any]]],
    revoke_session: Callable[[str], bool],
    delete_account: Callable[[str, str], bool],
    registration_service_factory: Optional[Callable[[], Any]] = None,
) -> Blueprint:
    """Create auth routes with dependencies supplied by ``api.server``."""
    bp = Blueprint("auth", __name__)

    @bp.route("/api/v1/auth/register", methods=["POST"])
    def register_account() -> ResponseTuple:
        if not db_available_factory():
            return jsonify({
                "success": False,
                "error": "Authentication requires a configured database",
            }), 503

        body = request.get_json(silent=True) or {}
        if registration_service_factory is not None:
            try:
                result = registration_service_factory().register(
                    email=str(body.get("email") or ""),
                    password=str(body.get("password") or ""),
                    invite_code=str(body.get("invite_number", body.get("inviteNumber")) or ""),
                    request_id=str(body.get("client_registration_request_id") or ""),
                )
            except RegistrationError as exc:
                return jsonify({"success": False, "error": exc.code}), exc.status
            result["success"] = True
            result["auth_mode"] = "registered"
            return jsonify(result), (201 if result.get("disposition") == "created" else 200)

        email = body.get("email")
        password = body.get("password")
        invite_number = body.get("invite_number", body.get("inviteNumber"))

        validation_error = validate_credentials(email, password)
        if validation_error:
            return validation_error

        invite_error = validate_registration_invite(invite_number)
        if invite_error:
            return invite_error

        normalized_email = normalize_email(email)
        existing = get_account_by_email(normalized_email)
        if existing:
            return jsonify({
                "success": False,
                "error": "An account with this email already exists",
            }), 409

        account = create_account(
            email=normalized_email,
            password_hash=generate_password_hash(str(password)),
        )
        if not account:
            return jsonify({"success": False, "error": "Failed to create account"}), 500

        session = create_session(account["id"])
        if not session:
            return jsonify({"success": False, "error": "Failed to create session"}), 500

        return jsonify(serialize_auth_payload(account, session)), 200

    @bp.route("/api/v1/auth/login", methods=["POST"])
    def login_account() -> ResponseTuple:
        if not db_available_factory():
            return jsonify({
                "success": False,
                "error": "Authentication requires a configured database",
            }), 503

        body = request.get_json(silent=True) or {}
        email = body.get("email")
        password = body.get("password")

        validation_error = validate_login_credentials(email, password)
        if validation_error:
            return validation_error

        account = get_account_by_email(normalize_email(email))
        if not account or not check_password_hash(account["password_hash"], str(password)):
            return jsonify({
                "success": False,
                "error": "Invalid email or password",
            }), 401

        session = create_session(account["id"])
        if not session:
            return jsonify({"success": False, "error": "Failed to create session"}), 500

        return jsonify(serialize_auth_payload(account, session)), 200

    @bp.route("/api/v1/auth/demo", methods=["POST"])
    def demo_login() -> ResponseTuple:
        """Create or reuse a deterministic demo account for MVP UI acceptance.

        This keeps mock UI testing independent from invite-code setup while
        still returning a real bearer token for protected backend endpoints.
        Set ``ENABLE_DEMO_AUTH=false`` to disable it in stricter environments.
        """
        if not is_demo_auth_enabled():
            return jsonify({"success": False, "error": "Demo auth is disabled"}), 403
        if not db_available_factory():
            return jsonify({
                "success": True,
                "token": "demo-mvp-token",
                "session": {
                    "id": "demo-mvp-session",
                    "expires_at": None,
                },
                "account": {
                    "id": "demo-mvp-account",
                    "email": "demo@awm.local",
                    "client_id": "client-demo-mvp",
                    "onboarding_completed": False,
                },
                "demo": True,
                "persistence": "memory",
            }), 200

        body = request.get_json(silent=True) or {}
        email = normalize_email(body.get("email") or "demo@awm.local")
        account = get_account_by_email(email)
        if not account:
            account = create_account(
                email=email,
                password_hash=generate_password_hash(f"demo:{email}:{secrets.token_urlsafe(32)}"),
            )
        if not account:
            return jsonify({"success": False, "error": "Failed to create demo account"}), 500
        session = create_session(account["id"])
        if not session:
            return jsonify({"success": False, "error": "Failed to create demo session"}), 500
        payload = serialize_auth_payload(account, session)
        payload["demo"] = True
        payload["auth_mode"] = "demo"
        return jsonify(payload), 200

    @bp.route("/api/v1/auth/oauth", methods=["POST"])
    def oauth_login() -> ResponseTuple:
        """Sign in with Google or Apple identity tokens.

        Google verification uses ``google-auth`` when available and the
        ``GOOGLE_OAUTH_CLIENT_ID`` audience is configured. Apple verification
        accepts the mobile-provided identity token shape for MVP and records the
        provider boundary; production can swap in a full JWKS verifier without
        changing this API contract.
        """
        body = request.get_json(silent=True) or {}
        provider = str(body.get("provider") or "").strip().lower()
        token = str(body.get("identity_token") or body.get("id_token") or "").strip()
        if provider not in {"google", "apple"}:
            return jsonify({"success": False, "error": "provider must be google or apple"}), 400
        if not token:
            return jsonify({"success": False, "error": "identity_token is required"}), 400

        identity = _verify_oauth_identity(provider, token)
        if identity.get("error") and identity.get("verification_required"):
            return jsonify({
                "success": False,
                "error": "OAuth token verification failed",
                "details": identity.get("error"),
            }), 401
        email = normalize_email(body.get("email") or identity.get("email"))
        if not email:
            return jsonify({"success": False, "error": "OAuth token did not include an email"}), 400

        if not db_available_factory():
            return jsonify({"success": False, "error": "Authentication requires a configured database"}), 503

        account = get_account_by_email(email)
        if not account:
            account = create_account(
                email=email,
                password_hash=generate_password_hash(f"oauth:{provider}:{secrets.token_urlsafe(32)}"),
            )
        if not account:
            return jsonify({"success": False, "error": "Failed to create account"}), 500

        session = create_session(account["id"])
        if not session:
            return jsonify({"success": False, "error": "Failed to create session"}), 500

        payload = serialize_auth_payload(account, session)
        payload["oauth"] = {
            "provider": provider,
            "subject": identity.get("sub"),
            "verification": identity.get("verification"),
        }
        return jsonify(payload), 200

    @bp.route("/api/v1/auth/me", methods=["GET"])
    @user_auth_decorator
    def get_current_account(auth_session: Dict[str, Any]) -> ResponseTuple:
        return jsonify({
            "success": True,
            "account": {
                "id": auth_session["id"],
                "email": auth_session["email"],
                "client_id": auth_session["client_id"],
                "onboarding_completed": bool(auth_session.get("onboarding_completed")),
            },
            "session": {
                "id": auth_session["session_id"],
                "expires_at": auth_session["expires_at"],
            },
        }), 200

    @bp.route("/api/v1/auth/logout", methods=["POST"])
    @user_auth_decorator
    def logout_account(auth_session: Dict[str, Any]) -> ResponseTuple:
        token = bearer_token_getter()
        revoke_session(token)
        return jsonify({
            "success": True,
            "session_id": auth_session["session_id"],
        }), 200

    @bp.route("/api/v1/auth/account", methods=["DELETE"])
    @user_auth_decorator
    def delete_current_account(auth_session: Dict[str, Any]) -> ResponseTuple:
        if not db_available_factory():
            return jsonify({
                "success": False,
                "error": "Account deletion requires a configured database",
            }), 503

        account_id = auth_session["id"]
        client_id = auth_session["client_id"]

        deleted = delete_account(account_id, client_id)
        if not deleted:
            return jsonify({
                "success": False,
                "error": "Failed to delete account. Please try again or contact support.",
            }), 500

        return jsonify({"success": True}), 200

    return bp


def _verify_oauth_identity(provider: str, token: str) -> Dict[str, Any]:
    if provider == "google":
        audience = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        if audience:
            try:
                from google.auth.transport import requests as google_requests
                from google.oauth2 import id_token as google_id_token

                info = google_id_token.verify_oauth2_token(
                    token,
                    google_requests.Request(),
                    audience,
                )
                return {**info, "verification": "google_oauth2_token"}
            except Exception as exc:
                return {
                    "verification": "google_verification_failed",
                    "verification_required": True,
                    "error": str(exc),
                }
    claims = _decode_jwt_claims_without_verification(token)
    claims["verification"] = f"{provider}_jwt_claims_unverified_mvp"
    return claims


def _decode_jwt_claims_without_verification(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}


__all__ = ["create_auth_blueprint"]
