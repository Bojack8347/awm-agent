"""Money pool persistence: the client-intent layer above proposal/policy/holdings.

A money pool is one pool of money with its own attributes and lifecycle. Pools
are upserted by (client_id, label) so the RM filling in a pool over several turns
updates it instead of creating duplicates; every meaningful change writes a
money_pool_events row (the audit trail + per-pool cash-flow timeline).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .core import _get_pool, _safe_getconn, database_mode


class MoneyPoolPersistenceError(RuntimeError):
    """Base error for durable money-pool writes."""


class DatabaseModeUnsupportedError(MoneyPoolPersistenceError):
    """Durable money pools are unavailable in the configured database mode."""


class DatabaseUnavailableError(MoneyPoolPersistenceError):
    """The configured database cannot currently be reached."""


class SchemaOutdatedError(MoneyPoolPersistenceError):
    """The database is reachable but lacks the required money-pool schema."""


class PersistenceValidationError(MoneyPoolPersistenceError):
    """The database rejected a money-pool write on a declared constraint."""


class ClientIdentityMissingError(PersistenceValidationError):
    """The client-scoped write has no matching identity-root row."""


class UnknownPersistenceError(MoneyPoolPersistenceError):
    """An unclassified database failure prevented the write."""


def _typed_write_error(exc: Exception) -> MoneyPoolPersistenceError:
    """Preserve structural PostgreSQL failure classes at the repository boundary."""

    code = str(getattr(exc, "pgcode", "") or "")
    name = exc.__class__.__name__
    constraint = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
    if code == "23503" and (
        constraint.endswith("_client") or 'not present in table "clients"' in str(exc)
    ):
        return ClientIdentityMissingError("client identity root is missing")
    if code == "42P01" or name == "UndefinedTable":
        return SchemaOutdatedError("required money-pool relation is missing")
    if code.startswith("23") or name in {
        "CheckViolation",
        "ForeignKeyViolation",
        "IntegrityError",
        "NotNullViolation",
        "UniqueViolation",
    }:
        return PersistenceValidationError("money-pool write violated a database constraint")
    if code.startswith("08") or name in {"InterfaceError", "OperationalError"}:
        return DatabaseUnavailableError("database connection is unavailable")
    return UnknownPersistenceError("money-pool write failed")

# Plain scalar columns the caller may set/update.
_TEXT_FIELDS = (
    "purpose_type", "description", "beneficiary", "currency", "funding_source",
    "source_account_ref", "horizon_hardness", "risk_tolerance", "objective",
)
_JSON_FIELDS = ("constraints", "funding_schedule", "drawdown_schedule")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Single source of truth for the `upsert_money_pool` tool the LLM is given. Both
# v2 voice/text channels share this deterministic money-pool contract.
# only in how the LLM is invoked, not in this tool's contract or its handler
# (upsert_money_pool below). Keep schema + handler co-located so they never drift.
MONEY_POOL_TOOL_NAME = "upsert_money_pool"
MONEY_POOL_TOOL_DESCRIPTION = (
    "Record or update a 'money pool' — a distinct pot of the client's money that has its "
    "own purpose, time horizon, and risk appetite (e.g. 'Kids education', 'RSU growth', "
    "'Emergency reserve'). Call this ONLY when the client has firmly committed to setting a "
    "specific pot aside to invest or save, with at least a rough amount they stated. A vague "
    "or hypothetical mention ('maybe a college fund someday', 'we might...') is NOT yet a "
    "pool — do not create one until it firms up. NEVER invent or assume an amount or any "
    "other field: pass `amount` only if the client actually gave a figure for THIS pot; "
    "otherwise omit it (do not fill a placeholder like 100k). Use the SAME label to refine a "
    "pool you already recorded; pass only the fields the client actually told you."
)
MONEY_POOL_TOOL_PROPERTIES: Dict[str, Any] = {
    "label": {"type": "string", "description": "Short stable name for this pool, e.g. 'Kids education'."},
    "amount": {"type": "number", "description": "Approximate money in this pool."},
    "purpose_type": {"type": "string", "enum": ["education", "retirement", "home", "emergency", "legacy", "growth", "purchase", "other"]},
    "risk_tolerance": {"type": "string", "description": "This pool's risk appetite, e.g. conservative / moderate / aggressive."},
    "objective": {"type": "string", "enum": ["growth", "income", "preservation", "liquidity"]},
    "horizon": {"type": "string", "description": "Target date (YYYY-MM-DD) or a phrase like '15 years'."},
    "funding_source": {"type": "string", "description": "Where the money comes from, e.g. 'vested RSU sale'."},
    "beneficiary": {"type": "string", "description": "Who it is for, if relevant."},
    "description": {"type": "string", "description": "Any extra context for this pool."},
}
MONEY_POOL_TOOL_REQUIRED = ("label",)


def _coerce_amount(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_PURPOSE_TYPE_ENUM = (
    "education",
    "retirement",
    "home",
    "emergency",
    "legacy",
    "growth",
    "purchase",
    "other",
)
_PURPOSE_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("education", ("college", "education", "tuition", "529", "school", "university")),
    ("retirement", ("retire", "retirement", "pension", "401k", "ira")),
    ("home", ("home", "house", "mortgage", "down payment")),
    ("emergency", ("emergency", "rainy day", "cash reserve")),
    ("legacy", ("legacy", "estate", "inheritance")),
    ("purchase", ("purchase", "buy a", "car ", "wedding")),
    ("growth", ("growth", "wealth", "diversif", "brokerage", "invest")),
)


def _infer_purpose_type(purpose: str) -> Optional[str]:
    """Map free-text Agents `purpose` onto the durable purpose_type enum."""

    text = str(purpose or "").strip().lower()
    if not text:
        return None
    if text in _PURPOSE_TYPE_ENUM:
        return text
    for purpose_type, keywords in _PURPOSE_TYPE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return purpose_type
    return "other"


def _derive_state(amount: Any, purpose_type: Any, explicit: Any) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if amount is not None and purpose_type:
        return "defined"
    return "mentioned"


def _clean_updates(pool: Dict[str, Any]) -> Dict[str, Any]:
    """Pull only the provided (non-None) settable fields out of a pool dict, with
    light coercion. horizon goes to horizon_date if it's an ISO date, otherwise
    its raw form is stashed in payload.horizon_text so nothing is lost."""
    updates: Dict[str, Any] = {}
    payload_extra: Dict[str, Any] = {}
    for f in _TEXT_FIELDS:
        v = pool.get(f)
        if isinstance(v, str) and v.strip():
            updates[f] = v.strip()
    # The Agents-facing schema predates the persistence schema and calls these
    # fields `purpose`, `source_of_funds`, and `horizon_years`. Preserve every
    # client-supplied value and map Agents aliases onto durable columns so
    # readiness checks (purpose_type / horizon) can clear.
    purpose = pool.get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        purpose_text = purpose.strip()
        if not updates.get("objective"):
            updates["objective"] = purpose_text
        if not updates.get("purpose_type"):
            inferred = _infer_purpose_type(purpose_text)
            if inferred:
                updates["purpose_type"] = inferred
    source_of_funds = pool.get("source_of_funds")
    if not updates.get("funding_source") and isinstance(source_of_funds, str) and source_of_funds.strip():
        updates["funding_source"] = source_of_funds.strip()
    if "amount" in pool and pool.get("amount") is not None:
        amt = _coerce_amount(pool.get("amount"))
        if amt is not None:
            updates["amount"] = amt
    if "priority" in pool and pool.get("priority") is not None:
        try:
            updates["priority"] = int(pool["priority"])
        except (TypeError, ValueError):
            pass
    for f in _JSON_FIELDS:
        v = pool.get(f)
        if isinstance(v, dict) and v:
            updates[f] = json.dumps(v)
    preference_constraints: Dict[str, Any] = {}
    existing_constraints = pool.get("constraints")
    if isinstance(existing_constraints, dict):
        preference_constraints.update(existing_constraints)
    for key in (
        "liquidity_needs",
        "liquidity_constraint_mode",
        "complexity_preference",
        "asset_class_preferences",
        "exclusions",
        "special_considerations",
        "tax_considerations",
    ):
        value = pool.get(key)
        if isinstance(value, str) and value.strip():
            preference_constraints[key] = value.strip()
        elif isinstance(value, list) and value:
            preference_constraints[key] = [
                str(item).strip() for item in value if str(item).strip()
            ]
    if preference_constraints:
        updates["constraints"] = json.dumps(preference_constraints)
    horizon_years = pool.get("horizon_years")
    if horizon_years not in (None, ""):
        try:
            years = float(horizon_years)
            if years >= 0:
                years_value = int(years) if years.is_integer() else years
                payload_extra["horizon_years"] = years_value
                # Readiness checks look for horizon_date/horizon_text; mirror years
                # into horizon_text so Agents-schema pools clear the horizon gap.
                if not payload_extra.get("horizon_text") and not updates.get("horizon_date"):
                    payload_extra["horizon_text"] = f"{years_value} years"
        except (TypeError, ValueError):
            pass
    horizon = pool.get("horizon_date") or pool.get("horizon") or pool.get("horizon_text")
    if isinstance(horizon, str) and horizon.strip():
        if _ISO_DATE.match(horizon.strip()):
            updates["horizon_date"] = horizon.strip()
        else:
            payload_extra["horizon_text"] = horizon.strip()
    return {"updates": updates, "payload_extra": payload_extra}


def _row_to_dict(row: Any, cols: List[str]) -> Dict[str, Any]:
    d = dict(zip(cols, row))
    if d.get("amount") is not None:
        d["amount"] = float(d["amount"])
    for k in ("created_at", "updated_at", "horizon_date"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for k in ("id", "overflow_pool_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    payload = d.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
        d["payload"] = payload
    if isinstance(payload, dict) and payload.get("horizon_text"):
        d["horizon_text"] = str(payload["horizon_text"])
    if isinstance(payload, dict) and payload.get("horizon_years") not in (None, ""):
        d["horizon_years"] = payload["horizon_years"]
    constraints = d.get("constraints")
    if isinstance(constraints, str):
        try:
            constraints = json.loads(constraints)
        except (TypeError, ValueError):
            constraints = {}
        d["constraints"] = constraints
    if isinstance(constraints, dict):
        for key in (
            "liquidity_needs",
            "liquidity_constraint_mode",
            "complexity_preference",
            "asset_class_preferences",
            "exclusions",
            "special_considerations",
            "tax_considerations",
        ):
            if constraints.get(key) not in (None, "", []):
                d[key] = constraints[key]
    return d


_SELECT_COLS = [
    "id", "client_id", "label", "state", "purpose_type", "description",
    "beneficiary", "amount", "currency", "funding_source", "source_account_ref",
    "horizon_date", "horizon_hardness", "risk_tolerance", "objective",
    "constraints", "funding_schedule", "drawdown_schedule", "priority",
    "overflow_pool_id", "payload", "created_at", "updated_at",
]


def _add_event(cur, pool_id: str, client_id: str, event_type: str,
               from_state: Optional[str] = None, to_state: Optional[str] = None,
               amount: Optional[float] = None, payload: Optional[Dict[str, Any]] = None) -> None:
    cur.execute(
        """
        INSERT INTO money_pool_events
            (pool_id, client_id, event_type, from_state, to_state, amount, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (pool_id, client_id, event_type, from_state, to_state, amount,
         json.dumps(payload or {})),
    )


