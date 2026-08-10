"""Deterministic, auditable financial-position resolver."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from client_file.fact_vocabulary import normalize_fact_keys


RESOLVER_POLICY_VERSION = "financial_position.v1"
PROVIDER_FRESHNESS_DAYS = 7
PROVIDER_VALUE_TOLERANCE = Decimal("0.10")

ASSET_FIELDS = {
    "cash": "cash", "taxable_brokerage": "taxable_brokerage",
    "retirement_accounts": "retirement", "college_529": "education",
    "home_value": "real_estate",
}
LIABILITY_FIELDS = {"mortgage_balance": "mortgage", "liabilities": "liabilities", "debt": "debt", "debts": "debts"}


def legacy_account_id(client_id: str, field: str) -> str:
    return f"account:{uuid.uuid5(uuid.NAMESPACE_URL, f'{client_id}:legacy-scalar:{field}')}"


def resolve_financial_position(
    *, client_id: str, client_file: Dict[str, Any],
    provider_observations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    version = int(client_file.get("client_file_version") or 0)
    rows = _canonical_rows(client_file)
    scalars: Dict[str, Dict[str, Any]] = {}
    accounts: Dict[str, Dict[str, Any]] = {}
    holdings: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    missing: List[str] = []

    for row in rows:
        envelope = row.get("value") if isinstance(row.get("value"), dict) else {}
        entity_type = str(envelope.get("entity_type") or "scalar_fact")
        provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
        if provenance.get("authority") not in {None, "client_confirmed"}:
            continue
        entity_id = str(row.get("entity_id") or envelope.get("field") or "")
        if entity_type == "account":
            accounts[entity_id] = {**envelope, "entity_id": entity_id, "provenance": provenance}
        elif entity_type == "holding":
            holdings[entity_id] = {**envelope, "entity_id": entity_id, "provenance": provenance}
        elif entity_type == "scalar_fact":
            field = next(iter(normalize_fact_keys({entity_id: envelope.get("value")})), entity_id)
            scalars[field] = {"value": envelope.get("value"), "entity_id": entity_id, "provenance": provenance, "valuation_as_of": row.get("observed_at")}

    # Project legacy aggregate facts into stable account identities. A typed
    # account carrying legacy_scalar_ref supersedes the virtual projection.
    materialized_fields = {str(item.get("legacy_scalar_ref")) for item in accounts.values() if item.get("legacy_scalar_ref")}
    for field, account_type in ASSET_FIELDS.items():
        if field not in scalars or field in materialized_fields:
            continue
        amount = _decimal(scalars[field].get("value"))
        if amount is None:
            continue
        account_id = legacy_account_id(client_id, field)
        accounts.setdefault(account_id, {
            "entity_id": account_id, "entity_type": "account",
            "account_type": account_type, "currency": "USD",
            "total_balance": amount, "holdings_coverage": "unknown",
            "lifecycle_status": "active", "legacy_scalar_ref": field,
            "provenance": scalars[field].get("provenance") or {},
        })

    observations = [
        item for item in (
            provider_observations if provider_observations is not None else list(client_file.get("linked_accounts") or [])
        )
        if isinstance(item, dict) and item.get("active_for_current_advice", True) is not False
    ]
    provider_revisions = sorted({str(item.get("revision_id") or item.get("observation_id") or item.get("id")) for item in observations if isinstance(item, dict) and (item.get("revision_id") or item.get("observation_id") or item.get("id"))})
    _reconcile_provider_values(holdings, observations, conflicts)

    eligible_holdings: Dict[str, Dict[str, Any]] = {}
    for holding_id, holding in holdings.items():
        if holding.get("lifecycle_status") != "active":
            continue
        if holding.get("ownership_status") not in {"beneficially_owned", "settled"}:
            continue
        account_id = str(holding.get("account_id") or "")
        if not account_id or account_id not in accounts:
            missing.append(f"holding_parent_account:{holding_id}")
            continue
        if holding.get("account_relationship_status") == "unresolved":
            conflicts.append({"code": "holding_account_relationship_unresolved", "holding_id": holding_id})
            continue
        eligible_holdings[holding_id] = holding

    currencies = {
        str(item.get("currency") or "USD").upper()
        for item in [*accounts.values(), *eligible_holdings.values()]
        if item.get("lifecycle_status") == "active"
    }
    if any(currency != "USD" for currency in currencies):
        conflicts.append({"code": "cross_currency_conversion_required", "currencies": sorted(currencies)})

    operands: List[Dict[str, Any]] = []
    disclosed_partial = False
    for account_id, account in sorted(accounts.items()):
        if account.get("lifecycle_status") != "active":
            continue
        children = [item for item in eligible_holdings.values() if item.get("account_id") == account_id]
        total = _decimal(account.get("total_balance"))
        coverage = str(account.get("holdings_coverage") or "unknown")
        if total is not None:
            value = total
            reason = "authoritative_account_total"
        elif coverage == "complete" and children:
            value = sum((_decimal(item.get("market_value")) or Decimal(0) for item in children), Decimal(0))
            reason = "complete_holding_decomposition"
        elif children:
            value = sum((_decimal(item.get("market_value")) or Decimal(0) for item in children), Decimal(0))
            reason = "known_disclosed_holdings_partial_account"
            disclosed_partial = True
            missing.append(f"account_total_or_complete_coverage:{account_id}")
        else:
            continue
        operands.append(_operand(
            operand_id=account_id, value=value, direction="add",
            reason=reason, source=account.get("provenance") or {},
            version=version, parent_account_id=None,
            valuation_as_of=account.get("valuation_as_of"),
            evidence_holding_ids=[item["entity_id"] for item in children],
            source_field_id=account.get("legacy_scalar_ref"),
        ))

    for field in LIABILITY_FIELDS:
        item = scalars.get(field)
        value = _decimal(item.get("value")) if item else None
        if value is not None:
            operands.append(_operand(
                operand_id=field, value=abs(value), direction="subtract",
                reason="client_confirmed_liability", source=item.get("provenance") or {},
                version=version, valuation_as_of=item.get("valuation_as_of"),
            ))

    employer_operands = [
        _operand(
            operand_id=holding_id, value=_decimal(holding.get("market_value")) or Decimal(0),
            direction="add", reason="active_beneficially_owned_employer_stock",
            source=holding.get("resolved_provenance") or holding.get("provenance") or {},
            version=version, parent_account_id=holding.get("account_id"),
            valuation_as_of=holding.get("valuation_as_of"),
        )
        for holding_id, holding in sorted(eligible_holdings.items())
        if holding.get("concentration_class") == "employer_stock"
    ]
    employer_holding_account_ids = {
        str(holding.get("account_id") or "")
        for holding in eligible_holdings.values()
        if holding.get("concentration_class") == "employer_stock"
    }
    for account_id, account in sorted(accounts.items()):
        subtype = " ".join(
            str(account.get(key) or "").lower().replace("_", " ")
            for key in ("account_type", "account_subtype")
        )
        total = _decimal(account.get("total_balance"))
        if (
            account_id not in employer_holding_account_ids
            and account.get("lifecycle_status") == "active"
            and account.get("ownership_status") in {"beneficially_owned", "settled"}
            and "employer" in subtype
            and any(label in subtype for label in ("equity", "stock"))
            and total is not None
        ):
            employer_operands.append(_operand(
                operand_id=account_id,
                value=total,
                direction="add",
                reason="beneficially_owned_employer_equity_account_total",
                source=account.get("provenance") or {},
                version=version,
                valuation_as_of=account.get("valuation_as_of"),
            ))
    ledger = {
        "source_client_file_version": version,
        "source_provider_revisions": provider_revisions,
        "resolver_policy_version": RESOLVER_POLICY_VERSION,
        "accounts": [_json_safe(item) for item in sorted(accounts.values(), key=lambda item: item["entity_id"])],
        "holdings": [_json_safe(item) for item in sorted(holdings.values(), key=lambda item: item["entity_id"])],
        "net_worth_operands": [_json_safe(item) for item in operands],
        "employer_stock_operands": [_json_safe(item) for item in employer_operands],
        "conflicts": conflicts,
        "missing_inputs": sorted(set(missing)),
    }
    fingerprint_ledger = _without_client_file_versions(ledger)
    encoded = json.dumps(
        fingerprint_ledger,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    fingerprint = f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
    return {
        "schema_version": "financial_position.v1",
        "snapshot_id": f"financial-input:{uuid.uuid5(uuid.NAMESPACE_URL, client_id + ':' + fingerprint)}",
        **ledger,
        "source_input_fingerprint": fingerprint,
        "currency": "USD",
        "assets": [item for item in ledger["net_worth_operands"] if item["direction"] == "add"],
        "liabilities": [item for item in ledger["net_worth_operands"] if item["direction"] == "subtract"],
        "completeness": "confirmed_disclosed_partial" if disclosed_partial else "confirmed_inputs",
    }


def _without_client_file_versions(value: Any) -> Any:
    """Remove provenance sequence numbers from the financial input identity."""

    if isinstance(value, dict):
        return {
            key: _without_client_file_versions(item)
            for key, item in value.items()
            if key not in {"client_file_version", "source_client_file_version"}
        }
    if isinstance(value, list):
        return [_without_client_file_versions(item) for item in value]
    return value


def _canonical_rows(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    typed = client_file.get("typed_facts")
    rows = [dict(item) for item in typed if isinstance(item, dict)] if isinstance(typed, list) else []
    if rows:
        return rows
    facts = normalize_fact_keys(client_file.get("facts") or {})
    return [
        {"entity_id": field, "value": {"entity_type": "scalar_fact", "field": field, "value": value, "currency": "USD"}, "provenance": {"authority": "client_confirmed"}}
        for field, value in facts.items()
        if field != "investment_preferences"
    ]


def _reconcile_provider_values(holdings: Dict[str, Dict[str, Any]], observations: Iterable[Any], conflicts: List[Dict[str, Any]]) -> None:
    by_identity = {str(item.get("provider_identity")): item for item in observations if isinstance(item, dict) and item.get("provider_identity")}
    for holding in holdings.values():
        identity = str(holding.get("provider_identity") or "")
        observed = by_identity.get(identity)
        if not observed:
            continue
        client_value = _decimal(holding.get("market_value"))
        provider_value = _decimal(observed.get("market_value"))
        observed_at = _datetime(observed.get("valuation_as_of") or observed.get("observed_at"))
        fresh = bool(observed_at and (datetime.now(timezone.utc) - observed_at).days <= PROVIDER_FRESHNESS_DAYS)
        if client_value is None or provider_value is None or not fresh:
            continue
        denominator = max(abs(client_value), Decimal(1))
        if abs(provider_value - client_value) / denominator <= PROVIDER_VALUE_TOLERANCE:
            holding["market_value"] = provider_value
            holding["resolved_provenance"] = {"authority": "fresh_provider_value_with_client_confirmed_identity", "provider_revision": observed.get("revision_id") or observed.get("id"), "client": holding.get("provenance") or {}}
        else:
            conflicts.append({"code": "provider_client_value_conflict", "holding_id": holding.get("entity_id"), "client_value": str(client_value), "provider_value": str(provider_value)})


def _operand(*, operand_id: str, value: Decimal, direction: str, reason: str, source: Dict[str, Any], version: int, parent_account_id: Optional[str] = None, valuation_as_of: Any = None, evidence_holding_ids: Optional[List[str]] = None, source_field_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": operand_id, "value": value, "currency": "USD", "direction": direction,
        "inclusion_reason": reason, "parent_account_id": parent_account_id,
        "source": source, "valuation_as_of": valuation_as_of,
        "client_file_version": version,
        **({"evidence_holding_ids": evidence_holding_ids} if evidence_holding_ids else {}),
        **({"source_field_id": source_field_id} if source_field_id else {}),
    }


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
