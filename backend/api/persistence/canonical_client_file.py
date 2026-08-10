"""Canonical typed Client File writes with atomic version/outbox updates."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Optional
import hashlib
import json
import uuid

from .core import _get_pool, _pg_json, _safe_getconn


SCHEMA_VERSION = 1

_memory_lock = RLock()
_memory_versions: Dict[str, int] = {}
_memory_facts: Dict[str, Dict[str, Dict[str, Any]]] = {}
_memory_confirmation_actions: Dict[tuple[str, str], Dict[str, Any]] = {}

_PERCENTAGE_FIELDS = {
    "expected_return",
    "home_appreciation_rate",
    "inflation_rate",
    "mortgage_interest_rate",
    "withdrawal_rate",
}
_MONEY_FIELDS = {
    "annual_income",
    "annual_spending",
    "cash",
    "college_529",
    "college_goal_amount",
    "home_value",
    "mortgage_balance",
    "retirement_accounts",
    "taxable_brokerage",
}


def _percentage(value: Any) -> Any:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    number = float(value)
    return number / 100.0 if abs(number) > 1.0 else number


def canonical_fact_value(field: str, value: Any) -> Dict[str, Any]:
    """Wrap one confirmed value in an explicit, versioned semantic envelope."""

    if isinstance(value, dict) and value.get("entity_type"):
        normalized = dict(value)
        normalized.setdefault("schema_version", SCHEMA_VERSION)
        if isinstance(normalized.get("percentage_allocation_weights"), dict):
            normalized["percentage_allocation_weights"] = {
                str(key): _percentage(weight)
                for key, weight in normalized["percentage_allocation_weights"].items()
            }
        return normalized

    unit = "scalar"
    normalized_value = value
    if field in _PERCENTAGE_FIELDS or field.endswith("_pct"):
        unit = "decimal_fraction"
        normalized_value = _percentage(value)
    elif field in _MONEY_FIELDS or any(
        token in field for token in ("balance", "income", "spending", "value")
    ):
        unit = "money"
    elif field.endswith("_age") or field == "age" or field.endswith("_years"):
        unit = "years"
    elif isinstance(value, bool):
        unit = "boolean"
    elif isinstance(value, str):
        unit = "text"
    return {
        "schema_version": SCHEMA_VERSION,
        "entity_type": "scalar_fact",
        "field": field,
        "value": normalized_value,
        "unit": unit,
        **({"currency": "USD"} if unit == "money" else {}),
    }


def _memory_write(
    *,
    client_id: str,
    event_type: str,
    payload: Dict[str, Any],
    source: Dict[str, Any],
    writeback: Dict[str, Any],
) -> Dict[str, Any]:
    with _memory_lock:
        confirmation_action_id = str(payload.get("confirmation_action_id") or "")
        action_fingerprint = _payload_fingerprint(payload)
        if confirmation_action_id:
            prior = _memory_confirmation_actions.get((client_id, confirmation_action_id))
            if prior:
                if prior["payload_fingerprint"] != action_fingerprint:
                    raise ValueError("confirmation_action_idempotency_conflict")
                return {**dict(prior["result"]), "idempotent_replay": True}
        version = _memory_versions.get(client_id, 0) + 1
        _memory_versions[client_id] = version
        client_facts = _memory_facts.setdefault(client_id, {})
        incoming_entities = [item for item in (payload.get("entities") or []) if isinstance(item, dict)]
        available_accounts = {
            key for key, item in client_facts.items()
            if isinstance(item.get("value"), dict) and item["value"].get("entity_type") == "account"
        } | {
            str(item.get("entity_id")) for item in incoming_entities if item.get("entity_type") == "account"
        }
        if any(item.get("entity_type") == "holding" and item.get("account_id") not in available_accounts for item in incoming_entities):
            _memory_versions[client_id] = version - 1
            raise ValueError("holding_parent_account_not_found")
        provenance_by_field = (payload.get("metadata") or {}).get("fact_provenance") or {}
        for field, raw_value in (payload.get("facts") or {}).items():
            client_facts[field] = {
                "id": str(uuid.uuid4()),
                "client_id": client_id,
                "schema_version": SCHEMA_VERSION,
                "fact_type": str(payload.get("fact_type") or "captured_fact"),
                "entity_id": field,
                "value": canonical_fact_value(field, raw_value),
                "provenance": {
                    "authority": "client_confirmed",
                    "source": source,
                    **(provenance_by_field.get(field) or {}),
                },
                "original_statement": payload.get("confirmation_text"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        for entity in payload.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            entity = {key: value for key, value in entity.items() if key != "requires_existing_account_validation"}
            entity_id = str(entity.get("entity_id") or "")
            client_facts[entity_id] = {
                "id": str(uuid.uuid4()), "client_id": client_id,
                "schema_version": SCHEMA_VERSION,
                "fact_type": str(payload.get("fact_type") or "captured_fact"),
                "entity_id": entity_id, "value": entity,
                "provenance": {"authority": "client_confirmed", "source": source},
                "original_statement": payload.get("confirmation_text"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
    from api.services.events import safe_publish_event
    from .planning import financial_input_fingerprint, request_planning_refresh

    source_input_fingerprint = financial_input_fingerprint(
        client_id=client_id,
        version=version,
        typed_facts=list(client_facts.values()),
    )
    request_planning_refresh(
        client_id=client_id, version=version,
        source_input_fingerprint=source_input_fingerprint,
    )

    event_payload = {
        "source": source,
        "version": version,
        "changed_fields": sorted((payload.get("facts") or {}).keys()),
        "changed_entity_ids": sorted(str(item.get("entity_id")) for item in (payload.get("entities") or []) if isinstance(item, dict) and item.get("entity_id")),
        "writeback": writeback,
        "source_input_fingerprint": source_input_fingerprint,
    }
    event = safe_publish_event(
        event_type="client_file.updated",
        client_id=client_id,
        aggregate_type="client_file",
        aggregate_id=client_id,
        event_source="advisor_runtime",
        event_key=f"client_file:{client_id}:{version}",
        payload=event_payload,
        status="pending",
    )
    result = {
        "ok": True,
        "event_type": "client_file.updated",
        "client_file_version": version,
        "event": event,
        "writeback": writeback,
    }
    if confirmation_action_id:
        with _memory_lock:
            _memory_confirmation_actions[(client_id, confirmation_action_id)] = {
                "payload_fingerprint": action_fingerprint, "result": dict(result),
            }
    return result


def write_canonical_client_file_update(
    *,
    client_id: str,
    event_type: str,
    payload: Dict[str, Any],
    source: Optional[Dict[str, Any]],
    writeback: Dict[str, Any],
) -> Dict[str, Any]:
    """Commit typed facts, version, refresh state, and outbox event together."""

    source_payload = dict(source or {})
    pool = _get_pool()
    if pool is None:
        return _memory_write(
            client_id=client_id,
            event_type=event_type,
            payload=payload,
            source=source_payload,
            writeback=writeback,
        )

    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE clients
                SET client_file_version = client_file_version + 1, updated_at = NOW()
                WHERE client_id = %s
                RETURNING client_file_version
                """,
                (client_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("client_identity_missing")
            version = int(row[0])
            confirmation_action_id = str(payload.get("confirmation_action_id") or "")
            action_fingerprint = _payload_fingerprint(payload)
            if confirmation_action_id:
                cur.execute(
                    "SELECT payload_fingerprint,client_file_version,entity_ids FROM client_file_confirmation_actions WHERE client_id=%s AND confirmation_action_id=%s",
                    (client_id, confirmation_action_id),
                )
                prior = cur.fetchone()
                if prior:
                    if prior[0] != action_fingerprint:
                        raise ValueError("confirmation_action_idempotency_conflict")
                    conn.rollback()
                    return {"ok": True, "event_type": "client_file.updated", "client_file_version": int(prior[1]), "entity_ids": list(prior[2] or []), "idempotent_replay": True, "event": None, "writeback": writeback}
            provenance_by_field = (payload.get("metadata") or {}).get("fact_provenance") or {}
            for field, raw_value in (payload.get("facts") or {}).items():
                provenance = {
                    "authority": "client_confirmed",
                    "source": source_payload,
                    **(provenance_by_field.get(field) or {}),
                }
                cur.execute(
                    """
                    INSERT INTO canonical_client_facts
                        (client_id, schema_version, fact_type, entity_id, value,
                         provenance, original_statement, observed_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                            NULLIF(%s, '')::timestamptz)
                    ON CONFLICT (client_id, entity_id)
                    DO UPDATE SET
                        schema_version = EXCLUDED.schema_version,
                        fact_type = EXCLUDED.fact_type,
                        value = EXCLUDED.value,
                        provenance = EXCLUDED.provenance,
                        original_statement = EXCLUDED.original_statement,
                        observed_at = EXCLUDED.observed_at,
                        updated_at = NOW()
                    """,
                    (
                        client_id,
                        SCHEMA_VERSION,
                        str(payload.get("fact_type") or "captured_fact"),
                        field,
                        _pg_json(canonical_fact_value(field, raw_value)),
                        _pg_json(provenance),
                        payload.get("confirmation_text"),
                        str((payload.get("metadata") or {}).get("observed_at") or ""),
                    ),
                )
            entities = [item for item in (payload.get("entities") or []) if isinstance(item, dict)]
            entity_ids = {str(item.get("entity_id") or "") for item in entities}
            incoming_account_ids = {
                str(item.get("entity_id") or "")
                for item in entities if item.get("entity_type") == "account"
            }
            referenced_accounts = {
                str(item.get("account_id") or "")
                for item in entities if item.get("entity_type") == "holding" and item.get("account_id")
            } - incoming_account_ids
            if referenced_accounts:
                cur.execute(
                    "SELECT entity_id FROM canonical_client_facts WHERE client_id=%s AND entity_id=ANY(%s) AND value->>'entity_type'='account'",
                    (client_id, list(referenced_accounts)),
                )
                if {str(item[0]) for item in cur.fetchall()} != referenced_accounts:
                    raise ValueError("holding_parent_account_not_found")
            for raw_entity in entities:
                entity = {key: value for key, value in raw_entity.items() if key != "requires_existing_account_validation"}
                entity_id = str(entity.get("entity_id") or "")
                cur.execute(
                    """
                    INSERT INTO canonical_client_facts
                      (client_id,schema_version,fact_type,entity_id,value,provenance,original_statement,observed_at)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,NULLIF(%s,'')::timestamptz)
                    ON CONFLICT (client_id,entity_id) DO UPDATE SET
                      schema_version=EXCLUDED.schema_version,fact_type=EXCLUDED.fact_type,
                      value=EXCLUDED.value,provenance=EXCLUDED.provenance,
                      original_statement=EXCLUDED.original_statement,
                      observed_at=EXCLUDED.observed_at,updated_at=NOW()
                    """,
                    (client_id, SCHEMA_VERSION, str(payload.get("fact_type") or "captured_fact"), entity_id,
                     _pg_json(entity), _pg_json({"authority": "client_confirmed", "source": source_payload}),
                     payload.get("confirmation_text"), str(entity.get("reported_at") or (payload.get("metadata") or {}).get("observed_at") or "")),
                )
            from .planning import request_planning_refresh_in_transaction

            source_input_fingerprint = request_planning_refresh_in_transaction(
                cur, client_id=client_id, version=version,
            )
            event_payload = {
                "source": source_payload,
                "version": version,
                "changed_fields": sorted((payload.get("facts") or {}).keys()),
                "changed_entity_ids": sorted(entity_ids),
                "writeback": writeback,
                "source_input_fingerprint": source_input_fingerprint,
            }
            cur.execute(
                """
                INSERT INTO business_events
                    (event_key, client_id, aggregate_type, aggregate_id, event_type,
                     event_source, status, payload)
                VALUES (%s, %s, 'client_file', %s, 'client_file.updated',
                        'advisor_runtime', 'pending', %s::jsonb)
                RETURNING id
                """,
                (
                    f"client_file:{client_id}:{version}",
                    client_id,
                    client_id,
                    _pg_json(event_payload),
                ),
            )
            event_id = str(cur.fetchone()[0])
            if confirmation_action_id:
                cur.execute(
                    "INSERT INTO client_file_confirmation_actions (client_id,confirmation_action_id,payload_fingerprint,client_file_version,entity_ids) VALUES (%s,%s,%s,%s,%s::jsonb)",
                    (client_id, confirmation_action_id, action_fingerprint, version, _pg_json(sorted(entity_ids))),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
    return {
        "ok": True,
        "event_type": "client_file.updated",
        "client_file_version": version,
        "event": {
            "id": event_id,
            "event_key": f"client_file:{client_id}:{version}",
            "event_type": "client_file.updated",
            "status": "pending",
            "payload": event_payload,
        },
        "writeback": writeback,
        "entity_ids": sorted(entity_ids),
    }


def _payload_fingerprint(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        {"facts": payload.get("facts") or {}, "entities": payload.get("entities") or [], "draft_group_ids": payload.get("draft_group_ids") or []},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def list_canonical_client_facts(*, client_id: str) -> List[Dict[str, Any]]:
    """Read authoritative typed facts; process-local storage mirrors DB semantics."""

    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            return [dict(item) for item in _memory_facts.get(client_id, {}).values()]
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, client_id, schema_version, fact_type, entity_id, value,
                           provenance, original_statement, observed_at, created_at, updated_at
                    FROM canonical_client_facts
                    WHERE client_id = %s
                    ORDER BY fact_type, entity_id
                    """,
                    (client_id,),
                )
                columns = [item[0] for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as exc:
            conn.rollback()
            if getattr(exc, "pgcode", None) not in {"42P01", "42703"}:
                raise
            rows = []
    finally:
        pool.putconn(conn)
    for row in rows:
        for key in ("id", "observed_at", "created_at", "updated_at"):
            if row.get(key) is not None:
                row[key] = str(row[key])
    return rows


def get_client_file_version(*, client_id: str) -> int:
    """Read the current monotonic input version."""

    pool = _get_pool()
    if pool is None:
        with _memory_lock:
            return int(_memory_versions.get(client_id, 0))
    conn = _safe_getconn(pool)
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT client_file_version FROM clients WHERE client_id = %s",
                    (client_id,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            conn.rollback()
            if getattr(exc, "pgcode", None) not in {"42P01", "42703"}:
                raise
            return 0
    finally:
        pool.putconn(conn)
