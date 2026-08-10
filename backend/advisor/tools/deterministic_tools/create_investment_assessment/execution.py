"""Create a pending investment assessment from authoritative Client File state."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from advisor.tools.deterministic_tools.investment_assessment_contract import (
    compute_assessment_content_fingerprint,
    prefer_first_assessment_by_identity,
    validate_assessment_eligibility,
    validate_assessment_content_fingerprint,
    validate_supported_mandate,
)
from advisor.tools.deterministic_tools.run_asset_allocation.execution import (
    ASSET_ALLOCATION_MODEL_ASSET_CLASSES,
    GATED_ASSET_CLASSES,
    resolve_asset_allocation_exclusion,
)
from advisor.tools.subagent_tools.financial_planning_specialist.agent import (
    FinancialPlanningAgentV2,
)


ASSESSMENT_SCHEMA_VERSION = "investment_assessment.v1"
PENDING_STATUSES = {
    "pending_client_signoff",
    "pending_signoff",
    "awaiting_signoff",
    "ready_for_signoff",
}
SIGNED_STATUSES = {"signed_off", "approved", "confirmed"}
NO_ADDITIONAL_POOL_LIQUIDITY_CONSTRAINT = "no_additional_pool_constraint"
SPECIFIC_IN_POOL_LIQUIDITY_CONSTRAINT = "specific_in_pool_constraint"
UNSPECIFIED_POOL_LIQUIDITY_CONSTRAINT = "unspecified"


def prepare_investment_assessment(
    arguments: Dict[str, Any],
    client_file: Dict[str, Any],
    *,
    client_id: str,
    session_id: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a versioned pending assessment without accepting pool facts from the model."""

    if not isinstance(client_file, dict):
        return _failure("client_file_invalid")
    money_pool_id = str(arguments.get("money_pool_id") or "").strip()
    if not money_pool_id:
        return _failure("money_pool_id_required")

    pools, pool_resolution = _resolve_money_pool_candidates(
        client_file,
        money_pool_id,
    )
    if not pools:
        return _failure(
            "money_pool_not_found",
            details={"observed": money_pool_id},
        )
    canonical_pools = {
        json.dumps(pool, sort_keys=True, separators=(",", ":"), default=str)
        for pool in pools
    }
    if len(canonical_pools) != 1:
        return _failure(
            "money_pool_ambiguous",
            details={
                "observed": money_pool_id,
                "resolution": pool_resolution,
            },
        )
    pool = pools[0]
    canonical_money_pool_id = str(
        pool.get("id") or pool.get("money_pool_id") or ""
    ).strip()
    if not canonical_money_pool_id:
        return _failure("money_pool_identity_missing")
    money_pool_id = canonical_money_pool_id

    pool_values, missing_pool_fields = _authoritative_pool_values(pool)
    if missing_pool_fields:
        return _failure(
            "money_pool_incomplete",
            missing_data=missing_pool_fields,
        )

    target_volatility = _finite_number(arguments.get("target_volatility_annual_decimal"))
    tolerance_bps = _finite_number(arguments.get("target_volatility_tolerance_bps"))
    active_risk = _finite_number(arguments.get("active_risk_percentage"))
    liquidity_requirement = str(arguments.get("liquidity_requirement") or "").strip()
    complexity_preference = str(arguments.get("complexity_preference") or "").strip()
    if target_volatility is None or not 0.05 <= target_volatility <= 0.20:
        return _failure("target_volatility_annual_decimal_invalid")
    if active_risk is None or not 0.0 <= active_risk <= 1.0:
        return _failure("active_risk_percentage_invalid")
    mandate_supported, mandate_error, mandate_details = validate_supported_mandate(
        tolerance_bps=tolerance_bps,
        liquidity_requirement=liquidity_requirement,
        complexity_preference=complexity_preference,
    )
    if not mandate_supported:
        return _failure(mandate_error or "assessment_mandate_unsupported", details=mandate_details)
    client_preferences = pool_values.get("client_preferences") or {}
    pool_liquidity_need = str(client_preferences.get("liquidity_needs") or "").strip()
    pool_liquidity_mode = _pool_liquidity_constraint_mode(client_preferences)
    if (
        pool_liquidity_mode == SPECIFIC_IN_POOL_LIQUIDITY_CONSTRAINT
        and liquidity_requirement
        == "no_additional_portfolio_liquidity_constraint"
    ):
        return _failure(
            "money_pool_liquidity_constraint_unsupported",
            details={
                "money_pool_id": money_pool_id,
                "pool_liquidity_needs": pool_liquidity_need,
                "pool_liquidity_constraint_mode": pool_liquidity_mode,
                "observed_liquidity_requirement": liquidity_requirement,
                "reason": (
                    "The current asset-allocation adapter cannot enforce a "
                    "client-specific in-pool liquidity amount."
                ),
            },
        )

    exclusions, exclusion_error, exclusion_details = _canonical_asset_class_list(
        arguments.get("exclusions"),
        field="exclusions",
    )
    if exclusion_error:
        return _failure(exclusion_error, details=exclusion_details or None)
    authorized, authorization_error, authorization_details = (
        _canonical_asset_class_list(
            arguments.get("authorized_specialized_asset_classes"),
            field="authorized_specialized_asset_classes",
        )
    )
    if authorization_error:
        return _failure(authorization_error, details=authorization_details or None)
    not_specialized = sorted(set(authorized) - GATED_ASSET_CLASSES)
    if not_specialized:
        return _failure(
            "authorized_specialized_asset_classes_invalid",
            details={"not_specialized": not_specialized},
        )
    contradictory = sorted(set(exclusions) & set(authorized))
    if contradictory:
        return _failure(
            "assessment_constraints_contradictory",
            details={"excluded_and_authorized": contradictory},
        )

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    valid_until_raw = arguments.get("valid_until")
    valid_until = _parse_iso_datetime(valid_until_raw)
    if valid_until is None:
        return _failure("valid_until_invalid")
    validity_source = "explicit_sdk_tool_input"
    if valid_until <= current_time:
        # Agents often pass "today" or a same-day boundary by mistake. Prefer a
        # standard 30-day review window over interrogating the client about dates.
        valid_until = current_time + timedelta(days=30)
        validity_source = "server_default_30_day_window"

    assessment_id = _stable_assessment_id(client_id, money_pool_id)
    existing = [
        assessment
        for assessment in _assessment_candidates(client_file)
        if _assessment_id(assessment) == assessment_id
        and _assessment_money_pool_id(assessment) == money_pool_id
    ]
    next_version = max((_assessment_version(item) for item in existing), default=0) + 1
    mandate_basis = {
        **pool_values,
        "target_volatility_annual_decimal": target_volatility,
        "target_volatility_tolerance_bps": tolerance_bps,
        "active_risk_percentage": active_risk,
        "liquidity_requirement": liquidity_requirement,
        "complexity_preference": complexity_preference,
        "exclusions": exclusions,
        "authorized_specialized_asset_classes": authorized,
    }
    canonical_mandate = _canonical_mandate(
        money_pool_id=money_pool_id,
        basis=mandate_basis,
        valid_until=valid_until,
    )
    matching_pending = [
        item
        for item in existing
        if _assessment_status(item) in PENDING_STATUSES
        and item.get("requires_revalidation") is not True
        and item.get("stale") is not True
        and _canonical_existing_mandate(item) == canonical_mandate
    ]
    if matching_pending:
        for item in matching_pending:
            fingerprint_valid, fingerprint_error, fingerprint_details = (
                validate_assessment_content_fingerprint(item)
            )
            if not fingerprint_valid:
                return _failure(
                    fingerprint_error or "assessment_content_fingerprint_invalid",
                    details=fingerprint_details,
                )
        canonical_matches = {
            _canonical_assessment_json(item)
            for item in matching_pending
        }
        if len(canonical_matches) != 1:
            return _failure("pending_assessment_ambiguous")
        return {
            "ok": True,
            "payload": copy.deepcopy(
                next(
                    (
                        item
                        for item in matching_pending
                        if item.get("durable_artifact_id")
                    ),
                    matching_pending[0],
                )
            ),
            "idempotent_replay": True,
        }

    request = {
        "assessment_id": assessment_id,
        "assessment_version": next_version,
        **mandate_basis,
        "target_volatility_pct": target_volatility,
        "money_pool_id": money_pool_id,
        "pool_id": money_pool_id,
    }
    artifact = FinancialPlanningAgentV2().assess_investment_request(
        client_id=client_id,
        session_id=session_id,
        request=request,
        client_file=client_file,
    )
    payload = copy.deepcopy(artifact.payload)
    assessment = payload.get("assessment")
    if not isinstance(assessment, dict):
        return _failure("assessment_generation_failed")
    assessment["schema_version"] = ASSESSMENT_SCHEMA_VERSION
    assessment["basis"] = copy.deepcopy(mandate_basis)
    assessment["client_summary"] = _client_summary(
        basis=mandate_basis,
        valid_until=valid_until,
    )
    eligible, eligibility_error, eligibility_details = validate_assessment_eligibility(
        assessment
    )
    if not eligible:
        return _failure(
            eligibility_error or "assessment_not_eligible",
            details=eligibility_details,
        )
    consultation_basis = (
        payload.get("consultation_basis")
        if isinstance(payload.get("consultation_basis"), dict)
        else {}
    )
    investment_consultation_id = str(
        consultation_basis.get("investment_consultation_id")
        or consultation_basis.get("consultation_id")
        or ""
    ).strip()
    assessment["assessment_id"] = assessment_id
    assessment["assessment_version"] = next_version
    assessment["money_pool_id"] = money_pool_id
    if investment_consultation_id:
        assessment["investment_consultation_id"] = investment_consultation_id
    payload.update(
        {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "artifact_type": "investment_assessment",
            "assessment_id": assessment_id,
            "assessment_version": next_version,
            "money_pool_id": money_pool_id,
            "investment_consultation_id": investment_consultation_id,
            "status": "ready",
            "assessment_status": "pending_client_signoff",
            "signed_off": False,
            "assessed_at": current_time.isoformat(),
            "valid_until": valid_until.isoformat(),
            "requires_revalidation": False,
            "assessment": assessment,
            "freshness": {
                "assessed_at": current_time.isoformat(),
                "valid_until": valid_until.isoformat(),
                "validity_source": validity_source,
            },
        }
    )
    payload["content_fingerprint"] = compute_assessment_content_fingerprint(payload)
    return {"ok": True, "payload": payload, "idempotent_replay": False}


