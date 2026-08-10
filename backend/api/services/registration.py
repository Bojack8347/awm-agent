"""Deterministic account registration boundary."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Callable, Dict

from werkzeug.security import generate_password_hash


class RegistrationError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class RegistrationService:
    def __init__(self, *, register_transaction: Callable[..., Dict[str, Any]]):
        self._register_transaction = register_transaction

    def register(self, *, email: str, password: str, invite_code: str, request_id: str) -> Dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        code = str(invite_code or "").strip()
        try:
            uuid.UUID(str(request_id))
        except (ValueError, TypeError) as exc:
            raise RegistrationError("client_registration_request_id_invalid") from exc
        if not normalized_email or "@" not in normalized_email:
            raise RegistrationError("email_invalid")
        if len(str(password or "")) < 8:
            raise RegistrationError("password_policy_failed")
        if not code:
            raise RegistrationError("invitation_required")
        invitation_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        identity_fingerprint = hashlib.sha256(f"{normalized_email}:{invitation_hash}".encode("utf-8")).hexdigest()
        result = self._register_transaction(
            request_id=str(request_id),
            normalized_email=normalized_email,
            password=str(password),
            password_hash=generate_password_hash(str(password)),
            invitation_hash=invitation_hash,
            identity_fingerprint=identity_fingerprint,
        )
        if not result:
            raise RegistrationError("registration_unavailable", 503)
        if result.get("error"):
            code = str(result["error"])
            status = 409 if code in {"duplicate_email", "idempotency_conflict", "invitation_redeemed"} else 403 if code.startswith("invitation_") else 503
            raise RegistrationError(code, status)
        return result
