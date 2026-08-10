"""Immutable allocation snapshots and a compact deterministic agent view."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict, Mapping, Optional

from advisor.tools.deterministic_tools.cashflow_analysis_snapshot import (
    canonical_fingerprint,
)


AGENT_VIEW_SCHEMA_VERSION = "awm.asset_allocation_agent_view.v1"
SNAPSHOT_SCHEMA_VERSION = "awm.asset_allocation_analysis_snapshot.v1"


_CATEGORY_BY_ASSET_CLASS = {
    "Cash": "cash",
    "US Treasury": "defensive_fixed_income",
    "Global Investment Grade Corporate Bond": "defensive_fixed_income",
    "Global High Yield Bond BB-B": "growth_fixed_income",
    "Emerging Market Local Currency Government Bonds": "growth_fixed_income",
    "Emerging Market Hard Currency Debt": "growth_fixed_income",
    "US Equity": "equity",
    "Dev. Europe ex UK Equity": "equity",
    "Japan Equity": "equity",
    "China Equity": "equity",
    "India Equity": "equity",
    "Commodities": "real_assets_and_alternatives",
    "Gold": "real_assets_and_alternatives",
    "Hedge Funds": "real_assets_and_alternatives",
    "Bitcoin": "real_assets_and_alternatives",
}


def build_asset_allocation_agent_view(
    *,
    allocation_id: Optional[str],
    full_result: Mapping[str, Any],
    source_assessment: Mapping[str, Any],
    authoritative_inputs: Mapping[str, Any],
    authorization: Mapping[str, Any],
    analysis_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Precompute the facts and summaries needed for safe follow-up narration."""

    layers = full_result.get("layers") if isinstance(full_result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    layer2 = layers.get("layer2") if isinstance(layers.get("layer2"), dict) else {}
    weights = (
        layer1.get("selected_weights")
        if isinstance(layer1.get("selected_weights"), dict)
        else {}
    )
    checks = (
        full_result.get("constraint_checks")
        if isinstance(full_result.get("constraint_checks"), dict)
        else {}
    )
    target_check = (
        checks.get("target_volatility")
        if isinstance(checks.get("target_volatility"), dict)
        else {}
    )
    security_dollar_check = (
        checks.get("security_dollar_sum")
        if isinstance(checks.get("security_dollar_sum"), dict)
        else {}
    )
    security_weight_check = (
        checks.get("security_weight_sum")
        if isinstance(checks.get("security_weight_sum"), dict)
        else {}
    )
    securities = [
        _json_copy(item)
        for item in full_result.get("securities") or []
        if isinstance(item, dict)
    ]
    sorted_securities = sorted(
        securities,
        key=lambda item: _number(item.get("weight")) or 0.0,
        reverse=True,
    )
    category_weights: Dict[str, float] = {}
    for asset_class, raw_weight in weights.items():
        weight = _number(raw_weight)
        if weight is None:
            continue
        category = _CATEGORY_BY_ASSET_CLASS.get(str(asset_class), "other")
        category_weights[category] = category_weights.get(category, 0.0) + weight
    sleeve_weights = {"active": 0.0, "passive": 0.0, "other": 0.0}
    for security in securities:
        weight = _number(security.get("weight"))
        if weight is None:
            continue
        sleeve = str(security.get("security_type") or "").strip().lower()
        sleeve = sleeve if sleeve in {"active", "passive"} else "other"
        sleeve_weights[sleeve] += weight

    typed_metrics: Dict[str, Dict[str, Any]] = {}

    def metric(
        key: str,
        value: Any,
        unit: str,
        source_path: str,
        *,
        derivation: str = "model_output_identity",
    ) -> None:
        if value is None:
            return
        typed_metrics[key] = {
            "value": _json_copy(value),
            "unit": unit,
            "source_path": source_path,
            "provenance": {
                "source_path": source_path,
                "derivation": derivation,
            },
        }

    metric(
        "target_volatility_annual_decimal",
        _number(target_check.get("target_annual_decimal")),
        "annual_decimal_0_to_1",
        "$.full_result.constraint_checks.target_volatility.target_annual_decimal",
    )
    metric(
        "target_volatility_difference_bps",
        _number(target_check.get("difference_bps")),
        "basis_points",
        "$.full_result.constraint_checks.target_volatility.difference_bps",
    )
    metric(
        "target_volatility_tolerance_bps",
        _number(target_check.get("allowed_tolerance_bps")),
        "basis_points",
        "$.full_result.constraint_checks.target_volatility.allowed_tolerance_bps",
    )
    metric(
        "target_volatility_passed",
        target_check.get("passed")
        if isinstance(target_check.get("passed"), bool)
        else None,
        "boolean",
        "$.full_result.constraint_checks.target_volatility.passed",
    )
    metric(
        "active_risk_percentage",
        _number(layer2.get("active_risk_pct")),
        "weight_0_to_1",
        "$.full_result.layers.layer2.active_risk_pct",
    )
    metric(
        "passive_risk_percentage",
        _number(layer2.get("passive_risk_pct")),
        "weight_0_to_1",
        "$.full_result.layers.layer2.passive_risk_pct",
    )
    metric(
        "active_sleeve_weight",
        sleeve_weights["active"],
        "weight_0_to_1",
        "$.asset_allocation_agent_view.sleeves.active.weight",
        derivation="sum_security_weights_by_security_type",
    )
    metric(
        "passive_sleeve_weight",
        sleeve_weights["passive"],
        "weight_0_to_1",
        "$.asset_allocation_agent_view.sleeves.passive.weight",
        derivation="sum_security_weights_by_security_type",
    )
    metric(
        "security_count",
        len(securities),
        "count",
        "$.asset_allocation_agent_view.holdings.security_count",
        derivation="count_normalized_securities",
    )
    metric(
        "asset_class_count",
        len(weights),
        "count",
        "$.asset_allocation_agent_view.allocation.asset_class_count",
        derivation="count_selected_asset_classes",
    )
    metric(
        "security_weight_sum",
        _number(security_weight_check.get("observed")),
        "weight_0_to_1",
        "$.full_result.constraint_checks.security_weight_sum.observed",
    )
    metric(
        "security_dollar_sum",
        _number(security_dollar_check.get("observed_usd")),
        "USD",
        "$.full_result.constraint_checks.security_dollar_sum.observed_usd",
    )
    for category, weight in sorted(category_weights.items()):
        metric(
            f"category_weight.{category}",
            weight,
            "weight_0_to_1",
            f"$.asset_allocation_agent_view.category_weights.{category}",
            derivation="sum_selected_asset_class_weights_by_category",
        )

    top_holdings = [
        {
            "ticker": item.get("ticker") or item.get("isin"),
            "asset_class": item.get("asset_class"),
            "security_type": item.get("security_type"),
            "weight": item.get("weight"),
            "amount": item.get("amount"),
        }
        for item in sorted_securities[:10]
    ]
    exclusions = [
        str(item)
        for item in full_result.get("excluded_asset_classes") or []
        if str(item).strip()
    ]
    mandate = authorization.get("mandate") if isinstance(authorization.get("mandate"), dict) else {}
    return {
        "schema_version": AGENT_VIEW_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "allocation_id": allocation_id,
        "created_at": created_at,
        "source_assessment": _json_copy(source_assessment),
        "mandate": {
            **_json_copy(mandate),
            "total_investment": authoritative_inputs.get("total_investment"),
            "target_volatility_annual_decimal": authoritative_inputs.get(
                "target_volatility"
            ),
            "target_volatility_tolerance_bps": authoritative_inputs.get(
                "target_volatility_tolerance_bps"
            ),
            "active_risk_percentage": authoritative_inputs.get(
                "active_risk_percentage"
            ),
            "excluded_asset_classes": exclusions,
        },
        "portfolio": {
            "expected_return_annual_decimal": full_result.get(
                "portfolio_expected_return_annual_decimal"
            ),
            "expected_volatility_annual_decimal": full_result.get(
                "portfolio_expected_volatility_annual_decimal"
            ),
            "target_volatility": _json_copy(target_check),
        },
        "allocation": {
            "asset_class_count": len(weights),
            "weights": _json_copy(weights),
            "category_weights": category_weights,
        },
        "sleeves": {
            "active": {
                "weight": sleeve_weights["active"],
                "signed_risk_percentage": layer2.get("active_risk_pct"),
            },
            "passive": {
                "weight": sleeve_weights["passive"],
                "signed_risk_percentage": layer2.get("passive_risk_pct"),
            },
        },
        "holdings": {
            "security_count": len(securities),
            "top_holdings": top_holdings,
            "all_securities": securities,
        },
        "reconciliation": {
            "asset_weights_passed": _check_passed(checks, "asset_weight_sum"),
            "security_weights_passed": _check_passed(checks, "security_weight_sum"),
            "security_dollars_passed": _check_passed(checks, "security_dollar_sum"),
            "security_weight_sum": security_weight_check.get("observed"),
            "security_dollar_sum": security_dollar_check.get("observed_usd"),
        },
        "constraint_summary": {
            "all_required_checks_passed": all(
                isinstance(value, dict) and value.get("passed") is True
                for value in checks.values()
            ),
            "failed_checks": [
                str(name)
                for name, value in checks.items()
                if not isinstance(value, dict) or value.get("passed") is not True
            ],
        },
        "typed_metrics": typed_metrics,
        "limitations": [
            {
                "code": "expected_metrics_not_guarantees",
                "meaning": "Expected return and volatility are modeled estimates, not guarantees.",
            },
            {
                "code": "risk_contribution_requires_separate_analysis",
                "meaning": (
                    "This optimizer result does not itself contain marginal or component "
                    "risk contributions; use the separate portfolio-risk tool."
                ),
            },
            {
                "code": "asset_location_requires_separate_analysis",
                "meaning": (
                    "This optimizer result does not itself assign holdings across "
                    "taxable and retirement accounts; use the separate asset-location tool."
                ),
            },
            {
                "code": "read_only_no_execution",
                "meaning": "The result does not save a proposal or execute trades.",
            },
        ],
        "follow_up_policy": {
            "answer_without_rerun": [
                "expected return, expected volatility, target tolerance, and constraint status",
                "asset-class, category, sleeve, security, and dollar explanation",
                "reconciliation, exclusions, assumptions, and limitations",
                "why the result is not a guarantee",
            ],
            "revised_signed_assessment_required": [
                "changed amount, target volatility, active-risk setting, exclusion, liquidity, or complexity preference",
            ],
            "separate_model_required": [
                "portfolio risk contribution or deterministic stress analysis through analyze_portfolio_risk",
                "tax-aware asset location through analyze_asset_location",
                "trade execution or proposal activation",
            ],
        },
    }


def build_asset_allocation_analysis_snapshot(
    *,
    client_id: str,
    session_id: str,
    full_result: Mapping[str, Any],
    recommendation_evidence: Mapping[str, Any],
    source_assessment: Mapping[str, Any],
    authoritative_inputs: Mapping[str, Any],
    authorization: Mapping[str, Any],
    allocation_id: Optional[str],
    agent_view: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build an immutable client-owned allocation result."""

    identity = {
        "client_id": client_id,
        "assessment_ref": source_assessment,
        "assessment_fingerprint": authorization.get("assessment_fingerprint"),
        "authoritative_inputs": authoritative_inputs,
        "allocation_id": allocation_id,
        "portfolio_metrics": {
            "expected_return": full_result.get(
                "portfolio_expected_return_annual_decimal"
            ),
            "expected_volatility": full_result.get(
                "portfolio_expected_volatility_annual_decimal"
            ),
        },
    }
    analysis_id = f"allocation_{canonical_fingerprint(identity)[:24]}"
    created_at = datetime.now(timezone.utc).isoformat()
    view = _json_copy(agent_view)
    view["analysis_id"] = analysis_id
    view["created_at"] = created_at
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "allocation_id": allocation_id,
        "client_id": client_id,
        "session_id": session_id,
        "created_at": created_at,
        "assessment_ref": _json_copy(source_assessment),
        "assessment_fingerprint": authorization.get("assessment_fingerprint"),
        "input_fingerprint": canonical_fingerprint(authoritative_inputs),
        "full_result": _json_copy(full_result),
        "recommendation_evidence": _json_copy(recommendation_evidence),
        "asset_allocation_agent_view": view,
    }


def _check_passed(checks: Mapping[str, Any], name: str) -> bool:
    value = checks.get(name)
    return isinstance(value, dict) and value.get("passed") is True


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
