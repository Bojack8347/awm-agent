"""Persistence for immutable optional external-data decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn

_LOCK = threading.RLock()
_MEMORY: Dict[str, Dict[str, Any]] = {}
_REQUESTS: Dict[tuple[str, str], Dict[str, Any]] = {}


def _fingerprint(decision: str, scopes: List[str], version: str, grant_reference: Optional[str]) -> str:
    value = json.dumps([decision, scopes, version, grant_reference], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


class ExternalDataDecisionRepository:
    def decide(self, *, client_id: str, client_request_id: str, decision: str, scopes: List[str], consent_text_version: str, grant_reference: Optional[str] = None) -> Dict[str, Any]:
        fingerprint = _fingerprint(decision, scopes, consent_text_version, grant_reference)
        pool = _get_pool()
        if pool is None:
            key = (client_id, client_request_id)
            with _LOCK:
                replay = _REQUESTS.get(key)
                if replay:
                    if replay["payload_fingerprint"] != fingerprint:
                        raise ValueError("idempotency_conflict")
                    return copy.deepcopy(replay)
                current = _MEMORY.get(client_id)
                if decision == "revoked" and (not current or current["sharing_decision"] != "granted" or grant_reference != current["decision_id"]):
                    raise ValueError("grant_reference_invalid")
                row = self._row(client_id, client_request_id, decision, scopes, consent_text_version, fingerprint)
                _REQUESTS[key] = row
                _MEMORY[client_id] = row
                return copy.deepcopy(row)
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload_fingerprint, decision_id, decision, scopes, consent_text_version, created_at FROM external_data_decisions WHERE client_id=%s AND client_request_id=%s FOR UPDATE", (client_id, client_request_id))
                replay = cur.fetchone()
                if replay:
                    if replay[0] != fingerprint:
                        conn.rollback(); raise ValueError("idempotency_conflict")
                    conn.rollback()
                    replay_decision = str(replay[2])
                    return {
                        "decision_id": str(replay[1]),
                        "client_id": client_id,
                        "client_request_id": client_request_id,
                        "sharing_decision": replay_decision,
                        "scopes": replay[3] or [],
                        "consent_text_version": replay[4],
                        "workflow_state": {"declined": "skipped_optional", "granted": "permission_granted", "revoked": "revoked"}[replay_decision],
                        "payload_fingerprint": replay[0],
                        "created_at": replay[5].isoformat() if replay[5] else None,
                        "idempotent_replay": True,
                    }
                cur.execute("SELECT decision_id, sharing_decision FROM external_data_current_state WHERE client_id=%s FOR UPDATE", (client_id,))
                current = cur.fetchone()
                if decision == "revoked" and (not current or current[1] != "granted" or str(current[0]) != str(grant_reference)):
                    conn.rollback(); raise ValueError("grant_reference_invalid")
                decision_id = str(uuid.uuid4())
                cur.execute("INSERT INTO external_data_decisions(decision_id, client_id, client_request_id, decision, scopes, consent_text_version, grant_reference, payload_fingerprint) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s)", (decision_id, client_id, client_request_id, decision, json.dumps(scopes), consent_text_version, grant_reference, fingerprint))
                workflow = {"declined": "skipped_optional", "granted": "permission_granted", "revoked": "revoked"}[decision]
                cur.execute("""INSERT INTO external_data_current_state(client_id, decision_id, sharing_decision, active_scopes, workflow_state, connection_status)
                    VALUES (%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(client_id) DO UPDATE SET decision_id=EXCLUDED.decision_id, sharing_decision=EXCLUDED.sharing_decision, active_scopes=EXCLUDED.active_scopes, workflow_state=EXCLUDED.workflow_state, connection_status=CASE WHEN EXCLUDED.sharing_decision='revoked' THEN 'disabled' ELSE external_data_current_state.connection_status END, updated_at=NOW()""",
                    (client_id, decision_id, decision, json.dumps(scopes if decision == "granted" else []), workflow, "disabled" if decision == "revoked" else "not_started"))
            conn.commit()
            return self.get_current(client_id) or {}
        except Exception:
            conn.rollback(); raise
        finally:
            pool.putconn(conn)

    def get_current(self, client_id: str) -> Optional[Dict[str, Any]]:
        pool = _get_pool()
        if pool is None:
            with _LOCK:
                return copy.deepcopy(_MEMORY.get(client_id))
        conn = _safe_getconn(pool)
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT decision_id, sharing_decision, active_scopes, workflow_state, connection_status, updated_at FROM external_data_current_state WHERE client_id=%s", (client_id,))
                row = cur.fetchone() if hasattr(cur, "fetchone") else None
                if not row:
                    return None
                return {"decision_id": str(row[0]), "sharing_decision": row[1], "scopes": row[2] or [], "workflow_state": row[3], "connection_status": row[4], "updated_at": row[5].isoformat() if row[5] else None}
            finally:
                if hasattr(cur, "close"):
                    cur.close()
        finally:
            pool.putconn(conn)

    def set_connection_status(self, client_id: str, status: str) -> None:
        pool = _get_pool()
        if pool is None:
            with _LOCK:
                if client_id in _MEMORY: _MEMORY[client_id]["connection_status"] = status
            return
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE external_data_current_state SET connection_status=%s, updated_at=NOW() WHERE client_id=%s AND sharing_decision='granted'", (status, client_id))
            conn.commit()
        finally:
            pool.putconn(conn)

    @staticmethod
    def _row(client_id: str, request_id: str, decision: str, scopes: List[str], version: str, fingerprint: str) -> Dict[str, Any]:
        return {"decision_id": str(uuid.uuid4()), "client_id": client_id, "client_request_id": request_id, "sharing_decision": decision, "scopes": list(scopes), "consent_text_version": version, "workflow_state": {"declined": "skipped_optional", "granted": "permission_granted", "revoked": "revoked"}[decision], "connection_status": "disabled" if decision == "revoked" else "not_started", "payload_fingerprint": fingerprint}
