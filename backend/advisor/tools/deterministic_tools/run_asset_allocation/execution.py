"""Execution for the run_asset_allocation deterministic tool."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from contracts.tools import build_asset_allocation_tool_args
from advisor.tools.deterministic_tools.common.cache import compute_tool_cache_key
from advisor.tools.deterministic_tools.execution_control import (
    commit_read_only_tool_state,
    effective_read_only_request_timeout,
    read_only_tool_execution_cancelled,
)
from advisor.tools.deterministic_tools.investment_assessment_contract import (
    prefer_first_assessment_by_identity,
    validate_assessment_content_fingerprint,
    validate_assessment_eligibility,
    validate_supported_mandate,
)


LogDebug = Callable[[str], None]


ASSET_ALLOCATION_RESULT_CONTRACT_VERSION = "asset_allocation_result.v2"
ASSET_ALLOCATION_ENGINE_RESPONSE_CONTRACT_VERSION = (
    "asset_allocation_engine_response.v1"
)
SIGNED_ASSESSMENT_STATUSES = {"signed_off", "approved", "confirmed"}
WEIGHT_TOLERANCE = 1e-6
ACTIVE_RISK_TOLERANCE = 1e-6


ASSET_ALLOCATION_MODEL_ASSET_CLASSES = [
    "Cash",
    "US Treasury",
    "Global Investment Grade Corporate Bond",
    "Global High Yield Bond BB-B",
    "Emerging Market Local Currency Government Bonds",
    "Emerging Market Hard Currency Debt",
    "US Equity",
    "Dev. Europe ex UK Equity",
    "Japan Equity",
    "China Equity",
    "India Equity",
    "Commodities",
    "Gold",
    "Hedge Funds",
    "Bitcoin",
]
ASSET_ALLOCATION_MODEL_ASSET_LOOKUP: Dict[str, str] = {
    name.lower(): name for name in ASSET_ALLOCATION_MODEL_ASSET_CLASSES
}
ASSET_ALLOCATION_MODEL_ALIAS_MAP: Dict[str, str] = {
    "btc": "Bitcoin",
    "crypto": "Bitcoin",
    "cryptocurrency": "Bitcoin",
    "cryptocurrencies": "Bitcoin",
    "bitcoin/crypto": "Bitcoin",
    "hedge fund": "Hedge Funds",
    "hedge funds": "Hedge Funds",
    "hedgefunds": "Hedge Funds",
    "us stocks": "US Equity",
    "us equities": "US Equity",
    "american equity": "US Equity",
    "american stocks": "US Equity",
    "treasuries": "US Treasury",
    "us treasuries": "US Treasury",
    "government bonds": "US Treasury",
    "treasury": "US Treasury",
    "corporate bonds": "Global Investment Grade Corporate Bond",
    "investment grade": "Global Investment Grade Corporate Bond",
    "ig bonds": "Global Investment Grade Corporate Bond",
    "high yield": "Global High Yield Bond BB-B",
    "high yield bonds": "Global High Yield Bond BB-B",
    "junk bonds": "Global High Yield Bond BB-B",
    "hy bonds": "Global High Yield Bond BB-B",
    "em local bonds": "Emerging Market Local Currency Government Bonds",
    "em local currency": "Emerging Market Local Currency Government Bonds",
    "em hard currency": "Emerging Market Hard Currency Debt",
    "em debt": "Emerging Market Hard Currency Debt",
    "european equity": "Dev. Europe ex UK Equity",
    "europe equity": "Dev. Europe ex UK Equity",
    "european stocks": "Dev. Europe ex UK Equity",
    "japan stocks": "Japan Equity",
    "japanese equity": "Japan Equity",
    "china stocks": "China Equity",
    "chinese equity": "China Equity",
    "india stocks": "India Equity",
    "indian equity": "India Equity",
}
GATED_ASSET_CLASSES = {
    "Bitcoin",
    "Commodities",
    "Gold",
    "Hedge Funds",
    "Emerging Market Local Currency Government Bonds",
    "Emerging Market Hard Currency Debt",
    "China Equity",
    "India Equity",
}
def build_asset_allocation_headers(config: Any) -> Dict[str, str]:
    """Build headers for asset allocation model API requests."""
    headers = {"Content-Type": "application/json"}
    api_key = (
        getattr(config, "asset_allocation_model_api_key", None)
        or getattr(config, "asset_allocation_api_key", None)
        or ""
    )
    if api_key:
        headers["X-Api-Key"] = api_key
    return headers


def _asset_allocation_model_url(config: Any) -> str:
    return str(
        getattr(config, "asset_allocation_model_url", None)
        or getattr(config, "asset_allocation_api_url", None)
        or ""
    ).rstrip("/")


def _asset_allocation_optimize_path(config: Any) -> str:
    path = str(
        getattr(config, "asset_allocation_model_optimize_path", None)
        or getattr(config, "asset_allocation_optimize_path", None)
        or "/asset-allocation/api/v1/optimize"
    )
    return path if path.startswith("/") else f"/{path}"


def resolve_asset_allocation_exclusion(raw_name: str) -> Optional[str]:
    """Resolve an exclusion only through an exact canonical name or explicit alias."""
    cleaned = raw_name.strip()
    if not cleaned:
        return None
    exact = ASSET_ALLOCATION_MODEL_ASSET_LOOKUP.get(cleaned.lower())
    if exact:
        return exact
    alias = ASSET_ALLOCATION_MODEL_ALIAS_MAP.get(cleaned.lower())
    if alias:
        return alias
    return None


def resolve_authoritative_asset_allocation_arguments(
    args: Dict[str, Any],
    client_file: Dict[str, Any],
    *,
    client_id: str,
    now: Optional[datetime] = None,
    technical_max_age_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Resolve a signed assessment reference into immutable engine inputs.

    The model supplies identity only. Amount, risk, active implementation,
    exclusions, and all other mandate facts come from the durable Client File.
    """

    assessment_ref = args.get("assessment_ref")
    if not isinstance(assessment_ref, dict):
        return _authorization_failure("assessment_ref_required")
    assessment_id = str(assessment_ref.get("assessment_id") or "").strip()
    money_pool_id = str(assessment_ref.get("money_pool_id") or "").strip()
    version_raw = assessment_ref.get("assessment_version")
    if (
        not assessment_id
        or not money_pool_id
        or not isinstance(version_raw, int)
        or isinstance(version_raw, bool)
        or version_raw < 1
    ):
        return _authorization_failure("assessment_ref_invalid")

    matches: List[Dict[str, Any]] = []
    for candidate in _assessment_candidates(client_file):
        if _assessment_id(candidate) != assessment_id:
            continue
        if _assessment_version(candidate) != version_raw:
            continue
        if _assessment_money_pool_id(candidate) != money_pool_id:
            continue
        matches.append(candidate)
    if not matches:
        return _authorization_failure("signed_assessment_not_found")

    signed_matches = [
        item
        for item in matches
        if str(
            item.get("assessment_status") or item.get("status") or ""
        ).strip().lower() in SIGNED_ASSESSMENT_STATUSES
        or item.get("signed_off") is True
    ]
    if not signed_matches:
        return _authorization_failure("assessment_not_signed")

    # Prefer immutable content fingerprints when present. Candidate rows often
    # include the durable assessment plus recent writeback projections that differ
    # only in envelope metadata (occurred_at / source ids / pool_label).
    fingerprints = {
        str(item.get("content_fingerprint") or "").strip()
        for item in signed_matches
        if str(item.get("content_fingerprint") or "").strip()
    }
    if fingerprints:
        if len(fingerprints) != 1:
            return _authorization_failure("signed_assessment_ambiguous")
    else:
        canonical_rows = {
            _canonical_assessment_json(item)
            for item in signed_matches
        }
        if len(canonical_rows) != 1:
            return _authorization_failure("signed_assessment_ambiguous")
    assessment = next(
        (item for item in signed_matches if item.get("durable_artifact_id")),
        signed_matches[0],
    )

    status = str(
        assessment.get("assessment_status") or assessment.get("status") or ""
    ).strip().lower()
    if status not in SIGNED_ASSESSMENT_STATUSES and assessment.get("signed_off") is not True:
        return _authorization_failure("assessment_not_signed")
    signed_off_at_raw = assessment.get("signed_off_at")
    signed_off_at = _parse_iso_datetime(signed_off_at_raw)
    if signed_off_at is None:
        return _authorization_failure("signed_off_at_required")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if signed_off_at > current_time + timedelta(minutes=5):
        return _authorization_failure("signed_off_at_in_future")
    if (
        technical_max_age_days is not None
        and technical_max_age_days > 0
        and current_time - signed_off_at > timedelta(days=technical_max_age_days)
    ):
        return _authorization_failure("signed_assessment_stale")
    if assessment.get("requires_revalidation") is True or assessment.get("stale") is True:
        return _authorization_failure("signed_assessment_stale")
    valid_until_raw = assessment.get("valid_until") or assessment.get("expires_at")
    if valid_until_raw in (None, ""):
        return _authorization_failure("signed_assessment_valid_until_required")
    valid_until = _parse_iso_datetime(valid_until_raw)
    if valid_until_raw not in (None, "") and valid_until is None:
        return _authorization_failure("signed_assessment_valid_until_invalid")
    if valid_until is not None and valid_until < current_time:
        return _authorization_failure("signed_assessment_stale")
    stale_reason = _assessment_stale_reason(
        client_file,
        assessment_id=assessment_id,
        money_pool_id=money_pool_id,
        signed_off_at=signed_off_at,
    )
    if stale_reason is not None:
        return _authorization_failure(
            "signed_assessment_stale",
            details={"reason": stale_reason},
        )

    fingerprint_valid, fingerprint_error, fingerprint_details = (
        validate_assessment_content_fingerprint(assessment)
    )
    if not fingerprint_valid:
        return _authorization_failure(
            fingerprint_error or "assessment_content_fingerprint_invalid",
            details=fingerprint_details,
        )

    assessment_content = assessment.get("assessment")
    eligible, eligibility_error, eligibility_details = validate_assessment_eligibility(
        assessment_content
    )
    if not eligible:
        return _authorization_failure(
            eligibility_error or "assessment_not_eligible",
            details=eligibility_details,
        )

    basis = _assessment_basis(assessment)
    missing: List[str] = []
    amount = _positive_number(
        basis.get("amount")
        if basis.get("amount") is not None
        else basis.get("investment_amount")
    )
    if amount is None:
        missing.append("amount")
    purpose = str(basis.get("purpose") or basis.get("purpose_type") or "").strip()
    if not purpose:
        missing.append("purpose")
    funding_source = str(basis.get("funding_source") or "").strip()
    if not funding_source:
        missing.append("funding_source")
    liquidity_requirement = str(
        basis.get("liquidity_requirement")
        or basis.get("liquidity_need")
        or basis.get("liquidity_preference")
        or ""
    ).strip()
    if not liquidity_requirement:
        missing.append("liquidity_requirement")
    complexity_preference = str(basis.get("complexity_preference") or "").strip()
    if not complexity_preference:
        missing.append("complexity_preference")

    exclusions_raw = (
        basis.get("exclusions")
        if basis.get("exclusions") is not None
        else basis.get("excluded_asset_classes")
    )
    if not isinstance(exclusions_raw, list):
        missing.append("exclusions")
        exclusions_raw = []

    target_volatility = _normalize_decimal_percentage(
        basis.get("target_volatility_annual_decimal")
        if basis.get("target_volatility_annual_decimal") is not None
        else (
            basis.get("target_volatility_pct")
            if basis.get("target_volatility_pct") is not None
            else basis.get("target_volatility")
        )
    )
    risk_name = str(
        basis.get("target_risk")
        or basis.get("recommended_risk")
        or basis.get("risk")
        or basis.get("risk_tolerance")
        or ""
    ).strip().lower()
    if target_volatility is None:
        missing.append("target_volatility")
    target_volatility_tolerance_bps = _finite_number(
        basis.get("target_volatility_tolerance_bps")
    )
    if target_volatility_tolerance_bps is None:
        missing.append("target_volatility_tolerance_bps")

    active_risk_percentage = _normalize_decimal_percentage(
        basis.get("active_risk_percentage")
        if basis.get("active_risk_percentage") is not None
        else basis.get("active_risk_pct")
    )
    if active_risk_percentage is None:
        missing.append("active_risk_percentage")
    authorized_specialized_raw = basis.get("authorized_specialized_asset_classes")
    if authorized_specialized_raw is None:
        missing.append("authorized_specialized_asset_classes")
    if missing:
        return _authorization_failure(
            "signed_assessment_incomplete",
            missing_data=sorted(set(missing)),
        )

    mandate_supported, mandate_error, mandate_details = validate_supported_mandate(
        tolerance_bps=target_volatility_tolerance_bps,
        liquidity_requirement=liquidity_requirement,
        complexity_preference=complexity_preference,
    )
    if not mandate_supported:
        return _authorization_failure(
            mandate_error or "assessment_mandate_unsupported",
            details=mandate_details,
        )

    normalized_exclusions, exclusion_error = _normalize_exclusions(exclusions_raw)
    if exclusion_error:
        return _authorization_failure(
            exclusion_error["error"],
            details=exclusion_error,
        )
    if not isinstance(authorized_specialized_raw, list):
        return _authorization_failure("authorized_specialized_asset_classes_invalid")
    authorized_specialized, authorization_error = _normalize_exclusions(
        authorized_specialized_raw
    )
    if authorization_error:
        return _authorization_failure(
            "authorized_specialized_asset_classes_invalid",
            details=authorization_error,
        )
    unauthorized_values = sorted(set(authorized_specialized) - GATED_ASSET_CLASSES)
    if unauthorized_values:
        return _authorization_failure(
            "authorized_specialized_asset_classes_invalid",
            details={"not_specialized": unauthorized_values},
        )
    authorized_specialized_set = set(authorized_specialized)
    for asset_class in sorted(GATED_ASSET_CLASSES - authorized_specialized_set):
        if asset_class not in normalized_exclusions:
            normalized_exclusions.append(asset_class)

    if not (0.05 <= float(target_volatility) <= 0.20):
        return _authorization_failure("target_volatility_out_of_range")
    if not (0.0 <= float(active_risk_percentage) <= 1.0):
        return _authorization_failure("active_risk_percentage_out_of_range")

    normalized_ref = {
        "assessment_id": assessment_id,
        "assessment_version": version_raw,
        "money_pool_id": money_pool_id,
        "signed_off_at": signed_off_at.isoformat(),
    }
    assessment_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "client_id": client_id,
                "assessment_ref": normalized_ref,
                "assessment": assessment,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "success": True,
        "assessment_ref": normalized_ref,
        "assessment_fingerprint": assessment_fingerprint,
        "arguments": {
            "target_volatility": float(target_volatility),
            "active_risk_percentage": float(active_risk_percentage),
            "total_investment": float(amount),
            "excluded_asset_classes": normalized_exclusions,
            "target_volatility_tolerance_bps": target_volatility_tolerance_bps,
        },
        "mandate": {
            "purpose": purpose,
            "funding_source": funding_source,
            "liquidity_requirement": liquidity_requirement,
            "complexity_preference": complexity_preference,
            "risk": risk_name or None,
        },
        "assessment_eligibility": {
            "passed": True,
            **eligibility_details,
        },
        "assessment_integrity": {
            "passed": True,
            **fingerprint_details,
        },
        "mandate_support": {
            "passed": True,
            **mandate_details,
        },
    }


