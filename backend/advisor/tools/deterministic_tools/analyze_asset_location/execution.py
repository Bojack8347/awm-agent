"""Deterministic, capacity-constrained asset-location analysis."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from domain.finance.capital_markets import normalize_weights


_TAX_INEFFICIENCY_SCORE = {
    "Global High Yield Bond BB-B": 100,
    "Emerging Market Local Currency Government Bonds": 95,
    "Emerging Market Hard Currency Debt": 92,
    "Global Investment Grade Corporate Bond": 90,
    "US Treasury": 75,
    "Hedge Funds": 70,
    "Commodities": 65,
    "Cash": 60,
    "Bitcoin": 55,
    "Gold": 50,
    "India Equity": 35,
    "China Equity": 35,
    "Japan Equity": 30,
    "Dev. Europe ex UK Equity": 30,
    "US Equity": 25,
}


def run_asset_location_analysis(
    *,
    allocation: Mapping[str, Any],
    taxable_balance: float,
    retirement_balance: float,
) -> Dict[str, Any]:
    weights = normalize_weights(allocation)
    total = float(taxable_balance) + float(retirement_balance)
    if not weights or total <= 0 or taxable_balance < 0 or retirement_balance < 0:
        return {"success": False, "error": "asset_location_inputs_invalid"}
    unknown = sorted(set(weights) - set(_TAX_INEFFICIENCY_SCORE))
    if unknown:
        return {
            "success": False,
            "error": "asset_location_tax_profile_missing",
            "unsupported_asset_classes": unknown,
        }

    remaining_retirement = float(retirement_balance)
    rows = []
    for asset_class, weight in sorted(
        weights.items(),
        key=lambda item: (-_TAX_INEFFICIENCY_SCORE[item[0]], item[0]),
    ):
        target_amount = weight * total
        retirement_amount = min(target_amount, remaining_retirement)
        taxable_amount = target_amount - retirement_amount
        remaining_retirement -= retirement_amount
        rows.append(
            {
                "asset_class": asset_class,
                "portfolio_weight": weight,
                "target_amount": target_amount,
                "retirement_amount": retirement_amount,
                "taxable_amount": taxable_amount,
                "tax_inefficiency_score": _TAX_INEFFICIENCY_SCORE[asset_class],
            }
        )
    taxable_total = sum(row["taxable_amount"] for row in rows)
    retirement_total = sum(row["retirement_amount"] for row in rows)
    return {
        "success": True,
        "full_result": {
            "schema_version": "awm.asset_location_analysis.v1",
            "methodology_version": "awm.tax_efficiency_ordering.us.v1",
            "account_capacity": {
                "taxable_brokerage": float(taxable_balance),
                "retirement": float(retirement_balance),
            },
            "placements": rows,
            "reconciliation": {
                "target_total": total,
                "taxable_total": taxable_total,
                "retirement_total": retirement_total,
                "difference": total - taxable_total - retirement_total,
            },
            "limitations": [
                "This ordering does not use the client's tax bracket, cost basis, state-specific bond treatment, RMD plan, or security-level tax characteristics.",
                "It preserves asset-class totals but does not propose trades.",
                "It is planning analysis, not tax preparation or individualized tax advice.",
            ],
        },
    }
