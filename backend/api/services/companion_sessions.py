"""Application service for Companion conversation lifecycle and ownership."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from api.persistence.companion_sessions import (
    close_companion_session,
    create_companion_session,
    get_companion_session,
    list_companion_sessions,
    touch_companion_session,
)


class CompanionSessionService:
    def create(self, *, auth_session: Dict[str, Any], launch_id: str, origin: str = "cold_launch") -> Dict[str, Any]:
        return create_companion_session(
            client_id=str(auth_session["client_id"]),
            account_id=str(auth_session.get("account_id") or auth_session["id"]),
            launch_id=launch_id,
            origin=origin,
        )

    def continue_from(self, *, auth_session: Dict[str, Any], previous_session_id: str, request_id: str) -> Dict[str, Any]:
        return create_companion_session(
            client_id=str(auth_session["client_id"]),
            account_id=str(auth_session.get("account_id") or auth_session["id"]),
            origin="continued",
            previous_session_id=previous_session_id,
            continuation_request_id=request_id,
        )

    def get_owned(self, *, auth_session: Dict[str, Any], session_id: str, require_active: bool = False) -> Optional[Dict[str, Any]]:
        row = get_companion_session(session_id)
        account_id = str(auth_session.get("account_id") or auth_session["id"])
        if row is None or row["client_id"] != str(auth_session["client_id"]) or row["account_id"] != account_id:
            return None
        if require_active and row["status"] != "active":
            return None
        return row

    def authorize_write(self, *, auth_session: Dict[str, Any], session_id: str) -> str:
        row = self.get_owned(auth_session=auth_session, session_id=session_id)
        if row is not None:
            if row["status"] != "active":
                return "session_closed"
            touch_companion_session(
                session_id=session_id,
                client_id=row["client_id"],
                account_id=row["account_id"],
            )
            return "ok"

        # Explicit compatibility switch for automated migration tests only.
        legacy = f"companion-{auth_session['id']}"
        if (
            session_id == legacy
            and os.getenv("AWM_ALLOW_LEGACY_COMPANION_WRITES", "false").strip().lower()
            in {"1", "true", "yes"}
        ):
            return "ok"
        return "forbidden"

    def list(self, *, auth_session: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
        return list_companion_sessions(client_id=str(auth_session["client_id"]), limit=limit)

    def close(self, *, auth_session: Dict[str, Any], session_id: str, expected_version: int) -> Optional[Dict[str, Any]]:
        return close_companion_session(
            session_id=session_id,
            client_id=str(auth_session["client_id"]),
            account_id=str(auth_session.get("account_id") or auth_session["id"]),
            expected_lifecycle_version=expected_version,
        )
