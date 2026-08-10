"""Deterministic consent state machine for optional provider data."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, Optional

from api.persistence.external_data import ExternalDataDecisionRepository

ALLOWED_SCOPES = {"account_identity", "balances", "holdings", "transactions", "insurance"}


class ExternalDataDecisionService:
    def __init__(self, repository: Optional[ExternalDataDecisionRepository] = None):
        self.repository = repository or ExternalDataDecisionRepository()

    def decide(self, *, client_id: str, decision: str, scopes: Iterable[str], consent_text_version: str, client_request_id: str, grant_reference: Optional[str] = None) -> Dict[str, Any]:
        decision = str(decision).strip().lower()
        normalized_scopes = sorted({str(item).strip() for item in scopes if str(item).strip()})
        try: uuid.UUID(client_request_id)
        except (ValueError, TypeError) as exc: raise ValueError("client_request_id_invalid") from exc
        if decision not in {"declined", "granted", "revoked"}: raise ValueError("decision_invalid")
        if not consent_text_version: raise ValueError("consent_text_version_required")
        if set(normalized_scopes) - ALLOWED_SCOPES: raise ValueError("scope_invalid")
        if decision == "granted" and not normalized_scopes: raise ValueError("scopes_required")
        if decision != "granted" and normalized_scopes: raise ValueError("scopes_not_allowed")
        return self.repository.decide(client_id=client_id, client_request_id=client_request_id, decision=decision, scopes=normalized_scopes, consent_text_version=consent_text_version, grant_reference=grant_reference)

    def current(self, client_id: str) -> Dict[str, Any]:
        return self.repository.get_current(client_id) or {"sharing_decision": "not_requested", "scopes": [], "workflow_state": "not_requested", "connection_status": "not_started"}

    def require_grant(self, client_id: str, scopes: Iterable[str]) -> Dict[str, Any]:
        current = self.current(client_id)
        if current["sharing_decision"] != "granted" or not set(scopes).issubset(set(current.get("scopes") or [])):
            raise ValueError("active_permission_required")
        return current