def _authorization_failure(
    error: str,
    *,
    missing_data: Optional[List[str]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "error": error,
        "status": {
            "execution": "blocked",
            "valid_for_recommendation": False,
            "contract_version": ASSET_ALLOCATION_RESULT_CONTRACT_VERSION,
        },
    }
    if missing_data:
        result["missing_data"] = missing_data
    if details:
        result["details"] = details
    return result


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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _normalize_decimal_percentage(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if number > 1.0:
        number /= 100.0
    return number


def _normalize_exclusions(
    exclusions: List[Any],
) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    normalized: List[str] = []
    unresolved: List[str] = []
    seen = set()
    for raw_value in exclusions:
        if not isinstance(raw_value, str):
            return [], {
                "error": "invalid_exclusion",
                "message": "Every exclusion must be an asset-class string.",
            }
        resolved = resolve_asset_allocation_exclusion(raw_value)
        if resolved is None:
            unresolved.append(raw_value.strip())
            continue
        if resolved not in seen:
            seen.add(resolved)
            normalized.append(resolved)
    if unresolved:
        return [], {
            "error": "unsupported_or_ambiguous_exclusion",
            "unresolved": unresolved,
            "supported_values": list(ASSET_ALLOCATION_MODEL_ASSET_CLASSES),
        }
    return normalized, None


def _assessment_stale_reason(
    client_file: Dict[str, Any],
    *,
    assessment_id: str,
    money_pool_id: str,
    signed_off_at: datetime,
) -> Optional[str]:
    """Return the durable lifecycle signal that invalidates an assessment.

    A signed assessment cannot authorize a new optimization after Client File
    facts have marked investment assessments for review, or after its money
    pool has changed.  The check is deliberately conservative: legacy stale
    impacts may not name one assessment, so an unresolved assessment-class
    impact invalidates every signed assessment until revalidation creates a
    new version.
    """

    stale_impacts = client_file.get("stale_impacts")
    if isinstance(stale_impacts, list):
        for impact in stale_impacts:
            if not isinstance(impact, dict):
                continue
            status = str(impact.get("status") or "").strip().lower()
            if status not in {
                "needs_review",
                "stale",
                "invalid",
                "requires_revalidation",
            }:
                continue
            record = str(
                impact.get("record")
                or impact.get("target_record")
                or impact.get("record_type")
                or ""
            ).strip().lower()
            if record not in {
                "assessment",
                "financial_plan",
                "investment_assessment",
                "investment_assessments",
                "financial_planning_assessment",
            }:
                continue
            target_id = str(
                impact.get("assessment_id")
                or impact.get("target_id")
                or impact.get("related_id")
                or ""
            ).strip()
            if target_id and target_id != assessment_id:
                continue
            impact_at = _parse_iso_datetime(
                impact.get("source_writeback_at")
                or impact.get("occurred_at")
                or impact.get("updated_at")
            )
            # Generic assessment-class impacts that predate sign-off were already
            # available when this version was signed. Only block when the impact
            # explicitly targets this assessment, or when facts changed after sign-off.
            if (
                not target_id
                and impact_at is not None
                and impact_at <= signed_off_at
            ):
                continue
            return "unresolved_assessment_stale_impact"

    money_pools = client_file.get("money_pools")
    pool_rows = money_pools if isinstance(money_pools, list) else []
    for pool in pool_rows:
        if not isinstance(pool, dict):
            continue
        pool_id = str(pool.get("id") or pool.get("money_pool_id") or "").strip()
        if pool_id != money_pool_id:
            continue
        updated_at_raw = pool.get("updated_at") or pool.get("modified_at")
        updated_at = _parse_iso_datetime(updated_at_raw)
        if updated_at_raw not in (None, "") and updated_at is None:
            return "money_pool_updated_at_invalid"
        if updated_at is not None and updated_at > signed_off_at:
            return "money_pool_changed_after_signoff"
        break
    return None


def _assessment_candidates(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(client_file, dict):
        return []
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
    plans = client_file.get("plans")
    if isinstance(plans, dict):
        rows.extend(_dict_rows(plans.get("writebacks")))
        rows.extend(_dict_rows(plans.get("artifacts")))
    else:
        rows.extend(_dict_rows(plans))
    recent_writebacks = client_file.get("recent_writebacks")
    if isinstance(recent_writebacks, list):
        for writeback in recent_writebacks:
            if not isinstance(writeback, dict):
                continue
            if writeback.get("operation") not in {
                "create_investment_assessment",
                "record_assessment_signoff",
            }:
                continue
            values = writeback.get("values")
            if isinstance(values, dict):
                rows.append(values)

    assessments: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if isinstance(payload, dict) and _looks_like_assessment(payload):
            assessments.append(payload)
    return prefer_first_assessment_by_identity(assessments)


def _dict_rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        if _looks_like_assessment(value):
            return [value]
        items = value.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
        return [row for row in value.values() if isinstance(row, dict)]
    return []


def _looks_like_assessment(value: Dict[str, Any]) -> bool:
    return bool(
        value.get("assessment_id")
        and (
            isinstance(value.get("assessment"), dict)
            or isinstance(value.get("sign_off_summary"), dict)
        )
    )


def _assessment_id(assessment: Dict[str, Any]) -> str:
    return str(assessment.get("assessment_id") or assessment.get("id") or "").strip()


def _assessment_version(assessment: Dict[str, Any]) -> int:
    value = assessment.get("assessment_version")
    if value is None:
        value = assessment.get("version")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _assessment_basis(assessment: Dict[str, Any]) -> Dict[str, Any]:
    content = assessment.get("assessment")
    if isinstance(content, dict):
        basis = content.get("basis")
        return basis if isinstance(basis, dict) else content
    request_data = assessment.get("request")
    signoff = assessment.get("sign_off_summary")
    return {
        **(signoff if isinstance(signoff, dict) else {}),
        **(request_data if isinstance(request_data, dict) else {}),
    }


def _assessment_money_pool_id(assessment: Dict[str, Any]) -> str:
    basis = _assessment_basis(assessment)
    return str(
        assessment.get("money_pool_id")
        or basis.get("money_pool_id")
        or basis.get("pool_id")
        or ""
    ).strip()


def _canonical_assessment_json(value: Dict[str, Any]) -> str:
    normalized = copy.deepcopy(value)
    normalized.pop("durable_artifact_id", None)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def run_asset_allocation_tool(
    args: Dict[str, Any],
    state: Any,
    *,
    config: Any,
    http_session: requests.Session,
    request_timeout_seconds: int,
    log_debug: LogDebug,
    session: Optional[requests.Session] = None,
    authorization: Optional[Dict[str, Any]] = None,
    _validation_retry_attempted: bool = False,
) -> Dict[str, Any]:
    """Execute a recommendation-grade allocation only after durable authorization."""
    if read_only_tool_execution_cancelled():
        return _cancelled_tool_result()
    if not isinstance(authorization, dict) or authorization.get("success") is not True:
        return _blocked_tool_result("signed_assessment_authorization_required")
    assessment_ref = authorization.get("assessment_ref")
    assessment_fingerprint = str(authorization.get("assessment_fingerprint") or "")
    authorized_arguments = authorization.get("arguments")
    authorized_mandate = authorization.get("mandate")
    assessment_eligibility = authorization.get("assessment_eligibility")
    assessment_integrity = authorization.get("assessment_integrity")
    mandate_support = authorization.get("mandate_support")
    if (
        not isinstance(assessment_ref, dict)
        or not assessment_fingerprint
        or not isinstance(authorized_arguments, dict)
        or not isinstance(authorized_mandate, dict)
        or not isinstance(assessment_eligibility, dict)
        or assessment_eligibility.get("passed") is not True
        or not isinstance(assessment_integrity, dict)
        or assessment_integrity.get("passed") is not True
        or not isinstance(mandate_support, dict)
        or mandate_support.get("passed") is not True
    ):
        return _blocked_tool_result("signed_assessment_authorization_invalid")
    if _canonical_engine_arguments(args) != _canonical_engine_arguments(authorized_arguments):
        return _blocked_tool_result("signed_assessment_input_mismatch")

    target_volatility = args.get("target_volatility")
    active_risk_percentage = args.get("active_risk_percentage")
    total_investment = args.get("total_investment")
    excluded_asset_classes_raw = args.get("excluded_asset_classes")
    target_volatility_tolerance_bps = _finite_number(
        args.get("target_volatility_tolerance_bps")
    )

    if target_volatility is None:
        return {"success": False, "error": "target_volatility is required"}
    if total_investment is None:
        return {"success": False, "error": "total_investment is required"}
    try:
        target_volatility = float(target_volatility)
    except (TypeError, ValueError):
        return {"success": False, "error": "target_volatility must be numeric"}
    if not math.isfinite(target_volatility) or not (0.05 <= target_volatility <= 0.20):
        return {"success": False, "error": "target_volatility must be between 0.05 and 0.20"}
    if active_risk_percentage is None:
        active_risk_percentage = 0.0
    try:
        active_risk_percentage = float(active_risk_percentage)
    except (TypeError, ValueError):
        return {"success": False, "error": "active_risk_percentage must be numeric"}
    if active_risk_percentage > 1.0:
        active_risk_percentage = active_risk_percentage / 100.0
    if not math.isfinite(active_risk_percentage) or not (0.0 <= active_risk_percentage <= 1.0):
        return {"success": False, "error": "active_risk_percentage must be between 0 and 1"}
    try:
        total_investment = float(total_investment)
    except (TypeError, ValueError):
        return {"success": False, "error": "total_investment must be numeric"}
    if not math.isfinite(total_investment) or total_investment <= 0:
        return {"success": False, "error": "total_investment must be positive"}
    mandate_supported, mandate_error, current_mandate_support = validate_supported_mandate(
        tolerance_bps=target_volatility_tolerance_bps,
        liquidity_requirement=authorized_mandate.get("liquidity_requirement"),
        complexity_preference=authorized_mandate.get("complexity_preference"),
    )
    if not mandate_supported:
        return _blocked_tool_result(mandate_error or "assessment_mandate_unsupported")

    tool_args = build_asset_allocation_tool_args(
        {
            "target_volatility": target_volatility,
            "active_risk_percentage": active_risk_percentage,
            "total_investment": total_investment,
        }
    )

    asset_allocation_payload: Dict[str, Any] = {
        "target_volatility": tool_args.target_volatility,
        "active_risk_percentage": tool_args.active_risk_percentage,
        "investment_amount": tool_args.total_investment,
    }
    if excluded_asset_classes_raw is not None:
        if not isinstance(excluded_asset_classes_raw, list):
            return {
                "success": False,
                "error": "excluded_asset_classes must be a list of asset-class names",
            }
        normalized_exclusions, exclusion_error = _normalize_exclusions(excluded_asset_classes_raw)
        if exclusion_error:
            return {
                "success": False,
                "error": exclusion_error["error"],
                "details": exclusion_error,
                "status": {
                    "execution": "blocked",
                    "valid_for_recommendation": False,
                    "contract_version": ASSET_ALLOCATION_RESULT_CONTRACT_VERSION,
                },
            }
        if normalized_exclusions:
            asset_allocation_payload["excluded_asset_classes"] = normalized_exclusions
        else:
            asset_allocation_payload["excluded_asset_classes"] = []

    url = f"{_asset_allocation_model_url(config)}{_asset_allocation_optimize_path(config)}"
    active_session = session or http_session

    cache_key = compute_tool_cache_key(
        "asset_allocation_model",
        {
            "assessment_fingerprint": assessment_fingerprint,
            "engine_inputs": asset_allocation_payload,
            "engine_response_contract_version": (
                ASSET_ALLOCATION_ENGINE_RESPONSE_CONTRACT_VERSION
            ),
        },
    )
    cached = state._tool_result_cache.get(cache_key)
    if cached is not None:
        def commit_cache_hit() -> None:
            if (
                cached.get("success")
                and cached.get("valid_for_recommendation") is True
                and cached.get("full_result")
            ):
                state.latest_asset_allocation_full = cached["full_result"]
            elif cached.get("valid_for_recommendation") is not True:
                state.latest_asset_allocation_full = None
                state.latest_asset_allocation = None

        committed, _ = commit_read_only_tool_state(commit_cache_hit)
        if not committed:
            return _cancelled_tool_result()
        log_debug(f"Asset allocation model cache HIT - skipping HTTP call (key={cache_key[:12]})")
        return copy.deepcopy(cached)

    if read_only_tool_execution_cancelled():
        return _cancelled_tool_result()
    try:
        response = active_session.post(
            url,
            json=asset_allocation_payload,
            headers=build_asset_allocation_headers(config),
            timeout=effective_read_only_request_timeout(request_timeout_seconds),
        )
    except requests.RequestException as exc:
        if read_only_tool_execution_cancelled():
            return _cancelled_tool_result()
        return {
            "success": False,
            "error": "Asset allocation model API call failed",
            "details": str(exc),
        }

    if response.status_code != 200:
        response_detail: Any = response.text[:600]
        try:
            response_detail = response.json()
        except (TypeError, ValueError):
            pass
        engine_code = (
            str(response_detail.get("code") or "").strip().lower()
            if isinstance(response_detail, dict)
            else ""
        )
        return {
            "success": False,
            "error": (
                "unsupported_constraint"
                if engine_code == "unsupported_constraint"
                else "Asset allocation model API call failed"
            ),
            "status_code": response.status_code,
            "details": response_detail,
            "status": {
                "execution": "blocked" if response.status_code == 422 else "failed",
                "valid_for_recommendation": False,
                "contract_version": ASSET_ALLOCATION_RESULT_CONTRACT_VERSION,
            },
        }

    try:
        raw_result = response.json()
    except (TypeError, ValueError) as exc:
        return {
            "success": False,
            "error": "asset_allocation_model_malformed_json",
            "details": str(exc),
        }
    if read_only_tool_execution_cancelled():
        return _cancelled_tool_result()
    if not isinstance(raw_result, dict):
        return {"success": False, "error": "asset_allocation_model_invalid_response"}
    if raw_result.get("success") is not True:
        return {
            "success": False,
            "error": str(raw_result.get("error") or "asset_allocation_model_failed"),
            "details": raw_result,
        }

    from api.services.asset_allocation_artifact_adapter import normalize_asset_allocation_result

    raw_engine_result = copy.deepcopy(raw_result)
    raw_result = normalize_asset_allocation_result(
        {**copy.deepcopy(raw_result), "total_investment": total_investment}
    )
    _canonicalize_allocation_asset_classes(raw_result)
    securities_raw = raw_result.get("securities")
    normalized_securities = _strict_normalize_engine_securities(securities_raw)

    requested_exclusions = list(asset_allocation_payload.get("excluded_asset_classes") or [])
    raw_result["securities"] = normalized_securities
    engine_response_schema = _engine_response_schema_check(
        raw_engine_result,
        raw_result,
    )
    constraint_checks = evaluate_asset_allocation_constraints(
        raw_result,
        target_volatility=target_volatility,
        active_risk_percentage=active_risk_percentage,
        total_investment=total_investment,
        excluded_asset_classes=requested_exclusions,
        signed_assessment_valid=True,
        target_tolerance_bps=target_volatility_tolerance_bps,
        engine_response_schema=engine_response_schema,
        assessment_eligibility=assessment_eligibility,
        assessment_integrity=assessment_integrity,
        mandate_support={"passed": True, **current_mandate_support},
    )
    valid_for_recommendation = all(
        isinstance(check, dict) and check.get("passed") is True
        for check in constraint_checks.values()
    )
    warnings = [
        f"Constraint check failed: {name}"
        for name, check in constraint_checks.items()
        if not isinstance(check, dict) or check.get("passed") is not True
    ]
    if not valid_for_recommendation and not _validation_retry_attempted:
        log_debug(
            "Asset allocation validation failed - retrying the signed mandate once "
            "before returning a blocked result"
        )
        return run_asset_allocation_tool(
            args,
            state,
            config=config,
            http_session=http_session,
            request_timeout_seconds=request_timeout_seconds,
            log_debug=log_debug,
            session=session,
            authorization=authorization,
            _validation_retry_attempted=True,
        )
    allocation_id = f"allocation_{cache_key[:24]}"
    expected_return_decimal = _normalize_decimal_percentage(
        raw_result.get("portfolio_expected_return_pct")
    )
    expected_volatility_decimal = _normalize_decimal_percentage(
        raw_result.get("portfolio_expected_volatility_pct")
    )

    compact_result = {
        "success": True,
        "allocation_id": allocation_id,
        "source_assessment": assessment_ref,
        "status": {
            "execution": "succeeded",
            "valid_for_recommendation": valid_for_recommendation,
            "contract_version": ASSET_ALLOCATION_RESULT_CONTRACT_VERSION,
        },
        "valid_for_recommendation": valid_for_recommendation,
        "constraint_checks": constraint_checks,
        "warnings": warnings,
        "requested_inputs": {
            "target_volatility_annual_decimal": target_volatility,
            "active_risk_percentage_decimal": active_risk_percentage,
            "investment_amount_usd": total_investment,
            "excluded_asset_classes": requested_exclusions,
            "target_volatility_tolerance_bps": target_volatility_tolerance_bps,
        },
        "portfolio_expected_return_annual_decimal": expected_return_decimal,
        "portfolio_expected_volatility_annual_decimal": expected_volatility_decimal,
        "portfolio_expected_return_pct": raw_result.get("portfolio_expected_return_pct"),
        "portfolio_expected_volatility_pct": raw_result.get("portfolio_expected_volatility_pct"),
        "total_investment": total_investment,
        "excluded_asset_classes": requested_exclusions,
        "constraint_contract": raw_result.get("constraint_contract"),
        "optimization_calibration": raw_engine_result.get(
            "optimization_calibration"
        ),
        "securities": normalized_securities,
        "investment_allocations": raw_result.get("investment_allocations"),
        "layers": raw_result.get("layers"),
        "portfolio_summary": raw_result.get("portfolio_summary"),
    }
    selected_weights = (
        raw_result.get("layers", {})
        .get("layer1", {})
        .get("selected_weights", {})
    )
    latest_asset_allocation = None
    if (
        valid_for_recommendation
        and isinstance(selected_weights, dict)
        and selected_weights
    ):
        latest_asset_allocation = {
            str(k): float(v)
            for k, v in selected_weights.items()
        }

    result = {
        "success": True,
        "valid_for_recommendation": valid_for_recommendation,
        "allocation_id": allocation_id,
        "status": compact_result["status"],
        "constraint_checks": constraint_checks,
        "warnings": warnings,
        "full_result": compact_result,
    }
    if engine_response_schema.get("passed") is not True:
        result["error"] = "asset_allocation_model_invalid_response_schema"
    cached_result = copy.deepcopy(result)

    def commit_success() -> None:
        if valid_for_recommendation and latest_asset_allocation is not None:
            state.latest_asset_allocation_full = compact_result
            state.latest_asset_allocation = latest_asset_allocation
        else:
            # Never let a malformed or constraint-invalid response become the
            # allocation consumed by a later cash-flow call in this session.
            state.latest_asset_allocation_full = None
            state.latest_asset_allocation = None
        if valid_for_recommendation:
            state._tool_result_cache[cache_key] = cached_result

    committed, _ = commit_read_only_tool_state(commit_success)
    if not committed:
        return _cancelled_tool_result()
    log_debug(f"Asset allocation model cache STORE (key={cache_key[:12]})")
    return result


def _blocked_tool_result(error: str) -> Dict[str, Any]:
    return {
        "success": False,
        "valid_for_recommendation": False,
        "error": error,
        "status": {
            "execution": "blocked",
            "valid_for_recommendation": False,
            "contract_version": ASSET_ALLOCATION_RESULT_CONTRACT_VERSION,
        },
        "constraint_checks": {
            "signed_assessment": {
                "passed": False,
                "error": error,
            }
        },
        "warnings": ["A durable, current, signed assessment is required."],
    }


def _cancelled_tool_result() -> Dict[str, Any]:
    return {
        "success": False,
        "valid_for_recommendation": False,
        "error": "tool_execution_cancelled",
        "cancelled": True,
        "status": {
            "execution": "cancelled",
            "valid_for_recommendation": False,
            "contract_version": ASSET_ALLOCATION_RESULT_CONTRACT_VERSION,
        },
        "constraint_checks": {},
        "warnings": ["Asset-allocation execution was cancelled before a result was accepted."],
    }


def _strict_normalize_engine_securities(
    value: Any,
) -> List[Dict[str, Any]]:
    """Normalize securities only when every engine row satisfies the contract.

    Partial normalization is unsafe here: dropping one malformed row can make
    the remaining rows appear to sum and reconcile correctly.  The companion
    ``engine_response_schema`` check retains the field-level errors; returning
    no rows ensures the other reconciliation checks also fail closed.
    """

    errors: List[Dict[str, Any]] = []
    _validate_security_rows(
        value,
        path="$.securities",
        errors=errors,
        require_nonempty=True,
    )
    if errors or not isinstance(value, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for sec in value:
        # The schema validation above establishes all of these types and fields.
        ticker = str(sec.get("isin") or sec.get("ticker") or sec.get("symbol")).strip()
        weight = float(sec["weight"])
        amount = float(sec["amount"])
        sec_type = str(sec.get("security_type", "") or "").strip().lower()
        normalized.append(
            {
                "isin": ticker,
                "ticker": ticker,
                "asset_class": str(sec["asset_class"]).strip(),
                "security_type": sec_type or "unspecified",
                "weight": weight,
                "amount": amount,
            }
        )
    return normalized


def _engine_response_schema_check(
    raw_result: Dict[str, Any],
    normalized_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate recommendation-critical engine output before accepting it.

    Both phases are checked.  Raw counts make any loss during adaptation
    visible, while normalized checks prevent a derived or canonicalized row
    from bypassing the same strict requirements.
    """

    errors: List[Dict[str, Any]] = []
    raw_layers = raw_result.get("layers")
    raw_layer1 = raw_layers.get("layer1") if isinstance(raw_layers, dict) else None
    raw_selected = (
        raw_layer1.get("selected_weights")
        if isinstance(raw_layer1, dict)
        else None
    )
    normalized_layers = normalized_result.get("layers")
    normalized_layer1 = (
        normalized_layers.get("layer1")
        if isinstance(normalized_layers, dict)
        else None
    )
    normalized_selected = (
        normalized_layer1.get("selected_weights")
        if isinstance(normalized_layer1, dict)
        else None
    )

    raw_selected_stats = _validate_selected_weights(
        raw_selected,
        path="$.layers.layer1.selected_weights",
        errors=errors,
    )
    normalized_selected_stats = _validate_selected_weights(
        normalized_selected,
        path="$.normalized.layers.layer1.selected_weights",
        errors=errors,
    )

    raw_securities_present = "securities" in raw_result
    raw_securities = raw_result.get("securities")
    raw_security_stats = _validate_security_rows(
        raw_securities,
        path="$.securities",
        errors=errors,
        # The production engine currently supplies securities through layers.
        # An absent or empty top-level list is therefore valid raw input.
        allow_absent=not raw_securities_present,
        require_nonempty=False,
    )
    normalized_securities = normalized_result.get("securities")
    normalized_security_stats = _validate_security_rows(
        normalized_securities,
        path="$.normalized.securities",
        errors=errors,
        require_nonempty=True,
    )

    raw_selected_count = raw_selected_stats["entry_count"]
    normalized_selected_count = normalized_selected_stats["entry_count"]
    if raw_selected_count != normalized_selected_count:
        errors.append(
            {
                "path": "$.layers.layer1.selected_weights",
                "code": "entry_count_changed_during_normalization",
                "raw_count": raw_selected_count,
                "normalized_count": normalized_selected_count,
            }
        )

    raw_security_count = raw_security_stats["row_count"]
    normalized_security_count = normalized_security_stats["row_count"]
    security_source = (
        "engine_supplied"
        if raw_securities_present and raw_security_count > 0
        else "derived_from_layers"
    )
    security_count_preserved: Optional[bool] = None
    if security_source == "engine_supplied":
        security_count_preserved = raw_security_count == normalized_security_count
        if not security_count_preserved:
            errors.append(
                {
                    "path": "$.securities",
                    "code": "row_count_changed_during_normalization",
                    "raw_count": raw_security_count,
                    "normalized_count": normalized_security_count,
                }
            )

    return {
        "passed": not errors,
        "contract_version": ASSET_ALLOCATION_ENGINE_RESPONSE_CONTRACT_VERSION,
        "raw": {
            "selected_weights": raw_selected_stats,
            "securities_present": raw_securities_present,
            "securities": raw_security_stats,
        },
        "normalized": {
            "selected_weights": normalized_selected_stats,
            "securities": normalized_security_stats,
        },
        "normalization": {
            "selected_weight_count_preserved": (
                raw_selected_count == normalized_selected_count
            ),
            "security_source": security_source,
            "security_row_count_preserved": security_count_preserved,
        },
        "errors": errors,
    }


def _validate_selected_weights(
    value: Any,
    *,
    path: str,
    errors: List[Dict[str, Any]],
) -> Dict[str, int]:
    if not isinstance(value, dict):
        errors.append(
            {
                "path": path,
                "code": "expected_nonempty_object",
                "observed_type": type(value).__name__,
            }
        )
        return {"entry_count": 0, "valid_entry_count": 0}
    if not value:
        errors.append({"path": path, "code": "empty_object"})

    valid_count = 0
    for raw_name, raw_weight in value.items():
        entry_valid = True
        entry_path = f"{path}.{raw_name}"
        if not isinstance(raw_name, str) or not raw_name.strip():
            errors.append(
                {
                    "path": entry_path,
                    "code": "asset_class_missing_or_non_string",
                    "observed_type": type(raw_name).__name__,
                }
            )
            entry_valid = False
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            errors.append(
                {
                    "path": entry_path,
                    "code": "weight_non_numeric",
                    "observed_type": type(raw_weight).__name__,
                }
            )
            entry_valid = False
        elif not math.isfinite(float(raw_weight)):
            errors.append({"path": entry_path, "code": "weight_nonfinite"})
            entry_valid = False
        if entry_valid:
            valid_count += 1
    return {"entry_count": len(value), "valid_entry_count": valid_count}


def _validate_security_rows(
    value: Any,
    *,
    path: str,
    errors: List[Dict[str, Any]],
    allow_absent: bool = False,
    require_nonempty: bool,
) -> Dict[str, int]:
    if value is None and allow_absent:
        return {"row_count": 0, "valid_row_count": 0}
    if not isinstance(value, list):
        errors.append(
            {
                "path": path,
                "code": "expected_array",
                "observed_type": type(value).__name__,
            }
        )
        return {"row_count": 0, "valid_row_count": 0}
    if require_nonempty and not value:
        errors.append({"path": path, "code": "empty_array"})

    valid_count = 0
    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        if not isinstance(row, dict):
            errors.append(
                {
                    "path": row_path,
                    "code": "row_not_object",
                    "observed_type": type(row).__name__,
                }
            )
            continue
        row_valid = True
        identifiers = [row.get(name) for name in ("isin", "ticker", "symbol")]
        if not any(isinstance(item, str) and item.strip() for item in identifiers):
            errors.append({"path": row_path, "code": "security_identifier_missing"})
            row_valid = False
        asset_class = row.get("asset_class")
        if not isinstance(asset_class, str) or not asset_class.strip():
            errors.append(
                {"path": f"{row_path}.asset_class", "code": "asset_class_missing"}
            )
            row_valid = False
        for field_name in ("weight", "amount"):
            field_value = row.get(field_name)
            field_path = f"{row_path}.{field_name}"
            if isinstance(field_value, bool) or not isinstance(field_value, (int, float)):
                errors.append(
                    {
                        "path": field_path,
                        "code": f"{field_name}_non_numeric",
                        "observed_type": type(field_value).__name__,
                    }
                )
                row_valid = False
            elif not math.isfinite(float(field_value)):
                errors.append(
                    {"path": field_path, "code": f"{field_name}_nonfinite"}
                )
                row_valid = False
        if row_valid:
            valid_count += 1
    return {"row_count": len(value), "valid_row_count": valid_count}


def _canonicalize_allocation_asset_classes(result: Dict[str, Any]) -> None:
    """Trim exact model labels and leave unknown labels visible for validation."""
    layers = result.get("layers") if isinstance(result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    selected = layer1.get("selected_weights")
    if isinstance(selected, dict):
        canonical: Dict[str, Any] = {}
        for raw_name, value in selected.items():
            cleaned = str(raw_name or "").strip()
            resolved = ASSET_ALLOCATION_MODEL_ASSET_LOOKUP.get(cleaned.lower())
            name = resolved or cleaned
            if name in canonical:
                existing_number = _finite_number(canonical[name])
                added_number = _finite_number(value)
                canonical[name] = (
                    existing_number + added_number
                    if existing_number is not None and added_number is not None
                    else value
                )
            else:
                canonical[name] = value
        layer1["selected_weights"] = canonical

    allocations = result.get("investment_allocations")
    by_asset_class = (
        allocations.get("by_asset_class") if isinstance(allocations, dict) else None
    )
    if isinstance(by_asset_class, dict):
        canonical_allocations: Dict[str, Any] = {}
        for raw_name, payload in by_asset_class.items():
            cleaned = str(raw_name or "").strip()
            resolved = ASSET_ALLOCATION_MODEL_ASSET_LOOKUP.get(cleaned.lower())
            canonical_allocations[resolved or cleaned] = payload
        allocations["by_asset_class"] = canonical_allocations

    securities = result.get("securities")
    if isinstance(securities, list):
        for security in securities:
            if not isinstance(security, dict):
                continue
            cleaned = str(security.get("asset_class") or "").strip()
            resolved = ASSET_ALLOCATION_MODEL_ASSET_LOOKUP.get(cleaned.lower())
            security["asset_class"] = resolved or cleaned


def evaluate_asset_allocation_constraints(
    result: Dict[str, Any],
    *,
    target_volatility: float,
    active_risk_percentage: float,
    total_investment: float,
    excluded_asset_classes: List[str],
    signed_assessment_valid: bool,
    target_tolerance_bps: Optional[float],
    engine_response_schema: Optional[Dict[str, Any]] = None,
    assessment_eligibility: Optional[Dict[str, Any]] = None,
    assessment_integrity: Optional[Dict[str, Any]] = None,
    mandate_support: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return typed fail-closed recommendation checks for a model result."""
    layers = result.get("layers") if isinstance(result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    layer2 = layers.get("layer2") if isinstance(layers.get("layer2"), dict) else {}
    selected = _numeric_mapping(layer1.get("selected_weights"))
    securities = [
        row
        for row in (result.get("securities") or [])
        if isinstance(row, dict)
    ]
    acknowledged_exclusions = {
        str(value).strip()
        for value in (result.get("excluded_asset_classes") or [])
        if isinstance(value, str)
    }
    requested_exclusions = set(excluded_asset_classes)
    constraint_contract = (
        result.get("constraint_contract")
        if isinstance(result.get("constraint_contract"), dict)
        else {}
    )
    acknowledgements = (
        constraint_contract.get("acknowledgements")
        if isinstance(constraint_contract.get("acknowledgements"), dict)
        else {}
    )
    active_ack = (
        acknowledgements.get("active_risk_percentage")
        if isinstance(acknowledgements.get("active_risk_percentage"), dict)
        else {}
    )
    amount_ack = (
        acknowledgements.get("investment_amount")
        if isinstance(acknowledgements.get("investment_amount"), dict)
        else {}
    )
    exclusions_ack = (
        acknowledgements.get("excluded_asset_classes")
        if isinstance(acknowledgements.get("excluded_asset_classes"), dict)
        else {}
    )
    contract_version_valid = (
        constraint_contract.get("version") == "asset_allocation_constraints.v1"
    )
    active_ack_value = _finite_number(active_ack.get("applied_decimal"))
    amount_ack_value = _finite_number(amount_ack.get("applied_usd"))
    exclusions_ack_values = exclusions_ack.get("applied")
    exclusions_ack_set = {
        str(value).strip()
        for value in exclusions_ack_values
        if isinstance(value, str)
    } if isinstance(exclusions_ack_values, list) else set()
    engine_contract_passed = bool(
        contract_version_valid
        and active_ack.get("supported") is True
        and active_ack.get("source") == "signed_mandate_api"
        and active_ack_value is not None
        and abs(active_ack_value - active_risk_percentage) <= ACTIVE_RISK_TOLERANCE
        and amount_ack.get("supported") is True
        and amount_ack_value is not None
        and abs(amount_ack_value - total_investment) <= 0.01
        and exclusions_ack.get("supported") is True
        and exclusions_ack_set == requested_exclusions
    )

    asset_weight_sum = sum(selected.values())
    asset_weights_in_bounds = bool(selected) and all(
        0.0 <= weight <= 1.0 for weight in selected.values()
    )
    security_weights = [
        float(row["weight"])
        for row in securities
        if isinstance(row.get("weight"), (int, float))
        and not isinstance(row.get("weight"), bool)
        and math.isfinite(float(row["weight"]))
    ]
    security_weight_sum = sum(security_weights)
    security_weights_in_bounds = bool(securities) and all(
        0.0 <= weight <= 1.0 for weight in security_weights
    )
    security_amounts = [
        float(row["amount"])
        for row in securities
        if isinstance(row.get("amount"), (int, float))
        and not isinstance(row.get("amount"), bool)
        and math.isfinite(float(row["amount"]))
    ]
    security_dollar_sum = sum(security_amounts)
    security_amounts_nonnegative = bool(securities) and all(
        amount >= 0.0 for amount in security_amounts
    )
    dollar_tolerance = max(0.01, len(securities) * 0.01)
    security_notional_differences: List[Dict[str, Any]] = []
    security_notionals_reconcile = bool(securities)
    for index, row in enumerate(securities):
        weight = _finite_number(row.get("weight"))
        amount = _finite_number(row.get("amount"))
        if weight is None or amount is None:
            security_notionals_reconcile = False
            continue
        expected_amount = total_investment * weight
        difference = amount - expected_amount
        row_tolerance = max(0.01, abs(expected_amount) * 1e-8)
        security_notional_differences.append(
            {
                "index": index,
                "ticker": row.get("ticker") or row.get("isin"),
                "observed_usd": amount,
                "expected_usd": expected_amount,
                "difference_usd": difference,
                "tolerance_usd": row_tolerance,
            }
        )
        if abs(difference) > row_tolerance:
            security_notionals_reconcile = False

    security_by_asset_class: Dict[str, float] = {}
    for row in securities:
        asset_class = str(row.get("asset_class") or "").strip()
        weight = row.get("weight")
        if (
            asset_class
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
            and math.isfinite(float(weight))
        ):
            security_by_asset_class[asset_class] = (
                security_by_asset_class.get(asset_class, 0.0) + float(weight)
            )
    reconciliation_differences = {
        asset_class: round(
            security_by_asset_class.get(asset_class, 0.0) - asset_weight,
            12,
        )
        for asset_class, asset_weight in selected.items()
    }
    extra_security_classes = sorted(set(security_by_asset_class) - set(selected))
    reconciliation_passed = bool(selected) and bool(securities) and not extra_security_classes and all(
        abs(value) <= WEIGHT_TOLERANCE
        for value in reconciliation_differences.values()
    )

    excluded_violations = sorted(
        asset_class
        for asset_class in requested_exclusions
        if selected.get(asset_class, 0.0) > WEIGHT_TOLERANCE
        or security_by_asset_class.get(asset_class, 0.0) > WEIGHT_TOLERANCE
    )
    exclusions_acknowledged = (
        requested_exclusions == acknowledged_exclusions
        and exclusions_ack_set == requested_exclusions
        and exclusions_ack.get("supported") is True
        and contract_version_valid
    )

    observed_active_risk = _finite_number(layer2.get("active_risk_pct"))
    active_risk_difference = (
        observed_active_risk - active_risk_percentage
        if observed_active_risk is not None
        else None
    )
    observed_volatility = _normalize_decimal_percentage(
        result.get("portfolio_expected_volatility_pct")
    )
    observed_expected_return = _finite_number(
        result.get("portfolio_expected_return_pct")
    )
    risk_difference_bps = (
        (observed_volatility - target_volatility) * 10_000.0
        if observed_volatility is not None
        else None
    )

    known_asset_classes = set(ASSET_ALLOCATION_MODEL_ASSET_CLASSES)
    unknown_asset_classes = sorted(
        (set(selected) | set(security_by_asset_class)) - known_asset_classes
    )
    response_schema_check = (
        copy.deepcopy(engine_response_schema)
        if isinstance(engine_response_schema, dict)
        else _engine_response_schema_check(result, result)
    )
    response_schema_check["passed"] = response_schema_check.get("passed") is True
    assessment_eligibility_check = (
        copy.deepcopy(assessment_eligibility)
        if isinstance(assessment_eligibility, dict)
        else {"passed": False, "error": "assessment_eligibility_evidence_missing"}
    )
    assessment_eligibility_check["passed"] = (
        assessment_eligibility_check.get("passed") is True
    )
    assessment_integrity_check = (
        copy.deepcopy(assessment_integrity)
        if isinstance(assessment_integrity, dict)
        else {"passed": False, "error": "assessment_integrity_evidence_missing"}
    )
    assessment_integrity_check["passed"] = (
        assessment_integrity_check.get("passed") is True
    )
    mandate_support_check = (
        copy.deepcopy(mandate_support)
        if isinstance(mandate_support, dict)
        else {"passed": False, "error": "mandate_support_evidence_missing"}
    )
    mandate_support_check["passed"] = mandate_support_check.get("passed") is True
    checks: Dict[str, Dict[str, Any]] = {
        "engine_response_schema": response_schema_check,
        "engine_constraint_contract": {
            "passed": engine_contract_passed,
            "required_version": "asset_allocation_constraints.v1",
            "observed_version": constraint_contract.get("version"),
            "acknowledgements": acknowledgements,
        },
        "signed_assessment": {
            "passed": signed_assessment_valid is True,
            "required": True,
        },
        "assessment_eligibility": assessment_eligibility_check,
        "assessment_integrity": assessment_integrity_check,
        "mandate_support": mandate_support_check,
        "hard_exclusions": {
            "passed": exclusions_acknowledged and not excluded_violations,
            "requested": sorted(requested_exclusions),
            "acknowledged": sorted(acknowledged_exclusions),
            "violations": excluded_violations,
        },
        "active_risk": {
            "passed": (
                engine_contract_passed
                and active_ack_value is not None
                and observed_active_risk is not None
                and active_risk_difference is not None
                and abs(active_risk_difference) <= ACTIVE_RISK_TOLERANCE
            ),
            "requested_decimal": active_risk_percentage,
            "observed_decimal": observed_active_risk,
            "tolerance_decimal": ACTIVE_RISK_TOLERANCE,
        },
        "asset_weight_sum": {
            "passed": (
                asset_weights_in_bounds
                and abs(asset_weight_sum - 1.0) <= WEIGHT_TOLERANCE
            ),
            "observed": asset_weight_sum,
            "expected": 1.0,
            "tolerance": WEIGHT_TOLERANCE,
        },
        "asset_weight_bounds": {
            "passed": asset_weights_in_bounds,
            "minimum": min(selected.values()) if selected else None,
            "maximum": max(selected.values()) if selected else None,
            "allowed": [0.0, 1.0],
        },
        "security_weight_sum": {
            "passed": (
                bool(securities)
                and len(security_weights) == len(securities)
                and security_weights_in_bounds
                and abs(security_weight_sum - 1.0) <= WEIGHT_TOLERANCE
            ),
            "observed": security_weight_sum,
            "expected": 1.0,
            "tolerance": WEIGHT_TOLERANCE,
        },
        "security_weight_bounds": {
            "passed": (
                len(security_weights) == len(securities)
                and security_weights_in_bounds
            ),
            "minimum": min(security_weights) if security_weights else None,
            "maximum": max(security_weights) if security_weights else None,
            "allowed": [0.0, 1.0],
        },
        "security_dollar_sum": {
            "passed": (
                bool(securities)
                and len(security_amounts) == len(securities)
                and security_amounts_nonnegative
                and abs(security_dollar_sum - total_investment) <= dollar_tolerance
            ),
            "observed_usd": security_dollar_sum,
            "expected_usd": total_investment,
            "tolerance_usd": dollar_tolerance,
        },
        "security_notional_reconciliation": {
            "passed": (
                len(security_notional_differences) == len(securities)
                and security_notionals_reconcile
                and security_amounts_nonnegative
            ),
            "rows": security_notional_differences,
        },
        "asset_class_reconciliation": {
            "passed": reconciliation_passed,
            "differences": reconciliation_differences,
            "extra_security_asset_classes": extra_security_classes,
        },
        "canonical_asset_classes": {
            "passed": not unknown_asset_classes,
            "unknown": unknown_asset_classes,
        },
        "portfolio_metrics_finite": {
            "passed": (
                observed_expected_return is not None
                and observed_volatility is not None
            ),
            "expected_return_raw": result.get("portfolio_expected_return_pct"),
            "expected_volatility_annual_decimal": observed_volatility,
        },
        "target_volatility": {
            "evaluated": target_tolerance_bps is not None,
            "passed": (
                target_tolerance_bps is not None
                and risk_difference_bps is not None
                and abs(risk_difference_bps) <= target_tolerance_bps
            ),
            "target_annual_decimal": target_volatility,
            "observed_annual_decimal": observed_volatility,
            "difference_bps": risk_difference_bps,
            "allowed_tolerance_bps": target_tolerance_bps,
            "tolerance_policy": "signed_assessment",
        },
    }
    return checks


def _numeric_mapping(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    mapped: Dict[str, float] = {}
    for key, raw_value in value.items():
        number = _finite_number(raw_value)
        if number is not None:
            mapped[str(key).strip()] = number
    return mapped


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_engine_arguments(value: Dict[str, Any]) -> str:
    """Canonicalize mandate inputs before binding them to authorization."""
    relevant = {
        key: value.get(key)
        for key in (
            "target_volatility",
            "active_risk_percentage",
            "total_investment",
            "excluded_asset_classes",
            "target_volatility_tolerance_bps",
        )
    }
    return json.dumps(relevant, sort_keys=True, separators=(",", ":"), default=str)
