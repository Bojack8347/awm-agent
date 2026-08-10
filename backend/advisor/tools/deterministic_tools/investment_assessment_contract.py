"""Fail-closed contract shared by assessment creation, sign-off, and allocation.

These limits describe what the current optimizer integration can actually
enforce.  They are technical eligibility rules, not investment advice or a
recommendation policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Layer 1's largest configured numerical risk tolerance is 0.008 annual
# volatility (80 basis points).  A wider client-supplied acceptance band would
# be looser than the optimizer's own modeled domain and could make virtually
# any result appear to meet its target.
MAX_TARGET_VOLATILITY_TOLERANCE_BPS = 80.0

# The current NEO request and response contracts do not implement portfolio
# liquidity or complexity constraints.  Until they do, the only safe inputs are
# explicit declarations that no additional constraint is being requested.
SUPPORTED_LIQUIDITY_REQUIREMENT = "no_additional_portfolio_liquidity_constraint"
SUPPORTED_COMPLEXITY_PREFERENCE = "optimizer_unrestricted"

SUPPORTED_ASSESSMENT_VERDICT = "aligned"
SUPPORTED_NON_BLOCKING_CONCERN_SEVERITY = "soft"


def prefer_first_assessment_by_identity(
    candidates: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep one row per assessment identity, preferring earlier (authoritative) copies.

    Callers must pass candidates with durable/authoritative sources first. Stale
    snapshot copies in artifacts or writebacks share the same identity but may
    lag metadata such as requires_revalidation; those later duplicates are dropped.
    """

    seen: set[Tuple[str, int, str]] = set()
    preferred: List[Dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        assessment_id = str(
            candidate.get("assessment_id") or candidate.get("id") or ""
        ).strip()
        if not assessment_id:
            continue
        version = candidate.get("assessment_version")
        if version is None:
            version = candidate.get("version")
        if isinstance(version, bool):
            version_number = 0
        elif isinstance(version, int):
            version_number = version
        elif isinstance(version, str) and version.isdigit():
            version_number = int(version)
        else:
            version_number = 0
        assessment = candidate.get("assessment")
        basis = assessment.get("basis") if isinstance(assessment, dict) else {}
        basis = basis if isinstance(basis, dict) else {}
        money_pool_id = str(
            candidate.get("money_pool_id")
            or basis.get("money_pool_id")
            or basis.get("pool_id")
            or ""
        ).strip()
        identity = (assessment_id, version_number, money_pool_id)
        if identity in seen:
            continue
        seen.add(identity)
        preferred.append(candidate)
    return preferred


def compute_assessment_content_fingerprint(payload: Dict[str, Any]) -> str:
    """Hash the immutable assessment identity, validity window, and content."""

    immutable = {
        key: payload.get(key)
        for key in (
            "assessment_id",
            "assessment_version",
            "money_pool_id",
            "assessed_at",
            "valid_until",
            "assessment",
        )
    }
    canonical = json.dumps(
        immutable,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_assessment_content_fingerprint(
    payload: Any,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Require a stored fingerprint to match the canonical immutable content."""

    if not isinstance(payload, dict):
        return False, "assessment_content_invalid", {}
    observed = payload.get("content_fingerprint")
    if not isinstance(observed, str) or not observed.strip():
        return False, "assessment_content_fingerprint_required", {}
    expected = compute_assessment_content_fingerprint(payload)
    if not hmac.compare_digest(observed.strip(), expected):
        return (
            False,
            "assessment_content_fingerprint_mismatch",
            {"expected": expected, "observed": observed.strip()},
        )
    return True, None, {"content_fingerprint": expected}


def validate_supported_mandate(
    *,
    tolerance_bps: Any,
    liquidity_requirement: Any,
    complexity_preference: Any,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Validate only constraints the current optimizer integration can honor."""

    if isinstance(tolerance_bps, bool):
        tolerance = None
    else:
        try:
            tolerance = float(tolerance_bps)
        except (TypeError, ValueError):
            tolerance = None
    if tolerance is None or not math.isfinite(tolerance):
        return False, "target_volatility_tolerance_bps_invalid", {}
    if not 0.0 <= tolerance <= MAX_TARGET_VOLATILITY_TOLERANCE_BPS:
        return (
            False,
            "target_volatility_tolerance_bps_unsupported",
            {
                "maximum_supported_bps": MAX_TARGET_VOLATILITY_TOLERANCE_BPS,
                "observed_bps": tolerance,
            },
        )

    liquidity = (
        liquidity_requirement.strip()
        if isinstance(liquidity_requirement, str)
        else ""
    )
    if liquidity != SUPPORTED_LIQUIDITY_REQUIREMENT:
        return (
            False,
            "liquidity_requirement_unsupported",
            {
                "supported_values": [SUPPORTED_LIQUIDITY_REQUIREMENT],
                "observed": liquidity or None,
            },
        )

    complexity = (
        complexity_preference.strip()
        if isinstance(complexity_preference, str)
        else ""
    )
    if complexity != SUPPORTED_COMPLEXITY_PREFERENCE:
        return (
            False,
            "complexity_preference_unsupported",
            {
                "supported_values": [SUPPORTED_COMPLEXITY_PREFERENCE],
                "observed": complexity or None,
            },
        )
    return True, None, {
        "target_volatility_tolerance_bps": tolerance,
        "maximum_supported_tolerance_bps": MAX_TARGET_VOLATILITY_TOLERANCE_BPS,
        "liquidity_requirement": liquidity,
        "complexity_preference": complexity,
        "support_basis": "current_optimizer_contract",
    }


def validate_assessment_eligibility(
    assessment: Any,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Require an explicit aligned verdict and internally consistent risk basis.

    Unknown verdicts, malformed review data, and concerns whose severity is not
    explicitly non-blocking are rejected.  Risk labels are compared exactly
    (case-insensitively) because no approved mapping between risk taxonomies is
    available.
    """

    if not isinstance(assessment, dict):
        return False, "assessment_content_invalid", {}
    verdict = str(assessment.get("verdict") or "").strip().lower()
    if verdict != SUPPORTED_ASSESSMENT_VERDICT:
        return (
            False,
            "assessment_verdict_unsupported",
            {
                "supported_values": [SUPPORTED_ASSESSMENT_VERDICT],
                "observed": verdict or None,
            },
        )

    internal_review = assessment.get("internal_review")
    if not isinstance(internal_review, dict):
        return False, "assessment_internal_review_invalid", {}
    concerns = internal_review.get("concerns")
    if not isinstance(concerns, list):
        return False, "assessment_concerns_invalid", {}
    for index, concern in enumerate(concerns):
        if not isinstance(concern, dict):
            return (
                False,
                "assessment_concerns_invalid",
                {"index": index},
            )
        severity = str(concern.get("severity") or "").strip().lower()
        if severity != SUPPORTED_NON_BLOCKING_CONCERN_SEVERITY:
            return (
                False,
                "assessment_blocking_concern",
                {
                    "index": index,
                    "severity": severity or None,
                    "issue": concern.get("issue") or concern.get("title"),
                },
            )

    basis = assessment.get("basis")
    if not isinstance(basis, dict):
        return False, "assessment_basis_invalid", {}
    expected_risk = str(
        basis.get("target_risk")
        or basis.get("risk")
        or basis.get("risk_tolerance")
        or ""
    ).strip()
    recommended_risk = str(assessment.get("recommended_risk_level") or "").strip()
    if not expected_risk or not recommended_risk:
        return False, "assessment_risk_level_required", {}
    if recommended_risk.casefold() != expected_risk.casefold():
        return (
            False,
            "assessment_risk_level_mismatch",
            {
                "basis_risk": expected_risk,
                "recommended_risk_level": recommended_risk,
                "comparison": "exact_case_insensitive",
            },
        )
    return True, None, {
        "verdict": SUPPORTED_ASSESSMENT_VERDICT,
        "blocking_concerns": 0,
        "recommended_risk_level": recommended_risk,
        "basis_risk": expected_risk,
        "risk_comparison": "exact_case_insensitive",
    }