def upsert_money_pool(client_id: str, pool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Create or update a pool by (client_id, label). Returns the stored pool.

    Partial fills are supported: only the provided fields are written, so a pool
    can start in `mentioned` with just a label and accrete attributes over later
    turns/sessions (moving to `defined`). Writes a money_pool_events row.
    """
    label = str(pool.get("label") or "").strip()
    if not client_id or not label:
        return None
    cleaned = _clean_updates(pool)
    updates = cleaned["updates"]
    payload_extra = cleaned["payload_extra"]

    try:
        pg = _get_pool()
    except RuntimeError as exc:
        raise DatabaseUnavailableError("database connection is unavailable") from exc
    if pg is None:
        if database_mode() == "off" or not os.getenv("DATABASE_URL", "").strip():
            raise DatabaseModeUnsupportedError(
                "durable money pools require a configured PostgreSQL database"
            )
        raise DatabaseUnavailableError("database connection is unavailable")
    try:
        conn = _safe_getconn(pg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, state, purpose_type, amount, payload FROM money_pools "
                    "WHERE client_id = %s AND lower(label) = lower(%s)",
                    (client_id, label),
                )
                existing = cur.fetchone()

                if existing:
                    pool_id, old_state, old_purpose, old_amount, old_payload = existing
                    pool_id = str(pool_id)
                    merged_amount = updates.get("amount", float(old_amount) if old_amount is not None else None)
                    merged_purpose = updates.get("purpose_type", old_purpose)
                    new_state = _derive_state(merged_amount, merged_purpose, pool.get("state"))
                    # A plain re-mention/refinement (the RM hears the pool again and
                    # re-upserts it) must NEVER downgrade a pool that has already
                    # advanced past 'defined' in its lifecycle — e.g. an executed
                    # pool sitting at 'active'. _derive_state would otherwise reset it
                    # to 'defined'. Only an explicit state in the call may move it.
                    if not pool.get("state") and old_state not in (None, "", "mentioned", "defined"):
                        new_state = old_state
                    if payload_extra:
                        merged_payload = dict(old_payload or {})
                        merged_payload.update(payload_extra)
                        updates["payload"] = json.dumps(merged_payload)
                    set_parts = [f"{k} = %s" for k in updates]
                    set_parts.append("state = %s")
                    set_parts.append("updated_at = NOW()")
                    params = list(updates.values()) + [new_state, pool_id]
                    cur.execute(
                        f"UPDATE money_pools SET {', '.join(set_parts)} WHERE id = %s",
                        params,
                    )
                    if new_state != old_state:
                        _add_event(cur, pool_id, client_id, "state_change",
                                   from_state=old_state, to_state=new_state,
                                   payload={"via": "upsert"})
                    else:
                        _add_event(cur, pool_id, client_id, "updated",
                                   payload={"fields": list(updates.keys())})
                else:
                    state = _derive_state(updates.get("amount"), updates.get("purpose_type"), pool.get("state"))
                    cols = ["client_id", "label", "state"] + list(updates.keys())
                    vals = [client_id, label, state] + list(updates.values())
                    if payload_extra and "payload" not in updates:
                        cols.append("payload")
                        vals.append(json.dumps(payload_extra))
                    placeholders = ", ".join(["%s"] * len(vals))
                    cur.execute(
                        f"INSERT INTO money_pools ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
                        vals,
                    )
                    pool_id = str(cur.fetchone()[0])
                    _add_event(cur, pool_id, client_id, "created",
                               to_state=state, payload={"label": label})

                cur.execute(
                    f"SELECT {', '.join(_SELECT_COLS)} FROM money_pools WHERE id = %s",
                    (pool_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row, _SELECT_COLS) if row else None
        finally:
            pg.putconn(conn)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[db] Failed to upsert money pool: {exc}", flush=True)
        raise _typed_write_error(exc) from exc


def list_money_pools(client_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return the client's pools, highest priority first."""
    pg = _get_pool()
    if pg is None:
        return []
    try:
        conn = _safe_getconn(pg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(_SELECT_COLS)} FROM money_pools "
                    "WHERE client_id = %s ORDER BY priority DESC, created_at ASC LIMIT %s",
                    (client_id, limit),
                )
                rows = cur.fetchall()
        finally:
            pg.putconn(conn)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[db] Failed to list money pools: {exc}", flush=True)
        return []
    return [_row_to_dict(r, _SELECT_COLS) for r in rows]


def list_money_pool_events(pool_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Return a pool's lifecycle/flow events, oldest first."""
    pg = _get_pool()
    if pg is None:
        return []
    cols = ["id", "pool_id", "client_id", "event_type", "from_state", "to_state",
            "amount", "payload", "occurred_at"]
    try:
        conn = _safe_getconn(pg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(cols)} FROM money_pool_events "
                    "WHERE pool_id = %s ORDER BY occurred_at ASC LIMIT %s",
                    (pool_id, limit),
                )
                rows = cur.fetchall()
        finally:
            pg.putconn(conn)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[db] Failed to list money pool events: {exc}", flush=True)
        return []
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("amount") is not None:
            d["amount"] = float(d["amount"])
        for k in ("id", "pool_id"):
            d[k] = str(d[k])
        if d.get("occurred_at") is not None:
            d["occurred_at"] = d["occurred_at"].isoformat()
        out.append(d)
    return out