def _authoritative_pool_values(pool: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    amount = _finite_number(pool.get("amount"))
    purpose = str(pool.get("purpose_type") or pool.get("purpose") or pool.get("objective") or "").strip()
    funding_source = str(pool.get("funding_source") or "").strip()
    risk = str(pool.get("risk_tolerance") or pool.get("risk") or "").strip()
    horizon = (
        pool.get("horizon_years")
        if pool.get("horizon_years") not in (None, "")
        else pool.get("horizon_date") or pool.get("horizon_text")
    )
    missing: List[str] = []
    if amount is None or amount <= 0:
        missing.append("amount")
    if not purpose:
        missing.append("purpose")
    if not funding_source:
        missing.append("funding_source")
    if not risk:
        missing.append("risk_tolerance")
    if horizon in (None, ""):
        missing.append("horizon")
    result: Dict[str, Any] = {
        "money_pool_id": str(pool.get("id") or pool.get("money_pool_id") or "").strip(),
        "pool_label": pool.get("label"),
        "amount": amount,
        "funding_source": funding_source or None,
        "purpose": purpose or None,
        "risk": risk or None,
        "target_risk": risk or None,
    }
    constraints = pool.get("constraints")
    if not isinstance(constraints, dict):
        constraints = {}
    client_preferences = {
        key: copy.deepcopy(value)
        for key in (
            "liquidity_needs",
            "liquidity_constraint_mode",
            "complexity_preference",
            "asset_class_preferences",
            "exclusions",
            "special_considerations",
            "tax_considerations",
        )
        if (value := pool.get(key, constraints.get(key))) not in (None, "", [])
    }
    if client_preferences:
        result["client_preferences"] = client_preferences
    if pool.get("horizon_years") not in (None, ""):
        result["horizon_years"] = pool.get("horizon_years")
    elif pool.get("horizon_date") not in (None, ""):
        result["horizon_date"] = pool.get("horizon_date")
    elif pool.get("horizon_text") not in (None, ""):
        result["horizon_text"] = pool.get("horizon_text")
    return result, missing


def _money_pool_candidates(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    value = client_file.get("money_pools")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _resolve_money_pool_candidates(
    client_file: Dict[str, Any],
    observed_identity: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """Resolve a pool by canonical id, or by one unique exact label.

    Agent tool calls sometimes carry the label returned in the conversation
    instead of the UUID returned by ``upsert_money_pool``. Accepting a unique
    exact label is deterministic and safe; fuzzy or ambiguous matches remain
    fail-closed.
    """

    candidates = _money_pool_candidates(client_file)
    by_id = [
        pool
        for pool in candidates
        if str(pool.get("id") or pool.get("money_pool_id") or "").strip()
        == observed_identity
    ]
    if by_id:
        return by_id, "canonical_id"
    normalized_identity = " ".join(observed_identity.casefold().split())
    by_label = [
        pool
        for pool in candidates
        if " ".join(str(pool.get("label") or "").casefold().split())
        == normalized_identity
    ]
    return by_label, "unique_exact_label"


def _pool_liquidity_constraint_mode(preferences: Dict[str, Any]) -> str:
    """Resolve a stable implementation mode instead of matching whole sentences."""

    explicit = str(preferences.get("liquidity_constraint_mode") or "").strip().lower()
    if explicit in {
        NO_ADDITIONAL_POOL_LIQUIDITY_CONSTRAINT,
        SPECIFIC_IN_POOL_LIQUIDITY_CONSTRAINT,
        UNSPECIFIED_POOL_LIQUIDITY_CONSTRAINT,
    }:
        return explicit

    # Backward compatibility for pools saved before the structured mode existed.
    # This classifies the business meaning, not intent routing: only an explicit
    # minimum/retained amount inside the pool is an unsupported optimizer constraint.
    text = " ".join(
        str(preferences.get("liquidity_needs") or "")
        .casefold()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )
    if not text:
        return UNSPECIFIED_POOL_LIQUIDITY_CONSTRAINT

    no_constraint_markers = (
        ("no " in f"{text} " or "none " in f"{text} " or "zero " in f"{text} ")
        and any(term in text for term in ("buffer", "liquidity", "cash"))
        and any(term in text for term in ("required", "needed", "constraint"))
    )
    external_reserve = (
        any(term in text for term in ("emergency", "reserve"))
        and any(term in text for term in ("separate", "outside", "untouched"))
    )
    if no_constraint_markers or external_reserve:
        return NO_ADDITIONAL_POOL_LIQUIDITY_CONSTRAINT

    liquidity_terms = ("liquid", "liquidity", "cash", "buffer", "reserve")
    explicit_amount = any(
        term in text
        for term in ("at least", "minimum", "$", "%", "amount", "dollar")
    )
    explicit_in_pool_scope = any(
        term in text
        for term in (
            "inside this pool",
            "within this pool",
            "in this pool",
            "of this pool",
        )
    )
    explicit_cash_reserve = any(
        term in text
        for term in (
            "cash buffer",
            "cash reserve",
            "liquidity buffer",
            "emergency reserve",
        )
    )
    in_pool_requirement = any(term in text for term in liquidity_terms) and (
        explicit_amount or explicit_in_pool_scope or explicit_cash_reserve
    )
    if in_pool_requirement:
        return SPECIFIC_IN_POOL_LIQUIDITY_CONSTRAINT
    return NO_ADDITIONAL_POOL_LIQUIDITY_CONSTRAINT


def _assessment_candidates(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key in (
        "investment_assessments",
        "signed_investment_assessments",
        "assessments",
        "financial_planning_assessments",
    ):
        rows.extend(_dict_rows(client_file.get(key)))
    artifacts = client_file.get("artifacts")
    if isinstance(artifacts, dict):
        rows.extend(_dict_rows(artifacts.get("plans")))
        rows.extend(_dict_rows(artifacts.get("assessments")))
    recent = client_file.get("recent_writebacks")
    if isinstance(recent, list):
        for writeback in recent:
            if not isinstance(writeback, dict) or writeback.get("operation") not in {
                "create_investment_assessment",
                "record_assessment_signoff",
            }:
                continue
            values = writeback.get("values")
            if isinstance(values, dict):
                rows.append(values)
    result: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if isinstance(payload, dict) and payload.get("assessment_id") and isinstance(payload.get("assessment"), dict):
            result.append(payload)
    return prefer_first_assessment_by_identity(result)


def _dict_rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if value.get("assessment_id"):
            return [value]
        items = value.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _canonical_asset_class_list(
    value: Any, *, field: str
) -> Tuple[List[str], Optional[str], Dict[str, Any]]:
    if not isinstance(value, list):
        return [], f"{field}_required", {}
    normalized: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [], f"{field}_invalid", {"observed": item}
        resolved = resolve_asset_allocation_exclusion(item)
        if resolved is None:
            return (
                [],
                f"{field}_unsupported_or_ambiguous",
                {
                    "observed": item.strip(),
                    "supported_asset_classes": list(
                        ASSET_ALLOCATION_MODEL_ASSET_CLASSES
                    ),
                    "hint": (
                        "Use [] when the client only asked for plain vanilla / "
                        "no options / no leverage; only pass resolvable NEO "
                        "asset-class names or aliases."
                    ),
                },
            )
        if resolved in normalized:
            return [], f"{field}_duplicate", {"observed": resolved}
        normalized.append(resolved)
    return normalized, None, {}


def _stable_assessment_id(client_id: str, money_pool_id: str) -> str:
    digest = hashlib.sha256(
        f"{client_id}:{money_pool_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"assessment-{digest}"


def _canonical_mandate(
    *, money_pool_id: str, basis: Dict[str, Any], valid_until: datetime
) -> str:
    return json.dumps(
        {
            "money_pool_id": money_pool_id,
            "basis": basis,
            "valid_until": valid_until.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _canonical_existing_mandate(assessment: Dict[str, Any]) -> str:
    content = assessment.get("assessment")
    basis = content.get("basis") if isinstance(content, dict) else {}
    valid_until = _parse_iso_datetime(assessment.get("valid_until"))
    return _canonical_mandate(
        money_pool_id=_assessment_money_pool_id(assessment),
        basis=basis if isinstance(basis, dict) else {},
        valid_until=valid_until or datetime.min.replace(tzinfo=timezone.utc),
    )


def _canonical_assessment_json(value: Dict[str, Any]) -> str:
    normalized = copy.deepcopy(value)
    normalized.pop("durable_artifact_id", None)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _client_summary(*, basis: Dict[str, Any], valid_until: datetime) -> Dict[str, Any]:
    horizon = (
        basis.get("horizon_years")
        or basis.get("horizon_date")
        or basis.get("horizon_text")
    )
    horizon_text = (
        f"{int(horizon)} years"
        if isinstance(horizon, (int, float)) and float(horizon).is_integer()
        else str(horizon)
    )
    exclusions = [
        str(item).strip()
        for item in (basis.get("exclusions") or [])
        if str(item).strip()
    ]
    client_preferences = (
        basis.get("client_preferences")
        if isinstance(basis.get("client_preferences"), dict)
        else {}
    )
    preference_parts: List[str] = []
    for key in ("complexity_preference", "liquidity_needs"):
        value = str(client_preferences.get(key) or "").strip()
        if value:
            if key == "complexity_preference" and value == "optimizer_unrestricted":
                value = "a broad, diversified opportunity set"
            preference_parts.append(value.rstrip(" .;"))
    asset_preferences = [
        str(item).strip()
        for item in (client_preferences.get("asset_class_preferences") or [])
        if str(item).strip()
    ]
    if asset_preferences:
        preference_parts.append(", ".join(asset_preferences))
    implementation_text = (
        " We'll keep the implementation aligned with your confirmed preferences: "
        + "; ".join(preference_parts)
        + "."
        if preference_parts
        else ""
    )
    exclusion_text = (
        f" We’ll keep {', '.join(exclusions)} out."
        if exclusions
        else ""
    )
    pool_label = " ".join(str(basis.get("pool_label") or "").split()).strip()
    pool_description = (
        pool_label
        if pool_label
        else "investment pool"
    )
    first = (
        f"Please confirm the {pool_description}: about "
        f"${float(basis['amount']):,.0f} from {basis['funding_source']}, "
        f"over roughly {horizon_text}, at a {basis['risk']} risk level."
    )
    second = (
        f"We’ll aim for about "
        f"{float(basis['target_volatility_annual_decimal']) * 100:g}% yearly "
        f"market swings.{implementation_text}{exclusion_text} "
        f"This summary stays available to review until "
        f"{valid_until.date().isoformat()}."
    )
    return {
        "title": "Investment Consultation Summary",
        "subtitle": "For your review",
        "paragraphs": [first, second],
    }


def _assessment_id(value: Dict[str, Any]) -> str:
    return str(value.get("assessment_id") or value.get("id") or "").strip()


def _assessment_version(value: Dict[str, Any]) -> int:
    version = value.get("assessment_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    if isinstance(version, str) and version.isdigit():
        return int(version)
    return 0


def _assessment_money_pool_id(value: Dict[str, Any]) -> str:
    content = value.get("assessment")
    basis = content.get("basis") if isinstance(content, dict) else {}
    return str(
        value.get("money_pool_id")
        or (basis.get("money_pool_id") if isinstance(basis, dict) else None)
        or ""
    ).strip()


def _assessment_status(value: Dict[str, Any]) -> str:
    status = str(value.get("assessment_status") or value.get("status") or "").strip().lower()
    if value.get("signed_off") is True and status not in SIGNED_STATUSES:
        return "signed_off"
    return status


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _failure(
    error: str,
    *,
    missing_data: Optional[List[str]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": error}
    if missing_data:
        result["missing_data"] = missing_data
    if details:
        result["details"] = details
    return result
